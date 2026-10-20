---

phase: binary
vuln_class: [reverse, anti-analysis, obfuscation]
title: 反分析的对抗手法
summary: 反分析题的本质是「静态看不到真实控制流」。判据是信号处理、无 call 的函数链、父进程改写子进程这三类结构。
---

# 反分析的对抗手法

反分析的基础分类（Linux/Windows 反调试、反虚拟机、反 DBI、代码完整性、反反汇编）见 [[binary/逆向工程/anti-analysis.md|anti-analysis]]；模拟执行与指令级插桩见 [[binary/逆向工程/tools-emulation.md|模拟执行]]；脚本层混淆见 [[binary/恶意样本/scripts-and-obfuscation.md|脚本与混淆]]。

## 0. 判据速查

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| 开头就 `signal(SIGILL/SIGFPE/SIGSEGV, ...)` | 信号驱动的控制流，静态分析无效 | §1、§2 |
| 大量代码块以 `leave; ret` 结尾但没有对应 `call` | 栈帧链式调用，函数边界识别不出来 | §3 |
| IDA 报 "stack frame is too big" | 同上 | §3 |
| `fork` + 子进程满是 `0xcc` | 父进程在运行时改写子进程 | §4 |
| 只有寄存器算术，无内存写 | 可逆的变换链 | §5 |
| .NET 且方法体为空/加密 | 运行时解密，需要 dump 模块 | §6 |
| 读 `PEB.BeingDebugged` 后分支 | 比较目标值会随调试状态变化 | §5.1 |

**核心思路**：反分析只阻止**静态**理解，不阻止**动态观察**。所以大多数题的正解是「让它自己跑，然后记录它做了什么」，而不是「读懂它在做什么」。

## 1. SIGILL：用非法指令做派发

装一个 SIGILL 处理器，把"非法指令"当自定义 VM 操作码用：

```c
void sigill_handler(int sig, siginfo_t *info, void *ucontext) {
    ucontext_t *ctx = (ucontext_t *)ucontext;
    unsigned char *pc = (unsigned char *)ctx->uc_mcontext.gregs[REG_RIP];
    // 从 PC 处的字节解出自定义操作码
    // 手动推进 PC 越过这条"指令"
    ctx->uc_mcontext.gregs[REG_RIP] += opcode_length;
}
```

同一个机制还能做 **32 位 / 64 位执行模式切换** —— 处理器改 `uc_mcontext` 里的段寄存器或代码段描述符。

```bash
# 看信号投递
strace -e signal ./binary

# 关键：让 GDB 不要拦住信号，直接放行给程序自己的处理器
(gdb) handle SIGILL nostop pass
(gdb) handle SIGSEGV nostop pass
(gdb) handle SIGFPE nostop pass
```

**`handle ... nostop pass` 是最关键的一条命令**。默认 GDB 会截住所有信号，程序的处理器永远收不到，于是你看到的执行流是错的。

## 2. SIGFPE：用信号数量做侧信道

控制流完全由 SIGFPE 处理器驱动时，静态分析拿不到任何东西。但**信号触发的次数与校验进度相关** —— 逐字符爆破，正确字符会引发更多信号：

```bash
for c in {a..z} {A..Z} {0..9}; do
    count=$(echo -n "${c}AAAAAAA" | strace -e signal=SIGFPE ./binary 2>&1 | grep -c SIGFPE)
    echo "$c: $count"
done
```

信号最多（或最少，取决于实现）的那个字符就是正确的。确定一位后把它固定为前缀，继续下一位。

要点：

- **先确认单调性**：跑两个已知错误的输入，看信号数是否稳定可区分。噪声大的话多跑几次取中位数。
- **`strace -e signal=SIGFPE` 只统计信号，开销小**，比全量 strace 快得多。
- 这类题的耗时主要在进程启动，不是信号处理。可以并发跑多个候选。

## 3. 无 call 的函数链

用栈帧伪造让 `leave; ret` 自己串起来，全程没有一条 `call`：

```text
每个函数的动作：
1. 把操作数和下一个函数的地址压栈
2. 把 saved RBP 指向下一个栈帧
3. 把返回地址设成下一个函数
4. leave 从 RBP 恢复 RSP（跳到下一帧），ret 跳到下一个函数
```

结果：反汇编器追踪 `call/ret` 平衡时彻底失效，函数边界识别不出来，IDA 报 "stack frame is too big"。

**对策**：不要试图让 IDA 自动识别，**逐个代码块单独 patch 成函数**，或者干脆手工按「压栈顺序」还原调用链。

```python
# 还原出的处理链（逆向执行时按相反顺序）
def reverse_processing(byte):
    res = byte | 0x80
    res = res ^ 0xCA
    res = (res + 66) & 0xFF
    res = res ^ 0xCA
    res = (res + 66) & 0xFF
    res = res ^ 0xCA
    res = (res + 66) & 0xFF
    res = res ^ 0xFE
    return res
# 逆序应用后再反转字符顺序
```

## 4. 父进程改写子进程

`fork` 后子进程全是 `int3`（`0xcc`），父进程用 `ptrace` + `process_vm_writev` 在每次陷阱触发前把真实指令写进去。静态看子进程是垃圾，单进程调试器看不到父进程的写入。

**正解是让 strace 记录父进程的写入**：

