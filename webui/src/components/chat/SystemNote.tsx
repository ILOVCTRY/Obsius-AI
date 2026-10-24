import { useState } from "react"
import { Cpu } from "lucide-react"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { cn } from "@/lib/utils"

// 系统注入消息行（2026-10-24）。
//
// **问题**：后台专家补达 / 候选 flag 唤醒 / 台账更新 / 主动唤醒这四类消息，后端
// 是**以 `role=user` 落库**的——`ChatTurn.run(text)` 是人类发消息与系统唤醒共用的
// 唯一入口，没有第二条区分通道。前端此前一律按人类发言渲染：`❯` 引导符 +
// `whitespace-pre-wrap` 原文直出。于是 600 字的补达（带 `**加粗**`、``` 围栏、
// `- ` 列表）糊满半屏，且 Markdown 全是字面量（用户实测截图）。
//
// 现在后端在 `store.list_messages` 里派生 `origin` 字段，本组件接住它：
//
// 1. **默认折叠**——系统消息是「背景事实」，不是人的指令。默认只给一行摘要，
//    点开才看全文。这是本组件存在的主要理由：补达动辄 600 字，展开式会把
//    人类自己的提问挤出视口。
// 2. **走 MarkdownView 渲染**——正文是给模型看的，用了大量 `**粗体**`、围栏代码
//    块、列表。原文直出等于把标记符号也给人看。
// 3. **视觉与人类发言分家**——不用 `❯`（那是人的引导符），改 CPU 图标 + 虚线左
//    边框 + 弱化底色。人一眼能分辨「这句不是我说的」。
//
// **不复用 `HumanNote`**：两者数据源相同（都是 role=user 行）但语义相反，且
// HumanNote 刻意保持单行 pre-wrap（人类发言里的换行有意义、Markdown 不该被解
// 析——人打的 `*` 可能就是要星号）。合成一个组件靠布尔分叉只会让两边都别扭。

export function SystemNote({ label, body, at, className }: {
  /** 已剥掉 `〔标签〕` 的正文 */
  body: string
  /** 短标签（「系统补达」等），渲染成摘要行前缀 */
  label: string
  /** 发送时刻（已格式化）；乐观行没有真实时刻时传空串，不编造 */
  at?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const text = (body ?? "").replace(/\r\n?/g, "\n").trim()
  if (!text) return null

  // 摘要：首个非空行。系统文案的第一行都是「一句话说清这是什么」
  // （如「你派出的专家已在后台跑完」），正好当摘要。
  const firstLine = text.split("\n").map((l) => l.trim()).find((l) => l.length > 0) ?? ""

  return (
    <div className={cn(
      "wb-anim my-1.5 rounded-md border-l-2 border-dashed border-border pl-2.5 pr-1",
      className,
    )}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? "折叠这条系统消息" : "展开全文"}
        className="flex w-full items-baseline gap-2 rounded px-1.5 py-1 text-left hover:bg-accent/30"
      >
        <Cpu size={12} strokeWidth={1.8} aria-hidden
          className="mt-[3px] shrink-0 self-start text-muted-foreground/60" />
        <span className="shrink-0 font-mono text-[10px] font-bold tracking-wide text-muted-foreground/80">
          {label}
        </span>
        {!open && firstLine && (
          <span className="min-w-0 flex-1 truncate text-[12px] text-muted-foreground/70">
            {firstLine}
          </span>
        )}
        {open && <span className="flex-1" />}
        {at && (
          <span className="shrink-0 select-none font-mono text-[10px] text-muted-foreground/60">
            {at}
          </span>
        )}
        <span aria-hidden
          className={cn("shrink-0 text-[8px] text-muted-foreground/60 transition-transform",
            open && "rotate-90")}>
          ▸
        </span>
      </button>
      {open && (
        <div className="mb-1 ml-5 mr-1 rounded-lg border border-border/60 bg-card/40 px-3 py-2">
          <MarkdownView content={text} prefix={`sys-note-${label}`}
            className="text-[12px] leading-relaxed text-foreground/85" />
        </div>
      )}
    </div>
  )
}
