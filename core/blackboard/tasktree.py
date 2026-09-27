"""任务尝试树 v2——意图驱动（docs/plans/归档-已完成/task-attempt-tree.md，2026-09-27）。

v1（计划步主干+动作叶）当日被用户反馈推翻：命令/工具动作叶是噪声，用户要看的是
**目标 → 意图 → 检验结果** 的单向树——意图可以是「对 xxx 进行 sql 注入尝试」这类
一句假设；结果=发现/死路；新发现下再长出新意图（树自然分叉）。

单向树**被动派生**（零打扰），全部读时现算零写入零事件，API
`GET /api/projects/{pid}/tree/{task_id}` 直出。树模型：

- 根 = 任务 objective；
- 意图节点 = intents 表行（task 窗内声明的），open=进行中（当前节点）、
  closed=dead_end（死路+死因）/vuln/finding（收尾产出）；
- 发现节点挂意图下，归属优先级：① 意图 outcome_refs 显式引用 → ② 存活窗
  归属（finding.new 事件时刻 ∈ 意图 [created_at, closed_at] 且作者会话一致，
  取最新声明）→ ③ 兜底「未挂意图发现」桶（**仅历史数据**——2026-09-27 起工具层
  门禁强制 Agent 登记发现前必须有 open 意图，新数据不允许游离）；
- 新意图嵌套：intent.basis_refs 引用 `finding:<fid>` 且 fid 已挂在某意图下
  → 该意图成为那条发现的孩子（递归成树；发现同挂多意图=分叉）。

任务归属复用 traces 的 R1 会话任务区间切分（claimed→done/failed 事件窗）；
多会话接力（reopen/换人）按事件 id 全序合并。
"""

import json
from typing import Any

from core.blackboard import traces
from core.blackboard.store import _loads

#: 单会话事件回放上限（防超长会话全表扫；同 traces.TRACE_MAX_EVENTS 量级）
TREE_MAX_EVENTS = 5000
#: 意图节点上限
TREE_MAX_INTENTS = 60
#: 发现节点上限（超出丢弃并置 truncated 标记）
TREE_MAX_FINDINGS = 120

_TASK_KINDS = ("intent.declared", "finding.new")


def _short(text: object, cap: int) -> str:
    s = str(text or "")
    return s if len(s) <= cap else s[: cap - 1] + "…"


