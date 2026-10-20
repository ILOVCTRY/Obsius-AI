---
name: ctf-pwn
description: 二进制漏洞利用：已有可交互的脆弱目标，要把内存破坏或底层原语变成代码执行或提权时使用。覆盖栈溢出、格式串、堆漏洞、ROP/ret2libc、shellcode、内核利用、seccomp 绕过、沙箱逃逸、Windows/Linux 利用链。不适用：卡点在「看不懂二进制在干什么」时先走逆向；纯 Web、磁盘/流量取证、独立密码学题目也不走本包。
license: MIT
compatibility: 需要可访问文件系统的 agent（Claude Code 或类似），具备 bash、Python 3，以及联网安装工具的能力。
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# 二进制漏洞利用

## 0. 判据速查

拿到目标先看 `checksec`，保护组合直接决定手法，不要凭经验猜。

| 保护状态 | 直接能做什么 | 去哪节 |
|---|---|---|
| No PIE + Partial RELRO | GOT 覆盖（最省事，地址全固定） | §3 |
| Full RELRO | GOT 只读 → 打 `__free_hook`/`__malloc_hook`（glibc<2.34）或返回地址 | §3、§6 |
| 有 Canary | 先泄漏，或干脆走堆 | §3、§6 |
| NX 开启 | 不能跑栈上 shellcode → ROP / ret2win | §4 |
| 栈溢出偏移已知 + 有 `pop rdi` | ret2win，一行打完 | §4 |
| 输入直接进 `printf` | 格式串，先泄后写 | §5 |
| 有 `malloc`/`free`/`edit` 菜单 | 堆，按 glibc 版本选手法 | §6 |
| 有 `seccomp` | 不能 `execve` → ORW / 换 syscall | §7 |
| 目标是内核模块 | 先定原语类型 | §8 |
| 是自定义 VM / 受限 shell | 逃逸，不是 pwn | §9 |

---

## 1. 环境

```bash
pip install "pwntools==4.15.0" "ROPgadget==7.7" "ropper==1.13.13"
# 或 bash scripts/install_ctf_tools.sh python

apt install gdb binutils strace ltrace qemu-system-x86
apt install qemu-user qemu-user-static gdb-multiarch binutils-multiarch \
            libc6-armhf-cross libc6-arm64-cross libc6-mips-cross   # 跨架构
brew install gdb binutils qemu                                    # macOS
gem install one_gadget seccomp-tools
```

pwndbg：Linux 用 GitHub，macOS 用 `brew install pwndbg/tap/pwndbg-gdb`。`checksec` 随 pwntools 一起装。

---

## 2. 三分钟侦察

```bash
checksec --file=binary
file binary
readelf -h binary

ROPgadget --binary binary | grep "pop rdi"
ropper -f binary --search "pop rdi"
one_gadget /lib/x86_64-linux-gnu/libc.so.6

gdb -q binary -ex 'start' -ex 'checksec'

python3 -c "from pwn import *; print(cyclic(200))"          # 找偏移
python3 -c "from pwn import *; print(cyclic_find(0x61616168))"

./libc-database/find puts <泄漏地址末 3 个半字节>            # 认 libc
```

**源码红旗**：`pthread` → 竞态；`usleep`/`sleep` → 时序窗口；多线程共享全局变量 → TOCTOU。

**竞态速测**：
```bash
bash -c '{ echo "cmd1"; echo "cmd2"; sleep 1; } | nc host port'
```

---

## 3. 保护对应的打法

| 保护 | 状态 | 含义 |
|---|---|---|
| PIE | 关 | 所有地址固定，直接覆盖 |
| RELRO | Partial | GOT 可写，GOT 覆盖可行 |
| RELRO | Full | GOT 只读，改打 hooks/vtable/返回地址 |
| NX | 开 | 栈堆不可执行，走 ROP 或 ret2win |
| Canary | 有 | 栈破坏会被发现，先泄漏或改用堆 |

---

## 4. 栈溢出

1. `cyclic 200` 找偏移，`cyclic -l <值>` 定位
2. `checksec` 看保护
3. No PIE + No Canary = 直接 ROP
4. Canary 用格式串泄漏，或部分覆盖
5. forking 服务器可以逐字节爆破 canary（最多 7×256 次）

