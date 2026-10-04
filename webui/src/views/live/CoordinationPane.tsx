import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Activity, ChevronRight, CircleAlert, Clock3, FileKey2, GitBranch, MessageCircle, Pause, Play, Radio, RefreshCw, Users, Mail } from "lucide-react"
import { api } from "@/lib/api"
import type { BBEvent, CoordinationOverview, CoordinationTask, CoordinationTaskStatus, Session, SessionGraph } from "@/lib/types"
import { PlanDag } from "./coordination/PlanDag"
import { PlanConfirmDialog } from "./coordination/PlanConfirmDialog"
import { PlanControlBar } from "./coordination/PlanControlBar"
import { CommunicationDrawer } from "./coordination/CommunicationDrawer"
import { cn } from "@/lib/utils"

// 多智能体协调（并入直播间 2026-10-04）：原独立顶级页 MultiAgentCoordinationView 迁为
// LiveRoom 第三段视图（直播｜任务树｜协调）。计划/依赖图/驾驶舱/对象冲突只读面复用，
// 数据源不变（coordination/sessions/session-graph/events 3s 轮询）。工作台不涉及。

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", active: "运行中", paused: "已暂停", completed: "已完成",
  pending: "待规划", ready: "可执行", running: "执行中", blocked: "等待依赖",
  failed: "失败",
}
const OBJECT_LABEL: Record<string, string> = {
  function: "函数", string: "字符串", xref: "Xref", behavior: "行为", evidence: "证据", artifact: "产物",
}

