"""情报抓取（E9，DESIGN.md §16）：触发式 Job 用的纯函数抓取层。

出站 HTTP 走标准库 urllib（不经执行网关——平台自身可信出站，与 Agent 命令通道无关），
getter 可注入供测试（永不触网）。数据源（定稿）：
- NVD API 2.0（近 7 日新 CVE，与 KEV 窗口对齐便于证据合并）/ CISA KEV（已在利用）/ GitHub
  Advisory（经 api.github.com/advisories 免 key REST，按发布日倒序取近页）
- 中文/技术社区 RSS（feeds.json 源清单）
- POC 启发式判定在 fetch 层（references 只在抓取响应里，不落库即丢失）：
  NVD "Exploit" 标签 + PoC 站点域名白名单；KEV 条目经 _enrich 按 CVE id 合并 NVD/GHSA
  证据，无证据的 KEV 条目 has_poc=False（不进简报，宁缺毋滥，DESIGN.md §16.1）。
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


# ---------- POC 启发式判定 ----------

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)

_POC_DOMAINS = ("github.com", "gitlab.com",           # 含 gist / PoC 仓库
                "exploit-db.com",
                "packetstormsecurity.com", "packetstormsecurity.net",
                "seclists.org", "securityfocus.com",
                "huntr.dev", "huntr.com")
_POC_URL_EXCLUDES = ("github.com/advisories",          # GHSA 详情页，非 PoC
                     "github.com/CVEProject",          # cvelistV5 仓库，几乎每条 CVE 都带
                     "github.com/advisory-database",
                     "nvd.nist.gov", "cve.org")
_GIT_PATH_RE = re.compile(r"(?i)\b(poc|exploit|cve-\d{4}-\d{4,7})")


def _poc_url_signal(url: str) -> bool:
    """URL 域名白名单判定（github/gitlab 还要求路径含 PoC 关键词，防补丁 commit 误报）。"""
    if not url or any(x in url.lower() for x in _POC_URL_EXCLUDES):
        return False
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    if not any(host == d or host.endswith("." + d) for d in _POC_DOMAINS):
        return False
    if host == "github.com" or host.endswith(".github.com") or \
            host == "gitlab.com" or host.endswith(".gitlab.com"):
        path = url.split("//", 1)[-1].split("/", 1)
        return bool(len(path) == 2 and _GIT_PATH_RE.search(path[1]))
    return True


def _poc_signal(refs: list) -> tuple[bool, str]:
    """references → (has_poc, 最优 poc_url)。

    refs 两种形态：NVD [{"url","source","tags":[...]}]，GHSA ["url", ...]（无 tag，容错跳过）。
    tag "Exploit" 命中 > 白名单 URL；任一命中即 has_poc=True。
    """
    best_tagged = ""
    best_domain = ""
    for ref in refs:
        if isinstance(ref, str):
            url, tags = ref, []
        elif isinstance(ref, dict):
            url, tags = ref.get("url", ""), ref.get("tags") or []
        else:
            continue
        tagged = any(t.lower() == "exploit" for t in tags if isinstance(t, str))
        in_domain = _poc_url_signal(url)
        if tagged and not best_tagged:
            best_tagged = url
        elif in_domain and not best_domain:
            best_domain = url
    if best_tagged:
        return True, best_tagged
    if best_domain:
        return True, best_domain
    return False, ""


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


def fetch_nvd(getter: RawGetter, *, days: int = 7) -> list[dict]:
    """NVD 2.0 近 days 天新 CVE（无 key 限速 5 req/30s，抓取频率人工触发足够）。

    窗口取 7 天与 KEV 对齐（_enrich 按 CVE id 合并 POC 证据）；perPage=200 不分页，
    全球周新增可能截断——只影响池完整度，不影响简报正确性（DESIGN.md §16.1 注记）。
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    url = ("https://services.nvd.nist.gov/rest/json/cves/2.0"
           f"?pubStartDate={_nvd_date(start)}&pubEndDate={_nvd_date(end)}"
           "&resultsPerPage=200")
    status, text = getter(url, 30.0)
    if status != 200:
        raise RuntimeError(f"NVD HTTP {status}")
    out = []
    for it in json.loads(text).get("vulnerabilities", []):
        cve = (it.get("cve") or {})
        cid = cve.get("id", "")
        descs = [d.get("value", "") for d in cve.get("descriptions", [])
                 if d.get("lang") == "en"]
        has_poc, poc_url = _poc_signal(cve.get("references") or [])
        out.append({"url": f"https://nvd.nist.gov/vuln/detail/{cid}", "title": cid,
                    "source": "nvd", "kind": "cve",
                    "summary": (descs[0] if descs else "")[:800],
                    "published_at": cve.get("published", ""),
                    "is_priority": has_poc,     # 有公开 POC 打优先标（§16.1 既有声明）
                    "has_poc": has_poc, "poc_url": poc_url})
    return out


