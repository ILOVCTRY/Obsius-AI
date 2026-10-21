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
import type { BBEvent, ChatMessage, DiagFailure, DiagLlmDump,
  DiagRedlineTest, DiagRequestDetail, DiagRequestMeta, DiagSession,
  DiagSkillUsage, DiagSystemPrompt, LlmProvider } from "@/lib/types"

/** 诊断（diagnostics-log，2026-10-14）：会话消息审计 + 工具/MCP 失败日志。
 *  左会话清单（chat 对话线程 / agent 执行会话），右侧会话消息流；
 *  「失败日志」页签按 类型/工具名/来源 过滤 tool.failed 与 mcp.failed。
 *  「记录请求体」（diagnostics-request-body，2026-10-17）：顶栏开关，开启后
 *  每步真实出网请求体按会话落盘，右侧可逐条查看（system/messages/tools 原样）。 */
export function DiagnosticsView({ pid }: { pid?: string | null }) {
  const [sessions, setSessions] = useState<DiagSession[]>([])
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [kindFilter, setKindFilter] = useState<"all" | "chat" | "agent">("all")
  const [search, setSearch] = useState("")
  const [sel, setSel] = useState<DiagSession | null>(null)
  const [chatMsgs, setChatMsgs] = useState<ChatMessage[] | null>(null)
  const [agentEvents, setAgentEvents] = useState<BBEvent[] | null>(null)
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [tab, setTab] = useState<"sessions" | "failures" | "skills">("sessions")
  // 请求体留痕开关（进程级）：进页面读一次，点击切换
  const [dump, setDump] = useState<DiagLlmDump | null>(null)
  const [dumpBusy, setDumpBusy] = useState(false)

  useEffect(() => {
    api.diagLlmDump().then(setDump).catch(() => setDump(null))
  }, [])

  const toggleDump = () => {
    if (dumpBusy) return
    const next = !(dump?.enabled ?? false)
    setDumpBusy(true)
    setDump((d) => (d ? { ...d, enabled: next } : d))  // 乐观
    api.setDiagLlmDump(next)
      .then(setDump)
      .catch(() => setDump((d) => (d ? { ...d, enabled: !next } : d)))
      .finally(() => setDumpBusy(false))
  }

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
          <Button size="sm" variant={tab === "skills" ? "secondary" : "ghost"}
                  className="h-7 px-2.5 text-xs" onClick={() => setTab("skills")}>
            技能使用
          </Button>
        </div>
        <span className="mx-1 h-4 w-px bg-border" />
        <Button size="sm" variant={dump?.enabled ? "secondary" : "ghost"}
                className="h-7 px-2.5 text-xs"
                title={"开启后把每一步真实发往服务端的请求体（system/messages/tools）"
                  + "按会话写入 logs/llm_requests，每个会话保留最近 20 条；关闭即零开销"}
                onClick={toggleDump}>
          {dump?.enabled ? "● 记录请求体" : "记录请求体"}
        </Button>
        {dump?.enabled && (
          <span className="text-[10px] text-muted-foreground">
            {dump.records} 条 · {(dump.bytes / 1024).toFixed(0)} KB
          </span>
        )}
      </div>

      {tab === "sessions" ? (
        <SessionsTab
          pid={pid}
          sessions={filtered} total={sessions.length} loading={loadingSessions}
          kindFilter={kindFilter} onKindFilter={setKindFilter}
          search={search} onSearch={setSearch}
          sel={sel} onSelect={setSel} dumpEnabled={!!dump?.enabled}
          chatMsgs={chatMsgs} agentEvents={agentEvents} loadingMsgs={loadingMsgs} />
      ) : tab === "failures" ? (
        <FailuresTab pid={pid}
          onOpenSession={(f) => {
            const hit = sessions.find((s) =>
              s.id === String(f.payload?.thread_id || f.session_id || ""))
            if (hit) { setSel(hit); setTab("sessions") }
          }} />
      ) : (
        <SkillUsageTab pid={pid} />
      )}
    </div>
  )
}

// ---------- 会话页签：左清单 / 右消息流 ----------
// 时间展示一律走 lib/datetime（禁止手拼 ISO）

