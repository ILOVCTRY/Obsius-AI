"""术语匹配共享原语（route-injection-hardening，2026-09-24）。

背景：route.json 路由键、route_index match 词、SKILL keywords 都用纯子串
匹配，2-3 字母英文术语（ak/sk/ai/pe…）在英文任务描述里确定性误命中
（make/backup 含 ak、main/email 含 ai）。本模块给出唯一匹配口径：

- 术语含中文 → 子串（中文无词边界，保持原行为，双向子串自然成立）；
- 纯 ASCII  → 词边界：两侧不得相邻 [a-z0-9]（underscore 视为边界）；
- 以 ``*`` 结尾 → 前缀语义（如 ret2* 命中 ret2text/ret2shellcode，
  house* 命中 house-of）；含中文退化为前缀子串；
- 中英混合 → 子串。

零内部依赖，供 router / kbindex / routeindex 复用，避免互相 import。
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[一-鿿]")


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
