"""一次性回填：pentest-pikachu 旧资产挂成 host → service → url 树 + 探测 title + 打「已扫」标。

背景（DESIGN.md §5.2）：资产树功能上线前的演练数据全是 parent_id=NULL 平铺行。
本脚本对该项目：
  1. 确保 host 资产（127.0.0.1）存在；service / url 挂载（url 挂 service，host → service → url）
  2. best-effort 探测每个 url 的页面 <title>（靶机不在线则跳过，不报错）
  3. url / service 合并 meta.scanned=true（recon 会话确实枚举过）与 title

写路径全部走 Blackboard 方法（set_asset_parent / update_asset_meta），author=demo-script(backfill)。

用法（项目根；Windows 控制台先 $env:PYTHONIOENCODING="utf-8"）：
    E:\\Miniconda3\\python.exe scripts\\backfill_asset_tree.py [项目slug，默认 pentest-pikachu] [--no-probe]
"""

import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.projects import ProjectStore

ROOT = Path(__file__).resolve().parent.parent
HOST_IP = "127.0.0.1"
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_title(url: str) -> str | None:
    """best-effort 取页面 <title>（= 人类浏览器行为，不经网关；失败返回 None）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "backfill/0.1"})
        with urllib.request.urlopen(req, timeout=3) as r:
            head = r.read(65536).decode("utf-8", errors="replace")
        m = TITLE_RE.search(head)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()[:120] or None
    except Exception as e:  # noqa: BLE001
        log(f"  [跳过] {url} 不可达：{e}")
    return None


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    slug = args[0] if args else "pentest-pikachu"
    probe = "--no-probe" not in sys.argv

    proj = ProjectStore(ROOT / "workspaces").open_project(slug)
    bb, pid = proj.bb, proj.id
    assets = bb.list_assets(pid)
    hosts = [a for a in assets if a["type"] == "host" and a["value"] == HOST_IP]
    host_id = hosts[0]["id"] if hosts else bb.upsert_asset(
        pid, "host", HOST_IP, meta={"note": "回填：本地授权靶机主机"}, author="demo-script(backfill)")["id"]
    log(f"[host] {HOST_IP} → {host_id}")

    services = [a for a in assets if a["type"] == "service"]
    service_id = services[0]["id"] if services else None
    if service_id:
        bb.set_asset_parent(service_id, host_id)
        log(f"[service] {services[0]['value']} 已挂到 host")

    n_mounted = n_titled = 0
    for a in assets:
        patch: dict = {"scanned": True}  # recon 会话确实枚举过这些入口
        if a["type"] == "url" and service_id:
            bb.set_asset_parent(a["id"], service_id)
            n_mounted += 1
        if probe and a["type"] == "url" and a["value"].startswith("http"):
            title = fetch_title(a["value"])
            if title:
                patch["title"] = title
                n_titled += 1
        bb.update_asset_meta(a["id"], patch)
    log(f"[回填完成] url 挂载 {n_mounted} 条，title 探测成功 {n_titled} 条")

    log("\n[终态树]")
    for h in [a for a in bb.list_assets(pid) if a["type"] == "host"]:
        log(f"  ▣ {h['value']} (host)")
        for s in [a for a in bb.list_assets(pid)
                  if a["parent_id"] == h["id"]]:
            log(f"    ⚙ {s['value']} (service)")
            for u in [a for a in bb.list_assets(pid) if a["parent_id"] == s["id"]]:
                log(f"       🔗 {u['value']}  title={u['meta'].get('title')}")
    bb.close()


if __name__ == "__main__":
    main()
