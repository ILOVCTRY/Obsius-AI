/** sec 黑板错误码（controller 统一映射点）。 */

export type BlackboardErrorCode =
  | 'BB_VALIDATION'
  | 'BB_NOT_FOUND'
  | 'BB_CONFLICT'
  | 'BB_GATE'
  | 'BB_APPROVAL'

/** 黑板业务错误：携带稳定错误码，供 Remote 层转 RemoteError。 */
export class BlackboardError extends Error {
  /**
   * @param code - 稳定错误码。
   * @param message - 人类可读信息。
   */
  constructor(
    readonly code: BlackboardErrorCode,
    message: string,
  ) {
    super(message)
    this.name = 'BlackboardError'
  }
}
