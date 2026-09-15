import { Handle, Position, type Node, type NodeProps } from "@xyflow/react"
import { Link2 } from "lucide-react"
import type { Finding } from "@/lib/types"
import { hasPoc } from "@/components/blackboard/FindingDetailDialog"
import { cn } from "@/lib/utils"
import { COL_W, LANE_W } from "./canvasModel"

// 评估画布发现卡片（E1）：severity 色条 + 标题（2 行截断）+ vuln_class
// + 状态角标（✓ verified / 虚线框 unverified / ✕ false-positive）+ POC 角标。

const SEV: Record<string, string> = {
  critical: "#f85149",
  high: "#f85149",
  medium: "#d29922",
  low: "#58a6ff",
  info: "#8b949e",
}

export type FindingNodeData = {
  f: Finding
  dim: boolean
  onOpen: (f: Finding) => void
  onQuickAdd: (f: Finding) => void
}

export type FindingFlowNode = Node<FindingNodeData, "finding">

export function FindingNode({ data }: NodeProps<FindingFlowNode>) {
  const { f, dim, onQuickAdd } = data
  const sevColor = SEV[f.severity] ?? SEV.info
  return (
    <div
      className={cn(
        "group relative w-56 rounded-md border bg-popover px-2.5 py-2 shadow-sm transition-opacity",
        f.status === "false-positive"
          ? "border-dashed border-muted-foreground/40"
          : f.status === "unverified"
            ? "border-dashed border-[#39424e]"
            : "border-[#3fb950]/50",
        dim && "opacity-20",
      )}
    >
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-0 !bg-[#39c5cf]" />
      <span className="absolute inset-y-0 left-0 w-1 rounded-l-md" style={{ background: sevColor }} />

      <button
        type="button"
        title="加入攻击链"
        onClick={(e) => { e.stopPropagation(); onQuickAdd(f) }}
        className="absolute right-1 top-1 hidden rounded text-muted-foreground hover:text-[#39c5cf] group-hover:block"
      >
        <Link2 className="size-3.5" />
      </button>

      <p className="line-clamp-2 pr-5 text-[11px] font-medium leading-tight">{f.title}</p>
      <div className="mt-1 flex items-center gap-1">
        <span className="min-w-0 flex-1 truncate rounded bg-muted px-1 font-mono text-[9px] text-muted-foreground">
          {f.vuln_class}
        </span>
        <span className="font-mono text-[9px] uppercase" style={{ color: sevColor }}>{f.severity}</span>
        {f.status === "verified" && (
          <span className="text-[10px] text-[#3fb950]" title="verified">✓</span>
        )}
        {f.status === "false-positive" && (
          <span className="text-[10px] text-muted-foreground" title="false-positive">✕</span>
        )}
        {hasPoc(f) && (
          <span className="rounded bg-[#39c5cf]/15 px-1 text-[9px] text-[#39c5cf]">POC</span>
        )}
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-0 !bg-[#39c5cf]" />
    </div>
  )
}

// 泳道背景：必须注册自定义 type——无 type 的节点会被 xyflow 按内置 default
// 渲染成白卡片（白底+左右连接点），在深色画布上直接穿帮。
export type LaneBackgroundData = Record<string, never>
export type LaneBackgroundNode = Node<LaneBackgroundData, "laneBg">

export function LaneBackground() {
  return <div className="h-full w-full rounded-lg border border-[#21262d] bg-[#161b22]/40" />
}

export type LaneHeaderData = {
  label: string
  counts: [number, number, number, number] // 四列（info/low｜medium｜high｜critical）发现数
}

export type LaneHeaderNode = Node<LaneHeaderData, "laneHeader">

const COL_LABEL = ["信息/低危", "中危", "高危", "严重"]

export function LaneHeader({ data }: NodeProps<LaneHeaderNode>) {
  return (
    <div className="relative h-10 rounded-md border border-[#30363d] bg-[#0d1117]/90" style={{ width: LANE_W }}>
      <div className="flex h-5 items-center px-2">
        <span className="truncate font-mono text-[11px] font-semibold text-foreground">🖧 {data.label}</span>
      </div>
      <div className="relative h-5 border-t border-[#30363d]">
        {COL_LABEL.map((label, i) => (
          <span
            key={i}
            className="absolute top-0.5 -translate-x-1/2 text-[9px] text-muted-foreground"
            style={{ left: i * COL_W + 112 }}
          >
            {label}{data.counts[i] > 0 && <span className="ml-1 text-muted-foreground/70">{data.counts[i]}</span>}
          </span>
        ))}
      </div>
    </div>
  )
}
