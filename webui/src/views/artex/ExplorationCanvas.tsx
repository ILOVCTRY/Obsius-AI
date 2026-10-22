import { useCallback, useMemo, useRef, useState } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, BackgroundVariant, Controls,
  Handle, MarkerType, MiniMap, Position,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "../blackboard/canvas.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import {
  Bug, ChevronRight, Compass, FlaskConical, Flag, Layers, Lightbulb,
  Target, X, type LucideIcon,
} from "lucide-react"
import { cn } from "@/lib/utils"
import type { ArtexExplorationGraph, ArtexExplorationNode } from "@/lib/types"

// ARTEX 探索图画布（artex-exploration-canvas，2026-10-20）。
//
// 取代原来的 `ExplorationPanel` 卡片墙——那个实现是 `grid lg:grid-cols-2` 的
// 平铺列表，**没有任何空间编码**，节点之间的关联只靠每张卡底部一行「N 条关系」
// 文字。一个 25 节点的 CTF 任务就有 42 条边，人类根本看不出谁连着谁、谁先发生。
//
// 三条设计红线：
// 1. **只有主脊边参与分层**（spawns/yields）。derived_from 方向与主脊相反，若参与
//    自动分层会和主脊互相拉扯，布局必然缠绕；proves 是系统挂链（占全部边 43%），
//    它把单个目标节点入度撑到十几，画出来就是一团乱麻。
// 2. **标题=结论、副标题=意图**。后端已把 fact 标题从「Worker 结果：<objective>」
//    改成结论首句；卡面再用小字挂 objective，人类才分得清哪张卡是哪条。
// 3. **时间不靠读时间戳**。节点按分层固定位置，另在卡片右侧画时间刻度条——
//    `created_at` 归一化到任务时间跨度，一眼看出先后。

const X_STEP = 320
const CARD_W = 252
const CARD_H = 88
const ROW_GAP = 16
const COL_PAD = 36

type Role = "begin" | "goal" | "intent" | "fact" | "summary" | "finding" | "hint"

type KindMeta = {
  label: string
  icon: LucideIcon
  /** 卡头图标底色（tailwind 类，随主题走语义色）。 */
  iconBg: string
  /** 卡片左边框强调色。 */
  bar: string
  /** 图例/徽章底色。 */
  chip: string
  /** MiniMap 用的十六进制色（SVG 不认 tailwind 类）。 */
  hex: string
}

