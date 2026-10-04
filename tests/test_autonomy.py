"""自主级别 / sessions_cap / 用量记账测试（批 2，DESIGN §6.8，不触网）。"""

import json

import pytest

from core import autonomy
from core.autonomy import normalize_autonomy, autonomy_of, record_llm_usage
from core.blackboard import Blackboard
from core.llm.provider import Usage
from core.projects import ProjectStore


# ---------- 配置归一化 ----------

def test_default_level_by_track():
    assert normalize_autonomy(None, track="ctf")["level"] == "L0"
    assert normalize_autonomy(None, track="pentest")["level"] == "L1"
    assert normalize_autonomy(None, track="redteam")["level"] == "L0"  # R1：红队宁严勿松
    assert normalize_autonomy(None, track="research")["level"] == "L1"
    assert normalize_autonomy(None, track="malware")["level"] == "L0"
    # 未知轨/旧库：保守 L0
    assert normalize_autonomy(None, track=None)["level"] == "L0"


def test_normalize_fills_defaults_and_validates():
    a = normalize_autonomy({"level": "L2"}, track="ctf")
    assert a == {"level": "L2", "paused": False, "sessions_cap": 4,
                 "max_chain_ticks": 3, "token_budget": None, "task_budget": None,
                 "auto_derive": False,  # C2 mission 自动派生开关（缺省关）
                 "max_concurrent_tasks": 3}  # v0.71 并发执行上限（缺省 3）
    with pytest.raises(ValueError):
        normalize_autonomy({"level": "L9"})
    with pytest.raises(ValueError):
        normalize_autonomy({"sessions_cap": 0})
    with pytest.raises(ValueError):
        normalize_autonomy({"sessions_cap": 21})
    with pytest.raises(ValueError):
        normalize_autonomy({"token_budget": -1})
    with pytest.raises(ValueError):
        normalize_autonomy({"token_budget": "100"})
    with pytest.raises(ValueError):
        normalize_autonomy({"paused": "yes"})
    assert normalize_autonomy({"token_budget": 1000})["token_budget"] == 1000


def test_autonomy_of_old_project_without_section():
    # 旧项目 config 无 autonomy 段：读时按轨补默认，不抛错
    assert autonomy_of({}, track="pentest")["level"] == "L1"


# ---------- 双写 ----------

@pytest.fixture()
def pstore(tmp_path):
    return ProjectStore(tmp_path / "ws")


def test_create_project_writes_autonomy_both_places(pstore):
    proj = pstore.create_project("评估演练", "pentest")
    disk = json.loads((proj.path / "project.json").read_text(encoding="utf-8"))
    assert disk["config"]["autonomy"]["level"] == "L1"
    row = proj.bb.get_project(proj.id)
    assert row["config"]["autonomy"]["sessions_cap"] == 4


def test_create_project_respects_explicit_autonomy(pstore):
    proj = pstore.create_project("全自动", "research",
                                 config={"autonomy": {"level": "L2", "paused": True}})
    a = proj.bb.get_project(proj.id)["config"]["autonomy"]
    assert a["level"] == "L2" and a["paused"] is True


def test_create_project_rejects_bad_autonomy(pstore):
    with pytest.raises(ValueError):
        pstore.create_project("坏配置", "ctf", config={"autonomy": {"level": "X"}})


def test_update_config_dual_write_and_merge(pstore):
    proj = pstore.create_project("项目", "ctf", config={"other": {"k": 1}})
    meta = pstore.update_config(
        proj.id, {"autonomy": {"level": "L1", "sessions_cap": 2}}, project=proj)
    # project.json
    disk = json.loads((proj.path / "project.json").read_text(encoding="utf-8"))
    assert disk["config"]["autonomy"]["level"] == "L1"
    assert disk["config"]["autonomy"]["sessions_cap"] == 2
    assert disk["config"]["other"] == {"k": 1}  # 顶层浅合并保留未知键
    # 黑板行
    assert proj.bb.get_project(proj.id)["config"]["autonomy"]["level"] == "L1"
    # 返回视图 + 内存句柄同步（即时生效）
    assert meta["config"]["autonomy"]["level"] == "L1"
    assert proj.meta["config"]["autonomy"]["sessions_cap"] == 2


def test_update_config_rejects_bad_value(pstore):
    proj = pstore.create_project("项目", "ctf")
    with pytest.raises(ValueError):
        pstore.update_config(proj.id, {"autonomy": {"sessions_cap": 99}}, project=proj)


# ---------- 用量记账 ----------

@pytest.fixture()
def bb(tmp_path):
    b = Blackboard(str(tmp_path / "u.db"))
    pid = b.create_project("用量", "pentest")["id"]
    yield b, pid
    b.close()


def test_usage_accumulates_and_emits_event(bb):
    b, pid = bb
    record_llm_usage(b, pid, Usage(100, 50, 10, 5), source="agent",
                     session_id="sess-x", model="m1")
    st = b.usage_state_get(pid)
    assert (st["tokens_in"], st["tokens_out"], st["tokens_cache_read"],
            st["tokens_cache_creation"], st["llm_calls"]) == (100, 50, 10, 5, 1)
    ev = [e for e in b.recent_events(pid) if e["kind"] == "llm.usage"]
    assert len(ev) == 1
    assert ev[0]["payload"]["total_tokens"] == 165
    assert ev[0]["author"] == "sess-x" and ev[0]["session_id"] == "sess-x"


