import { Component, useCallback, useEffect, useState, type ErrorInfo, type ReactNode } from "react"
import { Group, Panel, Separator, useDefaultLayout } from "react-resizable-panels"
import type { LucideIcon } from "lucide-react"
import {
  Activity,
  ArrowLeft,
  Bell,
  Blocks,
  BookOpen,
  Bot,
  BrainCircuit,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  Globe2,
  Inbox,
  FolderKanban,
  FolderTree,
  LayoutDashboard,
  ListChecks,
  Maximize2,
  Minus,
  PanelLeftOpen,
  Radio,
  Settings2,
  Sparkles,
  Square,
  X,
} from "lucide-react"
import { api, ApiError } from "@/lib/api"
import type { Approval, ProjectDetail } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { LiveRoom } from "@/views/LiveRoom"
import { AgentWorkbenchView } from "@/views/AgentWorkbenchView"
import { Blackboard } from "@/views/Blackboard"
import { ReverseWorkbench } from "@/views/reverse/ReverseWorkbench"
import { RevCompact } from "@/views/reverse/RevCompact"
import { deriveWorkbenchProfile } from "@/lib/workbench"
import { useEvents } from "@/lib/useEvents"
import { ProjectsView } from "@/views/ProjectsView"
import { IntelView } from "@/views/IntelView"
import { TaskBoard } from "@/views/TaskBoard"
import { BrowserView } from "@/views/browser/BrowserView"
import { SettingsView } from "@/views/SettingsView"
import { KnowledgeView } from "@/views/KnowledgeView"
import { SkillsView } from "@/views/SkillsView"
import { SampleAnalysisView } from "@/views/SampleAnalysisView"
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

type View = "projects" | "intel" | "live" | "board" | "tasks" | "agents" | "browser" | "sample-analysis" | "knowledge" | "skills" | "settings"

/** M4c 场景档 board_view 默认视图（config.board_view.default；黑板上自行校验可用 tab 回退） */
const boardViewOf = (m: ProjectDetail | null): string | undefined => {
  const v = (m?.config as { board_view?: { default?: unknown } } | undefined)?.board_view?.default
  return typeof v === "string" && v ? v : undefined
}

type NavItem = { key: View; label: string; icon: LucideIcon; needsProject: boolean; homeOnly?: boolean }

const NAV_GROUPS: { label: string; items: NavItem[]; collapsible?: boolean }[] = [
  {
    label: "工作区",
    items: [
      { key: "projects", label: "项目", icon: FolderKanban, needsProject: false },
      { key: "intel", label: "情报", icon: Inbox, needsProject: false, homeOnly: true },
      { key: "agents", label: "智能体", icon: Bot, needsProject: true },
      { key: "board", label: "黑板", icon: LayoutDashboard, needsProject: true },
    ],
  },
  {
    label: "工具",
    items: [
      { key: "browser", label: "浏览器", icon: Globe2, needsProject: true },
      { key: "sample-analysis", label: "样本分析", icon: FolderTree, needsProject: true },
      { key: "knowledge", label: "知识库", icon: BookOpen, needsProject: false },
      { key: "skills", label: "技能库", icon: BrainCircuit, needsProject: false },
      { key: "settings", label: "设置", icon: Settings2, needsProject: false },
    ],
  },
  // 更多（2026-09-30）：会话/任务降级为二级入口，默认折叠收纳（见 NavRail 折叠逻辑）
  {
    label: "更多",
    collapsible: true,
    items: [
      { key: "live", label: "会话", icon: Radio, needsProject: true },
      { key: "tasks", label: "任务", icon: ListChecks, needsProject: true },
    ],
  },
]

