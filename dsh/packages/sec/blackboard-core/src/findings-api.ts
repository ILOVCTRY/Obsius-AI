/**
 * findings 写读面（对齐 Python add_finding 简化版）：
 * dedup_fp 去重 → 证据并集 / severity 就高 / status 只升不降；
 * 疑似重复指纹分裂只提示不阻塞（dedup_warning，cap 3）。
 */
import { z } from 'zod'
import { newId, now } from './ids'
import type { Finding } from './types'
import { SEVERITIES, FINDING_STATUSES } from './schema'
import { BlackboardError } from './error'
import { parseInput } from './validation'
import { mergeEvidence } from './evidence'
import type { WriteContext } from './tables'

/** severity 排序（就高用）。 */
const SEVERITY_RANK: Record<(typeof SEVERITIES)[number], number> = {
  info: 0, low: 1, medium: 2, high: 3, critical: 4,
}

/** 登记入参。 */
export const FindingAddInput = z.strictObject({
  projectId: z.string(),
  title: z.string().min(1).max(300),
  vulnClass: z.string().max(128).optional(),
  targetAssetId: z.string().nullable().optional(),
  severity: z.enum(SEVERITIES).optional(),
  status: z.enum(FINDING_STATUSES).optional(),
  evidence: z.record(z.string(), z.unknown()).optional(),
  dedupFp: z.string().min(1).optional(),
  impact: z.string().max(4000).optional(),
  remediation: z.string().max(4000).optional(),
  author: z.string().min(1).max(64).optional(),
  sessionId: z.string().nullable().optional(),
})

/** 列表过滤入参。 */
export const FindingListInput = z.strictObject({
  targetAssetId: z.string().nullable().optional(),
  status: z.enum(FINDING_STATUSES).optional(),
})

/** 疑似重复条目。 */
export interface DedupWarning {
  id: string
  title: string
}

/** 登记结果。 */
export interface FindingAddResult {
  id: string
  merged: boolean
  severity: string
  dedupWarning?: DedupWarning[]
}

/**
 * 登记发现：命中 dedup_fp 走合并，否则建新行。
 */
export async function addFinding(io: WriteContext, raw: unknown): Promise<FindingAddResult> {
  const input = parseInput(FindingAddInput, raw)
  if (!io.tables.projects.get(input.projectId)) {
    throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${input.projectId}`)
  }
  const targetId = input.targetAssetId ?? null
  if (targetId) {
    const target = io.tables.assets.get(targetId)
    if (!target || target.project_id !== input.projectId) {
      throw new BlackboardError('BB_VALIDATION', `目标资产不存在或跨项目: ${targetId}`)
    }
  }
  const vulnClass = input.vulnClass ?? ''
  const severity = input.severity ?? 'info'
  const status = input.status ?? 'unverified'
  // key 缺省时 vulnClass 兜底；两者皆空 = 以自身 id 为指纹永不合并。
  let key = input.dedupFp ?? (vulnClass || null)

  let match: Finding | undefined
  if (key !== null) {
    for (const [, f] of io.tables.findings.entries()) {
      if (f.project_id === input.projectId
          && f.target_asset_id === targetId
          && f.dedup_fp === key) {
        match = f
        break
      }
    }
  }

  let id: string
  let merged: boolean
  let finalSeverity = severity

  if (match) {
    id = match.id
    merged = true
    const mergedEvidence = mergeEvidence(match.evidence, input.evidence ?? {})
    // severity 就高；status 只升不降（verified 人工出口除外）。
    let nextStatus = match.status
    if (SEVERITY_RANK[severity] > SEVERITY_RANK[match.severity]) {
      finalSeverity = severity
    } else {
      finalSeverity = match.severity
    }
    if (status === 'verified') nextStatus = 'verified'
    const next: Finding = {
      ...match,
      severity: finalSeverity,
      status: nextStatus,
      evidence: mergedEvidence,
      impact: match.impact || input.impact || '',
      remediation: match.remediation || input.remediation || '',
      revision: match.revision + 1,
      updated_at: now(),
    }
    await io.tables.findings.put(id, next)
  } else {
    id = newId('find')
    if (key === null) key = id
    merged = false
    const ts = now()
    const finding: Finding = {
      id,
      project_id: input.projectId,
      target_asset_id: targetId,
      vuln_class: vulnClass,
      title: input.title,
      severity,
      status,
      impact: (input.impact ?? '').trim(),
      remediation: (input.remediation ?? '').trim(),
      evidence: input.evidence ?? {},
      dedup_fp: key,
      revision: 1,
      created_at: ts,
      updated_at: ts,
    }
    await io.tables.findings.put(id, finding)
  }

  await io.appendEvent(
    input.projectId,
    merged ? 'finding.merged' : 'finding.new',
    {
      finding_id: id,
      vuln_class: vulnClass,
      severity: finalSeverity,
      title: input.title,
      target_asset_id: targetId,
      status,
    },
    input.sessionId ?? null,
  )

  const result: FindingAddResult = { id, merged, severity: finalSeverity }

  // 疑似重复：同 target 同 vuln_class 不同指纹（只提示不阻塞；无 target/无类不查）。
  if (targetId && vulnClass) {
    const dups = [...io.tables.findings.entries()]
      .map(([, f]) => f)
      .filter(f => f.project_id === input.projectId
        && f.target_asset_id === targetId
        && f.vuln_class === vulnClass
        && f.dedup_fp !== key
        && f.id !== id)
      .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
      .slice(0, 3)
    if (dups.length > 0) {
      result.dedupWarning = dups.map(f => ({ id: f.id, title: f.title }))
    }
  }

  return result
}

/** 取发现（不存在 / 跨项目 BB_NOT_FOUND）。 */
export function getFinding(io: WriteContext, projectId: string, id: string): Finding {
  const f = io.tables.findings.get(id)
  if (!f || f.project_id !== projectId) {
    throw new BlackboardError('BB_NOT_FOUND', `发现不存在: ${id}`)
  }
  return f
}

/** 列项目发现（目标 / 状态过滤）。 */
export function listFindings(io: WriteContext, projectId: string, raw: unknown = {}): Finding[] {
  const opts = parseInput(FindingListInput, raw)
  return [...io.tables.findings.entries()]
    .map(([, v]) => v)
    .filter(f => f.project_id === projectId)
    .filter(f => opts.targetAssetId === undefined || f.target_asset_id === opts.targetAssetId)
    .filter(f => !opts.status || f.status === opts.status)
    .sort((a, b) => (a.created_at < b.created_at ? -1 : 1))
}
