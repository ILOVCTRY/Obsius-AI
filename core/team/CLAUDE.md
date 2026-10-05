# core/team/

Team/Member/Run 是独立于旧 tasks 与 coordination DAG 的直接执行域。

- `store.py`：Team、成员、Run、Run 成员和 execution audit 的唯一写入口。
  **团队生命周期事件（2026-10-06）**：`_emit` 在**事务外**发 `team.created` / `team.updated` /
  `team.run.started` / `team.member.updated` / `team.run.finished`（`append_event` 自带事务，
  `_tx` 不可重入，**勿在 `_tx` 内调用**）；payload 带 `team_id`/`team_name`/`run_id`/`member_key`/
  `status`/`member_count` 等，事件失败只 log 不阻断写路径。消费方=直播间「指挥」页签团队卡。
- 创建 Team 只登记 roster，不创建旧 task 或 session。
- Start 必须经过 preflight revision 与人工 confirmations，再事务创建 Run 快照。
- 后续 fan-out 创建专属 Agent session；不得调用旧 TaskQueue 的 claim、take_session_next 或 start_direct。
- 运行安全边界仍由现有 role、runtime policy、ROE、approval、gateway、预算和 session cap 提供。
