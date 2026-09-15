"""core.llm —— LLM 接入层（DESIGN.md §8）。

对外入口：
    from core.llm import ArkCodingProvider, ModelRouter
"""

from core.llm.anthropic_compat import AnthropicCompatProvider
from core.llm.ark import ArkCodingProvider
from core.llm.provider import LLMError, LLMResponse, ToolCall, Usage
from core.llm.providers import ProviderError, ProviderStore, probe_credentials
from core.llm.routing import ModelRouter

__all__ = [
    "AnthropicCompatProvider",
    "ArkCodingProvider",
    "LLMError",
    "LLMResponse",
    "ToolCall",
    "Usage",
    "ModelRouter",
    "ProviderError",
    "ProviderStore",
    "probe_credentials",
]
