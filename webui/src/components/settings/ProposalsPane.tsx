import { useCallback, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { Proposal, ProposalStatus } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { fmtDateTime, utcTitle } from "@/lib/datetime"
import { DiffView } from "./DiffView"

// 统一变更提案审批：左列表（状态过滤）+ 右详情（实时 diff、rename/delete 引用面、
// 批准/拒绝/改后采纳）。只处理 packs 级提案；复盘 Job 在直播间发起、跳转到本 tab。

const STATUS_LABEL: Record<ProposalStatus, string> = {
  pending: "待审批", approved: "已应用", rejected: "已拒绝",
}

export function targetLabel(p: Proposal): string {
  const t = p.target
  if (t.kind === "kb") {
    const base = `${t.cap}/${t.path}`
    return p.mode === "rename" ? `${base} → ${t.cap}/${t.new_path}` : base
  }
  return `技能 ${t.skill_kind === "track" ? "轨" : "包"}/${t.owner}/${t.name}`
}

const MODE_LABEL: Record<string, string> = {
  edit: "改写", create: "新建", rename: "改名", delete: "删除",
}

export function ProposalsPane({ onPendingChange }: { onPendingChange?: (n: number) => void }) {
  const [filter, setFilter] = useState<ProposalStatus | "all">("pending")
  const [list, setList] = useState<Proposal[]>([])
  const [selId, setSelId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Proposal | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [rejectNote, setRejectNote] = useState("")
  const [reviseOpen, setReviseOpen] = useState(false)
  const [revContent, setRevContent] = useState("")
  const [revNewPath, setRevNewPath] = useState("")
  const [revSummary, setRevSummary] = useState("")
  const [revReason, setRevReason] = useState("")
  const [revNote, setRevNote] = useState("")

  const reload = useCallback(() => {
    api.proposals(filter === "all" ? undefined : filter).then((ps) => {
      setList(ps)
      setSelId((cur) => (cur && ps.some((p) => p.id === cur) ? cur : (ps[0]?.id ?? null)))
    }).catch((e) => setErr(String(e)))
    api.proposals("pending").then((ps) => onPendingChange?.(ps.length)).catch(() => {})
  }, [filter, onPendingChange])

  useEffect(reload, [reload])
  useEffect(() => {
    const h = () => reload()
    window.addEventListener("proposals-changed", h)
    return () => window.removeEventListener("proposals-changed", h)
  }, [reload])

  useEffect(() => {
    if (!selId) { setDetail(null); return }
    api.proposal(selId).then(setDetail).catch((e) => setErr(String(e)))
  }, [selId, list])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setErr(null)
    try {
      await fn()
      window.dispatchEvent(new Event("packs-changed"))
      setReviseOpen(false)
      reload()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const openRevise = () => {
    if (!detail) return
    setRevContent(detail.content ?? "")
    setRevNewPath(detail.target.new_path ?? "")
    setRevSummary(detail.summary)
    setRevReason(detail.reason)
    setRevNote("")
    setReviseOpen(true)
  }

  const doRevise = (andApply: boolean) =>
    act(async () => {
      if (!detail) return
      const changes: Record<string, string> = { summary: revSummary, reason: revReason }
      if (detail.mode === "edit" || detail.mode === "create") changes.content = revContent
      if (detail.mode === "rename") changes.new_path = revNewPath
      await api.reviseProposal(detail.id, changes, revNote)
      if (andApply) await api.applyProposal(detail.id)
    })

  return (
    <div className="flex h-full min-h-0">
      {/* 左：列表 */}
      <div className="flex w-72 shrink-0 flex-col border-r">
        <div className="flex shrink-0 items-center gap-1 border-b p-1.5">
          {(["pending", "all", "approved", "rejected"] as const).map((f) => (
            <button key={f}
              className={cn("rounded px-1.5 py-0.5 text-[11px]",
                filter === f ? "bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent/40")}
              onClick={() => setFilter(f)}>
              {f === "all" ? "全部" : STATUS_LABEL[f]}
            </button>
          ))}
          <span className="flex-1" />
          <button className="text-[11px] text-muted-foreground hover:text-primary" title="刷新"
                  onClick={reload}>⟳</button>
        </div>
        <ScrollArea className="min-h-0 flex-1">
          <div className="p-1">
            {list.map((p) => (
              <button key={p.id}
                className={cn("mb-0.5 block w-full rounded px-2 py-1 text-left hover:bg-accent/40",
                  selId === p.id && "bg-primary/10")}
                onClick={() => setSelId(p.id)}>
                <span className="flex items-center gap-1">
                  <Badge variant="outline" className={cn("shrink-0 px-1 text-[9px]",
                    p.mode === "delete" && "text-(--status-error)",
                    p.mode === "create" && "text-emerald-300")}>
                    {MODE_LABEL[p.mode] ?? p.mode}
                  </Badge>
                  <span className="min-w-0 flex-1 truncate text-[11px]">{p.summary || "(无摘要)"}</span>
                </span>
                <span className="block truncate font-mono text-[10px] text-muted-foreground">
                  {targetLabel(p)}
                </span>
                <span className="font-mono text-[9px] text-muted-foreground/70" title={utcTitle(p.created_at)}>
                  {p.origin} · {p.id.slice(3, 12)} · {fmtDateTime(p.created_at)}
                </span>
              </button>
            ))}
            {list.length === 0 && (
              <p className="p-3 text-[11px] leading-relaxed text-muted-foreground">
                无{filter === "pending" ? "待审批" : ""}提案。<br />
                Agent 执行任务中沉淀的文档变更、直播间「复盘沉淀」由 planner LLM 生成的提案都进这里。
              </p>
            )}
          </div>
        </ScrollArea>
      </div>

      {/* 右：详情 */}
      <div className="min-w-0 flex-1">
        {!detail ? (
          <p className="p-4 text-xs text-muted-foreground">选择左侧提案查看实时 diff 并审批</p>
        ) : (
          // 普通块级滚动容器：Radix ScrollArea 的 viewport 是 display:table，会被超长 diff 行撑宽——
          // 按钮行 justify-end 被排到视口右缘之外（水平无滚动条不可达），表现为「批准/拒绝按钮消失」
          <div className="h-full min-h-0 overflow-y-auto">
            <div className="flex flex-col gap-2 p-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge variant="outline" className="text-[10px]">{MODE_LABEL[detail.mode] ?? detail.mode}</Badge>
                <Badge variant="outline" className="text-[10px]">
                  {detail.target.kind === "kb" ? "知识库" : "技能"}
                </Badge>
                <Badge variant="outline" className="text-[10px]">
                  来源 {detail.origin === "agent" ? "Agent" : detail.origin === "review" ? "复盘" : "人类"}
                </Badge>
                <span className="font-mono text-[11px] text-muted-foreground">{targetLabel(detail)}</span>
                <span className="flex-1" />
                <Badge variant="outline" className={cn("text-[10px]",
                  detail.status === "pending" && "text-(--status-approval)",
                  detail.status === "approved" && "text-emerald-300",
                  detail.status === "rejected" && "text-(--status-error)")}>
                  {STATUS_LABEL[detail.status]}
                </Badge>
              </div>

              <div className="rounded border bg-card/30 p-2 text-[12px]">
                <p className="font-semibold">{detail.summary}</p>
                <p className="mt-1 text-muted-foreground">{detail.reason}</p>
                {detail.evidence && (
                  <p className="mt-1 whitespace-pre-wrap rounded bg-background/50 p-1.5 text-[10px] text-muted-foreground">
                    任务证据：{detail.evidence}
                  </p>
                )}
                <p className="mt-1 font-mono text-[10px] text-muted-foreground/70">
                  {detail.id}
                  {detail.project ? ` · 项目 ${detail.project.slice(0, 12)}` : ""}
                  {detail.task ? ` · 任务 ${detail.task.slice(0, 12)}` : ""}
                  {detail.revisions.length ? ` · 已修订 ${detail.revisions.length} 次` : ""}
                </p>
              </div>

              {!detail.live?.exists_now && detail.mode !== "create" && (
                <p className="rounded border border-(--status-approval)/50 p-1.5 text-[11px] text-(--status-approval)">
                  目标文件当前不在磁盘上（可能已被改名/删除）；应用前复核大概率失败。
                </p>
              )}

              {detail.live?.refs && detail.live.refs.length > 0 && (
                <div className="rounded border p-2 text-[11px]">
                  <p className="font-semibold text-(--status-approval)">
                    {detail.mode === "delete" ? "删除" : "改名"}影响面：{detail.live.refs.length} 处引用
                  </p>
                  {detail.live.refs.map((r, i) => (
                    <p key={i} className="truncate font-mono text-[10px] text-muted-foreground"
                       title={`${r.forms.join(" / ")}`}>
                      {r.file}:{r.line}（{r.kind}）
                    </p>
                  ))}
                  {detail.mode === "rename" && (
                    <p className="mt-0.5 text-[10px] text-muted-foreground">
                      应用时这些完整路径引用会在同一临界区内自动联动替换并各自备份。
                    </p>
                  )}
                </div>
              )}

              <div>
                <p className="mb-0.5 text-[10px] text-muted-foreground">
                  实时 diff（对照此刻磁盘，非存储快照）
                </p>
                <DiffView diff={detail.live?.diff ?? ""} className="max-h-[40vh] overflow-auto" />
              </div>

              {err && <p className="break-all text-[11px] text-(--status-error)">{err}</p>}

              {detail.status === "pending" && (
                reviseOpen ? (
                  <div className="space-y-1.5 rounded border p-2">
                    <p className="text-[11px] font-semibold">改后采纳：修订后重新校验，仍待应用</p>
                    {(detail.mode === "edit" || detail.mode === "create") && (
                      <Textarea value={revContent} onChange={(e) => setRevContent(e.target.value)}
                                className="min-h-48 font-mono text-[11px] leading-relaxed" spellCheck={false} />
                    )}
                    {detail.mode === "rename" && (
                      <Input value={revNewPath} onChange={(e) => setRevNewPath(e.target.value)}
                             className="font-mono text-xs" placeholder="新路径 new_path" />
                    )}
                    <Input value={revSummary} onChange={(e) => setRevSummary(e.target.value)}
                           className="text-xs" placeholder="摘要（≤300 字）" />
                    <Textarea value={revReason} onChange={(e) => setRevReason(e.target.value)}
                              className="min-h-16 text-xs" placeholder="理由（附任务证据）" />
                    <Input value={revNote} onChange={(e) => setRevNote(e.target.value)}
                           className="text-xs" placeholder="修订说明（记入 revisions，可空）" />
                    <div className="flex justify-end gap-1.5">
                      <Button size="sm" variant="ghost" onClick={() => setReviseOpen(false)}>取消</Button>
                      <Button size="sm" variant="outline" disabled={busy}
                              onClick={() => doRevise(false)}>只保存修订</Button>
                      <Button size="sm" disabled={busy} onClick={() => doRevise(true)}>
                        {busy ? "处理中…" : "修订并应用"}
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="space-y-1.5 rounded border p-2">
                    <Input value={rejectNote} onChange={(e) => setRejectNote(e.target.value)}
                           className="h-7 text-xs" placeholder="拒绝理由（可空）" />
                    <div className="flex justify-end gap-1.5">
                      <Button size="sm" variant="outline" className="text-(--status-error)"
                              disabled={busy}
                              onClick={() => act(async () => {
                                await api.rejectProposal(detail.id, rejectNote)
                                setRejectNote("")
                              })}>
                        拒绝
                      </Button>
                      <Button size="sm" variant="outline" disabled={busy} onClick={openRevise}>
                        改后采纳
                      </Button>
                      <Button size="sm" disabled={busy}
                              onClick={() => act(() => api.applyProposal(detail.id))}>
                        {busy ? "应用中…" : "批准并应用"}
                      </Button>
                    </div>
                    <p className="text-[10px] text-muted-foreground">
                      批准=你以人类身份决定（decided_by=human）；应用前会按磁盘现状再校验一次，自动备份/联动。
                    </p>
                  </div>
                )
              )}
              {detail.status !== "pending" && (
                <p className="font-mono text-[10px] text-muted-foreground">
                  {fmtDateTime(detail.decided_at)} 由 {detail.decided_by} {detail.status === "approved" ? "应用" : "拒绝"}
                  {detail.decision_note ? `：${detail.decision_note}` : ""}
                </p>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
