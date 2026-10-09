import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Activity, Globe, Play, RefreshCw, Square, Trash2, Upload, Zap,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { api, ApiError, pollJob } from "@/lib/api"
import type { ProxyJobProgress, ProxyRecord, ProxyStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

/** 代理池（fir-proxy 托管，2026-10-07）。
 *
 * 项目级 serve（本地 HTTP + SOCKS5 入口，内建轮换/故障切换）+ 池记录管理。
 * 人类与 AI 共享同一池：AI 经 MCP 控制面取端点自行 `curl -x`。
 * 池空/依赖缺失 → 后端 503 结构化，本页直显错误文案（不静默降级）。
 */
export function ProxyPoolView({ pid }: { pid: string }) {
  const [status, setStatus] = useState<ProxyStatus | null>(null)
  const [records, setRecords] = useState<ProxyRecord[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importText, setImportText] = useState("")
  const [jobNote, setJobNote] = useState<string | null>(null)
  const [progress, setProgress] = useState<ProxyProgress | null>(null)
  const jobRef = useRef<string | null>(null)   // 正在跟踪的 job id（防重复挂载）

  const refresh = useCallback(async () => {
    try {
      const [st, list] = await Promise.all([
        api.proxyStatus(pid), api.proxyList(pid),
      ])
      setStatus(st)
      setRecords(list.proxies)
      setErr(null)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    }
  }, [pid])

  useEffect(() => {
    let alive = true
    void (async () => { if (alive) await refresh() })()
    const timer = window.setInterval(() => { if (alive) void refresh() }, 3000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [refresh])

  const guard = async (fn: () => Promise<unknown>, note?: string) => {
    setBusy(true); setErr(null); setJobNote(null)
    try {
      await fn()
      if (note) setJobNote(note)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
      setProgress(null)
      void refresh()
    }
  }

  /** 跟踪一个抓取/验证 Job：轮询进度 → 收尾回执。首次发起与切页重挂共用。 */
  const runJob = useCallback(async (jobId: string, kind: string) => {
    jobRef.current = jobId
    setBusy(true)
    setProgress({ phase: "queued", message: "排队中…" })
    try {
      const job = await pollJob(jobId, (j) =>
        setProgress((j.meta?.progress as ProxyJobProgress | undefined) ?? null))
      if (job.status === "error") setErr(`任务失败：${job.error ?? ""}`)
      else setJobNote(noteFor(kind, job.result))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      jobRef.current = null
      setProgress(null)
      setBusy(false)
      void refresh()
    }
  }, [refresh])

  // 切页回来：/proxy/status 带出运行中的 job → 重挂轮询（进度不丢）
  useEffect(() => {
    const j = status?.job
    if (!j || jobRef.current === j.id) return
    void runJob(j.id, j.kind)
  }, [status?.job?.id, status?.job?.kind, runJob])

  const running = status?.running ?? false
  const endpoint = status?.endpoint

  const regionText = useMemo(() => {
    const entries = Object.entries(status?.regions ?? {})
    if (!entries.length) return "—"
    return entries.sort((a, b) => b[1] - a[1]).slice(0, 6)
      .map(([k, v]) => `${k} ${v}`).join(" · ")
  }, [status])

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* 顶部工具条 */}
      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b px-2 py-1.5">
        {running ? (
          <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                  disabled={busy} onClick={() => void guard(() => api.proxyStop(pid), "已停止")}>
            <Square size={12} /> 停止服务
          </Button>
        ) : (
          <Button size="sm" className="h-7 gap-1 text-xs" disabled={busy}
                  onClick={() => void guard(() => api.proxyStart(pid), "服务已启动")}
                  title="启动本地 HTTP + SOCKS5 代理入口">
            <Play size={12} /> 启动服务
          </Button>
        )}
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                disabled={busy || !running} onClick={() => void guard(() => api.proxyRotate(pid), "已轮换")}>
          <Zap size={12} /> 轮换 IP
        </Button>

        <span className="mx-1 h-5 w-px bg-border" />

        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs" disabled={busy}
                title="从在线源抓取并自动验证，剔除失效代理"
                onClick={() => void guard(async () => {
                  const { job_id } = await api.proxyFetch(pid)
                  await runJob(job_id, "proxy-fetch")
                })}>
          <Upload size={12} /> 在线抓取
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                disabled={busy || !records.length}
                title="验证池内全部代理并移除失效代理"
                onClick={() => void guard(async () => {
                  const { job_id } = await api.proxyValidate(pid)
                  await runJob(job_id, "proxy-validate")
                })}>
          <Activity size={12} /> 验证全部
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                onClick={() => setImportOpen((v) => !v)}>
          <Globe size={12} /> 导入
        </Button>
        <Button size="sm" variant="ghost" className="h-7 gap-1 text-xs"
                disabled={busy} onClick={() => void refresh()}>
          <RefreshCw size={12} /> 刷新
        </Button>

        <span className="ml-auto text-[11px] text-muted-foreground">
          池 {status?.pool_size ?? 0} · 可用 {status?.working ?? 0}
        </span>
      </div>

      {/* 服务状态条 */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-b px-3 py-2 text-[11px]">
        <span className="flex items-center gap-1.5">
          <span className={cn("inline-block h-2 w-2 rounded-full",
                              running ? "bg-(--status-ok)" : "bg-muted-foreground")} />
          {running ? "服务运行中" : "服务未启动"}
        </span>
        {endpoint && (
          <>
            <span className="text-muted-foreground">HTTP
              <code className="ml-1 rounded bg-muted px-1.5 py-0.5 font-mono">{endpoint.http}</code>
            </span>
            <span className="text-muted-foreground">SOCKS5
              <code className="ml-1 rounded bg-muted px-1.5 py-0.5 font-mono">{endpoint.socks5}</code>
            </span>
          </>
        )}
        <span className="text-muted-foreground">当前代理
          <code className="ml-1 rounded bg-muted px-1.5 py-0.5 font-mono">
            {status?.current?.proxy ?? "—"}
          </code>
        </span>
        <span className="text-muted-foreground">地区 {regionText}</span>
      </div>

      {err && (
        <div className="shrink-0 border-b border-destructive/40 bg-destructive/10 px-3 py-1.5 text-[11px] text-destructive">
          {err}
        </div>
      )}
      {progress && !err && <ProxyProgressBar p={progress} />}
      {jobNote && !err && !progress && (
        <div className="shrink-0 border-b bg-muted/40 px-3 py-1.5 text-[11px] text-muted-foreground">
          {jobNote}
        </div>
      )}

      {importOpen && (
        <div className="shrink-0 border-b px-3 py-2">
          <textarea
            className="h-24 w-full resize-none rounded-md border bg-transparent p-2 font-mono text-[11px] outline-none focus:border-primary"
            placeholder={"每行一个代理：protocol://host:port 或 host:port\n如 socks5://1.2.3.4:1080 / 1.2.3.4:8080"}
            value={importText} onChange={(e) => setImportText(e.target.value)} />
          <div className="mt-1.5 flex gap-1.5">
            <Button size="sm" className="h-7 text-xs" disabled={busy || !importText.trim()}
                    onClick={() => void guard(async () => {
                      const records = parseProxies(importText)
                      if (!records.length) throw new ApiError(0, "没有可识别的代理行", "")
                      const r = await api.proxyAdd(pid, records)
                      setImportText(""); setImportOpen(false)
                      setJobNote(`已导入 ${r.added} 条`)
                    })}>
              导入到池
            </Button>
            <Button size="sm" variant="ghost" className="h-7 text-xs"
                    onClick={() => setImportOpen(false)}>取消</Button>
          </div>
        </div>
      )}

      {/* 池列表 */}
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-[11px]">
          <thead className="sticky top-0 z-10 bg-(--surface-1) text-muted-foreground">
            <tr className="border-b">
              <th className="px-3 py-1.5 text-left font-medium">代理</th>
              <th className="px-2 py-1.5 text-left font-medium">协议</th>
              <th className="px-2 py-1.5 text-left font-medium">地区</th>
              <th className="px-2 py-1.5 text-right font-medium">延迟</th>
              <th className="px-2 py-1.5 text-right font-medium">评分</th>
              <th className="px-2 py-1.5 text-left font-medium">匿名度</th>
              <th className="px-2 py-1.5 text-left font-medium">状态</th>
              <th className="px-2 py-1.5" />
            </tr>
          </thead>
          <tbody>
            {records.map((r) => {
              const current = r.proxy === status?.current?.proxy
              return (
                <tr key={r.proxy}
                    className={cn("border-b border-border/50 hover:bg-accent/40",
                                  current && "bg-primary/10")}>
                  <td className="px-3 py-1 font-mono">
                    {current && <span className="mr-1 text-(--status-ok)">●</span>}
                    {r.proxy}
                  </td>
                  <td className="px-2 py-1 text-muted-foreground">{r.protocol ?? "—"}</td>
                  <td className="px-2 py-1">{r.location ?? "—"}</td>
                  <td className="px-2 py-1 text-right font-mono">
                    {typeof r.latency === "number" ? `${Math.round(r.latency * 1000)}ms` : "—"}
                  </td>
                  <td className="px-2 py-1 text-right font-mono">
                    {typeof r.score === "number" ? r.score.toFixed(0) : "—"}
                  </td>
                  <td className="px-2 py-1 text-muted-foreground">{r.anonymity ?? "—"}</td>
                  <td className="px-2 py-1">
                    <Badge variant={r.status === "Working" ? "secondary" : "outline"}
                           className="text-[10px]">
                      {r.status ?? "—"}
                    </Badge>
                  </td>
                  <td className="px-2 py-1 text-right">
                    <button type="button" title="从池中移除"
                            className="text-muted-foreground hover:text-destructive"
                            onClick={() => void guard(
                              () => api.proxyRemove(pid, [r.proxy]))}>
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              )
            })}
            {!records.length && (
              <tr>
                <td colSpan={8} className="px-3 py-10 text-center text-muted-foreground">
                  代理池为空——用「在线抓取」拉取，或「导入」粘贴代理列表
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/** 解析粘贴的代理列表：`protocol://host:port` / `protocol,host:port` / `host:port`。 */
function parseProxies(text: string): ProxyRecord[] {
  const out: ProxyRecord[] = []
  const seen = new Set<string>()
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith("#")) continue
    let protocol = "http"
    let address = line
    if (line.includes("://")) {
      const idx = line.indexOf("://")
      protocol = line.slice(0, idx).toLowerCase()
      address = line.slice(idx + 3)
    } else if (line.includes(",")) {
      const idx = line.indexOf(",")
      protocol = line.slice(0, idx).trim().toLowerCase()
      address = line.slice(idx + 1).trim()
    }
    address = address.trim()
    if (!address.includes(":")) continue
    const key = `${protocol}|${address}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ protocol, proxy: address, status: "Working" })
  }
  return out
}

/** 抓取 Job 结果（抓取后自动验证+清理）。 */
interface ProxyFetchResult {
  fetched: number
  validated: number
  removed: number
  added: number
  pool_size: number
}

/** 验证 Job 结果（验证并清理失效）。 */
interface ProxyValidateResult {
  validated: number
  working: number
  removed: number
  pool_size: number
}

/** Job 收尾回执文案（首次发起与切页重挂共用）。 */
function noteFor(kind: string, result: unknown): string {
  if (kind === "proxy-fetch") {
    const r = result as ProxyFetchResult | null
    return r
      ? `抓取 ${r.fetched} · 验证 ${r.validated} · 清理 ${r.removed} · 入池 ${r.added}（池 ${r.pool_size}）`
      : "抓取完成"
  }
  const r = result as ProxyValidateResult | null
  return r ? `验证完成：可用 ${r.working} · 清理 ${r.removed}（池 ${r.pool_size}）` : "验证完成"
}

/** 抓取/验证 Job 的实时进度（后端 `meta.progress`，可变 dict 引用，轮询即读最新值）。 */
type ProxyProgress = ProxyJobProgress

const PHASE_LABEL: Record<string, string> = {
  queued: "排队中", fetch: "抓取中", check: "验证中", prune: "清理中", done: "完成",
}

/** 秒 → 中文时长：`45 秒` / `1 分 20 秒` / `1 时 5 分`。 */
function fmtEta(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  const r = s % 60
  if (m < 60) return r ? `${m} 分 ${r} 秒` : `${m} 分`
  return `${Math.floor(m / 60)} 时 ${m % 60} 分`
}

/** 抓取/验证状态栏：相位 + 进度条（分母未知时不确定态）+ 预计剩余 + 已用。 */
function ProxyProgressBar({ p }: { p: ProxyProgress }) {
  const total = p.total ?? 0
  const done = p.done ?? 0
  const known = total > 0
  const pct = known ? Math.min(100, Math.round((done / total) * 100)) : 0
  const eta = typeof p.eta_seconds === "number" ? p.eta_seconds : null
  const elapsed = typeof p.elapsed_seconds === "number" ? p.elapsed_seconds : 0
  return (
    <div className="shrink-0 border-b bg-muted/40 px-3 py-1.5 text-[11px]">
      <div className="flex items-center gap-2">
        <span className="font-medium text-muted-foreground">
          {PHASE_LABEL[p.phase ?? ""] ?? "处理中"}
        </span>
        {known ? (
          <>
            <div className="h-1.5 w-40 overflow-hidden rounded-full bg-border">
              <div className="h-full rounded-full bg-primary transition-all"
                   style={{ width: `${pct}%` }} />
            </div>
            <span className="tabular-nums text-muted-foreground">{done}/{total}</span>
          </>
        ) : (
          <span className="text-muted-foreground">{p.message ?? "进行中…"}</span>
        )}
        {eta !== null && eta >= 0 && (
          <span className="text-muted-foreground">预计剩余 {fmtEta(eta)}</span>
        )}
        {elapsed > 0 && (
          <span className="text-muted-foreground">已用 {fmtEta(elapsed)}</span>
        )}
      </div>
    </div>
  )
}
