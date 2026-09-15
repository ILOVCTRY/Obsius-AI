import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { DoctorIssue, DoctorReport } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { cn } from "@/lib/utils"

// 健康度摘要条 + 详情弹窗；packs 写操作后 dispatchEvent("packs-changed") 触发刷新。

const LEVEL_META: Record<DoctorIssue["level"], { label: string; cls: string; dot: string }> = {
  error: { label: "error", cls: "text-red-300", dot: "bg-red-400" },
  warning: { label: "warning", cls: "text-amber-300", dot: "bg-amber-400" },
  info: { label: "info", cls: "text-muted-foreground", dot: "bg-muted-foreground" },
}
const LEVEL_ORDER = { error: 0, warning: 1, info: 2 } as const

/** target 是否可跳到对应管理页（角色 yaml / 技能 SKILL.md / 红线 md） */
export function isNavigableTarget(target: string): boolean {
  return /^tracks\/[\w.-]+\/roles\/[\w.-]+\.yaml$/.test(target)
    || /^(tracks|capabilities)\/[\w.-]+\/skills\/[\w.-]+\/SKILL\.md$/.test(target)
    || /^(tracks|capabilities)\/[\w.-]+\/rules\/redlines\.md$/.test(target)
}

export function DoctorBar({ onNavigate }: { onNavigate?: (target: string) => void }) {
  const [report, setReport] = useState<DoctorReport | null>(null)
  const [open, setOpen] = useState(false)

  const reload = useCallback(() => {
    api.packsDoctor().then(setReport).catch(() => {})
  }, [])
  useEffect(() => {
    reload()
    const onChanged = () => reload()
    window.addEventListener("packs-changed", onChanged)
    const timer = setInterval(reload, 30_000)
    return () => {
      window.removeEventListener("packs-changed", onChanged)
      clearInterval(timer)
    }
  }, [reload])

  if (!report) return null
  const c = report.counts
  const tone = c.error ? "text-red-300" : c.warning ? "text-amber-300" : "text-emerald-300"

  return (
    <>
      <button
        className={cn("flex shrink-0 items-center gap-2 border-b px-4 py-1 text-[10px] hover:bg-accent/30", tone)}
        onClick={() => { reload(); setOpen(true) }} title="pack doctor 体检详情">
        <span>健康度</span>
        <span className={cn("size-1.5 rounded-full", c.error ? "bg-red-400" : c.warning ? "bg-amber-400" : "bg-emerald-400")} />
        <span className="font-mono">{c.error} error / {c.warning} warning / {c.info} info</span>
        {c.error === 0 && c.warning === 0 && <span className="text-muted-foreground">— 全部健康</span>}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl">
          <DialogTitle>pack doctor 体检</DialogTitle>
          <DialogDescription>
            只读静态检查：error=绑定断裂（悬空引用/未注册类型/名实不符），warning=软问题，info=编辑提示。
          </DialogDescription>
          <ScrollArea className="max-h-[60vh]">
            <div className="space-y-1 pr-3">
              {[...report.issues]
                .sort((a, b) => LEVEL_ORDER[a.level] - LEVEL_ORDER[b.level]
                  || a.code.localeCompare(b.code) || a.target.localeCompare(b.target))
                .map((i, idx) => {
                  const navigable = onNavigate && isNavigableTarget(i.target)
                  return (
                    <div key={idx} className="flex gap-2 rounded border px-2 py-1 text-[11px]">
                      <span className={cn("mt-0.5 size-1.5 shrink-0 rounded-full", LEVEL_META[i.level].dot)} />
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className={cn("font-mono text-[10px]", LEVEL_META[i.level].cls)}>{i.level}</span>
                          <span className="font-mono text-[10px] text-muted-foreground">{i.code}</span>
                          {navigable && (
                            <Button size="xs" variant="outline" className="h-5 text-[10px]"
                                    onClick={() => { onNavigate!(i.target); setOpen(false) }}>
                              跳转 →
                            </Button>
                          )}
                        </div>
                        <div className="break-all">{i.message}</div>
                        {i.target && <div className="break-all font-mono text-[10px] text-muted-foreground">{i.target}</div>}
                      </div>
                    </div>
                  )
                })}
              {report.issues.length === 0 && (
                <p className="py-6 text-center text-xs text-muted-foreground">零问题。</p>
              )}
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
  )
}
