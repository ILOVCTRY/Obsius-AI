# web 能力域 · 知识库（packs/kb/web/）

> kb 全局单根（expert-pool M0，2026-09-21）：`packs/kb/` 一级目录=能力域，本目录=web 域。
> Agent 经 `kb_open(module=web/<快照>/<包内路径>)` 按需打开单个文件，全局路径域前缀消歧。
> 快照原文更新走 `scripts/import_kb.py` 重新导入。
>
> **本地基线（2026-09-14 起）**：本目录允许在 Skill 页就地修订与新建经验 md
> （新经验写新文件，不覆盖翻译英文原文）；重新导入会保留本地新增文件与
> `.history/` 版本历史，本 README 重写前也自动留版本备份。

导入日期（UTC）：2026-09-21

| 快照 | 来源与内容 |
|---|---|
| `recon/` | 信息收集：passive 被动 / active 主动 |
| `webapp/` | Web 漏洞族：authn 认证 / injection 注入 / 客户端 |
| `post-exp/`（本次未导入） | 后渗透（占位） |
| `notes/`（本次未导入） | 实战散篇：网关陷阱 / 字段笔记 |
| `playbooks/` | src-strike 快照（内部知识源）· SRC 方法论/弹药/rules |
| `refs/` | 参考资料：h1-reports/字典/poc/cves + refs/ctf-web（ctf-skills MIT） |

许可：ctf-skills 快照为 MIT（见 `../licenses/CTF-SKILLS-LICENSE`）；src-strike 为内部
知识源快照（无独立 LICENSE），仅限本项目授权使用，不外发。
