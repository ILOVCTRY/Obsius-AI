"""撤回传播测试（DESIGN.md §6.7 的 1.6 + 1.5 系统侧最小落地）。

覆盖：refs 三层识别（显式 ∪ 正文自动抽取）、撤回仅在非误报→误报跳变触发一次、
四类私信目标（作者/relates_to 反向边下游/context_refs 命中任务及子树认领者/
父任务认领者抄送）、open 任务只挂 stale_refs、done/failed 作者私信不改行、
inbox 未读去重、basis_stale_done 复核事件、Agent 认领开场白与步边界注入。
"""

import json

import pytest

from core.blackboard import Blackboard, TaskQueue
from core.runtime import ExecutionGateway, NativeBackend
from core.orchestrator import Orchestrator


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def project(bb):
    return bb.create_project("撤回测试", "assessment", ["web"])


def _sess(bb, pid, name):
    return bb.register_session(pid, name)["id"]


def _kinds(bb, pid):
    return [e["kind"] for e in bb.recent_events(pid, limit=500)]


# ---------- refs 三层识别 ----------

def test_publish_refs_explicit_plus_autoextract(bb, project):
    pid = project["id"]
    tq = TaskQueue(bb)
    tid = tq.publish(
        pid, "验证 find-0123456789ab 是否可利用，顺带看 find-abcdefabcdef",
        created_by="orchestrator", refs=["find-aaaaaaaaaaaa"])
    refs = tq.get_task(tid)["context_refs"]
    # 显式 refs ∪ 正文自动抽取，去重排序
    assert refs == ["find-0123456789ab", "find-aaaaaaaaaaaa", "find-abcdefabcdef"]


def test_orch_tool_threads_refs(bb, project):
    pid = project["id"]
    orch = Orchestrator(project_id=pid, bb=bb, llm=object())
    out = orch._tool_publish_task(
        objective="复查 find-0123456789ab", task_type="generic",
        refs=["find-bbbbbbbbbbbb"])
    assert "已发布" in out
    task = TaskQueue(bb).list_tasks(pid)[0]
    assert task["context_refs"] == ["find-0123456789ab", "find-bbbbbbbbbbbb"]


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


# ---------- 四类目标全链路 ----------

def test_retraction_routes_all_targets(bb, project):
    pid = project["id"]
    tq = TaskQueue(bb)

    s1 = _sess(bb, pid, "作者")           # a) 被推翻发现作者
    s2 = _sess(bb, pid, "下游")           # b) relates_to 反向边下游作者
    s3 = _sess(bb, pid, "打点者")         # c) claimed 命中任务认领者
    s4 = _sess(bb, pid, "已完成者")       # done 任务作者
    s5 = _sess(bb, pid, "子任务者")       # 命中任务子树认领者
    s6 = _sess(bb, pid, "父协调者")       # d) 父任务认领者抄送
    s7 = _sess(bb, pid, "已关窗")         # closed：不投递

    fid = bb.add_finding(pid, "sqli", "登录框 SQLi", author=s1)["id"]
    bb.add_finding(
        pid, "rce", "SQLi 写 shell", author=s2,
        evidence={"relates_to": [{"finding_id": fid, "note": "前提"}]})
    bb.add_finding(
        pid, "xss", "反射 XSS", author=s7,
        evidence={"relates_to": [{"finding_id": fid}]})
    bb.close_session(s7)

    t_parent = tq.publish(pid, "父任务")
    tq.claim(t_parent, s6)
    t_hit = tq.publish(pid, f"利用 {fid} 打点", parent_id=t_parent, refs=[fid])
    tq.claim(t_hit, s3)
    t_child = tq.publish(pid, "子任务", parent_id=t_hit)
    tq.claim(t_child, s5)
    t_open = tq.publish(pid, f"待认领 {fid}", refs=[fid])
    t_done = tq.publish(pid, f"已完成 {fid}", refs=[fid], created_by=s4)
    tq.claim(t_done, s4)
    tq.complete(t_done, s4, "完成")

    bb.patch_finding(pid, fid, status="false-positive",
                     evidence={"note": "WAF 特征，非注入"})

    inbox_sids = set()
    for s in (s1, s2, s3, s4, s5, s6, s7):
        got = bb.inbox_list(pid, s)
        if got:
            assert got[0]["kind"] == "basis_stale" and got[0]["ref_id"] == fid
            inbox_sids.add(s)
    assert inbox_sids == {s1, s2, s3, s4, s5, s6}  # closed 的 s7 不投递

    # unread 计数挂到 sessions 行
    unread = {s["id"]: s["unread"] for s in bb.list_sessions(pid)}
    assert all(unread[s] == 1 for s in (s1, s2, s3, s4, s5, s6))
    assert unread[s7] == 0

    # 每个新私信一条 message.inbox 事件（session_id=收件人）
    inbox_events = [e for e in bb.recent_events(pid, limit=500)
                    if e["kind"] == "message.inbox"]
    assert {e["session_id"] for e in inbox_events} == {s1, s2, s3, s4, s5, s6}

    # claimed/open 任务挂 stale_refs；父任务与 done 任务不改行
    assert tq.get_task(t_hit)["stale_refs"] == [fid]
    assert tq.get_task(t_child)["stale_refs"] == [fid]
    assert tq.get_task(t_open)["stale_refs"] == [fid]
    assert tq.get_task(t_parent)["stale_refs"] == []
    assert tq.get_task(t_done)["stale_refs"] == []


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


