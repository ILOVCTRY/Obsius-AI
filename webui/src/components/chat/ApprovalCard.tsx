import { useState } from "react"
import { HelpCircle, MessageSquare } from "lucide-react"
import type { Approval, DecideApprovalResult, SpawnSessionAction } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

// 对话内联「审批问答卡」（2026-10-04）：审批模块下线后，需要审批时直接在对话页弹此卡。
// 形态参考 Claude Code 的问答卡：图标 + 标题 / 说明 / 可点选选项（单选）/ 附加说明 / 提交。
// 卡片渲染所需的完整 action/boundary 由 `usePendingApprovals` 的列表端点提供（事件载荷字段不足）。
// op 专属文案（spawn_session）自原 ApprovalsView 迁入，勿重写。

const RISK_CLS: Record<string, string> = {
  high: "border-(--status-error)/50 text-(--status-error)",
  medium: "border-(--status-approval)/50 text-(--status-approval)",
  low: "border-border text-muted-foreground",
}

export function asSpawnAction(a: Approval): SpawnSessionAction | null {
  const o = a.action as Record<string, unknown>
  if (o && o.op === "spawn_session" && typeof o.role === "string") {
    return {
      op: "spawn_session", role: o.role,
      reason: typeof o.reason === "string" ? o.reason : "",
    }
  }
  return null
}

/** 批准后处理器回执：失败（executed:false）不回滚批准，需显式告知并引导手动处理 */
function ExecNote({ approval, result }: { approval: Approval; result: DecideApprovalResult }) {
  if (result.status !== "approved") return null
  if (result.executed === false) {
    return (
      <p className="mt-2 rounded border border-(--status-error)/50 bg-(--status-error)/10 p-2 text-[11px] text-(--status-error)">
        已批准，但自动建窗失败：{result.error}<br />
        可在会话手动开 {asSpawnAction(approval)?.role} 角色窗。
      </p>
    )
  }
  if (result.executed === true) {
    return <p className="mt-2 text-[11px] text-primary">已自动建窗（{result.session_id?.slice(0, 12)}）并提交跑队列</p>
  }
  return null
}

function Choice({ selected, onClick, title, desc, tone, disabled }: {
  selected: boolean
  onClick: () => void
  title: string
  desc: string
  tone: "ok" | "danger"
  disabled: boolean
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex w-full items-start gap-2.5 rounded-md border px-3 py-2 text-left transition-colors disabled:opacity-60",
        selected
          ? tone === "ok"
            ? "border-(--status-ok)/60 bg-(--status-ok)/10"
            : "border-(--status-error)/60 bg-(--status-error)/10"
          : "border-border hover:bg-accent/40",
      )}
    >
      <span className={cn(
        "mt-0.5 grid size-3.5 shrink-0 place-items-center rounded-full border",
        selected
          ? tone === "ok" ? "border-(--status-ok)" : "border-(--status-error)"
          : "border-muted-foreground/50",
      )}>
        {selected && (
          <span className={cn("size-1.5 rounded-full", tone === "ok" ? "bg-(--status-ok)" : "bg-(--status-error)")} />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-xs font-medium text-foreground">{title}</span>
        <span className="mt-0.5 block text-[11px] leading-relaxed text-muted-foreground">{desc}</span>
      </span>
    </button>
  )
}

export function ApprovalCard({ approval, roleName, onDecide, showChat = true }: {
  approval: Approval
  /** spawn_session 的角色显示名（LiveRoom 的 roleNames 反查；缺省退回 role slug） */
  roleName?: string
  onDecide: (approval: Approval, decision: "approved" | "rejected", note: string) => Promise<DecideApprovalResult | void>
  /** 工作台内本就在对话页 → 隐藏「和 Agent 聊聊」 */
  showChat?: boolean
}) {
  const [choice, setChoice] = useState<"approved" | "rejected" | null>(null)
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<DecideApprovalResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const spawn = asSpawnAction(approval)
  const done = result !== null

  const submit = async () => {
    if (!choice || busy) return
    setBusy(true); setError(null)
    try {
      const r = await onDecide(approval, choice, note)
      setResult(r ?? { approval_id: approval.id, status: choice })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const chat = () => {
    if (!approval.session_id) return
    window.dispatchEvent(new CustomEvent("goto-session", { detail: { sessionId: approval.session_id } }))
  }

  return (
    <div className="rounded-lg border border-(--status-approval)/40 bg-(--status-approval)/5 p-3">
      <div className="flex items-center gap-2">
        <HelpCircle size={15} className="shrink-0 text-(--status-approval)" />
        <span className="text-xs font-semibold text-foreground">Agent 需要你的确认</span>
        <span className={cn("shrink-0 rounded border px-1 font-mono text-[10px]", RISK_CLS[approval.risk] ?? RISK_CLS.low)}>
          风险 {approval.risk}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground/70">{approval.requested_by}</span>
      </div>

      {/* 说明区：op 专属文案 */}
      <div className="mt-2 space-y-1.5">
        {spawn ? (
          <>
            <p className="text-xs font-medium text-foreground/90">🪟 编排申请开新会话窗</p>
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded border border-border px-1.5 py-px font-mono text-[11px]">{spawn.role}</span>
              {roleName && <span className="text-[11px] text-muted-foreground">{roleName}</span>}
            </div>
            <p className="rounded bg-card/60 p-2 text-[11px] text-foreground/80">
              {spawn.reason || "（编排未填写理由，建议拒绝）"}
            </p>
          </>
        ) : (
          <pre className="overflow-auto rounded bg-card/60 p-2 font-mono text-[11px]">{JSON.stringify(approval.action, null, 2)}</pre>
        )}
        {approval.boundary && (
          <p className="rounded border border-(--status-approval)/40 bg-(--status-approval)/10 p-2 text-[11px] text-(--status-approval)">
            当前行动边界：{approval.boundary}
          </p>
        )}
      </div>

      {/* 选项（单选） */}
      <div className="mt-2 space-y-1.5">
        <Choice selected={choice === "approved"} onClick={() => setChoice("approved")} disabled={busy || done}
          tone="ok" title="批准"
          desc={spawn ? "自动建窗并提交跑队列" : "执行该动作，全程落审计事件"} />
        <Choice selected={choice === "rejected"} onClick={() => setChoice("rejected")} disabled={busy || done}
          tone="danger" title="拒绝"
          desc="不动作，Agent 下一轮经事件可见后自行改道" />
      </div>

      {!done && (
        <>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            placeholder="附加说明（可选，作为人工引导发给该会话）"
            className="mt-2 w-full resize-none rounded-md border border-border bg-background p-2 text-[11px] outline-none focus:border-primary/50"
          />
          <div className="mt-2 flex items-center justify-end gap-2">
            {showChat && approval.session_id && (
              <Button size="sm" variant="ghost" className="h-7 gap-1 text-[11px] text-muted-foreground" onClick={chat}>
                <MessageSquare size={12} /> 和 Agent 聊聊
              </Button>
            )}
            <Button size="sm" className="h-7 text-[11px]" disabled={!choice || busy} onClick={() => void submit()}>
              {busy ? "提交中…" : "提交"}
            </Button>
          </div>
        </>
      )}

      {error && <p className="mt-2 text-[11px] text-(--status-error)">{error}</p>}
      {result && <ExecNote approval={approval} result={result} />}
    </div>
  )
}
