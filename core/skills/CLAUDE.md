# core/skills/

> 场景轨 × 知识方向分类学（DESIGN.md §4.5）。物理布局见 [packs/CLAUDE.md](../../packs/CLAUDE.md)。
> **2026-10-13 能力包退役**：技能扁平 `packs/kb/skills/<name>/`，轨/红线并入 `packs/kb/`，项目 kb 方向改由专家 `knowledge` 白名单推导。
> 评分/解析全部只读 packs；**唯一写入例外是 `writing.py`**（packs 受控写原语，进程写锁），只由 core API 端点在临界区内调用。

## 文件

| 文件 | 职责 |
|------|------|
| `writing.py` | packs 受控写入：`pack_write_lock()`（模块级 RLock）。kb 本地基线 `resolve_kb`（K5 白名单 .md/.py/.txt/.json、拒 `..`/绝对/非法字符、1 MiB 上限）、list/write/delete/rename_kb_move/kb_versions/rollback、`skill_file_path`（扁平 `kb/skills/<name>/SKILL.md`）、`json_dump_atomic`；只读 `search_kb`（正文搜索 + 分面过滤）；通用 `backup_history/backup_and_write/trash_move`。 |
| `taxonomy.py` | 布局常量与路径助手：`kb_root`/`skills_root`/`skill_dir`/`track_dir`/`redline_path`/`platform_rules_dir`/`rating_dir`/`rule_templates_dir`；`list_directions`（kb 一级方向目录，排除保留目录与 TRACKS）、`list_packs(root,"track")`（读 `kb/<track>/track.yaml`，capability 恒空）、`load_task_types(root,track)`；`project_binding(meta)→(track, [])`（能力包退役，第二元素恒空）、`LEGACY_DOMAIN_MAP`/`LEGACY_TRACK_MAP` 读兼容。 |
| `registry.py` | `SkillRegistry.load()` 扫 `kb/skills/*/SKILL.md`；`SkillMeta` 只带 `name`/`path`（**无 kind/pack**）、labels、`enabled`、cc 资源清单。`all()` 只返回 load() 填充的缓存。 |
| `router.py` | `route(...)`；评分 features/file_features/labels ×3 > task_type +10 > 角色白名单偏好 +5 > keywords ×2 > description ×1。**技能扁平后无包过滤**（`packs` 参数保留兼容但为 no-op）；`RoutedSkill.breakdown` 供试算器展示。 |
| `matching.py` | 术语匹配共享原语 `term_matches`（ASCII 词边界 / 中文子串 / `*` 前缀），router/kbindex/routeindex 共用。 |
| `routeindex.py` | 测试点路由索引（全局单表 `packs/kb/route_index.yaml`）解析/缓存/域过滤/裁剪/渲染；`score_entry`/`top_route_entries`/`render_route_index_top`；`read_kb_module`。 |
| `kbindex.py` | legacy kb 检索索引（标题/分段/同义词/route.json）；只服务 `kb_search`/`kb_open` 与历史审计，新技能默认走 `skill_open`。 |
| `experts.py` | 专家池=运行时唯一角色源。`load_expert`（轨变体覆写）/`expert_exists`/`list_experts`；**推导面**：`expert_skills`（skills 硬选并集，null/空=不限定）、`expert_directions`（knowledge 方向白名单并集）、**`directions_effective`（原 caps_effective）**、`allowed_roles`。 |
| `profiles.py` | 场景档：`kb/<track>/profiles/<id>.yaml` 平铺五件套 + board_view；`list_track_profiles`/`load_profile`/`profile_snapshot`。物化即弃由 API 层执行。 |
| `rules.py` | 规则链：`pack_rules`（**轨红线** `kb/安全红线/轨红线/<track>.md`，能力包红线已退役）、`owner_rules`/`rating_rules`（**渗透独有**，`_PLATFORM_TRACK="pentest"` 门控）、`resolve_rule_profiles`（纯显式）、`load_rule_templates`（全局 `kb/安全红线/templates/`）、`load_kb_sources`（按方向合成源）、`build_rules_preamble`（轨红线 + 平台规则 + 评级硬指令；**无角色红线**）。 |
| `proposals.py` | 统一变更提案（`packs/.proposals/`，三态）；kb 四模式 + index（edit 增补）+ skill（edit/suggest，`_find_skill(root,name)` 扁平定位）+ case/pattern/playbook。`_validate_agent_kb` 机械 lint。 |
| `refs.py` | kb 引用只读扫描 + 改名联动（`snapshot_index`/`extract_modules`/`rewrite_module`/`classify_module`）。 |
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
- 跑测试：`E:\Miniconda3\python.exe -m pytest tests/test_skills.py -q`（先设 PYTHONIOENCODING=utf-8）。
