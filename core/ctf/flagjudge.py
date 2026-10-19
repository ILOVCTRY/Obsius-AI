"""CTF flag 判定内核（借鉴 hxbai `engine/judge.py`，2026-10-19）。

**要解决的问题**：CTF 会话里「是不是拿到了 flag」现在完全靠模型自述 + 人类肉眼
核对。这带来三个真实故障：

1. **误报**：把 `example{...}`、`flag{test}` 这类题面/README 里的占位符当答案，
   或者把 `{%csrf_token%}`、`${user.name}` 这类模板语法当成 flag（正则 `\\w+{...}`
   全都能匹配上）。
2. **漏报**：flag 不是原样躺在输出里，而是 base64/hex/url 编码/rot13 变换过的；
   模型说自己"解码得到了 flag"，但没人验证这个解码能不能回环。
3. **串题**：多题并发时，A 题的 flag 出现在 B 题会话的 `/tmp` 共享目录或上游
   扫描结果里，被当成 B 题的答案提交。

本模块提供三层纯函数（零 LLM、零 IO）：

- **候选抽取** `extract_candidates(text)`：从任意文本抓 `<前缀>{<内层>}` 形态，
  并剔除模板语法/代码片段（`_is_codeish`）。
- **置信打分** `flagishness(token)`：前缀可信度 + 内层熵 + 诱饵词惩罚，返回浮点分；
  `confident_flags(candidates)` 用阈值 + 同形簇抑制（同一前缀出现 ≥4 个不同内层
  = 刷屏的假阳性，全部丢弃）筛出可提交集合。
- **变换回环** `roundtrip_verify(value, source, transform)`：模型声明
  `submit_flag: xxx | source: 原始字节 | transform: base64` 时，验证
  `base64(value) ∈ source`——**真解码能回环，瞎猜不能**。
- **跨题家族** `family_key(flag)` / `neighbor_owner(flag, registry)`：内层高度
  相似（互为子串）的 flag 归为同族，用于识别「邻居题的 flag」。

**与项目既有口径的关系**：`core/chat/runtime.py` 的 CTF 提示里已经写了
「用户未说明 flag 格式时，只有 `flag{...}`/`xxxx{...}` 明确格式才可作正式 flag」——
本模块把那句自然语言纪律变成可执行判定，供工具层与收尾闸复用。
"""

from __future__ import annotations

import base64
import codecs
import re
from collections import Counter
from urllib.parse import quote

__all__ = [
    "extract_candidates",
    "flagishness",
    "confident_flags",
    "is_confident_flag",
    "parse_flag_declarations",
    "roundtrip_verify",
    "family_key",
    "inner_of",
    "neighbor_owner",
    "natural_language_inner",
    "is_bait_prefix",
    "is_bait_inner",
    "FLAGISH_FLOOR",
    "MIN_INNER_LEN",
]

# 内层字符集：排除 {} 换行 引号 反引号 $ ; | < > \ ( ) 与空白（防把 JSON/代码整段吃进来）
_INNER = r"[^{}\r\n'\"`$;|<>\\()]{2,200}"
_CAND_RX = re.compile(r"([A-Za-z][A-Za-z0-9_.\-]{1,30}\{" + _INNER + r"\})")
_INNER_OF = re.compile(r"\{(" + _INNER + r")\}$")

# 代码/模板语法特征（出现在内层即不是 flag）
_CODEISH = re.compile(
    r"\.\.\.|=>|::|&&|\|\||\bas\b|\bfunction\b|\bLIMIT\b|\b[a-z][a-z-]{2,}:|--[\w-]*:|(?-i:\.[A-Z]{3,})",
    re.I)
_STRONG_CODEISH = re.compile(r"\.\.\.|=>")
_MACRO_INNER_RX = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")

# 可信前缀（大小写不敏感）：已知赛事前缀 + 任何以 ctf 结尾的前缀
_KNOWN_PREFIXES = ("flag", "htb", "ctf", "canary", "key", "picoctf", "dasctf",
                   "hkcert", "n1ctf", "balsn", "hitcon", "rwctf", "0ctf")
