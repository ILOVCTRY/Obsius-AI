---
title: Windows 主机取证与事件时间线
phase: forensics
vuln_class: [windows-forensics, evtx, registry, ntfs, usn-journal, memory-forensics, anti-forensics]
summary: Windows 取证先分清证据层：事件日志给事件语义，USN/MFT 给文件变化，注册表给配置与时间线，内存给运行态；单一时间戳或单一日志都不足以定案。
---

# Windows 主机取证与事件时间线

Windows 取证要把不同证据源放进同一时间线。磁盘镜像与通用内存分析见 [[forensics/磁盘与内存/disk-and-memory.md|磁盘与内存取证]]；Linux 侧日志和容器痕迹见 [[forensics/系统取证/linux-forensics.md|Linux 系统取证]]；网络连接与协议对象见 [[forensics/网络取证/network.md|网络流量取证]]。

## 0. 判据速查

| 线索 | 首查证据 | 可回答的问题 |
|---|---|---|
| 用户新增、改名、删除 | Security.evtx 4720/4726/4781；SAM Names 键 | 账户何时变化、RID 是什么 |
| 日志被清除 | Security 1102；USN、MFT、Defender、PowerShell history | 清除前后发生了什么 |
| 文件疑似隐藏 | NTFS ADS、MFT、USN `$J`、回收站 `$I/$R` | 是否有隐藏流、删除或改名 |
| 怀疑 RDP 登录 | TerminalServices 日志 1149、21/22/23/24/25；USN 用户目录 | 来源 IP、登录/断开时间 |
| 怀疑 WMI 横向执行 | `WMIPRVSE.EXE`、Prefetch、`__<时间戳>.*`、USN | 执行次数和输出痕迹 |
| 只有内存镜像 | 先识别系统与进程，再查 clipboard、命令行、网络、hive | 运行态凭据和隐藏进程 |
| 有安全擦除迹象 | `EFSTMPWP`、`.ZZZ`、BleachBit 临时名 | 空闲区恢复是否还有价值 |

## 1. 事件日志：用事件 ID 建立语义时间线

常见 Security 事件：

| ID | 含义 |
|---|---|
| 4720 / 4722 | 创建 / 启用账户 |
| 4724 / 4726 | 尝试重置密码 / 删除账户 |
| 4738 / 4781 | 修改账户 / 重命名账户 |
| 1102 | Security 审计日志被清除 |
| 41 / 1001 | 非正常关机 / Bugcheck |

先用 Event Viewer、`evtx_dump.py` 或 `python-evtx` 导出 XML，再抽取事件时间、TargetUserName、SubjectUserName、LogonId、来源地址。事件 ID 的含义依日志类型而异；须连同 Provider、Channel 和 XML 字段一起核对。

RDP 相关日志通常分散在多个 channel：

- `TerminalServices-RemoteConnectionManager/Operational`：1149 认证成功，通常含来源 IP。
- `TerminalServices-LocalSessionManager/Operational`：21 登录、22 shell 启动、23 注销、24 断开、25 重连。
- `RemoteDesktopServices-RdpCoreTS/Operational`：连接接受/断开及客户端地址。

单独的 1149 不应直接等同于完整交互会话；与 LocalSessionManager、网络记录和用户 profile 创建时间交叉验证。

## 2. 注册表、SAM 与账户

常见 hive：`NTUSER.DAT`（用户配置）、`SAM`（账户与本地哈希）、`SYSTEM`（boot key/系统配置）、`SOFTWARE`（软件与系统配置）。离线检查可用 RegRipper 或 Registry 库：

```bash
rip.pl -r NTUSER.DAT -p all
```

SAM 哈希提取需要配套 `SYSTEM` hive 的 boot key：

```python
from impacket.examples.secretsdump import LocalOperations, SAMHashes
boot_key = LocalOperations("SYSTEM").getBootKey()
SAMHashes("SAM", boot_key).dump()
```

本地 RID 常见为 500（内置 Administrator）、501（Guest），普通账户通常从 1000 起；RID 不是用户名，也不证明账户曾成功登录。若 4720 已缺失，可检查 `SAM\Domains\Account\Users\Names` 子键最后修改时间，但它只是近似创建时间，需与 hive transaction log、USN 和事件日志交叉验证。

其他配置线索：检查 `SOFTWARE\Microsoft\Windows\CurrentVersion\OEMInformation` 的 SupportURL 等字段；异常 URL 是线索，不单独构成恶意判定。浏览器 Chromium/Edge 的 `History` 是 SQLite，先复制数据库并同时保留 `-wal`/`-shm`，再按 `last_visit_time` 解码时间。

## 3. NTFS：ADS、MFT、USN 与回收站

### 3.1 Alternate Data Streams

ADS 是同一 NTFS 文件记录下命名的数据流，常规目录列表可能不显示。镜像分析优先用 Sleuth Kit：

```bash
fls -r image.dd | grep ':'
istat image.dd <inode>
icat image.dd <inode>-<type>-<id> > extracted.bin
```

Windows 上可用 `Get-Item -Stream *`、`Get-Content -Stream <name>` 或 `dir /r`。`Zone.Identifier` 通常记录下载来源，不要把它与恶意 ADS 混为一谈。对镜像提取时保留 inode、属性 id、流名和 hash。

### 3.2 MFT 与 USN Journal

