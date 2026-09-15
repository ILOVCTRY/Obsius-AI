// Cheat Engine Lua 脚本档（纯前端模板，不触网、不附加进程、不执行样本）。
// 与 x64dbg.ts 同纪律：人在 CE 里手动粘贴；平台只生成文本。
// ASLR 下用「模块名+偏移」表达地址：delta = addr - imagebase。

export interface CeScriptOpts {
  /** 模块文件名（overview.meta.filename）；缺失时给占位由人替换 */
  moduleName?: string
  /** 函数地址（hex 串，如 "0x401189"） */
  addr: string
  /** 镜像基址（hex 串，如 "0x400000"） */
  imagebase?: string
  bits: number
  name?: string
}

function bi(s: string): bigint {
  const t = s.trim()
  return BigInt(t.toLowerCase().startsWith("0x") ? t : `0x${t}`)
}

export function ceScript({ moduleName, addr, imagebase, bits, name }: CeScriptOpts): string {
  const is32 = bits === 32
  const ax = is32 ? "EAX" : "RAX"
  const ip = is32 ? "EIP" : "RIP"
  const regs = is32
    ? { fmt: "EAX=%x ECX=%x EDX=%x ESP=%x", args: "EAX, ECX, EDX, ESP" }
    : { fmt: "RAX=%x RCX=%x RDX=%x R8=%x R9=%x RSP=%x",
        args: "RAX, RCX, RDX, R8, R9, RSP" }
  const label = name ? `${name} @ ${addr}` : addr
  const mod = moduleName?.trim() || "<模块文件名.exe>"

  const lines: string[] = [
    `-- cyberstrike-pro 人工动态验证（Cheat Engine Lua）：${label}（${bits} 位）`,
    "-- 用法：CE 附加目标进程后，Memory View → Tools → Lua Engine，粘贴 Execute。",
    "-- 平台只生成脚本文本：不附加进程、不扫描内存，动态分析纪律由人负责。",
    `local MODULE = ${JSON.stringify(mod)}`,
  ]
  if (mod.startsWith("<")) {
    lines.push("-- ⚠ 未知模块文件名：把 MODULE 替换成目标主模块（通常是样本自身的 exe/dll 名）。")
  }

  // 目标地址：优先「模块+偏移」（免疫 ASLR），缺基址/delta 异常时退回绝对地址并警告。
  let target: string
  if (imagebase) {
    try {
      const delta = bi(addr) - bi(imagebase)
      if (delta < 0) {
        lines.push(`-- ⚠ delta 为负（${addr} < imagebase ${imagebase}），地址不在该模块内；`)
        lines.push("--   退回绝对地址，重定位后会失效，请核对模块与基址。")
        target = addr
      } else {
        lines.push(`local delta = 0x${delta.toString(16)}   -- ${addr} - ${imagebase}（模块内偏移）`)
        target = `getAddress(MODULE .. "+" .. string.format("%x", delta))`
      }
    } catch {
      lines.push("-- ⚠ imagebase/addr 解析失败，退回绝对地址。")
      target = addr
    }
  } else {
    lines.push("-- ⚠ 缓存缺 imagebase，只能用绝对地址；开启 ASLR 后会失效，建议改用「模块+偏移」。")
    target = addr
  }

  lines.push(
    `local bp_addr = ${target}`,
    "",
    "-- ① 断点观察：命中打印寄存器，自动继续",
    "debug_setBreakpoint(bp_addr)",
    "debugger_onBreakpoint = function()",
    `  if ${ip} == bp_addr then`,
    `    print(string.format("[hit ${label}] ${regs.fmt}", ${regs.args}))`,
    "    debug_continueFromBreakpoint(co_run)",
    "    return 1",
    "  end",
    "end",
    "",
    "-- ② 爆破模板（check 类函数：在 ret 断点把返回值改成 1）",
    "-- 先在反汇编里确认 ret 指令地址，按同一「模块+偏移」算法替换 ret_delta：",
    "-- local ret_addr = getAddress(MODULE .. \"+<ret_offset>\")",
    "-- debug_setBreakpoint(ret_addr, function()",
    `--   ${ax} = 1          -- ${bits} 位下写 ${ax}`,
    "--   debug_continueFromBreakpoint(co_run)",
    "--   return 1",
    "-- end)",
    "",
    "-- 观察结束后清理：debug_removeBreakpoint(bp_addr)",
  )
  return lines.join("\n")
}
