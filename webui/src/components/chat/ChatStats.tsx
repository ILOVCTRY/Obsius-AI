// 会话统计行（会话流改造，2026-10-03）：`49m tokens（含缓存） · 最后更新 刚刚 · 1200 条消息`。
// 对齐 cc-haha 的 ActiveSession headerMetadataCandidates。两处消费：
//   · 工作台——精确值（thread.usage + messages.length）
//   · 直播间——窗口内近似（llm.usage 事件求和 + visible.length），调用方负责文案标注

export interface ChatStatsProps {
  /** 本轮/窗口累计 token（输入+输出+缓存读+缓存建） */
  tokens?: number
  /** 其中来自缓存读取的部分（>0 时文案加「含缓存」） */
  cachedTokens?: number
  /** 最后活动时间（ISO） */
  updatedAt?: string | null
  /** 消息/事件条数 */
  count?: number
  /** 条数单位后缀（缺省「条消息」，直播间可传「条事件」） */
  countLabel?: string
  className?: string
  endAdornment?: React.ReactNode
}

/** token 数短记法（对齐 cc-haha formatTokenCount）：847 / 1.2k / 1.2m */
export function fmtTokenCount(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0"
  if (n < 1000) return String(Math.round(n))
  if (n < 1_000_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}m`
}

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前（对齐 cc-haha lastUpdated） */
export function fmtRelative(iso?: string | null, now = Date.now()): string {
  if (!iso) return ""
  const t = new Date(iso.endsWith("Z") || iso.includes("T") ? iso : iso.replace(" ", "T") + "Z").getTime()
  if (!Number.isFinite(t)) return ""
  const d = Math.max(0, now - t)
  if (d < 60_000) return "刚刚"
  if (d < 3_600_000) return `${Math.floor(d / 60_000)} 分钟前`
  if (d < 86_400_000) return `${Math.floor(d / 3_600_000)} 小时前`
  return `${Math.floor(d / 86_400_000)} 天前`
}

export function ChatStats({ tokens = 0, cachedTokens = 0, updatedAt, count = 0,
                          countLabel = "条消息", className, endAdornment }: ChatStatsProps) {
  const parts: string[] = []
  if (tokens > 0) parts.push(`${fmtTokenCount(tokens)} tokens${cachedTokens > 0 ? "（含缓存）" : ""}`)
  const rel = fmtRelative(updatedAt)
  if (rel) parts.push(`最后更新 ${rel}`)
  if (count > 0) parts.push(`${count} ${countLabel}`)
  if (!parts.length) return null
  return (
    <div className={className ?? "flex shrink-0 items-center gap-2 truncate px-3 py-1 text-[11px] text-muted-foreground/70"}>
      <span className="min-w-0 truncate">{parts.join(" · ")}</span>
      {endAdornment && <span className="ml-auto flex shrink-0 items-center gap-1">{endAdornment}</span>}
    </div>
  )
}
