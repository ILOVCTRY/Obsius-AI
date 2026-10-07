import { useEffect, useMemo, useRef, useState } from "react"
import { Search } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
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

/** 响应区：状态行 + 「响应头 / 响应体」子 tab + JSON 美化 + 字符集 + 定位。 */
export function ResponsePane({ row, err, busy }: {
  row: HttpHistoryRow | null
  err: string | null
  busy: boolean
}) {
  const [tab, setTab] = useState("body")
  const [pretty, setPretty] = useState(false)
  const [charset, setCharset] = useState("UTF-8")
  const [find, setFind] = useState("")
  const bodyRef = useRef<HTMLTextAreaElement | null>(null)

  // 每次新响应回到「响应体 · 原文」，避免上一条的美化/字符集选择造成误读
  useEffect(() => { setTab("body"); setPretty(false); setFind("") }, [row?.id])

  const headersText = useMemo(() => {
    if (!row) return ""
    return Object.entries(row.resp_headers ?? {}).map(([k, v]) => `${k}: ${v}`).join("\n")
  }, [row])

  const decoded = useMemo(() => decodeWith(row?.resp_body ?? "", charset), [row?.resp_body, charset])
  const prettyResult = useMemo(() => tryPretty(decoded), [decoded])
  const bodyText = pretty && prettyResult.ok ? prettyResult.text : decoded

  const isJson = prettyResult.ok
  const failed = row != null && row.status == null
  const placeholder = row?.is_binary ? decoded : null

  const locate = () => {
    const el = bodyRef.current
    if (!el || !find) return
    const idx = bodyText.toLowerCase().indexOf(find.toLowerCase())
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
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-2 py-1.5 text-xs">
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
                disabled={!isJson} title={isJson ? "JSON 美化" : "非 JSON，不可美化"}
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

      <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
        <TabsList className="m-2 shrink-0 self-start">
          <TabsTrigger value="body">响应体</TabsTrigger>
          <TabsTrigger value="headers">响应头</TabsTrigger>
        </TabsList>
        <TabsContent value="body" className="min-h-0 flex-1 px-2 pb-2">
          {placeholder ? (
            <div className="flex h-full items-center justify-center rounded border border-dashed text-[11px] text-muted-foreground">
              {placeholder}（二进制响应，不渲染正文）
            </div>
          ) : (
            <textarea ref={bodyRef} readOnly spellCheck={false}
                      className="h-full w-full resize-none rounded-md border bg-background p-2 font-mono text-[11px] leading-relaxed outline-none"
                      value={bodyText} />
          )}
        </TabsContent>
        <TabsContent value="headers" className="min-h-0 flex-1 px-2 pb-2">
          <textarea readOnly spellCheck={false}
                    className="h-full w-full resize-none rounded-md border bg-background p-2 font-mono text-[11px] leading-relaxed outline-none"
                    value={headersText} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