# ---------- basis_stale_done 复核钩子 ----------

def test_basis_stale_done_only_on_complete(bb, project):
    pid = project["id"]
    tq = TaskQueue(bb)
    sid = _sess(bb, pid, "S")

    done = tq.publish(pid, "完成任务")
    tq.claim(done, sid)
    assert tq.add_stale_ref(done, "find-0123456789ab") is True
    tq.complete(done, sid, "仍可利用")
    events = [e for e in bb.recent_events(pid) if e["kind"] == "task.basis_stale_done"]
    assert len(events) == 1
    assert events[0]["payload"]["stale_refs"] == ["find-0123456789ab"]

    failed = tq.publish(pid, "失败任务")
    tq.claim(failed, sid)
    tq.add_stale_ref(failed, "find-0123456789ab")
    tq.fail(failed, sid, "打不动")
    assert _kinds(bb, pid).count("task.basis_stale_done") == 1  # fail 不补发

    # done/failed 任务不允许再挂标
    assert tq.add_stale_ref(done, "find-aaaaaaaaaaaa") is False


# ---------- Agent：认领开场白 + 步边界注入 ----------

def _agent_env(tmp_path):
    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("撤回 agent 测试", "assessment", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    tq = TaskQueue(bb)
    return bb, project, gw, tq, tmp_path


def test_agent_claim_injects_stale_notice(tmp_path):
    from test_agent import ScriptedLLM, make_agent

    bb, project, gw, tq, _ = _agent_env(tmp_path)
    pid = project["id"]
    fid = bb.add_finding(pid, "sqli", "登录框 SQLi", author="sess-other")["id"]
    tid = tq.publish(pid, f"利用 {fid} 打点", created_by="orchestrator")
    assert tq.get_task(tid)["context_refs"] == [fid]
    bb.patch_finding(pid, fid, status="false-positive", evidence={"note": "误报"})
    assert tq.get_task(tid)["stale_refs"] == [fid]

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "改道成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "改道"})]},
    ])
    agent = make_agent((bb, project, gw, tq, tmp_path), llm)
    assert agent.run_next_task() == "改道"

    first = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    assert "依据撤回" in first and fid in first and "三选一" in first
    # open 任务认领时无私信行；告警只出现一次（第二步不重复）
    assert json.dumps(llm.calls[1]["messages"], ensure_ascii=False).count("依据撤回") == 1
    assert tq.get_task(tid)["status"] == "done"
    assert "task.basis_stale_done" in _kinds(bb, pid)


# ---------- A4：finding_update 信息式增补通知 ----------

def _finding_update_rows(bb, pid, sid):
    return [m for m in bb.inbox_list(pid, sid) if m["kind"] == "finding_update"]


def test_finding_new_does_not_notify(bb, project):
    """首次创建 finding 不发 finding_update。"""
    pid = project["id"]
    bb.add_finding(pid, "sqli", "新洞", author="human")
    assert not [e for e in bb.recent_events(pid, limit=50)
                if e["kind"] == "message.inbox"
                and e["payload"].get("kind") == "finding_update"]


