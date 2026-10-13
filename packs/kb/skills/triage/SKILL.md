---
name: triage
description: CTF 题目分诊入口：拿到题包/远程服务/模糊题面时先判类别（web/pwn/reverse/crypto/forensics/misc/ai），再交对应能力包技能，避免开局走错方向
keywords: 题目, 题面, 附件, 远程, nc, 靶机, 分类, 分诊, 从哪开始, 解题, flag, challenge, ctf, 卡住
features: new_challenge, vague_description, mixed_category
task_types: triage
mode: self-contained
---

# triage —— CTF 题目分诊（本技能只分诊，不深挖）

> 拿到一道题（题包 / 远程地址 / 一句模糊描述）不知道从哪下手时先走本流程。
> 类别明确后**立即转对应能力包技能**，深水区方法论由各技能 `skill_open` 打开，
> 本技能不重复任何具体攻击技术。

## Step 1：五件事侦察（顺序执行）

1. **列文件**：`ls -la` 题包目录，对每个文件 `file *` 看真实类型；
2. **字符串/魔数**：二进制 `strings -n 6`、`xxd | head`、binwalk 扫附加数据；
3. **取题面上下文**：描述/文件名/注释/附件里的提示词，给了 URL 先 GET 看一眼；
4. **摸远程**：`nc host port` 连一次，看它要什么（交互菜单/数学题/HTTP/受限 shell）；
5. **记 flag 格式**：题面或规则里的前缀（flag{ / CTF{ / 自定义），最后校验要用。

附件本体落 `bb_add_asset`（type=binary，value=sha256，meta 记类型/保护），
分诊结论落 `bb_add_finding`（vuln_class=triage）——后续会话不重复分诊。

## Step 2：判类别（文件类型 → 类别）

| 附件/服务形态 | 类别 | 转谁 |
|---|---|---|
| URL / HTML/JS/PHP 源码 / HTTP 服务 | web | web 包：web-strike-entry |
| `.elf/.exe/.so/.dll`/无扩展名二进制 + **有远程服务** | pwn | binary 包：binary-pwn（先 file-triage 看保护） |
| 同上但**纯离线、要校验算法/序列号** | reverse | binary 包：binary-rev |
| `.py/.sage/.txt` 里是大整数 / `.pem/.pub` | crypto | crypto 包：crypto-triage |
| `.pcap/.raw/.dd/.evtx/.vmem`、看不到内容的图片音视频 | forensics | forensics 包：forensics-triage |
| `.apk/.wasm/.pyc` | reverse（先） | binary-rev |
| `.safetensors/.pt/.pth/.onnx`、prompt/LoRA 字眼 | ai-ml | misc 包（skill_open `references/misc/ai-ml/index.md`） |
| 编码套娃/jail/受限 shell/信号/游戏/小众语言 | misc | misc 包（skill_open `references/misc/misc/index.md`） |
| 只有"找谁/在哪/什么时间"线索 | osint | forensics 包：forensics-triage 的 OSINT 段 |
| 混淆脚本/C2 流量/疑似恶意 PE | malware | binary 包：binary-pwn §1（**默认恶意，只静态**） |

题面关键词直判：buffer/ROP/heap/libc→pwn；RSA/AES/nonce/lattice→crypto；
XSS/SQL/JWT/SSRF→web；disk/memory/pcap/spectrogram→forensics；
packed/beacon/jail/encoding→malware/misc。

nc 行为：长输入崩溃→pwn；出数学题→crypto；受限 shell/eval→misc(jail)。

## Step 3：卡住就换类（pivot）

CTF 题常跨类：Web 题的 JWT 伪造要 crypto；pcap 里可能裹着待重放的 pwn exploit；
逆向先于 pwn（先还原漏洞函数再利用）；取证恢复出文件后可能是隐写或加密。
一条路 20 分钟无进展，重新走一遍 Step 2，带着新观察换类，不要硬刚。

## Step 4：收尾

解出后按 `misc/misc` 包 writeup 方法论产出可复现 writeup：
`skill_open(path="references/misc/writeup/index.md")`——精简、可复现、队友照做能验证。
多个 flag 候选时做全库唯一性核对，报出来源文件/路径，不取疑似干扰串。
