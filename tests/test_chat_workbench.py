"""智能体工作台（K9）测试：store CRUD / 主控轮（todo+call_expert）/ MCP 桥 / API。

全程不触网：MCP http 连接经 monkeypatch 注入 transport；LLM 用脚本假件。
"""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.blackboard.store import Blackboard
from core.chat import store as chat_store
from core.chat.mcp_bridge import MCPBridge, _HttpConn
from core.chat.runtime import (
    ORCHESTRATOR_ID,
    ChatTurn,
    ChatTurn,
    _CTX_PLACEHOLDER_TAG,
    _CTX_SOFT_BUDGET,
    _CTX_SUMMARY_SYS,
    _CTX_SUMMARY_TAG,
    _MAX_TOOL_RESULT_CHARS,
    _MODEL_WINDOW_FALLBACK,
    _classify_error,
    _mask_old_tool_results,
    _msg_text,
    _recent_boundary,
    _sanitize_history,
)
from core.llm.provider import ContextOverflowError, LLMError, LLMResponse, ToolCall, Usage


# ---------- 假 LLM ----------

class FakeLLM:
    """脚本化 LLM：按队列吐响应；记录调用（system/tools 断言用）。
    脚本项为异常实例时直接抛出（测截断整轮重试等异常路径）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def chat(self, messages, **kwargs):
        with self.lock:
            self.calls.append({"messages": [dict(m) for m in messages],
                               "system": kwargs.get("system"),
                               "tools": kwargs.get("tools")})
            item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ThinkingLLM:
    """最小流式替身：验证 ChatTurn 把思考回调传给 provider。"""

    def chat(self, messages, **kwargs):
        callback = kwargs.get("on_thinking")
        if callback is not None:
            callback("先检查输入。")
            callback("再给出结论。")
        return _resp(text="完成")


def _resp(text="", tool_calls=None) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=tool_calls or [],
                       usage=Usage(input_tokens=1, output_tokens=1))


def _tc(id_, name, args) -> ToolCall:
    return ToolCall(id=id_, name=name, arguments=args)


# ---------- store ----------

def _mk_project(bb, pid: str) -> None:
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO projects(id, name, domain, track, config, created_at)"
            " VALUES(?,?,?,?,?,datetime('now'))", (pid, "测试", pid, pid, "{}"))


def test_chat_store_crud(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    t = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID, title="首测")
    assert t["id"].startswith("chat-") and t["status"] == "idle"
    assert t["todo"] == []
    # spawn 关系：父线程必须存在
    with pytest.raises(ValueError):
        chat_store.create_thread(bb, "p1", "web-solver", parent_thread_id="chat-nope")
    sub = chat_store.create_thread(bb, "p1", "web-solver",
                                   parent_thread_id=t["id"], spawned_task="测越权")
    assert sub["parent_thread_id"] == t["id"]
    listed = chat_store.list_threads(bb, "p1")
    assert {r["id"] for r in listed} == {t["id"], sub["id"]}
    assert chat_store.list_threads(bb, "p1", agent_id="web-solver")[0]["id"] == sub["id"]
    # 消息追加与状态/待办更新
    chat_store.append_message(bb, t["id"], "user", "你好")
    chat_store.append_message(bb, t["id"], "assistant", "好的",
                              tool_calls=[{"id": "t1", "name": "todo_write", "args": {}}])
    chat_store.append_message(bb, t["id"], "tool", "待办已更新", tool_use_id="t1")
    msgs = chat_store.list_messages(bb, t["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["name"] == "todo_write"
    updated = chat_store.update_thread(bb, t["id"], status="running",
                                       todo=[{"id": "todo-1", "title": "a", "status": "in_progress"}])
    assert updated["status"] == "running" and updated["todo"][0]["status"] == "in_progress"
    # 删除级联清消息
    assert chat_store.delete_thread(bb, t["id"])
    assert chat_store.list_messages(bb, t["id"]) == []
    assert chat_store.get_thread(bb, t["id"]) is None


def test_delete_thread_cascades_descendants(tmp_path):
    """回归：删有子线程的父线程曾报 FOREIGN KEY constraint failed → API 500
    （chat_messages.thread_id 外键无 CASCADE，parent_thread_id 的 DB 级联删
    子线程行时被子线程消息挡路）。修复：delete_thread 手动 BFS 收齐全部后代，
    先清消息再删线程行。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    orch = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID, title="父")
    sub = chat_store.create_thread(bb, "p1", "recon", title="子",
                                   parent_thread_id=orch["id"])
    grand = chat_store.create_thread(bb, "p1", "web-solver", title="孙",
                                     parent_thread_id=sub["id"])
    chat_store.append_message(bb, orch["id"], "user", "父消息")
    chat_store.append_message(bb, sub["id"], "user", "子消息")
    chat_store.append_message(bb, grand["id"], "user", "孙消息")
    assert chat_store.delete_thread(bb, orch["id"]) is True
    assert chat_store.list_threads(bb, "p1") == []
    for tid in (orch["id"], sub["id"], grand["id"]):
        assert chat_store.list_messages(bb, tid) == []
    # 无后代的单线程删除照旧；不存在的线程 False
    lone = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID, title="独")
    chat_store.append_message(bb, lone["id"], "user", "x")
    assert chat_store.delete_thread(bb, lone["id"]) is True
    assert chat_store.delete_thread(bb, "chat-nope") is False


def test_recover_running_threads(tmp_path):
    """回归（重启僵尸 running）：进程重启后执行轮次随旧进程消失，DB
    status=running 残留 → 工作台永久「执行中」（输入框禁用、停止 409）。
    项目打开时 recover_running_threads 归位 idle + 落中断消息（与手动停止
    同款观感，历史消息保留可续聊）；idle 线程不动；二次调用幂等空表。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    orch = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID, title="主控")
    sub = chat_store.create_thread(bb, "p1", "recon", title="子",
                                   parent_thread_id=orch["id"])
    chat_store.update_thread(bb, orch["id"], status="running")
    chat_store.update_thread(bb, sub["id"], status="running")
    chat_store.append_message(bb, orch["id"], "user", "重启前的消息")
    ok_t = chat_store.create_thread(bb, "p1", "recon", title="完好 idle")
    recovered = chat_store.recover_running_threads(bb)
    assert sorted(recovered) == sorted([orch["id"], sub["id"]])
    for tid in (orch["id"], sub["id"]):
        assert chat_store.get_thread(bb, tid)["status"] == "idle"
    assert chat_store.get_thread(bb, ok_t["id"])["status"] == "idle"
    msgs = chat_store.list_messages(bb, orch["id"])
    assert len(msgs) == 2 and msgs[-1]["role"] == "assistant"
    assert "进程重启" in msgs[-1]["content"] and "中断" in msgs[-1]["content"]
    # 幂等：无僵尸可清返回空表，不再重复落消息
    assert chat_store.recover_running_threads(bb) == []
    assert len(chat_store.list_messages(bb, orch["id"])) == 2
    # 活轮保护（2026-09-30 误报修复）：running 集合内的线程跳过不杀——
    # 执行中刷新页面（GET 项目）不会再把活轮误判僵尸落「进程重启」中断消息
    chat_store.update_thread(bb, ok_t["id"], status="running")
    assert chat_store.recover_running_threads(bb, {ok_t["id"]}) == []
    assert chat_store.get_thread(bb, ok_t["id"])["status"] == "running"
    assert chat_store.list_messages(bb, ok_t["id"]) == []
    # 集合外的僵尸照杀；集合内+外混合时只杀外的
    chat_store.update_thread(bb, sub["id"], status="running")
    assert chat_store.recover_running_threads(bb, {ok_t["id"]}) == [sub["id"]]
    assert chat_store.get_thread(bb, sub["id"])["status"] == "idle"
    assert chat_store.get_thread(bb, ok_t["id"])["status"] == "running"


def test_chat_thinking_stream_events(tmp_path):
    """工作台 ChatTurn 透传 on_thinking，并发布增量与终稿事件。

    2026-10-04：流式增量行（chat.thinking.delta / chat.delta）在轮正常收尾后被
    `prune_chat_thread_deltas` 清剪（终稿全文在 chat_messages，事件只承担实时
    可见）——故增量经**事件总线订阅**捕获实时发布，不读落库残留。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    published: list[dict] = []
    bb.bus.subscribe(published.append)
    turn = ChatTurn(bb=bb, llm=ThinkingLLM(), project_id="p1",
                    thread_id=thread["id"], packs_root="packs", track="ctf",
                    capabilities=["web"], mcp_bridge=None, expert_names=[])

    assert turn.run("分析") == "完成"
    deltas = [e for e in published if e["kind"] == "chat.thinking.delta"]
    assert deltas and deltas[-1]["payload"]["text"] == "先检查输入。再给出结论。"
    assert all(e["payload"]["thread_id"] == thread["id"] for e in deltas)
    # 终稿思考落库保留（审计），流式增量行已清剪（事件表不膨胀）
    events = bb.recent_events("p1", limit=200)
    finals = [e for e in events if e["kind"] == "chat.thinking"]
    assert finals and finals[-1]["payload"]["thinking"] == "先检查输入。再给出结论。"
    assert not [e for e in events if e["kind"] in
                ("chat.thinking.delta", "chat.delta")]
    # v31：思考正文随 assistant 消息落库（前端轮结束后持久渲染「已思考」折叠行）
    assistants = [m for m in chat_store.list_messages(bb, thread["id"])
                  if m["role"] == "assistant"]
    assert assistants and assistants[-1]["thinking"] == "先检查输入。再给出结论。"
    bb.close()


# ---------- 运行时：主控轮（todo → call_expert → 汇总） ----------

