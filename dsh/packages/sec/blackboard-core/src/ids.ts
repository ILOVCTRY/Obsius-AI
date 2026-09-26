/**
 * ID / 时间约定：与 Python 黑板同一规则。
 * 业务 id = `<前缀>-<uuid4 hex 前12>`；时间 UTC ISO8601 秒级。
 */
import { randomUUID } from 'node:crypto'

/**
 * 生成业务 id。
 * @param prefix - id 前缀（proj/asset/find/task/appr 等）。
 * @returns `<前缀>-<12hex>`。
 */
export function newId(prefix: string): string {
  return `${prefix}-${randomUUID().replace(/-/g, '').slice(0, 12)}`
}

/**
 * 当前 UTC 时间，秒级 ISO8601（`2026-09-26T08:30:00Z`）。
 * @returns 秒级时间字符串。
 */
export function now(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
}
