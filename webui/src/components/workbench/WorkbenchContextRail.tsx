import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Terminal } from "@xterm/xterm"
import { FitAddon } from "@xterm/addon-fit"
import "@xterm/xterm/css/xterm.css"
import { FolderTree, Globe2, RefreshCw, ShieldAlert, TerminalSquare } from "lucide-react"
import { api } from "@/lib/api"
import type { BrowserSessionInfo, BrowserState, WorkspaceTreeResponse } from "@/lib/types"
import { WorkspaceTree } from "./WorkspaceTree"
import { FindingsRail } from "./FindingsRail"
import { cn } from "@/lib/utils"

export function WorkbenchContextRail({ pid, tid, workDir, track }: {
  pid: string
  tid: string | null
  workDir?: string | null
  track?: string
}) {
  const [selected, setSelected] = useState<"browser" | "terminal" | "files" | "findings">(
    () => (window.localStorage.getItem("ui.wb-side-pane") as "browser" | "terminal" | "files" | "findings") || "browser")
  const [open, setOpen] = useState(false)
  const [railWidth, setRailWidth] = useState(360)
  const [dragging, setDragging] = useState(false)
  const dragOrigin = useRef<{ x: number; width: number } | null>(null)
  const [browser, setBrowser] = useState<BrowserState | null>(null)
  const [tree, setTree] = useState<WorkspaceTreeResponse | null>(null)
  const tab = selected === "findings" ? "browser" : selected
  useEffect(() => { window.localStorage.setItem("ui.wb-side-pane", selected) }, [selected])
  const [loadingTree, setLoadingTree] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [terminalId, setTerminalId] = useState<string | null>(null)
  const terminalPidRef = useRef<string | null>(null)
  const terminalRef = useRef<HTMLDivElement | null>(null)
  const terminalInstanceRef = useRef<Terminal | null>(null)
  const terminalFitRef = useRef<FitAddon | null>(null)

  const refreshBrowser = useCallback(() => {
    let alive = true
    api.browserState(pid).then((value) => { if (alive) setBrowser(value) }).catch(() => {})
    return () => { alive = false }
  }, [pid])
  useEffect(() => {
    if (!open) return
    const cleanup = refreshBrowser()
    const timer = window.setInterval(refreshBrowser, 3000)
    return () => { cleanup?.(); window.clearInterval(timer) }
  }, [open, refreshBrowser])

  const refreshTree = useCallback(async () => {
    setLoadingTree(true); setError(null)
    try { setTree(await api.workspaceTree(pid)) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setLoadingTree(false) }
  }, [pid])
  useEffect(() => {
    if (open && tab === "files" && !tree) void refreshTree()
  }, [open, tab, tree, refreshTree])

  const agentSessions = useMemo(() => {
    const sessions = (browser?.sessions ?? []).filter((s) => s.origin === "agent")
    const currentSid = tid ? `chat-${tid.slice(-12)}` : ""
    return [...sessions].sort((a, b) => Number(b.sid === currentSid) - Number(a.sid === currentSid))
  }, [browser, tid])

  const desktop = typeof window !== "undefined" ? window.desktopBrowser : undefined
  const onDragStart = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault()
    dragOrigin.current = { x: e.clientX, width: railWidth }
    setDragging(true)
  }
  useEffect(() => {
    if (!dragging) return
    const move = (e: MouseEvent) => {
      const origin = dragOrigin.current
      if (origin) {
        const delta = origin.x - e.clientX
        const stepped = Math.round(delta / 4) * 4
        setRailWidth(Math.max(320, Math.min(620, origin.width + stepped)))
      }
    }
    const up = () => setDragging(false)
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up)
    document.body.classList.add("wb-dragging")
    return () => {
      document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up)
      document.body.classList.remove("wb-dragging")
      dragOrigin.current = null
    }
  }, [dragging])
  const activateBrowser = async (sid: string) => {
    if (!desktop) return
    const tabs = await desktop.listTabs()
    const tab = tabs.find((item) => item.type === "browser" && item.sid === sid)
    if (tab) await desktop.activateTab(tab.id)
  }
  useEffect(() => {
    if (!desktop || !terminalId || tab !== "terminal" || !open || !terminalRef.current) return
    const styles = getComputedStyle(document.documentElement)
    const token = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback
    const terminal = new Terminal({
      cursorBlink: true, convertEol: true, fontFamily: token("--font-mono", "ui-monospace, monospace"), fontSize: 11,
      scrollback: 5000,
      theme: {
        background: token("--viz-terminal", "#11100d"),
        foreground: token("--viz-terminal-text", "#e7e3dc"),
        cursor: token("--primary", "#e8895b"),
        selectionBackground: token("--accent", "#3a3029"),
      },
    })
    const fit = new FitAddon()
    terminal.loadAddon(fit)
    terminal.open(terminalRef.current)
    fit.fit()
    terminal.focus()
    desktop.terminalResize(terminalId, terminal.cols, terminal.rows)
    const input = terminal.onData((data) => desktop.terminalWrite(terminalId, data))
    const resize = new ResizeObserver(() => {
      const width = terminalRef.current?.clientWidth ?? 0
      terminal.options.fontSize = width < 360 ? 10 : width < 480 ? 11 : 12
      fit.fit(); desktop.terminalResize(terminalId, terminal.cols, terminal.rows)
    })
    resize.observe(terminalRef.current)
    const off = desktop.onTerminalData((event) => { if (event.id === terminalId) terminal.write(event.data) })
    terminalInstanceRef.current = terminal; terminalFitRef.current = fit
    return () => { off(); input.dispose(); resize.disconnect(); terminal.dispose(); terminalInstanceRef.current = null; terminalFitRef.current = null }
  }, [desktop, terminalId, tab, open])
  useEffect(() => {
    if (!desktop || !open || tab !== "terminal") return
    let alive = true
    void desktop.listTabs().then(async (tabs) => {
      if (!alive) return
      let current = tabs.find((t) => t.type === "terminal" && t.pid === pid)
      if (!current && desktop.createTerminalTab) {
        const created = await desktop.createTerminalTab(pid, workDir ?? undefined)
        if (!("error" in created)) current = created
      }
      if (alive) { setTerminalId(current?.id ?? null); terminalPidRef.current = current ? pid : null }
    })
    return () => {
      alive = false
      if (terminalPidRef.current && terminalPidRef.current !== pid && terminalId) void desktop.terminalKill(terminalId)
    }
  }, [desktop, open, tab, pid, workDir, terminalId])
  const selectPane = (next: "browser" | "terminal" | "files" | "findings") => {
    if (open && selected === next) { setOpen(false); return }
    setSelected(next); setOpen(true)
  }
  const labels = { browser: "浏览器", terminal: "终端", files: "文件", findings: "发现" } as const
  const icons = { browser: Globe2, terminal: TerminalSquare, files: FolderTree, findings: ShieldAlert } as const
  return (
    <>
      {!open && <button type="button" className="wb-drawer-trigger" onClick={() => setOpen(true)} title="打开工具抽屉" aria-label="打开工具抽屉"><span className="wb-drawer-trigger-arrow" aria-hidden>‹</span></button>}
      {open && <div className="wb-drawer">
        <div className="wb-drawer-splitter" onMouseDown={onDragStart} role="separator" aria-label="拖动调整抽屉宽度" />
        <div className="wb-drawer-panel" style={{ width: railWidth, flexBasis: railWidth }}>
          <div className="wb-drawer-head"><b>{labels[selected]}</b><button onClick={() => setOpen(false)} title="关闭">×</button></div>
          <div className="wb-drawer-tabs">
            {(["browser", "terminal", "files", "findings"] as const).map((kind) => {
              if (kind === "terminal" && !desktop) return null
              const Icon = icons[kind]
              return <button key={kind} className={cn(selected === kind && "is-on")} onClick={() => selectPane(kind)}><Icon size={14} />{labels[kind]}</button>
            })}
          </div>
          {selected === "findings" && <FindingsRail pid={pid} track={track} embedded visible widthOverride={railWidth} />}
          {selected !== "findings" && (
          <div className="wb-context-body">
            {selected === "browser" && <>
              {!agentSessions.length && <p className="wb-context-muted">当前会话尚未使用浏览器</p>}
              {agentSessions.map((s: BrowserSessionInfo) => <button className="wb-context-card wb-context-card-button" key={s.sid} onClick={() => void activateBrowser(s.sid)} title="在 Electron 中激活此浏览器">
                <div className="wb-context-card-title"><Globe2 size={13} /><b>{s.title || "AI 浏览器"}</b></div>
                <div className="wb-context-card-url">{s.url || "尚未打开页面"}</div>
                <small>{s.paused ? "已暂停" : "活动中"} · {s.sid}</small>
              </button>)}
            </>}
            {selected === "terminal" && <div className="wb-context-terminal-wrap">
              {terminalId ? <div ref={terminalRef} className="wb-context-terminal" /> : <p className="wb-context-muted">当前项目尚未打开 PowerShell 终端</p>}
            </div>}
            {selected === "files" && <>
              <button className="wb-context-refresh" onClick={() => void refreshTree()} disabled={loadingTree}><RefreshCw size={12} />刷新工作区</button>
              {error && <p className="wb-context-error">{error}</p>}
              {tree && <WorkspaceTree pid={pid} nodes={tree.nodes} truncated={tree.truncated} />}
            </>}
          </div>)}
        </div>
      </div>}
    </>
  )
}
