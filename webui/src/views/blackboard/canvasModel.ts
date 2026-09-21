import type { Asset, Chain, Finding } from "@/lib/types"

// 评估攻击链画布的纯布局/边推导（E1，DESIGN §12 评估画布）。
// 不碰 React：给定 findings（已过 IP/sev/status 筛选）+ assets + chains 详情，
// 产出泳道/列归属与三级边。React Flow 节点组装在 FindingsCanvas.tsx。

export const UNASSIGNED = "__unassigned__"
export const GLOBAL_LANE = "__global__"   // C2 降噪：无筛选时单一全局泳道（severity 四列共用）

export const CARD_W = 320          // w-80（2026-09-18 放大，替代 w-56）
export const COL_W = 348           // 列宽（卡 320 + 列间距 28）
export const LANE_GAP = 44
export const HEADER_H = 40
export const CARD_STEP = 148       // 同列卡纵向步距（完整卡统一，C2 紧凑卡已废）
export const CARD_Y0 = 92          // 顶部留 44px 给画布工具条：泳道头 y=44/高40
export const BAND_ROWS = 8         // 噪声/孤立区折行行数上限，也是串联区货架（strip）的行高

export const SEV_RANK: Record<string, number> = {
  critical: 0, high: 1, medium: 2, low: 3, info: 4,
}

export interface Lane {
  key: string          // host asset id，UNASSIGNED 或 GLOBAL_LANE
  label: string        // IP（全局=「全部发现」）
  x: number
  width: number        // 泳道宽 = 总列数 * COL_W（各泳道可不同；最终放置阶段回填）
  height: number       // 泳道高（laneBg 用；按实际内容算，空泳道保底 420）
}

/** host 归属：从叶子资产沿 parent_id 爬到 type==='host'；找不到 → UNASSIGNED */
export function hostOf(f: Finding, byId: Map<string, Asset>): string {
  let cur = f.target_asset_id
  const seen = new Set<string>()
  while (cur && !seen.has(cur)) {
    seen.add(cur)
    const a = byId.get(cur)
    if (!a) break
    if (a.type === "host") return a.id
    cur = a.parent_id
  }
  return UNASSIGNED
}

export interface LaneModel {
  lanes: Lane[]
  /** finding.id → 槽位（col/row 网格已换算成 x/y；place 不再带严重度列语义） */
  place: Map<string, { x: number; y: number; laneKey: string }>
  /** finding.id → host id/UNASSIGNED（全局布局=GLOBAL_LANE） */
  laneOf: Map<string, string>
  /** F13：真孤点 id 集合（强边/链边连通分量=1）——工具条计数/过滤用 */
  isolatedIds: Set<string>
}

export interface BuildLanesOptions {
  /** 无资产筛选：单一全局泳道（所有发现共用一个三分区画布，不按 host 分块） */
  globalLayout?: boolean
  /** 强边+链边——连通分量/串联分层依据（弱边不参与：纯推导联想边） */
  orderEdges?: ModelEdge[]
  /** F13：孤立节点显示开关（缺省 true=显示）。false 时跳过 noise/orphans
   * 分区——真孤点（连通分量=1）不进 place，泳道宽高只按串联区收缩。 */
  showIsolated?: boolean
}

