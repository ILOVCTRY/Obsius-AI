import { useState } from "react"
import { Brain } from "lucide-react"
import { thinkingPreview } from "@/lib/activity"
import { cn } from "@/lib/utils"

// 思考块（会话流改造，2026-10-04）：对齐 cc-haha ThinkingBlock——**单行折叠**：
// 🧠 + 标签（思考中／已思考）+ 一行预览 + ▸ 箭头；点开在缩进框里看全文（内部
// 滚动，不撑破会话流）。此前把全文当斜体大段落平铺，既占版面又每次增量都整段
// 重排（「卡顿」的主因之一）；折成一行后重排面积恒定。
//
// 预览口径在 `lib/activity.ts` 的 thinkingPreview（流式中跟随尾部、稳定后回首行）。
// 与 `views/live/EventRow.tsx` 的 ThinkingRow 同视觉语言，但那条是事件行（带时间
// 戳与 duration），本组件是消息行，故不复用。

export function ThinkingBlock({ content, active = false, className }: {
  content: string
  /** 仍在流式：标签显「思考中」+ 预览跟随尾部 + 展开态底部光标 */
  active?: boolean
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const text = (content ?? "").replace(/\r\n?/g, "\n").trimEnd()
  if (!text.trim()) return null
  const preview = thinkingPreview(text, { streaming: active })

  return (
    <div className={cn("w-full shrink-0", className)}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? "折叠思考" : "展开思考全文"}
        className="flex w-full items-baseline gap-2 rounded-md px-2 py-1 text-left text-[12.5px] hover:bg-accent/40"
      >
        <Brain size={13} strokeWidth={1.8} aria-hidden
          className="mt-[3px] shrink-0 self-start text-muted-foreground/60" />
        <span className={cn("shrink-0 italic", active ? "text-primary" : "text-muted-foreground/70")}>
          {active ? "思考中" : "已思考"}
        </span>
        {preview
          ? <span className="min-w-0 flex-1 truncate italic text-muted-foreground/60">{preview}</span>
          : <span className="flex-1" />}
        <span aria-hidden
          className={cn("shrink-0 text-[8px] text-muted-foreground/60 transition-transform", open && "rotate-90")}>
          ▸
        </span>
      </button>
      {open && (
        <div className="mb-1.5 ml-5 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border/60 bg-card/40 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
          {text}
          {active && <span className="ml-0.5 inline-block h-3 w-[2px] animate-pulse bg-primary align-middle" />}
        </div>
      )}
    </div>
  )
}
