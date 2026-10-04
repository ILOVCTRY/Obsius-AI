import { memo, useMemo, useState, type ReactNode } from "react"
import { eventStyle, eventSummary } from "@/lib/events"
import { fmtDateTime, fmtTime, fmtUtcDateTime, parseTs } from "@/lib/datetime"
import type { Approval, BBEvent, DecideApprovalResult } from "@/lib/types"
import { cn } from "@/lib/utils"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { ActivityGroup } from "@/components/chat/ActivityGroup"
import { FileCardList } from "@/components/chat/FileCard"
import { ApprovalCard, asSpawnAction } from "@/components/chat/ApprovalCard"
import { mergeTargets, targetsFromProse, targetsFromTool, type FileTarget } from "@/lib/fileTargets"
import type { ActivityStep } from "@/lib/activity"

const NOOP_DECIDE = async (): Promise<void> => { /* 无决策注入时占位（引用稳定，勿改） */ }

// 直播流渲染层（Claude Code 终端风格，DESIGN.md §12）：状态点 + 折叠行。
// lib/events.tsx 的 eventStyle/eventSummary 只管筛选/标签/摘要，本文件管布局：
// 命令对 → IN/OUT 块；思考 → 耗时行展开全文；错误/审批 → 醒目行；其余 → 简洁单行；
// 对话轮（2026-09-20 会话窗对话化）→ 人类一行 + 过程折叠摘要组 + Agent 回复 prose。
// **会话流改造（2026-10-03，cc-haha 风格）**：轮的 process 组由平铺行改为
// ActivityGroup 折叠摘要（一行灰色摘要 + 耗时，展开仍是原来的逐条 EventRow——
// **审计零回退**）；Agent 回复下挂文件卡（叙述提到的文件）。命令对/思考/通用单行
// 本身不变（它们既是展开态明细，也是「未成组单条」的呈现）。

export type StreamItem =
  | { type: "pair"; command: BBEvent; result?: BBEvent } // result 缺 = 运行中/后端悬空
  // cont（2026-09-28）：轮开启语被窗口裁剪出窗的孤儿终稿回复 → 行顶「承接更早」提示
  | { type: "single"; event: BBEvent; cont?: boolean }
  // 对话轮分组（2026-09-20）：human_note 开轮，过程事件入 process，无 step 的
  // agent.chat 收口 reply；replyStream=流式增量（agent.chat.delta 累计全文，
  // 终稿到达后置空）。仅「全部」筛选下 note/process/reply 齐备时自然生效，
  // 其余筛选回落平铺审计行（装配层把空轮退化为 single）
  | { type: "turn"; note: BBEvent; process: StreamItem[]; reply?: BBEvent; replyStream?: BBEvent }

export interface EventRowProps {
  item: StreamItem
  open: boolean
  onToggle: (primaryId: number, defaultOpen: boolean) => void
  roleNames: Record<string, string>
  /** orch.proposed 的行内「采纳」按钮等，由 LiveRoom 注入 */
  action?: ReactNode
  /** 待审批完整行（usePendingApprovals 按 approval_id 反查；命中则渲染审批问答卡） */
  approval?: Approval
  /** 审批卡提交回调（LiveRoom/工作台注入：sessionNote + decideApproval） */
  onDecideApproval?: (approval: Approval, decision: "approved" | "rejected", note: string) => Promise<DecideApprovalResult | void>
  /** skill.routed 命中技能：单击路由名跳设置页（deep link 由 LiveRoom dispatch，F12） */
  onRouteJump?: (e: BBEvent, name: string) => void
  /** 摘要资产反查（live-stream-ux A3）：LiveRoom 自建 assets 映射传入 */
  assetName?: (id: string) => string | undefined
  /** 文件卡动作的落点项目（会话流改造 2026-10-03）；缺省=文件卡只读展示 */
  pid?: string
}

// ---------- 活动摘要映射（会话流改造 2026-10-03） ----------
// StreamItem（轮的 process 组）→ ActivityStep[]：tool.call 取 name/duration_s/ok，
// 命令对取 cmd/duration_s，llm.thinking 计「思考」。turnStream.ts 契约零改动。

