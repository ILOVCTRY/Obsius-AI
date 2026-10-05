"""C10 任务上下文归任务所有（schema v9）测试。

- 每步 checkpoint：对话现场落 <snapshots>/task-<tid>.json，fail/崩溃/重启清扫不丢。
- 跨会话接手：fail→reopen→新会话认领，注入旧现场 + 「第 N 次尝试接手」履历提示。
- 降级：现场文件损坏/task_id 不符 → 空列表降级，履历提示仍注入。
- delete 清理：删任务连现场文件一起删；文件缺失不报错。
"""
import json
import sqlite3

import pytest

from core.agent import AgentConfig, AgentSession
from core.agent.loop import task_transcript_path
from core.blackboard import Blackboard, TaskQueue
from core.blackboard.tasks import render_attempts_lines
from core.llm.anthropic_compat import AnthropicCompatProvider
from core.runtime import ExecutionGateway, NativeBackend

from test_agent import ScriptedLLM


@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("上下文项目", "pentest", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    tq = TaskQueue(bb)
    yield bb, project, gw, tq, tmp_path
    bb.close()


def make_agent(env, llm, artifacts_dir=None):
    bb, project, gw, tq, tmp_path = env
    packs = tmp_path / "packs"
    (packs / "experts").mkdir(parents=True, exist_ok=True)
    (packs / "experts" / "_generalist.yaml").write_text(
        'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
    return AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                        planner_llm=None, packs_root=packs, track="pentest",
                        capabilities=["web"], role="_generalist",
                        capability_prompt="## 能力清单\n- 测试",
                        config=AgentConfig(max_steps=10),
                        artifacts_dir=artifacts_dir)


_STEP = {"tool_use": [ScriptedLLM.tool_call("t1", "bb_add_asset",
                                            {"type": "host", "value": "10.0.0.1"})]}
_FAIL = {"tool_use": [ScriptedLLM.tool_call("t2", "fail_task",
                                            {"result_note": "端口全关，无法继续"})]}
_FINISH = {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "收尾"})]}
_DONE = {"tool_use": [ScriptedLLM.tool_call("t4", "complete_task",
                                            {"result_note": "接手完成"})]}
_DONE2 = {"tool_use": [ScriptedLLM.tool_call("t4b", "complete_task",
                                             {"result_note": "接手完成"})]}


def test_no_task_direct_run_writes_nothing(env):
    """无任务直跑（run_task 不带 task_id）不落盘，也不留 task-.json 空名残file
    （端到端走查发现：guard 只挡了 path is None 没挡空 task_id）。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    llm = ScriptedLLM([_FINISH])
    make_agent(env, llm, artifacts_dir=art).run_task("纯对话直跑")
    snap = art.parent / "snapshots"
    assert not snap.exists() or not list(snap.glob("task-*.json"))


def test_attempt_recorded_and_per_step_checkpoint(env):
    """每步落盘任务现场；收尾追加 attempt 履历；temp 文件不残留。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    tid = tq.publish(project["id"], "侦查任务", created_by="human")
    llm = ScriptedLLM([_STEP, _FAIL, _FINISH])
    agent = make_agent(env, llm, artifacts_dir=art)
    agent.run_task("侦查", task_id=tid)
    task = tq.get_task(tid)
    assert task["status"] == "failed"
    # 履历：一次失败尝试，result_note/blocked_reason 落库
    attempts = task["context"]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "failed"
    assert attempts[0]["result_note"] == "端口全关，无法继续"
    assert attempts[0]["blocked_reason"] == "error"
    assert attempts[0]["session_id"] == agent.session["id"]
    assert task["context"]["transcript"] == f"task-{tid}.json"
    # 现场：每步 checkpoint（初始 + 2 步），文件为最新完整现场
    path = task_transcript_path(art, tid)
    st = json.loads(path.read_text(encoding="utf-8"))
    assert st["task_id"] == tid and len(st["messages"]) >= 3
    assert not list(path.parent.glob("*.tmp"))  # temp+replace 原子替换不残留