function SessionsTab({ pid, sessions, total, loading, kindFilter, onKindFilter,
                      search, onSearch, sel, onSelect, chatMsgs, agentEvents,
                      loadingMsgs, dumpEnabled }: {
  pid: string
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
  /** 请求体留痕开关状态（关闭时右侧只提示去顶栏开启） */
  dumpEnabled: boolean
}) {
  // 系统提示词快照（diagnostics-system-prompt，v39）：右侧头部开关切换
  // 「消息流 / 完整系统提示词」。快照按会话取，切会话即退出并清缓存。
  const [showSys, setShowSys] = useState(false)
  const [sys, setSys] = useState<DiagSystemPrompt | null>(null)
  const [sysLoading, setSysLoading] = useState(false)
  const [sysErr, setSysErr] = useState<string | null>(null)
  // 请求体（diagnostics-request-body，2026-10-17）：与系统提示词互斥的第三视图
  const [showReq, setShowReq] = useState(false)

  useEffect(() => {
    setShowSys(false); setSys(null); setSysErr(null); setSysLoading(false)
    setShowReq(false)
  }, [sel?.id])

  const openSys = () => {
    setShowSys(true)
    if (!pid || !sel || sys || sysLoading) return
    setSysErr(null); setSysLoading(true)
    api.diagSystemPrompt(pid, sel.id)
      .then(setSys)
      .catch((e: unknown) => {
        const st = (e as { status?: number } | null)?.status
        setSysErr(st === 404
          ? "该会话暂无系统提示快照（快照自 v39 起落库，历史会话不回溯补写）"
          : "系统提示词加载失败")
      })
      .finally(() => setSysLoading(false))
  }

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
              <Button size="sm" variant={showSys ? "secondary" : "ghost"}
                      className="h-6 px-2 text-[10px]"
                      onClick={() => {
                        setShowReq(false)
                        showSys ? setShowSys(false) : openSys()
                      }}>
                {showSys ? "返回消息流" : "系统提示词"}
              </Button>
              <Button size="sm" variant={showReq ? "secondary" : "ghost"}
                      className="h-6 px-2 text-[10px]"
                      title="查看该会话每一步真实发往服务端的请求体（需先在顶栏开启「记录请求体」）"
                      onClick={() => { setShowSys(false); setShowReq((v) => !v) }}>
                {showReq ? "返回消息流" : "请求体"}
              </Button>
            </div>
            <ScrollArea className="min-h-0 flex-1">
              {showReq ? (
                <RequestsPanel pid={pid} sessionId={sel.id} enabled={dumpEnabled} />
              ) : showSys ? (
                <SystemPromptPanel pid={pid} sessionId={sel.id} snap={sys}
                                   loading={sysLoading} err={sysErr} />
              ) : (
                <>
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
                </>
              )}
            </ScrollArea>
          </>
        )}
      </Panel>
    </Group>
  )
}

// ---------- 系统提示词快照（diagnostics-system-prompt，v39） ----------
// 展示该会话最近一次实际下发给 LLM 的完整 system 正文（只读 + 复制 + 一键自检）。

