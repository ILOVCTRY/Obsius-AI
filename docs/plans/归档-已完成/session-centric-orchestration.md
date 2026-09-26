# 会话中心化编排（session-centric-orchestration）

- **状态**：**已实施（M1–M4 全量，2026-09-25）**，同日归档；定稿决策已回写 DESIGN.md §四「会话中心化」。实施修正见文内 §0。
- **一句话**：取消「任务池 / 接任务」心智——**一个窗口就是一个可连续接活、可中途换人的智能体会话**，大家公用黑板；编排器不再「发布任务」，而是「拉起/复用窗口，像人类与 AI 交互一样发一条委托让他干活」。
- **关联代码**：`core/blackboard/tasks.py`（委托单元）、`core/agent/loop.py`（会话轮）、`core/orchestrator/orchestrator.py`（delegate 动作）、`core/api/app.py`（调度/审批）、`webui/src/shell2/`（新壳）、`webui/src/views/{TaskBoard,live/TaskFlow}.tsx`（会话化改造）。

## 0. 实施修正注记（2026-09-25，M1–M4 落地实况）

1. **schema v24 纯语义迁移、零物理改动**：tasks 表零新列；`meta.bound_task_id` 退役（新窗不写、存量残留按历史字段忽略，行投影保留兼容），`spawn_task_id` 保留服务 resume 幂等。
2. **中途换人落地为两条严格分语义的通道**（拍板 #3 只说「复用 apply_role_change」，实施时细化）：①`apply_role_change(task)`=委托建议角色热换装，委托结束 `_restore_base_persona` 回窗底色；②`switch_session_role(role)`（POST /api/sessions/{sid}/role）=重定义窗底色身份、跨委托持续。两者历史与黑板全保留；共用事件 `session.persona_switched`（payload.reason 区分）。
3. **窗内队列读时派生**：`take_session_next`（`target_session=? AND status='open' ORDER BY priority,created_at`）取代 claim_next 公共池；租约 30min 过期回收仍落同窗，无认领竞争。
4. **Worker 统一入口 `agent.run_session()`**：有委托→run_task 全工具面、无委托→run_chat 回应收件箱、都没有→None 空退窗回待命；委托做完 while 重入接窗内下一件。
5. **编排器派单**：先 `_pick_reusable_window`（armed+idle、role 匹配、干过同 task_type 者取新）再开窗；L1 分流落新审批 `op="delegate_window"`（批准处理器开窗+写委托+带活起跑，补发 session.spawned/delegation.posted）；`spawn_session` 仍只用于纯对话/侦查辅助窗。
6. **会话协作图 session_graph**（GET /api/projects/{pid}/session-graph）：边四类——delegate（orch→窗）/derive（窗→窗：父委托挂另一存活窗）/inbox（同依据/更新跨 ≥2 窗聚类，无向）/dm（agent_message 私信，有向）。
7. **聊天安全口径（宁严勿松）**：人类消息踢 worker 时，未武装窗仅当 `session_has_live_work` 为假（窗内无 open/claimed 委托）才踢——一条聊天永远不能替未批准的委托起跑。
8. **旧壳冻结为「不删代码 + 顶部冻结条」**（App.tsx，本会话可关）：会话看板/协作流/中途换人只进新壳；旧壳 TaskBoard/TaskFlow 仅保证不崩。
9. **回归**：全量 1140 passed（实施中 2 处失败为测试侧硬编码版本号 23→24，已修）。

## 1. 背景与动机

现状（DESIGN.md 双循环、任务队列）虽在 v0.72 退役了「公共抢单」——每个任务发布即绑专属窗、worker 只认领 `bound_task_id`——但**任务仍是一等公民、窗口是附属品**：发布任务才建窗、一窗一任务、终生不换绑、任务结束窗变「续聊窗」被调度跳过。用户要把主客关系彻底倒过来：**窗口（智能体会话）是一等公民，「活」是派给会话的委托**。这更贴合真实班组与「人对 AI 说话」的直觉，也让「中途换人」「连续派活」自然成立。

## 2. 拍板记录（两轮共 7 项，2026-09-25）

| # | 议题 | 定稿 |
|---|---|---|
| 1 | task 去留深度 | **重塑为「会话内委托」**：去池/去认领/去专属窗/去终生绑定，保留「一件活」轻量单元承载验收/验证/预算/现场/轨迹 |
| 2 | 活干完后窗口 | **保留待命、可追加**：一生连续接多件，上下文/黑板热着；受 sessions_cap 约束 |
| 3 | 中途切智能体 | **接手**：对话历史 + 黑板全保留，复用 `apply_role_change`，新壳会话界面加入口 |
| 4 | TaskBoard/TaskFlow | **改造为会话视图**：看板列=会话状态，任务流=会话协作流 |
| 5 | 向忙窗发活 | **窗内排队**：当前活干完自动接下一件；跨窗 X/S 资源租约保留防撞 |
| 6 | L1 编排开窗派活 | **沿用审批口径**：建待命窗+委托注入+提审批，批准后该窗带活起跑 |
| 7 | 旧壳处置 | **新壳先行、旧壳冻结尽快退役**：借模型翻转结束观察期，旧壳不做会话化适配 |

