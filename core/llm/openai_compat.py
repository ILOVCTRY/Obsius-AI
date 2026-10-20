import http.client
import json
import ssl
import time
from typing import Any

from core.llm import parsing
from core.llm.anthropic_compat import StreamTransport, Transport
from core.llm.dump import capture_request as dump_request_capture
from core.llm.provider import (LLMResponse, ToolCall, Usage, LLMError,
                               collapse_reasoning_echo, image_data_url,
                               tool_result_content)
from core.llm.retry import (COOLDOWN_BACKOFF, HTTP_5XX_ATTEMPTS,
                            HTTP_5XX_BACKOFF, HTTP_5XX_RETRIES, HTTP_5XX_STATUS,
                            RATE_LIMIT_ATTEMPTS, RATE_LIMIT_BACKOFF,
                            RATE_LIMIT_RETRIES, is_channel_verification_error,
                            is_model_cooldown_error, retry_note,
                            sleep_cancellable)
from core.llm.sdk_engine import RawFallback, build_openai_transports

CHAT_COMPLETIONS = "openai-chat-completions"
RESPONSES = "openai-responses"

# Cloudflare 专有暂态状态：520=边缘代理从源站拿到未知/无效响应，524=已连上源站
# 但源站在边缘超时窗口内没返回。与标准 5xx 一并纳入统一重试预算（2026-10-07
# 拉齐：原 520/524 各自 5 次专用预算，现并入标准口径，见下方 OPENAI_5XX_RETRYABLE）。
# 2026-10-08 与 Anthropic 路径拉齐：连接类共 11 次尝试（首次 + 10 次重试），
# 退避 2/4/8/16/30s 封顶（原 5 次尝试）。
OPENAI_CONNECTION_ATTEMPTS = 11
OPENAI_CONNECTION_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)
# 上游暂态状态（标准 5xx 500/502/503/504 + Cloudflare 520/524）：共 11 次尝试
# （首次 + 10 次重试），退避封顶 30s——与 Anthropic 路径共享 HTTP_5XX_* 口径。
# 2026-10-10：429 由「刻意不重试」改为 11 次尝试（首次 + 10 次重试），30s 固定退避
# ——与 Anthropic 路径拉齐，见 retry.RATE_LIMIT_*。
OPENAI_5XX_RETRYABLE = tuple(sorted(HTTP_5XX_STATUS | {520, 524}))
OPENAI_5XX_ATTEMPTS = HTTP_5XX_ATTEMPTS
OPENAI_5XX_RETRIES = HTTP_5XX_RETRIES
OPENAI_5XX_BACKOFF = HTTP_5XX_BACKOFF

# ---------- system 兼容通道（2026-10-17，网关丢 system 事故修复） ----------
# 实测 `api.huanling.icu`（幻灵）在 Responses `instructions`、`input` 内 role=system
# 项、chat-completions `messages[0]` system **三种形态下全部不到模型**（canary 0/4、
# 0/1、0/1；同一句话放 user content 3/3 生效；同代码路径 DeepSeek 官方与 ark-plan
# 均 1/1 生效 → 断点在网关）。后果：走该网关的会话，安全红线/rating 注入实际空转，
# 模型只凭通用常识答题、表面无异样。
# 兼容做法：providers.json 条目 `"system_in_user": true` 时，把 system 前置于**首条
# user 消息**（带显式边界标记），其余形态一律不再下发 system 字段——payload 即模型
# 实见内容，诊断页看到的就是生效的那份。
SYSTEM_IN_USER_BEGIN = "<<<CYBERSTRIKE-SYSTEM-PROMPT-BEGIN>>>"
SYSTEM_IN_USER_END = "<<<CYBERSTRIKE-SYSTEM-PROMPT-END>>>"
# 只放边界标记、**不加**「这是系统提示词、优先于用户消息」这类框定语：A/B 实测
# 加框定 1/3、不加 5/6（样本小但无收益），且自称 system 的元指令可能被部分上游
# 当注入抵制。system 正文自身带「永久强制/优先级最高」口径已足够。


