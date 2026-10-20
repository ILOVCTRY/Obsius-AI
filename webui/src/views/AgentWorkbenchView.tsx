import {
  useCallback, useEffect, useMemo, useRef, useState,
  type MouseEvent as ReactMouseEvent,
} from "react"
import { createPortal } from "react-dom"
import {
  AlertTriangle, Bot, Check, ChevronDown, CircleSlash, Clock3, Cpu, Gauge, Globe,
  Loader2, Paperclip, Plug, Plus, RefreshCw, Send, Sparkles, Square, Trash2,
  Wrench, X, Zap,
} from "lucide-react"
import { api, ApiError } from "@/lib/api"
import type {
  AttachmentInfo, ChatAgent, ChatMcpServer, ChatMessage, ChatThread, ChatThreadError,
  ModelInfo, Asset, ChallengeFlag,
} from "@/lib/types"
import type { Approval, DecideApprovalResult, ProjectDetail } from "@/lib/types"
import { MarkdownView } from "@/components/settings/MarkdownView"
import { WorkbenchContextRail } from "@/components/workbench/WorkbenchContextRail"
import { ActivityGroup } from "@/components/chat/ActivityGroup"
import { ApprovalCard } from "@/components/chat/ApprovalCard"
import { CandidateFlagCard } from "@/components/chat/CandidateFlagCard"
import { FileCardList } from "@/components/chat/FileCard"
import { ChatStats } from "@/components/chat/ChatStats"
import { ThinkingBlock } from "@/components/chat/ThinkingBlock"
import { useCandidateFlags } from "@/lib/useCandidateFlags"
import { usePendingApprovals } from "@/lib/usePendingApprovals"
import { mergeTargets, relToWorkdir, targetsFromProse, targetsFromTool, type FileTarget } from "@/lib/fileTargets"
import type { ActivityStep } from "@/lib/activity"
import { parseTs } from "@/lib/datetime"
import { cn } from "@/lib/utils"
import { paneMaxWidth } from "@/lib/paneWidth"
import { useNavRailSlot } from "@/lib/navRail"
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
  "对资产进行去重",
  "对未测尽的资产继续进行测试",
]

type PanelTab = "agents" | "skills" | "mcp" | null
type SlashState = { mode: "menu" | "skills" | "mcp" }
/** 待发附件（粘贴/拖拽即上传；chip 三态） */
type PendingFile = {
  key: string
  status: "uploading" | "ready" | "error"
  name: string
  size: number
  file: File
  att?: AttachmentInfo
}

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

