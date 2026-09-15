import { Suspense, lazy, useCallback, useEffect, useMemo, useState, type ReactNode } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import { api } from "@/lib/api"
import type { Asset, Finding, FuncEntry } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { hexAddr } from "@/lib/workbench"
import { fmtDateTime, utcTitle } from "@/lib/datetime"
import { FindingDetailDialog } from "@/components/blackboard/FindingDetailDialog"

// 黑板视图（DESIGN.md §12 页面 4）：发现 / 资产 / 函数库 + human 共写入口
// 发现 tab 内 assessment 轨额外提供「列表｜链路」子视图（攻击链画布，E1）。

const SEVERITY_COLOR: Record<string, string> = {
  critical: "text-[--status-error]",
  high: "text-[--status-error]",
  medium: "text-[--status-approval]",
  low: "text-muted-foreground",
  info: "text-muted-foreground",
}

// 攻击链画布懒加载：@xyflow/react ~192KB 不进主包（与逆向 ChainView 同策略）
const FindingsCanvas = lazy(() =>
  import("./blackboard/FindingsCanvas").then((m) => ({ default: m.FindingsCanvas })))

// 函数库 tab 仅 capabilities 含 binary 时挂载（func_kb 只由二进制分析产生；
// assessment web-only 项目里永远空数据）。判据是能力不是轨。
export function Blackboard({ pid, compact = false, track, capabilities }: {
  pid: string; compact?: boolean; track?: string; capabilities?: string[]
}) {
  const tabs = capabilities?.includes("binary")
    ? ["findings", "assets", "funcs"] as const
    : ["findings", "assets"] as const
  return (
    <Tabs defaultValue="findings" className="flex h-full flex-col gap-0">
      <TabsList className="w-full justify-start rounded-none border-b bg-transparent p-0">
        {tabs.map((t) => (
          <TabsTrigger key={t} value={t} className="rounded-none border-b-2 px-3 py-1.5 text-xs">
            {t === "findings" ? "发现" : t === "assets" ? "资产" : "函数库"}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="findings" className="min-h-0 flex-1">
        {/* 子视图切换仅 assessment track 且非 compact 侧栏时渲染 */}
        <Findings pid={pid} compact={compact} showCanvas={!compact && track === "assessment"} />
      </TabsContent>
      <TabsContent value="assets" className="min-h-0 flex-1">
        <Assets pid={pid} compact={compact} tree={track === "assessment"} />
      </TabsContent>
      {capabilities?.includes("binary") && (
        <TabsContent value="funcs" className="min-h-0 flex-1"><Funcs pid={pid} /></TabsContent>
      )}
    </Tabs>
  )
}

// severity 降序（critical→info）排序用
const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cn("rounded px-2 py-0.5 text-[10px] transition-colors",
        active ? "bg-primary/10 text-primary ring-1 ring-inset ring-primary/40"
               : "text-muted-foreground hover:bg-accent/40")}
    >
      {children}
    </button>
  )
}

