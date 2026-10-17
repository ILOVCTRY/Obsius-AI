import { Component, useCallback, useEffect, useState, type ErrorInfo, type ReactNode } from "react"
import { Group, Panel, Separator, useDefaultLayout } from "react-resizable-panels"
import type { LucideIcon } from "lucide-react"
import {
  Activity,
  ArrowLeft,
  Blocks,
  BookOpen,
  Bot,
  BrainCircuit,
  ChevronDown,
  ChevronLeft,
  FileDiff,
  Globe2,
  Inbox,
  FolderKanban,
  Trash2,
  FolderTree,
  LayoutDashboard,
  Maximize2,
  Minus,
  Network,
  PanelLeftOpen,
  Radio,
  Settings2,
  Sparkles,
  Square,
  Stethoscope,
  X,
  Zap,
} from "lucide-react"
import { api, ApiError } from "@/lib/api"
import type { ProjectDetail } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { LiveRoom } from "@/views/LiveRoom"
import { AgentWorkbenchView } from "@/views/AgentWorkbenchView"
import { Blackboard } from "@/views/Blackboard"
import { ReverseWorkbench } from "@/views/reverse/ReverseWorkbench"
import { RevCompact } from "@/views/reverse/RevCompact"
import { deriveWorkbenchProfile } from "@/lib/workbench"
import { NavRailSlotContext } from "@/lib/navRail"
import { useEvents } from "@/lib/useEvents"
import { ProjectsView } from "@/views/ProjectsView"
import { TrashView } from "@/views/TrashView"
import { IntelView } from "@/views/IntelView"
import { BrowserView } from "@/views/browser/BrowserView"
import { FuzzerView } from "@/views/fuzzer/FuzzerView"
import { ProxyPoolView } from "@/views/proxy/ProxyPoolView"
import { SettingsView } from "@/views/SettingsView"
import { KnowledgeView } from "@/views/KnowledgeView"
import { SkillsView } from "@/views/SkillsView"
import { SampleAnalysisView } from "@/views/SampleAnalysisView"
import { DiagnosticsView } from "@/views/DiagnosticsView"
import { ProposalsView } from "@/views/ProposalsView"
import { cn } from "@/lib/utils"
import { bindingBadge } from "@/lib/taxonomy"

// 三栏指挥台（DESIGN.md §12 定稿）：左窄导航 / 中主区 / 直播间页右侧黑板常驻侧栏
// F1（2026-09-17）：左导航与右侧黑板侧栏 react-resizable-panels v4 可拖拽调宽
// （左 48–220px、右 288–640px），宽度 localStorage 持久化（ui.nav / ui.live-board）。
// TRAE 化新壳（shell2）2026-09-26 用户定稿移除：不再维护，代码已删（git 历史可考）；
// 旧壳为唯一壳。
// 侧栏收起（2026-09-29）：展开态 hover 分割线浮现「‹」收起；收起后完全隐藏，
// 左缘悬浮把手唤出；状态 localStorage（ui.nav-collapsed）。收起=条件渲染卸载
// nav Panel+Separator（同 boardOpen 先例），重挂由 useDefaultLayout 恢复宽度。

type View = "projects" | "intel" | "trash" | "live" | "board" | "agents" | "proposals" | "browser" | "fuzzer" | "proxy" | "sample-analysis" | "knowledge" | "skills" | "settings" | "diagnostics"

/** M4c 场景档 board_view 默认视图（config.board_view.default；黑板上自行校验可用 tab 回退） */
const boardViewOf = (m: ProjectDetail | null): string | undefined => {
  const v = (m?.config as { board_view?: { default?: unknown } } | undefined)?.board_view?.default
  return typeof v === "string" && v ? v : undefined
}

type NavItem = { key: View; label: string; icon: LucideIcon; needsProject: boolean; homeOnly?: boolean; hideTracks?: string[] }

/** 分组定义。`collapsible` = 标题行可点折叠；`defaultOpen` = 首次进入（无持久化状态）时是否展开。 */
type NavGroup = { label: string; items: NavItem[]; collapsible?: boolean; defaultOpen?: boolean }

