---
title: Unhook 与 syscall 绕过技术
summary: Peruns Fart/直接与间接 syscall/Hell's-Halo's-Tartarus Gate SSN 解析/硬件断点 Blindside/调用栈伪造的原理、实现骨架与选型对照
phase: reverse
vuln_class: [edr, defense-evasion, syscall]
---

# Unhook / 直接 / 间接 syscall 技术清单

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）。
> **仅限授权红队 / 对抗演练 / 自有产品测试，禁止用于未授权目标。**
> 汇总当前主流「绕过用户态 hook」技术，从经典 unhook 到 hardware breakpoint
> Blindside。对照 MITRE ATT&CK T1562.001 / T1027 / T1055。

## 1. Peruns Fart / Fresh Ntdll from disk

### 原理

EDR 的 hook 全部位于**当前进程内存中的 ntdll.dll**；磁盘上
`C:\Windows\System32\ntdll.dll` 是干净的。把磁盘 ntdll 重新映射进当前进程并覆盖
内存中的 `.text` 段，hook 就被擦掉。

```text
当前进程 ntdll.dll (RWX)
  ┌─────────────────────────┐
  │ .text (含 EDR hook jmp) │ ◄── 用磁盘干净 .text 覆盖
  └─────────────────────────┘
        ▲ NtMapViewOfSection(disk_ntdll)
  磁盘 C:\Windows\System32\ntdll.dll  ← 干净
```

### 实现要点

```c
// 1. CreateFileW("\\Device\\HarddiskVolumeX\\Windows\\System32\\ntdll.dll")  // 原生路径绕监控
// 2. NtCreateSection (SEC_IMAGE)
// 3. NtMapViewOfSection 到新地址
// 4. 找新地址 .text 段
// 5. NtProtectVirtualMemory 把当前 ntdll .text 改 RW
// 6. memcpy 覆盖
// 7. NtProtectVirtualMemory 还原为 RX
```

### 注意

- `NtProtectVirtualMemory` 本身可能就是 hook 的 → 链式问题：先用**直接 syscall**
  调它
- 现代 EDR 已监控对 ntdll 内存的 W 操作，需配合 ETW patch
- Peruns Fart 在 ETW-TI 下会留 `KERNEL_MODULE_LOAD`、`PROTECTVM` 事件——
  **一定要先压 ETW**

## 2. 直接 syscall（Direct Syscall）

不调 ntdll 导出函数，自己写 syscall stub：

```asm
NtAllocateVirtualMemory:
    mov r10, rcx
    mov eax, 0x18      ; SSN（每个 Windows 版本不同）
    syscall
    ret
```

`syscall` 指令直接从用户态跳到内核 SSDT，跳过任何用户态 hook。

### SysWhispers3 用法

```powershell
git clone https://github.com/klezVirus/SysWhispers3
cd SysWhispers3
python3 syswhispers.py --preset all --action edit -o syscalls
# 输出 syscalls.h / syscalls.c / syscalls.asm（MASM stub）
# VS 里把 .asm 加入项目启用 MASM，调用 Sw3NtAllocateVirtualMemory(...) 替换原 API
```

### 缺点

syscall 指令位于 implant 自己的 `.text`（非 ntdll 内）→ kernel-mode telemetry
容易看出「syscall from non-ntdll address」——这就是 indirect syscall 出现的原因。

## 3. 间接 syscall（Indirect Syscall）

syscall 指令仍来自 ntdll.dll（合法地址），SSN 和返回地址自己控制：

```text
implant 代码：
    mov r10, rcx
    mov eax, <SSN>
    jmp [<ntdll 中某个 syscall;ret gadget 的地址>]   ; syscall 不在 implant 里
```

gadget 通常就是 `Nt*` 函数末尾的 `syscall; ret` 两字节序列。kernel-mode ETW 看
到的 RIP 是 ntdll 地址，符合合法行为模式。

```powershell
python3 syswhispers.py --preset all --action edit --mode jumper -o syscalls
# --mode jumper            => indirect syscall
# --mode jumper_randomized => 随机化 jmp 目标减少签名
```

## 4. Hell's Gate / Halo's Gate / Tartarus Gate（SSN 动态解析演进）

### Hell's Gate

- 假设 ntdll 未被 hook：启动时遍历 ntdll 的 `Nt*` 导出，从前 4 字节
  `mov eax, <SSN>` 提取 SSN
- 优点：不写死 SSN，跨 Windows 版本通用
- 缺点：ntdll 已被 hook（第一字节变 jmp）时提取失败

### Halo's Gate

修复 hook 问题：发现某函数被 hook（非标准 prologue）时，**向上/向下扫描 ±N 个
函数**——利用 ntdll `Nt*` 函数 SSN 连续递增的事实，从邻居反推被 hook 函数的 SSN：

```text
如果 NtAllocateVirtualMemory 被 hook 看不到 SSN，看邻居：
  上一个未 hook 的导出 SSN = 0x17
  下一个未 hook 的导出 SSN = 0x19
  → NtAllocateVirtualMemory SSN = 0x18
```

### Tartarus Gate

进一步处理「Hook 改了 SSN 但保留 syscall 指令」的高级 hook：同时校验 SSN 与
`syscall;ret` gadget 地址。三者结合提供最稳定的 indirect syscall 基础。

