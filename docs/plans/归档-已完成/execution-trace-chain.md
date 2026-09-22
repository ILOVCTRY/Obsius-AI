# 方案：执行轨迹链路（意图-产出-过程时间链）

- **状态**：已实施（2026-09-22 M1+M2+M3 全量：traces.py 双轨 / v19 两列 / _finish+verified 双触发 / graph origin 过滤 / 三端点 / 前端轨迹段+主线切换档+效果榜；测试 14 用例 + 全量回归通过）
- **拍板记录**：用户拍板 D1-D4 见 §3；打磨定稿补充决策 R1-R7 见 §3.1（本轮直接消化 §5 全部待打磨项）
- **关联代码**：`core/blackboard/traces.py`（双轨核心，实施新增）、`core/blackboard/tasks.py`（_finish done 物化钩子）、`core/blackboard/store.py`（v19 两列 + patch/add verified 重物化钩子）、`core/blackboard/graph.py`（origin='manual' 过滤）、`core/api/app.py`（trace / trace-effect 端点）、`webui/src/components/blackboard/TaskTraceList.tsx`、`webui/src/views/blackboard/boardGraph/MainlineView.tsx`
- **实施后**：定稿决策已沉淀回 `DESIGN.md` §三「执行轨迹链路」定稿块，本文保留作方案背景

## 1. 愿景与背景

审计视角的时间链：直接看到 AI 在工作中**技能、知识库、工具**调用的先后顺序，知道 AI 尝试过什么——后续审计方便，也方便把有效的尝试沉淀为打法和知识。

与发现链路图（`board_graph`，DESIGN.md §12 定稿块）**互补**：

- **发现链路 = 结果空间**：资产-发现-知识的因果网（为什么得出这个结论）；
- **执行轨迹 = 过程空间**：任务时间序（AI 是怎么一步步干活的）。

承接 §12 讨论中已收敛未落盘的三条链编排口径（本方案一并收编）：意图与产出交替为优不强制、链头建议放意图、前端 task 节点给意图视觉角色——即「**有据可查的混合叙事**」。

## 2. 现状盘点（2026-09-21 逐点核实）

**已有数据源（零新增 emit 点）：**

- `skill.routed` 审计事件：任务路由时落一条，**payload 直挂 task_id**（含 task_type/query/kb_hits/route_points），未命中 `name=null`——`loop.py:636/:705`
- `kb.open`：payload `{source, module, path}`，session_id 一等列，**不带 task_id**——`tools.py:1276`
- `command / command.result`：执行网关配对落事件，payload `{call_id, cmd, runtime, exit_code, ...}`，**不带 task_id**——`gateway.py:180/:206`
- `tool.call`：非命令工具统一审计（run_cmd 除外不重复落），payload `{name, args, ok, duration_s, result_head}`——`tools.py:859-876`
- `task.claimed / task.done / task.failed`：payload 带 `task_id + session_id`——`tasks.py:402-412` / `_finish` :608-619
- `kb_open_sequence` 聚合（沉淀提案组装处，kb_opens 最近 50 条扫 command/command.result/skill.routed）——`app.py:5443-5491`（2026-09-21 行号）
- `chains` 表（hypothesis/validated/exploited）+ `chain_links` 表（**node_type 无 CHECK 约束**，注释仅列 finding/func_kb/artifact——写新值零迁移）——`schema.py:159-177`
- `chains.chain_active` 列（批 5 自动链预留，先存后用）

**关键口径事实（打磨调研定论）：**

- events 表 `session_id` 是一等列；command/tool.call/kb.open 均不带 task_id → **任务归属只能从任务生命周期事件推导**（→ R1）
- 一个会话同时只持一个任务（`AgentSession.current_task_id` 单值；`_finish` 校验 claimed_by 持有）→ 会话内任务区间**不嵌套**，时间切分良构
- `tasks.claimed_by` 在 done/failed 后保留最后认领者，但 reopen 重跑可换会话 → 纯 claimed_by 归属历史不可靠，**事件切分是唯一可靠口径**
- 迁移模式先例：`V5_ORCH_STATE_COLUMNS` 幂等 ALTER（`schema.py:57`）

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

