/**
 * @sec/blackboard-controller — sec 黑板对外 Remote API（M2.0 脚手架）。
 * 最终 28 动词在 M2.6 落地；当前仅 versionGet 用于打通生成管线。
 */
import type { Context } from '@deepseek-ai/cordis'
import { Remote, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import type { DomainVersionInfo } from './types'

export type { DomainVersionInfo } from './types'

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** sec 黑板 Remote 服务所有者（namespace=blackboard）。 */
    blackboardController: BlackboardController
  }
}

/** Host 服务，支撑生成的 ctx.remote.blackboard 命名空间。 */
export class BlackboardController extends TypertRemoteService {
  static inject = ['typert']

  /**
   * @param ctx - Host 上下文。
   */
  constructor(ctx: Context) {
    super(ctx, 'blackboardController', { namespace: 'blackboard' })
  }

  /**
   * 读取黑板域版本信息。
   * @returns 域名称 / 域版本 / 布局 / schema 版本。
   */
  @Remote('versionGet')
  versionGet(): DomainVersionInfo {
    return { name: 'sec', version: 1, layout: 'per-record', schemaVersion: 24 }
  }
}
