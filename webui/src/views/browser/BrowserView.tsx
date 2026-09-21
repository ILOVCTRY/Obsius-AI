import { useEffect, useRef, useState } from "react"
import { Group, Panel, Separator } from "react-resizable-panels"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import type { BBEvent } from "@/lib/types"
import { ActionTimeline } from "./ActionTimeline"
import { NavShotPane } from "./NavShotPane"
import { CaptureHistory } from "./CaptureHistory"
import { InterceptPanel } from "./InterceptPanel"
import { IntruderForm, ReplayForm } from "./ReplayForm"
import { cn } from "@/lib/utils"

// F6 内置浏览器页（DESIGN.md §7；F6-v3 去会话化）：就是一个普通浏览器——
// 人工走后端隐式会话 human-main（自动创建），AI 会话由 Agent 工具自动管理、
// 任务结束自动清除，前端零会话 UI。抓包可拦截（仅人工流量）。
// 爆破/重发/拦截裁决人类 UI 专属（Agent 无发起入口，红线）。轨外整页灰显。
export function BrowserView({ pid, track }: { pid: string; track?: string }) {
  // v0.70：CTF 轨启用（Web 题需真实浏览器渲染 reCAPTCHA/JS 挑战）；逆向分析/恶意样本轨仍灰显
  const trackOk = track === "pentest" || track === "redteam" || track === "ctf"
  const [status, setStatus] = useState<{ playwright_installed: boolean; install_cmd: string } | null>(null)
  const evCursor = useRef(0)
  const [timeline, setTimeline] = useState<
    { id: number; kind: string; author: string; summary: string; created_at: string }[]>([])

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
          <NavShotPane pid={pid} />
        </Panel>
        <Separator className="w-0.5 bg-transparent transition-colors hover:bg-accent" />
        {/* 右：抓包 | 拦截 | 重发 | 爆破 */}
        <Panel id="br-right" minSize={300} defaultSize={380} maxSize={560}>
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
                贴完整 HTTP 请求报文（请求行 + 头 + 空行 + 体）发送；从「抓包」点「重发」可预填。目标须在资产表。
              </p>
              <ReplayForm pid={pid} />
            </TabsContent>
            <TabsContent value="intruder" className="min-h-0 flex-1 overflow-auto p-2">
              <IntruderForm pid={pid} />
            </TabsContent>
          </Tabs>
        </Panel>
      </Group>
    </div>
  )
}

function isBrowserEvent(e: BBEvent): boolean {
  return e.kind.startsWith("browser.")
}

function mapEvent(e: BBEvent) {
  const p = e.payload as Record<string, unknown>
  const text = String(p.action ? `${p.action} ${p.url ?? ""}` : (p.reason ?? e.kind))
  return { id: e.id, kind: e.kind, author: e.author, summary: text.slice(0, 80), created_at: e.created_at }
}