### 3.1 打磨定稿补充决策（2026-09-21，逐项消化 §5 待打磨清单）

| # | 打磨点 | 定稿结论 |
|---|--------|----------|
| R1 | 事件→任务归属 | **会话内事件按 events.id 时间序，用该会话的 `task.claimed → task.done/task.failed` 区间切分**：区间内 command/tool.call/kb.open/skill.routed 归属该任务（skill.routed 自带 task_id 作交叉校验）；产出节点归属同口径（finding.new 事件落于任务区间）。区间外活动（chat 轮命令、等待期）= **会话游离段**，轨迹视图聚为「未挂任务活动」段，不进任务链。**零 emit 点改动** |
| R2 | 聚合规则 | 技能 = 每任务一节点（skill.routed 天然一条，未命中 name=null 记灰显反例节点）；kb = 同任务区间内同 module 归并一节点 + 打开次数；工具组 = 同任务区间内相邻两条工具事件间隔 ≤ `TRACE_TOOL_WINDOW_S`（常量 120s，可调）并组，组名 = 首工具名 + 「N 次」，命令组显 cmd 首行；**跨任务切分由 R1 区间边界天然保证，组不跨任务** |
| R3 | 物化时机与幂等 | **双触发**：①任务收尾 done（`TaskQueue._finish` done 分支后，try/except 旁路不挡收尾，同 task_receipt 先例）；②finding status→verified（`store.patch_finding` 路径重物化所属任务轨迹并升级链状态）。物化形态 = **每任务自动建一条链**（`chains.origin='trace'` 新列，name=objective[:60]），过程节点物化为 chain_links（node_type=`step`，node_id=聚合组稳定 id）；幂等键 = `chain_links.trace_ref` 新列（=`task-<task_id>`，人工链空串）→ 重物化 = 单事务 `DELETE WHERE trace_ref=?` + 重插。schema 增量 = V6 两列（chains.origin / chain_links.trace_ref，DEFAULT 兜底存量行） |
| R4 | 沉淀统计基于哪侧 | **物化侧为准**：链上 step/task 节点 × verified finding 关联统计——跨项目口径稳定、天然只算干完的任务（半途任务过程不进效果榜）；现算侧只管实时视图。正向效果榜 = (skill × kb 模块组合) × verified finding 计数 |
| R5 | 轨迹视图承载位置 | **任务详情内嵌「执行轨迹」段**（任务流 TaskFlow 节点详情浮卡 + 任务看板任务详情）——轨迹天然按任务切分（R1），任务详情是最自然入口；**不做项目级独立 tab** |
| R6 | node_type 扩展与 board_graph 兼容 | node_type 无 CHECK 约束 → 写 `task/step` **零迁移**（仅更新 DDL 注释 + 消费方容忍新值）；board_graph 兼容**双保险**——①step 节点非黑板实体，graph.py「两端不在节点集的段跳过」逻辑天然不出边；②chain 查询显式加 `WHERE origin='manual'`（一行防御，自动轨迹链永不进全景图）。前端零改动、**9 种边不新增** |
| R7 | 战果主线视图 | **board_graph 同页切换档**（全景 ↔ 战果主线两档，不新开 tab）；主线 = 过滤子图「目标资产 → verified 发现 → exploited 链」，unverified / false-positive 不上主线（与发现卡门禁口径一致）；布局新写**三列主线布局**（左目标 / 中发现 / 右链，不复用 flowModel 的任务 DAG 分列）；授权态不在本视图新增采集（board_graph 节点现无授权字段），pentest 分阶段工作流落地后若项目带授权态，主线头部顺带显项目级授权徽章，不阻塞本方案 |

## 4. 设计详述

### 4.1 双轨制（D3）

