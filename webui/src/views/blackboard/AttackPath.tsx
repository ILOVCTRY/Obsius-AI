import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react"
import {
  ReactFlow, ReactFlowProvider, Background, Controls, Handle,
  MarkerType, Position,
  type Edge, type Node,
} from "@xyflow/react"
import "@xyflow/react/dist/style.css"
import "./canvas.css" // 必须在 xyflow css 之后：深色覆盖（plain CSS 压层叠层）
import { api } from "@/lib/api"
import type {
  Asset, AttackAttempt, AttackNode, AttackPath as AttackPathData,
  HttpHistoryRow,
} from "@/lib/types"
import { fmtDateTime } from "@/lib/datetime"

// 单站攻击链路图 v3（website-attack-path-graph，2026-09-24）：
// 主脊 目标 → 意图（规划产物·可证伪假设）→ 收尾：漏洞 | 有效发现 | 死路；
// 执行层（测试点尝试）读时按意图存活窗归属，点意图卡展开为时间序尝试条；
// 死路是意图的关闭态，默认隐藏（bypass 穿通边），开关在顶栏。
// 由 Blackboard 发现 tab 的「链路」子视图懒加载（@xyflow/react 不进主包）。

const X_STEP = 300
const CARD_W = 248
const Y_GAP = 20
const TARGET_H = 60
const INTENT_H = 100
const FINDING_H = 72

// 执行层
const ATTEMPT_W = 136
const ATTEMPT_X = 148
const ATTEMPT_H = 96

// 五档执行结果：色 / 中文标
const RESULT_STYLE: Record<string, { color: string; label: string }> = {
  found: { color: "#f85149", label: "出洞" },
  hint: { color: "#d29922", label: "有反应" },
  blocked: { color: "#8b949e", label: "被拦" },
  no_reaction: { color: "#484f58", label: "无反应" },
  skipped: { color: "#58a6ff", label: "跳过" },
}

const SEV_COLOR: Record<string, string> = {
  critical: "#f85149", high: "#ff7b72", medium: "#d29922",
  low: "#58a6ff", info: "#8b949e",
}
const SEV_LABEL: Record<string, string> = {
  critical: "严重", high: "高危", medium: "中危", low: "低危", info: "提示",
}

const OUTCOME_BADGE: Record<string, { color: string; label: string }> = {
  vuln: { color: "#f85149", label: "漏洞" },
  finding: { color: "#3fb950", label: "有效发现" },
  dead_end: { color: "#8b949e", label: "死路" },
}

// ---------- 主脊节点 ----------

function TargetNode({ data }: { data: Record<string, unknown> }) {
  const n = data.node as AttackNode
  return (
    <div
      className="rounded-md border border-[#1f6feb]/60 bg-[#0f1c30] px-3 py-2"
      style={{ width: CARD_W, height: TARGET_H }}
    >
      <div className="flex items-center gap-1.5">
        <span className="text-xs">🎯</span>
        <span className="text-[10px] font-medium text-[#58a6ff]">目标</span>
        <span className="ml-auto rounded border border-[#30363d] px-1 font-mono text-[10px] text-muted-foreground">
          {n.asset_type}
        </span>
      </div>
      <p className="mt-1 truncate font-mono text-[12px]">{n.label}</p>
      <Handle type="source" position={Position.Right} style={{ background: "#58a6ff" }} />
    </div>
  )
}

