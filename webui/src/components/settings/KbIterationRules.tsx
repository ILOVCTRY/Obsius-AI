import { useEffect, useState } from "react"
import { Sparkles, Info, ChevronDown, ChevronRight } from "lucide-react"
import { api } from "@/lib/api"
import type { KbIterationRules } from "@/lib/types"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

// 知识库迭代触发规则说明（2026-10-20）：回答「什么情况下 Agent 才会提变更提案」。
//
// 规则数据全部来自后端 `GET /api/proposals/rules`——它和 `should_trigger()` 读
// 同一张 TRIGGER_RULES 表。**前端不写死任何阈值**：写死就会出现「页面写着 2 次、
// 后端按 3 次判」的分叉，人类按页面理解去调参反而更糊涂。

export function KbIterationRulesDialog({
  open, onOpenChange, pid,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  pid?: string
}) {
  const [doc, setDoc] = useState<KbIterationRules | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [openSection, setOpenSection] = useState<Record<string, boolean>>({ rules: true })

  useEffect(() => {
    if (!open) return
    setErr(null)
    api.proposalRules(pid).then(setDoc).catch((e) => setErr(String(e)))
  }, [open, pid])

  const toggle = (k: string) =>
    setOpenSection((s) => ({ ...s, [k]: !s[k] }))

  const Section = ({
    k, title, hint, children,
  }: { k: string; title: string; hint?: string; children: React.ReactNode }) => {
    const on = !!openSection[k]
    return (
      <div className="rounded border">
        <button
          type="button"
          className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left hover:bg-accent/30"
          onClick={() => toggle(k)}
        >
          {on ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" />}
          <span className="text-[12px] font-semibold">{title}</span>
          {hint && <span className="min-w-0 flex-1 truncate text-[10px] text-muted-foreground">{hint}</span>}
        </button>
        {on && <div className="border-t px-2.5 py-2">{children}</div>}
      </div>
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogTitle className="flex items-center gap-1.5 text-sm">
          <Sparkles className="size-4 text-primary" />
          知识库迭代触发规则
        </DialogTitle>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          这是 Agent 提出变更提案的全部触发条件。规则与后端判定同源，
          页面上写的阈值就是实际生效的阈值。
        </p>

        {err && <p className="text-[11px] text-(--status-error)">{err}</p>}
        {!doc && !err && <p className="text-[11px] text-muted-foreground">加载中…</p>}

        {doc && (
          <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
            {/* 开关 */}
            <div className="rounded border bg-card/30 px-2.5 py-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] font-semibold">{doc.switch.label}</span>
                <span className="rounded bg-accent/40 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
                  {doc.switch.key}
                </span>
                <span className="text-[10px] text-muted-foreground">{doc.switch.scope}</span>
                <span className="flex-1" />
                {doc.enabled !== null && (
                  <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium",
                    doc.enabled
                      ? "bg-emerald-500/15 text-emerald-300"
                      : "bg-accent/50 text-muted-foreground")}>
                    {doc.enabled ? "本项目已开启" : "本项目已关闭"}
                  </span>
                )}
              </div>
              <p className="mt-1 flex items-start gap-1 text-[11px] text-muted-foreground">
                <Info className="mt-0.5 size-3 shrink-0" />
                <span>{doc.switch.off_meaning}</span>
              </p>
            </div>

            {/* 触发通道 */}
            <Section k="channels" title="三条触发通道" hint={doc.channels.map((c) => c.label).join(" / ")}>
              <div className="space-y-1.5">
                {doc.channels.map((c) => (
                  <div key={c.key}>
                    <p className="text-[11px] font-semibold">{c.label}</p>
                    <p className="text-[11px] leading-relaxed text-muted-foreground">{c.detail}</p>
                  </div>
                ))}
              </div>
            </Section>

            {/* 规则表 */}
            <Section k="rules" title="轮末自动判定的规则信号" hint={`命中任一即触发（共 ${doc.rules.length} 条）`}>
              <div className="space-y-1">
                {doc.rules.map((r, i) => (
                  <div key={r.key} className="flex items-start gap-2 rounded bg-background/40 px-2 py-1.5">
                    <span className="mt-0.5 shrink-0 rounded bg-primary/10 px-1 font-mono text-[9px] text-primary">
                      {i + 1}
                    </span>
                    <div className="min-w-0">
                      <p className="text-[11px] font-semibold">{r.label}</p>
                      <p className="text-[11px] leading-relaxed text-muted-foreground">{r.detail}</p>
                    </div>
                  </div>
                ))}
              </div>
            </Section>

            {/* 限制与落地 */}
            <div className="rounded border px-2.5 py-2">
              <p className="text-[11px] font-semibold">额度与落地</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                每会话最多 <b className="text-foreground">{doc.limits.per_session_proposals}</b> 条 pending 提案。
                {doc.limits.note}
              </p>
              <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{doc.landing}</p>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** 头部行内触发按钮（放进「变更提案」页 header）。 */
export function KbIterationRulesButton({ pid }: { pid?: string }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title="查看什么规则下 Agent 会提出变更提案"
        className="inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent/40 hover:text-foreground"
      >
        <Info className="size-3" />
        触发规则
      </button>
      <KbIterationRulesDialog open={open} onOpenChange={setOpen} pid={pid} />
    </>
  )
}
