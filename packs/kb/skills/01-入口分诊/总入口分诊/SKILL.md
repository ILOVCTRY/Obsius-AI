---
name: 总入口分诊
description: CTF 题目分诊入口：拿到题包/远程服务/模糊题面时先判类别（web/pwn/reverse/crypto/forensics/misc/ai），再交对应能力包技能，避免开局走错方向
keywords: 分诊, 入口, 开局, 题包, 不知道, 从哪, 下手, 判类, triage, 题目分类
features: unknown_file_type
task_types: triage
mode: self-contained
---

# triage —— CTF 题目分诊（本技能只分诊，不深挖）

> 拿到一道题（题包 / 远程地址 / 一句模糊描述）不知道从哪下手时先走本流程。
> 类别明确后**立即转对应能力包技能**，深水区方法论由各技能 `kb_open` 打开，
> 本技能不重复任何具体攻击技术。

## Step 1：五件事侦察（顺序执行）

1. **列文件**：附件 zip **先调 `unpack_attachment`**（path=附件行给的
   `/workspace/artifacts/attachments/<zip>`）——服务端一次完成清旧残留+安全解压，
   落到 `scratch/att-<sha12>/` 并回结构化清单；**不要**用 run_cmd 拼
   `unzip -o`（只覆盖同名文件，同题重开时上轮分析脚本会整批残留进目录清单，
   把上下文淹掉）。已有解压目录时 `ls -lat` 看新旧（时间排序，残留一眼可辨）。
   对每个文件看真实类型：`file` 逐个点名，或
   `find . -maxdepth 1 -type f -exec file {} +`——**`file *` / `md5sum *` 遇到
   子目录直接报错**（`md5sum: xxx: Is a directory`，exit=1）。
2. **命令链拆开发**：`&&` 串起来时前半条报错会吞掉后面全部观察——用 `;` 或
   分多次 run_cmd，单条失败不阻断其余侦察。