// 七种视觉角色：goal 按 role 分「起点/目标」，fact 按 role 分「结论/整轮汇总」。
const META: Record<Role, KindMeta> = {
  begin: { label: "起点", icon: Flag, iconBg: "bg-slate-500", bar: "border-l-slate-400/70", chip: "border-slate-500/40 bg-slate-500/10 text-slate-300", hex: "#64748b" },
  goal: { label: "目标", icon: Target, iconBg: "bg-emerald-500", bar: "border-l-emerald-400/80", chip: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300", hex: "#10b981" },
  intent: { label: "意图", icon: Compass, iconBg: "bg-blue-500", bar: "border-l-blue-400/80", chip: "border-blue-500/40 bg-blue-500/10 text-blue-300", hex: "#3b82f6" },
  fact: { label: "结论", icon: FlaskConical, iconBg: "bg-teal-500", bar: "border-l-teal-400/80", chip: "border-teal-500/40 bg-teal-500/10 text-teal-300", hex: "#14b8a6" },
  summary: { label: "整轮汇总", icon: Layers, iconBg: "bg-teal-700", bar: "border-l-teal-700/60", chip: "border-teal-700/40 bg-teal-700/10 text-teal-400/80", hex: "#0f766e" },
  finding: { label: "发现", icon: Bug, iconBg: "bg-rose-500", bar: "border-l-rose-400/90", chip: "border-rose-500/40 bg-rose-500/10 text-rose-300", hex: "#f43f5e" },
  hint: { label: "提示", icon: Lightbulb, iconBg: "bg-amber-500", bar: "border-l-amber-400/70", chip: "border-amber-500/40 bg-amber-500/10 text-amber-300", hex: "#f59e0b" },
}

// 边三档（与后端 EDGE_CATEGORY 同口径）。颜色沿用既有 viz token，不新造色。
const EDGE_STYLE: Record<string, { label: string; color: string; dash?: string; width: number; animated?: boolean }> = {
  spine: { label: "主脊", color: "var(--viz-chain-active, #22d3ee)", width: 1.8 },
  reverse: { label: "依据", color: "var(--viz-accent-orange, #f0883e)", dash: "6 4", width: 1.4 },
  system: { label: "挂链", color: "var(--viz-edge-muted, #484f58)", dash: "2 4", width: 1 },
}

function roleOf(n: ArtexExplorationNode): Role {
  // 后端 exploration_graph() 已派生 role；老 API/缓存缺字段时按 metadata 反推，
  // 避免「起点」被画成「目标」。
  const r = (n as { role?: string }).role
  if (r && r in META) return r as Role
  const src = String((n.metadata || {}).source || "")
  if (n.kind === "goal") return src ? "goal" : "begin"
  if (n.kind === "fact") return src === "intent_result" ? "summary" : "fact"
  if (n.kind === "intent" || n.kind === "finding" || n.kind === "hint") return n.kind
  return "intent"
}

/**
 * 卡片标题裁剪：去掉开头那段长路径。
 *
 * 实测意图标题形如「对 /workspace/artifacts/attachments/c78c2e5a9ea7_图片.png 做
 * 非 LSB 隐写与容器内嵌扫描」——两行 clamp 之后只剩「对 /workspace/artifacts/…」，
 * 七张卡看起来一模一样，等于没标题。把路径替换成文件名（或直接省略）后，
 * 「做非 LSB 隐写与容器内嵌扫描」这类真正区分方向的部分才露得出来。
 */
function shortTitle(text: string): string {
  const raw = String(text || "").split("\n")[0].trim()
  if (!raw) return ""
  // 长路径 → 文件名（保留扩展名，去掉目录）
  let out = raw.replace(/[A-Za-z]:[\\/][^\s，。；、）)]+|\/(?:[\w.\-]+\/){2,}[\w.\-]+/g,
    m => m.split(/[\\/]/).pop() || m)
  // 头部若只剩「对 xxx 做」，把引导词也去掉，让动作先出现
  out = out.replace(/^(对|从|在|用)\s*/, "")
  return out.trim() || raw
}

function isCollapsedByDefault(n: ArtexExplorationNode): boolean {
  const flag = (n as { collapsed_by_default?: boolean }).collapsed_by_default
  if (typeof flag === "boolean") return flag
  return n.kind === "fact" && String((n.metadata || {}).source || "") === "intent_result"
}

const STATUS_TONE: Record<string, string> = {
  open: "border-(--border) text-(--text-3)",
  pending: "border-slate-500/40 bg-slate-500/10 text-slate-300",
  claimed: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  running: "border-amber-500/50 bg-amber-500/15 text-amber-200",
  paused: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
  completed: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  closed: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  verified: "border-rose-500/40 bg-rose-500/10 text-rose-300",
  unverified: "border-rose-500/30 bg-rose-500/[0.06] text-rose-300/80",
  failed: "border-red-500/40 bg-red-500/10 text-red-300",
  cancelled: "border-slate-500/40 bg-slate-500/10 text-slate-400",
  exhausted: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
  met: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  blocked: "border-orange-500/40 bg-orange-500/10 text-orange-300",
}

// 状态文案**按节点类型分叉**（artex-status-label，2026-10-21）。
//
// 同一个 `open` 在不同 kind 上意思完全不同：goal 的 open = 「这个目标还没达成」，
// hint 的 open = 「这条提示还没用上」，fact/finding 的 open = 「还没被确认」。
// 一律渲染成「待处理」会让人以为它们都在排队等调度——实测截图里 15 个 hint
// 全显示「待处理」，而它们根本不是待办事项，只是记录下来的方向依据。
//
// 只给**确有歧义**的 kind 单独建表；其余沿用通用表（避免每个 kind 抄一份）。
const STATUS_LABEL_BY_KIND: Partial<Record<Role, Record<string, string>>> = {
  goal: {
    open: "未达成", met: "已达成", pending: "待登记",
    blocked: "受阻", cancelled: "已放弃",
  },
  hint: {
    open: "未采用", closed: "已采用", completed: "已采用",
  },
  fact: {
    open: "待确认", closed: "已确认", completed: "已确认",
    exhausted: "未采信", failed: "未采信",
  },
  finding: {
    open: "待确认", unverified: "待确认", verified: "已确认",
    closed: "已确认", completed: "已确认",
  },
}

