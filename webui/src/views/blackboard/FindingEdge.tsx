import {
  BaseEdge, EdgeLabelRenderer, getBezierPath, getSmoothStepPath, getStraightPath,
  type Edge, type EdgeProps,
} from "@xyflow/react"
import type { EdgeKind } from "./canvasModel"

// 评估画布三级边（E1）：
//   weak   灰虚线（同类/同父任务升级推导，无标签）
//   strong 青实线（evidence.relates_to，note 作标签，点标签=快捷加入链）
//   chain  按链状态着色（hypothesis 黄/validated 蓝/exploited 红，edge_note 标签）
// 整条边可点选（加宽透明命中带），选中后由外层浮卡片操作（chain 删边 / strong 加入链）。
// C2 降噪路由：同列直线 / 同泳道跨列平滑折线 / 跨泳道贝塞尔。

export type FindingEdgeData = {
  kind: EdgeKind
  label?: string
  selected?: boolean
  dimmed?: boolean
  route?: "bezier" | "step" | "straight"
  /** 任一端点 finding 已被标 false-positive：边淡出（DESIGN §6.7 的 1.6 撤回传播） */
  stale?: boolean
  onSelect?: (id: string) => void
}

export type FindingFlowEdge = Edge<FindingEdgeData, "findingEdge">

export function FindingEdge({
  id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  data, style, markerEnd,
}: EdgeProps<FindingFlowEdge>) {
  const args = {
    sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  }
  const route = data?.route ?? "bezier"
  const [path, labelX, labelY] =
    route === "straight" ? getStraightPath(args)
      : route === "step" ? getSmoothStepPath({ ...args, borderRadius: 16 })
        : getBezierPath(args)
  const text = data?.label?.trim() ?? ""
  return (
    <>
      {/* 透明加宽命中带：点边选中 */}
      <path
        d={path} fill="none" stroke="transparent" strokeWidth={14}
        className="cursor-pointer"
        onClick={(e) => { e.stopPropagation(); data?.onSelect?.(id) }}
      />
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      {text && (
        <EdgeLabelRenderer>
          <span
            title={data?.stale
              ? "依据已被推翻（false-positive），此引用失效——见 DESIGN §6.7 的 1.6"
              : data?.kind === "chain" ? "点边查看/删除" : text}
            onClick={(e) => { e.stopPropagation(); data?.onSelect?.(id) }}
            className={`nodrag nopan pointer-events-auto absolute line-clamp-2 max-w-[170px] cursor-pointer rounded border
                        px-1.5 py-0.5 text-center text-[10px] leading-tight shadow-sm
                        ${data?.stale ? "opacity-30 line-through" : ""}
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
