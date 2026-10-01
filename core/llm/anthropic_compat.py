"""Anthropic Messages 协议 Provider 基类。

HTTP 传输层可注入（默认 urllib 标准库实现），单元测试用 fake transport，
不触网。子类只需固定 base_url / api_key / model（见 ark.py）。
"""

import json
import logging
import socket
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from core.llm.provider import (ContextOverflowError, LLMError, LLMResponse,
                               ToolCall, Usage)

log = logging.getLogger(__name__)

# 输入超限识别（2026-09-30）：网关因 prompt 过长拒收时的错误文案特征词。
# 命中则抛 ContextOverflowError（不可重试），由调用方强制压缩后兜底重试。
_OVERFLOW_HINTS = ("input length", "too long", "context length",
                   "maximum context", "prompt is too long", "exceeds",
                   "input too large", "request entity too large")

Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict[str, Any]]]
"""transport(url, headers, body_bytes) -> (http_status, parsed_json)"""

StreamTransport = Callable[[str, dict[str, str], bytes], tuple[int, Any]]
"""stream_transport(url, headers, body_bytes) -> (http_status, 响应流)
响应流 = SSE 逐行可迭代（字节行或 str 行均可，带 close 则用毕关闭）；
HTTP 非 200 时第二元为解析后的错误 dict（与非流式 transport 对齐）。"""

