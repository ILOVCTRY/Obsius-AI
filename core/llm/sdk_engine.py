"""官方 SDK 引擎（Anthropic / OpenAI）+ 原始字节 tee 垫片。

设计（2026-10-03 LLM SDK 迁移）：连接 / 代理 / UA / HTTP 状态分类 / 超时 / SSE
事件解析交给官方 SDK；本模块把 SDK 的流式事件**还原成 SSE 行**交给上层
`AnthropicCompatProvider._consume_stream` 复用——Ark 特化（截断 raw_decode 容错、
增量探针、逐帧回调、文案嗅探）全部零重写保留。

对 Ark 网关尾部冗余（SDK 的 JSON 解析会抛 `json.JSONDecodeError`）：
`TeeTransport` 已把原始字节旁路录制，`_SdkStream` 据此抛 `RawFallback`，
让上层用原始字节重走自研 SSE 解析（保住 raw_decode 抢救能力）。

对外契约与旧 `Transport` / `StreamTransport` 完全一致：
    transport(url, headers, body) -> (status, dict)
    stream_transport(url, headers, body) -> (status, 响应流 | 错误 dict)
所以 compat 层的 chat() 主循环一行不改，只是 transport 来源换成 SDK。
"""

import json
import logging
import threading
from typing import Any, Callable

import httpx

from core.llm.provider import TransientStreamError

log = logging.getLogger(__name__)

# 流内错误帧里判定「瞬时、值得重试」的文案特征（大小写不敏感）。中转站/网关的
# 瞬时故障文案（upstream_error / service unavailable / overloaded / 5xx 网关）——
# 内容策略、参数非法等硬错误不在列，原样上抛快速失败。
TRANSIENT_STREAM_HINTS = (
    "upstream", "temporarily unavailable", "service unavailable",
    "bad gateway", "gateway timeout", "overloaded",
)

Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict[str, Any]]]
StreamTransport = Callable[[str, dict[str, str], bytes], tuple[int, Any]]

# UA 与 anthropic_compat.CLIENT_USER_AGENT 同值。刻意不 import 它：
# anthropic_compat 需要 import 本模块的工厂，反向 import 会成环。
CLIENT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class RawFallback(Exception):
    """SDK 事件解析失败（Ark SSE 尾部冗余等）——携带 tee 录制的原始字节，
    上层据此改用自研 SSE 解析（raw_decode 容错）重新消费。"""

    def __init__(self, raw: bytes):
        super().__init__("SDK 事件解析失败，降级原始字节重解析")
        self.raw = raw

    @property
    def lines(self) -> list[bytes]:
        return self.raw.splitlines(keepends=True)


class _TeeStream(httpx.SyncByteStream):
    """包装底层字节流：边转发给 SDK，边把原始 chunk 追加到 capture。"""

    def __init__(self, inner: Any, capture: list[bytes]):
        self._inner = inner
        self._capture = capture

    def __iter__(self):
        for chunk in self._inner:
            self._capture.append(chunk)
            yield chunk

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()


class TeeTransport(httpx.BaseTransport):
    """包住真实 transport，流式响应的原始字节旁路录制到 thread-local 缓冲。

    thread-local 是为了「同一 provider 实例被多个会话线程复用」时各调各录，
    不串扰（同步 SDK 调用始终在当前线程完成请求 + 迭代）。"""

    def __init__(self, inner: httpx.BaseTransport):
        self._inner = inner
        self._local = threading.local()

    def capture(self) -> list[bytes]:
        buf = getattr(self._local, "buf", None)
        if buf is None:
            buf = []
            self._local.buf = buf
        return buf

    def reset(self) -> None:
        self._local.buf = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self._inner.handle_request(request)
        if resp.stream is not None:
            return httpx.Response(
                resp.status_code, headers=resp.headers,
                stream=_TeeStream(resp.stream, self.capture()),
                extensions=resp.extensions)
        return resp


