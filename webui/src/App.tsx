import { useCallback, useEffect, useState } from "react"
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
import { ApprovalsView } from "@/views/ApprovalsView"
import { SettingsView } from "@/views/SettingsView"
import { cn } from "@/lib/utils"
import { bindingBadge } from "@/lib/taxonomy"

// 三栏指挥台（DESIGN.md §12 定稿）：左窄导航 / 中主区 / 直播间页右侧黑板常驻侧栏

type View = "projects" | "intel" | "live" | "board" | "tasks" | "approvals" | "settings"

const NAV: { key: View; label: string; icon: string; needsProject: boolean }[] = [
  { key: "projects", label: "项目", icon: "◈", needsProject: false },
  { key: "intel", label: "情报", icon: "📡", needsProject: false },
  { key: "live", label: "直播间", icon: "◉", needsProject: true },
  { key: "board", label: "黑板", icon: "▤", needsProject: true },
  { key: "tasks", label: "任务", icon: "▦", needsProject: true },
  { key: "approvals", label: "审批", icon: "⚑", needsProject: true },
  { key: "settings", label: "技能/设置", icon: "⚙", needsProject: false },
]

export default function App() {
  const [pid, setPid] = useState<string | null>(null)
  const [meta, setMeta] = useState<ProjectDetail | null>(null)
  const [view, setView] = useState<View>("projects")
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const [boardOpen, setBoardOpen] = useState(true)
  // 直播间「复盘沉淀」完成 → 跨视图跳到设置指定 tab
  const [settingsNav, setSettingsNav] = useState<{ tab: string; n: number } | null>(null)
  // A3 任务流双击无会话节点 → 跳任务看板并高亮定位卡片（focus nonce 触发滚动）
  const [taskNav, setTaskNav] = useState<{ id: string; n: number } | null>(null)

  useEffect(() => {
    const h = (e: Event) => {
      const tab = (e as CustomEvent<{ tab?: string }>).detail?.tab
      setView("settings")
      if (tab) setSettingsNav({ tab, n: Date.now() })
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

  // 全局审批铃铛轮询
  useEffect(() => {
    if (!pid) {
      setPendingApprovals(0)
      return
    }
    const load = () =>
      api.approvals(pid, "pending").then((a) => setPendingApprovals(a.length)).catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [pid])

  // 工作台 profile：research+binary ⇒ rev-generic 逆向工作台（其他轨保持渗透模板）
  const profile = deriveWorkbenchProfile(meta)

  // 无项目上下文的视图（项目列表 / 全局情报页 §16.4）：左导航 + 主区
  if (!pid || view === "projects" || view === "intel") {
    return (
      <div className="flex h-screen">
        <nav className="flex w-14 shrink-0 flex-col items-center gap-1 border-r py-3">
          {NAV.map((n) => (
            <button
              key={n.key}
              disabled={n.needsProject && !pid}
              onClick={() => setView(n.key)}
              title={n.label}
              className={cn(
                "flex w-12 flex-col items-center gap-0.5 rounded-md py-2 text-[10px]",
                view === n.key ? "bg-secondary text-primary" : "text-muted-foreground hover:bg-accent",
                n.needsProject && !pid && "opacity-30",
              )}
            >
              <span className="text-base leading-none">{n.icon}</span>
              {n.label}
            </button>
          ))}
        </nav>
        <main className="min-w-0 flex-1 overflow-auto">
          {view === "intel" ? <IntelView /> : <ProjectsView onOpen={openProject} />}
        </main>
      </div>
    )
  }

  return (
    <div className="flex h-screen flex-col">
      {/* 全局顶栏：项目名 + 审批铃铛 */}
      <header className="flex h-11 shrink-0 items-center gap-3 border-b px-4">
        <Badge variant="outline" className="font-mono">{meta ? bindingBadge(meta.track, meta.capabilities) : "…"}</Badge>
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
            <span className="rounded-full bg-[--status-approval] px-1.5 text-[10px] font-bold text-[--background]">
              {pendingApprovals}
            </span>
          )}
        </button>
        <Button size="sm" variant="ghost" onClick={() => setView("projects")}>← 项目</Button>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 左：窄导航 */}
        <nav className="flex w-14 shrink-0 flex-col items-center gap-1 border-r py-3">
          {NAV.map((n) => (
            <button
              key={n.key}
              disabled={n.needsProject && !pid}
              onClick={() => setView(n.key)}
              title={n.label}
              className={cn(
                "flex w-12 flex-col items-center gap-0.5 rounded-md py-2 text-[10px]",
                view === n.key ? "bg-secondary text-primary" : "text-muted-foreground hover:bg-accent",
                n.needsProject && !pid && "opacity-30",
              )}
            >
              <span className="text-base leading-none">{n.icon}</span>
              {n.label}
            </button>
          ))}
        </nav>

        {/* 中：主区（直播间在 live 视图与黑板同屏共存） */}
        <main className={cn("min-w-0 flex-1", view === "live" ? "flex" : "overflow-auto")}>
          {view === "live" && (
            <>
              <div className="min-w-0 flex-1"><LiveRoom pid={pid} /></div>
              {boardOpen && (
                <aside className="w-96 shrink-0 border-l">
                  {profile === "rev-generic"
                    ? <RevCompact pid={pid} onOpenWorkbench={() => setView("board")} />
                    : <Blackboard pid={pid} compact track={meta?.track} capabilities={meta?.capabilities} />}
                </aside>
              )}
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
                : <Blackboard pid={pid} track={meta?.track} capabilities={meta?.capabilities} />}
            </div>
          )}
          {view === "tasks" && <TaskBoard pid={pid} focused={taskNav} />}
          {view === "approvals" && <ApprovalsView pid={pid} />}
          {view === "settings" && <SettingsView nav={settingsNav} />}
        </main>
      </div>
    </div>
  )
}