const NAV_GROUPS: NavGroup[] = [
  {
    label: "工作区",
    items: [
      { key: "projects", label: "项目", icon: FolderKanban, needsProject: false },
      { key: "intel", label: "情报", icon: Inbox, needsProject: false, homeOnly: true },
      { key: "trash", label: "回收站", icon: Trash2, needsProject: false, homeOnly: true },
      { key: "agents", label: "智能体", icon: Bot, needsProject: true },
      { key: "board", label: "黑板", icon: LayoutDashboard, needsProject: true },
      { key: "proposals", label: "变更提案", icon: FileDiff, needsProject: false },
      { key: "diagnostics", label: "诊断", icon: Stethoscope, needsProject: true },
    ],
  },
  // 工具（2026-10-15）：条目最多的一组（7 项），标题行可折叠收纳；默认展开。
  {
    label: "工具",
    collapsible: true,
    defaultOpen: true,
    items: [
      { key: "browser", label: "浏览器", icon: Globe2, needsProject: true },
      { key: "fuzzer", label: "重放", icon: Zap, needsProject: true },
      // 样本分析（2026-10-17）：CTF 轨隐藏——题面文件走对话附件 + bb_add_asset
      // （challenge-asset 流），没有分析包上传工作流（后端 bb_query 的 package
      // 面同轨裁剪，见 tool_registry track-query-split）。
      { key: "sample-analysis", label: "样本分析", icon: FolderTree, needsProject: true, hideTracks: ["ctf"] },
      { key: "proxy", label: "代理池", icon: Network, needsProject: false },
      { key: "knowledge", label: "知识库", icon: BookOpen, needsProject: false },
      { key: "skills", label: "技能库", icon: BrainCircuit, needsProject: false },
      { key: "settings", label: "设置", icon: Settings2, needsProject: false },
    ],
  },
  // 更多（2026-09-30）：会话降级为二级入口，默认折叠收纳（见 NavRail 折叠逻辑）；
  // 任务机制退役（2026-10-06）后该组仅剩「会话」。
  {
    label: "更多",
    collapsible: true,
    items: [
      { key: "live", label: "会话", icon: Radio, needsProject: true },
    ],
  },
]

// 分组折叠状态：按组名存 localStorage（JSON 对象）。旧版只有「更多」一组、键为
// ui.nav-more-open（"1"/"0"），首次读不到新键时迁移一次；之后旧键不再参与。
const GROUPS_OPEN_KEY = "ui.nav-groups-open"
const LEGACY_MORE_OPEN_KEY = "ui.nav-more-open"

function readGroupOpen(): Record<string, boolean> {
  // 先铺各组默认值（未声明 defaultOpen 的可折叠组默认折叠）
  const out: Record<string, boolean> = {}
  for (const g of NAV_GROUPS) if (g.collapsible && g.defaultOpen) out[g.label] = true
  try {
    const raw = window.localStorage.getItem(GROUPS_OPEN_KEY)
    if (raw) {
      const parsed: unknown = JSON.parse(raw)
      if (parsed && typeof parsed === "object") {
        for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
          if (typeof v === "boolean") out[k] = v
        }
      }
    } else if (window.localStorage.getItem(LEGACY_MORE_OPEN_KEY) === "1") {
      out["更多"] = true
    }
  } catch { /* 坏值/隐私模式：退回默认 */ }
  return out
}

/** 当前视图是否落在该可折叠组内（用户未做过显式选择时用来兜底展开、保高亮可见） */
function groupHasActive(items: NavItem[], active: View): boolean {
  return items.some((n) => n.key === active)
}

