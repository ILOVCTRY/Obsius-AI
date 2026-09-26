/** sec 黑板 Remote 跨线类型（所有 wire 边界类型必须从 ./types 公开）。 */

/** 黑板域版本信息（M2 验收点：域版本）。 */
export interface DomainVersionInfo {
  /** 域名称。 */
  name: string
  /** 域版本（defineDomain version 盖章）。 */
  version: number
  /** 域布局。 */
  layout: 'per-record'
  /** 对齐的 Python 黑板 schema 版本。 */
  schemaVersion: number
}
