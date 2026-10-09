import { useEffect, useMemo, useRef } from "react"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { fmtDateTimeMin, utcTitle } from "@/lib/datetime"
import { ActivityGroup } from "@/components/chat/ActivityGroup"
import { ThinkingBlock } from "@/components/chat/ThinkingBlock"
import { FileCardList } from "@/components/chat/FileCard"
import { TeamCard } from "@/components/chat/TeamCard"
import { mergeTargets, targetsFromProse, targetsFromTool, type FileTarget } from "@/lib/fileTargets"
import type { ActivityStep } from "@/lib/activity"
import type { BBEvent, OrchPersona, Team, TeamStatus } from "@/lib/types"

// 对话化编排器 M1/M2/M3（2026-09-21，DESIGN §6.4）：编排页签前两段——
// 顶部阶段目标条（M2 goal 闭环，编辑/清空经回调交父级弹层）+ 中部对话流（M1，
// orch.chat 事件组装成轮：human 消息开新轮、orch 回复追加；窗口裁剪造成的孤儿
// orch 消息自成一轮不丢）。编排动作事件流（任务派发/orch.*/goal.*/llm.error…）
// 由「❋ 审计」抽屉承载（2026-09-28 删除运行记录折叠区，本组件不含）。
//
// **2026-10-06 渲染向开窗事件流看齐**（用户拍板「统一直播间指挥与开窗窗口 UI」）：
// 去气泡——人类消息平铺 `❯` 一行、编排回复平铺 prose（无 rounded 气泡、作者小字靠右）、
// tool_trace 复用 `components/chat/ActivityGroup`（折叠摘要行，展开仍是逐条轨迹，
// 审计零回退）、思考复用 `components/chat/ThinkingBlock`（「已思考」单行折叠）、
// 回复挂 `FileCardList` 文件卡。与「开窗窗口」共用同一套会话流件，视觉统一。
// orch 回复走 MarkdownView（react-markdown 无 raw HTML，防注入）。

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

/** 团队生命周期事件（core/team store 发出，2026-10-06）——指挥流里渲染团队卡 */
export const TEAM_EVENT_KINDS = new Set([
  "team.created", "team.updated", "team.run.started", "team.member.updated", "team.run.finished",
])

type TimelineItem =
  | { kind: "turn"; key: number; human: BBEvent | null; orch: BBEvent[] }
  | { kind: "team"; key: string; teamId: string; name: string; status: TeamStatus; memberCount: number }
  | { kind: "notice"; key: number; label: string }

/** 事件序列 → 时间线：orch.chat 组装成轮（human 开新轮），team.* 落团队卡（每队一张，
 *  锚在首次出现处）。团队卡优先取实时 Team（`teamById`），缺省回落事件载荷快照。 */
function timelineOf(events: BBEvent[], teamById: Map<string, Team>): TimelineItem[] {
  const out: TimelineItem[] = []
  const seenTeam = new Set<string>()
  let cur: Extract<TimelineItem, { kind: "turn" }> | null = null
  for (const e of events) {
    if (TEAM_EVENT_KINDS.has(e.kind)) {
      const p = e.payload as { team_id?: unknown; team_name?: unknown; name?: unknown; status?: unknown; member_count?: unknown }
      const tid = typeof p.team_id === "string" ? p.team_id : ""
      if (!tid || seenTeam.has(tid)) continue
      seenTeam.add(tid)
      const live = teamById.get(tid)
      out.push({
        kind: "team", key: `team:${tid}`, teamId: tid,
        name: live?.name ?? String(p.team_name ?? p.name ?? tid),
        status: live?.status ?? (typeof p.status === "string" ? p.status as TeamStatus : "draft"),
        memberCount: live ? live.members.length : (typeof p.member_count === "number" ? p.member_count : 0),
      })
      cur = null
      continue
    }
    if (e.kind === "orch.compact") {
      // 指挥对话历史手动压缩（/compact，2026-10-06）：流内落一行分隔提示
      const p = e.payload as { summarized?: unknown } | null
      const n = typeof p?.summarized === "number" ? p.summarized : null
      out.push({ kind: "notice", key: e.id,
                 label: n != null ? `🧹 上下文已压缩（${n} 条旧对话压成摘要）` : "🧹 上下文已压缩" })
      cur = null
      continue
    }
    if (e.kind !== "orch.chat") continue
    const role = (e.payload as { role?: unknown } | null)?.role
    if (role === "human" || !cur) {
      cur = { kind: "turn", key: e.id, human: role === "human" ? e : null, orch: role === "human" ? [] : [e] }
      out.push(cur)
    } else {
      cur.orch.push(e)
    }
  }
  return out
}