def test_usage_zero_is_skipped(bb):
    b, pid = bb
    assert record_llm_usage(b, pid, Usage(), source="agent",
                            session_id=None, model="m") is None
    assert b.usage_state_get(pid)["llm_calls"] == 0
    assert not [e for e in b.recent_events(pid) if e["kind"] == "llm.usage"]


def test_orchestrator_usage_author(bb):
    b, pid = bb
    record_llm_usage(b, pid, Usage(7, 3), source="orchestrator",
                     session_id=None, model="m")
    ev = [e for e in b.recent_events(pid) if e["kind"] == "llm.usage"]
    assert ev[0]["author"] == "orchestrator"


def test_soft_warning_fires_once_then_resets_when_budget_raised(tmp_path):
    b = Blackboard(str(tmp_path / "w.db"))
    pid = b.create_project("预算", "ctf",
                           config={"autonomy": normalize_autonomy(
                               {"level": "L0", "token_budget": 100})})["id"]
    # 82 tokens → 跨 80%，首发警告
    record_llm_usage(b, pid, Usage(80, 2), source="agent", session_id=None, model="m")
    warnings = [e for e in b.recent_events(pid) if e["kind"] == "budget.soft_warning"]
    assert len(warnings) == 1 and warnings[0]["payload"]["used"] == 82
    # 继续用但不复位：不重发
    record_llm_usage(b, pid, Usage(10, 0), source="agent", session_id=None, model="m")
    assert len([e for e in b.recent_events(pid) if e["kind"] == "budget.soft_warning"]) == 1
    # 调大预算使用量回落到 80% 以下（90/1000）→ 标志复位
    b.update_project_config(pid, {"autonomy": normalize_autonomy(
        {"level": "L0", "token_budget": 1000})})
    record_llm_usage(b, pid, Usage(0, 0), source="agent",
                     session_id=None, model="m") is None  # 全 0 不会触发记账
    # 用一笔正数记账推动复位检查
    record_llm_usage(b, pid, Usage(1, 0), source="agent", session_id=None, model="m")
    assert b.usage_state_get(pid)["budget_warned"] == 0
    b.close()


def test_hard_block_session_cap(bb):
    b, pid = bb
    s1 = b.register_session(pid, "w1")
    s2 = b.register_session(pid, "w2")
    # 开窗只是 idle，不占活跃名额；默认 cap=4 可继续开窗
    assert autonomy.hard_block_reason(b, pid, "spawn_session") is None
    b.update_project_config(pid, {"autonomy": normalize_autonomy(
        {"level": "L2", "sessions_cap": 2})})
    assert autonomy.hard_block_reason(b, pid, "spawn_session") is None
    assert autonomy.count_active_sessions(b, pid) == 0
    b.set_session_status(s1["id"], "running")
    b.set_session_status(s2["id"], "running")
    reason = autonomy.hard_block_reason(b, pid, "spawn_session")
    assert reason and "sessions_cap=2" in reason
    # 停止一个运行会话后释放名额；closed 会话同样不计入
    b.set_session_status(s1["id"], "idle")
    assert autonomy.hard_block_reason(b, pid, "spawn_session") is None
    b.close_session(s2["id"])
    assert autonomy.hard_block_reason(b, pid, "spawn_session") is None


def test_hard_block_token_budget_blocks_both_autonomous_actions(bb):
    b, pid = bb
    b.update_project_config(pid, {"autonomy": normalize_autonomy(
        {"level": "L2", "token_budget": 100})})
    assert autonomy.hard_block_reason(b, pid, "publish_task") is None
    b.usage_add_llm(pid, ti=100, to=0, cache_read=0, cache_creation=0,
                    token_budget=100)
    assert "Token 预算已用尽" in autonomy.hard_block_reason(b, pid, "publish_task")
    assert "Token 预算已用尽" in autonomy.hard_block_reason(b, pid, "spawn_session")
    # 人手动作只拿警告文案
    assert "预算已用尽" in (autonomy.human_warning(b, pid) or "")


def test_hard_block_task_budget_only_publish(bb):
    b, pid = bb
    b.update_project_config(pid, {"autonomy": normalize_autonomy(
        {"level": "L2", "task_budget": 2})})
    b.usage_inc_tasks(pid)
    b.usage_inc_tasks(pid)
    assert "自主任务预算" in autonomy.hard_block_reason(b, pid, "publish_task")
    # task_budget 不拦开窗
    assert autonomy.hard_block_reason(b, pid, "spawn_session") is None


def test_usage_view_shape(bb):
    b, pid = bb
    view = autonomy.usage_view(b, pid)
    assert view["level"] == "L1"  # pentest（原 pentest）默认
    assert view["active_sessions"] == 0
    assert view["tokens"] == {"used": 0, "budget": None, "pct": None,
                            "cache_read": 0, "cache_creation": 0, "cache_hit": None}
    assert view["tasks"]["published"] == 0
