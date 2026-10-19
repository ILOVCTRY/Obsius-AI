"""ARTEX 任务级运行时状态与确定性目标收敛。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from core.blackboard.artex_exploration import list_nodes
from core.blackboard.artex_intents import list_intents
from core.blackboard.store import _row_to_dict, now

log = logging.getLogger(__name__)

STATUSES = frozenset({"exploring", "draining", "goal_reached", "needs_review", "cancelled"})
# 运行时 metadata 中跨轮保留的键：evaluate_task 重写 metadata 时必须原样带过去。
_STICKY_META_KEYS = ("goal_met", "goal_met_reason", "planner_todos")

# 规划待办（artex-planner-todos）：Planner 跨唤醒共享的「整条链」清单。上游
# planner.go:43 用 `todos map[int64]*TodoStore`（按 exploration id 每任务一份）
# 承载——每次 Plan() 是新会话，但共享 store 让串行利用链「记录一次、按产出逐步
# 派」。这里复用 runtime metadata（键 planner_todos）实现同一语义：不新表，
# 随任务运行时一起落库，并由 _STICKY_META_KEYS 保证跨轮不被 evaluate_task 冲掉。
PLANNER_TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})
_MAX_PLANNER_TODOS = 20
_TODO_MARKS = {"pending": "☐", "in_progress": "▶", "completed": "✔"}


def planner_todos(bb, project_id: str, task_id: str) -> list[dict]:
    """读任务级规划待办（已归一化：id / text / status）。"""
    runtime = get_runtime(bb, project_id, task_id) or {}
    return _normalize_todos((runtime.get("metadata") or {}).get("planner_todos"))


def _normalize_todos(raw) -> list[dict]:
    out: list[dict] = []
    for item in list(raw or [])[:_MAX_PLANNER_TODOS]:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status not in PLANNER_TODO_STATUSES:
            status = "pending"
        out.append({"id": f"pt-{len(out) + 1}", "text": text[:300], "status": status})
    return out


def set_planner_todos(bb, project_id: str, task_id: str, todos) -> list[dict]:
    """整体替换任务级规划待办（TodoWrite 语义）；空列表 = 清空。

    只动 metadata 这一个键，其余键（goal_met 等）原样保留。写入后**不**重推
    状态——待办是规划备忘，不是任务终态依据（终态只由目标态派生）。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    normalized = _normalize_todos(todos)
    previous = get_runtime(bb, project_id, task_id)
    metadata = dict(((previous or {}).get("metadata")) or {})
    if normalized:
        metadata["planner_todos"] = normalized
    else:
        metadata.pop("planner_todos", None)
    ts = now()
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO artex_task_runtime(project_id,task_id,status,goal,metadata,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,task_id) DO UPDATE SET "
            "metadata=excluded.metadata, updated_at=excluded.updated_at",
            (project_id, task_id, str((previous or {}).get("status") or "exploring"),
             str((previous or {}).get("goal") or ""),
             json.dumps(metadata, ensure_ascii=False),
             (previous or {}).get("created_at") or ts, ts),
        )
    bb.append_event(project_id, "artex.task.plan_todos",
                    {"task_id": task_id, "count": len(normalized),
                     "completed": sum(1 for t in normalized if t["status"] == "completed")})
    return normalized


def reconcile_planner_todos(bb, project_id: str, task_id: str) -> list[dict]:
    """按既有 Intent 的实际状态推进待办状态（确定性，不采信模型自述）。

    上游 ``renderPlannerTodos`` 要求 Planner「把已被 fact 满足的步骤标 completed」，
    但那依赖模型每轮记得改。这里改为**由系统核对**：待办文本与某条 Intent 的
    objective 指纹一致时——该 Intent completed → 该步 completed；pending/claimed/
    running → in_progress；failed/cancelled → 保持 pending（前置崩了，该步仍需人处置）。
    仅推进不倒退：已是 completed 的待办不会被改回。
    """
    todos = planner_todos(bb, project_id, task_id)
    if not todos:
        return []
    from core.blackboard.artex_planner import _fingerprint
    by_fp: dict[str, str] = {}
    for row in list_intents(bb, project_id, limit=2000):
        if str(row.get("task_id") or "") != str(task_id):
            continue
        fp = _fingerprint(str(row.get("objective") or ""))
        status = str(row.get("status") or "")
        if not fp:
            continue
        if status == "completed":
            by_fp[fp] = "completed"
        elif status in ("pending", "claimed", "running", "paused") and by_fp.get(fp) != "completed":
            by_fp[fp] = "in_progress"
    changed = False
    for todo in todos:
        if todo["status"] == "completed":
            continue
        want = by_fp.get(_fingerprint(todo["text"]))
        if want and want != todo["status"]:
            todo["status"] = want
            changed = True
    if changed:
        set_planner_todos(bb, project_id, task_id, todos)
    return todos


