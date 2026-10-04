import { useMemo, useState } from "react"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { ApprovalCard } from "@/components/chat/ApprovalCard"
import { fmtDateTimeMin, utcTitle } from "@/lib/datetime"
import type { Approval, BBEvent, DecideApprovalResult, Task } from "@/lib/types"

// Trae 风格会话视图 v2（2026-09-28，对齐 TraeWork 真实截图）：任务即计划条目的
// 执行视图。真实形态四要素——①条目行式无边框（✅完成/☁进行中(蓝脉冲)/✳待执行/
// ✗失败 + 标题截断 + ›）；②展开为行式二级折叠（「已读取 N 个文件 ›」每行可再展开
// 明细列表）；③「正在规划下一步」灰色极简状态行（无框，展开看编排器思考流）；
// ④头部任务耗时行。引导=右气泡、审批卡保留可决策。
// 与 claude 风格（终端行式对话流）经页签行风格钮切换（config.ui_style，项目级生效）。

const isHumanNote = (e: BBEvent) =>
  e.kind === "message.inbox" && e.payload.kind === "human_note"

// 单时间线归并项：引导气泡 / 任务条目 / 审批卡（ISO 串排序）
type Timeline = { ts: string; kind: "note" | "task" | "approval"; note?: BBEvent; task?: Task; apr?: BBEvent }

// 状态图标对齐 TraeWork：✅ 完成 / ☁ 蓝脉冲执行中 / ✳ 灰待执行 / ✗ 失败
const STATUS_META: Record<Task["status"], { icon: string; label: string; cls: string }> = {
  open: { icon: "✳", label: "待执行", cls: "text-muted-foreground" },
  claimed: { icon: "☁", label: "执行中", cls: "text-primary animate-pulse" },
  done: { icon: "✅", label: "已完成", cls: "" },
  failed: { icon: "✗", label: "失败", cls: "text-(--status-error)" },
}

/** 条目执行统计 + 明细列表（v1 按绑定窗全量累计；本窗会跨任务复用，口径=「本窗累计」） */
function statsOf(t: Task, events: BBEvent[]) {
  const readFiles: string[] = []
  const artifacts: string[] = []
  const toolNames: string[] = []
  const cmds: string[] = []
  const findingTitles: string[] = []
  const parseArgs = (raw: unknown): Record<string, unknown> | null => {
    try {
      const a = typeof raw === "string" ? JSON.parse(raw) : raw
      return a && typeof a === "object" ? (a as Record<string, unknown>) : null
    } catch {
      return null // args 截断串解析失败——忽略明细只留计数
    }
  }
  for (const e of events) {
    if (t.target_session && e.session_id !== t.target_session) continue
    if (e.kind === "tool.call") {
      toolNames.push(String(e.payload.name ?? "?"))
      if (e.payload.name === "read_file") {
        const p = parseArgs(e.payload.args)?.path
        if (typeof p === "string") readFiles.push(p)
      }
      if (e.payload.name === "bb_add_artifact") {
        const f = parseArgs(e.payload.args)?.filename
        if (typeof f === "string") artifacts.push(f)
      }
    } else if (e.kind === "command") {
      const c = String(e.payload.cmd ?? e.payload.command ?? "")
      if (c) cmds.push(c.split("\n")[0].slice(0, 90))
    } else if (e.kind === "finding.new") {
      const title = e.payload.title
      if (typeof title === "string") findingTitles.push(title)
    }
  }
  return { readFiles, artifacts, toolNames, cmds, findingTitles }
}

/** 行式二级折叠（trae 形态）：「已读取 N 个文件 ›」——summary 计数行，展开=明细列表 */
function StatLine({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <details>
      <summary className="cursor-pointer select-none py-px text-[11px] leading-relaxed text-muted-foreground hover:text-foreground">
        {label} <span className="text-muted-foreground/40">›</span>
      </summary>
      <ul className="mt-0.5 mb-1 space-y-0.5 pl-3">
        {items.slice(0, 20).map((x, i) => (
          <li key={i} className="truncate font-mono text-[10px] text-muted-foreground/80" title={x}>{x}</li>
        ))}
        {items.length > 20 && (
          <li className="text-[10px] text-muted-foreground/60">…共 {items.length} 条（全部见审计流）</li>
        )}
      </ul>
    </details>
  )
}

