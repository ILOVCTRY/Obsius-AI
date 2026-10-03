import { useEffect, useRef, useState } from "react"
import { Group, Panel, Separator } from "react-resizable-panels"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import type { BBEvent, BrowserSessionInfo } from "@/lib/types"
import { ActionTimeline } from "./ActionTimeline"
import { NavShotPane } from "./NavShotPane"
import { CaptureHistory } from "./CaptureHistory"
import { InterceptPanel } from "./InterceptPanel"
import { IntruderForm, ReplayForm } from "./ReplayForm"
import { cn } from "@/lib/utils"

// F6 内置浏览器：项目内真实 Chromium Page 多标签，包含人类主页面和 AI 页面。
export function BrowserView({ pid, track }: { pid: string; track?: string }) {
  // v0.70：CTF 轨启用（Web 题需真实浏览器渲染 reCAPTCHA/JS 挑战）；逆向分析/恶意样本轨仍灰显
  const trackOk = track === "pentest" || track === "redteam" || track === "ctf"
  const [status, setStatus] = useState<{ playwright_installed: boolean; install_cmd: string } | null>(null)
  const evCursor = useRef(0)
  const [timeline, setTimeline] = useState<
    { id: number; kind: string; author: string; summary: string; created_at: string }[]>([])
  const [sessions, setSessions] = useState<BrowserSessionInfo[]>([])
  const [selectedSid, setSelectedSid] = useState<string>(() => {
    try { return localStorage.getItem(`browser.active-sid:${pid}`) ?? "human-main" } catch { return "human-main" }
  })
  const [toolsOpen, setToolsOpen] = useState(false)
  const userSelected = useRef(false)

  useEffect(() => {
    userSelected.current = false
    try {
      const saved = localStorage.getItem(`browser.active-sid:${pid}`)
      userSelected.current = !!saved
      setSelectedSid(saved ?? "human-main")
    } catch { setSelectedSid("human-main") }
  }, [pid])

  // Keep the real Page list fresh. AI sessions are selected by their latest action
  // until the operator explicitly chooses a tab.
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const state = await api.browserState(pid)
        if (!alive) return
        const next = state.sessions ?? []
        setSessions(next)
        const ai = next.filter((s) => s.origin === "agent")
          .sort((a, b) => (b.last_action_at ?? 0) - (a.last_action_at ?? 0))[0]
        setSelectedSid((current) => {
          if (!userSelected.current && ai) return ai.sid
          return next.some((s) => s.sid === current) || current === "human-main" ? current : "human-main"
        })
      } catch { /* browser may be unavailable while starting */ }
    }
    void load()
    const t = setInterval(load, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  useEffect(() => {
    try { localStorage.setItem(`browser.active-sid:${pid}`, selectedSid) } catch { /* storage disabled */ }
  }, [pid, selectedSid])

  // 依赖探测（5s；未装 playwright 时后端接口 503 结构化不 500）
  useEffect(() => {
    let alive = true
    const load = () => api.browserStatus().then((s) => alive && setStatus(s)).catch(() => {})
    void load()
    const t = setInterval(load, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  // browser.* 事件 3s 增量（动作时间线）
  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const next = await api.events(pid, evCursor.current)
        if (!alive || next.length === 0) return
        evCursor.current = next[next.length - 1].id
        setTimeline((prev) => [...prev, ...next.filter(isBrowserEvent).map(mapEvent)].slice(-200))
      } catch { /* 静默 */ }
    }
    void load()
    const t = setInterval(load, 3000)
    return () => { alive = false; clearInterval(t) }
  }, [pid])

  if (!trackOk) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center text-sm text-muted-foreground">
          <div className="mb-2 text-3xl opacity-40">🌐</div>
          内置浏览器仅 pentest / redteam / ctf 轨可用
          <div className="mt-1 text-xs">（当前轨：{track ?? "未知"}）</div>
        </div>
      </div>
    )
  }

  const noPlaywright = status && !status.playwright_installed

  return (
    <div className="flex h-full min-w-0 flex-col">
      {noPlaywright && (
        <div className="flex items-center gap-2 border-b bg-(--status-paused)/10 px-3 py-1.5 text-xs">
          <span>⚠ playwright 未安装——浏览器功能降级（AI 工具同样 no-tool）。</span>
          <code className="rounded bg-accent px-1 py-0.5 font-mono text-[10px]">{status!.install_cmd}</code>
          <Button size="sm" variant="ghost" className="h-5 text-[10px]"
                  onClick={() => { void navigator.clipboard.writeText(status!.install_cmd) }}>
            复制
          </Button>
        </div>
      )}
      <div className="flex items-center gap-2 border-b px-2 py-1">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          <BrowserTab sid="human-main" label="人类主页面" active={selectedSid === "human-main"}
            info={sessions.find((s) => s.sid === "human-main")}
            onClick={() => { userSelected.current = true; setSelectedSid("human-main") }} />
          {sessions.filter((s) => s.sid !== "human-main").map((s) => (
            <BrowserTab key={s.sid} sid={s.sid} label={s.title || s.task_id || s.sid.slice(0, 12)}
              active={selectedSid === s.sid} info={s}
              onClick={() => { userSelected.current = true; setSelectedSid(s.sid) }} />
          ))}
        </div>
        <Button size="sm" variant={toolsOpen ? "default" : "outline"} className="h-7 shrink-0 text-xs"
          onClick={() => setToolsOpen((open) => !open)}>
          {toolsOpen ? "收起工具" : "抓包工具"}
        </Button>
      </div>
      <Group orientation="horizontal" className="flex min-h-0 w-full flex-1">
        {/* 左：动作时间线 */}
        <Panel id="br-left" minSize={180} defaultSize={220} maxSize={320}>
          <div className={cn("flex h-full flex-col", noPlaywright && "pointer-events-none opacity-40")}>
            <ActionTimeline events={timeline} />
          </div>
        </Panel>
        <Separator className="w-0.5 bg-transparent transition-colors hover:bg-accent" />
        {/* 中：导航 + 实时画面 */}
        <Panel id="br-center" minSize={320}>
          <NavShotPane pid={pid} sid={selectedSid}
            initialPaused={sessions.find((s) => s.sid === selectedSid)?.paused}
            onTakeover={(paused) => setSessions((all) => all.map((s) => s.sid === selectedSid ? { ...s, paused } : s))} />
        </Panel>
        {toolsOpen && <Separator className="w-0.5 bg-transparent transition-colors hover:bg-accent" />}
        {toolsOpen && <Panel id="br-right" minSize={300} defaultSize={380} maxSize={560}>
          <Tabs defaultValue="capture" className="flex h-full flex-col">
            <TabsList className="shrink-0">
              <TabsTrigger value="capture">抓包</TabsTrigger>
              <TabsTrigger value="intercept">拦截</TabsTrigger>
              <TabsTrigger value="replay">重发</TabsTrigger>
              <TabsTrigger value="intruder">爆破</TabsTrigger>
            </TabsList>
            <TabsContent value="capture" className="min-h-0 flex-1">
              <CaptureHistory pid={pid} />
            </TabsContent>
            <TabsContent value="intercept" className="min-h-0 flex-1">
              <InterceptPanel pid={pid} />
            </TabsContent>
            <TabsContent value="replay" className="min-h-0 flex-1 overflow-auto p-2">
              <p className="mb-2 text-xs text-muted-foreground">
                贴完整 HTTP 请求报文（请求行 + 头 + 空行 + 体）发送；从「抓包」点「重发」可预填。支持任意目标。
              </p>
              <ReplayForm pid={pid} />
            </TabsContent>
            <TabsContent value="intruder" className="min-h-0 flex-1 overflow-auto p-2">
              <IntruderForm pid={pid} />
            </TabsContent>
          </Tabs>
        </Panel>}
      </Group>
    </div>
  )
}

function BrowserTab({ sid, label, active, info, onClick }: {
  sid: string; label: string; active: boolean; info?: BrowserSessionInfo; onClick: () => void
}) {
  return <button type="button" onClick={onClick}
    className={cn("max-w-52 shrink-0 rounded-md border px-2 py-1 text-left text-[11px] transition-colors",
      active ? "border-primary bg-accent text-foreground" : "border-transparent text-muted-foreground hover:bg-accent/60")}
    title={info?.url ?? sid}>
    <span className="block truncate">{info?.paused ? "⏸ " : ""}{label}</span>
    <span className="block truncate font-mono text-[9px] text-muted-foreground">{info?.url ?? "未连接"}</span>
  </button>
}

function isBrowserEvent(e: BBEvent): boolean {
  return e.kind.startsWith("browser.")
}

function mapEvent(e: BBEvent) {
  const p = e.payload as Record<string, unknown>
  const text = String(p.action ? `${p.action} ${p.url ?? ""}` : (p.reason ?? e.kind))
  return { id: e.id, kind: e.kind, author: e.author, summary: text.slice(0, 80), created_at: e.created_at }
}
