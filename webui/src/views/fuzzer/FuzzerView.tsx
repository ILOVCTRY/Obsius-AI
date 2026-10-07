import { useCallback, useEffect, useState } from "react"
import { Group, Panel, Separator } from "react-resizable-panels"
import { Copy, Eraser, History, Maximize2, Minimize2, Send, Square, Wrench } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { api, pollJob } from "@/lib/api"
import type { GmStatus, HttpHistoryRow } from "@/lib/types"
import { cn } from "@/lib/utils"
import { toRawRequest } from "@/views/browser/ReplayForm"
import { ResponsePane } from "./ResponsePane"
import { HistoryDrawer } from "./HistoryDrawer"

// Web Fuzzer 重放工作台（2026-10-07）：左 raw 报文编辑器 / 右完整响应，秒级 POC 迭代。
// 传输双路：默认 httpx（显式代理、恒 trust_env=False）；勾「国密TLS」切 gmhttp sidecar。
// 红线：人类 UI 专属（Agent 无任何发起入口，只能只读 http_history）；目标不设门禁。
// 轨门控 pentest/redteam/ctf（与 F6 内置浏览器同口径）。

const TRACKS_OK = new Set(["pentest", "redteam", "ctf"])

function Toggle({ label, on, onChange, disabled, title }: {
  label: string; on: boolean; onChange: (v: boolean) => void
  disabled?: boolean; title?: string
}) {
  return (
    <button type="button" disabled={disabled} title={title}
            className={cn(
              "flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] transition-colors",
              disabled ? "cursor-not-allowed opacity-40"
                : on ? "border-primary bg-primary/15" : "text-muted-foreground hover:bg-accent")}
            onClick={() => { if (!disabled) onChange(!on) }}>
      <span className={cn("flex h-3.5 w-7 shrink-0 items-center rounded-full px-0.5 transition-colors",
                           on && !disabled ? "bg-primary" : "bg-muted")}>
        <span className={cn("block h-2.5 w-2.5 rounded-full bg-background transition-transform",
                             on && !disabled && "translate-x-3.5")} />
      </span>
      {label}
    </button>
  )
}

