# core/skills/

> 场景轨 × 知识方向分类学（DESIGN.md §4.5）。物理布局见 [packs/CLAUDE.md](../../packs/CLAUDE.md)。
> **2026-10-13 能力包退役**：技能扁平 `packs/kb/skills/<name>/`，轨/红线并入 `packs/kb/`，项目 kb 方向改由专家 `knowledge` 白名单推导。
> 评分/解析全部只读 packs；**唯一写入例外是 `writing.py`**（packs 受控写原语，进程写锁），只由 core API 端点在临界区内调用。

## 文件

| 文件 | 职责 |
|------|------|
| `writing.py` | packs 受控写入：`pack_write_lock()`（模块级 RLock）。kb 本地基线 `resolve_kb`（K5 白名单 .md/.py/.txt/.json、拒 `..`/绝对/非法字符、1 MiB 上限）、list/write/delete/rename_kb_move/kb_versions/rollback、`skill_file_path`（扁平 `kb/skills/<name>/SKILL.md`）、`json_dump_atomic`；只读 `search_kb`（正文搜索 + 分面过滤）；**技能目录原语（2026-10-14 提案 create/delete 落地用）**：`resolve_skill_dir`（校验 `_SKILL_NAME_RE` 后返回 `kb/skills/<name>/`）、`create_skill`（mkdir + 写 SKILL.md，frontmatter `name` 须 == 目录名）、`delete_skill`（移入 `kb/skills/.history/skills-trash/`，可恢复）；通用 `backup_history/backup_and_write/trash_move`。 |
| `taxonomy.py` | 布局常量与路径助手：`kb_root`/`skills_root`/`skill_dir`/`track_dir`/`redline_path`/`platform_rules_dir`/`rating_dir`/`rule_templates_dir`；`list_directions`（kb 一级方向目录，排除保留目录与 TRACKS）、`list_packs(root,"track")`（读 `kb/<track>/track.yaml`，capability 恒空）、`load_task_types(root,track)`；`project_binding(meta)→(track, [])`（能力包退役，第二元素恒空）、`LEGACY_DOMAIN_MAP`/`LEGACY_TRACK_MAP` 读兼容。 |
| `registry.py` | `SkillRegistry.load()` 扫 `kb/skills/*/SKILL.md`；`SkillMeta` 只带 `name`/`path`（**无 kind/pack**）、labels、`enabled`、cc 资源清单。`all()` 只返回 load() 填充的缓存。 |
| `router.py` | `route(...)`；评分 features/file_features/labels ×3 > task_type +10 > 角色白名单偏好 +5 > keywords ×2 > description ×1。**技能扁平后无包过滤**（`packs` 参数保留兼容但为 no-op）；`RoutedSkill.breakdown` 供试算器展示。 |
| `matching.py` | 术语匹配共享原语 `term_matches`（ASCII 词边界 / 中文子串 / `*` 前缀），router/kbindex/routeindex 共用。 |
| `routeindex.py` | 测试点路由索引（全局单表 `packs/kb/route_index.yaml`）解析/缓存/域过滤/裁剪/渲染；`score_entry`/`top_route_entries`/`render_route_index_top`；`read_kb_module`。 |
| `kbindex.py` | legacy kb 检索索引（标题/分段/同义词/route.json）；只服务 `kb_search`/`kb_open` 与历史审计，新技能默认走 `kb_open`。 |
| `kbsearch.py` | **现行** kb 全文索引（SQLite FTS5，库在 `<packs_root>/../data/kb-index.db`，schema v2）。`KbSearchIndex.search()` **每次调用先 `sync()`**——按 mtime/size+sha256 增量重建（源树仍是唯一真相，索引可随时重建）；`search_kb`（writing.py）是唯一生产消费者，`_tool_kb_search` 按能力逐方向各建一个实例 → 一次工具调用最多 N 次同步。**FTS 只用于纯 ASCII 查询**，含 CJK 的查询直接走 `content LIKE '%词%'` 全表扫（unicode61 不分中文词）。**性能红线（2026-10-14 修）**：`kb_documents_fts.path` 是 UNINDEXED 列，FTS5 对它 DELETE 只能全表扫 → 逐行 `DELETE ... WHERE path=?` 是 O(N²)。实测 643 篇 web：逐行删 3.86s / 批量删 0.46s；线上 1796 篇 235MB 库放大到 33s/方向，6 个方向合计让单个 `kb_search` 卡 **4 分钟**（Nan 报工具行一直「运行中」）。现改为先判定待写集合、再 `_delete_fts_rows` 一条 `IN` 批量清（400/批），全量重建 46s→6.5s、路径漂移 churn 92.5s→3.3s，热态同步 1.3s。**新增按 path 删 FTS 的代码一律走 `_delete_fts_rows`，勿写逐行 DELETE。** |
| `experts.py` | 专家池=运行时唯一角色源。`load_expert`（轨变体覆写）/`expert_exists`/`list_experts`；**推导面**：`expert_skills`（skills 硬选并集，null/空=不限定）、`expert_directions`（knowledge 方向白名单并集）、**`directions_effective`（原 caps_effective）**、`allowed_roles`。 |
| `profiles.py` | 场景档：`kb/<track>/profiles/<id>.yaml` 平铺五件套 + board_view；`list_track_profiles`/`load_profile`/`profile_snapshot`。物化即弃由 API 层执行。 |
| `rules.py` | 规则链：`pack_rules`（**轨红线** `kb/安全红线/轨红线/<track>.md`，能力包红线已退役）、`owner_rules`/`rating_rules`（**渗透独有**，`_PLATFORM_TRACK="pentest"` 门控）、`resolve_rule_profiles`（纯显式）、**`rating_profile_active(packs_root,track,rule_profiles)`（2026-10-16：项目是否勾选并激活「评级与价值口径」——勾选且文件存在才真，与注入同源，读取异常按未激活；判级依据硬闸与对话链 vuln_gate 接线的门禁）**、**`effective_rating_tags` / `validate_rating_basis`（2026-10-17：basis **引用**校验——必须含 `rating:<生效tag>` 或独立成词的 tag 名；点名 `*rating*.md` 旧副本、或只引红线/技能一律打回。治「非空但乱填」：实测有子专家写不存在的 `edus-rating-rules.md` 照样落库）**、`load_rule_templates`（全局 `kb/安全红线/templates/`）、`load_kb_sources`（按方向合成源）、`build_rules_preamble`（轨红线 + 平台规则 + 评级硬指令；**无角色红线**）。 |
| `proposals.py` | 统一变更提案（`packs/.proposals/`，三态：pending/approved/rejected）；kb 四模式 + index（edit 增补）+ **skill 四模式 `edit/suggest/create/delete`（2026-10-14 起允许 AI 新建/删除技能，`_find_skill(root,name)` 扁平定位；create=内容合法 + frontmatter name==目录名 + `writing.resolve_skill_dir` 不存在；delete=技能存在）** + case/pattern/playbook。`_validate_agent_kb` 机械 lint。**apply 分派**：skill create→`writing.create_skill`、delete→`writing.delete_skill`（进回收站），edit/suggest 原路径。 |
| `kbiteration.py` | **知识库迭代（2026-10-14 新增，智能体主控可选功能）**：`normalize_kb_iteration`/`kb_iteration_of`/`is_enabled`（项目 config 的 `kb_iteration.enabled`，缺省 False）。`signals_of(bb,pid,*,since_event_id,since_iso)` 按事件窗聚合本轮信号（tool_failures/repeat_failures/kb_refs/actions/new_findings/new_web_vulns）；`should_trigger(sig)→(bool,reason)` 规则触发（重复失败≥2 / 新 Web 漏洞>0 / 工具失败≥3 / kb 引用+失败≥2 / 动作≥25）；`run_iteration(...)` 规则命中后调 LLM 生成 kb/skill 提案（`create_proposal(origin="agent")`，≤5 条），**整体 try 包裹绝不抛**。消费点在 `core/chat/runtime.py` 轮末 daemon 线程。**开关语义（2026-10-15 起）=「Agent 提案总闸」**：`propose_pack_edit` 工具执行层也按 `is_enabled` 硬闸（关即拒、读失败按关），chat 工具面/提示/dispatcher 三处同口径剔除——关 = Agent 不提任何提案（轮末自动 + 主动沉淀全停）。 |
| `refs.py` | kb 引用只读扫描 + 改名联动（`snapshot_index`/`extract_modules`/`rewrite_module`/`classify_module`）。**三种引用形态**：`kb_open(module="…")`、引号/反引号里的完整路径、Obsidian wikilink `[[web/漏洞分类/sqli/手册|别名]]`（目标可省扩展名，`#锚点`/`|别名` 只是展示层）。`normalize_module(ref)` 是统一归一入口（kb_open/skill_open 的入参也先过它）；**只认全路径 wikilink**——kb 有 52 个 `手册.md`、30 个 `README.md`，裸名 `[[手册]]` 天然歧义，`classify_module` 直接忽略（不报错也不判活）。wiki 改名联动走 `_splice_wikilinks`（保留别名/锚点），其余整串字面替换。 |
| `doctor.py` | 只读体检：error=技能名实不符/专家引用悬空技能/未注册 task_type/`expert-generalist-missing`；warning=引用禁用技能/缺轨红线或 task_types.yaml/kb 引用失效/route_index 三类/`rating-without-owner`/`rule-template-*`/同义词/阶段剧本/场景档；info=孤儿技能/近重复/`.history` 待清理/`synonyms-missing`。**已删码**：`expert-skill-ambiguous`/`role-rules-orphan`/`kb-domain-missing`。 |
| `roles.py` | **已退役**（expert-pool M2）：仅留 `_parse_inline_value` 原语与历史夹具函数，生产链勿接。 |
| `judge.py` | 共享判定面 `judge_finding`（LLM 对照规则文本核对候选发现）。 |

