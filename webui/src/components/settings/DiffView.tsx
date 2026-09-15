import { cn } from "@/lib/utils"

// unified diff 高亮只读视图（历史版本/提案实时 diff 共用）

export function DiffView({ diff, className }: { diff: string; className?: string }) {
  if (!diff) return <p className="text-xs text-muted-foreground">无差异（当前内容与该版本/提案一致）。</p>
  return (
    <pre className={cn("rounded border bg-card/40 p-2 font-mono text-[10px] leading-relaxed",
      className)}>
      {diff.split(/\r?\n/).map((line, i) => (
        <div key={i} className={cn(
          line.startsWith("+") && !line.startsWith("+++") && "bg-emerald-500/10 text-emerald-300",
          line.startsWith("-") && !line.startsWith("---") && "bg-red-500/10 text-red-300",
          (line.startsWith("@@") || line.startsWith("+++") || line.startsWith("---"))
            && "text-muted-foreground",
        )}>{line || " "}</div>
      ))}
    </pre>
  )
}
