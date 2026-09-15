import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, MiniMap, Controls,
  MarkerType, useReactFlow,
  type Edge, type NodeChange,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "./flow.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import { Maximize2, RotateCcw, X } from "lucide-react"
import { api } from "@/lib/api"
import type { TaskGraph, TaskGraphEdge, TaskGraphNode } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { TaskNode, type TaskFlowNodeType } from "./TaskNode"
import { TaskFlowEdge, type TaskFlowEdgeType } from "./TaskFlowEdge"
import { layoutTasks, NODE_W, planStats } from "./flowModel"

// A3 直播间任务流（DESIGN §12）：第三个 React Flow 图，经 React.lazy 分包。
// 节点=全部任务（四态持久）；实线=parent 分解；紫虚线 ✉=会话实际私信。
// 数据 3s 轮询 + 父级 LiveRoom 经 WS 相关事件计数 bump（wsBump），本组件不另开 WS。

const nodeTypes = { task: TaskNode }
const edgeTypes = { taskEdge: TaskFlowEdge }

const OFFSETS_KEY = (pid: string) => `taskflow-offsets-v1:${pid}`
type Offsets = Record<string, { dx: number; dy: number }>

function loadOffsets(pid: string): Offsets {
  try { return JSON.parse(localStorage.getItem(OFFSETS_KEY(pid)) ?? "{}") as Offsets }
  catch { return {} }
}

const NODE_STATUS_COLOR: Record<TaskGraphNode["status"], string> = {
  open: "#8b949e",
  claimed: "#39c5cf",
  done: "#3fb950",
  failed: "#f85149",
}

export type TaskFlowProps = {
  pid: string
  pausedSids: ReadonlySet<string>
  /** LiveRoom 过滤后的相关事件计数（task 前缀 / message.inbox / session 前缀）；变化即去抖重拉 */
  wsBump: number
  onAttachSession: (sid: string) => void
}