const STATUS_LABEL: Record<string, string> = {
  open: "待处理", pending: "待调度", claimed: "已领取", running: "执行中",
  paused: "已暂停", completed: "已完成", closed: "已收尾", verified: "已确认",
  unverified: "未核实",
  failed: "失败", cancelled: "已取消", exhausted: "预算耗尽", met: "已达成",
  blocked: "阻塞",
}

/** 按 kind 取状态文案：先查该 kind 的专用表，再退回通用表，最后原样显示。 */
function statusLabel(kind: Role, status: string): string {
  const own = STATUS_LABEL_BY_KIND[kind]
  if (own && own[status]) return own[status]
  return STATUS_LABEL[status] || status
}

// ---------- 布局 ----------

type Pos = { x: number; y: number }

/**
 * 分层：spine 边算主脊，再按语义对「无 spine 边的节点」做二次归属。
 *
 * **只算 spine 的原因**：derived_from 方向与 spawns 相反，两者同时喂进拓扑排序
 * 会互相抵消、把节点压在中间层；proves 是「产出→目标」的长跨边，会让每个产出
 * 节点都向后连一条，把整图拉成蛛网。
 *
 * **必须二次归属的原因**（真实数据实测）：hint 与 finding 都没有 spine 边，
 * 拓扑排序把无前驱的节点一律算第 0 层——一个 25 节点的任务里 5 个 hint + 1 个
 * finding 全堆在起点那一列，语义上完全错位（hint 是意图的「依据」该在它左边，
 * finding 是事实的「发现」该在它右边）。
 *
 * 环安全：DFS 打标后把回边剔掉再拓扑（derived_from 手工挂链可能造出环）。
 */
function computeLayers(nodes: ArtexExplorationNode[], edges: Edge[]): Map<string, number> {
  const order = nodes.map(n => n.id)
  const idset = new Set(order)
  const spine = edges.filter(e => e.data?.category === "spine" && idset.has(e.source) && idset.has(e.target))

  const out = new Map<string, string[]>()
  for (const e of spine) {
    const arr = out.get(e.source)
    if (arr) arr.push(e.target)
    else out.set(e.source, [e.target])
  }

  // DFS 打标剔回边（0=未访问 1=栈上 2=已完成）
  const seen = new Map<string, 0 | 1 | 2>()
  const back = new Set<string>()
  for (const root of order) {
    if ((seen.get(root) ?? 0) !== 0) continue
    const stack: { u: string; i: number }[] = [{ u: root, i: 0 }]
    seen.set(root, 1)
    while (stack.length) {
      const top = stack[stack.length - 1]
      const nbrs = out.get(top.u) ?? []
      if (top.i >= nbrs.length) { seen.set(top.u, 2); stack.pop(); continue }
      const v = nbrs[top.i++]
      const s = seen.get(v) ?? 0
      if (s === 1) back.add(`${top.u}->${v}`)
      else if (s === 0) { seen.set(v, 1); stack.push({ u: v, i: 0 }) }
    }
  }

  const succ = new Map<string, string[]>()
  const indeg = new Map<string, number>(order.map(id => [id, 0]))
  for (const e of spine) {
    if (back.has(`${e.source}->${e.target}`)) continue
    const arr = succ.get(e.source)
    if (arr) arr.push(e.target)
    else succ.set(e.source, [e.target])
    indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1)
  }

  const layer = new Map<string, number>(order.map(id => [id, 0]))
  const queue = order.filter(id => (indeg.get(id) ?? 0) === 0)
  for (let qi = 0; qi < queue.length; qi++) {
    const u = queue[qi]
    for (const v of succ.get(u) ?? []) {
      layer.set(v, Math.max(layer.get(v) ?? 0, (layer.get(u) ?? 0) + 1))
      const d = (indeg.get(v) ?? 0) - 1
      indeg.set(v, d)
      if (d === 0) queue.push(v)
    }
  }

  // ---- 二次归属 ----
  // 注意：edges 里可能含**已被折叠**的节点（summary 事实默认不渲染），其 id 不在
  // `layer` 里。所有读写都必须先判存在，否则一条「折叠节点 → finding」的 proves
  // 边就会把整个布局打崩（真实数据里就有这种边）。
  const roleOfId = new Map(nodes.map(n => [n.id, roleOf(n)]))
  const has = (id: string) => layer.has(id)

  // hint：derived_from 存的是「意图 → 提示」（提示是意图的依据），故提示应落在
  // 该意图的**左一格**。一个提示可能被多条意图引用，取最左的那个意图。
  for (const e of edges) {
    if (e.data?.category !== "reverse") continue
    if (roleOfId.get(e.source) === "hint" || roleOfId.get(e.target) !== "hint") continue
    if (!has(e.source) || !has(e.target)) continue
    const want = Math.max(0, (layer.get(e.source) ?? 0) - 1)
    layer.set(e.target, Math.min(layer.get(e.target) ?? 0, want))
  }

  // finding：proves 存的是「事实 → 发现」，发现是产出的下一跳，落在右侧一格。
  for (const e of edges) {
    if (e.data?.category !== "system") continue
    if (roleOfId.get(e.target) !== "finding") continue
    if (roleOfId.get(e.source) === "goal") continue // 目标不是发现的来源
    if (!has(e.source) || !has(e.target)) continue
    const want = (layer.get(e.source) ?? 0) + 1
    layer.set(e.target, Math.max(layer.get(e.target) ?? 0, want))
  }

  // 注册目标：整图的收束点，永远压在最右一列（proves 边不参与分层，否则
  // 十几个产出节点都向后连它，它自己反而沉到中间）。
  const maxLayer = Math.max(0, ...[...layer.values()])
  for (const n of nodes) {
    if (roleOf(n) === "goal" && has(n.id)) layer.set(n.id, maxLayer + 1)
  }

  return layer
}

