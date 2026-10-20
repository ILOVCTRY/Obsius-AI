---

phase: binary
vuln_class: [reverse, anti-debug, anti-vm, obfuscation, integrity-check]
title: 反分析分类学与绕过
summary: 反分析分五类，各自挡不同的分析手段——反调试挡调试器、反虚拟机挡沙箱、反 DBI 挡 hook、完整性校验挡补丁、反汇编混淆挡静态读。判据是先认出是哪一类，再选「补丁 / hook / 模拟」三条路之一。
---

# 反分析分类学与绕过

CTF 写实用法（SIGILL 切模式、SIGFPE strace 侧信道、指令轨迹求逆、无 call 函数链、父进程改写子进程）见 [[binary/逆向工程/anti-analysis-ctf.md|反分析对抗]]；静态模式识别见 [[binary/逆向工程/patterns.md|静态模式]]；运行时观测见 [[binary/逆向工程/patterns-runtime.md|运行时补丁与预言机]]；模拟执行见 [[binary/逆向工程/tools-emulation.md|模拟执行]]。

## 0. 判据速查

| 现象 | 类别 | 首选绕过 | 去哪节 |
|---|---|---|---|
| 启动即退，`ptrace` 返回 -1 | 自 ptrace | LD_PRELOAD / 补丁 | §1 |
| 挂了调试器就卡在 `waitpid` | 双 ptrace 看门狗 | 杀掉子进程再挂 | §1 |
| 读 `/proc/self/status` 的 `TracerPid` | proc 检查 | mount namespace / hook `fopen` | §2 |
| `rdtsc` / `clock_gettime` 算耗时 | 时序检测 | NOP 掉 / `faketime` | §3 |
| 装了 SIGTRAP/SIGALRM/SIGSEGV 处理器 | 信号反调试 | GDB `handle ... nostop pass` | §4 |
| 直接 `syscall` 不调 libc | syscall 级规避 | `catch syscall` + 改 rax | §5 |
| `BeingDebugged` / `NtGlobalFlag` | PEB 检查 | ScyllaHide / 手工清零 | §6 |
| `NtQueryInformationProcess` | 内核查询 | hook ntdll | §7 |
| 堆的 `Flags`/`ForceFlags` 异常 | 堆标志 | hook `GetProcessHeap` | §8 |
| 入口点之前就退 | TLS 回调 | x64dbg 断 TLS 回调 | §9 |
| 读 `Dr0`-`Dr3` | 硬件断点检测 | 改用软件断点 | §10 |
| 扫 `0xCC` 或算 CRC | 软件断点检测 | 改用硬件断点 | §11 |
| `UnhandledExceptionFilter` / `int 2Dh` | 异常反调试 | 逐个识别 | §12 |
| `NtSetInformationThread(0x11)` | 线程隐藏 | hook 忽略 class 0x11 | §13 |
| `cpuid` 的 ECX bit31 | 反虚拟机 | 补丁 cpuid 结果 | §14 |
| MAC 前缀 / 文件 / 注册表 | 反沙箱 | 配足资源 + 改指纹 | §15、§16 |
| 扫 `/proc/self/maps` 找 frida | 反 DBI | 早加载 gadget / hook `strstr` | §17 |
| `.text` 自哈希 | 完整性校验 | 硬件断点 / 模拟 | §18 |
| 恒定分支 / 重叠指令 | 反汇编混淆 | 图模式反汇编 / D-810 | §19 |
| `(a\|b)&~(a&b)` 这类表达式 | MBA | SiMBA / Arybo 化简 | §20 |

---

## 1. 自 ptrace（Linux 最常见）

```c
if (ptrace(PTRACE_TRACEME, 0, 0, 0) == -1) exit(1);   // 已被跟踪 = 有调试器
```

**四条绕过**：

```bash
# 1. LD_PRELOAD 劫持
LD_PRELOAD=./hook.so ./binary

# 2. pwntools 直接补丁
python3 -c "
from pwn import *
elf = ELF('./binary', checksec=False)
elf.asm(elf.symbols.ptrace, 'xor eax, eax; ret')
elf.save('patched')
"

# 3. GDB 抓 syscall 后改返回值
(gdb) catch syscall ptrace
(gdb) run
(gdb) set $rax = 0
(gdb) continue

# 4. 改内核参数（需 root）
echo 0 > /proc/sys/kernel/yama/ptrace_scope
```

**双 ptrace 看门狗**：