# 这些前缀常出现在前端模板/脚本里（`{% if %}` / `${x}` / `@header{}`），不是 flag
_PREFIX_KEYWORDS = frozenset({
    "finally", "catch", "try", "else", "do",
    "const", "let", "var", "return",
    "import", "export",
    "header", "output", "plugin",
})

# 诱饵：题面示例/占位符（`flag{example}` / `fake_flag{...}` / `dummy{...}`）
_BAIT_AFFIX = re.compile(
    r"^(n|no|non|not|fake|faux|dummy|decoy|invalid|wrong|bad|"
    r"sample|example|test|honeypot|trap|bait|nota|placeholder|todo|xxx)[\W_]*$")
_BAIT_WORDS = frozenset(("fake", "dummy", "decoy", "honeypot", "trap", "bait",
                         "invalid", "example", "sample", "test", "nope",
                         "garbage", "junk", "placeholder", "todo", "xxx"))

_FLAGWORD_RX = re.compile(r"(?i)(flag|ctf|htb)")

# 置信阈值：≥ 此分才认为「可以提交」。见 flagishness() 的打分表。
FLAGISH_FLOOR = 2.0
# 题目声明格式的加权（challenge-flag-ledger v47）：题目 meta.flag_format 声明了
# ``flag{}`` 而本 flag 前缀匹配 → 加分。**只加不减**：声明的格式是**提示不是
# 约束**——真实比赛里题面写 flag{} 而实际是 DASCTF{} 的情况很常见，硬卡格式会
# 误杀正确答案；而不加权则「题目声明了格式」这个强信号被浪费。
FORMAT_MATCH_BONUS = 1.0
# 同形簇抑制：同一 (前缀可信, 内层长度桶, 内层形态) 出现 ≥ 此数 → 视为刷屏假阳性
SPRAY_MIN = 4
# 内层最短长度：`flag{x}` / `flag{1}` 是手工占位（真实赛事 flag 内层远长于此）
MIN_INNER_LEN = 3

# 变换名 → 编码函数（roundtrip_verify 用；只收可逆且确定性的）
_ROUNDTRIP_XFORMS = {
    "url": lambda s: quote(s, safe=""),
    "urlencode": lambda s: quote(s, safe=""),
    "urldecode": lambda s: quote(s, safe=""),
    "percent": lambda s: quote(s, safe=""),
    "urlencoded": lambda s: quote(s, safe=""),
    "base64": lambda s: base64.b64encode(s.encode()).decode(),
    "b64": lambda s: base64.b64encode(s.encode()).decode(),
    "hex": lambda s: s.encode().hex(),
    "rot13": lambda s: codecs.encode(s, "rot_13"),
}

_DECL_LINE_RX = re.compile(r"^[ \t]*submit_flag:[ \t]*(.+?)[ \t]*$", re.I | re.M)


# ---------------- 基础判定 ----------------

def inner_of(token: str) -> str:
    """取 `prefix{inner}` 的内层；不是该形态则返回空串。"""
    m = _INNER_OF.search(token or "")
    return m.group(1) if m else ""


def _prefix_of(token: str) -> str:
    return (token or "").split("{", 1)[0]


def _known_prefix(token: str) -> bool:
    pfx = _prefix_of(token).lower()
    return pfx in _KNOWN_PREFIXES or pfx.endswith("ctf")


def _flag_word(pfx: str) -> bool:
    return "flag" in pfx or "ctf" in pfx or "htb" in pfx


def _flag_onset(prefix: str) -> int:
    """前缀里最后一个 flag/ctf/htb 词的起始位置（`myflag` → 2；`flag` → 0）。"""
    last = -1
    for m in _FLAGWORD_RX.finditer(prefix or ""):
        last = m.start()
    return last


def is_bait_prefix(token: str) -> bool:
    """前缀是否带诱饵词（`fake_flag{}` / `example{}` / `test{}`）。"""
    if _known_prefix(token):
        return False
    pfx = _prefix_of(token).lower()
    if _flag_word(pfx):
        base = pfx.replace("flag", "").replace("ctf", "").replace("htb", "")
        return bool(base) and bool(_BAIT_AFFIX.match(base))
    return pfx in _BAIT_WORDS


