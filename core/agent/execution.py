"""Agent 执行上下文。

Team direct execution 不使用旧 task_id；上下文只描述本次 Team 成员执行。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    team_id: str
    run_id: str
    member_id: str
    session_id: str
    objective: str
    role: str = ""
    runtime: str = ""
    threat_class: str = "trusted"
