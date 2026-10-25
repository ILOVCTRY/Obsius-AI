"""情报文章日期与检索排序辅助。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

_WINDOWS = {"day": 1, "week": 7, "month": 30, "half_year": 183, "year": 365}
_WINDOW_FRESHNESS = {"day": 0.95, "week": 0.80, "month": 0.55, "half_year": 0.4, "year": 0.25}


def normalize_day(value: str | None) -> str:
    """归一化 ISO/RFC822/常见日期为 YYYY-MM-DD，未知返回空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    m = re.match(r"^(\d{4})[-/]([01]\d)[-/]([0-3]\d)", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return ""


def window_start(window: str, *, today: date | None = None) -> str:
    days = _WINDOWS.get(window, _WINDOWS["month"])
    base = today or datetime.now(timezone.utc).date()
    return (base - timedelta(days=days - 1)).isoformat()


def window_freshness(window: str) -> float:
    return _WINDOW_FRESHNESS.get(window, _WINDOW_FRESHNESS["month"])


def quality_score(item: dict) -> float:
    """可解释的无 LLM 质量估计，范围 0..1。"""
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    score = 0.35
    if len(item.get("summary") or "") >= 80:
        score += 0.15
    if any(k in text for k in ("原理", "分析", "复现", "实战", "poc", "exploit", "源码")):
        score += 0.2
    if item.get("source") in {"PortSwigger Research", "Seebug Paper", "腾讯玄武实验室", "奇安信攻防社区", "先知社区", "FreeBuf"}:
        score += 0.2
    if item.get("is_priority") or item.get("has_poc"):
        score += 0.1
    try:
        model_score = max(0.0, min(100.0, float(item.get("score") or 0))) / 100.0
    except (TypeError, ValueError):
        model_score = 0.0
    if model_score:
        score = score * 0.35 + model_score * 0.65
    return min(1.0, score)


def mainstream_score(item: dict) -> float:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    if any(k in text for k in ("ms16-", "ms08-", "flash", "internet explorer", "已弃用")):
        return 0.1
    if any(k in text for k in ("k8s", "kubernetes", "ebpf", "edr", "云原生", "container", "oauth", "supply chain")):
        return 0.9
    return 0.5


def ranking_score(item: dict, *, window: str = "month") -> float:
    quality = quality_score(item)
    known = normalize_day(item.get("published_day") or item.get("published_at"))
    if known:
        try:
            age = max(0, (datetime.now(timezone.utc).date() - date.fromisoformat(known)).days)
        except ValueError:
            age = 9999
        freshness = max(0.0, 1.0 - age / 177.0)
    else:
        freshness = window_freshness(str(item.get("window") or window))
    return round(100 * (quality ** 0.7) * (freshness ** 0.3) * mainstream_score(item), 1)
