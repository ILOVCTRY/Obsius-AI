import { useCallback, useEffect, useRef, useState } from "react"
import { Upload, RefreshCw, Maximize2 } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import type { Asset, BinaryOverview, Finding, FuncEntry } from "@/lib/types"
import { hexAddr } from "@/lib/workbench"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToolLamp, mcpLampHint } from "./SampleBar"

// 直播间右侧 384px 挂机侧栏：风险函数（默认）/ 发现 / 样本。只读为主，动作最小集。

export function RevCompact({ pid, onOpenWorkbench }: { pid: string; onOpenWorkbench: () => void }) {
  const [samples, setSamples] = useState<Asset[]>([])
  const [sha, setSha] = useState<string | null>(null)
  const [overview, setOverview] = useState<BinaryOverview | null>(null)
  const [funcs, setFuncs] = useState<FuncEntry[]>([])
  const [findings, setFindings] = useState<Finding[]>([])
  const [triaging, setTriaging] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(() => {
    api.assets(pid).then(setSamples).catch(() => {})
    api.findings(pid).then(setFindings).catch(() => {})
  }, [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  const binaries = samples.filter((a) => a.type === "binary")
  useEffect(() => {
    if (sha && binaries.some((a) => a.value === sha)) return
    setSha(binaries.length ? binaries[binaries.length - 1].value : null)
  }, [samples, sha]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!sha) { setOverview(null); setFuncs([]); return }
    let alive = true
    const load = () => {
      api.binaryOverview(pid, sha).then((o) => alive && setOverview(o)).catch(() => {})
      api.funcs(pid, sha).then((f) => alive && setFuncs(f)).catch(() => {})
    }
    load()
    const t = setInterval(load, 4000)
    return () => { alive = false; clearInterval(t) }
  }, [pid, sha])

  const riskFuncs = funcs.filter((f) => f.risk_tags.length)
  const assetId = binaries.find((a) => a.value === sha)?.id
  const relFindings = findings
    .filter((f) => !assetId || f.target_asset_id === assetId)
    .sort((a, b) =>
      Number(a.status === "verified") - Number(b.status === "verified") ||
      b.created_at.localeCompare(a.created_at))

  const runJob = async (jobId: string | null) => {
    if (!jobId) return
    setTriaging(true)
    try { await pollJob(jobId, () => {}, 1500) } finally { setTriaging(false) }
  }
  const upload = async (file: File) => {
    const r = await api.uploadSample(pid, file)
    setSha(r.sha)
    refresh()
    await runJob(r.job_id)
    refresh()
  }
  const retry = async () => {
    if (!sha) return
    const r = await api.retryTriage(pid, sha)
    await runJob(r.job_id)
  }

  return (
    <Tabs defaultValue="risk" className="flex h-full flex-col gap-0">
      <TabsList className="w-full justify-start rounded-none border-b bg-transparent p-0">
        <TabsTrigger value="risk" className="rounded-none border-b-2 px-3 py-1.5 text-[11px]">风险 {riskFuncs.length}</TabsTrigger>
        <TabsTrigger value="findings" className="rounded-none border-b-2 px-3 py-1.5 text-[11px]">发现 {relFindings.length}</TabsTrigger>
        <TabsTrigger value="sample" className="rounded-none border-b-2 px-3 py-1.5 text-[11px]">样本</TabsTrigger>
      </TabsList>

      <TabsContent value="risk" className="min-h-0 flex-1 overflow-auto">
        {riskFuncs.length === 0 && (
          <p className="p-3 text-[11px] text-muted-foreground">
            还没有标记风险的函数。在全屏工作台的「笔记」里给函数加 risk_tags。
          </p>
        )}
        {riskFuncs.map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={onOpenWorkbench}
            title="到全屏工作台查看"
            className="block w-full border-b px-3 py-1.5 text-left hover:bg-accent/40"
          >
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-(--status-error)">{f.name}</span>
              <span className="shrink-0 rounded bg-(--status-error)/15 px-1 text-[9px] text-(--status-error)">
                {f.risk_tags[0]}
              </span>
            </div>
            <div className="font-mono text-[9px] text-muted-foreground">{hexAddr(f.address)} · {f.confidence}</div>
          </button>
        ))}
      </TabsContent>

      <TabsContent value="findings" className="min-h-0 flex-1 overflow-auto">
        {relFindings.length === 0 && <p className="p-3 text-[11px] text-muted-foreground">暂无发现</p>}
        {relFindings.map((f) => (
          <div key={f.id} className="border-b px-3 py-1.5">
            <div className="flex items-center gap-1.5">
              <span className="min-w-0 flex-1 truncate text-[11px]">{f.title}</span>
              <span className={f.status === "verified" ? "text-[9px] text-primary" : "text-[9px] text-muted-foreground"}>
                {f.status}
              </span>
            </div>
            <div className="font-mono text-[9px] uppercase text-muted-foreground">
              {f.severity} · {f.vuln_class}
            </div>
          </div>
        ))}
      </TabsContent>

      <TabsContent value="sample" className="min-h-0 flex-1 overflow-auto p-2">
        <div className="space-y-1">
          {binaries.map((a) => (
            <button
              key={a.id}
              type="button"
              onClick={() => setSha(a.value)}
              className={
                "block w-full truncate rounded px-2 py-1 text-left font-mono text-[11px] " +
                (a.value === sha ? "bg-primary/10 text-primary" : "hover:bg-accent/40")
              }
              title={a.value}
            >
              {String(a.meta?.filename ?? a.value)}
            </button>
          ))}
          {binaries.length === 0 && <p className="text-[11px] text-muted-foreground">还没有样本</p>}
        </div>

        {overview && (
          <div className="mt-3 space-y-1.5 rounded border p-2">
            <div className="font-mono text-[10px] text-muted-foreground">
              {overview.function_count} 函数 · 已分析 {overview.analyzed_count} · 风险 {overview.risk_count}
            </div>
            {!overview.cached && (
              <div className="text-[10px] text-(--status-approval)">
                {triaging ? "headless 分诊中…" : "未分诊（缺少 IDA/Ghidra 后端时请在设置中检查工具）"}
              </div>
            )}
            <div className="flex items-center gap-2">
              <ToolLamp state={overview.tools.ida.state} label="IDA" />
              <ToolLamp state={overview.tools.ghidra.state} label="Ghidra" />
              <ToolLamp state={overview.tools.mcp.state} label="MCP" hint={mcpLampHint(overview.tools.mcp.state)} />
            </div>
          </div>
        )}

        <input
          ref={fileRef} type="file" className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = "" }}
        />
        <div className="mt-3 flex flex-wrap gap-1.5">
          <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]"
                  onClick={() => fileRef.current?.click()}>
            <Upload className="size-3" />上传
          </Button>
          <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]"
                  disabled={!sha || triaging} onClick={retry}>
            <RefreshCw className={triaging ? "size-3 animate-spin" : "size-3"} />重新分诊
          </Button>
          <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" onClick={onOpenWorkbench}>
            <Maximize2 className="size-3" />全屏工作台
          </Button>
        </div>
      </TabsContent>
    </Tabs>
  )
}
