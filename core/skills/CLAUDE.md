# core/skills/

> 正交分类学（DESIGN.md §4.5）：能力包 × 场景轨。物理布局见 [packs/CLAUDE.md](../../packs/CLAUDE.md)。
> 评分/解析全部只读 packs；**唯一写入例外是 `writing.py`**——packs 受控写原语（进程写锁），只由 core API 端点在临界区内调用，评分/路由路径不得调它。

## 文件

| 文件 | 职责 |
|------|------|
| `writing.py` | packs 受控写入：`pack_write_lock()`（模块级 **RLock**，同线程可重入）。所有 packs 写端点/提案应用在临界区内；**单进程设计**，多进程需换文件锁/Postgres。**kb 本地基线（C 批已落地）**：`resolve_kb`（中文多级目录允许、仅 .md、拒 `..`/绝对/`<>:"|?*`/空字节/.history、多源按真实落点归属、新建默认第一源）、`list/write/delete/rename_kb_move/kb_versions/kb_version_path/rollback`，备份布局 `kb/.history/kb-backups/<ts>[.n]__<打平路径>.bak`、删除进 `kb-trash/`、**1 MiB 上限**、`skill_file_path`、`json_dump_atomic`；通用 `backup_history/backup_and_write/trash_move`。 |
| `refs.py` | **kb 引用只读扫描+改名联动（C3）**：`snapshot_index`（doctor 旧实现已迁入）、`extract_modules`（统一 `kb_open(module=...)` 与引号/反引号完整路径两形态）、`find_module_refs`（扫全部技能+各源 kb md，排除 .history）、`rewrite_module`（全路径整串替换，技能走 `.history` 备份、kb 文档走 kb-backups；相对 md 链接无法自动改→`skipped_relative`）。 |
| `proposals.py` | **统一变更提案（C4）**：`packs/.proposals/pp_<UTCts>_<6hex>.json`，三态 pending/approved/rejected；**不存旧快照**，`live_diff` 按审批时磁盘实时算；AI 技能仅 edit、kb 四模式；`create_proposal` 落地前模拟校验（非法不建文件），`apply_proposal` 应用前再校验+复用 writing 备份/rename 联动，只认 `decided_by=human`/`demo-script(auto)`；`ProposalError`→422、`ProposalStateError`→409/404。审计事件由调用方经黑板发。 |
| `taxonomy.py` | 绑定模型：`LEGACY_DOMAIN_MAP`（pentest→assessment+[web]，ctf→ctf+[binary]）、`project_binding(meta)`、`list_packs(root,kind)`（读 pack.yaml/track.yaml 的 label/description）、`load_task_types(root,track)`（行式 `<type>: <noise>`，generic 恒内置 passive） |
| `registry.py` | `SkillRegistry.load()` 扫 `capabilities/*/skills/*/SKILL.md` 与 `tracks/*/skills/*/SKILL.md`；`SkillMeta` 带 kind（capability/track）、pack、labels（platforms/formats/vuln_classes 合并）、`enabled`（frontmatter，缺省 true）；极简 frontmatter 解析（无 PyYAML 依赖）；**kb/ 快照里的 SKILL.md 不扫** |
| `router.py` | `route(...)`；评分 **features/file_features/标签 ×3 > keywords ×2 > description ×1**；packs 集合 = 启用 caps ∪ {track}；角色白名单先窄化；`enabled:false` 默认排除。`RoutedSkill.breakdown`（C5）返回分类命中明细供试算器展示 |
| `roles.py` | `load_role(root, track, name)` 读 `tracks/<track>/roles/<name>.yaml`（极简 YAML：平铺+内联列表+null，解析剥离行内注释）；缺角色回退 `_generalist`。字段：description/persona/skills/task_types/default_noise/tools/max_runtime/max_steps |
| `rules.py` | 规则链：`pack_rules(root, capabilities, track)`（能力包 ∪ 轨的 `rules/*.md`，**glob 不递归**）、`owner_rules(root, track, owner_tags)`（owners/）、`role_rules(root, track, role)`（role-rules/，仅该角色）、`load_kb_sources(root, caps)`（每包 kb_sources.json，坏 JSON 跳过）、`build_rules_preamble(...)` |
| `doctor.py` | `diagnose(root)` 只读体检（CLI/`GET /api/packs/doctor` 共用）：error=角色引用不存在技能/未注册 task_type/frontmatter name 不一致/kb_sources root 缺失；warning=引用已禁用技能/缺 redlines 或 task_types.yaml/kb 引用失效（复用 refs.snapshot_index）/坏 JSON；info=孤儿技能、近重复技能（C5：Jaccard≥0.6 且共有词≥2）、`.history/trash` 非空 |

## 约定与坑

- 平台/格式/漏洞类**不建包**，只走 frontmatter 标签（platforms/formats/vuln_classes）+ file_features 路由加权。
- `rules/*.md` 不递归是有意设计：owners/、role-rules/ 借此天然分层，不自动注入。
- 角色 yaml 字段全是软边界：skills/task_types 为 null = 不过滤（不是拒绝一切）。
- 改路由权重/标签词表先改 DESIGN.md；测试护栏在 tests/test_skills.py（真实包断言 + 夹具评分断言）。
- 跑测试：`E:\Miniconda3\python.exe -m pytest tests/test_skills.py -q`（先设 PYTHONIOENCODING=utf-8）。
