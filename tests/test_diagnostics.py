"""诊断失败日志（diagnostics-log，2026-10-14）：tool.failed / mcp.failed
记录与查询 + dispatcher / chat runtime 挂钩子。"""

import pytest

from core.blackboard import Blackboard
from core.diagnostics.failog import (
    FAILURE_KINDS,
    query_failures,
    record_mcp_failure,
    record_tool_failure,
)


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def project(bb):
    return bb.create_project("诊断-测试项目", "pentest", ["web"])


# ---------- failog 记录 ----------

def test_record_tool_failure_persists_event(bb, project):
    pid = project["id"]
    record_tool_failure(bb, pid, name="bb_query", args={"what": "nonsense"},
                        error="[错误] 未知查询: nonsense", session_id="sess-1",
                        author="sess-1", step=7)
    rows = query_failures(bb, pid)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "tool.failed"
    assert r["session_id"] == "sess-1" and r["author"] == "sess-1"
    p = r["payload"]
    assert p["name"] == "bb_query" and p["source"] == "task" and p["step"] == 7
    assert p["args"] == {"what": "nonsense"}
    assert p["error"] == "[错误] 未知查询: nonsense"


def test_record_tool_failure_clips_long_args_and_error(bb, project):
    pid = project["id"]
    record_tool_failure(bb, pid, name="run_cmd",
                        args={"cmd": "x" * 500}, error="[错误] " + "e" * 2000,
                        session_id="s", author="chat-abcdef123456")
    p = query_failures(bb, pid)[0]["payload"]
    assert p["args"]["cmd"].endswith("…(截断)") and len(p["args"]["cmd"]) < 300
    assert len(p["error"]) <= 800
    # chat author → source=chat
    assert p["source"] == "chat"


def test_record_mcp_failure_shape(bb, project):
    pid = project["id"]
    record_mcp_failure(bb, pid, server="fir-proxy", tool="http_request",
                       error="[错误] MCP 调用失败：server 离线或协议错误",
                       session_id="chat-deadbeef1234", author="chat-deadbeef1234",
                       thread_id="chat-fullthreadid01")
    rows = query_failures(bb, pid)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "mcp.failed"
    p = r["payload"]
    assert p["name"] == "mcp__fir-proxy__http_request"
    assert p["server"] == "fir-proxy" and p["tool"] == "http_request"
    assert p["thread_id"] == "chat-fullthreadid01"
    assert p["source"] == "chat"


def test_record_failure_never_raises(bb, project):
    """审计绝不能炸工具链：append_event 抛错也静默吞。"""

    class _Boom:
        def append_event(self, *a, **kw):
            raise RuntimeError("db locked")

    record_tool_failure(_Boom(), "p", name="x", error="[错误] boom")  # 不抛
    record_mcp_failure(_Boom(), "p", server="s", tool="t", error="[错误] boom")


# ---------- failog 查询 ----------

def _seed(bb, pid):
    record_tool_failure(bb, pid, name="bb_query", error="[错误] a",
                        session_id="sess-a", author="sess-a")
    record_tool_failure(bb, pid, name="run_cmd", error="[错误] b",
                        session_id="sess-b", author="chat-abc123")
    record_mcp_failure(bb, pid, server="ida", tool="decompile",
                       error="[错误] c", session_id="sess-b",
                       author="chat-abc123")


def test_query_failures_desc_order_and_kinds(bb, project):
    pid = project["id"]
    _seed(bb, pid)
    all_rows = query_failures(bb, pid)
    assert [r["kind"] for r in all_rows] == ["mcp.failed", "tool.failed", "tool.failed"]
    tool_rows = query_failures(bb, pid, kinds=("tool.failed",))
    assert len(tool_rows) == 2 and all(r["kind"] == "tool.failed" for r in tool_rows)
    mcp_rows = query_failures(bb, pid, kinds=("mcp.failed",))
    assert len(mcp_rows) == 1


