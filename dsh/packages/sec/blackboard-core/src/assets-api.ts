/**
 * assets 写读面（对齐 Python store 资产八法 + assets.register_asset 简化版）。
 * M2.2 不做 DNS/CDN 自动派生——register 返回 derived=[]，不猜 DNS 不造 host 行。
 * 纯函数：写函数由服务面在 serialize work 内调用。
 */
import { z } from 'zod'
import { newId, now } from './ids'
import type { Asset, Finding, Project } from './types'
import { ASSET_STATUSES } from './schema'
import { BlackboardError } from './error'
import { parseInput } from './validation'
import { detectType } from './detect-type'
import { coverageReport, type CoverageReport } from './coverage'
import type { WriteContext } from './tables'

/** 资产注册入参。 */
export const AssetRegisterInput = z.strictObject({
  projectId: z.string(),
  value: z.string().min(1).max(2048),
  type: z.string().min(1).max(32).optional(),
  parentId: z.string().nullable().optional(),
  meta: z.record(z.string(), z.unknown()).optional(),
  author: z.string().min(1).max(64).optional(),
  sessionId: z.string().nullable().optional(),
})
export type AssetRegisterInputValue = z.infer<typeof AssetRegisterInput>

/** 改挂入参。 */
export const AssetParentInput = z.strictObject({
  parentId: z.string().nullable(),
  expectedRevision: z.number().int().min(1).optional(),
})

/** meta 合并入参。 */
export const AssetMetaInput = z.strictObject({
  patch: z.record(z.string(), z.unknown()),
  expectedRevision: z.number().int().min(1).optional(),
})

/** 状态流转入参。 */
export const AssetStatusInput = z.strictObject({
  status: z.enum(ASSET_STATUSES),
  note: z.string().max(2000).optional(),
  expectedRevision: z.number().int().min(1).optional(),
})

/** 删除入参。 */
export const AssetDeleteInput = z.strictObject({ author: z.string().min(1).max(64).optional() })

/** 列表过滤入参。 */
export const AssetListInput = z.strictObject({
  type: z.string().min(1).optional(),
  status: z.string().min(1).optional(),
  tag: z.string().min(1).optional(),
})

/** 注册结果。 */
export interface AssetRegisterResult {
  asset: Asset
  created: boolean
  /** 自动派生资产（DNS/CDN）——M2.2 恒空。 */
  derived: string[]
}

