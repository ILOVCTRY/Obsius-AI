import { useCallback, useEffect, useState } from "react"
import { Trash2 } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import type { IntelOverview, ProjectMeta } from "@/lib/types"
import { RECOMMENDED_CAPS, bindingBadge, capLabel, trackLabel, type Taxonomy } from "@/lib/taxonomy"
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
// 项目绑定 = 场景轨（单选）× 能力包（多选）（DESIGN.md §4.5）

export function ProjectsView({ onOpen }: { onOpen: (pid: string) => void }) {
  const [projects, setProjects] = useState<ProjectMeta[]>([])
  const [tax, setTax] = useState<Taxonomy | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [name, setName] = useState("")
  const [track, setTrack] = useState<string>("ctf")
  const [caps, setCaps] = useState<string[]>(["binary"])
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
      const t0 = t.tracks[0]?.name ?? "ctf"
      setTrack(t0)
      setCaps(RECOMMENDED_CAPS[t0] ?? [])
    }).catch(() => {})
  }, [])

  const pickTrack = (t: string) => {
    setTrack(t)
    setCaps(RECOMMENDED_CAPS[t] ?? [])
  }
  const toggleCap = (c: string) =>
    setCaps((cs) => cs.includes(c) ? cs.filter((x) => x !== c) : [...cs, c])

  const create = async () => {
    if (!name.trim()) return
    setCreating(true)
    setError(null)
    try {
      const p = await api.createProject(name.trim(), track, caps)
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
        <p className="text-sm text-muted-foreground">项目 = 场景轨（单选）× 能力包（多选）；数据随 workspaces/ 项目目录隔离</p>
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
                onClick={() => pickTrack(t.name)}
                title={t.description}
              >
                {t.label || trackLabel(t.name)}
              </Button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] text-muted-foreground">能力包</span>
            {(tax?.capabilities ?? []).map((c) => (
              <button
                key={c.name}
                onClick={() => toggleCap(c.name)}
                title={c.description}
                className={cn(
                  "rounded-md border px-2 py-0.5 text-xs",
                  caps.includes(c.name)
                    ? "border-primary/50 bg-primary/10 text-primary"
                    : "text-muted-foreground hover:bg-accent/50",
                )}
              >
                {c.label || capLabel(c.name)}
              </button>
            ))}
            {caps.length === 0 && (
              <span className="text-[10px] text-(--status-approval)">未勾能力包：只有轨级技能/规则生效</span>
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
            <Badge variant="outline" className="font-mono">{bindingBadge(p.track, p.capabilities)}</Badge>
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
