---
name: misc-triage
description: 杂项/AI 题分诊入口：pyjail/bashjail/编码链/游戏 VM/LLM/对抗样本/无线 SDR 等无明确学科归属题的特征路由
keywords: misc, 杂项, pyjail, 沙箱, 逃逸, bashjail, 编码, base64, 解码, 游戏, vm, wasm, llm, prompt, 提示注入, 越狱, 对抗样本, ai, 机器学习, dns, sdr, 无线, latin, 隐写杂项
formats: py, txt, wav, iq
task_types: solve, triage, reverse
---

# misc-triage —— 杂项/AI 题分诊与路由

> 分层纪律：本技能只做**题型判定 → 开专题**。资料在 misc 能力包英文知识库，
> 用 `kb_open(module=…)` 按表开单篇，禁止通读。拿不准先翻各目录 `index.md`。

## 0. 先看题面给了什么

- 一段被过滤/受限的 Python 源码 + 连接脚本 → pyjail
- 受限 bash/shell 环境 → bashjail
- 一串编码串/多层编码 → 编码链
- 游戏/仿真器/异构二进制（wasm/factorio/藏头在游戏里）→ games-and-vms
- 一个 LLM 服务/聊天机器人 → llm-attacks
- 模型文件/对抗扰动图 → adversarial-ml / model-attacks
- 无线信号 IQ/wav → rf-sdr
- DNS 交互题 → dns

## 1. 特征 → 专题对照表（module 路径）

| 特征 | kb_open 模块 |
|---|---|
| pyjail（过滤/沙箱/逃逸） | `misc/misc/pyjails.md` |
| bashjail / 受限 shell | `misc/misc/bashjails.md` |
| 编码识别与多层解码 | `misc/misc/encodings.md`、进阶 `misc/misc/encodings-advanced.md` |
| 游戏 / VM / wasm | `misc/misc/games-and-vms.md` |
| LLM 提示注入/越狱 | `misc/ai-ml/llm-attacks.md` |
| 模型攻击（成员推理/抽取） | `misc/ai-ml/model-attacks.md` |
| 对抗样本 | `misc/ai-ml/adversarial-ml.md` |
| AI 题总索引 | `misc/ai-ml/index.md` |
| DNS 隐道/协议 | `misc/misc/dns.md` |
| 无线 / SDR | `misc/misc/rf-sdr.md` |
| Linux 提权杂项（CTF 向） | `misc/misc/linux-privesc.md` |
| CTFd 平台导航 | `misc/misc/ctfd-navigation.md` |
| writeup 写法（收尾） | `misc/writeup/index.md` |

## 2. 纪律

- misc 题先穷举「信息在哪一侧」：服务器回显、附件元数据、协议交互，别急着写利用。
- pyjail 逃逸按「过滤了什么」反查专题索引，逐条试可用原语并记录。
- 需要 pip 装库先落 scratch requirements，docker 内执行。
