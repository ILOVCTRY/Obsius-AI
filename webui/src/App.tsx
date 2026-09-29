import { useCallback, useEffect, useState } from "react"
import { Group, Panel, Separator, useDefaultLayout } from "react-resizable-panels"
import type { LucideIcon } from "lucide-react"
import {
  Activity,
  ArrowLeft,
  Bell,
  Blocks,
  Bot,
  CheckCircle2,
  ChevronLeft,
  Globe2,
  Inbox,
  FolderKanban,
  LayoutDashboard,
  ListChecks,
  PanelLeftOpen,
  Radio,
  Settings2,
  ShieldCheck,
  Sparkles,
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
import { useEvents } from "@/lib/useEvents"
import { ProjectsView } from "@/views/ProjectsView"
import { IntelView } from "@/views/IntelView"
import { TaskBoard } from "@/views/TaskBoard"
import { BrowserView } from "@/views/browser/BrowserView"
import { ApprovalsView } from "@/views/ApprovalsView"
import { SettingsView } from "@/views/SettingsView"
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

type View = "projects" | "intel" | "live" | "board" | "tasks" | "agents" | "approvals" | "browser" | "settings"

/** M4c 场景档 board_view 默认视图（config.board_view.default；黑板上自行校验可用 tab 回退） */
const boardViewOf = (m: ProjectDetail | null): string | undefined => {
  const v = (m?.config as { board_view?: { default?: unknown } } | undefined)?.board_view?.default
  return typeof v === "string" && v ? v : undefined
}

type NavItem = { key: View; label: string; icon: LucideIcon; needsProject: boolean }

const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: "工作区",
    items: [
      { key: "projects", label: "项目", icon: FolderKanban, needsProject: false },
      { key: "intel", label: "情报", icon: Inbox, needsProject: false },
      { key: "live", label: "会话", icon: Radio, needsProject: true },
      { key: "agents", label: "智能体", icon: Bot, needsProject: true },
      { key: "board", label: "黑板", icon: LayoutDashboard, needsProject: true },
      { key: "tasks", label: "任务", icon: ListChecks, needsProject: true },
    ],
  },
  {
    label: "工具",
    items: [
      { key: "browser", label: "浏览器", icon: Globe2, needsProject: true },
      { key: "approvals", label: "审批", icon: ShieldCheck, needsProject: true },
      { key: "settings", label: "技能与设置", icon: Settings2, needsProject: false },
    ],
  },
]

