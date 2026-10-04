# 方案：多智能体协调模块并入直播间（LiveRoom）

- **状态**：**讨论收敛（2026-10-04）**，方向已拍板，待打磨
- **拍板记录**：§0
- **思想来源**：`开源优秀项目/AI编程工具/cc-haha-main` 的多智能体协作子系统（文件邮箱 + SendMessage 广播 + 结构化协议帧、后台任务引擎 + `<task-notification>` 回灌、依赖驱动执行）。**只借协作机制，不移植 TeamFile/tmux/in-process 隔离/Worktree**。
- **范围约束（用户定）**：**把现有「多智能体协调」模块并入 LiveRoom（会话模块）**，去掉独立导航项；**智能体模块（工作台 AgentWorkbench）不动**。后端仅做支撑性最小改动。
- **关联代码**：`webui/src/App.tsx:50/63/80/575`（导航项 + 视图接线）、`webui/src/views/MultiAgentCoordinationView.tsx`（现协调模块，~215 行）、`webui/src/views/LiveRoom.tsx:374/1416-1439`（viewMode 切换）、`core/coordination/store.py`、`core/api/app.py`（coordination 端点）、`core/blackboard/store.py`（`session_inbox`）。

## 0. 拍板记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 融合对象 | **把「多智能体协调」模块并入 LiveRoom**；不再作为独立顶级导航页 |
| 2 | 不动的模块 | **智能体工作台（AgentWorkbench）不改** |
| 3 | 落地形态 | LiveRoom 顶栏「直播｜任务树」扩展为「直播｜任务树｜**协调**」三段；协调内容以 LiveRoom 内子视图承载 |
| 4 | 移植边界 | 只借 cc-haha 的「协作消息 + 依赖驱动执行」；TeamFile/tmux/in-process/Worktree 不移植 |
| 5 | 后端改动 | 允许支撑性最小改动（会话间发消息端点 + 就绪驱动唤醒），不做架构重写 |

## 1. 背景与问题

现状是**两个并行模块**：

- **多智能体协调**（独立顶级导航 `App.tsx:80`）：`MultiAgentCoordinationView.tsx`——指标行 + 协调驾驶舱（会话状态/任务泳道/事件流）+ 计划列表 + 任务依赖图 + 结构化对象/冲突（只读兼容面）。它**本来就是「会话 + 任务 + 计划」的视图**，与 LiveRoom 同源数据，却拆成两个页面。
- **LiveRoom**（会话模块）：会话页签 + 事件流 + composer + 编排器对话；顶栏只有「直播｜任务树」两段（`LiveRoom.tsx:1416-1439`），**看不到计划、看不到协作消息**。

问题：协调模块与 LiveRoom 数据同源、场景重叠，分散在两个入口；而协调域（刚融合的 plans/tasks/depends_on）**无自动执行器**、**无协作消息面**。

目标：**把协调模块整体并入 LiveRoom**，让它成为会话视图的一部分；去掉独立导航项；顺带用 cc-haha 的协作机制补上「协作消息」与「依赖驱动执行」。

## 2. 设计

### 2.1 LiveRoom 三段视图模式（承载并入）

- `viewMode`（`LiveRoom.tsx:374`）由 `"live" | "tree"` 扩为 `"live" | "tree" | "coord"`；顶栏切换（`:1416-1439`）加第三段「协调」。
- 协调内容抽为 LiveRoom 子组件 `webui/src/views/live/CoordinationPane.tsx`（从 `MultiAgentCoordinationView.tsx` 迁移），复用其驾驶舱/计划/依赖图/对象冲突只读面；**数据源不变**（`api.coordination` + `api.sessions` + `api.sessionGraph` + `api.eventsTail`，3s 轮询）。
- `MultiAgentCoordinationView.tsx` 迁移后**删除**；`App.tsx` 删 import（`:50`）、`View` 联合去掉 `"coordination"`（`:63`）、导航项（`:80`）、渲染（`:575`）；`GitBranch` 图标若不再用则移除。

### 2.2 协作消息面（cc-haha 邮箱/SendMessage 借鉴）

- **后端（支撑性最小改动）**：新增 `POST /api/projects/{pid}/sessions/{sid}/message`，body `{to: sid|"*", text, subkind}`——人类/编排器向单会话或全项目广播协作消息，落既有 `post_agent_message`（复用 subkind 白名单 intel/handoff/assist，**零新表**）。
- **前端**：在协调子视图内加「协作消息」区（或抽屉），显示 `message.inbox` 事件（来源会话 → 目标 / subkind / 摘要），未读高亮 + 全部已读（`api.readSessionInbox`）；底部发送行（目标下拉 + 广播 `*`）。
- 现状缺口：`session_inbox` 已有 agent→agent 通道（`post_agent_message`）与 human→session（`/note`），但**无会话→会话 REST 端点**，LiveRoom 只有未读红点（`LiveRoom.tsx:527/1324`）。