```bash
strace -f -e trace=process_vm_writev -e write=all -o trace.log ./keygenme
```

日志形如：

```text
process_vm_writev(child_pid, [{iov_base="\x48\x89\xe5...", iov_len=12}], 1,
                  [{iov_base=0x400c80, iov_len=12}], 1, 0) = 12
```

解析出来生成 IDA 的 patch 脚本：

```python
import re, pathlib

patches = []
pattern = re.compile(
    r'process_vm_writev\(\d+, \[{iov_base="([^"]+)", iov_len=(\d+)}\].*?\[{iov_base=(0x[0-9a-f]+)',
)
for m in pattern.finditer(pathlib.Path("trace.log").read_text()):
    data = m.group(1).encode("latin1").decode("unicode_escape").encode("latin1")
    patches.append((int(m.group(3), 16), data))

with open("patch.py", "w") as fh:
    for addr, data in patches:
        for i, b in enumerate(data):
            fh.write(f"patch_byte({addr + i:#x}, {b:#x})\n")
```

在 IDA 里 `File → Script file` 加载 `patch.py`，全部写入完成后子进程就是可读的。

**同一招通用于所有「父进程改子进程代码」的实现**：`ptrace(PTRACE_POKEDATA)`、往 `/proc/<pid>/mem` 写、甚至 `write()` 到映射区 —— 只要走系统调用，strace 就看得见。

## 5. 纯算术变换链

**判据：没有内存写，只有寄存器算术**。这类完全可逆。

```text
1. 收集所有非跳转指令
2. 逆序排列
3. 成对互换逆操作：add↔sub、rol↔ror、xor 自逆
4. 用 Keystone 汇编回去，Unicorn 执行
```

```python
import idaapi, idc

def trace_transforms(start_ea, end_ea):
    out, ea = [], start_ea
    while ea < end_ea:
        mnem = idc.print_insn_mnem(ea)
        if mnem not in ("jmp", "je", "jne", "call", "ret"):
            out.append((ea, mnem, idc.print_operands(ea)))
        ea = idc.next_head(ea)
    return out

inverse_map = {"add": "sub", "sub": "add", "rol": "ror", "ror": "rol", "xor": "xor"}
inverted = [(inverse_map.get(m, m), op) for (_, m, op) in reversed(trace_transforms(0x401000, 0x401200))]
```

```python
from keystone import Ks, KS_ARCH_X86, KS_MODE_64
from unicorn import Uc, UC_ARCH_X86, UC_MODE_64
from unicorn.x86_const import UC_X86_REG_RAX

ks, uc = Ks(KS_ARCH_X86, KS_MODE_64), Uc(UC_ARCH_X86, UC_MODE_64)
encoding, _ = ks.asm("\n".join(f"{m} {op}" for m, op in inverted))

CODE_BASE = 0x400000
uc.mem_map(CODE_BASE, 0x10000)
uc.mem_write(CODE_BASE, bytes(encoding))
uc.reg_write(UC_X86_REG_RAX, known_output)      # 从观测到的输出倒推
uc.emu_start(CODE_BASE, CODE_BASE + len(encoding))
flag_bytes = uc.reg_read(UC_X86_REG_RAX).to_bytes(8, "little")
```

### 5.1 PEB 反调试会偷偷换掉比较目标

样本读 `PEB.BeingDebugged`，据此在两个不同的目标值之间二选一。**在 IDAPython 里 trace 到的是调试分支的目标值**，拿它去倒推永远是错的。

```text
对策 1：trace 之前把 BeingDebugged 改成 0
对策 2：找出两个分支，用非调试那个分支的目标值
```

判据：`mov rax, gs:[0x60]` / `fs:[0x30]` 之后有 `cmp byte ptr [rax+2], 0` 之类的比较。

## 6. .NET：运行时 dump 模块

ConfuserEx 加密方法体，在 `<Module>` 的构造函数里运行时解密。**磁盘上是密文，内存里是明文**。

```text
dnSpy:
  File → Open → target.exe
  Assembly Explorer → <Module> .cctor → F9 下断点
  F5 运行，等模块加载完成
  右键 assembly → Save Module → out.exe
```

```bash
de4dot out.exe        # 清理后续的符号混淆
```

要点：**断点下在构造函数，不是 Main**。模块构造完的瞬间才是"全部方法体都已解密"的时刻，早了下不全，晚了可能已被使用后清理。

## 7. 纪律

- **反分析阻止静态，不阻止动态**。想不通就让它跑，记录它做了什么，而不是读懂它。
- **GDB 默认会截住所有信号**。`handle SIGILL nostop pass` 之类不设，程序的处理器永远不执行，你看到的是假流程。
- **信号驱动的校验可以变成侧信道**。数信号次数比逆控制流快得多。
- **无 `call` 的函数链不要指望反汇编器**。手工按压栈顺序还原，或逐块 patch。
- **父进程改写子进程时，strace 父进程一次就够**。`process_vm_writev` 同时带地址和字节，不需要动态跟踪子进程。
- **纯算术变换一定能逆**，关键是别漏掉 `xor` 自逆、`rol/ror` 要换方向、以及**逆序**。
- **PEB 反调试会换比较目标**。trace 前先确认自己看的是哪个分支，否则倒推出来的答案是"调试模式下的正确答案"。
- **.NET 的断点下在 `<Module>` 构造函数**，那里是解密的完成点。
