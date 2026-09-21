"""存量孤儿资产通用回填（§5.2 资产树）：url/service 主机部命中既有 host/domain → 补挂。

背景：资产树/自动挂载（E6 register_asset）上线前登记的 url/service 行 parent_id=NULL，
平铺在资产列表顶层（如 CTF 项目的 url http://IP:PORT 与其 host 行并列——旧后端登记
不走自动挂载）。本脚本对全部项目（或指定 slug）做一次确定性扫描补挂，匹配规则与
register_asset 同一套：

  - url/service 主机部=IPv4 → 挂同名 host 资产（host 缺失只提示，不造行）
  - url/service 主机部=域名 → 挂同名 domain 资产（缺省不造行、不猜 DNS）
  - parent_id 已有值的行不动；host/domain/binary 等其他类型不碰

默认 dry-run 只打印计划；--apply 落库（写路径全部走 Blackboard 方法 set_asset_parent，
author=demo-script(adopt)，事件留审计）。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\adopt_orphan_assets.py                # 全部项目 dry-run
    E:\\Miniconda3\\python.exe scripts\\adopt_orphan_assets.py <slug> --apply  # 指定项目落库
"""

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.projects import ProjectStore

ROOT = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    print(msg, flush=True)


def host_part_of(a: dict) -> str | None:
    """url 取 hostname / service 取冒号前段（detect_type 保证 service 不含 IPv6）；其他 None。"""
    v = a["value"]
    if a["type"] == "url":
        try:
            return urlparse(v).hostname
        except ValueError:
            return None
    if a["type"] == "service" and ":" in v:
        return v.rsplit(":", 1)[0]
    return None


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    store = ProjectStore(ROOT / "workspaces")
    slugs = args or [p["slug"] for p in store.list_projects()]

    total = 0
    for slug in slugs:
        proj = store.open_project(slug)
        bb, pid = proj.bb, proj.id
        assets = bb.list_assets(pid)
        hosts = {a["value"]: a["id"] for a in assets if a["type"] == "host"}
        domains = {a["value"]: a["id"] for a in assets if a["type"] == "domain"}
        plans: list[tuple[dict, str, str]] = []  # (资产, 挂到类型, 目标 id)
        for a in assets:
            if a["parent_id"] or a["type"] not in ("url", "service"):
                continue
            hp = host_part_of(a)
            if not hp:
                continue
            if hp in hosts:
                plans.append((a, "host", hosts[hp]))
            elif hp in domains:
                plans.append((a, "domain", domains[hp]))
            else:
                log(f"[{slug}] [缺父] {a['type']} {a['value']} 主机部 {hp} 无同名 host/domain 行")
        for a, t, target_id in plans:
            log(f"[{slug}] [计划] {a['type']} {a['value']} → {t} {target_id}")
        if apply:
            for a, _t, target_id in plans:
                bb.set_asset_parent(a["id"], target_id)
        proj.close()
        total += len(plans)
        if not plans:
            log(f"[{slug}] 无孤儿 url/service")
    log(f"\n[{'已落库' if apply else 'dry-run，加 --apply 落库'}] 共 {total} 条补挂计划")


if __name__ == "__main__":
    main()
