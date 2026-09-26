import { useCallback, useEffect, useMemo, useState } from "react"
import { Plus } from "lucide-react"
import { api } from "@/lib/api"
import type { Approval, Session, Task } from "@/lib/types"
import { cn } from "@/lib/utils"
import { ApprovalCard } from "./ApprovalCard"
import { RoleSwitchButton } from "./RoleSwitchButton"

// 会话看板（会话中心化 M4，2026-09-25；取代旧壳 TaskBoard）：
// 列=会话状态 干活中/待审批/暂停阻塞/待命/收尾；卡=会话窗——身份 + 当前委托
// + 窗内队列数 + 产出件数；卡内就地换人、跑队列、裁决审批。closed 窗落收尾列暗色。

type ColKey = "running" | "approval" | "paused" | "idle" | "closed"

const COLUMNS: { key: ColKey; label: string; hint: string }[] = [
  { key: "running", label: "干活中", hint: "worker 正在跑委托" },
  { key: "approval", label: "待审批", hint: "等人类裁决才能继续" },
  { key: "paused", label: "暂停阻塞", hint: "软暂停/阻塞，可续跑" },
  { key: "idle", label: "待命", hint: "窗存活，随时可接新活" },
  { key: "closed", label: "收尾", hint: "已关窗，只读回看" },
]

function sessionDelegations(tasks: Task[], sid: string): {
  current: Task | null
  queue: Task[]
  done: number
} {
  let current: Task | null = null
  const queue: Task[] = []
  let done = 0
  for (const t of tasks) {
    if (t.target_session !== sid) continue
    if (t.status === "claimed") current = t
    else if (t.status === "open") queue.push(t)
    else if (t.status === "done") done++
  }
  queue.sort((a, b) => a.priority - b.priority
    || (a.created_at < b.created_at ? -1 : 1))
  return { current, queue, done }
}

function SessionCard({
  s, tasks, roleNames, roles, onOpen, onChanged,
}: {
  s: Session
  tasks: Task[]
  roleNames: Record<string, string>
  roles: { role: string; name: string }[]
  onOpen: (sid: string) => void
  onChanged: () => void
}) {
  const { current, queue, done } = sessionDelegations(tasks, s.id)
  const identity = roleNames[s.role] ?? s.role
  const isClosed = s.status === "closed"
  const [starting, setStarting] = useState(false)

  const runQueue = useCallback(() => {
    setStarting(true)
    api.agentWork(s.id)
      .catch(() => {})
      .finally(() => { setStarting(false); onChanged() })
  }, [s.id, onChanged])

  return (
    <div
      onClick={() => !isClosed && onOpen(s.id)}
      className={cn(
        "cursor-pointer rounded-lg border bg-card p-2 shadow-sm transition hover:border-primary/60",
        s.worker_running ? "border-emerald-500/40" : "border-border",
        s.status === "paused" && "border-amber-400/40",
        isClosed && "cursor-default opacity-55 hover:border-border")}
    >
      <div className="flex items-center gap-1.5">
        <span className={cn("size-2 shrink-0 rounded-full",
          s.worker_running ? "bg-emerald-400"
          : s.status === "paused" ? "bg-amber-400"
          : s.worker_armed ? "bg-sky-400"
          : "bg-muted-foreground/50")} />
        <span className="min-w-0 flex-1 truncate text-xs font-medium" title={identity}>
          {identity}
        </span>
        <span className="shrink-0 font-mono text-[9px] text-muted-foreground">
          {s.id.slice(0, 10)}
        </span>
      </div>

      {current ? (
        <p className="mt-1.5 line-clamp-2 rounded bg-accent/40 p-1 text-[11px] leading-snug"
           title={current.objective}>
          <span className="font-mono text-[9px] text-emerald-400">▶ 当前委托 · </span>
          {current.objective}
        </p>
      ) : (
        <p className="mt-1.5 text-[11px] text-muted-foreground">
          {queue.length > 0 ? `窗内队列 ${queue.length} 件` : "待命（无在窗委托）"}
        </p>
      )}
      {queue.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {queue.slice(0, 3).map((t) => (
            <li key={t.id} className="flex items-center gap-1 text-[10px] text-muted-foreground">
              <span>○</span>
              <span className="min-w-0 flex-1 truncate">{t.objective}</span>
            </li>
          ))}
          {queue.length > 3 && <li className="pl-4 text-[10px] text-muted-foreground">…另 {queue.length - 3} 件</li>}
        </ul>
      )}

      <div className="mt-1.5 flex items-center gap-2 font-mono text-[9px] text-muted-foreground">
        {done > 0 && <span title="窗内已完成委托数">✓ 产出 {done}</span>}
        {s.unread ? <span className="text-(--status-approval)">● 未读 {s.unread}</span> : null}
        <span className="flex-1" />
        {!isClosed && (
          <>
            {(s.status === "idle" || (!s.worker_running && !s.worker_armed)) && (
              <button
                type="button"
                disabled={starting}
                onClick={(e) => { e.stopPropagation(); runQueue() }}
                className="rounded-full border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
              >
                {starting ? "起跑中…" : "▶ 跑队列"}
              </button>
            )}
            <span onClick={(e) => e.stopPropagation()}>
              <RoleSwitchButton sid={s.id} roles={roles} currentRole={s.role} onChanged={onChanged} />
            </span>
          </>
        )}
      </div>
    </div>
  )
}

