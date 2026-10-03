# core/chat/ — 智能体工作台对话运行时（K9，2026-09-29）

对话式挖洞入口（蛙池式）的独立轻量运行时。**与会话窗/任务队列体系完全解耦**：
无 sessions 行、无任务认领、无计划闸/意图纪律——每线程一个 `ChatTurn` 跑一轮
（LLM 循环到纯文本回复为止）。复用件：LLM provider（`llm.chat` 接口）、
`ToolDispatcher`（黑板/技能/网关工具处理器原样复用，含网关审计/spill）、
`load_expert`（专家池）、技能注册表。

## 数据（blackboard v25）

`chat_threads`（project/agent_id/title/status idle|running|error/parent_thread_id
**ON DELETE CASCADE**/spawned_task/todo JSON）+ `chat_messages`（role
user|assistant|tool / content / tool_calls JSON / tool_use_id）。消息按 API
数组序落库，**重放即 LLM messages**（assistant 行重建 tool_use 块、tool 行转
tool_result）。唯一写入口 `store.py`（`bb._tx()`）；线程删除级联清消息+子线程
（`delete_thread` 手动 BFS 收齐全部后代 → 先清消息再删线程行；chat_messages
外键无 CASCADE 而 foreign_keys pragma 开启，DB 隐式级联会被子消息挡路报
FOREIGN KEY constraint failed → 曾致删有子线程的线程 500）。

## 运行时（runtime.py）

- **主控**（agent_id=chat-orchestrator）：todo_write + call_expert + skill_open/
  kb_search/kb_open/bb_query + `bb_delete_asset`/`bb_merge_assets` 受控资产维护 + mcp__*；
  不直接执行扫描/利用等实操（蛙池式主控轻）。专家清单进
  call_expert 工具描述（`_generalist`/主控自身不进清单）；相互独立的待办可在同轮
  生成多个 `call_expert` 并发执行，有依赖的待办仍按顺序委派。
  **技能名≠专家名消歧（2026-10-01 复盘）**：主控 system 同时注入「可用技能清单」
  （`recon-asset-enum` 等）与 call_expert 的「可用专家」（`recon`/`osint`…），
  模型曾把技能名 `recon-asset-enum` 当专家名传入报「未知专家」。三层防护：
  ① `expert` schema 加 `enum`（值域=可委派专家，见 `_delegatable_experts`）——
  软约束降错率；② 技能清单标题显式标注「下列是【技能名】，不是专家」；
  ③ 未知专家报错文案同口径（`_delegatable_experts`，不含 `_generalist`）+
  「这些是专家 id，不是技能名」。硬校验仍是 `_tool_call_expert` 成员判定。
- **子专家**（agent_id=专家池 id）：AGENT_TOOLS 全量裁剪——排除任务队列/计划/
  意图/收尾/私信/提案/HITL 控制原语（`_EXPERT_EXCLUDED`，防污染任务队列）；
  专家 yaml `tools:` 字段优先；+ mcp__*；无 call_expert（**spawn 深度=1**）。
  **None 语义统一（2026-09-29 回归修复）**：`expert_tool_names` 返回 None
  （yaml 未配 tools 字段，recon/web-solver 等全部专家）=「全量裁剪」——
  `_build_dispatcher` 与 `_tool_specs` 必须同一处理；首版曾把 `None or []`
  当空白名单降级为无工具面（specs 全量给 LLM、dispatch 层全拒绝，「工具 X
  不可用：本线程未装配工具面」），回归测试 test_expert_dispatcher_*。
- `call_expert`：spawn 持久子线程（parent_thread_id 留档）→ 隔离上下文跑完 →
  摘要+线程引用回传主控；不审批、事件流全程可见。同一轮主控一次生成多个
  `call_expert` 时并发执行，最多 4 个同时运行，超出的排队；工具结果仍按模型
  调用顺序写回，保证消息历史可重放。
