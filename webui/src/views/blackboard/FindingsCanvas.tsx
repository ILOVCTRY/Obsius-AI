import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, MiniMap, Controls,
  MarkerType, useReactFlow,
  type Connection, type Edge, type Node, type NodeChange,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "./canvas.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import { Maximize2, Minimize2, Map as MapIcon } from "lucide-react"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import type { Asset, Chain, ChainStatus, ChainSummary, Finding } from "@/lib/types"
import { FindingDetailDialog } from "@/components/blackboard/FindingDetailDialog"
import {
  FindingNode, LaneBackground, LaneHeader,
  type FindingFlowNode, type LaneHeaderNode,
} from "./FindingNode"
import { FindingEdge, type FindingFlowEdge, type FindingRoute } from "./FindingEdge"
import {
  buildChainEdges, buildLanes, buildStrongEdges, buildWeakEdges,
  isRightward, longRunY, CARD_W, type ModelEdge,
} from "./canvasModel"
import { AddChainEdgeDialog, type ChainEdgeRequest } from "./AddChainEdgeDialog"
import { ChainToolbar, STATUS_DOT } from "./ChainToolbar"

// 评估攻击链画布（E1，DESIGN §12 评估画布）：
// host IP 泳道 × 严重度四列 × 时间堆叠；弱边/relates_to 强边/chain 边三级；
// 单向零重叠（findings-canvas-dag-layout，2026-09-22）：布局锁定网格不可拖（D3），
// 边注记 hover/选中才显（D1），MiniMap 默认收起（D4），弱边只画右向、长边空行带
// 路由不穿卡（R1/R2）。
// 手拖连线/强边快捷确认 → AddChainEdgeDialog（edge_note 必填）→ 现成 chains API。
// 由 Blackboard 发现 tab 的「链路」子视图懒加载（@xyflow/react 不进主包）。

const nodeTypes = { finding: FindingNode, laneHeader: LaneHeader, laneBg: LaneBackground }
const edgeTypes = { findingEdge: FindingEdge }

