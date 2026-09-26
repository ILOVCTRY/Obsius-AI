/**
 * 覆盖度对账（对齐 Python core/coverage.py）：纯函数查询层。
 * 输入资产/发现快照，输出单资产对账态 / 分组对账报告 / 根状态读时派生。
 */
import type { Asset, Finding } from './types'

/** 终态（"测完"）状态机口径。 */
export const TERMINAL_STATUSES = ['tested_clean', 'na'] as const

/** 对账面资产类型（binary 等不入组）。 */
export const COVERAGE_TYPES = ['host', 'domain', 'url', 'service'] as const

export interface CoverageInput {
  assets: Asset[]
  findings?: Finding[]
}

/**
 * 单资产对账态：terminal-tested_clean/na/dead_end/finding | visited | scanning | open。
 * 判定顺序：状态机终态 > 死路标记 > 发现挂链 > 半程 > open。
 * @param asset - 资产记录。
 * @param findings - 挂在该资产上的发现。
 * @returns 对账态字符串。
 */
export function assetTerminalState(asset: Asset, findings: Finding[] = []): string {
  const st = asset.status
  if ((TERMINAL_STATUSES as readonly string[]).includes(st)) return `terminal-${st}`
  const nonFp = findings.filter(f => f.status !== 'false-positive')
  if (asset.meta.dead_end || (findings.length > 0 && nonFp.length === 0)) {
    return 'terminal-dead_end'
  }
  if (nonFp.length > 0) return 'terminal-finding'
  if (st === 'visited' || st === 'scanning') return st
  return 'open'
}

/** 分组对账结果。 */
export interface GroupCoverage {
  group: string
  total: number
  terminal: number
  converged: number
  done: boolean
  uncovered: Array<{ id: string; type: string; value: string; state: string }>
}

/** 覆盖报告。 */
export interface CoverageReport {
  overall: { groups: number; groups_done: number; assets: number; converged: number }
  by_group: GroupCoverage[]
}

/**
 * 全项目覆盖度对账。
 * @param assets - 项目全部资产。
 * @param findings - 项目全部发现。
 * @param uncoveredCap - 每组未覆盖清单上限。
 * @returns overall + by_group。
 */
export function coverageReport(
  assets: Asset[],
  findings: Finding[],
  uncoveredCap = 30,
): CoverageReport {
  const scoped = assets.filter(a => (COVERAGE_TYPES as readonly string[]).includes(a.type))
  const byId = new Map(scoped.map(a => [a.id, a]))

  const findingsByAsset = new Map<string, Finding[]>()
  for (const f of findings) {
    if (f.target_asset_id && byId.has(f.target_asset_id)) {
      findingsByAsset.set(f.target_asset_id, [...findingsByAsset.get(f.target_asset_id) ?? [], f])
    }
  }

  const state = new Map<string, string>()
  for (const a of scoped) {
    state.set(a.id, assetTerminalState(a, findingsByAsset.get(a.id)))
  }

  const children = new Map<string, Asset[]>()
  for (const a of scoped) {
    const pid = a.parent_id
    if (pid && byId.has(pid)) {
      children.set(pid, [...children.get(pid) ?? [], a])
    }
  }

  const groupKeyOf = (a: Asset): string => {
    let cur: Asset = a
    const seen = new Set<string>()
    while (cur.parent_id && byId.has(cur.parent_id) && !seen.has(cur.parent_id)) {
      seen.add(cur.id)
      cur = byId.get(cur.parent_id)!
    }
    return `${cur.type}:${cur.value.toLowerCase()}`
  }

  // 收敛传播（memo 化，in-progress 标记防环死循环）。
  const converged = new Map<string, boolean>()
  const isConverged = (aid: string): boolean => {
    const cached = converged.get(aid)
    if (cached !== undefined) return cached
    converged.set(aid, false)
    const st = state.get(aid)!
    let ok: boolean
    if (st.startsWith('terminal')) {
      ok = true
    } else {
      const kids = children.get(aid) ?? []
      ok = kids.length > 0 && kids.every(k => isConverged(k.id))
    }
    converged.set(aid, ok)
    return ok
  }

  const groups = new Map<string, Asset[]>()
  for (const a of scoped) {
    const key = groupKeyOf(a)
    groups.set(key, [...groups.get(key) ?? [], a])
  }

  const byGroup: GroupCoverage[] = []
  for (const key of [...groups.keys()].sort()) {
    const members = groups.get(key)!
    members.sort((a, b) => (state.get(a.id) === 'open' ? 0 : 1) - (state.get(b.id) === 'open' ? 0 : 1))
    const convN = members.filter(a => isConverged(a.id)).length
    byGroup.push({
      group: key,
      total: members.length,
      terminal: members.filter(a => state.get(a.id)!.startsWith('terminal')).length,
      converged: convN,
      done: convN === members.length,
      uncovered: members
        .filter(a => !isConverged(a.id))
        .map(a => ({ id: a.id, type: a.type, value: a.value.slice(0, 60), state: state.get(a.id)! }))
        .slice(0, uncoveredCap),
    })
  }

  return {
    overall: {
      groups: byGroup.length,
      groups_done: byGroup.filter(g => g.done).length,
      assets: scoped.length,
      converged: scoped.filter(a => isConverged(a.id)).length,
    },
    by_group: byGroup,
  }
}

/** 根状态读时派生条目。 */
export interface EffectiveStatus {
  status: string
  basis: 'explicit' | 'derived'
  settled: boolean
  has_findings: boolean
}

/**
 * 资产状态读时派生（叶子显式；父节点随子树派生）。
 * @param assets - 全部资产。
 * @param findings - 全部发现。
 * @returns aid → 派生状态。
 */
export function effectiveStatusMap(assets: Asset[], findings: Finding[] = []): Map<string, EffectiveStatus> {
  const scoped = assets.filter(a => (COVERAGE_TYPES as readonly string[]).includes(a.type))
  const byId = new Map(scoped.map(a => [a.id, a]))

  const findingsByAsset = new Map<string, Finding[]>()
  for (const f of findings) {
    if (f.target_asset_id && byId.has(f.target_asset_id)) {
      findingsByAsset.set(f.target_asset_id, [...findingsByAsset.get(f.target_asset_id) ?? [], f])
    }
  }

  const children = new Map<string, Asset[]>()
  for (const a of scoped) {
    if (a.parent_id && byId.has(a.parent_id)) {
      children.set(a.parent_id, [...children.get(a.parent_id) ?? [], a])
    }
  }

  const memo = new Map<string, EffectiveStatus>()
  const calc = (aid: string): EffectiveStatus => {
    const cached = memo.get(aid)
    if (cached) return cached
    const a = byId.get(aid)!
    const kids = children.get(aid) ?? []
    if (kids.length === 0) {
      const ts = assetTerminalState(a, findingsByAsset.get(aid))
      const view: EffectiveStatus = {
        status: a.status,
        basis: 'explicit',
        settled: ts.startsWith('terminal'),
        has_findings: ts === 'terminal-finding',
      }
      memo.set(aid, view)
      return view
    }
    const kidViews = kids.map(k => calc(k.id))
    const allSettled = kidViews.every(v => v.settled)
    const view: EffectiveStatus = {
      status: allSettled ? 'tested_clean' : 'open',
      basis: 'derived',
      settled: allSettled,
      has_findings: kidViews.some(v => v.has_findings),
    }
    memo.set(aid, view)
    return view
  }

  for (const a of scoped) calc(a.id)
  return memo
}
