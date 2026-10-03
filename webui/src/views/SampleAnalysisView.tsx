import { useEffect, useMemo, useRef, useState } from "react"
import { Binary, Braces, ChevronDown, ChevronRight, Code2, FileCode2, FileText, FolderOpen, MoreHorizontal, Play, RotateCcw, Search, Table2, Trash2, Upload } from "lucide-react"
import { api, pollJob } from "@/lib/api"
import type { SamplePackage, SamplePackageEntry, SamplePackagePreview, SamplePackageTarget, SampleTargetAnalysis } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

type Filter = "all" | "candidate" | "analyzed" | "unanalyzed"
type TreeNode = { name: string; path: string; directory: boolean; entry?: SamplePackageEntry; children: TreeNode[] }
const binaryFormats = new Set(["pe", "elf", "mach-o"])
const sizeText = (n: number) => n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB"

function buildTree(entries: SamplePackageEntry[]): TreeNode {
  const root: TreeNode = { name: "分析包", path: "", directory: true, children: [] }
  for (const entry of entries) {
    let parent = root
    const parts = entry.path.split("/").filter(Boolean)
    parts.forEach((part, index) => {
      const path = parts.slice(0, index + 1).join("/")
      let child = parent.children.find((node) => node.name === part)
      if (!child) { child = { name: part, path, directory: index < parts.length - 1, children: [] }; parent.children.push(child) }
      if (index === parts.length - 1) { child.directory = false; child.entry = entry }
      parent = child
    })
  }
  const sort = (node: TreeNode) => { node.children.sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name)); node.children.forEach(sort) }
  sort(root)
  return root
}

