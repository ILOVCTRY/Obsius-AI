export const THEMES = [
  { id: "cyber-dark", label: "暗黑", description: "深灰基底与青色行动强调" },
  { id: "warm-dark", label: "暖炭", description: "暖灰基底与珊瑚橙强调" },
  { id: "light", label: "明亮", description: "浅色基底，适合强光环境与打印对照" },
] as const

export type ThemeId = (typeof THEMES)[number]["id"]
export const THEME_IDS = THEMES.map((theme) => theme.id) as ThemeId[]
export const DEFAULT_THEME: ThemeId = "cyber-dark"
const THEME_KEY = "ui.theme"

function isThemeId(value: string | null): value is ThemeId {
  return value !== null && (THEME_IDS as string[]).includes(value)
}

export function getStoredTheme(): ThemeId {
  try {
    const value = window.localStorage.getItem(THEME_KEY)
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
  return theme
}

export function applyStoredTheme(): ThemeId {
  return applyTheme(getStoredTheme())
}
