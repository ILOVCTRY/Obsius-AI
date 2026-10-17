#!/usr/bin/env python
"""核对出网请求体转储（llm-request-dump）。

配套 ``core/llm/dump.py``：先设 ``CYBERSTRIKE_LLM_DUMP_DIR=<目录>`` 跑一轮真实
对话/任务，再用本脚本查看「服务端到底收到了什么」。

用法::

    python scripts/llm_dump_inspect.py <目录|文件> [--limit 5] [--json]
    python scripts/llm_dump_inspect.py dumps/ --limit 1 --show-system

输出每份请求的：协议/模型/字节数、消息角色序列（含每条字数）、system 是否在首位、
system 里命中哪些规则标记（红线/评级硬指令/rating 条款/owner 叠加）、工具数量、
tool 结果条数（即「上一轮工具输出有没有真的带上去」）。

**注意**：只读，不改任何转储文件。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# 规则标记：用来确认「红线 / 评级口径」这类本地拼接的固定串是否真进了请求体
MARKERS = {
    "红线": "安全红线",
    "规则前言": "场景规则与红线",
    "评级硬指令": "评级硬指令",
    "rating 条款": "rule:rating:",
    "owner 叠加": "rule:owner:",
    "track 规则": "rule:track:",
}

# system 兼容通道边界（与 core/llm/openai_compat.py 同源）
SYSTEM_IN_USER_BEGIN = "<<<CYBERSTRIKE-SYSTEM-PROMPT-BEGIN>>>"
SYSTEM_IN_USER_END = "<<<CYBERSTRIKE-SYSTEM-PROMPT-END>>>"


def _files(target: str) -> list[str]:
    if os.path.isdir(target):
        return sorted(glob.glob(os.path.join(target, "*.json")))
    return [target] if os.path.exists(target) else []


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(str(b.get("text") or b.get("content") or ""))
            else:
                out.append(str(b))
        return "".join(out)
    return "" if content is None else str(content)


def _analyze(rec: dict) -> dict:
    req = rec.get("request") or {}
    out: dict = {
        "ts": rec.get("ts"),
        "provider": rec.get("provider"),
        "format": rec.get("format"),
        "model": rec.get("model"),
        "url": rec.get("url"),
        "bytes": rec.get("bytes"),
        "sha256": (rec.get("sha256") or "")[:12],
        "truncated": rec.get("truncated"),
    }
    if rec.get("truncated"):
        return out

    # 三种形态：openai chat（messages 内含 system）/ openai responses（instructions +
    # input）/ anthropic（顶层 system + messages）
    system = ""
    if isinstance(req.get("system"), list):
        system = "\n".join(_text_of(b) if isinstance(b, dict) else str(b)
                           for b in req["system"])
    elif isinstance(req.get("system"), str):
        system = req["system"]
    if isinstance(req.get("instructions"), str):
        system = req["instructions"]

    msgs = req.get("messages")
    if msgs is None and isinstance(req.get("input"), list):
        msgs = req["input"]
    msgs = msgs or []

    roles = []
    tool_results = 0
    tool_calls = 0
    for m in msgs:
        if not isinstance(m, dict):
            roles.append(("?", 0))
            continue
        role = m.get("role") or ("tool" if _is_tool_result(m) else "?")
        if role == "system" and not system:
            system = _text_of(m.get("content"))
        if role == "tool" or _is_tool_result(m):
            tool_results += 1
        if m.get("tool_calls"):
            tool_calls += len(m["tool_calls"])
        roles.append((role, len(_text_of(m.get("content")))))

    tools = req.get("tools") or []
    names = []
    for t in tools:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            names.append(str(fn.get("name") or ""))

    # system 兼容通道（2026-10-17）：网关丢 system 字段时 system 前置于首条 user
    # 消息（带边界标记）——标准字段都取不到时从标记里取回，否则 markers 全空会误判。
    system_in_user = False
    if not system:
        for m in msgs:
            if not (isinstance(m, dict) and m.get("role") == "user"):
                continue
            c = m.get("content")
            if not isinstance(c, str) or not c.startswith(SYSTEM_IN_USER_BEGIN):
                continue
            end = c.find(SYSTEM_IN_USER_END)
            system = c[len(SYSTEM_IN_USER_BEGIN):end if end >= 0 else None].strip("\n")
            system_in_user = True
            break

    out.update({
        "system_chars": len(system),
        "system_first": bool(msgs) and isinstance(msgs[0], dict)
                        and (msgs[0].get("role") == "system"
                             or (system_in_user and msgs[0].get("role") == "user")),
        "system_in_user": system_in_user,
        "markers": [k for k, needle in MARKERS.items() if needle in system],
        "messages": len(msgs),
        "roles": roles,
        "tools": len(tools),
        "tool_names": [n for n in names if n],
        "tool_results": tool_results,
        "tool_calls": tool_calls,
        "system": system,
    })
    return out


def _is_tool_result(m: dict) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _print(info: dict, show_system: bool) -> None:
    print("=" * 72)
    print(f"[{info['ts']}] {info['provider']} / {info['format']} / {info['model']}")
    print(f"  url      : {info['url']}")
    print(f"  payload  : {info['bytes']} bytes  sha256:{info['sha256']}")
    if info.get("truncated"):
        print("  ** 超限，只落元信息（未存 request 正文）**")
        return
    print(f"  system   : {info['system_chars']} chars  "
          f"{'位于 messages[0]' if info['system_first'] else '不在 messages[0]（独立字段）'}"
          f"{'（user-content 兼容通道）' if info.get('system_in_user') else ''}")
    print(f"  规则命中 : {'、'.join(info['markers']) or '（无）'}")
    print(f"  消息     : {info['messages']} 条  "
          f"tool 结果 {info['tool_results']} 条 / tool 调用 {info['tool_calls']} 次")
    seq = " → ".join(f"{r}({n})" for r, n in info["roles"])
    print(f"  角色序列 : {seq}")
    print(f"  工具     : {info['tools']} 个")
    if info["tool_names"]:
        print(f"             {', '.join(info['tool_names'][:12])}"
              + (" …" if len(info["tool_names"]) > 12 else ""))
    if show_system and info.get("system"):
        print("-" * 72)
        print(info["system"])
        print("-" * 72)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核对出网请求体转储")
    ap.add_argument("target", help="转储目录或单个 .json 文件")
    ap.add_argument("--limit", type=int, default=5, help="只看最近 N 份（默认 5）")
    ap.add_argument("--json", action="store_true", help="输出机器可读摘要")
    ap.add_argument("--show-system", action="store_true", help="打印 system 全文")
    args = ap.parse_args(argv)

    files = _files(args.target)
    if not files:
        print(f"没有找到转储文件：{args.target}", file=sys.stderr)
        print("提示：先设 CYBERSTRIKE_LLM_DUMP_DIR=<目录> 再跑一轮对话/任务。",
              file=sys.stderr)
        return 1
    files = files[-args.limit:] if args.limit > 0 else files

    infos = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception as exc:  # noqa: BLE001 —— 坏文件跳过，不中断
            print(f"[skip] {path}: {exc}", file=sys.stderr)
            continue
        infos.append(_analyze(rec))

    if args.json:
        for info in infos:
            info.pop("system", None)
            info["roles"] = [f"{r}:{n}" for r, n in info.get("roles", [])]
        json.dump(infos, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    for info in infos:
        _print(info, args.show_system)
    print("=" * 72)
    print(f"共 {len(infos)} 份。system 首条 + tool 结果条数 = 你关心的「每轮真实内容」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
