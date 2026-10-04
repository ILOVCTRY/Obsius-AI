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

from test_api import (_chain_app, _exec_delegation_script, _l2_project,
                      _wait_job, _wait_no_running)
from test_orchestrator import ScriptedLLM


@pytest.fixture()
def client(tmp_path):
    # executor 剧本一件委托（armed 测试里「跑任务队列」要真跑完一个任务）
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=ScriptedLLM(_exec_delegation_script(1)),
                     planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        yield c


def test_armed_gate_blocks_auto_and_work_starts(client):
    """L1 会话中心化：人手开窗默认不武装不开工；未指派委托发布后不建窗不 kick；
    人手窗「跑队列」=武装启动但 run_session 只取本窗委托（未指派的摸不到）；
    人给委托开窗（spawn-window）+ 跑窗=像人类使唤 AI 一样跑完；暂停解除武装。"""
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

    # 发布未指派委托：不建窗、不 kick，session_id=None，任务保持 open
    r = client.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "侦查目标", "task_type": "generic"})
    assert r.status_code == 201 and r.json()["kicked"] == []
    task_id = r.json()["task_id"]
    assert r.json()["session_id"] is None
    assert client.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"

    # 人手窗「跑队列」= 武装启动：run_session 只取 target=本窗 的委托，未指派
    # 任务不被碰，worker 空退
    w = client.post(f"/api/agents/{sid}/work")
    assert w.status_code == 200 and w.json().get("job_id")
    _wait_job(client, w.json()["job_id"])
    _wait_no_running(client, pid)
    rows = {s["id"]: s for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert rows[sid]["worker_armed"] is True
    assert client.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"

    # 人给委托开专属窗（spawn-window：open 无绑→补绑待命窗，armed=false），
    # 再点「跑窗」武装起跑 → 认领完成
    sw = client.post(f"/api/tasks/{task_id}/spawn-window")
    assert sw.status_code == 200 and sw.json()["created"] is True
    bound_sid = sw.json()["session_id"]
    assert bound_sid and bound_sid != sid
    w2 = client.post(f"/api/agents/{bound_sid}/work")
    assert w2.status_code == 200 and w2.json().get("job_id")
    _wait_job(client, w2.json()["job_id"])
    _wait_no_running(client, pid)
    task = client.get(f"/api/projects/{pid}/tasks").json()[0]
    assert task["status"] == "done" and task["claimed_by"] == bound_sid

    # 空闲暂停 → 解除武装
    client.post(f"/api/sessions/{bound_sid}/pause")
    rows = {s["id"]: s for s in client.get(f"/api/projects/{pid}/sessions").json()}
    assert rows[bound_sid]["worker_armed"] is False


def test_publish_does_not_kick_manual_window_untargeted_never_runs(tmp_path):
    """会话中心化：armed 手动窗发布未指派委托时不被 kick，run_session 也摸不到
    未指派委托（任务保持 open）；委托显式指派给另一窗时该窗跑完，手动窗不碰。"""
    app = _chain_app(tmp_path, [], _exec_delegation_script(1))
    with TestClient(app) as c:
        pid = _l2_project(c, "armed放行")
        sp = c.post(f"/api/projects/{pid}/agents", json={"role": "_generalist",
                                                         "armed": True})
        sid = sp.json()["id"]
        assert sp.json().get("job_id")  # 触发点 C：armed + L2 未暂停即起
        _wait_no_running(c, pid)
        r = c.post(f"/api/projects/{pid}/tasks",
                   json={"objective": "侦查目标", "task_type": "generic"})
        assert r.json()["kicked"] == []  # 未指派：不 kick
        assert r.json()["session_id"] is None
        # 等一拍确认手动窗没碰它（无新 job 起、任务仍 open）
        time.sleep(0.2)
        assert c.get(f"/api/projects/{pid}/tasks").json()[0]["status"] == "open"

        # 显式开另一窗并把委托指给它（人类委托=武装+起跑），手动窗从未碰过
        ep = c.post(f"/api/projects/{pid}/agents",
                    json={"role": "_generalist", "armed": False})
        target = ep.json()["id"]
        r2 = c.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "第二个目标", "task_type": "generic",
                          "target_session": target})
        assert r2.status_code == 201 and r2.json()["session_id"] == target
        # 轮询终态（L2 下 replan-wait 30s 节流 job 与断言无关）
        task = None
        for _ in range(300):
            task = next(t for t in c.get(f"/api/projects/{pid}/tasks").json()
                        if t["id"] == r2.json()["task_id"])
            if task["status"] == "done":
                break
            time.sleep(0.02)
        # 任务由被指派窗跑完，手动窗从未碰过
        assert task["status"] == "done"
        assert task["claimed_by"] == task["target_session"] == target != sid
        # 第一件未指派委托还原样留着
        first = next(t for t in c.get(f"/api/projects/{pid}/tasks").json()
                     if t["objective"] == "侦查目标")
        assert first["status"] == "open" and first["target_session"] == ""


