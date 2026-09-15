// x64dbg 下断脚本（纯前端模板，不触网、不执行样本）。
// 用途：人在 x64dbg 里手动粘贴运行，命中时把寄存器写进日志，导出日志后
// 经「传日志确认」回流为 finding 的动态验证凭据（verified / confirm_by=dynamic）。

export function x64dbgScript(addr: string, bits: number, name?: string): string {
  const is32 = bits === 32
  const p = is32 ? "e" : "r"
  // Win64 入参 rcx/rdx/r8/r9；cdecl 32 位入参在栈上。日志只取计划要求的寄存器集。
  const logArgs = is32
    ? `${p}ax={${p}ax} ${p}cx={${p}cx} ${p}dx={${p}dx} ${p}sp={${p}sp}`
    : `rax={rax} rcx={rcx} rdx={rdx} r8={r8} r9={r9} rsp={rsp}`
  const label = name ? `${name} @ ${addr}` : addr
  return [
    `// cyberstrike-pro 人工动态验证：${label}（${bits} 位）`,
    `// 粘贴到 x64dbg 脚本窗口执行；命中断点后看日志窗口，随后 File → Export → Log 导出`,
    `bp ${addr}`,
    `SetBreakpointLog ${addr}, "[hit ${label}] ${logArgs}"`,
    `SetBreakpointLogCondition ${addr}, "1"`,
  ].join("\n")
}
