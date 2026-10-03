"""LLM 层测试：响应解析、错误路径、工具回填、模型路由、密钥解析。全部走 fake transport，不触网。
2026-09-28 全走流式后 chat 一律走 stream_transport：无回调调用用 _fake_stream_transport
（整份 JSON 行流，命中非 SSE 兜底，解析等价非流式）；transport= 仅降级/哨兵用。"""

import json
import socket
import urllib.error

import pytest

from core.llm import AnthropicCompatProvider, ArkCodingProvider, LLMError, ModelRouter
from core.llm.routing import load_dotenv


def _anthropic_response(content_blocks, stop_reason="end_turn", usage=None):
    return {
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
        "model": "fake-model",
    }


def _fake_transport(response=None, status=200, capture=None):
    """返回一个记录请求并吐固定响应的 transport。"""
    def transport(url, headers, body):
        if capture is not None:
            capture.append({"url": url, "headers": headers, "body": json.loads(body)})
        return status, response if status == 200 else response
    return transport


def _sse_json(resp_dict):
    """整份响应 dict → 单行行流（命中 _consume_stream 非 SSE 兜底，解析等价 _parse）。"""
    return iter([json.dumps(resp_dict, ensure_ascii=False)])


def _fake_stream_transport(response=None, status=200, capture=None):
    """流式版 fake transport（2026-09-28 全走流式后 chat 一律走 stream_transport）：
    200 吐整份 JSON 行流（非 SSE 兜底解析等价非流式）；非 200 吐错误 dict——
    错误处理分支流式/非流式共享，注入形态与非流式 fake 等价。"""
    def transport(url, headers, body):
        if capture is not None:
            capture.append({"url": url, "headers": headers, "body": json.loads(body)})
        if status == 200:
            return 200, _sse_json(response or {})
        return status, response
    return transport


# ---------- 响应解析 ----------

def test_parse_text_thinking_and_tool_use():
    captured = []
    stream_transport = _fake_stream_transport(
        _anthropic_response(
            [
                {"type": "thinking", "thinking": "先看 NX"},
                {"type": "text", "text": "我先用 checksec"},
                {"type": "tool_use", "id": "tc-1", "name": "run_cmd",
                 "input": {"cmd": "checksec a.out", "runtime": "sandbox"}},
            ],
            stop_reason="tool_use",
        ),
        capture=captured,
    )
    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=stream_transport)
    r = p.chat([{"role": "user", "content": "分析这个 ELF"}],
               system="你是逆向专家",
               tools=[{"name": "run_cmd", "description": "执行", "input_schema": {"type": "object"}}])
    assert r.text == "我先用 checksec"
    assert r.thinking == "先看 NX"
    assert r.tool_calls[0].name == "run_cmd"
    assert r.tool_calls[0].arguments["runtime"] == "sandbox"
    assert r.stop_reason == "tool_use"
    assert r.usage.input_tokens == 10
    # 请求侧检查：system / tools / 协议头（M1 prompt caching：str 自动包装单块打标）
    req = captured[0]
    assert req["url"].endswith("/v1/messages")
    assert req["headers"]["x-api-key"] == "key"
    assert req["body"]["system"] == [
        {"type": "text", "text": "你是逆向专家", "cache_control": {"type": "ephemeral"}}]
    assert req["body"]["tools"][0]["name"] == "run_cmd"


def test_system_blocks_passthrough_and_cache_disabled():
    """M1 prompt caching：块数组透传（dict 原样/str 包装不打标）+ enable_cache=False。"""
    captured = []
    stream_transport = _fake_stream_transport(
        _anthropic_response([{"type": "text", "text": "ok"}]), capture=captured)
    blocks = [
        {"type": "text", "text": "稳定块", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "动态块"},
    ]
    p = AnthropicCompatProvider("https://fake", "k", "m",
                                stream_transport=stream_transport)
    p.chat([{"role": "user", "content": "hi"}], system=blocks)
    assert captured[0]["body"]["system"] == blocks
    # 传入列表不被污染（浅拷贝透传）
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # enable_cache=False：str 不打标
    captured.clear()
    p2 = AnthropicCompatProvider("https://fake", "k", "m",
                                 stream_transport=stream_transport,
                                 enable_cache=False)
    p2.chat([{"role": "user", "content": "hi"}], system="纯文本")
    assert captured[0]["body"]["system"] == [{"type": "text", "text": "纯文本"}]


def test_cache_control_degrade_on_400():
    """M1：网关拒收 cache_control（400 文案含 cache_control）→ 去标重试成功，
    实例置位后续请求不再打标（镜像 thinking 降级先例）。"""
    calls = []

    def flaky(url, headers, body):
        req = json.loads(body)
        calls.append(req)
        if "cache_control" in json.dumps(req):
            return 400, {"error": {"code": "", "message": "unknown field cache_control"}}
        return 200, _sse_json(_anthropic_response([{"type": "text", "text": "ok"}]))

    p = AnthropicCompatProvider("https://fake", "k", "m", stream_transport=flaky)
    r = p.chat([{"role": "user", "content": "hi"}], system="稳定前缀")
    assert r.text == "ok"
    assert len(calls) == 2
    assert "cache_control" in json.dumps(calls[0]["system"])  # 首次带标被网关拒
    assert calls[1]["system"] == [{"type": "text", "text": "稳定前缀"}]  # 去标重试
    # 第二次请求直接不带标（降级一次性置位）
    p.chat([{"role": "user", "content": "hi"}], system="稳定前缀")
    assert "cache_control" not in json.dumps(calls[2]["system"])


def test_error_raises_llmerror():
    stream_transport = _fake_stream_transport(
        {"error": {"code": "InvalidEndpointOrModel.NotFound", "message": "no such model"}},
        status=404,
    )
    p = AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=stream_transport)
    with pytest.raises(LLMError, match="404"):
        p.chat([{"role": "user", "content": "hi"}])


