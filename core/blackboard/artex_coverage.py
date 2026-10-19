"""ARTEX 任务范围资产与覆盖度（2026-10-19，artex-coverage）。

复刻上游 ARTEX 的 **task_scope + Coverage**（``db/task_scope.go``、
``db/task_assets.go``）：把「这个任务到底要测哪些资产」从模糊的自述变成
**可查询的锚点集合**，并据此算出粗粒度覆盖度，供 Planner 做量化验收核对
（``agent/planner.go:316``：目标含可量化条件时 prove_goal 前必须核对
coverage.pct，未达标禁止盖章）。

三处与上游对齐的口径：

1. **分母 = 任务范围资产**（``artex_task_assets``）。锚点由 Planner 落树时
   从各 Intent 的 ``asset_ids`` 自动登记（source='planner'），也可由主控/
   API 显式锚定（source='manual'/'api'）。任务未锚定任何资产时回退到
   「Intent 与注册目标的 asset_ids 并集」（``scope_source='inferred'``）——
   历史任务没有锚点行，直接给 0/0 会让量化验收核对无从下手。
2. **分子 = 被探索图 fact 节点锚定过的资产**。上游同口径：``exploration_anchors``
   里 ``kind='fact'`` 的节点所挂资产即「已测（约）」。这是**下限**——资产被测过
   但没产出事实就不计入；覆盖度是**参考指标，不是精确度量**（上游注释原文
   "a reference figure for the agent, NOT a precise metric"）。
3. **深度优先于覆盖度**（``agent/planner.go:330``）：coverage 是下限/验收项、
   不是探索目标本身。本模块只把数算准，纪律由 Planner 提示词承载。

资产类型（``assets.type``）在本项目是 host/domain/service/url/binary/challenge
等，与上游 company/root_domain/subdomain/ip/service/app/endpoint 命名不同但
语义同层；本模块按**本项目自有类型**分桶，不做有损映射。
"""
from __future__ import annotations

import logging
from typing import Any

from core.blackboard.artex_exploration import list_nodes
from core.blackboard.artex_intents import list_intents
from core.blackboard.store import _row_to_dict, now

log = logging.getLogger(__name__)

# 锚点来源：Planner 落树自动 / 主控或 API 显式。
ANCHOR_SOURCES = frozenset({"planner", "manual", "api"})
_MAX_ANCHOR_BATCH = 200


# ---------- 任务范围资产锚定 ----------


def anchor_task_assets(*, bb, project_id: str, task_id: str, asset_ids: Any,
                       source: str = "manual", note: str = "") -> dict:
    """把资产登记为任务范围（幂等，PK=(project_id,task_id,asset_id)）。

    只接受**本项目真实存在**的资产：不存在的 id 进 ``missing`` 而不落行——
    范围锚点是覆盖度分母，凭幻觉 id 虚增分母会让覆盖度失去意义。返回
    ``{"added","existing","missing","total"}``。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    src = str(source or "manual").strip().lower()
    if src not in ANCHOR_SOURCES:
        src = "manual"
    note = str(note or "")[:500]
    raw = [str(x).strip() for x in (asset_ids or []) if str(x).strip()]
    # 去重保序；单批上限防一次灌爆（与上游 MaxTaskAssetMutationCount 同精神）。
    ids = list(dict.fromkeys(raw))[:_MAX_ANCHOR_BATCH]
    added: list[str] = []
    existing: list[str] = []
    missing: list[str] = []
    ts = now()
    for aid in ids:
        if bb.get_asset(aid) is None:
            missing.append(aid)
            continue
        row = bb.conn.execute(
            "SELECT 1 FROM artex_task_assets WHERE project_id=? AND task_id=? AND asset_id=?",
            (project_id, task_id, aid)).fetchone()
        if row:
            existing.append(aid)
            continue
        with bb._tx():
            bb.conn.execute(
                "INSERT INTO artex_task_assets(project_id,task_id,asset_id,source,note,created_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,task_id,asset_id) DO NOTHING",
                (project_id, task_id, aid, src, note, ts))
        added.append(aid)
    if added:
        bb.append_event(project_id, "artex.task.assets.anchored", {
            "task_id": task_id, "asset_ids": added, "source": src,
            "note": note})
    return {"added": added, "existing": existing, "missing": missing,
            "total": len(added) + len(existing)}


def list_task_assets(bb, project_id: str, task_id: str) -> list[dict]:
    """任务范围资产行（按登记顺序）。"""
    rows = bb.conn.execute(
        "SELECT * FROM artex_task_assets WHERE project_id=? AND task_id=?"
        " ORDER BY created_at ASC, asset_id ASC",
        (project_id, str(task_id or ""))).fetchall()
    return [_row_to_dict(r) or {} for r in rows]


def unanchor_task_assets(*, bb, project_id: str, task_id: str,
                         asset_ids: Any = None) -> dict:
    """摘除任务范围资产；``asset_ids`` 为空则清空本任务全部锚点。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    ids = [str(x).strip() for x in (asset_ids or []) if str(x).strip()]
    with bb._tx():
        if ids:
            marks = ",".join("?" for _ in ids)
            cur = bb.conn.execute(
                f"DELETE FROM artex_task_assets WHERE project_id=? AND task_id=?"
                f" AND asset_id IN ({marks})", (project_id, task_id, *ids))
        else:
            cur = bb.conn.execute(
                "DELETE FROM artex_task_assets WHERE project_id=? AND task_id=?",
                (project_id, task_id))
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if removed:
        bb.append_event(project_id, "artex.task.assets.unanchored", {
            "task_id": task_id, "asset_ids": ids, "removed": removed})
    return {"removed": removed, "remaining": len(list_task_assets(bb, project_id, task_id))}


