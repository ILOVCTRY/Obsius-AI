import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import {
  Activity, Check, ChevronDown, ChevronRight, Clock, GitBranch, Layers, Link2, Loader2, Network, Pause, Play,
  Plus, Power, Radar, RefreshCw, Send, Sparkles, Square, Target, WandSparkles, X,
} from "lucide-react"
import { api } from "@/lib/api"
import type {
  ArtexActivityEvent, ArtexCoverage, ArtexCoverageGraphNode, ArtexCoverageResponse,
  ArtexEngineStatus, ArtexExplorationGraph, ArtexGoal, ArtexIntent,
  ArtexTaskGraph, ArtexTaskRuntime, ArtexTaskSummary, ProjectDetail,
} from "@/lib/types"
import { cn } from "@/lib/utils"

// 探索图画布懒加载（artex-exploration-canvas，2026-10-20）：@xyflow/react 约
// 192KB，只在人类真的切到「探索图 → 画布」时才拉，不进主包。与 AttackPath 同约定。
const ExplorationCanvas = lazy(() => import("./artex/ExplorationCanvas")
  .then(m => ({ default: m.ExplorationCanvas })))

// ARTEX 工作台（artex-workbench-ui，2026-10-19）。
//
// 旧版是「一张输入卡 + 一行灰字」的单列平铺：没有任务导航、没有指标、没有分页，
// 空状态下整页 80% 面积是空的（用户截图反馈「太简陋」）。现改为定高工作台：
//
//   顶部指挥栏  —— 身份/轨道/项目计数 + Engine 状态灯 + 启动/刷新
//   目标输入条  —— 唯一主入口（flex-1），生成任务树
//   左侧任务栏  —— 项目级任务列表，常驻可滚动，点击切换
//   右侧主区    —— KPI 指标 + 执行中 Worker 条 + 「任务图/探索图/活动流」分页
//
// 三区都吃满高度、各自内部滚动，避免长列表把整页顶开；空状态改成引导卡，
// 示例目标可一键填入。所有数据接口与旧版一致，未改后端。

