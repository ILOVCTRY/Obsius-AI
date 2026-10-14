# misc 能力域 · 知识库（packs/kb/misc/）

> kb 全局单根（expert-pool M0，2026-09-21）：`packs/kb/` 一级目录=能力域，本目录=misc 域。
> Agent 经 `kb_open(module=misc/<模块>/<包内路径>)` 按需打开单个文件，全局路径域前缀消歧。
> route.json 为任务导航（测试点 → 手册）。
>
> **树状化重构（2026-10-14）**：本域由旧的 misc/ai-ml/writeup/refs 平铺改为中文
> 类目模块树（原 misc/、ai-ml/、writeup/ 收编目录按专题溶解）；旧 refs/ 原料库拆入
> 下列模块。知识模块允许就地修订与新建经验 md；`赛事专题/题目知识/` 下为 ctf-skills
> 快照原件，不翻译，与收编版字节相同的副本已去重，更新走 `scripts/import_kb.py --force`。

## 模块导览

| 模块 | 篇数 | 来源与内容 |
|---|---|---|
| `编码解码/` | 2 | 编码体系与进阶编码（收编改编） |
| `沙箱逃逸/` | 2 | bash jail / python jail 逃逸 |
| `协议异常/` | 1 | DNS 协议异常利用 |
| `游戏与虚拟机/` | 1 | 游戏 VM 与虚拟机逆向思路 |
| `系统杂项/` | 1 | Linux 提权杂项 |
| `射频信号/` | 1 | RF-SDR 信号分析 |
| `AI安全/` | 4 | LLM 攻击、模型攻击、对抗样本、index |
| `赛事专题/` | 119 | `题目知识/ctf-misc|ctf-ai-ml` 快照原件（9 篇）；`题目知识/../WP撰写/`（ctf-writeup 收编，2 篇）；`Writeup合集/`（57 篇）；`靶场复盘/`（50 篇） |
| `载荷与字典/` | 102 | CTF 常用脚本及工具（Base/键盘密码/CRC32/盲水印/二维码等） |

许可：ctf-skills 快照为 MIT（见 `../licenses/CTF-SKILLS-LICENSE`）。
