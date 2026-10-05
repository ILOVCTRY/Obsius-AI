"""C6 失败任务跨会话完整续跑测试（任务键快照 task-<tid>.resume.json）。

- 双写：三类暂停（软暂停/awaiting_human/步数耗尽）都会同步落任务键快照（不含 system）。
- 认领即复活：新会话认领 → messages 整体还原 + max_steps/next_step 断点续接 + 消费即删。
- objective 被改 → 删快照降级 C10 transcript 接手。
- E8 会话键恢复消费时同步删任务键（防 rewind 到旧暂停点）；done 清理；drop_scene。
"""
import json
import os

import pytest

from core.agent import AgentConfig, AgentSession
from core.agent.loop import persisted_snapshot_path, task_resume_path, task_transcript_path
from core.blackboard import Blackboard, TaskQueue
from core.llm.anthropic_compat import AnthropicCompatProvider
from core.runtime import ExecutionGateway, NativeBackend

from test_agent import ScriptedLLM


@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("续跑项目", "pentest", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    tq = TaskQueue(bb)
    yield bb, project, gw, tq, tmp_path
    bb.close()


def make_agent(env, llm, artifacts_dir, max_steps=10):
    bb, project, gw, tq, tmp_path = env
    packs = tmp_path / "packs"
    (packs / "experts").mkdir(parents=True, exist_ok=True)
    (packs / "experts" / "_generalist.yaml").write_text(
        'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
    return AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                        planner_llm=None, packs_root=packs, track="pentest",
                        capabilities=["web"], role="_generalist",
                        capability_prompt="## 能力清单\n- 测试",
                        config=AgentConfig(max_steps=max_steps),
                        artifacts_dir=artifacts_dir)


_STEP = {"tool_use": [ScriptedLLM.tool_call("t1", "bb_add_asset",
                                            {"type": "host", "value": "10.0.0.9"})]}
_AWAIT = {"tool_use": [ScriptedLLM.tool_call("t2", "fail_task",
                                             {"result_note": "等人工提供凭据",
                                              "blocked_reason": "awaiting_human"})]}
_DONE = {"tool_use": [ScriptedLLM.tool_call("t4", "complete_task",
                                            {"result_note": "复活后完成"})]}
_DONE2 = {"tool_use": [ScriptedLLM.tool_call("t4b", "complete_task",
                                             {"result_note": "复活后完成"})]}
_FINISH = {"tool_use": [ScriptedLLM.tool_call("t5", "finish", {"summary": "收尾"})]}


@pytest.fixture()
def paused_with_task_key(env):
    """请求软暂停 → 跑一步 → 控制点暂停（双写任务键快照，任务保持 claimed）。
    返回 (bb, project, gw, tq, tmp_path, tid, art, agent)。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    tid = tq.publish(project["id"], "渗透侦查任务", created_by="human")
    llm = ScriptedLLM([_STEP])
    agent = make_agent(env, llm, artifacts_dir=art, max_steps=10)
    agent.request_pause()
    agent.run_task("渗透侦查任务", task_id=tid)
    task = tq.get_task(tid)
    assert task["status"] == "claimed"  # 暂停保持 claimed
    tk = json.loads(task_resume_path(art, tid).read_text(encoding="utf-8"))
    assert "system" not in tk  # 跨角色安全：不存 system
    assert tk["messages"] and tk["next_step"] >= 1 and tk["max_steps"] == 10
    assert task_transcript_path(art, tid).exists()  # C10 transcript 照常
    return bb, project, gw, tq, tmp_path, tid, art, agent


@pytest.fixture()
def failed_with_task_key(env):
    """awaiting_human 挂起（双写任务键快照）→ 任务 failed。
    返回 (bb, project, gw, tq, tmp_path, tid, art, agent)。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    tid = tq.publish(project["id"], "渗透侦查任务", created_by="human")
    llm = ScriptedLLM([_STEP, _AWAIT])
    agent = make_agent(env, llm, artifacts_dir=art, max_steps=10)
    agent.run_task("渗透侦查任务", task_id=tid)
    task = tq.get_task(tid)
    assert task["status"] == "failed"  # awaiting_human fail
    # resumable 在 API 层派生（store 行无此列）；failed 事件带标记
    ev = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.failed"][-1]
    assert ev["payload"]["resumable"] is True
    return bb, project, gw, tq, tmp_path, tid, art, agent


def test_pause_dual_writes_task_key_snapshot_without_system(paused_with_task_key):
    bb, project, gw, tq, tmp_path, tid, art, agent = paused_with_task_key
    tk = task_resume_path(art, tid)
    tk_file = tk and tk.exists() and json.loads(tk.read_text(encoding="utf-8"))
    assert tk_file is not None and tk_file["task_id"] == tid
    assert "system" not in tk_file  # 跨角色安全：不存 system
    assert tk_file["messages"] and tk_file["next_step"] >= 1
    assert tk_file["max_steps"] == 10
    # 会话键快照照旧存在（E8 双写不互斥）
    assert task_transcript_path(art, tid).exists()


def test_claim_revives_across_sessions(failed_with_task_key):
    """新会话认领 failed 任务 → 认领即复活：messages 整体还原 + max_steps 断点
    续接 + 任务键快照消费即删 + 任务完成。"""
    bb, project, gw, tq, tmp_path, tid, art, agent = failed_with_task_key
    tq.reopen(tid, by="human")  # failed → open（现场保留）
    llm2 = ScriptedLLM([_DONE, _DONE2, _FINISH])
    agent2 = make_agent((bb, project, gw, tq, tmp_path), llm2,
                        artifacts_dir=art, max_steps=3)  # 预算更低
    tq.unassign_session(agent.session["id"])
    tq.bind_session(tid, agent2.session["id"], by="human-resume")
    agent2.run_task("渗透侦查任务", task_id=tid)
    # 复活：max_steps 被快照还原为 10（不是新会话的 3）
    assert agent2.dispatcher.max_steps == 10
    # 消费即删
    assert not task_resume_path(art, tid).exists()
    # 任务完成（复活后的 messages 里带着上一步 bb_add_asset 现场，完成收尾）
    assert tq.get_task(tid)["status"] == "done"
    # 复活的初始历史包含上会话的执行现场（bb_add_asset 工具调用）
    first_call_msgs = llm2.calls[0]["messages"]
    assert any("10.0.0.9" in json.dumps(m, ensure_ascii=False) for m in first_call_msgs)


def test_objective_changed_degrades_to_transcript(failed_with_task_key):
    """objective 被改 → 任务键快照删除，降级 C10 transcript 接手（现场不再还原）。"""
    bb, project, gw, tq, tmp_path, tid, art, agent = failed_with_task_key
    tq.update_task(tid, objective="改成完全不同的目标", by="human")
    assert task_resume_path(art, tid).exists()  # 改前还在
    tq.reopen(tid, by="human")  # failed → open 才能被新会话认领
    llm2 = ScriptedLLM([_DONE, _DONE2, _FINISH])
    agent2 = make_agent((bb, project, gw, tq, tmp_path), llm2,
                        artifacts_dir=art, max_steps=10)
    tq.unassign_session(agent.session["id"])
    tq.bind_session(tid, agent2.session["id"], by="human-resume")
    agent2.run_task("改成完全不同的目标", task_id=tid)
    assert not task_resume_path(art, tid).exists()  # 降级时删除
    # 降级走 C10 transcript 接手：末 60 条现场仍注入（消息里带旧现场）
    msgs = llm2.calls[0]["messages"]
    assert any("10.0.0.9" in json.dumps(m, ensure_ascii=False) for m in msgs)


def test_done_consumes_task_key_snapshot(failed_with_task_key):
    """done 生命周期：任务完成 → 任务键快照清理（transcript 保留供复盘）。"""
    bb, project, gw, tq, tmp_path, tid, art, agent = failed_with_task_key
    tq.reopen(tid, by="human")
    llm2 = ScriptedLLM([_DONE, _DONE2, _FINISH])
    agent2 = make_agent((bb, project, gw, tq, tmp_path), llm2,
                        artifacts_dir=art, max_steps=10)
    tq.unassign_session(agent.session["id"])
    tq.bind_session(tid, agent2.session["id"], by="human-resume")
    agent2.run_task("渗透侦查任务", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    assert not task_resume_path(art, tid).exists()  # done 清理
    assert task_transcript_path(art, tid).exists()  # transcript 保留供复盘


def test_e8_resume_consumes_task_key_too(paused_with_task_key):
    """E8 会话键恢复消费时同步删任务键（防「恢复后又 fail」rewind 到旧暂停点）。"""
    bb, project, gw, tq, tmp_path, tid, art, agent = paused_with_task_key
    llm2 = ScriptedLLM([_DONE, _DONE2, _FINISH])
    agent.llm = llm2  # 同会话恢复：换脚本供续跑步骤
    agent.session = dict(bb.get_session(agent.session["id"]))  # 刷新 meta 指针（API resume 同款）
    agent._resume_state = agent._load_persisted_snapshot()  # API resume 的恢复动作
    agent.paused = False
    agent.run_session()  # _resume_state 在内存 → E8 恢复路径
    assert tq.get_task(tid)["status"] == "done"
    assert not task_resume_path(art, tid).exists()  # C6 同步删任务键


def test_abort_resumable_via_task_key(paused_with_task_key):
    """C6 修复回归：暂停双写后删除会话键快照（模拟旧孤儿场景）→ 中断时
    任务键快照仍可证 resumable=True（旧实现只看会话键会误判不可续跑）。"""
    bb, project, gw, tq, tmp_path, tid, art, agent = paused_with_task_key
    os.unlink(persisted_snapshot_path(art, agent.session["id"]))  # 删会话键
    agent.request_abort()
    agent.run_session()  # 触发 _abort_current_task
    task = tq.get_task(tid)
    assert task["status"] == "failed"  # 人工中断
    ev = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.failed"][-1]
    assert ev["payload"]["resumable"] is True  # 任务键快照证真（旧实现误判 False）
    assert task_resume_path(art, tid).exists()  # E12 续跑凭证保留


def test_resume_cross_session_spawns_armed_window(tmp_path):
    """C6 端到端：原会话已不存在（无 claimed_by）→ POST resume 新建 armed 任务窗，
    worker 认领即复活（快照 messages 还原）并完成。"""
    from fastapi.testclient import TestClient

    from core.api.app import create_app

    llm = ScriptedLLM([_DONE, _DONE2, _FINISH])
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=llm, planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "p.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "跨会话续跑",
                                            "track": "pentest"}).json()["id"]
        tid = c.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "渗透侦查任务"}).json()["task_id"]
        # 模拟上一会话的暂停双写（任务键快照）后任务失败
        proj = app.state.projects[pid]
        rp = task_resume_path(proj.artifacts_dir, tid)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps({
            "task_id": tid, "objective": "渗透侦查任务",
            "messages": [{"role": "user", "content": "渗透侦查任务"},
                         {"role": "assistant", "content": [{"type": "text",
                          "text": "已开始侦察"}]}],
            "next_step": 2, "max_steps": 10, "reason": "pause",
        }, ensure_ascii=False), encoding="utf-8")
        tq = TaskQueue(proj.bb)
        sess = proj.bb.register_session(pid, "旧会话", role="_generalist")
        # v0.72：发布已建专属待命窗——先退绑再让测试窗认领（模拟上一进程的旧现场）
        bound = tq.get_task(tid)["target_session"]
        if bound:
            tq.unassign_session(bound)
        tq.claim(tid, sess["id"])
        tq.fail(tid, sess["id"], "服务重启中断")
        # 无原会话可复用（下方直接删行模拟跨进程丢失）——claimed_by 仍在但会话关闭
        proj.bb.close_session(sess["id"])

        r = c.post(f"/api/tasks/{tid}/resume")
        assert r.status_code == 200
        body = r.json()
        assert body["resume_mode"] == "snapshot"
        assert body["session_id"] != sess["id"]  # 新建 armed 任务窗
        # worker 异步认领即复活并完成
        import time
        status = None
        for _ in range(60):
            status = tq.get_task(tid)["status"]
            if status == "done":
                break
            time.sleep(0.1)
        assert status == "done"
        # 快照消费即删
        assert not task_resume_path(proj.artifacts_dir, tid).exists()


