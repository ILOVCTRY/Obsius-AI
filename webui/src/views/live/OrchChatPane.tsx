import { useEffect, useMemo, useRef } from "react"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { fmtDateTimeMin, utcTitle } from "@/lib/datetime"
import type { BBEvent, CoordinationPlanProposal, OrchPersona, PhaseGoal } from "@/lib/types"

// 停链 reason 中文（orch.chain_stopped.payload.reason，与后端 _stop_chain 调用点对齐）
const CHAIN_STOP_ZH: Record<string, string> = {
  converged: "收敛停止（编排器无新产出）",
  no_sessions: "停止（无可用会话窗）",
  level_changed: "停止（档位已变更）",
  paused: "停止（自动档已暂停）",
  restart: "停止（服务重启急停）",
  error: "停止（执行异常）",
  human: "手动停止",
}

// 对话化编排器 M1/M2/M3（2026-09-21，DESIGN §6.4）：编排页签前两段——
// 顶部阶段目标条（M2 goal 闭环，编辑/清空经回调交父级弹层）+ 中部对话流（M1，
// orch.chat 事件组装成轮：human 消息开新轮、orch 回复追加；窗口裁剪造成的孤儿
// orch 消息自成一轮不丢）。编排动作事件流（任务派发/orch.*/goal.*/llm.error…）
// 由「❋ 审计」抽屉承载（2026-09-28 删除运行记录折叠区，本组件不含）。
// orch 回复走 MarkdownView（react-markdown 无 raw HTML，防注入）；tool_trace
// 折叠展示（对话轮全闸门同源：发任务/开窗动作在轨迹里可见）。

type ChatTrace = { name: string; args: string; result: string }

const traceOf = (ev: BBEvent): ChatTrace[] => {
  const t = (ev.payload as { tool_trace?: unknown } | null)?.tool_trace
  if (!Array.isArray(t)) return []
  return t.filter((x): x is ChatTrace =>
    !!x && typeof x === "object" && typeof (x as ChatTrace).name === "string")
}

const textOf = (ev: BBEvent): string => {
  const t = (ev.payload as { text?: unknown } | null)?.text
  return typeof t === "string" ? t : ""
}

/** orch.chat 事件序列 → 对话轮：human 开新轮（截断上一轮），orch 追加当前轮 */
const turnsOf = (events: BBEvent[]) => {
  const turns: { key: number; human: BBEvent | null; orch: BBEvent[] }[] = []
  for (const e of events) {
    const role = (e.payload as { role?: unknown } | null)?.role
    if (role === "human" || !turns.length) {
      turns.push({ key: e.id, human: role === "human" ? e : null, orch: role === "human" ? [] : [e] })
    } else {
      turns[turns.length - 1].orch.push(e)
    }
  }
  return turns
}

function HumanBubble({ ev }: { ev: BBEvent }) {
  return (
    <div className="flex justify-end pr-1" title={utcTitle(ev.created_at)}>
      <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary/10 px-3 py-1.5 text-sm leading-relaxed text-foreground">
        <span className="whitespace-pre-wrap break-words">{textOf(ev)}</span>
      </div>
    </div>
  )
}

const WAKE_LABELS: Record<string, string> = {
  "task.failed": "任务失败",
  "task.starvation": "饿死/重绑告警",
  "budget.soft_warning": "预算软警",
  "phase.gate_open": "阶段出口门满足",
}

function coordinationPlansOf(ev: BBEvent): CoordinationPlanProposal[] {
  const raw = (ev.payload as { coordination_plans?: unknown } | null)?.coordination_plans
  if (!Array.isArray(raw)) return []
  return raw.filter((x): x is CoordinationPlanProposal => {
    if (!x || typeof x !== "object") return false
    const p = x as CoordinationPlanProposal
    return typeof p.plan_id === "string" && typeof p.name === "string" && Array.isArray(p.nodes)
  })
}

