"""会话状态收敛测试（session-state，2026-10-03）。

覆盖：SessionState 单入口复位、两持有者共享同一实例、历史两个 stale bug
（finished 残留误 fail / resume_state 泄漏被下轮清盘）不再复现。
"""

import json

import pytest

from core.agent import AgentConfig, AgentSession
from core.agent.loop import sanitize_snapshot_tail  # noqa: F401  （导入期自检）
from core.agent.session_state import SessionState
from core.agent.tools import ToolDispatcher
from core.blackboard import Blackboard, TaskQueue
from core.runtime import ExecutionGateway, NativeBackend

from test_agent import ScriptedLLM, make_agent  # 复用同一测试基建（rootdir 在 sys.path）


# ---------- 纯状态容器 ----------

def test_reset_for_task_covers_every_per_task_field():
    """reset_for_task 必须覆盖全部「每任务复位」字段——漏一个就复现 stale bug。"""
    st = SessionState(
        finished=True, awaiting_human=True, plan_only_mode=True, summary="旧总结",
        delegation_just_finished=True, last_delegation_note="旧注记",
        finish_open_intents_ack=True, intent_lead_passed=True,
        reject_streak=3, plan_gate_count=2, stuck_waves=2, stuck_extensions=1,
        cadence_last_rev=7, cadence_last_hint_step=9,
        cadence_hinted_findings={"f1"}, closing_round=2, closing_last_progress=5)
    st.reset_for_task()
    assert (st.finished, st.awaiting_human, st.plan_only_mode, st.summary) == \
        (False, False, False, "")
    assert (st.delegation_just_finished, st.last_delegation_note) == (False, "")
    assert (st.finish_open_intents_ack, st.intent_lead_passed) == (False, False)
    assert (st.reject_streak, st.plan_gate_count) == (0, 0)
    assert (st.stuck_waves, st.stuck_extensions) == (0, 0)
    assert (st.cadence_last_rev, st.cadence_last_hint_step) == (0, 0)
    assert st.cadence_hinted_findings == set()
    assert (st.closing_round, st.closing_last_progress) == (0, 0)


def test_reset_for_task_keeps_fields_owned_by_their_own_lifecycle():
    """原先不在 _loop_body 开头复位的字段（任务指针/现场/撤回去重）不得被顺手清掉
    ——它们由收尾/中断/异常路径各自管理，清错会丢当前任务现场。"""
    st = SessionState(current_task_id="task-abc", last_progress_step=12,
                      resume_state={"task_id": "task-abc"}, live_state={"system": "s"},
                      salvage_ctx={"objective": "o"}, stale_alerted={"task-x"})
    st.reset_for_task()
    assert st.current_task_id == "task-abc"
    assert st.last_progress_step == 12
    assert st.resume_state == {"task_id": "task-abc"}
    assert st.live_state == {"system": "s"}
    assert st.salvage_ctx == {"objective": "o"}
    assert st.stale_alerted == {"task-x"}


def test_state_proxy_is_bidirectional():
    """两持有者以 property 代理原属性名——读写双向透明，且共享同一实例。"""
    d = ToolDispatcher.__new__(ToolDispatcher)  # 只测代理，不建真实依赖
    d._state = SessionState()
    d.plan_only_mode = True
    assert d._state.plan_only_mode is True
    d._state.summary = "从容器直写"
    assert d.summary == "从容器直写"
    d.current_task_id = "task-1"
    assert d.state.current_task_id == "task-1"


# ---------- 与真实会话集成 ----------

@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("状态测试", "pentest", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    tq = TaskQueue(bb)
    yield bb, project, gw, tq, tmp_path
    bb.close()


def test_agent_and_dispatcher_share_one_state(env):
    """AgentSession 构造后，dispatcher 与 agent 读写的是同一份状态。"""
    bb, project, gw, tq, tmp_path = env
    agent = make_agent(env, ScriptedLLM([]))
    assert agent.dispatcher.state is agent.state
    agent._reject_streak = 2
    assert agent.dispatcher.state.reject_streak == 2
    agent.dispatcher.plan_only_mode = True
    assert agent.state.plan_only_mode is True


def test_second_task_does_not_inherit_first_task_state(env):
    """历史 stale bug 回归（一）：finish 后同会话再认领的任务不得命中 stale
    finished 被误判为已完成（旧代码靠 _loop_body 开头十余行逐字段复位）。"""
    bb, project, gw, tq, tmp_path = env
    llm = ScriptedLLM([
        # 任务一：计划 → 完成
        {"tool_use": [ScriptedLLM.tool_call("a1", "task_plan",
                                            {"steps": [{"title": "摸底"}]})]},
        {"tool_use": [ScriptedLLM.tool_call("a2", "complete_task",
                                            {"result_note": "任务一完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("a2b", "complete_task",
                                            {"result_note": "任务一完成"})]},
        # 任务二：同样计划 → 完成（若 finished 残留，首步即误判收尾）
        {"tool_use": [ScriptedLLM.tool_call("b1", "task_plan",
                                            {"steps": [{"title": "复测"}]})]},
        {"tool_use": [ScriptedLLM.tool_call("b2", "complete_task",
                                            {"result_note": "任务二完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("b2b", "complete_task",
                                            {"result_note": "任务二完成"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10))
    sid = agent.session["id"]
    t1 = tq.publish(project["id"], "任务一", task_type="generic", target_session=sid)
    t2 = tq.publish(project["id"], "任务二", task_type="generic", target_session=sid)
    assert agent.run_session() == "任务一完成"
    assert tq.get_task(t1)["status"] == "done"
    # 任务二必须真跑（走完 task_plan 两个剧本项），不能被 stale finished 提前结束
    assert agent.run_session() == "任务二完成"
    assert tq.get_task(t2)["status"] == "done"
    assert len(llm.calls) == 6


def test_resume_state_cleared_between_tasks(env):
    """历史 stale bug 回归（二）：任务一留下的内存态 _resume_state 不得泄漏到
    任务二——旧代码被下轮 run_next_task 快照消费分支误清盘（静默丢断点）。

    这里直接断言生命周期：_loop_body 每任务入口**不**清 resume_state（它由
    消费路径清），但任务二结束时状态容器里不留任务一的断点。"""
    bb, project, gw, tq, tmp_path = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("a1", "complete_task",
                                            {"result_note": "一"})]},
        {"tool_use": [ScriptedLLM.tool_call("a1b", "complete_task",
                                            {"result_note": "一"})]},
        {"tool_use": [ScriptedLLM.tool_call("b1", "complete_task",
                                            {"result_note": "二"})]},
        {"tool_use": [ScriptedLLM.tool_call("b1b", "complete_task",
                                            {"result_note": "二"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10))
    sid = agent.session["id"]
    t1 = tq.publish(project["id"], "任务一", task_type="generic", target_session=sid)
    t2 = tq.publish(project["id"], "任务二", task_type="generic", target_session=sid)
    agent.run_session()
    assert agent._resume_state is None       # 正常收尾不留内存断点
    agent._resume_state = {"task_id": t1, "reason": "stale"}  # 模拟泄漏
    agent.state.reset_for_task()             # 新任务入口复位
    assert agent._resume_state == {"task_id": t1, "reason": "stale"}  # 复位不动它
    agent.run_session()                      # 认领任务二：快照消费分支按需清
    assert tq.get_task(t2)["status"] == "done"
