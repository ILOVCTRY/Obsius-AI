import { useState } from "react"
import { api } from "@/lib/api"
import type { Approval, DecideApprovalResult } from "@/lib/types"
import { cn } from "@/lib/utils"
import { fmtDateTimeMin } from "@/lib/datetime"

// 对话内审批卡（webui-trae-shell M2，拍板 #7）：铃铛管全局未读，pending 审批
// 按归属会话 + created_at 时间位注入对话流，批准/拒绝就地裁决（无独立页跳转）。
// 后端不发 approval.requested 事件——数据靠 api.approvals(pid,"pending") 轮询；
// 裁决后 approval.approved/rejected 审计行走正常事件流，卡片在 pending 刷新后卸载，
// 卸载前在卡内显示处理器回执（自动建窗/一次性执行结果）。

const RISK_COLOR: Record<string, string> = {
  high: "text-(--status-error)",
  medium: "text-(--status-approval)",
  low: "text-muted-foreground",
}

type SpawnAction = { op: "spawn_session"; role: string; reason?: string; task_id?: string }
type EscAction = {
  op: "escalation"; kind?: string; cmd: string; runtime: string
  threat_class?: string; net?: string; reason: string
}

function asSpawn(a: Approval): SpawnAction | null {
  const o = a.action
  if (o && o.op === "spawn_session" && typeof o.role === "string") {
    return {
      op: "spawn_session", role: o.role,
      reason: typeof o.reason === "string" ? o.reason : "",
      task_id: typeof o.task_id === "string" ? o.task_id : undefined,
    }
  }
  return null
}

function asEsc(a: Approval): EscAction | null {
  const o = a.action
  if (o && o.op === "escalation" && typeof o.cmd === "string" && typeof o.runtime === "string") {
    return {
      op: "escalation",
      kind: typeof o.kind === "string" ? o.kind : "escalation",
      cmd: o.cmd, runtime: o.runtime,
      threat_class: typeof o.threat_class === "string" ? o.threat_class : undefined,
      net: typeof o.net === "string" ? o.net : undefined,
      reason: typeof o.reason === "string" ? o.reason : "",
    }
  }
  return null
}

/** 裁决后的处理器回执（与 ApprovalsView execNote 同口径，紧凑版） */
function Receipt({ r, esc }: { r: DecideApprovalResult; esc: EscAction | null }) {
  if (r.status === "rejected") {
    return <p className="mt-1 text-[11px] text-muted-foreground">已拒绝（拒绝回流至请求方）</p>
  }
  if (r.executed === false) {
    return (
      <p className="mt-1 rounded border border-(--status-error)/50 bg-(--status-error)/10 p-1.5 text-[11px] text-(--status-error)">
        已批准，但{esc ? "执行失败" : "自动建窗失败"}：{r.error}
      </p>
    )
  }
  if (r.executed === true) {
    if (esc) {
      return <p className="mt-1 text-[11px] text-primary">
        已执行一次（exit={r.exit_code ?? "?"}），回执已投递
      </p>
    }
    // 赛跑终检：任务审批等待期已被处理（spawn 处理器返回 session_id=null+skipped）
    if (!r.session_id) {
      return <p className="mt-1 text-[11px] text-muted-foreground">
        未建窗：{r.skipped || "目标已不处于待执行状态"}
      </p>
    }
    return (
      <p className="mt-1 text-[11px] text-primary">
        已自动建窗（{r.session_id.slice(0, 12)}）并提交跑队列
      </p>
    )
  }
  return <p className="mt-1 text-[11px] text-primary">已批准</p>
}

/** 已裁决审批的紧凑审计 chip（重载/回执过期后接档；审批行为据，无后端事件依赖） */
export function ApprovalAuditChip({ approval, roleNames }: {
  approval: Approval
  roleNames: Record<string, string>
}) {
  const approved = approval.status === "approved" || approval.status === "consumed"
  const spawn = asSpawn(approval)
  const esc = asEsc(approval)
  const label = spawn
    ? `🪟 开窗 ${spawn.role}${roleNames[spawn.role] ? "·" + roleNames[spawn.role] : ""}`
    : esc ? "🛫 越界执行" : `审批 ${String(approval.action?.op ?? "")}`
  return (
    <div className="flex justify-start pl-1">
      <div className="inline-flex max-w-full items-center gap-1 rounded-full border border-muted-foreground/30 bg-accent/20 px-2.5 py-0.5 text-[10px] text-muted-foreground">
        <span>{approved ? "✅ 已批准" : "❌ 已拒绝"}</span>
        <span className="truncate">{label}</span>
        <span className="font-mono">{fmtDateTimeMin(approval.decided_at || approval.created_at)}</span>
        {approval.decided_by && <span className="font-mono opacity-80">· {approval.decided_by}</span>}
      </div>
    </div>
  )
}

