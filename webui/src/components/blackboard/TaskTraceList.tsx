import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { TaskTrace, TraceStep } from "@/lib/types"
import { cn } from "@/lib/utils"
import { fmtTime, utcTitle } from "@/lib/datetime"

// 执行轨迹（execution-trace-chain M1，2026-09-22，DESIGN §12）：任务详情内嵌时间链
// ——R1 会话任务区间切分 + R2 过程聚合（查询时现算，零写入）。🧠 技能 / 📖 知识库 /
// ⚙ 工具组 / 📌 产出；区间外活动（游离段）折叠展示。任务流详情浮卡与任务看板共用。

const STEP_ICON: Record<string, string> = {
  skill: "🧠", kb: "📖", tools: "⚙", finding: "📌",
}

function StepLine({ s }: { s: TraceStep }) {
  const head = (
    <span className="shrink-0" title={utcTitle(s.ts)}>{STEP_ICON[s.kind] ?? "·"}</span>
  )
  if (s.kind === "skill") {
    return (
      <div className="flex min-w-0 items-baseline gap-1.5">
        {head}
        {s.hit ? (
          <span className="min-w-0 truncate">
            技能命中 <span className="font-medium">{s.name}</span>
            {s.score != null && <span className="text-muted-foreground">（score {s.score}）</span>}
          </span>
        ) : (
          <span className="min-w-0 truncate text-muted-foreground">未命中技能（反例）</span>
        )}
      </div>
    )
  }
  if (s.kind === "kb") {
    return (
      <div className="flex min-w-0 items-baseline gap-1.5">
        {head}
        <span className="min-w-0 truncate font-mono text-[11px]">{s.module}</span>
        {(s.count ?? 1) > 1 && <span className="shrink-0 text-muted-foreground">×{s.count}</span>}
      </div>
    )
  }
  if (s.kind === "tools") {
    const stat = (s.ok || s.fail)
      ? `（ok ${s.ok}/fail ${s.fail}）`
      : ""
    return (
      <div className="flex min-w-0 items-baseline gap-1.5">
        {head}
        <span className="min-w-0 truncate">
          {s.cmds?.[0] ?? s.name}{(s.count ?? 1) > 1 && <span className="text-muted-foreground"> ×{s.count}</span>}
          <span className="text-muted-foreground">{stat}</span>
        </span>
      </div>
    )
  }
  // finding：产出节点（服务端已富化标题/状态）
  return (
    <div className="flex min-w-0 items-baseline gap-1.5">
      {head}
      <span className="min-w-0 truncate font-medium">{s.title || s.vuln_class || s.finding_id}</span>
      {s.severity && <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{s.severity}</span>}
      {s.status === "verified" && (
        <span className="shrink-0 text-[10px] text-(--status-ok)">✓ verified</span>
      )}
    </div>
  )
}

export function TaskTraceList({ pid, taskId, className }: {
  pid: string; taskId: string; className?: string
}) {
  const [trace, setTrace] = useState<TaskTrace | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setTrace(null)
    setError(null)
    api.taskTrace(pid, taskId).then((t) => {
      if (alive) setTrace(t)
    }).catch((e) => {
      if (alive) setError(e instanceof Error ? e.message : String(e))
    })
    return () => { alive = false }
  }, [pid, taskId])

  if (error) return <p className={cn("text-[11px] text-(--status-error)", className)}>{error}</p>
  if (!trace) return <p className={cn("text-[11px] text-muted-foreground", className)}>轨迹计算中…</p>

  const hasIdle = trace.idle.length > 0
  return (
    <div className={cn("space-y-1", className)}>
      {trace.steps.length === 0 && !hasIdle && (
        <p className="text-[11px] text-muted-foreground">任务区间内暂无过程事件（未被认领或尚未动手）</p>
      )}
      <ol className="space-y-0.5">
        {trace.steps.map((s, i) => (
          <li key={`${s.ts}-${i}`} className="flex min-w-0 items-baseline gap-1 text-[11px] leading-5">
            <span className="w-11 shrink-0 font-mono text-[10px] text-muted-foreground"
                  title={utcTitle(s.ts)}>
              {fmtTime(s.ts)}
            </span>
            <StepLine s={s} />
          </li>
        ))}
      </ol>
      {hasIdle && (
        <details className="text-[11px] text-muted-foreground">
          <summary className="cursor-pointer select-none hover:text-foreground">
            未挂任务活动 {trace.idle.length} 步
          </summary>
          <ol className="mt-0.5 space-y-0.5">
            {trace.idle.map((s, i) => (
              <li key={`idle-${i}`} className="flex min-w-0 items-baseline gap-1 leading-5">
                <span className="w-11 shrink-0 font-mono text-[10px]" title={utcTitle(s.ts)}>
                  {fmtTime(s.ts)}
                </span>
                <StepLine s={s} />
              </li>
            ))}
          </ol>
        </details>
      )}
      {trace.truncated && (
        <p className="text-[10px] text-muted-foreground">（步数超上限，仅显示最早 {trace.steps.length} 步）</p>
      )}
    </div>
  )
}