export function buildLanes(findings: Finding[], assets: Asset[],
                           opts: BuildLanesOptions = {}): LaneModel {
  const byId = new Map(assets.map((a) => [a.id, a]))
  const laneOf = new Map<string, string>()
  const lanes: Lane[] = []
  const showIsolated = opts.showIsolated !== false

  if (opts.globalLayout) {
    // C2 降噪：无筛选时全部发现共用一个三分区画布（不按 IP 分泳道）
    // x/width/height 在最终放置阶段统一回填
    lanes.push({ key: GLOBAL_LANE, label: "全部发现", x: 0, width: 0, height: 420 })
    for (const f of findings) laneOf.set(f.id, GLOBAL_LANE)
  } else {
    const hosts = new Map<string, Asset>()
    for (const f of findings) {
      const hk = hostOf(f, byId)
      laneOf.set(f.id, hk)
      if (hk !== UNASSIGNED) {
        const h = byId.get(hk)
        if (h) hosts.set(hk, h)
      }
    }
    // 泳道按 IP 排序，「未归属」垫底；x/width 在最终放置阶段按实际宽度累计
    const hostLanes = [...hosts.values()].sort((a, b) => a.value.localeCompare(b.value))
    const keys = hostLanes.map((h) => h.id)
    if (laneOfContains(laneOf, UNASSIGNED)) keys.push(UNASSIGNED)
    keys.forEach((k) => lanes.push({
      key: k,
      label: k === UNASSIGNED ? "未归属" : (byId.get(k)?.value ?? k),
      x: 0, width: 0, height: 420,
    }))
  }

  const sorted = [...findings].sort((a, b) =>
    a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id))

  // 连通分量：强边/链边连通的发现归为一个串联单元（并查集）
  const orderEdges = opts.orderEdges ?? []
  const compParent = new Map<string, string>()
  const compFind = (x: string): string => {
    let r = compParent.get(x) ?? x
    while (r !== (compParent.get(r) ?? r)) r = compParent.get(r) ?? r
    let c = x
    while (c !== r) { const n = compParent.get(c) ?? r; compParent.set(c, r); c = n }
    return r
  }
  const compUnion = (a: string, b: string) => {
    const ra = compFind(a); const rb = compFind(b)
    if (ra !== rb) compParent.set(ra, rb)
  }
  for (const e of orderEdges) {
    if (laneOf.has(e.source) && laneOf.has(e.target)) compUnion(e.source, e.target)
  }
  const compSize = new Map<string, number>()
  for (const f of sorted) {
    const r = compFind(f.id)
    compSize.set(r, (compSize.get(r) ?? 0) + 1)
  }
  const compMinCreated = new Map<string, string>()
  for (const f of sorted) {
    const r = compFind(f.id)
    const min = compMinCreated.get(r)
    if (!min || f.created_at < min) compMinCreated.set(r, f.created_at)
  }

  // 串联分层：层 = 距链头（无入边节点）的最长路径；边方向 source→target（基→新 / 链 seq）。
  // 头层 0 在最左，往后每跳右移一列——链条从左到右读，头在最左边。
  const preds = new Map<string, string[]>()
  for (const e of orderEdges) {
    if (!laneOf.has(e.source) || !laneOf.has(e.target)) continue
    ;(preds.get(e.target) ?? preds.set(e.target, []).get(e.target)!).push(e.source)
  }
  const layerMemo = new Map<string, number>()
  const layerVisiting = new Set<string>()
  const layerOf = (id: string): number => {
    const memo = layerMemo.get(id)
    if (memo !== undefined) return memo
    if (layerVisiting.has(id)) return 0 // 环路防御（relates_to 理论上可能成环）
    layerVisiting.add(id)
    let l = 0
    for (const p of preds.get(id) ?? []) l = Math.max(l, layerOf(p) + 1)
    layerVisiting.delete(id)
    layerMemo.set(id, l)
    return l
  }

  // 三分区放置（每泳道独立）：噪声（无连接的 info/low）最左 ｜ 串联居中 ｜ 孤立中高危最右。
  // 先给网格坐标 (col,row)，再按行统一定 y（C2 紧凑卡已废 2026-09-18，全部完整卡统一 CARD_STEP）。
  const place = new Map<string, { x: number; y: number; laneKey: string }>()
  const isCompact = (f: Finding) => f.severity === "low" || f.severity === "info"
  const grid = new Map<string, { col: number; row: number }>()

  for (const lane of lanes) {
    const members = sorted.filter((f) => laneOf.get(f.id) === lane.key)
    const linked = members.filter((f) => (compSize.get(compFind(f.id)) ?? 1) >= 2)
    // F13：showIsolated=false 时跳过噪声/孤立分区——真孤点不进 place（节点组装自然跳过）
    const noise = showIsolated ? members.filter((f) =>
      (compSize.get(compFind(f.id)) ?? 1) === 1 && isCompact(f)) : []
    // 孤立中高危：严重度从高到低（critical→high→medium），再按时间
    const orphans = showIsolated ? members.filter((f) =>
      (compSize.get(compFind(f.id)) ?? 1) === 1 && !isCompact(f))
      .sort((a, b) => (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9) ||
        a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)) : []

    // 噪声区（最左）：列优先折行，最多 BAND_ROWS 行
    noise.forEach((f, i) =>
      grid.set(f.id, { col: Math.floor(i / BAND_ROWS), row: i % BAND_ROWS }))
    const noiseCols = Math.ceil(noise.length / BAND_ROWS)

    // 串联区（居中）：分量按最早发现时间排序，货架式 packing——
    // 同一货架行高 BAND_ROWS，放不下换下一货架（右移）；分量内层=列、行=链对齐。
    const laneComps = new Map<string, Finding[]>()
    for (const f of linked) {
      const r = compFind(f.id)
      ;(laneComps.get(r) ?? laneComps.set(r, []).get(r)!).push(f)
    }
    const comps = [...laneComps.entries()].sort((a, b) =>
      (compMinCreated.get(a[0]) ?? "").localeCompare(compMinCreated.get(b[0]) ?? ""))
    let stripCol = noiseCols
    let stripW = 0
    let rowCursor = 0
    for (const [, list] of comps) {
      // 行分配：链上节点优先跟前任同行（边呈水平直线一一对照），被占则找本层最小空行
      const ordered = [...list].sort((a, b) =>
        layerOf(a.id) - layerOf(b.id) ||
        a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id))
      const localRow = new Map<string, number>()
      const usedLocal = new Set<string>()
      let rowsUsed = 0
      for (const n of ordered) {
        const L = layerOf(n.id)
        const freeAt = (r: number) => !usedLocal.has(`${L}:${r}`)
        let r = -1
        for (const p of preds.get(n.id) ?? []) {
          const pr = localRow.get(p)
          if (pr !== undefined && freeAt(pr)) { r = pr; break }
        }
        if (r < 0) { r = 0; while (!freeAt(r)) r++ }
        usedLocal.add(`${L}:${r}`)
        localRow.set(n.id, r)
        rowsUsed = Math.max(rowsUsed, r + 1)
      }
      const width = Math.max(...ordered.map((n) => layerOf(n.id))) + 1
      if (rowCursor > 0 && rowCursor + rowsUsed > BAND_ROWS) {
        stripCol += stripW
        stripW = 0
        rowCursor = 0
      }
      for (const n of ordered) {
        grid.set(n.id, { col: stripCol + layerOf(n.id), row: rowCursor + (localRow.get(n.id) ?? 0) })
      }
      stripW = Math.max(stripW, width)
      rowCursor += rowsUsed
    }
    const chainEnd = stripCol + stripW

    // 孤立中高危区（最右）：列优先折行
    orphans.forEach((f, i) =>
      grid.set(f.id, { col: chainEnd + Math.floor(i / BAND_ROWS), row: i % BAND_ROWS }))
    const orphanCols = Math.ceil(orphans.length / BAND_ROWS)

    // 行 y = 前缀和（统一 CARD_STEP）；泳道宽 = 总列数 × COL_W（lane.x 在循环外按前序宽度累计）
    const maxRow = Math.max(0, ...[...grid.values()].map((g) => g.row))
    const rowY = new Map<number, number>()
    let acc = CARD_Y0
    for (let r = 0; r <= maxRow; r++) {
      rowY.set(r, acc)
      acc += CARD_STEP
    }
    for (const f of members) {
      const g = grid.get(f.id)
      if (!g) continue
      place.set(f.id, { x: g.col * COL_W, y: rowY.get(g.row) ?? CARD_Y0, laneKey: lane.key })
    }
    lane.width = Math.max(1, chainEnd + orphanCols) * COL_W
    lane.height = Math.max(420, acc + 24)
  }

  // 泳道 x：全局单泳道 x=0；host 模式按前序泳道实际宽度累计 + LANE_GAP。
  // place.x 此时是泳道内相对列坐标，最后统一加泳道 x。
  const laneX = new Map(lanes.map((l) => [l.key, l.x]))
  let cursorX = 0
  for (const lane of lanes) {
    lane.x = cursorX
    laneX.set(lane.key, lane.x)
    cursorX = lane.x + lane.width + LANE_GAP
  }
  for (const [id, p] of place) {
    place.set(id, { ...p, x: p.x + (laneX.get(p.laneKey) ?? 0) })
  }
  // F13：真孤点集合（连通分量=1）——工具条「孤立节点」计数
  const isolatedIds = new Set(
    sorted.filter((f) => (compSize.get(compFind(f.id)) ?? 1) === 1).map((f) => f.id))
  return { lanes, place, laneOf, isolatedIds }
}