/** 开窗工具条：选专家 → 开新窗（armed=false 待命，不耗 LLM） */
function OpenWindowBar({ pid, roles, onOpened }: {
  pid: string
  roles: { role: string; name: string }[]
  onOpened: (sid: string) => void
}) {
  const [role, setRole] = useState("_generalist")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const open = async () => {
    setBusy(true); setErr(null)
    try {
      const s = await api.spawnAgent(pid, role)
      onOpened(s.id)
    } catch (e) {
      setErr(String(e))
    } finally { setBusy(false) }
  }

  return (
    <div className="flex items-center gap-1.5 px-3 py-2">
      <select
        value={role}
        onChange={(e) => setRole(e.target.value)}
        className="h-7 rounded border bg-background px-1.5 text-[11px] [color-scheme:dark] [&>option]:bg-popover [&>option]:text-popover-foreground"
      >
        {roles.map((r) => (
          <option key={r.role} value={r.role}>
            {r.name}（{r.role}）
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={open}
        disabled={busy}
        className="flex h-7 items-center gap-1 rounded-md bg-primary px-2 text-[11px] text-primary-foreground hover:opacity-90 disabled:opacity-50"
      >
        <Plus className="size-3.5" />
        {busy ? "开窗中…" : "开窗"}
      </button>
      {err && <span className="truncate text-[11px] text-(--status-error)" title={err}>{err}</span>}
      <span className="flex-1" />
      <span className="text-[10px] text-muted-foreground">新窗默认待命，发消息或跑队列即开工</span>
    </div>
  )
}

export function SessionBoard({ pid, onOpenSession }: {
  pid: string
  onOpenSession: (sid: string) => void
}) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [pending, setPending] = useState<Approval[]>([])
  const [roles, setRoles] = useState<{ role: string; name: string }[]>([])
  const [tick, setTick] = useState(0)

  const reload = useCallback(() => setTick((n) => n + 1), [])

  useEffect(() => {
    let alive = true
    Promise.all([
      api.sessions(pid), api.tasks(pid),
      api.approvals(pid, "pending"), api.listRoles(pid),
    ]).then(([ss, ts, ps, rs]) => {
      if (!alive) return
      setSessions(ss); setTasks(ts); setPending(ps)
      setRoles(rs.map((r) => ({ role: r.role, name: r.name || r.role })))
    }).catch(() => {})
    const t = setInterval(() => {
      Promise.all([api.sessions(pid), api.tasks(pid), api.approvals(pid, "pending")])
        .then(([ss, ts, ps]) => { if (alive) { setSessions(ss); setTasks(ts); setPending(ps) } })
        .catch(() => {})
    }, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, tick])

  const roleNames = useMemo(
    () => Object.fromEntries(roles.map((r) => [r.role, r.name])), [roles])
  const pendingBySess = useMemo(() => {
    const m = new Map<string, Approval[]>()
    for (const a of pending) {
      const k = a.session_id ?? ""
      m.set(k, [...(m.get(k) ?? []), a])
    }
    return m
  }, [pending])

  const columnOf = useCallback((s: Session): ColKey => {
    if ((pendingBySess.get(s.id) ?? []).length > 0) return "approval"
    if (s.status === "paused") return "paused"
    if (s.worker_running || s.status === "running") return "running"
    if (s.status === "closed") return "closed"
    return "idle"
  }, [pendingBySess])

  const buckets = useMemo(() => {
    const m: Record<ColKey, Session[]> = {
      running: [], approval: [], paused: [], idle: [], closed: [],
    }
    for (const s of sessions) m[columnOf(s)].push(s)
    m.closed.sort((a, b) => (b.finished_at ?? "") < (a.finished_at ?? "") ? -1 : 1)
    return m
  }, [sessions, columnOf])

  return (
    <div className="absolute inset-0 flex flex-col bg-[#0d1117]">
      <div className="shrink-0 border-b border-[#21262d]">
        <OpenWindowBar pid={pid} roles={roles} onOpened={(sid) => { reload(); onOpenSession(sid) }} />
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-2">
        <div className="grid min-h-full grid-cols-1 gap-2 md:grid-cols-3 xl:grid-cols-5">
          {COLUMNS.map((col) => (
            <div key={col.key} className="flex min-h-32 flex-col rounded-lg border border-[#21262d] bg-[#161b22]/60">
              <div className="flex items-center gap-1.5 px-2 py-1.5">
                <span className="text-xs font-medium">{col.label}</span>
                <span className="rounded-full bg-muted/30 px-1.5 font-mono text-[10px] text-muted-foreground">
                  {col.key === "approval"
                    ? pending.length
                    : buckets[col.key].length}
                </span>
                <span className="min-w-0 flex-1 truncate text-right text-[9px] text-muted-foreground" title={col.hint}>
                  {col.hint}
                </span>
              </div>
              <div className="space-y-2 px-1.5 pb-2">
                {buckets[col.key].map((s) => (
                  <div key={s.id} className="space-y-1.5">
                    <SessionCard
                      s={s} tasks={tasks} roleNames={roleNames} roles={roles}
                      onOpen={onOpenSession} onChanged={reload} />
                    {(pendingBySess.get(s.id) ?? []).map((a) => (
                      <ApprovalCard key={a.id} approval={a} roleNames={roleNames} onDecided={reload} />
                    ))}
                  </div>
                ))}
                {/* 待审批列：未归属会话的 pending（任务级审批等） */}
                {col.key === "approval" && (pendingBySess.get("") ?? []).map((a) => (
                  <ApprovalCard key={a.id} approval={a} roleNames={roleNames} onDecided={reload} />
                ))}
                {buckets[col.key].length === 0
                  && !(col.key === "approval" && (pendingBySess.get("") ?? []).length > 0) && (
                  <p className="px-1 py-2 text-center text-[10px] text-muted-foreground/60">—</p>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