def render_planner_todos(todos) -> str:
    """把待办渲染成 Planner 提示词段（无待办时返回空串，首轮不注入）。"""
    items = _normalize_todos(todos)
    if not items:
        return ""
    lines = ["【你的规划待办（跨唤醒保留，上一轮你写的）】："]
    for item in items:
        lines.append(f"  {_TODO_MARKS.get(item['status'], '☐')} {item['text']}")
    lines.append("据此推进：只对【前置步骤已完成 / 其依赖的 fact 已存在】的下一步派意图；"
                 "用 todos 字段更新清单（把已被事实满足的步骤标 completed）。"
                 "不要重复派已在清单里 pending/in_progress 的步骤。")
    return "\n".join(lines)



def _artex_cfg(bb, project_id: str) -> dict:
    """读项目 config.artex（JSON 文本或 dict 均可）；失败按空配置处理。"""
    try:
        row = bb.get_project(project_id) or {}
        raw = row.get("config")
        cfg = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:  # noqa: BLE001
        return {}
    return (cfg or {}).get("artex") or {}


def task_limits(bb, project_id: str) -> tuple[float, int]:
    """返回 (deadline_s, max_intents)；0 表示不限。"""
    cfg = _artex_cfg(bb, project_id)
    try:
        deadline = max(0.0, float(cfg.get("task_deadline_s", 0) or 0))
    except (TypeError, ValueError):
        deadline = 0.0
    try:
        max_intents = max(0, int(cfg.get("max_intents", 50) or 0))
    except (TypeError, ValueError):
        max_intents = 50
    return deadline, max_intents


def limit_reason(intents: list[dict], *, deadline_s: float, max_intents: int) -> str:
    """任务级预算是否已超限；返回空串表示未超限。"""
    if max_intents > 0 and len(intents) >= max_intents:
        return f"任务 Intent 数达到上限 {max_intents}"
    if deadline_s <= 0:
        return ""
    root = next((item for item in intents if not item.get("parent_id")), None)
    started = str((root or {}).get("created_at") or "")
    if not started:
        return ""
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return ""
    if age > deadline_s:
        return f"任务运行超过 {deadline_s:g}s"
    return ""


def get_runtime(bb, project_id: str, task_id: str) -> dict | None:
    row = bb.conn.execute(
        "SELECT * FROM artex_task_runtime WHERE project_id=? AND task_id=?",
        (project_id, task_id),
    ).fetchone()
    if not row:
        return None
    result = _row_to_dict(row) or {}
    try:
        result["metadata"] = json.loads(result.get("metadata") or "{}")
    except (TypeError, ValueError):
        result["metadata"] = {}
    return result


def _derive_status(intents: list[dict], nodes: list[dict], current: str = "",
                   limit: str = "", goals: dict | None = None,
                   goal_declared: bool = False) -> str:
    """由 Intent 态 × 目标态共同推导任务终态。

    ``goals`` 是 ``artex_goals.goals_stats`` 的结果。有注册目标时，终局一律
    以目标为准（全部 met → ``goal_reached``；仍有 open → ``needs_review``，
    即「探索停摆但目标未达成」，需要主控补方向或人工介入）；无注册目标时
    （历史任务 / API 手工建树）保留「有 fact 即达成」的兜底代理。
    """
    goals = goals or {"total": 0, "met": 0, "open": 0}
    statuses = {str(item.get("status") or "") for item in intents}
    if not intents:
        return current or "exploring"
    if statuses <= {"cancelled"}:
        return "cancelled"
    # 任务级预算/时限超限：即使还有 pending 也转人工复核（Engine 会把它们暂停）。
    if limit and statuses & {"pending", "claimed", "running"}:
        return "needs_review"
    # artex-exhausted：exhausted = 撞运行预算被动收尾（试过但没做完）。它既不能
    # 当成 completed（否则该方向被永久跳过、任务假性收敛），也不能当成还在跑。
    # 无活跃 Worker 时一律转人工/规划复核——由 Planner 看 trace 决定续跑还是换向。
    if statuses & {"failed", "blocked", "exhausted"} and not statuses & {
            "pending", "claimed", "running", "paused"}:
        return "needs_review"
    if statuses & {"pending", "claimed", "running", "paused"}:
        return "exploring"
    # 以下都是「全部 Intent 已终止（completed/cancelled）」的收敛判定。
    if statuses <= {"completed", "cancelled"}:
        # 全局收官宣告（declare_goal_met）优先：等在手 Worker 收干后生效。
        if goal_declared:
            return "goal_reached"
        if goals["total"] > 0:
            return "goal_reached" if goals["open"] == 0 else "needs_review"
        has_fact = any(node.get("kind") == "fact" for node in nodes)
        return "goal_reached" if has_fact else "draining"
    return current or "exploring"


