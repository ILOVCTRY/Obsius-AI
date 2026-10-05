import { useEffect, useMemo, useRef } from "react"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { fmtDateTimeMin, utcTitle } from "@/lib/datetime"
import type { BBEvent, CoordinationPlanProposal, CoordinationTeamMember, OrchPersona } from "@/lib/types"

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
  const members = plan.team?.members ?? []
  const grouped = members.map((member) => ({
    member,
    nodes: plan.nodes.filter((node) => node.member_id === member.member_id),
  }))
  const ungrouped = members.length
    ? plan.nodes.filter((node) => !node.member_id || !members.some((member) => member.member_id === node.member_id))
    : []
  // 旧提案没有团队名册时，按 role 保留可读的团队视图。
  const fallback = members.length === 0
    ? [...new Set(plan.nodes.map((node) => node.role || "待分配"))].map((role) => ({
      member: { member_id: role, title: role, role } satisfies CoordinationTeamMember,
      nodes: plan.nodes.filter((node) => (node.role || "待分配") === role),
    }))
    : []
  const sections = [...grouped, ...fallback]
  return <div className="mt-2 rounded-lg border border-primary/30 bg-background/40 p-2.5 text-xs">
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <b>{plan.team?.name || plan.name}</b>
          <span className="rounded bg-primary/15 px-1.5 py-px text-[10px] text-primary">{draft ? "等待确认" : plan.status}</span>
        </div>
        <p className="mt-1 text-[10px] font-medium text-primary">Agent 团队 · 共享目标</p>
        <p className="mt-0.5 text-muted-foreground">{plan.objective || "尚未填写团队共享目标"}{plan.team?.source === "legacy_derived" && " · 成员按角色兼容推导"}</p>
      </div>
      <span className="shrink-0 text-muted-foreground">{plan.nodes.length} 个工作节点</span>
    </div>
    <div className="mt-2 space-y-2 border-t border-white/10 pt-2">
      {sections.map(({ member, nodes }) => { const memberInfo = member as CoordinationTeamMember; return <div key={memberInfo.member_id} className="rounded border border-white/10 bg-background/30 p-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0"><b>{memberInfo.label || memberInfo.title || memberInfo.member_id}</b>
            <span className="ml-2 text-muted-foreground">{memberInfo.role || "未指定角色"}</span>
            {memberInfo.description && <p className="mt-0.5 text-muted-foreground">{memberInfo.description}</p>}
          </div>
          <span className="shrink-0 text-muted-foreground">{nodes.length} 节点</span>
        </div>
        <div className="mt-1.5 space-y-1">
          {nodes.map((node) => <div key={node.id} className="flex gap-2">
            <span className="w-5 shrink-0 text-muted-foreground">{String(plan.nodes.indexOf(node) + 1).padStart(2, "0")}</span>
            <div className="min-w-0 flex-1"><b>{node.title}</b>
              {node.depends_on.length > 0 && <small className="ml-2 text-muted-foreground">依赖 {node.depends_on.length} 项：{node.depends_on.join("、")}</small>}
            </div>
          </div>)}
          {!nodes.length && <span className="text-muted-foreground">暂无分配节点</span>}
        </div>
      </div> })}
      {ungrouped.length > 0 && <div className="rounded border border-dashed border-white/20 p-2">
        <b className="text-muted-foreground">未编组节点</b>
        {ungrouped.map((node) => <div key={node.id} className="mt-1 flex gap-2"><span className="w-5 text-muted-foreground">{String(plan.nodes.indexOf(node) + 1).padStart(2, "0")}</span><div><b>{node.title}</b><span className="ml-2 text-muted-foreground">{node.role || "待分配"}</span>{node.depends_on.length > 0 && <small className="ml-2 text-muted-foreground">依赖 {node.depends_on.length} 项：{node.depends_on.join("、")}</small>}</div></div>)}
      </div>}
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
export function OrchChatPane({ events, busy, persona, onEditPersona, orchRunning,
  onConfirmPlan, onOpenPlan, uiStyle = "claude" }: {
  events: BBEvent[]
  busy: boolean
  persona: OrchPersona | null
  onEditPersona: () => void
  onConfirmPlan?: (plan: CoordinationPlanProposal) => void
  onOpenPlan?: (plan: CoordinationPlanProposal) => void
  // 编排 tick 运行中和强制接管（保留通用编排故障救济）
  orchRunning?: boolean
  onForceAcquire?: () => void
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
  const turns = useMemo(
    () => turnsOf(events.filter((e) => e.kind === "orch.chat")),
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
      <div className="shrink-0 border-b px-3 py-2 text-xs">
        <button type="button" onClick={onEditPersona}
          title={persona?.persona || "给编排器起名、立人设"}
          className="text-[10px] text-muted-foreground hover:text-foreground hover:underline">
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
            {t.orch.map((o) => (
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