def test_resume_transcript_takes_over_origin_window(tmp_path):
    """resume-origin-window（2026-09-23）：transcript 模式 + 原窗存活 → 原窗就近
    接手（不再飘新窗）——reopen 后 worker 首轮认领，接手现场续跑完成。"""
    from fastapi.testclient import TestClient

    from core.api.app import create_app

    llm = ScriptedLLM([_DONE, _DONE2, _FINISH])
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=llm, planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "p.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "原窗接手",
                                            "track": "pentest"}).json()["id"]
        tid = c.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "渗透侦查任务"}).json()["task_id"]
        proj = app.state.projects[pid]
        tq = TaskQueue(proj.bb)
        # 会话中心化：发布不自动建窗——显式开原窗并绑定委托（armed 语义与本测无关）
        bound = proj.bb.register_session(pid, "原任务窗", role="_generalist")["id"]
        tq.bind_session(tid, bound)
        # 构造失败窗现场：原窗认领后失败，无任务键快照（=transcript 模式），窗未关
        tq.claim(tid, bound)
        tq.fail(tid, bound, "构造失败现场")
        n_sessions = len(proj.bb.list_sessions(pid))

        r = c.post(f"/api/tasks/{tid}/resume")
        assert r.status_code == 200
        body = r.json()
        assert body["resume_mode"] == "transcript"
        assert body["session_id"] == bound  # 原窗就近接手（不建新窗）
        import time
        status = None
        for _ in range(60):
            status = tq.get_task(tid)["status"]
            if status == "done":
                break
            time.sleep(0.1)
        assert status == "done"
        # 会话数不增：任务回原窗跑，没有新窗
        assert len(proj.bb.list_sessions(pid)) == n_sessions


