import { useCallback, useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { KbRefHit, KbSourceTree } from "@/lib/types"
import { capOptionLabel, KB_ALL, type PackInfo } from "@/lib/taxonomy"
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
//
// 2026-10-23（Nan）：「全部方向」聚合视图。选择器的 cap 可以是哨兵
// `taxonomy.KB_ALL`——后端 `list_kb` 把全部方向合并成**单棵源树**，且每篇文档
// 的 path 带方向前缀（全局 module 形态 `<域>/<快照>/<路径>`）。本组件因此要维护
// 两个坐标：**树坐标**（`path`，聚合视图带前缀）与**文档坐标**
// （`splitTarget()` 拆出的 `(方向, 源内路径)`，编辑器/改名/删除/新建用它）。
// 不拆开的话，聚合视图下编辑器会把 `binary/逆向工程/x.md` 当成
// `packs/kb/__all__/binary/…` 去找——静默 404 后清空选中项。

export interface KbFocus { path: string; n: number }

/** 树坐标 → 文档坐标（方向 + 源内相对路径）。 */
function splitTarget(cap: string, treePath: string): { cap: string; path: string } {
  if (cap !== KB_ALL) return { cap, path: treePath }
  const i = treePath.indexOf("/")
  return i > 0 ? { cap: treePath.slice(0, i), path: treePath.slice(i + 1) }
               : { cap: "", path: treePath }
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
  const [del, setDel] = useState<{ cap: string; path: string; refs: KbRefHit[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const [mobilePane, setMobilePane] = useState<"tree" | "editor">("tree")
  const [openMode, setOpenMode] = useState<"edit" | "preview">("preview")

  const allMode = kbCap === KB_ALL
  const doc = path ? splitTarget(kbCap, path) : null
  // 新建文档只能落到**具体方向**目录：聚合视图下把方向交给用户在对话框里选。
  const dirs = useMemo(
    () => (capabilities ?? []).map((c) => c.name).filter((n) => n !== KB_ALL),
    [capabilities])
  const browseLabel = capOptionLabel(kbCap)

  const reload = useCallback(() => {
    api.kbList(kbCap).then((r) => setSources(r.sources)).catch(() => setSources([]))
  }, [kbCap])
  useEffect(reload, [reload])

  // 顶部知识方向切换：未保存修改拦截
  useEffect(() => {
    if (cap === kbCap) return
    if (dirty && !window.confirm(
      `知识库文档 ${path} 有未保存修改，切到 ${capOptionLabel(cap)} 将放弃修改，继续？`)) {
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

  // doctor/深链跳转：选中指定文件。深链来自设置页——那里的 cap 恒为具体方向、
  // 路径为源内相对形态，与单方向视图的树坐标同形，故直接赋值即可
  // （聚合视图不接深链：本项目只有设置页发深链，而设置页的 KB 页签没有选择器）。
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
      await api.kbDelete(del.cap, del.path, force)
      if (doc && doc.cap === del.cap && doc.path === del.path) { setPath(null); setDirty(false) }
      setDel(null)
      window.dispatchEvent(new Event("packs-changed"))
      reload()
    } catch (e) {
      const refs = refsFromError(e)
      if (refs.length > 0) setDel({ ...del, refs })
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
            <div className="flex min-w-0 items-center gap-2"><BookOpen size={17} className="shrink-0 text-primary" /><span className="truncate text-sm font-semibold">知识库工作台</span><Badge variant="outline" className="shrink-0 font-mono text-[9px]">{browseLabel}</Badge><span className="shrink-0 text-[10px] text-muted-foreground">{kbTotal} 份资料</span></div>
          </div>
          <div className="flex items-center gap-1.5">
            {capabilities && capabilities.length > 0 && <label className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
              知识方向
              <select className="rounded border bg-background px-2 py-1 text-xs" value={cap} onChange={(e) => onCapChange?.(e.target.value)}>
                {capabilities.map((item) => <option key={item.name} value={item.name}>{item.label || capOptionLabel(item.name)}</option>)}
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
                      title={browseLabel}>📚 {browseLabel} 知识库（{kbTotal}）</span>
              <button className="shrink-0 rounded px-1.5 text-[11px] text-primary hover:bg-accent/40"
                      title="新建知识库 md" onClick={() => setCreateOpen(true)}>＋新建</button>
            </div>
            <div className="min-h-0 flex-1">
              <KbTree cap={kbCap} sources={sources} selected={path}
                      onSelect={(nextPath) => { setPath(nextPath); setOpenMode("preview") }} onRename={setRenamePath}
                      onDelete={(p) => setDel({ ...splitTarget(kbCap, p), refs: [] })} />
            </div>
          </div>
        </Panel>
        <Separator className="kb-mobile-divider z-10 h-full w-px shrink-0 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary" />
        {/* 中栏：编辑器 */}
        <Panel className={cn("kb-workbench-panel kb-panel-editor", mobilePane !== "editor" && "kb-mobile-collapsed")} minSize="30%">
          {doc && doc.cap ? (
            <KbPane key={`${doc.cap}/${doc.path}`} cap={doc.cap} path={doc.path} reloadKey={0}
                    onDirtyChange={setDirty}
                    onSaved={() => { window.dispatchEvent(new Event("packs-changed")); reload() }}
                    onRename={() => setRenamePath(path)} onDelete={(p) => setDel({ cap: doc.cap, path: p, refs: [] })}
                    onOpenKb={(nextPath) => {
                      setPath(allMode ? `${doc.cap}/${nextPath}` : nextPath)
                      setOpenMode("preview")
                    }}
                    onMissing={clearMissingDocument} initialMode={openMode} />
          ) : (
            <p className="p-4 text-xs text-muted-foreground">
              从左树选择文档；英文上游快照只读纪律：不翻译、不覆盖，新经验点「＋新建」写新 md。
            </p>
          )}
        </Panel>
      </Group>
      </div>

      <KbCreateDialog open={createOpen} onOpenChange={setCreateOpen}
                      cap={doc?.cap || dirs[0] || kbCap}
                      capOptions={allMode ? dirs : undefined}
                      onCreated={(createdCap, p) => {
                        reload()
                        setPath(createdCap === kbCap ? p : `${createdCap}/${p}`)
                        setOpenMode("edit")
                        setMobilePane("editor")
                      }} />
      <KbRenameDialog open={renamePath !== null} onOpenChange={(v) => !v && setRenamePath(null)}
                      cap={renamePath ? splitTarget(kbCap, renamePath).cap : kbCap}
                      path={renamePath ? splitTarget(kbCap, renamePath).path : null}
                      onRenamed={(r) => {
                        const owner = splitTarget(kbCap, renamePath ?? "").cap
                        setPath(owner === kbCap ? r.new_path : `${owner}/${r.new_path}`)
                        reload(); window.dispatchEvent(new Event("packs-changed"))
                      }} />
      <AlertDialog open={del !== null} onOpenChange={(v) => !v && setDel(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除知识库文档{del && del.refs.length > 0 ? "（仍被引用）" : ""} {del?.path}？</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div>
                <p>文件将移入 {del?.cap ?? kbCap}/kb/.history/kb-trash/（带时间戳，可手工恢复）。</p>
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
