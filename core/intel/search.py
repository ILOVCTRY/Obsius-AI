"""Google News RSS and optional SearXNG search for intelligence articles."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from core.websearch import (
    DEFAULT_CONFIG_PATH,
    WebSearchConfig,
    WebSearchError,
    searxng_search,
)

_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
_TIME_QUERY = {
    "week": "7d",
    "month": "30d",
    "year": "1y",
}


def _text(value: str | None, limit: int) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def google_news_search(cfg: WebSearchConfig, query: str, *, num: int,
                       time_range: str = "") -> list[dict]:
    """Fetch Google News RSS results, retaining source and publication date."""
    if time_range and time_range not in {*_TIME_QUERY, "day", "half_year"}:
        raise WebSearchError(f"不支持的时间范围: {time_range}")
    q = str(query or "").strip()
    today = datetime.now(timezone.utc).date()
    if time_range == "day":
        since = (today - timedelta(days=1)).isoformat()
        until = (today + timedelta(days=1)).isoformat()
        q = f"{q} after:{since} before:{until}"
    elif time_range == "half_year":
        since = (today - timedelta(days=182)).isoformat()
        q = f"{q} after:{since}"
    elif time_range:
        q = f"{q} when:{_TIME_QUERY[time_range]}"
    params = {"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"}
    try:
        kwargs = {"timeout": cfg.timeout_s, "follow_redirects": True,
                  "trust_env": True}
        if cfg.proxy:
            kwargs["proxy"] = cfg.proxy
            kwargs["trust_env"] = False
        response = httpx.get(
            _GOOGLE_NEWS_RSS, params=params,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CyberstrikeIntel/1.0)"},
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise WebSearchError(f"Google News 请求失败: {exc}") from exc
    if response.status_code != 200:
        raise WebSearchError(f"Google News HTTP {response.status_code}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise WebSearchError("Google News 返回的 RSS XML 无法解析") from exc

    rows: list[dict] = []
    for item in root.findall(".//item"):
        url = _text(item.findtext("link"), 2000)
        if not url.startswith(("http://", "https://")):
            continue
        source = item.find("source")
        source_name = _text(source.text if source is not None else "", 120)
        published = _text(item.findtext("pubDate"), 100)
        try:
            parsed = parsedate_to_datetime(published)
            published = parsed.date().isoformat() if parsed else ""
        except (TypeError, ValueError, OverflowError):
            published = ""
        if not source_name:
            source_name = urlparse(url).hostname or "Google News"
        rows.append({
            "title": _text(item.findtext("title"), 300) or url,
            "url": url,
            "snippet": _text(item.findtext("description"), 1000),
            "source": source_name,
            "published_date": published,
        })
        if len(rows) >= max(1, min(int(num or 10), 100)):
            break
    return rows


def search_intel(query: str, search_config: dict | None = None, *, num: int = 10,
                 time_range: str = "", config_path: str = DEFAULT_CONFIG_PATH
                 ) -> tuple[list[dict], str]:
    """Search with enabled SearXNG first, then Google News RSS; never uses Bing."""
    cfg = WebSearchConfig.from_file(config_path)
    cfg.searxng_url = ""
    search_config = search_config if isinstance(search_config, dict) else {}
    searx = search_config.get("searxng") or {}
    errors: list[str] = []

    if isinstance(searx, dict) and searx.get("enabled"):
        try:
            port = max(1, min(int(searx.get("port", 8888)), 65535))
            cfg.searxng_url = f"http://127.0.0.1:{port}"
            rows = searxng_search(
                cfg, query, num=max(1, min(int(num or 10), 20)),
                engines=str(searx.get("engines") or "google"), language="zh-CN",
                time_range=time_range, include_metadata=True,
            )
            google_rows = [
                row for row in rows
                if isinstance(row, dict)
                and str(row.get("source") or "").strip().casefold() == "google"
            ]
            if google_rows:
                return google_rows, "searxng"
            errors.append("SearXNG: no Google-engine results; falling back to Google News RSS")
        except (TypeError, ValueError, WebSearchError) as exc:
            errors.append(f"SearXNG: {exc}")

    try:
        rows = google_news_search(cfg, query, num=num, time_range=time_range)
        return rows, "google_news"
    except WebSearchError as exc:
        errors.append(f"Google News: {exc}")
    raise WebSearchError("；".join(errors))
