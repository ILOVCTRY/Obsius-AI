import { useCallback, useEffect, useState } from "react"
import { NewShell } from "@/shell2/NewShell"
import { Group, Panel, Separator, useDefaultLayout } from "react-resizable-panels"
import { api, ApiError } from "@/lib/api"
import type { ProjectDetail } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { LiveRoom } from "@/views/LiveRoom"
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

type View = "projects" | "intel" | "live" | "board" | "tasks" | "approvals" | "browser" | "settings"

/** M4c 场景档 board_view 默认视图（config.board_view.default；黑板上自行校验可用 tab 回退） */
const boardViewOf = (m: ProjectDetail | null): string | undefined => {
  const v = (m?.config as { board_view?: { default?: unknown } } | undefined)?.board_view?.default
  return typeof v === "string" && v ? v : undefined
}

const NAV: { key: View; label: string; icon: string; needsProject: boolean }[] = [
  { key: "projects", label: "项目", icon: "◈", needsProject: false },
  { key: "intel", label: "情报", icon: "📡", needsProject: false },
  { key: "live", label: "会话", icon: "◉", needsProject: true },
  { key: "board", label: "黑板", icon: "▤", needsProject: true },
  { key: "tasks", label: "任务", icon: "▦", needsProject: true },
  { key: "browser", label: "浏览器", icon: "🌐", needsProject: true },
  { key: "approvals", label: "审批", icon: "⚑", needsProject: true },
  { key: "settings", label: "技能/设置", icon: "⚙", needsProject: false },
]

function NavRail({ active, onSelect, locked, className }: {
  active: View
  onSelect: (key: View) => void
  locked: boolean // pid 缺失时 needsProject 项禁用
  className?: string
}) {
  return (
    <nav className={cn("flex shrink-0 flex-col items-center gap-1 border-r py-3", className)}>
      {NAV.map((n) => (
        <button
          key={n.key}
          disabled={n.needsProject && locked}
          onClick={() => onSelect(n.key)}
          title={n.label}
          className={cn(
            "flex w-12 flex-col items-center gap-0.5 rounded-md py-2 text-[10px]",
            active === n.key ? "bg-secondary text-primary" : "text-muted-foreground hover:bg-accent",
            n.needsProject && locked && "opacity-30",
          )}
        >
          <span className="text-base leading-none">{n.icon}</span>
          {n.label}
        </button>
      ))}
    </nav>
  )
}

/**
 * 壳选择（M6 起默认新壳，2026-09-25）：
 * ?shell=1/2 显式参数优先；其次 localStorage ui.shell；
 * 从未选过（null）= 新壳。旧壳保留观察、不退役。
 */
function shell2Wanted(): boolean {
  const q = new URLSearchParams(location.search).get("shell")
  if (q === "1" || q === "2") return q === "2"
  try {
    const v = localStorage.getItem("ui.shell")
    return v === "1" ? false : true // null / "2" 皆新壳
  } catch {
    return true
  }
}

export default function App() {
  const [shell2] = useState(shell2Wanted)

  // ?shell= 参数落 localStorage 并剥参数（1/2 统一处理；两壳刷新后语义稳定）
  useEffect(() => {
    const u = new URL(location.href)
    const s = u.searchParams.get("shell")
    if (s !== "1" && s !== "2") return
    try { localStorage.setItem("ui.shell", s) } catch { /* 隐私模式 */ }
    u.searchParams.delete("shell")
    history.replaceState(null, "", u.toString())
  }, [])

  if (shell2) return <NewShell />
  return <LegacyApp />
}

/** 旧壳 → 新壳（M6：并存观察期的双向通道，与新壳 SideBar「旧壳」对称） */
function ToShell2Button({ className }: { className?: string }) {
  const go = () => {
    try { localStorage.setItem("ui.shell", "2") } catch { /* 隐私模式 */ }
    location.reload()
  }
  return (
    <Button size="sm" variant="ghost" onClick={go} className={className}>
      新壳 →
    </Button>
  )
}

