---

phase: binary
vuln_class: [malware, pe, dotnet, unpacking]
title: PE 与 .NET 样本分析
summary: 样本分析的唯一目标是「拿到配置」——C2 地址、加密密钥、投放路径。判据是「字符串在哪、解密函数在哪、配置结构长什么样」。
---

# PE 与 .NET 样本分析

脚本类与混淆手法见 [[binary/恶意样本/脚本混淆、YARA 与内存取证.md|脚本与混淆]]；C2 协议与流量侧见 [[binary/恶意样本/C2 流量与协议分析.md|C2 与协议]]；样本类型总览见 [[binary/恶意样本/恶意样本与流量分析.md|恶意样本索引]]。

## 0. 判据速查

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| PE 的 `.data`/`.rdata` 有高熵块 | 加密配置，要找解密函数 | §2 |
| 有 PE 资源段（`RT_RCDATA`/自定义） | 配置常藏在资源里 | §2.1 |
| .NET 且字符串明显是 Base64 | 配置 + AES/MD5 派生密钥 | §3 |
| 只有 DNS 请求没有 HTTP | DNS 隧道 C2 | §3.1 |
| 启动即 `IsDebuggerPresent` / 检测 VMware | 反分析，需先过检测 | §1 |
| 是 Python 打包的 exe | PyInstaller 提取，可能套 PyArmor | §4 |
| .NET 样本 IL 被控制流混淆 | 用 AsmResolver 编程遍历，别硬看 | §3.2 |

**先判断值不值得逆**：如果字符串里直接有 IP/域名，先 `strings` + `floss`，别急着开 IDA。

## 1. 快速分诊与反分析

```bash
peframe malware.exe         # 快速分诊：节区、导入表、可疑 API
pe-sieve /pid <PID>         # 运行时：从内存 dump 出被解包的镜像
pestudio                    # Windows 图形化静态分析
```

**反分析检测清单**（样本会查这些，分析环境要先抹掉）：

| 检测项 | 样本怎么做 | 分析侧对策 |
|---|---|---|
| 虚拟机 | 查 VMware/VirtualBox 的注册表键、驱动名、MAC 前缀 | 改 MAC、清注册表、用裸金属或 KVM 加隐藏 |
| 调试器 | `IsDebuggerPresent`、`CheckRemoteDebuggerPresent`、PEB 的 `BeingDebugged` | ScyllaHide / TitanHide |
| 时序 | `rdtsc` 差值、`GetTickCount` 跳变 | 单步时禁用（会破坏时序检测的绕过） |
| 环境 | 用户名、计算机名、域名是否含 `sandbox`/`malware`/`cuckoo` | 改成普通企业命名 |
| 分析工具 | 枚举进程找 `idaq.exe`、`x64dbg.exe`、`wireshark.exe` | 重命名或换机分析 |
| 文件/注册表 | 查 `C:\analysis`、`HKLM\...\VMware` | 清理痕迹 |
| 交互 | 鼠标是否移动、窗口数、`GetCursorPos` | 真实交互或模拟输入 |

**`pe-sieve` 是 .NET 与加壳样本的关键**：它在进程运行后从内存 dump 出已解包的镜像，比静态脱壳省事一个数量级。

## 2. 配置提取

### 2.1 常见存放位置

```text
.data 段             硬编码（最简单，strings 直接出）
PE 资源段            RT_RCDATA / 自定义资源类型
.NET 资源            .resources 流
安装时写的注册表     HKCU\Software\<vendor>\
落地到磁盘的加密文件 %APPDATA%\<name>\config.bin
```

### 2.2 提取命令

```bash
# PE 资源（-t 10 = RT_RCDATA）
wrestool -x -t 10 malware.exe -o config.bin

# .NET 资源
monodis --mresources malware.dll

# 直接看节区内容
objdump -s -j .rdata malware.exe
objdump -s -j .data  malware.exe

# 全量字符串（含宽字符与堆字符串）
strings -a -n 6 malware.exe
floss malware.exe          # 专门抓运行时解密的字符串，比 strings 强
```

**`floss` 优先于 `strings`**：它模拟执行解密 stub，能把运行时才出现的 C2 地址还原出来。

## 3. .NET 样本

### 3.1 DNS 型 C2

特征：样本只有 DNS 查询，没有明显的 HTTP 流量。域名是 base32/base64 编码的子域，请求间隔固定。

```text
分析路径：
1. 找网络相关类型 —— TcpClient、UdpClient、Dns、HttpClient、WebClient
2. 找编码/加密包装 —— Convert.FromBase64String、Encoding.UTF8.GetString
3. 找命令分发 —— switch/case 对 opcode 分支，分支体就是支持的命令
4. 找心跳 —— Timer、Thread.Sleep 的固定间隔
```

