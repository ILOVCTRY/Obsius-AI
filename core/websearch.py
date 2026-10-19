"""联网搜索后端（web_search 工具，2026-10-19）。

多后端 + 自动降级链（前者**通过质量闸**即止，否则继续下一个；全失败才抛
WebSearchError）：

1. **SearXNG（可选首选，需自建）**——自建元搜索引擎（AGPL，Docker 一行部署），
   自带 JSON API、**无需 Key**。配 `searxng_url` 才启用；出网隔离在 SearXNG 一侧
   （agent 只访问内网地址，无需给 agent 配代理）。
2. **DuckDuckGo HTML（默认首选免 Key 后端）**——POST `html.duckduckgo.com/html/`，
   抓 `result__a` / `result__snippet`。**实测检索质量最好**（CVE 能拿到 NVD 条目 /
   厂商公告 / GitHub PoC，API 问题能拿到官方文档），零第三方依赖、零 Key。
   注意：本机直连与沙箱环境代理都不通，**必须走可用正向代理**（见 `proxy`）。
3. **Bing HTML（次选免 Key 后端）**——GET `www.bing.com/search` 抓 `b_algo`。
   **有坑**：`www.bing.com` 在国内网络会被静默 302 到 `cn.bing.com`，后者对带编号/
   符号的查询会退化成「站点泛首页」结果（查 CVE 一律返回 cve.org/百度百科 CVE 词条，
   与查询词无关）。故本后端带**相关性闸**（见 `_looks_degraded`）：命中退化即视为
   失败、继续降级，绝不把泛结果当命中回给模型。
4. **ddgs（末位，需装包）**——原 `duckduckgo_search` 继任包。反爬/代理波动大，
   缺依赖静默跳过（import 不炸，工具层回填安装指引）。

配置 `config/web_search.json`（缺文件/坏文件全默认，绝不 500；口径同
core/browser/pool.py 的 BrowserConfig）：

    searxng_url    SearXNG 实例地址（如 http://127.0.0.1:8888）；空=跳过
    searxng_token  可选 Bearer token（实例前置 nginx 鉴权时用）
    timeout_s      单后端超时秒数（默认 20）
    num            默认返回条数 1-20（默认 10）
    engines        默认引擎限定，逗号分隔（仅 SearXNG 生效）
    language       默认结果语言（SearXNG/DDG 用；Bing 走 setlang/mkt）
    proxy          免 Key 后端（DDG/Bing/ddgs）的正向代理，如
                   http://127.0.0.1:7890（本机 Clash）；空=交 httpx 读环境变量
                   （注意：沙箱注入的环境代理可能是坏的，会返回无关结果）
    bing_market    Bing 市场代码（默认 zh-CN）
    disable_ddg    置 true 跳过 DuckDuckGo 后端
    disable_bing   置 true 跳过 Bing 后端
    strict         置 true 时「相关性闸」不禁用（默认）：退化结果直接判失败；
                   置 false 时退化结果仍会返回（带 degraded 标记），便于排障

统一结果结构（list of dict）：{"title", "url", "snippet"}，深读页面交给
browser_navigate / browser_replay（本模块只做检索，不抓正文）。
"""

from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

DEFAULT_CONFIG_PATH = "config/web_search.json"
_MAX_NUM = 20  # 各端硬顶（SearXNG 单页 + DDG/Bing count + ddgs max_results 口径）
_BING_URL = "https://www.bing.com/search"
_DDG_URL = "https://html.duckduckgo.com/html/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_ACCEPT_HTML = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8")

# 「站点泛首页」退化特征域名（cn.bing.com 降级时的典型输出）
_HUB_HOSTS = (
    "cve.org", "baike.baidu.com", "imfht.com", "cnnvd.org.cn",
    "nvd.nist.gov/vuln/search", "runoob.com", "zhihu.com", "52pojie.cn",
    "jingyan.baidu.com", "pythonlang.cn",
)


@dataclass
class WebSearchConfig:
    """config/web_search.json（缺文件/坏文件全默认，绝不 500）。"""

    searxng_url: str = ""
    searxng_token: str = ""
    timeout_s: float = 20.0
    num: int = 10
    engines: str = ""
    language: str = ""
    proxy: str = ""
    bing_market: str = "zh-CN"
    disable_ddg: bool = False
    disable_bing: bool = False
    strict: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> "WebSearchConfig":
        cfg = cls()
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        if not isinstance(data, dict):
            return cfg
        for k in ("searxng_url", "searxng_token", "engines", "language",
                  "proxy", "bing_market"):
            v = data.get(k)
            if isinstance(v, str):
                setattr(cfg, k, v.strip())
        for k in ("disable_ddg", "disable_bing", "strict"):
            if isinstance(data.get(k), bool):
                setattr(cfg, k, data[k])
        try:
            cfg.timeout_s = max(1.0, min(float(data.get("timeout_s", cfg.timeout_s)), 60.0))
        except (TypeError, ValueError):
            pass
        try:
            cfg.num = max(1, min(int(data.get("num", cfg.num)), _MAX_NUM))
        except (TypeError, ValueError):
            pass
        return cfg


