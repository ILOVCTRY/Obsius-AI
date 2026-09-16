import { useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import { fmtDateTimeMin, utcTitle } from "@/lib/datetime"
import type { Session, Task, TaskPlanStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

// 「计划」标签面板（直播间类型过滤栏特殊标签）：结构化展示任务计划步骤与进度，
// 替代事件流。数据源=GET /tasks（tasks.plan 随行返回，A2 计划闸保证认领后先写计划）；
// 3s 轮询（webui 任务轮询约定），仅在本标签激活时挂载，其余时间零开销。

// 步骤状态图标对齐后端 _render_plan（core/agent/tools.py），颜色对齐 TaskNode 四态
const STEP_ICON: Record<TaskPlanStatus, string> = { todo: "○", doing: "▶", done: "●", blocked: "■" }
const STEP_CLS: Record<TaskPlanStatus, string> = {
  todo: "text-muted-foreground",
  doing: "text-primary",
  done: "text-emerald-400/80",
  blocked: "text-amber-400",
}

const TASK_BADGE: Record<Task["status"], { label: string; cls: string }> = {
  open: { label: "待认领", cls: "border-muted-foreground/40 text-muted-foreground" },
  claimed: { label: "执行中", cls: "border-primary/60 text-primary" },
  done: { label: "已完成", cls: "border-emerald-400/50 text-emerald-400/80" },
  failed: { label: "失败", cls: "border-(--status-error)/60 text-(--status-error)" },
}

function planStats(plan: Task["plan"]) {
  return {
    done: plan.filter((s) => s.status === "done").length,
    doing: plan.filter((s) => s.status === "doing").length,
    blocked: plan.filter((s) => s.status === "blocked").length,
    total: plan.length,
  }
}

const STATUS_RANK: Record<Task["status"], number> = { claimed: 0, open: 1, failed: 2, done: 3 }

export function PlanPanel({ pid, activeTab, sessions }: {
  pid: string
  activeTab: string
  sessions: Session[]
}) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [err, setErr] = useState(false)
  useEffect(() => {
    let alive = true
    const refresh = () =>
      api.tasks(pid)
        .then((ts) => { if (alive) { setTasks(ts); setErr(false) } })
        .catch(() => { if (alive) setErr(true) })
    refresh()
    const t = setInterval(refresh, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  const sessionName = useMemo(
    () => new Map(sessions.map((s) => [s.id, s.name || s.role])), [sessions])

  const shown = useMemo(() => {
    const mine = activeTab.startsWith("sess-")
      ? tasks.filter((t) => t.claimed_by === activeTab)
      : tasks // 「全部」/「编排」页签 = 全项目任务总览
    // 有 doing/blocked 步的在前 → 任务态 → priority 高在前 → 最近更新在前
    return [...mine].sort((a, b) => {
      const pa = planStats(a.plan), pb = planStats(b.plan)
      const actA = pa.doing + pa.blocked > 0 ? 0 : 1
      const actB = pb.doing + pb.blocked > 0 ? 0 : 1
      if (actA !== actB) return actA - actB
      if (STATUS_RANK[a.status] !== STATUS_RANK[b.status]) {
        return STATUS_RANK[a.status] - STATUS_RANK[b.status]
      }
      if (a.priority !== b.priority) return b.priority - a.priority
      return a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0
    })
  }, [tasks, activeTab])

  const emptyHint = activeTab.startsWith("sess-")
    ? "当前会话暂无认领的任务（认领后经 A2 计划闸先写计划再动手）"
    : "项目暂无任务"

  return (
    <div className="min-h-0 flex-1 overflow-auto px-3 py-2">
      {err && tasks.length === 0 ? (
        <p className="py-6 text-center text-xs text-(--status-error)">任务计划加载失败</p>
      ) : shown.length === 0 ? (
        <p className="py-6 text-center text-xs text-muted-foreground">{emptyHint}</p>
      ) : (
        <div className="mx-auto flex max-w-3xl flex-col gap-2">
          {shown.map((t) => {
            const st = planStats(t.plan)
            const badge = TASK_BADGE[t.status]
            return (
              <div key={t.id} className="rounded-md border bg-card p-2.5 text-xs">
                <div className="flex items-baseline gap-2">
                  <span className={cn("shrink-0 rounded border px-1.5 py-px text-[11px] leading-tight", badge.cls)}>
                    {badge.label}
                  </span>
                  {t.status === "failed" && t.blocked_reason === "awaiting_human" && (
                    <span className="shrink-0 rounded border border-amber-400/60 px-1.5 py-px text-[11px] leading-tight text-amber-400">
                      ⏸ 待人工
                    </span>
                  )}
                  <span className="min-w-0 flex-1 truncate font-medium" title={t.objective}>
                    {t.objective}
                  </span>
                  {t.claimed_by && activeTab === "__all" && (
                    <span className="shrink-0 font-mono text-[10px] text-muted-foreground"
                          title={t.claimed_by}>
                      {sessionName.get(t.claimed_by) ?? t.claimed_by}
                    </span>
                  )}
                  {st.total > 0 && (
                    <span className="shrink-0 font-mono text-[10px] text-muted-foreground"
                          title={`P${t.priority}`}>
                      ▦ {st.done}/{st.total}
                    </span>
                  )}
                </div>
                {st.total === 0 ? (
                  <p className="mt-1.5 text-muted-foreground">
                    未制定计划（A2 计划闸：认领后先 task_plan 写计划再动手）
                  </p>
                ) : (
                  <ol className="mt-1.5 flex flex-col gap-1">
                    {t.plan.map((s) => (
                      <li key={s.id} className="flex items-start gap-1.5">
                        <span className={cn("shrink-0 font-mono", STEP_CLS[s.status])} title={s.status}>
                          {STEP_ICON[s.status]}
                        </span>
                        <span className={cn("min-w-0 flex-1",
                          s.status === "done" && "text-muted-foreground line-through decoration-muted-foreground/40")}>
                          {s.title}
                          {s.status === "blocked" && s.note && (
                            <span className="ml-1 text-amber-400">— {s.note}</span>
                          )}
                        </span>
                        <span className="shrink-0 font-mono text-[10px] text-muted-foreground"
                              title={utcTitle(s.ts)}>
                          {fmtDateTimeMin(s.ts)}
                        </span>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
