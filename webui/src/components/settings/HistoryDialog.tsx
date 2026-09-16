import { useCallback, useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { HistoryVersion } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"
import { fmtDateTime, utcTitle } from "@/lib/datetime"
import { DiffView } from "./DiffView"

// .history 版本面板：列版本（兼容两种备份命名）→ 看高亮 diff → 一键回滚（可往返）。
// source 注入后可复用于 kb 版本端点（packs 历史与 kb-backups 两套布局）。

// 版本号是 UTC 紧凑形（20260913T145750Z），展示转本地时区（§12 统一 formatter）
const fmtTs = (ts: string) => fmtDateTime(ts)

export interface HistorySource {
  list: () => Promise<{ versions: HistoryVersion[] }>
  diff: (version: string) => Promise<{ diff: string }>
  rollback: (version: string) => Promise<unknown>
}

export function HistoryButton({ file, onRolledBack, className, source, label = "历史" }: {
  file: string
  onRolledBack?: () => void
  className?: string
  /** 缺省=packs 历史端点；kb 文件注入 kb 版本三件套 */
  source?: HistorySource
  label?: string
}) {
  // 调用方可能内联传对象：经 ref 取用，避免 reload 身份每渲染变化导致面板反复重置
  const src: HistorySource = source ?? {
    list: () => api.historyList(file),
    diff: (v) => api.historyDiff(file, v),
    rollback: (v) => api.historyRollback(file, v),
  }
  const srcRef = useRef<HistorySource>(src)
  srcRef.current = src
  const [open, setOpen] = useState(false)
  const [versions, setVersions] = useState<HistoryVersion[]>([])
  const [exists, setExists] = useState(true)
  const [sel, setSel] = useState<string | null>(null)
  const [diff, setDiff] = useState("")
  const [confirming, setConfirming] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(() => {
    srcRef.current.list().then((r) => {
      setVersions(r.versions)
      setExists((r as { exists?: boolean }).exists ?? true)
    }).catch((e) => setErr(String(e)))
  }, [file])

  useEffect(() => {
    if (open) { setErr(null); setSel(null); setDiff(""); setConfirming(null); reload() }
  }, [open, reload])

  const showDiff = (v: string) => {
    if (sel === v && diff) { setSel(null); setDiff(""); return }
    setSel(v)
    srcRef.current.diff(v).then((r) => setDiff(r.diff)).catch((e) => setErr(String(e)))
  }

  const rollback = async (v: string) => {
    setBusy(true)
    setErr(null)
    try {
      await srcRef.current.rollback(v)
      setConfirming(null)
      setSel(null)
      setDiff("")
      reload()
      onRolledBack?.()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button size="sm" variant="outline" className={cn("text-[10px]", className)}
              onClick={() => setOpen(true)}>{label}</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl">
          <DialogTitle>历史版本</DialogTitle>
          <DialogDescription className="font-mono text-[10px]">
            {file}{!exists && "（当前文件不存在，回滚可恢复）"}
          </DialogDescription>
          {err && <p className="text-xs text-(--status-error)">{err}</p>}
          {versions.length === 0
            ? <p className="py-6 text-center text-xs text-muted-foreground">暂无历史版本（每次保存/回滚自动备份）</p>
            : (
              <ScrollArea className="max-h-[60vh]">
                <div className="space-y-1 pr-3">
                  {versions.map((v) => (
                    <div key={v.version} className="rounded border p-1.5">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[11px]" title={utcTitle(v.ts)}>{fmtTs(v.ts)}</span>
                        <span className="font-mono text-[10px] text-muted-foreground">{v.size}B</span>
                        <span className="flex-1" />
                        <Button size="sm" variant="ghost" className="h-6 text-[10px]"
                                onClick={() => showDiff(v.version)}>
                          {sel === v.version ? "收起 diff" : "看 diff"}
                        </Button>
                        {confirming === v.version ? (
                          <>
                            <Button size="sm" variant="destructive" className="h-6 text-[10px]"
                                    disabled={busy} onClick={() => rollback(v.version)}>
                              确认回滚
                            </Button>
                            <Button size="sm" variant="ghost" className="h-6 text-[10px]"
                                    onClick={() => setConfirming(null)}>取消</Button>
                          </>
                        ) : (
                          <Button size="sm" variant="outline" className="h-6 text-[10px]"
                                  onClick={() => setConfirming(v.version)}>回滚</Button>
                        )}
                      </div>
                      <div className="truncate font-mono text-[10px] text-muted-foreground">{v.version}</div>
                      {sel === v.version && <div className="mt-1 max-h-56 overflow-auto"><DiffView diff={diff} /></div>}
                    </div>
                  ))}
                </div>
              </ScrollArea>
            )}
          <p className="text-[10px] text-muted-foreground">
            回滚前会自动再备份当前版（生成新版本），因此回滚可逆；两种备份命名（时间戳前缀 / .bak 后缀）都会列出。
          </p>
        </DialogContent>
      </Dialog>
    </>
  )
}
