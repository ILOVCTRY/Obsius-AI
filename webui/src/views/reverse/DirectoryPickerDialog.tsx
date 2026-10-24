import { useEffect, useRef, useState } from "react"
import { ArrowUp, ChevronLeft, FolderOpen, HardDrive, Loader2, TriangleAlert } from "lucide-react"
import { api } from "@/lib/api"
import type { DirEntry, DirListing } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * 「选择分析包」目录选择弹层（2026-10-24）。
 *
 * 两条入口并存，按运行环境分派：
 * - 桌面壳（Electron）：顶部「系统对话框」按钮走原生 `pickDirectory`，一次点到底；
 * - 浏览器（启动新UI.bat）：原生对话框不存在，直接用下面这个应用内浏览器逐级点。
 *
 * 关键取舍——**扫描代价必须提前告知**：选中一个几十万文件的目录后会全量算
 * sha256，这不是瞬时动作。故选定后先停在确认态，明确写出「将扫描 N 个子项，
 * 大目录可能需要几分钟」，而不是点一下就静默卡住。
 */
export function DirectoryPickerDialog({ pid, open, onClose, onPick, busy = false, hint }: {
  pid: string
  open: boolean
  onClose: () => void
  onPick: (path: string) => void
  /** 父级正在扫描：禁用确认并显示进度文案。 */
  busy?: boolean
  hint?: string | null
}) {
  const [listing, setListing] = useState<DirListing | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [manual, setManual] = useState("")
  const [selected, setSelected] = useState("")
  const dialogRef = useRef<HTMLDivElement>(null)
  const nativeAvailable = typeof window !== "undefined" && typeof window.desktopBrowser?.pickDirectory === "function"

  const load = (path?: string) => {
    setLoading(true); setError(null)
    api.browseDirs(pid, path)
      .then((row) => { setListing(row); setSelected(row.path); setManual(row.path) })
      .catch((reason) => setError(String(reason)))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (!open) return
    setListing(null); setError(null); setSelected(""); setManual("")
    load()
  }, [open, pid]) // eslint-disable-line react-hooks/exhaustive-deps

  // 弹层打开即把焦点收进对话框（两个条件都要：bg-popover + role=dialog 是本仓
  // 弹层约定；焦点不进来则 Esc/键盘操作全落到背后的页面上）。
  useEffect(() => {
    if (!open) return
    const node = dialogRef.current
    if (node) node.focus()
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose() }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onClose])

  if (!open) return null

  const pickFromOs = async () => {
    if (!window.desktopBrowser?.pickDirectory) return
    try {
      const path = await window.desktopBrowser.pickDirectory(selected || listing?.path || "")
      if (path) onPick(path)
    } catch (reason) { setError(String(reason)) }
  }

  const confirm = () => {
    const target = (manual || selected || "").trim()
    if (!target) { setError("请先选择一个目录"); return }
    onPick(target)
  }

  const entryRow = (entry: DirEntry) => (
    <button key={entry.path} type="button"
      className={cn("flex w-full items-center gap-2 rounded px-2 py-1 text-left text-[11px] hover:bg-accent/60",
        selected === entry.path && "bg-primary/10 text-primary")}
      onClick={() => { setSelected(entry.path); setManual(entry.path) }}
      // 双击直接进去（单击只选中）：与系统文件对话框的肌肉记忆一致，且避免
      // 误触——这里的「误触」代价是白扫一个几十 GB 的目录。
      onDoubleClick={() => load(entry.path)}>
      {entry.root ? <HardDrive className="size-3 shrink-0" /> : <FolderOpen className="size-3 shrink-0" />}
      <span className="min-w-0 flex-1 truncate" title={entry.path}>{entry.root ? entry.path : entry.name}</span>
      {entry.entry_count !== null && entry.entry_count !== undefined && (
        <span className="shrink-0 text-[10px] text-muted-foreground">{entry.entry_count} 项</span>
      )}
      {entry.is_workspace && <span className="shrink-0 text-[10px] text-primary">当前工作区</span>}
      {entry.is_project && <span className="shrink-0 text-[10px] text-muted-foreground">项目目录</span>}
    </button>
  )

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label="选择分析包目录"
        className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border bg-popover shadow-xl outline-none">
        <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2">
          <FolderOpen className="size-4" />
          <span className="text-sm font-medium">选择分析包目录</span>
          <span className="min-w-0 flex-1" />
          <button type="button" className="rounded border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent"
            onClick={onClose}>关闭</button>
        </div>

        <div className="shrink-0 space-y-2 border-b px-4 py-2">
          <p className="text-[11px] text-muted-foreground">
            选中的目录会<b className="font-semibold text-foreground">原地</b>登记为分析包并作为执行工作区（agent 的 cwd），
            不会复制任何文件。大目录首次扫描要算全量哈希，可能需要几分钟。
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {nativeAvailable && (
              <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" onClick={() => void pickFromOs()}>
                <HardDrive className="size-3" />系统对话框
              </Button>
            )}
            <div className="flex min-w-0 flex-1 items-center gap-1">
              <input
                className="h-7 min-w-0 flex-1 rounded-md border bg-background px-2 font-mono text-[11px]"
                placeholder={"或直接粘贴绝对路径，如 E:\\Games\\SomeGame"}
                value={manual}
                onChange={(event) => setManual(event.target.value)}
                onKeyDown={(event) => { if (event.key === "Enter") confirm() }}
              />
              <Button size="icon-xs" variant="ghost" title="上一级"
                disabled={!listing || listing.roots || !listing.parent}
                onClick={() => listing?.parent && load(listing.parent)}>
                <ArrowUp className="size-3.5" />
              </Button>
            </div>
          </div>
          {!nativeAvailable && (
            <p className="text-[10px] text-muted-foreground">
              当前不在桌面壳里（浏览器打开），系统对话框不可用——用下面的目录列表逐级点，或直接粘贴路径。
            </p>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {loading && <p className="flex items-center gap-2 py-6 text-center text-xs text-muted-foreground"><Loader2 className="size-3 animate-spin" />正在读取目录…</p>}
          {error && <p className="px-2 py-3 text-[11px] text-destructive">{error}</p>}
          {!loading && listing && (
            <>
              {!listing.roots && (
                <div className="mb-1 flex items-center gap-2 px-2 py-1 text-[10px] text-muted-foreground">
                  <ChevronLeft className="size-3" />
                  <span className="min-w-0 flex-1 truncate font-mono">{listing.path || "磁盘"}</span>
                  {listing.truncated && <span className="shrink-0">子目录过多，仅显示前 {listing.entries.length} 个</span>}
                </div>
              )}
              {listing.entries.length === 0 && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {listing.roots ? "没有可用的磁盘起点" : "这个目录下没有子目录（仍可直接选中它）"}
                </p>
              )}
              <div className="space-y-0.5">{listing.entries.map(entryRow)}</div>
            </>
          )}
        </div>

        <div className="shrink-0 space-y-2 border-t px-4 py-2">
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-muted-foreground">已选：</span>
            <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={manual || selected}>
              {manual || selected || "（未选择）"}
            </span>
            <Button size="sm" className="h-7 text-[11px]" disabled={busy || !(manual || selected).trim()} onClick={confirm}>
              {busy ? "扫描中…" : "使用此目录"}
            </Button>
          </div>
          {hint && (
            <p className="flex items-start gap-1 text-[10px] text-(--status-approval)">
              <TriangleAlert className="mt-0.5 size-3 shrink-0" />{hint}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
