import { useCallback, useEffect, useState } from "react"
import { api, ApiError } from "@/lib/api"
import type { KbRead, KbRefHit } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { MarkdownView } from "./MarkdownView"
import { HistoryButton, type HistorySource } from "./HistoryDialog"

// kb 文件阅读/编辑（中栏）：路径/源/大小/被引数 + 编辑/预览 + 保存（PUT 自动备份）
// + kb 版本三件套 + 改名/删除（对话框在父组件，树悬停按钮也复用）。
// 英文上游快照原文不翻译、不覆盖——新经验请新建 md。

function RefBadge({ refs }: { refs: KbRefHit[] }) {
  if (refs.length === 0) return null
  const title = refs.map((r) => `${r.file}:${r.line}（${r.forms.join("/")}）`).join("\n")
  return (
    <Badge variant="outline" className="text-[10px]" title={title}>
      被引 {refs.length} 处
    </Badge>
  )
}

export function KbPane({ cap, path, reloadKey, onDirtyChange, onSaved,
                         onRename, onDelete, onContent }: {
  cap: string
  path: string
  reloadKey: number
  onDirtyChange: (dirty: boolean) => void
  onSaved: () => void
  onRename: (path: string) => void
  onDelete: (path: string) => void
  onContent?: (content: string) => void
}) {
  const [doc, setDoc] = useState<KbRead | null>(null)
  const [content, setContent] = useState("")
  const [dirty, setDirty] = useState(false)
  const [mode, setMode] = useState<"edit" | "preview">("edit")
  const [saved, setSaved] = useState(false)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const reload = useCallback(() => {
    api.kbRead(cap, path).then((d) => {
      setDoc(d)
      setContent(d.content)
      setDirty(false)
      onDirtyChange(false)
      onContent?.(d.content)
      setLoadErr(null)
    }).catch((e) => {
      setDoc(null)
      setLoadErr(String(e))
    })
  }, [cap, path, onDirtyChange, onContent])

  useEffect(reload, [reload, reloadKey])

  const setDirtyBoth = (v: boolean) => { setDirty(v); onDirtyChange(v) }

  const save = async () => {
    setSaving(true)
    setSaveErr(null)
    try {
      await api.kbUpdate(cap, path, content)
      setSaved(true)
      setTimeout(() => setSaved(false), 1500)
      setDirtyBoth(false)
      onSaved()
    } catch (e) {
      setSaveErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  const historySource: HistorySource = {
    list: () => api.kbVersions(cap, path),
    diff: (v) => api.kbDiff(cap, path, v),
    rollback: (v) => api.kbRollback(cap, path, v),
  }

  if (loadErr) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-4 text-xs">
        <p className="text-[--status-error]">{loadErr}</p>
        <Button size="sm" variant="outline" onClick={reload}>重试</Button>
      </div>
    )
  }
  if (!doc) return <p className="p-3 text-xs text-muted-foreground">加载中…</p>

  return (
    <div className="flex h-full min-h-0 flex-col gap-1.5 p-3">
      <div className="flex shrink-0 flex-wrap items-center gap-1.5">
        <span className="max-w-[40%] truncate font-mono text-xs text-primary" title={path}>{path}</span>
        <Badge variant="outline" className="text-[10px]">{doc.source}</Badge>
        <span className="font-mono text-[10px] text-muted-foreground">{doc.size}B</span>
        <RefBadge refs={doc.refs} />
        {saveErr && <span className="max-w-[30%] truncate text-[10px] text-[--status-error]" title={saveErr}>{saveErr}</span>}
        <span className="flex-1" />
        <div className="flex rounded border text-[10px]">
          <button className={mode === "edit" ? "bg-primary/10 px-2 py-0.5 text-primary" : "px-2 py-0.5 text-muted-foreground"}
                  onClick={() => setMode("edit")}>编辑</button>
          <button className={mode === "preview" ? "bg-primary/10 px-2 py-0.5 text-primary" : "px-2 py-0.5 text-muted-foreground"}
                  onClick={() => setMode("preview")}>预览</button>
        </div>
        <HistoryButton file={path} source={historySource} onRolledBack={() => { reload(); onSaved() }} />
        <Button size="sm" variant="outline" className="text-[10px]"
                onClick={() => onRename(path)}>改名</Button>
        <Button size="sm" variant="outline" className="text-[10px] text-[--status-error]"
                onClick={() => onDelete(path)}>删除</Button>
        <span className={saved ? "text-[10px] text-primary" : "text-[10px] opacity-0"}>已保存 ✓</span>
        <Button size="sm" onClick={save} disabled={!dirty || saving}>保存</Button>
      </div>
      {mode === "edit" ? (
        <textarea
          value={content}
          onChange={(e) => { setContent(e.target.value); setDirtyBoth(true); onContent?.(e.target.value) }}
          spellCheck={false}
          className="min-h-0 flex-1 resize-none rounded border bg-background p-2 font-mono text-[12px] leading-relaxed outline-none focus:border-primary/50"
        />
      ) : (
        <ScrollArea className="min-h-0 flex-1 rounded border bg-background/40 p-2">
          <MarkdownView content={content} prefix={`kb-${cap}`} />
        </ScrollArea>
      )}
      <p className="shrink-0 text-[10px] text-muted-foreground">
        保存自动备份到 .history/kb-backups（历史可回滚）；英文上游快照原文不翻译不覆盖，新经验写新 md。
      </p>
    </div>
  )
}

// 供删除对话框引用：409 detail 结构
export function refsFromError(e: unknown): KbRefHit[] {
  if (e instanceof ApiError && e.status === 409 && e.data && typeof e.data === "object") {
    return ((e.data as { refs?: KbRefHit[] }).refs) ?? []
  }
  return []
}