def test_resume_transcript_closed_window_still_spawns_new(tmp_path):
    """transcript 模式 + 原窗已关 → 跨会话新 armed 任务窗（现状行为防回归）。"""
    from fastapi.testclient import TestClient

    from core.api.app import create_app

    llm = ScriptedLLM([_DONE, _DONE2, _FINISH])
    app = create_app(workspace_root=str(tmp_path / "ws"), tools_root=None,
                     executor_llm=llm, planner_llm=ScriptedLLM([]),
                     providers_config=str(tmp_path / "p.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "原窗已关续跑",
                                            "track": "pentest"}).json()["id"]
        tid = c.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "渗透侦查任务"}).json()["task_id"]
        proj = app.state.projects[pid]
        tq = TaskQueue(proj.bb)
        # 会话中心化：显式开原窗+绑定委托，再退绑+关原窗（模拟原窗已被人工
        # 关闭的旧现场）；无任务键快照=transcript
        bound = proj.bb.register_session(pid, "原任务窗", role="_generalist")["id"]
        tq.bind_session(tid, bound)
        tq.unassign_session(bound)
        tq.claim(tid, bound)
        tq.fail(tid, bound, "构造失败现场")
        proj.bb.close_session(bound)

        r = c.post(f"/api/tasks/{tid}/resume")
        assert r.status_code == 200
        body = r.json()
        assert body["resume_mode"] == "transcript"
        assert body["session_id"] != bound  # 原窗已关 → 新 armed 任务窗
        import time
        status = None
        for _ in range(60):
            status = tq.get_task(tid)["status"]
            if status == "done":
                break
            time.sleep(0.1)
        assert status == "done"
