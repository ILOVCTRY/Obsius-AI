"""目标白名单校验（F6，DESIGN.md §7）：导航/重发/爆破目标必须命中项目资产表。

宁严勿松红线：未登记目标一律拒绝，不做 DNS 解析（导航前零网络副作用——
登记时的 DNS 挂载在 core/blackboard/assets.py，两处互补）。

匹配语义（host 归一化后，小写、去端口）：
- 目标 host 是 IP：host 资产 value 精确相等；
- 目标 host 是域名：domain 资产精确相等，或是某 domain 资产的子域
  （逐级取父判后缀，≤2 级；config.browser.domain_scope="exact" 可收紧为只认精确）；
- url 资产的 hostname 部相等；
- 命中以上任一即放行，否则 allowed=False（调用方回填/返回登记指引）。

本模块零 playwright 依赖，可独立单测。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse

__all__ = [
    "ParsedTarget", "TargetVerdict", "normalize_target", "check_target",
    "deny_event",
]

# domain 后缀匹配的最大层数（x.y.z.com 命中 z.com 最多逐级取 2 次父）
_SUBDOMAIN_MAX_STEPS = 2


@dataclass
class ParsedTarget:
    scheme: str
    host: str          # 小写、去端口、IPv6 去方括号
    port: int | None
    path: str
    is_ip: bool


@dataclass
class TargetVerdict:
    allowed: bool
    host: str          # 归一化主机部（拒绝时也填，供前端「一键登记资产」）
    is_ip: bool
    reason: str = ""   # 拒绝原因（中文，给 LLM 回填 / 前端 toast）
    asset_id: str | None = None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def normalize_target(url_or_host: str) -> ParsedTarget:
    """url 或裸 host 归一化：无 scheme 补 http://；IPv6 方括号剥离；host 小写。"""
    raw = (url_or_host or "").strip()
    if "://" not in raw:
        raw = "http://" + raw
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    return ParsedTarget(scheme=u.scheme or "http", host=host,
                        port=u.port, path=u.path or "/", is_ip=_is_ip(host))


def check_target(bb, project_id: str, url_or_host: str,
                 *, domain_scope: str = "subdomain") -> TargetVerdict:
    """资产白名单校验。bb 只用 find_asset / list_assets 读接口。

    domain_scope: "subdomain"（默认，子域随主域放行）| "exact"（只认精确）。
    """
    t = normalize_target(url_or_host)
    if not t.host:
        return TargetVerdict(False, "", t.is_ip, reason="无法解析目标主机")

    # 1) host(IP) 资产精确相等
    hit = bb.find_asset(project_id, "host", t.host)
    if hit is not None:
        return TargetVerdict(True, t.host, t.is_ip, asset_id=hit["id"])

    # 2) domain 资产：精确 → 子域（逐级取父判后缀）
    if not t.is_ip:
        hit = bb.find_asset(project_id, "domain", t.host)
        if hit is not None:
            return TargetVerdict(True, t.host, t.is_ip, asset_id=hit["id"])
        if domain_scope != "exact":
            # 逐级从左剥标签：api.target.example.com → target.example.com →
            # example.com（≤2 层）；剩单标签即停（不匹配顶级域）
            labels = t.host.split(".")
            for i in range(1, _SUBDOMAIN_MAX_STEPS + 1):
                if len(labels) - i < 2:
                    break  # 剩余不足两个标签（顶级域）不再匹配
                parent = ".".join(labels[i:])
                hit = bb.find_asset(project_id, "domain", parent)
                if hit is not None:
                    return TargetVerdict(True, t.host, t.is_ip, asset_id=hit["id"])

    # 3) url 资产 hostname 部相等（url 资产可能未登记 host/domain 父链）
    for a in bb.list_assets(project_id, type_="url"):
        u = urlparse(a.get("value") or "")
        if (u.hostname or "").lower() == t.host:
            return TargetVerdict(True, t.host, t.is_ip, asset_id=a["id"])

    return TargetVerdict(
        False, t.host, t.is_ip,
        reason=(f"目标 {t.host} 未登记：项目资产表中无 host/domain/url 资产命中。"
                "请先登记资产（bb_add_asset / POST /assets）后重试。"))


def deny_event(bb, project_id: str, url: str, verdict: TargetVerdict, *,
               session_id: str | None, author: str, origin: str) -> None:
    """拒绝动作落 browser.deny 审计（origin: agent|human）。"""
    bb.append_event(
        project_id, "browser.deny",
        {"url": url, "host": verdict.host, "origin": origin,
         "reason": verdict.reason},
        session_id=session_id, author=author)