工具：**dnSpy**（可编辑 + 调试）、**ILSpy**（只读，反编译质量高）、**dotPeek**。

### 3.2 编程化遍历（IL 被混淆时）

控制流混淆会把方法体拆成几十个 `switch` 分支，人工看基本无解。用 AsmResolver 批量遍历，先定位可疑方法：

```csharp
using AsmResolver.DotNet;

var module = ModuleDefinition.FromFile("malware.dll");
foreach (var type in module.GetAllTypes())
{
    foreach (var method in type.Methods)
    {
        if (!method.HasMethodBody) continue;
        var body = method.CilMethodBody;
        // 按指令特征筛选：Base64 调用、字符串常量、网络 API
        foreach (var ins in body.Instructions)
        {
            if (ins.Operand?.ToString()?.Contains("FromBase64String") == true)
                Console.WriteLine($"{type.FullName}::{method.Name}");
        }
    }
}
```

思路是**先按 API 调用筛出候选方法，再单点深挖**，不要在混淆的方法体里逐条读。

### 3.3 配置解密（LimeRAT 模式）

典型结构：配置类里有 Base64 字符串，解密方法是 AES-256-ECB，密钥由硬编码字符串的 MD5 派生。

```text
密钥派生（这是最容易搞错的一步）：
  md5(硬编码字符串) → 32 个 hex 字符 = 16 字节
  取 md5[:30]（前 15 字节）+ 完整 md5（16 字节）+ 0x00（1 字节）= 32 字节
```

```python
from Crypto.Cipher import AES
import hashlib, base64

key_source = '${8\',`d0}n,~@J;oZ"9a'
md5 = hashlib.md5(key_source.encode()).hexdigest()      # 32 hex chars

# 15 字节 + 16 字节 + 1 字节 = 32 字节
key = bytes.fromhex(md5[:30] + md5 + "00")[:32]

plaintext = AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(encrypted_b64))
```

要点：

- **`md5[:30]` 是 15 字节不是 15 字符**。hex 里 2 个字符 = 1 字节，`[:30]` 取 30 个 hex 字符。
- 拼接后是 30+32+2 = 64 个 hex 字符 = 32 字节，正好 AES-256 的密钥长度，`[:32]` 是防御性截断。
- **ECB 模式没有 IV**。如果代码里有 IV，说明是 CBC，别照抄。
- 这类「MD5 前 N 字节 + 完整 MD5 + 填充」的派生方式在小 RAT 家族里反复出现，遇到 `AES + MD5` 组合先试这个模式。

## 4. PyInstaller + PyArmor

```bash
# 第一步：拆 PyInstaller 归档
python pyinstxtractor.py malware.exe
# 产出目录里找主 .pyc（通常与 exe 同名）

# 第二步：若套了 PyArmor，用对应版本的 unpacker
# github.com/Svenskithesource/PyArmor-Unpacker
# 按 PyArmor 版本选方法（不同版本的保护机制差异很大）

# 第三步：清理反混淆后的源码
# 删掉混淆代码注入的大量死函数
# 定位真正的加密与外带逻辑
```

要点：

- **PyInstaller 的 `.pyc` 可能缺 magic number 头部**。直接 `uncompyle6`/`decompyle3` 会报错，需要按 Python 版本手工补 16 字节头。
- **PyArmor 版本必须对上**。不同版本的加密结构不兼容，用错 unpacker 会得到一堆乱码而不会报错。
- 反混淆后的源码里，**真正有价值的通常只有 2–3 个函数**：配置解密、网络发送、命令执行。其余都是注入的死代码，不用读。

## 5. 纪律

- **先 `floss` 再上 IDA**。运行时解密的字符串能省掉大半静态分析工作。
- **`.NET` 优先 dnSpy，不要硬看 IL**。dnSpy 能直接调试并改代码，比读 IL 快十倍。
- **IL 混淆时按 API 调用筛方法，不要逐条读指令**。混淆的目标就是让你逐条读。
- **AES 密钥派生要按字节数算，不是字符数**。hex 的 2 字符 = 1 字节，这是最常见的算错点。
- **`pe-sieve` 是从内存拿解包镜像的首选**，比静态脱壳稳。
- **反分析检测要先处理再分析**。带着 `IsDebuggerPresent` 挂调试器，样本会直接走垃圾分支，你分析的是假逻辑。
- **PyArmor 版本不对就别硬拆**，换对应版本的 unpacker，否则得到的是看似成功的乱码。
