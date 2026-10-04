import type { Edge, Node } from "@xyflow/react"
import type { CoordinationTask, Session } from "@/lib/types"

export type PlanNodeData = {
  task: CoordinationTask
  session?: Session
  unmet: string[]
  onOpenTask?: (taskId: string) => void
  onOpenSession?: (sessionId: string) => void
}

export function buildPlanFlow(tasks: CoordinationTask[], sessions: Session[], handlers: {
  onOpenTask?: (taskId: string) => void
  onOpenSession?: (sessionId: string) => void
}): { nodes: Node<PlanNodeData>[]; edges: Edge[]; cycle: boolean } {
  const byId = new Map(tasks.map((task) => [task.id, task]))
  const level = new Map<string, number>()
  const visiting = new Set<string>()
  const done = new Set<string>()
  let cycle = false
  const visit = (id: string): number => {
    if (visiting.has(id)) { cycle = true; return 0 }
    if (done.has(id)) return level.get(id) ?? 0
    visiting.add(id)
    const task = byId.get(id)
    const value = task ? Math.max(0, ...(task.depends_on.filter((dep) => byId.has(dep)).map((dep) => visit(dep) + 1))) : 0
    visiting.delete(id); done.add(id); level.set(id, value)
    return value
  }
  tasks.forEach((task) => visit(task.id))
  const columns = new Map<number, CoordinationTask[]>()
  for (const task of tasks) columns.set(level.get(task.id) ?? 0, [...(columns.get(level.get(task.id) ?? 0) ?? []), task])
  const nodeW = 280; const nodeH = 128; const colGap = 96; const rowGap = 22
  const nodes: Node<PlanNodeData>[] = []
  for (const [col, list] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    list.sort((a, b) => a.priority - b.priority || a.created_at.localeCompare(b.created_at))
    list.forEach((task, row) => {
      const session = task.task_id ? sessions.find((item) => item.bound_task_id === task.task_id) : undefined
      nodes.push({ id: `task:${task.id}`, type: "planTask", position: { x: col * (nodeW + colGap), y: row * (nodeH + rowGap) }, data: {
        task, session, unmet: task.depends_on.filter((dep) => byId.get(dep)?.status !== "completed"), ...handlers,
      }, draggable: false })
    })
  }
  const edges: Edge[] = []
  tasks.forEach((task) => task.depends_on.forEach((dep) => {
    if (byId.has(dep)) edges.push({ id: `dep:${dep}:${task.id}`, source: `task:${dep}`, target: `task:${task.id}`, type: "smoothstep", animated: task.status === "ready", style: { stroke: task.status === "blocked" ? "var(--viz-sev-medium)" : "var(--viz-edge-muted)", strokeWidth: 1.5 } })
  }))
  return { nodes, edges, cycle }
}
