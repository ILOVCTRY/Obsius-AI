---
title: EDR 钩子调研与指纹速查
summary: 主流 EDR/AV 用户态与内核监控点指纹表、ntdll 高频 hook 函数（按 ATT&CK 分组）、hook 表 dump 流程（IDA+windbg/pe-sieve）与 API Monitor 动态观察
phase: reverse
vuln_class: [edr, defense-evasion]
---

# EDR 钩子调研与指纹速查（hook survey）

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）。
> **仅限授权红队 / 对抗演练 / 自有产品测试，禁止用于未授权目标。**
> 汇总主流 EDR / AV 在用户态与内核态的监控点，供红队侦察阶段快速定位「该处理什么」。
> 配合技能 [edr-hook-rev](../../../tracks/redteam/skills/edr-hook-rev/SKILL.md)。

## 主流 EDR 指纹与 hook 模式速查

| 厂商 / 产品 | 用户态组件 | 内核驱动 | 主要监控面 |
|------------|-----------|---------|-----------|
| CrowdStrike Falcon | `CSFalconService.exe`, `CSAgent.sys` 注入目标进程 | `CSAgent.sys`, `CSBoot.sys` | 重内核 callback + ETW-TI；用户态 hook 较少（云查杀） |
| Microsoft Defender for Endpoint (MDE) | `MsMpEng.exe`, `MpClient.dll` | `WdFilter.sys`, `WdBoot.sys`, `WdNisDrv.sys` | AMSI + ETW-TI + ntdll inline hook + kernel callback 全面 |
| SentinelOne | `SentinelAgent.exe`, `SentinelHelperService.exe` | `SentinelMonitor.sys`, `SentinelDeviceControl.sys` | ntdll 用户态 hook 重 + 内核 callback + 自有 ETW provider |
| Elastic Defend | `elastic-endpoint.exe` | `elastic-endpoint-driver.sys` | 主要 ETW + 少量 ntdll hook，配合 Elastic Agent 上传 |
| ESET | `ekrn.exe`, `eamsi.dll` | `eamonm.sys`, `epfwwfp.sys` | 用户态 hook 非常多（NtCreateFile / NtOpenProcess 等） |
| Sophos Intercept X | `SophosFileScanner.exe`, `SophosNtpService.exe` | `SophosED.sys`, `hmpalert.sys` | ntdll hook + HMPA 内存防护 + 内核 callback |
| Kaspersky | `avp.exe`, `klif.sys` | `klif.sys`, `klhk.sys` | 重用户态 hook + KLIF 自有微过滤 + 网络过滤驱动 |
| Trend Micro Apex One | `TmListen.exe`, `TmCCSF.dll` | `tmcomm.sys`, `tmactmon.sys` | 用户态 hook + 行为监控驱动 |
| Carbon Black | `RepMgr.exe`, `RepWAV.exe` | `ParityDriver.sys` | 偏内核 callback + ETW |

### 快速指纹脚本

```powershell
$edrSigs = @{
    'CSAgent'           = 'CrowdStrike Falcon'
    'SentinelAgent'     = 'SentinelOne'
    'elastic-endpoint'  = 'Elastic Defend'
    'ekrn'              = 'ESET'
    'MsMpEng'           = 'Microsoft Defender'
    'SophosFileScanner' = 'Sophos Intercept X'
    'avp'               = 'Kaspersky'
    'TmListen'          = 'Trend Micro Apex One'
    'cb'                = 'Carbon Black'
}

Get-Process | ForEach-Object {
    foreach ($k in $edrSigs.Keys) {
        if ($_.ProcessName -match $k) {
            "[+] $($edrSigs[$k]) detected: $($_.ProcessName) (PID $($_.Id))"
        }
    }
}

Get-ChildItem 'C:\Windows\System32\drivers\*.sys' |
    Where-Object { $_.Name -match 'CSAgent|Sentinel|elastic|eam|WdFilter|Sophos|klif|tmcomm|Parity' } |
    Select-Object Name, VersionInfo
```

