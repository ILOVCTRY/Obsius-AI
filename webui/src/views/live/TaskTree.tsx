import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, BackgroundVariant, MiniMap, Controls,
  useReactFlow, useUpdateNodeInternals, useNodesState, useEdgesState,
  MarkerType, Handle, Position,
  type Edge, type Node, type NodeChange, type NodeProps, type NodeTypes,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "./tree.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import { Crosshair, Maximize2, X } from "lucide-react"
import { api } from "@/lib/api"
import { fmtDateTime } from "@/lib/datetime"
import type { TaskTree, TaskTreeNode } from "@/lib/types"

// 任务尝试树 v2（task-attempt-tree，2026-09-27 意图驱动改版）：
// 目标 → 意图（一句可证伪假设）→ 检验结果（发现/死路）→ 新发现下再长新意图。
// v1 的计划步主干+命令/工具动作叶整体退役（用户拍板：树里不看命令，直播流自会显示）。
// 数据=GET /tree/{task_id} 现算 nodes 平铺带 parent，本组件按 parent 组树 →
// 深度分列 tidy-tree 布局。3s 轮询 + wsBump 去抖重拉；open 意图=当前节点，
// 脉冲高亮 + 自动跟随（手动拖画布即停，点「当前」恢复）。

const ROOT_W = 264
const ROOT_H = 96
const INTENT_W = 264
const INTENT_H = 72 // 陈述 clamp-2 + 徽章行（死路带死因行）
const FINDING_W = 240
const FINDING_H = 38
const BUCKET_W = 220
const BUCKET_H = 34
const COL_X = 320 // 列距
const GAP_Y = 18

type TreeCardData = {
  variant: "root" | "intent" | "finding" | "bucket"
  label: string
  sub?: string
  badge?: { text: string; cls: string }
  sev?: string
  current?: boolean
}

function outcomeBadge(n: Extract<TaskTreeNode, { kind: "intent" }>): { text: string; cls: string } {
  if (n.status === "open") return { text: "进行中", cls: "border-(--status-doing) text-(--status-doing)" }
  if (n.outcome_type === "dead_end") return { text: "✕ 死路", cls: "border-(--viz-node-deadend) text-(--viz-node-deadend)" }
  if (n.outcome_type === "vuln") return { text: "✅ 漏洞", cls: "border-(--viz-sev-critical) text-(--viz-sev-critical)" }
  if (n.outcome_type === "finding") return { text: "🔵 发现", cls: "border-(--viz-node-finding) text-(--viz-node-finding)" }
  return { text: "已收尾", cls: "border-(--viz-node-deadend) text-(--viz-node-deadend)" }
}

const SEV_CLS: Record<string, string> = {
  critical: "border-l-(--viz-sev-critical)",
  high: "border-l-(--viz-sev-high)",
  medium: "border-l-(--viz-sev-medium)",
  low: "border-l-(--viz-sev-low)",
  info: "border-l-(--viz-sev-info)",
}

// 边渲染前提（2026-09-28 问题1 复发根因）：xyflow 的 getEdgePosition 用
// querySelectorAll('.source'/'target') 收集节点 Handle 作为连接锚点，节点没有任何
// Handle → handleBounds 为 null → 边整体不渲染（AttackPath 每个节点都带 Handle 才正常）。
// Handle 仅作锚点，视觉透明隐藏；position 左 target/右 source 与左→右流向匹配。
const HIDE_HANDLE = { opacity: 0, width: 4, height: 4, background: "transparent", border: "none" } as const

