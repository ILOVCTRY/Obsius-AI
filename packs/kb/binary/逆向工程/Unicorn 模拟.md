---

phase: reverse
vuln_class: [reverse, emulation, firmware, shellcode, vm, brute-force]
title: Unicorn 模拟
summary: 只要「没有 OS」就能用 Unicorn——裸 shellcode、bare-metal 固件、反调试切片、自定义 VM 派发。判据是需要 syscall 就换 Qiling，需要解方程就换 angr/Triton。
---

# Unicorn 模拟

工具链选型见 [[binary/逆向工程/静态工具链与异构格式.md|工具链]]、[[binary/逆向工程/模拟执行与侧信道工具.md|模拟与指令计数]]、[[binary/逆向工程/动态工具.md|动态与符号执行]]；反调试对抗见 [[binary/逆向工程/反分析的对抗手法.md|反分析对抗]]。

## 0. 判据速查

| 情形 | 用什么 | 为什么 |
|---|---|---|
| 裸 shellcode，没有 ELF | **Unicorn** | 没有 loader，映射完就能跑 |
| 有 `ptrace`/PEB/时序反调试 | **Unicorn** | 没有进程，没有痕迹 |
| bare-metal 固件 | **Unicorn** | MMIO hook 就是外设 |
| x86 宿主上跑 ARM/MIPS blob | **Unicorn** | 不用 QEMU/rootfs |
| 需要 `open`/`socket`/`ptrace` 语义 | **Qiling** | Unicorn 没有 OS 层 |
| 要符号求解、路径爆炸 | angr / Triton | Unicorn 只有具体执行 |
| 要跑整个 Linux/内核驱动 | QEMU | 需要完整系统与外设 |

工作流：静态分诊 → 圈出函数或 shellcode 区间 → 只对「反调试或 bare-metal 那一段」上 Unicorn → dump 寄存器/内存 → 求解。

| 坑 | 症状 | 修法 |
|---|---|---|
| `timeout` 与 `count` 同时非零 | 谁先到谁停，行为不确定 | 只留一个，另一个设 0 |
| 栈映射在 `0x0` 附近 | 空指针解引用静默成功 | 栈放 `0x7fff0000` |
| Thumb 没置 LSB | `UC_ERR_INSN_INVALID` | `CODE_ADDR \| 1` + `UC_MODE_THUMB` |
| MIPS 字节序不匹配 | 第一条指令就 invalid | `UC_MODE_LITTLE_ENDIAN` / `BIG_ENDIAN` 对上二进制 |
| 混合模式漏拷 EFLAGS/XMM | 标志位是旧的、SSE 寄存器为 0 | 拷 `RFLAGS & 0xffffffff` + `XMM0-15` |
| `mem_map` 不对齐 | `UC_ERR_ARG` | 地址和长度都用 `0x1000` 的倍数 |
| MMIO 写触发 `WRITE_UNMAPPED` | 外设写就崩 | 先 `mem_map` 那一页，再上 hook |
| SMC 后还在跑旧字节 | 解出来的代码不生效 | `ctl_remove_cache(start, end)` |
| hook 里写寄存器又触发 hook | 无限递归 | 加守卫标志，或用 `MEM_READ_AFTER` |

## 1. 骨架

```python
from unicorn import *
from unicorn.x86_const import *

CODE_ADDR, STACK_ADDR, STACK_SIZE = 0x400000, 0x7fff0000, 2*1024*1024
CODE = open("shellcode.bin", "rb").read()

mu = Uc(UC_ARCH_X86, UC_MODE_64)
mu.mem_map(CODE_ADDR, 2*1024*1024)
mu.mem_write(CODE_ADDR, CODE)
mu.mem_map(STACK_ADDR, STACK_SIZE)
mu.reg_write(UC_X86_REG_RSP, STACK_ADDR + STACK_SIZE - 0x100)

try:
    mu.emu_start(CODE_ADDR, CODE_ADDR + len(CODE), timeout=2*1000000, count=10000)
except UcError as e:
    print(f"UcError {e} errno={e.errno} at 0x{mu.reg_read(UC_X86_REG_RIP):x}")
```

`emu_start(begin, until, timeout, count)`：`timeout` 单位是**微秒**，`count` 是指令条数上限。到达 `until`、hook 里调 `emu_stop()`、或取指落空都会结束。

**栈一定要离开 `0x0`**，否则空指针解引用会静默成功，把「程序本来该崩」的路径也跑过去。

## 2. Hook 手册

