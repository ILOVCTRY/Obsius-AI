import { useCallback, useEffect, useState } from "react"
import { Trash2 } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import type { Expert, IntelOverview, ProjectMeta, TrackProfile } from "@/lib/types"
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
import { fmtDate, utcTitle } from "@/lib/datetime"

// 页面 1（DESIGN.md §12）：项目列表 + 创建 + 删除（回收站式，DESIGN.md §5.3）
// 项目绑定 = 场景轨（单选）× 专家组队（expert-pool M3：空=按轨全池存量直通）；
// 场景档（M4a）预填组队/看板视图，知识继承（M4b）复用同源项目资产。

const expertName = (e: Expert) => e.name || e.id

export function ProjectsView({ onOpen }: { onOpen: (pid: string) => void }) {
  const [projects, setProjects] = useState<ProjectMeta[]>([])
  const [tax, setTax] = useState<Taxonomy | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState("")
  const [track, setTrack] = useState<string>("ctf")
  const [pool, setPool] = useState<Expert[]>([])
  const [selected, setSelected] = useState<string[]>(["_generalist"])
  const [expertSearch, setExpertSearch] = useState("")
  const [profiles, setProfiles] = useState<TrackProfile[]>([])
  const [profile, setProfile] = useState<string | null>(null)
  const [inheritFrom, setInheritFrom] = useState<string | null>(null)
  const [inheritOpen, setInheritOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [confirmTarget, setConfirmTarget] = useState<ProjectMeta | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [delError, setDelError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.listProjects().then(setProjects).catch((e) => setError(String(e)))
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => {
    api.taxonomy().then((t) => {
      setTax(t)
      setTrack(t.tracks[0]?.name ?? "ctf")
    }).catch(() => {})
    // M3 virtual（编排器单例）不进组队多选
    api.listExperts().then((es) => setPool(es.filter((e) => e.kind !== "virtual"))).catch(() => {})
  }, [])

  // 专家是否服务本轨：tracks 缺省/null=全轨
  const serves = (e: Expert, t: string) => !e.tracks || e.tracks.length === 0 || e.tracks.includes(t)

  // 切轨：清场景档；组队剔除轨外专家，全剔则回退 _generalist（后端 422 前的前端兜底）
  const pickTrack = (t: string) => {
    setTrack(t)
    setProfile(null)
    setProfiles([]) // 先清再拉，防旧轨档残留闪现
    let alive = true
    api.trackProfiles(t).then((ps) => { if (alive) setProfiles(ps) }).catch(() => {})
    return () => { alive = false }
  }
  useEffect(() => pickTrack(track), [track]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setSelected((sel) => {
      const kept = sel.filter((id) => pool.find((e) => e.id === id && serves(e, track)))
      return kept.length > 0 ? kept : ["_generalist"]
    })
  }, [track, pool]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggleExpert = (id: string) =>
    setSelected((sel) => sel.includes(id) ? sel.filter((x) => x !== id) : [...sel, id])

  // 场景档：预填组队（显式勾选优先于档缺省，勾后仍可微调）
  const pickProfile = (p: TrackProfile) => {
    if (profile === p.id) { setProfile(null); return }
    setProfile(p.id)
    if (p.experts?.length) setSelected(p.experts.filter((id) => id === "_generalist" || pool.find((e) => e.id === id && serves(e, track))))
  }

  const create = async () => {
    if (!name.trim()) return
    setCreating(true)
    setError(null)
    try {
      const p = await api.createProject(name.trim(), track, selected,
        { profile, inherit_from: inheritFrom })
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

  return (
    <div className="flex min-h-screen">
      <div className="mx-auto w-full max-w-3xl space-y-4 p-6">
      <div>
        <h1 className="text-lg font-semibold">项目</h1>
        <p className="text-sm text-muted-foreground">项目 = 场景轨（单选）× 专家组队（不选=按轨全池）；能力面由专家技能自动推导，数据随 workspaces/ 项目目录隔离</p>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">新建项目</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              placeholder="项目名（可用中文）"
              className="w-64"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && create()}
            />
            <Button size="sm" onClick={create} disabled={creating || !name.trim()}>
              创建
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-muted-foreground">场景轨</span>
            {(tax?.tracks ?? []).map((t) => (
              <Button
                key={t.name}
                size="sm"
                variant={track === t.name ? "default" : "outline"}
                onClick={() => setTrack(t.name)}
                title={t.description}
              >
                {t.label || trackLabel(t.name)}
              </Button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-muted-foreground">场景档</span>
            {profiles.map((p) => (
              <button
                key={p.id}
                onClick={() => pickProfile(p)}
                title={p.description ?? p.id}
                className={cn(
                  "rounded-md border px-2 py-0.5 text-xs",
                  profile === p.id
                    ? "border-primary/50 bg-primary/10 text-primary"
                    : "text-muted-foreground hover:bg-accent/50",
                )}
              >
                {p.name || p.id}
              </button>
            ))}
            {profiles.length === 0 && (
              <span className="text-[10px] text-muted-foreground">（本轨无内置档，手选组队）</span>
            )}
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-muted-foreground">专家组队</span>
              <Input
                placeholder="搜索专家…"
                className="h-6 w-40 text-[11px]"
                value={expertSearch}
                onChange={(e) => setExpertSearch(e.target.value)}
              />
              {selected.length === 0 && (
                <span className="text-[10px] text-(--status-approval)">未选专家：按轨全池直通</span>
              )}
            </div>
            {(["通用", "轨专属"] as const).map((group) => {
              const members = pool
                .filter((e) => group === "通用"
                  ? !e.tracks || e.tracks.length === 0
                  : (e.tracks?.length ?? 0) > 0 && e.tracks!.includes(track))
                .filter((e) => {
                  const q = expertSearch.trim().toLowerCase()
                  if (!q) return true
                  return e.id.toLowerCase().includes(q)
                    || (e.name ?? "").toLowerCase().includes(q)
                    || (e.description ?? "").toLowerCase().includes(q)
                })
              if (members.length === 0) return null
              return (
                <div key={group} className="flex items-start gap-1.5">
                  <span className="mt-0.5 w-10 shrink-0 text-[10px] text-muted-foreground">{group}</span>
                  {/* 等宽网格（2026-09-21）：chips 定宽列对齐，不再随内容长短参差 */}
                  <div className="grid min-w-0 flex-1 grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-1.5">
                    {members.map((e) => {
                      const on = selected.includes(e.id)
                      const persona = (e.persona ?? e.description ?? "").split("\n")[0]
                      return (
                        <button
                          key={e.id}
                          onClick={() => toggleExpert(e.id)}
                          title={`${e.id}${persona ? `：${persona}` : ""}`}
                          className={cn(
                            "min-w-0 rounded-md border px-2 py-0.5 text-left text-xs",
                            on
                              ? "border-primary/50 bg-primary/10 text-primary"
                              : "text-muted-foreground hover:bg-accent/50",
                          )}
                        >
                          <span className="block truncate">{on ? "✓ " : ""}{expertName(e)}</span>
                          {persona && (
                            <span className="block truncate text-[10px] font-normal opacity-70">{persona}</span>
                          )}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
          <div>
            <button className="text-[11px] text-muted-foreground hover:text-foreground"
                    onClick={() => setInheritOpen((v) => !v)}>
              {inheritOpen ? "▾" : "▸"} 知识继承{inheritFrom ? "（已选源）" : ""}
            </button>
            {inheritOpen && (
              <div className="mt-1.5 flex items-center gap-2">
                <span className="text-[10px] text-muted-foreground">从既有项目继承 binary 资产 / 函数库 / 蓝图（只增不覆盖）</span>
                <select value={inheritFrom ?? ""} onChange={(e) => setInheritFrom(e.target.value || null)}
                        className="rounded border bg-background px-1.5 py-0.5 text-xs [color-scheme:dark] [&>option]:bg-popover [&>option]:text-popover-foreground">
                  <option value="">不继承</option>
                  {projects.filter((p) => p.id).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {error && <p className="text-sm text-(--status-error)">{error}</p>}

      <div className="space-y-2">
        {projects.length === 0 && !error && (
          <p className="py-8 text-center text-sm text-muted-foreground">还没有项目——上面创建一个</p>
        )}
        {projects.map((p) => (
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
    <aside className="w-72 shrink-0 space-y-2 border-l p-4">
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