function laneOfContains(laneOf: Map<string, string>, key: string): boolean {
  for (const v of laneOf.values()) if (v === key) return true
  return false
}

// ---------- 三级边 ----------

export type EdgeKind = "weak" | "strong" | "chain"

export interface ModelEdge {
  id: string
  source: string
  target: string
  kind: EdgeKind
  label?: string
  /** chain 边专用 */
  chainId?: string
  chainStatus?: Chain["status"]
  chainName?: string
  linkId?: string       // 删边用（chain 边）
}

function relsOf(f: Finding): { finding_id: string; note?: string }[] {
  const rels = (f.evidence as { relates_to?: unknown } | undefined)?.relates_to
  if (!Array.isArray(rels)) return []
  return rels.filter((r): r is { finding_id: string; note?: string } =>
    !!r && typeof r === "object" && typeof (r as { finding_id?: unknown }).finding_id === "string")
}

function taskIdOf(f: Finding): string {
  const ev = f.evidence as { task_id?: unknown; parent_task_id?: unknown } | undefined
  return typeof ev?.task_id === "string" ? ev.task_id
    : typeof ev?.parent_task_id === "string" ? ev.parent_task_id : ""
}

/**
 * 弱边：同泳道内、同 vuln_class 或同父任务，且严重度升级方向（rank 减小）。
 * 时间相邻的升级对连一条（链式而非全连），避免同组 N² 蜘蛛网吧。
 */
