import { Suspense, lazy, useCallback, useEffect, useState } from "react"
import { Upload, ShieldAlert } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import { useEvents } from "@/lib/useEvents"
import type {
  Asset, BinaryOverview, CachedFuncRow, CachedFunction, Finding, FuncEntry, Job,
  PullIdaFunctionsResult, PullNamesResult, PushNamesToIdaResult, XrefData,
} from "@/lib/types"
import { hexAddr } from "@/lib/workbench"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"
import type { ChainNodeType } from "@/lib/types"
import { SampleBar, type Engine } from "./SampleBar"
import { FunctionBrowser } from "./FunctionBrowser"
import { FunctionDetail } from "./FunctionDetail"
import { XrefPane } from "./XrefPane"
import { RelatedFindings } from "./RelatedFindings"
import { NotesPane } from "./NotesPane"
// React Flow 较重，攻击链视图按需切包，不拖慢工作台首屏
const ChainView = lazy(() =>
  import("./chains/ChainView").then((m) => ({ default: m.ChainView })))
// 蓝图视图（R4）同样按需切包
const BlueprintsView = lazy(() =>
  import("./blueprints/BlueprintsView").then((m) => ({ default: m.BlueprintsView })))
// 业务逻辑块视图（函数协作/业务语义笔记）按需切包
const LogicBlocksView = lazy(() =>
  import("./logic/LogicBlocksView").then((m) => ({ default: m.LogicBlocksView })))

// rev-generic 逆向理解工作台（DESIGN.md §12）：
// 样本条 + 三栏（函数浏览器｜结论+伪码｜xref/发现/笔记）。
// 三层数据：headless 缓存（全量客观）/ func_kb（分析过的）/ findings（挂 binary 资产）。

// 大样本阈值（2026-09-29 用户口径，与后端 SAMPLE_LARGE_BYTES 对齐）：
// >20MB headless 全量导出可能很久——上传/开始分析前二次确认，建议走 IDA 拉取
const SAMPLE_LARGE_BYTES = 20 * 1024 * 1024

function largeSampleConfirm(mb: number, engine: Engine = "ida"): boolean {
  const hint = engine === "ghidra"
    ? "当前为 Ghidra 模式：大样本走 Ghidra 并行分片导出；反汇编改为选中函数时按需生成（不内嵌缓存）。\n"
    : "更快的路子：在 IDA 里打开样本按 Ctrl-Alt-M 启动 MCP 插件，再用「从 IDA 拉取函数」（秒级拿全量函数清单）。\n"
  return window.confirm(
    `该样本约 ${mb}MB，属于大样本：headless 全量分析（自动分析+全量反编译）可能耗时很久。\n` +
    hint +
    "仍要继续 headless 分析吗？")
}

