"""术语匹配共享原语（route-injection-hardening，2026-09-24）。

背景：route.json 路由键、route_index match 词、SKILL keywords 都用纯子串
匹配，2-3 字母英文术语（ak/sk/ai/pe…）在英文任务描述里确定性误命中
（make/backup 含 ak、main/email 含 ai）。本模块给出唯一匹配口径：

- 术语含中文 → 子串（中文无词边界，保持原行为，双向子串自然成立）；
- 纯 ASCII  → 词边界：两侧不得相邻 [a-z0-9]（underscore 视为边界）；
- 以 ``*`` 结尾 → 前缀语义（如 ret2* 命中 ret2text/ret2shellcode，
  house* 命中 house-of）；含中文退化为前缀子串；
- 中英混合 → 子串。

另含**中文查询切词**（``query_tokens``，2026-10-19 中文检索修复）：中文没有
空格词边界，自然语言问句（「如何测试文件上传漏洞」）整串当 needle 必然零
命中。本模块给出统一切分：ASCII 词元原样保留，中文串 ≤4 字整段保留、超长
串再切 2-gram，并剔除问句虚词——kbsearch（FTS 索引）与 writing（legacy
扫描器）共用同一口径，避免两条检索路径对同一句查询给出不同答案。

零内部依赖，供 router / kbindex / routeindex / kbsearch 复用，避免互相 import。
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[一-鿿]")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:+/-]*")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")

# 中文查询里**不承载检索意图**的高频虚词/疑问词/客套词。命中它们只会把
# 无关文档拉进结果（「如何」「怎么」「有没有」几乎出现在任何一篇中文文档里），
# 故切词后整词丢弃。词表刻意保守：只收纯功能词，领域词（测试/验证/分析）
# 一律不动——它们是真实手法名（「测试点」「验证码」）。
_QUERY_STOPWORDS = frozenset({
    "如何", "怎么", "怎样", "什么", "哪些", "为何", "为什么", "是否",
    "有没有", "能否", "能不", "不能", "可以", "应该", "需要",
    "我想", "想找", "帮我", "我们", "你们", "这个", "那个", "这些", "那些",
    "一下", "一个", "看看", "试试", "进行", "可能", "使用", "通过",
    "还是", "或者", "以及", "但是", "就是", "没有", "相关",
    "方法", "打法", "思路", "技巧", "的手", "做的", "么做", "么打",
    # 单字虚词：只作分隔符用（剥除后不留词元），否则「绕过的打法」会剩「过的」
    # 这种跨虚词边界的碎片，而它在语料里真实出现（df>0）能骗过覆盖率门槛。
    "的", "了", "和", "与", "及", "或", "在", "是", "有", "也", "都", "就",
    "很", "再", "又", "还", "把", "被", "对", "从", "到", "给", "让", "而",
    "但", "并", "则", "等", "这", "那", "个", "些", "吧", "呢", "啊", "吗",
})

# 中文整段保留的最大长度：≤4 字的多为完整手法名（文件上传/越权/反序列化），
# 整段比 2-gram 更精确；超长串才退化切分。
_CJK_KEEP_CHARS = 4
# 虚词最长长度（贪心剥除时上界），按词表实际最大长度算，留 1 字余量。
_STOPWORD_MAX_LEN = max(len(w) for w in _QUERY_STOPWORDS)


def _strip_stopwords(run: str) -> list[str]:
    """把一段中文按**最长匹配**剥掉虚词，返回剩余实义片段（保序）。

    必须整词剥而不能只按 2-gram 过滤：``我想找一个文件上传绕过的打法`` 只做
    2-gram 过滤会剩 找一/过的/的打 三个跨虚词边界的垃圾词元（它们在语料里
    偶尔真实出现，df>0，能骗过覆盖率门槛）。先剥词再切，垃圾词元就不生成。
    """
    spans: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(run):
        hit = 0
        # 下限到 1：单字虚词（的/了/和）必须能剥，否则「绕过的打法」会剩
        # 「过的」这种跨虚词边界的碎片。
        for n in range(min(_STOPWORD_MAX_LEN, len(run) - i), 0, -1):
            if run[i:i + n] in _QUERY_STOPWORDS:
                hit = n
                break
        if hit:
            if cur:
                spans.append("".join(cur))
                cur = []
            i += hit
            continue
        cur.append(run[i])
        i += 1
    if cur:
        spans.append("".join(cur))
    return spans


def query_tokens(query: str) -> tuple[list[str], list[str]]:
    """中文友好切词 → (ASCII 词元, 中文词元)。

    口径：
    - ASCII：``[A-Za-z0-9_][A-Za-z0-9_.:+/-]*``，长度 >1 才收（单字母噪声大）；
    - 中文：先按最长匹配剥掉 ``_QUERY_STOPWORDS`` 里的纯虚词，剩余实义片段
      ≤4 字整段收（「文件上传」「反序列化」），>4 字再切 2-gram；
    - 两类词元保序去重。

    调用方据此决定 AND/降级策略：**ASCII 词元是硬约束**（用户明确打了
    技术词），中文词元是软约束（自然语言里必有虚词噪声）。
    """
    q = (query or "").lower()
    ascii_tokens = [t for t in _ASCII_TOKEN_RE.findall(q) if len(t) > 1]
    cjk: list[str] = []
    for run in _CJK_RUN_RE.findall(q):
        for seg in _strip_stopwords(run):
            if len(seg) <= _CJK_KEEP_CHARS:
                cjk.append(seg)
            else:
                cjk.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    ascii_tokens = [t for t in dict.fromkeys(ascii_tokens)
                    if t not in _QUERY_STOPWORDS]
    cjk = [t for t in dict.fromkeys(cjk) if t not in _QUERY_STOPWORDS]
    return ascii_tokens, cjk


def term_matches(term: str, text_lower: str) -> bool:
    """术语 term 是否命中已 lower 的文本 text_lower。口径见模块 docstring。"""
    t = (term or "").strip().lower()
    text = text_lower or ""
    if not t or not text:
        return False
    wildcard = t.endswith("*") and "*" not in t[:-1]
    if wildcard:
        t = t[:-1]
        if not t:
            return False
        if _CJK_RE.search(t):
            return t in text
        return re.search(r"(?<![a-z0-9])" + re.escape(t), text) is not None
    if _CJK_RE.search(t):
        return t in text
    return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])",
                     text) is not None
