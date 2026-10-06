"""SDK 引擎测试（2026-10-03 LLM SDK 迁移）。

`core/llm/sdk_engine` 把官方 anthropic/openai SDK 的流式事件还原成 SSE 行，
交给既有 `_consume_stream` 复用。这里用 `httpx.MockTransport` 替换底层传输，
让 SDK 走完整解析链路（事件解码 → 事件对象 → SSE 行还原），验证：

- 正常流（文本/思考/工具调用/用量/逐帧回调）
- Ark 特化在 SDK 路径下全部保住（400 三件套降级、尾部冗余 raw_decode 抢救、
  截断防御、非 SSE 兜底、缺 event: 行兜底、逐帧取消、上下文超限识别）
- SDK 异常 → 既有重试分支认得的异常类型（连接/超时/状态码）
"""

import json

import httpx
import pytest

from core.llm import sdk_engine
from core.llm.anthropic_compat import AnthropicCompatProvider
from core.llm.openai_compat import OpenAICompatProvider
from core.llm.provider import ContextOverflowError, LLMError


# ---------- 测试基建 ----------

def _wire(monkeypatch, handler):
    """把 SDK 的 httpx 传输换成 MockTransport，返回记录的请求列表。

    每个 Client 各配一个 TeeTransport（tee 缓冲按实例持有，跨调用共享会串扰）。"""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _build(timeout, proxy=None):
        tee = sdk_engine.TeeTransport(httpx.MockTransport(_record))
        return httpx.Client(transport=tee), tee

    monkeypatch.setattr(sdk_engine, "build_httpx_client", _build)
    return seen


def _sse(*events: str) -> httpx.Response:
    """按标准 SSE 拼事件（`event:` + `data:` + 空行），一次性吐出。"""
    body = "".join(events).encode("utf-8")

    class _Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield body

    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          stream=_Stream())


def ev(event: str, obj: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _msg_start(inp=10):
    return ev("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        "content": [], "stop_reason": None, "usage": {"input_tokens": inp, "output_tokens": 1}}})


def _block_start(index, block):
    return ev("content_block_start", {"type": "content_block_start",
                                      "index": index, "content_block": block})


def _delta(index, delta):
    return ev("content_block_delta", {"type": "content_block_delta",
                                      "index": index, "delta": delta})


def _msg_delta(stop_reason, out=20):
    return ev("message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                "usage": {"output_tokens": out}})


# ---------- Anthropic：正常流 ----------

def test_anthropic_sdk_stream_text_thinking_tool_use(monkeypatch):
    """SDK 事件 → SSE 行还原后，text/thinking/tool_use/usage/回调全对。"""
    _wire(monkeypatch, lambda r: _sse(
        _msg_start(12),
        _block_start(0, {"type": "thinking", "thinking": ""}),
        _delta(0, {"type": "thinking_delta", "thinking": "先看 NX"}),
        _block_start(1, {"type": "text", "text": ""}),
        _delta(1, {"type": "text_delta", "text": "我先用 checksec"}),
        _block_start(2, {"type": "tool_use", "id": "tu1", "name": "run_cmd", "input": {}}),
        _delta(2, {"type": "input_json_delta", "partial_json": '{"cmd":'}),
        _delta(2, {"type": "input_json_delta", "partial_json": '"id"}'}),
        _msg_delta("tool_use", 40),
        ev("message_stop", {"type": "message_stop"}),
    ))
    thinking, text = [], []
    resp = AnthropicCompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}],
        on_thinking=thinking.append, on_text=text.append)
    assert resp.text == "我先用 checksec"
    assert resp.thinking == "先看 NX"
    assert resp.stop_reason == "tool_use"
    assert resp.usage.input_tokens == 12 and resp.usage.output_tokens == 40
    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == \
        [("tu1", "run_cmd", {"cmd": "id"})]
    assert thinking == ["先看 NX"] and text == ["我先用 checksec"]


