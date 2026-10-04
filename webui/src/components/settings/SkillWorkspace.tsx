import { useEffect, useMemo, useState } from "react"
import { ChevronDown, File, FilePlus2, Folder, FolderPlus, Image as ImageIcon, Save, Search, Trash2, UserRound } from "lucide-react"
import { api } from "@/lib/api"
import type { SkillCatalog, SkillDef, SkillDetail, SkillFileEntry } from "@/lib/types"
import { capLabel } from "@/lib/taxonomy"
import { parseSkill } from "@/lib/skillfm"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { MarkdownView } from "./MarkdownView"
import { cn } from "@/lib/utils"

type SkillFocus = { source: "cap" | "track"; pack: string; name: string; n: number } | null

const isMarkdown = (path: string) => /\.(md|markdown)$/i.test(path)

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

export function SkillWorkspace({ pid, focus, onExpert }: { pid?: string | null; focus?: SkillFocus; onExpert?: (id: string) => void }) {
  const [catalog, setCatalog] = useState<SkillCatalog | null>(null)
  const [selected, setSelected] = useState<SkillDef | null>(null)
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [files, setFiles] = useState<SkillFileEntry[]>([])
  const [file, setFile] = useState<SkillFileEntry | null>(null)
  const [content, setContent] = useState("")
  const [fileMode, setFileMode] = useState<"edit" | "preview">("preview")
  const [dirty, setDirty] = useState(false)
  const [query, setQuery] = useState("")
  const [capFilter, setCapFilter] = useState("all")
  const [view, setView] = useState<"overview" | "files">("overview")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = () => api.skillCatalog(pid).then(setCatalog).catch((e) => setError(String(e)))
  useEffect(() => { load() }, [pid])
  useEffect(() => {
    if (!focus || focus.source !== "cap" || !catalog) return
    const found = catalog.capabilities.find((c) => c.name === focus.pack)?.skills.find((s) => s.name === focus.name)
    if (found) setSelected(found)
  }, [catalog, focus?.n])
  useEffect(() => {
    setDetail(null); setFiles([]); setFile(null); setFileMode("preview"); setDirty(false); setView("overview")
    if (!selected) return
    Promise.all([api.capSkillDetail(selected.pack, selected.name), api.capSkillFiles(selected.pack, selected.name)])
      .then(([d, f]) => { setDetail(d); setFiles(f.files) }).catch((e) => setError(String(e)))
  }, [selected?.pack, selected?.name])

  const packages = useMemo(() => (catalog?.capabilities ?? []).map((group) => ({
    ...group,
    skills: group.skills.filter((skill) => {
      if (capFilter !== "all" && group.name !== capFilter) return false
      const hay = `${skill.name} ${skill.description} ${group.name} ${group.label}`.toLowerCase()
      return hay.includes(query.trim().toLowerCase())
    }),
  })).filter((group) => group.skills.length || (!query.trim() && (capFilter === "all" || group.name === capFilter))), [catalog, capFilter, query])

  const chooseFile = async (entry: SkillFileEntry) => {
    if (dirty && !window.confirm("当前文件有未保存修改，放弃并打开其他文件？")) return
    setFile(entry); setDirty(false); setError(null)
    setFileMode(isMarkdown(entry.path) ? "preview" : "edit")
    if (entry.is_dir) { setContent(""); return }
    try {
      const next = await api.capSkillFile(selected!.pack, selected!.name, entry.path)
      setFile(next); setContent(next.content ?? "")
    } catch (e) { setError(String(e)) }
  }

  const saveFile = async () => {
    if (!selected || !file || file.kind !== "text") return
    setBusy(true); setError(null)
    try {
      if (file.path === "SKILL.md") await api.updateCapSkill(selected.pack, selected.name, content)
      else await api.updateCapSkillFile(selected.pack, selected.name, { path: file.path, content })
      setDirty(false); await load();
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  const createFile = async (isDir: boolean) => {
    if (!selected) return
    const path = window.prompt(isDir ? "新目录路径" : "新文件路径", isDir ? "references/new-folder" : "references/new-file.md")
    if (!path) return
    try { await api.createCapSkillFile(selected.pack, selected.name, { path, is_dir: isDir, content: "" }); setFiles((await api.capSkillFiles(selected.pack, selected.name)).files) }
    catch (e) { setError(String(e)) }
  }

  const deleteFile = async () => {
    if (!selected || !file || file.path === "SKILL.md" || !window.confirm(`删除 ${file.path}？`)) return
    try { await api.deleteCapSkillFile(selected.pack, selected.name, file.path); setFile(null); setContent(""); setFiles((await api.capSkillFiles(selected.pack, selected.name)).files) }
    catch (e) { setError(String(e)) }
  }

  const deleteSkill = async () => {
    if (!selected || !window.confirm(`删除技能 ${selected.name}？整个目录将移入历史回收站。`)) return
    try { await api.deleteCapSkill(selected.pack, selected.name); setSelected(null); await load() }
    catch (e) { setError(String(e)) }
  }

  // 概览底部正文：剥掉 frontmatter 只渲染 markdown 正文（frontmatter 字段已由头部与上方卡片呈现）
  const bodyMd = useMemo(() => {
    if (!detail) return ""
    const parsed = parseSkill(detail.raw)
    return parsed.hasFm ? parsed.form.body : detail.raw
  }, [detail])

  return <div className="flex h-full min-h-0 flex-col">
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-4 py-3">
      <div><h2 className="text-sm font-semibold">技能库</h2><p className="text-[10px] text-muted-foreground">能力包技能与文件资产</p></div>
      <div className="ml-auto flex items-center gap-2">
        <select aria-label="能力包过滤" className="rounded border bg-background px-2 py-1 text-xs" value={capFilter} onChange={(e) => setCapFilter(e.target.value)}>
          <option value="all">全部能力包</option>{(catalog?.capabilities ?? []).map((c) => <option key={c.name} value={c.name}>{c.label || capLabel(c.name)}</option>)}
        </select>
        <label className="flex items-center gap-1 rounded border px-2 py-1 text-xs text-muted-foreground"><Search size={13} /><input className="w-36 bg-transparent outline-none" placeholder="搜索技能" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
      </div>
    </div>
    <div className="grid min-h-0 flex-1 grid-cols-[280px_minmax(0,1fr)]">
      <aside className="min-h-0 overflow-y-auto border-r p-2">
        <div className="mb-2 grid grid-cols-3 gap-1 text-center text-[10px] text-muted-foreground"><span className="rounded bg-accent/30 p-1"><b className="block text-sm text-foreground">{catalog?.skill_count ?? 0}</b>技能</span><span className="rounded bg-accent/30 p-1"><b className="block text-sm text-foreground">{catalog?.capability_count ?? 0}</b>能力包</span><span className="rounded bg-accent/30 p-1"><b className="block text-sm text-foreground">{catalog?.estimated_tokens ?? 0}</b>估算 tokens</span></div>
        {packages.map((group) => <section key={group.name} className="mb-2"><div className="flex items-center gap-1 px-1 py-1 text-[11px] font-semibold"><ChevronDown size={13} /><span>{group.label || capLabel(group.name)}</span><span className="ml-auto text-muted-foreground">{group.skills.length}</span></div>{group.skills.map((skill) => <button key={`${skill.pack}/${skill.name}`} className={cn("mb-0.5 flex w-full items-start gap-2 rounded px-2 py-2 text-left text-xs hover:bg-accent/40", selected?.name === skill.name && selected.pack === skill.pack && "bg-primary/10 text-primary")} onClick={() => setSelected(skill)}><File size={13} className="mt-0.5 shrink-0" /><span className="min-w-0"><span className="block truncate font-mono">{skill.name}</span><span className="block truncate text-[10px] text-muted-foreground">{skill.description || "无描述"}</span></span></button>)}</section>)}
      </aside>
      <main className="min-h-0 overflow-hidden">
        {!selected || !detail ? <div className="flex h-full items-center justify-center text-xs text-muted-foreground">从左侧选择一个技能</div> : <div className="flex h-full min-h-0 flex-col">
          <header className="flex shrink-0 items-center gap-2 border-b px-5 py-3"><div><div className="font-mono text-sm">{selected.name}</div><div className="text-[10px] text-muted-foreground">{selected.description || "无描述"} · {capLabel(selected.pack)}</div></div><div className="ml-auto flex gap-1"><Button size="sm" variant="ghost" className="text-xs text-destructive" onClick={deleteSkill}><Trash2 size={13} className="mr-1" />删除技能</Button><Button size="sm" variant={view === "overview" ? "secondary" : "ghost"} className="text-xs" onClick={() => setView("overview")}>概览</Button><Button size="sm" variant={view === "files" ? "secondary" : "ghost"} className="text-xs" onClick={() => setView("files")}>文件</Button></div></header>
          {view === "overview" ? <div className="min-h-0 flex-1 overflow-y-auto p-5"><div className="grid max-w-3xl grid-cols-3 gap-3"><div className="rounded border p-3"><div className="text-[10px] text-muted-foreground">技能文件</div><b className="text-lg">{files.length}</b></div><div className="rounded border p-3"><div className="text-[10px] text-muted-foreground">估算 tokens</div><b className="text-lg">{selected.estimated_tokens ?? 0}</b></div><div className="rounded border p-3"><div className="text-[10px] text-muted-foreground">引用专家</div><b className="text-lg">{selected.expert_refs?.length ?? 0}</b></div></div><div className="mt-6 max-w-3xl rounded border p-4"><h3 className="mb-3 text-xs font-semibold">专家白名单引用</h3>{selected.expert_refs?.length ? <div className="flex flex-wrap gap-2">{selected.expert_refs.map((id) => <button key={id} className="inline-flex items-center gap-1 rounded border px-2 py-1 text-xs hover:bg-accent/40" onClick={() => onExpert?.(id)}><UserRound size={12} />{id}</button>)}</div> : <p className="text-xs text-muted-foreground">暂无显式白名单引用</p>}</div><div className="mt-6 max-w-3xl rounded border bg-background/40 p-4"><MarkdownView content={bodyMd} prefix={`skill-ov-${selected.name}`} /></div></div> : <div className="grid min-h-0 flex-1 grid-cols-[240px_minmax(0,1fr)_220px]"><aside className="min-h-0 overflow-y-auto border-r p-2"><div className="mb-2 flex items-center gap-1 text-[11px] font-semibold">文件树 <span className="ml-auto flex gap-1"><button title="新建文件" onClick={() => createFile(false)}><FilePlus2 size={14} /></button><button title="新建目录" onClick={() => createFile(true)}><FolderPlus size={14} /></button></span></div>{files.map((entry) => <button key={entry.path} className={cn("flex w-full items-center gap-1 rounded px-2 py-1.5 text-left text-[11px] hover:bg-accent/40", file?.path === entry.path && "bg-primary/10 text-primary", entry.is_dir && "font-semibold")} style={{ paddingLeft: `${8 + entry.path.split("/").length * 8}px` }} onClick={() => chooseFile(entry)}>{entry.is_dir ? <Folder size={13} /> : entry.kind === "image" ? <ImageIcon size={13} /> : <File size={13} />}<span className="truncate">{entry.name}</span></button>)}</aside><section className="flex min-h-0 flex-col">
            {file ? <>
              {file.kind === "text" ? (
                isMarkdown(file.path) && fileMode === "preview"
                  ? <ScrollArea className="min-h-0 flex-1 bg-background/40 p-4"><MarkdownView content={content} prefix={`skill-${selected?.name ?? "file"}`} /></ScrollArea>
                  : <textarea className="min-h-0 flex-1 resize-none bg-transparent p-4 font-mono text-xs leading-relaxed outline-none" value={content} onChange={(e) => { setContent(e.target.value); setDirty(true) }} />
              ) : file.kind === "image" && file.preview
                ? <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto p-4"><img src={file.preview} alt={file.path} className="max-h-full max-w-full object-contain" /></div>
                : <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">二进制文件仅显示元数据</div>}
              <div className="flex shrink-0 items-center justify-end gap-2 border-t px-3 py-2">
                <span className="mr-auto text-[10px] text-muted-foreground">{file.path} {dirty ? "· 未保存" : ""}</span>
                {file.kind === "text" && isMarkdown(file.path) && (
                  <div className="flex rounded border text-[10px]">
                    <button className={cn("px-2 py-0.5", fileMode === "edit" ? "bg-primary/10 text-primary" : "text-muted-foreground")} onClick={() => setFileMode("edit")}>编辑</button>
                    <button className={cn("px-2 py-0.5", fileMode === "preview" ? "bg-primary/10 text-primary" : "text-muted-foreground")} onClick={() => setFileMode("preview")}>预览</button>
                  </div>
                )}
                {file.path !== "SKILL.md" && <Button size="sm" variant="ghost" className="text-destructive" onClick={deleteFile}><Trash2 size={13} /></Button>}
                <Button size="sm" disabled={!dirty || file.kind !== "text" || busy} onClick={saveFile}><Save size={13} className="mr-1" />保存</Button>
              </div>
            </> : <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">选择文件查看或编辑</div>}
          </section><aside className="border-l p-4 text-xs">{file ? <><div className="mb-3 font-semibold">文件信息</div><dl className="space-y-2 text-muted-foreground"><div><dt>类型</dt><dd className="text-foreground">{file.kind}</dd></div><div><dt>大小</dt><dd className="text-foreground">{formatBytes(file.size)}</dd></div><div><dt>路径</dt><dd className="break-all font-mono text-foreground">{file.path}</dd></div></dl></> : <span className="text-muted-foreground">选择文件后显示元数据</span>}</aside></div>}
        </div>}
      </main>
    </div>
    {error && <div className="shrink-0 border-t px-4 py-2 text-xs text-destructive">{error}</div>}
  </div>
}
