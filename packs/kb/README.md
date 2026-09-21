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
