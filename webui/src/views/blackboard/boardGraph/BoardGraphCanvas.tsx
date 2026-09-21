import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, MiniMap, Controls,
  MarkerType, useReactFlow,
  type Edge, type Node, type NodeChange,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "../canvas.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import { Maximize2, Minimize2 } from "lucide-react"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"
import { hexAddr } from "@/lib/workbench"
import type { BoardGraph, BoardGraphEdge, BoardGraphNode, Finding } from "@/lib/types"
import { FindingDetailDialog } from "@/components/blackboard/FindingDetailDialog"
import { STATUS_DOT } from "../ChainToolbar"
import { CTF_LEVEL } from "../ctfLevel"
import {
  BoardColBg, BoardColHeader, BoardNode, TASK_DOT, TYPE_ACCENT,
  type BoardColBgNode, type BoardColHeaderNode, type BoardFlowNode,
} from "./BoardNode"
import { BoardEdge, type BoardFlowEdge } from "./BoardEdge"
import {
  BOARD_CARD_W, buildBoardLayout, COLUMN_ICON, type BoardLayout,
} from "./boardModel"

// 黑板链路图（任务 E，DESIGN.md §12 黑板链路图）：五类对象 × 类型分层 DAG 只读视图。
// 数据 = GET /api/projects/{pid}/board-graph（边口径服务端定稿）4s 轮询；
// 交互 = 悬停/点选聚焦一跳邻接、finding 点详情弹窗、其余底部浮卡、死路/孤立折叠。
// 由 Blackboard 第 4 tab「全景」懒加载（@xyflow/react 不进主包）。

const nodeTypes = { boardNode: BoardNode, boardColHeader: BoardColHeader, boardColBg: BoardColBg }
const edgeTypes = { boardEdge: BoardEdge }

const EDGE_COLOR: Record<string, string> = {
  asset_parent: "#58a6ff",
  func_of: "#bc8cff",
  targets: "#f85149",
  relates_to: "#39c5cf",
  poc: "#3fb950",
  basis: "#d29922",
  task_parent: "#8b949e",
  artifact_task: "#8b949e",
}
const EDGE_LABEL: Record<string, string> = {
  asset_parent: "父子资产", func_of: "函数归属", targets: "作用于资产",
  relates_to: "关联发现", poc: "POC 产物", basis: "任务依据",
  task_parent: "父子任务", artifact_task: "产物归属", chain: "攻击链",
}

const COL_BG_W = BOARD_CARD_W + 36  // 列背景/列头宽（卡 240 + 两侧各 18）

