"""LLM 传输层共享重试策略常量与可中断退避。"""

import time

from core.llm.provider import ContextOverflowError, LLMError

# 仅把已知的上游暂态状态纳入标准 5xx 重试；鉴权/参数类 5xx 不盲目重放。
HTTP_5XX_STATUS = frozenset({500, 502, 503, 504})
# 标准 5xx：共 11 次尝试（首次 + 10 次重试），退避封顶 30s——Anthropic/OpenAI
# 两条路径统一口径（此前 Anthropic 侧为 30×已用次数的线性退避，10 次累计近 27 分钟）。
# 2026-10-07 由「10 次尝试/9 次重试」提到 10 次重试；OpenAI 侧 Cloudflare 520/524
# 一并并入该预算（原各自 5 次专用预算，见 openai_compat.OPENAI_5XX_RETRYABLE）。
HTTP_5XX_RETRIES = 10
HTTP_5XX_ATTEMPTS = HTTP_5XX_RETRIES + 1
HTTP_5XX_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)

# 429（配额/限流）：共 11 次尝试（首次 + 10 次重试），与标准 5xx 同预算。
# 2026-10-10 由「共 2 次尝试」提到 11 次（两条 compat 路径统一口径；OpenAI 侧
# 此前刻意不重试，现一并纳入）。退避另用 RATE_LIMIT_BACKOFF=30s 固定间隔——
# 实测 ark 网关对大 max_tokens 请求有分钟级坏窗口，配额型 429 通常不会在几秒内
# 恢复，故不做快节奏指数退避。
RATE_LIMIT_RETRIES = 10
RATE_LIMIT_ATTEMPTS = RATE_LIMIT_RETRIES + 1
RATE_LIMIT_BACKOFF = 30.0

# 模型冷却识别（2026-10-10）：网关把「某模型在所有账号池里被冻结」这种**典型暂态
# 故障**塞进 HTTP 400（实测文案：`model is unavailable on every account
# (per-model cooldown), try another model`）。这类错误此前落 `can_retry=False`
# 直接抛，还被 chat 层 `_classify_error` 判成 bad_request「历史消息结构问题」——
# 方向完全跑偏。现按特征文案单独识别，走与 429 相同的 11 次尝试 + 30s 固定退避预算。
# 已知局限：per-model cooldown 常为分钟级到小时级，30s×10 仍可能全落同一冷却窗内
# （与 2026-09-28 ark 坏窗口同构）；故同时把错误分类改指向「换模型/换供应商」，
# 真正的跨模型 fallback 另议（见 docs/plans/）。
_COOLDOWN_HINTS = ("per-model cooldown", "on every account",
                   "try another model", "model is unavailable")
# 与 RATE_LIMIT_BACKOFF 同值：冷却窗口通常在分钟级，快节奏退避无意义。
COOLDOWN_BACKOFF = RATE_LIMIT_BACKOFF


def is_model_cooldown_error(status: int, message: str, code: str = "") -> bool:
    """判断 400/413 响应是否为「模型冷却」类暂态错误（而非真的参数不合法）。

    只认特征文案，避免把真正入参错误也拖进 5 分钟重试。
    """
    if status not in (400, 413):
        return False
    low = f"{message} {code}".lower()
    return any(h in low for h in _COOLDOWN_HINTS)


# 网关「渠道校验」拦截识别（2026-10-16 事故修复）：私有中转网关（实测
# api.huanling.icu）在风控窗口内对所有请求回 HTTP 400，报文形如
# ``{"code":11128,"msg":"conversation id missing","displayTips":
# {"en":"This call failed channel verification, possibly because it was
# not sent from an official client..."}}``。实测特征：
# ① 与请求内容/历史结构无关（同一条「hi」在窗口内必挂、窗外必过）；
# ② 按时间衰减——密集请求后触发，冷却约 3-5 分钟自行恢复（间隔 20s
#    连发 8 次全过、12 次突发也全过，说明恢复后不限频）；
# ③ UA 伪装（CLIENT_USER_AGENT 的 Chrome UA）是过校验的前提——裸 curl
#    的 UA 会被网关以 401 API_KEY_REQUIRED 形式挡在渠检之前。
# 这类错误此前落 chat 层 bad_request「历史消息结构问题」文案，方向跑偏
# （新线程首条消息也照挂），且一次就抛不做冷却等待。现按特征单独识别，
# 走与冷却同口径的 11 次尝试 + 30s 固定退避（总窗 5 分钟，覆盖实测
# 3-5 分钟恢复期）。已知局限：与 per-model cooldown 同构，若网关风控
# 超过 5 分钟仍会耗尽——此时文案指向「稍后重试/换供应商」。
_CHANNEL_VERIFY_HINTS = ("conversation id missing", "channel verification",
                         "渠道校验", "请求被拦截", "请求已被拦截", "官方客户端",
                         "通路验证", "通路驗證", "通路校驗", "official client")
_CHANNEL_VERIFY_CODES = ("11128",)


def is_channel_verification_error(status: int, message: str, code: str = "") -> bool:
    """判断 400 响应是否为网关「渠道校验」风控拦截（而非真的参数不合法）。

    只认特征码/文案（code=11128 或 conversation id missing / channel
    verification 等），避免把真正入参错误拖进 5 分钟重试。``message``
    建议传整个错误报文（JSON 序列化后）——该网关把特征字段放在顶层
    （``msg``/``displayTips``），不一定在 ``error.message`` 里。
    """
    if status != 400:
        return False
    low = f"{message} {code}".lower()
    if any(c in low for c in _CHANNEL_VERIFY_CODES):
        return True
    return any(h in low for h in _CHANNEL_VERIFY_HINTS)