## 用户态 ntdll hook 重点函数（按 ATT&CK 行为分组）

| 函数 | 监控的行为 | ATT&CK |
|------|-----------|--------|
| `NtCreateThreadEx` | 远程线程注入、QueueUserAPC 注入 | T1055.002 / T1055.004 |
| `NtAllocateVirtualMemory` | shellcode 申请 RWX 内存 | T1055 |
| `NtAllocateVirtualMemoryEx` | 跨进程内存申请（Win10+ 新 API） | T1055 |
| `NtProtectVirtualMemory` | 改页面权限 RW→RX | T1055 |
| `NtWriteVirtualMemory` | 跨进程写 shellcode | T1055.012 |
| `NtMapViewOfSection` | section-based 注入（Doppelganging / Ghosting） | T1055.013 |
| `NtCreateSection` | 配合 MapViewOfSection | T1055.013 |
| `NtOpenProcess` | 打开目标进程拿 handle | T1057 |
| `NtQueueApcThread` / `NtQueueApcThreadEx` | APC 注入 | T1055.004 |
| `NtCreateProcess(Ex)` / `NtCreateUserProcess` | 创建子进程（含 PPID spoof） | T1106 |
| `NtSetContextThread` | 改线程上下文（线程劫持注入） | T1055.003 |
| `NtResumeThread` | 注入完后恢复线程 | T1055 |
| `NtQuerySystemInformation` | 枚举进程 / 驱动 / handle | T1057 / T1082 |
| `NtAdjustPrivilegesToken` | 提权获取 SeDebugPrivilege 等 | T1134 |
| `NtLoadDriver` | 加载内核驱动（BYOVD） | T1543.003 |

### 验证 hook 是否存在

```powershell
# 1. 拿磁盘干净 ntdll
copy C:\Windows\System32\ntdll.dll C:\temp\ntdll_clean.dll

# 2. windbg attach 任意进程，导出当前 ntdll 的 .text 段
# .writemem c:\temp\ntdll_live.bin ntdll!.text L?<size>

# 3. IDA/radare2 反汇编 NtAllocateVirtualMemory，正常 prologue：
#    mov r10, rcx / mov eax, <SSN> / test byte ptr [...] / syscall / ret
#    第一条变成 jmp <某地址> 即为 hook
```

## 内核 callback 监控点

| API | 注册的回调时机 | 防御方用途 |
|-----|--------------|-----------|
| `PsSetCreateProcessNotifyRoutineEx` | 进程创建 / 退出 | 拦截可疑 child process |
| `PsSetCreateThreadNotifyRoutine` | 线程创建 / 退出 | 检测远程线程注入 |
| `PsSetLoadImageNotifyRoutine` | DLL / EXE 加载到任意进程 | 模块完整性 / 未签名拦截 |
| `CmRegisterCallback(Ex)` | 注册表操作 | 持久化检测 |
| `ObRegisterCallbacks` | `OpenProcess` / `OpenThread` 句柄请求 | 防 LSASS 句柄获取 (T1003.001) |
| `MmRegisterPhysicalMemoryCallback` | 物理内存映射 | 防 DMA / 内存取证 |
| `IoRegisterFsRegistrationChange` | 文件系统注册 | minifilter 协同 |
| `KeRegisterNmiCallback` | NMI（极少 EDR 用） | 异常监控 |
| `EtwRegister`（内核侧） | 内核 ETW 上报 | 跟 ETW-TI 共生 |

windbg 枚举已注册 callback：

```text
0: kd> dx -r1 nt!PspCreateProcessNotifyRoutine
0: kd> dx -r1 nt!PspCreateThreadNotifyRoutine
0: kd> dx -r1 nt!PspLoadImageNotifyRoutine
0: kd> !object \Callback
```