**ret2win + magic**：溢出 → `ret`（对齐）→ `pop rdi; ret` → magic → win()。

**三个高频坑**：
- **栈对齐**：`movaps` 上 SIGSEGV = 多塞一个 `ret` gadget
- **偏移算错**：缓冲区在 `rbp - N`，返回地址在 `rbp + 8`，总偏移 = N + 8
- **输入过滤**：断言 payload 里不含 `memmem()` 的黑名单串

**解析器栈溢出（未校验 `memcpy` 长度）**：自定义文件解析器（PCAP/图片/归档）用固定栈缓冲区但输入长度可超。`memcpy` 在长度校验前就拷贝，溢出保存寄存器与返回地址。必须**恢复 callee-saved 寄存器**（`rbx` 指向可读内存如 BSS、循环计数改成退出值），再 `ret` + win。详见 [[binary/漏洞利用/overflow-basics.md|栈与基础溢出]]。

**结构体指针覆盖（堆菜单题）**：create/modify/delete 的结构体含数据缓冲区 + 指针。把 name 溢出到指针字段填 GOT 地址，再用 modify 写 win 地址。

**有符号整数绕过**：`scanf("%d")` 不检查符号；负数量 × 单价 = 负总价，直接绕过余额检查。

**Canary 感知的部分溢出**：溢出缓冲区与 canary 之间的 `valid` 标志。用 `./` 当无操作路径填充精确长度。

**全局缓冲区溢出（CSV 注入）**：相邻全局变量；多塞几个 CSV 分隔符就能改掉文件名指针。

**隐藏 gadget**：`ROPgadget` 找不到时用 pwntools `ROP()`，它能从 CMP 立即数里挖出 gadget。

---

## 5. 格式串

输入直接进 `printf` 就是格式串。两阶段：先泄漏（libc/canary/栈），再写（GOT/hook）。

**经输入变换的格式串（ROT13）**：输入到达 `printf` 前被 ROT13/Caesar 变换，就把 payload 用逆变换预编码，让它到达时还原。

**关键判据**：能写几字节取决于 `%n` 家族（`%hhn` 1 字节、`%hn` 2 字节、`%n` 4 字节）；能泄什么取决于栈上恰好有什么。

详见 [[binary/漏洞利用/format-string.md|格式串]]。

---

## 6. 堆

按 glibc 版本选手法，**版本判断先于漏洞分析**：

| 版本 | 可用手法 |
|---|---|
| < 2.26 | fastbin dup、unsafe unlink、House of Force/Spirit |
| 2.26+ | tcache（无校验，最快） |
| 2.29+ | tcache key 校验 → 需要 double-free 变体 |
| 2.31+ | 移除 `__malloc_hook`/`__free_hook` → 转 FSOP |
| 2.32+ | safe-linking（指针混淆） |

**常用落点**：
- `__free_hook` / `__malloc_hook`（glibc < 2.34）
- `_IO_FILE` 结构体 + vtable（FSOP）
- `_IO_list_all`、`stdout` 的 `_IO_buf_base`

详见 [[binary/漏洞利用/heap-techniques.md|堆利用]] 与 [[binary/漏洞利用/heap-fsop.md|FILE 结构体利用]]。

---

## 7. ROP

**基础两阶段 ret2libc**：`puts@PLT(puts@GOT)` 泄 libc → 返回 vuln → 第二阶段 `system("/bin/sh")`。

**DynELF 自动识别 libc**：`pwntools.DynELF(leak_func, pointer_in_libc)` 在不知道 libc 版本时远程解析符号。

**小缓冲区里的受限 shellcode**：缓冲区太小就先用 `read()` 存根（< 20 字节）拉完整二阶段 shellcode。

**裸 syscall ROP**：`system()`/`execve()` 因 CET/IBT 崩掉时，改用 `pop rax; ret` + `syscall; ret`（都从 libc 取）。

**ret2csu**：`__libc_csu_init` 的 gadget 能控制 `rdx`/`rsi`/`edi` 并调用任意 GOT 函数——不需要 libc gadget 就能做三参数调用。