function Flow({ pid, pausedSids, wsBump, onAttachSession }: TaskFlowProps) {
  const rf = useReactFlow()
  const [graph, setGraph] = useState<TaskGraph | null>(null)

  const load = useCallback(() => {
    api.taskGraph(pid).then(setGraph).catch(() => {})
  }, [pid])
  useEffect(() => {
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [load])
  useEffect(() => {
    if (wsBump === 0) return
    const t = setTimeout(load, 300)
    return () => clearTimeout(t)
  }, [wsBump, load])

  // 手动偏移（相对自动槽位，localStorage 按项目持久，只本地不入库）
  const [offsets, setOffsets] = useState<Offsets>(() => loadOffsets(pid))
  useEffect(() => { setOffsets(loadOffsets(pid)) }, [pid])
  const saveOffsets = useCallback((next: Offsets) => {
    setOffsets(next)
    try { localStorage.setItem(OFFSETS_KEY(pid), JSON.stringify(next)) } catch { /* 满/隐私模式 */ }
  }, [pid])

  // v12 受控节点契约：dimensions change 必须回灌 measured，否则边卸载（见 webui/CLAUDE.md 坑）
  const [measuredById, setMeasuredById] = useState<Record<string, { width: number; height: number }>>({})
  const slots = useMemo(() => layoutTasks(graph?.nodes ?? []), [graph])

  const onNodesChangeCb = useCallback((changes: NodeChange[]) => {
    let dims: Record<string, { width: number; height: number }> | null = null
    const moved: Offsets = {}
    let hasMove = false
    let dragEnd: { dx: number; dy: number } | null = null
    for (const ch of changes) {
      if (ch.type === "dimensions" && ch.dimensions?.width && ch.dimensions?.height) {
        (dims ??= {})[ch.id] = { width: ch.dimensions.width, height: ch.dimensions.height }
      } else if (ch.type === "position" && ch.position) {
        const slot = slots.get(ch.id)
        if (slot) {
          const off = { dx: ch.position.x - slot.x, dy: ch.position.y - slot.y }
          moved[ch.id] = off
          hasMove = true
          if (ch.dragging === false) dragEnd = off
        }
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
  }, [slots, pid])

  // 首次测量齐了之后 fitView（maxZoom≤1），之后只由人手动触发
  const fittedRef = useRef(false)
  useEffect(() => {
    if (fittedRef.current || !graph || graph.nodes.length === 0) return
    if (!graph.nodes.every((n) => measuredById[n.id])) return
    fittedRef.current = true
    const raf = requestAnimationFrame(() => rf.fitView({ padding: 0.15, maxZoom: 1 }))
    return () => cancelAnimationFrame(raf)
  }, [graph, measuredById, rf])

  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const selectedEdge: TaskGraphEdge | null = useMemo(
    () => graph?.edges.find((e) => e.id === selectedEdgeId) ?? null,
    [graph, selectedEdgeId])

  // 双击：有活会话 → 挂回直播页签；open/会话已关 → 跳任务看板定位（App 监听 goto-tasks）
  const activateNode = useCallback((n: TaskGraphNode) => {
    if (n.claimed_by && n.session && n.session.status !== "closed") {
      onAttachSession(n.claimed_by)
    } else {
      window.dispatchEvent(new CustomEvent("goto-tasks", { detail: { taskId: n.id } }))
    }
  }, [onAttachSession])

  // 节点删除（四态皆可；claimed 警示，409 子任务错误留在对话框内）
  const [deleteTarget, setDeleteTarget] = useState<TaskGraphNode | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const askDelete = useCallback((n: TaskGraphNode) => {
    setDeleteError(null)
    setDeleteTarget(n)
  }, [])
  const doDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await api.deleteTask(deleteTarget.id)
      setDeleteTarget(null)
      load()
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(false)
    }
  }

  const flowNodes: TaskFlowNodeType[] = useMemo(() => (graph?.nodes ?? []).map((n) => {
    const slot = slots.get(n.id) ?? { x: 0, y: 0 }
    const off = offsets[n.id] ?? { dx: 0, dy: 0 }
    return {
      id: n.id,
      type: "task",
      position: { x: slot.x + off.dx, y: slot.y + off.dy },
      data: {
        node: n,
        paused: n.status === "claimed" && pausedSids.has(n.claimed_by ?? ""),
        onActivate: activateNode,
        onDelete: askDelete,
      },
      measured: measuredById[n.id],
      style: { width: NODE_W },
    }
  }), [graph, slots, offsets, measuredById, pausedSids, activateNode, askDelete])

  const flowEdges: TaskFlowEdgeType[] = useMemo(() => (graph?.edges ?? []).map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    type: "taskEdge",
    data: {
      kind: e.kind,
      refs: e.refs,
      selected: e.id === selectedEdgeId,
      onSelect: (id: string) => setSelectedEdgeId(id),
    },
    style: e.kind === "parent"
      ? { stroke: "#6e7681", strokeWidth: 1.5, opacity: 0.9 }
      : {
          stroke: "#a371f7",
          strokeWidth: e.id === selectedEdgeId ? 2 : 1.2,
          strokeDasharray: "5 4",
          opacity: e.id === selectedEdgeId ? 1 : 0.8,
        },
    markerEnd: e.kind === "parent"
      ? { type: MarkerType.ArrowClosed, color: "#6e7681", width: 14, height: 14 }
      : undefined,
    interactionWidth: 0,
  })), [graph, selectedEdgeId])

  return (
    <div className="tf-dark relative h-full w-full bg-[#0d1117]">
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges as Edge[]}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChangeCb}
        onPaneClick={() => setSelectedEdgeId(null)}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#30363d" gap={20} size={1} />
        <MiniMap
          nodeColor={(n) => {
            const st = (n.data as { node?: TaskGraphNode }).node?.status
            return st ? NODE_STATUS_COLOR[st] : "#30363d"
          }}
        />
        <Controls showInteractive={false} />
      </ReactFlow>

      {/* 工具条：图例 + 布局操作 */}
      <div className="absolute left-2 top-2 z-20 flex items-center gap-3 rounded-md border border-[#21262d] bg-[#0d1117]/90 px-2.5 py-1 text-[10px] text-muted-foreground">
        <span className="font-medium text-foreground">任务流</span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-5 border-t-2 border-[#6e7681]" />
          分解
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-5 border-t-2 border-dashed border-[#a371f7]" />
          ✉ 私信
        </span>
        <span className="hidden md:inline">双击节点挂回会话 / 跳看板</span>
        <button
          type="button" title="重置布局（清除手动位置）"
          className="rounded p-1 hover:bg-accent/40 hover:text-foreground"
          onClick={() => saveOffsets({})}
        >
          <RotateCcw className="size-3.5" />
        </button>
        <button
          type="button" title="适应视图"
          className="rounded p-1 hover:bg-accent/40 hover:text-foreground"
          onClick={() => rf.fitView({ padding: 0.15, maxZoom: 1 })}
        >
          <Maximize2 className="size-3.5" />
        </button>
      </div>

      {/* 空态 / 加载态 */}
      {graph === null ? (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
            任务流加载中…
          </p>
        </div>
      ) : graph.nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
            还没有任务——在任务看板或直播间插话发布一个，分解结构和私信会在这里成图
          </p>
        </div>
      )}

      {/* 选中私信边：依据浮卡 */}
      {selectedEdge && selectedEdge.kind === "inbox" && (
        <div className="absolute bottom-3 left-1/2 z-20 w-[26rem] max-w-[90%] -translate-x-1/2 rounded-md border border-[#a371f7]/40 bg-[#1c2128]/95 p-2.5 shadow-lg">
          <div className="flex items-center gap-2">
            <p className="text-[11px] font-medium text-[#d2a8ff]">✉ 私信协作边</p>
            <span className="flex-1" />
            <button
              type="button" title="关闭"
              className="rounded p-0.5 text-muted-foreground hover:text-foreground"
              onClick={() => setSelectedEdgeId(null)}
            >
              <X className="size-3.5" />
            </button>
          </div>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            同一依据触达两个会话（按其最近任务成边）：
          </p>
          <ul className="mt-1.5 space-y-1">
            {(selectedEdge.refs ?? []).map((r) => (
              <li key={`${r.kind}:${r.ref_id}`} className="flex items-start gap-1.5 text-[11px]">
                <span className={r.kind === "basis_stale"
                  ? "shrink-0 rounded bg-amber-400/15 px-1 text-amber-400"
                  : "shrink-0 rounded bg-sky-400/15 px-1 text-sky-400"}>
                  {r.kind === "basis_stale" ? "⚠ 依据撤回" : "🔵 发现增补"}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-foreground" title={r.title}>{r.title || "（无标题）"}</span>
                  <span className="font-mono text-[9px] text-muted-foreground">{r.ref_id}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 删除确认（四态皆可删；claimed 硬中断警示；有子任务 409 直显） */}
      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && !deleting && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除任务「{deleteTarget?.task_type}」？</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p className="line-clamp-3 rounded border bg-card p-2 font-mono text-xs">
                  {deleteTarget?.objective}
                </p>
                {deleteTarget && planStats(deleteTarget.plan).total > 0 && (
                  <p className="font-mono text-xs text-sky-400">
                    ▦ 计划 {planStats(deleteTarget.plan).done}/{planStats(deleteTarget.plan).total} 将随任务删除（审计事件保留）
                  </p>
                )}
                <p>
                  物理删除且不可恢复（留 task.deleted 审计）。
                  {deleteTarget?.status === "claimed" && (
                    <span className="text-[--status-approval]">
                      该任务正在执行——当前这一步做完后立即硬中断，执行中的工具调用不会被打断。
                    </span>
                  )}
                  {deleteTarget?.status === "done" && (
                    <span className="text-muted-foreground">已完成任务同样可删（战果快照进审计）。</span>
                  )}
                </p>
                {deleteError && <p className="text-sm text-[--status-error]">{deleteError}</p>}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <Button variant="outline" size="sm">取消</Button>
            </AlertDialogCancel>
            <Button variant="destructive" size="sm" onClick={doDelete} disabled={deleting}>
              {deleting ? "删除中…" : "删除"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

export function TaskFlow(props: TaskFlowProps) {
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  )
}
