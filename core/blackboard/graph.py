"""任务流图组装（A3，DESIGN.md §12 直播间任务流视图）。

只读组装，不写存储。三类边：
- parent 实线：tasks.parent_id（人/编排/子代理分解结构）；
- inbox 虚线：session_inbox 按 (kind, ref_id) 聚类——同一依据（被撤回/增补的 finding）
  触达 ≥2 个会话，而会话各自映射到「最近任务」后，任务两两连边；
- suggest 点虚线（机制 1.1）：同父任务或 context_refs 相交、却尚无私信记录的两个已认领
  任务——机器猜测"这两个子代理很可能需要交流"，不落库、前端可关。

查询条数固定（tasks/sessions/窗口映射/inbox/finding 标题共 5 条以内），与任务量无关，
无 N+1（tests/test_blackboard.py 用 set_trace_callback 守查询条数上限）。
"""

import json
from itertools import combinations
from typing import Any

from core.blackboard.tasks import TaskQueue

_INBOX_KINDS = ("basis_stale", "finding_update")


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def task_graph(bb: Any, project_id: str) -> dict[str, Any]:
    """返回 {nodes, edges}。节点含全部任务（四态皆保留，节点持久）+ 认领会话信息。"""
    tq = TaskQueue(bb)
    tasks = tq.list_tasks(project_id)  # 1 条 SQL，出口已解析 JSON 列

    # role 随节点下发：前端把存量会话（name=英文 role id）显示层映射为角色中文名
    sessions = {
        r["id"]: {"id": r["id"], "name": r["name"], "role": r["role"], "status": r["status"]}
        for r in bb.conn.execute(
            "SELECT id, name, role, status FROM sessions WHERE project_id=?", (project_id,))
    }

    # 会话→最近任务：claimed 优先 0，其余 1；同档按 updated_at 倒序，每组取首行
    mapped_rows = bb.conn.execute(
        "WITH ranked AS ("
        "  SELECT id, claimed_by, ROW_NUMBER() OVER ("
        "    PARTITION BY claimed_by"
        "    ORDER BY CASE status WHEN 'claimed' THEN 0 ELSE 1 END, updated_at DESC"
        "  ) AS rn FROM tasks WHERE project_id=? AND claimed_by IS NOT NULL"
        ") SELECT id, claimed_by FROM ranked WHERE rn=1",
        (project_id,),
    ).fetchall()
    # closed 会话不映射（窗口已关，私信只作历史审计，不构协作边）
    sess_task = {
        r["claimed_by"]: r["id"]
        for r in mapped_rows
        if sessions.get(r["claimed_by"], {}).get("status") != "closed"
    }

    nodes: list[dict[str, Any]] = []
    for t in tasks:
        sid = t.get("claimed_by")
        nodes.append({
            "id": t["id"],
            "objective": t["objective"],
            "task_type": t["task_type"],
            "status": t["status"],
            "priority": t["priority"],
            "noise_budget": t["noise_budget"],
            "parent_id": t.get("parent_id"),
            "claimed_by": sid,
            "target_session": t.get("target_session") or "",  # v0.71：专属执行窗（双击直开）
            "plan": t.get("plan", []),
            "updated_at": t.get("updated_at"),
            "attempts": len((t.get("context") or {}).get("attempts") or []),  # C10 履历数
            "session": sessions.get(sid) if sid else None,
        })

    edges: list[dict[str, Any]] = []
    node_ids = {t["id"] for t in tasks}
    for t in tasks:
        pid = t.get("parent_id")
        if pid and pid in node_ids:
            edges.append({"id": f"parent:{t['id']}", "source": pid,
                          "target": t["id"], "kind": "parent"})

    # inbox 聚类 → 任务对（同任务对多簇合并去重）
    placeholders = ",".join("?" * len(_INBOX_KINDS))
    inbox_rows = bb.conn.execute(
        f"SELECT to_session, kind, ref_id, payload FROM session_inbox"
        f" WHERE project_id=? AND kind IN ({placeholders})",
        (project_id, *_INBOX_KINDS),
    ).fetchall()
    clusters: dict[tuple[str, str], dict[str, list[str]]] = {}
    payload_titles: dict[str, str] = {}
    for r in inbox_rows:
        key = (r["kind"], r["ref_id"])
        clusters.setdefault(key, {"sessions": []})
        if r["to_session"] not in clusters[key]["sessions"]:
            clusters[key]["sessions"].append(r["to_session"])
        title = _loads(r["payload"], {}).get("title")
        if isinstance(title, str) and title and r["ref_id"] not in payload_titles:
            payload_titles[r["ref_id"]] = title

    ref_ids = sorted({ref for _, ref in clusters})
    finding_titles: dict[str, str] = {}
    if ref_ids:
        ph = ",".join("?" * len(ref_ids))
        finding_titles = {
            r["id"]: r["title"]
            for r in bb.conn.execute(
                f"SELECT id, title FROM findings WHERE project_id=? AND id IN ({ph})",
                (project_id, *ref_ids),
            )
        }

    pair_refs: dict[tuple[str, str], list[dict[str, str]]] = {}
    for (kind, ref_id), info in clusters.items():
        task_set = {sess_task[s] for s in info["sessions"] if s in sess_task}
        if len(task_set) < 2:
            continue
        ref = {
            "kind": kind,
            "ref_id": ref_id,
            "title": finding_titles.get(ref_id) or payload_titles.get(ref_id, ""),
        }
        for a, b in combinations(sorted(task_set), 2):
            pair_refs.setdefault((a, b), []).append(ref)

    for (a, b), refs in sorted(pair_refs.items()):
        # 同 (kind, ref_id) 只可能出现一次；防御性去重。id 只取任务对，轮询间稳定
        uniq: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            k = (ref["kind"], ref["ref_id"])
            if k not in seen:
                seen.add(k)
                uniq.append(ref)
        edges.append({"id": f"inbox:{a}:{b}", "source": a, "target": b,
                      "kind": "inbox", "refs": uniq})

    # 机制 1.1 建议私信边：同父或依据（context_refs）相交、都已认领、且尚无 inbox 边——
    # 点虚线提示"很可能需要交流"，机器猜测不落库（inbox 边出现后自然消失）
    task_by_id = {t["id"]: t for t in tasks}
    inbox_pairs = {(e["source"], e["target"]) for e in edges if e["kind"] == "inbox"}
    parent_pairs = {(e["source"], e["target"]) for e in edges if e["kind"] == "parent"}
    claimed = [t for t in tasks if t.get("claimed_by")]
    for a, b in combinations(sorted(t["id"] for t in claimed), 2):
        if (a, b) in inbox_pairs or (a, b) in parent_pairs:
            continue
        ta, tb = task_by_id[a], task_by_id[b]
        same_parent = ta.get("parent_id") and ta["parent_id"] == tb.get("parent_id")
        shared_refs = set(ta.get("context_refs", [])) & set(tb.get("context_refs", []))
        if same_parent or shared_refs:
            edges.append({"id": f"suggest:{a}:{b}", "source": a, "target": b,
                          "kind": "suggest"})

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# 黑板链路图（2026-09-20，DESIGN.md §12「黑板链路图」定稿块）
#
# 五类对象（asset/func_kb/finding/artifact/task）× 类型分层 DAG 的只读组装。
# 边口径定稿（9 种 kind，全部来自既有字段，无新表）：
# - asset_parent   asset→asset      assets.parent_id 外键（父不在节点集跳过）
# - func_of        func→asset       func_kb.binary_sha256 == assets(type=binary).value
#                                   （sha 匹配非外键，内存建 map）
# - targets        finding→asset    findings.target_asset_id 外键
# - relates_to     finding→finding  evidence.relates_to（E0 强校验；note 作边 label）
# - poc            finding→artifact findings.poc_artifact_id 外键
# - basis          task→finding     tasks.context_refs；ref ∈ stale_refs 时边带
#                                   stale:true——收录+标记不排除（全景审计视图，
#                                   丢边比多一条淡边危险）
# - task_parent    task→task        tasks.parent_id 外键
# - artifact_task  artifact→task    artifacts.meta.task_id，**只收孤儿 artifact**
#                                   （无任何 findings.poc_artifact_id 指回的）——
#                                   有 poc 边的产物已经 finding→poc+task→basis
#                                   连通，再收会成冗余三角
# - chain          链内相邻         chains JOIN chain_links 按 seq 相邻成对；两端
#                                   实体已删（不在节点集）的段跳过
# 查询条数固定 ≤6 条（assets/funcs/findings/artifacts/tasks/chains+links 各一），
# 与数据量无关，无 N+1（tests 用 set_trace_callback 守上限）。
# ---------------------------------------------------------------------------

