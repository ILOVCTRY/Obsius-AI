# core/agent/

> Agent 会话主循环与工具分发（DESIGN.md §3）。所有写操作走黑板 API / 执行网关，本目录不直写存储、不裸执行命令。

## 文件

- `loop.py` — `AgentSession(*, project_id, bb, gateway, llm, planner_llm?, packs_root="packs", track="ctf", capabilities=None, role="_generalist", session_name?, capability_prompt?, config?, decompiler?, artifacts_dir?)`。
  - 系统提示 = `build_rules_preamble`（轨+能力包红线、owner 规则、role-rules）+ 能力清单 + 角色人设 + 技能路由正文（候选集 = caps ∪ track，再经角色白名单）+ 任务目标 + 软边界行 + 工具纪律。
  - `run_task(objective, task_id?)`（open 任务自动认领）/ `run_next_task()`（Worker 入口）；卡死 N 步召唤 planner；`_trim` 裁剪旧工具输出；`_finalize` 未收尾任务自动 fail——**E8 起步数耗尽不走此路**（`_budget_pause` 自动暂停，见下），自动 fail 仅保留给显式结束场景。
  - **`last_claim_idle`（批 5，§6.8）**：最后一次 `claim_next` 返回 None（队列空退出）才为 True；每次认领前置 False，暂停/中断退出不残留。API 层只在 True 时触发 L2 续 tick（触发点 A）。
  - 会话控制：`request_pause()`（步边界软暂停，快照可恢复）/ `request_abort()`（硬中断）。
  - **E12 中断保留现场**：`_abort_current_task` 人工中断**不再清落盘快照**（`task.failed` 带 `resumable:true`；仅任务已删除触发的中断仍清，防孤儿）；新方法 `revive_snapshot(task_id)` 刷新会话行取最新指针、载回 `_resume_state`（限原会话，校验 task_id 匹配）；`_load_persisted_snapshot` 不再自带 `paused=True`（移至 `__init__` rehydrate 处）。API `POST /tasks/{tid}/resume` = 校验 failed+claimed_by → revive → reopen+claim → **清 `_stop_after_task`**（不清则 worker 领任务前即退）→ submit worker；budget 快照缺省 +200。close 会话对 agent 显式 `_clear_snapshot()`（closed 不可 rehydrate，快照必成孤儿）。
  - **rehydrate**：构造传 `existing_session=<sessions 行>` 则附着旧会话不新建行（服务重启/孤儿窗恢复；角色 yaml 按当盘重载，claimed 任务靠 30min 租约回 open）；E8 起暂停快照持久化到 workspace 文件，`_load_persisted_snapshot()` 载回即回 paused 可续跑。
  - **E8 步数预算与人工引导（§3，2026-09-16）**：①`_loop` 是 **while** 循环、每轮重读 `dispatcher.max_steps`（request_steps 当场增补后上界前移，range 预计算会截断自救）；剩余 <20 步边界注入预算提醒。②步数耗尽 → `_budget_pause`：快照（reason="budget"，next_step=耗尽步号）+ claimed/心跳保留 + `session.budget_paused` 事件，**不 fail**；恢复不增补预算时仍有一轮对话可 request_steps 自救。③暂停快照四方法 `_snapshot_path/_persist_snapshot/_load_persisted_snapshot/_clear_snapshot`：路径 `<workspace>/<pid>/snapshots/<sid>.json`（无 artifacts_dir 降级纯内存），`sessions.meta` 存 `resume_snapshot` 指针，续跑/中断消费后清文件+指针。④私信第三类分流 `_human_note_notice`（human_note →「💬 人类引导：…」），认领/步边界/恢复同注入。
  - **租约心跳（A1）**：认领后起守护线程 `_LeaseHeartbeat`，每 `lease_renew_seconds`（默认 600s）调 `renew_lease`，防长任务超 30 分钟 TTL 被回收双跑；complete/fail/abort/_finalize 必停，pause 期间保留（任务仍占有）；续租 ClaimError（任务易主/被删）心跳自退，其他异常下一周期重试。
  - **步边界任务存活检查（A1 急停）**：`_control_point` 在 abort/pause 检查后调 `_task_gone()`——current_task_id 非空且 `tq.get_task()` 为 None（任务被四态物理删除）→ `_abort_current_task()`：行已删则跳过 tq.fail（task.deleted 即审计），停心跳/idle/`session.aborted` 照常；当前步所有工具返回后才生效，单步工具调用不可打断。快照恢复遇删任务同路径（丢快照认领新任务）。
  - **私信两类分流（A4）**：drain 后按 kind 分 notice——basis_stale 走 `_basis_stale_notice`（⚠ 三选一，强制），finding_update 走 `_finding_update_notice`（ℹ「你任务引用的发现有增补」，信息式不强制动作）；认领首消息注入与步边界注入同一分流，暂停期间不 drain。
  - **skill.routed 事件（C5）**：`skill_context_for` 每次路由落一条——命中带 name/pack/score/matched/breakdown，未命中 name=null、score=0；query 截断 200，task_id 贯穿（run_task→run_next_task）。
  - **依据撤回强制自评（批 1B，§6.7 的 1.6）**：认领 open 任务后重读行，stale_refs 非空且该任务未告警（`_stale_alerted` 防暂停续跑重复）时，首条 user 消息前置「⚠ 依据撤回」三选一（带理由继续/fail_task/改道），同时 `inbox_drain` 捎带该会话私信；进行中撤回由 `_control_point` 在**步边界** drain inbox 注入（不打断当前工具调用）；消息拼装在 `_basis_stale_notice`（finding 现场补水，只采 status=false-positive）。任务 done 时 stale_refs 非空由 tasks._finish 发 `task.basis_stale_done`。
  - **用量记账（批 2，§6.8）**：executor chat 与 advisor planner chat 成功后由 `_record_usage(resp, source=agent/planner, llm_obj=)` 调 `record_llm_usage`（session_id=本会话、model 取 llm.model），异常只 log「用量记账失败」不阻断主循环；全 0 usage 在 core.autonomy 内跳过。
