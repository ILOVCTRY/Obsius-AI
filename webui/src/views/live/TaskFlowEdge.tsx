import {
  BaseEdge, EdgeLabelRenderer, getBezierPath,
  type Edge, type EdgeProps,
} from "@xyflow/react"
import type { TaskGraphEdgeRef } from "@/lib/types"
import { cn } from "@/lib/utils"

// A3 任务流两类边：
//   parent 灰实线带箭头 = tasks.parent_id 分解结构（人/编排/子代理）；
//   inbox  紫虚线 ✉     = 会话间实际私信（basis_stale 撤回 / finding_update 增补），
//                        按 (kind,ref_id) 聚类后映射到最近任务，点 ✉ 看依据浮卡。

export type TaskFlowEdgeData = {
  kind: "parent" | "inbox"
  refs?: TaskGraphEdgeRef[]
  selected?: boolean
  onSelect?: (id: string) => void
}

export type TaskFlowEdgeType = Edge<TaskFlowEdgeData, "taskEdge">

export function TaskFlowEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  data, style, markerEnd,
}: EdgeProps<TaskFlowEdgeType>) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  })
  const refs = data?.refs ?? []
  const isInbox = data?.kind === "inbox"
  const title = refs.length
    ? refs
        .map((r) => `${r.kind === "basis_stale" ? "依据撤回" : "发现增补"}：${r.title || r.ref_id}`)
        .join("\n")
    : "私信协作边"

  return (
    <>
      {/* inbox 边加宽透明命中带；parent 边不可点（样式区分即可） */}
      {isInbox && (
        <path
          d={path} fill="none" stroke="transparent" strokeWidth={14}
          className="cursor-pointer"
          onClick={(e) => { e.stopPropagation(); data?.onSelect?.(id) }}
        />
      )}
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      {isInbox && (
        <EdgeLabelRenderer>
          <span
            title={title}
            onClick={(e) => { e.stopPropagation(); data?.onSelect?.(id) }}
            className={cn(
              "nodrag nopan pointer-events-auto absolute flex size-5 items-center justify-center",
              "rounded-full border text-[9px] leading-none shadow-sm",
              data?.selected
                ? "border-[#a371f7] bg-[#1c2128] text-[#d2a8ff] ring-1 ring-[#a371f7]/50"
                : "border-[#39424e] bg-[#1c2128]/95 text-[#a371f7] hover:border-[#a371f7]/60",
            )}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {refs.length > 1 ? `✉${refs.length}` : "✉"}
          </span>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
