import type { BoardGraph, BoardGraphNode, BoardNodeType } from "@/lib/types"

// 黑板链路图布局器（P3，DESIGN.md §12 黑板链路图）：纯函数不碰 React。
// 类型分层 DAG：五列固定序 [asset, func_kb, finding, artifact, task]，空类型列
// 自动消失（左移）。行 packing 贪心跟已放置邻居同行（边呈水平直线，参照
// ../canvasModel.ts 串联货架的「跟前任同行」），无空位取本列最小空行。
// React Flow 节点组装在 BoardGraphCanvas.tsx。

export const BOARD_CARD_W = 240    // 节点卡宽
export const COL_W = 292           // 列宽（卡 240 + 间距 52，跨列边留水平走线空间）
export const BOARD_ROW_STEP = 118  // 行距
export const BOARD_Y0 = 92         // 顶部留 44px 工具条 + 40px 列头 + 余量
export const BOARD_X0 = 24

const COLUMN_ORDER: BoardNodeType[] = ["asset", "func_kb", "finding", "artifact", "task"]
export const COLUMN_LABEL: Record<BoardNodeType, string> = {
  asset: "资产", func_kb: "函数库", finding: "发现", artifact: "产物", task: "任务",
}
export const COLUMN_ICON: Record<BoardNodeType, string> = {
  asset: "🖧", func_kb: "ƒ", finding: "◈", artifact: "📦", task: "☐",
}

const SEV_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3, info: 4 }
// CTF 线索级别序（critical→info）与五档序一致，排序共用 SEV_RANK（显示词表见 ../ctfLevel.ts）

const TASK_STATUS_RANK: Record<string, number> = {
  claimed: 0, open: 1, done: 2, failed: 3,  // 在跑最上，死任务垫底；未知状态排最后
}

export interface BoardColumn {
  node_type: BoardNodeType
  label: string
  x: number
  count: number
}

export interface BoardLayout {
  /** 节点 id → 画布坐标（隐藏节点不进 place） */
  place: Map<string, { x: number; y: number }>
  /** 非空列元数据（空类型列已跳过左移），x 为列左缘 */
  columns: BoardColumn[]
  /** 死路 finding（status=false-positive）——工具条计数/开关 */
  deadEndIds: Set<string>
  /** 孤立节点（无任何边）——工具条计数/开关（函数库降噪） */
  isolatedIds: Set<string>
  /** 内容总高（列背景高度用） */
  height: number
}

export interface BoardLayoutOptions {
  /** 缺省 false：死路不进 place（全景审计仍可一键展开） */
  showDeadEnds?: boolean
  /** 缺省 false：无边孤立节点不占列（单二进制数百 func 时的降噪） */
  showIsolated?: boolean
}

