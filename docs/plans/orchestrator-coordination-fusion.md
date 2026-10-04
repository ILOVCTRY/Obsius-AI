# 方案：协调协议增强 + 窄化协调域为计划 DAG 层（借鉴 cc-haha 多智能体协调）

- **状态**：**阶段一（M1+M2）与阶段二（M3+M4）已实施（2026-10-04）**
- **拍板记录**：§0 + §0-bis + §0-ter（阶段二实施记录）
- **思想来源**：`开源优秀项目/AI编程工具/cc-haha-main`（Claude Code 开源克隆）的多智能体协调子系统——协调者模式提示词纪律（`src/coordinator/coordinatorMode.ts`）、`<task-notification>` 结构化回执信封、continue-vs-spawn 决策表、TeamFile/邮箱/四 spawn 路径。**只借协议与提示词，不移植 Team/Mailbox 运行时**（与本项目会话中心化模型重复）。
- **关联代码**：
  - 阶段一：`core/orchestrator/orchestrator.py`（`ORCH_SYSTEM_PROMPT`/`CHAT_SYSTEM_PROMPT`、`_tool_delegate`、`_pick_reusable_window`、`_finish_tick`、`_recent_tasks_view`、M4 唤醒 `collect_wake_triggers`/`wake_brief_text`）
  - 阶段二：`core/coordination/store.py`（`CoordinationStore` 六表）、`core/api/app.py`（:5009 `GET /coordination`、:5054 `PATCH /coordination/tasks/{id}`、:5153 `POST /coordination/verify`）、`core/blackboard/tasks.py`（`publish`/`complete`/`fail`）、`webui/src/views/MultiAgentCoordinationView.tsx`
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§四 会话中心化编排 / §6.4 编排器），同步 `core/orchestrator/`、`core/coordination/`、`core/api/`、`webui/` 的 CLAUDE.md；本文保留作方案背景

## 0. 拍板记录

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 融合范围 | **只借鉴协议与提示词**；cc-haha 的 TeamFile/邮箱/四 spawn 路径/Worktree 隔离**不移植**——本项目会话中心化模型（窗口=一等智能体、`target_session` 绑委托）已是同构且更完备（自主档 L0/L1/L2、`_APPROVAL_OP_HANDLERS` 白名单审批、唤醒触发器、覆盖度对账） |
| 2 | 落地节奏 | **分两阶段**：阶段一=协议增强现有编排器（低风险，不动架构）；阶段二=激活孤儿 `core/coordination/` 域（架构级） |
| 3 | 阶段一内容 | M1 协调者提示词纪律 + M2 结构化委托回执信封 |
| 4 | 阶段二内容 | M3 编排器消费协调域 + M4 前端协调页从手工表单转为编排计划面 |
| 5 | §4.1 根本张力（2026-10-04 拍板） | **按 C 推进**：协调域**窄化为「计划 DAG 层」**——只保留 `plans + tasks(depends_on)` 作编排器的跨任务计划/依赖（唯一不重复的能力）；`objects/conflicts/verifications` **阶段二不接线**（由 `findings`/`func_kb`/`verify.py` 承担），**不删不双写**。否决 A（双账本必然漂移）/ B（依赖做进 tasks 需动 claim/看板/尝试树全链路，改动面最大） |

## 0-bis. 实施记录（阶段一 M1+M2，2026-10-04）

