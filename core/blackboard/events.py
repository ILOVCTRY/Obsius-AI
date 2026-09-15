"""事件总线（DESIGN.md §5.3 变更通知）。

写路径同步落库；订阅者回调广播。API 层（FastAPI）负责把回调桥接为
asyncio.Queue / WebSocket 推送——黑板层不感知传输。
订阅者异常不阻断黑板写路径（写操作是事实，通知是尽力而为）。
"""

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

Subscriber = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscriber] = []

    def subscribe(self, cb: Subscriber) -> Callable[[], None]:
        """注册订阅者，返回退订函数。"""
        self._subs.append(cb)

        def _unsubscribe() -> None:
            if cb in self._subs:
                self._subs.remove(cb)

        return _unsubscribe

    def publish(self, event: dict[str, Any]) -> None:
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception:  # noqa: BLE001 —— 通知失败不阻断写路径
                log.exception("事件订阅者回调异常（事件仍已落库）: kind=%s", event.get("kind"))
