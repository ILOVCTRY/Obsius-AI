"""chat 轮次错误恢复回归（2026-10-03 健壮性修复）。

背景：一轮对话里任何「非 LLM」异常（文件读写 OSError、DB sqlite3.Error）原先
一律落 `_classify_error` 的 `unknown` 兜底 → 无重试、直接中断，且前端只看到
「执行异常」无从判断。本次修复四件事，逐条回归：

1. `_classify_error` 增 `filesystem`/`db` 两类，且不误伤 network（TimeoutError/
   ConnectionError 是 OSError 子类，必须仍归 network）。
2. `_retry_transient` 对瞬时 OSError/sqlite3.Error 退避重试 1 次，非瞬时错误不重试。
3. `_system_prompt` 在专家 yaml / 规则链读取失败时降级（空人设 / 空规则串）不中断。
4. `_build_dispatcher` 在白名单/专家读取失败时降级为无工具面而非抛穿。
"""

import sqlite3

import pytest

from core.blackboard.store import Blackboard, BlackboardClosedError
from core.chat import store as chat_store
from core.chat import runtime as R
from core.chat.runtime import ChatTurn, ORCHESTRATOR_ID, _classify_error, _retry_transient


# ---------- 1. 分类器 ----------

def test_classify_filesystem_and_db_categories():
    assert _classify_error(FileNotFoundError(2, "No such file or directory"))["category"] == "filesystem"
    assert _classify_error(PermissionError(13, "Permission denied"))["category"] == "filesystem"
    assert _classify_error(OSError(5, "I/O error"))["category"] == "filesystem"
    assert _classify_error(sqlite3.OperationalError("database is locked"))["category"] == "db"
    assert _classify_error(BlackboardClosedError("closed"))["category"] == "db"


def test_classify_network_not_shadowed_by_filesystem():
    """TimeoutError/ConnectionError 是 OSError 子类——必须仍归 network（分支顺序回归）。"""
    assert _classify_error(TimeoutError("timed out"))["category"] == "network"
    assert _classify_error(ConnectionError("reset"))["category"] == "network"


