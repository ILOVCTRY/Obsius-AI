"""F9 worker 启动制（armed）+ 任务窗 + 优雅关窗测试。

- armed 闸：人手开窗默认不接任务；触发点 D/kick 跳过未武装窗；
  「跑任务队列」（POST /agents/{sid}/work）= 启动；⏸暂停解除武装。
- 任务窗：双击已收尾任务卡 → POST /tasks/{tid}/spawn-window（幂等、
  上下文经 human_note 注入、角色沿用原认领者）。
- 优雅关窗：排水标记命中后 worker 自关（跑完当前任务不接新单）。
"""
import json
import threading
import time

import pytest

from fastapi.testclient import TestClient

from core.api.app import create_app
from core.blackboard.tasks import TaskQueue

from test_api import (_chain_app, _exec_finish_script, _l2_project,
                      _wait_job, _wait_no_running)
from test_orchestrator import ScriptedLLM


@pytest.fixture()
def client(tmp_path):
    # executor 剧本一轮 finish（armed 测试里「跑任务队列」要真跑完一个任务）
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=ScriptedLLM(_exec_finish_script(1)),
                     planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        yield c


def test_armed_gate_blocks_auto_and_work_starts(client):
    """L1：人手开窗默认未武装 → 触发点 D 不派活；跑任务队列=启动接单；暂停解除武装。"""
    pid = client.post("/api/projects", json={"name": "armed闸", "track": "assessment",
                                             "capabilities": ["web"]}).json()["id"]
    client.patch(f"/api/projects/{pid}/config",
                 json={"config": {"autonomy": {"level": "L1"}}})
    sp = client.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
    assert sp.status_code == 201 and "job_id" not in sp.json()  # 未武装不开工
    sid = sp.json()["id"]
    rows = client.get(f"/api/projects/{pid}/sessions").json()
    assert rows[0]["worker_armed"] is False and rows[0]["worker_running"] is False

    # 触发点 D：发任务后不 kick 未武装窗，任务保持 open
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "侦查目标", "task_type": "generic"})
    assert r.status_code == 201 and r.json()["kicked"] == []
    assert client.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"

    # 「跑任务队列」= 启动：armed=true + 起 worker 认领完成
    w = client.post(f"/api/agents/{sid}/work")
    assert w.status_code == 200 and w.json().get("job_id")
    _wait_job(client, w.json()["job_id"])
    _wait_no_running(client, pid)
    rows = client.get(f"/api/projects/{pid}/sessions").json()
    assert rows[0]["worker_armed"] is True
    task = client.get(f"/api/projects/{pid}/tasks").json()[0]
    assert task["status"] == "done" and task["claimed_by"] == sid

    # 启动态重复点「跑任务队列」→ 去重不重复起（当前空闲，再起会空跑一轮，允许）
    # 空闲暂停 → 解除武装
    client.post(f"/api/sessions/{sid}/pause")
    rows = client.get(f"/api/projects/{pid}/sessions").json()
    assert rows[0]["worker_armed"] is False


def test_armed_gate_allows_kick_when_armed(tmp_path):
    """武装窗在触发点 D 正常被 kick（闸只拦未武装，不改变 L1 自动语义）。"""
    from test_api import _make_project  # noqa: F401 —— 复用 _chain_app 即可
    app = _chain_app(tmp_path, [], _exec_finish_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "armed放行")
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "_generalist",
                                                         "armed": True})
        sid = sp.json()["id"]
        assert sp.json().get("job_id")  # 触发点 C：armed + L2 未暂停即起
        _wait_no_running(c, pid)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "侦查目标", "task_type": "generic"})
        assert r.json()["kicked"] == [sid]  # 触发点 D 放行
        _wait_no_running(c, pid)
        task = c.get(f"/api/projects/{pid}/tasks").json()[0]
        assert task["status"] == "done"


