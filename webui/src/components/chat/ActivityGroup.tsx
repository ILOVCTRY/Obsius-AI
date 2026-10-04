import { useState, type ReactNode } from "react"
import { ChevronRight } from "lucide-react"
import { activityDurationMs, buildActivitySegments, formatDuration, type ActivityStep } from "@/lib/activity"
import { cn } from "@/lib/utils"

// 折叠活动摘要行（会话流改造，2026-10-03）：一轮里的连续「思考/命令/工具」折成
// 一行灰色摘要（`编辑了 2 个文件，执行了一条命令，思考`）+ 右侧耗时，点开看明细。
// 对齐 cc-haha 的 ActivityGroup：
//   · 运行中恒展开（live），结束后折叠
//   · 单步不成组——直接渲染明细，不出摘要行
//   · 展开态是组件内 state，不进父级折叠记忆（与 views/live/EventRow.tsx 的
//     TurnRow.innerOpen 同纪律，避免污染顶层 overrides）
// 样式走 Tailwind 工具类（不依赖 .wb-* CSS）——工作台与直播间共用。

export function ActivityGroup({ steps, children, live, failedCount = 0, className }: {
  steps: ActivityStep[]
  /** 展开态的明细行（由调用方提供——直播间传 EventRow 行，工作台传工具行） */
  children: ReactNode
  /** 运行中：恒展开且不显耗时 */
  live?: boolean
  failedCount?: number
  className?: string
}) {
  const [pinned, setPinned] = useState<boolean | null>(null)
  const segments = buildActivitySegments(steps)
  const durationMs = activityDurationMs(steps)
  const collapsed = pinned ?? !live

  // 单步不成组：摘要行没有信息量，直接给明细
  if (steps.length <= 1) return <>{children}</>

  const summary = segments.map((s) => s.label).join("，")
  return (
    <div className={cn("w-full shrink-0", className)}>
      <button
        type="button"
        onClick={() => setPinned(!collapsed)}
        title={collapsed ? "展开过程明细" : "折叠过程明细"}
        className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[12px] leading-[1.6] text-muted-foreground hover:bg-accent/40 hover:text-foreground/80"
      >
        <ChevronRight className={cn("size-3 shrink-0 transition-transform", !collapsed && "rotate-90")} />
        <span className="min-w-0 flex-1 truncate">{summary}</span>
        {failedCount > 0 && (
          <span className="shrink-0 font-mono text-[10px] text-(--status-error)">{failedCount} 项失败</span>
        )}
        {!live && typeof durationMs === "number" && (
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground/60">
            {formatDuration(durationMs)}
          </span>
        )}
      </button>
      {!collapsed && <div className="mt-0.5 space-y-0.5">{children}</div>}
    </div>
  )
}
