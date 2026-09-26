"""资产树重建与根状态复位（asset-tree-derived-clean M2 配套，2026-09-24）。

两件事：

1. **根域名重新挂树**：``parent_id IS NULL`` 的 domain 行 → DoH 重新解析
   （默认 223.5.5.5，避开本机 Clash fake-ip / 系统代理；含 CNAME 链）→
   命中 CDN（cname 后缀或 CIDR）保持根行不动；非 CDN 建/复用 host 行并
   ``set_asset_parent`` 挂树（主域名/别名标记同 register_asset 口径）。
2. **有子根行状态复位**：任何带资产子节点、自身显式 ``tested_clean`` 的根行
   （父节点状态改由子树读时派生，AI 显式值已成脏数据）→ 置回 open 并留 note。

叶子行一律不动（D5：零子资产显式状态保持 AI 管理）。

写操作全部走 Blackboard 方法（set_asset_parent / set_asset_status /
upsert_asset / update_asset_meta），author=demo-script(rebuild-trees)。
默认 dry-run 只打印计划；--apply 落库。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\rebuild_asset_trees.py                  # 全部项目 dry-run
    E:\\Miniconda3\\python.exe scripts\\rebuild_asset_trees.py <slug> --apply   # 指定项目落库
    E:\\Miniconda3\\python.exe scripts\\rebuild_asset_trees.py --resolver https://1.12.12.12/dns-query
"""

import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.blackboard.cdn import default_cdn_lists, is_cdn
from core.projects import ProjectStore

AUTHOR = "demo-script(rebuild-trees)"
DEFAULT_RESOLVER = "https://223.5.5.5/dns-query"
FAKE_IP_NETS = [ipaddress.ip_network("198.18.0.0/15")]


def doh_resolve(domain: str, resolver: str, timeout: float = 10.0
                ) -> tuple[str | None, list[str]]:
    """DoH JSON 接口解析 → (首个 A 记录, CNAME 链 data 列表)；失败 (None, [])。"""
    url = f"{resolver}?name={quote(domain)}&type=A"
    req = Request(url, headers={"Accept": "application/dns-json"})
    try:
        with urlopen(req, timeout=timeout) as r:  # noqa: S310 — 固定可信 DoH
            data = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return None, []
    answers = data.get("Answer") or []
    cnames = [str(a.get("data") or "") for a in answers if a.get("type") == 5]
    ips = [str(a.get("data") or "") for a in answers if a.get("type") == 1]
    return (ips[0] if ips else None), cnames


def _is_fake_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in FAKE_IP_NETS)


def _attach_domain(bb, pid: str, asset: dict, ip: str) -> str:
    """domain 挂到 ip 对应 host（建/复用）+ primary_domain/alias 标记；返回 action 描述。"""
    host = bb.find_asset(pid, "host", ip)
    if host is None:
        r = bb.upsert_asset(pid, "host", ip, author=AUTHOR)
        host_id = r["id"]
    else:
        host_id = host["id"]
    host_row = bb.get_asset(host_id) or {}
    primary = (host_row.get("meta") or {}).get("primary_domain")
    if primary and primary != asset["value"]:
        bb.update_asset_meta(asset["id"], {"alias": True})
    elif not primary:
        bb.update_asset_meta(host_id, {"primary_domain": asset["value"]})
    bb.set_asset_parent(asset["id"], host_id)
    return f"reparent → host:{ip}"


def plan_project(project, resolver: str, cdn_lists) -> list[tuple[str, str, str]]:
    """该项目待处理清单 [(asset_id, value, action 文本)]。"""
    bb = project.bb
    pid = project.id
    assets = bb.list_assets(pid)
    by_id = {a["id"]: a for a in assets}
    children: dict[str, list[dict]] = {}
    for a in assets:
        parent = a.get("parent_id")
        if parent and parent in by_id:
            children.setdefault(parent, []).append(a)

    out: list[tuple[str, str, str]] = []
    for a in assets:
        # ① 根域名重新解析挂树（CDN 保持根行；解析失败不动）
        if a.get("type") == "domain" and not a.get("parent_id"):
            ip, cnames = doh_resolve(a["value"], resolver)
            if ip is None:
                continue
            if _is_fake_ip(ip):
                out.append((a["id"], a["value"],
                            "跳过：解析结果落 fake-ip 段（DoH 可能被代理劫持）"))
                continue
            if is_cdn(ip, a["value"], cname=(cnames[-1] if cnames else None),
                      meta=a.get("meta"), cdn_lists=cdn_lists):
                continue  # CDN 根行 = 期望形态，无操作
            out.append((a["id"], a["value"], f"reparent → host:{ip}"))
        # ② 有子根行显式 tested_clean → 复位 open（盘上值与派生口径对齐）
        if a.get("parent_id") is None and a.get("status") == "tested_clean" \
                and children.get(a["id"]):
            out.append((a["id"], a["value"],
                        "reset-open：有子根行 clean 改由子树派生"))
    return out


def apply_project(store: ProjectStore, slug: str, resolver: str,
                  cdn_lists) -> tuple[int, int]:
    """落库。返回 (处理数, 跳过数)。"""
    project = store.open_project(slug)
    bb = project.bb
    pid = project.id
    done = skipped = 0
    try:
        for aid, value, action in plan_project(project, resolver, cdn_lists):
            try:
                if action.startswith("reparent"):
                    ip = action.rsplit("host:", 1)[1]
                    _attach_domain(bb, pid, bb.get_asset(aid), ip)
                elif action.startswith("reset-open"):
                    bb.set_asset_status(
                        aid, "open",
                        note="资产树重建：父节点 tested_clean 改由子树读时派生",
                        author=AUTHOR)
                else:
                    skipped += 1
                    print(f"  [跳过] {value[:70]} — {action}")
                    continue
                done += 1
                print(f"  [{action}] {value[:70]}")
            except (ValueError, LookupError) as e:
                skipped += 1
                print(f"  [跳过] {value[:70]} — {str(e)[:100]}")
    finally:
        project.close()
    return done, skipped


def main() -> None:
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    resolver = DEFAULT_RESOLVER
    for a in sys.argv[1:]:
        if a.startswith("--resolver="):
            resolver = a.split("=", 1)[1]
    store = ProjectStore()
    cdn_lists = default_cdn_lists()
    slugs = [positional[0]] if positional else [m["slug"] for m in store.list_projects()]
    total_done = total_skip = 0
    for slug in slugs:
        project = store.open_project(slug)
        try:
            plan = plan_project(project, resolver, cdn_lists)
        finally:
            project.close()
        if not plan:
            continue
        print(f"\n== {slug}：{len(plan)} 条待处理 ==")
        if apply:
            d, s = apply_project(store, slug, resolver, cdn_lists)
            total_done += d
            total_skip += s
        else:
            for aid, value, action in plan:
                print(f"  [{action}] {value[:70]}")
    if not apply:
        print("\n（dry-run：加 --apply 落库）")
    else:
        print(f"\n完成：处理 {total_done} 条，跳过 {total_skip} 条")


if __name__ == "__main__":
    main()
