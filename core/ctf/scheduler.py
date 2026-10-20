"""放弃决策（借鉴 hxbai `engine/scheduler.py::_revive_policy`，2026-10-19）。

**要解决的问题**：CTF 里最难的不是「怎么解题」，而是「**什么时候别解了**」。
一个方向卡住时，继续投时间还是换方向？这决定整场比赛的得分上限。当前实现
把这件事交给模型的直觉——结果是两种坏结局：

1. **死磕**：对着一个已探尽的方向反复试，直到比赛结束；
2. **早弃**：一次快速检查没命中就放弃，而那道题再挖十分钟就能出。

hxbai 的解法是把这个判断**从模型手里拿走**，变成一组可枚举、可解释的判据。
本模块就是那组判据的移植——每次调用返回 ``(动作, 新状态, 理由字符串)``，
**理由是一句人话**（``zero-flag hungry yield`` / ``partial deepchain hard cap``
/ ``hint budget exhausted``），事后能复盘「为什么这题被丢了」。

**为什么不直接搬 hxbai 的 Scheduler 类**：它的 Scheduler 是进程级调度器，
管着线程池、并发槽位、任务队列——那与我们的 Team/Run/黑板三层直接冲突。
可搬的是**决策函数本身**：给定「跑了多久、拿了几分、试了几轮、用了多少提示」，
输出「该复活 / 该让位 / 该放弃」。调度动作仍由我们的会话层执行。

**判据优先级**（严格自上而下，先命中先返回）：

| 序 | 判据 | 动作 | 治什么 |
|---|------|------|--------|
| 1 | 轮次用尽（≥ max_passes） | drop | 无限重试 |
| 2 | 有产出但提示预算耗尽（≥1/3 预算） | drop | 靠提示续命的假进展 |
| 3 | 有产出但撞单题硬上限 | drop | 深链题无限深入 |
| 4 | 零产出 + 有题未开 + 超 hungry 额度 | **requeue** | 机会成本（让位给没开的题） |
| 5 | 零产出 + 撞零产出绝对上限 | drop | 一小时无 foothold 的磨洋工 |
| 6 | 零产出 + 无新题 + 超预算 | drop | 空转 |
| 7 | 有产出但 flag 停滞 + 超预算 | drop | 里程碑灌水的假进展 |
| 8 | 有里程碑进展 | **revive** | 真进展必须续投 |
| 9 | 未到最少轮次 | revive | 别过早判死 |
| 10 | 多 flag 题拿到部分 | revive | 多旗题要拿满 |
| 11 | 停滞超预算 | drop | α≈0 |
| 12 | 有产出/有弱进展且耐心未耗尽 | revive（有界） | 给一次机会 |
| 13 | 兜底 | drop | α≈0 |

**「可解释」是本模块的核心价值**：第 4 条 ``requeue`` 与第 5 条 ``drop``
的区别不是阈值高低，而是**语义不同**——前者是「这题还有救，但先让别人上」，
后者是「这题没救」。前端/日志据此能区分「暂时让位」和「判定死亡」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.ctf.pacing import Pacing

__all__ = [
    "Verdict",
    "ReviveInput",
    "decide",
    "ACTION_REVIVE",
    "ACTION_REQUEUE",
    "ACTION_DROP",
    "MIN_PASSES",
    "MAX_PASSES",
]

# 动作值域（与 hxbai 同名，便于对照阅读）
ACTION_REVIVE = "revive"      # 继续投入
ACTION_REQUEUE = "requeue"    # 让出槽位、回队尾（还有救，但先让别人上）
ACTION_DROP = "drop"          # 判定死亡，不再派

# 轮次边界：少于 MIN_PASSES 不许判死（避免一次快速检查就放弃）；
# 超过 MAX_PASSES 一律放弃（防止无限重试烧钱）
MIN_PASSES = 2
MAX_PASSES = 8

# 提示预算占比：有产出但把 1/3 预算耗在提示上 → 说明是自己挖不动靠提示续命
HINT_BUDGET_FACTOR = 1.0 / 3

# 零产出的两个绝对上限（秒，不随赛制缩放——「一小时没 foothold」在任何赛制下
# 都是磨洋工，长赛也不该给更多）
ZERO_FLAG_CAP_S = 60 * 60
# 零产出但还有题没开时，更早让位（机会成本）
ZERO_FLAG_HUNGRY_S = 30 * 60


@dataclass(frozen=True)
class Verdict:
    """一次放弃决策的结果。

    ``action`` 三选一；``reason`` 是**给人看的**一句理由（进日志/前端/复盘），
    ``code`` 是稳定机器标识（进事件、便于统计哪条判据最常命中）。
    """

    action: str
    reason: str
    code: str

    @property
    def should_continue(self) -> bool:
        return self.action == ACTION_REVIVE

    @property
    def frees_slot(self) -> bool:
        """是否让出并发槽位（requeue 与 drop 都让出，但语义不同）。"""
        return self.action in (ACTION_REQUEUE, ACTION_DROP)


@dataclass
class ReviveInput:
    """决策所需的全部事实（纯数据，无 IO）。

    **所有时间单位都是秒**。``*_elapsed`` 是「自某个基准点起」的墙钟耗时，
    不是「总耗时」——hxbai 用它们区分「这道题跑了多久」与「这场比赛跑了多久」。
    """

    # 本题已尝试轮次（一次「派给 worker 跑完」= 1 轮）
    passes: int = 0
    # 里程碑进展：已核验的能力/发现数（>0 表示真有产出）
    correct: int = 0
    # 本题声明的 flag 数（多 flag 题才 >1）
    need: int | None = None
    # 弱进展：侦察类产出（有信息但还没拿到实质突破）
    weak: bool = False
    # 弱进展被续投的次数（有界，防「一直有点小进展」无限续）
    weak_only: int = 0
    # **证伪**：本轮回执明确判定「这条路不通」（未命中/已排除/dead end）。
    # 与 weak 互斥——证伪是结论不是进展，命中即 drop（见判据 ⑧'）。
    refuted: bool = False
    # 本轮是否出现了新里程碑（与上轮比）
    milestone: bool = False
    # 自上次里程碑起停滞了多久
    time_stuck: float = 0.0
    # 本题总墙钟耗时（跨多轮累加）
    total_elapsed: float = 0.0
    # 自上次拿到 flag 起过了多久（多 flag 题用）
    flag_stuck: float = 0.0
    # 用了提示后过了多久（None = 没用过提示）
    hint_elapsed: float | None = None
    # 是否还有「没开过的题」在等槽位（机会成本判据）
    breadth_starved: bool = False
    # 停滞判定预算（通常取 Pacing.hard_total_cap_s() 或按题预算）
    stuck_budget: float = 0.0

    # 耐心参数（默认值与 hxbai 一致）
    weak_patience: int = 1
    correct_patience: int = 2


def decide(inp: ReviveInput, pacing: Pacing) -> Verdict:
    """给出这道题下一步该「继续 / 让位 / 放弃」，**并说明为什么**。

    纯函数：不读时间、不查库、不写状态。所有时间都由调用方从 ``ReviveInput``
    传入——这样同一组事实永远得到同一个判定（可测、可回放）。
    """
    budget = float(inp.stuck_budget or pacing.hard_total_cap_s())
    # 有产出时的绝对硬上限：至少给到「零产出上限 + 15 分钟」，否则多 flag 题
    # 拿到第一个 flag 后会被零产出上限误杀
    hard_s = max(pacing.hard_total_cap_s(), ZERO_FLAG_CAP_S + 15 * 60)
    hungry_s = min(pacing.hungry_cap_s(), ZERO_FLAG_CAP_S - 5 * 60)

    # ① 轮次用尽
    if inp.passes >= MAX_PASSES:
        return Verdict(ACTION_DROP,
                       f"已尝试 {inp.passes} 轮（上限 {MAX_PASSES}），不再续投",
                       "max_passes")

    # ② 有产出，但提示预算耗尽（≥1/3 预算）—— 靠提示续命的假进展
    if inp.correct > 0 and inp.hint_elapsed is not None:
        hint_floor = max(budget * HINT_BUDGET_FACTOR, pacing.hint_floor_s())
        if inp.hint_elapsed >= hint_floor and inp.passes >= MIN_PASSES:
            return Verdict(ACTION_DROP,
                           f"提示预算耗尽（距上次提示 {inp.hint_elapsed / 60:.0f} 分钟"
                           f" ≥ {hint_floor / 60:.0f} 分钟 = 预算的 1/3），让位",
                           "hint_budget_exhausted")

    # ③ 有产出，但撞单题硬上限（深链题无限深入）
    if inp.correct > 0 and inp.passes >= MIN_PASSES and inp.total_elapsed >= hard_s:
        return Verdict(ACTION_DROP,
                       f"深链题硬上限（本题累计 {inp.total_elapsed / 60:.0f} 分钟"
                       f" ≥ {pacing.hard_total_cap_s() / 60:.0f} 分钟），绝对让位",
                       "partial_hard_cap")

    # ④ 零产出 + 还有题没开 + 超 hungry 额度 → **让位回队尾**（不是判死！）
    if (inp.correct == 0 and inp.breadth_starved
            and inp.passes >= 1 and inp.total_elapsed >= hungry_s):
        return Verdict(ACTION_REQUEUE,
                       f"零产出 hungry 让位（{inp.total_elapsed / 60:.0f} 分钟未拿 flag，"
                       f"还有题没开；释放槽位回队尾，约 {hungry_s / 60:.0f} 分钟后再来）",
                       "zero_flag_hungry_yield")

    # ⑤ 零产出 + 撞零产出绝对上限
    if inp.correct == 0 and inp.passes >= 1 and inp.total_elapsed >= ZERO_FLAG_CAP_S:
        return Verdict(ACTION_DROP,
                       f"零产出绝对上限（累计 {inp.total_elapsed / 60:.0f} 分钟 0 flag，"
                       "一小时无 foothold 属于磨洋工）",
                       "zero_flag_cap")

    # ⑥ 零产出 + 无新题可开 + 超预算
    if (inp.correct == 0 and inp.breadth_starved
            and inp.passes >= MIN_PASSES and inp.total_elapsed >= budget):
        return Verdict(ACTION_DROP,
                       f"零交付空转（累计 {inp.total_elapsed / 60:.0f} 分钟 ≥ 预算 "
                       f"{budget / 60:.0f} 分钟，0 flag，且无新方向可开）",
                       "zero_delivery_churn")

    # ⑦ 有产出但 flag 停滞 + 无新方向 + 超预算（里程碑灌水免疫）
    if (inp.correct > 0 and inp.breadth_starved
            and inp.passes >= MIN_PASSES and inp.flag_stuck >= budget):
        return Verdict(ACTION_DROP,
                       f"部分深链 flag 停滞（距上次 flag {inp.flag_stuck / 60:.0f} 分钟"
                       f" ≥ 预算 {budget / 60:.0f} 分钟，且无新方向可开）",
                       "partial_flag_stall")

    # ⑧ 有里程碑进展 → 必须续投（真进展不该被杀）
    if inp.milestone:
        return Verdict(ACTION_REVIVE, "出现里程碑进展，续投", "milestone")

    # ⑧' **已证伪 → 直接放弃**（ctf-refute-converge，2026-10-20）。
    #
    # 位置必须在 ⑨ 的 min_passes 地板**之前**：证伪是确定性结论，不是「试得
    # 还不够」。放在地板之后就等于「未命中也要再试两轮」——那正是「证伪方向
    # 不收敛」的表现（用户复盘里三个方向反复重派同一测试面）。
    #
    # 也不能被 ⑫ 的 bounded_patience 救活：那里 `inp.weak` 为真就续投，而证伪
    # 回执**同时含「命中」二字**（`未命中`），旧口径必然误置 weak → 永远续投。
    # `session.record` 已改为证伪优先且不置 weak，此处再加一道硬判据双保险。
    #
    # 唯一例外：**已拿到部分 flag 的多 flag 题**（correct > 0 且未满 need）——
    # 那说明这道题整体还有戏，某个方向证伪不等于整题放弃，交给 ⑩ 判。
    if inp.refuted and inp.correct == 0:
        return Verdict(ACTION_DROP,
                       "该方向已证伪（本轮明确判定不成立/未命中），不再重派"
                       "——换个测试面，别在同一处反复试",
                       "refuted")

    # ⑨ 未到最少轮次 → 续投
    if inp.passes < MIN_PASSES:
        return Verdict(ACTION_REVIVE,
                       f"仅尝试 {inp.passes} 轮（少于 {MIN_PASSES} 轮不判死）",
                       "min_passes_floor")

    # ⑩ 多 flag 题拿到部分 → 续投（多旗题要拿满）
    if (inp.need is not None and 0 < inp.correct < inp.need and inp.weak
            and not (inp.breadth_starved and inp.time_stuck >= budget)):
        return Verdict(ACTION_REVIVE,
                       f"多 flag 题部分交付（{inp.correct}/{inp.need}），"
                       "续投拿满剩下的",
                       "partial_multiflag")

    # ⑪ 停滞超预算 → α≈0
    if inp.time_stuck >= budget:
        who = "部分深链被驱逐" if inp.correct > 0 else "零交付"
        return Verdict(ACTION_DROP,
                       f"α≈0 停滞（距上次里程碑 {inp.time_stuck / 60:.0f} 分钟"
                       f" ≥ 预算 {budget / 60:.0f} 分钟，{who}）",
                       "alpha_zero_stuck")

    # ⑫ 有产出/有弱进展且耐心未耗尽 → 有界续投
    patience = inp.correct_patience if inp.correct > 0 else inp.weak_patience
    if (inp.correct > 0 or inp.weak) and inp.weak_only < patience:
        reason = ("部分交付但无新里程碑（有界续投）" if inp.correct > 0
                  else "有界侦察进展")
        return Verdict(ACTION_REVIVE, reason, "bounded_patience")

    # ⑬ 兜底：α≈0
    if inp.correct > 0:
        reason = "α≈0 停滞（部分交付已保留，耐心耗尽）"
    elif inp.weak:
        reason = "α≈0 且无里程碑（仅侦察）"
    else:
        reason = "α≈0 无进展"
    return Verdict(ACTION_DROP, reason, "alpha_zero_default")
