---

phase: binary
vuln_class: [reverse, devirtualization, deobfuscation, patching, symbolic-execution]
title: 高级工具与去混淆
summary: 保护壳（VMP/Themida）与混淆（OLLVM/MBA）的共同点是「不值得完全还原」——判据是能否只追踪关键操作。符号执行工具的选择则取决于「要不要走全路径」。
---

# 高级工具与去混淆

静态工具链见 [[binary/逆向工程/tools.md|静态工具]]；动态工具（Frida/angr/lldb/x64dbg）见 [[binary/逆向工程/tools-dynamic.md|动态工具]]；模拟执行与侧信道见 [[binary/逆向工程/tools-emulation.md|模拟执行]]。

## 0. 判据速查

| 现象 | 第一动作 | 去哪节 |
|---|---|---|
| 有 `.vmp0`/`.vmp1` 节，节熵 > 7.5 | VMProtect，别想着完全反虚拟化 | §1 |
| 有 `.themida`/`.winlice` 节 | 先跑起来 dump 内存，再修 IAT | §2 |
| 给了两个版本的二进制 | 二进制 diff，看改了哪个函数 | §3 |
| OLLVM 平坦化 / MBA 表达式 | D-810（IDA）或 GOOMBA（Ghidra） | §4 |
| 想把表达式树化简 / 追数据流 | Miasm | §4 |
| 反调试太多补不完 / 异构架构 | Qiling（无调试器痕迹） | §5 |
| 单路径去混淆 + 污点追踪 | Triton（具体执行 + 符号状态） | §6 |
| 要探索所有路径 | angr（见动态工具） | §7 |
| 需要 EVM/智能合约 | Manticore | §7 |
| 想要 GUI + 内置反编译器 | Cutter | §8 |
| Ghidra 支持不了的架构 | RetDec | §9 |
| 自定义 VM 字节码很长 | lift 到 LLVM IR 让 `-O3` 化简 | §10 |
| 想反向执行 / 回退到上个断点 | `rr` | §11 |
| 输入字节之间有线性关系 | 位置编码输入 + GDB 抽约束 + ILP 求解 | §12 |
| 逐位校验（`>>bit & 1`） | 位置编码 + 监控 ZF | §13 |
| 二进制只有执行权限（`--x`） | LD_PRELOAD 构造函数读 `/proc/self/mem` | §14 |
| 上百个 `functionN(bit)` 包装 | PEDA 逐位刮取，不需要理解 `f_i` | §15 |
| 需要改二进制 | Binary Ninja API 或 LIEF | §16 |

---

## 1. VMProtect

把 x86/x64 代码虚拟化成自定义字节码，由生成的 VM 解释。CTF 里最难的一档。

### 识别

```bash
strings binary | grep -i "vmp\|vmprotect"
readelf -S binary | grep ".vmp"        # PE 里是 .vmp0/.vmp1
# 某些节的熵 > 7.5
```

**四个判据**：
- `push`/`pop` 极重的序言（VM 入口把所有寄存器压栈）
- 巨大的 switch-case 分派器（VM handler 循环）
- 反调试检查嵌在 VM handler 里
- 变异引擎：同一个操作码在不同构建里 handler 不同

### 思路

```text
1. 找 VM 入口——找 pushad/pushaq 式的序列
2. 找 handler 表——大的间接跳转 jmp [reg + offset]
3. 追踪 handler 执行——每个 handler 结尾都跳到下一个
4. 认 handler：
   vAdd / vSub / vMul / vXor / vNot   算术
   vPush / vPop                        栈操作
   vLoad / vStore                      内存访问
   vJmp / vJcc                         控制流
   vRet                                VM 退出（恢复真实寄存器）
5. 给 VM 字节码写反汇编器
6. 化简 / 去混淆 lift 出来的 IL
```

### 工具

- **VMPAttack**（IDA 插件）：自动识别 VM handler
- **NoVmp**：基于 VTIL 的反虚拟化（开源）
- 社区 IDA/Binary Ninja 反虚拟化脚本

### CTF 打法

**完全反虚拟化很少是必需的**。只追踪「对你的输入做了什么」：

```python
import frida

script = """
var vm_dispatch = ptr('0x...');   // handler 表跳转地址
Interceptor.attach(vm_dispatch, {
    onEnter(args) {
        var handler_idx = this.context.rax;
        console.log('Handler:', handler_idx, 'RSP:', this.context.rsp);
    }
});
"""
```

