"""上下文降级（借鉴 hxbai `engine/fold.py::folded_projection`，2026-10-19）。

**要解决的问题**：CTF 一道题跑两小时，黑板会攒下几百条侦察笔记：每个端口
一条、每个目录一条、每次 `curl` 的响应头一条。全塞进提示词有三个后果：

1. **超窗**：模型上下文被打满，最新最关键的那条（刚拿到的 foothold）反而被挤掉；
2. **淹没**：几百条「端口开放」里混着一条「拿到 admin 口令」，模型找不到；
3. **烧钱**：每轮都重投影一次，token 成本随会话时长线性涨。

hxbai 的解法是**按预算降级**：给定 ``token_budget``，逐级砍——先砍「最近
N 条」，再砍「显著条目数」，最后把单条事实截断——直到投影塞进预算。关键是
**砍的顺序不能乱**：先砍噪声、后砍显著项、最后才砍长度；**必保段永远不砍**。

**本项目的扩展**：

1. **四级降级阶梯**（hxbai 是两级循环）：FULL → COMPACT → SALIENT →
   HEADLINE，每级有明确语义与可观测的 ``tier`` 字段，事后能看「这轮降到了
   第几级」——降级本身是信号（说明会话已经很长了）；
2. **必保段**（``mandatory``）：flag 台账与自证结论**永不降级**。这是本项目
   的硬要求——``challenge_flags`` 是解题进度的唯一真值，把它砍掉等于让主控
   忘记自己已经拿了几分；
3. **中文 token 估算**：hxbai 用 ``len//4``，那是英文口径。中文一字约一
   token，按 ``len//4`` 估会把中文投影低估 3-4 倍，预算形同虚设；
4. **近重复抑制**：同一件事被 worker 用不同措辞记了三遍（「8080 开放」/
   「port 8080 open」/「8080/tcp open http」）只留一条，靠 token 集合 Jaccard。

**为什么是纯函数**：``fold()`` 只吃「段落列表 + 预算」，不查库、不读时钟。
这样同一份黑板快照永远得到同一份投影（可回放、可单测），也便于先离线调参。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "Section",
    "Projection",
    "estimate_tokens",
    "is_salient",
    "near_dup",
    "fold",
    "render",
    "DEFAULT_BUDGET",
    "TIER_FULL",
    "TIER_COMPACT",
    "TIER_SALIENT",
    "TIER_HEADLINE",
    "TIER_NAMES",
    "MIN_ITEM_CHARS",
]

# 默认预算：hxbai 的经验值。够放「一屏侦察结论 + 全部 flag 台账」。
DEFAULT_BUDGET = 2800

# 四级降级（数值越大降得越狠）
TIER_FULL = 0
TIER_COMPACT = 1
TIER_SALIENT = 2
TIER_HEADLINE = 3
TIER_NAMES = {
    TIER_FULL: "full",
    TIER_COMPACT: "compact",
    TIER_SALIENT: "salient",
    TIER_HEADLINE: "headline",
}

# 单条事实的绝对下限：再短就没有信息量了（截到 40 字符以下等于只剩「端口…」）
MIN_ITEM_CHARS = 40

_CJK_RX = re.compile(r"[\u4e00-\u9fff]")

# 显著度：出现这些内容的事实**不能随便砍**——它们是解题的支点。
# 与 hxbai 的 _SALIENT_FACT 同构，按本项目 CTF 场景重写：
# 凭据 / flag / 端口服务 / 路径 / 版本 / 哈希 / 编码载荷 / 反序列化线索。
_SALIENT_RX = re.compile(
    r"cred|passw|passwd|secret|api[_-]?key|\btoken\b|private[_ ]key|"
    r"foothold|webshell|web ?shell|reverse ?shell|shell\b|uid=0|"
    r"\badmin\b|\broot\b|"
    r"flag\{|[A-Za-z][A-Za-z0-9_.\-]{1,30}\{[^{}\r\n]{2,200}\}|"
    r"\bv?\d+\.\d+\.\d+\b|"
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b|"
    r"(?<![\w-])/[A-Za-z0-9_./-]{2,}|"
    r":\d{2,5}\b|/tcp\b|\bopen\b|"
    r"eyJ[A-Za-z0-9_-]{6,}|[A-Za-z0-9+/]{40,}={0,2}|\b[0-9a-f]{32,}\b|"
    r"0x[0-9a-fA-F]{3,}|\.(?:text|data|bss|rodata|plt|got)\b|opcode|disasm|bytecode|"
    r"\bxor\b|\brc4\b|\baes\b|\bdes\b|\brsa\b|keystream|cipher|plaintext|ciphertext|"
    r"decrypt|decode|nonce|pickle|unserialize|yaml\.load|"
    r"凭据|密码|口令|密钥|令牌|提权|反序列化|注入|上传|绕过|shell|flag|"
    r"\b[1-5]\d\d\b",
    re.I)


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    **不能照抄 hxbai 的 ``len//4``**：那是英文口径。中文一字约一 token，
    按 ``len//4`` 估会把中文投影低估 3-4 倍，预算形同虚设——降级永不触发，
    超窗照样发生。故中文按 1 字 1 token、ASCII 按 4 字符 1 token 分开算。
    """
    s = text or ""
    if not s:
        return 0
    cjk = len(_CJK_RX.findall(s))
    other = len(s) - cjk
    return cjk + other // 4 + 1