function NavRail({ active, onSelect, locked, className, expanded = false }: {
  active: View
  onSelect: (key: View) => void
  locked: boolean
  className?: string
  expanded?: boolean
}) {
  const [moreOpen, setMoreOpen] = useState(
    () => window.localStorage.getItem("ui.nav-more-open") === "1")
  useEffect(() => {
    window.localStorage.setItem("ui.nav-more-open", moreOpen ? "1" : "0")
  }, [moreOpen])

  // 无项目上下文时只展示全局项（needsProject=false：项目/情报/设置）；
  // 进入项目后移除项目入口和 homeOnly 项（情报）——项目工作区内不再显示
  // 「项目」，返回首页清掉 pid 后入口自然恢复。
  // 其余入口仍从 NAV_GROUPS 动态生成；仅对「项目」做显式生命周期过滤。
  const groups = locked
    ? [{ label: "", collapsible: false, items: NAV_GROUPS.flatMap((group) => group.items).filter((item) => !item.needsProject) }]
    : NAV_GROUPS.map((group) => ({
        label: group.label,
        collapsible: group.collapsible,
        items: group.items.filter((item) => !item.homeOnly && item.key !== "projects"),
      })).filter((group) => group.items.length > 0)

  // 「更多」折叠（2026-09-30）：会话/任务降级为二级入口，手动状态持久化（ui.nav-more-open）；
  // 当前视图在其中时强制展开保高亮；窄栏态无标题可点 → 直接展开。
  const moreActive = active === "live" || active === "tasks"

  return (
    <nav className={cn("app-nav flex shrink-0 flex-col border-r", expanded ? "items-stretch" : "items-center", className)}>
      <NavBrand expanded={expanded} />
      <div className="nav-scroll">
        {groups.map((group) => {
          const collapsible = !!group.collapsible
          const open = !collapsible || !expanded || moreOpen || moreActive
          return (
            <div className="nav-group" key={group.label || "home"}>
              {expanded && group.label && (collapsible ? (
                <button
                  className="nav-group-toggle"
                  aria-expanded={open}
                  onClick={() => setMoreOpen((v) => !v)}
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
                  </button>
                )
              })}
            </div>
          )
        })}
      </div>
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
  // 审批铃铛（2026-10-04 审批模块下线后保留）：待审批列表（点击跳有审批的会话）+ awaiting_human 任务数
  const [pendingApprovals, setPendingApprovals] = useState<Approval[]>([])
  const [awaitingHuman, setAwaitingHuman] = useState(0)
  const pendingCount = pendingApprovals.length + awaitingHuman
  void pendingCount
  const [boardOpen, setBoardOpen] = useState(true)
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
  // A3 任务流双击无会话节点 → 跳任务看板并高亮定位卡片（focus nonce 触发滚动）
  const [taskNav, setTaskNav] = useState<{ id: string; n: number } | null>(null)
  // v0.71 任务即窗口：任务卡/任务流双击 → 跳会话页并直开专属执行窗页签
  const [sessionNav, setSessionNav] = useState<{ sid: string; n: number } | null>(null)
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

  useEffect(() => {
    const h = (e: Event) => {
      const id = (e as CustomEvent<{ taskId?: string }>).detail?.taskId
      if (!id) return
      setView("tasks")
      setTaskNav({ id, n: Date.now() })
    }
    window.addEventListener("goto-tasks", h)
    return () => window.removeEventListener("goto-tasks", h)
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

  // 情报页「查看全部」等跨视图跳转（goto-* 自定义事件模式）
  useEffect(() => {
    const h = () => setView("intel")
    window.addEventListener("goto-intel", h)
    return () => window.removeEventListener("goto-intel", h)
  }, [])

  const openProject = useCallback((id: string) => {
    setPid(id)
    setView("live")
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
  const bumpMeta = useCallback(() => {
    if (!pid) return
    api.getProject(pid).then(setMeta).catch(() => {})
  }, [pid])
  const eventsWatcher = <EventsDebouncedRefresh pid={pid} onBump={bumpMeta} />
  // 全局审批铃铛轮询（C1：awaiting_human 任务计入红点，只计数不混 approval 表）
  useEffect(() => {
    if (!pid) {
      setPendingApprovals([])
      setAwaitingHuman(0)
      return
    }
    const load = () =>
      Promise.all([
        api.approvals(pid, "pending"),
        api.getProject(pid),
      ])
        .then(([a, detail]) => {
          setPendingApprovals(a)
          setAwaitingHuman(detail.task_stats.awaiting_human ?? 0)
        })
        .catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [pid])

  // 铃铛点击（2026-10-04）：跳「有审批的会话」（审批模块已下线，决策在对话内联卡完成）；
  // 无会话归属的审批/仅待人工任务 → 退任务看板；都没有 → 直播间。
  const openApprovals = useCallback(() => {
    const target = pendingApprovals.find((a) => a.session_id)
    if (target?.session_id) {
      window.dispatchEvent(new CustomEvent("goto-session", { detail: { sessionId: target.session_id } }))
    } else if (awaitingHuman > 0) {
      setView("tasks")
    } else {
      setView("live")
    }
  }, [pendingApprovals, awaitingHuman])
  void goHome
  void openApprovals

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
                <NavRail active={view} onSelect={setView} locked={!pid} expanded className="h-full w-full" />
              </Panel>
              <Separator className="nav-sep w-0.5 shrink-0 bg-transparent transition-colors hover:bg-accent data-[active]:bg-accent">
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

  return (
    <div className="app-shell flex h-screen flex-col">
      {eventsWatcher}
      <div className="flex min-h-0 flex-1">
        <Group orientation="horizontal" className="flex min-h-0 w-full"
               defaultLayout={navLayout.defaultLayout}
               onLayoutChanged={navLayout.onLayoutChanged}>
          {/* 左：项目导航（可拖拽 160–280px；悬浮「‹」收起，左缘把手唤出） */}
          {!navCollapsed && (
            <>
              <Panel id="nav" minSize={160} maxSize={280} defaultSize={180}>
                <NavRail active={view} onSelect={setView} locked={!pid} expanded className="h-full w-full" />
              </Panel>
              <Separator className="nav-sep w-0.5 shrink-0 bg-transparent transition-colors hover:bg-accent data-[active]:bg-accent">
                <button className="nav-edge-btn" title="收起侧栏" aria-label="收起侧栏" onClick={toggleNavCollapsed}>
                  <ChevronLeft size={12} />
                </button>
              </Separator>
            </>
          )}
          {/* 中：主区（直播间在 live 视图与黑板同屏共存） */}
          <Panel id="main">
            <div className="topbar flex h-16 shrink-0 items-center gap-4 border-b px-5">
              <div className="mobile-brand"><div className="brand-mark"><Sparkles size={15} /></div><span>Obsius</span></div>
              <div className="project-context"><span className="eyebrow">ACTIVE PROJECT</span><div className="project-title"><span className="project-pulse" /><h1>{meta?.name ?? "加载项目"}</h1><Badge variant="outline" className="project-badge">{meta ? bindingBadge(meta.track, meta.experts) : "…"}</Badge></div></div>
              {meta && <div className="project-stats"><span><CheckCircle2 size={13} />{meta.task_stats.done ?? 0}/{Object.values(meta.task_stats).reduce((a, b) => a + b, 0)} 任务</span><span><Blocks size={13} />{meta.findings} 发现</span><span><Globe2 size={13} />{meta.assets} 资产</span></div>}
              <span className="flex-1" />
              <button onClick={openApprovals} className="approval-action" title="待审批动作"><Bell size={16} /><span>审批</span>{pendingCount > 0 && <span className="approval-count">{pendingCount}</span>}</button>
              <Button size="sm" variant="ghost" className="back-project" onClick={goHome}><ArrowLeft size={15} />首页</Button>
              <WindowControls className="-mr-5" />
            </div>
            <main className={cn("h-full min-w-0", view === "live" ? "flex" : "overflow-auto")}>
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
                                        capabilities={meta?.capabilities} defaultView={boardViewOf(meta)} />}
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
                  ? <ReverseWorkbench key={pid} pid={pid} active={view === "board"} />
                  : <Blackboard key={pid} pid={pid} track={meta?.track} capabilities={meta?.capabilities}
                                defaultView={boardViewOf(meta)} />}
              </div>
            </ErrorBoundary>
          )}
          {view === "tasks" && <TaskBoard pid={pid} focused={taskNav} />}
          {view === "agents" && <AgentWorkbenchView pid={pid} meta={meta} />}
          {view === "browser" && (
            // F6 内置浏览器：轨门控（非 pentest/redteam 整页灰显）在视图内部处理；
            // 定高视图（面板组），照 rev 走 h-full + overflow-hidden
            <div className="h-full w-full overflow-hidden">
              <BrowserView pid={pid} track={meta?.track} />
            </div>
          )}
          {view === "sample-analysis" && (
            <div className="h-full w-full overflow-hidden">
              <SampleAnalysisView pid={pid} onOpenBinary={(sha) => {
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
          </Panel>
        </Group>
        {navCollapsed && <NavReveal onExpand={toggleNavCollapsed} />}
      </div>
    </div>
  )
}
