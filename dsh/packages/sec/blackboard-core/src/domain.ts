/**
 * sec 域声明：单一域 v1，per-record 布局。
 * global 持 {eventSeq}——events 单调序号的唯一事实源。
 */
import { z } from 'zod'
import { defineDomain, domainTable } from '@deepseek-ai/dsh-storage-domain'
import {
  ProjectRecord,
  AssetRecord,
  FindingRecord,
  TaskRecord,
  EventRecord,
  ApprovalRecord,
} from './schema'

/** global 槽 schema（不可接受 null：null 是「未写入」哨兵）。 */
export const SecGlobal = z.strictObject({
  /** 已分配的最大事件序号（append event 时 +1）。 */
  eventSeq: z.number().int().nonnegative(),
})

/** sec 域定义（version 盖章即「域版本」验收点）。 */
export const SEC_DOMAIN = defineDomain({
  name: 'sec',
  version: 1,
  layout: 'per-record',
  global: {
    schema: SecGlobal,
    initial: { eventSeq: 0 },
  },
  tables: {
    projects: domainTable(ProjectRecord),
    assets: domainTable(AssetRecord),
    findings: domainTable(FindingRecord),
    tasks: domainTable(TaskRecord),
    events: domainTable(EventRecord),
    approvals: domainTable(ApprovalRecord),
  },
})