function NavRail({ active, onSelect, locked, className, expanded = false, proposalsN = 0,
                   railActive = false, railSlotRef, track }: {
  active: View
  onSelect: (key: View) => void
  locked: boolean
  className?: string
  expanded?: boolean
  /** 待审提案数（「变更提案」项角标；0 不显示） */
  proposalsN?: number
  /** 工作台侧栏是否停靠到本列下半（agents 视图）：槽位 DOM 由本组件渲染（见 lib/navRail.ts） */
  railActive?: boolean
  /** 槽位节点回调：App 持有该节点并经 context 下发给工作台做 portal 目标 */
  railSlotRef?: (el: HTMLDivElement | null) => void
  /** 当前项目轨（hideTracks 轨级隐藏用；无项目上下文=undefined 不隐藏） */
  track?: string
}) {
  // 分组折叠（2026-09-30 起只有「更多」；2026-10-15 泛化为任意 collapsible 组）。
  // 状态按组名持久化在 ui.nav-groups-open（见 readGroupOpen：含 defaultOpen 与旧键迁移）。
  // 展开判定在渲染期派生：`groupOpen[label] ?? groupHasActive` —— 用户没做过显式选择时，
  // 「当前视图在组内」自动展开保高亮；手动折叠/展开过一次后就完全听手动状态（标准手风琴）。
  // 旧版写成渲染期恒 `|| moreActive` 强制展开，导致该组的折叠按钮点不动（老毛病）。
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>(readGroupOpen)
  useEffect(() => {
    try { window.localStorage.setItem(GROUPS_OPEN_KEY, JSON.stringify(groupOpen)) }
    catch { /* 隐私模式：仅本次会话有效 */ }
  }, [groupOpen])
  const toggleGroup = (label: string, open: boolean) =>
    setGroupOpen((m) => ({ ...m, [label]: !open }))

  // 无项目上下文时只展示全局项（needsProject=false：项目/情报/设置）；
  // 进入项目后移除项目入口和 homeOnly 项（情报）——项目工作区内不再显示
  // 「项目」，返回首页清掉 pid 后入口自然恢复。
  // 其余入口仍从 NAV_GROUPS 动态生成；仅对「项目」做显式生命周期过滤。
  // hideTracks：轨级整项隐藏（当前轨命中即从导航消失，非灰显）——样本分析对
  // CTF 不可用（题面走对话附件流，无分析包工作流）。
  const hidden = (item: NavItem) =>
    (!locked && item.key === "projects") || !!track && !!item.hideTracks?.includes(track)
  const groups = locked
    ? [{ label: "", collapsible: false, items: NAV_GROUPS.flatMap((group) => group.items).filter((item) => !item.needsProject && !hidden(item)) }]
    : NAV_GROUPS.map((group) => ({
        label: group.label,
        collapsible: group.collapsible,
        items: group.items.filter((item) => !item.homeOnly && !hidden(item)),
      })).filter((group) => group.items.length > 0)

  return (
    <nav className={cn("app-nav flex shrink-0 flex-col border-r", expanded ? "items-stretch" : "items-center",
                       railActive && expanded && "has-rail", className)}>
      <NavBrand expanded={expanded} />
      <div className="nav-scroll">
        {groups.map((group) => {
          const collapsible = !!group.collapsible
          // 窄栏态无标题可点 → 直接展开；展开态 = 手动状态优先，未选择过则看当前视图是否在组内。
          const open = !collapsible || !expanded
            || (groupOpen[group.label] ?? groupHasActive(group.items, active))
          return (
            <div className="nav-group" key={group.label || "home"}>
              {expanded && group.label && (collapsible ? (
                <button
                  type="button"
                  className="nav-group-toggle"
                  aria-expanded={open}
                  title={open ? `收起「${group.label}」` : `展开「${group.label}」`}
                  onClick={() => toggleGroup(group.label, open)}
                >
                  <span className="nav-group-label">{group.label}</span>
                  <ChevronDown size={12} className={cn("nav-group-chevron", open && "is-open")} />
                </button>
              ) : (
                <span className="nav-group-label">{group.label}</span>
              ))}
              {open && group.items.map((n) => {
                const Icon = n.icon
                const disabled = n.needsProject && locked
                return (
                  <button
                    key={n.key}
                    disabled={disabled}
                    onClick={() => onSelect(n.key)}
                    title={expanded ? undefined : n.label}
                    className={cn("nav-item", expanded ? "justify-start px-3" : "justify-center", active === n.key && "is-active", disabled && "is-locked")}
                  >
                    <Icon size={17} strokeWidth={1.8} />
                    {expanded && <span>{n.key === "settings" && locked ? "设置" : n.label}</span>}
                    {n.key === "proposals" && proposalsN > 0 && (
                      <b className="nav-count">{proposalsN}</b>
                    )}
                  </button>
                )
              })}
            </div>
          )
        })}
      </div>
      {/* 工作台侧栏槽位（2026-10-16）：导航分组之下、页脚之上，由工作台 portal 投递内容。
          只在本列展开且 agents 视图时存在；窄栏/收起/其它视图时不存在 → 工作台走原两列布局。 */}
      {expanded && railActive && <div className="nav-rail-slot" ref={railSlotRef} />}
      {expanded && <div className="nav-footer"><Activity size={14} /><span>系统在线</span><span className="status-dot" /></div>}
    </nav>
  )
}

/** 收起态的左缘唤出把手：常驻窄命中区，悬停浮现青条与展开图标 */
function NavReveal({ onExpand }: { onExpand: () => void }) {
  return (
    <button className="nav-reveal" title="展开侧栏" aria-label="展开侧栏" onClick={onExpand}>
      <PanelLeftOpen size={14} />
    </button>
  )
}

/** 桌面窗口状态（最大化标志 + 控制 API）；浏览器环境返回空。
 *  标题栏已拆件：品牌进左导航顶部（NavBrand），窗口按钮悬浮在内容区右上角（WindowControls）。 */