// 列内排序（按类型分派）
function assetDfsOrder(assets: BoardGraphNode[]): BoardGraphNode[] {
  // parent 树 DFS 序（参照 Assets 树视图）：父不在节点集的作根；同层按 created_at
  const ids = new Set(assets.map((a) => a.id))
  const childrenOf = new Map<string, BoardGraphNode[]>()
  const roots: BoardGraphNode[] = []
  for (const a of assets) {
    if (a.parent_id && ids.has(a.parent_id)) {
      ;(childrenOf.get(a.parent_id) ?? childrenOf.set(a.parent_id, []).get(a.parent_id)!).push(a)
    } else {
      roots.push(a)
    }
  }
  const byTime = (a: BoardGraphNode, b: BoardGraphNode) =>
    (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.id.localeCompare(b.id)
  roots.sort(byTime)
  for (const list of childrenOf.values()) list.sort(byTime)
  const out: BoardGraphNode[] = []
  const walk = (n: BoardGraphNode) => {
    out.push(n)
    for (const c of childrenOf.get(n.id) ?? []) walk(c)
  }
  for (const r of roots) walk(r)
  return out.length === assets.length ? out : assets  // 防御：成环时退化为原序
}

const byCreated = (a: BoardGraphNode, b: BoardGraphNode) =>
  (a.created_at ?? "").localeCompare(b.created_at ?? "") || a.id.localeCompare(b.id)

const SORTERS: Record<BoardNodeType, (list: BoardGraphNode[]) => BoardGraphNode[]> = {
  asset: assetDfsOrder,
  func_kb: (list) => [...list].sort((a, b) =>
    (a.binary_sha256 ?? "").localeCompare(b.binary_sha256 ?? "") ||
    (Number(a.address) || 0) - (Number(b.address) || 0) || a.id.localeCompare(b.id)),
  finding: (list) => [...list].sort((a, b) =>
    (SEV_RANK[a.severity ?? ""] ?? 9) - (SEV_RANK[b.severity ?? ""] ?? 9) || byCreated(a, b)),
  artifact: (list) => [...list].sort(byCreated),
  task: (list) => [...list].sort((a, b) =>
    (TASK_STATUS_RANK[a.status ?? ""] ?? 9) - (TASK_STATUS_RANK[b.status ?? ""] ?? 9) ||
    (a.priority ?? 2) - (b.priority ?? 2) ||
    (b.created_at ?? "").localeCompare(a.created_at ?? "") || a.id.localeCompare(b.id)),
}

export function buildBoardLayout(
  graph: BoardGraph, opts: BoardLayoutOptions = {},
): BoardLayout {
  const deadEndIds = new Set(graph.nodes
    .filter((n) => n.node_type === "finding" && n.status === "false-positive").map((n) => n.id))
  const degree = new Map<string, number>()
  for (const e of graph.edges) {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1)
    degree.set(e.target, (degree.get(e.target) ?? 0) + 1)
  }
  const isolatedIds = new Set(graph.nodes.filter((n) => !degree.get(n.id)).map((n) => n.id))

  const hidden = new Set<string>()
  if (!opts.showDeadEnds) for (const id of deadEndIds) hidden.add(id)
  if (!opts.showIsolated) for (const id of isolatedIds) hidden.add(id)

  const byType = new Map<BoardNodeType, BoardGraphNode[]>()
  for (const n of graph.nodes) {
    if (hidden.has(n.id)) continue
    ;(byType.get(n.node_type) ?? byType.set(n.node_type, []).get(n.node_type)!).push(n)
  }

  // 邻接表：行 packing 时「跟已放置邻居同行」——边方向无关紧要（task→finding 的
  // basis 边在列序上 finding 先放，改由后放的 task 跟 finding 行对齐）
  const neighbors = new Map<string, string[]>()
  for (const e of graph.edges) {
    ;(neighbors.get(e.source) ?? neighbors.set(e.source, []).get(e.source)!).push(e.target)
    ;(neighbors.get(e.target) ?? neighbors.set(e.target, []).get(e.target)!).push(e.source)
  }

  const place = new Map<string, { x: number; y: number }>()
  const columns: BoardColumn[] = []
  const rowOf = new Map<string, number>()
  let maxRow = 0
  let colIdx = 0
  for (const type of COLUMN_ORDER) {
    const list = byType.get(type)
    if (!list?.length) continue
    const x = BOARD_X0 + colIdx * COL_W
    const used = new Set<number>()
    for (const n of SORTERS[type](list)) {
      let r = -1
      for (const nb of neighbors.get(n.id) ?? []) {
        const nr = rowOf.get(nb)
        if (nr !== undefined && !used.has(nr)) { r = nr; break }
      }
      if (r < 0) { r = 0; while (used.has(r)) r++ }
      used.add(r)
      rowOf.set(n.id, r)
      place.set(n.id, { x, y: BOARD_Y0 + r * BOARD_ROW_STEP })
      maxRow = Math.max(maxRow, r)
    }
    columns.push({ node_type: type, label: COLUMN_LABEL[type], x, count: list.length })
    colIdx++
  }

  return {
    place, columns, deadEndIds, isolatedIds,
    height: BOARD_Y0 + (maxRow + 1) * BOARD_ROW_STEP + 24,
  }
}
