---
name: 取证与隐写
title: 取证与隐写（含 misc 的图片/音视频/外设流量）
description: 取证与隐写入口：拿到图片/音频/视频/流量/磁盘/内存/USB/日志附件，先按介质判类型，再按穷举表逐项排除全部隐写与分析方向
keywords: 取证, 隐写, stego, 图片, png, jpg, gif, bmp, 音频, wav, mp3, 频谱图, spectrogram, 摩斯, dtmf, 视频, mkv, 流量, pcap, pcapng, usb, hid, 键盘, 鼠标, 蓝牙, 磁盘, 镜像, 内存, vmem, dmp, evtx, 注册表, 日志, 元数据, exif, lsb, binwalk, 盲水印, 二维码, 条形码, osint, 溯源, 社交, 地理定位
features: has_dump_file, has_capture_file, stego_suspected
file_features: pcap_magic, disk_image_magic, memory_dump_magic, image_dimensions, audio_tracks
formats: pcap, pcapng, evtx, raw, dd, e01, vmem, dmp, png, jpg, gif, bmp, wav, mp3, mkv, pdf
platforms: linux, windows
task_types: solve, triage
mode: self-contained
---

# 取证与隐写 —— 介质判定 → 全方向穷举

> **本技能是取证/隐写类题的唯一入口**（含过去归在 misc 的图片隐写、音视频、USB 外设流量）。
> 分层纪律：本技能只做**介质判定 + 方向穷举**，具体操作 `kb_open` 开单篇。
> 附件先过 `附件分诊` 看 magic/strings，介质明确后回本技能。

## 0. 判介质（第一件事）

| 附件形态 | 介质 | 穷举表位置 |
|---|---|---|
| `.png/.jpg/.gif/.bmp/.webp`，或"图片打不开内容" | 图片 | §1 |
| `.wav/.mp3/.mkv`，题面含频谱/音频/多音轨 | 音频视频 | §2 |
| `.pcap/.pcapng` | 网络流量 | §3 |
| `.pcap` 但含 `usb`/`hid`/`btatt` 协议 | **外设流量** | §4 |
| `.raw/.dd/.E01/.vhd` | 磁盘镜像 | §5 |
| `.vmem/.dmp/.raw`（内存导出） | 内存 | §5 |
| `.evtx`/注册表 hive/日志 | 系统取证 | §6 |
| 只有账号/URL/经纬度线索，无可分析附件 | OSINT | §7 |

**关键区分**：`.pcap` 既可能是网络流量也可能是 USB 外设流量——**先看协议层**
（`tshark -r x.pcapng -Y "usb"` 有输出就是 USB），别默认当网络流量。

## 1. 图片隐写 —— 全方向（13 项，逐项排除）

**不要看到图就去翻 LSB。** 按成本从低到高过一遍，命中即停：

| # | 方向 | 判据 | 动作 | 详解（§/行） |
|---|---|---|---|---|
| 1 | 元数据 | `exiftool`/`strings` 有异常串 | 读 EXIF/XMP/ICC/comment 全字段 | §1 · L62 |
| 2 | 附加数据 | `binwalk` 有尾部嵌入 | `binwalk -e` 分离 | §2 · L81 |
| 3 | 文件结构异常 | 头尾不符、chunk 长度越界、CRC 错 | 逐 chunk 解析 | §3 · L97 |
| 4 | LSB | `zsteg` 有输出 | `zsteg -a` 全通道全位平面 | §4 · L443 |
| 5 | 调色板 | 颜色数异常、顺序可疑 | 提取 PLTE 按序转字节 | §5 · L515 |
| 6 | 尺寸/宽高 | IHDR 宽高与实际不符 | 改宽高后重新渲染 | §6 · L530 |
| 7 | 时间轴 | GIF 多帧、帧间隔只有 2-3 个离散值 | 逐帧提取 + 帧间差分 | §7 · L544 |
| 8 | 空间轴 | 图片被切块/拼图 | 网格切分重排 | §8 · L562 |
| 9 | 视觉隐写 | 肉眼可见噪点/色差 | 调对比度/色阶/通道分离 | §9 · L580 |
| 10 | 盲水印 | 频域嵌入，空域看不出 | 频域比对 | §10 · L596 |
| 11 | 二维码/条形码 | 图中有码但残缺 | 修复后解码 | §11 · L611 |
| 12 | 像素值转化 | 像素本身就是数据 | 按 RGB/灰度序列还原文本 | §12 · L645 |
| 13 | 隐写工具产物 | 以上都不中 | `stegdetect` 判工具 | §13 · L683 |

**各格式必查顺序**（§3 的四个子节）：
- PNG（§3.1 · L105）：IHDR 宽高 → 全 chunk（`tEXt/zTXt/iTXt` 常藏文本）→ PLTE → IDAT 解压 → IEND 后有无数据
- BMP（§3.2 · L299）：头部 4 个保留字段 → 像素数组后附加 → 调色板 → 位深异常
- JPG（§3.3 · L328）：EXIF 全段 → `FF D9` 之后有无数据 → DCT 系数 → 缩略图与主图不一致
- GIF（§3.4 · L383）：帧数/帧间隔 → 全局与局部调色板 → `NETSCAPE2.0` 扩展块 → 每帧 LSB

