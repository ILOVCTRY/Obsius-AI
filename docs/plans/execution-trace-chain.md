# 方案：执行轨迹链路（意图-产出-过程时间链）

- **状态**：讨论收敛，待打磨（2026-09-21）
- **拍板记录**：见 §3（4 项决策已确认；实时性双轨方案用户未反对，暂定通过）
- **关联代码**：`core/agent/loop.py`（skill.routed 审计事件）、`core/api/app.py`（kb_open_sequence 聚合）、`core/blackboard/schema.py`（chains/chain_links/chains.chain_active）、`core/blackboard/graph.py`（board_graph 发现链路）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§12 发现链路定稿块旁新增执行轨迹小节），本文保留作方案背景

## 1. 愿景与背景

审计视角的时间链：直接看到 AI 在工作中**技能、知识库、工具**调用的先后顺序，知道 AI 尝试过什么——后续审计方便，也方便把有效的尝试沉淀为打法和知识。

与发现链路图（`board_graph`，DESIGN.md §12 定稿块）**互补**：

- **发现链路 = 结果空间**：资产-发现-知识-工件的因果网（为什么得出这个结论）；
- **执行轨迹 = 过程空间**：任务时间序（AI 是怎么一步步干活的）。

承接 §12 讨论中已收敛未落盘的三条链编排口径（本方案一并收编）：意图与产出交替为优不强制、链头建议放意图、前端 task 节点给意图视觉角色——即「**有据可查的混合叙事**」。

## 2. 现状盘点（2026-09-21）

**已有数据源（核实为零新增）：**

- 每任务落一条 `skill.routed` 审计事件，未命中 `name=null`，payload 带 `route_points`——`core/agent/loop.py:634-724`
- `kb_open_sequence` 聚合（`kb_opens` 最近 50 条，扫 `command / command.result / skill.routed` 事件）——`core/api/app.py:5190-5237`
- `command / command.result` 事件已全程落 events 表
- `chains` 表（hypothesis/validated/exploited）+ `chain_links` 表（node_type 目前仅 `finding / func_kb / artifact`，**task 不在列——扩展点**）——`core/blackboard/schema.py:159-177`
- `chains.chain_active` 列（批 5 自动链预留，先存后用）——`schema.py:61`

**缺口：**

- 无「过程节点」本体（技能/知识库/工具组不进链）
- 无时间链组装与轨迹视图
- 沉淀飞轮（打法效果统计）无数据落点

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 节点构成 | **三类节点**：意图(task) + 产出(finding) + 过程节点（技能命中 / 知识库模块 / 工具调用组） |
| D2 | 粒度 | **聚合级**：工具调用并成组、kb 按模块归并、技能按命中计——不逐次 API 调用 |
| D3 | 实时性 | **记录与组装分离（双轨制）**：①轨迹视图 = 查询时现算，实时看进度，AI 零参与零效率影响；②持久链 = 收尾/verified 时后端钩子物化 chain_links，供报告与沉淀 |
| D4 | 落盘 | **单独方案**（本文件），不并入发现链路定稿块 |

## 4. 设计详述

### 4.1 双轨制（D3 核心）

- **轨 A 轨迹视图（实时，现算）**：`GET /api/projects/{id}/trace` 查询时从 events 表按时间序现算聚合，前端时间链展示。零写入、零 AI 参与、零效率影响——实时性来自「读时组装」而非「写时串联」（用户明确否定任务收尾才串：看不到实时进度）。
- **轨 B 持久链（物化）**：任务收尾 / finding verified 时后端钩子把当时的聚合结果物化进 chain_links（node_type 扩 `task` / `step`），供报告叙事、人工标注修正、跨项目沉淀统计。物化是异步旁路，不挡主流程。

### 4.2 过程节点三类聚合（D1 + D2）

| 过程节点 | 数据源 | 聚合规则 |
|---|---|---|
| 技能命中 | `skill.routed` 事件（route_points） | 每技能一节点（未命中 name=null 的也记，作反例） |
| 知识库模块 | kb_open 事件按 module 归并 | 同模块多次打开并为一节点 |
| 工具调用组 | `command / command.result` 事件 | 按 tool 名 + 连续时间窗归并为一组（窗宽阈值 → 待打磨 #2） |

### 4.3 链叙事（承接 §12 口径）

- 意图（task 节点）与产出（finding 节点）**交替为优，不强制**；
- 链头建议放意图；task 节点在前端给**意图视觉角色**（与 finding 同图区分）；
- 每条边带 `edge_note`（这条边为什么成立），与 board_graph 现行口径一致。

### 4.4 沉淀飞轮

- **正向**：(skill × kb 模块) 组合 × verified finding → **打法效果榜**（哪个组合出活多）→ 走提案制回写 route_index / SKILL.md。
- **反向**：K7 `route-index-zero-hit` 已有（长期注入过却从未命中的条目，供精简）。

## 5. 待打磨清单（下一轮细化消化）

1. 事件→任务归属口径：events 行如何可靠归到所属任务（session_id 推导？事件直挂 task_id？）。
2. 聚合规则细节：工具组合并阈值、kb 同模块合并、跨任务边界切分。
3. 物化触发时机（收尾 / verified / 报告前？）与幂等（重跑不重复插链）。
4. 沉淀统计基于哪侧：现算视图（全量历史）还是物化链（仅已验证任务）。
5. 轨迹视图承载位置：项目页独立 tab / 任务详情抽屉 / 与发现链路同页切换。
6. `chain_links.node_type` 扩 `task / step` 的迁移与 board_graph 兼容（9 种边是否新增 chain-step 类边）。
7. **战果主线视图**（2026-09-21 外部工具对比借鉴，DesRedTeam）：在 board_graph 全景与时间链之外，加一条**「目标 → verified 发现 → exploited 链」的简化叙事主线**——一眼讲完「从目标到战果」，兼作报告配图（report 阶段剧本直接引用）；只画 verified/exploited 干线，unverified 不上主线（与发现卡门禁口径一致）。打磨点：与 #5 承载位置的关系（独立视图还是主线切换档）、目标节点的授权态摘要是否顺带直显、布局复用 flowModel 还是新模型。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 轨迹视图**：trace 现算 API + 前端时间链（纯读，风险最低先行）。
- **M2 持久链**：node_type 扩展迁移 + 收尾/verified 物化钩子。
- **M3 沉淀**：效果榜统计 + 提案制回写闭环。