class WebSearchError(Exception):
    """后端不可用/查询失败（异常文案由工具层原样回填为 `[错误]` 前缀）。"""


class _Degraded(Exception):
    """某后端返回了退化（泛首页）结果——内部信号，不冒泡到工具层。"""


# ---------------------------------------------------------------- 工具

def _clamp_num(num: int | None, default: int) -> int:
    try:
        n = int(num) if num else 0
    except (TypeError, ValueError):
        n = 0
    if not n:
        n = default
    return max(1, min(n, _MAX_NUM))


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _http_kwargs(cfg: WebSearchConfig, *, trust_env: bool = False) -> dict[str, Any]:
    """公共 httpx kwargs：显式代理优先，否则按 trust_env 交环境变量。"""
    kw: dict[str, Any] = {"timeout": cfg.timeout_s, "follow_redirects": True}
    if cfg.proxy:
        kw["proxy"] = cfg.proxy
        kw["trust_env"] = False
    else:
        kw["trust_env"] = trust_env
    return kw


_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
_QUOTED_RE = re.compile(r'"([^"]{3,})"|“([^”]{3,})”')
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-\.:]{3,}")
# 版本号类（至少一个点，如 2.14.1 / 1.0 / 127.0.0.1）——单独一条，避免把裸年份
# （2024）也当特征词造成「香港 2024 天气」这类查询误判退化。
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def _distinctive_tokens(query: str) -> list[str]:
    """从查询里抽出「必须回显」的特征词：CVE 编号 / 引号短语 / 长标识符 / 版本号。

    纯自然语言（如「香港 天气」）抽不出特征词时返回空表——此时无法判退化，
    调用方应跳过相关性闸（宁放过不误杀）。"""
    toks: list[str] = []
    toks += [m.group(0) for m in _CVE_RE.finditer(query)]
    for m in _QUOTED_RE.finditer(query):
        toks.append((m.group(1) or m.group(2)).strip())
    # 带点的版本号/版本串（2.14.1）、连字符标识符（log4j-core）
    toks += [m.group(0) for m in _VERSION_RE.finditer(query)]
    for m in _TOKEN_RE.finditer(query):
        t = m.group(0)
        if "_" in t or "." in t or "-" in t:
            toks.append(t)
    # 去重保序、丢弃过短
    seen, out = set(), []
    for t in toks:
        tl = t.lower()
        if len(t) >= 4 and tl not in seen:
            seen.add(tl)
            out.append(t)
    return out


def _looks_degraded(query: str, rows: list[dict]) -> tuple[bool, str]:
    """判「站点泛首页」退化：查得到特征词却一条都没出现在结果里。

    返回 (degraded, reason)。抽不到特征词 → (False, "")，不判。"""
    if not rows:
        return False, ""
    toks = _distinctive_tokens(query)
    if not toks:
        return False, ""
    blob = " ".join(
        f"{r.get('title', '')} {r.get('url', '')} {r.get('snippet', '')}"
        for r in rows).lower()
    if any(t.lower() in blob for t in toks):
        return False, ""
    hub = sum(1 for r in rows if any(h in r.get("url", "") for h in _HUB_HOSTS))
    return True, (f"结果未回显任何特征词 {toks[:4]}（泛首页命中 {hub}/{len(rows)}）")


# ---------------------------------------------------------------- SearXNG

