"""会话/任务状态单一容器（session-state 收敛，2026-10-03）。

**为什么存在**：主循环状态原先散落在 `AgentSession` 与 `ToolDispatcher` 两处
（`_resume_state`/`_reject_streak`/`_stuck_waves` 在 loop，`finished`/
`plan_only_mode`/`closing_round` 在 dispatcher），复位点分散在 `_loop_body`
开头十余行 + `reset_closing()`，历史多次踩「上一任务残留字段带进新任务」的
stale 状态 bug（stale `finished` 误 fail、`_resume_state` 泄漏被下轮清盘）。

**只收敛存取，不改控制流**：两处仍各自保留原属性名（改成 `property` 代理到
共享的 `SessionState` 实例），闸门评估顺序与双向复位点原地不动。外部
`agent._resume_state = {...}`、`dispatcher.plan_only_mode = True` 等读写照旧。
"""

from dataclasses import dataclass, field


def state_proxy(attr: str) -> property:
    """生成双向代理到共享 `SessionState` 的 property。

    持有者（`AgentSession` / `ToolDispatcher`）各自以**原属性名**定义
    `xxx = state_proxy("xxx")`——于是既有读写点与测试断言（`agent._resume_state`、
    `dispatcher.plan_only_mode`）一字不改，状态却只有一份。"""
    return property(
        lambda self: getattr(self.state, attr),
        lambda self, value: setattr(self.state, attr, value),
    )


@dataclass
class SessionState:
    """会话状态。字段名 = 各持有者的属性名去下划线后的语义名。"""

    # ---------- AgentSession 侧（每任务复位组，见 reset_for_task） ----------
    resume_state: dict | None = None      # 暂停断点快照（内存态；用后必须清）
    live_state: dict | None = None        # _loop 在册的当前现场（暂停即时落盘用）
    salvage_ctx: dict | None = None       # fail 抢救收尾现场（异常路径专属）
    reject_streak: int = 0                # E2 硬拒绝熔断：连续含硬拒绝的模型步数
    plan_gate_count: int = 0              # 计划闸教练：含计划闸回执的模型步数
    stuck_waves: int = 0                  # D1 卡死波次
    stuck_extensions: int = 0             # D9 活跃探索静默延长次数
    cadence_last_rev: int = 0             # 阶段三节拍：上次 task.plan_revised 事件 id
    cadence_last_hint_step: int = 0       # 阶段三节拍：上次周期提示步
    cadence_hinted_findings: set[str] = field(default_factory=set)
    # 撤回传播去重（会话级，不随任务复位）：已在首条消息告过警的任务 id
    stale_alerted: set[str] = field(default_factory=set)

    # ---------- ToolDispatcher 侧 ----------
    current_task_id: str | None = None    # 非每任务复位：收尾/中断路径显式清
    last_progress_step: int = 0           # 最近一次实质进展的步号（卡死检测用）
    finished: bool = False
    awaiting_human: bool = False          # C1：fail_task(awaiting_human) 置位
    finish_open_intents_ack: bool = False # 意图纪律①：finish 撞未收尾意图后置位
    summary: str = ""
    delegation_just_finished: bool = False  # 会话中心化：委托真收尾置位
    last_delegation_note: str = ""
    plan_only_mode: bool = False          # 计划闸教练模式：工具面收缩到计划/控制
    closing_round: int = 0                # D6：0=未在确认；1/2=当前确认轮序号
    closing_last_progress: int = 0        # D6：本轮发起时的 last_progress_step

    def reset_for_task(self) -> None:
        """每任务复位（_loop_body 入口调用）。

        复位集合与重构前 `_loop_body` 开头逐字段赋值 + `reset_closing()` **逐字段
        等价**——只列原先真被复位的字段；`current_task_id`/`last_progress_step`/
        `resume_state`/`live_state`/`salvage_ctx`/`stale_alerted` 原先就不在此复位，
        由各自生命周期路径管理（收尾/中断/异常），不得顺手加进来。"""
        self.finished = False
        self.awaiting_human = False
        self.plan_only_mode = False
        self.summary = ""
        self.delegation_just_finished = False
        self.last_delegation_note = ""
        self.finish_open_intents_ack = False
        self.reject_streak = 0
        self.plan_gate_count = 0
        self.stuck_waves = 0
        self.stuck_extensions = 0
        self.cadence_last_rev = 0
        self.cadence_last_hint_step = 0
        self.cadence_hinted_findings = set()
        self.closing_round = 0
        self.closing_last_progress = 0
