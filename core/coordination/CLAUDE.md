# core/coordination/ — 多智能体协调域

该目录是多智能体协调的独立边界，负责协调计划、任务依赖、状态和证据引用。
它不直接修改 `core/chat/runtime.py`、`AgentSession` 或现有智能体工作台运行时。

## 当前能力

- `CoordinationStore`：在项目 SQLite 中维护 `coordination_plans` 与
  `coordination_tasks` 两组独立表。
- 计划状态：`draft`、`active`、`paused`、`completed`。
- 任务状态：`pending`、`ready`、`running`、`blocked`、`completed`、`failed`。
- 任务支持 `depends_on` 依赖列表、角色/能力标签和结构化 evidence 引用。
- 结构化对象表支持 `function`、`string`、`xref`、`behavior`、`evidence`、`artifact`；
  对象携带 `source`、`confidence`、扩展 `data` 和独立产物引用。
- `coordination_conflicts` 保存两个对象之间的冲突、字段、解决状态和处置说明。
- API 位于 `/api/projects/{pid}/coordination...`，前端入口为“多智能体协调”。

## 扩展顺序

后续调度器、资源租约、验证智能体和恢复机制应消费本域数据；不要把协调状态
写入 chat 线程或现有 agent 循环。跨域联动通过 API、黑板事件或明确的适配器完成。
