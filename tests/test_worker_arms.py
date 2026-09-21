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
from pathlib import Path

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
    """L1：任务由发布建的待命窗承接（无人手开窗也建）；人手窗跑队列=武装启动但
    认领恒空（v0.72 公共池退役）；执行审批批准=启动待命窗；暂停解除武装。"""
    pid = client.post("/api/projects", json={"name": "armed闸", "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
    client.patch(f"/api/projects/{pid}/config",
                 json={"config": {"autonomy": {"level": "L1"}}})
    sp = client.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
    assert sp.status_code == 201 and "job_id" not in sp.json()  # 未武装不开工
    sid = sp.json()["id"]
    rows = client.get(f"/api/projects/{pid}/sessions").json()
    assert all(r["worker_armed"] is False and r["worker_running"] is False
               for r in rows)

    # 触发点 D 退役：发任务后不 kick 任何人手窗；任务归自己的待命窗，保持 open
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "侦查目标", "task_type": "generic"})
    assert r.status_code == 201 and r.json()["kicked"] == []
    task_id = r.json()["task_id"]
    bound_sid = r.json()["session_id"]
    assert bound_sid and bound_sid != sid
    assert client.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"

    # 人手窗「跑任务队列」= 武装启动：认领恒空（一窗一任务），任务不被碰
    w = client.post(f"/api/agents/{sid}/work")
    assert w.status_code == 200 and w.json().get("job_id")
    _wait_job(client, w.json()["job_id"])
    _wait_no_running(client, pid)
    rows = {s["id"]: s for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert rows[sid]["worker_armed"] is True
    assert client.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"

    # 执行审批单就绪：批准=启动任务自己的待命窗（不新建）→ 认领完成
    items = client.get(f"/api/projects/{pid}/approvals").json()
    item = next(a for a in items if a["action"].get("task_id") == task_id)
    d = client.post(f"/api/approvals/{item['id']}/decide", json={"decision": "approved"})
    assert d.status_code == 200 and d.json()["executed"] is True
    assert d.json()["session_id"] == bound_sid
    _wait_no_running(client, pid)
    task = client.get(f"/api/projects/{pid}/tasks").json()[0]
    assert task["status"] == "done" and task["claimed_by"] == bound_sid

    # 空闲暂停 → 解除武装
    client.post(f"/api/sessions/{bound_sid}/pause")
    rows = {s["id"]: s for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert rows[bound_sid]["worker_armed"] is False


def test_publish_does_not_kick_manual_window_own_window_runs(tmp_path):
    """v0.72 触发点 D 退役：armed 手动窗发布时不被 kick、也认领不到公共池任务；
    任务由自己的专属窗自动跑完（L2）。"""
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
        assert r.json()["kicked"] == []  # 触发点 D 退役
        # 轮询终态（L2 下 replan-wait 30s 节流 job 与断言无关）
        task = None
        for _ in range(300):
            task = c.get(f"/api/projects/{pid}/tasks").json()[0]
            if task["status"] == "done":
                break
            time.sleep(0.02)
        # 任务由自己的专属窗跑完，手动窗从未碰过
        assert task["status"] == "done"
        assert task["claimed_by"] == task["target_session"] != sid


def _task_window_env(client, pid):
    """发任务 + 退绑发布建的待命窗 + 让「原窗口」认领完成（不经 worker，避免 LLM 依赖）。
    返回 (sess, tid, bound_sid)——bound_sid 是发布建的待命窗（保持存活）。"""
    bb = client.app.state.projects[pid].bb
    sess = bb.register_session(pid, "原窗口", role="recon")
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "逆向 check_flag", "task_type": "generic",
                          "noise_budget": "passive"})
    tid = r.json()["task_id"]
    tq = TaskQueue(bb)
    bound = tq.get_task(tid)["target_session"]
    if bound:  # v0.72：发布即建待命窗，退绑后任务让给测试窗
        tq.unassign_session(bound)
    tq.claim(tid, sess["id"])
    tq.complete(tid, sess["id"], result_note="三处字符串引用已核对")
    return sess, tid, bound


