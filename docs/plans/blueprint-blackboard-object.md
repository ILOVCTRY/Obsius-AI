# 方案：蓝图（blackboard 一等知识对象）

- **状态**：讨论收敛，待打磨（2026-09-21）
- **拍板记录**：见 §3（6 项决策已确认）
- **关联代码**：`core/blackboard/schema.py`（func_kb 表先例 :143、chains/chain_links、chain_active 先存后用先例 :61）、`core/blackboard/graph.py`（board_graph 9 边）、`core/orchestrator/`（任务派发）、`packs/tracks/research/`（blueprint/reconstruct task_type、rebuilder、blueprint-rebuild 技能、redlines spec 纪律）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§12 黑板本体定稿块旁新增蓝图小节），本文保留作方案背景

## 1. 愿景与背景

蓝图 = 样本业务逻辑的**结构化拆解**：模块划分（按业务职能）+ 每模块接口 spec + 模块间依赖关系 + 代码锚点。定位一句话：**func_kb 是函数级知识，蓝图是模块级知识**——黑板本体正好缺的上一层。

**三类消费方**（作为通用本体的回报）：

- **逆向开发重组**：蓝图钉死 → reconstruct 按模块领单 → 重组代码 + 自测；
- **游戏 mod**：拆解游戏逻辑 → 模块行 `mod_action` 变更集（keep/replace/hook）→ mod 包 + 制作方法存档；
- **病毒分析**：结构理解辅助行为解读（衔接沙箱行为报告专题——行为报告看到「读配置文件」，蓝图告诉你那个函数属于 C2 模块）。

**已有地基（R4 2026-09-20）**：research 轨 `blueprint` / `reconstruct` task_type（全 passive）、`rebuilder` 角色、`blueprint-rebuild` 轨技能；redlines 三条直接对应：「模块划分按业务职能+接口 spec **先钉死**（spec 错了写 notes 上报勿自行改）」「`module:<名>` risk_tag 归属」「自测不过不得标 tested」。

## 2. 现状盘点（2026-09-21）

- func_kb 已是一等知识表（UNIQUE(project_id, binary_sha256, address)，name_history JSON 演变史先例）——蓝图照此范式建模块级表。
- spec 无处落：现散在 notes/事件流，无结构化载体。
- reconstruct 任务无模块粒度：领单按任务不按模块，`module:<名>` risk_tag 只是标签约定。
- board_graph 9 种边无蓝图概念。
- mod 配方无载体（用户明确点名：游戏 mod「拆解部分游戏逻辑，然后存档 mod 如何制作」）。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 数据模型 | **黑板一等节点**：新表 `blueprints` + `blueprint_modules` + `blueprint_anchors` + `blueprint_edges`；spec 全文用文档附着，不全结构化 |
| D2 | 确认点 | `draft → 人工确认 → confirmed 冻结`；Agent 改 spec 仅 notes 上报（红线落到机制）；`rebuilt`（有代码产出）→ `selftested`（前置自测任务 verified） |
| D3 | 粒度 | **一样本一张主蓝图**（binary_sha256 定格）+ 版本历史；多拆解视角后置 |
| D4 | 产出流转 | blueprint 任务产 draft → 人工 confirmed → reconstruct 按模块领单 → 自测 verified → selftested；蓝图进 board_graph（新增 `module_of` / `module_depends` 边） |
| D5 | func_kb 锚定 | 模块锚 func_ids（anchors 表多对多）；`module:<名>` risk_tag 沿用；func_kb 增量更新 → **锚点漂移提示** |
| D6 | mod 配方 | **蓝图快照 + 变更集**：模块行 `mod_action` 注记（keep/replace/hook+说明）；mod 独立机制等真做游戏 mod 项目再沉淀（YAGNI） |

## 4. 设计详述

### 4.1 表草案（对齐 func_kb 范式）