```c
pid_t child = fork();
if (child == 0) {
    ptrace(PTRACE_ATTACH, getppid(), 0, 0);   // 子进程跟踪父进程
    // 卡在 waitpid 循环里，父进程就无法被别的调试器附加
} else {
    // 父进程继续跑真逻辑
}
```

**绕过**：杀掉看门狗子进程，再附加调试器。

---

## 2. `/proc` 检查

```c
FILE *f = fopen("/proc/self/status", "r");    // 看 TracerPid: 0
readlink("/proc/self/exe", buf, sizeof(buf)); // 有些调试器会改这个链接
// /proc/self/maps 里搜 "frida"
```

**绕过**：

```bash
# 1. LD_PRELOAD 劫持 fopen/fread 伪造内容
# 2. mount namespace 隔离
unshare -m bash -c 'mount --bind /dev/null /proc/self/status && ./binary'

# 3. GDB 在 fopen 下断改文件名
(gdb) b fopen
(gdb) run
(gdb) set {char[20]} $rdi = "/dev/null"
(gdb) continue
```

---

## 3. 时序检测

```c
uint64_t start = __rdtsc();
// ... 被测代码 ...
if (__rdtsc() - start > THRESHOLD) exit(1);   // 太慢 = 有调试器
```

也有 `clock_gettime(CLOCK_MONOTONIC)`、`gettimeofday`。

**绕过**：

```bash
# 1. GDB 把 rdtsc 打成 NOP
(gdb) set {unsigned char[2]} 0x401234 = {0x90, 0x90}

# 2. faketime 固定时间源
LD_PRELOAD=/usr/lib/faketime/libfaketime.so.1 FAKETIME="2024-01-01" ./binary

# 3. Frida hook clock_gettime；Pin 工具修 TSC 读数
```

---

## 4. 信号反调试

```c
// INT3 在调试器下会被调试器吃掉，不会进处理器
signal(SIGTRAP, handler);
__asm__("int3");
// 处理器跑了 = 无调试器；调试器截住 = 有调试器

signal(SIGALRM, kill_handler);   // 分析超时就自杀
alarm(5);

signal(SIGSEGV, real_logic);     // 故意崩溃，真逻辑在处理器里
*(int*)0 = 0;
```

**绕过**：

```bash
(gdb) handle SIGTRAP nostop pass
(gdb) handle SIGALRM ignore
(gdb) handle SIGSEGV nostop pass
```

**关键**：默认 GDB 会截住所有信号，程序的处理器根本收不到。`nostop pass` 是这类题的第一步。

---

## 5. syscall 级规避

```c
// 直接 syscall，绕开 LD_PRELOAD 对 libc 的 hook
asm volatile("syscall" : "=a"(ret) : "a"(101), "D"(0), "S"(0), "d"(0), "r"(0));
// x86_64 上 101 = ptrace
```

**绕过**：必须补丁二进制或在 syscall 层拦截。

```bash
(gdb) catch syscall 101
(gdb) commands
> set $rax = 0
> continue
> end
```

---

## 6. PEB 检查（Windows）

```c
bool debugged = NtCurrentPeb()->BeingDebugged;                 // PEB + 0x2

// 有调试器时这三个标志位 = 0x70
// FLG_HEAP_ENABLE_TAIL_CHECK | FLG_HEAP_ENABLE_FREE_CHECK | FLG_HEAP_VALIDATE_PARAMETERS
DWORD flags = *(DWORD*)((BYTE*)NtCurrentPeb() + 0xBC);         // 64 位偏移
if (flags & 0x70) exit(1);
```

**绕过**：x64dbg 装 ScyllaHide 自动补 PEB 字段；手工就是 dump PEB 后把 `BeingDebugged` 与 `NtGlobalFlag` 清零。

---

## 7. NtQueryInformationProcess

```c
// ProcessDebugPort (0x7)
DWORD_PTR debugPort = 0;
NtQueryInformationProcess(GetCurrentProcess(), 7, &debugPort, sizeof(debugPort), NULL);
if (debugPort != 0) exit(1);

// ProcessDebugObjectHandle (0x1E)：返回 STATUS_SUCCESS 就是有调试器
HANDLE debugObj = NULL;
NTSTATUS st = NtQueryInformationProcess(GetCurrentProcess(), 0x1E, &debugObj, sizeof(debugObj), NULL);
if (st == 0) exit(1);

// ProcessDebugFlags (0x1F)：语义是反的，返回 0 表示有调试器
DWORD noDebug = 0;
NtQueryInformationProcess(GetCurrentProcess(), 0x1F, &noDebug, sizeof(noDebug), NULL);
if (noDebug == 0) exit(1);
```

