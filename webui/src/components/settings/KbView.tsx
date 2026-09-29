import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { KbRefHit, KbSourceTree } from "@/lib/types"
import { Group, Panel, Separator } from "react-resizable-panels"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { KbPane, refsFromError } from "./KbPane"
import { KbTree } from "./KbTree"
import { MarkdownOutline } from "./MarkdownOutline"
import { KbCreateDialog, KbRenameDialog } from "./CreateDialogs"

// 知识库独立管理页（K7 2026-09-29：从技能页拆出）——左树 / 中编辑器 / 右大纲，
// 与技能页三栏同构。只管 packs/kb/<cap>/ 的文档（打法手册 + 经验沉淀），
// 与技能库（packs/*/skills/）彻底分离。

export interface KbFocus { path: string; n: number }

export function KbView({ cap, focus }: { cap: string; focus?: KbFocus | null }) {
  const [kbCap, setKbCap] = useState(cap)
  const [sources, setSources] = useState<KbSourceTree[]>([])
  const [path, setPath] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const [content, setContent] = useState("")
  const [createOpen, setCreateOpen] = useState(false)
  const [renamePath, setRenamePath] = useState<string | null>(null)
  const [del, setDel] = useState<{ path: string; refs: KbRefHit[] } | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(() => {
    api.kbList(kbCap).then((r) => setSources(r.sources)).catch(() => setSources([]))
  }, [kbCap])
  useEffect(reload, [reload])

  // 顶部能力包切换：未保存修改拦截
  useEffect(() => {
    if (cap === kbCap) return
    if (dirty && !window.confirm(
      `知识库文档 ${path} 有未保存修改，切到能力包 ${cap} 将放弃修改，继续？`)) return
    setKbCap(cap)
    setPath(null)
    setDirty(false)
  }, [cap, kbCap, dirty, path])

  // doctor/深链跳转：选中指定文件
  useEffect(() => {
    if (focus) setPath(focus.path)
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

  const kbTotal = sources.reduce((n, s) => n + s.files.length, 0)

  return (
    <div className="h-full min-h-0">
      <Group orientation="horizontal" className="h-full">
        {/* 左栏：知识库树 */}
        <Panel defaultSize={260} minSize="14%">
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex shrink-0 items-center gap-1 border-b px-1.5 py-1">
              <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground"
                    title={kbCap}>📚 {kbCap} 知识库（{kbTotal}）</span>
              <button className="shrink-0 rounded px-1.5 text-[11px] text-primary hover:bg-accent/40"
                      title="新建知识库 md" onClick={() => setCreateOpen(true)}>＋新建</button>
            </div>
            <div className="min-h-0 flex-1">
              <KbTree cap={kbCap} sources={sources} selected={path}
                      onSelect={setPath} onRename={setRenamePath}
                      onDelete={(p) => setDel({ path: p, refs: [] })} />
            </div>
          </div>
        </Panel>
        <Separator className="z-10 h-full w-px shrink-0 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary" />
        {/* 中栏：编辑器 */}
        <Panel minSize="30%">
          {path ? (
            <KbPane key={`${kbCap}/${path}`} cap={kbCap} path={path} reloadKey={0}
                    onDirtyChange={setDirty} onContent={setContent}
                    onSaved={() => { window.dispatchEvent(new Event("packs-changed")); reload() }}
                    onRename={setRenamePath} onDelete={(p) => setDel({ path: p, refs: [] })}
                    onOpenKb={setPath} />
          ) : (
            <p className="p-4 text-xs text-muted-foreground">
              从左树选择文档；英文上游快照只读纪律：不翻译、不覆盖，新经验点「＋新建」写新 md。
            </p>
          )}
        </Panel>
        <Separator className="z-10 h-full w-px shrink-0 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary" />
        {/* 右栏：大纲 */}
        <Panel defaultSize={280} minSize="17%">
          <div className="flex h-full min-h-0 flex-col">
            <p className="shrink-0 border-b px-2 py-1 text-[10px] font-semibold text-muted-foreground">
              文档大纲（h1–h3，点击滚动）
            </p>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <MarkdownOutline markdown={content} prefix={`kb-${kbCap}`} />
            </div>
          </div>
        </Panel>
      </Group>

      <KbCreateDialog open={createOpen} onOpenChange={setCreateOpen} cap={kbCap}
                      onCreated={(p) => { reload(); setPath(p) }} />
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
