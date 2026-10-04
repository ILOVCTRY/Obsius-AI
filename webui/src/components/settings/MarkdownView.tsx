import { memo } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

// 只读 Markdown 渲染（深色黑客风）。标题带 slug id 供大纲 scrollIntoView；
// idPrefix 防同页技能正文与 kb 文档撞 id。链接分三路（2026-09-19）：#锚点=页内
// scrollIntoView（原实现一律 target=_blank，点目录会新开首页页签）；.md 相对链接
// =解析相对当前文档路径后经 onOpenKb 打开该 kb 文件；其余（http/https）才新开页签。

/** 解析 md 相对链接为源内 posix 路径（剥 #锚点，. / .. 逐段归一） */
export function resolveKbRel(currentPath: string, href: string): string {
  const segs = currentPath.split("/").slice(0, -1)
  for (const seg of decodeURIComponent(href.split("#")[0]).split("/")) {
    if (seg === "." || seg === "") continue
    if (seg === "..") segs.pop()
    else segs.push(seg)
  }
  return segs.join("/")
}

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
  code: "rounded bg-card/60 px-1 py-0.5 font-mono text-[11px] text-(--viz-code)",
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
  { content, prefix = "doc", className, currentPath, onOpenKb }:
  { content: string; prefix?: string; className?: string;
    /** 当前文档源内路径（.md 相对链接解析基准，kb 预览传入） */
    currentPath?: string
    /** 相对 .md 链接点击时打开目标 kb 文件（不传则退化为新开页签） */
    onOpenKb?: (path: string) => void },
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
          a: ({ href, children }) => {
            const h = href ?? ""
            // #锚点：页内滚动到同 slug 标题（与 extractHeadings/headingId 同口径）
            if (h.startsWith("#")) {
              return (
                <a href={h} className={CLS.a}
                   onClick={(e) => {
                     e.preventDefault()
                     document.getElementById(headingId(prefix, h.slice(1)))
                       ?.scrollIntoView({ block: "start" })
                   }}>
                  {children}
                </a>
              )
            }
            // .md 相对链接：kb 内跳转（交父组件打开目标文件）；
            // 排除带协议/协议相对/纯锚点形态
            if (currentPath && onOpenKb && /\.md($|#)/.test(h) && !/^(#|[a-z][a-z0-9+.-]*:|\/\/)/i.test(h)) {
              const target = resolveKbRel(currentPath, h)
              return (
                <a href={h} className={CLS.a} title={target}
                   onClick={(e) => { e.preventDefault(); onOpenKb(target) }}>
                  {children}
                </a>
              )
            }
            return <a href={h} className={CLS.a} target="_blank" rel="noreferrer">{children}</a>
          },
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