/**
 * 层内排序 + 坐标：重心排序（barycenter）后按层分列。
 *
 * **为什么不能只按创建时间排**（真实数据实测）：同层 8 个意图若按时间排，
 * 它们各自连到下一层的事实节点位置完全不相关，连线会大幅交叉成蛛网——
 * 截图里 42 条边几乎一半在斜穿。重心法把每个节点按「邻居在相邻层的平均序号」
 * 排位，四轮双向扫描后交叉数显著下降（上游 computeLayout 同法）。
 *
 * 排序只影响同列内的 y 顺序，不改变层号；刷新后位置稳定（时间序作最终 tiebreak）。
 */
function computePositions(nodes: ArtexExplorationNode[], edges: Edge[]): Map<string, Pos> {
  const layer = computeLayers(nodes, edges)
  const columns = new Map<number, ArtexExplorationNode[]>()
  for (const n of nodes) {
    const c = layer.get(n.id) ?? 0
    const arr = columns.get(c)
    if (arr) arr.push(n)
    else columns.set(c, [n])
  }
  const colKeys = [...columns.keys()].sort((a, b) => a - b)

  // 初排：层内按创建时间（早期方向在上）——也是最终 tiebreak，保证稳定。
  for (const c of colKeys) {
    columns.get(c)!.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)))
  }

  // 邻接：只取**相邻层**的边（跨层长边对重心无意义，反而拉偏）
  const colOf = new Map<string, number>()
  for (const c of colKeys) for (const n of columns.get(c)!) colOf.set(n.id, c)
  const nbrs = new Map<string, string[]>()
  for (const e of edges) {
    const cs = colOf.get(e.source)
    const cd = colOf.get(e.target)
    if (cs === undefined || cd === undefined || Math.abs(cs - cd) !== 1) continue
    const a = nbrs.get(e.source)
    if (a) a.push(e.target)
    else nbrs.set(e.source, [e.target])
    const b = nbrs.get(e.target)
    if (b) b.push(e.source)
    else nbrs.set(e.target, [e.source])
  }

  const sweep = (dir: 1 | -1) => {
    const start = dir === 1 ? 1 : colKeys.length - 2
    const end = dir === 1 ? colKeys.length : -1
    for (let i = start; i !== end; i += dir) {
      const c = colKeys[i]
      const ref = colKeys[i - dir]
      if (ref === undefined) continue
      const refIndex = new Map(columns.get(ref)!.map((n, idx) => [n.id, idx]))
      const scored = columns.get(c)!.map((n, idx) => {
        const idxs = (nbrs.get(n.id) ?? [])
          .map(id => refIndex.get(id))
          .filter((v): v is number => v !== undefined)
        const bary = idxs.length ? idxs.reduce((a, b) => a + b, 0) / idxs.length : idx
        return { n, bary, idx }
      })
      scored.sort((a, b) => a.bary - b.bary || a.idx - b.idx)
      columns.set(c, scored.map(s => s.n))
    }
  }
  for (let iter = 0; iter < 4; iter++) { sweep(1); sweep(-1) }

  const pos = new Map<string, Pos>()
  const maxRows = Math.max(1, ...[...columns.values()].map(v => v.length))
  const midY = ((maxRows - 1) * (CARD_H + ROW_GAP)) / 2
  for (const c of colKeys) {
    const items = columns.get(c)!
    const startY = midY - ((items.length - 1) * (CARD_H + ROW_GAP)) / 2
    items.forEach((n, i) => {
      pos.set(n.id, { x: COL_PAD + c * X_STEP, y: COL_PAD + startY + i * (CARD_H + ROW_GAP) })
    })
  }
  return pos
}

