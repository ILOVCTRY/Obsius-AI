import { useEffect, useRef, useState } from "react"
import { ArrowLeft, ArrowRight, ChevronDown, ExternalLink, Plus, RefreshCw, TerminalSquare, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import type { BBEvent, HttpHistoryRow } from "@/lib/types"
import { ActionTimeline } from "./ActionTimeline"
import { ReplayForm, toRawRequest } from "./ReplayForm"
import { cn } from "@/lib/utils"

type TimelineItem = { id: number; kind: string; author: string; summary: string; created_at: string }

export function BrowserView({ pid, track }: { pid: string; track?: string }) {
  const trackOk = !track || track === "pentest" || track === "redteam" || track === "ctf"
  const desktop = typeof window !== "undefined" ? window.desktopBrowser : undefined
  const [tabs, setTabs] = useState<DesktopTab[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [hasHumanTab, setHasHumanTab] = useState(false)
  const [showNewMenu, setShowNewMenu] = useState(false)
  const [panelOpen, setPanelOpen] = useState(true)
  const [timeline, setTimeline] = useState<TimelineItem[]>([])
  const [history, setHistory] = useState<HttpHistoryRow[]>([])
  const [selectedHistory, setSelectedHistory] = useState<HttpHistoryRow | null>(null)
  const [eventCursor, setEventCursor] = useState(0)
  const surfaceRef = useRef<HTMLDivElement | null>(null)
  const createdRef = useRef(false)
  const requestedSessions = useRef(new Set<string>())

  useEffect(() => {
    if (!desktop) return
    let alive = true
    // 用「全局标签表」判定人类主页面是否已存在：human-main 是全应用单例，只看
    // 本项目过滤后的 tabs 会在切回模块（组件重挂、tabs 初值空）时误判为「没有」而重复新建。
    const apply = (all: DesktopTab[]) => {
      if (!alive) return
      const projectTabs = all.filter((tab) => !tab.pid || tab.pid === pid)
      setTabs(projectTabs)
      setActiveId((id) => id && projectTabs.some((tab) => tab.id === id) ? id : projectTabs[0]?.id ?? null)
      setHasHumanTab(all.some((tab) => tab.type === "browser" && tab.sid === "human-main"))
      setLoaded(true)
    }
    void desktop.listTabs().catch(() => []).then(apply)
    const off = desktop.onTabUpdated(apply)
    return () => { alive = false; off() }
  }, [desktop, pid])

  // 默认只开一个标签：等全局标签表加载完，仅当全局还没有 human-main 时新建，且每次挂载最多一次
  // （手动关掉最后一个标签后不再自动重建，与旧行为一致）。
  useEffect(() => {
    if (!desktop || !loaded || createdRef.current) return
    createdRef.current = true
    if (!hasHumanTab) void desktop.createBrowserTab(pid, "human-main")
  }, [desktop, loaded, hasHumanTab, pid])

  useEffect(() => {
    let alive = true
    const sync = async () => {
      try {
        const state = await api.browserState(pid)
        if (!alive) return
        if (!desktop) return
        const known = new Set((await desktop.listTabs()).map((tab) => tab.sid))
        for (const session of state.sessions ?? []) {
          if (session.origin !== "agent" || known.has(session.sid) || requestedSessions.current.has(session.sid)) continue
          requestedSessions.current.add(session.sid)
          const created = await desktop.createBrowserTab(pid, session.sid)
          if ("error" in created) requestedSessions.current.delete(session.sid)
        }
      } catch { /* browser service may still be starting */ }
    }
    void sync()
    const timer = setInterval(sync, 2500)
    return () => { alive = false; clearInterval(timer) }
  }, [desktop, pid])

  const activeTab = tabs.find((tab) => tab.id === activeId) ?? null
  useEffect(() => { if (desktop && activeId) void desktop.activateTab(activeId) }, [desktop, activeId])

  // 原生浏览器页是覆盖在窗口上的 WebContentsView，不由 React 树渲染——本组件卸载
  // （切到其它模块）时必须显式隐藏，否则原生视图会留在原位浮在新视图之上（切走不消失）。
  // 重新挂载时再显示并复用同一 activeId 页；显示由下方 activateTab 效应完成。
  useEffect(() => {
    if (!desktop) return
    desktop.setBrowserVisible(true)
    return () => desktop.setBrowserVisible(false)
  }, [desktop])

  useEffect(() => {
    if (!desktop || !activeTab || activeTab.type !== "browser" || !surfaceRef.current) return
    const update = () => { const rect = surfaceRef.current?.getBoundingClientRect(); if (rect) desktop.setBounds(activeTab.id, { x: rect.left, y: rect.top, width: rect.width, height: rect.height }) }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(surfaceRef.current)
    window.addEventListener("resize", update)
    return () => { observer.disconnect(); window.removeEventListener("resize", update) }
  }, [desktop, activeTab?.id, panelOpen])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const next = await api.events(pid, eventCursor)
        if (!alive || next.length === 0) return
        setEventCursor(next[next.length - 1].id)
        setTimeline((prev) => [...prev, ...next.filter((event) => event.kind.startsWith("browser.")).map(mapEvent)].slice(-200))
      } catch { /* API may be starting */ }
    }
    void load()
    const timer = setInterval(load, 2500)
    return () => { alive = false; clearInterval(timer) }
  }, [pid, eventCursor])

  useEffect(() => {
    let alive = true
    const load = async () => { try { const rows = await api.browserHistory(pid, { limit: 100 }); if (alive) setHistory(rows) } catch { /* no history yet */ } }
    void load()
    const timer = setInterval(load, 3000)
    return () => { alive = false; clearInterval(timer) }
  }, [pid])

  const createBrowser = async () => { setShowNewMenu(false); if (!desktop) return; const created = await desktop.createBrowserTab(pid); if (!("error" in created)) setActiveId(created.id) }
  const createTerminal = async () => { setShowNewMenu(false); if (!desktop) return; const created = await desktop.createTerminalTab(); if (!("error" in created)) setActiveId(created.id) }
  const closeActive = async () => { if (desktop && activeId) await desktop.closeTab(activeId) }

  if (!trackOk) return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">内置浏览器当前轨道不可用</div>

  return <div className="flex h-full min-w-0 flex-col overflow-hidden">
    <div className="flex min-h-10 items-center gap-1 border-b bg-background/90 px-2">
      <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">{tabs.map((tab) => <DesktopTabButton key={tab.id} tab={tab} active={tab.id === activeId} onClick={() => setActiveId(tab.id)} onClose={() => void desktop?.closeTab(tab.id)} />)}</div>
      <div className="relative shrink-0"><Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-xs" onClick={() => setShowNewMenu((open) => !open)} title="新建标签"><Plus size={14} /> 新建</Button>
        {showNewMenu && <div className="absolute right-0 top-8 z-30 w-36 overflow-hidden rounded-md border bg-popover p-1 shadow-lg"><button className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent" onClick={() => void createBrowser()}><ExternalLink size={13} /> 浏览器</button><button className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-accent" onClick={() => void createTerminal()}><TerminalSquare size={13} /> 终端</button></div>}
      </div>
    </div>
    {activeTab?.type === "browser" && <BrowserToolbar tab={activeTab} desktop={desktop} onClose={() => void closeActive()} />}
    <div className="flex min-h-0 flex-1"><div className="relative flex min-w-0 flex-1 flex-col bg-(--viz-terminal-input)/20">{activeTab?.type === "terminal" ? <TerminalPane tab={activeTab} desktop={desktop} /> : <div ref={surfaceRef} className="min-h-0 flex-1" />}{!desktop && <div className="absolute inset-0 flex items-center justify-center bg-background text-center text-xs text-muted-foreground"><div><div className="mb-2 text-2xl">▣</div>请使用 Electron 桌面端打开真实 Chromium 浏览器</div></div>}</div>
      {panelOpen && <aside className="flex w-80 shrink-0 flex-col border-l bg-background/95"><Tabs defaultValue="timeline" className="flex h-full flex-col"><TabsList className="m-2 shrink-0"><TabsTrigger value="timeline">AI 时间线</TabsTrigger><TabsTrigger value="replay">HTTP 重放</TabsTrigger></TabsList><TabsContent value="timeline" className="min-h-0 flex-1"><ActionTimeline events={timeline} /></TabsContent><TabsContent value="replay" className="min-h-0 flex-1 overflow-auto p-2"><ReplayPanel rows={history} selected={selectedHistory} onSelect={setSelectedHistory} pid={pid} /></TabsContent></Tabs></aside>}
      <button className="flex w-5 shrink-0 items-center justify-center border-l text-muted-foreground hover:bg-accent" onClick={() => setPanelOpen((open) => !open)} title={panelOpen ? "收起侧栏" : "展开侧栏"}><ChevronDown size={14} className={panelOpen ? "-rotate-90" : "rotate-90"} /></button>
    </div>
  </div>
}

