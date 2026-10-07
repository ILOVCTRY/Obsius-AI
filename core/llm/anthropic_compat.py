"""Anthropic Messages 协议 Provider 基类。

HTTP 传输层默认由官方 anthropic SDK 提供（`core/llm/sdk_engine`：SDK 负责连接/
代理/UA/HTTP 状态分类/超时/SSE 解析，httpx 垫片旁路录制原始字节），也可注入
fake transport（单元测试不触网）。子类只需固定 base_url / api_key / model（见 ark.py）。
"""

import http.client
import json
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any

from core.llm import parsing
from core.llm.provider import ContextOverflowError, LLMError, LLMResponse, ToolCall
from core.llm.retry import HTTP_5XX_ATTEMPTS, HTTP_5XX_BACKOFF, HTTP_5XX_RETRIES, HTTP_5XX_STATUS
from core.llm.sdk_engine import RawFallback, build_anthropic_transports

log = logging.getLogger(__name__)

# Some OpenAI-compatible gateways reject Python's default
# ``Python-urllib/...`` signature at the edge (Cloudflare error 1010).
CLIENT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

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

# 429 保持原有快速失败预算；标准上游 5xx 使用共享的 10 次尝试预算 + 封顶 30s 退避。
RETRYABLE_STATUS = {429, *HTTP_5XX_STATUS}
RATE_LIMIT_ATTEMPTS = 2
MAX_RETRIES = RATE_LIMIT_ATTEMPTS  # 兼容旧测试/调用方：429 总尝试次数
# 2026-09-28：backoff 3→30——实测 ark 网关对大 max_tokens 请求有分钟级坏窗口（同分钟
# 小预算请求秒通、大预算挂起，坏窗口可持续 6 分钟+，实测 11:25-11:31 三连超时实例）。
# 原 3/6s 间隔三次尝试全落同一窗口；30/60s 让第②③次尝试有机会跨入恢复窗口。
# 2026-10-07：本常量现仅用于 429 退避；标准 5xx 改用共享 HTTP_5XX_BACKOFF（封顶 30s）。
RETRY_BACKOFF = 30.0
# 连接类故障（TCP 重置/断连/超时，如 WSAECONNRESET 10054 / TLS SSLEOFError）单独
# 更宽松（2026-10-01 事故修复）：这类多为网关侧瞬时抖动，重试命中率远高于 5xx 坏
# 窗口；此前与 5xx 共用 2 次预算，一次长连接（SSE）重置即判死整轮。
# 2026-10-03 SDK 迁移：原先由自研 urllib 层按 errno 白名单分类，现由 httpx 把
# 连接重置/SSL EOF/超时统一抛成 httpx.TransportError，SDK 再包成 APIConnectionError
# /APITimeoutError，sdk_engine._as_conn_error 转回 ConnectionError/TimeoutError
# ——本预算分支口径不变。
CONN_RETRIES = 4                      # 连接类总尝试次数（3 次重试）
CONN_BACKOFF = (5.0, 10.0, 20.0)      # 各次重试前退避（秒）

