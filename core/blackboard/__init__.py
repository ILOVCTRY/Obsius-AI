"""core.blackboard —— 黑板系统（DESIGN.md §5、§6）。

对外入口：
    from core.blackboard import Blackboard, TaskQueue, ClaimError
"""

from core.blackboard.events import EventBus
from core.blackboard.store import Blackboard
from core.blackboard.tasks import ClaimError, TaskQueue

__all__ = ["Blackboard", "TaskQueue", "ClaimError", "EventBus"]