def _prefix_confident(token: str) -> bool:
    return _known_prefix(token) or (
        _flag_word(_prefix_of(token).lower()) and not is_bait_prefix(token))


def _printable_inner(token: str) -> bool:
    inner = inner_of(token) or token
    return bool(inner) and all(0x20 <= ord(ch) <= 0x7e for ch in inner)


def _is_codeish(token: str) -> bool:
    """模板语法/代码片段判定（`{% if %}` / `${x}` / `@app.route{...}`）。"""
    inner = inner_of(token)
    pfx = _prefix_of(token)
    if _known_prefix(token):
        # 已知前缀下仍可能是代码：`flag{...}` 里全是 `=>` 之类
        if len(inner) <= 3 and pfx.lower().startswith(inner.lower()):
            return True
        return bool(_STRONG_CODEISH.search(inner))
    if pfx.rstrip(".-_").rsplit(".", 1)[-1].lower() in _PREFIX_KEYWORDS:
        return True
    if _MACRO_INNER_RX.match(inner):
        return True
    return bool(_CODEISH.search(inner))


def _clean_twin(token: str) -> str | None:
    """`xxflag{abc}` → `flag{abc}`：从 flag 词起始处截断的"孪生"候选。

    模型常把上下文粘进前缀（`theflag{...}` / `picoCTFflag{...}`），孪生候选
    参与判分可救回真 flag。

    **诱饵前缀不洗**（2026-10-19）：`fake_flag{deadbeef99}` 截出来的
    `flag{deadbeef99}` 是**凭空造出来的干净串**——题面/页面里写的是「这是假的」，
    洗掉诱饵词反而让它拿到 +3 前缀分变成可提交候选。原先 `confident_flags`
    只惩罚前缀诱饵、不惩罚其孪生，于是 `fake_flag{}` 能稳定绕过三道闸。
    孪生只应处理**中性上下文**（`theflag` / `picoCTFflag`），不处理带 bait 词的。
    """
    if _known_prefix(token) or is_bait_prefix(token):
        return None
    pfx = _prefix_of(token)
    onset = _flag_onset(pfx)
    if onset <= 0:
        return None
    clean = token[onset:]
    if clean == token or _is_codeish(clean) or not _printable_inner(clean):
        return None
    return clean


def natural_language_inner(token: str) -> bool:
    """内层是自然语言（≥4 个 CJK 字符，或 ≥3 个纯字母英文单词）→ 是题面占位符。

    `flag{this is the flag}` 这种示例文本不该被当答案。
    """
    core = inner_of(token) or token
    if len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", core)) >= 4:
        return True
    if " " in core:
        words = sum(1 for t in core.split() if t.isascii() and t.isalpha())
        return words >= 3
    return False


def is_bait_inner(token: str) -> bool:
    """内层本身就是诱饵词（`flag{example}` / `flag{test}` / `flag{xxx}`）。

    hxbai 原版只惩罚**前缀**诱饵（`fake_flag{}`），但题面示例更常见的形态是
    **前缀正常、内层是占位词**——`flag{example}` 前缀是 `flag` 完全可信，光看
    前缀必然漏判。本函数补上内层这一维。
    """
    core = (inner_of(token) or "").strip().lower()
    if not core:
        return False
    # 允许带分隔符的变体（`flag{fake_flag}` / `flag{test-flag}`）
    bare = re.sub(r"[\W_]+", "", core)
    if bare in _BAIT_WORDS:
        return True
    # 内层是「诱饵词 + flag 词」的组合（`fakeflag` / `notaflag` / `testctf`）
    stripped = bare.replace("flag", "").replace("ctf", "").replace("htb", "")
    if stripped and stripped != bare and stripped in _BAIT_WORDS:
        return True
    return bare in ("flag", "ctf", "htb", "answer", "result",
                    "yourflag", "here", "value")


def _inner_too_short(token: str) -> bool:
    """内层过短（<3 字符）——`flag{x}` / `flag{1}` 是手工占位，不是真答案。"""
    return len((inner_of(token) or "").strip()) < MIN_INNER_LEN



# ---------------- 抽取与打分 ----------------

