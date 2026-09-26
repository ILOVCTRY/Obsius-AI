import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react"
import { api } from "@/lib/api"
import type { ProjectMeta, Session, Task } from "@/lib/types"
import { IntelView } from "@/views/IntelView"
import { ApprovalsView } from "@/views/ApprovalsView"
import { SettingsView } from "@/views/SettingsView"
import { SideBar } from "./SideBar"
import { ResourcePlaceholder } from "./placeholders"
import { WelcomePane } from "./WelcomePane"
import { ConversationPane } from "./ConversationPane"
import { FilesView } from "./FilesView"
import { TemplatesView } from "./TemplatesView"
import { TOOLS_TAB_KEY } from "./NarrowTabs"
import type { Posture, ResourceKey, Screen, ShellState } from "./types"

// 画布/工具姿态宿主懒加载（内部重型画布二次懒加载，@xyflow/react 不进壳主包）
const CanvasView = lazy(() =>
  import("./CanvasView").then((m) => ({ default: m.CanvasView })))
const ToolsView = lazy(() =>
  import("./ToolsView").then((m) => ({ default: m.ToolsView })))

// TRAE 化新壳（webui-trae-shell M4）：侧栏项目折叠任务行 + 三姿态。
// 工作台=ConversationPane（hidden 保活）；画布/工具=M4 窄 tab 迁入旧视图。

const STATE_KEY = "ui.shell2.state"

function includeExpanded(expanded: string[], pid: string): string[] {
  return expanded.includes(pid) ? expanded : [...expanded, pid]
}

function PostureLoading({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center">
      <p className="rounded border border-dashed px-4 py-2 text-xs text-muted-foreground">
        {label}加载中…
      </p>
    </div>
  )
}

function loadState(): ShellState {
  const fallback: ShellState = { posture: "work", target: null, expanded: [] }
  try {
    const raw = localStorage.getItem(STATE_KEY)
    if (!raw) return fallback
    const s = JSON.parse(raw) as Partial<ShellState>
    return {
      posture: s.posture === "canvas" || s.posture === "tools" ? s.posture : "work",
      target: s.target ?? null,
      expanded: Array.isArray(s.expanded) ? s.expanded : [],
    }
  } catch {
    return fallback
  }
}

