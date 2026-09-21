# core/orchestrator/

> 主代理（DESIGN.md §6.4/§6.8）：监控态势 → LLM 决策 → 派生任务/开窗/汇总。自身不执行命令、不写分析。
> 构造：`Orchestrator(project_id, bb, llm, session_factory, config=None, packs_root="packs", track=None, gate=None, on_task_published=None, state_loader=None, state_saver=None, heartbeat=None, autonomy_provider=None, campaign=None)`。后七个全是鸭子回调、None=脚本/旧测试不接线（纯内存行为）：gate/on_task_published 是批 2 预算闸门；state_loader/saver 装载/落盘游标轮数，heartbeat 每 LLM 步续租 tick 租约；**autonomy_provider（批 4）实时返回归一化 autonomy dict，level=="L1" 时 spawn 转人工审批**，本模块不 import core.autonomy；**campaign（2026-09-19 战役记忆全局库，§16.5）= tick 态势召回注入，None 不注入**。**批 6：`OrchestratorConfig.propose_only=True`（API 按实时档位 L0 单点注入）=L0 提案模式**，publish/spawn 只校验不写实体。

## 轨接线（§4.5.5）

- **任务类型注册表**：track 给定时 `self.task_types = load_task_types(...)`（含 generic）；LLM `publish_task` 传 `allowed_types=self.task_types`，未注册类型拒收并回填 `[拒绝]`，任务不入队。噪声缺省取注册表该类型默认值（如 assessment/exploit=low）。
- **enum 提示层**：`_orch_tools()` 返回工具表深拷贝，把本轨合法 task_type 作为 enum + 描述下发（让 LLM 一次填对；无 track 退回 ORCH_TOOLS 原表）。服务端拒收仍是最终护栏，勿删。
- **角色目录注入**：`role_catalog` = 轨 roles/*.yaml（name + description + task_types），系统提示含「可开角色目录」「静态分诊」段；spawn_session 仍受 config.allowed_roles 白名单 + max_sessions 双约束。
- **饿死检测** `_starvation_warnings`：open 任务类型①未注册（`未在 <track> 轨 ...`）或②已注册但无专才角色（`_generalist` 的 task_types=null 不计覆盖，reason=`无专才角色`）→ 落 `task.starvation` 事件；`(task_id, reason)` 去重，多轮 tick 不重复报。**v0.71（2026-09-20）role 维度修订**：删「无底色匹配会话在岗」分支（任务即窗口模型下每任务必有专属窗，该告警已无意义）；新增「open+target_session 指向 closed 会话 → 待调度器重绑」告警（绑窗在窗关闭瞬间会失败，重绑由调度器 60s sweep 兜底）。
- 无 track 时退化为仅 generic、不做饿死判断（旧调用兼容）。

## 文件

- `state.py`（批 3，§6.8/机制 1.9）— tick 租约 + 编排状态：`load_or_create`（无行返零值默认不落盘）/`save_fields`（白名单：event_cursor/cycles/last_digest_cycle/chain_*/last_replan_at(A5)/**last_derive_at·last_derive_result（v13，2026-09-18 mission 派生结果，API 层 _mission_on_done 写）**，非法字段 KeyError）/`increment_counters`（chain_ticks/auto_ticks_total，批 5 先备）/`acquire_tick_lease`（单 `bb._tx()`：建行→读租约→他人未过期抛 `TickLeaseError`→否则抢占，TTL 默认 900s）/`renew_tick_lease`（持有者心跳，易主抛错）/`release_tick_lease`（仅 owner 自清）。租约时间 UTC ISO 比较，崩溃靠 TTL 自然到期。

## replan_priorities（A5：一次性优先级重排）

- 独立于 tick 的**一次性 planner 轮**：`REPLAN_TOOLS` 只含 `set_priorities(updates:[{task_id,priority}])`（不允许 publish/spawn/digest），`REPLAN_SYSTEM_PROMPT` 槽 `{open_tasks}{blocked}{role_catalog}`，排序原则 0-2 被依赖前置/解阻塞、3-5 常规、6-9 可延后；claimed/done/failed 与清单外 id 不得提交、相同值不提交、只调一次。
- `replan_priorities()`：heartbeat→list_tasks→**无 open 直接返 `{updated,skipped,note}` 零 LLM 调用**→一轮 llm.chat+`record_llm_usage(source="orchestrator")`→逐条校验更新：bad-task_id/duplicate/not-open-or-unknown/bad-priority（先拦 bool）/unchanged/update-rejected 逐条进 skipped 不中断；合法且变化者 `tq.update_task(by="orchestrator-replan")` 逐行落 task.updated；**仅 updated 非空**才发 `orch.replan_priorities{updated,skipped_n}`。
- tick 纪律新增「**分析-分解-分派**」：publish_task 用 **role 参数**结构化指定建议认领角色（本轨已注册角色 id，留空=不限；scope 写角色的旧文本口径已退役）+ 初始 priority（0-9 整数，小者优先）；`_stats().tasks.blocked_plan` 聚合 claimed 任务 plan 中 blocked 步（`{task_id,type,blocked,note}`）喂给重排提示优先解阻塞。
- API 接线：手动端点任何档可跑（不受 30s/预算限制，租约占用 409）；L2 自动去抖在 core/api 层（见 api/CLAUDE.md），本模块不感知去抖；手动/自动共用 tick 租约严格单飞。

## tick() 流程

state_loader 装载持久游标/轮数 → `expire_leases`（回收过期租约并进系统提示）→ 态势收集（任务/发现/会话 + 增量事件 + 饿死告警 + 过期租约清单）→ LLM 工具循环（ORCH_TOOLS：publish_task / spawn_session / write_digest / done；每步 chat 前 heartbeat 续租）。digest_every 到期强制 write_digest。两条出口都走 `_finish_tick`：state_saver 落盘 event_cursor/cycles/last_digest_cycle（失败只 log 不丢已落地实体），返回**结构化 dict** `{summary, published:[task_id], spawned:[{session_id,role}], digest:str|None, proposals:[{op,args,event_id}]}`（proposals 仅 L0 提案模式非空；旧脚本按 `r["summary"]` 渲染）。注意游标在态势收集时推进，**上一轮自身产生的事件（llm.usage/digest）下轮仍可见**，属既定口径；backlog 超窗（>100）时只喂最新 100 条（`bb.latest_event_id` 取末端），游标**一次跳到 tick 开始时末端 id**，旧事件不逐轮回放（走查抓到过每轮 +100 爬行 bug，有回归测试）。

- **L0 提案模式（批 6，§6.8）**：`config.propose_only` 时 publish_task/spawn_session **校验照跑但不写实体**——publish 复刻注册表/噪声枚举/非 passive 必带 conflict_keys 校验（刻意不调 tq.publish 以免真发任务），spawn 仍过白名单/max_sessions；不走 gate、不计数、不发 task.published/session.spawned、不建审批。通过后经 `_propose(op,args)` 发 `orch.proposed{op,args}`（author=orchestrator）、收集进结构化 `proposals`（args 含缺省补全的 noise_budget），回填「已提案 #event-id，等待人类采纳」。同 tick write_digest 不受影响。系统提示经 `L0_AUTONOMY_NOTICE`（{autonomy_notice} 槽，优先于 L1 notice）。采纳是纯 API 侧行为：人以 created_by=human 走 POST /tasks、POST /agents，本模块无采纳代码。
- **C2 指挥编排器指令 + 判据模板（§6.9/§6.4）**：`_pending_directives/_overview` 注入「⚠ 人类指令（最高优先）」、`_finish_tick` 轮末落 orch.directive.done；`judgments.py resolve_criteria` 三层判据兜底（mission>所选模板>mode 内置默认）供 API 派生闸与提示共用；config/judgment_templates.json 用户模板。
- **C2 作战模式 mission 注入（§6.9；R2 2026-09-17 mode 退役→轨级）**：_stats 增 mission 视图（track/mission/redteam_roe，读项目 config）；ORCH_SYSTEM_PROMPT 增 {mission_section} 槽（轨级行动边界+ROE+判据清单；redteam 缺 ROE=按 pentest 上限兜底提醒）与纪律第 8 条（mission 判据达成或资产穷尽才 done）；pentest 轨缺省注入影响证明级上限提醒。
- **战役记忆召回（2026-09-19 借鉴 dsh，§16.5）**：`_campaign_section()`——campaign 注入时按「mission 文本 + high_value 目标值 + 同目标负载键」做 query，`CampaignMemory.recall(track=, capability=项目首能力)` 取 top-5（热度×30 天线性衰减，召回即计 usage），「既往战役打法（跨项目记忆，仅参考）」段注入 `{campaign_section}` 槽（单条截 300 字）；未注入/零命中/异常一律空段，**召回失败绝不阻断编排**。
- **态势注入增强（2026-09-17，§6.4 定稿块；uncovered 口径 2026-09-18 修订）**：`_assets_view` 返回 HVT 集合（meta.tags 含「高价值」）+ `_covered_ids`；**covered = open/claimed 任务文本命中 ∪ 资产 status ∈ visited/scanning/tested_clean（任务 done 后资产状态回流，uncovered 才能收敛；2026-09-18 前旧口径只看任务文本，与判据文案不符致永不收敛）**，uncovered 排序 HVT 优先；`in_progress`（visited/scanning cap 20）与顶层 `done_count`（tested_clean，"挖完"口径）；`high_value` 段（HVT 资产带 status/covered/挂该资产发现 cap 3，未到 tested_clean 持续列出）；`_recent_tasks_view`（recent_closed=done/failed 合并 cap 30 带 result_note[:100] + claimed_now 计划进度）；`_overview` 尾部注入最新 `project.digest` 全文（cap 2000，经 `bb.latest_digest`）。**内部键 `_hvt_ids/_covered_ids` 在 `_stats` 里 pop 后再返回**（勿漏，否则进 overview JSON）。
- **L1 开窗审批分流（批 4，§6.8）**：`spawn_session(role, reason)` 先过白名单→内存 max_sessions→gate 预检（cap/预算，两条路径共用），随后读 autonomy_provider：**L1 要求 reason 非空**（空回填 `[拒绝]`），不调工厂，改 `bb.request_approval(action={"op":"spawn_session","role","reason"}, risk="low", requested_by="orchestrator")`，回填「已提交审批 appr-x，批准后自动建窗开跑」；**不进结构化 `spawned`、不发 session.spawned**（窗未开），批准后的建窗/开跑由 API decide 处理器负责。L2/未接线维持直接开窗；L0 在闸门前先走上面的 propose_only 提案分支。L1 时系统提示追加 `L1_AUTONOMY_NOTICE`（占位 `{autonomy_notice}`）。
- **publish_task 透传 `refs`（批 1B）**：任务以某发现为依据时（如「验证 find-x」）填 refs；TaskQueue 与正文正则 `find-[0-9a-f]{12}` 自动抽取合并为 context_refs，该发现被标 false-positive 时执行者收到强制自评通知（§6.7 的 1.6，撤回广播 orch 经下轮 tick 事件游标自然可见，本模块无特判代码）。
- **v14 任务绑角色与防碎发布闸（2026-09-18，DESIGN §6.4）**：`_tool_publish_task` 签名加 `role=""`——①**role 前置校验**（`role_exists` 失败回 `[拒绝] 未注册的 role`，load_role 会静默回退不可用）；②**dedup 预检**在单轮发布硬闸后、L0 提案分支前（`dedup_fp(task_type, scope, objective)` + `find_dedup_target` 命中回 `[复用] task_id` 不发；L0 提案 args 也先查重）——此前编排器是三写路径中唯一不查重的空白点；③publish 透传 `role=role, allowed_roles=list_roles(轨注册清单)`（**app.py 内有同名端点函数，核心侧 import 用别名**）；④L0 提案 args 带 role。`_stats` 增 `targets`（`_target_load`：open+claimed 按 target_keys_of 聚合 top5，带 at_limit 标）注入态势；ORCH_SYSTEM_PROMPT 纪律 2 改 role 口径 + 注明同 target 阈值 `max_tasks_per_target`（=4）槽。
- **预算闸门（批 2，§6.8）**：每次 chat 后 `record_llm_usage(source="orchestrator")` 记账（失败只 log 不阻断）；publish/spawn 调 `self.gate(action)`——返回原因串则不写实体，`[拒绝] <原因>` 回填 LLM 改道（publish 闸门在噪声缺省判定**之前**）；publish 成功后 try/except 调 `on_task_published(task_id)`（**v0.71 签名带 task_id**：API 侧回调=tasks_published+1 + `_bind_task_window` 发布即建窗——建窗失败不回滚发布；计数失败不回滚任务）。gate 由 API tick 端点注入闭包，实时重读配置，本模块不 import core.autonomy。
- **v0.71 spawn_session 定位收窄（2026-09-20，DESIGN §6.4「任务即窗口」）**：publish_task 发布成功后系统自动为该任务建立专属执行窗（按建议角色装配、按并发上限起跑）——**编排器不应也不需再为执行窗调 spawn_session**；spawn_session 仅用于纯侦查/纯对话辅助窗（不挂任务）。ORCH_SYSTEM_PROMPT 可用工具段、L1_AUTONOMY_NOTICE、主代理纪律 #2 均已同步该口径，改 prompt 时勿回退。**v0.72 修订**：L1 下 publish_task 发布即建待命窗+按任务提**执行审批**单（app 层 `_on_published` 负责，编排器无感）；L1_AUTONOMY_NOTICE 与 spawn_session 工具描述已同步「任务自动建窗，spawn_session 只开辅助窗」口径——不再有「不即时建窗/建任务窗审批」旧文案，勿回退。

## 约定

- `session_factory(role) -> AgentSession` 由调用方注入（API/脚本），轨与能力包在工厂内闭包绑定；本模块对 Agent 只鸭子依赖 `.session["id"]`。API 注入的是**注册式工厂**（返回即进 app.state.agents，编排开窗不再是孤儿窗）。
- 人类随时可直接向 TaskQueue 插手（脚本/API 也可以不经过注册表直插，UI 走 API 校验）。
- 参照实现：scripts/demo_orchestrator.py（ctf）、scripts/demo_pentest.py（assessment，approval 自批标 demo-script(auto)）。
- 测试：tests/test_orchestrator.py（拒收/噪声缺省/饿死去重/角色目录注入/结构化结果/跨实例游标轮数/心跳/**L1 转审批：不建窗/必须 reason/gate 预检/L2 仍直接开**，`make_orch(..., autonomy_provider=)`；**L0 提案：不发任务不建窗/非法类型与缺 conflict_keys 仍拒/一轮多提案+digest 照常/L0 notice 注入**，`config=OrchestratorConfig(propose_only=True)`）+ tests/test_orchestrator_state.py（租约获取/释放/过期抢占/owner 误释放/白名单/并发只赢一个）。
