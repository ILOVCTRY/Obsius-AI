---

phase: binary
vuln_class: [malware, obfuscation, yara, forensics]
title: 脚本混淆、YARA 与内存取证
summary: 脚本和样本的分析靠「把执行换成打印」；内存取证靠「可执行+可写+无文件映射」三条同时成立。判据是行为，不是代码长相。
---

# 脚本混淆、YARA 与内存取证

PE / .NET 样本的配置提取见 [[binary/恶意样本/PE 与 .NET 样本分析.md|PE 与 .NET]]；C2 流量与加密协议见 [[binary/恶意样本/C2 流量与协议分析.md|C2 与协议]]；反分析分类与绕过见 [[binary/逆向工程/反分析分类学与绕过.md|反分析]] 与 [[binary/逆向工程/反分析的对抗手法.md|反分析对抗]]。

## 0. 判据速查

| 现象 | 第一动作 | 去哪节 |
|---|---|---|
| 脚本里有 `eval` / `iex` / `Execute` | 换成打印函数 | §1 |
| PowerShell 参数是 `-enc` | base64 解码 + **UTF-16LE 转码** | §3 |
| 反汇编里大量 NOP、push/pop 成对 | 过滤垃圾指令找真调用 | §4 |
| 一段 hex 字符串 | 转字节后试减 1、XOR | §5 |
| 装了 `.deb` 包 | 看 `postinst` 脚本 | §6 |
| 静态看不出行为 | strace + tcpdump + inotify 同时上 | §7 |
| 内存镜像里有可疑进程 | `malfind` | §9 |
| `strings` 里几乎没有 API 名 | API 哈希 | §10 |
| 样本一启动就退出 | 环境检查没通过 | §10.5 |
| 插件/依赖可疑 | 与官方版本 **diff** | §11 |

## 1. 通用：把执行换成打印

```text
JS      eval → console.log
PS1     IEX → Write-Output；-enc 先解码
Bash    eval → echo；curl|sh → curl 到文件再读
VBS     Execute → WScript.Echo
Python  exec → print
```

常见嵌套链：

```text
base64 解码 → gzip 解压 → 字符串反转 → 再 base64 解码
```

**逐层剥**，不要试图一次看清全部。每剥一层先 `file` 判断下层的类型。

## 2. JavaScript

```javascript
// 最有效的一招：直接替换 eval
eval = console.log;
// 然后原样运行混淆代码
```

常见解码函数：

```javascript
unescape()             // URL 解码
String.fromCharCode()  // 字符码
atob()                 // base64
```

## 3. PowerShell

```powershell
-enc / -EncodedCommand   # base64，注意是 UTF-16LE
IEX / Invoke-Expression  # 等价 eval
[System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String($encoded))
```

```bash
# -enc 的 payload 是 UTF-16LE，直接 base64 -d 会得到乱码加空字节
echo '<base64>' | base64 -d | iconv -f UTF-16LE -t UTF-8
```

**这是最高频的坑**：PowerShell 的 base64 是 UTF-16LE 编码的，不是 UTF-8。少了 `iconv` 那一步，看到的就是一堆 `\x00` 夹杂的碎片。

## 4. 垃圾代码过滤

**识别特征**：

```text
NOP sled（连续 0x90）
push/pop 成对抵消（push eax; pop eax）
恒零/恒等运算（xor eax, eax 后又加回原值）
写后未读的寄存器
跳到下一行的无条件 jmp
恒真条件（if (1==1)）
```

```python
def extract_real_calls(disassembly):
    return [i for i in disassembly
            if i.mnemonic == "call" and not is_junk_target(i.operand)]
```

思路：**不要读懂垃圾，直接把噪音滤掉，只看真调用**。混淆的目的就是让噪声淹没信号。

## 5. Hex payload

```text
1. hex 转字节
2. 试常见变换：每个字节减 1、XOR 单字节、XOR 递增索引
3. 结果用 file 判断类型
```

判据：变换后出现 `MZ` / `\x7fELF` / `PK\x03\x04` 就是对的。

## 6. Debian 包

```bash
ar -x package.deb
tar -xf control.tar.xz
# 重点看 postinst / preinst / postrm —— 安装钩子才是执行点
```

`postinst` 是安装完成后自动执行的脚本，恶意包几乎都把 payload 放这里。

## 7. 动态分析：三路同时监控

