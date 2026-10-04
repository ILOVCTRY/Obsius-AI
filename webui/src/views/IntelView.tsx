import { useCallback, useEffect, useRef, useState } from "react"
import { api, pollJob } from "@/lib/api"
import type {
  IntelArticle, IntelBriefMeta, IntelLearningProfile, IntelOverview,
  IntelPlan, IntelPlanMeta, IntelProfile,
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
  const [filter, setFilter] = useState<{ kind?: string; unread?: boolean; starred?: boolean }>({})
  const [refreshing, setRefreshing] = useState(false)
  // E10：学习档案（三来源聚合）+ 周计划（planWeek=""=最新一份）
  const [learning, setLearning] = useState<IntelLearningProfile | null>(null)
  const [plan, setPlan] = useState<IntelPlan | null>(null)
  const [plans, setPlans] = useState<IntelPlanMeta[]>([])
  const [planWeek, setPlanWeek] = useState<string>("")
  const [planBusy, setPlanBusy] = useState(false)
  const [copied, setCopied] = useState(false)
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