```sql
CREATE TABLE IF NOT EXISTS blueprints (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id),
    binary_sha256 TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft / confirmed / superseded（主档状态）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(project_id, binary_sha256)              -- D3：一样本一张主蓝图
);

CREATE TABLE IF NOT EXISTS blueprint_modules (
    id            TEXT PRIMARY KEY,
    blueprint_id  TEXT NOT NULL REFERENCES blueprints(id),
    name          TEXT NOT NULL,                   -- `module:<名>` risk_tag 沿用此名
    duty          TEXT NOT NULL DEFAULT '',        -- 业务职能描述
    spec_note     TEXT NOT NULL DEFAULT '',        -- 接口 spec 摘要
    spec_doc      TEXT NOT NULL DEFAULT '',        -- spec 全文指针（载体 → 待打磨 #2）
    spec_history  TEXT NOT NULL DEFAULT '[]',      -- JSON：spec 演变史（同 func_kb.name_history 先例）
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft / confirmed / rebuilt / selftested
    mod_action    TEXT NOT NULL DEFAULT '',        -- D6 预留：keep / replace / hook（先存后用，照 chain_active 先例）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(blueprint_id, name)
);

CREATE TABLE IF NOT EXISTS blueprint_anchors (    -- D5：模块 ↔ func_kb 多对多锚点
    id         TEXT PRIMARY KEY,
    module_id  TEXT NOT NULL REFERENCES blueprint_modules(id),
    func_id    TEXT NOT NULL REFERENCES func_kb(id),
    created_at TEXT NOT NULL,
    UNIQUE(module_id, func_id)
);

CREATE TABLE IF NOT EXISTS blueprint_edges (      -- D4：模块依赖
    id          TEXT PRIMARY KEY,
    blueprint_id TEXT NOT NULL REFERENCES blueprints(id),
    from_module TEXT NOT NULL,
    to_module   TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'calls',     -- 语义集合 → 待打磨 #1
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
```

### 4.2 状态机与确认流（D2）

- 模块状态：`draft → confirmed → rebuilt → selftested`；
- **confirmed = 人工冻结**（确认交互形态 → 待打磨 #3）：此后模块的 spec 写路径在 API 层收窄，Agent 只能 notes 上报（红线「spec 错了写 notes 上报勿自行改」从散文变校验）；
- `rebuilt`：关联 reconstruct 任务有代码产出；`selftested`：前置自测任务 verified（「自测不过不得标 tested」落机制）；
- 主档 `status=confirmed` 为整图冻结门槛（有未 confirmed 模块时主档不可 confirmed）。

### 4.3 func_kb 锚定与漂移（D5）

- anchors 表锚 func_kb.id；func_kb 行更新/新增（重命名、修正分析、新函数）时检查所属锚点模块 → **漂移提示**（实现层 → 待打磨 #4：doctor 体检 / 任务收尾检查 / 写入时同步检查）。

### 4.4 board_graph 集成（D4）

- 新边：`module_depends`（模块→模块，取 blueprint_edges）、`module_of`（func_kb→模块，取 anchors）；
- 蓝图节点进发现链路画布，与 finding/asset 同图；节点点击进蓝图视图。

### 4.5 mod 配方（D6，应用层预留）

- `mod_action` 列随批预留不启用；真做游戏 mod 项目时启用并沉淀 mod 导出/版本管理机制；
- 届时 mod 配方 = 蓝图快照（sha 定格天然存在）+ 变更集（各模块 mod_action）。

### 4.6 与功能面/场景档衔接（承接 expert-pool 方案 §4.5）

- 蓝图 = research 轨**功能面**；场景档 deliverables 引用（逆向开发档交付清单：蓝图 + 重组代码 + 自测结果）。

## 5. 待打磨清单

1. `blueprint_edges.kind` 语义集合（calls / data / order？）最小起步。
2. spec 全文载体：artifact vs workspace 文档 vs func_kb notes。
3. 确认交互形态：审批收件箱单 vs 工作台蓝图页按钮。
4. 锚点漂移提示实现层（doctor 体检 / 任务收尾 / func_kb 写入时同步）。
5. 前端蓝图视图：模块树/依赖图（复用发现链路图组件 @xyflow），承载位置与蓝图列表（多样本项目）。
6. API 形状：蓝图/模块 CRUD、confirm、按模块领单端点；confirmed 写路径收窄的校验位置。
7. 版本历史粒度：spec_history JSON 记什么、主档 superseded 语义。
8. 编排器口径：reconstruct 任务与模块行状态联动、selftested 前置校验的实现位置。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 表 + API 最小闭环**：四表迁移 + 蓝图/模块 CRUD + draft→confirmed 确认 + confirmed 写路径收窄。
- **M2 编排衔接**：reconstruct 按模块领单 + 状态联动 + selftested 前置校验 + 锚点锚定。
- **M3 图与前端**：board_graph 新边 + 蓝图视图（模块树/依赖图）。
- **M4（预留）**：mod_action 启用 + mod 应用层。
