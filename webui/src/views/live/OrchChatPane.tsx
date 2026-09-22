import { useEffect, useMemo, useRef } from "react"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { fmtDateTimeMin, utcTitle } from "@/lib/datetime"
import type { BBEvent, OrchPersona, PhaseGoal } from "@/lib/types"

// 对话化编排器 M1/M2/M3（2026-09-21，DESIGN §6.4）：编排页签三段式中的前两段——
// 顶部阶段目标条（M2 goal 闭环，编辑/清空经回调交父级弹层）+ 中部对话流（M1，
// orch.chat 事件组装成轮：human 消息开新轮、orch 回复追加；窗口裁剪造成的孤儿
// orch 消息自成一轮不丢）。运行记录折叠区由 LiveRoom 渲染，本组件不含。
// orch 回复走 MarkdownView（react-markdown 无 raw HTML，防注入）；tool_trace
// 折叠展示（对话轮全闸门同源：发任务/开窗动作在轨迹里可见）。

type ChatTrace = { name: string; args: string; result: string }

const traceOf = (ev: BBEvent): ChatTrace[] => {
  const t = (ev.payload as { tool_trace?: unknown } | null)?.tool_trace
  if (!Array.isArray(t)) return []
  return t.filter((x): x is ChatTrace =>
    !!x && typeof x === "object" && typeof (x as ChatTrace).name === "string")
}

const textOf = (ev: BBEvent): string => {
  const t = (ev.payload as { text?: unknown } | null)?.text
  return typeof t === "string" ? t : ""
}

/** orch.chat 事件序列 → 对话轮：human 开新轮（截断上一轮），orch 追加当前轮 */
const turnsOf = (events: BBEvent[]) => {
  const turns: { key: number; human: BBEvent | null; orch: BBEvent[] }[] = []
  for (const e of events) {
    const role = (e.payload as { role?: unknown } | null)?.role
    if (role === "human" || !turns.length) {
      turns.push({ key: e.id, human: role === "human" ? e : null, orch: role === "human" ? [] : [e] })
    } else {
      turns[turns.length - 1].orch.push(e)
    }
  }
  return turns
}

function HumanBubble({ ev }: { ev: BBEvent }) {
  return (
    <div className="flex justify-end pr-1" title={utcTitle(ev.created_at)}>
      <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary/10 px-3 py-1.5 text-sm leading-relaxed text-foreground">
        <span className="whitespace-pre-wrap break-words">{textOf(ev)}</span>
      </div>
    </div>
  )
}

function OrchBubble({ ev, name }: { ev: BBEvent; name: string }) {
  const trace = traceOf(ev)
  return (
    <div className="flex justify-start pl-1" title={utcTitle(ev.created_at)}>
      <div className="max-w-[92%] rounded-2xl rounded-bl-sm bg-accent/40 px-3 py-1.5">
        <p className="mb-0.5 text-[10px] text-muted-foreground">{name} · {fmtDateTimeMin(ev.created_at)}</p>
        <MarkdownView content={textOf(ev)} prefix={`orch-chat-${ev.id}`}
          className="max-h-96 overflow-auto text-sm leading-relaxed text-foreground/90" />
        {trace.length > 0 && (
          <details className="mt-1 border-t border-white/10 pt-1 text-[11px] text-muted-foreground">
            <summary className="cursor-pointer select-none hover:text-foreground">
              🔧 动作轨迹 · {trace.length} 步（发任务/开窗等同闸门）
            </summary>
            <div className="mt-1 space-y-1">
              {trace.map((t, i) => (
                <div key={i} className="rounded border border-white/10 bg-background/40 px-1.5 py-1 font-mono">
                  <span className="text-foreground">{t.name}</span>
                  {t.args && <span className="ml-1.5 break-all opacity-70">{t.args}</span>}
                  {t.result && <div className="mt-0.5 break-all opacity-60">→ {t.result}</div>}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    </div>
  )
}

export function OrchChatPane({ events, busy, persona, goal, onEditGoal, onEditPersona }: {
  events: BBEvent[]
  busy: boolean
  persona: OrchPersona | null
  goal: PhaseGoal | null
  onEditGoal: () => void
  onEditPersona: () => void
}) {
  const orchName = persona?.display_name || "编排器"
  const turns = useMemo(
    () => turnsOf(events.filter((e) => e.kind === "orch.chat")),
    [events])
  const scroller = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  // 自动滚底：仅在用户已接近底部时跟随（上翻历史时不抢滚动条）
  useEffect(() => {
    const el = scroller.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [turns, busy])
  const onScroll = () => {
    const el = scroller.current
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 顶部阶段目标条（M2 goal 闭环）：注入 tick + 对话轮系统提示的人类验收口径 */}
      <div className="shrink-0 border-b px-3 py-2 text-xs">
        {goal ? (
          <div className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <p className="text-foreground">
                🎯 <span className="font-medium">{goal.text}</span>
                {goal.phase && <span className="ml-2 text-[10px] text-muted-foreground">阶段：{goal.phase}</span>}
              </p>
              {!!goal.criteria?.length && (
                <ul className="mt-0.5 list-disc pl-5 leading-relaxed text-muted-foreground">
                  {goal.criteria.map((c, i) => <li key={i}>{c}</li>)}
                </ul>
              )}
              <p className="mt-0.5 text-[10px] text-muted-foreground">
                {fmtDateTimeMin(goal.created_at)} 由 {goal.confirmed_by} 确认 · 已注入编排 tick 与对话轮
              </p>
            </div>
            <button type="button" onClick={onEditGoal}
              title="修改或清空阶段目标（goal.confirm/goal.clear 事件留痕）"
              className="shrink-0 rounded border px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground">
              编辑
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2 text-muted-foreground">
            <span className="min-w-0 flex-1">
              尚未设定阶段目标——与{orchName}聊出方向后，把验收口径固化下来（注入每轮编排与对话）
            </span>
            <button type="button" onClick={onEditGoal}
              className="shrink-0 rounded border border-primary/50 px-2 py-0.5 text-[11px] text-primary hover:bg-primary/10">
              🎯 设定阶段目标
            </button>
          </div>
        )}
        <button type="button" onClick={onEditPersona}
          title={persona?.persona || "给编排器起名、立人设（只注入对话轮，tick 不受影响）"}
          className="mt-1 text-[10px] text-muted-foreground hover:text-foreground hover:underline">
          🎭 {orchName} · 身份设定
        </button>
      </div>
      {/* 中部对话流（M1）：升序渲染 + 接近底部自动跟随 */}
      <div ref={scroller} onScroll={onScroll}
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-3 py-3">
        {turns.length === 0 && !busy && (
          <div className="mx-auto mt-10 max-w-md rounded-lg border bg-card p-4 text-center text-xs leading-relaxed text-muted-foreground">
            <p className="mb-1 text-foreground">与{orchName}对话</p>
            问态势、问下一步、纠正方向……它可查黑板事件流、发任务、开窗（与编排一轮同闸门）。
            对话不推进编排游标、不消费指令——插队轮，回答完毕即返回。
          </div>
        )}
        {turns.map((t) => (
          <div key={t.key} className="flex flex-col gap-1.5">
            {t.human && <HumanBubble ev={t.human} />}
            {t.orch.map((o) => <OrchBubble key={o.id} ev={o} name={orchName} />)}
          </div>
        ))}
        {busy && (
          <div className="flex justify-start pl-1">
            <div className="flex items-center gap-2 rounded-2xl rounded-bl-sm bg-accent/40 px-3 py-1.5 text-sm text-muted-foreground">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
              {orchName}思考中…
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
