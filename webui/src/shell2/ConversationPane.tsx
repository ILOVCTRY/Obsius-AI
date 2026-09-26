import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { EventRow, type StreamItem } from "@/views/live/EventRow"
import { OrchChatPane } from "@/views/live/OrchChatPane"
import { GoalEditor, PersonaEditor } from "@/views/live/editors"
import { useEvents } from "@/lib/useEvents"
import { buildStreamItems } from "@/lib/turnStream"
import { eventStyle } from "@/lib/events"
import { sessionLabel } from "@/lib/roles"
import { parseTs } from "@/lib/datetime"
import { api } from "@/lib/api"
import type {
  Approval, Asset, BBEvent, OrchPersona, OrchProposal, PhaseGoal, Session, Task,
} from "@/lib/types"
import type { SessionStatus } from "@/components/StatusDot"
import { cn } from "@/lib/utils"
import { ConversationComposer } from "./ConversationComposer"
import { ApprovalCard, ApprovalAuditChip } from "./ApprovalCard"
import { RoleSwitchButton } from "./RoleSwitchButton"

// 对话工作台主窗（webui-trae-shell M2，拍板 #9）：一个项目一个常驻 pane——
// 侧栏编排器首行（sid=""）：OrchChatPane（goal 条+编排对话）+ 折叠「运行记录」；
// 侧栏任务行（sid=sess-…）：该会话气泡流（人右 / Agent MD 左 / ⚙过程折叠）。
// pending 审批按归属会话 + created_at 时间位注入（后端无 approval.requested 事件）。
// 事件装配/发送语义与 LiveRoom 共用 lib/turnStream、live/editors，行为对齐。

const PAGE = 50
const MAX_DOM = 300
// 已裁决审批卡在回执态额外保留时长（之后靠审计行 approval.* 留痕）
const EXTRA_TTL = 120_000

// 编排任务生命周期事件（与 LiveRoom 同集；带认领会话 session_id，按
// payload.created_by="orchestrator" 关联进编排视角）
const ORCH_TASK_EVENTS = new Set([
  "task.published", "task.claimed", "task.done", "task.failed",
  "task.lease_expired", "task.cancelled",
])

/** 纯事件推导会话状态（口径同 LiveRoom sessionStatus） */
function deriveStatus(mine: BBEvent[]): SessionStatus {
  if (mine.some((e) => e.kind === "session.finished")) return "finished"
  for (let i = mine.length - 1; i >= 0; i--) {
    const k = mine[i].kind
    if (k === "session.paused") return "paused"
    if (k === "session.resumed" || k === "session.aborted" || k === "session.finished") break
  }
  const last = mine[mine.length - 1]
  if (!last) return "idle"
  if (last.kind === "task.failed" || last.kind === "task.lease_expired") return "error"
  const age = Date.now() - parseTs(last.created_at).getTime()
  return age < 2 * 60 * 1000 ? "running" : "idle"
}

export interface ConversationTarget {
  /** "" = 编排器对话 */
  sid: string
  n: number
}

