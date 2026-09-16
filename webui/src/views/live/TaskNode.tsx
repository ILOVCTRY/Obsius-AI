import { Handle, Position, type Node, type NodeProps } from "@xyflow/react"
import { Trash2 } from "lucide-react"
import type { TaskGraphNode } from "@/lib/types"
import { cn } from "@/lib/utils"
import { NODE_W, planStats } from "./flowModel"

// A3 任务流节点卡：四态色条 + 目标两行截断 + A2 计划进度 + 认领会话。
// data 用 type 不用 interface（xyflow 的 Record<string, unknown> 约束，见 webui/CLAUDE.md 坑）。

const STATUS_COLOR: Record<TaskGraphNode["status"], string> = {
  open: "#8b949e",
  claimed: "#39c5cf",
  done: "#3fb950",
  failed: "#f85149",
}

const STATUS_BORDER: Record<TaskGraphNode["status"], string> = {
  open: "border-[#39424e]",
  claimed: "border-[#39c5cf]/60",
  done: "border-[#3fb950]/50",
  failed: "border-[#f85149]/60",
}

const STATUS_LABEL: Record<TaskGraphNode["status"], string> = {
  open: "待认领",
  claimed: "执行中",
  done: "完成",
  failed: "失败",
}

export type TaskFlowNodeData = {
  node: TaskGraphNode
  /** 认领会话当前 paused（仅 claimed 有意义，琥珀边框/会话名） */
  paused: boolean
  onActivate: (n: TaskGraphNode) => void
  onDelete: (n: TaskGraphNode) => void
}

export type TaskFlowNodeType = Node<TaskFlowNodeData, "task">

export function TaskNode({ data }: NodeProps<TaskFlowNodeType>) {
  const { node: n, paused, onActivate, onDelete } = data
  const color = STATUS_COLOR[n.status]
  const s = planStats(n.plan)
  // 双击三分支提示（F9，与 TaskFlow activateNode 一致）
  const activateTitle = n.claimed_by && n.session?.status !== "closed"
    ? "双击挂回该认领会话的直播页签"
    : n.status === "done" || n.status === "failed"
      ? "双击开任务窗：带该任务上下文的新会话（复盘/续研）"
      : "双击跳到任务看板定位此任务"

  return (
    <div
      onDoubleClick={() => onActivate(n)}
      title={activateTitle}
      className={cn(
        "group relative rounded-md border bg-popover px-2.5 py-2 shadow-sm transition-opacity",
        paused && n.status === "claimed" ? "border-amber-400/70" : STATUS_BORDER[n.status],
      )}
      style={{ width: NODE_W }}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-[#6e7681]" />
      <span className="absolute inset-y-0 left-0 w-1 rounded-l-md" style={{ background: color }} />

      <button
        type="button"
        title="删除任务（claimed 当前步结束即硬中断；有子任务被后端 409 拒绝）"
        onClick={(e) => { e.stopPropagation(); onDelete(n) }}
        onDoubleClick={(e) => e.stopPropagation()}
        className="absolute right-1 top-1 hidden rounded p-0.5 text-muted-foreground hover:text-[--status-error] group-hover:block"
      >
        <Trash2 className="size-3.5" />
      </button>

      <div className="flex items-center gap-1.5 pr-5">
        <span className="min-w-0 flex-1 truncate rounded bg-muted px-1 font-mono text-[9px] text-muted-foreground">
          {n.task_type}
        </span>
        {(n.attempts ?? 0) >= 2 && (
          <span className="shrink-0 font-mono text-[9px] text-muted-foreground" title="任务多次执行（上下文随任务保留，跨会话接手）">
            ↻{n.attempts}
          </span>
        )}
        <span className="font-mono text-[9px]" style={{ color }}>{STATUS_LABEL[n.status]}</span>
        <span className="font-mono text-[9px] text-muted-foreground">P{n.priority}</span>
      </div>

      <p className="mt-1 line-clamp-2 text-[11px] font-medium leading-tight">{n.objective}</p>

      {s.total > 0 && (
        <div className="mt-1 space-y-0.5">
          <p className="font-mono text-[10px] text-sky-400">
            ▦ {s.done}/{s.total}
          </p>
          {s.doing && (
            <p className="truncate text-[10px] text-muted-foreground" title={s.doing.title}>
              ▸ {s.doing.title}
            </p>
          )}
          {s.blocked && (
            <p className="truncate text-[10px] text-amber-400" title={s.blocked.note || "阻塞"}>
              ■ {s.blocked.note || "阻塞"}
            </p>
          )}
        </div>
      )}

      <div className="mt-1 flex items-center gap-1">
        {n.session ? (
          <span
            className={cn(
              "min-w-0 flex-1 truncate font-mono text-[9px]",
              paused && n.status === "claimed" ? "text-amber-400" : "text-primary",
            )}
            title={`认领会话：${n.session.name || n.session.id}${paused ? "（已暂停）" : ""}`}
          >
            {paused && n.status === "claimed" ? "⏸ " : "● "}{n.session.name || n.session.id.slice(0, 14)}
          </span>
        ) : (
          <span className="flex-1 truncate text-[9px] text-muted-foreground">未认领 · 双击跳看板</span>
        )}
        {n.noise_budget !== "passive" && (
          <span className="shrink-0 font-mono text-[9px] text-[--status-approval]">{n.noise_budget}</span>
        )}
      </div>

      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-[#6e7681]" />
    </div>
  )
}