function toActivitySteps(process: StreamItem[]): ActivityStep[] {
  const steps: ActivityStep[] = []
  for (const p of process) {
    if (p.type === "pair") {
      const cmd = str(p.command.payload.cmd)
      steps.push({ kind: "command", name: "command", ts: parseTs(p.command.created_at).getTime(),
                   durationS: typeof p.result?.payload.duration_s === "number"
                     ? p.result.payload.duration_s : undefined,
                   dedupKey: firstLine(cmd).slice(0, 120),
                   failed: p.result ? p.result.payload.exit_code !== 0 || p.result.payload.timed_out === true
                     : false })
      continue
    }
    const e = p.type === "turn" ? p.note : p.event
    if (e.kind === "llm.thinking" || e.kind === "llm.thinking.delta") {
      steps.push({ kind: "thinking", name: "thinking", ts: parseTs(e.created_at).getTime(),
                   durationS: typeof e.payload.duration_s === "number" ? e.payload.duration_s : undefined })
    } else if (e.kind === "tool.call") {
      const name = str(e.payload.name) || "tool"
      steps.push({ kind: "tool", name, ts: parseTs(e.created_at).getTime(),
                   durationS: typeof e.payload.duration_s === "number" ? e.payload.duration_s : undefined,
                   dedupKey: name === "read_file" ? str((e.payload.args as Record<string, unknown>)?.path) : undefined,
                   failed: e.payload.ok === false })
    } else {
      steps.push({ kind: "tool", name: e.kind, ts: parseTs(e.created_at).getTime() })
    }
  }
  return steps
}

/** 轮内所有 tool.call / 命令对产出的文件目标（供叙述文件卡对账） */
function turnFileTargets(process: StreamItem[]): FileTarget[] {
  const out: FileTarget[] = []
  for (const p of process) {
    if (p.type === "pair") { out.push(...targetsFromTool("run_cmd", { cmd: p.command.payload.cmd })); continue }
    const e = p.type === "turn" ? p.note : p.event
    if (e.kind !== "tool.call") continue
    out.push(...targetsFromTool(str(e.payload.name),
      (e.payload.args ?? {}) as Record<string, unknown>,
      str(e.payload.result_head) || str(e.payload.result)))
  }
  return out
}

// ---------- live-stream-ux D1/E1（2026-09-23）共用小件 ----------

/** 行点击展开/折叠的 selection 守卫：拖选松键后的 click 会触发行折叠把选择丢掉
 * （用户反馈 #5）——选区非空时跳过 toggle，宁多一次点击不丢选择 */
export function selectionCollapsed(): boolean {
  const s = window.getSelection()
  return !s || s.isCollapsed
}

/** E1/E2 行内本地时间微标：HH:mm:ss（本地）小字；悬停双标=本地完整时间为主、
 * UTC 原值对照（消除「差 8 小时」错觉源，用户反馈 #6） */
export function TimeTag({ ts }: { ts?: string | null }) {
  if (!ts) return null
  return (
    <span className="shrink-0 font-mono text-[10px] text-muted-foreground/50"
          title={timeTitle(ts)}>
      {fmtTime(ts)}
    </span>
  )
}

/** E2 悬停双标：本地完整时间为主、UTC 原值对照（替代原 utcTitle 单 UTC 标） */
export const timeTitle = (ts?: string | null): string | undefined =>
  ts ? `${fmtDateTime(ts)}（本地） · UTC ${fmtUtcDateTime(ts)}` : undefined

type DotState = "running" | "ok" | "error" | "warn" | "idle"

function Dot({ state }: { state: DotState }) {
  return (
    <span
      className={cn(
        "mt-[5px] size-1.5 shrink-0 self-start rounded-full",
        state === "running" && "animate-pulse bg-(--status-running)",
        state === "ok" && "bg-(--status-idle)",
        state === "error" && "bg-(--status-error)",
        state === "warn" && "bg-(--status-approval)",
        state === "idle" && "bg-(--status-idle)",
      )}
    />
  )
}

