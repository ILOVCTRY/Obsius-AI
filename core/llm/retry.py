"""LLM 传输层共享重试策略常量。"""

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
