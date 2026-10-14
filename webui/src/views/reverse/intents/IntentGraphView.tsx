import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, Controls, Handle, useReactFlow,
  MarkerType, Position, type Edge, type Node,
} from "@xyflow/react"
import { Search, ChevronUp, ChevronDown, X } from "lucide-react"
import "@xyflow/react/dist/style.css"
import "../../blackboard/canvas.css" // 必须在 xyflow css 之后：控件主题覆盖
import { api } from "@/lib/api"
import type { IntentGraph, IntentGraphNode } from "@/lib/types"
import { fmtDateTime } from "@/lib/datetime"
import { cn } from "@/lib/utils"

// 项目级意图推导图（2026-10-13，逆向轨「意图」subnav 的「链路」子视图）：
// 样本 → 意图 → 收尾（发现 | 死路），发现可再推导出新意图（可循环延伸）。
// 数据源 GET /projects/{pid}/intent-graph（core/blackboard/intentgraph.py，纯逻辑层）。
// 与渗透 AttackPath 的差别：项目级（多样本为多根）、无子目标层、**无执行层**
// （逆向资产是 binary，http_history 按 host 匹配恒空）。画布单向只读。
// 与「攻击链」subnav 的区别：那是**人工**线性建链，这里是 Agent 意图自动派生。
// Ctrl+F（或 ⌘F）就地搜索意图陈述/维度：命中计数 + 上一个/下一个 + 视图居中，
// 抢在浏览器原生查找之前（window capture 阶段 preventDefault）。

const UNATTACHED = "__unattached__" // 后端兜底样本节点 id（intentgraph.UNATTACHED_ID）

const X_STEP = 300
const CARD_W = 248
const Y_GAP = 20
const SAMPLE_H = 64
const INTENT_H = 132
const FINDING_H = 72

const SEV_COLOR: Record<string, string> = {
  critical: "var(--viz-sev-critical)", high: "var(--viz-sev-high)",
  medium: "var(--viz-sev-medium)", low: "var(--viz-sev-low)",
  info: "var(--viz-sev-info)",
}
const SEV_LABEL: Record<string, string> = {
  critical: "严重", high: "高危", medium: "中危", low: "低危", info: "提示",
}

const OUTCOME_BADGE: Record<string, { color: string; background: string; label: string }> = {
  vuln: { color: "var(--viz-sev-critical)", background: "var(--viz-sev-critical-bg)", label: "漏洞" },
  finding: { color: "var(--viz-node-finding)", background: "var(--viz-node-finding-bg)", label: "有效发现" },
  dead_end: { color: "var(--viz-node-deadend)", background: "var(--viz-node-deadend-bg)", label: "死路" },
}

function heightOf(n: IntentGraphNode): number {
  return n.type === "sample" ? SAMPLE_H : n.type === "intent" ? INTENT_H : FINDING_H
}

/** 按依赖层级自动分层 + 自底向上 tidy 落位（口径同 AttackPath.layoutGraph，
 *  差别是根为**多根森林**——每个样本一棵树）。 */
