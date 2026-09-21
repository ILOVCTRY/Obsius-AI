"""统一资产登记入口（E6，DESIGN.md §5.2）。

人工（POST /assets）与 Agent（bb_add_asset）共用同一登记路径：
类型自动识别 → host 按 IP / domain 按完整串去重 → 既有合并/新行落库 →
domain 自动 DNS 解析挂 host（解析失败独立成行）→ 主域名/别名标记。

本模块只经传入的 Blackboard 方法读写，不直接握连接。
"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

__all__ = ["detect_type", "register_asset", "resolve_ipv4"]

_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")


def resolve_ipv4(domain: str) -> str | None:
    """域名 → 首个 IPv4（getaddrinfo）；解析失败返回 None（存独立行，不猜）。"""
    try:
        infos = socket.getaddrinfo(domain.strip(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        return info[4][0]
    return None


def detect_type(value: str) -> str | None:
    """按值识别资产类型（E6 ②）：url / IPv4(host) / host:port(service) /
    完整域名(domain) / 64hex(binary)；识别不出返回 None（调用方拦截提示手选）。"""
    v = value.strip()
    if not v or len(v) > 2048:
        return None
    if "://" in v:
        try:
            u = urlparse(v)
        except ValueError:
            return None
        return "url" if u.hostname else None
    if "/" in v or " " in v or "@" in v or "?" in v or "#" in v:
        return None
    try:
        ipaddress.ip_address(v)
        return "host"
    except ValueError:
        pass
    if len(v) == 64 and all(c in "0123456789abcdefABCDEF" for c in v):
        return "binary"
    if ":" in v:
        host, _, port = v.rpartition(":")
        if host and ":" not in host and port.isdigit():
            return "service"
        return None
    if "." in v and len(v) <= 253 and all(_LABEL_RE.match(l) for l in v.split(".")):
        return "domain"
    return None


def _ensure_host(bb, project_id: str, ip: str, author: str,
                 session_id: str | None = None) -> tuple[str, bool]:
    """按 IP 去重建/复用 host 资产，返回 (host_id, 已存在)。"""
    existing = bb.find_asset(project_id, "host", ip)
    if existing is not None:
        return existing["id"], True
    r = bb.upsert_asset(project_id, "host", ip, author=author)
    if r["created"]:
        bb.append_event(project_id, "asset.new",
                        {"asset_id": r["id"], "type": "host", "value": ip,
                         "parent_id": None},
                        session_id=session_id, author=author)
    return r["id"], False


def register_asset(bb, project_id: str, value: str, type_: str = "auto",
                   parent_id: str | None = None, meta: dict | None = None,
                   author: str = "human", session_id: str | None = None) -> dict:
    """登记资产（E6 ①统一入口）。返回：
    {id, created, type, value, parent_id, host_id?, host_existed?, dns?}

    - type 省略/``auto`` → detect_type；识别不出抛 ValueError（API 422 / 工具回填）。
    - 既有同值资产（host 按 IP、domain 按完整串）→ 合并路径：补挂 parent + meta
      merge，不插新行（修人工路径重复行）。
    - domain 且未显式给 parent → 自动 DNS 解析挂 host；同 IP 已有主域名时
      本域名标 meta.alias=true，首个域名写 host.meta.primary_domain。
    - url/service 主机部为域名时：不猜 DNS 也不造行，同名 domain 资产已存在
      才精确挂其下（存量孤儿行走 scripts/adopt_orphan_assets.py 补挂）。
    """
    value = value.strip()
    if not value:
        raise ValueError("资产值不能为空")
    if type_ in ("", "auto", None):
        type_ = detect_type(value)
        if type_ is None:
            raise ValueError(
                f"无法识别资产类型：{value[:80]}——请手选 host/domain/service/url/binary")
    meta = dict(meta or {})
    result: dict = {"id": None, "created": False, "type": type_, "value": value,
                    "parent_id": parent_id}
    host_id = None
    host_existed = False

    # 自动挂载（§5.2）：url/service 值里含 IP 主机部 → 建/复用 host 挂其下；
    # 域名主机部不猜 DNS，但同名 domain 资产已存在时精确挂其下
    if parent_id is None and type_ in ("url", "service"):
        host_part = None
        if type_ == "url":
            host_part = urlparse(value).hostname
        elif ":" in value:
            host_part = value.rsplit(":", 1)[0]
        if host_part:
            try:
                ipaddress.ip_address(host_part)
                host_id, host_existed = _ensure_host(bb, project_id, host_part,
                                                     author, session_id)
                parent_id = host_id
            except ValueError:
                d = bb.find_asset(project_id, "domain", host_part)
                if d is not None:
                    parent_id = d["id"]  # 精确匹配既有 domain；缺失保持独立行

    # domain 自动 DNS 挂载 + 主域名/别名标记（E6 ③⑤）
    dns_resolved = False
    if type_ == "domain" and parent_id is None:
        ip = resolve_ipv4(value)
        if ip:
            host_id, host_existed = _ensure_host(bb, project_id, ip, author, session_id)
            parent_id = host_id
            dns_resolved = True
            host_row = bb.get_asset(host_id)
            primary = (host_row or {}).get("meta", {}).get("primary_domain")
            if primary and primary != value:
                meta.setdefault("alias", True)
            elif not primary:
                bb.update_asset_meta(host_id, {"primary_domain": value})
    result["parent_id"] = parent_id
    if host_id is not None:
        result["host_id"] = host_id
        result["host_existed"] = host_existed
        result["dns"] = dns_resolved

    # 去重（E6 ④）：host 按 IP / domain 按完整串，命中即合并
    existing = bb.find_asset(project_id, type_, value)
    if existing is not None:
        if parent_id and not existing.get("parent_id"):
            try:
                bb.set_asset_parent(existing["id"], parent_id)
            except ValueError:
                pass  # 挂载被拒（环/父缺失）：保留原状
        if meta:
            bb.update_asset_meta(existing["id"], meta)
        result["id"] = existing["id"]
        return result

    r = bb.upsert_asset(project_id, type_, value, parent_id=parent_id,
                        meta=meta, author=author)
    if r["created"]:
        bb.append_event(project_id, "asset.new",
                        {"asset_id": r["id"], "type": type_, "value": value,
                         "parent_id": parent_id},
                        session_id=session_id, author=author)
    result["id"] = r["id"]
    result["created"] = r["created"]
    return result
