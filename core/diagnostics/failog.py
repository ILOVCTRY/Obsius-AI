"""诊断：工具 / MCP 调用失败日志（diagnostics-log，2026-10-14）。

审计动因：平台有两类工具执行路径，失败可观测性不对称——
- 任务链 AgentSession → ToolDispatcher.dispatch（core/agent/tools.py）：
  tool.call 审计事件带 ok=False，但混在全部工具调用里，无独立失败查询面；
- 工作台 chat 线程 → ChatTurn._dispatch（core/chat/runtime.py）：本地工具
  同样落 ToolDispatcher，而 **MCP（mcp__ 分支）失败此前无任何事件**——只在
  chat_messages 的 tool 结果行里留一段 [错误] 文本，翻不动、筛不出。

本模块把两类失败收敛为独立事件（blackboard events，author/session_id 走列）：
- ``tool.failed``：本地/黑板工具失败（含 chat 的 todo_write / call_expert /
  分发异常 / 无工具面等非 dispatcher 分支）；
- ``mcp.failed``：MCP server 工具调用失败（server 离线 / 协议错误 / 工具报错）。

payload：``{name, server?, tool?, args(截断), error(截断), source, step?,
thread_id?}``。source=chat（author 形态 chat-<tid12>）或 task（任务链会话）。

**best-effort 红线**：记录失败绝不影响工具返回——任何落库异常静默吞掉
（审计事件炸掉工具链比丢一条日志严重得多）。
"""

from __future__ import annotations

from typing import Any

# 失败日志事件 kind（查询面与前端过滤 chips 共用）
FAILURE_KINDS = ("tool.failed", "mcp.failed")

_ERR_MAX = 800        # error 截断（与 tool.call 的 result_head 400 同量级）
_ARG_VAL_MAX = 200    # args 单值截断（对齐 tools._truncate_args）
_SCAN_CAP = 2000      # 查询面扫描上限（失败量级小，Python 侧过滤）


def _clip_args(args: dict[str, Any] | None) -> dict[str, Any]:
    """入参截断（防事件表膨胀；完整值在 chat_messages 的工具结果行里）。"""
    if not isinstance(args, dict):
        return {}
    return {k: (f"{v[:_ARG_VAL_MAX]}…(截断)"
                if isinstance(v, str) and len(v) > _ARG_VAL_MAX else v)
            for k, v in args.items()}


def _source_of(author: str) -> str:
    """chat 线程 dispatcher 的 author 形态为 chat-<tid12>（runtime._build_dispatcher）。"""
    return "chat" if author.startswith("chat-") else "task"


def _record(bb, project_id: str, kind: str, payload: dict[str, Any],
            session_id: str, author: str) -> None:
    try:
        bb.append_event(project_id, kind, payload,
                        session_id=session_id, author=author)
    except Exception:  # noqa: BLE001 —— 审计绝不炸工具链
        pass


def record_tool_failure(bb, project_id: str, *, name: str,
                        args: dict[str, Any] | None = None, error: str = "",
                        session_id: str = "", author: str = "",
                        step: int | None = None,
                        thread_id: str = "") -> None:
    """记一条工具失败（tool.failed）。"""
    payload: dict[str, Any] = {
        "name": name,
        "source": _source_of(author),
        "args": _clip_args(args),
        "error": error[:_ERR_MAX],
    }
    if step is not None:
        payload["step"] = step
    if thread_id:
        payload["thread_id"] = thread_id
    _record(bb, project_id, "tool.failed", payload, session_id, author)


def record_mcp_failure(bb, project_id: str, *, server: str, tool: str,
                       error: str = "", session_id: str = "",
                       author: str = "", thread_id: str = "") -> None:
    """记一条 MCP 调用失败（mcp.failed）。"""
    payload: dict[str, Any] = {
        "name": f"mcp__{server}__{tool}",
        "server": server,
        "tool": tool,
        "source": _source_of(author),
        "error": error[:_ERR_MAX],
    }
    if thread_id:
        payload["thread_id"] = thread_id
    _record(bb, project_id, "mcp.failed", payload, session_id, author)


def query_failures(bb, project_id: str, *, kinds: tuple[str, ...] | list[str] | None = None,
                   name: str | None = None, author: str | None = None,
                   since_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    """失败日志查询（倒序，最新在前；升序返回不便阅读故统一 desc）。

    kinds：缺省=两类失败全要；name：工具名/全名 mcp__s__t 子串过滤；
    author：**来源子串**过滤——events.author 列、events.session_id 列、
    payload.thread_id 三处任一命中即算（前端从会话列表点进来按会话筛失败
    用；任务链 author=session_id，chat author=chat-<tid12> 而 thread_id 才是
    前端持有的完整线程 id，故三处都要匹配）。过滤在 Python 侧做（失败量级小，
    避免 sqlite JSON 抽取的兼容性坑）；扫描上限 _SCAN_CAP。
    """
    want = tuple(kinds) if kinds else FAILURE_KINDS
    ph = ",".join("?" * len(want))
    rows = bb.conn.execute(
        f"SELECT * FROM events WHERE project_id=? AND kind IN ({ph})"
        f" AND id>? ORDER BY id DESC LIMIT ?",
        (project_id, *want, since_id, _SCAN_CAP)).fetchall()
    name_l = (name or "").strip().lower()
    author_l = (author or "").strip().lower()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        p = d.get("payload")
        if isinstance(p, str):
            import json
            try:
                p = json.loads(p)
            except ValueError:
                p = {}
        p = p if isinstance(p, dict) else {}
        full = str(p.get("name") or "")
        if name_l and name_l not in full.lower():
            continue
        if author_l and not any(author_l in str(v or "").lower() for v in
                                 (d.get("author"), d.get("session_id"),
                                  p.get("thread_id"))):
            continue
        d["payload"] = p
        out.append(d)
        if len(out) >= limit:
            break
    return out
