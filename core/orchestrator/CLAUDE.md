# core/orchestrator/

> 主代理（DESIGN.md §6.4/§6.8）：监控态势 → LLM 决策 → 派生任务/开窗/汇总 + **异常订阅唤醒扫描（M4）**。自身不执行命令、不写分析。
> 构造：`Orchestrator(project_id, bb, llm, session_factory, config=None, packs_root="packs", track=None, gate=None, on_task_published=None, state_loader=None, state_saver=None, heartbeat=None, autonomy_provider=None, campaign=None, meta_loader=None)`。后八个全是鸭子回调、None=脚本/旧测试不接线（纯内存行为）：gate/on_task_published 是批 2 预算闸门；state_loader/saver 装载/落盘游标轮数，heartbeat 每 LLM 步续租 tick 租约；**autonomy_provider（批 4）实时返回归一化 autonomy dict，level=="L1" 时 spawn/cancel_task/requeue_task 转人工审批（M4 生命周期两工具同款三分流）**，本模块不 import core.autonomy；**campaign（2026-09-19 战役记忆全局库，§16.5）= tick 态势召回注入，None 不注入**；**meta_loader（对话化 M1/M2，2026-09-21）实时返回 project.json meta（goal/persona 段实时生效），None=不注入**。**批 6：`OrchestratorConfig.propose_only=True`（API 按实时档位 L0 单点注入）=L0 提案模式**，publish/spawn 只校验不写实体。

## 轨接线（§4.5.5）

