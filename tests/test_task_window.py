"""会话与任务直派：定窗、同窗互斥、关窗接手，以及停用的自动调度。

存量 bind_session / take_session_next 接口仍有独立兼容测试；新执行入口
start_direct 不从队列自动认领，同一会话一次只能执行一项任务。
"""

import time

import pytest

from core.blackboard import Blackboard, ClaimError, TaskQueue


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def project(bb):
    return bb.create_project("任务窗-测试项目", "pentest", ["web"])


def _tq(bb):
    return TaskQueue(bb)


def _win(bb, project, name):
    """注册一个真实会话行（claimed_by 有外键约束，fake id 会炸）。"""
    return bb.register_session(project["id"], name)["id"]


# ---------- bind_session ----------

def test_start_direct_blocks_second_task_in_same_session(bb, project):
    tq = _tq(bb)
    sid = _win(bb, project, "直派窗口")
    first = tq.publish(project["id"], "第一件", target_session=sid)
    second = tq.publish(project["id"], "第二件", target_session=sid)
    tq.start_direct(first, sid)
    with pytest.raises(ClaimError, match="正在执行任务"):
        tq.start_direct(second, sid)
    assert tq.get_task(second)["status"] == "open"
    assert tq.get_task(first)["status"] == "claimed"
    assert [e["payload"]["task_id"] for e in bb.recent_events(project["id"])
            if e["kind"] == "task.claimed"] == [first]


def test_bind_session_binds_open_unbound_task(bb, project):
    """open 且未绑定的任务可绑定；落 task.window_bound 事件。"""
    pid = project["id"]
    tq = _tq(bb)
    w1 = _win(bb, project, "窗一")
    tid = tq.publish(pid, "专属任务")
    assert tq.get_task(tid)["target_session"] == ""
    tq.bind_session(tid, w1)
    assert tq.get_task(tid)["target_session"] == w1
    evs = [e for e in bb.recent_events(pid) if e["kind"] == "task.window_bound"]
    assert len(evs) == 1 and evs[-1]["payload"]["session_id"] == w1


def test_bind_session_rejects_double_bind_and_non_open(bb, project):
    """重复绑定 / 非 open（claimed）任务绑定 → ValueError（竞态防御）。"""
    tq = _tq(bb)
    w1, w2, w3 = (_win(bb, project, n) for n in ("甲", "乙", "丙"))
    tid = tq.publish(project["id"], "已绑任务")
    tq.bind_session(tid, w1)
    with pytest.raises(ValueError, match="不可重复绑定"):
        tq.bind_session(tid, w2)
    # claimed 后再绑也不行（终态/执行中一律拒绝）
    tq.claim(tid, w1)
    with pytest.raises(ValueError):
        tq.bind_session(tid, w3)


# ---------- 窗内队列：定窗隔离核心闸 ----------

def test_session_queue_isolates_windows(bb, project):
    """委托只在归属窗的队列可见可取；别的窗连起跑都摸不到（claim 硬门控兜底）。"""
    pid = project["id"]
    tq = _tq(bb)
    w1, w2 = _win(bb, project, "甲"), _win(bb, project, "乙")
    t1 = tq.publish(pid, "窗甲的委托", target_session=w1)
    t2 = tq.publish(pid, "窗乙的委托", target_session=w2)
    assert [t["id"] for t in tq.session_queue(pid, w1)] == [t1]
    assert [t["id"] for t in tq.session_queue(pid, w2)] == [t2]
    assert tq.take_session_next(pid, w1) == t1
    with pytest.raises(ClaimError):
        tq.claim(t2, w1)  # target_session 门控兜底


