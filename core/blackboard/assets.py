"""统一资产登记入口（E6，DESIGN.md §5.2）。

人工（POST /assets）与 Agent（bb_add_asset）共用同一登记路径：
类型自动识别 → host 按 IP / domain 按完整串去重 → 既有合并/新行落库 →
domain 自动 DNS 解析挂 host（解析失败独立成行）→ 主域名/别名标记。
批量导入（cyberspace-mapping M1，2026-09-23）：import_assets 走本模块
register_asset（quiet 静音逐行事件）+ DNS 批次内预热缓存。

本模块只经传入的 Blackboard 方法读写，不直接握连接。
"""

import ipaddress
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from core.blackboard.cdn import CdnLists, is_cdn
from core.blackboard.store import new_id  # 复用 <前缀>-<12hex> 约定

__all__ = ["detect_type", "register_asset", "resolve_ipv4", "import_assets",
           "warm_dns", "clear_dns_warm"]

_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")

# DNS 批次内预热缓存（仅 import_assets 填充/清空；批外 resolve 恒真实解析，
# 不做进程级缓存——负缓存（解析失败）长存会让 DNS 恢复后的域名永远挂不上）
_DNS_WARM: dict[str, str | None] = {}
_DNS_LOCK = threading.Lock()
_DNS_WORKERS = 16


def clear_dns_warm() -> None:
    with _DNS_LOCK:
        _DNS_WARM.clear()


