"""情报文章日期与检索排序辅助。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

_WINDOWS = {"day": 1, "week": 7, "month": 30, "half_year": 183, "year": 365}
_WINDOW_FRESHNESS = {"day": 0.95, "week": 0.80, "month": 0.55, "half_year": 0.4, "year": 0.25}

# ---- 主题相关性 --------------------------------------------------------------
# 安全技术语料。命中越多越可能「讲技术」，而不是「报道某公司出事了」。
# 这是**加分项**，不命中不等于淘汰（可能是新概念），只是排后。
_SECURITY_TERMS = (
    "漏洞", "渗透", "攻击", "防御", "提权", "注入", "逆向", "样本", "木马", "病毒",
    "勒索", "钓鱼", "免杀", "绕过", "沙箱", "逃逸", "利用", "复现", "exploit", "poc",
    "cve", "rce", "xss", "ssrf", "csrf", "sql", "0day", "exp", "payload", "shellcode",
    "webshell", "内网", "横向", "域控", "权限", "取证", "溯源", "蜜罐", "应急响应",
    "加固", "基线", "审计", "风控", "加密", "解密", "签名", "证书", "认证", "授权",
    "供应链", "容器", "云原生", "k8s", "kubernetes", "docker", "虚拟化", "内核",
    "frida", "ida", "ghidra", "wireshark", "burp", "nmap", "metasploit", "cobalt",
    "apt", "c2", "rat", "botnet", "ddos", "waf", "edr", "xdr", "siem", "soc",
)

# 技术深度信号：出现即说明是「讲清楚怎么做的」文章，而不是快讯。
_DEPTH_TERMS = (
    "原理", "分析", "复现", "实战", "详解", "深入", "源码", "从零", "手把手",
    "调试", "踩坑", "笔记", "总结", "教程", "入门", "进阶", "实践", "案例",
    "构造", "编写", "实现", "机制", "流程", "思路", "方法", "技巧",
)

# 垃圾/低质页特征。命中即判为噪声，直接不进结果。
# 实测这些页面确实会从搜索引擎索引里混进来（「泳装美女被扒开」「动漫被❌脱脱内内」
# 等色情导流页曾出现在「内网渗透」的搜索结果里）。
_SPAM_PATTERNS = (
    "免费全集", "高清完整", "在线观看", "无码", "裸", "脱内内", "吃奶", "泳装美女",
    "色情", "黄片", "av片", "成人片", "约炮", "裸聊", "福利视频", "激情", "少妇",
    "澳门赌场", "博彩", "时时彩", "六合彩", "开奖结果", "彩票投注",
    "加微信", "加qq", "代开发票", "办证", "贷款秒批",
)

# 与安全技术无关的领域词：出现且**无任何安全词**时判为跑题。
_OFFTOPIC_TERMS = (
    "信托", "理财", "基金", "股票", "楼市", "房价", "婚恋", "情感", "星座",
    "娱乐八卦", "明星", "综艺", "电视剧", "演唱会", "球赛", "比分",
)

# 高可信技术来源：厂商研究、攻防社区、独立技术博客。
_TRUSTED_HOSTS = (
    "portswigger.net", "seebug.org", "paper.seebug.org", "freebuf.com",
    "anquanke.com", "secpulse.com", "xz.aliyun.com", "cloud.tencent.com",
    "developer.aliyun.com", "bbs.ichunqiu.com", "www.anquanke.com",
    "ti.threatbook.com", "blog.qualys.com", "research.checkpoint.com",
    "unit42.paloaltonetworks.com", "cloud.google.com", "github.com",
    "github.io", "gitbook.io", "readthedocs.io", "cnblogs.com", "juejin.cn",
    "zhihu.com", "segmentfault.com", "jianshu.com", "51cto.com", "csdn.net",
    "owasp.org", "nvd.nist.gov", "cve.org", "exploit-db.com",
)

# 新闻站点：命中时降权（用户明确要技术文章，不要新闻）。
_NEWS_HOSTS = (
    "sina.com.cn", "sina.cn", "sohu.com", "163.com", "qq.com/news", "ifeng.com",
    "thepaper.cn", "jiemian.com", "yicai.com", "cls.cn", "stcn.com", "eastmoney.com",
    "leiphone.com", "36kr.com", "huxiu.com", "tmtpost.com", "donews.com",
    "cnbeta.com", "ithome.com", "pconline.com.cn", "zol.com.cn",
)


def _host_of(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def spam_score(item: dict) -> float:
    """1.0 = 干净；0.0 = 确认是垃圾页（调用方应直接丢弃）。"""
    text = f"{item.get('title', '')} {item.get('summary', '')}".casefold()
    if any(p in text for p in _SPAM_PATTERNS):
        return 0.0
    title = str(item.get("title") or "")
    if title:
        # 导流页典型形态：标题里塞 emoji / 禁止符号做「防屏蔽」变形，
        # 如「动漫被❌脱脱内内网站…」。词表匹配不到，靠符号密度兜住。
        odd = sum(1 for ch in title if not (ch.isalnum() or ch.isspace()
                                            or ch in "，。、：；！？（）()《》-—_|/·.,:;!?[]【】"))
        if odd / max(1, len(title)) > 0.30:
            return 0.0
        # 「…高清完整…免费全集」这类影视/导流模板，即使词表被绕过也要拦住：
        # 同时出现「完整/全集/在线」与省略号或重复短句，基本可判非技术内容。
        promo = sum(1 for p in ("完整", "全集", "在线", "免费", "下载") if p in title)
        if promo >= 2 and ("…" in title or "..." in title or title.count("-") >= 2):
            return 0.0
    return 1.0


def relevance_score(item: dict, *, query: str = "") -> float:
    """0..1 的主题相关性。安全技术词密度 + 查询词命中 + 来源可信度。"""
    title = str(item.get("title") or "").casefold()
    summary = str(item.get("summary") or "").casefold()
    text = f"{title} {summary}"
    if not text.strip():
        return 0.0

    hits = sum(1 for term in _SECURITY_TERMS if term in text)
    depth = sum(1 for term in _DEPTH_TERMS if term in text)

    # 安全词密度：命中 3 个基本可确认是技术内容
    density = min(1.0, hits / 3.0)
    # 深度信号最多再加 0.3
    depth_bonus = min(0.3, depth * 0.1)

    # 查询词覆盖：用户搜什么，标题里就该出现什么
    tokens = [t for t in re.split(r"[\s,，、;；]+", str(query or "").casefold()) if len(t) >= 2]
    covered = sum(1 for t in tokens if t in text)
    query_ratio = (covered / len(tokens)) if tokens else 0.0

    host = _host_of(str(item.get("url") or ""))
    trusted = 1.0 if any(host.endswith(h) for h in _TRUSTED_HOSTS) else 0.0
    newsy = 1.0 if any(h in host for h in _NEWS_HOSTS) else 0.0

    score = 0.45 * density + depth_bonus + 0.35 * query_ratio + 0.20 * trusted
    score -= 0.25 * newsy

    # 跑题判定：有非技术领域词且完全没有安全词 → 大幅降权
    if any(t in text for t in _OFFTOPIC_TERMS) and hits == 0:
        score -= 0.5
    return max(0.0, min(1.0, score))


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


def ranking_score(item: dict, *, window: str = "month", query: str = "") -> float:
    """0..100 排序分：相关性 × 质量 × 时效。

    此前只有 quality×freshness×mainstream，**完全没有主题相关性**，于是
    「报道某公司出事了」的新闻与技术长文同分，实测新闻常压过技术文章。
    现把 relevance 提到主权重（0.5），并把新闻站整体降权。
    """
    quality = quality_score(item)
    relevance = relevance_score(item, query=query)
    known = normalize_day(item.get("published_day") or item.get("published_at"))
    if known:
        try:
            age = max(0, (datetime.now(timezone.utc).date() - date.fromisoformat(known)).days)
        except ValueError:
            age = 9999
        freshness = max(0.0, 1.0 - age / 177.0)
    else:
        freshness = window_freshness(str(item.get("window") or window))
    blended = (relevance ** 0.9) * (quality ** 0.6) * (freshness ** 0.25)
    return round(100 * blended * mainstream_score(item), 1)
