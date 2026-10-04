import { Handle, Position, ReactFlow, Background, Controls, type NodeProps, type NodeTypes } from "@xyflow/react"
import { Activity, AlertTriangle, Check, CircleDot } from "lucide-react"
import type { Node } from "@xyflow/react"
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

export function PlanDag({ tasks, sessions, onOpenSession, onOpenTask }: Props) {
  const { nodes, edges, cycle } = buildPlanFlow(tasks, sessions, { onOpenSession, onOpenTask })
  return <div className="relative h-[min(62vh,680px)] min-h-[420px] overflow-hidden rounded border bg-background/40">
    {cycle && <div className="absolute left-2 top-2 z-10 rounded border border-(--status-error) bg-background px-2 py-1 text-[10px] text-(--status-error)">依赖图存在循环，请检查计划</div>}
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} nodesDraggable={false} nodesConnectable={false} fitView fitViewOptions={{ padding: 0.2 }}>
      <Background gap={24} size={1} />
      <Controls />
    </ReactFlow>
  </div>
}