// ---------- 时间刻度 ----------

type TimeSpan = { min: number; max: number }

function timeSpan(nodes: ArtexExplorationNode[]): TimeSpan {
  const ts = nodes.map(n => Date.parse(n.created_at)).filter(v => Number.isFinite(v))
  if (ts.length === 0) return { min: 0, max: 0 }
  return { min: Math.min(...ts), max: Math.max(...ts) }
}

/** 归一化到 0..1：0=最早，1=最晚。跨度为零时一律返回 0。 */
function timeRatio(n: ArtexExplorationNode, span: TimeSpan): number {
  const t = Date.parse(n.created_at)
  if (!Number.isFinite(t) || span.max <= span.min) return 0
  return (t - span.min) / (span.max - span.min)
}

function fmtClock(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ""
  return d.toLocaleTimeString("zh-CN", { hour12: false })
}

// ---------- 节点卡 ----------

type NodeData = { node: ArtexExplorationNode; span: TimeSpan; selected: boolean }

function ExploreNode({ data }: NodeProps<Node<NodeData>>) {
  const { node, span, selected } = data
  const role = roleOf(node)
  const meta = META[role]
  const Icon = meta.icon
  const live = node.status === "running" || node.status === "claimed"
  const ratio = timeRatio(node, span)
  // 结论标题可能为空（老数据 title 只有 objective），退回 content 首行。
  const title = shortTitle(node.title || node.content || node.id)
  // 副标题：fact/summary 挂它来自哪条意图（objective 在 metadata 里）。
  const objective = String((node.metadata || {}).objective || "")
  const subtitle = node.kind === "intent" || node.kind === "goal"
    ? ""
    : objective || (node.content && node.content !== node.title ? node.content.split("\n")[0] : "")

  return (
    <div className={cn(
      "rounded-lg border border-l-[3px] border-(--border) bg-(--surface-1) px-2.5 py-2 transition-colors",
      meta.bar,
      selected && "ring-1 ring-cyan-400/60",
      live && "border-amber-500/40",
    )} style={{ width: CARD_W, height: CARD_H }}>
      <Handle type="target" position={Position.Left}
        className="!h-1.5 !w-1.5 !border-0 !bg-(--viz-handle)" />
      <div className="flex items-center gap-1.5">
        <span className={cn("grid h-4 w-4 shrink-0 place-items-center rounded", meta.iconBg)}>
          <Icon className="h-2.5 w-2.5 text-white" />
        </span>
        <span className="shrink-0 text-[10px] font-medium text-(--text-3)">{meta.label}</span>
        {live && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-400" />}
        <span className={cn("ml-auto shrink-0 rounded border px-1 py-0 font-mono text-[9px]",
          STATUS_TONE[node.status] || "border-(--border) text-(--text-3)")}>
          {statusLabel(role, node.status)}
        </span>
      </div>
      <div className="mt-0.5 line-clamp-2 text-[11px] font-medium leading-snug text-(--text-1)">{title}</div>
      {subtitle && <div className="line-clamp-1 text-[9px] leading-snug text-(--text-3)">{subtitle}</div>}
      <div className="mt-0.5 flex items-center gap-1.5">
        <span className="shrink-0 font-mono text-[9px] text-(--text-3)">{fmtClock(node.created_at)}</span>
        {/* 时间刻度条：整个任务的跨度做底，本节点的时刻做点。位置即先后。 */}
        <span className="relative h-0.5 flex-1 rounded-full bg-(--surface-2)">
          <span className="absolute top-1/2 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full"
            style={{ left: `${ratio * 100}%`, background: meta.hex }} />
        </span>
      </div>
      <Handle type="source" position={Position.Right}
        className="!h-1.5 !w-1.5 !border-0 !bg-(--viz-handle)" />
    </div>
  )
}

const nodeTypes = { explore: ExploreNode }

// ---------- 详情抽屉 ----------

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="flex items-start gap-3 py-1 text-[11px]">
    <span className="w-14 shrink-0 text-(--text-3)">{label}</span>
    <span className="min-w-0 flex-1 break-words text-(--text-2)">{children}</span>
  </div>
}

