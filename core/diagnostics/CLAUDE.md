# core/diagnostics — 诊断（会话审计 + 失败日志）

`failog.py` — 工具 / MCP 调用失败日志（diagnostics-log，2026-10-14）。

## 职责

把两类失败的记录收敛成**独立 blackboard 事件**，给诊断页（webui
`DiagnosticsView.tsx`）提供查询面：

- `tool.failed`：本地/黑板工具失败；
- `mcp.failed`：MCP server 工具调用失败（server 离线/协议错误/工具报错）。

落库前三处调用点（**互不双写**）：

| 挂点 | 记什么 | 说明 |
|---|---|---|
| `core/agent/tools.py` `ToolDispatcher.dispatch` | tool.failed | 任务链会话 + chat 线程**本地工具**统一入口；`_TOOL_FAIL_PREFIXES` 命中即记；source 按 author 前缀 `chat-` 自动分流 task/chat |
| `core/chat/runtime.py` `ChatTurn._dispatch` → `_record_failure` | mcp.failed + tool.failed | 只记**不经 dispatcher 的分支**：mcp__（MCP 失败此前零事件）、todo_write / call_expert / 分发异常 / 无工具面 |

payload：`{name, server?, tool?, args(截断200/值), error(截断800), source,
step?, thread_id?}`；`session_id`/`author` 走 events 列（chat=chat-<tid12>，
任务链=会话 id）。chat 行**必带 thread_id**（完整线程 id）——诊断页按会话
归属计数与「定位会话」都靠它。

## 约定 / 坑

- **best-effort 红线**：`_record` 吞掉一切落库异常——审计事件炸掉工具链比
  丢一条日志严重得多。
- `query_failures` 的过滤（name/author）在 **Python 侧**做：失败量级小，
  且避开 sqlite JSON 抽取的构建差异。**author 是三处子串任一命中**：
  `events.author` 列、`events.session_id` 列、`payload.thread_id`——任务链
  author=session_id，chat author 是线程尾缀而 thread_id 才是完整 id，单查
  一列会漏。扫描上限 `_SCAN_CAP=2000`，倒序返回。
- 历史失败**不回填**：本模块上线前的失败只在 chat_messages 的 tool 结果行
  （`[错误]` 文本）里，事件面无记录。
- API：`GET /api/projects/{pid}/diagnostics/sessions`（统一会话清单，chat+
  agent，带 msg/fail 计数）与 `.../diagnostics/failures`（kind/name/author
  过滤）在 core/api/app.py chat 端点区；消息正文不新开端点——chat 走
  `GET /api/chat/threads/{tid}`，agent 走 `GET /api/projects/{pid}/events
  ?session_id=`，前端分流。
