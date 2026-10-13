"""响应解析（Anthropic / OpenAI 双协议 → 内部 LLMResponse）。

从 anthropic_compat._parse / openai_compat._parse 提出为模块级纯函数，
供 compat 类（实例方法委托）与 SDK 引擎共用，避免解析逻辑双份。
只依赖 core.llm.provider，不反向依赖 compat 模块（防环）。
"""

import json
from typing import Any

from core.llm.provider import (
    LLMError,
    LLMResponse,
    ToolCall,
    Usage,
    collapse_reasoning_echo,
)

CHAT_COMPLETIONS = "openai-chat-completions"
RESPONSES = "openai-responses"


def openai_usage_to_usage(u: dict[str, Any], *, input_key: str, output_key: str,
                          details_key: str) -> Usage:
    """OpenAI 系 usage dict → 内部 Usage（2026-10-13 缓存命中率）。

    ⚠️ **语义相反**：OpenAI 的输入总量（Chat Completions 的 `prompt_tokens` /
    Responses 的 `input_tokens`）**含**缓存命中部分（`*_tokens_details.cached_tokens`），
    而内部/Anthropic 口径的 `input_tokens` **不含**——故此处**相减**得未缓存部分，
    命中部分落 `cache_read_tokens`。否则消费方 `input_total = input + cache_read
    + cache_creation` 会把命中 token 算两遍（命中率失真）。"""
    total_in = int(u.get(input_key, 0) or 0)
    cached = int((u.get(details_key) or {}).get("cached_tokens", 0) or 0)
    return Usage(
        input_tokens=max(0, total_in - cached),
        output_tokens=int(u.get(output_key, 0) or 0),
        cache_read_tokens=cached,
    )


def parse_anthropic_response(data: dict[str, Any]) -> LLMResponse:
    """Anthropic /v1/messages 响应 → LLMResponse。

    content 块：text/thinking/tool_use 参与拼接；redacted_thinking 等未知块留在 raw。
    无 thinking block 时兜底接 OpenAI 式顶层 reasoning_content（部分网关泄漏）。"""
    resp = LLMResponse(
        stop_reason=data.get("stop_reason", ""),
        raw=data,
        usage=Usage(
            input_tokens=data.get("usage", {}).get("input_tokens", 0),
            output_tokens=data.get("usage", {}).get("output_tokens", 0),
            cache_read_tokens=data.get("usage", {}).get("cache_read_input_tokens", 0),
            cache_creation_tokens=data.get("usage", {}).get("cache_creation_input_tokens", 0),
        ),
    )
    texts: list[str] = []
    thinkings: list[str] = []
    for block in data.get("content", []):
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "thinking":
            thinkings.append(block.get("thinking", ""))
        elif btype == "tool_use":
            resp.tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}) or {},
                )
            )
    resp.text = "".join(texts)
    resp.thinking = "\n".join(thinkings)
    if not resp.thinking:
        rc = data.get("reasoning_content")
        if isinstance(rc, str) and rc.strip():
            resp.thinking = rc
    return collapse_reasoning_echo(resp)


def parse_openai_response(data: dict[str, Any], *, format: str) -> LLMResponse:
    """OpenAI Chat Completions / Responses 响应 → LLMResponse。

    function_call 缺工具名/调用 ID 时抛 LLMError（与旧实现一致，不静默吞）。"""
    if format == RESPONSES:
        text = "".join(
            x.get("text", "")
            for x in data.get("output", []) if x.get("type") == "message"
            for x in x.get("content", []) if x.get("type") == "output_text"
        )
        result = LLMResponse(
            text=text, raw=data, stop_reason=data.get("status", ""),
            usage=openai_usage_to_usage(
                data.get("usage") or {}, input_key="input_tokens",
                output_key="output_tokens", details_key="input_tokens_details"))
        seen_ids: set[str] = set()
        for item in data.get("output", []):
            if item.get("type") == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                if not call_id or not name:
                    raise LLMError(
                        "Responses function_call 缺少工具名或调用 ID "
                        f"(call_id={call_id or 'unknown'})")
                if call_id in seen_ids:
                    raise LLMError(f"工具调用 id 重复: {call_id}")
                try:
                    arguments = json.loads(item.get("arguments", "{}"))
                except json.JSONDecodeError as exc:
                    raise LLMError(f"工具调用参数 JSON 无效 (call_id={call_id})", truncated=True) from exc
                if not isinstance(arguments, dict):
                    raise LLMError(f"工具调用参数必须是对象 (call_id={call_id})")
                seen_ids.add(call_id)
                result.tool_calls.append(ToolCall(call_id, name, arguments))
        return collapse_reasoning_echo(result)
    choices = data.get("choices", [])
    msg = choices[0].get("message", {}) if choices else {}
    result = LLMResponse(
        text=msg.get("content", "") or "",
        thinking=msg.get("reasoning_content", "") or "",
        raw=data,
        stop_reason=(choices[0].get("finish_reason", "") if choices else ""),
        usage=openai_usage_to_usage(
            data.get("usage") or {}, input_key="prompt_tokens",
            output_key="completion_tokens", details_key="prompt_tokens_details"),
    )
    seen_ids: set[str] = set()
    for tc in msg.get("tool_calls", []):
        f = tc.get("function", {})
        call_id = str(tc.get("id") or "").strip()
        name = str(f.get("name") or "").strip()
        if not call_id or not name:
            raise LLMError(
                "Chat Completions tool_call 缺少工具名或调用 ID "
                f"(call_id={call_id or 'unknown'})")
        if call_id in seen_ids:
            raise LLMError(f"工具调用 id 重复: {call_id}")
        try:
            arguments = json.loads(f.get("arguments", "{}"))
        except json.JSONDecodeError as exc:
            raise LLMError(f"工具调用参数 JSON 无效 (call_id={call_id})", truncated=True) from exc
        if not isinstance(arguments, dict):
            raise LLMError(f"工具调用参数必须是对象 (call_id={call_id})")
        seen_ids.add(call_id)
        result.tool_calls.append(ToolCall(call_id, name, arguments))
    return collapse_reasoning_echo(result)
