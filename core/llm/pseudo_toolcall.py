"""伪工具调用（特殊 token 泄漏进正文）的检测与剥离。

背景（2026-10-20 事故）：DeepSeek 系模型的工具调用由**特殊 token** 承载
（``<｜｜DSML｜｜ calls>`` / ``<｜｜DSML｜｜ invoke name="…">`` /
``<｜｜DSML｜｜ parameter name="…">``，其中 ``｜`` 是 U+FF5C 全角竖线）。
正常应由网关解析进 ``tool_calls`` 字段；实测网关（幻灵-DEEP / openai-responses）
把整段标记**原样塞进 content**，于是：

- 对话链看到 ``tool_calls=[]``、``text`` 非空 → 判定「纯文本终稿」→ 落库、
  发 ``chat.message``、``break``。线程 ``status='idle'``、``error=''``，
  **系统认为一切正常**，用户体感却是「AI 突然把一坨标记吐在屏幕上就停了」。
- 任务管线（``core/agent/loop.py`` 的 run_chat）同一个漏洞。
- 更糟的是会**二次扩散**：``call_expert`` 回传的「收尾摘要」原样嵌入子线程
  的泄漏文本，落成主控的 tool_result，回放时继续教模型模仿这个格式。

本模块只做两件纯函数的事（**不反向解析成 tool_call 去执行**）：参数 JSON
可能不完整、值可能是幻觉，且绕过了 ``tool_calls`` 通道的 id/名称/参数校验
——把「显示错误」升级成「执行了不该执行的工具」不划算。正确处置是剥掉
标记、让模型用工具通道重发。
"""

from __future__ import annotations

import re

# 强标记：出现即判泄漏。这类 token 不可能出现在正常正文里（哪怕模型在写
# 工具调用格式的说明文档，也不会打出 U+FF5C 连写的特殊 token）。
_STRONG_PATTERNS: tuple[re.Pattern[str], ...] = (
    # DeepSeek DSML（实测形态）：<｜｜DSML｜｜ calls> / <｜｜DSML｜｜ invoke …>
    re.compile(r"<[｜|]{2}DSML[｜|]{2}"),
    # DeepSeek 旧特殊 token：<｜tool▁calls｜>（▁ = U+2581）
    re.compile(r"<[｜|]tool[▁_]?calls[｜|]>"),
)

# 弱标记：XML 形态（Qwen / GLM / 部分中转网关把工具调用序列化成文本）。这些
# 标签本身可能是**正文在讨论**它，故只在行首出现时判泄漏——真泄漏必然是整块
# 从行首起排的调用块。
_WEAK_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[ \t]*<(?:/)?tool_calls?>", re.M),
    re.compile(r"^[ \t]*<(?:/)?function_calls?>", re.M),
)

# 块级删除：成对标记连同中间载荷一起删（载荷是参数值，单删标签会留一地
# JSON 碎片）。
_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<[｜|]{2}DSML[｜|]{2}\s*calls\b.*?</[｜|]{2}DSML[｜|]{2}\s*calls>",
               re.S),
    re.compile(r"<[｜|]{2}DSML[｜|]{2}\s*invoke\b.*?</[｜|]{2}DSML[｜|]{2}\s*invoke>",
               re.S),
    re.compile(r"<[｜|]tool[▁_]?calls[｜|]>.*?</[｜|]tool[▁_]?calls[｜|]>", re.S),
    re.compile(r"<tool_calls?>.*?</tool_calls?>", re.S),
    re.compile(r"<function_calls?>.*?</function_calls?>", re.S),
)

# 残留标记 token（块未闭合 / 单标签）→ 直接删。
_TAG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"</?[｜|]{2}DSML[｜|]{2}[^>]*>"),
    re.compile(r"</?[｜|]tool[▁_]?calls[｜|]>"),
    re.compile(r"</?tool_calls?>"),
    re.compile(r"</?function_calls?>"),
)

