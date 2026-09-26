# 方案：蓝图（blackboard 一等知识对象）

- **状态**：**实施挂起——发现与 R4 现网实现冲突，待用户拍板**（2026-09-23 实施前盘点；定稿本身不变：2026-09-21 讨论收敛 6 项 + 2026-09-22 打磨八项 + 2026-09-23 方向性 4 项拍板通过）

> ⚠ **实施挂起（2026-09-23 夜间盘点发现，未拍板不实施）**
>
> schema v17 已存在 R4 逆向开发蓝图的**同名表与同前缀 API**：
> - `blueprints` 表（v17）：模块 JSON 内嵌单行（modules 列）、状态机 draft/reviewed/ready/building/built、UNIQUE(project_id, binary_sha256, name)、`goal`/`content_md` 列；
> - API `/api/projects/{pid}/blueprints` 五端点（list/create/get/patch/patch-module，app.py :2393-2440）；
> - 前端 ReverseWorkbench / BlueprintsView（reverse/blueprints/）；Agent 工具 bb_blueprint_*（create/content/module/status）。
>
> 与本方案冲突点：①§4.1 新表同名 `blueprints`——CREATE IF NOT EXISTS 不会建新表，列不兼容必炸；②§4.7 API 前缀相同——路由直接撞车；③状态机/语义不同（本方案 draft/confirmed/rebuilt/selftested vs R4 五态）。
>
> **实质**：本方案是 R4 蓝图的升级换代（内嵌 JSON → 四表一等对象），但方案未写迁移与兼容章节（旧数据迁移、BlueprintsView 与 bb_blueprint_* 工具去留、R4 五态→新四态映射）。属重大改向，须用户拍板后补 §迁移 再实施：
> - 选项 A：R4 模型整体迁移到四表（store / API / 前端 / Agent 工具四面重写 + 旧数据升级 SQL）；
> - 选项 B：新四表换名并存（与 R4 蓝图区分命名），消费面逐步切换；
> - 选项 C：维持 R4 现状，本方案仅吸收其确认流/锚点漂移设计增量迭代。

- **拍板记录**：§3（6 项决策 2026-09-21）+ §5（打磨轮八项定稿 2026-09-22，✅=已落 §4；方向性 4 项 2026-09-23 拍板）
- **关联代码**：`core/blackboard/schema.py`（func_kb 表先例 :143、chains/chain_links、chain_active 先存后用先例 :61）、`core/blackboard/tasks.py`（tasks.context 结构化载荷 :131-213、reconcile done 硬闸 :543、`_finish` 终态唯一写点 :632——状态联动钩子挂点）、`core/blackboard/graph.py`（board_graph 9 边）、`core/orchestrator/`（任务派发）、`webui/src/views/Blackboard.tsx`（黑板页页签——「蓝图」页签承载位）、`webui/src/views/blackboard/FindingsCanvas.tsx` + `boardGraph/`（@xyflow 画布复用源）、`packs/tracks/research/`（blueprint/reconstruct task_type、rebuilder、blueprint-rebuild 技能、redlines spec 纪律）
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
    status        TEXT NOT NULL DEFAULT 'draft',   -- draft / confirmed（主档两态；superseded 退役 → 待打磨 #7）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(project_id, binary_sha256)              -- D3：一样本一张主蓝图，重拆解=原行演进
);