function IntentNode({ data }: { data: Record<string, unknown> }) {
  const n = data.node as AttackNode
  const expanded = data.expanded as boolean
  const onToggle = data.onToggle as (id: string) => void
  const isDeadEnd = n.outcome === "dead_end"
  const badge = n.outcome ? OUTCOME_BADGE[n.outcome] : null
  return (
    <button
      type="button"
      onClick={() => onToggle(n.id)}
      className="rounded-md border bg-[#161b22] px-3 py-2 text-left transition-colors hover:bg-[#1c2128]"
      style={{
        width: CARD_W, height: INTENT_H,
        borderColor: isDeadEnd ? "#6e7681" : n.status === "open" ? "#1f6feb55" : "#30363d",
        borderStyle: isDeadEnd ? "dashed" : "solid",
      }}
    >
      <div className="flex items-center gap-1.5">
        <span
          className={`size-1.5 rounded-full ${n.status === "open" ? "animate-pulse" : ""}`}
          style={{ background: n.status === "open" ? "#58a6ff" : "#8b949e" }}
        />
        <span className="text-[10px] text-muted-foreground">
          {n.status === "open" ? "待收尾" : "已收尾"}
        </span>
        <span className="ml-auto text-[10px] text-muted-foreground">
          {n.request_count ? `${n.request_count} 请求 · ` : ""}{expanded ? "收起 ▾" : "执行 ▸"}
        </span>
      </div>
      <p className="mt-1.5 line-clamp-3 text-[11.5px] leading-snug">{n.statement}</p>
      <div className="absolute inset-x-3 bottom-1.5 flex items-center gap-1">
        {badge
          ? <span className="rounded px-1 text-[10px]"
                   style={{ background: `${badge.color}22`, color: badge.color }}>
              {badge.label}×{n.finding_ids?.length || 0}
            </span>
          : <span className="text-[10px] text-muted-foreground">假设待验证</span>}
        {n.created_at && (
          <span className="ml-auto font-mono text-[10px] text-muted-foreground/70">
            {fmtDateTime(n.created_at).slice(5, 16)}
          </span>
        )}
      </div>
      <Handle type="target" position={Position.Left} style={{ background: "#6e7681" }} />
      <Handle type="source" position={Position.Right} style={{ background: "#6e7681" }} />
    </button>
  )
}

function FindingNode({ data }: { data: Record<string, unknown> }) {
  const n = data.node as AttackNode
  const selected = data.selected as boolean
  const onSelect = data.onSelect as (n: AttackNode) => void
  const sev = SEV_COLOR[n.severity || "info"]
  const isVuln = n.category === "vuln"
  return (
    <button
      type="button"
      onClick={() => onSelect(n)}
      className="rounded-md border bg-[#161b22] px-3 py-2 text-left transition-colors hover:bg-[#1c2128]"
      style={{
        width: CARD_W, height: FINDING_H,
        borderColor: selected ? sev : "#30363d",
        boxShadow: selected ? `0 0 0 1px ${sev}` : undefined,
      }}
    >
      <div className="flex items-center gap-1">
        <span className="rounded px-1 text-[10px]"
              style={{
                background: isVuln ? "#f8514922" : "#3fb95022",
                color: isVuln ? "#f85149" : "#3fb950",
              }}>
          {isVuln ? "漏洞" : "有效发现"}
        </span>
        {n.status === "verified" && (
          <span className="rounded bg-[#21262d] px-1 text-[10px] text-emerald-400">已验证</span>
        )}
        <span className="ml-auto text-[10px]" style={{ color: sev }}>
          {SEV_LABEL[n.severity || "info"]}
        </span>
      </div>
      <p className="mt-1.5 line-clamp-2 text-[11.5px] leading-snug">{n.title}</p>
      <Handle type="target" position={Position.Left} style={{ background: "#6e7681" }} />
    </button>
  )
}

// ---------- 执行层节点 ----------

function AttemptNode({ data }: { data: Record<string, unknown> }) {
  const a = data.attempt as AttackAttempt
  const selected = data.selected as boolean
  const onOpen = data.onOpen as (a: AttackAttempt) => void
  const st = RESULT_STYLE[a.result]
  return (
    <button
      type="button"
      onClick={() => onOpen(a)}
      className="rounded border bg-[#161b22] px-2 py-1.5 text-left transition-colors hover:bg-[#1c2128]"
      style={{
        width: ATTEMPT_W, height: ATTEMPT_H,
        borderColor: selected ? st.color : "#30363d",
      }}
    >
      <div className="flex items-center gap-1">
        <span className="size-1.5 shrink-0 rounded-full" style={{ background: st.color }} />
        <span className="text-[9px]" style={{ color: st.color }}>{st.label}</span>
        <span className="text-[9px] text-muted-foreground">{a.method}</span>
        {a.request_count > 1 && (
          <span className="ml-auto text-[9px] text-muted-foreground">×{a.request_count}</span>
        )}
      </div>
      <p className="mt-1 line-clamp-2 break-all font-mono text-[9.5px] leading-tight">
        {a.path_template}
      </p>
      <div className="absolute inset-x-1.5 bottom-1 flex flex-wrap gap-0.5">
        {a.status_codes.slice(0, 3).map((s) => (
          <span key={s} className="rounded bg-[#21262d] px-0.5 font-mono text-[9px] text-muted-foreground">{s}</span>
        ))}
      </div>
    </button>
  )
}