## 3. 现状 → 目标

| 维度 | 现状 | 目标 |
|---|---|---|
| 一等公民 | task（open→claimed→done/failed） | 会话窗；委托挂在窗上 |
| 起活 | publish_task→自动建专属窗→双向绑定→worker claim | 编排器开窗/复用窗 + 注入委托消息，无池无竞争认领 |
| 窗口一生 | 一窗一任务、终生不换绑、终态即续聊窗 | 连续接多件、可换人、终态窗回待命 |
| worker | run_next_task（认领）→空退→run_chat（限制不改终态） | 统一「会话轮」：有委托干活/无委托对话、工具面一致 |
| 编排器 | publish_task / spawn_session 两动作 | 合一为 delegate（选窗→委派→起跑/审批） |

## 4. 目标模型

### 4.1 委托（delegation，物理仍用 tasks 表）

**物理表 `tasks` 与主键 id 保留**（最小迁移、下游 SQL/事件/轨迹不动），语义从「池中任务」改为「派给某会话的一件委托」，UI/文档一律称「委托」。未来若要物理改名另立迁移，本期不做。

字段重映射（schema.py:209-238）：

- `target_session`：语义=**委托归属窗**（保留）。区别于现状：①不再发布即自动建专属窗，由编排器显式选已有窗/开新窗时写入；②窗不再因一件委托终生绑定，可连续接收 `target_session` 指向它的后续委托。
- `status`：`open`=已指派、在该窗队列待执行（或审批中）；`claimed`=该窗正在做；`done/failed` 不变。
- `claimed_by`：保留「谁在做」事实（轨迹/审计依赖），但**起跑时由系统直接置为 target_session，不再事务竞争**（单进程、归属已定）。
- `lease_until / 心跳`：从「抢占租约」降级为**崩溃检测**——worker 异常退出心跳停 → 委托回 open、仍指原窗内排队重试（`expire_leases` 语义改「回收」而非「释放回池」）。
- `wait_for / conflict_keys / resource_leases`：**只保留跨窗冲突**（两窗不可独占同一目标，X/S 矩阵不变）；窗内排队天然不撞，窗内不再产生 wait_for。
- `role`：从「认领即换装的强制角色」降为**委托建议角色**——开窗/分派时按它挑专家；窗内实际身份可被中途换人覆盖。
- `parent_id / preferred_runtime / noise_budget / priority / plan / dedup_fp / context(reconcile·attempts·attachments) / result_note` 等：语义全部保留，附着对象即委托。
- **零新增列**：窗内队列用 `target_session=? AND status='open' ORDER BY priority, created_at` 读时派生；会话 `meta.bound_task_id`=当前在做（保留），待做清单不冗余存储。

### 4.2 worker 会话轮（统一双循环）

删除 `run_next_task` / `run_chat` 的主客二分，合并为一个会话轮（`run_session`）：

1. 窗内有 open 委托 → 取队首：`open→claimed`、`claimed_by=本窗`（BEGIN IMMEDIATE 仅防进程内线程，无竞争），按委托目标执行——完整工具面、认领/恢复时 drain 收件箱、现场每步落盘、卡死/预算/验收/独立验证全套沿用。
2. 无委托但有对话消息（人或编排器的非委托消息）→ 对话回应：**删去现状 chat 纪律里「不认领任务 / 不改资产终态」的限制**，工具面与委托轮一致，纯文本收口。
3. 都没有 → 空退：worker job 结束、窗回 idle（事件驱动，非常驻进程）。
4. 一件委托 `finish →  reconcile/独立验证 → done` 后：**不退出，立即看窗内队列**有下一件则自动接；队列空才空退。「保留待命」指窗行保留可被追加，而非常驻进程。

中断（abort）/暂停（pause）/卡死升级（stuck+顾问）/预算（步数·token）/现场持久化：逻辑不动，附着点从「任务」自然落到「委托」（同一张表）。

### 4.3 编排器 delegate（开窗即派活）

tick 的决策动作从 `publish_task`（派单+自动建专属窗）改为 **delegate（委派）**，`publish_task` 与 `spawn_session` 两工具合一：

- **选窗**：优先复用「idle 且干过同类 / 与目标有关联上下文」的窗（简单规则，避免把不相关活硬塞）；无合适窗或显式要求 → 开新窗。
- **委派**：写一条 open 委托（`target_session=该窗`）+ 向该会话注入一条委托消息（`author=orchestrator`，在会话流里呈现为「🧭 编排器委派」，等同人类那条消息的 kick 效力）。
- **起跑/审批**：L2 及授权内、窗 idle→直接起跑；窗忙→进窗内队列；L1→建/指窗后提审批单，批准带活起跑（沿用 §2#6）；L0→只发 `orch.proposed` 提案。
- 闸门不变：单次 tick 委派数上限、gate 预算、dedup 指纹、phase 入场门/分流、goal 目标注入、campaign 召回全部沿用。
- Agent 侧现有 `publish_task` 工具重塑为「派生委托」：可指定给本窗（排队）/新窗/指定窗，父子深限 1、每父 ≤3 子不变。

