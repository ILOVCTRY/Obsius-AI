"""ARTEX 调度 Intent 持久化层。

与 ``intents.py`` 的证据链意图分离：这里承载 frontier 和 worker 生命周期；
source_intent_id 可选地关联一条可证伪假设。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.blackboard.store import _row_to_dict, new_id, now

log = logging.getLogger(__name__)

STATUSES = ("pending", "claimed", "running", "paused", "blocked", "exhausted",
            "completed", "failed", "cancelled")

# 终态集合（exhausted 也是终态：本次运行已结束，只是没做完——可 retry 重开）。
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "exhausted"})
# 「中途没善终、信息不全」的状态（artex-exhausted）：Planner 处置时不能当成
# 已覆盖永久跳过，必须先看 trace 再决定「接着跑」还是「换打法」。
UNFINISHED_STATUSES = frozenset({"blocked", "exhausted", "failed"})



def _hydrate(row: dict) -> dict:
    for key, default in (("constraints", {}), ("asset_ids", []), ("depends_on", [])):
        raw = row.get(key)
        try:
            row[key] = json.loads(raw) if isinstance(raw, str) else (raw or default)
        except (TypeError, ValueError):
            row[key] = default
    return row


def _sync_explore(bb, project_id: str, intent: dict | None) -> None:
    """把 Intent 状态回写到探索图节点（失败不阻断调度）。

    单独包一层是为了**延迟导入**：`artex_exploration` 顶层 import 了
    `core.blackboard.store`，与本模块无循环依赖，但探索图属于「只读视图」，
    调度层不该在导入期就依赖它；且老库里可能没有探索图表，同步失败必须静默
    降级（图是展示层，不能因为画不出来就不干活）。
    """
    if not intent:
        return
    try:
        from core.blackboard.artex_exploration import sync_intent_status
        sync_intent_status(bb, project_id, intent)
    except Exception:  # noqa: BLE001 —— 图同步失败绝不影响调度
        log.exception("探索图状态同步异常 intent=%s", (intent or {}).get("id"))


def create_intent(bb, project_id: str, objective: str, *, task_id: str = "",
                   expert: str = "", asset_ids=None, constraints=None,
                   depends_on=None, priority: int = 0, parent_id: str | None = None,
                   source_intent_id: str | None = None) -> dict:
    objective = str(objective or "").strip()
    if not objective:
        raise ValueError("ARTEX Intent objective 必填")
    ids = [str(x).strip() for x in (asset_ids or []) if str(x).strip()]
    # 去 None（str(None) == "None" 会写出指向不存在 Intent 的假依赖）；编号/字符串保留。
    deps = [str(x).strip() for x in (depends_on or []) if x is not None and str(x).strip()]
    deps = list(dict.fromkeys(deps))
    ts = now()
    iid = new_id("artex")
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO artex_intents(id,project_id,parent_id,source_intent_id,task_id,"
            "objective,constraints,asset_ids,depends_on,expert,priority,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?)",
            (iid, project_id, parent_id, source_intent_id, str(task_id or ""), objective,
             json.dumps(constraints or {}, ensure_ascii=False),
             json.dumps(ids, ensure_ascii=False),
             json.dumps(deps, ensure_ascii=False),
             str(expert or ""), int(priority), ts, ts),
        )
    bb.append_event(project_id, "artex.intent.created",
                    {"intent_id": iid, "task_id": task_id, "objective": objective,
                     "expert": expert, "asset_ids": ids, "priority": priority,
                     "depends_on": deps})
    return get_intent(bb, project_id, iid) or {}


def get_intent(bb, project_id: str, intent_id: str) -> dict | None:
    row = bb.conn.execute("SELECT * FROM artex_intents WHERE id=? AND project_id=?",
                          (intent_id, project_id)).fetchone()
    return _hydrate(_row_to_dict(row) or {}) if row else None


def claim_intent(bb, project_id: str, intent_id: str, worker: str) -> dict:
    worker = str(worker or "").strip()
    if not worker:
        raise ValueError("worker 必填")
    ts = now()
    with bb._tx():
        cur = bb.conn.execute(
            "UPDATE artex_intents SET status='claimed', claimed_by=?, claimed_at=?, "
            "attempt=attempt+1, updated_at=? WHERE id=? AND project_id=? AND status='pending'",
            (worker, ts, ts, intent_id, project_id))
        if cur.rowcount == 0:
            raise ValueError("Intent 不存在或已被领取")
    bb.append_event(project_id, "artex.intent.claimed",
                    {"intent_id": intent_id, "worker": worker})
    row = get_intent(bb, project_id, intent_id) or {}
    # 探索图节点状态同步（artex-explore-status-sync，2026-10-21）：节点只在创建
    # 那一刻取过 status（恒 pending），此后没人回写 → 图上「意图永远待调度」。
    _sync_explore(bb, project_id, row)
    return row


def _dep_state(bb, project_id: str, intent: dict) -> tuple[bool, list[str], list[str]]:
    """依赖满足度：``(ready, unsatisfied, broken)``。

    - ``satisfied``：依赖 Intent 已完成，或依赖 id **已不存在**（被删）——悬空
      依赖视为已满足，避免删除前置后子 Intent 永久卡死。
    - ``broken``：依赖以 failed/cancelled/**exhausted** 收尾 —— 前置没成功（崩了，
      或探到一半被预算掐断），后置既拿不到可靠产出也不该硬跑，本 Intent 应置
      blocked 交人处理（可重试/续跑前置后恢复）。
    - ``unsatisfied``：依赖仍在 pending/claimed/running/paused/blocked —— 等。
    """
    deps = [str(x).strip() for x in (intent.get("depends_on") or []) if str(x).strip()]
    unsatisfied: list[str] = []
    broken: list[str] = []
    for dep_id in deps:
        row = get_intent(bb, project_id, dep_id)
        if row is None:
            continue  # 前置已删除：视为满足
        status = str(row.get("status") or "pending")
        if status == "completed":
            continue
        if status in ("failed", "cancelled", "exhausted"):
            broken.append(dep_id)
        else:
            unsatisfied.append(dep_id)
    return (not unsatisfied and not broken), unsatisfied, broken


def deps_ready(bb, project_id: str, intent: dict) -> bool:
    """Frontier 依赖闸：依赖全 completed（或已删）才可领取。"""
    ready, _, _ = _dep_state(bb, project_id, intent)
    return ready


def claim_next_intent(bb, project_id: str, worker: str, *,
                      per_challenge_limit: int = 0,
                      blocked_challenges: Any = None,
                      running_by_challenge: dict | None = None,
                      challenge_ids: Any = None) -> dict | None:
    """按优先级领取一个 pending Intent，供 Frontier/Engine 原子消费。

    依赖闸（artex-deps）：只领取**依赖已满足**的 Intent（前置全 completed 或
    已删）。依赖 broken（前置 failed/cancelled）的 pending Intent 在此顺带置
    blocked——否则它会一直占着队首，把后面独立方向一起堵死。

    **题目级限流与已解闸**（challenge-flag-ledger v47，CTF 轨；四个参数都由
    Engine 一次性算好传进来，本函数**不查** assets/challenge_flags，保持热路径
    零额外查询）：

    - ``challenge_ids``：本项目全部题目根资产 id 集合——用于把 Intent 的
      ``asset_ids`` 归属到题目（不在集合里的一律不算题目）；
    - ``blocked_challenges``：已解出/已放弃的题目，其 pending 一律置
      ``cancelled``（带 reason）而**不是**跳过——否则它们会永远堵在队首。
      这是「已获取 flag 就不再测试」的**调度层**落地；
    - ``running_by_challenge`` + ``per_challenge_limit``：同一道题在跑的 Worker
      达到上限即**跳过**该题 pending。这是「同一道题 work 子线程上限」的调度层
      落地——只靠提示词约束不住。

    四个参数全空时行为与引入本机制前完全一致（渗透轨无题目概念）。
    """
    worker = str(worker or "").strip()
    if not worker:
        raise ValueError("worker 必填")
    blocked = {str(c) for c in (blocked_challenges or []) if str(c)}
    known = {str(c) for c in (challenge_ids or []) if str(c)}
    running = {str(k): int(v or 0)
               for k, v in (running_by_challenge or {}).items()}
    limit = max(0, int(per_challenge_limit or 0))

    def _challenge_of(intent: dict) -> str:
        for aid in (intent.get("asset_ids") or []):
            sid = str(aid or "").strip()
            if sid and sid in known:
                return sid
        return ""

    ts = now()
    with bb._tx():
        candidates = bb.conn.execute(
            "SELECT id FROM artex_intents WHERE project_id=? AND status='pending' "
            "ORDER BY priority DESC, created_at ASC LIMIT 200", (project_id,)).fetchall()
        picked: str | None = None
        for cand in candidates:
            iid = cand["id"]
            intent = get_intent(bb, project_id, iid)
            if intent is None:
                continue
            cid = _challenge_of(intent)
            if cid and cid in blocked:
                # 已解出/已放弃：直接取消，别堵队首（幂等：只动 pending）
                bb.conn.execute(
                    "UPDATE artex_intents SET status='cancelled', error=?, updated_at=? "
                    "WHERE id=? AND project_id=? AND status='pending'",
                    ("题目已解出或已放弃，不再继续测试", ts, iid, project_id))
                continue
            if cid and limit > 0 and running.get(cid, 0) >= limit:
                # 该题并发已满：跳过（**不**置任何状态，等有槽位再来领）
                continue
            ready, _unsatisfied, broken = _dep_state(bb, project_id, intent)
            if broken:
                # 前置失败：置 blocked（不领取），交人处理。批量在同一事务内完成。
                bb.conn.execute(
                    "UPDATE artex_intents SET status='blocked', error=?, updated_at=? "
                    "WHERE id=? AND project_id=? AND status='pending'",
                    (("前置 Intent 未成功：" + "、".join(broken))[:4000], ts, iid, project_id))
                continue
            if ready:
                picked = iid
                break
        if picked is None:
            return None
        cur = bb.conn.execute(
            "UPDATE artex_intents SET status='claimed', claimed_by=?, claimed_at=?, "
            "attempt=attempt+1, updated_at=? WHERE id=? AND project_id=? AND status='pending'",
            (worker, ts, ts, picked, project_id))
        if cur.rowcount != 1:
            return None
    bb.append_event(project_id, "artex.intent.claimed",
                    {"intent_id": picked, "worker": worker, "frontier": True})
    return get_intent(bb, project_id, picked)


def challenge_gate(bb, project_id: str) -> dict:
    """CTF 轨题目闸所需的全部快照（Engine 每 tick 算一次，供 claim 用）。

    返回 ``{"challenge_ids": set, "blocked": set, "running": dict,
    "per_challenge_limit": int}``。非 CTF 轨（项目无 challenge 资产）时三个集合
    全空——claim 的题目逻辑自动失效，渗透轨行为不变。

    ``per_challenge_limit`` 取项目配置 ``artex.per_challenge_workers``（缺省 5，
    与用户约定一致）；``0`` 表示不限。
    """
    out = {"challenge_ids": set(), "blocked": set(), "running": {},
           "per_challenge_limit": 0}
    try:
        assets = bb.list_assets(project_id)
    except Exception:  # noqa: BLE001
        return out
    cids = {a["id"] for a in assets
            if a.get("type") == "challenge" and not a.get("parent_id")}
    if not cids:
        return out
    out["challenge_ids"] = cids
    try:
        from core.blackboard.challenge_flags import challenge_flag_summary
        running: dict[str, int] = {}
        for row in bb.conn.execute(
                "SELECT asset_ids FROM artex_intents WHERE project_id=? "
                "AND status IN ('claimed','running')", (project_id,)).fetchall():
            try:
                ids = json.loads(row["asset_ids"] or "[]")
            except (TypeError, ValueError):
                ids = []
            for aid in ids or []:
                sid = str(aid or "")
                if sid in cids:
                    running[sid] = running.get(sid, 0) + 1
                    break
        out["running"] = running
        summary = challenge_flag_summary(bb, project_id, list(cids), running)
        # `concluded`（主控主动收尾，2026-10-20）与 solved/abandoned 同效：
        # 都不再派工。区别只在状态语义——concluded 的题目仍是 partial/待复核，
        # 因为 pending 候选未经人工确认，不能伪装成 solved。
        out["blocked"] = {cid for cid, st in summary.items()
                          if st.get("solved") or st.get("abandoned")
                          or st.get("concluded")}
    except Exception:  # noqa: BLE001 —— 闸算失败退回「不限不拦」，不阻断调度
        log.exception("ARTEX 题目闸计算失败 project=%s", project_id)
    try:
        cfg = json.loads((bb.get_project(project_id) or {}).get("config") or "{}")
        artex = (cfg or {}).get("artex") or {}
        out["per_challenge_limit"] = max(
            0, int(artex.get("per_challenge_workers", 5) or 0))
    except Exception:  # noqa: BLE001
        out["per_challenge_limit"] = 5
    return out


def retry_intent(bb, project_id: str, intent_id: str) -> dict:
    """把没善终的 Intent 重新放回 Frontier；attempt 在下一次 claim 时递增。

    artex-exhausted：``failed``（彻底失败）、``exhausted``（撞步数/时间预算，
    本次没做完）与 ``blocked``（前置未成功，前置已修好时）都可重派。清
    ``finished_at`` 让下一轮收尾重新计时；依赖与 attempt 计数保留。
    """
    ts = now()
    with bb._tx():
        cur = bb.conn.execute(
            "UPDATE artex_intents SET status='pending', claimed_by='', claimed_at=NULL, "
            "started_at=NULL, finished_at=NULL, error='', updated_at=? "
            "WHERE id=? AND project_id=? AND status IN ('failed','exhausted','blocked')",
            (ts, intent_id, project_id))
        if cur.rowcount == 0:
            raise ValueError("Intent 不存在或当前状态不可重试")
    bb.append_event(project_id, "artex.intent.retried", {"intent_id": intent_id})
    aggregate_parent_status(bb, project_id, intent_id)
    return get_intent(bb, project_id, intent_id) or {}


def validate_dependencies(bb, project_id: str, task_id: str,
                          depends_on, *, exclude: str = "") -> list[str]:
    """校验依赖声明：返回**非法依赖 id**列表（空 = 全部合法）。

    非法 = ①不存在于本项目；②跨任务（依赖必须同一 task_id）；③构成依赖环
    （含自依赖）。落库前调用，避免把幻觉 id 或成环的链写进 Frontier——
    环会让整条链永远凑不齐「全 completed」而全体卡死。
    """
    deps = [str(x).strip() for x in (depends_on or [])
            if x is not None and str(x).strip()]
    if not deps:
        return []
    bad: list[str] = []
    self_id = str(exclude or "")
    for dep_id in dict.fromkeys(deps):
        if self_id and dep_id == self_id:
            bad.append(dep_id); continue
        row = get_intent(bb, project_id, dep_id)
        if row is None or str(row.get("task_id") or "") != str(task_id or ""):
            bad.append(dep_id); continue
    if bad:
        return bad
    # 环检测：从每个依赖出发沿 depends_on 上溯，若能回到 exclude（或任一依赖被
    # 重复访问到自身），即成环。exclude 为空（新建 Intent，尚无 id）时只查依赖
    # 子图内部是否已存在环。
    if self_id:
        for dep_id in deps:
            if _reaches(bb, project_id, dep_id, self_id):
                bad.append(dep_id)
    else:
        for dep_id in deps:
            if _reaches(bb, project_id, dep_id, dep_id):
                bad.append(dep_id)
    return list(dict.fromkeys(bad))


def _reaches(bb, project_id: str, start: str, target: str) -> bool:
    """沿 depends_on 从 start 上溯能否到达 target（含 start==target 的自环）。"""
    seen: set[str] = set()
    stack = [start]
    first = True
    while stack:
        cur = stack.pop()
        if cur == target and not first:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        first = False
        row = get_intent(bb, project_id, cur)
        if row is None:
            continue
        stack.extend(str(x) for x in (row.get("depends_on") or []) if str(x))
    return False


def dependents_of(bb, project_id: str, intent_id: str, *, limit: int = 500) -> list[dict]:
    """直接依赖 intent_id 的 Intent（下游），用于取消/失败影响面提示。"""
    out: list[dict] = []
    for row in list_intents(bb, project_id, limit=limit):
        deps = [str(x) for x in (row.get("depends_on") or []) if str(x)]
        if intent_id in deps:
            out.append(row)
    return out


def _block_downstream(bb, project_id: str, intent_id: str, *, reason: str) -> list[str]:
    """把直接依赖 intent_id 的 **pending** 下游置 blocked（前置已彻底没成功）。

    只动 pending：claimed/running 的下游即便前置失败也在产出证据，交其自然
    收尾；paused 属人工暂停，不越权改写。返回被置 blocked 的下游 Intent id。
    """
    ts = now()
    blocked: list[str] = []
    with bb._tx():
        for row in bb.conn.execute(
                "SELECT id, depends_on FROM artex_intents WHERE project_id=? AND status='pending'",
                (project_id,)).fetchall():
            deps = []
            try:
                deps = json.loads(row["depends_on"] or "[]")
            except (TypeError, ValueError):
                deps = []
            if intent_id not in [str(x) for x in deps]:
                continue
            cur = bb.conn.execute(
                "UPDATE artex_intents SET status='blocked', error=?, updated_at=?"
                " WHERE id=? AND project_id=? AND status='pending'",
                (reason[:4000], ts, row["id"], project_id))
            if cur.rowcount == 1:
                blocked.append(row["id"])
    for bid in blocked:
        bb.append_event(project_id, "artex.intent.blocked",
                        {"intent_id": bid, "reason": reason[:400],
                         "blocked_by": intent_id})
        # 级联改状态同样要回写探索图节点（artex-explore-status-sync）：下游节点
        # 若不同步，会永远停在创建时的 pending，图上看起来「还有一堆没派」。
        _sync_explore(bb, project_id, bid)
    return blocked


def _unblock_downstream(bb, project_id: str, intent_id: str) -> list[str]:
    """前置重试成功（completed）时解冻下游：依赖全满足的 blocked 下游回 pending。

    与 ``_block_downstream`` 对称。恢复条件是**全量依赖已满足**（而非仅前置
    completed）——串行链下游可能挂了多个前置，只要还有一个没完成就继续等，
    避免"前一个刚完成就把下游放出去"导致它仍拿不到该拿的东西。

    只动 blocked：pending/claimed/running/paused 一律不碰。
    """
    ts = now()
    resumed: list[str] = []
    with bb._tx():
        for row in bb.conn.execute(
                "SELECT id, depends_on FROM artex_intents WHERE project_id=? AND status='blocked'",
                (project_id,)).fetchall():
            try:
                deps = [str(x) for x in json.loads(row["depends_on"] or "[]") if str(x)]
            except (TypeError, ValueError):
                deps = []
            if intent_id not in deps:
                continue
            intent = get_intent(bb, project_id, row["id"])
            if intent is None:
                continue
            ready, _unsatisfied, broken = _dep_state(bb, project_id, intent)
            if not ready or broken:
                continue  # 还有前置未完成：继续等，不同时解冻
            cur = bb.conn.execute(
                "UPDATE artex_intents SET status='pending', error='', updated_at=?"
                " WHERE id=? AND project_id=? AND status='blocked'",
                (ts, row["id"], project_id))
            if cur.rowcount == 1:
                resumed.append(row["id"])
    for rid in resumed:
        bb.append_event(project_id, "artex.intent.unblocked",
                        {"intent_id": rid, "unblocked_by": intent_id})
        # 解冻同样回写（blocked → pending）：否则节点会停在 blocked，而调度层
        # 已经把它放回队列，两边说法不一致。
        _sync_explore(bb, project_id, rid)
    return resumed


def reopen_intent(bb, project_id: str, intent_id: str, *,
                  reason: str = "") -> dict:
    """把一条**没善终**的 Intent（blocked/exhausted/failed）重开回 pending。

    artex-exhausted：上游 ``db/exploration.go::ReopenIntent`` 同款语义——撞预算
    上限（exhausted）或被判 blocked 的 work 并非「这个方向不行」，而是「本次没
    跑完/外部故障」；Planner 看过 trace 后若判定「接着跑」，就用它把这条意图
    放回 Frontier（保留 attempt 计数与 depends_on，不清历史）。

    只接受 UNFINISHED_STATUSES：completed/cancelled 不可重开（已确认覆盖 /
    人工放弃）。返回更新后的 Intent；不合法状态抛 ValueError。
    """
    row = get_intent(bb, project_id, intent_id)
    if row is None:
        raise LookupError(f"ARTEX Intent 不存在: {intent_id}")
    current = str(row.get("status") or "")
    if current not in UNFINISHED_STATUSES:
        raise ValueError(
            f"Intent 当前状态 {current} 不能重开（只支持 "
            + "/".join(sorted(UNFINISHED_STATUSES)) + "）")
    ts = now()
    with bb._tx():
        bb.conn.execute(
            "UPDATE artex_intents SET status='pending', claimed_by='', claimed_at=NULL, "
            "started_at=NULL, error='', updated_at=? "
            "WHERE id=? AND project_id=? AND status=?",
            (ts, intent_id, project_id, current))
    bb.append_event(project_id, "artex.intent.reopened",
                    {"intent_id": intent_id, "from": current,
                     **({"reason": reason[:400]} if reason else {})})
    aggregate_parent_status(bb, project_id, intent_id)
    return get_intent(bb, project_id, intent_id) or {}


def recover_stale_intents(bb, project_id: str, *, older_than_seconds: float = 300,
                          max_attempts: int = 3) -> list[dict]:
    """回收服务重启遗留的 claimed/running Intent。"""
    cutoff = now()
    recovered: list[dict] = []
    rows = bb.conn.execute(
        "SELECT * FROM artex_intents WHERE project_id=? AND status IN ('claimed','running')",
        (project_id,)).fetchall()
    from datetime import datetime, timezone
    for raw in rows:
        row = _hydrate(_row_to_dict(raw) or {})
        stamp = row.get("started_at") or row.get("claimed_at") or row.get("updated_at")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
        except (TypeError, ValueError):
            age = older_than_seconds + 1
        if age < older_than_seconds:
            continue
        target = "failed" if int(row.get("attempt") or 0) >= max(1, int(max_attempts)) else "pending"
        ts = now()
        with bb._tx():
            if target == "pending":
                bb.conn.execute(
                    "UPDATE artex_intents SET status='pending', claimed_by='', claimed_at=NULL, "
                    "started_at=NULL, updated_at=? WHERE id=? AND project_id=? AND status IN ('claimed','running')",
                    (ts, row["id"], project_id))
            else:
                bb.conn.execute(
                    "UPDATE artex_intents SET status='failed', finished_at=?, error=?, updated_at=? "
                    "WHERE id=? AND project_id=? AND status IN ('claimed','running')",
                    (ts, "服务重启后超过最大尝试次数，已标记失败", ts, row["id"], project_id))
        bb.append_event(project_id, "artex.intent.recovered", {
            "intent_id": row["id"], "from": row["status"], "to": target,
            "attempt": row.get("attempt", 0), "age_seconds": round(age),
        })
        updated = get_intent(bb, project_id, row["id"])
        if updated:
            recovered.append(updated)
    return recovered


def aggregate_parent_status(bb, project_id: str, intent_id: str) -> list[dict]:
    """根据直接子 Intent 状态聚合父节点，并向上递归传播。"""
    changed: list[dict] = []
    current_id = str(intent_id or "")
    while current_id:
        child = get_intent(bb, project_id, current_id)
        if not child:
            break
        parent_id = str(child.get("parent_id") or "")
        if not parent_id:
            break
        parent = get_intent(bb, project_id, parent_id)
        if not parent:
            break
        children = [_hydrate(_row_to_dict(row) or {}) for row in bb.conn.execute(
            "SELECT * FROM artex_intents WHERE project_id=? AND parent_id=?",
            (project_id, parent_id)).fetchall()]
        if not children:
            break
        statuses = [str(child.get("status") or "pending") for child in children]
        if all(status == "completed" for status in statuses):
            target = "completed"
        elif all(status == "cancelled" for status in statuses):
            target = "cancelled"
        elif any(status == "running" for status in statuses):
            target = "running"
        elif any(status == "claimed" for status in statuses):
            target = "claimed"
        elif any(status == "blocked" for status in statuses):
            target = "blocked"
        elif any(status == "exhausted" for status in statuses) and not any(
                status in {"pending", "paused"} for status in statuses):
            # 有子方向撞预算没做完 → 父方向同样「没跑完」，不能当 completed。
            target = "exhausted"
        elif any(status == "failed" for status in statuses) and not any(
                status in {"pending", "paused"} for status in statuses):
            target = "failed"
        elif all(status == "paused" for status in statuses):
            target = "paused"
        else:
            target = "pending"
        if parent.get("status") != target:
            ts = now()
            fields = ["status=?", "updated_at=?"]
            args: list = [target, ts]
            if target in {"completed", "failed", "cancelled", "exhausted"}:
                fields.append("finished_at=?"); args.append(ts)
            with bb._tx():
                bb.conn.execute(
                    f"UPDATE artex_intents SET {', '.join(fields)} WHERE id=? AND project_id=?",
                    (*args, parent_id, project_id))
            bb.append_event(project_id, "artex.intent.aggregated", {
                "intent_id": parent_id, "from": parent.get("status"), "to": target,
                "children": len(children), "child_statuses": statuses,
            })
            updated = get_intent(bb, project_id, parent_id)
            if updated:
                changed.append(updated)
        current_id = parent_id
    return changed


def pause_task_pending(bb, project_id: str, task_id: str, *, reason: str = "") -> int:
    """把某任务全部 pending Intent 置为 paused（deadline/预算超限时停止新工作）。

    返回暂停条数；幂等——没有 pending 时返回 0。已 claimed/running 的 Worker
    不动，让其自然收尾，避免中途丢失证据。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return 0
    ts = now()
    with bb._tx():
        rows = bb.conn.execute(
            "SELECT id FROM artex_intents WHERE project_id=? AND task_id=? AND status='pending'",
            (project_id, task_id)).fetchall()
        for row in rows:
            bb.conn.execute(
                "UPDATE artex_intents SET status='paused', updated_at=?"
                " WHERE id=? AND project_id=? AND status='pending'",
                (ts, row["id"], project_id))
    count = len(rows)
    if count:
        bb.append_event(project_id, "artex.task.paused",
                        {"task_id": task_id, "paused": count, "reason": reason[:200]})
    return count


def control_intent(bb, project_id: str, intent_id: str, action: str) -> dict:
    """执行任务级控制动作；暂停任务不会被 Frontier 领取。"""
    row = get_intent(bb, project_id, intent_id)
    if not row:
        raise LookupError(f"ARTEX Intent 不存在: {intent_id}")
    current = row["status"]
    transitions = {
        "pause": {"pending": "paused", "claimed": "paused", "running": "paused"},
        "resume": {"paused": "pending"},
        "cancel": {"pending": "cancelled", "claimed": "cancelled", "running": "cancelled", "paused": "cancelled"},
        # retry 覆盖「彻底失败」与「撞预算没做完」两类：failed 重派、exhausted
        # 续跑（上游 ReopenIntent 同款——exhausted 不是「方向不行」，是「没跑完」）。
        "retry": {"failed": "pending", "exhausted": "pending", "blocked": "pending"},
    }
    target = transitions.get(action, {}).get(current)
    if target is None:
        raise ValueError(f"Intent 当前状态 {current} 不支持动作 {action}")
    if action == "retry":
        return retry_intent(bb, project_id, intent_id)
    ts = now()
    fields = ["status=?", "updated_at=?"]
    args: list = [target, ts]
    if target in ("cancelled",):
        fields.append("finished_at=?"); args.append(ts)
    if target == "pending":
        fields.extend(["claimed_by=''", "claimed_at=NULL", "started_at=NULL"])
    args.extend([intent_id, project_id])
    with bb._tx():
        bb.conn.execute(f"UPDATE artex_intents SET {', '.join(fields)} WHERE id=? AND project_id=?", args)
    bb.append_event(project_id, f"artex.intent.{action}",
                    {"intent_id": intent_id, "from": current, "to": target})
    if target in ("cancelled", "failed"):
        blocked = _block_downstream(bb, project_id, intent_id,
                                    reason=f"前置 Intent 已{target}")
    else:
        blocked = []
    aggregate_parent_status(bb, project_id, intent_id)
    out = get_intent(bb, project_id, intent_id) or {}
    if blocked:
        out["blocked_downstream"] = blocked
    return out


def transition_intent(bb, project_id: str, intent_id: str, status: str, *,
                      result: str = "", error: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(f"非法 ARTEX Intent 状态: {status}")
    row = get_intent(bb, project_id, intent_id)
    if not row:
        raise LookupError(f"ARTEX Intent 不存在: {intent_id}")
    if status == "running" and row["status"] not in ("claimed", "running"):
        raise ValueError(f"Intent 当前状态 {row['status']} 不能开始执行")
    # 收尾（含 exhausted：撞预算上限/超时，本次运行结束但没做完）只允许从
    # 执行中的状态进入；failed 允许自我覆盖（重试失败的 Worker 再失败一次）。
    if status in ("completed", "failed", "cancelled", "exhausted") and \
            row["status"] not in ("claimed", "running", "failed"):
        raise ValueError(f"Intent 当前状态 {row['status']} 不能收尾")
    ts = now()
    fields = ["status=?", "updated_at=?"]
    args: list = [status, ts]
    if status == "running":
        fields.append("started_at=COALESCE(started_at, ?)"); args.append(ts)
    if status in ("completed", "failed", "cancelled", "exhausted"):
        fields.append("finished_at=?"); args.append(ts)
    if result:
        fields.append("result=?"); args.append(result[:12000])
    if error:
        fields.append("error=?"); args.append(error[:4000])
    args.extend([intent_id, project_id])
    with bb._tx():
        bb.conn.execute(f"UPDATE artex_intents SET {', '.join(fields)} WHERE id=? AND project_id=?", args)
    bb.append_event(project_id, f"artex.intent.{status}",
                    {"intent_id": intent_id, **({"error": error[:400]} if error else {})})
    # 依赖级联：前置彻底没成功（failed/cancelled/exhausted）时，其 pending 下游
    # 一并置 blocked（不再空等）；前置完成时，把依赖已全满足的 blocked 下游解冻
    # 回 pending（重试前置后恢复）。exhausted 视同「没拿到可靠产出」——下游若
    # 依赖它的 fact，硬跑也拿不到东西。
    if status in ("failed", "cancelled", "exhausted"):
        _block_downstream(bb, project_id, intent_id, reason=f"前置 Intent 已{status}")
    elif status == "completed":
        _unblock_downstream(bb, project_id, intent_id)
    aggregate_parent_status(bb, project_id, intent_id)
    row = get_intent(bb, project_id, intent_id) or {}
    # 探索图节点状态同步（artex-explore-status-sync，2026-10-21）：见 `_sync_explore`。
    # **必须放在 aggregate_parent_status 之后**：父 Intent 的状态会被子任务聚合改写，
    # 先同步父节点会写进一个马上过期的值。
    _sync_explore(bb, project_id, row)
    return row


def task_graph(bb, project_id: str, task_id: str) -> dict:
    """返回一个 ARTEX task_id 的父子 Intent 图与注册目标摘要/覆盖度。"""
    rows = list_intents(bb, project_id, limit=1000)
    nodes = [row for row in rows if row.get("task_id") == str(task_id)]
    ids = {row["id"] for row in nodes}
    edges = [{"parent_id": row["parent_id"], "child_id": row["id"]}
             for row in nodes if row.get("parent_id") in ids]
    try:
        from core.blackboard.artex_goals import list_goals
        goals = list_goals(bb, project_id, str(task_id))
    except Exception:  # noqa: BLE001 —— 兼容无目标表/旧库
        goals = []
    try:
        from core.blackboard.artex_coverage import task_coverage
        coverage = task_coverage(bb, project_id, str(task_id))
    except Exception:  # noqa: BLE001 —— 兼容无范围资产表/旧库
        coverage = {"enabled": False}
    return {"task_id": str(task_id), "nodes": nodes, "edges": edges,
            "goals": goals, "coverage": coverage}


def list_intents(bb, project_id: str, *, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM artex_intents WHERE project_id=?"
    args: list = [project_id]
    if status:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"; args.append(max(1, min(int(limit), 1000)))
    return [_hydrate(_row_to_dict(row) or {}) for row in bb.conn.execute(sql, args).fetchall()]