# ---------- 覆盖度计算 ----------


def _coverage_enabled(bb, project_id: str) -> bool:
    """覆盖度功能开关（``config.artex.coverage_enabled``）；缺省/读失败 = 开。

    上游 ``CoverageEnabled`` fail-open 到 true，让未知/历史任务保持既有行为。
    """
    try:
        row = bb.get_project(project_id) or {}
        raw = row.get("config")
        import json
        cfg = json.loads(raw) if isinstance(raw, str) else (raw or {})
        artex = (cfg or {}).get("artex") or {}
        if "coverage_enabled" in artex:
            return bool(artex.get("coverage_enabled"))
    except Exception:  # noqa: BLE001
        pass
    return True


def _task_intents(bb, project_id: str, task_id: str) -> list[dict]:
    return [row for row in list_intents(bb, project_id, limit=2000)
            if str(row.get("task_id") or "") == task_id]


def scope_asset_ids(bb, project_id: str, task_id: str) -> tuple[list[str], str]:
    """任务的覆盖度分母资产 id 与来源。

    优先取 ``artex_task_assets`` 锚点（``scope_source='anchored'``）；无锚点时
    回退「Intent ∪ 注册目标」的 asset_ids 并集（``'inferred'``）——历史任务
    与手工建树没有锚点行，回退让覆盖度仍有意义。
    """
    task_id = str(task_id or "").strip()
    anchored = [str(row.get("asset_id") or "")
                for row in list_task_assets(bb, project_id, task_id)
                if str(row.get("asset_id") or "")]
    if anchored:
        return anchored, "anchored"
    ids: list[str] = []
    for intent in _task_intents(bb, project_id, task_id):
        ids.extend(str(x) for x in (intent.get("asset_ids") or []) if str(x))
    try:
        from core.blackboard.artex_goals import registered_goals
        for goal in registered_goals(bb, project_id, task_id):
            ids.extend(str(x) for x in (goal.get("asset_ids") or []) if str(x))
    except Exception:  # noqa: BLE001 —— 目标表缺失/旧库不影响分母推导
        pass
    return list(dict.fromkeys(ids)), "inferred"


def tested_asset_ids(bb, project_id: str, task_id: str) -> set[str]:
    """被本任务探索图 ``fact`` 节点锚定过的资产 id 集合（覆盖度分子）。

    上游只认 ``kind='fact'`` 的锚点——finding 不计入（它是 fact 的下游产物）。
    这是**下限**：资产被测过却没产出事实就不算已测。
    """
    tested: set[str] = set()
    for node in list_nodes(bb, project_id, task_id=str(task_id or ""), kind="fact"):
        for aid in node.get("asset_ids") or []:
            sid = str(aid).strip()
            if sid:
                tested.add(sid)
    return tested


