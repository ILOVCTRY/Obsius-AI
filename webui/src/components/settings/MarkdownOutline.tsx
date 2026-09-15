import { useEffect, useState } from "react"
import { extractHeadings, headingId } from "./MarkdownView"
import { cn } from "@/lib/utils"

// 当前文档 h1–h3 大纲：点击 scrollIntoView 到 MarkdownView 中同 id 标题。

export function MarkdownOutline({ markdown, prefix }: { markdown: string; prefix: string }) {
  const headings = extractHeadings(markdown)
  const [active, setActive] = useState<string | null>(null)

  // 切文档后清高亮
  useEffect(() => { setActive(null) }, [markdown, prefix])

  const jump = (slug: string) => {
    const el = document.getElementById(headingId(prefix, slug))
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" })
      setActive(slug)
    }
  }

  if (headings.length === 0) {
    return <p className="px-2 py-1 text-[10px] text-muted-foreground">本文档暂无标题大纲</p>
  }
  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
      {headings.map((h, i) => (
        <button
          key={`${h.slug}-${i}`}
          onClick={() => jump(h.slug)}
          className={cn(
            "block w-full truncate rounded py-0.5 pr-1 text-left text-[11px] hover:bg-accent/40",
            active === h.slug ? "text-primary" : "text-muted-foreground",
          )}
          style={{ paddingLeft: (h.level - 1) * 10 + 4 }}
          title={h.text}
        >
          {h.text}
        </button>
      ))}
    </div>
  )
}
