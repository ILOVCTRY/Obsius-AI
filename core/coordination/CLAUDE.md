# core/coordination/ — 多智能体协调域

该目录是多智能体协调的独立边界，负责协调计划、任务依赖、状态和证据引用。
它不直接修改 `core/chat/runtime.py`、`AgentSession` 或现有智能体工作台运行时。

## 当前能力

- `CoordinationStore`：在项目 SQLite 中维护计划、节点、结构化对象、冲突和验证记录；计划 `config.team` 保存团队 roster，节点 `member_id` 保存成员绑定，读取时为存量 role 生成兼容团队视图。
- 计划状态：`draft`、`active`、`paused`、`completed`；节点状态：`pending`、`ready`、`running`、`blocked`、`completed`、`failed`。
- **M3 计划层接线**：`plan_work` 只登记等待确认的 draft 团队计划与 `depends_on` DAG；人类经 preflight/start 激活后，`delegate(plan_node_id=...)` 才允许派发 ready 且未绑定节点，成功后回填真实 `tasks.id`。`config.team` 只描述成员身份/职责，不承担执行调度。
- `refresh_readiness` 单向同步真实任务：`open/claimed→running`、`done→completed`、`failed→failed`；全节点完成自动完成计划。协调域不反写 tasks。
- **M3 依赖驱动唤醒**：节点状态跃迁到 `ready` 时按计划分组发 `plan.node_ready {plan_id,node_ids}` 事件（重复 refresh 不重复发）；编排器将其纳入 `WAKE_TRIGGERS`，唤醒轮读取 `_stats.plan` 后用 `delegate(plan_node_id=...)` 逐节点派发。服务端只发唤醒、不越权硬派。
- `_stats.plan` 只注入 active 计划摘要；objects/conflicts/verifications 保留旧 API 能力，不由编排器双写。
- API 位于 `/api/projects/{pid}/coordination...`，前端入口已并入 LiveRoom 的「协调」第三段；计划图为只读执行面，人工建节点/对象/冲突入口已移除。
- 计划工作台支持服务端 preflight/start/control（pause/resume/interrupt/retry）、项目通信聚合与 timeline；preflight revision 是计划+节点+项目安全配置的内容摘要，失败节点阻止重新启动，start 必须携带有效 revision 和三项确认。前端协调面由 PlanDag/PlanControlBar/PlanConfirmDialog/CommunicationDrawer 组成，AgentWorkbench 不接线。

## 边界

结构化对象、冲突与验证仍可经既有 API 访问，后续若要激活这些能力，应先对齐 findings/func_kb/verify.py 的单一真相源，禁止再次形成双账本。跨域联动通过 API、黑板事件或明确的适配器完成。