const nodeTypes = {
  target: TargetNode, intent: IntentNode, finding: FindingNode, attempt: AttemptNode,
}

// ---------- 目标选择引导 ----------

function TargetGuide({ assets, onPick }: {
  assets: Asset[]; onPick: (id: string) => void
}) {
  const [q, setQ] = useState("")
  const roots = useMemo(() => {
    const kw = q.trim().toLowerCase()
    return assets
      .filter((a) => a.type === "host" || a.type === "domain")
      .filter((a) => !kw || a.value.toLowerCase().includes(kw))
      .sort((a, b) => a.value.localeCompare(b.value))
  }, [assets, q])
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="w-96 rounded-lg border border-[#30363d] bg-[#161b22] p-4">
        <p className="text-sm font-medium">选择要查看攻击链路的目标</p>
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          主脊：目标 → 意图（规划）→ 收尾（漏洞/发现/死路）；跨任务、跨会话合并。
        </p>
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="搜索 IP/域名…"
          className="mt-3 h-7 w-full rounded border border-[#30363d] bg-[#0d1117] px-2 text-[11px] outline-none focus:border-primary/50"
        />
        <div className="mt-2 max-h-72 space-y-0.5 overflow-auto">
          {roots.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => onPick(a.id)}
              className="flex w-full items-center gap-2 rounded px-2 py-1 text-left hover:bg-accent/40"
            >
              <span className="rounded border border-[#30363d] px-1 font-mono text-[10px] text-muted-foreground">
                {a.type}
              </span>
              <span className="truncate font-mono text-xs">{a.value}</span>
            </button>
          ))}
          {roots.length === 0 && (
            <p className="py-3 text-center text-[11px] text-muted-foreground">无匹配资产</p>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------- 节点/尝试明细侧栏 ----------

function DetailPane({ pid, attempt, node, onClose, onReopen }: {
  pid: string
  attempt: AttackAttempt | null
  node: AttackNode | null
  onClose: () => void
  onReopen: () => void
}) {
  const [row, setRow] = useState<HttpHistoryRow | null>(null)
  const [err, setErr] = useState("")
  const [busy, setBusy] = useState(false)
  const historyId = attempt?.representative.history_id ?? null

  useEffect(() => {
    setRow(null); setErr("")
    if (historyId == null) return
    api.browserHistoryRow(pid, historyId)
      .then(setRow)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [pid, historyId])

  const title = attempt ? attempt.key : node?.title || node?.statement || ""

  const reopen = async () => {
    if (!node) return
    setBusy(true)
    try {
      await api.reopenIntent(pid, node.id, "人类侧栏重开")
      onReopen()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="absolute inset-y-0 right-0 z-20 w-80 overflow-auto border-l border-[#30363d] bg-[#161b22]">
      <div className="sticky top-0 flex items-center gap-2 border-b border-[#21262d] bg-[#161b22] px-3 py-2">
        <span className="size-2 rounded-full" style={{
          background: attempt
            ? RESULT_STYLE[attempt.result].color
            : node?.type === "target" ? "#58a6ff" : SEV_COLOR[node?.severity || "info"],
        }} />
        <span className="min-w-0 flex-1 truncate text-xs font-medium">{title}</span>
        <button type="button" className="text-[11px] text-muted-foreground hover:text-foreground"
                onClick={onClose}>✕</button>
      </div>

      <div className="space-y-3 p-3 text-[11px]">
        {node?.type === "intent" && (
          <>
            <Section title="意图陈述"><p>{node.statement}</p></Section>
            <Section title="收尾状态">
              {node.status === "open"
                ? <p className="text-[#58a6ff]">待收尾（漏洞 / 有效发现 / 死路 三选一）</p>
                : <p>
                    <span style={{ color: OUTCOME_BADGE[node.outcome || ""]?.color }}>
                      {OUTCOME_BADGE[node.outcome || ""]?.label || "—"}
                    </span>
                    {node.closed_at && <span className="text-muted-foreground">
                      （{fmtDateTime(node.closed_at)}）
                    </span>}
                  </p>}
            </Section>
            {node.outcome === "dead_end" && (
              <Section title="死因"><p>{node.dead_reason}</p></Section>
            )}
            {node.finding_ids && node.finding_ids.length > 0 && (
              <Section title="收尾发现">
                <p className="break-all font-mono text-[10px]">{node.finding_ids.join(", ")}</p>
              </Section>
            )}
            {node.status === "closed" && (
              <button
                type="button"
                disabled={busy}
                onClick={reopen}
                className="w-full rounded border border-[#30363d] px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50"
              >
                🔓 重开意图（新证据/收尾被否决）
              </button>
            )}
          </>
        )}

        {node?.type === "finding" && (
          <>
            <Section title="类别">
              <p style={{ color: node.category === "vuln" ? "#f85149" : "#3fb950" }}>
                {node.category === "vuln" ? "漏洞" : "有效发现（intel）"}
              </p>
            </Section>
            <Field k="严重度" v={SEV_LABEL[node.severity || "info"]} />
            <Field k="状态" v={node.status === "verified" ? "已验证" : "未验证"} />
            <p className="text-muted-foreground">完整字段（复现步骤/修复建议）见发现列表详情。</p>
          </>
        )}

        {attempt && (
          <>
            {attempt.representative.title && (
              <div>
                <p className="text-muted-foreground">结论</p>
                <p>{attempt.representative.title}</p>
                {attempt.representative.note && (
                  <p className="mt-0.5 text-muted-foreground">{attempt.representative.note}</p>
                )}
              </div>
            )}
            {attempt.representative.url && (
              <Section title="代表请求">
                <Field k="状态" v={attempt.representative.status == null
                  ? "无响应" : String(attempt.representative.status)} />
                <Field k="类型" v={attempt.representative.resp_mime || ""} />
                <p className="break-all font-mono">{attempt.representative.url}</p>
                {attempt.representative.snippet && (
                  <pre className="mt-1 max-h-32 overflow-auto rounded bg-[#0d1117] p-1.5 font-mono text-[10px]">{attempt.representative.snippet}</pre>
                )}
              </Section>
            )}
            {historyId != null && (
              err
                ? <p className="text-(--status-error)">完整请求加载失败：{err}</p>
                : !row
                  ? <p className="text-muted-foreground">加载完整原始请求…</p>
                  : <RawSections row={row} />
            )}
            {attempt.session_ids.length > 0 && (
              <Section title="来源会话">
                <p className="break-all font-mono text-[10px]">{attempt.session_ids.join(", ")}</p>
              </Section>
            )}
          </>
        )}

        {err && !(attempt) && <p className="text-(--status-error)">{err}</p>}
      </div>
    </div>
  )
}

function RawSections({ row }: { row: HttpHistoryRow }) {
  return (
    <>
      <Section title="请求头">
        <Headers m={row.req_headers} />
        {row.req_body && <pre className="mt-1 max-h-32 overflow-auto rounded bg-[#0d1117] p-1.5 font-mono text-[10px]">{row.req_body}</pre>}
      </Section>
      <Section title="响应头">
        <Headers m={row.resp_headers} />
        {row.resp_body && <pre className="mt-1 max-h-40 overflow-auto rounded bg-[#0d1117] p-1.5 font-mono text-[10px]">{row.resp_body}</pre>}
      </Section>
    </>
  )
}

function Headers({ m }: { m: Record<string, string> }) {
  const keys = Object.keys(m)
  if (!keys.length) return <p className="text-muted-foreground">（空）</p>
  return (
    <div className="space-y-0.5 font-mono text-[10px]">
      {keys.map((k) => (
        <p key={k} className="break-all"><span className="text-muted-foreground">{k}:</span> {m[k]}</p>
      ))}
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <p className="mb-1 text-muted-foreground">{title}</p>
      {children}
    </div>
  )
}

function Field({ k, v }: { k: string; v: string }) {
  return <p><span className="text-muted-foreground">{k}：</span>{v}</p>
}

// ---------- 布局：分层 DAG + 执行层展开 ----------

interface Cluster {
  intentId: string | null
  label: string
  x: number
  attempts: AttackAttempt[]
}

function layoutGraph(
  data: AttackPathData,
  showDeadEnd: boolean,
  expanded: Set<string>,
): { rfNodes: Node[]; clusters: Cluster[]; bandBottom: number } {
  const visible = data.nodes.filter((n) => showDeadEnd || !n.default_hidden)
  const visIds = new Set(visible.map((n) => n.id))

  // 层级：derive/outcome；死路隐藏时 bypass 参与保持连通
  const pred: Record<string, string[]> = {}
  visIds.forEach((id) => { pred[id] = [] })
  for (const e of data.edges) {
    if (e.kind === "exec") continue
    if (visIds.has(e.source) && visIds.has(e.target)) {
      pred[e.target].push(e.source)
    }
  }
  const level: Record<string, number> = {}
  const calc = (id: string, stack: Set<string>): number => {
    if (level[id] != null) return level[id]
    if (stack.has(id)) return 0 // 防御（服务端另有 Kahn 断言）
    stack.add(id)
    const ps = pred[id]
    level[id] = ps.length
      ? Math.max(...ps.map((p) => calc(p, stack))) + 1
      : (id === data.target.id ? 0 : 1)
    stack.delete(id)
    return level[id]
  }
  visible.forEach((n) => calc(n.id, new Set()))

  // 列内堆叠（保持 nodes 原始序：target → intents → findings）
  const rfNodes: Node[] = []
  const cols = new Map<number, AttackNode[]>()
  for (const n of visible) {
    const l = level[n.id]
    if (!cols.has(l)) cols.set(l, [])
    cols.get(l)!.push(n)
  }
  let maxBottom = 0
  for (const l of [...cols.keys()].sort((a, b) => a - b)) {
    let y = 40
    for (const n of cols.get(l)!) {
      const h = n.type === "target" ? TARGET_H
        : n.type === "intent" ? INTENT_H : FINDING_H
      rfNodes.push({
        id: n.id, type: n.type, position: { x: l * X_STEP + 20, y },
        data: { node: n }, draggable: false,
      })
      y += h + Y_GAP
    }
    maxBottom = Math.max(maxBottom, y)
  }

  // 执行层：展开意图的尝试条 + 无主尝试条
  const clusters: Cluster[] = []
  let cx = 20
  const bandY = maxBottom + 56
  const intentX = (id: string) => {
    const l = level[id]
    return l == null ? 0 : l * X_STEP
  }
  const intentById = new Map(data.nodes.filter((n) => n.type === "intent").map((n) => [n.id, n]))
  const expandedIds = [...expanded]
    .filter((id) => intentById.has(id) && data.attempts.some((a) => a.intent_id === id))
    .sort((a, b) => intentX(a) - intentX(b))
  for (const iid of expandedIds) {
    const arr = data.attempts.filter((a) => a.intent_id === iid)
    clusters.push({
      intentId: iid, label: intentById.get(iid)!.statement || iid, x: cx, attempts: arr,
    })
    cx += Math.max(arr.length * ATTEMPT_X + 40, 220)
  }
  const unowned = data.attempts.filter((a) => a.intent_id == null)
  if (unowned.length) {
    clusters.push({ intentId: null, label: "未归属尝试（意图模型建立前的记录）", x: cx, attempts: unowned })
  }

  for (const c of clusters) {
    c.attempts.forEach((a, i) => {
      rfNodes.push({
        id: a.id, type: "attempt",
        position: { x: c.x + 36 + i * ATTEMPT_X, y: bandY + 40 },
        data: { attempt: a }, draggable: false,
        selectable: true,
      })
    })
  }

  return {
    rfNodes, clusters,
    bandBottom: clusters.length ? bandY + 40 + ATTEMPT_H : maxBottom,
  }
}

// ---------- 主画布 ----------

function Canvas({ pid, assets, initialTarget }: {
  pid: string; assets: Asset[]; initialTarget?: string
}) {
  const [target, setTarget] = useState(initialTarget ?? "")
  // 列表过滤态贯通（父级资产过滤选中后切链路直接带上）
  useEffect(() => {
    if (initialTarget) setTarget(initialTarget)
  }, [initialTarget])

  const [data, setData] = useState<AttackPathData | null>(null)
  const [selAttempt, setSelAttempt] = useState<AttackAttempt | null>(null)
  const [selNode, setSelNode] = useState<AttackNode | null>(null)
  const [showDeadEnd, setShowDeadEnd] = useState(false) // 纪律④：死路默认隐藏
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [loadErr, setLoadErr] = useState("")

  useEffect(() => {
    if (!target) { setData(null); return }
    let alive = true
    const load = () => api.attackPath(pid, target)
      .then((d) => { if (alive) { setData(d); setLoadErr("") } })
      .catch((e) => { if (alive) setLoadErr(e instanceof Error ? e.message : String(e)) })
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, target])

  const toggleIntent = (id: string) => {
    setExpanded((prev) => {
      const nextSet = new Set(prev)
      if (nextSet.has(id)) nextSet.delete(id); else nextSet.add(id)
      return nextSet
    })
  }

  const { rfNodes: laidNodes, clusters } = useMemo(
    () => data
      ? layoutGraph(data, showDeadEnd, expanded)
      : { rfNodes: [], clusters: [], bandBottom: 0 },
    [data, showDeadEnd, expanded])

  // 回调/选中态注入节点 data
  const nodeById = useMemo(() => {
    const m = new Map<string, AttackNode>()
    data?.nodes.forEach((n) => m.set(n.id, n))
    return m
  }, [data])
  const renderedAttempts = useMemo(() => {
    const m = new Set<string>()
    clusters.forEach((c) => c.attempts.forEach((a) => m.add(a.id)))
    return m
  }, [clusters])

  const rfNodes: Node[] = useMemo(() => laidNodes.map((n) => {
    if (n.type === "intent") {
      n.data = {
        ...n.data, expanded: expanded.has(n.id), onToggle: toggleIntent,
      }
    } else if (n.type === "finding") {
      n.data = {
        ...n.data, selected: selNode?.id === n.id, onSelect: setSelNode,
      }
    } else if (n.type === "attempt") {
      n.data = {
        ...n.data, selected: selAttempt?.id === n.id, onOpen: setSelAttempt,
      }
    }
    return n
  }), [laidNodes, expanded, selNode, selAttempt])

  const rfEdges: Edge[] = useMemo(() => {
    if (!data) return []
    const out: Edge[] = []
    const mk = (id: string, source: string, target2: string, style: CSSProperties) => ({
      id, source, target: target2, type: "smoothstep" as const,
      style,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: (style.stroke as string) || "#6e7681", width: 14, height: 14,
      },
    })
    // 主脊边
    for (const e of data.edges) {
      if (e.kind === "exec") continue
      const tgtNode = nodeById.get(e.target)
      if (e.kind === "derive") {
        out.push(mk(`${e.source}->${e.target}:${e.kind}`, e.source, e.target,
          { stroke: "#58a6ff", strokeWidth: 1.3, opacity: 0.7 }))
      } else if (e.kind === "outcome") {
        const color = tgtNode?.category === "vuln" ? "#f85149" : "#3fb950"
        out.push(mk(`${e.source}->${e.target}:${e.kind}`, e.source, e.target,
          { stroke: color, strokeWidth: 1.6, opacity: 0.85 }))
      } else if (e.kind === "bypass") {
        out.push(mk(`${e.source}->${e.target}:${e.kind}`, e.source, e.target,
          { stroke: "#8b949e", strokeWidth: 1.1, opacity: 0.5, strokeDasharray: "5 4" }))
      }
    }
    // 执行层边
    for (const e of data.exec_edges) {
      if (renderedAttempts.has(e.source) && renderedAttempts.has(e.target)) {
        out.push(mk(`${e.source}->${e.target}:exec`, e.source, e.target,
          { stroke: "#484f58", strokeWidth: 1, opacity: 0.8 }))
      }
    }
    for (const c of clusters) {
      if (c.intentId && c.attempts[0]) {
        out.push(mk(`${c.intentId}->${c.attempts[0].id}:conn`, c.intentId, c.attempts[0].id,
          { stroke: "#484f58", strokeWidth: 1, opacity: 0.55, strokeDasharray: "3 3" }))
      }
    }
    return out
  }, [data, nodeById, renderedAttempts, clusters])

  if (!target || (!assets.length && !data)) {
    if (!assets.length) {
      return <p className="p-6 text-center text-xs text-muted-foreground">资产加载中…</p>
    }
    return <TargetGuide assets={assets} onPick={setTarget} />
  }

  const empty = data != null
    && data.counts.intents === 0 && data.attempts.length === 0

  return (
    <div className="fc-dark absolute inset-0 bg-[#0d1117]">
      {/* 目标条 */}
      <div className="absolute inset-x-0 top-0 z-20 flex items-center gap-2 border-b border-[#21262d] bg-[#0d1117]/90 px-3 py-1.5">
        <button
          type="button"
          className="rounded border border-[#30363d] px-1.5 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
          onClick={() => { setTarget(""); setSelAttempt(null); setSelNode(null) }}
          title="重新选择目标"
        >
          换目标
        </button>
        <span className="truncate font-mono text-[11px]">
          {data?.target.value ?? loadErr ?? "…"}
        </span>
        {data && (
          <span className="shrink-0 text-[10px] text-muted-foreground">
            意图 {data.counts.intents}（待收尾 {data.counts.open}）· 发现 {data.counts.findings} ·
            {" "}{data.counts.total_requests} 请求
          </span>
        )}
        {data && data.counts.dead_end > 0 && (
          <label className="flex shrink-0 cursor-pointer items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground">
            <input
              type="checkbox"
              checked={showDeadEnd}
              onChange={(e) => setShowDeadEnd(e.target.checked)}
              className="size-3"
            />
            死路×{data.counts.dead_end}
          </label>
        )}
        <span className="ml-auto hidden shrink-0 text-[10px] text-muted-foreground md:inline">
          点意图卡展开执行层
        </span>
        {loadErr && <span className="shrink-0 text-[10px] text-(--status-error)">{loadErr}</span>}
      </div>

      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        onlyRenderVisibleElements
        fitView
        fitViewOptions={{ padding: 0.12, maxZoom: 1.1 }}
        minZoom={0.2}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#30363d" gap={20} size={1} />
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>

      {empty && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-center text-xs text-muted-foreground">
            该站暂无意图与执行记录<br />
            <span className="text-[10px]">Agent 声明意图（declare_intent）并探测后自动出现</span>
          </p>
        </div>
      )}

      {clusters.some((c) => c.intentId == null) && (
        <p className="pointer-events-none absolute bottom-2 left-1/2 z-10 -translate-x-1/2 text-[10px] text-muted-foreground/70">
          执行层：测试点尝试按意图存活时间窗读时归属
        </p>
      )}

      {(selAttempt || selNode) && (
        <DetailPane
          pid={pid}
          attempt={selAttempt}
          node={selNode}
          onClose={() => { setSelAttempt(null); setSelNode(null) }}
          onReopen={() => { setSelNode(null) }}
        />
      )}
    </div>
  )
}

export function AttackPath(props: {
  pid: string; assets: Asset[]; target?: string
}) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  )
}