# 流式抑制用的「标记前缀」集合：只收长度 ≥3 的前缀（``<｜｜`` / ``<||`` /
# ``<|t`` / ``<to`` / ``<fu``）。单个 ``<`` 太常见，收进来会把「正文以小于号
# 结尾」误判成标记开头。
_LITERAL_STARTS = (
    "<｜｜DSML｜｜", "<||DSML||", "<｜tool▁calls｜>", "<|tool_calls|>",
    "<tool_call>", "<tool_calls>", "<function_call>", "<function_calls>",
)
_PREFIXES: frozenset[str] = frozenset(
    m[:i] for m in _LITERAL_STARTS for i in range(3, len(m) + 1))


def has_pseudo_tool_call(text: str) -> bool:
    """正文里是否含伪工具调用标记（= 不能当纯文本终稿收轮）。"""
    if not text:
        return False
    if any(p.search(text) for p in _STRONG_PATTERNS):
        return True
    return any(p.search(text) for p in _WEAK_LINE_PATTERNS)


def has_marker_prefix_tail(text: str) -> bool:
    """正文尾部是否是某个标记的**前缀**（流式下用来「先别发，等下一块确认」）。

    只判断、不改状态：命中只是暂缓发布一次增量，不会把该步判成泄漏。
    """
    if not text:
        return False
    tail = text[-24:]
    for n in range(3, len(tail) + 1):
        if tail[-n:] in _PREFIXES:
            return True
    return False


def strip_pseudo_tool_calls(text: str) -> str:
    """剥掉伪工具调用标记，返回剩余正文（可能是空串）。

    顺序固定：① 删成对块（含载荷）→ ② 仍有强标记残留说明块未闭合（流被截断
    或格式残缺），从最早的强标记起**整段丢弃**（其后都是参数载荷）→ ③ 删
    行首的弱标记行 → ④ 删残留单标签 → ⑤ 归一空白。
    """
    if not text:
        return ""
    out = text
    for p in _BLOCK_PATTERNS:
        out = p.sub("", out)
    # 未闭合块：其后必是载荷，整段丢
    cut = min((m.start() for p in _STRONG_PATTERNS
               for m in [p.search(out)] if m), default=None)
    if cut is not None:
        out = out[:cut]
    out = "\n".join(ln for ln in out.splitlines()
                    if not any(p.match(ln) for p in _WEAK_LINE_PATTERNS))
    for p in _TAG_PATTERNS:
        out = p.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def sanitize_response_text(response) -> str:
    """就地剥掉 ``LLMResponse.text`` 里的伪工具调用标记，返回剥离后的文本。

    给「纯文本不是终态」的链路用（任务轮 `_loop_body`、编排器 tick/对话轮）：
    它们本来就会继续循环或另判结论，不需要 nudge 重试，只需保证**泄漏文本不
    进历史、不上屏**。有 tool_calls 的响应不动——正常路径 text 是调用前的叙述，
    真含标记也说明模型混写了，同样剥掉更安全，但保留 tool_calls 语义不变。
    """
    if has_pseudo_tool_call(getattr(response, "text", "") or ""):
        response.text = strip_pseudo_tool_calls(response.text)
    return response.text or ""


# 推回文案（两条链路共用一份，防两处口径漂移）。
PSEUDO_TC_NUDGE = (
    "（系统提示）你上一条回复把**工具调用写成了正文文本**（`<｜｜DSML｜｜ …>` "
    "这类特殊标记泄漏进了 content 通道），系统无法执行它。"
    "请改用工具调用通道重新发出该调用——不要在正文里手写任何调用标记、标签或"
    "参数 JSON。若你本意是给人类结论，就直接给结论，不要附带调用块。")

# 连续推回上限：超过则不再重试（防死循环），落明确终稿文案。
PSEUDO_TC_MAX = 3

# 上限后（或末步无工具可调时）的终稿文案：必须说清「为什么停」，不能像
# 原事故那样静默 idle——用户看到的就是「莫名其妙停了」。
PSEUDO_TC_FINAL_TEXT = (
    "（本轮中止：模型把工具调用写成了正文文本（特殊标记泄漏），系统无法执行。"
    "请重试；若反复出现，请在设置里换一个模型通道）")