def test_chat_orchestrator_turn_todo_and_call_expert(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        _resp(tool_calls=[_tc("t1", "todo_write", {"items": [
            {"title": "确认目标", "status": "completed"},
            {"title": "委派越权测试", "status": "in_progress"}]})]),
        _resp(tool_calls=[_tc("t2", "call_expert",
                              {"expert": "web-solver", "task": "对 target.com 做水平越权测试"})]),
        # 子专家线程（第 3 个响应）：纯文本收尾摘要
        _resp(text="越权测试完成：发现未授权访问接口 /api/orders，证据已落黑板。"),
        # 主控汇总（第 4 个响应）
        _resp(text="已汇总：子专家发现 /api/orders 越权，详见线程。"),
    ])
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None,
                    expert_names=["web-solver", "_generalist", ORCHESTRATOR_ID])
    final = turn.run("测试 target.com 的水平越权")
    assert "已汇总" in final
    # 主控线程：user + assistant(todo_write) + tool + assistant(call_expert)
    #         + tool + assistant(汇总) = 6 行；todo 持久化
    orch_msgs = chat_store.list_messages(bb, thread["id"])
    assert [m["role"] for m in orch_msgs] == ["user", "assistant", "tool",
                                              "assistant", "tool", "assistant"]
    assert orch_msgs[-1]["content"].startswith("已汇总")
    threads = chat_store.list_threads(bb, "p1")
    sub = [t for t in threads if t["parent_thread_id"] == thread["id"]]
    assert len(sub) == 1 and sub[0]["agent_id"] == "web-solver"
    assert "水平越权" in sub[0]["spawned_task"]
    assert sub[0]["status"] == "idle"
    sub_msgs = chat_store.list_messages(bb, sub[0]["id"])
    assert sub_msgs[0]["role"] == "user" and "target.com" in sub_msgs[0]["content"]
    assert sub_msgs[-1]["role"] == "assistant" and "越权测试完成" in sub_msgs[-1]["content"]
    # todo 落线程
    orch = chat_store.get_thread(bb, thread["id"])
    assert [t["status"] for t in orch["todo"]] == ["completed", "in_progress"]
    # 主控工具面只含主控集（无 run_cmd/finish/call_expert 之外的实操工具）
    orch_call = llm.calls[0]
    names = {s["name"] for s in orch_call["tools"]}
    assert {"todo_write", "call_expert", "skill_open", "kb_search",
            "kb_open", "bb_query", "bb_delete_asset", "bb_merge_assets"} <= names
    assert "run_cmd" not in names and "finish" not in names and "publish_task" not in names
    # 主控 system 含 persona；专家清单在 call_expert 工具描述
    # （_generalist/主控自身不进委派清单）
    assert "问答蛙式调度者" in orch_call["system"]
    ce = next(s for s in orch_call["tools"] if s["name"] == "call_expert")
    assert "web-solver" in ce["description"] and "_generalist" not in ce["description"]
    # expert 字段带 enum（2026-10-01）：值域=可委派专家（排除主控/兜底），
    # 防模型把技能名当专家名传（recon-asset-enum 误用复盘）
    assert ce["input_schema"]["properties"]["expert"]["enum"] == ["web-solver"]
    # 子专家工具面：全量裁剪（无任务队列/收尾原语），且无 call_expert（spawn 深度=1）
    sub_call = llm.calls[2]
    sub_names = {s["name"] for s in sub_call["tools"]}
    assert "run_cmd" in sub_names and "bb_add_finding" in sub_names
    assert "call_expert" not in sub_names and "todo_write" not in sub_names
    assert "complete_task" not in sub_names  # 任务队列原语仍排除
    # 意图三件套开放给对话子专家（2026-10-01 intent-tools-chat）：链路图不再空
    assert {"declare_intent", "close_intent", "reopen_intent",
            "bb_delete_intent"} <= sub_names
    # 主控也拿到意图工具（目标级意图声明 + 汇总收尾）
    assert {"declare_intent", "close_intent"} <= names
    # 事件流：chat.message / chat.tool / chat.spawn / chat.todo 可见
    events = [e for e in bb.recent_events("p1") if e["kind"].startswith("chat.")]
    kinds = {e["kind"] for e in events}
    assert {"chat.message", "chat.tool", "chat.spawn", "chat.todo"} <= kinds


def test_chat_orchestrator_parallel_call_expert_dispatch(tmp_path):
    """同一批 call_expert 必须并行执行，且结果按调用 id 收集。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=FakeLLM([]), project_id="p1",
                    thread_id=thread["id"], packs_root="packs", track="ctf",
                    capabilities=["web"], mcp_bridge=None,
                    expert_names=["web-solver", "recon"])
    entered = threading.Barrier(2)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_dispatch(tc):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            entered.wait(timeout=2)
            return True, f"done:{tc.id}"
        finally:
            with lock:
                active -= 1

    turn._dispatch = fake_dispatch
    published: list[dict] = []
    bb.bus.subscribe(published.append)
    results = turn._parallel_expert_dispatch([
        _tc("e1", "call_expert", {"expert": "web-solver", "task": "任务一"}),
        _tc("e2", "call_expert", {"expert": "recon", "task": "任务二"}),
    ])
    assert max_active == 2
    assert results == {"e1": (True, "done:e1"), "e2": (True, "done:e2")}
    tool_events = [e for e in published if e["kind"] == "chat.tool"]
    assert len(tool_events) == 4
    by_call: dict[str, list[dict]] = {}
    for event in tool_events:
        payload = event["payload"]
        by_call.setdefault(payload["tool_call_id"], []).append(payload)
    assert set(by_call) == {"e1", "e2"}
    assert all({p["phase"] for p in payloads} == {"start", "done"}
               for payloads in by_call.values())
    assert all(p["parallel"] is True for p in (e["payload"] for e in tool_events))


def test_mixed_tool_batch_runs_experts_concurrently_after_serial_tool(tmp_path):
    """混合批次：普通工具先执行，多个 call_expert 再并发；结果顺序仍按原批次。"""
    bb = Blackboard(str(tmp_path / "bb.db")); _mk_project(bb, "p1")
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=FakeLLM([]), project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=["web-solver", "recon"])
    entered = threading.Barrier(2); active = 0; max_active = 0; calls: list[str] = []
    lock = threading.Lock()
    def fake_dispatch(tc):
        nonlocal active, max_active
        calls.append(tc.name)
        if tc.name == "call_expert":
            with lock:
                active += 1; max_active = max(max_active, active)
            try:
                entered.wait(timeout=2); return True, f"done:{tc.id}"
            finally:
                with lock: active -= 1
        return True, "todo done"
    turn._dispatch = fake_dispatch
    batch = [_tc("todo", "todo_write", {"items": []}),
             _tc("e1", "call_expert", {"expert": "web-solver", "task": "一"}),
             _tc("e2", "call_expert", {"expert": "recon", "task": "二"})]
    serial = []
    for tc in batch:
        if tc.name != "call_expert": serial.append((tc.id, turn._dispatch(tc)))
    results = turn._parallel_expert_dispatch(batch)
    assert calls[:1] == ["todo_write"]
    assert max_active == 2
    assert results == {"e1": (True, "done:e1"), "e2": (True, "done:e2")}


def test_call_expert_rejects_skill_name_with_enum_and_hint(tmp_path):
    """技能名当专家名（2026-10-01 复盘）：主控 ctx 同时含技能清单与专家名录，
    模型曾把 recon-asset-enum（技能）当专家传给 call_expert。三层防护断言：
    ① expert enum 只含专家 id；② 技能清单标题显式标注「技能名，不是专家」；
    ③ 未知专家报错与 enum 同口径（不含 _generalist）且提示「不是技能名」。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([_resp(text="收到。")])
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="pentest", capabilities=["web"],
                    mcp_bridge=None,
                    expert_names=["recon", "osint", "_generalist", ORCHESTRATOR_ID])
    turn.run("先做被动侦察复核")
    call = llm.calls[0]
    ce = next(s for s in call["tools"] if s["name"] == "call_expert")
    enum = ce["input_schema"]["properties"]["expert"]["enum"]
    assert enum == ["recon", "osint"] and "_generalist" not in enum
    assert "recon-asset-enum" not in enum
    # 技能清单在 system 里，且标题明确划界（防同屏误认）
    assert "recon-asset-enum" in call["system"]
    assert "不是专家" in call["system"]
    # 报错回退：技能名 → 未知专家，可用清单与 enum 同口径、不含 _generalist
    msg = turn._tool_call_expert({"expert": "recon-asset-enum", "task": "x"})
    assert "未知专家 'recon-asset-enum'" in msg
    assert "recon" in msg and "osint" in msg and "_generalist" not in msg
    assert "不是技能名" in msg


