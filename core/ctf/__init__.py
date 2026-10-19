"""CTF 轨专用内核（借鉴 hxbai/hxctf 的自主交战引擎，2026-10-19）。

本包只服务 **CTF 轨**，与渗透/红队/研究轨无关，故独立成包而不是塞进 core/agent。
四块能力，全部纯函数/纯 stdlib，不 import 黑板（可单测、可被工具层与对话层共用）：

| 模块 | 解决的问题 | 来源 |
|------|-----------|------|
| `flagjudge` | 「这串东西是不是 flag」——候选抽取、诱饵识别、跨题家族去重、变换回环验证 | hxbai `engine/judge.py` |
| `observe` | 零 LLM 成本的观测结构化——nmap 行→端口/服务、URL/路径、凭据形态、salient 摘要、易变值归一化 | hxbai `engine/loop.py` 抽取器群 |
| `ammo` | 凭据弹药管线——产品指纹→厂商默认口令→年份/后缀/leet 变异 | hxbai `engine/ammo.py` |
| `pacing` | 时长无关化——预算随「比赛总时长 × 题量」缩放，同一套阈值通吃短赛长赛 | hxbai `engine/pacing.py` |

**为什么不直接抄 hxbai 的整条引擎**：它是 Linux 容器 + 纯 CLI + 单镜像形态，我们是
Windows 原生 + WebUI + 多轨平台。可迁移的是**判定与抽取这类无形态依赖的内核**，
调度/执行器那套（scheduler.py / cc.py）依赖它的 TSG 与 claude CLI 子进程模型，
硬搬会与本项目的会话/Team/黑板三层打架。故本包只收内核，接线在各消费点。
"""
