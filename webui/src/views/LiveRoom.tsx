import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ArrowUp, GitFork, Paperclip, Square, X } from "lucide-react"

// A3 任务流视图（第三个 React Flow 图）：懒加载，@xyflow/react 不进直播间主包
const TaskFlow = lazy(() => import("./live/TaskFlow").then((m) => ({ default: m.TaskFlow })))
import { PlanPanel } from "./live/PlanPanel"
import { EventRow, type StreamItem } from "./live/EventRow"
import { ApiError, api, pollJob } from "@/lib/api"
import { eventStyle } from "@/lib/events"
import { roleName, sessionLabel } from "@/lib/roles"
import { useEvents } from "@/lib/useEvents"
import { fmtDateTimeMin, parseTs } from "@/lib/datetime"
import type { AttachmentInfo, Autonomy, BBEvent, ModelInfo, OrchProposal, OrchTickResult, ProjectUsage, ReplanResult, RoleInfo, Session, Task } from "@/lib/types"
import { StatusDot, type SessionStatus } from "@/components/StatusDot"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

// 核心页（DESIGN.md §12）：Agent 直播间——多会话页签 + 状态点 + 事件流 + 插话

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

// 输入行模式徽章（2026-09-19 Claude Code 化；2026-09-20 会话窗对话化改双模式）：
// 会话态=「对话/引导」+「指派任务」两态循环，**显徽章**（双模式必须可辨——偏离
// 09-19 单模式不显徽章定稿，定稿翻转随对话化落地）；编排器态=「发任务/指挥编排」。
type InputMode = "task" | "note" | "assign" | "directive"
const MODE_LABELS: Record<InputMode, string> = {
  task: "发任务", note: "对话/引导", assign: "指派任务", directive: "指挥编排",
}
const MODE_HINTS: Record<InputMode, string> = {
  task: "以 human 名义向黑板发布一个 passive 任务（插话）；Enter 发送，Shift+Enter 换行",
  note: "与当前选中会话对话（Claude Code 式）：空闲 Agent 直接流式回复，可跑命令/查黑板/登记结论；任务进行中则轮末注入不打断当前工作；可携带附件",
  assign: "指派任务给当前选中会话：target_session 锁定该窗，后端自动武装并立即起跑；Enter 发送，可携带附件",
  directive: "指挥编排器（C2）：一次性目标指令，自动触发一轮编排；不支持附件",
}
const modesForCtx = (hasSession: boolean): InputMode[] =>
  hasSession ? ["note", "assign"] : ["task", "directive"]

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

/** C2 作战计划状态灯四态（§6.9，2026-09-18）：灯色反映真实派生状态——
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
  return (
    <div className="absolute right-0 top-9 z-20 w-60 rounded-lg border bg-popover p-3 text-xs shadow-md">
      <p className="mb-2 text-muted-foreground">资源上限与预算（任何档位安全层不放松）</p>
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

/** 轨级行动边界编辑弹层（R2：mode 退役→轨级，§6.9 2026-09-17）。
 * redteam 轨（红队行动：ROE 四要素 + mission）与 pentest 轨（作战计划：
 * mission + 默认判据「渗透默认」）两轨渲染；ROE 四要素块仅 redteam，
 * 不再强制（缺省=按 pentest 上限兜底，usage.roe_complete 提示补全）。 */