def board_graph(bb: Any, project_id: str) -> dict[str, Any]:
    """返回 {nodes, edges}。节点 id 用原始 id 不前缀化（与 chain_links.node_id 同口径）；
    label/sub 由服务端拼好，前端零二次映射。"""
    assets = bb.list_assets(project_id)          # 1：meta 已解析
    funcs = bb.list_funcs(project_id)            # 2：全量（不按 sha 过滤）
    findings = bb.list_findings(project_id)      # 3：evidence 已解析
    artifacts = bb.list_artifacts(project_id)    # 4：meta 仍是 JSON 文本，自行 _loads
    tasks = TaskQueue(bb).list_tasks(project_id)  # 5：context_refs/stale_refs 已解析
    chain_rows = bb.conn.execute(                # 6：链+节点一条 JOIN，按链内 seq 序
        "SELECT l.node_type, l.node_id, l.seq, l.edge_note,"
        " c.id AS chain_id, c.name AS chain_name, c.status AS chain_status"
        " FROM chains c JOIN chain_links l ON l.chain_id=c.id"
        " WHERE c.project_id=? AND c.origin='manual' ORDER BY c.id, l.seq",
        (project_id,)).fetchall()  # v19：origin 过滤（R6）——任务轨迹自动链（origin='trace'）不进全景图

    def _short(text: Any, cap: int = 48) -> str:
        s = str(text or "")
        return s if len(s) <= cap else s[:cap - 1] + "…"

    nodes: list[dict[str, Any]] = []
    binary_by_sha: dict[str, str] = {}  # sha → binary asset id（func_of 边用）
    for a in assets:
        if a.get("type") == "binary" and a.get("value"):
            binary_by_sha.setdefault(a["value"], a["id"])
        value = str(a.get("value") or "")
        label = f"binary:{value[:12]}" if a.get("type") == "binary" else value
        nodes.append({
            "id": a["id"], "node_type": "asset", "label": label,
            "sub": a.get("type") or "", "status": a.get("status"),
            "created_at": a.get("created_at"),
            "type": a.get("type"), "value": value, "parent_id": a.get("parent_id"),
        })

    for f in funcs:
        name = str(f.get("name") or "")
        nodes.append({
            "id": f["id"], "node_type": "func_kb",
            "label": name or f"func@{f.get('address')}",
            "sub": str(f.get("address") or ""), "status": None,
            "created_at": f.get("updated_at"),
            "name": name, "address": f.get("address"),
            "risk_tags": f.get("risk_tags") or [],
            "binary_sha256": f.get("binary_sha256"),
        })

    pocs_referred: set[str] = set()  # 被 poc_artifact_id 指回的产物（孤儿判定用）
    for fd in findings:
        if fd.get("poc_artifact_id"):
            pocs_referred.add(fd["poc_artifact_id"])
        nodes.append({
            "id": fd["id"], "node_type": "finding", "label": _short(fd.get("title")),
            "sub": f"{fd.get('severity')}/{fd.get('vuln_class') or 'rev'}",
            "status": fd.get("status"), "created_at": fd.get("created_at"),
            "title": fd.get("title"), "severity": fd.get("severity"),
            "category": fd.get("category"),
            "vuln_class": fd.get("vuln_class"),
            "author": fd.get("author"),  # 2026-09-20 对话化：sess- 前缀=对话轮产出
            "target_asset_id": fd.get("target_asset_id"),
            "poc_artifact_id": fd.get("poc_artifact_id"),
        })

    for art in artifacts:
        meta = _loads(art.get("meta"), {})
        nodes.append({
            "id": art["id"], "node_type": "artifact",
            "label": _short(art.get("path")), "sub": art.get("kind") or "file",
            "status": None, "created_at": art.get("created_at"),
            "path": art.get("path"), "kind": art.get("kind"),
            "description": _short(art.get("description"), 120),
            "task_id": meta.get("task_id"),
        })

    for t in tasks:
        nodes.append({
            "id": t["id"], "node_type": "task", "label": _short(t.get("objective"), 64),
            "sub": t.get("status") or "", "status": t.get("status"),
            "created_at": t.get("created_at"),
            "objective": t.get("objective"), "task_type": t.get("task_type"),
            "priority": t.get("priority"), "parent_id": t.get("parent_id"),
            "claimed_by": t.get("claimed_by"),
        })

    node_ids = {n["id"] for n in nodes}

    def _edge(kind: str, source: str, target: str, **extra: Any) -> dict[str, Any] | None:
        # 悬挂引用统一出口：两端不在节点集即丢弃（模式同 task_graph 的 parent 边）
        if source not in node_ids or target not in node_ids or source == target:
            return None
        return {"id": f"{kind}:{source}:{target}", "source": source,
                "target": target, "kind": kind, **extra}

    edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()

    def _add(e: dict[str, Any] | None) -> None:
        if e and e["id"] not in seen_edges:
            seen_edges.add(e["id"])
            edges.append(e)

    for a in assets:  # asset_parent
        _add(_edge("asset_parent", a["parent_id"], a["id"]) if a.get("parent_id") else None)
    for f in funcs:  # func_of（sha 匹配）
        aid = binary_by_sha.get(f.get("binary_sha256") or "")
        if aid:
            _add(_edge("func_of", f["id"], aid))
    for fd in findings:  # targets / relates_to / poc
        if fd.get("target_asset_id"):
            _add(_edge("targets", fd["id"], fd["target_asset_id"]))
        for rel in (fd.get("evidence") or {}).get("relates_to") or []:
            if isinstance(rel, dict) and rel.get("finding_id"):
                _add(_edge("relates_to", fd["id"], rel["finding_id"],
                           label=_short(rel.get("note"), 60)))
        if fd.get("poc_artifact_id"):
            _add(_edge("poc", fd["id"], fd["poc_artifact_id"]))
    for t in tasks:  # basis（stale 标记）/ task_parent
        stale = set(t.get("stale_refs") or [])
        for ref in t.get("context_refs") or []:
            _add(_edge("basis", t["id"], ref,
                       stale=True if ref in stale else None))
        if t.get("parent_id"):
            _add(_edge("task_parent", t["parent_id"], t["id"]))
    for art in artifacts:  # artifact_task：只收孤儿产物（防冗余三角）
        meta = _loads(art.get("meta"), {})
        tid = meta.get("task_id")
        if tid and art["id"] not in pocs_referred:
            _add(_edge("artifact_task", art["id"], tid))
    # chain：链内按 seq 相邻成对（link 的 node_id 同为原始 id）
    by_chain: dict[str, list[dict]] = {}
    for r in chain_rows:
        by_chain.setdefault(r["chain_id"], []).append(dict(r))
    for cid, links in by_chain.items():
        for prev, cur in zip(links, links[1:]):
            e = _edge("chain", prev["node_id"], cur["node_id"])
            if e:
                e["id"] = f"chain:{cid}:{cur['seq']}"  # 同节点对可属多条链，id 按链区分
                e.update({"chain_id": cid, "chain_name": prev["chain_name"],
                          "chain_status": prev["chain_status"],
                          "seq": cur["seq"], "edge_note": _short(prev["edge_note"], 60)})
                _add(e)

    return {"nodes": nodes, "edges": edges}
