# 方案：项目规则四段一体（轨默认 → 模板库 → 项目实例三级模型）

- **状态**：**打磨定稿（2026-09-23 夜间自主推进，7 项全消化见 §5），M1 已实施同日（实施记录见 §0），M2/M3 待排期**
- **拍板记录**：见 §3（3 项决策已确认）；打磨定稿见 §5（7 项）
- **关联代码**：`packs/tracks/*/rules/`（redlines/owners/rating/role-rules）、`core/api/app.py`（规则注入/判据组装）、`core/orchestrator/`（mission 判据与态势）、`core/skills/`（提案制/doctor）
- **实施后**：定稿决策沉淀回 `DESIGN.md`，本文保留作方案背景

## 0. M1 实施记录（2026-09-23，夜间自主推进）

- **解析器**（core/skills/rules.py）：`parse_rule_doc(path)`——frontmatter（PyYAML，`---` 围栏）+ 正文分离；frontmatter 缺失/不可解析返回空 meta + 原文（宁容错：散文层永远可注入，结构化是增量红利不是前置条件）；`validate_rule_meta(name, meta)`——字段类型校验（scope.in/out 字符串数组 / forbidden·uncollectable 字符串数组 / noise_caps 对象且值 ∈ {passive,low,medium,high} / rating_ref 字符串），坏字段 ValueError（入库声明出错 fail-fast，对齐 registry 惯例）。
- **模板库**：`load_rule_templates(packs_root, track)` 读 `tracks/<track>/rules/templates/*.md` → [{name, meta, body}]；首件 pilot `tracks/pentest/rules/templates/osrc.md`（owners/osrc.md + rating/osrc.md 四段一体合并：frontmatter 抽 scope.in 域名通配 / forbidden §10 硬禁 / uncollectable §6 白话清单，noise_caps 暂不填〔正文 §9 收敛纪律已覆盖，不臆造〕，rating_ref: self；正文=两文件全文合并保留原章节号）。redteam 轨 owners 文件与 pentest 有实质内容差异，迁移须按轨分别重排，随 M2 一并做。
- **doctor 体检**：`rule-template-invalid`（error，schema 校验不过）/ `rule-template-no-trigger`（warning，frontmatter 缺 trigger 或 trigger≠文件名——M2 选择器以 trigger 命中，缺了模板等于死件）。
- **edu-rating 残留清理**：`owners/edu-rating.md`（与 rating/edu-rating.md 正文逐字一致，仅 F11 头注差异）mv 入 `rules/.history/trash/`（回收站惯例，doctor info 报告，不真删文件）；`rating-without-owner` 体检注记同步（edu-rating 纯评级 tag 常驻此 warning 属预期，现状不变）。
- **注入面零改动**：M2 才动 build_rules_preamble 三级合成（模板库先落位、消费后切换——strangler 迁移，存量 owners/rating 流照旧工作）。
- **测试**：test_skills.py 增 6 用例（parse 好/坏 frontmatter、validate 四类坏字段、templates 加载与 pilot 实文件 schema 守卫、doctor 两条体检码、pilot frontmatter 内容守卫）。

## 1. 愿景与背景

轨、红线、owners、rating、判据五处规则**重复且全是散文**，合并为「**项目规则单文档四段**」：范围、规则、评级、判据。三级模型（轨默认纪律 → 模板库 → 项目实例）与 judgments 三级优先**同构**，机制可平移。

目标：

- 编排器/门控/分类器/报告**直接消费结构化规则**，不靠 LLM 读散文；
- 预制不同业主/场景模板，**开项目时选模板即得规则**，再按项目微调；
- 一页看懂项目规则全貌（现在分散 3-5 个文件）。

**结构化红利的实证**：本窗口删除 pentest 轨 privesc/lateral 角色 + 3 技能 + 3 task_types（因 owners 规则明令禁内网渗透/主机提权）是**手工同步**完成的——若有 `forbidden: [内网渗透, 主机提权]` 结构化字段，编排器读之自动收窄 task_type 与专家池候选，此类人工对齐消失。

## 2. 现状盘点与重复实锤（2026-09-21）

现存规则四处 + 判据一处：

| 位置 | 内容 | 注入方式 |
|---|---|---|
| `rules/redlines.md` | 轨级恒定纪律 | glob 全量注入 |
| `rules/owners/<tag>.md` | 业主授权边界 + 操作红线 | owner 标签选择 |
| `rules/rating/<tag>.md` | 评级与价值口径 | owner 标签选择 |
| `rules/role-rules/<role>.md` | 角色专属规则 | 绑定角色选择 |
| 判据 | mission 判据模板 | 项目手写 > 用户模板 > 轨内置默认 |

**重复实锤：**

1. `owners/edu-rating.md` 是 F11 残留——与 `rating/edu-rating.md` 同源重复，应删；
2. OSRC 规则被 F11 拆在 `owners/osrc.md` + `rating/osrc.md` 两文件，消费时要拼；
3. 三层规则全是散文，机器不可消费（见 §1 实证）。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 文件形态 | **frontmatter 结构化 + md 正文**：清单类字段（scope/forbidden/noise_caps/uncollectable）进 frontmatter 机器可读；判级条款等叙述性内容留 md 正文 |
| D2 | 模板库位置 | **轨下 `rules/templates/<tag>.md`**：owner 标签变模板选择器；用户自建模板复用 judgments 用户模板层机制 |
| D3 | 判据去向 | **并入项目规则成四段一体**（范围/规则/评级/判据）；judgments 三级优先（项目手写 > 用户模板 > 轨内置默认）平移进规则实例 |

## 4. 设计详述

### 4.1 三级注入模型

