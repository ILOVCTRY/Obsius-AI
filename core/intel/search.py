"""Google News RSS and optional SearXNG search for intelligence articles."""

from __future__ import annotations

import html
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlparse

import httpx

from core.websearch import (
    DEFAULT_CONFIG_PATH,
    WebSearchConfig,
    WebSearchError,
    searxng_search,
)

_GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
# Google News 中转链接解析的重试预算。经代理实测：瞬时 TLS 中断（
# SSL: UNEXPECTED_EOF_WHILE_READING）很常见，重试通常即可成功；而 429/风控
# 属于确定性拒绝，不重试（只会加重风控），故只在传输层异常时退避重试。
GOOGLE_RESOLVE_ATTEMPTS = 3
GOOGLE_RESOLVE_BACKOFF_S = 0.6
# SearXNG 空结果重试：引擎有 suspended 冷却窗，单次 0 条不代表全挂。
SEARCH_ATTEMPTS = 3
SEARCH_BACKOFF_S = 0.8
# 单次 SearXNG 查询的召回硬顶约 20~25 条（受引擎侧每页条数限制），调大 num
# 也不会更多。要「内容不少」只能多角度各查一次再合并去重——实测
# `内网渗透` 单查 23 条，加 3 个相关角度后累计 101 条。
QUERY_EXPANSION_LIMIT = 3
# 扩展查询必须**串行且留间隔**。实测 3 路并发会立刻把 brave 打成
# `Suspended: too many requests`（冷却 180s），连带主查询一起失败——
# 比不扩展更糟。单引擎环境下并发不是加速，是自杀。
QUERY_EXPANSION_GAP_S = 4.0
# 扩展两次之间必须间隔够久。brave 累计约 6~8 次请求就会被挂起 180s，而扩展
# 一次就要打 3 个请求；间隔太短会让「第一次有结果、第二次 0 条」成为常态，
# 比召回少更难用。60s 对应约 4 次/分钟的稳态频率。
_EXPAND_COOLDOWN_S = 60.0
# 引擎被挂起时，SearXNG 本身会在 180s 内直接返回空结果而不报错。把「刚刚
# 拿过 0 条」的时刻记下来，冷却期内直接跳过扩展——否则用户每点一次都在
# 无效请求上等 3 次退避重试，表现为「转很久然后 0 条」。
_RATE_LIMIT_COOLDOWN_S = 120.0
_expand_lock = threading.Lock()
# `None` 表示「本次进程还没扩展过」。不能用 0 当初始值：`time.monotonic()`
# 的起点是系统无关的任意值，通常远大于 cooldown，会让首次判断直接放行——
# 恰好把「进程刚启动」误判成「早已过冷却」。
_last_expand_at: float | None = None
_last_empty_at: float | None = None
# 安全域相关术语表：用于把用户的一句话扩成多角度查询。
# 只做「同域近义词」，不做泛化（例如不会把「内网渗透」扩成「网络安全」，
# 那会把新闻和跑题内容全带进来）。
_RELATED_TERMS: dict[str, tuple[str, ...]] = {
    "内网渗透": ("横向移动", "域渗透", "内网漫游", "后渗透", "权限维持", "Kerberos 攻击"),
    "渗透测试": ("漏洞利用", "提权", "横向移动", "边界突破", "渗透实战"),
    "权限提升": ("提权", "本地提权", "内核提权", "SUID 提权", "令牌窃取"),
    "权限维持": ("后门持久化", "计划任务 后门", "注册表 自启动", "rootkit"),
    "免杀": ("免杀绕过", "EDR 绕过", "shellcode 加密", "加载器 免杀"),
    "逆向": ("反编译", "静态分析 IDA", "动态调试", "脱壳"),
    "病毒样本分析": ("恶意样本分析", "沙箱 行为分析", "IOC 提取", "家族 溯源"),
    "虚拟化壳脱壳": ("VMP 脱壳", "虚拟机保护 还原", "OLLVM 反混淆", "壳 逆向"),
    "容器安全": ("容器逃逸", "Kubernetes 安全", "Docker 逃逸", "云原生 攻击面"),
    "云主机安全": ("云环境 渗透", "AK SK 泄露", "元数据服务 攻击", "云上 横向移动"),
    "web3": ("智能合约 漏洞", "链上 攻击分析", "DeFi 安全", "私钥 泄露"),
}
_TIME_QUERY = {
    "week": "7d",
    "month": "30d",
    "year": "1y",
}


