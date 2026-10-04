import type { Edge, Node } from "@xyflow/react"
import type { ChainLink } from "@/lib/types"

// 攻击链画布的纯定位逻辑（不做自动布局库：线性 seq 横排够用，DESIGN §12）

export const ENTITY_W = 200
// 间隙要放得下自定义边注卡片（见 ChainEdge，限宽 150px 两行）+ 两侧余量：
// 卡片比间隙宽会盖住相邻节点面；56 时节点屏幕重叠，96 时仍压边 27px
const GAP_X = 170
const CANVAS_Y = 40

export interface EntityNodeData extends Record<string, unknown> {
  link: ChainLink
  /** hover「× 移出链」 */
  onRemove: (link: ChainLink) => void
}

export type EntityFlowNode = Node<EntityNodeData, "entity">

/** links 已按 seq 升序；第 n 条 link 的 edge_note 标在 n-1→n 的入边上 */
export function layout(links: ChainLink[], onRemove: (l: ChainLink) => void) {
  const nodes: EntityFlowNode[] = links.map((link, i) => ({
    id: link.id,
    type: "entity",
    position: { x: 24 + i * (ENTITY_W + GAP_X), y: CANVAS_Y },
    data: { link, onRemove },
    draggable: false,
  }))
  const edges: Edge[] = []
  for (let i = 1; i < links.length; i++) {
    edges.push({
      id: `${links[i - 1].id}->${links[i].id}`,
      type: "entity-edge",
      source: links[i - 1].id,
      target: links[i].id,
      label: links[i].edge_note || " ",
      style: { stroke: "var(--viz-edge)", strokeWidth: 1.5 },
    })
  }
  return { nodes, edges }
}
