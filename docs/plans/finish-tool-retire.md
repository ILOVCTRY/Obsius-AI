# finish 工具退役

- **状态**：讨论收敛（2026-09-24 当日拍板当日定稿），待用户排期。
- **拍板记录**：

  | 决策点 | 结论 |
  |---|---|
  | finish 工具去留 | **直接退役**（不做「保留加闸」）——「结束会话」是调度器/人类动作，不该是 AI 工具 |
  | 任务轮正常退出信号 | `complete_task` 成功 → 退出；`fail_task(error)` 成功 → 退出（取代 finish） |
  | 触发背景 | 2026-09-24 真实事故：Agent 完成任务后误调 finish（任务动词「收口」与 finish 语义撞车），业务成功却被 _finalize 兜底标 failed/aborted，且打法不进沉淀 |
  | `_finalize` 兜底 fail | 保留作最后防线（异常路径等），正常路径不再触发 |
  | 自由窗收工 | run_chat 纯文本回复即终止；无任务时本就无任务轮 |

- **关联代码**：`core/agent/tools.py`（AGENT_TOOLS / _CONTROL_TOOLS / ToolDispatcher.finished / _tool_finish / _finish_or_report_deleted）、`core/agent/loop.py`（CHAT_TOOLS / _loop_body 退出检测 / 纯文本停等文案 / 注释）、`tests/test_agent.py`（约 50 处 finish 剧本）、`webui/src/lib/events.tsx`、`webui/src/views/SettingsView.tsx`、`scripts/demo_agent.py`。

## 1. 为什么可以退役

finish 服务于 v0.71 前的长命 worker 模型（一会话连续认领多任务，需要 AI 主动关停自己）。v0.71/v0.72「一任务一窗口」后：绑定窗 complete_task 后回到 `run_next_task → claim_next(only_task=) → None → worker 退出`，finish 全链路冗余。CHAT_TOOLS 早在 2026-09-19 已剔除 finish，任务轮工具面属模型迁移漏网。

## 2. 后端改动

### 2.1 tools.py

1. AGENT_TOOLS 删除 finish 条目（现 :712-720）。
2. `_CONTROL_TOOLS` 删 `"finish"`（:32）→ `{"complete_task","fail_task","request_steps"}`。
3. 删除 `_tool_finish`（:2156-2160）；dispatch 经 `getattr(self, f"_tool_{name}")`（:1042）动态解析，无分发表要清。
4. 删除 `self.finished` 状态（:965）。
5. `_finish_or_report_deleted`（:1801）成功收尾（complete/fail）与「任务被人类删除」两分支已都清 `current_task_id`——**新增存 `self.last_close_note`**：complete/fail 存本次 result_note，删除分支存删除提示语；作为 _loop_body 退出时的返回摘要。

### 2.2 loop.py

**核心：退出检测从「finished 标志」改为「任务收尾导致 current_task_id 变空」。**

1. `_loop_body` 每轮工具执行前记 `had_task = self.dispatcher.current_task_id`（并行批整体一个观察点，放在 resp.tool_calls 分支前）。
2. 现 :1617 `if not self.dispatcher.finished: _maybe_summarize(...)` → `if self.dispatcher.current_task_id: self._maybe_summarize(...)`（收尾后不压摘要）。
3. 现 :1645-1646 `if self.dispatcher.finished: return summary` →
   ```python
   if had_task and not self.dispatcher.current_task_id:
       return self.dispatcher.last_close_note or "任务已收尾"
   ```
   覆盖三种收尾：complete_task（done）、fail_task(error)、任务被人类物理删除。
4. awaiting_human 路径不冲突：`fail_task(awaiting_human)` 只置标志不清 current_task_id，由现有 C1 分支（:1620-1644）先快照后 fail 并 return None。
5. 删除 :1513 `self.dispatcher.finished = False` 复位行。
6. 纯文本停等文案（:1597-1599）改为：
   `（请继续执行：调用工具干活；任务完成请调 complete_task 提交结果，无法继续请调 fail_task 说明原因）`
