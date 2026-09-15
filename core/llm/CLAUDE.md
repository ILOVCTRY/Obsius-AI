# core/llm/

> LLM 接入层（DESIGN.md §8）：内部统一 Anthropic `/v1/messages` 标准，火山引擎 Ark 优先，多供应商可配。

## 文件

| 文件 | 职责 |
|------|------|
| `provider.py` | 抽象基类 + 数据类 `LLMResponse/ToolCall/Usage`、`LLMError`；`chat(messages, system, tools, max_tokens, temperature)` 为唯一调用面 |
| `anthropic_compat.py` | `AnthropicCompatProvider`：OpenAI/Anthropic 兼容协议适配（tools、tool_use、thinking 解析）；测试用 ScriptedLLM 继承它回放剧本；传输层对 429/5xx/超时重试 3 次 |
| `ark.py` | `ArkCodingProvider`：火山引擎 Ark 接入 |
| `providers.py` | `ProviderStore`：读写 `config/providers.json`（多供应商，PUT 全量替换语义、空 api_key=沿用原 key、至少一个启用）；`probe_credentials` 探活（先 /v1/models，404/405 降级最小 chat 调用）；`ProviderError` |
| `routing.py` | `ModelRouter`：planner/executor/classifier 模型路由与覆写；`load_dotenv()` 读 .env |

## 约定与坑

- 无可用 key 不崩：API Agent/编排端点返回 503；LLM 缺省 = 路由覆写目标或第一个启用供应商的第一个模型。
- Agent 会话内可经 `POST /agents/{sid}/llm` 运行时动态切换 provider 引用，下一次 chat 生效（落 llm.switched 事件）。
- 测试可注入 executor_llm/planner_llm 或用 tmp providers_config，避免种子污染真实 config/providers.json；禁止把任何 key 写进技能文件或提交物。
