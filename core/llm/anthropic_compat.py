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
        api_version: str = "2023-06-01",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.api_version = api_version
        self._transport = transport or _default_transport(timeout)

    # ---------- 公共接口 ----------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
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
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                status, data = self._transport(
                    f"{self.base_url}/v1/messages", self._headers(), payload)
                if status != 200:
                    err = data.get("error", {})
                    msg = (f"LLM 调用失败 HTTP {status}: "
                           f"{err.get('code', '')} {err.get('message', '')[:300]}")
                    if status in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                        last_err = LLMError(msg, status=status,
                                            body=json.dumps(data, ensure_ascii=False)[:500])
                        time.sleep(RETRY_BACKOFF * attempt)
                        continue
                    raise LLMError(msg, status=status,
                                   body=json.dumps(data, ensure_ascii=False)[:500])
                return self._parse(data)
            except (TimeoutError, ConnectionError) as e:
                last_err = e
                if attempt >= MAX_RETRIES:
                    raise LLMError(f"网络错误（已重试 {MAX_RETRIES} 次）: {e}") from e
                time.sleep(RETRY_BACKOFF * attempt)
        raise last_err  # pragma: no cover — 循环内必 return 或 raise

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