export function ReverseWorkbench({ pid, active = true }: { pid: string; active?: boolean }) {
  const { events } = useEvents(pid)
  const [tick, setTick] = useState(0)
  const bump = useCallback(() => setTick((t) => t + 1), [])
  useEffect(() => { bump() }, [events.length, bump])

  const [samples, setSamples] = useState<Asset[]>([])
  const [sha, setSha] = useState<string | null>(null)
  const [overview, setOverview] = useState<BinaryOverview | null>(null)
  // 反编译引擎模式（2026-10-01）：样本 meta.engine（缺省 ida）
  const engine: Engine = overview?.engine === "ghidra" ? "ghidra" : "ida"
  const [rows, setRows] = useState<CachedFuncRow[]>([])
  const [funcs, setFuncs] = useState<FuncEntry[]>([])
  const [findings, setFindings] = useState<Finding[]>([])
  const [addr, setAddr] = useState<string | null>(null)
  const [detail, setDetail] = useState<CachedFunction | null>(null)
  const [xref, setXref] = useState<XrefData | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [triaging, setTriaging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [busyAi, setBusyAi] = useState(false)
  // IDA 拉取进度（2026-09-30）：job 轮询带出 {pulled,total}；null=没在拉
  const [pullProgress, setPullProgress] = useState<{ pulled: number; total: number | null } | null>(null)
  // headless 导出进度（大样本 P3，2026-09-30）：job 轮询带出 {done,total}；null=没在导出
  // 2026-10-01 扩：phase（starting/analyzing/decompile/stopped）+ stoppable（仅 Ghidra 可停）
  const [triageProgress, setTriageProgress] = useState<
    { phase: string; done: number; total: number; discovered: number; completed: number; failed: number;
      stoppable: boolean; etaSeconds: number | null; elapsedSeconds: number; ratePerSecond: number } | null>(null)
  const [triageMsg, setTriageMsg] = useState<string | null>(null)
  // 样本删除确认（2026-10-01）：delTarget 非 null = 确认框打开
  const [delTarget, setDelTarget] = useState<{ sha: string; name: string; counts: BinaryOverview | null } | null>(null)
  const [delBusy, setDelBusy] = useState(false)
  const [delMsg, setDelMsg] = useState<string | null>(null)

  // subnav：逆向分析｜攻击链｜蓝图｜业务逻辑（切页不卸载分析状态，同级条件渲染）
  const [mode, setMode] = useState<"rev" | "chains" | "blueprint" | "logic">("rev")
  const [rightTab, setRightTab] = useState("xref")
  const [focusFinding, setFocusFinding] = useState<string | null>(null)

  useEffect(() => {
    const handler = (event: Event) => {
      const sha = (event as CustomEvent<{ sha?: string }>).detail?.sha
      if (sha) setSha(sha)
    }
    window.addEventListener("open-binary", handler)
    return () => window.removeEventListener("open-binary", handler)
  }, [])

  // 链节点点击 → 切回分析视图定位（artifact 无分析视图落点，忽略）
  const locateFromChain = (nodeType: ChainNodeType, nodeId: string, addr?: string) => {
    if (nodeType === "func_kb" && addr) {
      setAddr(addr)
      setRightTab("xref")
      setMode("rev")
    } else if (nodeType === "finding") {
      setFocusFinding(nodeId)
      setRightTab("findings")
      setMode("rev")
    }
  }

  const binaries = samples.filter((a) => a.type === "binary")
  const refreshSamples = useCallback(
    () => api.assets(pid).then(setSamples).catch(() => {}), [pid])
  // Keep-alive（2026-09-30）：active=false（黑板被隐藏）时停轮询；切回 active 变
  // true 本 effect 重跑 → 立即刷一次 + 恢复 4s 间隔。所有轮询 effect 同此模式。
  useEffect(() => {
    if (!active) return
    refreshSamples()
    const t = setInterval(refreshSamples, 4000)
    return () => clearInterval(t)
  }, [refreshSamples, active])
  useEffect(() => {
    if (!active) return
    refreshSamples()
  }, [tick, refreshSamples, active])

  // 样本删除（2026-10-01）：拉该样本 overview 计数 → 弹确认框 → 确认后 DELETE 级联
  const handleDeleteSample = useCallback(async (targetSha: string) => {
    const asset = binaries.find((a) => a.value === targetSha) ?? null
    const name = (asset?.meta?.filename as string) || `${targetSha.slice(0, 12)}…`
    const counts = targetSha === sha
      ? overview
      : await api.binaryOverview(pid, targetSha).catch(() => null)
    setDelMsg(null)
    setDelTarget({ sha: targetSha, name, counts })
  }, [binaries, sha, overview, pid])

  const confirmDeleteSample = async () => {
    if (!delTarget || delBusy) return
    setDelBusy(true)
    setDelMsg(null)
    try {
      await api.deleteSample(pid, delTarget.sha)
      const wasCurrent = delTarget.sha === sha
      setDelTarget(null)
      await refreshSamples()
      if (wasCurrent) setSha(null)
      bump()
    } catch (e) {
      setDelMsg(String(e))
    } finally {
      setDelBusy(false)
    }
  }

  // 默认选最新样本；当前 sha 消失则回退
  useEffect(() => {
    if (sha && binaries.some((a) => a.value === sha)) return
    setSha(binaries.length ? binaries[binaries.length - 1].value : null)
  }, [samples, sha]) // eslint-disable-line react-hooks/exhaustive-deps

  // 切样本：清空旧数据，等下一轮 effect 填充
  useEffect(() => {
    setOverview(null); setRows([]); setAddr(null); setDetail(null); setXref(null)
  }, [sha])

  // overview：缓存缺席也 200，照 4s 轮询（分诊完成自动转 cached）
  useEffect(() => {
    if (!active) return
    if (!sha) return
    let alive = true
    const load = () =>
      api.binaryOverview(pid, sha).then((o) => alive && setOverview(o)).catch(() => {})
    load()
    const t = setInterval(load, overview?.analysis_job ? 1200 : 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, sha, tick, active, overview?.analysis_job?.id])

  // Rehydrate analysis state after refresh/navigation.  The job registry is the
  // source of truth, so progress remains visible even when the user did not
  // start the job from this mounted workbench instance.
  useEffect(() => {
    const job = overview?.analysis_job
    if (!job) {
      setTriaging(false)
      setTriageProgress(null)
      return
    }
    const p = job.progress || {}
    setTriaging(true)
    setTriageProgress({
      phase: p.phase ?? "starting", done: p.done ?? 0, total: p.total ?? 0,
      discovered: p.discovered ?? p.total ?? 0, completed: p.completed ?? p.done ?? 0,
      failed: p.failed ?? 0, stoppable: !!p.stoppable,
      etaSeconds: typeof p.eta_seconds === "number" ? p.eta_seconds : null,
      elapsedSeconds: Number(p.elapsed_seconds ?? 0), ratePerSecond: Number(p.rate_per_second ?? 0),
    })
  }, [overview?.analysis_job?.id, overview?.analysis_job?.progress?.phase,
    overview?.analysis_job?.progress?.done, overview?.analysis_job?.progress?.total,
    overview?.analysis_job?.progress?.discovered, overview?.analysis_job?.progress?.completed,
    overview?.analysis_job?.progress?.failed, overview?.analysis_job?.progress?.eta_seconds,
    overview?.analysis_job?.progress?.elapsed_seconds, overview?.analysis_job?.progress?.rate_per_second,
    overview?.analysis_job?.progress?.stoppable])

  // 缓存函数行（cached 前 409 → 留空）。拉取中由 job 增量推送驱动，跳过全量重拉
  // （5万+ 行每 2s 全量拉+重建曾拖崩渲染）；拉完 setPullProgress(null) 触发本 effect
  // 重跑一次全量对账（与磁盘 partial 缓存对齐）。
  useEffect(() => {
    if (!active) return
    if (pullProgress) return
    if (!sha || !overview?.cached) { setRows([]); return }
    let alive = true
    const load = () => api.cachedFunctions(pid, sha).then((r) => alive && setRows(r)).catch(() => {})
    load()
    const live = !!overview?.analysis_job || !!overview?.meta?.partial
    const timer = live ? window.setInterval(load, 1800) : null
    return () => { alive = false; if (timer !== null) window.clearInterval(timer) }
  }, [pid, sha, overview?.cached, overview?.analysis_job?.id, overview?.meta?.partial, tick, pullProgress, active])

  // func_kb（该样本分析过的函数）
  useEffect(() => {
    if (!active) return
    if (!sha) return
    let alive = true
    const load = () => api.funcs(pid, sha).then((f) => alive && setFuncs(f)).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, sha, tick, active])

  // 发现（项目级，右栏按 binary 资产/func_id/address 客户端过滤）
  useEffect(() => {
    if (!active) return
    let alive = true
    const load = () => api.findings(pid).then((f) => alive && setFindings(f)).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, tick, active])

  // MCP 实时桥：缓存未分诊/被移走时，单函数伪码与 xref 仍可经 IDA 当前库实时取（列表不替代缓存）
  const mcpLive = overview?.tools.mcp.state === "installed"
  const funcReadable = !!overview?.cached || mcpLive

  // 选中函数：伪码 + xref（headless 缓存优先；缺席且 MCP 在线时后端自动实时降级）
  useEffect(() => {
    if (!active) return
    if (!sha || !addr || !funcReadable) { setDetail(null); setXref(null); return }
    let alive = true
    setDetailLoading(true)
    // Xrefs are cache-only and normally return immediately.  Keep them
    // independent from Ghidra's potentially slow first on-demand disassembly
    // so the right pane is usable while the assembly request is running.
    api.binaryXrefs(pid, sha, addr).then((x) => alive && setXref(x)).catch(() => alive && setXref(null))
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    const loadDetail = () => api.cachedFunction(pid, sha, addr).then((d) => {
      if (!alive) return
      setDetail(d)
      setDetailLoading(false)
      // Ghidra large samples enrich assembly asynchronously. Poll only while
      // that one function is pending, then stop as soon as lines arrive.
      if (engine === "ghidra" && d.disasm_pending && !d.disasm?.lines?.length)
        retryTimer = setTimeout(loadDetail, 1500)
    }).catch(() => {
      if (!alive) return
      setDetail(null)
      setDetailLoading(false)
    })
    loadDetail()
    return () => { alive = false; if (retryTimer) clearTimeout(retryTimer) }
  }, [pid, sha, addr, funcReadable, active, engine]) // eslint-disable-line react-hooks/exhaustive-deps

  const currentAsset = binaries.find((a) => a.value === sha) ?? null
  const kbFunc = funcs.find((f) => hexAddr(f.address) === addr) ?? null
  const cacheName = rows.find((r) => r.address === addr)?.name ?? null

  const awaitJob = async (jobId: string | null, onProgress?: (j: Job) => void) => {
    if (!jobId) { bump(); return null }
    setTriaging(true)
    const job = await pollJob(jobId, (j) => onProgress?.(j), 1500)
    setTriaging(false)
    bump()
    return job
  }

  const handleUpload = async (file: File) => {
    const mb = Math.round(file.size / 1024 / 1024)
    if (file.size > SAMPLE_LARGE_BYTES && !largeSampleConfirm(mb)) return
    setUploading(true)
    try {
      const r = await api.uploadSample(pid, file)
      await refreshSamples()
      setSha(r.sha)
      await awaitJob(r.job_id)
    } finally {
      setUploading(false)
    }
  }

  const handleRetry = async () => {
    if (!sha) return
    const size = overview?.asset_meta?.size
    if (typeof size === "number" && size > SAMPLE_LARGE_BYTES &&
        !largeSampleConfirm(Math.round(size / 1024 / 1024), engine)) return
    setTriageProgress({ phase: "starting", done: 0, total: 0, discovered: 0, completed: 0,
      failed: 0, stoppable: false, etaSeconds: null, elapsedSeconds: 0, ratePerSecond: 0 })
    try {
      const r = await api.retryTriage(pid, sha)
      const job = await awaitJob(r.job_id, (j) => {
        const p = j.meta?.progress as
          { phase?: string; done?: number; total?: number; discovered?: number; completed?: number;
            failed?: number; stoppable?: boolean; eta_seconds?: number | null;
            elapsed_seconds?: number; rate_per_second?: number } | undefined
        if (p) setTriageProgress({
          phase: p.phase ?? "", done: p.done ?? 0, total: p.total ?? 0,
          discovered: p.discovered ?? p.total ?? 0, completed: p.completed ?? p.done ?? 0,
          failed: p.failed ?? 0, stoppable: !!p.stoppable,
          etaSeconds: typeof p.eta_seconds === "number" ? p.eta_seconds : null,
          elapsedSeconds: Number(p.elapsed_seconds ?? 0), ratePerSecond: Number(p.rate_per_second ?? 0),
        })
      })
      const res = job?.result as { status?: string; hint?: string } | null
      if (res?.status === "stopped" && res.hint) setTriageMsg(res.hint)
    } finally {
      setTriageProgress(null)
    }
  }

  // 停止导出（大样本 P3，协作式）：当前函数反编译完停下并保留已导出部分
  const handleStopTriage = async () => {
    if (!sha) return
    try {
      await api.cancelTriage(pid, sha)
    } catch { /* 轮询结束分支兜底 */ }
  }

  // IDA 手改名 → func_kb：库内重导 diff（自动名 sub_/nullsub/unk_ 不拉）
  const handlePullNames = async (): Promise<string> => {
    if (!sha) return "无样本"
    try {
      const r = await api.pullNames(pid, sha)
      const job = await pollJob(r.job_id, () => {}, 1500)
      bump()
      const res = job.result as PullNamesResult | null
      if (job.status === "error") return `同步失败：${job.error}`
      if (!res) return "同步失败：无结果"
      if (res.status === "ok") return `已从 IDA 同步 ${res.changed?.length ?? 0} 个改名`
      if (res.status === "locked") return "IDA 正开着该库，请先关闭后再同步"
      if (res.status === "no-db") return "尚无 IDA 数据库，请先完成分诊"
      return res.guidance ?? `同步未执行：${res.status}`
    } catch (e) {
      return String(e)
    }
  }

  // GUI IDA MCP → 轻量缓存：list_funcs 全量清单（无伪码）+ IDA 手改名 diff 回拉
  // 拉取中 job 轮询（1s）带 progress（按钮进度环）；已有 headless 全量缓存时先确认覆盖
  const handlePullIdaFunctions = async (): Promise<string> => {
    if (!sha) return "无样本"
    const meta = overview?.meta
    if (meta?.source && meta.source !== "ida-mcp" && !meta.partial &&
        !window.confirm("该样本已有 headless 全量分析缓存（含伪码）。\n" +
          "从 IDA 拉取会把它替换成轻量清单缓存（伪码可重跑「开始分析」恢复）。继续吗？")) {
      return "已取消拉取"
    }
    setPullProgress({ pulled: 0, total: null })
    try {
      const r = await api.pullIdaFunctions(pid, sha)
      const job = await pollJob(r.job_id, (j) => {
        const p = j.meta?.progress
        if (p) setPullProgress({ pulled: p.pulled ?? 0, total: p.total ?? null })
        // 增量行：job 每页推送新行，按 address 去重 append——拉取中左栏渐进长出，
        // 不再每 2s 全量重拉整个函数清单（5万+ 行曾拖崩渲染/白屏）
        const inc = p?.rows
        if (inc?.length) {
          setRows((prev) => {
            const seen = new Set(prev.map((x) => x.address))
            const fresh = inc.filter((r) => !seen.has(r.address))
            return fresh.length ? [...prev, ...fresh] : prev
          })
        }
      }, 1000)
      bump()
      const res = job.result as PullIdaFunctionsResult | null
      if (job.status === "error") return `拉取失败：${job.error}`
      if (!res) return "拉取失败：无结果"
      if (res.status === "ok") {
        return `已拉取 ${res.function_count ?? 0} 个函数（回拉改名 ${res.changed?.length ?? 0} 个）`
      }
      if (res.status === "stopped") return res.hint ?? "已停止拉取"
      return res.hint ?? `拉取未执行：${res.status}`
    } catch (e) {
      return String(e)
    } finally {
      setPullProgress(null)
    }
  }

  // 停止拉取：后端页间检查点生效，已拉部分保持生效（partial），再点从断点继续
  const handleStopPull = async () => {
    if (!sha) return
    try {
      await api.cancelPullIdaFunctions(pid, sha)
    } catch { /* 轮询结束分支兜底 */ }
  }

  // 拉取中左栏渐进由 job 增量行驱动（见 handlePullIdaFunctions 的 pollJob 回调），
  // 不再每 2s bump 触发全量重拉；overview/funcs/findings 各自 4s 轮询保持原节奏。

  // 反向：func_kb 有效命名批量写回 GUI IDA 当前库（只改内存，提示用户落盘）
  const handlePushNames = async (): Promise<string> => {
    if (!sha) return "无样本"
    try {
      const r = await api.pushNamesToIda(pid, sha)
      const job = await pollJob(r.job_id, () => {}, 1500)
      bump()
      const res = job.result as PushNamesToIdaResult | null
      if (job.status === "error") return `同步失败：${job.error}`
      if (!res) return "同步失败：无结果"
      if (res.status === "ok") {
        const base = `已同步 ${res.applied ?? 0} 个命名到 IDA`
        return res.applied ? `${base}——请在 IDA 中保存数据库落盘` : (res.hint ?? base)
      }
      return res.hint ?? `同步未执行：${res.status}`
    } catch (e) {
      return String(e)
    }
  }

  // AI triage（任务机制退役后）：开通用窗 + 引导，objective 自包含（人工开窗引导=纯对话）
  const handleAiTriage = async () => {
    if (!sha || !overview) return
    const path = typeof overview.asset_meta?.path === "string" ? overview.asset_meta.path : "?"
    const objective = [
      `逆向分诊任务（triage）。样本文件：${path}`,
      `sha256=${sha}；headless 已导出 ${overview.function_count} 个函数（客观全量在缓存，不要重复跑反编译器）。`,
      "产出要求：",
      "1. 逐函数理解样本行为，结论写入 func_kb（先查后写）；重要函数才建行，不要全量搬运。",
      `2. 发现挂 binary 资产 ${currentAsset?.id ?? ""}，evidence 必须带 func_id 与 address，category 取 algorithm/protocol/data-structure/mechanism/risk 之一。`,
      "3. 未经动态验证的结论一律 unverified；需要执行样本的动作必须等人类授权，禁止自行运行样本。",
      "4. 伪码不进事件流。",
    ].join("\n")
    setBusyAi(true)
    try {
      const r = await api.spawnAgent(pid, "_generalist")
      await api.sessionNote(r.id, objective)
      setTimeout(() => setBusyAi(false), 2500)
    } catch {
      setBusyAi(false)
    }
  }

  // 反编译引擎模式（2026-10-01）：样本 meta.engine（缺省 ida）；切换落库并刷新 overview
  const handleSelectEngine = useCallback(async (next: Engine) => {
    if (!sha || next === engine) return
    await api.setBinaryEngine(pid, sha, next)
    await api.binaryOverview(pid, sha).then(setOverview).catch(() => {})
  }, [pid, sha, engine])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <SampleBar
        samples={binaries} sha={sha} onSelect={setSha} onDeleteSample={handleDeleteSample} overview={overview}
        engine={engine} onSelectEngine={handleSelectEngine}
        triaging={triaging} uploading={uploading} busyAi={busyAi}
        onUpload={handleUpload} onRetry={handleRetry} onAiTriage={handleAiTriage}
        onPullNames={handlePullNames}
        onPullIdaFunctions={handlePullIdaFunctions} onPushNames={handlePushNames}
        pullProgress={pullProgress} onStopPull={handleStopPull}
        triageProgress={triageProgress} onStopTriage={handleStopTriage} triageMsg={triageMsg}
      />
      {/* subnav：逆向分析｜攻击链｜蓝图｜业务逻辑（DESIGN §12 / §9 R4） */}
      <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1">
        {([["rev", "逆向分析"], ["chains", "攻击链"], ["blueprint", "蓝图"], ["logic", "业务逻辑"]] as const).map(([k, label]) => (
          <button
            key={k} type="button" onClick={() => setMode(k)}
            className={cn("rounded px-3 py-1 text-[11px]",
              mode === k ? "bg-secondary text-primary" : "text-muted-foreground hover:bg-accent")}
          >
            {label}
          </button>
        ))}
      </div>
      {mode === "chains" ? (
        <Suspense fallback={<div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">加载攻击链视图…</div>}>
          <ChainView pid={pid} tick={tick} onLocate={locateFromChain} />
        </Suspense>
      ) : mode === "blueprint" ? (
        <Suspense fallback={<div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">加载蓝图视图…</div>}>
          <BlueprintsView pid={pid} tick={tick}
                          onLocate={(bpSha, bpAddr) => {
                            if (bpSha && bpSha !== sha) setSha(bpSha)
                            setAddr(bpAddr)
                            setMode("rev")
                          }} />
        </Suspense>
      ) : mode === "logic" ? (
        <Suspense fallback={<div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">加载业务逻辑视图…</div>}>
          <LogicBlocksView pid={pid} tick={tick} sha={sha}
                           onLocate={(lbSha, lbAddr) => {
                             if (lbSha && lbSha !== sha) setSha(lbSha)
                             setAddr(lbAddr)
                             setMode("rev")
                           }} />
        </Suspense>
      ) : !sha ? (
        <EmptyUpload onUpload={handleUpload} uploading={uploading} />
      ) : (
        <div className="flex min-h-0 flex-1">
          {/* 左：函数浏览器（288px） */}
          <div className="w-72 shrink-0 border-r">
            <FunctionBrowser
              pid={pid} sha={sha}
              rows={rows} funcs={funcs} selected={addr} onSelect={setAddr}
              imports={overview?.imports ?? null} cached={!!overview?.cached}
              stringsCount={overview?.strings_count ?? 0}
              progress={{
                discovered: triageProgress?.discovered ?? overview?.analysis_job?.progress?.discovered ?? rows.length,
                completed: triageProgress?.completed ?? overview?.analysis_job?.progress?.completed ?? rows.filter((row) => row.status === "done").length,
                failed: triageProgress?.failed ?? overview?.analysis_job?.progress?.failed ?? rows.filter((row) => row.status === "failed").length,
              }}
            />
          </div>

          {/* 中：结论 + 伪码 */}
          <div className="min-w-0 flex-1">
            {addr ? (
              <FunctionDetail
                pid={pid} sha={sha} addr={addr} detail={detail} loading={detailLoading}
                kb={kbFunc} bits={overview?.meta?.bits ?? 64} hasDb={!!overview?.db_path}
                imagebase={overview?.meta?.imagebase ?? null}
                moduleName={overview?.meta?.filename ?? null}
                mcpLive={mcpLive} cached={!!overview?.cached} engine={engine}
              />
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                从左侧选择函数开始理解样本
              </div>
            )}
          </div>

          {/* 右：xref / 发现 / 笔记（320px） */}
          <div className="w-80 shrink-0 border-l">
            <Tabs value={rightTab} onValueChange={setRightTab} className="flex h-full flex-col gap-0">
              <TabsList className="w-full justify-start rounded-none border-b bg-transparent p-0">
                <TabsTrigger value="xref" className="rounded-none border-b-2 px-3 py-1.5 text-[11px]">xref</TabsTrigger>
                <TabsTrigger value="findings" className="rounded-none border-b-2 px-3 py-1.5 text-[11px]">发现</TabsTrigger>
                <TabsTrigger value="notes" className="rounded-none border-b-2 px-3 py-1.5 text-[11px]">笔记</TabsTrigger>
              </TabsList>
              <TabsContent value="xref" className="min-h-0 flex-1 overflow-auto">
                <XrefPane xref={xref} loading={detailLoading} onSelect={setAddr} />
              </TabsContent>
              <TabsContent value="findings" className="min-h-0 flex-1 overflow-auto">
                <RelatedFindings
                  pid={pid} findings={findings} assetId={currentAsset?.id ?? null}
                  addr={addr} kbId={kbFunc?.id ?? null} onChanged={bump}
                  focusId={focusFinding}
                />
              </TabsContent>
              <TabsContent value="notes" className="min-h-0 flex-1 overflow-auto">
                {addr
                  ? <NotesPane pid={pid} sha={sha} addr={addr} cacheName={cacheName}
                               kb={kbFunc} onSaved={bump} mcpLive={mcpLive} engine={engine} />
                  : <p className="p-3 text-[11px] text-muted-foreground">先在左侧选中函数</p>}
              </TabsContent>
            </Tabs>
          </div>
        </div>
      )}

      {/* 样本删除确认（列数量 + 不可逆提示；2026-10-01） */}
      <AlertDialog open={!!delTarget}
                   onOpenChange={(o) => { if (!o) { setDelTarget(null); setDelMsg(null) } }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>从项目中移除样本？</AlertDialogTitle>
            <AlertDialogDescription>
              将永久删除 <span className="font-mono text-foreground">{delTarget?.name}</span> 及其全部关联数据，不可逆：
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="ml-4 list-disc space-y-0.5 text-[11px] text-muted-foreground">
            <li>函数条目（func_kb）：{delTarget?.counts?.analyzed_count ?? 0}</li>
            <li>发现：{delTarget?.counts?.findings_count ?? 0}</li>
            <li>业务逻辑块：{delTarget?.counts?.logic_blocks_count ?? 0}</li>
            <li>样本文件与全部缓存（反编译缓存 / IDA 库 / Ghidra 工程）</li>
          </ul>
          {delMsg && <p className="text-[11px] text-(--status-error)">{delMsg}</p>}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={delBusy}>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => { e.preventDefault(); void confirmDeleteSample() }}
              disabled={delBusy}
              className="bg-(--status-error) text-white hover:bg-(--status-error)/90"
            >
              {delBusy ? "删除中…" : "确认删除"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function EmptyUpload({ onUpload, uploading }: { onUpload: (f: File) => void; uploading: boolean }) {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <div className="max-w-md rounded-lg border border-dashed p-8 text-center">
        <Upload className="mx-auto mb-3 size-8 text-muted-foreground" />
        <h2 className="mb-1 text-sm font-medium">上传第一个样本</h2>
        <p className="mb-4 text-[11px] leading-relaxed text-muted-foreground">
          上传后点「开始分析」跑 headless 全量导出（IDA 优先，Ghidra 兜底）；
          也可以在 IDA 里打开样本按 Ctrl-Alt-M，直接「从 IDA 拉取函数」。
          样本按 untrusted 处理，平台只做静态解析，<b>绝不在任何路径执行样本</b>。
        </p>
        <label className="inline-flex cursor-pointer items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground">
          <Upload className="size-3.5" />
          {uploading ? "上传中…" : "选择样本文件（≤256MB）"}
          <input type="file" className="hidden" onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) onUpload(f)
            e.target.value = ""
          }} />
        </label>
        <p className="mt-4 flex items-center justify-center gap-1 text-[10px] text-muted-foreground">
          <ShieldAlert className="size-3" />
          动态分析（x64dbg 等）必须人工在沙箱里做，日志回流后才标 verified
        </p>
      </div>
    </div>
  )
}
