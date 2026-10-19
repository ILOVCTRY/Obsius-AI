import { useCallback, useEffect, useMemo, useState } from "react"
import { Eraser, RotateCcw, Search, Trash2 } from "lucide-react"
import { api } from "@/lib/api"
import type { ProjectMeta } from "@/lib/types"
import { bindingBadge } from "@/lib/taxonomy"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"
import { fmtDateTime } from "@/lib/datetime"

// 回收站（DESIGN.md §5.3）：列出已删项目（目录在 workspaces/.trash/）。
// 删除仍是「整目录 rename 进 .trash」，不物理删除；恢复=反向 rename（同名后端
// 自动加 -2/-3 后缀，项目 name/id 不变）。终点操作（2026-10-18，用户需求）：
//   - 永久删除：物理抹除整条目（purge_trashed），不可恢复；
//   - 清除文件：抹除条目内项目文件、仅留 project.json 墓碑
//     （clear_trashed_contents，meta 落 trash_purged_at：名字信息仍在回收站
//     清单里可展示，但数据没了——恢复禁用，仅剩永久删除收口）。

/** 确认弹窗三种动作的文案/样式表 */
type ConfirmKind = "restore" | "purge" | "clear"

const CONFIRM_COPY: Record<ConfirmKind, {
  title: (name: string) => string
  desc: string
  action: string
  busy: string
  variant: "default" | "destructive"
}> = {
  restore: {
    title: (name) => `恢复项目「${name}」？`,
    desc: "该项目目录将从回收站移回项目列表，黑板数据完好；若已有同名项目，新目录会自动加后缀。",
    action: "恢复",
    busy: "恢复中…",
    variant: "default",
  },
  clear: {
    title: (name) => `清除项目「${name}」内的文件？`,
    desc: "将抹除项目内全部文件（黑板库、样本、产物、日志），仅保留项目名字信息——该条目仍显示在回收站清单中，但数据已不存在。此操作不可恢复，清除后仅能对条目做永久删除。",
    action: "清除文件",
    busy: "清除中…",
    variant: "default",
  },
  purge: {
    title: (name) => `永久删除「${name}」？`,
    desc: "将物理抹除回收站中的该条目及其全部数据（黑板库、样本、产物、日志），不可恢复。",
    action: "永久删除",
    busy: "删除中…",
    variant: "destructive",
  },
}

/** 回收站目录名 = `<slug>-<UTC紧凑时间戳>`；取尾部时间戳用于展示删除时间 */
const trashTime = (name?: string): string | undefined => {
  const m = /-(\d{8}T\d{6}Z)$/.exec(name ?? "")
  return m ? m[1] : undefined
}

