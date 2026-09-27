# 任务尝试树（task-attempt-tree）

> 把 agent 针对一个目标的尝试路径画成单向树状图：目标 → 尝试节点 → 动作，
> 近实时高亮当前工作节点。**替代会话页「任务流」（TaskFlow）并退役之。**

- **状态**：已实施（M1+M2 全量，2026-09-27 当日实施；**同日 v2 意图驱动改版落地**，见 §0-bis）
- **拍板记录**：
  | # | 决策点 | 结论 |
  |---|--------|------|
  | D1 | 树的中间层骨架 | **计划步为主，意图挂节点**——task_plan 步骤做主干（计划闸保证任何 agent 都有树，零依赖模型自觉），意图（假设/死路）作为特殊节点挂在根下（**v2 改版推翻，见 §0-bis**） |
  | D2 | 视图位置 | **替代会话页「任务流」**（LiveRoom 直播\|任务流分段切换 → 直播\|任务树），TaskFlow 组件退役 |
  | D3 | 打扰度 | 零打扰：agent 侧零改动、零新工具调用，全部从既有事件**被动派生**（**v2 例外：bb_add_finding 门禁是唯一 agent 侧写入约束，见 §0-bis**） |
- **关联代码**：`core/blackboard/tasktree.py`（树组装）、`core/agent/tools.py`（tool.call 审计补轮次号 + v2 bb_add_finding 必挂意图门禁）、`core/blackboard/graph.py`（task_graph 退役）、`core/api/app.py`（/tree 端点）、`webui/src/views/live/TaskTree.tsx`（TaskFlow 退役后继）

## §0-bis v2 意图驱动改版（2026-09-27 当日，用户看过 v1 截图后的三点反馈）

用户反馈：①树里出现运行命令等不想要的东西；②只想要「目标、意图 1/2/3，每个意图对应测试结果（死路/发现等），新发现再长新意图」；③意图形态=「对 xxx 进行 sql 注入尝试 / 对 xxx 函数进行 hook」。追问确认两点：**计划步与命令/工具动作叶全部移除**；**游离发现「不允许出现」**（=加写入门禁，非仅前端兜底）。改动：

1. **树模型重写**：`目标 → 意图（一句可证伪假设，intents 表行）→ 检验结果（发现🔵/死路✕）→ 新发现下长新意图`；v1 计划步主干+动作叶整体退役。
2. **发现归属优先级**：①意图 outcome_refs 显式引用 → ②存活窗（finding.new 事件时刻 ∈ 意图 [created_at, closed_at]，作者会话一致优先取最新声明）→ ③游离置 parent=""（前端归「未挂意图发现（历史）」桶——**仅历史数据**）。
3. **发现必挂意图门禁**（唯一 agent 侧改动）：`_tool_bb_add_finding` 对 author 以 `sess-` 开头的会话要求 intents 表有本会话 open 意图，否则拒绝并指路 declare_intent（_PLAN_TOOLS 恒放行，全轨可先声明）。落点工具层非 store 层（store 加闸破坏大量既有测试；人类路径不经此工具豁免）。
4. **current 指针**：claimed 态取最新声明的 open 意图——**按事件 id 序取**（SQL `ORDER BY created_at, rowid`；now() 秒级精度同秒并列时纯 created_at 序不稳定）。
5. **测试**：test_tasktree.py 重写 6 例（归属三分支/嵌套防环/门禁/端点 410）；test_agent 夹具 make_agent 与 test_campaign 直构 AgentSession 处预置 open 意图适配门禁。

## §0 实施修正（2026-09-27）

1. **tool.call 的 step 字段语义修正**：§2 原判「tool.call 缺 step 是唯一缺口」不成立——command 事件已有的 step 是 **agent 轮次号**（loop round），非计划步 id，无法用于归组。实际归组口径改为 **task.step(status=doing) 事件时间线**（doing 后的动作归该步，直至下一个 doing；无 doing 覆盖进「未归类」桶）。tool.call 审计仍补了 step 字段（轮次号，与 command 对齐的观测价值，1 行）。
2. **端点定名** `GET /api/projects/{pid}/tree/{task_id}`（对齐 /trace 惯例的项目前缀风格，非 §4 原拟 /api/tasks/{task_id}/tree）。
3. **实现位置**：树组装不进 traces.py，新模块 `core/blackboard/tasktree.py`（复用 traces 的 R1 区间切分私有函数）；命令 ok 判定经 command.result 的 call_id→exit_code 映射（窗滤外全量回放，宁真勿丢）。
4. **task-graph 退役**：`graph.task_graph` 函数删除（session_graph/board_graph 保留）、GET /task-graph 端点先回 **410 过渡一版**（非直接删路由，给外部消费留指路），api.taskGraph 方法与 TaskGraph* 类型删除，相关测试 5 个（test_blackboard ×3、test_api ×2）随迁。
5. **前端补齐**：任务选择器（默认=激活会话绑定任务，可切全项目任务）；连续同名失败叶合并 ✗×N 在前端做；每步默认折叠 8 条可展开；taskTrace 分组逻辑最终**未复用**（trace 是时间链聚合，树要按步归组，口径不同——仅复用 traces 的区间切分）。

## §1 需求（用户原始三条件）

1. 尽量不打扰 agent 的工作；
2. 把 agent 针对一个目标的尝试路径画成**单向树状图**；
3. 尽量实时——能看到 agent 正在哪个节点工作。

