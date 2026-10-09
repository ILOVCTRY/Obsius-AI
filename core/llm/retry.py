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
