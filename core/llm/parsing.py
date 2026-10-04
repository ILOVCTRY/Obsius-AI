"""响应解析（Anthropic / OpenAI 双协议 → 内部 LLMResponse）。

从 anthropic_compat._parse / openai_compat._parse 提出为模块级纯函数，
供 compat 类（实例方法委托）与 SDK 引擎共用，避免解析逻辑双份。
只依赖 core.llm.provider，不反向依赖 compat 模块（防环）。
"""

import json
from typing import Any

from core.llm.provider import LLMError, LLMResponse, ToolCall, Usage

CHAT_COMPLETIONS = "openai-chat-completions"
RESPONSES = "openai-responses"


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
    return resp


def parse_openai_response(data: dict[str, Any], *, format: str) -> LLMResponse:
    """OpenAI Chat Completions / Responses 响应 → LLMResponse。

    function_call 缺工具名/调用 ID 时抛 LLMError（与旧实现一致，不静默吞）。"""
    if format == RESPONSES:
        text = "".join(
            x.get("text", "")
            for x in data.get("output", []) if x.get("type") == "message"
            for x in x.get("content", []) if x.get("type") == "output_text"
        )
        result = LLMResponse(text=text, raw=data, stop_reason=data.get("status", ""))
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
        return result
    choices = data.get("choices", [])
    msg = choices[0].get("message", {}) if choices else {}
    result = LLMResponse(
        text=msg.get("content", "") or "",
        thinking=msg.get("reasoning_content", "") or "",
        raw=data,
        stop_reason=(choices[0].get("finish_reason", "") if choices else ""),
        usage=Usage(
            input_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            output_tokens=data.get("usage", {}).get("completion_tokens", 0)),
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
    return result
