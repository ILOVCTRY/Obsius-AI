import { useEffect, useMemo, useState } from "react"
import { ArrowLeft, RefreshCw, Users } from "lucide-react"
import { api } from "@/lib/api"
import { fmtDateTimeMin } from "@/lib/datetime"
import type { Team, TeamRun, TeamRunMember } from "@/lib/types"
import { cn } from "@/lib/utils"

// 团队运行报告（2026-10-06，Phase E）：直播间内全屏视图，参考 cc-haha AgentTeamsWorkbench 的
// 顶栏 + 编队布局，展示 Run 成员执行明细（objective/status/outcome/error + 打开执行会话）。
// 数据=GET /teams/{id}（编队）+ GET /teams/{id}/runs（历史 Run，取最新或手选），3s 轮询。
// Canvas 依赖图/通讯面板本阶段不做（core/team 无共享任务列表/邮箱数据）。

const MSTATUS: Record<string, { label: string; cls: string }> = {
  active: { label: "待命", cls: "text-muted-foreground border-border" },
  pending: { label: "待启动", cls: "text-muted-foreground border-border" },
  creating: { label: "创建中", cls: "text-(--status-approval) border-(--status-approval)/40" },
  running: { label: "执行中", cls: "text-primary border-primary/40" },
  completed: { label: "已完成", cls: "text-(--status-ok) border-(--status-ok)/40" },
  failed: { label: "失败", cls: "text-(--status-error) border-(--status-error)/40" },
  cancelled: { label: "已取消", cls: "text-muted-foreground border-border" },
}

function StatusPill({ status }: { status: string }) {
  const s = MSTATUS[status] ?? { label: status, cls: "text-muted-foreground border-border" }
  return <span className={cn("shrink-0 rounded-full border px-2 py-px text-[10px]", s.cls)}>{s.label}</span>
}