def is_salient(text: str) -> bool:
    """这条事实值不值得在降级时保留。"""
    return bool(_SALIENT_RX.search(text or ""))


# 强信号：命中即「这条不能丢」——它们是**解题支点**（拿到凭据 / 拿到 flag /
# 拿到执行），而路径、端口号只是**素材**。降级时素材可以让位，支点不行。
_STRONG_RX = re.compile(
    r"cred|passw|passwd|secret|api[_-]?key|\btoken\b|private[_ ]key|"
    r"foothold|webshell|web ?shell|reverse ?shell|uid=0|"
    r"flag\{|[A-Za-z][A-Za-z0-9_.\-]{1,30}\{[^{}\r\n]{2,200}\}|"
    r"eyJ[A-Za-z0-9_-]{6,}|"
    r"凭据|密码|口令|密钥|令牌|提权|反序列化|shell",
    re.I)

# **产物路径也是支点**（ctf-inspect-provenance，2026-10-20）。
#
# 实测：`fold.salience_score('flag.zip 落在 /workspace/scratch/att-abc/flag.zip') = 1`
# ——只算「一般显著」，降级到 `salient` 级就被四十条目录扫描挤掉了。后果是
# 主控知道「拼出了 flag.zip」却**不知道它在哪**，`unzip -l` 独立复核在物理上
# 做不到（用户复盘里「摘要说拼出 flag.zip，没说落在哪」）。
#
# 与「凭据 = 2」同源：产物路径是**可操作的事实**（照它就能复核），不是过程
# 描述。只认工作区路径（`/workspace/…`）与 scratch/work 前缀——裸相对文件名
# 不算，否则「a.py」这种泛指也会被提权，强信号就贬值了。
_ARTIFACT_PATH_RX = re.compile(
    r"/workspace/[^\s'\"`）)]+|"
    r"(?<![\w/])(?:scratch|work)/[^\s'\"`）)]+",
    re.I)


def salience_score(text: str) -> int:
    """显著度打分（0=普通素材，1=一般显著，2=强信号）。

    为什么需要分级：``/dir17`` 这类路径也命中「显著」，但一条路径远不如
    「拿到 admin:Passw0rd!」重要。只做二元判断时，四十条目录扫描会把唯一
    一条凭据挤出窗口——降级就白降了。

    **产物路径按强信号处理**（`_ARTIFACT_PATH_RX`）：`/workspace/…` 或
    `scratch/`/`work/` 前缀的路径是复核的钥匙，与凭据同级。
    """
    t = text or ""
    if not t:
        return 0
    if _STRONG_RX.search(t) or _ARTIFACT_PATH_RX.search(t):
        return 2
    if _SALIENT_RX.search(t):
        return 1
    return 0


def near_dup(a: str, b: str, thresh: float = 0.72) -> bool:
    """近似重复：token 集合 Jaccard ≥ thresh。

    **只用于去重，不用于判等**。阈值 0.72 是 hxbai 的经验值，够激进到合并
    「端口 8080/tcp open http」/「端口 8080/tcp open http 服务」这类追加式
    重述，又保守到不会把「8080 开放」和「8443 开放」合并（共享「开放」一个
    词，Jaccard 约 0.33）。

    **中英混合重述不指望能合并**（「端口 8080/tcp open」与「port 8080 open
    http」的 Jaccard 只有 0.3-0.4）：要合并它们需要语义理解，而任何放宽到
    那个程度的阈值都会顺手把「8080 开放」和「8443 开放」也合并。worker 通常
    用同一种语言记笔记，故这条限制在实践中不构成问题。
    """
    ta = set((a or "").lower().split())
    tb = set((b or "").lower().split())
    if not ta or not tb:
        return False
    union = len(ta | tb)
    return union > 0 and len(ta & tb) / union >= thresh


