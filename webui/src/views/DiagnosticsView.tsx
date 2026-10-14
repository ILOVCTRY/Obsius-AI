import { useEffect, useMemo, useState } from "react"
import { Group, Panel, Separator } from "react-resizable-panels"
import { api } from "@/lib/api"
import { fmtDateTimeMin } from "@/lib/datetime"
import { eventStyle, eventSummary } from "@/lib/events"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import type { BBEvent, ChatMessage, DiagFailure, DiagSession } from "@/lib/types"

/** 诊断（diagnostics-log，2026-10-14）：会话消息审计 + 工具/MCP 失败日志。
 *  左会话清单（chat 对话线程 / agent 执行会话），右侧会话消息流；
 *  「失败日志」页签按 类型/工具名/来源 过滤 tool.failed 与 mcp.failed。 */
export function DiagnosticsView({ pid }: { pid?: string | null }) {
  const [sessions, setSessions] = useState<DiagSession[]>([])
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [kindFilter, setKindFilter] = useState<"all" | "chat" | "agent">("all")
  const [search, setSearch] = useState("")
  const [sel, setSel] = useState<DiagSession | null>(null)
  const [chatMsgs, setChatMsgs] = useState<ChatMessage[] | null>(null)
  const [agentEvents, setAgentEvents] = useState<BBEvent[] | null>(null)
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [tab, setTab] = useState<"sessions" | "failures">("sessions")

  useEffect(() => {
    if (!pid) return
    setLoadingSessions(true)
    api.diagSessions(pid)
      .then((d) => setSessions([...d.chat, ...d.agent]))
      .catch(() => setSessions([]))
      .finally(() => setLoadingSessions(false))
  }, [pid])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return sessions.filter((s) => {
      if (kindFilter !== "all" && s.kind !== kindFilter) return false
      if (!q) return true
      return (s.name + " " + s.id + " " + s.agent_id).toLowerCase().includes(q)
    })
  }, [sessions, kindFilter, search])

  // 选中会话后拉消息：chat=chat_messages；agent=events（session_id 维度）
  useEffect(() => {
    if (!pid || !sel) { setChatMsgs(null); setAgentEvents(null); return }
    setLoadingMsgs(true)
    setChatMsgs(null)
    setAgentEvents(null)
    if (sel.kind === "chat") {
      api.chatThread(sel.id)
        .then((d) => setChatMsgs(d.messages))
        .catch(() => setChatMsgs([]))
        .finally(() => setLoadingMsgs(false))
    } else {
      api.eventsTail(pid, 500, sel.id)
        .then((rows) => setAgentEvents(rows))
        .catch(() => setAgentEvents([]))
        .finally(() => setLoadingMsgs(false))
    }
  }, [pid, sel])

  if (!pid) {
    return <div className="p-6 text-sm text-muted-foreground">诊断需要项目上下文：请先打开一个项目。</div>
  }

  return (
    <div className={cn("flex h-full min-h-0 flex-col", !pid && "pt-9")}>
      <div className="flex items-center gap-3 border-b px-4 py-2.5">
        <h1 className="text-sm font-medium">诊断</h1>
        <span className="text-[11px] text-muted-foreground">
          会话消息审计 · 工具/MCP 调用失败日志
        </span>
        <span className="flex-1" />
        <div className="flex items-center gap-1">
          <Button size="sm" variant={tab === "sessions" ? "secondary" : "ghost"}
                  className="h-7 px-2.5 text-xs" onClick={() => setTab("sessions")}>
            会话
          </Button>
          <Button size="sm" variant={tab === "failures" ? "secondary" : "ghost"}
                  className="h-7 px-2.5 text-xs" onClick={() => setTab("failures")}>
            失败日志
          </Button>
        </div>
      </div>

      {tab === "sessions" ? (
        <SessionsTab
          sessions={filtered} total={sessions.length} loading={loadingSessions}
          kindFilter={kindFilter} onKindFilter={setKindFilter}
          search={search} onSearch={setSearch}
          sel={sel} onSelect={setSel}
          chatMsgs={chatMsgs} agentEvents={agentEvents} loadingMsgs={loadingMsgs} />
      ) : (
        <FailuresTab pid={pid}
          onOpenSession={(f) => {
            const hit = sessions.find((s) =>
              s.id === String(f.payload?.thread_id || f.session_id || ""))
            if (hit) { setSel(hit); setTab("sessions") }
          }} />
      )}
    </div>
  )
}

