"""对话轮的外部投递箱（chat-inbox，2026-10-20）。

**用途**：给**正在跑**的那一轮从外面塞一句话，让它在下一个步边界自己读进去。

**为什么不复用 `abort_event`**：两者语义相反。`abort_event` 是「立刻停，别干了」
（`_persist_stopped` 落「已按人类要求停止」，不给模型任何收尾机会）；本箱是
「情况变了，你自己决定怎么收尾」。前者用在人点停止、项目删除；后者用在
「题目已解出，别再派新方向了，把手上东西写回并总结」。

**为什么需要跨实例通道**：`ChatTurn` 是**每轮新建**的（`app.py::_chat_launch_turn`
里 `ChatTurn(...)`），线程在跑时那一轮的实例只活在它的后台线程里，外面拿不到
引用。所以要有一个按 `thread_id` 寻址、线程安全的共享队列，由本模块持有，
两边都按 tid 取。

**为什么不用 `_inject_nudge` 直接改 messages**：messages 列表是那一轮实例的局部
变量，外部没有引用。唯一能跨实例传递的只有「按 tid 索引的容器」。

**为什么登记表在模块级而不是 `app.state`**（2026-10-20 修正，用户实测）：
`ChatTurn` 有**三个**构造点——`app._chat_launch_turn`（顶层轮次）、
`runtime._tool_call_expert`（spawn 的子专家）、`artex_worker.make_worker`
（ARTEX Worker）。后两个在 `core/chat/` 里跑，**拿不到 `app.state`**。登记表放
在 app 上时，子专家与 Worker 只能拿到 `notice_inbox=None`，于是「人工确认 flag
后通知所有在跑轮次」这条链**只覆盖主控自己**——子 agent 照旧跑满全程（用户看到
的就是「点了确认，其它子 agent 还在跑」）。

模块级登记表还有一个附带好处：**箱子存在 = 有一轮正持有它**。`ChatTurn` 在构造
时取箱、`run()` 收尾时释放，所以「有箱」本身就是比 `app.state.chat_running`
更准的存活信号——那个集合只登记顶层轮次，用它当判据会静默丢掉全部子线程。

实现刻意做成**无阻塞、无上限风险**：投递方（HTTP 请求线程）不该因为对端不读而
卡住；同一轮最多投几条，条数上限只是防御性护栏。
"""

from __future__ import annotations

import threading

# 单轮最多囤积的通知条数。正常场景一轮最多收到「题目已解出」一条；上限只是
# 防御「有人循环投递把内存撑爆」，到顶就丢最旧的（新的通常更相关）。
_MAX_PENDING = 16


class NoticeInbox:
    """按线程寻址的一次性消息队列（投递 → 下一个步边界取走）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[str] = []

    def post(self, text: str) -> bool:
        """投递一条通知。返回 False 表示文本为空或已到上限被丢弃。"""
        body = str(text or "").strip()
        if not body:
            return False
        with self._lock:
            if len(self._items) >= _MAX_PENDING:
                self._items.pop(0)      # 丢最旧的：新通知更贴近当前事实
            self._items.append(body)
        return True

    def drain(self) -> list[str]:
        """取走全部待读通知（**取走即清**，同一句不会被注入两遍）。"""
        with self._lock:
            items, self._items = self._items, []
        return items

    def pending(self) -> int:
        with self._lock:
            return len(self._items)


# ---------- 进程级登记表（按 thread_id 寻址） ----------
#
# 与 `NoticeInbox` 分开放：箱子是数据，登记表是寻址。三个构造点都只需
# `box_for(thread_id)`，不必知道对方是主控、子专家还是 ARTEX Worker。

_registry_lock = threading.Lock()
_registry: dict[str, NoticeInbox] = {}


def box_for(thread_id: str) -> NoticeInbox | None:
    """取该线程的箱，没有就建一个。``thread_id`` 为空返回 None。

    **建箱 = 宣布「这一轮要读通知」**。`ChatTurn` 在构造时调用，`run()` 收尾
    时用 `drop_box()` 释放——所以「箱还在」等价于「这一轮还在跑」。
    """
    tid = str(thread_id or "").strip()
    if not tid:
        return None
    with _registry_lock:
        box = _registry.get(tid)
        if box is None:
            box = NoticeInbox()
            _registry[tid] = box
        return box


def box_of(thread_id: str) -> NoticeInbox | None:
    """只查不建（投递方用：对端没在跑就别给它攒垃圾）。"""
    tid = str(thread_id or "").strip()
    if not tid:
        return None
    with _registry_lock:
        return _registry.get(tid)


def drop_box(thread_id: str) -> None:
    """释放该线程的箱（轮次收尾）。幂等。"""
    tid = str(thread_id or "").strip()
    if not tid:
        return
    with _registry_lock:
        _registry.pop(tid, None)


def live_thread_ids() -> set[str]:
    """当前**有轮次持有箱**的线程 id 全集（= 真的在跑的轮次）。"""
    with _registry_lock:
        return set(_registry)


def post(thread_id: str, text: str) -> bool:
    """向该线程投递一条通知。对端没在跑（无箱）→ 返回 False，不攒。

    **为什么无箱不建箱**：箱是「这一轮要读」的声明，由轮次自己建。投递方替
    对端建箱，会留下一条永不读取的通知——下一轮若沿用同一个 tid，这条旧通知
    会在**无关的新一轮**里被注入（「这题已解出」出现在一个刚问完别的事情的
    轮次里），比不投更糟。
    """
    box = box_of(thread_id)
    if box is None:
        return False
    return box.post(text)
