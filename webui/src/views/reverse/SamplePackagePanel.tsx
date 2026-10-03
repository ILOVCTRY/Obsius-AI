import { useEffect, useRef, useState } from "react"
import { FileArchive, FolderOpen, Loader2, Play, RefreshCw, Upload } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import type { SamplePackage, SamplePackageTarget, SampleTargetAnalysis } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function targetLabel(target: SamplePackageTarget): string {
  const format = target.format ? target.format.toUpperCase() : "文件"
  return `${format} · ${target.path}`
}

type TargetState = "idle" | "analyzing" | "done" | "failed"

/** 分析包入口：与旧 binary 工作台并列，支持目录、压缩包和 APK/AAB。 */
export function SamplePackagePanel({ pid, active = true, onOpenBinary }: {
  pid: string
  active?: boolean
  onOpenBinary?: (sha: string) => void
}) {
  const [packages, setPackages] = useState<SamplePackage[]>([])
  const [current, setCurrent] = useState<SamplePackage | null>(null)
  const [targetId, setTargetId] = useState<string>("")
  const [selectedTargetIds, setSelectedTargetIds] = useState<string[]>([])
  const [targetStates, setTargetStates] = useState<Record<string, TargetState>>({})
  const [targetReport, setTargetReport] = useState<SampleTargetAnalysis | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState<{ done: number; total: number } | null>(null)
  const [analyzing, setAnalyzing] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const folderRef = useRef<HTMLInputElement>(null)

  const refresh = () => api.samplePackages(pid).then((rows) => {
    setPackages(rows)
    if (!current && rows[0]) setCurrent(rows[0])
  }).catch(() => {})

  useEffect(() => { if (active) void refresh() }, [pid, active]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const target = current?.targets.find((row) => row.target_id === targetId) ?? current?.targets[0]
    if (target && target.target_id !== targetId) setTargetId(target.target_id)
    if (!target || !current) { setTargetReport(null); return }
    api.sampleTarget(pid, current.package_id, current.version_id, target.target_id)
      .then(setTargetReport).catch(() => setTargetReport(null))
  }, [pid, current, targetId])

  useEffect(() => {
    if (!current) {
      setSelectedTargetIds([])
      setTargetStates({})
      return
    }
    const first = current.targets[0]?.target_id
    setTargetId(first ?? "")
    setSelectedTargetIds(first ? [first] : [])
    setTargetStates(Object.fromEntries(current.targets.map((target) => [target.target_id, "idle"])))
    void Promise.all(current.targets.map(async (target) => {
      try {
        const row = await api.sampleTarget(pid, current.package_id, current.version_id, target.target_id)
        return [target.target_id, row.analysis ? "done" : "idle"] as const
      } catch { return [target.target_id, "idle"] as const }
    })).then((rows) => setTargetStates(Object.fromEntries(rows)))
  }, [pid, current])

  const upload = async (files: FileList | File[]) => {
    const list = Array.from(files)
    if (!list.length) return
    setUploading(true); setUploadProgress(null); setMessage(null)
    try {
      const isFolder = !!(list[0] as File & { webkitRelativePath?: string }).webkitRelativePath
      const result = list.length > 1 || isFolder
        ? await api.uploadSamplePackageFiles(pid, list)
        : list[0].size > 16 * 1024 * 1024
          ? await api.uploadSamplePackageResumable(pid, list[0], (done, total) => setUploadProgress({ done, total }))
          : await api.uploadSamplePackage(pid, list[0])
      setCurrent(result)
      setPackages((previous) => [result, ...previous.filter((item) => item.package_id !== result.package_id)])
      setTargetId(result.targets[0]?.target_id ?? "")
      setMessage(`已导入 ${result.file_count} 个文件，可选择目标开始分析`)
    } catch (error) {
      setMessage(`导入失败：${String(error)}`)
    } finally { setUploading(false); setUploadProgress(null) }
  }

  const analyzeTargets = async (ids: string[]) => {
    if (!current || !ids.length) return
    setAnalyzing(true); setMessage(null)
    let completed = 0
    let failed = 0
    try {
      await api.selectSampleTargets(pid, current.package_id, current.version_id, ids)
      for (const id of ids) {
        setTargetStates((previous) => ({ ...previous, [id]: "analyzing" }))
        try {
          const result = await api.analyzeSampleTarget(pid, current.package_id, current.version_id, id)
          if (result.job_id) await pollJob(result.job_id, () => {}, 1200)
          const report = await api.sampleTarget(pid, current.package_id, current.version_id, id)
          setTargetStates((previous) => ({ ...previous, [id]: report.analysis ? "done" : "failed" }))
          if (id === targetId) setTargetReport(report)
          report.analysis ? completed++ : failed++
        } catch {
          failed++
          setTargetStates((previous) => ({ ...previous, [id]: "failed" }))
        }
      }
      setMessage(`批量分析完成：${completed} 个成功${failed ? `，${failed} 个失败` : ""}`)
    } catch (error) {
      setMessage(`批量分析失败：${String(error)}`)
    } finally { setAnalyzing(false) }
  }

  const analyze = () => analyzeTargets([targetId])

  const selectedTarget = current?.targets.find((target) => target.target_id === targetId) ?? null
  const binarySha = typeof targetReport?.analysis?.binary_sha256 === "string"
    ? targetReport.analysis.binary_sha256 : null
  return (
    <section className="shrink-0 border-b bg-background px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">分析包</span>
        <select
          className="h-7 max-w-64 rounded-md border bg-background px-2 text-[11px]"
          value={current?.package_id ?? ""}
          onChange={(event) => {
            const next = packages.find((item) => item.package_id === event.target.value) ?? null
            setCurrent(next); setTargetId(next?.targets[0]?.target_id ?? "")
          }}
        >
          <option value="">选择分析包</option>
          {packages.map((item) => <option key={`${item.package_id}:${item.version_id}`} value={item.package_id}>
            {item.origin?.filename || item.origin?.source_type || item.package_id.slice(0, 16)} · {item.file_count} 文件
          </option>)}
        </select>
        <input ref={fileRef} type="file" className="hidden" onChange={(event) => {
          if (event.target.files) void upload(event.target.files)
          event.target.value = ""
        }} />
        <input ref={folderRef} type="file" multiple className="hidden"
          {...({ webkitdirectory: "", directory: "" } as Record<string, string>)} onChange={(event) => {
            if (event.target.files) void upload(event.target.files)
            event.target.value = ""
          }} />
        <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" disabled={uploading} onClick={() => fileRef.current?.click()}>
          {uploading ? <Loader2 className="size-3 animate-spin" /> : <Upload className="size-3" />}
          {uploadProgress ? `上传中 ${uploadProgress.done}/${uploadProgress.total}` : uploading ? "上传中…" : "上传文件/压缩包"}
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" disabled={uploading} onClick={() => folderRef.current?.click()}>
          <FolderOpen className="size-3" />上传文件夹
        </Button>
        <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]" onClick={() => void refresh()} title="刷新分析包列表">
          <RefreshCw className="size-3" />刷新
        </Button>
      </div>
      {current && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
          <FileArchive className="size-3 text-muted-foreground" />
          <span className="text-muted-foreground">目标（勾选后可批量分析）</span>
          <div className="max-h-32 min-w-72 flex-1 overflow-auto rounded-md border bg-muted/10 p-1">
            {current.targets.map((target) => {
              const state = targetStates[target.target_id] ?? "idle"
              const checked = selectedTargetIds.includes(target.target_id)
              return (
                <label key={target.target_id}
                  className={cn("flex cursor-pointer items-center gap-2 rounded px-2 py-1 font-mono text-[10px] hover:bg-accent/40",
                    target.target_id === targetId && "bg-primary/10")}
                  onClick={() => setTargetId(target.target_id)}>
                  <input type="checkbox" checked={checked} onChange={(event) => {
                    setSelectedTargetIds((previous) => event.target.checked
                      ? [...previous, target.target_id]
                      : previous.filter((id) => id !== target.target_id))
                  }} />
                  <span className="min-w-0 flex-1 truncate">{targetLabel(target)}</span>
                  <span className="shrink-0 text-muted-foreground">{formatBytes(target.size)}</span>
                  <span className={cn("shrink-0", state === "done" ? "text-primary" : state === "failed" ? "text-destructive" : state === "analyzing" ? "text-(--status-approval)" : "text-muted-foreground")}>
                    {state === "done" ? "已完成" : state === "failed" ? "失败" : state === "analyzing" ? "分析中" : "未分析"}
                  </span>
                </label>
              )
            })}
          </div>
          <Button size="sm" className="h-7 gap-1 text-[11px]" disabled={!selectedTarget || analyzing} onClick={() => void analyze()}>
            {analyzing ? <Loader2 className="size-3 animate-spin" /> : <Play className="size-3" />}
            {analyzing ? "分析中…" : "开始目标分析"}
          </Button>
          <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]"
            disabled={!selectedTargetIds.length || analyzing}
            onClick={() => void analyzeTargets(selectedTargetIds)}>
            <Play className="size-3" />批量分析（{selectedTargetIds.length}）
          </Button>
          {binarySha && onOpenBinary && (
            <Button size="sm" variant="outline" className="h-7 gap-1 text-[11px]"
              onClick={() => onOpenBinary(binarySha)} title="在现有逆向工作台打开该目标">
              打开逆向工作台
            </Button>
          )}
          <span className="text-muted-foreground">{current.file_count} 文件 · {formatBytes(current.total_bytes)}</span>
          {targetReport?.analysis && <span className="text-primary">已完成</span>}
        </div>
      )}
      {current && current.dependencies.length > 0 && (
        <details className="mt-2 rounded-md border bg-muted/10 px-2 py-1 text-[10px]">
          <summary className="cursor-pointer text-muted-foreground">
            依赖关系（{current.dependencies.length} 条）
          </summary>
          <div className="mt-1 max-h-24 overflow-auto font-mono">
            {current.dependencies.map((edge, index) => (
              <div key={`${edge.from}-${edge.to}-${index}`} className="flex gap-2 py-0.5">
                <span className="min-w-0 flex-1 truncate" title={edge.from}>{edge.from}</span>
                <span className="shrink-0 text-muted-foreground">→ {edge.kind}</span>
                <span className="min-w-0 flex-1 truncate" title={edge.to}>{edge.to}</span>
              </div>
            ))}
          </div>
        </details>
      )}
      {message && <p className={cn("mt-1 truncate text-[10px]", message.includes("失败") ? "text-destructive" : "text-muted-foreground")} title={message}>{message}</p>}
    </section>
  )
}