def _text(value: str | None, limit: int) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _google_article_id(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    if parsed.hostname != "news.google.com":
        return ""
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "articles" and re.fullmatch(r"[A-Za-z0-9_-]{8,512}", parts[-1]):
        return parts[-1]
    return ""


def _parse_google_signature(page: str) -> tuple[str, str] | None:
    signature = re.search(r'data-n-a-sg="([^"]+)"', page)
    timestamp = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not signature or not timestamp:
        return None
    return signature.group(1), timestamp.group(1)


def _parse_google_batchexecute(text: str) -> str:
    body = text.split("\n\n", 1)[-1].lstrip()
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1].lstrip()
    try:
        rows = json.loads(body)
        if rows and isinstance(rows[-1], list) and rows[-1] and rows[-1][0] in {"di", "e"}:
            rows = rows[:-1]
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            payload = row[2]
            if row[0] != "wrb.fr" or row[1] != "Fbv4je":
                continue
            if isinstance(payload, str):
                payload = json.loads(payload)
            if isinstance(payload, list) and len(payload) > 1 and payload[0] == "garturlres":
                target = str(payload[1])
                parsed = urlparse(target)
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    return target
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        pass
    if body.startswith("[") and "<" not in body:
        raise WebSearchError("Google News 返回的跳转数据无法解析")
    raise WebSearchError("Google News 未返回可用的来源文章链接")


def _resolve_error_reason(exc: Exception) -> str:
    """把 httpx/OpenSSL 原始异常翻译成用户能据此行动的短句。

    原始文本（``[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of
    protocol (_ssl.c:1028)``）会原样弹到界面上，用户既看不懂也不知道该做什么。
    返回的是 ``解析<原因>`` 的原因部分，调用方负责拼前缀。
    """
    text = str(exc).lower()
    if "unexpected_eof" in text or "ssl" in text or "protocol" in text:
        return "连接被中断（代理链路不稳定），请重试"
    if "timeout" in text or "timed out" in text:
        return "超时，请重试"
    if "connect" in text or "refused" in text or "unreachable" in text:
        return "无法连接，请检查网络或代理"
    return "失败，请重试"


def _resolve_failure_message(exc: Exception) -> str:
    """完整失败文案：``Google News 来源链接解析`` + 可读原因（自带处理建议）。"""
    return f"Google News 来源链接解析{_resolve_error_reason(exc)}"


