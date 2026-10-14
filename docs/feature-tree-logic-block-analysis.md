# 功能树 / 业务逻辑模块分析 —— AI 填充倾向问题与治理方案

> 2026-10-13。数据来源：`workspaces/aimod/blackboard.db`（截图项目）实证统计 + 全库代码走读。

## 一、模块现状（数据流）

| 维度 | 功能树 | 业务逻辑块 |
|---|---|---|
| 存储 | `feature_trees` + `feature_tree_nodes`（v36 拆表） | `logic_blocks` + `logic_block_funcs`（v26） |
| 写入口 | 工具 `bb_feature_tree_create` / `bb_feature_node`（add/update/delete）；API `/feature-trees*` | 工具 `bb_logic_block_create` / `bb_logic_block_update`；API `/logic-blocks*` |
| 读入口 | UI 功能树页签 + API GET | UI 业务逻辑页签 + API GET |
| 唯一性约束 | 每项目每样本一棵树（create 幂等） | 同项目同样本块名唯一（重名拒收） |
| 关联 | 蓝图 `feature_tree_id` 引用；逻辑块 `feature_node_id` 下钻 | 挂函数必须先登记 func_kb |
| status 流转 | Agent 不可改，验收是人类职责 | 无 status |
| 定位 | 分析阶段产物：自顶向下拆业务功能 | 函数协作如何构成业务功能（逆向理解笔记） |

定位分工（工具描述口径）：PWN/漏洞利用→攻击链，重建管线→蓝图，函数逻辑/业务理解→逻辑块，功能拆解→功能树。

## 二、实证：AI 到底写了多少（aimod 项目）

同一段逆向分析周期内（对照样本，56 个函数入库、48 条 findings、71 次意图声明）：

| 指标 | 数值 | 说明 |
|---|---|---|
| `feature_trees` | 2 | **全部 `author=human`**（人类在 UI 点的），`bb_feature_tree_create` 工具调用 = **0** |
| `feature_tree_nodes` | **0** | `bb_feature_node` 工具调用 = **0**，`feature_node.added` 事件 = 0 |
| `logic_blocks` | 4 | 全部 `author=chat-*`——发生在**人类窗口问话的对话轮**，团队执行轮 0 个 |
| `func_kb` | 56 | 执行轮主力产出 |
| `findings` | 48 | 执行轮主力产出 |

结论：用户感知准确且比感知更糟——**执行轮 AI 对功能树节点零写入；连逻辑块都是对话轮顺带产的**。写黑板的行为完全收敛在「func_kb 三件套 + findings + intents」上。

## 三、根因分析（按代码证据排序）

1. **`bb_query` 看不见这两个对象（核心结构缺口）**
   `what` 枚举只有 findings/assets/events/func/blueprint/site/package，没有 feature_tree / logic_block。而工具纪律第 2 条是「动手前先 bb_query 看黑板——别人可能已做过（**去重是硬规则**）」。对无法查询的对象：拿不到 tree_id / node_id（`bb_feature_node` 第一个参数就得填 tree_id），也无法确认会不会写重。AI 对「查不到又不让瞎写」的工具，理性选择就是不写。

2. **蓝图有 system 提示物化，功能树没有（不对称）**
   research 轨 `_mat_blueprint` 会把命中蓝图的全部模块注入 system。功能树零注入——AI 规划期不知道项目里已存在两棵树、更不知道对应哪个样本，`feature_tree_id` / `feature_node_id` 这类关联字段无从填起（填错=挂错或拒收）。

3. **Prompt 全链路零指引**
   - `STRICT_PROMPT_TAIL` 十条硬纪律：功能树/逻辑块 0 提及（激励全在 finding/intent）；
   - `binary-rev` SKILL.md 硬规则只鞭打「反编译前查 func + 分析完 bb_upsert_func」，流程第 5 步只说 bb_add_finding；
   - `reverse-analyst.yaml` persona 只提 func_kb 协作；
   - 全 `packs/` 目录 grep `功能树|逻辑块` = **0 命中**。
   AI 收到的完整信号就是「逆向 = decompile + bb_upsert_func + bb_add_finding」。