export function ConversationPane({ pid, focusTarget }: {
  pid: string
  focusTarget?: ConversationTarget | null
}) {
  const [curSid, setCurSid] = useState("")
  const isOrch = curSid === ""

  // 事件源：编排态走全局源，会话态走会话源（同 LiveRoom dataSid 规则）
  const dataSid = isOrch ? null : curSid
  const { events, connected, loadedAll, loadingEarlier, loadEarlier, trimDom } = useEvents(pid, dataSid)

  const [sessions, setSessions] = useState<Session[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [roles, setRoles] = useState<{ role: string; name: string }[]>([])
  const [assets, setAssets] = useState<Asset[]>([])
  const [overrides, setOverrides] = useState<Map<number, boolean>>(new Map())

  // pending 审批 + 本窗裁决后的回执态留存
  const [pending, setPending] = useState<Approval[]>([])
  const [extras, setExtras] = useState<Record<string, { approval: Approval; at: number }>>({})

  // 编排器元数据 / 运行记录折叠 / 编辑弹层
  const [orchMeta, setOrchMeta] = useState<{ phase_goal: PhaseGoal | null; persona: OrchPersona | null } | null>(null)
  const [showRunLog, setShowRunLog] = useState(false)
  const [goalOpen, setGoalOpen] = useState(false)
  const [personaOpen, setPersonaOpen] = useState(false)
  const [orchBusy, setOrchBusy] = useState(false)

  // L0 提案采纳态（内存；同 LiveRoom）
  const [adopted, setAdopted] = useState<Set<number>>(new Set())
  const [adoptingId, setAdoptingId] = useState<number | null>(null)

  // 外部 focus：侧栏点击/双击跳窗
  useEffect(() => {
    if (focusTarget?.n) setCurSid(focusTarget.sid)
  }, [focusTarget?.n]) // eslint-disable-line react-hooks/exhaustive-deps

  const refreshSessions = useCallback(() => {
    api.sessions(pid).then(setSessions).catch(() => setSessions([]))
  }, [pid])
  useEffect(() => {
    refreshSessions()
    const t = setInterval(refreshSessions, 5000)
    return () => clearInterval(t)
  }, [refreshSessions])
  useEffect(refreshSessions, [events.length, refreshSessions]) // 开窗事件后刷新

  useEffect(() => {
    api.tasks(pid).then(setTasks).catch(() => {})
    const t = setInterval(() => api.tasks(pid).then(setTasks).catch(() => {}), 5000)
    return () => clearInterval(t)
  }, [pid])

  useEffect(() => {
    api.listRoles(pid)
      .then((rs) => setRoles(rs.map((r) => ({ role: r.role, name: r.name || r.role }))))
      .catch(() => setRoles([]))
  }, [pid])

  const roleNames = useMemo(
    () => Object.fromEntries(roles.map((r) => [r.role, r.name])), [roles])

  // pending 审批轮询（后端 request_approval 不发事件，只能拉）
  const refreshPending = useCallback(() => {
    api.approvals(pid, "pending").then(setPending).catch(() => {})
  }, [pid])
  useEffect(() => {
    refreshPending()
    const t = setInterval(refreshPending, 4000)
    return () => clearInterval(t)
  }, [refreshPending])

  // 全量审批慢轮询：裁决后的审批（pending 列表已无）渲染为紧凑审计 chip，
  // 按归属会话 + created_at 注入对话流（审批裁决审计事件无 sid，会话态事件流
  // 带 session_id 服务端过滤拿不到，故直接以审批行为据）。
  const [allAppr, setAllAppr] = useState<Approval[]>([])
  useEffect(() => {
    const refresh = () => api.approvals(pid).then(setAllAppr).catch(() => {})
    refresh()
    const t = setInterval(refresh, 20_000)
    return () => clearInterval(t)
  }, [pid])

  // extras 过期清理
  useEffect(() => {
    const t = setInterval(() => setExtras((m) => {
      const now = Date.now()
      let changed = false
      const next: typeof m = {}
      for (const [k, v] of Object.entries(m)) {
        if (now - v.at > EXTRA_TTL) { changed = true; continue }
        next[k] = v
      }
      return changed ? next : m
    }), 30_000)
    return () => clearInterval(t)
  }, [])

  const keepExtra = useCallback((a: Approval) => {
    setExtras((m) => ({ ...m, [a.id]: { approval: a, at: Date.now() } }))
    // 裁决后稍延再刷 pending：让处理器先跑完，回执态卡片能接住
    setTimeout(refreshPending, 1500)
    // 同步全量审批：裁决审计行靠归属集注入本会话
    setTimeout(() => api.approvals(pid).then(setAllAppr).catch(() => {}), 1500)
  }, [refreshPending, pid])

  // 编排元数据
  const refreshOrchMeta = useCallback(() => {
    api.projectGoal(pid).then(setOrchMeta).catch(() => {})
  }, [pid])
  useEffect(() => {
    refreshOrchMeta()
    const t = setInterval(refreshOrchMeta, 10_000)
    return () => clearInterval(t)
  }, [refreshOrchMeta])

  // 资产 id→value 反查（全项目维度）
  useEffect(() => {
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

  // 切会话即读收件箱（即时清红点 + 落已读）
  useEffect(() => {
    if (isOrch) return
    setSessions((ss) => ss.map((s) => (s.id === curSid && s.unread ? { ...s, unread: 0 } : s)))
    api.readSessionInbox(curSid).catch(() => {})
  }, [curSid, isOrch])

  // ── 事件圈定与装配 ─────────────────────────────────────────────
  const visible = useMemo(() => {
    if (isOrch) {
      // 同 LiveRoom __orch 口径：编排任务生命周期按 created_by 关联 + 无 session_id
      // 的编排动作 + orchestrator/planner 源 llm.error
      return events.filter((e) => {
        if (ORCH_TASK_EVENTS.has(e.kind)) return e.payload.created_by === "orchestrator"
        if (e.session_id) return false
        if (e.author === "orchestrator") return true
        return e.kind === "llm.error"
          && (e.payload.source === "orchestrator" || e.payload.source === "planner")
      })
    }
    return events.filter((e) => e.session_id === curSid)
  }, [events, isOrch, curSid])

  const items = useMemo<StreamItem[]>(() => buildStreamItems(visible), [visible])
  // column-reverse：最新 = 首子 = 视觉最底
  const shown = useMemo(() => [...items].reverse(), [items])
  const orchShown = useMemo(
    () => [...items.filter((it) => it.type !== "single" || it.event.kind !== "orch.chat")].reverse(),
    [items])

  // ── 滚动：上翻分页 / 铺满视口 / 滑动窗口裁剪 ───────────────────
  const listRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)
  const onListScroll = useCallback(() => {
    const el = listRef.current
    if (!el) return
    const distTop = el.scrollHeight - el.clientHeight - Math.abs(el.scrollTop)
    if (distTop < 200) void loadEarlier()
    atBottomRef.current = Math.abs(el.scrollTop) < 200
  }, [loadEarlier])
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = 0
  }, [curSid])
  useEffect(() => {
    const el = listRef.current
    if (!el || loadedAll || loadingEarlier) return
    if (el.scrollHeight > el.clientHeight + 40) return
    void loadEarlier()
  }, [events, loadedAll, loadingEarlier, loadEarlier])
  useEffect(() => {
    if (!atBottomRef.current) return
    trimDom(MAX_DOM, PAGE)
  }, [events, trimDom])

  // ── EventRow 回调（稳定身份保 memo）────────────────────────────
  const toggleRow = useCallback((id: number, defaultValue: boolean) =>
    setOverrides((prev) => new Map(prev).set(id, !(prev.get(id) ?? defaultValue))), [])

  const handleRouteJump = useCallback((e: BBEvent, name: string) => {
    window.dispatchEvent(new CustomEvent("goto-settings", {
      detail: {
        tab: "skills",
        skill: {
          source: e.payload.kind === "track" ? "track" : "cap",
          pack: String(e.payload.pack ?? ""), name,
        },
      },
    }))
  }, [])

  // L0 提案采纳（口径同 LiveRoom adoptProposal：走人类写口，created_by=human）
  const adoptProposal = useCallback(async (ev: BBEvent) => {
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
          scope: str(a.scope), task_type: str(a.task_type),
          noise_budget: str(a.noise_budget),
          priority: typeof a.priority === "number" ? a.priority : undefined,
          conflict_keys: strs(a.conflict_keys), refs: strs(a.refs),
          parent_id: str(a.parent_id),
        })
      } else if (p.op === "spawn_session") {
        await api.spawnAgent(pid, String(p.args.role ?? ""))
        refreshSessions()
      } else if (p.op === "cancel_task") {
        await api.cancelTask(String(p.args.task_id ?? ""), String(p.args.reason ?? ""))
      } else if (p.op === "requeue_task") {
        await api.reopenTask(String(p.args.task_id ?? ""))
      }
      setAdopted((prev) => new Set(prev).add(ev.id))
    } finally {
      setAdoptingId(null)
    }
  }, [pid, adopted, adoptingId, refreshSessions])

  const renderItem = useCallback((item: StreamItem) => {
    const primary = item.type === "pair" ? item.command
      : item.type === "turn" ? item.note : item.event
    const primaryKind = item.type === "pair" ? "command"
      : item.type === "turn" ? "message.inbox" : item.event.kind
    const style = eventStyle(primaryKind, primary.payload)
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
            {adopted.has(item.event.id)
              ? "✓ 已采纳"
              : adoptingId === item.event.id ? "采纳中…" : "采纳"}
          </button>
        ) : undefined}
        onRouteJump={handleRouteJump}
        assetName={assetName}
      />
    )
  }, [overrides, toggleRow, roleNames, adopted, adoptingId, adoptProposal, handleRouteJump, assetName])

  // 审批（待裁全卡 + 裁决后紧凑 chip）按归属会话 + created_at 时间位与气泡流合并
  const mergedRows = useMemo(() => {
    const cards = [
      ...pending.filter((a) => a.session_id === curSid),
      ...Object.values(extras)
        .map((x) => x.approval)
        .filter((a) => a.session_id === curSid && !pending.some((p) => p.id === a.id)),
    ]
    const cardIds = new Set(cards.map((a) => a.id))
    const chips = allAppr.filter(
      (a) => a.session_id === curSid && a.status !== "pending" && !cardIds.has(a.id))

    type Entry = { at: number; node: ReactNode }
    const entries: Entry[] = [
      ...cards.map((a) => ({
        at: parseTs(a.created_at).getTime(),
        node: <div key={`appr-${a.id}`} className="shrink-0 px-1 py-1">
          <ApprovalCard approval={a} roleNames={roleNames} onDecided={keepExtra} />
        </div>,
      })),
      ...chips.map((a) => ({
        at: parseTs(a.created_at).getTime(),
        node: <div key={`appr-chip-${a.id}`} className="shrink-0 px-1 py-0.5">
          <ApprovalAuditChip approval={a} roleNames={roleNames} />
        </div>,
      })),
    ].sort((x, y) => (x.at < y.at ? -1 : x.at > y.at ? 1 : 0))

    const rows: ReactNode[] = []
    let ei = entries.length - 1
    for (const item of shown) {
      const primary = item.type === "pair" ? item.command
        : item.type === "turn" ? item.note : item.event
      const t = parseTs(primary.created_at).getTime()
      while (ei >= 0 && entries[ei].at >= t) { rows.push(entries[ei].node); ei-- }
      rows.push(<div key={primary.id} className="shrink-0">{renderItem(item)}</div>)
    }
    while (ei >= 0) { rows.push(entries[ei].node); ei-- }
    return rows
  }, [shown, pending, extras, allAppr, curSid, roleNames, keepExtra, renderItem])

  // ── 会话态派生（composer 入参）─────────────────────────────────
  const sessionsById = useMemo(() => new Map(sessions.map((s) => [s.id, s])), [sessions])
  const sessionEvents = useMemo(() => {
    const m = new Map<string, BBEvent[]>()
    for (const e of events) {
      if (!e.session_id) continue
      const arr = m.get(e.session_id)
      if (arr) arr.push(e); else m.set(e.session_id, [e])
    }
    return m
  }, [events])
  const status: SessionStatus | null = useMemo(() => {
    if (isOrch) return null
    const s = sessionsById.get(curSid)
    // 暂停稳定事实最优先（DB 状态不受事件窗口截断影响）
    if (s?.status === "paused") return "paused"
    const st = deriveStatus(sessionEvents.get(curSid) ?? [])
    // worker job 在跑（服务端 JobRegistry）→ 恒亮，不受窗口截断/2 分钟阈值影响
    if (s?.worker_running) return "running"
    // armed 待命盖过 idle/finished
    if (s?.worker_armed && (st === "idle" || st === "finished")) return "armed"
    return st
  }, [isOrch, curSid, sessionsById, sessionEvents])

  const activeSession = isOrch ? null : sessionsById.get(curSid) ?? null
  const activeTask = activeSession
    ? (tasks.find((t) => t.id === activeSession.bound_task_id)
       ?? tasks.find((t) => t.target_session === activeSession.id) ?? null)
    : null
  const sessionName = activeSession ? sessionLabel(activeSession, roleNames) : ""

  // task.claimed 到达 bump（composer 清排队条；游标只认新事件）
  const [claimedBump, setClaimedBump] = useState(0)
  const claimCursor = useRef(0)
  useEffect(() => {
    let maxId = claimCursor.current
    let hit = false
    for (const e of events) {
      if (e.id <= claimCursor.current) continue
      maxId = Math.max(maxId, e.id)
      if (e.kind !== "task.claimed") continue
      const sid = e.session_id ?? String((e.payload as { claimed_by?: string } | null)?.claimed_by ?? "")
      if (sid === curSid) hit = true
    }
    claimCursor.current = maxId
    if (hit) setClaimedBump((n) => n + 1)
  }, [events, curSid])

  // orch 对话轮 busy：发出后置忙，新 orch 回复事件到达解忙（45s 兜底）
  const orchReplyCursor = useRef(0)
  useEffect(() => {
    if (!orchBusy) return
    for (const e of events) {
      if (e.id <= orchReplyCursor.current) continue
      if (e.kind === "orch.chat"
          && (e.payload as { role?: unknown } | null)?.role !== "human") {
        setOrchBusy(false)
        break
      }
    }
  }, [events, orchBusy])

  // ── 写动作 ─────────────────────────────────────────────────────
  const onNote = useCallback((text: string, attIds: string[]) =>
    api.sessionNote(curSid, text, attIds), [curSid])
  const onOrchChat = useCallback(async (text: string) => {
    const r = await api.orchChat(pid, text)
    setOrchBusy(true)
    orchReplyCursor.current = events.reduce((m, e) => Math.max(m, e.id), 0)
    setTimeout(() => setOrchBusy(false), 45_000)
    return r
  }, [pid, events])
  const onAbort = useCallback(() => {
    api.abortSession(curSid).catch(() => {})
  }, [curSid])
  const onResume = useCallback(() => {
    api.resumeSession(curSid).then(refreshSessions).catch(() => {})
  }, [curSid, refreshSessions])
  const onRunWork = useCallback(() => {
    api.agentWork(curSid).then(() => setTimeout(refreshSessions, 800)).catch(() => {})
  }, [curSid, refreshSessions])

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      {/* 顶部细状态条 */}
      <div className="flex h-8 shrink-0 items-center gap-2 border-b px-3 text-[11px] text-muted-foreground">
        <span className="min-w-0 flex-1 truncate">
          {isOrch
            ? (orchMeta?.persona?.display_name || "编排器")
            : sessionName}
        </span>
        {!isOrch && activeSession && (
          <RoleSwitchButton
            sid={curSid}
            roles={roles.map((r) => ({ role: r.role, name: r.name }))}
            currentRole={activeSession.role}
            onChanged={refreshSessions}
          />
        )}
        <span className={cn("shrink-0 font-mono", connected ? "text-primary" : "text-(--status-error)")}>
          {connected ? "● live" : "○ 重连中"}
        </span>
      </div>

      {isOrch ? (
        <>
          <div className="flex min-h-0 flex-1 flex-col">
            <OrchChatPane
              events={events}
              busy={orchBusy}
              persona={orchMeta?.persona ?? null}
              goal={orchMeta?.phase_goal ?? null}
              onEditGoal={() => setGoalOpen(true)}
              onEditPersona={() => setPersonaOpen(true)}
            />
            <div className="shrink-0 border-t">
              <button type="button" onClick={() => setShowRunLog((v) => !v)}
                className="flex w-full items-center gap-1 px-3 py-1.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground">
                {showRunLog ? "▾" : "▸"} 运行记录（任务派发 / 编排动作；对话在上方流里）
              </button>
              {showRunLog && (
                <div ref={listRef} onScroll={onListScroll} className="h-64 overflow-y-auto px-3">
                  {orchShown.map((item) => {
                    const primary = item.type === "pair" ? item.command
                      : item.type === "turn" ? item.note : item.event
                    return <div key={primary.id}>{renderItem(item)}</div>
                  })}
                </div>
              )}
            </div>
          </div>
        </>
      ) : (
        <div
          ref={listRef}
          onScroll={onListScroll}
          className="flex min-h-0 flex-1 flex-col-reverse overflow-auto px-3 py-2"
        >
          {mergedRows}
          {!loadedAll ? (
            <button type="button" onClick={() => void loadEarlier()} disabled={loadingEarlier}
              className="shrink-0 rounded border bg-card px-2 py-1 text-center text-xs text-muted-foreground hover:bg-accent disabled:opacity-50">
              {loadingEarlier ? "加载中…" : "↑ 加载更早"}
            </button>
          ) : events.length > 0 ? (
            <span className="shrink-0 py-1 text-center text-[10px] text-muted-foreground">已到最早</span>
          ) : null}
        </div>
      )}

      <ConversationComposer
        pid={pid} sid={curSid} isOrch={isOrch}
        sessionName={sessionName}
        status={status}
        claimedBump={claimedBump}
        task={isOrch ? null : activeTask}
        onTaskChanged={() => api.tasks(pid).then(setTasks).catch(() => {})}
        onNote={onNote}
        onOrchChat={onOrchChat}
        onAbort={onAbort} onResume={onResume} onRunWork={onRunWork}
      />

      {/* 目标 / 身份编辑弹层 */}
      {goalOpen && (
        <div className="absolute bottom-16 left-3 z-20">
          <GoalEditor
            initial={orchMeta?.phase_goal ?? null}
            onClose={() => setGoalOpen(false)}
            onSave={async (body) => { await api.setGoal(pid, body); refreshOrchMeta() }}
            onClear={async () => { await api.setGoal(pid, { text: "" }); refreshOrchMeta() }}
          />
        </div>
      )}
      {personaOpen && (
        <div className="absolute bottom-16 left-3 z-20">
          <PersonaEditor
            initial={orchMeta?.persona ?? null}
            onClose={() => setPersonaOpen(false)}
            onSave={async (body) => { await api.setOrchPersona(pid, body); refreshOrchMeta() }}
          />
        </div>
      )}
    </div>
  )
}
