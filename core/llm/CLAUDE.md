# core/llm/

> LLM 接入层（DESIGN.md §8）：内部统一 Anthropic `/v1/messages` 标准，火山引擎 Ark 优先，多供应商可配。

## 文件

| 文件 | 职责 |
|------|------|
| `provider.py` | 抽象基类 + 数据类 `LLMResponse/ToolCall/Usage`、`LLMError`；`chat(messages, system, tools, max_tokens, temperature)` 为唯一调用面 |
| `anthropic_compat.py` | `AnthropicCompatProvider`：OpenAI/Anthropic 兼容协议适配（tools、tool_use、thinking 解析）；测试用 ScriptedLLM 继承它回放剧本；传输层对 429/5xx/超时重试 3 次。**thinking 开关（2026-09-19）**：`enable_thinking=True` 时 body 注入 `{"thinking":{type:enabled,budget_tokens:8192}}`；HTTP 400 文案含 "thinking" → 去参重建 payload 立即重试并记实例标志不再注入（模型不支持优雅降级）；`_parse` 对 content 无 thinking block 时兜底接 OpenAI 式顶层 `reasoning_content`。**思考流式（2026-09-19）**：`chat(..., on_thinking=, should_cancel=)`（类属性 `stream_capable=True` 供 Agent 层探测）走 SSE `stream:true`——`_consume_stream` 解析 `content_block_delta` 帧，`thinking_delta` 逐帧回调 on_thinking，帧拼回与非流式同形 dict 复用 `_parse`（tool_calls/usage 兼容）；建连在原重试循环内（首帧前 429/5xx/超时仍重试，**首帧后不重试**——流不可重放，网络错误包成 LLMError）；逐行轮询 should_cancel 命中即关流抛 LLMError（即点即停）；网关无视 stream 返整份 JSON → 兜底按普通响应解析；400 文案含 "stream" → `_stream_disabled` 本实例回退非流式。**截断防御（2026-09-20 事故修复）**：流结束后拼 tool_use 增量 JSON 失败（输出预算耗尽/网关断流致参数残缺，GLM 两次死在 `{"cmd":` 处）不再裸抛 JSONDecodeError 炸任务——包成 `LLMError(truncated=True)`（含 stop_reason），agent 层据此整轮重试；**缺省 `max_tokens` 4096→16384**（原值与思考 budget 8192 倒挂）。`stream_transport` 可注入（测试伪 SSE） |
| `ark.py` | `ArkCodingProvider`：火山引擎 Ark 接入（**enable_thinking 默认 True**；smoke 实证 ark-code-latest/deepseek-v4-flash 默认即吐 thinking block） |
| `providers.py` | `ProviderStore`：读写 `config/providers.json`（多供应商，PUT 全量替换语义、空 api_key=沿用原 key、至少一个启用；**顶层 `default_provider` 可选显式指定全局默认供应商**——`save(..., default_provider=)` 须命中启用中的供应商否则 ProviderError、`default()` 优先取它/未设置兜底第一个启用、`default_provider_name()` 供 UI 下拉）；`probe_credentials` 探活（先 /v1/models，404/405 降级最小 chat 调用）；`ProviderError`。**`build()` 思考开关（2026-09-19）**：条目 `"thinking": true/false` 显式控制，缺省=base_url 含 "ark" 默认开、其余默认关；**每模型最大上下文（2026-09-19）**：条目 `"model_context": {"<模型名>": token 数}`（可选，`_validate` 剔除非法/未勾选项），`build()` 带给实例 `context_tokens`，会话层据此换算上下文预算（见 core/agent loop.apply_context_budget；未声明默认 256K） |
| `routing.py` | `ModelRouter`：planner/executor/classifier 模型路由与覆写；`load_dotenv()` 读 .env |

## 约定与坑

- 无可用 key 不崩：API Agent/编排端点返回 503；LLM 缺省 = 路由覆写目标或第一个启用供应商的第一个模型。
- Agent 会话内可经 `POST /agents/{sid}/llm` 运行时动态切换 provider 引用，下一次 chat 生效（落 llm.switched 事件）。
- 测试可注入 executor_llm/planner_llm 或用 tmp providers_config，避免种子污染真实 config/providers.json；禁止把任何 key 写进技能文件或提交物。