export function AgentWorkbenchView({ pid, meta, focusThread, onFocusConsumed }: {
  pid: string; meta: ProjectDetail | null
  /** 外部（蓝图产出入口 goto-agents）指定要打开的线程 id；消费后回调清空 */
  focusThread?: string | null
  onFocusConsumed?: () => void
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
  // 停止请求已发出、但后端轮次尚未退出（2026-10-20）：此前点「停止」后按钮
  // 原样不动，而传输层重试退避最长要等 30s 才生效，用户以为按钮失灵。
  // 现置 stopping 让按钮立刻变「停止中…」并禁用，直到 running 转 false。
  const [stopping, setStopping] = useState(false)
  const [pendingIn, setPendingIn] = useState<string | null>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [panel, setPanel] = useState<PanelTab>(null)
  const [refsDraft, setRefsDraft] = useState<{ skills: string[]; mcps: string[] }>({ skills: [], mcps: [] })
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([])
  const [slash, setSlash] = useState<SlashState | null>(null)
  const [usageOpen, setUsageOpen] = useState(false)
  const [skills, setSkills] = useState<{ name: string; description: string }[]>([])
  const [mcpServers, setMcpServers] = useState<ChatMcpServer[]>([])
  const [agentMenuOpen, setAgentMenuOpen] = useState(false)
  // 线程级模型（v37，2026-10-13）：modelInfo=全局供应商/模型清单（懒拉一次）；
  // modelDraft=新建态（tid=null）的选择，创建线程时随 ChatThreadIn 带上。
  const [modelInfo, setModelInfo] = useState<ModelInfo | null>(null)
  const [modelPopOpen, setModelPopOpen] = useState(false)
  const [popProvider, setPopProvider] = useState("")
  const [modelDraft, setModelDraft] = useState<{ provider: string; model: string } | null>(null)
  // 联网搜索开关（v43，web-search-toggle，2026-10-19）：线程级；新建态草稿
  // （同 modelDraft 口径，创建线程时带上）。开 = 本线程主控 + spawn 子代理
  // 默认可用 web_search（SearXNG/ddgs 双后端）；不进专家工具勾选。
  const [webSearchDraft, setWebSearchDraft] = useState(false)
  const [asideWidth, setAsideWidth] = useState(ASIDE_DEFAULT)
  const [dragging, setDragging] = useState(false)
  const dragOrigin = useRef<{ x: number; w: number } | null>(null)
  const splitterRef = useRef<HTMLDivElement>(null)

  // 左栏停靠（2026-10-16）：左导航列下半留了空槽位时，把 agent 切换器 + 会话线程列表
  // portal 过去，与左导航共用同一列（主区因此多出整列宽，见 lib/navRail.ts）。
  // 槽位不存在（窄栏 / 侧栏收起 / 非 agents 视图）→ docked=false，走原来的两列布局。
  const railSlot = useNavRailSlot()
  const docked = !!railSlot

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

  // ---- 线程级模型（v37，2026-10-13）----
  // 当前生效的模型 = 已选线程的覆写 → 未选（新建态）取草稿；空=跟随全局 executor。
  const enabledProviders = useMemo(
    () => (modelInfo?.providers ?? []).filter((p) => p.enabled), [modelInfo])
  const activeModel = useMemo(
    () => (tid
      ? { provider: thread?.provider ?? "", model: thread?.model ?? "" }
      : modelDraft ?? { provider: "", model: "" }),
    [tid, thread?.provider, thread?.model, modelDraft])
  const modelLabel = activeModel.provider
    ? (activeModel.model || `${activeModel.provider} 默认`)
    : "默认模型"
  const openModelPop = () => {
    setPopProvider(activeModel.provider || enabledProviders[0]?.name || "")
    setModelPopOpen((v) => !v)
  }
  // 选定即切：已选线程走 PUT（坏值 422 不落盘），新建态先存草稿、创建线程时带上。
  const chooseModel = useCallback(async (provider: string, model: string) => {
    setModelPopOpen(false)
    if (!tid) { setModelDraft({ provider, model }); return }
    try {
      const t = await api.chatThreadLlm(tid, provider, model)
      setThread((cur) => (cur ? { ...cur, provider: t.provider, model: t.model } : cur))
      setThreads((ts) => ts.map((x) => (x.id === tid
        ? { ...x, provider: t.provider, model: t.model } : x)))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "切换模型失败")
    }
  }, [tid])

  // ---- 联网搜索开关（v43，web-search-toggle）----
  // 当前生效 = 已选线程的落盘值 → 未选（新建态）取草稿。
  const webSearchOn = tid ? !!thread?.web_search : webSearchDraft
  const toggleWebSearch = useCallback(async () => {
    const next = !webSearchOn
    if (!tid) { setWebSearchDraft(next); return }
    try {
      const t = await api.chatThreadWebSearch(tid, next)
      setThread((cur) => (cur ? { ...cur, web_search: t.web_search } : cur))
      setThreads((ts) => ts.map((x) => (x.id === tid
        ? { ...x, web_search: t.web_search } : x)))
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "切换联网搜索失败")
    }
  }, [tid, webSearchOn])

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
    api.assets(pid).then((as) => { if (alive) setAssets(as) }).catch(() => {})
    return () => { alive = false }
  }, [pid])

  // 供应商/模型清单（线程级模型选择浮层的数据源，v37）：全局口径，拉一次即可
  useEffect(() => {
    let alive = true
    api.models().then((m) => { if (alive) setModelInfo(m) }).catch(() => {})
    return () => { alive = false }
  }, [])

  const reloadThreads = useCallback((aid: string) => {
    api.chatThreads(pid, aid).then(setThreads).catch(() => setThreads([]))
  }, [pid])

  // 外部聚焦线程（goto-agents，M2）：agentId 切换会触发下面的重置 effect，故先存
  // ref、切到 ORCHESTRATOR，由重置 effect 消费 ref 选中；agentId 已是 ORCHESTRATOR
  // 时直接 setTid（重置 effect 不重跑）。
  const focusTidRef = useRef<string | null>(null)
  useEffect(() => {
    if (!focusThread) return
    if (agentId === ORCHESTRATOR) {
      setTid(focusThread)
    } else {
      focusTidRef.current = focusThread
      setAgentId(ORCHESTRATOR)
    }
    onFocusConsumed?.()
  }, [focusThread, agentId, onFocusConsumed])

  useEffect(() => {
    reloadThreads(agentId)
    setTid(focusTidRef.current)
    focusTidRef.current = null
    setThread(null); setMessages([])
  }, [agentId, reloadThreads])

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
      // 对账乐观行：持久化的 user 消息出现后才移除本地占位。**带附件时后端会把
      // 「附件：\n- 路径」段拼在正文后**（core/chat/runtime.py run()），故不能用
      // 全等匹配——否则带附件的消息 pendingIn 永不清除，正文会在时间线尾部重影
      // 一次（2026-10-13 修用户实测「用户消息重复渲染」）。取最近一条 user 消息，
      // 允许其后追加附件段（`cur + "\n"` 前缀）。
      setPendingIn((cur) => {
        if (!cur) return cur
        for (let i = d.messages.length - 1; i >= 0; i--) {
          const m = d.messages[i]
          if (m.role !== "user") continue
          return m.content === cur || m.content.startsWith(`${cur}\n`) ? null : cur
        }
        return cur
      })
    }).catch(() => {})
    reloadRef.current = load
    load()
    const timer = setInterval(load, 2000)
    return () => { alive = false; clearInterval(timer); reloadRef.current = null }
  }, [tid])

  useEffect(() => { reloadThreads(agentId) }, [thread?.status, agentId, reloadThreads])

  // 候选 flag 确认卡（ctf-flag-wake，2026-10-20）：子代理提交候选 flag 时后端会
  // 唤醒 CTF 解题主控说一段话（`core/chat/ctf_wake.py`），本卡贴 composer 上方，
  // 让人类一键复制 flag、一键确认（accepted）或标记为错误（rejected）。
  // **只在本线程绑定了 CTF 题目时启用**：challengeId 为空 → hook 空转、零请求，
  // 非 CTF 项目/普通专家线程完全不受影响。
  const challengeId = thread?.challenge_asset_id || null
  const flagWake = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const k = events[i].kind
      if (k === "challenge.flag.recorded" || k === "challenge.flag.updated"
          || k === "challenge.flag.decided") return events[i].id
    }
    return 0
  }, [events])
  const { flags: candidateFlags, refresh: refreshCandidates } =
    useCandidateFlags(challengeId, flagWake)
  const [decideFlagBusy, setDecideFlagBusy] = useState(false)
  const decideCandidateFlag = useCallback(
    async (flag: ChallengeFlag, verdict: "accepted" | "rejected", reason?: string) => {
      if (!challengeId) return
      setDecideFlagBusy(true)
      try {
        await api.decideChallengeFlag(challengeId, flag.id, { verdict, reason })
        refreshCandidates()
        reloadRef.current?.()
      } finally {
        setDecideFlagBusy(false)
      }
    }, [challengeId, refreshCandidates])

  useEffect(() => {
    if ((panel !== "skills" && slash?.mode !== "skills") || skills.length) return
    api.skills()
      .then((list) => setSkills(list.map((s) => ({
        name: s.name, description: (s as { description?: string }).description ?? "",
      }))))
      .catch(() => {})
  }, [panel, slash, skills.length])
  // MCP 面板：结果**缓存**，反复点开不再每次重新请求（后端 status() 会逐 server
  // 探活 tools/list，点一次刷一次既慢又闪）。首次点开拉一次，之后复用；需要时点
  // 面板内「刷新」强制重探（MCP 进程后起时用）。切项目（pid 变）即失效重拉。
  const mcpLoadedRef = useRef(false)
  useEffect(() => { mcpLoadedRef.current = false; setMcpServers([]) }, [pid])
  const loadMcp = useCallback((force = false) => {
    if (mcpLoadedRef.current && !force) return
    api.chatMcp(pid)
      .then((r) => { setMcpServers(r.servers); mcpLoadedRef.current = true })
      .catch(() => {})
  }, [pid])
  useEffect(() => {
    if (panel !== "mcp" && slash?.mode !== "mcp") return
    loadMcp()
  }, [panel, slash, loadMcp])

  // ---- 流式 delta / 工具活动（事件管道增强） ----
  // 只消费**当前用户消息之后**的事件（与 liveThinking 同款守卫，2026-10-12 修）：
  // 新一轮开始时上一轮的 chat.delta 仍留在事件流里，若不设起点会被当作本轮流式
  // 文本，以**原始 markdown** 渲染在时间线尾部——上一轮正文明明已落库、按 prose
  // 走 MarkdownView 渲染，却又以 raw 重影出现在末尾（用户实测「新一轮时上一轮
  // 输出不渲染」）。chat.delta 携带**累计全文**，故必须整段丢弃而非只丢最新一条。
  const liveDelta = useMemo(() => {
    let text = ""
    let started = false
    for (const e of events) {
      const p = e.payload as { thread_id?: string; role?: string; text?: string }
      if (e.kind === "chat.message" && p?.thread_id === tid && p.role === "user") {
        started = true
        text = ""
        continue
      }
      if (!started || e.kind !== "chat.delta" || p?.thread_id !== tid) continue
      if (typeof p.text === "string") text = p.text
    }
    return text
  }, [events, tid])
  // chat.tool 按 tool_call_id 累积：同一批并行专家共享主控 thread_id，不能只取
  // 最新一条事件，否则后一个 start 会覆盖先完成的 done。旧事件没有 id 时保留
  // name + args_head 回退匹配，兼容历史事件缓存。
  const liveTools = useMemo(() => {
    type LiveTool = {
      tool_call_id?: string; phase?: string; name?: string; args_head?: string
      ok?: boolean; duration_s?: number; result_head?: string
    }
    const byId = new Map<string, LiveTool>()
    const legacy: LiveTool[] = []
    // start 事件时刻（2026-10-13 修「已运行 0s」）：会话流里的挂起工具行靠它计时，
    // 之前只有底部实时行带时刻、持久化行没传 startedAt → 挂起工具永远显示 0s。
    const startAtById = new Map<string, string>()
    const legacyStarts: { name?: string; args_head?: string; created_at: string }[] = []
    for (const e of events) {
      if (e.kind !== "chat.tool") continue
      const p = e.payload as LiveTool & { thread_id?: string }
      if (p?.thread_id !== tid) continue
      if (p.tool_call_id) byId.set(p.tool_call_id, p)
      else legacy.push(p)
      if (p.phase === "start") {
        if (p.tool_call_id) startAtById.set(p.tool_call_id, e.created_at)
        else legacyStarts.push({ name: p.name, args_head: p.args_head, created_at: e.created_at })
      }
    }
    return { byId, legacy, startAtById, legacyStarts }
  }, [events, tid])
  // 底部实时工具行（「运行中」）判据（2026-10-13 修「重启后幽灵运行中」）：只认
  // 「本线程最近一条 chat.tool 事件是 start」且该 tool_call_id **尚无持久化结果行**。
  // 旧实现取 byId 里最后一条 phase=start 的事件——中断/重启遗留的孤儿 start（后端
  // 只补 chat_messages 结果行、没补 done 事件）在 Map 里永久驻留，被当成本轮正在跑
  // 的工具（用户实测：中断前的 list_symbols 重启后仍显示「运行中」）。改按事件流
  // 最后一条判定 + 已落库结果行交叉排除孤儿。
  const liveToolStart = useMemo(() => {
    type LiveTool = { tool_call_id?: string; phase?: string; name?: string; args_head?: string }
    const answered = new Set<string>()
    for (const m of messages) {
      if (m.role === "tool" && m.tool_use_id) answered.add(m.tool_use_id)
    }
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.kind !== "chat.tool") continue
      const p = e.payload as LiveTool & { thread_id?: string }
      if (p?.thread_id !== tid) continue
      if (p.phase !== "start") return null            // 最近一条已 done → 无工具在跑
      if (p.tool_call_id && answered.has(p.tool_call_id)) return null  // 结果已落库=遗留
      // 带上 start 事件时刻 → 工具行显示「已运行 Ns」（逐秒刷新，2026-10-13）
      return { ...p, created_at: e.created_at }
    }
    return null
  }, [events, tid, messages])
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
  // 缓存命中（2026-10-13）：口径 = **累计**（跨步自加，见 core/chat/runtime.py）——
  // 命中率 = cache_read_total / input_total，分段 token 同样取累计读/写/未缓存。
  // 首步冷缓存必 0%，显「首步」而非 0%。
  const cache = (() => {
    const total = usage?.input_total ?? 0
    if (!usage || total <= 0) return null
    const read = usage.cache_read_total ?? 0
    const write = usage.cache_creation_total ?? 0
    const uncached = Math.max(0, total - read - write)
    const pct = (n: number) => Math.round((n / total) * 100)
    const hitPct = pct(read)
    return {
      hitPct,
      cold: (usage.steps ?? 0) <= 1,
      level: hitPct >= 60 ? "is-ok" : hitPct >= 30 ? "is-warn" : "is-high",
      segs: [
        { key: "read", label: "缓存读", tokens: read, pct: pct(read), cls: "seg-cache-read" },
        { key: "write", label: "缓存写", tokens: write, pct: pct(write), cls: "seg-cache-write" },
        { key: "uncached", label: "未缓存", tokens: uncached, pct: pct(uncached), cls: "seg-uncached" },
      ],
    }
  })()

  // 切线程时收起斜杠面板与用量浮层；模型浮层同理，且新建态草稿由线程自身接管
  useEffect(() => {
    setUsageOpen(false); setSlash(null); setModelPopOpen(false); setModelDraft(null)
  }, [tid])

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

  // 附件随发（2026-10-08）：粘贴/拖拽即上传为 artifact（服务端同 sha 去重），chip 三态。
  const uploadOne = (key: string, file: File) => {
    api.uploadAttachment(pid, file)
      .then((att) => setPendingFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "ready", att } : x)))
      .catch(() => setPendingFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "error" } : x)))
  }
  const pickFiles = (files: FileList | null) => {
    for (const f of Array.from(files ?? [])) {
      const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`
      setPendingFiles((fs) => [...fs, { key, status: "uploading", name: f.name, size: f.size, file: f }])
      uploadOne(key, f)
    }
  }
  const retryFile = (key: string) => {
    const f = pendingFiles.find((x) => x.key === key)
    if (!f?.file) return
    setPendingFiles((fs) => fs.map((x) => x.key === key ? { ...x, status: "uploading" } : x))
    uploadOne(key, f.file)
  }
  const removeFile = (key: string) => setPendingFiles((fs) => fs.filter((x) => x.key !== key))

  // 发送 = 乐观上屏（用户气泡 + 思考占位先行）；POST 为 202 异步起线程，
  // 上屏由事件/轮询对账驱动，不再阻塞等全量 GET（修「发送后卡一下」）
  const send = useCallback(async (text?: string) => {
    const body = (text ?? input).trim()
    const readyIds = pendingFiles.filter((f) => f.status === "ready" && f.att).map((f) => f.att!.id)
    if ((!body && !readyIds.length) || sending) return
    setSending(true)
    setInput(""); setErr(null); setPendingIn(body)
    draftsRef.current.set(tid ?? "__new", "")
    const refs = refsDraft.skills.length || refsDraft.mcps.length ? refsDraft : null
    try {
      let id = tid
      if (!id) {
        // 新建线程时带上新建态选的模型（v37；空=跟随全局默认）与联网开关
        // （v43；主控开 = spawn 子代理默认继承）
        const t = await api.chatThreadCreate(pid, agentId, undefined,
                                             modelDraft?.provider, modelDraft?.model,
                                             webSearchDraft)
        id = t.id; setTid(id)
      }
      await api.chatSend(id, body, refs, readyIds)
      setRefsDraft({ skills: [], mcps: [] })
      const sent = new Set(pendingFiles.filter((f) => f.status === "ready").map((f) => f.key))
      setPendingFiles((fs) => fs.filter((f) => !sent.has(f.key)))
    } catch (e) {
      setPendingIn(null)
      setErr(e instanceof Error ? e.message : String(e))
    }
    finally { setSending(false); inputRef.current?.focus() }
  }, [input, sending, tid, pid, agentId, refsDraft, pendingFiles, modelDraft])

  const removeThread = useCallback(async (id: string) => {
    try {
      await api.chatThreadDelete(id)
      draftsRef.current.delete(id)
      if (id === tid) selectThread(null)
      reloadThreads(agentId)
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
  }, [tid, agentId, reloadThreads, selectThread])

  const stop = useCallback(async () => {
    if (!tid || !running || stopping) return
    setStopping(true)                 // 立刻给反馈，别等后端退出
    try {
      await api.chatStop(tid)
      reloadRef.current?.()
    } catch (e) {
      setStopping(false)              // 请求本身失败 → 恢复可点
      if (e instanceof ApiError && e.status === 409) {
        reloadRef.current?.()
        return
      }
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [tid, running, stopping])

  // 轮次真的退出后复位（后端落「已停止」说明 → status 不再是 running）
  useEffect(() => { if (!running) setStopping(false) }, [running])

  // 自动滚底（含思考增量——此前漏 liveThinking，思考增长时不跟滚，观感「卡住」）
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages.length, liveDelta, liveThinking, running])

  // 输入框自适应高度（2026-10-14）：下限 rows=2、上限 180px（≈8 行），超出内部滚动。
  // 必须先置 height:auto 再读 scrollHeight——否则读到上一轮写死的固定高度，删字时无法回缩；
  // 置 auto 后按 rows=2 取固有高，故空内容自然停在 2 行。用 effect（非 onChange）是为了
  // 程序化清空（发送 / /context / 点引用 chip）时也能回缩。与 LiveRoom.autoResize 同款口径。
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = "auto"
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`
  }, [input])

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
  // 左栏内容（会话线程列表）：停靠时不再自定宽（由左导航列给宽），未停靠时维持原宽度与拖拽调宽。
  // **智能体切换入口已从本栏移除（2026-10-16，Nan 需求）**：此前栏顶有一张 .wb-agent-trigger
  // 卡片 + 下拉，与 composer 里的 agent 药丸重复；现在全应用只在 composer 处切换
  // （.wb-composer-agent 药丸 + composer-bar 的 Agents 面板两个入口）。
  const rail = (
    <aside className={cn("wb-aside", docked && "is-docked")}
           style={docked ? undefined : { width: asideWidth, flexBasis: asideWidth }}>
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
  )

  return (
    <div className="wb-shell">
      {/* 左栏：停靠进左导航列（portal）或留在原位 */}
      {docked ? createPortal(rail, railSlot) : rail}
      {/* 分割线只在未停靠时存在——停靠后本列宽度统一由左导航自己的分割线管 */}
      {!docked && (
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
      )}

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
                {thread?.challenge_asset_id && (
                  <span className="wb-badge" title="该线程绑定的 CTF 题目">
                    题目 · {assets.find((a) => a.id === thread.challenge_asset_id)?.value ?? "…"}
                  </span>
                )}
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
                    {it.rows.map((r) => {
                      if (r.kind === "thinking") return <ThinkingBlock key={r.id} content={r.content} />
                      // start 事件时刻：后端串行执行同批 tool_calls，只有真正轮到的那个才有
                      // start 事件（2026-10-14 修「同批第二个工具恒显静态运行中」）。拿不到
                      // start 而本轮仍在跑 = 排在队里 → 退到调用发起时刻计「排队中 Ns」；
                      // 本轮已停且无 start（孤儿行）维持原「运行中」，不编造时刻。
                      const started = liveTools.startAtById.get(r.id)
                        ?? liveTools.legacyStarts.find((p) => p.name === r.name
                          && p.args_head === JSON.stringify(r.args ?? {}).slice(0, 300))?.created_at
                      // 激进主控的先返先触发会先落一条「[后台继续]」占位结果，
                      // 但对应 call_expert 仍在后台执行。占位不能进入完成态，否则
                      // ToolBlock 会只显示静态「Ns」，而不是「已运行 Ns」计时。
                      const pendingBackground = (r.result ?? "").startsWith("[后台继续]")
                      const rowDone = !pendingBackground
                        && (r.done || liveTools.byId.get(r.id)?.phase === "done")
                      const queued = !started && !rowDone && running
                      return <ToolBlock key={r.id} name={r.name} args={r.args}
                        result={r.result}
                        startedAt={started ?? (queued ? r.at : undefined)}
                        queued={queued}
                        done={rowDone}
                        live={liveTools.byId.get(r.id)
                          ?? liveTools.legacy.find((p) => p.name === r.name
                            && p.args_head === JSON.stringify(r.args ?? {}).slice(0, 300))
                          ?? null} />
                    })}
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
                    argsHead={liveToolStart.args_head} done={false}
                    startedAt={liveToolStart.created_at} />
                </div>
              )}
              {(running || !!pendingIn) && !liveDelta && !liveThinking && !liveToolStart && !hasPendingCall && (
                <div className="wb-anim py-0.5 text-[12px] text-muted-foreground">
                  {stopping
                    ? "正在停止…"
                    : liveRetry
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
                    {/* 缓存命中（2026-10-13）：第二根分段条——口径为**累计**（跨步自加）
                        的缓存读/写/未缓存；大号命中率 = cache_read_total / input_total，
                        右附累计步数。首步冷缓存显「首步」。 */}
                    {cache && (
                      <div className="mt-2 border-t border-border/50 pt-2">
                        <div className="wb-usage-head-row">
                          <b className="wb-usage-total">总缓存命中
                            <span className={cn("wb-usage-pct-big ml-1", cache.level)}>{cache.hitPct}%</span>
                          </b>
                          <span className="wb-thread-meta">
                            {cache.cold ? "首步（冷缓存）" : `累计 ${usage.steps ?? 0} 步`}
                          </span>
                        </div>
                        <div className="wb-usage-segs" role="img" aria-label="缓存构成分段条">
                          {cache.segs.map((s) => (
                            <i key={s.key} className={s.cls}
                              style={{ width: `${s.pct}%` }}
                              title={`${s.label} ${fmtTokens(s.tokens)}（${s.pct}%）`} />
                          ))}
                        </div>
                        <div className="wb-usage-legend">
                          {cache.segs.map((s) => (
                            <div className="wb-usage-li" key={s.key}>
                              <i className={cn("wb-usage-dot", s.cls)} />
                              <b className="shrink-0">{s.label}</b>
                              <span className="wb-usage-num">{fmtTokens(s.tokens)} · {s.pct}%</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    <div className="wb-kv"><b className="shrink-0">输出（累计）</b><span className="wb-usage-num">{fmtTokens(usage.output ?? 0)}</span></div>
                    <div className="wb-kv"><b className="shrink-0">模型步数</b><span className="wb-usage-num">{usage.steps ?? 0}</span></div>
                    {!bk && <div className="wb-kv is-hint">发送一条消息后生成分类构成（系统提示/工具/消息占比）</div>}
                  </>
                ) : (
                  <div className="wb-kv">本轮会话还没有用量数据，发送一条消息后生成</div>
                )}
              </div>
            </div>
          )}

          {/* 候选 flag 确认卡（ctf-flag-wake）：子代理提交候选后主控会先解释一段，
              这里给人类复制/确认的入口。与审批卡同一区块，但语义独立——
              审批卡是「要不要执行」，本卡是「这串是不是答案」。 */}
          <CandidateFlagCard flags={candidateFlags} onDecide={decideCandidateFlag}
            busy={decideFlagBusy} />

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

          <div className="wb-composer-wrap">
          <div className="wb-composer"
               onDragOver={(e) => { e.preventDefault() }}
               onDrop={(e) => { e.preventDefault(); pickFiles(e.dataTransfer?.files ?? null) }}>
            <div className="wb-composer-head">
              <button className="wb-composer-agent" onClick={() => setAgentMenuOpen(true)}>
                <span className={cn("wb-avatar is-sm", agentId === ORCHESTRATOR && "is-orch")}>
                  {agentId === ORCHESTRATOR ? <Bot size={11} /> : agentInitial(currentAgent)}
                </span>
                {currentAgent?.name ?? agentId}
                <ChevronDown size={11} className="opacity-70" />
              </button>
              <span className="wb-composer-status">
                {(running || !!pendingIn || sending)
                  ? <><span className="wb-dot is-running" />正在执行 · 可停止</>
                  : <><span className="wb-dot" />待命</>}
              </span>
            </div>
            {pendingFiles.length > 0 && (
              <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                {pendingFiles.map((f) => (
                  <span key={f.key}
                    className={cn("inline-flex max-w-64 items-center gap-1 rounded-md border px-2 py-1 text-xs",
                      f.status === "error" && "border-destructive/50 text-destructive",
                      f.status === "uploading" && "opacity-60")}
                    title={f.status === "error" ? "上传失败，点「重试」再传" : `${f.name}（${fmtBytes(f.size)}）`}>
                    <Paperclip className="size-3 shrink-0" />
                    <span className="min-w-0 truncate">{f.name}</span>
                    <span className="shrink-0 text-[10px] text-muted-foreground">{fmtBytes(f.size)}</span>
                    {f.status === "uploading" && <span className="shrink-0 text-[10px]">上传中…</span>}
                    {f.status === "error" && (
                      <button type="button" className="shrink-0 underline" onClick={() => retryFile(f.key)}>重试</button>
                    )}
                    <button type="button" className="shrink-0 text-muted-foreground hover:text-foreground"
                            onClick={() => removeFile(f.key)} title="移除附件">
                      <X className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <textarea
              ref={inputRef}
              rows={2}
              value={input}
              placeholder={running ? "当前线程执行中，可点「停止」中止本轮…" : "描述测试意图，回车发送（Shift+回车 换行，/ 唤出命令，可粘贴或拖入附件）"}
              onChange={(e) => onInputChange(e.target.value)}
              onPaste={(e) => {
                const files = e.clipboardData?.files
                if (files && files.length) { e.preventDefault(); pickFiles(files) }
              }}
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
              <button className={cn("wb-usage", usageLevel)}
                title="上下文用量（点击看明细，或输入 /context）" onClick={() => setUsageOpen((v) => !v)}>
                <UsageRing pct={usagePct} />
                <span className="wb-usage-pct">{usagePct}%</span>
              </button>
              {/* 联网搜索开关（v43，web-search-toggle，2026-10-19）：MCP/上下文
                  右侧。线程级——开 = 本线程主控与 spawn 的子代理默认可用
                  web_search（SearXNG/ddgs 双后端查公开信息）；独立于专家工具
                  勾选（不进 Agents/Skills 面板）。下一轮生效。 */}
              <button type="button" className={cn("wb-cap", webSearchOn && "is-active")}
                aria-pressed={webSearchOn}
                title={webSearchOn
                  ? "联网搜索已开启：主控与子代理可用 web_search 查公开信息（CVE/EXP/情报；SearXNG 优先、ddgs 备用，免 Key）"
                  : "联网搜索已关闭：点击开启后，本线程主控与其派出的子代理默认可用 web_search 查公开信息"}
                onClick={() => void toggleWebSearch()}>
                <Globe size={12} />
                联网
              </button>
              <span className="wb-spacer" />
              {/* 线程级模型按钮（v37，2026-10-13）：发送键左侧，点开供应商→模型浮层 */}
              <button type="button" className={cn("wb-model", modelPopOpen && "is-active")}
                title={activeModel.provider
                  ? `当前线程模型：${activeModel.provider}/${activeModel.model || "（供应商默认）"}（点击更换）`
                  : "当前线程跟随全局默认模型（点击指定）"}
                onClick={openModelPop}>
                <Cpu size={11} />
                <span className="wb-model-name">{modelLabel}</span>
                <ChevronDown size={10} className="opacity-70" />
              </button>
              {running ? (
                <button className="wb-send is-stop" disabled={stopping}
                  onClick={() => void stop()}>
                  <Square size={11} /> {stopping ? "停止中…" : "停止"}
                </button>
              ) : (
                <button className="wb-send" disabled={!input.trim() || sending} onClick={() => void send()}>
                  {sending ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />} 发送
                </button>
              )}
            </div>
          </div>

          {/* 模型选择浮层（v37）：供应商 → 展开该供应商模型；顶部「默认模型」= 清覆写。
              浮层挂在 composer 外层 wrap（.wb-composer 有 overflow:hidden，不能内嵌）。 */}
          {modelPopOpen && (
            <>
              <div className="wb-model-backdrop" onClick={() => setModelPopOpen(false)} />
              <div className="wb-model-pop">
                <button type="button"
                  className={cn("wb-model-opt", !activeModel.provider && "is-current")}
                  onClick={() => void chooseModel("", "")}>
                  <span>默认模型（跟随全局）</span>
                  {!activeModel.provider && <Check size={12} />}
                </button>
                <div className="wb-model-label">供应商 / 模型</div>
                {!enabledProviders.length && (
                  <div className="wb-model-hint">无启用的供应商（请在设置页配置）</div>
                )}
                {enabledProviders.map((p) => (
                  <div key={p.name}>
                    <button type="button"
                      className={cn("wb-model-opt", popProvider === p.name && "is-open")}
                      onClick={() => setPopProvider(popProvider === p.name ? "" : p.name)}>
                      <span className="min-w-0 truncate">{p.name}</span>
                      {activeModel.provider === p.name
                        ? <span className="wb-model-tag">当前</span>
                        : <ChevronDown size={11} className={cn("shrink-0 opacity-60",
                            popProvider === p.name && "rotate-180")} />}
                    </button>
                    {popProvider === p.name && (
                      <div className="wb-model-sub">
                        {(p.models ?? []).map((m) => (
                          <button key={m} type="button"
                            className={cn("wb-model-opt",
                              activeModel.provider === p.name && activeModel.model === m && "is-current")}
                            onClick={() => void chooseModel(p.name, m)}>
                            <span className="wb-model-opt-m min-w-0 truncate">{m}</span>
                            {activeModel.provider === p.name && activeModel.model === m
                              && <Check size={12} className="shrink-0" />}
                          </button>
                        ))}
                        <button type="button"
                          className={cn("wb-model-opt",
                            activeModel.provider === p.name && !activeModel.model && "is-current")}
                          onClick={() => void chooseModel(p.name, "")}>
                          <span className="wb-model-opt-m">供应商默认模型</span>
                          {activeModel.provider === p.name && !activeModel.model
                            && <Check size={12} className="shrink-0" />}
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}

          {/* 智能体选择浮层（2026-10-16 从左栏迁来）：由 composer 的 .wb-composer-agent 药丸
              触发，向上弹出。必须挂在 composer 外层 wrap —— .wb-composer 自身 overflow:hidden，
              内嵌会被裁掉（与上面的模型浮层同理）。backdrop 负责点外部关闭。 */}
          {agentMenuOpen && (
            <>
              <div className="wb-model-backdrop" onClick={() => setAgentMenuOpen(false)} />
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
            </>
          )}
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
                    <div className="flex items-center justify-between">
                      <span className="wb-thread-meta">MCP 服务器（点选加入本轮）</span>
                      <button className="wb-cap" title="重新探测 MCP 工具"
                        onClick={() => loadMcp(true)}>
                        <RefreshCw size={11} /> 刷新
                      </button>
                    </div>
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

/** 字节 → 人类可读（附件 chip 用；与 LiveRoom 同款）。 */
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
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
// `at` = 发起该调用的 assistant 消息时刻（2026-10-14）：后端**串行**执行同一批 tool_calls，
// `chat.tool` 的 start 事件只在真正轮到该工具时才发 → 排队中的工具没有 start 时刻，前端只能
// 退到「调用发起时刻」计时（显示「排队中 Ns」），否则只能挂一个静态「运行中」。
type GroupRow =
  | { kind: "tool"; id: string; name: string; args?: Record<string, unknown>
      result?: string; done: boolean; at?: string }
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
      // 同 tool_use_id 可能有两行 tool 结果（激进主控先返先触发：主线程先落
      // 「[后台继续]」占位，worker 完成后以同 id 续写真实结果）——取最后一
      // 行，探针卡从「后台继续」平滑更新为真实结果。单结果行历史等价。
      const trs = messages.filter((x) => x.role === "tool" && x.tool_use_id === tc.id)
      const tr = trs.length ? trs[trs.length - 1] : undefined
      pending.rows.push({ kind: "tool", id: tc.id, name: tc.name, args: tc.args,
                          result: tr?.content, done: !!tr, at: m.created_at })
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
function ToolBlock({ name, args, argsHead, result, done, live, startedAt, queued }: {
  name: string; args?: Record<string, unknown>; argsHead?: string
  result?: string; done: boolean
  live?: { ok?: boolean; duration_s?: number; result_head?: string } | null
  startedAt?: string
  /** 已发起但尚未轮到执行（同批串行排队）——计时改口径为「排队中」，见 GroupRow.at 注释 */
  queued?: boolean
}) {
  const [open, setOpen] = useState(false)
  // 运行中逐秒刷新「已运行 Ns」（2026-10-13）：替代静态「运行中」，长工具（MCP 全量
  // 分诊可达数十秒）能看出在跑而非卡死。结束/无起始时刻即停表。起始时刻必须走
  // parseTs（后端 naive ISO 按 UTC 解析），裸 new Date 会按本地时区算出负差 → 恒 0s。
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (done || !startedAt) return
    const t0 = parseTs(startedAt).getTime()
    const tick = () => setElapsed(
      Number.isNaN(t0) ? 0 : Math.max(0, Math.floor((Date.now() - t0) / 1000)))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [done, startedAt])
  // ok 三态来源：持久化结果行 → 「[错误]」前缀约定；事件实时态 → 后端结构化 ok
  const ok = done
    ? live ? live.ok !== false : !(result ?? "").startsWith("[错误")
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
        {!done && <span className={cn("shrink-0 font-mono text-[9px]",
          queued ? "text-muted-foreground" : "text-primary")}>
          {startedAt ? `${queued ? "排队中" : "已运行"} ${elapsed}s` : "运行中"}
        </span>}
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
            {done ? (result ?? live?.result_head ?? "").slice(0, 6000)
              : live?.result_head ? live.result_head.slice(0, 600) : "执行中…"}
          </pre>
        </div>
      )}
    </div>
  )
}
