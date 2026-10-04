# core/chat/ — 智能体工作台对话运行时（K9）

对话式挖洞入口（蛙池式）的独立轻量运行时。**与会话窗/任务队列体系完全解耦**：无 sessions 行、无任务认领、无计划闸——每线程一个 `ChatTurn` 跑一轮（LLM 循环到纯文本回复为止）。复用件：LLM provider（`llm.chat`）、`ToolDispatcher`（工具处理器原样复用，含审计/spill）、`load_expert`（专家池）、技能注册表。

## 数据（blackboard v25/v26）

`chat_threads`（project/agent_id/title/status idle|running|error/parent_thread_id **ON DELETE CASCADE**/spawned_task/todo JSON/usage JSON）+ `chat_messages`（role user|assistant|tool / content / **thinking（v31：assistant 行该步推理正文，纯展示不参与重放）** / tool_calls JSON / tool_use_id）。消息按 API 数组序落库，**重放即 LLM messages**。唯一写入口 `store.py`（`bb._tx()`）。

`delete_thread` 手动 BFS 收齐后代 → 先清消息再删线程行：chat_messages 外键无 CASCADE 而 foreign_keys pragma 开启，靠 DB 隐式级联会被子消息挡路报 FOREIGN KEY constraint failed。

## 运行时（runtime.py）