```bash
# 系统调用
strace -f -e trace=network,file -o trace.log ./sample
ltrace -f -o ltrace.log ./sample

# 网络（终端 1）
sudo tcpdump -i any -w traffic.pcap &

# DNS（终端 2）
sudo tcpdump -i any port 53 -l | tee dns.log &

# 文件系统（终端 3）
inotifywait -m -r /tmp /var/tmp --format '%T %w%f %e' --timefmt '%H:%M:%S' &

# 跑样本
timeout 60 ./sample
```

```python
import subprocess, tempfile

def run_sample(path, timeout=30):
    with tempfile.NamedTemporaryFile(suffix=".pcap", delete=False) as pcap:
        tcpdump = subprocess.Popen(
            ["sudo", "tcpdump", "-i", "any", "-w", pcap.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            r = subprocess.run(["strace", "-f", "-e", "trace=network,file", path],
                               capture_output=True, text=True, timeout=timeout)
            print("STDOUT:", r.stdout[:500])
            print("SYSCALLS:", r.stderr[:2000])
        except subprocess.TimeoutExpired:
            print(f"ran {timeout}s, killed")
        finally:
            tcpdump.terminate()
            print("PCAP:", pcap.name)
```

**运行期从内存取字符串**（静态看不到的都在这里）：

```bash
pid=$(pgrep sample)
cat /proc/$pid/mem 2>/dev/null | strings | grep -i flag
# 或指定范围 dump
gdb -p $pid -batch -ex 'dump memory dump.bin 0x400000 0x500000'
```

**必须在隔离环境跑**（VM 快照 / 容器），且三路同时开 —— 只看 syscall 会漏掉网络行为，只看流量会漏掉落地文件。

## 8. YARA

```yara
rule SuspiciousStrings {
    meta:
        description = "常见恶意指标"
    strings:
        $s1 = "cmd.exe /c" nocase
        $s2 = "powershell -enc" nocase
        $s3 = {4D 5A 90 00}                      // MZ 头
        $s4 = /https?:\/\/[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/
        $xor_loop = {31 ?? 80 ?? ?? 4? 75}       // XOR 解码循环
    condition:
        2 of ($s*) or $xor_loop
}
```

```bash
yara detect.yar suspicious.exe
yara -r detect.yar /path/to/dir
yara detect.yar memory.dmp
```

两个 CTF 高频规则：

```yara
rule Base64_PowerShell {
    strings:
        $enc = "powershell" nocase
        $b64 = /[A-Za-z0-9+\/]{50,}={0,2}/
    condition:
        $enc and $b64
}

rule XOR_Encrypted_PE {
    strings:
        $mz = {4D 5A}
    condition:
        not $mz at 0 and filesize < 1MB
        // 没有 MZ 头的 PE = 大概率被 XOR 加密
}
```

**写规则锚定字节模式，不要锚定字符串**。字符串改起来成本极低，XOR 循环的指令模式改起来成本高。

## 9. Shellcode

```bash
# 反汇编
objdump -d shellcode.bin -b binary -m i386:x86-64 -M intel

# Capstone 编程反汇编
python3 -c "
from capstone import *
md = Cs(CS_ARCH_X86, CS_MODE_64)
for i in md.disasm(open('shellcode.bin','rb').read(), 0):
    print(f'{i.address:#x}: {i.mnemonic} {i.op_str}')
"

# Windows shellcode 快速模拟
scdbg /f shellcode.bin
```

Unicorn 模拟（安全，拦截 syscall 而不真正执行）：

```python
from unicorn import *
from unicorn.x86_const import *

sc = open("shellcode.bin", "rb").read()
mu = Uc(UC_ARCH_X86, UC_MODE_64)
BASE, STACK = 0x400000, 0x7fff0000

mu.mem_map(BASE, 0x1000)
mu.mem_map(STACK - 0x1000, 0x2000)
mu.mem_write(BASE, sc)
mu.reg_write(UC_X86_REG_RSP, STACK)

def hook_syscall(mu, user_data):
    print(f"syscall: {mu.reg_read(UC_X86_REG_RAX)}")

mu.hook_add(UC_HOOK_INSN, hook_syscall, None, 1, 0, UC_X86_INS_SYSCALL)
mu.emu_start(BASE, BASE + len(sc))
```

**先找解码 stub**：绝大多数 shellcode 前面是一段短 XOR 循环，解完才是真载荷。

## 10. 内存取证