function CoordinationProposalCard({ plan, onConfirm, onOpen }: {
  plan: CoordinationPlanProposal
  onConfirm?: (plan: CoordinationPlanProposal) => void
  onOpen?: (plan: CoordinationPlanProposal) => void
}) {
  const draft = plan.status === "draft"
  return <div className="mt-2 rounded-lg border border-primary/30 bg-background/40 p-2.5 text-xs">
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <b>{plan.name}</b>
          <span className="rounded bg-primary/15 px-1.5 py-px text-[10px] text-primary">{draft ? "等待确认" : plan.status}</span>
        </div>
        {plan.objective && <p className="mt-1 text-muted-foreground">{plan.objective}</p>}
      </div>
      <span className="shrink-0 text-muted-foreground">{plan.nodes.length} 个成员任务</span>
    </div>
    <div className="mt-2 space-y-1 border-t border-white/10 pt-2">
      {plan.nodes.map((node, i) => <div key={node.id} className="flex gap-2">
        <span className="w-5 shrink-0 text-muted-foreground">{String(i + 1).padStart(2, "0")}</span>
        <div className="min-w-0 flex-1"><b>{node.title}</b><span className="ml-2 text-muted-foreground">{node.role || "待分配"}</span>
          {node.depends_on.length > 0 && <small className="ml-2 text-muted-foreground">依赖 {node.depends_on.length} 项</small>}
        </div>
      </div>)}
    </div>
    <div className="mt-2 flex items-center gap-2 border-t border-white/10 pt-2">
      {draft ? <button type="button" onClick={() => onConfirm?.(plan)} className="rounded border border-primary/60 bg-primary/10 px-2 py-1 text-[11px] text-primary hover:bg-primary/20">确认执行前检查</button> : <span className="text-(--status-ok)">✓ 已进入协调流程</span>}
      <button type="button" onClick={() => onOpen?.(plan)} className="rounded border px-2 py-1 text-[11px] text-muted-foreground hover:bg-accent">打开协调</button>
    </div>
  </div>
}