export function SampleAnalysisView({ pid, onOpenBinary }: { pid: string; onOpenBinary: (sha: string) => void }) {
  const [pkg, setPkg] = useState<(SamplePackage & { undo_available?: boolean }) | null>(null)
  const [selectedPath, setSelectedPath] = useState("")
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set([""]))
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<Filter>("all")
  const [reports, setReports] = useState<Record<string, SampleTargetAnalysis | null>>({})
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [moreOpen, setMoreOpen] = useState(false)
  const restoredPid = useRef<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const folderRef = useRef<HTMLInputElement>(null)
  const refresh = () => api.currentSamplePackage(pid).then(setPkg).catch(() => setPkg(null))
  useEffect(() => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(`sample-analysis:${pid}`) || "null") as { selectedPath?: string; expanded?: string[]; query?: string; filter?: Filter } | null
      if (saved) {
        setSelectedPath(saved.selectedPath || "")
        setExpanded(new Set(["", ...(saved.expanded || [])]))
        setQuery(saved.query || "")
        setFilter(saved.filter || "all")
      }
    } catch { /* invalid local state is disposable */ }
    restoredPid.current = pid
  }, [pid])
  useEffect(() => {
    if (restoredPid.current !== pid) return
    window.localStorage.setItem(`sample-analysis:${pid}`, JSON.stringify({
      selectedPath, expanded: Array.from(expanded), query, filter,
    }))
  }, [pid, selectedPath, expanded, query, filter])
  useEffect(() => { void refresh() }, [pid])
  useEffect(() => {
    if (!pkg) return
    void Promise.all((pkg.targets || []).map(async (target) => {
      try { return [target.path, await api.sampleTarget(pid, pkg.package_id, pkg.version_id, target.target_id)] as const }
      catch { return [target.path, null] as const }
    })).then((rows) => setReports(Object.fromEntries(rows)))
  }, [pid, pkg])
  const entries = pkg?.entries || []
  const targetByPath = useMemo(() => new Map((pkg?.targets || []).map((target) => [target.path, target])), [pkg])
  const tree = useMemo(() => buildTree(entries), [entries])
  const selectedEntry = entries.find((entry) => entry.path === selectedPath) || null
  const selectedTarget = selectedEntry ? targetByPath.get(selectedEntry.path) || null : null
  const selectedReport = selectedEntry ? reports[selectedEntry.path] || null : null
  const matches = (node: TreeNode): boolean => {
    if (node.directory) return node.children.some(matches)
    if (!node.entry) return false
    const q = query.trim().toLowerCase()
    if (q && !node.entry.path.toLowerCase().includes(q)) return false
    const analyzed = !!reports[node.entry.path]?.analysis
    if (filter === "candidate" && !node.entry.candidate) return false
    if (filter === "analyzed" && !analyzed) return false
    if (filter === "unanalyzed" && (!node.entry.candidate || analyzed)) return false
    return true
  }
  const toggle = (path: string) => setExpanded((previous) => { const next = new Set(previous); next.has(path) ? next.delete(path) : next.add(path); return next })
  const upload = async (list: FileList | File[]) => {
    const files = Array.from(list); if (!files.length) return
    setBusy(true); setMessage(null)
    try {
      const isFolder = !!(files[0] as File & { webkitRelativePath?: string }).webkitRelativePath
      const result = pkg ? await api.appendSamplePackageFiles(pid, files) : isFolder ? await api.uploadSamplePackageFiles(pid, files) : await api.uploadSamplePackage(pid, files[0])
      setPkg(result); setSelectedPath(""); setMessage("文件已加入分析包根目录")
    } catch (error) { setMessage("上传失败：" + String(error)) }
    finally { setBusy(false) }
  }
  const analyze = async (target: SamplePackageTarget) => {
    if (!pkg) return
    setBusy(true); setMessage(null)
    try { const result = await api.analyzeSampleTarget(pid, pkg.package_id, pkg.version_id, target.target_id); if (result.job_id) await pollJob(result.job_id, () => {}, 1200); await refresh(); setMessage("分析完成") }
    catch (error) { setMessage("分析失败：" + String(error)) }
    finally { setBusy(false) }
  }
  const remove = async (path: string) => {
    if (!path || !window.confirm("确认删除 " + path + "？删除后可使用撤销恢复文件树。")) return
    setBusy(true)
    try { setPkg(await api.deleteSamplePackageEntry(pid, path)); setSelectedPath(""); setMessage("已删除，可撤销") }
    catch (error) { setMessage("删除失败：" + String(error)) }
    finally { setBusy(false) }
  }
  const undo = async () => { setBusy(true); try { setPkg(await api.undoSamplePackage(pid)); setMessage("已撤销最近一次文件树变更") } catch (error) { setMessage("撤销失败：" + String(error)) } finally { setBusy(false) } }
  const move = async (source: string, target: string) => { if (!pkg || !source || source === target) return; setBusy(true); try { setPkg(await api.moveSamplePackageEntry(pid, source, target)); setMessage("已移动，可撤销") } catch (error) { setMessage("移动失败：" + String(error)) } finally { setBusy(false) } }
  const renderNode = (node: TreeNode, depth = 0): React.ReactNode => {
    if (!matches(node)) return null
    const open = Boolean(query.trim()) || expanded.has(node.path)
    return <div key={node.path || "root"}>
      <div draggable={node.path !== ""} onDragStart={(event) => event.dataTransfer.setData("text/sample-path", node.path)} onDragOver={(event) => node.directory && event.preventDefault()} onDrop={(event) => { if (!node.directory) return; event.preventDefault(); const source = event.dataTransfer.getData("text/sample-path"); if (source) void move(source, node.path) }} className={cn("flex items-center gap-1 rounded px-2 py-1 text-[11px] hover:bg-accent/40", selectedPath === node.path && "bg-primary/10 text-primary")} style={{ paddingLeft: 8 + depth * 14 }} onClick={() => node.directory ? toggle(node.path) : setSelectedPath(node.path)} onDoubleClick={() => { if (!node.entry || !binaryFormats.has(node.entry.format)) return; const sha = reports[node.entry.path]?.analysis?.binary_sha256; if (typeof sha === "string") onOpenBinary(sha) }}>
        {node.directory ? (open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />) : <span className="size-3" />} {node.directory ? <FolderOpen className="size-3.5 shrink-0" /> : node.entry?.candidate && binaryFormats.has(node.entry.format) ? <FileCode2 className="size-3.5 shrink-0" /> : <FileText className="size-3.5 shrink-0" />}<span className="min-w-0 flex-1 truncate">{node.name}</span>{node.entry && <span className="text-[9px] text-muted-foreground">{node.entry.format.toUpperCase()}</span>}{node.entry?.candidate && <span className={cn("size-1.5 rounded-full", reports[node.entry.path]?.analysis ? "bg-primary" : "bg-muted-foreground/40")} />}
      </div>{node.directory && open && node.children.map((child) => renderNode(child, depth + 1))}
    </div>
  }
  return <div className="flex h-full min-h-0 w-full overflow-hidden"><aside className="flex w-[360px] shrink-0 flex-col border-r">
    <div className="flex items-center gap-2 border-b px-3 py-2"><FolderOpen className="size-4" /><span className="text-sm font-medium">样本分析</span><span className="flex-1" /><Button size="icon-xs" variant="ghost" title="撤销最近一次变更" disabled={!pkg?.undo_available || busy} onClick={() => void undo()}><RotateCcw className="size-3.5" /></Button><div className="relative"><Button size="icon-xs" variant="ghost" title="更多" onClick={() => setMoreOpen((v) => !v)}><MoreHorizontal className="size-3.5" /></Button>{moreOpen && <div className="absolute right-0 top-7 z-10 w-40 rounded-md border bg-popover p-1 shadow-md"><button className="w-full rounded px-2 py-1.5 text-left text-[11px] text-destructive hover:bg-accent" onClick={async () => { setMoreOpen(false); if (!window.confirm("确认删除整个分析包？分析结果、逆向资产和依赖也会被清理。")) return; setBusy(true); try { await api.deleteSamplePackage(pid); setPkg(null); setSelectedPath(""); setMessage("分析包已删除") } catch (error) { setMessage("删除失败：" + String(error)) } finally { setBusy(false) } }}>删除分析包</button></div>}</div></div>
    <div className="flex gap-1 border-b p-2"><Button size="sm" variant="outline" className="h-7 gap-1 text-[10px]" disabled={busy} onClick={() => fileRef.current?.click()}><Upload className="size-3" />上传文件</Button><Button size="sm" variant="outline" className="h-7 gap-1 text-[10px]" disabled={busy} onClick={() => folderRef.current?.click()}><FolderOpen className="size-3" />上传文件夹</Button></div>
    <input ref={fileRef} type="file" className="hidden" onChange={(event) => { if (event.target.files) void upload(event.target.files); event.target.value = "" }} /><input ref={folderRef} type="file" multiple className="hidden" {...({ webkitdirectory: "", directory: "" } as Record<string, string>)} onChange={(event) => { if (event.target.files) void upload(event.target.files); event.target.value = "" }} />
    <div className="flex gap-1 border-b p-2"><div className="relative min-w-0 flex-1"><Search className="absolute left-2 top-1.5 size-3 text-muted-foreground" /><input className="h-7 w-full rounded-md border bg-background pl-7 pr-2 text-[10px]" placeholder="搜索文件路径…" value={query} onChange={(event) => setQuery(event.target.value)} /></div><select className="h-7 rounded-md border bg-background px-1 text-[10px]" value={filter} onChange={(event) => setFilter(event.target.value as Filter)}><option value="all">全部</option><option value="candidate">可分析</option><option value="analyzed">已分析</option><option value="unanalyzed">未分析</option></select></div>
    <div className="min-h-0 flex-1 overflow-auto p-1">{pkg ? renderNode(tree) : <div className="p-6 text-center text-xs text-muted-foreground">上传文件或文件夹创建分析包</div>}</div>{message && <div className="border-t px-2 py-1 text-[10px] text-muted-foreground">{message}</div>}
  </aside><main className="min-w-0 flex-1 overflow-auto p-6">{selectedEntry ? <Detail pid={pid} entry={selectedEntry} target={selectedTarget} report={selectedReport} busy={busy} onAnalyze={analyze} onDelete={remove} onOpenBinary={onOpenBinary} /> : <div className="flex h-full items-center justify-center text-sm text-muted-foreground">从文件树选择文件查看详情</div>}</main></div>
}