### 4.4 中途换人

- 新壳会话界面加「🔄 换智能体」入口：选专家 → `apply_role_change`（下个步边界重建 system/prompt/工具白名单），**对话历史与黑板全保留**（§2#3）。
- 切换瞬间有委托在跑：步边界立即生效，新身份接手**当前委托**继续（不重开）；委托的 `role`（建议角色）与窗实际身份分离的事实记入审计。
- 开窗装配（专家 yaml、caps、allowed_roles、advisor 配置、campaign）沿用 `_registered_session_factory`，不新增装配路径。

## 5. 前端改造（仅新壳）

- **侧栏 ProjectTree → 会话列表**：编排器固定首行；其下每个会话窗一行=状态字形（idle/running/paused/blocked）+ 当前身份名 + 当前委托摘要（idle 显「待命」）；展开看窗内待做队列。删除「任务行 / 辅助窗行」三分（所有行统一为会话）。落地态对账（项目消失、会话 closed）沿用现有 effect。
- **ConversationPane**：会话态收敛为单一输入——人发消息即派活/对话，窗 idle 起跑、忙则注入/排队；去掉 `assign/task` 模式及「发任务」区别（派活就是对话）；顶部加「🔄 换智能体」。委托在对话流内呈现（委派消息、审批/验收卡、完成回执）。编排器行（__orch）保留 orch 对话态。
- **TaskBoard → 会话看板**：列=会话状态（待命/干活中/待审批/暂停阻塞/收尾）；卡=会话（身份+当前委托+队列数+产出）；支持开窗、换人、就地裁决。
- **TaskFlow → 会话协作流**：节点=编排器+会话窗；边=委派/派生/私信。数据源由 task-graph 调整为会话+委托（task-graph 端点重塑或新增 session-graph）。
- **BoardGraph / AttackPath / TaskTrace**：task 节点/区间语义→委托，数据源同表，仅标签与文案调整。

## 6. 存量迁移（schema v23 → v24）

- **物理表与数据不动**（tasks 即委托），零数据搬迁；v24 仅做语义/口径迁移与注释更新（如需可加视图，不做破坏性改）。
- claimed/open 且 `target_session` 非空：天然=窗内当前/排队委托，不变。
- **终态「续聊窗」解绑**：现状 `meta.bound_task_id` 指向 done/failed 被调度跳过 → 迁移后按普通待命窗处理、可接新活（代码删除「bound 终态即跳过」分支；bound_task_id 留作「上一件」或清空）。
- 无 `target_session` 的 open 行（L0 提案残留/异常）：不自动跑，列交编排器重新委派或挂起。
- 存量 claimed 租约沿用；过期回收仍指同窗（不回公共池）。

## 7. 旧壳处置

- M6 已切新壳为默认。本期旧壳**冻结会话化适配**：仅保证后端模型变更后旧壳入口不崩（或直接对旧壳加「已冻结，请用新壳」提示并隐藏深度入口）；TaskBoard/LiveRoom 旧形态不随模型改造。
- 旧壳物理退役另开清理窗口、由用户拍板时点（本期只冻结、不删除代码——遵守「不删文件」约束）。

## 8. 里程碑切分（建议，最终用户排期）

- **M1 后端委托重塑**：去 claim 竞争/公共扫描；委托「指派即定窗」、窗内队列读时派生；worker 双循环合并为会话轮（含删 chat 终态限制）；终态续聊窗解绑。
- **M2 编排器 delegate + 闸口适配**：publish/spawn 合一、选窗复用规则、委托消息注入；L1 委派审批（批准带活起跑）、忙窗排队、租约降级为崩溃回收、跨窗 X/S 保留。
- **M3 新壳前端**：侧栏会话行、对话派活、换人入口、委托/审批/回执呈现。
- **M4 看板/流会话化 + 存量迁移 + 旧壳冻结 + 全量回归与文档回写**。
- M1/M2 可合为一个后端批次实施。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 核心 loop/tasks/orchestrator/app 改动大，既有测试大量基于 claim/publish | 批量改写而非删除用例；按里程碑小步、每批全量 pytest；先固化「委托接收/窗内排队/自动接下一件」关键路径用例 |
| 编排器「复用窗 vs 开新窗」决策质量差，把不相关活塞错窗 | 初期保守规则（同类/目标关联才复用，否则新窗），cap 兜底；规则演进不引入 LLM 二次判轨 |
| 换人时 system/工具白名单重建影响在跑委托 | 沿用 apply_role_change 步边界生效、接手当前委托；切换事件入审计可回放 |
| 崩溃回收（旧租约）与「窗内排队」竞态致委托重复执行 | 接收仍走 BEGIN IMMEDIATE + 单进程；回收只置 open 不换窗，幂等 |
| 旧壳冻结后残留旧壳入口用户体验断裂 | M6 已默认新壳；旧壳加显式冻结提示与跳转，不静默坏掉 |
