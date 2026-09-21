"""原始 HTTP 报文解析/渲染（F6-v3）：重放表单与拦截改包共用的纯函数层。

口径（DESIGN.md §7 F6-v3）：
- **解析放后端**——前端只贴/改报文文本，零解析；
- 相对路径 URL 用 ``base_url``（原请求 scheme+host）拼绝对；
- 二进制 body 不可编辑：渲染时以 ``<binary:N bytes>`` 占位（editable=False），
  解析出的 body 恒为文本；
- 同名头（如 Cookie）合并为 ``, `` 连接——重放/改包场景可接受的有损归一。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "parse_raw_request", "parse_raw_response",
    "render_raw_request", "render_raw_response", "BINARY_PLACEHOLDER_RE",
]

import re

# 渲染占位 <binary:N bytes>——decide 时据此拒绝编辑（editable=False 的硬标记）
BINARY_PLACEHOLDER_RE = re.compile(r"^<binary:\d+ bytes>$")

_MAX_RAW_CHARS = 512_000  # 渲染报文文本上限（防超大 body 撑爆 WS/前端）


def _join_url(url: str, base_url: str) -> str:
    """绝对 URL 直接用；相对写法用 base_url 拼绝对：
    "/path" → scheme+host+path；"host[:port][/path]"（无前导 /）→ 换 authority；
    其余按路径处理。"""
    url = url.strip()
    if not url:
        raise ValueError("请求行缺少 URL")
    if "://" in url:
        return url
    if not base_url:
        raise ValueError(f"相对路径 URL 需要 base_url 兜底: {url}")
    b = urlsplit(base_url)
    if url.startswith("/"):
        return urlunsplit((b.scheme, b.netloc, url, "", ""))
    head = url.split("/", 1)[0]
    if ("." in head or ":" in head) and "@" not in head and " " not in head:
        return urlunsplit((b.scheme, url, "/", "", ""))  # 裸 authority
    return urlunsplit((b.scheme, b.netloc, "/" + url, "", ""))


def _parse_head_and_body(text: str) -> tuple[list[str], str | None]:
    """报文文本 → (头部行列表, body 文本|None)。容忍 \\r\\n 与 \\n。"""
    normalized = text.replace("\r\n", "\n")
    if "\n\n" in normalized:
        head, body = normalized.split("\n\n", 1)
        return [ln for ln in head.split("\n") if ln.strip()], body
    return [ln for ln in normalized.split("\n") if ln.strip()], None


def _parse_headers(lines: list[str]) -> dict:
    headers: dict = {}
    for ln in lines:
        if ":" not in ln:
            raise ValueError(f"非法头行（缺冒号）: {ln[:60]}")
        k, v = ln.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k:
            raise ValueError("非法头行（空键）")
        headers[k] = f"{headers[k]}, {v}" if k in headers else v
    return headers


def parse_raw_request(text: str, *, base_url: str = "") -> dict:
    """原始请求报文 → {method, url(绝对), headers, body, is_binary}。

    坏请求行/非法头 ValueError → API 层 422。
    """
    lines, body = _parse_head_and_body(text)
    if not lines:
        raise ValueError("空报文：缺少请求行")
    parts = lines[0].split()
    if len(parts) < 2:
        raise ValueError(f"非法请求行: {lines[0][:60]}")
    method, url = parts[0].upper(), _join_url(parts[1], base_url)
    headers = _parse_headers(lines[1:])
    return {"method": method, "url": url, "headers": headers,
            "body": body, "is_binary": False}


def parse_raw_response(text: str) -> dict:
    """原始响应报文 → {status, headers, body, is_binary}。"""
    lines, body = _parse_head_and_body(text)
    if not lines:
        raise ValueError("空报文：缺少状态行")
    parts = lines[0].split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"非法状态行: {lines[0][:60]}")
    headers = _parse_headers(lines[1:])
    return {"status": int(parts[1]), "headers": headers,
            "body": body, "is_binary": False}


def _decode_or_placeholder(body: bytes | None) -> tuple[str | None, bool]:
    """body bytes → (展示文本|占位, editable)。utf-8 可解码视为文本可编辑。"""
    if body is None:
        return None, True
    try:
        return body.decode("utf-8"), True
    except UnicodeDecodeError:
        return f"<binary:{len(body)} bytes>", False


def render_raw_request(method: str, url: str, headers: dict,
                       body: bytes | None) -> tuple[str, bool]:
    """结构化请求 → (完整报文文本, editable)。二进制 body 占位不可编辑。"""
    body_text, editable = _decode_or_placeholder(body)
    lines = [f"{method} {url} HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    raw = "\n".join(lines)
    if body_text:
        raw += "\n\n" + body_text
    return raw[:_MAX_RAW_CHARS], editable


def render_raw_response(status: int, headers: dict, body: bytes | None,
                        mime: str = "") -> tuple[str, bool]:
    """结构化响应 → (完整报文文本, editable)。"""
    body_text, editable = _decode_or_placeholder(body)
    headers = dict(headers or {})
    if mime and "content-type" not in {k.lower() for k in headers}:
        headers["content-type"] = mime
    lines = [f"HTTP/1.1 {status}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    raw = "\n".join(lines)
    if body_text:
        raw += "\n\n" + body_text
    return raw[:_MAX_RAW_CHARS], editable