def _task_window_env(client, pid):
    """注册原认领者会话 + 发任务 + 直认领完成（不经 worker，避免 LLM 依赖）。"""
    bb = client.app.state.projects[pid].bb
    sess = bb.register_session(pid, "原窗口", role="recon")
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "逆向 check_flag", "task_type": "generic",
                          "noise_budget": "passive"})
    tid = r.json()["task_id"]
    tq = TaskQueue(bb)
    tq.claim(tid, sess["id"])
    tq.complete(tid, sess["id"], result_note="三处字符串引用已核对")
    return sess, tid


def test_spawn_task_window_idempotent_with_context(client):
    """任务窗：done 任务开新窗（角色沿用/上下文 human_note/meta.spawn_task_id）；
    重复双击幂等返回既有窗；claimed 任务 422；不存在 404。"""
    pid = client.post("/api/projects", json={"name": "任务窗", "track": "assessment",
                                             "capabilities": ["web"]}).json()["id"]
    sess, tid = _task_window_env(client, pid)

    r = client.post(f"/api/tasks/{tid}/spawn-window")
    assert r.status_code == 200 and r.json()["created"] is True
    sid = r.json()["session_id"]
    assert sid != sess["id"]
    bb = client.app.state.projects[pid].bb
    row = bb.get_session(sid)
    assert row["role"] == "recon"  # 角色沿用原认领者
    assert row["name"].startswith("任务窗·")
    meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
    assert meta["spawn_task_id"] == tid and meta["worker_armed"] is False
    # 上下文经 E8 human_note 注入收件箱（未读）
    inbox = client.get(f"/api/sessions/{sid}/inbox?unread=true").json()
    assert len(inbox) == 1 and inbox[0]["kind"] == "human_note"
    note = inbox[0]["payload"]["text"]
    assert "逆向 check_flag" in note and "三处字符串引用已核对" in note
    assert "历次尝试" in note  # C10：履历（_finish 落库的 attempt）进任务窗摘要
    # 会话列表补 worker 状态灯字段
    rows = {s["id"]: s for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert rows[sid]["worker_armed"] is False

    # 幂等：再次双击 → 既有窗
    r2 = client.post(f"/api/tasks/{tid}/spawn-window")
    assert r2.status_code == 200 and r2.json() == {"session_id": sid, "created": False}

    # claimed 任务 → 422；不存在 → 404
    bb2 = client.app.state.projects[pid].bb
    sess2 = bb2.register_session(pid, "窗口乙", role="recon")
    r3 = client.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "还在跑", "task_type": "generic",
                           "noise_budget": "passive"})
    tid3 = r3.json()["task_id"]
    TaskQueue(bb2).claim(tid3, sess2["id"])
    assert client.post(f"/api/tasks/{tid3}/spawn-window").status_code == 422
    assert client.post("/api/tasks/task-nope123456/spawn-window").status_code == 404


class _GatedScriptedLLM(ScriptedLLM):
    """首次 chat 阻塞在 gate 上——用于在任务执行中途触发排水关窗。"""

    def __init__(self, script, gate: threading.Event):
        super().__init__(script)
        self.gate = gate
        self._gated = False

    def chat(self, *args, **kwargs):
        if not self._gated:
            self._gated = True
            assert self.gate.wait(15), "gate 超时"
        return super().chat(*args, **kwargs)


