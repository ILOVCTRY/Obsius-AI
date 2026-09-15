"""情报抓取（E9，DESIGN.md §16）：触发式 Job 用的纯函数抓取层。

出站 HTTP 走标准库 urllib（不经执行网关——平台自身可信出站，与 Agent 命令通道无关），
getter 可注入供测试（永不触网）。数据源（定稿）：
- NVD API 2.0（近 2 日新 CVE）/ CISA KEV（已在利用打优先标）/ GitHub Advisory（经
  api.github.com/advisories 免 key REST，按发布日倒序取近页）
- 中文/技术社区 RSS（feeds.json 源清单）
单源失败只记 error 不中断整批。
"""

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

RawGetter = Callable[[str, float], tuple[int, str]]

_UA = {"User-Agent": "cyberstrike-pro-intel/0.1 (+local research platform)"}


def _urllib_get(url: str, timeout: float = 20.0) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


# ---------- RSS / Atom ----------

def parse_rss(xml_text: str, source: str) -> list[dict]:
    """RSS 2.0 与 Atom 通吃，解析失败抛 ValueError（调用方按源兜住）。"""
    root = ET.fromstring(xml_text)
    items: list[dict] = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = summary = published = ""
        for ch in item:
            t = ch.tag.rsplit("}", 1)[-1]
            if t == "title":
                title = (ch.text or "").strip()
            elif t == "link":
                link = (ch.text or "").strip() or ch.get("href", "")
            elif t in ("description", "summary", "content"):
                summary = summary or _strip_html(ch.text or "")
            elif t in ("pubDate", "published", "updated", "date"):
                published = published or (ch.text or "").strip()
        if title and link:
            items.append({"url": link, "title": _strip_html(title), "source": source,
                          "kind": "article", "summary": summary[:800],
                          "published_at": published, "is_priority": False})
    return items


# ---------- 结构化漏洞源 ----------

def _nvd_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000")


def fetch_nvd(getter: RawGetter, *, days: int = 2) -> list[dict]:
    """NVD 2.0 近 days 天新 CVE（无 key 限速 5 req/30s，抓取频率人工触发足够）。"""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    url = ("https://services.nvd.nist.gov/rest/json/cves/2.0"
           f"?pubStartDate={_nvd_date(start)}&pubEndDate={_nvd_date(end)}"
           "&resultsPerPage=50")
    status, text = getter(url, 30.0)
    if status != 200:
        raise RuntimeError(f"NVD HTTP {status}")
    out = []
    for it in json.loads(text).get("vulnerabilities", []):
        cve = (it.get("cve") or {})
        cid = cve.get("id", "")
        descs = [d.get("value", "") for d in cve.get("descriptions", [])
                 if d.get("lang") == "en"]
        out.append({"url": f"https://nvd.nist.gov/vuln/detail/{cid}", "title": cid,
                    "source": "nvd", "kind": "cve",
                    "summary": (descs[0] if descs else "")[:800],
                    "published_at": cve.get("published", ""), "is_priority": False})
    return out


def fetch_kev(getter: RawGetter) -> list[dict]:
    """CISA KEV 全量目录（已在利用 = 优先标；以 dateAdded 近 2 天为新条目）。"""
    status, text = getter(
        "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        30.0)
    if status != 200:
        raise RuntimeError(f"KEV HTTP {status}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    out = []
    for v in json.loads(text).get("vulnerabilities", []):
        if v.get("dateAdded", "") < cutoff:
            continue  # 只收近一周新入 KEV，防全量灌池
        cid = v.get("cveID", "")
        out.append({"url": f"https://nvd.nist.gov/vuln/detail/{cid}",
                    "title": f"[KEV] {cid} {v.get('vulnerabilityName', '')}".strip(),
                    "source": "kev", "kind": "cve",
                    "summary": v.get("shortDescription", "")[:800],
                    "published_at": v.get("dateAdded", ""), "is_priority": True})
    return out


def fetch_github_advisories(getter: RawGetter) -> list[dict]:
    """GitHub Advisory（免 key REST，published 倒序近页）；is_priority 在打分层按
    exploit/KEV 线索补标，这里只入池。"""
    status, text = getter(
        "https://api.github.com/advisories?per_page=40", 30.0)
    if status != 200:
        raise RuntimeError(f"GHSA HTTP {status}")
    out = []
    for a in json.loads(text):
        ghsa = a.get("ghsa_id", "")
        cve = a.get("cve_id") or ghsa
        out.append({"url": a.get("html_url") or f"https://github.com/advisories/{ghsa}",
                    "title": f"[GHSA] {cve} {a.get('summary', '')}".strip(),
                    "source": "ghsa", "kind": "cve",
                    "summary": (a.get("description") or "")[:800],
                    "published_at": a.get("published", ""),
                    "is_priority": bool(a.get("type") == "reviewed"
                                        and a.get("credits"))})
    return out


# ---------- 整批抓取 ----------

def fetch_all(getter: RawGetter | None = None,
              feeds: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """抓全部源 → (入池候选, errors)。单源失败不中断。"""
    getter = getter or _urllib_get
    items: list[dict] = []
    errors: list[str] = []
    for fn in (fetch_nvd, fetch_kev, fetch_github_advisories):
        try:
            items.extend(fn(getter))
        except Exception as e:  # noqa: BLE001 —— 单源失败可容忍
            errors.append(f"{fn.__name__}: {type(e).__name__}: {e}")
    for f in feeds or []:
        try:
            status, text = getter(f["url"], 20.0)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            items.extend(parse_rss(text, f["name"]))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{f.get('name', f.get('url', '?'))}: {type(e).__name__}: {e}")
    return items, errors