**判据**：hook VM 内部调用的比较/加密函数，比反虚拟化整个 VM 快一个数量级。

---

## 2. Themida / WinLicense

比 VMProtect 多了反调试层。

**识别**：`.themida`/`.winlice` 节；极重的反调试（内核级检查、装驱动）；变异 + 虚拟化 + 加壳三合一。

### 四步走

1. **dump 解包后的代码**：让它跑起来，解包完 dump 进程内存
2. **绕反调试**：x64dbg 里 ScyllaHide 选 Themida 专用预设
3. **修导入表**：Scylla 插件重建 IAT
4. **分析 dump**：解包后按普通二进制处理

```text
x64dbg 流程：
1. 载入二进制
2. 开 ScyllaHide → Profile: Themida
3. 跑到 OEP（可能要试几次）
4. Scylla dump：OEP → IAT Autosearch → Get Imports → Dump
5. Scylla → Fix Dump
6. 修好的 dump 拿去 Ghidra/IDA
```

---

## 3. 二进制 diff

**判据**：题目给了「打过补丁」和「原始」两个版本。diff 直接暴露漏洞或隐藏功能。

### BinDiff

```bash
# 先从 IDA/Ghidra 导出：File → BinExport → Export as BinExport2
bindiff primary.BinExport secondary.BinExport
```

**关键指标**：每个函数对的相似度（0.0-1.0）、变化的指令高亮、未匹配函数 = 新增/删除的代码。

### Diaphora

免费开源，IDA 插件形式：

```text
File → Script file → diaphora.py
# 先导出第一个，再打开第二个做 diff
# Ghidra 版：diaphora_ghidra.py
```

---

## 4. 去混淆框架

### D-810（IDA）

针对 OLLVM 的模式化去混淆插件。

```text
能力：
- MBA 化简：(a ^ b) + 2*(a & b) → a + b
- 死代码消除
- 恒定谓词移除
- 常量折叠
- 控制流平坦化还原（部分）

安装：拷到 IDA plugins 目录
使用：Edit → Plugins → D-810 → 选规则 → Apply
```

### GOOMBA（Ghidra）

```text
基于 Ghidra 的 P-Code，做 MBA 化简 + 已知混淆的模式匹配
安装：.jar 拷到 Ghidra 扩展目录
使用：Code Browser → Analysis → GOOMBA
```

### Miasm

带符号执行与 IR lift 的逆向框架。

```python
from miasm.analysis.binary import Container
from miasm.analysis.machine import Machine

cont = Container.from_stream(open("binary", "rb"))
machine = Machine(cont.arch)
mdis = machine.dis_engine(cont.bin_stream, loc_db=cont.loc_db)
asmcfg = mdis.dis_multiblock(entry_addr)

lifter = machine.lifter_model_call(loc_db=cont.loc_db)
ircfg = lifter.new_ircfg_from_asmcfg(asmcfg)

from miasm.ir.symbexec import SymbolicExecutionEngine
sb = SymbolicExecutionEngine(lifter)
# 符号执行后化简表达式
```

**用途**：化简表达式树、简化复杂算术、追混淆代码里的数据流。

---

## 5. Qiling

基于 Unicorn 的跨平台模拟框架，带 OS 层支持（syscall、文件系统、注册表）。

```python
from qiling import Qiling
from qiling.const import QL_VERBOSE

ql = Qiling(["./binary"], "rootfs/x8664_linux", verbose=QL_VERBOSE.DEBUG)

@ql.hook_address
def hook_check(ql, address, size):
    if address == 0x401234:
        ql.arch.regs.rax = 0
        ql.log.info("反调试已绕过")

@ql.hook_syscall(name="ptrace")
def hook_ptrace(ql, request, pid, addr, data):
    return 0

@ql.set_api("IsDebuggerPresent", target=ql.os.user_defined_api)
def hook_isdebug(ql, address, params):
    return 0

ql.run()
```

**比 Unicorn 多的四样**：OS 模拟（文件/网络/注册表）、多平台（Linux/Windows/macOS/Android/UEFI）、内置调试接口、rootfs 加载库。

**CTF 场景**：异构架构（ARM/MIPS/RISC-V）、一次性绕过所有反调试、无硬件时 fuzz 固件、不改代码就追踪执行。

**关键**：模拟环境里没有调试器痕迹，`ptrace(TRACEME)` 也不会有竞争——反调试天然失效。

---

## 6. Triton

基于 Pin 的动态二进制分析框架，带符号执行、污点分析、AST 化简。