MFT 文件记录常为 1024 字节；`$FILE_NAME` 与 `$STANDARD_INFORMATION` 各自保存时间戳。两组时间不一致可能提示 timestomping，但正常复制、解压、时区处理也会造成差异，必须结合 USN、日志和父目录事件判断。

USN Journal `$Extend\$UsnJrnl:$J` 记录创建、删除、写入、改名等变化，日志清除后仍可能保留部分痕迹。常用 reason：`FILE_CREATE=0x100`、`FILE_DELETE=0x200`、`DATA_EXTEND=0x2`、`DATA_OVERWRITE=0x1`、`CLOSE=0x80000000`。用成熟解析器优先；自写解析必须依据 `RecordLength`、版本和文件名偏移遍历，不能假设所有记录都是 V2。

USN 文件引用号可与 MFT 关联以恢复路径。PowerShell history 的 `DATA_EXTEND` 可帮助定位写入时间，但一次扩展未必严格对应一条命令；profile 目录创建可作为首次交互登录的近似锚点，不代表 WMI 执行。

### 3.3 回收站与擦除痕迹

`$Recycle.Bin\<SID>` 中 `$I*` 保存原路径、删除时间和大小等元数据，`$R*` 是内容。应按同名随机后缀配对，并保留 SID 与 hash。`EFSTMPWP` 可能来自 `cipher.exe /w` 擦除空闲空间；`.ZZZ` 或擦除工具临时文件也可能提示安全擦除。发现后降低对 free-space carving 的预期，转查回收站、卷影副本、MFT resident data、ADS 和 journal；目录名称本身不能证明是哪位用户执行。

## 4. 执行痕迹与防御日志

Impacket `wmiexec.py` 可能在 `C:\Windows\` 的 ADMIN$ 路径产生 `__<unix_timestamp>.<随机后缀>` 临时输出文件：创建、读取后删除的周期可由 USN/MFT 残迹重建。`WmiPrvSE.exe`、Prefetch、WMI repository 和网络连接可作佐证；单一文件名可伪造，需交叉关联。

PowerShell 命令历史常见于：

```text
C:\Users\<user>\AppData\Roaming\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt
```

Defender `MPLog-*.log`、DetectionHistory、ASR 规则事件可能在 Security.evtx 清除后仍保留检测和阻断记录。搜索 `DETECTION`、`THREAT`、`QUARANTINE`、`ASR`；将日志中的路径、hash、时间和进程名回连到 MFT/USN。

hosts 文件可检查行尾、异常空白和非文本字节；`.contact` 文件是 XML，检查 Notes 等字段。此类隐写线索先用十六进制/结构化解析确认，不要因 `strings` 输出异常就认定隐藏 payload。

## 5. Windows 内存：先识别，再按问题选插件

Volatility 3 先识别系统：

```bash
vol -f memory.dmp windows.info
vol -f memory.dmp windows.pslist
vol -f memory.dmp windows.pstree
vol -f memory.dmp windows.psscan
vol -f memory.dmp windows.netscan
```

`pslist` 与 `psscan`/其他进程视图差异可提示隐藏或已退出进程，不直接证明恶意。按证据问题使用插件：

- 剪贴板：`windows.clipboard`，优先找复制过的密码、令牌和 flag。
- 命令行/控制台：`windows.cmdline`、`windows.consoles`。
- 凭据：离线 hive 与受支持的 credential 插件；先确认 OS 版本与插件兼容性。
- 进程内存：dump 后按已知结构、编码和文件 magic 搜索，`strings` 仅作初筛。
- 文件对象：`windows.filescan` 后再 dump，注意对象可能不完整。

Base64 内存片段若以 `UEsD` 开头，可能对应 ZIP 的 `PK\x03` 前缀。提取候选后补齐 padding、严格解码并验证 `PK\x03\x04` 与中央目录/CRC；候选可能被截断或跨页，不能仅凭前缀认定可恢复。

## 6. 压缩包与其他 Windows 痕迹

WinZip AES 口令可用 `zip2john` 提取后按对应格式交给 Hashcat；模式号应依据当前工具版本和 ZIP 加密类型确认。成功破解后先验证归档完整性，再解压到隔离目录。

Telemetry 文件如 `imprbeacons.dat` 可能含客户端 IP、地理字段等：

```bash
strings imprbeacons.dat | tr '&' '\n' | grep -E 'CIP|geo_|COUNTRY'
```

字段可能是缓存或遥测残留，需与用户、时间及网络证据关联，不把地理字段当作设备实时位置。

## 7. 日志被清除时的交叉检查

按成本和独立性检查：Security 1102、其他 EVTX channel、USN/MFT、SAM/SYSTEM transaction logs、PowerShell history、Prefetch、Defender、RDP logs、WMI repository、浏览器 SQLite、回收站和卷影副本。建立统一时区的时间线，记录每个时间源的精度、时区和可能的修改方式。若多个来源冲突，先检查时钟偏差、复制行为和解析器版本，不要强行拼成单一叙事。

## 8. 纪律

先保护原始镜像与 hive，工作副本只读分析；每个结论附证据路径、记录号/事件 ID、时间转换方式和 hash。区分“发生过”“可能发生”和“无法确定”。时间戳偏差、工具输出缺项和名称特征都只是待验证线索，必须由独立证据支撑。
