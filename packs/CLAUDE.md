# packs/

> 场景轨 × 知识方向（DESIGN.md §4.5 为唯一真相源）。**2026-10-13 能力包退役**：原 `capabilities/<cap>/` 全部并入 `kb/`，技能扁平化（无包归属）；项目绑定只剩「场景轨 track（单选）× 专家组队 experts（多选可空）」，kb 可见方向改由专家 `knowledge` 白名单决定。旧 `tracks/`、`capabilities/` 目录已删。

## 结构

```
packs/
├─ kb/                    # 唯一内容根
│  ├─ skills/<name>/      # 技能（扁平、cc 风格自包含）：SKILL.md + references/scripts/examples/assets
│  │                      #   registry 只扫 kb/skills/*/SKILL.md；kb 资料树里的 SKILL.md 不收
│  ├─ <track>/            # 场景轨 ctf/pentest/redteam/research：
│  │                      #   track.yaml / task_types.yaml / profiles/<id>.yaml（场景档）/
│  │                      #   phases/<phase>.yaml（阶段剧本）/ dimensions.yaml（测试维度）
│  ├─ <方向>/             # kb 知识方向 web/binary/crypto/forensics/misc/cloud（资料树，不进 registry）
│  ├─ 安全红线/
│  │  ├─ 轨红线/<track>.md        # 全轨全量注入（永久强制）
│  │  ├─ 平台规则/<tag>.md        # owners（资产 meta.owner 命中才注入；渗透独有）
│  │  ├─ 评级与价值口径/<tag>.md  # F11 判级依据（带硬指令注入；渗透独有）
│  │  └─ templates/<tag>.md       # 四段一体规则模板（pilot pentest/osrc）
│  ├─ route_index.yaml    # 测试点路由索引全局单表（条目 kb 全局形态 <方向>/<快照>/<路径>）
│  ├─ synonyms.yaml       # 领域同义词表（跨语言查询扩展）
│  └─ licenses/           # 快照上游 LICENSE 收编
├─ experts/<id>.yaml      # 专家池（运行时唯一角色源）：skills 硬选白名单 + knowledge kb 方向白名单
└─ data/                  # cdn_ranges.json 等随包基线数据
```

## 关键约定

- **项目绑定** = 场景轨（单选）× 专家组队（多选可空）。kb 可见方向 = `directions_effective`：有绑定专家→`knowledge` 并集（任一 null=全方向）；无绑定→盘上 `meta.capabilities` 直通（存量读兼容）；research 轨无绑定时按 `TRACK_DEFAULT_CAPS` 补 `binary`。
- **技能扁平、无包归属**：`kb/skills/<name>/SKILL.md`。专家 `skills` 白名单=**硬选**（会话可见/可路由技能=绑定专家白名单并集；`null`/空=不限定=全量）。路由不再按包过滤（`route(packs=…)` 参数保留兼容但为 no-op）。
- **专家 yaml 字段**（扁平单文件，与旧角色 yaml 全兼容）：`tracks` 缺省=全轨；`protected: true` 仅 `_generalist`；`variant_<track>_*` 轨变体字段级覆写；`skills`/`knowledge` 为 null 或空列表 = 不限定。加载/推导面 `core/skills/experts.py`（`load_expert`/`expert_exists`/`list_experts`/`expert_skills`/`expert_directions`/`directions_effective`/`allowed_roles`）。
- **平台规则 / 评级与价值口径为渗透（pentest）独有**（`rules._PLATFORM_TRACK` 门控）；轨红线全轨恒注入。
- **场景档（M4a）**：`kb/<track>/profiles/<id>.yaml` 平铺五件套（experts/rule_profiles_owners+rating/playbook/artifacts/knowledge）+ board_view；建项物化即弃（D6），此后只作预设不随项目联动。内置档：ctf full-squad/solo-generalist、pentest standard-engagement/recon-first、redteam full-chain/initial-access、research rev-workbench/code-audit。
- **阶段剧本（pentest M1/M2）**：`kb/<track>/phases/<phase>.yaml`（stem=阶段 id，name 中文）；项目 `config.phases` 同名整体覆写；引擎 `core/phases.py`。
- **测试维度（test-dimensions M1）**：`kb/<track>/dimensions.yaml`（id/name/applies_to/intent/evidence_hint）；项目 `config.dimensions` 同名覆写；加载与判定 `core/dimensions.py`。
- **kb 方向即资料树**：`load_kb_sources` 按方向合成 `packs/kb/<方向>`（恒递归，目录不存在=空源）；`kb_open` module 用全局形态 `<方向>/<快照>/<路径>`；域按源隔离。
- **`kb/route_index.yaml`**：全局单表；条目 kb 路径必须真实存在（doctor `route-index-kb-missing` 抓悬空）；注入按会话评分 Top-5，其余走 `route_lookup`；K7 回填 `route_points`，doctor 报 `route-index-zero-hit`。维护=人工直改或 index 提案（只能 edit 增补）。
- **专家池现状**：17 专家（含 `_generalist` protected 兜底、`chat-orchestrator` 工作台主控）。专家 `skills` 硬选白名单、`knowledge` 方向白名单；`_generalist`/`report-writer` 等 skills=null=全量。
- **技能/红线修改**：AI 只能落 `.proposals/`，人类审阅后应用（永不直写）；API 写端点走 `pack_write_lock` 临界区。
- **删除进 `.history/trash/`**（可恢复）；编辑备份两命名（`<ts>[.n]_<file>` 与 `<file>.<ts>.bak`），历史端点两种都认；回滚前自动再备份当前版。
- 再导入/更新快照只跑 `scripts/import_kb.py`（幂等，`--force` 原位覆盖）；勿手改 kb/ 内资料。

## 坑与注意

- 样本执行必须 docker/sandbox（各轨红线）；宿主 docker 未启动时 Agent 改道静态分析并标 unverified。
- 专家 `skills` 白名单引用的技能必须存在（doctor `expert-skill-missing` 抓悬空，零 error 才健康）；跨轨引用按各轨注册表并集体检。
- research redlines 是硬纪律：样本 untrusted、headless 只解析不执行、三层数据不混淆、反编译前先查 func_kb、发现挂 binary 资产且 evidence 带 func_id/address、无证据标 unverified、伪码不进事件流。
