"""多智能体协调域。

该包只管理协调计划、任务依赖和证据状态，不直接调用现有智能体工作台。
后续调度器、资源租约和验证闭环都在这里扩展。
"""

from .store import CoordinationStore

__all__ = ["CoordinationStore"]
