import http.client
import json
import ssl
import time
from typing import Any

from core.llm import parsing
from core.llm.anthropic_compat import StreamTransport, Transport
from core.llm.provider import LLMResponse, ToolCall, Usage, LLMError
from core.llm.retry import HTTP_5XX_ATTEMPTS, HTTP_5XX_RETRIES, HTTP_5XX_STATUS
from core.llm.sdk_engine import RawFallback, build_openai_transports

CHAT_COMPLETIONS = "openai-chat-completions"
RESPONSES = "openai-responses"

# Cloudflare 520 表示边缘代理从源站拿到了未知/无效响应；524 表示已连上源站，
# 但源站在边缘超时窗口内没有返回结果。只对这两个明确的上游暂态状态重试，
# 避免把鉴权/参数错误重复发送。520 按首次请求失败后重试 5 次处理，
# 524 保持最多 5 次总请求；退避时间可由测试覆盖。
OPENAI_520_RETRIES = 5
OPENAI_520_ATTEMPTS = OPENAI_520_RETRIES + 1
OPENAI_520_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)
OPENAI_524_ATTEMPTS = 5
OPENAI_524_BACKOFF = (2.0, 4.0, 8.0, 16.0)
OPENAI_CONNECTION_ATTEMPTS = 5
OPENAI_CONNECTION_BACKOFF = (2.0, 4.0, 8.0, 16.0)
# 上游标准 5xx（500/502/503/504）：失败后重试 10 次，共 11 次尝试。
# 429 刻意不重试；520/524 继续使用各自的专用预算。
OPENAI_5XX_RETRYABLE = tuple(sorted(HTTP_5XX_STATUS))
OPENAI_5XX_ATTEMPTS = HTTP_5XX_ATTEMPTS
OPENAI_5XX_RETRIES = HTTP_5XX_RETRIES
OPENAI_5XX_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)


