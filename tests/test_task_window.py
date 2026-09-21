"""v0.71 任务即窗口：绑定原语 / 一窗一任务 / 并发上限 / 终态续聊保护。

黑板层用例直接打 TaskQueue（bind_session / claim_next(only_task) /
unassign_session 终态保护）；调度层用例经 app.state.schedule_sweep 直调
（触发点与 publish 路径已由 test_api.py 覆盖，这里验证并发上限语义）。
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


# ---------- claim_next(only_task)：一窗一任务核心闸 ----------

def test_claim_next_only_task_isolates_windows(bb, project):
    """only_task 非空时只认领该任务：别人的任务认领不到；自己的任务认领得到。"""
    pid = project["id"]
    tq = _tq(bb)
    w1, w2 = _win(bb, project, "甲"), _win(bb, project, "乙")
    t1 = tq.publish(pid, "窗甲的任务")
    t2 = tq.publish(pid, "窗乙的任务")
    tq.bind_session(t1, w1)
    tq.bind_session(t2, w2)
    # 甲窗只认领 t1；乙窗连别人的任务占位都拿不到
    assert tq.claim_next(pid, w1, only_task=t1) == t1
    assert tq.claim_next(pid, w2, only_task=t1) is None


def test_bound_window_exits_after_own_task_terminal(bb, project):
    """绑定窗跑完自己的任务（终态）后 claim_next(only_task) 恒空——
    不接公共池新任务（终态续聊窗与任务收尾窗共用的退出语义）。"""
    pid = project["id"]
    tq = _tq(bb)
    w1, w2 = _win(bb, project, "甲"), _win(bb, project, "乙")
    t1 = tq.publish(pid, "自己的任务")
    tq.bind_session(t1, w1)
    assert tq.claim_next(pid, w1, only_task=t1) == t1
    tq.complete(t1, w1, "完成")
    assert tq.claim_next(pid, w1, only_task=t1) is None
    # 即使后来又发布了别的 open 任务（无论有无绑定），该窗都拿不到
    t2 = tq.publish(pid, "别人的新任务")
    tq.bind_session(t2, w2)
    assert tq.claim_next(pid, w1, only_task=t1) is None
    with pytest.raises(ClaimError):
        tq.claim(t2, w1)  # target_session 门控兜底


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
    # 放回后原窗能再次认领（同窗续跑路径）
    assert tq.claim_next(pid, w1, only_task=tid) == tid


# ---------- 并发上限（max_concurrent_tasks） ----------

def test_patch_claimed_role_only(tmp_path):
    """执行中任务 PATCH 仅放行 role（热换装通道）：改 objective 等 409；
    未注册角色 422；Agent 不在内存时热换装钩子静默跳过不报错。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _l2_project

    app = _chain_app(tmp_path, [])
    with TestClient(app) as c:
        # paused：发布建待命窗但不起跑（本用例只验 PATCH 规则，不要 worker 干扰）
        pid = _l2_project(c, "L2改角色", paused=True)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "要换角色的任务", "task_type": "recon"})
        tid = r.json()["task_id"]
        bb = c.app.state.projects[pid].bb
        from core.blackboard import TaskQueue
        tq = TaskQueue(bb)
        # v0.72 一窗一任务：发布即建待命窗，任务由其绑定窗自身认领（外部窗认领被门控拒）
        sid = tq.get_task(tid)["target_session"]
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
    """自动挡同时执行任务数 ≤ max_concurrent_tasks：跑动过程中同时 running 的
    agent-work job 数不超过上限；最终全部任务到终态（不因上限饿死）。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _l2_project, _exec_finish_script

    app = _chain_app(tmp_path, [], _exec_finish_script(3))
    with TestClient(app) as c:
        pid = _l2_project(c, "L2并发上限")
        tids = []
        for i in range(3):
            r = c.post(f"/api/projects/{pid}/tasks",
                       json={"objective": f"并发任务{i}", "task_type": "recon"})
            assert r.status_code == 201
            tids.append(r.json()["task_id"])
        # 采样同时 running 的 agent-work job 峰值
        peak = 0
        rows = None
        for _ in range(600):
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

def test_l1_approval_rebinds_closed_window_and_starts(tmp_path):
    """L1 执行审批（v0.72）：发布建待命窗+执行审批单；批准时窗已被人工关闭 →
    退绑重绑新窗当场起跑（批准=「该任务被授权执行」，不拘泥哪扇窗）。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _exec_finish_script

    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "批准重绑", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "等审批的任务", "task_type": "recon"})
        tid = r.json()["task_id"]
        bound = r.json()["session_id"]
        assert bound
        items = c.get(f"/api/projects/{pid}/approvals").json()
        item = next(a for a in items if a["action"].get("task_id") == tid)
        assert item["status"] == "pending"
        # 人工关掉待命窗（绕过 API 直接退绑+关窗，不触发调度重绑——重绑留给批准时）
        bb = c.app.state.projects[pid].bb
        tq = TaskQueue(bb)
        tq.unassign_session(bound)
        bb.close_session(bound)

        d = c.post(f"/api/approvals/{item['id']}/decide", json={"decision": "approved"})
        assert d.status_code == 200 and d.json()["executed"] is True
        new_sid = d.json()["session_id"]
        assert new_sid and new_sid != bound  # 重绑了新窗
        for _ in range(300):
            row = tq.get_task(tid)
            if row["status"] in {"done", "failed"}:
                break
            time.sleep(0.02)
        row = tq.get_task(tid)
        assert row["status"] == "done" and row["claimed_by"] == new_sid


def test_reopen_kick_skips_unbound_manual_window(tmp_path):
    """v0.72 _kick_workers 收窄：reopen（触发点 D 保留）只踢绑定任务未终态的
    实现窗；armed 无绑手动窗不被踢、也认领不到放回的任务（公共池退役）。"""
    from fastapi.testclient import TestClient

    from test_api import _chain_app, _l2_project, _wait_no_running
    from test_orchestrator import ScriptedLLM as S

    executor = [
        {"tool_use": [S.tool_call("a1", "fail_task",
                                  {"result_note": "缺授权凭据，需要人类补充",
                                   "blocked_reason": "awaiting_human"})]},
        {"tool_use": [S.tool_call("c1", "complete_task", {"result_note": "人工已解决"})]},
    ]
    app = _chain_app(tmp_path, [], executor)
    with TestClient(app) as c:
        pid = _l2_project(c, "kick收窄")
        sp = c.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "armed": True})
        manual_sid = sp.json()["id"]
        assert sp.json().get("job_id")  # 触发点 C：armed + L2 即起（空退）
        _wait_no_running(c, pid)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "会挂起的任务", "task_type": "generic"})
        bound_sid = r.json()["session_id"]
        for _ in range(300):
            tasks = c.get(f"/api/projects/{pid}/tasks").json()
            if tasks[0]["status"] == "failed":
                break
            time.sleep(0.02)
        assert tasks[0]["status"] == "failed"
        # 放回：只踢原绑定窗，armed 手动窗不在列
        rr = c.post(f"/api/tasks/{tasks[0]['id']}/reopen", json={"note": "凭证已补"})
        assert rr.status_code == 200 and rr.json()["kicked"] == [bound_sid]
        # 轮询终态（L2 下 replan-wait 30s 节流 job 与断言无关）
        for _ in range(300):
            if c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "done":
                break
            time.sleep(0.02)
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "done"
