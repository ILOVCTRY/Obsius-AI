/**
 * 结果摘要（移植 ExecutionResult.brief）：喂给 Agent 上下文的截断版，
 * 防止工具输出淹没上下文。截断时尾部追加可见中文标记——Agent 必须能察觉
 * 被切，否则靠语义发现浪费 LLM 轮次。
 */

/** buildBrief 入参。 */
export interface BriefInput {
  runtime: string
  exitCode: number | null
  stdout: string
  stderr: string
  timedOut: boolean
  interrupted: boolean
}

/** 组装模型可读摘要。limit=每流保留字符数（默认 2000）。 */
export function buildBrief(input: BriefInput, limit = 2000): string {
  let s = `[${input.runtime}] exit=${input.exitCode}`
  if (input.interrupted) {
    s += ' (被人手中断)'
  } else if (input.timedOut) {
    s += ' (超时被杀)'
  }
  if (input.stdout) {
    let out = input.stdout.slice(0, limit)
    if (input.stdout.length > limit) {
      out += `\n…[stdout 已截断：${limit}/${input.stdout.length} 字符，请缩小窗口分段读]`
    }
    s += `\n${out}`
  }
  if (input.stderr) {
    let err = input.stderr.slice(0, limit)
    if (input.stderr.length > limit) {
      err += `\n…[stderr 已截断：${limit}/${input.stderr.length} 字符，请缩小窗口分段读]`
    }
    s += `\n[stderr]\n${err}`
  }
  return s
}
