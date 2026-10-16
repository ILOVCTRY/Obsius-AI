export const THEMES = [
  { id: "cyber-dark", label: "暗黑", description: "深灰基底与青色行动强调" },
  { id: "warm-dark", label: "暖炭", description: "暖灰基底与珊瑚橙强调" },
  { id: "light", label: "明亮", description: "浅色基底，适合强光环境与打印对照" },
  { id: "rose-dark", label: "绯樱", description: "深莓基底与玫粉行动强调，长时作业不刺眼" },
  { id: "rose-light", label: "蔷薇", description: "暖白基底与玫粉强调，柔和不刺眼" },
] as const

export type ThemeId = (typeof THEMES)[number]["id"]
export const THEME_IDS = THEMES.map((theme) => theme.id) as ThemeId[]
export const DEFAULT_THEME: ThemeId = "cyber-dark"
const THEME_KEY = "ui.theme"
const GLASS_KEY = "ui.glass"

function isThemeId(value: string | null): value is ThemeId {
  return value !== null && (THEME_IDS as string[]).includes(value)
}

/** 旧版（2026-10-15 及之前）毛玻璃是独立主题 id="glass"——迁移为 cyber-dark + 玻璃模式。 */
function migrateLegacyGlass(value: string | null): string | null {
  return value === "glass" ? DEFAULT_THEME : value
}

export function getStoredTheme(): ThemeId {
  try {
    const value = migrateLegacyGlass(window.localStorage.getItem(THEME_KEY))
    if (value !== window.localStorage.getItem(THEME_KEY)) {
      window.localStorage.setItem(THEME_KEY, value ?? DEFAULT_THEME)
      window.localStorage.setItem(GLASS_KEY, "1")
    }
    return isThemeId(value) ? value : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

export function applyTheme(value: string): ThemeId {
  const theme = isThemeId(value) ? value : DEFAULT_THEME
  document.documentElement.dataset.theme = theme
  try {
    window.localStorage.setItem(THEME_KEY, theme)
  } catch {
    // localStorage 受限时仍让当前页面生效。
  }
  applyGlassMode(getGlassMode())
  return theme
}

export function applyStoredTheme(): ThemeId {
  return applyTheme(getStoredTheme())
}

/** 毛玻璃质感模式（2026-10-15 起与配主题正交）：表面改半透明 + backdrop-filter，
 *  可叠在任一套配色上；关闭即回到该配色的实色表面。CSS 见 index.css 的
 *  glass-mode 块（scripts/gen_glass_theme.py 派生，起止标记夹住）。 */
export function getGlassMode(): boolean {
  try {
    return window.localStorage.getItem(GLASS_KEY) === "1"
  } catch {
    return false
  }
}

export function applyGlassMode(on: boolean): boolean {
  if (on) document.documentElement.dataset.glass = "1"
  else delete document.documentElement.dataset.glass
  try {
    window.localStorage.setItem(GLASS_KEY, on ? "1" : "0")
  } catch {
    // localStorage 受限时仍让当前页面生效。
  }
  return on
}

export function applyStoredGlassMode(): boolean {
  return applyGlassMode(getGlassMode())
}