function DetailDrawer({ node, byId, onPick, onClose }: {
  node: ArtexExplorationNode | null
  byId: Map<string, ArtexExplorationNode>
  onPick: (n: ArtexExplorationNode) => void
  onClose: () => void
}) {
  if (!node) return null
  const role = roleOf(node)
  const meta = META[role]
  const Icon = meta.icon
  // 邻居跳转：抽屉里能顺着血缘走，不用回画布找。
  const neighbors = (node as { _neighbors?: string[] })._neighbors || []
  return <aside className="absolute right-0 top-0 z-10 flex h-full w-[min(380px,80%)] flex-col border-l border-(--border) bg-(--surface-1) shadow-xl">
    <header className="flex shrink-0 items-start gap-2 border-b border-(--border) px-3 py-2.5">
      <span className={cn("mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded", meta.iconBg)}>
        <Icon className="h-3.5 w-3.5 text-white" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium text-(--text-1)">{meta.label}</div>
        <div className="truncate font-mono text-[10px] text-(--text-3)">{node.id}</div>
      </div>
      <button className="shrink-0 rounded p-1 text-(--text-3) transition-colors hover:bg-(--surface-2) hover:text-(--text-1)"
        onClick={onClose} aria-label="关闭">
        <X className="h-3.5 w-3.5" />
      </button>
    </header>
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
      <DetailRow label="状态">
        <span className={cn("rounded border px-1 py-0 font-mono text-[10px]",
          STATUS_TONE[node.status] || "border-(--border) text-(--text-3)")}>
          {statusLabel(role, node.status)}
        </span>
      </DetailRow>
      <DetailRow label="置信度">{Math.round((node.confidence || 0) * 100)}%</DetailRow>
      <DetailRow label="时间">{new Date(node.created_at).toLocaleString("zh-CN")}</DetailRow>
      {node.asset_ids.length > 0 && <DetailRow label="资产">
        <span className="font-mono text-[10px]">{node.asset_ids.join(", ")}</span>
      </DetailRow>}
      {String((node.metadata || {}).objective || "") && <DetailRow label="意图">
        {String((node.metadata || {}).objective)}
      </DetailRow>}
      <section className="mt-2 border-t border-(--border) pt-2">
        <div className="mb-1 text-[10px] font-medium text-(--text-3)">内容</div>
        <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded border border-(--border) bg-(--surface-0) p-2 font-mono text-[10px] leading-relaxed text-(--text-2)">
          {node.content || "（无内容）"}
        </pre>
      </section>
      {neighbors.length > 0 && <section className="mt-2 border-t border-(--border) pt-2">
        <div className="mb-1 text-[10px] font-medium text-(--text-3)">关联节点</div>
        <div className="space-y-1">
          {neighbors.map(id => {
            const n = byId.get(id)
            if (!n) return null
            const m = META[roleOf(n)]
            const NIcon = m.icon
            return <button key={id} onClick={() => onPick(n)}
              className="flex w-full items-start gap-1.5 rounded border border-(--border) bg-(--surface-0) px-1.5 py-1 text-left transition-colors hover:border-(--text-3)">
              <span className={cn("mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded", m.iconBg)}>
                <NIcon className="h-2.5 w-2.5 text-white" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="line-clamp-1 text-[10px] text-(--text-2)">{(n.title || n.id).split("\n")[0]}</span>
                <span className="block font-mono text-[9px] text-(--text-3)">{m.label}</span>
              </span>
              <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-(--text-3)" />
            </button>
          })}
        </div>
      </section>}
      <section className="mt-2 border-t border-(--border) pt-2">
        <div className="mb-1 text-[10px] font-medium text-(--text-3)">原始数据</div>
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded border border-(--border) bg-(--surface-0) p-2 font-mono text-[9px] leading-relaxed text-(--text-3)">
          {JSON.stringify(node, null, 2)}
        </pre>
      </section>
    </div>
  </aside>
}