def _ctf_task_state(bb, project_id: str, task_id: str,
                    intents: list[dict]) -> dict | None:
    """CTF 轨的终态判据（**按题目状态派生**，不是「有 fact 即达成」）。

    返回 ``None`` 表示「不是 CTF 任务」（无题目）→ 调用方走原有资产/目标口径。

    CTF 轨的正确终态判据是 **flag 台账**（challenge-flag-ledger v47）：

    - 任务范围内**全部题目 solved** → ``goal_reached``；
    - 有题目在跑 → ``exploring``；
    - 都不在跑且未全解 → ``needs_review``（停摆了但没解完，需要人/主控补方向）。

    为什么不能沿用 goals/fact 口径：题目不是待测资产、flag 也不是 fact，
    资产覆盖度与 fact 计数在 CTF 轨都是无意义数字；更危险的是「有 finding 即
    达成」会让「随便报个发现」直接假收敛。
    """
    try:
        assets = bb.list_assets(project_id)
    except Exception:  # noqa: BLE001
        return None
    cid_set = {a["id"] for a in assets
               if a.get("type") == "challenge" and not a.get("parent_id")}
    if not cid_set:
        return None
    # 任务范围题目：Intent 锚定的题目；一个都没锚定则视为「本任务打全部题目」。
    scoped: list[str] = []
    for intent in intents:
        for aid in (intent.get("asset_ids") or []):
            sid = str(aid or "")
            if sid in cid_set and sid not in scoped:
                scoped.append(sid)
    if not scoped:
        scoped = sorted(cid_set)
    running: dict[str, int] = {}
    for intent in intents:
        if str(intent.get("status") or "") not in ("claimed", "running"):
            continue
        for aid in (intent.get("asset_ids") or []):
            sid = str(aid or "")
            if sid in cid_set:
                running[sid] = running.get(sid, 0) + 1
                break
    try:
        from core.blackboard.challenge_flags import challenge_flag_summary
        summary = challenge_flag_summary(bb, project_id, scoped, running)
    except Exception:  # noqa: BLE001 —— flag 表缺失时不接管（退回原口径）
        log.exception("CTF 任务状态派生失败 task=%s", task_id)
        return None
    total = len(scoped)
    solved = sum(1 for cid in scoped
                 if (summary.get(cid) or {}).get("solved"))
    abandoned = sum(1 for cid in scoped
                    if (summary.get(cid) or {}).get("abandoned")
                    and not (summary.get(cid) or {}).get("solved"))
    active = sum(1 for cid in scoped
                 if (summary.get(cid) or {}).get("state") == "running")
    if total and solved >= total:
        state = "goal_reached"
    elif active > 0:
        state = "exploring"
    else:
        state = "needs_review"
    return {"state": state, "total": total, "solved": solved,
            "abandoned": abandoned, "active": active,
            "partial": sum(1 for cid in scoped
                           if (summary.get(cid) or {}).get("state") == "partial"),
            "untouched": sum(1 for cid in scoped
                             if (summary.get(cid) or {}).get("state") == "untouched")}


