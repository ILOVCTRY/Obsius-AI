"""工具结果保留库（H1，2026-09-19，借鉴 dsh output-retention）。

三个原语，替代散落在各工具里的一刀切 ``[:N]`` 截断：

- :func:`retain` —— 有界预览（head/tail 双窗）+ **精确省略计数**：
  模型看到的不再是「被截断了」而是「确切丢了多少字符」；
- :func:`omitted_note` —— 统一的省略注脚（计数口径单一出处）；
- :func:`spill_text` —— 超限结果全量落盘（spill），模型拿
  「预览 + 定位器 + 检索提示」按需用 run_cmd 分段取回，而非永久丢失。

设计约束：全部是尽力而为的纯函数/安全 IO，任何失败返回原值，绝不阻断工具链。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path


def retain(text: str, *, head: int, tail: int = 0) -> tuple[str, int]:
    """保留前 head + 后 tail 字符，返回 ``(保留文本, 省略字符数)``。

    未超限原样返回（省略数 0）；tail=0 退化为纯 head 窗。按 Python 字符
    （非字节）计数，与上下文预算口径一致。
    """
    if len(text) <= head + tail:
        return text, 0
    omitted = len(text) - head - tail
    kept = text[:head] + (text[-tail:] if tail else "")
    return kept, omitted


def omitted_note(omitted: int) -> str:
    """省略注脚：omitted=0 返回空串（调用方直接拼接即可）。"""
    return f"…[省略 {omitted} 字符]" if omitted > 0 else ""


def spill_text(text: str, name: str, spill_dir: Path) -> Path | None:
    """全量结果落盘到 spill_dir，返回文件路径；IO 失败返回 None（不阻断）。

    文件名 ``<本地时间>-<name>-<uuid6>.txt``；name 先做文件名安全化（非
    ``[A-Za-z0-9_-]`` 折线，防工具名带怪字符）。uuid 后缀（orchestrator-efficiency
    E1，2026-09-22）：原名仅（秒级时间戳+工具名）两维，同工具同秒两次超限
    （如同一轮先查 findings 再查 tasks，同为 bb_query）必互相覆盖丢数据。
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name) or "tool"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = spill_dir / f"{stamp}-{safe}-{uuid.uuid4().hex[:6]}.txt"
    try:
        spill_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    except OSError:
        return None
