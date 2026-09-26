/**
 * events 查询纯函数（对齐 Python recent_events / latest_event_id）：
 * 单调零填充键，字典序即时间序；since 排他 / before / tail / limit / kinds / sessionId。
 */
import { z } from 'zod'
import type { SecEvent } from './types'
import { parseInput } from './validation'

/** 查询入参。 */
export const EventQueryInput = z.strictObject({
  /** 排他下界（id > since）。 */
  since: z.string().min(1).optional(),
  /** 上翻页（id < before；优先于 since）。 */
  before: z.string().min(1).optional(),
  /** 只取最新 tail 条（优先于 since；结果仍升序）。 */
  tail: z.number().int().min(1).max(500).optional(),
  /** 窗口上限。 */
  limit: z.number().int().min(1).max(500).optional(),
  /** 类型白名单。 */
  kinds: z.array(z.string().min(1)).max(50).optional(),
  /** 会话过滤（null=只查无会话事件）。 */
  sessionId: z.string().min(1).nullable().optional(),
})

/** 公开追加入参。 */
export const EventAppendInput = z.strictObject({
  projectId: z.string(),
  kind: z.string().min(1).max(100),
  payload: z.record(z.string(), z.unknown()).optional(),
  sessionId: z.string().min(1).nullable().optional(),
})

/**
 * 查询事件。
 * @param projectEvents - 已按项目过滤的事件（任意顺序）。
 * @param raw - 查询入参。
 * @returns 按 id 升序的事件窗口。
 */
export function queryEvents(projectEvents: SecEvent[], raw: unknown): SecEvent[] {
  const q = parseInput(EventQueryInput, raw)
  const kindSet = q.kinds ? new Set(q.kinds) : null

  let rows = projectEvents
    .slice()
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))

  // before / since 互斥优先级（before 先）。
  if (q.before) {
    rows = rows.filter(e => e.id < q.before!)
  } else if (q.since) {
    rows = rows.filter(e => e.id > q.since!)
  }
  if (kindSet) rows = rows.filter(e => kindSet.has(e.kind))
  if (q.sessionId !== undefined) {
    const sid = q.sessionId ?? null
    rows = rows.filter(e => e.session_id === sid)
  }

  // tail 优先于 limit。
  if (q.tail) {
    rows = rows.slice(-q.tail)
  } else if (q.limit) {
    rows = rows.slice(0, q.limit)
  }
  return rows
}

/**
   * 项目最新事件 id（游标；无事件返回空串）。
   * @param projectEvents - 项目事件。
   */
export function latestEventCursor(projectEvents: SecEvent[]): string {
  let cur = ''
  for (const e of projectEvents) {
    if (e.id > cur) cur = e.id
  }
  return cur
}
