import { useCallback, useEffect, useMemo, useState } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, MiniMap, Controls,
  MarkerType, useReactFlow, type Edge, type Node,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import { Maximize2, X } from "lucide-react"
import { api } from "@/lib/api"
import type { SessionGraph, SessionGraphEdge, SessionGraphNode } from "@/lib/types"
import { cn } from "@/lib/utils"

// 会话协作流（会话中心化 M4，2026-09-25；取代旧任务流在新壳的语义）：
// 节点=编排器+会话窗（按委托派生深度分层）；边：delegate 委派 / derive 派生
// / inbox 同依据 / dm 私信。3s 轮询；点节点回工作台，点边看明细。

const NODE_W = 230
const NODE_H = 92
const COL_GAP = 280
const ROW_GAP = 16

const EDGE_STYLE: Record<SessionGraphEdge["kind"], {
  stroke: string; width: number; dash?: string
}> = {
  delegate: { stroke: "#58a6ff", width: 1.6 },
  derive: { stroke: "#8b949e", width: 1.3 },
  inbox: { stroke: "#a371f7", width: 1.2, dash: "5 4" },
  dm: { stroke: "#3fb950", width: 1.1, dash: "2 4" },
}
const EDGE_LABEL: Record<SessionGraphEdge["kind"], string> = {
  delegate: "委派", derive: "派生", inbox: "同依据", dm: "私信",
}

function statusColor(n: SessionGraphNode): string {
  if (n.status === "closed") return "#6e7681"
  if (n.status === "paused") return "#d29922"
  if (n.current) return "#3fb950"
  if (n.status === "running") return "#3fb950"
  return "#58a6ff"
}

function SessionNodeCard({ n, onOpen }: {
  n: SessionGraphNode
  onOpen: (sid: string) => void
}) {
  if (n.kind === "orch") {
    return (
      <div className="flex h-[80px] w-40 items-center justify-center rounded-lg border border-[#58a6ff]/50 bg-[#161b22] text-sm font-medium shadow">
        🧭 编排器
      </div>
    )
  }
  const closed = n.status === "closed"
  return (
    <div
      onClick={() => !closed && onOpen(n.id)}
      className={cn(
        "h-[92px] w-[230px] overflow-hidden rounded-lg border bg-[#161b22] p-2 shadow",
        closed ? "cursor-default opacity-60" : "cursor-pointer hover:border-primary/60")}
      style={{ borderColor: `${statusColor(n)}55` }}
    >
      <div className="flex items-center gap-1.5">
        <span className="size-2 shrink-0 rounded-full" style={{ background: statusColor(n) }} />
        <span className="min-w-0 flex-1 truncate text-xs font-medium" title={n.label}>
          {n.label}
        </span>
        {n.queue ? (
          <span className="shrink-0 rounded-full bg-sky-400/20 px-1.5 text-[9px] text-sky-300">
            队列 {n.queue}
          </span>
        ) : null}
      </div>
      <p className={cn("mt-1 line-clamp-2 text-[10px] leading-snug",
        n.current ? "text-foreground/90" : "text-muted-foreground")}>
        {n.current ? `▶ ${n.current}` : "待命（无在窗委托）"}
      </p>
      <p className="mt-0.5 truncate font-mono text-[9px] text-muted-foreground">
        {n.role} · {n.status}
      </p>
    </div>
  )
}