function useDesktopWindow() {
  const desktopWindow = typeof window !== "undefined" ? window.desktopWindow : undefined
  const [maximized, setMaximized] = useState(false)
  useEffect(() => {
    if (!desktopWindow) return
    void desktopWindow.isMaximized().then(setMaximized).catch(() => {})
    return desktopWindow.onMaximizedChanged(setMaximized)
  }, [desktopWindow])
  return { desktopWindow, maximized }
}

/** 左导航顶部的品牌行：兼作窗口拖动区（双击最大化）。 */
function NavBrand({ expanded }: { expanded: boolean }) {
  const { desktopWindow } = useDesktopWindow()
  return (
    <div className="nav-brand" onDoubleClick={() => void desktopWindow?.toggleMaximize()}>
      <span className="brand-mark"><Sparkles size={13} /></span>
      {expanded && <span>Obsius</span>}
    </div>
  )
}

/** 窗口控制按钮（最小化/最大化/关闭）；项目页并入顶栏、其余页面悬浮在内容右上角。 */
function WindowControls({ className }: { className?: string }) {
  const { desktopWindow, maximized } = useDesktopWindow()
  if (!desktopWindow) return null
  return (
    <div className={cn("window-controls", className)}>
      <button type="button" aria-label="最小化" title="最小化" onClick={() => void desktopWindow.minimize()}><Minus size={15} /></button>
      <button type="button" aria-label={maximized ? "还原" : "最大化"} title={maximized ? "还原" : "最大化"} onClick={() => void desktopWindow.toggleMaximize()}>{maximized ? <Square size={12} /> : <Maximize2 size={13} />}</button>
      <button type="button" className="window-close" aria-label="关闭" title="关闭" onClick={() => void desktopWindow.close()}><X size={15} /></button>
    </div>
  )
}

/** 全局渲染兜底：任一视图渲染异常不再整个页面白屏只剩底色（此前无 ErrorBoundary），
 * 显示错误信息 + 重试按钮，错误范围限制在主区，导航/顶栏不受影响 */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }
  static getDerivedStateFromError(error: Error) { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error("ErrorBoundary:", error, info) }
  render() {
    if (this.state.error) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-3 p-6">
          <p className="text-sm font-medium text-(--status-error)">界面渲染出错</p>
          <pre className="max-w-xl whitespace-pre-wrap break-words rounded bg-muted/40 p-3 font-mono text-[10px] text-muted-foreground">
            {String(this.state.error)}
          </pre>
          <Button size="sm" variant="outline" onClick={() => this.setState({ error: null })}>重试</Button>
        </div>
      )
    }
    return this.props.children
  }
}

// 事件订阅隔离壳（2026-10-04）：只订阅、去抖、回调，**渲染 null**——把「事件驱动的
// 状态更新」关在一个小组件里，父组件（App 根）不再随事件重渲。工作台/直播间同理
// 可按需复用（它们自身需要事件驱动渲染，故不适用）。
function EventsDebouncedRefresh({ pid, onBump }: {
  pid: string | null; onBump: () => void
}) {
  const { events } = useEvents(pid)
  const n = events.length
  useEffect(() => {
    if (!pid || n === 0) return
    const t = setTimeout(onBump, 400)
    return () => clearTimeout(t)
  }, [n, pid, onBump])
  return null
}