- **任务类型注册表**：track 给定时 `self.task_types = load_task_types(...)`（含 generic）；LLM `publish_task` 传 `allowed_types=self.task_types`，未注册类型拒收并回填 `[拒绝]`，任务不入队。噪声缺省取注册表该类型默认值（如 assessment/exploit=low）。
- **enum 提示层**：`_orch_tools()` 返回工具表深拷贝，把本轨合法 task_type 作为 enum + 描述下发（让 LLM 一次填对；无 track 退回 ORCH_TOOLS 原表）。服务端拒收仍是最终护栏，勿删。
- **角色目录注入**：`role_catalog` = 专家池按轨过滤清单（M2 起 `list_experts` + `load_expert`，name + description + task_types），系统提示含「可开角色目录」「静态分诊」段；spawn_session 仍受 config.allowed_roles 白名单 + max_sessions 双约束。
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
- **C2 指挥编排器指令 + 判据模板（§6.9/§6.4）**：`_pending_directives/_overview` 注入「⚠ 人类指令（最高优先）」、`_finish_tick` 轮末落 orch.directive.done；`judgments.py resolve_criteria` **四层判据兜底（goal〔meta.phase_goal，2026-09-22 goal 统一后居首〕> mission 存量 >所选模板>mode 内置默认）**供 API 派生闸与提示共用；config/judgment_templates.json 用户模板。
- **C2 行动边界注入（§6.9；R2 2026-09-17 mode 退役→轨级；goal 统一 2026-09-22 目标职责移交 goal_section；M5 D2 共享化 2026-09-23）**：_stats 增 mission 视图（track/mission/redteam_roe，读项目 config）；ORCH_SYSTEM_PROMPT 增 {mission_section} 槽=**纯行动边界**（redteam ROE 四要素+缺 ROE 按 pentest 上限兜底提醒+redteam 加一行「⚠ 边界即红线：授权申请走 request_authorization，勿自行越界」；其余轨恒注入影响证明级上限提醒）——**边界行唯一出处=模块级 `mission_boundary_lines(track, config)`**（返回纯文本行不带 markdown 修饰；_mission_section 自己加标题与 `- ` 前缀；core/api approvals 出口同源调用，审批卡对照边界审授权申请，勿在别处复制第二份边界文案）——mission.text/criteria 注入行已退役（目标与判据由 {goal_section} 槽承担，判据纪律第 8 条口径不变）。
- **战役记忆召回（2026-09-19 借鉴 dsh，§16.5；experience-sedimentation M2 元信息行，2026-09-22；M6 F1 死路分组 2026-09-23）**：`_campaign_section()`——campaign 注入时按「goal 文本（meta.phase_goal，mission 存量回退）+ high_value 目标值 + 同目标负载键」做 query，`CampaignMemory.recall(track=, capability=项目首能力)` 取 top-5（热度 log1p 马太×30 天线性衰减，召回即计 usage），「既往战役打法（跨项目记忆，仅参考——贴合当前目标再采用）」段注入 `{campaign_section}` 槽（单条截 300 字）——**段头带元信息行「本项目 verified 发现 N 个（campaign 收 verified 产出/exploited 链打法与死路记账负知识）」**（零产出项目看到跨项目打法时知道本项目还什么都没验证过）；**注入侧按 tags 分两组：死路条目（tags 含 dead_end）单独成「⚠ 既往死路（勿重走；确需重走先确认前提已变化）」组且不带热标，防误当正面经验——recall 池不拆，组内照旧打分排序**；未注入/零命中/异常一律空段，**召回失败绝不阻断编排**。
- **态势注入增强（2026-09-17，§6.4 定稿块；uncovered 口径 2026-09-18 修订）**：`_assets_view` 返回 HVT 集合（meta.tags 含「高价值」）+ `_covered_ids`；**covered = open/claimed 任务文本命中 ∪ 资产 status ∈ visited/scanning/tested_clean/na（任务 done 后资产状态回流，uncovered 才能收敛；2026-09-18 前旧口径只看任务文本，与判据文案不符致永不收敛；2026-09-24 补 na——对齐 coverage.py 终态，人工裁定不测的资产不再虚增 uncovered）**，uncovered 排序 HVT 优先；`in_progress`（visited/scanning cap 20）与顶层 `done_count`（tested_clean，"挖完"口径）；`high_value` 段（HVT 资产带 status/covered/挂该资产发现 cap 3，未到 tested_clean 持续列出；**2026-09-24 扩自动推导**：承载 verified 且 severity≥high 发现的资产并入、`derived=true` 标明非 tag 来源，verified medium/unverified high 不推导）；`_recent_tasks_view`（recent_closed=done/failed 合并 cap 30 带 result_note[:100] + claimed_now 计划进度）；`_overview` 尾部注入最新 `project.digest` 全文（cap 2000，经 `bb.latest_digest`）。**内部键 `_hvt_ids/_covered_ids` 在 `_stats` 里 pop 后再返回**（勿漏，否则进 overview JSON）。
- **L1 委派开窗审批（会话中心化，2026-09-25）**：`publish_task` 的 L1 分流落 `op="delegate_window"` 审批（action 带 role/objective/task_type/scope/noise/conflict_keys/priority/refs；risk 按 passive=low 否则 medium），批准后 API `_exec_approved_delegate_window` 自动开窗+写委托+带活起跑；不发 session.spawned/delegation.posted（批准处理器补发）。**`spawn_session` 工具的 L1 分流仍走 op="spawn_session"**（仅纯对话/侦查辅助窗，不挂委托）。L2 维持直接开窗/复用窗，L0 先走 propose_only 分支。
- **生命周期收编两工具（M4，2026-09-22，orchestrator-efficiency）**：`cancel_task(task_id, reason 必填)` / `requeue_task(task_id)`——**三分流照 spawn_session 先例**（propose_only→_propose / L1→request_approval risk=low / L2→直执）；L2 cancel 后调 `_interrupt_window(claimed_by)`＝只 `sess.request_abort()` **只打断不关窗**（在跑窗 fail 撞 ClaimError 被吞=既有容错先例，窗留 live_sessions 可接新任务）；状态校验在三分流**之前**（failed 不可 cancel 提示改用 requeue、open 不可 requeue）——L0/L1 对不可取消任务也直接拒收不提案；requeue 复用 reopen 原语零新代码。事件 `task.cancelled` 由 TaskQueue 发（不复用 task.failed=异常唤醒触发器），decision 纪律第 9 条与 C3 timeout 提示（run_cmd timeout 参数）已进系统提示，改 prompt 勿回退。
- **publish_task 透传 `refs`（批 1B）**：任务以某发现为依据时（如「验证 find-x」）填 refs；TaskQueue 与正文正则 `find-[0-9a-f]{12}` 自动抽取合并为 context_refs，该发现被标 false-positive 时执行者收到强制自评通知（§6.7 的 1.6，撤回广播 orch 经下轮 tick 事件游标自然可见，本模块无特判代码）。
- **v14 任务绑角色与防碎发布闸（2026-09-18，DESIGN §6.4；M2 2026-09-21 切 experts 口径）**：`_tool_publish_task` 签名加 `role=""`——①**role 前置校验**（`expert_exists(packs_root, role, track)` 失败回 `[拒绝] 专家不在池内或不可服务本轨`，load_expert 会静默回退不可用）；②**dedup 预检**在单轮发布硬闸后、L0 提案分支前（`dedup_fp(task_type, scope, objective)` + `find_dedup_target` 命中回 `[复用] task_id` 不发；L0 提案 args 也先查重）——此前编排器是三写路径中唯一不查重的空白点；③publish 透传 `role=role, allowed_roles=list_experts(轨过滤全池)`（**app.py 内有同名端点函数，核心侧 import 用别名**）；④L0 提案 args 带 role。`_stats` 增 `targets`（`_target_load`：open+claimed 按 target_keys_of 聚合 top5，带 at_limit 标）注入态势；ORCH_SYSTEM_PROMPT 纪律 2 改 role 口径 + 注明同 target 阈值 `max_tasks_per_target`（=4）槽。
- **预算闸门（批 2，§6.8）**：每次 chat 后 `record_llm_usage(source="orchestrator")` 记账（失败只 log 不阻断）；publish/spawn 调 `self.gate(action)`——返回原因串则不写实体，`[拒绝] <原因>` 回填 LLM 改道（publish 闸门在噪声缺省判定**之前**）；publish 成功后 try/except 调 `on_task_published(task_id)`（**v0.71 签名带 task_id**：API 侧回调=tasks_published+1 + `_bind_task_window` 发布即建窗——建窗失败不回滚发布；计数失败不回滚任务）。gate 由 API tick 端点注入闭包，实时重读配置，本模块不 import core.autonomy。
- **会话中心化派单口径（2026-09-25，DESIGN §四）**：`publish_task` = **像人类一样开窗/复用窗 + 写委托**——先 `_pick_reusable_window`（armed+idle、role 匹配、干过同 task_type 终态委托者取新）决定 sid，无则 `session_factory` 开窗落 session.spawned(origin=orchestrator-delegate)，再 `tq.publish(target_session=sid)` + `delegation.posted`（payload.new_window）；窗空闲经 on_task_published 回调起跑、窗忙已排队。**`spawn_session` 仅用于纯侦查/纯对话辅助窗**（不挂委托）。旧「发布即自动建专属窗/执行审批单」口径已废，改 prompt 勿回退。