1. **轨默认纪律**：`redlines.md` 恒定注入 Agent system（现状不变）；
2. **模板库**：轨下 `templates/<tag>.md` 三段/四段一体；owner 标签匹配 → 叠加注入；
3. **项目规则实例**：项目级最终生效层，四段齐备（可从模板生成后手改）。

优先级 = 实例 > 模板 > 轨默认——与 judgments「项目手写 > 用户模板 > 轨内置默认」同构，机制平移。

### 4.2 四段与 frontmatter 字段草案

```markdown
---
scope:                          # ── 范围段
  in: ["*.osrc.com", "1.2.3.0/24"]
  out: ["mail.osrc.com"]        # 明确禁测
forbidden: [内网渗透, 主机提权, DoS, 社工]      # ── 规则段：硬禁清单
noise_caps: {recon: passive, exploit: low}     # ── 规则段：噪声上限
uncollectable: [DoS, 社工]                      # ── 规则段：不收类型（白交即拒）
rating_ref: osrc                # ── 评级段：指向模板或内联
---
## 判级条款（正文，叙述性）
高危：可直接登录后台的注入点……
```

### 4.3 机器消费链路（红利落地清单）

| 消费方 | 消费字段 | 行为 |
|---|---|---|
| 编排器 | `forbidden` | 自动收窄 task_type 与专家池候选（联动实证：privesc 类任务不再需要手工对齐） |
| 网关/门控 | `noise_caps` | 命令噪声校验引用 |
| 分类器 | `uncollectable` | 收到的发现自动过滤/标注白交类型 |
| 报告 writer | rating 段 + `scope.in/out` | 报告口径直接消费 |
| 前端 | 全部 | **规则一页编辑**（frontmatter 表单 + 正文编辑器），开项目时选模板即得 |

### 4.4 存量迁移

- 删 `owners/edu-rating.md`（F11 残留）；
- `owners/osrc.md` + `rating/osrc.md` 合并为 `templates/osrc.md` 四段一体；
- 存量项目：无实例时按 owner 标签从模板一次性物化生成实例。

## 5. 打磨定稿记录（2026-09-23，七项全消化）

| # | 打磨点 | 定稿 |
|---|--------|------|
| 1 | frontmatter schema + 校验器 | 定稿字段：`trigger`（模板 id，=文件 stem，owner 标签命中即选）/ `scope: {in, out}`（字符串数组，域名通配或 CIDR）/ `forbidden` / `uncollectable`（字符串数组）/ `noise_caps`（对象，键=task_type，值 ∈ {passive, low, medium, high} 噪声**上限**档）/ `rating_ref`（`self`=正文含判级条款 / 其它模板 id / 省=无评级段）。校验 fail-fast（坏字段 ValueError，对齐 toolchain registry 惯例）；**frontmatter 缺失容错**（散文永远可注入，结构化是增量红利） |
| 2 | 模板选择器 | owner 标签**精确匹配** trigger；多标签=多模板全部注入（注入序=标签序）；机器字段冲突消解**宁严勿松**——forbidden/uncollectable 取并集、noise_caps 逐键取最严档（passive<low<medium<high 取 min）、scope.in 取交集 scope.out 取并集；散文冲突沿「更严者为准」交 LLM 遵循（现状语义不变） |
| 3 | 用户级模板 | `config/rule_templates/<name>.md`（与轨模板同构 frontmatter+md，文件名=模板 id）——**不复用 judgments 的 JSON 表形态**（judgment_templates.json 存纯文本判据；规则模板是 frontmatter+正文复合体，JSON 存 md 别扭），复用的是其**层级位置**（全局用户层，介于轨模板与项目实例之间）；API 照 tracks owners「文件即规则」模式（list/PUT/DELETE） |
| 4 | 项目规则实例存储 | **workspace 独立文件** `workspaces/<pid>/project-rules.md`（四段一体最终生效层），不走 project.json 内联（大文本不进 config、schema 不膨胀）；写经 core API 单一入口（GET/PUT `/api/projects/{pid}/rules`）；开项目选模板 → 物化生成实例 → 手改；实例缺失=模板层生效（宁严勿松：实例出现即整体压过模板，不做字段级合并——「实例=终稿」心智简单可解释） |
| 5 | 机器消费优先级 | M3 顺序：编排器 `forbidden` 收窄 task_type/专家候选（红利最直观，手工对齐实证消失）→ 网关/门控 `noise_caps` 校验 → 分类器 `uncollectable` 白交过滤 → 报告 writer `scope`+评级段 |
| 6 | role-rules 去留 | **通道保留、零迁移**：盘点实证四轨均无 role-rules/ 存量文件（专家池已接管角色规则职责），`role_rules()` 读取函数与 doctor `role-rules-orphan` 检查原样保留——零成本兼容，未来若需角色级规则文件仍可落 |
| 7 | 注入组装点 | 三级合成序：轨默认 redlines（`_read_md_dir` 现状不动）→ owner 命中模板（`templates/` 优先，**legacy owners/rating 回退**——未迁移 tag 照旧工作，strangler 渐进）→ 项目实例（M2，最末注入压过一切）；F11 评级硬指令触发条件扩展：eff_ratings 非空 **或** 命中模板 `rating_ref` 存在 |

## 6. 实施切分

- **M1 schema + 模板库（已实施 2026-09-23，见 §0）**：frontmatter 解析 + doctor 体检 + templates/ 迁移（osrc 四段一体 pilot、edu-rating 残留清理）。
- **M2 项目规则实例（待排期）**：实例存储 + 三级注入切换（build_rules_preamble 合成序落地、legacy 回退）+ redteam 轨模板迁移 + 用户模板 API + 前端规则一页。
- **M3 机器消费（待排期）**：编排器 forbidden 收窄 → 门控/分类器 → 报告。