- **对话链意图链路（intent-tools-chat，2026-10-01）**：意图三件套 + `bb_delete_intent`
  从 `_EXPERT_EXCLUDED` 移出、开放给对话主控与子专家——此前对话跑完「黑板→发现→
  链路」全空（对话链从未声明意图）；现对话链与任务管线同纪律：① `bb_add_finding`
  的意图先行门禁 author 判定扩到 `chat-*`（无 open 意图/声明后无执行动作均 `[拒绝]`）；
  ② 子专家交摘要前若本线程有 open 意图 → 拦截并要求先 `close_intent`（主控豁免，
  它是调度者）；③ `declare_intent` 回执按轨附 `[意图口径]` 提示（pentest=攻击面/
  漏洞假设，reverse=函数/协议/样本行为等，只提示不硬拦）。
- `todo_write`：整体覆写 thread.todo（工作记忆外化，前端 TodoCard 渲染）。
- 流式：llm on_text 攒 delta → 节流（≥80 字符或 1s）落 `chat.delta` 事件；
  llm on_thinking 同样透传并落 `chat.thinking.delta`，每步完成落
  `chat.thinking` 终稿，工作台按当前用户消息实时显示思考内容；
  工具调用落 `chat.tool`（**phase=start/done 两段**（2026-09-29）：dispatch 前
  start、完成后 done 带 ok/duration_s/result_head）；终稿落 `chat.message`；
  spawn 落 `chat.spawn`。**消息全文以 chat_messages 表为准，事件只承担实时可见性**。
- **截断整轮重试（2026-10-01 事故修复）**：`_chat` 对 `LLMError.truncated`（工具
  参数流截断/网关 SSE 尾部冗余）整轮重发 ≤2 次（与 agent 同口径——流不可重放，
  只能轮级重试）；重试前 `_reset_delta` 清空 delta 累加与节流基准，防「半截+新流」
  拼接上屏（前端取最后一条 chat.delta 的累计全文）。错误卡片新增 `stream`
  （网关流异常）分类，不再落 `unknown`→「执行异常」。
- **工具失败约定（2026-09-29）**：`_dispatch` 返回 `(ok, text)`，失败文案统一
  `[错误]` 前缀（agent 工具面兼容 `[工具异常]`/`[参数格式]`）；`ok` 是结构化
  真值进 chat.tool 事件，持久化消息靠 `[错误]` 前缀判定（勿再用 startsWith("[")——
  会把 bb_query 等 JSON 数组结果误判为失败）。
- **用量与人类引用（2026-09-29，K9-C）**：ChatTurn 消费 `LLMResponse.usage` →
  写 `chat_threads.usage`（schema v26，JSON {input,output,steps,cache_read,
  cache_creation}；input(+cache 三项)=当步上下文窗口占用，output/steps 跨步累计）
  并落 `chat.usage` 事件；前端按 input/256K 画卡头圆环。`POST messages` 可带
  `refs {skills:[], mcps:[]}`（人类本轮指定）——当轮系统提示注入「人类本轮指定」
  段（优先按技能 X 打法 / 优先用 MCP Y 的工具），**不入库不污染历史**。
- **上下文构成（2026-09-29，K10，Claude Code /context 式）**：`usage.breakdown`
  = {system,refs,tools,messages} 四块——`_loop` 每步按字符估算各块比例
  （~3 字符/token，`_est_tokens`/`_msg_text`），再用 LLM 真值 input_total 归一
  （scale 缩放 + messages 余数兜底，**四块之和恒等于 input**）。refs 块对应
  「人类本轮指定」注入段（无引用时为 0）。前端浮层画分段彩条+分类清单，
  剩余空间 = 窗口(256K) − input。估算非精确计数：比例可信、绝对值是近似。
- **规则链注入（2026-09-29，与任务链同构）**：`ChatTurn.__init__` 接收
  `owner_tags`/`rule_profiles`（app.py 构造点自项目 config 取，与 AgentConfig
  同款），`_system_prompt` **最前**注入 `build_rules_preamble`（红线+owner
  叠加+评级硬指令，role=agent_id、role-rules 文件存在时自动叠加）；
  `_tool_call_expert` spawn 子线程时透传同参。首版对话链完全不接规则链——
  子专家 bb_add_finding 判级自由发挥不引用 rating:<tag> 条款（任务链
  loop.py stable_parts[0] 早已注入，两条链不对称）；回归测试
  test_expert_thread_rule_preamble_injected / test_rule_preamble_propagates_
  to_spawned_expert。未配 owner/评级时静默降级（track 红线仍注入）。
