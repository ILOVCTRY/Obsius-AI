import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, MiniMap, Controls,
  MarkerType, useReactFlow,
  type Connection, type Edge, type Node, type NodeChange,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "./canvas.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import { Maximize2, Minimize2, RotateCcw } from "lucide-react"
import { api } from "@/lib/api"
import type { Asset, Chain, ChainStatus, ChainSummary, Finding } from "@/lib/types"
import { FindingDetailDialog } from "@/components/blackboard/FindingDetailDialog"
import {
  FindingNode, LaneBackground, LaneHeader,
  type FindingFlowNode, type LaneHeaderNode,
} from "./FindingNode"
import { FindingEdge, type FindingFlowEdge } from "./FindingEdge"
import {
  buildChainEdges, buildLanes, buildStrongEdges, buildWeakEdges,
  CARD_STEP, CARD_Y0, LANE_W,
  type ModelEdge,
} from "./canvasModel"
import { AddChainEdgeDialog, type ChainEdgeRequest } from "./AddChainEdgeDialog"
import { ChainToolbar, STATUS_DOT } from "./ChainToolbar"

// 评估攻击链画布（E1，DESIGN §12 评估画布）：
// host IP 泳道 × 严重度四列 × 时间堆叠；弱边/relates_to 强边/chain 边三级；
// 手拖连线/强边快捷确认 → AddChainEdgeDialog（edge_note 必填）→ 现成 chains API。
// 由 Blackboard 发现 tab 的「链路」子视图懒加载（@xyflow/react 不进主包）。

const nodeTypes = { finding: FindingNode, laneHeader: LaneHeader, laneBg: LaneBackground }
const edgeTypes = { findingEdge: FindingEdge }

const OFFSETS_KEY = (pid: string) => `findings-canvas-offsets-v1:${pid}`
type Offsets = Record<string, { dx: number; dy: number }>

function loadOffsets(pid: string): Offsets {
  try { return JSON.parse(localStorage.getItem(OFFSETS_KEY(pid)) ?? "{}") as Offsets }
  catch { return {} }
}

