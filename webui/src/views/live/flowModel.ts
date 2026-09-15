import type { TaskGraphNode, TaskPlanStep } from "@/lib/types"

// A3 任务流布局（DESIGN §12 直播间任务流）：parent 分解结构天然是森林，
// 列=parent 深度（左→右），同列纵向按 状态/优先级/更新时间 堆叠。
// 纯函数；手动偏移存 localStorage，不进这里。

export const NODE_W = 232
export const NODE_H_EST = 104 // 估算行高（实测高度由 xyflow measured 回灌，仅影响初始堆叠节奏）
export const COL_GAP = 84
export const ROW_GAP = 18
const PAD_X = 32
const PAD_Y = 56 // 给顶部工具条留位

export type Slot = { x: number; y: number }

export function layoutTasks(nodes: TaskGraphNode[]): Map<string, Slot> {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const depthCache = new Map<string, number>()
  const depthOf = (id: string, guard: Set<string> = new Set()): number => {
    const cached = depthCache.get(id)
    if (cached !== undefined) return cached
    // parent 结构理论无环；guard 防御脏数据
    if (guard.has(id)) return 0
    const n = byId.get(id)
    if (!n || !n.parent_id || !byId.has(n.parent_id)) {
      depthCache.set(id, 0)
      return 0
    }
    guard.add(id)
    const d = depthOf(n.parent_id, guard) + 1
    guard.delete(id)
    depthCache.set(id, d)
    return d
  }

  const statusRank = (s: TaskGraphNode["status"]): number =>
    s === "claimed" ? 0 : s === "open" ? 1 : s === "failed" ? 2 : 3

  const ordered = [...nodes].sort((a, b) => {
    const da = depthOf(a.id)
    const db = depthOf(b.id)
    if (da !== db) return da - db
    const sa = statusRank(a.status)
    const sb = statusRank(b.status)
    if (sa !== sb) return sa - sb
    if (a.priority !== b.priority) return b.priority - a.priority
    return (b.updated_at ?? "").localeCompare(a.updated_at ?? "")
  })

  const rowByDepth = new Map<number, number>()
  const slots = new Map<string, Slot>()
  for (const n of ordered) {
    const d = depthOf(n.id)
    const row = rowByDepth.get(d) ?? 0
    rowByDepth.set(d, row + 1)
    slots.set(n.id, {
      x: PAD_X + d * (NODE_W + COL_GAP),
      y: PAD_Y + row * (NODE_H_EST + ROW_GAP),
    })
  }
  return slots
}

export type PlanStats = {
  total: number
  done: number
  doing?: TaskPlanStep
  blocked?: TaskPlanStep
}

export function planStats(plan: TaskPlanStep[]): PlanStats {
  return {
    total: plan.length,
    done: plan.filter((s) => s.status === "done").length,
    doing: plan.find((s) => s.status === "doing"),
    blocked: plan.find((s) => s.status === "blocked"),
  }
}