def _ev_to_sse(ev: Any) -> bytes:
    """SDK 事件对象 → 单行 SSE 字节（`data: {...}\\n`），与真实 SSE 同形。"""
    data = ev.model_dump(exclude_none=True)
    return ("data: " + json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")


class _SdkStream:
    """把 SDK 流式事件迭代器包装成「逐行 SSE 字节」迭代器，供 _consume_stream 复用。

    两种降级为原始字节重解析的情形（都交给上层自研 SSE 解析）：
    1. SDK 迭代抛 JSONDecodeError——Ark 尾部冗余；
    2. SDK 一个事件都没产出——SDK 的 SSE 解码器按 `event:` 行分发，缺 `event:`
       行的网关（或无视 stream:true 直接回整份 JSON 的网关）会被静默丢空。
    保住 raw_decode 容错与非 SSE 兜底，避免静默返回空响应。"""

    def __init__(self, sdk_stream: Any, tee: TeeTransport):
        self._s = sdk_stream
        self._tee = tee

    def __iter__(self):
        emitted = 0
        try:
            for ev in self._s:
                emitted += 1
                yield _ev_to_sse(ev)
        except json.JSONDecodeError as e:
            raw = b"".join(self._tee.capture())
            if raw:
                log.warning("SDK 事件解析失败（%s），降级原始字节重解析（%d 字节）",
                            e, len(raw))
                raise RawFallback(raw) from e
            raise
        except Exception as e:  # noqa: BLE001 —— SDK 流内异常转连接级重试语义
            raise _as_conn_error(e) from e
        if emitted == 0:
            raw = b"".join(self._tee.capture())
            if raw:
                log.warning("SDK 未产出任何事件（网关 SSE 缺 event: 行或忽略 stream），"
                            "降级原始字节重解析（%d 字节）", len(raw))
                raise RawFallback(raw)

    def close(self) -> None:
        close = getattr(self._s, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass


def _as_conn_error(e: Exception) -> Exception:
    """SDK / httpx 连接类异常 → 现有 chat() 重试分支认得的 ConnectionError/TimeoutError。

    除 SDK 包装的 APIConnectionError/APITimeoutError，还须兜住**裸 httpx 传输异常**：
    流式响应体的读取发生在 SDK 的 `Stream` 迭代内（httpx 的 `send(stream=True)`
    只读响应头，body 延迟到 `iter_bytes`），SDK 的 `Stream.__stream__` 只 `finally`
    关流、**不包异常**——对端半途关连接会直接抛
    `httpx.RemoteProtocolError("peer closed connection without sending complete
    message body (incomplete chunked read)")`。不在此归类则它裸穿到上层：既绕开
    连接重试预算，又被错误分类器判成 unknown「执行异常」（2026-10-06 事故）。
    `httpx.TimeoutException` 是 `httpx.TransportError` 子类，故先判超时。

    **流内错误帧**（HTTP 200 已建连、SSE 里发 `{"error": {...}}`）：SDK 在迭代中抛
    `APIError`/`APIStatusError`（`{"error":{"type":"upstream_error"}}` 这类中转站
    瞬时故障），既不是传输异常也带不上 status，此前裸穿 → 不重试 + unknown
    （2026-10-07 事故）。仅**瞬时类文案**转 `TransientStreamError`（ConnectionError
    子类，被既有「未吐增量则安全重试」分支接住）；内容策略等硬错误原样上抛。"""
    import anthropic
    import openai
    if isinstance(e, (anthropic.APITimeoutError, openai.APITimeoutError,
                      httpx.TimeoutException)):
        return TimeoutError(str(e))
    if isinstance(e, (anthropic.APIConnectionError, openai.APIConnectionError,
                      httpx.TransportError)):
        return ConnectionError(str(e))
    if isinstance(e, (anthropic.APIError, openai.APIError)):
        low = str(e).lower()
        if any(h in low for h in TRANSIENT_STREAM_HINTS):
            return TransientStreamError(str(e))
    return e


def _error_dict(e: Any) -> dict[str, Any]:
    """SDK APIStatusError → 现有分支消费的 {"error": {"code","message"}} 形态。"""
    resp = getattr(e, "response", None)
    body: Any = None
    if resp is not None:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = None
    if isinstance(body, dict) and "error" in body:
        return body
    msg = ""
    if resp is not None:
        msg = getattr(resp, "text", "") or ""
    return {"error": {"code": "", "message": (msg or str(e))[:500]}}


def build_httpx_client(timeout: float, proxy: str | None = None) -> tuple[httpx.Client, TeeTransport]:
    """构造带 tee 的 httpx.Client（连接池复用 + 原始字节旁路录制）。"""
    inner: httpx.BaseTransport = (
        httpx.HTTPTransport(proxy=proxy) if proxy else httpx.HTTPTransport())
    tee = TeeTransport(inner)
    client = httpx.Client(transport=tee, timeout=timeout)
    return client, tee


def build_anthropic_transports(
    base_url: str, api_key: str, api_version: str, use_x_api_key: bool,
    timeout: float, proxy: str | None = None,
) -> tuple[Transport, StreamTransport]:
    """返回 (transport, stream_transport)，签名与旧 urllib 版一致。

    鉴权头必须由 SDK 自持凭据产生，**不能**只写进 default_headers：
    anthropic SDK 会把 `X-Api-Key: <api_key>` / `Authorization: Bearer <auth_token>`
    与自定义头**拼接合并**（`x-api-key: a, b`），且 api_key/auth_token 为 None 时
    会回退读 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`
    环境变量——本机 .env 正有 `ANTHROPIC_AUTH_TOKEN`（真实 Ark key），会串进
    无关供应商的请求。故用 `anthropic.Omit` 显式关掉另一条鉴权通道并阻断 env 回退。
    """
    import anthropic
    from anthropic import Omit

    http_client, tee = build_httpx_client(timeout, proxy)
    auth: dict[str, Any] = {"auth_token": Omit()}  # 缺省不发 Authorization
    if use_x_api_key:
        auth["api_key"] = api_key or "placeholder"
    else:
        auth["api_key"] = Omit()                   # 关掉 x-api-key 通道
        auth["auth_token"] = api_key or "placeholder"
    client = anthropic.Anthropic(
        **auth, base_url=base_url.rstrip("/"),
        http_client=http_client, max_retries=0, timeout=timeout,
        default_headers={"User-Agent": CLIENT_USER_AGENT,
                         "anthropic-version": api_version},
    )
    # Omit 会以 `Bearer <anthropic.Omit object at 0x...>` 的形式落到 Authorization
    # 头——必须在构造过 _validate_headers 之后置 None，否则线上会带一条垃圾头。
    if use_x_api_key:
        client.auth_token = None
    else:
        client.api_key = None

    def _req(body: bytes) -> dict[str, Any]:
        req = json.loads(body)
        req.pop("stream", None)  # stream 由本层显式控制
        return req

    def _transport(url, hdrs, body) -> tuple[int, dict[str, Any]]:
        try:
            msg = client.messages.create(**_req(body))
            return 200, msg.model_dump(exclude_none=True)
        except anthropic.APIStatusError as e:
            return e.status_code, _error_dict(e)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise _as_conn_error(e) from e
        except anthropic.AnthropicError as e:
            raise ConnectionError(str(e)) from e

    def _stream_transport(url, hdrs, body) -> tuple[int, Any]:
        tee.reset()
        try:
            stream = client.messages.create(**_req(body), stream=True)
            return 200, _SdkStream(stream, tee)
        except anthropic.APIStatusError as e:
            return e.status_code, _error_dict(e)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise _as_conn_error(e) from e
        except anthropic.AnthropicError as e:
            raise ConnectionError(str(e)) from e

    return _transport, _stream_transport


def build_openai_transports(
    base_url: str, api_key: str, timeout: float, proxy: str | None = None,
) -> tuple[Transport, StreamTransport]:
    """OpenAI 兼容路径（Chat Completions / Responses 双格式共用 SDK 引擎）。

    `api_key` 显式传入（含空串占位）以阻断 `OPENAI_API_KEY` / `OPENAI_BASE_URL`
    环境变量回退——供应商的 key 只该来自 providers.json 或 ARK_API_KEY。"""
    import openai

    http_client, tee = build_httpx_client(timeout, proxy)
    client = openai.OpenAI(
        api_key=api_key or "placeholder", base_url=base_url.rstrip("/") + "/v1",
        http_client=http_client, max_retries=0, timeout=timeout,
        default_headers={"User-Agent": CLIENT_USER_AGENT},
    )

    def _transport(url, hdrs, body) -> tuple[int, dict[str, Any]]:
        req = json.loads(body)
        req.pop("stream", None)
        try:
            if url.endswith("/responses"):
                resp = client.responses.create(**req)
            else:
                resp = client.chat.completions.create(**req)
            return 200, resp.model_dump(exclude_none=True)
        except openai.APIStatusError as e:
            return e.status_code, _error_dict(e)
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise _as_conn_error(e) from e
        except openai.OpenAIError as e:
            raise ConnectionError(str(e)) from e

    def _stream_transport(url, hdrs, body) -> tuple[int, Any]:
        tee.reset()
        req = json.loads(body)
        req.pop("stream", None)
        try:
            if url.endswith("/responses"):
                stream = client.responses.create(**req, stream=True)
            else:
                stream = client.chat.completions.create(**req, stream=True)
            return 200, _SdkStream(stream, tee)
        except openai.APIStatusError as e:
            return e.status_code, _error_dict(e)
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise _as_conn_error(e) from e
        except openai.OpenAIError as e:
            raise ConnectionError(str(e)) from e

    return _transport, _stream_transport
