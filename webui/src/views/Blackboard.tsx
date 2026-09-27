import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import { api } from "@/lib/api"
import type { Asset, Finding, FindingCategory, FuncEntry } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { SEVERITY_COLOR } from "@/lib/events"
import { hexAddr } from "@/lib/workbench"
import { fmtDateTime, utcTitle } from "@/lib/datetime"
import { FindingDetailDialog } from "@/components/blackboard/FindingDetailDialog"
import { CTF_LEVEL, CTF_LEVEL_ORDER } from "./blackboard/ctfLevel"
import { MappingPane, ProductChips } from "./blackboard/MappingPane"

// 黑板视图（DESIGN.md §12 页面 4）：发现 / 资产 / 函数库 + human 共写入口
// 发现 tab 内 assessment 轨额外提供「列表｜链路」子视图（攻击链画布，E1）。

// 单站攻击链路图懒加载：@xyflow/react ~192KB 不进主包（与逆向 ChainView 同策略）
const AttackPathCanvas = lazy(() =>
  import("./blackboard/AttackPath").then((m) => ({ default: m.AttackPath })))

// 函数库 tab 仅 capabilities 含 binary 时挂载（func_kb 只由二进制分析产生；
// assessment web-only 项目里永远空数据）。判据是能力不是轨。
// 测绘 tab CTF 轨不挂载（2026-09-23 用户反馈：CTF 无资产收集场景，FOFA/表格导入用不上）。
// compact 侧栏（直播间右侧窄栏）渗透轨不挂测绘/函数库（2026-09-26 用户要求：
// 渗透项目右侧用不上这两个 tab；主黑板视图不受影响）。
// 全景 tab（黑板链路图）2026-09-26 用户要求下线：tab 移除、boardGraph/ 前端删除；
// 后端 board-graph 只读端点保留。
// M4c 场景档 board_view：defaultView（config.board_view.default）不在可用集合时回退 findings。
export function Blackboard({ pid, compact = false, track, capabilities, defaultView }: {
  pid: string; compact?: boolean; track?: string; capabilities?: string[]; defaultView?: string
}) {
  const compactPentest = compact && track === "pentest"
  const tabs = [
    "findings", "assets",
    ...(track !== "ctf" && !compactPentest ? ["mapping"] as const : []),
    ...(capabilities?.includes("binary") && !compactPentest ? ["funcs"] as const : []),
  ] as const
  const allTabs: readonly string[] = tabs
  const initial = defaultView && allTabs.includes(defaultView) ? defaultView : "findings"
  const [tab, setTab] = useState(initial)
  // meta 异步晚到：defaultView 首次可用且用户尚未手动切过 tab 时补切一次
  const touched = useRef(false)
  useEffect(() => {
    if (!touched.current && defaultView && allTabs.includes(defaultView)) {
      setTab(defaultView)
    }
  }, [defaultView]) // eslint-disable-line react-hooks/exhaustive-deps
  // tabs 集合变化（如 meta 到齐后 CTF 轨剔除 mapping）时，选中值悬空则回退——防困在空白内容区
  const tabsKey = allTabs.join("|")
  useEffect(() => {
    setTab((prev) => (allTabs.includes(prev) ? prev : initial))
  }, [tabsKey]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Tabs value={tab} onValueChange={(v) => { touched.current = true; setTab(v) }} className="flex h-full flex-col gap-0">
      <TabsList className="w-full justify-start rounded-none border-b bg-transparent p-0">
        {tabs.map((t) => (
          <TabsTrigger key={t} value={t} className="rounded-none border-b-2 px-3 py-1.5 text-xs">
            {t === "findings" ? "发现" : t === "assets" ? "资产" : t === "mapping" ? "测绘" : "函数库"}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="findings" className="min-h-0 flex-1">
        {/* 子视图切换仅渗透/红队轨（R2 拆轨前判断 assessment）且非 compact 侧栏时渲染 */}
        <Findings pid={pid} compact={compact} track={track}
                  showCanvas={!compact && (track === "pentest" || track === "redteam")} />
      </TabsContent>
      <TabsContent value="assets" className="min-h-0 flex-1">
        {/* 树视图全轨启用（2026-09-20）：E6 自动挂载全轨生效，ctf 等轨同样有 parent 树；
            无 parent 的行自然退化平铺 */}
        <Assets pid={pid} compact={compact} tree />
      </TabsContent>
      {track !== "ctf" && !compactPentest && (
        <TabsContent value="mapping" className="min-h-0 flex-1">
          {/* 网络空间测绘（cyberspace-mapping M1+M2）：FOFA 查询导入 + 表格导入；
              compact 侧栏不挂（配置/表格类操作不适合窄栏） */}
          {!compact && <MappingPane pid={pid} />}
        </TabsContent>
      )}
      {capabilities?.includes("binary") && !compactPentest && (
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

// CTF 线索级别词表已提出共享：./blackboard/ctfLevel（列表视图与全景链路图共用）

export function Findings({ pid, compact, track, showCanvas }: {
  pid: string; compact?: boolean; track?: string; showCanvas: boolean
}) {
  const isCtf = track === "ctf"
  const [items, setItems] = useState<Finding[]>([])
  const [assets, setAssets] = useState<Asset[]>([])
  const [assetFilter, setAssetFilter] = useState("")
  const [sevFilter, setSevFilter] = useState("")      // ""=全部，否则作为 min_severity 下发
  const [statusFilter, setStatusFilter] = useState("") // "" | unverified | verified
  // C6 分两类硬切换（仅渗透/红队轨）：vuln=漏洞 / intel=有效发现·关键发现，互不混显
  const isSplit = track === "pentest" || track === "redteam"
  const [catView, setCatView] = useState<FindingCategory>("vuln")
  // 手动添加的严重度（漏洞视图不含 info——门禁①）
  const [addSev, setAddSev] = useState("low")
  const [levelFilter, setLevelFilter] = useState("")   // C2：ctf 线索级别筛选（""=全部未折叠）
  const [detail, setDetail] = useState<Finding | null>(null)  // 弹窗展示复现步骤/POC
  const [assetOpen, setAssetOpen] = useState(false)   // 资产筛选下拉展开态
  const [assetQuery, setAssetQuery] = useState("")     // 下拉内搜索词
  const [subView, setSubView] = useState<"list" | "canvas">("list")
  const [title, setTitle] = useState("")
  const [vulnClass, setVulnClass] = useState("")

  // 筛选全局生效；数据恒为当前项目（API 按 pid 查，无跨项目混杂）。
  // 资产维度客户端过滤（下拉只列 host/domain，选中时展开后代 service/url 一并匹配
  // ——findings 挂的 target_asset_id 是叶子资产，按根 id 精确匹配会漏）。
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

  // 资产筛选下拉只列 host/domain（2026-09-24 用户定稿：过滤只按 IP/域名），
  // 按值子串搜索；选中后沿 parent_id 展开子树过滤（url/service 叶子 finding 不漏）
  const assetOptions = useMemo(() => {
    const kw = assetQuery.trim().toLowerCase()
    return assets
      .filter((a) => ["host", "domain"].includes(a.type))
      .filter((a) => !kw || a.value.toLowerCase().includes(kw))
  }, [assets, assetQuery])

  const visible = useMemo(() => {
    const rank = (s: string) => {
      const i = (isCtf ? CTF_LEVEL_ORDER : SEVERITY_ORDER).indexOf(s)
      return i === -1 ? SEVERITY_ORDER.length : i
    }
    let filtered = statusFilter === "unverified"
      ? items.filter((f) => f.status === "unverified")
      : items
    // C6 分两类硬切换：渗透/红队轨按 catView 严格过滤（两侧互不混显）
    if (isSplit) filtered = filtered.filter((f) => (f.category ?? "vuln") === catView)
    if (isCtf) {
      // C2 死路默认折叠：status=false-positive 仅在「死路」筛选下显示
      filtered = levelFilter === "dead"
        ? filtered.filter((f) => f.status === "false-positive")
        : filtered.filter((f) => f.status !== "false-positive")
      if (levelFilter === "bg") {
        filtered = filtered.filter((f) => ["low", "medium", "info"].includes(f.severity))
      } else if (levelFilter && levelFilter !== "dead") {
        filtered = filtered.filter((f) => f.severity === levelFilter)
      }
    }
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
  }, [items, assets, statusFilter, assetFilter, isCtf, levelFilter, catView, isSplit])

  const add = async () => {
    if (!title.trim()) return
    await api.addFinding(pid, {
      vuln_class: vulnClass || "clue", title: title.trim(), status: "unverified",
      category: isSplit ? catView : undefined, severity: addSev,
    })
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
          title="按资产筛选（IP/域名/URL/服务）"
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
                  placeholder="搜索 IP/域名…"
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
                {assetOptions.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    className={cn("block w-full truncate rounded px-2 py-1 text-left font-mono text-[11px] hover:bg-accent/40",
                      assetFilter === a.id && "bg-primary/10 text-primary")}
                    onClick={() => { setAssetFilter(a.id); setAssetOpen(false) }}
                  >
                    <span className="mr-1 rounded border border-[#30363d] px-0.5 text-[9px] text-muted-foreground">
                      {a.type}
                    </span>
                    {a.value}
                  </button>
                ))}
                {assetOptions.length === 0 && (
                  <p className="px-2 py-1 text-[11px] text-muted-foreground">无匹配资产</p>
                )}
              </div>
            </div>
          </>
        )}
      </div>
      <div className="flex items-center gap-0.5">
        {isCtf ? (
          <>
            <Chip active={levelFilter === ""} onClick={() => setLevelFilter("")}>全部</Chip>
            <Chip active={levelFilter === "critical"} onClick={() => setLevelFilter(levelFilter === "critical" ? "" : "critical")}>
              <span className="text-(--status-error)">关键突破</span>
            </Chip>
            <Chip active={levelFilter === "high"} onClick={() => setLevelFilter(levelFilter === "high" ? "" : "high")}>
              <span className="text-(--status-approval)">有效线索</span>
            </Chip>
            <Chip active={levelFilter === "bg"} onClick={() => setLevelFilter(levelFilter === "bg" ? "" : "bg")}>背景信息</Chip>
            <Chip active={levelFilter === "dead"} onClick={() => setLevelFilter(levelFilter === "dead" ? "" : "dead")}>
              死路 {items.filter((f) => f.status === "false-positive").length || ""}
            </Chip>
          </>
        ) : (
          <>
            <Chip active={sevFilter === ""} onClick={() => setSevFilter("")}>全部</Chip>
            {/* 渗透/红队轨 info 停收（2026-09-18）：筛选 chips 同步剔除 info 档 */}
            {SEVERITY_ORDER.filter((s) => !isSplit || s !== "info").map((s) => (
              <Chip key={s} active={sevFilter === s} onClick={() => setSevFilter(sevFilter === s ? "" : s)}>
                <span className={cn("uppercase", SEVERITY_COLOR[s])}>{s}</span>
              </Chip>
            ))}
          </>
        )}
      </div>
      <div className="flex items-center gap-0.5">
        <Chip active={statusFilter === ""} onClick={() => setStatusFilter("")}>全部状态</Chip>
        <Chip active={statusFilter === "unverified"} onClick={() => setStatusFilter(statusFilter === "unverified" ? "" : "unverified")}>unverified</Chip>
        <Chip active={statusFilter === "verified"} onClick={() => setStatusFilter(statusFilter === "verified" ? "" : "verified")}>verified</Chip>
      </div>
      {/* C6 分两类硬切换：漏洞 / 有效发现 两视图互不混显（仅渗透/红队轨） */}
      {isSplit && (
        <div className="ml-auto flex items-center rounded border p-0.5 text-[11px]">
          <button
            type="button"
            className={cn("rounded px-2 py-0.5", catView === "vuln" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")}
            onClick={() => setCatView("vuln")}
          >
            漏洞
          </button>
          <button
            type="button"
            className={cn("rounded px-2 py-0.5", catView === "intel" ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")}
            onClick={() => setCatView("intel")}
          >
            有效发现
          </button>
        </div>
      )}
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
              加载攻击链路…
            </div>
          }>
            {/* 列表资产过滤态贯通：选中 IP/域名后切链路直接作为目标；未选 → 组件内目标引导 */}
            <AttackPathCanvas pid={pid} assets={assets} target={assetFilter || undefined} />
          </Suspense>
        </div>
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
                <span className={cn("text-[10px] font-medium", isCtf ? CTF_LEVEL[f.severity]?.cls : SEVERITY_COLOR[f.severity])}
                      title={f.rating_basis ? `判级依据: ${f.rating_basis}` : undefined}>
                  {isCtf ? (CTF_LEVEL[f.severity]?.label ?? f.severity) : f.severity}
                </span>
                <span className="text-sm">{f.title}</span>
                {isSplit && (
                  <Badge variant="outline"
                         className={cn("text-[10px]",
                           (f.category ?? "vuln") === "intel" ? "text-(--status-ok)" : "text-(--status-error)")}>
                    {(f.category ?? "vuln") === "intel" ? "📌 有效发现" : "漏洞"}
                  </Badge>
                )}
                {f.status === "verified" && <Badge variant="outline" className="text-[10px]">verified</Badge>}
                {f.status === "false-positive" && isCtf && (
                  <Badge variant="outline" className="text-[10px] text-muted-foreground">死路</Badge>
                )}
                {f.poc_artifact_id && <Badge variant="outline" className="text-[10px]">POC</Badge>}
                <span className="flex-1" />
                <span className="font-mono text-[10px] text-muted-foreground">{f.author}</span>
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                {f.vuln_class} · <span title={utcTitle(f.created_at)}>{fmtDateTime(f.created_at)}</span>
                {assetName(f.target_asset_id) && <> · {assetName(f.target_asset_id)}</>}
                {(f.rating_basis ?? "").trim() && <> · <span title={`判级依据: ${f.rating_basis}`}>{f.rating_basis.length > 40 ? `${f.rating_basis.slice(0, 40)}…` : f.rating_basis}</span></>}
              </div>
            </div>
          ))}
        </div>
      </ScrollArea>
      {!compact && (
        <div className="flex gap-2 border-t p-2">
          <select className="h-8 w-20 shrink-0 rounded-md border bg-background px-2 text-xs"
                  value={addSev} onChange={(e) => setAddSev(e.target.value)}
                  title="严重度">
            {(isSplit
              ? ["low", "medium", "high", "critical"] // 渗透/红队轨 info 停收（2026-09-18，全类别）
              : ["info", "low", "medium", "high", "critical"]
            ).map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <Input className="min-w-0 flex-1" value={vulnClass} onChange={(e) => setVulnClass(e.target.value)} placeholder="类别（如 暴露面/远程管理服务、信息点）" />
          <Input className="min-w-0 flex-1" value={title} onChange={(e) => setTitle(e.target.value)}
                 placeholder="手动添加发现（human 共写，§6.5）" onKeyDown={(e) => e.key === "Enter" && add()} />
          <Button size="sm" onClick={add} disabled={!title.trim()}>添加</Button>
        </div>
      )}
      {/* 点击卡片 → 弹窗展示复现步骤 + POC（一键复制） */}
      {detail && (
        <FindingDetailDialog
          key={detail.id}
          pid={pid}
          finding={detail}
          assetLabel={assetName(detail.target_asset_id)}
          track={track}
          onClose={() => setDetail(null)}
          onMutated={refresh}
        />
      )}
    </div>
  )
}

// E7 资产状态徽章（§5.2；2026-09-19 扩六态）：open 不显；旧数据 meta.scanned=true 映射「已访问」；
// 「有发现」= 该资产挂 verified finding 的前端反查（结论以 findings 为准，AI 不自报）；
// budget_stop/na（借鉴 dsh AttackAtlas 覆盖态）回答「哪里没挖完、为什么」
function AssetBadges({ a, hasFinding }: { a: Asset; hasFinding?: boolean }) {
  if (hasFinding)
    return <Badge variant="outline" className="text-[10px] text-(--status-error)">有发现</Badge>
  const s = a.status === "open" && a.meta?.scanned ? "visited" : a.status
  if (s === "visited")
    return <Badge variant="outline" className="text-[10px] text-muted-foreground">已访问</Badge>
  if (s === "scanning")
    return <Badge variant="outline" className="text-[10px] text-amber-400">扫描中</Badge>
  if (s === "tested_clean")
    return <Badge variant="outline" className="text-[10px] text-primary">已测试·干净</Badge>
  if (s === "budget_stop")
    return (
      <Badge variant="outline" className="text-[10px] text-(--status-paused)"
             title="预算/配额用尽被迫停手（原因见事件流 asset.status_changed 的 note）">
        ⏸ 预算停手
      </Badge>
    )
  if (s === "na")
    return (
      <Badge variant="outline" className="text-[10px] text-muted-foreground/70"
             title="确认不适用（原因见事件流 asset.status_changed 的 note）">
        ⊘ 不适用
      </Badge>
    )
  return null
}

/** 资产标签 chips（W 后续/态势增强：meta.tags；「高价值」资产会进编排器每轮态势注入） */
function AssetTags({ tags }: { tags: string[] }) {
  if (!tags.length) return null
  return (
    <>
      {tags.map((t) => (
        <Badge key={t} variant="outline"
               className={cn("text-[10px]",
                 t === "高价值" ? "border-amber-400/60 text-amber-400" : "text-muted-foreground")}>
          {t === "高价值" ? "⭐高价值" : `#${t}`}
        </Badge>
      ))}
    </>
  )
}

function AssetRow({ a, indent = false, hasFinding, onToggleHvt, onDelete }:
                  { a: Asset; indent?: boolean; hasFinding?: boolean
                    onToggleHvt?: (a: Asset) => void; onDelete?: (a: Asset) => void }) {
  const meta = a.meta ?? {}
  const src = meta.source as string | undefined
  const hint = (meta.module ?? meta.platform ?? (src ? undefined : meta.source)) as string | undefined
  const title = meta.title as string | undefined
  const tags = (meta.tags as string[] | undefined) ?? []
  const products = (meta.products as string[] | undefined) ?? []
  return (
    <div className={cn("rounded px-1 py-1 hover:bg-accent/40", indent && "ml-5")}>
      <div className="flex items-center gap-2">
        <Badge variant="outline" className="font-mono text-[10px]">{a.type}</Badge>
        {/* 状态徽章紧贴值（用户定稿 2026-09-19）；flex-1 占位在徽章之后，右侧只留标签/⭐/hint/会话 */}
        <span className="min-w-0 shrink truncate font-mono text-xs">{a.value}</span>
        <AssetBadges a={a} hasFinding={hasFinding} />
        <span className="min-w-0 flex-1" />
        <AssetTags tags={tags} />
        {/* 来源徽章（cyberspace-mapping M1：fofa=测绘 🛰 / xlsx|csv=文件导入 📥） */}
        {src === "fofa" && <span title="来源：FOFA 测绘导入" className="shrink-0 text-[10px]">🛰</span>}
        {(src === "xlsx" || src === "csv") && <span title={`来源：${src} 文件导入`} className="shrink-0 text-[10px]">📥</span>}
        <ProductChips products={products} />
        {onToggleHvt && (
          <button
            title={tags.includes("高价值") ? "取消高价值标记" : "标记为高价值（⭐ 进编排器每轮态势注入）"}
            className={cn("shrink-0 rounded px-1 text-[10px] hover:bg-accent",
              tags.includes("高价值") ? "text-amber-400" : "text-muted-foreground/50")}
            onClick={(e) => { e.stopPropagation(); onToggleHvt(a) }}
          >
            ⭐
          </button>
        )}
        {onDelete && (
          <button
            title="删除该资产：仅叶子且无发现引用可删（有子资产/发现引用请先处理）"
            className="shrink-0 rounded px-1 text-[10px] text-muted-foreground/50 hover:bg-accent hover:text-(--status-error)"
            onClick={(e) => { e.stopPropagation(); onDelete(a) }}
          >
            🗑
          </button>
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

export function Assets({ pid, compact, tree = false }: { pid: string; compact?: boolean; tree?: boolean }) {
  const [items, setItems] = useState<Asset[]>([])
  const [type, setType] = useState("auto")   // E6：默认自动识别（修旧默认 binary bug）
  const [value, setValue] = useState("")
  const [err, setErr] = useState("")
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // 态势增强：资产标签（meta.tags）+「高价值」筛选；标签随编排器每轮态势注入
  const [tagFilter, setTagFilter] = useState("")
  const [tagErr, setTagErr] = useState<string | null>(null)
  // E7「有发现」徽章数据源：verified findings 的 target_asset_id 反查集合
  const [verifiedIds, setVerifiedIds] = useState<Set<string>>(new Set())

  const refresh = useCallback(() => {
    api.assets(pid).then(setItems).catch(() => {})
    api.findings(pid).then((fs) =>
      setVerifiedIds(new Set(fs
        .filter((f) => f.status === "verified" && f.target_asset_id)
        .map((f) => f.target_asset_id as string)))).catch(() => {})
  }, [pid])
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

  const allTags = useMemo(() => {
    const s = new Map<string, number>()
    for (const a of items) {
      for (const t of (a.meta?.tags as string[] | undefined) ?? []) {
        const k = t.trim()
        if (k) s.set(k, (s.get(k) ?? 0) + 1)
      }
    }
    return [...s.entries()].sort((x, y) => y[1] - x[1]).map(([t]) => t)
  }, [items])
  const visible = tagFilter
    ? items.filter((a) => ((a.meta?.tags as string[] | undefined) ?? [])
        .some((t) => t.trim() === tagFilter))
    : items
  const toggleHvt = async (a: Asset) => {
    const cur = (a.meta?.tags as string[] | undefined) ?? []
    const tags = cur.includes("高价值")
      ? cur.filter((t) => t !== "高价值")
      : [...cur, "高价值"]
    setTagErr(null)
    try {
      await api.patchAsset(pid, a.id, { meta: { tags } })
      refresh()
    } catch (e) {
      setTagErr(`标签保存失败：${e instanceof Error ? e.message : e}`)
    }
  }

  const add = async () => {
    if (!value.trim()) return
    try {
      await api.addAsset(pid, type.trim(), value.trim())
      setErr("")
      setValue("")
      refresh()
    } catch (e) {
      // 识别不出类型的 422 提示手选（E6 ②）
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  // 删除指定资产（2026-09-25）：确认后走 DELETE 端点；409（有子资产/被发现引用）
  // 文案落底部 err 行
  const remove = async (a: Asset) => {
    if (!window.confirm(`确认删除资产「${a.value}」？\n仅叶子资产且无发现引用可删除，删除后不可恢复。`)) return
    try {
      await api.deleteAsset(pid, a.id)
      setErr("")
      refresh()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  const renderRows = () => {
    if (items.length === 0) return <Empty />
    if (tagFilter) {
      // 标签筛选时退平铺（树内子孙可能不满足筛选条件）
      if (visible.length === 0) return <p className="p-2 text-xs text-muted-foreground">该标签下暂无资产</p>
      return visible.map((a) => (
        <AssetRow key={a.id} a={a} hasFinding={verifiedIds.has(a.id)}
                  onToggleHvt={!compact ? toggleHvt : undefined}
                  onDelete={!compact ? remove : undefined} />
      ))
    }
    if (!tree) return items.map((a) => (
      <AssetRow key={a.id} a={a} hasFinding={verifiedIds.has(a.id)} onToggleHvt={!compact ? toggleHvt : undefined} />
    ))
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
            {/* 状态徽章紧贴值（用户定稿 2026-09-19）；flex-1 占位在徽章之后 */}
            <span className="min-w-0 shrink truncate font-mono text-xs">{a.value}</span>
            <AssetBadges a={a} hasFinding={verifiedIds.has(a.id)} />
            <span className="min-w-0 flex-1" />
            <AssetTags tags={(meta.tags as string[] | undefined) ?? []} />
            {!compact && (
              <button
                title={(meta.tags as string[] | undefined)?.includes("高价值")
                  ? "取消高价值标记" : "标记为高价值（⭐ 进编排器每轮态势注入）"}
                className={cn("shrink-0 rounded px-1 text-[10px] hover:bg-accent",
                  (meta.tags as string[] | undefined)?.includes("高价值")
                    ? "text-amber-400" : "text-muted-foreground/50")}
                onClick={(e) => { e.stopPropagation(); toggleHvt(a) }}
              >
                ⭐
              </button>
            )}
            {!compact && (
              <button
                title="删除该资产：仅叶子且无发现引用可删（有子资产/发现引用请先处理）"
                className="shrink-0 rounded px-1 text-[10px] text-muted-foreground/50 hover:bg-accent hover:text-(--status-error)"
                onClick={(e) => { e.stopPropagation(); remove(a) }}
              >
                🗑
              </button>
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
      {!compact && (
        <div className="flex flex-wrap items-center gap-1 border-b px-3 py-1.5">
          <button className={cn("rounded px-2 py-0.5 text-[10px]",
            tagFilter === "" ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent")}
            onClick={() => setTagFilter("")}>
            全部
          </button>
          {tagErr && <span className="text-[10px] text-(--status-error)">{tagErr}</span>}
          {["高价值", ...allTags.filter((t) => t !== "高价值")].map((t) => (
            <button key={t}
              className={cn("rounded px-2 py-0.5 text-[10px]",
                tagFilter === t ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-accent",
                t === "高价值" && "text-amber-400")}
              onClick={() => setTagFilter(tagFilter === t ? "" : t)}>
              {t === "高价值" ? `⭐${t}` : `#${t}`}
            </button>
          ))}
        </div>
      )}
      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-1 p-3">{renderRows()}</div>
      </ScrollArea>
      {!compact && (
        <div className="border-t p-2">
          <div className="flex gap-2">
            <select className="h-8 w-24 shrink-0 rounded-md border bg-background px-2 text-xs"
                    value={type} onChange={(e) => setType(e.target.value)}
                    title="auto=按值自动识别（url/IPv4/host:port/完整域名/64hex）">
              <option value="auto">自动</option>
              <option value="host">host</option>
              <option value="domain">domain</option>
              <option value="service">service</option>
              <option value="url">url</option>
              <option value="binary">binary</option>
            </select>
            <Input className="flex-1" value={value} onChange={(e) => setValue(e.target.value)}
                   placeholder="手动添加资产（值/哈希/路径；自动=识别类型）"
                   onKeyDown={(e) => e.key === "Enter" && add()} />
            <Button size="sm" onClick={add} disabled={!value.trim()}>添加</Button>
          </div>
          {err && <p className="mt-1 text-[10px] text-(--status-error)">{err}</p>}
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
                <span className="font-mono text-[10px] text-(--status-approval)">{f.risk_tags.join(",")}</span>
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
