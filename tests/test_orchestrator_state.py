"""编排状态持久化 + tick 租约测试（批 3，DESIGN §6.8/机制 1.9），不触网。"""

import threading

import pytest

from core.blackboard import Blackboard
from core.orchestrator import state as ost


@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "s.db"))
    pid = bb.create_project("租约测试", "ctf")["id"]
    yield bb, pid
    bb.close()


def test_load_defaults_without_row(env):
    bb, pid = env
    d = ost.load_or_create(bb, pid)
    assert d["event_cursor"] == 0 and d["cycles"] == 0
    assert d["last_digest_cycle"] == -999
    assert d["tick_owner"] == "" and d["tick_lease_until"] == ""
    # 只读默认不落行（惰性建账）
    assert bb.conn.execute(
        "SELECT COUNT(*) FROM orchestrator_state").fetchone()[0] == 0


def test_acquire_and_release_roundtrip(env):
    bb, pid = env
    row = ost.acquire_tick_lease(bb, pid, "owner-a")
    assert row["tick_owner"] == "owner-a" and row["tick_lease_until"]
    assert ost.release_tick_lease(bb, pid, "owner-a") is True
    row = ost.load_or_create(bb, pid)
    assert row["tick_owner"] == "" and row["tick_lease_until"] == ""


def test_second_owner_blocked_while_lease_active(env):
    bb, pid = env
    ost.acquire_tick_lease(bb, pid, "owner-a")
    # 同 owner 重入 = 续租，允许
    ost.acquire_tick_lease(bb, pid, "owner-a")
    with pytest.raises(ost.TickLeaseError):
        ost.acquire_tick_lease(bb, pid, "owner-b")
    # 他人不能误释放
    assert ost.release_tick_lease(bb, pid, "owner-b") is False
    assert ost.load_or_create(bb, pid)["tick_owner"] == "owner-a"


def test_expired_lease_is_preemptable(env):
    bb, pid = env
    ost.acquire_tick_lease(bb, pid, "dead-owner", ttl_seconds=-1)  # 崩溃即到期
    row = ost.acquire_tick_lease(bb, pid, "owner-b")  # 自然到期后抢占
    assert row["tick_owner"] == "owner-b"
    # 原 owner 已易主，释放/续租都不生效
    assert ost.release_tick_lease(bb, pid, "dead-owner") is False
    with pytest.raises(ost.TickLeaseError):
        ost.renew_tick_lease(bb, pid, "dead-owner")


def test_renew_requires_ownership(env):
    bb, pid = env
    with pytest.raises(ost.TickLeaseError):
        ost.renew_tick_lease(bb, pid, "ghost")  # 行都没有
    ost.acquire_tick_lease(bb, pid, "owner-a")
    ost.renew_tick_lease(bb, pid, "owner-a")  # 持有者续租 OK
    with pytest.raises(ost.TickLeaseError):
        ost.renew_tick_lease(bb, pid, "owner-b")


def test_empty_owner_rejected(env):
    bb, pid = env
    with pytest.raises(ValueError):
        ost.acquire_tick_lease(bb, pid, "")


def test_save_fields_and_increment_counters(env):
    bb, pid = env
    ost.save_fields(bb, pid, event_cursor=11, cycles=3, last_digest_cycle=2)
    row = ost.load_or_create(bb, pid)
    assert row["event_cursor"] == 11 and row["cycles"] == 3
    assert row["last_digest_cycle"] == 2
    # 整字段覆盖
    ost.save_fields(bb, pid, event_cursor=12)
    assert ost.load_or_create(bb, pid)["event_cursor"] == 12
    # 白名单：租约列/用量列不走这里
    with pytest.raises(KeyError):
        ost.save_fields(bb, pid, tick_owner="x")
    with pytest.raises(KeyError):
        ost.save_fields(bb, pid, tokens_in=1)
    # 自增计数器（批 5 自动链先备后用）
    ost.increment_counters(bb, pid, chain_ticks=1, auto_ticks_total=1)
    ost.increment_counters(bb, pid, chain_ticks=1)
    row = ost.load_or_create(bb, pid)
    assert row["chain_ticks"] == 2 and row["auto_ticks_total"] == 1
    with pytest.raises(KeyError):
        ost.increment_counters(bb, pid, cycles=1)


def test_concurrent_acquire_only_one_wins(env):
    """两线程并发抢租约：恰好一个 TickLeaseError（BEGIN IMMEDIATE 串行裁决）。"""
    bb, pid = env
    errors: list[Exception] = []
    start = threading.Event()

    def grab(owner: str):
        start.wait()
        try:
            ost.acquire_tick_lease(bb, pid, owner)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=grab, args=("a",))
    t2 = threading.Thread(target=grab, args=("b",))
    t1.start(); t2.start()
    start.set()
    t1.join(); t2.join()
    assert len(errors) == 1 and isinstance(errors[0], ost.TickLeaseError)
