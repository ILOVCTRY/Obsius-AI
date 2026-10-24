"""对话消息来源判定：人类发言 vs 系统注入（system-origin，2026-10-24）。

**问题**（用户实测，2026-10-24）：后台专家补达这条消息，前端渲染成了「人类说
的话」——`❯` 引导符 + 原文直出。于是模型看到的是 Markdown，人看到的是
`**加粗**`、围栏 ``` 的字面量；一条 600 字的补达把会话流糊满半屏。

**根因是两层约定没有绑定**。`background_wake.py` 与 `ctf_wake.py` 的注释都写着
「前端据此把这条渲染成系统提示」——但前端从来没实现那段识别，`role=user` 一律
走 `HumanNote`。文案层写了一份约定，渲染层没有对应实现，也没有测试把两者钉在
一起，所以静默失效了两个多月。

**为什么用前缀判定而不是落库字段**：`ChatTurn.run(text)` 是**人类发消息与系统
唤醒共用的唯一入口**（见 `app._chat_launch_turn` 的 docstring，两条链刻意共用
装配代码），它无条件把入参落成 `role=user`，没有第二条区分通道。加一列
`origin` 要动 schema 迁移 + 61 个 `append_message` 调用点，而判定依据（文案开头
的中文方括号标签）本来就写在每条系统文案的第一行——是**既有事实**，不是新造
的约定。

**但判定只在后端做一次**：前端不复制这份前缀表（两处各写一份必然漂移，正是
本次事故的形态）。`store.list_messages` 读出来时派生 `origin` 字段，前端只认
字段、不认字符串。

**白名单而不是「任意〔…〕」**：正文里也会出现 `〔dead_end 背书〕` 这类方括号
短语（见 `blackboard/store.py` 的提示词）。开放匹配会把人类长文里随手写的方括号
误判成系统消息。只认下面四个已知标签。
"""

from __future__ import annotations

import re

# 四类系统注入的标签（方括号内文字）。**新增一类系统文案时必须在此登记**，
# 否则前端会把它当人类发言渲染——`test_chat_system_origin.py` 有跨模块一致性
# 断言，改文案标签却漏登记会让它变红。
LABEL_BG_DONE = "系统补达"       # background_wake：后台专家结果回执
LABEL_CTF_WAKE = "系统唤醒"      # ctf_wake：候选 flag 待人工确认
LABEL_FLAG_LEDGER = "人工确认"   # app._solved_notice：flag 台账更新
LABEL_AUTO_WAKE = "主动唤醒"     # orchestrator.wake_brief_text：异常事件简报

SYSTEM_LABELS = frozenset({
    LABEL_BG_DONE, LABEL_CTF_WAKE, LABEL_FLAG_LEDGER, LABEL_AUTO_WAKE,
})

# `〔标签〕` 只允许出现在**行首**：系统文案的构造方都是 `f"{TAG}…"`。
# 标签限长 1–12 字（现最长「系统补达」4 字），避免把整段当标签吞掉。
_TAG_RE = re.compile(r"^〔([^〕\n]{1,12})〕[ \t]*")


def tag(label: str) -> str:
    """把标签渲染成方括号前缀。**所有系统文案必须经此生成**，别手写 `〔…〕`。"""
    return f"〔{label}〕"


def split(text: str) -> tuple[str, str] | None:
    """``〔标签〕正文`` → ``(标签, 正文)``；不是已知系统消息则返回 None。

    标签必须命中 `SYSTEM_LABELS` 白名单，正文取标签后的全部内容（含空行）。
    """
    body = str(text or "")
    m = _TAG_RE.match(body)
    if m is None:
        return None
    label = m.group(1)
    if label not in SYSTEM_LABELS:
        return None
    return label, body[m.end():]


def describe(content: str) -> dict[str, str] | None:
    """给 `store.list_messages` 用的派生结构；人类消息返回 None（不写该键）。

    返回 ``{"kind": "system", "label": ..., "body": ...}``：

    - ``label``：给人看的短标签（「系统补达」），渲染成摘要行前缀；
    - ``body``：**剥掉标签后的正文**——前端渲染 Markdown 用它，而不是整段
      content。`content` 仍是审计原文，不改写（诊断页要看到真实落库内容）。
    """
    got = split(content)
    if got is None:
        return None
    label, body = got
    return {"kind": "system", "label": label, "body": body}
