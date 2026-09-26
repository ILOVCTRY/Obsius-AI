import { cn } from "@/lib/utils"

// 姿态内导航：主区顶部窄 tab（拍板「姿态内导航=主区顶部窄 tab」）。
// 选中 tab 的持久化键在此统一导出，NewShell 资源入口也要写它（🔌插件→工具·MCP）。

export const CANVAS_TAB_KEY = "ui.shell2.canvas-tab"
export const TOOLS_TAB_KEY = "ui.shell2.tools-tab"

export interface NarrowTabItem {
  key: string
  label: string
}

export function NarrowTabs({ items, active, onSelect }: {
  items: readonly NarrowTabItem[]
  active: string
  onSelect: (key: string) => void
}) {
  return (
    <div className="flex h-8 shrink-0 items-center gap-0.5 border-b bg-[#0d1117] px-2">
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          onClick={() => onSelect(it.key)}
          className={cn(
            "rounded px-2.5 py-0.5 text-[11px] transition-colors",
            active === it.key
              ? "bg-primary/15 font-medium text-primary"
              : "text-muted-foreground hover:bg-accent/40",
          )}
        >
          {it.label}
        </button>
      ))}
    </div>
  )
}