function Detail({ pid, entry, target, report, busy, onAnalyze, onDelete, onOpenBinary }: { pid: string; entry: SamplePackageEntry; target: SamplePackageTarget | null; report: SampleTargetAnalysis | null; busy: boolean; onAnalyze: (target: SamplePackageTarget) => void; onDelete: (path: string) => void; onOpenBinary: (sha: string) => void }) {
  const analysis = report?.analysis
  const sha = typeof analysis?.binary_sha256 === "string" ? analysis.binary_sha256 : null
  return <div className="mx-auto max-w-3xl space-y-5"><div className="flex items-start gap-3"><FileText className="mt-1 size-5 text-primary" /><div className="min-w-0 flex-1"><h2 className="break-all text-lg font-semibold">{entry.path}</h2><p className="mt-1 text-xs text-muted-foreground">{entry.format.toUpperCase()} · {sizeText(entry.size)}{entry.platform ? " · " + entry.platform : ""}</p></div><Button size="sm" variant="outline" className="gap-1" onClick={() => onDelete(entry.path)} disabled={busy}><Trash2 className="size-3.5" />删除</Button></div><section className="rounded-md border p-4"><h3 className="mb-3 text-xs font-medium">文件信息</h3><dl className="grid grid-cols-[120px_1fr] gap-y-2 text-xs"><dt className="text-muted-foreground">路径</dt><dd className="break-all font-mono">{entry.path}</dd><dt className="text-muted-foreground">SHA-256</dt><dd className="break-all font-mono">{entry.sha256 || "未记录"}</dd><dt className="text-muted-foreground">分析状态</dt><dd>{analysis ? String(analysis.status || "已分析") : entry.candidate ? "未分析" : "无需分析"}</dd></dl></section><FilePreview pid={pid} entry={entry} />{target && <section className="rounded-md border p-4"><div className="mb-3 flex items-center justify-between"><h3 className="text-xs font-medium">分析操作</h3><span className="text-[10px] text-muted-foreground">{target.analyzers.join(" · ")}</span></div><div className="flex gap-2"><Button size="sm" className="gap-1" disabled={busy} onClick={() => onAnalyze(target)}><Play className="size-3.5" />开始分析</Button>{sha && <Button size="sm" variant="outline" onClick={() => onOpenBinary(sha)}>打开逆向工作台</Button>}</div></section>}{analysis && <AndroidDetails format={entry.format} analysis={analysis} />}{analysis && !["apk", "aab", "dex"].includes(entry.format) && <section className="rounded-md border p-4"><h3 className="mb-3 text-xs font-medium">分析摘要</h3><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-muted/30 p-3 font-mono text-[10px]">{JSON.stringify(analysis, null, 2)}</pre></section>}</div>
}

type PreviewMode = "auto" | "structure" | "text" | "hex"

function FilePreview({ pid, entry }: { pid: string; entry: SamplePackageEntry }) {
  const [preview, setPreview] = useState<SamplePackagePreview | null>(null)
  const [mode, setMode] = useState<PreviewMode>("auto")
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    setPreview(null); setError(null); setMode("auto")
    void api.samplePackagePreview(pid, entry.path).then(setPreview).catch((reason) => setError(String(reason)))
  }, [pid, entry.path, entry.sha256])
  if (error) return <section className="rounded-md border p-4 text-xs text-muted-foreground">预览不可用：{error}</section>
  if (!preview) return <section className="rounded-md border p-4 text-xs text-muted-foreground">正在读取预览…</section>
  const actualMode = mode === "auto" ? (preview.kind === "text" ? "text" : preview.kind === "structure" ? "structure" : preview.kind === "image" ? "image" : "hex") : mode
  const structure = preview.archive || preview.dex
  return <section className="overflow-hidden rounded-md border"><div className="flex flex-wrap items-center gap-1 border-b bg-muted/20 px-3 py-2"><Code2 className="mr-1 size-3.5 text-primary" /><h3 className="mr-auto text-xs font-medium">文件预览</h3>{(["auto", "structure", "text", "hex"] as PreviewMode[]).map((item) => <button key={item} className={cn("rounded px-2 py-1 text-[10px]", mode === item ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent")} onClick={() => setMode(item)}>{item === "auto" ? "自动" : item === "structure" ? <><Braces className="mr-1 inline size-3" />结构</> : item === "text" ? <><Table2 className="mr-1 inline size-3" />文本</> : <><Binary className="mr-1 inline size-3" />十六进制</>}</button>)}</div><div className="border-b px-3 py-1.5 text-[10px] text-muted-foreground">已读取 {sizeText(preview.preview_bytes)} / {sizeText(preview.size)}{preview.truncated ? " · 仅显示文件开头" : ""}</div>{actualMode === "image" && <div className="flex max-h-[520px] justify-center overflow-auto bg-muted/10 p-4"><img src={api.samplePackageContentUrl(pid, entry.path)} alt={entry.path} className="max-h-[480px] max-w-full object-contain" /></div>}{actualMode === "text" && <pre className="max-h-[520px] overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-[11px] leading-5">{preview.text || ""}</pre>}{actualMode === "structure" && <pre className="max-h-[520px] overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-[11px] leading-5">{structure ? JSON.stringify(structure, null, 2) : "该格式暂未提供结构解析，切换到十六进制查看原始内容。"}</pre>}{actualMode === "hex" && <div className="max-h-[520px] overflow-auto p-2 font-mono text-[10px]"><table className="w-full border-collapse"><thead><tr className="border-b text-left text-muted-foreground"><th className="w-20 px-2 py-1 font-normal">偏移</th><th className="px-2 py-1 font-normal">Hex</th><th className="w-36 px-2 py-1 font-normal">ASCII</th></tr></thead><tbody>{preview.hex_rows.map((row) => <tr key={row.offset} className="border-b border-border/40"><td className="px-2 py-1 text-muted-foreground">{row.offset.toString(16).padStart(8, "0")}</td><td className="whitespace-pre px-2 py-1">{row.hex}</td><td className="whitespace-pre px-2 py-1 text-muted-foreground">{row.ascii}</td></tr>)}</tbody></table></div>}</section>
}

function AndroidDetails({ format, analysis }: { format: string; analysis: Record<string, unknown> }) {
  const packageInfo = (analysis.package && typeof analysis.package === "object" ? analysis.package : {}) as Record<string, unknown>
  const manifest = (analysis.manifest && typeof analysis.manifest === "object" ? analysis.manifest : packageInfo.manifest && typeof packageInfo.manifest === "object" ? packageInfo.manifest : {}) as Record<string, unknown>
  const dex = (analysis.dex && typeof analysis.dex === "object" ? analysis.dex : {}) as Record<string, unknown>
  const dexFiles = Array.isArray(packageInfo.dex_files) ? packageInfo.dex_files : []
  const nativeAbis = packageInfo.native_abis && typeof packageInfo.native_abis === "object" ? packageInfo.native_abis as Record<string, unknown> : {}
  const permissions = Array.isArray(manifest.permissions) ? manifest.permissions : []
  const components = manifest.components && typeof manifest.components === "object" ? manifest.components as Record<string, unknown> : {}
  const value = (item: unknown) => item === undefined || item === null || item === "" ? "未提供" : String(item)
  return <div className="space-y-3">
    <section className="rounded-md border p-4"><h3 className="mb-3 text-xs font-medium">Android 包摘要</h3><dl className="grid grid-cols-[150px_1fr] gap-y-2 text-xs"><dt className="text-muted-foreground">格式</dt><dd>{format.toUpperCase()}</dd><dt className="text-muted-foreground">压缩包条目</dt><dd>{value(packageInfo.entry_count)}</dd><dt className="text-muted-foreground">展开后大小</dt><dd>{typeof packageInfo.total_uncompressed === "number" ? sizeText(packageInfo.total_uncompressed) : "未提供"}</dd><dt className="text-muted-foreground">DEX 数量</dt><dd>{dexFiles.length || value(dexFiles)}</dd><dt className="text-muted-foreground">Native ABI</dt><dd>{Object.keys(nativeAbis).join(", ") || "无"}</dd></dl></section>
    <section className="rounded-md border p-4"><h3 className="mb-3 text-xs font-medium">Manifest</h3><dl className="grid grid-cols-[150px_1fr] gap-y-2 text-xs"><dt className="text-muted-foreground">包名</dt><dd>{value(manifest.package)}</dd><dt className="text-muted-foreground">版本名</dt><dd>{value(manifest.version_name)}</dd><dt className="text-muted-foreground">版本号</dt><dd>{value(manifest.version_code)}</dd><dt className="text-muted-foreground">权限</dt><dd className="break-all">{permissions.length ? permissions.join(", ") : "未解析或无权限"}</dd><dt className="text-muted-foreground">组件</dt><dd>{Object.entries(components).flatMap(([kind, names]) => Array.isArray(names) ? names.map((name) => `${kind}: ${name}`) : []).join("; ") || "未解析"}</dd></dl></section>
    <section className="rounded-md border p-4"><h3 className="mb-3 text-xs font-medium">DEX</h3><dl className="grid grid-cols-[150px_1fr] gap-y-2 text-xs"><dt className="text-muted-foreground">文件</dt><dd className="break-all">{dexFiles.length ? dexFiles.join(", ") : "当前目标不是 APK/AAB 或未发现 DEX"}</dd><dt className="text-muted-foreground">method_ids_size</dt><dd>{value(dex.method_ids_size)}</dd><dt className="text-muted-foreground">class_defs_size</dt><dd>{value(dex.class_defs_size)}</dd><dt className="text-muted-foreground">string/type/proto/field</dt><dd>{[dex.string_ids_size, dex.type_ids_size, dex.proto_ids_size, dex.field_ids_size].map(value).join(" / ")}</dd></dl></section>
    <section className="rounded-md border p-4"><h3 className="mb-3 text-xs font-medium">Native 库</h3>{Object.keys(nativeAbis).length ? <div className="space-y-2 text-xs">{Object.entries(nativeAbis).map(([abi, names]) => <div key={abi}><div className="font-medium">{abi}</div><div className="break-all text-muted-foreground">{Array.isArray(names) ? names.join(", ") : value(names)}</div></div>)} </div> : <p className="text-xs text-muted-foreground">未发现 lib/&lt;abi&gt;/*.so</p>}</section>
  </div>
}