/** 资产森林。 */
export interface AssetForest {
  assets: Asset[]
  roots: string[]
  children: Record<string, string[]>
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
 * 注册资产：trim → 自动识别类型 → (project,type,value) 去重合并 → 父校验 → 落行。
 */
export async function registerAsset(io: WriteContext, raw: unknown): Promise<AssetRegisterResult> {
  const input = parseInput(AssetRegisterInput, raw)
  const project = io.tables.projects.get(input.projectId)
  if (!project) {
    throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${input.projectId}`)
  }
  const value = input.value.trim()
  const type = input.type && input.type !== 'auto' ? input.type : detectType(value)
  if (!type) {
    throw new BlackboardError(
      'BB_VALIDATION',
      `资产类型识别不出（值：${value.slice(0, 80)}），请显式指定 type`,
    )
  }

  const existing = findAssetValue(io, input.projectId, type, value)
  if (existing) {
    let merged = existing
    if (input.meta) {
      const candidate: Asset = { ...existing, meta: { ...existing.meta, ...input.meta } }
      if (JSON.stringify(candidate.meta) !== JSON.stringify(existing.meta)) {
        merged = { ...candidate, revision: existing.revision + 1, updated_at: now() }
        await io.tables.assets.put(merged.id, merged)
      }
    }
    return { asset: merged, created: false, derived: [] }
  }

  const parentId: string | null = input.parentId ?? null
  if (parentId) {
    const parent = io.tables.assets.get(parentId)
    if (!parent || parent.project_id !== input.projectId) {
      throw new BlackboardError('BB_VALIDATION', `父资产不存在或跨项目: ${parentId}`)
    }
  }

  const id = newId('asset')
  const ts = now()
  const asset: Asset = {
    id,
    project_id: input.projectId,
    type,
    value,
    parent_id: parentId,
    status: 'open',
    meta: input.meta ?? {},
    revision: 1,
    created_at: ts,
    updated_at: ts,
  }
  await io.tables.assets.put(id, asset)
  await io.appendEvent(
    input.projectId,
    'asset.new',
    { asset_id: id, type, value, parent_id: parentId, by: input.author ?? 'system' },
    input.sessionId ?? null,
  )
  return { asset, created: true, derived: [] }
}

/** 取资产（不存在 BB_NOT_FOUND）。 */
export function getAsset(io: WriteContext, id: string): Asset {
  const asset = io.tables.assets.get(id)
  if (!asset) throw new BlackboardError('BB_NOT_FOUND', `资产不存在: ${id}`)
  return asset
}

/** 按 (project,type,value) 查（不看 parent；缺失返回 undefined）。 */
export function findAssetValue(
  io: WriteContext,
  projectId: string,
  type: string,
  value: string,
): Asset | undefined {
  for (const [, asset] of io.tables.assets.entries()) {
    if (asset.project_id === projectId && asset.type === type && asset.value === value) {
      return asset
    }
  }
  return undefined
}

/** 按 (project,type,value) 查（不存在 BB_NOT_FOUND）。 */
export function findAsset(io: WriteContext, projectId: string, type: string, value: string): Asset {
  const asset = findAssetValue(io, projectId, type, value)
  if (!asset) {
    throw new BlackboardError('BB_NOT_FOUND', `资产不存在: ${type}:${value.slice(0, 60)}`)
  }
  return asset
}

/** 列项目资产（类型/状态/标签过滤）。 */
export function listAssets(io: WriteContext, projectId: string, raw: unknown = {}): Asset[] {
  const opts = parseInput(AssetListInput, raw)
  const tag = opts.tag?.trim().toLowerCase()
  return [...io.tables.assets.entries()]
    .map(([, v]) => v)
    .filter(a => a.project_id === projectId)
    .filter(a => !opts.type || a.type === opts.type)
    .filter(a => !opts.status || a.status === opts.status)
    .filter(a => {
      if (!tag) return true
      const tags = (a.meta.tags as unknown[] | undefined)
        ?.map(t => String(t).trim().toLowerCase())
        .filter(Boolean) ?? []
      return tags.includes(tag)
    })
    .sort((a, b) => (a.created_at < b.created_at ? -1 : 1))
}

/** 改挂父资产（None=摘挂为根；防环；乐观锁）。 */
export async function setAssetParent(io: WriteContext, assetId: string, raw: unknown): Promise<Asset> {
  const input = parseInput(AssetParentInput, raw)
  const cur = io.tables.assets.get(assetId)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `资产不存在: ${assetId}`)
  checkRevision(cur.revision, input.expectedRevision)
  const parentId = input.parentId
  if (cur.parent_id === parentId) return cur
  if (parentId === assetId) {
    throw new BlackboardError('BB_CONFLICT', '资产不能挂自己')
  }
  if (parentId !== null) {
    const parent = io.tables.assets.get(parentId)
    if (!parent || parent.project_id !== cur.project_id) {
      throw new BlackboardError('BB_VALIDATION', `父资产不存在或跨项目: ${parentId}`)
    }
    let walker: string | null = parentId
    while (walker) {
      if (walker === assetId) {
        throw new BlackboardError('BB_CONFLICT', '挂载会形成环（parent 链经过自身）')
      }
      walker = io.tables.assets.get(walker)?.parent_id ?? null
    }
  }
  const oldParent = cur.parent_id
  const next: Asset = {
    ...cur,
    parent_id: parentId,
    revision: cur.revision + 1,
    updated_at: now(),
  }
  await io.tables.assets.put(assetId, next)
  await io.appendEvent(cur.project_id, 'asset.reparent', {
    asset_id: assetId,
    old_parent_id: oldParent,
    parent_id: parentId,
  })
  return next
}

/** 合并更新 meta（读-合并-写；乐观锁）。 */
export async function updateAssetMeta(io: WriteContext, assetId: string, raw: unknown): Promise<Asset> {
  const input = parseInput(AssetMetaInput, raw)
  const cur = io.tables.assets.get(assetId)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `资产不存在: ${assetId}`)
  checkRevision(cur.revision, input.expectedRevision)
  const merged = { ...cur.meta, ...input.patch }
  const next: Asset = {
    ...cur,
    meta: merged,
    revision: cur.revision + 1,
    updated_at: now(),
  }
  await io.tables.assets.put(assetId, next)
  return next
}

/** 状态机流转（六态；终态三态必带 note；父节点 tested_clean 拒绝；同态 no-op）。 */
export async function setAssetStatus(io: WriteContext, assetId: string, raw: unknown): Promise<Asset> {
  const input = parseInput(AssetStatusInput, raw)
  const cur = io.tables.assets.get(assetId)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `资产不存在: ${assetId}`)
  checkRevision(cur.revision, input.expectedRevision)
  const status = input.status
  if (cur.status === status) return cur
  if (['tested_clean', 'budget_stop', 'na'].includes(status)
      && !(input.note && input.note.trim())) {
    throw new BlackboardError(
      'BB_VALIDATION',
      `${status} 必须附 note（测了什么/为什么停/为什么不适用）`,
    )
  }
  if (status === 'tested_clean') {
    for (const [, a] of io.tables.assets.entries()) {
      if (a.parent_id === assetId) {
        throw new BlackboardError(
          'BB_CONFLICT',
          '该资产存在子资产：父节点 tested_clean 由子树全部终态自动派生，请先流转子节点',
        )
      }
    }
  }
  const next: Asset = {
    ...cur,
    status,
    ...(input.note && input.note.trim() ? { meta: { ...cur.meta, note: input.note } } : {}),
    revision: cur.revision + 1,
    updated_at: now(),
  }
  await io.tables.assets.put(assetId, next)
  await io.appendEvent(cur.project_id, 'asset.status_changed', {
    asset_id: assetId,
    old: cur.status,
    new: status,
    note: (input.note ?? '').slice(0, 200),
  })
  return next
}

/** 物理删除叶子资产（finding 引用/子资产双重防护；落审计事件）。 */
export async function deleteAsset(
  io: WriteContext,
  assetId: string,
  raw: unknown = {},
): Promise<{ id: string }> {
  const input = parseInput(AssetDeleteInput, raw)
  const cur = io.tables.assets.get(assetId)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `资产不存在: ${assetId}`)
  for (const [, f] of io.tables.findings.entries()) {
    if (f.target_asset_id === assetId) {
      throw new BlackboardError(
        'BB_CONFLICT',
        `资产 ${cur.value} 仍被发现 ${f.id} 引用，请先删除或改挂该发现`,
      )
    }
  }
  for (const [, a] of io.tables.assets.entries()) {
    if (a.parent_id === assetId) {
      throw new BlackboardError(
        'BB_CONFLICT',
        `资产 ${cur.value} 存在子资产，请先处理子资产再删除`,
      )
    }
  }
  await io.tables.assets.delete(assetId)
  await io.appendEvent(cur.project_id, 'asset.deleted', {
    asset_id: assetId,
    by: input.author ?? 'human',
    type: cur.type,
    value: cur.value,
    parent_id: cur.parent_id,
  })
  return { id: assetId }
}

/** 项目资产父子森林。 */
export function assetForest(io: WriteContext, projectId: string): AssetForest {
  const assets = listAssets(io, projectId)
  const children: Record<string, string[]> = {}
  const roots: string[] = []
  for (const a of assets) {
    if (a.parent_id && assets.some(x => x.id === a.parent_id)) {
      (children[a.parent_id] ??= []).push(a.id)
    } else {
      roots.push(a.id)
    }
  }
  return { assets, roots, children }
}

/** 项目覆盖度对账报告。 */
export function assetCoverage(io: WriteContext, projectId: string): CoverageReport {
  const project: Project | undefined = io.tables.projects.get(projectId)
  if (!project) throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${projectId}`)
  const assets = listAssets(io, projectId)
  const findings: Finding[] = [...io.tables.findings.entries()]
    .map(([, v]) => v)
    .filter(f => f.project_id === projectId)
  return coverageReport(assets, findings)
}
