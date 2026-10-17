"""飞书推送 worker（feishu-push v1，docs/plans/feishu-connector.md）。

单守护线程串行消费 task 队列：天然限流（按 rate_per_min 节流）、失败重试
（指数退避）、连续失败熔断（degraded，停止发送直到测试端点成功复位）。
sender 可注入（测试 fake 零触网）；异常绝不逃逸线程（只记账+记日志）。
"""

import logging
import queue
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

MAX_QUEUE = 200          # 队列上限，溢出丢弃并记账（宁丢不堵）
FAIL_LIMIT = 5           # 连续失败上限 → 熔断
BACKOFF_BASE = 1.0       # 重试退避基数（秒）
BACKOFF_CAP = 30.0       # 退避封顶（秒）
RECENT_KEEP = 20         # 最近推送记录保留条数


class PushWorker:
    """sender(task)：可调用对象，负责真正发送（默认解包 task 里的 client/card）。"""

    def __init__(self, sender, *, max_queue: int = MAX_QUEUE,
                 rate_per_min: int = 20, retry: int = 3):
        self._sender = sender
        self._retry = max(0, int(retry))
        self._min_interval = 60.0 / max(1, int(rate_per_min))
        self.rate_per_min = int(rate_per_min)
        self.retry_attempts = int(retry)
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._dropped = 0
        self._sent = 0
        self._failed = 0
        self._consecutive_failures = 0
        self._degraded = False
        self._last_send_at = 0.0
        self._recent: deque = deque(maxlen=RECENT_KEEP)

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="feishu-push")
        self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 投递 ----

    def submit(self, task: dict) -> bool:
        """非阻塞入队；未启动/熔断中/队列满 → 丢弃返回 False。"""
        with self._lock:
            if self._degraded:
                self._dropped += 1
                return False
        if not self.running:
            with self._lock:
                self._dropped += 1
            return False
        try:
            self._q.put_nowait(task)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            log.warning("飞书推送队列已满（%s），丢弃一条: %s",
                        self._q.maxsize, (task.get("meta") or {}).get("title"))
            return False

    def reset(self) -> None:
        """人工复位（测试端点成功后调用）：清熔断 + 连败计数。"""
        with self._lock:
            self._degraded = False
            self._consecutive_failures = 0

    # ---- 主循环 ----

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._rate_limit()
            if self._stop.is_set():
                break
            self._deliver(task)

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_send_at
        remain = self._min_interval - elapsed
        if remain > 0:
            self._stop.wait(remain)

    def _deliver(self, task: dict) -> None:
        meta = task.get("meta") or {}
        last_err: Exception | None = None
        for attempt in range(self._retry + 1):
            if self._stop.is_set():
                return
            if attempt:
                delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
                if self._stop.wait(delay):
                    return
            try:
                self._sender(task)
                self._on_success(meta)
                return
            except Exception as e:  # noqa: BLE001 —— 任何异常只记账不逃逸
                last_err = e
                if not self._is_retryable(e):
                    break
        self._on_failure(meta, last_err)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """可重试判定：配置/签名类错误（webhook 缺失、签名失败、HTTP 4xx）
        重试无益，立即判死；网络/5xx/限流进重试预算。"""
        from core.integrations.feishu import (FeishuAuthError,
                                              FeishuConfigError)
        return not isinstance(exc, (FeishuConfigError, FeishuAuthError))

    def _on_success(self, meta: dict) -> None:
        with self._lock:
            self._sent += 1
            self._consecutive_failures = 0
            self._degraded = False
            self._last_send_at = time.monotonic()
            self._recent.appendleft({"ts": _now_iso(), "ok": True,
                                     "detail": "", **meta})

    def _on_failure(self, meta: dict, err: Exception | None) -> None:
        detail = str(err)[:200] if err else "unknown"
        with self._lock:
            self._failed += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= FAIL_LIMIT:
                self._degraded = True
                log.error("飞书推送连续失败 %d 次，已熔断挂起（修复后用「测试连接」复位）",
                          self._consecutive_failures)
            self._recent.appendleft({"ts": _now_iso(), "ok": False,
                                     "detail": detail, **meta})
        log.warning("飞书推送失败: %s", detail)

    # ---- 状态 ----

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "queue_depth": self._q.qsize(),
                "queue_cap": self._q.maxsize,
                "sent": self._sent,
                "failed": self._failed,
                "dropped": self._dropped,
                "consecutive_failures": self._consecutive_failures,
                "degraded": self._degraded,
                "recent": list(self._recent),
            }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