def test_window_keeps_taking_serially_after_terminal(bb, project):
    """会话中心化：委托到终态后窗保持待命——后续委托照样进窗起跑
    （窗不退役、不退出；队列空时 take_session_next 返 None）。"""
    pid = project["id"]
    tq = _tq(bb)
    w1 = _win(bb, project, "甲")
    t1 = tq.publish(pid, "第一件委托", target_session=w1)
    assert tq.take_session_next(pid, w1) == t1
    tq.complete(t1, w1, "完成")
    assert tq.take_session_next(pid, w1) is None  # 队列空
    t2 = tq.publish(pid, "第二件委托", target_session=w1)
    assert tq.take_session_next(pid, w1) == t2  # 窗复用，照常起跑


def test_unassign_session_spares_terminal_tasks(bb, project):
    """unassign_session 只清 open 行：终态任务保留 target_session
    （延续模式数据源；窗关了任务还认自己的窗）。"""
    pid = project["id"]
    tq = _tq(bb)
    w1 = _win(bb, project, "窗一")
    t_open = tq.publish(pid, "还开着的")
    t_done = tq.publish(pid, "已完成的")
    tq.bind_session(t_open, w1)
    tq.bind_session(t_done, w1)
    tq.claim(t_done, w1)
    tq.complete(t_done, w1)
    affected = tq.unassign_session(w1)
    assert affected == [t_open]
    assert tq.get_task(t_open)["target_session"] == ""
    assert tq.get_task(t_done)["target_session"] == w1


def test_reopen_keeps_binding_for_original_window(bb, project):
    """reopen 保留 target_session：失败任务归原绑定窗（同窗续跑）。"""
    pid = project["id"]
    tq = _tq(bb)
    w1 = _win(bb, project, "失败窗")
    tid = tq.publish(pid, "失败再放回")
    tq.bind_session(tid, w1)
    tq.claim(tid, w1)
    tq.fail(tid, w1, "炸了")
    tq.reopen(tid, by="human")
    row = tq.get_task(tid)
    assert row["status"] == "open" and row["target_session"] == w1
    # 放回后原窗能再次起跑（同窗续跑路径）
    assert tq.take_session_next(pid, w1) == tid


# ---------- 并发上限（max_concurrent_tasks） ----------

def test_patch_claimed_role_only(tmp_path):
    """执行中委托 PATCH 仅放行 role（热换装通道）：改 objective 等 409；
    未注册角色 422；Agent 不在内存时热换装钩子静默跳过不报错。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _l2_project

    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        # paused：本用例只验 PATCH 规则，不要 worker 干扰——直接建窗+定窗委托+
        # 手动 claim（不经 publish 端点，避免 human-delegate 手动 override 起跑）
        pid = _l2_project(c, "L2改角色", paused=True)
        bb = c.app.state.projects[pid].bb
        from core.blackboard import TaskQueue
        sp = c.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "armed": False})
        sid = sp.json()["id"]
        tq = TaskQueue(bb)
        tid = tq.publish(pid, "要换角色的委托", task_type="recon",
                         target_session=sid)
        tq.claim(tid, sid)
        # 仅 role：放行（200）
        r = c.patch(f"/api/tasks/{tid}", json={"role": "recon"})
        assert r.status_code == 200 and r.json()["role"] == "recon"
        # 其他字段：409（已固化进在跑会话上下文）
        r = c.patch(f"/api/tasks/{tid}", json={"objective": "改不动"})
        assert r.status_code == 409
        # 未注册角色：422
        r = c.patch(f"/api/tasks/{tid}", json={"role": "ghost"})
        assert r.status_code == 422


def _wait_terminal(c, pid, task_ids, tries=600):
    for _ in range(tries):
        rows = {t["id"]: t["status"] for t in c.get(f"/api/projects/{pid}/tasks").json()}
        if all(rows.get(t) in {"done", "failed"} for t in task_ids):
            return rows
        time.sleep(0.02)
    raise AssertionError(f"任务未到终态: {rows}")


def test_closed_origin_reopen_can_spawn_new_window(tmp_path):
    from fastapi.testclient import TestClient
    from test_api import _chain_app, _l2_project

    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        pid = _l2_project(c, "关闭原窗后放回", paused=True)
        sid = c.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"}).json()["id"]
        tq = TaskQueue(c.app.state.projects[pid].bb)
        tid = tq.publish(pid, "待接手任务", target_session=sid)
        tq.start_direct(tid, sid)
        tq.fail(tid, sid, "需补充信息")
        assert c.post(f"/api/sessions/{sid}/close").status_code == 200
        reopened = c.post(f"/api/tasks/{tid}/reopen")
        assert reopened.status_code == 200 and reopened.json()["kicked"] == []
        opened = c.post(f"/api/tasks/{tid}/spawn-window")
        assert opened.status_code == 200 and opened.json()["created"] is True
        new_sid = opened.json()["session_id"]
        assert new_sid != sid and tq.get_task(tid)["target_session"] == new_sid
        assert tq.get_task(tid)["status"] == "open"


def test_scheduler_does_not_claim_unassigned_tasks(tmp_path):
    """停用公共池调度后，sweep 不应把已有 open 任务自动认领或启动。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _l2_project

    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        pid = _l2_project(c, "L2直派", max_concurrent_tasks=2)
        bb = c.app.state.projects[pid].bb
        tq = TaskQueue(bb)
        tids = []
        for i in range(3):
            sp = c.post(f"/api/projects/{pid}/agents",
                        json={"role": "_generalist", "armed": False})
            sid = sp.json()["id"]
            tids.append(tq.publish(pid, f"并发任务{i}", task_type="recon",
                                   target_session=sid))
        c.app.state.schedule_sweep(pid, "poll-sweep")
        assert all(tq.get_task(tid)["status"] == "open" for tid in tids)
        assert not [j for j in c.app.state.jobs.all_jobs()
                    if j["meta"].get("project_id") == pid
                    and j["kind"] == "agent-work" and j["status"] == "running"]


