"""token 分层计数（tokenizer，2026-10-03）。

**为什么**：上下文预算此前用 `sum(len(json.dumps(m)))`（字符数）当近似——中英
混写下偏差可达 2-3 倍，同一个 `context_char_budget` 对英文会话偏松（该压不压、
真撞供应商上下文墙）、对中文会话偏紧（过早压缩丢细节）。改用按模型分层的计数：

| 层 | 适用 | 精度 |
|----|------|------|
| `TiktokenCounter` | OpenAI 系（模型名可映射到 encoding） | 真精确（BPE） |
| `HeuristicCounter` | Anthropic / Ark / GLM / DeepSeek（无公开 tokenizer） | 加权估算 |
| （两者皆不可用） | 任意 | 退化为字符数（现状口径，保证不抛） |

**口径约定（关键）**：本模块的 `count_*` 返回**等价字符数**——「该文本折算成
多少字符量级」——而非 token 数。理由：`AgentConfig.context_char_budget` /
`context_summary_chars` 的语义与取值（默认 120k/60k，`apply_context_budget` 按
`context_tokens×2` 换算）是既有契约，7 处测试断言钉死；把单位改成 token 会连带
改掉全部阈值语义。故实现为 `token 数 × 2`（沿用「CJK≈1 token/字、EN≈1 token/4
字符」的换算基准），**阈值一个字不动，计数从「字符数」升级为「按模型校准的
token 折算」**。

**缺失降级**：`tiktoken` 是可选依赖（`pyproject.toml` 的 `tokens` extra）；未装
时 OpenAI 系模型也走估算器，不抛不 503。估算器系数可由历史 `usage.input_tokens`
回归校准（见 `calibrate`），当前内置系数取中英混写实测中位数。
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

# token → 等价字符数的换算系数（与 apply_context_budget 的 tokens×2 同基准）
CHARS_PER_TOKEN = 2

# 每条消息 / 每个工具的固定开销（token）：role 标记、分隔符、协议样板。
# 取 Anthropic Messages 协议量级；数量级正确即可（相对正文占比很小）。
_PER_MESSAGE_TOKENS = 4
_PER_TOOL_TOKENS = 12

# CJK 表意文字 + 全角标点：近似 1 token/字（GPT/Claude 系 BPE 对 CJK 普遍 1-2 字/token）
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
# 其余非 ASCII（西里尔/阿拉伯/emoji 等）：按 2 字符/token 估
_WORDISH_RE = re.compile(r"[A-Za-z0-9_]+")


class TokenCounter(Protocol):
    """计数器协议：全部返回**等价字符数**（见模块文档）。"""

    def count_text(self, text: str) -> int: ...

    def count_messages(self, messages: list[dict[str, Any]], *,
                       system: Any = None, tools: Any = None) -> int: ...


class HeuristicCounter:
    """无公开 tokenizer 的模型（Anthropic / Ark / GLM / DeepSeek）用的加权估算。

    CJK 每字 1 token、ASCII 词按 4 字符 1 token（含词边界损耗）、其余非 ASCII
    按 2 字符 1 token；再乘 `CHARS_PER_TOKEN` 折算回等价字符数。
    """

    def __init__(self, *, cjk_per_token: float = 1.0, ascii_chars_per_token: float = 4.0,
                 other_chars_per_token: float = 2.0) -> None:
        self.cjk_per_token = cjk_per_token
        self.ascii_chars_per_token = ascii_chars_per_token
        self.other_chars_per_token = other_chars_per_token

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(_CJK_RE.findall(text))
        rest = _CJK_RE.sub("", text)
        ascii_chars = sum(len(m.group()) for m in _WORDISH_RE.finditer(rest))
        other_chars = max(0, len(rest) - ascii_chars)
        tokens = (cjk / self.cjk_per_token
                  + ascii_chars / self.ascii_chars_per_token
                  + other_chars / self.other_chars_per_token)
        return max(1, int(tokens + 0.5))

    def count_text(self, text: str) -> int:
        return self.count_tokens(text) * CHARS_PER_TOKEN

    def count_messages(self, messages: list[dict[str, Any]], *,
                       system: Any = None, tools: Any = None) -> int:
        tokens = sum(self.count_tokens(_as_text(m)) for m in messages)
        tokens += self.count_tokens(_as_text(system))
        if tools:
            tokens += len(tools) * _PER_TOOL_TOKENS
            tokens += self.count_tokens(_as_text(tools))
        tokens += len(messages) * _PER_MESSAGE_TOKENS
        return max(1, tokens) * CHARS_PER_TOKEN


class TiktokenCounter:
    """OpenAI 系模型：用 tiktoken 真 BPE 精确计数。"""

    def __init__(self, encoding: Any) -> None:
        self._enc = encoding

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(self._enc.encode(text, disallowed_special=()))

    def count_text(self, text: str) -> int:
        return self.count_tokens(text) * CHARS_PER_TOKEN

    def count_messages(self, messages: list[dict[str, Any]], *,
                       system: Any = None, tools: Any = None) -> int:
        tokens = sum(self.count_tokens(_as_text(m)) for m in messages)
        tokens += self.count_tokens(_as_text(system))
        if tools:
            tokens += len(tools) * _PER_TOOL_TOKENS
            tokens += self.count_tokens(_as_text(tools))
        tokens += len(messages) * _PER_MESSAGE_TOKENS
        return max(1, tokens) * CHARS_PER_TOKEN


class CharCounter:
    """兜底：等价字符数 == 字符数（迁移前的口径，保证任何情况下不抛）。"""

    def count_text(self, text: str) -> int:
        return len(text)

    def count_messages(self, messages: list[dict[str, Any]], *,
                       system: Any = None, tools: Any = None) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


# ---------------- 模型 → 计数器 ----------------

# 模型名子串 → tiktoken encoding（OpenAI 系）。未命中的一律走估算器。
_ENCODING_BY_PREFIX = (
    ("gpt-4o", "o200k_base"), ("gpt-4.1", "o200k_base"), ("gpt-5", "o200k_base"),
    ("o1", "o200k_base"), ("o3", "o200k_base"), ("o4", "o200k_base"),
    ("chatgpt-4o", "o200k_base"),
    ("gpt-4", "cl100k_base"), ("gpt-3.5", "cl100k_base"),
    ("text-embedding", "cl100k_base"),
)

_heuristic = HeuristicCounter()
_char = CharCounter()
_tiktoken_cache: dict[str, Any] = {}


def _tiktoken_encoding(model: str) -> Any:
    """按模型名取 tiktoken encoding；非 OpenAI 系/未装 tiktoken/取不到 → None。"""
    if model in _tiktoken_cache:
        return _tiktoken_cache[model]
    enc = None
    name = (model or "").lower()
    try:
        import tiktoken
    except ImportError:
        _tiktoken_cache[model] = None
        return None
    for prefix, encoding in _ENCODING_BY_PREFIX:
        if name.startswith(prefix):
            try:
                enc = tiktoken.get_encoding(encoding)
            except Exception:  # noqa: BLE001 —— 词表下载失败等，降级估算
                enc = None
            break
    else:
        try:  # 未命中前缀表：交给 tiktoken 自己按模型名猜（认不出会抛）
            enc = tiktoken.encoding_for_model(name)
        except Exception:  # noqa: BLE001
            enc = None
    _tiktoken_cache[model] = enc
    return enc


def get_counter(model: str | None) -> TokenCounter:
    """按模型名取计数器（分层）。任何异常都退回字符计数，绝不抛。"""
    try:
        enc = _tiktoken_encoding(model or "")
    except Exception:  # noqa: BLE001
        enc = None
    return TiktokenCounter(enc) if enc is not None else _heuristic


def calibrate(samples: list[tuple[str, int]]) -> float:
    """用历史 (文本, 实际 input_tokens) 样本回归估算器系数。

    返回「估算 token / 实际 token」的比值——1.0 表示无需调整。当前不做自动
    改写（系数是模块级常量），此函数供离线核查估算偏差用。
    """
    est = sum(_heuristic.count_tokens(t) for t, _ in samples)
    act = sum(n for _, n in samples)
    return (est / act) if act else 1.0


# ---------------- 内部：把消息结构摊平成文本 ----------------

def _as_text(obj: Any) -> str:
    """消息/工具结构 → 待计数文本。dict 的键不计（协议样板，已按条计固定开销），
    只取字符串与标量值——省掉键名重复计数，也更贴近真实 token 构成。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return "\n".join(_as_text(v) for v in obj)
    if isinstance(obj, dict):
        return "\n".join(_as_text(v) for v in obj.values())
    return str(obj)
