"""LLM 传输层共享重试策略常量。"""

# 仅把已知的上游暂态状态纳入标准 5xx 重试；鉴权/参数类 5xx 不盲目重放。
HTTP_5XX_STATUS = frozenset({500, 502, 503, 504})
HTTP_5XX_RETRIES = 10
HTTP_5XX_ATTEMPTS = HTTP_5XX_RETRIES + 1


def is_retryable_http_5xx(status: int) -> bool:
    """判断状态是否属于标准上游 5xx 暂态错误。"""
    return status in HTTP_5XX_STATUS