function SystemPromptPanel({ pid, sessionId, snap, loading, err }: {
  pid?: string | null
  sessionId: string
  snap: DiagSystemPrompt | null
  loading: boolean
  err: string | null
}) {
  const [copied, setCopied] = useState(false)
  const [test, setTest] = useState<DiagRedlineTest | null>(null)
  const [testing, setTesting] = useState(false)
  const [testErr, setTestErr] = useState<string | null>(null)
  const [providers, setProviders] = useState<LlmProvider[]>([])
  const [probeProvider, setProbeProvider] = useState("")
  const [probeModel, setProbeModel] = useState("")
  const probeModels = useMemo(
    () => providers.find((p) => p.name === probeProvider)?.models || [],
    [providers, probeProvider])

  useEffect(() => {
    if (!pid) return
    api.llmProviders()
      .then((r) => setProviders((r.providers || []).filter((p) => p.enabled !== false)))
      .catch(() => setProviders([]))
  }, [pid])

  const runTest = () => {
    if (!pid || testing) return
    setTesting(true); setTestErr(null); setTest(null)
    api.redlineTest(pid, sessionId, probeProvider || undefined, probeModel || undefined)
      .then(setTest)
      .catch((e: unknown) => setTestErr(`自检请求失败：${String((e as Error)?.message || e)}`))
      .finally(() => setTesting(false))
  }

  if (loading) return <div className="p-4 text-xs text-muted-foreground">加载中…</div>
  if (err) return <div className="p-4 text-xs text-muted-foreground">{err}</div>
  if (!snap) return <div className="p-4 text-xs text-muted-foreground">无快照</div>
  const copy = () => {
    navigator.clipboard.writeText(snap.text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }).catch(() => {})
  }
  return (
    <div className="p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
        <Badge variant="outline" className="h-4 px-1 text-[10px]">
          {snap.kind === "chat" ? "对话线程" : "执行会话"}
        </Badge>
        <span>{snap.agent_id || "—"}</span>
        <span>·</span>
        <span>{snap.chars.toLocaleString()} 字符</span>
        <span>·</span>
        <span>≈ {snap.tokens.toLocaleString()} token</span>
        <span>·</span>
        <span>{fmtDateTimeMin(snap.updated_at)}</span>
        <span className="flex-1" />
        <span className="text-muted-foreground">探测用</span>
        <select value={probeProvider}
                onChange={(e) => { setProbeProvider(e.target.value); setProbeModel("") }}
                title="用哪个供应商打探测请求（缺省=跟随该会话自己的供应商）"
                className="h-6 max-w-[9rem] rounded border bg-card px-1 text-[10px]">
          <option value="">跟随会话</option>
          {providers.map((p) => (
            <option key={p.name} value={p.name}>{p.name}</option>
          ))}
        </select>
        {probeModels.length > 1 && (
          <select value={probeModel}
                  onChange={(e) => setProbeModel(e.target.value)}
                  title="探测用模型（缺省=该供应商第一个模型）"
                  className="h-6 max-w-[9rem] rounded border bg-card px-1 text-[10px]">
            <option value="">默认模型</option>
            {probeModels.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        )}
        <Button size="sm" variant={testing ? "secondary" : "outline"}
                className="h-6 px-2 text-[10px]" disabled={testing || !pid}
                title="拿本会话真实 system 正文 + canary 打一发，判定规则注没注入、送没送到模型"
                onClick={runTest}>
          {testing ? "自检中…" : "红线一键测试"}
        </Button>
        <Button size="sm" variant="ghost" className="h-6 px-2 text-[10px]"
                onClick={copy}>
          {copied ? "已复制" : "复制全文"}
        </Button>
      </div>
      {testErr && (
        <div className="mb-2 rounded border border-(--status-error)/40 bg-(--status-error)/10 px-2.5 py-1.5 text-[11px] text-(--status-error)">
          {testErr}
        </div>
      )}
      {test && <RedlineTestCard test={test} />}
      <pre className="whitespace-pre-wrap break-all rounded border bg-card/50 p-3 text-[11px] leading-relaxed">
        {snap.text}
      </pre>
    </div>
  )
}

// ---------- 红线一键自检结果（diagnostics-redline-test，2026-10-17） ----------

const VERDICT_STYLE: Record<string, { label: string; cls: string }> = {
  ok: { label: "注入且送达", cls: "bg-(--viz-sev-low)/15 text-(--viz-sev-low)" },
  not_injected: { label: "本地未注入", cls: "bg-(--status-error)/15 text-(--status-error)" },
  channel_broken: { label: "通道断", cls: "bg-(--status-error)/15 text-(--status-error)" },
  inconclusive: { label: "信号不足", cls: "bg-(--status-warning)/15 text-(--status-warning)" },
  no_system: { label: "无 system 可取", cls: "bg-muted text-muted-foreground" },
  probe_failed: { label: "探测未发出", cls: "bg-muted text-muted-foreground" },
  error: { label: "自检出错", cls: "bg-muted text-muted-foreground" },
}