## §2 数据源盘点（被动派生的可行性）

| 树需要的数据 | 已有来源 | 结论 |
|---|---|---|
| 根=目标 | tasks 表 objective | ✓ |
| 中间层=尝试分解 | task_plan（p1..pN，计划闸强制必写）+ task_step 状态流 | ✓ |
| 意图（假设/死路） | intents 表 + declare/close/reopen 事件（链路图 v3） | ✓ |
| 叶=动作 | tool.call 审计（per 调用）/ command+command.result（per 命令） | ✓ 但 **tool.call 缺 step 字段**（command 已有） |
| 当前位置 | task_step 的 doing 状态 + 事件时序 | ✓ |
| 死路/回溯 | blocked 步、fail_task、close_intent(dead_end) | ✓ |
| 按步分组动作 | **execution-trace-chain M1 的 api.taskTrace 已实现**（按步区间分组+游离段折叠） | ✓ 直接复用 |

**唯一缺口**：tool.call 审计 payload 无 `step`。补法：`ToolDispatcher._dispatch_once` 的审计 payload 加 `"step": self._step`（现成属性，~5 行）。旧项目历史事件无 step → 走 trace 已有的「游离段」折叠兜底。

## §3 树模型

```
根：任务（objective，四态色条沿用 open/claimed/done/failed）
 ├─ 步节点 p1「侦察」（done ✓ 绿）
 │   ├ ⚙ bb_query(func) · 0.8s ✓
 │   └ ⚙ strings_search("flag") · 0.3s ✓
 ├─ 步节点 p2「还原算法」（doing ● ← 当前节点脉冲高亮）
 │   ├ ⚙ decompile(0x401160) ✓
 │   └ ⚙ run_cmd(capstone) ✗×3   ← 同名工具连续失败合并计数，标红
 ├─ 步节点 p3「落库」（blocked ■ 琥珀 + note）
 └─ ◇ 意图「主校验在 0x401160」（dead_end ✕ 灰 / open ◇ 青 / vuln ✓ 绿）
```

- **步节点**：id/标题/状态（○▶●■ 对齐后端 _render_plan 口径）；doing 即「当前节点」。
- **动作叶**：tool.call（带 step 归组）+ command 对；`ok=false` 连续同名合并 `✗×N`；超出 N 条（待打磨定值，暂定 8）折叠「…另有 M 次」。
- **意图节点**：挂根下独立分支；statement 截断 + outcome 徽章（复用 AttackPath 卡样式）；与步的自动关联（时序/basis_refs 近似）后置观察，先不做边。
- **游离段**：无 step 归属的动作折叠为一个「未归类活动」叶（对齐 TaskTraceList 语义）。
- **多窗接力**：树按 task_id 聚合，与会话无关（会话中心化后任务被多窗接力不影响树）；当前节点跨会话取全局最新 doing/最新事件。

## §4 实施切分

### M1 后端（~120 行）

1. tool.call 审计补 `step`（tools.py dispatch payload）。
2. 新只读端点 `GET /api/tasks/{task_id}/tree`：复用 traces.py 的按步分组现算 + intents 查询 + tasks 行，组装树 JSON：
   `{task:{id,objective,status}, steps:[{id,title,status,actions:[{kind,name,brief,ok,duration_s,ts,fail_count}]}], intents:[{id,statement,status,outcome,dead_reason}], current:{step_id,last_action_ts}}`。
   纯读聚合，不落库、不新表。

### M2 前端：TaskTree 替换 TaskFlow（~250 行）

1. 新 `TaskTree.tsx`（React.lazy 同 TaskFlow 现状）：xyflow 单向 DAG 布局（根左→步→叶右，复用 findings-canvas-dag-layout 的分层思路）；节点卡新写（轻量，不复用 TaskNode）。
2. 实时：`wsBump` 去抖重拉（同 TaskFlow 现制）+ 3s 轮询兜底；当前节点 `animate-pulse` 高亮 + 自动 pan 到当前节点（首挂 fitView，之后跟随）。
3. LiveRoom「直播｜任务流」分段切换改名「直播｜任务树」，渲染组件替换。
4. **退役清理**：删 `TaskFlow.tsx / TaskNode.tsx / TaskFlowEdge.tsx / flowModel.ts / flow.css`；`App.tsx` 任务流双击跳转逻辑改挂看板（TaskBoard 双击入口保留）；`api.taskGraph` 端点与 `core/blackboard/graph.py` 若无其他消费方同步退役（实施时核实）。

## §5 待打磨清单

1. 意图节点与步骤的关联（时序近似 vs basis_refs 匹配 vs 不关联）——M2 先不画边，观察实用性再定。
2. 动作叶折叠阈值（暂定 8）与 detail 展开（点击叶出浮卡：result_head/耗时/时间，复用 TaskTraceList 浮卡？）。
3. 历史项目无 step 的旧事件兼容（游离段折叠已有兜底，验证够不够）。
4. 当前节点跟随交互：自动 pan 是否会被手动拖拽打断（xyflow onMoveStart 停跟随，点「回到当前」恢复——TaskFlow 同款问题先例）。
5. `api.taskGraph`/graph.py 退役边界核实（TaskBoard/看板是否消费）。
6. 树性能：max_steps=200 × 每步动作数十条，虚拟化/折叠策略是否必要。
