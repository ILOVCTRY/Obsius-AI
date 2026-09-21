# 方案：项目规则四段一体（轨默认 → 模板库 → 项目实例三级模型）

- **状态**：讨论收敛，待打磨（2026-09-21）
- **拍板记录**：见 §3（3 项决策已确认）
- **关联代码**：`packs/tracks/*/rules/`（redlines/owners/rating/role-rules）、`core/api/app.py`（规则注入/判据组装）、`core/orchestrator/`（mission 判据与态势）、`core/skills/`（提案制/doctor）
- **实施后**：定稿决策沉淀回 `DESIGN.md`，本文保留作方案背景

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

## 5. 待打磨清单

1. frontmatter 字段 schema 定稿 + 校验器（doctor 体检项：字段类型、scope 可解析、rating_ref 存在）。
2. 模板选择器机制：owner 标签匹配规则、多标签叠加语义。
3. 用户级模板存放位置与 API（复用 judgments 用户模板层的具体形态）。
4. 项目规则实例存储：project.json 内联 vs workspace 独立文件。
5. 机器消费点改造优先级（建议编排器 forbidden 收窄先行，红利最直观）。
6. `role-rules/<role>.md` 去留：并入专家 persona（衔接 [expert-pool.md](expert-pool.md)）还是保留。
7. 注入组装点改造：现 redlines 全量注入代码路径与新三级的合成顺序。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 schema + 模板库**：frontmatter 解析 + doctor 体检 + templates/ 迁移（osrc 四段一体、edu-rating 残留清理）。
- **M2 项目规则实例**：存储 + 三级注入 + 前端规则一页。
- **M3 机器消费**：编排器 forbidden 收窄 → 门控/分类器 → 报告。
