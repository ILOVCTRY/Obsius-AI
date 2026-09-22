import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, MiniMap, Controls,
  MarkerType, useReactFlow, useUpdateNodeInternals,
  type Edge, type NodeChange,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "./flow.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import { Maximize2, RotateCcw, X } from "lucide-react"
import { api } from "@/lib/api"
import { sessionLabel } from "@/lib/roles"
import { fmtDateTimeMin, utcTitle } from "@/lib/datetime"
import type { TaskGraph, TaskGraphEdge, TaskGraphNode, TaskPlanStatus } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { TaskNode, type TaskFlowNodeType } from "./TaskNode"
import { TaskFlowEdge, type TaskFlowEdgeType } from "./TaskFlowEdge"
import { TaskTraceList } from "@/components/blackboard/TaskTraceList"
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

const STATUS_LABEL: Record<TaskGraphNode["status"], string> = {
  open: "待认领",
  claimed: "执行中",
  done: "完成",
  failed: "失败",
}

// 步骤状态图标对齐后端 _render_plan 与 PlanPanel（core/agent/tools.py）
const STEP_ICON: Record<TaskPlanStatus, string> = { todo: "○", doing: "▶", done: "●", blocked: "■" }
const STEP_CLS: Record<TaskPlanStatus, string> = {
  todo: "text-muted-foreground",
  doing: "text-primary",
  done: "text-emerald-400/80",
  blocked: "text-amber-400",
}

export type TaskFlowProps = {
  pid: string
  pausedSids: ReadonlySet<string>
  /** LiveRoom 过滤后的相关事件计数（task 前缀 / message.inbox / session 前缀）；变化即去抖重拉 */
  wsBump: number
}