def _resolve_once(cfg: WebSearchConfig, params_url: str, art_id: str,
                  headers: dict, client_kwargs: dict) -> str:
    """单次解析尝试：取签名 → batchexecute → 解析目标地址。

    每次调用都新建 Client：签名页种下的 Cookie 必须与随后的 POST 同源复用（见
    ``resolve_google_news_url`` 注释），而连接被掐断后旧 Client 的连接池已不可信。
    """
    try:
        with httpx.Client(**client_kwargs) as client:
            page = client.get(params_url)
            if page.status_code != 200:
                raise WebSearchError(f"Google News 签名页面 HTTP {page.status_code}")
            parsed = _parse_google_signature(page.text)
            if not parsed:
                raise WebSearchError("Google News 页面缺少文章签名")
            signature, timestamp = parsed
            context = [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None,
                        1, None, None, None, None, None, 0, 1], "X", "X", 1,
                       [1, 1, 1], 1, 1, None, 0, 0, None, 0]
            inner = ["garturlreq", context, art_id,
                     int(timestamp) if timestamp.isdigit() else timestamp, signature]
            envelope = ["Fbv4je", json.dumps(inner, separators=(",", ":")), None, "0"]
            payload = "f.req=" + quote(json.dumps([[envelope]], separators=(",", ":")))
            batch = client.post(
                "https://news.google.com/_/DotsSplashUi/data/batchexecute",
                content=payload,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "X-Same-Domain": "1",
                    "Referer": params_url,
                    "Origin": "https://news.google.com",
                    "Accept": "*/*",
                },
            )
    except httpx.TransportError:
        raise  # 交给外层判定是否值得重试
    except httpx.HTTPError as exc:
        raise WebSearchError(f"Google News 来源链接解析请求失败: {exc}") from exc
    # follow_redirects=True 会把 302 到 google.com/sorry 的响应变成 200 的验证页，
    # 故不能只看状态码，必须同时看最终 URL 与正文。
    blocked = (
        batch.status_code == 429
        or "sorry" in str(batch.url)
        or "unusual traffic" in batch.text[:4000].lower()
    )
    if blocked:
        raise WebSearchError(
            "Google News 拒绝了来源链接解析（出口 IP 触发异常流量校验），请稍后重试，或直接打开 Google News 页面")
    if batch.status_code != 200:
        raise WebSearchError(f"Google News 来源链接解析 HTTP {batch.status_code}")
    return _parse_google_batchexecute(batch.text)


def resolve_google_news_url(cfg: WebSearchConfig, url: str) -> str:
    """Resolve a Google News RSS article token to its publisher URL.

    **必须用同一个 Client 完成两次请求**：签名页会种下 Cookie（实测 2 个），
    独立 ``httpx.get`` + ``httpx.post`` 不共享 Cookie 时 batchexecute 直接 302
    到 ``google.com/sorry`` 验证页（比 429 更不可诊断）。复用后才是真实的
    200 / 429。

    传输层失败（``SSL: UNEXPECTED_EOF_WHILE_READING`` / 连接重置 / 超时）经代理
    实测是**瞬时**的，单次失败不代表真的解析不了，故做有限重试；429/风控类错误
    立即上抛——重试只会加重风控。
    """
    art_id = _google_article_id(url)
    if not art_id or len(art_id) > 512:
        raise WebSearchError("不是有效的 Google News 文章链接")
    source = urlparse(url)
    source_qs = parse_qs(source.query)
    hl = (source_qs.get("hl") or ["en-US"])[0]
    gl = (source_qs.get("gl") or ["US"])[0]
    ceid = (source_qs.get("ceid") or ["US:en"])[0]
    # 只有 /rss/articles/ 对非浏览器客户端返回 200；/articles/ 与 /read/ 会 302
    # 到 google.com/sorry（实测）。故统一重写为 RSS 形式再取签名。
    params_url = (
        f"https://news.google.com/rss/articles/{quote(art_id, safe='')}"
        f"?hl={quote(hl, safe='')}&gl={quote(gl, safe='')}&ceid={quote(ceid, safe='')}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    client_kwargs: dict = {"timeout": cfg.timeout_s, "follow_redirects": True,
                           "headers": headers, "trust_env": True}
    if cfg.proxy:
        client_kwargs.update(proxy=cfg.proxy, trust_env=False)
    last: httpx.TransportError | None = None
    for attempt in range(GOOGLE_RESOLVE_ATTEMPTS):
        try:
            return _resolve_once(cfg, params_url, art_id, headers, client_kwargs)
        except httpx.TransportError as exc:
            last = exc
            if attempt + 1 < GOOGLE_RESOLVE_ATTEMPTS:
                time.sleep(GOOGLE_RESOLVE_BACKOFF_S * (2 ** attempt))
    raise WebSearchError(_resolve_failure_message(last)) from last


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
        # <source url="https://www.secrss.com"> 是发布方站点根地址（实测每条都有）。
        # Google 中转链接解析被限流时，这是唯一可靠的「去哪儿找这篇」线索，
        # 故随结果一起返回，不参与打分。
        source_site = ""
        if source is not None:
            candidate = str(source.get("url") or "").strip()
            parsed_site = urlparse(candidate)
            if parsed_site.scheme in {"http", "https"} and parsed_site.hostname:
                source_site = candidate
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
            "source_site": source_site,
            "published_date": published,
        })
        if len(rows) >= max(1, min(int(num or 10), 100)):
            break
    return rows


