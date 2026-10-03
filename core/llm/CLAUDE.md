# core/llm/

> LLM 接入层（DESIGN.md §8）：内部统一 Anthropic `/v1/messages` 标准，火山引擎 Ark 优先，多供应商可配。

供应商条目可设置 `proxy: "http://host:port"`，该代理会同时用于普通请求、SSE 流式请求、模型发现和测活；未设置时沿用系统 `HTTP_PROXY/HTTPS_PROXY` 环境变量。当前代理协议为 HTTP/HTTPS。

## 文件

| 文件 | 职责 |
|------|------|
| `provider.py` | 抽象基类 + 数据类 `LLMResponse/ToolCall/Usage`、`LLMError`；`chat(messages, system, tools, max_tokens, temperature)` 为唯一调用面 |
| `anthropic_compat.py` | `AnthropicCompatProvider`：OpenAI/Anthropic 兼容协议适配（tools、tool_use、thinking 解析）；测试用 ScriptedLLM 继承它回放剧本；传输层对 429/5xx/超时重试 1 次（共 2 次尝试，2026-09-28 由 3 次收紧——坏窗口不拖长静默）。**全走流式（2026-09-28）**：只要网关支持（未 `_stream_disabled`）所有 chat 都走 SSE——回调可选，不传只是不上屏；动因：非流式「有回调才 stream」让 planner/intel 等无回调调用吃 600s「总等待」超时，长思考（实测 6-10 分钟）必炸且重试=原样重发注定再超时；SSE 早回头+帧间隔超时天然抗长思考。**thinking 开关（2026-09-19）**：`enable_thinking=True` 时 body 注入 `{"thinking":{type:enabled,budget_tokens:8192}}`；HTTP 400 文案含 "thinking" → 去参重建 payload 立即重试并记实例标志不再注入（模型不支持优雅降级）；`_parse` 对 content 无 thinking block 时兜底接 OpenAI 式顶层 `reasoning_content`。**思考流式（2026-09-19）**：`chat(..., on_thinking=, should_cancel=)`（类属性 `stream_capable=True` 供 Agent 层探测）走 SSE `stream:true`——`_consume_stream` 解析 `content_block_delta` 帧，`thinking_delta` 逐帧回调 on_thinking，帧拼回与非流式同形 dict 复用 `_parse`（tool_calls/usage 兼容）；建连在原重试循环内（首帧前 429/5xx/超时仍重试，**首帧后不重试**——流不可重放，网络错误包成 LLMError）；逐行轮询 should_cancel 命中即关流抛 LLMError（即点即停）；网关无视 stream 返整份 JSON → 兜底按普通响应解析；400 文案含 "stream" → `_stream_disabled` 本实例回退非流式。**Prompt caching（retrieval-upgrade M1，2026-09-23）**：`chat(..., system=)` 接受 str 或块数组（`build_system_blocks` 产的 stable/dynamic 两 text 块）——`_system_payload` 对 str 单块包装（`enable_cache=True` 缺省打标）并在 stable 块末尾落 `cache_control:{"type":"ephemeral"}`（Ark 兼容层实测直接接受，同前缀第二跑 cache_read>0）；dict 元素浅拷贝透传；HTTP 400 文案含 "cache_control" → `_cache_disabled` 实例置位、剥除全部标记重试一次（不支持的供应商优雅降级）；每次调用新建块对象不污染调用方。**截断防御（2026-09-20 事故修复）**：流结束后拼 tool_use 增量 JSON 失败（输出预算耗尽/网关断流致参数残缺，GLM 两次死在 `{"cmd":` 处）不再裸抛 JSONDecodeError 炸任务——包成 `LLMError(truncated=True)`（含 stop_reason），agent 层据此整轮重试；**尾部冗余容错（2026-10-01 事故修复）**：`json.loads` 失败时先 `json.JSONDecoder().raw_decode` 取「开头完整的 JSON 值」——仅接受 dict，成功即采用并 `log.warning` 留痕（网关 SSE 尾部多发字符：stop_reason=tool_use 正常收尾却报 "Extra data"）；仍失败才抛 `LLMError(truncated=True)` 交上层整轮重试。**按类别重试预算（2026-10-01）**：429/5xx 维持 `MAX_RETRIES=2`/`RETRY_BACKOFF=30s`（保留 2026-09-28 快速失败基调）；**连接类**（`ConnectionError`/超时，含 WSAECONNRESET 10054）独立 `CONN_RETRIES=4` + `CONN_BACKOFF=(5,10,20)s`——SSE 长连接一次重置不再判死整轮。**缺省 `max_tokens` 4096→16384**（原值与思考 budget 8192 倒挂）。`stream_transport` 可注入（测试伪 SSE） |
| `ark.py` | `ArkCodingProvider`：火山引擎 Ark 接入（**enable_thinking 默认 True**；smoke 实证 ark-code-latest/deepseek-v4-flash 默认即吐 thinking block） |
| `providers.py` | `ProviderStore`：读写 `config/providers.json`（多供应商，PUT 全量替换语义、空 api_key=沿用原 key、至少一个启用；**顶层 `default_provider` 可选显式指定全局默认供应商**——`save(..., default_provider=)` 须命中启用中的供应商否则 ProviderError、`default()` 优先取它/未设置兜底第一个启用、`default_provider_name()` 供 UI 下拉）；`probe_credentials` 探活（先 /v1/models，404/405 降级最小 chat 调用）；`ProviderError`。**`build()` 思考开关（2026-09-19）**：条目 `"thinking": true/false` 显式控制，缺省=base_url 含 "ark" 默认开、其余默认关；**每模型最大上下文（2026-09-19）**：条目 `"model_context": {"<模型名>": token 数}`（可选，`_validate` 剔除非法/未勾选项），`build()` 带给实例 `context_tokens`，会话层据此换算上下文预算（见 core/agent loop.apply_context_budget；未声明默认 256K） |
| `routing.py` | `ModelRouter`：planner/executor/classifier 模型路由与覆写；`load_dotenv()` 读 .env |

## 约定与坑

- **OpenAI 兼容层 HTTP 520 重试（2026-10-03）**：上游中转/CDN 返回 520 时，`openai_compat.py` 在首次请求失败后最多重试 5 次（总计 6 次尝试），退避为 2/4/8/16/30 秒；每次通过 `on_retry` 落 `chat.retry` 事件，界面显示 `x/5`。鉴权、参数和其它非暂态 HTTP 错误不重试。
- **TLS EOF 连接重试（2026-10-03）**：`urllib` 包装的 `ssl.SSLEOFError`（尤其 `UNEXPECTED_EOF_WHILE_READING`）判为连接级瞬时故障，交给连接重试预算；其它 SSL 错误仍快速失败。

- 无可用 key 不崩：API Agent/编排端点返回 503；LLM 缺省 = 路由覆写目标或第一个启用供应商的第一个模型。
- Agent 会话内可经 `POST /agents/{sid}/llm` 运行时动态切换 provider 引用，下一次 chat 生效（落 llm.switched 事件）。
- 测试可注入 executor_llm/planner_llm 或用 tmp providers_config，避免种子污染真实 config/providers.json；禁止把任何 key 写进技能文件或提交物。
