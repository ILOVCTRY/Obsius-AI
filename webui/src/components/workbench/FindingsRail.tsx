import {
  lazy, memo, Suspense,
  useCallback, useEffect, useMemo, useRef, useState,
  type MouseEvent as ReactMouseEvent,
} from "react"
import { ChevronLeft, ChevronRight, GitBranch, ShieldAlert } from "lucide-react"
import { api } from "@/lib/api"
import type { Asset, Finding } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"
import { paneMaxWidth } from "@/lib/paneWidth"
import { SEVERITY_COLOR } from "@/lib/events"
import { FindingDetailDialog } from "@/components/blackboard/FindingDetailDialog"
import { useEvents } from "@/lib/useEvents"

// 智能体工作台·右侧发现栏（2026-10-01）：默认收起；展开后为「漏洞/发现」精简列表
// （点击项弹详情，复用 FindingDetailDialog）+「链路视图」原地内联切换（仅渗透/红队轨）。
// 收起态为右缘竖排把手：展开箭头 + 「发现 N」（N=未验证数）+ 高危红点。
// 数据：api.findings 4s 轮询（与黑板同）+ finding.* 事件即时刷新；宽度 ≥280 可拖拽（去固定上限）。
// 内联链路（2026-10-01）：点「链路视图」右栏主体原地切为懒加载 AttackPath（@xyflow/react 不进主包），
// 顶部换「← 返回列表」；画布 self 拉 api.assets，目标按项目记忆（localStorage ui.wb-chain-target-<pid>），
// 切图时自动加宽到 CHAIN_W（不超过「容器 − 主区保底」），返回列表还原原宽度。

const AttackPathLazy = lazy(() =>
  import("@/views/blackboard/AttackPath").then((m) => ({ default: m.AttackPath })))

const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
const RAIL_MIN = 280
const RAIL_DEFAULT = 360
const CHAIN_W = 720                       // 内联链路切图时的舒适宽度
const RAIL_RESERVE = [".wb-aside", ".wb-splitter"]  // 右栏最大宽需扣掉左栏 + 左分割线
const RAIL_RESERVE_EXTRA = 16             // 右分割线 + 边框余量

type CatView = "vuln" | "intel"
type StatusView = "all" | "unverified" | "verified"

// finding.* 事件即时刷新（比 4s 轮询更早反映新增/编辑/删除）。**订阅隔离**（2026-10-04）：
// 订阅挪进这个返回 null 的小组件——否则本栏随工作台流式期的高频事件整栏重渲。
function FindingsWatcher({ pid, onFinding }: { pid: string; onFinding: () => void }) {
  const { events } = useEvents(pid)
  const lastId = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--)
      if (events[i].kind.startsWith("finding.")) return events[i].id
    return 0
  }, [events])
  useEffect(() => { if (lastId) onFinding() }, [lastId, onFinding])
  return null
}