function OrchBubble({ ev, name, onConfirmPlan, onOpenPlan }: { ev: BBEvent; name: string; onConfirmPlan?: (plan: CoordinationPlanProposal) => void; onOpenPlan?: (plan: CoordinationPlanProposal) => void }) {
  const trace = traceOf(ev)
  const plans = coordinationPlansOf(ev)
  const p = ev.payload as { proactive?: unknown; triggers?: unknown } | null
  const triggers = Array.isArray(p?.triggers)
    ? p!.triggers.filter((x): x is string => typeof x === "string") : []
  return (
    <div className="flex justify-start pl-1" title={utcTitle(ev.created_at)}>
      <div className="max-w-[92%] rounded-2xl rounded-bl-sm bg-accent/40 px-3 py-1.5">
        <p className="mb-0.5 text-[10px] text-muted-foreground">
          {p?.proactive === true && (
            <span className="mr-1 rounded bg-amber-500/20 px-1 py-px text-amber-400"
              title={`异常订阅唤醒：${triggers.map(t => WAKE_LABELS[t] ?? t).join("、") || "异常事件"}`}>
              🔔 主动唤醒
            </span>
          )}
          {name} · {fmtDateTimeMin(ev.created_at)}
        </p>
        <MarkdownView content={textOf(ev)} prefix={`orch-chat-${ev.id}`}
          className="max-h-96 overflow-auto text-sm leading-relaxed text-foreground/90" />
        {plans.map((plan) => <CoordinationProposalCard key={plan.plan_id} plan={plan} onConfirm={onConfirmPlan} onOpen={onOpenPlan} />)}
        {trace.length > 0 && (
          <details className="mt-1 border-t border-white/10 pt-1 text-[11px] text-muted-foreground">
            <summary className="cursor-pointer select-none hover:text-foreground">
              🔧 动作轨迹 · {trace.length} 步（发任务/开窗等同闸门）
            </summary>
            <div className="mt-1 space-y-1">
              {trace.map((t, i) => (
                <div key={i} className="rounded border border-white/10 bg-background/40 px-1.5 py-1 font-mono">
                  <span className="text-foreground">{t.name}</span>
                  {t.args && <span className="ml-1.5 break-all opacity-70">{t.args}</span>}
                  {t.result && <div className="mt-0.5 break-all opacity-60">→ {t.result}</div>}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    </div>
  )
}

/** 研判卡（auto-attack 2026-09-28）：orch.auto_attack.analyzed 事件的流内呈现——
 * 渗透计划全文（markdown）+ 内联「开跑」确认。链已活=按钮转「运行中」态（幂等防重）。 */
function OrchAnalysisCard({ ev, name, chainActive, startBusy, onStartRun }: {
  ev: BBEvent
  name: string
  chainActive?: boolean
  startBusy?: boolean
  onStartRun?: (ev: BBEvent) => void
}) {
  const p = ev.payload as { budget_ticks?: unknown; summary?: unknown } | null
  const summary = typeof p?.summary === "string" ? p.summary : ""
  const budget = typeof p?.budget_ticks === "number" ? p.budget_ticks : null
  return (
    <div className="flex justify-start pl-1" title={utcTitle(ev.created_at)}>
      <div className="max-w-[92%] rounded-2xl rounded-bl-sm border border-primary/30 bg-accent/40 px-3 py-1.5">
        <p className="mb-1 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
          <span className="rounded bg-primary/20 px-1 py-px text-primary">🚀 自动渗透研判</span>
          {budget != null && (
            <span className="rounded bg-amber-500/20 px-1 py-px text-amber-400">预算 {budget} 轮</span>
          )}
          <span>{name} · {fmtDateTimeMin(ev.created_at)}</span>
        </p>
        <MarkdownView content={summary} prefix={`atk-${ev.id}`}
          className="max-h-96 overflow-auto text-sm leading-relaxed text-foreground/90" />
        <div className="mt-1.5 border-t border-white/10 pt-1.5">
          {chainActive ? (
            <span className="text-[11px] text-(--status-ok)" title="自动链已按计划运行（停止走 goal 条右侧按钮）">
              ✓ 自动链运行中
            </span>
          ) : (
            <button type="button" onClick={() => onStartRun?.(ev)} disabled={startBusy}
              title="按此计划开跑：写入链预算并触发首轮编排（有产出即自动续链）"
              className="rounded border border-primary/60 bg-primary/10 px-2 py-0.5 text-[11px] text-primary hover:bg-primary/20 disabled:opacity-50">
              ▶ 开跑{budget != null ? ` · ${budget} 轮` : ""}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export function OrchChatPane({ events, busy, persona, goal, onEditGoal, onEditPersona,
  onConfirmPlan, onOpenPlan, level, chainActive, autoBusy, onStartAuto, onStopAuto, onStartRun, orchRunning,
  chainTicks, chainEstranged, onForceAcquire, uiStyle = "claude" }: {
  events: BBEvent[]
  busy: boolean
  persona: OrchPersona | null
  goal: PhaseGoal | null
  onEditGoal: () => void
  onEditPersona: () => void
  onConfirmPlan?: (plan: CoordinationPlanProposal) => void
  onOpenPlan?: (plan: CoordinationPlanProposal) => void
  // 自动渗透（auto-attack 2026-09-28）：仅 L2 档显示——链未活=「启动」（弹层选
  // 轮数档→研判→流内确认开跑），链活=「停止」（停链不降档）。非 L2 不渲染。
  level?: string
  chainActive?: boolean
  autoBusy?: boolean
  onStartAuto?: () => void
  onStopAuto?: () => void
  // 研判卡内联「开跑」（写链预算 + 触发首轮编排）
  onStartRun?: (ev: BBEvent) => void
  // 编排 tick 运行中（研判/开跑后首/链轮）：对话流内极简进度行 + 编排器思考展开
  orchRunning?: boolean
  // 链状态行（2026-09-28 继承修复）：ticks=已跑轮数；estranged=后端重启急停（DB 活
  // 但进程 runner 丢）——链状态本就持久化（DB chain_active + chain_started/stopped
  // 事件），此前 goal 条只有二态按钮，重启/停链后「跑过什么、为何停」无任何痕迹
  chainTicks?: number
  chainEstranged?: boolean
  // 强制接管（2026-09-28 人工救济）：编排轮卡死（已运行超 15 分钟）时显示的
  // 「⚡ 强制接管」按钮回调——清租约后可重新点火
  onForceAcquire?: () => void
  // 思考进度行双形态（2026-09-28）：claude=✻ 暗淡行直显最新思考尾部（不点开，
  // 点开看全量）；trae=灰色极简行折叠（现状）。数据源 config.ui_style（LiveRoom）
  uiStyle?: "claude" | "trae"
}) {
  const orchName = persona?.display_name || "编排器"
  // 当前编排轮已运行分钟数（从最近 orch.tick.started 事件算）：状态行显示时长，
  // 让「慢」（几分钟正常）和「卡死」（15 分钟+）可分辨——409 救济的判断依据
  const tickStartedMin = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].kind === "orch.tick.started") {
        const t = new Date(events[i].created_at).getTime()
        return Number.isNaN(t) ? null : Math.max(0, Math.round((Date.now() - t) / 60000))
      }
    }
    return null
  }, [events])
  // 自动渗透入口（仅 L2）：链活→停止；链未活→启动（弹层选档→研判→流内确认）
  const autoBtn = level === "L2" && (chainActive ? (
    <button type="button" onClick={onStopAuto} disabled={autoBusy}
      title="停止自动链（不降档；在跑任务不受影响）"
      className="shrink-0 rounded border border-(--status-error)/50 px-2 py-0.5 text-[11px] text-(--status-error) hover:bg-(--status-error)/10 disabled:opacity-50">
      ⏹ 停止自动链
    </button>
  ) : (
    <button type="button" onClick={onStartAuto} disabled={autoBusy}
      title="启动自动渗透：先研判态势出计划，你确认后再开跑"
      className="shrink-0 rounded border border-primary/50 px-2 py-0.5 text-[11px] text-primary hover:bg-primary/10 disabled:opacity-50">
      🚀 启动自动渗透
    </button>
  ))
  // 强制接管（救济）：编排轮已跑超 15 分钟（明显异常于正常几分钟的研判/链轮）
  // 才显示——正常慢轮不误导；点击走确认弹层（双跑风险由人拍板）
  const forceBtn = orchRunning && (tickStartedMin ?? 0) >= 15 && onForceAcquire && (
    <button type="button" onClick={onForceAcquire} disabled={autoBusy}
      title={`编排轮已运行 ${tickStartedMin} 分钟，疑似卡死——强制接管将清除其执行租约，之后可重新「启动」`}
      className="shrink-0 rounded border border-(--status-approval)/50 px-2 py-0.5 text-[11px] text-(--status-approval) hover:bg-(--status-approval)/10 disabled:opacity-50">
      ⚡ 强制接管
    </button>
  )
  const turns = useMemo(
    () => turnsOf(events.filter((e) =>
      e.kind === "orch.chat" || e.kind === "orch.auto_attack.analyzed")),
    [events])
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  // 编排器思考流（tick 运行中「正在研判/规划」状态行的展开体）：研判与链轮期间
  // 唯一的流内进度反馈——orch.tick.started/llm.thinking 不进对话轮，之前全程静默
  const orchThinking = useMemo(() => events
    .filter((e) => (e.kind === "llm.thinking" || e.kind === "llm.thinking.delta")
      && e.author === "orchestrator")
    .slice(-12), [events])
  // claude 形态直显体：最新一条思考的尾部（暗淡行不点开可见，title 悬停看全文）
  const latestThinking = orchThinking.length
    ? String(orchThinking[orchThinking.length - 1].payload.thinking ?? "")
    : ""
  const thinkingTail = latestThinking.length > 160
    ? "…" + latestThinking.slice(-160)
    : latestThinking
  // 链状态行数据源：最近一条链生命周期事件（started/stopped 均持久化，重启/刷新
  // 后从此恢复「上次链」显示——修复「重启窗口后启动自动渗透状态归零」）
  const chainEv = useMemo(() => {
    let last: BBEvent | null = null
    for (const e of events) {
      if (e.kind === "orch.chain_started" || e.kind === "orch.chain_stopped") {
        if (!last || e.id > last.id) last = e
      }
    }
    return last
  }, [events])
  // 自动滚底：仅在用户已接近底部时跟随（上翻历史时不抢滚动条）
  useEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [turns, busy])
  const onScroll = () => {
    const el = scroller.current
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 顶部阶段目标条（M2 goal 闭环）：注入 tick + 对话轮系统提示的人类验收口径 */}
      <div className="shrink-0 border-b px-3 py-2 text-xs">
        {goal ? (
          <div className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <p className="text-foreground">
                🎯 <span className="font-medium">{goal.text}</span>
                {goal.phase && <span className="ml-2 text-[10px] text-muted-foreground">阶段：{goal.phase}</span>}
              </p>
              {!!goal.criteria?.length && (
                <ul className="mt-0.5 list-disc pl-5 leading-relaxed text-muted-foreground">
                  {goal.criteria.map((c, i) => <li key={i}>{c}</li>)}
                </ul>
              )}
              <p className="mt-0.5 text-[10px] text-muted-foreground">
                {fmtDateTimeMin(goal.created_at)} 由 {goal.confirmed_by} 确认 · 已注入编排 tick 与对话轮
              </p>
            </div>
            {forceBtn}{autoBtn}
            <button type="button" onClick={onEditGoal}
              title="修改或清空阶段目标（goal.confirm/goal.clear 事件留痕）"
              className="shrink-0 rounded border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground">
              编辑
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-muted-foreground">
            <span className="min-w-0 flex-1">
              尚未设定阶段目标——与{orchName}聊出方向后，把验收口径固化下来（注入每轮编排与对话）
            </span>
            {forceBtn}{autoBtn}
            <button type="button" onClick={onEditGoal}
              className="shrink-0 rounded border border-primary/50 px-2 py-0.5 text-[11px] text-primary hover:bg-primary/10">
              🎯 设定阶段目标
            </button>
          </div>
        )}
        {/* 链状态行（持久继承，2026-09-28）：数据源全持久化——usage.chain(DB chain_active)
            + chain_started/stopped 事件，重启/刷新后自动还原显示，不再「归零感」。
            三态：运行中（绿）/ 待恢复（琥珀，服务重启过 runner 失联）/ 上次停止（灰） */}
        {level === "L2" && (chainActive ? (
          chainEstranged ? (
            <p className="mt-1 text-[10px] text-(--status-approval)"
              title="后端服务重启过：链开关仍在但自动执行器已失联——点「停止自动链」清理后可重新启动">
              ● 自动链待恢复 · 已跑 {chainTicks ?? 0} 轮（服务重启过）
            </p>
          ) : (
            <p className="mt-1 text-[10px] text-(--status-ok)">
              ● 自动链运行中 · 已跑 {chainTicks ?? 0} 轮
            </p>
          )
        ) : chainEv?.kind === "orch.chain_stopped" ? (
          <p className="mt-1 text-[10px] text-muted-foreground"
            title={`停于 ${fmtDateTimeMin(chainEv.created_at)}`}>
            ○ 上次自动链：{CHAIN_STOP_ZH[String(chainEv.payload.reason)] ?? String(chainEv.payload.reason ?? "未知")}
            （跑了 {String(chainEv.payload.chain_ticks ?? "0")} 轮）· {fmtDateTimeMin(chainEv.created_at)}
          </p>
        ) : null)}
        <button type="button" onClick={onEditPersona}
          title={persona?.persona || "给编排器起名、立人设（只注入对话轮，tick 不受影响）"}
          className="mt-1 text-[10px] text-muted-foreground hover:text-foreground hover:underline">
          🎭 {orchName} · 身份设定
        </button>
      </div>
      {/* 中部对话流（M1）：升序渲染 + 接近底部自动跟随 */}
      <div ref={scroller} onScroll={onScroll}
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-3 py-3">
        {turns.length === 0 && !busy && (
          <div className="mx-auto mt-10 max-w-md rounded-lg border bg-card p-4 text-center text-xs leading-relaxed text-muted-foreground">
            <p className="mb-1 text-foreground">与{orchName}对话</p>
            问态势、问下一步、纠正方向……它可查黑板事件流、发任务、开窗（与编排一轮同闸门）。
            对话不推进编排游标、不消费指令——插队轮，回答完毕即返回。
          </div>
        )}
        {turns.map((t) => (
          <div key={t.key} className="flex flex-col gap-1.5">
            {t.human && <HumanBubble ev={t.human} />}
            {t.orch.map((o) => o.kind === "orch.auto_attack.analyzed" ? (
              <OrchAnalysisCard key={o.id} ev={o} name={orchName}
                chainActive={chainActive} startBusy={autoBusy} onStartRun={onStartRun} />
            ) : (
              <OrchBubble key={o.id} ev={o} name={orchName} onConfirmPlan={onConfirmPlan} onOpenPlan={onOpenPlan} />
            ))}
          </div>
        ))}
        {orchRunning && (
          // 编排 tick 运行中的流内进度反馈（研判/开跑后首/链轮）——补上「点启动
          // 后主会话静默」的断层。双形态（2026-09-28）：claude=✻ 暗淡行直显最新
          // 思考尾部（不点开，点开看全量）；trae=灰色极简行折叠（现状不变）。
          <details className="pl-1" key="orch-running">
            {uiStyle === "trae" ? (
              <summary className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted-foreground">
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                正在研判态势 / 规划下一步
                {tickStartedMin != null && (
                  <span className={tickStartedMin >= 15 ? "text-(--status-approval)" : "text-muted-foreground/60"}>
                    · 已 {tickStartedMin} 分钟{tickStartedMin >= 15 ? "（疑似卡死，可强制接管）" : ""}
                  </span>
                )}
                {orchThinking.length > 0 && <span className="text-muted-foreground/40">›</span>}
              </summary>
            ) : (
              <summary className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted-foreground/70">
                <span className="shrink-0 animate-pulse text-muted-foreground/60">✻</span>
                <span className="min-w-0 flex-1 truncate" title={latestThinking || undefined}>
                  {thinkingTail || "正在研判态势 / 规划下一步"}
                </span>
                {tickStartedMin != null && (
                  <span className={tickStartedMin >= 15 ? "shrink-0 text-(--status-approval)" : "shrink-0 text-muted-foreground/50"}>
                    · {tickStartedMin}m{tickStartedMin >= 15 ? " ⚠" : ""}
                  </span>
                )}
              </summary>
            )}
            {orchThinking.length > 0 && (
              <div className="ml-3 mt-0.5 max-h-64 space-y-1 overflow-auto border-l border-white/10 pl-2.5">
                {orchThinking.map((e) => (
                  <p key={e.id} className="text-xs leading-relaxed text-muted-foreground/90">
                    {String(e.payload.thinking ?? "")}
                  </p>
                ))}
              </div>
            )}
          </details>
        )}
        {busy && (
          <div className="flex justify-start pl-1">
            <div className="flex items-center gap-2 rounded-2xl rounded-bl-sm bg-accent/40 px-3 py-1.5 text-sm text-muted-foreground">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
              {orchName}思考中…
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
