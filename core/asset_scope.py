"""资产范围（asset scope）解析与校验——渗透测试与情报联动的范围闸。

**要解决的问题**：`intel_assets` 拿到今天情报里的漏洞指纹后，`fofa_search` 要去圈同类资产。
「圈哪些」必须有机器可读的判据——`packs/kb/安全红线/平台规则/<tag>.md` 是人读散文（授权条款、
无害化边界），把它的文字用正则抠出来当查询条件，改一次文案就静默失效。

**分层**（与既有模块的边界）：

- `packs/kb/安全红线/资产范围/<tag>.md` — 唯一机器可读源（格式见同目录 README）。
- `core/asset_scope.py`（本模块） — 解析 + 匹配 + 查询拼装，纯函数、零 IO 副作用（只读文件）。
- `core/scope_policy.py` — **另一个东西**：项目级授权范围（`projects.config.scope`，人工登记
  的域名/IP/CIDR），管「这个项目允许打哪些目标」。本模块管「这个平台收录哪些资产」。
  两者不可互相替代：前者是授权，后者是选战场。资产最终仍要过 `check_project_scope`。

**为什么域名行与 fofa 行分开**：`org="China Education and Research Network Center"` 能圈出
大量教育资产，但也会带出 org 字段误标/代管主机；域名后缀能精确判定但圈不全（教育资产不一定
以 edu.cn 结尾）。两段串起来用：**先圈后筛**，各自都不足但合起来够用。

**容错口径**：文件缺失/格式坏行 → 跳过该行，绝不抛（资产范围是增强，不该阻断工具）。
全部条目为空 → `AssetScope.empty` 为真，调用方应拒绝「按平台范围圈资产」的请求而不是
静默放开（**宁严勿松**：范围文件丢了就当没有范围，不能当成「范围无限」）。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ASSET_SCOPE_DIR", "AssetScope", "parse_asset_scope", "load_asset_scope",
    "list_asset_scope_tags", "match_host", "build_fofa_query",
]

# 相对 packs_root 的目录（**不含 packs 本身**——packs_root 由调用方给）
ASSET_SCOPE_DIR = Path("kb") / "安全红线" / "资产范围"

# `fofa: <片段>` —— 查询侧约束。允许全角冒号（手写文档常见）。
_FOFA_RE = re.compile(r"^fofa\s*[:：]\s*(.+)$", re.I)
# `说明:` 行只给人看
_NOTE_RE = re.compile(r"^(说明|备注|note)\s*[:：]", re.I)
# 通配前缀 `*.` 剥掉后按后缀匹配；裸 `*` 视为无意义
_WILDCARD_PREFIX = "*."


@dataclass(frozen=True)
class AssetScope:
    """一个平台的资产范围。

    domains: 域名后缀（已小写、已剥 `*.`）——匹配时按 `host == d or host.endswith("." + d)`
    cidrs:   ipaddress 网段对象
    fofa:    追加到查询语句的片段（不含首尾空格，多个片段按 AND 串联）
    notes:   说明行原文（给人看，也用于工具回执里解释范围来源）
    tag:     平台标签（`edusrc` 等），回执里要标明
    """

    tag: str = ""
    domains: tuple[str, ...] = ()
    cidrs: tuple = ()
    fofa: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    source: str = ""

    @property
    def empty(self) -> bool:
        """没有任何可用于圈定/筛选的条目。"""
        return not (self.domains or self.cidrs or self.fofa)

    @property
    def has_filter(self) -> bool:
        """有结果侧判据（域名/IP）——没有它就只能靠 fofa 片段圈，无法剔除误命中。"""
        return bool(self.domains or self.cidrs)


def _strip_line(raw: str) -> str:
    """去行内注释与首尾空白。`#` 之后视为注释（FOFA 语法里 `#` 不是合法字符）。"""
    line = str(raw or "").strip()
    if not line:
        return ""
    # 只在 `#` 前面是空白或行首时才当注释——域名里不会出现 `#`，
    # 但保守处理避免把 `a#b` 这类脏值切断后误判成合法域名
    idx = line.find("#")
    if idx >= 0:
        line = line[:idx].rstrip()
    return line


def _classify(value: str) -> tuple[str, object] | None:
    """把一行值归类成 ("domain"|"cidr", 归一化值)；无法识别返回 None。

    判据顺序（**先 CIDR 后域名**）：含 `/` 或能 `ip_address` 解析 → CIDR/IP；
    含 `.` 且不含空白 → 域名。裸 `*` / 空 / 含空白的多词文本一律丢弃。
    """
    v = value.strip().lower()
    if not v or v == "*" or any(c.isspace() for c in v):
        return None
    if "/" in v:
        try:
            return ("cidr", ipaddress.ip_network(v, strict=False))
        except ValueError:
            return None
    try:
        return ("cidr", ipaddress.ip_network(ipaddress.ip_address(v)))
    except ValueError:
        pass
    if "." in v:
        # 剥通配前缀；`*.*.x` 这类多级通配不在支持范围（会退化成后缀匹配，语义仍正确）
        while v.startswith(_WILDCARD_PREFIX):
            v = v[len(_WILDCARD_PREFIX):]
        if v and "." in v:
            return ("domain", v)
    return None


def parse_asset_scope(text: str, tag: str = "", source: str = "") -> AssetScope:
    """解析资产范围文件正文。坏行静默跳过（不抛——见模块 docstring 的容错口径）。"""
    domains: list[str] = []
    cidrs: list = []
    fofa: list[str] = []
    notes: list[str] = []
    for raw in str(text or "").splitlines():
        line = _strip_line(raw)
        if not line:
            continue
        m = _FOFA_RE.match(line)
        if m:
            frag = m.group(1).strip()
            if frag and frag not in fofa:
                fofa.append(frag)
            continue
        if _NOTE_RE.match(line):
            note = line.split(":", 1)[-1].split("：", 1)[-1].strip()
            if note:
                notes.append(note)
            continue
        if line.startswith("#"):
            continue
        kind = _classify(line)
        if kind is None:
            continue
        k, v = kind
        if k == "domain":
            if v not in domains:
                domains.append(v)  # type: ignore[arg-type]
        elif v not in cidrs:
            cidrs.append(v)
    return AssetScope(tag=tag, domains=tuple(domains), cidrs=tuple(cidrs),
                      fofa=tuple(fofa), notes=tuple(notes), source=source)


def asset_scope_dir(packs_root: str | Path) -> Path:
    return Path(packs_root) / ASSET_SCOPE_DIR


def list_asset_scope_tags(packs_root: str | Path) -> list[str]:
    """枚举有资产范围文件的平台标签（`README.md` 不算）。"""
    d = asset_scope_dir(packs_root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.md") if p.stem.lower() != "readme")


def load_asset_scope(packs_root: str | Path, tag: str) -> AssetScope:
    """读某平台的资产范围。文件不存在 → 空 scope（`empty` 为真，调用方据此拒绝）。"""
    tag = str(tag or "").strip()
    if not tag:
        return AssetScope()
    # 防路径穿越：tag 只允许字母数字与 -_.
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", tag):
        return AssetScope(tag=tag)
    f = asset_scope_dir(packs_root) / f"{tag}.md"
    try:
        text = f.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return AssetScope(tag=tag, source=str(f))
    return parse_asset_scope(text, tag=tag, source=str(f))


def match_host(scope: AssetScope, value: str) -> bool:
    """目标是否落在范围内的**结果侧判据**内（域名后缀 / 网段）。

    - 无任何结果侧判据（`has_filter` 为假）→ 返回 True（调用方已靠 fofa 片段圈定，
      没有判据时不能凭空拒绝）；
    - 值是 IP → 只按 cidrs 判；值是域名 → 只按 domains 判（**不反查 DNS**：本模块
      纯函数无网络，且 DNS 结果会随时间漂移）；
    - 值带 scheme / 端口 → 先剥掉再判（FOFA 的 host 字段常是 `https://x` / `x:8080`）。
    """
    if not scope.has_filter:
        return True
    host = _host_of(value)
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return any(ip in net for net in scope.cidrs)
    if not scope.domains:
        return False
    return any(host == d or host.endswith("." + d) for d in scope.domains)


def _host_of(value: str) -> str:
    """从 `https://a.b:8443/x` / `a.b:8443` / `a.b` 取出小写主机名。"""
    v = str(value or "").strip()
    if not v:
        return ""
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):                      # IPv6 字面量 [::1]:80
        v = v[1:].split("]", 1)[0]
    elif v.count(":") == 1:                    # host:port（IPv6 裸写有多个冒号，不动）
        v = v.split(":", 1)[0]
    return v.strip().lower().rstrip(".")


def build_fofa_query(scope: AssetScope, base: str = "") -> str:
    """把「基础语句 + 范围片段」拼成最终 FOFA 查询。

    `base` 是调用方给的指纹/语法（如 `product="致远OA"` 或 `app="xxx"`）；范围片段
    用 `&&` 串在后面。**不在这里加括号**——FOFA 的 `&&` 优先级与直觉一致，加括号反而
    在部分中转实现里不被支持。片段之间也按 `&&` 连。
    """
    parts: list[str] = []
    b = str(base or "").strip()
    if b:
        parts.append(b)
    for frag in scope.fofa:
        f = str(frag or "").strip()
        if f and f not in parts:
            parts.append(f)
    return " && ".join(parts)
