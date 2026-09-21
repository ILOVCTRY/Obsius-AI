import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { Approval, DecideApprovalResult, RoleInfo, SpawnSessionAction, Task } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { fmtDateTime, utcTitle } from "@/lib/datetime"

// 审批收件箱（DESIGN.md §12 一等公民）：请求者/动作/风险 → 批准/拒绝，全程落审计
// 批 4：spawn_session（编排 L1 开窗申请）渲染专属卡，其余 action 维持通用 JSON 卡

const RISK_COLOR: Record<string, string> = {
  high: "text-(--status-error)",
  medium: "text-(--status-approval)",
  low: "text-muted-foreground",
}

function asSpawnAction(a: Approval): SpawnSessionAction | null {
  const o = a.action as Record<string, unknown>
  if (o && o.op === "spawn_session" && typeof o.role === "string") {
    return {
      op: "spawn_session", role: o.role,
      reason: typeof o.reason === "string" ? o.reason : "",
    }
  }
  return null
}

// H3：request_escalation（一次性越界执行申请）专属卡数据
type EscalationAction = {
  kind: string
  cmd: string
  runtime: string
  threat_class?: string
  net?: string
  reason: string
}

function asEscalationAction(a: Approval): EscalationAction | null {
  const o = a.action as Record<string, unknown>
  if (o && o.op === "escalation" && typeof o.cmd === "string" && typeof o.runtime === "string") {
    return {
      kind: typeof o.kind === "string" ? o.kind : "escalation",
      cmd: o.cmd, runtime: o.runtime,
      threat_class: typeof o.threat_class === "string" ? o.threat_class : undefined,
      net: typeof o.net === "string" ? o.net : undefined,
      reason: typeof o.reason === "string" ? o.reason : "",
    }
  }
  return null
}