4. **聚合粒度判据缺失**
   func_kb 是「每函数一条」有硬规则强制；逻辑块/功能树是「跨函数的业务功能」粒度，需要模型自己判断何时值得聚合。工具描述说了「别和攻击链/蓝图混用」，但没说什么信号**触发**聚合。模糊触发条件的行动，模型倾向跳过。

5. **无收尾自检、无正反馈闭环**
   - close_intent/finish 前没有任何机制测量「本会话分析了多少函数、多少已被节点/逻辑块覆盖」；
   - 节点 status 验收在人类侧，写了对 AI 没有可见回报；
   - `bb_query what=func` 不告诉 AI「这个函数已属于某功能」，重复分析与首次沉淀都缺激励。

## 四、治理方案：三层防线（可查 → 该写 → 有回报）

### 第一层：可查（去重的地基，优先级最高）

1. `bb_query` 新增两个 what 面：
   - `what=feature_tree`：列树（可按 binary_sha256 筛），单棵树带节点清单（id/名/父/已挂函数数）；
   - `what=logic_block`：列块，带 func_count 与 funcs。
   store 方法（`list_feature_trees`/`get_feature_tree`/`list_logic_blocks`）已存在，纯接线。
2. 仿 `_mat_blueprint` 增 `_mat_feature_tree`：执行轮 system 注入当前样本功能树摘要（tree_id + 节点名清单 + 挂函数数），让 AI 规划期就持有 tree_id、并看见已有节点避免重名。

### 第二层：该写（提倾向，同时天然防重复）

3. **触发条件写进 prompt**（改动最小、见效最快）：
   - `binary-rev` SKILL.md 硬规则加一条「功能域沉淀」纪律；
   - `STRICT_PROMPT_TAIL` 第 4 条边干边写后补半句。
   核心口径：**「识别出可命名的业务功能域（命令分发/协议握手/算法校验/云通信…）时，先 `bb_query what=feature_tree` 看有无同名或同职责节点——有→`bb_feature_node` update 补函数地址/desc；无→add 新节点。该功能 ≥2 个核心函数分析齐后，建/补一个逻辑块挂函数（块名=功能名）。」**「先查后写」一句同时解决两个问题：查不到才 add 防重复，查到节点就 update 防漏写。
4. **服务端幂等强化**（防重复的硬底线）：
   - `add_feature_node`：同树**同级同名**节点不再新建，返回既有 id 并标注 `merged`（对齐 `bb_add_asset` 重报合并语义）；
   - `bb_logic_block_create` 重名拒收时，错误串直接带既有块 id（现文案「换名或复用既有块」没给 id，AI 拿不到就没法走 update）。
5. **收尾软自检**（轻量、不熔断）：finish/close_intent 时统计本会话新增 func_kb 中未被任何节点 `func_addresses` / 逻辑块覆盖的比例，超阈值在回执附一句引导（同 budget 提醒语气）。只提醒不拒绝，防卡死。

### 第三层：有回报（正反馈）

6. `bb_query what=func` 返回行附带函数归属（所属功能树节点名 / 逻辑块名）。AI 下次查函数即见「已属于某功能」：重复分析动机下降，「写节点 = 队友可见归属」成为沉淀激励。

### 「防重复 / 防不添加」映射表

| 机制 | 防重复 | 防不添加 |
|---|---|---|
| bb_query 新两面 | ✔ AI 能查现状 | ✔ 查完才知道写什么 |
| system 物化功能树 | ✔ 看见已有节点名 | ✔ 规划期即有 tree_id |
| prompt 触发纪律（先查后写） | ✔ 查到就 update 不新建 | ✔ 明确「何时必须写」 |
| 节点同级同名合并 | ✔ 服务端兜底 | — （update 语义引导补写） |
| 收尾软自检 | — | ✔ 收尾前量一次覆盖率 |
| func 查询带归属 | ✔ 减少重复分析 | ✔ 沉淀有可见回报 |

## 五、改动清单（按优先级）

