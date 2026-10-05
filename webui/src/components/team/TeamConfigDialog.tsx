import { useCallback, useEffect, useState } from "react"
import { Plus, Trash2, X } from "lucide-react"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import type { Team, TeamMemberInput, TeamPreflight } from "@/lib/types"

// 「查看并配置」团队弹窗（2026-10-06，Phase D）：对齐 cc-haha AgentTeamsPlanCard 的编辑弹窗。
// 展示 preflight（成员/职责/模型/运行时/安全边界/容量/blockers），可编辑 roster（PATCH 整体替换，
// revision 乐观锁由后端校验），确认四项后启动（POST start，事务创建 Run 快照并 fan-out 专属会话）。
// 仅 draft/ready 可编辑/启动（后端 409 约束）。

const RUNTIMES = ["", "host", "wsl", "docker", "sandbox"]
const THREATS = ["trusted", "untrusted", "malware_live", "unknown"]

const FIELD = "h-7 w-full rounded border bg-background px-1.5 text-[11px]"

export function TeamConfigDialog({ pid, teamId, onClose, onStarted }: {
  pid: string
  teamId: string
  onClose: () => void
  /** 启动成功回调（run_id），供父级刷新/切到运行报告 */
  onStarted?: (runId: string) => void
}) {
  const [team, setTeam] = useState<Team | null>(null)
  const [pf, setPf] = useState<TeamPreflight | null>(null)
  const [name, setName] = useState("")
  const [goal, setGoal] = useState("")
  const [members, setMembers] = useState<TeamMemberInput[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [confirm, setConfirm] = useState({ members: false, goal: false, safety: false, execution: false })

  const load = useCallback(async () => {
    const [t, p] = await Promise.all([api.team(pid, teamId), api.teamPreflight(pid, teamId)])
    setTeam(t); setPf(p)
    setName(t.name); setGoal(t.goal_text)
    setMembers(t.members.map((m) => ({
      member_key: m.member_key, label: m.label, responsibility: m.responsibility,
      role: m.role, provider: m.provider, model: m.model,
      runtime: m.runtime, threat_class: m.threat_class, max_steps: m.max_steps,
    })))
  }, [pid, teamId])

  useEffect(() => { load().catch((e) => setErr(e instanceof Error ? e.message : String(e))) }, [load])

  const editable = team?.status === "draft" || team?.status === "ready"
  const patchMember = (i: number, patch: Partial<TeamMemberInput>) =>
    setMembers((cur) => cur.map((m, idx) => idx === i ? { ...m, ...patch } : m))
  const addMember = () =>
    setMembers((cur) => [...cur, {
      member_key: `member-${cur.length + 1}`, label: "", responsibility: "",
      role: "", provider: "", model: "", runtime: "", threat_class: "trusted", max_steps: null,
    }])
  const removeMember = (i: number) => setMembers((cur) => cur.filter((_, idx) => idx !== i))

  const save = async () => {
    setBusy(true); setErr(null); setNote(null)
    try {
      await api.patchTeam(pid, teamId, { name: name.trim(), goal_text: goal.trim(), members })
      await load()
      setNote("已保存（重新预检，启动前请再次确认）")
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  const start = async () => {
    if (!pf) return
    setBusy(true); setErr(null); setNote(null)
    try {
      const r = await api.startTeam(pid, teamId, { preflight_revision: pf.revision, confirmations: confirm })
      onStarted?.(r.run.id)
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }

  const allConfirmed = confirm.members && confirm.goal && confirm.safety && confirm.execution

  return (
    <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div className="flex max-h-[85dvh] w-[min(880px,94vw)] flex-col rounded-xl border bg-popover text-xs shadow-2xl"
        onClick={(e) => e.stopPropagation()}>
        {/* 头部 */}
        <div className="flex shrink-0 items-center gap-2 border-b px-4 py-2.5">
          <span className="font-medium">🪟 团队配置</span>
          {team && <span className="rounded bg-primary/15 px-1.5 py-px text-[10px] text-primary">{team.status}</span>}
          <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground">{teamId}</span>
          <button type="button" onClick={onClose} title="关闭"
            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">
            <X className="size-3.5" />
          </button>
        </div>

        {/* 主体（滚动） */}
        <div className="min-h-0 flex-1 space-y-3 overflow-auto px-4 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex min-w-56 flex-1 flex-col gap-1">
              <span className="text-muted-foreground">团队名</span>
              <input value={name} disabled={!editable} onChange={(e) => setName(e.target.value)}
                className="h-7 rounded border bg-background px-2 disabled:opacity-60" />
            </label>
            <label className="flex min-w-56 flex-1 flex-col gap-1">
              <span className="text-muted-foreground">共享目标（goal）</span>
              <input value={goal} disabled={!editable} onChange={(e) => setGoal(e.target.value)}
                placeholder="团队共同要达成什么"
                className="h-7 rounded border bg-background px-2 disabled:opacity-60" />
            </label>
          </div>

          {/* 成员 roster */}
          <div className="rounded-lg border">
            <div className="flex items-center gap-2 border-b px-2.5 py-1.5">
              <b>成员 · {members.length}</b>
              <span className="text-[10px] text-muted-foreground">每行一位成员；key 为名册主键，勿重复</span>
              <span className="flex-1" />
              <Button size="sm" variant="outline" className="h-6 gap-1 text-[11px]"
                disabled={!editable || busy} onClick={addMember}>
                <Plus className="size-3" /> 添加成员
              </Button>
            </div>
            <div className="space-y-1.5 p-2">
              {members.map((m, i) => (
                <div key={i} className="rounded border bg-background/40 p-2">
                  <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
                    <label className="flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">名册 key</span>
                      <input className={FIELD} value={m.member_key} disabled={!editable}
                        onChange={(e) => patchMember(i, { member_key: e.target.value })} /></label>
                    <label className="flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">显示名</span>
                      <input className={FIELD} value={m.label ?? ""} disabled={!editable}
                        onChange={(e) => patchMember(i, { label: e.target.value })} /></label>
                    <label className="flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">角色</span>
                      <input className={FIELD} value={m.role ?? ""} disabled={!editable}
                        onChange={(e) => patchMember(i, { role: e.target.value })} /></label>
                    <label className="flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">模型</span>
                      <input className={FIELD} value={m.model ?? ""} disabled={!editable}
                        onChange={(e) => patchMember(i, { model: e.target.value })} /></label>
                    <label className="flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">runtime</span>
                      <select className={FIELD} value={m.runtime ?? ""} disabled={!editable}
                        onChange={(e) => patchMember(i, { runtime: e.target.value })}>
                        {RUNTIMES.map((r) => <option key={r} value={r}>{r || "（默认）"}</option>)}
                      </select></label>
                    <label className="flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">threat_class</span>
                      <select className={FIELD} value={m.threat_class ?? "trusted"} disabled={!editable}
                        onChange={(e) => patchMember(i, { threat_class: e.target.value })}>
                        {THREATS.map((t) => <option key={t} value={t}>{t}</option>)}
                      </select></label>
                    <label className="flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">max_steps</span>
                      <input className={FIELD} type="number" min={1} value={m.max_steps ?? ""} disabled={!editable}
                        onChange={(e) => patchMember(i, { max_steps: e.target.value ? Number(e.target.value) : null })} /></label>
                    <div className="flex items-end justify-end">
                      <button type="button" disabled={!editable || busy} title="移除该成员"
                        onClick={() => removeMember(i)}
                        className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-(--status-error) disabled:opacity-40">
                        <Trash2 className="size-3.5" />
                      </button>
                    </div>
                  </div>
                  <label className="mt-1.5 flex flex-col gap-0.5"><span className="text-[10px] text-muted-foreground">职责 / objective</span>
                    <textarea rows={2} value={m.responsibility ?? ""} disabled={!editable}
                      onChange={(e) => patchMember(i, { responsibility: e.target.value })}
                      className="w-full resize-y rounded border bg-background px-1.5 py-1 text-[11px] leading-relaxed disabled:opacity-60" /></label>
                </div>
              ))}
              {!members.length && <p className="py-2 text-center text-muted-foreground">暂无成员</p>}
            </div>
          </div>

          {/* preflight：安全边界 / 容量 / 阻塞 */}
          {pf && (
            <div className="space-y-1 rounded-lg border bg-background/40 p-2.5">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
                <span>场景轨 <b className="text-foreground/80">{pf.safety.track || "—"}</b></span>
                <span>会话容量 <b className="text-foreground/80">{pf.capacity.running}+{pf.capacity.requested} / {pf.capacity.cap}</b></span>
                <span>revision <span className="font-mono">{pf.revision.slice(0, 12)}</span></span>
              </div>
              {pf.blockers.length > 0 ? (
                <ul className="space-y-0.5 text-[11px] text-(--status-error)">
                  {pf.blockers.map((b, i) => <li key={i}>⛔ {b.message}</li>)}
                </ul>
              ) : (
                <p className="text-[11px] text-(--status-ok)">✓ 执行前检查通过</p>
              )}
            </div>
          )}
        </div>

        {/* 底部：确认 + 动作 */}
        <div className="shrink-0 space-y-2 border-t px-4 py-2.5">
          {editable && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
              {([["members", "已确认成员名册"], ["goal", "已确认共享目标"], ["safety", "已确认安全边界"], ["execution", "确认执行"]] as const)
                .map(([k, label]) => (
                  <label key={k} className="flex cursor-pointer items-center gap-1.5 text-muted-foreground">
                    <input type="checkbox" checked={confirm[k]} disabled={busy}
                      onChange={(e) => setConfirm((c) => ({ ...c, [k]: e.target.checked }))} />
                    {label}
                  </label>
                ))}
            </div>
          )}
          {err && <p className="text-[11px] text-(--status-error)">{err}</p>}
          {note && <p className="text-[11px] text-primary">{note}</p>}
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" onClick={onClose}>关闭</Button>
            {editable && (
              <>
                <Button size="sm" variant="outline" disabled={busy} onClick={() => void save()}>
                  {busy ? "处理中…" : "保存名册"}
                </Button>
                <span className="flex-1" />
                <Button size="sm" disabled={busy || !allConfirmed || !pf || pf.blockers.length > 0}
                  title={!allConfirmed ? "请先勾选四项确认" : pf?.blockers.length ? "存在阻塞项，无法启动" : "事务创建 Run 快照并 fan-out 专属会话"}
                  onClick={() => void start()}>
                  {busy ? "启动中…" : "确认并启动"}
                </Button>
              </>
            )}
            {!editable && <span className="flex-1" />}
          </div>
        </div>
      </div>
    </div>
  )
}