function layoutGraph(data: IntentGraph, showDeadEnd: boolean): Node[] {
  const visible = data.nodes.filter((n) => showDeadEnd || !n.default_hidden)
  const visIds = new Set(visible.map((n) => n.id))
  const byId = new Map(visible.map((n) => [n.id, n]))

  const pred: Record<string, string[]> = {}
  const children = new Map<string, string[]>()
  visIds.forEach((id) => { pred[id] = []; children.set(id, []) })
  for (const e of data.edges) {
    if (!visIds.has(e.source) || !visIds.has(e.target)) continue
    pred[e.target].push(e.source)
    const arr = children.get(e.source)!
    if (!arr.includes(e.target)) arr.push(e.target)
  }

  // 层级 = 最长前驱路径（bypass 参与，死路隐藏时保持连通）
  const level: Record<string, number> = {}
  const calc = (id: string, stack: Set<string>): number => {
    if (level[id] != null) return level[id]
    if (stack.has(id)) return 0 // 防御（服务端另有 Kahn 断言）
    stack.add(id)
    const ps = pred[id]
    level[id] = ps.length ? Math.max(...ps.map((p) => calc(p, stack))) + 1 : 0
    stack.delete(id)
    return level[id]
  }
  visible.forEach((n) => calc(n.id, new Set()))

  // DFS 建树：同父子块连续；DAG 共享节点取首次到访的父
  const treeKids = new Map<string, string[]>()
  visIds.forEach((id) => treeKids.set(id, []))
  const order: string[] = []
  const seen = new Set<string>()
  const visit = (id: string, parent: string | null) => {
    if (seen.has(id)) return
    seen.add(id)
    order.push(id)
    if (parent) treeKids.get(parent)!.push(id)
    for (const c of children.get(id) ?? []) visit(c, id)
  }
  // 多根：无前驱者；「未归属样本」排最后（它不是真样本，别抢头条）
  const roots = visible.filter((n) => !pred[n.id].length).map((n) => n.id)
    .sort((a, b) =>
      Number(a === UNATTACHED) - Number(b === UNATTACHED) || a.localeCompare(b))
  for (const r of roots) visit(r, null)
  for (const n of visible) visit(n.id, null)

  const subH = new Map<string, number>()
  const subtreeH = (id: string): number => {
    const cached = subH.get(id)
    if (cached != null) return cached
    const kids = treeKids.get(id) ?? []
    let h = heightOf(byId.get(id)!)
    if (kids.length) {
      let kh = 0
      kids.forEach((k, i) => { kh += subtreeH(k) + (i ? Y_GAP : 0) })
      h = Math.max(h, kh)
    }
    subH.set(id, h)
    return h
  }

  const yOf = new Map<string, number>()
  const place = (id: string, yTop: number) => {
    const n = byId.get(id)!
    const kids = treeKids.get(id) ?? []
    let y = yTop
    kids.forEach((k, i) => {
      if (i) y += Y_GAP
      place(k, y)
      y += subtreeH(k)
    })
    // 父卡垂直居中于子块；子块矮于卡片时只下移、绝不向上溢出（同 AttackPath 修重叠口径）
    yOf.set(id, kids.length
      ? yTop + Math.max(0, (y - yTop - heightOf(n)) / 2)
      : yTop)
  }
  let cursor = 40
  for (const r of roots) {
    place(r, cursor)
    cursor += subtreeH(r) + Y_GAP
  }
  const minY = Math.min(...order.map((id) => yOf.get(id) ?? 40))
  const shift = minY < 20 ? 20 - minY : 0

  return order.map((id) => {
    const n = byId.get(id)!
    return {
      id, type: n.type,
      position: { x: (level[id] ?? 0) * X_STEP + 20, y: (yOf.get(id) ?? 40) + shift },
      data: { node: n },
      draggable: false,
    }
  })
}

// ---------- 节点 ----------

function SampleNode({ data }: { data: Record<string, unknown> }) {
  const n = data.node as IntentGraphNode
  const stats = (data.stats ?? { intents: 0, findings: 0 }) as { intents: number; findings: number }
  const isBinary = n.asset_type === "binary"
  return (
    <div
      className="flex flex-col justify-center rounded-md border bg-(--viz-card) px-3 py-2"
      style={{ width: CARD_W, height: SAMPLE_H, borderColor: "var(--viz-target-border)" }}
    >
      <div className="flex items-center gap-1.5">
        <span className="shrink-0 rounded px-1 text-[9px]"
              style={{ background: "var(--viz-sev-info-bg)", color: "var(--viz-text-muted)" }}>
          {n.asset_type || "?"}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11.5px]" title={n.value || n.label}>
          {n.label}
        </span>
      </div>
      <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted-foreground">
        {isBinary
          ? <span className="truncate font-mono opacity-70">{(n.value ?? "").slice(0, 16)}…</span>
          : <span className="truncate opacity-70">非二进制锚点</span>}
        {!!(stats.intents || stats.findings) && (
          <span className="ml-auto shrink-0">
            意图 {stats.intents} · 发现 {stats.findings}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Right} style={{ background: "var(--viz-handle)" }} />
    </div>
  )
}

