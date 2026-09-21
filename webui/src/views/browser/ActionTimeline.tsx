import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

// browser.* 动作时间线（3s 增量；F6-v3 自 SessionBar.tsx 迁出为独立文件）
export function ActionTimeline({ events, className }: {
  events: { id: number; kind: string; author: string; summary: string; created_at: string }[]
  className?: string
}) {
  return (
    <div className={cn("min-h-0 flex-1 overflow-auto p-2", className)}>
      <div className="mb-1 text-[10px] font-semibold text-muted-foreground">动作时间线</div>
      {events.length === 0 && (
        <div className="text-[11px] text-muted-foreground">暂无 browser.* 事件</div>
      )}
      <ul className="space-y-1">
        {events.map((e) => (
          <li key={e.id} className="rounded border px-1.5 py-1 font-mono text-[10px]">
            <span className="text-muted-foreground">{e.created_at.slice(11, 19)}</span>{" "}
            <Badge variant="outline" className="px-1 py-0 text-[9px]">{e.kind}</Badge>{" "}
            <span title={e.author}>{e.summary}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
