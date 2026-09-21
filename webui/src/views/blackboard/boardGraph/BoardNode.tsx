import { Handle, Position, type Node, type NodeProps } from "@xyflow/react"
import { cn } from "@/lib/utils"
import type { BoardGraphNode, BoardNodeType } from "@/lib/types"
import { BOARD_CARD_W } from "./boardModel"

// 黑板链路图五类节点卡（P4，DESIGN.md §12）：按 node_type 分色/图标；
// finding 复用严重度左色条语义。label/sub 服务端已拼好，这里零二次映射。
// colHeader/colBg 必须注册自定义 type——无 type 节点会被 xyflow 渲染成内置
// 白卡片（同 LaneBackground 的穿帮坑，见 ../FindingNode.tsx 注释）。

export const TYPE_ACCENT: Record<BoardNodeType, string> = {
  asset: "#58a6ff",
  func_kb: "#bc8cff",
  finding: "#f85149",
  artifact: "#3fb950",
  task: "#d29922",
}

export const TASK_DOT: Record<string, string> = {
  claimed: "#d29922", open: "#58a6ff", done: "#3fb950", failed: "#f85149",
}

export type BoardNodeData = {
  n: BoardGraphNode
  dim: boolean
  /** CTF 轨 finding 的级别显示词（关键突破/有效线索/背景信息），非 CTF 缺省用 severity 原词 */
  sevText?: string
  onSelect: (n: BoardGraphNode) => void
  selected: boolean
}

export type BoardFlowNode = Node<BoardNodeData, "boardNode">

export function BoardNode({ data }: NodeProps<BoardFlowNode>) {
  const { n, dim, onSelect, selected } = data
  const accent = TYPE_ACCENT[n.node_type] ?? "#8b949e"
  const isFinding = n.node_type === "finding"
  const sevColor = isFinding ? (TYPE_ACCENT.finding) : undefined
  const statusDot = n.node_type === "task" ? TASK_DOT[n.status ?? ""] : undefined
  // 对话轮产出（2026-09-20 会话窗对话化）：finding 作者为会话窗（sess- 前缀）→
  // 「对话产出」徽章。任务轮 finding 无任务上下文也有 author=sess-（对话轮
  // bb_add_finding），与设计一致——对话产 finding 无 basis 边，靠此徽章可辨来源。
  const chatMade = isFinding && typeof n.author === "string" && n.author.startsWith("sess-")
  return (
    <div
      style={{ width: BOARD_CARD_W }}
      className={cn(
        "relative rounded-md border bg-popover px-2.5 py-2 shadow-sm transition-opacity",
        selected ? "border-primary/70 ring-1 ring-primary/40" : "border-[#39424e]",
        dim && "opacity-[0.08]",
      )}
      onClick={(e) => { e.stopPropagation(); onSelect(n) }}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0" style={{ background: accent }} />
      <span className="absolute inset-y-0 left-0 w-1 rounded-l-md"
            style={{ background: isFinding ? (sevColor ?? accent) : accent, opacity: isFinding ? 0.8 : 0.6 }} />
      <p className="line-clamp-2 break-all pr-1 text-[12px] font-medium leading-snug">{n.label}</p>
      <div className="mt-1 flex items-center gap-1">
        {statusDot && <span className="size-1.5 shrink-0 rounded-full" style={{ background: statusDot }} />}
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground">{n.sub}</span>
        {chatMade && (
          <span className="shrink-0 rounded bg-primary/15 px-1 font-mono text-[9px] leading-4 text-primary"
                title={`对话轮产出（${n.author}）`}>对话产出</span>
        )}
        {n.node_type === "finding" && data.sevText && (
          <span className="shrink-0 font-mono text-[10px]" style={{ color: sevColor }}>{data.sevText}</span>
        )}
        {n.node_type === "func_kb" && (n.risk_tags?.length ?? 0) > 0 && (
          <span className="shrink-0 font-mono text-[10px] text-(--status-approval)">
            {n.risk_tags!.slice(0, 2).join(",")}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0" style={{ background: accent }} />
    </div>
  )
}

// 列头（列类型 + 节点数）
export type BoardColHeaderData = { label: string; icon: string; count: number; width: number }
export type BoardColHeaderNode = Node<BoardColHeaderData, "boardColHeader">

export function BoardColHeader({ data }: NodeProps<BoardColHeaderNode>) {
  return (
    <div className="flex items-center gap-1.5 rounded-md border border-[#30363d] bg-[#0d1117]/90 px-2"
         style={{ width: data.width }}>
      <span className="font-mono text-[12px] text-primary">{data.icon}</span>
      <span className="truncate font-mono text-[12px] font-semibold text-foreground">{data.label}</span>
      <span className="ml-auto font-mono text-[10px] text-muted-foreground">{data.count}</span>
    </div>
  )
}

// 列背景（最底层，不可交互）
export type BoardColBgData = { width: number; height: number }
export type BoardColBgNode = Node<BoardColBgData, "boardColBg">

export function BoardColBg({ data }: NodeProps<BoardColBgNode>) {
  return <div className="h-full w-full rounded-lg border border-[#21262d] bg-[#161b22]/40"
              style={{ width: data.width, height: data.height }} />
}