- **重启僵尸清扫（2026-09-29）**：进程重启后执行轮次随旧进程消失（abort_event/
  执行线程/run_cmd 子进程全灭），DB status=running 残留 → 工作台永久「执行中」
  （输入框禁用、停止 409 死锁）。`store.recover_running_threads(bb)`：扫 running
  → 归位 idle + 落「⚠ 进程重启，本轮执行中断」assistant 消息（与手动停止同款
  观感，历史保留可续聊）；单事务手写（_tx 不可嵌套）；无僵尸 no-op 幂等。
  挂在 `GET /api/projects/{pid}`（进项目页必经，新进程 chat_running 空集 →
  扫到的 running 必是僵尸；与任务侧 estranged「重启急停」同构）。
  回归测试 test_recover_running_threads。
- **工作区/重装备装配（2026-09-29，与任务链对齐）**：ChatTurn 构造接
  `artifacts_dir`/`browser_pool`/`decompiler_factory`，`_build_dispatcher`
  传 artifacts_dir + role_skills（专家 yaml skills 字段），构造后挂
  browser（按轨池，轨外 app.py 传 None）与 decompiler（每线程一实例，
  工厂闭包携带自建 gateway/ida_mcp 上下文，失败降级 None）。首版漏传
  artifacts_dir——read_file/search_files/bb_add_artifact 全拒「未装配工作
  区」、run_cmd workspace=None cwd 漂移（nmap -oN 输出写丢且 docker 无挂载
  随容器销毁）；任务队列系参数（allowed_roles/allowed_task_types/max_steps/
  stuck_after/closing_max_rounds）**不装**——消费者 publish_task/request_steps/
  finish 已被 _EXPERT_EXCLUDED 排除，装了无消费者。回归测试
  test_expert_thread_workspace_tools / test_workspace_params_propagate_to_
  spawned_expert。
- max_steps：主控 200 / 子专家 200（2026-10-01 由 24/32 提升）。**末步强制终稿**：
  循环最后一步不传 tools（`step_tools=None`），逼模型输出纯文本终稿，避免「步数
  耗尽但全程只调工具」→ 落「本轮未产出文本回复」。线程 status：running 起、
  idle 正常收、error 异常收（异常也落一条 chat.message 事件，前端可见）。
- **纯文本终稿截断自动续写（2026-10-01，修「话说一半就正常终止」）**：`_loop`
  拿到响应后，若为纯文本终稿且 `_is_truncated_final(resp)`（`stop_reason ∈
  {length, max_tokens}`）**不当作终稿收尾**——把半截文本作为 assistant 前缀
  落库并入历史 + 追一条 user 催续（`_CONTINUE_NUDGE`，**只入内存 messages，
  不落库**，避免污染人类对话），继续循环拼接成完整终稿（`text_acc` 逐段拼接）。
  续写上限 `_CHAT_CONTINUE_MAX=3`，超限接受现有文本并落 `chat.truncated`
  （phase=giveup）；步数耗尽但有累积文本时也按其收尾并落 giveup
  （reason=steps_exhausted）。带 tool_calls 的响应不算终稿截断（走工具路径 /
  工具参数整轮重试）。**只治纯文本终稿截断**；工具参数截断仍走 `_chat` 的整轮
  重试。

## MCP 桥（mcp_bridge.py）

读 `config/mcp.json`（app 传 `MCP_CONFIG_PATH`），按项目域过滤（domains ∩
capabilities∪track）。transport：streamable-http（JSON-RPC over HTTP，
initialize→Mcp-Session-Id→initialized→tools/list/call，**只允许 loopback**；
协议时序同 decompiler.MCPBackend）+ stdio（子进程行分隔 JSON-RPC，后台读线程
收响应、stderr 排干、崩溃重拉一次）。工具名 `mcp__<server>__<tool>`；tools/list
结果 300s TTL 缓存；单 server 离线不阻断（状态点 `online=tools 非空`）。

