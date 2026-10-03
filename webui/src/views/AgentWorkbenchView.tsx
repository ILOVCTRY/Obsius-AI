import {
  useCallback, useEffect, useMemo, useRef, useState,
  type MouseEvent as ReactMouseEvent,
} from "react"
import {
  AlertTriangle, Bot, Check, ChevronDown, CircleSlash, Clock3, Cpu, Gauge, Loader2,
  Plug, Plus, Send, Sparkles, Square, Trash2, Wrench, X, Zap,
} from "lucide-react"
import { api } from "@/lib/api"
import type {
  ChatAgent, ChatMcpServer, ChatMessage, ChatThread, ChatThreadError,
} from "@/lib/types"
import type { ProjectDetail, SkillDef } from "@/lib/types"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { FindingsRail } from "@/components/workbench/FindingsRail"
import { cn } from "@/lib/utils"
import { paneMaxWidth } from "@/lib/paneWidth"
import { useEvents } from "@/lib/useEvents"

// 智能体工作台（K9，2026-09-29）：对话式挖洞入口（蛙池式）。
// K9-UI（2026-09-29）：蛙池式结构语言 × 深色黑客风精修——左栏 agent 切换 +
// 线程列表、中栏结构化时间线（头像/作者行/工具折叠块/进度待办卡/空态 hero）、
// 底部输入大卡片（agent pill + 胶囊能力钮 + 渐变发送/停止）。样式集中在
// index.css `.wb-*` 段；动效尊重 prefers-reduced-motion。左栏分割线可拖宽
// （180–420px，键盘 ←/→ 微调）。数据链路不变：REST 2s 轮询为主，
// chat.delta 事件做实时输入行增强。

const ORCHESTRATOR = "chat-orchestrator"
const CHIPS = [
  "对目标站点做信息收集与资产测绘",
  "测试目标网站是否存在 SQL 注入漏洞",
  "检测目标系统的水平越权漏洞",
  "扫描目标目录与敏感信息泄露",
]

type PanelTab = "agents" | "skills" | "mcp" | null
type SlashState = { mode: "menu" | "skills" | "mcp" }

// 斜杠命令（K9-C）；CTX_LIMIT：上下文窗口分母，默认按 256K 计
const CTX_LIMIT = 256 * 1024
const SLASH_COMMANDS = [
  { cmd: "/context", desc: "查看当前上下文用量明细" },
  { cmd: "/skill", desc: "选择技能，人类本轮指定优先使用" },
  { cmd: "/mcp", desc: "指定 MCP server，本轮优先使用其工具" },
]

const ASIDE_MIN = 180
const ASIDE_DEFAULT = 236
// 左栏最大宽动态取「容器 − 主区保底 − 右栏占位」（见 lib/paneWidth）
const ASIDE_RESERVE = [".wb-rail, .wb-rail-handle"]
const ASIDE_RESERVE_EXTRA = 16

