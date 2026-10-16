import { useEffect, useRef, useState } from "react"
import { Check, Image as ImageIcon, Palette, RotateCcw, Upload } from "lucide-react"
import {
  applyGlassMode,
  applyTheme,
  getGlassMode,
  getStoredTheme,
  THEMES,
  type ThemeId,
} from "@/lib/theme"
import glassWallpaper from "@/assets/glass-wallpaper.svg"
import {
  clearWallpaperBlob,
  getWallpaperUrl,
  readWallpaperSettings,
  saveWallpaperBlob,
  writeWallpaperSettings,
  type WallpaperSettings,
} from "@/lib/wallpaper"
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

/** 毛玻璃 + 壁纸分区：质感开关（与配色正交，可叠加任一套主题）+ 壁纸上传/遮罩/填充。 */
function WallpaperSection({
  glass,
  onGlassChange,
  themeLabel,
}: {
  glass: boolean
  onGlassChange: (on: boolean) => void
  themeLabel: string
}) {
  const [settings, setSettings] = useState<WallpaperSettings>(() => readWallpaperSettings())
  const [customUrl, setCustomUrl] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let alive = true
    void getWallpaperUrl()
      .then((url) => { if (alive) setCustomUrl(url) })
      .catch(() => { /* IndexedDB 不可用时保持内置默认 */ })
    return () => { alive = false }
  }, [])

  const previewUrl = customUrl ?? glassWallpaper

  const onPick = async (file: File | undefined) => {
    if (!file) return
    setErr(null)
    setNote(null)
    setBusy(true)
    try {
      const url = await saveWallpaperBlob(file)
      setCustomUrl(url)
      // 壁纸只有毛玻璃能让它显形——上传后自动开启，避免「设了却看不到」。
      onGlassChange(applyGlassMode(true))
      setNote(`已应用「${file.name}」，并自动开启毛玻璃效果`)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ""
    }
  }

  const onReset = async () => {
    setErr(null)
    setNote(null)
    try {
      await clearWallpaperBlob()
      setCustomUrl(null)
      setNote("已恢复内置默认壁纸")
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="mt-6 max-w-3xl">
      <div className="mb-3 flex items-start gap-3 rounded border border-(--brand-border) bg-(--brand-bg) p-3">
        <ImageIcon size={16} className="mt-0.5 shrink-0 text-primary" />
        <div>
          <p className="font-medium text-foreground">毛玻璃与壁纸</p>
          <p className="mt-1 text-muted-foreground">
            毛玻璃是一种可叠加在任一套配色上的质感：开启后当前配色的表面变半透明并轻微糊化。
            壁纸衬在界面最底层，开启毛玻璃后会从半透明表面透出；关闭则被实色表面完全盖住。
            图片只存在本机浏览器里，不上传云端。
          </p>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-card p-3">
        {/* 质感开关：与上方配色选择正交 */}
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="font-medium text-foreground">毛玻璃效果</p>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              叠加在当前「{themeLabel}」配色上；关闭即回到该配色的实色表面
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={glass}
            aria-label="毛玻璃效果"
            title={glass ? "关闭毛玻璃" : "开启毛玻璃"}
            onClick={() => onGlassChange(applyGlassMode(!glass))}
            className={cn(
              "relative h-4 w-7 shrink-0 rounded-full border transition-colors",
              glass
                ? "border-(--brand-border) bg-(--brand-bg)"
                : "border-border bg-(--surface-1)",
            )}
          >
            <span
              className={cn(
                "absolute top-1/2 h-2.5 w-2.5 -translate-y-1/2 rounded-full transition-all",
                glass ? "left-[16px] bg-(--brand)" : "left-0.5 bg-(--muted-foreground)",
              )}
            />
          </button>
        </div>

        <div className="my-3 border-t border-border" />

        {/* 预览：按当前 fill/dim 实时示意（遮罩色与 .wallpaper-layer::after 同族） */}
        <div
          className="relative h-32 w-full overflow-hidden rounded-md border border-border bg-(--surface-0)"
          role="img"
          aria-label="壁纸预览"
        >
          <div
            className="absolute inset-0"
            style={{
              backgroundImage: `url("${previewUrl}")`,
              backgroundSize: settings.fit === "cover" ? "cover" : "contain",
              backgroundPosition: "center",
              backgroundRepeat: "no-repeat",
            }}
          />
          <div
            className="absolute inset-0"
            style={{ background: `oklch(0.08 0.014 250 / ${settings.dim})` }}
          />
          <span className="absolute bottom-1.5 right-2 rounded-full bg-black/50 px-1.5 py-px text-[9px] text-white/90">
            {customUrl ? "自定义壁纸" : "内置默认壁纸"}
          </span>
        </div>

        {!glass && (
          <p className="mt-2 text-[11px] text-muted-foreground">
            毛玻璃当前关闭——壁纸不会显示在界面上；开启开关（或上传图片会自动开启）后立即生效。
          </p>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
            className="inline-flex items-center gap-1.5 rounded-md bg-(--brand) px-3 py-1.5 font-medium text-(--primary-foreground) transition-colors hover:opacity-90 disabled:opacity-50"
          >
            <Upload size={13} />
            {busy ? "处理中…" : "选择图片"}
          </button>
          <button
            type="button"
            disabled={!customUrl || busy}
            onClick={() => void onReset()}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 font-medium text-foreground transition-colors hover:bg-(--surface-1) disabled:opacity-40"
          >
            <RotateCcw size={13} />
            恢复默认
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => void onPick(e.target.files?.[0])}
          />
        </div>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="block">
            <span className="flex items-center justify-between text-[11px] text-muted-foreground">
              <span>遮罩浓度</span>
              <span className="font-mono">{Math.round(settings.dim * 100)}%</span>
            </span>
            <input
              type="range"
              min={0}
              max={0.8}
              step={0.05}
              value={settings.dim}
              onChange={(e) => setSettings(writeWallpaperSettings({ dim: Number(e.target.value) }))}
              className="mt-1.5 w-full accent-(--brand)"
            />
          </label>
          <div>
            <span className="block text-[11px] text-muted-foreground">填充方式</span>
            <div className="mt-1.5 flex gap-2">
              {([["cover", "封面（裁切铺满）"], ["contain", "完整（留边）"]] as const).map(([fit, label]) => (
                <button
                  key={fit}
                  type="button"
                  aria-pressed={settings.fit === fit}
                  onClick={() => setSettings(writeWallpaperSettings({ fit }))}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-[11px] transition-colors",
                    settings.fit === fit
                      ? "border-(--brand-border) bg-(--brand-bg) text-foreground"
                      : "border-border text-muted-foreground hover:text-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>

        {err && (
          <p className="mt-3 rounded border border-(--status-error)/40 bg-(--status-error)/10 px-2.5 py-1.5 text-[11px] text-(--status-error)">
            {err}
          </p>
        )}
        {note && !err && (
          <p className="mt-3 rounded border border-(--status-ok)/40 bg-(--status-ok)/10 px-2.5 py-1.5 text-[11px] text-foreground">
            {note}
          </p>
        )}
      </div>
    </div>
  )
}

export function AppearancePane() {
  const [selected, setSelected] = useState<ThemeId>(() => getStoredTheme())
  // 毛玻璃质感与配色正交：开关只改 html[data-glass]，卡片预览同步带上该属性
  const [glass, setGlass] = useState<boolean>(() => getGlassMode())

  const selectTheme = (id: ThemeId) => setSelected(applyTheme(id))
  const selectedLabel = THEMES.find((t) => t.id === selected)?.label ?? selected

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
              data-glass={glass ? "1" : undefined}
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
      <WallpaperSection glass={glass} onGlassChange={setGlass} themeLabel={selectedLabel} />
    </div>
  )
}