def test_no_artifacts_dir_degrades(env):
    """无 artifacts_dir（部分测试/直跑）→ 不落盘，任务照常收尾。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "无目录任务", created_by="human")
    llm = ScriptedLLM([_STEP, {"tool_use": [ScriptedLLM.tool_call(
        "t5", "complete_task", {"result_note": "完成"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t5b", "complete_task", {"result_note": "完成"})]}, _FINISH])
    agent = make_agent(env, llm)  # 不传 artifacts_dir
    agent.run_task("侦查", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    assert tq.get_task(tid)["context"]["attempts"][0]["outcome"] == "done"


def test_crash_keeps_transcript_and_sweep_records_attempt(env):
    """「崩溃」：第二步 LLM 抛异常 → worker 异常兜底立即 fail（不悬 claimed，
    防孤儿心跳续租把看板钉死在「执行中」），现场文件停在最近一步；真进程死亡
    （兜底没机会跑）的裸认领 claimed 仍由 fail_interrupted_claims 重启清扫收尾。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    tid = tq.publish(project["id"], "会崩的任务", created_by="human")

    class CrashAfterFirst(ScriptedLLM):
        def chat(self, *a, **kw):
            if len(self.calls) >= 1:
                raise RuntimeError("模拟进程崩溃")
            return super().chat(*a, **kw)

    llm = CrashAfterFirst([_STEP])
    agent = make_agent(env, llm, artifacts_dir=art)
    with pytest.raises(RuntimeError):
        agent.run_task("侦查", task_id=tid)
    task = tq.get_task(tid)
    assert task["status"] == "failed"
    # experience-sedimentation M1：worker 异常兜底（LLM 传输层炸）属 aborted 档——
    # 非方法论性失败，不进失败复盘与战役记忆
    assert task["blocked_reason"] == "aborted"
    assert "worker 异常退出" in task["context"]["attempts"][-1]["result_note"]
    path = task_transcript_path(art, tid)
    st = json.loads(path.read_text(encoding="utf-8"))
    assert any("10.0.0.1" in json.dumps(m, ensure_ascii=False) for m in st["messages"])

    # 重启清扫只收真孤儿（上面崩溃任务已被兜底收尾，不再 claimed）
    sess = bb.register_session(project["id"], "旧窗", role="_generalist")
    tid2 = tq.publish(project["id"], "孤儿认领", created_by="human")
    tq.claim(tid2, sess["id"])
    interrupted = tq.fail_interrupted_claims(project["id"])
    assert interrupted == [tid2]
    task2 = tq.get_task(tid2)
    assert task2["status"] == "failed"
    assert task2["blocked_reason"] == "awaiting_human"
    assert "后端重启" in task2["context"]["attempts"][-1]["result_note"]
    assert path.exists()  # 现场不随进程消失，新会话可接手