/** 任务耗时（trae 头部「任务耗时 14m 15s」）：done/failed=收尾耗时，claimed=至今 */
const durOf = (t: Task) => {
  const ms = Math.max(0, new Date(t.updated_at).getTime() - new Date(t.created_at).getTime())
  const m = Math.floor(ms / 60000)
  const s = Math.floor((ms % 60000) / 1000)
  return `${m}m ${s}s`
}

function TaskCard({ task, events, report, reportBusy, onGenerate }: {
  task: Task
  events: BBEvent[]
  report?: BBEvent
  reportBusy: boolean
  onGenerate: (tid: string) => void
}) {
  const [open, setOpen] = useState(false)
  const meta = STATUS_META[task.status]
  const stats = useMemo(() => statsOf(task, events), [task, events])
  return (
    <div title={utcTitle(task.created_at)}>
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 rounded px-1 py-1 text-left hover:bg-accent/40">
        <span className={`shrink-0 text-[13px] leading-none ${meta.cls}`}>{meta.icon}</span>
        <span className="min-w-0 flex-1 truncate text-[13px] text-foreground/90"
          title={task.objective}>{task.objective}</span>
        {(task.status === "done" || task.status === "failed" || task.status === "claimed") && (
          <span className="shrink-0 font-mono text-[10px] text-muted-foreground/50">{durOf(task)}</span>
        )}
        <span className="shrink-0 text-[10px] text-muted-foreground/50">{open ? "⌄" : "›"}</span>
      </button>
      {open && (
        <div className="ml-[18px] space-y-0.5 border-l border-white/10 py-0.5 pl-2.5">
          {(task.result_note || null) && (
            <p className="whitespace-pre-wrap break-words py-0.5 text-xs leading-relaxed text-muted-foreground"
              title="任务结论（result_note）">
              {task.result_note}
            </p>
          )}
          <StatLine label={`已读取 ${stats.readFiles.length} 个文件`} items={stats.readFiles} />
          <StatLine label={`已写入 ${stats.artifacts.length} 个产物`} items={stats.artifacts} />
          <StatLine label={`调用了 ${stats.toolNames.length} 个工具`} items={stats.toolNames} />
          <StatLine label={`执行了 ${stats.cmds.length} 条命令`} items={stats.cmds} />
          <StatLine label={`登记了 ${stats.findingTitles.length} 条发现`} items={stats.findingTitles} />
          <p className="py-0.5 text-[10px] text-muted-foreground/60">
            {task.task_type || "generic"} · {fmtDateTimeMin(task.created_at)} 发布 · 统计为绑定窗累计
          </p>
          {task.status === "done" && (
            report ? (
              <details open>
                <summary className="cursor-pointer select-none py-px text-[11px] text-primary hover:underline">
                  📋 任务报告（{fmtDateTimeMin(report.created_at)} 生成）
                </summary>
                <div className="my-1 rounded border bg-background/60 px-2 py-1.5">
                  <MarkdownView content={String(report.payload.report ?? "")}
                    prefix={`trae-report-${task.id}`}
                    className="max-h-96 overflow-auto text-xs leading-relaxed text-foreground/90" />
                </div>
              </details>
            ) : (
              <button type="button" disabled={reportBusy}
                onClick={() => onGenerate(task.id)}
                title="生成 LLM 任务状况报告（md，持久化可查）"
                className="my-0.5 rounded border border-primary/50 px-2 py-0.5 text-[11px] text-primary hover:bg-primary/10 disabled:opacity-50">
                {reportBusy ? "📋 报告生成中…" : "📋 生成任务报告"}
              </button>
            )
          )}
        </div>
      )}
    </div>
  )
}

function NoteBubble({ event }: { event: BBEvent }) {
  const text = String(event.payload.text ?? event.payload.title ?? "")
  return (
    <div className="flex justify-end pr-1" title={utcTitle(event.created_at)}>
      <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary/10 px-3 py-1.5 text-sm leading-relaxed text-foreground">
        <span className="whitespace-pre-wrap break-words">{text}</span>
        <span className="ml-2 align-baseline text-[10px] text-muted-foreground/60">
          {fmtDateTimeMin(event.created_at)}
        </span>
      </div>
    </div>
  )
}