**坏字符 XOR 绕过**：把 payload 与 key XOR 后写进 `.data`，再用 ROP gadget 原地 XOR 回来。避开 null、换行等被过滤的字节。

**冷门 gadget（BEXTR/XLAT/STOSB/PEXT）**：没有标准 `mov` 写 gadget 时，串这些冷门指令做逐字节写。

**栈枢轴（`xchg rax, esp`）**：溢出空间不够放完整链时，把栈指针换到可控堆/缓冲区。需要先 `pop rax; ret` 装枢轴地址。

**rdx 控制**：`puts()` 之后 rdx 被清成 1。用 libc 的 `pop rdx; pop rbx; ret`，或重入二进制的 read 设置 + 栈枢轴。

**Canary XOR 尾声当 rdx 清零 gadget**：没有 `pop rdx; ret` 时，跳进 canary 校验尾声 `xor rdx, fs:28h`——canary 完好时它正好把 RDX 清零。

**`stub_execveat` 替代 execve**：没有 `pop rax; ret` 时用 syscall 322（0x142），**正好发 0x142 字节**让 `read()` 的返回值把 rax 设成 322。

**拿 shell 后**：`sleep(1)` 再 `sendline(b'cat /flag*')`——不等会读到回显前的空缓冲。

详见 [[binary/漏洞利用/rop-and-shellcode.md|ROP 与 shellcode]]、[[binary/漏洞利用/rop-advanced.md|ROP 进阶]]。

---

## 8. seccomp

**判据**：`seccomp-tools dump ./binary` 看允许哪些 syscall。

- 禁 `execve` 但允许 `open`/`read`/`write` → **ORW** shellcode
- 只禁 `open` → 试 `openat`（syscall 437）、`openat2`
- 禁 x86-64 → 切 x32 ABI（syscall 号 | `0x40000000`）或 `retf` 切 32 位
- 禁 `read`/`write` → `sendfile`、`splice`、`pread64`

详见 [[binary/漏洞利用/advanced.md|语义误用与 seccomp]]、[[binary/漏洞利用/rop-advanced.md|ROP 进阶]]。

---

## 9. 内核

先定原语：AAW / UAF / 竞态 / 越界。有 AAW 就改 `modprobe_path`，别硬做 kROP。

- 基础（环境、堆喷、栈溢出、提权、`modprobe_path`、`core_pattern`）：[[binary/漏洞利用/kernel.md|内核基础]]
- 进阶（`tty_struct` kROP、uffd、SLUB、跨缓存、PTE 重叠）：[[binary/漏洞利用/kernel-techniques.md|内核进阶]]
- 保护绕过（KASLR/FGKASLR/KPTI/SMEP/SMAP）：[[binary/漏洞利用/kernel-bypass.md|保护绕过]]

---

## 10. 沙箱与模拟器逃逸

**判据**：目标不是「有内存破坏的原生程序」，而是「限制你能调什么」的容器。

- 自定义 VM、FUSE/CUSE 设备、busybox 受限 shell
- `process_vm_readv` 沙箱绕过、命名管道文件大小绕过
- CPU 模拟器 print opcode 的 Python `eval` 注入：模拟器用 `eval('"' + buf + '"')` 处理转义序列时，用 ADD 指令在模拟内存里拼出 `"+__import__("os").system("cmd")#`，跳出字符串执行 Python

详见 [[binary/漏洞利用/sandbox-escape.md|沙箱逃逸]]、[[misc/沙箱逃逸/pyjails.md|pyjail]]。

---

## 11. 何时该换方向

- **看不懂二进制在干什么** → 先走 [[binary/逆向工程/index.md|逆向]]，别硬爆
- **本质是受限 shell / 编码题 / 沙箱语言** → [[misc/沙箱逃逸/pyjails.md|misc 轨]]
- **利用链依赖 Web 端点、会话、上传原语** → Web 轨
- **必须先破密码学原语** → crypto 轨

---

## 12. 分册导航

