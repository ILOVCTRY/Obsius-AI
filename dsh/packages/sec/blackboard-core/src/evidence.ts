/**
 * 发现证据并集（对齐 Python merge_finding_evidence / _evidence_item_fp）。
 */
/** 列表键：按内容指纹去重追加。 */
const EVIDENCE_LIST_UNION_KEYS = ['pocs', 'repro_steps', 'requests', 'relates_to', 'screenshots']

/**
 * 稳定序列化（键排序），证据项去重指纹。
 * @param value - 任意 JSON 值。
 * @returns 规范字符串。
 */
export function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>
    return `{${Object.keys(obj)
      .sort()
      .map(k => `${JSON.stringify(k)}:${stableStringify(obj[k])}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined || value === '') return true
  if (Array.isArray(value) && value.length === 0) return true
  if (typeof value === 'object' && value !== null && Object.keys(value).length === 0) return true
  return false
}

/**
 * 重复发现证据并集：
 * 列表键去重追加；notes 非空分段追加；其余键只补空不覆盖。
 * @param oldEv - 既有证据。
 * @param newEv - 新报证据。
 * @returns 合并后证据（新对象）。
 */
export function mergeEvidence(
  oldEv: Record<string, unknown>,
  newEv: Record<string, unknown>,
): Record<string, unknown> {
  const merged: Record<string, unknown> = { ...oldEv }
  for (const [k, v] of Object.entries(newEv)) {
    if (EVIDENCE_LIST_UNION_KEYS.includes(k) && Array.isArray(v)) {
      const base = Array.isArray(merged[k]) ? [...(merged[k] as unknown[])] : []
      const seen = new Set(base.map(stableStringify))
      for (const item of v) {
        const fp = stableStringify(item)
        if (!seen.has(fp)) {
          seen.add(fp)
          base.push(item)
        }
      }
      merged[k] = base
    } else if (k === 'notes' && typeof v === 'string' && v.trim()) {
      const cur = merged[k]
      merged[k] = typeof cur === 'string' && cur.trim()
        ? `${cur.replace(/\s+$/, '')}\n\n${v.trim()}`
        : v.trim()
    } else if (isEmpty(merged[k])) {
      merged[k] = v
    }
  }
  return merged
}
