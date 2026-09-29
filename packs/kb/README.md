# packs/kb/ · 知识库全局根（expert-pool M0，2026-09-21）

> 全局单根：一级目录=能力域（binary / crypto / forensics / misc / web，与能力包同键）；
> 各域结构见域内 README.md。`kb_open(module=<域>/<快照>/<包内路径>)` 全局路径。
>
> 本目录文件：
> - `route_index.yaml` — 全局路由索引（条目 kb 字段带域前缀，按会话启用域过滤）
> - `licenses/` — CTF-SKILLS-LICENSE（五域快照共用一份）
> - `<域>/route.json` — 域内任务导航（值带域前缀，留域内）
>
> 收编改编区（各域非 refs 快照）允许就地修订（本地基线 K3）；refs/ 快照原件
> 不翻译不就地改，更新走 `scripts/import_kb.py` 重新导入。

## webapp 手册 frontmatter 约定（K7 2026-09-29）

`web/webapp/<漏洞类>/手册.md` 统一带 frontmatter 元数据，供 kbindex 提示行
（标题/摘要）与检索质量使用：

- `title:` 中文测试手册名（**禁止英文 slug 当标题**——中文 2-gram 匹配不到；
  如 `authbypass-authentication-flaws` 曾致该手册对中文任务隐形）
- `summary:` 一句话「本篇讲什么/怎么测」（优先于正文首行提取；正文首行多为
  报告纪律引用，对模型零信息量）
- `report_policy: rarely|forbidden`（可选）：「几乎不交」类测试点（clickjacking/
  cors/crlf 等）把纪律从标题挪进此字段，避免 title 误导匹配，纪律仍留在 summary
- `phase:` / `vuln_class:` 分面标签（K5 检索过滤用，保持不变）
