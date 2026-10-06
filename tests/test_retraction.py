"""撤回传播测试（DESIGN.md §6.7 的 1.6 + 1.5 系统侧最小落地）。

任务机制退役（2026-10-06）后收敛为通用传播面：
覆盖：撤回仅在非误报→误报跳变触发一次、私信目标（作者 + relates_to 反向边
下游作者）、closed 会话不投递、inbox 未读去重/已读、首次创建 finding 不通知、
Agent 步边界收件箱注入（basis_stale 强制三选一 / finding_update 信息式）。
"""

import json

import pytest

from core.blackboard import Blackboard
from core.runtime import ExecutionGateway, NativeBackend


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def project(bb):
    return bb.create_project("撤回测试", "pentest", ["web"])


def _sess(bb, pid, name):
    return bb.register_session(pid, name)["id"]


def _kinds(bb, pid):
    return [e["kind"] for e in bb.recent_events(pid, limit=500)]


# ---------- 撤回触发一次，不重放 ----------

def test_retraction_fires_only_on_transition(bb, project):
    pid = project["id"]
    fid = bb.add_finding(pid, "sqli", "登录框 SQLi", author="human")["id"]
    bb.patch_finding(pid, fid, status="false-positive")
    assert _kinds(bb, pid).count("finding.retracted") == 1
    # 重复 PATCH 误报态不重放
    bb.patch_finding(pid, fid, status="false-positive")
    assert _kinds(bb, pid).count("finding.retracted") == 1
    # 普通更新（非误报方向）不触发
    bb.patch_finding(pid, fid, evidence={"note": "x"})
    assert _kinds(bb, pid).count("finding.retracted") == 1


# ---------- 私信目标全链路（作者 + relates_to 下游） ----------

def test_retraction_routes_all_targets(bb, project):
    pid = project["id"]

    s1 = _sess(bb, pid, "作者")           # a) 被推翻发现作者
    s2 = _sess(bb, pid, "下游")           # b) relates_to 反向边下游作者
    s7 = _sess(bb, pid, "已关窗")         # closed：不投递

    fid = bb.add_finding(pid, "sqli", "登录框 SQLi", author=s1)["id"]
    bb.add_finding(
        pid, "rce", "SQLi 写 shell", author=s2,
        evidence={"relates_to": [{"finding_id": fid, "note": "前提"}]})
    bb.add_finding(
        pid, "xss", "反射 XSS", author=s7,
        evidence={"relates_to": [{"finding_id": fid}]})
    bb.close_session(s7)

    bb.patch_finding(pid, fid, status="false-positive",
                     evidence={"note": "WAF 特征，非注入"})

    inbox_sids = set()
    for s in (s1, s2, s7):
        got = bb.inbox_list(pid, s)
        if got:
            assert got[0]["kind"] == "basis_stale" and got[0]["ref_id"] == fid
            inbox_sids.add(s)
    assert inbox_sids == {s1, s2}  # closed 的 s7 不投递

    # unread 计数挂到 sessions 行
    unread = {s["id"]: s["unread"] for s in bb.list_sessions(pid)}
    assert all(unread[s] == 1 for s in (s1, s2))
    assert unread[s7] == 0

    # 每个新私信一条 message.inbox 事件（session_id=收件人）
    inbox_events = [e for e in bb.recent_events(pid, limit=500)
                    if e["kind"] == "message.inbox"]
    assert {e["session_id"] for e in inbox_events} == {s1, s2}


# ---------- inbox 未读去重 / drain / 已读 ----------

def test_inbox_unread_dedup_and_redeliver_after_read(bb, project):
    pid = project["id"]
    sid = _sess(bb, pid, "S")
    fid = "find-0123456789ab"
    assert bb.inbox_post(pid, sid, "basis_stale", fid, {"title": "x"}) is True
    assert bb.inbox_post(pid, sid, "basis_stale", fid, {"title": "x"}) is False
    drained = bb.inbox_drain(pid, sid)
    assert len(drained) == 1
    assert bb.inbox_list(pid, sid, unread_only=True) == []
    # 读后再次投递（同一依据被二次推翻）允许
    assert bb.inbox_post(pid, sid, "basis_stale", fid, {"title": "x"}) is True


def test_inbox_mark_read_scoped(bb, project):
    pid = project["id"]
    sid = _sess(bb, pid, "S")
    bb.inbox_post(pid, sid, "basis_stale", "find-000000000001", {})
    bb.inbox_post(pid, sid, "basis_stale", "find-000000000002", {})
    assert bb.inbox_mark_read(pid, sid, ["nonexistent"]) == 0
    assert bb.inbox_mark_read(pid, sid) == 2
    assert bb.inbox_mark_read(pid, sid) == 0


# ---------- 首次创建不通知 ----------

def test_finding_new_does_not_notify(bb, project):
    """首次创建 finding 不发 finding_update（任务机制退役后该通知恒无目标）。"""
    pid = project["id"]
    bb.add_finding(pid, "sqli", "新洞", author="human")
    assert not [e for e in bb.recent_events(pid, limit=50)
                if e["kind"] == "message.inbox"
                and e["payload"].get("kind") == "finding_update"]


# ---------- Agent：步边界收件箱注入 ----------

def _agent_env(tmp_path):
    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("撤回 agent 测试", "pentest", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    return bb, project, gw, tmp_path


def test_agent_step_boundary_injects_finding_update(tmp_path):
    from test_agent import ScriptedLLM, make_agent, run_objective

    bb, project, gw, tmp_path = _agent_env(tmp_path)
    pid = project["id"]
    fid = "find-0123456789ab"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "echo hi", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收到增补"})]},
    ])
    agent = make_agent((bb, project, gw, tmp_path), llm)
    sid = agent.session["id"]
    bb.inbox_post(pid, sid, "finding_update", fid,
                  {"finding_id": fid, "title": "旧依据",
                   "changes": ["新增 POC"], "by": "sess-other"})
    run_objective(agent, "干活")
    before = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    after = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "发现增补" not in before
    assert "发现增补" in after and "新增 POC" in after
    assert "三选一" not in after  # 信息式，不强制动作


def test_agent_step_boundary_injects_inbox(tmp_path):
    from test_agent import ScriptedLLM, make_agent, run_objective

    bb, project, gw, tmp_path = _agent_env(tmp_path)
    pid = project["id"]
    fid = "find-0123456789ab"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "echo hi", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收到告警"})]},
    ])
    agent = make_agent((bb, project, gw, tmp_path), llm)
    sid = agent.session["id"]
    # 执行中投递：首步看不到，步边界注入后第二步必见
    bb.inbox_post(pid, sid, "basis_stale", fid,
                  {"finding_id": fid, "title": "旧依据", "by": "human"})
    run_objective(agent, "干活")
    before = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    after = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "依据撤回" not in before
    assert "依据撤回" in after and fid in after
    # drain 已标记已读
    assert bb.inbox_list(pid, sid, unread_only=True) == []