function parseArgs(s: string): Record<string, unknown> | null {
  try {
    const o = JSON.parse(s)
    return o && typeof o === "object" ? o as Record<string, unknown> : null
  } catch { return null }
}

/** tool_trace → ActivityStep[]（摘要行用；read_file 带 path 去重键，与开窗窗口同口径） */
function traceSteps(trace: ChatTrace[]): ActivityStep[] {
  return trace.map((t) => {
    const args = t.name === "read_file" ? parseArgs(t.args) : null
    const p = args?.path
    return {
      kind: "tool" as const, name: t.name, ts: 0,
      dedupKey: typeof p === "string" ? p : undefined,
    }
  })
}

/** tool_trace → 文件目标（回复文件卡对账） */
function traceTargets(trace: ChatTrace[]): FileTarget[] {
  const out: FileTarget[] = []
  for (const t of trace) out.push(...targetsFromTool(t.name, parseArgs(t.args), t.result))
  return out
}

// 人类消息行（2026-10-06 去气泡）：平铺 `❯ <text>`，对齐工作台 HumanNote。
function HumanRow({ ev }: { ev: BBEvent }) {
  return (
    <div className="py-1 text-[13px] leading-relaxed" title={utcTitle(ev.created_at)}>
      <span className="mr-1.5 select-none font-mono text-primary">❯</span>
      <span className="whitespace-pre-wrap break-words text-foreground">{textOf(ev)}</span>
    </div>
  )
}

const WAKE_LABELS: Record<string, string> = {
  "team.run.finished": "团队运行结束",
  "budget.soft_warning": "预算软警",
  "phase.gate_open": "阶段出口门满足",
  "finding.new": "高危发现落库",
}

// 工具轨迹明细行（ActivityGroup 展开态）：⏺ 名称 + 参数 + ⎿ 结果，等宽克制风。
function TraceRow({ t }: { t: ChatTrace }) {
  return (
    <div className="rounded px-2 py-1 font-mono text-[11px]">
      <span className="text-foreground/90">⏺ {t.name}</span>
      {t.args && <span className="ml-1.5 break-all text-muted-foreground/70">{t.args}</span>}
      {t.result && <div className="mt-0.5 break-all text-muted-foreground/60">⎿ {t.result}</div>}
    </div>
  )
}

// 编排回复行（2026-10-06 去气泡）：正文平铺 prose（MarkdownView），作者·时间靠右小字，
// 对齐开窗窗口 EventRow.AgentChatRow；工具轨迹折成 ActivityGroup 摘要行；回复挂文件卡。
function OrchRow({ ev, name, pid }: {
  ev: BBEvent; name: string; pid?: string
}) {
  const trace = traceOf(ev)
  const text = textOf(ev)
  const p = ev.payload as { proactive?: unknown; triggers?: unknown } | null
  const triggers = Array.isArray(p?.triggers)
    ? p!.triggers.filter((x): x is string => typeof x === "string") : []
  const steps = traceSteps(trace)
  const known = traceTargets(trace)
  const targets = mergeTargets(targetsFromProse(text, known), known)
  return (
    <div className="py-0.5" title={utcTitle(ev.created_at)}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          {p?.proactive === true && (
            <div className="mb-0.5">
              <span className="rounded bg-amber-500/20 px-1 py-px text-[10px] text-amber-400"
                title={`异常订阅唤醒：${triggers.map((t) => WAKE_LABELS[t] ?? t).join("、") || "异常事件"}`}>
                🔔 主动唤醒
              </span>
            </div>
          )}
          {text && (
            <MarkdownView content={text} prefix={`orch-chat-${ev.id}`}
              className="text-sm leading-relaxed text-foreground/90" />
          )}
        </div>
        <span className="shrink-0 pt-0.5 font-mono text-[10px] text-muted-foreground/60">
          {name} · {fmtDateTimeMin(ev.created_at)}
        </span>
      </div>
      {trace.length > 0 && (
        <ActivityGroup steps={steps}>
          {trace.map((t, i) => <TraceRow key={i} t={t} />)}
        </ActivityGroup>
      )}
      <FileCardList targets={targets} pid={pid} />
    </div>
  )
}

