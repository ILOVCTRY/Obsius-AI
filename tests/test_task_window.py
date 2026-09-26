"""会话中心化（2026-09-25）：委托定窗 / 窗内队列 / 串行承接 / 关窗退回 / 并发上限。

黑板层用例直接打 TaskQueue（target_session 定窗 / session_queue /
take_session_next / unassign_session）；调度层用例经 app.state.schedule_sweep
直调（触发点与 publish 路径已由 test_api.py 覆盖，这里验证并发上限语义）。
bind_session 为未指派 open 行的遗留定窗口，用例保留。
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


def test_scheduler_respects_max_concurrent_tasks(tmp_path):
    """自动挡同时执行委托数 ≤ max_concurrent_tasks：sweep 只武装+起跑上限内的窗，
    跑动过程中同时 running 的 agent-work job 数不超过上限；窗内委托随收尾释放
    名额补起（最终全部到终态，不因上限饿死）。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _l2_project, _exec_delegation_script

    app = _chain_app(tmp_path, [], _exec_delegation_script(3))
    with TestClient(app) as c:
        # 显式上限 2（全局缺省 3，假设上限 2 必须自设，防再次漂移）
        pid = _l2_project(c, "L2并发上限", max_concurrent_tasks=2)
        bb = c.app.state.projects[pid].bb
        tq = TaskQueue(bb)
        tids = []
        for i in range(3):
            sp = c.post(f"/api/projects/{pid}/agents",
                        json={"role": "_generalist", "armed": False})
            sid = sp.json()["id"]
            tids.append(tq.publish(pid, f"并发任务{i}", task_type="recon",
                                   target_session=sid))
        # 直调调度器：未武装 L2 窗当场武装起跑，只起 cap=2 个
        c.app.state.schedule_sweep(pid, "poll-sweep")
        # 采样同时 running 的 agent-work job 峰值（预算放宽：全量回归负载下
        # 600 次偶发超时误报「未到终态」，与超卖断言无关）
        peak = 0
        rows = None
        for _ in range(2000):
            running = [j for j in c.app.state.jobs.all_jobs()
                       if j["meta"].get("project_id") == pid
                       and j["kind"] == "agent-work" and j["status"] == "running"]
            peak = max(peak, len(running))
            statuses = {t["id"]: t["status"]
                        for t in c.get(f"/api/projects/{pid}/tasks").json()}
            if all(statuses.get(t) in {"done", "failed"} for t in tids):
                rows = statuses
                break
            time.sleep(0.01)
        assert rows is not None, "任务未全部到终态"
        # 上限 2：峰值不得超卖（调度器逐个重算的合同）
        assert peak <= 2


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


def test_reopen_kicks_all_armed_windows(tmp_path):
    """会话中心化 _kick_workers：reopen（触发点 D）踢全部 armed 且无 job 的会话——
    原执行窗起跑；别的 armed 窗无委托无消息则零成本空退（不消费 LLM）。"""
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
        # 放回：原执行窗必在 kicked；manual armed 窗也被踢（空退）
        rr = c.post(f"/api/tasks/{tid}/reopen", json={"note": "凭证已补"})
        kicked = rr.json()["kicked"]
        assert rr.status_code == 200 and bound_sid in kicked
        assert manual_sid in kicked
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