def test_context_overflow_raises_and_model_override():
    """输入超限识别（2026-09-30 ct-6）：400/413 文案命中 _OVERFLOW_HINTS → 抛
    ContextOverflowError（不可重试，调用方据此强制压缩）；非命中 400 → 普通
    LLMError（不误判为压缩信号）；chat(model=) 覆盖 body.model（摘要器专用模型路径）。"""
    from core.llm.provider import ContextOverflowError

    calls: list[dict] = []

    def overflow_400(url, headers, body):
        calls.append(json.loads(body))
        return 400, {"error": {"message": "Input length exceeds the maximum context length"}}

    p = AnthropicCompatProvider("https://fake", "k", "m", stream_transport=overflow_400)
    with pytest.raises(ContextOverflowError):
        p.chat([{"role": "user", "content": "x"}])
    assert len(calls) == 1  # 超限不可重试：一次即抛

    def plain_400(url, headers, body):
        return 400, {"error": {"message": "invalid request payload"}}

    p2 = AnthropicCompatProvider("https://fake", "k", "m", stream_transport=plain_400)
    with pytest.raises(LLMError) as ei:
        p2.chat([{"role": "user", "content": "x"}])
    assert not isinstance(ei.value, ContextOverflowError)

    def overflow_413(url, headers, body):
        calls.append(json.loads(body))
        return 413, {"error": {"message": "request entity too large"}}

    p3 = AnthropicCompatProvider("https://fake", "k", "m", stream_transport=overflow_413)
    with pytest.raises(ContextOverflowError):
        p3.chat([{"role": "user", "content": "x"}], model="cheap-summarizer")
    assert calls[-1]["model"] == "cheap-summarizer"  # model= 覆盖实例模型


def test_retry_on_transient_status_then_success(monkeypatch):
    """429/5xx 瞬时故障自动重试，恢复后成功——一次抖动不该杀死编排。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    statuses = iter([500, 200])
    n_calls = {"n": 0}

    def transport(url, headers, body):
        n_calls["n"] += 1
        status = next(statuses)
        if status == 200:
            return 200, _sse_json(_anthropic_response([{"type": "text", "text": "ok"}]))
        return status, {"error": {"message": "transient"}}

    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=transport)
    r = p.chat([{"role": "user", "content": "hi"}])
    assert r.text == "ok" and n_calls["n"] == 2


def test_retry_exhausts_then_raises(monkeypatch):
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    monkeypatch.setattr("core.llm.anthropic_compat.MAX_RETRIES", 2)

    def transport(url, headers, body):
        return 503, {"error": {"message": "down"}}

    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=transport)
    with pytest.raises(LLMError, match="503"):
        p.chat([{"role": "user", "content": "hi"}])


# ---------- 瞬时网络错误（2026-10-01 健壮性） ----------
# 背景：网关 TCP 重置（Windows WSAECONNRESET 10054）此前被包成 LLMError，
# 重试循环接不住 → 0 次重试直接判轮次失败。下列测试锁定「连接类故障可重试」。

class _BreakingStream:
    """迭代时先吐 N 行再抛 ConnectionResetError（模拟流式建连后中途断流）。"""

    def __init__(self, lines, exc_after=0):
        self._lines = list(lines)
        self._exc_after = exc_after
        self._i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._i >= self._exc_after:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")
        line = self._lines[self._i]
        self._i += 1
        return line

    def close(self):
        pass


def test_default_transport_classifies_connection_reset_as_retryable(monkeypatch):
    """_default_transport/_default_stream_transport：URLError(ConnectionResetError
    10054) → 抛 ConnectionError（可重试）；DNS 解析失败仍 LLMError（快速暴露）。"""
    from core.llm import anthropic_compat as ac

    def _reset(*a, **k):
        raise urllib.error.URLError(
            ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"))

    monkeypatch.setattr(ac.urllib.request, "urlopen", _reset)
    with pytest.raises(ConnectionError):
        ac._default_transport(5.0)("https://fake/v1/messages", {}, b"{}")
    with pytest.raises(ConnectionError):
        ac._default_stream_transport(5.0)("https://fake/v1/messages", {}, b"{}")

    def _dns(*a, **k):
        raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))

    monkeypatch.setattr(ac.urllib.request, "urlopen", _dns)
    with pytest.raises(ac.LLMError):
        ac._default_transport(5.0)("https://fake/v1/messages", {}, b"{}")


def test_provider_proxy_uses_explicit_proxy_and_survives_build(tmp_path, monkeypatch):
    """供应商专属代理进入普通传输层，ProviderStore.build 也保留该配置。"""
    from core.llm import anthropic_compat as ac
    from core.llm.providers import ProviderStore

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"ok": true}'

    calls = {}

    class Opener:
        def open(self, req, timeout):
            calls["url"] = req.full_url
            calls["timeout"] = timeout
            return Response()

    def build_opener(handler):
        calls["proxies"] = handler.proxies
        return Opener()

    monkeypatch.setattr(ac.urllib.request, "build_opener", build_opener)
    status, data = ac._default_transport(7.0, "http://127.0.0.1:7890")(
        "https://llm.example/v1/messages", {}, b"{}")
    assert status == 200 and data == {"ok": True}
    assert calls["proxies"]["http"] == "http://127.0.0.1:7890"
    assert calls["url"] == "https://llm.example/v1/messages"

    store = ProviderStore(tmp_path / "providers.json")
    store.save([{"name": "custom", "base_url": "https://llm.example", "api_key": "k",
                 "models": ["m"], "enabled": True,
                 "proxy": "http://127.0.0.1:7890"}])
    assert store.get("custom")["proxy"] == "http://127.0.0.1:7890"
    assert store.build("custom").proxy == "http://127.0.0.1:7890"
    with pytest.raises(ValueError, match=r"proxy 须为 http\(s\) 地址"):
        store.save([{"name": "bad-proxy", "base_url": "https://llm.example",
                     "api_key": "k", "models": ["m"], "enabled": True,
                     "proxy": "socks5://127.0.0.1:1080"}])


def test_retry_on_connection_reset_then_success(monkeypatch):
    """连接重置（10054）自动重试，恢复后成功——一次 TCP 重置不该杀死整轮。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    monkeypatch.setattr("core.llm.anthropic_compat.CONN_BACKOFF", (0, 0, 0))
    n = {"n": 0}

    def transport(url, headers, body):
        n["n"] += 1
        if n["n"] == 1:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")
        return 200, _sse_json(_anthropic_response([{"type": "text", "text": "ok"}]))

    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=transport)
    r = p.chat([{"role": "user", "content": "hi"}])
    assert r.text == "ok" and n["n"] == 2