### 2.3 计划 DAG 自动续派（= M5，cc-haha 依赖驱动借鉴）

- **后端**：委托终态（`TaskQueue._finish`）时若任务绑定计划节点，`refresh_readiness` 后对新就绪节点发 `plan.node_ready {plan_id, node_ids}`；把 `task.done`（或 `plan.node_ready`）纳入 `WAKE_TRIGGERS`（`orchestrator.py:1178`）唤醒编排器；编排器提示增「有就绪节点则逐节点 delegate」纪律。**保持编排器唯一决策，服务端不硬派**。
- **前端**：协调子视图的计划 DAG 复用 `TaskTree.tsx` 的 React Flow 受控节点 + tidy-tree 分列 + `--viz-*` 范式，节点显示状态/绑定任务（点击跳会话）/未满足依赖。

### 2.4 （可选）会话控制面收敛

LiveRoom 现有控制散在 composer chips 与页签 `X`（暂停端点存在但前端无入口）。可在协调子视图顺带收敛暂停/恢复/中断/续跑/换人/收件箱为一个「会话控制」面板——**纯前端，非本次必做**，视打磨结论。

## 3. 里程碑切分

- **M1 · 协调模块并入 LiveRoom**（纯前端迁移）：三段视图 + `CoordinationPane` + 删独立导航项；零后端改动，可先落地验证形态。
- **M2 · 协作消息面**（后端 1 端点 + 前端区）：`POST /sessions/{sid}/message` + 消息展示/发送。
- **M3 · 计划 DAG 自动续派**（后端唤醒 + 前端画布）：`plan.node_ready` + 唤醒白名单 + DAG 视图。**后端已实施（2026-10-04）**：ready 跃迁按计划分组发事件，`WAKE_TRIGGERS` 冷却 120s，唤醒轮读取 plan 段后由编排器 `delegate(plan_node_id=...)`，服务端不硬派；LiveRoom 协调 DAG 视图已随 M1 并入。

依赖：M1 是基座；M2/M3 独立，可并行。

## 4. 待打磨清单

1. **并入形态**：协调内容作为**第三段视图**（推荐，对齐任务树）还是**右栏常驻**？驾驶舱（会话/泳道/事件）在 LiveRoom 内是否与现有事件流重复、要不要精简。
2. **对象/冲突段去留**：M4 已把 objects/conflicts 降为只读兼容面——并入后是否直接隐藏（后端 API 保留）。
3. **M2 发送权限**：端点仅服务人类 UI（agent 走内部 `inbox_post`）还是也开放给编排器；`*` 广播是否排除 closed 会话、是否要频率限额。
4. **M3 自动派发边界**：唤醒编排器让它自己 delegate（保守，推荐）vs 服务端直接派就绪节点（激进）。
5. **M3 计划 DAG 与 `parent_id` 拆解**：跨任务计划依赖与深度 1 父子拆解在 UI 上是否分两种边。
6. **导航收敛副作用**：去掉「多智能体协调」后，深链/`goto-*` 事件、`App.tsx` 的 `View` 类型、`webui/CLAUDE.md` 页面清单需同步；确认无其它入口引用该视图。

## 5. 关联代码

- `webui/src/App.tsx`（删导航项/import/渲染/View 成员）
- `webui/src/views/MultiAgentCoordinationView.tsx`（迁移 → 删除）
- `webui/src/views/live/CoordinationPane.tsx`（**新建**，承载并入内容）
- `webui/src/views/LiveRoom.tsx`（viewMode 三段）
- `webui/src/views/live/TaskTree.tsx`（DAG 呈现范式复用）
- `webui/src/lib/api.ts` + `types.ts`（新端点客户端 + 类型）
- `core/api/app.py`（M2 新端点）
- `core/blackboard/store.py`（复用 `post_agent_message`/`inbox_post`）
- `core/orchestrator/orchestrator.py`（M3 唤醒白名单）
- `core/coordination/store.py`（M3 就绪节点检测）
- 文档：`webui/CLAUDE.md`（页面清单去「多智能体协调」、LiveRoom 段补三段视图）、`core/api/CLAUDE.md`、`core/orchestrator/CLAUDE.md`