- **主控**（agent_id=chat-orchestrator）只做编排与只读查询：todo_write + call_expert + skill_open/kb_search/kb_open/bb_query + `bb_delete_asset`/`bb_merge_assets` + mcp__*，不直接执行扫描/利用。专家清单进 call_expert 工具描述（`_generalist`/主控自身不进清单）。
- **技能名≠专家名消歧**：主控 system 同时注入「技能清单」与 call_expert 的「专家」列表，模型曾把技能名当专家名传入——`expert` schema 加 `enum`（值域=`_delegatable_experts`）、技能清单标题标注「下列是【技能名】，不是专家」、未知专家报错同口径；硬校验仍是 `_tool_call_expert`。
- **子专家**（agent_id=专家池 id）：AGENT_TOOLS 全量裁剪——排除任务队列/计划/意图/收尾/私信/提案/HITL 控制原语（`_EXPERT_EXCLUDED`）；专家 yaml `tools:` 优先；+ mcp__*；无 call_expert（**spawn 深度=1**）。`expert_tool_names` 返回 **None**（yaml 未配 tools）=「全量裁剪」而非空白名单——`_build_dispatcher` 与 `_tool_specs` 必须同一处理，否则 specs 全量给 LLM、dispatch 层全拒。
- `call_expert`：spawn 持久子线程（parent_thread_id 留档）→ 隔离上下文跑完 → 摘要+线程引用回传；同轮多个并发执行，**最多 4 个同时运行**，超出排队；工具结果按模型调用顺序写回。
- **对话链意图链路**：意图三件套 + `bb_delete_intent` 开放给对话主控与子专家，与任务管线同纪律——`bb_add_finding` 意图先行门禁扩到 `chat-*`；子专家交摘要前若有 open 意图须先 `close_intent`（主控豁免，它是调度者）；`declare_intent` 回执按轨附 `[意图口径]` 提示。
- **流式事件**：`chat.delta`/`chat.thinking.delta`（节流 ≥6 字符或 0.05s≈20Hz，2026-10-04 由 80/1s 一路压密求顺滑）→ 每步完成落 `chat.thinking` 终稿；工具调用落 `chat.tool`（**phase=start/done 两段**）；终稿 `chat.message`；spawn 落 `chat.spawn`。**消息全文以 chat_messages 表为准，事件只做实时可见**；**思考正文（v31）随 assistant 消息落库**（`append_message(..., thinking=)`），轮结束后前端仍能渲染「已思考」折叠行。**顺滑要三处同步压密**：节流阈值 + WS 投递节奏（app.py `ws_events` 1s→50ms）+ 前端 flush 窗口（`useEvents` 的 `flushMs`，工作台传 50ms）——只降其中一处=白降。轮**正常收尾后**调 `bb.prune_chat_thread_deltas` 清剪本轮流式 delta（终稿全文已在 chat_messages；异常/中断不调，残留=现场审计）。
- **工具历史严格配对（2026-10-04）**：`_sanitize_history` 装载旧线程时按 assistant tool-use ID 去重、只保留首个同 ID result、按 tool-use 顺序重排并补缺失占位，孤儿/错配结果不回放；当前 `resp.tool_calls` 落库前拒绝空/重复 ID、空工具名和非 dict 参数。修复只作用于内存回放，原始 chat_messages 审计不删除。
- **截断重试 + 工具失败约定**：`_chat` 对 `LLMError.truncated`（工具参数流截断/网关 SSE 尾部冗余）整轮重发 ≤2 次（流不可重放），重试前 `_reset_delta` 清空累加与节流基准防「半截+新流」拼接上屏；`_dispatch` 返回 `(ok, text)`，失败文案统一 `[错误]` 前缀（兼容 `[工具异常]`/`[参数格式]`），持久化消息靠该前缀判定（**勿用 startsWith("[")**——会把 bb_query 的 JSON 数组结果误判为失败）。**工具异常兜底红线（2026-10-03）**：`_dispatch` **整个分发体**包 try——mcp__ / todo_write / call_expert / _dispatcher 四分支任一抛异常一律转 `[错误] 工具 X 执行异常: …` 作为 tool_result 回给 LLM，**工具失败绝不等于轮失败**（此前只有 `_dispatcher` 分支有 try，`call_expert` 的子 ChatTurn 构造读专家 yaml 失败会穿出把整轮炸成 unknown）；`_tool_call_expert` 的**子线程构造 + run 同在一个 try 内**（构造失败回文本，主控可改派）。
- **用量与人类引用**：`LLMResponse.usage` → `chat_threads.usage`（v26，{input,output,steps,cache_read,cache_creation}）+ `chat.usage` 事件；前端按 input/256K 画卡头圆环。`POST messages` 可带 `refs {skills,mcps}`（人类本轮指定）——当轮系统提示注入「人类本轮指定」段，**不入库不污染历史**。`usage.breakdown`={system,refs,tools,messages}，按字符估算（~3 字符/token）再用 LLM 真值 input 归一（**四块之和恒等于 input**）。
- **规则链注入（与任务链同构）**：`ChatTurn.__init__` 接 `owner_tags`/`rule_profiles`（app.py 自项目 config 取），`_system_prompt` **最前**注入 `build_rules_preamble`（红线+owner 叠加+评级硬指令，role=agent_id）；`_tool_call_expert` spawn 子线程透传同参。
- **重启僵尸清扫**：进程重启后 DB status=running 残留 → 工作台永久「执行中」（停止 409 死锁）。`store.recover_running_threads(bb)` 扫 running → 归位 idle + 落「⚠ 进程重启，本轮执行中断」assistant 消息；单事务手写（_tx 不可嵌套）、幂等；挂在 `GET /api/projects/{pid}`。
- **工作区装配（与任务链对齐）**：构造接 `artifacts_dir`/`browser_pool`/`decompiler_factory`，`_build_dispatcher` 传 artifacts_dir + role_skills，构造后挂 browser（按轨池）与 decompiler（每线程一实例，失败降级 None）；任务队列系参数**不装**（消费者已被 `_EXPERT_EXCLUDED` 排除）。
- max_steps 主控/子专家均 200；**末步强制终稿**（最后一步不传 tools）；线程 status：running 起、idle 正常收、error 异常收。
- **纯文本终稿截断自动续写**：`_loop` 若见纯文本终稿且 `_is_truncated_final`（`stop_reason ∈ {length, max_tokens}`）**不当作终稿**——半截文本作 assistant 前缀落库并入历史 + 追一条 user 催续（`_CONTINUE_NUDGE`，**只入内存 messages 不落库**）继续拼接；上限 `_CHAT_CONTINUE_MAX=3`，超限落 `chat.truncated`（phase=giveup）。带 tool_calls 不算终稿截断。
- **失败分类与降级不中断（2026-10-03 健壮性）**：`run()` 只接住 `_loop` 抛出的异常 → `_classify_error` 落结构化 error + `chat.error` 事件。分类枚举：`stream`（截断）/`context`（超限）/`network`/`rate_limit`/`auth`/`quota`/`bad_request`/**`filesystem`（OSError 兜底）**/**`db`（sqlite3.Error / BlackboardClosedError）**/`unknown`。**分支顺序红线**：`TimeoutError`/`ConnectionError` 是 `OSError` 子类，network 分支必须排在 filesystem 之前。**重试面**：LLM 传输层（provider 内）→ `_chat` 截断整轮 ≤2 → `_chat` ContextOverflow 压缩重试 1 次 → **`_retry_transient`（装载期 `_load_history`/`_tool_specs` 的瞬时 OSError/sqlite3.Error 退避重试 1 次，语义性错误如 BlackboardClosedError 不重试）**。**降级不中断**：`_system_prompt` 的 `load_expert`/`build_rules_preamble` 失败回退空人设/空规则串；`_build_dispatcher` 的白名单/专家读取失败降级无工具面——都不再把整轮炸成 unknown。**构造期异常**由 app.py `chat_send` 的 `_run` 兜底归位（仅当状态仍 running 时置 error），消除「ChatTurn(...) 抛错 → 线程永久卡 running」。