def task_coverage(bb, project_id: str, task_id: str) -> dict:
    """任务粗粒度覆盖度（**按轨分派**：CTF 轨走题目覆盖度）。

    返回 ``{enabled, scope_rows, scope_source, denominator, tested, untested,
    pct, by_type}``——上游 ``Coverage`` 结构的超集（多 ``scope_source`` /
    ``untested``）。``pct`` 分母为 0 时是 ``None``（不臆造 100%）。已删除的
    资产不计入分母（锚点行可能悬空）。

    **CTF 轨为什么要分派**（challenge-flag-ledger v47）：题目不是待测资产、
    flag 也不是 fact。用资产口径算出来是无意义的数字（「已测资产 0/3，覆盖度
    0%」），而且一旦有 finding 被当证据挂到目标上，还会诱发「有发现即达成」
    的假收敛。CTF 轨的正确口径是**分母 = 题目数、分子 = 已解题目数**。
    """
    task_id = str(task_id or "").strip()
    if not _coverage_enabled(bb, project_id):
        return {"enabled": False, "scope_rows": 0, "scope_source": "",
                "denominator": 0, "tested": 0, "untested": 0, "pct": None,
                "by_type": []}
    ctf = _ctf_challenge_coverage(bb, project_id, task_id)
    if ctf is not None:
        return ctf
    scope_ids, scope_source = scope_asset_ids(bb, project_id, task_id)
    tested = tested_asset_ids(bb, project_id, task_id)
    live = {str(a.get("id")): a for a in bb.list_assets(project_id)}
    by_type: dict[str, dict] = {}
    denominator = 0
    tested_count = 0
    for aid in scope_ids:
        asset = live.get(aid)
        if asset is None:  # 资产已删：锚点悬空，不计入分母
            continue
        type_ = str(asset.get("type") or "unknown")
        slot = by_type.setdefault(type_, {"type": type_, "total": 0, "tested": 0})
        slot["total"] += 1
        denominator += 1
        if aid in tested:
            slot["tested"] += 1
            tested_count += 1
    pct = (tested_count / denominator) if denominator else None
    return {"enabled": True,
            "scope_rows": len(list_task_assets(bb, project_id, task_id)),
            "scope_source": scope_source,
            "denominator": denominator, "tested": tested_count,
            "untested": denominator - tested_count, "pct": pct,
            "by_type": [by_type[k] for k in sorted(by_type)]}


def _ctf_challenge_coverage(bb, project_id: str, task_id: str) -> dict | None:
    """CTF 轨的覆盖度（分母=题目、分子=已解）；非 CTF 轨返回 None（走资产口径）。

    判定条件：本项目**有** challenge 根资产。有题目 = 这是 CTF 项目，资产
    覆盖度对它没有意义（题目刻意不进 ``COVERAGE_TYPES``，见 assets.py 注释）。

    范围题目取：任务锚点里的题目 ∪ 任务 Intent 锚定的题目；都为空时退化为
    **全部题目**（CTF 项目常见「一个任务打全部题」）。
    """
    try:
        assets = bb.list_assets(project_id)
    except Exception:  # noqa: BLE001
        return None
    all_cids = [a["id"] for a in assets
                if a.get("type") == "challenge" and not a.get("parent_id")]
    if not all_cids:
        return None
    cid_set = set(all_cids)
    scoped: list[str] = []
    for aid in scope_asset_ids(bb, project_id, task_id)[0]:
        if aid in cid_set and aid not in scoped:
            scoped.append(aid)
    if not scoped:
        scoped = all_cids
    running: dict[str, int] = {}
    try:
        import json as _json
        for row in bb.conn.execute(
                "SELECT asset_ids FROM artex_intents WHERE project_id=? "
                "AND task_id=? AND status IN ('claimed','running')",
                (project_id, str(task_id or ""))).fetchall():
            try:
                ids = _json.loads(row["asset_ids"] or "[]")
            except (TypeError, ValueError):
                ids = []
            for aid in ids or []:
                sid = str(aid or "")
                if sid in cid_set:
                    running[sid] = running.get(sid, 0) + 1
                    break
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.blackboard.challenge_flags import challenge_coverage
        return challenge_coverage(bb, project_id, scoped, running)
    except Exception:  # noqa: BLE001 —— flag 表缺失时退回资产口径，不炸覆盖度
        log.exception("CTF 题目覆盖度计算失败 project=%s task=%s",
                      project_id, task_id)
        return None