function NavRail({ active, onSelect, locked, className, expanded = false, pendingApprovals = 0 }: {
  active: View
  onSelect: (key: View) => void
  locked: boolean
  className?: string
  expanded?: boolean
  pendingApprovals?: number
}) {
  // 无项目上下文时只展示全局项（needsProject=false：项目/情报/设置），
  // 不再硬编码 key 白名单——8cb819d 曾因硬编码列表与 NAV_GROUPS 漂移致情报入口消失
  const groups = locked
    ? [{ label: "", items: NAV_GROUPS.flatMap((group) => group.items).filter((item) => !item.needsProject) }]
    : NAV_GROUPS

  return (
    <nav className={cn("app-nav flex shrink-0 flex-col border-r", expanded ? "items-stretch" : "items-center", className)}>
      <div className={cn("nav-brand", expanded ? "justify-start px-4" : "justify-center")}>
        <div className="brand-mark"><Sparkles size={15} /></div>
        {expanded && <span>Obsius</span>}
      </div>
      <div className="nav-scroll">
        {groups.map((group) => (
          <div className="nav-group" key={group.label || "home"}>
            {expanded && group.label && <span className="nav-group-label">{group.label}</span>}
            {group.items.map((n) => {
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
                  {n.key === "approvals" && pendingApprovals > 0 && <span className="nav-count">{pendingApprovals}</span>}
                </button>
              )
            })}
          </div>
        ))}
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

export default function App() {
  const [pid, setPid] = useState<string | null>(null)
  const [meta, setMeta] = useState<ProjectDetail | null>(null)
  const [view, setView] = useState<View>("projects")
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const [boardOpen, setBoardOpen] = useState(true)
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
      setView("settings")
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

  // 事件到达即去抖重拉项目元数据（顶栏任务/发现/资产统计；5s 轮询兜底，不另加轮询）
  const { events } = useEvents(pid)
  useEffect(() => {
    if (!pid || events.length === 0) return
    const t = setTimeout(() => {
      api.getProject(pid).then(setMeta).catch(() => {})
    }, 400)
    return () => clearTimeout(t)
  }, [events.length, pid])

  // 全局审批铃铛轮询（C1：awaiting_human 任务计入红点，只计数不混 approval 表）
  useEffect(() => {
    if (!pid) {
      setPendingApprovals(0)
      return
    }
    const load = () =>
      Promise.all([
        api.approvals(pid, "pending"),
        api.getProject(pid),
      ])
        .then(([a, detail]) =>
          setPendingApprovals(a.length + (detail.task_stats.awaiting_human ?? 0)))
        .catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [pid])

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

  // 无项目上下文的视图（项目列表 / 全局情报页 / 设置 §16.4）：左导航 + 主区
  if (!pid || view === "projects" || view === "intel" || view === "settings" && !pid) {
    return (
      <div className="app-shell flex h-screen">
        <NavRail active={view} onSelect={setView} locked={!pid} className="w-16" />
        <main className="app-content relative min-w-0 flex-1 overflow-auto">
          <div className="context-bar">
            <div className="context-label"><span className="eyebrow">OBSIUS / CONTROL CENTER</span><span className="context-title">{view === "intel" ? "情报中心" : view === "settings" ? "设置" : "项目空间"}</span></div>
            <div className="context-status"><span className="status-dot" />在线</div>
          </div>
          {view === "intel"
            ? <IntelView />
            : view === "settings" && !pid
              ? <SettingsView nav={settingsNav} />
              : <ProjectsView onOpen={openProject} />}
        </main>
      </div>
    )
  }

  return (
    <div className="app-shell flex h-screen flex-col">
      <header className="topbar flex h-16 shrink-0 items-center gap-4 border-b px-5">
        <div className="mobile-brand"><div className="brand-mark"><Sparkles size={15} /></div><span>Obsius</span></div>
        <div className="project-context">
          <span className="eyebrow">ACTIVE PROJECT</span>
          <div className="project-title"><span className="project-pulse" /><h1>{meta?.name ?? "加载项目"}</h1><Badge variant="outline" className="project-badge">{meta ? bindingBadge(meta.track, meta.experts) : "…"}</Badge></div>
        </div>
        {meta && <div className="project-stats"><span><CheckCircle2 size={13} />{meta.task_stats.done ?? 0}/{Object.values(meta.task_stats).reduce((a, b) => a + b, 0)} 任务</span><span><Blocks size={13} />{meta.findings} 发现</span><span><Globe2 size={13} />{meta.assets} 资产</span></div>}
        <span className="flex-1" />
        <button onClick={() => setView("approvals")} className={cn("approval-action", view === "approvals" && "is-active")}>
          <Bell size={16} />
          <span>审批</span>
          {pendingApprovals > 0 && <span className="approval-count">{pendingApprovals}</span>}
        </button>
        <Button size="sm" variant="ghost" className="back-project" onClick={() => setView("projects")}><ArrowLeft size={15} />首页</Button>
      </header>

      <div className="flex min-h-0 flex-1">
        <Group orientation="horizontal" className="flex min-h-0 w-full"
               defaultLayout={navLayout.defaultLayout}
               onLayoutChanged={navLayout.onLayoutChanged}>
          {/* 左：项目导航（可拖拽 160–280px；悬浮「‹」收起，左缘把手唤出） */}
          {!navCollapsed && (
            <>
              <Panel id="nav" minSize={160} maxSize={280} defaultSize={180}>
                <NavRail active={view} onSelect={setView} locked={!pid} pendingApprovals={pendingApprovals} expanded className="h-full w-full" />
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
            <main className={cn("h-full min-w-0", view === "live" ? "flex" : "overflow-auto")}>
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
          {view === "board" && (
            // main 是 h-screen flex 行的定高 flex 项：h-full 相对其内容盒可解析，
            // 打通 main→wrapper→黑板的高度链（rev 三栏/评估攻击链画布都需要定高；
            // 列表/资产/函数库内部本就是 ScrollArea flex-1）。
            <div className={profile === "rev-generic" ? "h-full w-full overflow-hidden" : "h-full w-full"}>
              {profile === "rev-generic"
                ? <ReverseWorkbench pid={pid} />
                : <Blackboard pid={pid} track={meta?.track} capabilities={meta?.capabilities}
                              defaultView={boardViewOf(meta)} />}
            </div>
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
          {view === "approvals" && <ApprovalsView pid={pid} onGotoTasks={() => setView("tasks")} />}
          {view === "settings" && <SettingsView nav={settingsNav} pid={pid} />}
            </main>
          </Panel>
        </Group>
        {navCollapsed && <NavReveal onExpand={toggleNavCollapsed} />}
      </div>
    </div>
  )
}