function LegacyApp() {
  const [pid, setPid] = useState<string | null>(null)
  const [meta, setMeta] = useState<ProjectDetail | null>(null)
  const [view, setView] = useState<View>("projects")
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const [boardOpen, setBoardOpen] = useState(true)
  // 直播间「复盘沉淀」完成 → 跨视图跳到设置指定 tab；skill.routed 双击 → 带 skill 深链选中
  const [settingsNav, setSettingsNav] = useState<{
    tab: string; n: number; skill?: { source: "cap" | "track"; pack: string; name: string }
  } | null>(null)
  // A3 任务流双击无会话节点 → 跳任务看板并高亮定位卡片（focus nonce 触发滚动）
  const [taskNav, setTaskNav] = useState<{ id: string; n: number } | null>(null)
  // 会话中心化（2026-09-25）：旧壳冻结提示条，本会话内可关
  const [frozenHidden, setFrozenHidden] = useState(false)
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
    id: "app-nav", panelIds: ["nav", "main"], storage: window.localStorage,
    onlySaveAfterUserInteractions: true,
  })
  const liveBoardLayout = useDefaultLayout({
    id: "live-board", panelIds: ["live-main", "live-aside"], storage: window.localStorage,
    onlySaveAfterUserInteractions: true,
  })

  // 无项目上下文的视图（项目列表 / 全局情报页 / 设置 §16.4）：左导航 + 主区
  if (!pid || view === "projects" || view === "intel" || view === "settings" && !pid) {
    return (
      <div className="flex h-screen">
        <NavRail active={view} onSelect={setView} locked={!pid} className="w-14" />
        <main className="relative min-w-0 flex-1 overflow-auto">
          <div className="absolute right-2 top-2 z-10">
            <ToShell2Button />
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
    <div className="flex h-screen flex-col">
      {/* 全局顶栏：项目名 + 审批铃铛 */}
      <header className="flex h-11 shrink-0 items-center gap-3 border-b px-4">
        <Badge variant="outline" className="font-mono">{meta ? bindingBadge(meta.track, meta.experts) : "…"}</Badge>
        <h1 className="text-sm font-semibold">{meta?.name ?? "…"}</h1>
        {meta && (
          <span className="font-mono text-[10px] text-muted-foreground">
            任务 {meta.task_stats.done ?? 0}/{Object.values(meta.task_stats).reduce((a, b) => a + b, 0)} ·
            发现 {meta.findings} · 资产 {meta.assets}
          </span>
        )}
        <span className="flex-1" />
        <button
          onClick={() => setView("approvals")}
          className={cn(
            "flex items-center gap-1 rounded-md px-2 py-1 text-xs",
            view === "approvals" ? "bg-secondary" : "hover:bg-accent",
          )}
        >
          🔔 审批
          {pendingApprovals > 0 && (
            <span className="rounded-full bg-(--status-approval) px-1.5 text-[10px] font-bold text-(--background)">
              {pendingApprovals}
            </span>
          )}
        </button>
        <Button size="sm" variant="ghost" onClick={() => setView("projects")}>← 项目</Button>
        <ToShell2Button />
      </header>

      {/* 旧壳冻结条：会话中心化后新功能只进新壳，旧壳仅保证不崩 */}
      {!frozenHidden && (
        <div className="flex h-7 shrink-0 items-center gap-2 border-b border-amber-400/30 bg-amber-400/10 px-3 text-[11px]">
          <span className="text-amber-300">⚠ 旧壳已冻结——会话看板、会话协作流、中途换智能体等新能力请在新壳使用。</span>
          <span className="flex-1" />
          <button
            onClick={() => {
              try { localStorage.setItem("ui.shell", "2") } catch { /* 隐私模式 */ }
              location.reload()
            }}
            className="rounded-full bg-primary px-2 text-[10px] text-primary-foreground hover:opacity-90"
          >
            进入新壳
          </button>
          <button onClick={() => setFrozenHidden(true)}
            className="text-muted-foreground hover:text-foreground" title="本会话内隐藏">
            ✕
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <Group orientation="horizontal" className="flex min-h-0 w-full"
               defaultLayout={navLayout.defaultLayout}
               onLayoutChanged={navLayout.onLayoutChanged}>
          {/* 左：窄导航（F1 可拖拽 48–220px） */}
          <Panel id="nav" minSize={48} maxSize={220} defaultSize={56}>
            <NavRail active={view} onSelect={setView} locked={!pid} className="h-full w-full" />
          </Panel>
          <Separator className="w-0.5 shrink-0 bg-transparent transition-colors hover:bg-accent data-[active]:bg-accent" />
          {/* 中：主区（直播间在 live 视图与黑板同屏共存） */}
          <Panel id="main">
            <main className={cn("h-full min-w-0", view === "live" ? "flex" : "overflow-auto")}>
          {view === "live" && (
            <>
              <Group orientation="horizontal" className="flex min-h-0 w-full"
                     defaultLayout={liveBoardLayout.defaultLayout}
                     onLayoutChanged={liveBoardLayout.onLayoutChanged}>
                <Panel id="live-main" minSize={320}>
                  <div className="h-full min-w-0">
                    <LiveRoom pid={pid} focusSession={sessionNav} />
                  </div>
                </Panel>
                {boardOpen && (
                  <>
                    <Separator className="w-0.5 shrink-0 bg-transparent transition-colors hover:bg-accent" />
                    <Panel id="live-aside" minSize={288} maxSize={640} defaultSize={384}>
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
      </div>
    </div>
  )
}