class AnthropicCompatProvider:
    """Anthropic /v1/messages 协议。超时默认 600s（10 分钟），用于吸收长思考和
    大上下文请求的慢响应；流式调用为帧间隔超时不受损，UI 测活单独传 30s 不受影响。
    2026-09-28 全走流式：只要网关支持（未 _stream_disabled）所有调用都走 SSE——
    流式为帧间隔超时，长思考（planner/intel 无回调调用实测 6-10 分钟）不再触发
    非流式「总等待」超时；回调可选，不传只是不上屏。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 600.0,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
        api_version: str = "2023-06-01",
        enable_thinking: bool = False,
        enable_cache: bool = True,
        context_tokens: int | None = None,  # 模型最大上下文（providers.json model_context；预算换算用）
        ctx_soft_budget: int | None = None,  # 有效软上限（providers.json ctx_soft_budget；超此主动压缩）
        summarizer_model: str | None = None,  # 专用摘要模型（providers.json summarizer_model；缺省用本模型）
        use_x_api_key: bool = True,
        proxy: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.api_version = api_version
        self._use_x_api_key = use_x_api_key
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
        self.proxy = proxy or None
        # 模型不支持 thinking 参数时自动降级（HTTP 400 去参重试后置位，本实例不再注入）
        self._thinking_disabled = False
        # 网关拒收 cache_control 标记（400 文案含 cache_control）时置位——本实例
        # system 不再打标（镜像 thinking/stream 降级先例）
        self._cache_disabled = False
        # 2026-10-03 SDK 迁移：默认传输层换成官方 anthropic SDK（连接池/代理/UA/
        # 超时/HTTP 状态分类/SSE 解析全交 SDK），sdk_engine 把 SDK 事件还原成 SSE
        # 行供 _consume_stream 复用。注入的 transport/stream_transport 优先且
        # 按需构建 SDK（两侧都注入则完全不碰 SDK）——测试与 ScriptedLLM 走注入路径。
        self._transport = transport
        self._stream_transport = stream_transport
        # 网关不认 stream 参数（400 文案含 stream）时置位，本实例回退非流式
        self._stream_disabled = False

    stream_capable = True
    """Agent 层据此决定是否传 on_thinking/should_cancel（duck-type 能力探测）。"""

    # ---------- 内部 ----------

    def _sdk_transports(self) -> tuple[Transport, StreamTransport]:
        """按需构建 SDK 传输层（只在真正调用时建，且构建一次即缓存）。"""
        if self._transport is None or self._stream_transport is None:
            sdk_t, sdk_st = build_anthropic_transports(
                self.base_url, self.api_key, self.api_version, self._use_x_api_key,
                self.timeout, self.proxy)
            if self._transport is None:
                self._transport = sdk_t
            if self._stream_transport is None:
                self._stream_transport = sdk_st
        return self._transport, self._stream_transport

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
        on_retry: Callable[[int, int, int | str], None] | None = None,
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
        # 无回调调用走非流式——服务端思考完才回响应头，600s 是总等待上限，长思考
        # （实测 6-10 分钟）必超时；重试= 原样重发同一请求注定再超时，纯浪费 5+ 分钟。
        # SSE 早回头 + 帧间隔超时天然抗长思考，故只要网关支持（未降级）就始终流式。
        use_stream = not self._stream_disabled
        if use_stream:
            body["stream"] = True
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        max_attempts = (RATE_LIMIT_ATTEMPTS + HTTP_5XX_ATTEMPTS
                        + CONN_RETRIES + 4)  # 各类预算 + 若干降级重试
        last_err: Exception | None = None
        conn_used = 0      # 连接类已用尝试数（含首次）
        rate_limit_used = 0  # 429 已用尝试数（含首次）
        status_5xx_used = 0  # 标准 5xx 已用尝试数（含首次）
        transport, stream_transport = self._sdk_transports()
        for _ in range(max_attempts):
            try:
                if use_stream:
                    status, data = stream_transport(
                        f"{self.base_url}/v1/messages", self._headers(), payload)
                else:
                    status, data = transport(
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
                    if status == 429:
                        rate_limit_used += 1
                        can_retry = rate_limit_used < RATE_LIMIT_ATTEMPTS
                        retry_attempt = rate_limit_used
                        retry_total = RATE_LIMIT_ATTEMPTS - 1
                    elif status in HTTP_5XX_STATUS:
                        status_5xx_used += 1
                        can_retry = status_5xx_used < HTTP_5XX_ATTEMPTS
                        retry_attempt = status_5xx_used
                        retry_total = HTTP_5XX_RETRIES
                    else:
                        can_retry = False
                        retry_attempt = 0
                        retry_total = 0
                    if can_retry:
                        last_err = LLMError(msg, status=status,
                                            body=json.dumps(data, ensure_ascii=False)[:500])
                        if on_retry is not None:
                            on_retry(retry_attempt, retry_total, status)
                        if status == 429:
                            time.sleep(RETRY_BACKOFF * rate_limit_used)
                        else:
                            time.sleep(HTTP_5XX_BACKOFF[min(
                                status_5xx_used - 1, len(HTTP_5XX_BACKOFF) - 1)])
                        continue
                    raise LLMError(msg, status=status,
                                   body=json.dumps(data, ensure_ascii=False)[:500])
                if use_stream:
                    # 流已开建连成功：中途断开默认不重试（流不可重放，重试会重复回调
                    # on_thinking/on_text 造成重复上屏）。健壮性修复（2026-10-01）：
                    # 包一层增量探针——若尚未吐出任何思考/文本增量（用户什么都没看到），
                    # 则安全重试（杀连接重发原请求）；已吐过增量维持不重试。
                    seen = {"emitted": False}

                    def _watch(cb):
                        def _inner(delta):
                            seen["emitted"] = True
                            if cb is not None:
                                cb(delta)
                        return _inner

                    try:
                        try:
                            return self._consume_stream(
                                data, _watch(on_thinking) if on_thinking is not None else None,
                                should_cancel,
                                _watch(on_text) if on_text is not None else None)
                        except RawFallback as fb:
                            # SDK 事件解析失败（Ark SSE 尾部冗余）→ 用 tee 录制的
                            # 原始字节重走自研 SSE 解析（raw_decode 容错），保住
                            # 2026-10-01 的尾部冗余抢救能力。已吐过增量则不能重放
                            # （会重复上屏），按流中断语义如实上报。
                            if seen["emitted"]:
                                raise LLMError("SDK 事件解析失败且已有增量输出，无法重放") from fb
                            return self._consume_stream(
                                iter(fb.lines),
                                _watch(on_thinking) if on_thinking is not None else None,
                                should_cancel,
                                _watch(on_text) if on_text is not None else None)
                    except (TimeoutError, ConnectionError,
                            http.client.IncompleteRead, ssl.SSLError) as e:
                        if not seen["emitted"]:
                            # 尚无任何增量输出：可重试（重发不产生重复上屏）
                            raise ConnectionError(f"流式传输中断（无增量，可重试）: {e}") from e
                        raise LLMError(f"流式传输中断: {e}") from e
                return self._parse(data)
            except (TimeoutError, ConnectionError) as e:
                last_err = e
                conn_used += 1
                if conn_used >= CONN_RETRIES:
                    raise LLMError(f"网络错误（已重试 {conn_used - 1} 次）: {e}") from e
                time.sleep(CONN_BACKOFF[min(conn_used - 1, len(CONN_BACKOFF) - 1)])
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
                        # 尾部冗余容错（2026-10-01）：JSON 本体完整但尾部多发字符
                        # （网关 SSE 异常：stop_reason=tool_use 正常收尾却报
                        # "Extra data"）——用 raw_decode 取开头完整的 JSON 值、忽略
                        # 尾部冗余；仅接受 dict（工具参数必为对象），成功即采用。
                        salvaged = None
                        try:
                            obj, _end = json.JSONDecoder().raw_decode(raw.lstrip())
                            if isinstance(obj, dict):
                                salvaged = obj
                        except json.JSONDecodeError:
                            salvaged = None
                        if salvaged is not None:
                            log.warning("工具参数尾部冗余已忽略（stop_reason=%s，"
                                        "已收 %d 字符）", stop_reason or "未知", len(raw))
                            b["input"] = salvaged
                            continue
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
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self.api_version,
        }
        if self._use_x_api_key:
            headers["x-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _parse(self, data: dict[str, Any]) -> LLMResponse:
        """委托 core.llm.parsing（SDK 引擎与 compat 层共用同一解析口径）。"""
        return parsing.parse_anthropic_response(data)

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
