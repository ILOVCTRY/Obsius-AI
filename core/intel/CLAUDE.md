# core/intel/

> 情报面板（E9，DESIGN.md §16）：跨项目的**全局**学习/情报模块，数据不进任何 blackboard.db。存储在 `config/intel/`（intel.db + feeds.json + profile.json）。

## 文件

- `store.py` — `IntelStore(intel_dir="config/intel")`：SQLite WAL 全局库（SCHEMA_VERSION=1），表 `articles`（url UNIQUE 幂等入池，score/direction/is_priority/read/starred）+ `briefs`（date 主键，同日覆盖）。单连接 + 写锁（流量低，不照抄 Blackboard 线程局部连接）；`upsert_articles`（返回新增数，重复跳过不动打分/已读）、`unscored`/`apply_scores`、`list_articles(kind=/unread_only=/starred_only=)`（is_priority→score→fetched_at 排序）、`mark_article`、`save_brief`（INSERT ON CONFLICT 覆盖）/`get_brief`/`list_briefs`（不含全文）、`counts`。
- `config.py` — feeds.json（`load_feeds/save_feeds`，首访自动种子默认源：安全客/FreeBuf/看雪/先知/PortSwigger/arXiv cs.CR；坏 JSON 兜底回种子**不覆写盘上文件**）+ profile.json（`load_profile/save_profile`：七方向 web/ai/vehicle/reverse/android/pwn/forensics 权重 0–5 钳制 + stage 声明；手编漏项自动补默认）。
- `fetch.py` — 纯函数抓取层，`RawGetter = (url, timeout) -> (status, text)` **可注入（测试永不触网）**，默认 urllib 出站、**不经执行网关**（平台自身可信出站）。`parse_rss`（RSS 2.0 + Atom 通吃、HTML 剥离）、`fetch_nvd`（2.0 API 近 2 天新 CVE）、`fetch_kev`（CISA KEV 近一周新入 = is_priority）、`fetch_github_advisories`（免 key REST 近页；**实施注记**：设计写的 GitHub Advisory 落地走 api.github.com/advisories，GraphQL 要 token 放弃）、`fetch_all`（**单源失败只记 errors 不中断整批**）。
- `intel_service.py` — 打分与简报。`score_articles(items, profile, llm=None)`：is_priority 恒满分；LLM（classifier 路由，一次批量只喂标题+摘要 150 字符）→ 方向×分数，**profile 权重在侧乘（封顶 100）**；**LLM 任何失败降级 `_rule_score`（方向关键词×深度关键词，粗糙但可解释）**，score_detail.by 标 llm/rule/priority。`compose_brief`（LLM 中文编辑合成，失败降级 markdown 模板）、`run_refresh`（管线：抓取→入池→打分→简报落库，文章推送配比=热点 1–2 + 技术 3–5，返回 stats 含 new/errors/scored）。

## API（在 core/api/app.py，全局无项目前缀）

- `GET /api/intel/overview`（counts + 今日简报 + top_unread）、`GET|PUT /api/intel/feeds`、`GET|PUT /api/intel/profile`、`POST /api/intel/fetch`（JobRegistry Job kind=intel-refresh）、`GET /api/intel/briefs[/ {date}]`、`GET /api/intel/articles`、`PATCH /api/intel/articles/{id}`（read/starred）。
- 惰性建库：`app.state.intel` 首访问才落 config/intel/（测试传 `intel_dir=tmp`）；**测试注入口 `app.state.intel_getter` / `app.state.intel_llm`**（None = 真 urllib / classifier 路由）。
- `_intel_classifier()` 构建失败返回 None 降级规则，**不 503**（情报功能不依赖 LLM 在场）。

## 约定与坑

- 全局 DB 新模式先例：feeds/profile 写经 `pack_write_lock()` + `_pack_history_backup`（与 config/mcp.json 同锁同备份）。
- 抓取是触发式 Job（打开情报页今日无简报自动补跑一次 + 手动刷新），**无 scheduler**。
- 测试：tests/test_intel.py（getter/LLM 全注入，hermetic）。改 intel.db schema 必须升 SCHEMA_VERSION + 幂等迁移。