export const FindingsRail = memo(function FindingsRail({ pid, track, embedded = false, visible = false, widthOverride }: {
  pid: string
  track?: string
  embedded?: boolean
  visible?: boolean
  widthOverride?: number
}) {
  const [open, setOpen] = useState(
    () => window.localStorage.getItem("ui.wb-findings-open") === "1")
  const isOpen = embedded ? visible : open
  const [width, setWidth] = useState(() => {
    const w = Number(window.localStorage.getItem("ui.wb-findings-w"))
    return Number.isFinite(w) && w >= RAIL_MIN ? w : RAIL_DEFAULT
  })
  const [dragging, setDragging] = useState(false)
  const [chain, setChain] = useState(false)   // 右栏内联模式：false=发现列表 / true=链路画布
  const [assets, setAssets] = useState<Asset[]>([])
  const [chainTarget, setChainTarget] = useState(
    () => window.localStorage.getItem(`ui.wb-chain-target-${pid}`) ?? "")
  const splitterRef = useRef<HTMLDivElement>(null)
  const listWidthRef = useRef(RAIL_DEFAULT)   // 进链路前的列表宽度（返回时还原）
  const dragOrigin = useRef<{ x: number; w: number } | null>(null)
  const [items, setItems] = useState<Finding[]>([])
  const [cat, setCat] = useState<CatView>("vuln")
  const [status, setStatus] = useState<StatusView>("all")
  const [detail, setDetail] = useState<Finding | null>(null)

  const isSplit = track === "pentest" || track === "redteam"
  const canChain = isSplit   // 链路画布仅渗透/红队轨（与黑板 showCanvas 判据一致）

  // 右栏可拖到的最大宽（动态：容器 − 主区保底 − 左栏 − 分割线）
  const maxNow = useCallback(
    () => paneMaxWidth(splitterRef.current, RAIL_RESERVE, RAIL_RESERVE_EXTRA), [])

  const refresh = useCallback(() => {
    api.findings(pid).then(setItems).catch(() => {})
  }, [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  const filtered = useMemo(() => {
    let list = items
    if (isSplit) list = list.filter((f) => (f.category ?? "vuln") === cat)
    if (status === "unverified") list = list.filter((f) => f.status === "unverified")
    else if (status === "verified") list = list.filter((f) => f.status === "verified")
    const rank = (s: string) => {
      const i = SEVERITY_ORDER.indexOf(s)
      return i === -1 ? SEVERITY_ORDER.length : i
    }
    return [...list].sort((a, b) =>
      rank(a.severity) - rank(b.severity) || b.created_at.localeCompare(a.created_at))
  }, [items, cat, status, isSplit])

  const unverified = useMemo(() => items.filter((f) => f.status === "unverified"), [items])
  const hasHighUnverified = unverified.some(
    (f) => f.severity === "critical" || f.severity === "high")

  const toggle = useCallback(() => {
    setOpen((v) => {
      const n = !v
      window.localStorage.setItem("ui.wb-findings-open", n ? "1" : "0")
      return n
    })
  }, [])

  // 拖左缘调宽（向左拖 = 变宽）；保下限、上限取动态值；松手落盘
  const onSplitterDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    dragOrigin.current = { x: e.clientX, w: width }
    setDragging(true)
  }
  useEffect(() => {
    if (!dragging) return
    const move = (e: MouseEvent) => {
      const o = dragOrigin.current
      if (o) setWidth(Math.max(RAIL_MIN, Math.min(maxNow(), o.w + (o.x - e.clientX))))
    }
    const up = () => setDragging(false)
    document.addEventListener("mousemove", move)
    document.addEventListener("mouseup", up)
    document.body.classList.add("wb-dragging")
    return () => {
      document.removeEventListener("mousemove", move)
      document.removeEventListener("mouseup", up)
      document.body.classList.remove("wb-dragging")
      dragOrigin.current = null
    }
  }, [dragging, maxNow])
  // 列表宽度落盘（内联链路态不写回，避免把加宽的画布宽度记成列表宽度）
  useEffect(() => {
    if (!dragging && !chain) window.localStorage.setItem("ui.wb-findings-w", String(width))
  }, [dragging, chain, width])
  // 窗口尺寸变化时把宽度夹回动态上限（改小窗口后不越界）
  useEffect(() => {
    if (!open) return
    const clamp = () => setWidth((w) => Math.max(RAIL_MIN, Math.min(w, maxNow())))
    clamp()
    window.addEventListener("resize", clamp)
    return () => window.removeEventListener("resize", clamp)
  }, [open, chain, maxNow])

  // 进入链路才拉资产（供画布目标选择/取值）
  useEffect(() => {
    if (!chain) return
    api.assets(pid).then(setAssets).catch(() => {})
  }, [chain, pid])

  // 切到内联链路：记下列表宽度，自动加宽到舒适宽度（不超上限）
  const enterChain = useCallback(() => {
    listWidthRef.current = width
    setChain(true)
    setWidth(Math.max(RAIL_MIN, Math.min(CHAIN_W, maxNow())))
  }, [width, maxNow])
  // 返回列表：还原原列表宽度
  const exitChain = useCallback(() => {
    setChain(false)
    setWidth(Math.max(RAIL_MIN, Math.min(listWidthRef.current, maxNow())))
  }, [maxNow])
  // 画布目标变更 → 按项目记忆（清空则移除记录）
  const onTargetChange = useCallback((t: string) => {
    setChainTarget(t)
    const key = `ui.wb-chain-target-${pid}`
    if (t) window.localStorage.setItem(key, t)
    else window.localStorage.removeItem(key)
  }, [pid])

  if (embedded && !isOpen) return null

  // ---- 收起态：右缘竖排把手 ----
  if (!isOpen) {
    return (
      <button type="button" className="wb-rail-handle" onClick={toggle}
              title="展开：漏洞 / 发现" aria-label={`发现 ${unverified.length}`}>
        <ChevronLeft size={13} />
        <span className="wb-rail-vtext">发现 {unverified.length}</span>
        {hasHighUnverified && <span className="wb-rail-dot" title="存在高危未验证发现" />}
      </button>
    )
  }

  // ---- 展开态 ----
  return (
    <>
      <FindingsWatcher pid={pid} onFinding={refresh} />
      {!embedded && <div ref={splitterRef} className={cn("wb-splitter", dragging && "is-dragging")} role="separator"
           aria-orientation="vertical" aria-label="拖动调整发现栏宽度"
           onMouseDown={onSplitterDown} />}
      <aside className={cn("wb-rail", embedded && "wb-rail-embedded")} style={embedded ? { width: widthOverride ?? width, flexBasis: widthOverride ?? width } : { width, flexBasis: width }}>
        <div className="wb-rail-head">
          <ShieldAlert size={13} className="text-primary" />
          <span className="wb-rail-title">{chain ? "链路视图" : "漏洞 / 发现"}</span>
          {!chain && (
            <span className="text-[10px] text-muted-foreground">{unverified.length} 未验证</span>
          )}
          <span className="flex-1" />
          {chain ? (
            <button type="button" className="wb-rail-back" onClick={exitChain}
                    title="返回发现列表">
              <ChevronLeft size={12} /> 返回列表
            </button>
          ) : (
            canChain && (
              <button type="button" className="wb-rail-chain" onClick={enterChain}
                      title="在右栏原地查看攻击链路">
                <GitBranch size={11} /> 链路视图
              </button>
            )
          )}
          <button type="button" className="wb-rail-collapse" onClick={toggle} title="收起">
            <ChevronRight size={13} />
          </button>
        </div>

        {!chain && (
          <div className="wb-rail-filter">
            {isSplit && (
              <>
                <button type="button" className={cn("wb-rail-chip", cat === "vuln" && "is-on")}
                        onClick={() => setCat("vuln")}>漏洞</button>
                <button type="button" className={cn("wb-rail-chip", cat === "intel" && "is-on")}
                        onClick={() => setCat("intel")}>有效发现</button>
                <span className="mx-0.5 h-4 w-px bg-border" />
              </>
            )}
            {(["all", "unverified", "verified"] as const).map((s) => (
              <button key={s} type="button" className={cn("wb-rail-chip", status === s && "is-on")}
                      onClick={() => setStatus(s)}>
                {s === "all" ? "全部" : s === "unverified" ? "未验证" : "已验证"}
              </button>
            ))}
          </div>
        )}

        {chain ? (
          <div className="wb-rail-chain-body">
            <Suspense fallback={<div className="wb-rail-empty">加载链路画布…</div>}>
              <AttackPathLazy pid={pid} assets={assets} target={chainTarget || undefined}
                              onTargetChange={onTargetChange} />
            </Suspense>
          </div>
        ) : (
          <ScrollArea className="min-h-0 min-w-0 flex-1">
            <div className="min-w-0 space-y-1.5 p-2">
              {filtered.length === 0 && <div className="wb-rail-empty">暂无发现</div>}
              {filtered.map((f) => (
                <div key={f.id} className="wb-rail-row" onClick={() => setDetail(f)}>
                  <div className="flex min-w-0 items-start gap-2">
                    <span className={cn("shrink-0 text-[10px] font-medium", SEVERITY_COLOR[f.severity])}
                          title={f.rating_basis ? `判级依据: ${f.rating_basis}` : undefined}>
                      {f.severity}
                    </span>
                    <span className="min-w-0 flex-1 break-words text-[12px] leading-5">{f.title}</span>
                    {f.status === "verified" && (
                      <Badge variant="outline" className="shrink-0 text-[9px]">verified</Badge>
                    )}
                  </div>
                  <div className="mt-0.5 min-w-0 break-words font-mono text-[10px] leading-4 text-muted-foreground">
                    {f.vuln_class}
                    {f.poc_artifact_id && <> · POC</>}
                  </div>
                </div>
              ))}
            </div>
          </ScrollArea>
        )}
      </aside>

      {detail && (
        <FindingDetailDialog pid={pid} finding={detail} track={track}
          onClose={() => setDetail(null)}
          onMutated={() => { refresh(); setDetail(null) }} />
      )}
    </>
  )
})