@dataclass
class Section:
    """投影中的一段（对应提示词里的一个 ``##`` 小节）。

    ``priority`` 越小越重要（同预算下先保它）；``mandatory=True`` 的段
    **任何降级级别下都不会被整段砍掉**，也不会被砍到零条——这是 flag 台账
    与自证结论的保障。
    """

    key: str
    title: str
    items: list[str] = field(default_factory=list)
    priority: int = 50
    mandatory: bool = False
    # 该段的引导语（一句话纪律），降级时保留——它比条目更省 token 且更重要
    note: str = ""

    def cost(self, items: list[str] | None = None) -> int:
        body = items if items is not None else self.items
        return estimate_tokens(self.title) + estimate_tokens(self.note) + sum(
            estimate_tokens(x) for x in body)


@dataclass
class Projection:
    """降级后的投影结果。"""

    sections: list[Section]
    tier: int = TIER_FULL
    budget: int = DEFAULT_BUDGET
    tokens: int = 0
    # 被砍掉/截断的说明（给日志与前端看，别静默丢数据）
    dropped: list[str] = field(default_factory=list)

    @property
    def tier_name(self) -> str:
        return TIER_NAMES.get(self.tier, "?")

    @property
    def over_budget(self) -> bool:
        return self.tokens > self.budget

    def describe(self) -> str:
        """一行摘要（进日志）：`tier=compact 2.4k/2.8k 段=5 砍=3`。"""
        return (f"tier={self.tier_name} {self.tokens / 1000:.1f}k/"
                f"{self.budget / 1000:.1f}k 段={len(self.sections)} "
                f"砍={len(self.dropped)}")


def _dedupe(items: list[str]) -> list[str]:
    """保序去重：先按完全相同，再按近似重复。"""
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        t = (it or "").strip()
        if not t or t in seen:
            continue
        if any(near_dup(t, k) for k in out):
            continue
        seen.add(t)
        out.append(t)
    return out


