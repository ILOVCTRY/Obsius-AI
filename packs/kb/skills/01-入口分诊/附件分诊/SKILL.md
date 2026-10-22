---
name: 附件分诊
description: CTF 附件静态分诊：文件类型识别、字符串提取、保护检测、入口点定位
keywords: 附件, 识别, file, strings, 静态, 分诊, triage
features: has_binary, has_pcap, has_archive, unknown_file_type
task_types: triage, recon
mode: self-contained
---

# 附件分诊 —— 附件静态分诊

> 分层纪律：本技能只做**第一眼分诊**。深水区（反汇编/调试）在 二进制逆向手法。

## 流程（每个附件必做）

1. **定类型**：`file`（WSL）/ 扩展名 + magic bytes，判断 category：
   ELF/PE 可执行、脚本源码、pcap、压缩包、磁盘镜像、其他。
2. **查判据（别跳）**：类型一定下来，先 `kb_search` 查这个载体的判据，再继续跑命令。
   知识库里是历史踩坑沉淀的具体命令/字段/陷阱，比凭记忆拼参数可靠：

   | 类型 | 建议查询式 |
   |---|---|
   | ELF/PE 可执行 | `kb_search("ELF 逆向 入口点")`、`kb_search("upx 脱壳")` |
   | pcap / 流量 | `kb_search("pcap 提取 凭据")`、`kb_search("流量分析 隐蔽信道")` |
   | 压缩包 | `kb_search("zip 伪加密")`、`kb_search("压缩包 密码")` |
   | 磁盘/内存镜像 | `kb_search("内存镜像 取证")`、`kb_search("磁盘 恢复 分区")` |
   | 图片/音频 | `kb_search("图片 隐写 LSB")`、`kb_search("音频 频谱")` |
   | 其他/看不懂 | `route_lookup("<关键现象>")` 或 `kb_search("<现象词>")` |

   命中就 `kb_open` 读正文；**一次零命中换个说法再查一次**（中文/英文、术语/现象
   各试一次），两次都不对口才转自己探索。不要因为一次没中就再也不查。
3. **字符串层**：`strings -n 6` 抽取；重点找 flag 格式、提示语、库函数痕迹。
4. **保护检测**（ELF）：`checksec` 类信息——NX/PIE/canary/RELRO，决定后续 pwn 打法。
5. **落黑板**：
   - 附件本体 `bb_add_asset type=binary value=<sha256>`（meta 记路径/类型/保护）；
   - 分诊结论 `bb_add_finding`（vuln_class=triage，evidence 里放命令输出摘要）。
   - **CTF 轨边界**：不要传 `parent_id`（自动挂到本题资产下）；`value` 必须是
     附件**真实 sha256**；`unpack_attachment` 解压出的 zip 内文件**不要登记**——
     路径已在解压回执里，直接读即可（登记会落成根级 binary，题目页会多出一道题）。
6. **建议下一步**：基于分诊结果在 finding 里写"建议任务"（如：需逆向校验函数）。

## 红线提醒

- 附件默认 untrusted：识别类命令（file/strings）走 host 安全；
  任何**执行**必须 docker/sandbox（见领域红线 #3）。