- **轨 A 轨迹视图（实时，现算）**：`GET /api/projects/{pid}/trace?task_id=...` 查询时现算——recent_events 回放 + R1 区间切分 + R2 聚合 → `{task, steps:[{kind: skill|kb|tools|finding, ...}], idle:[游离活动]}`，前端任务详情渲染时间链。**零写入、零 AI 参与、零效率影响**——实时性来自「读时组装」而非「写时串联」（用户明确否定任务收尾才串：看不到实时进度）。
- **轨 B 持久链（物化）**：R3 双触发钩子 → 自动链（origin='trace'）+ chain_links（node_type=task/step + trace_ref 幂等键），供报告叙事、人工标注修正、跨项目沉淀统计（R4）。物化是异步旁路，不挡主流程。
- **schema 增量（M2 落地时）**：`V6_CHAIN_TRACE_COLUMNS` 两列——`chains.origin TEXT NOT NULL DEFAULT 'manual'`、`chain_links.trace_ref TEXT NOT NULL DEFAULT ''`；新库 DDL 同步加列 + chain_links 注释更新为 `finding / func_kb / artifact / task / step`。存量行 DEFAULT 兜底，人工链零感知。

### 4.2 过程节点三类聚合（D1 + D2，细则=R2）

| 过程节点 | 数据源 | 聚合规则 |
|---|---|---|
| 技能命中 | `skill.routed`（payload 含 name/score/matched/route_points） | 每任务一节点（未命中 name=null 也记，灰显反例） |
| 知识库模块 | `kb.open` 按 module 归并 | 同任务区间同模块并一节点 + 次数 |
| 工具调用组 | `command/command.result`（配对按 call_id）+ `tool.call` | 同任务区间相邻间隔 ≤120s 并组；组名=首工具名+次数 |

链内完整形态（物化侧）：`[task 头节点] → step*（过程，按时间序）→ finding*（产出，区间内 finding.new）`；edge_note 自动填（如「任务区间内调用」「任务区间内产出」），人工可后补修正——与 board_graph 现行 edge_note 口径一致。

### 4.3 链叙事（承接 §12 口径）

- 意图（task 节点）与产出（finding 节点）**交替为优，不强制**；
- 链头建议放意图；task 节点在前端给**意图视觉角色**（与 finding 同图区分）；
- 自动轨迹链 status 只自动升到 `validated`（区间内存在 verified finding），`exploited` 仅人工链语义，不自动判。

### 4.4 沉淀飞轮

- **正向**（基于物化侧，R4）：(skill × kb 模块) 组合 × verified finding → **打法效果榜**（哪个组合出活多）→ 走提案制回写 route_index / SKILL.md。
- **反向**：K7 `route-index-zero-hit` 已有（长期注入过却从未命中的条目，供精简）——`app.py:5047-5069`。

## 5. 待打磨清单（已全部消化，2026-09-21）

原七项逐项定稿结论：#1→R1（归属切分）、#2→R2（聚合细则）、#3→R3（双触发+trace_ref 幂等）、#4→R4（物化侧为准）、#5→R5（任务详情内嵌）、#6→R6（零迁移+origin 防御）、#7→R7（主线切换档）。

## 6. 实施切分建议（已定稿，待用户排期）

- **M1 轨迹视图**（纯读，风险最低先行）：trace 现算 API（R1 切分 + R2 聚合，新模块 `core/blackboard/traces.py` 纯查询组装）+ 任务详情「执行轨迹」段时间链（R5）。
- **M2 持久链**：V6 两列迁移 → `_finish` done 物化钩子 + verified 重物化（R3）→ `graph.py` chain 查询 origin='manual' 防御过滤（R6）→ 战果主线切换档（R7，纯前端过滤 + 三列主线布局）。
- **M3 沉淀**：效果榜统计（基于物化侧，R4）+ 提案制回写闭环。

测试要点：M1 切分单测（claimed→done 区间归属 / reopen 双区间 / 游离段不混入）；M2 物化幂等（重物化不重复插链）/ verified 升级链状态 / board_graph 不含 origin='trace' 边；M3 效果榜计数口径。
