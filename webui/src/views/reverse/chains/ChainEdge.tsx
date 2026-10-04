import {
  BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps,
} from "@xyflow/react"

// 攻击链自定义边：贝塞尔连线 + HTML 边注卡片（EdgeLabelRenderer portal）。
// 默认 edge 的 label 走 SVG <text>：不换行，长中文边注会溢出压住节点；
// HTML span 可限宽两行，全文悬停可见。pointer-events-none 让双击落到边线上触发编辑。

export type ChainFlowEdge = Edge

export function ChainEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  label, style,
}: EdgeProps<ChainFlowEdge>) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  })
  const text = typeof label === "string" ? label : ""
  return (
    <>
      <BaseEdge id={id} path={path} style={style} />
      {text.trim() ? (
        <EdgeLabelRenderer>
          <span
            title={text}
            className="nodrag nopan pointer-events-none absolute line-clamp-2 max-w-[150px]
                       rounded border border-(--viz-edge)/60 bg-(--viz-card)/95 px-1.5 py-0.5
                       text-center text-[10px] leading-tight text-muted-foreground shadow-sm"
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {text}
          </span>
        </EdgeLabelRenderer>
      ) : null}
    </>
  )
}