def test_anthropic_sdk_stream_url_and_auth_headers(monkeypatch):
    """SDK 引擎打到 {base_url}/v1/messages，鉴权头沿用 x-api-key / bearer 开关。

    回归：SDK 会把自持凭据与自定义头**拼接**，并回退读 `ANTHROPIC_AUTH_TOKEN` /
    `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` 环境变量——本机 .env 正有真实
    `ANTHROPIC_AUTH_TOKEN`，曾导致 `x-api-key: ark-key, ark-key` 与无关 token 泄漏。"""
    seen = _wire(monkeypatch, lambda r: _sse(_msg_start(), _msg_delta("end_turn")))
    AnthropicCompatProvider("https://ark.example/api/coding", "ark-key", "m").chat(
        [{"role": "user", "content": "hi"}])
    assert str(seen[0].url) == "https://ark.example/api/coding/v1/messages"
    assert seen[0].headers["x-api-key"] == "ark-key"
    assert "authorization" not in seen[0].headers
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    assert seen[0].headers["user-agent"].startswith("Mozilla/5.0")

    seen = _wire(monkeypatch, lambda r: _sse(_msg_start(), _msg_delta("end_turn")))
    AnthropicCompatProvider("https://ark.example/api/coding", "ark-key", "m",
                            use_x_api_key=False).chat([{"role": "user", "content": "hi"}])
    assert seen[0].headers["authorization"] == "Bearer ark-key"
    assert "x-api-key" not in seen[0].headers


def test_anthropic_sdk_proxy_reaches_httpx_client(monkeypatch):
    """供应商专属代理透传到 httpx 传输层（真实客户端构造一次即缓存）。"""
    seen = _wire(monkeypatch, lambda r: _sse(_msg_start(), _msg_delta("end_turn")))
    p = AnthropicCompatProvider("https://ark.example", "k", "m",
                                proxy="http://127.0.0.1:7890")
    p.chat([{"role": "user", "content": "hi"}])
    assert p.proxy == "http://127.0.0.1:7890"
    assert str(seen[0].url) == "https://ark.example/v1/messages"


# ---------- Anthropic：Ark 特化在 SDK 路径下保住 ----------

def test_anthropic_sdk_thinking_400_degrades_and_retries(monkeypatch):
    """400 文案含 thinking → 去参重试一次并置实例标志，后续不再注入。"""
    seen = _wire(monkeypatch, lambda r: httpx.Response(400, json={"error": {
        "type": "invalid_request_error", "message": "thinking is not supported"}}))
    p = AnthropicCompatProvider("https://fake", "k", "m", enable_thinking=True)
    with pytest.raises(LLMError, match="HTTP 400"):
        p.chat([{"role": "user", "content": "hi"}])
    assert p._thinking_disabled is True
    first = json.loads(seen[0].content)
    second = json.loads(seen[1].content)
    assert first["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert "thinking" not in second


def test_anthropic_sdk_stream_400_degrades_to_plain(monkeypatch):
    """400 文案含 stream → 本实例回退非流式（第二次请求不带 stream）。"""
    def handler(request):
        if json.loads(request.content).get("stream"):
            return httpx.Response(400, json={"error": {
                "type": "invalid_request_error", "message": "stream is not supported"}})
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1}})

    seen = _wire(monkeypatch, handler)
    p = AnthropicCompatProvider("https://fake", "k", "m")
    assert p.chat([{"role": "user", "content": "hi"}]).text == "OK"
    assert p._stream_disabled is True
    assert json.loads(seen[0].content)["stream"] is True
    assert "stream" not in json.loads(seen[1].content)


def test_anthropic_sdk_cache_control_400_strips_marker(monkeypatch):
    """400 文案含 cache_control → 剥除标记重试，system 仍在但无 cache_control。"""
    def handler(request):
        if "cache_control" in request.content.decode("utf-8"):
            return httpx.Response(400, json={"error": {
                "type": "invalid_request_error", "message": "cache_control not allowed"}})
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1}})

    seen = _wire(monkeypatch, handler)
    p = AnthropicCompatProvider("https://fake", "k", "m")
    assert p.chat([{"role": "user", "content": "hi"}], system="you are a helper").text == "OK"
    assert p._cache_disabled is True
    assert "cache_control" in json.loads(seen[0].content)["system"][0]
    assert "cache_control" not in json.loads(seen[1].content)["system"][0]


