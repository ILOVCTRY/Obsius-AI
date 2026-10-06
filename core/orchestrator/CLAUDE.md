# core/orchestrator/

> 主代理（指挥，DESIGN.md §6.4/§6.8）：**组建队伍（`build_team`）+ 亲自执行（`execute`）** + 监控态势 / 汇总简报 + 异常订阅唤醒扫描（M4）。普通 LLM 会话 + 专用工具集——大块工作交团队子代理，小范围亲自下场。
> 构造：`Orchestrator(project_id, bb, llm, session_factory, config=None, packs_root="packs", track=None, gate=None, on_task_published=None, state_loader=None, state_saver=None, heartbeat=None, autonomy_provider=None, campaign=None, meta_loader=None)`。后八个全是鸭子回调、None=脚本/旧测试不接线：gate/on_task_published 是预算闸门；state_loader/saver 装载/落盘游标轮数，heartbeat 每 LLM 步续租 tick 租约；**autonomy_provider 实时返回归一化 autonomy dict，level=="L1" 时 build_team/execute 转人工审批**，本模块不 import core.autonomy；**campaign= tick 态势召回注入，None 不注入**；**meta_loader 实时返回 project.json meta（goal/persona），None 不注入**。**`OrchestratorConfig.propose_only=True`（API 按 L0 注入）=L0 提案模式**，build_team/execute 只校验不写实体。

## 工具面（ORCH_TOOLS，2026-10-06 任务机制退役后）

- `build_team(name, goal_text, members[])`：**组建 Team（指挥核心职责）**——成员 role 从本轨专家池选（留空=按职责现场定义的动态成员）。只登记 draft roster（调 `TeamStore.create_team`），**不启动、不派单**；人类在「指挥」页 preflight + 四项确认后才启动。`_tool_build_team` 校验名称/成员 key 唯一/role 在池与白名单/runtime·threat_class 合法。
- `execute(objective)`：**指挥亲自执行**——建 `_generalist` 会话跑 `run_team_execution(ExecutionContext)`（完整 Agent 工具面：命令/文件/黑板/知识库/浏览器），tick 内同步执行、步数上限 `config.execute_max_steps=40`；受 gate + `max_sessions` 约束。
- `write_digest(summary)` / `done()`：写 project.digest / 结束本轮。
- 只读查询三工具 `ORCH_QUERY_TOOLS`（bb_overview / budget_status / session_list）——**零写权红线**，用于核对与决策（渐进披露：态势看摘要、存疑拉详情）。auto-attack 研判模式（`config.analyze_only`）只下发只读查询+done。
- **已退役工具**（勿回退）：delegate / plan_work / cancel_task / requeue_task / spawn_session / task_detail。

## 轨接线（§4.5.5）

- **任务类型注册表**：track 给定时 `self.task_types = load_task_types(...)`（含 generic）。
- **角色目录注入**：`role_catalog` = 专家池按轨过滤清单（`list_experts`+`load_expert`，name+description），系统提示含「可开角色目录」段；建队成员 role 校验受 `config.allowed_roles` 白名单约束。
- 无 track 时退化为仅 generic。

## tick() 流程

state_loader 装载持久游标/轮数 → 态势收集（`_stats` + 增量事件 + 人类指令 + 团队视图）→ LLM 工具循环（ORCH_TOOLS；每步 chat 前 heartbeat 续租）。digest_every 到期强制 write_digest。两条出口都走 `_finish_tick`：state_saver 落盘 event_cursor/cycles/last_digest_cycle（失败只 log），返回**结构化 dict** `{summary, published, spawned, teams, digest, proposals}`（proposals 仅 L0 非空）。游标在态势收集时推进（上一轮自身事件下轮仍可见，属既定口径）；backlog 超窗（>100）只喂最新 100 条，游标**一次跳到 tick 开始时末端 id**（勿逐轮回放）。