function Canvas({ pid, findings, assets, assetFilter, onMutated, track }: {
  pid: string
  findings: Finding[]      // 已过 IP/sev/status 共享筛选
  assets: Asset[]
  assetFilter?: string     // C2 降噪：选中 host → 泳道模式；空 → 全局严重度分块
  onMutated: () => void
  track?: string
}) {
  const wrapperRef = useRef<HTMLDivElement>(null)
  const rf = useReactFlow()

  // chains（自带 4s 轮询；findings 由父级轮询推入）
  const [summaries, setSummaries] = useState<ChainSummary[]>([])
  const [details, setDetails] = useState<Map<string, Chain>>(new Map())
  const reloadChains = useCallback(() => {
    api.chains(pid).then(async (list) => {
      setSummaries(list)
      const rs = await Promise.allSettled(list.map((c) => api.chain(pid, c.id)))
      const m = new Map<string, Chain>()
      rs.forEach((r, i) => { if (r.status === "fulfilled") m.set(list[i].id, r.value) })
      setDetails(m)
    }).catch(() => {})
  }, [pid])
  useEffect(() => {
    reloadChains()
    const t = setInterval(reloadChains, 4000)
    return () => clearInterval(t)
  }, [reloadChains])

  const [selectedChainId, setSelectedChainId] = useState("")
  useEffect(() => {
    if (!selectedChainId || !summaries.some((c) => c.id === selectedChainId)) {
      setSelectedChainId(summaries[0]?.id ?? "")
    }
  }, [summaries, selectedChainId])

  // D4：MiniMap 默认收起，工具条开关（localStorage 记忆）
  const [miniOpen, setMiniOpen] = useState<boolean>(() => {
    try { return localStorage.getItem(`findings-minimap:${pid}`) === "1" } catch { return false }
  })
  const changeMiniOpen = useCallback((v: boolean) => {
    setMiniOpen(v)
    try { localStorage.setItem(`findings-minimap:${pid}`, v ? "1" : "0") } catch { /* 忽略 */ }
  }, [pid])

  // C2 边显示三态：全部（含弱边）/ 仅强边（弱边隐藏，缺省）/ 仅链边（只看人工确认链）
  type EdgeMode = "all" | "strong" | "chain"
  const EDGE_MODE_KEY = (pid: string) => `findings-edgemode:${pid}`
  const [edgeMode, setEdgeMode] = useState<EdgeMode>(() => {
    try { return (localStorage.getItem(EDGE_MODE_KEY(pid)) as EdgeMode) || "strong" } catch { return "strong" }
  })
  const changeEdgeMode = useCallback((m: EdgeMode) => {
    setEdgeMode(m)
    try { localStorage.setItem(EDGE_MODE_KEY(pid), m) } catch { /* 忽略 */ }
  }, [pid])
  const [focusOn, setFocusOn] = useState(true)     // C2 聚焦：hover/点选节点 → 只亮一跳邻接
  // F13 孤立节点开关：默认隐藏真孤点（连通分量=1：噪声/孤立分区），localStorage 持久
  const [showIsolated, setShowIsolated] = useState<boolean>(() => {
    try { return localStorage.getItem(`findings-showisolated:${pid}`) === "1" } catch { return false }
  })
  const changeShowIsolated = useCallback((v: boolean) => {
    setShowIsolated(v)
    try { localStorage.setItem(`findings-showisolated:${pid}`, v ? "1" : "0") } catch { /* 忽略 */ }
  }, [pid])
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null)  // D1：注记 hover 才显
  const [detail, setDetail] = useState<Finding | null>(null)
  const [dlg, setDlg] = useState<ChainEdgeRequest | null>(null)
  const [mutating, setMutating] = useState(false)
  const [isFs, setIsFs] = useState(false)
  useEffect(() => {
    const h = () => setIsFs(!!document.fullscreenElement)
    document.addEventListener("fullscreenchange", h)
    return () => document.removeEventListener("fullscreenchange", h)
  }, [])

  // C2：强/链边先算——供 buildLanes 重心排序减少交叉（弱边依赖 laneOf 在模型之后）
  const strongEdgesPre = useMemo(() => buildStrongEdges(findings), [findings])
  const chainEdgesPre = useMemo(
    () => buildChainEdges([...details.values()], new Set(findings.map((f) => f.id))),
    [details, findings])
  const globalLayout = !assetFilter
  const model = useMemo(
    () => buildLanes(findings, assets, {
      globalLayout, orderEdges: [...strongEdgesPre, ...chainEdgesPre],
      showIsolated,
    }),
    [findings, assets, globalLayout, showIsolated, strongEdgesPre, chainEdgesPre])
  const assetById = useMemo(() => new Map(assets.map((a) => [a.id, a])), [assets])
  // F13：真孤点计数（工具条开关显示；隐藏时这些发现不进 place，节点组装自然跳过）
  const isolatedCount = model.isolatedIds.size

  // 受控 ReactFlow 契约：节点由 model 派生，但内部测量变化必须回写——否则任一重渲染
  // 都会用不带 measured 的 props 节点把内部 measured/handleBounds 清空（xyflow 对受控
  // 节点重建 internalNode），边会因此被整体卸载。D3 锁定网格后 position change 不再产生
  // （拖拽退役），只回收 dimensions。
  const [measuredById, setMeasuredById] = useState<Record<string, { width: number; height: number }>>({})
  const onNodesChangeCb = useCallback((changes: NodeChange[]) => {
    let dims: Record<string, { width: number; height: number }> | null = null
    for (const ch of changes) {
      if (ch.type === "dimensions" && ch.dimensions?.width && ch.dimensions?.height) {
        ;(dims ??= {})[ch.id] = { width: ch.dimensions.width, height: ch.dimensions.height }
      }
    }
    if (dims) setMeasuredById((m) => ({ ...m, ...dims }))
  }, [])

  // ---------- 边 ----------
  const edges = useMemo(() => {
    const weak = edgeMode === "all" ? buildWeakEdges(findings, model.laneOf) : []
    const strong = edgeMode === "chain" ? [] : strongEdgesPre
    const chain = edgeMode === "all" || edgeMode === "strong" ? chainEdgesPre : []
    return [...weak, ...strong, ...chain]
  }, [edgeMode, findings, model.laneOf, strongEdgesPre, chainEdgesPre])

  // R1/R2 边路由辅助：同走廊（泳道×列间隙）多边垂直段按边序 ±3px 阶梯（方案 §3.2）；
  // g1=源列后走廊（同泳道层差≥1 与跨泳道都用），gd=目标列前走廊（长边/跨泳道用）。
  const corridorOff = useMemo(() => {
    const groups = new Map<string, string[]>()
    const push = (key: string, id: string) => {
      ;(groups.get(key) ?? groups.set(key, []).get(key)!).push(id)
    }
    for (const e of edges) {
      const s = model.place.get(e.source)
      const t = model.place.get(e.target)
      if (!s || !t) continue
      if (s.laneKey === t.laneKey) {
        if (t.col - s.col >= 1) push(`g1:${s.laneKey}:${s.col}`, e.id)
        if (t.col - s.col >= 2) push(`gd:${t.laneKey}:${t.col}`, e.id)
      } else {
        push(`g1:${s.laneKey}:${s.col}`, e.id)
        push(`gd:${t.laneKey}:${t.col}`, e.id)
      }
    }
    const off = new Map<string, { g1: number; gd: number }>()
    for (const [key, ids] of groups) {
      ids.forEach((id, i) => {
        const v = (i - (ids.length - 1) / 2) * 3
        const cur = off.get(id) ?? { g1: 0, gd: 0 }
        off.set(id, key.startsWith("g1:") ? { ...cur, g1: v } : { ...cur, gd: v })
      })
    }
    return off
  }, [edges, model.place])
  // 跨泳道绕行带：取所有泳道内容之下的 y——水平长跑横穿中间泳道也不穿卡
  const crossBandY = useMemo(
    () => (model.lanes.length ? Math.max(...model.lanes.map((l) => l.height)) + 24 : 464),
    [model.lanes])
  const maxRow = useMemo(() => {
    let m = 0
    for (const [, p] of model.place) m = Math.max(m, p.row)
    return m
  }, [model.place])

  // C2 聚焦：hover/点选的当前焦点节点（聚焦关闭时仅点选生效）
  const focusNodeId = focusOn ? (selectedNodeId ?? hoverNodeId) : null

  const adjacent = useMemo(() => {
    const s = new Set<string>()
    if (focusNodeId) {
      s.add(focusNodeId)
      for (const e of edges) {
        if (e.source === focusNodeId) s.add(e.target)
        if (e.target === focusNodeId) s.add(e.source)
      }
    }
    return s
  }, [focusNodeId, edges])

  const selectedEdge: ModelEdge | undefined = useMemo(
    () => edges.find((e) => e.id === selectedEdgeId), [edges, selectedEdgeId])

  const selectEdge = useCallback((id: string) => {
    setSelectedEdgeId(id)
    setSelectedNodeId(null)
  }, [])

  const edgeColor = (e: ModelEdge): string => {
    if (e.kind === "weak") return "#8b949e"
    if (e.kind === "strong") return "#39c5cf"
    return STATUS_DOT[e.chainStatus ?? "hypothesis"]
  }

  // false-positive 端点集合：撤回传播场景下，凡沾上误报节点的引用边都淡出（DESIGN §6.7 的 1.6）
  const fpIds = useMemo(
    () => new Set(findings.filter((f) => f.status === "false-positive").map((f) => f.id)),
    [findings])
  const selectedEdgeStale = !!selectedEdge
    && (fpIds.has(selectedEdge.source) || fpIds.has(selectedEdge.target))

  const flowEdges: FindingFlowEdge[] = useMemo(() => edges.map((e): FindingFlowEdge | null => {
    const connected = !focusNodeId
      || e.source === focusNodeId || e.target === focusNodeId
    const chainDim = e.kind === "chain" && !!selectedChainId && e.chainId !== selectedChainId
    const stale = fpIds.has(e.source) || fpIds.has(e.target)
    const hovered = e.id === hoveredEdgeId
    const dim = (!connected || chainDim) && !hovered  // hover 边在聚焦态下也点亮
    const color = edgeColor(e)
    // 单向零重叠路由（R1/R2）：同泳道层差=1 走中缝、>1 走空行带长跑、跨泳道走
    // 全泳道底绕行带——正交折线，水平段不穿卡（构造保证）。
    const sp = model.place.get(e.source)
    const tp = model.place.get(e.target)
    if (!sp || !tp) return null  // F13：端点被隐藏（孤立节点关闭）→ 不渲染该边
    if (e.kind === "weak" && !isRightward(e, model.place)) return null  // R1② 弱边只画右向
    const offs = corridorOff.get(e.id)
    let route: FindingRoute
    let longY: number | undefined
    if (sp.laneKey !== tp.laneKey) {
      route = "cross"
      longY = crossBandY + (offs?.g1 ?? 0) * 1.5
    } else if (tp.col - sp.col >= 2) {
      route = "long"
      longY = longRunY({
        sCol: sp.col, sRow: sp.row, tCol: tp.col, tRow: tp.row,
        occupiedIn: (col, row) => model.occupied.has(`${sp.laneKey}:${col}:${row}`),
        maxRow,
      }) + (offs?.g1 ?? 0) * 1.5
    } else {
      route = "adjacent"
    }
    return {
      id: e.id, source: e.source, target: e.target, type: "findingEdge",
      data: {
        kind: e.kind, label: e.label, selected: e.id === selectedEdgeId,
        hovered, dimmed: dim, stale, route, longY,
        g1off: offs?.g1, gdoff: offs?.gd, onSelect: selectEdge,
      },
      style: {
        stroke: color,
        strokeWidth: hovered && e.kind !== "weak"
          ? (e.kind === "chain" ? 2.2 : 2)
          : e.kind === "chain" ? 1.8 : e.kind === "strong" ? 1.6 : 1,
        strokeDasharray: stale ? "3 4" : e.kind === "weak" ? "5 4" : undefined,
        opacity: dim ? (focusOn ? 0.08 : 0.15)
          : stale ? 0.25
          : hovered ? (e.kind === "weak" ? 0.85 : 1)
          : e.kind === "weak" ? 0.4 : 0.95,
      },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
      interactionWidth: 14,
    }
  }).filter((e): e is FindingFlowEdge => e !== null),
  [edges, fpIds, focusNodeId, focusOn, hoveredEdgeId, selectedChainId, selectedEdgeId, selectEdge,
   model.place, model.occupied, corridorOff, crossBandY, maxRow])

  // ---------- 节点 ----------
  const openDetail = useCallback((f: Finding) => setDetail(f), [])
  const quickAdd = useCallback((f: Finding) => setDlg({ source: null, target: f }), [])

  const flowNodes = useMemo(() => {
    const nodes: Node[] = []
    // F13：隐藏孤立节点时，无串联发现的空泳道整条跳过（背景/头部都不渲染）
    const laneHasNodes = new Map<string, boolean>()
    for (const [, p] of model.place) laneHasNodes.set(p.laneKey, true)
    // 泳道背景（最底层，不可交互；宽高由模型按实际内容回填）
    model.lanes.forEach((lane) => {
      if (!showIsolated && !laneHasNodes.get(lane.key)) return
      nodes.push({
        id: `bg:${lane.key}`, type: "laneBg", position: { x: lane.x - 12, y: 0 },
        draggable: false, selectable: false, focusable: false,
        data: {}, zIndex: -10,
        measured: measuredById[`bg:${lane.key}`],
        style: { width: lane.width + 32, height: lane.height, pointerEvents: "none" },
      })
    })
    // 泳道头（host IP；布局已是三分区，不再有严重度带标签）
    model.lanes.forEach((lane) => {
      if (!showIsolated && !laneHasNodes.get(lane.key)) return
      const hdr: LaneHeaderNode = {
        id: `lane:${lane.key}`, type: "laneHeader",
        position: { x: lane.x, y: 44 }, draggable: false, selectable: false, focusable: false,
        data: { label: lane.label, width: lane.width },
        measured: measuredById[`lane:${lane.key}`],
      }
      nodes.push(hdr)
    })
    // 发现卡片（D3 锁定网格：位置恒为布局算法产物，不可拖）
    for (const f of findings) {
      const p = model.place.get(f.id)
      if (!p) continue
      const dim = focusNodeId !== null && !adjacent.has(f.id)
      const node: FindingFlowNode = {
        id: f.id, type: "finding",
        position: { x: p.x, y: p.y },
        data: { f, dim, onOpen: openDetail, onQuickAdd: quickAdd },
        measured: measuredById[f.id],
        style: { width: CARD_W },
      }
      nodes.push(node)
    }
    return nodes
  }, [model, findings, focusNodeId, adjacent, openDetail, quickAdd, measuredById])

  const onConnect = useCallback((c: Connection) => {
    if (!c.source || !c.target || c.source === c.target) return
    const s = findings.find((f) => f.id === c.source)
    const t = findings.find((f) => f.id === c.target)
    if (s && t) setDlg({ source: s, target: t })
  }, [findings])

  // ---------- chains 写操作 ----------
  const createChain = useCallback(async (name: string, goal: string) => {
    const ch = await api.createChain(pid, { name, goal: goal || undefined })
    await reloadChains()
    setSelectedChainId(ch.id)
  }, [pid, reloadChains])

  const advanceChain = useCallback(async (next: ChainStatus) => {
    if (!selectedChainId) return
    setMutating(true)
    try {
      await api.updateChain(pid, selectedChainId, { status: next })
      await reloadChains()
    } finally { setMutating(false) }
  }, [pid, selectedChainId, reloadChains])

  const deleteSelectedEdge = useCallback(async () => {
    if (!selectedEdge || selectedEdge.kind !== "chain" || !selectedEdge.linkId) return
    const ok = window.confirm(
      `从链「${selectedEdge.chainName}」移除目标节点？\n（链按 seq 相邻成边，移除后该节点的相邻边一并消失，实体本身不删）`)
    if (!ok) return
    setMutating(true)
    try {
      await api.deleteChainLink(pid, selectedEdge.linkId)
      setSelectedEdgeId(null)
      await reloadChains()
      onMutated()
    } finally { setMutating(false) }
  }, [pid, selectedEdge, reloadChains, onMutated])

  const strongEdgeDlg = useCallback(() => {
    if (!selectedEdge || selectedEdge.kind !== "strong") return
    const s = findings.find((f) => f.id === selectedEdge.source)
    const t = findings.find((f) => f.id === selectedEdge.target)
    if (t) setDlg({ source: s ?? null, target: t, presetNote: selectedEdge.label })
  }, [findings, selectedEdge])

  return (
    <div ref={wrapperRef} className="fc-dark absolute inset-0 bg-[#0d1117]">
      {/* 顶部工具条 */}
      <div className="absolute inset-x-0 top-0 z-20 flex flex-wrap items-center gap-2 border-b border-[#21262d] bg-[#0d1117]/90 px-2 py-1.5">
        <ChainToolbar
          chains={summaries}
          selectedId={selectedChainId}
          onSelect={setSelectedChainId}
          onCreate={createChain}
          onAdvance={advanceChain}
          mutating={mutating}
        />
        <span className="flex-1" />
        {/* F13 孤立节点开关：默认隐藏真孤点（连通分量=1），打开恢复三分区全量 */}
        <button
          type="button"
          title={showIsolated
            ? "孤立节点显示中：点击隐藏无任何连接的发现（噪声/孤立分区）"
            : "孤立节点已隐藏：仅显示成链发现；点击恢复全量"}
          className={cn("rounded border border-[#30363d] px-2 py-0.5 text-[11px] transition-colors",
            showIsolated ? "text-muted-foreground hover:bg-accent/40"
              : "bg-primary/15 font-medium text-primary")}
          onClick={() => changeShowIsolated(!showIsolated)}
        >
          孤立节点{isolatedCount > 0 ? ` (${isolatedCount})` : ""}
        </button>
        {/* C2 聚焦：hover/点选节点 → 只亮一跳邻接（默认开） */}
        <label className="flex cursor-pointer items-center gap-1 text-[11px] text-muted-foreground">
          <input type="checkbox" checked={focusOn} onChange={(e) => setFocusOn(e.target.checked)} />
          聚焦
        </label>
        {/* C2 边显示三态：全部（含弱边）/ 仅强边（缺省）/ 仅链边 */}
        <div className="flex items-center overflow-hidden rounded border border-[#30363d] text-[11px]">
          {([["all", "全部"], ["strong", "仅强边"], ["chain", "仅链边"]] as const).map(([m, label]) => (
            <button
              key={m} type="button"
              className={cn("px-2 py-0.5 transition-colors",
                edgeMode === m ? "bg-primary/15 font-medium text-primary" : "text-muted-foreground hover:bg-accent/40",
                m !== "all" && "border-l border-[#30363d]")}
              onClick={() => changeEdgeMode(m)}
            >
              {label}
            </button>
          ))}
        </div>
        {/* D4：MiniMap 默认收起（曾恒显右下角盖卡片），开关状态 localStorage 记忆 */}
        <button
          type="button" title={miniOpen ? "收起小地图" : "展开小地图"}
          className={cn("rounded p-1 transition-colors",
            miniOpen ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent/40 hover:text-foreground")}
          onClick={() => changeMiniOpen(!miniOpen)}
        >
          <MapIcon className="size-3.5" />
        </button>
        <button
          type="button" title="适应视图"
          className="rounded p-1 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          onClick={() => rf.fitView({ padding: 0.15, maxZoom: 1.4 })}
        >
          <Maximize2 className="size-3.5" />
        </button>
        <button
          type="button" title="全屏"
          className="rounded p-1 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          onClick={() => {
            if (document.fullscreenElement) void document.exitFullscreen()
            else void wrapperRef.current?.requestFullscreen()
          }}
        >
          {isFs ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
        </button>
      </div>

      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges as Edge[]}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        nodesDraggable={false}
        nodesConnectable
        onNodesChange={onNodesChangeCb}
        onConnect={onConnect}
        onEdgeClick={(_, edge) => selectEdge(edge.id)}
        onEdgeMouseEnter={(_, edge) => setHoveredEdgeId(edge.id)}
        onEdgeMouseLeave={(_, edge) => setHoveredEdgeId((cur) => (cur === edge.id ? null : cur))}
        onNodeMouseEnter={(_, node) => {
          if (node.type === "finding") setHoverNodeId(node.id)
        }}
        onNodeMouseLeave={(_, node) => {
          if (node.type === "finding") setHoverNodeId((cur) => (cur === node.id ? null : cur))
        }}
        onlyRenderVisibleElements
        onNodeClick={(_, node) => {
          if (node.type !== "finding") return
          setSelectedNodeId(node.id)
          setSelectedEdgeId(null)
          const f = findings.find((x) => x.id === node.id)
          if (f) openDetail(f)
        }}
        onPaneClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null) }}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 1.4 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#30363d" gap={20} size={1} />
        {miniOpen && (
          <MiniMap
            nodeColor={(n) => {
              if (n.type === "finding") {
                const f = (n.data as { f?: Finding }).f
                if (!f) return "#30363d"
                return { critical: "#f85149", high: "#f85149", medium: "#d29922", low: "#58a6ff", info: "#8b949e" }[f.severity] ?? "#8b949e"
              }
              return "#21262d"
            }}
          />
        )}
        {/* 深色覆盖样式见 canvas.css（xyflow 未分层 CSS 压不住 Tailwind 层） */}
        <Controls showInteractive={false} />
      </ReactFlow>

      {/* 空态 */}
      {findings.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
            当前筛选下没有发现（切回列表添加，或调整 IP/严重度/状态筛选）
          </p>
        </div>
      )}

      {/* 选中边浮卡片 */}
      {selectedEdge && (
        <div className="absolute bottom-3 left-1/2 z-20 w-80 -translate-x-1/2 rounded-md border border-[#39424e] bg-[#1c2128]/95 p-2 shadow-lg">
          {selectedEdge.kind === "weak" && (
            <div className="space-y-1">
              <p className="text-[11px] font-medium text-muted-foreground">弱边（推导）</p>
              <p className="text-[10px] text-muted-foreground">
                同 vuln_class 或同父任务、同泳道且严重度升级——系统推导的弱信号，不写入数据。
              </p>
            </div>
          )}
          {selectedEdge.kind === "strong" && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <p className="text-[11px] font-medium text-[#39c5cf]">强边 relates_to</p>
                <button
                  type="button"
                  className="rounded bg-[#39c5cf]/15 px-2 py-0.5 text-[10px] text-[#39c5cf] hover:bg-[#39c5cf]/25"
                  onClick={strongEdgeDlg}
                >
                  加入链 →
                </button>
              </div>
              <p className="line-clamp-3 text-[11px] text-foreground">{selectedEdge.label || "（无关联理由）"}</p>
              <p className="text-[10px] text-muted-foreground">Agent 上报，存于发现 evidence.relates_to。</p>
              {selectedEdgeStale && (
                <p className="text-[10px] text-amber-400">⚠ 一端发现已被标为误报（false-positive），该引用依据已被推翻。</p>
              )}
            </div>
          )}
          {selectedEdge.kind === "chain" && (
            <div className="space-y-1.5">
              <div className="flex items-center gap-2">
                <span className="size-2 rounded-full"
                      style={{ background: STATUS_DOT[selectedEdge.chainStatus ?? "hypothesis"] }} />
                <p className="min-w-0 flex-1 truncate text-[11px] font-medium">{selectedEdge.chainName}</p>
                <button
                  type="button"
                  className="rounded border border-(--status-error)/40 px-2 py-0.5 text-[10px] text-(--status-error) hover:bg-(--status-error)/10"
                  onClick={deleteSelectedEdge}
                >
                  删除链边
                </button>
              </div>
              <p className="line-clamp-3 text-[11px] text-foreground">{selectedEdge.label || "（无 edge_note）"}</p>
              {selectedEdgeStale && (
                <p className="text-[10px] text-amber-400">⚠ 链上一端发现已被标为误报，请人工复核此链段。</p>
              )}
            </div>
          )}
        </div>
      )}

      {/* 入链对话框 */}
      {dlg && (
        <AddChainEdgeDialog
          pid={pid}
          req={dlg}
          chains={summaries}
          onDone={() => { reloadChains(); onMutated() }}
          onClose={() => setDlg(null)}
        />
      )}

      {/* 发现详情（复用列表同款弹窗） */}
      {detail && (
        <FindingDetailDialog
          key={detail.id}
          pid={pid}
          finding={detail}
          assetLabel={detail.target_asset_id ? assetById.get(detail.target_asset_id)?.value : undefined}
          track={track}
          onClose={() => setDetail(null)}
          onMutated={onMutated}
        />
      )}
    </div>
  )
}

export function FindingsCanvas(props: {
  pid: string; findings: Finding[]; assets: Asset[]; assetFilter?: string; onMutated: () => void; track?: string
}) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  )
}
