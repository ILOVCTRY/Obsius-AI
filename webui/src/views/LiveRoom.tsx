import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react"
import { useVirtualizer } from "@tanstack/react-virtual"
import { GitFork, X } from "lucide-react"

// A3 任务流视图（第三个 React Flow 图）：懒加载，@xyflow/react 不进直播间主包
const TaskFlow = lazy(() => import("./live/TaskFlow").then((m) => ({ default: m.TaskFlow })))
import { api, ApiError, pollJob } from "@/lib/api"
import { eventStyle, eventSummary } from "@/lib/events"
import { useEvents } from "@/lib/useEvents"
import { fmtTime, parseTs, utcTitle } from "@/lib/datetime"
import type { Autonomy, BBEvent, ModelInfo, OrchProposal, OrchTickResult, ProjectUsage, ReplanResult, RoleInfo, Session } from "@/lib/types"
import { StatusDot, type SessionStatus } from "@/components/StatusDot"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"

// 核心页（DESIGN.md §12）：Agent 直播间——多会话页签 + 状态点 + 事件流 + 插话

type Tab = { key: string; label: string; sessionId: string | null }

// 自主级别（DESIGN §6.8）；档位行为已全部落地（批 4 开窗审批 / 批 5 自动链 / 批 6 L0 提案）
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
      {err && <p className="mt-2 text-[--status-error]">{err}</p>}
      <Button size="sm" className="mt-3 w-full" disabled={saving} onClick={save}>
        {saving ? "保存中…" : "保存"}
      </Button>
    </div>
  )
}