- **态势 `_stats`**：mission 视图（track/mission/roe）+ high_value（HVT ∪ verified 高危资产自动推导，`derived=true`）+ **`_teams_view`（Team 名册 cap 20）+ `_recent_runs_view`（最近 Team Run cap 20 + running_members）** + findings（top 20，verified/exploited 优先）+ 活跃会话（closed 出清）+ approvals + `_assets_view`（HVT/uncovered/coverage）。**任务退役后无 tasks 段**；`_overview` 无参（尾部注入最新 project.digest 全文 cap 2000）。内部键 `_hvt_ids/_covered_ids` 在 `_stats` pop 后再返回（勿漏）。
- **上下文预算**：全量段压常数级——findings 只进 top 20、closed 会话出清、事件窗剔除纯观测 kind `llm.usage`/`llm.thinking.delta`（`_ORCH_EVENT_EXCLUDE`，游标照推不重放）。被裁细节走 bb_overview 按需拉，**勿回退成全量**。
- **行动边界**：`mission_boundary_lines(track, config)` 是边界文案**唯一出处**（redteam ROE 四要素 / 其余轨影响证明级上限），`_mission_section` 与 API approvals 出口同源调用，勿复制第二份。
- **战役记忆召回**：`_campaign_section()` 按 goal 文本 + high_value 目标做 query，`CampaignMemory.recall` 取 top-5 注入；死路条目（tags 含 dead_end）单列「⚠ 既往死路」组。零命中/异常=空段，**召回失败绝不阻断编排**。
- **L0 提案模式**：`config.propose_only` 时 build_team/execute 校验照跑但不写实体——`_tool_build_team` 走 `_propose("build_team", ...)`、`_tool_execute` 走 `_propose("execute", ...)`，发 `orch.proposed{op,args}` 收集进结构化 `proposals`；系统提示经 `L0_AUTONOMY_NOTICE`。采纳是纯 API 侧行为（建队→`POST /teams`），本模块无采纳代码。

## 异常订阅唤醒（M4，§4.6）

- 类常量 `WAKE_TRIGGERS`（白名单 kind→冷却秒）= `{team.run.finished:600, budget.soft_warning:3600, phase.gate_open:600}`；`WAKE_LOOKBACK=1800s`（无锚点首启回看窗）+ `WAKE_MAX_EVENTS`/`WAKE_CHAT_WINDOW`。
- `collect_wake_triggers(bb, pid, *, now=None)` 纯函数——锚点=最近 proactive `orch.chat` 事件（triggers 字段 kind 级 id 防重 + created_at 冷却双维度）；`wake_brief_text(triggers)` 合成「〔主动唤醒〕…」user 消息（只进 LLM messages **不落 orch.chat 历史**）；`chat_turn(text, *, wake=None)` 唯一差异=回复 payload 加 `proactive:true, triggers:[kind]`（前端 🔔 徽章 + 下次锚点双用途）。触发接线在 API 层 `_post_tick`，本模块零新表零调度。

## 对话插队轮 chat_turn（对话化编排器 M1-M3）