## 只读查询工具面（M2，2026-09-22，orchestrator-efficiency）

- **`ORCH_QUERY_TOOLS` 四工具**（tick 与 chat_turn 都消费——`_orch_tools()` 返回 `ORCH_TOOLS + ORCH_QUERY_TOOLS` 深拷贝，enum 注入对四工具同样生效；**零写权红线：dispatch 层无任何写实体路径**）：
  - `task_detail(task_id)`：按 id 拉任务全量（objective/scope/result_note **全文不截断**/plan/context（reconcile、attempts））——核对执行者结论的唯一可信通道；跨项目/不存在 `[错误]` 回填；
  - `bb_overview(section=all|assets|findings|events, asset_type?, asset_status?, limit=50)`：assets 默认复用 `_assets_view`（剔除 `_hvt_ids/_covered_ids` 内部键）；**给 asset_type/asset_status 时换成精简过滤清单**（枚举校验非法 `[错误]`，matched_total/shown/items，items 仅 id/type/value[:80]/status——资产多勿整表拉取）；findings=分级统计+verified 总数+最新 20 条全标题；events=尾部 50 条（payload 截 300）；
  - `budget_status`：usage_state_get + projects.config 的 autonomy 段——**token_budget/task_budget 原值保留 + `*_effective` 给确定语义**（null/缺省→unlimited 不限，正整数才输出 used/remaining/pct；2026-09-24 外显，此前裸抛 null 被误读为没接线），**只报余量数字不做闸门判定**（闸门仍是 API 注入的 gate 回调）；
  - `session_list`：live 窗清单（状态/角色/worker_armed/unread/最后活动时间——events 表 GROUP BY 一条 SQL 无 N+1）。
