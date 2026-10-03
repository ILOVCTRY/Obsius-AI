import { useCallback, useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api, browserWsUrl } from "@/lib/api"

/** A real Chromium Page rendered through the project's browser websocket. */
export function NavShotPane({ pid, sid, initialPaused = false, onTakeover }: {
  pid: string
  sid: string
  initialPaused?: boolean
  onTakeover?: (paused: boolean) => void
}) {
  const [urlInput, setUrlInput] = useState("")
  const [frame, setFrame] = useState<{ data: string; meta: { w: number; h: number } } | null>(null)
  const [live, setLive] = useState(false)
  const [takeover, setTakeover] = useState(initialPaused)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [navNote, setNavNote] = useState<string | null>(null)
  const isAgentPage = sid !== "human-main"
  const wsRef = useRef<WebSocket | null>(null)
  const imgBoxRef = useRef<HTMLDivElement | null>(null)
  const lastMove = useRef(0)

  useEffect(() => setTakeover(initialPaused), [initialPaused, sid])

  useEffect(() => {
    setFrame(null); setErr(null); setNavNote(null)
    let ws: WebSocket | null = null
    let alive = true
    let delay = 1000
    let timer: ReturnType<typeof setTimeout>
    const connect = () => {
      if (!alive) return
      ws = new WebSocket(browserWsUrl(pid, sid))
      wsRef.current = ws
      ws.onopen = () => { setLive(true); delay = 1000 }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string)
          if (msg.type === "frame") {
            const m = msg.metadata ?? {}
            setFrame({ data: msg.data as string,
              meta: { w: (m.deviceWidth as number) || 1280, h: (m.deviceHeight as number) || 720 } })
          } else if (msg.type === "error") { setErr(msg.message as string); alive = false }
        } catch { /* ignore malformed websocket messages */ }
      }
      ws.onclose = () => {
        setLive(false); wsRef.current = null
        if (alive) { timer = setTimeout(connect, delay); delay = Math.min(delay * 2, 5000) }
      }
      ws.onerror = () => ws?.close()
    }
    connect()
    return () => { alive = false; clearTimeout(timer); ws?.close(); wsRef.current = null }
  }, [pid, sid])

  const sendInput = useCallback((payload: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: "input", ...payload }))
  }, [])
  const doNavigate = useCallback(async (url: string) => {
    if (!url.trim()) return
    setBusy(true); setErr(null); setNavNote(null)
    try { setNavNote((await api.browserNavigate(pid, url.trim(), sid)).final_url) }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }, [pid, sid])
  const doAction = useCallback(async (action: "back") => {
    setBusy(true); setErr(null); setNavNote(null)
    try { setNavNote((await api.browserAction(pid, { action, sid })).final_url ?? null) }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }, [pid, sid])
  const toggleTakeover = async () => {
    const next = !takeover
    setBusy(true); setErr(null)
    try { await api.browserTakeover(pid, sid, next); setTakeover(next); onTakeover?.(next) }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  const frameCoord = useCallback((e: { clientX: number; clientY: number }) => {
    const box = imgBoxRef.current
    if (!box || !frame) return null
    const rect = box.getBoundingClientRect()
    return { x: (e.clientX - rect.left) * frame.meta.w / rect.width,
      y: (e.clientY - rect.top) * frame.meta.h / rect.height }
  }, [frame])
  const onPointer = (kind: "down" | "up" | "click" | "dblclick") => (e: React.MouseEvent) => {
    if (!takeover) return
    const c = frameCoord(e)
    if (c) sendInput({ kind, x: c.x, y: c.y, button: e.button === 2 ? "right" : "left" })
  }
  const onWheel = (e: React.WheelEvent) => {
    if (!takeover) return
    e.preventDefault(); sendInput({ kind: "wheel", delta_x: e.deltaX, delta_y: e.deltaY })
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (!takeover || Date.now() - lastMove.current < 50) return
    lastMove.current = Date.now(); const c = frameCoord(e)
    if (c) sendInput({ kind: "move", x: c.x, y: c.y })
  }
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!takeover) return
    if (e.key === "Enter" || e.key.length === 1 || e.key.startsWith("Arrow") || ["Backspace", "Delete", "Tab", "Escape"].includes(e.key)) {
      e.preventDefault(); sendInput({ kind: "key", key: e.key })
    }
  }
  const onPaste = (e: React.ClipboardEvent) => {
    if (!takeover) return
    const text = e.clipboardData.getData("text")
    if (text) { e.preventDefault(); sendInput({ kind: "type", text }) }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-1 border-b p-2">
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void doAction("back")}>←</Button>
        <Input className="h-7 flex-1 font-mono text-xs" placeholder="https://example.com"
          value={urlInput} onChange={(e) => setUrlInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void doNavigate(urlInput) }} />
        <Button size="sm" disabled={busy || !urlInput.trim()} onClick={() => void doNavigate(urlInput)}>{busy ? "…" : "前往"}</Button>
        {isAgentPage && <Button size="sm" variant={takeover ? "default" : "outline"} disabled={!live || busy}
          onClick={() => void toggleTakeover()} title="暂停或恢复该页面的 AI 输入动作">
          🖱 {takeover ? "释放接管" : "接管"}
        </Button>}
      </div>
      {err && <div className="border-b px-2 py-1 text-xs text-red-400">{err}</div>}
      {navNote && <div className="border-b px-2 py-1 font-mono text-[10px] text-muted-foreground">{navNote}</div>}
      <div className="min-h-0 flex-1 overflow-auto bg-black/40 p-2">
        {frame ? <div ref={imgBoxRef} tabIndex={0}
          className={takeover ? "relative w-full cursor-crosshair outline-2 outline-(--status-ok)" : "relative w-full outline-2 outline-transparent"}
          onPointerDown={onPointer("down")} onPointerUp={onPointer("up")} onClick={onPointer("click")}
          onDoubleClick={onPointer("dblclick")} onPointerMove={onPointerMove} onWheel={onWheel}
          onKeyDown={onKeyDown} onPaste={onPaste}>
          <img alt="实时画面" draggable={false} src={`data:image/jpeg;base64,${frame.data}`} className="w-full select-none" />
          {takeover && <span className="absolute left-1 top-1 rounded bg-(--status-ok) px-1.5 py-0.5 text-[10px] font-bold text-(--background)">🖱 接管中</span>}
        </div> : <div className="p-4 text-center text-xs text-muted-foreground">{live ? "等待画面帧…" : "连接浏览器页面…"}</div>}
        {live && <div className="px-1 pt-1 text-right text-[10px] text-muted-foreground">● 实时</div>}
      </div>
    </div>
  )
}
