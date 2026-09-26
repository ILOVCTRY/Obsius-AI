import { useState, type ReactNode } from "react"
import type { ProjectMeta, Session, Task } from "@/lib/types"
import { bindingBadge } from "@/lib/taxonomy"
import { cn } from "@/lib/utils"
import type { Target } from "./types"

// 会话中心化（2026-09-25）：项目下不再按「任务行/辅助窗行」三分——编排器固定
// 首行，其下每个会话窗一行：状态字形 + 当前身份名 + 当前委托摘要（idle 显待命）；
// 点行首字形展开看窗内待做队列。closed 窗沉底暗色保留可查。

const ROW_CLS =
  "flex w-full items-center gap-1 rounded px-1.5 py-1 text-[13px] hover:bg-accent"

/** 会话状态 → 字形 + 颜色 */
function sessionMark(s: Session): { glyph: string; cls: string } {
  if (s.status === "paused")
    return { glyph: "⏸", cls: "text-amber-400" }
  if (s.worker_running)
    return { glyph: "▶", cls: "text-emerald-400" }
  if (s.status === "closed")
    return { glyph: "⊘", cls: "text-muted-foreground" }
  if (s.worker_armed)
    return { glyph: "◔", cls: "text-sky-400" }
  return { glyph: "○", cls: "text-muted-foreground" }
}

function Row({
  selected, onClick, children,
}: {
  selected: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button onClick={onClick} className={cn(ROW_CLS, selected && "bg-secondary text-primary")}>
      {children}
    </button>
  )
}

/** 该会话窗内委托：claimed=当前，open=队列；无则 null */
function sessionDelegations(tasks: Task[], sid: string): {
  current: Task | null
  queue: Task[]
} {
  let current: Task | null = null
  const queue: Task[] = []
  for (const t of tasks) {
    if (t.target_session !== sid) continue
    if (t.status === "claimed") current = t
    else if (t.status === "open") queue.push(t)
  }
  queue.sort((a, b) => a.priority - b.priority
    || (a.created_at < b.created_at ? -1 : 1))
  return { current, queue }
}

export function ProjectTree({
  projects, expanded, tasksMap, sessionsMap, rolesMap, target,
  onToggleExpanded, onSelectTarget,
}: {
  projects: ProjectMeta[]
  expanded: string[]
  tasksMap: Record<string, Task[]>
  sessionsMap: Record<string, Session[]>
  rolesMap: Record<string, Record<string, string>>
  target: Target | null
  onToggleExpanded: (pid: string) => void
  onSelectTarget: (pid: string, sid: string) => void
}) {
  // 会话行内队列展开（本地态；key=`pid/sid`）
  const [queuedOpen, setQueuedOpen] = useState<Set<string>>(new Set())
  const toggleQueue = (key: string) =>
    setQueuedOpen((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key); else next.add(key)
      return next
    })

  return (
    <div className="space-y-0.5">
      {projects.map((p) => {
        const isOpen = expanded.includes(p.id)
        const tasks = tasksMap[p.id] ?? []
        const sessions = sessionsMap[p.id] ?? []
        const roleNames = rolesMap[p.id] ?? {}
        const live = sessions.filter((s) => s.status !== "closed")
        const closed = sessions.filter((s) => s.status === "closed")
        return (
          <div key={p.id}>
            <div
              onClick={() => onToggleExpanded(p.id)}
              className="flex cursor-pointer items-center gap-1 rounded px-1 py-1 text-sm hover:bg-accent"
            >
              <span className="w-3 shrink-0 text-[9px] text-muted-foreground">
                {isOpen ? "▾" : "▸"}
              </span>
              <span className="min-w-0 flex-1 truncate font-medium">{p.name}</span>
              <span className="shrink-0 rounded bg-secondary px-1 font-mono text-[9px] text-muted-foreground">
                {bindingBadge(p.track, p.experts)}
              </span>
            </div>

            {isOpen && (
              <div className="ml-3 border-l pl-1">
                {/* 编排器：项目下固定首行 */}
                <div className="py-0.5">
                  <Row
                    selected={target?.pid === p.id && target.sid === "__orch"}
                    onClick={() => onSelectTarget(p.id, "__orch")}
                  >
                    <span className="w-4 shrink-0 text-center">🧭</span>
                    <span className="min-w-0 flex-1 truncate text-left">编排器</span>
                  </Row>
                </div>

                {[...live, ...closed].map((s) => {
                  const mark = sessionMark(s)
                  const qKey = `${p.id}/${s.id}`
                  const isQOpen = queuedOpen.has(qKey)
                  const { current, queue } = sessionDelegations(tasks, s.id)
                  const identity = roleNames[s.role] ?? s.role
                  const summary = current ? current.objective
                    : queue.length > 0 ? `队列 ${queue.length}：${queue[0].objective}`
                    : "待命"
                  return (
                    <div key={s.id} className="py-0.5">
                      <Row
                        selected={target?.pid === p.id && target.sid === s.id}
                        onClick={() => onSelectTarget(p.id, s.id)}
                      >
                        <button
                          type="button"
                          disabled={queue.length === 0}
                          onClick={(e) => {
                            e.stopPropagation()
                            toggleQueue(qKey)
                          }}
                          title={queue.length ? "展开窗内待做队列" : "窗内队列空"}
                          className={cn("w-4 shrink-0 text-center text-[11px] disabled:cursor-default",
                            mark.cls)}
                        >
                          {queue.length > 0 ? (isQOpen ? "▾" : mark.glyph) : mark.glyph}
                        </button>
                        <span className="w-16 shrink-0 truncate text-[10px] text-muted-foreground"
                              title={identity}>
                          {identity}
                        </span>
                        <span className={cn("min-w-0 flex-1 truncate text-left",
                          !current && queue.length === 0 && "text-muted-foreground/70",
                          s.status === "closed" && "text-muted-foreground/60")}>
                          {s.status === "closed" ? `已关闭（${summary}）` : summary}
                        </span>
                      </Row>
                      {isQOpen && queue.length > 0 && (
                        <div className="ml-5 space-y-0.5 border-l pl-1.5">
                          {queue.map((t) => (
                            <div key={t.id}
                                 className="flex items-center gap-1 py-0.5 text-[11px] text-muted-foreground">
                              <span className="w-3 text-center">○</span>
                              <span className="min-w-0 flex-1 truncate">{t.objective}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
