import {
  useCallback, useEffect, useMemo, useRef, useState,
  type MouseEvent as ReactMouseEvent,
} from "react"
import {
  AlertTriangle, Bot, Check, ChevronDown, CircleSlash, Clock3, Cpu, Gauge, Loader2,
  Plug, Plus, Send, Sparkles, Square, Trash2, Wrench, X, Zap,
} from "lucide-react"
import { api, ApiError } from "@/lib/api"
import type {
  ChatAgent, ChatMcpServer, ChatMessage, ChatThread, ChatThreadError,
} from "@/lib/types"
import type { Approval, DecideApprovalResult, ProjectDetail, SkillDef } from "@/lib/types"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { WorkbenchContextRail } from "@/components/workbench/WorkbenchContextRail"
import { ActivityGroup } from "@/components/chat/ActivityGroup"
import { ApprovalCard } from "@/components/chat/ApprovalCard"
import { FileCardList } from "@/components/chat/FileCard"
import { ChatStats } from "@/components/chat/ChatStats"
import { ThinkingBlock } from "@/components/chat/ThinkingBlock"
import { usePendingApprovals } from "@/lib/usePendingApprovals"
import { mergeTargets, relToWorkdir, targetsFromProse, targetsFromTool, type FileTarget } from "@/lib/fileTargets"
import type { ActivityStep } from "@/lib/activity"
import { parseTs } from "@/lib/datetime"
import { cn } from "@/lib/utils"
import { paneMaxWidth } from "@/lib/paneWidth"
import { useEvents } from "@/lib/useEvents"