def test_chat_system_prompt_includes_binary_skills_for_research(tmp_path):
    """逆向项目 skill 注入（2026-09-30 修复）：caps_effective 对 research 轨默认补
    binary → chat system 提示含 binary 包逆向技能（binary-rev/file-triage 等）。
    此前无专家/无能力的逆向项目 capabilities 为空，只注入 research 轨技能。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([_resp(text="收到。")])
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="research", capabilities=["binary"],
                    mcp_bridge=None, expert_names=None)
    turn.run("分析这个样本")
    system = llm.calls[0]["system"]
    assert "binary-rev" in system and "file-triage" in system
    assert "blueprint-rebuild" in system  # research 轨技能也在（pack 并集）


def test_chat_max_steps_budget():
    """对话轮步数上限：主控/子专家均 200（2026-10-01 由 24/32 提升）。"""
    from core.chat import runtime
    assert runtime._ORCH_MAX_STEPS == 200
    assert runtime._EXPERT_MAX_STEPS == 200


def test_chat_last_step_forces_text_final(tmp_path, monkeypatch):
    """末步强制终稿（2026-10-01）：循环最后一步不传 tools，逼模型输出纯文本终稿，
    避免「步数耗尽但全程只调工具」→ 落「本轮未产出文本回复」。"""
    from core.chat import runtime
    monkeypatch.setattr(runtime, "_EXPERT_MAX_STEPS", 3)
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        _resp(tool_calls=[_tc("t1", "kb_search", {"q": "x"})]),
        _resp(tool_calls=[_tc("t2", "kb_search", {"q": "y"})]),
        _resp(text="收尾总结：完成探测"),
    ])
    thread = chat_store.create_thread(bb, "p1", "web-solver")
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=None)
    final = turn.run("探测目标")
    assert final == "收尾总结：完成探测"
    assert "本轮未产出文本回复" not in final
    assert llm.calls[0]["tools"] is not None     # 前步带工具
    assert llm.calls[-1]["tools"] is None        # 末步不传工具
    assert chat_store.list_messages(bb, thread["id"])[-1]["content"] == "收尾总结：完成探测"


def test_chat_truncated_tool_args_round_retry(tmp_path):
    """2026-10-01 事故：工具参数流截断（LLMError.truncated）→ chat 轮级整轮重试
    ≤2 次；重试成功则轮正常收尾（不落错误卡片、线程 status=idle）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        LLMError("工具参数流截断（stop_reason=tool_use，已收 2200 字符）: Extra data",
                 truncated=True),
        _resp(text="重试成功"),
    ])
    thread = chat_store.create_thread(bb, "p1", "web-solver")
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=None)
    final = turn.run("探测目标")
    assert final == "重试成功"
    assert len(llm.calls) == 2                       # 首次截断 + 重试一次
    t = chat_store.get_thread(bb, thread["id"])
    assert t["status"] == "idle" and not t.get("error")


def test_classify_error_stream_category():
    """2026-10-01：工具参数流截断归类为 stream（网关流异常），不再落 unknown。"""
    info = _classify_error(LLMError(
        "工具参数流截断（stop_reason=tool_use，已收 2200 字符）: Extra data",
        truncated=True))
    assert info["category"] == "stream"
    assert "网关流异常" in info["title"]


# ---------- 纯文本终稿截断自动续写（2026-10-01） ----------

def _trunc_turn(bb, llm, *, agent_id="web-solver"):
    thread = chat_store.create_thread(bb, "p1", agent_id)
    return ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=None), thread


def test_chat_continuation_on_truncated_final(tmp_path):
    """终稿 stop_reason=length → 不当作终稿收尾：把半截文本作为 assistant 前缀 +
    追 user 催续，继续循环拼接为完整终稿（修「话说一半就正常终止」）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    tail = LLMResponse(text="并已确认 /admin 未授权访问。", usage=Usage(1, 1),
                       stop_reason="end_turn")
    head = LLMResponse(text="正在分析目标：已完成资产测绘，",
                       usage=Usage(1, 1), stop_reason="length")
    llm = FakeLLM([head, tail])
    turn, thread = _trunc_turn(bb, llm)
    final = turn.run("分析目标")
    assert final == "正在分析目标：已完成资产测绘，并已确认 /admin 未授权访问。"
    assert len(llm.calls) == 2
    # 第二次调用：历史里应有 assistant 半截 + user 催续
    msgs = llm.calls[1]["messages"]
    assert any(m.get("role") == "assistant" and "已完成资产测绘" in _msg_text(m)
               for m in msgs)
    assert any(m.get("role") == "user" and "接着上一句" in _msg_text(m)
               for m in msgs)
    # 事件：先 continue 后正常收尾（无 giveup）
    kinds = [e["kind"] for e in bb.recent_events("p1")]
    assert "chat.truncated" in kinds
    cont = [e for e in bb.recent_events("p1") if e["kind"] == "chat.truncated"]
    assert cont[0]["payload"]["phase"] == "continue"
    # 线程正常收尾
    t = chat_store.get_thread(bb, thread["id"])
    assert t["status"] == "idle" and not t.get("error")


def test_chat_continuation_gives_up_after_max(tmp_path, monkeypatch):
    """连续截断超过 _CHAT_CONTINUE_MAX → 接受现有文本收尾并落 chat.truncated
    giveup 事件（不报错、不置 error）。"""
    from core.chat import runtime
    monkeypatch.setattr(runtime, "_CHAT_CONTINUE_MAX", 2)
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        LLMResponse(text="片段一 ", usage=Usage(1, 1), stop_reason="length"),
        LLMResponse(text="片段二 ", usage=Usage(1, 1), stop_reason="max_tokens"),
        LLMResponse(text="片段三", usage=Usage(1, 1), stop_reason="length"),
    ])
    turn, thread = _trunc_turn(bb, llm)
    final = turn.run("长任务")
    # 2 次续写 + 收口（第三次响应即达上限，接受其文本）
    assert "片段一" in final and "片段三" in final
    evs = [e for e in bb.recent_events("p1") if e["kind"] == "chat.truncated"]
    assert any(e["payload"]["phase"] == "giveup" for e in evs)
    t = chat_store.get_thread(bb, thread["id"])
    assert t["status"] == "idle" and not t.get("error")


def test_chat_no_continuation_on_normal_final(tmp_path):
    """正常终稿（stop_reason=end_turn / 空）不触发续写，单次调用即收尾。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([_resp(text="正常完成")])
    turn, thread = _trunc_turn(bb, llm)
    final = turn.run("普通问题")
    assert final == "正常完成"
    assert len(llm.calls) == 1
    assert not [e for e in bb.recent_events("p1") if e["kind"] == "chat.truncated"]


def test_is_truncated_final_helper():
    from core.chat.runtime import _is_truncated_final
    assert _is_truncated_final(LLMResponse(text="x", stop_reason="length"))
    assert _is_truncated_final(LLMResponse(text="x", stop_reason="max_tokens"))
    assert not _is_truncated_final(LLMResponse(text="x", stop_reason="end_turn"))
    assert not _is_truncated_final(LLMResponse(text="x", stop_reason=""))
    # 带 tool_calls 的响应当工具路径处理，不算终稿截断
    assert not _is_truncated_final(LLMResponse(
        text="x", stop_reason="length", tool_calls=[_tc("t", "bb_query", {})]))


def test_usage_breakdown_calibrated(tmp_path):
    """K10 上下文构成（Claude Code /context 式）：usage.breakdown 四块
    （系统提示/本轮注入/工具定义/会话消息）按字符估算比例、LLM 真值归一，
    总和恒等于窗口占用，随 usage 一起持久化。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        LLMResponse(text="", tool_calls=[_tc("t1", "bb_query", {"q": "assets"})],
                    usage=Usage(input_tokens=9000, output_tokens=50)),
        LLMResponse(text="黑板暂无资产记录。", tool_calls=[],
                    usage=Usage(input_tokens=9500, output_tokens=30)),
    ])
    thread = chat_store.create_thread(bb, "p1", "recon")
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=None)
    turn.run("查一下黑板资产")
    usage = chat_store.get_thread(bb, thread["id"])["usage"]
    assert usage["input"] == 9500
    bk = usage.get("breakdown")
    assert bk is not None and set(bk) == {"system", "refs", "tools", "messages"}
    assert bk["system"] > 0 and bk["tools"] > 0 and bk["messages"] > 0
    assert bk["refs"] == 0
    assert bk["system"] + bk["refs"] + bk["tools"] + bk["messages"] == usage["input"]


def test_msg_text_counts_tool_result_content(tmp_path):
    """伪影修复回归（2026-09-30）：tool_result 的 content 必须计入可估文本——
    此前漏计导致 est_msgs 低估、真值归一 scale 膨胀、缺口被成倍记进
    「工具定义」桶（面板 271K 假象）。content 支持 str 与内容块数组两种形态。"""
    assert _msg_text({"role": "user", "content": "纯文本"}) == "纯文本"
    assert _msg_text({"role": "assistant", "content": [
        {"type": "text", "text": "前缀"},
        {"type": "tool_use", "id": "t1", "name": "bb_query",
         "input": {"q": "assets"}},
    ]}) == "前缀" + json.dumps({"q": "assets"}, ensure_ascii=False)
    assert _msg_text({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": "IDA 伪码结果一万字"},
    ]}) == "IDA 伪码结果一万字"
    assert _msg_text({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": "块一"},
                     {"type": "text", "text": "块二"}]},
    ]}) == "块一块二"


def test_chat_expert_thread_cannot_call_expert_and_history_replays(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        _resp(tool_calls=[_tc("t1", "call_expert", {"expert": "web-solver", "task": "x"})]),
        _resp(text="收到，无法委派。"),
    ])
    thread = chat_store.create_thread(bb, "p1", "web-solver")
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=[])
    final = turn.run("帮我委派一个任务")
    assert "无法委派" in final
    # 历史重放：第二轮 messages 含 user/assistant(tool_use)/tool_result 三条
    msgs = llm.calls[1]["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert any(b.get("type") == "tool_use" for b in msgs[1]["content"])
    assert msgs[2]["role"] == "user"
    assert any(b.get("type") == "tool_result" for b in msgs[2]["content"])


# ---------- 专家工具面装配（回归） ----------

def test_expert_dispatcher_default_full_tools(tmp_path):
    """回归（8cb819d 首版 bug）：专家 yaml 未配 tools 字段时 expert_tool_names
    返回 None，契约语义=全量裁剪（与 _tool_specs 一致）；_build_dispatcher 曾把
    None or [] 当空白名单降级为无工具面——专家线程所有工具报
    「[错误] 工具 X 不可用：本线程未装配工具面」（recon.yaml 等无 tools 字段
    的专家全灭）。修复后缺省应装配全量工具（排除 _EXPERT_EXCLUDED 与
    todo_write/call_expert），显式白名单专家不受影响。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    orch = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID, title="父")
    sub = chat_store.create_thread(bb, "p1", "recon",
                                   parent_thread_id=orch["id"], spawned_task="侦察")
    turn = ChatTurn(bb=bb, llm=FakeLLM([]), project_id="p1", thread_id=sub["id"],
                    packs_root="packs", track="pentest", capabilities=["web"],
                    mcp_bridge=None, expert_names=["recon"])
    d = turn._build_dispatcher()
    assert d is not None and d.allowed_tools
    # 实操工具齐备（recon.yaml 没有 tools 字段 → 全量）
    assert {"run_cmd", "bb_query", "bb_add_finding"} <= set(d.allowed_tools)
    # 控制原语仍排除（任务队列/计划闸/派单/待办）
    assert not {"publish_task", "task_plan", "call_expert",
                "todo_write"} & set(d.allowed_tools)


