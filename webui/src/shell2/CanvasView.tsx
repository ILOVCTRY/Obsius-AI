import { lazy, Suspense, useEffect, useState } from "react"
import { api } from "@/lib/api"
import type { Asset, ProjectMeta, Task } from "@/lib/types"
import { deriveWorkbenchProfile } from "@/lib/workbench"
import { TaskTraceList } from "@/components/blackboard/TaskTraceList"
import { CANVAS_TAB_KEY, NarrowTabs, type NarrowTabItem } from "./NarrowTabs"
import { SessionBoard } from "./SessionBoard"

// 画布姿态（webui-trae-shell M4，2026-09-25）：主区窄 tab + 全宽画布。
// 会话看板 / 发现 / 资产 / 黑板全景 / 协作流 / 攻击路径 / 轨迹链；rev-generic 追加「逆向工作台」。
// 重型画布全部 React.lazy（@xyflow/react 不进壳主包）；SessionBoard 本壳直引。

const BoardGraphCanvas = lazy(() =>
  import("@/views/blackboard/boardGraph/BoardGraphCanvas").then((m) => ({ default: m.BoardGraphCanvas })))
const SessionFlow = lazy(() =>
  import("./SessionFlow").then((m) => ({ default: m.SessionFlow })))
const AttackPathCanvas = lazy(() =>
  import("@/views/blackboard/AttackPath").then((m) => ({ default: m.AttackPath })))
const ReverseWorkbench = lazy(() =>
  import("@/views/reverse/ReverseWorkbench").then((m) => ({ default: m.ReverseWorkbench })))
// 发现/资产列表复用旧壳 Blackboard 模块的导出件（2026-09-26）：lazy 分包，
// 避免模块静态依赖（MappingPane/xlsx 等）进入壳主包。
const FindingsList = lazy(() =>
  import("@/views/Blackboard").then((m) => ({ default: m.Findings })))
const AssetsList = lazy(() =>
  import("@/views/Blackboard").then((m) => ({ default: m.Assets })))

type CanvasTab = "sessions" | "findings" | "assets" | "board" | "taskflow" | "attackpath" | "trace" | "reverse"

const BASE_ITEMS: NarrowTabItem[] = [
  { key: "sessions", label: "会话看板" },
  { key: "findings", label: "发现" },
  { key: "assets", label: "资产" },
  { key: "board", label: "黑板全景" },
  { key: "taskflow", label: "协作流" },
  { key: "attackpath", label: "攻击路径" },
  { key: "trace", label: "轨迹链" },
]

const TAB_VALUES: readonly CanvasTab[] = [
  "sessions", "findings", "assets", "board", "taskflow", "attackpath", "trace", "reverse"]

function loadTab(): CanvasTab {
  try {
    const v = localStorage.getItem(CANVAS_TAB_KEY)
    if (v && (TAB_VALUES as string[]).includes(v)) return v as CanvasTab
  } catch { /* 隐私模式 */ }
  return "sessions"
}

const selectCls =
  "h-6 rounded border bg-background px-1.5 text-[11px] [color-scheme:dark] [&>option]:bg-popover [&>option]:text-popover-foreground"

// ---------- 攻击路径：需资产列表（组件内部含目标选择引导） ----------

function AttackPathHost({ pid }: { pid: string }) {
  const [assets, setAssets] = useState<Asset[]>([])
  useEffect(() => {
    api.assets(pid).then(setAssets).catch(() => {})
  }, [pid])
  return <AttackPathCanvas pid={pid} assets={assets} />
}

// ---------- 轨迹链：任务选择 + 全高时间链 ----------

function TraceHost({ pid }: { pid: string }) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [tid, setTid] = useState("")

  useEffect(() => {
    let alive = true
    const load = () =>
      api.tasks(pid).then((ts) => {
        if (!alive) return
        setTasks(ts)
        setTid((cur) => (cur && ts.some((x) => x.id === cur) ? cur : (ts[0]?.id ?? "")))
      }).catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  return (
    <div className="absolute inset-0 flex flex-col bg-[#0d1117]">
      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-[#21262d] px-3">
        <span className="text-[10px] text-muted-foreground">委托</span>
        <select value={tid} onChange={(e) => setTid(e.target.value)} className={selectCls}>
          {tasks.map((x) => (
            <option key={x.id} value={x.id}>
              {x.task_type} · {x.objective.slice(0, 40)}
            </option>
          ))}
        </select>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {tid
          ? <TaskTraceList pid={pid} taskId={tid} />
          : <p className="text-xs text-muted-foreground">还没有委托——让编排器委派或在会话窗发消息。</p>}
      </div>
    </div>
  )
}

function CanvasLoading() {
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
      <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
        画布加载中…
      </p>
    </div>
  )
}

export function CanvasView({ pid, meta, onOpenSession }: {
  pid: string
  meta?: ProjectMeta
  /** 会话看板卡开窗：回工作台对话姿态 */
  onOpenSession: (sid: string) => void
}) {
  const [tab, setTab] = useState<CanvasTab>(loadTab)
  useEffect(() => {
    try { localStorage.setItem(CANVAS_TAB_KEY, tab) } catch { /* 忽略 */ }
  }, [tab])

  // reverse 重型工作台随画布重生：仅 rev-generic 项目出现该 tab
  const profile = deriveWorkbenchProfile(meta)
  const items: NarrowTabItem[] = profile === "rev-generic"
    ? [...BASE_ITEMS, { key: "reverse", label: "逆向工作台" }]
    : BASE_ITEMS
  useEffect(() => {
    if (tab === "reverse" && profile !== "rev-generic") setTab("board")
  }, [tab, profile])

  return (
    <div className="flex h-full flex-col">
      <NarrowTabs items={items} active={tab} onSelect={(k) => setTab(k as CanvasTab)} />
      <div className="relative min-h-0 flex-1">
        <Suspense fallback={<CanvasLoading />}>
          {tab === "sessions" && <SessionBoard pid={pid} onOpenSession={onOpenSession} />}
          {tab === "findings" && (
            /* showCanvas=false：攻击路径已是独立 tab，不迁列表内嵌的「列表｜链路」切换 */
            <FindingsList pid={pid} track={meta?.track} showCanvas={false} />
          )}
          {tab === "assets" && <AssetsList pid={pid} tree />}
          {tab === "board" && <BoardGraphCanvas pid={pid} track={meta?.track} />}
          {tab === "taskflow" && (
            <SessionFlow pid={pid} onOpenSession={onOpenSession} />
          )}
          {tab === "attackpath" && <AttackPathHost pid={pid} />}
          {tab === "trace" && <TraceHost pid={pid} />}
          {tab === "reverse" && profile === "rev-generic" && <ReverseWorkbench pid={pid} />}
        </Suspense>
      </div>
    </div>
  )
}
