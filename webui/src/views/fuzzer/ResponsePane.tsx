import { useEffect, useMemo, useRef, useState } from "react"
import { Search } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import type { HttpHistoryRow } from "@/lib/types"
import { cn } from "@/lib/utils"

/** 可选字符集（TextDecoder 支持；后端以 utf-8 解码入库，故可按字节无损还原再重解） */
const CHARSETS = ["UTF-8", "GBK", "GB18030", "Big5", "Shift_JIS", "ISO-8859-1"]

/** 按目标字符集重解：resp_body 由后端 utf-8 解码得来 → TextEncoder 可无损还原原始字节，
 *  再用 TextDecoder 按所选字符集重新解释（乱码场景的救场）。UTF-8 恒原样返回。 */
function decodeWith(text: string, charset: string): string {
  if (!text || charset === "UTF-8") return text
  try {
    const bytes = new TextEncoder().encode(text)
    return new TextDecoder(charset).decode(bytes)
  } catch {
    return text
  }
}

/** JSON 美化（仅当整体是合法 JSON；失败回原文，调用方据 ok 决定是否置灰按钮） */
function tryPretty(text: string): { text: string; ok: boolean } {
  const t = text.trim()
  if (!t || !(t.startsWith("{") || t.startsWith("["))) return { text, ok: false }
  try {
    return { text: JSON.stringify(JSON.parse(t), null, 2), ok: true }
  } catch {
    return { text, ok: false }
  }
}

function byteLen(text: string): number {
  try { return new TextEncoder().encode(text).length } catch { return text.length }
}

/** 响应区：**完整原始响应报文**单栏呈现（状态行 + 响应头 + 空行 + 响应体，对齐 Burp/Yakit），
 *  不再用「响应头 / 响应体」tab 拆分；保留 JSON 美化（仅作用体段）+ 字符集重解 + 定位。 */
export function ResponsePane({ row, err, busy }: {
  row: HttpHistoryRow | null
  err: string | null
  busy: boolean
}) {
  const [pretty, setPretty] = useState(false)
  const [charset, setCharset] = useState("UTF-8")
  const [find, setFind] = useState("")
  const ref = useRef<HTMLTextAreaElement | null>(null)

  // 每次新响应回到「原文 · UTF-8」，避免上一条的美化/字符集选择造成误读
  useEffect(() => { setPretty(false); setFind("") }, [row?.id])

  const decoded = useMemo(() => decodeWith(row?.resp_body ?? "", charset), [row?.resp_body, charset])
  const prettyResult = useMemo(() => tryPretty(decoded), [decoded])
  const bodyText = pretty && prettyResult.ok ? prettyResult.text : decoded

  const isJson = prettyResult.ok
  const failed = row != null && row.status == null

  // 完整报文：状态行 + 头 + 空行 + 体（体段受美化/字符集影响，头段恒原样）
  const raw = useMemo(() => {
    if (!row) return ""
    const statusLine = row.status != null ? `HTTP/1.1 ${row.status}` : "HTTP/1.1 (无响应)"
    const hs = Object.entries(row.resp_headers ?? {}).map(([k, v]) => `${k}: ${v}`).join("\n")
    const body = row.is_binary
      ? `${decoded}（二进制响应，不渲染正文）`
      : bodyText
    let out = statusLine + (hs ? "\n" + hs : "")
    if (body) out += "\n\n" + body
    return out
  }, [row, decoded, bodyText])

  const locate = () => {
    const el = ref.current
    if (!el || !find) return
    const idx = raw.toLowerCase().indexOf(find.toLowerCase())
    if (idx < 0) return
    el.focus()
    el.setSelectionRange(idx, idx + find.length)
  }

  if (!row && !err && !busy) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
        发送请求后在此查看响应
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-2 py-1.5 text-[11px]">
        {busy && <span className="text-muted-foreground">请求中…</span>}
        {row && (
          <>
            <span className={cn("font-mono font-bold", failed ? "text-red-400" : "text-(--status-ok)")}>
              {row.status ?? "失败"}
            </span>
            <span className="text-muted-foreground">{row.duration_ms ?? "?"}ms</span>
            <span className="text-muted-foreground">{byteLen(row.resp_body ?? "")} B</span>
            {row.meta?.gm_tls === true && <Badge variant="outline">国密</Badge>}
            {typeof row.meta?.proxy === "string" && <Badge variant="outline">代理</Badge>}
            {row.body_truncated && <Badge variant="outline">已截断</Badge>}
            {row.is_binary && <Badge variant="outline">二进制</Badge>}
          </>
        )}
        <span className="flex-1" />
        <Button size="sm" variant={pretty ? "default" : "outline"} className="h-6 text-[10px]"
                disabled={!isJson} title={isJson ? "JSON 美化（仅体段）" : "非 JSON，不可美化"}
                onClick={() => setPretty((v) => !v)}>美化</Button>
        <select className="h-6 rounded-md border bg-background px-1 font-mono text-[10px]"
                value={charset} onChange={(e) => setCharset(e.target.value)}>
          {CHARSETS.map((c) => <option key={c}>{c}</option>)}
        </select>
        <div className="flex items-center gap-1">
          <Input className="h-6 w-40 font-mono text-[10px]" value={find}
                 onChange={(e) => setFind(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") locate() }}
                 placeholder="输入定位内容" />
          <Button size="sm" variant="outline" className="h-6 w-6 p-0" onClick={locate} title="定位">
            <Search size={12} />
          </Button>
        </div>
      </div>

      {err && <div className="shrink-0 border-b bg-red-500/10 px-2 py-1 text-[11px] text-red-400">{err}</div>}
      {failed && typeof row?.meta?.error === "string" && (
        <div className="shrink-0 border-b bg-red-500/10 px-2 py-1 text-[11px] text-red-400">
          {row.meta.error}
        </div>
      )}

      <textarea ref={ref} readOnly spellCheck={false}
                className="min-h-0 flex-1 resize-none border-0 bg-background p-2 font-mono text-[11px] leading-relaxed outline-none"
                value={raw} />
    </div>
  )
}
