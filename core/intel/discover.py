"""按用户画像关注词主动发现外部情报。"""

from __future__ import annotations

from collections.abc import Callable

from core.intel.ranking import normalize_day


def _call_search(searcher: Callable, query: str, *, num: int, time_range: str):
    try:
        return searcher(query, num=num, time_range=time_range)
    except TypeError:
        # 测试替身/旧适配器可能只收 query、num。
        return searcher(query, num=num)


def discover_by_keywords(keywords, *, time_range: str = "day", limit_per: int = 10,
                         searcher: Callable | None = None) -> tuple[list[dict], list[str]]:
    """每个关注词查询一次，失败只记录错误，不中断其他关注词。"""
    if searcher is None:
        from core.websearch import search_web
        searcher = search_web
    items: list[dict] = []
    errors: list[str] = []
    for raw in keywords or []:
        text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
        text = str(text).strip()
        if not text:
            continue
        try:
            result = _call_search(searcher, text, num=max(1, min(int(limit_per), 20)),
                                  time_range=time_range)
            if isinstance(result, tuple):
                rows, backend = result
            else:
                rows, backend = result, "external"
            for row in rows or []:
                if not isinstance(row, dict) or not row.get("url"):
                    continue
                day = normalize_day(row.get("published_date") or row.get("publishedDate"))
                items.append({
                    "url": str(row["url"]),
                    "title": str(row.get("title") or row["url"])[:300],
                    "source": str(row.get("source") or backend),
                    "kind": "article",
                    "summary": str(row.get("snippet") or row.get("content") or row.get("body") or "")[:800],
                    "published_at": str(row.get("published_at") or row.get("published_date") or ""),
                    "published_day": day,
                    "is_priority": False,
                    "has_poc": False,
                    "poc_url": "",
                    "via": "focus",
                    "focus_keyword": text,
                    "window": time_range,
                })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{text}: {type(exc).__name__}: {exc}")
    return items, errors
