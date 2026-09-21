# core/intel/

> 情报面板（E9/E10，DESIGN.md §16）：跨项目的**全局**学习/情报模块，数据不进任何 blackboard.db。存储在 `config/intel/`（intel.db + feeds.json + profile.json）。

## 文件

- `store.py` — `IntelStore(intel_dir="config/intel")`：SQLite WAL 全局库（**SCHEMA_VERSION=3**），四表：`articles`（url UNIQUE，score/direction/is_priority/read/starred + **v3 `has_poc`/`poc_url`**）+ `briefs`（date 主键，同日覆盖）+ **E10 `vault_notes`**（path 主键= vault 相对 POSIX 路径，tags JSON，content 正文仅本地搜索）+ **E10 `learning_plans`**（week=ISO 周一主键，同周覆盖）。单连接 + 写锁。**schema 变更惯例（v3 起，吸取 9515da9 教训）**：新增 `_migrate(conn)`——按 `PRAGMA table_info` 实测列集幂等 ALTER，**不按版本号分支**；版本号 upsert 移到 migrate 之后。`upsert_articles` 为**只升不降**的 upsert（已存在仅补 has_poc/poc_url，NVD 后补 Exploit 标签的窗口差回填），返回值只数真插入行。E10 方法：`replace_notes`/`list_notes`/`search_notes`/`note_stats`/`platform_direction_counts`/`save_plan`/`get_plan`/`latest_plan`/`list_plans`。
- `config.py` — feeds.json（`load_feeds/save_feeds`，首访自动种子默认源；坏 JSON 兜底回种子**不覆写盘上文件**）+ profile.json（`load_profile/save_profile`：七方向权重 0–5 钳制 + stage + **E10 vault{path,enabled}**；**load/save 均透传未知键**——手编字段不丢，已知键归一化、漏项补默认）。
- `vault.py`（E10）— `index_vault(root)`：`rglob("*.md")` 跳 `.obsidian/.trash/.git/.history`，frontmatter 复用 skills.registry，title=fm.title→首个 h1→stem，tags=fm+内联 `#tag`（扫前 4000 字符）去重截 20，content 存原文；`build_tree(notes)` 平铺路径嵌套化（目录在前各自排序）。
- `fetch.py` — 纯函数抓取层，`RawGetter = (url, timeout) -> (status, text)` **可注入（测试永不触网）**，默认 urllib 出站、**不经执行网关**（平台自身可信出站）。`parse_rss`（RSS 2.0 + Atom 通吃）、`fetch_nvd`（**7 天窗**，perPage=200 不分页）/`fetch_kev`/`fetch_github_advisories`（走 api.github.com/advisories 免 key REST）、`fetch_all`（**单源失败只记 errors 不中断整批**）。**POC 启发式判定在 fetch 层（§16.1 定稿）**：`_poc_signal`/`_poc_url_signal`（NVD "Exploit" tag + 域名白名单，github/gitlab 要求路径含 poc/exploit/CVE 号，排除 `github.com/advisories`/`CVEProject` 等）；三源产出 `has_poc`/`poc_url` 两键；`_enrich` 按 CVE id 把 NVD/GHSA 证据合并到 KEV 条目（**无证据 KEV 保持 has_poc=False 不进简报**；NVD 单 CVE 补查留扩展点未做）。
- `intel_service.py` — 打分与简报（E9）：`score_articles`（is_priority 恒满分；LLM 批量只喂标题+摘要 150 字符，**任何失败降级 `_rule_score`**）、`compose_brief`、`run_refresh` 管线。**简报漏洞板块只收 has_poc 条目（§16.1）**：`run_refresh` 里 `cves=[a for a in _top(...) if a["has_poc"]][:8]`；复现文章关联在选材层做（`_CVE_RE` 提 CVE 号 + `_REPRO_KWS` 判复现 → `cve_assoc` 挂子行、`repro_extra` 提进板块），`compose_brief`/`_llm_brief` 保持纯渲染（prompt 明令素材未给 POC 证据的 CVE 不得出现、板块空则整段省略）。E10 追加：`infer_direction`、`week_start`、`learning_profile`、`compose_weekly_plan`/`_llm_weekly_plan`（LLM 入参**仅元数据**；失败降级模板，stats.by 标 llm/template）。

## API（在 core/api/app.py，全局无项目前缀）

- E9：`GET /api/intel/overview`、`GET|PUT /api/intel/feeds`、`GET|PUT /api/intel/profile`、`POST /api/intel/fetch`（Job intel-refresh）、`GET /api/intel/briefs[/ {date}]`、`GET /api/intel/articles`、`PATCH /api/intel/articles/{id}`。
- E10 八端点：`GET|PUT /api/intel/vault`（写 profile.json 经 pack_write_lock+备份；**配置了路径 PUT 即返 index_job_id 自动索引**）、`POST /api/intel/vault/index`（Job vault-index，全量重建；未配置 422）、`GET /api/intel/vault/tree`、`GET /api/intel/vault/search?q=`、`GET /api/intel/learning/profile`、`POST /api/intel/learning/plan`（Job learning-plan：聚合→当周简报缺则最新→top15 文章→compose→save_plan）、`GET /api/intel/learning/plan[?week=]`（缺省 latest，无 404）、`GET /api/intel/learning/plans`。
- 惰性建库：`app.state.intel` 首访问才落 config/intel/（测试传 `intel_dir=tmp`）；**测试注入口 `app.state.intel_getter` / `app.state.intel_llm`**。`_intel_classifier()` 构建失败返回 None 降级规则，**不 503**。

## 约定与坑

- **隐私红线（E10 定稿，不可放松）**：LLM 入参只含 vault **元数据**（文件名/标题/标签/目录/计数）；笔记正文只存 intel.db 供本地搜索，**绝不进任何 LLM 入参**（有测试断言）；vault 只读，平台绝不写回。
- vault 读取全走索引：tree/search 只查 intel.db，请求路径零 FS 访问（路径来自 DB 天然无穿越）；**v1 接受 staleness**（编辑后手动重建索引，无自动监听）。
- 全局写经 `pack_write_lock()` + `_pack_history_backup`（与 config/mcp.json 同锁同备份）；抓取/索引/周计划全是触发式 Job，**无 scheduler**。
- 测试：tests/test_intel.py（getter/LLM 全注入，hermetic）。改 intel.db schema 必须升 SCHEMA_VERSION + 幂等迁移（DDL 全 IF NOT EXISTS）。
