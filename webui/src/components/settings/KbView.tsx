import { useCallback, useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { KbRefHit, KbSourceTree } from "@/lib/types"
import type { PackInfo } from "@/lib/taxonomy"
import { Group, Panel, Separator } from "react-resizable-panels"
import { BookOpen, FileCode2, FileText, Layers3, RefreshCw } from "lucide-react"
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

// 知识库独立管理页（K7 2026-09-29：从技能页拆出）——左树 / 中编辑器两栏。
// 只管 packs/kb/<cap>/ 的文档（打法手册 + 经验沉淀），与技能库彻底分离。
// 四层分类（原始资料/实战案例/技术模式/方法论）与右栏「知识演化」面板已于
// 2026-10-13 退役（区分按路径推断、实战案例/技术模式恒空、无人使用）；
// 目录结构保持现状，playbooks/refs 等仍作普通文件夹。

export interface KbFocus { path: string; n: number }

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
  const [mobilePane, setMobilePane] = useState<"tree" | "editor">("tree")
  const [openMode, setOpenMode] = useState<"edit" | "preview">("preview")

  const reload = useCallback(() => {
    api.kbList(kbCap).then((r) => setSources(r.sources)).catch(() => setSources([]))
  }, [kbCap])
  useEffect(reload, [reload])

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
    setMobilePane("tree")
    setOpenMode("preview")
  }, [cap, kbCap, dirty, path, onCapChange])

  const clearMissingDocument = useCallback(() => {
    setPath(null)
    setDirty(false)
    reload()
  }, [reload])

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

  const kbTotal = useMemo(
    () => sources.reduce((n, source) => n + source.files.length, 0), [sources])

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
              <select className="rounded border bg-background px-2 py-1 text-xs" value={cap} onChange={(e) => onCapChange?.(e.target.value)}>
                {capabilities.map((item) => <option key={item.name} value={item.name}>{item.label || item.name}</option>)}
              </select>
            </label>}
            <Button size="icon-sm" variant="outline" title="刷新知识库" aria-label="刷新知识库" onClick={reload}><RefreshCw size={14} /></Button>
            <Button size="sm" variant="outline" className="gap-1 text-[10px]" onClick={() => setCreateOpen(true)}><FileText size={13} />新建文档</Button>
          </div>
        </div>
        <div className="kb-mobile-tabs" role="tablist" aria-label="知识库工作区">
          <button className={cn(mobilePane === "tree" && "is-active")} onClick={() => setMobilePane("tree")}><Layers3 size={12} />文档树</button>
          <button className={cn(mobilePane === "editor" && "is-active")} onClick={() => setMobilePane("editor")}><FileCode2 size={12} />编辑器</button>
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
              <KbTree cap={kbCap} sources={sources} selected={path}
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