**会话级 server 隔离（2026-10-01）**：`SESSION_SCOPED_SERVERS={"playwright"}` 的
stdio server **按 `(server, session_id)` 各起一个独立子进程**，用环境变量
`PW_SESSION_ID` 把会话标识传给启动器；启动器据此派生独立 Chrome profile / CDP
端口，修「多个会话抢同一持久化 profile → Browser is already in use」。同会话内多轮
复用同一进程（登录态跨轮保留），异会话互不干扰。`ChatTurn._dispatch_mcp` 传
`session_id=self.thread_id`。`MAX_BROWSER_SESSIONS=4` 限制并发会话数，超限回结构化
错误引导（提示等空闲回收或减并发）；已死会话（空闲回收/退出）的配额会被自动回收。
装配工具面时用 `PW_NO_CHROME=1` 探针进程（只 `tools/list`，不起真实浏览器）；
`status()` 对会话级 server 标 `session_scoped=true`，也通过该探针返回工具清单。
启动器空闲 1h（`PW_IDLE_MS` 可覆盖）无活动页 → 关自管 Chrome + 删 profile + 释放锁。

**项目内嵌（2026-10-03）**：Playwright 实际调用按项目注入
`PW_BROWSER_CDP_ENDPOINT`，先启动 `BrowserPool` 的项目持久浏览器，再让 MCP
通过 loopback CDP 接入同一 context/profile；因此 MCP 与内置浏览器共享登录态、页面
和抓包链路，不再另起外部 Chrome。stdio server 统一以仓库根目录为 cwd，配置中的
相对命令路径不依赖服务启动目录。

**stdio 握手（2026-10-01）**：`_StdioConn._rpc` 现在先做 MCP 握手
（`initialize` → `notifications/initialized`）再发业务请求，且每次进程重启后重做
（`_ready` 标志）。此前缺握手，`@playwright/mcp` 的 `tools/call` 会被拒（`tools/list`
却可能成功），表现为「server 离线或协议错误」。启动器侧：`--user-data-dir` 必须排在
`--remote-debugging-port` 前（否则有常规 Chrome 时调试端口不监听）；端口选取跳过
Windows TCP 排除段（`netsh interface ipv4 show excludedportrange protocol=tcp`，
Hyper-V/WinNAT 保留段，本机为 8421~9880）；默认基址 9244 → **14000**（9244 恰在
保留段内）；Chrome 绑不上调试端口时换端口重试而非降级到自托管浏览器（降级会丢会话隔离）。

## API（app.py「智能体工作台」节）

`GET /api/chat/agents?pid`（主控+专家池清单，排除 _generalist）/ `GET|POST
/api/projects/{pid}/chat/threads` / `GET|DELETE /api/chat/threads/{tid}`（detail
带 messages，after_id 增量）/ `POST /api/chat/threads/{tid}/messages`（**202 +
后台线程跑轮**；同线程并发 409；`app.state.chat_running` 集合去重）/
`GET /api/chat/mcp?pid`（桥状态）。`_pid_of_chat` 反查项目 id（扫已打开项目）。

## 已知边界（v1）

- **中止（2026-09-29 补）**：`POST /api/chat/threads/{tid}/stop` 置每线程一个
  threading.Event（`app.state.chat_abort_events`，轮次收尾弹出防泄漏）→
  ChatTurn 在**步间 / llm.chat 流式帧（should_cancel 逐帧探测）/ 工具分发前**
  退出并落「已按人类要求停止」assistant 消息（stopped:true 事件）；事件向下传
  （call_expert 子轮同事件、ToolDispatcher abort_event 进 run_cmd 等）；前端
  running 时发送按钮变红色停止钮。线程没在跑 stop=409。
- 无 per-thread 模型选择（全局 executor LLM）。
- LLM usage 不进 orchestrator_state 记账（对话成本不入编排预算）。
- chat.* 事件未接 lib/events.tsx 样式（事件流显原始 JSON；工作台内已结构化渲染）。
