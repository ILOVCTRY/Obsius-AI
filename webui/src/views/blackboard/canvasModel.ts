import type { Asset, Chain, Finding } from "@/lib/types"

// 评估攻击链画布的纯布局/边推导（E1，DESIGN §12 评估画布）。
// 不碰 React：给定 findings（已过 IP/sev/status 筛选）+ assets + chains 详情，
// 产出泳道/列归属与三级边。React Flow 节点组装在 FindingsCanvas.tsx。

export const UNASSIGNED = "__unassigned__"

export const CARD_W = 224          // w-56
export const COL_W = 252           // 列宽（卡 224 + 列间距 28）
export const LANE_GAP = 36
export const LANE_W = COL_W * 4    // 四列严重度带（info/low｜medium｜high｜critical）
export const HEADER_H = 40
export const CARD_STEP = 118       // 同列卡纵向步距
export const CARD_Y0 = 92          // 顶部留 44px 给画布工具条：泳道头 y=44/高40

export const SEV_RANK: Record<string, number> = {
  critical: 0, high: 1, medium: 2, low: 3, info: 4,
}

// info/low｜medium｜high｜critical 四带
export function sevColumn(sev: string): 0 | 1 | 2 | 3 {
  if (sev === "medium") return 1
  if (sev === "high") return 2
  if (sev === "critical") return 3
  return 0
}

export interface Lane {
  key: string          // host asset id，或 UNASSIGNED
  label: string        // IP（未归属=「未归属」）
  x: number
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
  /** finding.id → {laneX, col, y（同列按 created_at 升序的堆叠序）} */
  place: Map<string, { x: number; y: number; laneKey: string; col: 0 | 1 | 2 | 3 }>
  /** finding.id → host id/UNASSIGNED */
  laneOf: Map<string, string>
}

export function buildLanes(findings: Finding[], assets: Asset[]): LaneModel {
  const byId = new Map(assets.map((a) => [a.id, a]))
  const laneOf = new Map<string, string>()
  const hosts = new Map<string, Asset>()
  for (const f of findings) {
    const hk = hostOf(f, byId)
    laneOf.set(f.id, hk)
    if (hk !== UNASSIGNED) {
      const h = byId.get(hk)
      if (h) hosts.set(hk, h)
    }
  }
  // 泳道按 IP 排序，「未归属」垫底
  const hostLanes = [...hosts.values()].sort((a, b) => a.value.localeCompare(b.value))
  const keys = hostLanes.map((h) => h.id)
  if (laneOfContains(laneOf, UNASSIGNED)) keys.push(UNASSIGNED)
  const lanes: Lane[] = keys.map((k, i) => ({
    key: k,
    label: k === UNASSIGNED ? "未归属" : (byId.get(k)?.value ?? k),
    x: i * (LANE_W + LANE_GAP),
  }))

  // 泳道 × 列内按 created_at 升序堆叠（早→晚自上而下）
  const counters = new Map<string, number>()
  const place = new Map<string, { x: number; y: number; laneKey: string; col: 0 | 1 | 2 | 3 }>()
  const laneX = new Map(lanes.map((l) => [l.key, l.x]))
  const sorted = [...findings].sort((a, b) =>
    a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id))
  for (const f of sorted) {
    const lk = laneOf.get(f.id)!
    const col = sevColumn(f.severity)
    const ck = `${lk}:${col}`
    const idx = counters.get(ck) ?? 0
    counters.set(ck, idx + 1)
    place.set(f.id, {
      x: (laneX.get(lk) ?? 0) + col * COL_W,
      y: CARD_Y0 + idx * CARD_STEP,
      laneKey: lk, col,
    })
  }
  return { lanes, place, laneOf }
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