export function CoordinationPane({ pid, onOpenSession }: {
  pid: string
  onOpenSession?: (sid: string) => void
}) {
  const [data, setData] = useState<CoordinationOverview | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionGraph, setSessionGraph] = useState<SessionGraph | null>(null)
  const [events, setEvents] = useState<BBEvent[]>([])
  const loadSeq = useRef(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<string | null>(null)
  const [planName, setPlanName] = useState("")
  const [objective, setObjective] = useState("")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [commOpen, setCommOpen] = useState(false)
  const [controlBusy, setControlBusy] = useState(false)

  const load = useCallback(() => {
    const seq = ++loadSeq.current
    setLoading(true)
    return Promise.allSettled([
      api.coordination(pid),
      api.sessions(pid),
      api.sessionGraph(pid),
      api.eventsTail(pid, 80),
    ]).then(([coordination, sessionRows, graph, activity]) => {
      if (seq !== loadSeq.current) return
      if (coordination.status === "fulfilled") {
        const next = coordination.value
        setData(next)
        setSelectedId((cur) => cur && next.plans.some((p) => p.id === cur) ? cur : next.plans[0]?.id ?? null)
      } else setNotice("协调数据加载失败")
      if (sessionRows.status === "fulfilled") setSessions(sessionRows.value)
      if (graph.status === "fulfilled") setSessionGraph(graph.value)
      if (activity.status === "fulfilled") setEvents(activity.value)
    }).finally(() => setLoading(false))
  }, [pid])

  useEffect(() => { void load(); const t = window.setInterval(() => void load(), 3000); return () => window.clearInterval(t) }, [load])

  const selected = useMemo(() => data?.plans.find((p) => p.id === selectedId) ?? null, [data, selectedId])
  const selectedTask = useMemo(() => selected?.tasks.find((task) => task.id === selectedTaskId) ?? null, [selected, selectedTaskId])
  useEffect(() => {
    if (!selected) { setSelectedTaskId(null); return }
    setSelectedTaskId((cur) => cur && selected.tasks.some((task) => task.id === cur) ? cur : selected.tasks[0]?.id ?? null)
  }, [selected])
  // 节点绑定真实任务（task_id）→ 反查承载会话（sessions.bound_task_id），供「跳到会话」
  const openBoundTask = useCallback((taskId: string) => {
    const sid = sessions.find((s) => s.bound_task_id === taskId)?.id
    if (sid) onOpenSession?.(sid)
  }, [sessions, onOpenSession])

  const createPlan = async () => {
    if (!planName.trim()) return
    try {
      const created = await api.coordinationPlanCreate(pid, { name: planName, objective })
      setPlanName(""); setObjective(""); setSelectedId(created.id); setNotice("协调计划已创建"); await load()
    } catch { setNotice("创建计划失败") }
  }
  const setPlanStatus = async (status: string) => {
    if (!selected) return
    try { await api.coordinationPlanStatus(pid, selected.id, status); await load() } catch { setNotice("计划状态更新失败") }
  }
  const controlPlan = async (action: "pause" | "resume" | "interrupt" | "retry") => {
    if (!selected) return
    setControlBusy(true)
    try { await api.coordinationPlanControl(pid, selected.id, action); setNotice(`计划已执行：${action}`); await load() }
    catch (e) { setNotice(e instanceof Error ? e.message : "计划控制失败") }
    finally { setControlBusy(false) }
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-1.5">
        <GitBranch className="size-3.5 text-muted-foreground" />
        <span className="text-xs font-medium">多智能体协调</span>
        <span className="text-[11px] text-muted-foreground">计划由 Orchestrator 的 plan_work 生成，节点绑定真实任务后自动同步</span>
        <span className="flex-1" />
        {notice && <span className="text-[11px] text-muted-foreground">{notice}</span>}
        <button type="button" className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent" title="通信" onClick={() => setCommOpen(true)}><Mail className="size-3.5" /></button>
        <button type="button" className="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent" title="刷新协调数据" onClick={() => void load()}>
          <RefreshCw className="size-3.5" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
      <section className="coord-metrics">
        <Metric label="协调计划" value={data?.summary.plans ?? 0} />
        <Metric label="全部任务" value={data?.summary.tasks ?? 0} />
        <Metric label="执行中" value={data?.summary.running ?? 0} accent="active" />
        <Metric label="等待处理" value={data?.summary.blocked ?? 0} accent="warn" />
        <Metric label="已完成" value={data?.summary.completed ?? 0} accent="done" />
        <Metric label="结构化对象" value={data?.summary.objects ?? 0} />
        <Metric label="开放冲突" value={data?.summary.conflicts ?? 0} accent="warn" />
      </section>

      <PlanControlBar plan={selected} busy={controlBusy} onStart={() => setConfirmOpen(true)} onControl={(action) => void controlPlan(action)} onTimeline={() => setNotice("计划时间线将在回看面板展示")} />
      <CoordinationCockpit
        plan={selected}
        sessions={sessions}
        sessionGraph={sessionGraph}
        events={events}
        selectedTask={selectedTask}
        onSelectTask={setSelectedTaskId}
        onOpenSession={onOpenSession}
      />

      <div className="coord-grid">
        <aside className="coord-plans">
          <div className="coord-section-head"><span>协调计划</span><span className="coord-count">{data?.plans.length ?? 0}</span></div>
          <div className="coord-plan-create">
            <input value={planName} onChange={(e) => setPlanName(e.target.value)} placeholder="新计划名称" onKeyDown={(e) => e.key === "Enter" && void createPlan()} />
            <textarea value={objective} onChange={(e) => setObjective(e.target.value)} placeholder="目标与边界（可选）" rows={2} />
            <button className="coord-primary" disabled={!planName.trim()} onClick={() => void createPlan()}>创建计划</button>
          </div>
          <div className="coord-plan-list">
            {loading && !data && <div className="coord-empty">加载协调数据…</div>}
            {!loading && !data?.plans.length && <div className="coord-empty">还没有协调计划</div>}
            {data?.plans.map((plan) => (
              <button key={plan.id} className={cn("coord-plan-item", selectedId === plan.id && "is-selected")} onClick={() => setSelectedId(plan.id)}>
                <span className="coord-plan-dot" data-status={plan.status} />
                <span className="coord-plan-copy"><b>{plan.name}</b><small>{plan.tasks.length} 个任务 · {STATUS_LABEL[plan.status]}</small></span>
                <ChevronRight size={14} />
              </button>
            ))}
          </div>
        </aside>

        <main className="coord-detail">
          {!selected ? <div className="coord-empty coord-empty-large"><GitBranch size={28} /><b>选择或创建一个协调计划</b><span>计划会在这里展开任务依赖和执行状态。</span></div> : (
            <>
              <div className="coord-detail-head">
                <div><div className="coord-eyebrow">ACTIVE PLAN</div><h2>{selected.name}</h2><p>{selected.objective || "尚未填写计划目标"}</p></div>
                <div className="coord-actions">
                  {selected.status === "active" && <button className="coord-secondary" onClick={() => void setPlanStatus("paused")}><Pause size={13} />暂停计划</button>}
                  {selected.status !== "active" && selected.status !== "completed" && <button className="coord-primary" onClick={() => void setPlanStatus("active")}><Play size={13} />启动计划</button>}
                  {selected.status === "paused" && <button className="coord-secondary" onClick={() => void setPlanStatus("draft")}><RefreshCw size={13} />回到草稿</button>}
                </div>
              </div>
              <div className="coord-task-board">
                <div className="coord-section-head"><span>编排计划依赖图</span><span className="coord-hint">节点绑定真实任务后自动同步；点击节点打开会话</span></div>
                {!selected.tasks.length ? <div className="coord-empty">等待编排器生成计划</div> : <PlanDag tasks={selected.tasks} sessions={sessions} onOpenSession={onOpenSession} onOpenTask={openBoundTask} />}
              </div>
              <TeamRoster plan={selected} sessions={sessions} onOpenSession={onOpenSession} />
              <section className="coord-knowledge coord-legacy-knowledge">
                <div className="coord-section-head"><span><FileKey2 size={13} /> 结构化对象与冲突</span><span className="coord-hint">只读兼容面；产出由 Agent 黑板 / findings / func_kb 写入</span></div>
                {(data?.objects ?? []).slice(0, 12).map((obj) => <div className="coord-object" key={obj.id}><span className="coord-object-kind">{OBJECT_LABEL[obj.kind] ?? obj.kind}</span><div><b>{obj.name || obj.object_ref}</b><small>{obj.object_ref && obj.name ? obj.object_ref : ""} · 来源 {obj.source || "未指定"} · 置信度 {Math.round(obj.confidence * 100)}%</small></div></div>)}
                {(data?.conflicts ?? []).filter((c) => c.status === "open").map((conflict) => <div className="coord-conflict" key={conflict.id}><CircleAlert size={13} /><span>{conflict.summary}</span><small>待对应验证/发现流程处理</small></div>)}
              </section>
            </>
          )}
        </main>
      </div>
      </div>
      <PlanConfirmDialog pid={pid} plan={selected} open={confirmOpen} onOpenChange={setConfirmOpen} onStarted={() => { setNotice("计划已启动，编排轮已提交"); void load() }} />
      <CommunicationDrawer pid={pid} sessions={sessions} open={commOpen} onClose={() => setCommOpen(false)} onOpenSession={onOpenSession} />
    </div>
  )
}

