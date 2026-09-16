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

    sessions = {
        r["id"]: {"id": r["id"], "name": r["name"], "status": r["status"]}
        for r in bb.conn.execute(
            "SELECT id, name, status FROM sessions WHERE project_id=?", (project_id,))
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
            "plan": t.get("plan", []),
            "updated_at": t.get("updated_at"),
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
