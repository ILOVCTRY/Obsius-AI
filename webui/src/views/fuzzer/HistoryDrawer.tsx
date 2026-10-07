import { useEffect, useState } from "react"
import { X } from "lucide-react"
import { api } from "@/lib/api"
import type { HttpHistoryRow } from "@/lib/types"
import { cn } from "@/lib/utils"

/** 重放历史抽屉（左滑出）：本项目 source=replay 记录，点选回填编辑器并载入该次响应。
 *  仅在打开时拉取（不轮询），倒序 cap 100。 */
export function HistoryDrawer({ pid, open, onClose, onPick }: {
  pid: string
  open: boolean
  onClose: () => void
  onPick: (row: HttpHistoryRow) => void
}) {
  const [rows, setRows] = useState<HttpHistoryRow[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!open) return
    let alive = true
    setLoading(true)
    void api.browserHistory(pid, { source: "replay", limit: 100 })
      .then((r) => { if (alive) setRows(r) })
      .catch(() => { /* 后端可能刚启动 */ })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open, pid])

  if (!open) return null
  return (
    <aside className="flex w-72 shrink-0 flex-col border-r bg-background/95">
      <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1.5">
        <span className="text-[11px] font-medium">重放历史（本项目）</span>
        <span className="flex-1" />
        <button className="text-muted-foreground hover:text-foreground" title="关闭" onClick={onClose}>
          <X size={13} />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-1">
        {loading && rows.length === 0 && <div className="p-2 text-[10px] text-muted-foreground">加载中…</div>}
        {!loading && rows.length === 0 && <div className="p-2 text-[10px] text-muted-foreground">暂无重放记录</div>}
        {rows.map((r) => (
          <button key={r.id}
                  className="block w-full rounded px-1.5 py-1 text-left text-[10px] hover:bg-accent"
                  onClick={() => onPick(r)}>
            <div className="flex items-center gap-1">
              <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full",
                                   r.status == null ? "bg-red-500"
                                     : r.status < 400 ? "bg-(--status-ok)" : "bg-(--status-paused)")} />
              <span className="font-mono font-bold">{r.method}</span>
              <span className="font-mono">{r.status ?? "-"}</span>
              <span className="text-muted-foreground">{r.duration_ms ?? "?"}ms</span>
              {r.meta?.gm_tls === true && <span className="text-muted-foreground">国密</span>}
            </div>
            <div className="truncate text-muted-foreground">{r.url}</div>
          </button>
        ))}
      </div>
    </aside>
  )
}
