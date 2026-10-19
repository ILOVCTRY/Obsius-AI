"""项目授权范围策略（scope policy）。

策略存放在 ``projects.config["scope"]``。未配置 scope 时保持历史行为；配置后，
资产登记与网络目标检查必须命中登记的域名/IP/CIDR/URL 规则。

示例：
{
  "scope": {
    "domains": ["example.com"],
    "ips": ["203.0.113.10"],
    "cidrs": ["203.0.113.0/24"],
    "urls": ["https://example.com:8443"],
    "ports": [80, "443-445"],
    "allow_subdomains": true,
    "allow_site_expansion": true,
    "allow_port_expansion": true
  }
}
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ScopeVerdict:
    allowed: bool
    reason: str = ""
    host: str = ""
    port: int | None = None


def project_scope(bb, project_id: str) -> dict:
    """读取项目 scope；缺失或空对象表示未启用项目级 scope。"""
    project = bb.get_project(project_id) or {}
    config = project.get("config") or {}
    scope = config.get("scope") if isinstance(config, dict) else None
    return scope if isinstance(scope, dict) else {}


def scope_enabled(scope: dict | None) -> bool:
    if not isinstance(scope, dict):
        return False
    return any(scope.get(k) for k in ("domains", "ips", "cidrs", "urls", "hosts"))


def _host_port(value: str, default_port: int | None = None) -> tuple[str, int | None]:
    raw = str(value or "").strip()
    # service 资产形如 host:port；urlparse 对无 scheme 的 IPv6/主机端口不稳定，
    # 统一补 scheme 后解析。非法端口由 ValueError 转成无主机拒绝。
    if "://" not in raw and re.match(r"^[^/]+:\\d+$", raw):
        raw = "http://" + raw
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
        return (parsed.hostname or "").lower(), parsed.port or default_port
    except ValueError:
        return "", default_port


def _port_allowed(scope: dict, port: int | None) -> bool:
    allowed = scope.get("ports")
    if not allowed or port is None:
        return True
    for item in allowed:
        try:
            if isinstance(item, str) and "-" in item:
                lo, hi = (int(x.strip()) for x in item.split("-", 1))
                if lo <= port <= hi:
                    return True
            elif int(item) == port:
                return True
        except (TypeError, ValueError):
            continue
    return False


def check_scope(scope: dict | None, value: str, *, port: int | None = None) -> ScopeVerdict:
    """检查资产/网络目标是否处于项目授权范围内。"""
    if not scope_enabled(scope):
        return ScopeVerdict(True)
    scope = scope or {}
    allow_subdomains = bool(scope.get("allow_subdomains", scope.get("allow_site_expansion", True)))
    allow_port_expansion = bool(scope.get("allow_port_expansion", True))
    host, parsed_port = _host_port(value, port)
    port = parsed_port
    if not host:
        return ScopeVerdict(False, "无法解析授权范围目标", host=host, port=port)

    host_match = False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        for raw in [*(scope.get("ips") or []), *(scope.get("hosts") or [])]:
            try:
                if ip == ipaddress.ip_address(str(raw).strip()):
                    host_match = True
                    break
            except ValueError:
                continue
        if not host_match:
            for raw in scope.get("cidrs") or []:
                try:
                    if ip in ipaddress.ip_network(str(raw).strip(), strict=False):
                        host_match = True
                        break
                except ValueError:
                    continue
    else:
        for raw in [*(scope.get("domains") or []), *(scope.get("hosts") or [])]:
            rule = str(raw).strip().lower().rstrip(".")
            if not rule:
                continue
            if host == rule or (allow_subdomains and host.endswith("." + rule)):
                host_match = True
                break
        if not host_match:
            for raw in scope.get("urls") or []:
                rule_host, rule_port = _host_port(str(raw))
                if host == rule_host or (allow_subdomains and rule_host
                                         and host.endswith("." + rule_host)):
                    if rule_port is None or port is None or rule_port == port:
                        host_match = True
                        break
    if not host_match:
        return ScopeVerdict(False, f"目标 {host} 超出项目授权范围", host=host, port=port)
    if not allow_port_expansion and port is not None:
        explicit_ports = scope.get("ports") or []
        if explicit_ports and not _port_allowed(scope, port):
            return ScopeVerdict(False, f"目标端口 {port} 不在项目授权范围内", host=host, port=port)
    if not _port_allowed(scope, port):
        return ScopeVerdict(False, f"目标端口 {port} 不在项目授权范围内", host=host, port=port)
    return ScopeVerdict(True, host=host, port=port)


def check_project_scope(bb, project_id: str, value: str, *, port: int | None = None) -> ScopeVerdict:
    return check_scope(project_scope(bb, project_id), value, port=port)


def command_targets(cmd: str) -> list[str]:
    """提取命令中明确出现的 URL、IPv4/CIDR 主机和域名端点。

    这是执行门禁的保守辅助，不尝试解析 shell 变量或 DNS 结果；无法识别的
    动态目标交由网络审批和运行时策略处理。
    """
    pattern = re.compile(
        r"(?:https?://[^\s'\"<>]+|(?:(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?::\d+)?|(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?)"
    )
    out: list[str] = []
    for match in pattern.findall(str(cmd or "")):
        target = match.rstrip(".,;)]}")
        if target and target not in out:
            out.append(target)
    return out


def check_command_scope(bb, project_id: str, cmd: str) -> ScopeVerdict:
    scope = project_scope(bb, project_id)
    if not scope_enabled(scope):
        return ScopeVerdict(True)
    for target in command_targets(cmd):
        verdict = check_scope(scope, target)
        if not verdict.allowed:
            return ScopeVerdict(False, "命令目标超出项目授权范围：" + verdict.reason,
                                host=verdict.host, port=verdict.port)
    return ScopeVerdict(True)


def expand_project_scope(bb, project_id: str, value: str) -> dict:
    """将已批准的结构化目标并入项目 scope，返回更新后的 scope。

    仅供审批处理器调用；普通调用仍必须先经过人工审批。自由文本申请没有
    ``scope_value`` 时不会进入本函数，避免把自然语言误当配置。
    """
    project = bb.get_project(project_id) or {}
    config = dict(project.get("config") or {})
    scope = dict(config.get("scope") or {})
    raw = str(value or "").strip()
    host, port = _host_port(raw)
    if not host:
        raise ValueError("批准的 scope_value 无法解析目标")
    try:
        ipaddress.ip_address(host)
        key = "ips"
    except ValueError:
        key = "urls" if "://" in raw else "domains"
    values = list(scope.get(key) or [])
    item = raw if key == "urls" else host
    if item not in values:
        values.append(item)
    scope[key] = values
    if port is not None and scope.get("ports"):
        ports = list(scope.get("ports") or [])
        if port not in ports:
            ports.append(port)
        scope["ports"] = ports
    config["scope"] = scope
    bb.update_project_config(project_id, config)
    return scope
