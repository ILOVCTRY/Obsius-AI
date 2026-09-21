import { useCallback, useEffect, useRef, useState } from "react"
import { api, wsUrl } from "./api"
import type { BBEvent } from "./types"

// 直播间事件流（2026-09-17 改版，DESIGN.md §12 直播间实现要求）：
// - 首屏只 REST 拉最新 PAGE 条（tail），不再 WS 从 0 全量回放历史；
// - 增量走 WS（连接时带缓存游标，只推新事件），~100ms 批量 flush（2026-09-20：
//   思考流式 delta 频率高，逐条 setState 会全列表重渲，合并后一帧一渲染）；
// - 更早历史按 PAGE 条/批上翻懒加载（REST before_id 翻页），loadEarlier 供滚动触发；
// - 已加载事件缓存在模块级 Map（SPA 会话内常驻），切走再回直播间/多实例
//   （App.tsx / LiveRoom / ReverseWorkbench 各有一个 hook 实例）共享同一份数据，
//   重进不重拉；写缓存用按 id 归并去重，多实例并发写不冲突。
// - 回入水合截断 + 缓存总量上限（2026-09-20，会话页加载延迟修复）：长跑项目
//   缓存会积累数千条，全量水合=回入一次性 mount 全部行——重入只水合尾部
//   HYDRATE_LIMIT 条，更早的留缓存（loadEarlier 先吃缓存再打网络）；缓存超过
//   CACHE_LIMIT 裁最旧并把 loadedAll 降级 false（内存不再覆盖全部历史，头部
//   交回网络分页重取，语义保持正确）。

const PAGE = 50
const HYDRATE_LIMIT = 300
const CACHE_LIMIT = 2000

const cache = new Map<string, { events: BBEvent[]; loadedAll: boolean }>()

// 两个升序数组按 id 归并去重（append 与 prepend 共用）
function mergeSorted(a: BBEvent[], b: BBEvent[]): BBEvent[] {
  const out: BBEvent[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i].id < b[j].id) out.push(a[i++])
    else if (a[i].id > b[j].id) out.push(b[j++])
    else { out.push(a[i++]); j++ }
  }
  while (i < a.length) out.push(a[i++])
  while (j < b.length) out.push(b[j++])
  return out
}

function writeCache(pid: string, incoming: BBEvent[], loadedAll?: boolean): BBEvent[] {
  const cur = cache.get(pid) ?? { events: [], loadedAll: true }
  let events = mergeSorted(cur.events, incoming)
  let all = loadedAll ?? cur.loadedAll
  if (events.length > CACHE_LIMIT) {
    events = events.slice(-CACHE_LIMIT)
    all = false // 头部被裁：内存不再覆盖全部历史，上翻交回网络分页重取
  }
  cache.set(pid, { events, loadedAll: all })
  return events
}