**坑**：`0x1F` 的语义与其他两个**相反**，写 hook 时容易搞反。

**绕过**：hook `NtQueryInformationProcess` 返回假值，或用 ScyllaHide。

---

## 8. 堆标志

```c
PHEAP heap = (PHEAP)GetProcessHeap();
// 64 位下 Flags 在 0x70（正常应为 HEAP_GROWABLE = 0x2），ForceFlags 在 0x74（正常 0）
if (heap->Flags != 0x2 || heap->ForceFlags != 0) exit(1);
```

**绕过**：hook `GetProcessHeap` 返回伪造结构，或直接改堆头。

---

## 9. TLS 回调（入口点之前）

TLS 回调在 `main()` / 入口点**之前**执行。

```c
void NTAPI TlsCallback(PVOID DllHandle, DWORD Reason, PVOID Reserved) {
    if (Reason == DLL_PROCESS_ATTACH && IsDebuggerPresent())
        ExitProcess(1);        // 在 main 之前就杀掉进程
}

#pragma comment(linker, "/INCLUDE:_tls_used")
#pragma data_seg(".CRT$XLB")
PIMAGE_TLS_CALLBACK callbacks[] = { TlsCallback, NULL };
```

**识别**：IDA/Ghidra 里看 PE 的 TLS Directory → `AddressOfCallBacks`。那里的函数先于入口点跑。

**绕过**：x64dbg 里断 TLS 回调（Options → Events → TLS Callbacks），或补丁 TLS 目录项。

---

## 10. 硬件断点检测

```c
CONTEXT ctx;
ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS;
GetThreadContext(GetCurrentThread(), &ctx);
if (ctx.Dr0 || ctx.Dr1 || ctx.Dr2 || ctx.Dr3) exit(1);
```

**绕过**：改用软件断点；或 hook `GetThreadContext` 把 DR 清零。

---

## 11. 软件断点检测（扫 `0xCC`）

```c
for (int i = 0; i < code_size; i++) {
    checksum += code[i];
    if (code[i] == 0xCC) exit(1);       // INT3 = 软件断点
}
if (checksum != EXPECTED_CHECKSUM) exit(1);
```

**绕过**：改用硬件断点（不改代码）；或 hook 扫描函数。

---

## 12. 异常反调试

```c
// 有调试器时 UnhandledExceptionFilter 不会被调用
SetUnhandledExceptionFilter(handler);
RaiseException(EXCEPTION_ACCESS_VIOLATION, 0, 0, NULL);

// INT 2Dh：调试器会静默消费这个异常
__asm { int 2dh }
// 若继续执行 = 有调试器
```

**判据**：这两者的语义都反过来了——「处理器没跑」才是有调试器。

---

## 13. 线程隐藏

```c
NtSIT(GetCurrentThread(), 0x11 /* ThreadHideFromDebugger */, NULL, 0);
// 之后调试器看不到这个线程的断点与异常
```

**绕过**：hook `NtSetInformationThread` 忽略 class `0x11`，或补丁该调用。

---

## 14. CPUID 反虚拟机

```c
int regs[4];
__cpuid(regs, 1);
if (regs[2] & (1 << 31)) exit(1);       // ECX bit31 = hypervisor present

__cpuid(regs, 0x40000000);
char brand[13] = {0};
memcpy(brand, &regs[1], 12);            // "VMwareVMware" / "KVMKVMKVM" / "Microsoft Hv"
```

**绕过**：补丁 cpuid 结果，或上裸机。

---

## 15. 硬件指纹与资源

**MAC 前缀**：VMware `00:0C:29`/`00:50:56`、VirtualBox `08:00:27`、Hyper-V `00:15:5D`、Parallels `00:1C:42`、QEMU `52:54:00`。

**时序判 VM**：特权指令触发 VM exit，耗时可测。

```c
uint64_t start = __rdtsc();
__cpuid(regs, 0);                  // 强制 VM exit
if (__rdtsc() - start > 500) { /* 很可能是 VM */ }
```

**文件/注册表/服务痕迹**：
- 文件 `C:\Windows\System32\drivers\vm*.sys`、`vbox*.dll`、`VBoxService.exe`
- 注册表 `HKLM\SOFTWARE\VMware, Inc.\VMware Tools`
- 服务 `VMTools`、`VBoxService`
- 进程 `vmtoolsd.exe`、`VBoxTray.exe`、`qemu-ga.exe`
- Linux `/sys/class/dmi/id/product_name`、`dmesg | grep -i "hypervisor detected"`

**资源检查**（沙箱通常配置很低）：

