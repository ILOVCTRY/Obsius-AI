"""ARTEX 主控操舵层（2026-10-19，artex-fusion）。

原生 ARTEX 的分层是 **MainAgent（观察 + 操舵）+ Planner（唯一 Intent 生成者）
+ Engine（Frontier 调度 + 并发 Worker）+ Worker（执行）**。此前本项目把
``artex-controller`` 做成「主控自己 call_expert 派活」的第二个激进主控，
与工作台的 ARTEX 任务引擎（``plan_intents`` → ``ArtexEngine`` → Worker）完全
隔离：同名不同物、调度逻辑重复、两边状态互不可见。

本模块把对话式主控接到引擎这一侧。四个工具对应四个动作：

- ``artex_plan``    → Planner：新建任务树 / 向同一任务追加方向
- ``artex_status``  → 观察：任务 runtime + Intent 状态 + 探索图摘要
- ``artex_hint``    → 操舵：写探索图 Hint；运行中的 Worker 直接收到 steer
- ``artex_control`` → 调度控制：start / drain / stop / pause / resume / cancel / kill

主控因此只做判断，不再自己决定「派给谁」——派发权归 Engine，避免两条调度
路径各自维护并发、重试与生命周期。

工具结果文本统一以 ``[ARTEX]`` 成功 / ``[错误]`` 失败开头：前者便于模型
确认动作已落地，后者是对话链既有失败判定约定（``_dispatch`` 只看前缀）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from core.blackboard.artex_exploration import create_node, list_nodes, record_hint
from core.blackboard.artex_intents import (
    control_intent,
    list_intents,
    pause_task_pending,
)
from core.blackboard.artex_planner import add_intents, plan_followup, plan_intents
from core.blackboard.artex_runtime import evaluate_task, get_runtime

log = logging.getLogger(__name__)

# 仍在推进的 Intent 状态：只要任务里还有这些，任务就算「进行中」。
ACTIVE_STATUSES = frozenset({"pending", "claimed", "running", "paused"})
_TODO_MARKS = {"pending": "☐", "in_progress": "▶", "completed": "✔"}

_CONTROL_ACTIONS = ("start", "drain", "stop", "pause", "resume", "cancel", "kill")


@dataclass
class SteeringContext:
    """主控操舵所需的全部依赖（由 ChatTurn 装配，避免本模块反向依赖 runtime）。"""

    bb: Any
    project_id: str
    track: str
    llm: Any = None
    expert_names: list[str] = field(default_factory=list)
    engine_provider: Callable[[], Any] | None = None
    author: str = "artex-controller"
    task_id: str = ""
    # 发起任务的对话线程：写进 Intent constraints，Worker 线程据此挂到主控线程
    # 之下（UI 可追溯「谁派生的这个 Worker」）。空串=不建立血缘。
    thread_id: str = ""

    def constraints(self) -> dict:
        return {"parent_thread_id": self.thread_id} if self.thread_id else {}

    def engine(self):
        """取项目 Engine（可能尚未创建）；失败返回 None，不阻断主控对话。"""
        if self.engine_provider is None:
            return None
        try:
            return self.engine_provider()
        except Exception:  # noqa: BLE001 —— 引擎不可用不该炸掉整轮
            log.exception("ARTEX Engine 获取失败 project=%s", self.project_id)
            return None


def _intents_of(bb, project_id: str, task_id: str) -> list[dict]:
    return [row for row in list_intents(bb, project_id, limit=1000)
            if str(row.get("task_id") or "") == task_id]


def active_task_id(bb, project_id: str) -> str:
    """当前项目「还在推进」的任务 id（取最近创建的一个）。

    **降级路径**：v47 起主控线程用 ``chat_threads.artex_task_id`` 显式绑定
    （一个主控只绑一个任务，见 `resolve_task`）。本函数只在**未绑定**时兜底
    ——例如老线程、API 直连、工作台无会话场景。两个主控会话同时开时，
    显式绑定让它们各打各的任务；未绑定的才会互相抢最近活跃任务。
    """
    latest: dict[str, str] = {}
    for row in list_intents(bb, project_id, limit=1000):
        tid = str(row.get("task_id") or "")
        if not tid or str(row.get("status") or "") not in ACTIVE_STATUSES:
            continue
        stamp = str(row.get("created_at") or "")
        if stamp >= latest.get(tid, ""):
            latest[tid] = stamp
    if not latest:
        return ""
    return max(latest.items(), key=lambda kv: kv[1])[0]


def bind_task_to_thread(ctx: SteeringContext, task_id: str) -> None:
    """把任务绑定到主控线程（``chat_threads.artex_task_id``）。

    「一个 ARTEX 主控只绑定一个任务」的落地：绑定后 `resolve_task` 恒返回它，
    不再按「最近活跃」猜。绑不上不阻断（任务已建，后续可重绑/靠降级路径）。
    同一线程改绑另一个任务 = 显式切换（新任务接管本会话）。
    """
    tid = str(getattr(ctx, "thread_id", "") or "").strip()
    task_id = str(task_id or "").strip()
    if not tid or not task_id:
        return
    try:
        from core.chat.store import update_thread
        update_thread(ctx.bb, tid, artex_task_id=task_id)
    except Exception:  # noqa: BLE001 —— 绑定失败退回 active_task_id 降级路径
        log.exception("ARTEX 任务绑定线程失败 thread=%s task=%s", tid, task_id)
    try:
        ctx.task_id = task_id
    except Exception:  # noqa: BLE001 —— dataclass 被冻结等极端情况
        pass


def resolve_task(ctx: SteeringContext, task_id: str = "") -> str:
    """本会话该操作哪个任务：显式传参 > 线程绑定 > 最近活跃（降级）。"""
    explicit = str(task_id or "").strip()
    if explicit:
        return explicit
    bound = str(getattr(ctx, "task_id", "") or "").strip()
    if bound:
        return bound
    return active_task_id(ctx.bb, ctx.project_id)


def _intent_line(row: dict, *, limit: int = 90) -> str:
    objective = str(row.get("objective") or "").replace("\n", " ")[:limit]
    expert = str(row.get("expert") or "-")
    assets = ",".join(str(x) for x in (row.get("asset_ids") or [])[:3])
    tail = f" assets={assets}" if assets else ""
    deps = [str(x) for x in (row.get("depends_on") or []) if str(x)]
    tail += f" 依赖前置={','.join(deps[:3])}" if deps else ""
    return (f"- {row.get('id')} [{row.get('status')}] expert={expert} "
            f"p{row.get('priority', 0)} {objective}{tail}")


def _fact_line(node: dict, *, limit: int = 90) -> str:
    """事实行：标题 + confidence + 来源意图（artex-record-fact 的展示口径）。

    confidence 缺省时按分值反推（兜底汇总事实没有 level 字段，其 1.0 表示
    「整轮跑完了」而非「直接看到」，反推成 observed 只是为了兼容展示，
    提示词侧另有说明）。
    """
    meta = node.get("metadata") or {}
    level = str(meta.get("confidence_level") or "")
    if not level:
        level = ("observed" if float(node.get("confidence") or 0) >= 1.0
                 else "inferred")
    src = str(node.get("intent_id") or "")
    tags = [level] + ([f"来自 {src}"] if src else [])
    return (f"- {str(node.get('title') or '')[:limit]}"
            f"（{'；'.join(tags)}）")


def _engine_state(ctx: SteeringContext) -> tuple[Any, str]:
    engine = ctx.engine()
    if engine is None:
        return None, "Engine 未创建"
    try:
        running = bool(engine.running)
        workers = int(engine.active_workers)
    except Exception:  # noqa: BLE001
        return engine, "Engine 状态读取失败"
    return engine, (f"Engine 运行中，活跃 Worker {workers}" if running
                    else "Engine 未启动")


# ---------- artex_plan ----------


def artex_plan(ctx: SteeringContext, args: dict) -> str:
    """新建任务树或向既有任务追加方向。"""
    mode = str(args.get("mode") or "auto").strip().lower()
    if mode not in ("auto", "new", "extend"):
        return "[错误] mode 只能是 auto / new / extend"
    goal = str(args.get("goal") or "").strip()
    explicit_task = str(args.get("task_id") or "").strip()
    asset_ids = [str(x).strip() for x in (args.get("asset_ids") or []) if str(x).strip()]
    items = args.get("intents")
    if items is not None and not isinstance(items, list):
        return "[错误] intents 必须是数组"
    try:
        max_new = max(1, min(int(args.get("max_new", 3) or 3), 10))
    except (TypeError, ValueError):
        max_new = 3

    current = resolve_task(ctx, explicit_task)
    creating = mode == "new" or (mode == "auto" and not current)

    if creating:
        if not goal:
            return "[错误] 新建 ARTEX 任务需要 goal（目标描述）"
        try:
            result = plan_intents(
                bb=ctx.bb, project_id=ctx.project_id, llm=ctx.llm, goal=goal,
                track=ctx.track, allowed_experts=ctx.expert_names,
                asset_ids=asset_ids, task_id=explicit_task,
                constraints_extra=ctx.constraints())
        except Exception as exc:  # noqa: BLE001 —— 规划失败回文本让模型改道
            return f"[错误] ARTEX 任务规划失败: {exc}"
        task_id = str((result.get("root") or {}).get("task_id") or explicit_task)
        # 「一个主控只绑定一个任务」：新任务建完立刻绑到本线程，后续
        # artex_plan/artex_status 不再按「最近活跃」猜（两个主控会抢）。
        bind_task_to_thread(ctx, task_id)
        lines = [f"[ARTEX] 已创建任务 {task_id}（{result.get('count', 0)} 个 Intent）"]
        lines.extend(_intent_line(row) for row in (result.get("intents") or [])[:12])
        goal_total = int(result.get("goal_total") or 0)
        if goal_total:
            lines.append(f"目标已达登记 {goal_total} 条（终态将由目标达成情况决定，"
                         "用 artex_goal(action=\"list\") 查看）：")
            lines.extend(f"- {g.get('id')}｜{str(g.get('title') or '')[:110]}"
                         for g in (result.get("goals") or [])[:10])
        else:
            lines.append("注意：目标分解失败，本次未登记目标——"
                         "终态会退回「有事实即达成」的粗判，"
                         "建议用 artex_goal(action=\"set\") 补登记。")
        anchored = result.get("anchored_assets") or []
        if anchored:
            lines.append(f"已锚定 {len(anchored)} 个范围资产（覆盖度分母）；"
                         "用 artex_status 查看覆盖度与未测清单。")
        lines.append(_start_engine(ctx))
        return "\n".join(lines)

    task_id = current
    todos = args.get("todos")
    if todos is not None and not isinstance(todos, list):
        return "[错误] todos 必须是数组（每项 {text, status}）"
    if items:
        try:
            result = add_intents(
                bb=ctx.bb, project_id=ctx.project_id, task_id=task_id,
                track=ctx.track, items=items, goal=goal,
                allowed_experts=ctx.expert_names, asset_ids=asset_ids,
                max_new=max_new, constraints_extra=ctx.constraints(),
                todos=todos)
        except Exception as exc:  # noqa: BLE001
            return f"[错误] ARTEX 追加方向失败: {exc}"
    else:
        if ctx.llm is None:
            return "[错误] 未配置 Planner 模型，无法自动规划；请改用 intents 显式给出方向"
        facts = list_nodes(ctx.bb, ctx.project_id, task_id=task_id, kind="fact")
        findings = list_nodes(ctx.bb, ctx.project_id, task_id=task_id, kind="finding")
        try:
            result = plan_followup(
                bb=ctx.bb, project_id=ctx.project_id, llm=ctx.llm,
                task_id=task_id, track=ctx.track, goal=goal,
                allowed_experts=ctx.expert_names, asset_ids=asset_ids,
                facts=facts, findings=findings, max_new=max_new,
                constraints_extra=ctx.constraints())
        except Exception as exc:  # noqa: BLE001
            return f"[错误] ARTEX 再规划失败: {exc}"

    if items is None and todos is not None:
        # 只登记/更新规划待办（不改方向）：主控显式记下串行链后按步派。
        try:
            from core.blackboard.artex_runtime import set_planner_todos
            rows = set_planner_todos(ctx.bb, ctx.project_id, task_id, todos)
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 写入规划待办失败: {exc}"
        done = sum(1 for t in rows if t["status"] == "completed")
        if not rows:
            return f"[ARTEX] 任务 {task_id} 的规划待办已清空。"
        lines = [f"[ARTEX] 任务 {task_id} 规划待办已更新：{len(rows)} 项（{done} 完成）"]
        lines.extend(f"  {_TODO_MARKS.get(t['status'], '☐')} {t['text']}"
                     for t in rows[:12])
        lines.append("Planner 下次唤醒会带上该清单，只派前置已满足的下一步。")
        return "\n".join(lines)

    if result.get("budget_exhausted"):
        return (f"[ARTEX] 任务 {task_id} 已达 Intent 预算上限，未新增方向："
                f"{result.get('reason') or ''}。用 artex_control(action=\"pause\") "
                "暂停剩余方向，或先收敛既有 Worker。")
    added = result.get("intents") or []
    met_note = ""
    accepted = result.get("goals_met") or []
    rejected = result.get("goals_rejected") or []
    if accepted:
        met_note = f"已证明 {len(accepted)} 条目标：" + "；".join(
            f"{row.get('goal_id')}（{row.get('why')}）" for row in accepted[:5])
    if rejected:
        met_note += (("；" if met_note else "") + "目标达成声明被驳回：" + "；".join(
            f"{row.get('goal_id')}（{row.get('why')}）" for row in rejected[:5]))
    if result.get("goal_covered"):
        return (f"[ARTEX] 任务 {task_id} 的注册目标已全部达成，未新增方向："
                f"{result.get('reason') or '（无说明）'}。"
                + (met_note + "。" if met_note else "")
                + "用 artex_status 复核后收尾。")
    if not added:
        reason = result.get("reason") or "方向与既有 Intent 重复"
        return (f"[ARTEX] 任务 {task_id} 未新增方向（skipped="
                f"{result.get('skipped', 0)}）：{reason}。"
                + (met_note + "。" if met_note else "")
                + "换一个尚未覆盖的方向再试。")
    lines = [f"[ARTEX] 任务 {task_id} 新增 {len(added)} 个 Intent"
             f"（skipped={result.get('skipped', 0)}）"]
    if met_note:
        lines.append(met_note + "。")
    lines.extend(_intent_line(row) for row in added[:12])
    lines.append(_start_engine(ctx))
    return "\n".join(lines)


def _start_engine(ctx: SteeringContext) -> str:
    """新 Intent 落库后确保 Engine 在跑，否则 Frontier 永远无人领取。"""
    engine = ctx.engine()
    if engine is None:
        return "注意：Engine 未就绪，Intent 已入队但不会自动执行。"
    try:
        if not engine.running:
            engine.start()
            return "Engine 已启动，Worker 将按优先级领取 Intent。"
        return "Engine 已在运行，新 Intent 会进入 Frontier。"
    except Exception as exc:  # noqa: BLE001
        log.exception("ARTEX Engine 启动失败 project=%s", ctx.project_id)
        return f"注意：Engine 启动失败（{exc}），Intent 已入队但不会自动执行。"


# ---------- artex_status ----------


def _challenge_status_lines(ctx: SteeringContext, task_id: str,
                            intents: list[dict]) -> list[str]:
    """CTF 轨的题目态势段（challenge-flag-ledger v47）。

    非 CTF 项目（无 challenge 资产）返回空列表——渗透轨不受影响。

    显示三件事，都是主控做「继续 / 收尾」判断的直接依据：

    1. **进度**：解出 N / 共 M 道（这是 CTF 轨的覆盖度，不是资产覆盖度）；
    2. **逐题状态**：untouched/running/partial/solved/abandoned + 已拿 flag 数；
    3. **已否决 flag**：人工标错的串——它不再算解出，模型也不该再提交。

    只显示任务范围内的题目（Intent 锚定的题目）；一个都没锚定则显示全部。
    """
    try:
        assets = ctx.bb.list_assets(ctx.project_id)
    except Exception:  # noqa: BLE001
        return []
    cid_set = {a["id"] for a in assets
               if a.get("type") == "challenge" and not a.get("parent_id")}
    if not cid_set:
        return []
    scoped: list[str] = []
    for intent in intents:
        for aid in (intent.get("asset_ids") or []):
            sid = str(aid or "")
            if sid in cid_set and sid not in scoped:
                scoped.append(sid)
    if not scoped:
        scoped = sorted(cid_set)
    running: dict[str, int] = {}
    for intent in intents:
        if str(intent.get("status") or "") not in ("claimed", "running"):
            continue
        for aid in (intent.get("asset_ids") or []):
            sid = str(aid or "")
            if sid in cid_set:
                running[sid] = running.get(sid, 0) + 1
                break
    try:
        from core.blackboard.challenge_flags import challenge_flag_summary
        summary = challenge_flag_summary(ctx.bb, ctx.project_id, scoped, running)
    except Exception:  # noqa: BLE001 —— 台账读取失败不阻断状态观察
        log.exception("ARTEX 题目态势读取失败 task=%s", task_id)
        return []
    marks = {"solved": "✓", "partial": "◐", "running": "▶",
             "abandoned": "✗", "untouched": "○"}
    labels = {"solved": "已解出", "partial": "部分解出", "running": "求解中",
              "abandoned": "已放弃", "untouched": "未跑"}
    solved = sum(1 for cid in scoped if (summary.get(cid) or {}).get("solved"))
    rejected_total = sum(int((summary.get(cid) or {}).get("rejected") or 0)
                         for cid in scoped)
    lines = [f"题目进度：解出 {solved}/{len(scoped)} 道"
             + (f"（另有 {rejected_total} 条 flag 已被人工否决）"
                if rejected_total else "")]
    name_of = {a["id"]: str(a.get("value") or a["id"]) for a in assets}
    for cid in scoped[:20]:
        st = summary.get(cid) or {}
        state = str(st.get("state") or "untouched")
        quota = int(st.get("quota") or 1)
        got = int(st.get("accepted") or 0)
        tail = f" {got}/{quota}" if quota > 1 else ""
        if st.get("running"):
            tail += f"（{st['running']} 个 Worker）"
        if st.get("rejected"):
            tail += f"（否决 {st['rejected']}）"
        lines.append(f"  {marks.get(state, '○')} {name_of.get(cid, cid)[:60]}"
                     f"｜{labels.get(state, state)}{tail}")
    if len(scoped) > 20:
        lines.append(f"  …另有 {len(scoped) - 20} 道未列出")
    lines.append("CTF 轨口径：题目状态由 flag 台账派生，终态 = 全部题目解出；"
                 "已解出的题调度层不会再派 Worker（要再测需撤销否决或重新建题）。")
    return lines


def artex_status(ctx: SteeringContext, args: dict) -> str:
    """观察：任务 runtime + Intent 状态 + 探索图摘要。"""
    detail = str(args.get("detail") or "summary").strip().lower()
    task_id = resolve_task(ctx, args.get("task_id"))
    if not task_id:
        return ("[ARTEX] 当前项目没有进行中的任务。用 artex_plan(goal=\"…\", "
                "mode=\"new\") 建立任务树。")
    intents = _intents_of(ctx.bb, ctx.project_id, task_id)
    if not intents:
        return f"[ARTEX] 任务 {task_id} 不存在或没有任何 Intent。"
    runtime = get_runtime(ctx.bb, ctx.project_id, task_id)
    try:
        # 每次观察都重推状态：prove_goal/set/手工追加目标可能发生在 runtime
        # 上次更新后；不能让主控看到过期的 goal_reached / needs_review。
        runtime = evaluate_task(ctx.bb, ctx.project_id, task_id,
                                reason="status_query")
    except Exception:  # noqa: BLE001 —— 状态推导失败不影响观察
        log.exception("ARTEX 任务状态推导失败: %s", task_id)
        runtime = runtime or {}
    nodes = list_nodes(ctx.bb, ctx.project_id, task_id=task_id)
    counts: dict[str, int] = {}
    for row in intents:
        key = str(row.get("status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    _, engine_note = _engine_state(ctx)

    lines = [f"[ARTEX] 任务 {task_id}｜状态 {runtime.get('status') or 'unknown'}"
             f"｜{engine_note}"]
    if runtime.get("goal"):
        lines.append(f"目标：{str(runtime.get('goal'))[:160]}")
    # 目标态是终态判定依据，必须放在最显眼处（artex-goals）。
    try:
        from core.blackboard.artex_goals import list_goals
        goals = list_goals(ctx.bb, ctx.project_id, task_id)
    except Exception:  # noqa: BLE001 —— 目标读取失败不影响观察其它维度
        goals = []
    if goals:
        met = sum(1 for g in goals if str(g.get("status")) == "met")
        lines.append(f"目标：{len(goals)} 条（已达成 {met}，未达成 {len(goals) - met}）")
        for goal in goals:
            mark = "✓" if str(goal.get("status")) == "met" else "○"
            lines.append(f"  {mark} {goal.get('id')}｜{str(goal.get('title') or '')[:110]}"
                         + (f"（证据 {len(goal.get('evidence') or [])} 条）"
                            if goal.get("evidence") else "（无证据）"))
    else:
        lines.append("目标：未注册（终态将退回「有事实即达成」的粗判；"
                     "可用 artex_goal(action=\"set\") 补登记）")
    # CTF 轨的终态判据是 **flag 台账**（challenge-flag-ledger v47），不是目标/fact：
    # 题目不是待测资产、flag 也不是 fact。这里显式给出「解出几道 / 共几道」与
    # 逐题状态，主控据此判断该继续派方向还是收尾。
    challenge_lines = _challenge_status_lines(ctx, task_id, intents)
    if challenge_lines:
        lines.extend(challenge_lines)
    lines.append("Intent 计数：" + "，".join(
        f"{k}={v}" for k, v in sorted(counts.items())))
    # 规划待办（artex-planner-todos）：跨唤醒保留的串行链清单——先按 Intent 实际
    # 状态校正（确定性），让主控看到「链走到哪一步、下一步该派什么」。
    try:
        from core.blackboard.artex_runtime import reconcile_planner_todos
        todos = reconcile_planner_todos(ctx.bb, ctx.project_id, task_id)
    except Exception:  # noqa: BLE001 —— 待办读取失败不影响观察其它维度
        todos = []
    if todos:
        done = sum(1 for t in todos if t["status"] == "completed")
        lines.append(f"规划待办（{len(todos)} 项，{done} 完成）：")
        lines.extend(f"  {_TODO_MARKS.get(t['status'], '☐')} {t['text'][:100]}"
                     for t in todos[:10])
    # 阻塞 Intent（artex-deps）：前置失败/取消或被手工拦下——它不会自己恢复，
    # 必须由主控处置（重试前置、改派替代方向、或放弃该链），故显式列出。
    blocked_intents = [row for row in intents if str(row.get("status")) == "blocked"]
    if blocked_intents:
        lines.append(f"阻塞 Intent（{len(blocked_intents)} 条，需处置——"
                     "前置失败/取消后下游不会自行恢复）：")
        for row in blocked_intents[:8]:
            err = str(row.get("error") or "").replace("\n", " ")[:80]
            lines.append(f"  ⛔ {row.get('id')}｜{str(row.get('objective') or '')[:70]}"
                         + (f"（{err}）" if err else ""))
        lines.append("  处置：artex_control(action=\"retry\", intent_id=前置id) 重试前置；"
                     "或 artex_plan(intents=[…]) 派替代方向（不带 depends_on）。")
    # 撞预算没做完的 Intent（artex-exhausted）：与 blocked 不同——它不是被前置
    # 拦住，而是**自己跑了但没跑完**。主控要看清「试过但没做完」再决定续跑或换向，
    # 绝不能当成已覆盖（否则该方向被永久跳过、任务假性收敛）。
    exhausted_intents = [row for row in intents
                         if str(row.get("status")) == "exhausted"]
    if exhausted_intents:
        lines.append(f"预算耗尽 Intent（{len(exhausted_intents)} 条，**未完成**——"
                     "该方向试过但没做完，不是已覆盖）：")
        for row in exhausted_intents[:8]:
            err = str(row.get("error") or "").replace("\n", " ")[:80]
            lines.append(f"  ⏳ {row.get('id')}｜{str(row.get('objective') or '')[:70]}"
                         + (f"（{err}）" if err else ""))
        lines.append("  处置：先 artex_status 看结果/trace 判断做到哪一步，再 "
                     "artex_control(action=\"retry\", intent_id=…) 续跑，"
                     "或 artex_plan 派「接上次进度继续」的新方向。")
    facts = [n for n in nodes if n.get("kind") == "fact"]
    findings = [n for n in nodes if n.get("kind") == "finding"]
    hints = [n for n in nodes if n.get("kind") == "hint"]
    lines.append(f"探索图：Fact {len(facts)}｜Finding {len(findings)}｜Hint {len(hints)}")
    # 资产覆盖度（artex-coverage）：量化验收核对的实测值——主控据此判断
    # 「覆盖度达 X%」类目标是否真达成，而不是凭 worker 跑完就盖章。
    try:
        from core.blackboard.artex_coverage import task_coverage
        coverage = task_coverage(ctx.bb, ctx.project_id, task_id)
    except Exception:  # noqa: BLE001 —— 覆盖度读取失败不影响观察其它维度
        coverage = {}
    if coverage.get("enabled") and coverage.get("denominator"):
        pct = coverage.get("pct")
        by_type = "，".join(
            f"{row['type']} {row['tested']}/{row['total']}"
            for row in coverage.get("by_type") or [])
        lines.append(
            f"资产覆盖度：{coverage.get('tested')}/{coverage.get('denominator')}"
            f"（{('%.0f%%' % (pct * 100)) if pct is not None else '—'}；"
            f"{'任务锚点' if coverage.get('scope_source') == 'anchored' else '推断范围'}）"
            + (f"｜{by_type}" if by_type else ""))
        lines.append(f"未覆盖资产：{coverage.get('untested')} 个")
    elif coverage.get("enabled") and coverage.get("scope_rows") == 0:
        lines.append("资产覆盖度：范围未锚定（用 artex_goal/artex_plan 挂资产后可算）")
    if runtime.get("metadata", {}).get("limit_reason"):
        # 任务级预算到点（artex-taskclock）：与「单条 Intent 预算耗尽」是两件事——
        # 这里是整场探索到点，Engine 已暂停剩余方向并跑过终局目标判定。
        lines.append(f"任务级预算：{runtime['metadata']['limit_reason']}"
                     "（剩余方向已暂停，终局判定已跑过；要接着打需调大预算后恢复，"
                     "或新建任务承接）")

    shown = intents if detail == "full" else [
        row for row in intents if str(row.get("status")) in ACTIVE_STATUSES][:12]
    if shown:
        lines.append("Intent：")
        lines.extend(_intent_line(row) for row in shown[:20])
        if len(shown) > 20:
            lines.append(f"- …共 {len(shown)} 条（detail=\"full\" 看全量）")
    if facts:
        lines.append("最近事实：")
        # confidence 与来源意图一并展示（artex-record-fact）：主控据此判断
        # 「这条是直接看到的还是推断的」「哪个方向已经试过了」。
        lines.extend(_fact_line(n) for n in facts[-5:])
    if findings:
        lines.append("已登记发现：")
        lines.extend(f"- {str(n.get('title') or '')[:90]}" for n in findings[-5:])
    if hints:
        lines.append("最近方向提示：")
        lines.extend(f"- {str(n.get('content') or '')[:120]}" for n in hints[-3:])
    return "\n".join(lines)


# ---------- artex_hint ----------


def artex_hint(ctx: SteeringContext, args: dict) -> str:
    """写探索图 Hint；若目标 Intent 正在运行，同时下发 steer。"""
    text = str(args.get("text") or "").strip()
    if not text:
        return "[错误] text 必填：请给出具体的方向调整"
    intent_id = str(args.get("intent_id") or "").strip()
    task_id = resolve_task(ctx, args.get("task_id"))
    want_steer = args.get("steer", True)
    want_steer = True if want_steer is None else bool(want_steer)

    engine = ctx.engine() if intent_id and want_steer else None
    if engine is not None:
        try:
            engine.steer_work(intent_id, text)
            return (f"[ARTEX] 已向运行中的 Worker {intent_id} 下发方向调整"
                    f"（同时沉淀为探索图 Hint）：{text[:200]}")
        except LookupError:
            pass  # Worker 未运行：退化为纯 Hint 落库
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 下发方向调整失败: {exc}"

    if not intent_id and task_id:
        # 无具体 Intent：挂到任务根节点，保证 Hint 仍留在任务血缘里。
        root = next((row for row in _intents_of(ctx.bb, ctx.project_id, task_id)
                     if not row.get("parent_id")), None)
        intent_id = str((root or {}).get("id") or "")
    try:
        node = record_hint(ctx.bb, ctx.project_id, task_id=task_id,
                           intent_id=intent_id, text=text, author=ctx.author)
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 写入探索图 Hint 失败: {exc}"
    if node is None:
        # 任务还没有任何 Intent：直接落一个任务级 Hint 节点。
        try:
            node = create_node(
                ctx.bb, ctx.project_id, "hint", title=text[:120], content=text,
                task_id=task_id, status="open", confidence=0.5,
                metadata={"author": ctx.author, "scope": "task"})
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 写入探索图 Hint 失败: {exc}"
        return (f"[ARTEX] 已记录任务级 Hint（任务尚无 Intent，规划时会作为依据）："
                f"{text[:200]}")
    return (f"[ARTEX] 已记录 Hint（task={task_id or '-'} intent={intent_id or '-'}），"
            f"后续规划会把它作为新方向的依据：{text[:200]}")


# ---------- artex_control ----------


def artex_control(ctx: SteeringContext, args: dict) -> str:
    """调度控制：start / drain / stop / pause / resume / cancel / kill。"""
    action = str(args.get("action") or "").strip().lower()
    if action not in _CONTROL_ACTIONS:
        return "[错误] action 只能是 " + " / ".join(_CONTROL_ACTIONS)
    reason = str(args.get("reason") or "").strip()[:200]
    intent_id = str(args.get("intent_id") or "").strip()
    task_id = resolve_task(ctx, args.get("task_id"))

    if action in ("start", "drain", "stop"):
        engine = ctx.engine()
        if engine is None:
            return "[错误] Engine 未就绪：当前项目没有可用的 ARTEX 调度器"
        try:
            if action == "start":
                engine.start()
                return "[ARTEX] Engine 已启动，Frontier 开始领取 Intent。"
            if action == "drain":
                result = engine.drain(float(args.get("timeout", 5) or 5))
                return (f"[ARTEX] 已停止领取新 Intent，"
                        f"drained={result.get('drained')}，"
                        f"活跃 Worker {result.get('active_workers')} 个。")
            engine.stop(float(args.get("timeout", 5) or 5))
            return "[ARTEX] Engine 已停止（运行中 Worker 会在当前步后退出）。"
        except Exception as exc:  # noqa: BLE001
            return f"[错误] Engine {action} 失败: {exc}"

    if action == "kill":
        if not intent_id:
            return "[错误] kill 需要 intent_id"
        engine = ctx.engine()
        if engine is None:
            return "[错误] Engine 未就绪，无法终止 Worker"
        try:
            engine.kill_work(intent_id, reason or "ARTEX 主控终止 Worker")
        except LookupError as exc:
            return f"[错误] {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 终止 Worker 失败: {exc}"
        return f"[ARTEX] 已请求终止 Worker {intent_id}（{reason or '主控终止'}）。"

    if action == "cancel":
        if not intent_id and not task_id:
            return "[错误] cancel 需要 intent_id 或 task_id"
        targets = [intent_id] if intent_id else [
            str(row.get("id")) for row in _intents_of(ctx.bb, ctx.project_id, task_id)
            if str(row.get("status")) in ("pending", "paused")]
        if not targets:
            return f"[ARTEX] 任务 {task_id} 没有可取消的 pending/paused Intent。"
        cancelled, skipped = 0, 0
        for iid in targets:
            try:
                control_intent(ctx.bb, ctx.project_id, iid, "cancel")
                cancelled += 1
            except Exception:  # noqa: BLE001 —— 状态不允许取消时跳过
                skipped += 1
        if task_id:
            try:
                evaluate_task(ctx.bb, ctx.project_id, task_id, reason="cancelled")
            except Exception:  # noqa: BLE001
                log.exception("ARTEX 取消后状态推导失败: %s", task_id)
        return (f"[ARTEX] 已取消 {cancelled} 个 Intent"
                + (f"（{skipped} 个状态不允许取消）" if skipped else "") + "。")

    # pause / resume 面向任务
    if not task_id:
        return f"[错误] {action} 需要 task_id（当前项目没有进行中的任务）"
    if action == "pause":
        try:
            paused = pause_task_pending(ctx.bb, ctx.project_id, task_id,
                                        reason=reason or "ARTEX 主控暂停")
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 暂停任务失败: {exc}"
        return (f"[ARTEX] 任务 {task_id} 已暂停 {paused} 个 pending Intent"
                "（运行中的 Worker 继续收尾，不丢证据）。")
    # resume：把 paused 重新放回 Frontier
    resumed = 0
    for row in _intents_of(ctx.bb, ctx.project_id, task_id):
        if str(row.get("status")) != "paused":
            continue
        try:
            control_intent(ctx.bb, ctx.project_id, str(row.get("id")), "resume")
            resumed += 1
        except Exception:  # noqa: BLE001
            continue
    if resumed:
        note = _start_engine(ctx)
        return f"[ARTEX] 任务 {task_id} 已恢复 {resumed} 个 Intent。{note}"
    return f"[ARTEX] 任务 {task_id} 没有 paused 的 Intent 可恢复。"


# ---------- artex_goal ----------


_GOAL_ACTIONS = ("list", "set", "prove", "goal_met", "anchor")


def artex_goal(ctx: SteeringContext, args: dict) -> str:
    """目标与范围治理：查看 / 登记 / 证明 / 全局收官 / 锚定范围资产。

    对应上游 goals_api 的四个动作 + ``add_task_scope``。目标达成是任务收敛
    的**唯一依据**：``artex_status`` 的终态由目标态派生，因此「证明哪条目标
    达成」是主控最关键的判断动作——不是「跑完 Intent」。``anchor`` 把资产
    纳入任务范围（覆盖度分母），是量化验收核对的前提。
    """
    from core.blackboard.artex_coverage import (
        anchor_task_assets,
        task_coverage,
        unanchor_task_assets,
    )
    from core.blackboard.artex_goals import (
        declare_goal_met,
        list_goals,
        prove_goal,
        set_goals,
    )
    from core.blackboard.artex_runtime import declare_task_goal_met

    action = str(args.get("action") or "list").strip().lower()
    if action not in _GOAL_ACTIONS:
        return "[错误] action 只能是 " + " / ".join(_GOAL_ACTIONS)
    task_id = resolve_task(ctx, args.get("task_id"))
    if not task_id:
        return ("[ARTEX] 当前项目没有进行中的任务。用 artex_plan(goal=\"…\", "
                "mode=\"new\") 建立任务树后目标才会登记。")

    if action == "list":
        goals = list_goals(ctx.bb, ctx.project_id, task_id)
        if not goals:
            return (f"[ARTEX] 任务 {task_id} 还没有注册目标（历史任务或手工建树）。"
                    "用 artex_goal(action=\"set\", goals=[\"…\"]) 补登记，"
                    "或重建任务让 Planner 自动分解。")
        met = sum(1 for g in goals if str(g.get("status")) == "met")
        lines = [f"[ARTEX] 任务 {task_id} 目标 {len(goals)} 条（已达成 {met}，"
                 f"未达成 {len(goals) - met}）："]
        for goal in goals:
            mark = "✓" if str(goal.get("status")) == "met" else "○"
            vc = str((goal.get("metadata") or {}).get("vulnclass") or "")
            tail = f" [{vc}]" if vc else ""
            lines.append(f"- {mark} {goal.get('id')}｜"
                         f"{str(goal.get('title') or '')[:120]}{tail}")
            for ev in (goal.get("evidence") or [])[:3]:
                lines.append(f"    证据 {ev.get('id')}（{ev.get('kind')}）："
                             f"{str(ev.get('title') or '')[:80]}")
            if len(goal.get("evidence") or []) > 3:
                lines.append(f"    …共 {len(goal['evidence'])} 条证据")
        return "\n".join(lines)

    if action == "set":
        raw = args.get("goals")
        if not raw:
            return "[错误] set 需要 goals（数组，可含 {\"text\":\"…\",\"vulnclass\":\"…\"}）"
        try:
            result = set_goals(
                bb=ctx.bb, project_id=ctx.project_id, task_id=task_id,
                goals=raw, author=ctx.author,
                asset_ids=[str(x).strip() for x in (args.get("asset_ids") or [])
                           if str(x).strip()] or None)
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 登记目标失败: {exc}"
        added = result.get("added") or []
        if not added:
            return (f"[ARTEX] 任务 {task_id} 目标无变化（已存在或超限）："
                    f"skipped={len(result.get('skipped') or [])}，"
                    f"当前共 {result.get('total', 0)} 条。")
        lines = [f"[ARTEX] 任务 {task_id} 新增 {len(added)} 条目标"
                 f"（共 {result.get('total', 0)} 条）："]
        lines.extend(f"- {g.get('id')}｜{str(g.get('title') or '')[:120]}"
                     for g in added)
        lines.append("任务终态将由这些目标的达成情况决定；用 artex_goal(action=\"prove\") "
                     "把证据连到目标上。")
        return "\n".join(lines)

    if action == "prove":
        goal_id = str(args.get("goal_id") or "").strip()
        evidence = args.get("evidence_ids") or []
        if not goal_id:
            return "[错误] prove 需要 goal_id（用 artex_goal(action=\"list\") 查看）"
        if not evidence:
            return "[错误] prove 需要 evidence_ids（本任务 fact/finding 节点 id）"
        try:
            result = prove_goal(
                bb=ctx.bb, project_id=ctx.project_id, task_id=task_id,
                goal_id=goal_id, evidence_ids=evidence,
                reason=str(args.get("reason") or "").strip(), author=ctx.author)
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 证明目标失败: {exc}"
        note = ("目标全部达成，任务可收敛（artex_status 会显示 goal_reached）。"
                if result.get("all_met") else
                f"仍有 {result.get('open', 0)} 条目标未达成，继续推进。")
        return (f"[ARTEX] 已证明目标 {goal_id}（关联 {len(result.get('linked') or [])} "
                f"条证据）：{str(result.get('reason') or '')[:200]}。{note}")

    if action == "anchor":
        do_remove = bool(args.get("remove")) if args.get("remove") is not None else False
        asset_ids = [str(x).strip() for x in (args.get("asset_ids") or [])
                     if str(x).strip()]
        if not asset_ids:
            # 无 asset_ids：报当前覆盖度与未测清单（观察用途）。
            coverage = task_coverage(ctx.bb, ctx.project_id, task_id)
            if not coverage.get("enabled") or not coverage.get("denominator"):
                return (f"[ARTEX] 任务 {task_id} 尚未锚定范围资产"
                        "（覆盖度分母为 0，无法做量化验收）。"
                        "用 artex_goal(action=\"anchor\", asset_ids=[…]) 挂入要测的资产。")
            pct = coverage.get("pct")
            by_type = "，".join(
                f"{row['type']} {row['tested']}/{row['total']}"
                for row in coverage.get("by_type") or [])
            return (f"[ARTEX] 任务 {task_id} 资产覆盖度 "
                    f"{coverage.get('tested')}/{coverage.get('denominator')} "
                    f"（{('%.0f%%' % (pct * 100)) if pct is not None else '—'}）；"
                    f"未覆盖 {coverage.get('untested')} 个"
                    + (f"；{by_type}" if by_type else ""))
        try:
            if do_remove:
                result = unanchor_task_assets(
                    bb=ctx.bb, project_id=ctx.project_id, task_id=task_id,
                    asset_ids=asset_ids)
                return (f"[ARTEX] 已从任务 {task_id} 摘除 {result.get('removed')} 个"
                        f"范围资产（剩 {result.get('remaining')} 个）。")
            result = anchor_task_assets(
                bb=ctx.bb, project_id=ctx.project_id, task_id=task_id,
                asset_ids=asset_ids, source="manual",
                note=str(args.get("note") or "ARTEX 主控锚定"))
        except Exception as exc:  # noqa: BLE001
            return f"[错误] 锚定范围资产失败: {exc}"
        added = result.get("added") or []
        missing = result.get("missing") or []
        coverage = task_coverage(ctx.bb, ctx.project_id, task_id)
        lines = [f"[ARTEX] 任务 {task_id} 新增 {len(added)} 个范围资产"
                 f"（本次已有 {len(result.get('existing') or [])} 个，"
                 f"任务共锚定 {coverage.get('scope_rows') or 0} 个）"]
        if missing:
            lines.append(f"注意：{len(missing)} 个资产 id 在本项目不存在，已忽略："
                         + "，".join(missing[:5]))
        if coverage.get("denominator"):
            pct = coverage.get("pct")
            lines.append(f"当前覆盖度 {coverage.get('tested')}/{coverage.get('denominator')}"
                         f"（{('%.0f%%' % (pct * 100)) if pct is not None else '—'}），"
                         f"未覆盖 {coverage.get('untested')} 个——"
                         "量化目标须据此判定是否达成。")
        return "\n".join(lines)

    # goal_met：全局收官宣告
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return ("[错误] goal_met 需要 reason：说明目标真正达成的依据"
                "（不能是「本轮无新方向」）。")
    try:
        goals_result = declare_goal_met(
            bb=ctx.bb, project_id=ctx.project_id, task_id=task_id,
            reason=reason, author=ctx.author)
        runtime = declare_task_goal_met(
            ctx.bb, ctx.project_id, task_id, reason=reason, author=ctx.author)
    except Exception as exc:  # noqa: BLE001
        return f"[错误] 宣布任务收官失败: {exc}"
    return (f"[ARTEX] 任务 {task_id} 已宣布收官（标 met {len(goals_result.get('marked') or [])} "
            f"条目标），当前状态 {runtime.get('status')}。"
            "在手 Worker 会继续收尾，随后任务转 goal_reached。"
            "注意：这是未经逐条证明的全局宣告，审计上可辨（metadata.met_via）。")


# ---------- artex_fact ----------


_FACT_ACTIONS = ("list", "get")


def artex_fact(ctx: SteeringContext, args: dict) -> str:
    """事实库检索：列事实（带 confidence 与血缘）/ 取单条完整内容。

    对应上游 ``list_facts`` + ``node_detail`` 两个只读工具。为什么主控需要它：
    ``artex_status`` 只给最近 5 条标题，规划态势动辄几十条事实，主控要判断
    「这个方向是不是已经试过、结论是 observed 还是 inferred」必须能按需翻。

    Worker **不给**这两个能力（它只做自己那条意图，跨 work 的事实由 trace
    查询覆盖）——这是上游 ``WorkerTools()`` 的明确取舍，此处照搬。
    """
    from core.blackboard.artex_exploration import get_node, list_edges
    from core.blackboard.artex_facts import CONFIDENCE_LEVELS, list_facts

    action = str(args.get("action") or "list").strip().lower()
    if action not in _FACT_ACTIONS:
        return "[错误] action 只能是 " + " / ".join(_FACT_ACTIONS)
    task_id = resolve_task(ctx, args.get("task_id"))
    if not task_id:
        return ("[ARTEX] 当前项目没有进行中的任务。用 artex_plan(goal=\"…\", "
                "mode=\"new\") 建立任务树后才有事实库。")

    if action == "get":
        node_id = str(args.get("id") or "").strip()
        if not node_id:
            return "[错误] get 需要 id（从 action=\"list\" 的返回里取）"
        node = get_node(ctx.bb, ctx.project_id, node_id)
        if node is None:
            return (f"[错误] 未找到探索节点 {node_id}。注意资产 id 与探索节点 id "
                    "是两个空间，资产请用 bb_query。")
        if str(node.get("task_id") or "") != task_id:
            return f"[错误] 节点 {node_id} 不属于任务 {task_id}"
        meta = node.get("metadata") or {}
        level = str(meta.get("confidence_level") or "")
        if level not in CONFIDENCE_LEVELS:
            level = ("observed" if float(node.get("confidence") or 0) >= 1.0
                     else "inferred")
        lines = [f"[ARTEX] {node.get('kind')} {node_id}｜confidence={level}"
                 f"｜来自 {node.get('intent_id') or '-'}"]
        lines.append(f"标题：{str(node.get('title') or '')[:200]}")
        if meta.get("summary"):
            lines.append(f"结论：{str(meta['summary'])[:300]}")
        if meta.get("evidence"):
            lines.append(f"证据：{str(meta['evidence'])[:600]}")
        body = str(node.get("content") or "")
        lines.append("详情：\n" + (body[:3000] if body else "（无）"))
        if len(body) > 3000:
            lines.append(f"…（共 {len(body)} 字，已截断）")
        assets = [str(x) for x in (node.get("asset_ids") or []) if str(x)]
        if assets:
            lines.append("关联资产：" + "，".join(assets[:10]))
        edges = list_edges(ctx.bb, ctx.project_id, node_id=node_id)
        if edges:
            rels = []
            for e in edges[:12]:
                if e["from_node_id"] == node_id:
                    rels.append(f"{e['relation']} → {e['to_node_id']}")
                else:
                    rels.append(f"{e['from_node_id']} --{e['relation']}→ 本节点")
            lines.append("血缘：" + "；".join(rels))
        return "\n".join(lines)

    # list
    level_filter = str(args.get("confidence") or "").strip().lower()
    if level_filter and level_filter not in CONFIDENCE_LEVELS:
        return "[错误] confidence 只能是 observed / inferred（留空不筛）"
    intent_id = str(args.get("intent_id") or "").strip()
    page = list_facts(
        ctx.bb, ctx.project_id, task_id,
        intent_id=intent_id or None,
        limit=int(args.get("limit") or 20),
        offset=int(args.get("offset") or 0),
        detail_limit=int(args.get("detail_limit") or 200))
    rows = page.get("facts") or []
    if level_filter:
        rows = [r for r in rows if str(r.get("confidence")) == level_filter]
    scope = f"意图 {intent_id} " if intent_id else "任务 "
    if not rows:
        extra = f"（已按 confidence={level_filter} 过滤）" if level_filter else ""
        return (f"[ARTEX] {scope}{task_id} 暂无事实{extra}。"
                "事实由 Worker 在探索过程中用 record_fact 写入；"
                "若 Worker 未显式写，收尾时会落一条整轮汇总事实。")
    lines = [f"[ARTEX] {scope}{task_id} 事实 {page.get('total')} 条，"
             f"本页 {len(rows)} 条（新→旧）："]
    for row in rows:
        tags = [str(row.get("confidence") or "")]
        if row.get("from_intent"):
            tags.append(f"来自 {row['from_intent']}")
        if row.get("source") == "intent_result":
            tags.append("整轮汇总")
        lines.append(f"- {row['id']}｜{str(row.get('summary') or '')[:100]}"
                     f"（{'；'.join(t for t in tags if t)}）")
        if row.get("evidence"):
            lines.append(f"    证据：{str(row['evidence'])[:150]}")
        if row.get("detail"):
            lines.append(f"    {str(row['detail'])[:200]}")
    if page.get("has_more"):
        lines.append(f"…还有更多，用 offset={int(args.get('offset') or 0) + len(rows)} 继续翻。")
    lines.append("取某条完整内容：artex_fact(action=\"get\", id=\"…\")。")
    return "\n".join(lines)


# ---------- 分发 ----------

_HANDLERS: dict[str, Callable[[SteeringContext, dict], str]] = {
    "artex_plan": artex_plan,
    "artex_status": artex_status,
    "artex_hint": artex_hint,
    "artex_control": artex_control,
    "artex_goal": artex_goal,
    "artex_fact": artex_fact,
}


def dispatch(ctx: SteeringContext, name: str, args: dict) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"[错误] 未知 ARTEX 工具: {name}"
    try:
        return handler(ctx, args or {})
    except Exception as exc:  # noqa: BLE001 —— 操舵失败回文本，绝不炸整轮
        log.exception("ARTEX 操舵失败 name=%s project=%s", name, ctx.project_id)
        return f"[错误] {name} 执行异常: {exc}"


def is_artex_tool(name: str) -> bool:
    return str(name or "") in _HANDLERS
