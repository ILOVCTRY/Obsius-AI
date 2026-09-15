// 时间本地化（DESIGN.md §12）：后端存储一律 UTC ISO（naive，无 Z；
// packs 历史版本号是紧凑形 20260913T145750Z），展示统一转浏览器本地时区。
// 全 UI 时间渲染只准走这里——禁止再 slice ISO 字符串（那会直接显示 UTC，北京差 8 小时）。

const pad = (n: number) => String(n).padStart(2, "0")

/** 解析后端时间：naive ISO 当 UTC；带时区后缀原样解析；兼容紧凑版本号 20260913T145750Z。 */
export function parseTs(ts: string): Date {
  const s = ts.trim()
  const compact = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$/.exec(s)
  if (compact) {
    const [, y, mo, d, h, mi, se] = compact
    return new Date(Date.UTC(+y, +mo - 1, +d, +h, +mi, +se))
  }
  // 末尾无 Z / ±HH:mm 时区偏移的，按 UTC 补 Z
  return new Date(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z")
}

function parts(d: Date, utc: boolean): [number, number, number, number, number, number] {
  return utc
    ? [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate(), d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()]
    : [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds()]
}

function format(ts: string | null | undefined, pattern: string, utc: boolean): string {
  if (!ts) return ""
  const d = parseTs(ts)
  if (Number.isNaN(d.getTime())) return ts
  const [y, mo, da, h, mi, se] = parts(d, utc)
  const map: Record<string, string> = {
    YYYY: String(y), MM: pad(mo), DD: pad(da), HH: pad(h), mm: pad(mi), ss: pad(se),
  }
  return pattern.replace(/YYYY|MM|DD|HH|mm|ss/g, (k) => map[k])
}

/** 本地时区 YYYY-MM-DD HH:mm:ss（§12 统一格式） */
export const fmtDateTime = (ts?: string | null) => format(ts ?? "", "YYYY-MM-DD HH:mm:ss", false)
/** 本地时区 YYYY-MM-DD HH:mm（窄列卡片） */
export const fmtDateTimeMin = (ts?: string | null) => format(ts ?? "", "YYYY-MM-DD HH:mm", false)
/** 本地时区 YYYY-MM-DD */
export const fmtDate = (ts?: string | null) => format(ts ?? "", "YYYY-MM-DD", false)
/** 本地时区 HH:mm:ss（直播间事件流） */
export const fmtTime = (ts?: string | null) => format(ts ?? "", "HH:mm:ss", false)
/** UTC 原值 YYYY-MM-DD HH:mm:ss——给悬停 title 核对用 */
export const fmtUtcDateTime = (ts?: string | null) => format(ts ?? "", "YYYY-MM-DD HH:mm:ss", true)

/** <span title={...}> 用：悬停看 UTC 原值 */
export const utcTitle = (ts?: string | null) => (ts ? `UTC ${fmtUtcDateTime(ts)}` : undefined)