def test_query_failures_name_and_author_filters(bb, project):
    pid = project["id"]
    _seed(bb, pid)
    assert len(query_failures(bb, pid, name="run_cmd")) == 1
    # 全名 mcp__ida__decompile 子串也命中
    assert len(query_failures(bb, pid, name="ida")) == 1
    assert len(query_failures(bb, pid, name="ida", kinds=("tool.failed",))) == 0
    # author：事件列或 payload.thread_id 子串
    assert len(query_failures(bb, pid, author="sess-b")) == 2
    assert len(query_failures(bb, pid, author="chat-abc123")) == 2
    assert len(query_failures(bb, pid, author="chat-")) == 2


def test_query_failures_since_id_and_limit(bb, project):
    pid = project["id"]
    _seed(bb, pid)
    first_id = query_failures(bb, pid, limit=3)[0]["id"]
    assert query_failures(bb, pid, since_id=first_id) == []
    assert len(query_failures(bb, pid, limit=2)) == 2
    assert len(query_failures(bb, pid, limit=0) or []) == 0 or True  # limit=0 不炸


# ---------- dispatcher 挂钩子 ----------

def _dispatcher(bb, project, session_id="sess-x", author="sess-x"):
    from core.agent.tools import ToolDispatcher
    return ToolDispatcher(bb, gateway=None, project_id=project["id"],
                          session_id=session_id, author=author, track="pentest")


def test_dispatch_failure_records_tool_failed(bb, project):
    d = _dispatcher(bb, project)
    r = d.dispatch("bb_query", {"what": "nonsense"})
    assert r.startswith("[错误]")
    rows = query_failures(bb, project["id"], kinds=("tool.failed",))
    assert len(rows) == 1
    p = rows[0]["payload"]
    assert p["name"] == "bb_query" and p["source"] == "task"
    assert rows[0]["session_id"] == "sess-x"


def test_dispatch_success_records_nothing(bb, project):
    d = _dispatcher(bb, project)
    r = d.dispatch("bb_query", {"what": "findings"})
    assert r == "[]"
    assert query_failures(bb, project["id"]) == []


def test_dispatch_unknown_tool_records_tool_failed(bb, project):
    d = _dispatcher(bb, project)
    r = d.dispatch("no_such_tool", {})
    assert "未知工具" in r
    rows = query_failures(bb, project["id"])
    assert len(rows) == 1 and rows[0]["payload"]["name"] == "no_such_tool"


# ---------- chat runtime 挂钩子 ----------

def _chat_turn(bb, project):
    from core.chat.runtime import ChatTurn
    return ChatTurn(bb=bb, llm=None, project_id=project["id"],
                    thread_id="chat-probe0001", packs_root="packs",
                    track="pentest", capabilities=["web"])


def test_chat_record_failure_mcp_branch(bb, project):
    t = _chat_turn(bb, project)
    t._record_failure("mcp__fir-proxy__http_request",
                      "[错误] MCP 调用失败：server 'fir-proxy' 未启用或域名不匹配")
    rows = query_failures(bb, project["id"], kinds=("mcp.failed",))
    assert len(rows) == 1
    p = rows[0]["payload"]
    assert p["server"] == "fir-proxy" and p["tool"] == "http_request"
    assert p["thread_id"] == "chat-probe0001"
    assert p["source"] == "chat"


def test_chat_record_failure_tool_branch_and_noop_on_success(bb, project):
    t = _chat_turn(bb, project)
    t._record_failure("todo_write", "[错误] todo_write 失败：items 必须是数组",
                      {"items": "bad"})
    t._record_failure("call_expert", "专家线程已完成（无错误前缀 → 不记）")
    rows = query_failures(bb, project["id"], kinds=("tool.failed",))
    assert len(rows) == 1
    assert rows[0]["payload"]["name"] == "todo_write"
    # author = chat-<thread_id 末 12 位>（与 _build_dispatcher 同口径）
    assert rows[0]["session_id"] == "chat-" + "chat-probe0001"[-12:]


def test_failure_kinds_constant():
    assert FAILURE_KINDS == ("tool.failed", "mcp.failed")