export function OrchChatPane({ events, busy, persona, pid, teams = [], onEditPersona, orchRunning,
  onOpenTeamReport, onConfigureTeam }: {
  events: BBEvent[]
  busy: boolean
  persona: OrchPersona | null
  /** 文件卡动作落点项目（2026-10-06）；缺省=文件卡只读展示 */
  pid?: string
  /** 项目团队实时态（LiveRoom 轮询 /teams 传入）；团队卡优先取它，缺省回落事件载荷快照 */
  teams?: Team[]
  onEditPersona: () => void
  /** 团队卡「打开运行报告」（Phase E：Run 成员执行明细） */
  onOpenTeamReport?: (teamId: string) => void
  /** 团队卡「查看并配置」（Phase D：团队配置弹窗） */
  onConfigureTeam?: (teamId: string) => void
  // 编排 tick 运行中和强制接管（保留通用编排故障救济）
  orchRunning?: boolean
  onForceAcquire?: () => void
}) {
  const orchName = persona?.display_name || "编排器"
  const teamById = useMemo(() => new Map(teams.map((t) => [t.id, t])), [teams])
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
  const timeline = useMemo(
    () => timelineOf(events, teamById),
    [events, teamById])
  // 兜底：事件窗口已滚出（或项目内早先建的）团队在流末补列，保证「打开运行报告」始终可达。
  const anchoredTeamIds = useMemo(
    () => new Set(timeline.flatMap((it) => it.kind === "team" ? [it.teamId] : [])),
    [timeline])
  const looseTeams = useMemo(
    () => teams.filter((t) => !anchoredTeamIds.has(t.id)).slice(0, 6),
    [teams, anchoredTeamIds])
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  // 编排器思考流（tick 运行中「正在研判/规划」状态行的展开体）：研判与链轮期间
  // 唯一的流内进度反馈——orch.tick.started/llm.thinking 不进对话轮，之前全程静默。
  // 2026-10-06：折叠成一行「已思考」（复用 ThinkingBlock），全文在审计抽屉。
  const orchThinking = useMemo(() => events
    .filter((e) => (e.kind === "llm.thinking" || e.kind === "llm.thinking.delta")
      && e.author === "orchestrator")
    .slice(-12), [events])
  const latestThinking = orchThinking.length
    ? String(orchThinking[orchThinking.length - 1].payload.thinking ?? "")
    : ""
  // 自动滚底：仅在用户已接近底部时跟随（上翻历史时不抢滚动条）
  useEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [timeline, busy, orchThinking])
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
        className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-2">
        {timeline.length === 0 && looseTeams.length === 0 && !busy && (
          <div className="mx-auto mt-10 max-w-md rounded-lg border bg-card p-4 text-center text-xs leading-relaxed text-muted-foreground">
            <p className="mb-1 text-foreground">与{orchName}对话</p>
            问态势、问下一步、纠正方向……它可查黑板事件流、发任务、开窗（与编排一轮同闸门）。
            对话不推进编排游标、不消费指令——插队轮，回答完毕即返回。
          </div>
        )}
        {timeline.map((it) => it.kind === "team" ? (
          <TeamCard key={it.key} name={it.name} status={it.status} memberCount={it.memberCount}
            onOpenReport={() => onOpenTeamReport?.(it.teamId)}
            onConfigure={() => onConfigureTeam?.(it.teamId)} />
        ) : it.kind === "notice" ? (
          <div key={it.key} className="py-0.5 text-center font-mono text-[11px] text-muted-foreground/70">
            {it.label}
          </div>
        ) : (
          <div key={it.key} className="flex flex-col gap-1.5">
            {it.human && <HumanRow ev={it.human} />}
            {it.orch.map((o) => (
              <OrchRow key={o.id} ev={o} name={orchName} pid={pid} />
            ))}
          </div>
        ))}
        {looseTeams.map((t) => (
          <TeamCard key={`loose:${t.id}`} name={t.name} status={t.status} memberCount={t.members.length}
            onOpenReport={() => onOpenTeamReport?.(t.id)}
            onConfigure={() => onConfigureTeam?.(t.id)} />
        ))}
        {/* 编排 tick 运行中的流内进度反馈：一行状态（含时长/卡死警示），
            有思考时再补一行折叠「已思考」（ThinkingBlock，与开窗窗口同视觉）。 */}
        {orchRunning && (
          <div className="flex items-center gap-1.5 py-0.5 text-[11px] text-muted-foreground">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
            正在研判态势 / 规划下一步
            {tickStartedMin != null && (
              <span className={tickStartedMin >= 15 ? "text-(--status-approval)" : "text-muted-foreground/60"}>
                · 已 {tickStartedMin} 分钟{tickStartedMin >= 15 ? "（疑似卡死，可强制接管）" : ""}
              </span>
            )}
          </div>
        )}
        {orchThinking.length > 0 && (
          <ThinkingBlock content={latestThinking} active={orchRunning} className="wb-anim" />
        )}
        {busy && (
          <div className="flex items-center gap-2 py-0.5 text-sm text-muted-foreground">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
            {orchName}思考中…
          </div>
        )}
      </div>
    </div>
  )
}
