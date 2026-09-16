import { cn } from "@/lib/utils"

// 状态色语义化（DESIGN.md §12）：运行=青 / 审批=琥珀 / 异常=红 / 空闲=灰 / 暂停=青蓝
// F9：「armed」=已启动待命（worker 常驻接单，空闲），绿点区别于灰点「未启动」
export type SessionStatus = "running" | "idle" | "approval" | "error" | "finished" | "paused" | "armed"

const COLOR: Record<SessionStatus, string> = {
  running: "bg-[--status-running]",
  idle: "bg-[--status-idle]",
  approval: "bg-[--status-approval]",
  error: "bg-[--status-error]",
  finished: "bg-muted-foreground/40",
  paused: "bg-[--status-paused]",
  armed: "bg-emerald-500",
}

const TITLE: Record<SessionStatus, string> = {
  running: "运行中",
  idle: "未启动（点「跑任务队列」接单）",
  approval: "等待审批",
  error: "异常",
  finished: "已结束",
  paused: "已暂停",
  armed: "已启动·待命（任务到达即自动接单）",
}

export function StatusDot({ status, className }: { status: SessionStatus; className?: string }) {
  return (
    <span
      className={cn("inline-block h-2 w-2 shrink-0 rounded-full", COLOR[status], className)}
      title={TITLE[status]}
    />
  )
}