签名统一是 `hook(mu, address, size, user_data)`，`INTR`/`INSN` 例外。

```python
# CODE / BLOCK：只要覆盖率就用 BLOCK，便宜一个数量级
mu.hook_add(UC_HOOK_BLOCK, hook_block)
mu.hook_add(UC_HOOK_CODE, hook_code, begin=0x401000, end=0x401200)  # 区间过滤

# 内存读写
def hook_mem_write(mu, access, address, size, value, user_data):
    print(f"WRITE 0x{address:x} size={size} value=0x{value:x}")
mu.hook_add(UC_HOOK_MEM_WRITE, hook_mem_write)

# 未映射 / MMIO：返回 True 表示「已处理，继续跑」，False 表示按错误抛出
def hook_invalid(mu, access, address, size, value, user_data):
    return False
mu.hook_add(UC_HOOK_MEM_INVALID, hook_invalid)

# 中断与 syscall
mu.hook_add(UC_HOOK_INTR, hook_intr)
mu.hook_add(UC_HOOK_INSN, hook_syscall, None, 1, 0, UC_X86_INS_SYSCALL)
```

`UC_HOOK_MEM_INVALID` 覆盖 `READ/WRITE/FETCH_UNMAPPED` 与三种 `_PROT`，需要区分时看 `access`。

`begin`/`end` 的作用不只是过滤——**它把判断挪进了 C（TCG）层**，不加的话每条指令都要陷进 Python。

## 3. 架构与模式

### 3.1 Thumb

```python
mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
mu.emu_start(CODE_ADDR | 1, CODE_ADDR + len(CODE))   # LSB 必须置 1
```

不置 LSB 时 Thumb 编码会被当 ARM 解，直接 `UC_ERR_INSN_INVALID`。

### 3.2 ARM A32 / MIPS

```python
mu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
mu = Uc(UC_ARCH_MIPS, UC_MODE_32 + UC_MODE_LITTLE_ENDIAN)   # 或 + UC_MODE_BIG_ENDIAN
```

**字节序错了第一条指令就 invalid**，这是 MIPS 最常见的翻车点。Capstone 侧要同步 `CS_MODE_MIPS32` 加同样的 endian 标志。

### 3.3 64→32 混合模式

`retf`/`retfq` 切模式，**Unicorn 不能中途切**：停下、按目标模式新建 `Uc`、拷状态、续跑。

- `retf` 弹 `EIP(4) + CS(2)` = 6 字节
- `retfq` 弹 `RIP(8) + CS(8)` = 16 字节

```python
mu32 = Uc(UC_ARCH_X86, UC_MODE_32)
mu32.mem_map(0x400000, 0x20000); mu32.mem_write(0x400000, mu64.mem_read(0x400000, 0x20000))
for r32, r64 in reg_map.items():
    mu32.reg_write(r32, mu64.reg_read(r64) & 0xffffffff)
mu32.reg_write(UC_X86_REG_EFLAGS, mu64.reg_read(UC_X86_REG_RFLAGS) & 0xffffffff)
for xr in (UC_X86_REG_XMM0, UC_X86_REG_XMM1, UC_X86_REG_XMM2, UC_X86_REG_XMM3,
           UC_X86_REG_XMM4, UC_X86_REG_XMM5, UC_X86_REG_XMM6, UC_X86_REG_XMM7,
           UC_X86_REG_XMM8, UC_X86_REG_XMM9, UC_X86_REG_XMM10, UC_X86_REG_XMM11,
           UC_X86_REG_XMM12, UC_X86_REG_XMM13, UC_X86_REG_XMM14, UC_X86_REG_XMM15):
    mu32.reg_write(xr, mu64.reg_read(xr))

eip = int.from_bytes(mu32.mem_read(mu32.reg_read(UC_X86_REG_ESP), 4), "little")
mu32.reg_write(UC_X86_REG_EIP, eip)
mu32.reg_write(UC_X86_REG_ESP, mu32.reg_read(UC_X86_REG_ESP) + 6)
```

三个常见漏项：**忘 `EFLAGS`、没截断到 32 位、SSE blob 漏 XMM**。

## 4. 固件 MMIO

固件会摸固定外设地址（UART `0x40000000`、GPIO、定时器），这些页默认未映射。

```python
MMIO_BASE = 0x40000000
mu.mem_map(MMIO_BASE, 0x1000)
mu.mem_write(MMIO_BASE, b"\x01\x00\x00\x00")   # status = ready

mmio = {0x40000000: 0x01, 0x40000004: 0x41}

def hook_mmio_write(mu, access, address, size, value, user_data):
    if MMIO_BASE <= address < MMIO_BASE + 0x1000:
        mmio[address & ~0x3] = value & 0xffffffff
    return True
```

