"""CTF 解题会话调度器（代码侧，2026-10-19）。

**要解决的问题**：CTF 主控此前是「LLM 自己决定派谁、自己决定何时放弃」。
两个坏结局都来自这个决定权：

1. **死磕**：对已探尽的方向反复派 worker，直到比赛结束；
2. **早弃**：一次快检没命中就换方向，而那道题再挖十分钟就出。

本模块把这件事从模型手里拿走：**方向准入、轮次记账、放弃判定、方向互斥、
自证核验、上下文降级**全部是代码，模型的职责只剩「提出往哪走」。

**四个内核在这里合流**：

| 内核 | 在本模块的角色 |
|------|----------------|
| `scheduler.decide` | 每轮给每个方向一个 verdict（revive/requeue/drop + 理由） |
| `lease.ClaimTable` | 同一方向不许两个 worker 同时做 |
| `attest` | worker 声称「拿到执行」→ 强制目标机自证 |
| `fold` | 把「flag 台账 + 能力 + 事实」压进预算 |

**为什么记账在代码而不是让 LLM 自述**：LLM 报「这题我试了 2 轮」和「我试了
5 轮」在上下文里长得一模一样，而放弃判据的分母正是轮次。代码记账才有唯一
真值（``passes``/``correct``/``flag_stuck`` 都由实际派发与回执推进）。

**为什么调度计划要回给 LLM 而不是直接派发**：派发动作必须与 tool_use 配对
（网关要求每个 tool_use 有 tool_result），代码内部偷偷 spawn 会破坏历史重放。
故代码只产**计划**，由 LLM 在同一轮用多个 call_expert 并发执行计划——而
``_tool_call_expert`` 会校验「这条派发在计划内」，越权派发被代码拒收。

**落库边界（ctf-direction-ledger，2026-10-25 修正）**：本模块的判据与计数仍是
**代码侧唯一真值**，但方向账本会**镜像**到黑板（`core/ctf/ledger.py`）：
`CtfSession` 可被传入一个 `ledger`，此时

1. 构造时从账本恢复 `rounds` 与已派方向（跨轮不再「尚无方向记账」，分诊闸也
   不会因为新开一轮而重新拦）；
2. 准派/记账时把 `DirectionState` 回写账本（失败只记日志，不阻断调度）；
3. 每新增 5 个**终态**方向组切一个摘要批次（幂等键在 `ctf_summary_items`）。

镜像**不改判据**：租约仍是进程内 TTL 表（秒级易变状态不该落库），scheduler
的 verdict 仍只看 `DirectionState`。账本坏了，调度照跑。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import logging

from core.ctf import scheduler as sched
from core.ctf.attest import (
    Challenge,
    AttestResult,
    make_challenge,
    render_challenge,
    scan,
)
from core.ctf.fold import DEFAULT_BUDGET, Section, fold, render as render_fold
from core.ctf.lease import ClaimTable, direction_key
from core.ctf.observe import parse_linktypes, parse_script_names
from core.ctf.pacing import Pacing

log = logging.getLogger(__name__)

__all__ = [
    "DirectionState",
    "DispatchPlan",
    "CtfSession",
    "subgoal_of",
    "is_triage",
    "EXEC_CLAIM_RX",
    "STATUS_OPEN",
    "STATUS_REQUEUED",
    "STATUS_DROPPED",
    "STATUS_SOLVED",
    "STATUS_RETIRED",
    "triage_concluded",
    "dir_label",
]

STATUS_OPEN = "open"
STATUS_REQUEUED = "requeued"
STATUS_DROPPED = "dropped"
STATUS_SOLVED = "solved"
# **使命已完成**（ctf-triage-retire，2026-10-20）：方向的目标达成，但**不是**
# 靠 flag 达成——分诊方向的使命是「搞清楚这是什么题」，产出结论即收工。
#
# **为什么不能复用 SOLVED**：`solved` 在 CTF 语境里被读成「这题解出来了」，
# review 表格里给分诊方向打 `solved` 会让主控以为 flag 到手（那是台账的
# 语义，不是方向状态的语义）。两者必须分开，否则「已解出」这个词就贬值了。
STATUS_RETIRED = "retired"

# 「worker 声称拿到执行」的识别式。命中即强制自证（attest）。
# 刻意保守：只认**明确的执行证据**，不认「可能有注入点」这类猜测——
# 误触发自证会让 worker 白跑一轮，漏触发只是少一道保险。
EXEC_CLAIM_RX = re.compile(
    r"uid=0\(|uid=\d+\(|gid=\d+\(|"
    r"root@|www-data@|nt authority\\\\system|"
    r"\brce\b|remote code execution|command execution|代码执行|命令执行|"
    r"getshell|got shell|拿到\s*shell|反弹\s*shell|reverse shell|"
    r"webshell|web shell|已上传\s*shell|"
    r"\$\s*\w+@\w+|shell>|#\s*id\s*$",
    re.I)

# 弱进展识别：出现这些说明「本轮拿到了信息，但还没拿到实质突破」。
#
# **这是有界耐心判据的唯一入口**：`scheduler` 的「有产出/有弱进展且耐心未
# 耗尽 → 有界续投」要求 `weak=True`。识别式太窄的后果是实质缺陷——侦察结论
# （发现路径、端口、版本、参数、报错）全部不被记成弱进展，方向跑完 2 轮就
# 被 `alpha_zero_default` 判死，而它明明每一轮都在往前挪。
#
# 口径：**信息性**信号都算（路径/端口/版本/参数/报错/指纹），
# **结论性**信号（凭据/flag/执行）由 flags_added 与 attest 另行推进 correct。
#
# **证伪必须先剥掉再匹配**（ctf-refute-converge，2026-10-20 修正真 bug）：
# `[未命中] 0 帧 DNS，该方向证伪` 这类回执里有「命中」二字（`未命中` 的后半），
# 旧口径直接 `search` 就命中 `命中` 分支 → 判成 weak=True → 方向被当成「有侦察
# 进展」反复 revive，**证伪永远不收敛**（假性存活）。故先跑 `_REFUTED_RX`：
# 命中即 `refuted=True` 且**不置 weak**——证伪是结论，不是进展。
_REFUTED_RX = re.compile(
    r"证伪|已排除|排除掉|不成立|(?:未|没有|无)\s*命中|(?:未|没有|无)\s*发现|"
    r"not\s+found|no\s+(?:hit|finding|result|evidence)|dead[_\s-]?end|"
    r"此路不通|走不通|确认不存在|不存在该",
    re.I)

_WEAK_PROGRESS_RX = re.compile(
    r"flag\{|[A-Za-z][A-Za-z0-9_.\-]{1,30}\{[^{}\r\n]{2,200}\}|"
    r"cred|passw|password|口令|密码|凭据|"
    r"uid=0|shell|webshell|foothold|"
    r"反序列化|注入点|上传点|未授权|越权|"
    # 侦察结论（本轮补：没有这些，纯侦察轮全被判成「零进展」）
    r"发现|找到|存在|命中|疑似|可能|"
    r"路径|目录|端口|版本|参数|报错|响应|指纹|banner|header|"
    r"(?<![\w-])/[A-Za-z0-9_./-]{2,}|"
    r":\d{2,5}\b|\bv?\d+\.\d+\.\d+\b|"
    r"\b[45]\d\d\b|HTTP/\d",
    re.I)


# 「这一轮是不是分诊」的识别式（ctf-triage-first，2026-10-20，P0-5）。
#
# **为什么需要它**：用户复盘里 3 条方向盲派，其中第 1 条（基础分诊）与第 3 条
# （TLS/流重组）执行上完全重叠——都走「USB 载体识别 → SCSI LBA 重组 → 解
# flag.zip → 解 XOR」。代码判「准派 3 个」没拦，因为**「两条路径会走到同一个
# 终点」是执行后才可知的事实**，任何基于措辞的相似度判据都是在猜（`lease.py`
# 已为这个理由放弃模糊匹配）。
#
# 所以不猜重叠，改**编排**：先花 30 秒把「这是什么题」搞清楚，再派深挖。
# 用户裁决是**无条件**（「明显题目也做分诊」）——分诊成本极低，而「我以为一眼
# 就看出来」恰恰是误判率最高的时刻，本轮 3 份重复解析器就是从这句开始的。
#
# 判据刻意只认**元信息动作**（分诊/题型/魔数/题面/载体/file），不认「格式」
# 「类型」单字——后者太宽（「检查文件格式异常」也算），会把深挖方向误判成分诊。
_TRIAGE_RX = re.compile(
    r"分诊|triage|"
    r"题型|"
    # 「判断/确认/识别 + 这是(什么/什么类型)」——最常见的分诊措辞。
    # 允许「判断这是压缩包还是流量」这种二选一辨识（没有「什么」二字也算）。
    r"(?:识别|判断|确认|搞清|看清)\s*(?:一下)?\s*(?:这|它)?\s*是|"
    r"魔数|magic\s*(?:number|bytes?)|"
    r"题面|"
    r"\bfile\b|"
    r"载体",
    re.I)

# 分诊闸的**上界**（轮）：模型连续这么多轮都没做分诊就不再拦。
# 没有它，「模型死活不做分诊」会让主控永久不可用——闸的目的是一轮纠正，
# 不是把控制器锁死。3 轮足够它读两次拒绝理由并改道。
_TRIAGE_MAX_ROUNDS = 3


def is_triage(text: str) -> bool:
    """这条方向是不是「先搞清楚这是什么题」的分诊动作。"""
    return bool(_TRIAGE_RX.search(text or ""))


# 「分诊有结论了没有」的判据（ctf-triage-retire，2026-10-20）。
#
# **为什么光看 `is_triage(subgoal)` 不够**：那只能证明「派出去的是分诊」，
# 不能证明「分诊办完了」。worker 回执是 `[未命中] file 不可用` 也算派过。
# 闸放行深挖的前提必须是「已经知道这是什么题」，故要判**回执里有没有结论**。
#
# 判据刻意宽松（宁放过不误拦）：
#   - 回执里出现明确的载体/格式词（pcapng/pcap/zip/elf/pe/图片/音频/流量/...）
#   - 或出现魔数/文件类型标识（`PK\x03\x04`/`ELF`/`linktype`/`MIME`/`file 类型`）
#   - 或明确说「没有附件/纯交互题」（那也是一种结论：载体是「无」）
# 长度门槛 12 字符：`[未命中]` 这种三字空回执不算结论。
_TRIAGE_RESULT_RX = re.compile(
    r"pcapng|pcap|usb|hid|scsi|"
    r"\bzip\b|\brar\b|\b7z\b|\bgzip\b|\btar\b|"
    r"\belf\b|\bpe\b|\bpdf\b|\bpng\b|\bjpe?g\b|\bgif\b|\bbmp\b|\bwav\b|"
    r"\bmp3\b|\bmp4\b|\bavi\b|\bapk\b|\bpyc\b|\bclass\b|\bwasm\b|"
    r"魔数|magic|linktype|mime|"
    r"文件头|文件类型|真实形态|真实类型|"
    r"流量|抓包|磁盘镜像|内存镜像|图片|音频|视频|压缩包|可执行|"
    r"没有附件|无附件|纯交互题|纯交互|纯 web|无文件",
    re.I)

# 结论回执的最短长度：`[未命中]` / `无` / `N/A` 这类不算「搞清楚了」。
_TRIAGE_RESULT_MIN_CHARS = 12


def triage_concluded(summary: str) -> bool:
    """这条回执是否给出了「这是什么题/什么载体」的结论。

    **只看回执，不看方向名**：方向名叫「分诊」不代表分诊成功。worker 完全
    可能回一句 `[未命中] file 命令不存在`——那不是结论，是执行失败。
    """
    text = (summary or "").strip()
    # 明确的「没有附件/纯交互」本身就是完整结论，允许短回执；其他结论仍需
    # 有足够上下文，避免把单独一个 `pcap` / `zip` 误当作分诊完成。
    if re.search(r"没有附件|无附件|纯交互题|纯交互|纯 web|无文件", text, re.I):
        return True
    if len(text) < _TRIAGE_RESULT_MIN_CHARS:
        return False
    return bool(_TRIAGE_RESULT_RX.search(text))


def _fmt_dur(seconds: float) -> str:
    """秒 → 人读时长（`45s` / `12m` / `1.4h`）。负值/零一律 `0s`。

    刻意只做粗粒度：复盘关心的是「10 分钟还是 2 小时」，不是秒级精度。
    """
    s = max(0.0, float(seconds or 0.0))
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    return f"{s / 3600:.1f}h"


def dir_label(st, *, limit: int = 28) -> str:
    """方向的可读标签（给模型/人看）。

    **不要用 `st.key` 展示**：那是 `direction_key()` 的 2-gram 归一键，中文
    方向会切出跨词碎片——实测一条「先分诊：file 魔数 + 题面关键句，判断载体
    与题型」显示成 `file 先分 关键 分诊 判断 断载 载体 键句 面关 题型 题面 魔数`
    （「断载」「键句」「面关」都是跨词边界）。主控因此认不出那是自己刚派过的
    分诊方向，把它读成「另一套版本」，进而重复派工。

    键仍然保留在 state 里做去重/互斥——**只是不给人看**。
    """
    text = " ".join(str(getattr(st, "subgoal", "") or "").split())
    if not text:
        text = str(getattr(st, "key", "") or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# 「派发文本 → 纯方向」的剥壳式。**这是 2026-10-20 事故的正解**：
#
# `_task_text()` 给 worker 的任务是「【方向】X + 【要求】三条模板 + 可选【自证
# 任务】」，而 `allowed()` 要判的是「这条派发在不在计划内」。此前两者都直接
# `direction_key()` 整段文本 —— 模板里的「只做这个方向」「结论以 [命中] 开头」
# 「拿到疑似 flag 用 ctf_submit_flag 登记」全被当成方向词，于是**代码自己生成
# 的 task 原样派发 100% 被拒**（`【要求】` 段贡献了 `只/命中/开头/方向/结论/
# 登记/候选/证据/命令/输出…` 十来个 token，键必然不同）。
#
# 后果不是「偶发误拒」而是**主控完全不可用**：计划说「按 task 文本原样派发」，
# 照做就被打回，模型只能反复重试或干脆绕过调度。
#
# 所以：**键的推导必须先剥掉代码自己加的壳**，且剥壳只认代码写入的标记
# （【方向】/【要求】/【自证任务】），不做任何模糊猜测——模型自由发挥的文本
# 原样进 `direction_key()`，与从前行为一致。
_WRAP_HEAD_RX = re.compile(r"^\s*【方向】\s*")
_WRAP_TAIL_RX = re.compile(r"\n\s*【(?:要求|自证任务)】")


def subgoal_of(text: str) -> str:
    """从派发文本里还原纯方向（剥掉 `_task_text` 加的【方向】/【要求】壳）。

    没有代码标记的文本原样返回（模型自己写的方向不做任何加工）。
    """
    s = (text or "").strip()
    if not s:
        return ""
    m = _WRAP_TAIL_RX.search(s)
    if m:
        s = s[:m.start()]
    s = _WRAP_HEAD_RX.sub("", s)
    return s.strip()


def key_of(text: str) -> str:
    """派发文本 → 方向键。**所有判键入口都必须走这里**。

    直接用 `direction_key()` 吃整段 task 是 2026-10-20 那个 bug 的成因：
    模板文字会污染键。此函数先剥壳再归一，`plan`/`allowed`/`record` 三处
    口径因此强制一致——它们此前各算各的，才会出现「准派了但派不出去」。
    """
    return direction_key(subgoal_of(text))


@dataclass
class DirectionState:
    """一个方向的全部记账（代码侧唯一真值）。"""

    subgoal: str
    key: str
    expert: str = "worker"
    passes: int = 0
    correct: int = 0
    need: int | None = None
    weak: bool = False
    weak_only: int = 0
    milestone: bool = False
    # **证伪**（ctf-refute-converge，2026-10-20）：本轮回执明确说「这条路不通」
    # （未命中/已排除/不存在/dead end）。与 weak 互斥——证伪是**结论**不是进展，
    # 一旦置位就该收敛（`scheduler.decide` 的 `refuted` 判据直接 drop），
    # 而不是当成「有侦察进展」继续 revive。这是「证伪方向不收敛」的修复点。
    refuted: bool = False
    refuted_reason: str = ""
    status: str = STATUS_OPEN
    # 单调递增的墙钟（秒）——由代码在 record 时写入，不由 LLM 报
    first_at: float = 0.0
    last_progress_at: float = 0.0
    last_flag_at: float = 0.0
    hint_elapsed: float | None = None
    # 自证（attest）：worker 声称执行 → 出题；下一轮回执核验
    challenge: Challenge | None = None
    attest: AttestResult | None = None
    # 最近一次 worker 摘要（截断，供 fold/复盘）
    last_summary: str = ""
    # 该方向累计拿到的 flag 数（由调用方从台账差值写入）
    flags_got: int = 0
    # **回执里显式声明的 linktype**（p2-linktype，2026-10-20）：按出现顺序累积，
    # 供 `linktype_conflicts()` 做跨回执一致性检查。只收显式声明，不猜。
    linktypes: list[int] = field(default_factory=list)
    # **本轮产出的脚本文件名**（p2-dup-artifact，2026-10-20）：从回执里抽
    # `xxx.py` 之类的产物名，供 `duplicate_artifacts()` 发现「两个方向各写了
    # 一遍解析器」。**只做事后信号**——它不是拦截判据（发现时活已经干完了）。
    scripts: list[str] = field(default_factory=list)

    def revive_input(self, *, breadth_starved: bool, budget: float,
                     now: float) -> sched.ReviveInput:
        """把记账翻译成放弃决策的输入（纯映射，不做判断）。"""
        return sched.ReviveInput(
            passes=self.passes,
            correct=self.correct,
            need=self.need,
            weak=self.weak,
            weak_only=self.weak_only,
            milestone=self.milestone,
            refuted=self.refuted,
            time_stuck=max(0.0, now - (self.last_progress_at or self.first_at or now)),
            total_elapsed=max(0.0, now - (self.first_at or now)),
            flag_stuck=max(0.0, now - (self.last_flag_at or self.first_at or now)),
            hint_elapsed=self.hint_elapsed,
            breadth_starved=breadth_starved,
            stuck_budget=budget,
        )


@dataclass
class DispatchPlan:
    """一轮派发计划：代码已判定「哪些方向准派、哪些不准、为什么」。"""

    granted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    # 本轮已派发过的方向键（_tool_call_expert 据此校验越权派发）
    keys: set[str] = field(default_factory=set)
    # 分诊闸拦下的整批方向（ctf-triage-first）：非空时 granted 必为空，
    # 渲染时用**专门文案**而不是逐条「被拦下」——这是「先改编排」的纠正，
    # 不是「你这几个方向都不行」的否定。
    triage_blocked: bool = False

    def render(self) -> str:
        """给 LLM 看的计划文本（含被拒方向与理由——这是「为什么别派」的来源）。"""
        lines: list[str] = []
        if self.triage_blocked:
            lines.append("## 本轮准派：无（**分诊还没得出结论，先做分诊**）")
            lines.append(
                "\n先派**一条廉价方向**搞清楚这是什么题：`file` / 魔数 / linktype /"
                " 题面关键句。拿到「这是什么载体、什么格式」的结论，再派深挖方向。\n"
                "**为什么**：盲派 N 条深挖会各自重写一遍解析器（实测：3 个 agent"
                "各写了一份 USB 解析脚本，方向还完全重叠）。分诊只花 30 秒。\n"
                "**怎么做**：`ctf_dispatch(directions=[{subgoal: \"分诊：file 魔数 +"
                " 题面关键句，判断载体与题型\"}])`——这一轮只提这 1 条，"
                "不必凑满并发槽位。\n"
                "**注意**：结论必须明确说出载体/格式（如 pcapng / ZIP / ELF / "
                "纯交互题）。`[未命中] file 不存在` 这类执行失败**不算**分诊完成。")
            return "\n".join(lines)
        if self.granted:
            lines.append(f"## 本轮准派（{len(self.granted)} 个方向，请**同一轮并发**派出）")
            for i, g in enumerate(self.granted, 1):
                lines.append(f"{i}. expert={g['expert']} 方向={g['subgoal']}")
                lines.append(f"   task：{g['task']}")
                if g.get("attest"):
                    lines.append("   **本题必须先做自证**（见 task 内的【自证任务】段）")
        else:
            lines.append("## 本轮准派：无（所有方向都被代码判据拦下，见下）")
        if self.rejected:
            lines.append(f"\n## 被拦下的方向（{len(self.rejected)} 个，**不要派**）")
            for r in self.rejected:
                lines.append(f"- {r['subgoal'][:40]}：{r['reason']}")
        return "\n".join(lines)


class CtfSession:
    """一次 CTF 会话的代码侧调度状态（线程内，不跨会话共享）。"""

    def __init__(self, pacing: Pacing, *, claims: ClaimTable | None = None,
                 holder: str = "ctf-controller",
                 budget: int = DEFAULT_BUDGET,
                 clock: Any = None, ledger: Any = None):
        self.pacing = pacing
        self.claims = claims or ClaimTable()
        self.holder = holder
        self.budget = budget
        self._clock = clock or time.time
        self.dirs: dict[str, DirectionState] = {}
        self.rounds = 0
        self._last_plan: DispatchPlan | None = None
        # plan/record 会被主线程与并发派发的子线程同时触碰（先返先触发模式下
        # worker 完成即记账），故所有状态变更都要过这把锁。
        self._lock = threading.Lock()
        # 持久账本（可选）。None = 纯内存行为，与引入前完全一致（测试/独立
        # 调用不被迫建库）。有它才恢复跨轮状态、才切摘要批次。
        self.ledger = ledger
        if ledger is not None:
            self._restore_from_ledger()

    def _restore_from_ledger(self) -> None:
        """从账本恢复轮次与方向状态（线程内一次性，构造时跑）。

        **恢复的不是「权威判据」而是「上一轮的观测」**：passes/flags/状态都从
        库里读回，`DirectionState` 里无法持久化的部分（自证 challenge/attest、
        linktype 收集）保持初值——那些是单轮内的核验动作，跨轮重放反而会让
        「已出题未核验」的旧 nonce 重新生效。
        """
        try:
            self.rounds = self.ledger.rounds()
            for g in self.ledger.list_groups():
                key = str(g.get("key") or "")
                if not key:
                    continue
                self.dirs[key] = DirectionState(
                    subgoal=str(g.get("subgoal") or ""), key=key,
                    expert=str(g.get("expert") or "worker"),
                    passes=int(g.get("passes") or 0),
                    correct=int(g.get("correct") or 0),
                    need=(int(g["need"]) if g.get("need") is not None else None),
                    status=str(g.get("status") or STATUS_OPEN),
                    first_at=float(g.get("first_epoch") or 0.0),
                    last_progress_at=float(g.get("first_epoch") or 0.0),
                    last_flag_at=float(g.get("first_epoch") or 0.0),
                    last_summary=str(g.get("last_summary") or ""),
                    flags_got=int(g.get("flags_got") or 0))
        except Exception:  # noqa: BLE001 —— 账本故障退回纯内存调度
            log.exception("ctf 账本恢复失败 holder=%s", self.holder)

    # ---------- 方向准入 ----------

    def plan(self, directions: list[dict], *, breadth_starved: bool = False,
             default_expert: str = "worker") -> DispatchPlan:
        """给定 LLM 提出的方向，产出「准派 / 拦下」计划。

        ``directions`` 每项 ``{subgoal|task, expert?, need?}``。**同一方向键只
        准一次**（重复提交会被合并，防 LLM 换个措辞重派同一件事）。
        """
        with self._lock:
            return self._plan_locked(directions, breadth_starved=breadth_starved,
                                     default_expert=default_expert)

    def _plan_locked(self, directions: list[dict], *, breadth_starved: bool,
                     default_expert: str) -> DispatchPlan:
        self.rounds += 1
        self._persist_rounds()
        now = self._clock()
        plan = DispatchPlan()
        # 无条件首轮分诊闸（ctf-triage-first，P0-5）：**分诊还没得出结论之前**
        # 只放行分诊方向。判据是「有没有一条像分诊的」，不是「有没有一条不像
        # 深挖的」——宽进严出：只要提了一条分诊，其余深挖方向照常走后面的判据
        # （允许它一轮既分诊又试一个明确的面）。
        #
        # **判据是「分诊结论存在」，不是「第几轮」**（ctf-triage-retire，
        # 2026-10-20 修正）。原实现写 `self.rounds <= _TRIAGE_MAX_ROUNDS`，
        # 于是第 1 轮正确做了分诊、第 2 轮派深挖时 `rounds` 仍是 2 ≤ 3，
        # **闸再次拦下**——用户实测的「分诊做完了却还被硬拦」就是这个。轮次
        # 只是闸的**安全阀**（模型死活不做分诊时别把控制器锁死），不是闸的
        # 判据本身。判据必须是「这件事办完了没有」。
        if self.rounds <= _TRIAGE_MAX_ROUNDS and not self._triage_concluded_locked():
            clean_subs = [str(raw.get("subgoal") or raw.get("task") or "").strip()
                          for raw in directions or []]
            if clean_subs and not any(is_triage(s) for s in clean_subs):
                plan.triage_blocked = True
                plan.rejected.extend(
                    {"key": key_of(subgoal_of(s) or s) if s else "",
                     "subgoal": s,
                     "reason": "分诊还没得出结论（见本轮准派说明）"}
                    for s in clean_subs if s)
                self._last_plan = plan
                return plan
        seen_this_round: set[str] = set()
        for raw in directions or []:
            subgoal = str(raw.get("subgoal") or raw.get("task") or "").strip()
            if not subgoal:
                continue
            # 入口就剥壳：模型可能把上一轮的整段 task 当作新方向传进来。
            # `subgoal` 会被 `ClaimTable.claim()` 拿去**再算一次** `direction_key`
            # （lease 是独立模块，不认识【要求】模板），若这里不归一，state 键
            # 与租约键就会分叉——互斥闸静默失效，两个 worker 同方向并行。
            subgoal = subgoal_of(subgoal) or subgoal
            key = key_of(subgoal)
            expert = str(raw.get("expert") or default_expert).strip() or default_expert
            need = raw.get("need")
            need = int(need) if isinstance(need, (int, float)) and need > 0 else None

            if key and key in seen_this_round:
                plan.rejected.append({
                    "key": key, "subgoal": subgoal,
                    "reason": "与本轮另一条方向归一后相同（换个说法不算新方向）"})
                continue
            if key:
                seen_this_round.add(key)

            st = self.dirs.get(key) if key else None
            if st is not None and st.status == STATUS_DROPPED:
                plan.rejected.append({
                    "key": key, "subgoal": subgoal,
                    "reason": f"代码已判定放弃该方向（{st.last_summary[:60] or '见 ctf_review'}）"})
                continue
            # 判死后换个措辞重派 → 必须继续拦（否则 drop 形同虚设：新建 state
            # 的 passes 从 0 起，min_passes_floor 立刻让它复活）。
            if st is None and key:
                shadow = self._shadow_of(key)
                if shadow is not None:
                    plan.rejected.append({
                        "key": key, "subgoal": subgoal,
                        "reason": (f"该方向族已被判死（{dir_label(shadow, limit=20)}："
                                   f"{shadow.last_summary[:60] or '见 ctf_review'}）；"
                                   "换个说法不算新方向，要换就换**证据来源**完全不同的面")})
                    continue
            if st is not None and st.status == STATUS_SOLVED:
                plan.rejected.append({
                    "key": key, "subgoal": subgoal,
                    "reason": "该方向 flag 已拿满（台账已解出），别再派"})
                continue
            if st is not None and st.status == STATUS_RETIRED:
                plan.rejected.append({
                    "key": key, "subgoal": subgoal,
                    "reason": "该分诊方向已完成使命（已有载体/格式结论），不要续派"})
                continue

            # 放弃判据（代码判定，LLM 无权推翻）
            if st is not None:
                verdict = sched.decide(
                    st.revive_input(breadth_starved=breadth_starved,
                                    budget=float(self.pacing.hard_total_cap_s()),
                                    now=now),
                    self.pacing)
                if verdict.action == sched.ACTION_DROP:
                    st.status = STATUS_DROPPED
                    st.last_summary = verdict.reason
                    plan.rejected.append({"key": key, "subgoal": subgoal,
                                          "reason": f"放弃判据命中：{verdict.reason}"})
                    continue
                if verdict.action == sched.ACTION_REQUEUE:
                    st.status = STATUS_REQUEUED
                    plan.rejected.append({"key": key, "subgoal": subgoal,
                                          "reason": f"让位（回队尾，稍后再来）：{verdict.reason}"})
                    continue

            # 方向互斥（同方向已有他人在做 → 不重复派）
            claim = self.claims.claim(subgoal, self.holder)
            if not claim.granted:
                plan.rejected.append({"key": key, "subgoal": subgoal,
                                      "reason": claim.describe(subgoal)})
                continue

            if st is None:
                st = DirectionState(subgoal=subgoal, key=key, expert=expert,
                                    need=need, first_at=now,
                                    last_progress_at=now, last_flag_at=now)
                self.dirs[key or subgoal] = st
            else:
                st.expert = expert or st.expert
                if need:
                    st.need = need

            plan.granted.append({
                "key": key, "subgoal": subgoal, "expert": st.expert,
                "task": self._task_text(st, subgoal),
                "attest": bool(st.challenge and st.attest is None),
            })
            plan.keys.add(key)

        # 准派结果镜像到账本（ctf-direction-ledger）：**准派即登记**，而不是
        # 等子线程跑完才记。理由：主控可能派了但子线程被删/主控被停，那条方向
        # 仍然「已经提出并获准」，下一轮复核必须看得见它。登记失败的日志在
        # ledger 内部吞掉，调度不受影响。
        self._persist_granted(plan.granted)
        self._last_plan = plan
        return plan

    # ---------- 账本镜像（失败不阻断调度） ----------

    def _persist_rounds(self) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.set_rounds(self.rounds)
        except Exception:  # noqa: BLE001 —— 账本故障不该让主控停摆
            log.exception("ctf 轮次落库失败 holder=%s", self.holder)

    def _persist_granted(self, granted: list[dict]) -> None:
        if self.ledger is None or not granted:
            return
        for g in granted:
            try:
                st = self.dirs.get(g.get("key") or "")
                self.ledger.ensure_group(
                    subgoal=str(g.get("subgoal") or ""),
                    key=str(g.get("key") or ""),
                    expert=str(g.get("expert") or ""),
                    need=(int(st.need) if st is not None and st.need else None),
                    first_at=(st.first_at if st is not None else 0.0))
                if st is not None:
                    self.ledger.sync_from_state(st)
            except Exception:  # noqa: BLE001
                log.exception("ctf 方向组登记失败 key=%s", g.get("key"))

    def _persist_state(self, st) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.sync_from_state(st)
            # 终态组攒够 BATCH_SIZE 就切批次；摘要正文由 `run_pending_batches`
            # 单独触发（`ctf_review` 里做），派发热路径上只做「切批」这一件
            # 轻活，避免在 call_expert 收尾时多跑一次文本生成。
            self.ledger.ensure_batches()
        except Exception:  # noqa: BLE001 —— 账本故障不该让记账失败
            log.exception("ctf 方向组回写失败 key=%s", getattr(st, "key", ""))

    def flush_summaries(self) -> list[dict]:
        """生成所有 pending 批次的摘要并返回结果（供 `ctf_review` 调用）。"""
        if self.ledger is None:
            return []
        try:
            self.ledger.ensure_batches()
            return self.ledger.run_pending_batches()
        except Exception:  # noqa: BLE001
            log.exception("ctf 摘要批次处理失败 holder=%s", self.holder)
            return []

    def _task_text(self, st: DirectionState, subgoal: str) -> str:
        """生成自包含的 worker 任务文本（代码拼装，LLM 不用操心格式）。

        自证段在前：worker 若上一轮声称拿到执行，这一轮**先自证**再继续——
        否则它会一路往下挖，最后主控才发现整条链是幻觉。
        """
        parts = [f"【方向】{subgoal}", "", "【要求】",
                 "- 只做这个方向，不要顺手做别的方向；",
                 "- 结论以 [命中] 或 [未命中] 开头，后跟一句关键证据（命令/输出/偏移）；",
                 "- 拿到疑似 flag 用 ctf_submit_flag 登记（候选线索加 candidate=true）；"]
        if st.passes:
            parts.append(f"- 这是该方向的第 {st.passes + 1} 轮；上一轮结论："
                         f"{(st.last_summary or '（无）')[:400]}")
        if st.challenge is not None and st.attest is None:
            parts += ["", render_challenge(st.challenge,
                                           primitive=st.last_summary[:600])]
        return "\n".join(parts)

    # ---------- 记账 ----------

    def record(self, subgoal: str, summary: str, *, ok: bool = True,
               flags_added: int = 0, correct: int | None = None,
               need: int | None = None) -> dict:
        """记一轮结果。返回 ``{verdict, attest_issued, note}``。

        ``flags_added`` 由调用方从 flag 台账差值给出（**代码读库，不信自述**）。
        ``correct`` 未给时按「拿到 flag 或出现里程碑」判 1。
        """
        with self._lock:
            return self._record_locked(subgoal, summary, ok=ok,
                                       flags_added=flags_added,
                                       correct=correct, need=need)

    def _record_locked(self, subgoal: str, summary: str, *, ok: bool,
                       flags_added: int, correct: int | None,
                       need: int | None) -> dict:
        key = self._resolve_key_locked(subgoal)
        st = self.dirs.get(key)
        now = self._clock()
        if st is None:
            st = DirectionState(subgoal=subgoal_of(subgoal) or subgoal, key=key,
                                first_at=now, last_progress_at=now,
                                last_flag_at=now)
            self.dirs[key or subgoal] = st
        # 释放租约必须用**建租时那把键**（`st.subgoal`），不能拿本轮回执的
        # 文本重算——回执可能是带【要求】模板的 task，重算得到的是另一个键，
        # `release` 会静默失败（它只对持有者释放），租约于是挂到 TTL 到期，
        # 这期间该方向谁都派不出去。这是同一类「键口径不一致」缺陷的第二个面。
        lease_text = st.subgoal or subgoal
        st.passes += 1
        st.milestone = False
        text = summary or ""
        st.last_summary = text[:1000]
        # 收集回执里显式声明的 linktype（p2-linktype）：单条无从判断对错，
        # 攒起来才能发现「两个 worker 报了 220 和 249」这种跨回执矛盾。
        for v in parse_linktypes(text):
            if v not in st.linktypes:
                st.linktypes.append(v)
        # 收集产物脚本名（p2-dup-artifact）：两个方向各写一遍解析器 = 方向重叠
        # 的事后铁证。**只记录不拦截**——发现时活已经干完，拦没有意义。
        for s in parse_script_names(text):
            if s not in st.scripts:
                st.scripts.append(s)

        if flags_added > 0:
            st.flags_got += flags_added
            st.correct = max(st.correct, st.flags_got)
            st.milestone = True
            st.last_flag_at = now
            st.last_progress_at = now
        if need:
            st.need = need
        if st.need is not None and st.flags_got >= st.need:
            st.status = STATUS_SOLVED
            self.claims.release(lease_text, self.holder)

        # **分诊方向拿到结论即退役**（ctf-triage-retire，2026-10-20）。
        #
        # 根因：分诊方向的使命是「搞清楚这是什么题」，产出结论就完成了。
        # 但旧代码里它和普通方向一样走 `scheduler.decide`——零 flag、一轮、
        # 无里程碑 → 命中 ⑨ min_passes_floor → `revive`，状态留 `open`，
        # 租约续着。后果有三层，全在用户复盘的截图里：
        #   1. `review` 显示「open / 存活 1 个方向」，主控据此以为**分诊没
        #      收尾**，于是又派一条分诊方向「闭环」——正是截图里那句
        #      「我先派一条廉价分诊方向的续接」；
        #   2. 那一轮白烧一次 worker；
        #   3. 更糟：主控以为分诊还没完成 → 深挖批次继续被闸拦 → 整题卡死。
        #
        # 用 `RETIRED` 而不是 `SOLVED`：`solved` 在 CTF 语境里读作「解出来了」，
        # 给分诊方向打这个标会让「已解出」贬值（台账语义 ≠ 方向状态语义）。
        if (st.status in (STATUS_OPEN, STATUS_REQUEUED)
                and is_triage(st.subgoal) and triage_concluded(text)):
            st.status = STATUS_RETIRED
            self.claims.release(lease_text, self.holder)

        if correct is not None:
            if correct > st.correct:
                st.milestone = True
                st.last_progress_at = now
            st.correct = max(st.correct, correct)
        elif _REFUTED_RX.search(text) and not st.milestone:
            # **证伪优先于弱进展**（顺序不能反）：`[未命中] 0 帧 DNS，该方向
            # 证伪` 同时命中「命中」与「证伪」两个模式。先判证伪 → 置 refuted
            # 且**不置 weak**，下一轮 `scheduler.decide` 直接 drop。顺序反了
            # 就是原来那个 bug：证伪被记成侦察进展，方向反复 revive 不收敛。
            st.refuted = True
            st.refuted_reason = text[:300]
        elif _WEAK_PROGRESS_RX.search(text) and not st.milestone:
            # 弱进展：有信息但没拿到实质突破（由下一轮 verdict 决定要不要续）
            st.weak = True
            st.weak_only += 1

        # 自证触发：声称执行 → 出题（已出题未核验则不重复出）
        issued = False
        if st.challenge is None and EXEC_CLAIM_RX.search(text):
            st.challenge = make_challenge()
            issued = True

        # 已出题且本轮回执里出现期望摘要 → 核验通过
        attest_note = ""
        if st.challenge is not None and st.attest is None:
            res = scan(st.challenge, observations=[text], assertions=[])
            if res.confirmed:
                st.attest = res
                st.correct = max(st.correct, 1)
                st.milestone = True
                st.last_progress_at = now
                attest_note = "自证通过（目标机确实执行了）"
            elif res.code != "no_evidence":
                st.attest = res
                attest_note = f"自证未通过：{res.reason}"

        verdict = sched.decide(
            st.revive_input(breadth_starved=False,
                            budget=float(self.pacing.hard_total_cap_s()),
                            now=now), self.pacing)
        if st.status == STATUS_RETIRED:
            # scheduler 的通用方向判据不知道「分诊」这一特殊使命；状态已退役
            # 时，回执事件也必须反映已收尾，不能继续写 `revive` 造成口径撕裂。
            verdict = sched.Verdict(
                sched.ACTION_DROP,
                "分诊已得出载体/格式结论，方向使命完成并退役（不代表题目已解出）",
                "triage_concluded")
        if st.status == STATUS_OPEN:
            if verdict.action == sched.ACTION_DROP:
                st.status = STATUS_DROPPED
                self.claims.release(lease_text, self.holder)
            elif verdict.action == sched.ACTION_REQUEUE:
                st.status = STATUS_REQUEUED
                self.claims.release(lease_text, self.holder)
        # 回写账本：**在 verdict 落定之后**，否则会把「本轮判定前」的 open
        # 状态写进去，跨轮恢复后看到的方向状态永远慢一拍。
        self._persist_state(st)
        return {"verdict": verdict, "attest_issued": issued,
                "attest_note": attest_note, "state": st}

    def release(self, subgoal: str) -> bool:
        return self.claims.release(subgoal, self.holder)

    def _resolve_key(self, text: str) -> str:
        """派发文本 → **已有 state 的键**（精确键优先，其次唯一的方向族超集）。

        `record`/`release` 用它：被 `allowed` 以方向族放行的长键，记账必须
        落回**原来那个 state**，否则每次回贴都新建一个 `passes=0` 的孤立
        方向——放弃判据的分母永远归零，`drop` 永不触发（这是本 bug 的
        第二副面孔，比「派不出去」更隐蔽：调度看起来在跑，实际不记账）。

        注意：``_record_locked`` 的调用方已持锁，故真正干活的是
        ``_resolve_key_locked``；本方法是给外部（无锁上下文）用的包装。
        """
        with self._lock:
            return self._resolve_key_locked(text)

    def _resolve_key_locked(self, text: str) -> str:
        """同 `_resolve_key`，**调用方必须已持有 `self._lock`**（不可重入）。"""
        key = key_of(text)
        if not key:
            return ""
        if key in self.dirs:
            return key
        toks = set(key.split())
        hits = [k for k in self.dirs
                if k and set(k.split()) and set(k.split()) <= toks]
        # 多义时不猜（宁可按新方向处理，也不把结果记到错的题/方向上）
        return hits[0] if len(hits) == 1 else key

    def allowed(self, subgoal: str) -> bool:
        """``_tool_call_expert`` 的越权派发校验：这条派发在计划内吗？

        ``subgoal`` 可能有两种形态：模型直接写方向，或**原样回贴**计划里的
        task（含【方向】/【要求】模板）。两者必须判成同一个键——`key_of()`
        负责剥壳。这是 2026-10-20 那个「准派了却派不出去」的根因修复点。

        精确键之外还认**方向族**（``plan_key ⊆ 派发键``）：模型回贴计划里的
        task 时常常顺手补一句自己的说明（「…，顺便看 LSB」），键就多出
        ``lsb``。这不是越权，是把同一件事写长了。**只放行单向包含**——
        反过来（派发键 ⊆ 计划键）意味着模型砍掉了计划里的关键约束，仍然
        拦下让它按原样派。方向族判据是确定性的集合包含，与 `_shadow_of`
        同源：不引入相似度阈值，避免把真不同的方向合并。
        """
        key = key_of(subgoal)
        with self._lock:
            plan = self._last_plan
            if plan is None:
                return True      # 还没 plan 过（首轮盘点类派发）→ 放行
            if not key:
                return True      # 抽不出方向 → 放行（宁可重复也不锁死）
            if key in plan.keys:
                return self._still_dispatchable_locked(key)
            toks = set(key.split())
            for pk in plan.keys:
                ptoks = set((pk or "").split())
                if ptoks and ptoks <= toks:
                    return self._still_dispatchable_locked(pk)
        return False

    def _still_dispatchable_locked(self, key: str) -> bool:
        """**调用方必须已持锁**。在计划内，且**当前状态**仍允许派发。

        只查 `plan.keys` 是不够的（2026-10-20 第二个事故面）：一轮里并发派出
        多个 worker，其中一个先返回并在 `record()` 里把方向判死，此时
        `_last_plan` 还是旧的——同轮另一个同方向的派发会被放行，等于
        「判死了还在派」。所以状态也要看。

        ``requeue`` 必须放行（那是「先让别人上」，不是判死）；只拒
        ``dropped``/``solved``，与 `plan()` 的拦截口径一致。
        """
        st = self.dirs.get(key)
        if st is None:
            return True
        return st.status not in (STATUS_DROPPED, STATUS_SOLVED, STATUS_RETIRED)

    def _shadow_of(self, key: str) -> DirectionState | None:
        """``key`` 是否落在某个**已判死方向**的方向族里。

        **为什么必须有这道闸**：判死后若换个措辞就新建一个 `DirectionState`，
        它的 ``passes`` 从 0 开始 → `min_passes_floor` 立刻判 ``revive`` ——
        drop 形同虚设，模型只要重述一遍就能无限重派同一件事。这是实测出来的
        真实漏洞，不是理论风险。

        **判据是确定性的集合包含，不是相似度阈值**：``{robots.txt, 文件}``
        与 ``{robots.txt}`` 构成包含关系，判为同族；而 ``{sql, inject, login}``
        与 ``{xss, reflected}`` 毫无交集，各自独立。用 Jaccard 阈值会同时把
        「文件上传绕过 WAF」和「文件上传漏洞」判成同族（见 `lease.py` 里为
        什么放弃模糊匹配）——包含关系不会。

        **只对 DROPPED 生效**：``requeue`` 是「先让别人上」，不是判死，稍后
        必须能重派。
        """
        toks = set((key or "").split())
        if not toks:
            return None
        for st in self.dirs.values():
            if st.status != STATUS_DROPPED:
                continue
            dtoks = set((st.key or "").split())
            if not dtoks:
                continue
            if not (toks <= dtoks or dtoks <= toks):
                continue
            return st
        return None

    # ---------- 复核 ----------

    def review(self) -> str:
        """给 LLM 看的复核视图：verdict 表 + 方向占用 + 自证状态。"""
        with self._lock:
            return self._review_locked()

    def _review_locked(self) -> str:
        now = self._clock()
        budget = float(self.pacing.hard_total_cap_s())
        lines = ["## 代码侧复核（放弃判据由代码判定，你无权推翻）"]
        # 账本段单独算：内存为空但库里有记录时，正是「跨轮恢复」最关键的那一
        # 次复核——不能在这里早退成「尚无方向记账」（用户复盘现场）。
        ledger_lines: list[str] = []
        if self.ledger is not None:
            ledger_lines = self.ledger.review_lines()
        if not self.dirs:
            lines.append("- （尚无方向记账：先用 ctf_dispatch 提出方向）")
            if ledger_lines:
                # 库里有、内存里没有 = 恢复失败（不该发生，但要说清而不是装作
                # 没派过）。正常路径下 ledger 里的组在构造时已装进 self.dirs。
                lines.append("- 注意：库里存在历史方向组但未能载入内存，"
                             "下面的账本段仍可作为参考；派发前请先 ctf_dispatch。")
                lines.extend(ledger_lines)
            return "\n".join(lines)
        # 分诊结论置顶（ctf-triage-first，P0-5）：它是后续所有方向的**输入**，
        # 不是普通轮次。埋在表格里会被模型当「又一条侦察笔记」略过，于是
        # 后面的方向又变成盲派——这正是要修的行为。
        triage = self._triage_summary_locked()
        if triage:
            lines.append("\n### 分诊结论（后续方向必须依据它，别再盲派）")
            lines.append(f"- 方向：{triage[0]}")
            lines.append(f"- 结论：{triage[1][:600] or '（worker 未给结论）'}")
            lines.append("- 状态：已完成分诊并退役；这不代表题目已解出。")
            lines.append("派深挖方向前先看这条：它说了「这是什么载体/格式」。"
                         "和它矛盾的方向不要派。")
            lines.append("")
        # linktype 矛盾（p2-linktype，2026-10-20）：同一题内出现多个不同值时
        # 必须**在表格之前**报——它是「后续所有解析都建在错的布局上」的前置
        # 问题，埋在表格里等于没有。
        lt = self._linktype_conflicts_locked()
        if lt:
            detail = "；".join(f"{v}（{', '.join(keys)}）" for v, keys in lt.items())
            lines.append("\n### ⚠ linktype 矛盾（至少一半的解析建在错的布局上）")
            lines.append(f"- 回执里报了 {len(lt)} 个不同值：{detail}")
            lines.append("- 代码不知道哪个对。**先派一个 worker 用一手命令复核**"
                         "（`capinfos` / `tshark -r x.pcapng -V | head`），"
                         "确认后再派深挖；别再让两个方向各按一个值往下解。")
            lines.append("")
        lines.append("| 方向 | 轮次 | 已得/需要 | 耗时 | 状态 | 代码判定 |")
        lines.append("|---|---|---|---|---|---|")
        for st in self.dirs.values():
            if st.status == STATUS_RETIRED:
                v = sched.Verdict(
                    sched.ACTION_DROP,
                    "分诊已得出载体/格式结论，方向使命完成并退役（不代表题目已解出）",
                    "triage_concluded")
            else:
                v = sched.decide(
                    st.revive_input(breadth_starved=False, budget=budget, now=now),
                    self.pacing)
            need = f"/{st.need}" if st.need else ""
            at = ""
            if st.attest is not None:
                at = "（自证✓）" if st.attest.confirmed else "（自证✗）"
            elif st.challenge is not None:
                at = "（待自证）"
            # 耗时列（ctf-cost，P1-2）：**不改任何判据**（时间判据已在 scheduler
            # 里用），只是让人看得见「这个方向烧了多久」。机会成本不量化，
            # 复盘就没法回答「这题值不值 3 个并发槽位」。
            lines.append(f"| {dir_label(st)} | {st.passes} | "
                         f"{st.correct}{need} | {_fmt_dur(now - st.first_at)} | "
                         f"{st.status}{at} | "
                         f"{v.action}：{v.reason} |")
        lines.append("")
        lines.append(self._cost_line_locked(now))
        # 重复产物（p2-dup-artifact）：只在**同一脚本名被 ≥2 个方向产出**时报。
        # 它是「方向重叠」的事后铁证——三个 worker 各写一遍解析器，说明分诊
        # 没把载体/格式说清楚。**不拦任何东西**（发现时活已干完），只提示
        # 「后续方向别再写第四遍，去复用」。
        dup = self._duplicate_artifacts_locked(min_dirs=2)
        if dup:
            lines.append(f"\n**⚠ 重复产物**：{len(dup)} 个**尚未成功**的方向各写了脚本 —— "
                         + "；".join(dup))
            lines.append("正常情况一个方向写完就解出来了。**两个以上方向各写一遍"
                         "**说明它们在干同一件事：后续方向**先复用**已有脚本，"
                         "别再写第四遍；下一轮分诊要把「载体/格式」说得更具体。"
                         "（已拿到 flag 或自证通过的方向不计入本告警——它们写脚本是"
                         "正常工作，不是重复劳动。）")
        table = self.claims.render_table()
        if table:
            lines.append("\n## 进行中的方向（互斥表）")
            lines.append(table)
        if ledger_lines:
            lines.extend(ledger_lines)
        return "\n".join(lines)

    def _cost_line_locked(self, now: float) -> str:
        """末尾一行「本题累计 N 轮 / M 分钟」。

        轮次用 `rounds`（派发轮数，含被拦的），耗时用**最早方向的 first_at**
        到 now——它是本题真正开始投入的时刻。没有方向时给 0，不给假数。
        """
        total_passes = sum(st.passes for st in self.dirs.values())
        started = min((st.first_at for st in self.dirs.values() if st.first_at),
                      default=0.0)
        elapsed = (now - started) if started else 0.0
        return (f"**本题累计**：{self.rounds} 轮派发 / {total_passes} 次执行 / "
                f"{_fmt_dur(elapsed)} / 存活 {sum(1 for s in self.dirs.values() if s.status == STATUS_OPEN)} 个方向")

    def linktype_conflicts(self) -> dict[int, list[str]]:
        """跨回执 linktype 矛盾：同一题内出现多个不同 linktype 时，返回
        ``{值: [报出该值的方向键, ...]}``；无矛盾返回空 dict。

        **为什么需要它**（p2-linktype，2026-10-20 复盘）：两个 worker 分别报
        「linktype 220」和「linktype 249」，主控两条都信，于是按两个不同的
        链路层布局各解一遍——其中至少一次是白跑的。

        **为什么判据是「同一题内多个不同值」而不是「值不合法」**：单条回执
        里任何值都语法合法，无从判断对错。矛盾只在**并置**时出现。

        **为什么 ≥2 个值才算冲突、而不是「与题面 meta 不符」**：题面写的
        linktype 是提示不是约束（与 `flag_format` 同一教训）——写 `220` 实际
        `249` 在 CTF 里很常见，拿它比对会误杀真结论。

        **不自动判谁对**：代码不知道哪个值是真的，只能告诉主控「这里有矛盾，
        去用一手命令复核」。自动挑一个等于赌。
        """
        with self._lock:
            return self._linktype_conflicts_locked()

    def _linktype_conflicts_locked(self) -> dict[int, list[str]]:
        by_value: dict[int, list[str]] = {}
        for st in self.dirs.values():
            for v in st.linktypes:
                by_value.setdefault(v, []).append(dir_label(st, limit=20))
        # 只有一个值（或零个）→ 没有矛盾，不报
        return by_value if len(by_value) > 1 else {}

    def duplicate_artifacts(self, *, min_dirs: int = 2) -> list[str]:
        """重复产物：**≥2 个不同方向都产出了脚本** → 返回「类别: 脚本@方向」清单。

        **为什么判据是「方向数」而不是「脚本名相同」或「语义类别相同」**
        （p2-dup-artifact，2026-10-20，两次修正后定稿）：复盘里真实的重复劳动是
        `solve_usb_pcapng.py`、`usbpcap_extract.py`、`parse.py`——三个名字全不
        一样，语义词也分属 solve/extract/parse 三档。按名字比对抓不到；按语义
        词分档同样抓不到（各档各一份）。

        而「两个不同方向都写了脚本」本身就是**方向重叠的强信号**：正常情况
        一个方向写完就解出来了，写脚本说明卡住了；两个方向各写一遍，说明它们
        在做同一件事。故代码只给这个信号，**不替模型判两个脚本是不是同一类**——
        那需要语义理解，猜错的代价是误报把真信号淹没。

        **为什么只做事后信号、不做拦截**：发现重复时活已经干完了。价值在两处：
        ① 事后复盘调 `_TRIAGE_RX` 的覆盖面（三个 worker 各写一遍解析器，说明
        分诊没把「载体与格式」说清楚）；② 本轮把「已有哪些脚本」注入给后续
        worker，避免第四遍。

        ``min_dirs`` 是「至少几个不同方向各写过」才算重复。默认 2——同一方向
        写两版脚本是正常迭代，不算重复劳动。

        **排除「已经成功的方向」**（dup-artifact-false-positive，2026-10-23）：
        R3 那题的 `reverse` 方向交付了**四道双向回环的独立复核**（真产出、真结论），
        却与另一条探索方向一起被算成「两个方向各写了脚本」→ 判 `retired/drop`。
        主控据此以为该方向零产出。

        判据：`flags_got > 0`（拿到过 flag）或 `correct`（自证核验通过）的方向
        **不参与**重复判定——它已经产出了可验证的结果，写脚本是正常工作而不是
        重复劳动。只把「都还没成、却各自写了一版」算作重叠信号。
        """
        with self._lock:
            return self._duplicate_artifacts_locked(min_dirs=min_dirs)

    def _duplicate_artifacts_locked(self, *, min_dirs: int) -> list[str]:
        # 先按方向聚：{方向键: [脚本名...]}，只留真产出过脚本的**未成功**方向
        by_dir: dict[str, list[str]] = {}
        for st in self.dirs.values():
            if not st.scripts:
                continue
            # 已产出可验证结果的方向不算重复劳动（见 docstring）
            if st.flags_got or st.correct:
                continue
            key = dir_label(st, limit=20)
            by_dir.setdefault(key, [])
            for name in st.scripts:
                if name not in by_dir[key]:
                    by_dir[key].append(name)
        if len(by_dir) < min_dirs:
            return []
        return [f"{', '.join(names)}@{key}" for key, names in by_dir.items()]

    def _triage_concluded_locked(self) -> bool:
        """分诊是否已得出结论（**调用方必须已持锁**）。

        判据与 `review()` 里「分诊结论」小节**同一个** `triage_concluded()`——
        否则会出现「复核说已经分诊过了、闸却说还没做」的撕裂（这是
        `is_triage()` 那次撕裂的翻版，同类缺陷不能再犯）。
        """
        return any(is_triage(st.subgoal) and triage_concluded(st.last_summary)
                   for st in self.dirs.values())

    def _triage_summary_locked(self) -> tuple[str, str] | None:
        """找最近一次「分诊方向」的回执（subgoal, summary）；没有则 None。

        判据与 `plan()` 的闸**同一个** `is_triage()`——否则会出现「闸放行了
        它，复核却不认它是分诊」的撕裂。
        """
        best: tuple[float, str, str] | None = None
        for st in self.dirs.values():
            if not is_triage(st.subgoal):
                continue
            if not st.last_summary:
                continue
            if best is None or st.last_progress_at >= best[0]:
                best = (st.last_progress_at, st.subgoal, st.last_summary)
        return (best[1], best[2]) if best else None

    # ---------- 上下文降级 ----------

    def folded_context(self, *, flags: list[str] | None = None,
                       caps: list[str] | None = None,
                       facts: list[str] | None = None,
                       budget: int | None = None) -> str:
        """把「flag 台账 + 能力 + 事实」压进预算后渲染。

        ``flags`` 段**必保**（mandatory）：它是解题进度唯一真值，任何降级级别
        下都不许消失——否则主控会忘记自己已经拿了几分，重复派工。
        """
        secs = [
            Section("flags", "flag 台账（进度唯一真值）", list(flags or []),
                    priority=1, mandatory=True, note="拿满即解出，别再派该方向"),
            Section("caps", "已证实能力（自证通过）", list(caps or []), priority=5),
            Section("facts", "侦察事实", list(facts or []), priority=10),
        ]
        proj = fold(secs, budget=budget or self.budget)
        return render_fold(proj)

    # ---------- 展示 ----------

    def describe(self) -> str:
        with self._lock:
            alive = [s for s in self.dirs.values() if s.status in
                     (STATUS_OPEN, STATUS_REQUEUED)]
            return (f"rounds={self.rounds} 方向={len(self.dirs)} 存活={len(alive)} "
                    f"租约={self.claims.stats()['active']} {self.pacing.describe()}")
