import { useCallback, useEffect, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { Task } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { cn } from "@/lib/utils"

// 任务看板（DESIGN.md §12 页面 5 / §6.4 人类插手通道）：
// 四态 open/claimed/done/failed——open/failed 可编辑；failed 可放回待认领；
// 四态皆可物理删除（A1：留 task.deleted 审计，claimed 当前步结束即硬中断，有子任务 409）。
// claimed 卡显示认领者计划进度（A2：task.plan，done/total + blocked 原因）。

const COLUMNS: { status: Task["status"]; label: string }[] = [
  { status: "open", label: "待认领" },
  { status: "claimed", label: "执行中" },
  { status: "done", label: "完成" },
  { status: "failed", label: "失败" },
]

const NOISE_LEVELS = ["passive", "low", "medium", "high"]

// A3：任务流双击 open 节点经 window goto-tasks 跳来，focused 非空=高亮并滚动到该卡
export function TaskBoard({ pid, focused }: {
  pid: string
  focused?: { id: string; n: number } | null
}) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [objective, setObjective] = useState("")
  const [taskType, setTaskType] = useState("generic")
  const [noise, setNoise] = useState("passive")
  const [conflictKeys, setConflictKeys] = useState("")
  const [priority, setPriority] = useState(2)
  const [error, setError] = useState<string | null>(null)
  // 轨任务类型注册表（task_types.yaml + generic）：输入框 datalist 提示，非法值后端 422
  const [typeOptions, setTypeOptions] = useState<Record<string, string>>({ generic: "passive" })
  // B1 发布去重：命中同指纹 open/claimed 任务 → 确认"仍要发布"后 force 重发
  const [dupPending, setDupPending] = useState<{
    body: Parameters<typeof api.publishTask>[1]; existed: string; taskId: string
  } | null>(null)
  // C1：「已解决，放回继续」附注（写进任务行 result_note 落审计）
  const [resolveTarget, setResolveTarget] = useState<Task | null>(null)
  const [resolveNote, setResolveNote] = useState("")

  useEffect(() => {
    Promise.all([api.getProject(pid), api.taxonomy()])
      .then(([proj, tax]) => setTypeOptions({ generic: "passive", ...(tax.task_types[proj.track] ?? {}) }))
      .catch(() => {})
  }, [pid])

  // 删除确认（claimed 任务有警示文案；删除失败 409/422 时对话框保持打开）
  const [deleteTarget, setDeleteTarget] = useState<Task | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  const refresh = useCallback(() =>
    api.tasks(pid).then(setTasks).catch((e) => setError(String(e))), [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 3000)
    return () => clearInterval(t)
  }, [refresh])

  const publish = async (force = false) => {
    setError(null)
    const keys = conflictKeys.split(",").map((s) => s.trim()).filter(Boolean)
    const body = {
      objective: objective.trim(),
      task_type: taskType.trim(),
      noise_budget: noise,
      priority,
      conflict_keys: noise === "passive" ? undefined : keys,
      force: force || undefined,
    }
    try {
      const r = await api.publishTask(pid, body)
      if (r.deduplicated) {
        setDupPending({ body, existed: r.existed_status ?? "open", taskId: r.task_id })
        return
      }
      setObjective("")
      setConflictKeys("")
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const remove = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await api.deleteTask(deleteTarget.id)
      setDeleteTarget(null)
      refresh()
    } catch (e) {
      // 有子任务等 409：对话框保持打开，原因显示在描述下方
      setDeleteError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="flex h-full flex-col">
      {/* 手动发布 */}
      <div className="flex flex-wrap items-center gap-2 border-b p-3">
        <Input className="w-24" value={taskType} onChange={(e) => setTaskType(e.target.value)}
               placeholder="类型" list="task-type-options" />
        <datalist id="task-type-options">
          {Object.entries(typeOptions).map(([t, n]) =>
            <option key={t} value={t}>默认 {n}</option>)}
        </datalist>
        <select
          value={noise}
          onChange={(e) => setNoise(e.target.value)}
          className="h-9 rounded-md border bg-card px-2 text-sm"
        >
          {NOISE_LEVELS.map((n) => (
            <option key={n} value={n}>{n}{n === "passive" ? "" : "（active）"}</option>
          ))}
        </select>
        <Input
          type="number" min={0} max={9} className="w-16 text-center font-mono"
          value={priority} onChange={(e) => setPriority(Number(e.target.value))}
          title="优先级 0-9，大者优先"
        />
        {noise !== "passive" && (
          <Input className="w-56 font-mono" value={conflictKeys} onChange={(e) => setConflictKeys(e.target.value)}
                 placeholder="conflict_keys（逗号分隔，必填）" />
        )}
        <Input className="min-w-64 flex-1" value={objective} onChange={(e) => setObjective(e.target.value)}
               placeholder="任务目标…" onKeyDown={(e) => e.key === "Enter" && objective.trim() && publish()} />
        <Button size="sm" onClick={() => publish()} disabled={!objective.trim()}>发布</Button>
      </div>

      {/* C1：「已解决，放回继续」——附注写进任务行落审计 */}
      <AlertDialog
        open={resolveTarget !== null}
        onOpenChange={(open) => !open && setResolveTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>标记问题已解决并放回？</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p className="line-clamp-2 rounded border bg-card p-2 font-mono text-xs">
                  {resolveTarget?.objective}
                </p>
                <p>补充说明会写进任务行（认领会话在旧计划注入提示中可见）。</p>
                <textarea
                  value={resolveNote}
                  onChange={(e) => setResolveNote(e.target.value)}
                  rows={2}
                  className="w-full rounded-md border bg-background p-1.5 text-xs"
                  placeholder="人类已做了什么 / 需要执行者接下来注意什么（可空）"
                />
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <Button variant="outline" size="sm">取消</Button>
            </AlertDialogCancel>
            <Button
              size="sm"
              onClick={() => {
                const t = resolveTarget
                setResolveTarget(null)
                if (t) {
                  api.reopenTask(t.id, resolveNote.trim())
                    .then(() => { setResolveNote(""); refresh() })
                    .catch((e) => setError(String(e)))
                }
              }}
            >
              ✅ 已解决，放回继续
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* B1 发布去重确认：同指纹任务已存在，确认后 force 重发 */}
      <AlertDialog
        open={dupPending !== null}
        onOpenChange={(open) => !open && setDupPending(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>已存在相同目标任务</AlertDialogTitle>
            <AlertDialogDescription>
              任务 <span className="font-mono">{dupPending?.taskId}</span>
              （状态 {dupPending?.existed}）与本条发布内容相同（类型/范围/目标指纹一致）。
              仍要发布将产生一条重复任务。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <Button variant="outline" size="sm">取消</Button>
            </AlertDialogCancel>
            <Button
              size="sm"
              onClick={() => {
                const pending = dupPending
                setDupPending(null)
                if (pending) {
                  api.publishTask(pid, { ...pending.body, force: true })
                    .then(() => { setObjective(""); setConflictKeys(""); refresh() })
                    .catch((e) => setError(String(e)))
                }
              }}
            >
              仍要发布
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      {error && <p className="px-3 pt-2 text-xs text-(--status-error)">{error}</p>}

      {/* 看板列 */}
      <div className="grid min-h-0 flex-1 grid-cols-4 gap-3 overflow-auto p-3">
        {COLUMNS.map(({ status, label }) => (
          <div key={status} className="flex min-h-0 flex-col rounded-lg border bg-card/40">
            <div className="flex items-center gap-2 border-b px-3 py-2 text-xs font-medium">
              {label}
              <Badge variant="secondary" className="font-mono text-[10px]">
                {tasks.filter((t) => t.status === status).length}
              </Badge>
            </div>
            <div className="min-h-0 flex-1 space-y-2 overflow-auto p-2">
              {tasks.filter((t) => t.status === status).map((t) => (
                <TaskCard
                  key={t.id}
                  task={t}
                  onChanged={refresh}
                  onDelete={setDeleteTarget}
                  onResolve={setResolveTarget}
                  focused={focused?.id === t.id}
                  focusNonce={focused?.n ?? 0}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && !deleting && setDeleteTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除任务「{deleteTarget?.task_type}」？</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p className="line-clamp-3 rounded border bg-card p-2 font-mono text-xs">
                  {deleteTarget?.objective}
                </p>
                <p>
                  任务将被物理删除且不可恢复（删除会留一条 task.deleted 审计事件）。
                  {deleteTarget?.status === "claimed" && (
                    <span className="text-(--status-approval)">
                      该任务正在执行——当前这一步做完后会被立即硬中断（按人工终止收尾），
                      执行中的工具调用不会被打断；conflict_keys 锁立即释放。
                    </span>
                  )}
                  {deleteTarget?.status === "done" && (
                    <span className="text-muted-foreground">
                      已完成任务同样可删（审计事件保留战果快照）。
                    </span>
                  )}
                </p>
                {deleteError && <p className="text-sm text-(--status-error)">{deleteError}</p>}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <Button variant="outline" size="sm">取消</Button>
            </AlertDialogCancel>
            {/* 普通按钮而非 AlertDialogAction：删除失败时对话框不自动关闭 */}
            <Button variant="destructive" size="sm" onClick={remove} disabled={deleting}>
              {deleting ? "删除中…" : "删除"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <p className="border-t px-3 py-1.5 text-[10px] text-muted-foreground">
        待认领/失败任务可编辑（目标/类型/噪声/优先级/互斥键）；失败任务可放回待认领；
        四态任务均可删除（A1：claimed 取消在当前步结束即硬中断，done 战果快照进审计）；有子任务需先处理子任务（§6.4）
      </p>
    </div>
  )
}

function TaskCard({ task, onChanged, onDelete, onResolve, focused, focusNonce }: {
  task: Task
  onChanged: () => void
  onDelete: (t: Task) => void
  onResolve: (t: Task) => void
  focused: boolean
  focusNonce: number
}) {
  const editable = task.status === "open" || task.status === "failed"
  const [editing, setEditing] = useState(false)
  const [resumeErr, setResumeErr] = useState<string | null>(null)
  const cardRef = useRef<HTMLDivElement>(null)

  // A3：任务流跳来——滚动到卡片并高亮（nonce 变化即重新触发，同卡二次跳转也生效）
  useEffect(() => {
    if (!focused || !focusNonce) return
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })
  }, [focused, focusNonce])

  if (editing && editable) {
    return <TaskEdit task={task} onDone={() => { setEditing(false); onChanged() }} onCancel={() => setEditing(false)} />
  }

  return (
    <div
      ref={cardRef}
      className={cn("rounded-md border bg-card p-2 transition-shadow",
        focused && "ring-2 ring-primary")}
    >
      <div className="flex items-center gap-1.5">
        <Badge variant="outline" className="font-mono text-[10px]">{task.task_type}</Badge>
        <Badge variant="outline" className={cn("font-mono text-[10px]",
          task.noise_budget === "passive" ? "text-muted-foreground" : "text-(--status-approval)")}>
          {task.noise_budget}
        </Badge>
        {task.status === "failed" && task.blocked_reason === "awaiting_human" && (
          <Badge variant="outline" className="text-[10px] text-(--status-approval)"
                 title="Agent 挂起等待人工输入；现场快照保留，可续跑或放回继续（C1）">
            ⏸ 待人工
          </Badge>
        )}
        {(task.context?.attempts?.length ?? 0) >= 2 && (
          <Badge variant="outline" className="text-[10px] text-muted-foreground"
                 title={task.context!.attempts.map((a) =>
                   `${a.session_name ?? a.session_id} ${a.outcome}${a.blocked_reason ? `（${a.blocked_reason}）` : ""}：${a.result_note.slice(0, 80)}`).join("\n")}>
            ↻ {task.context!.attempts.length} 次尝试
          </Badge>
        )}
        <span className="flex-1" />
        <span className="font-mono text-[10px] text-muted-foreground">P{task.priority}</span>
      </div>
      <p className="mt-1.5 text-xs leading-relaxed">{task.objective}</p>
      {task.status === "failed" && task.blocked_reason === "awaiting_human" && task.result_note && (
        <p className="mt-1 rounded border border-(--status-approval)/40 bg-(--status-approval)/5 p-1.5 text-[10px] leading-relaxed">
          <span className="font-medium">需要人工：</span>{task.result_note}
        </p>
      )}
      {task.plan.length > 0 && (
        <p className="mt-1 truncate font-mono text-[10px] text-sky-400"
           title={task.plan.map((s) => `${s.id} ${s.title}：${s.status}${s.note ? `（${s.note}）` : ""}`).join("\n")}>
          ▦ {task.plan.filter((s) => s.status === "done").length}/{task.plan.length}
          {task.plan.some((s) => s.status === "blocked") && (
            <span className="text-amber-400">
              {" "}■ {task.plan.find((s) => s.status === "blocked")?.note ?? "阻塞"}
            </span>
          )}
        </p>
      )}
      {task.conflict_keys.length > 0 && (
        <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground"
           title={task.conflict_keys.join(", ")}>
          ⚔ {task.conflict_keys.join(", ")}
        </p>
      )}
      {(task.status === "open" && (task.wait_for?.length || task.workset?.length)) && (
        <p className="mt-1 flex flex-wrap gap-1">
          {(task.wait_for ?? []).map((k) => (
            <Badge key={k} variant="outline" className="font-mono text-[10px] text-(--status-approval)"
                   title="资源被其他任务占用，释放后可被认领（B2 wait_for 门控）">
              ⏳ {k}
            </Badge>
          ))}
          {(task.workset ?? []).map((w) => (
            <Badge key={w} variant="outline" className="font-mono text-[10px] text-muted-foreground"
                   title="工作集软声明：有人正在分析此目标（advisory，不阻塞）">
              {w}
            </Badge>
          ))}
        </p>
      )}
      {resumeErr && (
        <p className="mt-1 truncate text-[10px] text-(--status-error)" title={resumeErr}>
          续跑失败：{resumeErr}（可改用「放回」重新派发）
        </p>
      )}
      <div className="mt-1.5 flex items-center gap-2">
        {task.claimed_by && (
          <span className="font-mono text-[10px] text-primary">{task.claimed_by.slice(0, 14)}</span>
        )}
        {task.result_note && (
          <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground" title={task.result_note}>
            {task.result_note}
          </span>
        )}
        <span className="flex-1" />
        {task.status === "failed" && task.resumable && (
          <button
            onClick={async () => {
              setResumeErr(null)
              try {
                await api.resumeTask(task.id)
                onChanged()
              } catch (e) {
                setResumeErr(e instanceof Error ? e.message : String(e))
              }
            }}
            className="text-[10px] text-primary hover:underline"
            title="带现场续跑（E12）：原会话从落盘快照与步数断点恢复，上下文不丢"
          >
            ▶ 续跑
          </button>
        )}
        {task.status === "failed" && task.blocked_reason === "awaiting_human" && (
          <button
            onClick={() => onResolve(task)}
            className="text-[10px] text-primary hover:underline"
            title="人工已解决挂起原因：附注后放回待认领（附注写进任务行落审计，C1）"
          >
            ✅ 已解决，放回继续
          </button>
        )}
        {task.status === "failed" && task.blocked_reason !== "awaiting_human" && (
          <button
            onClick={async () => {
              try {
                await api.reopenTask(task.id)
                onChanged()
              } catch { /* 3s 轮询会反映状态；冲突时按钮仍在 */ }
            }}
            className="text-[10px] text-primary hover:underline"
            title="放回待认领（保留失败备注在库存中）"
          >
            ♻ 放回
          </button>
        )}
        {editable && (
          <button onClick={() => setEditing(true)}
                  className="text-[10px] text-muted-foreground hover:text-foreground">
            ✏ 编辑
          </button>
        )}
        <button
          onClick={() => onDelete(task)}
          title={task.status === "claimed"
            ? "取消执行中任务：当前步结束即硬中断（A1）"
            : "物理删除（留 task.deleted 审计）"}
          className="text-[10px] text-muted-foreground hover:text-(--status-error)"
        >
          {task.status === "claimed" ? "🗑 取消" : "🗑 删除"}
        </button>
      </div>
    </div>
  )
}

// 内联编辑（open/failed）：五字段；failed 额外提供「保存并放回」
function TaskEdit({ task, onDone, onCancel }: {
  task: Task
  onDone: () => void
  onCancel: () => void
}) {
  const [objective, setObjective] = useState(task.objective)
  const [taskType, setTaskType] = useState(task.task_type)
  const [noise, setNoise] = useState(task.noise_budget)
  const [priority, setPriority] = useState(task.priority)
  const [conflictKeys, setConflictKeys] = useState(task.conflict_keys.join(", "))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const buildPatch = () => {
    const keys = conflictKeys.split(",").map((s) => s.trim()).filter(Boolean)
    return {
      objective: objective.trim(),
      task_type: taskType.trim(),
      noise_budget: noise,
      priority,
      conflict_keys: noise === "passive" ? [] : keys,
    }
  }

  const save = async (andReopen: boolean) => {
    setSaving(true)
    setError(null)
    try {
      await api.updateTask(task.id, buildPatch())
      if (andReopen) await api.reopenTask(task.id)
      onDone()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-1.5 rounded-md border border-primary/40 bg-card p-2">
      <Input className="h-8 text-xs" value={taskType} onChange={(e) => setTaskType(e.target.value)}
             placeholder="类型" list="task-type-options" />
      <select
        value={noise}
        onChange={(e) => setNoise(e.target.value)}
        className="h-8 w-full rounded-md border bg-background px-1.5 text-xs"
      >
        {NOISE_LEVELS.map((n) => (
          <option key={n} value={n}>{n}{n === "passive" ? "" : "（active）"}</option>
        ))}
      </select>
      {noise !== "passive" && (
        <Input className="h-8 font-mono text-xs" value={conflictKeys}
               onChange={(e) => setConflictKeys(e.target.value)}
               placeholder="conflict_keys（逗号分隔，必填）" />
      )}
      <div className="flex items-center gap-1.5">
        <span className="text-[10px] text-muted-foreground">优先级</span>
        <Input
          type="number" min={0} max={9} className="h-8 w-16 text-center font-mono text-xs"
          value={priority} onChange={(e) => setPriority(Number(e.target.value))}
        />
      </div>
      <textarea
        value={objective}
        onChange={(e) => setObjective(e.target.value)}
        rows={3}
        className="w-full rounded-md border bg-background p-1.5 text-xs leading-relaxed"
        placeholder="任务目标…"
      />
      {error && <p className="text-[10px] text-(--status-error)">{error}</p>}
      <div className="flex justify-end gap-1.5">
        <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={onCancel} disabled={saving}>
          取消
        </Button>
        {task.status === "failed" && (
          <Button size="sm" variant="outline" className="h-7 text-xs"
                  onClick={() => save(true)} disabled={saving || !objective.trim()}>
            {saving ? "…" : "保存并放回"}
          </Button>
        )}
        <Button size="sm" className="h-7 text-xs"
                onClick={() => save(false)} disabled={saving || !objective.trim()}>
          {saving ? "…" : "保存"}
        </Button>
      </div>
    </div>
  )
}
