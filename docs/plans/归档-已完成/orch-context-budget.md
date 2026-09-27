# 编排器上下文预算（orch-context-budget）

> 编排器每轮态势里全量段随项目线性膨胀（本项目实测：findings 68 条全进、
> sessions 61 条全进且 60 条已 closed、事件窗被纯观测事件占坑）——把它压成
> 常数级，同时压低单次 LLM 请求的最坏耗时（2026-09-27 编排 tick 读超时事故）。

- **状态**：已实施（2026-09-27 当日）
- **拍板记录**：
  | # | 决策点 | 结论 |
  |---|--------|------|
  | D1 | 裁剪范围 | findings 分级 cap 20 + closed 会话出清 + 事件窗剔除纯观测 kind；**不做事件表归档/分表**（SQLite 查询非瓶颈，动表破坏 traces/tasktree 回放语义） |
  | D2 | 被裁信息去向 | 编排器按需走现成 `bb_overview`/`task_detail` 工具拉全文——态势里只放计数行 |
- **关联代码**：`core/orchestrator/orchestrator.py`（`_stats`/`_overview`/`_overview_for_chat`）、`core/blackboard/store.py`（`recent_events` 加 `exclude_kinds`）

## §1 三项改动

1. **closed 会话出清**：`_stats.sessions` 只进非 closed，加 `sessions_closed` 计数。
2. **findings 分级 cap**：`_stats.findings` 只进 top 20（verified/exploited 优先 →
   severity 降序），加 `findings_total`/`findings_truncated` 字段。
3. **事件窗 kind 剔除**：`recent_events` 加 `exclude_kinds` 参数（与既有三分支/
   kinds 正交）；编排器 tick 与 chat 两处事件窗剔除 `llm.usage`/`llm.thinking.delta`
   （纯观测零信息量，该项目占事件总量 25%+），游标照推不重放。

## §0 实施修正

零偏差：三项改动均按 §1 落地，新增测试 2 个（test_orchestrator 65→67——`test_stats_findings_sessions_context_budget` 验证 findings top-20 裁剪 + closed 会话出清计数、`test_overview_event_window_excludes_observation_kinds` 验证事件窗剔除且游标照推）。实现注记：top-20 排序键复用同函数上游的 `sev_rank`；`recent_events` 的 `exclude_kinds` 与既有三分支/kinds 完全正交。
