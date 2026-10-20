"""LLM Provider 统一接口与数据类型（DESIGN.md §8）。

消息格式采用 Anthropic Messages 风格作为内部标准：
    {"role": "user"|"assistant", "content": str | [block, ...]}
block 类型：text / thinking / tool_use / tool_result / image。
未来接入 OpenAI 兼容厂商时由该 provider 负责双向翻译，Agent 层无感知。

**image 块（vision 通道，2026-10-19）**：内部统一用 Anthropic 形态
``{"type": "image", "source": {"type": "base64", "media_type": "image/png",
"data": "<b64>"}}``，各 provider 自行翻译成自家协议（OpenAI chat-completions
的 ``image_url`` / Responses 的 ``input_image``）。**只走 base64 内联**，不用
URL 引用：CTF 附件全在本地工作区，没有可公网访问的 URL，且中转站普遍不回源。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


def image_block(data: str, media_type: str = "image/png") -> dict[str, Any]:
    """内部标准 image 块（Anthropic 形态）。``data`` 是 **base64 正文**，不含
    ``data:`` 前缀——前缀由各 provider 按自家协议拼，避免两处重复编码。"""
    return {"type": "image",
            "source": {"type": "base64",
                       "media_type": media_type or "image/png",
                       "data": data}}


def image_data_url(block: dict[str, Any]) -> str:
    """内部 image 块 → ``data:<mime>;base64,<正文>``（OpenAI 系两种格式都要它）。"""
    src = block.get("source") or {}
    media = str(src.get("media_type") or "image/png")
    return f"data:{media};base64,{src.get('data') or ''}"


class ToolOutput(str):
    """工具结果：**文本即值**，附带图片（vision 通道，2026-10-19）。

    为什么继承 ``str`` 而不是包一层 dataclass：全仓有 20+ 处对工具结果做
    ``startswith("[错误]")`` / ``len()`` / ``[:400]`` / ``in`` 判定（失败前缀
    分类、spill 阈值、审计截断、DB 落库…）。继承 str 让这些判定**零改动**继续
    正确工作，只有真正需要图片的三处组装点（两个 provider 的
    ``tool_result_message`` 与 chat runtime 的 tool_result 块）显式读 ``.images``。

    切片/拼接会退化成普通 str（丢 images）——这是刻意的：那些路径都是审计
    与截断，本来就不该带图。
    """

    images: list[dict[str, Any]]

    def __new__(cls, text: str = "", images: list[dict[str, Any]] | None = None):
        obj = super().__new__(cls, text)
        obj.images = list(images or [])
        return obj

    @property
    def has_images(self) -> bool:
        return bool(self.images)


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


# 推理回声判定的后缀长度下限：短答复恰好等于思考尾部属正常（如思考以「已完成。」
# 收尾、回复也是「已完成。」），不判；长文本互为后缀则只能是上游泄漏。
_REASONING_ECHO_SUFFIX_MIN = 200


def is_reasoning_echo(text: str, thinking: str) -> bool:
    """内容通道是否只是思考通道的复制（上游把推理灌进了 content）。

    判据（去首尾空白后）：逐字相同，或 text 长度 ≥ ``_REASONING_ECHO_SUFFIX_MIN``
    且是 thinking 的结尾段。真答复与思考逐字/长段同尾的概率可忽略。

    背景（2026-10-09 事故）：第三方中转（responses 协议）在单个响应里把模型的
    推理正文同时经 output_text 与 reasoning_summary 两条通道下发，实测
    ``resp.text == resp.thinking``（52545 字符逐字相同）。对话链把非空 text 当
    纯文本终稿收轮——于是「推理被当成答案落库上屏」，轮次也就此提前终止。
    """
    t = (text or "").strip()
    k = (thinking or "").strip()
    if not t or not k:
        return False
    return t == k or (len(t) >= _REASONING_ECHO_SUFFIX_MIN and k.endswith(t))


def collapse_reasoning_echo(response: LLMResponse) -> LLMResponse:
    """把「只是思考复制品」的 content 清空，只保留 thinking（展示用）。

    在解析层收口调用（``parsing.parse_*`` 与 ``openai_compat._consume_stream``），
    使下游（对话链/Agent 循环）看到的是「无文本响应」而不是「一段推理正文」——
    于是走空响应 nudge 重来，而不是把它当终稿收轮。原样返回便于链式调用。
    """
    if is_reasoning_echo(response.text, response.thinking):
        response.text = ""
    return response


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


def tool_result_content(content: Any) -> str | list[dict[str, Any]]:
    """工具结果 → ``tool_result.content`` 取值（Anthropic 形态：str 或块数组）。

    无图时**原样返回 str**（历史行为一字不动，网关兼容面最大）；有图时返回
    ``[text 块, image 块…]`` —— Anthropic 协议原生支持，OpenAI 系两种格式由
    各自 provider 翻译（见 ``openai_compat._chat_messages`` /
    ``_response_input``）。
    """
    images = list(getattr(content, "images", None) or [])
    text = str(content)
    if not images:
        return text
    return [{"type": "text", "text": text}, *images]


class LLMError(RuntimeError):
    """调用失败（HTTP 非 2xx / 响应不可解析）。携带 status 与响应片段。
    truncated=True 表示流式响应中途截断（工具参数 JSON 残缺等）——可整轮重试。
    partial=True 表示流式响应**已吐出增量后**连接中断（半截内容已上屏，流不可
    重放）——调用方据此走「接着续写」而非整轮失败（2026-10-08）。"""

    def __init__(self, message: str, status: int = 0, body: str = "",
                 truncated: bool = False, partial: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.truncated = truncated
        self.partial = partial


class ContextOverflowError(LLMError):
    """输入超限（网关因 prompt 过长返回 400/413）——不可通过原样重试解决。
    调用方据此触发「强制压缩上下文后重试一次」（reactive 兜底）。"""


class TransientStreamError(ConnectionError):
    """流内瞬时错误（HTTP 200 已建连，SSE 里发 {"error": ...} 且属瞬时类）。

    继承 `ConnectionError` 是为了让既有「流已建连、未吐增量则安全重试」的连接
    重试分支原样接住它（compat 层零改动）；错误分类器据类型/文案归「上游服务
    不可用」而非「网络中断」。仅瞬时类（upstream / service unavailable /
    overloaded / 5xx 网关文案）才转成本类型，内容策略等硬错误仍原样上抛。"""


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
