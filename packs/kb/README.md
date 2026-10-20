# packs/kb/ · 知识库全局根（expert-pool M0，2026-09-21）

> 全局单根：一级目录=能力域（web / binary / crypto / forensics / misc / cloud，与能力包同键）；
> 各域结构见域内 README.md。`kb_open(module=<域>/<模块>/<包内路径>)` 全局路径。
>
> **树状化重构（2026-10-14）**：各域内部由旧的「英文快照平铺」（recon/webapp/
> playbooks/refs/notes 等）改为**三层知识树**——一级=方向（轨名，不可动）、二级=
> **中文类目模块**（如 web/漏洞分类、binary/逆向工程、crypto/非对称加密）、三级=
> 模块内子目录（类目/英文小类，如 漏洞分类/SQL注入）。旧 `refs/` 原料库已按类别拆入
> 各方向模块。收编版（知识模块，可本地修订）与快照原件（`<方向>/赛事专题/题目知识/
> ctf-<x>/`，上游 ctf-skills 原样、不翻译）分层存放；两者字节相同的副本已去重
> （正本保留在收编版模块）。
>
> 本目录文件：
> - `route_index.yaml` — 全局路由索引（条目 kb 字段带域前缀，按会话启用域过滤）
> - `licenses/` — 快照上游 LICENSE 收编
> - `<域>/route.json` — 域内任务导航（值带域前缀，留域内）
> - `安全红线/` — 轨红线 + 平台规则（owners）+ 评级与价值口径 + 规则模板
> - `skills/<name>/` — 技能（扁平，不进 registry 资料树面）
>
> 收编改编区（各域非快照模块）允许就地修订（本地基线 K3）；`赛事专题/题目知识/`
> 快照原件不翻译、不新增内容（其内部链接已按去重后正本改写的除外），更新走
> `scripts/import_kb.py --force` 重新导入。

## 使用口径

**库内链接口径 = Obsidian 全路径 wikilink（2026-10-14）**：本目录已注册为
Obsidian vault（`.obsidian/` 为本机配置，不入库；`newLinkFormat=absolute` +
`useMarkdownLinks=false`）。人/编辑器/Obsidian 写库内链接一律
`[[web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）|别名]]`，目标可省扩展名，`#锚点`/`|别名` 只是展示层；
裸名 wikilink 一律不解析（库内有 52 个 `手册.md`、30 个 `README.md`，天然歧义）。
存量相对 md 链接已由 `scripts/kb_links_to_wikilinks.py` 一次性抹平；**不转**：
代码围栏/行内代码、图片、外链、`*/题目知识/*` 上游快照原件、`*/.history/`。
迁移后仍有 141 条历史断链未修，需人工，勿猜目标。护栏见
`tests/test_kb_links_wikilink.py`、`tests/test_kb_refs_wikilink.py`、
`tests/test_skill_kb_paths.py`。

## webapp 手册 frontmatter 约定（K7 2026-09-29）

`web/漏洞分类/<漏洞类>/手册.md` 统一带 frontmatter 元数据，供 kbindex 提示行
（标题/摘要）与检索质量使用：

- `title:` 中文测试手册名（**禁止英文 slug 当标题**——中文 2-gram 匹配不到；
  如 `authbypass-authentication-flaws` 曾致该手册对中文任务隐形）
- `summary:` 一句话「本篇讲什么/怎么测」（优先于正文首行提取；正文首行多为
  报告纪律引用，对模型零信息量）
- `report_policy: rarely|forbidden`（可选）：「几乎不交」类测试点（clickjacking/
  cors/crlf 等）把纪律从标题挪进此字段，避免 title 误导匹配，纪律仍留在 summary
- `phase:` / `vuln_class:` 分面标签（K5 检索过滤用，保持不变）