export default function App() {
  const [pid, setPid] = useState<string | null>(null)
  const [meta, setMeta] = useState<ProjectDetail | null>(null)
  const [view, setView] = useState<View>("projects")
  // 变更提案待审数（工作区「变更提案」角标；提案全局共享，无项目也拉）
  const [proposalsN, setProposalsN] = useState(0)
  const [boardOpen, setBoardOpen] = useState(true)
  // 蓝图「生成/深析」派发后跳智能体工作台并选中新线程（goto-agents，M2 2026-10-13）：
  // 存线程 id 作 prop 下传，由 AgentWorkbenchView 消费后回调清空。
  const [focusChatTid, setFocusChatTid] = useState<string | null>(null)
  // 工作台侧栏停靠槽位（2026-10-16）：agents 视图下把工作台的左栏（agent 切换器 +
  // 会话线程）并入左导航列下半，主区因此多出整列宽度。节点由 NavRail 渲染并回传，
  // 经 NavRailSlotContext 下发；工作台用 createPortal 投递（见 lib/navRail.ts）。
  const [railSlot, setRailSlot] = useState<HTMLDivElement | null>(null)
  // 黑板 Keep-alive（2026-09-30）：首次点开「黑板」的 pid 才挂载（没点过的项目不
  // 预加载）；同项目切视图不卸载只隐藏，换项目（pid 变）时因 key={pid} 全新挂载重载
  const [boardSeenPid, setBoardSeenPid] = useState<string | null>(null)
  useEffect(() => {
    if (view === "board" && pid) setBoardSeenPid((prev) => (prev === pid ? prev : pid))
  }, [view, pid])
  // 全局侧栏收起（2026-09-29）：完全隐藏，左缘把手唤出；记忆上次形态
  const [navCollapsed, setNavCollapsed] = useState(
    () => window.localStorage.getItem("ui.nav-collapsed") === "1")
  const toggleNavCollapsed = useCallback(() => {
    const next = !navCollapsed
    setNavCollapsed(next)
    window.localStorage.setItem("ui.nav-collapsed", next ? "1" : "0")
  }, [navCollapsed])
  // 直播间「复盘沉淀」完成 → 跨视图跳到设置指定 tab；skill.routed 双击 → 带 skill 深链选中
  const [settingsNav, setSettingsNav] = useState<{
    tab: string; n: number; skill?: { source: "cap" | "track"; pack: string; name: string }
  } | null>(null)
  // v0.71 任务即窗口：任务卡/任务流双击 → 跳会话页并直开专属执行窗页签
  const [sessionNav, setSessionNav] = useState<{ sid: string; n: number } | null>(null)
  // 重放工作台深链：抓包行「重发」/ 直播间入口 → 跳重放视图并预填报文（2026-10-07）
  const [fuzzerNav, setFuzzerNav] = useState<{ raw?: string; n: number } | null>(null)
  useEffect(() => {
    const h = (e: Event) => {
      const d = (e as CustomEvent<{
        tab?: string; skill?: { source: "cap" | "track"; pack: string; name: string }
      }>).detail
      if (d?.tab === "skills") {
        setView("skills")
      } else if (d?.tab === "kb") {
        setView("knowledge")
      } else {
        setView("settings")
      }
      if (d?.tab || d?.skill) setSettingsNav({ tab: d.tab ?? "skills", n: Date.now(), skill: d.skill })
    }
    window.addEventListener("goto-settings", h)
    return () => window.removeEventListener("goto-settings", h)
  }, [])

  // v0.71 任务即窗口：双击任务卡/任务流节点直开会话页签（detail.sessionId 必带）
  useEffect(() => {
    const h = (e: Event) => {
      const sid = (e as CustomEvent<{ sessionId?: string }>).detail?.sessionId
      if (!sid) return
      setView("live")
      setSessionNav({ sid, n: Date.now() })
    }
    window.addEventListener("goto-session", h)
    return () => window.removeEventListener("goto-session", h)
  }, [])

  // 重放工作台入口（goto-fuzzer 自定义事件模式；detail.raw 预填报文）
  useEffect(() => {
    const h = (e: Event) => {
      const d = (e as CustomEvent<{ raw?: string }>).detail
      setView("fuzzer")
      setFuzzerNav({ raw: d?.raw, n: Date.now() })
    }
    window.addEventListener("goto-fuzzer", h)
    return () => window.removeEventListener("goto-fuzzer", h)
  }, [])

  // 情报页「查看全部」等跨视图跳转（goto-* 自定义事件模式）
  useEffect(() => {
    const h = () => setView("intel")
    window.addEventListener("goto-intel", h)
    return () => window.removeEventListener("goto-intel", h)
  }, [])

  // 蓝图产出入口（M2）派发后跳「智能体工作台」并选中新线程：detail.threadId 经
  // focusChatTid 作 prop 下传（组件挂载时即有，避开「事件早于挂载」竞态）。
  useEffect(() => {
    const h = (e: Event) => {
      const t = (e as CustomEvent<{ threadId?: string }>).detail?.threadId
      setView("agents")
      setFocusChatTid(t ?? null)
    }
    window.addEventListener("goto-agents", h)
    return () => window.removeEventListener("goto-agents", h)
  }, [])

  const openProject = useCallback((id: string) => {
    setPid(id)
    // 进入项目即定位到「智能体」会话窗口（2026-10-11）：此前落 live（会话直播间
    // 的指挥/空态落地页），改为直接打开左导航「智能体」= AgentWorkbenchView
    // （agent 触发卡 + 线程列表 + 对话挖洞），少一次手动切换。
    setView("agents")
  }, [])

  // 项目页返回入口由悬浮控件承载；保留回调供跨视图调用。
  const goHome = useCallback(() => {
    // 首页是无项目上下文的入口；清掉 pid 让项目入口和全局导航恢复。
    setPid(null)
    setMeta(null)
    setView("projects")
  }, [])

  useEffect(() => {
    if (!pid) {
      setMeta(null)
      return
    }
    let alive = true
    const load = () =>
      api.getProject(pid).then((m) => {
        if (alive) setMeta(m)
      }).catch((e: unknown) => {
        // 项目已被删除（常见：另一个标签里删的）：退回列表，子视图轮询随卸载全停。
        // 只认 404；409（删除中）等下一轮变 404 再退，网络错误不退防误伤。
        if (alive && e instanceof ApiError && e.status === 404) setPid(null)
      })
    load()
    const t = setInterval(load, 5000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [pid])

  // 事件到达即去抖重拉项目元数据（顶栏任务/发现/资产统计；5s 轮询兜底）。
  // **订阅隔离**（2026-10-04）：事件订阅移到返回 null 的 EventsDebouncedRefresh
  // 子组件——此前 useEvents 直接挂在 App 根上，流式期每条事件（~6-8Hz）都重渲
  // 整棵应用树（工作台/发现栏/直播间全跟着重渲），是「输出过程卡卡的」主因之一。
  const refreshProposalsN = useCallback(() => {
    api.proposals("pending").then((ps) => setProposalsN(ps.length)).catch(() => {})
  }, [])
  // 提案变化（人工审批 / 复盘 Job / Agent 自动提案）→ 刷新角标
  useEffect(() => {
    refreshProposalsN()
    window.addEventListener("proposals-changed", refreshProposalsN)
    return () => window.removeEventListener("proposals-changed", refreshProposalsN)
  }, [refreshProposalsN])
  const bumpMeta = useCallback(() => {
    if (pid) api.getProject(pid).then(setMeta).catch(() => {})
    refreshProposalsN()  // 新事件里可能含 proposal.created（Agent 自动提案）
  }, [pid, refreshProposalsN])
  const eventsWatcher = <EventsDebouncedRefresh pid={pid} onBump={bumpMeta} />

  // 工作台 profile：research+binary ⇒ rev-generic 逆向工作台（其他轨保持渗透模板）
  const profile = deriveWorkbenchProfile(meta)

  // F1：三栏宽度持久化（localStorage；仅用户拖拽后的布局保存）
  const navLayout = useDefaultLayout({
    id: "app-nav-v3", panelIds: ["nav", "main"], storage: window.localStorage,
    onlySaveAfterUserInteractions: true,
  })
  const liveBoardLayout = useDefaultLayout({
    id: "live-board-v3", panelIds: ["live-main", "live-aside"], storage: window.localStorage,
    onlySaveAfterUserInteractions: true,
  })

  // 无项目上下文的视图（项目列表 / 全局情报页 / 知识库 / 技能库 / 设置）：左导航 + 主区
  // 导航与项目页同构（2026-10-01）：展开态同时显示「图标+文字」，可拖宽 160–280px、
  // 悬浮「‹」收起、左缘把手唤出；宽度/收起态与项目页共用（app-nav-v3 / ui.nav-collapsed）。
  if (!pid || view === "projects" || view === "intel" || view === "settings" && !pid) {
    return (
      <div className="app-shell flex h-screen flex-col">
        {eventsWatcher}
        <Group orientation="horizontal" className="flex min-h-0 w-full flex-1"
               defaultLayout={navLayout.defaultLayout}
               onLayoutChanged={navLayout.onLayoutChanged}>
          {!navCollapsed && (
            <>
              <Panel id="nav" minSize={160} maxSize={280} defaultSize={180}>
                <NavRail active={view} onSelect={setView} locked={!pid} expanded className="h-full w-full" proposalsN={proposalsN} track={meta?.track} />
              </Panel>
              <Separator className="nav-sep w-0.5 shrink-0 bg-transparent transition-colors hover:bg-accent data-[separator=active]:bg-accent">
                <button className="nav-edge-btn" title="收起侧栏" aria-label="收起侧栏" onClick={toggleNavCollapsed}>
                  <ChevronLeft size={12} />
                </button>
              </Separator>
            </>
          )}
          <Panel id="main">
            <div className="relative h-full min-h-0">
              {/* 无独立标题栏：顶部 36px 为拖动区，窗口按钮悬浮右上角；
                  让位由各视图自身根节点的 pt-9 承担（视图背景因此一直铺到最顶，避免接缝） */}
              <div className="window-drag-strip" />
              <WindowControls className="window-controls-float" />
              <main className="app-content h-full min-h-0 min-w-0 overflow-hidden">
                {view === "intel"
                  ? <IntelView />
                  : view === "trash"
                    ? <TrashView />
                  : view === "proxy"
                    ? <ProxyPoolView />
                  : view === "knowledge"
                    ? <KnowledgeView />
                    : view === "skills"
                      ? <SkillsView focus={settingsNav?.skill ? { ...settingsNav.skill, n: settingsNav.n } : null} />
                  : view === "settings" && !pid
                    ? <SettingsView nav={settingsNav} />
                    : <ProjectsView onOpen={openProject} />}
              </main>
            </div>
          </Panel>
        </Group>
        {navCollapsed && <NavReveal onExpand={toggleNavCollapsed} />}
      </div>
    )
  }

  // 工作台左栏停靠条件：agents 视图 + 有项目（此时 NavRail 会渲染 .nav-rail-slot）。
  const railActive = view === "agents" && !!pid

  return (
    <NavRailSlotContext.Provider value={railActive ? railSlot : null}>
    <div className="app-shell flex h-screen flex-col overflow-hidden">
      {eventsWatcher}
      <div className="flex min-h-0 flex-1">
        <Group orientation="horizontal" className="flex min-h-0 w-full"
               defaultLayout={navLayout.defaultLayout}
               onLayoutChanged={navLayout.onLayoutChanged}>
          {/* 左：项目导航（可拖拽 160–280px；悬浮「‹」收起，左缘把手唤出）。
              agents 视图下本列下半还停靠工作台的 agent 切换器 + 会话线程列表。 */}
          {!navCollapsed && (
            <>
              <Panel id="nav" minSize={160} maxSize={280} defaultSize={180}>
                <NavRail active={view} onSelect={setView} locked={!pid} expanded className="h-full w-full" proposalsN={proposalsN}
                         railActive={railActive} railSlotRef={setRailSlot} track={meta?.track} />
              </Panel>
              <Separator className="nav-sep w-0.5 shrink-0 bg-transparent transition-colors hover:bg-accent data-[separator=active]:bg-accent">
                <button className="nav-edge-btn" title="收起侧栏" aria-label="收起侧栏" onClick={toggleNavCollapsed}>
                  <ChevronLeft size={12} />
                </button>
              </Separator>
            </>
          )}
          {/* 中：主区（直播间在 live 视图与黑板同屏共存） */}
          <Panel id="main">
            <div className="flex h-full min-h-0 flex-col overflow-hidden">
            {/* 顶栏（2026-10-14 方案 A+B 改版）：实底 surface-1 + 1px 底边线收口；
                项目信息由「英文 eyebrow + 无框标题行」改为胶囊卡（当时参照中栏的
                .wb-agent-trigger，该类已于 2026-10-16 删除）；统计从项目名右侧移到最右
                （与「首页」同组）并改中点分隔。样式见 index.css 的 .topbar / .project-card 段。 */}
            <div className="topbar flex h-16 shrink-0 items-center gap-4 px-5">
              <div className="mobile-brand"><div className="brand-mark"><Sparkles size={15} /></div><span>Obsius</span></div>
              <div className="project-card"><span className="project-pulse" /><h1 className="project-name">{meta?.name ?? "加载项目"}</h1><Badge variant="outline" className="project-badge">{meta ? bindingBadge(meta.track, meta.experts) : "…"}</Badge></div>
              <span className="flex-1" />
              {meta && <div className="project-stats"><span><Blocks size={13} />{meta.findings} 发现</span><span className="project-stats-sep">·</span><span title={`资产数只计二级子节点（根节点 → 二级子节点 → 三级 URL）；已测干净 ${meta.assets_clean ?? 0} / ${meta.assets}`}><Globe2 size={13} />已测干净 {meta.assets_clean ?? 0}/{meta.assets} · {meta.assets_clean_pct ?? 0}%</span></div>}
              <Button size="sm" variant="ghost" className="back-project" onClick={goHome}><ArrowLeft size={15} />首页</Button>
              <WindowControls className="-mr-5" />
            </div>
            <main className={cn("min-h-0 min-w-0 flex-1 bg-(--surface-0)", view === "live" ? "flex" : "overflow-hidden")}>
            <ErrorBoundary>
          {view === "live" && (
            <>
              <Group orientation="horizontal" className="flex min-h-0 w-full"
                     defaultLayout={liveBoardLayout.defaultLayout}
                     onLayoutChanged={liveBoardLayout.onLayoutChanged}>
                <Panel id="live-main" minSize={500} defaultSize={65}>
                  <div className="h-full min-w-0">
                    <LiveRoom pid={pid} focusSession={sessionNav} />
                  </div>
                </Panel>
                {boardOpen && (
                  <>
                    <Separator className="w-0.5 shrink-0 bg-transparent transition-colors hover:bg-accent" />
                    <Panel id="live-aside" minSize={320} maxSize={560} defaultSize={35}>
                      <aside className="h-full w-full border-l">
                        {profile === "rev-generic"
                          ? <RevCompact pid={pid} onOpenWorkbench={() => setView("board")} />
                          : <Blackboard pid={pid} compact track={meta?.track}
                                        capabilities={meta?.directions} defaultView={boardViewOf(meta)} />}
                      </aside>
                    </Panel>
                  </>
                )}
              </Group>
              <button
                onClick={() => setBoardOpen(!boardOpen)}
                className="w-5 shrink-0 border-l text-[10px] text-muted-foreground hover:bg-accent"
                title="折叠/展开黑板侧栏"
              >
                {boardOpen ? "›" : "‹"}
              </button>
            </>
          )}
          {pid && boardSeenPid === pid && (
            // 黑板 Keep-alive（2026-09-30）：首次点开才挂载；同项目切视图不卸载只
            // hidden 隐藏，切回原样恢复（滚动/选中/已拉详情全保留）；换项目因
            // key={pid} 全新挂载=全量重载。嵌套 ErrorBoundary：隐藏的黑板若崩了
            // 不影响当前正在看的视图（错误状态随 key={pid} 逐项目重置）。
            // main 是 h-screen flex 行的定高 flex 项：h-full 相对其内容盒可解析，
            // 打通 main→wrapper→黑板的高度链（rev 三栏/评估攻击链画布都需要定高；
            // 列表/资产/函数库内部本就是 ScrollArea flex-1）。
            <ErrorBoundary key={pid}>
              <div className={cn(profile === "rev-generic" ? "h-full w-full overflow-hidden" : "h-full w-full", view !== "board" && "hidden")}>
                {profile === "rev-generic"
                  ? <ReverseWorkbench key={pid} pid={pid} active={view === "board"} track={meta?.track} />
                  : <Blackboard key={pid} pid={pid} track={meta?.track} capabilities={meta?.directions}
                                defaultView={boardViewOf(meta)} />}
              </div>
            </ErrorBoundary>
          )}
          {view === "agents" && <AgentWorkbenchView pid={pid} meta={meta}
                                                    focusThread={focusChatTid}
                                                    onFocusConsumed={() => setFocusChatTid(null)} />}
          {view === "diagnostics" && <DiagnosticsView pid={pid} />}
          {view === "proposals" && <ProposalsView pid={pid ?? undefined} />}
          {view === "browser" && (
            // F6 内置浏览器：轨门控（非 pentest/redteam 整页灰显）在视图内部处理；
            // 定高视图（面板组），照 rev 走 h-full + overflow-hidden
            <div className="h-full w-full overflow-hidden">
              <BrowserView pid={pid} track={meta?.track} />
            </div>
          )}
          {view === "fuzzer" && (
            // Web Fuzzer 重放工作台（2026-10-07）：轨门控在视图内部处理；
            // 定高双栏（react-resizable-panels），照 rev/browser 走 h-full + overflow-hidden
            <div className="h-full w-full overflow-hidden">
              <FuzzerView key={fuzzerNav?.n} pid={pid} track={meta?.track}
                          initialRaw={fuzzerNav?.raw} />
            </div>
          )}
          {view === "proxy" && <ProxyPoolView pid={pid} />}
          {view === "sample-analysis" && (
            <div className="h-full w-full overflow-hidden">
              <SampleAnalysisView pid={pid} track={meta?.track} onOpenBinary={(sha) => {
                setView("board")
                window.setTimeout(() => window.dispatchEvent(new CustomEvent("open-binary", { detail: { sha } })), 0)
              }} />
            </div>
          )}
          {view === "knowledge" && <KnowledgeView pid={pid} />}
          {view === "skills" && <SkillsView pid={pid} focus={settingsNav?.skill ? { ...settingsNav.skill, n: settingsNav.n } : null} />}
          {view === "settings" && <SettingsView nav={settingsNav} pid={pid} />}
            </ErrorBoundary>
            </main>
            </div>
          </Panel>
        </Group>
        {navCollapsed && <NavReveal onExpand={toggleNavCollapsed} />}
      </div>
    </div>
    </NavRailSlotContext.Provider>
  )
}