export function TeamRunReport({ pid, teamId, onClose, onOpenSession }: {
  pid: string
  teamId: string
  onClose: () => void
  /** 「打开执行会话」→ 切到该会话页签（LiveRoom 注入） */
  onOpenSession?: (sessionId: string) => void
}) {
  const [team, setTeam] = useState<Team | null>(null)
  const [runs, setRuns] = useState<TeamRun[]>([])
  const [selRunId, setSelRunId] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => Promise.all([api.team(pid, teamId), api.teamRuns(pid, teamId)])
      .then(([t, rs]) => { if (alive) { setTeam(t); setRuns(rs) } })
      .catch((e) => { if (alive) setErr(e instanceof Error ? e.message : String(e)) })
    load()
    const timer = setInterval(load, 3000)
    return () => { alive = false; clearInterval(timer) }
  }, [pid, teamId])

  const run = useMemo(
    () => runs.find((r) => r.id === selRunId) ?? runs[0] ?? null,
    [runs, selRunId])

  // 状态计数（按当前 Run 成员）
  const counts = useMemo(() => {
    const c: Record<string, number> = { completed: 0, running: 0, pending: 0, failed: 0 }
    for (const m of run?.members ?? []) {
      if (m.status === "completed") c.completed++
      else if (m.status === "running" || m.status === "creating") c.running++
      else if (m.status === "failed") c.failed++
      else c.pending++
    }
    return c
  }, [run])

  const memberLabel = (rm: TeamRunMember) =>
    team?.members.find((m) => m.id === rm.member_id)?.label || rm.member_key

  return (
    <div className="absolute inset-0 z-40 flex flex-col bg-background">
      {/* 顶栏 */}
      <div className="flex shrink-0 flex-wrap items-center gap-3 border-b px-4 py-2.5">
        <button type="button" onClick={onClose}
          className="flex shrink-0 items-center gap-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground">
          <ArrowLeft className="size-3.5" /> 返回会话
        </button>
        <span className="text-[10px] uppercase tracking-wide text-muted-foreground">Agent Teams · 运行报告</span>
        <span className="min-w-0 truncate font-mono text-sm font-semibold text-foreground/90">{team?.name ?? teamId}</span>
        {team && <span className="shrink-0 rounded-full border border-(--status-approval)/50 px-2 py-px text-[10px] text-(--status-approval)">{team.status}</span>}
        <span className="flex-1" />
        <div className="flex shrink-0 items-center gap-3 text-[11px] text-muted-foreground">
          <span>完成 <b className="text-(--status-ok)">{counts.completed}</b></span>
          <span>进行中 <b className="text-primary">{counts.running}</b></span>
          <span>待启动 <b className="text-foreground/70">{counts.pending}</b></span>
          <span>失败 <b className="text-(--status-error)">{counts.failed}</b></span>
        </div>
        {runs.length > 1 && (
          <select value={run?.id ?? ""} onChange={(e) => setSelRunId(e.target.value)}
            className="h-7 shrink-0 rounded border bg-background px-1 text-[11px]">
            {runs.map((r, i) => (
              <option key={r.id} value={r.id}>{`Run ${runs.length - i} · ${r.status}`}</option>
            ))}
          </select>
        )}
        <span className="shrink-0 text-[10px] text-muted-foreground/60">
          <RefreshCw className="mr-1 inline size-3 animate-spin" style={{ animationDuration: "3s" }} />3s
        </span>
      </div>

      {err && <p className="shrink-0 px-4 py-1 text-[11px] text-(--status-error)">{err}</p>}

      <div className="min-h-0 flex-1 space-y-4 overflow-auto px-4 py-3">
        {/* 编队 */}
        <section>
          <h3 className="mb-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Users className="size-3.5" /> 编队 · {team?.members.length ?? 0} 名成员
          </h3>
          <div className="flex flex-wrap gap-2">
            {(team?.members ?? []).map((m) => (
              <div key={m.id} className="w-64 rounded-lg border bg-card/60 p-2.5">
                <div className="flex items-center gap-2">
                  <span className="grid size-7 shrink-0 place-items-center rounded-md bg-accent/40 text-muted-foreground">
                    <Users className="size-3.5" />
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-foreground/90" title={m.label}>
                    {m.label || m.member_key}
                  </span>
                  <StatusPill status={m.status} />
                </div>
                <p className="mt-1.5 line-clamp-3 text-[11px] leading-relaxed text-muted-foreground" title={m.responsibility}>
                  {m.responsibility || "（未填写职责）"}
                </p>
                <p className="mt-1 truncate font-mono text-[10px] text-muted-foreground/60">
                  {[m.role, m.model, m.runtime].filter(Boolean).join(" · ") || "—"}
                </p>
              </div>
            ))}
            {!team?.members.length && <p className="text-xs text-muted-foreground">暂无成员</p>}
          </div>
        </section>

        {/* Run 成员执行明细 */}
        <section>
          <h3 className="mb-1.5 text-xs text-muted-foreground">
            执行明细{run ? ` · ${run.status} · ${fmtDateTimeMin(run.started_at ?? run.created_at)}` : ""}
          </h3>
          {run ? (
            <div className="space-y-1.5">
              {run.members.map((rm) => (
                <div key={rm.id} className="rounded-lg border bg-card/50 p-2.5">
                  <div className="flex items-center gap-2">
                    <span className="min-w-0 truncate text-[12.5px] font-medium text-foreground/90">{memberLabel(rm)}</span>
                    <span className="shrink-0 rounded border border-border px-1 font-mono text-[10px] text-muted-foreground/70">{rm.role || "—"}</span>
                    <StatusPill status={rm.status} />
                    <span className="flex-1" />
                    {rm.session_id && (
                      <button type="button" onClick={() => onOpenSession?.(rm.session_id!)}
                        className="shrink-0 rounded border border-border/70 px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground">
                        打开执行会话
                      </button>
                    )}
                  </div>
                  <p className="mt-1 text-[11px] leading-relaxed text-foreground/75">{rm.objective || "（无 objective）"}</p>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-muted-foreground/60">
                    <span>{[rm.provider, rm.model].filter(Boolean).join("/") || "默认模型"}</span>
                    {rm.runtime && <span>runtime={rm.runtime}</span>}
                    {rm.threat_class && <span>threat={rm.threat_class}</span>}
                    {rm.max_steps != null && <span>max_steps={rm.max_steps}</span>}
                    {rm.execution_id && <span>exec={rm.execution_id}</span>}
                  </div>
                  {rm.error && <p className="mt-1 text-[11px] text-(--status-error)">⛔ {rm.error}</p>}
                </div>
              ))}
              {!run.members.length && <p className="text-xs text-muted-foreground">该 Run 暂无成员</p>}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">尚未启动任何 Run。在团队卡「查看并配置」里确认并启动。</p>
          )}
        </section>
      </div>
    </div>
  )
}