// ---------- 会话页签：左清单 / 右消息流 ----------
// 时间展示一律走 lib/datetime（禁止手拼 ISO）

function SessionsTab({ sessions, total, loading, kindFilter, onKindFilter,
                      search, onSearch, sel, onSelect, chatMsgs, agentEvents,
                      loadingMsgs }: {
  sessions: DiagSession[]
  total: number
  loading: boolean
  kindFilter: "all" | "chat" | "agent"
  onKindFilter: (v: "all" | "chat" | "agent") => void
  search: string
  onSearch: (v: string) => void
  sel: DiagSession | null
  onSelect: (s: DiagSession) => void
  chatMsgs: ChatMessage[] | null
  agentEvents: BBEvent[] | null
  loadingMsgs: boolean
}) {
  const chips: { key: "all" | "chat" | "agent"; label: string }[] = [
    { key: "all", label: "全部" },
    { key: "chat", label: "对话线程" },
    { key: "agent", label: "执行会话" },
  ]
  return (
    <Group orientation="horizontal" className="min-h-0 flex-1">
      {/* 左：会话清单（宽度可拖，同 FuzzerView 分屏口径） */}
      <Panel defaultSize={320} minSize={220} maxSize={480}
             className="flex min-h-0 flex-col">
        <div className="space-y-2 border-b p-2.5">
          <div className="flex items-center gap-1">
            {chips.map((c) => (
              <button key={c.key}
                      className={cn("rounded-full px-2.5 py-1 text-[11px]",
                                    kindFilter === c.key
                                      ? "bg-primary/10 text-primary"
                                      : "text-muted-foreground hover:bg-accent")}
                      onClick={() => onKindFilter(c.key)}>
                {c.label}
              </button>
            ))}
            <span className="flex-1" />
            <span className="text-[10px] text-muted-foreground">
              {sessions.length}/{total}
            </span>
          </div>
          <Input value={search} placeholder="搜名称 / id / 智能体"
                 onChange={(e) => onSearch(e.target.value)}
                 className="h-7 text-xs" />
        </div>
        <ScrollArea className="min-h-0 flex-1">
          {loading && <div className="p-4 text-xs text-muted-foreground">加载中…</div>}
          {!loading && !sessions.length && (
            <div className="p-4 text-xs text-muted-foreground">没有匹配的会话</div>
          )}
          {sessions.map((s) => (
            <button key={s.id}
                    className={cn("w-full border-b px-3 py-2 text-left hover:bg-accent/50",
                                  sel?.id === s.id && "bg-accent")}
                    onClick={() => onSelect(s)}>
              <div className="flex items-center gap-1.5">
                <Badge variant="outline" className="h-4 shrink-0 px-1 text-[10px]">
                  {s.kind === "chat" ? "对话" : "执行"}
                </Badge>
                <span className="min-w-0 flex-1 truncate text-xs">{s.name}</span>
                {s.fail_count > 0 && (
                  <span className="shrink-0 rounded bg-(--status-error)/15 px-1 text-[10px] text-(--status-error)">
                    {s.fail_count} 失败
                  </span>
                )}
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                <span className="truncate">{s.agent_id}</span>
                <span>·</span>
                <span>{s.msg_count} 条</span>
                <span>·</span>
                <span>{fmtDateTimeMin(s.updated_at)}</span>
              </div>
            </button>
          ))}
        </ScrollArea>
      </Panel>

      <Separator className="z-10 h-full w-px shrink-0 bg-border transition-colors hover:bg-primary/60 data-[separator-active]:bg-primary" />

      {/* 右：消息流 */}
      <Panel className="flex min-h-0 flex-1 flex-col">
        {!sel ? (
          <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
            左侧选择会话，查看该会话发送的全部消息（含工具调用与失败）
          </div>
        ) : (
          <>
            <div className="flex items-center gap-2 border-b px-4 py-2">
              <Badge variant="outline" className="h-4 px-1 text-[10px]">
                {sel.kind === "chat" ? "对话线程" : "执行会话"}
              </Badge>
              <span className="truncate text-xs font-medium">{sel.name}</span>
              <span className="truncate text-[10px] text-muted-foreground">
                {sel.agent_id} · {sel.msg_count} 条 · {fmtDateTimeMin(sel.created_at)} 起
              </span>
              <span className="flex-1" />
              <span className="text-[10px] text-muted-foreground">id: {sel.id}</span>
            </div>
            <ScrollArea className="min-h-0 flex-1">
              {loadingMsgs && <div className="p-4 text-xs text-muted-foreground">加载中…</div>}
              {!loadingMsgs && sel.kind === "chat" && (
                <ChatStream msgs={chatMsgs ?? []} />
              )}
              {!loadingMsgs && sel.kind === "agent" && (
                <EventStream events={agentEvents ?? []} />
              )}
              {!loadingMsgs && !chatMsgs && !agentEvents && (
                <div className="p-4 text-xs text-muted-foreground">无消息</div>
              )}
            </ScrollArea>
          </>
        )}
      </Panel>
    </Group>
  )
}

