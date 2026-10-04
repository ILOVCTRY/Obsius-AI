import { Handle, Position, ReactFlow, ReactFlowProvider, Background, Controls, useReactFlow, useUpdateNodeInternals, type NodeChange, type NodeProps, type NodeTypes } from "@xyflow/react"
import { Activity, AlertTriangle, Check, CircleDot } from "lucide-react"
import type { Edge, Node } from "@xyflow/react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import type { Session } from "@/lib/types"
import { buildPlanFlow, type PlanNodeData } from "./coordinationFlow"
import "@xyflow/react/dist/style.css"
import "@/views/live/tree.css"

type Props = { tasks: Parameters<typeof buildPlanFlow>[0]; sessions: Session[]; onOpenSession?: (sid: string) => void; onOpenTask?: (tid: string) => void }
const statusIcon = (status: string) => status === "completed" ? <Check size={13} /> : status === "blocked" || status === "failed" ? <AlertTriangle size={13} /> : status === "running" ? <Activity size={13} /> : <CircleDot size={13} />

function PlanTaskNode({ data }: NodeProps<Node<PlanNodeData>>) {
  const { task, session, unmet, onOpenSession, onOpenTask } = data
  return <div className={`plan-dag-node is-${task.status}`}>
    <Handle type="target" position={Position.Left} />
    <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">{statusIcon(task.status)}<span>{task.status}</span><span className="ml-auto font-mono">P{task.priority}</span></div>
    <b className="mt-1 block truncate text-xs" title={task.title}>{task.title}</b>
    <div className="mt-1 flex flex-wrap gap-1 text-[10px] text-muted-foreground"><span>{task.role || "通用"}</span>{task.task_id && <button type="button" className="text-primary hover:underline" onClick={() => task.task_id && onOpenTask?.(task.task_id)}>task</button>}{session && <button type="button" className="text-primary hover:underline" onClick={() => onOpenSession?.(session.id)}>会话</button>}</div>
    {unmet.length > 0 && <div className="mt-1 truncate text-[10px] text-(--viz-sev-medium)">等待 {unmet.length} 个依赖</div>}
    <Handle type="source" position={Position.Right} />
  </div>
}

const nodeTypes: NodeTypes = { planTask: PlanTaskNode }

function PlanDagInner({ tasks, sessions, onOpenSession, onOpenTask }: Props) {
  const initial = useMemo(() => buildPlanFlow(tasks, sessions, { onOpenSession, onOpenTask }), [])
  const [nodes, setNodes] = useState(initial.nodes)
  const [edges, setEdges] = useState<Edge[]>(initial.edges)
  const [cycle, setCycle] = useState(initial.cycle)
  const measured = useRef(new Map<string, { width: number; height: number }>())
  const topology = useMemo(() => tasks.map((task) => `${task.id}:${task.depends_on.join(",")}:${task.priority}:${task.created_at}`).join("|"), [tasks])
  const topologyRef = useRef("")
  const fittedRef = useRef(false)
  const updateNodeInternals = useUpdateNodeInternals()
  const rf = useReactFlow()

  useEffect(() => {
    const next = buildPlanFlow(tasks, sessions, { onOpenSession, onOpenTask })
    setNodes(next.nodes.map((node) => {
      const size = measured.current.get(node.id)
      return size ? { ...node, measured: size, style: { ...node.style, ...size } } : node
    }))
    setEdges(next.edges)
    setCycle(next.cycle)
    if (topologyRef.current !== topology) {
      topologyRef.current = topology
      fittedRef.current = false
    }
  }, [tasks, sessions, onOpenSession, onOpenTask, topology])

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    for (const change of changes) {
      if (change.type === "dimensions" && change.dimensions?.width && change.dimensions?.height) {
        measured.current.set(change.id, change.dimensions)
      }
    }
    setNodes((current) => {
      let next = current
      for (const change of changes) {
        if (change.type === "dimensions" && change.dimensions) {
          next = next.map((node) => node.id === change.id
            ? { ...node, measured: change.dimensions, style: { ...node.style, ...change.dimensions } }
            : node)
        }
      }
      return next
    })
  }, [])

  useEffect(() => {
    if (!nodes.length) return
    let tick = 0
    const timer = window.setInterval(() => {
      tick += 1
      const pending = nodes.filter((node) => !measured.current.has(node.id)).map((node) => node.id)
      if (!pending.length || tick > 40) { window.clearInterval(timer); return }
      updateNodeInternals(pending)
    }, 60)
    return () => window.clearInterval(timer)
  }, [nodes, updateNodeInternals])

  useEffect(() => {
    if (fittedRef.current || !nodes.length || !nodes.every((node) => measured.current.has(node.id))) return
    fittedRef.current = true
    const timer = window.setTimeout(() => { void rf.fitView({ padding: 0.2, maxZoom: 1, duration: 0 }) }, 0)
    return () => window.clearTimeout(timer)
  }, [nodes, rf])

  return <div className="plan-dag-canvas relative h-[min(62vh,680px)] min-h-[420px] overflow-hidden rounded border bg-background/40">
    {cycle && <div className="absolute left-2 top-2 z-10 rounded border border-(--status-error) bg-background px-2 py-1 text-[10px] text-(--status-error)">依赖图存在循环，请检查计划</div>}
    <ReactFlow nodes={nodes} edges={edges} onNodesChange={onNodesChange} nodesDraggable={false} nodesConnectable={false} nodeTypes={nodeTypes} proOptions={{ hideAttribution: true }}>
      <Background gap={24} size={1} />
      <Controls showInteractive={false} position="bottom-left" />
    </ReactFlow>
  </div>
}

export function PlanDag(props: Props) {
  return <ReactFlowProvider><PlanDagInner {...props} /></ReactFlowProvider>
}
