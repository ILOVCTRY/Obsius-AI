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
from core.chat.runtime import ORCHESTRATOR_ID, ChatTurn
from core.llm.provider import LLMResponse, ToolCall, Usage


# ---------- 假 LLM ----------

class FakeLLM:
    """脚本化 LLM：按队列吐响应；记录调用（system/tools 断言用）。"""

    def __init__(self, script: list[LLMResponse]):
        self.script = list(script)
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def chat(self, messages, **kwargs):
        with self.lock:
            self.calls.append({"messages": [dict(m) for m in messages],
                               "system": kwargs.get("system"),
                               "tools": kwargs.get("tools")})
            return self.script.pop(0)


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
            "kb_open", "bb_query"} <= names
    assert "run_cmd" not in names and "finish" not in names and "publish_task" not in names
    # 主控 system 含 persona；专家清单在 call_expert 工具描述
    # （_generalist/主控自身不进委派清单）
    assert "问答蛙式调度者" in orch_call["system"]
    ce = next(s for s in orch_call["tools"] if s["name"] == "call_expert")
    assert "web-solver" in ce["description"] and "_generalist" not in ce["description"]
    # 子专家工具面：全量裁剪（无任务队列/收尾原语），且无 call_expert（spawn 深度=1）
    sub_call = llm.calls[2]
    sub_names = {s["name"] for s in sub_call["tools"]}
    assert "run_cmd" in sub_names and "bb_add_finding" in sub_names
    assert "call_expert" not in sub_names and "todo_write" not in sub_names
    assert "complete_task" not in sub_names and "declare_intent" not in sub_names
    # 事件流：chat.message / chat.tool / chat.spawn / chat.todo 可见
    events = [e for e in bb.recent_events("p1") if e["kind"].startswith("chat.")]
    kinds = {e["kind"] for e in events}
    assert {"chat.message", "chat.tool", "chat.spawn", "chat.todo"} <= kinds


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
    """stop 端点：未在跑 409；执行中 204 且轮次以「已停止」收尾、状态归位。"""
    r = chat_client.post("/api/projects", json={
        "name": "停止测试", "track": "ctf", "capabilities": ["web"]})
    pid = r.json()["id"]
    tid = chat_client.post(f"/api/projects/{pid}/chat/threads",
                           json={"agent_id": ORCHESTRATOR_ID}).json()["id"]
    assert chat_client.post(f"/api/chat/threads/{tid}/stop").status_code == 409
    # 慢 LLM：让执行窗口足够长，命中运行中的 stop
    import core.api.app as app_mod
    from core.llm.provider import LLMResponse as _R

    class Slow:
        def __init__(self):
            self.aborted_seen = False

        def chat(self, messages, **kwargs):
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
        deadline = time.time() + 5
        while time.time() < deadline:
            th = c2.get(f"/api/chat/threads/{tid2}").json()["thread"]
            if th["status"] == "running":
                break
            time.sleep(0.05)
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