export function AgentWorkbenchView({ pid, meta }: {
  pid: string; meta: ProjectDetail | null
}) {
  const [agents, setAgents] = useState<ChatAgent[]>([])
  const [agentId, setAgentId] = useState<string>(ORCHESTRATOR)
  const [threads, setThreads] = useState<ChatThread[]>([])
  const [tid, setTid] = useState<string | null>(null)
  const [thread, setThread] = useState<ChatThread | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState("")
  const [sending, setSending] = useState(false)
  const [pendingIn, setPendingIn] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [panel, setPanel] = useState<PanelTab>(null)
  const [refsDraft, setRefsDraft] = useState<{ skills: string[]; mcps: string[] }>({ skills: [], mcps: [] })
  const [slash, setSlash] = useState<SlashState | null>(null)
  const [usageOpen, setUsageOpen] = useState(false)
  const [skills, setSkills] = useState<{ name: string; description: string }[]>([])
  const [mcpServers, setMcpServers] = useState<ChatMcpServer[]>([])
  const [agentMenuOpen, setAgentMenuOpen] = useState(false)
  const [asideWidth, setAsideWidth] = useState(ASIDE_DEFAULT)
  const [dragging, setDragging] = useState(false)
  const dragOrigin = useRef<{ x: number; w: number } | null>(null)
  const splitterRef = useRef<HTMLDivElement>(null)

  // 左栏可拖到的最大宽（动态：容器 − 主区保底 − 右栏占位）
  const maxAside = useCallback(
    () => paneMaxWidth(splitterRef.current, ASIDE_RESERVE, ASIDE_RESERVE_EXTRA), [])
  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const { events } = useEvents(pid)

  const currentAgent = agents.find((a) => a.id === agentId)

  // 分割线拖宽：mousedown 记起点，document mousemove 调宽，mouseup 收尾
  const onSplitterDown = (e: ReactMouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    dragOrigin.current = { x: e.clientX, w: asideWidth }
    setDragging(true)
  }
  useEffect(() => {
    if (!dragging) return
    const move = (e: MouseEvent) => {
      const o = dragOrigin.current
      if (o) setAsideWidth(Math.max(ASIDE_MIN, Math.min(maxAside(), o.w + (e.clientX - o.x))))
    }
    const up = () => setDragging(false)
    document.addEventListener("mousemove", move)
    document.addEventListener("mouseup", up)
    document.body.classList.add("wb-dragging")
    return () => {
      document.removeEventListener("mousemove", move)
      document.removeEventListener("mouseup", up)
      document.body.classList.remove("wb-dragging")
      dragOrigin.current = null
    }
  }, [dragging, maxAside])

  // ---- 加载 ----
  useEffect(() => {
    let alive = true
    api.chatAgents(pid).then((as) => { if (alive) setAgents(as) }).catch(() => {})
    return () => { alive = false }
  }, [pid])

  const reloadThreads = useCallback((aid: string) => {
    api.chatThreads(pid, aid).then(setThreads).catch(() => setThreads([]))
  }, [pid])

  useEffect(() => { reloadThreads(agentId); setTid(null); setThread(null); setMessages([]) },
    [agentId, reloadThreads])

  useEffect(() => {
    if (!tid) { setThread(null); setMessages([]); return }
    let alive = true
    const load = () => api.chatThread(tid).then((d) => {
      if (!alive) return
      setThread(d.thread)
      setMessages(d.messages)
      // 对账乐观气泡：持久化的 user 消息出现后移除本地占位
      setPendingIn((cur) => cur && d.messages.some(
        (m) => m.role === "user" && m.content === cur) ? null : cur)
    }).catch(() => {})
    load()
    const timer = setInterval(load, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [tid])

  useEffect(() => { reloadThreads(agentId) }, [thread?.status, agentId, reloadThreads])

  useEffect(() => {
    if ((panel !== "skills" && slash?.mode !== "skills") || skills.length) return
    const packs = [...new Set([...(meta?.capabilities ?? []), ...(meta?.track ? [meta.track] : [])])]
    Promise.all(packs.map((p) => api.capSkills(p).catch(() => [] as SkillDef[])))
      .then((lists) => setSkills(lists.flat().map((s) => ({
        name: s.name, description: (s as { description?: string }).description ?? "",
      }))))
      .catch(() => {})
  }, [panel, slash, skills.length, meta])
  useEffect(() => {
    if (panel !== "mcp" && slash?.mode !== "mcp") return
    api.chatMcp(pid).then((r) => setMcpServers(r.servers)).catch(() => {})
  }, [panel, slash, pid])

  // ---- 流式 delta / 工具活动（事件管道增强） ----
  const liveDelta = useMemo(() => {
    let text = ""
    for (const e of events) {
      if (e.kind !== "chat.delta") continue
      const p = e.payload as { thread_id?: string; text?: string }
      if (p?.thread_id === tid && typeof p.text === "string") text = p.text
    }
    return text
  }, [events, tid])
  // chat.tool 本线程最新事件：phase=start → 实时「执行中」块（先于轮询）；
  // phase=done → 立即标完成态（ok/耗时），轮询结果行到达后接管
  const liveTool = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.kind !== "chat.tool") continue
      const p = e.payload as {
        thread_id?: string; phase?: string; name?: string; args_head?: string
        ok?: boolean; duration_s?: number; result_head?: string
      }
      if (p?.thread_id !== tid) continue
      return p
    }
    return null
  }, [events, tid])
  const liveToolStart = liveTool?.phase === "start" ? liveTool : null
  const liveToolDone = liveTool?.phase === "done" ? liveTool : null
  // 传输层 524 重试进度：provider 通过 chat.retry 事件实时上报，避免长时间
  // 只显示「思考中」让人误以为请求已经卡死。
  const liveRetry = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.kind === "chat.retry") {
        const p = e.payload as { thread_id?: string; attempt?: number; total?: number; status?: number }
        if (p?.thread_id === tid) return p
      }
      if (e.kind === "chat.message") {
        const p = e.payload as { thread_id?: string; role?: string }
        if (p?.thread_id === tid && p.role === "user") break
      }
    }
    return null
  }, [events, tid])
  // assistant(tool_calls) 已落库但部分调用尚无结果行 → 锚定转圈块（轮询滞后 ≤2s）
  const hasPendingCall = useMemo(() => messages.some((m) =>
    m.role === "assistant" && (m.tool_calls ?? []).some((tc) =>
      !messages.some((x) => x.role === "tool" && x.tool_use_id === tc.id))), [messages])
  const running = thread?.status === "running"

  // 上下文用量（K9-C / ct-8）：分母=后端下发的真实硬窗口（缺省回落 256K）；
  // 软上限=有效工作窗口（支持 1M≠在 1M 最好），超此会触发压缩，作为警戒线。
  const usage = thread?.usage ?? null
  const ctxLimit = usage?.ctx_limit ?? CTX_LIMIT
  const ctxSoft = usage?.ctx_soft ?? ctxLimit
  const usagePct = Math.min(100, Math.round(((usage?.input ?? 0) / ctxLimit) * 100))
  const softPct = ctxSoft > 0 ? (usage?.input ?? 0) / ctxSoft : 0
  // 档位按软上限（警戒线）判定：<70% 青 / 70~90% 琥珀 / >90% 红（接近压缩）
  const usageLevel = softPct >= 0.9 ? "is-high" : softPct >= 0.7 ? "is-warn" : "is-ok"
  const softLeftPct = ctxLimit > 0 ? Math.min(100, (ctxSoft / ctxLimit) * 100) : 100
  const showSoftLine = ctxSoft < ctxLimit
  // 上下文构成（Claude Code /context 式）：后端估算+真值归一的分类 breakdown
  const bk = usage?.breakdown
  const usageFree = Math.max(0, ctxLimit - (usage?.input ?? 0))
  const usageSegs = bk
    ? ([
        { key: "system", label: "系统提示", tokens: bk.system, cls: "seg-sys" },
        { key: "tools", label: "工具定义", tokens: bk.tools, cls: "seg-tools" },
        { key: "refs", label: "本轮注入", tokens: bk.refs, cls: "seg-refs" },
        { key: "messages", label: "会话消息", tokens: bk.messages, cls: "seg-msgs" },
      ] as const).filter((s) => s.tokens > 0)
    : []

  // 切线程时收起斜杠面板与用量浮层
  useEffect(() => { setUsageOpen(false); setSlash(null) }, [tid])

  // chat.message(user) 事件对账乐观气泡（比轮询更快）
  useEffect(() => {
    if (!pendingIn) return
    for (const e of events) {
      if (e.kind !== "chat.message") continue
      const p = e.payload as { thread_id?: string; role?: string; text?: string }
      if (p?.thread_id !== tid || p.role !== "user" || typeof p.text !== "string") continue
      if (p.text === pendingIn || (p.text.length >= 2000 && pendingIn.startsWith(p.text))) {
        setPendingIn(null); return
      }
    }
  }, [events, tid, pendingIn])

  // ---- 动作 ----
  // 线程切换统一入口：草稿按线程内存保留（__new=新建态独立槽），切走存、切入载
  const draftsRef = useRef(new Map<string, string>())
  const selectThread = useCallback((nextId: string | null) => {
    const curKey = tid ?? "__new"
    const nextKey = nextId ?? "__new"
    if (curKey === nextKey) return
    draftsRef.current.set(curKey, input)
    setTid(nextId)
    setInput(draftsRef.current.get(nextKey) ?? "")
    setRefsDraft({ skills: [], mcps: [] })
  }, [tid, input])

  // 发送 = 乐观上屏（用户气泡 + 思考占位先行）；POST 为 202 异步起线程，
  // 上屏由事件/轮询对账驱动，不再阻塞等全量 GET（修「发送后卡一下」）
  const send = useCallback(async (text?: string) => {
    const body = (text ?? input).trim()
    if (!body || sending) return
    setInput(""); setErr(null); setPendingIn(body)
    draftsRef.current.set(tid ?? "__new", "")
    const refs = refsDraft.skills.length || refsDraft.mcps.length ? refsDraft : null
    try {
      let id = tid
      if (!id) {
        const t = await api.chatThreadCreate(pid, agentId)
        id = t.id; setTid(id)
      }
      await api.chatSend(id, body, refs)
      setRefsDraft({ skills: [], mcps: [] })
    } catch (e) {
      setPendingIn(null)
      setErr(e instanceof Error ? e.message : String(e))
    }
    finally { setSending(false); inputRef.current?.focus() }
  }, [input, sending, tid, pid, agentId, refsDraft])

  const removeThread = useCallback(async (id: string) => {
    try {
      await api.chatThreadDelete(id)
      draftsRef.current.delete(id)
      if (id === tid) selectThread(null)
      reloadThreads(agentId)
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }, [tid, agentId, reloadThreads, selectThread])

  const stop = useCallback(async () => {
    if (!tid) return
    try { await api.chatStop(tid) } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }, [tid])

  // 自动滚底
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, liveDelta, running])

  const agentInitial = (a: ChatAgent | undefined) =>
    (a?.name ?? "?").slice(0, 1)

  // ---- 斜杠命令 / 引用 chips（K9-C） ----
  const runSlash = (cmd: string) => {
    if (cmd === "/context") { setUsageOpen(true); setSlash(null); setInput(""); inputRef.current?.focus() }
    else if (cmd === "/skill") { setSlash({ mode: "skills" }); setInput("") }
    else if (cmd === "/mcp") { setSlash({ mode: "mcp" }); setInput("") }
  }
  const addRef = (kind: "skills" | "mcps", name: string) => {
    setRefsDraft((r) => (r[kind].includes(name) ? r : { ...r, [kind]: [...r[kind], name] }))
    setSlash(null); setInput(""); inputRef.current?.focus()
  }
  const removeRef = (kind: "skills" | "mcps", name: string) =>
    setRefsDraft((r) => ({ ...r, [kind]: r[kind].filter((x) => x !== name) }))
  const onInputChange = (v: string) => {
    setInput(v)
    if (slash?.mode === "menu") {
      if (!v.startsWith("/") || v.includes(" ")) setSlash(null)
    } else if (!slash && v.startsWith("/") && !v.includes(" ")) {
      setSlash({ mode: "menu" })
    }
  }

  // 斜杠面板候选项：菜单按「/后文本」过滤，二级模式按输入框文本过滤
  const slashQuery = (slash?.mode === "menu" ? input.replace(/^\//, "") : slash ? input : "").trim().toLowerCase()
  const slashCmds = SLASH_COMMANDS.filter((c) => c.cmd.slice(1).includes(slashQuery))
  const slashSkills = skills.filter((s) => `${s.name} ${s.description}`.toLowerCase().includes(slashQuery))
  const slashMcps = mcpServers.filter((s) => `${s.name} ${s.domains.join("/")}`.toLowerCase().includes(slashQuery))

  // ---- 渲染 ----
  return (
    <div className="wb-shell">
      {/* 左栏：agent 切换器 + 线程列表 */}
      <aside className="wb-aside" style={{ width: asideWidth, flexBasis: asideWidth }}>
        <div className="wb-aside-head">
          <button className="wb-agent-trigger" onClick={() => setAgentMenuOpen((v) => !v)}>
            <span className={cn("wb-avatar", agentId === ORCHESTRATOR && "is-orch")}>
              {agentId === ORCHESTRATOR ? <Bot size={14} /> : agentInitial(currentAgent)}
            </span>
            <span className="min-w-0 flex-1">
              <span className="wb-agent-name">{currentAgent?.name ?? agentId}</span>
              <span className="wb-agent-role">{agentId === ORCHESTRATOR ? "主控" : "专家"}</span>
            </span>
            <ChevronDown size={13} className="shrink-0 text-muted-foreground" />
          </button>
          {agentMenuOpen && (
            <div className="wb-agent-menu">
              <div className="wb-agent-menu-label">智能体</div>
              {agents.map((a) => (
                <button key={a.id}
                  className={cn("wb-agent-option", a.id === agentId && "is-current")}
                  onClick={() => { setAgentId(a.id); setAgentMenuOpen(false) }}>
                  <span className={cn("wb-avatar", a.id === ORCHESTRATOR && "is-orch")}>
                    {a.id === ORCHESTRATOR ? <Bot size={13} /> : agentInitial(a)}
                  </span>
                  <span className="wb-agent-option-body">
                    <span className="wb-agent-option-name">
                      {a.name}
                      {a.id === agentId && <Check size={12} className="wb-check" />}
                    </span>
                    <span className="wb-agent-option-desc">{a.description}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="wb-aside-sub">
          会话线程
          <button className="wb-thread-new" onClick={() => selectThread(null)}>
            <Plus size={11} /> 新建
          </button>
        </div>
        <div className="wb-thread-list">
          {threads.map((t) => (
            <div key={t.id} className={cn("wb-thread", t.id === tid && "is-active")}
              onClick={() => selectThread(t.id)} title={t.title || "(未命名)"}>
              <span className={cn("wb-dot", t.status === "running" && "is-running",
                t.status === "error" && "is-error")} />
              <span className="min-w-0 flex-1">
                <span className="wb-thread-title">{t.title || "(未命名)"}</span>
                <span className="wb-thread-meta">
                  {t.parent_thread_id && <span>↳ spawn</span>}
                  <Clock3 size={9} />
                  {fmtTime(t.updated_at)}
                  <span>·</span>
                  {t.todo.filter((x) => x.status === "completed").length}/{t.todo.length} 待办
                </span>
              </span>
              <button className="wb-thread-del" title="删除线程"
                onClick={(ev) => { ev.stopPropagation(); void removeThread(t.id) }}>
                <Trash2 size={12} />
              </button>
            </div>
          ))}
          {!threads.length && <div className="wb-aside-empty">暂无线程，发送消息或点「新建」</div>}
        </div>
      </aside>
      <div
        ref={splitterRef}
        className={cn("wb-splitter", dragging && "is-dragging")}
        role="separator"
        aria-orientation="vertical"
        aria-label="拖动调整侧栏宽度"
        tabIndex={0}
        onMouseDown={onSplitterDown}
        onKeyDown={(e) => {
          if (e.key === "ArrowLeft") setAsideWidth((w) => Math.max(ASIDE_MIN, w - 16))
          if (e.key === "ArrowRight") setAsideWidth((w) => Math.min(maxAside(), w + 16))
        }}
      />

      {/* 中栏：头 + 时间线 + 输入大卡片 */}
      <main className="wb-main">
        <div className="wb-header">
          <span className={cn("wb-avatar", agentId === ORCHESTRATOR && "is-orch")}>
            {agentId === ORCHESTRATOR ? <Bot size={14} /> : agentInitial(currentAgent)}
          </span>
          <span className="wb-header-title">{thread?.title || currentAgent?.name || "智能体工作台"}</span>
          {running && <span className="wb-badge"><span className="wb-dot is-running" />执行中</span>}
          {thread?.status === "error" && (
            <span className="wb-badge is-error"
              title={thread.error ? `${thread.error.title}：${thread.error.hint}` : undefined}>
              出错
            </span>
          )}
          {thread?.parent_thread_id && <span className="wb-badge is-ghost">子专家线程</span>}
        </div>

        <div ref={scrollRef} className="wb-stream">
          {(!tid && !pendingIn) ? (
            <Hero agentName={currentAgent?.name} isOrch={agentId === ORCHESTRATOR} />
          ) : (
            <div className="wb-timeline">
              {thread && thread.todo.length > 0 && <TodoCard todo={thread.todo} />}
              {messages.map((m) => {
                if (m.role === "tool") return null
                if (m.role === "user") {
                  return (
                    <div key={m.id} className="wb-row is-user wb-anim">
                      <div className="wb-bubble-user">{m.content}</div>
                    </div>
                  )
                }
                const stopped = m.content.includes("已按人类要求停止")
                const calls = m.tool_calls ?? []
                return (
                  <div key={m.id} className="wb-row wb-anim">
                    <span className={cn("wb-avatar", "is-sm", agentId === ORCHESTRATOR && "is-orch")}>
                      {agentId === ORCHESTRATOR ? <Bot size={11} /> : agentInitial(currentAgent)}
                    </span>
                    <div className="wb-msg">
                      <div className="wb-msg-head">
                        <span className="wb-msg-author">{currentAgent?.name ?? agentId}</span>
                        {stopped && <span className="wb-badge is-ghost">已停止</span>}
                      </div>
                      {(m.content || !calls.length) && (
                        <div className={cn("wb-card", stopped && "is-stopped")}>
                          {m.content
                            ? <MarkdownView content={m.content} prefix={`chat-${m.id}`} className="text-[12px]" />
                            : <span className="text-muted-foreground">（调用工具中…）</span>}
                        </div>
                      )}
                      {calls.map((tc) => {
                        const tr = messages.find(
                          (x) => x.role === "tool" && x.tool_use_id === tc.id)
                        const live = !tr && liveToolDone
                          && liveToolDone.name === tc.name
                          && liveToolDone.args_head === JSON.stringify(tc.args).slice(0, 300)
                          ? liveToolDone : null
                        return (
                          <ToolBlock key={tc.id} name={tc.name} args={tc.args}
                            result={tr?.content} done={!!tr} live={live} />
                        )
                      })}
                    </div>
                  </div>
                )
              })}
              {pendingIn && (
                <div className="wb-row is-user wb-anim">
                  <div className="wb-bubble-user">{pendingIn}</div>
                </div>
              )}
              {(running || !!pendingIn) && liveDelta && liveDelta !== lastAssistantText(messages) && (
                <div className="wb-row wb-anim">
                  <span className={cn("wb-avatar is-sm", agentId === ORCHESTRATOR && "is-orch")}>
                    {agentId === ORCHESTRATOR ? <Bot size={11} /> : agentInitial(currentAgent)}
                  </span>
                  <div className="wb-msg">
                    <div className="wb-msg-head"><span className="wb-msg-kind">正在输入</span></div>
                    <div className="wb-typing">
                      <span className="wb-caret" />
                      <span className="wb-typing-text">{liveDelta.slice(-600)}</span>
                    </div>
                  </div>
                </div>
              )}
              {running && liveToolStart && !hasPendingCall && (
                <div className="wb-row wb-anim">
                  <span className={cn("wb-avatar is-sm", agentId === ORCHESTRATOR && "is-orch")}>
                    {agentId === ORCHESTRATOR ? <Bot size={11} /> : agentInitial(currentAgent)}
                  </span>
                  <div className="wb-msg">
                    <ToolBlock name={liveToolStart.name ?? "?"}
                      argsHead={liveToolStart.args_head} done={false} />
                  </div>
                </div>
              )}
              {(running || !!pendingIn) && !liveDelta && !liveToolStart && !hasPendingCall && (
                <div className="wb-row wb-anim">
                  <span className={cn("wb-avatar is-sm", agentId === ORCHESTRATOR && "is-orch")}>
                    {agentId === ORCHESTRATOR ? <Bot size={11} /> : agentInitial(currentAgent)}
                  </span>
                  <div className="wb-msg">
                    <div className="wb-msg-head"><span className="wb-msg-kind">
                      {liveRetry
                        ? `正在第 ${liveRetry.attempt ?? "?"}/${liveRetry.total ?? "?"} 次重试`
                        : "思考中"}
                    </span></div>
                    <div className="wb-typing"><span className="wb-caret" /></div>
                  </div>
                </div>
              )}
              {thread?.status === "error" && thread.error && !running && (
                <div className="wb-row wb-anim">
                  <span className="wb-avatar is-sm"><CircleSlash size={11} /></span>
                  <div className="wb-msg">
                    <ThreadErrorCard error={thread.error} />
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {err && <div className="wb-err">{err}</div>}

        {/* 底部 dock：chips + 输入大卡片 + 能力面板 */}
        <div className="wb-dock">
          <div className="wb-chips">
            {CHIPS.map((c) => (
              <button key={c} className="wb-chip" onClick={() => void send(c)}>{c}</button>
            ))}
          </div>

          {(refsDraft.skills.length > 0 || refsDraft.mcps.length > 0) && (
            <div className="wb-refs">
              {refsDraft.skills.map((s) => (
                <button key={`sk:${s}`} className="wb-ref-chip" title="点击移除"
                  onClick={() => removeRef("skills", s)}>
                  <Zap size={10} /> {s} <X size={10} />
                </button>
              ))}
              {refsDraft.mcps.map((m) => (
                <button key={`mcp:${m}`} className="wb-ref-chip" title="点击移除"
                  onClick={() => removeRef("mcps", m)}>
                  <Cpu size={10} /> {m} <X size={10} />
                </button>
              ))}
            </div>
          )}

          {slash && (
            <div className="wb-panel wb-anim">
              <div className="wb-panel-inner">
                {slash.mode === "menu" && (
                  <>
                    {slashCmds.map((c) => (
                      <button key={c.cmd} className="wb-slash-item" onClick={() => runSlash(c.cmd)}>
                        <b>{c.cmd}</b>
                        <span>{c.desc}</span>
                      </button>
                    ))}
                    {!slashCmds.length && <div className="wb-kv">无匹配命令（Esc 关闭后可直接发送）</div>}
                  </>
                )}
                {slash.mode === "skills" && (
                  <>
                    {slashSkills.map((s) => (
                      <button key={s.name} className="wb-slash-item" onClick={() => addRef("skills", s.name)}>
                        <b>{s.name}</b>
                        <span>{s.description}</span>
                      </button>
                    ))}
                    {!skills.length && <div className="wb-kv">技能清单加载中或为空</div>}
                    {!!skills.length && !slashSkills.length && <div className="wb-kv">无匹配技能</div>}
                  </>
                )}
                {slash.mode === "mcp" && (
                  <>
                    {slashMcps.map((s) => (
                      <button key={s.name} className="wb-slash-item" onClick={() => addRef("mcps", s.name)}>
                        <b>{s.name}</b>
                        <span>{s.transport} · {s.domains.join("/")} · {s.tools.length} 工具</span>
                      </button>
                    ))}
                    {!mcpServers.length && <div className="wb-kv">无启用的 MCP server（设置页可配置）</div>}
                    {!!mcpServers.length && !slashMcps.length && <div className="wb-kv">无匹配 MCP server</div>}
                  </>
                )}
              </div>
            </div>
          )}

          {usageOpen && (
            <div className="wb-panel wb-anim">
              <div className="wb-panel-inner">
                <div className="wb-usage-head">
                  <Gauge size={12} className="text-primary" />
                  <b>上下文用量</b>
                  <span className="wb-thread-meta">{usage ? `分母 ${Math.round(ctxLimit / 1024)}K${showSoftLine ? ` · 软上限 ${Math.round(ctxSoft / 1024)}K` : ""}` : "暂无数据"}</span>
                  <button className="wb-usage-close" title="关闭" onClick={() => setUsageOpen(false)}>
                    <X size={12} />
                  </button>
                </div>
                {usage ? (
                  <>
                    <div className="wb-usage-head-row">
                      <b className="wb-usage-total">{fmtTokens(usage.input ?? 0)} / {Math.round(ctxLimit / 1024)}K</b>
                      <span className={cn("wb-usage-pct-big", usageLevel)}>{usagePct}%</span>
                    </div>
                    {bk ? (
                      <>
                        <div className="wb-usage-segs" role="img" aria-label="上下文构成分段条">
                          {usageSegs.map((s) => (
                            <i key={s.key} className={s.cls}
                              style={{ width: `${(s.tokens / ctxLimit) * 100}%` }}
                              title={`${s.label} ${fmtTokens(s.tokens)}（${Math.round((s.tokens / ctxLimit) * 100)}%）`} />
                          ))}
                          {usageFree > 0 && <i className="seg-free" style={{ width: `${(usageFree / ctxLimit) * 100}%` }} title={`剩余 ${fmtTokens(usageFree)}`} />}
                          {showSoftLine && <i className="seg-soft" style={{ left: `${softLeftPct}%` }} title={`软上限警戒线 ${Math.round(ctxSoft / 1024)}K`} />}
                        </div>
                        <div className="wb-usage-legend">
                          {usageSegs.map((s) => (
                            <div className="wb-usage-li" key={s.key}>
                              <i className={cn("wb-usage-dot", s.cls)} />
                              <b className="shrink-0">{s.label}</b>
                              <span className="wb-usage-num">{fmtTokens(s.tokens)} · {Math.round((s.tokens / ctxLimit) * 100)}%</span>
                            </div>
                          ))}
                          <div className="wb-usage-li">
                            <i className={cn("wb-usage-dot", "seg-free")} />
                            <b className="shrink-0">剩余空间</b>
                            <span className="wb-usage-num">{fmtTokens(usageFree)} · {Math.max(0, 100 - usagePct)}%</span>
                          </div>
                          {showSoftLine && (
                            <div className="wb-usage-li">
                              <i className={cn("wb-usage-dot", "seg-soft")} />
                              <b className="shrink-0">软上限</b>
                              <span className="wb-usage-num">{Math.round(ctxSoft / 1024)}K 起触发压缩</span>
                            </div>
                          )}
                        </div>
                      </>
                    ) : (
                      <div className="wb-usage-bar"><i className={usageLevel} style={{ width: `${usagePct}%` }} />{showSoftLine && <i className="seg-soft" style={{ left: `${softLeftPct}%` }} />}</div>
                    )}
                    <div className="wb-kv"><b className="shrink-0">输出（累计）</b><span className="wb-usage-num">{fmtTokens(usage.output ?? 0)}</span></div>
                    <div className="wb-kv"><b className="shrink-0">模型步数</b><span className="wb-usage-num">{usage.steps ?? 0}</span></div>
                    <div className="wb-kv"><b className="shrink-0">缓存读取</b><span className="wb-usage-num">{fmtTokens(usage.cache_read ?? 0)}</span></div>
                    <div className="wb-kv"><b className="shrink-0">缓存写入</b><span className="wb-usage-num">{fmtTokens(usage.cache_creation ?? 0)}</span></div>
                    {!bk && <div className="wb-kv is-hint">发送一条消息后生成分类构成（系统提示/工具/消息占比）</div>}
                  </>
                ) : (
                  <div className="wb-kv">本轮会话还没有用量数据，发送一条消息后生成</div>
                )}
              </div>
            </div>
          )}

          <div className="wb-composer">
            <div className="wb-composer-head">
              <button className="wb-composer-agent" onClick={() => setAgentMenuOpen(true)}>
                <span className={cn("wb-avatar is-sm", agentId === ORCHESTRATOR && "is-orch")}>
                  {agentId === ORCHESTRATOR ? <Bot size={11} /> : agentInitial(currentAgent)}
                </span>
                {currentAgent?.name ?? agentId}
                <ChevronDown size={11} className="opacity-70" />
              </button>
              <button className={cn("wb-usage", usageLevel)}
                title="上下文用量（点击看明细，或输入 /context）" onClick={() => setUsageOpen((v) => !v)}>
                <UsageRing pct={usagePct} />
                <span className="wb-usage-pct">{usagePct}%</span>
              </button>
              <span className="wb-composer-status">
                {(running || !!pendingIn || sending)
                  ? <><span className="wb-dot is-running" />正在执行 · 可停止</>
                  : <><span className="wb-dot" />待命</>}
              </span>
            </div>
            <textarea
              ref={inputRef}
              rows={2}
              value={input}
              placeholder={running ? "当前线程执行中，可点「停止」中止本轮…" : "描述测试意图，回车发送（Shift+回车 换行，/ 唤出命令）"}
              onChange={(e) => onInputChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape" && slash) { setSlash(null); e.preventDefault(); return }
                if (e.key !== "Enter" || e.shiftKey) return
                e.preventDefault()
                if (slash) {
                  if (slash.mode === "menu") { if (slashCmds[0]) runSlash(slashCmds[0].cmd) }
                  else if (slash.mode === "skills") { if (slashSkills[0]) addRef("skills", slashSkills[0].name) }
                  else if (slashMcps[0]) addRef("mcps", slashMcps[0].name)
                  return
                }
                void send()
              }}
            />
            <div className="wb-composer-bar">
              {(["agents", "skills", "mcp"] as const).map((t) => {
                const Icon = t === "agents" ? Bot : t === "skills" ? Zap : Plug
                return (
                  <button key={t} className={cn("wb-cap", panel === t && "is-active")}
                    onClick={() => setPanel(panel === t ? null : t)}>
                    <Icon size={12} />
                    {t === "agents" ? "Agents" : t === "skills" ? "Skills" : "MCP"}
                  </button>
                )
              })}
              <span className="wb-spacer" />
              {(running || !!pendingIn || sending) ? (
                <button className="wb-send is-stop" onClick={() => void stop()}>
                  <Square size={11} /> 停止
                </button>
              ) : (
                <button className="wb-send" disabled={!input.trim() || sending} onClick={() => void send()}>
                  {sending ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />} 发送
                </button>
              )}
            </div>
          </div>

          {panel && (
            <div className="wb-panel wb-anim">
              <div className="wb-panel-inner">
                {panel === "agents" && (
                  <div className="wb-panel-grid">
                    {agents.map((a) => (
                      <button key={a.id}
                        className={cn("wb-panel-card", a.id === agentId && "is-current")}
                        onClick={() => { setAgentId(a.id); setPanel(null) }}>
                        <div className="flex items-center gap-1.5 text-[11.5px] font-semibold">
                          {a.id === ORCHESTRATOR ? <Bot size={11} className="text-primary" /> : <Sparkles size={11} className="text-muted-foreground" />}
                          {a.name}
                        </div>
                        <div className="wb-panel-line mt-0.5">{a.description}</div>
                      </button>
                    ))}
                  </div>
                )}
                {panel === "skills" && (
                  <div>
                    {skills.map((s) => (
                      <button key={s.name} className="wb-kv wb-kv-click" title="点击添加到本轮（仅当轮生效）"
                        onClick={() => addRef("skills", s.name)}>
                        <b className="shrink-0">{s.name}</b>
                        <span className="truncate">{s.description}</span>
                        <Plus size={11} className="wb-kv-add" />
                      </button>
                    ))}
                    {!skills.length && <div className="wb-kv">技能清单加载中或为空</div>}
                  </div>
                )}
                {panel === "mcp" && (
                  <div className="grid gap-2">
                    {mcpServers.map((s) => (
                      <button key={s.name} className="wb-mcp-row" title="点击添加到本轮（仅当轮生效）"
                        onClick={() => addRef("mcps", s.name)}>
                        <div className="flex items-center gap-1.5 text-[11px] font-semibold">
                          <Cpu size={11} className="text-primary" />
                          {s.name}
                          <span className={cn("wb-dot", s.online && "is-running")} />
                          <span className="wb-thread-meta">{s.transport} · {s.domains.join("/")}</span>
                          {s.session_scoped && <span className="wb-thread-meta">项目内嵌</span>}
                          <Plus size={11} className="wb-kv-add" />
                        </div>
                        <div className="wb-panel-line mt-0.5 pl-5">
                          {s.tools.length
                            ? `已发现 ${s.tools.length} 个工具：${s.tools.map((t) => t.name).join("、")}`
                            : "（工具发现失败或为空，请检查 MCP 进程）"}
                        </div>
                      </button>
                    ))}
                    {!mcpServers.length && <div className="wb-kv">无启用的 MCP server（设置页可配置）</div>}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </main>

      {/* 右栏：漏洞/发现（默认收起；2026-10-01）——链路视图内联于此 */}
      <FindingsRail pid={pid} track={meta?.track} />
    </div>
  )
}

// 用量圆环（K9-C）：环形进度，颜色档位由外层 .wb-usage.is-* 控制
function UsageRing({ pct }: { pct: number }) {
  const r = 6.5
  const c = 2 * Math.PI * r
  return (
    <svg viewBox="0 0 18 18" width={16} height={16} aria-hidden>
      <circle className="wb-ring-bg" cx="9" cy="9" r={r} fill="none" strokeWidth="2.6" />
      {pct > 0 && (
        <circle className="wb-ring-fg" cx="9" cy="9" r={r} fill="none" strokeWidth="2.6"
          strokeDasharray={`${(pct / 100) * c} ${c}`} strokeLinecap="round"
          transform="rotate(-90 9 9)" />
      )}
    </svg>
  )
}

function fmtTokens(n: number): string {
  return n >= 1024 ? `${(n / 1024).toFixed(1)}K` : String(n)
}

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso.endsWith("Z") || iso.includes("T") ? iso : iso.replace(" ", "T") + "Z")
    return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
  } catch { return "" }
}

// 轮次失败错误卡片（2026-10-01）：分类标题 + 友好原因/建议 + 可折叠技术细节。
// 后端在 round 失败时落 chat_threads.error（status=error），此处渲染于时间线末尾。
function ThreadErrorCard({ error }: { error: ChatThreadError }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="wb-card wb-error-card">
      <div className="wb-error-head">
        <AlertTriangle size={13} className="wb-error-icon" />
        <span className="wb-error-title">本轮执行失败 · {error.title}</span>
        <span className="wb-error-cat">{error.category}</span>
      </div>
      <p className="wb-error-hint">{error.hint}</p>
      <button type="button" className="wb-error-toggle" onClick={() => setOpen((v) => !v)}>
        <ChevronDown size={11} className={cn("wb-error-caret", open && "is-open")} />
        {open ? "收起技术细节" : "展开技术细节"}
      </button>
      {open && <pre className="wb-error-detail">{error.message}</pre>}
    </div>
  )
}

function Hero({ agentName, isOrch }: { agentName?: string; isOrch: boolean }) {
  return (
    <div className="wb-hero">
      <div className="wb-hero-mark"><Bot size={24} /></div>
      <h2>{agentName ? `与 ${agentName} 开始对话` : "选择智能体开始对话"}</h2>
      <p>
        {isOrch
          ? "主控会把测试意图拆解为待办并委派专家执行；专家线程全程留档，可随时回看与追问。"
          : "直接下达该专家负责范围内的测试任务；过程与结论都会落档。"}
      </p>
      <div className="wb-hero-hint"><Wrench size={10} /> AGENTS · SKILLS · MCP · 输入 / 唤出命令</div>
    </div>
  )
}

function lastAssistantText(messages: ChatMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--)
    if (messages[i].role === "assistant" && messages[i].content) return messages[i].content
  return ""
}

function TodoCard({ todo }: { todo: { id: string; title: string; status: string }[] }) {
  const done = todo.filter((t) => t.status === "completed").length
  const pct = todo.length ? Math.round((done / todo.length) * 100) : 0
  return (
    <div className="wb-todo">
      <div className="wb-todo-head">
        <CircleSlash size={11} /> 待办
        <span className="wb-todo-count">{done}/{todo.length} 完成 · {pct}%</span>
      </div>
      <div className="wb-todo-bar"><i style={{ width: `${pct}%` }} /></div>
      {todo.map((t) => (
        <div key={t.id} className={cn("wb-todo-item",
          t.status === "completed" && "is-done",
          t.status === "in_progress" && "is-doing")}>
          <span className="wb-todo-glyph">
            {t.status === "completed" ? "✓" : t.status === "in_progress" ? "▸" : "○"}
          </span>
          <span>{t.title}</span>
        </div>
      ))}
    </div>
  )
}

function ToolBlock({ name, args, argsHead, result, done, live }: {
  name: string; args?: Record<string, unknown>; argsHead?: string
  result?: string; done: boolean
  live?: { ok?: boolean; duration_s?: number; result_head?: string } | null
}) {
  const [open, setOpen] = useState(false)
  // ok 三态来源：持久化结果行 → 「[错误]」前缀约定；事件实时态 → 后端结构化 ok
  const ok = done ? !(result ?? "").startsWith("[错误")
    : live ? live.ok !== false : true
  return (
    <div className={cn("wb-tool", open && "is-open", !done && "is-running", !ok && "is-error")}>
      <button className="wb-tool-head" onClick={() => setOpen((v) => !v)}>
        <span className="wb-tool-icon">
          {!done ? <Loader2 size={10} className="animate-spin" />
            : ok ? <Wrench size={10} /> : <CircleSlash size={10} />}
        </span>
        <span className="wb-tool-name">{name}</span>
        <span className="wb-tool-args">
          {args ? JSON.stringify(args).slice(0, 140) : argsHead ?? ""}
        </span>
        {!done && <span className="wb-tool-live">运行中</span>}
        {done && live && <span className="wb-tool-live">{live.duration_s ?? "?"}s</span>}
        {done && <ChevronDown size={12} className="wb-tool-chev" />}
      </button>
      {open && (
        <div className="wb-tool-body">
          {args != null && (
            <>
              <div className="wb-tool-kicker">参数</div>
              <pre className="wb-tool-pre">{JSON.stringify(args, null, 2).slice(0, 2000)}</pre>
            </>
          )}
          {args == null && argsHead != null && (
            <>
              <div className="wb-tool-kicker">参数</div>
              <pre className="wb-tool-pre">{argsHead}</pre>
            </>
          )}
          <div className="wb-tool-kicker">结果</div>
          <pre className="wb-tool-pre">
            {done ? (result ?? "").slice(0, 6000)
              : live?.result_head ? live.result_head.slice(0, 600) : "执行中…"}
          </pre>
        </div>
      )}
    </div>
  )
}
