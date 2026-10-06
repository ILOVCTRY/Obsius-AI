# core/team/

Team/Member/Run 是独立于旧 tasks 与 coordination DAG 的直接执行域（**旧任务机制已退役，2026-10-06**）。

- `store.py`：Team、成员、Run、Run 成员和 execution audit 的唯一写入口（`TeamStore`）。
  - **团队生命周期事件（2026-10-06）**：`_emit` 在**事务外**发 `team.created` / `team.updated` /
    `team.run.started` / `team.member.updated` / `team.run.finished`（`append_event` 自带事务，
    `_tx` 不可重入，**勿在 `_tx` 内调用**）；payload 带 `team_id`/`team_name`/`run_id`/`member_key`/
    `status`/`member_count` 等，事件失败只 log 不阻断写路径。消费方=直播间「指挥」页签团队卡。
  - **创建 Team 只登记 roster**（`create_team`，status=draft），不创建旧 task 或 session。
  - **Start 必须经 preflight 与人工确认**：`preflight` 按 Team/成员/项目安全配置生成 `revision`
    （含 sessions_cap 容量核算与 blockers）；`create_run_and_members` 要求 `revision` 一致 +
    四项确认（members/goal/safety/execution）全 true，再事务创建 Run 快照与逐成员
    `execution_audits`（`source=team_direct`）。revision 不符抛 RuntimeError（乐观锁），有 blockers 抛 ValueError。
  - **fan-out**：Run 创建后由 API `_fanout_team` 逐成员创建专属 Agent session（`attach_session`
    绑 session_id），并经 `_submit_worker` Team 分支跑 `AgentSession.run_team_execution(ExecutionContext)`；
    `mark_member`/`reconcile_run` 收敛成员与 Run 终态（completed/partial_failed/failed/cancelled）。
    **不得调用旧 TaskQueue 的 claim、take_session_next 或 start_direct**。
  - `list_runs` 列历史 Run（新→旧，前端运行报告取最新）；`cancel_run` 置 cancelling 并回填未启动
    成员，API 对 running 成员 `request_abort`。
- **execution_audits**：执行履历（objective/role/status/**outcome**），会话复盘
  `POST /api/sessions/{sid}/review` 的 runs_view 读它。
- 运行安全边界仍由现有 role、runtime policy、ROE、approval、gateway、预算和 session cap 提供。
