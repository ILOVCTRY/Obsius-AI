import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { api } from "@/lib/api"
import type { Artifact, RoleInfo, Task } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { TaskTraceList } from "@/components/blackboard/TaskTraceList"
import { cn } from "@/lib/utils"

// 任务看板（DESIGN.md §12 页面 5 / §6.4 人类插手通道）：
// 四态 open/claimed/done/failed——open/failed 可编辑；failed 可放回待执行；
// 四态皆可物理删除（A1：留 task.deleted 审计，claimed 当前步结束即硬中断，有子任务 409）。
// claimed 卡显示认领者计划进度（A2：task.plan，done/total + blocked 原因）。
// v0.71 任务即窗口：双击任务卡直开专属执行窗（四态通用；终态窗=延续模式可续聊）。

const COLUMNS: { status: Task["status"]; label: string }[] = [
  { status: "open", label: "待执行" },
  { status: "claimed", label: "执行中" },
  { status: "done", label: "已完成" },
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
  const [publishRole, setPublishRole] = useState("")
  const [noise, setNoise] = useState("passive")
  const [conflictKeys, setConflictKeys] = useState("")
  // ⑤ 验收条目（一行一条，存 context.reconcile；Agent 全部收口前 complete 被硬拦）
  const [acceptance, setAcceptance] = useState("")
  const [priority, setPriority] = useState(2)
  const [error, setError] = useState<string | null>(null)
  // C1 放回后无 worker 被唤醒（paused/L0）→ 提示任务已排队等恢复，不会自动执行
  const [reopenNotice, setReopenNotice] = useState<string | null>(null)
  // 轨任务类型注册表（task_types.yaml + generic）：输入框 datalist 提示，非法值后端 422
  const [typeOptions, setTypeOptions] = useState<Record<string, string>>({ generic: "passive" })
  // v14 任务绑定角色：发布栏角色下拉（GET /roles）+ 任务卡 🎭 显示名映射
  const [roles, setRoles] = useState<RoleInfo[]>([])
  const roleNames = useMemo(
    () => Object.fromEntries(roles.map((r) => [r.role, r.name])) as Record<string, string>,
    [roles])
  // B1 发布去重：命中同指纹 open/claimed 任务 → 确认"仍要发布"后 force 重发
  const [dupPending, setDupPending] = useState<{
    body: Parameters<typeof api.publishTask>[1]; existed: string; taskId: string
  } | null>(null)
  // C1：「已解决，放回继续」附注（写进任务行 result_note 落审计）
  const [resolveTarget, setResolveTarget] = useState<Task | null>(null)
  const [resolveNote, setResolveNote] = useState("")
  // C6：放回时可选「丢弃现场，从零重做」（reopen drop_scene）
  const [dropScene, setDropScene] = useState(false)

  useEffect(() => {
    Promise.all([api.getProject(pid), api.taxonomy()])
      .then(([proj, tax]) => setTypeOptions({ generic: "passive", ...(tax.task_types[proj.track] ?? {}) }))
      .catch(() => {})
    api.listRoles(pid).then(setRoles).catch(() => {})
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
      role: publishRole || undefined,
      noise_budget: noise,
      priority,
      conflict_keys: noise === "passive" ? undefined : keys,
      acceptance: acceptance.split("\n").map((s) => s.trim()).filter(Boolean),
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
      setAcceptance("")
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
        {/* v14 任务绑定角色：底色匹配窗排序优先，任何窗均可即时认领（认领即换装，无宽限兜底——v0.63） */}
        <select
          value={publishRole}
          onChange={(e) => setPublishRole(e.target.value)}
          className="h-9 rounded-md border bg-card px-2 text-sm"
          title="建议认领角色（可选）：专属执行窗按该角色装配，中途可改（热换装）"
        >
          <option value="">角色不限</option>
          {roles.map((r) => (
            <option key={r.role} value={r.role}>{r.name || r.role}</option>
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
        <textarea
          value={acceptance}
          onChange={(e) => setAcceptance(e.target.value)}
          rows={2}
          className="h-9 w-full resize-y rounded-md border bg-card p-1.5 text-xs"
          placeholder="验收条目（可选，一行一条）：发布后执行者须逐条 task_reconcile 收口，全部收口前 complete_task 被硬拦"
        />
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
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <input type="checkbox" checked={dropScene} onChange={(e) => setDropScene(e.target.checked)} />
                  丢弃现场，从零重做（删除任务快照与对话现场；默认保留，认领即断点续接）
                </label>
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
                  api.reopenTask(t.id, resolveNote.trim(), dropScene)
                    .then((r) => {
                      setResolveNote("")
                      setDropScene(false)
                      setReopenNotice((r.kicked?.length ?? 0) > 0 ? null :
                        "任务已放回待认领，但没有空闲窗被唤醒（自主编排处于暂停或 L0 手动档）——到会话「跑任务队列」或恢复编排后才会开始执行")
                      refresh()
                    })
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
      {reopenNotice && (
        <p className="px-3 pt-2 text-xs text-(--status-approval)">{reopenNotice}</p>
      )}

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
                  pid={pid}
                  task={t}
                  roles={roles}
                  roleNames={roleNames}
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
                {/* wrap-anywhere：超长不可断 token（URL/base64）会沿 grid 链路撑爆 min-content 致弹窗溢出，anywhere 才收窄 min-content（break-words 无效） */}
                <p className="line-clamp-3 wrap-anywhere rounded border bg-card p-2 font-mono text-xs">
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
        任务即窗口（v0.71）：发布即建专属执行窗，一窗一任务；双击任务卡直开会话（待执行=跳转并立刻起跑，无窗自动补绑待命窗后起跑；终态窗=延续模式可续聊）；
        待执行/失败任务可编辑（目标/类型/噪声/优先级/角色/互斥键）；执行中任务仅可改角色（热换装，下个步进生效）；
        失败任务可放回原窗续跑；四态任务均可删除（A1：claimed 取消在当前步结束即硬中断，done 战果快照进审计）；
        有子任务需先处理子任务（§6.4）
      </p>
    </div>
  )
}

function TaskCard({ pid, task, roles, roleNames, onChanged, onDelete, onResolve, focused, focusNonce }: {
  pid: string
  task: Task
  roles: RoleInfo[]
  roleNames: Record<string, string>
  onChanged: () => void
  onDelete: (t: Task) => void
  onResolve: (t: Task) => void
  focused: boolean
  focusNonce: number
}) {
  const editable = task.status === "open" || task.status === "failed"
  const [editing, setEditing] = useState(false)
  const [resumeErr, setResumeErr] = useState<string | null>(null)
  // v0.71：执行中任务中途改角色（PATCH 仅放行 role，热换装下个步进生效）
  const [roleEditing, setRoleEditing] = useState(false)
  const [newRole, setNewRole] = useState(task.role ?? "")
  const [roleErr, setRoleErr] = useState<string | null>(null)
  // 双击直开会话（v0.71 任务即窗口，四态通用）：有专属窗挂回；终态旧窗关闭时创建复盘窗；open 无绑 →
  // 手动补绑待命窗（2026-09-23）成功即挂回，失败提示留卡上；其余回看板定位。
  // 待执行（open）任务挂回后显式按任务 ID 直派，避免会话中其他待执行任务被误启动。
  const [spawnErr, setSpawnErr] = useState<string | null>(null)
  const startWork = (sid: string) => {
    api.agentWork(sid, task.id).catch((e) =>
      setSpawnErr(`自动启动失败：${e instanceof Error ? e.message : String(e)}`))
  }
  const openSession = async () => {
    let sid = task.target_session || task.claimed_by
    const sessionAlive = sid ? await api.sessions(pid).then((rows) => rows.some((row) => row.id === sid && row.status !== "closed")).catch(() => false) : false
    if (sid && sessionAlive) {
      window.dispatchEvent(new CustomEvent("goto-session", { detail: { sessionId: sid } }))
      if (task.status === "open") startWork(sid)
      return
    }
    if (task.status === "done" || task.status === "failed") {
      try {
        setSpawnErr(null)
        const r = await api.spawnWindow(task.id)
        sid = r.session_id
        window.dispatchEvent(new CustomEvent("goto-session", { detail: { sessionId: sid } }))
        return
      } catch (e) {
        setSpawnErr(e instanceof Error ? e.message : String(e))
      }
    }
    if (task.status === "open") {
      try {
        setSpawnErr(null)
        const r = await api.spawnWindow(task.id)
        sid = r.session_id
        window.dispatchEvent(new CustomEvent("goto-session", { detail: { sessionId: sid } }))
        startWork(sid)
        return
      } catch (e) {
        setSpawnErr(e instanceof Error ? e.message : String(e))
      }
    }
    window.dispatchEvent(new CustomEvent("goto-tasks", { detail: { taskId: task.id } }))
  }
  // 工作区隔离（W3）：按任务归属查看产物清单
  const [arts, setArts] = useState<Artifact[] | null>(null)
  const [showArts, setShowArts] = useState(false)
  // 执行轨迹（execution-trace-chain M1）：认领过的任务才可展开（未被认领无区间）
  const [showTrace, setShowTrace] = useState(false)
  const cardRef = useRef<HTMLDivElement>(null)

  // A3：任务流跳来——滚动到卡片并高亮（nonce 变化即重新触发，同卡二次跳转也生效）
  useEffect(() => {
    if (!focused || !focusNonce) return
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })
  }, [focused, focusNonce])

  const saveRole = async () => {
    setRoleErr(null)
    try {
      await api.updateTask(task.id, { role: newRole })
      setRoleEditing(false)
      onChanged()
    } catch (e) {
      setRoleErr(e instanceof Error ? e.message : String(e))
    }
  }

  if (editing && editable) {
    return <TaskEdit task={task} roles={roles} onDone={() => { setEditing(false); onChanged() }} onCancel={() => setEditing(false)} />
  }

  return (
    <div
      ref={cardRef}
      onDoubleClick={openSession}
      title="双击打开会话窗（待执行=跳转并立刻起跑·无窗自动补绑 / 执行中=在跑窗 / 已结束=延续模式续聊）"
      className={cn("cursor-pointer rounded-md border bg-card p-2 transition-shadow",
        focused && "ring-2 ring-primary")}
    >
      <div className="flex items-center gap-1.5">
        <Badge variant="outline" className="font-mono text-[10px]">{task.task_type}</Badge>
        {task.created_by === "playbook" && (
          <Badge variant="outline" className="text-[10px] text-primary"
                 title="阶段剧本首发任务（分阶段工作流：阶段启动时照剧本原样发布，回退重进不重发）">
            📋 剧本
          </Badge>
        )}
        {task.role && (
          <Badge variant="outline" className="text-[10px] text-(--status-paused)"
                 title={`建议认领角色 ${task.role}：底色匹配窗排序优先，任何窗均可即时认领（认领即换装）`}>
            🎭 {roleNames[task.role] || task.role}
          </Badge>
        )}
        {(task.context?.attachments?.length ?? 0) > 0 && (
          <Badge variant="outline" className="text-[10px] text-muted-foreground"
                 title={task.context!.attachments!.map((a) => `${a.name}（${a.size}B）`).join("\n")}>
            📎 {task.context!.attachments!.length === 1
              ? task.context!.attachments![0].name
              : `${task.context!.attachments!.length} 个附件`}
          </Badge>
        )}
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
        {(task.status === "done" || task.status === "failed") && (
          <Badge variant="outline"
                 className="cursor-pointer text-[10px] text-muted-foreground hover:bg-accent"
                 title="本任务产物清单（工作区隔离 W3：产物按任务归属，正式产物在 artifacts/）"
                 onClick={async () => {
                   if (!showArts) {
                     try {
                       setArts(await api.artifacts(pid, { task_id: task.id }))
                     } catch { setArts([]) }
                   }
                   setShowArts((v) => !v)
                 }}>
            📎 产物
          </Badge>
        )}
        {task.claimed_by && (
          <Badge variant="outline"
                 className="cursor-pointer text-[10px] text-muted-foreground hover:bg-accent"
                 title="本任务执行轨迹（会话任务区间内的过程聚合：技能/知识库/工具/产出）"
                 onClick={() => setShowTrace((v) => !v)}>
            🧭 轨迹
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
      {showArts && (
        <div className="mt-1.5 rounded border bg-background p-1.5 text-[10px]">
          {arts === null ? (
            <span className="text-muted-foreground">加载中…</span>
          ) : arts.length === 0 ? (
            <span className="text-muted-foreground">本任务暂无产物（Agent 落正式产物走 bb_add_artifact）</span>
          ) : (
            <ul className="space-y-0.5">
              {arts.map((a) => (
                <li key={a.id} className="truncate font-mono text-muted-foreground"
                    title={`${a.path} · sha256=${a.sha256.slice(0, 16)} · ${a.author}`}>
                  📄 {a.path}
                  <span className="ml-1 text-muted-foreground/60">{a.created_at.slice(11, 19)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {showTrace && <TaskTraceList pid={pid} taskId={task.id} className="mt-1.5 rounded border bg-background p-1.5" />}
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
      {(task.context?.reconcile?.length ?? 0) > 0 && (() => {
        const rec = task.context!.reconcile!
        const done = rec.filter((r) => r.state !== "pending").length
        const label = { met: "✅", failed: "❌", blocked: "⛔", pending: "☐" } as const
        return (
          <p className="mt-1 truncate font-mono text-[10px] text-emerald-400"
             title={`验收对账 ${done}/${rec.length}（全部收口前 complete_task 被硬拦）\n` +
               `🔬=独立验证条目：met/failed 由服务端验证器判定，Agent 不可自报\n` +
               rec.map((r) => `${label[r.state]} #${r.id}${r.verify ? "🔬" : ""} ${r.text}${r.note ? `（${r.note}）` : ""}`).join("\n")}>
            ☑ {done}/{rec.length}
            {rec.some((r) => r.state === "blocked") && (
              <span className="text-amber-400">
                {" "}■ {rec.find((r) => r.state === "blocked")?.note || "受阻"}
              </span>
            )}
          </p>
        )
      })()}
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
      {spawnErr && (
        <p className="mt-1 truncate text-[10px] text-(--status-error)" title={spawnErr}>
          开窗失败：{spawnErr}
        </p>
      )}
      <div className="mt-1.5 flex items-center gap-2">
        {task.claimed_by && (
          <button
            className="rounded border px-1 font-mono text-[10px] text-primary hover:bg-accent"
            title={`执行窗 ${task.claimed_by}（点击打开会话页签）`}
            onClick={() => window.dispatchEvent(new CustomEvent("goto-session", { detail: { sessionId: task.claimed_by } }))}
          >
            ⧉ {task.claimed_by.slice(0, 14)}
          </button>
        )}
        {task.status === "open" && task.target_session && !task.claimed_by && (
          <Badge variant="outline" className="font-mono text-[10px] text-(--status-approval)"
                 title="已指派给该窗口（v18 认领门控：仅该窗可认领；关窗自动退回公共池）">
            → {task.target_session.slice(0, 14)}
          </Badge>
        )}
        {task.result_note && (
          <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground" title={task.result_note}>
            {task.result_note}
          </span>
        )}
        <span className="flex-1" />
        {task.status === "failed" && (
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
            title={task.resume_mode === "snapshot"
              ? "⚡ 带现场续跑（C6）：从任务键断点快照恢复对话与步数预算，可跨会话/跨角色"
              : "↩ 接手现场续跑（C6）：恢复最近对话现场（末 60 条）与尝试履历，重新认领"}
          >
            {task.resume_mode === "snapshot" ? "⚡ 带现场续跑" : "↩ 接手现场续跑"}
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
        {task.status === "claimed" && (
          <button onClick={() => { setNewRole(task.role ?? ""); setRoleEditing((v) => !v); setRoleErr(null) }}
                  className="text-[10px] text-muted-foreground hover:text-foreground"
                  title="执行中任务仅可改角色：保存后对在跑会话立即热换装（prompt 下个步进生效）">
            🎭 改角色
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
      {roleEditing && task.status === "claimed" && (
        <div className="mt-1.5 flex items-center gap-1.5 rounded border border-primary/40 bg-background p-1.5">
          <select
            value={newRole}
            onChange={(e) => setNewRole(e.target.value)}
            className="h-7 flex-1 rounded border bg-card px-1.5 text-xs"
            title="热换装角色：保存后对在跑会话立即生效（prompt 下个步进重建）"
          >
            <option value="">角色不限</option>
            {roles.map((r) => (
              <option key={r.role} value={r.role}>{r.name || r.role}</option>
            ))}
          </select>
          <Button size="sm" variant="ghost" className="h-7 text-xs"
                  onClick={() => setRoleEditing(false)} disabled={roleErr !== null}>
            取消
          </Button>
          <Button size="sm" className="h-7 text-xs" onClick={saveRole}>换装</Button>
          {roleErr && <span className="truncate text-[10px] text-(--status-error)">{roleErr}</span>}
        </div>
      )}
    </div>
  )
}

// 内联编辑（open/failed）：六字段（v0.71 加角色）；failed 额外提供「保存并放回」
function TaskEdit({ task, roles, onDone, onCancel }: {
  task: Task
  roles: RoleInfo[]
  onDone: () => void
  onCancel: () => void
}) {
  const [objective, setObjective] = useState(task.objective)
  const [taskType, setTaskType] = useState(task.task_type)
  const [noise, setNoise] = useState(task.noise_budget)
  const [priority, setPriority] = useState(task.priority)
  const [editRole, setEditRole] = useState(task.role ?? "")
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
      role: editRole || "",
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
        <select
          value={editRole}
          onChange={(e) => setEditRole(e.target.value)}
          className="h-8 flex-1 rounded-md border bg-background px-1.5 text-xs"
          title="专属执行窗按该角色装配（v0.71 任务即窗口）"
        >
          <option value="">角色不限</option>
          {roles.map((r) => (
            <option key={r.role} value={r.role}>{r.name || r.role}</option>
          ))}
        </select>
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