def build_task_tree(bb: Any, pid: str, task_id: str) -> dict | None:
    """现算任务尝试树（零写入）。任务不存在返 None；从未被认领返空树（只有根）。

    返回 {task, nodes, current, truncated_findings}——nodes 平铺带 parent
    （""=挂根），前端按 parent 组树分列布局；current.intent_id 取最新声明的
    open 意图（仅任务 claimed 时——收尾后 current 归位）。
    """
    conn = bb.conn
    task = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND project_id=?", (task_id, pid)).fetchone()
    if task is None:
        return None

    intent_ids: list[str] = []
    # finding.new 事件：fid -> (事件时刻, 会话)（取首见；同 fid 重报不换归属锚点）
    finding_seen: dict[str, tuple[str, str]] = {}
    last_activity_ts: str | None = None

    for sid in traces._claimed_sessions(conn, pid, task_id):
        wins = [w for w in traces._session_task_windows(conn, pid, sid)
                if w["task_id"] == task_id]
        if not wins:
            continue
        marks = ",".join("?" * len(_TASK_KINDS))
        rows = conn.execute(
            "SELECT id, kind, payload, created_at FROM events"
            " WHERE project_id=? AND session_id=? AND kind IN"
            f" ({marks}) ORDER BY id DESC LIMIT {TREE_MAX_EVENTS}",
            (pid, sid, *_TASK_KINDS)).fetchall()
        for r in rows:  # 窗滤：(lo, hi] 口径同 traces.label_of
            if not any(r["id"] > w["lo"] and (w["hi"] is None or r["id"] <= w["hi"])
                       for w in wins):
                continue
            try:
                payload = json.loads(r["payload"] or "{}")
            except ValueError:
                payload = {}
            if r["created_at"] and (last_activity_ts is None
                                    or r["created_at"] > last_activity_ts):
                last_activity_ts = r["created_at"]
            if r["kind"] == "intent.declared":
                iid = payload.get("intent_id")
                if iid and str(iid) not in intent_ids:
                    intent_ids.append(str(iid))
            elif r["kind"] == "finding.new":
                fid = payload.get("finding_id")
                if fid and str(fid) not in finding_seen:
                    finding_seen[str(fid)] = (r["created_at"], sid)

    # 意图行（树域）
    intents: list[dict] = []
    if intent_ids:
        ph = ",".join("?" * len(intent_ids))
        for r in conn.execute(
                f"SELECT id, statement, status, outcome_type, outcome_refs,"
                f" basis_refs, dead_reason, author, created_at, closed_at"
                f" FROM intents WHERE project_id=? AND id IN ({ph})"
                f" ORDER BY created_at, rowid", (pid, *intent_ids)).fetchall():
            intents.append({
                "id": r["id"], "statement": _short(r["statement"], 160),
                "status": r["status"], "outcome_type": r["outcome_type"] or "",
                "outcome_refs": _loads(r["outcome_refs"], []),
                "basis_refs": _loads(r["basis_refs"], []),
                "dead_reason": _short(r["dead_reason"], 160),
                "author": r["author"] or "",
                "created_at": r["created_at"], "closed_at": r["closed_at"]})
    intents = intents[:TREE_MAX_INTENTS]
    by_id = {it["id"]: it for it in intents}

    # 发现域 = 窗内 finding.new ∪ 树域意图 outcome_refs 显式引用（并集，去重）
    fids = list(finding_seen.keys())
    for it in intents:
        for fid in it["outcome_refs"]:
            if fid and fid not in finding_seen and fid not in fids:
                fids.append(fid)
    truncated_findings = len(fids) > TREE_MAX_FINDINGS
    fids = fids[:TREE_MAX_FINDINGS]

    findings: dict[str, dict] = {}
    if fids:
        ph = ",".join("?" * len(fids))
        for r in conn.execute(
                f"SELECT id, title, severity, status, vuln_class, created_at"
                f" FROM findings WHERE project_id=? AND id IN ({ph})",
                (pid, *fids)).fetchall():
            findings[r["id"]] = {
                "id": r["id"], "kind": "finding", "parent": "",
                "title": _short(r["title"], 80), "severity": r["severity"],
                "status": r["status"], "vuln_class": r["vuln_class"] or "",
                "created_at": r["created_at"]}

    # 发现 → 意图归属：① outcome_refs 显式引用优先（一发现只挂一意图）
    for it in intents:
        for fid in it["outcome_refs"]:
            f = findings.get(fid)
            if f is not None and not f["parent"]:
                f["parent"] = it["id"]
    # ② 存活窗归属：finding.new 时刻 ∈ 意图 [created_at, closed_at]，
    #    作者会话一致优先，同窗多意图取最新声明
    for fid, (ts, sid) in finding_seen.items():
        f = findings.get(fid)
        if f is None or f["parent"]:
            continue
        cands = [it for it in intents
                 if it["created_at"] <= ts
                 and (it["closed_at"] is None or it["closed_at"] >= ts)]
        if not cands:
            continue
        cands.sort(key=lambda it: (it["author"] == sid, it["created_at"]))
        f["parent"] = cands[-1]["id"]
    # ③ 游离发现 → 兜底桶（仅历史数据；新数据被工具层门禁拦死）

    # 意图嵌套：basis_refs 引用 finding:<fid> 且该发现已在树上 → 子意图挂**发现
    # 节点**下（用户口径：新发现长出新意图）；发现恰好归属本意图时跳过（防环）
    for it in intents:
        it["parent"] = ""
        for ref in it["basis_refs"]:
            if isinstance(ref, str) and ref.startswith("finding:"):
                f = findings.get(ref[len("finding:"):])
                if f is not None and f["parent"] != it["id"]:
                    it["parent"] = f["id"]
                    break

    # 组装 nodes（根层=created_at 序的顶层意图，兜底桶殿后）
    nodes: list[dict] = []
    for it in intents:
        nodes.append({
            "id": it["id"], "kind": "intent", "parent": it["parent"],
            "statement": it["statement"], "status": it["status"],
            "outcome_type": it["outcome_type"], "dead_reason": it["dead_reason"],
            "created_at": it["created_at"], "closed_at": it["closed_at"]})
    for f in findings.values():
        nodes.append(f)
    orphans = [f for f in findings.values() if not f["parent"]]
    if orphans:
        nodes.append({"id": "_orphan", "kind": "bucket", "parent": "",
                      "title": f"未挂意图的发现（历史）×{len(orphans)}"})

    doing = (next((it for it in reversed(intents) if it["status"] == "open"), None)
             if task["status"] == "claimed" else None)  # 收尾后 current 归位
    return {
        "task": {"id": task["id"], "objective": task["objective"],
                 "task_type": task["task_type"], "status": task["status"],
                 "priority": task["priority"], "claimed_by": task["claimed_by"],
                 "result_note": _short(task["result_note"], 300)},
        "nodes": nodes,
        "current": {"intent_id": doing["id"] if doing else None,
                    "last_activity_ts": last_activity_ts},
        "truncated_findings": truncated_findings,
    }