```python
from triton import *

ctx = TritonContext(ARCH.X86_64)

with open("binary", "rb") as f:
    ctx.setConcreteMemoryAreaValue(0x400000, f.read())

for i in range(32):
    ctx.symbolizeMemory(MemoryAccess(INPUT_ADDR + i, CPUSIZE.BYTE), f"input_{i}")

pc = ENTRY_POINT
while pc:
    inst = Instruction(pc, ctx.getConcreteMemoryAreaValue(pc, 16))
    ctx.processing(inst)

    if pc == CMP_ADDR:
        ast = ctx.getPathConstraintsAst()
        for k, v in sorted(ctx.getModel(ast).items()):
            print(f"input[{k}] = {chr(v.getValue())}", end="")
        break

    pc = ctx.getConcreteRegisterValue(ctx.registers.rip)
```

### Triton vs angr

| 维度 | Triton | angr |
|---|---|---|
| 执行方式 | 具体 + 符号（DSE） | 全符号 |
| 速度 | 快（具体驱动） | 慢（探索所有路径） |
| 路径爆炸 | 少（只跟一条路） | 主要问题 |
| API | C++ / Python | Python |
| 最适合 | 单路径去混淆、污点追踪 | 多路径探索 |

**关键**：Triton 的强项是去混淆——具体跑程序，同时记符号状态，最后化简收集到的约束。

---

## 7. Manticore

Trail of Bits 的符号执行工具。类似 angr，但原生支持 EVM（以太坊）。

```python
from manticore.native import Manticore

m = Manticore("./binary")

@m.hook(0x401234)
def success(state):
    buf = state.solve_one_n_batched(state.input_symbols, 32)
    print("Flag:", bytes(buf))
    m.kill()

@m.hook(0x401256)
def fail(state):
    state.abandon()

m.run()
```

**判据**：EVM/智能合约首选；简单 Linux 二进制也能用，但复杂逆向 angr 更成熟。

---

## 8. Rizin / Cutter

Rizin 是 radare2 的维护分支，Cutter 是它的 Qt GUI。

```bash
rizin -d ./binary
> aaa
> afl
> pdf @ main
> VV                 # 可视化图模式

cutter binary         # GUI + 反编译器
```

**Cutter 的优势**：内置 Ghidra 反编译器（r2ghidra 插件）、图视图/十六进制编辑器/调试面板同在一个窗口、集成 Python/JS 脚本控制台。

---

## 9. RetDec

基于 LLVM 的可重定向反编译器，支持多架构，免费开源。

```bash
pip install retdec-decompiler
# 或网页版 https://retdec.com/decompilation/

retdec-decompiler binary
# 产出 binary.c（反编译 C）与 binary.dsm（反汇编）

retdec-decompiler --select-ranges 0x401000-0x401100 binary
```

**强项**：x86/ARM/MIPS/PowerPC/PIC32 多架构、产出可编译 C、Ghidra 支持不好的架构可用它。

---

## 10. 自定义 VM lift 到 LLVM IR

**判据**：自定义 VM 有几十个寄存器、少量操作码，直接读字节码极难，但一对一映射到 LLVM IR 很容易。

```text
流程：VM 字节码 → 自写反汇编器 → LLVM IR → 优化 → 反编译
1. 给自定义操作码写反汇编器
2. 每个操作码产出 IR：
   INC reg  → %reg = add i32 %reg, 1
   CDEC reg → 条件减
   CALL fn  → call void @fn()
3. 用 llc 或 MCJIT 优化：
   opt -O3 -S vm_lifted.ll -o vm_optimized.ll
4. 把优化后的 IR 载入 IDA 或用 RetDec 反编译
结果：1300 行 → 150 行
```

**关键**：LLVM 的内联、常量折叠、死代码消除会把 lift 出来的 VM 字节码大幅化简。一个 26 寄存器、3 操作码的自定义 VM 产生 1300 行 IL，`-O3` 后剩约 150 行，底层算法（比如 Collatz 序列）就露出来了。

---

## 11. GDB 高级用法

### Python 脚本：日志型断点

```python
import gdb

class TraceCompare(gdb.Breakpoint):
    def __init__(self, addr):
        super().__init__(f"*{addr}", gdb.BP_BREAKPOINT)

    def stop(self):
        frame = gdb.selected_frame()
        rdi = int(frame.read_register("rdi"))
        rsi = int(frame.read_register("rsi"))
        rdx = int(frame.read_register("rdx"))
        inf = gdb.selected_inferior()
        print(f"memcmp({inf.read_memory(rdi, rdx).tobytes()!r}, "
              f"{inf.read_memory(rsi, rdx).tobytes()!r}, {rdx})")
        return False          # 不停，只记录
```

