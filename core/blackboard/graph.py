"""黑板只读图组装：session_graph（会话协作流，会话中心化 M4）+
board_graph（黑板链路图，2026-09-20）。

历史注记：task_graph（A3 任务流图，DESIGN.md §12）已随 TaskFlow 视图退役
（task-attempt-tree M2，2026-09-27——前端改用任务尝试树 /tree/{task_id}，
模块函数一并移除）。只读组装，不写存储，查询条数固定无 N+1。
"""

import json
from itertools import combinations
from typing import Any

_INBOX_KINDS = ("basis_stale", "finding_update")


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def session_graph(bb: Any, project_id: str) -> dict[str, Any]:
    """会话协作流（会话中心化 M4，2026-09-25；任务机制退役 2026-10-06）：
    节点=编排器+全部会话窗；边两类——delegate（orch→窗：该窗承接了 Team 成员执行）、
    inbox（同窗依据聚类）/dm（agent_message 定向私信，有向）。同节点对聚合成一条边、
    refs 带明细。查询条数固定。工作单元来自 team_run_members（不读旧 tasks）。"""
    members = bb.conn.execute(
        "SELECT rm.session_id, rm.objective, rm.status"
        " FROM team_run_members rm JOIN team_runs r ON r.id=rm.run_id"
        " WHERE r.project_id=? ORDER BY rm.created_at, rm.id",
        (project_id,)).fetchall()
    sess_rows = bb.conn.execute(
        "SELECT id, name, role, status FROM sessions WHERE project_id=?",
        (project_id,)).fetchall()
    sessions = {r["id"]: dict(r) for r in sess_rows}

    nodes: list[dict[str, Any]] = [
        {"id": "__orch", "kind": "orch", "label": "编排器",
         "name": "编排器", "role": "", "status": "orch"}]
    queue_by_sid: dict[str, int] = {}
    current_by_sid: dict[str, str] = {}
    for m in members:
        sid = m["session_id"] or ""
        if not sid:
            continue
        if m["status"] in ("running", "creating"):
            current_by_sid.setdefault(sid, m["objective"])
        elif m["status"] == "pending":
            queue_by_sid[sid] = queue_by_sid.get(sid, 0) + 1
    for sid, s in sessions.items():
        nodes.append({
            "id": sid, "kind": "session", "label": s["name"] or s["role"],
            "name": s["name"], "role": s["role"], "status": s["status"],
            "current": current_by_sid.get(sid, ""),
            "queue": queue_by_sid.get(sid, 0),
        })

    pair_refs: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def _add_pair(kind: str, a: str, b: str, ref: dict[str, Any]) -> None:
        pair_refs.setdefault((kind, a, b), []).append(ref)

    for m in members:
        sid = m["session_id"] or ""
        if not sid or sid not in sessions:
            continue
        _add_pair("delegate", "__orch", sid,
                  {"objective": (m["objective"] or "")[:80], "status": m["status"]})

    # inbox 依据聚类（basis_stale/finding_update）：直接会话两两成边，closed 不参与
    placeholders = ",".join("?" * len(_INBOX_KINDS))
    inbox_rows = bb.conn.execute(
        f"SELECT to_session, kind, ref_id, payload FROM session_inbox"
        f" WHERE project_id=? AND kind IN ({placeholders})",
        (project_id, *_INBOX_KINDS)).fetchall()
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
                (project_id, *ref_ids))}

    for (kind, ref_id), info in clusters.items():
        sids = [s for s in info["sessions"]
                if sessions.get(s, {}).get("status") != "closed"]
        if len(sids) < 2:
            continue
        ref = {"kind": kind, "ref_id": ref_id[:18],
               "title": finding_titles.get(ref_id) or payload_titles.get(ref_id, "")}
        for a, b in combinations(sorted(sids), 2):
            _add_pair("inbox", a, b, ref)

    # agent_message 私信：有向 dm 边（payload.from → to_session）
    dm_rows = bb.conn.execute(
        "SELECT to_session, payload FROM session_inbox"
        " WHERE project_id=? AND kind='agent_message'",
        (project_id,)).fetchall()
    seen_dm: set[tuple[str, str, str]] = set()
    for r in dm_rows:
        p = _loads(r["payload"], {})
        frm = p.get("from")
        if not frm or frm not in sessions or r["to_session"] not in sessions:
            continue
        dedup = (frm, r["to_session"], str(p.get("text", "")[:60]))
        if dedup in seen_dm:
            continue
        seen_dm.add(dedup)
        _add_pair("dm", frm, r["to_session"],
                  {"subkind": p.get("subkind", ""),
                   "text": str(p.get("text", ""))[:120]})

    edges = [{"id": f"{kind}:{a}:{b}", "source": a, "target": b,
              "kind": kind, "refs": refs}
             for (kind, a, b), refs in sorted(pair_refs.items())]
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# 黑板链路图（2026-09-20，DESIGN.md §12「黑板链路图」定稿块）
#
# 四类对象（asset/func_kb/finding/artifact）× 类型分层 DAG 的只读组装。
# 任务机制退役（2026-10-06）后 task 节点与 basis/task_parent 边一并移除。
# 边口径（全部来自既有字段，无新表）：
# - asset_parent   asset→asset      assets.parent_id 外键（父不在节点集跳过）
# - func_of        func→asset       func_kb.binary_sha256 == assets(type=binary).value
#                                   （sha 匹配非外键，内存建 map）
# - targets        finding→asset    findings.target_asset_id 外键
# - relates_to     finding→finding  evidence.relates_to（E0 强校验；note 作边 label）
# - poc            finding→artifact findings.poc_artifact_id 外键
# - artifact_task  artifact→task    artifacts.meta.task_id（历史产物仍挂旧任务 id；
#                                   任务节点已删故该边悬挂即丢）
# - chain          链内相邻         chains JOIN chain_links 按 seq 相邻成对；两端
#                                   实体已删（不在节点集）的段跳过
# 查询条数固定 ≤5 条（assets/funcs/findings/artifacts/chains+links 各一），
# 与数据量无关，无 N+1（tests 用 set_trace_callback 守上限）。
# ---------------------------------------------------------------------------

def board_graph(bb: Any, project_id: str) -> dict[str, Any]:
    """返回 {nodes, edges}。节点 id 用原始 id 不前缀化（与 chain_links.node_id 同口径）；
    label/sub 由服务端拼好，前端零二次映射。"""
    assets = bb.list_assets(project_id)          # 1：meta 已解析
    funcs = bb.list_funcs(project_id)            # 2：全量（不按 sha 过滤）
    findings = bb.list_findings(project_id)      # 3：evidence 已解析
    artifacts = bb.list_artifacts(project_id)    # 4：meta 仍是 JSON 文本，自行 _loads
    chain_rows = bb.conn.execute(                # 5：链+节点一条 JOIN，按链内 seq 序
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