## MCP 桥（mcp_bridge.py）

读 `config/mcp.json`（app 传 `MCP_CONFIG_PATH`），按项目域过滤（domains ∩ capabilities∪track）。transport：streamable-http（JSON-RPC over HTTP，initialize→Mcp-Session-Id→initialized→tools/list/call，**只允许 loopback**）+ stdio（子进程行分隔 JSON-RPC，后台读线程收响应、stderr 排干、崩溃重拉一次）。工具名 `mcp__<server>__<tool>`；tools/list 300s TTL 缓存；单 server 离线不阻断。要点：

- **会话级 server 隔离**：`SESSION_SCOPED_SERVERS={"playwright"}` 的 stdio server **按 `(server, session_id)` 各起独立子进程**（环境变量 `PW_SESSION_ID` 传会话标识，启动器据此派生独立 Chrome profile/CDP 端口）——修「多会话抢同一持久化 profile」；同会话多轮复用同一进程（登录态跨轮保留）。`ChatTurn._dispatch_mcp` 传 `session_id=self.thread_id`。`MAX_BROWSER_SESSIONS=4` 限并发，超限回结构化错误，已死会话配额自动回收；装配工具面用 `PW_NO_CHROME=1` 探针进程（只 tools/list）；`status()` 标 `session_scoped=true`；启动器空闲 1h（`PW_IDLE_MS` 可覆盖）无活动页 → 关自管 Chrome + 删 profile + 释放锁。
- **项目内嵌**：Playwright 调用按项目注入 `PW_BROWSER_CDP_ENDPOINT`，先启动 `BrowserPool` 的项目持久浏览器，再让 MCP 通过 loopback CDP 接入同一 context/profile——共享登录态/页面/抓包链路，不再另起外部 Chrome。stdio server 统一以仓库根目录为 cwd。
- **stdio 握手**：`_StdioConn._rpc` 先做 MCP 握手（`initialize` → `notifications/initialized`）再发业务请求，每次进程重启后重做（`_ready` 标志）——缺握手时 `@playwright/mcp` 的 `tools/call` 会被拒（`tools/list` 却可能成功）。启动器：`--user-data-dir` 必须排在 `--remote-debugging-port` 前；端口跳过 Windows TCP 排除段（本机 8421~9880），默认基址 **14000**；绑不上调试端口时换端口重试而非降级自托管浏览器（降级会丢会话隔离）。

## API 与边界

- 端点（app.py「智能体工作台」节）：`GET /api/chat/agents?pid`（主控+专家池，排除 _generalist）/ `GET|POST /api/projects/{pid}/chat/threads` / `GET|DELETE /api/chat/threads/{tid}`（detail 带 messages，after_id 增量）/ `POST /api/chat/threads/{tid}/messages`（**202 + 后台线程跑轮**；同线程并发 409；`app.state.chat_running` 去重）/ `GET /api/chat/mcp?pid`。`_pid_of_chat` 反查项目 id。
- **中止**：`POST /api/chat/threads/{tid}/stop` 置每线程一个 threading.Event（`app.state.chat_abort_events`，轮次收尾弹出防泄漏）→ ChatTurn 在**步间 / llm.chat 流式帧（should_cancel 逐帧探测）/ 工具分发前**退出并落「已按人类要求停止」消息（stopped:true）；事件向下传（call_expert 子轮同事件、ToolDispatcher abort_event 进 run_cmd）。没在跑 409。
- 无 per-thread 模型选择（全局 executor LLM）；LLM usage 不进 orchestrator_state 记账。chat.* 事件未接 lib/events.tsx 样式（事件流显原始 JSON；工作台内已结构化渲染）。