def test_merge_update_routes_to_claimants_aggregated(bb, project):
    """merge 实质增补：claimed 命中任务（子树）+父认领者各收一条聚合 DM；
    open/done 任务不收；作者本人不收；closed 不收。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    s_parent = _sess(bb, pid, "父协调者")
    s_worker = _sess(bb, pid, "打点者")
    s_child = _sess(bb, pid, "子任务者")
    s_closed = _sess(bb, pid, "已关窗")
    s_reporter = _sess(bb, pid, "报告者")

    fid = bb.add_finding(pid, "sqli", "登录框 SQLi",
                         severity="info", author="human")["id"]
    t_parent = tq.publish(pid, "父任务")
    tq.claim(t_parent, s_parent)
    t_hit = tq.publish(pid, f"利用 {fid}", parent_id=t_parent, refs=[fid])
    tq.claim(t_hit, s_worker)
    t_child = tq.publish(pid, "子任务", parent_id=t_hit)
    tq.claim(t_child, s_child)
    t_open = tq.publish(pid, f"待认领 {fid}", refs=[fid])
    t_done = tq.publish(pid, f"已完成 {fid}", refs=[fid], created_by=s_closed)
    tq.claim(t_done, s_closed)
    tq.complete(t_done, s_closed, "done")
    bb.close_session(s_closed)

    # 报告者上报增补：新 POC + 严重度升高（多变化聚合一条）
    bb.add_finding(
        pid, "sqli", "登录框 SQLi", severity="high", author=s_reporter,
        evidence={"pocs": [{"artifact_id": "art-poc1", "note": "延时注入"}]})

    for sid in (s_parent, s_worker, s_child):
        rows = _finding_update_rows(bb, pid, sid)
        assert len(rows) == 1 and rows[0]["ref_id"] == fid
        changes = rows[0]["payload"]["changes"]
        assert "新增 POC" in changes and any("high" in c for c in changes)
    assert _finding_update_rows(bb, pid, s_reporter) == []  # 不给作者本人
    assert _finding_update_rows(bb, pid, s_closed) == []     # closed 不投递
    # open 任务没有认领者，自然无 DM；done 任务也不收
    assert tq.get_task(t_open)["status"] == "open"

    msgs = [e for e in bb.recent_events(pid, limit=500)
            if e["kind"] == "message.inbox"
            and e["payload"].get("kind") == "finding_update"]
    assert {e["session_id"] for e in msgs} == {s_parent, s_worker, s_child}


def test_merge_no_change_no_notify_and_redeliver_after_read(bb, project):
    """无实质变化的重复上报不 DM；读后再有增补可重新投递。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    sid = _sess(bb, pid, "打点者")
    fid = bb.add_finding(pid, "sqli", "SQLi", severity="low",
                         evidence={"pocs": [{"a": 1}]}, author="human")["id"]
    tid = tq.publish(pid, f"利用 {fid}", refs=[fid])
    tq.claim(tid, sid)

    # 同证据同严重度重复上报
    bb.add_finding(pid, "sqli", "SQLi", severity="low", author="sess-other",
                   evidence={"pocs": [{"a": 1}]})
    assert _finding_update_rows(bb, pid, sid) == []

    # 读空 inbox（模拟步边界消费）
    assert bb.inbox_drain(pid, sid) == []
    # 新的实质增补（verified 升级）
    bb.add_finding(pid, "sqli", "SQLi", status="verified", author="sess-other")
    rows = _finding_update_rows(bb, pid, sid)
    assert len(rows) == 1
    assert rows[0]["payload"]["changes"] == ["升级为已验证"]


def test_patch_verified_notifies_but_fp_only_basis_stale(bb, project):
    """PATCH 升 verified 走 finding_update；转误报只走 basis_stale。"""
    pid = project["id"]
    tq = TaskQueue(bb)
    sid = _sess(bb, pid, "打点者")
    fid = bb.add_finding(pid, "sqli", "SQLi", author="human")["id"]
    tid = tq.publish(pid, f"验证 {fid}", refs=[fid])
    tq.claim(tid, sid)

    bb.patch_finding(pid, fid, evidence={"pocs": [{"artifact_id": "art-1"}]})
    rows = _finding_update_rows(bb, pid, sid)
    assert len(rows) == 1 and "新增 POC" in rows[0]["payload"]["changes"]
    bb.inbox_drain(pid, sid)

    bb.patch_finding(pid, fid, status="false-positive", evidence={"note": "误报"})
    inbox = bb.inbox_list(pid, sid, unread_only=True)
    assert [m["kind"] for m in inbox] == ["basis_stale"]
    assert not [m for m in inbox if m["kind"] == "finding_update"]
    assert tq.get_task(tid)["stale_refs"] == [fid]  # 撤回挂标照旧


def test_agent_step_boundary_injects_finding_update(tmp_path):
    from test_agent import ScriptedLLM, make_agent

    bb, project, gw, tq, _ = _agent_env(tmp_path)
    pid = project["id"]
    fid = "find-0123456789ab"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "echo hi", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收到增补"})]},
    ])
    agent = make_agent((bb, project, gw, tq, tmp_path), llm)
    sid = agent.session["id"]
    bb.inbox_post(pid, sid, "finding_update", fid,
                  {"finding_id": fid, "title": "旧依据",
                   "changes": ["新增 POC"], "by": "sess-other"})
    agent.run_task("干活")
    before = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    after = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "发现增补" not in before
    assert "发现增补" in after and "新增 POC" in after
    assert "三选一" not in after  # 信息式，不强制动作


def test_agent_step_boundary_injects_inbox(tmp_path):
    from test_agent import ScriptedLLM, make_agent

    bb, project, gw, tq, _ = _agent_env(tmp_path)
    pid = project["id"]
    fid = "find-0123456789ab"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "echo hi", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收到告警"})]},
    ])
    agent = make_agent((bb, project, gw, tq, tmp_path), llm)
    sid = agent.session["id"]
    # 任务进行中投递：首步看不到，步边界注入后第二步必见
    bb.inbox_post(pid, sid, "basis_stale", fid,
                  {"finding_id": fid, "title": "旧依据", "by": "human"})
    agent.run_task("干活")
    before = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    after = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "依据撤回" not in before
    assert "依据撤回" in after and fid in after
    # drain 已标记已读
    assert bb.inbox_list(pid, sid, unread_only=True) == []