function TreeCard({ data }: NodeProps<Node<TreeCardData>>) {
  if (data.variant === "root") {
    return (
      <div style={{ width: ROOT_W, minHeight: ROOT_H }}
        className="cursor-pointer rounded-md border border-(--status-doing) bg-(--viz-card) px-3 py-2 shadow-lg">
        <div className="text-[10px] uppercase tracking-wider text-(--viz-text-muted)">目标</div>
        <div className="mt-1 line-clamp-3 text-[13px] leading-snug text-(--viz-text)">{data.label}</div>
        <Handle type="source" position={Position.Right} style={HIDE_HANDLE} />
      </div>
    )
  }
  if (data.variant === "intent") {
    const b = data.badge
    return (
      <div style={{ width: INTENT_W }}
        className={`cursor-pointer rounded-md border bg-(--viz-card) px-3 py-2 shadow-md ${
          data.current
            ? "border-(--status-doing) shadow-[0_0_10px_var(--viz-chain-glow)]"
            : "border-(--viz-edge)"}`}>
        <div className="flex items-start gap-2">
          <div title={data.label}
            className={`min-w-0 flex-1 line-clamp-2 text-[12px] leading-snug text-(--viz-text) ${
              data.current ? "animate-pulse" : ""}`}>
            {data.label}
          </div>
          {b && <span className={`shrink-0 rounded border px-1 py-px text-[10px] ${b.cls}`}>{b.text}</span>}
        </div>
        {data.sub && (
          <div title={data.sub} className="mt-1 line-clamp-1 text-[10px] leading-snug text-(--viz-text-muted)">
            {data.sub}
          </div>
        )}
        <Handle type="target" position={Position.Left} style={HIDE_HANDLE} />
        <Handle type="source" position={Position.Right} style={HIDE_HANDLE} />
      </div>
    )
  }
  if (data.variant === "finding") {
    return (
      <div style={{ width: FINDING_W }}
        className={`cursor-pointer rounded border border-(--viz-edge) border-l-2 bg-(--viz-card) px-2 py-1.5 shadow-sm ${
          SEV_CLS[data.sev ?? "info"] ?? SEV_CLS.info}`}>
        <div title={data.label} className="line-clamp-1 text-[11px] leading-snug text-(--viz-text-soft)">{data.label}</div>
        <div className="mt-0.5 text-[10px] text-(--viz-text-muted)">{data.sub}</div>
        <Handle type="target" position={Position.Left} style={HIDE_HANDLE} />
        <Handle type="source" position={Position.Right} style={HIDE_HANDLE} />
      </div>
    )
  }
  return (
    <div style={{ width: BUCKET_W }}
      className="cursor-pointer rounded border border-dashed border-(--viz-edge) bg-(--viz-canvas) px-2 py-1.5">
      <div className="text-[11px] text-(--viz-text-muted)">{data.label}</div>
      <Handle type="target" position={Position.Left} style={HIDE_HANDLE} />
      <Handle type="source" position={Position.Right} style={HIDE_HANDLE} />
    </div>
  )
}

const NODE_TYPES: NodeTypes = { tree: TreeCard }

// ---------- 组树布局（tidy tree：子树高度递归，节点对子块垂直居中） ----------

type Layout = {
  tree: TaskTree
  pos: Map<string, { x: number; y: number }>
  rootLevel: TaskTreeNode[]
}

function buildLayout(tree: TaskTree): Layout {
  const byParent = new Map<string, TaskTreeNode[]>()
  for (const n of tree.nodes) {
    // 游离发现归兜底桶（仅历史数据；新数据被 bb_add_finding 门禁拦死）
    const key = n.kind === "finding" && !n.parent ? "_orphan" : n.parent
    const arr = byParent.get(key)
    if (arr) arr.push(n)
    else byParent.set(key, [n])
  }
  for (const arr of byParent.values()) {
    // bucket 无 created_at，视作最旧（殿后）
    arr.sort((a, b) => ((a as { created_at?: string }).created_at ?? "")
      .localeCompare((b as { created_at?: string }).created_at ?? ""))
  }

  const size = (n: TaskTreeNode): { w: number; h: number } =>
    n.kind === "intent"
      ? { w: INTENT_W, h: INTENT_H }
      : n.kind === "finding"
        ? { w: FINDING_W, h: FINDING_H }
        : { w: BUCKET_W, h: BUCKET_H }

  const subtreeH = (n: TaskTreeNode): number => {
    const kids = byParent.get(n.id) ?? []
    if (kids.length === 0) return size(n).h
    const kidH = kids.reduce((acc, k) => acc + subtreeH(k), 0) + GAP_Y * (kids.length - 1)
    return Math.max(size(n).h, kidH)
  }

  const pos = new Map<string, { x: number; y: number }>()
  const place = (n: TaskTreeNode, x: number, yTop: number) => {
    const kids = byParent.get(n.id) ?? []
    const { h: nh } = size(n)
    let y = yTop
    for (const k of kids) {
      place(k, x + COL_X, y)
      y += subtreeH(k) + GAP_Y
    }
    const kidBottom = kids.length ? y - GAP_Y : yTop
    pos.set(n.id, { x, y: kids.length ? (yTop + kidBottom) / 2 - nh / 2 : yTop })
  }

  const rootLevel = byParent.get("") ?? []
  let y = 0
  for (const r of rootLevel) {
    place(r, COL_X, y)
    y += subtreeH(r) + GAP_Y
  }
  // 根节点对根层子块垂直居中（无根层子块时居中于原点）
  const rootY = rootLevel.length ? (y - GAP_Y) / 2 : 0
  pos.set("__root", { x: 0, y: rootY - ROOT_H / 2 })
  return { tree, pos, rootLevel }
}

