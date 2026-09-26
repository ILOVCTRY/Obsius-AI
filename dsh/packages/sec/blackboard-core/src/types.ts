/** 六表记录类型（由 zod schema 推导，不重复定义）。 */
import type { z } from 'zod'
import type {
  ProjectRecord,
  AssetRecord,
  FindingRecord,
  TaskRecord,
  EventRecord,
  ApprovalRecord,
  ApprovalAction,
} from './schema'

export type Project = z.infer<typeof ProjectRecord>
export type Asset = z.infer<typeof AssetRecord>
export type Finding = z.infer<typeof FindingRecord>
export type Task = z.infer<typeof TaskRecord>
export type SecEvent = z.infer<typeof EventRecord>
export type Approval = z.infer<typeof ApprovalRecord>
export type ApprovalActionValue = z.infer<typeof ApprovalAction>
