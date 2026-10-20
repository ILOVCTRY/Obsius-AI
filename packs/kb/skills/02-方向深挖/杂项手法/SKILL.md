---
name: 杂项手法
title: 杂项手法（无明确学科归属的题）
description: 杂项入口：pyjail/bashjail 沙箱逃逸、多层编码链、游戏与 VM、LLM 提示注入、对抗样本、DNS 隧道、无线 SDR 等无明确学科归属的题；图片/音视频/流量/磁盘一律走取证与隐写
keywords: misc, 杂项, pyjail, jail, 沙箱逃逸, bashjail, 受限shell, 编码链, base64, 多层编码, 编码识别, 游戏, vm, wasm, llm, prompt注入, 提示注入, 越狱, 对抗样本, ai, 机器学习, dns隧道, dns外带, sdr, 无线, iq, 信号
formats: py, txt, iq
task_types: solve, triage, reverse
mode: self-contained
---

# 杂项手法 —— 只接「真无学科归属」的题

> **边界（重要）**：图片隐写、音频视频、网络流量、USB 外设流量、磁盘内存 —— **全部走
> `取证与隐写` 技能**，不要在本技能里处理。本技能只接确实没有学科归属的题型。
>
> 分层纪律：本技能只做**题型判定 + 方向穷举**，操作细节 `kb_open` 开单篇。

## 0. 先做一次排除（避免抢取证/隐写的题）

| 如果题面/附件是… | 去哪 |
|---|---|
| 图片、音频、视频、频谱 | → `取证与隐写` |
| `.pcap/.pcapng`（含 USB/HID） | → `取证与隐写` |
| 磁盘镜像、内存导出、evtx、日志 | → `取证与隐写` |
| 只有账号/URL/经纬度 | → `取证与隐写`（OSINT 段） |
| **以下都不符合，才继续看本技能** | ↓ |

## 1. 方向穷举表

| 方向 | 判据 | 全部子方向 | kb_open |
|---|---|---|---|
| **pyjail** | 给了受限 Python 源码 + 连接脚本 | 内置函数过滤绕过 / 对象链回溯（`().__class__.__mro__`）/ 编码与字符串拼接 / 审计钩子绕 / 导入劫持 / `__builtins__` 恢复 / 沙箱模块绕过 | `misc/沙箱逃逸/pyjails.md` |
| **bashjail** | 受限 shell、命令白名单 | 命令拼接 / 通配符展开 / 变量替换 / 重定向写文件 / 环境变量注入 / `$()` 与反引号 / 编码执行 | `misc/沙箱逃逸/bashjails.md` |
| **编码链** | 一串可疑字符、多层嵌套 | 单层识别（base64/base32/hex/url/rot13）/ 多层剥壳 / 自定义码表 / 二进制位操作 / 异或 / 压缩嵌套 | `misc/编码解码/encodings.md`、`encodings-advanced.md` |
| **游戏 / VM / wasm** | 游戏存档、自制 VM、wasm 字节码 | 存档格式解析 / VM 指令集还原 / 字节码反编译 / 内存改值 | `misc/游戏与虚拟机/games-and-vms.md` |
| **LLM 提示注入** | 有 LLM 服务、聊天机器人 | 直接注入 / 越狱 / 上下文污染 / 工具调用劫持 / 系统提示泄露 | `misc/AI安全/llm-attacks.md` |
| **对抗样本** | 模型文件、扰动图 | 对抗扰动 / 成员推理 / 模型抽取 | `misc/AI安全/adversarial-ml.md`、`model-attacks.md` |
| **DNS 隧道** | 有 DNS 交互 | 子域编码 / TXT 记录外带 / 时序编码 | `misc/协议异常/dns.md` |
| **无线 SDR** | IQ 文件、`.wav` 射频录制 | 调制识别 / 解调 / 协议解析 | `misc/赛事专题/题目知识/ctf-misc/rf-sdr.md` |
| **Linux 提权杂项** | CTF 向本地提权 | SUID / 定时任务 / 内核漏洞 / 环境变量 | `misc/系统杂项/linux-privesc.md` |

## 2. 判据速查

- 给了 `.py` 源码且能连远程 → pyjail（先看它过滤了什么）。
- 只能执行少数命令、有 `$` 提示符 → bashjail。
- 字符串长度是 4 的倍数且只有 `A-Za-z0-9+/=` → base64 优先。
- 解出来还是乱码 → **继续剥，别停**（多层编码是常规套路）。
- 有 `.wasm` → 反编译成 wat 看逻辑。
- 题面出现 "flag 在系统提示里"/"让它说出密码" → LLM 注入。

## 3. 纪律

- 先穷举「信息在哪一侧」：服务器回显、附件元数据、协议交互，别急着写利用。
- pyjail 按「过滤了什么」反查，逐条试可用原语并记录。
- 需要 pip 装库先落 scratch requirements，docker 内执行。
- 收尾 writeup 写法见 `misc/赛事专题/WP撰写/index.md`。