// ---------- chat 线程消息流 ----------

function ChatStream({ msgs }: { msgs: ChatMessage[] }) {
  if (!msgs.length) return <div className="p-4 text-xs text-muted-foreground">无消息</div>
  return (
    <div className="space-y-2 p-3">
      {msgs.map((m) => {
        const failed = m.role === "tool" && m.content.startsWith("[错误")
        return (
          <div key={`${m.role}-${m.id}`} className="rounded border bg-card/50 p-2.5">
            <div className="mb-1 flex items-center gap-2 text-[10px] text-muted-foreground">
              <Badge variant="outline" className={cn(
                "h-4 px-1 text-[10px]",
                m.role === "user" && "text-(--viz-sev-low)",
                m.role === "assistant" && "text-primary",
                failed && "border-(--status-error)/40 text-(--status-error)")}>
                {m.role === "user" ? "用户" : m.role === "assistant" ? "助手" : "工具结果"}
              </Badge>
              <span>{fmtDateTimeMin(m.created_at)}</span>
              {m.thinking ? <span className="text-muted-foreground/70">含思考</span> : null}
            </div>
            {m.tool_calls?.length ? (
              <div className="mb-1.5 flex flex-wrap gap-1">
                {m.tool_calls.map((tc) => (
                  <code key={tc.id} className="rounded bg-muted px-1.5 py-0.5 text-[10px]">
                    {tc.name}
                  </code>
                ))}
              </div>
            ) : null}
            {m.thinking ? (
              <details className="mb-1.5">
                <summary className="cursor-pointer text-[10px] text-muted-foreground">思考</summary>
                <pre className="mt-1 whitespace-pre-wrap rounded bg-muted/50 p-2 text-[11px] leading-relaxed text-muted-foreground">
                  {m.thinking}
                </pre>
              </details>
            ) : null}
            {m.content ? (
              <pre className={cn(
                "whitespace-pre-wrap break-all text-[11px] leading-relaxed",
                failed ? "text-(--status-error)" : "text-foreground/90")}>
                {m.content}
              </pre>
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

// ---------- agent 执行会话事件流 ----------

function EventStream({ events }: { events: BBEvent[] }) {
  if (!events.length) return <div className="p-4 text-xs text-muted-foreground">无事件</div>
  return (
    <div className="space-y-1 p-3">
      {events.map((e) => {
        const st = eventStyle(e.kind, e.payload)
        const sum = eventSummary(e.payload, undefined, undefined, e.kind)
        const failed = e.kind === "tool.call" && e.payload?.ok === false
        return (
          <details key={e.id} className="rounded border bg-card/50 px-2.5 py-1.5" open={st.defaultOpen}>
            <summary className="flex cursor-pointer items-center gap-2 text-[11px]">
              <span className={cn("shrink-0", st.className)}>{st.label}</span>
              {failed && (
                <span className="shrink-0 rounded bg-(--status-error)/15 px-1 text-[10px] text-(--status-error)">
                  失败
                </span>
              )}
              <span className="min-w-0 flex-1 truncate text-muted-foreground">{sum}</span>
              <span className="shrink-0 text-[10px] text-muted-foreground/70">
                {fmtDateTimeMin(e.created_at)}
              </span>
            </summary>
            <pre className="mt-1.5 whitespace-pre-wrap break-all rounded bg-muted/50 p-2 text-[10px] leading-relaxed text-muted-foreground">
              {JSON.stringify(e.payload, null, 2)}
            </pre>
          </details>
        )
      })}
    </div>
  )
}

// ---------- 失败日志页签 ----------

function FailuresTab({ pid, onOpenSession }: {
  pid: string
  onOpenSession: (f: DiagFailure) => void
}) {
  const [rows, setRows] = useState<DiagFailure[] | null>(null)
  const [kind, setKind] = useState<"all" | "tool" | "mcp">("all")
  const [name, setName] = useState("")
  const [author, setAuthor] = useState("")

  const load = () => {
    setRows(null)
    api.diagFailures(pid, {
      kind: kind === "all" ? undefined : kind,
      name: name.trim() || undefined,
      author: author.trim() || undefined,
      limit: 200,
    })
      .then((d) => setRows(d.failures))
      .catch(() => setRows([]))
  }
  // pid/kind 变化自动重查；name/author 改动走「查询」按钮或输入框回车
  // （避免每次按键都打接口）。load 闭包捕获当期 state，deps 须含 kind。
  useEffect(load, [pid, kind])  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b p-2.5">
        {(["all", "tool", "mcp"] as const).map((k) => (
          <button key={k}
                  className={cn("rounded-full px-2.5 py-1 text-[11px]",
                                kind === k ? "bg-primary/10 text-primary"
                                           : "text-muted-foreground hover:bg-accent")}
                  onClick={() => setKind(k)}>
            {k === "all" ? "全部" : k === "tool" ? "工具失败" : "MCP 失败"}
          </button>
        ))}
        <Input value={name} placeholder="工具名子串（如 bb_query / ida）"
               onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && load()}
               className="h-7 w-52 text-xs" />
        <Input value={author} placeholder="来源子串（会话/线程 id）"
               onChange={(e) => setAuthor(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && load()}
               className="h-7 w-52 text-xs" />
        <Button size="sm" variant="outline" className="h-7 px-2.5 text-xs" onClick={load}>
          查询
        </Button>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        {rows === null && <div className="p-4 text-xs text-muted-foreground">加载中…</div>}
        {rows !== null && !rows.length && (
          <div className="p-4 text-xs text-muted-foreground">没有失败记录</div>
        )}
        {rows?.map((f) => {
          const p = f.payload
          const err = String(p?.error || "")
          return (
            <div key={f.id} className="border-b px-3 py-2">
              <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                <Badge variant="outline" className={cn(
                  "h-4 px-1 text-[10px]",
                  f.kind === "mcp.failed" ? "text-(--status-approval)"
                                          : "text-(--status-error)")}>
                  {f.kind === "mcp.failed" ? "MCP" : "工具"}
                </Badge>
                <span className="font-mono text-foreground/80">{String(p?.name || "")}</span>
                <span>·</span>
                <span>{fmtDateTimeMin(f.created_at)}</span>
                <span className="flex-1" />
                <button className="text-primary hover:underline"
                        onClick={() => onOpenSession(f)}>
                  定位会话
                </button>
              </div>
              <pre className="mt-1 whitespace-pre-wrap break-all text-[11px] leading-relaxed text-(--status-error)">
                {err.slice(0, 400)}{err.length > 400 ? " …" : ""}
              </pre>
              <div className="mt-1 text-[10px] text-muted-foreground/70">
                {String(p?.source || "")} · {String(f.author || "")}
                {f.session_id ? ` · ${f.session_id}` : ""}
                {p?.thread_id ? ` · ${String(p.thread_id)}` : ""}
              </div>
            </div>
          )
        })}
      </ScrollArea>
    </div>
  )
}