function IntentNode({ data }: { data: Record<string, unknown> }) {
  const n = data.node as IntentGraphNode
  const selected = !!data.selected
  const matched = !!data.matched // Ctrl+F 当前命中项
  const isDeadEnd = n.outcome === "dead_end"
  const badge = n.outcome ? OUTCOME_BADGE[n.outcome] : null
  return (
    <button
      type="button"
      onClick={() => (data.onSelect as (n: IntentGraphNode) => void)(n)}
      className={cn("flex flex-col rounded-md border bg-(--viz-card) px-3 py-2 text-left",
        "transition-colors hover:bg-(--viz-card-hover)")}
      style={{
        width: CARD_W, height: INTENT_H,
        borderColor: selected ? "var(--viz-target-border)"
          : isDeadEnd ? "var(--viz-handle)"
          : n.status === "open" ? "var(--viz-target-border)" : "var(--viz-edge)",
        borderStyle: isDeadEnd ? "dashed" : "solid",
        borderWidth: selected ? 2 : 1,
        // 搜索命中：橙色外环（区别于蓝调的选中边框，一眼可辨）
        boxShadow: matched ? "0 0 0 2px var(--viz-accent-orange)" : undefined,
      }}
    >
      <div className="flex items-center gap-1.5">
        <span
          className={cn("size-1.5 rounded-full", n.status === "open" && "animate-pulse")}
          style={{ background: n.status === "open" ? "var(--viz-sev-low)" : "var(--viz-sev-info)" }}
        />
        <span className="text-[10px] text-muted-foreground">
          {n.status === "open" ? "待收尾" : "已收尾"}
        </span>
        <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground/70">
          {n.created_at ? fmtDateTime(n.created_at).slice(5, 16) : ""}
        </span>
      </div>
      <p
        className="mt-1.5 min-h-0 flex-1 overflow-hidden break-words text-[11.5px] leading-snug [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:4]"
        title={n.statement}
      >
        {n.statement}
      </p>
      <div className="flex items-center gap-1 pt-0.5">
        {badge
          ? <span className="rounded px-1 text-[10px]"
                  style={{ background: badge.background, color: badge.color }}>
              {badge.label}×{n.finding_ids?.length || 0}
            </span>
          : <span className="text-[10px] text-muted-foreground">假设待验证</span>}
      </div>
      <Handle type="target" position={Position.Left} style={{ background: "var(--viz-handle)" }} />
      <Handle type="source" position={Position.Right} style={{ background: "var(--viz-handle)" }} />
    </button>
  )
}

function FindingNode({ data }: { data: Record<string, unknown> }) {
  const n = data.node as IntentGraphNode
  const color = SEV_COLOR[n.severity ?? "info"] ?? "var(--viz-sev-info)"
  return (
    <button
      type="button"
      onClick={() => (data.onLocate as (id: string) => void)(n.id)}
      title="回到「逆向分析」查看这条发现"
      className="flex flex-col justify-center rounded-md border bg-(--viz-card) px-3 py-1.5 text-left transition-colors hover:bg-(--viz-card-hover)"
      style={{ width: CARD_W, height: FINDING_H, borderColor: color }}
    >
      <div className="flex items-center gap-1.5">
        <span className="shrink-0 text-[10px] font-medium" style={{ color }}>
          {SEV_LABEL[n.severity ?? "info"] ?? n.severity}
        </span>
        <span className="shrink-0 rounded px-1 text-[9px]"
              style={{
                background: n.category === "vuln" ? "var(--viz-sev-critical-bg)" : "var(--viz-node-finding-bg)",
                color: n.category === "vuln" ? "var(--viz-sev-critical)" : "var(--viz-node-finding)",
              }}>
          {n.category === "vuln" ? "漏洞" : "有效发现"}
        </span>
        {n.status === "verified" && (
          <span className="shrink-0 rounded border px-1 text-[9px] text-muted-foreground">verified</span>
        )}
      </div>
      <p className="mt-1 min-h-0 overflow-hidden break-words text-[11px] leading-snug [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2]"
         title={n.title}>
        {n.title}
      </p>
      <Handle type="target" position={Position.Left} style={{ background: "var(--viz-handle)" }} />
      {/* 发现也可作后继意图的推导依据（finding → intent 的 derive 边），故两侧都有 handle */}
      <Handle type="source" position={Position.Right} style={{ background: "var(--viz-handle)" }} />
    </button>
  )
}

const nodeTypes = { sample: SampleNode, intent: IntentNode, finding: FindingNode }

// ---------- 主画布 ----------

