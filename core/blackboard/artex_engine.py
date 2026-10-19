"""ARTEX Frontier/Engine 运行时。

Engine 只负责调度持久化 Intent；具体专家执行由 handler 注入，避免把 ChatTurn
和 Blackboard 调度层互相耦合。handler(intent) 应返回字符串或结果对象。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime
from typing import Callable, Any

from core.blackboard.artex_intents import (
    TERMINAL_STATUSES,
    challenge_gate,
    claim_next_intent,
    get_intent,
    list_intents,
    pause_task_pending,
    recover_stale_intents,
    transition_intent,
)

log = logging.getLogger(__name__)

# 收尾宽限（artex-taskclock，对齐上游 server/engine_timeout.go 的
# ``settleDrainGrace = 90 * time.Second``）：任务到点后，在跑的 Worker 因预算被
# 夹逼会**自己**停下来进收尾阶段（settlement，默认再跑 10 轮落库）。这段收尾
# 时间必须给，否则 Engine 会在 Worker 刚进收尾时就 future.cancel() 硬砍——
# 那等于把「夹逼→主动收尾」退化回「外面硬砍→什么都没捞回」，整套设计白做。
SETTLE_DRAIN_GRACE_S = 90.0


class ArtexEngine:
    """单项目常驻调度器：Frontier 原子领取，Worker 执行，失败可重试。

    ``max_workers`` 是**全局**并发上限（默认 15）。它与题目级上限
    （``artex.per_challenge_workers``，默认 5）是**两级约束**：全局必须显著
    大于单题上限，否则单题上限永远打不满、形同虚设。15 = 5 × 3 道并发题，
    留一档余量给非题目轨（渗透轨没有题目概念，直接吃全局额度）。
    """

    def __init__(self, bb, project_id: str, handler: Callable[[dict], Any], *,
                 worker_id: str = "artex-engine", poll_interval: float = 1.0,
                 timeout: float = 300.0, max_workers: int = 15,
                 auto_recover: bool = True, recover_after_seconds: float = 300,
                 max_attempts: int = 3,
                 task_deadline_s: float = 0.0, max_intents: int = 50,
                 settle_grace_s: float | None = None,
                 on_intent_finished: Callable[[dict, str], Any] | None = None,
                 settler: Callable[[str, str], Any] | None = None):
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        self.bb = bb
        self.project_id = project_id
        self.handler = handler
        self.worker_id = str(worker_id or "artex-engine")
        self.poll_interval = max(0.01, float(poll_interval))
        self.timeout = max(0.01, float(timeout))
        self.auto_recover = bool(auto_recover)
        self.recover_after_seconds = max(0.0, float(recover_after_seconds))
        self.max_attempts = max(1, int(max_attempts))
        # 任务级预算（0=不限）：超限时把该任务剩余 pending 置 paused，
        # 已运行 Worker 不动，交由人工决定恢复或收敛。
        self.task_deadline_s = max(0.0, float(task_deadline_s))
        self.max_intents = max(0, int(max_intents))
        # 收尾宽限（秒）：默认 **0**（= 硬超时就是预算，行为与引入本机制前一致）。
        # 由装配层（app.py）显式传 SETTLE_DRAIN_GRACE_S——只有真实 ChatTurn
        # Worker 会「撞预算后自己进收尾」，才需要这段额外窗口；Engine 独立使用
        # （裸 handler / 测试）传 0，否则 90s 宽限会让「超时」变成「永不超时」。
        self.settle_grace_s = (0.0 if settle_grace_s is None
                               else max(0.0, float(settle_grace_s)))
        self._limits_checked_at = 0.0
        # 任务绝对 deadline（unix 秒；0=不限）缓存，按 task_id。上游
        # ``Engine.taskDeadline`` 同款：进程内 map 优先，缺了再从任务根 Intent
        # 的 created_at + task_deadline_s 推出并回填。
        self._task_deadlines: dict[str, float] = {}
        # 每个在跑 Worker 的硬超时（秒）：有任务 deadline 时 = 自有预算 + 收尾宽限，
        # 保证「夹逼→主动收尾」那段落库窗口不被外面的 future.cancel() 砍掉。
        self._hard_timeout: dict[str, float] = {}
        # 已跑过终局规划轮的任务（幂等：deadline 每分钟都命中，只能收尾一次）。
        self._settled_tasks: set[str] = set()
        # 终局规划器：可选注入（Engine 不依赖 Planner 的 LLM）。签名
        # ``settler(task_id, reason) -> dict``；未注入时任务到点只暂停 pending、
        # 不跑终局判定（退化为引入前的行为，不报错）。
        if settler is not None and not callable(settler):
            raise TypeError("settler 必须可调用")
        self.settler = settler
        if on_intent_finished is not None and not callable(on_intent_finished):
            raise TypeError("on_intent_finished 必须可调用")
        self.on_intent_finished = on_intent_finished
        self.recovered: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._max_workers = max(1, int(max_workers))
        self._executor: ThreadPoolExecutor | None = None
        self._active: set[str] = set()
        self._futures: dict[str, tuple[dict, Future]] = {}
        self._started_at: dict[str, float] = {}
        self._controls: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _ensure_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix="artex-worker")
            return self._executor

    def start(self) -> None:
        if self.running:
            return
        if self.auto_recover:
            self.recovered = recover_stale_intents(
                self.bb, self.project_id,
                older_than_seconds=self.recover_after_seconds,
                max_attempts=self.max_attempts)
        self._ensure_executor()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="artex-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def active_workers(self) -> int:
        with self._lock:
            return len(self._futures)

    def drain(self, timeout: float = 5.0) -> dict:
        """停止领取新 Intent，并等待现有 Worker 协作式收尾。"""
        self._stop.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self.active_workers and time.monotonic() < deadline:
            self._collect()
            time.sleep(min(self.poll_interval, 0.05))
        self._collect()
        return {"drained": self.active_workers == 0, "active_workers": self.active_workers}

    def kill_work(self, intent_id: str, reason: str = "人工终止 Worker") -> dict:
        """请求单个运行中 Worker 终止；handler 需协作检查控制状态。"""
        iid = str(intent_id or "").strip()
        if not iid:
            raise ValueError("intent_id 必填")
        with self._lock:
            item = self._futures.get(iid)
            if item is None:
                raise LookupError(f"Worker 未运行: {iid}")
            self._controls.setdefault(iid, {})["kill"] = str(reason or "人工终止 Worker")[:500]
            item[1].cancel()
        self.bb.append_event(self.project_id, "artex.worker.kill", {
            "intent_id": iid, "reason": str(reason or "人工终止 Worker")[:500]
        })
        return get_intent(self.bb, self.project_id, iid) or {}

    def steer_work(self, intent_id: str, message: str) -> dict:
        """向运行中 Worker 排队一条方向调整指令。"""
        iid = str(intent_id or "").strip()
        text = str(message or "").strip()
        if not iid or not text:
            raise ValueError("intent_id 和 message 必填")
        with self._lock:
            if iid not in self._futures:
                raise LookupError(f"Worker 未运行: {iid}")
            self._controls.setdefault(iid, {}).setdefault("steer", []).append(text[:2000])
        self.bb.append_event(self.project_id, "artex.worker.steer", {
            "intent_id": iid, "message": text[:500]
        })
        # 方向调整同时沉淀为探索图 Hint：Worker 换了方向，这条判断依据要留痕。
        try:
            from core.blackboard.artex_exploration import record_hint
            row = get_intent(self.bb, self.project_id, iid) or {}
            record_hint(self.bb, self.project_id,
                        task_id=str(row.get("task_id") or ""),
                        intent_id=iid, text=text, author="human")
        except Exception:  # noqa: BLE001 —— 探索图写回失败不影响 steer
            log.exception("ARTEX Hint 写回失败: %s", iid)
        return get_intent(self.bb, self.project_id, iid) or {}

    def worker_control(self, intent_id: str) -> dict:
        with self._lock:
            state = dict(self._controls.get(str(intent_id), {}))
            state["running"] = str(intent_id) in self._futures
            state["steer"] = list(state.get("steer", []))
            return state

    def poll_worker_control(self, intent_id: str) -> dict:
        with self._lock:
            state = self._controls.get(str(intent_id), {})
            steer = list(state.get("steer", []))
            state["steer"] = []
            return {"kill": state.get("kill", ""), "steer": steer}

    def _task_deadline_at(self, task_id: str,
                          items: list[dict] | None = None) -> float:
        """任务绝对 deadline（unix 秒）；0 = 该任务无时限。

        上游 ``Engine.taskDeadline`` 的等价物：优先用进程内缓存（本次会话推过的），
        缺了再从任务**根 Intent 的 created_at + task_deadline_s** 推出并回填。
        用根 Intent 而不是「现在」作起点，是因为 deadline 是**任务绝对到点时间**：
        任务已跑 40 分钟、限额 60 分钟时，新领的 Worker 只剩 20 分钟，不是 60。

        ``items`` 可选传入该任务的 Intent 行（调用方已扫过全表时避免重复扫描）。
        取不到根 Intent（任务树被裁剪/历史数据）时返回 0（不限），不猜。
        """
        tid = str(task_id or "").strip()
        if not tid or self.task_deadline_s <= 0:
            return 0.0
        cached = self._task_deadlines.get(tid)
        if cached:
            return cached
        deadline = 0.0
        try:
            rows = items
            if rows is None:
                rows = [row for row in list_intents(self.bb, self.project_id, limit=5000)
                        if str(row.get("task_id") or "") == tid]
            root = next((row for row in rows if not row.get("parent_id")), None)
            started = str((root or {}).get("created_at") or "")
            if started:
                deadline = (datetime.fromisoformat(started).timestamp()
                            + self.task_deadline_s)
        except Exception:  # noqa: BLE001 —— 推不出 deadline 就当不限，不拦调度
            log.exception("ARTEX 任务 deadline 推算失败: %s", tid)
            deadline = 0.0
        # 只缓存**有效**值：0 可能是「根 Intent 还没落库」的瞬时状态，缓存它
        # 会让该任务永久失去时限（任务随后长出根节点也补不回来）。
        if deadline > 0:
            self._task_deadlines[tid] = deadline
        return deadline

    def _finish(self, iid: str, intent: dict, future: Future) -> None:
        with self._lock:
            control = dict(self._controls.get(iid, {}))
        try:
            result = future.result(timeout=0)
            # 撞预算上限（artex-exhausted）：Worker 在返回文本末尾带标记 →
            # 本次运行跑完了但**没做完**，标 exhausted 而非 completed。上游口径
            # （server/engine.go:1133-1146）：撞步数上限 ≠ 完成，Planner 据此
            # 知道「这个方向试过但需换角度/接着跑」，而不是当成已覆盖永久跳过。
            from core.chat.artex_worker import strip_exhausted_mark
            clean, exhausted = strip_exhausted_mark(str(result or ""))
            current = get_intent(self.bb, self.project_id, iid) or {}
            # 已是终态（Worker 内部已收尾 / 被 pause / cancel）则不覆盖——
            # 尤其不能把 exhausted/blocked/failed 强写成 completed。
            if current.get("status") in TERMINAL_STATUSES or \
                    current.get("status") == "paused":
                pass
            elif control.get("kill"):
                transition_intent(self.bb, self.project_id, iid, "cancelled")
            elif exhausted:
                transition_intent(self.bb, self.project_id, iid, "exhausted",
                                  result=clean[:12000],
                                  error="撞运行预算上限（步数/时长）被动收尾：本次只写回部分产出")
            else:
                transition_intent(self.bb, self.project_id, iid, "completed",
                                  result=clean[:12000])
        except TimeoutError:
            future.cancel()
            # Worker 墙钟超时：同属「探到一半被掐断」→ exhausted（上游
            # ReasonTimeout 同口径），不是彻底失败。
            hard = self._hard_timeout.get(iid, self.timeout)
            self._exhaust(iid, f"worker 超时（>{hard:g}s）：本次只写回部分产出")
        except Exception as exc:  # noqa: BLE001
            self._fail(iid, str(exc))
        finally:
            with self._lock:
                self._controls.pop(iid, None)
                self._active.discard(iid)
                self._futures.pop(iid, None)
                self._started_at.pop(iid, None)
                self._hard_timeout.pop(iid, None)
            if control.get("kill"):
                current = get_intent(self.bb, self.project_id, iid) or {}
                if current.get("status") not in TERMINAL_STATUSES | {"paused"}:
                    try:
                        transition_intent(self.bb, self.project_id, iid, "cancelled")
                    except Exception:  # noqa: BLE001
                        log.exception("Worker 终止收尾失败: %s", iid)
            finished = get_intent(self.bb, self.project_id, iid) or {}
            if self.on_intent_finished is not None and finished:
                try:
                    self.on_intent_finished(finished, "finished")
                except Exception:  # noqa: BLE001
                    log.exception("ARTEX Intent 完成回调异常: %s", iid)

    def _collect(self) -> bool:
        progressed = False
        with self._lock:
            active = list(self._futures.items())
        now_mono = time.monotonic()
        for iid, (intent, future) in active:
            started = self._started_at.get(iid, now_mono)
            # 每个 Worker 的硬超时单独算（有任务 deadline 时 = 夹逼预算 + 收尾宽限）。
            limit = self._hard_timeout.get(iid, self.timeout)
            if future.done() or now_mono - started >= limit:
                self._finish(iid, intent, future)
                progressed = True
        return progressed

    def _enforce_task_limits(self) -> None:
        """按任务检查 deadline 与 Intent 预算，超限暂停剩余 pending。

        节流 5s 一次：tick 频率高，且 limit 判定本身要全表扫描。幂等——
        处理过的任务没有 pending，下一轮自然跳过。

        两件事**分开判**（artex-taskclock）：

        * **暂停 pending**：只在还有 pending 时才有意义；
        * **收尾时序**：任务一到点就该跑，与「还有没有 pending」无关——上游
          ``settleTask`` 是在 deadline 到点即触发，哪怕此刻全是在跑 Worker。
          若把两者绑在一起，任务到点时 pending 恰好全被领走，终局目标判定就
          永远不会发生，任务卡在 exploring。
        """
        if self.task_deadline_s <= 0 and self.max_intents <= 0:
            return
        now_mono = time.monotonic()
        if now_mono - self._limits_checked_at < 5.0:
            return
        self._limits_checked_at = now_mono
        rows = list_intents(self.bb, self.project_id, limit=5000)
        by_task: dict[str, list[dict]] = {}
        for row in rows:
            by_task.setdefault(str(row.get("task_id") or ""), []).append(row)
        for task_id, items in by_task.items():
            if not task_id:
                continue
            reason = ""
            deadline_hit = False
            if self.max_intents > 0 and len(items) >= self.max_intents:
                reason = f"任务 Intent 数达到上限 {self.max_intents}"
            elif self.task_deadline_s > 0:
                # 与 Worker 夹逼共用同一份 deadline 推算（_task_deadline_at）：
                # 两处各算一遍会漂——尤其 paused/resume 后 created_at 不变，
                # 而「现在」变了，两套算法可能一个说超限一个说没超。
                deadline_at = self._task_deadline_at(task_id, items)
                if deadline_at > 0 and time.time() >= deadline_at:
                    reason = f"任务运行超过 {self.task_deadline_s:g}s"
                    deadline_hit = True
            if not reason:
                continue
            has_pending = any(item.get("status") == "pending" for item in items)
            if has_pending:
                try:
                    pause_task_pending(self.bb, self.project_id, task_id,
                                       reason=reason)
                except Exception:  # noqa: BLE001 —— 预算闸失败不阻断调度
                    log.exception("ARTEX 任务预算暂停失败: %s", task_id)
            # 任务到点收尾时序（artex-taskclock，对齐上游 settleTask ⑤）：跑**一次**
            # 终局规划轮（任务超时收尾词、只做最后目标判定、不派新意图）。
            # 只在 deadline 命中时触发；Intent 数超限不属「到点」，不该做终局判定。
            if deadline_hit and task_id not in self._settled_tasks:
                self._settled_tasks.add(task_id)
                self._start_settle(task_id, reason)

    def _start_settle(self, task_id: str, reason: str) -> None:
        """任务到点后的收尾时序：起一个后台线程跑终局规划轮（不阻塞 tick）。

        对齐上游 ``server/engine_timeout.go::settleTask`` ⑤：
        ``runFinalPlannerRound`` 用**任务超时收尾词**做最后一次目标判定。
        放独立线程是因为它要等一次 LLM（可能几十秒），而 tick 必须保持
        调度节奏；同时保证同一任务只跑一次（``_settled_tasks`` 在调用方置位）。

        未注入 settler（Engine 独立使用 / 测试）时直接跳过——只暂停 pending，
        行为与引入本机制前一致。
        """
        if self.settler is None:
            return
        tid = str(task_id or "").strip()
        if not tid:
            return
        self.bb.append_event(self.project_id, "artex.task.settle.started",
                             {"task_id": tid, "reason": str(reason or "")[:200]})

        def _run() -> None:
            try:
                result = self.settler(tid, str(reason or ""))
                self.bb.append_event(self.project_id, "artex.task.settle.done", {
                    "task_id": tid,
                    "goal_covered": bool((result or {}).get("goal_covered")),
                    "added": int((result or {}).get("added") or 0),
                    "reason": str((result or {}).get("reason") or "")[:500],
                })
            except Exception as exc:  # noqa: BLE001 —— 终局判定失败不炸 Engine
                log.exception("ARTEX 任务终局规划轮失败: %s", tid)
                self.bb.append_event(self.project_id, "artex.task.settle.failed", {
                    "task_id": tid, "error": str(exc)[:500]})

        try:
            threading.Thread(target=_run, name=f"artex-settle-{tid[:12]}",
                             daemon=True).start()
        except Exception:  # noqa: BLE001 —— 起线程失败不阻断调度
            log.exception("ARTEX 终局规划线程启动失败: %s", tid)

    def tick(self, *, wait: bool = True) -> bool:
        """回收 Worker 并填满并发槽位；默认等待本轮新领取任务完成。"""
        self._enforce_task_limits()
        progressed = self._collect()
        # 题目闸（challenge-flag-ledger v47）：CTF 轨每题并发上限 + 已解/已放弃
        # 题目不再派 Worker。**每 tick 只算一次**（要扫 assets + 在跑 Intent），
        # 非 CTF 轨返回全空集合，claim 的题目逻辑自动失效。
        gate = challenge_gate(self.bb, self.project_id)
        submitted: list[tuple[str, dict, Future]] = []
        while True:
            with self._lock:
                slots = self._max_workers - len(self._futures)
            if slots <= 0:
                break
            intent = claim_next_intent(
                self.bb, self.project_id, self.worker_id,
                per_challenge_limit=int(gate.get("per_challenge_limit") or 0),
                blocked_challenges=gate.get("blocked"),
                running_by_challenge=gate.get("running"),
                challenge_ids=gate.get("challenge_ids"))
            if not intent:
                break
            iid = intent["id"]
            try:
                transition_intent(self.bb, self.project_id, iid, "running")
                runtime_intent = dict(intent)
                runtime_intent["_worker_control"] = lambda iid=iid: self.poll_worker_control(iid)
                # 任务级 deadline 夹逼（artex-taskclock，对齐上游 agent/taskclock.go
                # 的 ``WithTaskClock``）：把任务的**绝对到点时间**交给 handler，
                # 让它把本次 run 的墙钟预算夹到任务剩余时间，并在到点时自己进收尾
                # ——而不是等这里 future.cancel() 硬砍（硬砍时收尾阶段进不去，
                # 「已识别未写回」的内容照样蒸发）。
                deadline_at = self._task_deadline_at(str(intent.get("task_id") or ""))
                runtime_intent["_task_deadline_at"] = deadline_at
                runtime_intent["_run_budget_s"] = self.timeout
                # 外面的硬超时是**兜底**（防 handler 不理会预算卡死），永远等于
                # 「夹逼后的预算 + 收尾宽限」：
                # * 有任务 deadline 时 eff = min(自有预算, 任务剩余)；
                # * 没 deadline 时 eff = 自有预算（self.timeout，原语义不动）。
                # 两种都要加宽限——否则 Worker 一撞预算进收尾，外面的 cancel
                # 就在收尾第 0 步把它砍掉，「主动收尾」退化回「硬砍什么都没捞回」。
                if deadline_at > 0:
                    remaining = max(0.0, deadline_at - time.time())
                    eff = min(self.timeout, remaining) if self.timeout > 0 else remaining
                    # 下限 1s 只在这里需要：eff 可能因「已过点」变成 ≤0，而
                    # 「0」在下面的比较里意味着立刻超时——那是想要的行为，但
                    # 夹逼值本身不能是负数。
                    eff = max(1.0, eff)
                else:
                    eff = self.timeout
                hard = eff + self.settle_grace_s
                future = self._ensure_executor().submit(self.handler, runtime_intent)
                with self._lock:
                    self._active.add(iid)
                    self._futures[iid] = (intent, future)
                    self._started_at[iid] = time.monotonic()
                    self._hard_timeout[iid] = hard
                submitted.append((iid, intent, future))
                progressed = True
            except Exception as exc:  # noqa: BLE001
                self._fail(iid, str(exc))
                progressed = True
        if wait and submitted:
            # 等本轮新领的任务跑完：用它们各自的硬超时上界，不能用裸 self.timeout
            # ——否则会在收尾窗口里提前退出等待（虽然不会误杀，但 tick 语义失真）。
            with self._lock:
                waits = [self._hard_timeout.get(iid, self.timeout)
                         for iid, _, _ in submitted]
            deadline = time.monotonic() + max(waits or [self.timeout])
            while any(not future.done() for _, _, future in submitted) and time.monotonic() < deadline:
                self._collect()
                time.sleep(min(self.poll_interval, 0.01))
            self._collect()
        return self._collect() or progressed

    def _fail(self, intent_id: str, error: str) -> None:
        try:
            transition_intent(self.bb, self.project_id, intent_id, "failed", error=error)
        except Exception:  # noqa: BLE001
            log.exception("ARTEX Intent 收尾失败: %s", intent_id)

    def _exhaust(self, intent_id: str, error: str) -> None:
        """标 exhausted（撞预算/超时：本次跑完了但没做完，可续跑）。

        与 ``_fail`` 的区别只在语义与 Planner 处置：exhausted 不是「这个方向
        不行」，而是「没跑完」，故不进 failed 计数、可 retry 续跑。
        """
        try:
            transition_intent(self.bb, self.project_id, intent_id,
                              "exhausted", error=error)
        except Exception:  # noqa: BLE001
            log.exception("ARTEX Intent 预算耗尽收尾失败: %s", intent_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.tick():
                    self._stop.wait(self.poll_interval)
            except Exception:  # noqa: BLE001
                log.exception("ARTEX Engine tick 异常")
                self._stop.wait(self.poll_interval)
