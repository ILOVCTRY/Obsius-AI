import { useEffect, useRef, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import { api } from "@/lib/api"
import type { HttpHistoryRow } from "@/lib/types"
import { ReplayForm, toRawRequest } from "./ReplayForm"

// 抓包历史（http_history 3s 增量游标）+ 单条详情弹窗（全量 body + 重发预填）
export function CaptureHistory({ pid }: { pid: string }) {
  const [rows, setRows] = useState<HttpHistoryRow[]>([])
  const [detail, setDetail] = useState<HttpHistoryRow | null>(null)
  const [replayRow, setReplayRow] = useState<HttpHistoryRow | null>(null)
  const cursor = useRef(0)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const next = await api.browserHistory(pid, { since_id: cursor.current, limit: 200 })
        if (!alive || next.length === 0) return
        cursor.current = next[next.length - 1].id
        setRows((prev) => [...prev, ...next].slice(-500)) // 上限防撑爆
      } catch { /* 后端重启等，下一轮再试 */ }
    }
    void load()
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  const openDetail = async (id: number) => {
    try { setDetail(await api.browserHistoryRow(pid, id)) } catch { /* 静默 */ }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between border-b px-2 py-1">
        <span className="text-[10px] font-semibold text-muted-foreground">
          抓包记录（{rows.length}）
        </span>
        <Button size="sm" variant="ghost" className="h-5 text-[10px]"
                onClick={() => { setRows([]); cursor.current = 0 }}>
          清显示
        </Button>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <table className="w-full text-left font-mono text-[10px]">
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="cursor-pointer hover:bg-accent"
                  onClick={() => void openDetail(r.id)}>
                <td className="w-10 px-1 py-0.5">
                  <Badge variant="outline" className="px-1 text-[9px]">{r.source}</Badge>
                </td>
                <td className="w-10 px-1 py-0.5">{r.method}</td>
                <td className="w-8 px-1 py-0.5 text-right">
                  <StatusDot status={r.status} />
                </td>
                <td className="px-1 py-0.5">
                  <span className="block truncate" title={r.url}>{r.url}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollArea>

      <Dialog open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent className="max-w-2xl">
          <DialogTitle className="font-mono text-xs">
            {detail?.method} {detail?.url} → {detail?.status ?? "—"}
          </DialogTitle>
          {detail && <HistoryDetail row={detail} onReplay={() => { setReplayRow(detail); setDetail(null) }} />}
        </DialogContent>
      </Dialog>
      <Dialog open={!!replayRow} onOpenChange={(o) => !o && setReplayRow(null)}>
        <DialogContent className="max-w-2xl">
          <DialogTitle className="text-xs">重发（贴原始报文可改包）</DialogTitle>
          {replayRow && <ReplayForm pid={pid} initialRaw={toRawRequest(replayRow)} />}
        </DialogContent>
      </Dialog>
    </div>
  )
}

function StatusDot({ status }: { status: number | null }) {
  const cls = status == null ? "text-muted-foreground"
    : status < 300 ? "text-(--status-ok)"
    : status < 400 ? "text-(--status-approval)"
    : status < 500 ? "text-(--status-paused)" : "text-red-400"
  return <span className={cls}>{status ?? "?"}</span>
}

function HistoryDetail({ row, onReplay }: { row: HttpHistoryRow; onReplay: () => void }) {
  const kv = (h: Record<string, string>) =>
    Object.entries(h ?? {}).map(([k, v]) => (
      <div key={k} className="flex gap-1"><span className="text-muted-foreground">{k}:</span><span className="break-all">{v}</span></div>
    ))
  return (
    <div className="max-h-[60vh] space-y-2 overflow-auto text-xs">
      <Section title="请求头">{kv(row.req_headers)}</Section>
      {row.req_body && <Section title="请求体">{<pre className="whitespace-pre-wrap break-all">{row.req_body}</pre>}</Section>}
      <Section title="响应头">{kv(row.resp_headers)}</Section>
      {row.resp_body && (
        <Section title={`响应体${row.body_truncated ? "（已截断）" : ""}${row.is_binary ? "（二进制 base64）" : ""}`}>
          <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-all">{row.resp_body}</pre>
        </Section>
      )}
      <div className="flex justify-end">
        <Button size="sm" variant="outline" onClick={onReplay}>✉ 重发…</Button>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-0.5 text-[10px] font-semibold text-muted-foreground">{title}</div>
      <div className="rounded border p-1 font-mono text-[10px]">{children}</div>
    </div>
  )
}