def test_connection_reset_exhausts_then_raises_network_error(monkeypatch):
    """连接类独立预算（2026-10-01 事故修复）：CONN_RETRIES=4 → 共 4 次尝试后放弃
    （此前与 5xx 共用 2 次预算，一次 SSE 长连接重置即判死整轮）。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    monkeypatch.setattr("core.llm.anthropic_compat.CONN_BACKOFF", (0, 0, 0))
    n = {"n": 0}

    def transport(url, headers, body):
        n["n"] += 1
        raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")

    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=transport)
    with pytest.raises(LLMError, match="网络错误") as ei:
        p.chat([{"role": "user", "content": "hi"}])
    assert n["n"] == 4  # CONN_RETRIES=4：重试 3 次后放弃
    assert "已重试 3 次" in str(ei.value)


def test_retry_budget_per_category(monkeypatch):
    """按类别预算（2026-10-01）：429/5xx 仍 2 次尝试（保留快速失败基调）；
    连接类 4 次。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    monkeypatch.setattr("core.llm.anthropic_compat.CONN_BACKOFF", (0, 0, 0))
    s = {"n": 0}

    def t503(url, headers, body):
        s["n"] += 1
        return 503, {"error": {"message": "down"}}

    with pytest.raises(LLMError, match="503"):
        AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=t503).chat(
            [{"role": "user", "content": "hi"}])
    assert s["n"] == 2          # 5xx 维持 2 次

    c = {"n": 0}

    def treset(url, headers, body):
        c["n"] += 1
        raise ConnectionResetError(10054, "reset")

    with pytest.raises(LLMError, match="网络错误"):
        AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=treset).chat(
            [{"role": "user", "content": "hi"}])
    assert c["n"] == 4          # 连接类 4 次