- **A2 态势截断放宽**：`_overview`/`_overview_for_chat` 事件行 payload `[:120]→[:300]`、`_recent_tasks_view` result_note `[:100]→[:300]`——有 task_detail 全文兜底后态势保持摘要定位（渐进披露）。
- 系统提示可用工具段已注明：四工具用于**核对与决策**（对执行者结论存疑先 task_detail 拉全文再判断），**不用于替代派单执行**。
- **B2 覆盖度对账并入态势（M3，2026-09-22）**：`_assets_view` 出口新增 `assets.coverage` 键 = core/coverage.py `coverage_report` 分组收敛摘要（groups_done/converged 比值 + 每组一行「domain:x 收敛 n/m · 未收口: …」未收口 cap 5）——tick 态势 JSON 与 bb_overview assets 分区同源受益；对账异常 log 后 coverage=None 不阻断态势注入。

## 对话插队轮 chat_turn（对话化编排器 M1-M3，2026-09-21）

- **入口** `chat_turn(text)`（API `POST /orchestrator/chat` 产 Job `orchestrator-chat` 调用）：人类消息由 API 层落 `orch.chat {role:"human", text[:2000]}`（author=human），回复落 `{role:"orch", text[:2000], tool_trace}`（author=orchestrator）——对话历史=events 表 orch.chat 单一来源，`_chat_history()` 取最近 40 条组装（human→user / orch→assistant，**开头连续 orch 跳过**保证首条是 user）。
- **循环语义与 tick 不同**：每步 heartbeat 续租 → `llm.chat(tools=ORCH_TOOLS 全闸门同源)` → `record_llm_usage(source="orchestrator-chat")` → **无 tool_calls 即 break（纯文本回复=回答完毕）**——勿回退成 tick 的「催促调工具 continue」（会空转烧轮）；有 tool_calls 逐个 dispatch 并记 tool_trace（{name,args[:200],result[:200]}）；`_finished(done)` break。步数上限 `CHAT_MAX_STEPS=8`。
- **系统提示** `CHAT_SYSTEM_PROMPT`（与 tick 的 ORCH_SYSTEM_PROMPT 分立）：对话轮人设 + 只读边界声明；注入 `_goal_section()`（M2 goal，tick 与对话轮同槽）与 `_persona_section()`（M3，**仅对话轮**）。`_overview_for_chat` 态势窗=固定最近 100 条事件（**不推进 event_cursor**）。
- **插队轮只读边界（全部只读，勿破坏）**：不推进 event_cursor、不计 cycles、不动 last_digest_cycle、不消费 C2 指令（不标 directive.done）、不发饿死告警、不写 state_saver；发布/开窗动作本身照常写实体（与 tick 全闸门同源：dedup/gate/L0 提案/L1 审批）。
- **M2 goal**：`_goal_section()` 读 meta_loader 的 `phase_goal`（text/criteria/phase）；meta_loader None=不注入（纯内存测试路径）。**M3 拟人**：`_persona_section()` 读 `orchestrator_persona`（persona[:600] 注入「## 你的身份」；display_name 不注入提示、由前端贯穿）。
- **M4 异常订阅唤醒（2026-09-22）**：类常量 `WAKE_TRIGGERS`（白名单 kind→冷却秒：task.failed/task.starvation/phase.gate_open=600、budget.soft_warning=3600）+ `WAKE_LOOKBACK=1800s`（无锚点首启回看窗）+ `WAKE_MAX_EVENTS`/`WAKE_CHAT_WINDOW`；`collect_wake_triggers(bb, pid, *, now=None)` 纯函数——锚点=最近 proactive orch.chat 事件（triggers 字段 kind 级 id 防重 + created_at 冷却双维度），starvation 摘 warnings.reason+objective、failed/budget 摘 note、gate_open 摘 summary；`wake_brief_text(triggers)` 合成「〔主动唤醒〕…」user 消息（只进 LLM messages **不落 orch.chat 历史**——下轮「开头连续 assistant 跳过」规则会连带丢弃唤醒回复，属定稿口径）；`chat_turn(text, *, wake=None)` 唯一差异=回复 payload 加 `proactive:true, triggers:[kind]`（前端 🔔 徽章 + 下次锚点双用途）。触发接线在 API 层 `_post_tick`（见 api/CLAUDE.md），本模块零新表零调度。