function RedlineTestCard({ test }: { test: DiagRedlineTest }) {
  const v = VERDICT_STYLE[test.verdict] || VERDICT_STYLE.error
  const mark = (ok: boolean | null | undefined) =>
    ok === true ? "✓" : ok === false ? "✗" : "—"
  return (
    <div className="mb-2 space-y-2 rounded border bg-card/50 p-2.5 text-[11px]">
      <div className="flex flex-wrap items-center gap-2">
        <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", v.cls)}>
          {v.label}
        </span>
        <span className="text-muted-foreground">
          system {test.system_source === "snapshot" ? "来自系统提示快照"
            : test.system_source === "record" ? "来自请求体记录" : "未取到"}
          {test.system_chars ? ` · ${test.system_chars.toLocaleString()} 字` : ""}
          {test.probe ? ` · 探测 ${test.probe.provider}/${test.probe.model}（${test.probe.format}${test.probe.system_in_user ? " · 兼容通道" : ""}）` : ""}
          {test.elapsed_ms != null ? ` · ${(test.elapsed_ms / 1000).toFixed(1)}s` : ""}
        </span>
      </div>
      <div className="text-[11px] leading-relaxed">{test.summary}</div>
      {test.local && (
        <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
          <span>① 本地注入 {mark(test.local.status !== "none")}</span>
          {test.local.found.map((k) => (
            <span key={k} className="rounded bg-(--viz-sev-low)/15 px-1 text-(--viz-sev-low)">{k}</span>
          ))}
          {test.local.missing.map((k) => (
            <span key={k} className="rounded bg-muted px-1 line-through">{k}</span>
          ))}
        </div>
      )}
      {test.channel && (
        <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
          <span>② canary 通道 {mark(test.channel.ok)}</span>
          <code className="rounded bg-muted px-1">{test.channel.canary}</code>
          {test.channel.ok
            ? <span className="text-(--viz-sev-low)">模型复述了 canary = system 到得了模型</span>
            : test.channel.empty_reply
              ? <span className="text-(--status-warning)">回复正文为空（思考预算吃满），不据此判通道断</span>
              : <span className="text-(--status-error)">模型没理 canary = system 送不到模型</span>}
          {test.channel.reply_head && (
            <span className="truncate" title={test.channel.reply_head}>
              回复开头：{test.channel.reply_head.slice(0, 40)}
            </span>
          )}
          {test.channel.error && <span className="text-(--status-error)">{test.channel.error}</span>}
        </div>
      )}
      {test.content && (
        <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
          <span>③ 内容引用（辅证）{mark(test.content.ok)}</span>
          {test.content.needle && (
            <span className="truncate" title={test.content.needle}>
              题目：{test.content.needle.slice(0, 40)}
            </span>
          )}
          {test.content.reply_head && (
            <span className="truncate" title={test.content.reply_head}>
              回答：{test.content.reply_head.slice(0, 40)}
            </span>
          )}
          {test.content.note && <span className="opacity-70">{test.content.note}</span>}
        </div>
      )}
      {test.error && (
        <div className="text-[10px] text-(--status-error)">{test.error}</div>
      )}
    </div>
  )
}

// ---------- 请求体留痕（diagnostics-request-body，2026-10-17） ----------
// 该会话每一步**真实出网 body**：先列表（时间/模型/字节/tool 结果条数），
// 点开看全文（system 是否首位、messages 原样、tools schema）。开关在顶栏。

