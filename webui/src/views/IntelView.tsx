import { useCallback, useEffect, useState, type MouseEvent } from "react"
import { api, pollJob } from "@/lib/api"
import type {
  IntelArticle, IntelBriefMeta, IntelLearningProfile, IntelOverview,
  IntelPlan, IntelPlanMeta, IntelProfile, IntelSearchHistoryItem,
} from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { fmtDateTimeMin } from "@/lib/datetime"
import { cn } from "@/lib/utils"

// 情报面板（E9/E10，DESIGN.md §16.4/§16.3）：全局模块，与项目无关。
// 三 tab = 简报（每日中文简报 + 归档）/ 文章（高分文章池）/ 学习（三来源档案 + 当周周计划）。
// 打开情报页时若今日无简报自动补跑一次抓取（设计定稿的触发式补跑，非 scheduler）。

const DIRECTION_LABELS: Record<string, string> = {
  web: "Web", ai: "AI 安全", vehicle: "车联网", reverse: "逆向",
  android: "Android", pwn: "PWN", forensics: "取证",
}

// 模块级（跨卸载存活）：切页时 App 用条件渲染，IntelView 会被整个卸载重建，
// 组件内的 ref/state 全部归零。若把「在抓 / 今日已自动补跑」只放组件里，切回来
// 时会看到 today 仍为 null（简报还没落库）而再触发一次抓取。后端 submit_unique
// 已能复用进行中的 Job，但前端也该续上同一轮而不是重走一遍。
let inflightFetch: Promise<boolean> | null = null
let autoFetchedFor = "" // 已为哪一天自动补跑过（UTC 日期，与后端 overview 口径一致）

async function fetchOnce(): Promise<boolean> {
  try {
    const { job_id } = await api.intelFetch()
    const job = await pollJob(job_id, () => {})
    return job.status === "done"
  } catch { return false /* 端点失败静默，页面数据维持现状 */ }
  finally { inflightFetch = null }
}

function ArticleRow({ a, onToggle }: {
  a: IntelArticle
  onToggle: (id: string, patch: { read?: boolean; starred?: boolean }) => void
}) {
  let hot = false
  try { hot = !!JSON.parse(a.score_detail || "{}").hot } catch { /* 坏 JSON 不显热点 */ }
  return (
    <div className={cn("rounded border p-2 text-xs", a.read ? "opacity-60" : "")}>
      <div className="flex items-center gap-2">
        {Number(a.is_priority) === 1 && <Badge className="bg-(--status-error) text-[9px] text-white">KEV/优先</Badge>}
        {hot && <Badge variant="outline" className="text-[9px] text-(--status-approval)">热点</Badge>}
        {a.direction && (
          <Badge variant="outline" className="text-[9px]">{DIRECTION_LABELS[a.direction] ?? a.direction}</Badge>
        )}
        <span className="font-mono text-[10px] text-muted-foreground">{a.source}</span>
        <span className="font-mono text-[10px] text-muted-foreground">score {a.score}</span>
        <span className="flex-1" />
        <button onClick={() => onToggle(a.id, { starred: !a.starred })}
                title={a.starred ? "取消收藏" : "收藏"}
                className={cn("px-1", a.starred ? "text-(--status-approval)" : "text-muted-foreground hover:text-foreground")}>
          {a.starred ? "★" : "☆"}
        </button>
        <button onClick={() => onToggle(a.id, { read: !a.read })}
                title={a.read ? "标未读" : "标已读"}
                className="px-1 text-muted-foreground hover:text-foreground">
          {a.read ? "↩" : "✓"}
        </button>
      </div>
      <a href={a.url} target="_blank" rel="noreferrer"
         className="mt-1 block font-medium hover:text-primary" onClick={() => !a.read && onToggle(a.id, { read: true })}>
        {a.title}
      </a>
      {a.summary && <p className="mt-0.5 line-clamp-2 text-[10px] text-muted-foreground">{a.summary}</p>}
    </div>
  )
}

