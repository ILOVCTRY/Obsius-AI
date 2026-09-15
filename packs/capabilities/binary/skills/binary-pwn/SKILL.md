---
name: binary-pwn
description: Pwn 利用入口：checksec 保护判定 → 栈/堆/内核打法路由；兼恶意样本静态分析入口（混淆脚本/C2/PE .NET）
keywords: pwn, 溢出, 栈, 堆, uaf, double free, shellcode, rop, ret2libc, 格式化字符串, seccomp, 沙箱逃逸, 内核, 驱动, 恶意样本, c2, beacon, 混淆
features: has_remote_service, crashes_on_input, has_shellcode_hint
file_features: ELF, NX, Canary, PIE, RELRO, FORTIFY, stripped, dynamically_linked, PE, seccomp
formats: elf, pe, so, dll
vuln_classes: stack-overflow, heap, format-string, rop, kernel, sandbox-escape, malware-c2
platforms: linux, windows
task_types: solve, reverse
---

# binary-pwn —— Pwn 利用与恶意样本静态入口

> 分层纪律：本技能只做**保护判定 + 打法路由**。具体技术在 ctf-pwn / ctf-malware
> 英文知识库，用 `kb_open(module=…)` 按表开单篇，禁止通读。逆向校验逻辑先走
> file-triage 分诊、binary-rev 还原；拿到漏洞点再回本技能选利用路线。

## 0. 第一眼：checksec → 保护组合 → 路线

`checksec`（或 readelf 手判）后按下表开模块（路径均为 kb_open 的 module）：

| 观察到的保护/形态 | 首选模块（ctf-pwn/） |
|---|---|
| 无 NX（栈可执行）/ ret2shellcode | `ctf-pwn/rop-and-shellcode.md` |
| NX 开、No PIE：ret2text / 基础栈溢出 | `ctf-pwn/overflow-basics.md` |
| NX+PIE：ret2libc / ROP / 栈迁移 | `ctf-pwn/rop-and-shellcode.md` → `ctf-pwn/rop-advanced.md` |
| 有格式化字符串（%x/%p/%n） | `ctf-pwn/format-string.md`（配套 `ctf-pwn/scripts/fmtstr_payload_suite.py`） |
| Canary：泄露/爆破/覆盖 | `ctf-pwn/rop-advanced.md` |
| 菜单题 / malloc-free / UAF / double free / tcache | `ctf-pwn/heap-techniques.md` → `heap-techniques-2.md` → `heap-fsop.md` |
| seccomp 过滤（orw / open-read-write） | `ctf-pwn/sandbox-escape.md`（配套 `scripts/seccomp_orw_generator.py`、`shellcraft_asm.py`） |
| 内核题（vmlinux/bzImage/启动脚本/qemu）/ 驱动 ioctl | `ctf-pwn/kernel.md` → `kernel-techniques.md` → `kernel-bypass.md` |
| SROP / 高阶花式 | `ctf-pwn/advanced.md`、`advanced-exploits.md`（至 `-5.md`） |
| 临场 checklist / 踩坑 | `ctf-pwn/field-notes.md` |

配套 exp 骨架（Read 参考，**运行只准 docker/sandbox**）：
`ctf-pwn/scripts/`：ret2libc_two_stage.py、srop_execve.py、fmtstr_payload_suite.py、
seccomp_orw_generator.py、shellcraft_asm.py。

## 1. 恶意样本/可疑文件的静态分析入口（ctf-malware/）

未知样本默认按恶意处理：**只做静态，不执行**；确需动态一律 L3 沙箱 + 断网。

| 样本形态 | 模块（ctf-malware/） |
|---|---|
| 混淆脚本（JS/VBS/PowerShell/宏）、loader、打包器 | `ctf-malware/scripts-and-obfuscation.md` |
| C2 流量特征、信标协议、回连行为推断 | `ctf-malware/c2-and-protocols.md` |
| PE / .NET 程序集、托管代码反编 | `ctf-malware/pe-and-dotnet.md` |

函数级结论（解密例、校验函数）照例 `bb_upsert_func` 落 func_kb，别重复反编译。

## 2. 产出落点

- exp / 解密脚本 → `bb_add_artifact(kind="poc")`（自包含，拿到能跑，限授权目标）；
- 漏洞机制结论 → findings（CTF 题可 info 级）；保护组合写进资产 meta，供后续会话复用。

## 红线

- 样本与 exp **只在 docker/sandbox 执行**；host 端只允许 file/readelf/objdump/
  python-capstone 等不执行样本的静态命令。
- exp 的远程目标只能是题目靶机或授权范围；越界走审批，禁止对真实第三方 IP 打。
