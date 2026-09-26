/**
 * sec-blackboard-core 入口插件：装配 BlackboardService（打开 sec 域）。
 */
import type { Context } from '@deepseek-ai/cordis'
import { BlackboardService } from './service'

/** 插件名。 */
export const name = 'sec-blackboard-core'
/** storage-domain 必须先行（sec 域经它打开）。 */
export const inject = ['storageDomain']

/**
 * @param ctx - Host 上下文。
 */
export function apply(ctx: Context): void {
  ctx.plugin(BlackboardService)
}