| 分册 | 内容 |
|---|---|
| [[binary/漏洞利用/overflow-basics.md\|栈与基础溢出]] | 栈/全局溢出、ret2win、canary 绕过、结构体指针覆盖、有符号整数、隐藏 gadget、解析器溢出 |
| [[binary/漏洞利用/rop-and-shellcode.md\|ROP 与 shellcode]] | ret2libc、syscall ROP、rdx 控制、ret2csu、坏字符 XOR、冷门 gadget、栈枢轴、DynELF、`stub_execveat` |
| [[binary/漏洞利用/rop-advanced.md\|ROP 进阶]] | 双栈枢轴到 BSS、SROP、seccomp 绕过、`retf` 架构切换、`.fini_array` 劫持、ret2vdso、x32 ABI、时间盲注外带 |
| [[binary/漏洞利用/format-string.md\|格式串]] | 泄漏、GOT 覆盖、盲打、canary 泄漏、`__free_hook`、`.rela.plt` 改写、`argv[0]` 覆盖、`.fini_array` 循环、`__printf_chk` 绕过、ROT13 变换 |
| [[binary/漏洞利用/heap-techniques.md\|堆利用]] | House of Apple/Einherjar/Orange/Spirit/Lore/Force、堆整形、自定义分配器（nginx/talloc）、经典 unlink、musl 堆、tcache stashing unlink |
| [[binary/漏洞利用/heap-fsop.md\|FILE 结构体]] | `_IO_FILE` 利用、fastbin stdout vtable 两段劫持、`_IO_buf_base` 空字节劫持、vtable 校验绕过、`mp_` 结构体、`realloc(ptr,0)` 当 free、引用计数回绕 |
| [[binary/漏洞利用/advanced.md\|语义误用]] | seccomp 进阶、UAF、JIT、冷门 GOT、进制转换堆重叠、`ret2dlresolve`、内核基础 |
| [[binary/漏洞利用/advanced-exploits.md\|进阶原语合集]] | VM 有符号比较、BF JIT shellcode、类型混淆、ASAN 影子内存、`io_uring` UAF、整数截断、GC 空引用、无泄漏 libc、TLS 析构劫持、Windows SEH/CFG、GF(2) 消元、单比特翻转、神经网络索引越界、Chip-8 OOB |
| [[binary/漏洞利用/sandbox-escape.md\|沙箱逃逸]] | 自定义 VM、FUSE/CUSE、busybox 受限 shell、`process_vm_readv` 绕过、命名管道、CPU 模拟器 eval 注入 |
| [[binary/漏洞利用/kernel.md\|内核基础]] | 环境搭建、QEMU 调试、堆喷结构、内核栈溢出、canary 泄漏、提权、`modprobe_path`、`core_pattern` |
| [[binary/漏洞利用/kernel-techniques.md\|内核进阶]] | `tty_struct` kROP、ioctl AAW、uffd、SLUB 内部、跨缓存 CPU 分裂、PTE 重叠、`addr_limit`、自定义 binfmt |
| [[binary/漏洞利用/kernel-bypass.md\|内核保护绕过]] | KASLR/FGKASLR（`__ksymtab`）、KPTI（swapgs trampoline）、SMEP/SMAP、GDB 调模块、initramfs/virtio-9p |
| [[binary/漏洞利用/field-notes.md\|实战笔记]] | 堆利用速查、补充笔记、常用命令 |

---

## 13. 纪律

- **`checksec` 先于一切**。保护组合决定手法，不先看就是瞎猜。
- **glibc 版本决定堆手法**。`< 2.26` / `2.26+` / `2.31+` 是完全不同的题，先 `ldd --version` 或从泄漏里认。
- **能泄就先泄**。有格式串、有 `puts`、有未初始化栈，先把 libc 拿到手，再谈写。
- **报的是保护组合不是漏洞名**。「有栈溢出」不够，要知道 PIE/RELRO/Canary/NX 才能定打法。
- **一阶段打完发现打不动，先怀疑对齐**。`movaps` 崩、`system` 崩、`one_gadget` 不中——多半是栈对齐或多塞一个 `ret` 就好。
- **`seccomp` 一定要先 dump**。不 dump 就写 ORW，可能白写一遍。