const STATUS_META: Record<string, { label: string; dot: string; pill: string; bar: string }> = {
  pending: { label: "待执行", dot: "bg-slate-400", pill: "border-slate-500/40 bg-slate-500/10 text-slate-300", bar: "border-l-slate-500/50" },
  claimed: { label: "已领取", dot: "bg-cyan-400", pill: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300", bar: "border-l-cyan-500/60" },
  running: { label: "执行中", dot: "bg-amber-400", pill: "border-amber-500/40 bg-amber-500/10 text-amber-300", bar: "border-l-amber-500/70" },
  paused: { label: "已暂停", dot: "bg-violet-400", pill: "border-violet-500/40 bg-violet-500/10 text-violet-300", bar: "border-l-violet-500/60" },
  blocked: { label: "已阻塞", dot: "bg-orange-400", pill: "border-orange-500/40 bg-orange-500/10 text-orange-300", bar: "border-l-orange-500/60" },
  // 撞运行预算被动收尾（artex-exhausted）：试过但没做完，既不是完成也不是失败。
  exhausted: { label: "预算耗尽", dot: "bg-yellow-400", pill: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300", bar: "border-l-yellow-500/60" },
  completed: { label: "已完成", dot: "bg-emerald-400", pill: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300", bar: "border-l-emerald-500/60" },
  failed: { label: "失败", dot: "bg-red-400", pill: "border-red-500/40 bg-red-500/10 text-red-300", bar: "border-l-red-500/60" },
  cancelled: { label: "已取消", dot: "bg-zinc-400", pill: "border-zinc-500/40 bg-zinc-500/10 text-zinc-400", bar: "border-l-zinc-500/40" },
}
const statusOf = (status: string) => STATUS_META[status] || STATUS_META.pending

const RUNTIME_META: Record<string, { label: string; pill: string }> = {
  exploring: { label: "探索中", pill: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300" },
  draining: { label: "收敛中", pill: "border-amber-500/40 bg-amber-500/10 text-amber-300" },
  goal_reached: { label: "目标已达成", pill: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  needs_review: { label: "需要复核", pill: "border-red-500/40 bg-red-500/10 text-red-300" },
  cancelled: { label: "已取消", pill: "border-zinc-500/40 bg-zinc-500/10 text-zinc-400" },
}
const runtimeOf = (status: string) =>
  RUNTIME_META[status] || { label: status || "未知", pill: "border-(--border) text-(--text-3)" }

const EXPLORATION_META: Record<string, { label: string; pill: string }> = {
  goal: { label: "Goal", pill: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300" },
  intent: { label: "Intent", pill: "border-violet-500/40 bg-violet-500/10 text-violet-300" },
  fact: { label: "Fact", pill: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  finding: { label: "Finding", pill: "border-red-500/40 bg-red-500/10 text-red-300" },
  hint: { label: "Hint", pill: "border-amber-500/40 bg-amber-500/10 text-amber-300" },
}

/** 事实的置信度等级（artex-record-fact）：缺 level 时按分值反推。
 *  兜底汇总事实的 1.0 表示「整轮跑完了」而非「直接看到」，反推为 inferred
 *  会误导（它确实写的是 observed）——故按与后端一致的分值规则处理。 */
const confidenceLevel = (node: { confidence?: number; metadata?: Record<string, unknown> }) => {
  const raw = String((node.metadata || {}).confidence_level || "")
  if (raw === "observed" || raw === "inferred") return raw
  return (node.confidence || 0) >= 1 ? "observed" : "inferred"
}

// 空状态引导：按轨道给几条可直接用的目标，避免用户面对空输入框不知道写什么。
const GOAL_SAMPLES: Record<string, string[]> = {
  pentest: [
    "枚举 example.com 的子域与开放端口，验证暴露面",
    "盘点外网 Web 入口，覆盖认证、会话与越权测试面",
    "复核已登记资产里尚未覆盖的测试维度并补测",
  ],
  redteam: [
    "从外网入口推进到内网边界，梳理可达攻击路径",
    "评估域内凭据暴露面与横向移动可行性",
    "验证钓鱼落地页到凭据获取的完整链路",
  ],
  ctf: [
    "分析题目附件，判定类型并定位 flag 获取路径",
    "从流量样本中提取关键证据并复现利用",
    "对给定二进制做静态与动态交叉分析",
  ],
}

type Tab = "graph" | "goals" | "coverage" | "exploration" | "activity"

export function ArtexWorkbenchView({ pid, meta }: { pid: string; meta: ProjectDetail | null }) {
  const [taskId, setTaskId] = useState("")
  const [goal, setGoal] = useState("")
  const [graph, setGraph] = useState<ArtexTaskGraph | null>(null)
  const [exploration, setExploration] = useState<ArtexExplorationGraph | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [engine, setEngine] = useState<ArtexEngineStatus | null>(null)
  const [runtime, setRuntime] = useState<ArtexTaskRuntime | null>(null)
  const [activity, setActivity] = useState<ArtexActivityEvent[]>([])
  const [tasks, setTasks] = useState<ArtexTaskSummary[]>([])
  const [coverage, setCoverage] = useState<ArtexCoverageResponse | null>(null)
  const [coverageGraph, setCoverageGraph] = useState<{ task_id: string; nodes: ArtexCoverageGraphNode[]; edges: { src: string; dst: string }[] } | null>(null)
  const [tab, setTab] = useState<Tab>("graph")
  // 探索图两种形态（artex-exploration-canvas）：画布看结构，列表找具体结论。
  // 画布是默认——「关联与时间关系」正是卡片墙看不出来的东西。
  const [explorationView, setExplorationView] = useState<"canvas" | "list">("canvas")
  const activityCursor = useRef(0)
  const streamConnected = useRef(false)

  const load = useCallback(async (id = taskId) => {
    try {
      const taskResponse = await api.artexTaskList(pid)
      setTasks(taskResponse.tasks)
      if (!id.trim()) {
        setGraph(null); setExploration(null); setRuntime(null); setEngine(null); setActivity([])
        setCoverage(null); setCoverageGraph(null)
        setError(null)
        return
      }
      const [taskGraph, explorationGraph, coverageResponse] = await Promise.all([
        api.artexTaskGraph(pid, id.trim()),
        api.artexExploration(pid, id.trim()),
        api.artexTaskCoverage(pid, id.trim()).catch(() => null),
      ])
      setGraph(taskGraph)
      setExploration(explorationGraph)
      setCoverage(coverageResponse)
      setRuntime(await api.artexTaskRuntime(pid, id.trim()))
      setEngine(await api.artexEngineStatus(pid, id.trim()))
      const activityResponse = await api.artexActivity(pid, id.trim(), activityCursor.current)
      if (activityCursor.current === 0) setActivity(activityResponse.events.slice(-80).reverse())
      else if (activityResponse.events.length > 0) setActivity(current => [...activityResponse.events].reverse().concat(current).slice(0, 80))
      activityCursor.current = Math.max(activityCursor.current, activityResponse.latest_id)
      setError(null)
    }
    catch (e) { setError(e instanceof Error ? e.message : "任务图加载失败") }
  }, [pid, taskId])

  // 覆盖图只在实际查看「覆盖」页签时按需加载（独立接口，避免每次轮询都拉全图）。
  // 缓存带上 task_id，切任务时靠 key 不匹配自然失效，无需在 effect 里重置 state。
  const openCoverageTab = useCallback(async () => {
    setTab("coverage")
    const id = taskId.trim()
    if (!id || coverageGraph?.task_id === id) return
    try { setCoverageGraph(await api.artexTaskCoverageGraph(pid, id)) }
    catch { /* 覆盖图失败不影响覆盖度数字展示 */ }
  }, [pid, taskId, coverageGraph])

  // 锚定/摘除后刷新覆盖度数字与覆盖图（覆盖图会因范围变化而变化）。
  const refreshCoverage = useCallback(async () => {
    if (!taskId.trim()) return
    try {
      const [response, g] = await Promise.all([
        api.artexTaskCoverage(pid, taskId.trim()),
        api.artexTaskCoverageGraph(pid, taskId.trim()).catch(() => null),
      ])
      setCoverage(response)
      if (g) setCoverageGraph(g)
      void load(taskId)
    } catch { /* 忽略，保持旧数据 */ }
  }, [pid, taskId, load])

  // 首次进入：自动选中最近更新的任务，避免「明明有任务却显示空页面」。
  useEffect(() => {
    let cancelled = false
    void api.artexTaskList(pid).then(response => {
      if (cancelled) return
      setTasks(response.tasks)
      setTaskId(current => current || response.tasks[0]?.task_id || "")
    }).catch(() => { /* 列表失败交给 load 的 error 通道 */ })
    return () => { cancelled = true }
  }, [pid])

  const plan = async () => {
    if (!goal.trim()) return
    setBusy(true); setError(null)
    try {
      const result = await api.artexPlan(pid, { goal: goal.trim(), task_id: taskId.trim() || undefined, start_engine: false })
      setTaskId(result.root.task_id || taskId)
      const resolvedTaskId = result.root.task_id || taskId
      const [taskGraph, explorationGraph] = await Promise.all([
        api.artexTaskGraph(pid, resolvedTaskId),
        api.artexExploration(pid, resolvedTaskId),
      ])
      setGraph(taskGraph)
      setExploration(explorationGraph)
      setRuntime(await api.artexTaskRuntime(pid, resolvedTaskId))
      setCoverage(await api.artexTaskCoverage(pid, resolvedTaskId).catch(() => null))
      setCoverageGraph(null)
      setGoal("")
      setTab("graph")
    } catch (e) { setError(e instanceof Error ? e.message : "Planner 执行失败") }
    finally { setBusy(false) }
  }

  useEffect(() => {
    activityCursor.current = 0
    streamConnected.current = false
    if (taskId) void load(taskId)
  }, [taskId, load])
  useEffect(() => {
    if (!taskId) return
    const stream = new EventSource(api.artexActivityStreamUrl(pid, taskId, activityCursor.current))
    stream.onopen = () => { streamConnected.current = true }
    stream.onmessage = event => {
      try {
        const item = JSON.parse(event.data) as ArtexActivityEvent
        activityCursor.current = Math.max(activityCursor.current, Number(event.lastEventId || item.id || 0))
        setActivity(current => [item, ...current.filter(existing => existing.id !== item.id)].slice(0, 80))
        void load(taskId)
      } catch { /* 忽略损坏事件，等待轮询校正 */ }
    }
    stream.onerror = () => { streamConnected.current = false; stream.close() }
    const timer = window.setInterval(() => { if (!streamConnected.current) void load(taskId) }, 3000)
    return () => { stream.close(); window.clearInterval(timer) }
  }, [pid, taskId, load])

  const root = useMemo(() => graph?.nodes.find(n => !n.parent_id), [graph])
  const byParent = useMemo(() => {
    const map = new Map<string, ArtexIntent[]>()
    for (const node of graph?.nodes || []) {
      if (!node.parent_id) continue
      const list = map.get(node.parent_id) || []
      list.push(node)
      map.set(node.parent_id, list)
    }
    for (const list of map.values()) list.sort((a, b) => b.priority - a.priority)
    return map
  }, [graph])
  const byId = useMemo(() => {
    const map = new Map<string, ArtexIntent>()
    for (const node of graph?.nodes || []) map.set(node.id, node)
    return map
  }, [graph])

  // 指标口径：执行中合并 claimed + running（对用户是同一件事：Worker 在跑）。
  const kpis = useMemo(() => {
    const counts = engine?.counts || {}
    const total = graph?.nodes.length || 0
    const done = counts.completed || 0
    return {
      total,
      pending: counts.pending || 0,
      active: (counts.claimed || 0) + (counts.running || 0),
      done,
      // exhausted **必须算进失败**（artex-kpi-scope，2026-10-21）：它是「试过但
      // 没做完」，对用户就是「这条方向没拿到结果」。此前只算 failed+blocked，
      // 实测 5 条 Intent 全是未成功（3 exhausted + 2 failed），界面却显示
      // 「失败/阻塞 2」——把 3 条耗尽静默藏掉了，人以为还有 3 条在正常跑。
      // `STATUS_META` 里本来就定义了 exhausted，这里只是没接上。
      failed: (counts.failed || 0) + (counts.blocked || 0) + (counts.exhausted || 0),
      exhausted: counts.exhausted || 0,
      paused: counts.paused || 0,
      percent: total > 0 ? Math.round((done / total) * 100) : 0,
    }
  }, [engine, graph])

  const facts = useMemo(() => exploration?.nodes.filter(n => n.kind === "fact").length || 0, [exploration])
  const findings = useMemo(() => exploration?.nodes.filter(n => n.kind === "finding").length || 0, [exploration])

  // 目标态是收敛的唯一依据：先于 Intent 计数展示，避免「Intent 跑完=任务完成」的错觉。
  const goals = useMemo(() => graph?.goals || [], [graph])
  const goalStats = useMemo(() => ({
    total: goals.length,
    met: goals.filter(g => String(g.status) === "met").length,
    open: goals.filter(g => String(g.status) !== "met").length,
  }), [goals])

  // 覆盖度：任务范围资产（分母）里有多少被 Fact 锚定过（分子）。上游口径是「粗略参考」，
  // 分母 0 时 pct 为 null —— 这里如实显示「—」而不是伪造 0%。
  const cov = coverage?.coverage
  const covStats = useMemo(() => {
    if (!cov || !cov.enabled) return { on: false, denominator: 0, tested: 0, untested: 0, percent: null as number | null }
    const denominator = cov.denominator || 0
    const tested = cov.tested || 0
    return {
      on: true,
      denominator,
      tested,
      untested: cov.untested ?? Math.max(0, denominator - tested),
      percent: cov.pct === null || cov.pct === undefined ? null : Math.round(cov.pct * 100),
    }
  }, [cov])

  const toggleEngine = async () => {
    setBusy(true); setError(null)
    try {
      if (engine?.running) await api.artexEngineStop(pid)
      else await api.artexEngineStart(pid)
      setEngine(await api.artexEngineStatus(pid, taskId || undefined))
    } catch (e) { setError(e instanceof Error ? e.message : "Engine 控制失败") }
    finally { setBusy(false) }
  }

  const steerWorker = async (intent: ArtexIntent, message: string) => {
    if (!message.trim()) return
    setBusy(true); setError(null)
    try { await api.artexWorkerControl(pid, intent.id, "steer", message.trim()); await load() }
    catch (e) { setError(e instanceof Error ? e.message : "Worker 方向调整失败") }
    finally { setBusy(false) }
  }

  const killWorker = async (intent: ArtexIntent) => {
    if (!window.confirm(`终止 Worker？\n\n${intent.objective}`)) return
    setBusy(true); setError(null)
    try { await api.artexWorkerControl(pid, intent.id, "kill", "人工终止 Worker"); await load() }
    catch (e) { setError(e instanceof Error ? e.message : "Worker 终止失败") }
    finally { setBusy(false) }
  }

  const control = async (intent: ArtexIntent, action: "pause" | "resume" | "cancel" | "retry") => {
    setBusy(true); setError(null)
    try { await api.artexIntentControl(pid, intent.id, action); await load() }
    catch (e) { setError(e instanceof Error ? e.message : "任务控制失败") }
    finally { setBusy(false) }
  }

  const samples = GOAL_SAMPLES[meta?.track || "pentest"] || GOAL_SAMPLES.pentest

  return <div className="flex h-full min-h-0 flex-col overflow-hidden bg-(--surface-0) text-sm text-(--text-1)">
    {/* 顶部指挥栏 */}
    <header className="shrink-0 border-b border-(--border) bg-(--surface-1) px-5 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-cyan-500/30 bg-cyan-500/10 text-cyan-300">
          <WandSparkles className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-sm font-semibold">ARTEX 主控</h1>
            <span className="rounded-full border border-cyan-500/30 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-cyan-300">
              {meta?.track || "pentest"}
            </span>
          </div>
          <p className="truncate text-[11px] text-(--text-3)">Planner 生成 Intent · Engine 调度 Frontier · Worker 并发执行</p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {meta && <div className="mr-1 hidden items-center gap-3 text-[11px] text-(--text-3) lg:flex">
            <span>资产 <b className="font-semibold text-(--text-2)">{meta.assets}</b></span>
            <span>发现 <b className="font-semibold text-(--text-2)">{meta.findings}</b></span>
          </div>}
          {runtime && <span className={cn("rounded-full border px-2 py-1 text-[11px]", runtimeOf(runtime.status).pill)}>{runtimeOf(runtime.status).label}</span>}
          <span className={cn("inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[11px]",
            engine?.running ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" : "border-(--border) bg-(--surface-2) text-(--text-3)")}>
            <i className={cn("h-1.5 w-1.5 rounded-full", engine?.running ? "animate-pulse bg-emerald-400" : "bg-zinc-500")} />
            {engine?.running ? `Engine · ${engine.active_workers} Worker` : "Engine 未运行"}
          </span>
          <button className="wb-btn-secondary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs" onClick={() => void toggleEngine()} disabled={busy}>
            <Power className="h-3.5 w-3.5" />{engine?.running ? "停止" : "启动"}
          </button>
          <button className="wb-btn-secondary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs" onClick={() => void load()} disabled={!taskId || busy}>
            <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />刷新
          </button>
        </div>
      </div>
    </header>

    {/* 目标输入条：唯一主入口 */}
    <div className="shrink-0 border-b border-(--border) px-5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Target className="hidden h-4 w-4 shrink-0 text-(--text-3) sm:block" />
        <input
          className="h-9 min-w-[220px] flex-1 rounded-lg border border-(--border) bg-(--surface-1) px-3 text-sm outline-none transition-colors placeholder:text-(--text-3) focus:border-cyan-500/60"
          value={goal} onChange={e => setGoal(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") void plan() }}
          placeholder={`描述 ${meta?.track || "当前轨道"} 目标，例如：枚举并验证外网入口`} />
        <input
          className="h-9 w-40 rounded-lg border border-(--border) bg-(--surface-1) px-3 font-mono text-xs outline-none transition-colors placeholder:text-(--text-3) focus:border-cyan-500/60"
          value={taskId} onChange={e => setTaskId(e.target.value)} placeholder="任务 ID（可选）" />
        <button className="wb-btn-primary inline-flex h-9 items-center gap-1.5 px-4 text-xs" onClick={() => void plan()} disabled={!goal.trim() || busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}生成任务树
        </button>
      </div>
      {error && <div className="mt-2 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
        <X className="mt-0.5 h-3.5 w-3.5 shrink-0 cursor-pointer opacity-70 hover:opacity-100" onClick={() => setError(null)} />
        <span className="min-w-0 flex-1">{error}</span>
      </div>}
    </div>

    {/* 主体：左任务栏 + 右主区 */}
    <div className="flex min-h-0 flex-1 gap-4 px-5 py-4">
      <aside className="flex w-[240px] shrink-0 flex-col gap-2">
        <div className="flex items-center justify-between px-1">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">项目任务</span>
          <span className="font-mono text-[10px] text-(--text-3)">{tasks.length}</span>
        </div>
        <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-0.5">
          {tasks.length === 0
            ? <div className="rounded-lg border border-dashed border-(--border) px-3 py-6 text-center text-[11px] leading-relaxed text-(--text-3)">
              还没有任务<br />在上方输入目标生成第一棵任务树
            </div>
            : tasks.map(task => <TaskItem key={task.task_id} task={task} active={task.task_id === taskId} onClick={() => setTaskId(task.task_id)} />)}
        </div>
        <button className="wb-btn-secondary inline-flex items-center justify-center gap-1.5 py-1.5 text-[11px]" onClick={() => setTaskId("")}>
          <Plus className="h-3.5 w-3.5" />新建任务
        </button>
      </aside>

      <main className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
        {!graph
          ? <EmptyState samples={samples} onPick={setGoal} />
          : <>
            <div className="grid shrink-0 grid-cols-3 gap-2 sm:grid-cols-4 xl:grid-cols-7">
              <Kpi label="目标" value={goalStats.total > 0 ? `${goalStats.met}/${goalStats.total}` : "—"}
                tone={goalStats.total > 0 && goalStats.open === 0 ? "text-emerald-300" : "text-cyan-300"}
                hint={goalStats.total > 0 ? `未达成 ${goalStats.open}` : "未登记"} />
              <Kpi label="资产覆盖" value={covStats.on && covStats.denominator > 0 ? `${covStats.tested}/${covStats.denominator}` : "—"}
                tone={covStats.percent !== null && covStats.percent >= 100 ? "text-emerald-300" : "text-violet-300"}
                hint={!covStats.on ? "覆盖度关闭"
                  : covStats.denominator === 0 ? "范围未锚定"
                    : `${covStats.percent ?? 0}% · 未覆盖 ${covStats.untested}`} />
              <Kpi label="总 Intent" value={kpis.total} hint={`完成 ${kpis.percent}%`} />
              <Kpi label="待执行" value={kpis.pending} tone="text-slate-300" />
              <Kpi label="执行中" value={kpis.active} tone="text-amber-300" pulse={kpis.active > 0} />
              <Kpi label="未成功" value={kpis.failed} tone={kpis.failed > 0 ? "text-red-300" : undefined}
                hint={kpis.exhausted > 0 ? `含耗尽 ${kpis.exhausted}` : undefined} />
              <Kpi label="Fact / Finding" value={`${facts} / ${findings}`} tone="text-cyan-300" />
            </div>

            {engine?.running && engine.active.length > 0 && <div className="shrink-0 rounded-lg border border-amber-500/25 bg-amber-500/[0.06] px-3 py-2">
              <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-amber-300/80">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />正在执行 {engine.active.length} 个 Worker
              </div>
              <div className="flex flex-wrap gap-1.5">
                {engine.active.slice(0, 6).map(intent => <span key={intent.id} className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-(--border) bg-(--surface-1) px-2 py-1 text-[11px]">
                  <span className="font-mono text-[10px] text-amber-300">{intent.expert || "auto"}</span>
                  <span className="truncate text-(--text-2)">{intent.objective}</span>
                </span>)}
              </div>
            </div>}

            {/* 任务级预算到点（artex-taskclock）：不是「方向失败」，是整场探索到点了。
                与「预算耗尽」徽章分开说——前者是任务级、后者是单条 Intent 级，
                处置方式也不同（前者只能等人工/复盘，后者可续跑）。 */}
            {runtime?.metadata?.limit_reason && <div className="shrink-0 rounded-lg border border-orange-500/30 bg-orange-500/[0.07] px-3 py-2">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-orange-300/90">
                <Clock className="h-3 w-3" />任务级预算到点
              </div>
              <p className="text-[11px] leading-relaxed text-orange-200/90">
                {runtime.metadata.limit_reason}——Engine 已暂停剩余待执行方向，并跑了一轮
                <b className="font-semibold">终局目标判定</b>（只判定已证成的目标，不再派新方向）。
                要接着打，调大任务预算后恢复暂停的 Intent，或新建任务承接。
              </p>
            </div>}

            <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-(--border) bg-(--surface-1)">
              <div className="flex shrink-0 items-center gap-1 border-b border-(--border) px-2 py-1.5">
                <TabButton icon={GitBranch} label="任务图" count={graph.nodes.length} active={tab === "graph"} onClick={() => setTab("graph")} />
                <TabButton icon={Target} label="目标" count={goalStats.total} active={tab === "goals"} onClick={() => setTab("goals")} />
                <TabButton icon={Radar} label="覆盖" count={covStats.denominator} active={tab === "coverage"} onClick={() => void openCoverageTab()} />
                <TabButton icon={Layers} label="探索图" count={exploration?.nodes.length || 0} active={tab === "exploration"} onClick={() => setTab("exploration")} />
                <TabButton icon={Activity} label="活动流" count={activity.length} active={tab === "activity"} onClick={() => setTab("activity")} />
                <div className="ml-auto flex items-center gap-2 pr-1 text-[10px] text-(--text-3)">
                  <span className="font-mono">{graph.task_id}</span>
                  {root && <span className={cn("rounded-full border px-1.5 py-0.5", statusOf(root.status).pill)}>{statusOf(root.status).label}</span>}
                </div>
              </div>
              {/* 探索图画布要定高（xyflow 需要真实像素高度），其余分页仍是滚动列表。
                  p-3 在画布模式下挪进内层，否则画布四周多出 12px 造成「贴边不齐」。 */}
              <div className={cn("min-h-0 flex-1",
                tab === "exploration" && explorationView === "canvas"
                  ? "overflow-hidden" : "overflow-y-auto p-3")}>
                {tab === "graph" && (root
                  ? <IntentTree node={root} byParent={byParent} byId={byId} onControl={control} onSteer={steerWorker} onKill={killWorker} busy={busy} depth={0} />
                  : <div className="py-10 text-center text-xs text-(--text-3)">任务图没有根 Intent。</div>)}
                {tab === "goals" && <GoalPanel goals={goals} runtimeStatus={runtime?.status || ""} />}
                {tab === "coverage" && <CoveragePanel coverage={coverage} graph={coverageGraph} pid={pid}
                  taskId={graph.task_id || taskId} onChanged={refreshCoverage} />}
                {tab === "exploration" && <div className="flex h-full min-h-0 flex-col">
                  <div className="flex shrink-0 items-center gap-1 border-b border-(--border) px-2 py-1">
                    <SubTabButton icon={Network} label="画布" active={explorationView === "canvas"}
                      onClick={() => setExplorationView("canvas")} />
                    <SubTabButton icon={Layers} label="列表" active={explorationView === "list"}
                      onClick={() => setExplorationView("list")} />
                    <span className="ml-auto pr-1 text-[10px] text-(--text-3)">
                      {exploration ? `${exploration.nodes.length} 节点 · ${exploration.edges.length} 边` : ""}
                    </span>
                  </div>
                  <div className={cn("min-h-0 flex-1", explorationView === "canvas" ? "p-2" : "overflow-y-auto p-3")}>
                    {explorationView === "canvas"
                      ? <Suspense fallback={<div className="grid h-full place-items-center text-xs text-(--text-3)">
                        <span className="inline-flex items-center gap-1.5"><Loader2 className="h-3.5 w-3.5 animate-spin" />画布加载中…</span>
                      </div>}>
                        {exploration ? <ExplorationCanvas graph={exploration} />
                          : <div className="grid h-full place-items-center text-xs text-(--text-3)">探索图加载中…</div>}
                      </Suspense>
                      : <ExplorationPanel graph={exploration} />}
                  </div>
                </div>}
                {tab === "activity" && <ActivityTimeline events={activity} />}
              </div>
            </section>
          </>}
      </main>
    </div>
  </div>
}

// ---------- 左栏 ----------

function TaskItem({ task, active, onClick }: { task: ArtexTaskSummary; active: boolean; onClick: () => void }) {
  // 左侧卡片优先显示目标态：任务是否完成由目标决定，不是 Intent 计数。
  const meta = runtimeOf(task.runtime_status || "") 
  const hasRuntime = Boolean(task.runtime_status)
  const goalTotal = task.goal_count || 0
  const goalMet = task.goal_met_count || 0
  const percent = goalTotal > 0
    ? Math.round((goalMet / goalTotal) * 100)
    : task.intent_count > 0 ? Math.round(((task.counts.completed || 0) / task.intent_count) * 100) : 0
  // 覆盖度作为次级指标挂在卡片底部：范围锚定过才显示，避免老任务显示无意义的 0/0。
  const cov = task.coverage
  const covDenom = cov?.enabled ? cov.denominator || 0 : 0
  const covPercent = cov?.enabled && covDenom > 0 && cov.pct !== null && cov.pct !== undefined
    ? Math.round(cov.pct * 100) : null
  return <button
    className={cn("w-full rounded-lg border px-2.5 py-2 text-left transition-colors",
      active ? "border-cyan-500/50 bg-cyan-500/10" : "border-(--border) bg-(--surface-1) hover:border-(--text-3)")}
    onClick={onClick}>
    <div className="flex items-start gap-2">
      <i className={cn("mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full",
        !hasRuntime ? "bg-slate-400" : task.running ? "animate-pulse bg-amber-400"
          : task.runtime_status === "goal_reached" ? "bg-emerald-400"
            : task.runtime_status === "needs_review" ? "bg-red-400" : "bg-cyan-400")} />
      <div className="min-w-0 flex-1">
        <div className="line-clamp-2 text-xs font-medium leading-snug text-(--text-1)">{task.objective || task.task_id}</div>
        <div className="mt-1 flex items-center justify-between gap-1 text-[10px] text-(--text-3)">
          <span className="truncate">
            {goalTotal > 0 ? `目标 ${goalMet}/${goalTotal}` : `${task.intent_count} Intent`}
            {hasRuntime ? ` · ${meta.label}` : ""}
          </span>
          <span className="font-mono">{percent}%</span>
        </div>
        <div className="mt-1 h-0.5 w-full overflow-hidden rounded-full bg-(--surface-2)">
          <div className={cn("h-full rounded-full",
            percent >= 100 ? "bg-emerald-400/80" : percent > 0 ? "bg-cyan-400/70" : "bg-transparent")}
            style={{ width: `${percent}%` }} />
        </div>
        {covPercent !== null && <div className="mt-1 flex items-center gap-1.5 text-[10px] text-(--text-3)">
          <Radar className="h-2.5 w-2.5 shrink-0 text-violet-300/80" />
          <span className="truncate">覆盖 {cov!.tested}/{covDenom}</span>
          <span className="font-mono text-violet-300/80">{covPercent}%</span>
        </div>}
      </div>
    </div>
  </button>
}

// ---------- 主区小件 ----------

function Kpi({ label, value, hint, tone, pulse }: { label: string; value: number | string; hint?: string; tone?: string; pulse?: boolean }) {
  return <div className="rounded-lg border border-(--border) bg-(--surface-1) px-3 py-2">
    <div className="flex items-center gap-1.5 text-[10px] text-(--text-3)">
      {pulse && <i className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />}
      <span className="truncate">{label}</span>
    </div>
    <div className={cn("mt-0.5 font-mono text-lg font-semibold tabular-nums leading-tight", tone || "text-(--text-1)")}>{value}</div>
    {hint && <div className="text-[10px] text-(--text-3)">{hint}</div>}
  </div>
}

function TabButton({ icon: Icon, label, count, active, onClick }: {
  icon: typeof GitBranch; label: string; count: number; active: boolean; onClick: () => void
}) {
  return <button onClick={onClick}
    className={cn("inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs transition-colors",
      active ? "bg-cyan-500/10 text-cyan-300" : "text-(--text-3) hover:bg-(--surface-2) hover:text-(--text-2)")}>
    <Icon className="h-3.5 w-3.5" />{label}
    <span className={cn("rounded-full px-1.5 font-mono text-[10px]", active ? "bg-cyan-500/20" : "bg-(--surface-2)")}>{count}</span>
  </button>
}

/** 探索图子切换（画布/列表）：比 TabButton 轻一档，不带计数徽章。 */
function SubTabButton({ icon: Icon, label, active, onClick }: {
  icon: typeof Network; label: string; active: boolean; onClick: () => void
}) {
  return <button onClick={onClick}
    className={cn("inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] transition-colors",
      active ? "bg-cyan-500/10 text-cyan-300" : "text-(--text-3) hover:bg-(--surface-2) hover:text-(--text-2)")}>
    <Icon className="h-3 w-3" />{label}
  </button>
}

function EmptyState({ samples, onPick }: { samples: string[]; onPick: (goal: string) => void }) {
  return <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-5 rounded-xl border border-dashed border-(--border) bg-(--surface-1) px-6 py-10">
    <div className="flex flex-col items-center gap-2 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-2xl border border-cyan-500/30 bg-cyan-500/10 text-cyan-300">
        <WandSparkles className="h-6 w-6" />
      </span>
      <h2 className="text-sm font-semibold">还没有选中任务</h2>
      <p className="max-w-md text-xs leading-relaxed text-(--text-3)">
        在上方输入目标，Planner 会把它拆成可并行的 Intent；Engine 从 Frontier 领取后
        并发派给 Worker 执行，结果以 Fact 与 Finding 回写到探索图。
      </p>
    </div>
    <div className="flex w-full max-w-xl flex-col gap-1.5">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">试试这些目标</div>
      {samples.map(sample => <button key={sample}
        className="group flex items-center gap-2 rounded-lg border border-(--border) bg-(--surface-0) px-3 py-2 text-left text-xs text-(--text-2) transition-colors hover:border-cyan-500/40 hover:text-(--text-1)"
        onClick={() => onPick(sample)}>
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-(--text-3) group-hover:text-cyan-300" />
        <span className="min-w-0 flex-1">{sample}</span>
      </button>)}
    </div>
    <div className="grid w-full max-w-xl grid-cols-3 gap-2 text-center text-[10px] text-(--text-3)">
      {[["1 规划", "Planner 拆 Intent"], ["2 调度", "Engine 并发派发"], ["3 收敛", "目标达成或复核"]].map(([step, desc]) =>
        <div key={step} className="rounded-lg border border-(--border) bg-(--surface-0) px-2 py-2">
          <div className="font-mono text-[10px] text-cyan-300">{step}</div>
          <div className="mt-0.5">{desc}</div>
        </div>)}
    </div>
  </div>
}

// ---------- 任务图 ----------

function IntentTree({ node, byParent, byId, onControl, onSteer, onKill, busy, depth }: {
  node: ArtexIntent; byParent: Map<string, ArtexIntent[]>; byId: Map<string, ArtexIntent>
  onControl: (n: ArtexIntent, a: "pause" | "resume" | "cancel" | "retry") => void
  onSteer: (n: ArtexIntent, message: string) => void
  onKill: (n: ArtexIntent) => void
  busy: boolean; depth: number
}) {
  const [open, setOpen] = useState(true)
  const nested = byParent.get(node.id) || []
  const depInfo = useMemo(() => {
    const deps = node.depends_on || []
    if (!deps.length) return undefined
    const refs: { intent: ArtexIntent; label: string }[] = []
    let waiting = 0
    for (const did of deps) {
      const intent = byId.get(did)
      if (!intent) { waiting++; continue }
      const s = intent.status
      // exhausted 是「试过但没做完」：对下游而言和失败同效（依赖它的产出不可靠），
      // 但措辞要区分，免得用户以为是方向本身不行。
      const label = s === "completed" ? "已完成"
        : s === "exhausted" ? "预算耗尽"
        : s === "failed" || s === "cancelled" ? "已失败"
        : s === "running" || s === "claimed" ? "进行中" : "待调度"
      if (s !== "completed") waiting++
      refs.push({ intent, label })
    }
    return { refs, waiting }
  }, [node.depends_on, byId])
  return <div className="relative">
    <IntentCard node={node} onControl={onControl} onSteer={onSteer} onKill={onKill} busy={busy}
      hasChildren={nested.length > 0} open={open} onToggle={() => setOpen(v => !v)} depth={depth} depInfo={depInfo} />
    {open && nested.length > 0 && <div className="ml-3 border-l border-cyan-500/25 pl-3 pt-1.5 md:ml-5 md:pl-4">
      {nested.map(child => <div key={child.id} className="relative pb-1.5">
        <IntentTree node={child} byParent={byParent} byId={byId} onControl={onControl} onSteer={onSteer}
          onKill={onKill} busy={busy} depth={depth + 1} />
      </div>)}
    </div>}
  </div>
}

function IntentCard({ node, onControl, onSteer, onKill, busy, hasChildren = false, open = true, onToggle, depth, depInfo }: {
  node: ArtexIntent
  onControl: (n: ArtexIntent, a: "pause" | "resume" | "cancel" | "retry") => void
  onSteer: (n: ArtexIntent, message: string) => void
  onKill: (n: ArtexIntent) => void
  busy: boolean; hasChildren?: boolean; open?: boolean; onToggle?: () => void; depth: number
  depInfo?: { refs: { intent: ArtexIntent; label: string }[]; waiting: number }
}) {
  const [steerOpen, setSteerOpen] = useState(false)
  const [steerText, setSteerText] = useState("")
  const meta = statusOf(node.status)
  const actions: { action: "pause" | "resume" | "cancel" | "retry"; label: string; icon: typeof Pause }[] =
    node.status === "paused" ? [{ action: "resume", label: "恢复", icon: Play }]
      // exhausted：撞预算没做完 → 主操作是「续跑」（与 failed 的重试同一条 API）。
      : node.status === "failed" || node.status === "exhausted"
        ? [{ action: "retry", label: node.status === "exhausted" ? "续跑" : "重试", icon: RefreshCw }]
        : node.status === "blocked" ? [{ action: "cancel", label: "取消", icon: Square }]
          : [
            ...(node.status === "pending" || node.status === "claimed" || node.status === "running"
              ? [{ action: "pause" as const, label: "暂停", icon: Pause }] : []),
            ...(node.status !== "completed" && node.status !== "cancelled"
              ? [{ action: "cancel" as const, label: "取消", icon: Square }] : []),
          ]
  const running = node.status === "running" || node.status === "claimed"

  return <article className={cn("group rounded-lg border border-(--border) border-l-2 bg-(--surface-0) px-3 py-2.5 transition-colors hover:border-(--text-3)", meta.bar,
    depth === 0 && "bg-(--surface-2)")}>
    <div className="flex items-start gap-2">
      {hasChildren
        ? <button className="mt-0.5 shrink-0 text-(--text-3) transition-colors hover:text-cyan-300" onClick={onToggle}
          aria-label={open ? "折叠子任务" : "展开子任务"}>
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
        : <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-(--text-3)" />}
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium leading-snug text-(--text-1)">{node.objective}</div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--text-3)">
          <span className={cn("rounded border px-1.5 py-0.5 font-mono", meta.pill)}>{meta.label}</span>
          <span className="font-mono text-cyan-300/80">{node.expert || "自动选择"}</span>
          <span>优先级 {node.priority}</span>
          {node.attempt > 0 && <span>尝试 {node.attempt}</span>}
          {node.asset_ids.length > 0 && <span className="truncate">资产 {node.asset_ids.join(", ")}</span>}
        </div>
        {/* 串行链依赖：下游要等前置 completed 才被调度，否则置 blocked。 */}
        {depInfo && depInfo.refs.length > 0 && <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px]">
          <span className="inline-flex items-center gap-1 text-(--text-3)"><Link2 className="h-3 w-3" />依赖</span>
          {depInfo.refs.map(({ intent, label }) => <span key={intent.id}
            className={cn("truncate rounded border px-1.5 py-0.5 font-mono",
              label === "已完成" ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                : label === "预算耗尽" ? "border-yellow-500/30 bg-yellow-500/10 text-yellow-300"
                : label === "已失败" ? "border-red-500/30 bg-red-500/10 text-red-300"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-300")}>
            {intent.objective.slice(0, 40)} · {label}
          </span>)}
          {depInfo.waiting > 0 && <span className="text-amber-300/90">等 {depInfo.waiting} 个前置</span>}
        </div>}
        {node.error && <div className="mt-2 rounded border border-red-500/30 bg-red-500/10 px-2 py-1 text-[11px] leading-snug text-red-300">{node.error}</div>}
        {node.status === "blocked" && <div className="mt-2 rounded border border-orange-500/30 bg-orange-500/10 px-2 py-1 text-[11px] leading-snug text-orange-200">
          前置失败/取消后下游不会自行恢复：重试或修复前置后，本步骤会自动放行；也可直接取消该链。
        </div>}
        {node.status === "exhausted" && <div className="mt-2 rounded border border-yellow-500/30 bg-yellow-500/10 px-2 py-1 text-[11px] leading-snug text-yellow-200">
          撞运行预算被动收尾——这个方向**试过但没做完**，不是已覆盖：可「重试」接着跑，或让规划派「接上次进度继续」的新方向。
        </div>}
        {node.result && <div className="mt-2 line-clamp-2 rounded border border-(--border) bg-(--surface-1) px-2 py-1 text-[11px] leading-snug text-(--text-2)">{node.result}</div>}
        {(actions.length > 0 || running) && <div className="mt-2 flex flex-wrap items-center gap-1.5 opacity-70 transition-opacity group-hover:opacity-100">
          {actions.map(({ action, label, icon: Icon }) => <button key={action}
            className="inline-flex items-center gap-1 rounded border border-(--border) bg-(--surface-1) px-1.5 py-0.5 text-[10px] text-(--text-2) transition-colors hover:border-(--text-3) disabled:opacity-40"
            disabled={busy} onClick={() => onControl(node, action)}>
            <Icon className="h-3 w-3" />{label}
          </button>)}
          {running && <>
            <button className="inline-flex items-center gap-1 rounded border border-cyan-500/40 bg-cyan-500/10 px-1.5 py-0.5 text-[10px] text-cyan-300 transition-colors hover:bg-cyan-500/20 disabled:opacity-40"
              disabled={busy} onClick={() => setSteerOpen(v => !v)}>
              <Send className="h-3 w-3" />调整方向
            </button>
            <button className="inline-flex items-center gap-1 rounded border border-red-500/40 bg-red-500/10 px-1.5 py-0.5 text-[10px] text-red-300 transition-colors hover:bg-red-500/20 disabled:opacity-40"
              disabled={busy} onClick={() => onKill(node)}>
              <X className="h-3 w-3" />终止
            </button>
          </>}
        </div>}
        {steerOpen && running && <div className="mt-2 flex items-center gap-1.5">
          <input autoFocus
            className="h-7 min-w-0 flex-1 rounded border border-cyan-500/40 bg-(--surface-1) px-2 text-[11px] outline-none placeholder:text-(--text-3) focus:border-cyan-500"
            value={steerText} onChange={e => setSteerText(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter" && steerText.trim()) { onSteer(node, steerText); setSteerText(""); setSteerOpen(false) }
              if (e.key === "Escape") setSteerOpen(false)
            }}
            placeholder="给这个 Worker 一条新的方向，回车下发" />
          <button className="wb-btn-primary px-2 py-1 text-[10px]" disabled={busy || !steerText.trim()}
            onClick={() => { onSteer(node, steerText); setSteerText(""); setSteerOpen(false) }}>下发</button>
        </div>}
      </div>
    </div>
  </article>
}

// ---------- 目标面板 ----------

// 目标是收敛的唯一依据：Intent 跑完不等于任务完成，只有目标 met 才算达成。
// 这里把「目标 → 已关联证据」的血缘显式铺开，让主控与人工都能复核每条目标的
// 达成依据，而不是读一句模型自述。
function GoalPanel({ goals, runtimeStatus }: { goals: ArtexGoal[]; runtimeStatus: string }) {
  if (goals.length === 0) return <div className="flex flex-col items-center gap-2 py-10 text-center">
    <Target className="h-6 w-6 text-(--text-3)" />
    <div className="text-xs text-(--text-3)">当前任务没有注册目标。</div>
    <p className="max-w-md text-[11px] leading-relaxed text-(--text-3)">
      历史任务或手工建树不会有目标；新建任务时 Planner 会自动分解并登记。
      主控也可用 artex_goal(action="set") 补登记，收敛将以目标达成情况为准。
    </p>
  </div>
  const met = goals.filter(g => String(g.status) === "met").length
  const allMet = met === goals.length
  return <div className="space-y-3">
    <div className={cn("flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-[11px]",
      allMet ? "border-emerald-500/30 bg-emerald-500/[0.07] text-emerald-300"
        : "border-cyan-500/25 bg-cyan-500/[0.05] text-cyan-200")}>
      <Target className="h-3.5 w-3.5 shrink-0" />
      <span>目标达成 <b className="font-mono">{met}/{goals.length}</b></span>
      <span className="text-(--text-3)">·</span>
      <span className="text-(--text-3)">
        {allMet ? "全部目标已达成，任务可收敛（goal_reached）"
          : runtimeStatus === "needs_review" ? "探索已停摆但仍有未达成目标，需要补方向或人工复核"
            : `仍有 ${goals.length - met} 条目标待证明`}
      </span>
    </div>
    <div className="grid gap-2 lg:grid-cols-2">
      {goals.map(goal => {
        const done = String(goal.status) === "met"
        const vuln = str(goal.metadata?.vulnclass)
        const evidence = goal.evidence || []
        return <article key={goal.id} className={cn("rounded-lg border bg-(--surface-0) p-2.5",
          done ? "border-emerald-500/30" : "border-(--border)")}>
          <div className="flex items-start gap-2">
            <span className={cn("mt-px grid h-4 w-4 shrink-0 place-items-center rounded-full border font-mono text-[9px]",
              done ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-300" : "border-(--border) text-(--text-3)")}>
              {done ? <Check className="h-2.5 w-2.5" /> : "○"}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium leading-snug text-(--text-1)">{goal.title || goal.id}</div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--text-3)">
                <span className={cn("rounded border px-1.5 py-0.5 font-mono",
                  done ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" : "border-(--border)")}>
                  {done ? "已达成" : "未达成"}
                </span>
                {vuln && <span className="rounded border border-cyan-500/30 px-1.5 py-0.5 font-mono text-cyan-300">{vuln}</span>}
                <span className="font-mono">{goal.id}</span>
                {goal.asset_ids?.length > 0 && <span className="truncate">资产 {goal.asset_ids.join(", ")}</span>}
              </div>
              {evidence.length > 0
                ? <div className="mt-2 space-y-1 rounded border border-(--border) bg-(--surface-1) px-2 py-1.5">
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">证据 {evidence.length} 条</div>
                  {evidence.slice(0, 4).map(ev => <div key={ev.id} className="flex min-w-0 items-center gap-1.5 text-[11px]">
                    <span className={cn("shrink-0 rounded border px-1 py-0.5 font-mono text-[9px]",
                      EXPLORATION_META[ev.kind]?.pill || "border-(--border) text-(--text-3)")}>
                      {EXPLORATION_META[ev.kind]?.label || ev.kind}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-(--text-2)">{ev.title || ev.id}</span>
                  </div>)}
                  {evidence.length > 4 && <div className="text-[10px] text-(--text-3)">…共 {evidence.length} 条</div>}
                </div>
                : <div className="mt-2 rounded border border-dashed border-(--border) px-2 py-1.5 text-[10px] text-(--text-3)">
                  尚无系统关联证据；可用 artex_goal(action="prove") 显式证明
                </div>}
            </div>
          </div>
        </article>
      })}
    </div>
  </div>
}

const str = (value: unknown) => typeof value === "string" ? value : ""

// ---------- 覆盖面板 ----------

// 资产类型标签（本项目自有类型，不做上游 kind 的有损映射）。
const ASSET_META: Record<string, { label: string; pill: string }> = {
  domain: { label: "域名", pill: "border-cyan-500/40 bg-cyan-500/10 text-cyan-300" },
  host: { label: "主机", pill: "border-violet-500/40 bg-violet-500/10 text-violet-300" },
  service: { label: "服务", pill: "border-amber-500/40 bg-amber-500/10 text-amber-300" },
  url: { label: "URL", pill: "border-sky-500/40 bg-sky-500/10 text-sky-300" },
  binary: { label: "二进制", pill: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300" },
  challenge: { label: "题目", pill: "border-rose-500/40 bg-rose-500/10 text-rose-300" },
}
const assetMeta = (type: string) =>
  ASSET_META[type] || { label: type || "资产", pill: "border-(--border) text-(--text-3)" }

// 覆盖度是把「进度」从主观判断改成可核验指标的那一刀：分母是任务锚定的范围资产，
// 分子是被探索图 Fact 锚定过的资产。上游明说这是「粗略参考」，因此这里不粉饰——
// 范围未锚定时显式提示回退为 Intent∪目标推断，分母 0 时如实显示「—」。
function CoveragePanel({ coverage, graph, pid, taskId, onChanged }: {
  coverage: ArtexCoverageResponse | null
  graph: { task_id: string; nodes: ArtexCoverageGraphNode[]; edges: { src: string; dst: string }[] } | null
  pid: string
  taskId: string
  onChanged: () => void
}) {
  const [type, setType] = useState("")
  const [items, setItems] = useState<{ id: string; type: string; label: string; status: string }[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [anchorText, setAnchorText] = useState("")
  const [anchorBusy, setAnchorBusy] = useState(false)
  const [anchorMsg, setAnchorMsg] = useState<string | null>(null)

  const cov: ArtexCoverage | undefined = coverage?.coverage
  const denominator = cov?.denominator || 0
  const tested = cov?.tested || 0
  const percent = cov?.pct === null || cov?.pct === undefined ? null : Math.round(cov.pct * 100)

  const fetchUntested = useCallback(async (nextType: string, offset: number) => {
    if (!taskId.trim()) return
    setLoading(true)
    try {
      const response = await api.artexTaskCoverage(pid, taskId.trim(), nextType, 30, offset)
      setTotal(response.untested.total)
      setItems(current => offset === 0 ? response.untested.assets : current.concat(response.untested.assets))
    } catch { /* 未测清单失败不阻断覆盖度展示 */ }
    finally { setLoading(false) }
  }, [pid, taskId])

  useEffect(() => { void fetchUntested(type, 0) }, [fetchUntested, type])

  // 手工锚定：Planner 未登记的范围资产，可由人工补上（或从范围内摘除）。
  const submitAnchor = async (remove: boolean) => {
    const ids = anchorText.split(/[\s,，;；]+/).map(v => v.trim()).filter(Boolean)
    if (ids.length === 0 || !taskId.trim()) return
    setAnchorBusy(true); setAnchorMsg(null)
    try {
      const response = await api.artexTaskScope(pid, taskId.trim(), { asset_ids: ids, remove })
      if (remove) setAnchorMsg(`已摘除 ${response.result?.removed ?? 0} 个`)
      else {
        const added = response.result?.added?.length ?? 0
        const missing = response.result?.missing?.length ?? 0
        const existing = response.result?.existing?.length ?? 0
        setAnchorMsg(`新增 ${added} · 已存在 ${existing}${missing > 0 ? ` · 无效 ${missing}` : ""}`)
      }
      setAnchorText("")
      onChanged()
    } catch (e) { setAnchorMsg(e instanceof Error ? e.message : "锚定失败") }
    finally { setAnchorBusy(false) }
  }

  if (!coverage || !cov) return <div className="py-10 text-center text-xs text-(--text-3)">覆盖度加载中…</div>
  if (!cov.enabled) return <div className="flex flex-col items-center gap-2 py-10 text-center">
    <Radar className="h-6 w-6 text-(--text-3)" />
    <div className="text-xs text-(--text-3)">当前项目未开启资产覆盖度。</div>
    <p className="max-w-md text-[11px] leading-relaxed text-(--text-3)">
      覆盖度用于把「测没测过」变成可核验指标。开启后，Planner 会把任务范围资产登记为分母，
      Worker 产出的事实会累积分子。配置项：config.artex.coverage_enabled。
    </p>
  </div>

  const inferred = cov.scope_source === "inferred"
  return <div className="space-y-3">
    {/* 概览：分子/分母 + 进度条 + 口径提示 */}
    <div className={cn("rounded-lg border px-3 py-2.5",
      percent !== null && percent >= 100 ? "border-emerald-500/30 bg-emerald-500/[0.07]"
        : "border-violet-500/25 bg-violet-500/[0.05]")}>
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <Radar className={cn("h-3.5 w-3.5 shrink-0", percent !== null && percent >= 100 ? "text-emerald-300" : "text-violet-300")} />
        <span>资产覆盖 <b className="font-mono">{tested}/{denominator}</b>{percent !== null && <>（<b className="font-mono">{percent}%</b>）</>}</span>
        <span className="text-(--text-3)">·</span>
        <span className="text-(--text-3)">未覆盖 {cov.untested ?? Math.max(0, denominator - tested)} 个</span>
        <span className="ml-auto rounded border px-1.5 py-0.5 font-mono text-[10px] border-(--border) text-(--text-3)">
          {inferred ? "范围推断" : "任务锚点"}
        </span>
      </div>
      <div className="mt-2 h-1 w-full overflow-hidden rounded-full bg-(--surface-2)">
        <div className={cn("h-full rounded-full transition-[width]",
          percent !== null && percent >= 100 ? "bg-emerald-400/80" : "bg-violet-400/70")}
          style={{ width: `${percent ?? 0}%` }} />
      </div>
      {denominator === 0 && <div className="mt-2 rounded border border-dashed border-(--border) px-2 py-1.5 text-[10px] leading-relaxed text-(--text-3)">
        还没登记范围资产，分母为空 —— 覆盖度暂无意义。新建任务时 Planner 会自动锚定；也可用下方输入框或 artex_goal(action="anchor") 手工补登记。
      </div>}
      {/* 未量化验收提醒：目标含「覆盖度达 X%」时，它是下限/验收项，不是探索目标本身。 */}
      {percent !== null && percent < 100 && denominator > 0 && <div className="mt-2 text-[10px] leading-relaxed text-(--text-3)">
        覆盖度是粗粒度下限参考（非精确度量），深度优先于覆盖度；补齐未覆盖资产前不应提前声明量化目标达成。
      </div>}
    </div>

    {/* 手工锚定范围资产（Planner 之外的兜底入口） */}
    <div className="rounded-lg border border-(--border) bg-(--surface-0) p-2.5">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">登记范围资产</div>
      <div className="flex flex-wrap items-center gap-1.5">
        <input
          className="h-8 min-w-[200px] flex-1 rounded border border-(--border) bg-(--surface-1) px-2 font-mono text-[11px] outline-none placeholder:text-(--text-3) focus:border-violet-500/60"
          value={anchorText} onChange={e => setAnchorText(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") void submitAnchor(false) }}
          placeholder="资产 ID，空格/逗号分隔" />
        <button className="wb-btn-primary inline-flex items-center gap-1 px-2.5 py-1 text-[11px]"
          disabled={anchorBusy || !anchorText.trim()} onClick={() => void submitAnchor(false)}>
          {anchorBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}锚定
        </button>
        <button className="wb-btn-secondary inline-flex items-center gap-1 px-2.5 py-1 text-[11px]"
          disabled={anchorBusy || !anchorText.trim()} onClick={() => void submitAnchor(true)}>
          <X className="h-3 w-3" />摘除
        </button>
      </div>
      {anchorMsg && <div className="mt-1.5 text-[10px] text-(--text-3)">{anchorMsg}</div>}
      <div className="mt-1 text-[10px] leading-relaxed text-(--text-3)">锚定会把资产登记进覆盖度分母（只接受本项目真实存在的资产）。</div>
    </div>

    {/* 按类型分桶 */}
    {cov.by_type.length > 0 && <div className="rounded-lg border border-(--border) bg-(--surface-0) p-2.5">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">按类型</div>
      <div className="space-y-1.5">
        {cov.by_type.map(row => {
          const rowPct = row.total > 0 ? Math.round((row.tested / row.total) * 100) : 0
          const meta = assetMeta(row.type)
          return <div key={row.type} className="flex items-center gap-2">
            <span className={cn("w-16 shrink-0 rounded border px-1.5 py-0.5 text-center font-mono text-[10px]", meta.pill)}>{meta.label}</span>
            <span className="w-16 shrink-0 font-mono text-[10px] text-(--text-3)">{row.tested}/{row.total}</span>
            <div className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-(--surface-2)">
              <div className={cn("h-full rounded-full", rowPct >= 100 ? "bg-emerald-400/80" : "bg-violet-400/70")}
                style={{ width: `${rowPct}%` }} />
            </div>
            <span className="w-8 shrink-0 text-right font-mono text-[10px] text-(--text-3)">{rowPct}%</span>
          </div>
        })}
      </div>
    </div>}

    {/* 未覆盖清单（补测 backlog） */}
    <div className="rounded-lg border border-(--border) bg-(--surface-0) p-2.5">
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">未覆盖清单</span>
        <span className="font-mono text-[10px] text-(--text-3)">{total}</span>
        <div className="ml-auto flex flex-wrap items-center gap-1">
          <button onClick={() => setType("")}
            className={cn("rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors",
              type === "" ? "border-violet-500/50 bg-violet-500/10 text-violet-300" : "border-(--border) text-(--text-3) hover:text-(--text-2)")}>
            全部
          </button>
          {cov.by_type.map(row => <button key={row.type} onClick={() => setType(row.type)}
            className={cn("rounded border px-1.5 py-0.5 font-mono text-[10px] transition-colors",
              type === row.type ? "border-violet-500/50 bg-violet-500/10 text-violet-300" : "border-(--border) text-(--text-3) hover:text-(--text-2)")}>
            {assetMeta(row.type).label}
          </button>)}
        </div>
      </div>
      {items.length === 0
        ? <div className="rounded border border-dashed border-(--border) px-2 py-3 text-center text-[10px] text-(--text-3)">
          {loading ? "加载中…" : denominator === 0 ? "范围未锚定，暂无可补测资产。" : "范围内资产都已覆盖。"}
        </div>
        : <div className="space-y-1">
          {items.map(asset => {
            const meta = assetMeta(asset.type)
            return <div key={asset.id} className="flex min-w-0 items-center gap-2 rounded border border-(--border) bg-(--surface-1) px-2 py-1 text-[11px]">
              <span className={cn("shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px]", meta.pill)}>{meta.label}</span>
              <span className="min-w-0 flex-1 truncate text-(--text-2)">{asset.label || asset.id}</span>
              <span className="shrink-0 font-mono text-[10px] text-(--text-3)">{asset.status}</span>
            </div>
          })}
          {items.length < total && <button disabled={loading}
            className="wb-btn-secondary mt-1 inline-flex w-full items-center justify-center gap-1.5 py-1 text-[10px] disabled:opacity-50"
            onClick={() => void fetchUntested(type, items.length)}>
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <ChevronDown className="h-3 w-3" />}
            加载更多（{items.length}/{total}）
          </button>}
        </div>}
    </div>

    {/* 覆盖图：范围内资产 + 为连边拉入的范围外祖先（灰） */}
    {graph && (graph.nodes.length > 0
      ? <div className="rounded-lg border border-(--border) bg-(--surface-0) p-2.5">
        <div className="mb-2 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">
          <span>覆盖图</span>
          <span className="ml-auto flex items-center gap-2 font-normal normal-case">
            <span className="inline-flex items-center gap-1"><i className="h-1.5 w-1.5 rounded-full bg-emerald-400" />已测</span>
            <span className="inline-flex items-center gap-1"><i className="h-1.5 w-1.5 rounded-full bg-violet-400" />在范围</span>
            <span className="inline-flex items-center gap-1"><i className="h-1.5 w-1.5 rounded-full bg-zinc-500" />范围外</span>
          </span>
        </div>
        <CoverageTree nodes={graph.nodes} edges={graph.edges} />
      </div>
      : <div className="rounded-lg border border-dashed border-(--border) px-2 py-3 text-center text-[10px] text-(--text-3)">覆盖图为空。</div>)}
  </div>
}

function CoverageTree({ nodes, edges }: {
  nodes: ArtexCoverageGraphNode[]; edges: { src: string; dst: string }[]
}) {
  const byKey = useMemo(() => new Map(nodes.map(node => [node.key, node])), [nodes])
  const children = useMemo(() => {
    const map = new Map<string, string[]>()
    for (const edge of edges) {
      if (!byKey.has(edge.src) || !byKey.has(edge.dst)) continue
      const list = map.get(edge.dst) || []
      list.push(edge.src)
      map.set(edge.dst, list)
    }
    return map
  }, [edges, byKey])
  const roots = useMemo(() => {
    const childKeys = new Set(edges.map(edge => edge.src))
    return nodes.filter(node => !childKeys.has(node.key)).map(node => node.key)
  }, [nodes, edges])
  const [open, setOpen] = useState<Record<string, boolean>>({})
  if (nodes.length === 0) return null

  const render = (key: string, depth: number): ReactNode => {
    const node = byKey.get(key)
    if (!node) return null
    const kids = children.get(key) || []
    const expanded = open[key] ?? depth < 1
    const meta = assetMeta(node.kind)
    return <div key={key}>
      <div className="flex items-center gap-1.5 rounded border border-(--border) bg-(--surface-1) px-2 py-1"
        style={{ marginLeft: depth * 12 }}>
        {kids.length > 0
          ? <button className="shrink-0 text-(--text-3) hover:text-cyan-300"
            onClick={() => setOpen(current => ({ ...current, [key]: !expanded }))} aria-label={expanded ? "折叠" : "展开"}>
            {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          </button>
          : <span className="w-3 shrink-0" />}
        <i className={cn("h-1.5 w-1.5 shrink-0 rounded-full",
          node.tested ? "bg-emerald-400" : node.in_scope ? "bg-violet-400" : "bg-zinc-500")} />
        <span className={cn("shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px]",
          node.in_scope ? meta.pill : "border-(--border) text-(--text-3)")}>{meta.label}</span>
        <span className={cn("min-w-0 flex-1 truncate text-[11px]",
          node.in_scope ? "text-(--text-2)" : "text-(--text-3)")}>{node.label || node.asset_id}</span>
        {node.tested && <Check className="h-3 w-3 shrink-0 text-emerald-400/80" />}
        {!node.in_scope && <span className="shrink-0 font-mono text-[9px] text-(--text-3)">范围外</span>}
      </div>
      {expanded && kids.map(kid => render(kid, depth + 1))}
    </div>
  }
  return <div className="space-y-1">{roots.map(key => render(key, 0))}</div>
}

// ---------- 探索图 ----------

function ExplorationPanel({ graph }: { graph: ArtexExplorationGraph | null }) {
  if (!graph) return <div className="py-10 text-center text-xs text-(--text-3)">探索图加载中…</div>
  const byId = new Map(graph.nodes.map(node => [node.id, node]))
  if (graph.nodes.length === 0) return <div className="py-10 text-center text-xs text-(--text-3)">当前任务尚未产生探索节点。</div>
  const counts = graph.nodes.reduce<Record<string, number>>((acc, node) => {
    acc[node.kind] = (acc[node.kind] || 0) + 1
    return acc
  }, {})
  return <div className="space-y-3">
    <div className="flex flex-wrap items-center gap-1.5">
      {Object.entries(counts).map(([kind, count]) => <span key={kind}
        className={cn("rounded-full border px-2 py-0.5 text-[10px]", EXPLORATION_META[kind]?.pill || "border-(--border) text-(--text-3)")}>
        {EXPLORATION_META[kind]?.label || kind} {count}
      </span>)}
      <span className="ml-auto text-[10px] text-(--text-3)">{graph.edges.length} 条血缘边</span>
    </div>
    <div className="grid gap-2 lg:grid-cols-2">
      {graph.nodes.map(node => {
        const edges = graph.edges.filter(edge => edge.from_node_id === node.id || edge.to_node_id === node.id)
        return <article key={node.id} className="rounded-lg border border-(--border) bg-(--surface-0) p-2.5">
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 items-start gap-1.5">
              <span className={cn("mt-px shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px]", EXPLORATION_META[node.kind]?.pill || "border-(--border) text-(--text-3)")}>
                {EXPLORATION_META[node.kind]?.label || node.kind}
              </span>
              <span className="min-w-0 flex-1 text-xs font-medium leading-snug text-(--text-1)">{node.title || node.id}</span>
            </div>
            <span className="shrink-0 font-mono text-[10px] text-(--text-3)">{Math.round((node.confidence || 0) * 100)}%</span>
          </div>
          {node.content && <p className="mt-1.5 line-clamp-2 text-[11px] leading-snug text-(--text-2)">{node.content}</p>}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-(--text-3)">
            <span>{node.status}</span>
            {/* 事实的置信度语义（artex-record-fact）：observed=输出里直接看到、
                inferred=据现象推断。数值百分比看不出这个区别，而它决定
                「该方向是否已穷尽」——否定结论多为 inferred，不能当成已覆盖。 */}
            {node.kind === "fact" && <span className={cn("rounded border px-1 py-0.5 font-mono",
              confidenceLevel(node) === "observed"
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                : "border-amber-500/30 bg-amber-500/10 text-amber-300")}>
              {confidenceLevel(node)}
            </span>}
            {node.kind === "fact" && String((node.metadata || {}).source || "") === "intent_result"
              && <span className="rounded border border-(--border) px-1 py-0.5">整轮汇总</span>}
            <span>{edges.length} 条关系</span>
            {node.asset_ids.length > 0 && <span className="truncate">资产 {node.asset_ids.join(", ")}</span>}
          </div>
        </article>
      })}
    </div>
    {graph.edges.length > 0 && <div className="rounded-lg border border-(--border) bg-(--surface-0) p-2.5">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-(--text-3)">血缘关系</div>
      <div className="space-y-1">
        {graph.edges.map(edge => <div key={edge.id} className="flex min-w-0 items-center gap-1.5 text-[11px]">
          <span className="max-w-[38%] truncate text-(--text-2)">{byId.get(edge.from_node_id)?.title || edge.from_node_id}</span>
          <span className="shrink-0 rounded border border-cyan-500/30 px-1 py-0.5 font-mono text-[9px] text-cyan-300">{edge.relation}</span>
          <ChevronRight className="h-3 w-3 shrink-0 text-(--text-3)" />
          <span className="max-w-[38%] truncate text-(--text-2)">{byId.get(edge.to_node_id)?.title || edge.to_node_id}</span>
        </div>)}
      </div>
    </div>}
  </div>
}

// ---------- 活动流 ----------

const ACTIVITY_TONE: Record<string, string> = {
  "artex.intent.created": "border-cyan-500/40",
  "artex.intent.completed": "border-emerald-500/40",
  "artex.intent.failed": "border-red-500/40",
  "artex.worker.steer": "border-amber-500/40",
  "artex.task.status": "border-violet-500/40",
  "artex.replan.completed": "border-cyan-500/40",
  "artex.replan.failed": "border-red-500/40",
  "chat.tool": "border-slate-500/40",
}

function ActivityTimeline({ events }: { events: ArtexActivityEvent[] }) {
  if (events.length === 0) return <div className="py-10 text-center text-xs text-(--text-3)">暂无 ARTEX 活动。启动 Engine 后这里会实时滚动。</div>
  return <div className="space-y-1">
    {events.map(event => <div key={event.id}
      className={cn("flex items-start gap-2.5 rounded-md border-l-2 bg-(--surface-0) px-2.5 py-1.5", ACTIVITY_TONE[event.kind] || "border-(--border)")}>
      <div className="min-w-0 flex-1">
        <div className="text-[11px] font-medium text-(--text-2)">{activityLabel(event.kind)}</div>
        <div className="mt-0.5 line-clamp-2 text-[10px] leading-snug text-(--text-3)">{activityDetail(event)}</div>
      </div>
      <time className="shrink-0 font-mono text-[10px] text-(--text-3)">{new Date(event.created_at).toLocaleTimeString()}</time>
    </div>)}
  </div>
}

// ---------- 活动流文案 ----------

function activityLabel(kind: string): string {
  switch (kind) {
    case "artex.intent.created": return "生成 Intent"
    case "artex.intent.claimed": return "Worker 领取"
    case "artex.intent.running": return "开始执行"
    case "artex.intent.completed": return "执行完成"
    case "artex.intent.failed": return "执行失败"
    case "artex.intent.recovered": return "崩溃恢复"
    case "artex.intent.aggregated": return "父任务聚合"
    case "artex.intent.pause": return "暂停 Intent"
    case "artex.intent.resume": return "恢复 Intent"
    case "artex.intent.cancel": return "取消 Intent"
    case "artex.intent.retry": case "artex.intent.retried": return "重试 Intent"
    case "artex.worker.steer": return "方向调整"
    case "artex.worker.kill": return "终止 Worker"
    case "artex.replan.started": return "重规划开始"
    case "artex.replan.completed": return "重规划完成"
    case "artex.replan.failed": return "重规划失败"
    case "artex.task.status": return "任务状态变化"
    case "artex.task.paused": return "任务暂停"
    case "artex.exploration.node.created": return "探索新节点"
    case "artex.exploration.fact.recorded": return "记录事实"
    case "chat.spawn": return "派生子代理"
    case "chat.tool": return "工具调用"
    case "chat.steer": return "方向干预"
    case "chat.error": return "对话错误"
    case "approval.requested": return "等待审批"
    case "approval.approved": return "审批通过"
    case "approval.rejected": return "审批驳回"
    case "scope.expanded": return "范围扩展"
    case "finding.new": return "新发现"
    case "finding.updated": return "发现更新"
    case "finding.deleted": return "发现删除"
    case "finding.retracted": return "发现撤下"
    default: return kind
  }
}

function pstr(payload: Record<string, unknown>, key: string): string {
  const v = payload[key]
  if (v === undefined || v === null) return ""
  if (typeof v === "string") return v
  if (typeof v === "number" || typeof v === "boolean") return String(v)
  try { return JSON.stringify(v) } catch { return "" }
}

function activityDetail(event: ArtexActivityEvent): string {
  const p = event.payload || {}
  const iid = pstr(p, "intent_id") || pstr(p, "source_intent_id")
  const who = event.author ? ` · ${event.author}` : ""
  switch (event.kind) {
    case "artex.intent.created": {
      const expert = pstr(p, "expert")
      const prio = pstr(p, "priority")
      return [pstr(p, "objective"), expert ? `专家 ${expert}` : "", prio ? `P${prio}` : ""]
        .filter(Boolean).join(" · ")
    }
    case "artex.intent.claimed":
      return `${iid} 被 ${pstr(p, "worker") || "Worker"} 领取`
    case "artex.intent.failed":
      return [iid, pstr(p, "error")].filter(Boolean).join(" · ")
    case "artex.intent.recovered": {
      const from = pstr(p, "from"); const to = pstr(p, "to")
      const age = pstr(p, "age_seconds")
      return [`${iid} ${from}→${to}`, `第 ${pstr(p, "attempt")} 次尝试`, age ? `停滞 ${age}s` : ""]
        .filter(Boolean).join(" · ")
    }
    case "artex.intent.aggregated": {
      const n = pstr(p, "children")
      const covered = pstr(p, "child_statuses")
      return [`父 Intent ${iid}`, n ? `${n} 个子任务` : "", covered].filter(Boolean).join(" · ")
    }
    case "artex.worker.steer":
      return `${iid}：${pstr(p, "message")}`
    case "artex.worker.kill":
      return `${iid}：${pstr(p, "reason") || "人工终止"}`
    case "artex.replan.started":
      return `触发源 ${iid}（${pstr(p, "trigger") || "intent.finished"}）`
    case "artex.replan.completed": {
      const added = pstr(p, "intent_count")
      const idx = pstr(p, "replan_index")
      return [`第 ${idx || "?"} 轮`, `新增 ${added || 0} 个 Intent`,
        p.goal_covered ? "目标已覆盖" : "目标未全覆盖",
        p.budget_exhausted ? "预算耗尽" : "", pstr(p, "reason")].filter(Boolean).join(" · ")
    }
    case "artex.replan.failed":
      return [iid, pstr(p, "error")].filter(Boolean).join(" · ")
    case "artex.task.status":
      return `${pstr(p, "previous_status") || "?"} → ${pstr(p, "status")}${pstr(p, "reason") ? ` · ${pstr(p, "reason")}` : ""}`
    case "artex.task.paused":
      return `暂停 ${pstr(p, "paused")} 个 Intent${pstr(p, "reason") ? ` · ${pstr(p, "reason")}` : ""}`
    case "chat.tool": {
      const phase = pstr(p, "phase")
      const name = pstr(p, "name")
      if (phase === "done") return `工具 ${name} 返回`
      return `调用工具 ${name}`
    }
    case "chat.spawn": {
      const role = pstr(p, "role") || pstr(p, "expert") || pstr(p, "name")
      return role ? `派生专家 ${role}${who}` : `派生执行体${who}`
    }
    case "finding.new": {
      const title = pstr(p, "title") || pstr(p, "name")
      const sev = pstr(p, "severity")
      return [title, sev].filter(Boolean).join(" · ")
    }
    default: {
      const keys = Object.keys(p).filter(k => k !== "intent_id")
      if (keys.length === 0) return iid ? `${iid}${who}` : who.replace(/^ · /, "") || "—"
      return keys.slice(0, 3).map(k => pstr(p, k)).filter(Boolean).join(" · ") || (iid + who) || "—"
    }
  }
}
