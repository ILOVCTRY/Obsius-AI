# forensics 能力域 · 知识库（packs/kb/forensics/）

> kb 全局单根（expert-pool M0，2026-09-21）：`packs/kb/` 一级目录=能力域，本目录=forensics 域。
> Agent 经 `kb_open(module=forensics/<快照>/<包内路径>)` 按需打开单个文件，全局路径域前缀消歧。
> route.json 为任务导航（测试点 → 手册）。
>
> **本地基线（K2 收编，2026-09-20 起）**：`forensics/`、`osint/` 为上游 ctf-skills
> 的收编改编版（切片已合并），允许在 Skill 页就地修订与新建经验 md（新经验写新
> 文件，不覆盖英文原文）；`refs/` 下为快照原件，不翻译、不就地改，
> 更新走 `scripts/import_kb.py` 重新导入。

| 快照 | 来源与内容 |
|---|---|
| `forensics/` | K2 收编改编 · 磁盘/内存、流量、隐写、信号与外设取证 |
| `osint/` | K2 收编改编 · 开源情报收集与交叉验证 |
| `refs/` | 快照原件：ctf-forensics / ctf-osint（ctf-skills，MIT） |

许可：ctf-skills 快照为 MIT（见 `../licenses/CTF-SKILLS-LICENSE`）。
