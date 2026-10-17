# core/diagnostics — 诊断（会话审计 + 失败日志 + 请求体留痕 + 红线自检）

`failog.py` — 工具 / MCP 调用失败日志（diagnostics-log，2026-10-14）。
`system_prompt.py` — 会话系统提示词快照（diagnostics-system-prompt，2026-10-16）。
`requests.py` — **LLM 请求体留痕（diagnostics-request-body，2026-10-17）**。
`redline_test.py` — **红线一键自检（diagnostics-redline-test，2026-10-17）**。

## requests.py：请求体留痕

`sink` 由 `core/llm/dump.py` 回调（`set_sink(request_log.store)` 在 `create_app`
注册），把每一步**真实发往服务端的 body** 按 `(project_id, session_id)` 落成
`logs/llm_requests/<pid>/<sid>.jsonl`，诊断页「请求体」视图直接读。

- **开关**：`config/llm_dump.json` 的 `{"enabled": true}`，诊断页顶栏切换
  （`PUT /api/diagnostics/llm-dump`）。**默认关**——关闭时 `store()` 首行返回，
  不产生任何文件；`is_enabled()` 按文件 mtime 缓存（一次 stat/调用）。
- **上限**：每会话保留最近 `KEEP_PER_SESSION=20` 条（追加时裁剪），单条正文超
  `STORE_MAX_BYTES`（默认 4MB，`CYBERSTRIKE_LLM_DUMP_STORE_MB` 可调）只留元信息。
- **字节级自证（wire）**：每条 record 的 `wire` 是 httpx request hook 录到的
  **真实出网字节**（`bytes`+`sha256`，可能多条——重试逐次录），`wire_match` 表示
  「唯一一次出网且字节==payload」。列表接口的摘要带 `wire_bytes`/`wire_sha256`，
  前端详情据此渲染「本地 payload N B · sha → 真实出网 M B · sha → 字节一致 ✓ /
  字节不同（SDK 重序列化，字段一致）」。注意 `wire_match=false` 不代表内容不一致
  （SDK 重新序列化所致，实测顶层键/`messages`/`tools` 无差异）；也不覆盖中转网关
  是否原样转发（需外部抓包）。详见 core/llm/CLAUDE.md「字节级自证」条目。
- **system 兼容通道统计（2026-10-17）**：供应商开了 `system_in_user`（网关丢 system）
  时 payload 里没有标准 system 字段，system 前置于首条 user 消息（边界标记
  `<<<CYBERSTRIKE-SYSTEM-PROMPT-BEGIN>>>`）。`_summarize` 会从该标记取回 system，
  照常报 `system_chars`/`system_first`，并加 `system_in_user: true`——否则列表显示
  「system 0 字」会再次被误读成「没注入」。见 `_extract_inline_system()`。
- **归属**：LLM 层不知道会话，靠 `core/llm/dump.py::scope()`（contextvar）由
  `ChatTurn._loop` 与 `AgentSession._loop` 包住一轮；**contextvar 不跨线程**，
  子线程（专家/后台任务）各自在入口再包一层，未包的不落（宁缺不错记）。
- **红线**：写路径异常只 log 不抛；`_rid` 用「时间戳去符号 + payload 指纹前 8 位」
  （可安全进 URL 路径，`#`/`:` 会破坏路由）。
- 测试 `tests/test_diag_requests.py`（10 例，含 wire 捕获、hook 不可用降级、sink+scope 端到端与「回调炸了不影响请求」）。

## redline_test.py：红线一键自检

把「system 到底进没进上下文」的排查固化成一次点击：
诊断页「系统提示词」面板头部点「红线一键测试」→ `GET` 取该会话
最近一次实际下发的 system → 三层检查。

- **① 注入检查（本地，不联网）**：`local_check(system_text)` 数 6 类规则
  标记（红线 / 规则前言 / 评级硬指令 / `rule:rating:` / `rule:owner:` /
  `rule:track:`），得 `full`（全中）/ `partial` / `none`（system 为空直接
  `none`）。**它只证明「本地准备发什么」，不证明「线上收到什么」**——这正是
  当年幻灵网关事故里被误读的一环。
- **② canary 通道检查（决定性）**：`_canary_probe` 拿一份带随机
  canary 串的 system 副本（原文 + 「回复必须以 <串> 开头」）发一次
  **无工具、纯 chat** 探测请求（不写任何数据），看模型回复开头是否照做。
  模型无法可靠内省系统提示，canary 是判定 system 通道是否真通的唯一硬
  证据。**坑**：思考链（`reasoning effort=high`）会把输出预算吃光，
  `max_tokens` 太小返回空 text——不能据此判「通道断」，故
  `CANARY_MAX_TOKENS=1024` 且空回复标 `empty_reply` 降级为辅证
  （若内容探针命中 needle 仍判 ok）。探测地址由 `_resolve_target`
  取「显式传参 > 线程覆写 > 全局默认」，前端可选供应商/模型覆写。
- **③ 内容引用检查（辅证）**：`_content_probe` 从 system 里取一条
  `## rule:` 标题行（`_pick_needle`，问法不泄题），要求模型复述它——
  用来在 canary 失效时兜底举证。
- **verdict**（`_verdict` 汇总，永不抛）：`ok` / `not_injected`
  （system 或标记缺失）/ `channel_broken`（本地有、canary 不遵守且内容
  也不命中）/ `inconclusive`（空回复等判不了）/ `no_system` /
  `probe_failed` / `error`。**`channel_broken` ≈ 网关又在丢 system**，
  查 provider 是不是漏开 `system_in_user`（见 core/llm/CLAUDE.md）。
- **会话定位**：`_session_system` 快照表 `session_system_prompts` 优先，
  回落**请求体记录** `requests.py` 的 jsonl（含 inline 兼容通道取回）；
  `PROBE_TIMEOUT=60`（`build(timeout=)` 透传防长挂）。
- API：`POST /api/projects/{pid}/diagnostics/redline-test`
  （body `{session_id, provider?, model?}`，无 session_id 回 422），
  前端 `RedlineTestCard` 渲染 ①②③ + 总结建议。
- 测试 `tests/test_diag_redline_test.py`（14 例：full/partial/none、needle
  不泄题、三种主 verdict、空回复降级、无 system、探测失败、记录回落含
  inline、线程覆写、LLM 抛错不外抛）。

## failog.py：失败日志

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