参考实现：am0nsec/HellsGate（含 Halo's fallback）、SafeBreach-Labs/HalosGate-PoC、
trickster0/TartarusGate；SysWhispers3 已集成三者。

## 5. Hardware Breakpoint Blindside

### 原理

用调试寄存器 `DR0-DR3` 在 EDR hook trampoline 入口设硬件断点；VEH（Vectored
Exception Handler）在断点命中时把 RIP **直接改到 hook trampoline 后面**，跳过
EDR 检测代码，落到 ntdll 真正的 syscall 段。

### 优势

- 不需要写 ntdll 内存（无 `NtProtectVirtualMemory` 告警）
- 不需要 unhook（hook 还在，只是被绕过）
- ETW-TI 看不到内存修改

### 实现骨架

```c
// 1. AddVectoredExceptionHandler
// 2. 在每个被 hook 函数入口设 DR0..DR3（最多 4 个，配合 single-step rotate）
// 3. SetThreadContext(thread, &ctx) 写 DRx
// 4. EDR hook trampoline 触发硬件断点 → VEH 接管
// 5. VEH 把 ContextRecord->Rip 改到 ntdll 的合法 syscall;ret

LONG CALLBACK Blindside(EXCEPTION_POINTERS* ep) {
    if (ep->ExceptionRecord->ExceptionCode == EXCEPTION_SINGLE_STEP) {
        DWORD64 rip = ep->ContextRecord->Rip;
        if (rip == g_hookedNtAllocVM) {
            // SSN 已在 eax；R10 = RCX；跳到 ntdll 的 syscall;ret
            ep->ContextRecord->Rip = (DWORD64)g_syscallGadget;
            return EXCEPTION_CONTINUE_EXECUTION;
        }
    }
    return EXCEPTION_CONTINUE_SEARCH;
}
```

### 限制

- DRx 每线程独立 → 多线程要分别设
- 一些 EDR 已 hook `NtSetContextThread` / `NtGetContextThread`，先用前面的技术绕过
- Win11 22H2+ 的 HVCI / 反调试缓解可能干扰

## 6. Call Stack Spoofing

### 问题

现代 EDR 在 `NtAllocateVirtualMemory` / `NtCreateThreadEx` 等内核入口调
`RtlCaptureStackBackTrace` 拿完整调用栈上报。implant 的栈出现
**non-image-backed memory** 帧 → 高置信告警。

### 方案 A：CallStackSpoofer（William Burgess）

1. syscall 前 swap 当前线程栈 → 伪造的合法栈
2. 伪造栈帧填充 `kernel32!BaseThreadInitThunk → ntdll!RtlUserThreadStart` 全合法链
3. syscall 返回后 swap 回真实栈

### 方案 B：SilentMoonwalk

更激进，desynchronized stack：

```text
implant → 自定义 trampoline（改 RSP/RBP/栈内容）
        → syscall（RtlCaptureStackBackTrace 看到伪造栈）
        → trampoline 还原 → 继续 implant
```

关键是 unwinding：让 `RtlVirtualUnwind` 走入伪造的 `RUNTIME_FUNCTION` /
`UNWIND_INFO` 链。

### 实战 OPSEC

- call stack spoof + indirect syscall + ETW patch 是当前过 CrowdStrike /
  SentinelOne 较稳的组合
- sleep 阶段也要 spoof——单纯执行时 spoof 不够（EDR 定期采样）

## 7. 技术选型对照表

| 技术 | 对抗 | 复杂度 | 当前有效性 | ATT&CK |
|------|------|--------|------------|--------|
| Peruns Fart | 用户态 hook | 低 | 中（易被 ETW 抓） | T1562.001 |
| Direct syscall | 用户态 hook | 低 | 低-中（kernel 看 RIP 在 implant） | T1106 / T1562.001 |
| Indirect syscall (jumper) | hook + kernel RIP 检测 | 中 | 中-高 | T1106 |
| Hell's / Halo's / Tartarus | SSN 解析 | 中 | 高（基础设施） | T1027 |
| HWBP Blindside | hook + 无写操作 | 高 | 高 | T1562.001 |
| CallStackSpoofer / SilentMoonwalk | call stack telemetry | 高 | 高 | T1564 |

实战推荐链：**Halo's Gate + indirect syscall + CallStackSpoofer + ETW patch**。

## 参考

SysWhispers3: https://github.com/klezVirus/SysWhispers3 ·
HellsGate: https://github.com/am0nsec/HellsGate ·
HalosGate-PoC: https://github.com/SafeBreach-Labs/HalosGate-PoC ·
TartarusGate: https://github.com/trickster0/TartarusGate ·
CallStackSpoofer: https://github.com/WithSecureLabs/CallStackSpoofer ·
SilentMoonwalk: https://github.com/klezVirus/SilentMoonwalk ·
Blindside: https://www.cyberark.com/resources/threat-research-blog/blindside-a-new-technique-for-edr-evasion-with-hardware-breakpoints

unhook 只是绕过的一半，另一半是遥测致盲：进入
[telemetry-blinding.md](telemetry-blinding.md)。