def test_expert_dispatcher_explicit_whitelist(tmp_path):
    """显式 tools 白名单优先：配了 tools 的专家按白名单装配（不放大）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    orch = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID, title="父")
    sub = chat_store.create_thread(bb, "p1", "_generalist",
                                   parent_thread_id=orch["id"], spawned_task="通才")
    turn = ChatTurn(bb=bb, llm=FakeLLM([]), project_id="p1", thread_id=sub["id"],
                    packs_root="packs", track="ctf", capabilities=[],
                    mcp_bridge=None, expert_names=["_generalist"])
    d = turn._build_dispatcher()
    # _generalist.yaml tools: null → 同样走全量裁剪语义（显式 null ≈ 未配）
    assert d is not None and "run_cmd" in set(d.allowed_tools)


# ---------- 规则链注入（回归） ----------

def test_expert_thread_rule_preamble_injected(tmp_path):
    """回归（评级规则注入）：对话链 ChatTurn._system_prompt 曾完全不接规则
    链——主控委派的子专家登记漏洞时判级自由发挥，不引用 rating:<tag> 条款
    （任务链 loop.py stable_parts[0] 早已注入，两条链不对称）。修复：同构
    注入 build_rules_preamble（红线+owner 叠加+评级口径）放系统提示最前。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([_resp(text="发现已按评级口径登记。")])
    thread = chat_store.create_thread(bb, "p1", "web-solver")
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="pentest", capabilities=["web"],
                    mcp_bridge=None, expert_names=["web-solver"],
                    owner_tags=["edusrc"],
                    rule_profiles={"owners": ["edusrc"],
                                   "rating": ["edu-rating"]})
    turn.run("登记 appsettings.json 泄露漏洞")
    system = llm.calls[0]["system"]
    # 规则链在最前（角色段之前）
    assert "场景规则与红线" in system
    assert system.index("场景规则与红线") < system.index("# 角色")
    # 评级硬指令 + 生效口径（图二理想态「rating:edu-rating 高危#N …」的来源）
    assert "评级硬指令" in system and "rating_basis" in system
    assert "rating:edu-rating" in system
    # owner 叠加段
    assert "owner:edusrc" in system
    # 未配 owner/评级时静默降级：无评级硬指令（track 红线仍注入）
    llm2 = FakeLLM([_resp(text="ok")])
    lone = chat_store.create_thread(bb, "p1", "web-solver")
    turn2 = ChatTurn(bb=bb, llm=llm2, project_id="p1", thread_id=lone["id"],
                     packs_root="packs", track="pentest", capabilities=["web"],
                     mcp_bridge=None, expert_names=["web-solver"])
    turn2.run("x")
    assert "评级硬指令" not in llm2.calls[0]["system"]
    assert "场景规则与红线" in llm2.calls[0]["system"]


def test_rule_preamble_propagates_to_spawned_expert(tmp_path):
    """透传链：主控 call_expert spawn 的子线程必须继承 owner_tags/
    rule_profiles（_tool_call_expert 构造 sub_turn 时透传），否则子专家
    系统提示缺评级口径——图一 bug 的完整链路。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        _resp(tool_calls=[_tc("t1", "call_expert",
                              {"expert": "web-solver",
                               "task": "对泄露的 appsettings.json 登记漏洞"})]),
        _resp(text="专家摘要：漏洞已按评级口径登记。"),
        _resp(text="汇总完成。"),
    ])
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="pentest", capabilities=["web"],
                    mcp_bridge=None,
                    expert_names=["web-solver", ORCHESTRATOR_ID],
                    owner_tags=["edusrc"],
                    rule_profiles={"owners": ["edusrc"],
                                   "rating": ["edu-rating"]})
    turn.run("登记泄露漏洞")
    # 主控 system 含规则链
    assert "评级硬指令" in llm.calls[0]["system"]
    # 子专家（spawn 线程）system 同样含评级口径与 owner 叠加
    assert "评级硬指令" in llm.calls[1]["system"]
    assert "rating:edu-rating" in llm.calls[1]["system"]
    assert "owner:edusrc" in llm.calls[1]["system"]


# ---------- 工作区/重装备装配（回归） ----------

def test_expert_thread_workspace_tools(tmp_path):
    """回归（工作区装配）：对话链 ChatTurn 构造 ToolDispatcher 漏传
    artifacts_dir——read_file/search_files/bb_add_artifact 全拒「未装配工作区」，
    run_cmd workspace=None cwd 漂移（nmap -oN 输出写丢，截图 bug）。修复：与
    任务链同款传 artifacts_dir（workspace=其父目录）+ browser/decompiler 构造
    后挂载 + role_skills 对齐专家 yaml skills 字段。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    ws = tmp_path / "proj"
    (ws / "scratch" / "gygll113").mkdir(parents=True)
    (ws / "scratch" / "gygll113" / "ports_fast.txt").write_text(
        "8080  open  http", encoding="utf-8")
    llm = FakeLLM([_resp(text="ok")])
    thread = chat_store.create_thread(bb, "p1", "web-solver")
    decomp = object()
    builds: list[dict] = []
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=["web-solver"],
                    artifacts_dir=ws / "artifacts",
                    browser_pool=object(),
                    decompiler_factory=lambda **kw: (builds.append(kw), decomp)[1])
    d = turn._build_dispatcher()
    assert d is not None
    # read_file 相对 scratch 闭环（截图场景：run_cmd 产物读得回）
    out = d._tool_read_file("gygll113/ports_fast.txt")
    assert "[错误]" not in out and "8080" in out
    # 工作区与重装备挂载
    assert Path(d.artifacts_dir) == ws / "artifacts"
    assert d.browser is not None
    assert d.decompiler is decomp
    # 工厂收到会话标识（runner 审计落点）
    assert builds and "chat-" in builds[0]["session_id"]
    # role_skills 自专家 yaml（web-solver.yaml skills 字段）
    assert "web-injection" in (d.role_skills or [])


def test_workspace_params_propagate_to_spawned_expert(tmp_path):
    """透传链：主控 call_expert spawn 的子线程必须继承 artifacts_dir/
    browser_pool/decompiler_factory，否则子专家工作区工具全灭（同规则链
    透传缺口模式）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    ws = tmp_path / "proj"
    (ws / "scratch" / "gygll113").mkdir(parents=True)
    (ws / "scratch" / "gygll113" / "ports_fast.txt").write_text(
        "8080  open  http", encoding="utf-8")
    llm = FakeLLM([
        _resp(tool_calls=[_tc("t1", "call_expert",
                              {"expert": "web-solver",
                               "task": "读取 nmap 扫描结果文件"})]),
        _resp(tool_calls=[_tc("t2", "read_file",
                              {"path": "gygll113/ports_fast.txt"})]),
        _resp(text="专家摘要：读到扫描结果。"),
        _resp(text="汇总完成。"),
    ])
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None,
                    expert_names=["web-solver", ORCHESTRATOR_ID],
                    artifacts_dir=ws / "artifacts",
                    browser_pool=object(),
                    decompiler_factory=lambda **kw: object())
    turn.run("让专家读扫描结果")
    sub = next(t for t in chat_store.list_threads(bb, "p1")
               if t["parent_thread_id"] == thread["id"])
    sub_msgs = chat_store.list_messages(bb, sub["id"])
    # 子线程 read_file 成功：tool 消息带文件内容，无「未装配工作区」
    tool_msgs = [m for m in sub_msgs if m["role"] == "tool"]
    assert tool_msgs and "8080" in tool_msgs[0]["content"]
    assert "未装配工作区" not in tool_msgs[0]["content"]


# ---------- 中止（停止按钮） ----------

def test_chat_turn_abort_between_steps(tmp_path):
    """步间中止：LLM 返回工具调用后置中止事件 → 下一步退出并落「已停止」。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    abort = threading.Event()

    class StopAfterFirst(FakeLLM):
        def chat(self, messages, **kwargs):
            resp = super().chat(messages, **kwargs)
            if len(self.calls) >= 1:
                abort.set()  # 第一次响应（todo_write）落库后中止
            return resp

    llm = StopAfterFirst([
        _resp(tool_calls=[_tc("t1", "todo_write", {"items": [
            {"title": "a", "status": "in_progress"}]})]),
        _resp(text="这步永远不该被看到"),
    ])
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=["web-solver"],
                    abort_event=abort)
    final = turn.run("随便做点什么")
    assert "已按人类要求停止" in final
    assert chat_store.get_thread(bb, thread["id"])["status"] == "idle"
    msgs = chat_store.list_messages(bb, thread["id"])
    assert msgs[-1]["role"] == "assistant" and "停止" in msgs[-1]["content"]
    # 中止后工具分发也拒绝
    _ok, msg = turn._dispatch(_tc("t9", "bb_query", {}))
    assert not _ok and "[错误] 已停止" in msg