def _mark_expanded() -> None:
    """记录一次成功扩展的时刻，用于下一次的冷却判断。"""
    global _last_expand_at
    with _expand_lock:
        _last_expand_at = time.monotonic()


def _mark_engine_ok() -> None:
    """拿到结果即说明引擎已恢复，清掉挂起标记。"""
    global _last_empty_at
    with _expand_lock:
        _last_empty_at = None


def _mark_engine_empty() -> None:
    """记下「刚刚拿到 0 条」，供下一次判断是否仍在挂起冷却中。"""
    global _last_empty_at
    with _expand_lock:
        _last_empty_at = time.monotonic()


def _engine_suspended() -> bool:
    """引擎是否处于挂起冷却期（刚拿过 0 条且未超时）。"""
    with _expand_lock:
        if _last_empty_at is None:
            return False
        return (time.monotonic() - _last_empty_at) < _RATE_LIMIT_COOLDOWN_S


def _expansion_allowed(extras: list[str]) -> bool:
    """冷却期内跳过扩展，只保留主查询——避免把引擎打进 180s suspended。"""
    if not extras:
        return False
    with _expand_lock:
        if _last_empty_at is not None and \
                (time.monotonic() - _last_empty_at) < _RATE_LIMIT_COOLDOWN_S:
            # 引擎刚被挂起，现在扩展只会继续加重。
            return False
        if _last_expand_at is None:
            return True
        return (time.monotonic() - _last_expand_at) >= _EXPAND_COOLDOWN_S


def expand_queries(query: str, limit: int = QUERY_EXPANSION_LIMIT) -> list[str]:
    """把一句话扩成「原句 + 同域相关角度」，用于突破单次查询的召回硬顶。

    只取命中的术语组，按配置顺序取前 `limit` 个，且不重复原句。
    未命中任何术语组时返回空列表（调用方退化为单查询，不会凭空造词）。
    """
    base = str(query or "").strip()
    if not base:
        return []
    lowered = base.casefold()
    extras: list[str] = []
    for key, terms in _RELATED_TERMS.items():
        if key.casefold() not in lowered:
            continue
        for term in terms:
            if term.casefold() not in lowered and term not in extras:
                extras.append(term)
    return extras[:max(0, int(limit))]


