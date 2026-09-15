import { useEffect, useRef, useState } from "react"
import { wsUrl } from "./api"
import type { BBEvent } from "./types"

// 直播间事件流：WS 连接 + 断线重连后按 since_id 增量回放（DESIGN.md §12 直播间实现要求）

export function useEvents(pid: string | null) {
  const [events, setEvents] = useState<BBEvent[]>([])
  const [connected, setConnected] = useState(false)
  // 断线重连要用的游标放 ref，避免闭包拿到旧值
  const cursorRef = useRef(0)
  const closedRef = useRef(false)

  useEffect(() => {
    if (!pid) return
    setEvents([])
    cursorRef.current = 0
    closedRef.current = false

    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null

    const connect = () => {
      if (closedRef.current) return
      ws = new WebSocket(wsUrl(pid, cursorRef.current))
      ws.onopen = () => setConnected(true)
      ws.onmessage = (m) => {
        const e = JSON.parse(m.data as string) as BBEvent
        cursorRef.current = Math.max(cursorRef.current, e.id)
        setEvents((prev) => {
          // 回放/增量都按 id 去重（重连瞬间可能重叠）
          if (prev.length && e.id <= prev[prev.length - 1].id) return prev
          return [...prev, e]
        })
      }
      ws.onclose = (ev) => {
        setConnected(false)
        // 1008 = 服务端主动拒绝（项目删除中/已删除），停止重连，避免对已删项目无限刷错
        if (!closedRef.current && ev.code !== 1008) {
          retryTimer = setTimeout(connect, 2000)
        }
      }
      ws.onerror = () => ws?.close()
    }
    connect()

    return () => {
      closedRef.current = true
      if (retryTimer) clearTimeout(retryTimer)
      ws?.close()
    }
  }, [pid])

  return { events, connected }
}