2.x 也可以用 `mmio_map(base, size, read_cb, None, write_cb, None)`。

**两种让朴素写法失效的模型**：

1. **读清零（status clear on read）**——`SR` 读一次就归零。不建模的话轮询永远退不出去。
2. **自由计数（free-running counter）**——`CNT` 每次读自增。不建模的话定时永远不触发。

```python
def mmio_read(addr, size):
    if off == 0x10:                 # SR
        val = timer["SR"]; timer["SR"] = 0; return val
    if off == 0x24:                 # CNT
        timer["CNT"] = (timer["CNT"] + 1) & 0xffffffff; return timer["CNT"]
```

写回方式：hook 里 `mu.mem_write(address, val.to_bytes(size, "little"))` 再 `return True`，让 load 看到值。

### 4.1 定时器中断注入

```python
def hook_block_timer(mu, address, size, user_data):
    tick[0] += 1
    if tick[0] % 1000 == 0:
        timer["SR"] |= 1              # UIF
        nvic["pending"] |= 1 << 28    # TIM2 IRQn = 28
```

**CTF 里通常只需置标志让固件自己轮询**，不必真的劫持 PC 到 ISR。真要向量中断，就 `context_save()` → 写 `PC`/`LR`/`xPSR` → 模拟 ISR → `context_restore()`。

## 5. 快照与爆破

每个候选重建 `Uc` 比快照恢复慢 10-100 倍。

```python
mu.emu_start(CODE_ADDR, CODE_ADDR + 0x40)   # 预热到检查点
snap = mu.context_save()                    # 寄存器 + MSR + 隐藏状态
mem_snap = mu.mem_read(DATA_ADDR, 0x10000)  # 显式 RAM 快照
```

**`context_save()` 不包含 RAM**——它只给寄存器、标志、段基址、MSR。任何目标会改的内存区（输入缓冲区、堆、SMC 代码页）都得自己 `mem_read`/`mem_write`。`context_update(ctx)` 是原地写回，用于增量打点。

```python
def try_byte(prefix, cand):
    mu.context_restore(base_ctx)
    mu.mem_write(DATA_ADDR, base_mem)
    mu.mem_write(DATA_ADDR, prefix + bytes([cand]))
    mu.reg_write(UC_X86_REG_RDI, DATA_ADDR)
    try:
        mu.emu_start(mu.reg_read(UC_X86_REG_RIP), CODE_ADDR + len(CODE),
                     timeout=500000, count=5000)
    except UcError:
        return False
    return mu.reg_read(UC_X86_REG_RAX) == 0    # 前缀对时返回 0
```

**每确认一个字节就重新快照一次**，这样下一个位置从新状态出发，不必重跑前缀。

多进程隔离：`fork()` 后每个子进程从保存的 blob 自己建 `Uc`。Unicorn 状态跨线程不安全，跨 `fork()` 安全。

## 6. 覆盖率与超时

```python
blocks = {}
mu.hook_add(UC_HOOK_BLOCK, lambda mu, a, s, u: blocks.__setitem__(a, blocks.get(a, 0) + 1),
            begin=func_start, end=func_end)

edges, prev = set(), None
def hook_edge(mu, address, size, user_data):
    global prev
    if prev is not None: edges.add((prev, address))
    prev = address
```

| 设置 | 效果 | 用在哪 |
|---|---|---|
| `timeout=2e6, count=0` | 墙钟上限（微秒） | 轮询循环、固件死循环 |
| `timeout=0, count=20000` | 指令上限 | 确定性函数、求解 oracle |
| 都是 0 | 跑到 `until`/`emu_stop`/出错 | 短 shellcode、终点已知 |
| 都非零 | 谁先到谁停 | 别用，除非你就是要这个竞态 |

`count` 设成「实测条数 × 2」（先用计数 hook 量一次再硬编码）。固件优先用 `timeout`（2-3M 微秒）配一个「空闲就 `emu_stop`」的 BLOCK hook。

### `UC_ERR` 对照

