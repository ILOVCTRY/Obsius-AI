"""Anthropic Messages 协议 Provider 基类。

HTTP 传输层可注入（默认 urllib 标准库实现），单元测试用 fake transport，
不触网。子类只需固定 base_url / api_key / model（见 ark.py）。
"""

import json
import socket
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from core.llm.provider import LLMError, LLMResponse, ToolCall, Usage

Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict[str, Any]]]
"""transport(url, headers, body_bytes) -> (http_status, parsed_json)"""

StreamTransport = Callable[[str, dict[str, str], bytes], tuple[int, Any]]
"""stream_transport(url, headers, body_bytes) -> (http_status, 响应流)
响应流 = SSE 逐行可迭代（字节行或 str 行均可，带 close 则用毕关闭）；
HTTP 非 200 时第二元为解析后的错误 dict（与非流式 transport 对齐）。"""

# 瞬时故障（网络超时 / 限流 / 网关抖动）自动重试：多会话长跑中一次抖动不该杀死整个编排
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF = 3.0


def _default_transport(timeout: float) -> Transport:
    def _transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": {"message": raw[:500]}}
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError(str(e.reason)) from e
            raise LLMError(f"网络错误: {e.reason}") from e

    return _transport


def _default_stream_transport(timeout: float) -> StreamTransport:
    """SSE 流式默认实现：urlopen 后不整读，交给调用方逐行迭代（思考增量边到边回调）。"""
    def _transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, Any]:
        req = urllib.request.Request(url, data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": {"message": raw[:500]}}
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError(str(e.reason)) from e
            raise LLMError(f"网络错误: {e.reason}") from e
        return resp.status, resp
    return _transport


class AnthropicCompatProvider:
    """Anthropic /v1/messages 协议。超时默认 120s（Agent 工具循环单步较重）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 120.0,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
        api_version: str = "2023-06-01",
        enable_thinking: bool = False,
        context_tokens: int | None = None,  # 模型最大上下文（providers.json model_context；预算换算用）
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.api_version = api_version
        self._enable_thinking = enable_thinking
        self.context_tokens = context_tokens
        # 模型不支持 thinking 参数时自动降级（HTTP 400 去参重试后置位，本实例不再注入）
        self._thinking_disabled = False
        self._transport = transport or _default_transport(timeout)
        self._stream_transport = stream_transport or _default_stream_transport(timeout)
        # 网关不认 stream 参数（400 文案含 stream）时置位，本实例回退非流式
        self._stream_disabled = False

    stream_capable = True
    """Agent 层据此决定是否传 on_thinking/should_cancel（duck-type 能力探测）。"""

    # ---------- 公共接口 ----------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        # max_tokens 缺省 16384（2026-09-20 事故修正，原 4096）：思考 budget 8192
        # 与 4096 倒挂（协议要求 budget < max_tokens），GLM 长思考+长工具参数把
        # 输出预算打爆 → 流截断在 {"cmd": 处 → worker 崩。上限非目标，不增成本
        max_tokens: int = 16384,
        temperature: float | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> LLMResponse:
        """on_thinking：SSE thinking_delta 逐帧回调（思考流式上屏，2026-09-19）；
        on_text：SSE text_delta 逐帧回调（回复流式，2026-09-20 对话窗）；
        should_cancel：逐帧间轮询，命中即掐断连接抛 LLMError——全缺省走原
        非流式路径。"""
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if self._enable_thinking and not self._thinking_disabled:
            # 思考链路（2026-09-19 直播间终端化）：请求侧显式开启，模型吐 thinking block
            # → 落 llm.thinking 事件，前端展开看思考全文。budget 8192 平衡质量与耗时。
            body["thinking"] = {"type": "enabled", "budget_tokens": 8192}
        use_stream = (on_thinking is not None or on_text is not None) \
            and not self._stream_disabled
        if use_stream:
            body["stream"] = True
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if use_stream:
                    status, data = self._stream_transport(
                        f"{self.base_url}/v1/messages", self._headers(), payload)
                else:
                    status, data = self._transport(
                        f"{self.base_url}/v1/messages", self._headers(), payload)
                if status != 200:
                    err = data.get("error", {})
                    msg = (f"LLM 调用失败 HTTP {status}: "
                           f"{err.get('code', '')} {err.get('message', '')[:300]}")
                    # 思考参数优雅降级（2026-09-19）：模型/网关不认 thinking 参数（400）时
                    # 去参立即重试一次，并记实例标志后续不再注入——思考行不亮但不炸循环
                    if (status == 400 and self._enable_thinking
                            and not self._thinking_disabled
                            and "thinking" in str(err.get("message", "")).lower()
                            and isinstance(body.get("thinking"), dict)):
                        self._thinking_disabled = True
                        body.pop("thinking")
                        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                        continue
                    # stream 参数优雅降级：网关不认（400 文案含 stream）→ 本实例回退非流式
                    if (status == 400 and use_stream
                            and "stream" in str(err.get("message", "")).lower()):
                        self._stream_disabled = True
                        use_stream = False
                        body.pop("stream", None)
                        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                        continue
                    if status in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        last_err = LLMError(msg, status=status,
                                            body=json.dumps(data, ensure_ascii=False)[:500])
                        time.sleep(RETRY_BACKOFF * attempt)
                        continue
                    raise LLMError(msg, status=status,
                                   body=json.dumps(data, ensure_ascii=False)[:500])
                if use_stream:
                    # 流已开建连成功：中途任何错误不再重试（流不可重放，重试会重复回调
                    # on_thinking）——网络错误包成 LLMError 直接抛
                    try:
                        return self._consume_stream(data, on_thinking, should_cancel,
                                                    on_text)
                    except (TimeoutError, ConnectionError) as e:
                        raise LLMError(f"流式传输中断: {e}") from e
                return self._parse(data)
            except (TimeoutError, ConnectionError) as e:
                last_err = e
                if attempt >= MAX_RETRIES:
                    raise LLMError(f"网络错误（已重试 {MAX_RETRIES} 次）: {e}") from e
                time.sleep(RETRY_BACKOFF * attempt)
        raise last_err  # pragma: no cover — 循环内必 return 或 raise

    def _consume_stream(
        self,
        stream: Any,
        on_thinking: Callable[[str], None] | None,
        should_cancel: Callable[[], bool] | None,
        on_text: Callable[[str], None] | None = None,
    ) -> LLMResponse:
        """消费 SSE 流：thinking_delta 逐帧回调 on_thinking；帧拼回与非流式同形的
        dict 后复用 _parse（tool_calls/usage 全兼容）。网关无视 stream 参数返回整份
        JSON 时兜底按普通响应解析。should_cancel 逐行轮询，命中即关流抛 LLMError。"""
        blocks: dict[int, dict[str, Any]] = {}  # index -> content block 累积
        stop_reason = ""
        usage: dict[str, int] = {}
        try:
            first = True
            for raw in stream:
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) \
                    else str(raw)
                line = line.strip()
                if not line:
                    continue
                if first and not (line.startswith("data:") or line.startswith("event:")):
                    # 非 SSE：网关忽略了 stream:true，整份 JSON 兜底
                    rest = [line] + [
                        r.decode("utf-8", errors="replace") if isinstance(r, (bytes, bytearray)) else str(r)
                        for r in stream]
                    return self._parse(json.loads("".join(rest)))
                first = False
                if should_cancel is not None and should_cancel():
                    raise LLMError("已中断")
                if not line.startswith("data:"):
                    continue  # event:/注释/空行——payload 自带 type，只认 data 行
                evt = json.loads(line[5:].strip())
                t = evt.get("type")
                if t == "message_start":
                    usage.update(evt.get("message", {}).get("usage", {}) or {})
                elif t == "content_block_start":
                    blocks[evt.get("index", 0)] = dict(evt.get("content_block") or {})
                elif t == "content_block_delta":
                    b = blocks.setdefault(evt.get("index", 0), {})
                    d = evt.get("delta") or {}
                    if d.get("type") == "thinking_delta":
                        chunk = d.get("thinking", "")
                        b["thinking"] = b.get("thinking", "") + chunk
                        if on_thinking is not None:
                            on_thinking(chunk)
                    elif d.get("type") == "text_delta":
                        b["text"] = b.get("text", "") + d.get("text", "")
                        if on_text is not None:
                            on_text(d.get("text", ""))
                    elif d.get("type") == "input_json_delta":
                        b["_json"] = b.get("_json", "") + d.get("partial_json", "")
                elif t == "message_delta":
                    d = evt.get("delta") or {}
                    stop_reason = d.get("stop_reason") or stop_reason
                    usage.update(evt.get("usage") or {})
                # message_stop / ping：无需处理
            for b in blocks.values():  # tool_use 增量 JSON 拼装回 input dict
                if "_json" in b:
                    raw = b.pop("_json") or "{}"
                    try:
                        b["input"] = json.loads(raw)
                    except json.JSONDecodeError as e:
                        # 流截断防御（2026-09-20 事故修复）：输出预算耗尽
                        # （stop_reason=max_tokens）或网关优雅断流时工具参数残缺，
                        # 裸 JSONDecodeError 穿到 agent 循环会直接 fail 任务
                        # （GLM 两次死在 {"cmd": 处）——包成带截断标记的
                        # LLMError 供上层整轮重试
                        raise LLMError(
                            f"工具参数流截断（stop_reason={stop_reason or '未知'}，"
                            f"已收 {len(raw)} 字符）: {e}",
                            truncated=True, body=raw[:200]) from e
            data = {
                "stop_reason": stop_reason,
                "content": [blocks[i] for i in sorted(blocks)],
                "usage": usage,
            }
            return self._parse(data)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 —— 关流失败不影响已组装结果
                    pass

    # ---------- 内部 ----------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": self.api_version,
        }

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
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
            # redacted_thinking 等未知块：留在 raw，不参与拼接
        resp.text = "".join(texts)
        resp.thinking = "\n".join(thinkings)
        if not resp.thinking:
            # OpenAI 式 reasoning_content 兜底（2026-09-19）：部分网关按 OpenAI 风格
            # 泄漏思考字段，Anthropic content 里没有 thinking block 时接住它
            rc = data.get("reasoning_content")
            if isinstance(rc, str) and rc.strip():
                resp.thinking = rc
        return resp

    def tool_result_message(self, tool_call: ToolCall, content: str, is_error: bool = False) -> dict:
        """构造工具结果消息（Agent 循环回填用）。"""
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": content,
                    **({"is_error": True} if is_error else {}),
                }
            ],
        }
