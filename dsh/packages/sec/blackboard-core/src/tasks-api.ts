/**
 * tasks 写读面（M2 记录事实层；调度/租约归 M5）：
 * publish 按 dedup_fp 去重；complete/fail 状态机；update 可改字段/重开。
 */
import { createHash } from 'node:crypto'
import { z } from 'zod'
import { newId, now } from './ids'
import type { Task } from './types'
import { TASK_STATUSES } from './schema'
import { BlackboardError } from './error'
import { parseInput } from './validation'
import type { WriteContext } from './tables'

/** 发布入参。 */
export const TaskPublishInput = z.strictObject({
  projectId: z.string(),
  title: z.string().min(1).max(500),
  note: z.string().max(4000).optional(),
  assignee: z.string().min(1).max(128).nullable().optional(),
  dedupFp: z.string().min(1).max(128).optional(),
  author: z.string().min(1).max(64).optional(),
})

/** 更新入参（status=open 表示重开）。 */
export const TaskUpdateInput = z.strictObject({
  title: z.string().min(1).max(500).optional(),
  note: z.string().max(4000).optional(),
  assignee: z.string().min(1).max(128).nullable().optional(),
  status: z.enum(TASK_STATUSES).optional(),
  expectedRevision: z.number().int().min(1).optional(),
})

/** 终态流转入参。 */
export const TaskFinishInput = z.strictObject({
  note: z.string().max(4000).optional(),
  expectedRevision: z.number().int().min(1).optional(),
})

/** 发布结果。 */
export interface TaskPublishResult {
  task: Task
  created: boolean
}

/** 列表过滤入参。 */
export const TaskListInput = z.strictObject({ status: z.enum(TASK_STATUSES).optional() })

/**
 * 标题指纹：归一化折叠空白后 sha256 截 16（对齐 Python dedup_fp 简化版）。
 * @param title - 任务标题。
 */
function titleFp(title: string): string {
  const norm = title.split(/\s+/).join(' ').trim()
  return createHash('sha256').update(norm, 'utf-8').digest('hex').slice(0, 16)
}

/** 乐观锁校验。 */
function checkRevision(current: number, expected: number | undefined): void {
  if (expected !== undefined && current !== expected) {
    throw new BlackboardError(
      'BB_CONFLICT',
      `乐观锁冲突：当前 revision=${current}，请求基于 ${expected}——请重新读取后再改`,
    )
  }
}

/**
 * 发布任务：open/claimed 同指纹 → 复用既有行；否则建新行。
 */
export async function publishTask(io: WriteContext, raw: unknown): Promise<TaskPublishResult> {
  const input = parseInput(TaskPublishInput, raw)
  if (!io.tables.projects.get(input.projectId)) {
    throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${input.projectId}`)
  }
  const fp = input.dedupFp ?? titleFp(input.title)

  for (const [, t] of io.tables.tasks.entries()) {
    if (t.project_id === input.projectId
        && t.dedup_fp === fp
        && (t.status === 'open' || t.status === 'claimed')) {
      return { task: t, created: false }
    }
  }

  const id = newId('task')
  const ts = now()
  const task: Task = {
    id,
    project_id: input.projectId,
    title: input.title,
    status: 'open',
    assignee: input.assignee ?? null,
    note: input.note ?? '',
    dedup_fp: fp,
    revision: 1,
    created_at: ts,
    updated_at: ts,
  }
  await io.tables.tasks.put(id, task)
  await io.appendEvent(input.projectId, 'task.published', {
    task_id: id,
    title: input.title,
    by: input.author ?? 'human',
  })
  return { task, created: true }
}

/** 取任务（不存在 / 跨项目 BB_NOT_FOUND）。 */
export function getTask(io: WriteContext, projectId: string, id: string): Task {
  const t = io.tables.tasks.get(id)
  if (!t || t.project_id !== projectId) {
    throw new BlackboardError('BB_NOT_FOUND', `任务不存在: ${id}`)
  }
  return t
}

/** 列项目任务（状态过滤）。 */
export function listTasks(io: WriteContext, projectId: string, raw: unknown = {}): Task[] {
  const opts = parseInput(TaskListInput, raw)
  return [...io.tables.tasks.entries()]
    .map(([, v]) => v)
    .filter(t => t.project_id === projectId)
    .filter(t => !opts.status || t.status === opts.status)
    .sort((a, b) => (a.created_at < b.created_at ? -1 : 1))
}

/**
 * 更新任务字段；status=open 作用于 done/failed = 重开，
 * 其余状态值非法（done→failed 等 BB_CONFLICT）。
 */
export async function updateTask(io: WriteContext, taskId: string, raw: unknown): Promise<Task> {
  const input = parseInput(TaskUpdateInput, raw)
  const cur = io.tables.tasks.get(taskId)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `任务不存在: ${taskId}`)
  checkRevision(cur.revision, input.expectedRevision)

  let next: Task = { ...cur }
  let reopened = false
  if (input.status !== undefined) {
    if (input.status === 'open' && (cur.status === 'done' || cur.status === 'failed')) {
      next.status = 'open'
      reopened = true
    } else if (input.status !== cur.status) {
      throw new BlackboardError(
        'BB_CONFLICT',
        `非法任务状态迁移：${cur.status}→${input.status}（仅终态可重开为 open）`,
      )
    }
  }
  if (input.title !== undefined) next.title = input.title
  if (input.note !== undefined) next.note = input.note
  if (input.assignee !== undefined) next.assignee = input.assignee

  const changed = next.title !== cur.title
    || next.note !== cur.note
    || next.assignee !== cur.assignee
    || next.status !== cur.status
  if (changed) {
    next = { ...next, revision: cur.revision + 1, updated_at: now() }
    await io.tables.tasks.put(taskId, next)
    await io.appendEvent(cur.project_id, reopened ? 'task.reopened' : 'task.updated', {
      task_id: taskId,
    })
  }
  return next
}

/**
 * 流转终态的通用内核：open/claimed → 目标态，其余 BB_CONFLICT。
 */
async function finish(
  io: WriteContext,
  taskId: string,
  target: 'done' | 'failed',
  raw: unknown,
): Promise<Task> {
  const input = parseInput(TaskFinishInput, raw)
  const cur = io.tables.tasks.get(taskId)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `任务不存在: ${taskId}`)
  checkRevision(cur.revision, input.expectedRevision)
  if (cur.status !== 'open' && cur.status !== 'claimed') {
    throw new BlackboardError(
      'BB_CONFLICT',
      `非法任务状态迁移：${cur.status}→${target}（仅 open/claimed 可收尾）`,
    )
  }
  const next: Task = {
    ...cur,
    status: target,
    ...(input.note !== undefined ? { note: input.note } : {}),
    revision: cur.revision + 1,
    updated_at: now(),
  }
  await io.tables.tasks.put(taskId, next)
  await io.appendEvent(cur.project_id, target === 'done' ? 'task.done' : 'task.failed', {
    task_id: taskId,
    note: (input.note ?? '').slice(0, 200),
  })
  return next
}

/** 完成任务（open/claimed → done）。 */
export function completeTask(io: WriteContext, taskId: string, raw: unknown = {}): Promise<Task> {
  return finish(io, taskId, 'done', raw)
}

/** 置失败（open/claimed → failed）。 */
export function failTask(io: WriteContext, taskId: string, raw: unknown = {}): Promise<Task> {
  return finish(io, taskId, 'failed', raw)
}