function Flow({ pid, pausedSids, wsBump }: TaskFlowProps) {
  const rf = useReactFlow()
  const updateNodeInternals = useUpdateNodeInternals()
  const [graph, setGraph] = useState<TaskGraph | null>(null)
  // 角色中文名映射（存量会话页签/认领节点显中文，与 LiveRoom 同源 GET /roles）
  const [roleNames, setRoleNames] = useState<Record<string, string>>({})
  useEffect(() => {
    api.listRoles(pid).then((rs) => setRoleNames(
      Object.fromEntries(rs.map((r) => [r.role, r.name || r.role])),
    )).catch(() => {})
  }, [pid])

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
  // TaskFlow 在 LiveRoom 以 key={pid} 挂载，换项目即重建，初始化器即可
  const [offsets, setOffsets] = useState<Offsets>(() => loadOffsets(pid))
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

  // 测量兜底：React StrictMode 开发态双挂载会让全 flow 唯一的 ResizeObserver 在
  // disconnect 后不重连（节点 effect 误判已初始化不再 observe），dimensions 可能整批不到。
  // 对全部节点强制重测，收齐即停。注意后台标签页 rAF/RO 被 Chromium 冻结，此时不出边属
  // 浏览器行为，页面转可见后 RO 补发首包即恢复（生产构建无 StrictMode 双挂载）。
  const measuredCountRef = useRef(0)
  useEffect(() => { measuredCountRef.current = Object.keys(measuredById).length })
  useEffect(() => {
    if (!graph || graph.nodes.length === 0) return
    const total = graph.nodes.length
    let tries = 0
    let timer: ReturnType<typeof setTimeout>
    const tick = () => {
      if (measuredCountRef.current >= total) return
      if (tries++ >= 40) return // 至多 ~2.4s；3s 轮询换 graph 后会再起一轮
      updateNodeInternals(graph.nodes.map((n) => n.id))
      timer = setTimeout(tick, 60)
    }
    timer = setTimeout(tick, 0)
    return () => clearTimeout(timer)
  }, [graph, updateNodeInternals])

  // 全部节点测量完成才 fitView，避免视口在未测量时算错；边始终正常供给，无需两阶段喂边。
  const [ready, setReady] = useState(false)
  useEffect(() => {
    if (ready || !graph || graph.nodes.length === 0) return
    if (!graph.nodes.every((n) => measuredById[n.id])) return
    const t = setTimeout(() => setReady(true))
    return () => clearTimeout(t)
  }, [ready, graph, measuredById])

  // 定型挂载后 fitView（maxZoom≤1）一次，之后只由人手动触发
  useEffect(() => {
    if (!ready) return
    const raf = requestAnimationFrame(() => rf.fitView({ padding: 0.15, maxZoom: 1 }))
    return () => cancelAnimationFrame(raf)
  }, [ready, rf])

  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null)
  const selectedEdge: TaskGraphEdge | null = useMemo(
    () => graph?.edges.find((e) => e.id === selectedEdgeId) ?? null,
    [graph, selectedEdgeId])

  // 单击节点 → 详情浮卡（objective 全文 + plan 全步骤）；数据轮询更新时浮卡内容自动跟进。
  // 会话名与 flowNodes 同源映射角色中文名（TaskNode 直读 n.session.name）。
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const selectedNode: TaskGraphNode | null = useMemo(() => {
    const n = graph?.nodes.find((x) => x.id === selectedNodeId) ?? null
    if (!n) return null
    return n.session ? { ...n, session: { ...n.session, name: sessionLabel(n.session, roleNames) } } : n
  }, [graph, selectedNodeId, roleNames])

  // 双击节点（v0.71 任务即窗口）：统一 goto-session 直开专属执行窗页签
  // （target_session 优先，缺省回退认领会话；两者皆无才回退任务看板定位）
  const activateNode = useCallback((n: TaskGraphNode) => {
    const sid = n.target_session || n.claimed_by
    if (sid) {
      window.dispatchEvent(new CustomEvent("goto-session", { detail: { sessionId: sid } }))
    } else {
      window.dispatchEvent(new CustomEvent("goto-tasks", { detail: { taskId: n.id } }))
    }
  }, [])

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
    // 存量会话 name=英文 role id：显示层映射为角色中文名（TaskNode 直读 n.session.name）
    const sess = n.session ? { ...n.session, name: sessionLabel(n.session, roleNames) } : undefined
    return {
      id: n.id,
      type: "task",
      position: { x: slot.x + off.dx, y: slot.y + off.dy },
      data: {
        node: sess ? { ...n, session: sess } : n,
        paused: n.status === "claimed" && pausedSids.has(n.claimed_by ?? ""),
        onActivate: activateNode,
        onDelete: askDelete,
      },
      measured: measuredById[n.id],
      style: { width: NODE_W },
    }
  }), [graph, slots, offsets, measuredById, pausedSids, activateNode, askDelete, roleNames])

  const flowEdges: TaskFlowEdgeType[] = useMemo(() => (graph?.edges ?? []).map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    type: "taskEdge",
    data: {
      kind: e.kind,
      refs: e.refs,
      selected: e.id === selectedEdgeId,
      // 两个浮卡互斥：选边清节点详情，选节点（onNodeClick）也清边
      onSelect: (id: string) => { setSelectedEdgeId(id); setSelectedNodeId(null) },
    },
    style: e.kind === "parent"
      ? { stroke: "#6e7681", strokeWidth: 1.5, opacity: 0.9 }
      : e.kind === "suggest"
        ? { stroke: "#6e7681", strokeWidth: 1, strokeDasharray: "2 4", opacity: 0.55 }
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
        // 单击节点弹详情（xyflow 层挂：拖拽后不误触发，节点内删除钮 stopPropagation 天然隔离）
        onNodeClick={(_, node) => { setSelectedEdgeId(null); setSelectedNodeId(node.id) }}
        onPaneClick={() => { setSelectedEdgeId(null); setSelectedNodeId(null) }}
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
        <span className="hidden md:inline">单击节点看详情 · 双击挂回会话 / 跳看板</span>
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
            还没有任务——在任务看板或会话页插话发布一个，分解结构和私信会在这里成图
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

      {/* 单击节点：详情浮卡（objective 全文 + plan 全步骤；随 3s 轮询实时跟进） */}
      {selectedNode && (
        <div
          className="absolute bottom-3 left-1/2 z-20 w-[30rem] max-w-[90%] -translate-x-1/2 rounded-md border bg-[#1c2128]/95 p-2.5 shadow-lg"
          style={{ borderColor: `${NODE_STATUS_COLOR[selectedNode.status]}66` }}
        >
          <div className="flex items-center gap-1.5">
            <span className="min-w-0 truncate rounded bg-muted px-1 font-mono text-[9px] text-muted-foreground">
              {selectedNode.task_type}
            </span>
            {(selectedNode.attempts ?? 0) >= 2 && (
              <span className="shrink-0 font-mono text-[9px] text-muted-foreground" title="任务多次执行（上下文随任务保留，跨会话接手）">
                ↻{selectedNode.attempts}
              </span>
            )}
            <span className="shrink-0 font-mono text-[9px]" style={{ color: NODE_STATUS_COLOR[selectedNode.status] }}>
              {STATUS_LABEL[selectedNode.status]}
            </span>
            <span className="shrink-0 font-mono text-[9px] text-muted-foreground">P{selectedNode.priority}</span>
            {selectedNode.session && (
              <span className="min-w-0 flex-1 truncate text-right font-mono text-[9px] text-primary" title={`认领会话：${selectedNode.session.name || selectedNode.session.id}`}>
                ● {selectedNode.session.name || selectedNode.session.id.slice(0, 14)}
              </span>
            )}
            <span className="flex-1" />
            <button
              type="button" title="关闭"
              className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
              onClick={() => setSelectedNodeId(null)}
            >
              <X className="size-3.5" />
            </button>
          </div>

          <p className="mt-1 text-[10px] font-medium text-muted-foreground">任务描述</p>
          <p className="mt-0.5 max-h-32 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-snug">
            {selectedNode.objective}
          </p>

          <p className="mt-2 text-[10px] font-medium text-muted-foreground">
            任务计划
            {planStats(selectedNode.plan).total > 0 && (
              <span className="ml-1.5 font-mono text-sky-400">
                ▦ {planStats(selectedNode.plan).done}/{planStats(selectedNode.plan).total}
              </span>
            )}
          </p>
          {planStats(selectedNode.plan).total === 0 ? (
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              未制定计划（认领后经 A2 计划闸先写计划再动手）
            </p>
          ) : (
            <ol className="mt-0.5 max-h-40 space-y-1 overflow-auto">
              {selectedNode.plan.map((s) => (
                <li key={s.id} className="flex items-start gap-1.5 text-[11px]">
                  <span className={`shrink-0 font-mono ${STEP_CLS[s.status]}`} title={s.status}>
                    {STEP_ICON[s.status]}
                  </span>
                  <span className={`min-w-0 flex-1 ${s.status === "done" ? "text-muted-foreground line-through decoration-muted-foreground/40" : ""}`}>
                    {s.title}
                    {s.status === "blocked" && s.note && (
                      <span className="ml-1 text-amber-400">— {s.note}</span>
                    )}
                  </span>
                  <span className="shrink-0 font-mono text-[9px] text-muted-foreground" title={utcTitle(s.ts)}>
                    {fmtDateTimeMin(s.ts)}
                  </span>
                </li>
              ))}
            </ol>
          )}

          <p className="mt-2 text-[10px] font-medium text-muted-foreground">执行轨迹</p>
          <TaskTraceList pid={pid} taskId={selectedNode.id} className="mt-0.5 max-h-44 overflow-auto" />

          <p className="mt-1.5 text-[9px] text-muted-foreground">双击节点挂回会话 / 跳看板</p>
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
                    <span className="text-(--status-approval)">
                      该任务正在执行——当前这一步做完后立即硬中断，执行中的工具调用不会被打断。
                    </span>
                  )}
                  {deleteTarget?.status === "done" && (
                    <span className="text-muted-foreground">已完成任务同样可删（战果快照进审计）。</span>
                  )}
                </p>
                {deleteError && <p className="text-sm text-(--status-error)">{deleteError}</p>}
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