- `tools.py` — `AGENT_TOOLS`（Anthropic schema）+ `ToolDispatcher`：run_cmd / **bb_add_asset（E6：走 register_asset 统一入口——type 省略自动识别（url/IP/host:port/域名/64hex），domain 自动 DNS 挂载，重报合并；命中既有资产/同 IP 回执追加「先 bb_query 查重」防重扫提示）** / **bb_asset_status（E7：visited→scanning→tested_clean 流转；tested_clean 必带 note 服务端强制；asset.status_changed 审计）** / bb_add_finding（E0：顶层入参 `relates_to:[{finding_id,note}]` 并入 evidence；悬空/跨项目由 store 抛 ValueError → `[工具异常]` 回填改道）/ bb_add_artifact / bb_query（E7 起 assets 支持.type/status 过滤、行带 status）/ **kb_open** / bb_upsert_func / **propose_pack_edit**（C4：唯一经验沉淀口，只落 pending 提案，绝不直写文件；每会话上限 3 条，超限/非法回填 `[拒绝]`）/ decompile（func_kb 命中即返缓存）/ list_symbols / **task_plan、task_step（A2）** / **publish_task（A5 子代理分解）** / **request_steps（E8：自助加步一次固定 +200，剩余 >20 拒收防囤步，落 step.budget_extended；max_steps≤0 未装配拒收；入 `_CONTROL_TOOLS` 恒放行）** / complete_task / fail_task / finish。
  - `AgentConfig(max_steps=200, context_char_budget, stuck_after, task_types, owner_tags, max_noise, allowed_tools, max_runtime, lease_minutes=30, lease_renew_seconds=600)`；构造器另收 `allowed_task_types`（轨 task_types.yaml 注册表护栏，None=未接线不校验）；dispatcher 构造传 `max_steps=self.config.max_steps`（E8，`step` property 供耗尽断点）。
  - **计划闸（A2）**：current_task_id 非空 + 任务 plan 为空时，除 `_PLAN_PRE_ALLOWED`（task_plan/task_step/bb_query/kb_open/list_symbols/decompile）与控制原语（complete_task/fail_task/finish/request_steps 恒放行）外，实质工具一律回填「请先调 task_plan 写下解决计划」；每调度现查行（跨暂停/新 job 不丢）。
  - **publish_task（A5）**：子代理分解任务的唯一口；`parent_id=current_task_id`、`created_by=session_id` 由服务端钉死（LLM 入参不收），type 走轨注册表护栏，非 passive 必填 conflict_keys；属实质工具（「分解是计划的一部分」，须先过计划闸）。回填明示「不要自己抢领，继续推进当前任务」。

## 角色软边界（§6.6，违例回填 `[越界拒绝]`，循环不中断）

- **tools 白名单**：不在 `allowed_tools` 的工具分发前拒绝；complete_task/fail_task/finish 永远放行。
- **max_runtime 是运行时等级软上限，不是秒**：`RUNTIME_RANK host<wsl<docker<sandbox`，超等级拒绝（`timeout` 只是普通透传参数）。只能比网关 threat_class 允许集更严。
- **default_noise → max_noise**：`claim_next` SQL 按噪声等级过滤，高于角色预算的任务不认领。
- task_types 为认领过滤器；越界审批通道（escalation）为阶段 4，现在无放松口子。

## kb_open（多源）

- 数据源 = 项目启用能力包各自的 `kb_sources.json`（`{sources:[{id,root(包内相对),recursive}]}`；现网均为单源 id=`<cap>-kb`、root=`kb`）；module 为源内相对路径且**带快照名前缀**（如 `ctf-web/auth-jwt.md`、`src-strike/知识库/idor-test.md`），resolve 后强制落在 root 内（穿越返回 `[拒绝]`，且不落 kb.open 事件）；不存在时回可选清单（前 50 个，防幻觉）。同名消歧的显式 source 参数未做，靠前缀隔离。
- kb/ 已从"只读文物"翻为**可增改的本地基线**：人类经 Skill 页/API 直改（备份/版本/改名联动在 writing.py+refs.py）；**Agent 只能经 propose_pack_edit 提 pending 提案，等人批准，不直写**。英文上游快照原文不翻译、不覆盖——新经验写**新 md**（系统提示第 5 条纪律：仅在文档矛盾/缺失/任务验证有效时提案，reason 附任务证据，每会话≤3）。

## 坑

- 工具异常一律回填文本，不抛断循环；`GatewayDenied` → `[网关拒绝]` 让 Agent 改道。
- bb_add_asset 重复 type+value 走合并；补挂/改 meta 必须用 update_asset_meta/find_asset/set_asset_parent。
- 测试：tests/test_agent.py（含白名单/等级/噪声三软边界 + kb 多源/防穿越）；提案工具与 skill.routed 见 tests/test_kb_proposals.py。