class OpenAICompatProvider:
    stream_capable = True

    def __init__(self, base_url: str, api_key: str, model: str, *, format: str = CHAT_COMPLETIONS,
                 timeout: float = 600.0, transport: Transport | None = None,
                 stream_transport: StreamTransport | None = None, enable_thinking: bool = False,
                 context_tokens: int | None = None, ctx_soft_budget: int | None = None,
                 summarizer_model: str | None = None, proxy: str | None = None,
                 system_in_user: bool = False, thinking_effort: str | None = None,
                 supports_vision: bool = False):
        if format not in {CHAT_COMPLETIONS, RESPONSES}:
            raise ValueError(f"不支持的 OpenAI 格式: {format}")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.format = format
        self.timeout = timeout
        # vision 能力位（2026-10-19）：providers.json `vision: true` 显式声明才有。
        # 未声明 → view_image 工具不出图（回填 OCR 引导），避免往不支持的网关
        # 塞图片块被 400 打死整轮。真实能力由网关/模型决定，配置是唯一真源。
        self.supports_vision = bool(supports_vision)
        self._enable_thinking = enable_thinking
        self.thinking_effort = (str(thinking_effort or "high").strip().lower()
                                if enable_thinking else "high")
        if self.thinking_effort not in {"low", "medium", "high"}:
            self.thinking_effort = "high"
        self._thinking_disabled = False
        # 网关不支持 stream_options（400 文案含 stream_options）时置位——本实例
        # 不再下发，退化为「流式无 usage」（2026-10-13 缓存命中率）。
        self._stream_options_disabled = False
        self.context_tokens = context_tokens
        self.ctx_soft_budget = ctx_soft_budget
        self.summarizer_model = summarizer_model
        self.proxy = proxy or None
        # system 兼容通道（2026-10-17）：网关整个丢弃 system 字段时，把 system 前置于
        # 首条 user 消息下发（providers.json 条目 system_in_user=true）。两种格式都走
        # 同一开关——chat-completions 的 messages[0] 同样被该网关吞掉。
        self._system_in_user = bool(system_in_user)
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

        def request_with_retries(payload: bytes) -> tuple[int, Any, str]:
            """重试上游暂态状态（500/502/503/504/520/524 / 429 / 400 模型冷却
            / 400 渠道校验风控）或瞬时连接断开，其他错误立即返回。

            四类暂态状态统一 10 次重试预算（2026-10-07 拉齐：520/524 原为 5 次
            专用预算，现并入标准 5xx 口径；2026-10-10 429 亦并入 11 次尝试；
            2026-10-16 渠道校验拦截并入，与冷却同口径）。返回
            ``(status, data, note)``——``note`` 是重试耗尽后的计数后缀
            （``retry_note``），非重试类错误为空串，供错误文案显式带出「试了几次」。
            """
            max_attempts = (OPENAI_5XX_ATTEMPTS + RATE_LIMIT_ATTEMPTS
                            + RATE_LIMIT_ATTEMPTS + RATE_LIMIT_ATTEMPTS
                            + OPENAI_CONNECTION_ATTEMPTS)
            status_5xx_used = 0
            rate_limit_used = 0
            cooldown_used = 0
            channel_used = 0  # 400「渠道校验」风控拦截已用尝试数（2026-10-16）
            for attempt in range(1, max_attempts + 1):
                # 每轮重试**发起前**先探一次中止（2026-10-20）：退避已改可中断，
                # 但「退避结束 → 真正出网」之间还有一个窗口；且首次尝试前的
                # 这一步让停止信号在第一次失败后立刻生效，不必再等一整轮超时。
                if should_cancel is not None and should_cancel():
                    raise LLMError("已中断")
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
                    sleep_cancellable(OPENAI_CONNECTION_BACKOFF[min(
                        attempt - 1, len(OPENAI_CONNECTION_BACKOFF) - 1)],
                        should_cancel)
                    continue

                if status == 429:
                    rate_limit_used += 1
                    if rate_limit_used >= RATE_LIMIT_ATTEMPTS:
                        return status, data, retry_note(rate_limit_used - 1,
                                                        RATE_LIMIT_RETRIES)
                    if on_retry is not None:
                        on_retry(rate_limit_used, RATE_LIMIT_RETRIES, status)
                    sleep_cancellable(RATE_LIMIT_BACKOFF, should_cancel)
                    continue

                # 注意：HTTP 非 200 时 data 是错误 dict，但 200 随流式响应时是
                # SSE 生成器（无 .get）——故先判 isinstance(dict)（2026-10-10 修复
                # 越界访存致 13 例 OpenAI 流式测试 'generator' has no attribute）。
                err_obj = data.get("error", {}) if isinstance(data, dict) else {}
                # 渠道校验拦截（2026-10-16）：网关风控窗口内对所有请求回
                # 400，特征字段（code=11128/msg/displayTips）在报文**顶层**
                # 而非 error 子对象——故扫整份序列化错误体，不只看 err_obj。
                err_text = (json.dumps(data, ensure_ascii=False)
                            if isinstance(data, dict) else "")
                if is_channel_verification_error(status, err_text):
                    # 与「模型冷却」同口径：11 次尝试 + 30s 固定退避（冷却
                    # 约 3-5 分钟自行恢复，快节奏退避无意义）。
                    channel_used += 1
                    if channel_used >= RATE_LIMIT_ATTEMPTS:
                        return status, data, retry_note(channel_used - 1,
                                                        RATE_LIMIT_RETRIES)
                    if on_retry is not None:
                        on_retry(channel_used, RATE_LIMIT_RETRIES, status)
                    sleep_cancellable(COOLDOWN_BACKOFF, should_cancel)
                    continue
                if is_model_cooldown_error(
                        status, str(err_obj.get("message", "")),
                        str(err_obj.get("code", ""))):
                    # 400/413「模型全账号冷却」（2026-10-10）：与 Anthropic 路径
                    # 同口径——按暂态故障处理，11 次尝试 + 30s 固定退避。
                    cooldown_used += 1
                    if cooldown_used >= RATE_LIMIT_ATTEMPTS:
                        return status, data, retry_note(cooldown_used - 1,
                                                        RATE_LIMIT_RETRIES)
                    if on_retry is not None:
                        on_retry(cooldown_used, RATE_LIMIT_RETRIES, status)
                    sleep_cancellable(COOLDOWN_BACKOFF, should_cancel)
                    continue

                if status in OPENAI_5XX_RETRYABLE:
                    status_5xx_used += 1
                    if status_5xx_used >= OPENAI_5XX_ATTEMPTS:
                        return status, data, retry_note(status_5xx_used - 1,
                                                        OPENAI_5XX_RETRIES)
                    if on_retry is not None:
                        on_retry(status_5xx_used, OPENAI_5XX_RETRIES, status)
                    sleep_cancellable(OPENAI_5XX_BACKOFF[min(
                        status_5xx_used - 1, len(OPENAI_5XX_BACKOFF) - 1)],
                        should_cancel)
                    continue

                return status, data, ""
            raise RuntimeError("unreachable")  # pragma: no cover

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        # 出网请求体转储（llm-request-dump）：同一调用内按 payload 指纹去重，
        # 降级重发（剥 thinking/stream_options）才会多落一份。默认关闭。
        dumped: set[str] = set()
        stream_attempt = 0
        while True:
            # 请求体留痕（diagnostics-request-body）：capture_request 包住本次发送，
            # 退出时补上 SDK 真正出网的字节（wire/wire_match）。默认关闭。
            with dump_request_capture(url, payload, provider="openai",
                                      model=str(body.get("model") or ""),
                                      fmt=self.format, seen=dumped):
                status, data, note = request_with_retries(payload)
            if status != 200:
                message = str((data or {}).get("error", {}).get("message", "请求失败"))
                if (self._enable_thinking and not self._thinking_disabled
                        and status == 400 and "reason" in message.lower()):
                    self._thinking_disabled = True
                    body.pop("reasoning_effort", None)
                    body.pop("reasoning", None)
                    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    continue
                if (not self._stream_options_disabled and status == 400
                        and "stream_options" in message.lower()):
                    # 网关不认 stream_options → 本实例剥除重试一次（退化为无 usage）
                    self._stream_options_disabled = True
                    body.pop("stream_options", None)
                    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    continue
                raise LLMError(f"LLM 调用失败 HTTP {status}{note}: {message}",
                               status=status,
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
                        # 已吐增量后中断：半截内容已上屏，流不可重放。打 partial 标记
                        # 交上层走「接着续写」（2026-10-08）。
                        raise LLMError(f"流式传输中断: {exc}", partial=True) from exc
                    raise LLMError(
                        f"网络连接失败（已重试 {stream_attempt} 次）: {exc}") from exc
                stream_attempt += 1
                if on_retry is not None:
                    on_retry(stream_attempt, OPENAI_CONNECTION_ATTEMPTS, "stream")
                sleep_cancellable(OPENAI_CONNECTION_BACKOFF[min(
                    stream_attempt - 1, len(OPENAI_CONNECTION_BACKOFF) - 1)],
                    should_cancel)

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

    def _body(self, messages, system, tools, max_tokens, temperature, model):
        # system_in_user 兼容通道：system 改走 user content，标准 system 字段一律不下发
        # （下了也被网关丢，只会让诊断页显示一份模型根本看不到的「已注入」）。
        inline_system = bool(self._system_in_user and system)
        field_system = None if inline_system else system
        if self.format == CHAT_COMPLETIONS:
            body: dict[str, Any] = {"model": model, "messages": self._chat_messages(messages, field_system), "max_tokens": max_tokens, "stream": True}
            # 流式末帧带回 usage（含 prompt_tokens_details.cached_tokens）——OpenAI
            # Chat Completions 默认不下发，须显式 include_usage（2026-10-13）。
            if not self._stream_options_disabled:
                body["stream_options"] = {"include_usage": True}
            if tools:
                body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}} for t in tools]
            if temperature is not None:
                body["temperature"] = temperature
            if self._enable_thinking and not self._thinking_disabled:
                body["reasoning_effort"] = self.thinking_effort
            if inline_system:
                body["messages"] = self._ensure_system_in_user(
                    body["messages"], self._system_text(system))
            return body
        body = {"model": model, "input": self._response_input(messages), "max_output_tokens": max_tokens, "stream": True}
        if field_system:
            body["instructions"] = self._system_text(field_system)
        if tools:
            body["tools"] = [{"type": "function", "name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})} for t in tools]
        if temperature is not None:
            body["temperature"] = temperature
        if self._enable_thinking and not self._thinking_disabled:
            body["reasoning"] = {"effort": self.thinking_effort}
        if inline_system:
            body["input"] = self._ensure_system_in_user(
                body["input"], self._system_text(system))
        return body

    def _ensure_system_in_user(self, items: list[dict[str, Any]],
                               system_text: str) -> list[dict[str, Any]]:
        """把 system 前置于首条 user 消息（带边界标记），返回新的 items 列表。

        - 历史里已有该标记（首条 user 消息一直带着）→ 原样返回，**不重复注入**；
        - 没有 user 消息（罕见）→ 在最前面插一条只含 system 的 user 消息。
        两种格式的 items 都是 [{role/content}] 或 Responses 的
        [{type: function_call…}] 混合形态，故只认 role=="user" 的项。

        **带图消息（vision 通道，2026-10-19）**：content 是数组时必须插一个
        text 块，**不能** json.dumps 整条——那会把 image_url/input_image 块序列化
        成字符串，图当场消失。text 块类型按格式区分（chat-completions 用
        ``text``、Responses 用 ``input_text``），错用会被网关 400。
        """
        text_type = "input_text" if self.format == RESPONSES else "text"
        for m in items:
            c = m.get("content")
            if isinstance(c, str) and c.startswith(SYSTEM_IN_USER_BEGIN):
                return items
            if isinstance(c, list) and any(
                    isinstance(b, dict) and str(b.get("text") or "").startswith(
                        SYSTEM_IN_USER_BEGIN) for b in c):
                return items
        block = f"{SYSTEM_IN_USER_BEGIN}\n{system_text}\n{SYSTEM_IN_USER_END}\n\n"
        out = [dict(m) if isinstance(m, dict) else m for m in items]
        for m in out:
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, list):
                    m["content"] = [{"type": text_type, "text": block.rstrip("\n")}, *c]
                    return out
                head = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                m["content"] = block + head
                return out
        return [{"role": "user", "content": block.rstrip("\n")}] + out

    def _system_text(self, system):
        if isinstance(system, str):
            return system
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)

    # ---------- vision 通道（2026-10-19）：内部 image 块 → 各格式 payload ----------
    # 内部标准是 Anthropic 形态的 image 块（见 core.llm.provider.image_block）；
    # 两种 OpenAI 格式的图片字段**形态不同**，且 tool 结果的承载方式也不同：
    #   chat-completions：user content 用 {"type":"image_url","image_url":{"url":…}}；
    #                     **tool 消息的 content 只收字符串** → 图片必须另起一条 user。
    #   responses：       user content 用 {"type":"input_image","image_url":"data:…"}；
    #                     function_call_output.output 收块数组，图片可就地内嵌。
    # 两条路径都经真实网关实测（幻灵-DEEP / To-Tokens，deepseek-v4.1-flash、
    # gpt-6.1-sol 均能正确报出三色带的红绿蓝）。

    @staticmethod
    def _image_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
        return [b for b in blocks
                if isinstance(b, dict) and b.get("type") == "image"]

    def _chat_parts(self, blocks: list[Any]) -> list[dict[str, Any]] | None:
        """内部块数组 → chat-completions content 数组；**无图返回 None**（走旧路）。

        返回 None 是为了让「纯文本块数组」一字不差地沿用旧行为（拼成单个
        string）——老网关对数组形态的 text 支持参差，能不改就不改。
        """
        if not self._image_blocks(blocks):
            return None
        parts: list[dict[str, Any]] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif b.get("type") == "image":
                parts.append({"type": "image_url",
                              "image_url": {"url": image_data_url(b)}})
        return parts or None

    def _response_parts(self, blocks: list[Any]) -> list[dict[str, Any]] | None:
        """内部块数组 → Responses content 数组；无图返回 None（走旧路）。"""
        if not self._image_blocks(blocks):
            return None
        parts: list[dict[str, Any]] = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append({"type": "input_text", "text": b.get("text", "")})
            elif b.get("type") == "image":
                parts.append({"type": "input_image",
                              "image_url": image_data_url(b)})
        return parts or None

    def _split_tool_result(self, raw: Any) -> tuple[str, list[dict[str, Any]]]:
        """tool_result.content（str 或块数组）→ (纯文本, 图片块列表)。"""
        if isinstance(raw, str):
            return raw, []
        if not isinstance(raw, list):
            return str(raw or ""), []
        texts = [str(b.get("text", "")) for b in raw
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts), self._image_blocks(raw)

    def _chat_messages(self, messages, system):
        out = []
        if system:
            out.append({"role": "system", "content": self._system_text(system)})
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, list):
                if any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_result":
                            continue
                        text, imgs = self._split_tool_result(b.get("content", ""))
                        out.append({"role": "tool",
                                    "tool_call_id": b.get("tool_use_id", ""),
                                    "content": text})
                        # chat-completions 的 tool 消息只收字符串 → 图片另起一条
                        # user 消息紧跟其后（实测该形态被网关接受且模型能看见）。
                        if imgs:
                            out.append({"role": "user", "content": [
                                {"type": "image_url", "image_url": {
                                    "url": image_data_url(img)}} for img in imgs]})
                elif any(b.get("type") == "tool_use" for b in content if isinstance(b, dict)):
                    out.append({"role": "assistant", "content": next((b.get("text", "") for b in content if b.get("type") == "text"), None), "tool_calls": [
                        {"id": b.get("id", ""), "type": "function", "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)}}
                        for b in content if b.get("type") == "tool_use"
                    ]})
                else:
                    parts = self._chat_parts(content)
                    out.append({"role": m.get("role", "user"),
                                "content": parts if parts is not None
                                else self._system_text(content)})
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
                # 带图的整条消息走数组形态（与下面逐块摊平互斥）
                parts = self._response_parts(content)
                if parts is not None:
                    out.append({"role": m.get("role", "user"), "content": parts})
                    continue
                for b in content:
                    if b.get("type") == "tool_result":
                        text, imgs = self._split_tool_result(b.get("content", ""))
                        item: dict[str, Any] = {
                            "type": "function_call_output",
                            "call_id": b.get("tool_use_id", "")}
                        if imgs:
                            # Responses 的 function_call_output 收块数组 →
                            # 图片可就地内嵌，不必另起 user 消息。
                            item["output"] = [
                                {"type": "input_text", "text": text},
                                *({"type": "input_image",
                                   "image_url": image_data_url(img)} for img in imgs)]
                        else:
                            item["output"] = text
                        out.append(item)
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
                # 末帧 usage（stream_options.include_usage 触发；该帧 choices 通常为空）
                if evt.get("usage"):
                    usage = parsing.openai_usage_to_usage(
                        evt["usage"], input_key="prompt_tokens",
                        output_key="completion_tokens",
                        details_key="prompt_tokens_details")
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
                    usage = parsing.openai_usage_to_usage(
                        response.get("usage") or {}, input_key="input_tokens",
                        output_key="output_tokens",
                        details_key="input_tokens_details")
                    # 某些网关省略 output_item.done，只在最终 response.output
                    # 中返回完整 function_call；这里作为最后一道元数据补全。
                    for item in response.get("output", []) or []:
                        if item.get("type") == "function_call":
                            merge_call(item)
        # 推理回声清理（2026-10-09 事故）：中转把同一段推理同时经 output_text 与
        # reasoning_summary 下发时，text 只是 thinking 的复制品——清空 text，避免
        # 下游把它当「纯文本终稿」收轮（实测 52545 字符逐字相同→轮次提前终止）。
        result = collapse_reasoning_echo(
            LLMResponse(text=text, thinking=thinking, usage=usage))
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
        # content 可为 ToolOutput（vision 通道）：tool_result 块内部仍按 Anthropic
        # 形态带 [text, image…]，由 _chat_messages/_response_input 各自翻译。
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": tool_result_content(content), **({"is_error": True} if is_error else {})}]}
