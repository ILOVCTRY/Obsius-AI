---
name: forensics-triage
description: 取证题/流量磁盘内存隐写与 OSINT 入口：按附件形态（pcap/镜像/日志/图片音视频）路由到对应分析专题
keywords: 取证, 流量, pcap, pcapng, 磁盘, 内存, 镜像, 隐写, stego, 日志, evtx, 注册表, memdump, vmem, 溯源, osint, 社交, 地理定位, 元数据, 信号, sdr, 频谱图
features: has_dump_file, has_capture_file, stego_suspected
file_features: pcap_magic, disk_image_magic, memory_dump_magic, image_dimensions, audio_tracks
formats: pcap, pcapng, evtx, raw, dd, e01, vmem, dmp, png, jpg, gif, wav, mp3, mkv, pdf
platforms: linux, windows
task_types: solve, triage
---

# forensics-triage —— 取证与 OSINT 分诊路由

> 分层纪律：本技能只做**介质判定 → 开专题**。操作细节在 forensics /
> osint 英文知识库，用 `kb_open(module=…)` 按表开单篇，禁止通读。
> 附件先过 file-triage（magic/strings），介质明确后回本技能选模块。

## 0. 先看附件是什么

`.pcap/.pcapng` → 流量；`.raw/.dd/.E01` 磁盘镜像；`.vmem/.dmp` 内存；
`.evtx`/注册表 hive → Windows 取证；图片/音频/视频/PDF"打不开内容"→ 隐写；
只有账号/URL/照片经纬度线索 → OSINT。

## 1. 介质 → 专题对照表

**forensics（kb_open module 前缀 `forensics/`）**

| 附件形态/题目关键词 | 模块 |
|---|---|
| 抓包、会话重组、协议解析、包间隔编码、TCP 流 | `network.md`；进阶 `network-advanced.md` |
| 磁盘镜像、分区、删除恢复、文件雕刻 | `disk-and-memory.md`；恢复 `disk-recovery.md`；进阶 `disk-advanced.md` |
| 内存镜像、进程/网络连接提取、volatility | `disk-and-memory.md`；Windows 侧 `windows.md`，Linux 侧 `linux-forensics.md` |
| 注册表、事件日志 evtx、Windows 时间线 | `windows.md` |
| 图片隐写（LSB/EXIF/附加数据/binwalk） | `stego-image.md`；总览 `steganography.md`；进阶 `stego-advanced.md`（两段连载合一篇） |
| 音频/频谱图/摩斯/DTMF/多音轨 | `signals-and-hardware.md`（题目描述含 spectrogram/audio tracks/MKV 也走此篇） |
| 键盘/USB/HID 流量、外设抓包、逻辑分析仪 | `peripheral-capture.md`、`signals-and-hardware.md` |
| 3D 打印文件（冷门，先 binwalk） | `3d-printing.md` |

**osint（前缀 `osint/`）**——题目只有"找人/找位置/找时间"，没有可分析附件：

| 线索 | 模块 |
|---|---|
| 社交账号、用户名复用、帖子回溯 | `social-media.md` |
| 照片定位、街景匹配、EXIF/媒体溯源 | `geolocation-and-media.md` |
| DNS/被动 DNS、网页归档、证书透明度 | `web-and-dns.md` |

## 2. 跨类提醒

- pcap 里可能裹着 exploit（→ binary-pwn 重放）或加密流量（→ crypto-triage 解密）；
  镜像里的可疑二进制交给 binary-rev；卡住时换类，别死磕。
- 对真实样本做动态行为分析不是本技能范围——那是 L3 沙箱 + fakenet（malware 轨）。
- 产物（解密文件、雕刻结果、flag 截图）落 artifact；分析链写 finding 便于队友接手。