// Claude Code 式调用字形（2026-09-28 会话窗三段式改造）：过程行用 ⏺（命令/工具）、
// ✻（思考）替代圆点——运行中脉冲主色、异常红、正常灰，与 Dot 并存（结论/审计行仍用点）
function Glyph({ state, char = "⏺" }: { state: DotState; char?: string }) {
  return (
    <span
      className={cn(
        "shrink-0 select-none text-[10px] leading-[1.45]",
        state === "running" && "animate-pulse text-primary",
        state === "ok" && "text-muted-foreground/40",
        state === "error" && "text-(--status-error)",
        state === "warn" && "text-(--status-approval)",
        state === "idle" && "text-muted-foreground/40",
      )}
    >
      {char}
    </span>
  )
}

// heredoc 写入特判（2026-09-28）：`cat > x << 'EOF'` 类命令在渗透流里高频出现且
// 脚本首行无信息量——折叠行不复述原文，改述「写入 x（heredoc N 行）」（Trae 式
// 写文件卡在无 diff 场景的替代品）；命令全文与输出仍在展开块里，审计零回退
function heredocOf(cmd: string): { file: string; lines: number; tag: string } | null {
  if (!cmd.includes("<<")) return null
  const m = /(?:cat|tee)\s+(?:-[A-Za-z]+\s+)*>{1,2}\s*([^\s<>'";|&]+)/.exec(cmd)
  if (!m) return null
  const tag = /<<\s*-?['"]?([A-Za-z_][A-Za-z0-9_]*)/.exec(cmd)?.[1] ?? "EOF"
  // 首行「cat > x << EOF」+ 末行「EOF」不计入内容行数
  return { file: m[1], lines: Math.max(cmd.split("\n").length - 2, 1), tag }
}

function fmtDuration(s: number): string {
  if (s >= 60) return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
  return s >= 10 ? `${Math.round(s)}s` : `${Math.round(s * 10) / 10}s`
}

function firstLine(s: string): string {
  const line = s.split("\n").find((l) => l.trim())
  return (line ?? "").trim()
}

// 错误/审批醒目行白名单（红点/琥珀点 + 左侧提示条；与 eventStyle defaultOpen=true 对齐）
const ERROR_KINDS = new Set([
  "llm.error", "audit.deny", "task.failed", "session.aborted",
  "binary.triage_failed", "finding.retracted", "task.lease_expired",
])
const WARN_KINDS = new Set([
  "approval.requested", "budget.soft_warning", "step.budget_extended",
  "session.budget_paused", "session.paused", "task.basis_stale_done",
  "task.reconcile_blocked", "orch.chain_stopped", "orch.proposed", "proposal.created",
])

function alertState(kind: string): DotState | null {
  if (ERROR_KINDS.has(kind)) return "error"
  if (WARN_KINDS.has(kind)) return "warn"
  return null
}

function str(v: unknown): string {
  return typeof v === "string" ? v : ""
}

// ---------- 命令对：⏺ <RUNTIME> <cmd 首行> + ⎿ 结果摘要行，展开 IN/OUT ----------
// 2026-09-28 三段式改造：折叠态从单行升级为「⏺ 调用 + ⎿ 首行结果」两行（Claude Code
// 式）——结果摘要说业务不说 exit 码（有输出取 stdout/stderr 首行，无输出才报 exit N），
// 耗时右侧小字；展开 IN/OUT 块原样保留（审计零回退）

function CommandPairRow({ command, result, open, onToggle }: {
  command: BBEvent; result?: BBEvent; open: boolean
  onToggle: (primaryId: number, defaultOpen: boolean) => void
}) {
  const cmd = str(command.payload.cmd)
  const hd = heredocOf(cmd)
  let state: DotState
  if (result) {
    const ok = result.payload.exit_code === 0 && !result.payload.timed_out
    state = ok ? "ok" : "error"
  } else {
    // 悬空兜底：后端崩溃/重启窗口里 command 无 result；10 分钟内视为运行中
    const age = Date.now() - parseTs(command.created_at).getTime()
    state = age < 10 * 60 * 1000 ? "running" : "idle"
  }
  const exitOk = result ? result.payload.exit_code === 0 && !result.payload.timed_out : false
  const dur = result?.payload.duration_s
  const outHead = str(result?.payload.stdout_head)
  const errHead = str(result?.payload.stderr_head)
  // ⎿ 摘要口径：失败优先取 stderr 首行（错误对人有用），成功取 stdout 首行，
  // 双空才退「exit N」；heredoc 写入行摘要由 ⏺ 行承担，⎿ 仍显执行结果
  const resLine = result
    ? firstLine(!exitOk && errHead ? errHead : outHead) || `exit ${String(result.payload.exit_code)}`
    : null
  return (
    <div className="w-full shrink-0 py-0.5">
      <div className="cursor-pointer rounded px-2 py-1 hover:bg-accent/40" title={timeTitle(command.created_at)}
           onClick={() => { if (selectionCollapsed()) onToggle(command.id, false) }}>
        <div className="flex items-baseline gap-2 text-xs">
          <Glyph state={state} />
          <span className="font-mono text-[11px] text-foreground/70">
            {str(command.payload.runtime).toUpperCase() || "CMD"}
          </span>
          <span className="min-w-0 flex-1 truncate font-mono text-foreground/90">
            {hd ? `写入 ${hd.file}（heredoc ${hd.lines} 行 · ${hd.tag}）` : firstLine(cmd)}
          </span>
          <TimeTag ts={command.created_at} />
        </div>
        {result && !open && (
          <div className="ml-5 mt-0.5 flex items-baseline gap-1.5 text-[11px] leading-relaxed">
            <span className="shrink-0 select-none text-muted-foreground/40">⎿</span>
            <span className={cn("min-w-0 flex-1 truncate", exitOk ? "text-muted-foreground/70" : "text-(--status-error)/80")}>
              {resLine}
            </span>
            {typeof dur === "number" && (
              <span className="shrink-0 font-mono text-[10px] text-muted-foreground/40">{fmtDuration(dur)}</span>
            )}
          </div>
        )}
        {!result && (
          <div className="ml-5 mt-0.5 flex items-baseline gap-1.5 text-[11px] leading-relaxed">
            <span className="shrink-0 select-none text-muted-foreground/40">⎿</span>
            <span className={state === "running" ? "animate-pulse text-primary/80" : "text-muted-foreground/50"}>
              {state === "running" ? "运行中…" : "（无结果返回）"}
            </span>
          </div>
        )}
        {open && (
          <div className="ml-5 border-l border-border/60 pl-3">
            <div className="text-[10px] font-mono text-muted-foreground/60">IN</div>
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted-foreground">
              {cmd}
            </pre>
            {result && (
              <>
                <div className="mt-1 text-[10px] font-mono text-muted-foreground/60">
                  OUT ·{" "}
                  <span className={exitOk ? "text-(--status-ok)" : "text-(--status-error)"}>
                    exit {String(result.payload.exit_code)}
                  </span>
                  {typeof dur === "number" && ` · ${fmtDuration(dur)}`}
                  {result.payload.timed_out === true && (
                    <span className="text-(--status-approval)"> · 超时</span>
                  )}
                </div>
                {str(result.payload.stdout_head) && (
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted-foreground/70">
                    {result.payload.stdout_head as string}
                  </pre>
                )}
                {str(result.payload.stderr_head) && (
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-(--status-error)/80">
                    {result.payload.stderr_head as string}
                  </pre>
                )}
                {!str(result.payload.stdout_head) && !str(result.payload.stderr_head) && (
                  <div className="text-[11px] text-muted-foreground/60">（无输出）</div>
                )}
              </>
            )}
            {!result && (
              <div className="text-[11px] text-muted-foreground/60">（无结果返回）</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- 思考：✻ 思考 Ns，展开全文（正文非等宽，克制黑客风约定） ----------

function ThinkingRow({ event, open, onToggle, streaming }: { event: BBEvent; open: boolean; onToggle: (id: number, d: boolean) => void; streaming?: boolean }) {
  const dur = event.payload.duration_s
  const preview = firstLine(str(event.payload.thinking)).slice(0, 80)
  return (
    <div className="w-full shrink-0 py-0.5">
      <div
        className="cursor-pointer rounded px-2 py-1 hover:bg-accent/40"
        title={timeTitle(event.created_at)}
        onClick={() => { if (selectionCollapsed()) onToggle(event.id, false) }}
      >
        <div className="flex items-baseline gap-2 text-xs">
          <Glyph state={streaming ? "running" : "idle"} char="✻" />
          <span className={streaming ? "min-w-0 flex-1 truncate text-primary italic" : "min-w-0 flex-1 truncate italic text-muted-foreground/70"}>
            {/* streaming 行无 duration（流未结束）；终稿行沿用「无 duration_s 不空心」约定 */}
            {streaming ? "思考中…" : typeof dur === "number" ? `思考 ${fmtDuration(dur)}` : "思考"}
            {(!open && preview) && (
              <span className="ml-2 not-italic text-muted-foreground/60">{preview}</span>
            )}
          </span>
          <TimeTag ts={event.created_at} />
        </div>
        {open && (
          <div className="ml-5 border-l border-border/60 pl-3">
            <div className="max-h-96 overflow-auto whitespace-pre-wrap text-[11px] leading-relaxed text-muted-foreground">
              {str(event.payload.thinking)}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- Agent 回复：正文即行（Claude Code 式 prose——无标签行、无摘要复述，
// 回复文本直接平铺；作者小字靠右，超长内部滚动） ----------

function AgentChatRow({ event }: { event: BBEvent }) {
  return (
    <div className="w-full shrink-0 py-0.5">
      <div className="rounded px-2 py-1" title={timeTitle(event.created_at)}>
        <div className="flex items-start gap-2 text-xs">
          <Dot state="idle" />
          {/* 2026-09-28：LLM 产出是 markdown（孤儿回复/任务轮叙述/顾问发言共用本行），
              此前 whitespace-pre-wrap 直显源码；与 AgentReplyRow 终稿同走 MarkdownView
              （无 rehype-raw 不渲染 raw HTML，防注入） */}
          <div className="min-w-0 flex-1">
            <MarkdownView content={str(event.payload.text)} prefix={`achat-${event.id}`}
              className="text-sm leading-relaxed text-foreground/90" />
          </div>
          <TimeTag ts={event.created_at} />
          <span className="shrink-0 pt-0.5 font-mono text-[10px] text-muted-foreground/60">{event.author}</span>
        </div>
      </div>
    </div>
  )
}

// ---------- 对话轮（2026-09-28 三段式改造，Claude Code 骨架） ----------
// ❯ 人类一行（顶部时间分隔线，替代气泡）→ ⏺/⎿ 过程行常显（每行可展开审计明细，
// 不再整组折叠——过程即内容）→ Agent 正文回复（MarkdownView 平铺，替代气泡；
// 流式中 pre-wrap+光标，终稿切 MarkdownView——react-markdown 无 rehype-raw 不渲染
// raw HTML，防注入）。

function HumanNoteRow({ event }: { event: BBEvent }) {
  const text = str(event.payload.text) || str(event.payload.title)
  return (
    <div className="w-full" title={timeTitle(event.created_at)}>
      <div className="px-1 text-sm leading-relaxed">
        <span className="mr-1.5 select-none font-mono text-primary">❯</span>
        <span className="whitespace-pre-wrap break-words text-foreground">{text}</span>
      </div>
    </div>
  )
}

function AgentReplyRow({ event, streaming }: { event: BBEvent; streaming?: boolean }) {
  const text = str(event.payload.text)
  if (streaming) {
    return (
      <div className="px-1 py-0.5 text-sm leading-relaxed text-foreground/90" title={timeTitle(event.created_at)}>
        <span className="whitespace-pre-wrap break-words">{text}</span>
        <span className="ml-0.5 inline-block h-3.5 w-[2px] animate-pulse bg-primary align-middle" />
      </div>
    )
  }
  return (
    <div className="px-1 py-0.5" title={timeTitle(event.created_at)}>
      {/* 回复正文交给外层消息流滚动，避免嵌套滚动容器造成下方空白和内容截断。 */}
      <MarkdownView content={text} prefix={`chat-${event.id}`}
        className="text-sm leading-relaxed text-foreground/90" />
    </div>
  )
}

// 轮本体不再有整组折叠（2026-09-28 过程常显），onToggle 仅透传给过程行自管展开态；
// 注意 open/onToggle 两 prop 保留在 EventRowProps 上（EventRowImpl 统一传参）
function TurnRow({ item, roleNames, onRouteJump, assetName, pid, approval, onDecideApproval }:
  EventRowProps & { item: Extract<StreamItem, { type: "turn" }> }) {
  // 过程组内行展开态自管（局部 map，不进 LiveRoom overrides——轮内细节不污染顶层折叠记忆）
  const [innerOpen, setInnerOpen] = useState<Map<number, boolean>>(new Map())
  const innerToggle = (id: number, d: boolean) =>
    setInnerOpen((m) => new Map(m).set(id, !(m.get(id) ?? d)))
  // 轮的「运行中」判据：replyStream 在场而终稿 reply 未到（turnStream 收口时清 replyStream）
  const live = !!item.replyStream && !item.reply
  const steps = useMemo(() => toActivitySteps(item.process), [item.process])
  const known = useMemo(() => turnFileTargets(item.process), [item.process])
  const replyText = str(item.reply?.payload.text) || str(item.replyStream?.payload.text)
  const targets = useMemo(
    () => mergeTargets(targetsFromProse(replyText, known), known), [replyText, known])
  return (
    <div className="w-full shrink-0 space-y-0.5 py-1">
      <HumanNoteRow event={item.note} />
      {/* 会话流改造（2026-10-03，cc-haha 风格）：过程组由平铺行改折叠摘要行——
          一行灰色摘要 + 耗时，点开仍是原来的逐条 EventRow（**审计零回退**）；
          运行中的轮恒展开（live）。单步不成组由 ActivityGroup 内部短路。 */}
      {item.process.length > 0 && (
        <div className="pl-2">
          <ActivityGroup steps={steps} live={live}
            failedCount={steps.filter((s) => s.failed).length}>
            {item.process.map((p) => {
              const pe = p.type === "pair" ? p.command
                : p.type === "turn" ? p.note : p.event
              const pk = p.type === "pair" ? "command"
                : p.type === "turn" ? "message.inbox" : p.event.kind
              const st = eventStyle(pk, pe.payload)
              return <EventRowImpl key={pe.id} item={p}
                open={innerOpen.get(pe.id) ?? st.defaultOpen}
                onToggle={innerToggle} roleNames={roleNames} onRouteJump={onRouteJump}
                assetName={assetName} pid={pid} approval={approval} onDecideApproval={onDecideApproval} />
            })}
          </ActivityGroup>
        </div>
      )}
      {item.replyStream && <AgentReplyRow event={item.replyStream} streaming />}
      {item.reply && <AgentReplyRow event={item.reply} />}
      <FileCardList targets={targets} pid={pid} />
    </div>
  )
}

// ---------- 审批（2026-10-04 改版：问答卡 + 紧凑审计行） ----------
// 审批模块已下线：待审批（usePendingApprovals 命中）渲染整块「审批问答卡」
// （选项单选 + 附加说明 + 提交，见 components/chat/ApprovalCard）；已决策或数据未到
// 退回紧凑琥珀行（无按钮）。决策后的 approval.{decision} 事件仍走通用审计行。

function ApprovalCardRow({ event }: { event: BBEvent }) {
  const op = str(event.payload.op) || "unknown"
  const risk = str(event.payload.risk)
  const summary = str(event.payload.summary)
  return (
    <div className="w-full shrink-0 py-1">
      <div className="rounded-lg border border-(--status-approval)/40 bg-(--status-approval)/5 px-3 py-2"
           title={timeTitle(event.created_at)}>
        <div className="flex items-center gap-2 text-xs">
          <span className="shrink-0">🔔</span>
          <span className="shrink-0 font-medium text-foreground/90">审批 · {op}</span>
          {risk && (
            <span className="shrink-0 rounded border border-(--status-approval)/40 px-1 font-mono text-[10px] text-(--status-approval)">
              {risk}
            </span>
          )}
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground/70" title={summary}>
            {summary}
          </span>
          <TimeTag ts={event.created_at} />
        </div>
      </div>
    </div>
  )
}

// ---------- 其余：简洁单行 / 错误审批醒目行（展开 JSON 详情） ----------

function EventRowImpl({ item, open, onToggle, roleNames, action, onRouteJump, assetName, pid, approval, onDecideApproval }: EventRowProps) {
  if (item.type === "turn") {
    return <TurnRow item={item} open={open} onToggle={onToggle} roleNames={roleNames}
      onRouteJump={onRouteJump} assetName={assetName} pid={pid} approval={approval} onDecideApproval={onDecideApproval} />
  }
  if (item.type === "pair") {
    // pair 折叠主键 = command.id（IN/OUT 一个开关整体折叠，LiveRoom overrides 统一存）
    return (
      <CommandPairRow command={item.command} result={item.result} open={open} onToggle={onToggle} />
    )
  }
  const e = item.event
  if (e.kind === "llm.thinking" || e.kind === "llm.thinking.delta") {
    return <ThinkingRow event={e} streaming={e.kind === "llm.thinking.delta"} open={open} onToggle={onToggle} />
  }
  // Agent 回复走专属 prose 行（正文即行，无标签/摘要复述）；无 text 的异常载荷退回通用 JSON 行。
  // cont（2026-09-28）：轮开启语被窗口裁剪出窗时的孤儿终稿回复，行顶提示承接，
  // 消除「轮内过程事件随窗口滑动在折叠/平铺间翻转、看似消失」的困惑
  if (e.kind === "agent.chat" && typeof e.payload.text === "string") {
    return (
      <>
        {item.type === "single" && item.cont && (
          <div className="shrink-0 pl-1 text-[10px] leading-relaxed text-muted-foreground/60">
            ⋯ 承接更早的对话轮（开启语已随窗口裁剪，上翻加载完整上下文）
          </div>
        )}
        <AgentChatRow event={e} />
      </>
    )
  }
  // 回复流式增量兜底行（2026-09-20 对话化）：正常在对话轮气泡内滚动（装配层），
  // 只有无轮上下文的孤儿 delta（escalation-only 回复等）落到这里
  if (e.kind === "agent.chat.delta") {
    return <AgentReplyRow event={e} streaming />
  }
  // 策略顾问发言（2026-09-20）：正文即行——顾问建议同样值得通读，展开 JSON 对人无意义
  if (e.kind === "advisor.intervention" && typeof e.payload.text === "string") {
    return <AgentChatRow event={e} />
  }
  // 内联审批（2026-10-04）：待审批命中 → 问答卡；否则紧凑审计行
  if (e.kind === "approval.requested") {
    const aid = str(e.payload.approval_id)
    if (approval && aid && approval.id === aid) {
      return (
        <div className="w-full shrink-0 py-1">
          <ApprovalCard approval={approval}
                        roleName={roleNames[asSpawnAction(approval)?.role ?? ""]}
                        onDecide={onDecideApproval ?? NOOP_DECIDE} />
        </div>
      )
    }
    return <ApprovalCardRow event={e} />
  }

  const style = eventStyle(e.kind, e.payload)
  const summary = eventSummary(e.payload, roleNames, { assetName }, e.kind)
  const alert = alertState(e.kind)
  const routedName = e.kind === "skill.routed" && typeof e.payload.name === "string" ? e.payload.name : null
  // F12：命中行路由名链接化——悬停变色+下划线，单击 stopPropagation 直跳技能（不触发行的
  // JSON 展开，原「双击跳转但单击先展开」的别扭取消）；命中摘要尾部（分数/命中词）自拼，
  // 与 events.tsx eventSummary 的 skill.routed 命中分支保持同口径
  const routedTail = routedName
    ? ` · ${String(e.payload.score ?? 0)} 分` +
      (Array.isArray(e.payload.matched) && e.payload.matched.length
        ? ` · ${(e.payload.matched as unknown[]).slice(0, 4).join(", ")}`
        : "")
    : null
  // 只在展开时才序列化详情（回放期全表重渲染时省掉几百次 stringify）
  const detail = open ? JSON.stringify(e.payload, null, 2) : ""
  // P4（2026-09-20 对话化）：bb_add_finding 成功 = 结论已入黑板链路图——行尾
  // 高亮徽章一眼可辨（对话轮登记结论的落点），finding id 取自结果头进悬停提示
  const mapped = e.kind === "tool.call" && e.payload.name === "bb_add_finding" && e.payload.ok === true
  const mappedId = mapped
    ? /finding=(find-[0-9a-z]+)/.exec(String(e.payload.result_head ?? ""))?.[1] ?? ""
    : ""
  return (
    <div className="w-full shrink-0 py-0.5">
      <div
        className={cn(
          "cursor-pointer rounded px-2 py-1 hover:bg-accent/40",
          alert === "error" && "border-l-2 border-(--status-error)/60",
          alert === "warn" && "border-l-2 border-(--status-approval)/60",
        )}
        onClick={() => { if (selectionCollapsed()) onToggle(e.id, style.defaultOpen) }}
        title={timeTitle(e.created_at)}
      >
        <div className="flex items-baseline gap-2 text-xs">
          {alert && <Dot state={alert} />}
          <span className={style.className}>{style.label}</span>
          {routedName && onRouteJump ? (
            <span className="min-w-0 flex-1 truncate font-mono">
              <span
                className="cursor-pointer hover:text-primary hover:underline"
                title={`单击查看技能 ${routedName}`}
                onClick={(click) => { click.stopPropagation(); onRouteJump(e, routedName) }}
              >
                {routedName}
              </span>
              <span className="text-muted-foreground">{routedTail}</span>
            </span>
          ) : summary && (
            <span className="min-w-0 flex-1 truncate"
                  title={typeof summary === "string" ? summary : undefined}>
              {summary}
            </span>
          )}
          {mapped && (
            <span className="shrink-0 text-[10px] text-(--status-ok)"
                  title={`结论已入黑板链路图 ${mappedId}`}>📌 结论已上图</span>
          )}
          {action}
          <TimeTag ts={e.created_at} />
          <span className="shrink-0 font-mono text-[10px] text-muted-foreground/60">{e.author}</span>
        </div>
        {open && detail !== "{}" && (
          <pre className="mt-1 max-h-48 overflow-auto rounded bg-card p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {detail}
          </pre>
        )}
      </div>
    </div>
  )
}

// memo 化（2026-09-20 会话页卡顿修复）：活跃会话的 llm.thinking.delta 高频到达会触发
// visible/items 全量重算，未 memo 时几千行同步重渲。比较器用**事件对象引用相等**——
// BBEvent 落库后不可变、跨重算引用稳定（思考流式 delta 的原位替换会换新对象，恰好
// 需要重渲），比 id 比较更精确；onToggle/onRouteJump 须 useCallback 保持稳定
// （LiveRoom 侧已收口），action JSX 仅 orch.proposed 行传入（量少，放行重渲无妨）。
function areRowEqual(a: EventRowProps, b: EventRowProps): boolean {
  if (a.open !== b.open || a.roleNames !== b.roleNames || a.action !== b.action
      || a.approval !== b.approval || a.onDecideApproval !== b.onDecideApproval
      || a.onToggle !== b.onToggle || a.onRouteJump !== b.onRouteJump
      || a.assetName !== b.assetName || a.pid !== b.pid) return false
  if (a.item.type !== b.item.type) return false
  if (a.item.type === "pair" && b.item.type === "pair") {
    return a.item.command === b.item.command && a.item.result === b.item.result
  }
  if (a.item.type === "single" && b.item.type === "single") {
    return a.item.event === b.item.event && a.item.cont === b.item.cont
  }
  if (a.item.type === "turn" && b.item.type === "turn") {
    // process 内对象每次装配重建，引用必变 → 轮有任何更新即重渲（保守正确优先）
    return a.item.note === b.item.note && a.item.reply === b.item.reply
      && a.item.replyStream === b.item.replyStream
  }
  return false
}

export const EventRow = memo(EventRowImpl, areRowEqual)