CREATE TABLE IF NOT EXISTS blueprint_modules (
    id            TEXT PRIMARY KEY,
    blueprint_id  TEXT NOT NULL REFERENCES blueprints(id),
    name          TEXT NOT NULL,                   -- `module:<名>` risk_tag 沿用此名
    duty          TEXT NOT NULL DEFAULT '',        -- 业务职能描述
    spec_note     TEXT NOT NULL DEFAULT '',        -- 接口 spec 摘要（列表行展示）
    spec_doc      TEXT NOT NULL DEFAULT '',        -- spec 全文 markdown **直接入库**（载体定稿 → 待打磨 #2）
    spec_history  TEXT NOT NULL DEFAULT '[]',      -- JSON 演变史（结构 → 待打磨 #7；同 func_kb.name_history 先例）
    notes         TEXT NOT NULL DEFAULT '[]',      -- JSON：Agent 上报追加（confirmed 后唯一可写字段，红线机制化 → 待打磨 #6）
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
    kind        TEXT NOT NULL DEFAULT 'calls',     -- 'calls' | 'data'（集合定稿 → 待打磨 #1）
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
```

### 4.2 状态机与确认流（D2）

- 模块状态：`draft → confirmed → rebuilt → selftested`；转移合法性 store 层校验（rebuilt 只能来自 confirmed、selftested 只能来自 rebuilt——「自测不过不得标 tested」落机制）；
- **确认交互 = 黑板页蓝图页签按钮**（待打磨 #3 定稿）：模块行「确认」按钮（支持多选批量）+ 全部 confirmed 后主档「冻结」按钮；**不做审批收件箱单**——确认是审阅整图后的浏览型操作，收件箱单粒度是任务/操作，塞不下整图上下文；blueprint 任务收尾发黑板事件 `blueprint.drafted` 提醒去确认；
- **confirmed = 人工冻结，写路径收窄落 store 层**（待打磨 #6 定稿）：`update_module(author, …)` 按 author×status 校验——human 全字段可写；Agent 会话 draft 态可改 duty/spec_note/spec_doc/mod_action，**confirmed 后仅允许追加 `notes`**（「spec 错了写 notes 上报」红线从散文变校验；API 参数层 + store 双保险）；
- `rebuilt`：关联 reconstruct 任务 done（代码产出落 artifacts）；`selftested`：该任务获得 verified 判定（verified 机制依赖 [independent-verification-audit.md](independent-verification-audit.md) M1 验证器，未上线前由人工在蓝图页点 selftested 代打）；
- 主档 `status=confirmed` 为整图冻结门槛（有未 confirmed 模块时主档不可 confirmed）。

### 4.3 func_kb 锚定与漂移（D5）

- anchors 表锚 func_kb.id；**漂移提示双通道（待打磨 #4 定稿）**：
  - **func_kb 写入时同步检查**（主通道）：func_kb 写点（upsert/重命名）按 func_id 反查 anchors 命中模块 → bump 模块 `updated_at` + notes 追加系统条目（「锚点 func@0x… 名称变更 a→b」），不弹事件不打断——在做的会话下次读模块自然可见；
  - **doctor 兜底**（批处理面）：`blueprint-anchors-stale` 体检项——anchor 的 func_id 已不存在（error）+ confirmed 模块零锚点（warning，draft 豁免）；
  - 任务收尾不做（收尾已挂复盘/沉淀链，检查面不外扩）。

### 4.4 board_graph 集成（D4）

- 新边：`module_depends`（模块→模块，取 blueprint_edges）、`module_of`（func_kb→模块，取 anchors）；
- 蓝图模块节点进发现链路画布，与 finding/asset 同图；节点点击跳黑板页「蓝图」页签并定位到该模块（承载位 → 待打磨 #5）。

### 4.5 mod 配方（D6，应用层预留）

- `mod_action` 列随批预留不启用；真做游戏 mod 项目时启用并沉淀 mod 导出/版本管理机制；
- 届时 mod 配方 = 蓝图快照（sha 定格天然存在）+ 变更集（各模块 mod_action）。

### 4.6 与功能面/场景档衔接（承接 expert-pool 方案 §4.5）

- 蓝图 = research 轨**功能面**；场景档 deliverables 引用（逆向开发档交付清单：蓝图 + 重组代码 + 自测结果）。

### 4.7 API 形状（待打磨 #6 定稿）

经 core API 单一入口（黑板写红线），全部挂 `/api/projects/{pid}/blueprints` 前缀：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 蓝图列表（模块计数/状态摘要，多样本=多行按 binary_sha256） |
| `/{bid}` | GET | 详情：模块全量 + edges + anchors（func_id+name+address 投影） |
| `/{bid}` | POST | 建 draft 蓝图（Agent blueprint 任务收尾调用；binary_sha256 已有主蓝图 → 转更新） |
| `/{bid}/confirm` | POST | 主档冻结（校验全部模块 confirmed） |
| `/{bid}/modules` | POST | 建模块（draft 态蓝图内） |
| `/{bid}/modules/{mid}` | PATCH | 改模块（store 按 author×status 校验，见 §4.2） |
| `/{bid}/modules/confirm` | POST | 模块确认（human；body.module_ids 批量） |
| `/{bid}/modules/{mid}/notes` | POST | Agent 上报追加（confirmed 后唯一写通道） |

**按模块领单不做新端点**：publish_task 请求带 `context.module_id`（tasks.context 结构化载荷先例 tasks.py:131-213）。

### 4.8 版本历史与主档语义（待打磨 #7 定稿）

- `spec_history` 条目结构：`{ts, author, prev_spec_doc, reason}`——改前全文快照（markdown 千字节级，SQLite 无压力），cap 最近 20 条、更早仅留元数据；Agent notes 追加不进 spec_history（notes 本身即史）；
- **主档 `superseded` 退役**：UNIQUE(project_id, binary_sha256) 下不存在多主档更替——重拆解 = 同一主蓝图行内演进（模块增删 + 模块级 spec_history 承担「版本」），主档状态机简化为 draft/confirmed 两态；多拆解视角（同一样本多蓝图）若后置成真需求，届时加 view 字段而非放开 UNIQUE。

### 4.9 编排器与状态联动（待打磨 #8 定稿）

- **发布**：reconstruct 任务 `context.module_id` 指向目标模块；发布端点校验模块 status=confirmed（防跳过冻结直接开工）；rebuilder「按模块领单」= 领 context 指向自己模块的任务，非公共池抢单；
- **联动实现位置：`_finish` 终态钩子**（tasks.py:632，context 列唯一写点同函数）——任务 done 且 context.module_id 非空 → confirmed→rebuilt；任务获得 verified 判定 → rebuilt→selftested（同 terminal 事件则两连跳；前置校验范式同 reconcile done 硬闸 tasks.py:543）；
- 编排器零新逻辑（任务照常派发，联动全在黑板终态钩子）；新增事件 kind `blueprint.drafted` / `blueprint.confirmed` 供事件流与提醒消费。

## 5. 打磨轮定稿记录（2026-09-22 落定；方向性 4 项 2026-09-23 用户拍板通过；✅ 已落 §4）

1. ✅ `blueprint_edges.kind` 集合 = `calls` + `data` 两值起步：calls=调用依赖、data=共享配置/数据/常量依赖；顺序语义可由 calls 推导不做，note 字段兜细分。
2. ✅ spec 全文载体 = 直接入库：spec_doc 存 markdown 全文（弃 artifact/workspace 文件指针）——confirmed 冻结只有入库才可门控（文件写拦不住）、无路径漂移、与 spec_history 同库演进；D1「文档附着」措辞随之修订为「全文文本入表、不全结构化为列」。
3. ✅ 确认交互 = 黑板页蓝图页签按钮（模块行确认/批量 + 主档冻结）；收件箱不做确认单——浏览型审阅需要整图上下文，收件箱单粒度是任务/操作；`blueprint.drafted` 事件提醒替代。
4. ✅ 漂移提示双通道：func_kb 写入时同步（主，反查 anchors + notes 系统条目）+ doctor `blueprint-anchors-stale`（兜底）；任务收尾不做。
5. ✅ 前端承载 = 黑板页新增「蓝图」页签：蓝图列表（按 binary_sha256）→ 详情（模块表 + @xyflow 依赖图，复用 FindingsCanvas/boardGraph 布局）+ 模块侧栏（spec 全文 / notes / 确认按钮）；画布模块节点随 M3。
6. ✅ API 形状定稿（§4.7 八端点表）；写路径收窄落 store 层 `update_module(author×status)`；领单复用 publish_task `context.module_id` 不做新端点。
7. ✅ 主档 `superseded` 退役：UNIQUE 约束下无多主档更替，重拆解=行内演进；主档两态 draft/confirmed；spec_history={ts, author, prev_spec_doc, reason} cap 20。
8. ✅ 编排器联动 = `_finish` 终态钩子（done→rebuilt / verified→selftested）+ 发布校验模块须 confirmed；编排器零新逻辑；事件 kind `blueprint.drafted`/`blueprint.confirmed`。

## 6. 实施切分建议（定稿后由用户排期）

- **M1 表 + API 闭环**：四表迁移（spec_doc 全文入库 / notes 列 / 主档两态）+ §4.7 八端点 + store author×status 写路径收窄 + draft→confirmed 确认流。
- **M2 编排衔接**：publish 带 context.module_id + 发布校验 confirmed + `_finish` 状态联动（done→rebuilt / verified→selftested）+ func_kb 写入时漂移检查 + doctor `blueprint-anchors-stale`（verified 判据视 independent-verification-audit M1 进度，未上前人工代打）。
- **M3 图与前端**：黑板页「蓝图」页签（列表/详情/模块侧栏/确认按钮）+ @xyflow 依赖图 + board_graph 新边与画布模块节点 + `blueprint.*` 事件。
- **M4（预留）**：mod_action 启用 + mod 应用层。