def fetch_kev(getter: RawGetter) -> list[dict]:
    """CISA KEV 全量目录（已在利用 = 优先标；以 dateAdded 近 7 天为新条目）。

    KEV JSON 本身无 references，has_poc 由 _enrich 按 CVE id 合并 NVD/GHSA 证据回填；
    无证据保持 False——严格语义：没有公开 POC 的 KEV 条目不进简报（宁缺毋滥）。
    """
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
                    "published_at": v.get("dateAdded", ""), "is_priority": True,
                    "has_poc": False, "poc_url": ""})
    return out


def fetch_github_advisories(getter: RawGetter) -> list[dict]:
    """GitHub Advisory（免 key REST，published 倒序近页）；POC 证据取自 references
    （URL 字符串数组，无 tag，_poc_signal 容错）。"""
    status, text = getter(
        "https://api.github.com/advisories?per_page=40", 30.0)
    if status != 200:
        raise RuntimeError(f"GHSA HTTP {status}")
    out = []
    for a in json.loads(text):
        ghsa = a.get("ghsa_id", "")
        cve = a.get("cve_id") or ghsa
        has_poc, poc_url = _poc_signal(a.get("references") or [])
        out.append({"url": a.get("html_url") or f"https://github.com/advisories/{ghsa}",
                    "title": f"[GHSA] {cve} {a.get('summary', '')}".strip(),
                    "source": "ghsa", "kind": "cve",
                    "summary": (a.get("description") or "")[:800],
                    "published_at": a.get("published", ""),
                    "is_priority": bool(a.get("type") == "reviewed"
                                        and a.get("credits")) or has_poc,
                    "has_poc": has_poc, "poc_url": poc_url})
    return out


# ---------- 整批抓取 ----------

def _enrich(items: list[dict]) -> None:
    """按 CVE id 把 NVD/GHSA 的 POC 证据合并到 KEV 条目（原地修改）。

    KEV JSON 无 references，唯一免费证据来自同批 NVD/GHSA。扩展点：若未来要补
    NVD 单 CVE 限速补查（5 req/30s），在 `not has_poc and source=="kev"` 分支接入。
    """
    poc_map: dict[str, str] = {}
    for it in items:
        if it.get("has_poc") and it.get("source") in ("nvd", "ghsa"):
            m = _CVE_RE.search(f"{it.get('title', '')} {it.get('url', '')}")
            if m:
                poc_map.setdefault(m.group(0).upper(), it["poc_url"] or it["url"])
    for it in items:
        if it.get("source") == "kev" and not it.get("has_poc"):
            m = _CVE_RE.search(f"{it.get('title', '')} {it.get('url', '')}")
            if m and m.group(0).upper() in poc_map:
                it["has_poc"] = True
                it["poc_url"] = poc_map[m.group(0).upper()]


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
    _enrich(items)
    return items, errors