| errno | 常量 | 原因 | 修 |
|---|---|---|---|
| 6 | `UC_ERR_READ_UNMAPPED` | 读未映射 | `mem_map` 或 `MEM_INVALID` hook |
| 7 | `UC_ERR_WRITE_UNMAPPED` | 写未映射 | `mem_map`，或 MMIO hook 返回 `True` |
| 8 | `UC_ERR_FETCH_UNMAPPED` | 跳到未映射 | 映射目标，或 hook FETCH |
| 10 | `UC_ERR_INSN_INVALID` | 坏 opcode / 模式错 | 查 `UC_MODE_THUMB` + `ADDR\|1`、字节序 |
| 11 | `UC_ERR_MAP` | 映射重叠 | 先 `mem_unmap` |
| 12/13/14 | `WRITE/READ/FETCH_PROT` | 权限不足 | `mem_protect` 到 `UC_PROT_ALL` |
| 15 | `UC_ERR_ARG` | `mem_map` 不对齐 | 地址/长度对齐 `0x1000` |
| 0 | `UC_ERR_OK` | 正常停在 `until` | 不是错误，核对 `RIP` 是否到终点 |

日志里**同时打 `errno` 和 `RIP`/`PC`**，只看 errno 定位不到哪条指令。

## 7. 自改代码与多线程

### 7.1 SMC

```python
def hook_mem_write_smc(mu, access, address, size, value, user_data):
    if code_start <= address < code_end:
        mu.ctl_remove_cache(address, address + size)   # 或 ctl_flush_tb()
```

Unicorn 缓存翻译块（TB），**不 flush 就会继续跑旧字节**。等价于 QEMU 的 `qemu_tb_flush`。

### 7.2 多线程

Unicorn 单线程，没有并发 `emu_start`。用轮转模拟：

```python
for tick in range(100):
    for ctx in (thread1_ctx, thread2_ctx):
        mu.context_restore(ctx)
        try:
            mu.emu_start(mu.reg_read(UC_X86_REG_RIP), 0, timeout=100000, count=100)
        except UcError:
            pass
        mu.context_update(ctx)     # 原地写回，下一轮接着跑
```

共享内存在同一个 `Uc` 里天然共享，私有寄存器和栈靠 context 区分。遇到 `lock cmpxchg` / `ldrex`/`strex` 就在 hook 里模拟预留状态。

## 8. 当 GDB 后端

```python
breakpoints = {0x401020, 0x401050}
def hook_bp(mu, address, size, user_data):
    if address in breakpoints:
        mu.emu_stop()      # 交回给 stub
mu.hook_add(UC_HOOK_CODE, hook_bp)
```

寄存器同步按 GDB 顺序打包（`RAX RBX RCX RDX RSI RDI RBP RSP R8-R15 RIP EFLAGS`，各 `<Q`）。单步用 `count=1` 再读新 `RIP`。反汇编用 Capstone，`vmmap` 用 `mu.mem_regions()`。

**没有 `ptrace`，反调试天然被绕过。** 需要连反调试 syscall 也吞掉时：

```python
def hook_syscall_ptrace(mu, user_data):
    if mu.reg_read(UC_X86_REG_RAX) == 101:      # ptrace on x86_64
        mu.reg_write(UC_X86_REG_RAX, 0)
        mu.reg_write(UC_X86_REG_RIP, mu.reg_read(UC_X86_REG_RIP) + 2)  # 跳过 0f 05
```

## 9. 性能

| Hook | 单次开销 | 说明 |
|---|---|---|
| `UC_HOOK_CODE` | 5-15 us | Python 回调 + 读寄存器 |
| `UC_HOOK_BLOCK` | 5-15 us | 单价相同，命中少 10-30 倍 |
| `UC_HOOK_MEM_READ/WRITE` | 10-20 us | 还额外带 `mem_read`/`mem_write` |
| 无 hook（原生 TCG） | ~0.02 us/条 | 快 2-3 个数量级 |

100 万条指令：CODE hook 5-15 秒，BLOCK hook（5 万块）0.3-0.8 秒，无 hook 0.02 秒。

```python
slow = bytes(mu.mem_read(addr + i, 1)[0] for i in range(256))   # 256 次跨界
fast = mu.mem_read(addr, 256)                                   # 1 次
```

**批量 `mem_read`/`mem_write` + `reg_read_batch`**，爆破时只快照输入缓冲区而不是整个地址空间。映射也合并：相邻区域并成一个 `mem_map`，别搞 1 字节映射（权限检查按页算）。

## 10. 轨迹求逆

二进制只做 `add/sub/xor/rol/ror` 且无内存副作用时：**倒着走轨迹、把每个操作换成逆操作、用 Keystone 汇编回去、Unicorn 跑一遍**。

