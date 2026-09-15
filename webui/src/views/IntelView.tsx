import { useCallback, useEffect, useRef, useState } from "react"
import { api, pollJob } from "@/lib/api"
import type { IntelArticle, IntelBriefMeta, IntelOverview, IntelProfile } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"

// 情报面板（E9，DESIGN.md §16.4）：全局模块，与项目无关。
// 三 tab = 简报（每日中文简报 + 归档）/ 文章（高分文章池）/ 学习（已读收藏 = E10 档案素材）。
// 打开情报页时若今日无简报自动补跑一次抓取（设计定稿的触发式补跑，非 scheduler）。

const DIRECTION_LABELS: Record<string, string> = {
  web: "Web", ai: "AI 安全", vehicle: "车联网", reverse: "逆向",
  android: "Android", pwn: "PWN", forensics: "取证",
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
        {Number(a.is_priority) === 1 && <Badge className="bg-[--status-error] text-[9px] text-white">KEV/优先</Badge>}
        {hot && <Badge variant="outline" className="text-[9px] text-[--status-approval]">热点</Badge>}
        {a.direction && (
          <Badge variant="outline" className="text-[9px]">{DIRECTION_LABELS[a.direction] ?? a.direction}</Badge>
        )}
        <span className="font-mono text-[10px] text-muted-foreground">{a.source}</span>
        <span className="font-mono text-[10px] text-muted-foreground">score {a.score}</span>
        <span className="flex-1" />
        <button onClick={() => onToggle(a.id, { starred: !a.starred })}
                title={a.starred ? "取消收藏" : "收藏"}
                className={cn("px-1", a.starred ? "text-[--status-approval]" : "text-muted-foreground hover:text-foreground")}>
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
  const [filter, setFilter] = useState<{ kind?: string; unread?: boolean; starred?: boolean }>({})
  const [refreshing, setRefreshing] = useState(false)
  const autoFetched = useRef(false)

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

  useEffect(() => { loadOverview(); loadBriefs(); loadProfile() },
    [loadOverview, loadBriefs, loadProfile])
  useEffect(() => { loadArticles() }, [loadArticles])
  useEffect(() => { loadBrief(briefDate) }, [briefDate, loadBrief])

  const refresh = useCallback(async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      const { job_id } = await api.intelFetch()
      await pollJob(job_id, () => {})
      loadOverview(); loadBriefs(); loadArticles()
    } catch { /* 端点失败静默，页面数据维持现状 */ }
    finally { setRefreshing(false) }
  }, [refreshing, loadOverview, loadBriefs, loadArticles])

  // 触发式补跑（设计定稿）：打开页时今日无简报 → 自动抓一次（仅一次，防循环）
  useEffect(() => {
    if (autoFetched.current || !overview) return
    autoFetched.current = true
    if (overview.today === null) {
      const t = setTimeout(refresh, 0)  // 延后一拍，避免 effect 内同步 setState
      return () => clearTimeout(t)
    }
  }, [overview, refresh])

  const toggle = (id: string, patch: { read?: boolean; starred?: boolean }) => {
    api.intelMarkArticle(id, patch)
      .then(() => { loadArticles(); loadOverview() })
      .catch(() => {})
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
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
          {([["briefs", "简报"], ["articles", "文章"], ["learning", "学习"]] as const).map(([k, label]) => (
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
        <TabsContent value="learning" className="min-h-0 flex-1 overflow-y-auto p-4">
          {profile && (
            <div className="space-y-3 text-xs">
              <div>
                <p className="mb-1 text-[10px] text-muted-foreground">兴趣方向画像（权重在设置页「情报源」调整）</p>
                <div className="flex flex-wrap gap-1">
                  {Object.entries(profile.directions).map(([d, w]) => (
                    <Badge key={d} variant="outline" className="text-[10px]">
                      {DIRECTION_LABELS[d] ?? d} ×{w}
                    </Badge>
                  ))}
                </div>
              </div>
              <div>
                <p className="mb-1 text-[10px] text-muted-foreground">学习素材（已读 / 收藏文章，E10 档案与周计划的数据源）</p>
                {overview && (
                  <p className="font-mono text-[11px]">
                    已读 {overview.counts.articles - overview.counts.unread} · 收藏 {overview.counts.starred}
                  </p>
                )}
              </div>
              <p className="text-[10px] text-muted-foreground">
                Obsidian vault 接入与 LLM 周学习计划为 E10（§16.3），本 tab 届时扩展。
              </p>
            </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  )
}