function Canvas({ pid, findings, assets, onMutated }: {
  pid: string
  findings: Finding[]      // 已过 IP/sev/status 共享筛选
  assets: Asset[]
  onMutated: () => void
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

  // 节点手动偏移（localStorage 按项目持久；相对自动槽位，筛泳道不错位）
  const [offsets, setOffsets] = useState<Offsets>(() => loadOffsets(pid))
  useEffect(() => { setOffsets(loadOffsets(pid)) }, [pid])
  const saveOffsets = useCallback((next: Offsets) => {
    setOffsets(next)
    try { localStorage.setItem(OFFSETS_KEY(pid), JSON.stringify(next)) } catch { /* 满/隐私模式忽略 */ }
  }, [pid])

  const [weakOn, setWeakOn] = useState(true)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Finding | null>(null)
  const [dlg, setDlg] = useState<ChainEdgeRequest | null>(null)
  const [mutating, setMutating] = useState(false)
  const [isFs, setIsFs] = useState(false)
  useEffect(() => {
    const h = () => setIsFs(!!document.fullscreenElement)
    document.addEventListener("fullscreenchange", h)
    return () => document.removeEventListener("fullscreenchange", h)
  }, [])

  const model = useMemo(() => buildLanes(findings, assets), [findings, assets])
  const assetById = useMemo(() => new Map(assets.map((a) => [a.id, a])), [assets])

  // 受控 ReactFlow 契约：节点由 model+offsets 派生，但内部测量/拖拽变化必须回写——
  // 否则任一重渲染都会用不带 measured 的 props 节点把内部 measured/handleBounds
  // 清空（xyflow 对受控节点重建 internalNode），边会因此被整体卸载。
  const [measuredById, setMeasuredById] = useState<Record<string, { width: number; height: number }>>({})
  const onNodesChangeCb = useCallback((changes: NodeChange[]) => {
    let dims: Record<string, { width: number; height: number }> | null = null
    const moved: Offsets = {}
    let hasMove = false
    let dragEnd: { dx: number; dy: number } | null = null
    for (const ch of changes) {
      if (ch.type === "dimensions" && ch.dimensions?.width && ch.dimensions?.height) {
        ;(dims ??= {})[ch.id] = { width: ch.dimensions.width, height: ch.dimensions.height }
      } else if (ch.type === "position" && ch.position && model.place.has(ch.id)) {
        const slot = model.place.get(ch.id)!
        const off = { dx: ch.position.x - slot.x, dy: ch.position.y - slot.y }
        moved[ch.id] = off
        hasMove = true
        if (ch.dragging === false) dragEnd = off
      }
    }
    if (dims) setMeasuredById((m) => ({ ...m, ...dims }))
    if (hasMove) {
      setOffsets((prev) => {
        const next = { ...prev, ...moved }
        if (dragEnd) {
          try { localStorage.setItem(OFFSETS_KEY(pid), JSON.stringify(next)) } catch { /* 忽略 */ }
        }
        return next
      })
    }
  }, [model.place, pid])

  // ---------- 边 ----------
  const edges = useMemo(() => {
    const weak = weakOn ? buildWeakEdges(findings, model.laneOf) : []
    const strong = buildStrongEdges(findings)
    const chain = buildChainEdges([...details.values()], new Set(findings.map((f) => f.id)))
    return [...weak, ...strong, ...chain]
  }, [weakOn, findings, model.laneOf, details])

  const adjacent = useMemo(() => {
    const s = new Set<string>()
    if (selectedNodeId) {
      s.add(selectedNodeId)
      for (const e of edges) {
        if (e.source === selectedNodeId) s.add(e.target)
        if (e.target === selectedNodeId) s.add(e.source)
      }
    }
    return s
  }, [selectedNodeId, edges])

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

  const flowEdges: FindingFlowEdge[] = useMemo(() => edges.map((e): FindingFlowEdge => {
    const connected = !selectedNodeId
      || e.source === selectedNodeId || e.target === selectedNodeId
    const chainDim = e.kind === "chain" && !!selectedChainId && e.chainId !== selectedChainId
    const stale = fpIds.has(e.source) || fpIds.has(e.target)
    const dim = !connected || chainDim
    const color = edgeColor(e)
    return {
      id: e.id, source: e.source, target: e.target, type: "findingEdge",
      data: {
        kind: e.kind, label: e.label, selected: e.id === selectedEdgeId,
        dimmed: dim, stale, onSelect: selectEdge,
      },
      style: {
        stroke: color,
        strokeWidth: e.kind === "chain" ? 1.8 : e.kind === "strong" ? 1.6 : 1,
        strokeDasharray: stale ? "3 4" : e.kind === "weak" ? "5 4" : undefined,
        opacity: dim ? 0.15 : stale ? 0.25 : e.kind === "weak" ? 0.65 : 0.95,
      },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
      interactionWidth: 0,
    }
  }), [edges, fpIds, selectedNodeId, selectedChainId, selectedEdgeId, selectEdge])

  // ---------- 节点 ----------
  const openDetail = useCallback((f: Finding) => setDetail(f), [])
  const quickAdd = useCallback((f: Finding) => setDlg({ source: null, target: f }), [])

  const flowNodes = useMemo(() => {
    const nodes: Node[] = []
    // 泳道背景（最底层，不可交互）
    model.lanes.forEach((lane) => {
      const inLane = findings.filter((f) => model.laneOf.get(f.id) === lane.key)
      const h = Math.max(420, (inLane.length + 1) * CARD_STEP + CARD_Y0)
      nodes.push({
        id: `bg:${lane.key}`, type: "laneBg", position: { x: lane.x - 12, y: 0 },
        draggable: false, selectable: false, focusable: false,
        data: {}, zIndex: -10,
        measured: measuredById[`bg:${lane.key}`],
        style: { width: LANE_W + 24, height: h, pointerEvents: "none" },
      })
    })
    // 泳道头（host IP + 四列标签）
    model.lanes.forEach((lane) => {
      const counts = [0, 0, 0, 0] as [number, number, number, number]
      for (const f of findings) {
        if (model.laneOf.get(f.id) !== lane.key) continue
        const p = model.place.get(f.id)
        if (p) counts[p.col]++
      }
      const hdr: LaneHeaderNode = {
        id: `lane:${lane.key}`, type: "laneHeader",
        position: { x: lane.x, y: 44 }, draggable: false, selectable: false, focusable: false,
        data: { label: lane.label, counts },
        measured: measuredById[`lane:${lane.key}`],
      }
      nodes.push(hdr)
    })
    // 发现卡片
    for (const f of findings) {
      const p = model.place.get(f.id)
      if (!p) continue
      const off = offsets[f.id] ?? { dx: 0, dy: 0 }
      const dim = selectedNodeId !== null && !adjacent.has(f.id)
      const node: FindingFlowNode = {
        id: f.id, type: "finding",
        position: { x: p.x + off.dx, y: p.y + off.dy },
        data: { f, dim, onOpen: openDetail, onQuickAdd: quickAdd },
        measured: measuredById[f.id],
        style: { width: 224 },
      }
      nodes.push(node)
    }
    return nodes
  }, [model, findings, offsets, selectedNodeId, adjacent, openDetail, quickAdd, measuredById])

  const onNodeDragStop = useCallback((_: unknown, node: Node) => {
    // 位置已在 onNodesChangeCb 实时回写并在拖拽结束时落盘；这里只兜底再持久化一次
    const p = model.place.get(node.id)
    if (!p) return
    setOffsets((prev) => {
      const next = { ...prev, [node.id]: { dx: node.position.x - p.x, dy: node.position.y - p.y } }
      try { localStorage.setItem(OFFSETS_KEY(pid), JSON.stringify(next)) } catch { /* 忽略 */ }
      return next
    })
  }, [model.place, pid])

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
        <label className="flex cursor-pointer items-center gap-1 text-[11px] text-muted-foreground">
          <input type="checkbox" checked={weakOn} onChange={(e) => setWeakOn(e.target.checked)} />
          弱边
        </label>
        <button
          type="button" title="重置布局（清除手动位置）"
          className="rounded p-1 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          onClick={() => saveOffsets({})}
        >
          <RotateCcw className="size-3.5" />
        </button>
        <button
          type="button" title="适应视图"
          className="rounded p-1 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          onClick={() => rf.fitView({ padding: 0.15, maxZoom: 1 })}
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
        nodesDraggable
        nodesConnectable
        onNodesChange={onNodesChangeCb}
        onConnect={onConnect}
        onNodeDragStop={onNodeDragStop}
        onNodeClick={(_, node) => {
          if (node.type !== "finding") return
          setSelectedNodeId(node.id)
          setSelectedEdgeId(null)
          const f = findings.find((x) => x.id === node.id)
          if (f) openDetail(f)
        }}
        onPaneClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null) }}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#30363d" gap={20} size={1} />
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
                  className="rounded border border-[--status-error]/40 px-2 py-0.5 text-[10px] text-[--status-error] hover:bg-[--status-error]/10"
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
          pid={pid}
          finding={detail}
          assetLabel={detail.target_asset_id ? assetById.get(detail.target_asset_id)?.value : undefined}
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  )
}

export function FindingsCanvas(props: {
  pid: string; findings: Finding[]; assets: Asset[]; onMutated: () => void
}) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  )
}