def test_handover_to_new_session(env):
    """fail→reopen→新会话认领：初始历史 = 旧现场 + 接手提示（含上次收尾与
    履历），objective 仍是末条锚点。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    tid = tq.publish(project["id"], "交接任务", created_by="human")
    llm_a = ScriptedLLM([_STEP, _FAIL, _FINISH])
    agent_a = make_agent(env, llm_a, artifacts_dir=art)
    agent_a.run_task("侦查内网", task_id=tid)

    tq.reopen(tid)  # 人工「放回继续」
    llm_b = ScriptedLLM([_DONE, _DONE2, _FINISH])
    agent_b = make_agent(env, llm_b, artifacts_dir=art)
    tq.unassign_session(agent_a.session["id"])
    tq.bind_session(tid, agent_b.session["id"], by="human-handover")
    agent_b.run_task("侦查内网", task_id=tid)

    msgs = llm_b.calls[0]["messages"]
    # 旧现场在前：A 会话的 run 步 tool_result（资产登记回执）可寻
    dump = json.dumps(msgs, ensure_ascii=False)
    assert "10.0.0.1" in dump
    texts = [m.get("content") if isinstance(m.get("content"), str) else "" for m in msgs]
    handover_idx = next(i for i, t in enumerate(texts) if "🔁 第 2 次尝试接手" in t)
    assert any("端口全关" in t for t in texts)
    # objective 末位锚点：B 的 objective（现场里 A 的同名 objective 之后最后一条）
    # 在接手提示之后、步首注入（预算提醒等）之前
    obj_idx = max(i for i, t in enumerate(texts) if t == "侦查内网")
    assert obj_idx > handover_idx
    assert tq.get_task(tid)["status"] == "done"
    assert len(tq.get_task(tid)["context"]["attempts"]) == 2


def test_load_transcript_degrades_on_corrupt_or_mismatch(env):
    """现场文件损坏 / task_id 不符 → 空降级；履历提示仍经 attempts 注入。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    tid = tq.publish(project["id"], "降级任务", created_by="human")
    sess = bb.register_session(project["id"], "旧窗", role="_generalist")
    tq.claim(tid, sess["id"])
    tq.fail(tid, sess["id"], "此前失败过")  # 造一条 attempt
    tq.reopen(tid)
    # 写一个 task_id 不符的现场文件
    path = task_transcript_path(art, tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"task_id": "task-other000000", "messages": [
        {"role": "user", "content": "别家任务的现场"}]}), encoding="utf-8")
    llm = ScriptedLLM([_DONE, _DONE2, _FINISH])
    agent = make_agent(env, llm, artifacts_dir=art)
    agent.run_task("重新来", task_id=tid)
    msgs = llm.calls[0]["messages"]
    dump = json.dumps(msgs, ensure_ascii=False)
    assert "别家任务的现场" not in dump          # 不符现场被拒载
    assert "1 次历史尝试" in dump and "此前失败过" in dump  # 履历照常注入
    assert "以上对话现场" not in dump            # 无现场 → 接手提示不含现场元说明

    # 损坏 JSON 同样降级
    tid2 = tq.publish(project["id"], "损坏任务", created_by="human")
    p2 = task_transcript_path(art, tid2)
    p2.write_text("{not json", encoding="utf-8")
    llm2 = ScriptedLLM([_DONE, _DONE2, _FINISH])
    make_agent(env, llm2, artifacts_dir=art).run_task("再来", task_id=tid2)
    assert "not json" not in json.dumps(llm2.calls[0]["messages"], ensure_ascii=False)


def test_render_attempts_lines():
    lines = render_attempts_lines([
        {"session_name": "ctf/web", "session_id": "sess-a", "outcome": "failed",
         "result_note": "端口全关\n第二行", "blocked_reason": "error",
         "ended_at": "2026-09-16T12:00:00+00:00"},
        {"session_name": None, "session_id": "sess-b", "outcome": "failed",
         "result_note": "后端重启，任务中断", "blocked_reason": "awaiting_human",
         "ended_at": "2026-09-16T13:00:00+00:00"},
    ])
    assert lines[0].startswith("- ① ctf/web failed（error）：端口全关")
    assert lines[1].startswith("- ② sess-b failed（awaiting_human）：后端重启")
    assert render_attempts_lines([]) == []


def test_delete_task_removes_transcript_file(tmp_path):
    """删任务连现场文件一起删；文件本就不存在也不报错。"""
    from fastapi.testclient import TestClient

    from core.api.app import create_app
    from test_orchestrator import ScriptedLLM as OrchLLM

    app = create_app(workspace_root=str(tmp_path / "workspaces"), tools_root=None,
                     executor_llm=OrchLLM([]), planner_llm=OrchLLM([]),
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "删除清理", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        bb = c.app.state.projects[pid].bb
        proj = c.app.state.projects[pid]
        tid = c.post(f"/api/projects/{pid}/tasks",
                     json={"objective": "要删的任务", "task_type": "generic"}
                     ).json()["task_id"]
        path = task_transcript_path(proj.artifacts_dir, tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        assert c.delete(f"/api/tasks/{tid}").status_code == 200
        assert not path.exists()
        # 文件本就不存在 → 照常删
        tid2 = c.post(f"/api/projects/{pid}/tasks",
                      json={"objective": "无现场任务", "task_type": "generic"}
                      ).json()["task_id"]
        assert c.delete(f"/api/tasks/{tid2}").status_code == 200