- **M1 委托纪律**：新增模块常量 `DELEGATION_DISCIPLINE`（`core/orchestrator/orchestrator.py`），经 `{delegation_discipline}` 槽注入 `ORCH_SYSTEM_PROMPT`（tick）与 `CHAT_SYSTEM_PROMPT`（对话轮）两处。四条=综合不可下放 / 委托单自包含 / 续用窗 vs 新开窗 / 并行 fan-out。纯提示词，控制流不动。
- **M1 附带修正（实施时发现的既有 bug）**：提示词里写的工具名 `publish_task` / `spawn_session` **两个工具都不存在**——会话中心化（2026-09-25）后真实工具是 `delegate`（`_tool_delegate`，测试亦按此名驱动），`spawn_session` 已退役。原提示词从未出现 `delegate`，等于让 LLM 调不存在的工具。已全量改 `publish_task`→`delegate`、删 `spawn_session` 段、L0/L1 notice 与 `_phase_section` 注入文案同步改。**这是 M1 的硬前置**（不改则纪律段与工具名互相矛盾）。
- **M2 结构化委托回执**：**决定不加新事件 kind**，改为在 `TaskQueue._finish`（`core/blackboard/tasks.py`）对**绑窗委托**（`target_session` 非空）的 `task.done`/`task.failed` payload 追加 `receipt={role,status,findings[:5],artifacts[:5],refs[:5],attempts}`。理由见 §4 待打磨 #3。消费面：①`collect_wake_triggers` 的 `task.failed` 摘要补「产出 N 发现/M 产物」；②前端 `events.tsx` 的 `taskDoneSummary`/`taskFailedSummary` 补「🏷N发现 · 🔧N产物」。失败只 log 不影响收尾。
- **§4 待打磨 #3 决议（实施时定）**：**采用「富化既有 `task.done/failed` payload」而非新增 `delegation.finished`**——单一真相源、零事件表膨胀、前端/触发器无需双份维护，且不新增事件 kind 就无需动 FILTERS/EventStyle。
- **测试**：`tests/test_orchestrator.py::test_delegation_discipline_injected_both_prompts`（纪律注入两处 + 无过期工具名）、`test_blackboard.py::test_delegation_receipt_on_terminal`（绑窗带/未绑窗不带 + failed 路径）、`test_orch_wake.py::test_collect_failed_receipt_appends_output_counts`；L1 提示词断言随 M1 改版更新（`审批收件箱`→`执行审批`）。**全量 1433 passed；前端 `npm run build` 零 TS 错误**。
- **未做（后置/不做）**：`delegation.finished` 新事件（改富化 payload）、M3/M4（阶段二）。

## 0-ter. 实施记录（阶段二 M3+M4，2026-10-04）

- **M3 计划 DAG 层**：`coordination_tasks.task_id` 幂等迁移；`CoordinationStore.refresh_readiness` 单向同步真实 tasks 状态并自动完成 active 计划；新增 `get_task`、`set_dependencies`（同计划校验+环检测）、`bind_task`、`unmet_dependencies`、`plan_view`。
- **M3 编排器接线**：新增 `plan_work` 工具（最多 20 节点，仅登记计划，不开窗）；`delegate(plan_node_id)` 依赖闸与角色一致性校验，成功回填真实 task；L1 `delegate_window` 批准处理器同样校验并绑定；`_stats.plan` 注入 active 计划摘要。
- **M4 前端**：协调页移除人工创建计划/添加节点/登记 objects/冲突/手动验证入口；保留计划列表、暂停/恢复、依赖图、驾驶舱与旧 objects/conflicts 只读兼容面；节点显示真实 `task_id` 与未满足依赖原因。
- **测试与验证**：后端协调/编排器 **87 passed**；前端 `npm run build` 零 TS 错误；API 全套重跑中唯一失败为既有 mission 派生异步时序，单测隔离重跑通过（flaky，非本次改动）。
- **边界**：objects/conflicts/verifications API 与存储保留，但阶段二不由编排器双写；不改 tasks 表、不把状态写入 chat 线程。

## 1. 背景与问题

**动机**：cc-haha 是一套成熟的多智能体协调实现，其「协调者」提示词纪律与结构化回执协议有可借鉴的工程价值。同时，本项目 `core/coordination/` 是一个**没有任何消费者的孤儿域**——`CoordinationStore` 只被 `app.py` 和它自己引用，`DESIGN.md` 零提及，编排器完全不知道它存在，前端 `MultiAgentCoordinationView` 是纯手工表单。两个问题合并处理。

**问题一（编排器纪律缺口）**：`ORCH_SYSTEM_PROMPT` 有角色/枚举/去重/预算/边界纪律，但**缺「如何写一张高质量委托单」的显式纪律**——尤其「协调者必须自己综合 worker 产出、禁止把理解下放」与「何时复用窗、何时新开窗」两条，目前靠 LLM 自由发挥。

**问题二（孤儿协调域）**：协调域是一套人工驱动的计划/依赖/对象/冲突/验证 DAG，六张表（`coordination_plans/tasks/objects/object_artifacts/conflicts/verifications`），有 `refresh_readiness`/`verify_task`/`verify_plan` 完整逻辑，但**无调度器、无 agent、无编排器接线**——`core/coordination/CLAUDE.md` 自己写着「后续调度器、资源租约、验证智能体和恢复机制应消费本域数据」，而这一天从未到来。