function ModePopover({ usage, track, onClose, onSave }: {
  usage: ProjectUsage
  track: "pentest" | "redteam"
  onClose: () => void
  onSave: (patch: Record<string, unknown>) => Promise<void>
}) {
  const isRedteam = track === "redteam"
  const builtinName = isRedteam ? "红队默认" : "渗透默认"
  const builtinKey = isRedteam ? "__b_redteam" : "__b_pentest"
  const [text, setText] = useState(usage.mission?.text ?? "")
  const [criteria, setCriteria] = useState(usage.mission?.criteria ?? "")
  const [autoDerive, setAutoDerive] = useState(usage.auto_derive ?? false)
  const [templateName, setTemplateName] = useState(usage.criteria_template ?? "")
  const [tplName, setTplName] = useState("")
  const [templates, setTemplates] = useState<{ builtin: Record<string, string>; user: Record<string, string> }>({ builtin: {}, user: {} })
  const [targets, setTargets] = useState(usage.redteam_roe?.targets ?? "")
  const [window_, setWindow_] = useState(usage.redteam_roe?.window ?? "")
  const [exclusions, setExclusions] = useState(usage.redteam_roe?.exclusions ?? "")
  const [approver, setApprover] = useState(usage.redteam_roe?.approver ?? "")
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState("")
  // 已应用的内置模板（""=无）：下拉选择后保持显示所选项，不复位回占位符
  const [appliedBuiltin, setAppliedBuiltin] = useState<"" | "__b_redteam" | "__b_pentest">(
    () => (usage.criteria_template === "红队默认" ? "__b_redteam"
      : usage.criteria_template === "渗透默认" ? "__b_pentest" : ""))
  const prefilledRef = useRef(false)

  useEffect(() => {
    api.judgmentTemplates().then(setTemplates).catch(() => {})
  }, [])

  // 打开弹层时判据为空 → 自动预填轨内置默认判据（templates 首包到位后一次）；
  // pentest 轨 mission 目标同时预填默认作战计划「挖掘更多漏洞」
  useEffect(() => {
    if (prefilledRef.current || !Object.keys(templates.builtin).length) return
    if (!criteria.trim()) {
      const text0 = templates.builtin[builtinName]
      if (text0) {
        setCriteria(text0)
        setAppliedBuiltin(builtinKey)
      }
    }
    if (!isRedteam && !text.trim()) setText("挖掘更多漏洞")
    prefilledRef.current = true
  }, [templates.builtin, criteria])

  const save = async () => {
    // R2：ROE 不再强制——留空的键会被服务端归一化剥除（行为按 pentest 上限兜底）
    setSaving(true)
    setErr("")
    try {
      const patch: Record<string, unknown> = {
        criteria_template: templateName
          || (appliedBuiltin === "__b_redteam" ? "红队默认"
            : appliedBuiltin === "__b_pentest" ? "渗透默认" : ""),
        autonomy: {
          level: usage.level, paused: usage.paused,
          sessions_cap: usage.sessions_cap, max_chain_ticks: usage.max_chain_ticks,
          token_budget: usage.token_budget, task_budget: usage.task_budget,
          auto_derive: autoDerive,
        },
      }
      if (text.trim() || criteria.trim()) {
        patch.mission = { text: text.trim(), criteria: criteria.trim() }
      }
      if (isRedteam) {
        patch.redteam_roe = {
          targets: targets.trim(), window: window_.trim(),
          exclusions: exclusions.trim(), approver: approver.trim(),
        }
      }
      await onSave(patch)
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
      <p className="mb-2 flex items-center gap-1.5 text-muted-foreground">
        <span
          className={cn("inline-block size-2 rounded-full", deriveLamp(usage).cls)}
          title={deriveLamp(usage).title}
        />
        <span className={cn("shrink-0", deriveLamp(usage).textCls)}
              title={deriveLamp(usage).title}>
          {deriveLamp(usage).label}
        </span>
        行动边界：{isRedteam ? "红队行动" : "作战计划"}与 mission（§6.9；安全红线不放松）
      </p>
      <label className="mb-2 flex cursor-pointer items-center gap-1.5">
        <input type="checkbox" checked={autoDerive} onChange={(e) => setAutoDerive(e.target.checked)} />
        <span>任务空时自动派生新任务（L1/L2 生效）</span>
      </label>
      {isRedteam && <div className="mb-2 space-y-1 rounded border border-(--status-approval)/40 p-2">
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
      </div>}
      <label className="mb-1 block text-muted-foreground">判据（自动派生的方向；每行一条）</label>
      <div className="mb-1 flex gap-1">
        <select
          className="h-7 min-w-0 flex-1 rounded border bg-background px-1 text-[11px]"
          value={templateName ? templateName : appliedBuiltin}
          onChange={(e) => {
            const name = e.target.value
            if (!name) return
            if (name === "__b_redteam" || name === "__b_pentest") {
              setCriteria(templates.builtin[name === "__b_redteam" ? "红队默认" : "渗透默认"] ?? "")
              setTemplateName("")
              setAppliedBuiltin(name)
              return
            }
            if (name in templates.user) {
              setCriteria(templates.user[name])
              setTemplateName(name)
              setAppliedBuiltin("")
            }
          }}
        >
          <option value="">应用判据模板…</option>
          <optgroup label="内置">
            {isRedteam
              ? <option value="__b_redteam">内置：红队默认</option>
              : <option value="__b_pentest">内置：渗透默认</option>}
          </optgroup>
          {Object.keys(templates.user).length > 0 && (
            <optgroup label="我的模板">
              {Object.keys(templates.user).map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </optgroup>
          )}
        </select>
      </div>
      <textarea className={field} rows={8} value={criteria} onChange={(e) => setCriteria(e.target.value)}
                placeholder="□ 判据一&#10;□ 判据二（写判据 = 授权自动派生往此方向打）" />
      <div className="mb-2 mt-1 flex gap-1">
        <input className={cn(field, "min-w-0 flex-1")} value={tplName} onChange={(e) => setTplName(e.target.value)}
               placeholder="存为模板（名称）" />
        <Button size="sm" variant="outline"
                disabled={!tplName.trim() || !criteria.trim()}
                onClick={async () => {
                  const merged = { ...templates.user, [tplName.trim()]: criteria }
                  setTemplates((t) => ({ ...t, user: merged }))
                  setTemplateName(tplName.trim())
                  await api.saveJudgmentTemplates(merged)
                }}>
          存模板
        </Button>
        {templateName && templates.user[templateName] !== undefined && (
          <Button size="sm" variant="outline"
                  onClick={async () => {
                    const merged = { ...templates.user }
                    delete merged[templateName]
                    setTemplates((t) => ({ ...t, user: merged }))
                    setTemplateName("")
                    await api.deleteJudgmentTemplate(templateName)
                  }}>
            删
          </Button>
        )}
      </div>
      <label className="mb-1 block text-muted-foreground">{isRedteam ? "mission 目标" : "作战计划目标"}（可选；留空则只用判据驱动）</label>
      <textarea className={field} rows={3} value={text} onChange={(e) => setText(e.target.value)}
                placeholder={isRedteam ? "战役目标一句话" : "默认：挖掘更多漏洞"} />
      <p className="mt-2 rounded border bg-card p-2 text-[10px] leading-relaxed text-muted-foreground">
        <span className="font-medium text-foreground">启动方式：</span>
        ① 勾选「任务空时自动派生」② 点保存——勾选状态下会立即启动一轮编排，之后任务空了自动续批；
        也可用输入框「指挥编排」直接下达一次性指令。判据全部达成或资产穷尽（uncovered=0）时自动收工。
      </p>
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
      k === "project.digest" || k === "finding.new" || k === "advisor.intervention" },
  { key: "route", label: "路由", match: (k: string) =>
      k === "skill.routed" || k === "skill.open" || k === "kb.open" || k === "kb.search" },
  { key: "command", label: "命令", match: (k: string) =>
      k === "command" || k === "command.result" || k === "audit.deny" },
  { key: "tools", label: "工具", match: (k: string) => k === "tool.call" },
  { key: "finding", label: "发现", match: (k: string) =>
      k.startsWith("finding.") || k === "func.upsert" || k === "asset.new" },
  // 「计划」是特殊标签：选中时主区渲染 PlanPanel（结构化任务计划/进度）而非事件流，match 仅供类型完整
  { key: "plan", label: "计划", match: (k: string) =>
      k === "task.plan_set" || k === "task.plan_revised" || k === "task.step" },
] as const

// 编排页签收录的任务状态变迁事件（编排发布任务的，按 payload.created_by=orchestrator
// 关联；计划步进属会话内部执行细节，留会话页签——2026-09-18 定稿）
const ORCH_TASK_EVENTS = new Set([
  "task.claimed", "task.done", "task.failed", "task.reopened", "task.lease_expired",
])

function sessionStatus(mine: BBEvent[]): SessionStatus {
  if (mine.some((e) => e.kind === "session.finished")) return "finished"
  // 控制事件优先：paused 后无 resumed/aborted/finished → 已暂停（§3 会话控制）
  for (let i = mine.length - 1; i >= 0; i--) {
    const k = mine[i].kind
    if (k === "session.paused") return "paused"
    if (k === "session.resumed" || k === "session.aborted" || k === "session.finished") break
  }
  const last = mine[mine.length - 1]
  if (!last) return "idle"
  if (last.kind === "task.failed" || last.kind === "task.lease_expired") return "error"
  // 最近 2 分钟有动静 → 运行中
  const age = Date.now() - parseTs(last.created_at).getTime()
  return age < 2 * 60 * 1000 ? "running" : "idle"
}

export function LiveRoom({ pid, focusSession }: { pid: string; focusSession?: { sid: string; n: number } | null }) {
  // 事件流分页（2026-09-17）：首屏最新 50 条，上翻懒加载更早；已加载缓存常驻不重拉
  const { events, connected, loadedAll, loadingEarlier, loadEarlier } = useEvents(pid)
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeTab, setActiveTab] = useState<string>("__all")
  const [filter, setFilter] = useState<string>("all")
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
  // E8：开窗步数预算（留空=后端默认 200）与输入框模式（随上下文收敛，见 modesForCtx）
  const [maxSteps, setMaxSteps] = useState("")
  const [inputMode, setInputMode] = useState<InputMode>("task")
  const [orchOpen, setOrchOpen] = useState(false)
  const [orchRoles, setOrchRoles] = useState<Set<string>>(new Set())
  // 排队引导条（2026-09-19 轮末语义）：note 发送后 human_note 留收件箱等下一轮
  // 认领期注入，此处仅组件内存的可见排队态；task.claimed 事件到达即清（已注入）
  const [queuedNotes, setQueuedNotes] = useState<{ key: string; sid: string; text: string; attCount: number }[]>([])
  // A3：直播｜任务流 顶栏切换（任务流双击节点挂回会话时自动切回直播）
  const [viewMode, setViewMode] = useState<"live" | "flow">("live")
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
  const refreshUsage = () =>
    api.getProject(pid).then((p) => { setUsage(p.usage); setTrack(p.track ?? null) }).catch(() => {})
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
  // v0.71 任务即窗口：双击任务卡/任务流节点跳来——直开该任务专属窗页签并回直播模式
  useEffect(() => {
    if (!focusSession?.sid) return
    setActiveTab(focusSession.sid)
    setViewMode("live")
  }, [focusSession?.n, focusSession?.sid])  // eslint-disable-line react-hooks/exhaustive-deps
  // v0.71 任务即窗口：任务清单轮询（延续模式判定=绑定任务终态）
  const [tasks, setTasks] = useState<Task[]>([])
  useEffect(() => {
    api.tasks(pid).then(setTasks).catch(() => {})
    const t = setInterval(() => api.tasks(pid).then(setTasks).catch(() => {}), 5000)
    return () => clearInterval(t)
  }, [pid])

  // 角色中文名映射（事件流「提议开窗」摘要 / 会话页签显中文）
  const roleNames = useMemo(
    () => Object.fromEntries(roles.map((r) => [r.role, r.name || r.role])),
    [roles])
  // 页签：全部 / 编排（无 session_id 的编排事件）/ 各会话（已关闭的隐藏；2026-09-19 起 ×=结束会话，A1 摘离退役）
  const tabs: Tab[] = useMemo(() => [
    { key: "__all", label: "全部", sessionId: null },
    { key: "__orch", label: "编排", sessionId: "__orch" },
    ...sessions.filter((s) => s.status !== "closed")
      .map((s) => ({ key: s.id, label: sessionLabel(s, roleNames), sessionId: s.id })),
  ], [sessions, roleNames])
  // 会话收件箱未读数（撤回传播系统私信；与审批收件箱分设，DESIGN §6.7 的 1.5）
  const unreadBySid = useMemo(
    () => new Map(sessions.map((s) => [s.id, s.unread ?? 0])), [sessions])

  const visible = useMemo(() => {
    const f = FILTERS.find((x) => x.key === filter)!
    return events.filter((e) => {
      // 第一步：页签圈定范围
      let inTab: boolean
      if (activeTab === "__orch") {
        // 编排发布任务的全生命周期（2026-09-18）：五类状态变迁带认领会话的
        // session_id，按 payload.created_by 关联进编排页签（须在 session_id
        // 剔除之前判）；旧事件无 created_by → 不显示（退回现状，不回填）。
        if (ORCH_TASK_EVENTS.has(e.kind)) inTab = e.payload.created_by === "orchestrator"
        else if (e.session_id) inTab = false
        else if (e.author === "orchestrator") inTab = true
        // 编排器/顾问源 LLM 失败也属编排视角（worker 源不进，看会话页签/全部）
        else inTab = e.kind === "llm.error"
          && (e.payload.source === "orchestrator" || e.payload.source === "planner")
      } else if (activeTab !== "__all" && e.session_id !== activeTab) {
        inTab = false
      } else {
        inTab = true
      }
      // 第二步：类型过滤（此前 __orch 分支提前 return，四筛选在编排页签失效）
      return inTab && f.match(e.kind)
    })
  }, [events, activeTab, filter])

  const listRef = useRef<HTMLDivElement>(null)
  // 命令对配对（Claude Code 式渲染，DESIGN.md §12）：command/command.result 就地合并为
  // 一条折叠行。新数据按 payload.call_id 配对；存量旧事件无 call_id，降级为「同会话最近
  // 未闭合 command」游标配对（run_cmd 串行执行，会话内相邻性成立），session_id 不符宁走
  // 孤儿渲染不错配。visible 升序 = 落库序（events 表 id 单调，WS 按 id 推进），无需处理乱序。
  const items = useMemo<StreamItem[]>(() => {
    const out: StreamItem[] = []
    // call_id → pair 位置 [ti, pi]：ti=-1 = out 顶层，否则 = 所在 turn 下标
    // （命令对可能入轮过程组，顶层下标不再指向 pair 本体）
    const pendingIdx = new Map<string, [number, number]>()
    let legacy: { loc: [number, number]; event: BBEvent } | null = null
    // 中断双事件去重（任务中断时 task.failed 与 session.aborted 成对落库，见
    // loop.py _abort_finish）：按（会话， 任务）配对，渲染层只留「❌ 任务失败」；
    // 纯会话中断（无同任务失败行，如空闲窗被打断）仍显示「⛔ 人工中断」。审计两条都在。
    const failedKeys = new Set<string>()
    // 思考流式（2026-09-19）：已有终稿 llm.thinking 的 stream_id 集合——终稿到达后
    // 该流的 delta 过渡行跳过（终稿行自带全文+耗时；delta 行后端已清剪，此处兜底旧视图）
    const finalStreams = new Set<string>()
    // 回复流式（2026-09-20 对话化）：终稿 agent.chat 的 stream_id 集合，同上先例
    const finalChats = new Set<string>()
    for (const e of visible) {
      if (e.kind === "task.failed") {
        const tid = typeof e.payload.task_id === "string" ? e.payload.task_id : ""
        if (!tid) continue
        const sid = typeof e.session_id === "string" ? e.session_id
          : typeof e.payload.session_id === "string" ? e.payload.session_id : ""
        failedKeys.add(`${sid} ${tid}`)
      } else if (e.kind === "llm.thinking" && typeof e.payload.stream_id === "string") {
        finalStreams.add(e.payload.stream_id)
      } else if (e.kind === "agent.chat" && typeof e.payload.stream_id === "string") {
        finalChats.add(e.payload.stream_id)
      }
    }
    const liveThinking = new Map<string, number>() // stream_id -> out 中 delta 组下标
    const liveOrphanChat = new Map<string, number>() // 无轮上下文回复 delta → out 下标
    // 对话轮分组（2026-09-20）：human_note 开轮（同会话未收口轮先收口），过程事件
    // （thinking/命令对/tool.call/任务轮叙述）入 process，无 step 的 agent.chat 收口
    // reply。非「全部」筛选下 note/process/reply 不齐 → 空轮回落平铺审计行，零回退。
    const liveChatTurn = new Map<string, number>() // 回复 stream_id → turn 下标
    const liveThinkTurn = new Map<string, [number, number]>() // 思考 stream_id → [turn 下标, process 下标]
    const openTurn = new Map<string, number>() // 会话 → 未收口 turn 下标
    const skey = (ev: BBEvent) => typeof ev.session_id === "string" ? ev.session_id : ""
    type Turn = Extract<StreamItem, { type: "turn" }>
    const turnAt = (idx: number) => out[idx] as Turn
    for (const e of visible) {
      if (e.kind === "command") {
        const cid = typeof e.payload.call_id === "string" ? e.payload.call_id : ""
        let loc: [number, number]
        if (openTurn.has(skey(e))) {
          const ti = openTurn.get(skey(e))!
          turnAt(ti).process.push({ type: "pair", command: e })
          loc = [ti, turnAt(ti).process.length - 1]
        } else {
          out.push({ type: "pair", command: e })
          loc = [-1, out.length - 1]
        }
        if (cid) pendingIdx.set(cid, loc)
        else legacy = { loc, event: e }
      } else if (e.kind === "command.result") {
        const cid = typeof e.payload.call_id === "string" ? e.payload.call_id : ""
        const loc = cid ? pendingIdx.get(cid)
          : legacy && legacy.event.session_id === e.session_id ? legacy.loc : undefined
        if (loc !== undefined) {
          const [ti, pi] = loc
          const cmd = (ti === -1
            ? (out[pi] as Extract<StreamItem, { type: "pair" }>).command
            : (turnAt(ti).process[pi] as Extract<StreamItem, { type: "pair" }>).command)
          const pair: StreamItem = { type: "pair", command: cmd, result: e }
          if (ti === -1) out[pi] = pair
          else turnAt(ti).process[pi] = pair
          if (cid) pendingIdx.delete(cid)
          else legacy = null
        } else {
          out.push({ type: "single", event: e }) // 孤儿 result：单独渲染
        }
      } else if (e.kind === "session.aborted" && e.payload.note === "人工中断") {
        const tid = typeof e.payload.task_id === "string" ? e.payload.task_id : ""
        const sid = typeof e.session_id === "string" ? e.session_id
          : typeof e.payload.session_id === "string" ? e.payload.session_id : ""
        if (!tid || !failedKeys.has(`${sid} ${tid}`)) out.push({ type: "single", event: e })
      } else if (e.kind === "message.inbox" && e.payload.kind === "human_note") {
        const prev = openTurn.get(skey(e))
        if (prev !== undefined) {
          const t = turnAt(prev)
          if (!t.reply && !t.replyStream && t.process.length === 0) {
            out[prev] = { type: "single", event: t.note } // 空轮回落平铺审计行
          }
          openTurn.delete(skey(e))
        }
        out.push({ type: "turn", note: e, process: [] })
        openTurn.set(skey(e), out.length - 1)
      } else if (e.kind === "llm.thinking.delta") {
        // 思考流式增量（2026-09-19）：按 stream_id 组装成一行滚动「思考中…」；
        // 轮上下文在场时入过程组原位替换（liveThinkTurn 记 [轮下标, process 下标]）
        const sid = typeof e.payload.stream_id === "string" ? e.payload.stream_id : ""
        if (!sid || finalStreams.has(sid)) continue
        const inTurn = liveThinkTurn.get(sid)
        if (inTurn && openTurn.get(skey(e)) === inTurn[0]) {
          const t = turnAt(inTurn[0])
          const prev = t.process[inTurn[1]] as Extract<StreamItem, { type: "single" }>
          t.process[inTurn[1]] = { type: "single", event: { ...e, id: prev.event.id } }
        } else if (openTurn.has(skey(e))) {
          const ti = openTurn.get(skey(e))!
          turnAt(ti).process.push({ type: "single", event: e })
          liveThinkTurn.set(sid, [ti, turnAt(ti).process.length - 1])
        } else {
          const idx = liveThinking.get(sid)
          if (idx !== undefined) {
            const prev = out[idx] as Extract<StreamItem, { type: "single" }>
            out[idx] = { type: "single", event: { ...e, id: prev.event.id } }
          } else {
            out.push({ type: "single", event: e })
            liveThinking.set(sid, out.length - 1)
          }
        }
      } else if (e.kind === "agent.chat.delta") {
        // 回复流式增量（2026-09-20）：轮内 replyStream 原位替换（累计全文自愈）；
        // 无轮上下文的孤儿 delta 单行流式渲染（escalation-only 回复等）
        const sid = typeof e.payload.stream_id === "string" ? e.payload.stream_id : ""
        if (!sid || finalChats.has(sid)) continue
        const ti = liveChatTurn.get(sid)
        if (ti !== undefined) {
          turnAt(ti).replyStream = e
        } else if (openTurn.has(skey(e))) {
          const t = openTurn.get(skey(e))!
          turnAt(t).replyStream = e
          liveChatTurn.set(sid, t)
        } else {
          const idx = liveOrphanChat.get(sid)
          if (idx !== undefined) {
            const prev = out[idx] as Extract<StreamItem, { type: "single" }>
            out[idx] = { type: "single", event: { ...e, id: prev.event.id } }
          } else {
            out.push({ type: "single", event: e })
            liveOrphanChat.set(sid, out.length - 1)
          }
        }
      } else if (e.kind === "agent.chat") {
        if (e.payload.step === undefined) {
          // 对话轮回复（无 step）：收口轮；已按 stream_id 关联的先清 replyStream
          const sid = typeof e.payload.stream_id === "string" ? e.payload.stream_id : ""
          const ti = sid ? liveChatTurn.get(sid) : undefined
          if (ti !== undefined) {
            const t = turnAt(ti)
            t.reply = e
            t.replyStream = undefined
            openTurn.delete(skey(e))
          } else if (openTurn.has(skey(e))) {
            const t = openTurn.get(skey(e))!
            turnAt(t).reply = e
            openTurn.delete(skey(e))
          } else {
            out.push({ type: "single", event: e })
          }
        } else if (openTurn.has(skey(e))) {
          // 任务轮叙述行（带 step）：轮在场入过程组，否则平铺（任务页签现状不变）
          turnAt(openTurn.get(skey(e))!).process.push({ type: "single", event: e })
        } else {
          out.push({ type: "single", event: e })
        }
      } else if ((e.kind === "llm.thinking" || e.kind === "tool.call")
                 && openTurn.has(skey(e))) {
        turnAt(openTurn.get(skey(e))!).process.push({ type: "single", event: e })
      } else {
        out.push({ type: "single", event: e })
      }
    }
    // 流扫尾：仍未收口且空过程的轮 → 平铺（引导被任务轮消化/纯排队场景，审计不缺行）
    for (const idx of openTurn.values()) {
      const t = turnAt(idx)
      if (!t.reply && !t.replyStream && t.process.length === 0) {
        out[idx] = { type: "single", event: t.note }
      }
    }
    return out
  }, [visible])
  // 倒序：最新事件 = DOM 首子 = column-reverse 视觉最底 = 滚动原点 0。
  // 贴底由浏览器布局保证（scrollTop 初始/钳制在 0 即最新），零脚本滚动零竞态；
  // 上翻阅读的位置稳定交给浏览器 scroll anchoring。DOM 只渲染已加载的分页
  // （首屏 50 条，上翻按 50 条/批从后端补），不再内存全量+窗口切片。
  const shown = useMemo(() => [...items].reverse(), [items])
  // 切上下文（会话页签/类型筛选）= 用户要看最新：回到滚动原点（最底部）
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = 0
  }, [activeTab, filter])
  // 上翻到视觉顶部附近自动加载更早一页。column-reverse 的滚动原点在最底（最新），
  // Chrome 走负值域（scrollTop=0 贴底，视觉顶部=-max）；取 |scrollTop| 兼容正负两套实现，
  // 距顶 = 可滚动总距离 - 已上翻距离。
  const onListScroll = () => {
    const el = listRef.current
    if (!el) return
    const distTop = el.scrollHeight - el.clientHeight - Math.abs(el.scrollTop)
    if (distTop < 200) void loadEarlier()
  }

  const toggleRow = useCallback((id: number, defaultValue: boolean) =>
    setOverrides((prev) => new Map(prev).set(id, !(prev.get(id) ?? defaultValue))), [])

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
    setOrchOpen(false)
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
      const bits = [`📤 ${r.published.length} 任务`, `🪟 ${r.spawned.length} 窗`]
      if (r.proposals.length) bits.push(`💡 ${r.proposals.length} 提案待采纳`)
      if (r.digest) bits.push("📝 已写简报")
      setJobInfo(`编排完成：${bits.join(" · ")}${r.summary ? ` · ${r.summary}` : ""}`)
    } catch (e) {
      setJobInfo(quotaHint(String(e)) ? `⚠ LLM 配额不足或限流 · 编排失败：${e}` : `编排失败：${e}`)
    }
  }

  // A5 手动重排：planner 一轮只调 set_priorities，只改 open 行；
  // 人类动作不受自主档/30s 去抖/预算限制，tick/重排租约占用时服务端 409。
  const [replanning, setReplanning] = useState(false)
  const replanPriorities = async () => {
    setReplanning(true)
    setJobInfo("重排优先级中…")
    try {
      const { job_id } = await api.replanPriorities(pid)
      const job = await pollJob(job_id, () => {})
      if (job.status !== "done") {
        setJobInfo(`重排失败：${job.error}`)
        return
      }
      const r = job.result as ReplanResult
      if (r.error) {
        setJobInfo(`重排失败：${r.error}`)
        return
      }
      if (r.note) {
        setJobInfo(`重排跳过：${r.note}`)
        return
      }
      if (r.updated.length) {
        const detail = r.updated
          .map((u) => `${u.task_id.slice(-6)}:P${u.old}→P${u.new}`).join("，")
        setJobInfo(`重排完成：${r.updated.length} 条改级（${detail}），跳过 ${r.skipped.length} 条`)
      } else {
        setJobInfo(`重排完成：无调整${r.skipped.length ? `（跳过 ${r.skipped.length} 条）` : ""}`)
      }
    } catch (e) {
      setJobInfo(`重排不可用：${String(e)}`)  // 含 409：tick/重排租约占用
    } finally {
      setReplanning(false)
    }
  }

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

  const toggleOrchRole = (r: string) =>
    setOrchRoles((prev) => {
      const next = new Set(prev)
      if (next.has(r)) next.delete(r)
      else next.add(r)
      return next
    })

  // F9：跑任务队列=启动 worker（armed=true，之后自动接单；⏸暂停即停）
  const runWork = async (sid: string) => {
    setJobInfo("Worker 执行中…")
    try {
      const r = await api.agentWork(sid)
      if (r.already_running) {
        setJobInfo("Worker 已在跑（会话已启动）")
        refreshSessions()
        return
      }
      const job = await pollJob(r.job_id!, () => {})
      const n = Number(job.result ?? 0)
      setJobInfo(job.status === "done"
        ? (n > 0 ? `Worker 完成（${n} 个任务）` : "Worker 完成：队列无可认领任务（已被认领或队列已空）")
        : `Worker 出错：${job.error}`)
      refreshSessions()
    } catch (e) {
      setJobInfo(`Worker 出错：${e}`)
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
  // A3 任务流入参：暂停会话集（tabStatus 稳定事实感知，节点 paused 琥珀态）
  const pausedSids = useMemo(
    () => new Set(sessions
      .filter((s) => s.status !== "closed" && tabStatus(s.id) === "paused")
      .map((s) => s.id)),
    [sessions, tabStatus])
  // A3 任务流 WS bump：只数图关心的事件（3s 轮询兜底，组件内去抖重拉）
  const flowBump = useMemo(
    () => events.filter((e) =>
      e.kind.startsWith("task.") || e.kind === "message.inbox" || e.kind.startsWith("session.")).length,
    [events])

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
      if (activeTab === sid) setActiveTab("__all")
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

  // 输入框（E8，2026-09-19 附件随发；2026-09-20 对话化改双模式）：会话态
  // 「对话/引导」= human_note 私信直达当前选中会话（空闲=后端踢对话轮流式回复，
  // 在跑=轮末注入不打断当前工作，可纯附件无文字）；「指派任务」= 显式指派
  // （target_session 锁定该窗，后端自动武装起跑）。编排器态「发任务」= 以 human
  // 名义发布 passive 任务；「指挥编排」（C2）= 一次性目标指令（不支持附件）。
  // 发送时携带就绪附件，成功后清 chips。
  const sendRemark = async () => {
    const text = remark.trim()
    const readyIds = pendingFiles.filter((f) => f.status === "ready" && f.att).map((f) => f.att!.id)
    if (uploadingAtt) return
    if (inputMode === "assign") {
      // 显式指派（2026-09-20 对话化翻转：v18「空闲打字默认指派」分支删除，指派
      // 收敛到专属模式，避免「想对话却发了任务」的歧义）
      if (!activeSession || !text) return
      const sid = activeSession.id
      setRemark("")
      try {
        await api.publishTask(pid, {
          objective: text, task_type: "generic", noise_budget: "passive",
          target_session: sid,
          ...(readyIds.length ? { attachment_ids: readyIds } : {}),
        })
        const sent = new Set(pendingFiles.filter((f) => f.status === "ready").map((f) => f.key))
        setPendingFiles((fs) => fs.filter((f) => !sent.has(f.key)))
        setJobInfo(`已指派给「${sessionLabel(activeSession, roleNames)}」起跑（看板可查）`)
      } catch (e) {
        setJobInfo(`指派失败：${e}`)
      }
      return
    }
    if (inputMode === "note") {
      if (!activeSession || (!text && readyIds.length === 0)) return
      const sid = activeSession.id
      setRemark("")
      try {
        await api.sessionNote(sid, text, readyIds)
        const sent = new Set(pendingFiles.filter((f) => f.status === "ready").map((f) => f.key))
        setPendingFiles((fs) => fs.filter((f) => !sent.has(f.key)))
        // 轮末语义（2026-09-19）：轮进行中（canAbort）→ human_note 等下一轮认领期
        // 注入，卡片上方排队可见、可「立即发送」提前注入；空闲 → 后端直接踢对话轮
        //（run_chat），Agent 回复落事件流 agent.chat，不显排队条。task.claimed 到达
        // 自动清条（游标 effect 只看新事件）
        if (canAbort) {
          setQueuedNotes((qs) => [...qs, {
            key: `qn-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            sid, text, attCount: readyIds.length,
          }])
        } else {
          setJobInfo("已发送：Agent 正在回复（看事件流「🤖 Agent 回复」）")
        }
      } catch (e) {
        setJobInfo(`引导投递失败：${e}`)
      }
      return
    }
    if (inputMode === "directive") {
      if (!text) return
      setRemark("")
      setJobInfo("指令下发中，编排器拆解任务/分资产/开窗…")
      try {
        const r = await api.orchDirective(pid, text)
        setJobInfo(`指令已下达（#${r.event_id}），编排进行中——结果看任务看板与事件流`)
      } catch (e) {
        setJobInfo(`指令失败：${e}`)
      }
      return
    }
    if (!text) return  // 任务 objective 必填（纯附件场景走「引导会话」）
    setRemark("")
    try {
      await api.publishTask(pid, {
        objective: text, task_type: "generic", noise_budget: "passive",
        ...(readyIds.length ? { attachment_ids: readyIds } : {}),
      })
      const sent = new Set(pendingFiles.filter((f) => f.status === "ready").map((f) => f.key))
      setPendingFiles((fs) => fs.filter((f) => !sent.has(f.key)))
    } catch (e) {
      setJobInfo(`插话失败：${e}`)
    }
  }

  // 批 6 L0：采纳编排提案——走人类既有写口（POST /tasks、POST /agents），
  // 实体 created_by=human，责任不挂编排；成功后按钮仅在本页内存态置灰。
  const adoptProposal = async (ev: BBEvent) => {
    const p = ev.payload as unknown as OrchProposal
    if (!p || typeof p !== "object" || adopted.has(ev.id) || adoptingId !== null) return
    setAdoptingId(ev.id)
    try {
      if (p.op === "publish_task") {
        const a = p.args
        const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined)
        const strs = (v: unknown): string[] | undefined =>
          Array.isArray(v) && v.every((x) => typeof x === "string") ? (v as string[]) : undefined
        await api.publishTask(pid, {
          objective: String(a.objective ?? ""),
          scope: str(a.scope),
          task_type: str(a.task_type),
          noise_budget: str(a.noise_budget),
          priority: typeof a.priority === "number" ? a.priority : undefined,
          conflict_keys: strs(a.conflict_keys),
          refs: strs(a.refs),
          parent_id: str(a.parent_id),  // C1 编排拆解：透传父子关系（任务流实线/撤回子树依赖）
        })
        setJobInfo("已采纳任务提案：以人类名义入队（L0 不自动跑队列，需要时点「跑队列」）")
      } else if (p.op === "spawn_session") {
        const r = await api.spawnAgent(pid, String(p.args.role ?? ""))
        await refreshSessions()
        void refreshUsage()
        setJobInfo(r.warning ? `已采纳开窗提案（⚠ ${r.warning}）` : "已采纳开窗提案：以人类名义开了窗（L0 不自动跑队列）")
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
  // v0.71：当前会话绑定的任务（bound_task_id 优先；延续模式=任务已终态，续聊不接新任务）
  const activeTask = activeSession
    ? (tasks.find((t) => t.id === activeSession.bound_task_id)
       ?? tasks.find((t) => t.target_session === activeSession.id))
    : null
  const continuing = !!activeTask && (activeTask.status === "done" || activeTask.status === "failed")
  // 输入行模型徽章（2026-09-19）：当前会话记忆中的模型（无值=后端默认）与发送可用性
  const sessionModelOf = activeSession ? sessionModel[activeSession.id] : undefined
  const uploadingAtt = pendingFiles.some((f) => f.status === "uploading")
  const hasReadyAtt = pendingFiles.some((f) => f.status === "ready")
  const canSend = uploadingAtt ? false
    : inputMode === "directive" ? remark.trim().length > 0
    : inputMode === "note" ? (!!activeSession && (remark.trim().length > 0 || hasReadyAtt))
    : remark.trim().length > 0
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
  // 回到编排器=发任务，徽章只在编排器态显示。上下文切换即归位缺省模式。
  const hasSessionCtx = activeSession != null
  useEffect(() => {
    setInputMode(hasSessionCtx ? "note" : "task")
  }, [hasSessionCtx])

  // 排队引导条自动清理：会话下一轮 task.claimed 到达 = 认领期 drain 已把引导注入
  // 排队条清理：只看新到达的 task.claimed——用游标记上次扫到的事件 id，
  // 避免历史事件（加载更早/重连重放）每次 events 变化都误清排队条
  const claimCursor = useRef(0)
  useEffect(() => {
    let maxId = claimCursor.current
    const claimed = new Set<string>()
    for (const e of events) {
      if (e.id <= claimCursor.current) continue
      maxId = Math.max(maxId, e.id)
      if (e.kind !== "task.claimed") continue
      const sid = e.session_id ?? String((e.payload as { claimed_by?: string } | null)?.claimed_by ?? "")
      if (sid) claimed.add(sid)
    }
    claimCursor.current = maxId
    if (claimed.size === 0) return
    setQueuedNotes((qs) => qs.filter((q) => !claimed.has(q.sid)))
  }, [events])

  // 排队条「立即发送」：中断当前轮（abort 硬中断）+ armed 窗自动跑下一轮——认领期
  // drain 即把排队引导注入；队列已空时 runWork 空退，引导等下次「跑任务队列」注入
  const sendQueuedNow = async (key: string) => {
    const q = queuedNotes.find((x) => x.key === key)
    if (!q) return
    const armed = sessionsById.get(q.sid)?.worker_armed
    setQueuedNotes((qs) => qs.filter((x) => x.key !== key))
    await controlSession("abort", q.sid)
    if (armed) void runWork(q.sid)
    setJobInfo("已中断本轮：引导将在下一轮认领时注入（队列已空则等下次「跑任务队列」）")
  }

  return (
    <div className="flex h-full flex-col">
      {/* 第一行：会话页签（多了横向滚动，不换行）+ live 状态点 */}
      <div className="flex h-9 shrink-0 items-center gap-1 border-b px-2">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {tabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setActiveTab(t.key)}
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
          ))}
        </div>
        {/* A3：直播｜任务流 分段切换（原生 button，避开 radix Tabs mousedown 激活坑） */}
        <div className="mr-1 ml-1 flex shrink-0 items-center rounded-md border p-0.5 text-[11px]">
          <button
            type="button"
            onClick={() => setViewMode("live")}
            className={cn(
              "rounded px-2 py-0.5 whitespace-nowrap",
              viewMode === "live" ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-accent",
            )}
          >
            直播
          </button>
          <button
            type="button"
            title="任务流：parent 分解实线 + 会话私信虚线（节点持久，双击挂回/定位）"
            onClick={() => setViewMode("flow")}
            className={cn(
              "flex items-center gap-1 rounded px-2 py-0.5 whitespace-nowrap",
              viewMode === "flow" ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-accent",
            )}
          >
            <GitFork className="size-3" />任务流
          </button>
        </div>
        <span className={cn("ml-1 shrink-0 font-mono text-[10px]", connected ? "text-primary" : "text-(--status-error)")}>
          {connected ? "● live" : "○ 重连中"}
        </span>
      </div>
      {viewMode === "live" && (
      <>

      {/* 批 5 §6.8：重启=急停。DB 链活但本进程无标记 → 提示手动编排恢复 */}
      {usage?.chain?.estranged && (
        <div className="flex flex-wrap items-center gap-2 border-b border-amber-400/40 bg-amber-400/10 px-3 py-1 text-xs text-amber-400">
          <span>
            ⛓ L2 自动链因服务重启已急停（链内自动 {usage.chain.ticks} 轮），不会自动恢复。
          </span>
          <button type="button" className="underline hover:no-underline"
                  onClick={() => setOrchOpen(true)}>
            点此「编排一轮」手动恢复（轮数清零重算）
          </button>
        </div>
      )}

      {/* 类型过滤（jobInfo 可能是数千字符的编排 summary——min-w-0+truncate 单行省略，
          悬停 title 看全文；按钮 shrink-0 永不被长文案挤成竖排，2026-09-18） */}
      <div className="flex gap-1 border-b px-3 py-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={cn(
              "shrink-0 rounded px-2 py-0.5 text-xs",
              filter === f.key ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent",
            )}
          >
            {f.label}
          </button>
        ))}
        {jobInfo && (
          <span className="ml-auto min-w-0 truncate font-mono text-xs text-muted-foreground"
                title={jobInfo}>
            {jobInfo}
          </span>
        )}
      </div>

      {/* 「计划」标签：结构化计划/进度面板替代事件流（GET /tasks，3s 轮询仅在本标签激活时跑） */}
      {filter === "plan" ? (
        <PlanPanel pid={pid} activeTab={activeTab} sessions={sessions} />
      ) : (
      <div
        ref={listRef}
        onScroll={onListScroll}
        className="flex min-h-0 flex-1 flex-col-reverse overflow-auto px-3"
      >
        {shown.map((item) => {
            const primary = item.type === "pair" ? item.command
              : item.type === "turn" ? item.note : item.event
            const primaryKind = item.type === "pair" ? "command"
              : item.type === "turn" ? "message.inbox" : item.event.kind
            const style = eventStyle(primaryKind, primary.payload)
            // turn 的 open=过程组折叠态（默认收起；键复用 note.id，与顶层折叠记忆统一）
            const open = item.type === "turn"
              ? overrides.get(primary.id) ?? false
              : overrides.get(primary.id) ?? style.defaultOpen
            return (
              <EventRow
                key={primary.id}
                item={item}
                open={open}
                onToggle={toggleRow}
                roleNames={roleNames}
                action={item.type === "single" && item.event.kind === "orch.proposed" ? (
                  <button
                    type="button"
                    title="以人类名义采纳：走与手动发任务/开窗相同的写口（created_by=human）"
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
                ) : undefined}
                onRouteJump={handleRouteJump}
              />
            )
          })}
          {!loadedAll ? (
            <button
              type="button"
              onClick={() => void loadEarlier()}
              disabled={loadingEarlier}
              className="shrink-0 rounded border bg-card px-2 py-1 text-center text-xs text-muted-foreground hover:bg-accent disabled:opacity-50"
            >
              {loadingEarlier ? "加载中…" : "↑ 加载更早"}
            </button>
          ) : (
            events.length > 0 && (
              <span className="shrink-0 py-1 text-center text-[10px] text-muted-foreground">
                已到最早
              </span>
            )
          )}
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
        {/* v0.71 延续模式徽章：绑定任务已终态，窗保留可续聊（不接新任务，页签 × 才真关） */}
        {continuing && activeTask && (
          <div className="mb-2 flex items-center gap-2 rounded-md border border-emerald-400/40 bg-emerald-400/5 px-2 py-1 text-[11px] text-emerald-300">
            ✅ 任务已结束 · 延续模式
            <span className="min-w-0 flex-1 truncate text-emerald-300/70"
                  title={activeTask.objective}>
              {activeTask.objective}
            </span>
            <span className="shrink-0 text-emerald-300/60">继续聊属于该任务的延续，不会接新任务</span>
          </div>
        )}
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
        {/* 附件 chips 行（指挥编排模式下半透明保留 + 提示） */}
        {pendingFiles.length > 0 && (
          <div className={cn("mb-2 flex flex-wrap items-center gap-1.5", inputMode === "directive" && "opacity-50")}>
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
            {inputMode === "directive" && (
              <span className="text-[10px] text-muted-foreground">指挥编排不支持附件</span>
            )}
          </div>
        )}
        {/* 输入区：无边框透明多行，Enter 发送 / Shift+Enter 换行，自适应 1-6 行 */}
        <textarea
          ref={taRef}
          rows={1}
          value={remark}
          placeholder={inputMode === "directive"
            ? "指挥编排器：一句话目标（如「对已登记资产做漏洞挖掘」）——自动触发编排拆解/分资产/开窗…"
            : inputMode === "assign"
            ? (activeSession
                ? `指派任务给「${sessionLabel(activeSession, roleNames)}」：锁定该窗执行，后端自动武装并立即起跑…`
                : "指派任务：先选中一个会话页签…")
            : inputMode === "note"
            ? (activeSession
                ? (continuing
                    ? `续聊此任务「${activeTask?.objective.slice(0, 24)}…」：作为该任务的延续对话（不接新任务）…`
                    : canAbort
                    ? `引导「${sessionLabel(activeSession, roleNames)}」：一句话指示，本轮结束后注入（可在上方排队条点「立即发送」中断当前轮）…`
                    : `与「${sessionLabel(activeSession, roleNames)}」对话：Agent 空闲时直接回复，可跑命令、查黑板、登记结论…`)
                : "对话/指派：先选中一个会话页签…")
            : "插话：向黑板发布一个任务或一条指示…"}
          onChange={(e) => { setRemark(e.target.value); autoResize() }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              sendRemark()
            }
          }}
          className="max-h-36 min-h-6 w-full resize-none bg-transparent text-sm leading-6 outline-none placeholder:text-muted-foreground"
        />
        {/* 底部工具行（与输入区之间一条细分隔线）：+ 附件 → 模式徽章 → 模型徽章（仅选中会话）→ 圆形 ↑ 发送 */}
        <div className="mt-1.5 flex items-center gap-1.5 border-t border-white/10 pt-1.5">
          <button type="button"
            disabled={inputMode === "directive"}
            title={inputMode === "directive" ? "指挥编排不支持附件"
              : "添加附件（exe/elf/图片等，单文件 ≤64MB，随「发任务/引导会话」下发，Agent 可 run_cmd 读取）"}
            onClick={() => fileInputRef.current?.click()}
            className={cn("flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
              inputMode === "directive" && "cursor-not-allowed opacity-40 hover:bg-transparent hover:text-muted-foreground")}>
            <Paperclip className="size-4" />
          </button>
          {/* 模式徽章（2026-09-20 对话化：会话态恢复显徽章——对话/指派双模式必须可辨，
              偏离 09-19「单模式不显徽章」定稿；编排器态=task/directive 循环不变）。
              assign 态高亮（写消息=动任务，需醒目），其余弱化 */}
          <button type="button" title={MODE_HINTS[inputMode]}
            onClick={() => setInputMode((m) => {
              const modes = modesForCtx(hasSessionCtx)
              const i = modes.indexOf(m)
              return modes[(i + 1) % modes.length] ?? modes[0]
            })}
            className={cn("rounded-full border px-2.5 py-0.5 text-xs transition-colors hover:bg-accent hover:text-foreground",
              inputMode === "assign"
                ? "border-primary/50 text-primary hover:bg-primary/10 hover:text-primary"
                : "text-muted-foreground hover:bg-accent hover:text-foreground")}>
            {MODE_LABELS[inputMode]}
          </button>
          {/* 编排器态 chips（2026-09-19 顶部第二行工具栏并入 composer，整行删除）：
              ＋开窗 / ⚡编排一轮 / 🎯作战计划 / 🧠自主档 / ∑用量 / ⛓链状态 / ⋯更多，弹层统一向上 */}
          {!hasSessionCtx && (
            <>
              {/* ＋ 开窗：低频动作收弹层——角色/供应商/模型/步数 + 开窗钮 */}
              <div className="relative">
                <button type="button" onClick={() => setWinOpen((v) => !v)}
                  title="手动开一个 Agent 窗（角色/供应商/模型/步数预算）"
                  className={ORCH_CHIP_CLS}>
                  ＋ 开窗
                </button>
                {winOpen && (
                  <div className="absolute bottom-9 left-0 z-10 w-72 rounded-lg border bg-popover p-3 shadow-md">
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
              </div>
              {/* ⚡ 编排一轮：角色多选弹层原样迁入 */}
              <div className="relative">
                <button type="button" onClick={() => setOrchOpen((v) => !v)}
                  title="跑一轮编排：监控/派生/分资产/开窗（轮数预算清零重算）"
                  className={cn(ORCH_CHIP_CLS, "text-primary")}>
                  ⚡ 编排一轮
                </button>
                {orchOpen && (
                  <div className="absolute bottom-9 left-0 z-10 w-64 rounded-lg border bg-popover p-3 text-xs shadow-md">
                    <p className="mb-2 text-muted-foreground">允许编排开窗的角色（不选 = 不限）</p>
                    <div className="flex flex-wrap gap-1.5">
                      {roles.map((r) => (
                        <button
                          key={r.role}
                          onClick={() => toggleOrchRole(r.role)}
                          className={cn(
                            "rounded border px-2 py-0.5",
                            orchRoles.has(r.role)
                              ? "border-primary bg-primary/15 text-primary"
                              : "text-muted-foreground hover:bg-accent",
                          )}
                        >
                          {roleName(r.name, r.role)}
                        </button>
                      ))}
                    </div>
                    <Button size="sm" className="mt-2 w-full" onClick={() => orchTick([...orchRoles])}>
                      开始编排（{orchRoles.size || "不限"}）
                    </Button>
                  </div>
                )}
              </div>
              {/* 🎯 作战计划/红队行动（R2/C2 两轨弹层 + deriveLamp 状态灯）原样迁入 */}
              {(track === "redteam" || track === "pentest") && (
                <div className="relative">
                  <button type="button"
                          className={cn(ORCH_CHIP_CLS, "gap-1.5", usage?.roe_complete === false && "text-(--status-approval)")}
                          title={track === "redteam"
                            ? "行动边界：红队 ROE 四要素与 mission（§6.9；ROE 未核验齐全时行为按渗透测试上限兜底）"
                            : "作战计划：mission 目标 + 判据 + 自动派生（§6.9；保存即启动一轮编排）"}
                          onClick={() => setModeOpen((o) => !o)}>
                    <span
                      className={cn("inline-block size-2 rounded-full", deriveLamp(usage).cls)}
                      title={deriveLamp(usage).title}
                    />
                    🎯 {track === "redteam"
                          ? (usage?.roe_complete === false ? "红队行动 · ROE 未核验" : "红队行动")
                          : "作战计划"}
                  </button>
                  {modeOpen && usage && (
                    <ModePopover
                      usage={usage}
                      track={track as "pentest" | "redteam"}
                      onClose={() => setModeOpen(false)}
                      onSave={async (patch) => {
                        await api.patchProjectConfig(pid, patch)
                        void refreshUsage()
                        setJobInfo(track === "redteam"
                          ? "红队行动边界已更新（ROE 未核验齐全时行为按渗透测试上限兜底；在跑会话维持创建时固化语义）"
                          : "作战计划已更新（自动派生开启时任务空时自动续派）")
                        // C2：保存即启动——勾选自动派生时立即触发一轮编排
                        if ((patch.autonomy as { auto_derive?: boolean } | undefined)?.auto_derive) {
                          void orchTick()
                        }
                      }}
                    />
                  )}
                </div>
              )}
              {/* 🧠 自主档：L0/L1/L2 三选 + 暂停自动收弹层；chip 显当前档与暂停态 */}
              {usage && (
                <div className="relative">
                  <button type="button" onClick={() => setAutoOpen((v) => !v)}
                    title="项目自主级别与自动消费开关（§6.8）"
                    className={cn(ORCH_CHIP_CLS, usage.paused && "text-(--status-paused)")}>
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
                    title="会话上限 / Token / 任务预算（点击编辑）"
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
                    <span className={usage.tasks.budget
                      ? (usage.tasks.pct ?? 0) >= 1
                        ? "text-(--status-error)"
                        : (usage.tasks.pct ?? 0) >= 0.8 ? "text-amber-400" : "text-muted-foreground"
                      : "text-muted-foreground"}>
                      📋 {usage.tasks.published}
                      {usage.tasks.budget ? `/${usage.tasks.budget}` : ""}
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
                  title="链因服务重启已急停：状态保留但不会自动恢复，点「编排一轮」手动恢复（轮数预算清零重算）"
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
                  title="重排优先级 / 清理工作区"
                  className={ORCH_CHIP_CLS}>
                  ⋯
                </button>
                {moreOpen && (
                  <div className="absolute bottom-9 left-0 z-10 w-44 rounded-lg border bg-popover p-2 shadow-md">
                    <Button size="sm" variant="outline" className="mb-1.5 w-full" disabled={replanning} onClick={replanPriorities}
                            title="让 planner 重排待认领任务优先级（0-9，小者优先，只改 open）。手动随时可跑，不受自主档/30s 去抖/预算限制；tick 或重排在跑时返回 409">
                      {replanning ? "重排中…" : "重排优先级"}
                    </Button>
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
          {activeSession && (activeStatus === "armed" || activeStatus === "idle" || activeStatus === "finished") && (
            <button type="button"
              title="F9 启动 worker：自动接任务队列（未启动的窗不自动接单，armed 后待命自动接单）"
              onClick={() => void runWork(activeSession.id)}
              className="rounded-full border px-2.5 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground">
              ▶ 跑任务队列
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

      {/* A3 任务流视图（懒加载分包；页签行常驻，双击有会话节点挂回本视图） */}
      {viewMode === "flow" && (
        <div className="min-h-0 flex-1 border-t">
          <Suspense fallback={
            <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
              任务流加载中…
            </div>
          }>
            <TaskFlow
              key={pid}
              pid={pid}
              pausedSids={pausedSids}
              wsBump={flowBump}
            />
          </Suspense>
        </div>
      )}

      {/* E12 中断确认弹窗已退役（2026-09-19）：中断并入输入行 ■ 钮，单击直接中断（快照保留可续跑） */}
    </div>
  )
}
