---
name: file-triage
description: CTF 附件静态分诊：文件类型识别、字符串提取、保护检测、入口点定位
keywords: 附件, 识别, file, strings, 静态, 分诊, triage
features: has_binary, has_pcap, has_archive, unknown_file_type
task_types: triage, recon
---

# file-triage —— 附件静态分诊

> 分层纪律：本技能只做**第一眼分诊**。深水区（反汇编/调试）在 binary-rev。

## 流程（每个附件必做）

1. **定类型**：`file`（WSL）/ 扩展名 + magic bytes，判断 category：
   ELF/PE 可执行、脚本源码、pcap、压缩包、磁盘镜像、其他。
2. **字符串层**：`strings -n 6` 抽取；重点找 flag 格式、提示语、库函数痕迹。
3. **保护检测**（ELF）：`checksec` 类信息——NX/PIE/canary/RELRO，决定后续 pwn 打法。
4. **落黑板**：
   - 附件本体 `bb_add_asset type=binary value=<sha256>`（meta 记路径/类型/保护）；
   - 分诊结论 `bb_add_finding`（vuln_class=triage，evidence 里放命令输出摘要）。
5. **建议下一步**：基于分诊结果在 finding 里写"建议任务"（如：需逆向校验函数）。

## 红线提醒

- 附件默认 untrusted：识别类命令（file/strings）走 host 安全；
  任何**执行**必须 docker/sandbox（见领域红线 #3）。