def searxng_search(cfg: WebSearchConfig, query: str, *, num: int,
                   engines: str, language: str) -> list[dict]:
    """SearXNG JSON API 检索（/search?format=json）。"""
    base = cfg.searxng_url.rstrip("/")
    if not base:
        raise WebSearchError("searxng_url 未配置")
    params: dict[str, Any] = {"q": query, "format": "json"}
    if language:
        params["language"] = language
    if engines:
        params["engines"] = engines
    headers = {}
    if cfg.searxng_token:
        headers["Authorization"] = f"Bearer {cfg.searxng_token}"
    try:
        r = httpx.get(base + "/search", params=params, headers=headers,
                      timeout=cfg.timeout_s, trust_env=False,
                      follow_redirects=True)
    except httpx.HTTPError as e:
        raise WebSearchError(
            f"SearXNG 请求失败（{base}，确认容器在跑/端口对）：{e}") from e
    if r.status_code == 429:
        raise WebSearchError(
            "SearXNG 429（实例内置 limiter：settings.yml 里 limiter 置 false 或调额度）")
    if r.status_code != 200:
        raise WebSearchError(f"SearXNG HTTP {r.status_code}（{base}）")
    try:
        data = r.json()
    except ValueError as e:
        raise WebSearchError(
            "SearXNG 未返回 JSON（settings.yml 的 formats 须含 json）") from e
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise WebSearchError("SearXNG 返回结构异常（无 results 数组）")
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _clean(row.get("url"), 2000)
        if not url:
            continue
        out.append({"title": _clean(row.get("title"), 300) or url,
                    "url": url,
                    "snippet": _clean(row.get("content"), 1000)})
    return out[:num]


# ---------------------------------------------------------------- DuckDuckGo

def _unwrap_ddg_url(raw: str) -> str:
    """解 DDG 跳转包装（//duckduckgo.com/l/?uddg=<urlencoded>）。"""
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
        vals = parse_qs(parsed.query).get("uddg", [])
        if vals:
            return unquote(vals[0])
    return raw


def _parse_ddg_html(text: str, num: int) -> list[dict]:
    out: list[dict] = []
    # 以 result__a 切块：每块 = 一条结果（title + 紧随其后的 snippet）
    for chunk in re.split(r'class="result__a"', text)[1:]:
        m = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        url = _unwrap_ddg_url(html.unescape(m.group(1)))
        if not url.lower().startswith("http"):
            continue
        title = _clean(_strip_tags(m.group(2)), 300) or url
        # 摘要块：DDG 两种版式——<a class="result__snippet">…</a> 或
        # <div class="result__snippet">…</div>；两种收口都兼容。
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)(?:</a>|</div>)',
                       chunk, re.S)
        snippet = _clean(_strip_tags(sm.group(1)), 1000) if sm else ""
        out.append({"title": title, "url": _clean(url, 2000), "snippet": snippet})
    return out[:num]