# 瞬时故障（网络超时 / 限流 / 网关抖动）自动重试：多会话长跑中一次抖动不该杀死整个编排
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 2026-09-28：原 3 次尝试（2 次重试）+ 30/60s backoff 会把坏窗口拖成十几分钟静默——
# 改为失败只重试 1 次（MAX_RETRIES=总尝试次数），错误快速暴露交人工判断
MAX_RETRIES = 2
# 2026-09-28：backoff 3→30——实测 ark 网关对大 max_tokens 请求有分钟级坏窗口（同分钟
# 小预算请求秒通、大预算挂起，坏窗口可持续 6 分钟+，实测 11:25-11:31 三连超时实例）。
# 原 3/6s 间隔三次尝试全落同一窗口；30/60s 让第②③次尝试有机会跨入恢复窗口。
RETRY_BACKOFF = 30.0


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
    """Anthropic /v1/messages 协议。超时默认 240s（2026-09-28：120→240——实测正常态
    tick 18s/慢态可达 ~126s（耗时随 max_tokens 预算线性增长），240s 吸收慢态避免
    120s 误杀；流式调用为帧间隔超时不受损，UI 测活单独传 30s 不受影响）。
    2026-09-28 全走流式：只要网关支持（未 _stream_disabled）所有调用都走 SSE——
    流式为帧间隔超时，长思考（planner/intel 无回调调用实测 6-10 分钟）不再触发
    非流式「总等待」超时；回调可选，不传只是不上屏。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 240.0,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
        api_version: str = "2023-06-01",
        enable_thinking: bool = False,
        enable_cache: bool = True,
        context_tokens: int | None = None,  # 模型最大上下文（providers.json model_context；预算换算用）
        ctx_soft_budget: int | None = None,  # 有效软上限（providers.json ctx_soft_budget；超此主动压缩）
        summarizer_model: str | None = None,  # 专用摘要模型（providers.json summarizer_model；缺省用本模型）
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.api_version = api_version
        self._enable_thinking = enable_thinking
        # prompt caching（2026-09-23 retrieval-upgrade M1）：system 块打
        # cache_control ephemeral 断点，稳定前缀跨步命中缓存（Agent 每步重发
        # 同一 system 是最大成本项）。缓存 token 仍计数只是更便宜，预算不动。
        self._enable_cache = enable_cache
        self.context_tokens = context_tokens
        # 上下文治理（2026-09-30 ct-7）：软上限与专用摘要模型由 provider 配置下发，
        # 供 ChatTurn 压缩上下文时读取；缺省走运行时模块常量/本模型。
        self.ctx_soft_budget = ctx_soft_budget
        self.summarizer_model = summarizer_model
        # 模型不支持 thinking 参数时自动降级（HTTP 400 去参重试后置位，本实例不再注入）
        self._thinking_disabled = False
        # 网关拒收 cache_control 标记（400 文案含 cache_control）时置位——本实例
        # system 不再打标（镜像 thinking/stream 降级先例）
        self._cache_disabled = False
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
        system: str | list[dict[str, Any] | str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        # max_tokens 缺省 16384（2026-09-20 事故修正，原 4096）：思考 budget 8192
        # 与 4096 倒挂（协议要求 budget < max_tokens），GLM 长思考+长工具参数把
        # 输出预算打爆 → 流截断在 {"cmd": 处 → worker 崩。上限非目标，不增成本
        max_tokens: int = 16384,
        temperature: float | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        model: str | None = None,  # 单次调用模型覆盖（摘要器专用模型；缺省用实例模型）
    ) -> LLMResponse:
        """on_thinking：SSE thinking_delta 逐帧回调（思考流式上屏，2026-09-19）；
        on_text：SSE text_delta 逐帧回调（回复流式，2026-09-20 对话窗）；
        should_cancel：逐帧间轮询，命中即掐断连接抛 LLMError。回调全缺省也走
        流式传输（2026-09-28 全走流式：帧间隔超时抗长思考），只是不逐帧上屏。
        system：str（自动包装单块打 cache_control，M1 默认路径）或块数组
        （str 元素自动包装不打标 / dict 原样——调用方精细控制断点位置）。"""
        body: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            body["system"] = self._system_payload(system)
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        if self._enable_thinking and not self._thinking_disabled:
            # 思考链路（2026-09-19 直播间终端化）：请求侧显式开启，模型吐 thinking block
            # → 落 llm.thinking 事件，前端展开看思考全文。budget 8192 平衡质量与耗时。
            body["thinking"] = {"type": "enabled", "budget_tokens": 8192}
        # 2026-09-28 全走流式（回调可选）：原「有回调才 stream」让 planner/intel 等
        # 无回调调用走非流式——服务端思考完才回响应头，240s 是总等待上限，长思考
        # （实测 6-10 分钟）必超时；重试= 原样重发同一请求注定再超时，纯浪费 5+ 分钟。
        # SSE 早回头 + 帧间隔超时天然抗长思考，故只要网关支持（未降级）就始终流式。
        use_stream = not self._stream_disabled
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
                    # 输入超限（2026-09-30）：prompt 过长被网关拒收（400/413），
                    # 原样重试注定再失败 → 抛 ContextOverflowError，调用方强制
                    # 压缩上下文后兜底重试（reactive 兜底；不再走可重试状态码分支）
                    if status in (400, 413):
                        low = (str(err.get("message", "")) + " "
                               + str(err.get("code", ""))).lower()
                        if any(h in low for h in _OVERFLOW_HINTS):
                            raise ContextOverflowError(
                                msg, status=status,
                                body=json.dumps(data, ensure_ascii=False)[:500])
                    # 思考参数优雅降级（2026-09-19）：模型/网关不认 thinking 参数（400）时
                    # 去参立即重试一次，并记实例标志后续不再注入——思考行不亮但不炸循环
                    if (status == 400 and self._enable_thinking
                            and not self._thinking_disabled
                            and "thinking" in str(err.get("message", "")).lower()
                            and isinstance(body.get("thinking"), dict)):
                        # 降级可见化（2026-09-30）：此前静默永久关闭，用户只看到
                        # 思考链消失无从排查——现在落到日志（终端/日志文件可见）
                        log.warning("思考链被网关拒收（HTTP 400: %s），本实例降级关闭——"
                                    "后续请求不再注入 thinking 参数",
                                    str(err.get("message", ""))[:120])
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
                    # cache_control 优雅降级（2026-09-23 M1）：网关拒收缓存标记
                    # （400 文案含 cache_control）→ 本实例 system 不再打标重试
                    if (status == 400 and not self._cache_disabled
                            and "cache_control" in str(err.get("message", "")).lower()):
                        self._cache_disabled = True
                        if system:
                            body["system"] = self._system_payload(system)
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
                    raise LLMError(f"网络错误（已重试 {MAX_RETRIES - 1} 次）: {e}") from e
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

    def _system_payload(self, system: str | list[dict[str, Any] | str]) -> list[dict[str, Any]]:
        """system 归一化为块数组（M1 prompt caching，2026-09-23）：str → 单
        text 块，enable_cache 且未降级时打 cache_control ephemeral 断点；list
        → str 元素包装为 text 块（不打标，断点位置由调用方 dict 元素自行声明），
        dict 元素浅拷贝透传。_cache_disabled（网关拒收降级）时剥除全部标记。
        每次调用新建块对象，不污染调用方传入的列表。"""
        if isinstance(system, str):
            block: dict[str, Any] = {"type": "text", "text": system}
            if self._enable_cache and not self._cache_disabled:
                block["cache_control"] = {"type": "ephemeral"}
            return [block]
        blocks = [dict(b) if isinstance(b, dict) else {"type": "text", "text": b}
                  for b in system]
        if self._cache_disabled:
            for b in blocks:
                b.pop("cache_control", None)
        return blocks

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
