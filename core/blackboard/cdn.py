"""CDN/共享托管判定（asset-tree-derived-clean M1，2026-09-24，DESIGN §5.2）。

共享 CDN IP 上的域名彼此无关，并入同一 IP 树会让「一个站测干净」的结论
错绑到整树，因此 domain 登记解析命中 CDN 时保持根行、不挂 host。

判定信号（任一命中即 CDN）：
1. 资产 meta.cdn 人工显式覆盖（True/False，最高优先）；
2. CNAME 后缀命中（``cname`` 由调用方解析后传入，本模块不做 DNS）；
3. IP 命中清单：``packs/data/cdn_ranges.json`` 随包基线 +
   ``config/cdn.json`` 同结构用户增补；坏文件 ValueError fail-fast（doctor 体检）。

拿不准默认「非 CDN」——私有单 IP 不并入只是保守，真 CDN 误并才会错绑结论。
本模块零 bb 依赖，纯逻辑 + 文件读，可独立单测。
"""

from __future__ import annotations

import ipaddress
import json
import threading
from pathlib import Path

__all__ = ["CdnLists", "load_cdn_lists", "is_cdn", "default_cdn_lists",
           "ProjectRoot"]

# 项目根（core/blackboard/cdn.py → 上两级；PyInstaller 下 __file__ 落包内目录，
# packs 资源随包故仍成立；config 增补在 onedir 外，打包形态不加载）
ProjectRoot = Path(__file__).resolve().parents[2]

_BASELINE = ProjectRoot / "packs" / "data" / "cdn_ranges.json"
_CONFIG = ProjectRoot / "config" / "cdn.json"


class CdnLists:
    """合并后的 CDN 清单：CIDR 网络对象 + CNAME 后缀（小写、无前导点）。"""

    def __init__(self, cidrs: list[str], suffixes: list[str]):
        self.networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for c in cidrs:
            c = str(c).strip()
            if c:
                self.networks.append(ipaddress.ip_network(c, strict=False))
        self.suffixes = [str(s).strip().lower().lstrip(".")
                         for s in suffixes if str(s).strip()]

    def ip_hit(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return False
        return any(addr in net for net in self.networks)

    def cname_hit(self, cname: str | None) -> bool:
        if not cname:
            return False
        c = cname.strip().lower().rstrip(".")
        if not c:
            return False
        return any(c == s or c.endswith("." + s) for s in self.suffixes)


def _read_lists_file(path: Path) -> dict:
    """读清单 JSON：缺失返 {}，结构坏/JSON 坏抛 ValueError（fail-fast）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ValueError(f"CDN 清单读取失败 {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"CDN 清单 JSON 解析失败 {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"CDN 清单结构非法 {path}: 顶层必须是对象")
    return data


def _extract(data: dict) -> tuple[list[str], list[str]]:
    cidrs = data.get("cidr", [])
    suffixes = data.get("cname_suffixes", [])
    if not isinstance(cidrs, list) or not isinstance(suffixes, list):
        raise ValueError("CDN 清单结构非法: cidr / cname_suffixes 必须是数组")
    cidr_out, suffix_out = [], []
    for c in cidrs:
        # 非法 CIDR 直接 ValueError（坏清单 fail-fast，不静默跳过）
        ipaddress.ip_network(str(c).strip(), strict=False)
        cidr_out.append(str(c))
    for s in suffixes:
        if str(s).strip():
            suffix_out.append(str(s))
    return cidr_out, suffix_out


def load_cdn_lists(baseline: Path | str = _BASELINE,
                   config: Path | str | None = _CONFIG) -> CdnLists:
    """合并基线 + 用户增补；baseline 缺失（打包裁剪）只用 config，皆空返空清单。"""
    cidrs: list[str] = []
    suffixes: list[str] = []
    base = _read_lists_file(Path(baseline))
    if base:
        c, s = _extract(base)
        cidrs.extend(c)
        suffixes.extend(s)
    if config is not None:
        extra = _read_lists_file(Path(config))
        if extra:
            c, s = _extract(extra)
            cidrs.extend(c)
            suffixes.extend(s)
    return CdnLists(cidrs, suffixes)


_cache_lock = threading.Lock()
_cached: CdnLists | None = None


def default_cdn_lists() -> CdnLists:
    """进程内缓存的默认清单（register_asset 默认使用；测试读仓库文件不触网）。"""
    global _cached
    with _cache_lock:
        if _cached is None:
            _cached = load_cdn_lists()
        return _cached


def is_cdn(ip: str | None, domain: str | None = None, cname: str | None = None,
           *, meta: dict | None = None, cdn_lists: CdnLists | None = None) -> bool:
    """判定 (ip/domain/cname) 是否 CDN/共享托管。

    优先级：meta.cdn 人工覆盖（True/False） > CNAME 后缀 > IP CIDR；
    信号不足返 False（拿不准不并）。``meta`` 为资产行 meta（新行入参或存量行）。
    """
    meta = meta or {}
    manual = meta.get("cdn")
    if manual is not None:
        return bool(manual)
    lists = cdn_lists or default_cdn_lists()
    if lists.cname_hit(cname):
        return True
    if ip and lists.ip_hit(ip):
        return True
    return False