def test_chat_stop_endpoint(chat_client, tmp_path):
    """stop 端点：未在跑按幂等成功 204（前端状态刷新竞态）；执行中 204 且轮次以
    「已停止」收尾、状态归位。"""
    r = chat_client.post("/api/projects", json={
        "name": "停止测试", "track": "ctf", "capabilities": ["web"]})
    pid = r.json()["id"]
    tid = chat_client.post(f"/api/projects/{pid}/chat/threads",
                           json={"agent_id": ORCHESTRATOR_ID}).json()["id"]
    assert chat_client.post(f"/api/chat/threads/{tid}/stop").status_code == 204
    # 慢 LLM：让执行窗口足够长，命中运行中的 stop
    import core.api.app as app_mod
    from core.llm.provider import LLMResponse as _R

    class Slow:
        def __init__(self):
            self.aborted_seen = False
            self.started = threading.Event()

        def chat(self, messages, **kwargs):
            self.started.set()  # 已进入 LLM 调用：消除「步间中止」竞态
            for _ in range(50):
                if kwargs.get("should_cancel") and kwargs["should_cancel"]():
                    self.aborted_seen = True
                    return _R(text="", tool_calls=[],
                              usage=Usage(input_tokens=1, output_tokens=1))
                time.sleep(0.1)
            return _R(text="慢回复", tool_calls=[],
                      usage=Usage(input_tokens=1, output_tokens=1))

    slow = Slow()
    app2 = create_app(workspace_root=str(tmp_path / "ws2"), tools_root=None,
                      executor_llm=slow, planner_llm=None,
                      providers_config=str(tmp_path / "providers.json"))
    with TestClient(app2) as c2:
        r2 = c2.post("/api/projects", json={
            "name": "停止慢速", "track": "ctf", "capabilities": ["web"]})
        pid2 = r2.json()["id"]
        tid2 = c2.post(f"/api/projects/{pid2}/chat/threads",
                       json={"agent_id": ORCHESTRATOR_ID}).json()["id"]
        assert c2.post(f"/api/chat/threads/{tid2}/messages",
                       json={"text": "慢慢做"}).status_code == 202
        # 先确保已进入 LLM 调用再停：否则 stop 可能落在步间（_loop 步首探中止），
        # 轮次直接 _persist_stopped 而 Slow.chat 从未被调 → aborted_seen 永假（竞态）
        assert slow.started.wait(5)
        assert c2.get(f"/api/chat/threads/{tid2}").json()["thread"]["status"] == "running"
        assert c2.post(f"/api/chat/threads/{tid2}/stop").status_code == 204
        deadline = time.time() + 10
        msgs = []
        while time.time() < deadline:
            d = c2.get(f"/api/chat/threads/{tid2}").json()
            if d["thread"]["status"] == "idle" and len(d["messages"]) >= 2:
                msgs = d["messages"]
                break
            time.sleep(0.1)
        assert msgs and "已按人类要求停止" in msgs[-1]["content"]
        assert slow.aborted_seen  # should_cancel 逐帧探测被命中


# ---------- MCP 桥 ----------

