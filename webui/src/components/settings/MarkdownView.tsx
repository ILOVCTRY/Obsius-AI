import { memo } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

// 只读 Markdown 渲染（深色黑客风）。标题带 slug id 供大纲 scrollIntoView；
// idPrefix 防同页技能正文与 kb 文档撞 id。链接限本机/相对，原样新开页签。

export function slugify(text: string): string {
  return text.toLowerCase()
    .replace(/[`*_~[\]()>#+.!！？?，,。；;：:、"'“”‘’]/g, "")
    .trim()
    .replace(/\s+/g, "-")
}

const CLS = {
  h1: "mb-1.5 mt-2 border-b border-border/60 pb-1 text-base font-semibold text-primary",
  h2: "mb-1 mt-2.5 text-sm font-semibold text-foreground",
  h3: "mb-0.5 mt-2 text-[13px] font-semibold text-foreground/90",
  p: "my-1 leading-relaxed",
  ul: "my-1 list-disc pl-5 leading-relaxed",
  ol: "my-1 list-decimal pl-5 leading-relaxed",
  li: "my-0.5",
  a: "text-primary underline decoration-dotted underline-offset-2",
  code: "rounded bg-card/60 px-1 py-0.5 font-mono text-[11px] text-[#9fe6c8]",
  pre: "my-1.5 overflow-x-auto rounded border bg-card/60 p-2 font-mono text-[11px] leading-relaxed",
  blockquote: "my-1.5 border-l-2 border-primary/50 pl-2 text-muted-foreground",
  table: "my-1.5 border-collapse text-[12px]",
  th: "border bg-card/60 px-2 py-0.5 text-left font-semibold",
  td: "border px-2 py-0.5",
  hr: "my-2 border-border/60",
  strong: "font-semibold text-foreground",
}

export function extractHeadings(markdown: string): { level: number; text: string; slug: string }[] {
  const out: { level: number; text: string; slug: string }[] = []
  const seen = new Map<string, number>()
  let inFence = false
  for (const line of markdown.split(/\r?\n/)) {
    // 围栏开关（``` 或 ~~~，可带语言后缀）；围栏内 # 注释不是标题
    if (/^\s*(```|~~~)/.test(line)) { inFence = !inFence; continue }
    if (inFence) continue
    const m = /^(#{1,3})\s+(.+?)\s*#*\s*$/.exec(line)
    if (!m) continue
    const text = m[2].replace(/[`*_~[\]]/g, "").trim()
    let slug = slugify(text)
    const n = (seen.get(slug) ?? 0) + 1
    seen.set(slug, n)
    if (n > 1) slug = `${slug}-${n}`
    out.push({ level: m[1].length, text, slug })
  }
  return out
}

/** 取标题在文档内的最终 id（与渲染器的去重策略保持一致：仅首次 slug） */
export function headingId(prefix: string, slug: string): string {
  return `md-${prefix}-${slug}`
}

export const MarkdownView = memo(function MarkdownView(
  { content, prefix = "doc", className }:
  { content: string; prefix?: string; className?: string },
) {
  return (
    <div className={className ?? "text-[13px] text-foreground/90"}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => {
            const text = String(children)
            return <h1 id={headingId(prefix, slugify(text))} className={CLS.h1}>{children}</h1>
          },
          h2: ({ children }) => {
            const text = String(children)
            return <h2 id={headingId(prefix, slugify(text))} className={CLS.h2}>{children}</h2>
          },
          h3: ({ children }) => {
            const text = String(children)
            return <h3 id={headingId(prefix, slugify(text))} className={CLS.h3}>{children}</h3>
          },
          p: ({ children }) => <p className={CLS.p}>{children}</p>,
          ul: ({ children }) => <ul className={CLS.ul}>{children}</ul>,
          ol: ({ children }) => <ol className={CLS.ol}>{children}</ol>,
          li: ({ children }) => <li className={CLS.li}>{children}</li>,
          a: ({ href, children }) => (
            <a href={href} className={CLS.a} target="_blank" rel="noreferrer">{children}</a>
          ),
          code: ({ children }) => <code className={CLS.code}>{children}</code>,
          pre: ({ children }) => <pre className={CLS.pre}>{children}</pre>,
          blockquote: ({ children }) => <blockquote className={CLS.blockquote}>{children}</blockquote>,
          table: ({ children }) => <table className={CLS.table}>{children}</table>,
          th: ({ children }) => <th className={CLS.th}>{children}</th>,
          td: ({ children }) => <td className={CLS.td}>{children}</td>,
          hr: () => <hr className={CLS.hr} />,
          strong: ({ children }) => <strong className={CLS.strong}>{children}</strong>,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  )
})