export function useEvents(pid: string | null) {
  const [events, setEvents] = useState<BBEvent[]>([])
  const [connected, setConnected] = useState(false)
  // 更早历史是否已取尽（取尽后上翻不再请求）；loadingEarlier=翻页请求在途
  const [loadedAll, setLoadedAll] = useState(true)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  // 断线重连要用的游标放 ref，避免闭包拿到旧值
  const cursorRef = useRef(0)
  const closedRef = useRef(false)
  const loadingRef = useRef(false)
  // loadEarlier 要读当前展示窗口的头 id；用 ref 镜像 state（effect 同步，读侧不触发重渲）
  const eventsRef = useRef<BBEvent[]>([])
  useEffect(() => { eventsRef.current = events }, [events])

  useEffect(() => {
    if (!pid) return
    closedRef.current = false
    const cached = cache.get(pid)
    let needTail = true
    if (cached && cached.events.length) {
      // 常驻缓存命中：只水合尾部 HYDRATE_LIMIT 条（长跑项目缓存数千条，全量水合=
      // 回入一次性 mount 全部行——这是会话页打开卡顿的主因）；更早的留缓存，
      // loadEarlier 先吃缓存再打网络
      const win = cached.events.slice(-HYDRATE_LIMIT)
      setEvents(win)
      setLoadedAll(cached.loadedAll && cached.events.length <= HYDRATE_LIMIT)
      cursorRef.current = cached.events[cached.events.length - 1].id
      needTail = false
    } else {
      setEvents([])
      setLoadedAll(true)
      cursorRef.current = 0
    }

    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let flushTimer: ReturnType<typeof setTimeout> | null = null
    // WS 增量批量 flush：思考流式 delta 频率高，逐条 setState=每条一次全列表重算
    // 重渲；攒 ~100ms 合并成一帧一渲染。写缓存即时（多实例共享不丢），setState 延迟。
    let buf: BBEvent[] = []
    const flush = () => {
      flushTimer = null
      if (!buf.length) return
      const batch = buf
      buf = []
      writeCache(pid, batch)
      setEvents((prev) => {
        const last = prev.length ? prev[prev.length - 1].id : 0
        const fresh = batch.filter((e) => e.id > last)
        return fresh.length ? [...prev, ...fresh] : prev
      })
    }

    const connect = () => {
      if (closedRef.current) return
      ws = new WebSocket(wsUrl(pid, cursorRef.current))
      ws.onopen = () => setConnected(true)
      ws.onmessage = (m) => {
        const e = JSON.parse(m.data as string) as BBEvent
        if (e.id <= cursorRef.current) return
        cursorRef.current = e.id
        buf.push(e)
        if (flushTimer == null) flushTimer = setTimeout(flush, 100)
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

    if (needTail) {
      // 首次：先取最新一页再连 WS（WS 带尾页游标，只推增量），失败也照连（降级为全量回放）
      api.eventsTail(pid, PAGE)
        .then((tail) => {
          if (closedRef.current) return
          const all = tail.length < PAGE
          setLoadedAll(all)
          cursorRef.current = tail.length ? tail[tail.length - 1].id : 0
          setEvents(writeCache(pid, tail, all).slice(-HYDRATE_LIMIT))
        })
        .catch(() => {})
        .finally(() => { if (!closedRef.current) connect() })
    } else {
      connect()
    }

    return () => {
      closedRef.current = true
      if (retryTimer) clearTimeout(retryTimer)
      if (flushTimer != null) clearTimeout(flushTimer)
      if (buf.length) writeCache(pid, buf) // 未 flush 的增量落缓存（setState 已无意义）
      buf = []
      ws?.close()
    }
  }, [pid])

  // 上翻加载更早一页：返回是否有更多（滚动触发与「加载更早」按钮共用）。
  // 先吃缓存（水合截断留在 cache 里的更早事件，零网络），缓存不够再打 before_id 翻页。
  const loadEarlier = useCallback(async (): Promise<boolean> => {
    if (!pid || loadingRef.current) return false
    const cur = cache.get(pid)
    const headId = eventsRef.current.length ? eventsRef.current[0].id : 0
    if (!headId || !cur || !cur.events.length) return false
    loadingRef.current = true
    setLoadingEarlier(true)
    try {
      // cache ⊇ 展示窗口且连续，头部之前的缓存事件必然紧挨窗口头
      const cachedOlder = cur.events.filter((e) => e.id < headId)
      if (cachedOlder.length >= PAGE) {
        const win = cachedOlder.slice(-PAGE)
        setEvents((prev) => [...win, ...prev])
        return true
      }
      let older = cachedOlder
      let all = cur.loadedAll
      if (!cur.loadedAll) {
        // 缓存被裁过（CACHE_LIMIT）时 cache 头可能比展示窗口头还新——起点取两者
        // 较小值，保证取回的全是未展示区间
        const fetchFrom = Math.min(cur.events[0].id, headId)
        const fetched = await api.eventsBefore(pid, fetchFrom, PAGE)
        writeCache(pid, fetched)
        all = fetched.length < PAGE
        older = [...fetched.filter((e) => e.id < headId), ...cachedOlder].slice(-PAGE)
      }
      if (!older.length) {
        setLoadedAll(true)
        return false
      }
      setEvents((prev) => [...older, ...prev])
      setLoadedAll(all)
      return !all
    } catch {
      return false
    } finally {
      loadingRef.current = false
      setLoadingEarlier(false)
    }
  }, [pid])

  return { events, connected, loadedAll, loadingEarlier, loadEarlier }
}
