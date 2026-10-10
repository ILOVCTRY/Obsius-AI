import { useCallback, useEffect, useState } from "react"
import { RotateCcw } from "lucide-react"
import { api } from "@/lib/api"
import type { ProjectMeta } from "@/lib/types"
import { bindingBadge } from "@/lib/taxonomy"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"
import { fmtDateTime } from "@/lib/datetime"

// 回收站（DESIGN.md §5.3）：列出已删项目（目录在 workspaces/.trash/），可一键恢复。
// 删除仍是「整目录 rename 进 .trash」，不物理删除；恢复=反向 rename（同名后端
// 自动加 -2/-3 后缀，项目 name/id 不变）。不做彻底删除/自动清空（DESIGN 明确不做）。

/** 回收站目录名 = `<slug>-<UTC紧凑时间戳>`；取尾部时间戳用于展示删除时间 */
const trashTime = (name?: string): string | undefined => {
  const m = /-(\d{8}T\d{6}Z)$/.exec(name ?? "")
  return m ? m[1] : undefined
}

export function TrashView() {
  const [items, setItems] = useState<ProjectMeta[]>([])
  const [error, setError] = useState<string | null>(null)
  const [confirmTarget, setConfirmTarget] = useState<ProjectMeta | null>(null)
  const [restoring, setRestoring] = useState(false)
  const [restoreError, setRestoreError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.listTrashedProjects().then(setItems).catch((e) => setError(String(e)))
  }, [])
  useEffect(refresh, [refresh])

  const restore = async () => {
    const target = confirmTarget
    if (!target?.trash_name) return
    setRestoring(true)
    try {
      await api.restoreProject(target.trash_name)
      setConfirmTarget(null)
      refresh()
    } catch (e) {
      // 422 目录占用 / 404 条目消失：保持弹窗打开，原因显示在描述下方
      setError(null)
      setRestoreError(e instanceof Error ? e.message : String(e))
    } finally {
      setRestoring(false)
    }
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto bg-[radial-gradient(circle_at_top_right,oklch(var(--brand-lch)/0.12),transparent_42%),linear-gradient(135deg,var(--surface-1),var(--surface-0))] pt-9">
      <div className="mx-auto w-full max-w-4xl space-y-7 p-6 lg:p-10">
        <header className="space-y-3">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-cyan-300/75">
            <span className="status-dot" />Recycle bin
          </div>
          <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">回收站</h1>
          <p className="max-w-xl text-sm leading-6 text-muted-foreground">
            已删除的项目整目录保留在 workspaces/.trash/ 中，数据不会丢失。点击「恢复」即可移回项目列表；
            若已有同名项目，目录会自动加后缀，不影响已有项目。
          </p>
        </header>

        {error && <p className="text-sm text-(--status-error)">{error}</p>}

        <div className="space-y-2">
          {items.length === 0 && !error && (
            <p className="py-8 text-center text-sm text-muted-foreground">回收站是空的</p>
          )}
          {items.map((p) => (
            <div
              key={p.trash_name ?? p.id}
              className={cn(
                "group flex w-full items-center gap-3 rounded-lg border p-3 text-left",
                "hover:border-(--ring) hover:bg-accent/50",
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
                onClick={() => {
                  setRestoreError(null)
                  setConfirmTarget(p)
                }}
              >
                <RotateCcw size={14} />恢复
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
              <AlertDialogTitle>恢复项目「{confirmTarget?.name}」？</AlertDialogTitle>
              <AlertDialogDescription>
                该项目目录将从回收站移回项目列表，黑板数据完好；若已有同名项目，新目录会自动加后缀。
              </AlertDialogDescription>
              {restoreError && <p className="text-sm text-(--status-error)">{restoreError}</p>}
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel asChild>
                <Button variant="outline" size="sm">取消</Button>
              </AlertDialogCancel>
              {/* preventDefault 拦下 Radix 点击即关弹，失败原因留在弹窗内；成功由 restore 收口 */}
              <AlertDialogAction asChild>
                <Button size="sm"
                        onClick={(e) => { e.preventDefault(); restore() }} disabled={restoring}>
                  {restoring ? "恢复中…" : "恢复"}
                </Button>
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  )
}