@pytest.fixture()
def mcp_config(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": [
        {"name": "ida", "url": "http://127.0.0.1:13337/mcp",
         "transport": "streamable-http", "enabled": True, "domains": ["reverse"]},
        {"name": "fofa", "url": "", "transport": "stdio",
         "command": "no-such-binary-xyz", "args": [], "enabled": True,
         "domains": ["pentest"]},
        {"name": "disabled", "url": "", "transport": "stdio",
         "command": "x", "args": [], "enabled": False, "domains": ["pentest"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    return cfg


def test_mcp_bridge_domains_and_stdio_offline(mcp_config):
    bridge = MCPBridge(mcp_config, domains=["pentest"])
    names = [s["name"] for s in bridge.status()]
    assert names == ["fofa"]  # reverse 域被过滤 + disabled 不启用
    assert names and bridge.status()[0]["online"] is False  # 假二进制 → 离线
    assert bridge.tool_specs() == []  # 离线 server 无工具
    assert "未启用" in bridge.call("ida", "any", {})


def test_mcp_bridge_http_tools_and_call(mcp_config, monkeypatch):
    def fake_post(self, method, params, *, notification, timeout):
        if method == "initialize":
            return 200, {"result": {"serverInfo": {"name": "ida"}}}, {
                "Mcp-Session-Id": "s-1"}
        if method == "tools/list":
            return 200, {"result": {"tools": [
                {"name": "decompile", "description": "反编译函数",
                 "inputSchema": {"type": "object", "properties": {"addr": {}}}}]}}, {}
        if method == "tools/call":
            return 200, {"result": {"content": [
                {"type": "text", "text": "0x401000: push rbp"}]}}, {}
        return 200, {"result": {}}, {}

    monkeypatch.setattr(_HttpConn, "_post", fake_post)
    bridge = MCPBridge(mcp_config, domains=["reverse"])
    specs = bridge.tool_specs()
    assert [s["name"] for s in specs] == ["mcp__ida__decompile"]
    assert "反编译" in specs[0]["description"]
    out = bridge.call("ida", "decompile", {"addr": "0x401000"})
    assert "push rbp" in out
    status = bridge.status()[0]
    assert status["online"] and status["tools"][0]["name"] == "decompile"


# ---------- MCP 会话隔离 + 并发上限（2026-10-01） ----------

@pytest.fixture()
def pw_config(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": [
        {"name": "playwright", "url": "", "transport": "stdio",
         "command": "no-such-bin-pw", "args": [], "enabled": True,
         "domains": ["pentest"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    return cfg


def _capture_session_runtimes(bridge, monkeypatch):
    """拦截会话级 runtime 构造，记录每个 (session -> env) 而不真起进程。"""
    made = {}

    class FakeRuntime:
        def __init__(self, name, cfg, env_extra=None):
            self.name = name
            self.transport = "stdio"
            self.domains = cfg.get("domains") or []
            self.env = dict(env_extra or {})
            made[self.env.get("PW_SESSION_ID")] = self

        def list_tools(self, *, refresh=False):
            return [{"name": "browser_navigate", "description": "导航",
                     "input_schema": {"type": "object", "properties": {"url": {}}}}]

        def call(self, tool, args):
            return f"ok:{tool}:{self.env.get('PW_SESSION_ID')}"

        def is_alive(self):
            return True

    monkeypatch.setattr("core.chat.mcp_bridge.MCPServerRuntime", FakeRuntime)
    return made


def test_mcp_session_scoped_server_gets_session_env(pw_config, monkeypatch):
    """会话级 server：PW_SESSION_ID 按调用方 session 注入，同会话复用、异会话隔离。"""
    made = _capture_session_runtimes(None, monkeypatch)
    bridge = MCPBridge(pw_config, domains=["pentest"])
    a1 = bridge.call("playwright", "browser_navigate", {}, session_id="thread-A")
    a2 = bridge.call("playwright", "browser_navigate", {}, session_id="thread-A")
    b1 = bridge.call("playwright", "browser_navigate", {}, session_id="thread-B")
    assert "thread-A" in a1 and "thread-A" in a2 and "thread-B" in b1
    assert set(made) == {"thread-A", "thread-B"}      # 同会话只建一个
    assert len(bridge._session_servers) == 2


def test_mcp_session_scoped_concurrency_limit(pw_config, monkeypatch):
    """并发浏览器会话超上限 → 结构化错误引导，不再新建进程。"""
    _capture_session_runtimes(None, monkeypatch)
    monkeypatch.setattr(MCPBridge, "MAX_BROWSER_SESSIONS", 2)
    bridge = MCPBridge(pw_config, domains=["pentest"])
    assert "thread-1" in bridge.call("playwright", "browser_navigate", {},
                                     session_id="thread-1")
    assert "thread-2" in bridge.call("playwright", "browser_navigate", {},
                                     session_id="thread-2")
    out = bridge.call("playwright", "browser_navigate", {}, session_id="thread-3")
    assert out.startswith("[错误]") and "上限" in out and "空闲回收" in out
    assert len(bridge._session_servers) == 2


def test_mcp_session_scoped_reaps_dead_sessions(pw_config, monkeypatch):
    """已死会话占用的配额会被回收，允许新会话顶替（空闲回收后不永久卡限）。"""
    made = _capture_session_runtimes(None, monkeypatch)
    monkeypatch.setattr(MCPBridge, "MAX_BROWSER_SESSIONS", 1)
    bridge = MCPBridge(pw_config, domains=["pentest"])
    assert "thread-1" in bridge.call("playwright", "browser_navigate", {},
                                     session_id="thread-1")
    made["thread-1"].is_alive = lambda: False     # 模拟空闲回收/进程退出
    out = bridge.call("playwright", "browser_navigate", {}, session_id="thread-2")
    assert "thread-2" in out
    assert set(bridge._session_servers) == {("playwright", "thread-2")}


def test_mcp_status_marks_session_scoped_server(pw_config):
    """无法探针时仍标记会话级 server，且不报告为在线。"""
    bridge = MCPBridge(pw_config, domains=["pentest"])
    st = bridge.status()[0]
    assert st["name"] == "playwright" and st["session_scoped"] is True
    assert st["online"] is False


def test_mcp_status_discovers_session_scoped_tools(pw_config, monkeypatch):
    """状态探针不启动真实浏览器，但应返回会话级 server 的工具清单。"""
    _capture_session_runtimes(None, monkeypatch)
    bridge = MCPBridge(pw_config, domains=["pentest"])
    st = bridge.status()[0]
    assert st["session_scoped"] is True
    assert st["online"] is True
    assert st["tools"][0]["name"] == "browser_navigate"


# ---------- API ----------

@pytest.fixture()
def chat_client(tmp_path):
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None,
                     executor_llm=FakeLLM([_resp(text="收到。")]),
                     planner_llm=None,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        yield c


def test_chat_agents_and_thread_api(chat_client):
    r = chat_client.post("/api/projects", json={
        "name": "工作台", "track": "ctf", "capabilities": ["binary"]})
    pid = r.json()["id"]
    agents = chat_client.get(f"/api/chat/agents?pid={pid}").json()
    ids = [a["id"] for a in agents]
    assert ids[0] == ORCHESTRATOR_ID and agents[0]["kind"] == "orchestrator"
    assert "web-solver" in ids and "_generalist" not in ids
    # 建线程 / 列表 / 详情
    t = chat_client.post(f"/api/projects/{pid}/chat/threads",
                         json={"agent_id": ORCHESTRATOR_ID}).json()
    assert t["agent_id"] == ORCHESTRATOR_ID and t["status"] == "idle"
    assert chat_client.post(f"/api/projects/{pid}/chat/threads",
                            json={"agent_id": "ghost"}).status_code == 422
    listed = chat_client.get(f"/api/projects/{pid}/chat/threads",
                             params={"agent_id": ORCHESTRATOR_ID}).json()
    assert [x["id"] for x in listed] == [t["id"]]
    detail = chat_client.get(f"/api/chat/threads/{t['id']}").json()
    assert detail["thread"]["id"] == t["id"] and detail["messages"] == []
    # 发消息 → 202 → 轮次后台跑完 → assistant 回复落库（假 LLM 纯文本单步）
    r = chat_client.post(f"/api/chat/threads/{t['id']}/messages",
                         json={"text": "你好"})
    assert r.status_code == 202
    deadline = time.time() + 10
    msgs = []
    while time.time() < deadline:
        msgs = chat_client.get(f"/api/chat/threads/{t['id']}").json()["messages"]
        if len(msgs) >= 2:
            break
        time.sleep(0.2)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "收到。"
    th = chat_client.get(f"/api/chat/threads/{t['id']}").json()["thread"]
    assert th["title"] == "你好" and th["status"] == "idle"
    # 空消息 422；删除 204 后 404
    assert chat_client.post(f"/api/chat/threads/{t['id']}/messages",
                            json={"text": "  "}).status_code == 422
    assert chat_client.delete(f"/api/chat/threads/{t['id']}").status_code == 204
    assert chat_client.get(f"/api/chat/threads/{t['id']}").status_code == 404


def test_chat_mcp_endpoint(chat_client, tmp_path, monkeypatch):
    import core.api.app as app_mod
    cfg = tmp_path / "mcp.json"
    # 先隔离配置路径（测试机真实 config/mcp.json 不得渗入；桥按项目缓存，两项目两态）
    monkeypatch.setattr(app_mod, "MCP_CONFIG_PATH", cfg)
    cfg.write_text("{}", encoding="utf-8")
    p1 = chat_client.post("/api/projects", json={
        "name": "mcp 空配置", "track": "pentest", "capabilities": ["web"]}).json()["id"]
    out = chat_client.get(f"/api/chat/mcp?pid={p1}").json()
    assert out == {"servers": []}
    # 写入配置后用新项目（避开已缓存的桥）：stdio 假 server 离线可见（best-effort 不 500）
    cfg.write_text(json.dumps({"servers": [
        {"name": "fofa", "url": "", "transport": "stdio",
         "command": "no-such-binary-xyz", "args": [], "enabled": True,
         "domains": ["pentest"]}]}, ensure_ascii=False), encoding="utf-8")
    p2 = chat_client.post("/api/projects", json={
        "name": "mcp 有配置", "track": "pentest", "capabilities": ["web"]}).json()["id"]
    out = chat_client.get(f"/api/chat/mcp?pid={p2}").json()
    assert len(out["servers"]) == 1
    assert out["servers"][0]["name"] == "fofa" and out["servers"][0]["online"] is False


def test_chat_mcp_reverse_domain_for_research(chat_client, tmp_path, monkeypatch):
    """逆向项目（research 轨）加载配置的 reverse 域 MCP server（2026-09-30 修复）。

    此前 _chat_bridge 用盘上原始 capabilities（M3 起创建恒空）→ domains 只有
    {research}，与 config 里 ida server 的 reverse 域不相交 → 逆向项目永远加载
    不了配置的 MCP。修复后 caps_effective 按轨默认补 binary + 旧域反向匹配
    （LEGACY_DOMAIN_MAP: reverse→(research,[binary])）放行 reverse 域。"""
    import core.api.app as app_mod
    cfg = tmp_path / "mcp-rev.json"
    monkeypatch.setattr(app_mod, "MCP_CONFIG_PATH", cfg)
    cfg.write_text(json.dumps({"servers": [
        {"name": "ida", "url": "", "transport": "stdio",
         "command": "no-such-binary-xyz", "args": [], "enabled": True,
         "domains": ["reverse"]},
        {"name": "fofa", "url": "", "transport": "stdio",
         "command": "no-such-binary-xyz", "args": [], "enabled": True,
         "domains": ["pentest"]}]}, ensure_ascii=False), encoding="utf-8")
    # 蛙池AI逆向 场景：research 轨、无专家、无显式能力 → 默认 binary → reverse 域放行
    p1 = chat_client.post("/api/projects", json={
        "name": "逆向无专家", "track": "research"}).json()["id"]
    out1 = chat_client.get(f"/api/chat/mcp?pid={p1}").json()
    assert [s["name"] for s in out1["servers"]] == ["ida"]
    # 显式声明非 binary 能力（crypto）的 research 项目：不默认放行 reverse
    p2 = chat_client.post("/api/projects", json={
        "name": "研究非binary", "track": "research",
        "capabilities": ["crypto"]}).json()["id"]
    out2 = chat_client.get(f"/api/chat/mcp?pid={p2}").json()
    assert [s["name"] for s in out2["servers"]] == []
    # pentest 项目：fofa(pentest) 加载、ida(reverse) 不加载（行为不变）
    p3 = chat_client.post("/api/projects", json={
        "name": "渗透", "track": "pentest", "capabilities": ["web"]}).json()["id"]
    out3 = chat_client.get(f"/api/chat/mcp?pid={p3}").json()
    assert [s["name"] for s in out3["servers"]] == ["fofa"]


# ---------- 上下文治理（2026-09-30 压缩上下文方案） ----------

def _gov_messages(big: int = 20000) -> list[dict]:
    """构一段「旧区含单条巨物 tool_result + 12 个 assistant 步」的历史：
    _recent_boundary(keep=12) 落在下标 1（首个 assistant），故下标 0 的巨物
    落在可压缩旧区。"""
    msgs = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t0", "content": "A" * big}]}]
    for i in range(12):
        msgs.append({"role": "assistant", "content": f"a{i}"})
        msgs.append({"role": "user", "content": f"u{i}"})
    return msgs


def _gov_turn(bb, llm, tmp_path) -> ChatTurn:
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    return ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=[ORCHESTRATOR_ID],
                    artifacts_dir=tmp_path / "artifacts")


def _small_budget(llm, window: int = 300, soft: int = 1000) -> None:
    """把 provider 窗口/软上限调小，使极小历史即可触发压缩（等价 provider
    配置下发路径），避免构造数百万字符的真巨物。"""
    llm.context_tokens = window
    llm.ctx_soft_budget = soft


def test_clip_tool_result_truncates_and_spills(tmp_path):
    """①单条工具结果截断（2026-09-30）：巨物入历史前截到 _MAX_TOOL_RESULT_CHARS
    + 尾注；完整原文落 artifacts/chat-results/（事件流记路径）；未超限原样返回
    且不落盘。截断必须落在「写 DB」这一步（DB 是下轮回放源）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    turn = _gov_turn(bb, FakeLLM([]), tmp_path)
    # 未超限：原样、无落盘
    small, p = turn._clip_tool_result("短结果", _tc("t1", "run_cmd", {}))
    assert small == "短结果" and p == ""
    # 超限：截断 + 落盘
    big = "X" * (_MAX_TOOL_RESULT_CHARS + 5000)
    clipped, path = turn._clip_tool_result(big, _tc("t2", "run_cmd", {}))
    assert len(clipped) < len(big)
    assert clipped.startswith("X" * 100)
    assert "已截断" in clipped and str(len(big)) in clipped
    assert path and Path(path).is_file()
    assert Path(path).read_text(encoding="utf-8") == big


def test_prepare_context_level1_masks_old_tool_results(tmp_path):
    """一级压缩（ct-3）：超阈值时只把旧区 tool_result 就地占位化（保留
    tool_use_id 配对与 assistant 文本），零 LLM 成本；幂等。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([])
    _small_budget(llm)
    turn = _gov_turn(bb, llm, tmp_path)
    msgs = _gov_messages()
    assert _recent_boundary(msgs, 12) == 1  # 巨物落在可压缩旧区
    out, info = turn._prepare_context(msgs, None, [])
    assert out is msgs
    assert info["compacted"] is True and info["level"] == 1 and info["masked"] == 1
    assert msgs[0]["content"][0]["content"].startswith(_CTX_PLACEHOLDER_TAG)
    assert llm.calls == []  # 一级零 LLM 成本
    assert _mask_old_tool_results(msgs, 1) == 0  # 幂等：已占位不再重压


def test_prepare_context_level2_summary_uses_original_and_caches(tmp_path):
    """二级 LLM 摘要（ct-4）：force 下旧区压成 _CTX_SUMMARY_TAG 摘要消息替换
    旧区。关键回归——摘要输入必须是旧区**原文**而非一级占位符（否则丢失逆向
    细节）；相同旧区第二次命中摘要缓存（cached=True，不再调 LLM）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([_resp(text="【摘要】目标 x；地址 0x401000")])
    _small_budget(llm)
    turn = _gov_turn(bb, llm, tmp_path)
    msgs = _gov_messages()
    out, info = turn._prepare_context(msgs, None, [], force=True)
    assert info["level"] == 2 and info["cached"] is False and info["masked"] == 1
    assert out[0]["content"].startswith(_CTX_SUMMARY_TAG)
    assert "【摘要】" in out[0]["content"]
    assert len(out) == len(msgs)  # 摘要 1 条替换旧区 1 条
    # 摘要调用：system=逆向增强模板；输入为原文（20000 个 A），非占位符
    s = llm.calls[0]
    assert s["system"] == _CTX_SUMMARY_SYS
    assert s["messages"][0]["content"] == "A" * 20000
    assert _CTX_PLACEHOLDER_TAG not in s["messages"][0]["content"]
    # 摘要缓存命中：相同旧区（新拷贝）不再调 LLM
    out2, info2 = turn._prepare_context(_gov_messages(), None, [], force=True)
    assert info2["level"] == 2 and info2["cached"] is True
    assert len(llm.calls) == 1


def test_prepare_context_no_compaction_under_budget(tmp_path):
    """未超阈值原样返回（不压缩、不占位、不调 LLM）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([])
    turn = _gov_turn(bb, llm, tmp_path)
    msgs = [{"role": "user", "content": "hi"}]
    out, info = turn._prepare_context(msgs, None, [])
    assert out is msgs and info["compacted"] is False and info["level"] == 0
    assert llm.calls == []


def test_chat_reactive_overflow_forces_compaction_and_retries(tmp_path):
    """reactive 兜底（ct-5）：发送前压缩后仍被网关判输入超限（
    ContextOverflowError）→ 强制压缩（force）一次后重试，且 emit
    chat.ctx phase=reactive。对应 Claude Code 五级级联最末 reactive compact。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")

    class OverflowOnce(FakeLLM):
        def __init__(self, script):
            super().__init__(script)
            self.overflowed = False

        def chat(self, messages, **kwargs):
            if not self.overflowed:
                self.overflowed = True
                with self.lock:
                    self.calls.append({"messages": [dict(m) for m in messages],
                                       "system": kwargs.get("system"),
                                       "tools": kwargs.get("tools")})
                raise ContextOverflowError("Input length exceeds the maximum")
            return super().chat(messages, **kwargs)

    llm = OverflowOnce([_resp(text="【摘要】压缩后"), _resp(text="重试成功")])
    _small_budget(llm)
    turn = _gov_turn(bb, llm, tmp_path)
    resp = turn._chat(_gov_messages(), None, None)
    assert resp.text == "重试成功"
    assert len(llm.calls) == 3  # ①超限抛 ②摘要 ③重试
    assert llm.calls[1]["system"] == _CTX_SUMMARY_SYS
    assert llm.calls[2]["messages"][0]["content"].startswith(_CTX_SUMMARY_TAG)
    evs = [e for e in bb.recent_events("p1") if e["kind"] == "chat.ctx"]
    assert any(e["payload"].get("phase") == "reactive" for e in evs)


def test_usage_includes_ctx_window(tmp_path):
    """面板分母/警戒线由后端下发：usage 带 ctx_limit（硬窗口）与 ctx_soft
    （软上限），缺省走运行时模块常量；并带 compaction 说明（ct-8）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([_resp(text="收到。")])
    thread = chat_store.create_thread(bb, "p1", "recon")
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=None)
    turn.run("你好")
    usage = chat_store.get_thread(bb, thread["id"])["usage"]
    assert usage["ctx_limit"] == _MODEL_WINDOW_FALLBACK
    assert usage["ctx_soft"] == _CTX_SOFT_BUDGET
    assert usage["compaction"]["compacted"] is False


# ---------- 工具结果合并（2026-09-30：Ark 400「没说两句就死」根因回归） ----------

def test_load_history_merges_multiple_tool_results(tmp_path):
    """回归（Ark 400 根因）：同一轮 assistant 的多个 tool_use，其 tool_result
    回放时曾各占一条 user 消息 → Ark Anthropic→OpenAI 翻译层报
    「insufficient tool messages following tool_calls message」直接 400（会话
    没说几句即死）。修复：_load_history 缓冲连续 tool 行，合并成「一条」user
    承载多个 tool_result。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    t = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    chat_store.append_message(bb, t["id"], "user", "开始")
    chat_store.append_message(bb, t["id"], "assistant", "",
                              tool_calls=[{"id": "t1", "name": "todo_write", "args": {}},
                                          {"id": "t2", "name": "todo_write", "args": {}}])
    chat_store.append_message(bb, t["id"], "tool", "结果1", tool_use_id="t1")
    chat_store.append_message(bb, t["id"], "tool", "结果2", tool_use_id="t2")
    chat_store.append_message(bb, t["id"], "assistant", "完成")
    turn = ChatTurn(bb=bb, llm=FakeLLM([]), project_id="p1", thread_id=t["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=None)
    msgs = turn._load_history()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert len(msgs[1]["content"]) == 2  # 两个 tool_use
    # 关键：两条 tool 行合并进「一条」user，内含两个 tool_result（配对齐全）
    assert len(msgs[2]["content"]) == 2
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["t1", "t2"]


def test_loop_sends_one_user_message_for_multi_tool_turn(tmp_path):
    """回归（Ark 400 根因）：_loop 一度逐条 append(user:tool_result) → 一轮多
    工具即触发翻译层 400。修复后发送体里 assistant(tool_use×N) 紧跟「恰好一条」
    user（含 N 个 tool_result），不再拆成 N 条 user。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    llm = FakeLLM([
        _resp(tool_calls=[_tc("t1", "todo_write",
                              {"items": [{"title": "a", "status": "pending"}]}),
                          _tc("t2", "todo_write",
                              {"items": [{"title": "b", "status": "pending"}]})]),
        _resp(text="收尾"),
    ])
    t = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=t["id"],
                    packs_root="packs", track="ctf", capabilities=["web"],
                    mcp_bridge=None, expert_names=None)
    assert turn.run("开工") == "收尾"
    sent = llm.calls[1]["messages"]  # 第二次调用（工具结果回填后）
    a_idx = [i for i, m in enumerate(sent) if m["role"] == "assistant"
             and isinstance(m["content"], list)
             and any(b.get("type") == "tool_use" for b in m["content"])]
    assert a_idx, sent
    i = a_idx[-1]
    assert len(sent[i]["content"]) == 2  # assistant 发两个 tool_use
    assert sent[i + 1]["role"] == "user"  # 紧跟一条 user（非多条）
    trs = [b for b in sent[i + 1]["content"] if b.get("type") == "tool_result"]
    assert [b["tool_use_id"] for b in trs] == ["t1", "t2"]
    # 其后不应再有游离的 tool_result user 消息（未被 assistant 消费）
    assert not any(m["role"] == "user" and isinstance(m.get("content"), list)
                   and any(b.get("type") == "tool_result" for b in m["content"])
                   for m in sent[i + 2:])


# ---------- 对话链意图链路（intent-tools-chat，2026-10-01） ----------

def _mk_asset(bb, pid, value):
    from core.blackboard.assets import register_asset
    return register_asset(bb, pid, value, "domain", quiet=True)["id"]


def test_chat_expert_intent_first_gate(tmp_path):
    """对话子专家（author=chat-*）也受意图先行门禁：无 open 意图登记发现 → [拒绝]；
    declare_intent（挂资产锚点）后再登记 → 放行。修复「对话跑完链路图空」。"""
    from core.agent.tools import ToolDispatcher
    from core.runtime.gateway import ExecutionGateway
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    aid = _mk_asset(bb, "p1", "target.example.com")
    d = ToolDispatcher(bb, gateway=ExecutionGateway(bb=bb),
                       project_id="p1", session_id="chat-abcdef123456",
                       author="chat-abcdef123456", track="pentest")
    # 无意图 → 拒
    out = d.dispatch("bb_add_finding", {"vuln_class": "recon-x", "title": "暴露",
                                        "severity": "low", "category": "intel"})
    assert out.startswith("[拒绝]") and "declare_intent" in out
    # 声明（带资产锚点）→ 需先有一次执行动作再登记发现（第二道闸）
    di = d.dispatch("declare_intent", {"statement": "验证目标是否存在未授权入口",
                                       "target_asset_id": aid})
    assert di.startswith("intent=") and "[意图口径]" in di
    d.dispatch("run_cmd", {"cmd": "echo probe", "runtime": "host",
                           "threat_class": "trusted"})
    out2 = d.dispatch("bb_add_finding", {"vuln_class": "recon-x", "title": "暴露",
                                         "severity": "low", "category": "intel",
                                         "target_asset_id": aid})
    assert not out2.startswith("[拒绝]"), out2
    # 意图落库（链路图有数据可画）
    rows = bb.conn.execute("SELECT statement, target_asset_id FROM intents"
                           " WHERE project_id='p1'").fetchall()
    assert len(rows) == 1 and rows[0]["target_asset_id"] == aid


def test_chat_expert_must_close_intent_before_summary(tmp_path):
    """子专家声明意图后直接交摘要 → 被拦（回执提示先 close_intent），
    补收尾后下一条文本才被当终稿放行。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    thread = chat_store.create_thread(bb, "p1", "web-solver")
    aid = _mk_asset(bb, "p1", "t.example.com")
    author = f"chat-{thread['id'][-12:]}"
    from core.blackboard.intents import declare_intent
    it = declare_intent(bb, "p1", "验证入口可达性", target_asset_id=aid,
                        author=author)
    # dead_end 收尾需 evidence_refs → 先造一条 http 历史
    hid = bb.add_http_history("p1", source="replay", method="GET",
                              url="http://t.example.com/", status=404)
    llm = FakeLLM([
        _resp(text="我打算先说结论"),                       # 1：被拦（有 open 意图）
        _resp(tool_calls=[_tc("c1", "close_intent",
                              {"intent_id": it["id"], "outcome": "dead_end",
                               "dead_reason": "入口不可达",
                               "evidence_refs": [f"http:{hid}"]})]),
        _resp(text="结论：入口不可达（已收尾）"),            # 3：放行
    ])
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="pentest", capabilities=["web"],
                    mcp_bridge=None, expert_names=None)
    final = turn.run("做被动侦察")
    # 第一次纯文本被拦 → 第二轮消息里应出现收尾提醒
    blob = json.dumps(llm.calls[1]["messages"], ensure_ascii=False, default=str)
    assert "未收尾的意图" in blob and "close_intent" in blob
    # 收尾后放行终稿
    assert final == "结论：入口不可达（已收尾）"
    row = bb.conn.execute("SELECT status FROM intents WHERE id=?",
                          (it["id"],)).fetchone()
    assert row["status"] == "closed"


def test_chat_orchestrator_not_blocked_by_open_intent(tmp_path):
    """主控是调度者：即便有 open 意图也不拦其汇总文本（拦截只针对子专家）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    author = f"chat-{thread['id'][-12:]}"
    aid = _mk_asset(bb, "p1", "o.example.com")
    from core.blackboard.intents import declare_intent
    declare_intent(bb, "p1", "目标级假设", target_asset_id=aid, author=author)
    llm = FakeLLM([_resp(text="汇总：任务完成")])
    turn = ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=thread["id"],
                    packs_root="packs", track="pentest", capabilities=["web"],
                    mcp_bridge=None, expert_names=["recon"])
    assert turn.run("汇总") == "汇总：任务完成"


def test_bb_delete_intent_guard_and_delete(tmp_path):
    """bb_delete_intent：open 意图可物理删（落 intent.deleted）；已收尾拒删。"""
    from core.agent.tools import ToolDispatcher
    from core.runtime.gateway import ExecutionGateway
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    aid = _mk_asset(bb, "p1", "d.example.com")
    d = ToolDispatcher(bb, gateway=ExecutionGateway(bb=bb),
                       project_id="p1", session_id="sess-xyz", author="sess-xyz",
                       track="pentest")
    di = d.dispatch("declare_intent", {"statement": "误声明意图",
                                       "target_asset_id": aid})
    iid = di.split("intent=", 1)[1].split(" ", 1)[0]
    out = d.dispatch("bb_delete_intent", {"intent_id": iid, "reason": "误声明"})
    assert "已物理删除" in out
    assert bb.conn.execute("SELECT COUNT(*) c FROM intents").fetchone()["c"] == 0
    assert any(e["kind"] == "intent.deleted"
               for e in bb.recent_events("p1"))
    # 已收尾拒删：新建一条并收尾
    di2 = d.dispatch("declare_intent", {"statement": "会收尾的意图",
                                        "target_asset_id": aid})
    iid2 = di2.split("intent=", 1)[1].split(" ", 1)[0]
    # 造证据（http 历史）用于 dead_end 收尾
    hid = bb.add_http_history("p1", source="replay", method="GET",
                              url="http://d.example.com/", status=404)
    d.dispatch("close_intent", {"intent_id": iid2, "outcome": "dead_end",
                                "dead_reason": "不可达", "evidence_refs": [f"http:{hid}"]})
    out2 = d.dispatch("bb_delete_intent", {"intent_id": iid2})
    assert out2.startswith("[拒绝]") and "已收尾" in out2


# ---------- 健壮性（2026-10-01）：错误结构化 + 悬空 tool_calls 修复 ----------

class _BoomLLM:
    """chat 即抛连接重置（模拟网关 TCP 重置 10054）。"""

    def chat(self, messages, **kwargs):
        raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")


class _AbortAfterRespLLM:
    """返回带 tool_calls 的响应（中止时机由测试自定）。"""

    def __init__(self, resp):
        self._resp = resp

    def chat(self, messages, **kwargs):
        return self._resp


def _turn(bb, tid, llm, *, track="ctf", caps=("web",), abort_event=None):
    return ChatTurn(bb=bb, llm=llm, project_id="p1", thread_id=tid,
                    packs_root="packs", track=track, capabilities=list(caps),
                    mcp_bridge=None, expert_names=None, abort_event=abort_event)


def test_classify_error_categories():
    """异常 → 结构化错误分类（网络/限流/鉴权/额度/超限/请求非法/未知）。"""
    assert _classify_error(ConnectionResetError(10054, "x"))["category"] == "network"
    assert _classify_error(TimeoutError("t"))["category"] == "network"
    from core.llm.provider import LLMError
    assert _classify_error(LLMError("HTTP 429 rate limit", status=429))["category"] \
        == "rate_limit"
    assert _classify_error(LLMError("HTTP 401 unauthorized", status=401))["category"] \
        == "auth"
    assert _classify_error(ContextOverflowError("input length too long", status=400))[
        "category"] == "context"
    assert _classify_error(LLMError("HTTP 400 InvalidParameter", status=400))[
        "category"] == "bad_request"
    assert _classify_error(ValueError("weird"))["category"] == "unknown"
    # 每类都带 title / hint / message（前端卡片三要素）
    info = _classify_error(ConnectionResetError(10054, "reset"))
    assert info["title"] and info["hint"] and "reset" in info["message"]


def test_chat_run_failure_persists_structured_error(tmp_path):
    """轮次失败：落结构化 error（分类/建议/技术细节）到 chat_threads；新轮清空。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    t = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    with pytest.raises(ConnectionResetError):
        _turn(bb, t["id"], _BoomLLM()).run("hi")
    thread = chat_store.get_thread(bb, t["id"])
    assert thread["status"] == "error"
    err = thread["error"]
    assert err and err["category"] == "network"
    assert err["title"] and err["hint"] and "10054" in err["message"]
    # 新轮成功 → status=idle 且 error 清空（None）
    assert _turn(bb, t["id"], FakeLLM([_resp(text="好的")])).run("再来") == "好的"
    after = chat_store.get_thread(bb, t["id"])
    assert after["status"] == "idle" and after["error"] is None


def test_chat_abort_mid_tools_keeps_tool_pairing(tmp_path):
    """中止发生在工具循环中途（工具 1 执行时按停止）：为当前及剩余 tool_calls
    补落占位结果，历史配对完整（否则悬空 tool_calls → 下轮 400，线程永久损坏）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    _mk_project(bb, "p1")
    ev = threading.Event()
    resp = _resp(tool_calls=[_tc("t1", "todo_write", {"items": []}),
                             _tc("t2", "todo_write", {"items": []})])
    t = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID)
    turn = _turn(bb, t["id"], _AbortAfterRespLLM(resp), abort_event=ev)
    orig = turn._dispatch

    def dispatch(tc):
        result = orig(tc)
        ev.set()  # 第一个工具执行完毕 → 模拟人此刻按停止
        return result

    turn._dispatch = dispatch
    turn.run("开工")
    msgs = chat_store.list_messages(bb, t["id"])
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool", "assistant"]
    assert len(msgs[1]["tool_calls"]) == 2
    assert {m["tool_use_id"] for m in msgs if m["role"] == "tool"} == {"t1", "t2"}
    assert _pairing_ok(_load_api(bb, t["id"]))  # 每个 tool_use 都有对应 tool_result
    assert "已按人类要求停止" in msgs[-1]["content"]
    assert chat_store.get_thread(bb, t["id"])["status"] == "idle"


def test_sanitize_history_repairs_dangling_and_orphans():
    """历史兜底：悬空 tool_use 补占位结果、孤儿 tool_result 丢弃、正常历史零改动。"""
    normal = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    assert _sanitize_history(normal) == normal  # 幂等
    dangling = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t9", "name": "x", "input": {}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "停止"}]},
    ]
    fixed = _sanitize_history(dangling)
    assert fixed[0]["content"][0]["type"] == "tool_use"
    assert fixed[1]["role"] == "user"
    assert fixed[1]["content"][0] == {
        "type": "tool_result", "tool_use_id": "t9",
        "content": "（该工具调用无结果记录：历史已自动修复）"}
    assert fixed[2]["content"][0]["text"] == "停止"
    orphan = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ghost", "content": "x"}]},
    ]
    assert _sanitize_history(orphan) == [{"role": "user", "content": "hi"}]


