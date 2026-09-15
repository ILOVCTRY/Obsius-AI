"""编排器状态持久化 + tick 租约（批 3，DESIGN §6.8 / 机制 1.9）。

- orchestrator_state 一行承载：用量计数（批 2，store.usage_*）、编排游标/轮数
  （event_cursor/cycles/last_digest_cycle）、批 5 自动链字段（chain_*，先存后用）、
  tick 租约（tick_owner/tick_lease_until）。
- tick 租约防同一项目并发编排：单 _tx()（BEGIN IMMEDIATE + 进程写锁）内
  「建行→读租约→他人未过期则拒→否则抢占/续租」，崩溃不释放也会在 900s TTL 后
  自然到期；job 每步可 renew 心跳，结束只清自己的租约（owner 身份释放）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.blackboard.store import now

# load_or_create 在无行时返回的零值默认（键须覆盖 DDL 全部业务列）
_STATE_DEFAULTS: dict[str, object] = {
    "tokens_in": 0,
    "tokens_out": 0,
    "tokens_cache_read": 0,
    "tokens_cache_creation": 0,
    "llm_calls": 0,
    "tasks_published": 0,
    "budget_warned": 0,
    "event_cursor": 0,
    "cycles": 0,
    "last_digest_cycle": -999,
    "chain_active": 0,
    "chain_ticks": 0,
    "last_auto_tick_at": "",
    "auto_ticks_total": 0,
    "tick_owner": "",
    "tick_lease_until": "",
    "last_replan_at": "",
}

# save_fields / increment_counters 允许写的列（tick_owner/lease 只走租约函数）
_EDITABLE_FIELDS = {
    "event_cursor", "cycles", "last_digest_cycle",
    "chain_active", "chain_ticks", "last_auto_tick_at", "auto_ticks_total",
    "last_replan_at",
}
_INCREMENTAL = {"chain_ticks", "auto_ticks_total"}

TICK_LEASE_TTL_SECONDS = 900


class TickLeaseError(Exception):
    """已有未过期的他人 tick 租约（API 层手动 tick → 409；自动 tick → 跳过）。"""


def _lease_active(owner: str, until: str) -> bool:
    """租约当前是否被他人有效持有。''=空闲；ISO 时间 <= 当前即已到期。"""
    if not owner or not until:
        return False
    try:
        expires = datetime.fromisoformat(until)
    except ValueError:
        return False
    return expires > datetime.now(timezone.utc)


def load_or_create(bb, project_id: str) -> dict:
    """读编排状态行；从未建账（无任何 tick/记账）则返回零值默认（不建行，
    首次 acquire/save 时随 INSERT OR IGNORE 惰性落盘）。"""
    row = bb.conn.execute(
        "SELECT * FROM orchestrator_state WHERE project_id=?",
        (project_id,)).fetchone()
    if row is None:
        return dict(_STATE_DEFAULTS)
    d = dict(row)
    # 旧库 ALTER 后理论上列齐；防御性补缺键
    for k, v in _STATE_DEFAULTS.items():
        d.setdefault(k, v)
    return d


def save_fields(bb, project_id: str, **fields) -> dict:
    """白名单整字段 UPDATE 编排状态（event_cursor/cycles/last_digest_cycle 等），
    惰性建行。非法字段名直接 KeyError——编程错误不静默。返回更新后全行。"""
    bad = set(fields) - _EDITABLE_FIELDS
    if bad:
        raise KeyError(f"orchestrator_state 不可写字段: {sorted(bad)}")
    if not fields:
        return load_or_create(bb, project_id)
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    params = dict(fields)
    params.update({"pid": project_id, "now": now()})
    with bb._tx():
        bb.conn.execute(
            "INSERT OR IGNORE INTO orchestrator_state(project_id, updated_at)"
            " VALUES(:pid, '')", params)
        bb.conn.execute(
            f"UPDATE orchestrator_state SET {sets}, updated_at=:now"
            " WHERE project_id=:pid", params)
    return load_or_create(bb, project_id)


def increment_counters(bb, project_id: str, **deltas) -> dict:
    """原子自增批 5 自动链计数器（chain_ticks/auto_ticks_total）；本批先备后用。"""
    bad = set(deltas) - _INCREMENTAL
    if bad:
        raise KeyError(f"不可自增字段: {sorted(bad)}")
    if not deltas:
        return load_or_create(bb, project_id)
    sets = ", ".join(f"{k}={k}+:{k}" for k in deltas)
    params = dict(deltas)
    params.update({"pid": project_id, "now": now()})
    with bb._tx():
        bb.conn.execute(
            "INSERT OR IGNORE INTO orchestrator_state(project_id, updated_at)"
            " VALUES(:pid, '')", params)
        bb.conn.execute(
            f"UPDATE orchestrator_state SET {sets}, updated_at=:now"
            " WHERE project_id=:pid", params)
    return load_or_create(bb, project_id)


def acquire_tick_lease(bb, project_id: str, owner: str,
                       ttl_seconds: int = TICK_LEASE_TTL_SECONDS) -> dict:
    """获取 tick 租约。单事务：他人租约未过期抛 TickLeaseError；空闲或到期
    （含自己已持有）则（重）置 owner/lease_until。返回最新状态行。"""
    if not owner:
        raise ValueError("tick 租约 owner 不能为空")
    lease_until = (datetime.now(timezone.utc)
                   + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    with bb._tx():
        bb.conn.execute(
            "INSERT OR IGNORE INTO orchestrator_state(project_id, updated_at)"
            " VALUES(?, '')", (project_id,))
        cur = bb.conn.execute(
            "SELECT tick_owner, tick_lease_until FROM orchestrator_state"
            " WHERE project_id=?", (project_id,)).fetchone()
        other = cur["tick_owner"]
        if other and other != owner and _lease_active(other, cur["tick_lease_until"]):
            raise TickLeaseError(
                f"项目 {project_id} 已有编排 tick 在执行（owner={other}）")
        bb.conn.execute(
            "UPDATE orchestrator_state SET tick_owner=:owner,"
            " tick_lease_until=:until, updated_at=:now WHERE project_id=:pid",
            {"owner": owner, "until": lease_until, "now": now(),
             "pid": project_id})
    return load_or_create(bb, project_id)


def renew_tick_lease(bb, project_id: str, owner: str,
                     ttl_seconds: int = TICK_LEASE_TTL_SECONDS) -> dict:
    """持有者心跳续租；身份不符（租约到期后被他人抢占）抛 TickLeaseError。"""
    lease_until = (datetime.now(timezone.utc)
                   + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    with bb._tx():
        cur = bb.conn.execute(
            "SELECT tick_owner FROM orchestrator_state WHERE project_id=?",
            (project_id,)).fetchone()
        if cur is None or cur["tick_owner"] != owner:
            raise TickLeaseError("租约已不属于本 tick，停止续租")
        bb.conn.execute(
            "UPDATE orchestrator_state SET tick_lease_until=:until,"
            " updated_at=:now WHERE project_id=:pid",
            {"until": lease_until, "now": now(), "pid": project_id})
    return load_or_create(bb, project_id)


def release_tick_lease(bb, project_id: str, owner: str) -> bool:
    """释放租约：仅当当前 owner 是自己才清空（误释放/过期被抢占后不动作）。
    返回是否真的释放。"""
    with bb._tx():
        cur = bb.conn.execute(
            "SELECT tick_owner FROM orchestrator_state WHERE project_id=?",
            (project_id,)).fetchone()
        if cur is None or cur["tick_owner"] != owner:
            return False
        bb.conn.execute(
            "UPDATE orchestrator_state SET tick_owner='', tick_lease_until='',"
            " updated_at=:now WHERE project_id=:pid",
            {"now": now(), "pid": project_id})
    return True