def list_untested_assets(bb, project_id: str, task_id: str, *,
                         type_: str | None = None, limit: int = 10,
                         offset: int = 0) -> dict:
    """范围内尚未被事实锚定过的资产（可补测清单），按类型过滤 + 分页。

    返回 ``{"assets": [{"id","type","label","status"}], "total", "type"}``。
    """
    task_id = str(task_id or "").strip()
    scope_ids, _ = scope_asset_ids(bb, project_id, task_id)
    tested = tested_asset_ids(bb, project_id, task_id)
    live = {str(a.get("id")): a for a in bb.list_assets(project_id)}
    filt = str(type_ or "").strip()
    rows: list[dict] = []
    for aid in scope_ids:
        asset = live.get(aid)
        if asset is None or aid in tested:
            continue
        type_ = str(asset.get("type") or "unknown")
        if filt and type_ != filt:
            continue
        rows.append({"id": aid, "type": type_,
                     "label": str(asset.get("value") or ""),
                     "status": str(asset.get("status") or "")})
    total = len(rows)
    limit = max(1, min(int(limit or 10), 200))
    offset = max(0, int(offset or 0))
    return {"assets": rows[offset:offset + limit], "total": total,
            "type": filt}


def coverage_graph(bb, project_id: str, task_id: str) -> dict:
    """任务覆盖图：范围内资产 + 为连边拉入的范围外祖先（灰）。

    节点 ``{key,kind,label,tested,in_scope,asset_id,status,parent_id}``；
    边是「子 → 父」包含边（取自 ``assets.parent_id``）。范围外祖先只是连接
    上下文（in_scope=False），不计入覆盖度分母。
    """
    task_id = str(task_id or "").strip()
    scope_ids, _ = scope_asset_ids(bb, project_id, task_id)
    tested = tested_asset_ids(bb, project_id, task_id)
    live = {str(a.get("id")): a for a in bb.list_assets(project_id)}
    in_scope = {aid for aid in scope_ids if aid in live}
    nodes: dict[str, dict] = {}
    edge_set: set[tuple[str, str]] = set()
    edges: list[dict] = []

    def _key(aid: str) -> str:
        return f"a:{aid}"

    def _add(asset: dict, in_scope_flag: bool) -> dict:
        k = _key(str(asset.get("id")))
        node = nodes.get(k)
        if node is None:
            node = {"key": k, "kind": str(asset.get("type") or "unknown"),
                    "label": str(asset.get("value") or ""),
                    "tested": str(asset.get("id")) in tested,
                    "in_scope": in_scope_flag,
                    "asset_id": str(asset.get("id")),
                    "status": str(asset.get("status") or ""),
                    "parent_id": asset.get("parent_id")}
            nodes[k] = node
        elif in_scope_flag:
            node["in_scope"] = True
        return node

    for aid in sorted(in_scope):
        _add(live[aid], True)

    def _link(child_key: str, parent_id: Any) -> None:
        pid = str(parent_id or "")
        if not pid or pid not in live or pid == child_key[2:]:
            return
        pk = _key(pid)
        if pk not in nodes:
            _add(live[pid], False)  # 范围外祖先：连接上下文
        pair = (child_key, pk)
        if pair not in edge_set:
            edge_set.add(pair)
            edges.append({"src": child_key, "dst": pk})

    for aid in sorted(in_scope):
        cur_id = aid
        visited = {aid}
        parent = live[aid].get("parent_id")
        while parent and str(parent) in live and str(parent) not in visited:
            pid = str(parent)
            visited.add(pid)
            _link(_key(cur_id), pid)
            if pid in in_scope:
                break  # 已入范围：边连到它即止，不再向上攀
            cur_id = pid
            parent = live[pid].get("parent_id")
    return {"task_id": task_id, "nodes": list(nodes.values()), "edges": edges}
