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
// - 会话维度分页（2026-09-23 直播间会话窗口）：sessionId 非空=会话源——tail/
//   before 按 session_id 过滤（store 层 F8 既支持），cache 键 `${pid}:${sid}`
//   每会话独立窗口；WS 增量按 sid 过滤（不属于本会话的事件只推游标不进窗口，
//   断线重连回放不重复）。sessionId=null 走全局流（__all/编排页签/其他调用点）。

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

function writeCache(key: string, incoming: BBEvent[], loadedAll?: boolean): BBEvent[] {
  const cur = cache.get(key) ?? { events: [], loadedAll: true }
  let events = mergeSorted(cur.events, incoming)
  let all = loadedAll ?? cur.loadedAll
  if (events.length > CACHE_LIMIT) {
    events = events.slice(-CACHE_LIMIT)
    all = false // 头部被裁：内存不再覆盖全部历史，上翻交回网络分页重取
  }
  cache.set(key, { events, loadedAll: all })
  return events
}

export function useEvents(pid: string | null, sessionId?: string | null) {
  const cacheKey = sessionId ? `${pid}:${sessionId}` : `${pid ?? ""}`
  const [events, setEvents] = useState<BBEvent[]>([])
  const [connected, setConnected] = useState(false)
  // 更早历史是否已取尽（取尽后上翻不再请求）；loadingEarlier=翻页请求在途
  const [loadedAll, setLoadedAll] = useState(true)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  // 断线重连要用的游标放 ref，避免闭包拿到旧值
  const cursorRef = useRef(0)
  const loadingRef = useRef(false)
  // loadEarlier 要读当前展示窗口的头 id；用 ref 镜像 state（effect 同步，读侧不触发重渲）
  const eventsRef = useRef<BBEvent[]>([])
  useEffect(() => { eventsRef.current = events }, [events])

  useEffect(() => {
    if (!pid) return
    // effect 局部存活标记（2026-09-24）：本 effect 内所有异步路径（tail 响应/
    // WS onmessage·onclose/flush/重连）一律只认它。**不能用共享 ref 守卫**——
    // cleanup 置位后新 effect 同步重置，旧页签迟到的 tail 响应、旧 WS 异步
    // onclose（握手在重置后才完成）会穿透守卫：tail 覆盖新页签内容，onclose
    // 还会用旧闭包调度 connect 产生自我续命的孤儿 WebSocket，把旧会话事件混进
    // 新页签（切页签偶发不刷新的根因）。
    let alive = true
    const cached = cache.get(cacheKey)
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
      if (!alive || !buf.length) return
      const batch = buf
      buf = []
      writeCache(cacheKey, batch)
      setEvents((prev) => {
        const last = prev.length ? prev[prev.length - 1].id : 0
        const fresh = batch.filter((e) => e.id > last)
        return fresh.length ? [...prev, ...fresh] : prev
      })
    }

    const connect = () => {
      if (!alive) return
      ws = new WebSocket(wsUrl(pid, cursorRef.current))
      ws.onopen = () => { if (alive) setConnected(true) }
      ws.onmessage = (m) => {
        if (!alive) return
        const e = JSON.parse(m.data as string) as BBEvent
        if (e.id <= cursorRef.current) return
        cursorRef.current = e.id
        // 会话源：不属于本会话的事件只推游标不进窗口（游标恒前进，
        // 断线重连回放不重复；切回全局源时这些事件经缓存/翻页自然可见）
        if (sessionId && e.session_id !== sessionId) return
        buf.push(e)
        if (flushTimer == null) flushTimer = setTimeout(flush, 100)
      }
      ws.onclose = (ev) => {
        if (!alive) return // 旧 effect 的异步 onclose：不 setConnected、不重连（防孤儿 WS）
        setConnected(false)
        // 1008 = 服务端主动拒绝（项目删除中/已删除），停止重连，避免对已删项目无限刷错
        if (ev.code !== 1008) {
          retryTimer = setTimeout(connect, 2000)
        }
      }
      ws.onerror = () => { if (alive) ws?.close() }
    }

    if (needTail) {
      // 首次：先取最新一页再连 WS（WS 带尾页游标，只推增量），失败也照连（降级为全量回放）
      api.eventsTail(pid, PAGE, sessionId ?? undefined)
        .then((tail) => {
          if (!alive) return
          const all = tail.length < PAGE
          setLoadedAll(all)
          cursorRef.current = tail.length ? tail[tail.length - 1].id : 0
          setEvents(writeCache(cacheKey, tail, all).slice(-HYDRATE_LIMIT))
        })
        .catch(() => {})
        .finally(() => { if (alive) connect() })
    } else {
      connect()
    }

    return () => {
      alive = false
      if (retryTimer) clearTimeout(retryTimer)
      if (flushTimer != null) clearTimeout(flushTimer)
      if (buf.length) writeCache(cacheKey, buf) // 未 flush 的增量落缓存（setState 已无意义）
      buf = []
      ws?.close()
    }
  }, [pid, sessionId, cacheKey])

  // 上翻加载更早一页：返回是否有更多（滚动触发与「加载更早」按钮共用）。
  // 先吃缓存（水合截断留在 cache 里的更早事件，零网络），缓存不够再打 before_id 翻页。
  const loadEarlier = useCallback(async (): Promise<boolean> => {
    if (!pid || loadingRef.current) return false
    const cur = cache.get(cacheKey)
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
        const fetched = await api.eventsBefore(pid, fetchFrom, PAGE, sessionId ?? undefined)
        writeCache(cacheKey, fetched)
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
  }, [pid, sessionId, cacheKey])

  // 展示窗口裁剪（滑动窗口上界，2026-09-23 会话窗口）：只裁组件 state，模块
  // cache 不动——cache 始终是展示窗口超集，loadEarlier 可从缓存补回被裁的头部。
  const trimDom = useCallback((max: number, keep: number) => {
    setEvents((prev) => (prev.length > max ? prev.slice(prev.length - max + keep) : prev))
  }, [])

  return { events, connected, loadedAll, loadingEarlier, loadEarlier, trimDom }
}