/** C2 作战模式编辑弹层（§6.9）：mode 切换（redteam ROE 四要素必填）+ mission 编辑 */
function ModePopover({ usage, onClose, onSave }: {
  usage: ProjectUsage
  onClose: () => void
  onSave: (patch: Record<string, unknown>) => Promise<void>
}) {
  const [mode, setMode] = useState(usage.mode ?? "pentest")
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

  useEffect(() => {
    api.judgmentTemplates().then(setTemplates).catch(() => {})
  }, [])

  const save = async () => {
    if (mode === "redteam" &&
        (!targets.trim() || !window_.trim() || !exclusions.trim() || !approver.trim())) {
      setErr("切换红队必须填写 ROE 四要素（授权目标/时间窗口/禁止事项/授权人）")
      return
    }
    setSaving(true)
    setErr("")
    try {
      const patch: Record<string, unknown> = {
        mode,
        criteria_template: templateName,
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
      if (mode === "redteam") {
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
    <div className="absolute left-0 top-9 z-20 w-72 rounded-lg border bg-popover p-3 text-xs shadow-md">
      <p className="mb-2 flex items-center gap-1.5 text-muted-foreground">
        <span
          className={cn("inline-block size-2 rounded-full",
            usage.auto_derive
              ? (usage.tokens.pct !== null && usage.tokens.pct >= 100) || usage.paused
                ? "bg-[--status-approval]"
                : "bg-[--status-ok]"
              : "bg-muted-foreground/40")}
          title={usage.auto_derive
            ? (usage.paused ? "已暂停：自动派生暂被拦住" : "自动派生已开启：任务空时自动派生新任务")
            : "自动派生已关闭"}
        />
        作战模式与 mission（§6.9；安全红线不放松）
      </p>
      <label className="mb-2 flex cursor-pointer items-center gap-1.5">
        <input type="checkbox" checked={autoDerive} onChange={(e) => setAutoDerive(e.target.checked)} />
        <span>任务空时自动派生新任务（L1/L2 生效）</span>
      </label>
      <div className="mb-2 flex gap-1">
        <button
          className={cn("flex-1 rounded border px-2 py-1",
            mode === "pentest" ? "border-primary/60 bg-primary/10 text-primary" : "text-muted-foreground")}
          onClick={() => setMode("pentest")}
        >
          渗透测试
        </button>
        <button
          className={cn("flex-1 rounded border px-2 py-1",
            mode === "redteam" ? "border-[--status-error]/60 bg-[--status-error]/10 text-[--status-error]" : "text-muted-foreground")}
          onClick={() => setMode("redteam")}
        >
          红队行动
        </button>
      </div>
      {mode === "redteam" && (
        <div className="mb-2 space-y-1 rounded border border-[--status-approval]/40 p-2">
          <p className="text-[10px] text-[--status-approval]">ROE 四要素（必填，留档审计）</p>
          <input className={field} value={targets} onChange={(e) => setTargets(e.target.value)}
                 placeholder="① 授权目标清单" />
          <input className={field} value={window_} onChange={(e) => setWindow_(e.target.value)}
                 placeholder="② 时间窗口" />
          <input className={field} value={exclusions} onChange={(e) => setExclusions(e.target.value)}
                 placeholder="③ 禁止事项" />
          <input className={field} value={approver} onChange={(e) => setApprover(e.target.value)}
                 placeholder="④ 授权人" />
        </div>
      )}
      <label className="mb-1 block text-muted-foreground">判据（自动派生的方向；每行一条）</label>
      <div className="mb-1 flex gap-1">
        <select
          className="h-7 min-w-0 flex-1 rounded border bg-background px-1 text-[11px]"
          value=""
          onChange={(e) => {
            const name = e.target.value
            if (!name) return
            if (name === "__b_pentest") { setCriteria(templates.builtin["渗透默认"] ?? ""); return }
            if (name === "__b_redteam") { setCriteria(templates.builtin["红队默认"] ?? ""); return }
            if (name in templates.user) {
              setCriteria(templates.user[name])
              setTemplateName(name)
            }
          }}
        >
          <option value="">应用判据模板…</option>
          <optgroup label="内置">
            <option value="__b_pentest">内置：渗透默认</option>
            <option value="__b_redteam">内置：红队默认</option>
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
      <textarea className={field} rows={3} value={criteria} onChange={(e) => setCriteria(e.target.value)}
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
      <label className="mb-1 block text-muted-foreground">mission 目标（可选）</label>
      <textarea className={field} rows={2} value={text} onChange={(e) => setText(e.target.value)}
                placeholder="战役目标一句话" />
      {err && <p className="mt-2 text-[--status-error]">{err}</p>}
      <Button size="sm" className="mt-2 w-full" disabled={saving} onClick={save}>
        {saving ? "保存中…" : "保存"}
      </Button>
    </div>
  )
}

const FILTERS = [
  { key: "all", label: "全部", match: () => true },
  { key: "thinking", label: "思考", match: (k: string) => k === "llm.thinking" },
  { key: "decision", label: "决策", match: (k: string) =>
      k.startsWith("task.") || k.startsWith("session.") || k.startsWith("approval.") ||
      k.startsWith("orch.") ||
      k === "project.digest" || k === "finding.new" },
  { key: "route", label: "路由", match: (k: string) => k === "skill.routed" },
  { key: "command", label: "命令", match: (k: string) =>
      k === "command" || k === "command.result" || k === "audit.deny" },
  { key: "finding", label: "发现", match: (k: string) =>
      k.startsWith("finding.") || k === "func.upsert" || k === "asset.new" },
] as const

function sessionStatus(sessionId: string, events: BBEvent[]): SessionStatus {
  const mine = events.filter((e) => e.session_id === sessionId)
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

export function LiveRoom({ pid }: { pid: string }) {
  const { events, connected } = useEvents(pid)
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
  const [switchProvider, setSwitchProvider] = useState("")
  const [switchModel, setSwitchModel] = useState("")
  // E8：开窗步数预算（留空=后端默认 200）与输入框模式（发任务｜引导会话）
  const [maxSteps, setMaxSteps] = useState("")
  const [inputMode, setInputMode] = useState<"task" | "note" | "directive">("task")
  const [orchOpen, setOrchOpen] = useState(false)
  const [orchRoles, setOrchRoles] = useState<Set<string>>(new Set())
  // A1：关页签=本地 detach（后台任务继续跑），可从溢出菜单/任务流视图挂回
  const [detached, setDetached] = useState<Set<string>>(new Set())
  const [detachMenu, setDetachMenu] = useState(false)
  // A3：直播｜任务流 顶栏切换（任务流双击节点挂回会话时自动切回直播）
  const [viewMode, setViewMode] = useState<"live" | "flow">("live")
  // 批 6 L0 提案采纳态只存内存（刷新后可再次采纳，不做服务端去重）
  const [adopted, setAdopted] = useState<Set<number>>(new Set())
  const [adoptingId, setAdoptingId] = useState<number | null>(null)
  // 项目自主配置/用量（§6.8，5s 轮询；闸门服务端实时重读，改配置即时生效）
  const [usage, setUsage] = useState<ProjectUsage | null>(null)
  // C2 作战模式弹层（§6.9）
  const [modeOpen, setModeOpen] = useState(false)
  // E12：中断确认（防误触）——确认后任务标记失败，落盘快照保留，看板 failed 卡可「带现场续跑」
  const [abortTarget, setAbortTarget] = useState<string | null>(null)
  const [budgetOpen, setBudgetOpen] = useState(false)
  const [savingAuto, setSavingAuto] = useState(false)
  const refreshUsage = () =>
    api.getProject(pid).then((p) => setUsage(p.usage)).catch(() => {})
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
      setSwitchProvider(m.default?.provider ?? "")
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

  // 页签：全部 / Orchestrator（无 session_id 的编排事件）/ 各会话（已关闭/已分离的隐藏）
  const tabs: Tab[] = useMemo(() => [
    { key: "__all", label: "全部", sessionId: null },
    { key: "__orch", label: "Orchestrator", sessionId: "__orch" },
    ...sessions.filter((s) => s.status !== "closed" && !detached.has(s.id))
      .map((s) => ({ key: s.id, label: s.name || s.role, sessionId: s.id })),
  ], [sessions, detached])
  // 已分离但会话仍在册（未关窗）的页签——挂回入口
  const detachedSessions = useMemo(
    () => sessions.filter((s) => s.status !== "closed" && detached.has(s.id)),
    [sessions, detached])
  // 会话收件箱未读数（撤回传播系统私信；与审批收件箱分设，DESIGN §6.7 的 1.5）
  const unreadBySid = useMemo(
    () => new Map(sessions.map((s) => [s.id, s.unread ?? 0])), [sessions])

  const visible = useMemo(() => {
    const f = FILTERS.find((x) => x.key === filter)!
    return events.filter((e) => {
      if (activeTab === "__orch" && (e.session_id || e.author !== "orchestrator")) return false
      if (activeTab !== "__all" && activeTab !== "__orch" && e.session_id !== activeTab) return false
      return f.match(e.kind)
    })
  }, [events, activeTab, filter])

  const listRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => 44,
    overscan: 20,
  })
  // 新事件自动滚底（仅当原本就在底部附近）
  const stickToBottom = useRef(true)
  useEffect(() => {
    if (stickToBottom.current) virtualizer.scrollToIndex(visible.length - 1, { align: "end" })
  }, [visible.length])

  const toggleRow = (id: number, defaultValue: boolean) =>
    setOverrides((prev) => new Map(prev).set(id, !(prev.get(id) ?? defaultValue)))

  const enabledProviders = modelInfo?.providers.filter((p) => p.enabled) ?? []
  const spawnTarget = enabledProviders.find((p) => p.name === provider)
    ?? enabledProviders.find((p) => p.name === modelInfo?.default?.provider)
  const switchTarget = enabledProviders.find((p) => p.name === switchProvider)

  const switchLlm = async () => {
    if (!activeSession || !switchTarget) return
    try {
      const r = await api.switchAgentLlm(
        activeSession.id, switchTarget.name, switchModel || undefined)
      setSwitchModel("")
      setJobInfo(`已切换到 ${r.provider}/${r.model}，下一次调用生效`)
    } catch (e) {
      setJobInfo(`切换失败：${e}`)
    }
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
    setJobInfo("编排中…")
    try {
      const { job_id } = await api.orchTick(pid, allowed?.length ? { allowed_roles: allowed } : {})
      const job = await pollJob(job_id, () => {})
      if (job.status !== "done") {
        setJobInfo(`编排失败：${job.error}`)
        return
      }
      const r = job.result as OrchTickResult
      const bits = [`📤 ${r.published.length} 任务`, `🪟 ${r.spawned.length} 窗`]
      if (r.proposals.length) bits.push(`💡 ${r.proposals.length} 提案待采纳`)
      if (r.digest) bits.push("📝 已写简报")
      setJobInfo(`编排完成：${r.summary}（${bits.join(" · ")}）`)
    } catch (e) {
      setJobInfo(`编排失败：${e}`)
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

  // 复盘沉淀：planner LLM 复盘本项目任务 → 变更提案进设置「提案」tab 等人审（无 key 503）
  const [reviewing, setReviewing] = useState(false)
  const reviewProposals = async () => {
    setReviewing(true)
    setJobInfo("复盘沉淀中（planner LLM 复盘任务成败/被引文档）…")
    try {
      const { job_id } = await api.reviewProposals(pid)
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

  const runWork = async (sid: string) => {
    setJobInfo("Worker 执行中…")
    try {
      const { job_id } = await api.agentWork(sid)
      const job = await pollJob(job_id, () => {})
      setJobInfo(job.status === "done" ? `Worker 完成（${String(job.result)} 个任务）` : `Worker 出错：${job.error}`)
    } catch (e) {
      setJobInfo(`Worker 出错：${e}`)
    }
  }

  // A1：× 只 detach（纯本地隐藏，不调后端）——会话与后台任务继续跑，可随时挂回
  const detachSession = (sid: string) => {
    setDetached((prev) => new Set(prev).add(sid))
    if (activeTab === sid) setActiveTab("__all")
  }
  const attachSession = (sid: string) => {
    setDetached((prev) => {
      const next = new Set(prev)
      next.delete(sid)
      return next
    })
    setActiveTab(sid)
    setDetachMenu(false)
  }
  // A3：任务流双击节点挂回——挂回页签并切回直播模式
  const attachFromFlow = (sid: string) => {
    attachSession(sid)
    setViewMode("live")
  }

  // A3 任务流入参：暂停会话集（事件流感知，节点 paused 琥珀态）
  const pausedSids = useMemo(
    () => new Set(sessions
      .filter((s) => s.status !== "closed" && sessionStatus(s.id, events) === "paused")
      .map((s) => s.id)),
    [sessions, events])
  // A3 任务流 WS bump：只数图关心的事件（3s 轮询兜底，组件内去抖重拉）
  const flowBump = useMemo(
    () => events.filter((e) =>
      e.kind.startsWith("task.") || e.kind === "message.inbox" || e.kind.startsWith("session.")).length,
    [events])

  // 终止入口之一：会话控制组的「结束会话」（另一处是任务看板删除 claimed 任务）
  const closeSession = async (sid: string) => {
    try {
      await api.closeSession(sid)
      setDetached((prev) => {
        const next = new Set(prev)
        next.delete(sid)
        return next
      })
      if (activeTab === sid) setActiveTab("__all")
      refreshSessions()
      setJobInfo("会话已结束")
    } catch (e) {
      const hint = e instanceof ApiError && e.status === 409
        ? "会话任务执行中，请先暂停或中断任务再结束会话"
        : String(e)
      setJobInfo(`结束会话失败：${hint}`)
    }
  }

  // 会话控制（DESIGN.md §3）：暂停/恢复/中断，状态由事件流感知
  const controlSession = async (action: "pause" | "resume" | "abort", sid: string) => {
    const fn = action === "pause" ? api.pauseSession
      : action === "resume" ? api.resumeSession : api.abortSession
    const label = action === "pause" ? "暂停" : action === "resume" ? "恢复" : "中断"
    try {
      await fn(sid)
      setJobInfo(`${label}请求已发送（当前步做完后生效）`)
      refreshSessions()
    } catch (e) {
      setJobInfo(`${label}失败：${e}`)
    }
  }

  // 输入框（E8）：「发任务」= 以 human 名义发布 passive 任务；「引导会话」=
  // human_note 私信直达当前选中会话（步边界注入「💬 人类引导」，不打断当前工具调用）；
  // 「指挥编排」（C2）= 一次性目标指令直达编排器（最高优先注入 + 自动触发一轮编排）
  const sendRemark = async () => {
    const text = remark.trim()
    if (!text) return
    if (inputMode === "note") {
      if (!activeSession) return
      setRemark("")
      try {
        await api.sessionNote(activeSession.id, text)
      } catch (e) {
        setJobInfo(`引导投递失败：${e}`)
      }
      return
    }
    if (inputMode === "directive") {
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
    setRemark("")
    try {
      await api.publishTask(pid, { objective: text, task_type: "generic", noise_budget: "passive" })
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
  const activeStatus = activeSession ? sessionStatus(activeSession.id, events) : null
  // E8：步数预算用尽自动暂停的会话集（恢复/中断/收尾即移出）——控制组显「继续」
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
                <StatusDot status={sessionStatus(t.sessionId, events)} />
              )}
              {t.label}
              {t.sessionId && t.sessionId !== "__orch" && (unreadBySid.get(t.sessionId) ?? 0) > 0 && (
                <span
                  title="有系统私信（依据撤回需自评 / 发现增补通知）"
                  className="inline-flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-[--status-error] px-1 text-[9px] font-bold leading-none text-white"
                >
                  {(unreadBySid.get(t.sessionId) ?? 0) > 9 ? "9+" : unreadBySid.get(t.sessionId)}
                </span>
              )}
              {t.sessionId && t.sessionId !== "__orch" && (
                <span
                  role="button"
                  aria-label={`分离页签 ${t.label}（任务后台继续）`}
                  title="收起页签（不结束会话，任务继续在后台执行；可从右侧「已分离」挂回）"
                  className="-mr-1 rounded p-0.5 opacity-0 transition-opacity group-hover:opacity-100 hover:bg-accent text-muted-foreground hover:text-foreground"
                  onClick={(e) => {
                    e.stopPropagation()
                    detachSession(t.sessionId!)
                  }}
                >
                  <X className="size-3" />
                </span>
              )}
            </button>
          ))}
        </div>
        {detachedSessions.length > 0 && (
          <div className="relative mr-1 shrink-0">
            <button
              type="button"
              onClick={() => setDetachMenu((v) => !v)}
              title="已分离页签（会话仍在后台运行），点击挂回"
              className="rounded-md border border-dashed px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent"
            >
              ⊟ 已分离 {detachedSessions.length}
            </button>
            {detachMenu && (
              <div className="absolute right-0 top-7 z-30 w-52 rounded-lg border bg-popover p-1 shadow-md">
                {detachedSessions.map((s) => (
                  <button
                    key={s.id}
                    onClick={() => attachSession(s.id)}
                    className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-xs hover:bg-accent"
                  >
                    <StatusDot status={sessionStatus(s.id, events)} />
                    <span className="min-w-0 flex-1 truncate">{s.name || s.role}</span>
                    {(unreadBySid.get(s.id) ?? 0) > 0 && (
                      <span className="inline-flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-[--status-error] px-1 text-[9px] font-bold leading-none text-white">
                        {unreadBySid.get(s.id)}
                      </span>
                    )}
                    <span className="text-[10px] text-muted-foreground">挂回</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
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
        <span className={cn("ml-1 shrink-0 font-mono text-[10px]", connected ? "text-primary" : "text-[--status-error]")}>
          {connected ? "● live" : "○ 重连中"}
        </span>
      </div>
      {viewMode === "live" && (
      <>
      {/* 第二行：开窗三下拉常驻铺开 + 右侧会话控制/编排动作（窄屏兜底 wrap） */}
      <div className="flex min-h-10 flex-wrap items-center gap-1.5 border-b px-2 py-1.5">
        <select
          value={role}
          onChange={(e) => setRole(e.target.value)}
          title={roles.find((r) => r.role === role)?.description
            ? `${role === "_generalist" ? "通用" : role} · ${roles.find((r) => r.role === role)?.description}`
            : "开窗角色"}
          className="h-8 max-w-48 truncate rounded-md border bg-background px-2 text-xs"
        >
          {roles.map((r) => (
            <option key={r.role} value={r.role}>
              {r.role === "_generalist" ? "通用" : r.role}{r.description ? ` · ${r.description}` : ""}
            </option>
          ))}
        </select>
        <select
          value={provider}
          onChange={(e) => { setProvider(e.target.value); setModel("") }}
          title="开窗供应商（缺省=全局默认供应商）"
          className="h-8 max-w-32 rounded-md border bg-background px-2 font-mono text-xs"
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
          className="h-8 max-w-44 rounded-md border bg-background px-2 font-mono text-xs"
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
          className="h-8 w-20 shrink-0 rounded-md border bg-background px-2 font-mono text-xs placeholder:text-muted-foreground"
        />
        <Button size="sm" variant="outline" onClick={spawn} disabled={spawning || !roles.length}>开窗</Button>
        {/* 自主级别 / 暂停 / L2 链状态 / 用量预算（DESIGN §6.8，批 2-5） */}
        {usage && (
          <>
            <div className="flex h-8 overflow-hidden rounded-md border text-xs"
                 title="项目自主级别（§6.8：L0 全手动 / L1 任务自动·开窗审批 / L2 全自动链）；安全层任何档位不放松">
              {(["L0", "L1", "L2"] as const).map((lv) => (
                <button
                  key={lv}
                  title={LEVEL_HINTS[lv]}
                  disabled={savingAuto}
                  onClick={() => saveAutonomy({ level: lv })}
                  className={cn(
                    "px-2 transition-colors disabled:opacity-50",
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
                "h-8 rounded-md border px-2 text-xs disabled:opacity-50",
                usage.paused
                  ? "border-[--status-paused] text-[--status-paused]"
                  : "text-muted-foreground hover:bg-accent",
              )}
            >
              {usage.paused ? "⏸ 自动已暂停" : "⏸ 暂停自动"}
            </button>
            {/* 批 5 §6.8：L2 自动链状态（5s usage 轮询刷新） */}
            {usage.chain?.estranged ? (
              <span
                title="链因服务重启已急停：状态保留但不会自动恢复，点「编排一轮」手动恢复（轮数预算清零重算）"
                className="flex h-8 animate-pulse items-center rounded-md border border-amber-400 px-2 font-mono text-xs text-amber-400"
              >
                ⛓ 急停
              </span>
            ) : usage.chain?.active ? (
              <span
                title={`L2 自动链进行中：链内已自动 ${usage.chain.ticks}/${usage.max_chain_ticks} 轮（本项目累计 ${usage.chain.auto_ticks_total} 轮自动 tick）`}
                className="flex h-8 items-center rounded-md border border-primary px-2 font-mono text-xs text-primary"
              >
                ⛓ {usage.chain.ticks}/{usage.max_chain_ticks}
              </span>
            ) : null}
            <div className="relative">
              <button
                type="button"
                onClick={() => setBudgetOpen((v) => !v)}
                title="会话上限 / Token / 任务预算（点击编辑）"
                className="flex h-8 items-center gap-2 rounded-md border px-2 font-mono text-xs hover:bg-accent"
              >
                <span className={usage.tokens.budget
                  ? (usage.tokens.pct ?? 0) >= 1
                    ? "text-[--status-error]"
                    : (usage.tokens.pct ?? 0) >= 0.8 ? "text-amber-400" : "text-muted-foreground"
                  : "text-muted-foreground"}>
                  ∑ {fmtTokens(usage.tokens.used)}
                  {usage.tokens.budget
                    ? `/${fmtTokens(usage.tokens.budget)} ${Math.round((usage.tokens.pct ?? 0) * 100)}%`
                    : ""}
                </span>
                <span className={usage.tasks.budget
                  ? (usage.tasks.pct ?? 0) >= 1
                    ? "text-[--status-error]"
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
          </>
        )}
        <span className="ml-auto" />
        {activeSession && (
          activeStatus === "paused" ? (
            <>
              <Button
                size="sm" variant="outline"
                title={budgetPausedSids.has(activeSession.id)
                  ? "步数预算用尽自动暂停：从快照+步数断点恢复（默认自动 +200 步，落审计）；恢复后可用输入框「引导会话」补充指示"
                  : "从快照恢复被暂停的任务"}
                onClick={() => controlSession("resume", activeSession.id)}
              >
                {budgetPausedSids.has(activeSession.id) ? "▶ 继续" : "▶ 恢复"}
              </Button>
              <Button size="sm" variant="outline"
                      className="text-[--status-error] hover:text-[--status-error]"
                      onClick={() => setAbortTarget(activeSession.id)}>
                ⛔ 中断
              </Button>
            </>
          ) : (
            <>
              <Button size="sm" variant="outline" onClick={() => runWork(activeSession.id)}>
                跑任务队列
              </Button>
              <Button size="sm" variant="outline" onClick={() => controlSession("pause", activeSession.id)}>
                ⏸ 暂停
              </Button>
              <Button size="sm" variant="outline"
                      className="text-[--status-error] hover:text-[--status-error]"
                      onClick={() => setAbortTarget(activeSession.id)}>
                ⛔ 中断
              </Button>
            </>
          )
        )}
        {activeSession && (
          <Button
            size="sm" variant="outline"
            title="真正结束会话（关页签 × 只是收起，任务继续）；执行中需先暂停或中断"
            onClick={() => {
              if (window.confirm(`结束会话「${activeSession.name || activeSession.role}」？后台任务必须先暂停或中断。`))
                void closeSession(activeSession.id)
            }}
          >
            结束会话
          </Button>
        )}
        {activeSession && (
          <div className="flex items-center gap-1" title="动态切换当前会话的供应商/模型，下一次 LLM 调用生效">
            <span className="text-[10px] text-muted-foreground">🔀</span>
            <select
              value={switchProvider}
              onChange={(e) => setSwitchProvider(e.target.value)}
              className="h-8 max-w-28 rounded-md border bg-background px-1.5 font-mono text-xs"
            >
              {enabledProviders.map((p) => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
            <select
              value={switchModel}
              onChange={(e) => setSwitchModel(e.target.value)}
              className="h-8 max-w-40 rounded-md border bg-background px-1.5 font-mono text-xs"
            >
              <option value="">默认模型</option>
              {(switchTarget?.models ?? []).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <Button size="sm" variant="outline" onClick={switchLlm}>切换</Button>
          </div>
        )}
        <div className="relative">
          <Button size="sm" onClick={() => setOrchOpen((v) => !v)}>编排一轮</Button>
          {orchOpen && (
            <div className="absolute right-0 top-9 z-10 w-64 rounded-lg border bg-popover p-3 text-xs shadow-md">
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
                    {r.role === "_generalist" ? "通用" : r.role}
                  </button>
                ))}
              </div>
              <Button size="sm" className="mt-2 w-full" onClick={() => orchTick([...orchRoles])}>
                开始编排（{orchRoles.size || "不限"}）
              </Button>
            </div>
          )}
        </div>
        <Button size="sm" variant="outline" disabled={replanning} onClick={replanPriorities}
                title="让 planner 重排待认领任务优先级（0-9，小者优先，只改 open）。手动随时可跑，不受自主档/30s 去抖/预算限制；tick 或重排在跑时返回 409">
          {replanning ? "重排中…" : "重排优先级"}
        </Button>
        <Button size="sm" variant="outline" disabled={reviewing} onClick={reviewProposals}
                title="planner LLM 复盘本项目任务，把文档错漏沉淀为变更提案（人类审批后才落盘）">
          {reviewing ? "复盘中…" : "复盘沉淀"}
        </Button>
        <div className="relative">
          <Button size="sm" variant="outline"
                  className={cn(usage?.mode === "redteam" && "text-[--status-error]")}
                  title="作战模式与 mission（§6.9）：pentest/redteam 切换（redteam 需 ROE 四要素）"
                  onClick={() => setModeOpen((o) => !o)}>
            🎯 {usage?.mode === "redteam" ? "红队" : usage?.mode === "pentest" ? "渗透" : (usage?.mode ?? "渗透")}
          </Button>
          {modeOpen && usage && (
            <ModePopover
              usage={usage}
              onClose={() => setModeOpen(false)}
              onSave={async (patch) => {
                await api.patchProjectConfig(pid, patch)
                void refreshUsage()
                setJobInfo("作战模式已更新（mode.changed 审计已落；在跑会话维持创建时固化语义）")
              }}
            />
          )}
        </div>
      </div>

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

      {/* 类型过滤 */}
      <div className="flex gap-1 border-b px-3 py-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={cn(
              "rounded px-2 py-0.5 text-xs",
              filter === f.key ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent",
            )}
          >
            {f.label}
          </button>
        ))}
        {jobInfo && <span className="ml-auto font-mono text-xs text-muted-foreground">{jobInfo}</span>}
      </div>

      {/* 事件流（虚拟滚动） */}
      <div
        ref={listRef}
        onScroll={(e) => {
          const el = e.currentTarget
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
        }}
        className="min-h-0 flex-1 overflow-auto px-3"
      >
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {virtualizer.getVirtualItems().map((vi) => {
            const e = visible[vi.index]
            const style = eventStyle(e.kind, e.payload)
            const open = overrides.get(e.id) ?? style.defaultOpen
            const summary = eventSummary(e.payload)
            const detail = JSON.stringify(e.payload, null, 2)
            // skill.routed 命中技能：双击跳设置页 Skill tab 选中该技能（deep link 经 goto-settings）
            const routedName = e.kind === "skill.routed" && typeof e.payload.name === "string"
              ? e.payload.name : null
            return (
              <div
                key={e.id}
                ref={virtualizer.measureElement}
                data-index={vi.index}
                className="absolute left-0 top-0 w-full py-0.5"
                style={{ transform: `translateY(${vi.start}px)` }}
              >
                <div
                  className="cursor-pointer rounded px-2 py-1 hover:bg-accent/40"
                  onClick={() => toggleRow(e.id, style.defaultOpen)}
                  onDoubleClick={routedName ? () => {
                    window.dispatchEvent(new CustomEvent("goto-settings", {
                      detail: {
                        tab: "skills",
                        skill: {
                          source: e.payload.kind === "track" ? "track" : "cap",
                          pack: String(e.payload.pack ?? ""),
                          name: routedName,
                        },
                      },
                    }))
                  } : undefined}
                  title={routedName ? `双击查看技能 ${routedName}` : undefined}
                >
                  <div className="flex items-baseline gap-2 text-xs">
                    <span className="font-mono text-[10px] text-muted-foreground" title={utcTitle(e.created_at)}>
                      {fmtTime(e.created_at)}
                    </span>
                    <span className={style.className}>{style.label}</span>
                    {summary && <span className="min-w-0 flex-1 truncate">{summary}</span>}
                    {e.kind === "orch.proposed" && (
                      <button
                        type="button"
                        title="以人类名义采纳：走与手动发任务/开窗相同的写口（created_by=human）"
                        onClick={(click) => { click.stopPropagation(); void adoptProposal(e) }}
                        disabled={adopted.has(e.id) || adoptingId !== null}
                        className={cn(
                          "shrink-0 rounded border px-1.5 py-px text-[11px] leading-tight",
                          adopted.has(e.id)
                            ? "border-muted-foreground/40 text-muted-foreground"
                            : "border-amber-400/60 text-amber-400 hover:bg-amber-400/10",
                        )}
                      >
                        {adopted.has(e.id) ? "✓ 已采纳" : adoptingId === e.id ? "采纳中…" : "采纳"}
                      </button>
                    )}
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {e.author}
                    </span>
                  </div>
                  {open && detail !== "{}" && (
                    <pre className="mt-1 max-h-48 overflow-auto rounded bg-card p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
                      {detail}
                    </pre>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* 插话输入（人类插手通道 §6.4）：发任务=human 名义 passive 任务；E8 引导会话=human_note 直达选中会话 */}
      <div className="flex items-center gap-2 border-t p-3">
        <div className="flex h-9 shrink-0 overflow-hidden rounded-md border text-xs">
          <button
            type="button"
            title="以 human 名义向黑板发布一个 passive 任务（插话）"
            onClick={() => setInputMode("task")}
            className={cn("px-2.5 transition-colors",
              inputMode === "task" ? "bg-primary/15 font-medium text-primary" : "text-muted-foreground hover:bg-accent")}
          >
            发任务
          </button>
          <button
            type="button"
            title={activeSession
              ? "向当前选中会话投递人类引导（步边界注入，不打断当前工作；暂停期投递恢复时随快照注入）"
              : "引导直达会话：先在上方选中一个会话页签"}
            onClick={() => setInputMode("note")}
            className={cn("border-l px-2.5 transition-colors",
              inputMode === "note" ? "bg-primary/15 font-medium text-primary" : "text-muted-foreground hover:bg-accent",
              !activeSession && "opacity-50")}
          >
            引导会话
          </button>
          <button
            type="button"
            title="指挥编排器（C2）：一次性目标指令——自动触发一轮编排，编排器按指令拆解任务/分资产/开窗（最高优先落实）"
            onClick={() => setInputMode("directive")}
            className={cn("border-l px-2.5 transition-colors",
              inputMode === "directive" ? "bg-primary/15 font-medium text-primary" : "text-muted-foreground hover:bg-accent")}
          >
            指挥编排
          </button>
        </div>
        <Input
          placeholder={inputMode === "directive"
            ? "指挥编排器：一句话目标（如「对已登记资产做漏洞挖掘」）——自动触发编排拆解/分资产/开窗…"
            : inputMode === "note"
            ? (activeSession
                ? `引导「${activeSession.name || activeSession.role}」：一句话指示，步边界注入不打断当前工作…`
                : "引导会话：先选中一个会话页签…")
            : "插话：向黑板发布一个任务或一条指示…"}
          value={remark}
          onChange={(e) => setRemark(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && sendRemark()}
        />
        <Button size="sm" onClick={sendRemark}
                disabled={!remark.trim() || (inputMode === "note" && !activeSession)}>
          {inputMode === "note" ? "引导" : "发送"}
        </Button>
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
              onAttachSession={attachFromFlow}
            />
          </Suspense>
        </div>
      )}

      {/* E12：中断确认（两处「⛔ 中断」都经此）——落盘快照保留，任务可续跑 */}
      <AlertDialog
        open={abortTarget !== null}
        onOpenChange={(open) => !open && setAbortTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>硬中断会话 {abortTarget ?? ""}？</AlertDialogTitle>
            <AlertDialogDescription>
              当前任务将标记为失败（人工中断，不回队列）。暂停/执行中已落盘的
              现场快照会保留——稍后可在任务看板该失败卡上「▶ 续跑」，
              原会话将从快照与步数断点恢复，上下文不丢失。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <Button variant="outline" size="sm">取消</Button>
            </AlertDialogCancel>
            <Button
              variant="destructive" size="sm"
              onClick={() => {
                const sid = abortTarget
                setAbortTarget(null)
                if (sid) void controlSession("abort", sid)
              }}
            >
              确认中断
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