def _task_window_env(client, pid):
    """开「原窗口」+ 发委托直接指派该窗 + 认领完成（不经 worker，避免 LLM 依赖）。
    返回 (sess, tid)——sess 就是委托的归属窗（保持存活）。"""
    bb = client.app.state.projects[pid].bb
    sess = bb.register_session(pid, "原窗口", role="recon")
    tq = TaskQueue(bb)
    tid = tq.publish(pid, "逆向 check_flag", task_type="generic",
                     noise_budget="passive", created_by="human",
                     target_session=sess["id"])
    # 与 _spawn_session_for_task 建窗同口径：done 复盘窗幂等检查认 spawn_task_id
    bb.set_session_meta(sess["id"], {"spawn_task_id": tid, "bound_task_id": tid})
    tq.claim(tid, sess["id"])
    tq.complete(tid, sess["id"], result_note="三处字符串引用已核对")
    return sess, tid


def test_spawn_task_window_idempotent_with_context(client):
    """任务窗：done 任务归属窗还活着→双击幂等挂回；归属窗关了→开新复盘窗
    （角色沿用/上下文 human_note/meta.spawn_task_id）；重复双击幂等；
    claimed 无活窗 → 409 引导放回；不存在 404。"""
    pid = client.post("/api/projects", json={"name": "任务窗", "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
    sess, tid = _task_window_env(client, pid)

    # 任务归属窗还活着 → 双击任务卡幂等挂回原窗
    r0 = client.post(f"/api/tasks/{tid}/spawn-window")
    assert r0.status_code == 200 and r0.json() == {"session_id": sess["id"],
                                                   "created": False}
    # 原窗已关 → 双击开新复盘窗（角色沿用原认领者/上下文 human_note 注入）
    assert client.post(f"/api/sessions/{sess['id']}/close").status_code == 200

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

    # claimed 且无活窗 → 409 引导放回（2026-09-23：不再 422）；不存在 → 404
    bb2 = client.app.state.projects[pid].bb
    sess2 = bb2.register_session(pid, "窗口乙", role="recon")
    tq3 = TaskQueue(bb2)
    tid3 = tq3.publish(pid, "还在跑", task_type="generic",
                       noise_budget="passive", created_by="human")
    tq3.claim(tid3, sess2["id"])  # 未指派 open 行可直接认领
    assert client.post(f"/api/tasks/{tid3}/spawn-window").status_code == 409
    assert client.post("/api/tasks/task-nope123456/spawn-window").status_code == 404


def test_spawn_task_window_open_rebind(client):
    """open 无绑任务双击 = 手动补绑待命窗（armed=false 双向绑定，2026-09-23
    放开非终态）；再次双击幂等挂回；退绑后可再补新窗；cap 满 409（人手开窗同口径）。"""
    pid = client.post("/api/projects", json={"name": "补绑窗", "track": "pentest",
                                             "capabilities": ["web"]}).json()["id"]
    bb = client.app.state.projects[pid].bb
    tq = TaskQueue(bb)
    tid = tq.publish(pid, "补绑目标", task_type="generic",
                     noise_budget="passive", created_by="human")
    assert tq.get_task(tid)["target_session"] == ""  # 会话中心化：发布不建窗
    # open 无绑 → 双击补绑待命窗，双向绑定回填
    r1 = client.post(f"/api/tasks/{tid}/spawn-window")
    assert r1.status_code == 200 and r1.json()["created"] is True
    sid = r1.json()["session_id"]
    assert tq.get_task(tid)["target_session"] == sid
    row = bb.get_session(sid)
    meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
    assert meta.get("worker_armed") is False  # 待命不起跑（起跑走跑队列/挡位）
    # 再次双击 → 幂等挂回
    r2 = client.post(f"/api/tasks/{tid}/spawn-window")
    assert r2.status_code == 200 and r2.json() == {"session_id": sid,
                                                   "created": False}
    # 退绑（模拟 cap 满绑定失败的拥塞暂态）→ 双击再补一扇新待命窗
    tq.unassign_session(sid)
    client.post(f"/api/sessions/{sid}/close")
    r2b = client.post(f"/api/tasks/{tid}/spawn-window")
    assert r2b.status_code == 200 and r2b.json()["created"] is True
    sid = r2b.json()["session_id"]
    assert tq.get_task(tid)["target_session"] == sid

    # idle 待命窗不占 active_sessions；只有运行中的会话才占 cap。
    client.patch(f"/api/projects/{pid}/config",
                 json={"config": {"autonomy": {"level": "L0", "paused": True,
                                               "sessions_cap": 1}}})
    tid3 = tq.publish(pid, "超额目标", task_type="generic",
                      noise_budget="passive", created_by="human")
    assert tq.get_task(tid3)["target_session"] == ""
    assert client.post(f"/api/tasks/{tid3}/spawn-window").status_code == 200


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
        {"tool_use": [S.tool_call("c0b", "complete_task", {"result_note": "核查完成"})]},
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

        # 宁严勿松：发未指派委托，再 spawn-window 开绑定它的待命窗（armed=false）
        # ——绑 open 任务的待命窗 note 绝不 kick：无新 agent-work job，引导滞留
        # 收件箱未读
        r2 = c.post(f"/api/projects/{pid}/tasks",
                    json={"objective": "侦查目标", "task_type": "generic"})
        assert r2.json()["kicked"] == []  # 未指派发布不 kick
        tid = r2.json()["task_id"]
        assert r2.json()["session_id"] is None
        sw = c.post(f"/api/tasks/{tid}/spawn-window")
        assert sw.status_code == 200 and sw.json()["created"] is True
        bound_sid = sw.json()["session_id"]
        before = len(work_jobs_of(bound_sid))
        assert c.post(f"/api/sessions/{bound_sid}/note",
                      json={"text": "先别动，等审批"}).status_code == 201
        assert len(work_jobs_of(bound_sid)) == before
        inbox = bb.inbox_list(pid, bound_sid, unread_only=True)
        assert any(r["kind"] == "human_note" for r in inbox)

        # 任务链照常：人点「跑窗」武装起跑 → 认领完成（起跑轮消化滞留引导）
        w = c.post(f"/api/agents/{bound_sid}/work")
        assert w.status_code == 200 and w.json().get("job_id")
        _wait_no_running(c, pid)
        task = next(t for t in c.get(f"/api/projects/{pid}/tasks").json()
                    if t["id"] == tid)
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
        # 暂停隔离：显式开两窗、委托经 tq 直接定窗（不起跑）——本测验证关窗中断
        # 语义，任务二须留在自己的待命窗
        pid = _l2_project(c, "中断关窗", paused=True)
        bb = c.app.state.projects[pid].bb
        tq = TaskQueue(bb)

        def _window_with_task(objective):
            sp = c.post(f"/api/projects/{pid}/agents",
                        json={"role": "_generalist", "armed": False})
            wsid = sp.json()["id"]
            wtid = tq.publish(pid, objective, task_type="generic",
                              created_by="human", target_session=wsid)
            return wsid, wtid

        sid, tid1 = _window_with_task("任务一")
        sid2, _ = _window_with_task("任务二")
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
