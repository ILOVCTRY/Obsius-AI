/**
 * 输入校验统一口：zod 失败折成 BlackboardError('BB_VALIDATION')，
 * 服务面/Remote 面共用同一错误码口径。
 */
import { ZodError, type ZodType } from 'zod'
import { BlackboardError } from './error'

/**
 * 按 zod schema 解析入参；失败抛 BB_VALIDATION。
 * @param schema - zod schema（带 parse）。
 * @param raw - 原始入参。
 * @returns 解析后的值。
 */
export function parseInput<T>(schema: ZodType<T> | { parse(raw: unknown): T }, raw: unknown): T {
  try {
    return schema.parse(raw)
  } catch (e) {
    if (e instanceof ZodError) {
      const msg = e.issues
        .map(i => `${i.path.map(String).join('.') || '<root>'}: ${i.message}`)
        .join('；')
      throw new BlackboardError('BB_VALIDATION', msg)
    }
    throw e
  }
}