// ---------- 详情面板（2026-09-28 问题4：卡片文本截断，点击看全量字段） ----------

function DetailField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mb-2.5">
      <div className="text-[10px] uppercase tracking-wider text-(--viz-text-muted)">{label}</div>
      <div className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-(--viz-text)">{children}</div>
    </div>
  )
}

function NodeDetail({ selId, node, tree, onClose }: {
  selId: string
  node: TaskTreeNode | null
  tree: TaskTree
  onClose: () => void
}) {
  let title = "详情"
  let body: ReactNode
  if (selId === "__root") {
    title = "目标"
    body = (
      <>
        <DetailField label="目标">{tree.task.objective}</DetailField>
        <DetailField label="类型 / 状态">{tree.task.task_type} · {tree.task.status}</DetailField>
        <DetailField label="结果备注">{tree.task.result_note || "—"}</DetailField>
      </>
    )
  } else if (node?.kind === "intent") {
    title = "意图"
    body = (
      <>
        <DetailField label="假设陈述">{node.statement}</DetailField>
        <DetailField label="状态">
          {node.status === "open"
            ? "● 进行中"
            : node.outcome_type === "dead_end" ? "✕ 死路"
            : node.outcome_type === "vuln" ? "✅ 漏洞"
            : node.outcome_type === "finding" ? "🔵 发现" : "已收尾"}
        </DetailField>
        {node.status === "closed" && node.dead_reason && (
          <DetailField label="死因">{node.dead_reason}</DetailField>
        )}
        <DetailField label="创建时间">{fmtDateTime(node.created_at)}</DetailField>
        {node.closed_at && <DetailField label="收尾时间">{fmtDateTime(node.closed_at)}</DetailField>}
      </>
    )
  } else if (node?.kind === "finding") {
    title = "发现"
    body = (
      <>
        <DetailField label="标题">{node.title || "—"}</DetailField>
        {node.vuln_class && <DetailField label="漏洞类型">{node.vuln_class}</DetailField>}
        <DetailField label="严重级 / 状态">{node.severity} · {node.status}</DetailField>
        <DetailField label="创建时间">{fmtDateTime(node.created_at)}</DetailField>
      </>
    )
  } else {
    title = "孤儿发现"
    body = <DetailField label="说明">{node?.title ?? "—"}</DetailField>
  }
  return (
    <div className="absolute bottom-3 right-3 top-3 z-20 flex w-[min(380px,88%)] flex-col rounded-md border border-(--viz-edge) bg-(--viz-canvas)/95 shadow-xl">
      <div className="flex items-center gap-2 border-b border-(--viz-outline) px-3 py-2">
        <span className="text-xs font-medium text-(--viz-text)">{title}</span>
        <span className="flex-1" />
        <button type="button" onClick={onClose} className="text-(--viz-text-muted) hover:text-(--viz-text)">
          <X size={14} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">{body}</div>
    </div>
  )
}

// ---------- 主视图 ----------