// 智能体工作台（K9，2026-09-29）：对话式挖洞入口（蛙池式）。
// K9-UI（2026-09-29）：蛙池式结构语言 × 深色黑客风精修——左栏 agent 切换 +
// 线程列表、中栏时间线、底部输入大卡片（agent pill + 胶囊能力钮 + 渐变发送/停止）。
// **会话流改造（2026-10-03，cc-haha 风格）**：中栏时间线由「气泡 + 卡片 + 逐个工具块」
// 改为**无气泡 prose + 折叠活动摘要 + 文件卡**——用户消息一行 `❯`、agent 叙述平铺
// markdown、一轮内连续工具调用折成一行灰色摘要（`components/chat/ActivityGroup`）、
// 叙述提到的文件渲染为卡片（`components/chat/FileCard`）、顶部统计行
// （`components/chat/ChatStats`）。左栏/底部 dock/右栏不动。样式集中在
// index.css `.wb-*` 段（会话流部分已迁至 Tailwind 工具类，见 components/chat/CLAUDE.md）；
// 动效尊重 prefers-reduced-motion。左栏分割线可拖宽（180–420px，键盘 ←/→ 微调）。
// 数据链路不变：REST 2s 轮询为主，chat.delta 事件做实时输入行增强。

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
  const [loaded, setLoaded] = useState(false)
  const [workDir, setWorkDir] = useState<string | null>(null)
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
  // flushMs=50（≈20Hz）：工作台流式期要跟得上后端 ~20Hz 的 delta 投递，否则前端
  // 100ms 批合并会把输出压成「一顿顿的」（2026-10-04 顺滑化）。
  const { events } = useEvents(pid, null, { flushMs: 50 })

  // 待审批（2026-10-04）：工作台时间线是 ChatMessage[]，审批事件不在其中 → 用「贴 composer
  // 的悬浮卡」呈现项目待审批单；新 approval.requested 到达即唤醒重拉。
  const aprWake = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) if (events[i].kind === "approval.requested") return events[i].id
    return 0
  }, [events])
  const { items: pendingApprovals } = usePendingApprovals(pid, aprWake)
  const decideApproval = useCallback(async (approval: Approval, decision: "approved" | "rejected", note: string): Promise<DecideApprovalResult> => {
    const text = note.trim()
    if (text && approval.session_id) {
      try { await api.sessionNote(approval.session_id, text) } catch { /* 引导失败不阻断决策 */ }
    }
    return api.decideApproval(approval.id, decision)
  }, [])

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

  // 轮询拉线程详情（2s）。load 存进 ref，供「用户消息事件到达」时即时补拉。
  // **2026-10-04 修「发送后空白」**：乐观行 pendingIn 只在「持久化的 user 消息
  // 真的进了 messages」时清除——此前 WS 的 chat.message(user) 事件一到就清，
  // 而 messages 只靠 2s 轮询刷新，事件先于轮询到达时时间线会空一段。
  const reloadRef = useRef<(() => void) | null>(null)
  useEffect(() => {
    if (!tid) { setThread(null); setMessages([]); setLoaded(false); reloadRef.current = null; return }
    let alive = true
    setLoaded(false)
    const load = () => api.chatThread(tid).then((d) => {
      if (!alive) return
      setThread(d.thread)
      setMessages(d.messages)
      setWorkDir(d.work_dir ?? null)
      setLoaded(true)
      // 对账乐观行：持久化的 user 消息出现后才移除本地占位
      setPendingIn((cur) => cur && d.messages.some(
        (m) => m.role === "user" && m.content === cur) ? null : cur)
    }).catch(() => {})
    reloadRef.current = load
    load()
    const timer = setInterval(load, 2000)
    return () => { alive = false; clearInterval(timer); reloadRef.current = null }
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
  // 思考内容与回复分开组装：后端发送的是累计全文，因此每个新事件直接覆盖。
  // 只消费当前用户消息之后的事件，避免上一轮思考在新消息等待时短暂闪现。
  const liveThinking = useMemo(() => {
    let text = ""
    let started = false
    for (const e of events) {
      const p = e.payload as { thread_id?: string; role?: string; text?: string; thinking?: string }
      if (e.kind === "chat.message" && p?.thread_id === tid && p.role === "user") {
        started = true
        text = ""
        continue
      }
      if (!started || p?.thread_id !== tid) continue
      if (e.kind === "chat.thinking.delta" && typeof p.text === "string") text = p.text
      else if (e.kind === "chat.thinking" && typeof p.thinking === "string") text = p.thinking
    }
    return text
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

  // chat.message(user) 事件到达 → 立即补拉一次线程详情（比 2s 轮询快）。
  // **不在这里清 pendingIn**——清除只发生在 load() 确认该消息已进 messages 之后，
  // 否则事件先到、轮询未到时会闪空白（2026-10-04 修）。
  useEffect(() => {
    if (!pendingIn || !tid) return
    for (const e of events) {
      if (e.kind !== "chat.message") continue
      const p = e.payload as { thread_id?: string; role?: string }
      if (p?.thread_id === tid && (p.role === "user" || p.role === "assistant")) { reloadRef.current?.(); return }
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
    setSending(true)
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
    if (!tid || !running) return
    try {
      await api.chatStop(tid)
      reloadRef.current?.()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        reloadRef.current?.()
        return
      }
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [tid, running])

  // 自动滚底（含思考增量——此前漏 liveThinking，思考增长时不跟滚，观感「卡住」）
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, liveDelta, liveThinking, running])

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
  // 会话流渲染项（cc-haha 风格）：连续工具调用折成一行活动摘要。**memo 化**——
  // messages 只在轮询（2s）时换新数组，避免每个事件帧都重算整条时间线（卡顿源之一）。
  const items = useMemo(() => buildWbItems(messages), [messages])
  // 空态判定（2026-10-04）：无线程且无乐观行、或线程已加载但确实为空 → 回退 Hero，
  // 不再留一片空白（此前选中空线程 / 发送后竞态窗口会全空）。
  const showHero = !tid
    ? !pendingIn
    : loaded && messages.length === 0 && !pendingIn && !running
      && !(thread?.todo?.length) && !thread?.error
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

      {/* 中栏：时间线 + 输入大卡片；标题栏信息并入统计行 */}
      <main className="wb-main">
        {/* 统计行（会话流改造 2026-10-03）：token/最后更新/条数——工作台侧取精确值 */}
        {tid && (
          <ChatStats
            tokens={(usage?.input ?? 0) + (usage?.output ?? 0)
              + (usage?.cache_read ?? 0) + (usage?.cache_creation ?? 0)}
            cachedTokens={(usage?.cache_read ?? 0) + (usage?.cache_creation ?? 0)}
            updatedAt={thread?.updated_at}
            count={messages.length}
            endAdornment={
              <>
                {running && <span className="wb-badge"><span className="wb-dot is-running" />执行中</span>}
                {thread?.status === "error" && (
                  <span className="wb-badge is-error"
                    title={thread.error ? `${thread.error.title}：${thread.error.hint}` : undefined}>出错</span>
                )}
                {thread?.parent_thread_id && <span className="wb-badge is-ghost">子专家线程</span>}
              </>
            }
          />
        )}

        <div ref={scrollRef} className="wb-stream">
          {showHero ? (
            <Hero agentName={currentAgent?.name} isOrch={agentId === ORCHESTRATOR} />
          ) : (
            <div className="wb-timeline">
              {thread && thread.todo.length > 0 && <TodoCard todo={thread.todo} />}
              {items.map((it) => {
                if (it.kind === "user") {
                  return <HumanNote key={it.m.id} m={it.m} />
                }
                if (it.kind === "thinking") {
                  return <ThinkingBlock key={it.id} content={it.content} className="wb-anim" />
                }
                if (it.kind === "prose") {
                  const stopped = it.m.content.includes("已按人类要求停止")
                  return (
                    <div key={it.m.id} className="wb-anim py-0.5">
                      <div className="flex items-baseline gap-2">
                        <MarkdownView content={it.m.content} prefix={`chat-${it.m.id}`}
                          className="min-w-0 flex-1 text-[12.5px] leading-relaxed text-foreground/90" />
                        {stopped && <span className="wb-badge is-ghost shrink-0">已停止</span>}
                      </div>
                      <FileCardList targets={relTargets(mergeTargets(it.targets, it.known), workDir)}
                        pid={pid} />
                    </div>
                  )
                }
                // activity：一轮内连续工具调用折成一行摘要，展开看逐条明细
                return (
                  <ActivityGroup key={it.id} steps={it.steps} live={running}
                    failedCount={it.steps.filter((s) => s.failed).length}
                    className="wb-anim">
                    {it.rows.map((r) => r.kind === "thinking"
                      ? <ThinkingBlock key={r.id} content={r.content} />
                      : <ToolBlock key={r.id} name={r.name} args={r.args}
                          result={r.result} done={r.done}
                          live={!r.done && liveToolDone
                            && liveToolDone.name === r.name
                            && liveToolDone.args_head === JSON.stringify(r.args ?? {}).slice(0, 300)
                            ? liveToolDone : null} />)}
                  </ActivityGroup>
                )
              })}
              {pendingIn && <HumanNote m={null} text={pendingIn} />}
              {/* 实时叠加层（会话流改造后统一为平铺 prose + 光标，无气泡无卡片） */}
              {(running || !!pendingIn) && liveDelta && liveDelta !== lastAssistantText(messages) && (
                <div className="wb-anim py-0.5 text-[12.5px] leading-relaxed text-foreground/90">
                  <span className="whitespace-pre-wrap break-words">{liveDelta.slice(-600)}</span>
                  <span className="ml-0.5 inline-block h-3.5 w-[2px] animate-pulse bg-primary align-middle" />
                </div>
              )}
              {(running || !!pendingIn) && liveThinking
                && liveThinking !== lastPersistedThinking(messages) && (
                <ThinkingBlock content={liveThinking} active={running && !liveDelta}
                  className="wb-anim" />
              )}
              {running && liveToolStart && !hasPendingCall && (
                <div className="wb-anim">
                  <ToolBlock name={liveToolStart.name ?? "?"}
                    argsHead={liveToolStart.args_head} done={false} />
                </div>
              )}
              {(running || !!pendingIn) && !liveDelta && !liveThinking && !liveToolStart && !hasPendingCall && (
                <div className="wb-anim py-0.5 text-[12px] text-muted-foreground">
                  {liveRetry
                    ? `正在第 ${liveRetry.attempt ?? "?"}/${liveRetry.total ?? "?"} 次重试`
                    : "思考中"}
                  <span className="ml-0.5 inline-block h-3.5 w-[2px] animate-pulse bg-primary align-middle" />
                </div>
              )}
              {thread?.status === "error" && thread.error && !running && (
                <div className="wb-anim">
                  <ThreadErrorCard error={thread.error} />
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

          {/* 待审批问答卡（贴 composer；本页即对话页 → 隐藏「和 Agent 聊聊」） */}
          {pendingApprovals.length > 0 && (
            <div className="mb-2">
              <ApprovalCard approval={pendingApprovals[0]} onDecide={decideApproval} showChat={false} />
              {pendingApprovals.length > 1 && (
                <p className="mt-1 text-center text-[10px] text-muted-foreground">
                  还有 {pendingApprovals.length - 1} 条待确认
                </p>
              )}
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
              {running ? (
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

      {/* 右栏：会话上下文 + 漏洞/发现（默认收起） */}
      <WorkbenchContextRail pid={pid} tid={tid} workDir={workDir} track={meta?.track} />
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
    <div className="rounded-xl border border-(--status-error)/40 bg-(--status-error)/5 px-3.5 py-2.5 text-[12.5px]">
      <div className="mb-1.5 flex items-center gap-2">
        <AlertTriangle size={13} className="shrink-0 text-(--status-error)" />
        <span className="text-[12.5px] font-semibold text-(--status-error)">本轮执行失败 · {error.title}</span>
        <span className="ml-auto shrink-0 rounded-full border border-(--status-error)/35 px-2 py-0.5 font-mono text-[9px] tracking-wide text-(--status-error)/90">
          {error.category}
        </span>
      </div>
      <p className="text-[11.5px] leading-relaxed text-muted-foreground">{error.hint}</p>
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="mt-2 inline-flex items-center gap-1 text-[10px] text-primary hover:underline">
        <ChevronDown size={11} className={cn("transition-transform", open && "rotate-180")} />
        {open ? "收起技术细节" : "展开技术细节"}
      </button>
      {open && (
        <pre className="mt-2 max-h-56 overflow-auto rounded-md bg-background/70 p-2.5 font-mono text-[10.5px] leading-relaxed whitespace-pre-wrap break-words text-(--status-error)/80">
          {error.message}
        </pre>
      )}
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

// ---------- 会话流装配（2026-10-03 cc-haha 风格） ----------
// ChatMessage[] → 渲染项：连续 assistant(tool_calls) + tool(result) 归入同一
// activity 组（折成一行摘要）；有正文的 assistant 走 prose 行 + 文件卡。
// 纯函数，不碰 React——与直播间 lib/turnStream.ts 的 buildStreamItems 同思路，
// 但消息模型不同（ChatMessage[] vs BBEvent[]）故各自实现。

// 活动组内一行：工具调用 或 该步的思考（思考折进行内，摘要也能带上「思考」计数）
type GroupRow =
  | { kind: "tool"; id: string; name: string; args?: Record<string, unknown>
      result?: string; done: boolean }
  | { kind: "thinking"; id: string; content: string }

type WbItem =
  | { kind: "user"; m: ChatMessage }
  | { kind: "thinking"; id: string; content: string }
  | { kind: "activity"; id: string; steps: ActivityStep[]; rows: GroupRow[] }

/** 时间戳解析：与 lib/datetime.ts parseTs 同口径（含 packs 紧凑形态） */
function tsOf(iso: string): number {
  const t = parseTs(iso).getTime()
  return Number.isFinite(t) ? t : 0
}

// 叙述段（prose）——含所属轮的工具目标（文件卡对账用，渲染时才合并，保持纯函数）
type WbProse = { kind: "prose"; m: ChatMessage; targets: FileTarget[]; known: FileTarget[] }

/** ChatMessage[] → 渲染项。连续 assistant(tool_calls)+tool(result) 归入同一 activity 组。 */
function buildWbItems(messages: ChatMessage[]): (WbItem | WbProse)[] {
  const out: (WbItem | WbProse)[] = []
  let pending: { steps: ActivityStep[]; rows: GroupRow[]; known: FileTarget[] } | null = null
  const flush = () => {
    if (!pending) return
    out.push({ kind: "activity", id: `act-${pending.rows[0]?.id ?? out.length}`,
               steps: pending.steps, rows: pending.rows })
    pending = null
  }
  for (const m of messages) {
    if (m.role === "user") { flush(); out.push({ kind: "user", m }); continue }
    if (m.role === "tool") continue  // 结果挂在对应 tool_call 上，不单独出行
    const calls = m.tool_calls ?? []
    const thinking = (m.thinking ?? "").trim() ? (m.thinking as string) : ""
    if (!calls.length) {
      flush()
      // 思考先于正文成行（cc-haha 风格：单行折叠，展开看全文）
      if (thinking) out.push({ kind: "thinking", id: `th-${m.id}`, content: thinking })
      out.push({ kind: "prose", m, targets: targetsFromProse(m.content, []), known: [] })
      continue
    }
    // 有工具调用的 assistant：正文（若有）先出 prose，工具并进当前 activity 组
    if (m.content) {
      flush()
      // 有正文时思考单独成行（先于正文）——否则并进后面的活动组会落到正文下方
      if (thinking) out.push({ kind: "thinking", id: `th-${m.id}`, content: thinking })
      out.push({ kind: "prose", m: { ...m, tool_calls: [] },
                 targets: targetsFromProse(m.content, []), known: [] })
    }
    if (!pending) pending = { steps: [], rows: [], known: [] }
    // 无正文时思考并进活动组：摘要能带上「思考」计数（对齐 cc-haha）
    if (thinking && !m.content) {
      pending.rows.push({ kind: "thinking", id: `th-${m.id}`, content: thinking })
      pending.steps.push({ kind: "thinking", name: "thinking", ts: tsOf(m.created_at) })
    }
    for (const tc of calls) {
      const tr = messages.find((x) => x.role === "tool" && x.tool_use_id === tc.id)
      pending.rows.push({ kind: "tool", id: tc.id, name: tc.name, args: tc.args,
                          result: tr?.content, done: !!tr })
      pending.steps.push({
        kind: tc.name === "run_cmd" ? "command" : "tool",
        name: tc.name,
        ts: tsOf(m.created_at),
        dedupKey: tc.name === "read_file" ? String(tc.args?.path ?? "") : undefined,
        failed: !!tr && (tr.content ?? "").startsWith("[错误"),
      })
      pending.known.push(...targetsFromTool(tc.name, tc.args, tr?.content))
    }
    // 该轮的工具目标回填给「轮内叙述」——同 basename 时叙述文件卡指向真实路径
    if (pending.known.length) {
      for (let i = out.length - 1; i >= 0; i--) {
        const it = out[i]
        if (it.kind === "activity" || it.kind === "user") break
        if (it.kind === "prose") { it.known = pending.known; break }
      }
    }
  }
  flush()
  return out
}

/** 最后一条已持久化的思考正文——实时思考尾块据此去重（持久化后不再重复显示）。 */
function lastPersistedThinking(messages: ChatMessage[]): string {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === "assistant" && m.thinking) return m.thinking
  }
  return ""
}

/** 文件卡目标归一：工具参数里的工作区绝对路径 → 相对路径（后端 /files/open 口径） */
function relTargets(targets: FileTarget[], workDir: string | null): FileTarget[] {
  return targets.map((t) => {
    const rel = relToWorkdir(t.relPath, workDir)
    return rel === t.relPath ? t
      : { ...t, relPath: rel, name: rel.split("/").pop() ?? rel }
  })
}

// 人类消息行（会话流改造 2026-10-03）：单行 `❯ <text>`——去气泡，对齐 cc-haha。
// m=null 时用 text（乐观上屏的 pendingIn 占位）。
function HumanNote({ m, text }: { m: ChatMessage | null; text?: string }) {
  const body = text ?? m?.content ?? ""
  return (
    <div className="wb-anim py-1 text-[13px] leading-relaxed">
      <span className="mr-1.5 select-none font-mono text-primary">❯</span>
      <span className="whitespace-pre-wrap break-words text-foreground">{body}</span>
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

// 工具明细行（会话流改造 2026-10-03）：折在 ActivityGroup 展开态里的一行，
// 点开看参数/结果原文（Tailwind 工具类，不再用 .wb-tool 卡片样式）。
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
    <div className="rounded-md border border-border/50 bg-card/40">
      <button type="button" onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-2 py-1 text-left">
        <span className={cn("shrink-0", !done ? "text-primary" : ok ? "text-muted-foreground" : "text-(--status-error)")}>
          {!done ? <Loader2 size={10} className="animate-spin" />
            : ok ? <Wrench size={10} /> : <CircleSlash size={10} />}
        </span>
        <span className="shrink-0 font-mono text-[11px] font-semibold text-foreground/85">{name}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground/70">
          {args ? JSON.stringify(args).slice(0, 140) : argsHead ?? ""}
        </span>
        {!done && <span className="shrink-0 font-mono text-[9px] text-primary">运行中</span>}
        {done && live && <span className="shrink-0 font-mono text-[9px] text-muted-foreground/60">{live.duration_s ?? "?"}s</span>}
        {done && <ChevronDown size={12} className={cn("shrink-0 text-muted-foreground transition-transform", open && "rotate-180")} />}
      </button>
      {open && (
        <div className="border-t border-border/50">
          {args != null && (
            <>
              <div className="px-3 pt-1.5 font-mono text-[8px] uppercase tracking-widest text-muted-foreground/70">参数</div>
              <pre className="max-h-56 overflow-auto px-3 pb-2.5 font-mono text-[10px] leading-relaxed whitespace-pre-wrap break-words text-foreground/80">
                {JSON.stringify(args, null, 2).slice(0, 2000)}
              </pre>
            </>
          )}
          {args == null && argsHead != null && (
            <>
              <div className="px-3 pt-1.5 font-mono text-[8px] uppercase tracking-widest text-muted-foreground/70">参数</div>
              <pre className="max-h-56 overflow-auto px-3 pb-2.5 font-mono text-[10px] leading-relaxed whitespace-pre-wrap break-words text-foreground/80">
                {argsHead}
              </pre>
            </>
          )}
          <div className="px-3 pt-1.5 font-mono text-[8px] uppercase tracking-widest text-muted-foreground/70">结果</div>
          <pre className="max-h-56 overflow-auto px-3 pb-2.5 font-mono text-[10px] leading-relaxed whitespace-pre-wrap break-words text-foreground/80">
            {done ? (result ?? "").slice(0, 6000)
              : live?.result_head ? live.result_head.slice(0, 600) : "执行中…"}
          </pre>
        </div>
      )}
    </div>
  )
}