function Flow({ pid, onOpenSession }: {
  pid: string
  onOpenSession: (sid: string) => void
}) {
  const rf = useReactFlow()
  const [graph, setGraph] = useState<SessionGraph | null>(null)

  const load = useCallback(() => {
    api.sessionGraph(pid).then(setGraph).catch(() => {})
  }, [pid])
  useEffect(() => {
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [load])

  // 分层：derive 边定深度，其余窗默认 1（orch 直属）
  const layout = useMemo(() => {
    const nodes = graph?.nodes ?? []
    const edges = graph?.edges ?? []
    const depth: Record<string, number> = {}
    for (const n of nodes)
      if (n.kind === "session") depth[n.id] = 1
    for (let pass = 0; pass < 6; pass++) {
      let changed = false
      for (const e of edges) {
        if (e.kind !== "derive") continue
        const nd = (depth[e.source] ?? 1) + 1
        if (nd > (depth[e.target] ?? 1)) {
          depth[e.target] = Math.min(nd, 6); changed = true
        }
      }
      if (!changed) break
    }
    const rows: Record<number, SessionGraphNode[]> = {}
    for (const n of nodes) {
      if (n.kind !== "session") continue
      const col2: number = depth[n.id] ?? 1;
      (rows[col2] ??= []).push(n)
    }
    const pos = new Map<string, { x: number; y: number }>()
    for (const [col, arr] of Object.entries(rows)) {
      arr.sort((a, b) => (a.id < b.id ? -1 : 1))
      arr.forEach((n, i) => pos.set(n.id, {
        x: (Number(col) - 1) * COL_GAP + 40,
        y: i * (NODE_H + ROW_GAP),
      }))
    }
    const total = nodes.filter((n) => n.kind === "session").length
    const meanY = total ? Math.max(0, (total * NODE_H + (total - 1) * ROW_GAP) / 2 - 40) : 0
    pos.set("__orch", { x: -200, y: meanY })
    return pos
  }, [graph])

  const flowNodes: Node[] = useMemo(() => (graph?.nodes ?? []).map((n) => {
    const p = layout.get(n.id) ?? { x: 0, y: 0 }
    const isOrch = n.kind === "orch"
    return {
      id: n.id,
      position: p,
      measured: { width: isOrch ? 160 : NODE_W, height: isOrch ? 80 : NODE_H },
      style: { width: isOrch ? 160 : NODE_W },
      data: {},
      node: n, // 自定义渲染经 node-children（见下）
    } as Node & { node: SessionGraphNode }
  }), [graph, layout])

  // 自定义节点内容：用 nodeRenderer（xyflow 无 prop 直挂 children，走 nodeTypes 闭包）
  const nodeTypes = useMemo(() => ({
    sessionNode: (props: { data: { n?: SessionGraphNode } }) => {
      const n = props.data.n
      if (!n) return null
      return <SessionNodeCard n={n} onOpen={onOpenSession} />
    },
  }), [onOpenSession])

  const typedNodes = useMemo(() => flowNodes.map((n) => {
    const sn = (n as Node & { node: SessionGraphNode }).node
    return {
      ...n,
      type: "sessionNode",
      data: { n: sn },
    }
  }), [flowNodes])

  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const selectedEdge: SessionGraphEdge | null = useMemo(
    () => graph?.edges.find((e) => e.id === selectedEdgeId) ?? null,
    [graph, selectedEdgeId])

  const flowEdges: Edge[] = useMemo(() => (graph?.edges ?? []).map((e) => {
    const st = EDGE_STYLE[e.kind]
    const selected = e.id === selectedEdgeId
    return {
      id: e.id, source: e.source, target: e.target,
      style: {
        stroke: st.stroke, strokeWidth: selected ? st.width + 0.8 : st.width,
        strokeDasharray: st.dash, opacity: selected ? 1 : 0.85,
      },
      markerEnd: { type: MarkerType.ArrowClosed, color: st.stroke, width: 14, height: 14 },
      interactionWidth: 12,
    }
  }), [graph, selectedEdgeId])

  // 首次加载完成后 fitView 一次
  const [didFit, setDidFit] = useState(false)
  useEffect(() => {
    if (didFit || !graph || graph.nodes.length === 0) return
    setDidFit(true)
    const raf = requestAnimationFrame(() => rf.fitView({ padding: 0.2, maxZoom: 1 }))
    return () => cancelAnimationFrame(raf)
  }, [graph, didFit, rf])
  useEffect(() => setDidFit(false), [pid])

  return (
    <div className="relative h-full w-full bg-[#0d1117]">
      <ReactFlow
        nodes={typedNodes}
        edges={flowEdges}
        nodeTypes={nodeTypes}
        nodesDraggable={false}
        onNodeClick={(_, n) => {
          if (n.id !== "__orch") setSelectedEdgeId(null)
        }}
        onEdgeClick={(_, e) => { setSelectedEdgeId(e.id) }}
        onPaneClick={() => setSelectedEdgeId(null)}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#30363d" gap={20} size={1} />
        <MiniMap nodeColor={() => "#58a6ff"} />
        <Controls showInteractive={false} />
      </ReactFlow>

      <div className="absolute left-2 top-2 z-20 flex items-center gap-3 rounded-md border border-[#21262d] bg-[#0d1117]/90 px-2.5 py-1 text-[10px] text-muted-foreground">
        <span className="font-medium text-foreground">会话协作流</span>
        {(Object.keys(EDGE_STYLE) as SessionGraphEdge["kind"][]).map((k) => (
          <span key={k} className="flex items-center gap-1">
            <span className="inline-block w-5 border-t"
              style={{ borderColor: EDGE_STYLE[k].stroke,
                borderStyle: EDGE_STYLE[k].dash ? "dashed" : "solid" }} />
            {EDGE_LABEL[k]}
          </span>
        ))}
        <span className="hidden lg:inline">点节点回工作台 · 点边看依据明细</span>
        <button type="button" title="适应视图"
          className="rounded p-1 hover:bg-accent/40 hover:text-foreground"
          onClick={() => rf.fitView({ padding: 0.2, maxZoom: 1 })}>
          <Maximize2 className="size-3.5" />
        </button>
      </div>

      {graph === null && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
            协作流加载中…
          </p>
        </div>
      )}
      {graph && graph.nodes.length <= 1 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
            还没有会话窗——在会话看板开一个窗，或让编排器委派委托
          </p>
        </div>
      )}

      {selectedEdge && (
        <div className="absolute bottom-3 left-1/2 z-20 w-[26rem] max-w-[90%] -translate-x-1/2 rounded-md border bg-[#1c2128]/95 p-2.5 shadow-lg"
             style={{ borderColor: `${EDGE_STYLE[selectedEdge.kind].stroke}66` }}>
          <div className="flex items-center gap-2">
            <p className="text-[11px] font-medium" style={{ color: EDGE_STYLE[selectedEdge.kind].stroke }}>
              {EDGE_LABEL[selectedEdge.kind]}边 · {selectedEdge.refs.length} 条
            </p>
            <span className="flex-1" />
            <button type="button" title="关闭"
              className="rounded p-0.5 text-muted-foreground hover:text-foreground"
              onClick={() => setSelectedEdgeId(null)}>
              <X className="size-3.5" />
            </button>
          </div>
          <ul className="mt-1 max-h-48 space-y-1 overflow-auto">
            {selectedEdge.refs.map((r, i) => (
              <li key={i} className="rounded bg-card/60 px-1.5 py-1 text-[10px] leading-snug">
                {typeof r.objective === "string" && r.objective}
                {typeof r.text === "string" && `💬 ${r.text}`}
                {typeof r.title === "string" && r.title}
                {!r.objective && !r.text && !r.title && JSON.stringify(r)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

export function SessionFlow(props: {
  pid: string
  onOpenSession: (sid: string) => void
}) {
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  )
}
