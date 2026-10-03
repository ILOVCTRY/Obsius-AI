import { useCallback, useEffect, useMemo, useState } from "react"
import { Activity, Check, ChevronRight, CircleAlert, Clock3, FileKey2, GitBranch, MessageCircle, Pause, Play, Plus, Radio, RefreshCw, ShieldAlert, Users, X } from "lucide-react"
import { api } from "@/lib/api"
import type { BBEvent, CoordinationOverview, CoordinationTask, CoordinationTaskStatus, Session, SessionGraph } from "@/lib/types"
import { cn } from "@/lib/utils"

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿", active: "运行中", paused: "已暂停", completed: "已完成",
  pending: "待规划", ready: "可执行", running: "执行中", blocked: "等待依赖",
  failed: "失败",
}
const OBJECT_LABEL: Record<string, string> = {
  function: "函数", string: "字符串", xref: "Xref", behavior: "行为", evidence: "证据", artifact: "产物",
}

export function MultiAgentCoordinationView({ pid }: { pid: string }) {
  const [data, setData] = useState<CoordinationOverview | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionGraph, setSessionGraph] = useState<SessionGraph | null>(null)
  const [events, setEvents] = useState<BBEvent[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<string | null>(null)
  const [planName, setPlanName] = useState("")
  const [objective, setObjective] = useState("")
  const [taskTitle, setTaskTitle] = useState("")
  const [taskDescription, setTaskDescription] = useState("")
  const [taskRole, setTaskRole] = useState("")
  const [depends, setDepends] = useState<string[]>([])
  const [objectKind, setObjectKind] = useState("function")
  const [objectName, setObjectName] = useState("")
  const [objectRef, setObjectRef] = useState("")
  const [objectSource, setObjectSource] = useState("")
  const [objectConfidence, setObjectConfidence] = useState("0.5")
  const [objectData, setObjectData] = useState("")
  const [artifactRefs, setArtifactRefs] = useState("")
  const [conflictLeft, setConflictLeft] = useState("")
  const [conflictRight, setConflictRight] = useState("")
  const [conflictSummary, setConflictSummary] = useState("")

  const load = useCallback(() => {
    setLoading(true)
    return Promise.allSettled([
      api.coordination(pid),
      api.sessions(pid),
      api.sessionGraph(pid),
      api.eventsTail(pid, 80),
    ]).then(([coordination, sessionRows, graph, activity]) => {
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
  const createPlan = async () => {
    if (!planName.trim()) return
    try {
      const created = await api.coordinationPlanCreate(pid, { name: planName, objective })
      setPlanName(""); setObjective(""); setSelectedId(created.id); setNotice("协调计划已创建"); await load()
    } catch { setNotice("创建计划失败") }
  }
  const addTask = async () => {
    if (!selected || !taskTitle.trim()) return
    try {
      await api.coordinationTaskCreate(pid, selected.id, {
        title: taskTitle, description: taskDescription, role: taskRole, depends_on: depends,
      })
      setTaskTitle(""); setTaskDescription(""); setTaskRole(""); setDepends([]); setNotice("任务已加入协调计划"); await load()
    } catch { setNotice("任务创建失败，请检查依赖关系") }
  }
  const setPlanStatus = async (status: string) => {
    if (!selected) return
    try { await api.coordinationPlanStatus(pid, selected.id, status); await load() } catch { setNotice("计划状态更新失败") }
  }
  const setTaskStatus = async (task: CoordinationTask, status: CoordinationTaskStatus) => {
    try { await api.coordinationTaskUpdate(pid, task.id, { status }); await load() } catch { setNotice("任务状态更新失败") }
  }
  const addObject = async () => {
    if (!objectName.trim() && !objectRef.trim()) return
    let parsed: Record<string, unknown> = {}
    if (objectData.trim()) {
      try { parsed = JSON.parse(objectData) as Record<string, unknown> } catch { setNotice("对象数据必须是 JSON") ; return }
    }
    try {
      await api.coordinationObjectCreate(pid, {
        kind: objectKind, name: objectName, object_ref: objectRef, source: objectSource,
        confidence: Number(objectConfidence), data: parsed, plan_id: selected?.id ?? null,
        artifact_refs: artifactRefs.split(/[\n,]/).map((v) => v.trim()).filter(Boolean),
      })
      setObjectName(""); setObjectRef(""); setObjectSource(""); setObjectData(""); setArtifactRefs(""); setNotice("结构化对象已登记"); await load()
    } catch { setNotice("结构化对象登记失败，请检查类型和置信度") }
  }
  const addConflict = async () => {
    if (!conflictLeft || !conflictRight || !conflictSummary.trim()) return
    try {
      await api.coordinationConflictCreate(pid, { left_object_id: conflictLeft, right_object_id: conflictRight, summary: conflictSummary })
      setConflictSummary(""); setNotice("冲突记录已创建"); await load()
    } catch { setNotice("冲突记录创建失败") }
  }
  const resolveConflict = async (id: string) => {
    try { await api.coordinationConflictUpdate(pid, id, { status: "resolved", resolution: "已由协调人员确认" }); await load() } catch { setNotice("冲突状态更新失败") }
  }
  const verifyPlan = async () => {
    if (!selected) return
    try {
      const result = await api.coordinationVerify(pid, { plan_id: selected.id })
      setNotice("status" in result && result.status === "completed"
        ? "计划验证通过，已完成"
        : `计划仍需处理：${"passed" in result ? result.passed : 0}/${"total" in result ? result.total : 0} 个任务通过`)
      await load()
    } catch { setNotice("计划验证失败") }
  }
  const verifyTask = async (task: CoordinationTask) => {
    try {
      const result = await api.coordinationVerify(pid, { task_id: task.id })
      setNotice(result.status === "passed" ? `任务「${task.title}」验证通过` : `任务「${task.title}」需要补充处理`)
      await load()
    } catch { setNotice("任务验证失败") }
  }

  return (
    <div className="coord-shell">
      <header className="coord-header">
        <div>
          <div className="coord-eyebrow"><GitBranch size={13} /> COORDINATION</div>
          <h1>多智能体协调</h1>
          <p>把分析目标拆成可依赖、可验证的协作任务，协调模块与智能体工作台相互隔离。</p>
        </div>
        <button className="coord-icon-btn" title="刷新协调数据" onClick={() => void load()}><RefreshCw size={15} /></button>
      </header>

      {notice && <div className="coord-notice"><Activity size={13} />{notice}<button onClick={() => setNotice(null)}><X size={13} /></button></div>}

      <section className="coord-metrics">
        <Metric label="协调计划" value={data?.summary.plans ?? 0} />
        <Metric label="全部任务" value={data?.summary.tasks ?? 0} />
        <Metric label="执行中" value={data?.summary.running ?? 0} accent="active" />
        <Metric label="等待处理" value={data?.summary.blocked ?? 0} accent="warn" />
        <Metric label="已完成" value={data?.summary.completed ?? 0} accent="done" />
        <Metric label="结构化对象" value={data?.summary.objects ?? 0} />
        <Metric label="开放冲突" value={data?.summary.conflicts ?? 0} accent="warn" />
      </section>

      <CoordinationCockpit
        plan={selected}
        sessions={sessions}
        sessionGraph={sessionGraph}
        events={events}
        selectedTask={selectedTask}
        onSelectTask={setSelectedTaskId}
      />

      <div className="coord-grid">
        <aside className="coord-plans">
          <div className="coord-section-head"><span>协调计划</span><span className="coord-count">{data?.plans.length ?? 0}</span></div>
          <div className="coord-plan-create">
            <input value={planName} onChange={(e) => setPlanName(e.target.value)} placeholder="新计划名称" onKeyDown={(e) => e.key === "Enter" && void createPlan()} />
            <textarea value={objective} onChange={(e) => setObjective(e.target.value)} placeholder="目标与边界（可选）" rows={2} />
            <button className="coord-primary" disabled={!planName.trim()} onClick={() => void createPlan()}><Plus size={14} />创建计划</button>
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
                  <button className="coord-secondary" onClick={() => void verifyPlan()}><ShieldAlert size={13} />验证计划</button>
                  {selected.status === "active" ? <button className="coord-secondary" onClick={() => void setPlanStatus("paused")}><Pause size={13} />暂停计划</button> : selected.status !== "completed" && <button className="coord-primary" onClick={() => void setPlanStatus("active")}><Play size={13} />启动计划</button>}
                  {selected.status === "paused" && <button className="coord-secondary" onClick={() => void setPlanStatus("draft")}><RefreshCw size={13} />回到草稿</button>}
                </div>
              </div>
              <div className="coord-task-board">
                <div className="coord-section-head"><span>任务依赖图</span><span className="coord-hint">依赖未完成的任务会自动标记为等待</span></div>
                {!selected.tasks.length && <div className="coord-empty">添加第一个任务开始构建 DAG</div>}
                <div className="coord-task-list">{selected.tasks.map((task) => (
                  <TaskCard key={task.id} task={task} all={selected.tasks} selected={selectedTaskId === task.id} onSelect={() => setSelectedTaskId(task.id)} onStatus={setTaskStatus} onVerify={verifyTask} />
                ))}</div>
              </div>
              <div className="coord-task-create">
                <div className="coord-section-head"><span>添加协调任务</span><span className="coord-hint">先定义任务，再由后续调度器分配专家</span></div>
                <div className="coord-form-grid"><input value={taskTitle} onChange={(e) => setTaskTitle(e.target.value)} placeholder="任务标题" /><input value={taskRole} onChange={(e) => setTaskRole(e.target.value)} placeholder="所需能力/专家（可选）" /></div>
                <textarea value={taskDescription} onChange={(e) => setTaskDescription(e.target.value)} placeholder="任务范围、输入产物和验收条件" rows={2} />
                {!!selected.tasks.length && <div className="coord-deps"><span>依赖：</span>{selected.tasks.map((task) => <label key={task.id}><input type="checkbox" checked={depends.includes(task.id)} onChange={(e) => setDepends((cur) => e.target.checked ? [...cur, task.id] : cur.filter((x) => x !== task.id))} />{task.title}</label>)}</div>}
                <button className="coord-primary" disabled={!taskTitle.trim()} onClick={() => void addTask()}><Plus size={14} />加入计划</button>
              </div>
              <section className="coord-knowledge">
                <div className="coord-section-head"><span><FileKey2 size={13} /> 结构化黑板</span><span className="coord-hint">对象与证据独立于聊天线程</span></div>
                <div className="coord-object-form">
                  <div className="coord-form-grid"><select value={objectKind} onChange={(e) => setObjectKind(e.target.value)} aria-label="对象类型">{Object.entries(OBJECT_LABEL).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><input value={objectName} onChange={(e) => setObjectName(e.target.value)} placeholder="对象名称" /><input value={objectRef} onChange={(e) => setObjectRef(e.target.value)} placeholder="地址 / 引用 / 唯一标识" /></div>
                  <div className="coord-form-grid"><input value={objectSource} onChange={(e) => setObjectSource(e.target.value)} placeholder="来源智能体或工具" /><input value={objectConfidence} onChange={(e) => setObjectConfidence(e.target.value)} type="number" min="0" max="1" step="0.05" placeholder="置信度 0~1" /><input value={artifactRefs} onChange={(e) => setArtifactRefs(e.target.value)} placeholder="产物引用，逗号分隔" /></div>
                  <textarea value={objectData} onChange={(e) => setObjectData(e.target.value)} placeholder={'扩展数据 JSON，例如 {"address":"0x401000","arch":"x64"}'} rows={2} />
                  <button className="coord-secondary" disabled={!objectName.trim() && !objectRef.trim()} onClick={() => void addObject()}><Plus size={13} />登记对象</button>
                </div>
                <div className="coord-object-list">{(data?.objects ?? []).slice(0, 12).map((obj) => <div className="coord-object" key={obj.id}><span className="coord-object-kind">{OBJECT_LABEL[obj.kind] ?? obj.kind}</span><div><b>{obj.name || obj.object_ref}</b><small>{obj.object_ref && obj.name ? obj.object_ref : ""} · 来源 {obj.source || "未指定"} · 置信度 {Math.round(obj.confidence * 100)}%</small>{obj.artifact_refs.length > 0 && <small>产物：{obj.artifact_refs.map((a) => a.artifact_ref).join("、")}</small>}</div></div>)}</div>
                {!!data?.objects.length && <div className="coord-conflict-form"><div className="coord-section-head"><span><ShieldAlert size={13} /> 记录对象冲突</span><span className="coord-hint">保留不同专家的相反判断</span></div><div className="coord-form-grid"><select value={conflictLeft} onChange={(e) => setConflictLeft(e.target.value)} aria-label="冲突对象一"><option value="">对象一</option>{data.objects.map((o) => <option key={o.id} value={o.id}>{OBJECT_LABEL[o.kind]} · {o.name || o.object_ref}</option>)}</select><select value={conflictRight} onChange={(e) => setConflictRight(e.target.value)} aria-label="冲突对象二"><option value="">对象二</option>{data.objects.map((o) => <option key={o.id} value={o.id}>{OBJECT_LABEL[o.kind]} · {o.name || o.object_ref}</option>)}</select></div><input value={conflictSummary} onChange={(e) => setConflictSummary(e.target.value)} placeholder="冲突说明" /><button className="coord-secondary" disabled={!conflictLeft || !conflictRight || !conflictSummary.trim()} onClick={() => void addConflict()}><ShieldAlert size={13} />登记冲突</button></div>}
                {(data?.conflicts ?? []).filter((c) => c.status === "open").map((conflict) => <div className="coord-conflict" key={conflict.id}><CircleAlert size={13} /><span>{conflict.summary}</span><button onClick={() => void resolveConflict(conflict.id)}>标记已解决</button></div>)}
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

function Metric({ label, value, accent }: { label: string; value: number; accent?: string }) {
  return <div className="coord-metric"><span>{label}</span><b className={accent ? `is-${accent}` : undefined}>{value}</b></div>
}

function CoordinationCockpit({
  plan, sessions, sessionGraph, events, selectedTask, onSelectTask,
}: {
  plan: CoordinationOverview["plans"][number] | null
  sessions: Session[]
  sessionGraph: SessionGraph | null
  events: BBEvent[]
  selectedTask: CoordinationTask | null
  onSelectTask: (id: string) => void
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
        {!activeSessions.length ? <div className="coord-panel-empty">暂无运行中的会话</div> : <div className="coord-session-list">{activeSessions.map((session) => <div className="coord-session-row" key={session.id}>
          <span className={cn("coord-live-dot", session.worker_running ? "is-running" : session.worker_armed ? "is-armed" : "is-idle")} />
          <div><b>{session.name || session.role || "未命名会话"}</b><small>{session.role || "通用智能体"} · {session.status}{session.bound_task_id ? ` · ${session.bound_task_id.slice(0, 14)}` : ""}</small></div>
          {!!session.unread && <em>{session.unread}</em>}
        </div>)}</div>}
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

function TaskCard({ task, all, selected, onSelect, onStatus, onVerify }: { task: CoordinationTask; all: CoordinationTask[]; selected: boolean; onSelect: () => void; onStatus: (task: CoordinationTask, status: CoordinationTaskStatus) => void; onVerify: (task: CoordinationTask) => void }) {
  const deps = all.filter((t) => task.depends_on.includes(t.id))
  return <article className={cn("coord-task", `is-${task.status}`, selected && "is-selected")} onClick={onSelect}>
    <div className="coord-task-status"><span className="coord-task-icon">{task.status === "completed" ? <Check size={12} /> : task.status === "blocked" ? <CircleAlert size={12} /> : <Activity size={12} />}</span><span>{STATUS_LABEL[task.status]}</span></div>
    <div className="coord-task-body"><b>{task.title}</b>{task.description && <p>{task.description}</p>}<div className="coord-task-meta"><span>{task.role || "待分配能力"}</span><span>优先级 {task.priority}</span>{deps.length > 0 && <span>依赖 {deps.map((d) => d.title).join("、")}</span>}</div></div>
    <div className="coord-task-controls"><select value={task.status} onChange={(e) => void onStatus(task, e.target.value as CoordinationTaskStatus)} aria-label="任务状态">
      {(["pending", "ready", "running", "blocked", "failed", "completed"] as CoordinationTaskStatus[]).map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
    </select>{task.status !== "completed" && <button className="coord-verify-btn" title="验证任务完成条件" onClick={() => onVerify(task)}><ShieldAlert size={12} />验证</button>}</div>
  </article>
}
