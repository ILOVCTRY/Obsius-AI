import type { BBEvent } from "./types"
import type { StreamItem } from "@/views/live/EventRow"

// 对话流装配（2026-09-25 从 LiveRoom 抽出，webui-trae-shell M2）：
// 事件（已按页签/筛选圈定、升序）→ StreamItem[]——命令对配对、中断双事件
// 去重、思考/回复流式 delta 原位替换、human_note 开轮分组。LiveRoom 与
// ConversationPane 共用同一份逻辑：本文件是纯函数，不碰 React。
//
// 对话轮分组纪律：仅「全部」视图下 note/process/reply 齐备时自然成轮，
// 空轮（被任务轮消化/纯排队）回落平铺审计行，审计零回退。

export function buildStreamItems(visible: BBEvent[]): StreamItem[] {
  const out: StreamItem[] = []
  // call_id → pair 位置 [ti, pi]：ti=-1 = out 顶层，否则 = 所在 turn 下标
  // （命令对可能入轮过程组，顶层下标不再指向 pair 本体）
  const pendingIdx = new Map<string, [number, number]>()
  let legacy: { loc: [number, number]; event: BBEvent } | null = null
  // 中断双事件去重（任务中断时 task.failed 与 session.aborted 成对落库，见
  // loop.py _abort_finish）：按（会话， 任务）配对，渲染层只留「❌ 任务失败」；
  // 纯会话中断（无同任务失败行，如空闲窗被打断）仍显示「⛔ 人工中断」。审计两条都在。
  const failedKeys = new Set<string>()
  // 思考流式（2026-09-19）：已有终稿 llm.thinking 的 stream_id 集合——终稿到达后
  // 该流的 delta 过渡行跳过（终稿行自带全文+耗时；delta 行后端已清剪，此处兜底旧视图）
  const finalStreams = new Set<string>()
  // 回复流式（2026-09-20 对话化）：终稿 agent.chat 的 stream_id 集合，同上先例
  const finalChats = new Set<string>()
  for (const e of visible) {
    if (e.kind === "task.failed") {
      const tid = typeof e.payload.task_id === "string" ? e.payload.task_id : ""
      if (!tid) continue
      const sid = typeof e.session_id === "string" ? e.session_id
        : typeof e.payload.session_id === "string" ? e.payload.session_id : ""
      failedKeys.add(`${sid} ${tid}`)
    } else if (e.kind === "llm.thinking" && typeof e.payload.stream_id === "string") {
      finalStreams.add(e.payload.stream_id)
    } else if (e.kind === "agent.chat" && typeof e.payload.stream_id === "string") {
      finalChats.add(e.payload.stream_id)
    }
  }
  const liveThinking = new Map<string, number>() // stream_id -> out 中 delta 组下标
  const liveOrphanChat = new Map<string, number>() // 无轮上下文回复 delta → out 下标
  // 对话轮分组（2026-09-20）：human_note 开轮（同会话未收口轮先收口），过程事件
  // （thinking/命令对/tool.call/任务轮叙述）入 process，无 step 的 agent.chat 收口
  // reply。非「全部」筛选下 note/process/reply 不齐 → 空轮回落平铺审计行，零回退。
  const liveChatTurn = new Map<string, number>() // 回复 stream_id → turn 下标
  const liveThinkTurn = new Map<string, [number, number]>() // 思考 stream_id → [turn 下标, process 下标]
  const openTurn = new Map<string, number>() // 会话 → 未收口 turn 下标
  const skey = (ev: BBEvent) => typeof ev.session_id === "string" ? ev.session_id : ""
  type Turn = Extract<StreamItem, { type: "turn" }>
  const turnAt = (idx: number) => out[idx] as Turn
  for (const e of visible) {
    if (e.kind === "command") {
      const cid = typeof e.payload.call_id === "string" ? e.payload.call_id : ""
      let loc: [number, number]
      if (openTurn.has(skey(e))) {
        const ti = openTurn.get(skey(e))!
        turnAt(ti).process.push({ type: "pair", command: e })
        loc = [ti, turnAt(ti).process.length - 1]
      } else {
        out.push({ type: "pair", command: e })
        loc = [-1, out.length - 1]
      }
      if (cid) pendingIdx.set(cid, loc)
      else legacy = { loc, event: e }
    } else if (e.kind === "command.result") {
      const cid = typeof e.payload.call_id === "string" ? e.payload.call_id : ""
      const loc = cid ? pendingIdx.get(cid)
        : legacy && legacy.event.session_id === e.session_id ? legacy.loc : undefined
      if (loc !== undefined) {
        const [ti, pi] = loc
        const cmd = (ti === -1
          ? (out[pi] as Extract<StreamItem, { type: "pair" }>).command
          : (turnAt(ti).process[pi] as Extract<StreamItem, { type: "pair" }>).command)
        const pair: StreamItem = { type: "pair", command: cmd, result: e }
        if (ti === -1) out[pi] = pair
        else turnAt(ti).process[pi] = pair
        if (cid) pendingIdx.delete(cid)
        else legacy = null
      } else {
        out.push({ type: "single", event: e }) // 孤儿 result：单独渲染
      }
    } else if (e.kind === "session.aborted" && e.payload.note === "人工中断") {
      const tid = typeof e.payload.task_id === "string" ? e.payload.task_id : ""
      const sid = typeof e.session_id === "string" ? e.session_id
        : typeof e.payload.session_id === "string" ? e.payload.session_id : ""
      if (!tid || !failedKeys.has(`${sid} ${tid}`)) out.push({ type: "single", event: e })
    } else if (e.kind === "message.inbox" && e.payload.kind === "human_note") {
      const prev = openTurn.get(skey(e))
      if (prev !== undefined) {
        const t = turnAt(prev)
        if (!t.reply && !t.replyStream && t.process.length === 0) {
          out[prev] = { type: "single", event: t.note } // 空轮回落平铺审计行
        }
        openTurn.delete(skey(e))
      }
      out.push({ type: "turn", note: e, process: [] })
      openTurn.set(skey(e), out.length - 1)
    } else if (e.kind === "llm.thinking.delta") {
      // 思考流式增量（2026-09-19）：按 stream_id 组装成一行滚动「思考中…」；
      // 轮上下文在场时入过程组原位替换（liveThinkTurn 记 [轮下标, process 下标]）
      const sid = typeof e.payload.stream_id === "string" ? e.payload.stream_id : ""
      if (!sid || finalStreams.has(sid)) continue
      const inTurn = liveThinkTurn.get(sid)
      if (inTurn && openTurn.get(skey(e)) === inTurn[0]) {
        const t = turnAt(inTurn[0])
        const prev = t.process[inTurn[1]] as Extract<StreamItem, { type: "single" }>
        t.process[inTurn[1]] = { type: "single", event: { ...e, id: prev.event.id } }
      } else if (openTurn.has(skey(e))) {
        const ti = openTurn.get(skey(e))!
        turnAt(ti).process.push({ type: "single", event: e })
        liveThinkTurn.set(sid, [ti, turnAt(ti).process.length - 1])
      } else {
        const idx = liveThinking.get(sid)
        if (idx !== undefined) {
          const prev = out[idx] as Extract<StreamItem, { type: "single" }>
          out[idx] = { type: "single", event: { ...e, id: prev.event.id } }
        } else {
          out.push({ type: "single", event: e })
          liveThinking.set(sid, out.length - 1)
        }
      }
    } else if (e.kind === "agent.chat.delta") {
      // 回复流式增量（2026-09-20）：轮内 replyStream 原位替换（累计全文自愈）；
      // 无轮上下文的孤儿 delta 单行流式渲染（escalation-only 回复等）
      const sid = typeof e.payload.stream_id === "string" ? e.payload.stream_id : ""
      if (!sid || finalChats.has(sid)) continue
      const ti = liveChatTurn.get(sid)
      if (ti !== undefined) {
        turnAt(ti).replyStream = e
      } else if (openTurn.has(skey(e))) {
        const t = openTurn.get(skey(e))!
        turnAt(t).replyStream = e
        liveChatTurn.set(sid, t)
      } else {
        const idx = liveOrphanChat.get(sid)
        if (idx !== undefined) {
          const prev = out[idx] as Extract<StreamItem, { type: "single" }>
          out[idx] = { type: "single", event: { ...e, id: prev.event.id } }
        } else {
          out.push({ type: "single", event: e })
          liveOrphanChat.set(sid, out.length - 1)
        }
      }
    } else if (e.kind === "agent.chat") {
      if (e.payload.step === undefined) {
        // 对话轮回复（无 step）：收口轮；已按 stream_id 关联的先清 replyStream
        const sid = typeof e.payload.stream_id === "string" ? e.payload.stream_id : ""
        const ti = sid ? liveChatTurn.get(sid) : undefined
        if (ti !== undefined) {
          const t = turnAt(ti)
          t.reply = e
          t.replyStream = undefined
          openTurn.delete(skey(e))
        } else if (openTurn.has(skey(e))) {
          const t = openTurn.get(skey(e))!
          turnAt(t).reply = e
          openTurn.delete(skey(e))
        } else {
          out.push({ type: "single", event: e })
        }
      } else if (openTurn.has(skey(e))) {
        // 任务轮叙述行（带 step）：轮在场入过程组，否则平铺（任务页签现状不变）
        turnAt(openTurn.get(skey(e))!).process.push({ type: "single", event: e })
      } else {
        out.push({ type: "single", event: e })
      }
    } else if ((e.kind === "llm.thinking" || e.kind === "tool.call")
               && openTurn.has(skey(e))) {
      turnAt(openTurn.get(skey(e))!).process.push({ type: "single", event: e })
    } else {
      out.push({ type: "single", event: e })
    }
  }
  // 流扫尾：仍未收口且空过程的轮 → 平铺（引导被任务轮消化/纯排队场景，审计不缺行）
  for (const idx of openTurn.values()) {
    const t = turnAt(idx)
    if (!t.reply && !t.replyStream && t.process.length === 0) {
      out[idx] = { type: "single", event: t.note }
    }
  }
  return out
}