export function TraeView({ pid, tasks, events, orchRunning, reportBusy,
  onGenerateReport, approvalById, onDecideApproval }: {
  pid: string
  tasks: Task[]
  events: BBEvent[]
  orchRunning: boolean
  reportBusy: boolean
  onGenerateReport: (tid: string) => void
  /** 待审批完整行（usePendingApprovals 的 byId；未命中=已决策 → 出流） */
  approvalById: Map<string, Approval>
  onDecideApproval: (approval: Approval, decision: "approved" | "rejected", note: string) => Promise<DecideApprovalResult | void>
}) {
  const reports = useMemo(() => {
    const m = new Map<string, BBEvent>()
    for (const e of events) {
      if (e.kind === "task.report" && typeof e.payload.task_id === "string") {
        const prev = m.get(e.payload.task_id)
        if (!prev || e.id > prev.id) m.set(e.payload.task_id, e)  // 重生成取最新
      }
    }
    return m
  }, [events])

  // 单时间线：引导气泡 + 任务条目 + 审批卡按时间归并升序（ISO 串字典序=时间序）
  const timeline = useMemo(() => {
    const items: Timeline[] = []
    for (const e of events) {
      if (isHumanNote(e)) items.push({ ts: e.created_at, kind: "note", note: e })
      else if (e.kind === "approval.requested") items.push({ ts: e.created_at, kind: "approval", apr: e })
    }
    for (const t of tasks) {
      items.push({ ts: t.created_at, kind: "task", task: t })
    }
    return items.sort((a, b) => a.ts.localeCompare(b.ts))
  }, [events, tasks])

  // 「正在规划下一步」的思考展开：编排器（author=orchestrator）思考流最近 12 条
  const orchThinking = useMemo(() => events
    .filter((e) => (e.kind === "llm.thinking" || e.kind === "llm.thinking.delta")
      && e.author === "orchestrator")
    .slice(-12), [events])

  // 头部耗时行（trae「任务耗时 14m 15s」）：取绑定窗最新一条任务的耗时
  const lastTask = tasks.length > 0 ? tasks[tasks.length - 1] : null

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto px-3 py-2">
      <div className="flex items-center gap-2 px-1 py-0.5 text-[11px] text-muted-foreground/70">
        <span>执行视图</span>
        {lastTask && <span>· 任务耗时 {durOf(lastTask)}</span>}
        {lastTask && <span className="text-muted-foreground/40">·</span>}
        <span className="text-muted-foreground/40">{tasks.length} 条计划</span>
      </div>
      {orchRunning && (
        // trae 形态：灰色极简状态行（无框无卡片），展开=编排器思考流
        <details className="px-1">
          <summary className="flex cursor-pointer select-none items-center gap-1.5 text-[11px] text-muted-foreground">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
            正在规划下一步
            {orchThinking.length > 0 && <span className="text-muted-foreground/40">›</span>}
          </summary>
          {orchThinking.length > 0 && (
            <div className="ml-3 mt-0.5 max-h-64 space-y-1 overflow-auto border-l border-white/10 pl-2.5">
              {orchThinking.map((e) => (
                <p key={e.id} className="text-xs leading-relaxed text-muted-foreground/90">
                  {String(e.payload.text ?? "")}
                </p>
              ))}
            </div>
          )}
        </details>
      )}
      {timeline.length === 0 && (
        <div className="mx-auto mt-10 max-w-md rounded-lg border bg-card p-4 text-center text-xs leading-relaxed text-muted-foreground">
          <p className="mb-1 text-foreground">执行视图</p>
          发任务/引导会话后，这里以计划清单呈现执行状态；任务完成可生成 LLM 状况报告。
        </div>
      )}
      {timeline.map((it) => {
        if (it.kind === "note" && it.note) return <NoteBubble key={`n${it.note.id}`} event={it.note} />
        if (it.kind === "approval" && it.apr) {
          const aid = String(it.apr.payload.approval_id ?? "")
          const apr = approvalById.get(aid)
          // 已决策（不在待审批列表）即出流，保持清单干净
          return apr ? <ApprovalCard key={`a${it.apr.id}`} approval={apr} onDecide={onDecideApproval} /> : null
        }
        if (it.kind === "task" && it.task) {
          return (
            <TaskCard key={`t${it.task.id}`} task={it.task} events={events}
              report={reports.get(it.task.id)} reportBusy={reportBusy}
              onGenerate={onGenerateReport} />
          )
        }
        return null
      })}
      <span className="hidden">{pid}</span>
    </div>
  )
}