def search_intel(query: str, search_config: dict | None = None, *, num: int = 10,
                 time_range: str = "", config_path: str = DEFAULT_CONFIG_PATH
                 ) -> tuple[list[dict], str]:
    """以 SearXNG 网页检索为主返回技术文章，不再使用 Google News 新闻流。

    为什么弃用 Google News：它是**新闻**聚合，召回天然偏「报道」而非「技术文章」，
    且对中文技术内容覆盖很差（实测「内网渗透」近半年仅 18 条，其中混入色情垃圾页、
    时政内容与完全无关的财经新闻）。SearXNG 走通用网页索引，同一查询可稳定返回
    20+ 条技术文档/博客/厂商研究（安全脉搏、FreeBuf、腾讯云开发者社区等）。

    多引擎：SearXNG 侧默认启用 google/bing/brave，任一可用即返回；调用方不再按
    engine 名过滤（此前只收 `source == "google"` 的行，导致 brave 有结果也判空）。
    """
    cfg = WebSearchConfig.from_file(config_path)
    cfg.searxng_url = ""
    search_config = search_config if isinstance(search_config, dict) else {}
    searx = search_config.get("searxng") or {}
    errors: list[str] = []
    if not (isinstance(searx, dict) and searx.get("enabled")):
        raise WebSearchError("检索需要启用 SearXNG（设置 → 情报 → 检索服务）")

    port = max(1, min(int(searx.get("port", 8888)), 65535))
    cfg.searxng_url = f"http://127.0.0.1:{port}"
    engines = str(searx.get("engines") or "google,bing,brave")
    want = max(1, min(int(num or 10), 50))

    # 单次空结果不等于「引擎全挂」：SearXNG 的引擎有 suspended 冷却窗口
    # （google 一旦 access denied 会被挂 180s，期间只有 brave/bing 供结果），
    # 而 brave 偶尔也会超时。实测同一 query 相邻两次可能一次 31 条、一次 0 条，
    # 直接把 0 条当失败会让用户看到「搜索到的内容少」甚至空列表。
    # 故空结果退避重试；每次换一下 engines 顺序，避免总是先打被挂的引擎。
    def _fetch(q: str, attempts: int = SEARCH_ATTEMPTS) -> list[dict]:
        nonlocal last_error
        for attempt in range(max(1, attempts)):
            try:
                rows = searxng_search(
                    cfg, q, num=want,
                    engines=engines, language="zh-CN",
                    # SearXNG 的 time_range 只认 day/week/month/year，传 half_year 会直接
                    # 抛 SearxParameterException（整次检索失败），故此处不透传。
                    time_range="", include_metadata=True,
                )
            except (TypeError, ValueError, WebSearchError) as exc:
                last_error = str(exc)
                rows = []
            if rows:
                _mark_engine_ok()
                return rows
            if attempt + 1 < attempts:
                time.sleep(SEARCH_BACKOFF_S * (2 ** attempt))
        _mark_engine_empty()
        return []

    last_error = ""
    if _engine_suspended():
        # 引擎刚被挂起（SearXNG 会静默返回空），此刻重试只是白等。
        raise WebSearchError(
            f"检索引擎被限流挂起（{engines}），请约 2 分钟后重试；"
            "频繁检索会触发上游限流")
    primary = _fetch(query)
    if not primary:
        raise WebSearchError(
            f"SearXNG 未返回结果（引擎 {engines} 可能均不可用"
            + (f"：{last_error}" if last_error else "")
            + "；可在 SearXNG 页面确认出口代理是否正常）")

    # 召回扩展：单次查询有 ~20 条的硬顶，多角度各查一次再合并去重。
    # 这是「搜索到的内容少」的正面解法——实测 23 → 101 条。
    # 必须串行 + 间隔：并发会触发上游限流，连主查询一起废掉。
    # 任一次扩展命中限流就停止后续扩展并保留已有结果——宁可少几条，
    # 也不能把用户已经拿到的召回弄丢。
    extras = expand_queries(query)
    if _expansion_allowed(extras):
        merged: list[dict] = list(primary)
        seen = {str(r.get("url") or "") for r in merged}
        for index, extra in enumerate(extras):
            if index:
                time.sleep(QUERY_EXPANSION_GAP_S)
            # 扩展查询只打一次：此刻引擎很可能已在限流冷却中，重试只会
            # 延长 suspended 时间、拖慢响应，不会拿到结果。
            rows = _fetch(extra, attempts=1)
            if not rows:
                # 空结果说明引擎已被限流/挂起，继续打只会加重。
                break
            for row in rows:
                url = str(row.get("url") or "")
                if not url or url in seen:
                    continue
                seen.add(url)
                merged.append(row)
        if len(merged) > len(primary):
            _mark_expanded()
            return merged, "searxng+expand"
        # 跑了扩展却一条新内容都没有：引擎已在限流边缘（`_fetch` 会置挂起标记），
        # 同理需要解释，否则用户看到的就是「怎么这么少」。
        return primary, "searxng+throttled"
    elif extras:
        # 有扩展词但处于冷却期 —— 这是「结果比上次少」的确定原因，需回报给
        # 前端解释，否则用户只看到数量变少，会当成功能退化。
        return primary, "searxng+throttled"
    return primary, "searxng"
