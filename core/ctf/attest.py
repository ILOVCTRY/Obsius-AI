"""目标机自证（借鉴 hxbai `engine/verify.py::make_challenge`，2026-10-19）。

**要解决的问题**：CTF 里「我拿到命令执行了」这句话几乎不可证伪。worker 说
「SSTI 打通了，`id` 返回 `uid=0`」——这可能是真的，也可能是它把题面的
样例输出抄了一遍，或者它**在自己机器上**跑 `id` 看到 root。主控据此投入
三十分钟去「提权拿 flag」，最后发现压根没有 shell。

hxbai 的解法是**让目标机自己算一道题**：

1. 主控生成随机 nonce（32 hex），期望值 = ``sha256(nonce)[:16]``；
2. 要求 worker **通过那个所谓的执行原语**，让**目标机**计算并返回摘要；
3. 判据不是「模型说算出来了」，而是「摘要出现在**工具回执**里」。

因为 nonce 是随机的，模型无法预知；因为判据要求摘要落在**回执**而非模型
自述里，编造无法通过；因为要求摘要由**目标**算，本地跑 `sha256sum` 得到
同一个值也说明不了问题——所以还要看那条命令**是否经过目标**（``curl`` /
webshell 参数 / 模板注入标记等载体）。

**本项目的扩展**：

1. **四种拒绝形状分别可解释**（hxbai 只返回 ``confirmed``/``inconclusive``）：
   ``fabricated_in_assertion``（摘要只在模型自述里）/ ``echo_not_execution``
   （回执里只有 nonce 原文，说明只是反射）/ ``no_target_vector``（回执有摘要
   但命令没有目标载体，很可能是本地算的）/ ``wrong_digest``（算了但算错）；
2. **不硬判「本地算的」**：``no_target_vector`` 是 **inconclusive 而非 reject**。
   交互式 shell 里跑 `sha256sum` 时命令行确实看不到载体，硬判会误杀真战果；
   由主控看理由自行决定要不要补一次带载体的复证。
3. **纯函数判定**：``scan()`` 只吃「回执文本 + 自述文本」，不碰网络、不读时钟。
   这样同一份事件流永远得到同一个判定（可回放、可单测）。

**与 ``flagjudge`` 的分工**：``flagjudge`` 判「这个字符串是不是 flag」；
本模块判「这个执行原语是不是真的」。两者都不做动作，只产判定。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field

__all__ = [
    "Challenge",
    "AttestResult",
    "make_challenge",
    "render_challenge",
    "scan",
    "ATTEST_SENTINEL",
    "NONCE_BYTES",
    "DIGEST_HEX_LEN",
    "TARGET_VECTORS",
    "CODE_CONFIRMED",
    "CODE_FABRICATED",
    "CODE_ECHO_ONLY",
    "CODE_NO_VECTOR",
    "CODE_WRONG_DIGEST",
    "CODE_NO_EVIDENCE",
]

# 模型要在最后一行回填的哨兵（沿用 hxbai 的写法，便于两边日志对照）
ATTEST_SENTINEL = "verify_output:"

# nonce 长度：16 字节 = 32 hex。够长到不可猜，够短到能塞进 URL/命令行。
NONCE_BYTES = 16
# 摘要截断长度：16 hex = 64 bit。碰撞概率可忽略，且短到不会被模型「凑巧」写对。
DIGEST_HEX_LEN = 16

# 判定码
CODE_CONFIRMED = "confirmed"
CODE_FABRICATED = "fabricated_in_assertion"
CODE_ECHO_ONLY = "echo_not_execution"
CODE_NO_VECTOR = "no_target_vector"
CODE_WRONG_DIGEST = "wrong_digest"
CODE_NO_EVIDENCE = "no_evidence"

# 「这条命令经过目标」的载体特征。
#
# 这是**启发式**，不是证明：它拦得住「本地跑 sha256sum 冒充」这种最常见的
# 偷懒，拦不住精心构造的假回执（那需要签名，超出本模块范围）。故命中它
# 只算「没发现破绽」，不命中算「无法判定」而非「判定为假」。
TARGET_VECTORS: tuple[re.Pattern[str], ...] = tuple(re.compile(p, re.I) for p in (
    r"\bcurl\b", r"\bwget\b", r"https?://",
    r"\bnc\b", r"\bncat\b", r"\bsocat\b", r"/dev/tcp/",
    # webshell / 命令注入参数
    r"[?&](?:cmd|exec|command|shell|ping|run|system)=",
    # 模板注入
    r"\{\{", r"\$\{", r"%\{", r"<%=",
    # 语言层执行
    r"\bos\.system\b", r"\bsubprocess\b", r"\b__import__\b",
    r"\bshell_exec\b", r"\bpassthru\b", r"\bsystem\s*\(",
    r"Runtime\.getRuntime", r"ProcessBuilder",
    # 远程会话
    r"\bssh\b", r"\bsmbclient\b", r"\bwinrm\b", r"\bpsexec\b",
    r"powershell\s+-[ec]", r"\bmshta\b", r"\bcertutil\b",
))

# 哨兵后的值：模型常把目标返回的字节直接贴在哨兵后面
# （``verify_output: 6ca13d52…``）。取到行尾更稳，但值里不能有空格。
_SENTINEL_RX = re.compile(re.escape(ATTEST_SENTINEL) + r"\s*(\S+)", re.I)


@dataclass(frozen=True)
class Challenge:
    """一道「让目标机自证」的题。

    ``command`` 是给 worker 抄的**可直接执行**的命令（POSIX 口径）；
    ``expected`` 只进主控侧判定，**绝不能**出现在给 worker 的提示里——
    否则模型直接把期望值抄进自述就能骗过「自述侧」检查。
    """

    nonce: str
    expected: str
    command: str

    def describe(self) -> str:
        """一行摘要（进日志/事件）：不泄 expected。"""
        return f"nonce={self.nonce[:8]}… 期望={self.expected[:4]}…"


@dataclass(frozen=True)
class AttestResult:
    """自证判定的结果。

    ``confirmed=True`` 才表示「目标机真的执行了」；``code`` 是稳定机器标识，
    ``reason`` 是给人看的一句话（进日志/前端/复盘）。
    """

    confirmed: bool
    code: str
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def inconclusive(self) -> bool:
        """既没证成也没证伪——主控可决定补一次带载体的复证。"""
        return not self.confirmed and self.code in (CODE_NO_VECTOR, CODE_NO_EVIDENCE)


def make_challenge(*, nonce: str | None = None) -> Challenge:
    """生成一道自证题。

    ``nonce`` 可注入（测试用）；生产一律走 ``secrets.token_hex``——用
    ``random`` 会因可预测而让「预知期望值」重新成为可能。
    """
    n = (nonce or secrets.token_hex(NONCE_BYTES)).strip().lower()
    expected = hashlib.sha256(n.encode()).hexdigest()[:DIGEST_HEX_LEN]
    # 三种目标都可能没有 sha256sum（busybox/精简镜像），故给一条 shell 内建兜底
    command = (f"printf %s {n} | sha256sum | cut -c1-{DIGEST_HEX_LEN}")
    return Challenge(nonce=n, expected=expected, command=command)


def render_challenge(ch: Challenge, primitive: str = "", recipe: str = "") -> str:
    """生成派给 worker 的自证提示（**不含 expected**）。

    刻意写得像 hxbai 的原版：明确「不许在自己机器上算」「目标不返回就是
    正确结果」——后一句是关键，否则模型倾向于「必须给出一个答案」而编造。
    """
    prim = (primitive or "").strip()[:2000]
    prim_block = ("\n".join("  | " + ln for ln in prim.splitlines())
                  if prim else "  | （未提供，按你已建立的执行原语自行选择）")
    parts = [
        "【自证任务】你声称已经拿到目标机上的命令执行。现在**让目标机自己算一道题**，"
        "以此证明那个原语真的在目标上执行。",
        "",
        "已建立的原语（据称可在目标上执行命令）：",
        prim_block,
    ]
    r = (recipe or "").strip()[-2500:]
    if r:
        parts += [
            "",
            "建立该原语的前置步骤（多步/有状态的利用可能需要先复现，"
            "例如先投毒再触发，之后这道题才跑得通）：",
            "\n".join("  | " + ln for ln in r.splitlines()),
        ]
    parts += [
        "",
        f"题目：让**目标机**计算字符串 {ch.nonce} 的 SHA-256，取前 "
        f"{DIGEST_HEX_LEN} 个十六进制字符。",
        f"参考命令（通过你的原语在目标上执行）：{ch.command}",
        "",
        "铁律：",
        "- 只能通过那个原语让**目标**执行；**不许**在你自己的机器上算这个哈希，"
        "不许猜，不许编。",
        "- 如果原语其实没有在目标上执行（比如只是把你的输入反射回来），"
        "那么目标响应里**不会**有正确答案——**这是正确且有用的结果**，照实报告。",
        "- 缺一个答案是有效的否定结论；编一个答案是零价值。",
        "",
        f"最后单独一行给出目标返回的原文：{ATTEST_SENTINEL} <目标返回的字节>",
    ]
    return "\n".join(parts)


def _has_vector(text: str) -> bool:
    return any(p.search(text or "") for p in TARGET_VECTORS)


def _norm(text: str) -> str:
    return (text or "").lower()


def scan(ch: Challenge, observations: list[str] | None = None,
         assertions: list[str] | None = None,
         *, require_target_vector: bool = True) -> AttestResult:
    """判定自证是否成立。

    - ``observations``：**工具回执**原文（``run_cmd`` 输出、HTTP 响应体等）——
      这是唯一算数的证据源；
    - ``assertions``：模型自己的话——**只用来识别编造形状，绝不用来证成**；
    - ``require_target_vector``：回执里必须能看到目标载体（curl/webshell 参数/
      模板标记等）。关掉它的唯一理由是调用方已知这是交互式远程会话（命令行
      里看不到载体）。

    判定顺序即优先级：**证成 > 编造 > 反射 > 算错 > 无证据**。
    """
    obs = [o for o in (observations or []) if o]
    asrt = [a for a in (assertions or []) if a]
    exp = ch.expected.lower()
    obs_join = "\n".join(obs)
    asrt_join = "\n".join(asrt)

    # ① 回执里出现期望摘要 → 目标真的算了。再看有没有目标载体。
    if exp in _norm(obs_join):
        if not require_target_vector or _has_vector(obs_join):
            return AttestResult(
                True, CODE_CONFIRMED,
                f"目标机自证成立（回执含期望摘要 {exp[:4]}…）",
                {"expected": exp, "vector": _has_vector(obs_join)})
        return AttestResult(
            False, CODE_NO_VECTOR,
            "回执里有正确摘要，但命令看不到目标载体（可能是本地算的）；"
            "建议用带 curl/webshell 参数的方式复证一次",
            {"expected": exp, "vector": False})

    # ② 摘要只在模型自述里 → 典型编造形状（回执里根本没有）
    if exp in _norm(asrt_join):
        return AttestResult(
            False, CODE_FABRICATED,
            "摘要只出现在你的自述里、工具回执中不存在——这是编造形状，不予采信",
            {"expected": exp})

    # ③ 回执里只有 nonce 原文 → 反射，不是执行
    if ch.nonce in _norm(obs_join):
        return AttestResult(
            False, CODE_ECHO_ONLY,
            "回执里只有 nonce 原文、没有摘要——说明只是把输入反射回来了，"
            "该原语并没有在目标上执行命令",
            {"nonce": ch.nonce})

    # ④ 回执里有 verify_output 哨兵但值不对 → 算了，但算错/算的是别的东西
    #    只看哨兵后的值，不扫全篇 hex：回执里常有 CSRF token、session id 等
    #    无关 hex，扫全篇会把它们误判成「算错」。
    for m in _SENTINEL_RX.finditer(obs_join):
        got = m.group(1).strip().lower()
        if got and got != exp:
            return AttestResult(
                False, CODE_WRONG_DIGEST,
                f"回执里 verify_output 的值与期望不符（得到 {got[:16]}，"
                "可能算的不是这个 nonce，或目标侧哈希实现不同）",
                {"expected": exp, "got": got[:64]})

    return AttestResult(
        False, CODE_NO_EVIDENCE,
        "既没找到摘要也没找到 nonce——没有任何自证证据（可能原语没跑起来）",
        {"expected": exp})
