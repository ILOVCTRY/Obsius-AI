import { useCallback, useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { KbEvolutionDraft, KbEvolutionSource, KbRefHit, KbSourceTree, Proposal } from "@/lib/types"
import type { PackInfo } from "@/lib/taxonomy"
import { Group, Panel, Separator } from "react-resizable-panels"
import {
  ArrowRight, BookOpen, CheckCircle2, FileCode2, FileText, GitPullRequest,
  Layers3, Network, RefreshCw, Sparkles, Workflow,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { KbPane, refsFromError } from "./KbPane"
import { KbTree } from "./KbTree"
import { KbCreateDialog, KbRenameDialog } from "./CreateDialogs"
import { cn } from "@/lib/utils"

// 知识库独立管理页（K7 2026-09-29：从技能页拆出）——左树 / 中编辑器 / 右大纲，
// 与技能页三栏同构。只管 packs/kb/<cap>/ 的文档（打法手册 + 经验沉淀），
// 与技能库（packs/*/skills/）彻底分离。

export interface KbFocus { path: string; n: number }

type LayerId = "playbook" | "pattern" | "case" | "ref"
type KbLayer = "all" | LayerId

const LAYERS: { id: LayerId; label: string; short: string; icon: typeof BookOpen; tone: string }[] = [
  { id: "ref", label: "原始资料", short: "REF", icon: FileText, tone: "text-muted-foreground" },
  { id: "case", label: "实战案例", short: "CASE", icon: Network, tone: "text-amber-300" },
  { id: "pattern", label: "技术模式", short: "PATTERN", icon: Workflow, tone: "text-violet-300" },
  { id: "playbook", label: "方法论", short: "PLAYBOOK", icon: BookOpen, tone: "text-primary" },
]

function layerOf(path: string): LayerId {
  const parts = path.toLowerCase().split("/")
  if (parts.includes("playbooks")) return "playbook"
  if (parts.includes("patterns")) return "pattern"
  if (parts.includes("cases")) return "case"
  return "ref"
}

function layerMeta(layer: KbLayer) {
  return LAYERS.find((item) => item.id === layer) ?? LAYERS[0]
}

function openProposals() {
  window.dispatchEvent(new CustomEvent("goto-settings", { detail: { tab: "proposals" } }))
}

function LayerIcon({ layer, size = 14 }: { layer: KbLayer; size?: number }) {
  const Icon = layer === "all" ? Layers3 : layerMeta(layer).icon
  return <Icon size={size} />
}

function KbRail({ cap, selected, selectedLayer, counts, pending, onProposal, evolving, recommendations, selectedSources, onStartEvolution, onToggleSource, onRunEvolution, busy, error, search, onSearch, hits, draft, onDraftChange, onSubmitDraft }: {
  cap: string
  selected: string | null
  selectedLayer: LayerId | null
  counts: Record<LayerId, number>
  pending: Proposal[]
  onProposal: () => void
  evolving: boolean
  recommendations: KbEvolutionSource[]
  selectedSources: string[]
  onStartEvolution: () => void
  onToggleSource: (path: string) => void
  onRunEvolution: () => void
  busy: boolean
  error: string | null
  search: string
  onSearch: (value: string) => void
  hits: KbEvolutionSource[]
  draft: KbEvolutionDraft | null
  onDraftChange: (draft: KbEvolutionDraft) => void
  onSubmitDraft: () => void
}) {
  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto p-3">
      <section className="border-b pb-3">
        <div className="flex items-center gap-2">
          <Sparkles size={14} className="text-primary" />
          <span className="text-[11px] font-semibold">知识演化</span>
          <span className="ml-auto font-mono text-[9px] text-muted-foreground">{cap.toUpperCase()}</span>
        </div>
        <div className="mt-3 flex items-center gap-1 text-[10px]">
          {LAYERS.map((item, i) => (
            <div key={item.id} className="flex min-w-0 flex-1 items-center gap-1">
              <div className={"flex min-w-0 flex-1 flex-col items-center gap-1 text-center " + item.tone}>
                <LayerIcon layer={item.id} size={15} />
                <span className="truncate text-[9px]">{item.label}</span>
                <span className="font-mono text-[9px] text-muted-foreground">{counts[item.id]}</span>
              </div>
              {i < LAYERS.length - 1 && <ArrowRight size={11} className="shrink-0 text-muted-foreground/50" />}
            </div>
          ))}
        </div>
      </section>

      {selected && selectedLayer !== "playbook" && (
        <section className="border-b pb-3">
          <div className="flex items-center gap-2">
            <Sparkles size={14} className="text-primary" />
            <span className="text-[11px] font-semibold">演化工作区</span>
          </div>
          {!evolving ? (
            <Button size="sm" className="mt-2 h-8 w-full gap-1 text-[10px]" onClick={onStartEvolution}>
              <Sparkles size={13} />开始演化
            </Button>
          ) : draft ? (
            <div className="mt-2 space-y-2">
              <p className="text-[9px] text-muted-foreground">AI 草稿，可编辑后提交人工审批</p>
              <input className="w-full rounded border bg-background px-2 py-1 text-[10px]" value={draft.title} onChange={(e) => onDraftChange({ ...draft, title: e.target.value })} />
              <input className="w-full rounded border bg-background px-2 py-1 font-mono text-[10px]" value={draft.path} onChange={(e) => onDraftChange({ ...draft, path: e.target.value })} />
              <select className="w-full rounded border bg-background px-2 py-1 text-[10px]" value={draft.kind} onChange={(e) => onDraftChange({ ...draft, kind: e.target.value as KbEvolutionDraft["kind"] })}>
                <option value="case">实战案例</option><option value="pattern">技术模式</option><option value="playbook">方法论</option>
              </select>
              <textarea className="min-h-[220px] w-full resize-y rounded border bg-background p-2 font-mono text-[10px]" value={draft.content} onChange={(e) => onDraftChange({ ...draft, content: e.target.value })} />
              <Button size="sm" className="h-7 w-full text-[10px]" disabled={busy || !draft.content.trim()} onClick={onSubmitDraft}>提交人工审批</Button>
            </div>
          ) : (
            <div className="mt-2 space-y-2">
              <div className="flex gap-1"><input className="min-w-0 flex-1 rounded border bg-background px-2 py-1 text-[10px]" placeholder="搜索补充资料" value={search} onChange={(e) => onSearch(e.target.value)} /><Button size="sm" variant="outline" className="h-7 text-[10px]" onClick={() => onSearch(search)}>搜索</Button></div>
              <p className="text-[9px] text-muted-foreground">已选 {selectedSources.length}/6（当前文档已自动加入）</p>
              {[...recommendations, ...hits].filter((item, i, all) => all.findIndex((x) => x.path === item.path) === i).map((item) => (
                <label key={item.path} className="flex cursor-pointer items-start gap-1.5 rounded border px-2 py-1.5 text-[10px] hover:bg-accent/30">
                  <input type="checkbox" checked={selectedSources.includes(item.path)} disabled={!selectedSources.includes(item.path) && selectedSources.length >= 6} onChange={() => onToggleSource(item.path)} />
                  <span className="min-w-0"><span className="block truncate" title={item.path}>{item.title || item.path}</span><span className="block truncate font-mono text-[9px] text-muted-foreground">{item.path}</span><span className="block text-[9px] text-muted-foreground">{item.reason}</span></span>
                </label>
              ))}
              {busy && <p className="text-[9px] text-primary">正在生成草稿…</p>}
              {error && <p className="break-words text-[9px] text-destructive">{error}</p>}
              <Button size="sm" className="h-7 w-full text-[10px]" disabled={busy || selectedSources.length === 0} onClick={onRunEvolution}>AI 提炼</Button>
            </div>
          )}
        </section>
      )}

      <section className="border-b pb-3">
        <div className="flex items-center gap-2">
          <FileCode2 size={14} className="text-muted-foreground" />
          <span className="text-[11px] font-semibold">当前文档</span>
        </div>
        {selected ? (
          <div className="mt-2 space-y-2">
            <p className="break-all font-mono text-[10px] text-primary">{selected}</p>
            <div className="flex items-center gap-1.5">
              {selectedLayer && <Badge variant="outline" className={"gap-1 text-[9px] " + layerMeta(selectedLayer).tone}><LayerIcon layer={selectedLayer} size={11} />{layerMeta(selectedLayer).label}</Badge>}
              <span className="text-[9px] text-muted-foreground">{selectedLayer === "ref" ? "事实来源" : "可演化层"}</span>
            </div>
          </div>
        ) : (
          <p className="mt-2 text-[10px] text-muted-foreground">尚未选择文档</p>
        )}
      </section>

      <section className="min-h-0">
        <div className="flex items-center gap-2">
          <GitPullRequest size={14} className="text-amber-300" />
          <span className="text-[11px] font-semibold">待审核演化</span>
          <Badge variant="outline" className="ml-auto text-[9px]">{pending.length}</Badge>
        </div>
        {pending.length > 0 ? (
          <div className="mt-2 space-y-1.5">
            {pending.slice(0, 5).map((item) => {
              const kind = (item.target.kind === "kb" || item.target.kind === "skill") ? "ref" : item.target.kind
              return (
                <button key={item.id} className="block w-full rounded border px-2 py-1.5 text-left transition-colors hover:border-primary/40 hover:bg-primary/5" onClick={onProposal}>
                  <div className="flex items-center gap-1.5">
                    <Badge variant="outline" className={"text-[9px] " + layerMeta(kind as KbLayer).tone}>{layerMeta(kind as KbLayer).label}</Badge>
                    <span className="min-w-0 flex-1 truncate text-[10px]">{item.summary}</span>
                  </div>
                  <span className="mt-1 block truncate font-mono text-[9px] text-muted-foreground">{item.target.cap}/{item.target.path}</span>
                </button>
              )
            })}
            {pending.length > 5 && <p className="text-[9px] text-muted-foreground">还有 {pending.length - 5} 条待审核</p>}
            <Button size="sm" variant="outline" className="mt-1 h-7 w-full text-[10px]" onClick={onProposal}>打开提案中心</Button>
          </div>
        ) : (
          <div className="mt-2 flex items-center gap-2 rounded border border-dashed px-2 py-2 text-[10px] text-muted-foreground">
            <CheckCircle2 size={13} className="text-emerald-300" /> 当前没有待审核提案
          </div>
        )}
      </section>
    </div>
  )
}

export function KbView({ cap, capabilities, onCapChange, focus }: {
  cap: string
  capabilities?: PackInfo[]
  onCapChange?: (cap: string) => void
  focus?: KbFocus | null
}) {
  const [kbCap, setKbCap] = useState(cap)
  const [sources, setSources] = useState<KbSourceTree[]>([])
  const [path, setPath] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [renamePath, setRenamePath] = useState<string | null>(null)
  const [del, setDel] = useState<{ path: string; refs: KbRefHit[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [layer, setLayer] = useState<KbLayer>("all")
  const [pending, setPending] = useState<Proposal[]>([])
  const [mobilePane, setMobilePane] = useState<"tree" | "editor" | "rail">("tree")
  const [openMode, setOpenMode] = useState<"edit" | "preview">("preview")
  const [evolving, setEvolving] = useState(false)
  const [recommendations, setRecommendations] = useState<KbEvolutionSource[]>([])
  const [selectedSources, setSelectedSources] = useState<string[]>([])
  const [evolutionSearch, setEvolutionSearch] = useState("")
  const [evolutionHits, setEvolutionHits] = useState<KbEvolutionSource[]>([])
  const [evolutionBusy, setEvolutionBusy] = useState(false)
  const [evolutionError, setEvolutionError] = useState<string | null>(null)
  const [draft, setDraft] = useState<KbEvolutionDraft | null>(null)

  const reload = useCallback(() => {
    api.kbList(kbCap).then((r) => setSources(r.sources)).catch(() => setSources([]))
  }, [kbCap])
  useEffect(reload, [reload])

  useEffect(() => {
    let alive = true
    api.proposals("pending").then((rows) => {
      if (alive) setPending(rows.filter((p) => p.target.cap === kbCap && ["case", "pattern", "playbook"].includes(p.target.kind)))
    }).catch(() => { if (alive) setPending([]) })
    return () => { alive = false }
  }, [kbCap])

  // 顶部能力包切换：未保存修改拦截
  useEffect(() => {
    if (cap === kbCap) return
    if (dirty && !window.confirm(
      `知识库文档 ${path} 有未保存修改，切到能力包 ${cap} 将放弃修改，继续？`)) {
      onCapChange?.(kbCap)
      return
    }
    setKbCap(cap)
    setPath(null)
    setDirty(false)
    setLayer("all")
    setMobilePane("tree")
    setOpenMode("preview")
  }, [cap, kbCap, dirty, path, onCapChange])

  const clearMissingDocument = useCallback(() => {
    setPath(null)
    setDirty(false)
    setLayer("all")
    reload()
  }, [reload])
  const selectedLayer = path ? layerOf(path) : null

  const startEvolution = useCallback(async () => {
    if (!path || selectedLayer === "playbook") return
    setEvolving(true); setDraft(null); setEvolutionError(null); setEvolutionBusy(true)
    try {
      const result = await api.kbEvolutionRecommend(kbCap, path, 5)
      setRecommendations(result.results)
      setSelectedSources([path])
    } catch (e) { setEvolutionError(String(e)) }
    finally { setEvolutionBusy(false) }
  }, [kbCap, path, selectedLayer])

  const searchEvolution = useCallback(async (query = evolutionSearch) => {
    if (!query.trim()) { setEvolutionHits([]); return }
    try {
      const result = await api.kbSearch(kbCap, query.trim(), 20)
      setEvolutionHits(result.results.filter((item) => item.path !== path).map((item) => ({
        path: item.path, source: item.source, title: item.title, kind: item.kind,
        layer: item.layer, snippet: item.snippet, reason: "搜索命中",
      })))
    } catch (e) { setEvolutionError(String(e)) }
  }, [evolutionSearch, kbCap, path])

  const runEvolution = useCallback(async () => {
    if (selectedSources.length === 0) return
    setEvolutionBusy(true); setEvolutionError(null)
    try {
      const { job_id } = await api.kbEvolution(kbCap, selectedSources)
      for (;;) {
        const job = await api.job(job_id)
        if (job.status === "done") { setDraft(job.result as KbEvolutionDraft); break }
        if (job.status === "error") { throw new Error(job.error || "知识演化生成失败") }
        await new Promise((resolve) => setTimeout(resolve, 1200))
      }
    } catch (e) { setEvolutionError(String(e)) }
    finally { setEvolutionBusy(false) }
  }, [kbCap, selectedSources])

  const submitDraft = useCallback(async () => {
    if (!draft) return
    setEvolutionBusy(true); setEvolutionError(null)
    try {
      await api.createProposal({
        target: { kind: draft.kind, cap: kbCap, path: draft.path }, mode: "create",
        content: draft.content, summary: draft.summary || draft.title,
        reason: `由 ${draft.sources.length} 篇知识库资料提炼，提交人工审批`, origin: "human",
        evidence: "知识演化草稿（来源见 source_refs）", source_refs: draft.sources,
      })
      setDraft(null); setEvolving(false)
      const rows = await api.proposals("pending")
      setPending(rows.filter((p) => p.target.cap === kbCap && ["case", "pattern", "playbook"].includes(p.target.kind)))
    } catch (e) { setEvolutionError(String(e)) }
    finally { setEvolutionBusy(false) }
  }, [draft, kbCap])

  // doctor/深链跳转：选中指定文件
  useEffect(() => {
    if (focus) {
      setPath(focus.path)
      setOpenMode("preview")
      setMobilePane("editor")
    }
  }, [focus])

  const doDelete = async (force: boolean) => {
    if (!del) return
    setBusy(true)
    try {
      await api.kbDelete(kbCap, del.path, force)
      if (path === del.path) { setPath(null); setDirty(false) }
      setDel(null)
      window.dispatchEvent(new Event("packs-changed"))
      reload()
    } catch (e) {
      const refs = refsFromError(e)
      if (refs.length > 0) setDel({ path: del.path, refs })
      else setDel(null)
    } finally {
      setBusy(false)
    }
  }

  const allFiles = useMemo(() => sources.flatMap((source) => source.files), [sources])
  const counts = useMemo(() => {
    const out: Record<LayerId, number> = { playbook: 0, pattern: 0, case: 0, ref: 0 }
    allFiles.forEach((file) => { out[layerOf(file.path)] += 1 })
    return out
  }, [allFiles])
  const visibleSources = useMemo(() => layer === "all" ? sources : sources.map((source) => ({
    ...source, files: source.files.filter((file) => layerOf(file.path) === layer),
  })).filter((source) => source.files.length > 0), [layer, sources])
  const kbTotal = allFiles.length

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b px-4 py-3">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-[190px] flex-1">
            <div className="flex min-w-0 items-center gap-2"><BookOpen size={17} className="shrink-0 text-primary" /><span className="truncate text-sm font-semibold">知识库工作台</span><Badge variant="outline" className="shrink-0 font-mono text-[9px]">{kbCap}</Badge><span className="shrink-0 text-[10px] text-muted-foreground">{kbTotal} 份资料</span></div>
          </div>
          <div className="flex items-center gap-1.5">
            {capabilities && capabilities.length > 0 && <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
              能力包
              <select className="rounded border bg-background px-2 py-1 text-xs [color-scheme:dark]" value={cap} onChange={(e) => onCapChange?.(e.target.value)}>
                {capabilities.map((item) => <option key={item.name} value={item.name}>{item.label || item.name}</option>)}
              </select>
            </label>}
            <Button size="icon-sm" variant="outline" title="刷新知识库" aria-label="刷新知识库" onClick={reload}><RefreshCw size={14} /></Button>
            <Button size="sm" variant="outline" className="gap-1 text-[10px]" onClick={() => setCreateOpen(true)}><FileText size={13} />新建文档</Button>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <button className={cn("inline-flex items-center gap-1 rounded border px-2 py-1 text-[10px]", layer === "all" ? "border-primary/40 bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")} onClick={() => setLayer("all")}><Layers3 size={12} />全部 <span className="font-mono">{kbTotal}</span></button>
          {LAYERS.map((item) => <button key={item.id} className={cn("inline-flex items-center gap-1 rounded border px-2 py-1 text-[10px]", layer === item.id ? "border-primary/40 bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")} onClick={() => setLayer(item.id)}><item.icon size={12} />{item.label} <span className="font-mono">{counts[item.id]}</span></button>)}
          <span className="ml-auto hidden font-mono text-[9px] text-muted-foreground md:inline">REFS → CASES → PATTERNS → PLAYBOOKS</span>
        </div>
        <div className="kb-mobile-tabs" role="tablist" aria-label="知识库工作区">
          <button className={cn(mobilePane === "tree" && "is-active")} onClick={() => setMobilePane("tree")}><Layers3 size={12} />文档树</button>
          <button className={cn(mobilePane === "editor" && "is-active")} onClick={() => setMobilePane("editor")}><FileCode2 size={12} />编辑器</button>
          <button className={cn(mobilePane === "rail" && "is-active")} onClick={() => setMobilePane("rail")}><Sparkles size={12} />演化</button>
        </div>
      </div>
      <div className="min-h-0 flex-1">
      <Group orientation="horizontal" className="kb-workbench-group h-full">
        {/* 左栏：知识库树 */}
        <Panel className={cn("kb-workbench-panel kb-panel-tree", mobilePane !== "tree" && "kb-mobile-collapsed")} defaultSize={260} minSize="14%">
            <div className="flex h-full min-h-0 flex-col">
              <div className="flex shrink-0 items-center gap-1 border-b px-1.5 py-1">
                <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground"
                      title={kbCap}>📚 {kbCap} 知识库（{kbTotal}）</span>
              <button className="shrink-0 rounded px-1.5 text-[11px] text-primary hover:bg-accent/40"
                      title="新建知识库 md" onClick={() => setCreateOpen(true)}>＋新建</button>
            </div>
            <div className="min-h-0 flex-1">
              <KbTree cap={kbCap} sources={visibleSources} selected={path}
                      onSelect={(nextPath) => { setPath(nextPath); setOpenMode("preview") }} onRename={setRenamePath}
                      onDelete={(p) => setDel({ path: p, refs: [] })} />
            </div>
          </div>
        </Panel>
        <Separator className="kb-mobile-divider z-10 h-full w-px shrink-0 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary" />
        {/* 中栏：编辑器 */}
        <Panel className={cn("kb-workbench-panel kb-panel-editor", mobilePane !== "editor" && "kb-mobile-collapsed")} minSize="30%">
          {path ? (
            <KbPane key={`${kbCap}/${path}`} cap={kbCap} path={path} reloadKey={0}
                    onDirtyChange={setDirty}
                    onSaved={() => { window.dispatchEvent(new Event("packs-changed")); reload() }}
                    onRename={setRenamePath} onDelete={(p) => setDel({ path: p, refs: [] })}
                    onOpenKb={(nextPath) => { setPath(nextPath); setOpenMode("preview") }}
                    onMissing={clearMissingDocument} initialMode={openMode} />
          ) : (
            <p className="p-4 text-xs text-muted-foreground">
              从左树选择文档；英文上游快照只读纪律：不翻译、不覆盖，新经验点「＋新建」写新 md。
            </p>
          )}
        </Panel>
        <Separator className="kb-mobile-divider z-10 h-full w-px shrink-0 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary" />
        {/* 右栏：大纲 */}
        <Panel className={cn("kb-workbench-panel kb-panel-rail", mobilePane !== "rail" && "kb-mobile-collapsed")} defaultSize={300} minSize="19%">
          <KbRail cap={kbCap} selected={path} selectedLayer={selectedLayer} counts={counts} pending={pending} onProposal={openProposals}
                   evolving={evolving} recommendations={recommendations} selectedSources={selectedSources}
                   onStartEvolution={startEvolution} onToggleSource={(source) => setSelectedSources((old) => old.includes(source) ? old.filter((x) => x !== source) : old.length < 6 ? [...old, source] : old)}
                   onRunEvolution={runEvolution} busy={evolutionBusy} error={evolutionError} search={evolutionSearch}
                   onSearch={(value) => { setEvolutionSearch(value); if (value.trim()) searchEvolution(value) }} hits={evolutionHits}
                   draft={draft} onDraftChange={setDraft} onSubmitDraft={submitDraft} />
        </Panel>
      </Group>
      </div>

      <KbCreateDialog open={createOpen} onOpenChange={setCreateOpen} cap={kbCap}
                      onCreated={(p) => { reload(); setPath(p); setOpenMode("edit"); setMobilePane("editor") }} />
      <KbRenameDialog open={renamePath !== null} onOpenChange={(v) => !v && setRenamePath(null)}
                      cap={kbCap} path={renamePath}
                      onRenamed={(r) => { setPath(r.new_path); reload(); window.dispatchEvent(new Event("packs-changed")) }} />
      <AlertDialog open={del !== null} onOpenChange={(v) => !v && setDel(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除知识库文档{del && del.refs.length > 0 ? "（仍被引用）" : ""} {del?.path}？</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div>
                <p>文件将移入 {kbCap}/kb/.history/kb-trash/（带时间戳，可手工恢复）。</p>
                {del && del.refs.length > 0 && (
                  <div className="mt-2 max-h-40 overflow-y-auto rounded border p-1.5">
                    <p className="text-(--status-approval)">以下 {del.refs.length} 处完整路径引用将变成悬空引用（doctor 报 error）：</p>
                    {del.refs.map((r, i) => (
                      <p key={i} className="truncate font-mono text-[10px]" title={r.forms.join(" / ")}>
                        {r.file}:{r.line}（{r.kind}）
                      </p>
                    ))}
                    <p className="mt-1">相对 md 链接无法静态扫描，删除前请自行确认。</p>
                  </div>
                )}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction disabled={busy}
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={() => doDelete(del?.refs.length === 0 ? false : true)}>
              {busy ? "删除中…" : del && del.refs.length > 0
                ? `强制删除（${del.refs.length} 处引用悬空）` : "删除"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
