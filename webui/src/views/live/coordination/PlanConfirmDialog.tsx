import { useEffect, useState } from "react"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog"
import { api } from "@/lib/api"
import type { CoordinationPlan, CoordinationPreflight } from "@/lib/types"

export function PlanConfirmDialog({ pid, plan, open, onOpenChange, onStarted }: { pid: string; plan: CoordinationPlan | null; open: boolean; onOpenChange: (v: boolean) => void; onStarted: () => void }) {
  const [data, setData] = useState<CoordinationPreflight | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { if (!open || !plan) return; setError(null); void api.coordinationPreflight(pid, plan.id).then(setData).catch((e) => setError(e instanceof Error ? e.message : "预检失败")) }, [open, pid, plan])
  const blocked = !!data?.blockers.some((b) => b.severity === "error")
  const start = async () => {
    if (!plan || !data || blocked) return
    setBusy(true); setError(null)
    try { await api.coordinationPlanStart(pid, plan.id, { preflight_revision: data.revision, confirmations: { dependencies: true, safety: true, execution: true } }); onOpenChange(false); onStarted() }
    catch (e) { setError(e instanceof Error ? e.message : "启动失败") }
    finally { setBusy(false) }
  }
  return <AlertDialog open={open} onOpenChange={onOpenChange}><AlertDialogContent className="max-h-[85dvh] max-w-2xl overflow-y-auto">
    <AlertDialogHeader><AlertDialogTitle>计划执行前确认</AlertDialogTitle><AlertDialogDescription>确认任务、依赖和安全边界后启动；既有审批、ROE、阶段门和自主档闸门仍然有效。</AlertDialogDescription></AlertDialogHeader>
    {data ? <div className="space-y-3 text-xs"><div className="rounded border p-3"><b>{plan?.name}</b><p className="mt-1 text-muted-foreground">{plan?.objective || "未填写目标"}</p><div className="mt-2 flex flex-wrap gap-3 text-[11px] text-muted-foreground"><span>节点 {data.dependencies.task_count}</span><span>可执行 {data.dependencies.ready_count}</span><span>阻塞 {data.dependencies.blocked_count}</span><span>轨道 {data.safety.track}</span><span>自主档 {String(data.safety.autonomy.level || "-")}</span></div></div><div><b>安全边界</b><pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap rounded bg-muted/30 p-2 text-[10px]">{JSON.stringify(data.safety, null, 2)}</pre></div>{data.blockers.length > 0 && <div className="space-y-1 rounded border border-(--status-error) p-2 text-(--status-error)">{data.blockers.map((b) => <p key={b.code}>{b.message}</p>)}</div>}</div> : <p className="py-6 text-center text-muted-foreground">{error || "预检中…"}</p>}
    {error && data && <p className="text-xs text-(--status-error)">{error}</p>}
    <AlertDialogFooter><AlertDialogCancel disabled={busy}>取消</AlertDialogCancel><AlertDialogAction disabled={busy || !data || blocked} onClick={(e) => { e.preventDefault(); void start() }}>{busy ? "启动中…" : "确认并启动"}</AlertDialogAction></AlertDialogFooter>
  </AlertDialogContent></AlertDialog>
}