def test_drain_close_finishes_current_task_then_closes(tmp_path):
    """优雅关窗全链：任务执行中途 close → draining（不 409）→ 当前任务完整跑完
    → worker 自关（不再认领后续任务）。"""
    from test_orchestrator import ScriptedLLM as S
    gate = threading.Event()
    executor = _GatedScriptedLLM(_exec_finish_script(1), gate)
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=executor, planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = _l2_project(c, "排水关窗")
        sp = c.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "armed": True})
        sid = sp.json()["id"]
        _wait_no_running(c, pid)
        # 任务一（会被 worker 认领并在首个 LLM 调用处阻塞）+ 任务二（排水后不得认领）
        r1 = c.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "任务一", "task_type": "generic"})
        assert r1.json()["kicked"] == [sid]
        c.post(f"/api/projects/{pid}/tasks",
               json={"objective": "任务二", "task_type": "generic"})
        # 等 worker 真正认领任务一
        for _ in range(300):
            tasks = {t["objective"]: t for t in c.get(f"/api/projects/{pid}/tasks").json()}
            if tasks["任务一"]["status"] == "claimed":
                break
            time.sleep(0.02)
        assert tasks["任务一"]["claimed_by"] == sid
        # 执行中途关窗 → draining + close_pending
        r = c.post(f"/api/sessions/{sid}/close")
        assert r.status_code == 200 and r.json()["status"] == "draining"
        # 放行执行 → 当前任务完整收尾 → worker 自关
        gate.set()
        _wait_no_running(c, pid)
        for _ in range(300):
            tasks = {t["objective"]: t for t in c.get(f"/api/projects/{pid}/tasks").json()}
            if tasks["任务一"]["status"] == "done":
                break
            time.sleep(0.02)
        assert tasks["任务一"]["status"] == "done"
        for _ in range(300):
            rows = c.get(f"/api/projects/{pid}/sessions").json()
            if rows[0]["status"] == "closed":
                break
            time.sleep(0.02)
        assert rows[0]["status"] == "closed"
        assert tasks["任务二"]["status"] == "open"  # 排水后不再认领
        events = c.app.state.projects[pid].bb.recent_events(pid)
        assert any(e["kind"] == "session.closed" for e in events)


def test_restart_sweep_interrupts_orphan_claims(tmp_path):
    """重启清扫（§3）：进程重启后项目首开——孤儿 claimed 统一 failed(awaiting_human
    「后端重启，任务中断」，不自动重跑)；陈旧 running 归位 idle、带快照 running 归位
    paused、排水未竟（close_pending）补关窗。"""
    ws = str(tmp_path / "workspaces")
    prov = str(tmp_path / "providers.json")

    def _app():
        return create_app(workspace_root=ws, tools_root=None,
                          executor_llm=ScriptedLLM([]), planner_llm=ScriptedLLM([]),
                          providers_config=prov)

    with TestClient(_app()) as c1:
        pid = c1.post("/api/projects", json={"name": "重启走查", "track": "assessment",
                                             "capabilities": ["web"]}).json()["id"]
        bb = c1.app.state.projects[pid].bb
        sess = bb.register_session(pid, "旧窗", role="recon")
        sid = sess["id"]
        bb.set_session_status(sid, "running")           # 重启遗留：running 无快照
        sess2 = bb.register_session(pid, "排水窗", role="recon")
        bb.set_session_status(sess2["id"], "running")
        bb.set_session_meta(sess2["id"], {"close_pending": True})  # 排水被重启打断
        sess3 = bb.register_session(pid, "快照窗", role="recon")
        bb.set_session_status(sess3["id"], "running")
        bb.set_session_meta(sess3["id"], {"resume_snapshot": "resume-x.json"})
        tid = c1.post(f"/api/projects/{pid}/tasks",
                      json={"objective": "重启前任务", "task_type": "generic"}
                      ).json()["task_id"]
        TaskQueue(bb).claim(tid, sid)

    # 「重启」：同 workspace 新 app 实例，首开项目触发清扫
    with TestClient(_app()) as c2:
        rows = {s["id"]: s for s in c2.get(f"/api/projects/{pid}/sessions").json()}
        assert rows[sid]["status"] == "idle"            # 陈旧 running 归位
        assert rows[sess2["id"]]["status"] == "closed"  # close_pending 补关窗
        assert rows[sess3["id"]]["status"] == "paused"  # 带快照可续跑
        task = c2.get(f"/api/projects/{pid}/tasks").json()[0]
        assert task["status"] == "failed"
        assert task["blocked_reason"] == "awaiting_human"
        assert "后端重启" in task["result_note"]
        events = c2.app.state.projects[pid].bb.recent_events(pid)
        failed = [e for e in events if e["kind"] == "task.failed"]
        assert failed and failed[-1]["payload"]["blocked_reason"] == "awaiting_human"
        # 人工决定路径可用：「✅ 已解决，放回继续」= reopen → open
        assert c2.post(f"/api/tasks/{tid}/reopen").status_code == 200
        assert c2.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"
