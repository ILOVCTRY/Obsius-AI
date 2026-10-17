"""诊断（diagnostics）：会话消息审计 + 工具/MCP 失败日志 + 系统提示词快照。

- failog：失败记录与查询（tool.failed / mcp.failed 事件）。
- system_prompt：会话系统提示词快照（v39，诊断页「查看系统提示词」数据源）。
"""

from core.diagnostics.failog import (
    FAILURE_KINDS,
    query_failures,
    record_mcp_failure,
    record_tool_failure,
)
from core.diagnostics.system_prompt import get_system_prompt, save_system_prompt

__all__ = [
    "FAILURE_KINDS",
    "query_failures",
    "record_mcp_failure",
    "record_tool_failure",
    "get_system_prompt",
    "save_system_prompt",
]