```python
inverse = {"add": "sub", "sub": "add", "rol": "ror", "ror": "rol", "xor": "xor"}
inverted = [(inverse.get(m, m), op) for m, op in reversed(transforms)]
encoding, _ = Ks(KS_ARCH_X86, KS_MODE_64).asm("\n".join(f"{m} {op}" for m, op in inverted))
```

跳过所有跳转/调用（`jmp`/`je`/`jne`/`call`/`ret`/`jbe`/`ja`/`jb`），只留算术。然后 `reg_write(RAX, known_output)` → 跑 → 读回输入。

前缀相关的校验器用 §5 的 `try_byte`。

## 11. 脱壳

多级 loader 原地 XOR 解码下一阶段。**别逆密码，跑到循环结束直接 dump 缓冲区**。

```python
DECODE_END = CODE_ADDR + 0x120      # 反汇编找到循环结束地址
mu.emu_start(CODE_ADDR, DECODE_END, timeout=2*1000000, count=10000)
decoded = mu.mem_read(DATA_ADDR, 0x1000)
if decoded[:2] == b"MZ":        open("stage2.bin","wb").write(decoded)
elif decoded[:4] == b"\x7fELF": open("stage2.bin","wb").write(decoded)
```

`DECODE_END` 猜错是唯一失败原因——dump 出来是垃圾就往前/往后挪。

## 12. 自定义 VM

Unicorn 当 CPU，用 CODE hook 读 VM 自己的取指指针做派发：

```python
def hook_dispatch(mu, address, size, user_data):
    pc = mu.reg_read(UC_X86_REG_RSI)
    print(f"op=0x{mu.mem_read(pc,1)[0]:02x} pc=0x{pc:x} RAX=0x{mu.reg_read(UC_X86_REG_RAX):x}")
```

比「逆 opcode 表 + 写反汇编器」快得多，尤其 VM 只做校验时。要 lift 到 LLVM IR 再 `opt -O3` 化简见 [[binary/逆向工程/高级工具与去混淆.md|保护与去混淆]]。

## 13. 引擎选型

| 引擎 | 执行方式 | OS/syscall | 架构 | 符号 | 快照 | 适合 |
|---|---|---|---|---|---|---|
| **Unicorn** | 具体，TCG | 无（裸 CPU） | x86/ARM/MIPS/RISCV/M68K/SPARC/S390X | 无 | `context_save/restore` + `mem_read` | shellcode、bare-metal、反调试切片、VM 派发 |
| **Qiling** | 具体，Unicorn+OS | 有（rootfs 提供 syscall/FS/注册表） | 同上 + OS 层 | 无（但可 Python hook） | `ql.save/restore` | 带 libc 的异构 ELF、syscall 密集、`ptrace`/socket |
| **QEMU** | 具体，TCG/系统 | 完整系统或 user-mode | 同上 + 全外设 | 无 | VM snapshot | 完整 Linux、内核驱动、`-machine` 硬件 MMIO |
| **angr** | 符号（VEX） | SimProcedures | x86/ARM/MIPS | 有（Claripy/Z3） | `state.copy()` | 校验器、路径爆炸 |
| **Triton** | 具体 + 符号（DSE） | Pin/DBI | x86/x86_64/AArch64 | 有（Ast） | 单路径轨迹 | 线性去混淆、污点、单路径求解 |

决策：裸 blob/固件/无 OS → Unicorn；要 `open`/`socket`/`ptrace` → Qiling；整个 OS/内核 → QEMU；要「解出输入」而非爆破 → angr/Triton。

**Unicorn + Capstone + Keystone 是 angr 的补充而不是替代**：具体轨迹能喂给符号执行做切片，反过来符号执行给出的候选可以用 Unicorn 快速验证。

## 14. 纪律

- **先问「有没有 OS」**。有 syscall 语义就上 Qiling，硬用 Unicorn 会被迫手写一堆 syscall 模拟，成本远超收益。
- **栈离开 `0x0`**。这一条不做，你会得到「程序跑通了但结果不对」的假象——空指针解引用静默成功了。
- **`context_save` 不含内存**。爆破结果莫名不对，九成是忘了 `mem_read`/`mem_write` 恢复 RAM。
- **SMC 必 flush**。`ctl_remove_cache` 不做，模拟器会一直执行解包前的旧字节。
- **hook 加 `begin`/`end`**。这不是优化选项，是把过滤挪进 C 层的开关，不加就是几百倍差距。
- **报的是「哪条指令、哪个 errno」**，不是「Unicorn 不工作」。errno 10 是模式/字节序，errno 8 是跳飞了，errno 15 是对齐——每条都指向不同的错。