def ddg_html_search(cfg: WebSearchConfig, query: str, *, num: int,
                    language: str = "") -> list[dict]:
    """DuckDuckGo HTML 端点检索（免 Key、纯 httpx）。"""
    data: dict[str, str] = {"q": query}
    if language and language.lower().startswith(("zh", "cn")):
        data["kl"] = "cn-zh"
    headers = {"User-Agent": _UA, "Accept": _ACCEPT_HTML,
               "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}
    try:
        r = httpx.post(_DDG_URL, data=data, headers=headers,
                       **_http_kwargs(cfg))
    except httpx.HTTPError as e:
        raise WebSearchError(f"DuckDuckGo 请求失败：{e}") from e
    if r.status_code == 202:
        raise WebSearchError("DuckDuckGo 202（反爬限流，稍后重试）")
    if r.status_code != 200:
        raise WebSearchError(f"DuckDuckGo HTTP {r.status_code}")
    rows = _parse_ddg_html(r.text, num)
    if not rows and "result__a" not in r.text:
        # 区分「真无结果」（合法空表，如三段引号短语过窄）与「被拦/改版」（真错误）
        low = r.text.lower()
        if "no results" in low or 'class="no-results"' in low:
            return []
        raise WebSearchError("DuckDuckGo 返回页无结果块（可能被拦或改版）")
    return rows


# ---------------------------------------------------------------- Bing

def _unwrap_bing_url(raw: str) -> str:
    """解码 Bing 结果链接（www.bing.com/ck/a?...&u=a1<base64>）。"""
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    if "bing.com" not in (parsed.netloc or ""):
        return raw
    vals = parse_qs(parsed.query).get("u", [])
    if not vals or len(vals[0]) <= 2:
        return raw
    b64 = vals[0][2:]
    try:
        decoded = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        out = decoded.decode("utf-8", "replace")
    except (ValueError, TypeError):
        return raw
    return out if out.startswith("http") else raw


def _parse_bing_html(text: str, num: int) -> list[dict]:
    out: list[dict] = []
    for b in re.findall(r'<li class="b_algo".*?</li>', text, re.S):
        m = re.search(r'<h2>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        url = _unwrap_bing_url(html.unescape(m.group(1)))
        if not url:
            continue
        title = _clean(_strip_tags(m.group(2)), 300) or url
        snippet = ""
        sm = re.search(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>',
                       b, re.S) or re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        if sm:
            snippet = _clean(_strip_tags(sm.group(1)), 1000)
        out.append({"title": title, "url": url, "snippet": snippet})
    return out[:num]


def bing_search(cfg: WebSearchConfig, query: str, *, num: int,
                language: str) -> list[dict]:
    """Bing HTML 免 Key 检索（抓 b_algo）。退化结果按失败处理（见模块头）。"""
    params: dict[str, str] = {
        "q": query,
        "count": str(num),
        "setlang": (language or "zh-hans"),
        "mkt": cfg.bing_market or "zh-CN",
    }
    headers = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
               "Accept": _ACCEPT_HTML}
    try:
        r = httpx.get(_BING_URL, params=params, headers=headers,
                      **_http_kwargs(cfg))
    except httpx.HTTPError as e:
        raise WebSearchError(f"Bing 请求失败：{e}") from e
    if r.status_code != 200:
        raise WebSearchError(f"Bing HTTP {r.status_code}")
    rows = _parse_bing_html(r.text, num)
    degraded, why = _looks_degraded(query, rows)
    if degraded and cfg.strict:
        raise _Degraded(f"Bing 结果退化：{why}")
    return rows


# ---------------------------------------------------------------- ddgs

def ddgs_search(query: str, *, num: int, timeout_s: float,
                proxy: str = "") -> list[dict]:
    """DuckDuckGo 免 Key 检索（ddgs 包；缺失/异常即本后端不可用）。"""
    try:
        from ddgs import DDGS
    except ImportError as e:
        raise WebSearchError(
            "ddgs 未安装（pip install ddgs，或配 searxng_url 启用自建后端）") from e
    try:
        kw: dict[str, Any] = {}
        if proxy:
            kw["proxy"] = proxy
        try:
            ddgs = DDGS(timeout=min(int(timeout_s), 30), **kw)
        except TypeError:  # 旧版构造器不收 timeout/proxy
            ddgs = DDGS()
        rows = ddgs.text(query, max_results=num)
    except Exception as e:  # noqa: BLE001 —— ddgs 反爬变动/限流统一转后端失败
        raise WebSearchError(f"ddgs 查询失败: {e}") from e
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = _clean(row.get("href") or row.get("url"), 2000)
        if not url:
            continue
        out.append({"title": _clean(row.get("title"), 300) or url,
                    "url": url,
                    "snippet": _clean(row.get("body") or row.get("snippet"), 1000)})
    return out[:num]


# ---------------------------------------------------------------- 编排

def search_web(query: str, *, num: int | None = None,
               engines: str = "", language: str = "",
               config_path: str | Path = DEFAULT_CONFIG_PATH
               ) -> tuple[list[dict], str]:
    """联网检索入口：SearXNG → DuckDuckGo → Bing → ddgs，全失败抛 WebSearchError。

    返回 (results, backend)——backend 供工具层回填与审计。空结果是合法
    检索结果（某后端 200 但确无结果时直接返回空表）；退化结果会被相关性闸
    拦下并继续降级（除非 strict=false）。"""
    cfg = WebSearchConfig.from_file(config_path)
    n = _clamp_num(num, cfg.num)
    engines = (engines or cfg.engines).strip()
    language = (language or cfg.language).strip()

    errs: list[str] = []

    if cfg.searxng_url:
        try:
            return searxng_search(cfg, query, num=n, engines=engines,
                                  language=language), "searxng"
        except WebSearchError as e:
            errs.append(f"SearXNG: {e}")
    else:
        errs.append("searxng_url 未配置")

    if not cfg.disable_ddg:
        try:
            return ddg_html_search(cfg, query, num=n, language=language), "ddg"
        except WebSearchError as e:
            errs.append(f"DuckDuckGo: {e}")
        except _Degraded as e:
            errs.append(f"DuckDuckGo 退化: {e}")

    if not cfg.disable_bing:
        try:
            return bing_search(cfg, query, num=n, language=language), "bing"
        except WebSearchError as e:
            errs.append(f"Bing: {e}")
        except _Degraded as e:
            errs.append(str(e))

    try:
        return ddgs_search(query, num=n, timeout_s=cfg.timeout_s,
                           proxy=cfg.proxy), "ddgs"
    except WebSearchError as e:
        errs.append(f"ddgs: {e}")

    raise WebSearchError(
        "；".join(errs)
        + "（排查：① 配 config/web_search.json 的 proxy 指向可用正向代理"
          "（本机 7890）；② 或 Docker 起 SearXNG 并配 searxng_url；"
          "③ 或 pip install ddgs）")