function Metric({ label, value, accent }: { label: string; value: number; accent?: string }) {
  return <div className="coord-metric"><span>{label}</span><b className={accent ? `is-${accent}` : undefined}>{value}</b></div>
}

function TeamRoster({ plan, sessions, onOpenSession }: {
  plan: CoordinationOverview["plans"][number]
  sessions: Session[]
  onOpenSession?: (sid: string) => void
}) {
  const rows = plan.tasks.map((task) => ({
    task,
    session: task.task_id ? sessions.find((item) => item.bound_task_id === task.task_id) : undefined,
  }))
  const bound = rows.filter((row) => row.session)
  const running = bound.filter((row) => row.session?.worker_running).length
  return <section className="coord-team-roster">
    <div className="coord-section-head">
      <span><Users size={13} /> 当前计划团队编制</span>
      <span className="coord-hint">按计划节点展示，不合并同角色任务</span>
    </div>
    <div className="coord-roster-summary">
      <Metric label="成员任务" value={rows.length} />
      <Metric label="已绑定会话" value={bound.length} accent="active" />
      <Metric label="运行中" value={running} accent="active" />
      <Metric label="待派发" value={rows.length - bound.length} accent="warn" />
    </div>
    {!rows.length ? <div className="coord-empty">当前计划还没有团队任务</div> : <div className="coord-roster-list">{rows.map(({ task, session }) => (
      <div className="coord-roster-row" key={task.id}>
        <span className={cn("coord-live-dot", session?.worker_running ? "is-running" : session?.worker_armed ? "is-armed" : "is-idle")} />
        <div className="coord-roster-main">
          <b title={task.title}>{task.title}</b>
          <small>{task.role || "待分配角色"} · {STATUS_LABEL[task.status]} · {session ? (session.name || session.id) : "尚未绑定会话"}</small>
        </div>
        <span className={cn("coord-roster-state", session ? "is-bound" : "is-pending")}>{session ? (session.worker_running ? "运行中" : session.worker_armed ? "待命" : "空闲") : "待派发"}</span>
        {session && <button type="button" className="coord-roster-open" onClick={() => onOpenSession?.(session.id)}>打开</button>}
      </div>
    ))}</div>}
  </section>
}

