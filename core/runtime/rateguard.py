"""扫描限速纪律（借鉴 dsh-redteam-model 的 rateDiscipline，2026-09-19 落地）。

裸奔扫描 = 不带限速参数的全量扫描：一跑就是数万包/数千并发连接，轻则触发
WAF 封出口 IP、重则打瘫目标（「宁严勿松」的时序面）。此处拦的不是「能不能
扫」，而是「不许不限速地扫」——拒绝文案给出放行参数，Agent 补参数即可重试。

纯函数无 IO；分段解析（; && || |）后逐段识别工具名再套规则。
诚实边界与 pathguard 相同：变量间接/别名不静态穷尽，本模块是纪律护栏，
不是资源隔离；阈值（masscan 1000、nmap T0-3 等）为定稿保守值。
"""

from __future__ import annotations

import re

from core.runtime.pathguard import _clean, _split_tokens

_SEG_SPLIT_RE = re.compile(r"&&|\|\||;|\|")

# nmap 全端口写法（-p- / -p1-65535 / -p 1-65535 / -p-65535）
_FULL_RANGE = {"-", "1-65535", "0-65535", "-65535"}


def _segments(cmd: str) -> list[list[str]]:
    """按 ; && || | 分段，各段再切 token（引号感知）。"""
    out: list[list[str]] = []
    for seg in _SEG_SPLIT_RE.split(cmd):
        toks = _split_tokens(seg)
        if toks:
            out.append(toks)
    return out


def _binname(toks: list[str]) -> str:
    """段内工具名 = 首个 token 的 basename（容忍 /usr/bin/nmap、.\nmap.exe）。"""
    if not toks:
        return ""
    t = _clean(toks[0]).replace("\\", "/").lower()
    return t.rsplit("/", 1)[-1].removesuffix(".exe")


def _flag_value(low: list[str], i: int) -> str:
    """--flag=vals 连写取等号后；否则取下一 token。"""
    t = low[i]
    if "=" in t:
        return t.split("=", 1)[1]
    return low[i + 1] if i + 1 < len(low) else ""


def _has_flag(low: list[str], *names: str) -> bool:
    return any(t.split("=", 1)[0] in names for t in low)


def _full_range(v: str) -> bool:
    return v in _FULL_RANGE


def _nmap_reason(low: list[str]) -> str | None:
    """全端口扫描必须带限速参数（-T0..3 / --max-rate / --max-parallelism / --scan-delay）。"""
    full = False
    for i, t in enumerate(low):
        if t == "-p-":
            full = True
        elif t in ("-p", "--ports"):
            if _full_range(_flag_value(low, i)):
                full = True
        elif t.startswith("-p") and t != "-p" and _full_range(t[2:]):
            full = True
        elif t.startswith("--ports=") and _full_range(t.split("=", 1)[1]):
            full = True
        if full:
            break
    if not full:
        return None
    throttled = any(re.fullmatch(r"-t[0-3]", t) for t in low) or (
        "-t" in low and any(low[i + 1] in {"0", "1", "2", "3"}
                            for i, t in enumerate(low) if t == "-t" and i + 1 < len(low))
    ) or _has_flag(low, "--max-rate", "--max-parallelism", "--scan-delay")
    if throttled:
        return None
    return ("限速纪律：nmap 全端口扫描未带限速参数（一跑就是 65535 端口全速发包）。"
            "补任一即可放行：-T3 --max-rate 200（推荐）/ -T0..2 / --max-parallelism / --scan-delay")


def _masscan_reason(low: list[str]) -> str | None:
    for i, t in enumerate(low):
        if t.split("=", 1)[0] == "--rate":
            v = _flag_value(low, i)
            if v.isdigit() and int(v) > 1000:
                return (f"限速纪律：masscan --rate {v} 超过阈值 1000（p/s）。"
                        "降到 --rate 1000 及以下（保守默认 500）再执行")
    return None


def _ffuf_reason(low: list[str]) -> str | None:
    if not _has_flag(low, "-rate", "-rl"):
        return ("限速纪律：ffuf 未带速率限制（默认不限速全速跑）。"
                "补 -rate 50（推荐）或 -rl <每秒请求数> 再执行")
    return None


def _hydra_reason(low: list[str]) -> str | None:
    has_t = any(re.fullmatch(r"-t\d+", t) for t in low) or any(
        t == "-t" and i + 1 < len(low) and low[i + 1].isdigit()
        for i, t in enumerate(low)
    )
    if not has_t:
        return ("限速纪律：hydra 未带并发任务数 -t（默认 16 并发易触发账户锁定/封禁）。"
                "补 -t 8（推荐）及以下再执行")
    return None


# 工具名 → 校验函数（校验失败返回拒因文案）
_RULES = {
    "nmap": _nmap_reason,
    "masscan": _masscan_reason,
    "ffuf": _ffuf_reason,
    "hydra": _hydra_reason,
}


def check_rate(cmd: str) -> str | None:
    """检查命令中的扫描工具是否带限速参数。返回拒因文案；None=放行。"""
    for toks in _segments(cmd):
        rule = _RULES.get(_binname(toks))
        if rule is None:
            continue
        low = [_clean(t).lower() for t in toks]
        reason = rule(low)
        if reason:
            return reason
    return None