def extract_candidates(text: str) -> set[str]:
    """从任意文本抽取 flag 候选（含孪生候选）；模板语法/代码片段已剔除。"""
    if not text:
        return set()
    out: set[str] = set()
    for m in _CAND_RX.finditer(text):
        # URL 编码里 `%7B...%7D` 不该被当 flag；紧邻 `%` 的丢弃
        if m.start() > 0 and text[m.start() - 1] == "%":
            continue
        t = m.group(1)
        if _is_codeish(t):
            continue
        out.add(t)
        twin = _clean_twin(t)
        if twin:
            out.add(twin)
    return out


def flagishness(token: str) -> float:
    """置信打分（可解释，便于日志/审计）。

    | 信号 | 分 |
    |---|---|
    | 前缀可信（已知赛事前缀 / 非诱饵的 flag 词前缀） | +3 |
    | 内层含数字 | +1 |
    | 内层形如 hex（`[0-9a-f-]{8,}`） | +2 |
    | 内层含 `_` 或 `-` | +0.5 |
    | 内层含空格 | -0.5 |
    | 前缀是诱饵（fake/example/test…） | -5 |
    | 内层是诱饵词（`flag{example}` / `flag{test}`） | -5 |
    | 内层过短（<3 字符，`flag{x}`） | -3 |
    | 前缀含 flag 词但不在开头（`theflag{}`） | -2.5 |
    | 内层无任何字母数字 | -5 |
    | 内层含 shell 元字符（每个） | -1 |
    | 内层同时含 `_` 与 `-` | -0.6 |

    阈值 `FLAGISH_FLOOR=2.0`：`flag{abc123}`=4，`example{abc123}`=-1（拒），
    `flag{example}`=-2（拒），`flag{x}`=0（拒），`{{config}}`=-5（拒）。
    """
    inner = inner_of(token)
    score = 0.0
    if _prefix_confident(token):
        score += 3
    if any(c.isdigit() for c in inner):
        score += 1
    if re.fullmatch(r"[0-9a-f-]{8,}", inner):
        score += 2
    if "_" in inner or "-" in inner:
        score += 0.5
    if " " in inner:
        score -= 0.5
    if is_bait_prefix(token):
        score -= 5
    elif not _known_prefix(token) and _flag_onset(_prefix_of(token)) > 0:
        score -= 2.5
    # 内层诱饵/过短（本模块相对 hxbai 的补强：题面示例多为「前缀正常+内层占位」）
    if is_bait_inner(token):
        score -= 5
    if _inner_too_short(token):
        score -= 3
    if inner and not any(c.isalnum() for c in inner):
        score -= 5
    _shell_meta = set("&?*|;<>$`'\" ")
    bad = _shell_meta & set(inner)
    if bad:
        score -= 1.0 * len(bad)
    if "_" in inner and "-" in inner:
        score -= 0.6
    return score


def is_confident_flag(token: str) -> bool:
    return flagishness(token) >= FLAGISH_FLOOR


