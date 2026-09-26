import { lazy, Suspense, useEffect, useState } from "react"
import { IntelView } from "@/views/IntelView"
import { FilesView } from "./FilesView"
import { NarrowTabs, TOOLS_TAB_KEY, type NarrowTabItem } from "./NarrowTabs"

// 工具姿态（webui-trae-shell M4，2026-09-25）：主区窄 tab + 专用动作界面。
// 浏览器 / 测绘 / 情报 / 产物文件 / 插件·MCP。情报/文件壳内已随资源入口直引，
// 其余懒加载；工具各自管理数据与写口，本层零后端改动。

const BrowserView = lazy(() =>
  import("@/views/browser/BrowserView").then((m) => ({ default: m.BrowserView })))
const MappingPane = lazy(() =>
  import("@/views/blackboard/MappingPane").then((m) => ({ default: m.MappingPane })))
const McpPane = lazy(() =>
  import("@/components/McpPane").then((m) => ({ default: m.McpPane })))

type ToolTab = "browser" | "mapping" | "intel" | "files" | "mcp"

const ITEMS: NarrowTabItem[] = [
  { key: "browser", label: "浏览器" },
  { key: "mapping", label: "测绘" },
  { key: "intel", label: "情报" },
  { key: "files", label: "产物文件" },
  { key: "mcp", label: "插件·MCP" },
]

const TAB_VALUES: readonly ToolTab[] = ["browser", "mapping", "intel", "files", "mcp"]

function loadTab(): ToolTab {
  try {
    const v = localStorage.getItem(TOOLS_TAB_KEY)
    if (v && (TAB_VALUES as string[]).includes(v)) return v as ToolTab
  } catch { /* 隐私模式 */ }
  return "browser"
}

function ToolLoading() {
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
      <p className="rounded border border-dashed border-[#30363d] px-4 py-2 text-xs text-muted-foreground">
        工具加载中…
      </p>
    </div>
  )
}

function NoProjectHint() {
  return (
    <div className="absolute inset-0 flex items-center justify-center p-6">
      <p className="text-xs text-muted-foreground">先在侧栏选择或新建一个项目。</p>
    </div>
  )
}

export function ToolsView({ pid, track }: { pid?: string; track?: string }) {
  const [tab, setTab] = useState<ToolTab>(loadTab)
  useEffect(() => {
    try { localStorage.setItem(TOOLS_TAB_KEY, tab) } catch { /* 忽略 */ }
  }, [tab])

  return (
    <div className="flex h-full flex-col">
      <NarrowTabs items={ITEMS} active={tab} onSelect={(k) => setTab(k as ToolTab)} />
      <div className="relative min-h-0 flex-1">
        <Suspense fallback={<ToolLoading />}>
          {tab === "browser" && (pid ? <BrowserView pid={pid} track={track} /> : <NoProjectHint />)}
          {tab === "mapping" && (pid ? <MappingPane pid={pid} /> : <NoProjectHint />)}
          {tab === "intel" && <IntelView />}
          {tab === "files" && <FilesView pid={pid} />}
          {tab === "mcp" && <McpPane />}
        </Suspense>
      </div>
    </div>
  )
}