> **第一层已于 2026-10-13 落地**（track-query-split）：bb_query 九面按轨拆分 + 新增 feature_tree/logic_block 两个查重面 + handler 硬闸 + 三处下发点（AgentSession 主循环/对话轮、chat runtime）+ 测试 14 个 + CLAUDE.md 三份同步。实施形态与下表第 1 项有出入（用户拍板「渗透有自己的 what，逆向有自己的 what」），以实际实现为准，见下节。

| # | 文件 | 改动 | 风险点 |
|---|---|---|---|
| 1 | `core/agent/tool_registry.py` | bb_query what 枚举 + description 补两面；工具描述加「add 前先查」一句 | test_tool_registry 快照对 flags 漂移敏感，flags 不动应无虞，需跑测试确认 |
| 2 | `core/agent/tools.py` | `_tool_bb_query` 实现新两面；add 合并/reject 带 id 透传 | — |
| 3 | `core/blackboard/store.py` | `add_feature_node` 同级同名合并；`create_logic_block` 重名异常带 id | 合并语义需补 `tests/test_blackboard.py` 用例 |
| 4 | `core/agent/loop.py` | STRICT_PROMPT_TAIL 补纪律段；`_mat_feature_tree` 物化 | prompt 拼接测试有全等断言（`build_system_prompt`），需同步 |
| 5 | `packs/kb/skills/binary-rev/SKILL.md` | 硬规则加功能域沉淀纪律 | — |
| 6 | `core/agent/tools.py` | 收尾软自检（finish/close_intent 路径） | 只提醒不拒绝；统计逻辑走一次 SQL |
| 7 | `core/blackboard/schema.py` | 无 | 已有表结构够用 |

未动：webui（UI 已完整）、API 层（人类 CRUD 已完整）、schema。

### 第一层实际实现（2026-10-13，track-query-split）

按 Nan 拍板改为**按轨拆 what 家族**，而非单一全量枚举：

- **逆向 family（research/ctf）**：findings / assets / events / func / blueprint / package / **feature_tree（新）** / **logic_block（新）**——拿到 site 用不到，删。
- **渗透 family（pentest/redteam）**：findings / assets / events / site——逆向六面整组删，连参数面（binary_sha256/address/risk_tag/blueprint_id/package_id/tree_id/block_id）一起删，模型看不见就不会传。
- 家族表 `_BB_QUERY_TRACK_WHAT` + `bb_query_what_for(track)` / `bb_query_schema_for(track)`（tool_registry.py）；canonical 注册项保持全量向后兼容，**未知/空轨=全量回退**。
- 三处下发点套用裁剪：`loop.py` 模块级 `_llm_tool_schemas()`（`_loop` 主循环 + `run_chat` 对话轮，裁在调用点——team execution 替换 `_task_tool_schemas` 也不漏）、`core/chat/runtime.py` `_tool_specs()`（智能体工作台子专家，即 aimod 产逻辑块的那条 chat-* 路径）。
- handler 入口硬闸兜底：异家族面（模型硬传/旧缓存）回 `[错误] what=X 本轨不可用（…轨允许: …）`，不静默执行。
- 新两面语义（先查后写的查重面）：`what=feature_tree` 列树/单棵（binary_sha256 筛、tree_id、节点清单），`what=logic_block` 列块/单个（binary_sha256 筛、block_id、funcs）；schema description 写明「增删改节点前先查，同名走 update」「建块前先查，重名拒收改 update」。
- 测试：`tests/test_tool_registry.py` +6（家族划分/映射/schema 裁剪/无注册表污染）、`tests/test_agent.py` +3（两面 handler + 跨族硬闸）；既有 `test_bb_query_blueprint`/`test_bb_query_package_face` 随家族拆分改 research 轨；`make_agent` 增 `track` 参数（默认 pentest 不变）。全量 299 passed（test_agent/tool_registry/intents/blackboard）。

## 六、待拍板

- 收尾自检阈值建议「本会话新增函数 ≥3 且覆盖率 <50% 时报」，可否接受？
- 「同级同名合并」是否扩展到跨级同名（同名不同父视为不同功能）？建议否，减少意外。
- 功能树 prompt 注入只在 research 轨（对齐蓝图），还是全轨？
