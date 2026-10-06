import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { ArrowUp, Paperclip, Plus, Square, X } from "lucide-react"

import { OrchChatPane, TEAM_EVENT_KINDS } from "./live/OrchChatPane"
import { TeamRunReport } from "./live/TeamRunReport"
import { TeamConfigDialog } from "@/components/team/TeamConfigDialog"
import { PersonaEditor } from "./live/editors"
import { EventRow, type StreamItem } from "./live/EventRow"
import { ApiError, api, pollJob } from "@/lib/api"
import { eventStyle } from "@/lib/events"
import { roleName, sessionLabel } from "@/lib/roles"
import { useEvents } from "@/lib/useEvents"
import { usePendingApprovals } from "@/lib/usePendingApprovals"
import { buildStreamItems } from "@/lib/turnStream"
import { fmtDateTimeMin } from "@/lib/datetime"
import type { Approval, AttachmentInfo, Asset, Autonomy, BBEvent, DecideApprovalResult, ModelInfo, OrchPersona, OrchProposal, OrchTickResult, PhaseGoal, ProjectUsage, RoleInfo, Session, Team, TeamMemberInput } from "@/lib/types"
import { StatusDot, type SessionStatus } from "@/components/StatusDot"
import { Button } from "@/components/ui/button"
import { ChatStats } from "@/components/chat/ChatStats"
import { cn } from "@/lib/utils"

// 核心页（DESIGN.md §12）：Agent 直播间——多会话页签 + 状态点 + 事件流 + 插话

// 滑动窗口（2026-09-23 会话窗口定稿）：DOM 展示上界与裁剪粒度（PAGE 与 useEvents 对齐；
// 超上界裁头部一批留余量，避免每条事件都触发裁剪）
const PAGE = 50
const MAX_DOM = 300

type Tab = { key: string; label: string; sessionId: string | null }

// 附件随发（2026-09-19）：chips 三态（上传中灰显 / 就绪可发 / 失败标红可重试）；File 留本地态供重试
type PendingFile = {
  key: string
  status: "uploading" | "ready" | "error"
  name: string
  size: number
  file: File
  att?: AttachmentInfo
}

// 输入行模式徽章（2026-09-19 Claude Code 化；2026-09-20 会话窗对话化改双模式；
// 2026-09-21 对话化编排器 M1：编排器态 C2 指令退役 → 「与编排对话」插队轮）：
// 输入模式收敛（阶段二，2026-09-28）：会话态=「对话/引导」单模式、编排器态=「与编排对话」
// 单模式——去模式徽章循环，「发任务/指派」语义由 Agent 在对话中自判（run_chat 意图判定升级任务）。
type InputMode = "note" | "orch"

// 会话态斜杠命令（2026-10-06）：目前仅 /compact——把会话持久对话历史（chat-<sid>.json）
// 的旧问答对压成摘要重写，后续对话从「摘要 + 最近若干条」续起。选中会话页签时可用。
const SESSION_SLASH_COMMANDS = [
  { cmd: "/compact", desc: "压缩本会话上下文：旧历史压成摘要，后续对话从摘要续起" },
]

function fmtBytes(n: number): string {
  if (n >= 1_048_576) return `${(n / 1_048_576).toFixed(1)}MB`
  if (n >= 1024) return `${Math.floor(n / 1024)}KB`
  return `${n}B`
}

// 自主级别（DESIGN §6.8）；档位行为已全部落地（批 4 开窗审批 / 批 5 自动链 / 批 6 L0 提案）
// 编排器态 chip 通用样式（2026-09-19 顶部第二行工具栏并入 composer 后的统一胶囊钮）
const ORCH_CHIP_CLS =
  "flex h-7 items-center rounded-full border px-2.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"