### 条件断点 / 忽略次数 / 只记录

```bash
(gdb) b *0x401234 if $rax == 0x41
(gdb) b *0x401234 if *(char*)$rdi == 'f'
(gdb) ignore 1 99          # 跳过前 99 次，第 100 次才停

(gdb) b *0x401234
(gdb) commands
> silent
> printf "rax=%lx rdi=%lx\n", $rax, $rdi
> continue
> end
```

### 观察点

```bash
(gdb) watch  *(int*)0x601050     # 写时断
(gdb) rwatch *(int*)0x601050     # 读时断
(gdb) awatch *(int*)0x601050     # 读或写都断
(gdb) watch *(int*)0x601050 if *(int*)0x601050 == 0x42
```

### 反向调试（rr）

```bash
rr record ./binary
rr replay

(gdb) reverse-continue     # 倒着跑到上一个断点
(gdb) reverse-stepi        # 反向单步
(gdb) checkpoint
(gdb) restart 1            # 回到检查点 1
```

**判据**：单步走过头了、或反调试破坏状态时，倒回去比重跑一遍省事得多。

### pwndbg / GEF

```text
pwndbg 常用：
  context            寄存器/栈/代码/回溯
  vmmap              内存映射
  search -s "flag{"  内存搜字符串
  telescope $rsp 20  智能栈 dump
  cyclic 200         De Bruijn 模式
  got / plt          GOT / PLT 表

GEF 常用：
  xinfo $rdi         地址详细信息
  checksec           保护
  heap chunks        堆块列表
  pattern create 100 模式
```

---

## 12. GDB 抽线性约束 + ILP 求解

**判据**：二进制对输入字节之间施加**线性算术关系**（求和、求差）。

**手法**：发位置编码输入（`input[i] = i`），这样比较触发时就能知道涉及哪几个位置、它们的和/差应该等于多少。收集全部约束后交给 ILP 求解器。

```python
from pulp import *

n = 32
prob = LpProblem("crackme", LpMinimize)
x = [LpVariable(f'x{i}', 32, 126, cat='Integer') for i in range(n)]
prob += 0

prob += x[3] + x[7] == 0xAB
prob += x[1] - x[5] == 0x0C
# ... 全部抽到的约束

for xi in x:
    prob += xi >= 32
    prob += xi <= 126

prob.solve(PULP_CBC_CMD(msg=0))
print(''.join(chr(int(value(xi))) for xi in x))
```

**关键**：位置编码让「比较的是什么位置」自解释，不需要反推索引映射。

---

## 13. 位置编码 + 零标志监控

**判据**：二进制逐字节比较，且能单步。

发 `input[i] = i`，单步监控 ZF。当 ZF 在某次比较处被置位，说明该位置的值就等于它自己的索引——**那个索引本身就是期望值**。

```python
import gdb

class ZFMonitor(gdb.Breakpoint):
    def stop(self):
        zf = (int(gdb.parse_and_eval('$eflags')) >> 6) & 1
        if zf:
            rip = int(gdb.parse_and_eval('$rip'))
            disasm = gdb.execute(f'x/1i {rip-5}', to_string=True)
            print(f"ZF set at {rip:#x}: {disasm.strip()}")
        return False

# 输入 b'\x00\x01\x02...\x1f' 跑一遍
```

**关键**：一趟就能把每个输入字节的期望值全拿到，不需要手工逆向。

---

## 14. LD_PRELOAD dump 只有执行权限的二进制

**判据**：二进制权限是 `--x`（没有读位）。直接读不了，但内核执行时仍会把它映射进内存。

用 LD_PRELOAD 一个带构造函数的共享库，在进程内通过 `/proc/self/mem` 读自己的映射：

```c
// gcc -shared -fPIC -o dump_xo.so dump_xo.c
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

__attribute__((constructor)) void dump() {
    FILE *maps = fopen("/proc/self/maps", "r");
    char line[256];
    unsigned long base = 0, end = 0;

    while (fgets(line, sizeof(line), maps)) {
        if (strstr(line, "binary_name")) {
            sscanf(line, "%lx-%lx", &base, &end);
            break;
        }
    }
    fclose(maps);

    FILE *mem = fopen("/proc/self/mem", "rb");
    fseek(mem, base, SEEK_SET);
    size_t size = end - base;
    void *buf = malloc(size);
    fread(buf, 1, size, mem);
    fclose(mem);

    FILE *out = fopen("/tmp/dumped_binary", "wb");
    fwrite(buf, 1, size, out);
    fclose(out);
}
// 用法：LD_PRELOAD=./dump_xo.so ./binary_xo
```