```c
if (si.dwNumberOfProcessors < 2) exit(1);
if (ms.ullTotalPhys < 2ULL * 1024 * 1024 * 1024) exit(1);   // < 2GB
// 磁盘 < 60GB = 沙箱
```

**绕过**：直接给足资源（4+ CPU、8GB+ RAM、100GB+ 磁盘），比逐个 hook 省事。

---

## 16. 反 DBI

### Frida 检测的五条判据

```c
// 1. /proc/self/maps 里搜 "frida" / "gadget"
// 2. 连默认端口 27042
// 3. 检查内联 hook（函数前几字节被改成 E9/FF = JMP）
unsigned char *p = (unsigned char *)strcmp;
if (p[0] == 0xE9 || p[0] == 0xFF) exit(1);
// 4. 线程名（frida 会建 "gmain"、"gdbus"、"frida-*"）
// 5. Windows 命名管道 \\.\pipe\frida-*
```

**绕过**：

```javascript
Interceptor.attach(Module.findExportByName(null, "strstr"), {
    onEnter(args) {
        this.needle = Memory.readUtf8String(args[1]);
    },
    onLeave(retval) {
        if (this.needle && (this.needle.includes("frida") || this.needle.includes("gadget")))
            retval.replace(ptr(0));
    }
});
```

**关键**：Frida 要用 gadget 形式**尽早加载**，赶在反 DBI 检查跑之前。

### Pin / DynamoRIO

`/proc/self/maps` 里搜 `pin-`、`pinbin`、`pinatrace`、`dynamorio`、`drcov`、`drrun`；或用指令数时序（DBI 会加明显开销）。

---

## 17. 完整性校验

```c
uint32_t crc = compute_crc32(text_start, text_size);
if (crc != EXPECTED_CRC) exit(1);       // 代码被改过（断点/补丁）
```

**五条绕过**（按代价排序）：

1. **硬件断点**（不改代码，`DR0`-`DR3`）
2. **补丁比较指令**，让它恒为真
3. **hook 哈希函数**返回期望值
4. **改用模拟**（Unicorn/Qiling 不改代码）
5. **快照 + 还原**：前后 dump 内存做 diff，定位校验点

**循环自校验**：

```c
void *watchdog(void *arg) {
    while (1) {
        if (compute_crc32(text_start, text_end - text_start) != saved_crc) {
            memset(flag_buffer, 0, flag_len);    // 直接把 flag 抹掉
            exit(1);
        }
        usleep(100000);
    }
}
```

**绕过**：杀掉看门狗线程，或把它的 `usleep` 补成死循环。

---

## 18. 反汇编混淆

### 恒定谓词（opaque predicates）

```asm
mov eax, [some_memory]
imul eax, eax          ; x²
and eax, 1             ; x² mod 2 恒为 0
jnz fake_branch        ; 永不跳，但反汇编器不知道
```

**识别**：用 Z3/SMT 证明分支恒真/恒假。

### 垃圾字节 / 重叠指令

```asm
jmp real_code
db 0xE8           ; 线性反汇编器会当成 CALL 的开头
real_code:
mov eax, 1
```

**修法**：切图模式反汇编（Ghidra/IDA 都处理得好）；手工做法是 undefine 后从正确偏移重新分析。

### 跳进指令中间

```asm
eb 01          ; jmp +1，跳过下一字节
e8             ; 假的 CALL 操作码
90             ; 真代码：NOP（从 jmp 落在这里）
```

### 函数分块

函数被拆成不连续的块，用无条件跳转连接，破坏线性函数边界检测。IDA 用 "Append function tail"，Ghidra 在每个块上 "Create function"。

### 控制流平坦化（OLLVM 系）

- **虚假控制流**：用恒定谓词造假分支
- **指令替换**：`a + b` → `a - (-b)`，`a ^ b` → `(a | b) & ~(a & b)`
- **字符串加密**：运行时解密，用完清空

**工具**：D-810（IDA 插件，模式化去混淆 + MBA 化简）、GOOMBA（Ghidra，OLLVM 自动去混淆）、Miasm（符号执行去混淆）、Arybo / SiMBA（MBA 表达式化简）。

```bash
# D-810 装进 IDA plugins 目录，Edit → Plugins → D-810
# 会把 (a | b) & ~(a & b) 化简成 a ^ b，并去掉恒定谓词
```

---

## 19. MBA 化简

必背六个恒等式：

```text
(x & y) + (x | y)      == x + y
(x ^ y) + 2*(x & y)    == x + y
(x | y) - (x & ~y)     == y
~(~x & ~y)             == x | y          (De Morgan)
(x | y) & ~(x & y)     == x ^ y
```

