import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from "react"
import { ChevronLeft, ChevronRight, GitBranch, ShieldAlert } from "lucide-react"
import { api } from "@/lib/api"
import type { Finding } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"
import { SEVERITY_COLOR } from "@/lib/events"
import { FindingDetailDialog } from "@/components/blackboard/FindingDetailDialog"
import { useEvents } from "@/lib/useEvents"

// 智能体工作台·右侧发现栏（2026-10-01）：默认收起；展开后为「漏洞/发现」精简列表
// （点击项弹详情，复用 FindingDetailDialog）+「链路视图」快捷跳转（仅渗透/红队轨）。
// 收起态为右缘竖排把手：展开箭头 + 「发现 N」（N=未验证数）+ 高危红点。
// 数据：api.findings 4s 轮询（与黑板同）+ finding.* 事件即时刷新；宽度 280–460 可拖拽。

const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
const RAIL_MIN = 280
const RAIL_MAX = 460
const RAIL_DEFAULT = 320

type CatView = "vuln" | "intel"
type StatusView = "all" | "unverified" | "verified"

export function FindingsRail({ pid, track, onOpenChain }: {
  pid: string
  track?: string
  onOpenChain?: () => void
}) {
  const [open, setOpen] = useState(
    () => window.localStorage.getItem("ui.wb-findings-open") === "1")
  const [width, setWidth] = useState(() => {
    const w = Number(window.localStorage.getItem("ui.wb-findings-w"))
    return Number.isFinite(w) && w >= RAIL_MIN && w <= RAIL_MAX ? w : RAIL_DEFAULT
  })
  const [dragging, setDragging] = useState(false)
  const dragOrigin = useRef<{ x: number; w: number } | null>(null)
  const [items, setItems] = useState<Finding[]>([])
  const [cat, setCat] = useState<CatView>("vuln")
  const [status, setStatus] = useState<StatusView>("all")
  const [detail, setDetail] = useState<Finding | null>(null)
  const { events } = useEvents(pid)

  const isSplit = track === "pentest" || track === "redteam"
  const canChain = isSplit   // 链路画布仅渗透/红队轨（与黑板 showCanvas 判据一致）

  const refresh = useCallback(() => {
    api.findings(pid).then(setItems).catch(() => {})
  }, [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  // finding.* 事件即时刷新（比 4s 轮询更早反映新增/编辑/删除）
  const lastFindingEv = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--)
      if (events[i].kind.startsWith("finding.")) return events[i]
    return null
  }, [events])
  useEffect(() => { if (lastFindingEv) refresh() }, [lastFindingEv, refresh])

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

  // 拖左缘调宽（向左拖 = 变宽）；松手落盘
  const onSplitterDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    dragOrigin.current = { x: e.clientX, w: width }
    setDragging(true)
  }
  useEffect(() => {
    if (!dragging) return
    const move = (e: MouseEvent) => {
      const o = dragOrigin.current
      if (o) setWidth(Math.min(RAIL_MAX, Math.max(RAIL_MIN, o.w + (o.x - e.clientX))))
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
  }, [dragging])
  useEffect(() => {
    if (!dragging) window.localStorage.setItem("ui.wb-findings-w", String(width))
  }, [dragging, width])

  // ---- 收起态：右缘竖排把手 ----
  if (!open) {
    return (
      <button type="button" className="wb-rail-handle" onClick={toggle}
              title="展开：漏洞 / 发现">
        <ChevronLeft size={13} />
        <span className="wb-rail-vtext">发现 {unverified.length}</span>
        {hasHighUnverified && <span className="wb-rail-dot" title="存在高危未验证发现" />}
      </button>
    )
  }

  // ---- 展开态 ----
  return (
    <>
      <div className={cn("wb-splitter", dragging && "is-dragging")} role="separator"
           aria-orientation="vertical" aria-label="拖动调整发现栏宽度"
           onMouseDown={onSplitterDown} />
      <aside className="wb-rail" style={{ width, flexBasis: width }}>
        <div className="wb-rail-head">
          <ShieldAlert size={13} className="text-primary" />
          <span className="wb-rail-title">漏洞 / 发现</span>
          <span className="text-[10px] text-muted-foreground">{unverified.length} 未验证</span>
          <span className="flex-1" />
          {canChain && (
            <button type="button" className="wb-rail-chain" onClick={onOpenChain}
                    title="打开黑板·发现·链路视图">
              <GitBranch size={11} /> 链路视图
            </button>
          )}
          <button type="button" className="wb-rail-collapse" onClick={toggle} title="收起">
            <ChevronRight size={13} />
          </button>
        </div>

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

        <ScrollArea className="min-h-0 flex-1">
          <div className="space-y-1.5 p-2">
            {filtered.length === 0 && <div className="wb-rail-empty">暂无发现</div>}
            {filtered.map((f) => (
              <div key={f.id} className="wb-rail-row" onClick={() => setDetail(f)}>
                <div className="flex items-center gap-2">
                  <span className={cn("text-[10px] font-medium", SEVERITY_COLOR[f.severity])}
                        title={f.rating_basis ? `判级依据: ${f.rating_basis}` : undefined}>
                    {f.severity}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[12px]">{f.title}</span>
                  {f.status === "verified" && (
                    <Badge variant="outline" className="text-[9px]">verified</Badge>
                  )}
                </div>
                <div className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">
                  {f.vuln_class}
                  {f.poc_artifact_id && <> · POC</>}
                </div>
              </div>
            ))}
          </div>
        </ScrollArea>
      </aside>

      {detail && (
        <FindingDetailDialog pid={pid} finding={detail} track={track}
          onClose={() => setDetail(null)}
          onMutated={() => { refresh(); setDetail(null) }} />
      )}
    </>
  )
}