**明确不做（防过度工程）**：TeamFile/邮箱/文件锁（事件总线 + `session_inbox` 已覆盖）；四 spawn 路径（`delegate_window`/`spawn_session`/子代理 `publish_task` 已覆盖）；Worktree 隔离（本项目单工作区模型）；Fork 字节一致缓存（子代理是独立会话窗，前缀不同，收益不成立）；bubble 权限（本项目审批体系更强）。

## 2. 现状盘点（2026-10-04 核实）

**cc-haha 侧**（仅列拟借鉴项）：
- 协调者 = **一段系统提示词**（`coordinatorMode.ts:113`），四段式 Research(并行)→Synthesis(仅协调者)→Implementation→Verification；含 continue-vs-spawn 决策表（`:282-295`）与「Never write "based on your findings"」纪律（`:261`）。
- worker 结果以 `<task-notification>` XML 作为「user 消息」回灌协调者循环（`framework.ts:300` `enqueueTaskNotification`），带 `status/summary/result/usage`。

**本项目侧**：
- 编排器已是协调者：`publish_task` → `delegate_window`（L1）/ 直接开窗复用窗（L2）/ `_propose`（L0）；`_pick_reusable_window`（`orchestrator.py:1930`）已实现「复用 armed+idle 角色匹配窗」。
- 结果回流靠 `task.done`/`task.failed` 事件 + `result_note[:300]` + 唤醒触发器（`WAKE_TRIGGERS`，`task.failed`/`task.starvation`/`phase.gate_open`）+ `task_detail` 全文工具。
- 协调域六表结构见 `core/coordination/store.py:41-123`；状态机 `pending/ready/running/blocked/completed/failed`（与 tasks 表的 `open/claimed/done/failed` **不同**）；`verify_task`（`:436`）按「有证据对象 + 无 open 冲突」判 passed，自动建补充证据/复核冲突 followup 任务。
- 协调域端点全在 `app.py:5009-5175`，每个写操作落 `coordination.*` 黑板事件（author=human / coordination-verifier）。

## 3. 设计详述

### 阶段一：协调协议增强（低风险，不动架构）

#### 3.1 M1 协调者提示词纪律（纯提示词）

在 `ORCH_SYSTEM_PROMPT`（tick）与 `CHAT_SYSTEM_PROMPT`（对话轮）各加一节「委托纪律」，提炼 cc-haha 三原则：

1. **Synthesis 是协调者不可下放的工作**——收到 worker 回执后必须自己读懂、定位到 file:line/资产/发现 id，再写下一张委托单；**禁止**「基于该会话的发现，继续修复」这类把理解下放给 worker 的委托（对齐 cc-haha `:261` 与项目既有「Grounding 引用容错」精神）。
2. **委托单必须自包含**——worker 是独立会话窗，**看不到编排器的态势与对话**；委托单须含：目标、依据（refs/资产/发现 id）、完成定义、边界。这是本项目「worker 看不到对话」与 cc-haha 完全一致的约束。
3. **continue-vs-spawn 决策表**（对齐 `:282-295`）：研究恰好覆盖待改文件→复用窗续派；研究宽泛而实现聚焦→新开窗；修正/延伸近期工作→复用窗；验证他人刚写的产出→新开窗（避免实现假设污染）；方向整体错误→新开窗。落地为提示词表格 + 对应现有 `_pick_reusable_window` 的调用语义（复用=命中 armed+idle 窗；新开=`session_factory`）。
4. **并行 fan-out 纪律**：只读研究类委托可并行多发；写入类按目标/文件分区串行。对齐 cc-haha `:215-220`。

> 注：本项**只改提示词字符串**，不改控制流；`_pick_reusable_window` 已有，纪律只是教 LLM 何时用它。

#### 3.2 M2 结构化委托回执信封（协议增强）

> **实施修正（2026-10-04）**：实际采用**富化既有 `task.done`/`task.failed` payload** 而非新增 `delegation.finished` 事件（理由见 §0-bis）。下文「新增事件」为原始设计，保留作推导背景。

**问题**：编排器当前从 `task.done` 事件 + `result_note[:300]` + 分散的 usage/findings 事件重建「谁完成了什么」，唤醒轮（M4）的 `wake_brief_text` 只摘 `note`，信息密度低。

**设计**：新增 `delegation.finished` 事件（`author="orchestrator"` 域外，实际由 `TaskQueue.complete/fail` 在委托终态时发），payload 结构化信封：