class OpenAICompatProvider:
    stream_capable = True

    def __init__(self, base_url: str, api_key: str, model: str, *, format: str = CHAT_COMPLETIONS,
                 timeout: float = 600.0, transport: Transport | None = None,
                 stream_transport: StreamTransport | None = None, enable_thinking: bool = False,
                 context_tokens: int | None = None, ctx_soft_budget: int | None = None,
                 summarizer_model: str | None = None, proxy: str | None = None):
        if format not in {CHAT_COMPLETIONS, RESPONSES}:
            raise ValueError(f"不支持的 OpenAI 格式: {format}")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.format = format
        self.timeout = timeout
        self._enable_thinking = enable_thinking
        self._thinking_disabled = False
        self.context_tokens = context_tokens
        self.ctx_soft_budget = ctx_soft_budget
        self.summarizer_model = summarizer_model
        self.proxy = proxy or None
        # 2026-10-03 SDK 迁移：默认传输层换成官方 openai SDK（sdk_engine 把 SDK 的
        # 流式事件还原成 SSE 行，_consume_stream 零改动）。注入的 transport/
        # stream_transport 优先且按需构建 SDK（两侧都注入则完全不碰 SDK）。
        self._transport = transport
        self._stream_transport = stream_transport

    def _sdk_transports(self) -> tuple[Transport, StreamTransport]:
        """按需构建 SDK 传输层（只在真正调用时建，且构建一次即缓存）。"""
        if self._transport is None or self._stream_transport is None:
            sdk_t, sdk_st = build_openai_transports(
                self.base_url, self.api_key, self.timeout, self.proxy)
            if self._transport is None:
                self._transport = sdk_t
            if self._stream_transport is None:
                self._stream_transport = sdk_st
        return self._transport, self._stream_transport

    def chat(self, messages: list[dict[str, Any]], *, system: str | list[dict[str, Any] | str] | None = None,
             tools: list[dict[str, Any]] | None = None, max_tokens: int = 16384,
             temperature: float | None = None, on_thinking=None, on_text=None,
             should_cancel=None, model: str | None = None, on_retry=None) -> LLMResponse:
        body = self._body(messages, system, tools, max_tokens, temperature, model or self.model)
        url = f"{self.base_url}/v1/{'chat/completions' if self.format == CHAT_COMPLETIONS else 'responses'}"
        _, stream_transport = self._sdk_transports()  # OpenAI 路径恒走流式

        def request_with_retries(payload: bytes):
            """重试上游明确的 520/524/5xx 或瞬时连接断开，其他错误立即返回。"""
            max_attempts = (OPENAI_520_ATTEMPTS + OPENAI_5XX_ATTEMPTS
                            + OPENAI_524_ATTEMPTS + OPENAI_CONNECTION_ATTEMPTS)
            status_5xx_used = 0
            for attempt in range(1, max_attempts + 1):
                try:
                    status, data = stream_transport(
                        url, self._headers(), payload)
                except (TimeoutError, ConnectionError) as exc:
                    if attempt >= OPENAI_CONNECTION_ATTEMPTS:
                        raise LLMError(
                            f"网络连接失败（已重试 {attempt - 1} 次）: {exc}") from exc
                    if on_retry is not None:
                        on_retry(attempt, OPENAI_CONNECTION_ATTEMPTS,
                                 "connection")
                    time.sleep(OPENAI_CONNECTION_BACKOFF[min(
                        attempt - 1, len(OPENAI_CONNECTION_BACKOFF) - 1)])
                    continue

                if status == 520:
                    if attempt >= OPENAI_520_ATTEMPTS:
                        return status, data
                    if on_retry is not None:
                        on_retry(attempt, OPENAI_520_RETRIES, 520)
                    time.sleep(OPENAI_520_BACKOFF[min(
                        attempt - 1, len(OPENAI_520_BACKOFF) - 1)])
                    continue

                if status in OPENAI_5XX_RETRYABLE:
                    status_5xx_used += 1
                    if status_5xx_used >= OPENAI_5XX_ATTEMPTS:
                        return status, data
                    if on_retry is not None:
                        on_retry(status_5xx_used, OPENAI_5XX_RETRIES, status)
                    time.sleep(OPENAI_5XX_BACKOFF[min(
                        status_5xx_used - 1, len(OPENAI_5XX_BACKOFF) - 1)])
                    continue

                if status != 524:
                    return status, data
                if attempt >= OPENAI_524_ATTEMPTS:
                    return status, data
                if on_retry is not None:
                    on_retry(attempt, OPENAI_524_ATTEMPTS, 524)
                time.sleep(OPENAI_524_BACKOFF[min(
                    attempt - 1, len(OPENAI_524_BACKOFF) - 1)])
            raise RuntimeError("unreachable")  # pragma: no cover

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        stream_attempt = 0
        while True:
            status, data = request_with_retries(payload)
            if status != 200:
                message = str((data or {}).get("error", {}).get("message", "请求失败"))
                if (self._enable_thinking and not self._thinking_disabled
                        and status == 400 and "reason" in message.lower()):
                    self._thinking_disabled = True
                    body.pop("reasoning_effort", None)
                    body.pop("reasoning", None)
                    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    continue
                raise LLMError(f"LLM 调用失败 HTTP {status}: {message}", status=status,
                               body=json.dumps(data or {}, ensure_ascii=False)[:500])
            if isinstance(data, dict):
                return self._parse(data)

            # urlopen 在收到响应头后，SSE 迭代期间的 socket 读取超时不会从
            # request_with_retries() 抛出；这里补上同一套有限重试。已经产生增量
            # 时不能重发，否则会把前半段内容重复推送给 UI。
            emitted = {"value": False}

            def _watch(callback):
                def _wrapped(delta):
                    if delta:
                        emitted["value"] = True
                    if callback is not None:
                        callback(delta)
                return _wrapped

            try:
                try:
                    return self._consume_stream(
                        data,
                        _watch(on_thinking),
                        _watch(on_text),
                        should_cancel,
                    )
                except RawFallback as fb:
                    # SDK 事件解析失败 / 零事件（网关 SSE 缺 event: 行、忽略
                    # stream:true 回整份 JSON）→ 用 tee 录制的原始字节重走自研
                    # 逐行 SSE 解析（含非 SSE 兜底）。已吐过增量则不能重放。
                    if emitted["value"]:
                        raise LLMError("SDK 事件解析失败且已有增量输出，无法重放") from fb
                    return self._consume_stream(
                        iter(fb.lines),
                        _watch(on_thinking),
                        _watch(on_text),
                        should_cancel,
                    )
            except (TimeoutError, ConnectionError,
                    http.client.IncompleteRead, ssl.SSLError) as exc:
                close = getattr(data, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
                if emitted["value"] or stream_attempt + 1 >= OPENAI_CONNECTION_ATTEMPTS:
                    if emitted["value"]:
                        raise LLMError(f"流式传输中断: {exc}") from exc
                    raise LLMError(
                        f"网络连接失败（已重试 {stream_attempt} 次）: {exc}") from exc
                stream_attempt += 1
                if on_retry is not None:
                    on_retry(stream_attempt, OPENAI_CONNECTION_ATTEMPTS, "stream")
                time.sleep(OPENAI_CONNECTION_BACKOFF[min(
                    stream_attempt - 1, len(OPENAI_CONNECTION_BACKOFF) - 1)])

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def _body(self, messages, system, tools, max_tokens, temperature, model):
        if self.format == CHAT_COMPLETIONS:
            body: dict[str, Any] = {"model": model, "messages": self._chat_messages(messages, system), "max_tokens": max_tokens, "stream": True}
            if tools:
                body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}} for t in tools]
            if temperature is not None:
                body["temperature"] = temperature
            if self._enable_thinking and not self._thinking_disabled:
                body["reasoning_effort"] = "high"
            return body
        body = {"model": model, "input": self._response_input(messages), "max_output_tokens": max_tokens, "stream": True}
        if system:
            body["instructions"] = self._system_text(system)
        if tools:
            body["tools"] = [{"type": "function", "name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})} for t in tools]
        if temperature is not None:
            body["temperature"] = temperature
        if self._enable_thinking and not self._thinking_disabled:
            body["reasoning"] = {"effort": "high"}
        return body

    def _system_text(self, system):
        if isinstance(system, str):
            return system
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)

    def _chat_messages(self, messages, system):
        out = []
        if system:
            out.append({"role": "system", "content": self._system_text(system)})
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                if any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
                    for b in content:
                        if b.get("type") == "tool_result":
                            out.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": b.get("content", "")})
                elif any(b.get("type") == "tool_use" for b in content if isinstance(b, dict)):
                    out.append({"role": "assistant", "content": next((b.get("text", "") for b in content if b.get("type") == "text"), None), "tool_calls": [
                        {"id": b.get("id", ""), "type": "function", "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}}
                        for b in content if b.get("type") == "tool_use"
                    ]})
                else:
                    out.append({"role": m.get("role", "user"), "content": self._system_text(content)})
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": content or None, "tool_calls": [
                    {"id": c.get("id", ""), "type": "function", "function": {
                        "name": c.get("name", ""), "arguments": json.dumps(c.get("input", {}), ensure_ascii=False)}}
                    for c in m["tool_calls"]
                ]})
            else:
                out.append(dict(m))
        return out

    def _response_input(self, messages):
        out = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "tool_result":
                        out.append({"type": "function_call_output", "call_id": b.get("tool_use_id", ""), "output": b.get("content", "")})
                    elif b.get("type") == "tool_use":
                        out.append({"type": "function_call", "call_id": b.get("id", ""), "name": b.get("name", ""), "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)})
                    elif b.get("type") == "text":
                        out.append({"role": m.get("role", "user"), "content": b.get("text", "")})
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                for call in m["tool_calls"]:
                    out.append({"type": "function_call", "call_id": call.get("id", ""), "name": call.get("name", ""), "arguments": json.dumps(call.get("input", {}), ensure_ascii=False)})
                if content:
                    out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": m.get("role", "user"), "content": content})
        return out

    def _consume_stream(self, stream, on_thinking, on_text, should_cancel):
        text = ""
        thinking = ""
        calls: dict[Any, dict[str, Any]] = {}
        call_aliases: dict[str, Any] = {}
        # Responses 网关的 SSE 实现并不总是把 function_call 的完整元数据
        # 放在 output_item.added：部分中转站只在 output_item.done 或
        # response.completed.response.output 中补齐 name/arguments。保留
        # item_id -> 内部调用记录的映射，后续事件到达时合并到同一条调用。
        def merge_call(item: dict[str, Any], *, item_id: str = "") -> dict[str, Any]:
            item_key = str(item.get("id") or item_id or "")
            call_id = str(item.get("call_id") or "")
            key = call_aliases.get(item_key) if item_key else None
            if key is None and call_id:
                key = call_aliases.get(call_id)
            if key is None:
                key = call_id or item_key
            if not key:
                raise LLMError("Responses function_call 缺少 id")
            c = calls.setdefault(key, {"id": call_id or item_key, "name": "", "arguments": ""})
            if call_id:
                c["id"] = call_id
                call_aliases[call_id] = key
            if item_key:
                call_aliases[item_key] = key
            if item.get("name"):
                c["name"] = str(item["name"])
            if item.get("arguments") is not None:
                args = item.get("arguments")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                if args:
                    c["arguments"] = str(args)
            return c

        usage = Usage()
        for raw in stream:
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            line = line.strip()
            if not line:
                continue
            # Responses SSE includes metadata frames such as
            # ``event: response.created`` before each JSON data frame.
            if line.startswith(("event:", "id:", "retry:", ":")):
                continue
            if not line.startswith("data:"):
                if not text and not thinking and not calls:
                    return self._parse(json.loads(line))
                continue
            value = line[5:].strip()
            if value == "[DONE]":
                break
            evt = json.loads(value)
            if should_cancel and should_cancel():
                raise LLMError("已中断")
            if self.format == CHAT_COMPLETIONS:
                for choice_position, choice in enumerate(evt.get("choices", [])):
                    choice_index = choice.get("index", choice_position)
                    d = choice.get("delta", {})
                    chunk = d.get("content") or ""
                    text += chunk
                    if chunk and on_text:
                        on_text(chunk)
                    rc = d.get("reasoning_content") or d.get("reasoning") or ""
                    thinking += rc
                    if rc and on_thinking:
                        on_thinking(rc)
                    for fallback_index, tc in enumerate(d.get("tool_calls", [])):
                        f = tc.get("function", {})
                        tc_index = tc.get("index", fallback_index)
                        key = ("chat", choice_index, tc_index)
                        call_id = str(tc.get("id") or "")
                        if call_id and key in calls and calls[key].get("id") not in ("", call_id):
                            raise LLMError("Chat Completions tool call id changed within index")
                        c = calls.setdefault(key, {"id": call_id, "name": f.get("name", ""), "arguments": ""})
                        if call_id:
                            c["id"] = call_id
                        c["name"] = c["name"] or f.get("name", "")
                        c["arguments"] += f.get("arguments", "")
            else:
                typ = evt.get("type", "")
                if typ == "response.output_text.delta":
                    chunk = evt.get("delta", "")
                    text += chunk
                    if on_text:
                        on_text(chunk)
                elif typ == "response.reasoning_summary_text.delta":
                    chunk = evt.get("delta", "")
                    thinking += chunk
                    if on_thinking:
                        on_thinking(chunk)
                elif typ == "response.output_item.added":
                    item = evt.get("item") or {}
                    if item.get("type") == "function_call":
                        call_id = str(item.get("call_id") or "")
                        if call_id and call_id in call_aliases:
                            raise LLMError(f"工具调用 id 重复: {call_id}")
                        merge_call(item)
                elif typ == "response.function_call_arguments.delta":
                    item_id = str(evt.get("item_id") or evt.get("call_id") or "")
                    c = merge_call({"call_id": evt.get("call_id"), "arguments": ""}, item_id=item_id)
                    c["arguments"] += evt.get("delta", "")
                elif typ == "response.function_call_arguments.done":
                    item_id = str(evt.get("item_id") or evt.get("call_id") or "")
                    c = merge_call({"call_id": evt.get("call_id"), "arguments": evt.get("arguments")}, item_id=item_id)
                    if evt.get("name"):
                        c["name"] = str(evt["name"])
                elif typ == "response.output_item.done":
                    item = evt.get("item") or {}
                    if item.get("type") == "function_call":
                        merge_call(item, item_id=str(evt.get("item_id") or ""))
                elif typ == "response.completed":
                    response = evt.get("response") or {}
                    u = response.get("usage") or {}
                    usage.input_tokens = u.get("input_tokens", 0)
                    usage.output_tokens = u.get("output_tokens", 0)
                    # 某些网关省略 output_item.done，只在最终 response.output
                    # 中返回完整 function_call；这里作为最后一道元数据补全。
                    for item in response.get("output", []) or []:
                        if item.get("type") == "function_call":
                            merge_call(item)
        result = LLMResponse(text=text, thinking=thinking, usage=usage)
        seen_ids: set[str] = set()
        for c in calls.values():
            if not c["id"]:
                raise LLMError("工具调用缺少 call id")
            if c["id"] in seen_ids:
                raise LLMError(f"工具调用 id 重复: {c['id']}")
            if not c["name"]:
                raise LLMError(
                    "工具调用缺少工具名 "
                    f"(call_id={c['id']})")
            seen_ids.add(c["id"])
            try:
                arguments = json.loads(c["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError(f"工具调用参数 JSON 无效 (call_id={c['id']})", truncated=True) from exc
            if not isinstance(arguments, dict):
                raise LLMError(f"工具调用参数必须是对象 (call_id={c['id']})")
            result.tool_calls.append(ToolCall(c["id"], c["name"], arguments))
        return result

    def _parse(self, data):
        """委托 core.llm.parsing（SDK 引擎与 compat 层共用同一解析口径）。"""
        return parsing.parse_openai_response(data, format=self.format)

    def tool_result_message(self, tool_call: ToolCall, content: str, is_error: bool = False):
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": content, **({"is_error": True} if is_error else {})}]}
