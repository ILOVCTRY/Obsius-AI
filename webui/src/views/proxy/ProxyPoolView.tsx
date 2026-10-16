import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Activity, Globe, Play, RefreshCw, Settings2, ShieldAlert, Square, Trash2, Upload, Zap,
} from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { api, ApiError, pollJob } from "@/lib/api"
import type {
  ProxyCheckUrlResult, ProxyConfigView, ProxyJobProgress, ProxyRecord, ProxyStatus,
  ProxyUrlCheckRow,
} from "@/lib/types"
import { cn } from "@/lib/utils"

/** 代理池（fir-proxy 托管，2026-10-07 → 2026-10-11 全局单池）。
 *
 * 全局 serve（本地 HTTP + SOCKS5 入口，内建轮换/故障切换）+ 池记录管理。
 * 人类与 AI 共享同一池：AI 经 MCP 控制面取端点自行 `curl -x`。
 * 池空/依赖缺失 → 后端 503 结构化，本页直显错误文案（不静默降级）。
 * 协议过滤（2026-10-11）：http / https（能力探测）/ socks（socks4+socks5）。
 */

/** 协议过滤维度：https 是**能力探测**（非协议类目，池本无 https 代理）。 */
type ProxyFilter = "all" | "http" | "https" | "socks"

const FILTERS: { key: ProxyFilter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "http", label: "HTTP" },
  { key: "https", label: "HTTPS" },
  { key: "socks", label: "SOCKS" },
]

/** 「单次抓取条数」本地缓存键（2026-10-15）。
 *
 * 权威值在 `config/proxy.json` 的 `fetch_limit`（后端「网站设置」可改、自动巡检同用）；
 * 本地缓存只用于首屏立刻显示 + 读配置失败时兜底。 */
const FETCH_LIMIT_KEY = "obsius.proxy.fetchLimit"

/** 单次抓取条数的内置缺省（与后端 `ProxyConfig.fetch_limit` 默认一致）。 */
const FETCH_LIMIT_DEFAULT = 3000

