"""core.agent —— Agent 主循环（DESIGN.md §3）。

对外入口：
    from core.agent import AgentSession, AgentConfig
"""

from core.agent.loop import AgentConfig, AgentSession
from core.agent.tools import AGENT_TOOLS, ToolDispatcher

__all__ = ["AgentSession", "AgentConfig", "ToolDispatcher", "AGENT_TOOLS"]