function CoordinationCockpit({
  plan, sessions, sessionGraph, events, selectedTask, onSelectTask, onOpenSession,
}: {
  plan: CoordinationOverview["plans"][number] | null
  sessions: Session[]
  sessionGraph: SessionGraph | null
  events: BBEvent[]
  selectedTask: CoordinationTask | null
  onSelectTask: (id: string) => void
  onOpenSession?: (sid: string) => void
}) {
  const tasks = plan?.tasks ?? []
  const messages = events.filter((event) => event.kind === "message.inbox")
    .filter((event) => String(event.payload.kind ?? "").includes("message") || event.payload.kind === "human_note")
    .slice(-8).reverse()
  const activity = events.filter((event) => event.kind !== "message.inbox").slice(-8).reverse()
  const activeSessions = sessions.filter((session) => session.status !== "closed")
  const edgeCount = sessionGraph?.edges.length ?? 0
  return <section className="coord-cockpit">
    <div className="coord-cockpit-head">
      <div><div className="coord-eyebrow"><Radio size={12} /> LIVE CONTROL ROOM</div><h2>协调驾驶舱</h2><p>任务进度、会话状态和协作消息实时汇总。</p></div>
      <div className="coord-cockpit-stat"><Users size={14} /><b>{activeSessions.length}</b><span>活跃会话</span><i>{edgeCount} 条协作链路</i></div>
    </div>
    <div className="coord-cockpit-grid">
      <div className="coord-panel coord-session-panel">
        <div className="coord-panel-title"><span><Users size={13} /> 会话状态</span><small>{sessions.length} 个窗口</small></div>
        {!activeSessions.length ? <div className="coord-panel-empty">暂无运行中的会话</div> : <div className="coord-session-list">{activeSessions.map((session) => <button type="button" key={session.id}
          className={cn("coord-session-row w-full text-left", onOpenSession && "hover:bg-accent/40")}
          onClick={() => onOpenSession?.(session.id)}>
          <span className={cn("coord-live-dot", session.worker_running ? "is-running" : session.worker_armed ? "is-armed" : "is-idle")} />
          <div><b>{session.name || session.role || "未命名会话"}</b><small>{session.role || "通用智能体"} · {session.status}{session.bound_task_id ? ` · ${session.bound_task_id.slice(0, 14)}` : ""}</small></div>
          {!!session.unread && <em>{session.unread}</em>}
        </button>)}</div>}
      </div>
      <div className="coord-panel coord-lane-panel">
        <div className="coord-panel-title"><span><GitBranch size={13} /> 任务进度泳道</span><small>{tasks.length} 个任务</small></div>
        {!tasks.length ? <div className="coord-panel-empty">选择计划后显示任务</div> : <div className="coord-lanes">{(["pending", "ready", "running", "blocked", "failed", "completed"] as CoordinationTaskStatus[]).map((status) => <div className="coord-lane" key={status}>
          <div className="coord-lane-label"><span className={`is-${status}`} />{STATUS_LABEL[status]}<b>{tasks.filter((task) => task.status === status).length}</b></div>
          <div className="coord-lane-items">{tasks.filter((task) => task.status === status).map((task) => <button key={task.id} className={cn("coord-lane-task", selectedTask?.id === task.id && "is-selected")} onClick={() => onSelectTask(task.id)}><b>{task.title}</b><small>{task.role || "待分配"}{task.depends_on.length ? ` · 依赖 ${task.depends_on.length}` : ""}</small></button>)}</div>
        </div>)}</div>}
      </div>
      <div className="coord-panel coord-feed-panel">
        <div className="coord-panel-title"><span><MessageCircle size={13} /> 私信与系统事件</span><small>最近 {messages.length + activity.length} 条</small></div>
        <div className="coord-feed-tabs"><span className="is-active">全部</span><span>私信 {messages.length}</span><span>事件 {activity.length}</span></div>
        <div className="coord-feed-list">{[...messages, ...activity].sort((a, b) => b.id - a.id).slice(0, 8).map((event) => <div className="coord-feed-item" key={`${event.kind}-${event.id}`}>
          <span className={cn("coord-feed-icon", event.kind === "message.inbox" ? "is-message" : "is-event")}>{event.kind === "message.inbox" ? <MessageCircle size={12} /> : <Activity size={12} />}</span>
          <div><b>{event.kind === "message.inbox" ? String(event.payload.subkind || event.payload.kind || "协作消息") : event.kind}</b><small>{String(event.payload.text || event.payload.title || event.payload.summary || "状态已更新")}</small><time>{event.created_at.replace("T", " ").slice(0, 19)}</time></div>
        </div>)}{!messages.length && !activity.length && <div className="coord-panel-empty">暂无协作消息</div>}</div>
      </div>
    </div>
    {selectedTask && <div className="coord-focus-bar"><Clock3 size={13} /><b>当前关注：</b><span>{selectedTask.title}</span><small>{STATUS_LABEL[selectedTask.status]} · {selectedTask.verification?.status ? `验证 ${selectedTask.verification.status}` : "尚未验证"}</small></div>}
  </section>
}