```json
{
  "task_id": "task-…", "session_id": "sess-…", "role": "…",
  "status": "done|failed", "blocked_reason": "…",
  "summary": "<result_note 首段>", "result_note": "<全文，cap 2000>",
  "usage": {"tokens": N, "tools": N, "duration_ms": N},
  "artifacts": ["art-…"], "findings": ["find-…"], "refs": ["…"]
}
```

- **不替换** `task.done/failed`（保留既有触发器与前端渲染），是**追加**一条聚合信封，供编排器唤醒轮/tick 态势直接消费。
- 唤醒轮 `wake_brief_text`（`orchestrator.py`）在 `task.failed` 触发时改摘信封（status/summary/usage），信息更全。
- 前端 `EventRow` 可为 `delegation.finished` 加专属摘要行（`📬 <role> 回执：<summary> · 🪙N · 🔧N`），与 cc-haha 的 `<task-notification>` 观感对齐。

### 阶段二：窄化协调域为「计划 DAG 层」（§4.1=C 定案）

> **§4.1 定案（2026-10-04，用户拍板 C）**：协调域六表中**只有 `plans + tasks(depends_on)` 不重复**（跨任务计划/依赖，编排器唯一缺的能力）；`objects`（↔`func_kb`/`findings`/`assets`）、`conflicts`（↔finding 去重/FP）、`verifications`（↔`core/verify.py` + reconcile）均与既有子系统重复。故**只接线计划 DAG 层，其余三组不接线、不删、不双写**。否决 A（双账本必然漂移）/ B（依赖做进 tasks 需动 claim/看板/尝试树全链路，改动面最大）。

#### 3.3 M3 编排器消费「计划 DAG 层」

**目标**：让 `plans + tasks` 成为编排器的**跨任务计划面**——当前编排器只有 `parent_id` 深度 1 分解 + 每任务 `plan` 步，**没有跨任务依赖**。

**M3.1 计划登记工具 `plan_work`（新编排器工具）**
- 入参：`plan_name`、`objective`、`nodes:[{title, role, depends_on:[节点序号|标题], priority}]`。
- 行为：`create_plan` + 逐条 `add_task`（`depends_on` 解析为已建节点 id）；返回 `plan_id` + `[{index, node_id, title}]`。
- **自主档：不限档**（只登记计划，不派单/不开窗/零执行，L0 亦允许）；真正派单仍走 `delegate` 全闸门。

**M3.2 节点 ↔ 真实任务绑定**
- `coordination_tasks` **增列 `task_id TEXT`**（协调域 `ensure_schema` 加幂等 `ALTER TABLE ... ADD COLUMN`，缺省空）——节点被派单后回填真实 `tasks.id`。
- `delegate` 增可选参 `plan_node_id`：派单成功后回填 `coordination_tasks.task_id` 并把节点置 `running`。
- 绑定放协调域一侧（**不改 `tasks` 表、不碰 `tasks.context` 唯一写点**）。

**M3.3 依赖闸（确定性，服务端）**
- `_tool_delegate(plan_node_id=...)` 派单前查节点依赖是否全 `completed`，未满足回 `[拒绝] 计划节点依赖未完成：…`（对齐现有 role/gate 预检先例）。
- 不带 `plan_node_id` 的派单不受影响（保持现有行为）。

**M3.4 状态单向同步（tasks → coordination）**
- 扩展 `CoordinationStore.refresh_readiness`：①先按 `task_id` join `tasks` 同步节点终态（done→completed / failed→failed / claimed→running）；②再按 `depends_on` 派生 ready/blocked；③全部节点 completed → 计划置 completed。
- 触发：既有 `overview()`（编排器 tick 与前端 3s 轮询都会触发）——**零新调度、零新表**。
- **严格单向**：coordination 永不反向改 `tasks`（避免双写冲突）。

**M3.5 态势注入**
- 编排器 `_stats` 增 `plan` 段：当前 active 计划的节点清单（node_id/标题/role/status/依赖/绑定任务）+ 就绪清单——LLM 据此决定「下一张派单给哪个就绪节点」。
- 无计划=空段（向后兼容，脚本/旧测试不受影响）。

**明确不做（C 定案）**：`objects/conflicts/verifications` 接线；把计划写进 chat 线程；coordination → tasks 反向写。

#### 3.4 M4 前端协调页转编排计划面