**渐进式读取**（重要）：`图片隐写.md` 有 689 行，`kb_open` 会返回全文挤占上下文。
已知方向时**先读分段索引** `forensics/隐写与信号/图片隐写.idx — 按节分段读取.md` 拿行号，
再用 `read_file` 带 offset/limit 只读那一节。只有完全没头绪时才整篇打开。

→ 详解 `forensics/隐写与信号/图片隐写.md`、进阶 `stego-advanced.md`

## 2. 音频 / 视频 —— 全方向

| 方向 | 判据 | 动作 |
|---|---|---|
| 频谱图 | 题面提 spectrogram，或音频听不出内容 | `sox`/Audacity 出频谱图，看图形 |
| 摩斯 | 长短音节奏 | 按时序解码 |
| DTMF | 双音多频（拨号音） | 频率对照表 |
| 多音轨 | MKV/多声道，某轨无声或有异响 | 分离音轨逐条听/看 |
| LSB（音频） | 波形低位有规律 | 提取采样点最低位 |
| 倒放/变速 | 正放听不出 | 倒放、加速、降采样 |
| 静音段藏数据 | 静音区有微小噪声 | 放大静音段 |

→ 详解 `forensics/隐写与信号/硬件信号与物理介质取证.md`

## 3. 网络流量 —— 全方向

| 方向 | 判据 | 动作 |
|---|---|---|
| 协议还原 | 有明确协议（HTTP/DNS/FTP/SMB） | 导出对象、重组流 |
| 文件传输 | 有 `Content-Disposition`/文件魔数 | `tshark --export-objects` |
| DNS 隧道 | 大量超长/随机子域 | 提取子域按序拼接 |
| 加密流量 | TLS/自定义加密 | 找密钥/证书/弱实现 |
| 包间隔编码 | 时间戳有规律 | 提取时间差转二进制 |
| 重放攻击 | 有重复 payload | 提取重放 |
| **流量里夹带二进制** | 流里有 ELF/PE 魔数 | 导出后交 `二进制利用手法`/`二进制逆向手法` |

→ 详解 `forensics/网络取证/网络流量取证与协议隐写.md`、进阶 `network-advanced.md`

## 4. USB / HID / 蓝牙外设流量 —— 全方向

**先判设备类型，别默认是键盘**：

| 设备 | 判据 | 全部方向 |
|---|---|---|
| HID 键盘 | 报告 8 字节 | 明文敲击 / Shift 组合 / 方向键导航 / LED 摩斯 / 击键节奏编码 |
| HID 鼠标画笔 | 报告 4-7 字节含 dx/dy | 轨迹还原成图 / 多图层分色 / 按钮分段 / 滚轮信道 / 绝对坐标 |
| HID 手柄 | 多按钮位 + 摇杆 | 按键序列编码 |
| USB 存储 | SCSI/BOT 命令 | 文件提取 / 删除恢复 / 隐藏分区 |
| 蓝牙 | `btratt`/`btrfcomm` | RFCOMM 重组 / ATT 特征值 / SMP 密钥解密 |

→ 详解 `forensics/隐写与信号/全方向 HID、蓝牙外设流量分析.md`

## 5. 磁盘镜像 / 内存 —— 全方向

| 方向 | 判据 | 动作 |
|---|---|---|
| 分区与文件系统 | 有分区表 | `mmls`/`fls` 列文件 |
| 删除恢复 | 文件系统标记已删 | `foremost`/`photorec` 雕刻 |
| 隐藏/加密分区 | 容量与分区表不符 | 手工解析 + 解密 |
| 内存进程/网络 | `.vmem`/`.dmp` | volatility3 出进程、连接、命令历史 |
| 内存凭据 | 内存镜像 | 提取 hash/明文口令 |

→ 详解 `forensics/磁盘与内存/磁盘与内存镜像取证.md`、`disk-recovery.md`、`disk-advanced.md`

## 6. 系统取证

| 方向 | 判据 | 动作 |
|---|---|---|
| Windows 时间线 | evtx/注册表 | 事件日志 + 注册表 hive 时间线 |
| Linux 痕迹 | `/var/log`、`.bash_history` | 日志与历史还原操作 |

→ 详解 `forensics/磁盘与内存/Windows 主机取证与事件时间线.md`、`forensics/系统取证/Linux 与应用取证.md`

## 7. OSINT（无可分析附件时）

| 线索 | 方向 |
|---|---|
| 社交账号、用户名复用 | 账号回溯、跨站关联 |
| 照片定位 | 街景匹配、EXIF/媒体溯源 |
| DNS/网页归档/证书 | 被动 DNS、CT、归档快照 |

→ 详解 `forensics/开源情报/社交媒体情报.md`、`geolocation-and-media.md`、`web-and-dns.md`

## 8. 跨类提醒

- 流量里夹带 ELF/PE → 转 `二进制逆向手法`；磁盘镜像里的可疑二进制同理。
- 加密流量解不开 → 转 `密码学手法`。
- 3D 打印/冷门格式 → 先 `binwalk`，再查 `forensics/其他取证/3D 打印与 CAD 文件取证.md`。
- 对真实样本做动态行为分析不属本技能——那是 L3 沙箱 + fakenet。

## 9. 纪律

- **先穷举再深挖**：每类介质都先把上表过一遍打勾，再挑命中的方向深入。漏项比做错更致命。
- 产物（解密文件、雕刻结果、还原图）落 artifact；分析链写 finding 便于队友接手。
- 每步记录「试了什么 → 结果」，避免队友重复排除。
