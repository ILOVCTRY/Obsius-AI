"""诊断（diagnostics）：会话消息审计 + 工具/MCP 失败日志。

- failog：失败记录与查询（tool.failed / mcp.failed 事件）。
"""

from core.diagnostics.failog import (
    FAILURE_KINDS,
    query_failures,
    record_mcp_failure,
    record_tool_failure,
)

__all__ = [
    "FAILURE_KINDS",
    "query_failures",
    "record_mcp_failure",
    "record_tool_failure",
]