```bash
vol -f memory.dmp windows.pslist                    # 进程列表
vol -f memory.dmp windows.pstree                    # 进程树（看父子关系）
vol -f memory.dmp windows.psscan                    # 找隐藏/断链进程
vol -f memory.dmp windows.memmap --pid PID --dump   # dump 进程内存
vol -f memory.dmp windows.malfind                   # 注入代码
vol -f memory.dmp windows.netscan                   # 网络连接
vol -f memory.dmp windows.cmdline                   # 命令行参数
vol -f memory.dmp windows.dlllist --pid PID         # 进程加载的 DLL
```

```bash
# 内存上跑 YARA
vol -f memory.dmp windows.vadyarascan.VadYaraScan \
    --yara-rules 'rule t { strings: $s = "flag{" condition: $s }'
```

**`malfind` 的判据是三条同时成立**：可执行 + 可写 + 无对应磁盘文件。只看一条会大量误报。

`pstree` 看**异常的父子关系**：`cmd.exe` 拉起的 `svchost.exe`、`explorer.exe` 拉起的 `powershell.exe` 都是可疑。

## 11. 反分析

完整的反调试、反虚拟机、反 DBI 绕过策略见 [[binary/逆向工程/反分析分类学与绕过.md|反分析分类]]。

### 11.1 虚拟机检测

```text
CPUID      → leaf 1 的 ECX bit31 是 hypervisor 位
             GDB: set $ecx = $ecx & ~(1<<31)
MAC 前缀   → VMware 00:0C:29 / 00:50:56；VBox 08:00:27
             改 VM 网卡 MAC
注册表     → HKLM\SOFTWARE\VMware, Inc.\VMware Tools
             HKLM\SYSTEM\CurrentControlSet\Services\VBoxGuest
文件/进程  → vmtoolsd.exe、VBoxService.exe、qemu-ga.exe
             /sys/class/dmi/id/product_name（"VMware Virtual Platform"）
磁盘容量   → if total_disk < 60GB: exit()
CPU/内存   → if cpu_count < 2 or ram < 2GB: exit()
```

### 11.2 时序规避

```text
检测：sleep(300) 之后检查实际流逝时间 < 290 → 判定被沙箱加速 → 退出
绕过：
  1. 把 sleep 补丁成 ret：elf.asm(elf.symbols['sleep'], 'ret')
  2. hook GetTickCount / time() 返回期望值
  3. GDB 里在 sleep 后下断，手动推进
  4. 直接改二进制：sleep(300) → sleep(0)
```

**样本「什么都不做」时，八成卡在 sleep 型反沙箱检查上。** 看有没有 `NtDelayExecution`、`GetTickCount64`、`QueryPerformanceCounter`。

### 11.3 API 哈希

**判据**：`strings` 里几乎没有可读的 API 名，但程序明显在做复杂操作。

```python
def ror13_hash(name):                 # Metasploit 用的，最常见
    h = 0
    for c in name:
        h = ((h >> 13) | (h << 19)) & 0xFFFFFFFF
        h = (h + ord(c)) & 0xFFFFFFFF
    return h

def djb2_hash(name):
    h = 5381
    for c in name:
        h = ((h * 33) + ord(c)) & 0xFFFFFFFF
    return h

import binascii
def crc32_hash(name):
    return binascii.crc32(name.encode()) & 0xFFFFFFFF
```

逆向路径：

```text
1. 找哈希函数（小循环，含 XOR/rotate/add）
2. 在比较处取出常量
3. 查 hashdb.openanalysis.net，或自建 kernel32/ntdll 导出表的彩虹表
4. 典型形态：遍历 PEB → Ldr → InMemoryOrderModuleList，对每个导出名算哈希
```

### 11.4 进程注入

```text
经典注入：OpenProcess → VirtualAllocEx(RWX) → WriteProcessMemory → CreateRemoteThread
进程镂空：CreateProcess(SUSPENDED) → NtUnmapViewOfSection → 写入恶意 PE
          → SetThreadContext 改入口 → ResumeThread
APC 注入：QueueUserAPC（不新建线程，目标线程下次 alertable wait 时执行）
```

```bash
vol -f memory.dmp windows.malfind
vol -f memory.dmp windows.hollowprocesses.HollowProcesses    # VAD 与 PEB 不一致
vol -f memory.dmp windows.malfind --dump --pid <PID>
```

**现代注入变体：判据都在「内存里没有对应文件」**。