```python
# pip install simba-simplifier
from simba import simplify_mba
print(simplify_mba("(a | b) + (a & b) - (~a & b)"))   # → a
```

---

## 20. 综合绕过

### 通用检查清单

1. **先把所有反分析点找全**——搜 `ptrace`、`IsDebuggerPresent`、`rdtsc`、`cpuid`、`NtQuery`、`GetTickCount`、`CheckRemoteDebuggerPresent`、`/proc/self`、`SIGTRAP`、`alarm`
2. **静态补丁**——pwntools 或 Ghidra 提前 NOP 掉
3. **LD_PRELOAD**（Linux）——hook libc 返回假值
4. **ScyllaHide**（Windows x64dbg）——自动补 PEB、hook NT 函数
5. **模拟执行**（Unicorn/Qiling）——没有调试器痕迹可检测
6. **内核层**——改 `/proc/sys/kernel/yama/ptrace_scope`、用 `prctl`

### 分层反调试的真实形态

```text
1. TLS 回调 → IsDebuggerPresent（main 之前）
2. main() → ptrace(TRACEME)
3. 看门狗线程 → 时序检查 + /proc 扫描
4. 代码段 → 自 CRC32 完整性
5. 信号处理器 → 真逻辑在 SIGSEGV 里
```

**打法**：**先把所有检查点找全再动手**。逐个补丁或 hook；点多到补不完就上模拟器。

---

## 21. 两个必记的案例模式

### Trap Flag 自检 + `cmovz` 补丁器（Hack.lu 2018）

```asm
check_debugger:
    pushf
    pop   edx
    and   edx, 0x100          ; 只取 Trap Flag
    test  edx, edx
    cmovz eax, ebx            ; 只在「没单步」时才覆盖 eax
    mov   [rip+target], eax
```

**判据**：GDB 单步会让 TF 置位，`cmovz` 永不触发，程序静默走错分支**且不崩**——这种「不报错的错」最难查。

**绕过**：改用硬件断点。

```gdb
(gdb) hbreak *0x56557267       # 硬件断点：不写 INT3，也不动 TF
(gdb) run
```

**关键**：`pushf; pop reg; and reg, 0x100` 是检查 TF 最干净的方式，不触发陷阱。软件断点 + `stepi` 会同时污染 TF 和指令流水。凡是读 EFLAGS/RFLAGS/DR6 的反调试，一律换成 `hbreak`。

### SIGFPE 处理器里 `mprotect` 改代码（Hack.lu 2018）

```c
void on_fpe(int sig, siginfo_t *info, void *uap) {
    ucontext_t *ctx = uap;
    void *page = (void *)((uintptr_t)ctx->uc_mcontext.gregs[REG_RIP] & ~0xfff);
    mprotect(page, 0x1000, PROT_READ | PROT_WRITE | PROT_EXEC);
    *((uint32_t *)(page + 0x42)) = 0xDEADBEEF;    // 改掉下一个检查用的常量
}
```

**判据**：`SIGFPE` 在正常路径下**根本不会触发**，所以静态分析看不到这段变异；而多数调试器会在投递前截住 SIGFPE，所以动态分析也看不到。

**绕过**：

```bash
(gdb) handle SIGFPE nostop noprint pass     # 让信号真的送到程序
(gdb) break *on_fpe
(gdb) run
```

**关键**：见到 `sys_sigaction(SIGFPE,...)` 同时又有 `mprotect` 调用，就用 `strace -e signal=SIGFPE` 跟，然后 diff 第一次 FPE 前后的 `.text` 页，把变异区域标出来。

---

## 22. 纪律

- **先分类再动手**。反调试、反虚拟机、反 DBI、完整性校验、反汇编混淆是五类不同的问题，绕法完全不同。
- **信号题第一步永远是 `handle ... nostop pass`**。默认 GDB 截住所有信号，程序处理器收不到，你会误判成「逻辑走错」。
- **不报错的错最难查**：TF 自检、`cmovz` 补丁器、`SIGFPE` 变异都属于「静默走错分支」。行为正常但答案不对，先怀疑这几类。
- **补丁不是唯一路**：点多到补不完就上模拟（Qiling 天然没有调试器痕迹），或者用硬件断点（完全不改代码）。
- **完整性校验优先用硬件断点**，因为它不改 `.text`，从根上不触发。
- **报的是检查类型不是难度**：见到 `rdtsc` 就想时序，见到 `0xCC` 扫描就想硬件断点，见到 `mprotect` + 信号处理器就想「代码会被改」。