def test_spawn_task_window_idempotent_with_context(client):
    """任务窗：done 任务开新窗（角色沿用/上下文 human_note/meta.spawn_task_id）；
    重复双击幂等返回既有窗；claimed 任务 422；不存在 404。"""
    pid = client.post("/api/projects", json={"name": "任务窗", "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
    sess, tid, bound_sid = _task_window_env(client, pid)

    # v0.72 一窗一任务：任务自己的窗还活着 → 双击任务卡幂等挂回原窗
    r0 = client.post(f"/api/tasks/{tid}/spawn-window")
    assert r0.status_code == 200 and r0.json() == {"session_id": bound_sid,
                                                   "created": False}
    # 原窗已关 → 双击开新复盘窗（角色沿用原认领者/上下文 human_note 注入）
    assert client.post(f"/api/sessions/{bound_sid}/close").status_code == 200

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
    tq3 = TaskQueue(bb2)
    bound3 = tq3.get_task(tid3)["target_session"]
    if bound3:  # v0.72：发布即建待命窗，退绑后手动认领（claimed 422 路径）
        tq3.unassign_session(bound3)
    tq3.claim(tid3, sess2["id"])
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


def test_idle_note_triggers_chat_round(tmp_path):
    """对话化（2026-09-20，翻转 09-19「未武装不 kick」）：空闲窗 note → 端点自动
    踢 worker 跑对话轮直接回应（agent.chat 事件，无任务认领），**未武装无绑定窗
    也回对话**；**绑 open 任务的待命窗绝不 kick**（宁严勿松：L0/L1 审批语义，
    引导滞留收件箱等起跑轮注入）；note 事件带全文；对话轮后任务链照常。"""
    from test_orchestrator import ScriptedLLM as S
    # 剧本：①② 两条对话轮文本回复（未武装 kick 轮消化「你好」、第二轮消化长引导）
    # ③④ 后续任务的 complete_task → finish
    script = [
        {"text": "你好，我是通用测试员。"},
        {"text": "收到，我是通用测试员。"},
        {"tool_use": [S.tool_call("c0", "complete_task", {"result_note": "核查完成"})]},
        {"tool_use": [S.tool_call("f0", "finish", {"summary": "完成"})]},
    ]
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=ScriptedLLM(script), planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "空闲对话", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        c.patch(f"/api/projects/{pid}/config",
                json={"config": {"autonomy": {"level": "L1"}}})
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "_generalist"})
        sid = sp.json()["id"]
        bb = c.app.state.projects[pid].bb

        def work_jobs_of(sid_):
            return [j for j in c.app.state.jobs.all_jobs()
                    if j["kind"] == "agent-work" and j["meta"].get("session_id") == sid_]

        # 未武装无绑定窗：note → kick 对话轮直接回应（无任务认领）
        c.post(f"/api/sessions/{sid}/note", json={"text": "你好"})
        _wait_no_running(c, pid)
        chat_evs = [e for e in bb.recent_events(pid) if e["kind"] == "agent.chat"]
        assert chat_evs and "通用测试员" in chat_evs[-1]["payload"]["text"]

        # 第二轮：>80 字长引导（事件 payload.text 须带全文，title 仍截 80 向后兼容）
        long_text = "介绍下自己，并说明你能查黑板、跑命令、登记发现，举例说明。"
        long_text = long_text * 3  # 29 字 ×3 > 80
        c.post(f"/api/sessions/{sid}/note", json={"text": long_text})
        _wait_no_running(c, pid)
        chat_evs = [e for e in bb.recent_events(pid) if e["kind"] == "agent.chat"]
        assert len(chat_evs) == 2
        notes = [e for e in bb.recent_events(pid) if e["kind"] == "message.inbox"
                 and (e["payload"] or {}).get("kind") == "human_note"]
        assert notes and notes[-1]["payload"]["text"] == long_text
        assert len(notes[-1]["payload"]["title"]) == 80

        # 宁严勿松：绑 open 任务的待命窗（发布即建、未武装）note 不 kick——
        # 无新 agent-work job，引导滞留收件箱未读
        r2 = c.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "侦查目标", "task_type": "generic"})
        assert r2.json()["kicked"] == []  # v0.72 触发点 D 退役：不 kick 人手窗
        tid = r2.json()["task_id"]
        bound_sid = r2.json()["session_id"]
        before = len(work_jobs_of(bound_sid))
        assert c.post(f"/api/sessions/{bound_sid}/note",
                      json={"text": "先别动，等审批"}).status_code == 201
        assert len(work_jobs_of(bound_sid)) == before
        inbox = bb.inbox_list(pid, bound_sid, unread_only=True)
        assert any(r["kind"] == "human_note" for r in inbox)

        # 任务链照常：执行审批批准 → 启动原待命窗认领完成（起跑轮消化滞留引导）
        items = c.get(f"/api/projects/{pid}/approvals").json()
        item = next(a for a in items if a["action"].get("task_id") == tid)
        d = c.post(f"/api/approvals/{item['id']}/decide", json={"decision": "approved"})
        assert d.status_code == 200 and d.json()["executed"] is True
        assert d.json()["session_id"] == bound_sid
        _wait_no_running(c, pid)
        task = c.get(f"/api/projects/{pid}/tasks").json()[0]
        assert task["status"] == "done"


