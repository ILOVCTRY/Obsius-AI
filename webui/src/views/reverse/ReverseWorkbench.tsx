import { Suspense, lazy, useCallback, useEffect, useState } from "react"
import { Upload, ShieldAlert } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import { useEvents } from "@/lib/useEvents"
import type {
  Asset, BinaryOverview, CachedFuncRow, CachedFunction, Finding, FuncEntry,
  PullNamesResult, XrefData,
} from "@/lib/types"
import { hexAddr } from "@/lib/workbench"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import type { ChainNodeType } from "@/lib/types"
import { SampleBar } from "./SampleBar"
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

export function ReverseWorkbench({ pid }: { pid: string }) {
  const { events } = useEvents(pid)
  const [tick, setTick] = useState(0)
  const bump = useCallback(() => setTick((t) => t + 1), [])
  useEffect(() => { bump() }, [events.length, bump])

  const [samples, setSamples] = useState<Asset[]>([])
  const [sha, setSha] = useState<string | null>(null)
  const [overview, setOverview] = useState<BinaryOverview | null>(null)
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

  // subnav：逆向分析｜攻击链｜蓝图｜业务逻辑（切页不卸载分析状态，同级条件渲染）
  const [mode, setMode] = useState<"rev" | "chains" | "blueprint" | "logic">("rev")
  const [rightTab, setRightTab] = useState("xref")
  const [focusFinding, setFocusFinding] = useState<string | null>(null)

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
  useEffect(() => {
    refreshSamples()
    const t = setInterval(refreshSamples, 4000)
    return () => clearInterval(t)
  }, [refreshSamples])
  useEffect(() => { refreshSamples() }, [tick, refreshSamples])

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
    if (!sha) return
    let alive = true
    const load = () =>
      api.binaryOverview(pid, sha).then((o) => alive && setOverview(o)).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, sha, tick])

  // 缓存函数行（cached 前 409 → 留空）
  useEffect(() => {
    if (!sha || !overview?.cached) { setRows([]); return }
    let alive = true
    api.cachedFunctions(pid, sha).then((r) => alive && setRows(r)).catch(() => {})
    return () => { alive = false }
  }, [pid, sha, overview?.cached, tick])

  // func_kb（该样本分析过的函数）
  useEffect(() => {
    if (!sha) return
    let alive = true
    const load = () => api.funcs(pid, sha).then((f) => alive && setFuncs(f)).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, sha, tick])

  // 发现（项目级，右栏按 binary 资产/func_id/address 客户端过滤）
  useEffect(() => {
    let alive = true
    const load = () => api.findings(pid).then((f) => alive && setFindings(f)).catch(() => {})
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, tick])

  // MCP 实时桥：缓存未分诊/被移走时，单函数伪码与 xref 仍可经 IDA 当前库实时取（列表不替代缓存）
  const mcpLive = overview?.tools.mcp.state === "installed"
  const funcReadable = !!overview?.cached || mcpLive

  // 选中函数：伪码 + xref（headless 缓存优先；缺席且 MCP 在线时后端自动实时降级）
  useEffect(() => {
    if (!sha || !addr || !funcReadable) { setDetail(null); setXref(null); return }
    let alive = true
    setDetailLoading(true)
    Promise.allSettled([
      api.cachedFunction(pid, sha, addr),
      api.binaryXrefs(pid, sha, addr),
    ]).then(([df, xf]) => {
      if (!alive) return
      setDetail(df.status === "fulfilled" ? df.value : null)
      setXref(xf.status === "fulfilled" ? xf.value : null)
      setDetailLoading(false)
    })
    return () => { alive = false }
  }, [pid, sha, addr, funcReadable, tick]) // eslint-disable-line react-hooks/exhaustive-deps

  const currentAsset = binaries.find((a) => a.value === sha) ?? null
  const kbFunc = funcs.find((f) => hexAddr(f.address) === addr) ?? null
  const cacheName = rows.find((r) => r.address === addr)?.name ?? null

  const awaitJob = async (jobId: string | null) => {
    if (!jobId) { bump(); return }
    setTriaging(true)
    const job = await pollJob(jobId, () => {}, 1500)
    setTriaging(false)
    bump()
    if (job.status === "error") setTriaging(false)
  }

  const handleUpload = async (file: File) => {
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
    const r = await api.retryTriage(pid, sha)
    await awaitJob(r.job_id)
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

  // AI triage：只发被动任务（不投 Job 不开 Agent）；objective 自包含
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
      await api.publishTask(pid, { task_type: "triage", noise_budget: "passive", objective })
      setTimeout(() => setBusyAi(false), 2500)
    } catch {
      setBusyAi(false)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <SampleBar
        samples={binaries} sha={sha} onSelect={setSha} overview={overview}
        triaging={triaging} uploading={uploading} busyAi={busyAi}
        onUpload={handleUpload} onRetry={handleRetry} onAiTriage={handleAiTriage}
        onPullNames={handlePullNames}
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
                mcpLive={mcpLive} cached={!!overview?.cached}
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
                               kb={kbFunc} onSaved={bump} mcpLive={mcpLive} />
                  : <p className="p-3 text-[11px] text-muted-foreground">先在左侧选中函数</p>}
              </TabsContent>
            </Tabs>
          </div>
        </div>
      )}
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
          上传后自动跑 headless 反编译（IDA 优先，Ghidra 兜底），全量客观函数进缓存；
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
