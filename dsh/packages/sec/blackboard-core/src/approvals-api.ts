/**
 * approvals 写读面（对齐 Python request_approval / decide_approval）：
 * pending → approved/rejected 一次性裁决；request 不发事件，裁决落 approval.{decision}。
 * 另含运行时核验：verifyNetReal（status=approved 且 action.kind=net.real 且未消费）
 * 与 consumeApproval（置 consumed，防审批复用）——供执行网关经 ctx.get 调用，
 * 不开 Remote 动词。比 Python 仅判状态更严（安全默认值，宁严勿松）。
 */
import { z } from 'zod'
import { newId, now } from './ids'
import type { Approval } from './types'
import { APPROVAL_STATUSES } from './schema'
import { ApprovalAction } from './schema'
import { BlackboardError } from './error'
import { parseInput } from './validation'
import type { WriteContext } from './tables'

/** 请求入参（risk 不落库：TS 域无此列）。 */
export const ApprovalRequestInput = z.strictObject({
  projectId: z.string(),
  action: ApprovalAction,
  requestedBy: z.string().min(1).max(64).optional(),
  author: z.string().min(1).max(64).optional(),
  sessionId: z.string().min(1).max(128).nullable().optional(),
})

/** 裁决入参。 */
export const ApprovalDecideInput = z.strictObject({
  decision: z.enum(['approved', 'rejected']),
  decidedBy: z.string().min(1).max(64).optional(),
  note: z.string().max(4000).optional(),
  sessionId: z.string().min(1).max(128).nullable().optional(),
})

/** 列表过滤入参。 */
export const ApprovalListInput = z.strictObject({
  status: z.enum(APPROVAL_STATUSES).optional(),
})

/** verifyNetReal 暴露给执行网关的最小形状。 */
export interface NetRealApproval {
  id: string
  projectId: string
  action: { kind: string }
}

/**
 * 请求审批：项目须存在，落 pending 行（consumed=false）。
 * Python 侧 request 不发审计事件，保持一致。
 */
export async function requestApproval(io: WriteContext, raw: unknown): Promise<Approval> {
  const input = parseInput(ApprovalRequestInput, raw)
  if (!io.tables.projects.get(input.projectId)) {
    throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${input.projectId}`)
  }
  const id = newId('appr')
  const approval: Approval = {
    id,
    project_id: input.projectId,
    action: input.action,
    status: 'pending',
    requested_by: input.requestedBy ?? input.author ?? 'human',
    decided_by: null,
    decision_note: '',
    consumed: false,
    created_at: now(),
    decided_at: null,
  }
  await io.tables.approvals.put(id, approval)
  return approval
}

/** 取审批（不存在 BB_NOT_FOUND）。 */
export function getApproval(io: WriteContext, id: string): Approval {
  const a = io.tables.approvals.get(id)
  if (!a) throw new BlackboardError('BB_NOT_FOUND', `审批不存在: ${id}`)
  return a
}

/** 列审批（可按状态过滤；created_at 升序）。 */
export function listApprovals(io: WriteContext, projectId: string, raw: unknown = {}): Approval[] {
  const opts = parseInput(ApprovalListInput, raw)
  return [...io.tables.approvals.entries()]
    .map(([, v]) => v)
    .filter(a => a.project_id === projectId)
    .filter(a => !opts.status || a.status === opts.status)
    .sort((a, b) => (a.created_at < b.created_at ? -1 : 1))
}

/**
 * 裁决审批：仅 pending 可裁决，否则 BB_CONFLICT；
 * 落 approval.approved / approval.rejected 事件（payload 带 action）。
 */
export async function decideApproval(io: WriteContext, approvalId: string, raw: unknown): Promise<Approval> {
  const input = parseInput(ApprovalDecideInput, raw)
  const cur = io.tables.approvals.get(approvalId)
  if (!cur) throw new BlackboardError('BB_NOT_FOUND', `审批不存在: ${approvalId}`)
  if (cur.status !== 'pending') {
    throw new BlackboardError(
      'BB_CONFLICT',
      `审批 ${approvalId} 已处理（${cur.status}），不可重复裁决`,
    )
  }
  const next: Approval = {
    ...cur,
    status: input.decision,
    decided_by: input.decidedBy ?? 'human',
    decision_note: input.note ?? '',
    decided_at: now(),
  }
  await io.tables.approvals.put(approvalId, next)
  await io.appendEvent(cur.project_id, `approval.${input.decision}`, {
    approval_id: approvalId,
    action: cur.action,
    requested_by: cur.requested_by,
  }, input.sessionId ?? null)
  return next
}

/**
 * net=real 放行核验：行存在 + status=approved + action.kind=net.real + 未消费，
 * 任一不满足抛 BB_APPROVAL。只读不标记（消费在命令完成后由 consumeApproval 落）。
 */
export function verifyNetReal(io: WriteContext, approvalId: string): NetRealApproval {
  const a = io.tables.approvals.get(approvalId)
  if (!a) throw new BlackboardError('BB_APPROVAL', `审批不存在: ${approvalId}`)
  if (a.status !== 'approved') {
    throw new BlackboardError(
      'BB_APPROVAL',
      `net=real 审批未通过：${approvalId} 当前状态=${a.status}`,
    )
  }
  if (a.action.kind !== 'net.real') {
    throw new BlackboardError(
      'BB_APPROVAL',
      `审批动作不匹配：${approvalId} action.kind=${a.action.kind}（需要 net.real）`,
    )
  }
  if (a.consumed) {
    throw new BlackboardError('BB_APPROVAL', `审批已消费，不可复用: ${approvalId}`)
  }
  return { id: a.id, projectId: a.project_id, action: { kind: a.action.kind } }
}

/**
 * 消费审批：重新核验通过后置 consumed=true；重复消费抛 BB_APPROVAL。
 */
export async function consumeApproval(io: WriteContext, approvalId: string): Promise<void> {
  // verifyNetReal 覆盖不存在/状态/动作/已消费四种拒因。
  verifyNetReal(io, approvalId)
  const cur = io.tables.approvals.get(approvalId)!
  await io.tables.approvals.put(approvalId, { ...cur, consumed: true })
}