7. CHAT_TOOLS（:54-56）：finish 已不在 AGENT_TOOLS，过滤失去意义 → `CHAT_TOOLS = AGENT_TOOLS`（保留常量名，run_chat 引用不动；注释更新）。
8. 注释同步：:5「直至 finish」、:1064-1065、:1511-1512、:1624（「不走 finished/_finalize」）。

### 2.3 退出后链路（无需改，确认自洽）

_loop_body 返回摘要 → run_task `_finalize()`：current_task_id 已空 → 不再兜底 fail；照常落 session.finished 事件 + status=idle。worker 回 run_next_task：绑定窗 claim 恒空 → 退出；不进公共池。

## 3. 前端 / 脚本

- `webui/src/lib/events.tsx`：删除 TOOL_LABELS `finish: "🏁 收尾会话"`（:202）与 TOOL_SUMMARIZERS `finish:`（:270）死代码；`session.finished` 事件样式（:142）**保留**——那是 _finalize 事件不是工具。
- `webui/src/views/SettingsView.tsx:404` 白名单提示文案：「complete/fail/finish 永远放行」→「complete/fail 永远放行」。
- 工具目录（GET /api/agent-tools）静态全集自动少一项，AgentToolsPane 零改动。
- `scripts/demo_agent.py:50` 提示文案「最后 finish 总结」→「最后 complete_task 提交结果」；`scripts/CLAUDE.md:11` 同步。

## 4. 测试迁移（tests/test_agent.py）

约 50 处剧本调用，机械替换 + 两类专项处理：

1. **机械替换**：`ScriptedLLM.tool_call(x, "finish", {"summary": ...})` → `"complete_task", {"result_note": ...}`。
2. **D6 收尾闸隔离**：complete_task 首次过 `_closing_gate`，通用用例不接受两轮收尾。策略：测试用 AgentConfig 构造统一传 `closing_max_rounds=0`（cap_zero 首次申报即放行，与测试隔离惯例一致）；D6 专门用例保持缺省自行装配。实施时按全库 AgentConfig( 出现点统一处理。
3. **专项用例**：
   - `test_finish_with_open_task_auto_fails`（:186）、:313-319 自动 fail 场景：经工具已不可触发，删除原用例，改为**直调 `_finalize`（人为置 current_task_id）的单测**保留兜底覆盖。
   - `test_agent_heartbeat_renews_during_long_task_and_finish`（:1601）：改名 `..._and_complete`，finish→complete，心跳停/任务 done 断言不变。
   - 模块 docstring（:3-4）「会话收尾安全（未收尾任务自动 fail）」表述保留（兜底仍在）。
4. 断言 `session.finished` 事件的用例（:153 等）语义不变（complete → _finalize 照落）。

## 5. 不做项

- 不做 finish「保留加闸」中间态（用户已拍板直接退役）。
- 不动 `session.finished` 事件、StatusDot finished 会话态、LiveRoom 相关判断——均为会话生命周期语义。
- 不动 DB schema。

## 6. 实施步骤（排期后）

1. 后端：tools.py 五项 + loop.py 八项。
2. 前端 events.tsx/SettingsView 文案 + scripts。
3. 测试迁移并全量回归（基线 1030；用例数基本持平：删 2 增 1）。
4. `npm run build` 零 TS 错误。
5. 文档：DESIGN.md（§3 Agent 工具链：退出语义定稿、finish 退役注记）、core/agent/CLAUDE.md（工具清单/白名单/计划闸行）、webui/CLAUDE.md（工具相关描述若有）、scripts/CLAUDE.md；归档本文件。

## 7. 验证

- `PYTHONIOENCODING=utf-8 /e/Miniconda3/python.exe -m pytest tests -q` 全绿。
- `cd webui && npm run build` 零 TS 错误。
- 手工冒烟：任务正常 complete → 会话 idle、session.finished 照落；fail_task(error) → 任务 failed、会话 idle；看板无「未收尾自动 fail」误标。
- 不 commit（固定模式）。