def test_close_finishes_current_step_then_aborts_and_closes(tmp_path):
    """关窗=硬中断全链（2026-09-19，排水退役；■ 即点即停后中断不等当前步——
    LLM 阻塞中 abort 直接放弃等待）：任务执行中途 close → closing（不 409）→
    任务 fail（人工中断）→ worker 自关（不再认领后续任务），后续任务留在队列。"""
    from test_orchestrator import ScriptedLLM as S
    # 两步剧本：第 1 步执行命令（在首个 LLM 调用处阻塞），第 2 步才收尾——
    # 放行后步边界恰好在任务进行中触发 abort（单步 complete 剧本会直接跑完变 done）
    script = [
        {"tool_use": [S.tool_call("c0", "run_cmd",
                                  {"cmd": "whoami", "runtime": "host",
                                   "threat_class": "trusted"})]},
        {"tool_use": [S.tool_call("f0", "finish", {"summary": "完成"})]},
    ]
    gate = threading.Event()
    executor = _GatedScriptedLLM(script, gate)
    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=executor, planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        # 暂停隔离：发布建待命窗但不起跑（本测验证关窗中断语义，任务二须留在自己的待命窗）
        pid = _l2_project(c, "中断关窗", paused=True)
        r1 = c.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "任务一", "task_type": "generic"})
        sid = r1.json()["session_id"]
        assert r1.json()["kicked"] == []
        r2 = c.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "任务二", "task_type": "generic"})
        sid2 = r2.json()["session_id"]
        # 人工 override 启动任务一的绑定窗（暂停不拦人手动作），阻塞在首个 LLM 调用处
        w = c.post(f"/api/agents/{sid}/work")
        assert w.status_code == 200 and w.json().get("job_id")
        # 等 worker 真正认领任务一
        for _ in range(300):
            tasks = {t["objective"]: t for t in c.get(f"/api/projects/{pid}/tasks").json()}
            if tasks["任务一"]["status"] == "claimed":
                break
            time.sleep(0.02)
        assert tasks["任务一"]["claimed_by"] == sid
        # 执行中途关窗 → closing（abort + close_pending）
        r = c.post(f"/api/sessions/{sid}/close")
        assert r.status_code == 200 and r.json()["status"] == "closing"
        # 放行当前步 → 步边界 abort → 任务 fail（人工中断）→ worker 自关
        gate.set()
        _wait_no_running(c, pid)
        for _ in range(300):
            tasks = {t["objective"]: t for t in c.get(f"/api/projects/{pid}/tasks").json()}
            if tasks["任务一"]["status"] == "failed":
                break
            time.sleep(0.02)
        assert tasks["任务一"]["status"] == "failed"
        assert "人工中断" in (tasks["任务一"].get("result_note") or "")
        for _ in range(300):
            rows = {s["id"]: s for s in c.get(f"/api/projects/{pid}/sessions").json()}
            if rows[sid]["status"] == "closed":
                break
            time.sleep(0.02)
        assert rows[sid]["status"] == "closed"
        # 任务二留在自己的待命窗（暂停不挡建窗、不起跑，中断关窗不影响它）
        assert tasks["任务二"]["status"] == "open"
        assert tasks["任务二"]["target_session"] == sid2 != sid
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
        pid = c1.post("/api/projects", json={"name": "重启走查", "track": "pentest",
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
        # v0.64：清扫只认「指针+文件」双在场——补真实快照文件
        (Path(c1.app.state.projects[pid].path) / "snapshots").mkdir(parents=True,
                                                                    exist_ok=True)
        (Path(c1.app.state.projects[pid].path) / "snapshots" / "resume-x.json"
         ).write_text(json.dumps({"messages": [], "task_id": None}),
                      encoding="utf-8")
        tid = c1.post(f"/api/projects/{pid}/tasks",
                      json={"objective": "重启前任务", "task_type": "generic"}
                      ).json()["task_id"]
        tq = TaskQueue(bb)
        bound = tq.get_task(tid)["target_session"]
        if bound:  # v0.72：发布已建待命窗，退绑后让「旧窗」持有孤儿认领
            tq.unassign_session(bound)
        tq.claim(tid, sid)

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
