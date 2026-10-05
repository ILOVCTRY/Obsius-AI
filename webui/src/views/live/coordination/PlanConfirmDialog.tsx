import { useEffect, useMemo, useState } from "react"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog"
import { api } from "@/lib/api"
import type { CoordinationPlan, CoordinationPreflight } from "@/lib/types"

export function PlanConfirmDialog({ pid, plan, open, onOpenChange, onStarted }: { pid: string; plan: CoordinationPlan | null; open: boolean; onOpenChange: (v: boolean) => void; onStarted: () => void }) {
  const [data, setData] = useState<CoordinationPreflight | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [draftTasks, setDraftTasks] = useState<Record<string, { title: string; description: string; role: string; member_id: string }>>({})
  const [draftRoe, setDraftRoe] = useState({ targets: "", window: "", exclusions: "", approver: "" })
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  const taskRows = useMemo(() => data?.plan?.tasks ?? [], [data])
  useEffect(() => { if (!open || !plan) return; setError(null); setDirty(false); void api.coordinationPreflight(pid, plan.id).then((next) => { setData(next); setDraftTasks(Object.fromEntries(next.plan.tasks.map((task) => [task.id, { title: task.title, description: task.description, role: task.role, member_id: task.member_id }]))); const roe = next.safety.roe ?? {}; setDraftRoe({ targets: String(roe.targets ?? ""), window: String(roe.window ?? ""), exclusions: String(roe.exclusions ?? ""), approver: String(roe.approver ?? "") }) }).catch((e) => setError(e instanceof Error ? e.message : "预检失败")) }, [open, pid, plan])
  const blocked = !!data?.blockers.some((b) => b.severity === "error")
  const updateTaskDraft = (id: string, patch: Partial<{ title: string; description: string; role: string; member_id: string }>) => { setDraftTasks((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } })); setDirty(true) }
  const saveEdits = async () => {
    if (!data || saving) return
    setSaving(true); setError(null)
    try {
      for (const task of taskRows) {
        const draft = draftTasks[task.id]
        if (!draft) continue
        const patch = {
          ...(draft.title !== task.title ? { title: draft.title } : {}),
          ...(draft.description !== task.description ? { description: draft.description } : {}),
          ...(draft.role !== task.role ? { role: draft.role } : {}),
          ...(draft.member_id !== task.member_id ? { member_id: draft.member_id } : {}),
        }
        if (Object.keys(patch).length) await api.coordinationTaskUpdate(pid, task.id, patch)
      }
      const roe = data.safety.roe ?? {}
      if (Object.keys(draftRoe).some((key) => draftRoe[key as keyof typeof draftRoe] !== String(roe[key] ?? ""))) {
        await api.patchProjectConfig(pid, { redteam_roe: draftRoe })
      }
      const refreshed = await api.coordinationPreflight(pid, plan!.id)
      setData(refreshed)
      setDraftTasks(Object.fromEntries(refreshed.plan.tasks.map((task) => [task.id, { title: task.title, description: task.description, role: task.role, member_id: task.member_id }])))
      const savedRoe = refreshed.safety.roe ?? {}
      setDraftRoe({ targets: String(savedRoe.targets ?? ""), window: String(savedRoe.window ?? ""), exclusions: String(savedRoe.exclusions ?? ""), approver: String(savedRoe.approver ?? "") })
      setDirty(false)
    } catch (e) { setError(e instanceof Error ? e.message : "保存修改失败") }
    finally { setSaving(false) }
  }
  const start = async () => {
    if (!plan || !data || blocked || dirty || saving) return
    setBusy(true); setError(null)
    try { await api.coordinationPlanStart(pid, plan.id, { preflight_revision: data.revision, confirmations: { dependencies: true, safety: true, execution: true } }); onOpenChange(false); onStarted() }
    catch (e) { setError(e instanceof Error ? e.message : "启动失败") }
    finally { setBusy(false) }
  }
  return <AlertDialog open={open} onOpenChange={onOpenChange}><AlertDialogContent className="max-h-[85dvh] max-w-2xl overflow-y-auto">
    <AlertDialogHeader><AlertDialogTitle>计划执行前确认</AlertDialogTitle><AlertDialogDescription>确认任务、依赖和安全边界后启动；既有审批、ROE、阶段门和自主档闸门仍然有效。</AlertDialogDescription></AlertDialogHeader>
    {data ? <div className="space-y-3 text-xs"><div className="rounded border p-3"><b>{plan?.name}</b><p className="mt-1 text-muted-foreground">{plan?.objective || "未填写目标"}</p><div className="mt-2 flex flex-wrap gap-3 text-[11px] text-muted-foreground"><span>节点 {data.dependencies.task_count}</span><span>可执行 {data.dependencies.ready_count}</span><span>阻塞 {data.dependencies.blocked_count}</span><span>轨道 {data.safety.track}</span><span>自主档 {String(data.safety.autonomy.level || "-")}</span></div></div><div className="rounded border border-primary/20 bg-primary/5 p-3"><div className="flex items-center justify-between"><b>Agent 团队 · 共享目标</b><span className="text-[10px] text-muted-foreground">只读名册</span></div><p className="mt-1 text-muted-foreground">{data.plan.objective || "未填写共享目标"}</p><div className="mt-2 grid gap-2 sm:grid-cols-2">{(data.plan.team?.members ?? []).map((member) => { const count = data.plan.tasks.filter((task) => task.member_id === member.member_id).length; return <div key={member.member_id} className="rounded border bg-background/40 px-2 py-1.5"><div className="flex justify-between gap-2"><b>{member.label || member.title || member.member_id}</b><span className="text-muted-foreground">{count} 节点</span></div><div className="text-[10px] text-muted-foreground">{member.role || "未指定角色"}{member.description ? ` · ${member.description}` : ""}</div></div> })}{!(data.plan.team?.members?.length) && <p className="text-muted-foreground">暂无团队名册，节点按角色 fallback。</p>}</div></div><div><b>团队任务分配</b><div className="mt-1 overflow-hidden rounded border"><div className="grid grid-cols-[minmax(110px,0.35fr)_minmax(180px,1fr)_minmax(70px,auto)] gap-2 border-b bg-muted/30 px-2 py-1.5 text-[10px] text-muted-foreground"><span>角色</span><span>任务</span><span>依赖</span></div>{(data.plan?.tasks ?? []).map((task) => <div key={task.id} className="grid grid-cols-[minmax(110px,0.35fr)_minmax(180px,1fr)_minmax(70px,auto)] gap-2 border-b px-2 py-2 last:border-b-0">{data.plan.team?.source !== "legacy_derived" && data.plan.team?.members?.length ? <select value={draftTasks[task.id]?.member_id ?? task.member_id} onChange={(e) => { const member = data.plan.team.members.find((item) => item.member_id === e.target.value); updateTaskDraft(task.id, { member_id: e.target.value, role: member?.role ?? task.role }) }} className="h-7 rounded border bg-background px-1.5 text-primary"><option value="">未分配</option>{data.plan.team.members.map((member) => <option key={member.member_id} value={member.member_id}>{member.label || member.title || member.member_id}</option>)}</select> : <input value={draftTasks[task.id]?.role ?? task.role} onChange={(e) => updateTaskDraft(task.id, { role: e.target.value, member_id: e.target.value })} className="h-7 rounded border bg-background px-1.5 font-mono text-primary" placeholder="角色" />}<input value={draftTasks[task.id]?.title ?? task.title} onChange={(e) => updateTaskDraft(task.id, { title: e.target.value })} className="h-7 min-w-0 rounded border bg-background px-1.5 font-medium" title={task.title} /><span className="text-muted-foreground">{task.depends_on?.length ? `${task.depends_on.length} 项` : "—"}</span></div>)}</div></div><div><b>安全边界</b><div className="mt-1 grid gap-2 rounded border bg-muted/20 p-2 sm:grid-cols-2">{([['targets','授权目标'],['window','时间窗口'],['exclusions','禁止事项'],['approver','授权人']] as const).map(([key, label]) => <label key={key} className="space-y-1"><span className="block text-[10px] text-muted-foreground">{label}</span><input value={draftRoe[key]} onChange={(e) => { setDraftRoe((prev) => ({ ...prev, [key]: e.target.value })); setDirty(true) }} className="h-8 w-full rounded border bg-background px-2" placeholder="未设置" /></label>)}</div><p className="mt-1 text-[10px] text-muted-foreground">自主档、暂停状态、并发上限和预算为只读约束。</p></div>{data.blockers.length > 0 && <div className="space-y-1 rounded border border-(--status-error) p-2 text-(--status-error)">{data.blockers.map((b) => <p key={b.code}>{b.message}</p>)}</div>}</div> : <p className="py-6 text-center text-muted-foreground">{error || "预检中…"}</p>}
    {error && data && <p className="text-xs text-(--status-error)">{error}</p>}
    <AlertDialogFooter className="mt-4 flex-col gap-2 border-t pt-3 sm:flex-row sm:items-center sm:justify-between"><div className="flex items-center gap-2 text-[11px] text-muted-foreground">{dirty && <span className="text-(--status-approval)">有未保存修改</span>}<button type="button" disabled={!dirty || saving} onClick={() => { if (data) { setDraftTasks(Object.fromEntries(data.plan.tasks.map((task) => [task.id, { title: task.title, description: task.description, role: task.role, member_id: task.member_id }]))); const roe = data.safety.roe ?? {}; setDraftRoe({ targets: String(roe.targets ?? ""), window: String(roe.window ?? ""), exclusions: String(roe.exclusions ?? ""), approver: String(roe.approver ?? "") }); setDirty(false) } }} className="rounded px-2.5 py-1.5 hover:bg-accent disabled:opacity-40">重置</button>{dirty && <button type="button" disabled={saving} onClick={() => void saveEdits()} className="rounded border border-primary/50 px-3 py-1.5 text-primary hover:bg-primary/10 disabled:opacity-50">{saving ? "保存中…" : "保存修改"}</button>}</div><div className="flex justify-end gap-2"><AlertDialogCancel disabled={busy || saving} className="rounded-lg border-muted-foreground/40 px-4">取消</AlertDialogCancel><AlertDialogAction disabled={busy || saving || !data || blocked || dirty} onClick={(e) => { e.preventDefault(); void start() }} className="rounded-lg bg-primary px-5 text-primary-foreground shadow-[0_0_18px_rgba(255,120,70,0.25)] hover:bg-primary/90 disabled:opacity-50">{busy ? "启动中…" : "确认并启动"}</AlertDialogAction></div></AlertDialogFooter>
  </AlertDialogContent></AlertDialog>
}
