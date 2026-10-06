"""Agent 执行上下文。

一次「受管执行」的描述：Team 成员执行（team_id/run_id/member_id 齐全）或
指挥亲自执行（team 字段留空）。该入口不设置旧 task_id，不调用 TaskQueue。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    team_id: str = ""
    run_id: str = ""
    member_id: str = ""
    session_id: str = ""
    objective: str = ""
    role: str = ""
    runtime: str = ""
    threat_class: str = "trusted"
