import { useState } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"

// 导入表（底部 tab）：按 DLL/模块分组折叠；ELF 的模块名是 .dynsym。

export function ImportsTable({ imports }: { imports: Record<string, string[]> | null }) {
  const modules = Object.entries(imports ?? {})
  const [open, setOpen] = useState<Set<string>>(() => new Set(modules.slice(0, 1).map(([m]) => m)))
  if (!imports || modules.length === 0) {
    return <p className="py-6 text-center text-[11px] text-muted-foreground">无导入表（未分诊或缓存增强段缺失）</p>
  }
  const toggle = (m: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(m)) next.delete(m)
      else next.add(m)
      return next
    })
  return (
    <div className="p-1.5">
      {modules.map(([mod, fns]) => {
        const expanded = open.has(mod)
        return (
          <div key={mod}>
            <button
              type="button"
              onClick={() => toggle(mod)}
              className="flex w-full items-center gap-1 rounded px-1 py-1 text-left text-[11px] hover:bg-accent/40"
            >
              {expanded ? <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
                        : <ChevronRight className="size-3 shrink-0 text-muted-foreground" />}
              <span className="font-mono">{mod}</span>
              <span className="font-mono text-[10px] text-muted-foreground">{fns.length}</span>
            </button>
            {expanded && (
              <div className="ml-5 border-l pl-2">
                {fns.map((fn) => (
                  <div key={fn} className={cn("truncate py-0.5 font-mono text-[10px] text-muted-foreground")}>
                    {fn}
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
