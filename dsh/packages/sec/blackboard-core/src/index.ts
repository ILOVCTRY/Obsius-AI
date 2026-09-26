/**
 * @sec/blackboard-core — sec 黑板域：六表记录 + 单一写入口服务。
 * 纯域与服务，不含 Remote（Remote 面在 @sec/blackboard-controller）。
 */
export * from './ids'
export * from './schema'
export * from './types'
export * from './domain'
export * from './error'
export * from './validation'
export * from './detect-type'
export * from './coverage'
export * from './tables'
export * from './evidence'
export * from './projects-api'
export * from './assets-api'
export * from './findings-api'
export * from './tasks-api'
export * from './events-query'
export * from './approvals-api'
export {
  BlackboardService,
  SCHEMA_VERSION,
} from './service'
export type { DomainVersionInfo } from './service'
export {
  name,
  inject,
  apply,
} from './register'
