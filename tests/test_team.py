import json

from core.blackboard.store import Blackboard
from core.team import TeamStore


def _project(bb, pid="p1"):
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO projects(id,name,domain,track,config,created_at) VALUES(?,?,?,?,?,datetime('now'))",
            (pid, "Team 测试", pid, "research", json.dumps({"autonomy": {"sessions_cap": 4}})),
        )


def test_team_create_preflight_and_run_have_no_legacy_side_effect(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = TeamStore(bb)
    team = store.create_team(
        "p1", name="研究组", goal_text="独立分析",
        members=[{"member_key": "static", "label": "静态", "role": "", "runtime": "host"}],
    )
    assert team["members"][0]["member_key"] == "static"
    # 任务机制退役（2026-10-06）：旧 tasks/coordination 表已 DROP——Team 路径
    # 无任何旧表写入面（表根本不存在）。
    names = {r[0] for r in bb.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tasks" not in names and "coordination_tasks" not in names
    assert bb.conn.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0] == 0
    pf = store.preflight("p1", team["id"])
    assert pf["revision"]
    run = store.create_run_and_members("p1", team["id"], revision=pf["revision"], confirmations={"members": True, "goal": True, "safety": True, "execution": True})
    assert run["status"] == "starting"
    assert len(run["members"]) == 1
    assert run["members"][0]["execution_id"].startswith("exec-")
    assert bb.conn.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0] == 1
    bb.close()


def test_team_revision_and_member_cas(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = TeamStore(bb)
    team = store.create_team("p1", name="组", members=[{"member_key": "a"}])
    pf = store.preflight("p1", team["id"])
    changed = store.update_team("p1", team["id"], goal_text="新目标")
    assert changed["revision"] == team["revision"] + 1
    try:
        store.create_run_and_members("p1", team["id"], revision=pf["revision"], confirmations={"members": True, "goal": True, "safety": True, "execution": True})
    except RuntimeError:
        pass
    else:
        raise AssertionError("stale revision must fail")
    current = store.preflight("p1", team["id"])
    run = store.create_run_and_members("p1", team["id"], revision=current["revision"], confirmations={"members": True, "goal": True, "safety": True, "execution": True})
    member = run["members"][0]
    assert store.claim_member(member["id"])["status"] == "creating"
    assert store.claim_member(member["id"]) is None
    bb.close()