export function FuzzerView({ pid, track, initialRaw }: {
  pid: string
  track?: string
  initialRaw?: string
}) {
  const trackOk = !track || TRACKS_OK.has(track)
  const [raw, setRaw] = useState(initialRaw ?? "")
  const [busy, setBusy] = useState(false)
  const [runId, setRunId] = useState<string | null>(null)
  const [result, setResult] = useState<HttpHistoryRow | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [builderOpen, setBuilderOpen] = useState(false)

  // 传输选项（默认=与既有行为一致）
  const [forceHttps, setForceHttps] = useState(false)
  const [gmTls, setGmTls] = useState(false)
  const [followRedirects, setFollowRedirects] = useState(true)
  const [proxyOn, setProxyOn] = useState(false)
  const [proxy, setProxy] = useState("")
  const [insecure, setInsecure] = useState(false)
  const [bodyLimitKb, setBodyLimitKb] = useState(1024)

  // 构造请求弹层
  const [bMethod, setBMethod] = useState("GET")
  const [bUrl, setBUrl] = useState("")
  const [bHeaders, setBHeaders] = useState("")
  const [bBody, setBBody] = useState("")

  const [gm, setGm] = useState<GmStatus | null>(null)

  useEffect(() => { if (initialRaw !== undefined) setRaw(initialRaw) }, [initialRaw])

  useEffect(() => {
    let alive = true
    void api.gmStatus(pid)
      .then((s) => { if (alive) setGm(s) })
      .catch(() => { if (alive) setGm({ available: false, path: null, guide: "国密能力探测失败" }) })
    return () => { alive = false }
  }, [pid])

  const send = useCallback(async () => {
    if (!raw.trim() || busy) return
    setBusy(true); setErr(null); setResult(null)
    try {
      const { job_id, run_id } = await api.browserReplay(pid, {
        raw,
        force_https: forceHttps,
        follow_redirects: followRedirects,
        proxy: proxyOn && proxy.trim() ? proxy.trim() : null,
        body_max_bytes: bodyLimitKb > 0 ? bodyLimitKb * 1024 : null,
        insecure,
        gm_tls: gmTls,
        timeout_s: 30,
      })
      setRunId(run_id)
      const job = await pollJob(job_id, () => {}, 700)
      if (job.status === "error") setErr(job.error ?? "重发失败")
      else setResult(job.result as HttpHistoryRow)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false); setRunId(null)
    }
  }, [pid, raw, busy, forceHttps, followRedirects, proxyOn, proxy, bodyLimitKb, insecure, gmTls])

  const stop = async () => {
    if (!runId) return
    try { await api.replayStop(pid, runId) } catch { /* 404=已结束，忽略 */ }
  }

  const buildRaw = () => {
    const lines = [`${bMethod} ${bUrl.trim() || "http://"} HTTP/1.1`]
    for (const ln of bHeaders.split("\n")) { const t = ln.trim(); if (t) lines.push(t) }
    let text = lines.join("\n")
    if (bBody) text += `\n\n${bBody}`
    setRaw(text)
    setBuilderOpen(false)
  }

  if (!trackOk) {
    return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
      重放工作台当前轨道不可用
    </div>
  }

  const requestPane = (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1.5 text-[11px] text-muted-foreground">
        <span className="font-medium text-foreground">Request</span>
        <span className="flex-1" />
        <Button size="sm" variant="ghost" className="h-6 w-6 p-0" title="复制报文"
                onClick={() => void navigator.clipboard.writeText(raw)}><Copy size={12} /></Button>
        <Button size="sm" variant="ghost" className="h-6 w-6 p-0" title="清空"
                onClick={() => setRaw("")}><Eraser size={12} /></Button>
        <Button size="sm" variant="ghost" className="h-6 w-6 p-0"
                title={fullscreen ? "退出全屏" : "全屏编辑"}
                onClick={() => setFullscreen((v) => !v)}>
          {fullscreen ? <Minimize2 size={12} /> : <Maximize2 size={12} />}
        </Button>
      </div>
      <textarea
        className="min-h-0 flex-1 resize-none rounded-none border-0 bg-background p-2 font-mono text-[11px] leading-relaxed outline-none"
        value={raw} spellCheck={false}
        onChange={(e) => setRaw(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.altKey || e.ctrlKey || e.metaKey)) {
            e.preventDefault(); void send()
          }
        }}
        placeholder={"GET http://host/path HTTP/1.1\nHost: host\n\n（请求体放空行之后）\n\nAlt+Enter 发送"}
      />
    </div>
  )

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* 顶部工具条 */}
      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b px-2 py-1.5">
        <Button size="sm" className="h-7 gap-1 text-xs" disabled={busy || !raw.trim()}
                onClick={() => void send()} title="发送（Alt+Enter / Ctrl+Enter）">
          <Send size={13} /> {busy ? "发送中…" : "发送请求"}
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs" disabled={!busy}
                onClick={() => void stop()} title="中断当前请求">
          <Square size={12} /> 停止
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                onClick={() => setBuilderOpen(true)} title="表单式构造请求报文">
          <Wrench size={12} /> 构造请求
        </Button>
        <Button size="sm" variant={historyOpen ? "default" : "outline"} className="h-7 gap-1 text-xs"
                onClick={() => setHistoryOpen((v) => !v)} title="本项目重放历史">
          <History size={12} /> 历史
        </Button>

        <span className="mx-1 h-5 w-px bg-border" />

        <Toggle label="强制HTTPS" on={forceHttps} onChange={setForceHttps} />
        <Toggle label="国密TLS" on={gmTls} onChange={setGmTls}
                disabled={!gm?.available}
                title={gm?.available
                  ? "国密 TLS（GM/T 0024）——走 gmhttp sidecar"
                  : (gm?.guide || "国密 TLS 不可用")} />
        <Toggle label="跟随重定向" on={followRedirects} onChange={setFollowRedirects} />
        <Toggle label="跳过证书校验" on={insecure} onChange={setInsecure} />
        <Toggle label="设置代理" on={proxyOn} onChange={setProxyOn} />
        {proxyOn && (
          <Input className="h-7 w-52 font-mono text-[11px]" value={proxy}
                 onChange={(e) => setProxy(e.target.value)}
                 placeholder="http://127.0.0.1:8080" />
        )}

        <span className="mx-1 h-5 w-px bg-border" />
        <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
          响应体长度限制
          <Input type="number" min={0} className="h-7 w-20 font-mono text-[11px]"
                 value={bodyLimitKb}
                 onChange={(e) => setBodyLimitKb(Math.max(0, +e.target.value || 0))} />
          KB
        </label>
      </div>

      {!gm?.available && gmTls === false && gm && (
        <div className="shrink-0 border-b bg-(--status-paused)/10 px-2 py-1 text-[10px] text-(--status-paused)">
          国密 TLS 不可用：{gm.guide}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <HistoryDrawer pid={pid} open={historyOpen} onClose={() => setHistoryOpen(false)}
                       onPick={(row) => { setRaw(toRawRequest(row)); setResult(row); setErr(null) }} />
        {fullscreen ? (
          <div className="min-w-0 flex-1">{requestPane}</div>
        ) : (
          <Group orientation="horizontal" className="h-full min-w-0 flex-1">
            <Panel defaultSize="50%" minSize="20%" className="min-h-0">{requestPane}</Panel>
            <Separator className="z-10 h-full w-px shrink-0 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary" />
            <Panel defaultSize="50%" minSize="20%" className="min-h-0">
              <ResponsePane row={result} err={err} busy={busy} />
            </Panel>
          </Group>
        )}
      </div>

      <Dialog open={builderOpen} onOpenChange={setBuilderOpen}>
        <DialogContent className="max-w-2xl">
          <DialogTitle>构造请求</DialogTitle>
          <DialogDescription>填好后点「生成报文」写入左侧编辑器（解析仍由后端负责）。</DialogDescription>
          <div className="flex gap-2">
            <select className="h-8 rounded-md border bg-background px-2 font-mono text-xs"
                    value={bMethod} onChange={(e) => setBMethod(e.target.value)}>
              {["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].map((m) => <option key={m}>{m}</option>)}
            </select>
            <Input className="h-8 flex-1 font-mono text-xs" value={bUrl}
                   onChange={(e) => setBUrl(e.target.value)} placeholder="http://host/path?q=1" />
          </div>
          <textarea className="h-24 w-full resize-none rounded-md border bg-background p-2 font-mono text-[11px]"
                    value={bHeaders} spellCheck={false}
                    onChange={(e) => setBHeaders(e.target.value)}
                    placeholder={"每行一个头，如：\nHost: example.com\nContent-Type: application/json"} />
          <textarea className="h-28 w-full resize-none rounded-md border bg-background p-2 font-mono text-[11px]"
                    value={bBody} spellCheck={false}
                    onChange={(e) => setBBody(e.target.value)} placeholder="请求体（可空）" />
          <div className="flex justify-end gap-2">
            <Button size="sm" variant="outline" onClick={() => setBuilderOpen(false)}>取消</Button>
            <Button size="sm" onClick={buildRaw}>生成报文</Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