function Findings({ pid, compact, showCanvas }: { pid: string; compact?: boolean; showCanvas: boolean }) {
  const [items, setItems] = useState<Finding[]>([])
  const [assets, setAssets] = useState<Asset[]>([])
  const [assetFilter, setAssetFilter] = useState("")
  const [sevFilter, setSevFilter] = useState("")      // ""=全部，否则作为 min_severity 下发
  const [statusFilter, setStatusFilter] = useState("") // "" | unverified | verified
  const [detail, setDetail] = useState<Finding | null>(null)  // 弹窗展示复现步骤/POC
  const [assetOpen, setAssetOpen] = useState(false)   // 资产筛选下拉展开态
  const [assetQuery, setAssetQuery] = useState("")     // 下拉内搜索词
  const [subView, setSubView] = useState<"list" | "canvas">("list")
  const [title, setTitle] = useState("")
  const [vulnClass, setVulnClass] = useState("triage")

  // 筛选全局生效；数据恒为当前项目（API 按 pid 查，无跨项目混杂）。
  // 资产维度客户端过滤（下拉只列 host，选中时展开后代 service/url 一并匹配
  // ——findings 挂的 target_asset_id 是叶子资产，按 host id 精确匹配会漏）。
  const refresh = useCallback(() =>
    api.findings(pid, {
      min_severity: sevFilter || undefined,
      verified_only: statusFilter === "verified" || undefined,
    }).then(setItems).catch(() => {}), [pid, sevFilter, statusFilter])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  // 资产筛选下拉数据源
  useEffect(() => {
    api.assets(pid).then(setAssets).catch(() => {})
  }, [pid])

  const assetName = useCallback((id: string | null) =>
    id ? assets.find((a) => a.id === id)?.value : undefined, [assets])

  // 资产筛选下拉数据源：只列 host（IP），按搜索词过滤
  const hostOptions = useMemo(
    () => assets.filter((a) => a.type === "host")
      .filter((a) => !assetQuery.trim() || a.value.toLowerCase().includes(assetQuery.trim().toLowerCase())),
    [assets, assetQuery])

  const visible = useMemo(() => {
    const rank = (s: string) => {
      const i = SEVERITY_ORDER.indexOf(s)
      return i === -1 ? SEVERITY_ORDER.length : i
    }
    let filtered = statusFilter === "unverified"
      ? items.filter((f) => f.status === "unverified")
      : items
    if (assetFilter) {
      // 选中 host → 展开其全部后代（service/url），挂在子树内的 finding 都算
      const subtree = new Set<string>([assetFilter])
      let grew = true
      while (grew) {
        grew = false
        for (const a of assets) {
          if (a.parent_id && subtree.has(a.parent_id) && !subtree.has(a.id)) {
            subtree.add(a.id)
            grew = true
          }
        }
      }
      filtered = filtered.filter((f) => f.target_asset_id && subtree.has(f.target_asset_id))
    }
    return [...filtered].sort((a, b) =>
      rank(a.severity) - rank(b.severity) || b.created_at.localeCompare(a.created_at))
  }, [items, assets, statusFilter, assetFilter])

  const add = async () => {
    if (!title.trim()) return
    await api.addFinding(pid, { vuln_class: vulnClass, title: title.trim(), status: "unverified" })
    setTitle("")
    refresh()
  }

  // IP/严重度/状态筛选行（列表与链路共用）
  const filterRow = (
    <div className="flex flex-wrap items-center gap-2 border-b px-3 py-1.5">
      {/* 资产筛选：可搜索下拉框（点开带搜索输入，按 IP 过滤后点击选择） */}
      <div className="relative">
        <button
          type="button"
          title="按资产筛选（host/IP）"
          onClick={() => { setAssetOpen((o) => !o); setAssetQuery("") }}
          className={cn("flex h-6 min-w-32 max-w-44 items-center justify-between gap-1 rounded border px-1.5 font-mono text-[11px]",
            assetFilter ? "border-primary/50 text-primary" : "text-foreground")}
        >
          <span className="truncate">
            {assetFilter ? (assetName(assetFilter) ?? "全部资产") : "全部资产"}
          </span>
          <ChevronDown className={cn("size-3 shrink-0 text-muted-foreground transition-transform", assetOpen && "rotate-180")} />
        </button>
        {assetOpen && (
          <>
            {/* 点外部关闭 */}
            <div className="fixed inset-0 z-40" onClick={() => setAssetOpen(false)} />
            <div className="absolute left-0 top-full z-50 mt-1 w-64 rounded-md border bg-popover text-popover-foreground shadow-md">
              <div className="border-b p-1.5">
                <Input
                  autoFocus
                  value={assetQuery}
                  onChange={(e) => setAssetQuery(e.target.value)}
                  placeholder="搜索 IP…"
                  className="h-6 text-[11px]"
                />
              </div>
              <div className="max-h-56 overflow-auto p-1">
                <button
                  type="button"
                  className={cn("block w-full rounded px-2 py-1 text-left text-[11px] hover:bg-accent/40",
                    !assetFilter && "bg-primary/10 text-primary")}
                  onClick={() => { setAssetFilter(""); setAssetOpen(false) }}
                >
                  全部资产
                </button>
                {hostOptions.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    className={cn("block w-full truncate rounded px-2 py-1 text-left font-mono text-[11px] hover:bg-accent/40",
                      assetFilter === a.id && "bg-primary/10 text-primary")}
                    onClick={() => { setAssetFilter(a.id); setAssetOpen(false) }}
                  >
                    {a.value}
                  </button>
                ))}
                {hostOptions.length === 0 && (
                  <p className="px-2 py-1 text-[11px] text-muted-foreground">无匹配资产</p>
                )}
              </div>
            </div>
          </>
        )}
      </div>
      <div className="flex items-center gap-0.5">
        <Chip active={sevFilter === ""} onClick={() => setSevFilter("")}>全部</Chip>
        {SEVERITY_ORDER.map((s) => (
          <Chip key={s} active={sevFilter === s} onClick={() => setSevFilter(sevFilter === s ? "" : s)}>
            <span className={cn("uppercase", SEVERITY_COLOR[s])}>{s}</span>
          </Chip>
        ))}
      </div>
      <div className="flex items-center gap-0.5">
        <Chip active={statusFilter === ""} onClick={() => setStatusFilter("")}>全部状态</Chip>
        <Chip active={statusFilter === "unverified"} onClick={() => setStatusFilter(statusFilter === "unverified" ? "" : "unverified")}>unverified</Chip>
        <Chip active={statusFilter === "verified"} onClick={() => setStatusFilter(statusFilter === "verified" ? "" : "verified")}>verified</Chip>
      </div>
      {showCanvas && (
        <div className="ml-auto flex items-center rounded border p-0.5 text-[11px]">
          <button
            type="button"
            className={cn("rounded px-2 py-0.5", subView === "list" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")}
            onClick={() => setSubView("list")}
          >
            列表
          </button>
          <button
            type="button"
            className={cn("rounded px-2 py-0.5", subView === "canvas" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")}
            onClick={() => setSubView("canvas")}
          >
            链路
          </button>
        </div>
      )}
    </div>
  )

  if (showCanvas && subView === "canvas") {
    return (
      <div className="flex h-full flex-col">
        {filterRow}
        <div className="relative min-h-0 flex-1">
          <Suspense fallback={
            <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
              加载攻击链画布…
            </div>
          }>
            <FindingsCanvas pid={pid} findings={visible} assets={assets} onMutated={refresh} />
          </Suspense>
        </div>
        {detail && (
          <FindingDetailDialog
            pid={pid}
            finding={detail}
            assetLabel={assetName(detail.target_asset_id)}
            onClose={() => setDetail(null)}
          />
        )}
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      {filterRow}
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-1.5 p-3">
          {visible.length === 0 && <Empty />}
          {visible.map((f) => (
            <div
              key={f.id}
              className="cursor-pointer rounded border p-2 hover:bg-accent/30"
              onClick={() => setDetail(f)}
            >
              <div className="flex items-center gap-2">
                <span className={cn("text-[10px] font-medium uppercase", SEVERITY_COLOR[f.severity])}>
                  {f.severity}
                </span>
                <span className="text-sm">{f.title}</span>
                {f.status === "verified" && <Badge variant="outline" className="text-[10px]">verified</Badge>}
                {f.poc_artifact_id && <Badge variant="outline" className="text-[10px]">POC</Badge>}
                <span className="flex-1" />
                <span className="font-mono text-[10px] text-muted-foreground">{f.author}</span>
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                {f.vuln_class} · <span title={utcTitle(f.created_at)}>{fmtDateTime(f.created_at)}</span>
                {assetName(f.target_asset_id) && <> · {assetName(f.target_asset_id)}</>}
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>
      {!compact && (
        <div className="flex gap-2 border-t p-2">
          <Input className="w-28" value={vulnClass} onChange={(e) => setVulnClass(e.target.value)} placeholder="类别" />
          <Input className="flex-1" value={title} onChange={(e) => setTitle(e.target.value)}
                 placeholder="手动添加发现（human 共写，§6.5）" onKeyDown={(e) => e.key === "Enter" && add()} />
          <Button size="sm" onClick={add} disabled={!title.trim()}>添加</Button>
        </div>
      )}
      {/* 点击卡片 → 弹窗展示复现步骤 + POC（一键复制） */}
      {detail && (
        <FindingDetailDialog
          pid={pid}
          finding={detail}
          assetLabel={assetName(detail.target_asset_id)}
          onClose={() => setDetail(null)}
        />
      )}
    </div>
  )
}

function AssetRow({ a, indent = false }: { a: Asset; indent?: boolean }) {
  const meta = a.meta ?? {}
  const hint = (meta.module ?? meta.platform ?? meta.source) as string | undefined
  const title = meta.title as string | undefined
  return (
    <div className={cn("rounded px-1 py-1 hover:bg-accent/40", indent && "ml-5")}>
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="font-mono text-[10px]">{a.type}</Badge>
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{a.value}</span>
        {/* 扫描标识（§5.2 meta.scanned）：AI 扫过的目标显「已扫」徽章 */}
        {!!meta.scanned && (
          <Badge variant="outline" className="text-[10px] text-primary">已扫</Badge>
        )}
        {hint && <span className="max-w-32 truncate text-[10px] text-muted-foreground">{hint}</span>}
        <span className="font-mono text-[10px] text-muted-foreground">{a.author}</span>
      </div>
      {/* 一句话简述（§5.2 meta.title）：页面 <title> 等人读信息 */}
      {title && (
        <div className="ml-1 mt-0.5 truncate text-[11px] text-muted-foreground">{title}</div>
      )}
    </div>
  )
}

function Assets({ pid, compact, tree = false }: { pid: string; compact?: boolean; tree?: boolean }) {
  const [items, setItems] = useState<Asset[]>([])
  const [type, setType] = useState("binary")
  const [value, setValue] = useState("")
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const refresh = useCallback(() =>
    api.assets(pid).then(setItems).catch(() => {}), [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  // 树视图分组（DESIGN.md §5.2 资产树）：parent_id 指向的资产存在 → 挂其下；
  // 悬挂引用（父不存在）回退主行。全无 parent 的旧数据自然退化平铺。
  const { roots, childrenOf } = useMemo(() => {
    const ids = new Set(items.map((a) => a.id))
    const childrenOf = new Map<string, Asset[]>()
    const roots: Asset[] = []
    for (const a of items) {
      if (a.parent_id && ids.has(a.parent_id)) {
        const list = childrenOf.get(a.parent_id) ?? []
        list.push(a)
        childrenOf.set(a.parent_id, list)
      } else {
        roots.push(a)
      }
    }
    return { roots, childrenOf }
  }, [items])

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const add = async () => {
    if (!value.trim()) return
    await api.addAsset(pid, type.trim(), value.trim())
    setValue("")
    refresh()
  }

  const renderRows = () => {
    if (items.length === 0) return <Empty />
    if (!tree) return items.map((a) => <AssetRow key={a.id} a={a} />)
    // 递归渲染（host → service → url 多层）：任意带子行的节点都可展开
    const countDesc = (id: string): number =>
      (childrenOf.get(id) ?? []).reduce((n, c) => n + 1 + countDesc(c.id), 0)
    const renderNode = (a: Asset, depth: number): ReactNode => {
      const children = childrenOf.get(a.id) ?? []
      const open = expanded.has(a.id)
      const meta = a.meta ?? {}
      return (
        <div key={a.id}>
          <div
            className={cn("flex items-center gap-2 rounded px-1 py-1 hover:bg-accent/40",
                          children.length && "cursor-pointer")}
            style={{ marginLeft: depth * 20 }}
            onClick={() => children.length && toggle(a.id)}
          >
            {children.length ? (
              open ? <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
                   : <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
            ) : <span className="size-3.5 shrink-0" />}
            <Badge variant="outline" className="font-mono text-[10px]">{a.type}</Badge>
            <span className="min-w-0 flex-1 truncate font-mono text-xs">{a.value}</span>
            {!!meta.scanned && (
              <Badge variant="outline" className="text-[10px] text-primary">已扫</Badge>
            )}
            {children.length > 0 && (
              <Badge variant="outline" className="font-mono text-[10px] text-muted-foreground">
                {countDesc(a.id)}
              </Badge>
            )}
            <span className="font-mono text-[10px] text-muted-foreground">{a.author}</span>
          </div>
          {typeof meta.title === "string" && (
            <div
              className="mt-0.5 truncate text-[11px] text-muted-foreground"
              style={{ marginLeft: depth * 20 + 22 }}
            >
              {meta.title}
            </div>
          )}
          {open && children.map((c) => renderNode(c, depth + 1))}
        </div>
      )
    }
    return roots.map((a) => renderNode(a, 0))
  }

  return (
    <div className="flex h-full flex-col">
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-1 p-3">{renderRows()}</div>
      </ScrollArea>
      {!compact && (
        <div className="flex gap-2 border-t p-2">
          <Input className="w-28" value={type} onChange={(e) => setType(e.target.value)} placeholder="类型" />
          <Input className="flex-1" value={value} onChange={(e) => setValue(e.target.value)}
                 placeholder="手动添加资产（值/哈希/路径）" onKeyDown={(e) => e.key === "Enter" && add()} />
          <Button size="sm" onClick={add} disabled={!value.trim()}>添加</Button>
        </div>
      )}
    </div>
  )
}

function Funcs({ pid }: { pid: string }) {
  const [items, setItems] = useState<FuncEntry[]>([])
  const refresh = useCallback(() =>
    api.funcs(pid).then(setItems).catch(() => {}), [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  return (
    <ScrollArea className="h-full">
      <div className="space-y-1 p-3">
        {items.length === 0 && <Empty />}
        {items.map((f) => (
          <div key={f.id} className="rounded border p-2">
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-xs text-primary">{f.name}</span>
              <span className="font-mono text-[10px] text-muted-foreground">@{hexAddr(f.address)}</span>
              {f.risk_tags.length > 0 && (
                <span className="font-mono text-[10px] text-[--status-approval]">{f.risk_tags.join(",")}</span>
              )}
              <span className="flex-1" />
              <span className="font-mono text-[10px] text-muted-foreground">{f.confidence}</span>
            </div>
            <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">sha256:{f.binary_sha256.slice(0, 12)}… · {f.analyzed_by}</div>
            {f.analysis && <p className="mt-1 text-xs text-muted-foreground">{f.analysis}</p>}
          </div>
        ))}
      </div>
    </ScrollArea>
  )
}

function Empty() {
  return <p className="py-6 text-center text-xs text-muted-foreground">暂无数据</p>
}