# ---------- v0.72 全局一窗一任务：审批=执行 / kick 收窄 ----------

def test_delegate_window_approval_creates_window_and_runs(tmp_path):
    """L1 delegate_window 审批（会话中心化）：编排器委托开窗的唯一自动路径——
    批准时才开窗（armed）+ 委托写入新窗 + 带活起跑；批准前无窗无委托。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _exec_delegation_script

    app = _chain_app(tmp_path, [], _exec_delegation_script(1))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "批准开窗", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        bb = c.app.state.projects[pid].bb
        # L1 编排器 delegate 无复用窗 → request_approval（用例直写审批单）
        action = {"op": "delegate_window", "role": "_generalist",
                  "objective": "等批准开窗的委托", "task_type": "recon",
                  "noise_budget": "passive", "priority": 2}
        aid = bb.request_approval(pid, action, risk="low",
                                  requested_by="orchestrator")["id"]
        assert bb.list_sessions(pid) == []
        d = c.post(f"/api/approvals/{aid}/decide", json={"decision": "approved"})
        assert d.status_code == 200 and d.json()["executed"] is True
        sid = d.json()["session_id"]
        assert sid
        tq = TaskQueue(bb)
        for _ in range(300):
            rows = tq.list_tasks(pid)
            if rows and rows[0]["status"] in {"done", "failed"}:
                break
            time.sleep(0.02)
        rows = tq.list_tasks(pid)
        assert len(rows) == 1 and rows[0]["status"] == "done"
        assert rows[0]["target_session"] == sid and rows[0]["claimed_by"] == sid


def test_reopen_kicks_only_bound_window(tmp_path):
    """放回只起跑原任务归属窗，不唤醒无关的 armed 空闲会话。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _l2_project, _wait_no_running
    from test_orchestrator import ScriptedLLM as S

    executor = [
        {"tool_use": [S.tool_call("a1", "fail_task",
                                  {"result_note": "缺授权凭据，需要人类补充",
                                   "blocked_reason": "awaiting_human"})]},
        # D6 收尾确认：首次 complete 被闸拦（注入收尾清单），再来一轮零新增后
        # 第二次 complete 才真 done
        {"tool_use": [S.tool_call("c1", "complete_task", {"result_note": "人工已解决"})]},
        {"tool_use": [S.tool_call("c1b", "complete_task", {"result_note": "人工已解决"})]},
    ]
    app = _chain_app(tmp_path, [], executor)
    with TestClient(app) as c:
        pid = _l2_project(c, "kick开窗")
        sp = c.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "armed": True})
        manual_sid = sp.json()["id"]
        assert sp.json().get("job_id")  # 触发点 C：armed + L2 即起（空退）
        _wait_no_running(c, pid)
        # 人类给 manual_sid 显式委派：委托进该窗起跑、脚本无关——故建一个独立执行窗
        ep = c.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "armed": False})
        bound_sid = ep.json()["id"]
        bb = c.app.state.projects[pid].bb
        tid = TaskQueue(bb).publish(
            pid, "会挂起的委托", task_type="generic", target_session=bound_sid)
        # 起跑该窗（manual override）
        c.post(f"/api/agents/{bound_sid}/work")
        for _ in range(300):
            task = TaskQueue(bb).get_task(tid)
            if task["status"] == "failed":
                break
            time.sleep(0.02)
        assert task["status"] == "failed"
        # 等原执行窗 worker 退出（fail 落盘先于 job 收尾，reopen 抢跑会因 job
        # 在跑漏掉 bound_sid 的 kick——on_done 的 _schedule 仍会兜起跑，但 kicked
        # 断言需要稳定口径）
        for _ in range(300):
            running = [j for j in c.app.state.jobs.all_jobs()
                       if j["kind"] == "agent-work"
                       and j["meta"].get("session_id") == bound_sid
                       and j["status"] == "running"]
            if not running:
                break
            time.sleep(0.02)
        # 放回只直派原任务归属窗；无关的 armed 空窗不被唤醒。
        rr = c.post(f"/api/tasks/{tid}/reopen", json={"note": "凭证已补"})
        kicked = rr.json()["kicked"]
        assert rr.status_code == 200 and kicked == [bound_sid]
        assert manual_sid not in kicked
        # 轮询终态（L2 下 replan-wait 30s 节流 job 与断言无关）
        for _ in range(300):
            if TaskQueue(bb).get_task(tid)["status"] == "done":
                break
            time.sleep(0.02)
        assert TaskQueue(bb).get_task(tid)["status"] == "done"