function RequestsPanel({ pid, sessionId, enabled }: {
  pid: string
  sessionId: string
  enabled: boolean
}) {
  const [rows, setRows] = useState<DiagRequestMeta[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [rid, setRid] = useState<string | null>(null)
  const [seq, setSeq] = useState(0)   // 手动刷新计数

  useEffect(() => {
    setRows(null); setErr(null); setRid(null)
    api.diagRequests(pid, sessionId)
      .then((d) => setRows(d.records))
      .catch(() => setErr("请求体清单加载失败"))
  }, [pid, sessionId, seq])

  if (rid) {
    return <RequestDetail pid={pid} sessionId={sessionId} rid={rid}
                          onBack={() => setRid(null)} />
  }
  return (
    <div className="p-3">
      <div className="mb-2 flex items-center gap-2 text-[10px] text-muted-foreground">
        <span>{enabled
          ? "本会话最近 20 次出网请求（最新在前）"
          : "「记录请求体」当前关闭——开启后新产生的请求才会被记录"}</span>
        <span className="flex-1" />
        <Button size="sm" variant="ghost" className="h-6 px-2 text-[10px]"
                onClick={() => setSeq((n) => n + 1)}>
          刷新
        </Button>
      </div>
      {err && <div className="text-xs text-muted-foreground">{err}</div>}
      {!err && rows === null && (
        <div className="text-xs text-muted-foreground">加载中…</div>
      )}
      {rows && !rows.length && (
        <div className="text-xs text-muted-foreground">
          暂无记录。{enabled
            ? "在该会话里发一轮消息后点「刷新」。"
            : "先在顶栏开启「记录请求体」，再发一轮消息。"}
        </div>
      )}
      {rows && rows.length > 0 && (
        <div className="space-y-1.5">
          {rows.map((r) => (
            <button key={r.rid} onClick={() => setRid(r.rid)}
                    className="w-full rounded border bg-card/50 px-2.5 py-2 text-left hover:bg-accent/50">
              <div className="flex items-center gap-1.5 text-[11px]">
                <Badge variant="outline" className="h-4 shrink-0 px-1 text-[10px]">
                  {r.provider || "llm"}
                </Badge>
                <span className="truncate">{r.model || "—"}</span>
                <span className="flex-1" />
                <span className="shrink-0 text-muted-foreground">
                  {(r.bytes / 1024).toFixed(1)} KB
                </span>
              </div>
              <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                <span>{fmtDateTimeMin(r.ts)}</span>
                <span>·</span>
                <span>{r.messages} 条消息</span>
                <span>·</span>
                <span>tool 结果 {r.tool_results}</span>
                <span>·</span>
                <span>system {r.system_chars.toLocaleString()} 字
                  {r.system_first ? "（首位）" : ""}
                  {r.system_in_user ? "（user-content 兼容通道）" : ""}</span>
                {r.truncated && <span>· 正文超限未存</span>}
                {r.wire_bytes > 0 && (
                  <span className={r.wire_match ? "text-(--viz-sev-low)" : undefined}>
                    · 出网字节 {r.wire_match ? "一致" : "≠ 本地序列化（字段同）"}
                  </span>
                )}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function RequestDetail({ pid, sessionId, rid, onBack }: {
  pid: string
  sessionId: string
  rid: string
  onBack: () => void
}) {
  const [d, setD] = useState<DiagRequestDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    setD(null); setErr(null)
    api.diagRequest(pid, sessionId, rid).then(setD).catch(() => setErr("请求体加载失败"))
  }, [pid, sessionId, rid])

  if (err) return <div className="p-4 text-xs text-muted-foreground">{err}</div>
  if (!d) return <div className="p-4 text-xs text-muted-foreground">加载中…</div>

  const body = d.request || {}
  const msgs: Record<string, unknown>[] = Array.isArray(body.messages)
    ? (body.messages as Record<string, unknown>[])
    : Array.isArray(body.input) ? (body.input as Record<string, unknown>[]) : []
  const tools: Record<string, unknown>[] = Array.isArray(body.tools)
    ? (body.tools as Record<string, unknown>[]) : []
  // system：标准字段（system / instructions / role=system 首条）优先；都没有则可能是
  // user-content 兼容通道（网关丢 system，providers.json system_in_user）——从首条
  // user 消息的边界标记里取回，仍然按 system 展示，避免误读成「没注入」。
  const SYS_BEGIN = "<<<CYBERSTRIKE-SYSTEM-PROMPT-BEGIN>>>"
  const SYS_END = "<<<CYBERSTRIKE-SYSTEM-PROMPT-END>>>"
  const extractInline = (): string => {
    for (const m of msgs) {
      if (m.role !== "user") continue
      const c = m.content
      if (typeof c !== "string" || !c.startsWith(SYS_BEGIN)) continue
      const end = c.indexOf(SYS_END)
      return c.slice(SYS_BEGIN.length, end >= 0 ? end : undefined).replace(/^\n+|\n+$/g, "")
    }
    return ""
  }
  let system = typeof body.system === "string" ? body.system
    : typeof body.instructions === "string" ? body.instructions : ""
  let systemViaUser = false
  if (!system) {
    for (const m of msgs) {
      if (m.role === "system" && typeof m.content === "string") {
        system = m.content
        break
      }
    }
  }
  if (!system) {
    system = extractInline()
    systemViaUser = Boolean(system)
  }

  const copy = () => {
    navigator.clipboard.writeText(JSON.stringify(body, null, 2)).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }).catch(() => {})
  }

  return (
    <div className="p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
        <Button size="sm" variant="ghost" className="h-6 px-2 text-[10px]"
                onClick={onBack}>
          ← 列表
        </Button>
        <Badge variant="outline" className="h-4 px-1 text-[10px]">{d.provider}</Badge>
        <span>{d.model || "—"}</span>
        <span>·</span>
        <span>{d.format || "—"}</span>
        <span>·</span>
        <span>{(d.bytes / 1024).toFixed(1)} KB</span>
        <span>·</span>
        <span>sha {d.sha256.slice(0, 12)}</span>
        <span>·</span>
        <span>{fmtDateTimeMin(d.ts)}</span>
        <span className="flex-1" />
        <Button size="sm" variant="ghost" className="h-6 px-2 text-[10px]"
                onClick={copy}>
          {copied ? "已复制" : "复制 JSON"}
        </Button>
      </div>
      <div className="mb-2 break-all text-[10px] text-muted-foreground">{d.url}</div>
      {/* 字节级自证：本地 payload vs httpx 层录到的真实出网字节 */}
      <div className="mb-2 flex flex-wrap items-center gap-2 rounded border bg-card/50 px-2.5 py-1.5 text-[10px] text-muted-foreground">
        <span>本地 payload {d.bytes.toLocaleString()} B · sha {d.sha256.slice(0, 12)}</span>
        <span>→</span>
        <span>真实出网 {(d.wire_bytes || 0).toLocaleString()} B · sha {(d.wire_sha256 || "—").slice(0, 12)}</span>
        <span className={cn("rounded px-1.5 py-0.5",
                           d.wire_match
                             ? "bg-(--viz-sev-low)/15 text-(--viz-sev-low)"
                             : "bg-muted text-muted-foreground")}>
          {d.wire_match ? "字节一致 ✓" : "字节不同（SDK 重序列化，字段一致）"}
        </span>
      </div>
      {d.truncated && (
        <div className="mb-2 rounded border border-(--status-error)/40 bg-(--status-error)/10 px-2 py-1 text-[10px] text-(--status-error)">
          正文超过存储上限，只保留元信息（调大 CYBERSTRIKE_LLM_DUMP_STORE_MB 可放宽）
        </div>
      )}
      {system && (
        <details className="mb-2 rounded border bg-card/50">
          <summary className="cursor-pointer px-2.5 py-1.5 text-[11px]">
            system · {system.length.toLocaleString()} 字
            {systemViaUser
              ? "（经 user-content 兼容通道下发：该供应商网关丢弃 system 字段）"
              : d.system_first ? "（在 messages[0]，原样随请求发出）" : "（独立字段）"}
          </summary>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all border-t p-2.5 text-[11px] leading-relaxed">
            {system}
          </pre>
        </details>
      )}
      {msgs.map((m, i) => {
        const role = String(m.role || "?")
        const calls = Array.isArray(m.tool_calls) ? m.tool_calls as Record<string, unknown>[] : []
        const content = m.content
        const chars = typeof content === "string" ? content.length
          : Array.isArray(content) ? JSON.stringify(content).length : 0
        return (
          <details key={i} className="mb-1.5 rounded border bg-card/50">
            <summary className="cursor-pointer px-2.5 py-1.5 text-[11px]">
              <span className="mr-1.5 font-mono text-[10px] text-muted-foreground">
                {i + 1}
              </span>
              <span className={cn(
                "mr-1.5",
                role === "system" && "text-primary",
                role === "tool" && "text-(--viz-sev-low)",
                role === "assistant" && "text-primary")}>
                {role}
              </span>
              <span className="text-[10px] text-muted-foreground">
                {chars.toLocaleString()} 字
                {calls.length ? ` · tool_calls ${calls.length}` : ""}
                {role === "tool" && m.tool_call_id
                  ? ` · ← ${String(m.tool_call_id)}` : ""}
              </span>
            </summary>
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all border-t p-2.5 text-[11px] leading-relaxed">
              {JSON.stringify(m, null, 2)}
            </pre>
          </details>
        )
      })}
      {tools.length > 0 && (
        <details className="mb-1.5 rounded border bg-card/50">
          <summary className="cursor-pointer px-2.5 py-1.5 text-[11px]">
            tools · {tools.length} 个
          </summary>
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-all border-t p-2.5 text-[11px] leading-relaxed">
            {JSON.stringify(tools, null, 2)}
          </pre>
        </details>
      )}
    </div>
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

// ---------- 技能使用页签（diagnostics-skills，2026-10-21） ----------
//
// 回答「Agent 到底有没有用技能和知识库」。三个数最要紧：
//   - 注入次数：技能进了 system 提示几次
//   - 打开次数：正文真被 skill_open 几次（真使用）
//   - 打开率 = 打开/注入。**注入很多但打开率 0 的技能**是最该处理的：
//     要么 description 不吸引模型，要么专家 yaml 白名单配错了。
// kb 零命中率单独列出：那是「问了但库里没有」，直接对应知识缺口。

function SkillUsageTab({ pid }: { pid: string }) {
  const [data, setData] = useState<DiagSkillUsage | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const load = () => {
    setLoading(true)
    setErr(null)
    api.diagSkills(pid)
      .then(setData)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }
  useEffect(load, [pid])

  if (err) {
    return <div className="p-4 text-[11px] text-(--status-error)">{err}</div>
  }
  if (!data) {
    return <div className="p-4 text-[11px] text-muted-foreground">加载中…</div>
  }
  const t = data.totals
  const dead = data.skills.filter((s) => (s.injected ?? 0) > 0 && (s.opened ?? 0) === 0)

  const Stat = ({ label, value, hint, warn }: {
    label: string; value: string | number; hint?: string; warn?: boolean
  }) => (
    <div className={cn("rounded border px-3 py-2", warn && "border-(--status-approval)/60")}>
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <b className={cn("text-lg", warn && "text-(--status-approval)")}>{value}</b>
      {hint && <div className="mt-0.5 text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  )

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4">
      <div className="mb-3 flex items-center gap-2">
        <span className="text-[11px] text-muted-foreground">
          统计基于本项目事件表（扫描 {data.events_scanned} 条）
        </span>
        <span className="flex-1" />
        <Button size="sm" variant="ghost" className="h-6 px-2 text-[11px]"
                disabled={loading} onClick={load}>
          {loading ? "刷新中…" : "刷新"}
        </Button>
      </div>

      <div className="mb-4 grid max-w-3xl grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="技能注入" value={t.skill_injected}
              hint={`${t.distinct_skills_opened} 个技能被打开过`} />
        <Stat label="skill_open" value={t.skill_open}
              warn={t.skill_injected > 0 && t.skill_open === 0}
              hint={t.skill_injected > 0 && t.skill_open === 0
                ? "注入了但从未打开" : undefined} />
        <Stat label="kb_search" value={t.kb_search} hint={`kb_open ${t.kb_open} 次`} />
        <Stat label="检索零命中" value={t.kb_search_zero}
              warn={(t.kb_zero_rate ?? 0) >= 0.5}
              hint={t.kb_zero_rate === null ? "无检索记录"
                : `占全部检索 ${(t.kb_zero_rate * 100).toFixed(0)}%`} />
      </div>

      {dead.length > 0 && (
        <div className="mb-3 max-w-3xl rounded border border-(--status-approval)/60 p-2.5">
          <p className="text-[11px] font-semibold text-(--status-approval)">
            {dead.length} 个技能注入过但从未被打开
          </p>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            技能进了系统提示，模型一次都没 skill_open 读正文。可能是 description
            不够吸引、与任务不匹配，或专家 yaml 白名单配多了。
          </p>
          <p className="mt-1 font-mono text-[10px] text-muted-foreground">
            {dead.map((s) => s.name).join("、")}
          </p>
        </div>
      )}

      <table className="w-full max-w-3xl text-[11px]">
        <thead className="border-b text-left text-muted-foreground">
          <tr>
            <th className="py-1 font-medium">技能</th>
            <th className="w-20 py-1 text-right font-medium">注入</th>
            <th className="w-20 py-1 text-right font-medium">打开</th>
            <th className="w-24 py-1 text-right font-medium">打开率</th>
          </tr>
        </thead>
        <tbody>
          {data.skills.map((s) => (
            <tr key={s.name} className="border-b border-border/40">
              <td className="py-1">{s.name}</td>
              <td className="py-1 text-right font-mono text-muted-foreground">{s.injected}</td>
              <td className={cn("py-1 text-right font-mono",
                s.opened > 0 ? "text-foreground" : "text-muted-foreground/60")}>
                {s.opened}
              </td>
              <td className="py-1 text-right font-mono">
                {s.opened_rate === null ? "—"
                  : <span className={cn(s.opened_rate === 0 && "text-(--status-approval)")}>
                      {(s.opened_rate * 100).toFixed(0)}%
                    </span>}
              </td>
            </tr>
          ))}
          {data.skills.length === 0 && (
            <tr>
              <td colSpan={4} className="py-3 text-muted-foreground">
                暂无技能使用记录。技能注入事件自 2026-10-21 起在对话链上报，
                更早的会话没有该数据。
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
