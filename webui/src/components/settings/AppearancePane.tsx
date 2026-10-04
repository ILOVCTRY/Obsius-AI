import { useState } from "react"
import { Check, Palette } from "lucide-react"
import { applyTheme, getStoredTheme, THEMES, type ThemeId } from "@/lib/theme"
import { cn } from "@/lib/utils"

// 每张卡用 data-theme 作用域自身，预览即该主题的真实配色（token 选择器是属性匹配，
// 不限于 :root，所以嵌套元素也能局部套用整套主题变量）。
function ThemePreview() {
  return (
    <div className="flex h-24 gap-2 rounded-md border border-border bg-(--surface-0) p-2">
      <span className="flex w-10 shrink-0 flex-col gap-1.5 rounded-sm bg-(--surface-1) p-1.5">
        <span className="h-1.5 w-full rounded-full bg-(--brand)" />
        <span className="h-1.5 w-3/4 rounded-full bg-(--muted-foreground)/40" />
        <span className="h-1.5 w-2/3 rounded-full bg-(--muted-foreground)/40" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1.5">
        <span className="h-2 w-2/3 rounded-sm bg-(--foreground)/70" />
        <span className="h-1.5 w-1/2 rounded-sm bg-(--muted-foreground)/50" />
        <span className="mt-auto flex items-center gap-1.5">
          <span className="h-3.5 w-12 rounded-full bg-(--brand)" />
          <span className="h-3.5 w-3.5 rounded-full bg-(--status-approval)" />
          <span className="h-3.5 w-3.5 rounded-full bg-(--status-ok)" />
          <span className="h-3.5 w-3.5 rounded-full bg-(--status-error)" />
        </span>
      </span>
    </div>
  )
}

export function AppearancePane() {
  const [selected, setSelected] = useState<ThemeId>(() => getStoredTheme())

  const selectTheme = (id: ThemeId) => setSelected(applyTheme(id))

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4 text-xs">
      <div className="mb-4 flex items-start gap-3 rounded border border-(--brand-border) bg-(--brand-bg) p-3">
        <Palette size={16} className="mt-0.5 shrink-0 text-primary" />
        <div>
          <p className="font-medium text-foreground">界面主题</p>
          <p className="mt-1 text-muted-foreground">点击卡片即时切换并记住选择；主题只影响 WebUI 外观，不改变任务、规则或执行策略。</p>
        </div>
      </div>
      <div className="grid max-w-3xl gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {THEMES.map((theme) => {
          const active = selected === theme.id
          return (
            <button
              key={theme.id}
              type="button"
              data-theme={theme.id}
              aria-pressed={active}
              onClick={() => selectTheme(theme.id)}
              className={cn(
                "rounded-lg border p-3 text-left transition-colors",
                active
                  ? "border-(--brand-border) bg-(--brand-bg)"
                  : "border-border bg-card hover:border-(--brand-border) hover:bg-(--brand-bg)",
              )}
            >
              <ThemePreview />
              <div className="mt-3 flex items-start gap-2">
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="font-medium text-foreground">{theme.label}</span>
                    {active && (
                      <span className="rounded-full bg-(--brand-bg) px-1.5 py-px text-[9px] font-medium text-(--brand)">
                        当前
                      </span>
                    )}
                  </span>
                  <span className="mt-1 block text-[10px] text-muted-foreground">{theme.description}</span>
                  <span className="mt-2 block font-mono text-[9px] uppercase tracking-wider text-muted-foreground/70">{theme.id}</span>
                </span>
                {active && <Check size={16} className="shrink-0 text-(--brand)" />}
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}
