# misc 能力域 · 知识库（packs/kb/misc/）

> kb 全局单根（expert-pool M0，2026-09-21）：`packs/kb/` 一级目录=能力域，本目录=misc 域。
> Agent 经 `kb_open(module=misc/<快照>/<包内路径>)` 按需打开单个文件，全局路径域前缀消歧。
> route.json 为任务导航（测试点 → 手册）。
>
> **本地基线（K2 收编，2026-09-20 起）**：`misc/`、`ai-ml/`、`writeup/` 为上游
> ctf-skills 的收编改编版（切片已合并），允许在 Skill 页就地修订与新建经验 md
> （新经验写新文件，不覆盖英文原文）；`refs/` 下为快照原件，不翻译、不就地改，
> 更新走 `scripts/import_kb.py` 重新导入。

| 快照 | 来源与内容 |
|---|---|
| `misc/` | K2 收编改编 · jail/编码套娃/游戏与 VM/DNS/RF-SDR/Linux 提权杂项 |
| `ai-ml/` | K2 收编改编 · LLM 攻击、模型攻击、对抗样本 |
| `writeup/` | K2 收编改编 · writeup 收尾方法论 |
| `refs/` | 快照原件：ctf-misc / ctf-ai-ml / ctf-writeup（ctf-skills，MIT） |

许可：ctf-skills 快照为 MIT（见 `../licenses/CTF-SKILLS-LICENSE`）。
