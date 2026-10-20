"""方向互斥（借鉴 hxbai `engine/intent_lease.py::WorkClaimTable`，2026-10-19）。

**要解决的问题**：并发派工时，两个 worker 常常在**同一个方向**上重复劳动——
一个在试 `robots.txt` 找备份，另一个也在试 `robots.txt` 找备份。它们互相不知道
对方存在，因为 LLM 生成子目标时各自独立措辞：

    子目标 A： "exploit the login form SQL injection"
    子目标 B： "test login form sql injection"

字面不同，**方向完全相同**。结果是并发度看着是 3，实际有效并发是 1。

hxbai 的解法：把子目标**归一成一个方向键**（去掉动作词、去重、排序 token），
同一方向键只允许一个持有者。第二个来领的人被拒绝，并被告知「谁在占着」。

**本项目的扩展**：

1. **中文方向键**（hxbai 只处理 ``[a-z0-9.]``，中文串会被整个丢掉，导致
   中文子目标全部归一成空键——空键互相冲突，反而把并发锁成 1）。这里把中文
   与 ASCII 分开切词，各自剥虚词，再过一层**同义词归一**（口令/密码/passwd
   → ``pass``）与**词形归一**（backups→backup）；
2. **可解释拒绝**：拒绝时返回持有者 + 剩余租期，LLM 拿到就能换方向，而不是
   收到一句「失败」继续重试同一件事；
3. **可注入提示词**：``render_table()`` 产出紧凑表格，让主控**看见**当前
   所有占用中的方向，从源头上避免生成重复子目标（比事后拒绝更省一轮）。

**刻意的取舍：精确匹配，不做模糊归一**。``"probe robots.txt for hidden
backup paths"`` 与 ``"check robots.txt to find backup files"`` 会得到两个
不同的键——它们确实是同一件事，但把「hidden path」与「file」合并需要语义
理解，而任何基于 token 重叠的近似匹配都会同时把「文件上传绕过 WAF」和
「文件上传漏洞」这类**真不同**的方向合并。**误锁死并发比重复劳动更贵**：
重复劳动只是浪费一个槽位，误锁死会让一个方向永远没人做。故本模块只做
确定性的字面归一，模糊判定留给主控的提示词（它看得见 ``render_table()``）。

**为什么是实例而非模块级单例**：本项目多会话并发，进程级表会让 A 会话的方向
占用影响 B 会话。表由调用方（主控实例）持有，随会话生灭。

**为什么不落黑板**：方向占用是**秒级易变状态**，不是知识。落库会让 30 秒前
的死锁一直留在图里，还得写清扫逻辑。TTL + 进程内表已经够用；真要跨进程，
那是另一套设计（需要心跳与租约续期协议），不该由本模块半吊子承担。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "direction_key",
    "ClaimResult",
    "ClaimTable",
    "DEFAULT_TTL_S",
    "ACTION_WORDS",
    "FILLER_WORDS",
]

DEFAULT_TTL_S = 30 * 60.0

# 动作词：它们描述「怎么干」，不描述「干什么」。剥掉才能让
# "exploit the login form" 与 "test login form" 归一成同一方向。
#
# **手法名不能进这张表**：inject/upload/download/bypass/deserialize 是
# **方向本身**（「上传点在哪」和「注入点在哪」是两道完全不同的活）。
# 把它们当动作词剥掉，会把所有 web 方向归一成同一个空壳键，锁死并发。
# 同理 ``force`` 要进表——``brute force`` 的 force 只是动作的一部分。
ACTION_WORDS: frozenset[str] = frozenset("""
exploit attack try attempt test check probe scan brute bruteforce crack pivot
enumerate investigate explore verify mine harvest dump read fetch get find search
use run execute query evade force
""".split())

# 填充词：不承载方向信息（"the login form on host A" → "login form"）
FILLER_WORDS: frozenset[str] = frozenset("""
the a an this that these those on at against for of to via using with from by
host hosts server servers service services box node machine target targets
and or then next please now also
""".split())

# 中文动作词/虚词：同样只剥「怎么干」和「废话」。
# 刻意保守——「注入」「上传」「反序列化」这类**手法名**必须保留，
# 剥掉它们会把 web 题所有方向归一成同一个键，锁死并发。
_CJK_NOISE: frozenset[str] = frozenset("""
尝试 测试 检查 探测 扫描 攻击 利用 爆破 枚举 排查 分析 挖掘 收集 查找 获取 读取
一下 一个 这个 那个 这些 那些 进行 继续 然后 接着 目标 主机 服务器 服务 机器
的 了 和 与 及 或 在 是 有 也 都 就 很 再 又 还 把 被 对 从 到 给 让 而 但 并 则 等
找 看 试 查 挖 拿 做 干 想 要 能 会
""".split())

# 词形/同义归一：同一个概念的中英不同说法必须落到同一个 token，
# 否则「爆破 admin 口令」与「尝试爆破 admin 的密码」判成两个方向，
# 两个 worker 就会各自去爆一遍 admin。**只收 CTF 高频等价词**，
# 不做泛化同义词（「漏洞」和「绕过」是不同手法，绝不能合并）。
_SYNONYMS: dict[str, str] = {
    # 口令类
    "口令": "pass", "密码": "pass", "passwd": "pass", "password": "pass",
    "pwd": "pass", "credential": "pass", "creds": "pass", "凭据": "pass",
    # 上传/下载
    "上传": "upload", "download": "download", "下载": "download",
    # 注入
    "注入": "inject", "injection": "inject", "sqli": "inject",
    # 反序列化
    "反序列化": "deserial", "deserialize": "deserial",
    "deserialization": "deserial",
    # 爆破
    "爆破": "brute", "bruteforce": "brute", "brute-force": "brute",
    # 绕过
    "绕过": "bypass", "waf": "waf",
    # 备份
    "备份": "backup",
    # 提权
    "提权": "privesc", "privilege": "privesc", "escalation": "privesc",
    # 目录/路径扫描
    "目录": "dir", "directory": "dir", "路径": "path",
}


def _stem(tok: str) -> str:
    """英文复数/常见后缀归一：backups→backup、files→file、paths→path。

    只处理长度 >3 且以 s 结尾的 token——``dns``/``sql``/``xss`` 这类
    本身就是缩写，剥 s 会变成 ``dn``/``sq``/``xs``，反而破坏互斥。
    """
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok

# 归一化后仍为空 → 无法判定方向，用这个哨兵（**不参与互斥**，
# 见 ClaimTable.claim 的 empty_key 分支：宁可放行也不能把无关工作锁死）
EMPTY_KEY = ""

_CJK = r"\u4e00-\u9fff"
_SPLIT_RE = re.compile(rf"[^a-z0-9.{_CJK}]+")
_CJK_RUN_RE = re.compile(rf"[{_CJK}]+")
_ASCII_RUN_RE = re.compile(r"[a-z0-9.]+")


def _cjk_tokens(run: str) -> list[str]:
    """中文段切词：剥噪声词 → 最长匹配同义词 → 残余片段切 2-gram。

    三层是为了同时解决三个问题：

    1. **噪声**：「尝试爆破 admin 的密码」里的「尝试/的」不承载方向；
    2. **同义**：「口令」与「密码」必须落到同一个 token，否则同一件事被判成
       两个方向（这是并发锁失效最常见的原因）；
    3. **残余**：「文件上传」这类组合词里的「文件」不剥不切，但也不该整段
       保留（「文件上传」与「文件下载」应共享「文件」）。

    为什么不直接 2-gram 全切：「上传」与「下载」会共享「载」这类无意义
    交集，反而把无关方向判成同一个。
    """
    spans: list[str] = []
    cur: list[str] = []
    i = 0
    max_len = max(len(w) for w in _CJK_NOISE)
    while i < len(run):
        hit = 0
        for n in range(min(max_len, len(run) - i), 0, -1):
            if run[i:i + n] in _CJK_NOISE:
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

    syn_max = max(len(w) for w in _SYNONYMS)
    out: list[str] = []
    for s in spans:
        residual: list[str] = []
        j = 0

        def _flush() -> None:
            r = "".join(residual)
            residual.clear()
            if not r:
                return
            if len(r) <= 2:
                out.append(r)
            else:
                out.extend(r[k:k + 2] for k in range(len(r) - 1))

        while j < len(s):
            matched = 0
            for n in range(min(syn_max, len(s) - j), 1, -1):
                w = s[j:j + n]
                if w in _SYNONYMS:
                    _flush()
                    out.append(_SYNONYMS[w])
                    matched = n
                    break
            if matched:
                j += matched
                continue
            residual.append(s[j])
            j += 1
        _flush()
    return out


def direction_key(text: str) -> str:
    """子目标/假设 → 归一方向键（同向不同措辞必须得到同一个键）。

    口径：小写 → 按「非字母数字点非中文」切段 → ASCII 段剥动作词/填充词、
    中文段剥噪声词并切 2-gram → 去重 → 排序 → 空格连接。

    排序是必须的：``"login sql injection"`` 与 ``"sql injection login"``
    必须同键，否则词序不同就漏判。

    无法抽出任何实义词时返回空串（调用方据此**放行**而非拒绝）。
    """
    s = (text or "").strip().lower()
    if not s:
        return EMPTY_KEY
    toks: set[str] = set()
    for chunk in _SPLIT_RE.split(s):
        if not chunk:
            continue
        for run in _CJK_RUN_RE.findall(chunk):
            toks.update(_cjk_tokens(run))
        for run in _ASCII_RUN_RE.findall(chunk):
            if run in ACTION_WORDS or run in FILLER_WORDS:
                continue
            toks.add(_SYNONYMS.get(run) or _stem(run))
    return " ".join(sorted(t for t in toks if t))


@dataclass(frozen=True)
class ClaimResult:
    """一次方向认领的结果。

    ``granted=False`` 时 ``holder``/``expires_in`` 是**给人看的关键信息**：
    主控拿到就知道「这个方向 B 在做，还剩 12 分钟」，据此换方向，
    而不是把同一件事重试一遍。
    """

    granted: bool
    key: str
    holder: str | None = None
    expires_in: float = 0.0
    reason: str = ""

    def describe(self, label: str = "") -> str:
        """人话描述。``label`` 给「给人看的名字」——默认回落归一键。

        归一键（`direction_key`）是 2-gram 拼接，中文会切出跨词碎片，直接展示
        会让主控认不出那是自己刚派过的方向。调用方能给可读原文时务必传入。
        """
        who = label or self.key
        if self.granted:
            return f"已认领方向「{who}」：{self.reason}"
        return f"方向「{who}」被 {self.holder} 占用：{self.reason}"


@dataclass
class _Claim:
    holder: str
    claimed_at: float
    expires_at: float
    subgoal: str = ""


class ClaimTable:
    """方向占用表（线程安全，TTL 自动回收）。

    典型用法（主控侧）::

        table = ClaimTable()
        r = table.claim("probe /robots.txt for backups", "worker-1")
        if not r.granted:
            # 换方向，别重试
            ...
        table.release("probe /robots.txt for backups", "worker-1")

    同一持有者重复认领同一方向 = **续租**（返回 granted），这是心跳场景。
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S,
                 clock: Callable[[], float] | None = None):
        self._claims: dict[str, _Claim] = {}
        self._lock = threading.Lock()
        self._ttl = float(ttl_s)
        self._clock = clock or time.time
        self._conflicts: list[dict] = []
        self._grants = 0
        self._renewals = 0

    # ---- 核心 ----

    def claim(self, subgoal: str, holder: str) -> ClaimResult:
        """认领方向。同一方向已有他人持有 → 拒绝并说明持有者。"""
        key = direction_key(subgoal)
        if not key:
            # 抽不出方向 → 放行。把无关工作锁死比重复劳动更糟。
            return ClaimResult(True, EMPTY_KEY,
                               reason="子目标无实义词，不参与互斥（放行）")
        now = self._clock()
        with self._lock:
            self._evict(now)
            cur = self._claims.get(key)
            if cur is not None:
                if cur.holder == holder:
                    cur.expires_at = now + self._ttl
                    self._renewals += 1
                    return ClaimResult(True, key, holder=holder,
                                       expires_in=self._ttl,
                                       reason="同一持有者续租")
                self._conflicts.append({
                    "key": key, "requester": holder, "holder": cur.holder,
                    "at": now, "subgoal": (subgoal or "")[:200],
                })
                if len(self._conflicts) > 200:
                    del self._conflicts[:100]
                return ClaimResult(
                    False, key, holder=cur.holder,
                    expires_in=max(0.0, cur.expires_at - now),
                    reason=f"该方向已由 {cur.holder} 在做，换一个方向或等它释放")
            self._claims[key] = _Claim(holder=holder, claimed_at=now,
                                       expires_at=now + self._ttl,
                                       subgoal=(subgoal or "")[:200])
            self._grants += 1
            return ClaimResult(True, key, holder=holder, expires_in=self._ttl,
                               reason="首次认领")

    def release(self, subgoal: str, holder: str) -> bool:
        """释放方向（只有持有者能释放；别人的释放请求静默失败）。"""
        key = direction_key(subgoal)
        if not key:
            return False
        with self._lock:
            cur = self._claims.get(key)
            if cur is not None and cur.holder == holder:
                del self._claims[key]
                return True
            return False

    def release_holder(self, holder: str) -> int:
        """释放某持有者的**全部**方向（worker 结束时调用，防僵尸占用）。"""
        with self._lock:
            keys = [k for k, v in self._claims.items() if v.holder == holder]
            for k in keys:
                del self._claims[k]
            return len(keys)

    # ---- 查询 ----

    def active(self) -> dict[str, dict]:
        """当前占用中的方向（key → {holder, expires_in, subgoal}）。"""
        with self._lock:
            now = self._clock()
            self._evict(now)
            return {k: {"holder": v.holder,
                        "expires_in": max(0.0, v.expires_at - now),
                        "subgoal": v.subgoal}
                    for k, v in self._claims.items()}

    def is_held(self, subgoal: str) -> ClaimResult | None:
        """只查不领：这个方向有人在做吗？（用于派工前预检）"""
        key = direction_key(subgoal)
        if not key:
            return None
        with self._lock:
            now = self._clock()
            self._evict(now)
            cur = self._claims.get(key)
            if cur is None:
                return None
            return ClaimResult(False, key, holder=cur.holder,
                               expires_in=max(0.0, cur.expires_at - now),
                               reason="已被占用")

    def conflicts(self, limit: int = 20) -> list[dict]:
        """最近的拒绝记录（复盘「浪费了多少次重复派工」）。"""
        with self._lock:
            return [dict(c) for c in self._conflicts[-limit:]]

    def stats(self) -> dict:
        with self._lock:
            self._evict(self._clock())
            return {"active": len(self._claims),
                    "grants": self._grants,
                    "renewals": self._renewals,
                    "conflicts": len(self._conflicts),
                    "directions": sorted(self._claims.keys())}

    def render_table(self, max_rows: int = 12) -> str:
        """紧凑表格，注入提示词让主控**看见**占用情况。

        返回空串表示没有占用——调用方应整段省略，别塞一句「（无）」占 token。
        """
        rows = sorted(self.active().items(),
                      key=lambda kv: kv[1]["expires_in"])
        if not rows:
            return ""
        lines = ["| 进行中的方向 | 谁在做 | 剩余 |", "|---|---|---|"]
        for k, v in rows[:max_rows]:
            # 优先展示认领时的方向原文；键只是回落（中文键是 2-gram 碎片，
            # 直接展示等于让主控看见一串它认不出的词）。
            label = (v.get("subgoal") or "").strip() or k
            lines.append(f"| {label[:60]} | {v['holder']} | "
                         f"{v['expires_in'] / 60:.0f}min |")
        if len(rows) > max_rows:
            lines.append(f"| …另有 {len(rows) - max_rows} 个方向 | | |")
        return "\n".join(lines)

    # ---- 内部 ----

    def _evict(self, now: float) -> None:
        """清过期占用（调用方须持锁）。"""
        dead = [k for k, v in self._claims.items() if v.expires_at <= now]
        for k in dead:
            del self._claims[k]
