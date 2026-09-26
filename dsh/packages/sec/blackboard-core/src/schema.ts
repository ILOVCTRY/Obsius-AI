/**
 * sec 黑板六表记录 schema（zod 4.4.3，全部 strictObject）。
 * 记录事实层：projects / assets / findings / tasks / events / approvals。
 * 输入 schema（建/改入参）在各业务切片文件中单独声明。
 */
import { z } from 'zod'

/** UTC 秒级 ISO8601 时间。 */
const timestamp = z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)

/** 项目状态（TS 域新增：Python 侧无此列，默认 active）。 */
export const PROJECT_STATUSES = ['active', 'archived'] as const

/** 资产扫描状态六态（对齐 Python set_asset_status 白名单）。 */
export const ASSET_STATUSES = [
  'open',
  'visited',
  'scanning',
  'tested_clean',
  'budget_stop',
  'na',
] as const

/** 严重度五档。 */
export const SEVERITIES = ['info', 'low', 'medium', 'high', 'critical'] as const

/** 发现状态（只升不降：unverified→verified，false-positive 为人工出口）。 */
export const FINDING_STATUSES = ['unverified', 'verified', 'false-positive'] as const

/** 任务状态四态（M2 记录事实层；调度层归 M5）。 */
export const TASK_STATUSES = ['open', 'claimed', 'done', 'failed'] as const

/** 审批状态三态。 */
export const APPROVAL_STATUSES = ['pending', 'approved', 'rejected'] as const

/** 不透明 JSON 对象。 */
const jsonObject = z.record(z.string(), z.unknown())

/** projects 记录。 */
export const ProjectRecord = z.strictObject({
  id: z.string(),
  name: z.string().min(1),
  track: z.string(),
  status: z.enum(PROJECT_STATUSES),
  config: jsonObject,
  created_at: timestamp,
  updated_at: timestamp,
})

/** assets 记录。 */
export const AssetRecord = z.strictObject({
  id: z.string(),
  project_id: z.string(),
  type: z.string().min(1),
  value: z.string().min(1),
  parent_id: z.string().nullable(),
  status: z.enum(ASSET_STATUSES),
  meta: jsonObject,
  revision: z.number().int().min(1),
  created_at: timestamp,
  updated_at: timestamp,
})

/** findings 记录。 */
export const FindingRecord = z.strictObject({
  id: z.string(),
  project_id: z.string(),
  target_asset_id: z.string().nullable(),
  vuln_class: z.string(),
  title: z.string().min(1),
  severity: z.enum(SEVERITIES),
  status: z.enum(FINDING_STATUSES),
  impact: z.string(),
  remediation: z.string(),
  evidence: jsonObject,
  dedup_fp: z.string().min(1),
  revision: z.number().int().min(1),
  created_at: timestamp,
  updated_at: timestamp,
})

/** tasks 记录（M2 字段；调度列 revision/assignee 已含，租约/冲突归 M5）。 */
export const TaskRecord = z.strictObject({
  id: z.string(),
  project_id: z.string(),
  title: z.string().min(1),
  status: z.enum(TASK_STATUSES),
  assignee: z.string().nullable(),
  note: z.string(),
  dedup_fp: z.string(),
  revision: z.number().int().min(1),
  created_at: timestamp,
  updated_at: timestamp,
})

/** events 记录（id=evt-12 位零填充单调键）。 */
export const EventRecord = z.strictObject({
  id: z.string(),
  project_id: z.string(),
  kind: z.string().min(1),
  payload: jsonObject,
  session_id: z.string().nullable(),
  created_at: timestamp,
})

/** 审批动作：至少带 kind，其余字段透传（net.real 等）。 */
export const ApprovalAction = z.object({
  kind: z.string().min(1),
}).passthrough()

/** approvals 记录。 */
export const ApprovalRecord = z.strictObject({
  id: z.string(),
  project_id: z.string(),
  action: ApprovalAction,
  status: z.enum(APPROVAL_STATUSES),
  requested_by: z.string(),
  decided_by: z.string().nullable(),
  decision_note: z.string(),
  consumed: z.boolean(),
  created_at: timestamp,
  decided_at: timestamp.nullable(),
})