- 入口 `chat_turn(text)`（API `POST /orchestrator/chat`）：人类消息落 `orch.chat{role:"human", text}`，回复落 `{role:"orch", text, tool_trace}`——对话历史=events 表 orch.chat 单一来源（`_chat_history()` 取最近 40 条）。
- **循环语义与 tick 不同**：每步 heartbeat 续租 → `llm.chat(tools=ORCH_TOOLS 全闸门同源)` → **无 tool_calls 即 break（纯文本=回答完毕）**，勿回退成 tick 的「催促调工具 continue」（会空转烧轮）；步数上限 `CHAT_MAX_STEPS=8`。
- **系统提示** `CHAT_SYSTEM_PROMPT`（与 tick 分立）注入 `_goal_section()`（M2 goal，tick 与对话轮同槽）与 `_persona_section()`（M3，**仅对话轮**，persona[:600]）。**插队轮只读边界**：不推进 event_cursor、不计 cycles、不动 last_digest_cycle、不消费 C2 指令、不写 state_saver。
- **手动持久压缩 `compact_chat()`（/compact，2026-10-06）**：直播间「指挥」页签输入 `/compact`（`POST /orchestrator/compact`，同 tick 租约 busy 409）——较早 orch.chat 消息经 LLM（`ORCH_COMPACT_SYSTEM`）压成摘要落 `orch.compact{summary,cutoff_id,...}` 事件；`_chat_history()` 读最近一条 orch.compact，只回放 `id>cutoff_id` 的消息并前置摘要（原本固定取最近 40 条，长对话下早期决策会被挤出窗口）。历史过短 noop 不烧 LLM；摘要失败/空不落事件。
- **上下文用量与自动压缩（`/context`，2026-10-06）**：`context_usage()` 返回 `{window,used,pct,threshold,source,breakdown}`——窗口 = `core.llm.tokenizer.context_window_tokens(self.llm)`（`model_context` 优先，缺省 256K）；占用取最近一条**编排** `llm.usage`（source ∈ orchestrator/orchestrator-chat，排除摘要调用 `orchestrator-compact`）的 input；breakdown = system(估算)/tools/messages 归一。`chat_turn` 收尾调 `_maybe_autocompact()`：≥`window×0.85` → `compact_chat()`（吞异常不阻断）。API：`GET /api/projects/{pid}/orchestrator/context`（只读不抢租约）。

## 分阶段工作流接线（pentest M1+M2）

- `_phase_section()`：meta_loader 提供的项目 meta 有阶段剧本时（`load_track_phases` 非空）拼「## 阶段工作流」段注入 `{phase_section}` 槽（tick 与对话轮同槽）——当前阶段/阶段目标/重心配额建议/出口门进度；book 空/异常=空段。
- **门进度只读不重算（单一事实源）**：出口门行读 meta 的 `phase_gate_state`（API 层 `_phase_gate_check` 每轮评估落盘）——**勿回退成现算**（注入在 tick 开始、判定在 tick 末，现算会用上轮 idle 制造矛盾窗口）。
- `state.py` v21 新列 **`derive_idle_rounds`**（_STATE_DEFAULTS/_EDITABLE_FIELDS）：派单空闲轮数，API 层 `_phase_post_tick` 维护（本模块只读）。

## state.py（批 3，§6.8/机制 1.9）

tick 租约 + 编排状态：`load_or_create`（无行返零值默认不落盘）/`save_fields`（白名单：event_cursor/cycles/last_digest_cycle/chain_*/last_replan_at/**last_derive_at·last_derive_result（v13 mission 派生结果，API 层写）**，非法字段 KeyError）/`increment_counters`（chain_ticks/auto_ticks_total）/`acquire_tick_lease`（单 `bb._tx()`：建行→读租约→他人未过期抛 `TickLeaseError`→抢占，TTL 900s）/`renew_tick_lease`（持有者心跳，易主抛错）/`release_tick_lease`（仅 owner 自清）。租约时间 UTC ISO 比较，崩溃靠 TTL 自然到期。

## 约定

- `session_factory(role) -> AgentSession` 由调用方注入（API/脚本），本模块对 Agent 只鸭子依赖 `.session["id"]`。API 注入的是**注册式工厂**（返回即进 app.state.agents）。
- 参照实现：scripts/demo_orchestrator.py（ctf）、scripts/demo_pentest.py（assessment）。
- 测试：tests/test_orchestrator.py（拒收/角色目录注入/结构化结果/跨实例游标轮数/心跳/**L1 转审批：不建窗**/**L0 提案：不发实体**/**M4 生命周期**）+ tests/test_orchestrator_state.py（租约获取/释放/过期抢占/owner 误释放/白名单/并发只赢一个）。