def test_anthropic_sdk_429_retries_then_succeeds(monkeypatch):
    """SDK 的 429 映射回状态码分支 → 既有 MAX_RETRIES 预算重试。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {
                "type": "rate_limit_error", "message": "slow down"}})
        return _sse(_msg_start(), _block_start(0, {"type": "text", "text": ""}),
                    _delta(0, {"type": "text_delta", "text": "OK"}), _msg_delta("end_turn"))

    _wire(monkeypatch, handler)
    resp = AnthropicCompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}])
    assert resp.text == "OK" and calls["n"] == 2


def test_anthropic_sdk_context_overflow_hint_raises(monkeypatch):
    """400/413 文案命中超限特征词 → ContextOverflowError（不可重试）。"""
    _wire(monkeypatch, lambda r: httpx.Response(400, json={"error": {
        "type": "invalid_request_error",
        "message": "prompt is too long: maximum context length exceeded"}}))
    with pytest.raises(ContextOverflowError):
        AnthropicCompatProvider("https://fake", "k", "m").chat(
            [{"role": "user", "content": "hi"}])


def test_anthropic_sdk_connection_and_timeout_use_conn_budget(monkeypatch):
    """SDK 连接/超时异常 → 既有 CONN_RETRIES 连接预算（4 次尝试后如实上报）。"""
    monkeypatch.setattr("core.llm.anthropic_compat.CONN_BACKOFF", (0, 0, 0))
    for exc in (httpx.ConnectError("[WinError 10054] reset"), httpx.ReadTimeout("slow")):
        calls = {"n": 0}

        def handler(request, _exc=exc):
            calls["n"] += 1
            raise _exc

        _wire(monkeypatch, handler)
        with pytest.raises(LLMError, match="网络错误（已重试 3 次）"):
            AnthropicCompatProvider("https://fake", "k", "m").chat(
                [{"role": "user", "content": "hi"}])
        assert calls["n"] == 4


def test_anthropic_sdk_stream_body_drop_retries_then_succeeds(monkeypatch):
    """响应头已到、body 迭代中被对端半途关连接（裸 httpx.RemoteProtocolError
    "incomplete chunked read"）→ 归类 ConnectionError → 既有连接重试预算生效。

    回归（2026-10-06 事故）：SDK 的 `Stream.__stream__` 只 `finally` 关流、不包装
    body 读取异常，裸 httpx 异常此前绕过重试，且被错误分类器判成 unknown。"""
    monkeypatch.setattr("core.llm.anthropic_compat.CONN_BACKOFF", (0, 0, 0))
    calls = {"n": 0}

    class _Drop(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)")

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_Drop())
        return _sse(_msg_start(), _block_start(0, {"type": "text", "text": ""}),
                    _delta(0, {"type": "text_delta", "text": "OK"}),
                    _msg_delta("end_turn"))

    _wire(monkeypatch, handler)
    resp = AnthropicCompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}])
    assert resp.text == "OK" and calls["n"] == 2


def test_anthropic_sdk_tool_arg_trailing_junk_salvaged(monkeypatch):
    """工具参数尾部冗余（stop_reason=tool_use 却多发字符）→ raw_decode 抢救。"""
    _wire(monkeypatch, lambda r: _sse(
        _msg_start(),
        _block_start(0, {"type": "tool_use", "id": "t1", "name": "run_cmd", "input": {}}),
        _delta(0, {"type": "input_json_delta", "partial_json": '{"cmd":"id"}EXTRA'}),
        _msg_delta("tool_use"),
    ))
    resp = AnthropicCompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}])
    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == \
        [("t1", "run_cmd", {"cmd": "id"})]


def test_anthropic_sdk_truncated_tool_args_raise(monkeypatch):
    """工具参数残缺 → LLMError(truncated=True)（供 agent 层整轮重试）。"""
    _wire(monkeypatch, lambda r: _sse(
        _msg_start(),
        _block_start(0, {"type": "tool_use", "id": "t1", "name": "run_cmd", "input": {}}),
        _delta(0, {"type": "input_json_delta", "partial_json": '{"cmd":'}),
        _msg_delta("max_tokens"),
    ))
    with pytest.raises(LLMError) as ei:
        AnthropicCompatProvider("https://fake", "k", "m").chat(
            [{"role": "user", "content": "hi"}])
    assert ei.value.truncated is True


def test_anthropic_sdk_missing_event_lines_falls_back_to_raw(monkeypatch):
    """网关 SSE 缺 `event:` 行 → SDK 静默丢空 → 降级原始字节重解析救回。"""
    body = "".join([
        f'data: {json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5, "output_tokens": 1}}})}\n\n',
        f'data: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})}\n\n',
        f'data: {json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "救回"}})}\n\n',
        f'data: {json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}})}\n\n',
    ]).encode("utf-8")

    class _Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield body

    _wire(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_Stream()))
    resp = AnthropicCompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}])
    assert resp.text == "救回" and resp.usage.input_tokens == 5


def test_anthropic_sdk_whole_json_response_falls_back(monkeypatch):
    """网关无视 stream:true 直接回整份 JSON → SDK 零事件 → 按普通响应解析。"""
    payload = {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
               "usage": {"input_tokens": 2, "output_tokens": 1}}

    class _Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield json.dumps(payload).encode("utf-8")

    _wire(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-type": "application/json"}, stream=_Stream()))
    resp = AnthropicCompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}])
    assert resp.text == "OK" and resp.stop_reason == "end_turn"


def test_anthropic_sdk_should_cancel_interrupts(monkeypatch):
    """逐帧轮询 should_cancel，命中即抛 LLMError（即点即停）。"""
    _wire(monkeypatch, lambda r: _sse(
        _msg_start(),
        _block_start(0, {"type": "text", "text": ""}),
        _delta(0, {"type": "text_delta", "text": "x"}),
        _msg_delta("end_turn"),
    ))
    with pytest.raises(LLMError, match="已中断"):
        AnthropicCompatProvider("https://fake", "k", "m").chat(
            [{"role": "user", "content": "hi"}], should_cancel=lambda: True)


# ---------- OpenAI：SDK 路径 ----------

def _openai_chunk(delta, finish=None, usage=None):
    body = {"id": "c1", "object": "chat.completion.chunk", "created": 1,
            "model": "m", "choices": [{"index": 0, "delta": delta,
                                       "finish_reason": finish}]}
    if usage is not None:
        body["choices"] = []
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n"


def test_openai_sdk_chat_completions_stream(monkeypatch):
    """OpenAI 兼容路径走 SDK：文本/思考/工具调用/逐帧回调全对。"""
    body = "".join([
        _openai_chunk({"role": "assistant", "content": ""}),
        _openai_chunk({"reasoning_content": "想想"}),
        _openai_chunk({"content": "好的"}),
        _openai_chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                       "function": {"name": "run_cmd",
                                                    "arguments": '{"cmd": "id"}'}}]},
                      finish="tool_calls"),
        "data: [DONE]\n\n",
    ]).encode("utf-8")

    class _Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield body

    seen = _wire(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_Stream()))
    thinking, text = [], []
    resp = OpenAICompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}],
        on_thinking=thinking.append, on_text=text.append)
    assert resp.text == "好的" and resp.thinking == "想想"
    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == \
        [("call_1", "run_cmd", {"cmd": "id"})]
    assert thinking == ["想想"] and text == ["好的"]
    assert str(seen[0].url) == "https://fake/v1/chat/completions"


def test_openai_sdk_responses_stream(monkeypatch):
    """Responses 格式走 SDK：output_text 增量 + 工具元数据补全。"""
    events = [
        'event: response.output_item.added\n'
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_1","call_id":"call_1"}}\n\n',
        'event: response.function_call_arguments.delta\n'
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","delta":"{\\"cmd\\":\\"id\\"}"}\n\n',
        'event: response.output_item.done\n'
        'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"run_cmd","arguments":"{\\"cmd\\":\\"id\\"}"}}\n\n',
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"function_call","id":"fc_1","call_id":"call_1","name":"run_cmd","arguments":"{\\"cmd\\":\\"id\\"}"}]}}\n\n',
        "data: [DONE]\n\n",
    ]
    body = "".join(events).encode("utf-8")

    class _Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield body

    seen = _wire(monkeypatch, lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_Stream()))
    resp = OpenAICompatProvider("https://fake", "k", "m", format="openai-responses").chat(
        [{"role": "user", "content": "hi"}])
    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == \
        [("call_1", "run_cmd", {"cmd": "id"})]
    assert str(seen[0].url) == "https://fake/v1/responses"


def test_openai_sdk_whole_json_response_falls_back(monkeypatch):
    """网关无视 stream:true 回整份 JSON → SDK 零事件 → 按普通响应解析。"""
    for fmt, payload, expected in (
        ("openai-chat-completions",
         {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 2, "completion_tokens": 1}}, "OK"),
        ("openai-responses",
         {"output": [{"type": "message",
                      "content": [{"type": "output_text", "text": "R-OK"}]}],
          "status": "completed"}, "R-OK"),
    ):
        class _Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield json.dumps(payload).encode("utf-8")

        _wire(monkeypatch, lambda r: httpx.Response(
            200, headers={"content-type": "application/json"}, stream=_Stream()))
        resp = OpenAICompatProvider("https://fake", "k", "m", format=fmt).chat(
            [{"role": "user", "content": "hi"}])
        assert resp.text == expected


def test_openai_sdk_520_retries_then_succeeds(monkeypatch):
    """上游 520（CDN 边缘）经 SDK 映射回状态码 → 既有 520 重试预算生效。"""
    from core.llm import openai_compat

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(520, json={"error": {"message": "origin error"}})
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "OK"}, "finish_reason": "stop"}]})

    _wire(monkeypatch, handler)
    retries = []
    resp = OpenAICompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}],
        on_retry=lambda *a: retries.append(a))
    assert resp.text == "OK" and calls["n"] == 3
    assert retries == [(1, 5, 520), (2, 5, 520)]


def test_openai_sdk_connection_reset_retries(monkeypatch):
    """连接重置经 SDK 映射为 ConnectionError → 既有连接重试预算生效。"""
    from core.llm import openai_compat

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("[WinError 10054] 远程主机强迫关闭了一个现有的连接。")
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "OK"}, "finish_reason": "stop"}]})

    _wire(monkeypatch, handler)
    assert OpenAICompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}]).text == "OK"
    assert calls["n"] == 3


def test_openai_sdk_stream_body_drop_retries_then_succeeds(monkeypatch):
    """同上（OpenAI 兼容路径）：body 迭代中断 → 既有连接重试预算生效。"""
    from core.llm import openai_compat

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    calls = {"n": 0}

    class _Drop(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)")

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_Drop())
        return _sse(_openai_chunk({"content": "OK"}, finish="stop"))

    _wire(monkeypatch, handler)
    assert OpenAICompatProvider("https://fake", "k", "m").chat(
        [{"role": "user", "content": "hi"}]).text == "OK"
    assert calls["n"] == 2


# ---------- sdk_engine 单元 ----------

def test_sdk_engine_maps_bare_httpx_errors_to_conn_class():
    """裸 httpx 传输异常也必须归类为连接级——否则绕过 chat() 重试预算，
    并被 `_classify_error` 判成 unknown。"""
    assert isinstance(sdk_engine._as_conn_error(
        httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body "
            "(incomplete chunked read)")), ConnectionError)
    assert isinstance(sdk_engine._as_conn_error(httpx.ConnectError("reset")),
                      ConnectionError)
    assert isinstance(sdk_engine._as_conn_error(httpx.ReadTimeout("slow")),
                      TimeoutError)
    other = ValueError("无关异常")
    assert sdk_engine._as_conn_error(other) is other

def test_sdk_engine_error_dict_prefers_json_error_body():
    """APIStatusError → 既有分支消费的 {"error": {...}} 形态。"""
    request = httpx.Request("POST", "https://fake/v1/messages")
    response = httpx.Response(429, json={"error": {"type": "rate_limit_error",
                                                   "message": "slow down"}},
                              request=request)
    err = sdk_engine._error_dict(Exception("boom"))
    assert err == {"error": {"code": "", "message": "boom"}}

    import anthropic
    api_err = anthropic.APIStatusError("slow down", response=response, body=None)
    assert sdk_engine._error_dict(api_err) == {
        "error": {"type": "rate_limit_error", "message": "slow down"}}


def test_sdk_engine_rawfallback_exposes_lines():
    """RawFallback 把 tee 原始字节按行暴露给上层自研解析（保留行尾，供 SSE 解析）。"""
    raw = b"data: {\"a\": 1}\n\ndata: {\"b\": 2}\n\n"
    fb = sdk_engine.RawFallback(raw)
    assert fb.raw == raw
    assert [ln for ln in fb.lines if ln.strip()] == \
        [b'data: {"a": 1}\n', b'data: {"b": 2}\n']
