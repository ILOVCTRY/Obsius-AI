# binary 能力域 · 知识库（packs/kb/binary/）

> kb 全局单根（expert-pool M0，2026-09-21）：`packs/kb/` 一级目录=能力域，本目录=binary 域。
> Agent 经 `kb_open(module=binary/<快照>/<包内路径>)` 按需打开单个文件，全局路径域前缀消歧。
> route.json 为任务导航（测试点 → 手册）。
>
> **本地基线（K2 收编，2026-09-20 起）**：`pwn/`、`reverse/`、`malware/` 为上游
> ctf-skills 的收编改编版（切片已合并），允许在 Skill 页就地修订与新建经验 md
> （新经验写新文件，不覆盖英文原文）；`refs/` 下为快照原件，不翻译、不就地改，
> 更新走 `scripts/import_kb.py` 重新导入。

| 快照 | 来源与内容 |
|---|---|
| `pwn/` | K2 收编改编 · 堆/FSOP、格式化串、ROP、沙箱逃逸等利用手法 |
| `reverse/` | K2 收编改编 · 静态/动态分析、脱壳、混淆还原 |
| `malware/` | K2 收编改编 · PE/.NET、C2 协议、脚本混淆（默认恶意，只静态） |
| `refs/` | 快照原件：ctf-pwn / ctf-reverse / ctf-malware（ctf-skills，MIT） |

许可：ctf-skills 快照为 MIT（见 `../licenses/CTF-SKILLS-LICENSE`）。