3. **字符串/魔数**：二进制 `strings -n 6`、`xxd | head`、binwalk 扫附加数据；
4. **取题面上下文**：描述/文件名/注释/附件里的提示词，给了 URL 先 GET 看一眼；
5. **摸远程**：`nc host port` 连一次，看它要什么（交互菜单/数学题/HTTP/受限 shell）；
6. **记 flag 格式**：题面或规则里的前缀（flag{ / CTF{ / 自定义），最后校验要用。

附件本体落 `bb_add_asset`（type=binary，value=sha256，meta 记类型/保护），
分诊结论落 `bb_add_finding`（vuln_class=triage）——后续会话不重复分诊。

## Step 2：判类别（文件类型 → 类别）

| 附件/服务形态 | 类别 | 转谁 |
|---|---|---|
| URL / HTML/JS/PHP 源码 / HTTP 服务 | web | web 包：外网打点入口 |
| **URL 首页是纯静态页（游戏/工具/Canvas），无后端接口** | **web-前端逆向** | **web 包：前端 JS 逆向与混淆还原**（见下方 Step 2.5） |
| `.elf/.exe/.so/.dll`/无扩展名二进制 + **有远程服务** | pwn | binary 包：二进制利用手法（先 附件分诊 看保护） |
| 同上但**纯离线、要校验算法/序列号** | reverse | binary 包：二进制逆向手法 |
| `.py/.sage/.txt` 里是大整数 / `.pem/.pub` | crypto | crypto 包：密码学手法 |
| `.pcap/.raw/.dd/.evtx/.vmem`、看不到内容的图片音视频 | forensics | forensics 包：取证与隐写 |
| `.apk/.wasm/.pyc` | reverse（先） | 二进制逆向手法 |
| `.safetensors/.pt/.pth/.onnx`、prompt/LoRA 字眼 | ai-ml | misc 包（kb_open `misc/AI安全/AI、ML 题总入口.md`） |
| 编码套娃/jail/受限 shell/信号/游戏/小众语言 | misc | misc 包（kb_open `misc/知识库.md`） |
| 只有"找谁/在哪/什么时间"线索 | osint | forensics 包：取证与隐写 的 OSINT 段 |
| 混淆脚本/C2 流量/疑似恶意 PE | malware | binary 包：二进制利用手法 §1（**默认恶意，只静态**） |

题面关键词直判：buffer/ROP/heap/libc→pwn；RSA/AES/nonce/lattice→crypto；
XSS/SQL/JWT/SSRF→web；disk/memory/pcap/spectrogram→forensics；
packed/beacon/jail/encoding→malware/misc。

nc 行为：长输入崩溃→pwn；出数学题→crypto；受限 shell/eval→misc(jail)。

## Step 2.5：Web 题先分流「静态前端」还是「有后端」

> Web 类别内部还要再分一次。**纯静态前端题打注入/上传/爆破是空转**，
> 20 分钟过去一个包都没打进去，是分诊阶段最常见的浪费。

判据（同时命中 ≥3 条即判定为静态前端题）：

1. 首页响应头 `Server:` 为 nginx/openresty/Apache，`Etag`/`Last-Modified` 呈
   **静态文件特征**（如 `Etag: "7ad-5ccfc80d7bb40-gzip"`），无 `Set-Cookie`、无 `X-Powered-By`
2. 目录扫描（dirsearch/ffuf 全字典）只出 `index.html` + `js/` + `src/` + `images/`
   + `favicon.ico`（目录本身 403 禁列），**没有任何 .php/.jsp/.do 动态端点**
3. 全站 JS grep 不到 `XMLHttpRequest` / `fetch(` / `document.cookie` / `token` / `/api`
4. 页面是**游戏、计算器、登录壳、验证工具**（HTML5 小游戏 / Canvas 动画最常见）
5. 换个 path 或加 `?a=1` 时返回的是**前置代理自己的 404 页**（如 nginx 的
   `<center>404 Not Found</center>`），而不是应用框架的报错——说明没有应用层

判定成立 → **停止**注入/上传/目录爆破投入，转
`kb_open(module="web/实战打法/前端 JS 逆向与混淆还原.md")`，按「抓全 JS → grep `_0x`/`atob`
定位混淆 → 复现解密」走。flag 通常在 obfuscator.io 字符串数组 + RC4/XOR 里。

判据不成立（有动态端点/有 API/有登录态）→ 照常走 `web 包：外网打点入口`。

## Step 3：卡住就换类（pivot）

CTF 题常跨类：Web 题的 JWT 伪造要 crypto；pcap 里可能裹着待重放的 pwn exploit；
逆向先于 pwn（先还原漏洞函数再利用）；取证恢复出文件后可能是隐写或加密。
一条路 20 分钟无进展，重新走一遍 Step 2，带着新观察换类，不要硬刚。

### Step 3.5：卡住先查判据库（比换类更省轮次）

换类之前先确认**不是自己漏了前置条件**。判据库是从 1027 篇赛事 WP 抽出来的
「现象 → 判据 → 打法」速查，按**你观察到的现象**索引，不按题目类型：

`kb_open(module="web/赛事专题/判据库/判据库总索引.md")`

最常用的三张表：

- **按现象路由**：源码里出现 `==`/`unserialize`/`include`、Cookie 是 JWT、
  参数是 `rO0AB`、给了 `n`/`e`/`hint`、出现 `0x9E3779B9`、给了内存镜像 —— 对号入座。
- **按「卡在哪」路由**：有执行但无回显、有注入但 payload 不生效、链构造出来打不通、
  分解不出来、格出来的向量没意义、偏移全错 —— 这些是「手法没错但漏了前提」，
  换类也解决不了，必须查判据。
- **各篇「纪律」节**：写的是「什么时候这条判据不成立」，比正向手法更省时间。

判据库只给选路与失效条件，具体命令与代码仍在主库
（`web/漏洞分类/`、`binary/漏洞利用/` 等）。判据指路，主库给实现。

## Step 4：收尾

解出后按 `misc/misc` 包 writeup 方法论产出可复现 writeup：
`kb_open(module="misc/赛事专题/WP撰写/CTF Write-up Generator.md")`——精简、可复现、队友照做能验证。
多个 flag 候选时做全库唯一性核对，报出来源文件/路径，不取疑似干扰串。