function TreeInner({ pid, activeTaskId, wsBump }: {
  pid: string
  activeTaskId?: string | null
  wsBump: number
}) {
  const [tree, setTree] = useState<TaskTree | null>(null)
  const [taskId, setTaskId] = useState<string | null>(activeTaskId ?? null)
  const [tasks, setTasks] = useState<{ id: string; objective: string; status: string }[]>([])
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<TreeCardData>>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [err, setErr] = useState("")
  const [follow, setFollow] = useState(true)
  const rf = useReactFlow()
  const measuredRef = useRef<Set<string>>(new Set())
  const fittedRef = useRef(false)
  const lastCurrentRef = useRef<string | null>(null)
  const treeSigRef = useRef("")
  const [selId, setSelId] = useState<string | null>(null)
  // 详情面板选中节点（null + selId!=="__root" 时由 selValid 门禁兜底不渲染）
  const selNode = useMemo(
    () => (tree && selId && selId !== "__root" ? tree.nodes.find((n) => n.id === selId) ?? null : null),
    [tree, selId],
  )
  const [measuredTick, setMeasuredTick] = useState(0)
  const updateNodeInternals = useUpdateNodeInternals()

  // 任务清单（选择器）：3s 轮询
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const list = await api.tasks(pid)
        if (alive) setTasks(list.map((t) => ({ id: t.id, objective: t.objective, status: t.status })))
      } catch { /* 轮询容错 */ }
    }
    void load()
    const h = setInterval(load, 3000)
    return () => {
      alive = false
      clearInterval(h)
    }
  }, [pid])

  // 树数据：taskId 变化 / 轮询 / wsBump（300ms 去抖）重拉。
  // 内容签名短路（2026-09-28 问题3）：轮询命中相同内容则跳过 setTree——否则每 3s 一个新
  // 引用触发 layout 重算 → nodes 全量重建 + fittedRef 复位 → fitView 把用户缩放复原。
  useEffect(() => {
    if (!taskId) return
    let alive = true
    const apply = (t: TaskTree) => {
      if (!alive) return
      const sig = JSON.stringify([t.task.objective, t.current.intent_id, t.nodes])
      if (sig === treeSigRef.current) return
      treeSigRef.current = sig
      setTree(t)
      setErr("")
    }
    const h = setTimeout(async () => {
      try {
        apply(await api.taskTree(pid, taskId))
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : String(e))
      }
    }, wsBump ? 300 : 0)
    const poll = setInterval(() => {
      api.taskTree(pid, taskId).then(apply).catch(() => { /* 轮询容错 */ })
    }, 3000)
    return () => {
      alive = false
      clearTimeout(h)
      clearInterval(poll)
    }
  }, [pid, taskId, wsBump])

  // 默认任务：activeTaskId 在清单 > 首个 claimed > 第一个
  useEffect(() => {
    if (taskId || tasks.length === 0) return
    const pick = (activeTaskId && tasks.find((t) => t.id === activeTaskId)?.id)
      || tasks.find((t) => t.status === "claimed")?.id
      || tasks[0].id
    setTaskId(pick)
  }, [tasks, taskId, activeTaskId])

  // 会话页签切换跟随（2026-09-28 问题5）：activeTaskId 变化即同步任务选择；
  // 下拉手动切任务不受影响——只在 prop 值真正变化时才同步
  const lastPropTaskRef = useRef(activeTaskId ?? null)
  useEffect(() => {
    if (!activeTaskId || activeTaskId === lastPropTaskRef.current) return
    lastPropTaskRef.current = activeTaskId
    setTaskId(activeTaskId)
    lastCurrentRef.current = null
  }, [activeTaskId])

  const layout = useMemo(() => (tree ? buildLayout(tree) : null), [tree])

  // 布局 → xyflow nodes/edges（边始终全量供给；ready 只控 fitView）
  useEffect(() => {
    if (!layout) {
      setNodes([])
      setEdges([])
      return
    }
    const { tree: t, pos } = layout
    const card = (id: string): Node<TreeCardData> => {
      const p = pos.get(id) ?? { x: 0, y: 0 }
      if (id === "__root") {
        return { id, type: "tree", position: p, draggable: false, data: { variant: "root", label: t.task.objective } }
      }
      const n = t.nodes.find((x) => x.id === id)!
      if (n.kind === "intent") {
        return {
          id,
          type: "tree",
          position: p,
          draggable: false,
          data: {
            variant: "intent",
            label: n.statement,
            sub: n.status === "closed" && n.outcome_type === "dead_end" && n.dead_reason
              ? `死因：${n.dead_reason}`
              : undefined,
            badge: outcomeBadge(n),
            current: t.current.intent_id === id,
          },
        }
      }
      if (n.kind === "finding") {
        return {
          id,
          type: "tree",
          position: p,
          draggable: false,
          data: {
            variant: "finding",
            label: n.title || n.vuln_class || n.id,
            sub: `${n.severity} · ${n.status}${n.vuln_class ? ` · ${n.vuln_class}` : ""}`,
            sev: n.severity,
          },
        }
      }
      return { id, type: "tree", position: p, draggable: false, data: { variant: "bucket", label: n.title } }
    }
    const ns = [...pos.keys()].map(card)
    // 边样式（2026-09-28 问题1+2）：暗边提亮一档 + 闭合箭头，方向边可见；
    // 活跃链（当前意图沿 parent 上溯到根）用青色流动动画，「目标→意图→发现/死路→新意图」
    // 推理链一眼可辨
    const chain = new Set<string>(["__root"])
    const byId = new Map(t.nodes.map((n) => [n.id, n]))
    let cur = t.current.intent_id
    while (cur && byId.has(cur)) {
      chain.add(cur)
      cur = byId.get(cur)!.parent
    }
    const dim = "var(--viz-edge-muted)"
    const lit = "var(--viz-chain-active)"
    const mkEdge = (source: string, target: string): Edge => {
      const on = chain.has(source) && chain.has(target)
      return {
        id: `e:${source}->${target}`,
        source,
        target,
        type: "smoothstep",
        animated: on,
        style: { stroke: on ? lit : dim, strokeWidth: on ? 1.8 : 1.2 },
        markerEnd: { type: MarkerType.ArrowClosed, width: 15, height: 15, color: on ? lit : dim },
      }
    }
    const es: Edge[] = []
    for (const n of t.nodes) {
      const target = n.kind === "finding" && !n.parent ? "_orphan" : n.parent
      if (!target) continue
      es.push(mkEdge(target, n.id))
    }
    for (const r of layout.rootLevel) {
      es.push(mkEdge("__root", r.id))
    }
    setNodes(ns)
    setEdges(es)
    measuredRef.current = new Set()
    fittedRef.current = false
    setMeasuredTick((v) => v + 1) // 触发测量重试链
  }, [layout, setNodes, setEdges])

  // v12 受控节点契约：回收 dimensions 回灌 measured，否则重渲染清测量、边卸载
  const onNodesChangeCb = useCallback((changes: NodeChange<Node<TreeCardData>>[]) => {
    for (const ch of changes) {
      if (ch.type === "dimensions" && ch.dimensions?.width && ch.dimensions?.height) {
        measuredRef.current.add(ch.id)
      }
    }
    onNodesChange(changes)
  }, [onNodesChange])

  // 测量重试链（setTimeout 强测，勿用 rAF——后台标签页冻结 rAF/RO）
  useEffect(() => {
    if (nodes.length === 0) return
    let ticks = 0
    const h = setInterval(() => {
      ticks += 1
      const unmeasured = nodes.filter((n) => !measuredRef.current.has(n.id))
      if (unmeasured.length === 0 || ticks > 40) {
        clearInterval(h)
        return
      }
      updateNodeInternals(unmeasured.map((n) => n.id))
    }, 60)
    return () => clearInterval(h)
  }, [measuredTick, nodes, updateNodeInternals])

  // 全部测量完成 → fit 一次（rAF 下一帧等 xyflow 内部消费 measured）
  useEffect(() => {
    if (fittedRef.current || nodes.length === 0) return
    if (!nodes.every((n) => measuredRef.current.has(n.id))) return
    fittedRef.current = true
    requestAnimationFrame(() => {
      void rf.fitView({ padding: 0.12, maxZoom: 1, duration: 0 })
    })
  }, [nodes, rf])

  // 跟随当前意图：current 变化自动 pan；手动拖拽停跟随，「当前」恢复
  const currentId = tree?.current.intent_id ?? null
  useEffect(() => {
    if (!currentId || !follow || !fittedRef.current) return
    if (lastCurrentRef.current === currentId) return
    lastCurrentRef.current = currentId
    const p = layout?.pos.get(currentId)
    if (p) {
      void rf.setCenter(p.x + INTENT_W / 2, p.y + INTENT_H / 2, { zoom: rf.getZoom(), duration: 400 })
    }
  }, [currentId, follow, layout, rf])

  const panToCurrent = useCallback(() => {
    if (!currentId) return
    setFollow(true)
    const p = layout?.pos.get(currentId)
    if (p) {
      void rf.setCenter(p.x + INTENT_W / 2, p.y + INTENT_H / 2, {
        zoom: Math.max(rf.getZoom(), 0.9),
        duration: 400,
      })
    }
  }, [currentId, layout, rf])

  const currentIntent = tree?.nodes.find((n) => n.id === currentId)
  // 详情面板有效性：切任务后旧选中 id 失效即隐藏（selNode 为 null 且非 root）
  const selValid = !!selId && (selId === "__root" || !!tree?.nodes.some((n) => n.id === selId))

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-(--viz-outline) px-3 py-1.5">
        <span className="shrink-0 text-xs text-(--viz-text-muted)">任务树</span>
        <select
          className="max-w-72 min-w-0 truncate rounded border border-(--viz-edge) bg-(--viz-card) px-2 py-1 text-xs text-(--viz-text-soft)"
          value={taskId ?? ""}
          onChange={(e) => {
            setTaskId(e.target.value)
            lastCurrentRef.current = null
          }}
        >
          {tasks.map((t) => (
            <option key={t.id} value={t.id}>
              {t.status === "claimed" ? "▶ " : ""}{t.objective}
            </option>
          ))}
        </select>
        {currentIntent && currentIntent.kind === "intent" && (
          <>
            <span className="hidden min-w-0 truncate text-xs text-(--status-doing) xl:inline">
              ● {currentIntent.statement.slice(0, 24)}…
            </span>
            <button type="button" onClick={panToCurrent} title="回到当前意图节点"
              className="flex shrink-0 items-center gap-1 rounded border border-(--viz-edge) px-2 py-1 text-xs text-(--viz-text-soft) hover:bg-(--viz-outline)">
              <Crosshair size={13} /> 当前
            </button>
          </>
        )}
        <span className="flex-1" />
        <button type="button" title="适应视图"
          onClick={() => void rf.fitView({ padding: 0.12, maxZoom: 1, duration: 300 })}
          className="flex shrink-0 items-center gap-1 rounded border border-(--viz-edge) px-2 py-1 text-xs text-(--viz-text-soft) hover:bg-(--viz-outline)">
          <Maximize2 size={13} />
        </button>
      </div>
      {err && <div className="px-3 py-1 text-xs text-(--viz-sev-critical)">{err}</div>}
      <div className="min-h-0 flex-1">
        <ReactFlow<Node<TreeCardData>>
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChangeCb}
          onEdgesChange={onEdgesChange}
          onNodeClick={(_, n) => setSelId(n.id)}
          onPaneClick={() => setSelId(null)}
          nodeTypes={NODE_TYPES}
          nodesConnectable={false}
          onMoveStart={() => setFollow(false)}
          minZoom={0.2}
          maxZoom={1.6}
          proOptions={{ hideAttribution: true }}>
          <Background variant={BackgroundVariant.Dots} gap={22} size={1.4} color="var(--viz-card-hover)" />
          <Controls showInteractive={false} position="bottom-left" />
          <MiniMap pannable zoomable bgColor="var(--viz-canvas)" maskColor="var(--viz-canvas-mask)"
            nodeColor="var(--viz-edge)" />
        </ReactFlow>
        {tree && selId && selValid && (
          <NodeDetail selId={selId} node={selNode} tree={tree} onClose={() => setSelId(null)} />
        )}
      </div>
    </div>
  )
}

export function TaskTreeView(props: { pid: string; activeTaskId?: string | null; wsBump: number }) {
  return (
    <ReactFlowProvider>
      <TreeInner {...props} />
    </ReactFlowProvider>
  )
}
