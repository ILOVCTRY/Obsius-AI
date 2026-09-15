import { cn } from "@/lib/utils"

// 状态色语义化（DESIGN.md §12）：运行=青 / 审批=琥珀 / 异常=红 / 空闲=灰 / 暂停=青蓝
export type SessionStatus = "running" | "idle" | "approval" | "error" | "finished" | "paused"

const COLOR: Record<SessionStatus, string> = {
  running: "bg-[--status-running]",
  idle: "bg-[--status-idle]",
  approval: "bg-[--status-approval]",
  error: "bg-[--status-error]",
  finished: "bg-muted-foreground/40",
  paused: "bg-[--status-paused]",
}

export function StatusDot({ status, className }: { status: SessionStatus; className?: string }) {
  return (
    <span
      className={cn("inline-block h-2 w-2 shrink-0 rounded-full", COLOR[status], className)}
      title={status}
    />
  )
}