function Canvas({ pid, track }: { pid: string; track?: string }) {
  const wrapperRef = useRef<HTMLDivElement>(null)
  const rf = useReactFlow()
  const isCtf = track === "ctf"

  const [graph, setGraph] = useState<BoardGraph | null>(null)
  useEffect(() => {
    let alive = true
    const load = () => api.boardGraph(pid).then((g) => { if (alive) setGraph(g) }).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  const [showDeadEnds, setShowDeadEnds] = useState<boolean>(() => {
    try { return localStorage.getItem(`board-deadend:${pid}`) === "1" } catch { return false }
  })
  const changeDeadEnds = useCallback((v: boolean) => {
    setShowDeadEnds(v)
    try { localStorage.setItem(`board-deadend:${pid}`, v ? "1" : "0") } catch { /* 忽略 */ }
  }, [pid])
  const [showIsolated, setShowIsolated] = useState<boolean>(() => {
    try { return localStorage.getItem(`board-isolated:${pid}`) === "1" } catch { return false }
  })
  const changeIsolated = useCallback((v: boolean) => {
    setShowIsolated(v)
    try { localStorage.setItem(`board-isolated:${pid}`, v ? "1" : "0") } catch { /* 忽略 */ }
  }, [pid])
  const [focusOn, setFocusOn] = useState(true)
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Finding | null>(null)
  const [isFs, setIsFs] = useState(false)
  useEffect(() => {
    const h = () => setIsFs(!!document.fullscreenElement)
    document.addEventListener("fullscreenchange", h)
    return () => document.removeEventListener("fullscreenchange", h)
  }, [])

  const layout: BoardLayout = useMemo(
    () => buildBoardLayout(graph ?? { nodes: [], edges: [] }, { showDeadEnds, showIsolated }),
    [graph, showDeadEnds, showIsolated])
  const nodeById = useMemo(
    () => new Map((graph?.nodes ?? []).map((n) => [n.id, n])),
    [graph])

  // 首帧数据到达后适应一次视图（轮询刷新不打断用户当前视野）
  const didFit = useRef(false)
  useEffect(() => {
    if (graph && !didFit.current && layout.columns.length > 0) {
      didFit.current = true
      requestAnimationFrame(() => rf.fitView({ padding: 0.15, maxZoom: 1.2 }))
    }
  }, [graph, layout.columns.length, rf])

  // 受控 ReactFlow 契约：节点由 layout 派生，但内部测量变化必须回写——否则任一
  // 重渲染都会用不带 measured 的 props 节点清空内部 measured/handleBounds，
  // 边会因此被整体卸载（FindingsCanvas 同款坑，节点不可拖也必须保留）。
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

  // 聚焦：hover/点选节点 → 一跳邻接
  const focusNodeId = focusOn ? (selectedNodeId ?? hoverNodeId) : null
  const adjacent = useMemo(() => {
    const s = new Set<string>()
    if (focusNodeId) {
      s.add(focusNodeId)
      for (const e of graph?.edges ?? []) {
        if (e.source === focusNodeId) s.add(e.target)
        if (e.target === focusNodeId) s.add(e.source)
      }
    }
    return s
  }, [focusNodeId, graph])

  // ---------- 边 ----------
  const flowEdges: BoardFlowEdge[] = useMemo(() => (graph?.edges ?? [])
    .map((e: BoardGraphEdge): BoardFlowEdge | null => {
      const connected = !focusNodeId || e.source === focusNodeId || e.target === focusNodeId
      const sp = layout.place.get(e.source)
      const tp = layout.place.get(e.target)
      if (!sp || !tp) return null  // 端点被隐藏（死路/孤立折叠）→ 不渲染该边
      const stale = !!e.stale
      const color = e.kind === "chain" ? STATUS_DOT[(e.chain_status ?? "hypothesis") as keyof typeof STATUS_DOT]
        : EDGE_COLOR[e.kind] ?? "#8b949e"
      const dim = !connected
      return {
        id: e.id, source: e.source, target: e.target, type: "boardEdge",
        data: {
          kind: e.kind,
          label: e.label ?? e.edge_note,
          selected: e.id === selectedEdgeId,
          stale,
          onSelect: (id: string) => { setSelectedEdgeId(id); setSelectedNodeId(null) },
        },
        style: {
          stroke: color,
          strokeWidth: e.kind === "chain" ? 1.8 : 1.2,
          strokeDasharray: stale ? "2 4" : e.kind === "relates_to" ? "5 3" : undefined,
          opacity: dim ? (focusOn ? 0.08 : 0.15) : stale ? 0.25 : e.kind === "chain" ? 0.95 : 0.7,
        },
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 12, height: 12 },
        interactionWidth: 0,
      }
    }).filter((e): e is BoardFlowEdge => e !== null),
    [graph, focusNodeId, focusOn, selectedEdgeId, layout.place])

  // ---------- 节点 ----------
  const openFinding = useCallback((id: string) => {
    api.findings(pid).then((list) => {
      const f = list.find((x) => x.id === id)
      if (f) setDetail(f)
    }).catch(() => {})
  }, [pid])

  const selectNode = useCallback((n: BoardGraphNode) => {
    setSelectedNodeId(n.id)
    setSelectedEdgeId(null)
    if (n.node_type === "finding") openFinding(n.id)
  }, [openFinding])

  const flowNodes = useMemo(() => {
    const nodes: Node[] = []
    // 列背景（最底层）+ 列头
    for (const col of layout.columns) {
      nodes.push({
        id: `bg:${col.node_type}`, type: "boardColBg",
        position: { x: col.x - 18, y: 44 },
        draggable: false, selectable: false, focusable: false,
        data: { width: COL_BG_W, height: Math.max(80, layout.height - 44) },
        zIndex: -10, measured: measuredById[`bg:${col.node_type}`],
        style: { pointerEvents: "none" },
      } satisfies BoardColBgNode)
      nodes.push({
        id: `hdr:${col.node_type}`, type: "boardColHeader",
        position: { x: col.x - 18, y: 44 },
        draggable: false, selectable: false, focusable: false,
        data: { label: col.label, icon: COLUMN_ICON[col.node_type], count: col.count, width: COL_BG_W },
        measured: measuredById[`hdr:${col.node_type}`],
      } satisfies BoardColHeaderNode)
    }
    for (const n of graph?.nodes ?? []) {
      const p = layout.place.get(n.id)
      if (!p) continue
      const dim = focusNodeId !== null && !adjacent.has(n.id)
      const node: BoardFlowNode = {
        id: n.id, type: "boardNode",
        position: p,
        data: {
          n, dim, selected: selectedNodeId === n.id, onSelect: selectNode,
          sevText: n.node_type === "finding" && isCtf
            ? CTF_LEVEL[n.severity ?? ""]?.label : undefined,
        },
        measured: measuredById[n.id],
        style: { width: BOARD_CARD_W },
      }
      nodes.push(node)
    }
    return nodes
  }, [graph, layout, focusNodeId, adjacent, selectedNodeId, selectNode, measuredById, isCtf])

  // ---------- 底部浮卡（非 finding 节点 / 选中边） ----------
  const selNode = selectedNodeId ? nodeById.get(selectedNodeId) : undefined
  const selEdge = (graph?.edges ?? []).find((e) => e.id === selectedEdgeId)

  return (
    <div ref={wrapperRef} className="fc-dark absolute inset-0 bg-[#0d1117]">
      {/* 顶部工具条 */}
      <div className="absolute inset-x-0 top-0 z-20 flex flex-wrap items-center gap-2 border-b border-[#21262d] bg-[#0d1117]/90 px-2 py-1.5">
        <span className="text-[11px] font-medium text-muted-foreground">
          全景链路（资产 → 函数 → 发现 → 产物 → 任务）
        </span>
        <span className="flex-1" />
        <button
          type="button"
          title={showDeadEnds ? "死路显示中：点击折叠 false-positive 发现" : "死路已折叠：点击展开误报/死路发现"}
          className={cn("rounded border border-[#30363d] px-2 py-0.5 text-[11px] transition-colors",
            showDeadEnds ? "text-muted-foreground hover:bg-accent/40"
              : "bg-primary/15 font-medium text-primary")}
          onClick={() => changeDeadEnds(!showDeadEnds)}
        >
          死路{layout.deadEndIds.size > 0 ? ` (${layout.deadEndIds.size})` : ""}
        </button>
        <button
          type="button"
          title={showIsolated ? "孤立节点显示中：点击隐藏无任何连接的节点" : "孤立节点已隐藏：仅显示有连接的节点"}
          className={cn("rounded border border-[#30363d] px-2 py-0.5 text-[11px] transition-colors",
            showIsolated ? "text-muted-foreground hover:bg-accent/40"
              : "bg-primary/15 font-medium text-primary")}
          onClick={() => changeIsolated(!showIsolated)}
        >
          孤立节点{layout.isolatedIds.size > 0 ? ` (${layout.isolatedIds.size})` : ""}
        </button>
        <label className="flex cursor-pointer items-center gap-1 text-[11px] text-muted-foreground">
          <input type="checkbox" checked={focusOn} onChange={(e) => setFocusOn(e.target.checked)} />
          聚焦
        </label>
        <button
          type="button" title="适应视图"
          className="rounded p-1 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          onClick={() => rf.fitView({ padding: 0.15, maxZoom: 1.2 })}
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
        nodesConnectable={false}
        onNodesChange={onNodesChangeCb}
        onNodeMouseEnter={(_, node) => {
          if (node.type === "boardNode") setHoverNodeId(node.id)
        }}
        onNodeMouseLeave={(_, node) => {
          if (node.type === "boardNode") setHoverNodeId((cur) => (cur === node.id ? null : cur))
        }}
        onlyRenderVisibleElements
        onNodeClick={(_, node) => {
          if (node.type !== "boardNode") return
          const n = nodeById.get(node.id)
          if (n) selectNode(n)
        }}
        onPaneClick={() => { setSelectedNodeId(null); setSelectedEdgeId(null) }}
        minZoom={0.1}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#30363d" gap={20} size={1} />
        <MiniMap nodeColor={(n) => {
          if (n.type === "boardNode") {
            const nt = (n as BoardFlowNode).data.n.node_type
            return TYPE_ACCENT[nt] ?? "#30363d"
          }
          return "#21262d"
        }} />
        <Controls showInteractive={false} />
      </ReactFlow>

      {/* 空态 */}
      {graph && graph.nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
            黑板还是空的——先登记资产或等 Agent 产出发现
          </p>
        </div>
      )}

      {/* 选中边浮卡 */}
      {selEdge && (
        <div className="absolute bottom-3 left-1/2 z-20 w-80 -translate-x-1/2 rounded-md border border-[#39424e] bg-[#1c2128]/95 p-2 shadow-lg">
          <div className="flex items-center gap-2">
            {selEdge.kind === "chain" && (
              <span className="size-2 rounded-full"
                    style={{ background: STATUS_DOT[(selEdge.chain_status ?? "hypothesis") as keyof typeof STATUS_DOT] }} />
            )}
            <p className="text-[11px] font-medium text-muted-foreground">
              {selEdge.kind === "chain" ? `攻击链 · ${selEdge.chain_name ?? ""}` : EDGE_LABEL[selEdge.kind] ?? selEdge.kind}
            </p>
          </div>
          {(selEdge.label ?? selEdge.edge_note) && (
            <p className="line-clamp-3 text-[11px] text-foreground">{selEdge.label ?? selEdge.edge_note}</p>
          )}
          {selEdge.stale && (
            <p className="text-[10px] text-amber-400">⚠ 该依据已被推翻（stale）——图上仅标记不剔除。</p>
          )}
        </div>
      )}

      {/* 非 finding 节点底部详情浮卡（v1 只读） */}
      {selNode && selNode.node_type !== "finding" && (
        <div className="absolute bottom-3 left-1/2 z-20 w-96 -translate-x-1/2 rounded-md border border-[#39424e] bg-[#1c2128]/95 p-2 shadow-lg">
          <div className="flex items-center gap-2">
            <span className="rounded bg-muted px-1 font-mono text-[10px] text-muted-foreground">
              {selNode.node_type}
            </span>
            <p className="min-w-0 flex-1 truncate text-[12px] font-medium">{selNode.label}</p>
            {selNode.node_type === "task" && selNode.status && (
              <span className="size-2 shrink-0 rounded-full" style={{ background: TASK_DOT[selNode.status] }} />
            )}
          </div>
          <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">
            {selNode.node_type === "asset" && <>type={selNode.sub} · {selNode.value}</>}
            {selNode.node_type === "func_kb" && <>
              @{hexAddr(Number(selNode.address) || 0)} · sha256:{(selNode.binary_sha256 ?? "").slice(0, 12)}…
              {(selNode.risk_tags?.length ?? 0) > 0 && <> · risk: {selNode.risk_tags!.join(",")}</>}
            </>}
            {selNode.node_type === "artifact" && <>{selNode.path} · {selNode.kind}</>}
            {selNode.node_type === "task" && <>
              {selNode.task_type} · P{selNode.priority}
              {selNode.claimed_by && <> · {selNode.claimed_by}</>}
            </>}
          </p>
          {selNode.node_type === "artifact" && selNode.description && (
            <p className="mt-1 line-clamp-2 text-[11px] text-foreground">{selNode.description}</p>
          )}
        </div>
      )}

      {/* 发现详情（复用列表同款弹窗） */}
      {detail && (
        <FindingDetailDialog
          key={detail.id}
          pid={pid}
          finding={detail}
          assetLabel={detail.target_asset_id ? nodeById.get(detail.target_asset_id)?.label : undefined}
          track={track}
          onClose={() => { setDetail(null); setSelectedNodeId(null) }}
          onMutated={() => {}}
        />
      )}
    </div>
  )
}

export function BoardGraphCanvas(props: { pid: string; track?: string }) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  )
}
