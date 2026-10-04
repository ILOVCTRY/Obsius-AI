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


def assistant_message(response: LLMResponse) -> dict[str, Any]:
    """把 provider 无关的 typed 响应重建为内部 assistant 消息。

    ``LLMResponse.raw`` 保持供应商原始协议，调用方不得从 raw 猜 content。
    """
    blocks: list[dict[str, Any]] = []
    if response.thinking:
        blocks.append({"type": "thinking", "thinking": response.thinking})
    if response.text:
        blocks.append({"type": "text", "text": response.text})
    blocks.extend({"type": "tool_use", "id": call.id, "name": call.name,
                   "input": call.arguments} for call in response.tool_calls)
    return {"role": "assistant", "content": blocks}


def tool_results_message(results: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一 assistant 回合的多个工具结果聚合为一个 user 消息。"""
    return {"role": "user", "content": results}


class LLMError(RuntimeError):
    """调用失败（HTTP 非 2xx / 响应不可解析）。携带 status 与响应片段。
    truncated=True 表示流式响应中途截断（工具参数 JSON 残缺等）——可整轮重试。"""

    def __init__(self, message: str, status: int = 0, body: str = "",
                 truncated: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.truncated = truncated


class ContextOverflowError(LLMError):
    """输入超限（网关因 prompt 过长返回 400/413）——不可通过原样重试解决。
    调用方据此触发「强制压缩上下文后重试一次」（reactive 兜底）。"""


class LLMProvider(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 16384,
        temperature: float | None = None,
        on_thinking: Any = None,      # SSE thinking_delta 回调（流式实现可选支持）
        on_text: Any = None,          # SSE text_delta 回调（回复流式，2026-09-20；可选支持）
        should_cancel: Any = None,    # 逐帧轮询取消探测（流式实现可选支持）
    ) -> LLMResponse: ...
