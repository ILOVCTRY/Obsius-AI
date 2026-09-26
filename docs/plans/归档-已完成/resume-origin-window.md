# C6 续跑原窗就近接手：transcript 模式放宽原窗复活（LiveRoom 失败窗续跑归位）

## 状态

**已实施**（2026-09-23 同日落地，实战痛点修复批次②）：`reusable` 去 `mode=="snapshot"` 前置，分支内按 mode 分流（snapshot=现状 revive 断点复活 / transcript=reopen+清延续闸门+worker 首轮认领接手，零搬运）；返回体 `resume_mode` 如实带 mode。tests/test_task_resume.py 新增两用例（`test_resume_transcript_takes_over_origin_window` 原窗接手端到端〔session_id==原 sid、会话数不增、任务 done〕+ `test_resume_transcript_closed_window_still_spawns_new` 原窗已关新窗防回归），9 passed。tooltip 文案更新（原窗存活即本窗接手）；core/api/CLAUDE.md 端点表与 webui/CLAUDE.md 两处同步。定稿决策沉淀回写 DESIGN.md 见 §6.1 v0.72 修订块的后续维护（本方案为端点行为修订，主记录在 core/api/CLAUDE.md 端点表）。

## 问题与拍板记录

用户实测（中原工学院项目）：失败任务窗（延续模式提示条）点「↩ 接手现场续跑」，任务**飘去新建任务窗**执行，当前窗空挂失败态不关闭——预期是本窗原地续跑。

| 决策点 | 结论 |
|----|----|
| 根源 | `resume_task` 的 `reusable` 只认 `mode=="snapshot"`，transcript 恒走跨会话新窗（app.py:3355）；该端点原为 TaskBoard 设计（无「当前窗」概念），09-22 LiveRoom 按钮复用后语义错位（按钮注释却写「原窗复活」） |
| 方向 | **A：原窗就近接手**——`reusable` 去掉 mode 前置，放宽为「原窗存活即原窗接手」；B（维持恒新窗+旧窗自动关）否决：关窗副作用多、60 条截断丢现场、窗切换跳动 |
| 定稿依据 | reopen 的 v0.71 定稿（tasks.py:1075）早已写明「保留 target_session——失败任务归原绑定窗，原窗重跑接手履历最完整」；本方案把 resume 端点对齐该既有语义，v0.72 一窗一任务下绑定失败窗本就是续跑主场 |
| 附带红利 | 「旧窗不会自动关闭」问题自然消失（任务回本窗跑，无空挂）；TaskBoard 入口同样受益（原窗活着回原窗，用户去原窗看） |

## 定稿设计

`POST /api/tasks/{task_id}/resume`（app.py:3337）三级瀑布：

1. **原窗存活**（`sid = task["claimed_by"]` 且 `orig_row` 存在且 `status != "closed"`）：
   - **snapshot 模式** → 现状 revive 路径保持（revive_snapshot → reopen+claim → budget 扩展 → 清闸门 → running → submit worker）——断点 next_step/max_steps 有价值，不动。
   - **transcript 模式** → **新增原窗接手分支**：`_ensure_agent` + `_session_job_running` 防重入检查（409）→ `tq.reopen(task_id, by="human", scene="kept")`（**不 claim**，认留给 worker 首轮——与跨会话新窗分支同时点）→ 清延续模式闸门（`_stop_after_task=False` / `_pause_req.clear()` / `_abort_req.clear()`，与 snapshot 复活路径同款）→ `set_session_status(sid, "running")` → `_submit_worker(origin="task-resume")` → 返回 `{"session_id": sid, "resume_mode": "transcript"}`。
   - worker 首轮 `run_next_task(only_task=bound)` → `claim_next` 拿到 reopen 后的 open 任务（target_session=reopen v0.71 保留语义，仍指本窗）→ `run_task` 接手路径：从任务现场文件重建末 60 条 + handover 提示 + 注入链，与跨窗接手口径完全一致。
2. **原窗已关/不存在** → 现状跨会话新 armed 任务窗（`_spawn_session_for_task`），TaskBoard 场景与原窗丢失场景共用，不动。

关键不变式：

- **无搬运**：C10 任务现场文件（task-<tid>.json 每步落盘 + 延续聊天 `_append_chat_to_transcript` 回写）归任务所有，原窗接手时上下文重建口径与跨窗一致，UI 消息流零搬家、视觉连续。
- **调度器不抢**：reopen 后窗口期任务 open+target_session 指存活原窗，调度器只对「绑定指向 closed 窗」重绑，不会中途截胡。
- **预算不加**：transcript 接手无快照预算语义，与跨会话新窗分支一致（max_steps 用 agent 当前配置）。
- **跑完回归延续模式**：任务终态后 worker 退、窗留续聊——bound_task 全程不变，一窗一任务自洽。

## 改动面

| 文件 | 改动 |
|----|----|
| `core/api/app.py` resume_task | `reusable` 去 `mode=="snapshot"` 前置；分支内按 mode 分流（snapshot=现状 revive / transcript=新原窗接手段）；返回体 resume_mode 如实带 mode |
| `webui/src/views/LiveRoom.tsx:1694` | tooltip：「无任务键快照时将在新任务窗执行」→「原窗存活即本窗接手，仅原窗已关时才在新任务窗执行」；toast 分支（1387-1389「本窗恢复执行」）已存在零改 |
| `webui/src/views/TaskBoard.tsx:621` | 不动（「重新认领」表述与新语义不冲突） |
| `tests/test_task_resume.py` | 新增两用例（见测试节）；现有 `test_resume_cross_session_spawns_armed_window` 为「原窗已关」场景，新语义下保持成立，防回归 |

## 测试

1. **transcript + 原窗存活 → 原窗接手**（端到端，TestClient）：发布任务（v0.72 发布即建待命窗，原窗即绑定窗）→ 窗内跑至 failed（awaiting_human fail，无快照=transcript 模式，先清掉/不落任务键快照）→ 会话保持非 closed → `POST resume` → 断言 200、`body["session_id"] == 原 sid`、项目会话数不增 → 轮询任务至 done → 快照路径全程不存在。
2. **transcript + 原窗已关 → 新 armed 窗**：同 1 但 `close_session(原窗)` → 断言返回新 sid、新窗完成（现状行为防回归）。
3. **保持**：snapshot + 原窗存活 → revive（现有 agent 层用例覆盖认领即复活；API 层 182 用例覆盖原窗已关）。
4. 全量回归：`PYTHONIOENCODING=utf-8 /e/Miniconda3/python.exe -m pytest tests -q`（基线 974）。

## 验收（用户实操）

中原工学院项目失败窗点「↩ 接手现场续跑」→ toast「任务已续跑，本窗恢复执行」、本窗失败提示条变执行态原地起跑、无新窗出现；会话窗对话历史完整保留。

## 范围外（记打磨账）

- **handover 提示语原窗语境**：原窗接手仍注入「🔁 第 N 次尝试接手：以上对话现场来自此前执行…」——在本窗语境略突兀（历史消息明明都在 UI 上）。统一提示不做 agent 层传参侵入，后续打磨再评估。
- **snapshot revive 丢延续聊天**：快照可能落后窗内现场（失败后人类在延续窗聊过天，revive 整体还原回断点）——既有 C6 语义，低频，另行评估。
- E12 会话键恢复（/sessions/{sid}/resume）不经本端点，互斥不动。