export function NewShell() {
  const [state, setState] = useState<ShellState>(loadState)
  const [screen, setScreen] = useState<Screen>({ kind: "posture", posture: state.posture })

  const [projects, setProjects] = useState<ProjectMeta[]>([])
  // 首次项目列表成功前不碰 target（防轮询瞬态/慢响应把落地选中项冲掉）
  const [projectsLoaded, setProjectsLoaded] = useState(false)
  const [tasksMap, setTasksMap] = useState<Record<string, Task[]>>({})
  const [sessionsMap, setSessionsMap] = useState<Record<string, Session[]>>({})
  // 会话行身份显示名：pid → (role id → 名称)
  const [rolesMap, setRolesMap] = useState<Record<string, Record<string, string>>>({})
  const [pending, setPending] = useState(0)

  // ?shell= 参数的 localStorage 落账与剥参已统一在 App 层（M6 起两壳共用）。

  // 状态持久化（姿态/选中/展开）
  useEffect(() => {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(state)) } catch { /* 配额满 */ }
  }, [state])

  // 项目列表 5s
  useEffect(() => {
    let alive = true
    const load = () =>
      api.listProjects()
        .then((ps) => {
          if (!alive) return
          setProjects(ps)
          setProjectsLoaded(true)
        })
        .catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  // 展开项目的任务+会话 5s（折叠即停）
  const expandedKey = state.expanded.join(",")
  useEffect(() => {
    const pids = state.expanded
    if (pids.length === 0) return
    let alive = true
    const load = () =>
      Promise.all(
        pids.map((pid) =>
          Promise.all([api.tasks(pid), api.sessions(pid), api.listRoles(pid)])
            .then(([tasks, sessions, roles]) => [pid, tasks, sessions, roles] as const)
            .catch(() => null)),
      ).then((rs) => {
        if (!alive) return
        const tm: Record<string, Task[]> = {}
        const sm: Record<string, Session[]> = {}
        const rm: Record<string, Record<string, string>> = {}
        for (const r of rs) {
          if (!r) continue
          tm[r[0]] = r[1]
          sm[r[0]] = r[2]
          rm[r[0]] = Object.fromEntries(
            r[3].map((x) => [x.role, x.name || x.role]))
        }
        setTasksMap((prev) => ({ ...prev, ...tm }))
        setSessionsMap((prev) => ({ ...prev, ...sm }))
        setRolesMap((prev) => ({ ...prev, ...rm }))
      })
    load()
    const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandedKey])

  // 选中项目的审批铃铛 5s
  const bellPid = state.target?.pid
  useEffect(() => {
    if (!bellPid) {
      setPending(0)
      return
    }
    let alive = true
    const load = () =>
      Promise.all([api.approvals(bellPid, "pending"), api.getProject(bellPid)])
        .then(([a, d]) => {
          if (alive) setPending(a.length + (d.task_stats.awaiting_human ?? 0))
        })
        .catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [bellPid])

  // 落地态对账：projectsLoaded 后——项目消失→null/最近活跃项目编排器；
  // 目标会话在会话列表中彻底消失（非尚未加载）→ 退回编排器
  useEffect(() => {
    if (!projectsLoaded) return
    if (projects.length === 0) {
      setState((s) => (s.target || s.expanded.length ? { ...s, target: null, expanded: [] } : s))
      return
    }
    const live = new Set(projects.map((p) => p.id))
    // expanded 剪枝：删掉的项目不残留（否则每 5s 对死 pid 轮询 404）
    const pruned = state.expanded.filter((id) => live.has(id))
    const t = state.target
    if (!t || !live.has(t.pid)) {
      const pid = projects[0].id
      setState((s) => ({
        ...s,
        target: { pid, sid: "__orch" },
        expanded: includeExpanded(pruned, pid),
      }))
      return
    }
    if (pruned.length !== state.expanded.length) {
      setState((s) => ({ ...s, expanded: pruned }))
      return
    }
    if (t.sid !== "__orch") {
      const ss = sessionsMap[t.pid]
      if (ss && !ss.some((x) => x.id === t.sid)) {
        setState((s) => ({ ...s, target: { pid: t.pid, sid: "__orch" } }))
      }
    }
  }, [projectsLoaded, projects, state.target, state.expanded, sessionsMap])

  const selectPosture = useCallback((posture: Posture) => {
    setState((s) => ({ ...s, posture }))
    setScreen({ kind: "posture", posture })
  }, [])

  const selectTarget = useCallback((pid: string, sid: string) => {
    // 从任意屏幕点任务行：直接进工作台（M4 起画布/工具姿态会绑定各自呈现）
    setState((s) => ({
      posture: "work",
      target: { pid, sid },
      expanded: includeExpanded(s.expanded, pid),
    }))
    setScreen({ kind: "posture", posture: "work" })
  }, [])

  const toggleExpanded = useCallback((pid: string) => {
    setState((s) => ({
      ...s,
      expanded: s.expanded.includes(pid)
        ? s.expanded.filter((x) => x !== pid)
        : [...s.expanded, pid],
    }))
  }, [])

  const openResource = useCallback((resource: ResourceKey) => {
    // 🔌 插件：工具姿态·MCP tab（预置选中 tab 再切姿态）
    if (resource === "plugins") {
      try { localStorage.setItem(TOOLS_TAB_KEY, "mcp") } catch { /* 忽略 */ }
      setState((s) => ({ ...s, posture: "tools" }))
      setScreen({ kind: "posture", posture: "tools" })
      return
    }
    setScreen({ kind: "resource", resource })
  }, [])

  const handleCreated = useCallback((pid: string) => {
    setState((s) => ({
      posture: "work",
      target: { pid, sid: "__orch" },
      expanded: includeExpanded(s.expanded, pid),
    }))
    setScreen({ kind: "posture", posture: "work" })
  }, [])

  // ConversationPane 切会话行 nonce（切项目走 key=pid 重挂；Date.now 作切换 nonce）；
  // pane 用 "" 表编排器，壳内沿用 "__orch" 哨兵
  const focus = useMemo(() => {
    const t = state.target
    return t ? { sid: t.sid === "__orch" ? "" : t.sid, n: Date.now() } : null
  }, [state.target?.pid, state.target?.sid]) // eslint-disable-line react-hooks/exhaustive-deps

  // 铃铛（拍板 #7）：跳到首个 pending 审批的归属会话（卡在对话内裁决）；
  // 无 pending 仅有 awaiting_human 计数 → 兜底旧审批屏
  const handleBell = useCallback(async () => {
    const t = state.target
    if (!t) { setScreen({ kind: "approvals" }); return }
    try {
      const apprs = await api.approvals(t.pid, "pending")
      if (apprs.length && apprs[0].session_id) {
        selectTarget(t.pid, apprs[0].session_id)
        return
      }
    } catch { /* 落兜底 */ }
    setScreen({ kind: "approvals" })
  }, [state.target, selectTarget])

  // 画布节点双击经 CustomEvent 挂回会话（TaskFlow/AttackPath 沿用旧事件契约）
  useEffect(() => {
    const h = (e: Event) => {
      const sid = (e as CustomEvent<{ sessionId?: string }>).detail?.sessionId
      const pid = state.target?.pid
      if (sid && pid) selectTarget(pid, sid)
    }
    window.addEventListener("goto-session", h)
    return () => window.removeEventListener("goto-session", h)
  }, [state.target?.pid, selectTarget]) // eslint-disable-line react-hooks/exhaustive-deps

  const t = state.target
  const curMeta = useMemo(
    () => projects.find((p) => p.id === t?.pid),
    [projects, t?.pid])
  const showWork = screen.kind === "posture" && screen.posture === "work"

  return (
    <div className="flex h-screen">
      <SideBar
        projects={projects}
        posture={state.posture}
        screen={screen}
        target={t}
        expanded={state.expanded}
        tasksMap={tasksMap}
        sessionsMap={sessionsMap}
        rolesMap={rolesMap}
        pending={pending}
        onSelectPosture={selectPosture}
        onSelectTarget={selectTarget}
        onToggleExpanded={toggleExpanded}
        onOpenResource={openResource}
        onOpenSettings={() => setScreen({ kind: "settings" })}
        onOpenApprovals={handleBell}
        onCreated={handleCreated}
      />

      <main className="h-full min-w-0 flex-1 overflow-hidden">
        {/* 工作台：ConversationPane hidden 保活；无目标=欢迎页 */}
        <div className="h-full w-full" hidden={!showWork}>
          {t
            ? <ConversationPane key={t.pid} pid={t.pid} focusTarget={focus} />
            : <WelcomePane onCreated={handleCreated} />}
        </div>

        {screen.kind === "posture" && screen.posture === "canvas" && (
          t ? (
            <Suspense fallback={<PostureLoading label="画布" />}>
              <CanvasView pid={t.pid} meta={curMeta}
                           onOpenSession={(sid) => selectTarget(t.pid, sid)} />
            </Suspense>
          ) : <WelcomePane onCreated={handleCreated} />
        )}
        {screen.kind === "posture" && screen.posture === "tools" && (
          t ? (
            <Suspense fallback={<PostureLoading label="工具" />}>
              <ToolsView pid={t.pid} track={curMeta?.track} />
            </Suspense>
          ) : <WelcomePane onCreated={handleCreated} />
        )}

        {screen.kind === "resource" && screen.resource === "intel" && <IntelView />}
        {screen.kind === "resource" && screen.resource === "files" && (
          <FilesView pid={t?.pid} />
        )}
        {screen.kind === "resource" && screen.resource === "templates" && (
          <TemplatesView onCreated={handleCreated} />
        )}
        {screen.kind === "resource" && screen.resource !== "intel"
          && screen.resource !== "files" && screen.resource !== "templates" && (
          <ResourcePlaceholder resource={screen.resource} />
        )}

        {screen.kind === "settings" && (
          <SettingsView pid={t?.pid} />
        )}

        {screen.kind === "approvals" && t && (
          <div className="h-full overflow-auto">
            <ApprovalsView pid={t.pid}
                           onGotoTasks={() => setScreen({ kind: "posture", posture: "canvas" })} />
          </div>
        )}
      </main>
    </div>
  )
}