def is_retryable_http_5xx(status: int) -> bool:
    """判断状态是否属于标准上游 5xx 暂态错误。"""
    return status in HTTP_5XX_STATUS


def retry_note(done: int, budget: int) -> str:
    """重试耗尽后附在错误文案里的计数后缀（2026-10-07）。

    动机：502/503 这类上游 5xx 报错只写「LLM 调用失败 HTTP 502」，无从判断是
    「一次就抛」还是「10 次全试过」——排查时要翻事件流数 chat.retry 才知道。

    ``done``=已实际重试次数（不含首次），``budget``=该类允许的重试次数；
    ``budget<=0``（本就不可重试的鉴权/参数错误）返回空串，不硬凑「0/0 次」。
    """
    if budget <= 0:
        return ""
    return f"（已重试 {done}/{budget} 次）"


ABORT_MESSAGE = "已中断"   # sleep_cancellable / 流式中止统一用这条（勿改字面量）


def classify_provider_error(e: BaseException) -> str:
    """异常 → 策略类别（诊断的 ``category`` 字段也用这组值）。

    类别：``aborted / partial / truncated / context / auth / quota / rate_limit /
    cooldown / channel_block / upstream / network / bad_request / unknown``。
    """
    msg = str(e)
    low = msg.lower()
    status = int(getattr(e, "status", 0) or 0)
    if msg == ABORT_MESSAGE or "已中断" in msg:
        return "aborted"
    if getattr(e, "partial", False):
        return "partial"
    if getattr(e, "truncated", False) or "工具参数流截断" in msg:
        return "truncated"
    if isinstance(e, ContextOverflowError) or (
            status in (400, 413) and ("too long" in low or "input length" in low
                                      or "context length" in low
                                      or "maximum context" in low)):
        return "context"
    # 冷却 / 渠道校验必须先于 bad_request：两者都是网关塞进 400 的**暂态**故障，
    # 而且上游文案本身就在喊「try another model」——正是该换供应商的场合。
    if is_model_cooldown_error(status, msg) or (
            "per-model cooldown" in low or "on every account" in low
            or "try another model" in low):
        return "cooldown"
    if is_channel_verification_error(status, msg) or (
            "conversation id missing" in low or "channel verification" in low
            or "11128" in low or "渠道校验" in msg or "请求被拦截" in msg):
        return "channel_block"
    if status == 429 or "429" in msg or "rate limit" in low or "限流" in msg:
        return "rate_limit"
    if status in (401, 403) or "unauthorized" in low \
            or "authentication" in low or "invalid api key" in low:
        return "auth"
    if status == 402 or "余额" in msg or "insufficient" in low or "quota" in low:
        return "quota"
    if status in (500, 502, 503, 504, 520, 524) or "upstream" in low \
            or "temporarily unavailable" in low or "service unavailable" in low \
            or "bad gateway" in low or "gateway timeout" in low or "overloaded" in low:
        return "upstream"
    if isinstance(e, (TimeoutError, ConnectionError)) \
            or "网络错误" in msg or "网络连接失败" in msg or "流式传输中断" in msg \
            or "connection reset" in low or "connection aborted" in low \
            or "remote host" in low or "incomplete chunked read" in low \
            or "peer closed connection" in low:
        return "network"
    if status in (400, 413) or "invalidparameter" in low:
        return "bad_request"
    return "unknown"


# 不值得换供应商的类别（换一家也不会更好，或换了会破坏上层已有的自愈逻辑）：
#   partial       已吐增量，流不可重放 → 上层走「接着续写」，换家重发会让半截答复重复上屏
#   truncated     工具参数流截断 → 上层整轮重发（最后一轮降级非流式），换家会绕过该降级
#   context       输入超限 → 上层强制压缩后重试（reactive compact），换家等于放弃压缩
#   bad_request   请求本身不合法（非冷却/非渠检）→ 换家同样 400，白等一圈
#   aborted       人类点了停止
_NO_FAILOVER = frozenset({"aborted", "partial", "truncated", "context", "bad_request"})


def should_failover(e: BaseException) -> bool:
    """该异常是否值得「换下一个供应商」。"""
    return classify_provider_error(e) not in _NO_FAILOVER


# 可中断退避的轮询粒度。0.2s 让「停止」在体感上是立刻生效（前端 chat.retry
# 一直转圈时点停止，最多 0.2s 就断），同时不至于空转烧 CPU。
CANCEL_POLL_SLICE = 0.2

def sleep_cancellable(seconds: float, should_cancel=None) -> None:
    """退避等待，期间轮询 ``should_cancel``；命中立即抛 ``LLMError("已中断")``。

    2026-10-20 事故（用户实测「重试中点停止不会立刻停」）：此前所有退避都是
    ``time.sleep`` 裸睡——429 / 模型冷却 / 渠道校验固定 30s，5xx 封顶 30s，
    每类各 10 次重试，最坏 **5 分钟**内点「停止」毫无反应。更糟的是
    ``should_cancel`` 原先只在**流式读取数据帧**时轮询
    （``openai_compat._consume_stream``）——重试期间上游回的是 429/5xx，
    压根没有流，回调一次都不会被调到。于是停止信号被彻底吞掉。

    改成切片轮询后，退避期间每 ``CANCEL_POLL_SLICE`` 秒探一次中止标志；
    抛 ``LLMError`` 是为了让上层既有的 ``except LLMError`` + ``if self._aborted()``
    分支直接把它转成「已按人类要求停止」，不必新增异常类型。

    不传 ``should_cancel`` 时退化为普通 ``time.sleep``（单测/离线调用不受影响）。
    """
    if should_cancel is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if should_cancel():
            raise LLMError("已中断")
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(CANCEL_POLL_SLICE, left))
