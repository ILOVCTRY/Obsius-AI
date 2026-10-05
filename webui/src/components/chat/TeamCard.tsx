import { Users } from "lucide-react"
import type { TeamStatus } from "@/lib/types"

// 会话内团队卡（2026-10-06）：直播间「指挥」对话流里呈现 Team（core/team 域）。
// 两形态（对齐 cc-haha Agent Teams 参考图）：
//   · 待确认态（draft/ready）：`等待人工确认 · 尚未启动成员 · N 位成员` + 团队名 + 「查看并配置」
//   · 已建/运行态：头像 + 团队名（等宽）+「组建团队」徽章 + `Agent 团队 · N 名成员` + 「打开运行报告」
// 纯展示件（Tailwind，不依赖 .wb-*），数据由调用方从 `team.*` 事件 + 实时 Team 反查给出。

/** 待人工确认（尚未启动成员）的两态 */
export const isTeamPending = (status: TeamStatus) => status === "draft" || status === "ready"

export function TeamCard({ name, status, memberCount, onOpenReport, onConfigure }: {
  name: string
  status: TeamStatus
  memberCount: number
  onOpenReport?: () => void
  onConfigure?: () => void
}) {
  const pending = isTeamPending(status)

  if (pending) {
    return (
      <div className="rounded-xl border border-border/60 bg-card/60 px-3.5 py-3">
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-[11px] text-muted-foreground">
              等待人工确认 · 尚未启动成员 · {memberCount} 位成员
            </p>
            <p className="mt-0.5 truncate text-[15px] font-semibold text-foreground/90" title={name}>
              {name}
            </p>
          </div>
          <button
            type="button"
            onClick={onConfigure}
            className="shrink-0 rounded-lg border border-border/70 px-3 py-1.5 text-xs text-foreground/80 transition-colors hover:bg-accent hover:text-foreground"
          >
            查看并配置
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-border/60 bg-card/60 px-3.5 py-2.5">
      <div className="flex items-center gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-accent/40 text-muted-foreground">
          <Users className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate font-mono text-[13px] font-semibold text-foreground/90" title={name}>
              {name}
            </span>
            <span className="shrink-0 rounded-full border border-(--status-approval)/50 px-2 py-px text-[10px] text-(--status-approval)">
              组建团队
            </span>
            {status !== "running" && (
              <span className="shrink-0 text-[10px] text-muted-foreground/70">{STATUS_LABEL[status] ?? status}</span>
            )}
          </div>
          <p className="mt-0.5 text-[11px] text-muted-foreground">Agent 团队 · {memberCount} 名成员</p>
        </div>
        <button
          type="button"
          onClick={onOpenReport}
          className="shrink-0 text-xs text-(--status-approval) transition-colors hover:underline"
        >
          打开运行报告 ›
        </button>
      </div>
    </div>
  )
}

const STATUS_LABEL: Record<TeamStatus, string> = {
  draft: "草稿",
  ready: "待启动",
  starting: "启动中",
  running: "运行中",
  partial_failed: "部分失败",
  cancelling: "取消中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
}
