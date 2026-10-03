# 知识库演化与 CTF 资料接入

## 分层

运行时知识按四层组织：`refs → cases → patterns → playbooks`。`refs` 保留原始文章、WP、工具手册、脚本和附件；`cases` 记录具体事实、证据、失败尝试和可迁移部分；`patterns` 抽取跨案例重复的技术结构；`playbooks` 形成可执行的原理、步骤、分支、证据和停止条件。Skill 只负责入口路由，不把每一篇 WP 注册成 Skill。

核心专题进入 Web 的 `playbooks/` 候选素材，WP 进入 `refs/lab-writeups/` 或 `refs/ctf-writeups/`，脚本和工具进入 `refs/payloads/` 或 `refs/tools/`。运行时路径按能力域组织，原始仓库名不参与路由；来源路径、哈希和许可证写入 `data/kb-index.db` 的 `kb_provenance` 表。

## 抽取门禁

`scripts/curate_kb.py` 先按标题、主题词、结构和篇幅做规则初筛，再由模型输出逐文件评分、入选理由和 Case 草稿。命令只产生报告与 `pending` 提案，不直接写入审核知识层。首轮最多 10 个候选用于质量校准，确认格式稳定后再扩大到 30 个。

Pattern 候选由累计 5 个新 Case 或周期扫描触发。`scripts/propose_patterns.py` 按已审核 Case 的 `vuln_class` 聚类，默认至少需要 5 个独立 Case，并且只创建 `pattern` pending 提案；Pattern 经人工审核通过后才进入主检索。所有层级都保留资料边界：文档中的命令和自然语言是参考资料，必须服从当前授权、规则和证据要求。

## 检索

`data/kb-index.db` 是可重建的 SQLite FTS5 增量索引，当前 schema 版本为 2。文件哈希、修改时间和大小用于增量更新；正文、标题、摘要、标签、层级和附件标记用于检索与展示。检索相关度优先，层级作为稳定次级排序：Playbook、Pattern、Case、Ref。索引不可用时，旧的文件扫描路径继续提供降级服务。