export function ApprovalCard({ approval, roleNames, onDecided }: {
  approval: Approval
  roleNames: Record<string, string>
  /** 裁决成功回调：pending 刷新会卸载本卡，父窗据此把卡留住在回执态 */
  onDecided?: (approval: Approval) => void
}) {
  const [decided, setDecided] = useState<DecideApprovalResult | null>(null)
  const [busy, setBusy] = useState<"approved" | "rejected" | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const decide = async (decision: "approved" | "rejected") => {
    setErr(null); setBusy(decision)
    try {
      const r = await api.decideApproval(approval.id, decision)
      setDecided(r)
      onDecided?.(approval)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(null)
    }
  }

  const spawn = asSpawn(approval)
  const esc = asEsc(approval)
  return (
    <div className="flex justify-start pl-1">
      <div className="w-full max-w-[94%] rounded-2xl rounded-bl-sm border border-(--status-approval)/40 bg-accent/30 px-3 py-2">
        <p className="mb-1 text-[10px] text-muted-foreground">
          <span className={cn("font-mono", RISK_COLOR[approval.risk])}>风险 {approval.risk}</span>
          <span className="ml-2 font-mono">{approval.requested_by}</span>
          <span className="ml-2">{fmtDateTimeMin(approval.created_at)}</span>
        </p>

        {spawn ? (
          <div className="space-y-1 text-xs">
            <p className="font-medium">🪟 申请开新会话窗 ·{" "}
              <span className="font-mono text-muted-foreground">{spawn.role}</span>
              {roleNames[spawn.role] && <span className="ml-1 text-muted-foreground">{roleNames[spawn.role]}</span>}
            </p>
            {spawn.task_id && (
              <p className="text-[11px] text-muted-foreground">任务 {spawn.task_id.slice(0, 18)}</p>
            )}
            <p className="rounded bg-card p-1.5 text-[11px] leading-relaxed">
              {spawn.reason || "（未填写理由，建议拒绝）"}
            </p>
          </div>
        ) : esc ? (
          <div className="space-y-1 text-xs">
            <p className="font-medium">🛫 一次性越界执行申请
              {esc.net && <span className="ml-1 text-[10px] text-(--status-error)">net={esc.net}</span>}
            </p>
            <pre className="max-h-32 overflow-auto rounded bg-card p-1.5 font-mono text-[10px] whitespace-pre-wrap">
              {esc.cmd}
            </pre>
            <p className="font-mono text-[10px] text-muted-foreground">
              {esc.runtime}
              {esc.threat_class && ` · threat_class=${esc.threat_class}`}
            </p>
            <p className="rounded bg-card p-1.5 text-[11px] leading-relaxed">
              {esc.reason || "（未填写理由，建议拒绝）"}
            </p>
          </div>
        ) : (
          <pre className="max-h-32 overflow-auto rounded bg-card p-1.5 font-mono text-[10px]">
            {JSON.stringify(approval.action, null, 2)}
          </pre>
        )}

        {approval.boundary && (
          <p className="mt-1 rounded border border-(--status-approval)/30 bg-(--status-approval)/5 p-1.5 text-[10px] text-(--status-approval)">
            行动边界：{approval.boundary}
          </p>
        )}
        {err && <p className="mt-1 text-[11px] text-(--status-error)">{err}</p>}
        {decided
          ? <Receipt r={decided} esc={esc} />
          : (
            <div className="mt-1.5 flex justify-end gap-1.5">
              <button type="button" disabled={busy !== null}
                onClick={() => decide("rejected")}
                className="rounded-full border px-2.5 py-0.5 text-[11px] text-muted-foreground hover:bg-accent disabled:opacity-50">
                {busy === "rejected" ? "处理中…" : "拒绝"}
              </button>
              <button type="button" disabled={busy !== null}
                onClick={() => decide("approved")}
                className="rounded-full bg-primary px-2.5 py-0.5 text-[11px] text-primary-foreground hover:opacity-90 disabled:opacity-50">
                {busy === "approved" ? "处理中…" : "批准"}
              </button>
            </div>
          )}
      </div>
    </div>
  )
}