**关键**：只有执行权限能防「读文件」，防不住「执行」。构造函数在进程内跑，`/proc/self/mem` 无视文件权限。

---

## 15. 逐位刮取（黑盒预言机）

**判据**：庞大的混淆校验器，每个 flag 位分派一次 `call functionN`。每个检查读 `flag[offset] >> bit & 1`，调用一个不透明的 `functionN(那一位)`，再把返回值和常量比较。

**不要**去反编译几百个包装函数——用 PEDA 单步走分派器，直接读 `edi`（参数）与 `eax`（返回值）。

```python
def get_current_inst():
    return peda.current_inst(peda.getreg("rip"))[1]

peda.execute('file ./elementary')
peda.set_breakpoint(0x555555554000 + 0xCEB88)
peda.execute("run < _input")          # _input = 'A' * 103，只需要长度对

flag = ['0'] * 832                     # 104 字节 × 8 位
while peda.getreg("rip") < 0x555555554000 + 0xD827F:
    offset = bit = 0
    while 'and' != get_current_inst()[:3]:
        cur = get_current_inst()
        if cur == 'mov    rax,QWORD PTR [rbp-0x18]':
            offset = 0; bit = 0
        elif cur.startswith('sar'):
            bit = int(cur.split(',')[1], 16)      # 移位数 → 位索引
        elif cur.startswith('add'):
            offset = int(cur.split(',')[1], 16)   # 字节索引
        peda.execute('si')
    while 'call' not in get_current_inst(): peda.execute('si')
    tmp = peda.getreg('edi')
    peda.execute('ni')
    ret = peda.getreg('eax')
    # 预言机：返回等于参数 → 该位判 0，否则判 1
    flag[offset * 8 + bit] = '0' if (ret != 0 and tmp == ret) else '1'
    peda.execute('set $eax=0')                    # 中和返回值，让循环继续
```

**关键**：任何形如 `f_i(bit_i) == const_i` 的校验器都是黑盒预言机——**不需要理解 `f_i`**。`current_inst()` + `si`/`ni` 三十行就能一趟刮完；解析前面的 `sar imm` / `add imm` 就能拿到 `(字节偏移, 位索引)`，不用反汇编校验器的算术。

---

## 16. 改二进制

### Binary Ninja API

```python
import binaryninja as bn

bv = bn.open_view("binary")

bv.write(0x401234, b"\x90" * 5)                    # NOP 掉
bv.write(0x401234, b"\x74")                        # JNZ(0x75) → JZ(0x74)
bv.write(0x401234, b"\xb8\x01\x00\x00\x00\xc3")    # mov eax,1; ret

bv.save("patched")
```

### LIEF

```python
import lief

binary = lief.parse("binary")

section = lief.ELF.Section(".patch")
section.content = list(b"\xcc" * 0x100)
section.type = lief.ELF.SECTION_TYPES.PROGBITS
section.flags = lief.ELF.SECTION_FLAGS.EXECINSTR | lief.ELF.SECTION_FLAGS.ALLOC
binary.add(section)

binary.header.entrypoint = 0x401000
binary.patch_pltgot("strcmp", 0x401000)

binary.write("patched")
```

**LIEF 的优势**：跨格式（ELF/PE/Mach-O）、能加节/段、改头、patch 导入表。

---

## 17. 纪律

- **保护壳题不要追完全反虚拟化**。追踪「对输入做了什么」就够了，hook 比较/加密函数即可。
- **Themida/VMProtect 的第一动作是 dump**，不是分析。壳里没有真代码。
- **符号执行工具按「要不要走全路径」选**：单路径去混淆用 Triton，多路径探索用 angr。
- **自定义 VM 一律先试 lift 到 LLVM IR**。让编译器做化简，比手写去混淆省一个数量级。
- **黑盒预言机 > 理解实现**：`f_i(bit) == const` 这种形状直接刮，不要读 `f_i`。
- **位置编码是通用加速器**：`input[i]=i` 让「比较的是哪一位」自解释，配 ZF 监控或约束抽取都能一趟拿全。
- **报的是「值不知道」还是「结构看不懂」**：前者下断取，后者才需要去混淆框架。