export function TrashView() {
  const [items, setItems] = useState<ProjectMeta[]>([])
  const [error, setError] = useState<string | null>(null)
  // 单一确认弹窗：kind=恢复/清除文件/永久删除（三者共用开关与忙碌态）
  const [confirm, setConfirm] = useState<{ kind: ConfirmKind; target: ProjectMeta } | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [query, setQuery] = useState("")   // 项目名称搜索过滤（2026-10-11）

  const refresh = useCallback(() => {
    api.listTrashedProjects().then(setItems).catch((e) => setError(String(e)))
  }, [])
  useEffect(refresh, [refresh])

  const ask = (kind: ConfirmKind, target: ProjectMeta) => {
    setActionError(null)
    setConfirm({ kind, target })
  }

  const run = async () => {
    const cur = confirm
    if (!cur?.target.trash_name) return
    setBusy(true)
    try {
      if (cur.kind === "restore") {
        await api.restoreProject(cur.target.trash_name)
      } else if (cur.kind === "purge") {
        await api.purgeTrashedProject(cur.target.trash_name)
      } else {
        await api.clearTrashedProject(cur.target.trash_name)
      }
      setConfirm(null)
      refresh()
    } catch (e) {
      // 422 目录占用 / 404 条目消失：保持弹窗打开，原因显示在描述下方
      setError(null)
      setActionError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // 名称/ID 过滤（大小写不敏感）；空白查询=全量（不改变原顺序）
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items
    return items.filter((p) =>
      p.name.toLowerCase().includes(q) || p.id.toLowerCase().includes(q))
  }, [items, query])

  const copy = confirm ? CONFIRM_COPY[confirm.kind] : null

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
            若已有同名项目，目录会自动加后缀，不影响已有项目。「清除文件」仅保留项目名字信息、抹除项目数据；
            「永久删除」物理抹除整条目——两者均不可恢复。
          </p>
        </header>

        {error && <p className="text-sm text-(--status-error)">{error}</p>}

        {/* 搜索过滤（2026-10-11）：按项目名称/ID 过滤已删除项目 */}
        <div className="relative">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索项目名称或 ID"
            aria-label="搜索已删除项目"
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

        <div className="space-y-2">
          {items.length === 0 && !error && (
            <p className="py-8 text-center text-sm text-muted-foreground">回收站是空的</p>
          )}
          {items.length > 0 && shown.length === 0 && (
            <p className="py-8 text-center text-sm text-muted-foreground">
              没有匹配「{query.trim()}」的项目
            </p>
          )}
          {shown.map((p) => {
            // 墓碑条目（clear 后）：只剩 project.json，数据没了——禁恢复、禁再清
            const purged = Boolean(p.trash_purged_at)
            return (
              <div
                key={p.trash_name ?? p.id}
                className={cn(
                  "group flex w-full flex-wrap items-center gap-3 rounded-lg border p-3 text-left",
                  "hover:border-(--ring) hover:bg-accent/50",
                )}
              >
                <Badge variant="outline" className="font-mono">{bindingBadge(p.track, p.experts)}</Badge>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{p.name}</span>
                    {purged && (
                      <span
                        className="shrink-0 rounded border border-(--status-warning)/50 px-1.5 py-0.5 text-[10px] leading-4 text-(--status-warning)"
                        title="项目内文件已清除，仅保留名字信息"
                      >
                        文件已清空
                      </span>
                    )}
                  </div>
                  <div className="font-mono text-xs text-muted-foreground">{p.id}</div>
                </div>
                {trashTime(p.trash_name) && (
                  <span className="text-xs text-muted-foreground">
                    删除于 {fmtDateTime(trashTime(p.trash_name))}
                  </span>
                )}
                <div className="flex shrink-0 items-center gap-2">
                  {purged ? (
                    <span className="text-xs text-muted-foreground" title="项目内文件已清除，无法恢复">不可恢复</span>
                  ) : (
                    <Button
                      variant="outline"
                      size="sm"
                      className="shrink-0"
                      disabled={busy}
                      onClick={() => ask("restore", p)}
                    >
                      <RotateCcw size={14} />恢复
                    </Button>
                  )}
                  {!purged && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="shrink-0"
                      disabled={busy}
                      title="抹除项目内全部文件（黑板库/样本/产物/日志），仅保留项目名字信息"
                      onClick={() => ask("clear", p)}
                    >
                      <Eraser size={14} />清除文件
                    </Button>
                  )}
                  <Button
                    variant="destructive"
                    size="sm"
                    className="shrink-0"
                    disabled={busy}
                    title="从回收站永久删除，物理抹除，不可恢复"
                    onClick={() => ask("purge", p)}
                  >
                    <Trash2 size={14} />永久删除
                  </Button>
                </div>
              </div>
            )
          })}
        </div>

        <AlertDialog
          open={confirm !== null}
          onOpenChange={(open) => !open && setConfirm(null)}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{copy?.title(confirm?.target.name ?? "")}</AlertDialogTitle>
              <AlertDialogDescription>{copy?.desc}</AlertDialogDescription>
              {actionError && <p className="text-sm text-(--status-error)">{actionError}</p>}
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel asChild>
                <Button variant="outline" size="sm" disabled={busy}>取消</Button>
              </AlertDialogCancel>
              {/* preventDefault 拦下 Radix 点击即关弹，失败原因留在弹窗内；成功由 run 收口 */}
              <AlertDialogAction asChild>
                <Button size="sm"
                        variant={copy?.variant}
                        onClick={(e) => { e.preventDefault(); run() }} disabled={busy}>
                  {busy ? copy?.busy : copy?.action}
                </Button>
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  )
}