def test_sanitize_history_deduplicates_and_reorders_tool_results():
    history = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "one", "input": {}},
            {"type": "tool_use", "id": "a", "name": "duplicate", "input": {}},
            {"type": "tool_use", "id": "b", "name": "two", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "b", "content": "B"},
            {"type": "tool_result", "tool_use_id": "b", "content": "B-dup"},
            {"type": "tool_result", "tool_use_id": "ghost", "content": "bad"},
            {"type": "tool_result", "tool_use_id": "a", "content": "A"},
        ]},
    ]
    fixed = _sanitize_history(history)
    assert [b["id"] for b in fixed[0]["content"]] == ["a", "b"]
    assert [(b["tool_use_id"], b["content"]) for b in fixed[1]["content"]] == [("a", "A"), ("b", "B")]


def test_sanitize_history_drops_empty_tool_names():
    """旧 Responses 解析残留的空工具名不能再发给网关。"""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "bad", "name": "", "input": {}},
            {"type": "text", "text": "继续处理"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "bad", "content": "旧结果"},
        ]},
    ]
    assert _sanitize_history(history) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "继续处理"},
        ]},
    ]


def _load_api(bb, tid):
    """经 _load_history 装载（含 _sanitize_history）后的 API 消息数组。"""
    return _turn(bb, tid, FakeLLM([]))._load_history()


def _pairing_ok(messages) -> bool:
    """校验：每个 assistant tool_use 都有对应 tool_result；无孤儿 tool_result。"""
    pending: set[str] = set()
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                pending.add(b.get("id"))
            elif b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid not in pending:
                    return False
                pending.discard(tid)
    return not pending
