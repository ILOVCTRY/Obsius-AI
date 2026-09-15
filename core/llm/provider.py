"""LLM Provider 统一接口与数据类型（DESIGN.md §8）。

消息格式采用 Anthropic Messages 风格作为内部标准：
    {"role": "user"|"assistant", "content": str | [block, ...]}
block 类型：text / thinking / tool_use / tool_result。
未来接入 OpenAI 兼容厂商时由该 provider 负责双向翻译，Agent 层无感知。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class LLMResponse:
    text: str = ""                     # 拼接全部 text 块
    thinking: str = ""                 # 拼接全部 thinking 块（规划上下文用）
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""              # end_turn / tool_use / max_tokens ...
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)  # 原始响应，审计用


class LLMError(RuntimeError):
    """调用失败（HTTP 非 2xx / 响应不可解析）。携带 status 与响应片段。"""

    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class LLMProvider(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse: ...