| 手法 | 核心动作 | 判据（检测侧） |
|---|---|---|
| **Manual Map / 反射加载** | 自己实现 PE 加载器，不走 `LoadLibrary` | 内存里的 PE 不在 `PEB->Ldr` 链表中；`malfind` 有 `MZ` 头但模块列表没有 |
| **Module Stomping** | 把 shellcode 写进**合法已加载 DLL** 的空隙 | 模块列表正常，但该模块的 `.text` 校验和变了；`PAGE_EXECUTE_READWRITE` 落在合法模块范围内 |
| **Thread Hijack** | 不建新线程，改已有线程的 `RIP` | 无新线程但出现异常执行流；`GetThreadContext` 记录异常 |
| **Early Bird** | `CreateProcess(SUSPENDED)` → 在 `main` 前用 APC 注入 | 进程刚建就有 APC 队列 |
| **Process Doppelgänging** | 用 **TxF 事务**改文件再回滚，PE 从未落盘 | 文件内容与内存映像不一致；`$UsnJrnl` 有事务回滚记录 |
| **Herpaderping** | 写文件 → 创建进程 → **改回原内容** → 才执行 | 磁盘文件与内存 PE 头不匹配 |
| **Transacted Hollowing** | 上面两者结合，用事务段做映像 | 同上，且 `\Device\NamedPipe` 有异常事务 |

**共性判据**：**磁盘上没有对应文件，或磁盘文件与内存映像的 PE 头/节内容不一致**。
所以检测这类样本必须做「内存 vs 磁盘」比对，光看文件扫描是零命中。

```bash
# 内存里的 PE 头 vs 磁盘文件
vol -f memory.dmp windows.malfind --dump --pid <PID>
# 对比 dump 出来的 MZ/PE 头与磁盘上同名文件
```

**为什么这些手法要考**：它们绕过的是「文件落地检测」这一整类防线。
判据不是「有什么可疑 API」，而是「**应该出现的东西没出现**」——这是负向判据，比正向特征难得多。

### 11.5 环境检查

```python
# 主机名、用户名、域、语言、特定文件是否存在
# if socket.gethostname() != "TARGET-PC": exit()
# if os.getlogin() in ["admin", "sandbox", "malware"]: exit()
# if "WORKGROUP" in os.environ.get("USERDOMAIN", ""): exit()
# if not os.path.exists("C:\\Users\\victim\\document.xlsx"): exit()
```

绕过：跑之前设好环境变量（`export COMPUTERNAME=TARGET-PC`），或补丁掉比较。

**样本立刻退出时，用 `strace`/`ltrace` 看它读了什么环境值**，前几条系统调用里就有线索。

## 12. 供应链投毒

**方法是 diff，不是通读代码**。

```python
# 自定义字母表的轮转解码 C2 域名
C = "0123456789abcdefghijklmnopqrstuvwxyz-."
domain = "".join(C[(C.index(ch) - 0x0D) % len(C)] for ch in encoded_domain)

# 路径再套一层 XOR
path = "".join(chr(b ^ 0x42) for b in [44, 45, 54, 43, 36, 59])

uuid = register_with_c2(domain)
flag = requests.get(f"http://{domain}/{path}", cookies={"uuid": uuid}).text
```

要点：

- 注入点通常在 **`try/except` 里** —— 异常处理块能吞掉错误，避免暴露。
- 自定义字母表用的是**模运算轮转**，不是标准凯撒。要按字母表长度取模。
- 一定要与官方 release 逐文件 diff，否则看不出哪几十行是注入的。

## 13. 纪律

- **PowerShell 的 `-enc` 是 UTF-16LE**。少了 `iconv` 那一步，看到的是夹空字节的碎片。
- **垃圾代码直接滤掉，不要读懂**。混淆的目的就是让噪声淹没信号。
- **动态分析三路同开**。只看 syscall 漏网络，只看流量漏落地文件。
- **`malfind` 要三条同时成立**（可执行+可写+无文件映射），只看一条大量误报。
- **`pstree` 看异常父子关系**比看进程名有效得多。
- **YARA 锚字节模式，不锚字符串**。字符串改起来太便宜。
- **`strings` 里没有 API 名就怀疑 API 哈希**。找那个小循环，再查 hashdb。
- **样本立刻退出先查环境检查**，不要先怀疑样本损坏。
- **供应链投毒靠 diff**。注入点只有几十行，藏在异常处理里。
