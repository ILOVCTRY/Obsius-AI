"""core.blackboard —— 黑板系统（DESIGN.md §5、§6）。

对外入口：
    from core.blackboard import Blackboard
"""

from core.blackboard.events import EventBus
from core.blackboard.store import Blackboard

__all__ = ["Blackboard", "EventBus"]
