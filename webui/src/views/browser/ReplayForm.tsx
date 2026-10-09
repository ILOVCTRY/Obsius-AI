import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { api, pollJob } from "@/lib/api"
import type { HttpHistoryRow, IntruderPayloadSpec } from "@/lib/types"

/** 抓包行 → 完整原始请求报文文本（纯格式化非解析；重发预填用）。
 *  F6-v3：重发表单只收原始报文，解析在后端（httpmsg.parse_raw_request）。 */
export function toRawRequest(row: HttpHistoryRow): string {
  const lines = [`${row.method} ${row.url} HTTP/1.1`]
  for (const [k, v] of Object.entries(row.req_headers ?? {})) lines.push(`${k}: ${v}`)
  let raw = lines.join("\n")
  if (row.req_body) raw += `\n\n${row.req_body}`
  return raw
}

// 重发表单（F6-v3 重写）：单一原始报文框 → 202 job → pollJob 轮询 → 结果行。
// 解析/改包校验全在后端；坏报文 422 直显。
export function ReplayForm({ pid, initialRaw }: {
  pid: string
  initialRaw?: string
}) {
  const [raw, setRaw] = useState(initialRaw ?? "")
  const [result, setResult] = useState<HttpHistoryRow | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const submit = async () => {
    setBusy(true); setErr(null); setResult(null)
    try {
      const { job_id } = await api.browserReplay(pid, { raw })
      const job = await pollJob(job_id, () => {}, 1000)
      if (job.status === "error") setErr(job.error ?? "重发失败")
      else setResult(job.result as HttpHistoryRow)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  return (
    <div className="space-y-2 text-xs">
      <textarea
        className="h-64 w-full rounded-md border bg-background p-1 font-mono text-[10px]"
        value={raw} onChange={(e) => setRaw(e.target.value)}
        spellCheck={false}
        placeholder={"GET http://…/path HTTP/1.1\nHost: …\n\n（请求体放空行之后）"}
      />
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={busy || !raw.trim()} onClick={() => void submit()}>
          {busy ? "发送中…" : "发送"}
        </Button>
        {err && <span className="text-red-400">{err}</span>}
      </div>
      {result && (
        <div className="rounded border p-1 font-mono text-[10px]">
          <div className="flex gap-2">
            <Badge variant={result.meta?.modified ? "default" : "outline"}>
              {result.meta?.modified ? "已修改" : "原样"}
            </Badge>
            <span className="font-bold">{result.status ?? "失败"}</span>
            <span className="text-muted-foreground">{result.duration_ms ?? "?"}ms</span>
            <Button size="sm" variant="outline" className="ml-auto h-6 text-[10px]"
              onClick={() => void navigator.clipboard.writeText(raw)}>复制为 HTTP POC</Button>
          </div>
          {result.resp_body && (
            <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all">{result.resp_body}</pre>
          )}
        </div>
      )}
    </div>
  )
}

// 爆破表单（人类 UI 专属，红线：Agent 无发起入口）：§POS§ 标记 + payload 集 + 停止钮
export function IntruderForm({ pid }: { pid: string }) {
  const [method, setMethod] = useState("GET")
  const [url, setUrl] = useState("")
  const [body, setBody] = useState("")
  const [payloads, setPayloads] = useState<IntruderPayloadSpec[]>([])
  const [newPos, setNewPos] = useState("")
  const [newVals, setNewVals] = useState("")
  const [concurrency, setConcurrency] = useState(5)
  const [rate, setRate] = useState(10)
  const [proxy, setProxy] = useState("")
  const [batchId, setBatchId] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [done, setDone] = useState<{ total: number; failed: number; stopped: boolean } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const start = async () => {
    setRunning(true); setDone(null); setErr(null)
    try {
      const r = await api.browserIntruder(pid, {
        template: { method, url, body: body || undefined },
        payloads, concurrency, rate_per_sec: rate,
        proxy: proxy.trim() || undefined,
      })
      setBatchId(r.batch_id)
      const job = await pollJob(r.job_id, () => {}, 1500)
      setRunning(false)
      if (job.status === "error") setErr(job.error ?? "爆破失败")
      else {
        const s = job.result as { total?: number; failed?: number; stopped?: boolean }
        setDone({ total: s.total ?? 0, failed: s.failed ?? 0, stopped: !!s.stopped })
      }
    } catch (e) {
      setRunning(false)
      setErr(e instanceof Error ? e.message : String(e))
    }
  }
  const stop = async () => {
    if (!batchId) return
    try { await api.intruderStop(pid, batchId) } catch { /* 已结束 404 忽略 */ }
  }

  const hasMarker = /§[A-Za-z0-9_]+§/.test(url) || /§[A-Za-z0-9_]+§/.test(body)

  return (
    <div className="space-y-2 text-xs">
      <div className="flex gap-1">
        <select value={method} onChange={(e) => setMethod(e.target.value)}
                className="rounded-md border bg-background px-1 py-1 font-mono text-xs">
          {["GET", "POST"].map((m) => <option key={m}>{m}</option>)}
        </select>
        <Input className="flex-1 font-mono text-xs" value={url} onChange={(e) => setUrl(e.target.value)}
               placeholder="http://…/login?u=§U§&p=§P§" />
      </div>
      <textarea className="h-14 w-full rounded-md border bg-background p-1 font-mono text-[10px]"
                value={body} onChange={(e) => setBody(e.target.value)}
                placeholder="请求体，可用 §标记§（如 user=§U§&pass=§P§）" />
      {/* payload 集编辑器 */}
      <div className="space-y-1">
        {payloads.map((p, i) => (
          <div key={i} className="flex items-center gap-1 rounded border px-1 py-0.5 font-mono text-[10px]">
            <Badge variant="outline">§{p.position}§</Badge>
            <span className="text-muted-foreground">
              {p.type === "list" ? `${p.values?.length ?? 0} 个值` : `${p.start}–${p.stop}`}
            </span>
            <span className="flex-1" />
            <button className="text-red-400" onClick={() => setPayloads(payloads.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <div className="flex gap-1">
          <Input className="w-20 font-mono text-[10px]" value={newPos}
                 onChange={(e) => setNewPos(e.target.value)} placeholder="标记名" />
          <Input className="flex-1 font-mono text-[10px]" value={newVals}
                 onChange={(e) => setNewVals(e.target.value)}
                 placeholder="逗号分隔候选，或 1-100 区间" />
          <Button size="sm" variant="outline" className="h-6 text-[10px]"
                  onClick={() => {
                    if (!newPos.trim() || !newVals.trim()) return
                    const range = newVals.match(/^(\d+)\s*-\s*(\d+)$/)
                    setPayloads([...payloads, range
                      ? { position: newPos.trim(), type: "range", start: +range[1], stop: +range[2] }
                      : { position: newPos.trim(), type: "list",
                          values: newVals.split(",").map((s) => s.trim()).filter(Boolean) }])
                    setNewPos(""); setNewVals("")
                  }}>＋</Button>
        </div>
      </div>
      <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
        并发
        <Input type="number" min={1} max={5} className="h-6 w-14" value={concurrency}
               onChange={(e) => setConcurrency(Math.min(5, Math.max(1, +e.target.value || 1)))} />
        速率/秒
        <Input type="number" min={1} className="h-6 w-16" value={rate}
               onChange={(e) => setRate(Math.max(1, +e.target.value || 1))} />
      </div>
      <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
        代理
        <Input className="h-6 flex-1 font-mono text-[10px]" value={proxy}
               onChange={(e) => setProxy(e.target.value)}
               placeholder="显式代理，如 127.0.0.1:1801（代理池入口，做 IP 轮换）；留空直连" />
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={running || !hasMarker || !url.trim() || payloads.length === 0}
                onClick={() => void start()}>
          {running ? "爆破中…" : "开始爆破"}
        </Button>
        {running && batchId && (
          <Button size="sm" variant="outline" onClick={() => void stop()}>⏹ 停止</Button>
        )}
        {done && (
          <span className={done.failed > 0 ? "text-(--status-paused)" : "text-(--status-ok)"}>
            完成 {done.total} 条{done.failed > 0 ? `（失败 ${done.failed}）` : ""}{done.stopped ? " · 已提前停止" : ""}
          </span>
        )}
        {err && <span className="text-red-400">{err}</span>}
        {!hasMarker && <span className="text-muted-foreground">url/body 需含 §标记§</span>}
      </div>
      {batchId && (
        <div className="text-[10px] text-muted-foreground">
          批次 <span className="font-mono">{batchId}</span> 的逐请求结果在「抓包」tab 按 source=intruder 查看
        </div>
      )}
    </div>
  )
}