或用 PChunter / DRVHV 可视化查看 callback 列表。

## 静态 dump hook 表（IDA + windbg 流程）

```text
流程 A：单进程比对
1. 找一个已被 EDR 注入用户态组件的进程（任意已存活进程）
2. windbg attach (-pn target.exe)
3. lm m ntdll  → 模块基址
4. .writemem c:\temp\ntdll_live.bin ntdll+0x0 L?<image size>
5. 复制 C:\Windows\System32\ntdll.dll 为 c:\temp\ntdll_disk.dll
6. IDA 加载两个文件，跳到 NtAllocateVirtualMemory：
   disk：标准 prologue；live：第一条 jmp <0x7FFE000000xx>
7. 跟 jmp 目标 → EDR 的 trampoline，dump 出来
8. 进 trampoline 看最终落到哪个 DLL，确认 EDR 模块名

流程 B：批量 hook 表（HookHunter 或自写脚本）
$disk = 磁盘 ntdll 字节；$live = OpenProcess+ReadProcessMemory 拿
对比 .text 段每个 export 的前 16 字节
```

## pe-sieve 自动检测

```powershell
# 基本扫描
pe-sieve64.exe /pid 1234

# 推荐组合（含 shellcode 与 hook 检测）
pe-sieve64.exe /pid 1234 /shellc 3 /modules 3 /imp 3 /data 3 /dir hooks_dump
#   /shellc N  shellcode 扫描等级 (0-3)   /modules N  模块完整性检查
#   /imp N     IAT hook 检查              /data N     数据段扫描
```

输出 `hooks_dump/<pid>.<name>/*.tag` 列出 hook 地址：

```text
modified_modules.tag 示例：
71f10000;ntdll.dll
71f1a3b0;hook;jmp_far
71f1c020;hook;jmp_near
```

可直接喂给 IDA 跳到对应 RVA 做后续分析。实战中常把 pe-sieve 编译为 lib
（`libpe-sieve`）让 implant 启动自检：发现 ntdll 被 hook 时触发 unhook 流程；
发现自己被 hook 也可能意味着在沙箱里。

## API Monitor v2 动态观察（lab）

```text
1. 管理员启动 API Monitor v2
2. API Filter 勾选：NT Native API → Memory Management / Process and Thread
3. Monitor New Process → 选 implant 测试样本
4. 观察 NtAllocateVirtualMemory 调用顺序、是否被 EDR DLL 中转
5. Modules tab 看哪些 EDR DLL 被 LoadLibrary 注入
```

## 常见 EDR 用户态 DLL 速查

| DLL | 厂商 | 备注 |
|-----|------|------|
| `umppc*.dll` | Microsoft Defender | MpClient userland |
| `mpoav.dll` | Microsoft Defender | AMSI provider |
| `aswAMSI.dll` | Avast | AMSI provider |
| `eamsi.dll` | ESET | AMSI provider |
| `IDPMServiceClient.dll` | Sophos | HMPA 注入 |
| `klsihk64.dll` | Kaspersky | 注入到目标进程 |
| `CrowdStrike.Sensor.dll` | CrowdStrike | 旧版本，新版主要靠内核 |
| `SentinelInjection64.dll` | SentinelOne | 用户态注入 |
| `TmUmEvt64.dll` | Trend Micro | 行为监控 |

确认目标 EDR 后，再决定逆向哪个 DLL 取 hook 表。

## 参考

pe-sieve: https://github.com/hasherezade/pe-sieve · HollowsHunter:
https://github.com/hasherezade/hollows_hunter · API Monitor v2:
http://www.rohitab.com/apimonitor · ired.team:
https://www.ired.team/offensive-security/defense-evasion

完成 hook 调研后，进入绕过技术选型：见
[unhook-techniques.md](unhook-techniques.md) 与
[telemetry-blinding.md](telemetry-blinding.md)。
