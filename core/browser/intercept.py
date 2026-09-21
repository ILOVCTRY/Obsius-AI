"""拦截枢纽（F6-v3，DESIGN.md §7）：人工页面的请求/响应挂起与裁决。

线程模型红线：
- pending 表 / 开关由 ``threading.Lock`` 保护——API 线程与 loop 线程都可触碰；
- **asyncio.Future 只能在实例 loop 线程 set_result**——API 线程的
  toggle/decide/cancel_all 一律经 ``inst._loop.call_soon_threadsafe`` 转交
  （实例已停 → 静默放弃，route 协程随 loop 一起消亡）；
- route 协程（loop 线程）用 ``asyncio.wait_for(hold.future, timeout)`` 等裁决，
  超时视作放行原文。

范围红线：只挂人工隐式会话（human-main）的流量——判定在 capture._on_route，
本模块不感知 sid。挂起上限每方向 MAX_PENDING=50，溢出自动放行原文（宁通勿卡）。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field

__all__ = ["InterceptHub", "PendingHold"]


@dataclass
class PendingHold:
    """一个被拦下的请求/响应。route 对象不进字段——由 capture 侧闭包持有，
    仅 loop 线程触碰。"""
    direction: str                       # "request" | "response"
    method: str
    url: str
    status: int | None                   # response 向才有
    req_headers: dict = field(default_factory=dict)
    req_body: bytes | None = None
    resp_headers: dict | None = None
    resp_body: bytes | None = None
    resp_mime: str = ""
    raw: str = ""                        # 后端渲染好的完整报文文本
    editable: bool = True                # False=二进制 body，只可放行/丢弃
    hold_id: str = field(default_factory=lambda: f"ih-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    timeout_s: float = 120.0
    future: object = None                # asyncio.Future（loop 线程创建）


class InterceptHub:
    """挂在 BrowserInstance 上（inst._intercept）。"""

    MAX_PENDING = 50

    def __init__(self, instance, timeout_s: float = 120.0):
        self._inst = instance
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self.req_enabled = False
        self.resp_enabled = False
        self.pending: dict[str, PendingHold] = {}

    # ---- loop 线程（capture route 协程） ----

    def new_hold(self, **kw) -> PendingHold:
        """组一个 hold（loop 线程调用，创建 Future）。"""
        hold = PendingHold(timeout_s=self._timeout_s, **kw)
        hold.future = asyncio.get_running_loop().create_future()
        return hold

    def try_register(self, hold: PendingHold) -> bool:
        """登记挂起；满/关开关返回 False（调用方走放行原文路径）。"""
        enabled = self.req_enabled if hold.direction == "request" else self.resp_enabled
        if not enabled:
            return False
        with self._lock:
            if len(self.pending) >= self.MAX_PENDING:
                return False  # 溢出自动放行原文（宁通勿卡）
            self.pending[hold.hold_id] = hold
        return True

    def discard(self, hold_id: str) -> None:
        """裁决/超时后的兜底清理（幂等，不 resolve）。"""
        with self._lock:
            self.pending.pop(hold_id, None)

    # ---- API 线程 ----

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            items = sorted(self.pending.values(), key=lambda h: h.created_at)
            pending = [{
                "hold_id": h.hold_id, "direction": h.direction,
                "method": h.method, "url": h.url, "status": h.status,
                "raw": h.raw, "editable": h.editable,
                "is_binary": not h.editable,
                "size": len(h.req_body or h.resp_body or b""),
                "created_at": h.created_at,
                "expires_in_ms": max(0, int((h.created_at + h.timeout_s - now) * 1000)),
            } for h in items]
            return {"request_enabled": self.req_enabled,
                    "response_enabled": self.resp_enabled,
                    "pending": pending}

    def toggle(self, direction: str, enabled: bool) -> None:
        with self._lock:
            if direction == "request":
                self.req_enabled = enabled
            else:
                self.resp_enabled = enabled
            if not enabled:  # 关开关：该方向全部挂起包自动放行原文
                to_release = [h for h in self.pending.values()
                              if h.direction == direction]
                for h in to_release:
                    self.pending.pop(h.hold_id, None)
        if not enabled:
            for h in to_release:
                self._resolve(h, ("forward", None))

    def decide(self, hold_id: str, action: str,
               mods: dict | None = None) -> PendingHold:
        """裁决一个挂起包。不存在/已裁决/已超时 KeyError（API 层 404）；
        超时的包已被 route 协程 discard，同样 KeyError。"""
        with self._lock:
            hold = self.pending.pop(hold_id, None)
        if hold is None:
            raise KeyError(hold_id)
        result = ("drop", None) if action == "drop" else ("forward", mods)
        self._resolve(hold, result)
        return hold

    def cancel_all(self, reason: str = "") -> None:
        """会话关闭/实例停机：未裁决包统一放行原文（route 失效由 capture 吞）。"""
        with self._lock:
            holds = list(self.pending.values())
            self.pending.clear()
        for h in holds:
            self._resolve(h, ("forward", None))

    # ---- 跨线程 resolve（内部） ----

    def _resolve(self, hold: PendingHold, result: tuple) -> None:
        loop = getattr(self._inst, "_loop", None)

        def _set() -> None:
            fut = hold.future
            if fut is not None and not fut.done():
                fut.set_result(result)

        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(_set)
        except RuntimeError:  # 实例已停：route 协程已消亡，无需 resolve
            pass