- **保留**：计划列表 + 任务依赖图 + 驾驶舱（会话/泳道/事件）——编排器写的计划自动出现在这里。
- **去手工主入口**：移除「创建计划 / 添加协调任务」表单（计划由 `plan_work` 产生；人类干预走既有 PATCH 节点状态/角色 + 暂停/恢复计划）。
- **节点卡增强**：显示绑定真实任务（`task_id` → 跳直播间会话）+ 就绪/阻塞原因。
- **结构化黑板段（objects/conflicts）与「验证计划/验证」按钮**：阶段二不接线 → 折叠或隐藏（API 保留，不删）〔待打磨〕。

## 4. 待打磨清单

1. ~~阶段二的根本张力~~ **已拍板（2026-10-04）= C**：窄化为计划 DAG 层（见 §0 行 5 / §3.3 引言）。
2. M1 提示词纪律的具体文案与注入槽位（`ORCH_SYSTEM_PROMPT` 已有多个 `{…}` 槽，勿与 `{goal_section}`/`{mission_section}` 冲突）；continue-vs-spawn 是否可机械化为代码提示而非纯提示词。**（M1 已实施，本条收尾。）**
3. ~~M2 `delegation.finished` 与既有 `task.done/failed` 的事件重复度~~ **已决议（2026-10-04）**：采用富化 `task.done/failed` payload，不新增 kind。
4. **M3 新增**：`coordination_tasks.task_id` 的幂等迁移方式（协调域 `ensure_schema` 现只有 `CREATE TABLE IF NOT EXISTS`，加列需 `PRAGMA table_info` 守卫的 `ALTER TABLE ADD COLUMN`）——是否顺带引入协调域自己的版本号 meta。
5. **M3 新增**：`coordination_plans.objective` 与项目 `phase_goal`（goal 统一后唯一目标判据）的关系——默认各自独立（objective 可写 goal 摘要，不强制复用）；是否要求「有计划必挂 goal」。
6. **M3 新增**：状态同步边界——跨窗换人/交棒/requeue/cancel 时，节点 `task_id` 指向的任务终态变化如何跟随（`refresh_readiness` 按 `task_id` 重读即可覆盖，但 requeue 后任务回 open/claimed 时节点需回 running，需确认单向同步的幂等性）。
7. **M4 新增**：objects/conflicts 段与「验证」按钮是**折叠**还是**隐藏**（API 均保留不删）；人类写入口保留哪些（节点状态/角色 PATCH、计划暂停/恢复）。

## 5. 实施切分

- **阶段一（已实施 2026-10-04，全量 1433 passed；详见 §0-bis）**
  - **M1**：`DELEGATION_DISCIPLINE` 注入 tick+对话轮两处系统提示 + 顺带修正过期工具名（`publish_task`→`delegate`、删已退役 `spawn_session`）。
  - **M2**：绑窗委托终态 `task.done/failed` payload 富化 `receipt`（**不新增事件 kind**）+ 唤醒轮计数 + 前端摘要行。
- **阶段二（已实施 2026-10-04，详见 §0-ter）**
  - **M3 后端**（按 C）：协调域 `task_id` 迁移/单向同步/自动完成；`plan_work` 与 `delegate.plan_node_id` 依赖闸/绑定；`_stats.plan` 态势注入。
  - **M4 前端**：去手工建单表单与旧域写入口；依赖图保留执行面；objects/conflicts 只读兼容面。
  - **验证**：协调/编排器 87 passed；前端 build 零 TS 错误。

依赖关系：M3 内部 ①→②→③④⑤ 顺序；M4 依赖 M3。阶段一已实施（2026-10-04，1433 passed），阶段二独立于阶段一。

## 6. 关联代码（阶段二）

- `core/coordination/store.py`（`ensure_schema` 加列 / `refresh_readiness` 扩展 / `add_task` 返回 node id）
- `core/orchestrator/orchestrator.py`（`ORCH_TOOLS` 增 `plan_work` + `delegate.plan_node_id`；`_tool_plan_work` / `_tool_delegate` 依赖闸；`_stats` 增 `plan` 段）
- `core/api/app.py`（`coordination` 端点无需改；若加计划自动完成状态可见性则核对 `GET /coordination`）
- `webui/src/views/MultiAgentCoordinationView.tsx`（去手工表单 / 节点卡增强 / 折叠 objects·conflicts 段）
- `webui/src/lib/types.ts` + `api.ts`（`CoordinationTask` 加 `task_id`；`plan_work` 无前端面）
