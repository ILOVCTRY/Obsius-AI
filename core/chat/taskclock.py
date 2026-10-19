"""任务级 deadline 夹逼（task clock）。

对应上游 ``ARTEX-main/agent/taskclock.go``：

    // TaskClock carries a task's absolute deadline into a worker/planner run so the
    // run can clamp its own wall-clock budget to the task's remaining time and pick
    // the right wrap-up words (per-run vs task-timeout).

以及 ``server/engine_timeout.go::clockCtx``：Engine 在每次 run 的 ctx 上挂
``TaskClock{DeadlineUnix, Final}``，worker/planner 据此：

1. **夹逼自己的墙钟预算**（``clampMaxDuration``）：本次 run 最多只能跑到
   任务剩余时间——任务只剩 2 分钟时，给 run 配 10 分钟预算毫无意义，到点
   被外面硬砍的代价是「连收尾都来不及做」。
2. **按实际 reason 挑收尾词**：``clamped=true`` 且因**超时**收尾 = 整个任务
   到点了（任务超时词）；``clamped=true`` 但因**步数**先耗尽 = 任务还剩时间
   （per-run 词）。上游原文——

       // 与 per-run 收尾词是【两套】：per-run 是"你这一次 run 的预算用完了"；
       // 任务超时是 "整个任务到点、即将结束"。语义常相反（尤其 planner：
       // per-run 说"别停继续规划"，任务超时说"到点停止规划、做最后判定"）。

为什么必须有这一步：没有夹逼时，任务到点由 Engine 从外面 ``future.cancel()``
硬砍——Worker 连一句总结都来不及写，收尾阶段（settlement）根本进不去，
「已识别未写回」的观察照样蒸发。夹逼把「硬砍」变成「自己知道要到点、主动
收尾」，这是上游整套任务级超时设计的地基。

本模块只放**纯策略**（无 IO、无时钟状态）：给定 deadline 与自有预算算出
有效预算与是否被夹逼。谁提供 deadline（Engine 从任务根 Intent 的
created_at + task_deadline_s 推出）由调用方决定。
"""
from __future__ import annotations

import time

# 任务 deadline 缺失/非法时的语义值：0 = 不限（行为与引入本模块前完全一致）。
NO_DEADLINE = 0.0

# 剩余时间下限（上游 ``if remaining < time.Second { remaining = time.Second }``）：
# 绝不返回 ≤0 的预算——调用方把 0 读作「不限」，那正好与「已经到点了」相反。
MIN_REMAINING_S = 1.0


def remaining_seconds(deadline_at: float, *, now: float | None = None) -> float:
    """距 deadline 的剩余秒数；无 deadline 返回 0（= 不限）。"""
    try:
        dl = float(deadline_at or 0.0)
    except (TypeError, ValueError):
        return NO_DEADLINE
    if dl <= 0:
        return NO_DEADLINE
    cur = time.time() if now is None else float(now)
    return max(MIN_REMAINING_S, dl - cur)


def clamp_max_duration(deadline_at: float,
                       own_budget: float, *,
                       now: float | None = None) -> tuple[float, bool]:
    """把任务 deadline 折进本次 run 的墙钟预算。

    返回 ``(eff, clamped)``：

    * ``own_budget`` = 该 run 自己的墙钟预算（0 = 不限）；
    * ``eff`` = 实际该用的预算（有 deadline 时下限 1s，绝不返回 ≤0——上游
      注释：harness 把 0 读作 unlimited）；
    * ``clamped`` = **任务 deadline 是否是这次 run 的约束**（剩余 ≤ 自有预算，
      或自有预算不限）。没有 deadline 时原样返回 ``(own_budget, False)``。

    注意 ``clamped`` 与 ``eff`` 是两个正交的信息：``eff`` 决定「跑多久」，
    ``clamped`` 决定「收尾时说哪套话」。任务还剩 3 分钟、自有预算 5 分钟时
    ``eff=180s, clamped=True``——到点收尾就是**任务到点**，不是 run 预算耗尽。
    """
    try:
        dl = float(deadline_at or 0.0)
    except (TypeError, ValueError):
        dl = NO_DEADLINE
    try:
        own = max(0.0, float(own_budget or 0.0))
    except (TypeError, ValueError):
        own = 0.0
    if dl <= 0:
        return own, False
    remaining = remaining_seconds(dl, now=now)
    clamped = own <= 0 or remaining <= own
    eff = own if (own > 0 and own < remaining) else remaining
    return eff, clamped
