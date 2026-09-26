/**
 * projects 写读面（对齐 Python store.create/get/update_config/update_track）。
 * 纯函数：写函数由服务面在 serialize work 内调用；读函数直接调。
 */
import { z } from 'zod'
import { newId, now } from './ids'
import type { Project } from './types'
import { BlackboardError } from './error'
import { parseInput } from './validation'
import type { WriteContext } from './tables'

/** 建项入参。 */
export const ProjectCreateInput = z.strictObject({
  name: z.string().min(1).max(200),
  track: z.string().min(1).max(64),
  config: z.record(z.string(), z.unknown()).optional(),
  id: z.string().min(1).optional(),
})
export type ProjectCreateInputValue = z.infer<typeof ProjectCreateInput>

/** config 整体替换入参。 */
export const ProjectConfigUpdateInput = z.record(z.string(), z.unknown())

/** track 更新入参。 */
export const ProjectTrackUpdateInput = z.strictObject({ track: z.string().min(1).max(64) })

/** 建项（id 显式冲突 → BB_CONFLICT）。 */
export async function createProject(io: WriteContext, raw: unknown): Promise<Project> {
  const input = parseInput(ProjectCreateInput, raw)
  const id = input.id ?? newId('proj')
  if (io.tables.projects.get(id)) {
    throw new BlackboardError('BB_CONFLICT', `项目已存在: ${id}`)
  }
  const ts = now()
  const proj: Project = {
    id,
    name: input.name,
    track: input.track,
    status: 'active',
    config: input.config ?? {},
    created_at: ts,
    updated_at: ts,
  }
  await io.tables.projects.put(id, proj)
  return proj
}

/** 取项目（不存在 BB_NOT_FOUND）。 */
export function getProject(io: WriteContext, id: string): Project {
  const proj = io.tables.projects.get(id)
  if (!proj) throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${id}`)
  return proj
}

/** 列全部项目（按创建时间升序）。 */
export function listProjects(io: WriteContext): Project[] {
  return [...io.tables.projects.entries()]
    .map(([, v]) => v)
    .sort((a, b) => (a.created_at < b.created_at ? -1 : 1))
}

/** 整体替换 config。 */
export async function updateProjectConfig(io: WriteContext, id: string, raw: unknown): Promise<Project> {
  const config = parseInput(ProjectConfigUpdateInput, raw)
  const cur = io.tables.projects.get(id)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${id}`)
  const next: Project = { ...cur, config, updated_at: now() }
  await io.tables.projects.put(id, next)
  return next
}

/** 更新场景轨。 */
export async function updateProjectTrack(io: WriteContext, id: string, raw: unknown): Promise<Project> {
  const input = parseInput(ProjectTrackUpdateInput, raw)
  const cur = io.tables.projects.get(id)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${id}`)
  const next: Project = { ...cur, track: input.track, updated_at: now() }
  await io.tables.projects.put(id, next)
  return next
}