def _class_key(token: str) -> tuple:
    """同形簇分类键：前缀是否可信 + 内层长度桶 + 内层字符形态。

    刷屏假阳性的特征：几十个**结构相同**的候选（同前缀、同长度量级、同字符形态）
    只是内层随机串不同——例如网页里每个 `<div>` 都匹配上 `xxx{a1b2}`。
    真 flag 在题内通常只出现 1~3 次，不会形成 ≥4 的同形簇。
    """
    inner = inner_of(token)
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", inner, re.I):
        shape = "uuid"
    elif re.fullmatch(r"[0-9a-fA-F]{8,}", inner):
        shape = "hex"
    elif inner.isdigit():
        shape = "num"
    elif re.fullmatch(r"[A-Za-z0-9_-]+", inner):
        shape = "alnum"
    else:
        shape = "other"
    return (_prefix_confident(token), len(inner) // 4, shape)


def confident_flags(candidates, *, need: int = 1) -> set[str]:
    """从候选集筛出「可提交」的 flag 集合。

    三道过滤：① 自然语言内层（题面占位符）丢；② 置信分 < 阈值丢；
    ③ 同形簇 ≥ SPRAY_MIN 的**非可信前缀**候选丢（刷屏假阳性）；最后按内层去重
    取最高分那条（同一 flag 可能带不同前缀同时命中）。

    ``need`` 只影响返回是否被截断（保留最高分 need 条）；不传则全给。
    """
    candidates = {c for c in (candidates or ())
                  if not natural_language_inner(c) and not is_bait_inner(c)}
    cands = list(candidates)
    cset = set(cands)
    # 孪生候选：`theflag{x}` 与 `flag{x}` 同时在场时，后者是前者的清洗版
    derived = {tw for c in cset if (tw := _clean_twin(c)) is not None and tw in cset}

    def _sprayable(c: str) -> bool:
        return (not _prefix_confident(c)) or (c in derived) or (_clean_twin(c) is not None)

    spray_clusters = Counter(_class_key(c) for c in cands if _sprayable(c))

    out: set[str] = set()
    for c in cands:
        if flagishness(c) < FLAGISH_FLOOR:
            continue
        if not _printable_inner(c):
            continue
        if _sprayable(c) and spray_clusters[_class_key(c)] >= SPRAY_MIN:
            continue
        out.add(c)
    # 同内层只留最高分（同分留更长的）
    best: dict[str, str] = {}
    for c in out:
        inner = inner_of(c) or c
        if inner not in best or (flagishness(c), -len(c)) > (flagishness(best[inner]), -len(best[inner])):
            best[inner] = c
    ranked = sorted(best.values(), key=lambda t: (-flagishness(t), t))
    if need and need > 0:
        return set(ranked[:need])
    return set(ranked)


# ---------------- 声明与回环验证 ----------------

def parse_flag_declarations(text: str) -> list[dict]:
    """解析模型显式声明行：

        submit_flag: <flag> | source: <原始观测字节> | transform: <变换>

    source/transform 可选；两者都在时才可做回环验证。
    """
    out: list[dict] = []
    for m in _DECL_LINE_RX.finditer(text or ""):
        parts = [p.strip() for p in m.group(1).split("|")]
        val = parts[0].strip()
        if not val:
            continue
        src = xf = ""
        for p in parts[1:]:
            low = p.lower()
            if low.startswith("source:"):
                src = p[len("source:"):].strip()
            elif low.startswith("transform:"):
                xf = p[len("transform:"):].strip()
        out.append({"value": val, "source": src, "transform": xf})
    return out


def roundtrip_verify(value: str, source: str, transform: str) -> bool | None:
    """验证「value 是由 source 经 transform 变换而来」。

    返回 True=回环成立（真解码），False=不成立（瞎猜/错解），None=无法判定
    （transform 未知或含多个变换名，不做断言）。

    口径：把 value **编码**回 transform 指定的形态，检查是否出现在 source 里
    （大小写不敏感）。真解码必然回环；随手打的字符串不会。
    """
    if not value or not source:
        return None
    toks = set(re.findall(r"[a-z0-9]+", (transform or "").lower()))
    fns = {f for k, f in _ROUNDTRIP_XFORMS.items() if k in toks}
    if len(fns) != 1:
        return None
    try:
        enc = next(iter(fns))(value)
    except Exception:  # noqa: BLE001 —— 编码失败按无法判定
        return None
    return enc.lower() in source.lower()


# ---------------- 跨题家族（防串题） ----------------

def family_key(flag: str) -> str:
    """flag 家族键：内层去非字母数字并小写（`flag{a-b_c}` 与 `flag{abc}` 同族）。"""
    inner = inner_of(flag) or flag
    return re.sub(r"[^0-9a-z]", "", (inner or "").casefold())


def neighbor_owner(flag: str, registry: dict, self_code: str = "") -> str | None:
    """在跨题注册表里找「这个 flag 属于别的题」的证据。

    ``registry`` 形如 ``{flag: owner_code}``（由调用方维护，一个项目一份）。
    判定：内层长度 ≥8 且互为子串 → 判为同族；owner 不是自己 → 返回 owner。
    长度下限 8 是防 `flag{1}` 这种短内层误配。
    """
    ci = inner_of(flag) or flag
    if len(ci) < 8:
        return None
    for rf, owner in (registry or {}).items():
        if owner in (None, "", self_code):
            continue
        ri = inner_of(rf) or rf
        if len(ri) >= 8 and (ci in ri or ri in ci):
            return str(owner)
    return None
