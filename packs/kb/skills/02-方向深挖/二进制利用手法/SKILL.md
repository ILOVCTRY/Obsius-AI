---
name: 二进制利用手法
description: Pwn 利用入口：checksec 保护判定 → 栈/堆/内核打法路由；认 libc 用离线 libc-db（23 个 glibc 版本，零网络）；兼恶意样本静态分析入口（混淆脚本/C2/PE .NET）
keywords: pwn, 溢出, 栈, 堆, uaf, double free, shellcode, rop, ret2libc, 格式化字符串, seccomp, 沙箱逃逸, 内核, 驱动, 恶意样本, c2, beacon, 混淆, libc, glibc, libc-db, libc版本, 偏移, one_gadget
features: has_remote_service, crashes_on_input, has_shellcode_hint
file_features: ELF, NX, Canary, PIE, RELRO, FORTIFY, stripped, dynamically_linked, PE, seccomp
formats: elf, pe, so, dll
vuln_classes: stack-overflow, heap, format-string, rop, kernel, sandbox-escape, malware-c2
platforms: linux, windows
task_types: solve, reverse
mode: self-contained
---

# 二进制利用手法 —— Pwn 利用与恶意样本静态入口

> 分层纪律：本技能只做**保护判定 + 打法路由**。具体技术在 pwn / malware
> 英文参考资料，用 `kb_open(module=…)` 按表开单篇，禁止通读。逆向校验逻辑先走
> 附件分诊 分诊、二进制逆向手法 还原；拿到漏洞点再回本技能选利用路线。

## 0. 第一眼：checksec → 保护组合 → 路线

`checksec`（或 readelf 手判）后按下表开模块（路径均为 kb_open 的 module）：

| 观察到的保护/形态 | 首选模块（binary/漏洞利用/） |
|---|---|
| 无 NX（栈可执行）/ ret2shellcode | `binary/漏洞利用/ROP 与 shellcode.md` |
| NX 开、No PIE：ret2text / 基础栈溢出 | `binary/漏洞利用/栈与基础溢出.md` |
| NX+PIE：ret2libc / ROP / 栈迁移 | `binary/漏洞利用/ROP 与 shellcode.md` → `binary/漏洞利用/ROP 进阶.md` |
| 有格式化字符串（%x/%p/%n） | `binary/漏洞利用/格式串利用.md`（配套 `binary/漏洞利用/scripts/fmtstr_payload_suite.py`） |
| Canary：泄露/爆破/覆盖 | `binary/漏洞利用/ROP 进阶.md` |
| 菜单题 / malloc-free / UAF / double free / tcache | `binary/漏洞利用/堆利用.md` → `binary/漏洞利用/FILE 结构体与堆利用.md` |
| seccomp 过滤（orw / open-read-write） | `binary/漏洞利用/沙箱逃逸与受限环境.md`（配套 `binary/漏洞利用/scripts/seccomp_orw_generator.py`、`binary/漏洞利用/scripts/shellcraft_asm.py`） |
| 内核题（vmlinux/bzImage/启动脚本/qemu）/ 驱动 ioctl | `binary/漏洞利用/Linux 内核提权全手法（按缓解选路）.md` → `binary/漏洞利用/内核利用进阶手法.md` → `binary/漏洞利用/内核保护绕过.md` |
| SROP / 高阶花式 | `binary/漏洞利用/Pwn 进阶手法.md`、`binary/漏洞利用/高级利用手法判据.md`（四段连载合一篇） |
| 临场 checklist / 踩坑 | `binary/漏洞利用/原语速查表.md` |

配套 exp 骨架（Read 参考，**运行只准 docker/sandbox**）：
`binary/漏洞利用/scripts/`：ret2libc_two_stage.py、srop_execve.py、fmtstr_payload_suite.py、
seccomp_orw_generator.py、shellcraft_asm.py。

## 0.5 认 libc：先 `libc-db`（离线），查不到才联网

**镜像已预置 23 个 glibc 版本（2.19~2.44，49 个 .so），在 `/opt/libc-db/`。
认 libc 全程零网络，不要现场下载**——实测容器到 libc.rip 只有 10~20KB/s
（2MB 要 2~3 轮续传），到 github 直接不通；且 `curl` 超时后容易拿到**静默残缺**
文件（818931 字节 / 正常 1.9MB），到 pwntools 解析才炸 `ELFParseError`。
现场下载曾让 agent 烧掉 4~5 轮工具调用。

```bash
libc-db --find puts=0x875a0 read=0x110fa0   # 已知泄漏偏移 → 反查版本（唯一命中，一次拿全表）
libc-db 2.31                                # 已知版本 → 列常用符号偏移
libc-db 2.31 system str_bin_sh              # 只查指定符号
libc-db --list                              # 看预置了哪些版本
```

判定纪律：
- **`--find` 给 2~3 个符号**几乎必然唯一命中，比只给一个可靠得多。
- 偏移必须**先减基址**再比；函数偏移天然页对齐（真实偏移的低 12 位一般不是 0，
  但泄漏值减基址后应落在合理范围，别把未减基址的值直接丢进去）。
- 本地查不到（版本不在 2.19~2.44）才联网：`libc-fetch --puts <偏移>`。
- **确实需要本地 libc 文件**（跑 `one_gadget`、`DynELF`、`patchelf` 换 libc）时，
  直接用预置文件，别再下载：`one_gadget /opt/libc-db/2.31/libc.so.6`。
- 真要下载就用 `libc-fetch --download <路径>`（自带断点续传 + sha256 + ELF
  结构校验，失败会删残片）。**禁止手搓 `curl -o libc.so`**。
- `readelf -h` 和 `readelf -S` **都校验不了截断**（前者 header 在前 64 字节，
  后者截断时 rc 仍是 0、错误只写 stderr）。判完整性必须看 stderr 有没有 `Error`。

## 1. 恶意样本/可疑文件的静态分析入口（binary/恶意样本/）

未知样本默认按恶意处理：**只做静态，不执行**；确需动态一律 L3 沙箱 + 断网。

| 样本形态 | 模块（binary/恶意样本/） |
|---|---|
| 混淆脚本（JS/VBS/PowerShell/宏）、loader、打包器 | `binary/恶意样本/脚本混淆、YARA 与内存取证.md` |
| C2 流量特征、信标协议、回连行为推断 | `binary/恶意样本/C2 流量与协议分析.md` |
| PE / .NET 程序集、托管代码反编 | `binary/恶意样本/PE 与 .NET 样本分析.md` |

函数级结论（解密例、校验函数）照例 `bb_upsert_func` 落 func_kb，别重复反编译。

## 2. 产出落点

- exp / 解密脚本 → `bb_add_artifact(kind="poc")`（自包含，拿到能跑，限授权目标）；
- 漏洞机制结论 → findings（CTF 题可 info 级）；保护组合写进资产 meta，供后续会话复用。

## 红线

- 样本与 exp **只在 docker/sandbox 执行**；host 端只允许 file/readelf/objdump/
  python-capstone 等不执行样本的静态命令。
- exp 的远程目标只能是题目靶机或授权范围；越界走审批，禁止对真实第三方 IP 打。
