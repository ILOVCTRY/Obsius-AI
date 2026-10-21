import { useState } from "react"
import { Check, Copy, Flag, ShieldAlert, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import type { ChallengeFlag } from "@/lib/types"

/** 对话内联「候选 flag 确认卡」（ctf-flag-wake，2026-10-20）。
 *
 *  场景：CTF 解题主控的子代理提交候选 flag（verdict=pending）→ 后端唤醒主控说一段
 *  话（解释这条 flag 怎么来的、证据够不够）→ 本卡片贴在主控那段话下面，让人类
 *  **一键复制**去靶场提交、**一键确认**（accepted）或**标记为错误**（rejected）。
 *
 *  三条刻意的设计：
 *  1. **flag 完整显示 + 独立复制钮**：人类要的是「复制这串」，不是「点徽章试试看」。
 *     徽章式点复制（`Blackboard.FlagBadge`）在对话流里不好发现——那里没有台账
 *     表格的上下文提示。
 *  2. **确认按钮写明后果**：确认 = 题目判为解出 + **通知这道题所有在跑的轮次
 *     （含子 agent）尽快收工**（`app._notify_project_chat_turns` →
 *     `core/chat/inbox.py`）。
 *     注意用词：是「通知收工」不是「中止」——轮次会在下一个步边界读到这条
 *     事实、写完手上的结论再停，不会被掐在半个工具调用里。写成「中止」会让
 *     用户以为结论会丢。
 *     范围是**这道题**不是整个项目：同一项目里并行跑的另一道题不受影响
 *     （按 pid 全量投递会让别题的 worker 停掉自己正在做的事）。
 *  3. **标记为错误要填理由**：理由写进台账后，同一串再提交会被 `is_rejected`
 *     拦下（`ctf_submit_flag` 直接拒收）。不填理由等于让模型反复提交同一串。
 */
export function CandidateFlagCard({ flags, onDecide, busy = false }: {
  flags: ChallengeFlag[]
  /** verdict: accepted（确认）/ rejected（标记为错误） */
  onDecide: (flag: ChallengeFlag, verdict: "accepted" | "rejected", reason?: string) => Promise<void>
  busy?: boolean
}) {
  if (!flags.length) return null
  return (
    <div className="mb-2 space-y-1.5">
      {flags.map((f) => <OneFlag key={f.id} flag={f} onDecide={onDecide} busy={busy} />)}
    </div>
  )
}

function OneFlag({ flag, onDecide, busy }: {
  flag: ChallengeFlag
  onDecide: (flag: ChallengeFlag, verdict: "accepted" | "rejected", reason?: string) => Promise<void>
  busy: boolean
}) {
  const [copied, setCopied] = useState(false)
  const [working, setWorking] = useState<"accepted" | "rejected" | null>(null)
  const [err, setErr] = useState("")

  const copy = () => {
    navigator.clipboard.writeText(flag.flag).catch(() => { /* 无剪贴板权限时静默 */ })
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const decide = async (verdict: "accepted" | "rejected") => {
    if (busy || working) return
    let reason = ""
    if (verdict === "rejected") {
      // 理由不是可选项：它决定「模型之后还能不能重提这串」。
      const input = window.prompt(
        "标记为错误 flag 的理由（会写进台账，AI 之后不会再提交这串）：", "")
      if (input === null) return          // 用户取消
      reason = input.trim()
      if (!reason) { setErr("标记为错误必须填理由"); return }
    }
    setErr(""); setWorking(verdict)
    try {
      await onDecide(flag, verdict, reason)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setWorking(null)
    }
  }

  const conf = Number.isFinite(flag.confidence) ? flag.confidence.toFixed(1) : "—"
  const hasRoundtrip = !!(flag.transform || "").trim()
  return (
    <div className="rounded-lg border border-(--status-approval)/40 bg-(--status-approval)/5 p-3">
      <div className="flex items-center gap-2">
        <Flag size={14} className="shrink-0 text-(--status-approval)" />
        <span className="text-xs font-semibold text-foreground">候选 flag 待确认</span>
        <span className={cn("shrink-0 rounded border px-1 font-mono text-[10px]",
          hasRoundtrip
            ? "border-(--status-ok)/50 text-(--status-ok)"
            : "border-(--status-approval)/50 text-(--status-approval)")}>
          {hasRoundtrip ? "可回环复算" : "仅形态判定"}
        </span>
        <span className="min-w-0 flex-1 truncate text-right font-mono text-[10px] text-muted-foreground/70">
          置信 {conf}
        </span>
      </div>

      {/* flag 本体：完整、等宽、可选中；右边独立复制钮 */}
      <div className="mt-2 flex items-center gap-2">
        <code className="min-w-0 flex-1 select-all break-all rounded bg-card/70 px-2 py-1.5 font-mono text-[12px] text-foreground">
          {flag.flag}
        </code>
        <Button size="sm" variant="outline" className="h-7 shrink-0 gap-1 text-[11px]"
          onClick={copy} title="复制 flag">
          {copied ? <><Check size={12} /> 已复制</> : <><Copy size={12} /> 复制</>}
        </Button>
      </div>

      {/* 证据：解题路径 + 变换链。空也要显示（「没有」本身是判断依据） */}
      <div className="mt-2 grid gap-1 rounded bg-card/60 p-2 text-[11px]">
        <div className="flex gap-1.5">
          <span className="shrink-0 text-muted-foreground">解题路径</span>
          <span className="min-w-0 break-words text-foreground/85">
            {flag.source?.trim() || <em className="text-muted-foreground/70">worker 未写路径</em>}
          </span>
        </div>
        <div className="flex gap-1.5">
          <span className="shrink-0 text-muted-foreground">变换链</span>
          <span className="min-w-0 break-words text-foreground/85">
            {hasRoundtrip
              ? flag.transform
              : <em className="text-muted-foreground/70">无——没跑变换回环，只是形态像</em>}
          </span>
        </div>
      </div>

      <p className="mt-2 flex items-start gap-1.5 rounded border border-(--status-approval)/40 bg-(--status-approval)/10 p-2 text-[11px] text-(--status-approval)">
        <ShieldAlert size={13} className="mt-0.5 shrink-0" />
        确认 = 该题判为解出，并<b className="mx-0.5">通知这道题所有在跑的轮次（含子 agent）尽快收工</b>
        （含仍在探索的其他方向；它们会先把手上的结论写回再停）。之后调度层不再为
        这道题派新方向。候选不计解出，所以这一步只能由你来做。
      </p>

      <div className="mt-2 flex items-center justify-end gap-2">
        <Button size="sm" variant="ghost" className="h-7 gap-1 text-[11px] text-muted-foreground"
          disabled={busy || !!working} onClick={() => void decide("rejected")}>
          <X size={12} /> {working === "rejected" ? "提交中…" : "标记为错误"}
        </Button>
        <Button size="sm" className="h-7 gap-1 text-[11px]"
          disabled={busy || !!working} onClick={() => void decide("accepted")}>
          <Check size={12} /> {working === "accepted" ? "提交中…" : "确认有效"}
        </Button>
      </div>

      {err && <p className="mt-1.5 text-[11px] text-(--status-error)">{err}</p>}
    </div>
  )
}
