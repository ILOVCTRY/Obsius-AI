import { BaseEdge, EdgeLabelRenderer, type Edge, type EdgeProps } from "@xyflow/react"
import { GAP_HALF, type Bend, type EdgeKind } from "./canvasModel"

// 评估画布三级边（E1）：
//   weak   灰虚线（同类/同父任务升级推导，只画右向）
//   strong 青实线（evidence.relates_to，note 作标签，选中浮卡=快捷加入链）
//   chain  按链状态着色（hypothesis 黄/validated 蓝/exploited 红，edge_note 标签）
// 单向零重叠正交路由（findings-canvas-dag-layout R1/R2，2026-09-22）——水平段只出现在
// 中缝 14px 短桩 / 空行带长跑 / 泳道底绕行带，构造上不穿卡：
//   adjacent 层差=1：源右缘 → 中缝垂直拐 → 目标左缘（同行退化为直线）
//   long     层差>1：源列后走廊垂直离场 → 空行带长跑 → 目标列前走廊垂直进场
//   cross    跨泳道：源列后走廊下探 → 全泳道底绕行带 → 目标列前走廊上浮
// 注记默认隐藏（D1）：hover/选中该边才浮现，落点=最长段中点（多在空带内，瞬态遮挡可接受）。

export type FindingRoute = "adjacent" | "long" | "cross"

export type FindingEdgeData = {
  kind: EdgeKind
  label?: string
  selected?: boolean
  hovered?: boolean
  dimmed?: boolean
  route: FindingRoute
  /** long/cross：水平长跑 y（模型 longRunY 空行带中心 / 全泳道底绕行带） */
  longY?: number
  /** 走廊 micro-offset（±3px 阶梯）：同走廊多边的垂直段保持可辨 */
  g1off?: number
  gdoff?: number
  /** 任一端点 finding 已被标 false-positive：边淡出（DESIGN §6.7 的 1.6 撤回传播） */
  stale?: boolean
  onSelect?: (id: string) => void
}

export type FindingFlowEdge = Edge<FindingEdgeData, "findingEdge">

/** 正交折线 → 圆角 path（拐角沿两邻段截短 r 用二次曲线过渡；相邻重复点先滤掉） */
function orthoPath(pts: Bend[], r = 8): string {
  const p = pts.filter((q, i) =>
    i === 0 || Math.abs(q.x - pts[i - 1].x) > 0.5 || Math.abs(q.y - pts[i - 1].y) > 0.5)
  if (p.length < 2) return ""
  let d = `M ${p[0].x} ${p[0].y}`
  for (let i = 1; i < p.length - 1; i++) {
    const a = p[i - 1]
    const v = p[i]
    const b = p[i + 1]
    const l1 = Math.hypot(v.x - a.x, v.y - a.y)
    const l2 = Math.hypot(b.x - v.x, b.y - v.y)
    const rr = Math.min(r, l1 / 2, l2 / 2)
    d += ` L ${v.x - ((v.x - a.x) / l1) * rr} ${v.y - ((v.y - a.y) / l1) * rr}`
    d += ` Q ${v.x} ${v.y} ${v.x + ((b.x - v.x) / l2) * rr} ${v.y + ((b.y - v.y) / l2) * rr}`
  }
  const last = p[p.length - 1]
  d += ` L ${last.x} ${last.y}`
  return d
}

/** 标签落点 = 最长段中点（长跑段在空带、竖直段在中缝、直线段在卡间缝——均避开卡体） */
function longestMid(pts: Bend[]): Bend {
  let best = pts[0]
  let bestLen = -1
  for (let i = 1; i < pts.length; i++) {
    const len = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
    if (len > bestLen) {
      bestLen = len
      best = { x: (pts[i].x + pts[i - 1].x) / 2, y: (pts[i].y + pts[i - 1].y) / 2 }
    }
  }
  return best
}

export function FindingEdge({
  id, sourceX, sourceY, targetX, targetY, data, style, markerEnd, interactionWidth,
}: EdgeProps<FindingFlowEdge>) {
  const route = data?.route ?? "adjacent"
  // 锚点=xyflow 实测 handle（源卡右缘/目标卡左缘）；走廊 x=卡缘 ± GAP_HALF（28px 缝内居中）
  const g1 = sourceX + GAP_HALF + (data?.g1off ?? 0)
  const gd = targetX - GAP_HALF + (data?.gdoff ?? 0)
  const start: Bend = { x: sourceX, y: sourceY }
  const end: Bend = { x: targetX, y: targetY }
  let bends: Bend[]
  if (route === "adjacent") {
    // 同行（±12px 内，卡高实测有差）退化为直线——保留「一一对照」水平观感
    bends = Math.abs(sourceY - targetY) <= 12
      ? []
      : [{ x: g1, y: sourceY }, { x: g1, y: targetY }]
  } else {
    const y = data?.longY ?? sourceY
    bends = [
      { x: g1, y: sourceY },
      { x: g1, y },
      { x: gd, y },
      { x: gd, y: targetY },
    ]
  }
  const pts = [start, ...bends, end]
  const path = orthoPath(pts)
  const mid = longestMid(pts)
  const text = data?.label?.trim() ?? ""
  const showLabel = !!text && !!(data?.hovered || data?.selected)
  return (
    <>
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} interactionWidth={interactionWidth} />
      {showLabel && (
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
            style={{ transform: `translate(-50%, -50%) translate(${mid.x}px, ${mid.y}px)` }}
          >
            {text}
          </span>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
