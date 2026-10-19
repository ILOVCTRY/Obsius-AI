"""时长无关的预算缩放（借鉴 hxbai `engine/pacing.py`，2026-10-19）。

**要解决的问题**：项目里所有「这道题再花多久就该放弃」的时间阈值都是写死的
（例如「单题上限 45 分钟」）。但同一个阈值在 2 小时短赛和 48 小时长赛里含义
完全相反：

- 短赛里 45 分钟是**大半场比赛**——不敢用，题做不完；
- 长赛里 45 分钟是**零头**——过早放弃，明明还能挖。

hxbai 的解法是把所有阈值乘以一个**缩放系数**：

    scale = (本场时长 / 锚定时长 6h) × (锚定题量 63 / 本题量)

再 clamp 到 [0.25, 3.0]。于是同一套「45 分钟上限」在短赛自动变成 ~11 分钟、
长赛自动变成 ~2 小时，阈值表不用改一个字。

**本项目的扩展**：

- 显式 ``total_s`` / ``n_tasks``（hxbai 用全局单例 + 时长阶梯猜），
  并保留 ``infer_session_len`` 处理「不知道比赛多久」的情形；
- ``Pacing.from_deadline()``：CTF 常给截止时间戳（平台 API / 赛题页），
  比「猜总时长」准；
- ``remaining_s`` / ``in_endgame`` 给出**收尾窗口**：最后 10% 时长（最少 45
  分钟）进入 endgame，此时应停止开新战线，只做「能交的题收尾」。

**为什么是纯计算、不带全局状态**：本项目是多会话多题并发的，一个进程级
单例会让 A 题的时间推进影响 B 题的预算。故本模块只提供 ``Pacing`` 值对象，
由调用方（工具层/会话上下文）持有实例。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Pacing",
    "ANCHOR_SESSION_S",
    "ANCHOR_NTASKS",
    "SESSION_LADDER",
    "SCALE_MIN",
    "SCALE_MAX",
    "BASE_BUDGETS",
    "infer_session_len",
    "scale_for",
    "normalize_ctf_config",
]

# 锚点：hxbai 的经验值——6 小时 / 63 题时 scale == 1.0
ANCHOR_SESSION_S = 6 * 3600
ANCHOR_NTASKS = 63
# 不知道赛制时长时，按已用时长向上猜（跑过 6h 就按 8h 赛算）
SESSION_LADDER = (6 * 3600, 8 * 3600, 10 * 3600)
SCALE_MIN, SCALE_MAX = 0.25, 3.0

# 基准预算表（秒）——hxbai 原值，scale==1.0 时即当前行为
BASE_BUDGETS: dict[str, float] = {
    "hard_total": 150 * 60,      # 单题总时长硬上限
    "hungry": 30 * 60,           # 「有进展」时的续投额度
    "golden_park": 20 * 60,      # 高价值线索的挂起等待
    "hint_floor": 25 * 60,       # 低于此时长不给提示（防过早求提示）
    "max_attempt": 45 * 60,      # 单次尝试上限
    "hi_window": 40 * 60,        # 高价值窗口下限
    "drought_work": 35 * 60,     # 连续无产出后仍应工作多久
    "hint_wall_fuse": 10 * 60,   # 提示后的熔断
}


def infer_session_len(elapsed_s: float) -> int:
    """已用时长 → 推断的本场总时长（阶梯上取第一个不小于已用时的档）。"""
    elapsed = max(0.0, float(elapsed_s or 0.0))
    for step in SESSION_LADDER:
        if elapsed < step:
            return step
    return SESSION_LADDER[-1]


def scale_for(session_len_s: float, n_tasks: int) -> float:
    """核心缩放：时长越长、题量越少 → 系数越大。clamp 到 [0.25, 3.0]。"""
    sl = max(1.0, float(session_len_s or ANCHOR_SESSION_S))
    nt = max(1, int(n_tasks or ANCHOR_NTASKS))
    s = (sl / ANCHOR_SESSION_S) * (ANCHOR_NTASKS / nt)
    return max(SCALE_MIN, min(SCALE_MAX, s))


@dataclass
class Pacing:
    """一题（或一场）的预算标尺。线程内使用，不跨题共享。

    ``total_s`` 未知时传 None，走 ``infer_session_len`` 阶梯推断；
    ``started_at`` 用 ``time.time()`` 口径，便于直接和 ``time.time()`` 比较。
    """

    total_s: float | None = None
    n_tasks: int = ANCHOR_NTASKS
    started_at: float | None = None
    # 显式传入的缩放（优先级最高，便于测试与「按题给预算」的场景）
    scale_override: float | None = None

    # ---- 构造 ----

    @classmethod
    def from_deadline(cls, deadline_ts: float, n_tasks: int = ANCHOR_NTASKS,
                      started_at: float | None = None) -> "Pacing":
        """按截止时间戳构造（平台赛题页一般会给结束时间）。"""
        now = time.time()
        total = max(60.0, float(deadline_ts) - now)
        return cls(total_s=total, n_tasks=n_tasks, started_at=started_at or now)

    # ---- 时间 ----

    def elapsed_s(self) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, time.time() - self.started_at)

    def session_len_s(self) -> int:
        if self.total_s:
            return int(self.total_s)
        return infer_session_len(self.elapsed_s())

    def remaining_s(self) -> float:
        return max(0.0, self.session_len_s() - self.elapsed_s())

    def endgame_window_s(self) -> int:
        """收尾窗口：总时长的 10%，但不小于 45 分钟。"""
        return max(int(0.1 * self.session_len_s()), 45 * 60)

    def in_endgame(self) -> bool:
        return self.remaining_s() <= self.endgame_window_s()

    # ---- 缩放 ----

    def scale(self) -> float:
        if self.scale_override:
            return max(SCALE_MIN, min(SCALE_MAX, float(self.scale_override)))
        return scale_for(self.session_len_s(), self.n_tasks)

    # ---- 预算 ----

    def budget(self, name: str) -> int:
        """按名字取缩放后的预算（秒）；未知名字抛 KeyError（早暴露笔误）。"""
        base = BASE_BUDGETS[name]
        return int(base * self.scale())

    def hard_total_cap_s(self) -> int:
        return self.budget("hard_total")

    def hungry_cap_s(self) -> int:
        """续投额度：缩放后再夹在 [15min, 35min]，防长赛无限续投。"""
        return int(min(max(30 * 60 * self.scale(), 15 * 60), 35 * 60))

    def golden_park_wall_s(self) -> int:
        return self.budget("golden_park")

    def hint_floor_s(self) -> int:
        return self.budget("hint_floor")

    def max_attempt_cap_s(self) -> int:
        return self.budget("max_attempt")

    def hi_window_floor_s(self) -> int:
        return self.budget("hi_window")

    def drought_work_s(self) -> int:
        return self.budget("drought_work")

    def hint_wall_fuse_s(self) -> int:
        return self.budget("hint_wall_fuse")

    # ---- 展示 ----

    def describe(self) -> str:
        """一行摘要（进提示词/日志用）：`scale=1.00 总时长=6.0h 题量=63 剩余=2.1h`。"""
        return (f"scale={self.scale():.2f} "
                f"总时长={self.session_len_s() / 3600:.1f}h "
                f"题量={self.n_tasks} "
                f"剩余={self.remaining_s() / 3600:.1f}h"
                f"{' [收尾窗口]' if self.in_endgame() else ''}")


# ---------------- 配置归一化 ----------------

def normalize_ctf_config(raw: Any) -> dict:
    """归一化 ``config["ctf"]``；非法抛 ValueError（API→422），空段返 {}。

    此前 `config.ctf` 只有读路径（`ChatTurn._pacing_context`），写路径靠手改
    project.json——前端「赛制时间」面板落地后必须有同一道闸，否则
    ``{"tasks": "abc"}`` 这类值会静默进盘、到读路径才被 `int()` 吞成 0
    （等于悄悄回退到锚定题量，人看不出配置没生效）。

    口径：
    - ``deadline``：非空字符串/正整数才留（ISO8601 或 epoch 秒，解析在
      `core.chat.runtime._to_epoch`）；空串/None 剥键。
    - ``tasks``：正整数才留（``"63"`` 这类数字串容忍，其它拒）；空剥键。
    - 两项都空 → 返回 {}（调用方剥键，回落到阶梯推断）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("ctf 必须是对象（{deadline, tasks}）")
    out: dict = {}
    dl = raw.get("deadline")
    # 纯空白串等同「没填」（前端清空后可能留空格），不能当非法值拒
    if isinstance(dl, str):
        if dl.strip():
            out["deadline"] = dl.strip()
    elif isinstance(dl, (int, float)) and not isinstance(dl, bool) and dl > 0:
        out["deadline"] = float(dl)
    elif dl not in (None, ""):
        raise ValueError("ctf.deadline 须为 ISO8601 字符串或 epoch 秒（正数）")
    n = raw.get("tasks")
    if isinstance(n, bool):
        raise ValueError("ctf.tasks 须为正整数")
    if isinstance(n, (int, float)):
        if int(n) != n or int(n) <= 0:
            raise ValueError("ctf.tasks 须为正整数")
        out["tasks"] = int(n)
    elif isinstance(n, str):
        if n.strip():
            if not n.strip().isdigit() or int(n.strip()) <= 0:
                raise ValueError("ctf.tasks 须为正整数")
            out["tasks"] = int(n.strip())
    elif n not in (None, ""):
        raise ValueError("ctf.tasks 须为正整数")
    return out
