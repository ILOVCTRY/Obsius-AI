import {
  BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type Edge, type EdgeProps,
} from "@xyflow/react"

// 黑板链路图自定义边（P4）：复制 ../FindingEdge.tsx 骨架（BaseEdge + 加宽命中带 +
// EdgeLabelRenderer 标签），路由统一 smoothstep（类型分层 DAG 全是跨列水平走线）。
// 着色/虚线由外层 style 决定（kind 分色 / chain 按链状态 / stale 点虚线淡化）。

export type BoardEdgeData = {
  kind: string
  label?: string
  selected?: boolean
  stale?: boolean          // basis 边：依据已撤回（收录+标记不排除，图上点虚线淡化）
  onSelect?: (id: string) => void
}

export type BoardFlowEdge = Edge<BoardEdgeData, "boardEdge">

export function BoardEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  data, style, markerEnd,
}: EdgeProps<BoardFlowEdge>) {
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 12,
  })
  const text = data?.label?.trim() ?? ""
  return (
    <>
      <path
        d={path} fill="none" stroke="transparent" strokeWidth={12}
        className="cursor-pointer"
        onClick={(e) => { e.stopPropagation(); data?.onSelect?.(id) }}
      />
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      {text && (
        <EdgeLabelRenderer>
          <span
            title={data?.stale ? "依据已被推翻（stale），仅标记不剔除" : text}
            onClick={(e) => { e.stopPropagation(); data?.onSelect?.(id) }}
            className={`nodrag nopan pointer-events-auto absolute line-clamp-2 max-w-[160px] cursor-pointer rounded border
                        px-1.5 py-0.5 text-center text-[10px] leading-tight shadow-sm
                        ${data?.stale ? "opacity-30" : ""}
                        ${data?.selected
                          ? "border-primary/70 bg-[#1c2128] text-foreground ring-1 ring-primary/40"
                          : "border-[#39424e]/60 bg-[#1c2128]/95 text-muted-foreground hover:border-[#39c5cf]/60"}`}
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {text}
          </span>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