// ---------- 主体 ----------

type EdgeToggles = { spine: boolean; reverse: boolean; system: boolean }

function CanvasInner({ graph }: { graph: ArtexExplorationGraph }) {
  const [show, setShow] = useState<EdgeToggles>({ spine: true, reverse: true, system: false })
  const [showSummary, setShowSummary] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)

  const span = useMemo(() => timeSpan(graph.nodes), [graph.nodes])
  const byId = useMemo(() => new Map(graph.nodes.map(n => [n.id, n])), [graph.nodes])

  // 默认折叠「整轮汇总」事实：它与同意图的显式结论重复，展开只是噪声。
  // 但老任务可能只有这类节点——所以给开关，不能一刀切藏死。
  const visibleNodes = useMemo(
    () => graph.nodes.filter(n => showSummary || !isCollapsedByDefault(n)),
    [graph.nodes, showSummary])

  const visibleIds = useMemo(() => new Set(visibleNodes.map(n => n.id)), [visibleNodes])

  const allEdges = useMemo<Edge[]>(() => graph.edges.map((e, i) => {
    const cat = String((e as { category?: string }).category || "system")
    const st = EDGE_STYLE[cat] || EDGE_STYLE.system
    return {
      id: `e${i}-${e.from_node_id}-${e.to_node_id}`,
      source: e.from_node_id,
      target: e.to_node_id,
      // 反向边视觉方向统一：derived_from 存的是「新意图 → 依据」，画出来是回指，
      // 会与主脊交叉成环。翻转成「依据 → 新意图」后全图只有一个左→右流向。
      type: "smoothstep",
      animated: false,
      label: cat === "system" ? undefined : st.label,
      labelShowBg: false,
      labelStyle: { fontSize: 9, fill: st.color },
      style: {
        stroke: st.color,
        strokeWidth: st.width,
        strokeDasharray: st.dash,
        strokeOpacity: cat === "system" ? 0.45 : 0.85,
      },
      markerEnd: { type: MarkerType.ArrowClosed, color: st.color, width: 12, height: 12 },
      data: { category: cat },
    } as Edge
  }), [graph.edges])

  // derived_from 翻转源目标（见上）；两端都得在可见集里才画。
  const flowEdges = useMemo(() => allEdges.flatMap(e => {
    const cat = String(e.data?.category || "system")
    if (!show[cat as keyof EdgeToggles]) return []
    if (!visibleIds.has(e.source) || !visibleIds.has(e.target)) return []
    if (cat === "reverse") {
      return [{ ...e, id: `${e.id}-flip`, source: e.target, target: e.source }]
    }
    return [e]
  }), [allEdges, show, visibleIds])

  // 位置必须基于**全量边**算，不能用 flowEdges：后者随边档开关变化，关掉主脊
  // 时它变空，所有节点会被重新算成第 0 层、整图塌成一列。开关只影响「画不画」，
  // 不该影响「摆在哪」。
  const positions = useMemo(() => computePositions(visibleNodes, allEdges), [visibleNodes, allEdges])

  const flowNodes = useMemo<Node<NodeData>[]>(() => visibleNodes.map(n => ({
    id: n.id,
    type: "explore",
    position: positions.get(n.id) ?? { x: 0, y: 0 },
    data: { node: n, span, selected: selected === n.id },
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    draggable: false,
  })), [visibleNodes, positions, span, selected])

  // 邻居索引（抽屉用）：沿所有边（含隐藏档）走一跳。
  const neighborsOf = useCallback((id: string) => {
    const out = new Set<string>()
    for (const e of graph.edges) {
      if (e.from_node_id === id) out.add(e.to_node_id)
      else if (e.to_node_id === id) out.add(e.from_node_id)
    }
    return [...out]
  }, [graph.edges])

  const selectedNode = selected ? byId.get(selected) ?? null : null
  const decorated = useMemo(() => selectedNode
    ? { ...selectedNode, _neighbors: neighborsOf(selectedNode.id) } as ArtexExplorationNode
    : null, [selectedNode, neighborsOf])

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const e of allEdges) {
      const cat = String(e.data?.category || "system")
      c[cat] = (c[cat] || 0) + 1
    }
    return c
  }, [allEdges])

  if (visibleNodes.length === 0) {
    return <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
      <span className="grid h-10 w-10 place-items-center rounded-xl border border-cyan-500/30 bg-cyan-500/10 text-cyan-300">
        <Layers className="h-5 w-5" />
      </span>
      <p className="max-w-sm text-xs leading-relaxed text-(--text-3)">
        当前任务尚未产生探索节点。Planner 拆出目标、Engine 派出 Worker 之后，
        起点 → 意图 → 产出 会在这里从左到右长出来。
      </p>
    </div>
  }

  return <div ref={wrapRef} className="fc-dark relative h-full w-full overflow-hidden rounded-lg border border-(--border) bg-(--viz-canvas)">
    <ReactFlow
      nodes={flowNodes}
      edges={flowEdges}
      nodeTypes={nodeTypes}
      onNodeClick={(_, n) => setSelected(n.id)}
      onPaneClick={() => setSelected(null)}
      fitView
      // minZoom 0.85：**宁可滚动，也不把 28 个节点压成缩略图**。
      // 实测 0.55 时 11px 正文只剩 6px，人类读不了——而「读得清」正是这次改造
      // 的目的。fitView 会在 minZoom 处停下，超出的部分靠平移/滚轮查看。
      fitViewOptions={{ padding: 0.1, minZoom: 0.85, maxZoom: 1 }}
      proOptions={{ hideAttribution: true }}
      minZoom={0.35}
      maxZoom={1.6}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--viz-outline)" />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable
        maskColor="var(--viz-canvas-mask)"
        nodeColor={n => {
          const d = n.data as NodeData | undefined
          return d?.node ? META[roleOf(d.node)].hex : "#64748b"
        }}
        nodeStrokeWidth={0} nodeBorderRadius={3} />
      {/* 图例 + 边档开关：一个面板解决「这图怎么看」和「太乱了怎么收」。
          放在左下角——左上会被起点列压住（真实数据里 L0 有 6 个节点）。 */}
      <div className="pointer-events-auto absolute bottom-2 left-2 z-10 max-w-[290px] rounded-lg border border-(--border) bg-(--surface-1)/95 px-2 py-1.5 text-[10px] backdrop-blur">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {(Object.keys(META) as Role[]).map(r => {
            const m = META[r]
            const I = m.icon
            return <span key={r} className="inline-flex items-center gap-1 text-(--text-2)">
              <span className={cn("grid h-3.5 w-3.5 place-items-center rounded", m.iconBg)}>
                <I className="h-2 w-2 text-white" />
              </span>{m.label}
            </span>
          })}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1 border-t border-(--border) pt-1.5">
          {(Object.keys(EDGE_STYLE) as (keyof EdgeToggles)[]).map(cat => {
            const st = EDGE_STYLE[cat]
            const on = show[cat]
            return <button key={cat}
              className={cn("inline-flex items-center gap-1 rounded border px-1 py-0.5 transition-colors",
                on ? "border-(--text-3) bg-(--surface-2) text-(--text-2)" : "border-(--border) text-(--text-3) opacity-60")}
              onClick={() => setShow(s => ({ ...s, [cat]: !s[cat] }))}>
              <span className="h-0.5 w-3 rounded-full" style={{
                background: on ? st.color : "var(--viz-edge-muted)",
                // 虚线档用 background-image 画段，开关状态一眼可见
                ...(st.dash ? { backgroundImage: `repeating-linear-gradient(90deg, ${on ? st.color : "var(--viz-edge-muted)"} 0 3px, transparent 3px 5px)`, background: "none" } : {}),
              }} />
              {st.label} {counts[cat] || 0}
            </button>
          })}
          <label className="ml-auto inline-flex cursor-pointer items-center gap-1 text-(--text-3)">
            <input type="checkbox" className="h-2.5 w-2.5 accent-cyan-500"
              checked={showSummary} onChange={e => setShowSummary(e.target.checked)} />
            整轮汇总
          </label>
        </div>
      </div>
    </ReactFlow>
    <DetailDrawer node={decorated} byId={byId} onPick={n => setSelected(n.id)} onClose={() => setSelected(null)} />
  </div>
}

export function ExplorationCanvas({ graph }: { graph: ArtexExplorationGraph }) {
  // key 挂在 task 上：切任务时整图重建，避免旧位置/旧选中态残留。
  return <div className="h-full w-full">
    <ReactFlowProvider key={graph.task_id || "none"}>
      <CanvasInner graph={graph} />
    </ReactFlowProvider>
  </div>
}