## 约定与坑

- 平台/格式/漏洞类**不建包**，只走 frontmatter 标签（platforms/formats/vuln_classes）+ file_features 路由加权。
- 专家 yaml 字段全是软边界：`skills`/`knowledge` 为 null 或空列表 = 不过滤（不是拒绝一切）；空绑定清单在 `directions_effective` 语境 = 走 fallback/轨默认。
- `directions_effective(root, track, experts, fallback)`：有绑定→专家 knowledge 并集（任一 null=全方向）；无绑定→fallback（meta.capabilities）直通；fallback 空→`TRACK_DEFAULT_CAPS`（research→binary）。
- rating 是「评级与价值口径」（判级依据），平台规则是「接触程度红线」（授权边界）；同一 tag 可两处各有文件，doctor 只要求 rating 有同名 平台规则 配套（反向不要求）。
- `route_index.yaml` 维护路径：人工直改或 index 提案（只能 edit 增补，Agent 不直写，doctor 三类 warning 兜底）。
- 改路由权重/标签词表先改 DESIGN.md；测试护栏在 tests/test_skills.py、tests/test_experts.py、tests/test_doctor.py。
- **kb 内链口径 = Obsidian 全路径 wikilink**（2026-10-14）：`packs/kb` 是 Obsidian vault，vault config `newLinkFormat=absolute` + `useMarkdownLinks=false`。人/编辑器/Obsidian 写库内链接一律 `[[web/漏洞分类/sqli/手册|别名]]`。存量相对 md 链接（`x.md` / `../a/b.md`）已由 `scripts/kb_links_to_wikilinks.py` 一次性抹平（689+56 条）；**不转这些**：代码围栏与行内代码（kb 里大量 Lua/Python/JSON 的 `[x](y)` 文本）、图片 `![](...)`、外链、`*/题目知识/*` 上游快照原件（与 ctf-skills 仓库字节一致，更新走 `scripts/import_kb.py --force`）、`.obsidian/` 与 `*/.history/`。护栏：`tests/test_kb_links_wikilink.py`（存量不得再残留可解析 md 链接 / 裸名 wikilink）+ `tests/test_kb_refs_wikilink.py`（读取层认 wikilink）。仍有 141 条历史断链未修（解析不到 vault 内文件），需人工，勿猜目标。
- **`kb_open` 的 module 与 `skill_open` 的 path 同口径收口**：反引号/引号包裹、wikilink 都先过 `refs.normalize_module`。旧布局链接 `../../capabilities/<域>/skills/<名>/SKILL.md` 与 `../../../tracks/<轨>/skills/<名>/SKILL.md` 确定性回填到 `skills/<名>/SKILL.md`（能力包扁平化后同一份正文就在 vault 内）。
- 跑测试：`E:\Miniconda3\python.exe -m pytest tests/test_skills.py -q`（先设 PYTHONIOENCODING=utf-8）。