function Canvas({ pid, onLocateFinding, onChanged }: {
  pid: string
  onLocateFinding: (findingId: string) => void
  onChanged?: () => void
}) {
  const [data, setData] = useState<IntentGraph | null>(null)
  const [selIntent, setSelIntent] = useState<IntentGraphNode | null>(null)
  const [showDeadEnd, setShowDeadEnd] = useState(false) // 死路默认隐藏（同 AttackPath）
  const [busy, setBusy] = useState(false)
  const [loadErr, setLoadErr] = useState("")

  const { setCenter } = useReactFlow()

  // ---- Ctrl+F 意图搜索（命中计数 + 上一个/下一个 + 视图居中）----
  const [findOpen, setFindOpen] = useState(false)
  const [findQ, setFindQ] = useState("")
  const [findIdx, setFindIdx] = useState(0)
  const findInputRef = useRef<HTMLInputElement | null>(null)

  const reload = useCallback(() =>
    api.intentGraph(pid)
      .then((d) => { setData(d); setLoadErr("") })
      .catch((e) => setLoadErr(e instanceof Error ? e.message : String(e))), [pid])

  useEffect(() => {
    let alive = true
    const load = () => { if (alive) void reload() }
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [reload])

  // 样本 → 名下意图/发现计数（节点上直读，省得点开才知道有没有料）
  const sampleStats = useMemo(() => {
    const m = new Map<string, { intents: number; findings: number }>()
    for (const n of data?.nodes ?? []) {
      if (n.type === "sample" || !n.sample_id) continue
      const cur = m.get(n.sample_id) ?? { intents: 0, findings: 0 }
      if (n.type === "intent") cur.intents += 1
      else cur.findings += 1
      m.set(n.sample_id, cur)
    }
    return m
  }, [data])

  const laid = useMemo(
    () => (data ? layoutGraph(data, showDeadEnd) : []), [data, showDeadEnd])

  // ---- 搜索：坐标镜像到 ref，让「居中定位」保持稳定引用（4s 轮询不会重复居中）----
  const posRef = useRef<Map<string, { x: number; y: number }>>(new Map())
  useEffect(() => {
    posRef.current = new Map(laid.map((n) => [n.id, n.position]))
  }, [laid])

  // 命中集：只搜意图节点（陈述 + 维度），按画布顺序
  const matchIds = useMemo(() => {
    const q = findQ.trim().toLowerCase()
    if (!q) return []
    return laid
      .filter((n) => n.type === "intent")
      .filter((n) => {
        const g = (n.data as { node: IntentGraphNode }).node
        return (g.statement ?? "").toLowerCase().includes(q)
          || (g.dimension ?? "").toLowerCase().includes(q)
      })
      .map((n) => n.id)
  }, [laid, findQ])

  // 取模循环：结果集变小时自动回绕，索引不会越界
  const effIdx = matchIds.length
    ? ((findIdx % matchIds.length) + matchIds.length) % matchIds.length : 0
  const curMatchId = findOpen ? matchIds[effIdx] : undefined

  const focusMatch = useCallback((id: string) => {
    const p = posRef.current.get(id)
    if (!p) return
    void setCenter(p.x + CARD_W / 2, p.y + INTENT_H / 2, { zoom: 1, duration: 450 })
  }, [setCenter])

  useEffect(() => { if (curMatchId) focusMatch(curMatchId) }, [curMatchId, focusMatch])

  const openFind = useCallback(() => {
    setFindOpen(true)
    requestAnimationFrame(() => findInputRef.current?.select())
  }, [])
  const closeFind = useCallback(() => {
    setFindOpen(false); setFindQ(""); setFindIdx(0)
  }, [])
  const stepFind = useCallback((d: number) => setFindIdx((i) => i + d), [])

  // 全局 Ctrl+F / ⌘F：capture 阶段 preventDefault，抢在浏览器原生查找之前
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "f" || e.key === "F")) {
        e.preventDefault()
        openFind()
      } else if (e.key === "Escape" && findOpen) {
        e.preventDefault()
        closeFind()
      }
    }
    window.addEventListener("keydown", onKey, true)
    return () => window.removeEventListener("keydown", onKey, true)
  }, [openFind, closeFind, findOpen])

  const onFindKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") { e.preventDefault(); stepFind(e.shiftKey ? -1 : 1) }
  }

  const rfNodes: Node[] = useMemo(() => laid.map((n) => {
    if (n.type === "sample") {
      n.data = { ...n.data, stats: sampleStats.get(n.id) ?? { intents: 0, findings: 0 } }
    } else if (n.type === "intent") {
      n.data = {
        ...n.data, selected: selIntent?.id === n.id,
        matched: curMatchId === n.id, onSelect: setSelIntent,
      }
    } else if (n.type === "finding") {
      n.data = { ...n.data, onLocate: onLocateFinding }
    }
    return n
  }), [laid, sampleStats, selIntent, onLocateFinding, curMatchId])

  const nodeById = useMemo(
    () => new Map((data?.nodes ?? []).map((n) => [n.id, n])), [data])

  const rfEdges: Edge[] = useMemo(() => {
    if (!data) return []
    const out: Edge[] = []
    const mk = (id: string, source: string, target: string, style: CSSProperties): Edge => ({
      id, source, target, type: "smoothstep", style,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: (style.stroke as string) || "var(--viz-handle)", width: 14, height: 14,
      },
    })
    for (const e of data.edges) {
      if (e.kind === "derive") {
        out.push(mk(`${e.source}->${e.target}:derive`, e.source, e.target,
          { stroke: "var(--viz-sev-low)", strokeWidth: 1.3, opacity: 0.7 }))
      } else if (e.kind === "outcome") {
        const color = nodeById.get(e.target)?.category === "vuln"
          ? "var(--viz-sev-critical)" : "var(--viz-success)"
        out.push(mk(`${e.source}->${e.target}:outcome`, e.source, e.target,
          { stroke: color, strokeWidth: 1.6, opacity: 0.85 }))
      } else if (e.kind === "bypass") {
        out.push(mk(`${e.source}->${e.target}:bypass`, e.source, e.target,
          { stroke: "var(--viz-sev-info)", strokeWidth: 1.1, opacity: 0.5, strokeDasharray: "5 4" }))
      }
    }
    return out
  }, [data, nodeById])

  const reopen = async (id: string) => {
    setBusy(true)
    try {
      await api.reopenIntent(pid, id, "意图链路视图重开")
      setSelIntent(null)
      await reload()
      onChanged?.()
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const empty = !!data && data.counts.intents === 0

  return (
    <div className="fc-dark absolute inset-0 bg-(--viz-canvas)">
      <div className="absolute inset-x-0 top-0 z-10 flex flex-wrap items-center gap-3 border-b bg-(--viz-card) px-3 py-1.5 text-[11px]">
        <span className="text-muted-foreground">样本 → 意图 → 收尾（发现｜死路）</span>
        {data && (
          <span className="shrink-0 text-muted-foreground">
            样本 {data.counts.samples} · 意图 {data.counts.intents}（待收尾 {data.counts.open}）·
            发现 {data.counts.findings}
          </span>
        )}
        {data && data.counts.dead_end > 0 && (
          <label className="flex shrink-0 cursor-pointer items-center gap-1 text-muted-foreground hover:text-foreground">
            <input type="checkbox" checked={showDeadEnd}
                   onChange={(e) => setShowDeadEnd(e.target.checked)} className="size-3" />
            死路×{data.counts.dead_end}
          </label>
        )}
        <button
          type="button"
          onClick={openFind}
          title="搜索意图（Ctrl+F）"
          className="ml-auto flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-accent/40 hover:text-foreground"
        >
          <Search className="size-3" />
          搜索意图
          <kbd className="rounded border px-1 text-[9px] opacity-70">Ctrl+F</kbd>
        </button>
        <span className="hidden shrink-0 text-[10px] text-muted-foreground lg:inline">
          点发现节点回分析视图 · 点意图节点看收尾详情
        </span>
        {loadErr && <span className="shrink-0 text-[10px] text-(--status-error)">{loadErr}</span>}
      </div>

      {findOpen && (
        <div
          className="absolute z-30 flex items-center gap-1 rounded-md border bg-(--viz-card) px-2 py-1 shadow-lg"
          style={{ top: 38, right: selIntent ? 332 : 12 }} // 详情面板打开时左移，别压住它
        >
          <Search className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            ref={findInputRef}
            autoFocus
            value={findQ}
            spellCheck={false}
            onChange={(e) => { setFindQ(e.target.value); setFindIdx(0) }}
            onKeyDown={onFindKey}
            placeholder="搜索意图陈述 / 维度…"
            className="w-56 bg-transparent text-[11px] outline-none placeholder:text-muted-foreground"
          />
          <span className="shrink-0 tabular-nums text-[10px] text-muted-foreground">
            {findQ.trim()
              ? (matchIds.length ? `${effIdx + 1}/${matchIds.length}` : "无命中")
              : ""}
          </span>
          <button type="button" disabled={!matchIds.length} onClick={() => stepFind(-1)}
                  title="上一个（Shift+Enter）"
                  className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30">
            <ChevronUp className="size-3.5" />
          </button>
          <button type="button" disabled={!matchIds.length} onClick={() => stepFind(1)}
                  title="下一个（Enter）"
                  className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30">
            <ChevronDown className="size-3.5" />
          </button>
          <button type="button" onClick={closeFind} title="关闭（Esc）"
                  className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground">
            <X className="size-3.5" />
          </button>
        </div>
      )}

      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        onlyRenderVisibleElements
        fitView
        fitViewOptions={{ padding: 0.12, maxZoom: 1.1 }}
        minZoom={0.2}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--viz-edge)" gap={20} size={1} />
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>

      {empty && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-(--viz-edge) px-4 py-2 text-center text-xs text-muted-foreground">
            还没有意图，画不出推导链<br />
            <span className="text-[10px]">Agent 用 declare_intent 声明假设、收尾为发现/死路后自动生长</span>
          </p>
        </div>
      )}

      {selIntent && (
        <aside className="absolute inset-y-0 right-0 z-20 flex w-80 flex-col border-l bg-(--viz-card)">
          <div className="flex items-center gap-2 border-b px-3 py-2">
            <span className="text-[11px] font-medium">意图详情</span>
            <span className={cn("text-[10px]",
              selIntent.status === "open" ? "text-(--status-warning)" : "text-muted-foreground")}>
              {selIntent.status === "open" ? "待收尾" : "已收尾"}
            </span>
            <button type="button" onClick={() => setSelIntent(null)}
                    className="ml-auto rounded px-1.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground">
              关闭
            </button>
          </div>
          <div className="min-h-0 flex-1 space-y-3 overflow-auto p-3 text-[11px]">
            <p className="leading-relaxed">{selIntent.statement}</p>
            <div className="space-y-1 text-[10px] text-muted-foreground">
              {selIntent.outcome && (
                <div className="flex items-center gap-1.5">
                  <span>收尾</span>
                  <span className="rounded px-1"
                        style={{
                          background: OUTCOME_BADGE[selIntent.outcome]?.background,
                          color: OUTCOME_BADGE[selIntent.outcome]?.color,
                        }}>
                    {OUTCOME_BADGE[selIntent.outcome]?.label}
                  </span>
                  {!!selIntent.finding_ids?.length && <span>×{selIntent.finding_ids.length}</span>}
                </div>
              )}
              {selIntent.dimension && <div>维度 {selIntent.dimension}</div>}
              <div className="font-mono">{selIntent.author}</div>
              <div>声明 {fmtDateTime(selIntent.created_at ?? "")}</div>
              {selIntent.closed_at && <div>收尾 {fmtDateTime(selIntent.closed_at)}</div>}
            </div>
            {selIntent.dead_reason && (
              <div>
                <div className="mb-1 text-[10px] text-muted-foreground">死因</div>
                <p className="text-muted-foreground">{selIntent.dead_reason}</p>
              </div>
            )}
            {!!selIntent.finding_ids?.length && (
              <div className="space-y-1">
                <div className="text-[10px] text-muted-foreground">收尾发现</div>
                {selIntent.finding_ids.map((fid) => {
                  const f = nodeById.get(fid)
                  return (
                    <button key={fid} type="button" onClick={() => onLocateFinding(fid)}
                            title="回分析视图查看"
                            className="block w-full truncate rounded border px-2 py-1 text-left hover:bg-accent/40">
                      <span className="mr-1 text-[9px]" style={{ color: SEV_COLOR[f?.severity ?? "info"] }}>
                        {SEV_LABEL[f?.severity ?? "info"]}
                      </span>
                      {f?.title ?? fid}
                    </button>
                  )
                })}
              </div>
            )}
          </div>
          {selIntent.status === "closed" && (
            <div className="border-t p-2">
              <button type="button" disabled={busy} onClick={() => void reopen(selIntent.id)}
                      className="w-full rounded border px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50">
                {busy ? "重开中…" : "重开意图（收尾被否决 / 有新证据）"}
              </button>
            </div>
          )}
        </aside>
      )}
    </div>
  )
}

export function IntentGraphView(props: {
  pid: string
  onLocateFinding: (findingId: string) => void
  onChanged?: () => void
}) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  )
}
