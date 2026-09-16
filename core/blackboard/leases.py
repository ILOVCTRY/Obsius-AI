"""资源租约键归一化与冲突判定（B2 机制 1.4，DESIGN.md §6.7.1）。

设计要点：
- 键方案白名单，服务端归一化、不信任 LLM 原文：非法方案/空值/过宽键（如 `ip:*`）
  一律 ValueError（publish 422 / Agent 工具回填 [拒绝]）。
- 锁模式由任务噪声预算映射：passive → S 共享（被动分析可并行），active → X 独占。
- 冲突矩阵：X 与 X/S 同键冲突；S 只与 X 冲突（S+S 兼容）。
- 租约有效性 = JOIN tasks（持有任务 claimed 且 lease_until 未过期）判定，
  免与任务心跳双写；任务收尾/删除/租约过期时释放行。
- 运行期动态锁申请后置（发布期/认领期门控为主，2026-09-16 定稿）。
"""

import ipaddress
import re
from urllib.parse import urlsplit

from core.blackboard.store import now

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_USER_NS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def normalize_key(raw: str) -> str:
    """归一化单条资源键；非法/过宽抛 ValueError。

    方案白名单：ip: / host: / domain: / url: / binary:<sha256> /
    func:<sha256>:<hex 地址> / tool:<name>[:<arg>] / user:<ns>:<v>。
    """
    if not isinstance(raw, str):
        raise ValueError(f"资源键必须是字符串: {raw!r}")
    key = raw.strip()
    if not key or "*" in key or any(ch.isspace() for ch in key):
        raise ValueError(f"非法资源键（空/含通配或空白）: {raw!r}")
    scheme, sep, value = key.partition(":")
    if not sep:
        raise ValueError(f"资源键缺少方案前缀（如 ip:1.2.3.4）: {raw!r}")
    scheme, value = scheme.strip().lower(), value.strip()
    if not value:
        raise ValueError(f"资源键值为空: {raw!r}")
    try:
        if scheme == "ip":
            return f"ip:{ipaddress.ip_address(value)}"
        if scheme == "host":
            return f"host:{value.encode('idna').decode('ascii').lower()}"
        if scheme == "domain":
            return f"domain:{value.rstrip('.').lower()}"
        if scheme == "url":
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                raise ValueError("url: 须为 http(s) 完整地址")
            path = parts.path.rstrip("/") or "/"
            return f"url:{parts.hostname.lower()}{path}"
        if scheme == "binary":
            v = value.lower()
            if not _HEX64_RE.match(v):
                raise ValueError("binary: 须为 64 位十六进制 sha256")
            return f"binary:{v}"
        if scheme == "func":
            sha, _, addr = value.partition(":")
            sha, addr = sha.lower(), addr.lower().removeprefix("0x")
            if not _HEX64_RE.match(sha) or not re.fullmatch(r"[0-9a-f]+", addr or ""):
                raise ValueError("func: 须为 <sha256>:<hex 地址>")
            return f"func:{sha}:{addr}"
        if scheme == "tool":
            name, _, arg = value.partition(":")
            if not _TOOL_NAME_RE.match(name):
                raise ValueError("tool: 名称须为小写字母/数字/连字符/下划线")
            if arg and any(ch.isspace() for ch in arg):
                raise ValueError("tool: 参数不得含空白")
            return f"tool:{name}:{arg}" if arg else f"tool:{name}"
        if scheme == "user":
            ns, _, v = value.partition(":")
            if not _USER_NS_RE.match(ns) or not v:
                raise ValueError("user: 须为 <受限命名空间>:<值>")
            return f"user:{ns}:{v}"
    except ValueError as e:
        if str(e).startswith(("url:", "binary:", "func:", "tool:", "user:")):
            raise
        raise ValueError(f"资源键 {raw!r} 归一化失败: {e}") from e
    raise ValueError(f"未知资源键方案: {scheme!r}（白名单 ip/host/domain/url/binary/func/tool/user）")


def normalize_keys(keys: list[str] | None) -> list[str]:
    """批量归一化 + 去重排序；非法抛 ValueError（消息带原键，便于回填）。"""
    out: set[str] = set()
    for k in (keys or []):
        out.add(normalize_key(k))
    return sorted(out)


def mode_for(noise_budget: str) -> str:
    """噪声预算 → 锁模式：passive=S 共享（被动可并行），active=X 独占。"""
    return "S" if noise_budget == "passive" else "X"


def keys_conflict(mode_a: str, mode_b: str) -> bool:
    """X 与任何非 advisory 同键冲突；S 只与 X 冲突。"""
    return "X" in (mode_a.upper(), mode_b.upper())


_ACTIVE_LEASE_SQL = (
    " FROM resource_leases rl JOIN tasks t ON t.id = rl.task_id"
    " WHERE rl.project_id = ? AND t.status = 'claimed'"
    " AND t.lease_until IS NOT NULL AND t.lease_until > ?"
)


def active_leases_for_keys(bb, project_id: str, keys: list[str],
                           exclude_task_id: str | None = None) -> list[dict]:
    """查询给定键上仍有效的租约行（持有任务 claimed 且租约未过期）。"""
    if not keys:
        return []
    sql = (
        "SELECT rl.project_id, rl.resource_key, rl.mode, rl.task_id, rl.session_id"
        + _ACTIVE_LEASE_SQL
        + f" AND rl.resource_key IN ({','.join('?' * len(keys))})"
    )
    params: list = [project_id, now(), *keys]
    if exclude_task_id:
        sql += " AND rl.task_id != ?"
        params.append(exclude_task_id)
    return [dict(r) for r in bb.conn.execute(sql, params).fetchall()]


def held_keys_by_task(bb, project_id: str) -> dict[str, set[str]]:
    """全部有效租约 task_id → 持有键集合（wait-for 图与释放重校验用）。"""
    sql = ("SELECT rl.resource_key, rl.task_id" + _ACTIVE_LEASE_SQL)
    out: dict[str, set[str]] = {}
    for r in bb.conn.execute(sql, (project_id, now())).fetchall():
        out.setdefault(r["task_id"], set()).add(r["resource_key"])
    return out