export function ApprovalsView({ pid, onGotoTasks }: { pid: string; onGotoTasks?: () => void }) {
  const [items, setItems] = useState<Approval[]>([])
  const [roles, setRoles] = useState<Record<string, RoleInfo>>({})
  const [results, setResults] = useState<Record<string, DecideApprovalResult>>({})
  const [blocked, setBlocked] = useState<Task[]>([])
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api.approvals(pid).then(setItems).catch((e) => setError(String(e)))
    // C1：awaiting_human 任务计入顶栏铃铛红点，但不是 approval 行——在此一并列出，
    // 让徽章数与页面内容对得上（处理动作在任务看板：✅ 放回继续 / 取消）
    api.tasks(pid)
      .then((ts) => setBlocked(ts.filter(
        (t) => t.status === "failed" && t.blocked_reason === "awaiting_human")))
      .catch(() => {})
  }, [pid])
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  // 角色名/描述只用于展示；拉不到就退回 role slug
  useEffect(() => {
    api.listRoles(pid)
      .then((rs) => setRoles(Object.fromEntries(rs.map((r) => [r.role, r]))))
      .catch(() => {})
  }, [pid])

  const decide = async (aid: string, decision: "approved" | "rejected") => {
    setError(null)
    try {
      const r = await api.decideApproval(aid, decision)
      setResults((prev) => ({ ...prev, [aid]: r }))
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  // 批准后处理器回执：失败（executed:false）不回滚批准，需显式告知并引导手动处理
  const execNote = (a: Approval) => {
    const r = results[a.id]
    if (!r || r.status !== "approved") return null
    const esc = asEscalationAction(a)
    if (r.executed === false) {
      return (
        <p className="mt-2 rounded border border-(--status-error)/50 bg-(--status-error)/10 p-2 text-[11px] text-(--status-error)">
          {esc ? "已批准，但执行失败（审批保持「已批准」，落 approval.exec_failed 事件）：" : "已批准，但自动建窗失败："}
          {r.error}
          {!esc && (<><br />可在会话手动开 {asSpawnAction(a)?.role} 角色窗。</>)}
        </p>
      )
    }
    if (r.executed === true) {
      if (esc) {
        return (
          <p className="mt-1 text-[11px] text-primary">
            已执行一次（exit={r.exit_code ?? "?"}），结果已投递回请求会话收件箱（🛫 回执）
          </p>
        )
      }
      return (
        <p className="mt-1 text-[11px] text-primary">
          已自动建窗（{r.session_id?.slice(0, 12)}）并提交跑队列
        </p>
      )
    }
    return null
  }

  const pending = items.filter((a) => a.status === "pending")
  const decided = items.filter((a) => a.status !== "pending")

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <div>
        <h1 className="text-lg font-semibold">审批收件箱</h1>
        <p className="text-sm text-muted-foreground">
          编排开窗申请（L1）/ 越界审批 / net:real / cross_target 等待批动作；批准与拒绝均落事件流审计
        </p>
      </div>
      {error && <p className="text-sm text-(--status-error)">{error}</p>}

      <div className="space-y-2">
        {pending.length === 0 && blocked.length === 0 && (
          <p className="py-8 text-center text-sm text-muted-foreground">暂无待审批动作</p>
        )}
        {blocked.map((t) => (
          <div key={t.id} className="rounded-lg border border-(--status-approval)/40 p-3">
            <div className="flex items-center gap-2">
              <Badge variant="outline" className="font-mono text-[10px] text-(--status-approval)">
                ⏸ 待人工
              </Badge>
              <span className="min-w-0 flex-1 truncate text-sm font-medium">{t.objective}</span>
              <span className="font-mono text-[10px] text-muted-foreground" title={utcTitle(t.created_at)}>
                {fmtDateTime(t.created_at)}
              </span>
            </div>
            {t.result_note && (
              <p className="mt-2 rounded bg-card p-2 text-xs">{t.result_note}</p>
            )}
            <div className="mt-2 flex items-center justify-end gap-2">
              <span className="text-[11px] text-muted-foreground">
                任务挂起等待人工处置（非审批单，计入顶栏 🔔 数）
              </span>
              {onGotoTasks && (
                <Button size="sm" variant="outline" onClick={onGotoTasks}>去任务看板处理</Button>
              )}
            </div>
          </div>
        ))}
        {pending.map((a) => {
          const spawn = asSpawnAction(a)
          const esc = asEscalationAction(a)
          const ri = spawn ? roles[spawn.role] : undefined
          return (
            <div key={a.id} className="rounded-lg border border-(--status-approval)/40 p-3">
              <div className="flex items-center gap-2">
                <Badge variant="outline" className={cn("font-mono text-[10px]", RISK_COLOR[a.risk])}>
                  风险 {a.risk}
                </Badge>
                <span className="font-mono text-xs text-muted-foreground">{a.requested_by}</span>
                <span className="flex-1" />
                <span className="font-mono text-[10px] text-muted-foreground" title={utcTitle(a.created_at)}>
                  {fmtDateTime(a.created_at)}
                </span>
              </div>
              {spawn ? (
                <div className="mt-2 space-y-2">
                  <p className="text-sm font-medium">🪟 编排申请开新会话窗</p>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="font-mono text-xs">{spawn.role}</Badge>
                    {ri?.name && <span className="text-sm">{ri.name}</span>}
                  </div>
                  {ri?.description && (
                    <p className="text-xs text-muted-foreground">{ri.description}</p>
                  )}
                  <div>
                    <p className="text-[11px] text-muted-foreground">开窗理由（审批人只看得到角色与理由）</p>
                    <p className="mt-1 rounded bg-card p-2 text-xs">
                      {spawn.reason || "（编排未填写理由，建议拒绝）"}
                    </p>
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    批准后系统自动建窗并立即提交跑队列；拒绝不动作，编排下一轮经事件可见后改道。
                  </p>
                </div>
              ) : esc ? (
                <div className="mt-2 space-y-2">
                  <p className="text-sm font-medium">
                    🛫 一次性越界执行申请
                    {esc.kind === "net_real" && (
                      <Badge variant="outline" className="ml-2 font-mono text-[10px] text-(--status-error)">
                        net=real 真实网络
                      </Badge>
                    )}
                    {esc.kind === "role_runtime" && (
                      <Badge variant="outline" className="ml-2 font-mono text-[10px] text-(--status-approval)">
                        超角色 runtime 上限
                      </Badge>
                    )}
                  </p>
                  <pre className="overflow-auto rounded bg-card p-2 font-mono text-[11px] whitespace-pre-wrap">
                    {esc.cmd}
                  </pre>
                  <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-muted-foreground">
                    <Badge variant="outline" className="font-mono text-[10px]">{esc.runtime}</Badge>
                    {esc.threat_class && <span>threat_class={esc.threat_class}</span>}
                    {esc.net && <span>net={esc.net}</span>}
                  </div>
                  <div>
                    <p className="text-[11px] text-muted-foreground">升级理由</p>
                    <p className="mt-1 rounded bg-card p-2 text-xs">
                      {esc.reason || "（未填写理由，建议拒绝）"}
                    </p>
                  </div>
                  <p className="text-[11px] text-muted-foreground">
                    批准后命令只执行一次（同单不可复用），结果投递回请求会话收件箱；拒绝不动作。
                  </p>
                </div>
              ) : (
                <pre className="mt-2 overflow-auto rounded bg-card p-2 font-mono text-[11px]">
                  {JSON.stringify(a.action, null, 2)}
                </pre>
              )}
              {execNote(a)}
              <div className="mt-2 flex justify-end gap-2">
                <Button size="sm" variant="outline" onClick={() => decide(a.id, "rejected")}>拒绝</Button>
                <Button size="sm" onClick={() => decide(a.id, "approved")}>批准</Button>
              </div>
            </div>
          )
        })}
      </div>

      {decided.length > 0 && (
        <>
          <h2 className="pt-2 text-sm font-medium text-muted-foreground">已处理</h2>
          <div className="space-y-1.5">
            {decided.map((a) => {
              const spawn = asSpawnAction(a)
              const esc = asEscalationAction(a)
              const ri = spawn ? roles[spawn.role] : undefined
              return (
                <div key={a.id} className="rounded border px-3 py-2 text-xs">
                  <div className="flex items-center gap-2">
                    <Badge variant="outline"
                           className={cn("font-mono text-[10px]",
                             a.status === "approved" || a.status === "consumed" ? "text-primary" : "text-(--status-error)")}>
                      {a.status === "approved" ? "已批准" : a.status === "consumed" ? "已执行" : "已拒绝"}
                    </Badge>
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
                      {spawn
                        ? `🪟 开窗 ${ri?.name ? `${ri.name}（${spawn.role}）` : spawn.role}`
                        : esc
                          ? `🛫 越界执行（${esc.runtime}）${esc.cmd}`
                          : JSON.stringify(a.action)}
                    </span>
                    <span className="font-mono text-[10px] text-muted-foreground">{a.decided_by}</span>
                  </div>
                  {execNote(a)}
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