def test_classify_network_covers_stream_body_drop():
    """流式 body 半途断开的两副面孔都须归 network（2026-10-06 事故回归）：
    ① 裸 httpx.RemoteProtocolError（SDK 迭代不包装 body 异常）；
    ② OpenAI 路径最终抛的 LLMError「网络连接失败（已重试 N 次）」文案。
    此前两者都落 unknown「执行异常」。"""
    import httpx

    from core.llm.provider import LLMError

    assert _classify_error(httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"))["category"] == "network"
    assert _classify_error(LLMError(
        "网络连接失败（已重试 4 次）: peer closed connection"))["category"] == "network"


def test_classify_known_categories_unchanged():
    from core.llm.provider import ContextOverflowError
    assert _classify_error(ContextOverflowError("too long"))["category"] == "context"
    assert _classify_error(ValueError("随便"))["category"] == "unknown"


# ---------- 2. 瞬时重试 ----------

def test_retry_transient_recovers_on_second_attempt():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError(2, "locked by AV")
        return "ok"

    assert _retry_transient(flaky, delay=0.01) == "ok"
    assert calls["n"] == 2


def test_retry_transient_gives_up_and_raises():
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise sqlite3.OperationalError("locked")

    with pytest.raises(sqlite3.OperationalError):
        _retry_transient(always, delay=0.01)
    assert calls["n"] == 2  # attempts=2 → 首次 + 重试 1 次


def test_retry_transient_does_not_retry_semantic_errors():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ValueError("语义错误不重试")

    with pytest.raises(ValueError):
        _retry_transient(bad, delay=0.01)
    assert calls["n"] == 1

    calls2 = {"n": 0}

    def closed():
        calls2["n"] += 1
        raise BlackboardClosedError("项目正在删除")

    with pytest.raises(BlackboardClosedError):
        _retry_transient(closed, delay=0.01)
    assert calls2["n"] == 1  # 删除中的项目不浪费重试


# ---------- 3/4. 装配期降级 ----------

def _bare_turn(**over) -> ChatTurn:
    """绕过 __init__（不建 DB/不起装配），只造一个能调 _system_prompt/_build_dispatcher 的壳。"""
    t = object.__new__(ChatTurn)
    t.packs_root = "packs"
    t.agent_id = "recon"
    t.track = "pentest"
    t.capabilities = ["web"]
    t.owner_tags = []
    t.rule_profiles = None
    t.is_orchestrator = False
    t.thread_id = "chat-test"
    t.thread = {}
    t.bb = None
    t.artifacts_dir = None
    t.browser_pool = None
    t.decompiler_factory = None
    t.abort_event = None
    t.llm = None
    for k, v in over.items():
        setattr(t, k, v)
    return t


def test_system_prompt_degrades_when_expert_and_rules_fail(monkeypatch):
    t = _bare_turn()
    monkeypatch.setattr(R, "load_expert",
                        lambda *a, **k: (_ for _ in ()).throw(
                            FileNotFoundError(2, "No such file or directory")))
    monkeypatch.setattr(R, "build_rules_preamble",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
    prompt = t._system_prompt()  # 不抛
    assert isinstance(prompt, str) and prompt.strip()


def test_build_dispatcher_degrades_when_whitelist_read_fails(monkeypatch):
    t = _bare_turn()
    monkeypatch.setattr(R, "expert_tool_names",
                        lambda *a, **k: (_ for _ in ()).throw(
                            FileNotFoundError(2, "No such file or directory")))
    assert t._build_dispatcher() is None  # 降级无工具面，不抛穿


# ---------- 5. API 构造失败归位（不卡 running） ----------

def test_dispatch_returns_error_text_when_tool_branch_raises(monkeypatch):
    """工具分支（mcp__/todo_write/call_expert）抛异常必须转成 [错误] tool_result
    回给 LLM——此前只有 _dispatcher 分支有 try，其余分支抛错会穿出 _loop 把整轮
    炸成「执行异常 unknown」（工具失败 ≠ 轮失败）。"""
    import threading

    class _TC:
        def __init__(self, name, args):
            self.name, self.arguments = name, args

    t = object.__new__(ChatTurn)
    t.thread_id = "chat-t"
    t.abort_event = threading.Event()
    t._dispatcher = None

    t._tool_todo_write = lambda a: (_ for _ in ()).throw(
        FileNotFoundError(2, "No such file or directory"))
    ok, text = t._dispatch(_TC("todo_write", {"items": []}))
    assert ok is False and text.startswith("[错误]") and "No such file" in text

    t._dispatch_mcp = lambda n, a: (_ for _ in ()).throw(OSError(5, "io"))
    ok, text = t._dispatch(_TC("mcp__srv__tool", {}))
    assert ok is False and text.startswith("[错误]")

    t._tool_call_expert = lambda a: (_ for _ in ()).throw(
        FileNotFoundError(2, "No such file or directory"))
    ok, text = t._dispatch(_TC("call_expert", {"expert": "recon", "task": "x"}))
    assert ok is False and text.startswith("[错误]")


def test_call_expert_returns_error_when_subturn_construction_fails(tmp_path, monkeypatch):
    """子专家 ChatTurn 构造抛错（读专家 yaml 失败等）须回 [错误] 文本，不再穿出
    主控整轮。"""
    import threading

    bb = Blackboard(str(tmp_path / "bb.db"))
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO projects(id, name, domain, track, config, created_at)"
            " VALUES(?,?,?,?,?,datetime('now'))", ("p1", "测试", "p1", "p1", "{}"))
    t = object.__new__(ChatTurn)
    t.bb = bb
    t.llm = object()
    t.project_id = "p1"
    t.thread_id = "chat-parent0000000000"
    t.packs_root = "packs"
    t.track = "pentest"
    t.capabilities = ["web"]
    t.mcp_bridge = None
    t.expert_names = ["recon"]
    t.abort_event = threading.Event()
    t.owner_tags = []
    t.rule_profiles = None
    t.artifacts_dir = None
    t.browser_pool = None
    t.decompiler_factory = None
    t.is_orchestrator = True
    t.agent_id = ORCHESTRATOR_ID
    t.thread = {}

    def _boom(**kw):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(R, "ChatTurn", _boom)
    out = t._tool_call_expert({"expert": "recon", "task": "测目标"})
    assert out.startswith("[错误]") and "recon" in out
    bb.close()


def test_constructor_failure_resets_thread_status(tmp_path):
    """ChatTurn(...) 构造抛错时，线程状态必须从 running 归位 error（原先只 log，
    前端永久「执行中」且无错误卡片）。"""
    bb = Blackboard(str(tmp_path / "bb.db"))
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO projects(id, name, domain, track, config, created_at)"
            " VALUES(?,?,?,?,?,datetime('now'))", ("p1", "测试", "p1", "p1", "{}"))
    thread = chat_store.create_thread(bb, "p1", ORCHESTRATOR_ID, title="构造失败")
    chat_store.update_thread(bb, thread["id"], status="running")

    # 模拟 app.py 的兜底归位逻辑
    err = FileNotFoundError(2, "No such file or directory")
    th = chat_store.get_thread(bb, thread["id"])
    if th and th.get("status") == "running":
        chat_store.update_thread(bb, thread["id"], status="error",
                                 error=_classify_error(err))
    after = chat_store.get_thread(bb, thread["id"])
    assert after["status"] == "error"
    assert after["error"]["category"] == "filesystem"
    bb.close()