def test_stream_break_without_delta_is_retried(monkeypatch):
    """流式建连后中途断开、但尚无任何增量输出 → 安全重试（重发不重复上屏）。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    monkeypatch.setattr("core.llm.anthropic_compat.CONN_BACKOFF", (0, 0, 0))
    n = {"n": 0}

    def transport(url, headers, body):
        n["n"] += 1
        if n["n"] == 1:
            return 200, _BreakingStream([], exc_after=0)
        return 200, _sse_json(_anthropic_response([{"type": "text", "text": "ok"}]))

    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=transport)
    r = p.chat([{"role": "user", "content": "hi"}], on_text=lambda _d: None)
    assert r.text == "ok" and n["n"] == 2


def test_stream_break_after_delta_is_not_retried(monkeypatch):
    """流式中途断开但已吐出增量 → 不重试（避免重复回调 on_text 造成重复上屏）。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    n = {"n": 0}
    chunk = json.dumps({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": "hi"}})

    def transport(url, headers, body):
        n["n"] += 1
        return 200, _BreakingStream([f"data: {chunk}"], exc_after=1)

    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=transport)
    with pytest.raises(LLMError, match="流式传输中断"):
        p.chat([{"role": "user", "content": "hi"}], on_text=lambda _d: None)
    assert n["n"] == 1


# ---------- 思考开关（2026-09-19 直播间终端化） ----------

def test_thinking_param_injected_when_enabled():
    """enable_thinking=True：请求 body 带 thinking 参数；默认关不注入。"""
    captured = []
    p = AnthropicCompatProvider(
        "https://fake", "key", "m", stream_transport=_fake_stream_transport(
            _anthropic_response([{"type": "thinking", "thinking": "推理中"},
                                 {"type": "text", "text": "好"}]), capture=captured),
        enable_thinking=True)
    r = p.chat([{"role": "user", "content": "hi"}])
    assert captured[0]["body"]["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert r.thinking == "推理中"

    captured2 = []
    p2 = AnthropicCompatProvider(
        "https://fake", "key", "m", stream_transport=_fake_stream_transport(
            _anthropic_response([{"type": "text", "text": "好"}]), capture=captured2))
    p2.chat([{"role": "user", "content": "hi"}])
    assert "thinking" not in captured2[0]["body"]


def test_thinking_param_fallback_on_400():
    """模型不认 thinking 参数（400 文案含 thinking）→ 去参重试成功且本实例不再注入。"""
    responses = iter([
        (400, {"error": {"message": "thinking is not supported by this model"}}),
        (200, _sse_json(_anthropic_response([{"type": "text", "text": "ok"}]))),
        (200, _sse_json(_anthropic_response([{"type": "text", "text": "ok"}]))),
    ])
    calls: list[dict] = []

    def transport(url, headers, body):
        calls.append(json.loads(body))
        return next(responses)

    p = AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=transport, enable_thinking=True)
    r = p.chat([{"role": "user", "content": "hi"}])
    assert r.text == "ok"
    assert "thinking" in calls[0] and "thinking" not in calls[1]
    p.chat([{"role": "user", "content": "hi"}])
    assert "thinking" not in calls[2]  # 实例级记住：后续不再注入


def test_reasoning_content_fallback():
    """OpenAI 式 reasoning_content 兜底：content 无 thinking block 时接住思考字段。"""
    stream_transport = _fake_stream_transport({
        "stop_reason": "end_turn", "model": "fake-model",
        "content": [{"type": "text", "text": "答"}],
        "reasoning_content": "隐藏推理链",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    p = AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=stream_transport)
    r = p.chat([{"role": "user", "content": "hi"}])
    assert r.thinking == "隐藏推理链" and r.text == "答"


def test_retry_on_timeout(monkeypatch):
    """网络超时同样重试（演练中真实发生的故障形态）。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    calls = {"n": 0}

    def transport(url, headers, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return 200, _sse_json(_anthropic_response([{"type": "text", "text": "recovered"}]))

    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=transport)
    assert p.chat([{"role": "user", "content": "hi"}]).text == "recovered"


def test_tool_result_message_roundtrip():
    """工具结果回填：Agent 循环必须能把 tool_result 拼回消息流（含 is_error）。"""
    from core.llm import ToolCall
    p = AnthropicCompatProvider("https://fake", "key", "m", transport=_fake_transport({}))
    tc = ToolCall(id="tc-9", name="run_cmd", arguments={"cmd": "id"})
    msg = p.tool_result_message(tc, "uid=0(root)", is_error=False)
    assert msg["content"][0]["tool_use_id"] == "tc-9"
    err = p.tool_result_message(tc, "权限不足", is_error=True)
    assert err["content"][0]["is_error"] is True


# ---------- 思考流式（2026-09-19：SSE stream:true，thinking_delta 逐帧回调） ----------

_SSE_LINES = [
    'event: message_start',
    'data: {"type":"message_start","message":{"usage":{"input_tokens":42}}}',
    '',
    'event: content_block_start',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"先想"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"一下"}}',
    'data: {"type":"content_block_stop","index":0}',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text"}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"答案"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}',
    'data: {"type":"message_stop"}',
]


def _sse_transport(lines=None, responses=None, capture=None):
    """伪 SSE stream transport：lines=成功响应的 SSE 行；responses=按次弹出的
    (status, payload|lines)。"""
    queue = list(responses or [])
    def transport(url, headers, body):
        if capture is not None:
            capture.append(json.loads(body))
        if queue:
            return queue.pop(0)
        return 200, iter(lines if lines is not None else _SSE_LINES)
    return transport


def test_stream_thinking_deltas_and_final_response():
    captured, deltas = [], []
    p = AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=_sse_transport(capture=captured),
                                transport=_fake_transport({}),
                                enable_thinking=True)
    r = p.chat([{"role": "user", "content": "hi"}],
               on_thinking=deltas.append, should_cancel=lambda: False)
    assert deltas == ["先想", "一下"]
    assert r.thinking == "先想一下" and r.text == "答案"
    assert r.stop_reason == "end_turn"
    assert r.usage.input_tokens == 42 and r.usage.output_tokens == 7
    assert captured[0]["stream"] is True and "thinking" in captured[0]


def test_stream_tool_use_assembly():
    """tool_use 增量 JSON（input_json_delta）拼回 input dict，_parse 出 ToolCall。"""
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"run_cmd"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\": \\"id\\""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":", \\"runtime\\": \\"host\\"}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":5}}',
    ]
    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=_sse_transport(lines))
    r = p.chat([{"role": "user", "content": "hi"}], on_thinking=lambda _: None)
    assert r.stop_reason == "tool_use"
    assert r.tool_calls[0].name == "run_cmd"
    assert r.tool_calls[0].arguments == {"cmd": "id", "runtime": "host"}


def test_stream_truncated_tool_args_raises_marked_error():
    """2026-09-20 事故：input_json_delta 只收到 {"cmd": 就结束（max_tokens 耗尽/
    网关断流）→ 抛带 truncated 标记的 LLMError（含 stop_reason），而非裸
    JSONDecodeError 穿到 agent 循环炸任务。"""
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"run_cmd"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":16384}}',
    ]
    p = AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=_sse_transport(lines))
    with pytest.raises(LLMError, match="截断") as ei:
        p.chat([{"role": "user", "content": "hi"}], on_thinking=lambda _: None)
    assert ei.value.truncated is True
    assert "max_tokens" in str(ei.value)


def test_stream_trailing_junk_tool_args_salvaged():
    """2026-10-01 事故：网关 SSE 尾部多发字符（stop_reason=tool_use 正常收尾却报
    "Extra data"）——JSON 本体完整，raw_decode 取首值忽略尾部冗余，不再抛错。"""
    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"run_cmd"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\": \\"id\\"}"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":3}}',
    ]
    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=_sse_transport(lines))
    r = p.chat([{"role": "user", "content": "hi"}], on_thinking=lambda _: None)
    assert r.stop_reason == "tool_use"
    assert r.tool_calls[0].name == "run_cmd"
    assert r.tool_calls[0].arguments == {"cmd": "id"}   # 尾部冗余被忽略


def test_stream_tool_args_non_dict_trailing_still_truncated():
    """首值非 dict（工具参数必为对象）时不抢救，仍抛带 truncated 标记的 LLMError。"""
    lines = [
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"run_cmd"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"123x"}}',
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
    ]
    p = AnthropicCompatProvider("https://fake", "key", "m", stream_transport=_sse_transport(lines))
    with pytest.raises(LLMError) as ei:
        p.chat([{"role": "user", "content": "hi"}], on_thinking=lambda _: None)
    assert ei.value.truncated is True


def test_default_max_tokens_raised_to_16384():
    """2026-09-20 事故修正：缺省 max_tokens 16384（原 4096 与思考 budget 8192
    倒挂，GLM 长思考+长工具参数被打爆截断）。"""
    captured = []
    p = AnthropicCompatProvider(
        "https://fake", "key", "m",
        stream_transport=_fake_stream_transport(
            _anthropic_response([{"type": "text", "text": "ok"}]), capture=captured))
    p.chat([{"role": "user", "content": "hi"}])
    assert captured[0]["body"]["max_tokens"] == 16384


def test_stream_cancel_between_frames():
    """should_cancel 逐行轮询：命中即抛 LLMError 且流被关闭（不再读后续帧）。"""
    closed = {"n": 0}

    class FakeStream:
        def __init__(self):
            self._lines = iter(_SSE_LINES)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._lines)

        def close(self):
            closed["n"] += 1

    p = AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=lambda *a: (200, FakeStream()))
    with pytest.raises(LLMError, match="已中断"):
        p.chat([{"role": "user", "content": "hi"}],
               on_thinking=lambda _: None, should_cancel=lambda: True)
    assert closed["n"] == 1


def test_stream_non_sse_json_fallback():
    """网关无视 stream:true 返回整份 JSON → 兜底按普通响应解析，不报错。"""
    body = json.dumps(_anthropic_response([{"type": "thinking", "thinking": "整份思考"}]))
    p = AnthropicCompatProvider(
        "https://fake", "key", "m",
        stream_transport=lambda *a: (200, iter([body])))
    r = p.chat([{"role": "user", "content": "hi"}], on_thinking=lambda _: None)
    assert r.thinking == "整份思考" and r.text == ""


def test_stream_thinking_400_degrade_still_works():
    """流式模式下 thinking 400 去参降级：去 thinking 参数后以流式重试成功。"""
    captured = []
    p = AnthropicCompatProvider(
        "https://fake", "key", "m",
        stream_transport=_sse_transport(
            responses=[(400, {"error": {"message": "thinking is not supported"}})],
            capture=captured),
        transport=_fake_transport({}),
        enable_thinking=True)
    r = p.chat([{"role": "user", "content": "hi"}],
               on_thinking=lambda _: None, should_cancel=lambda: False)
    assert r.thinking == "先想一下"
    assert "thinking" in captured[0] and "thinking" not in captured[1]


def test_stream_disabled_falls_back_to_plain_transport():
    """_stream_disabled 置位后 chat 走非流式 transport（不再发 stream:true）。"""
    captured = []
    p = AnthropicCompatProvider(
        "https://fake", "key", "m",
        stream_transport=_sse_transport(responses=[
            (400, {"error": {"message": "stream is not supported"}})]),
        transport=_fake_transport(
            _anthropic_response([{"type": "text", "text": "好"}]), capture=captured))
    r = p.chat([{"role": "user", "content": "hi"}], on_thinking=lambda _: None)
    assert r.text == "好"
    assert "stream" not in captured[0]
    assert p._stream_disabled is True


def test_all_calls_stream_even_without_callbacks():
    """2026-09-28 全走流式：无 on_thinking/on_text 回调也走 SSE 传输——planner/
    intel 等无回调调用不再吃非流式「总等待」超时（长思考 6-10 分钟 > 600s 才超时
    且重试注定失败），帧间隔超时天然抗长思考。"""
    captured = []
    p = AnthropicCompatProvider(
        "https://fake", "key", "m",
        stream_transport=_fake_stream_transport(
            _anthropic_response([{"type": "text", "text": "ok"}]), capture=captured))
    r = p.chat([{"role": "user", "content": "hi"}])
    assert r.text == "ok"
    assert captured[0]["body"]["stream"] is True  # 无回调也发 stream:true


# ---------- 模型路由 ----------

def test_router_defaults_and_override():
    r = ModelRouter()
    assert r.model_for("executor") == "ark-code-latest"
    assert r.model_for("classifier") == "deepseek-v4-flash"
    r2 = ModelRouter(models={"executor": "glm-5-3-flash-260828"})
    assert r2.model_for("executor") == "glm-5-3-flash-260828"
    with pytest.raises(ValueError, match="未知模型角色"):
        ModelRouter(models={"ceo": "x"})


def test_router_config_file(tmp_path):
    cfg = tmp_path / "llm.json"
    cfg.write_text(json.dumps({"classifier": "glm-5-3-flash-260828"}), encoding="utf-8")
    r = ModelRouter(config_path=cfg)
    assert r.model_for("classifier") == "glm-5-3-flash-260828"
    assert r.model_for("planner") == "ark-code-latest"  # 未覆盖角色保持默认


def test_router_target_for_provider_dict(tmp_path):
    """llm.json 文件级覆写：裸模型名→(None, model)；{provider,model}→(provider, model)。"""
    cfg = tmp_path / "llm.json"
    cfg.write_text(json.dumps({
        "executor": {"provider": "ark-plan", "model": "glm-5-3-flash-260828"},
        "planner": "deepseek-v4-flash",
    }), encoding="utf-8")
    r = ModelRouter(config_path=cfg)
    assert r.target_for("executor") == ("ark-plan", "glm-5-3-flash-260828")
    assert r.target_for("planner") == (None, "deepseek-v4-flash")
    assert r.target_for("classifier") is None  # 未覆写 → 走全局默认


# ---------- 供应商管理（DESIGN.md §8） ----------

def test_provider_store_seed_mask_default(tmp_path):
    from core.llm.providers import ProviderStore
    s = ProviderStore(tmp_path / "providers.json")
    assert [p["name"] for p in s.load()] == ["ark-coding", "ark-plan"]
    assert all("api_key" not in p for p in s.masked())  # 读出脱敏
    assert s.default_target() == ("ark-coding", "ark-code-latest")


def test_provider_store_save_validation_and_key_keep(tmp_path):
    from core.llm.providers import ProviderError, ProviderStore
    s = ProviderStore(tmp_path / "providers.json")
    base = [
        {"name": "ark-coding", "base_url": "https://x/api/", "api_key": "",
         "models": ["m1"], "enabled": True},
        {"name": "ark-plan", "base_url": "https://y/api", "api_key": "secret",
         "models": ["m2", "m3"], "enabled": False},
    ]
    s.save(base)
    # 空 key 保存 → 沿用原 key
    again = [dict(p, api_key="") for p in base]
    s.save(again)
    assert s.get("ark-plan")["api_key"] == "secret"
    # base_url 去尾斜杠；plan 停用中，默认仍是首个启用的 coding
    assert s.get("ark-coding")["base_url"] == "https://x/api"
    assert s.default_target() == ("ark-coding", "m1")
    # 校验三连
    with pytest.raises(ProviderError):
        s.save([dict(base[0], name="bad name"), dict(base[1], enabled=True)])
    with pytest.raises(ProviderError):
        s.save([dict(base[0], models=[]), dict(base[1], enabled=True)])
    with pytest.raises(ProviderError):
        s.save([dict(base[0], enabled=False), dict(base[1], enabled=False)])


def test_provider_store_build(tmp_path, monkeypatch):
    from core.llm.providers import ProviderError, ProviderStore
    monkeypatch.setenv("ARK_API_KEY", "env-key")
    s = ProviderStore(tmp_path / "providers.json")
    p = s.build()  # 全局默认（ark-coding，key 空 → env）
    assert p.base_url.endswith("/api/coding") and p.model == "ark-code-latest"
    assert p.api_key == "env-key"
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a", "b"], "enabled": True}])
    p2 = s.build("ark-coding", "b")
    assert p2.api_key == "k1" and p2.model == "b"
    with pytest.raises(ProviderError):
        s.build("nope")
    # 停用 coding（plan 仍启用）→ build 拒、默认转到 plan
    s.save([
        {"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
         "models": ["a"], "enabled": False},
        {"name": "ark-plan", "base_url": "https://y/api", "api_key": "",
         "models": ["p"], "enabled": True},
    ])
    with pytest.raises(ProviderError):
        s.build("ark-coding")  # 已停用
    assert s.default_target() == ("ark-plan", "p")


def test_provider_store_model_context(tmp_path, monkeypatch):
    """每模型最大上下文（F16）：_validate 归一（非法/未勾选剔除）、build 带给实例、
    未声明= None。"""
    from core.llm.providers import ProviderStore
    monkeypatch.setenv("ARK_API_KEY", "env-key")
    s = ProviderStore(tmp_path / "providers.json")
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a", "b"], "enabled": True,
             "model_context": {"a": 128000, "b": -5, "c": 999, "a2": "x"}}])
    p = s.get("ark-coding")
    # 只留已勾选模型且为正整数的项（c 未勾选、b 非法、"a2" 非数）
    assert p["model_context"] == {"a": 128000}
    assert s.build("ark-coding", "a").context_tokens == 128000
    assert s.build("ark-coding", "b").context_tokens is None
    # 缺省条目 → 空 dict，build 仍可用
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a"], "enabled": True}])
    assert s.get("ark-coding")["model_context"] == {}


def test_provider_store_ctx_governance_fields(tmp_path, monkeypatch):
    """上下文治理字段（2026-09-30 ct-7）：_validate 显式才落、缺省不写；build 把
    window/软上限/摘要模型透传到 provider 实例（旧坑：白名单漏登记 → 设置页保存
    一次即丢，与 thinking 同源）。"""
    from core.llm.providers import ProviderStore
    monkeypatch.setenv("ARK_API_KEY", "env-key")
    s = ProviderStore(tmp_path / "providers.json")
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a"], "enabled": True,
             "model_window": 1048566, "ctx_soft_budget": 512000,
             "summarizer_model": "cheap-model"}])
    p = s.get("ark-coding")
    assert p["model_window"] == 1048566
    assert p["ctx_soft_budget"] == 512000
    assert p["summarizer_model"] == "cheap-model"
    # 保存 → 读回不丢（masked 出参也透传）
    assert s.masked()[0]["model_window"] == 1048566
    inst = s.build("ark-coding", "a")
    assert inst.context_tokens == 1048566
    assert inst.ctx_soft_budget == 512000
    assert inst.summarizer_model == "cheap-model"
    # 缺省：三者均不落字段，build 实例对应属性为 None（运行时走模块常量兜底）
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a"], "enabled": True}])
    p2 = s.get("ark-coding")
    assert "model_window" not in p2 and "ctx_soft_budget" not in p2
    assert "summarizer_model" not in p2
    inst2 = s.build("ark-coding", "a")
    assert inst2.context_tokens is None and inst2.ctx_soft_budget is None
    assert inst2.summarizer_model is None


def test_provider_ctx_window_priority_and_invalid_dropped(tmp_path, monkeypatch):
    """window 取值优先级（ct-7）：provider 级 model_window 优先于按模型的
    model_context[model]；非法值（非数/非正/超大/空白）静默剔除。"""
    from core.llm.providers import ProviderStore
    monkeypatch.setenv("ARK_API_KEY", "env-key")
    s = ProviderStore(tmp_path / "providers.json")
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a"], "enabled": True,
             "model_window": -1, "ctx_soft_budget": "x", "summarizer_model": "   "}])
    p = s.get("ark-coding")
    assert "model_window" not in p and "ctx_soft_budget" not in p
    assert "summarizer_model" not in p
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a"], "enabled": True, "model_context": {"a": 128000}}])
    assert s.build("ark-coding", "a").context_tokens == 128000
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a"], "enabled": True,
             "model_window": 1048566, "model_context": {"a": 128000}}])
    assert s.build("ark-coding", "a").context_tokens == 1048566  # provider 级胜出


def test_apply_context_budget(tmp_path):
    """模型声明上下文 → 会话预算换算（tokens×2 字符，摘要阈值取半）；
    未声明=默认 256K（预算 512k 字符）。"""
    from core.agent.loop import AgentConfig, apply_context_budget

    cfg = AgentConfig()
    apply_context_budget(cfg, type("L", (), {"context_tokens": 128000})())
    assert cfg.context_char_budget == 256000
    assert cfg.context_summary_chars == 128000
    # 未声明（None）/无属性 → 默认 256K
    cfg2 = AgentConfig()
    apply_context_budget(cfg2, type("L", (), {"context_tokens": None})())
    apply_context_budget(cfg2, object())
    assert cfg2.context_char_budget == 512_000
    assert cfg2.context_summary_chars == 256_000


def test_provider_store_default_provider_selection(tmp_path):
    """可选默认供应商：default_provider 优先于「第一个启用供应商」；
    未启用/不存在 → 422；未设置 → 旧行为兜底。"""
    from core.llm.providers import ProviderError, ProviderStore
    s = ProviderStore(tmp_path / "providers.json")
    base = [
        {"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
         "models": ["a"], "enabled": True},
        {"name": "ark-plan", "base_url": "https://y/api", "api_key": "k2",
         "models": ["p"], "enabled": True},
    ]
    s.save(base)  # 未设置 default_provider → 第一个启用供应商兜底
    assert s.default_target() == ("ark-coding", "a")
    # 显式选 ark-plan 为默认（顺序不变，coding 仍是列表第一个）
    s.save(base, default_provider="ark-plan")
    assert s.default_provider_name() == "ark-plan"
    assert s.default_target() == ("ark-plan", "p")
    # 停用被选中的默认供应商 → 保存 422
    disabled = [dict(base[0], enabled=False), dict(base[1], enabled=True)]
    with pytest.raises(ProviderError, match="已停用"):
        s.save(disabled, default_provider="ark-coding")
    # default_provider 不在清单 → 422
    with pytest.raises(ProviderError, match="不存在"):
        s.save(base, default_provider="nope")
    # 不传 default_provider → 沿用已存设置
    s.save(base)
    assert s.default_provider_name() == "ark-plan"
    # build() 跟随默认供应商
    assert s.build().model == "p"


def test_provider_discover_listed_and_fallback(tmp_path):
    """发现：/v1/models 命中→过滤 Shutdown；404→候选探活；401→报错。"""
    from core.llm.providers import ProviderError, ProviderStore
    s = ProviderStore(tmp_path / "providers.json")

    def http_listed(url, headers, timeout=20.0):
        return 200, {"data": [
            {"id": "live-model", "status": "Running"},
            {"id": "dead-model", "status": "Shutdown"},
        ]}

    out = s.discover("ark-coding", http_get=http_listed)
    assert out["listed"] is True and [m["id"] for m in out["models"]] == ["live-model"]

    def http_404(url, headers, timeout=20.0):
        return 404, None

    prober = lambda model: model == "deepseek-v4-flash"  # noqa: E731
    out = s.discover("ark-coding", http_get=http_404, prober=prober)
    assert out["listed"] is False and out["probed"] == ["deepseek-v4-flash"]

    def http_401(url, headers, timeout=20.0):
        return 401, {"error": {"message": "bad key"}}

    with pytest.raises(ProviderError, match="401"):
        s.discover("ark-coding", http_get=http_401)


# ---------- 密钥解析 ----------

def test_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# 注释\nARK_API_KEY=ark-test-123\nEMPTY=\n", encoding="utf-8")
    vals = load_dotenv(env)
    assert vals["ARK_API_KEY"] == "ark-test-123"
    assert "EMPTY" not in vals or vals["EMPTY"] == ""


def test_ark_key_resolution_order(tmp_path, monkeypatch):
    """显式参数 > 环境变量 > .env（DESIGN.md 密钥不入库）。"""
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    from core.llm.ark import resolve_api_key
    # 1) .env 兜底
    (tmp_path / ".env").write_text("ARK_API_KEY=ark-from-dotenv\n", encoding="utf-8")
    assert resolve_api_key() == "ark-from-dotenv"
    # 2) 环境变量优先于 .env
    monkeypatch.setenv("ARK_API_KEY", "ark-from-env")
    assert resolve_api_key() == "ark-from-env"
    # 3) 显式参数最高
    assert resolve_api_key("ark-explicit") == "ark-explicit"
    # 4) 全无 → 报错
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    (tmp_path / ".env").unlink()
    with pytest.raises(ValueError, match="ARK_API_KEY"):
        resolve_api_key()


def test_ark_provider_defaults():
    captured = []
    stream_transport = _fake_stream_transport(
        _anthropic_response([{"type": "text", "text": "ok"}]), capture=captured)
    p = ArkCodingProvider(api_key="ark-x", stream_transport=stream_transport,
                          model="deepseek-v4-flash")
    p.chat([{"role": "user", "content": "hi"}])
    assert captured[0]["url"].startswith("https://ark.cn-beijing.volces.com/api/coding")
    assert captured[0]["body"]["model"] == "deepseek-v4-flash"


def test_openai_chat_completions_protocol():
    from core.llm.openai_compat import OpenAICompatProvider
    captured = []
    response = {"choices": [{"message": {"content": "ok", "tool_calls": [{
        "id": "call-1", "function": {"name": "run_cmd", "arguments": '{"cmd":"id"}'}}]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    p = OpenAICompatProvider("https://fake", "key", "m", stream_transport=_fake_stream_transport(response, capture=captured))
    result = p.chat([{"role": "user", "content": "hi"}], system="sys", tools=[{"name": "run_cmd", "input_schema": {"type": "object"}}])
    req = captured[0]
    assert req["url"] == "https://fake/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer key"
    assert req["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert req["body"]["tools"][0]["type"] == "function"
    assert result.tool_calls[0].arguments["cmd"] == "id"


def test_openai_responses_protocol_and_tool_result():
    from core.llm.openai_compat import OpenAICompatProvider
    captured = []
    response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]},
                             {"type": "function_call", "call_id": "call-1", "name": "run_cmd", "arguments": '{"cmd":"id"}'}],
                "status": "completed"}
    p = OpenAICompatProvider("https://fake", "key", "m", format="openai-responses",
                             stream_transport=_fake_stream_transport(response, capture=captured))
    result = p.chat([{"role": "user", "content": "hi"}])
    assert captured[0]["url"] == "https://fake/v1/responses"
    assert result.text == "ok" and result.tool_calls[0].name == "run_cmd"
    msg = p.tool_result_message(result.tool_calls[0], "uid=0")
    assert msg["content"][0]["tool_use_id"] == "call-1"


def test_openai_responses_sse_ignores_event_metadata():
    """Responses SSE emits event metadata lines before each JSON data frame."""
    events = [
        "event: response.created",
        'data: {"type":"response.created"}',
        "event: response.output_text.delta",
        'data: {"type":"response.output_text.delta","delta":"OK"}',
        "event: response.completed",
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "data: [DONE]",
    ]

    def stream_transport(url, headers, body):
        return 200, iter(events)

    from core.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider("https://fake", "key", "m",
                             format="openai-responses",
                             stream_transport=stream_transport)
    assert p.chat([{"role": "user", "content": "hi"}]).text == "OK"


def test_openai_responses_sse_recovers_tool_name_from_done_events():
    """兼容中转站：added 只有 id，工具 name/arguments 延迟到 done/completed。"""
    events = [
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_1","call_id":"call_1"}}',
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1","delta":"{\\"cmd\\":\\"id\\"}"}',
        'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"fc_1","call_id":"call_1","name":"run_cmd","arguments":"{\\"cmd\\":\\"id\\"}"}}',
        'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"function_call","id":"fc_1","call_id":"call_1","name":"run_cmd","arguments":"{\\"cmd\\":\\"id\\"}"}]}}',
        "data: [DONE]",
    ]

    from core.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider("https://fake", "key", "m",
                             format="openai-responses",
                             stream_transport=lambda *args: (200, iter(events)))
    result = p.chat([{"role": "user", "content": "hi"}])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "run_cmd"
    assert result.tool_calls[0].arguments == {"cmd": "id"}


def test_openai_responses_sse_rejects_tool_call_without_name():
    events = [
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc_missing","call_id":"call_missing"}}',
        'data: {"type":"response.function_call_arguments.delta","item_id":"fc_missing","delta":"{}"}',
        'data: {"type":"response.completed","response":{"status":"completed"}}',
        "data: [DONE]",
    ]

    from core.llm.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider("https://fake", "key", "m",
                             format="openai-responses",
                             stream_transport=lambda *args: (200, iter(events)))
    with pytest.raises(LLMError, match="缺少工具名.*call_missing"):
        p.chat([{"role": "user", "content": "hi"}])


def test_openai_524_retries_until_success(monkeypatch):
    from core.llm import openai_compat

    attempts = []
    retries = []
    response = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "OK"}]}], "status": "completed"}

    def transport(url, headers, body):
        attempts.append(1)
        if len(attempts) < 3:
            return 524, {"error": {"message": "origin timeout"}}
        return 200, iter([json.dumps(response)])

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    p = openai_compat.OpenAICompatProvider(
        "https://fake", "key", "m", format="openai-responses",
        stream_transport=transport)
    assert p.chat([{"role": "user", "content": "hi"}],
                   on_retry=lambda *args: retries.append(args)).text == "OK"
    assert len(attempts) == 3
    assert retries == [(1, 5, 524), (2, 5, 524)]


def test_openai_524_stops_after_five_total_attempts(monkeypatch):
    from core.llm import openai_compat

    attempts = []

    def transport(url, headers, body):
        attempts.append(1)
        return 524, {"error": {"message": "origin timeout"}}

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    p = openai_compat.OpenAICompatProvider(
        "https://fake", "key", "m", format="openai-responses",
        stream_transport=transport)
    with pytest.raises(LLMError, match="HTTP 524"):
        p.chat([{"role": "user", "content": "hi"}])
    assert len(attempts) == 5


def test_openai_connection_reset_retries_until_success(monkeypatch):
    from core.llm import openai_compat

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    attempts, retries = [], []
    response = {"choices": [{"message": {"content": "OK"},
                              "finish_reason": "stop"}]}

    def transport(url, headers, body):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")
        return 200, iter([json.dumps(response)])

    p = openai_compat.OpenAICompatProvider(
        "https://fake", "key", "m", stream_transport=transport,
        format="openai-chat-completions")
    assert p.chat([{"role": "user", "content": "hi"}],
                   on_retry=lambda *args: retries.append(args)).text == "OK"
    assert len(attempts) == 3
    assert retries == [(1, 5, "connection"), (2, 5, "connection")]


def test_openai_connection_reset_stops_after_five_total_attempts(monkeypatch):
    from core.llm import openai_compat

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    attempts = []

    def transport(url, headers, body):
        attempts.append(1)
        raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。")

    p = openai_compat.OpenAICompatProvider(
        "https://fake", "key", "m", stream_transport=transport)
    with pytest.raises(LLMError, match="网络连接失败.*已重试 4 次"):
        p.chat([{"role": "user", "content": "hi"}])
    assert len(attempts) == 5


def test_openai_stream_read_timeout_retries_until_success(monkeypatch):
    """响应头已收到后，SSE 读取阶段的 read timeout 也必须有限重试。"""
    from core.llm import openai_compat

    monkeypatch.setattr(openai_compat.time, "sleep", lambda _: None)
    attempts, retries = [], []
    response = {"choices": [{"message": {"content": "OK"},
                              "finish_reason": "stop"}]}

    def stream_transport(url, headers, body):
        attempts.append(1)
        if len(attempts) == 1:
            def broken_stream():
                raise TimeoutError("The read operation timed out")
                yield  # pragma: no cover
            return 200, broken_stream()
        return 200, iter([json.dumps(response)])

    p = openai_compat.OpenAICompatProvider(
        "https://fake", "key", "m", stream_transport=stream_transport)
    assert p.chat([{"role": "user", "content": "hi"}],
                   on_retry=lambda *args: retries.append(args)).text == "OK"
    assert len(attempts) == 2
    assert retries == [(1, 5, "stream")]


def test_anthropic_messages_uses_x_api_key():
    captured = []
    p = AnthropicCompatProvider("https://fake", "key", "m",
                                stream_transport=_fake_stream_transport(
                                    _anthropic_response([{"type": "text", "text": "ok"}]), capture=captured))
    p.chat([{"role": "user", "content": "hi"}])
    assert captured[0]["url"].endswith("/v1/messages")
    assert captured[0]["headers"]["x-api-key"] == "key"
    assert "Authorization" not in captured[0]["headers"]