const LEVEL_HINTS: Record<"L0" | "L1" | "L2", string> = {
  L0: "L0 全手动·提案模式：编排只发提案（事件流行内「采纳」），任务/开窗均以人类名义落地",
  L1: "L1 任务自动 · 开窗需人批：编排直接发任务并自动跑队列，开窗转审批收件箱",
  L2: "L2 全自动：自动 worker + 自动续 tick 成链（受链预算/收敛/节流约束，安全审批永远强制）",
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

/** 错误文案命中 LLM 配额/限流特征（后端 llm.error 的 kind_hint 同一特征串，§6.9） */
function quotaHint(msg: string | null | undefined): boolean {
  return !!msg && /429|quota|insufficient|余额|配额|rate.?limit/i.test(msg)
}

/** C2 自动派生状态灯四态（§6.9，2026-09-18；goal 统一后挂 🧠 自主档 chip）：
 *  灰=auto_derive 关；琥珀=paused/Token 预算耗尽/上次派生 error·empty；绿=其余（运行中）。
 *  label=状态词（弹层内点+文字直接可读）；title=悬停详情。 */
function deriveLamp(usage: ProjectUsage | null | undefined): { cls: string; textCls: string; title: string; label: string } {
  if (!usage?.auto_derive) {
    return { cls: "bg-muted-foreground/40", textCls: "text-muted-foreground",
             title: "自动派生已关闭", label: "已关闭" }
  }
  const last = usage.derive?.last_result || ""
  const budgetOut = usage.tokens.pct !== null && usage.tokens.pct >= 100
  const blocked = usage.paused || budgetOut
    || last.startsWith("error") || last.startsWith("empty")
  const when = usage.derive?.last_at
  const deriveNote = when
    ? `上次派生 ${fmtDateTimeMin(when)}${last ? ` · ${last}` : ""}`
    : "尚未派生过（等任务队列空时判定）"
  if (blocked) {
    const why = usage.paused ? "项目暂停" : budgetOut ? "Token 预算耗尽"
      : last.startsWith("error") ? "上次派生失败" : "上次派生零发布"
    return {
      cls: "bg-(--status-approval)", textCls: "text-(--status-approval)",
      title: `自动派生被闸拦住（${why}） · ${deriveNote}`,
      label: `被拦住（${why}）`,
    }
  }
  return {
    cls: "bg-(--status-ok)", textCls: "text-(--status-ok)",
    title: `自动派生运行中：任务空时自动派生新任务 · ${deriveNote}`,
    label: "运行中",
  }
}

/** cap / 预算编辑弹层（受控于 usage 快照，保存时整体 PATCH autonomy） */
function BudgetPopover({ usage, onClose, onSave }: {
  usage: ProjectUsage
  onClose: () => void
  onSave: (patch: Partial<Autonomy>) => Promise<void>
}) {
  const [cap, setCap] = useState(String(usage.sessions_cap))
  const [chain, setChain] = useState(String(usage.max_chain_ticks))
  const [tokenB, setTokenB] = useState(usage.token_budget ? String(usage.token_budget) : "")
  const [taskB, setTaskB] = useState(usage.task_budget ? String(usage.task_budget) : "")
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState("")

  const numOrNull = (s: string): number | null =>
    s.trim() === "" ? null : Math.floor(Number(s))

  const save = async () => {
    const patch: Partial<Autonomy> = {
      sessions_cap: Number(cap),
      max_chain_ticks: Number(chain),
      token_budget: numOrNull(tokenB),
      task_budget: numOrNull(taskB),
    }
    const inRange = (v: number | undefined): v is number =>
      typeof v === "number" && Number.isInteger(v) && v >= 1 && v <= 20
    if (!inRange(patch.sessions_cap) || !inRange(patch.max_chain_ticks)) {
      setErr("会话上限 1..20，自动链轮数 1..20")
      return
    }
    if ((patch.token_budget ?? 1) <= 0 || (patch.task_budget ?? 1) <= 0) {
      setErr("预算必须是正整数（留空 = 不限）")
      return
    }
    setSaving(true)
    setErr("")
    try {
      await onSave(patch)
      onClose()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  const field = "h-7 w-full rounded-md border bg-background px-2 text-xs font-mono"
  const hit = usage.tokens.cache_hit
  return (
    <div className="absolute right-0 top-9 z-20 w-60 rounded-lg border bg-popover p-3 text-xs shadow-md">
      <p className="mb-2 text-muted-foreground">资源上限与预算（任何档位安全层不放松）</p>
      {/* M1 prompt caching 观测（2026-09-23）：累计用量 + 缓存命中率 */}
      <p className="mb-2 rounded-md bg-accent/40 px-2 py-1 font-mono text-[11px] text-muted-foreground">
        累计 {fmtTokens(usage.tokens.used)}
        {usage.tokens.budget ? ` / ${fmtTokens(usage.tokens.budget)}（${Math.round((usage.tokens.pct ?? 0) * 100)}%）` : ""}
        {hit != null && hit > 0
          ? <> · 缓存命中 <span className="text-(--status-ok)">{Math.round(hit * 100)}%</span></>
          : ""}
      </p>
      <label className="mb-1 block text-muted-foreground">活跃会话上限 sessions_cap</label>
      <input className={field} type="number" min={1} max={20} value={cap}
             onChange={(e) => setCap(e.target.value)} />
      <label className="mb-1 mt-2 block text-muted-foreground">自动链最大轮数 max_chain_ticks</label>
      <input className={field} type="number" min={1} max={20} value={chain}
             onChange={(e) => setChain(e.target.value)} />
      <label className="mb-1 mt-2 block text-muted-foreground">Token 预算（留空不限；80% 预警，超支硬拦自主动作）</label>
      <input className={field} type="number" min={1} placeholder="不限" value={tokenB}
             onChange={(e) => setTokenB(e.target.value)} />
      <label className="mb-1 mt-2 block text-muted-foreground">自主任务数预算（编排发布；留空不限）</label>
      <input className={field} type="number" min={1} placeholder="不限" value={taskB}
             onChange={(e) => setTaskB(e.target.value)} />
      {err && <p className="mt-2 text-(--status-error)">{err}</p>}
      <Button size="sm" className="mt-3 w-full" disabled={saving} onClick={save}>
        {saving ? "保存中…" : "保存"}
      </Button>
    </div>
  )
}

/** 阶段目标编辑弹层（对话化编排器 M2，§4.3；goal 统一后=唯一目标判据层）：
 *  text 一句话 + criteria 验收判据（一行一条，判据第一优先源——自动派生 L1
 *  判跳吃它）+ phase 可空；保存=goal.confirm、清空=goal.clear（事件留痕，
 *  变更历史可回放）。goal 注入编排 tick 与对话轮系统提示。
 *  「应用模板」下拉把内置/用户判据模板内容填入判据框（存删模板 API 端点保留）。 */
/** 行动边界编辑弹层（goal 统一 2026-09-22：原「作战计划/红队行动」两轨弹层
 *  退役——目标与判据归 🎯 阶段目标（GoalEditor），自动派生开关归 🧠 自主档；
 *  本弹层只留轨级行动边界：redteam ROE 四要素留档编辑。ROE 不再强制
 *  （缺省=按 pentest 上限兜底，usage.roe_complete 提示补全）。仅 redteam 渲染。 */
function RoePopover({ usage, onClose, onSave }: {
  usage: ProjectUsage
  onClose: () => void
  onSave: (patch: Record<string, unknown>) => Promise<void>
}) {
  const [targets, setTargets] = useState(usage.redteam_roe?.targets ?? "")
  const [window_, setWindow_] = useState(usage.redteam_roe?.window ?? "")
  const [exclusions, setExclusions] = useState(usage.redteam_roe?.exclusions ?? "")
  const [approver, setApprover] = useState(usage.redteam_roe?.approver ?? "")
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState("")

  const save = async () => {
    // ROE 不再强制——留空的键会被服务端归一化剥除（行为按 pentest 上限兜底）
    setSaving(true)
    setErr("")
    try {
      await onSave({
        redteam_roe: {
          targets: targets.trim(), window: window_.trim(),
          exclusions: exclusions.trim(), approver: approver.trim(),
        },
      })
      onClose()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  const field = "w-full rounded border bg-background p-1.5 text-[11px]"
  return (
    <div className="absolute left-0 top-9 z-20 w-80 rounded-lg border bg-popover p-3 text-xs shadow-md">
      <p className="mb-2 text-muted-foreground">
        行动边界：红队 ROE 留档（§6.9；安全红线不放松）——目标与判据在 🎯 阶段目标
      </p>
      <div className="mb-2 space-y-1 rounded border border-(--status-approval)/40 p-2">
        <p className="text-[10px] text-(--status-approval)">
          ROE 四要素{usage.roe_complete === false ? "（未核验齐全：行动按渗透测试上限兜底）" : "（留档审计；留空的项按未授权处理）"}
        </p>
        <input className={field} value={targets} onChange={(e) => setTargets(e.target.value)}
               placeholder="① 授权目标清单" />
        <input className={field} value={window_} onChange={(e) => setWindow_(e.target.value)}
               placeholder="② 时间窗口" />
        <input className={field} value={exclusions} onChange={(e) => setExclusions(e.target.value)}
               placeholder="③ 禁止事项" />
        <input className={field} value={approver} onChange={(e) => setApprover(e.target.value)}
               placeholder="④ 授权人" />
      </div>
      {err && <p className="mt-2 text-(--status-error)">{err}</p>}
      <Button size="sm" className="mt-2 w-full" disabled={saving} onClick={save}>
        {saving ? "保存中…" : "保存"}
      </Button>
    </div>
  )
}

const FILTERS = [
  { key: "all", label: "全部", match: () => true },
  { key: "thinking", label: "思考", match: (k: string) =>
      k === "llm.thinking" || k === "llm.thinking.delta" || k === "agent.chat"
      || k === "agent.chat.delta" },
  { key: "decision", label: "决策", match: (k: string) =>
      k.startsWith("task.") || k.startsWith("session.") || k.startsWith("approval.") ||
      k.startsWith("orch.") || k.startsWith("mission.") ||
      k === "project.digest" || k === "finding.new" || k === "advisor.intervention" ||
      k === "goal.confirm" || k === "goal.clear" ||
      k === "phase.changed" || k === "phase.gate_open" ||
      k === "verify.result" },
  { key: "route", label: "路由", match: (k: string) =>
      k === "skill.routed" || k === "skill.open" || k === "kb.open" || k === "kb.search" },
  { key: "command", label: "命令", match: (k: string) =>
      k === "command" || k === "command.result" || k === "audit.deny" },
  { key: "tools", label: "工具", match: (k: string) => k === "tool.call" },
  { key: "finding", label: "发现", match: (k: string) =>
      k.startsWith("finding.") || k === "func.upsert" || k === "asset.new" },
] as const

// 稀疏组保底（2026-09-28）：思考/命令/工具高频刷屏会在几分钟内把低频关键事件
// 挤出纯计数窗口——「路由/决策筛选时间一久就空」的根因。裁剪时对各稀疏组
// 额外保留最近 KEEP_SPARSE 条；受保底事件散布窗口中段，loadEarlier 只向头部
// 之前补页，语义不变。
const KEEP_SPARSE = 30
const SPARSE_FLOORS: { match: (kind: string) => boolean; keep: number }[] = FILTERS
  .filter((f) => f.key === "decision" || f.key === "route" || f.key === "finding")
  .map((f) => ({ match: f.match, keep: KEEP_SPARSE }))

function sessionStatus(mine: BBEvent[]): SessionStatus {
  if (mine.some((e) => e.kind === "session.finished")) return "finished"
  // 控制事件优先：paused 后无 resumed/aborted/finished → 已暂停（§3 会话控制）
  for (let i = mine.length - 1; i >= 0; i--) {
    const k = mine[i].kind
    if (k === "session.paused") return "paused"
    if (k === "session.resumed" || k === "session.aborted" || k === "session.finished") break
  }
  // 余晖退役（2026-09-29）：「最近 2 分钟有动静→running」曾把 session.spawned
  // 等生命周期事件误判成执行态——新开窗假执行中 2 分钟。执行态唯一权威=
  // worker_running（服务端 Job 在跑，tabStatus 恒优先），此处只留终态推导。
  const last = mine[mine.length - 1]
  void last
  return "idle"
}

export function LiveRoom({ pid, focusSession }: { pid: string; focusSession?: { sid: string; n: number } | null }) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeTab, setActiveTab] = useState<string>("__orch")
  // 事件流分页（2026-09-17）：首屏最新 50 条，上翻懒加载更早；已加载缓存常驻不重拉。
  // 会话维度分页（2026-09-23 会话窗口）：会话页签走会话源（打开即拉本会话历史，
  // 修「已结束会话页签近乎空白」——全局最新 50 条里该会话可能零事件），编排走全局源
  const dataSid = activeTab !== "__orch" ? activeTab : null
  const { events, connected, loadedAll, loadingEarlier, loadEarlier, trimDom } = useEvents(pid, dataSid)
  // 编排器对话历史按 kind 全量补拉（2026-09-26）：首屏只水合尾部 300 条全局事件，
  // 长跑项目 orch.chat 轮几乎必然落窗外——编排页签打开时单独拉全，与直播流按 id 归并
  // （新增 orch.chat 经 WS/轮询进 events，归并去重不重复渲染）
  const [orchHistory, setOrchHistory] = useState<BBEvent[]>([])
  const orchHistorySeq = useRef(0)
  const refreshOrchHistory = useCallback(() => {
    if (activeTab !== "__orch") return
    const seq = ++orchHistorySeq.current
    api.eventsByKind(pid, "orch.chat").then((es) => {
      if (seq === orchHistorySeq.current) setOrchHistory(es)
    }).catch(() => {})
  }, [pid, activeTab])
  useEffect(() => {
    refreshOrchHistory()
  }, [refreshOrchHistory])
  useEffect(() => {
    if (activeTab !== "__orch") return
    const wakeKinds = new Set(["team.created", "team.run.started", "team.run.finished", "session.spawned"])
    if (!events.some((event) => wakeKinds.has(event.kind))) return
    refreshOrchHistory()
  }, [events, activeTab, refreshOrchHistory])
  const orchEvents = useMemo(() => {
    if (orchHistory.length === 0) return events
    const seen = new Set(orchHistory.map((e) => e.id))
    return [...orchHistory, ...events.filter((e) => !seen.has(e.id))]
      .sort((a, b) => a.id - b.id)
  }, [orchHistory, events])
  // 团队实时态（2026-10-06）：指挥页签团队卡的数据源（卡优先取实时态，缺省回落事件快照）。
  // 仅指挥页签激活时轮询，避免其它页签空跑。
  const [teams, setTeams] = useState<Team[]>([])
  const [teamConfigId, setTeamConfigId] = useState<string | null>(null)
  const [teamReportId, setTeamReportId] = useState<string | null>(null)
  useEffect(() => {
    if (!pid || activeTab !== "__orch") return
    let alive = true
    const load = () => api.teams(pid).then((ts) => { if (alive) setTeams(ts) }).catch(() => {})
    load()
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, activeTab])
  // 审计抽屉（2026-09-28 会话窗三段式改造）：主区常显对话轮形态，类型筛选迁入右侧
  // 抽屉——抽屉复用同一 events 窗口（inTabScope 圈定）+ 本地筛选态平铺渲染
  const [auditOpen, setAuditOpen] = useState(false)
  const [auditFilter, setAuditFilter] = useState<string>("all")
  // 内联审批（2026-10-04 改版）：待审批完整行由 usePendingApprovals 供（事件载荷字段不足），
  // 新 approval.requested 事件到达即唤醒重拉，卡片「即弹」不等下一拍轮询
  const aprWake = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) if (events[i].kind === "approval.requested") return events[i].id
    return 0
  }, [events])
  const { byId: approvalById } = usePendingApprovals(pid, aprWake)
  // auto-attack（2026-09-28）：L2 自动渗透——三档弹层开关 + 研判/开跑/停止进行中
  const [overrides, setOverrides] = useState<Map<number, boolean>>(new Map())
  const [remark, setRemark] = useState("")
  const [jobInfo, setJobInfo] = useState<string | null>(null)
  const [spawning, setSpawning] = useState(false)
  const [roles, setRoles] = useState<RoleInfo[]>([])
  const [role, setRole] = useState("_generalist")
  const [modelInfo, setModelInfo] = useState<ModelInfo | null>(null)
  const [provider, setProvider] = useState("")
  const [model, setModel] = useState("")
  // 切模型（2026-09-19 迁入输入行徽章浮层）：会话模型本地记忆（Session 无 model 字段，无值显「默认模型」）
  const [sessionModel, setSessionModel] = useState<Record<string, { provider: string; model: string }>>({})
  const [modelPopOpen, setModelPopOpen] = useState(false)
  const [popProvider, setPopProvider] = useState("")
  // 附件随发（2026-09-19）：+ 钮选文件即上传为 artifact，发送（发任务/引导会话）携带就绪附件
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  // E8：开窗步数预算（留空=后端默认 200）与输入框模式（随上下文收敛，见 useEffect 下方）
  const [maxSteps, setMaxSteps] = useState("")
  const [inputMode, setInputMode] = useState<InputMode>("note")
  // 排队引导条（2026-09-19 轮末语义）：note 发送后 human_note 留收件箱等下一轮
  // 认领期注入，此处仅组件内存的可见排队态；task.claimed 事件到达即清（已注入）
  const [queuedNotes, setQueuedNotes] = useState<{ key: string; sid: string; text: string; attCount: number }[]>([])
  // 批 6 L0 提案采纳态只存内存（刷新后可再次采纳，不做服务端去重）
  const [adopted, setAdopted] = useState<Set<number>>(new Set())
  const [adoptingId, setAdoptingId] = useState<number | null>(null)
  // 项目自主配置/用量（§6.8，5s 轮询；闸门服务端实时重读，改配置即时生效）
  const [usage, setUsage] = useState<ProjectUsage | null>(null)
  // 项目场景轨（R2：🎯 行动边界弹层仅 redteam 轨渲染）
  const [track, setTrack] = useState<string | null>(null)
  // 作战模式弹层（R2 退役切换：redteam 轨专属 ROE/mission 编辑）
  const [modeOpen, setModeOpen] = useState(false)
  const [budgetOpen, setBudgetOpen] = useState(false)
  // 编排器工具栏并入 composer（2026-09-19）：开窗/自主档/更多三枚新弹层开关
  const [winOpen, setWinOpen] = useState(false)
  const [autoOpen, setAutoOpen] = useState(false)
  const [moreOpen, setMoreOpen] = useState(false)
  const [savingAuto, setSavingAuto] = useState(false)
  // 对话化编排器（M1/M2/M3，2026-09-21）：对话轮在跑（输入不禁用，busy 409 提示）
  // + goal/persona 元数据 + 编辑弹层开关
  const [orchBusy, setOrchBusy] = useState(false)
  const [orchMeta, setOrchMeta] = useState<{ phase_goal: PhaseGoal | null; persona: OrchPersona | null } | null>(null)
  const [personaOpen, setPersonaOpen] = useState(false)
  const refreshOrchMeta = () =>
    api.projectGoal(pid).then(setOrchMeta).catch(() => {})
  useEffect(() => {
    refreshOrchMeta()
  }, [pid])
  const refreshUsage = () =>
    api.getProject(pid).then((p) => {
      setUsage(p.usage); setTrack(p.track ?? null)
    }).catch(() => {})
  useEffect(() => {
    refreshUsage()
    const t = setInterval(refreshUsage, 5000)
    return () => clearInterval(t)
  }, [pid])

  const saveAutonomy = async (patch: Partial<Autonomy>) => {
    if (!usage || savingAuto) return
    setSavingAuto(true)
    const body: Autonomy = {
      level: usage.level, paused: usage.paused, sessions_cap: usage.sessions_cap,
      max_chain_ticks: usage.max_chain_ticks, token_budget: usage.tokens.budget,
      task_budget: usage.tasks.budget, ...patch,
    }
    try {
      await api.patchProjectConfig(pid, { autonomy: body })
      await refreshUsage()
    } catch (e) {
      setJobInfo(`自主配置保存失败：${e}`)
    } finally {
      setSavingAuto(false)
    }
  }

  const refreshSessions = () =>
    api.sessions(pid).then(setSessions).catch(() => setSessions([]))
  useEffect(() => {
    refreshSessions()
    const t = setInterval(refreshSessions, 5000)
    return () => clearInterval(t)
  }, [pid])
  useEffect(() => {
    // 角色清单（开窗下拉 + 编排 allowed_roles 多选的数据源，项目场景轨驱动）
    api.listRoles(pid).then((rs) => {
      setRoles([...rs.filter((r) => r.role === "_generalist"), ...rs.filter((r) => r.role !== "_generalist")])
    }).catch(() => setRoles([]))
  }, [pid])
  useEffect(() => {
    // 供应商+模型清单（开窗两级选择，DESIGN.md §8）
    api.models().then((m) => {
      setModelInfo(m)
    }).catch(() => {})
  }, [])
  useEffect(() => {
    refreshSessions() // 会话开窗事件后刷新页签
  }, [events.length]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    // 切到某会话页签即视为读其系统私信：即时清红点并落已读，5s 轮询随后对齐
    if (!activeTab.startsWith("sess-")) return
    const sid = activeTab
    setSessions((ss) => ss.map((s) => (s.id === sid && s.unread ? { ...s, unread: 0 } : s)))
    api.readSessionInbox(sid).catch(() => {})
  }, [activeTab])
  // 双击任务卡/任务流节点跳来——直开该会话页签
  useEffect(() => {
    if (!focusSession?.sid) return
    setActiveTab(focusSession.sid)
  }, [focusSession?.n, focusSession?.sid])  // eslint-disable-line react-hooks/exhaustive-deps
  // 角色中文名映射（事件流「提议开窗」摘要 / 会话页签显中文）
  const roleNames = useMemo(
    () => Object.fromEntries(roles.map((r) => [r.role, r.name || r.role])),
    [roles])
  // live-stream-ux A3（2026-09-23）：资产 id→value 反查映射——工具摘要（bb_asset_status/
  // bb_query/finding 行「目标 x」）显资产值不显不可读 id；WS asset.new 增量维护
  const [assets, setAssets] = useState<Asset[]>([])
  useEffect(() => {
    // dataSid 入依赖：会话源下 WS 增量只捕获本会话的 asset.new，切页签重拉全量
    // 补齐跨页签映射（资产 id→value 反查是全项目维度）
    api.assets(pid).then(setAssets).catch(() => {})
  }, [pid, dataSid])
  useEffect(() => {
    if (!events.length) return
    setAssets((prev) => {
      let changed = false
      const next = [...prev]
      for (const e of events) {
        if (e.kind === "asset.new" && typeof e.payload.asset_id === "string"
            && typeof e.payload.value === "string") {
          const id = e.payload.asset_id
          if (!next.some((a) => a.id === id)) {
            next.push({
              id, project_id: pid, type: String(e.payload.type ?? ""),
              value: e.payload.value, parent_id: null, status: "open",
              meta: {}, author: String(e.author ?? ""), created_at: e.created_at,
            })
            changed = true
          }
        }
      }
      return changed ? next : prev
    })
  }, [events, pid])
  const assetName = useCallback(
    (id: string) => assets.find((a) => a.id === id)?.value, [assets])
  // 页签：指挥（无 session_id 的编排事件；M3 起显拟人显示名）/ 各会话（已关闭的隐藏；
  // 2026-09-19 起 ×=结束会话，A1 摘离退役）。同名会话（多个同角色窗）无法区分——
  // 2026-09-28：标签重复时追加短 id 后缀（如「侦察·1960f」），消除「不同会话显示相同
  // 内容」的识别困惑；title 悬停给完整 id。
  const tabs: Tab[] = useMemo(() => {
    const live = sessions.filter((s) => s.status !== "closed")
    const base = live.map((s) => ({ key: s.id, label: sessionLabel(s, roleNames), sessionId: s.id }))
    const seen = new Map<string, number>()
    for (const t of base) seen.set(t.label, (seen.get(t.label) ?? 0) + 1)
    const labeled = base.map((t) => ({
      ...t,
      label: (seen.get(t.label) ?? 0) > 1 ? `${t.label}·${t.sessionId.slice(-4)}` : t.label,
    }))
    return [
      { key: "__orch", label: orchMeta?.persona?.display_name || "指挥", sessionId: "__orch" },
      ...labeled,
    ]
  }, [sessions, roleNames, orchMeta])
  // 会话收件箱未读数（撤回传播系统私信；与审批收件箱分设，DESIGN §6.7 的 1.5）
  const unreadBySid = useMemo(
    () => new Map(sessions.map((s) => [s.id, s.unread ?? 0])), [sessions])

  // 页签圈定（2026-09-28 抽出复用）：主区 visible 与审计抽屉 auditVisible 共用同一
  // 圈定口径，只差类型筛选层
  const inTabScope = useCallback((e: BBEvent) => {
    if (activeTab === "__orch") {
      // 团队生命周期事件（2026-10-06）：无 session_id，指挥页签对话流渲染团队卡
      if (TEAM_EVENT_KINDS.has(e.kind)) return true
      if (e.session_id) return false
      if (e.author === "orchestrator") return true
      // 编排器/顾问源 LLM 失败也属编排视角（worker 源不进，看会话页签/全部）
      return e.kind === "llm.error"
        && (e.payload.source === "orchestrator" || e.payload.source === "planner")
    }
    return e.session_id === activeTab
  }, [activeTab])

  const visible = useMemo(
    () => events.filter((e) => inTabScope(e)),
    [events, inTabScope])
  // 审计抽屉数据（2026-09-28）：同一 events 窗口按抽屉筛选态平铺；「全部」下经
  // buildStreamItems 仍成轮（与主区一致），其余筛选 note 被滤掉自然退化为平铺审计行
  const auditVisible = useMemo(() => {
    const f = FILTERS.find((x) => x.key === auditFilter)!
    return events.filter((e) => inTabScope(e) && f.match(e.kind))
  }, [events, inTabScope, auditFilter])
  const auditItems = useMemo(() => buildStreamItems(auditVisible), [auditVisible])
  const auditShown = useMemo(() => [...auditItems].reverse(), [auditItems])

  const listRef = useRef<HTMLDivElement>(null)
  const [streamShort, setStreamShort] = useState(false)
  // 命令对配对（Claude Code 式渲染，DESIGN.md §12）：command/command.result 就地合并为
  // 一条折叠行。新数据按 payload.call_id 配对；存量旧事件无 call_id，降级为「同会话最近
  // 未闭合 command」游标配对（run_cmd 串行执行，会话内相邻性成立），session_id 不符宁走
  // 孤儿渲染不错配。visible 升序 = 落库序（events 表 id 单调，WS 按 id 推进），无需处理乱序。
  // 装配逻辑 2026-09-25 抽至 lib/turnStream.ts（ConversationPane 共用，行为不变）
  const items = useMemo<StreamItem[]>(() => buildStreamItems(visible), [visible])
  // 倒序：最新事件 = DOM 首子 = column-reverse 视觉最底 = 滚动原点 0。
  // 贴底由浏览器布局保证（scrollTop 初始/钳制在 0 即最新），零脚本滚动零竞态；
  // 上翻阅读的位置稳定交给浏览器 scroll anchoring。DOM 只渲染已加载的分页
  // （首屏 50 条，上翻按 50 条/批从后端补），不再内存全量+窗口切片。
  // 简洁主区（2026-09-28 方案A）：对话轮/命令对已是主形态，轮外系统事件
  // （session.*/task.*/skill.routed/孤儿 tool.call/llm.error/goal.*/phase.*…）
  // 平铺进主流只会把对话冲散——「❋ 审计」抽屉全量承接（auditItems 独立装配不受影响）。
  // 白名单原则：对话内容 + 需用户行动的负向回执（中断/失败/审批/收件箱）保留，
  // 正向状态流转（claimed/done/paused/finished…）退场。
  const STREAMLINE_KINDS = new Set([
    "llm.thinking", "llm.thinking.delta",
    "agent.chat", "agent.chat.delta",
    "advisor.intervention", "approval.requested",
    "finding.new", "message.inbox",
    "session.aborted",
  ])
  const leanItems = useMemo<StreamItem[]>(
    () => items.filter((it) => it.type === "turn" || it.type === "pair"
      || (it.type === "single" && STREAMLINE_KINDS.has(it.event.kind))),
    [items])
  const shown = useMemo(() => [...leanItems].reverse(), [leanItems])
  useEffect(() => {
    const el = listRef.current
    if (!el) return
    const update = () => setStreamShort(el.scrollHeight <= el.clientHeight + 2)
    update()
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null
    observer?.observe(el)
    return () => observer?.disconnect()
  }, [shown.length, activeTab])
  // 统计行（会话流改造 2026-10-03，cc-haha 风格）：token/最后更新/条数。
  // token 取当前窗口内 llm.usage 事件求和——**窗口内近似**（已上翻的分页不在内），
  // 故文案标注「窗口内」；精确值在工作台侧（thread.usage）。
  const stats = useMemo(() => {
    let tokens = 0, cached = 0
    for (const e of visible) {
      if (e.kind !== "llm.usage") continue
      if (typeof e.payload.total_tokens === "number") tokens += e.payload.total_tokens
      const cr = e.payload.cache_read_input_tokens, cc = e.payload.cache_creation_input_tokens
      if (typeof cr === "number") cached += cr
      if (typeof cc === "number") cached += cc
    }
    return { tokens, cached, updatedAt: visible.length ? visible[visible.length - 1].created_at : null }
  }, [visible])
  // 切上下文（会话页签/类型筛选）= 用户要看最新：回到滚动原点（最底部）
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = 0
  }, [activeTab])
  // 上翻到视觉顶部附近自动加载更早一页。column-reverse 的滚动原点在最底（最新），
  // Chrome 走负值域（scrollTop=0 贴底，视觉顶部=-max）；取 |scrollTop| 兼容正负两套实现，
  // 距顶 = 可滚动总距离 - 已上翻距离。
  // 滑动窗口（2026-09-23 会话窗口定稿）：DOM 上界 MAX_DOM——滚回底部附近裁掉
  // 头部一批（贴底视觉零跳动：column-reverse 原点在底部，顶部内容缩减不位移）；
  // 裁掉的更早消息留模块缓存，再上翻 loadEarlier 先吃缓存零网络补回。
  const atBottomRef = useRef(true)
  const onListScroll = () => {
    const el = listRef.current
    if (!el) return
    const distTop = el.scrollHeight - el.clientHeight - Math.abs(el.scrollTop)
    if (distTop < 200) void loadEarlier()
    atBottomRef.current = Math.abs(el.scrollTop) < 200
  }
  // 铺满视口：内容不足一屏且未取尽 → 继续补批直到铺满或取尽（会话源首屏 50 条
  // 常不够铺满，循环 1-3 轮；loadEarlier 取尽即置 loadedAll 停）。
  useEffect(() => {
    const el = listRef.current
    if (!el || loadedAll || loadingEarlier) return
    if (el.scrollHeight > el.clientHeight + 40) return // 已铺满
    void loadEarlier()
  }, [events, loadedAll, loadingEarlier, loadEarlier])
  // 裁剪只动展示 state（visible/items 派生自动瘦身），模块缓存不动；
  // 用户在上方阅读时不裁（atBottomRef），防视觉跳动；内容未铺满一屏时让位
  // （2026-09-28 死循环修复）：铺满 effect 正在上翻补批，而前插批=最旧批，
  // 先裁后量则 scrollHeight 永不增长 → 前插/裁剪互相抵消的无限循环（铺满判定
  // 恒假、CPU 空转，缓存耗尽后退化为对后端 eventsBefore 的节拍式持续打点）。
  // 同一 commit 内两 effect 读同一 DOM，铺满判定一致。
  useEffect(() => {
    if (!atBottomRef.current) return
    const el = listRef.current
    if (el && el.scrollHeight <= el.clientHeight + 40) return // 未铺满：让位补批
    trimDom(MAX_DOM, PAGE, SPARSE_FLOORS)
  }, [events, trimDom])

  const toggleRow = useCallback((id: number, defaultValue: boolean) =>
    setOverrides((prev) => new Map(prev).set(id, !(prev.get(id) ?? defaultValue))), [])

  // 内联审批（2026-10-04）：问答卡提交 → 附加说明先作人工引导发给该会话（失败不阻断决策），
  // 再落 decide；成功靠 approval.{decision} 事件留痕，待审批列表随之剔除该单。
  // 失败不吞：抛回卡片内联显示（可重试）。
  const decideApr = useCallback(async (approval: Approval, decision: "approved" | "rejected", note: string): Promise<DecideApprovalResult> => {
    const text = note.trim()
    if (text && approval.session_id) {
      try { await api.sessionNote(approval.session_id, text) } catch { /* 引导失败不阻断决策 */ }
    }
    const r = await api.decideApproval(approval.id, decision)
    setJobInfo(`审批 ${approval.id} 已${decision === "approved" ? "批准" : "拒绝"}（决定事件见流内审计行）`)
    return r
  }, [])

  // skill.routed 命中技能：双击跳设置页 Skill tab 选中该技能（deep link 经 goto-settings）
  const handleRouteJump = useCallback((e: BBEvent, name: string) => {
    window.dispatchEvent(new CustomEvent("goto-settings", {
      detail: {
        tab: "skills",
        skill: {
          source: e.payload.kind === "track" ? "track" : "cap",
          pack: String(e.payload.pack ?? ""),
          name,
        },
      },
    }))
  }, [])

  const enabledProviders = modelInfo?.providers.filter((p) => p.enabled) ?? []
  const spawnTarget = enabledProviders.find((p) => p.name === provider)
    ?? enabledProviders.find((p) => p.name === modelInfo?.default?.provider)

  // 切模型（2026-09-19）：由输入行徽章浮层触发，选定即切 + 回填本地记忆 + 关浮层；model 空串=该供应商默认模型
  const switchLlm = async (providerName: string, modelName: string) => {
    if (!activeSession) return
    try {
      const r = await api.switchAgentLlm(activeSession.id, providerName, modelName || undefined)
      setSessionModel((m) => ({ ...m, [activeSession.id]: { provider: r.provider, model: r.model } }))
      setModelPopOpen(false)
      setJobInfo(`已切换到 ${r.provider}/${r.model}，下一次调用生效`)
    } catch (e) {
      setJobInfo(`切换失败：${e}`)
    }
  }

  // 附件随发（2026-09-19）：选文件即上传为 artifact（服务端同 sha 去重），chip 三态
  const uploadOne = (key: string, file: File) => {
    api.uploadAttachment(pid, file)
      .then((att) => setPendingFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "ready", att } : x)))
      .catch(() => setPendingFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "error" } : x)))
  }
  const pickFiles = (files: FileList | null) => {
    for (const f of Array.from(files ?? [])) {
      const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`
      setPendingFiles((fs) => [...fs, { key, status: "uploading", name: f.name, size: f.size, file: f }])
      uploadOne(key, f)
    }
  }
  const retryFile = (key: string) => {
    const f = pendingFiles.find((x) => x.key === key)
    if (!f?.file) return
    setPendingFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "uploading" } : x))
    uploadOne(key, f.file)
  }
  const removeFile = (key: string) => setPendingFiles((fs) => fs.filter((x) => x.key !== key))

  // 输入框自适应 1-6 行（行高 24px，6 行封顶内部滚动）
  const autoResize = () => {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = "auto"
    ta.style.height = `${Math.min(ta.scrollHeight, 144)}px`
  }

  const spawn = async () => {
    setSpawning(true)
    try {
      const ms = maxSteps ? Number(maxSteps) : undefined
      const r = await api.spawnAgent(pid, role, undefined, model || undefined,
                                     provider || undefined, ms)
      await refreshSessions()
      void refreshUsage()
      // 预算超支：人手动作放行仅警告（硬闸只拦编排自主动作，§6.8）
      if (r.warning) setJobInfo(`已开窗（⚠ ${r.warning}）`)
    } catch (e) {
      setJobInfo(`开窗失败：${e}`)
    } finally {
      setSpawning(false)
    }
  }

  const orchTick = async (allowed?: string[]) => {
    const t0 = Date.now()
    setJobInfo("编排中…")
    try {
      const { job_id } = await api.orchTick(pid, allowed?.length ? { allowed_roles: allowed } : {})
      // pollJob 每 1.5s 回调一次：借机显示运行时长（编排开窗/派活可能跑几分钟）
      const job = await pollJob(job_id, () => {
        setJobInfo(`编排中… ${Math.round((Date.now() - t0) / 1000)}s`)
      })
      if (job.status !== "done") {
        setJobInfo(quotaHint(job.error) ? `⚠ LLM 配额不足或限流 · 编排失败：${job.error}` : `编排失败：${job.error}`)
        return
      }
      const r = job.result as OrchTickResult
      // 统计位在前（截断后首屏可见的是数字而非 raw_arguments JSON；summary 尾随，悬停 title 见全文）
      const bits = [`🪟 ${r.spawned.length} 窗`, `🏗 ${r.teams.length} 队`]
      if (r.proposals.length) bits.push(`💡 ${r.proposals.length} 提案待采纳`)
      if (r.digest) bits.push("📝 已写简报")
      setJobInfo(`编排完成：${bits.join(" · ")}${r.summary ? ` · ${r.summary}` : ""}`)
    } catch (e) {
      setJobInfo(quotaHint(String(e)) ? `⚠ LLM 配额不足或限流 · 编排失败：${e}` : `编排失败：${e}`)
    }
  }

  // A5 手动重排已随任务机制退役（2026-10-06）：函数与入口一并删除。

  // F8 会话级复盘：planner LLM 复盘本会话跑过的任务 → 变更提案进设置「提案」tab 等人审（无 key 503）
  const [reviewing, setReviewing] = useState(false)
  const reviewSession = async () => {
    if (!activeSession) return
    setReviewing(true)
    setJobInfo("复盘中（planner LLM 复盘本会话任务/命令/文档引用）…")
    try {
      const { job_id } = await api.sessionReview(activeSession.id)
      const job = await pollJob(job_id, () => {})
      if (job.status !== "done") {
        setJobInfo(`复盘失败：${job.error}`)
        return
      }
      const r = job.result as { landed?: { id: string; summary: string }[]; rejected?: { summary: string; error: string }[]; error?: string }
      const landed = r.landed?.length ?? 0
      const rejected = r.rejected?.length ?? 0
      window.dispatchEvent(new Event("proposals-changed"))
      if (landed > 0) {
        setJobInfo(`复盘沉淀 ${landed} 条提案${rejected ? `，LLM 自剔 ${rejected} 条` : ""}，已跳转审批`)
        window.dispatchEvent(new CustomEvent("goto-settings", { detail: { tab: "proposals" } }))
      } else {
        setJobInfo(`复盘完成：无新提案${rejected ? `（自剔 ${rejected} 条）` : ""}${r.error ? `；${r.error}` : ""}`)
      }
    } catch (e) {
      setJobInfo(`复盘不可用：${String(e)}`)
    } finally {
      setReviewing(false)
    }
  }

  // F9 状态灯：事件流派生叠加 armed——armed 且事件流判空闲 → 绿点「已启动待命」；
  // 未 armed 空闲保持灰点（未启动）。armed 数据来自 sessions 轮询（GET sessions 增强）。
  const sessionsById = useMemo(() => new Map(sessions.map((s) => [s.id, s])), [sessions])
  // 每页签状态派生的事件源：一次分组扫描代替每页签一次 O(全量) filter
  // （页签多 × 事件多 × delta 高频时，原先每渲染 O(页签×事件)）
  const eventsBySid = useMemo(() => {
    const m = new Map<string, BBEvent[]>()
    for (const e of events) {
      if (!e.session_id) continue
      const arr = m.get(e.session_id)
      if (arr) arr.push(e)
      else m.set(e.session_id, [e])
    }
    return m
  }, [events])
  const tabStatus = useCallback((sid: string): SessionStatus => {
    const s = sessionsById.get(sid)
    // 暂停稳定事实最优先：sessions 行 status 是 DB 稳定态，不受事件窗口截断影响
    // （session.paused 事件约 2 分钟即被挤出 50 条窗口，纯事件推导会让按钮从「▶继续」退回「⏸暂停」）
    if (s?.status === "paused") return "paused"
    const st = sessionStatus(eventsBySid.get(sid) ?? [])
    // 稳定事实优先：worker job 在跑（服务端 JobRegistry）→ 恒亮青灯，
    // 不受事件窗口截断（窗口外会话 events 为空）与 2 分钟阈值抖动影响
    if (s?.worker_running) return "running"
    // F9 worker 模型下排空队列也落 session.finished（非终态），armed 待命要盖过 idle/finished
    if (s?.worker_armed && (st === "idle" || st === "finished")) return "armed"
    return st
  }, [eventsBySid, sessionsById])

  // 终止入口（2026-09-19 起：页签 ×=结束会话，会话控制组已删；另一处是任务看板删除 claimed 任务）。
  // armed/running/paused 先 confirm——关窗语义已改硬中断，执行中任务会 fail（快照保留可续跑）
  const closeSession = async (sid: string) => {
    const st = tabStatus(sid)
    if (st === "running" || st === "paused" || st === "armed") {
      if (!window.confirm("关闭页签将结束会话；执行中/已认领的任务会中断（快照保留，任务看板可续跑）。确定关闭？")) return
    }
    try {
      const r = await api.closeSession(sid)
      if (r.status === "closing") {
        // 硬中断关窗：worker 步边界 fail 当前任务后经 close_pending 自关
        refreshSessions()
        setJobInfo("中断并关闭中：当前任务将标记为失败（快照保留可续跑），随后自动关闭")
        return
      }
      if (activeTab === sid) setActiveTab("__orch")
      refreshSessions()
      setJobInfo("会话已结束")
    } catch (e) {
      setJobInfo(`结束会话失败：${e}`)
    }
  }

  // 会话控制（DESIGN.md §3）：恢复/中断（暂停入口 2026-09-19 退役，仅预算暂停自动触发）
  const controlSession = async (action: "resume" | "abort", sid: string) => {
    const fn = action === "resume" ? api.resumeSession : api.abortSession
    const label = action === "resume" ? "恢复" : "中断"
    try {
      const res = await fn(sid)
      setJobInfo(`${label}请求已发送（当前步做完后生效）`)
      refreshSessions()
      return res
    } catch (e) {
      const msg = e instanceof ApiError && typeof e.data === "string" && e.data ? e.data : String(e)
      setJobInfo(`${label}失败：${msg}`)
      return null
    }
  }

  // 会话手动压缩上下文（/compact，2026-10-06）：调后端把 chat-<sid>.json 旧历史压成
  // 摘要重写，后续对话从压缩后历史续起。不走 human_note、不发对话轮。
  const runCompact = async (sid: string) => {
    setRemark("")
    try {
      const r = await api.compactSession(sid)
      if (r.status === "compacted") {
        const kept = Math.max(0, (r.after_msgs ?? 0) - 2) // 减摘要头两条
        setJobInfo(`已压缩上下文：${r.before_msgs ?? "?"} 条 → 摘要 + 最近 ${kept} 条（见事件流「🧹 上下文压缩」）`)
      } else if (r.status === "noop") {
        setJobInfo("当前对话历史较短，无需压缩")
      } else {
        setJobInfo(`上下文压缩失败：${r.reason ?? r.status}`)
      }
    } catch (e) {
      setJobInfo(e instanceof ApiError && e.status === 409
        ? "会话正在执行中，稍候再压缩"
        : `上下文压缩失败：${e}`)
    }
  }

  // 输入框（E8，2026-09-19 附件随发；2026-09-20 对话化改双模式；2026-09-21
  // 对话化编排器 M1 编排器态改「与编排对话」；2026-09-28 阶段二去模式徽章）：
  // 会话态=「对话/引导」（human_note 直达当前选中会话，空闲=后端踢对话轮流式
  // 回复、在跑=轮末注入不打断；发任务语义由 Agent 在对话中自判升级为任务）。
  // 编排器态=「与编排对话」（orch.chat 插队轮，busy 409 不排队）。发送时携带
  // 就绪附件，成功后清 chips（编排对话不支持附件）。
  const sendRemark = async () => {
    const text = remark.trim()
    const readyIds = pendingFiles.filter((f) => f.status === "ready" && f.att).map((f) => f.att!.id)
    if (uploadingAtt) return
    if (inputMode === "note") {
      if (!activeSession) return
      // 斜杠命令 /compact：压缩本会话上下文，不发引导（2026-10-06）
      if (text === "/compact") { void runCompact(activeSession.id); return }
      if (!text && readyIds.length === 0) return
      const sid = activeSession.id
      setRemark("")
      try {
        const sent = await api.sessionNote(sid, text, readyIds)
        const sentFiles = new Set(pendingFiles.filter((f) => f.status === "ready").map((f) => f.key))
        setPendingFiles((fs) => fs.filter((f) => !sentFiles.has(f.key)))
        // 轮末语义（2026-09-19）：轮进行中（canAbort）→ human_note 等下一轮认领期
        // 注入，卡片上方排队可见、可「立即发送」提前注入；空闲 → 后端直接踢对话轮
        //（run_chat），Agent 回复落事件流 agent.chat，不显排队条。task.claimed 到达
        // 自动清条（游标 effect 只看新事件）。wake（2026-09-27）= 后端实际处置：
        // resumed=暂停会话被引导唤醒走恢复；queued/deferred=滞留收件箱等注入。
        if (canAbort) {
          setQueuedNotes((qs) => [...qs, {
            key: `qn-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            sid, text, attCount: readyIds.length,
          }])
        } else if (sent.wake === "resumed") {
          setJobInfo("已发送：会话此前处于暂停，已随引导恢复续跑（事件流可见「会话已恢复」）")
        } else if (sent.wake === "queued" || sent.wake === "deferred") {
          setJobInfo("已发送：引导已入收件箱，等会话下一轮注入（窗内有活未踢）")
        } else {
          setJobInfo("已发送：Agent 正在回复（看事件流「🤖 Agent 回复」）")
        }
      } catch (e) {
        setJobInfo(`引导投递失败：${e}`)
      }
      return
    }
    if (inputMode === "orch") {
      // 对话化编排器 M1（2026-09-21，§4.2）：与编排器对话——插队轮。C2 指令
      // 「一次性目标+自动 tick」退役（orchDirective API 留 deprecated），目标
      // 固化走 M2 goal 闭环（对话流顶部「设定阶段目标」）。busy 409 不排队
      //（定稿 #6）：只提示，不禁用输入。
      if (!text || orchBusy) return
      setRemark("")
      setOrchBusy(true)
      try {
        const r = await api.orchChat(pid, text)
        setJobInfo(`已发送：${orchMeta?.persona?.display_name || "编排器"}思考中…（回复见对话流）`)
        const job = await pollJob(r.job_id, () => {}, 1500)
        if (job.status === "done") setJobInfo("编排器已回复（见对话流）")
        else setJobInfo(`编排器回复失败：${job.error ?? job.status}`)
      } catch (e) {
        setJobInfo(e instanceof ApiError && e.status === 409
          ? "编排器正在思考（自动编排/其他对话占用中），请稍候再发"
          : `发送失败：${e}`)
      } finally {
        setOrchBusy(false)
      }
      return
    }
  }

  // 批 6 L0：采纳编排提案——走人类既有写口（POST /teams、POST /agents），
  // 实体 created_by=human，责任不挂编排；成功后按钮仅在本页内存态置灰。
  // 任务机制退役（2026-10-06）后 op 仅剩 build_team / execute。
  const adoptProposal = async (ev: BBEvent) => {
    const p = ev.payload as unknown as OrchProposal
    if (!p || typeof p !== "object" || adopted.has(ev.id) || adoptingId !== null) return
    setAdoptingId(ev.id)
    try {
      if (p.op === "build_team") {
        // 采纳=以人类名义建队（draft，不启动）；启动仍需团队卡 preflight + 四项确认
        const a = p.args
        const rawMembers = Array.isArray(a.members) ? (a.members as Record<string, unknown>[]) : []
        const members: TeamMemberInput[] = rawMembers.map((m, i) => ({
          member_key: String(m.member_key ?? m.id ?? `member-${i + 1}`),
          label: typeof m.label === "string" ? m.label : undefined,
          responsibility: typeof m.responsibility === "string" ? m.responsibility : undefined,
          role: typeof m.role === "string" ? m.role : undefined,
          runtime: typeof m.runtime === "string" ? m.runtime : undefined,
          threat_class: typeof m.threat_class === "string" ? m.threat_class : undefined,
          max_steps: typeof m.max_steps === "number" ? m.max_steps : undefined,
        }))
        await api.createTeam(pid, {
          name: String(a.name ?? ""),
          goal_text: typeof a.goal_text === "string" ? a.goal_text : undefined,
          members,
        })
        setJobInfo("已采纳建队提案：请在团队卡「查看并配置」确认启动")
      } else if (p.op === "execute") {
        // 采纳=亲自执行：以人类名义开通用窗并引导（新模型下人工开窗引导=纯对话）
        const r = await api.spawnAgent(pid, "_generalist")
        await api.sessionNote(r.id, String(p.args.objective ?? ""))
        await refreshSessions()
        void refreshUsage()
        setJobInfo(r.warning
          ? `已开窗引导执行（⚠ ${r.warning}）`
          : "已采纳执行提案：已开窗并引导 Agent 执行该目标")
      }
      setAdopted((prev) => new Set(prev).add(ev.id))
    } catch (e) {
      setJobInfo(`采纳失败：${e}`)
    } finally {
      setAdoptingId(null)
    }
  }

  const activeSession = sessions.find((s) => s.id === activeTab)
  const activeStatus = activeSession ? tabStatus(activeSession.id) : null
  // 会话态斜杠命令提示（2026-10-06）：选中会话且输入以 / 开头（无空格）时浮出候选。
  const slashQuery = inputMode === "note" && activeSession
    && remark.startsWith("/") && !remark.includes(" ")
    ? remark.slice(1).toLowerCase() : null
  const sessionSlashCmds = slashQuery === null
    ? [] : SESSION_SLASH_COMMANDS.filter((c) => c.cmd.slice(1).includes(slashQuery))
  // 输入行模型徽章（2026-09-19）：当前会话记忆中的模型（无值=后端默认）与发送可用性
  const sessionModelOf = activeSession ? sessionModel[activeSession.id] : undefined
  const uploadingAtt = pendingFiles.some((f) => f.status === "uploading")
  const hasReadyAtt = pendingFiles.some((f) => f.status === "ready")
  const canSend = uploadingAtt ? false
    : inputMode === "orch" ? remark.trim().length > 0 && !orchBusy
    : (!!activeSession && (remark.trim().length > 0 || hasReadyAtt))
  // E8：步数预算用尽自动暂停的会话集（恢复/中断/收尾即移出）——composer 显「▶ 继续」
  const budgetPausedSids = useMemo(() => {
    const s = new Set<string>()
    for (const e of events) {
      if (!e.session_id) continue
      if (e.kind === "session.budget_paused") s.add(e.session_id)
      else if (e.kind === "session.resumed" || e.kind === "session.aborted"
               || e.kind === "session.finished") s.delete(e.session_id)
    }
    return s
  }, [events])
  // 发送钮双态（2026-09-19）：会话有任务在身（running/paused）时 ■=中断本轮，否则 ↑=发送
  const canAbort = activeStatus === "running" || activeStatus === "paused"
  // 模式随上下文收敛（2026-09-19 用户定稿）：选中会话=引导会话（**不显徽章**，写消息即引导）；
  // 回到编排器=与编排对话（M1 缺省，对话化编排器定稿），徽章只在编排器态显示。上下文切换即归位缺省模式。
  const hasSessionCtx = activeSession != null
  useEffect(() => {
    setInputMode(hasSessionCtx ? "note" : "orch")
  }, [hasSessionCtx])

  // 排队引导条清理（2026-10-06 任务退役后）：会话不再 running = 本轮结束，
  // 收件箱引导随下一轮对话注入，排队提示随之清除。
  useEffect(() => {
    if (queuedNotes.length === 0) return
    setQueuedNotes((qs) => qs.filter((q) => tabStatus(q.sid) === "running"))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events])

  // 事件驱动即时刷 sessions（2026-09-29 余晖退役配套）：会话生命周期事件到达即刷，
  // 执行灯（worker_running 权威信号）开始/结束亚秒亮灭，不吃 5s 轮询延迟；
  // 游标去重防历史回放/重连风暴。
  const lifeCursor = useRef(0)
  useEffect(() => {
    let maxId = lifeCursor.current
    let touched = false
    for (const e of events) {
      if (e.id <= lifeCursor.current) continue
      maxId = Math.max(maxId, e.id)
      if (e.kind === "session.spawned" || e.kind === "session.finished"
          || e.kind === "session.paused" || e.kind === "session.resumed"
          || e.kind === "session.aborted") touched = true
    }
    lifeCursor.current = maxId
    if (touched) void refreshSessions()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events])

  // 排队引导条仅保留当前轮中断入口；任务不再通过会话队列认领。
  const sendQueuedNow = async (key: string) => {
    const q = queuedNotes.find((x) => x.key === key)
    if (!q) return
    setQueuedNotes((qs) => qs.filter((x) => x.key !== key))
    await controlSession("abort", q.sid)
    setJobInfo("已中断当前轮，引导将在会话恢复后处理")
  }

  // 事件流渲染体（2026-09-21 抽出复用）：会话页签主区与审计抽屉（2026-09-28）共用
  // 同一套 EventRow/加载更早逻辑，仅列表数据不同（shown vs auditShown）。
  // 2026-09-28：withBusy=true 时流尾（column-reverse 视觉最底）加「✻ 执行中」忙碌行
  // （仅会话页签且 tabStatus=running；审计抽屉复用时传 false）。
  const streamBusy = activeTab !== "__orch" && tabStatus(activeTab) === "running"
  const renderStream = (list: StreamItem[], withBusy = false) => (
    <>
      {withBusy && streamBusy && (
        <div className="shrink-0 px-2 pb-1.5 pt-1 text-xs text-muted-foreground/80">
          <span className="mr-2 inline-block animate-pulse text-primary">✻</span>
          执行中…（输入框右侧 ■ 可中断；过程与回复实时追加）
        </div>
      )}
      {list.map((item) => {
        const primary = item.type === "pair" ? item.command
          : item.type === "turn" ? item.note : item.event
        const primaryKind = item.type === "pair" ? "command"
          : item.type === "turn" ? "message.inbox" : item.event.kind
        const style = eventStyle(primaryKind, primary.payload)
        // turn 的 open=过程组折叠态（默认收起；键复用 note.id，与顶层折叠记忆统一）
        const open = item.type === "turn"
          ? overrides.get(primary.id) ?? false
          : overrides.get(primary.id) ?? style.defaultOpen
        // 行内操作位：orch.proposed=「采纳」；approval.requested=整块问答卡（见 EventRow）
        let action: ReactNode
        if (item.type === "single" && item.event.kind === "orch.proposed") {
          action = (
            <button
              type="button"
              title="以人类名义采纳：走与手动建队/开窗相同的写口（created_by=human）"
              onClick={(click) => { click.stopPropagation(); void adoptProposal(item.event) }}
              disabled={adopted.has(item.event.id) || adoptingId !== null}
              className={cn(
                "shrink-0 rounded border px-1.5 py-px text-[11px] leading-tight",
                adopted.has(item.event.id)
                  ? "border-muted-foreground/40 text-muted-foreground"
                  : "border-amber-400/60 text-amber-400 hover:bg-amber-400/10",
              )}
            >
              {adopted.has(item.event.id) ? "✓ 已采纳" : adoptingId === item.event.id ? "采纳中…" : "采纳"}
            </button>
          )
        }
        // 待审批完整行（命中 → 渲染问答卡；未命中=已决策或数据未到 → 紧凑审计行）
        const aprId = item.type === "single" && item.event.kind === "approval.requested"
          ? String(item.event.payload.approval_id ?? "") : ""
        const approval = aprId ? approvalById.get(aprId) : undefined
        return (
          <EventRow
            key={primary.id}
            item={item}
            open={open}
            onToggle={toggleRow}
            roleNames={roleNames}
            action={action}
            approval={approval}
            onDecideApproval={decideApr}
            onRouteJump={handleRouteJump}
            assetName={assetName}
            pid={pid}
          />
        )
      })}
      {!loadedAll && (
        <button
          type="button"
          onClick={() => void loadEarlier()}
          disabled={loadingEarlier}
          className="shrink-0 rounded border bg-card px-2 py-1 text-center text-xs text-muted-foreground hover:bg-accent disabled:opacity-50"
        >
          {loadingEarlier ? "加载中…" : "↑ 加载更早"}
        </button>
      )}
    </>
  )

  return (
    <div className="relative flex h-full flex-col">
      {/* 第一行：会话页签（多了横向滚动，不换行）+ live 状态点 */}
      <div className="relative flex h-9 shrink-0 items-center gap-1 border-b px-2">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {tabs.map((t) => (
            <div key={t.key} className="relative flex shrink-0 items-center gap-0.5">
              <button
                onClick={() => { setActiveTab(t.key); setWinOpen(false) }}
                title={t.sessionId && t.sessionId !== "__orch"
                  ? `${t.label} · ${t.sessionId}` : undefined}
                className={cn(
                  "group flex shrink-0 items-center gap-1.5 rounded-md px-2.5 py-1 text-xs whitespace-nowrap",
                  activeTab === t.key ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-accent",
                )}
              >
                {t.sessionId && t.sessionId !== "__orch" && (
                  <StatusDot status={tabStatus(t.sessionId)} />
                )}
                {t.label}
                {t.sessionId && t.sessionId !== "__orch" && (unreadBySid.get(t.sessionId) ?? 0) > 0 && (
                  <span
                    title="有系统私信（依据撤回需自评 / 发现增补通知）"
                    className="inline-flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-(--status-error) px-1 text-[9px] font-bold leading-none text-white"
                  >
                    {(unreadBySid.get(t.sessionId) ?? 0) > 9 ? "9+" : unreadBySid.get(t.sessionId)}
                  </span>
                )}
                {t.sessionId && t.sessionId !== "__orch" && (
                  <span
                    role="button"
                    aria-label={`结束会话 ${t.label}`}
                    title="结束会话（执行中任务会中断，快照保留可在任务看板续跑）"
                    className="-mr-1 rounded p-0.5 opacity-0 transition-opacity group-hover:opacity-100 hover:bg-accent text-muted-foreground hover:text-foreground"
                    onClick={(e) => {
                      e.stopPropagation()
                      void closeSession(t.sessionId!)
                    }}
                  >
                    <X className="size-3" />
                  </span>
                )}
              </button>
              {t.key === "__orch" && (
                <>
                  <button
                    type="button"
                    aria-label="在指挥中开窗"
                    title="在指挥中打开一个 Agent 窗"
                    onClick={() => setWinOpen((v) => !v)}
                    className={cn(
                      "flex size-6 items-center justify-center rounded-md border text-muted-foreground hover:bg-accent hover:text-foreground",
                      winOpen && "bg-secondary text-foreground",
                    )}
                  >
                    <Plus className="size-3.5" />
                  </button>
                </>
              )}
            </div>
          ))}
        </div>
        {winOpen && (
          <div className="absolute left-2 top-8 z-40 w-72 rounded-lg border bg-popover p-3 text-xs shadow-xl">
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              title={roles.find((r) => r.role === role)?.description
                ? `${roleName(roles.find((r) => r.role === role)?.name, role)} · ${roles.find((r) => r.role === role)?.description}`
                : "开窗角色"}
              className="mb-2 h-8 w-full rounded-md border bg-background px-2 text-xs"
            >
              {roles.map((r) => (
                <option key={r.role} value={r.role}>
                  {roleName(r.name, r.role)}{r.description ? ` · ${r.description}` : ""}
                </option>
              ))}
            </select>
            <select
              value={provider}
              onChange={(e) => { setProvider(e.target.value); setModel("") }}
              title="开窗供应商（缺省=全局默认供应商）"
              className="mb-2 h-8 w-full rounded-md border bg-background px-2 font-mono text-xs"
            >
              <option value="">默认供应商</option>
              {enabledProviders.map((p) => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              title="开窗模型（缺省=该供应商首个模型）"
              className="mb-2 h-8 w-full rounded-md border bg-background px-2 font-mono text-xs"
            >
              <option value="">默认模型{modelInfo?.default ? `（${modelInfo.default.model}）` : ""}</option>
              {(spawnTarget?.models ?? []).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <input
              value={maxSteps}
              onChange={(e) => setMaxSteps(e.target.value.replace(/\D/g, ""))}
              placeholder="步数 200"
              title="开窗步数预算 max_steps（E8，缺省 200；角色 yaml 上限取更严者；步数吃紧时 Agent 可 request_steps 自助 +200，耗尽自动暂停）"
              className="mb-2 h-8 w-full rounded-md border bg-background px-2 font-mono text-xs placeholder:text-muted-foreground"
            />
            <Button size="sm" className="w-full" onClick={spawn} disabled={spawning || !roles.length}>
              {spawning ? "开窗中…" : "开窗"}
            </Button>
          </div>
        )}
        <span className={cn("ml-1 shrink-0 font-mono text-[10px]", connected ? "text-primary" : "text-(--status-error)")}>
          {connected ? "● live" : "○ 重连中"}
        </span>
        {/* 审计入口（2026-09-28 三段式改造）：类型筛选 chips 从主区撤除迁入审计抽屉 */}
        {true && (
          <button
            type="button"
            title="审计流：按类型平铺过滤当前页签事件（思考/命令/工具/发现…）"
            onClick={() => setAuditOpen((v) => !v)}
            className={cn(
              "shrink-0 rounded px-2 py-1 text-[11px] whitespace-nowrap",
              auditOpen ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-accent",
            )}
          >
            ❋ 审计
          </button>
        )}
      </div>
      {/* 分阶段工作流阶段条（pentest M4）：轨无剧本自渲染 null，直播视图共用 */}
      {/* 统计行（会话流改造 2026-10-03） */}
      <ChatStats tokens={stats.tokens} cachedTokens={stats.cached}
        updatedAt={stats.updatedAt} count={visible.length} countLabel="条事件"
        className="shrink-0 border-b px-3 py-1 text-[11px] text-muted-foreground/70" />
      {true && (
      <>

      {/* 批 5 §6.8：重启=急停。DB 链活但本进程无标记 → 提示人工恢复 */}
      {usage?.chain?.estranged && (
        <div className="flex flex-wrap items-center gap-2 border-b border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-400">
          <span>
            ⛓ L2 自动链因服务重启已急停（链内自动 {usage.chain.ticks} 轮），不会自动恢复。
          </span>
          <button type="button" className="underline hover:no-underline"
                  onClick={() => void orchTick()}>
            恢复自动链
          </button>
        </div>
      )}

      {/* 类型筛选 chips 已撤除（2026-09-28）：主区常显对话轮形态，「❋ 审计」抽屉承接
          类型过滤；「▦ 计划」在页签行切换 PlanPanel。jobInfo（长任务结果摘要）移为
          条件横幅——min-w-0+truncate 单行省略，悬停 title 看全文 */}
      {jobInfo && (
        <div className="shrink-0 truncate border-b px-3 py-1 font-mono text-[11px] text-muted-foreground"
             title={jobInfo}>
          {jobInfo}
        </div>
      )}

      {activeTab === "__orch" ? (
      // 对话化编排器（M1/M2/M3，2026-09-21）：编排页签两段式——顶部 goal 条 + 对话流
      // （OrchChatPane）。编排动作事件（任务派发/orch.*/goal.*/llm.error…）由「❋ 审计」
      // 抽屉承载——2026-09-28 删除运行记录折叠区：与审计抽屉数据源/渲染体完全相同，
      // 抽屉还多类型筛选，折叠区是功能重复的碍眼冗余
      <OrchChatPane
        events={orchEvents}
        busy={orchBusy}
        persona={orchMeta?.persona ?? null}
        pid={pid}
        teams={teams}
        onEditPersona={() => setPersonaOpen(true)}
        onOpenTeamReport={(tid) => setTeamReportId(tid)}
        onConfigureTeam={(tid) => setTeamConfigId(tid)}
        orchRunning={!!usage?.orch_running}
      />
      ) : (
      <div
        ref={listRef}
        onScroll={onListScroll}
        className={cn(
          "min-h-0 flex-1 overflow-auto px-3",
          streamShort ? "flex flex-col" : "flex flex-col-reverse",
        )}
      >
        {renderStream(streamShort ? leanItems : shown, true)}
      </div>
      )}

      {/* 审计抽屉（2026-09-28 三段式改造）：右侧滑出覆盖流区（页签行保留可切页签），
          类型筛选 chips 迁此；复用同一 events 窗口 + renderStream（busy 行不传），
          加载更早/行展开/审批按钮与主区共享状态 */}
      {auditOpen && (
        <div className="absolute bottom-0 right-0 top-9 z-30 flex w-[min(720px,92%)] flex-col border-l bg-background shadow-2xl">
          <div className="flex h-9 shrink-0 items-center gap-2 border-b px-3">
            <span className="shrink-0 text-xs font-medium">❋ 审计流</span>
            <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">
              {activeTab === "__orch"
                ? "编排视角"
                : tabs.find((t) => t.key === activeTab)?.label ?? activeTab}
              {" "}· 窗口内 {auditVisible.length} 条
            </span>
            <button
              type="button"
              onClick={() => setAuditOpen(false)}
              title="关闭审计流（Esc 不响应，点 ✕ 或再点「❋ 审计」）"
              className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <X className="size-3.5" />
            </button>
          </div>
          <div className="flex shrink-0 flex-wrap gap-1 border-b px-3 py-1.5">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                onClick={() => setAuditFilter(f.key)}
                className={cn(
                  "shrink-0 rounded px-2 py-0.5 text-xs",
                  auditFilter === f.key ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent",
                )}
              >
                {f.label}
              </button>
            ))}
          </div>
          <div className="flex min-h-0 flex-1 flex-col-reverse overflow-auto px-3">
            {auditShown.length === 0 ? (
              <span className="py-6 text-center text-xs text-muted-foreground">
                当前窗口内无此类事件（回主区上翻加载更早历史后自动可见）
              </span>
            ) : (
              renderStream(auditShown)
            )}
          </div>
        </div>
      )}

      {/* 插话输入（2026-09-19 Claude Code 化）：排队引导条 + 附件 chips 行 + 无边框多行输入 + 底部工具行
          （+ 附件 / 模式徽章循环 / 模型徽章浮层 / 会话 chips（跑队列·继续·复盘）/ 圆形 ↑ 或 ■ 中断钮）。
          发任务=human 名义 passive 任务；E8 引导会话=human_note 直达选中会话（轮末注入）；指挥编排不支持附件 */}
      <div className="relative p-3">
        {/* 输入卡片（2026-09-19 定稿，Claude Code 形态）：圆角白描边、光标进入聚焦发光，
            输入区与底部功能栏之间有细分隔线；外层不再整宽 border-t（卡片自身即边界） */}
        <div className={cn(
          "rounded-xl border border-white/25 bg-background/30 px-3 py-2",
          "transition-[border-color,box-shadow] duration-200",
          "focus-within:border-white/60 focus-within:shadow-[0_0_18px_rgba(255,255,255,0.15)]")}>
        {/* 排队引导条（2026-09-19 轮末语义）：note 发送后等本轮结束注入，可「立即发送」中断当前轮提前注入 */}
        {activeSession && queuedNotes.filter((q) => q.sid === activeSession.id).map((q) => (
          <div key={q.key}
               className="mb-2 flex items-center gap-2 rounded-md border border-white/10 bg-accent/40 px-2 py-1 text-xs">
            <span className="min-w-0 flex-1 truncate text-muted-foreground"
                  title={q.text}>
              💬 {q.text || `📎 附件 ×${q.attCount}`}（本轮结束后注入）
            </span>
            {canAbort && (
              <button type="button" className="shrink-0 rounded border border-amber-400/60 px-1.5 py-px text-[11px] text-amber-400 hover:bg-amber-400/10"
                      title="中断当前轮（任务 fail 但快照保留可续跑）并立即跑下一轮注入该引导"
                      onClick={() => void sendQueuedNow(q.key)}>
                ⚡ 立即发送
              </button>
            )}
            <button type="button" className="shrink-0 text-muted-foreground hover:text-foreground"
                    title="仅移除排队提示（消息已在收件箱，仍会随下一轮注入）"
                    onClick={() => setQueuedNotes((qs) => qs.filter((x) => x.key !== q.key))}>
              <X className="size-3" />
            </button>
          </div>
        ))}
        {/* 附件 chips 行（与编排对话模式下半透明保留 + 提示） */}
        {pendingFiles.length > 0 && (
          <div className={cn("mb-2 flex flex-wrap items-center gap-1.5", inputMode === "orch" && "opacity-50")}>
            {pendingFiles.map((f) => (
              <span key={f.key}
                className={cn("inline-flex max-w-64 items-center gap-1 rounded-md border px-2 py-1 text-xs",
                  f.status === "error" && "border-destructive/50 text-destructive",
                  f.status === "uploading" && "opacity-60")}
                title={f.status === "error" ? "上传失败，点「重试」再传" : `${f.name}（${fmtBytes(f.size)}）`}>
                <Paperclip className="size-3 shrink-0" />
                <span className="min-w-0 truncate">{f.name}</span>
                <span className="shrink-0 text-[10px] text-muted-foreground">{fmtBytes(f.size)}</span>
                {f.status === "uploading" && <span className="shrink-0 text-[10px]">上传中…</span>}
                {f.status === "error" && (
                  <button type="button" className="shrink-0 underline" onClick={() => retryFile(f.key)}>重试</button>
                )}
                <button type="button" className="shrink-0 text-muted-foreground hover:text-foreground"
                        onClick={() => removeFile(f.key)} title="移除附件">
                  <X className="size-3" />
                </button>
              </span>
            ))}
            {inputMode === "orch" && (
              <span className="text-[10px] text-muted-foreground">与编排对话不支持附件</span>
            )}
          </div>
        )}
        {/* 会话态斜杠命令提示（2026-10-06）：输入以 / 开头时浮出候选（目前仅 /compact），
            点击即执行；Esc 关闭（清空输入）。 */}
        {sessionSlashCmds.length > 0 && (
          <div className="mb-2 rounded-md border border-white/15 bg-card/95 p-1 shadow-lg">
            {sessionSlashCmds.map((c) => (
              <button key={c.cmd} type="button"
                      className="flex w-full items-baseline gap-2 rounded px-2 py-1 text-left text-xs hover:bg-accent"
                      onClick={() => { if (activeSession) void runCompact(activeSession.id) }}>
                <span className="font-mono text-foreground">{c.cmd}</span>
                <span className="text-muted-foreground">{c.desc}</span>
              </button>
            ))}
          </div>
        )}
        {/* 输入区：无边框透明多行，Enter 发送 / Shift+Enter 换行，自适应 1-6 行 */}
        <textarea
          ref={taRef}
          rows={1}
          value={remark}
          placeholder={inputMode === "orch"
            ? "与编排器对话：问态势/问下一步/纠正方向——插队轮即时回复，可让它发任务、开窗（同闸门）…"
            : (activeSession
                ? (canAbort
                    ? `引导「${sessionLabel(activeSession, roleNames)}」：一句话指示，本轮结束后注入（可在上方排队条点「立即发送」中断当前轮）…`
                    : `与「${sessionLabel(activeSession, roleNames)}」对话：提问或下达指示，Agent 空闲时自动回复…`)
                : "对话：先选中一个会话页签…")}
          onChange={(e) => { setRemark(e.target.value); autoResize() }}
          onKeyDown={(e) => {
            if (e.key === "Escape" && sessionSlashCmds.length > 0) {
              e.preventDefault(); setRemark(""); return
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              sendRemark()
            }
          }}
          className="max-h-36 min-h-6 w-full resize-none bg-transparent text-sm leading-6 outline-none placeholder:text-muted-foreground"
        />
        {/* 底部工具行（与输入区之间一条细分隔线）：+ 附件 → 模型徽章（仅选中会话）→ 圆形 ↑ 发送。
              模式徽章已退役（阶段二 2026-09-28）：会话态/编排器态各为单模式，无切换 */}
          <div className="mt-1.5 flex items-center gap-1.5 border-t border-white/10 pt-1.5">
            <button type="button"
              disabled={inputMode === "orch"}
              title={inputMode === "orch" ? "与编排对话不支持附件"
                : "添加附件（exe/elf/图片等，单文件 ≤64MB，随「对话/任务」下发，Agent 可 run_cmd 读取）"}
              onClick={() => fileInputRef.current?.click()}
              className={cn("flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                inputMode === "orch" && "cursor-not-allowed opacity-40 hover:bg-transparent hover:text-muted-foreground")}>
              <Paperclip className="size-4" />
            </button>
            {/* 编排器工具入口已收敛到顶部「指挥」页签；底部只保留会话输入相关动作。 */}
          {!hasSessionCtx && (
            <>
              {/* 🛡 行动边界（goal 统一：原「作战计划/红队行动」弹层退役，本弹层
                  只留 redteam ROE 留档编辑；目标与判据在「🎯 设定阶段目标」） */}
              {track === "redteam" && (
                <div className="relative">
                  <button type="button"
                          className={cn(ORCH_CHIP_CLS, usage?.roe_complete === false && "text-(--status-approval)")}
                          title="行动边界：红队 ROE 四要素留档（§6.9；未核验齐全时行为按渗透测试上限兜底）"
                          onClick={() => setModeOpen((o) => !o)}>
                    🛡 {usage?.roe_complete === false ? "行动边界 · ROE 未核验" : "行动边界"}
                  </button>
                  {modeOpen && usage && (
                    <RoePopover
                      usage={usage}
                      onClose={() => setModeOpen(false)}
                      onSave={async (patch) => {
                        await api.patchProjectConfig(pid, patch)
                        void refreshUsage()
                        setJobInfo("行动边界已更新（ROE 未核验齐全时行为按渗透测试上限兜底；在跑会话维持创建时固化语义）")
                      }}
                    />
                  )}
                </div>
              )}
              {/* 🧠 自主档：L0/L1/L2 三选 + 暂停 + 自动派生开关（goal 统一后从
                  退役弹层迁入）；chip 显当前档与暂停态，状态灯=自动派生四态 */}
              {usage && (
                <div className="relative">
                  <button type="button" onClick={() => setAutoOpen((v) => !v)}
                    title="项目自主档（§6.8）：级别 / 暂停 / 自动派生开关"
                    className={cn(ORCH_CHIP_CLS, "gap-1.5", usage.paused && "text-(--status-paused)")}>
                    <span
                      className={cn("inline-block size-2 rounded-full", deriveLamp(usage).cls)}
                      title={deriveLamp(usage).title}
                    />
                    🧠 {usage.level}{usage.paused ? " ⏸" : ""}
                  </button>
                  {autoOpen && (
                    <div className="absolute bottom-9 left-0 z-10 w-56 rounded-lg border bg-popover p-3 shadow-md">
                      <div className="mb-2 flex overflow-hidden rounded-md border text-xs"
                           title="项目自主级别（§6.8：L0 全手动 / L1 任务自动·开窗审批 / L2 全自动链）；安全层任何档位不放松">
                        {(["L0", "L1", "L2"] as const).map((lv) => (
                          <button
                            key={lv}
                            title={LEVEL_HINTS[lv]}
                            disabled={savingAuto}
                            onClick={() => saveAutonomy({ level: lv })}
                            className={cn(
                              "flex-1 px-2 py-1 transition-colors disabled:opacity-50",
                              usage.level === lv
                                ? "bg-primary/15 font-medium text-primary"
                                : "text-muted-foreground hover:bg-accent",
                            )}
                          >
                            {lv}
                          </button>
                        ))}
                      </div>
                      <button
                        type="button"
                        disabled={savingAuto}
                        title="暂停/恢复自动消费（L1 插话后自动跑、L2 自动链）；不拦人手显式跑队列，取消暂停不自动恢复已停的链"
                        onClick={() => saveAutonomy({ paused: !usage.paused })}
                        className={cn(
                          "w-full rounded-md border px-2 py-1 text-xs disabled:opacity-50",
                          usage.paused
                            ? "border-(--status-paused) text-(--status-paused)"
                            : "text-muted-foreground hover:bg-accent",
                        )}
                      >
                        {usage.paused ? "⏸ 自动已暂停（点击恢复）" : "⏸ 暂停自动"}
                      </button>
                      <label className="mt-2 flex cursor-pointer items-center gap-1.5 border-t pt-2 text-xs"
                             title={deriveLamp(usage).title}>
                        <input type="checkbox" disabled={savingAuto}
                               checked={usage.auto_derive ?? false}
                               onChange={(e) => {
                                 saveAutonomy({ auto_derive: e.target.checked })
                                 // C2：开启即启动——立即触发一轮编排（原「保存即启动」语义）
                                 if (e.target.checked) void orchTick()
                               }} />
                        <span>任务空时自动派生（L1 生效）</span>
                      </label>
                    </div>
                  )}
                </div>
              )}
              {/* ∑ 用量迷你 chip（点击开预算编辑，BudgetPopover 原样） */}
              {usage && (
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setBudgetOpen((v) => !v)}
                    title={`会话上限 / Token / 任务预算（点击编辑）${usage.tokens.cache_hit ? ` · 缓存命中 ${Math.round(usage.tokens.cache_hit * 100)}%` : ""}`}
                    className="flex h-7 items-center gap-2 rounded-full border px-2.5 font-mono text-xs hover:bg-accent"
                  >
                    <span className={usage.tokens.budget
                      ? (usage.tokens.pct ?? 0) >= 1
                        ? "text-(--status-error)"
                        : (usage.tokens.pct ?? 0) >= 0.8 ? "text-amber-400" : "text-muted-foreground"
                      : "text-muted-foreground"}>
                      ∑ {fmtTokens(usage.tokens.used)}
                      {usage.tokens.budget
                        ? `/${fmtTokens(usage.tokens.budget)} ${Math.round((usage.tokens.pct ?? 0) * 100)}%`
                        : ""}
                    </span>
                    <span className="text-muted-foreground">
                      🪟 {usage.active_sessions}/{usage.sessions_cap}
                    </span>
                  </button>
                  {budgetOpen && (
                    <BudgetPopover
                      usage={usage}
                      onClose={() => setBudgetOpen(false)}
                      onSave={saveAutonomy}
                    />
                  )}
                </div>
              )}
              {/* ⛓ L2 链状态（仅急停/进行中时出现，5s usage 轮询刷新） */}
              {usage?.chain?.estranged ? (
                <span
                  title="链因服务重启已急停：状态保留但不会自动恢复，点「恢复自动链」重新启动"
                  className="flex h-7 animate-pulse items-center rounded-full border border-amber-400 px-2.5 font-mono text-xs text-amber-400"
                >
                  ⛓ 急停
                </span>
              ) : usage?.chain?.active ? (
                <span
                  title={`L2 自动链进行中：链内已自动 ${usage.chain.ticks}/${usage.max_chain_ticks} 轮（本项目累计 ${usage.chain.auto_ticks_total} 轮自动 tick）`}
                  className="flex h-7 items-center rounded-full border border-primary px-2.5 font-mono text-xs text-primary"
                >
                  ⛓ {usage.chain.ticks}/{usage.max_chain_ticks}
                </span>
              ) : null}
              {/* ⋯ 更多：低频动作收弹层 */}
              <div className="relative">
                <button type="button" onClick={() => setMoreOpen((v) => !v)}
                  title="清理工作区"
                  className={ORCH_CHIP_CLS}>
                  ⋯
                </button>
                {moreOpen && (
                  <div className="absolute bottom-9 left-0 z-10 w-44 rounded-lg border bg-popover p-2 shadow-md">
                    {/* 工作区隔离（W3）：清空 scratch + .tmp（临时工作区），正式产物 artifacts/ 不动 */}
                    <Button size="sm" variant="outline" className="w-full"
                            title="清空项目自由工作区（scratch 临时文件与 .tmp 系统临时重定向区）；正式产物 artifacts/ 不受影响"
                            onClick={async () => {
                              if (!window.confirm("清空本项目 scratch/.tmp 临时工作区？正式产物不受影响。")) return
                              try {
                                const r = await api.clearScratch(pid)
                                setJobInfo(`已清理临时工作区 ${r.removed} 项${r.failed.length ? `，${r.failed.length} 项被占用跳过` : ""}`)
                              } catch (e) {
                                setJobInfo(`清理失败：${e}`)
                              }
                            }}>
                      🧹 清理工作区
                    </Button>
                  </div>
                )}
              </div>
            </>
          )}
          {activeSession && (
            <button type="button"
              title="切换当前会话的供应商/模型，下一次 LLM 调用生效"
              onClick={() => {
                setPopProvider(sessionModelOf?.provider ?? enabledProviders[0]?.name ?? "")
                setModelPopOpen((v) => !v)
              }}
              className={cn("max-w-52 rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                modelPopOpen && "bg-accent text-foreground")}>
              <span className="block truncate">⚙ {sessionModelOf ? `${sessionModelOf.provider}/${sessionModelOf.model}` : "默认模型"}</span>
            </button>
          )}
          {/* 会话上下文 chips（2026-09-19 自控制组迁入）：跑任务队列（armed 空闲）/ 继续（暂停态）/ 复盘 */}
          {activeSession && activeStatus === "paused" && (
            <button type="button"
              title={budgetPausedSids.has(activeSession.id)
                ? "步数预算用尽自动暂停：从快照+步数断点恢复（默认自动 +200 步，落审计）；恢复后可用「引导会话」补充指示"
                : "从快照恢复被暂停的任务"}
              onClick={() => void controlSession("resume", activeSession.id)}
              className="rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
              ▶ 继续
            </button>
          )}
          {activeSession && (
            <button type="button" disabled={reviewing}
              title="F8 会话级复盘：planner LLM 复盘本会话跑过的任务/命令/文档引用，把验证有效的手法沉淀为变更提案（人类审批后才落盘）"
              onClick={reviewSession}
              className="rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50">
              {reviewing ? "复盘中…" : "📋 复盘"}
            </button>
          )}
          <span className="min-w-0 flex-1" />
          {/* 发送钮双态（2026-09-19 定稿 Claude Code 式）：会话有任务在身（running/paused）
              时 ↑ 变 ■——单击立即中断（后端即点即停：LLM 等待/命令执行中直接掐断，
              任务 fail「人工中断」快照保留可续跑，不再等当前步做完）；否则 ↑=发送。
              运行中发引导走 Enter（入收件箱排队，卡片上方排队条可管理）。 */}
          {canAbort && activeSession ? (
            <button type="button"
              title="停止：立即中断当前任务（任务标记失败，快照保留可续跑）"
              onClick={() => void controlSession("abort", activeSession.id)}
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground transition-opacity hover:opacity-90">
              <Square className="size-3.5 fill-current" />
            </button>
          ) : (
            <button type="button" onClick={sendRemark} disabled={!canSend}
              title="发送（Enter；Shift+Enter 换行；附件上传中不可发送）"
              className="flex size-8 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40">
              <ArrowUp className="size-4" />
            </button>
          )}
        </div>
        </div>{/* 输入卡片闭合 */}
        {/* 模型选择浮层（BudgetPopover 形态）：供应商列 → 展开该供应商模型，选定即切 + 关 */}
        {modelPopOpen && activeSession && (
          <div className="absolute bottom-full left-3 z-20 mb-2 max-h-80 w-72 overflow-y-auto rounded-md border bg-popover p-1 shadow-md">
            {enabledProviders.length === 0 && (
              <div className="px-2 py-3 text-xs text-muted-foreground">无可用供应商（请在设置页配置）</div>
            )}
            {enabledProviders.map((p) => (
              <div key={p.name}>
                <button type="button" onClick={() => setPopProvider(p.name)}
                  className={cn("flex w-full items-center justify-between rounded px-2 py-1 text-xs transition-colors hover:bg-accent",
                    popProvider === p.name && "bg-accent font-medium text-foreground")}>
                  <span>{p.name}</span>
                  {sessionModelOf?.provider === p.name && (
                    <span className="text-[10px] text-muted-foreground">当前供应商</span>
                  )}
                </button>
                {popProvider === p.name && (
                  <div className="ml-3 border-l pl-1">
                    {(p.models ?? []).map((m) => (
                      <button key={m} type="button" onClick={() => switchLlm(p.name, m)}
                        className={cn("flex w-full items-center justify-between rounded px-2 py-1 font-mono text-xs transition-colors hover:bg-accent",
                          sessionModelOf?.provider === p.name && sessionModelOf?.model === m && "text-primary")}>
                        <span className="min-w-0 truncate">{m}</span>
                        {sessionModelOf?.provider === p.name && sessionModelOf?.model === m && (
                          <span className="shrink-0 text-[10px]">✓ 当前</span>
                        )}
                      </button>
                    ))}
                    <button type="button" onClick={() => switchLlm(p.name, "")}
                      className="flex w-full items-center justify-between rounded px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent">
                      <span>默认模型</span>
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
        <input ref={fileInputRef} type="file" multiple className="hidden"
               onChange={(e) => { pickFiles(e.target.files); e.target.value = "" }} />
      </div>
      </>
      )}

      {personaOpen && (
        <div className="absolute inset-0 z-30 flex items-center justify-center bg-black/50 p-4"
             onClick={() => setPersonaOpen(false)}>
          <div onClick={(e) => e.stopPropagation()}>
            <PersonaEditor
              initial={orchMeta?.persona ?? null}
              onClose={() => setPersonaOpen(false)}
              onSave={async (body) => { await api.setOrchPersona(pid, body); await refreshOrchMeta() }}
            />
          </div>
        </div>
      )}

      {/* 团队（2026-10-06）：查看并配置弹窗 + 运行报告全屏视图 */}
      {teamConfigId && (
        <TeamConfigDialog pid={pid} teamId={teamConfigId}
          onClose={() => setTeamConfigId(null)}
          onStarted={() => { const tid = teamConfigId; setTeamConfigId(null); setTeamReportId(tid) }} />
      )}
      {teamReportId && (
        <TeamRunReport pid={pid} teamId={teamReportId}
          onClose={() => setTeamReportId(null)}
          onOpenSession={(sid) => { setTeamReportId(null); setActiveTab(sid) }} />
      )}
    </div>
  )
}
