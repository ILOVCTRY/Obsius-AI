"""ARTEX Intent 到 ChatTurn 专家 Worker 的适配层。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.chat import store as chat_store
from core.chat.runtime import ChatTurn

# 撞预算上限标记（artex-exhausted）：Worker 返回文本末尾带它 → Engine 标
# exhausted 而非 completed。用不可见控制字符包裹，避免被当成正文的一部分
# 展示；Engine 落 result 前会剥掉它（见 artex_engine._finish）。
EXHAUSTED_MARK = "\u200b[artex:exhausted]\u200b"
_EXHAUSTED_MARK = EXHAUSTED_MARK


def strip_exhausted_mark(text: str) -> tuple[str, bool]:
    """剥掉 ``EXHAUSTED_MARK``，返回 ``(干净文本, 是否撞预算)``。"""
    raw = str(text or "")
    if EXHAUSTED_MARK in raw:
        return raw.replace(EXHAUSTED_MARK, "").rstrip(), True
    return raw, False


def make_worker(*, bb, llm, project_id: str, packs_root: str | Path,
                track: str, capabilities: list[str] | None = None,
                artifacts_dir: str | Path | None = None,
                browser_pool=None, containers=None, recovery=None):
    """返回可直接交给 ArtexEngine 的 handler(intent) 闭包。

    每个 Intent 创建独立 Chat 线程和普通专家 ChatTurn；不设置 probe_mode，
    因而沿用专家 YAML 工具白名单、资产边界和正常意图纪律。

    终态约定（artex-exhausted，对齐上游 server/engine.go:1133-1146）：
    handler 正常返回 = 本次运行跑完了，Engine 标 ``completed``；若本轮是**撞
    步数上限**被动收尾（``ChatTurn.budget_exhausted``），返回文本末尾带上
    ``EXHAUSTED_MARK``，由 Engine 据此标 ``exhausted``——「试过但没做完」，
    Planner 必须换角度或续跑，不能当成已覆盖。
    """
    def run(intent: dict[str, Any]) -> str:
        expert = str(intent.get("expert") or "").strip()
        objective = str(intent.get("objective") or "").strip()
        if not expert or not objective:
            raise ValueError("ARTEX Intent 缺少 expert 或 objective")
        asset_ids = [str(x).strip() for x in (intent.get("asset_ids") or [])
                     if str(x).strip()]
        # Engine 注入的 Worker 控制（kill/steer）；缺失时保持原行为。
        control = intent.get("_worker_control")
        # 任务级 deadline 夹逼（artex-taskclock，对齐上游 agent/taskclock.go）：
        # Engine 注入任务的**绝对到点时间**与本次 run 的自有墙钟预算；ChatTurn
        # 据此夹逼自己的预算并在到点时主动进收尾（而不是被外面 future.cancel()
        # 硬砍）。缺省 0 = 不限，行为与引入前完全一致。
        try:
            task_deadline_at = max(0.0, float(intent.get("_task_deadline_at") or 0.0))
        except (TypeError, ValueError):
            task_deadline_at = 0.0
        try:
            run_budget_s = max(0.0, float(intent.get("_run_budget_s") or 0.0))
        except (TypeError, ValueError):
            run_budget_s = 0.0
        # 血缘：Intent 由对话主控发起时带 parent_thread_id，Worker 线程挂到主控
        # 线程之下（UI 显示「↳ spawn」，删除主控线程会级联清理）。父线程已不
        # 存在（跨会话/已删除）时降级为独立线程，不影响执行。
        constraints = intent.get("constraints") or {}
        parent_thread_id = str(constraints.get("parent_thread_id") or "").strip()
        # 联网搜索继承（v43，web-search-toggle）：Intent 由对话主控发起时带
        # parent_thread_id 血缘；主控线程开着「联网」= Worker 线程默认也启用
        # （与 call_expert spawn 同口径）。父线程缺失/已删时按关，不猜。
        web_search = False
        if parent_thread_id:
            parent = chat_store.get_thread(bb, parent_thread_id)
            web_search = bool((parent or {}).get("web_search"))
        thread_kwargs: dict[str, Any] = {"title": objective[:60],
                                         "spawned_task": objective,
                                         "web_search": web_search,
                                         "artex_intent_id": str(intent.get("id") or ""),
                                         "artex_task_id": str(intent.get("task_id") or "")}
        if parent_thread_id:
            try:
                thread = chat_store.create_thread(
                    bb, project_id, expert, parent_thread_id=parent_thread_id,
                    **thread_kwargs)
            except ValueError:
                thread = chat_store.create_thread(bb, project_id, expert,
                                                  **thread_kwargs)
        else:
            thread = chat_store.create_thread(bb, project_id, expert,
                                              **thread_kwargs)
        turn = ChatTurn(
            bb=bb, llm=llm, project_id=project_id, thread_id=thread["id"],
            packs_root=packs_root, track=track,
            capabilities=capabilities or [],
            expert_names=[expert],
            artifacts_dir=artifacts_dir,
            browser_pool=browser_pool, containers=containers,
            assigned_asset_ids=asset_ids,
            probe_mode=False,
            artex_intent_id=str(intent.get("id") or ""),
            artex_task_id=str(intent.get("task_id") or ""),
            task_deadline_at=task_deadline_at,
            run_budget_s=run_budget_s,
            worker_control=control if callable(control) else None,
            recovery=recovery,
        )
        result = str(turn.run(objective) or "")
        # 撞预算上限：Engine 收到带标记的返回值 → 标 exhausted 而非 completed。
        # 标记放**末尾**（前端/日志按原文读，前缀不加噪声），且只在确实耗尽时加。
        if getattr(turn, "budget_exhausted", False):
            return result + EXHAUSTED_MARK
        return result
    return run