def test_l0_scheduler_never_opens_windows(tmp_path):
    """L0 调度器不自动开窗（会话中心化 2026-09-25）：关窗退回的未指派委托，
    sweep 不重绑不建窗——窗只经人类拍板出现（人开窗/审批 delegate_window/
    提案采纳）。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app

    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "L0不自动开窗", "track": "pentest"}
                     ).json()["id"]
        r = c.patch(f"/api/projects/{pid}/config",
                    json={"config": {"autonomy": {"level": "L0"}}})
        assert r.status_code == 200
        # 人先开窗（L0 下不自跑），委托直接定窗该窗
        sp = c.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "armed": False})
        sid = sp.json()["id"]
        bb = c.app.state.projects[pid].bb
        tid = TaskQueue(bb).publish(pid, "人工窗的委托", task_type="recon",
                                    target_session=sid)
        # 关窗退指派：委托回 open+未指派，L0 sweep 不得自动开窗重绑
        assert c.post(f"/api/sessions/{sid}/close").status_code == 200
        c.app.state.schedule_sweep(pid, "poll-sweep")
        task = next(t for t in c.get(f"/api/projects/{pid}/tasks").json()
                    if t["id"] == tid)
        assert task["status"] == "open" and not task.get("target_session")
        sessions = c.get(f"/api/projects/{pid}/sessions").json()
        assert [s["id"] for s in sessions] == [sid]  # 只剩原窗行，无新窗冒出
