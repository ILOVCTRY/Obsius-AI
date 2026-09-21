"""火山引擎 Ark（coding plan）Provider —— 第一公民（DESIGN.md §8）。

端点为 Anthropic 兼容协议：https://ark.cn-beijing.volces.com/api/coding
（实证：/api/coding/v1/messages，Bearer / x-api-key 均可，见 scripts/smoke_ark.py）。
"""

import os
from pathlib import Path

from core.llm.anthropic_compat import AnthropicCompatProvider
from core.llm.routing import load_dotenv

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding"
DEFAULT_EXECUTOR_MODEL = "ark-code-latest"
DEFAULT_CLASSIFIER_MODEL = "deepseek-v4-flash"


def resolve_api_key(explicit: str | None = None, env_file: str | Path = ".env") -> str:
    """密钥解析顺序：显式参数 > 环境变量 ARK_API_KEY > 项目 .env 文件。"""
    if explicit:
        return explicit
    key = os.environ.get("ARK_API_KEY")
    if key:
        return key
    for k, v in load_dotenv(env_file).items():
        if k == "ARK_API_KEY" and v:
            return v
    raise ValueError(
        "未找到 Ark API Key：请设置环境变量 ARK_API_KEY 或在项目根 .env 中配置"
    )


class ArkCodingProvider(AnthropicCompatProvider):
    """coding plan 入口。executor 主循环默认 ark-code-latest（glm-5-3-flash 底座）。"""

    def __init__(
        self,
        model: str = DEFAULT_EXECUTOR_MODEL,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        enable_thinking: bool = True,  # 思考链路默认开（不支持时基类 400 去参降级）
        **kwargs,
    ):
        super().__init__(
            base_url=base_url,
            api_key=resolve_api_key(api_key),
            model=model,
            enable_thinking=enable_thinking,
            **kwargs,
        )