def evaluate_task(bb, project_id: str, task_id: str, *, reason: str = "state_change") -> dict:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    intents = list_intents(bb, project_id, limit=2000)
    intents = [item for item in intents if str(item.get("task_id") or "") == task_id]
    nodes = list_nodes(bb, project_id, task_id=task_id)
    previous = get_runtime(bb, project_id, task_id)
    prev_meta = (previous or {}).get("metadata") or {}
    deadline_s, max_intents = task_limits(bb, project_id)
    exceeded = limit_reason(intents, deadline_s=deadline_s,
                            max_intents=max_intents)
    # 目标态驱动终态：导入失败（老库无目标表等）退回无目标代理，不阻断状态推导。
    try:
        from core.blackboard.artex_goals import goals_stats
        goal_stats = goals_stats(bb, project_id, task_id)
    except Exception:  # noqa: BLE001
        goal_stats = {"total": 0, "met": 0, "open": 0}
    goal_declared = bool(prev_meta.get("goal_met"))
    # CTF 轨优先：终态判据是 flag 台账（题目状态派生），不是 goals/fact。
    ctf = _ctf_task_state(bb, project_id, task_id, intents)
    if ctf is not None:
        # 预算超限与「还有 pending」的既有语义仍然生效（暂停 + 转人工复核）。
        if exceeded and any(item.get("status") in ("pending", "claimed", "running")
                            for item in intents):
            status = "needs_review"
        else:
            status = str(ctf["state"])
    else:
        status = _derive_status(intents, nodes, (previous or {}).get("status", ""),
                                limit=exceeded, goals=goal_stats,
                                goal_declared=goal_declared)
    counts: dict[str, int] = {}
    for item in intents:
        key = str(item.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    ts = now()
    metadata = {"reason": reason, "counts": counts, "goal_stats": goal_stats,
                "fact_count": sum(1 for n in nodes if n.get("kind") == "fact")}
    if ctf is not None:
        # 题目口径：前端「题目 tab」与主控 artex_status 都读这几个键。
        metadata["challenge_stats"] = {k: v for k, v in ctf.items() if k != "state"}
        metadata["track"] = "ctf"
    # 跨轮保留任务级宣告：declare_goal_met 落的 metadata 不能被本轮覆盖冲掉。
    for key in _STICKY_META_KEYS:
        if key in prev_meta:
            metadata[key] = prev_meta[key]
    if exceeded:
        metadata["limit_reason"] = exceeded
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO artex_task_runtime(project_id,task_id,status,goal,metadata,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,task_id) DO UPDATE SET "
            "status=excluded.status, metadata=excluded.metadata, updated_at=excluded.updated_at",
            (project_id, task_id, status,
             str((previous or {}).get("goal") or (next((i.get("objective") for i in intents if not i.get("parent_id")), ""))),
             json.dumps(metadata, ensure_ascii=False),
             (previous or {}).get("created_at") or ts, ts),
        )
    result = get_runtime(bb, project_id, task_id) or {}
    if not previous or previous.get("status") != status:
        bb.append_event(project_id, "artex.task.status", {
            "task_id": task_id, "status": status, "previous_status": (previous or {}).get("status", ""),
            "reason": reason, "counts": counts,
        })
    return result


def declare_task_goal_met(bb, project_id: str, task_id: str, *, reason: str,
                          author: str = "") -> dict:
    """goal_met 的任务级宣告：写 runtime metadata 并立刻重推状态。

    与 ``artex_goals.declare_goal_met`` 分工：后者负责把已注册目标标 met，
    本函数负责把「未经逐条证明的全局收官」落到 runtime 上，供 ``_derive_status``
    在有在手 Worker 时先压住判定（等收干后自然转 goal_reached）。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id 必填")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("goal_met 需要 reason")
    previous = get_runtime(bb, project_id, task_id)
    metadata = dict(((previous or {}).get("metadata")) or {})
    metadata["goal_met"] = {"reason": reason[:500],
                            "author": str(author or "")[:80], "at": now()}
    ts = now()
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO artex_task_runtime(project_id,task_id,status,goal,metadata,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,task_id) DO UPDATE SET "
            "metadata=excluded.metadata, updated_at=excluded.updated_at",
            (project_id, task_id, str((previous or {}).get("status") or "exploring"),
             str((previous or {}).get("goal") or ""),
             json.dumps(metadata, ensure_ascii=False),
             (previous or {}).get("created_at") or ts, ts),
        )
    bb.append_event(project_id, "artex.task.goal_met", {
        "task_id": task_id, "reason": reason[:500], "author": str(author or "")[:80]})
    return evaluate_task(bb, project_id, task_id, reason="goal_met")