/** 读本地缓存的抓取条数；无/非法/无 localStorage 返回 null。 */
function readCachedFetchLimit(): number | null {
  try {
    const raw = window.localStorage.getItem(FETCH_LIMIT_KEY)
    if (raw === null) return null
    const n = Number(raw)
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null
  } catch {
    return null
  }
}
export function ProxyPoolView({ pid }: { pid?: string | null }) {
  const [status, setStatus] = useState<ProxyStatus | null>(null)
  const [records, setRecords] = useState<ProxyRecord[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [importText, setImportText] = useState("")
  const [jobNote, setJobNote] = useState<string | null>(null)
  const [progress, setProgress] = useState<ProxyProgress | null>(null)
  const [filter, setFilter] = useState<ProxyFilter>("all")   // 协议过滤（2026-10-11）
  const [checkOpen, setCheckOpen] = useState(false)          // 访问测试弹窗（2026-10-12）
  const [configOpen, setConfigOpen] = useState(false)        // 网站设置弹窗（2026-10-12）
  // 单次抓取条数（2026-10-15）：`fetchLimit` 是生效值（点「在线抓取」时随请求发出），
  // `limitText` 是输入框文本（允许中途为空/半截，失焦或回车才落定）。
  const [fetchLimit, setFetchLimit] = useState<number>(
    () => readCachedFetchLimit() ?? FETCH_LIMIT_DEFAULT)
  const [limitText, setLimitText] = useState<string>(
    () => String(readCachedFetchLimit() ?? FETCH_LIMIT_DEFAULT))
  const jobRef = useRef<string | null>(null)   // 正在跟踪的 job id（防重复挂载）

  const refresh = useCallback(async () => {
    try {
      const [st, list] = await Promise.all([
        api.proxyStatus(), api.proxyList(),
      ])
      setStatus(st)
      setRecords(list.proxies)
      setErr(null)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    let alive = true
    void (async () => { if (alive) await refresh() })()
    const timer = window.setInterval(() => { if (alive) void refresh() }, 3000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [refresh])

  // 抓取条数初值（2026-10-15）：以 config/proxy.json 的 `fetch_limit` 为准（网站设置同源）；
  // 读不到时保留本地缓存/内置默认，不打扰用户。
  useEffect(() => {
    let alive = true
    api.proxyConfigGet()
      .then((c) => {
        if (!alive || typeof c.fetch_limit !== "number") return
        setFetchLimit(c.fetch_limit)
        setLimitText(String(c.fetch_limit))
      })
      .catch(() => { /* 读配置失败：沿用本地缓存值 */ })
    return () => { alive = false }
  }, [])

  /** 落定输入框的抓取条数并返回生效值：写本地缓存 + 回写 config（静默；请求直接用返回值，不等落盘）。 */
  const commitFetchLimit = useCallback((): number => {
    const raw = limitText.trim()
    const parsed = raw === "" ? fetchLimit : Number(raw)
    const value = Number.isFinite(parsed) ? Math.max(0, Math.floor(parsed)) : fetchLimit
    setLimitText(String(value))
    if (value !== fetchLimit) {
      setFetchLimit(value)
      try { window.localStorage.setItem(FETCH_LIMIT_KEY, String(value)) } catch { /* 忽略 */ }
      void api.proxyConfigPut({ fetch_limit: value }).catch(() => { /* 静默：本地已生效 */ })
    }
    return value
  }, [limitText, fetchLimit])

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
  const counts = status?.counts

  // 协议过滤（2026-10-11）：http=协议类目；socks=socks4+socks5 合并；https=能力探测。
  const shown = useMemo(() => {
    if (filter === "all") return records
    if (filter === "http") return records.filter((r) => (r.protocol ?? "").toLowerCase() === "http")
    if (filter === "socks")
      return records.filter((r) => ["socks4", "socks5"].includes((r.protocol ?? "").toLowerCase()))
    return records.filter((r) => r.https_ok === true)   // https
  }, [records, filter])

  const regionText = useMemo(() => {
    const entries = Object.entries(status?.regions ?? {})
    if (!entries.length) return "—"
    return entries.sort((a, b) => b[1] - a[1]).slice(0, 6)
      .map(([k, v]) => `${k} ${v}`).join(" · ")
  }, [status])

  return (
    // 全局视图（needsProject=false，首页与项目内都渲染）：pt-9 = 首页无顶栏分支的
    // 36px 拖动区让位；项目内有 .topbar(h-16)，再加就是重复死区（对齐 KnowledgeView/SkillsView）。
    <div className={cn("flex h-full min-h-0 flex-col overflow-hidden", !pid && "pt-9")}>
      {/* 顶部工具条 */}
      <div className="flex shrink-0 flex-wrap items-center gap-1.5 border-b px-2 py-1.5">
        {running ? (
          <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                  disabled={busy} onClick={() => void guard(() => api.proxyStop(), "已停止")}>
            <Square size={12} /> 停止服务
          </Button>
        ) : (
          <Button size="sm" className="h-7 gap-1 text-xs" disabled={busy}
                  onClick={() => void guard(() => api.proxyStart(), "服务已启动")}
                  title="启动本地 HTTP + SOCKS5 代理入口">
            <Play size={12} /> 启动服务
          </Button>
        )}
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                disabled={busy || !running} onClick={() => void guard(() => api.proxyRotate(), "已轮换")}>
          <Zap size={12} /> 轮换 IP
        </Button>

        <span className="mx-1 h-5 w-px bg-border" />

        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs" disabled={busy}
                title={`从在线源抓取约 ${fetchLimit || "不限量"} 条并自动验证，剔除失效代理`}
                onClick={() => void guard(async () => {
                  const limit = commitFetchLimit()
                  const { job_id } = await api.proxyFetch([], limit)
                  await runJob(job_id, "proxy-fetch")
                })}>
          <Upload size={12} /> 在线抓取
        </Button>
        <label className="flex items-center gap-1 text-[11px] text-muted-foreground"
               title="单次「在线抓取」拉取的代理条数上限（按协议轮转取；0=不限）。失焦或回车生效，并写回 config/proxy.json。">
          条数
          <input type="number" min={0} step={500} inputMode="numeric"
                 value={limitText} disabled={busy}
                 onChange={(e) => setLimitText(e.target.value)}
                 onBlur={commitFetchLimit}
                 onKeyDown={(e) => { if (e.key === "Enter") commitFetchLimit() }}
                 className="h-7 w-16 rounded-md border bg-transparent px-1.5 text-right font-mono text-[11px] outline-none focus:border-primary disabled:opacity-50" />
        </label>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                disabled={busy || !records.length}
                title="验证池内全部代理并移除失效代理"
                onClick={() => void guard(async () => {
                  const { job_id } = await api.proxyValidate()
                  await runJob(job_id, "proxy-validate")
                })}>
          <Activity size={12} /> 验证全部
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                onClick={() => setImportOpen((v) => !v)}>
          <Globe size={12} /> 导入
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                disabled={busy || !records.length}
                title="指定一个网站，用池内代理逐一访问，检测哪些代理可达、哪些被该站 WAF 封禁"
                onClick={() => setCheckOpen(true)}>
          <ShieldAlert size={12} /> 访问测试
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                title="配置验证基准网站（连通性基准 / HTTPS 能力探测 / 交叉验证目标）"
                onClick={() => setConfigOpen(true)}>
          <Settings2 size={12} /> 网站设置
        </Button>
        <Button size="sm" variant="ghost" className="h-7 gap-1 text-xs"
                disabled={busy} onClick={() => void refresh()}>
          <RefreshCw size={12} /> 刷新
        </Button>

        {/* 协议过滤（2026-10-11）：点击切「全部/HTTP/HTTPS/SOCKS」 */}
        <div className="ml-auto flex items-center gap-1">
          {FILTERS.map((f) => (
            <button key={f.key} type="button"
                    onClick={() => setFilter(f.key)}
                    className={cn(
                      "rounded px-2 py-0.5 text-[11px] transition-colors",
                      filter === f.key
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:bg-accent")}>
              {f.label}
              {f.key !== "all" && counts && (
                <span className="ml-1 tabular-nums opacity-70">{counts[f.key]}</span>
              )}
            </button>
          ))}
        </div>
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
        <span className="ml-auto text-muted-foreground">
          池 {status?.pool_size ?? 0} · 可用 {status?.working ?? 0}
          {counts && <> · HTTP {counts.http} · HTTPS {counts.https} · SOCKS {counts.socks}</>}
        </span>
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
                      const r = await api.proxyAdd(records)
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
            {shown.map((r) => {
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
                    <div className="flex items-center gap-1">
                      <Badge variant={r.status === "Working" ? "secondary" : "outline"}
                             className="text-[10px]">
                        {r.status ?? "—"}
                      </Badge>
                      {r.https_ok === true && (
                        <Badge variant="outline" className="text-[10px] text-(--status-ok)">
                          HTTPS
                        </Badge>
                      )}
                    </div>
                  </td>
                  <td className="px-2 py-1 text-right">
                    <button type="button" title="从池中移除"
                            className="text-muted-foreground hover:text-destructive"
                            onClick={() => void guard(
                              () => api.proxyRemove([r.proxy]))}>
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              )
            })}
            {!shown.length && (
              <tr>
                <td colSpan={8} className="px-3 py-10 text-center text-muted-foreground">
                  {filter === "all"
                    ? "代理池为空——用「在线抓取」拉取，或「导入」粘贴代理列表"
                    : "当前过滤条件下无代理（换「全部」看看，或抓取补充）"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <CheckUrlDialog open={checkOpen} onOpenChange={setCheckOpen}
                      running={running} currentProxy={status?.current?.proxy ?? null}
                      onUse={(p) => api.proxyUse(p)}
                      onDone={() => void refresh()} />
      <ProxyConfigDialog open={configOpen} onOpenChange={setConfigOpen} />
    </div>
  )
}

/** 验证基准网站设置弹窗（2026-10-12）：配置连通性基准 / HTTPS 探测 / 交叉验证目标。
 *  写 config/proxy.json 并热更新（下轮「验证全部」「在线抓取」即生效）。 */
function ProxyConfigDialog({ open, onOpenChange }: {
  open: boolean
  onOpenChange: (v: boolean) => void
}) {
  const [cfg, setCfg] = useState<ProxyConfigView | null>(null)
  const [target, setTarget] = useState("")
  const [httpsTarget, setHttpsTarget] = useState("")
  const [targetB, setTargetB] = useState("")
  const [err, setErr] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    let alive = true
    setErr(null); setSaved(false)
    api.proxyConfigGet()
      .then((c) => {
        if (!alive) return
        setCfg(c)
        setTarget(c.validate_target ?? "")
        setHttpsTarget(c.validate_target_https ?? "")
        setTargetB(c.validate_target_b ?? "")
      })
      .catch((e) => { if (alive) setErr(e instanceof ApiError ? e.message : String(e)) })
    return () => { alive = false }
  }, [open])

  const save = async () => {
    setBusy(true); setErr(null); setSaved(false)
    try {
      const next = await api.proxyConfigPut({
        validate_target: target.trim(),
        validate_target_https: httpsTarget.trim(),
        validate_target_b: targetB.trim(),
      })
      setCfg(next); setSaved(true)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const restore = () => {
    if (!cfg) return
    setTarget(cfg.defaults.validate_target ?? "")
    setHttpsTarget(cfg.defaults.validate_target_https ?? "")
    setTargetB(cfg.defaults.validate_target_b ?? "")
    setSaved(false)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogTitle>验证基准网站设置</DialogTitle>
        <DialogDescription>
          指定「验证全部」与「在线抓取后自动验证」使用的基准网站。写入 config/proxy.json
          并热更新，保存后下一轮验证即生效（无需重启）。
        </DialogDescription>

        <div className="space-y-3 text-[12px]">
          <label className="block">
            <span className="text-muted-foreground">连通性基准（必填）</span>
            <input value={target} onChange={(e) => { setTarget(e.target.value); setSaved(false) }}
                   placeholder="http://example.com/"
                   className="mt-1 h-8 w-full rounded-md border bg-transparent px-2 font-mono text-[12px] outline-none focus:border-primary" />
            <span className="mt-0.5 block text-[10.5px] text-muted-foreground">
              经代理 GET 它，拿到 2xx/3xx 即判该代理 Working。用 http:// 明文基准不会大量剔除纯 HTTP 代理。
            </span>
          </label>

          <label className="block">
            <span className="text-muted-foreground">HTTPS 能力探测（可空=关闭）</span>
            <input value={httpsTarget} onChange={(e) => { setHttpsTarget(e.target.value); setSaved(false) }}
                   placeholder="https://example.com/"
                   className="mt-1 h-8 w-full rounded-md border bg-transparent px-2 font-mono text-[12px] outline-none focus:border-primary" />
            <span className="mt-0.5 block text-[10.5px] text-muted-foreground">
              经 CONNECT 隧道测 https 转发能力，只标 https_ok 徽章，不参与 Working 判定。
            </span>
          </label>

          <label className="block">
            <span className="text-muted-foreground">内容注入交叉验证第二目标（可空=关闭）</span>
            <input value={targetB} onChange={(e) => { setTargetB(e.target.value); setSaved(false) }}
                   placeholder="http://example.org/"
                   className="mt-1 h-8 w-full rounded-md border bg-transparent px-2 font-mono text-[12px] outline-none focus:border-primary" />
            <span className="mt-0.5 block text-[10.5px] text-muted-foreground">
              须与「连通性基准」内容不同；两目标经同一代理回相同响应体即判劫持型出口并剔除。
            </span>
          </label>

          {err && (
            <div className="rounded border border-destructive/40 bg-destructive/10 px-2 py-1 text-[11px] text-destructive">
              {err}
            </div>
          )}
          {saved && !err && (
            <div className="rounded border border-(--status-ok)/40 bg-(--status-ok)/10 px-2 py-1 text-[11px] text-(--status-ok)">
              已保存，下一轮验证/抓取即生效
            </div>
          )}
        </div>

        <div className="flex items-center gap-2">
          <Button size="sm" variant="ghost" className="h-8 text-xs"
                  disabled={busy || !cfg} onClick={restore}>
            恢复默认
          </Button>
          <Button size="sm" className="ml-auto h-8 text-xs"
                  disabled={busy || !target.trim()} onClick={() => void save()}>
            {busy ? "保存中…" : "保存"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

/**
 * 访问测试弹窗（2026-10-12）：指定网站 → 池内代理逐一访问 → 可达/被封禁结论。
 * 2026-10-15：结果按「可达 → 被封禁 → 不可达」置顶排序（组内按延迟升序），可达行
 * 带「切换」按钮——顺序轮换是碰运气，判出可达后直接钉死该 IP。
 * 切换成败一律弹窗内直显（失败给红色横幅 + 旧 serve 重启提示）：父级 guard() 的错误
 * 落在页面级 banner，会被本模态整个盖住，用户侧就是「点击没反应」。
 * 结果表 table-fixed + colgroup 收口列宽，避免 auto 布局把余量摊成「说明」后的空白。
 */
function CheckUrlDialog({ open, onOpenChange, onDone, running, currentProxy, onUse }: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onDone: () => void
  running: boolean
  currentProxy: string | null
  onUse: (proxy: string) => Promise<unknown>
}) {
  const [url, setUrl] = useState("")
  const [probeWaf, setProbeWaf] = useState(true)
  const [testRunning, setTestRunning] = useState(false)
  const [switching, setSwitching] = useState<string | null>(null)
  const [switchNote, setSwitchNote] = useState<string | null>(null)
  const [switchErr, setSwitchErr] = useState<string | null>(null)
  const [progress, setProgress] = useState<ProxyProgress | null>(null)
  const [result, setResult] = useState<ProxyCheckUrlResult | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const run = async () => {
    const target = url.trim()
    if (!target) { setErr("请输入目标 URL"); return }
    setTestRunning(true); setErr(null); setResult(null)
    setSwitchNote(null); setSwitchErr(null)
    setProgress({ phase: "queued", message: "排队中…" })
    try {
      const { job_id } = await api.proxyCheckUrl({ url: target, probe_waf: probeWaf })
      const job = await pollJob(job_id, (j) =>
        setProgress((j.meta?.progress as ProxyJobProgress | undefined) ?? null))
      if (job.status === "error") setErr(`任务失败：${job.error ?? ""}`)
      else setResult(job.result as ProxyCheckUrlResult)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e))
    } finally {
      setTestRunning(false); setProgress(null); onDone()
    }
  }

  /** 精确切换到某个可达代理：成功/失败都在弹窗内直显（不靠父级 guard——
   *  guard() 的错误落在页面级 banner，会被本模态弹窗整个盖住，用户侧就是
   *  「点击没反应」）。失败不影响已出的测试结果。 */
  const use = async (proxy: string) => {
    setSwitching(proxy); setSwitchErr(null); setSwitchNote(null)
    try {
      await onUse(proxy)
      setSwitchNote(`已切换当前代理 → ${proxy}（此后全走这个 IP）`)
      onDone()   // 立刻刷新状态条与行高亮，不等 3s 轮询
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e)
      // 旧版 serve / 未重启后端都会走到这：提示先看平台是否刚更新过
      const stale = /404|unknown path|旧版|控制通道/.test(msg)
      setSwitchErr(stale
        ? `切换失败：${msg}（若刚更新过平台，请先重启后端，再「停止服务」→「启动服务」重拉代理服务）`
        : `切换失败：${msg}`)
    } finally {
      setSwitching(null)
    }
  }

  // 可达置顶（2026-10-15）：可达（未封禁且通）→ 被封禁 → 不可达；组内延迟升序。
  // 封禁行才是「被判死」的，沉底降低噪声；可达的不必再翻表找。
  const rows = useMemo(() => {
    if (!result) return []
    const rank = (r: ProxyUrlCheckRow) => (r.blocked ? 1 : (r.ok ? 0 : 2))
    return [...result.results].sort((a, b) => {
      const d = rank(a) - rank(b)
      if (d !== 0) return d
      const la = typeof a.latency === "number" ? a.latency : Infinity
      const lb = typeof b.latency === "number" ? b.latency : Infinity
      return la - lb
    })
  }, [result])

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!testRunning) onOpenChange(v) }}>
      <DialogContent className="max-w-3xl">
        <DialogTitle>访问测试 · 指定网站代理可达性</DialogTitle>
        <DialogDescription>
          用池内全部代理逐一访问目标网站，并与平台直连基线对比——直连能过、经代理却拿到
          WAF 拦截页（403/429/Cloudflare/雷池…）者判「被该站封禁」。只报告，不改动代理池。
        </DialogDescription>

        <div className="flex items-center gap-2">
          <input
            value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="http://target.example.com/ （可省 scheme，默认 http://）"
            className="h-8 flex-1 rounded-md border bg-transparent px-2 font-mono text-[12px] outline-none focus:border-primary"
            onKeyDown={(e) => { if (e.key === "Enter" && !testRunning) void run() }} />
          <label className="flex shrink-0 cursor-pointer items-center gap-1 text-[11px] text-muted-foreground">
            <input type="checkbox" checked={probeWaf}
                   onChange={(e) => setProbeWaf(e.target.checked)} className="size-3" />
            检测 WAF/封禁
          </label>
          <Button size="sm" className="h-8 text-xs" disabled={testRunning} onClick={() => void run()}>
            {testRunning ? "测试中…" : "开始测试"}
          </Button>
        </div>

        {err && (
          <div className="rounded border border-destructive/40 bg-destructive/10 px-2 py-1 text-[11px] text-destructive">
            {err}
          </div>
        )}
        {progress && !err && <ProxyProgressBar p={progress} />}

        {result && (
          <div className="flex min-h-0 flex-1 flex-col gap-2">
            <div className="flex flex-wrap items-center gap-3 text-[11px]">
              <span>共测 <b>{result.total}</b></span>
              <span className="text-(--status-ok)">可达 {result.reachable}</span>
              <span className="text-(--status-error)">被封禁 {result.blocked}</span>
              <span className="text-muted-foreground">
                直连基线 {result.baseline?.ok
                  ? `HTTP ${result.baseline.status_code ?? "?"}`
                  : `不可达（${result.baseline?.error ?? "超时"}）`}
              </span>
              <span className="text-muted-foreground">
                当前代理 <code className="rounded bg-muted px-1 font-mono">{currentProxy ?? "—"}</code>
              </span>
              {!running && (
                <span className="text-muted-foreground">（服务未运行，切换不可用）</span>
              )}
            </div>

            {switchErr && (
              <div className="rounded border border-destructive/40 bg-destructive/10 px-2 py-1 text-[11px] text-destructive">
                {switchErr}
              </div>
            )}
            {switchNote && (
              <div className="rounded border bg-muted/40 px-2 py-1 text-[11px] text-muted-foreground">
                {switchNote}
              </div>
            )}

            {result.usable.length > 0 && (
              <div className="rounded-md border bg-muted/30 px-2 py-1.5">
                <p className="mb-1 text-[11px] font-medium">
                  可用代理（未被封禁，{result.usable.length} 个，按延迟升序）
                </p>
                <div className="flex flex-wrap gap-1">
                  {result.usable.map((u) => (
                    <button key={u.proxy} type="button"
                            disabled={!running || switching !== null}
                            title={running ? `切换当前代理到 ${u.protocol ?? "http"}://${u.proxy}` : "服务未运行"}
                            onClick={() => void use(u.proxy)}
                            className={cn(
                              "rounded bg-background px-1.5 py-0.5 font-mono text-[10.5px] transition-colors",
                              running ? "cursor-pointer hover:bg-primary hover:text-primary-foreground" : "cursor-not-allowed opacity-60",
                              currentProxy === u.proxy && "ring-1 ring-(--status-ok)")}>
                      {u.protocol ? `${u.protocol}://` : ""}{u.proxy}
                      {typeof u.latency === "number" && (
                        <span className="text-muted-foreground"> {Math.round(u.latency * 1000)}ms</span>
                      )}
                    </button>
                  ))}
                </div>
                <p className="mt-1 text-[10px] text-muted-foreground">点任意一个即切换为当前代理（下表可达行同效）</p>
              </div>
            )}

            {/* table-fixed + colgroup：auto 布局会把剩余宽度按内容比例摊给各列，
                「说明」短文本后留一大块空白才顶到操作按钮——固定列宽后按钮贴内容。 */}
            <div className="min-h-0 flex-1 overflow-auto rounded-md border">
              <table className="w-full table-fixed border-collapse text-[11px]">
                <colgroup>
                  <col className="w-[27%]" />  代理
                  <col className="w-[8%]" />   协议
                  <col className="w-[9%]" />   状态码
                  <col className="w-[9%]" />   延迟
                  <col className="w-[9%]" />   结论
                  <col />                       说明（吃剩余宽度，截断不换行）
                  <col className="w-[68px]" /> 操作
                </colgroup>
                <thead className="sticky top-0 z-10 bg-(--surface-1) text-muted-foreground">
                  <tr className="border-b">
                    <th className="px-2 py-1 text-left font-medium">代理</th>
                    <th className="px-2 py-1 text-left font-medium">协议</th>
                    <th className="px-2 py-1 text-right font-medium">状态码</th>
                    <th className="px-2 py-1 text-right font-medium">延迟</th>
                    <th className="px-2 py-1 text-left font-medium">结论</th>
                    <th className="px-2 py-1 text-left font-medium">说明</th>
                    <th className="px-2 py-1 text-left font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.proxy} className={cn(
                      "border-b border-border/50",
                      currentProxy === r.proxy && "bg-(--status-ok)/8")}>
                      <td className="truncate px-2 py-1 font-mono" title={r.proxy}>{r.proxy}</td>
                      <td className="truncate px-2 py-1 text-muted-foreground">{r.protocol ?? "—"}</td>
                      <td className="px-2 py-1 text-right font-mono">{r.status_code ?? "—"}</td>
                      <td className="px-2 py-1 text-right font-mono">
                        {typeof r.latency === "number" ? `${Math.round(r.latency * 1000)}ms` : "—"}
                      </td>
                      <td className="px-2 py-1"><CheckVerdict row={r} /></td>
                      <td className="truncate px-2 py-1 text-muted-foreground" title={r.reason ?? ""}>
                        {r.reason ?? ""}
                      </td>
                      <td className="px-2 py-1">
                        {currentProxy === r.proxy ? (
                          <span className="text-[10px] text-(--status-ok)">当前</span>
                        ) : r.ok && !r.blocked ? (
                          <Button size="sm" variant="outline"
                                  className="h-6 gap-1 whitespace-nowrap px-1.5 text-[10px]"
                                  disabled={!running || switching !== null}
                                  title={`切换当前代理到 ${r.proxy}（此后全走这个 IP）`}
                                  onClick={() => void use(r.proxy)}>
                            <Zap size={10} />
                            {switching === r.proxy ? "切换中…" : "切换"}
                          </Button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** 单条结论徽章：可达（绿）/ 被封禁（红）/ 不可达（灰）。 */
function CheckVerdict({ row }: { row: ProxyUrlCheckRow }) {
  if (row.blocked)
    return <Badge className="bg-(--status-error)/15 text-[10px] text-(--status-error)">被封禁</Badge>
  if (row.ok)
    return <Badge className="bg-(--status-ok)/15 text-[10px] text-(--status-ok)">可达</Badge>
  return <Badge variant="outline" className="text-[10px] text-muted-foreground">不可达</Badge>
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
