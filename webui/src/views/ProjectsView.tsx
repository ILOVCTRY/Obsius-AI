import { useCallback, useEffect, useMemo, useState } from "react"
import { ArrowRight, Layers3, RotateCcw, Search, Trash2, Users } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import type { Expert, IntelOverview, ProjectMeta } from "@/lib/types"
import { bindingBadge, trackLabel, type Taxonomy } from "@/lib/taxonomy"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"
import { fmtDate, fmtDateTime, utcTitle } from "@/lib/datetime"

// 页面 1（DESIGN.md §12）：项目列表 + 创建 + 删除（回收站式，DESIGN.md §5.3）
// 项目绑定由场景轨直接决定：创建时自动加载该轨全部可用专家；知识继承
// （M4b）仍可复用同源项目资产。

/** 回收站目录名 = `<slug>-<UTC紧凑时间戳>`；取尾部时间戳用于展示删除时间 */
const trashTime = (name?: string): string | undefined => {
  const m = /-(\d{8}T\d{6}Z)$/.exec(name ?? "")
  return m ? m[1] : undefined
}

export function ProjectsView({ onOpen }: { onOpen: (pid: string) => void }) {
  const [projects, setProjects] = useState<ProjectMeta[]>([])
  const [tax, setTax] = useState<Taxonomy | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState("")
  const [track, setTrack] = useState<string>("ctf")
  const [pool, setPool] = useState<Expert[]>([])
  const [inheritFrom, setInheritFrom] = useState<string | null>(null)
  const [inheritOpen, setInheritOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [confirmTarget, setConfirmTarget] = useState<ProjectMeta | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [delError, setDelError] = useState<string | null>(null)
  const [query, setQuery] = useState("")   // 项目名称搜索过滤（2026-10-11）
  const [trashed, setTrashed] = useState<ProjectMeta[]>([])   // 回收站（搜索命中时展示，2026-10-11）
  const [trashBusy, setTrashBusy] = useState<string | null>(null)   // 正在恢复的 trash_name

  const refresh = useCallback(() => {
    api.listProjects().then(setProjects).catch((e) => setError(String(e)))
    api.listTrashedProjects().then(setTrashed).catch(() => {})   // 回收站静默——仅搜索时展示
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => {
    api.taxonomy().then((t) => {
      setTax(t)
      setTrack(t.tracks[0]?.name ?? "ctf")
    }).catch(() => {})
    api.listExperts().then((es) => setPool(es.filter((e) => e.kind !== "virtual"))).catch(() => {})
  }, [])

  const trackExperts = pool.filter((e) => !e.tracks || e.tracks.length === 0 || e.tracks.includes(track))

  const create = async () => {
    if (!name.trim()) return
    setCreating(true)
    setError(null)
    try {
      const p = await api.createProject(name.trim(), track,
        trackExperts.length ? trackExperts.map((expert) => expert.id) : [],
        { inherit_from: inheritFrom })
      setName("")
      onOpen(p.id)
    } catch (e) {
      setError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const del = async () => {
    if (!confirmTarget) return
    setDeleting(true)
    try {
      await api.deleteProject(confirmTarget.id)
      setConfirmTarget(null)
      refresh()
    } catch (e) {
      // 409 运行中 / 422 目录占用：保持对话框打开，原因显示在描述下方
      setError(null)
      setDelError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(false)
    }
  }

  const capabilityNames = Array.from(new Set(trackExperts.flatMap((expert) => expert.skills ?? [])))

  // 名称/ID 过滤（大小写不敏感）；空白查询=全量（不改变原顺序）
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return projects
    return projects.filter((p) =>
      p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q))
  }, [projects, query])

  // 回收站命中（2026-10-11）：仅在有查询时列出——首页搜索也能搜到已删项目并直接恢复
  const trashedShown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return []
    return trashed.filter((p) =>
      p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q))
  }, [trashed, query])

  const restore = async (p: ProjectMeta) => {
    if (!p.trash_name) return
    setTrashBusy(p.trash_name)
    setError(null)
    try {
      await api.restoreProject(p.trash_name)
      refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setTrashBusy(null)
    }
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto bg-[radial-gradient(circle_at_top_right,oklch(var(--brand-lch)/0.16),transparent_42%),linear-gradient(135deg,var(--surface-1),var(--surface-0))] pt-9">
      <div className="mx-auto grid w-full max-w-7xl gap-8 p-6 lg:grid-cols-[minmax(0,1fr)_21rem] lg:gap-10 lg:p-10">
        <div className="space-y-7">
          <header className="max-w-2xl space-y-3">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-cyan-300/75"><span className="status-dot" />Mission control</div>
            <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">从一个清晰的目标开始。</h1>
            <p className="max-w-xl text-sm leading-6 text-muted-foreground">创建一个隔离的安全研究空间。选择场景方向后，系统会自动装配完整专家池与对应能力，不需要手动编排。</p>
          </header>

          <Card className="overflow-hidden border-cyan-300/20 bg-white/[0.04] shadow-2xl shadow-cyan-950/20">
            <CardHeader className="border-b border-white/10 pb-5">
              <div className="flex items-center justify-between gap-3">
                <div><CardTitle className="text-base">新建项目</CardTitle><p className="mt-1 text-xs text-muted-foreground">配置项目上下文，剩余工作交给自动编排。</p></div>
                <Layers3 className="text-cyan-300/80" size={20} />
              </div>
            </CardHeader>
            <CardContent className="space-y-6 p-5">
              <div className="space-y-2"><label className="text-xs font-medium text-muted-foreground">项目名称</label><Input placeholder="例如：支付系统安全评估" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} /></div>
              <div className="space-y-2"><label className="text-xs font-medium text-muted-foreground">场景方向</label><div className="grid gap-2 sm:grid-cols-3">{(tax?.tracks ?? []).map((t) => <button key={t.name} onClick={() => setTrack(t.name)} title={t.description} className={cn("rounded-lg border px-3 py-3 text-left transition", track === t.name ? "border-cyan-300/60 bg-cyan-300/10 text-cyan-100" : "border-white/10 text-muted-foreground hover:border-white/25 hover:bg-white/[0.04]")}><span className="block text-sm font-medium">{t.label || trackLabel(t.name)}</span><span className="mt-1 block line-clamp-2 text-[11px] opacity-65">{t.description || "自动匹配研究能力"}</span></button>)}</div></div>
              <div className="rounded-xl border border-cyan-300/15 bg-cyan-300/[0.05] p-4"><div className="flex items-start gap-3"><Users size={18} className="mt-0.5 shrink-0 text-cyan-300" /><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium">当前场景专家</span><Badge variant="outline" className="border-cyan-300/25 text-cyan-200">{trackExperts.length} 位可用</Badge></div><p className="mt-1 text-xs leading-5 text-muted-foreground">创建项目时自动加载该场景方向的全部专家，无需手动组队。</p><div className="mt-3 flex flex-wrap gap-1.5">{capabilityNames.slice(0, 12).map((skill) => <span key={skill} className="rounded bg-black/20 px-2 py-1 text-[10px] text-cyan-100/75">{skill}</span>)}{capabilityNames.length > 12 && <span className="px-1 py-1 text-[10px] text-muted-foreground">+{capabilityNames.length - 12} 能力</span>}{trackExperts.length === 0 && <span className="text-xs text-muted-foreground">专家池加载中…</span>}</div></div></div></div>
              <div className="space-y-3"><button className="text-xs text-muted-foreground transition hover:text-foreground" onClick={() => setInheritOpen((v) => !v)}>{inheritOpen ? "▾" : "▸"} 知识继承{inheritFrom ? "（已选源）" : ""}</button>{inheritOpen && <div className="flex flex-wrap items-center gap-3 rounded-lg border border-white/10 bg-black/10 p-3"><span className="text-xs text-muted-foreground">从既有项目继承资产、函数库与蓝图</span><select value={inheritFrom ?? ""} onChange={(e) => setInheritFrom(e.target.value || null)} className="rounded border border-white/10 bg-background px-2 py-1.5 text-xs [&>option]:bg-popover [&>option]:text-popover-foreground"><option value="">不继承</option>{projects.filter((p) => p.id).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>}</div>
              <Button className="w-full justify-between bg-cyan-300 text-slate-950 hover:bg-cyan-200" onClick={create} disabled={creating || !name.trim()}>{creating ? "创建中…" : "创建项目"}<ArrowRight size={16} /></Button>
            </CardContent>
          </Card>

      {/* 搜索过滤（2026-10-11）：位于「创建项目」按钮下方、项目列表之上 */}
      <div className="relative">
        <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索项目名称或 ID"
          aria-label="搜索项目"
          className="pl-9 pr-9"
        />
        {query && (
          <button
            type="button"
            aria-label="清除搜索"
            onClick={() => setQuery("")}
            className="absolute right-2 top-1/2 -translate-y-1/2 rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:text-foreground"
          >
            ✕
          </button>
        )}
      </div>

      {error && <p className="text-sm text-(--status-error)">{error}</p>}

      <div className="space-y-2">
        {projects.length === 0 && !error && (
          <p className="py-8 text-center text-sm text-muted-foreground">还没有项目——上面创建一个</p>
        )}
        {projects.length > 0 && shown.length === 0 && trashedShown.length === 0 && (
          <p className="py-8 text-center text-sm text-muted-foreground">
            没有匹配「{query.trim()}」的项目
          </p>
        )}
        {shown.map((p) => (
          <div
            key={p.id}
            role="button"
            tabIndex={0}
            onClick={() => onOpen(p.id)}
            onKeyDown={(e) => e.key === "Enter" && onOpen(p.id)}
            className={cn(
              "group flex w-full cursor-pointer items-center gap-3 rounded-lg border p-3 text-left",
              "hover:border-(--ring) hover:bg-accent/50",
            )}
          >
            <Badge variant="outline" className="font-mono">{bindingBadge(p.track, p.experts)}</Badge>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium">{p.name}</div>
              <div className="font-mono text-xs text-muted-foreground">{p.id}</div>
            </div>
            <span className="text-xs text-muted-foreground" title={utcTitle(p.created_at)}>{fmtDate(p.created_at)}</span>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`删除 ${p.name}`}
              className="opacity-0 transition-opacity group-hover:opacity-100 text-(--status-error) hover:text-(--status-error)"
              onClick={(e) => {
                e.stopPropagation()
                setDelError(null)
                setConfirmTarget(p)
              }}
            >
              <Trash2 />
            </Button>
          </div>
        ))}

        {/* 回收站命中（2026-10-11）：仅搜索时展示——同名/同 ID 的已删项目可一键恢复 */}
        {trashedShown.length > 0 && (
          <div className="space-y-2 pt-4">
            <p className="text-xs uppercase tracking-[0.18em] text-muted-foreground">
              回收站中的项目 · {trashedShown.length}
            </p>
            {trashedShown.map((p) => (
              <div
                key={p.trash_name ?? p.id}
                className={cn(
                  "group flex w-full items-center gap-3 rounded-lg border border-dashed p-3 text-left",
                  "opacity-80 hover:border-(--ring) hover:bg-accent/50",
                )}
              >
                <Badge variant="outline" className="font-mono">{bindingBadge(p.track, p.experts)}</Badge>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{p.name}</div>
                  <div className="font-mono text-xs text-muted-foreground">{p.id}</div>
                </div>
                {trashTime(p.trash_name) && (
                  <span className="text-xs text-muted-foreground">
                    删除于 {fmtDateTime(trashTime(p.trash_name))}
                  </span>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  disabled={trashBusy === p.trash_name}
                  onClick={() => void restore(p)}
                >
                  <RotateCcw size={14} />{trashBusy === p.trash_name ? "恢复中…" : "恢复"}
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      <AlertDialog
        open={confirmTarget !== null}
        onOpenChange={(open) => !open && setConfirmTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除项目「{confirmTarget?.name}」？</AlertDialogTitle>
            <AlertDialogDescription>
              整个项目目录将移入回收站（workspaces/.trash/），数据不会立即丢失，可手动移回恢复；运行中的项目无法删除。
            </AlertDialogDescription>
            {delError && <p className="text-sm text-(--status-error)">{delError}</p>}
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <Button variant="outline" size="sm">取消</Button>
            </AlertDialogCancel>
            {/* preventDefault 拦下 Radix 的点击即关弹——否则失败原因写进 delError 时弹窗已关，
                用户只见「关了没删」无任何反馈；成功由 del 内 setConfirmTarget(null) 收口 */}
            <AlertDialogAction asChild>
              <Button variant="destructive" size="sm"
                      onClick={(e) => { e.preventDefault(); del() }} disabled={deleting}>
                {deleting ? "删除中…" : "删除"}
              </Button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      </div>
      <IntelBriefCard />
      </div>
    </div>
  )
}

// 今日简报摘要卡（E9，§16.4）：项目列表右栏；无简报时显示引导抓取。
function IntelBriefCard() {
  const [overview, setOverview] = useState<IntelOverview | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() =>
    api.intelOverview().then(setOverview).catch(() => {}), [])
  useEffect(() => { load() }, [load])

  const fetchNow = async () => {
    if (busy) return
    setBusy(true)
    try {
      const { job_id } = await api.intelFetch()
      await pollJob(job_id, () => {})
      load()
    } catch { /* 静默，卡片维持现状 */ }
    finally { setBusy(false) }
  }

  // 摘要 = 简报 markdown 的前几条要点（剥 md 标记）
  const bullets = (overview?.today?.content ?? "")
    .split("\n").filter((l) => l.trim().startsWith("- "))
    .map((l) => l.replace(/^-\s*/, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1"))
    .slice(0, 4)

  return (
    <aside className="w-72 shrink-0 space-y-2 p-4 lg:justify-self-end">
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold">今日简报</h2>
        <span className="flex-1" />
        <Button size="sm" variant="outline" className="h-6 px-2 text-[10px]"
                onClick={fetchNow} disabled={busy}>
          {busy ? "抓取中…" : "刷新情报"}
        </Button>
      </div>
      {!overview || !overview.today ? (
        <p className="text-xs text-muted-foreground">
          今日暂无简报。点「刷新情报」抓取漏洞源与社区热点，自动合成中文简报。
        </p>
      ) : (
        <>
          <ul className="space-y-1.5 text-xs">
            {bullets.map((b, i) => (
              <li key={i} className="line-clamp-2 text-muted-foreground">· {b}</li>
            ))}
            {bullets.length === 0 && <li className="text-muted-foreground">（简报为空）</li>}
          </ul>
          <Button size="sm" variant="ghost" className="h-6 px-2 text-[10px]"
                  onClick={() => window.dispatchEvent(new Event("goto-intel"))}>
            查看全部 →
          </Button>
        </>
      )}
    </aside>
  )
}