export function buildWeakEdges(findings: Finding[], laneOf: Map<string, string>): ModelEdge[] {
  const visible = new Set(findings.map((f) => f.id))
  const groups = new Map<string, Finding[]>()
  for (const f of findings) {
    if (f.status === "false-positive") continue
    const keys = new Set<string>()
    if (f.vuln_class) keys.add(`vc:${f.vuln_class}`)
    const t = taskIdOf(f)
    if (t) keys.add(`task:${t}`)
    for (const k of keys) {
      const list = groups.get(k) ?? []
      list.push(f)
      groups.set(k, list)
    }
  }
  const seen = new Set<string>()
  const edges: ModelEdge[] = []
  for (const list of groups.values()) {
    const ordered = [...list].sort((a, b) =>
      a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id))
    for (let i = 0; i < ordered.length; i++) {
      const a = ordered[i]
      // 找时间上之后、同泳道、严重度更高的最近一个
      for (let j = i + 1; j < ordered.length; j++) {
        const b = ordered[j]
        if (laneOf.get(a.id) !== laneOf.get(b.id)) continue
        const ra = SEV_RANK[a.severity] ?? 9
        const rb = SEV_RANK[b.severity] ?? 9
        if (rb < ra) {
          const id = `weak:${a.id}->${b.id}`
          if (!seen.has(id)) {
            seen.add(id)
            edges.push({ id, source: a.id, target: b.id, kind: "weak" })
          }
          break
        }
      }
    }
  }
  // 防御性：端点必须都在当前可见集
  return edges.filter((e) => visible.has(e.source) && visible.has(e.target))
}

/** 强边：evidence.relates_to（base → 新发现），note 作边标签 */
export function buildStrongEdges(findings: Finding[]): ModelEdge[] {
  const visible = new Set(findings.map((f) => f.id))
  const edges: ModelEdge[] = []
  for (const f of findings) {
    for (const r of relsOf(f)) {
      if (!visible.has(r.finding_id)) continue
      edges.push({
        id: `strong:${r.finding_id}->${f.id}`,
        source: r.finding_id, target: f.id, kind: "strong",
        label: r.note || "",
      })
    }
  }
  return dedupEdges(edges)
}

/**
 * 链边：每条链按 seq 相邻、两端都是当前可见 finding 时画一条。
 * 非 finding 实体 / 已删实体 / 不可见 finding 只是打断，不影响其它段。
 */
export function buildChainEdges(chains: Chain[], visibleIds: Set<string>): ModelEdge[] {
  const edges: ModelEdge[] = []
  for (const ch of chains) {
    const links = [...(ch.links ?? [])]
      .filter((l) => !l.deleted && l.node_type === "finding" && l.entity)
      .sort((a, b) => a.seq - b.seq)
    for (let i = 1; i < links.length; i++) {
      const prev = links[i - 1]
      const cur = links[i]
      if (!visibleIds.has(prev.node_id) || !visibleIds.has(cur.node_id)) continue
      edges.push({
        id: `chain:${ch.id}:${cur.id}`,
        source: prev.node_id, target: cur.node_id, kind: "chain",
        label: cur.edge_note || "",
        chainId: ch.id, chainStatus: ch.status, chainName: ch.name, linkId: cur.id,
      })
    }
  }
  return dedupEdges(edges)
}

function dedupEdges(edges: ModelEdge[]): ModelEdge[] {
  const byKey = new Map<string, ModelEdge>()
  for (const e of edges) {
    const k = `${e.kind}:${e.source}->${e.target}:${e.chainId ?? ""}`
    if (!byKey.has(k)) byKey.set(k, e)
  }
  return [...byKey.values()]
}