function BrowserToolbar({ tab, desktop, onClose }: { tab: DesktopTab; desktop?: DesktopBrowserApi; onClose: () => void }) {
  const [url, setUrl] = useState(tab.url === "about:blank" ? "" : tab.url)
  useEffect(() => setUrl(tab.url === "about:blank" ? "" : tab.url), [tab.url])
  const go = () => { if (desktop && url.trim()) void desktop.navigate(tab.id, url.trim().match(/^https?:\/\//) ? url.trim() : `https://${url.trim()}`) }
  return <div className="flex items-center gap-1 border-b px-2 py-1"><Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={() => void desktop?.goBack(tab.id)} title="后退"><ArrowLeft size={14} /></Button><Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={() => void desktop?.goForward(tab.id)} title="前进"><ArrowRight size={14} /></Button><Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={() => void desktop?.reload(tab.id)} title="刷新"><RefreshCw size={14} /></Button><Input className="h-7 min-w-0 flex-1 font-mono text-xs" value={url} onChange={(event) => setUrl(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") go() }} placeholder="输入 URL" /><Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={() => void desktop?.openDevTools(tab.id)} title="开发者工具"><ExternalLink size={14} /></Button><Button size="sm" variant="ghost" className="h-7 w-7 p-0" onClick={onClose} title="关闭标签"><X size={14} /></Button></div>
}

function DesktopTabButton({ tab, active, onClick, onClose }: { tab: DesktopTab; active: boolean; onClick: () => void; onClose: () => void }) {
  return <div className={cn("flex min-w-28 max-w-52 items-center gap-1 rounded-t border px-2 py-1", active ? "border-primary bg-accent" : "border-transparent text-muted-foreground hover:bg-accent/60")}><button className="min-w-0 flex-1 text-left text-[11px]" onClick={onClick}><span className="block truncate">{tab.type === "terminal" ? "终端" : tab.title || "浏览器"}</span><span className="block truncate font-mono text-[9px] text-muted-foreground">{tab.type === "browser" ? tab.url : "PowerShell"}</span></button><button className="text-muted-foreground hover:text-foreground" onClick={onClose} title="关闭"><X size={12} /></button></div>
}

function TerminalPane({ tab, desktop }: { tab: DesktopTab; desktop?: DesktopBrowserApi }) {
  const [output, setOutput] = useState("")
  const [input, setInput] = useState("")
  const preRef = useRef<HTMLPreElement | null>(null)
  useEffect(() => { if (!desktop) return; const off = desktop.onTerminalData((event) => { if (event.id === tab.id) setOutput((current) => (current + event.data).slice(-120000)) }); return off }, [desktop, tab.id])
  useEffect(() => { preRef.current?.scrollTo(0, preRef.current.scrollHeight) }, [output])
  const send = () => { if (!desktop || !input) return; desktop.terminalWrite(tab.id, input + "\r"); setInput("") }
  return <div className="flex min-h-0 flex-1 flex-col bg-(--viz-terminal) p-2"><pre ref={preRef} className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap font-mono text-xs text-(--viz-terminal-text)">{output || "PowerShell · 项目根目录\n"}</pre><div className="mt-2 flex gap-1"><Input className="h-7 flex-1 border-(--viz-terminal-border)/10 bg-(--viz-terminal-input)/20 font-mono text-xs" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") send() }} placeholder="输入命令" /><Button size="sm" className="h-7" onClick={send}>执行</Button></div></div>
}

function ReplayPanel({ rows, selected, onSelect, pid }: { rows: HttpHistoryRow[]; selected: HttpHistoryRow | null; onSelect: (row: HttpHistoryRow) => void; pid: string }) {
  return <div className="space-y-2"><div className="space-y-1">{rows.filter((row) => row.source !== "intruder").slice(0, 30).map((row) => <button key={row.id} className={cn("block w-full rounded border p-1.5 text-left text-[10px] hover:bg-accent", selected?.id === row.id && "border-primary bg-accent")} onClick={() => onSelect(row)}><span className="font-mono font-bold">{row.method} {row.status ?? "-"}</span><span className="ml-1 block truncate text-muted-foreground">{row.url}</span></button>)}</div>{selected ? <ReplayForm key={selected.id} pid={pid} initialRaw={toRawRequest(selected)} /> : <div className="rounded border border-dashed p-3 text-center text-[10px] text-muted-foreground">选择一条 HTTP 请求开始重放</div>}</div>
}

function mapEvent(event: BBEvent): TimelineItem { const payload = event.payload as Record<string, unknown>; const summary = String(payload.action ? `${payload.action} ${payload.url ?? ""}` : (payload.reason ?? event.kind)); return { id: event.id, kind: event.kind, author: event.author, summary: summary.slice(0, 100), created_at: event.created_at } }