## 分阶段工作流接线（pentest M1+M2，2026-09-22）

- **`_phase_section()`**：meta_loader 提供的项目 meta 有阶段剧本时（`load_track_phases(packs_root, track, config)` 非空）拼「## 阶段工作流」段注入 `{phase_section}` 槽（tick 与对话轮同槽）——当前阶段（id+中文名）/阶段目标/重心配额建议（focus 权重）/出口门进度；book 空/异常=空段。
- **门进度只读不重算（M3 B3 单一事实源，2026-09-22）**：出口门行读 meta 的 `phase_gate_state`（API 层 `_phase_gate_check` 每轮评估落盘、enter_phase 流转顺写新阶段）——已过门=等待流转，未过=列状态里的 unmet+「exploit 类任务会被拒收」，**无状态/阶段不符=「待编排校准」**（存量项目首轮）；**勿回退成现算**——注入在 tick 开始、判定在 tick 末，现算会用上轮 idle 制造矛盾窗口；派单撞门的实时拒绝仍由 `_phase_gate_reject` 现算（动作侧确定性不变）。
- **`_phase_gate_reject(task_type)`**：publish_task 前置拦截（dedup 预检后）——当前阶段 `gate_types` 含该类型且门未过（`phases.gate_block_reason`，纯确定性含空闲逃生）→ `[拒绝] 入场门…` 回填 LLM；**异常一律放行**（门故障不瘫痪派单）。人工流转阶段是放行阀。
- `state.py` v21 新列 **`derive_idle_rounds`**（_STATE_DEFAULTS/_EDITABLE_FIELDS）：派单空闲轮数，API 层 `_phase_post_tick` 维护（本模块只读）。

## 约定

- `session_factory(role) -> AgentSession` 由调用方注入（API/脚本），轨与能力包在工厂内闭包绑定；本模块对 Agent 只鸭子依赖 `.session["id"]`。API 注入的是**注册式工厂**（返回即进 app.state.agents，编排开窗不再是孤儿窗）。
- 人类随时可直接向 TaskQueue 插手（脚本/API 也可以不经过注册表直插，UI 走 API 校验）。
- 参照实现：scripts/demo_orchestrator.py（ctf）、scripts/demo_pentest.py（assessment，approval 自批标 demo-script(auto)）。
- 测试：tests/test_orchestrator.py（拒收/噪声缺省/饿死去重/角色目录注入/结构化结果/跨实例游标轮数/心跳/**L1 转审批：不建窗/必须 reason/gate 预检/L2 仍直接开**，`make_orch(..., autonomy_provider=)`；**L0 提案：不发任务不建窗/非法类型与缺 conflict_keys 仍拒/一轮多提案+digest 照常/L0 notice 注入**，`config=OrchestratorConfig(propose_only=True)`；**M4 生命周期：L2 直执 tick 全链路+打断/L0 提案/L1 审批卡**）+ tests/test_orchestrator_state.py（租约获取/释放/过期抢占/owner 误释放/白名单/并发只赢一个）。
