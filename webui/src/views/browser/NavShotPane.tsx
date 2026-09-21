import { useCallback, useEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Badge } from "@/components/ui/badge"
import { api, ApiError, browserWsUrl } from "@/lib/api"
import type { AssetMissingDetail } from "@/lib/types"

// 导航栏 + 实时画面（F6-v2：CDP screencast 帧流经 WS 推送，页面静止自动停推）。
// F6-v3 去会话化：固定连接后端隐式人工会话（human-main，自动创建），接管恒可开——
// 鼠标点击/滚轮/键盘按容器缩放坐标回传，服务端注入。页面自身跳转放行
// （等价真人开 Chrome）；地址栏仍走白名单。
export function NavShotPane({ pid }: {
  pid: string
}) {
  const [urlInput, setUrlInput] = useState("")
  const [frame, setFrame] = useState<{ data: string; meta: { w: number; h: number } } | null>(null)
  const [live, setLive] = useState(false)
  const [takeover, setTakeover] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [missing, setMissing] = useState<AssetMissingDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const [navNote, setNavNote] = useState<string | null>(null)
  const lastNav = useRef<string>("") // 登记资产后自动重试用
  const wsRef = useRef<WebSocket | null>(null)
  const imgBoxRef = useRef<HTMLDivElement | null>(null)
  const lastMove = useRef(0)

  // ---- 实时帧流（断线指数退避重连 1s→5s） ----
  useEffect(() => {
    setFrame(null)
    setTakeover(false)
    let ws: WebSocket | null = null
    let alive = true
    let delay = 1000
    let timer: ReturnType<typeof setTimeout>
    const connect = () => {
      if (!alive) return
      ws = new WebSocket(browserWsUrl(pid))
      wsRef.current = ws
      ws.onopen = () => { setLive(true); delay = 1000 }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string)
          if (msg.type === "frame") {
            const m = msg.metadata ?? {}
            setFrame({ data: msg.data as string,
                       meta: { w: (m.deviceWidth as number) || 1280, h: (m.deviceHeight as number) || 720 } })
          } else if (msg.type === "error") {
            setErr(msg.message as string)
            alive = false // 结构性错误（依赖缺失/会话没了）不再重连
          }
        } catch { /* 非 JSON 忽略 */ }
      }
      ws.onclose = () => {
        setLive(false); setTakeover(false); wsRef.current = null
        if (alive) { timer = setTimeout(connect, delay); delay = Math.min(delay * 2, 5000) }
      }
      ws.onerror = () => ws?.close()
    }
    connect()
    return () => {
      alive = false
      clearTimeout(timer)
      ws?.close()
      wsRef.current = null
    }
  }, [pid])

  const sendInput = useCallback((payload: Record<string, unknown>) => {
    wsRef.current?.readyState === WebSocket.OPEN &&
      wsRef.current.send(JSON.stringify({ type: "input", ...payload }))
  }, [])

  const doNavigate = useCallback(async (url: string) => {
    if (!url.trim()) return
    setBusy(true); setErr(null); setMissing(null); setNavNote(null)
    lastNav.current = url
    try {
      const r = await api.browserNavigate(pid, url.trim())
      setNavNote(r.final_url)
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const d = e.data as AssetMissingDetail
        if (d?.asset_missing) setMissing(d)
        else setErr(d?.reason ?? String(e))
      } else setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }, [pid])

  const registerAndRetry = useCallback(async () => {
    if (!missing) return
    try {
      await api.addAsset(pid, "auto", missing.host)
      setMissing(null)
      if (lastNav.current) void doNavigate(lastNav.current)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [missing, pid, doNavigate])

  const doAction = useCallback(async (action: "back") => {
    setBusy(true); setErr(null); setNavNote(null)
    try {
      const r = await api.browserAction(pid, { action })
      setNavNote(r.final_url ?? null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }, [pid])

  // ---- 接管输入：画面坐标 → 帧坐标缩放回传 ----
  const frameCoord = useCallback((e: { clientX: number; clientY: number }) => {
    const box = imgBoxRef.current
    if (!box || !frame) return null
    const rect = box.getBoundingClientRect()
    const sx = frame.meta.w / rect.width
    const sy = frame.meta.h / rect.height
    return { x: (e.clientX - rect.left) * sx, y: (e.clientY - rect.top) * sy }
  }, [frame])

  const onPointer = (kind: "down" | "up" | "click" | "dblclick") =>
    (e: React.MouseEvent) => {
      if (!takeover) return
      const c = frameCoord(e)
      if (c) sendInput({ kind, x: c.x, y: c.y, button: e.button === 2 ? "right" : "left" })
    }
  const onWheel = (e: React.WheelEvent) => {
    if (!takeover) return
    e.preventDefault()
    sendInput({ kind: "wheel", delta_x: e.deltaX, delta_y: e.deltaY })
  }
  const onPointerMove = (e: React.PointerEvent) => {
    if (!takeover) return
    const now = Date.now()
    if (now - lastMove.current < 50) return // 节流防帧流被 move 冲爆
    lastMove.current = now
    const c = frameCoord(e)
    if (c) sendInput({ kind: "move", x: c.x, y: c.y })
  }
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!takeover) return
    if (e.key === "Enter" || e.key.length === 1 || e.key.startsWith("Arrow")
        || ["Backspace", "Delete", "Tab", "Escape"].includes(e.key)) {
      e.preventDefault()
      sendInput({ kind: "key", key: e.key === "Enter" ? "Enter" : e.key })
    }
  }
  const onPaste = (e: React.ClipboardEvent) => {
    if (!takeover) return
    const text = e.clipboardData.getData("text")
    if (text) { e.preventDefault(); sendInput({ kind: "type", text }) }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* URL 栏（白名单硬校验：未登记 422 → 一键登记） */}
      <div className="flex items-center gap-1 border-b p-2">
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void doAction("back")}>←</Button>
        <Input
          className="h-7 flex-1 font-mono text-xs"
          placeholder="http://<项目资产表中的目标>"
          value={urlInput}
          onChange={(e) => setUrlInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void doNavigate(urlInput) }}
        />
        <Button size="sm" disabled={busy || !urlInput.trim()} onClick={() => void doNavigate(urlInput)}>
          {busy ? "…" : "前往"}
        </Button>
        <Button size="sm" variant={takeover ? "default" : "outline"}
                disabled={!live}
                onClick={() => setTakeover(!takeover)}
                title={live ? "接管后可直接在画面里点击/输字/滚动" : "等实时画面连上后可接管"}>
          🖱 {takeover ? "接手中" : "接管"}
        </Button>
      </div>
      {missing && (
        <div className="flex items-center gap-2 border-b bg-(--status-paused)/10 px-2 py-1 text-xs">
          <Badge variant="outline" className="font-mono">{missing.host}</Badge>
          <span className="flex-1 truncate text-muted-foreground">{missing.reason}</span>
          <Button size="sm" variant="outline" onClick={() => void registerAndRetry()}>一键登记资产</Button>
        </div>
      )}
      {err && <div className="border-b px-2 py-1 text-xs text-red-400">{err}</div>}
      {navNote && <div className="border-b px-2 py-1 font-mono text-[10px] text-muted-foreground">{navNote}</div>}
      {/* 实时画面 */}
      <div className="min-h-0 flex-1 overflow-auto bg-black/40 p-2">
        {frame
          ? (
            <div
              ref={imgBoxRef}
              tabIndex={0}
              className={takeover
                ? "relative w-full cursor-crosshair outline-2 outline-(--status-ok)"
                : "relative w-full outline-2 outline-transparent"}
              onPointerDown={onPointer("down")}
              onPointerUp={onPointer("up")}
              onClick={onPointer("click")}
              onDoubleClick={onPointer("dblclick")}
              onPointerMove={onPointerMove}
              onWheel={onWheel}
              onKeyDown={onKeyDown}
              onPaste={onPaste}
            >
              <img
                alt="实时画面"
                draggable={false}
                src={`data:image/jpeg;base64,${frame.data}`}
                className="w-full select-none"
              />
              {takeover && (
                <span className="absolute left-1 top-1 rounded bg-(--status-ok) px-1.5 py-0.5 text-[10px] font-bold text-(--background)">
                  🖱 接管中——点击/键盘/滚动直接生效
                </span>
              )}
            </div>
          )
          : (
            <div className="p-4 text-center text-xs text-muted-foreground">
              {live ? "等待画面帧…（导航后即出）" : "连接隐式浏览会话…"}
            </div>
          )}
        {live && (
          <div className="px-1 pt-1 text-right text-[10px] text-muted-foreground">
            ● 实时（页面静止时自动停推）
          </div>
        )}
      </div>
    </div>
  )
}
