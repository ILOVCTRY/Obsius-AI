"""LLM 传输层共享重试策略常量。"""

# 仅把已知的上游暂态状态纳入标准 5xx 重试；鉴权/参数类 5xx 不盲目重放。
HTTP_5XX_STATUS = frozenset({500, 502, 503, 504})
# 标准 5xx：共 10 次尝试（首次 + 9 次重试），退避封顶 30s——Anthropic/OpenAI
# 两条路径统一口径（此前 Anthropic 侧为 30×已用次数的线性退避，10 次累计近 27 分钟）。
HTTP_5XX_RETRIES = 9
HTTP_5XX_ATTEMPTS = HTTP_5XX_RETRIES + 1
HTTP_5XX_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0)


def is_retryable_http_5xx(status: int) -> bool:
    """判断状态是否属于标准上游 5xx 暂态错误。"""
    return status in HTTP_5XX_STATUS