export function IntelView() {
  const [overview, setOverview] = useState<IntelOverview | null>(null)
  const [profile, setProfile] = useState<IntelProfile | null>(null)
  const [briefs, setBriefs] = useState<IntelBriefMeta[]>([])
  const [briefDate, setBriefDate] = useState<string>("")
  const [briefMd, setBriefMd] = useState<string>("")
  const [articles, setArticles] = useState<IntelArticle[]>([])
  const [searchQuery, setSearchQuery] = useState("")
  const [searchResults, setSearchResults] = useState<IntelArticle[]>([])
  const [searchCacheKey, setSearchCacheKey] = useState("")
  const [searchTotal, setSearchTotal] = useState(0)
  const [searchOffset, setSearchOffset] = useState(0)
  const [searchHasMore, setSearchHasMore] = useState(false)
  const [searchBusy, setSearchBusy] = useState(false)
  const [searchError, setSearchError] = useState("")
  // 检索降级/过滤的非致命提示（引擎部分不可用、丢弃垃圾条数）。
  // 与 searchError 分开：这里仍有结果可看，只是需要解释为什么少。
  const [searchNotice, setSearchNotice] = useState("")
  const [searchHistory, setSearchHistory] = useState<IntelSearchHistoryItem[]>([])
  const [filter, setFilter] = useState<{ kind?: string; unread?: boolean; starred?: boolean }>({})
  const [refreshing, setRefreshing] = useState(false)
  // E10：学习档案（三来源聚合）+ 周计划（planWeek=""=最新一份）
  const [learning, setLearning] = useState<IntelLearningProfile | null>(null)
  const [plan, setPlan] = useState<IntelPlan | null>(null)
  const [plans, setPlans] = useState<IntelPlanMeta[]>([])
  const [planWeek, setPlanWeek] = useState<string>("")
  const [planBusy, setPlanBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  // 把「为什么结果少」讲清楚：引擎被限流/部分不可用、以及丢弃了多少垃圾页。
  // 不给解释的话，用户只会看到数量变少却不知道是代码问题还是外部限流。
  const buildSearchNotice = (response: {
    backend?: string; errors?: string[]; dropped_spam?: number
  }): string => {
    const parts: string[] = []
    const errors = (response.errors ?? []).filter(Boolean)
    if (errors.length) {
      parts.push(`部分检索不可用：${errors.join("；")}`)
    } else if (response.backend === "searxng+throttled") {
      // 后端明确回报「有扩展词但处于冷却期」，不是猜的
      parts.push("引擎冷却中，本次仅单轮检索；稍后再搜可获得更多结果")
    }
    if (response.dropped_spam && response.dropped_spam > 0) {
      parts.push(`已过滤 ${response.dropped_spam} 条垃圾/不相关内容`)
    }
    return parts.join(" · ")
  }

  const loadOverview = useCallback(() =>
    api.intelOverview().then(setOverview).catch(() => {}), [])

  const loadBrief = useCallback((date: string) => {
    if (!date) return
    api.intelBrief(date).then((b) => setBriefMd(b.content)).catch(() => setBriefMd(""))
  }, [])

  const loadBriefs = useCallback(() =>
    api.intelBriefs().then((r) => {
      setBriefs(r.briefs)
      setBriefDate((cur) => cur || r.briefs[0]?.date || "")
    }).catch(() => {}), [])

  const loadArticles = useCallback(() =>
    api.intelArticles({ ...filter, limit: 100 }).then((r) => setArticles(r.articles)).catch(() => {}),
    [filter])

  const loadProfile = useCallback(() =>
    api.intelProfile().then(setProfile).catch(() => {}), [])

  const loadLearning = useCallback(() =>
    api.intelLearningProfile().then(setLearning).catch(() => {}), [])

  const loadPlans = useCallback(() =>
    api.intelLearningPlans().then((r) => setPlans(r.plans)).catch(() => {}), [])

  const loadPlan = useCallback((week: string) => {
    api.intelLearningPlan(week || undefined).then(setPlan).catch(() => setPlan(null))
  }, [])

  useEffect(() => { loadOverview(); loadBriefs(); loadProfile(); loadLearning(); loadPlans() },
    [loadOverview, loadBriefs, loadProfile, loadLearning, loadPlans])
  useEffect(() => { loadPlan(planWeek) }, [planWeek, loadPlan])
  useEffect(() => { loadArticles() }, [loadArticles])
  useEffect(() => { loadBrief(briefDate) }, [briefDate, loadBrief])
  useEffect(() => {
    let cancelled = false
    const restoreLatestSearch = async () => {
      try {
        const response = await api.intelSearchHistory()
        const items = response.items ?? []
        if (cancelled) return
        setSearchHistory(items)
        const latest = items[0]
        if (!latest) return
        const saved = await api.intelSearchHistoryGet(latest.cache_key, 10, 0)
        if (cancelled) return
        setSearchQuery(saved.query || latest.query)
        setSearchResults(saved.results ?? [])
        setSearchCacheKey(saved.cache_key ?? latest.cache_key)
        setSearchTotal(saved.total ?? latest.total)
        setSearchOffset(saved.results?.length ?? 0)
        setSearchHasMore(Boolean(saved.has_more))
      } catch {
        // A missing or expired history entry must not block the rest of the view.
      }
    }
    void restoreLatestSearch()
    return () => { cancelled = true }
  }, [])

  const refresh = useCallback(async () => {
    // 已有在跑的一轮（含上一个组件实例发起的）→ 只跟随，不重复提交
    const pending = inflightFetch ?? (inflightFetch = fetchOnce())
    setRefreshing(true)
    try { await pending }
    finally {
      setRefreshing(false)
      loadOverview(); loadBriefs(); loadArticles()
    }
  }, [loadOverview, loadBriefs, loadArticles])

  // 触发式补跑（设计定稿）：打开页时今日无简报 → 自动抓一次（仅一次，防循环）。
  // 2026-10-20：autoFetchedFor 记的是「哪一天补过」而非 bool——切页回来组件重建，
  // 若今日抓取还没跑完（简报未落库）today 仍是 null，bool 归零就会再抓一遍。
  useEffect(() => {
    if (!overview || inflightFetch) return
    const today = new Date().toISOString().slice(0, 10)  // UTC，与 /api/intel/overview 同口径
    if (overview.today === null && autoFetchedFor !== today) {
      autoFetchedFor = today
      void refresh()
    }
  }, [overview, refresh])

  // 挂载时有上一实例遗留的抓取在跑 → 按钮维持「抓取中…」，结束后补一次数据刷新
  useEffect(() => {
    if (!inflightFetch) return
    setRefreshing(true)
    const pending = inflightFetch
    void pending.finally(() => {
      setRefreshing(false)
      loadOverview(); loadBriefs(); loadArticles()
    })
  }, [loadOverview, loadBriefs, loadArticles])

  const runSearch = async (refresh = false) => {
    if (!searchQuery.trim() || searchBusy) return
    setSearchBusy(true)
    setSearchError("")
    setSearchNotice("")
    try {
      const response = await api.intelSearch({ q: searchQuery.trim(), limit: 10, refresh })
      setSearchResults(response.results ?? [])
      setSearchCacheKey(response.cache_key ?? "")
      setSearchTotal(response.total ?? response.results?.length ?? 0)
      setSearchOffset(response.results?.length ?? 0)
      setSearchHasMore(Boolean(response.has_more))
      setSearchQuery(response.query || searchQuery.trim())
      setSearchNotice(buildSearchNotice(response))
      api.intelSearchHistory().then((history) => setSearchHistory(history.items ?? [])).catch(() => {})
    } catch (e) { setSearchError(e instanceof Error ? e.message : String(e)) }
    finally { setSearchBusy(false) }
  }

  const restoreSearch = async (item: IntelSearchHistoryItem) => {
    if (searchBusy) return
    setSearchBusy(true)
    setSearchError("")
    // 历史结果是当初检索的快照，不带本次的限流/过滤信息，清掉免得误导。
    setSearchNotice("")
    try {
      const response = await api.intelSearchHistoryGet(item.cache_key, 10, 0)
      setSearchQuery(response.query || item.query)
      setSearchResults(response.results ?? [])
      setSearchCacheKey(response.cache_key ?? item.cache_key)
      setSearchTotal(response.total ?? item.total)
      setSearchOffset(response.results?.length ?? 0)
      setSearchHasMore(Boolean(response.has_more))
    } catch (e) {
      setSearchError(e instanceof Error ? e.message : String(e))
    } finally { setSearchBusy(false) }
  }

  const loadMoreSearch = async () => {
    if (!searchCacheKey || searchBusy || !searchHasMore) return
    setSearchBusy(true)
    try {
      const response = await api.intelSearch({ q: searchQuery.trim(), limit: 10, cache_key: searchCacheKey, offset: searchOffset })
      setSearchResults((current) => [...current, ...(response.results ?? [])])
      setSearchOffset((current) => current + (response.results?.length ?? 0))
      setSearchTotal(response.total ?? searchTotal)
      setSearchHasMore(Boolean(response.has_more))
    } catch (e) { setSearchError(e instanceof Error ? e.message : String(e)) }
    finally { setSearchBusy(false) }
  }

  const openSearchResult = async (event: MouseEvent<HTMLAnchorElement>, article: IntelArticle) => {
    if (event.button !== 0 && event.button !== 1) return
    let source: URL
    try { source = new URL(article.url) } catch { return }
    if (source.hostname !== "news.google.com" || !/^\/(?:rss\/)?articles\/[A-Za-z0-9_-]+\/?$/.test(source.pathname)) return
    event.preventDefault()
    setSearchError("")
    // 桌面壳的 setWindowOpenHandler 对任何 window.open 一律 deny（返回 null），
    // 所以不能靠「先开空白页再改 location」承接异步解析——那会被误报成弹窗拦截。
    // 有原生通道就先解析、再交系统浏览器；纯浏览器环境才回落到新标签页。
    const nativeOpen = window.desktopBrowser?.openExternal
    const target = nativeOpen ? null : window.open("about:blank", "_blank")
    if (!nativeOpen && !target) {
      setSearchError("浏览器阻止了新窗口，请允许本站弹出窗口后重试")
      return
    }
    if (target) target.opener = null
    try {
      const result = await api.intelResolveNewsUrl(article.url)
      const resolved = new URL(result.url)
      if (!["http:", "https:"].includes(resolved.protocol) || resolved.hostname === "news.google.com") {
        throw new Error("Google News 未返回有效的来源文章链接")
      }
      if (nativeOpen) {
        const opened = await nativeOpen(resolved.toString())
        if (!opened) throw new Error("系统浏览器未能打开该地址")
      } else {
        target!.location.replace(resolved.toString())
      }
    } catch (error) {
      target?.close()
      // Google 中转链接在浏览器里本身也打不开（实测显示「无效重定向」），
      // 解析被限流时唯一可靠的出口是 RSS 给出的发布方站点。
      const site = article.source_site?.trim()
      let hint = ""
      if (site) {
        try { hint = new URL(site).hostname } catch { hint = "" }
      }
      const reason = error instanceof Error ? error.message : String(error)
      setSearchError(hint
        ? `${reason}。发布方为 ${article.source || hint}（${hint}），可在其站内搜索标题。`
        : reason)
    }
  }

  const toggle = (id: string, patch: { read?: boolean; starred?: boolean }) => {
    api.intelMarkArticle(id, patch)
      .then(() => { loadArticles(); loadOverview() })
      .catch(() => {})
  }

  // E10：生成/重新生成当周计划（Job 轮询），完成后刷新归档并选中当周
  const generatePlan = useCallback(async () => {
    if (planBusy) return
    setPlanBusy(true)
    try {
      const { job_id, week } = await api.intelLearningPlanGenerate()
      await pollJob(job_id, () => {})
      setPlanWeek(week)
      loadPlans()
    } catch { /* 失败静默，按钮恢复可再试 */ }
    finally { setPlanBusy(false) }
  }, [planBusy, loadPlans])

  const copyPlan = () => {
    if (!plan) return
    navigator.clipboard.writeText(plan.content)
      .then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) })
      .catch(() => {})
  }

  return (
    <div className="flex h-full min-h-0 flex-col pt-9">
      <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
        <h2 className="text-sm font-semibold">情报</h2>
        {overview && (
          <span className="font-mono text-[10px] text-muted-foreground">
            文章池 {overview.counts.articles} · 未读 {overview.counts.unread} · 收藏 {overview.counts.starred}
          </span>
        )}
        {profile?.stage && (
          <Badge variant="outline" className="text-[10px]">阶段：{profile.stage}</Badge>
        )}
        <span className="flex-1" />
        <Button size="sm" onClick={refresh} disabled={refreshing}>
          {refreshing ? "抓取中…" : "刷新"}
        </Button>
      </div>
      <Tabs defaultValue="briefs" className="min-h-0 flex-1 flex-col gap-0">
        <TabsList className="w-full shrink-0 justify-start rounded-none border-b bg-transparent p-0">
          {([["briefs", "简报"], ["articles", "文章"], ["search", "检索"], ["learning", "学习"]] as const).map(([k, label]) => (
            <TabsTrigger key={k} value={k} className="rounded-none border-b-2 px-3 py-1.5 text-xs">{label}</TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="briefs" className="min-h-0 flex-1">
          <div className="flex h-full min-h-0">
            <div className="w-40 shrink-0 overflow-y-auto border-r p-2">
              {briefs.length === 0 && <p className="p-1 text-[10px] text-muted-foreground">暂无简报，点「刷新」抓取</p>}
              {briefs.map((b) => (
                <button key={b.date} onClick={() => setBriefDate(b.date)}
                        className={cn("block w-full rounded px-2 py-1 text-left font-mono text-xs",
                                      b.date === briefDate ? "bg-secondary text-primary" : "hover:bg-accent/40")}>
                  {b.date}
                </button>
              ))}
            </div>
            <div className="min-w-0 flex-1 overflow-y-auto p-4">
              {briefMd
                ? <MarkdownView content={briefMd} prefix="intel" />
                : <p className="text-xs text-muted-foreground">选择左侧日期查看简报</p>}
            </div>
          </div>
        </TabsContent>
        <TabsContent value="articles" className="min-h-0 flex-1 overflow-y-auto">
          <div className="flex gap-1 p-2">
            {([["", "全部"], ["cve", "漏洞"], ["article", "文章"]] as const).map(([k, label]) => (
              <Button key={k || "all"} size="sm" variant={filter.kind === k || (!filter.kind && !k) ? "default" : "outline"}
                      className="h-6 px-2 text-[10px]"
                      onClick={() => setFilter((f) => ({ ...f, kind: k || undefined }))}>{label}</Button>
            ))}
            <span className="w-2" />
            {([["unread", "未读"], ["starred", "收藏"]] as const).map(([k, label]) => (
              <Button key={k} size="sm" variant={filter[k] ? "default" : "outline"}
                      className="h-6 px-2 text-[10px]"
                      onClick={() => setFilter((f) => ({ ...f, [k]: !f[k] }))}>{label}</Button>
            ))}
          </div>
          <div className="space-y-1.5 px-2 pb-3">
            {articles.length === 0 && (
              <p className="p-2 text-xs text-muted-foreground">无匹配内容——点「刷新」抓取或调整筛选</p>
            )}
            {articles.map((a) => <ArticleRow key={a.id} a={a} onToggle={toggle} />)}
          </div>
        </TabsContent>
        <TabsContent value="search" className="min-h-0 flex-1 overflow-y-auto p-3">
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void runSearch(false) }}>
            <input value={searchQuery} onChange={(e) => setSearchQuery(e.target.value)} placeholder="输入关键词或语句" className="h-8 min-w-0 flex-1 rounded border bg-background px-2 text-xs" />
            <Button size="sm" type="submit" disabled={searchBusy}>{searchBusy ? "检索中…" : "检索"}</Button>
            <Button size="sm" type="button" variant="outline" disabled={searchBusy || !searchQuery.trim()} onClick={() => void runSearch(true)}>联网重查</Button>
          </form>
          {searchError && <p className="mt-2 text-xs text-(--status-error)">{searchError}</p>}
          {searchHistory.length > 0 && <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-muted-foreground">最近搜索</span>
            {searchHistory.map((item) => <Button key={item.cache_key} type="button" size="sm" variant="outline"
              className="h-6 max-w-56 truncate px-2 text-[10px]" disabled={searchBusy}
              title={`${item.query} · ${item.total} 条 · ${fmtDateTimeMin(item.created_at)}`}
              onClick={() => void restoreSearch(item)}>{item.query} ({item.total})</Button>)}
          </div>}
          <div className="mt-2 space-y-1.5">
            {searchResults.map((a, i) => (
              <div key={`${a.url}-${i}`} className="rounded border p-2 text-xs">
                <div className="flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground"><span>{a.source || a.via || "未知来源"}</span><span>{a.published_day || a.published_at || (a.date_known === false ? "发布日期未提供 · 搜索结果位于近半年查询窗" : "日期未知")}</span><span>综合 {a.ranking_score ?? "—"}</span><span>内容 {a.score ?? "—"}</span></div>
                <a href={a.url} target="_blank" rel="noreferrer" onClick={(event) => void openSearchResult(event, a)} className="mt-1 block font-medium hover:text-primary">{a.title}</a>
                {a.summary && <p className="mt-0.5 line-clamp-2 text-[10px] text-muted-foreground">{a.summary}</p>}
              </div>
            ))}
            {!searchResults.length && <p className="text-xs text-muted-foreground">输入关键词后开始检索</p>}
            {searchResults.length > 0 && <p className="text-[10px] text-muted-foreground">已显示 {searchResults.length} / {searchTotal} 条 · 近半年 · {(() => {
              const via = searchResults[0]?.via
              if (via === "google_news") return "Google News"
              if (via === "searxng") return "SearXNG"
              if (via === "searxng+expand") return "SearXNG（多角度扩展）"
              if (via === "searxng+throttled") return "SearXNG（引擎冷却）"
              return "本地与联网结果"
            })()}</p>}
            {searchNotice && <p className="text-[10px] text-amber-600 dark:text-amber-500">{searchNotice}</p>}
          </div>
          {searchHasMore && <Button size="sm" variant="outline" className="mt-2" disabled={searchBusy} onClick={() => void loadMoreSearch()}>{searchBusy ? "加载中…" : "加载更多"}</Button>}
        </TabsContent>
        <TabsContent value="learning" className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="space-y-4 text-xs">
            {profile && (
              <div>
                <p className="mb-1 text-[10px] text-muted-foreground">① 声明画像（权重与阶段在设置页「情报源」调整）</p>
                <div className="flex flex-wrap items-center gap-1">
                  {Object.entries(profile.directions).map(([d, w]) => (
                    <Badge key={d} variant="outline" className="text-[10px]">
                      {DIRECTION_LABELS[d] ?? d} ×{w}
                    </Badge>
                  ))}
                  {profile.stage && <Badge variant="outline" className="text-[10px]">阶段：{profile.stage}</Badge>}
                </div>
              </div>
            )}
            {learning && (
              <>
                <div>
                  <p className="mb-1 text-[10px] text-muted-foreground">② Obsidian vault 推断（只读元数据，笔记正文不出本机）</p>
                  {learning.vault.total === 0 ? (
                    <p className="text-[11px] text-muted-foreground">
                      尚未配置 vault——到设置页「情报源」填入 Obsidian 库路径并索引。
                    </p>
                  ) : (
                    <>
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(learning.vault.by_direction).filter(([, v]) => v.notes > 0).map(([d, v]) => (
                          <Badge key={d} variant="outline" className="text-[10px]" title={`最近活跃 ${v.last_active ? fmtDateTimeMin(v.last_active) : "—"}`}>
                            {DIRECTION_LABELS[d] ?? d} {v.notes} 篇
                          </Badge>
                        ))}
                        <span className="font-mono text-[10px] text-muted-foreground">共 {learning.vault.total} 篇</span>
                      </div>
                      {(learning.vault.top_tags ?? []).length > 0 && (
                        <div className="mt-1.5 flex flex-wrap items-center gap-1">
                          <span className="text-[10px] text-muted-foreground">高频主题：</span>
                          {(learning.vault.top_tags ?? []).map((t) => (
                            <Badge key={t.tag} variant="outline" className="text-[10px] text-muted-foreground">
                              {t.tag}×{t.count}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
                <div>
                  <p className="mb-1 text-[10px] text-muted-foreground">③ 平台学习记录（文章池按方向的已读 / 收藏）</p>
                  <div className="flex flex-wrap gap-1">
                    {Object.entries(learning.platform).filter(([, v]) => v.total > 0).map(([d, v]) => (
                      <Badge key={d} variant="outline" className="text-[10px]">
                        {DIRECTION_LABELS[d] ?? d} 已读 {v.read} / 收藏 {v.starred}
                      </Badge>
                    ))}
                    {Object.keys(learning.platform).length === 0 && (
                      <span className="text-[11px] text-muted-foreground">暂无打分记录——去「文章」tab 阅读收藏。</span>
                    )}
                  </div>
                </div>
              </>
            )}
            <div className="border-t pt-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[10px] text-muted-foreground">
                  当周学习计划{plan?.week ? `（${plan.week} 起）` : ""}
                </span>
                <span className="flex-1" />
                <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]" disabled={!plan || copied} onClick={copyPlan}>
                  {copied ? "已复制 ✓" : "复制 md"}
                </Button>
                <Button size="sm" className="h-6 px-2 text-[10px]" onClick={generatePlan} disabled={planBusy}>
                  {planBusy ? "生成中…" : plan ? "重新生成" : "生成本周计划"}
                </Button>
              </div>
              {plans.length > 1 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {plans.map((p) => (
                    <button key={p.week} onClick={() => setPlanWeek(p.week)}
                            className={cn("rounded px-1.5 py-0.5 font-mono text-[10px]",
                                          p.week === planWeek ? "bg-secondary text-primary" : "text-muted-foreground hover:bg-accent/40")}>
                      {p.week}
                    </button>
                  ))}
                </div>
              )}
              <div className="mt-2">
                {plan
                  ? <MarkdownView content={plan.content} prefix="plan" />
                  : <p className="text-[11px] text-muted-foreground">
                      暂无周计划——点「生成本周计划」，由画像 + 当周简报 + 高分文章合成（classifier 缺席降级模板）。
                    </p>}
              </div>
            </div>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  )
}