def _recent_plus_salient(items: list[str], keep: int) -> list[str]:
    """取最近 ``keep//2`` 条 + 强信号条目 + 其余显著条目补齐到 ``keep``。

    **``items`` 的约定顺序是「新 → 旧」**（调用方按 ``updated_ts DESC`` 传）。

    三级填充是有意为之：

    1. **最近一半**：保住时序感（「刚才试过什么」），否则模型重复劳动；
    2. **强信号优先**：拿到凭据/flag/执行这类支点**永远**在窗口里，
       哪怕它是两小时前的——这是「不淹没关键结论」的核心；
    3. **剩余名额**再给一般显著条目，最后才给普通素材。
    """
    if keep <= 0:
        return []
    head = items[:max(1, keep // 2)]
    out = list(head)
    for score in (2, 1, 0):
        for it in items:
            if len(out) >= keep:
                return out
            if it in out or salience_score(it) != score:
                continue
            out.append(it)
    return out


def _build(sections: list[Section], tier: int,
           keep: int, item_cap: int) -> list[Section]:
    """按指定降级级别构造投影（不判预算，由 fold() 循环调用）。"""
    out: list[Section] = []
    for s in sorted(sections, key=lambda x: (x.priority, x.key)):
        items = _dedupe(s.items)
        if tier == TIER_FULL:
            body = [x[:item_cap] for x in items]
        elif tier == TIER_COMPACT:
            body = [x[:item_cap] for x in _recent_plus_salient(items, keep)]
        elif tier == TIER_SALIENT:
            # 只留显著条目，按显著度降序（强信号在前），同分保持原顺序。
            # 名额取 keep 的一半：SALIENT 的语义是「**明显比 COMPACT 更省**」，
            # 同名额会让两级几乎等价（实测 107 token vs 107 token），降级阶梯
            # 退化成「compact 直接跳 headline」，中间的 SALIENT 白占一级。
            ranked = sorted(enumerate(items),
                            key=lambda p: (-salience_score(p[1]), p[0]))
            picked = [t for _, t in ranked if salience_score(t) > 0]
            body = [x[:item_cap] for x in (picked or items)[:max(2, keep // 2)]]
        else:  # TIER_HEADLINE
            # 每段只留标题 + **强信号条目**（至多 2 条）。
            #
            # 为什么非必保段也给条目：HEADLINE 若整段砍空，「凭据 admin:Passw0rd!」
            # 这种支点会随四十条目录扫描一起消失——模型下一轮会重新去扫目录，
            # 反而更费 token。强信号条目（凭据/flag/执行）**任何级别都不许丢**。
            picked = [x for x in items if salience_score(x) == 2]
            cap_n = max(2, keep // 4) if s.mandatory else 2
            body = [x[:item_cap] for x in picked[:cap_n]]
            if s.mandatory and not body:
                body = [x[:item_cap] for x in items[:2]]
        if s.mandatory and not body:
            body = [x[:item_cap] for x in (items[:2] or [])]
        if body or s.mandatory:
            out.append(Section(key=s.key, title=s.title, items=body,
                               priority=s.priority, mandatory=s.mandatory,
                               note=s.note))
    return out


def fold(sections: list[Section], budget: int = DEFAULT_BUDGET,
         *, keep: int = 12, item_cap: int = 300) -> Projection:
    """把段落列表降到预算内，返回带降级级别与丢弃说明的投影。

    阶梯：FULL → COMPACT → SALIENT → HEADLINE → 截断长度 → 放弃。
    **必保段全程不砍**；真到 HEADLINE 还超，就把条目长度对半砍到
    ``MIN_ITEM_CHARS``，仍超则接受超预算但如实标注（``over_budget``）——
    静默丢 flag 台账比超一点预算危险得多。
    """
    total = sum(s.cost() for s in sections)
    if total <= budget:
        built = _build(sections, TIER_FULL, keep, item_cap)
        return Projection(sections=built, tier=TIER_FULL, budget=budget,
                          tokens=sum(s.cost() for s in built))

    for tier in (TIER_COMPACT, TIER_SALIENT, TIER_HEADLINE):
        built = _build(sections, tier, keep, item_cap)
        tokens = sum(s.cost() for s in built)
        if tokens <= budget:
            dropped = _dropped_notes(sections, built)
            return Projection(sections=built, tier=tier, budget=budget,
                              tokens=tokens, dropped=dropped)

    # HEADLINE 仍超 → 砍条目长度（只砍非必保段，必保段保持可读）
    cap = item_cap
    built = _build(sections, TIER_HEADLINE, keep, cap)
    while sum(s.cost() for s in built) > budget and cap > MIN_ITEM_CHARS:
        cap = max(MIN_ITEM_CHARS, cap // 2)
        built = _build(sections, TIER_HEADLINE, keep, cap)
    tokens = sum(s.cost() for s in built)
    return Projection(sections=built, tier=TIER_HEADLINE, budget=budget,
                      tokens=tokens, dropped=_dropped_notes(sections, built))


def _dropped_notes(orig: list[Section], built: list[Section]) -> list[str]:
    """列出「哪段砍了多少条」——降级必须可解释，不能静默丢数据。"""
    by_key = {s.key: len(s.items) for s in built}
    out: list[str] = []
    for s in orig:
        n = len(_dedupe(s.items))
        m = by_key.get(s.key, 0)
        if m < n:
            out.append(f"{s.title}：{n}→{m} 条")
    return out


def render(proj: Projection, *, show_tier: bool = True) -> str:
    """投影 → 提示词文本。

    没有内容的段整段省略（不输出「（无）」占 token）；降级信息只在
    真降过级时提示一句，让模型知道「你现在看到的已经是压缩视图」。
    """
    parts: list[str] = []
    for s in proj.sections:
        if not s.items and not s.mandatory:
            continue
        block = [f"## {s.title}"]
        if s.note:
            block.append(s.note)
        block.extend(f"- {x}" for x in s.items)
        if not s.items and s.mandatory:
            block.append("- （暂无）")
        parts.append("\n".join(block))
    body = "\n\n".join(parts)
    if show_tier and proj.tier != TIER_FULL:
        tail = f"\n\n（上下文已压缩到 {proj.tier_name} 级，{proj.tokens}/{proj.budget} token）"
        if proj.dropped:
            tail += "；省略：" + "；".join(proj.dropped[:6])
        body += tail
    return body
