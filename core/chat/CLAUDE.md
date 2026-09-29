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
  kb_search/kb_open/bb_query + mcp__*；不碰实操（蛙池式主控轻）。专家清单进
  call_expert 工具描述（`_generalist`/主控自身不进清单）。
- **子专家**（agent_id=专家池 id）：AGENT_TOOLS 全量裁剪——排除任务队列/计划/
  意图/收尾/私信/提案/HITL 控制原语（`_EXPERT_EXCLUDED`，防污染任务队列）；
  专家 yaml `tools:` 字段优先；+ mcp__*；无 call_expert（**spawn 深度=1**）。
  **None 语义统一（2026-09-29 回归修复）**：`expert_tool_names` 返回 None
  （yaml 未配 tools 字段，recon/web-solver 等全部专家）=「全量裁剪」——
  `_build_dispatcher` 与 `_tool_specs` 必须同一处理；首版曾把 `None or []`
  当空白名单降级为无工具面（specs 全量给 LLM、dispatch 层全拒绝，「工具 X
  不可用：本线程未装配工具面」），回归测试 test_expert_dispatcher_*。
- `call_expert`：spawn 持久子线程（parent_thread_id 留档）→ 隔离上下文跑完 →
  摘要+线程引用回传主控；不审批、事件流全程可见。
- `todo_write`：整体覆写 thread.todo（工作记忆外化，前端 TodoCard 渲染）。
- 流式：llm on_text 攒 delta → 节流（≥80 字符或 1s）落 `chat.delta` 事件；
  工具调用落 `chat.tool`（**phase=start/done 两段**（2026-09-29）：dispatch 前
  start、完成后 done 带 ok/duration_s/result_head）；终稿落 `chat.message`；
  spawn 落 `chat.spawn`。**消息全文以 chat_messages 表为准，事件只承担实时可见性**。
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
- max_steps：主控 24 / 子专家 32。线程 status：running 起、idle 正常收、
  error 异常收（异常也落一条 chat.message 事件，前端可见）。

## MCP 桥（mcp_bridge.py）

读 `config/mcp.json`（app 传 `MCP_CONFIG_PATH`），按项目域过滤（domains ∩
capabilities∪track）。transport：streamable-http（JSON-RPC over HTTP，
initialize→Mcp-Session-Id→initialized→tools/list/call，**只允许 loopback**；
协议时序同 decompiler.MCPBackend）+ stdio（子进程行分隔 JSON-RPC，后台读线程
收响应、stderr 排干、崩溃重拉一次）。工具名 `mcp__<server>__<tool>`；tools/list
结果 300s TTL 缓存；单 server 离线不阻断（状态点 `online=tools 非空`）。

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