def _resolve_nocache(domain: str) -> str | None:
    try:
        infos = socket.getaddrinfo(domain.strip(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        return info[4][0]
    return None


def warm_dns(domains: list[str], max_workers: int = _DNS_WORKERS) -> None:
    """并发预热 DNS（导入 264 行串行 resolve 最坏分钟级 → 线程池摊平）；
    结果只进批次内缓存，import_assets 结束即清。"""
    todo = []
    seen: set[str] = set()
    for d in domains:
        d = (d or "").strip().lower()
        if d and d not in seen and d not in _DNS_WARM:
            seen.add(d)
            todo.append(d)
    if not todo:
        return
    with _DNS_LOCK:
        _DNS_WARM.update({d: None for d in todo})  # 占位防重复解析
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for d, ip in zip(todo, pool.map(_resolve_nocache, todo)):
            with _DNS_LOCK:
                _DNS_WARM[d] = ip


def resolve_ipv4(domain: str) -> str | None:
    """域名 → 首个 IPv4（getaddrinfo）；解析失败返回 None（存独立行，不猜）。
    命中批次内预热缓存直接返回（导入路径免重复 DNS）。"""
    d = domain.strip().lower()
    with _DNS_LOCK:
        if d in _DNS_WARM:
            return _DNS_WARM[d]
    return _resolve_nocache(d)


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
                 session_id: str | None = None, quiet: bool = False) -> tuple[str, bool]:
    """按 IP 去重建/复用 host 资产，返回 (host_id, 已存在)。quiet=不发 asset.new。"""
    existing = bb.find_asset(project_id, "host", ip)
    if existing is not None:
        return existing["id"], True
    r = bb.upsert_asset(project_id, "host", ip, author=author)
    if r["created"] and not quiet:
        bb.append_event(project_id, "asset.new",
                        {"asset_id": r["id"], "type": "host", "value": ip,
                         "parent_id": None},
                        session_id=session_id, author=author)
    return r["id"], False


def register_asset(bb, project_id: str, value: str, type_: str = "auto",
                   parent_id: str | None = None, meta: dict | None = None,
                   author: str = "human", session_id: str | None = None,
                   quiet: bool = False,
                   cdn_lists: CdnLists | None = None) -> dict:
    """登记资产（E6 ①统一入口）。返回：
    {id, created, type, value, parent_id, host_id?, host_existed?, dns?, cdn?}

    - type 省略/``auto`` → detect_type；识别不出抛 ValueError（API 422 / 工具回填）。
    - 既有同值资产（host 按 IP、domain 按完整串）→ 合并路径：补挂 parent + meta
      merge，不插新行（修人工路径重复行）。
    - domain 且未显式给 parent → 自动 DNS 解析；**命中 CDN/共享托管（cdn.is_cdn：
      meta.cdn 人工覆盖 > CNAME 后缀 > CIDR 清单）保持根行不挂 host**；非 CDN
      挂 host，同 IP 已有主域名时本域名标 meta.alias=true，首个写
      host.meta.primary_domain。
    - **DNS 漂移**：存量 domain 行已挂 host，重报时解析落在别的 host → set_asset_parent
      改挂；CDN 状态翻转 → 摘挂为根行。解析失败不动现有挂载（不猜）。
    - url/service 主机部为域名时：不猜 DNS 也不造行，同名 domain 资产已存在
      才精确挂其下（存量孤儿行走 scripts/adopt_orphan_assets.py 补挂）。
    - quiet=True（cyberspace-mapping M1）静音本行全部 asset.new 事件——批量
      导入 264 行不刷屏事件流，由 import_assets 落单条汇总事件。
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
    # 显式 domain 的防御性规范化（2026-09-24）：导入路径已在 _host_of 兜底，
    # 这里拦住 API/Agent 直传——剥 scheme（取 hostname）/尾斜/端口尾部，
    # 不允许「https://x」「x:8080/」形态落成 domain 脏行。
    if type_ == "domain":
        if "://" in value:
            value = urlparse(value).hostname or ""
        value = value.rstrip("/")
        if ":" in value:
            head, _, tail = value.rpartition(":")
            if head and tail.isdigit() and len(tail) <= 5:
                value = head
        if not value:
            raise ValueError("domain 值规范化后为空（请传裸域名，不带 http:// 或端口）")
    result: dict = {"id": None, "created": False, "type": type_, "value": value,
                    "parent_id": parent_id}
    explicit_parent = parent_id
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
                                                     author, session_id, quiet)
                parent_id = host_id
            except ValueError:
                d = bb.find_asset(project_id, "domain", host_part)
                if d is not None:
                    parent_id = d["id"]  # 精确匹配既有 domain；缺失保持独立行

    # domain 自动 DNS 挂载（E6 ③⑤；asset-tree-derived-clean M1：CDN 保持根行）
    dns_resolved = False
    cdn_hit = False
    if type_ == "domain" and parent_id is None:
        # 存量行 meta（人工 meta.cdn 覆盖）要参与判定——先轻查一次
        prior = bb.find_asset(project_id, "domain", value)
        prior_meta = (prior or {}).get("meta") or {}
        ip = resolve_ipv4(value)
        if ip:
            cdn_hit = is_cdn(ip, value, meta={**prior_meta, **meta},
                             cdn_lists=cdn_lists)
            if not cdn_hit:
                host_id, host_existed = _ensure_host(bb, project_id, ip, author,
                                                     session_id, quiet)
                parent_id = host_id
                dns_resolved = True
                host_row = bb.get_asset(host_id)
                primary = (host_row or {}).get("meta", {}).get("primary_domain")
                if primary and primary != value:
                    meta.setdefault("alias", True)
                elif not primary:
                    bb.update_asset_meta(host_id, {"primary_domain": value})
    result["parent_id"] = parent_id
    result["cdn"] = cdn_hit
    if host_id is not None:
        result["host_id"] = host_id
        result["host_existed"] = host_existed
        result["dns"] = dns_resolved

    # 去重（E6 ④）：host 按 IP / domain 按完整串，命中即合并
    existing = bb.find_asset(project_id, type_, value)
    if existing is not None:
        if explicit_parent and not existing.get("parent_id"):
            # 显式 parent 的补挂（人工/调用方意图；挂载被拒保留原状）
            try:
                bb.set_asset_parent(existing["id"], explicit_parent)
            except ValueError:
                pass
        elif explicit_parent is None and type_ == "domain" \
                and (dns_resolved or cdn_hit):
            # DNS 漂移：现存挂载 ≠ 本次解析期望挂载 → 改挂/摘挂。
            # 解析失败（两信号皆 False）不进此分支，挂载保持不动。
            desired = host_id if dns_resolved else None
            if existing.get("parent_id") != desired:
                try:
                    bb.set_asset_parent(existing["id"], desired)
                except ValueError:
                    pass
        if meta:
            bb.update_asset_meta(existing["id"], meta)
        result["id"] = existing["id"]
        return result

    r = bb.upsert_asset(project_id, type_, value, parent_id=parent_id,
                        meta=meta, author=author)
    if r["created"] and not quiet:
        bb.append_event(project_id, "asset.new",
                        {"asset_id": r["id"], "type": type_, "value": value,
                         "parent_id": parent_id},
                        session_id=session_id, author=author)
    result["id"] = r["id"]
    result["created"] = r["created"]
    return result


# ---------- 批量导入（cyberspace-mapping M1，2026-09-23） ----------

def _datetime_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def import_assets(bb, project_id: str, rows: list[dict], source: str,
                  author: str = "human") -> dict:
    """批量导入（cyberspace-mapping M1）：rows 为归一化行
    {ip?, port?, host?/domain?, url?, title?, products?, protocol?}；一律经
    register_asset 单一入口（quiet=True 静音逐行 asset.new），整批只落单条
    `asset.imported` 汇总事件（payload 带 batch_id 预留按批次撤销）。

    行路由：url → 主机部为域名且无既有 domain 行时先补登 domain（其内部
    自动 DNS 挂 host，失败不阻塞）再登 url；host → 按 IP/域名分流（FOFA 的
    host 字段常回「ip:port」，剥端口后是 IP → 有端口 service/无端口 host；
    是域名 → domain，行还带 ip:port 时域名+端点双锚点补登 service）；
    ip+port → service；仅 ip → host。处理序 domain 行先于 url 行（register
    对 url 只挂既有 domain，域名先行提高挂载率）；DNS 线程池预热（16
    workers）填充批次内缓存，串行登记命中缓存零等待。
    返回 {batch_id, source, total, created, merged, skipped, failed}。
    """
    batch_id = new_id("imp")
    meta_of = lambda extra: {**{k: v for k, v in (extra or {}).items() if v},
                             "source": source, "imported_at": _datetime_now(),
                             "import_batch": batch_id}

    def _host_of(row: dict) -> str:
        # 清洗逻辑抽到模块级 clean_host（FOFA host 展示共用同一规范化），
        # host 缺失回退 domain——镜像导入登记的实际取值。
        return clean_host(str(row.get("host") or row.get("domain") or ""))

    # 预热 DNS：host 域名 + url 主机部域名
    domains: list[str] = []
    for row in rows:
        h = _host_of(row)
        if h and "/" not in h:
            domains.append(h)
        url = str(row.get("url") or "").strip()
        if "://" in url:
            hp = urlparse(url).hostname
            if hp:
                domains.append(hp)
    try:
        warm_dns(domains)
        created = merged = skipped = 0

        def _reg(value: str, type_: str, meta: dict) -> dict:
            nonlocal created, merged
            r = register_asset(bb, project_id, value, type_=type_, meta=meta,
                               author=author, quiet=True)
            if r.get("created"):
                created += 1
            else:
                merged += 1
            if r.get("host_id") and not r.get("host_existed"):
                created += 1  # 自动挂载新建的 host 行也计入新建
            return r

        failed: list[dict] = []
        # 域名行先行（提高 url 行挂载率），url 次之，其余随原序
        def _order(pair):
            idx, row = pair
            if row.get("url"):
                return 1
            if row.get("host") or row.get("domain"):
                return 0
            return 2
        for idx, row in sorted(enumerate(rows), key=_order):
            ip = str(row.get("ip") or "").strip()
            port = str(row.get("port") or "").strip()
            meta = meta_of({"title": (str(row["title"]).strip()[:200]
                                      if row.get("title") else ""),
                            "products": row.get("products"),
                            "protocol": (str(row["protocol"]).strip()
                                         if row.get("protocol") else "")})
            try:
                url = str(row.get("url") or "").strip()
                raw_host = str(row.get("host") or row.get("domain") or "").strip()
                host = _host_of(row)
                # host 列原值其实是完整 URL（xlsx 列映射错误的兜底）：改走 url
                # 路径——先补 domain 再登 url，端点信息不丢（直接按 domain 登
                # 会丢掉端口/路径）。
                if not url and "://" in raw_host:
                    url = raw_host
                if url:
                    if "://" not in url:
                        raise ValueError(f"url 缺协议: {url[:80]}")
                    hp = urlparse(url).hostname
                    if hp and _looks_domain(hp) and bb.find_asset(
                            project_id, "domain", hp) is None:
                        _reg(hp, "domain", dict(meta))
                    _reg(url, "url", meta)
                elif host:
                    if _is_ip(host):
                        # host 字段是 IP 形态（FOFA host 常回「ip:port」再被
                        # _host_of 剥端口）：有端口 → service，无 → host
                        _reg(f"{host}:{port}" if port else host,
                             "service" if port else "host", meta)
                    else:
                        _reg(host, "domain", meta)
                        # 行同时带端点（ip:port / host:port）：域名+端点双锚点
                        svc_host = ip or host
                        if port and svc_host:
                            _reg(f"{svc_host}:{port}", "service", meta)
                elif ip and port:
                    _reg(f"{ip}:{port}", "service", meta)
                elif ip:
                    _reg(ip, "host", meta)
                else:
                    skipped += 1
            except Exception as e:  # noqa: BLE001 — 单行失败不断整批
                failed.append({"index": idx, "reason": str(e)[:200]})
        summary = {"batch_id": batch_id, "source": source, "total": len(rows),
                   "created": created, "merged": merged, "skipped": skipped,
                   "failed_count": len(failed), "failed": failed[:20],
                   "author": author}
        bb.append_event(project_id, "asset.imported", summary, author=author)
        return summary
    finally:
        clear_dns_warm()


def clean_host(value: str) -> str:
    """剥 scheme（「://」→ urlparse.hostname）、尾斜杠、数字端口/路径尾。

    FOFA host 展示与导入登记的共用规范化（幂等，干净值原样过）：
    「https://zczx.zut.edu.cn」「authserver.zut.edu.cn:8080」「x.com/」
    都归一成裸主机名——端口随 port 字段走，此处只留主机名。
    """
    h = str(value or "").strip()
    if "://" in h:
        try:
            u = urlparse(h)
        except ValueError:
            return ""
        h = u.hostname or ""
    h = h.rstrip("/")
    if h and ":" in h:
        head, _, tail = h.rpartition(":")
        # 「x.com:8080/」形态：尾部带路径（非数字 tail）整段切掉
        if head and tail.isdigit() and len(tail) <= 5:
            return head
    return h


def _looks_domain(host: str) -> bool:
    """url 主机部是否域名（非 IP）——复用类型识别的宽松判断。"""
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return bool(host) and "." in host and " " not in host


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
