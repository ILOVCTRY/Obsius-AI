"""存量脏 domain 行规范化（2026-09-24 资产导入规范化修复的配套脚本）。

背景：xlsx 导入列映射为 host 而列内容是完整 URL（或带端口/路径）时，旧路径以
显式 ``type_="domain"`` 登记，绕过 detect_type——产出 ``https://x`` / ``x:8080/``
形态的脏 domain，与裸域名无法合并、虚增 uncovered。导入路径与 register_asset
均已加规范化，本脚本清理**存量**脏行：

  - scheme 脏行且同 hostname 的裸 domain 已存在 → 删脏行（asset.deleted 留痕）
  - scheme 脏行无裸 domain → 先经 register_asset 登记裸 domain（自动 DNS 挂 host
    等既有逻辑全复用），再删脏行（资产不丢、值变干净）
  - 非 scheme 但带尾部 ``:port/`` 的脏 domain（如 x:8080/）→ 同上按裸值合并
  - 占位/表头文本（「主机名」「网站标题」等非域名形态）→ 直接删

只处理**叶子且无 finding 引用**的行（delete_asset 服务端门控，拒绝即跳过列出，
绝不强删）；写路径全部走 Blackboard/register_asset 单一入口，
author=demo-script(normalize-domains)，事件留审计。

默认 dry-run 只打印计划；--apply 落库。

环境坑：normalize 走 register_asset 会触发 DNS 自动挂 host。若本机处于
Clash/Surge fake-ip（198.18.0.0/15）代理环境，所有域名都会解析到假 IP 并
造出 fake host 行——落库后须核对 host 列表，把 fake host 摘挂域名后删除
（域名保留为根行，等同「解析失败独立成行」语义）。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\normalize_dirty_domains.py                # 全部项目 dry-run
    E:\\Miniconda3\\python.exe scripts\\normalize_dirty_domains.py <slug> --apply  # 指定项目落库
"""

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.blackboard.assets import detect_type, register_asset
from core.projects import ProjectStore

AUTHOR = "demo-script(normalize-domains)"
# 表头/占位文本黑名单（xlsx 表头被当资产的实战样本）
PLACEHOLDER_VALUES = {"主机名", "网站标题", "域名", "网址", "链接"}


def bare_hostname(value: str) -> str:
    """脏值 → 裸主机名（剥 scheme/尾斜/端口尾部）；无法归一返回空串。"""
    v = value.strip()
    if "://" in v:
        v = urlparse(v).hostname or ""
    v = v.rstrip("/")
    if ":" in v:
        head, _, tail = v.rpartition(":")
        if head and tail.isdigit() and len(tail) <= 5:
            v = head
    return v


def plan_project(project) -> list[tuple[str, str, str]]:
    """返回该项目待处理清单 [(asset_id, value, action)]，action∈
    delete（裸值已存在/占位）/ normalize（需先登记裸 domain）。"""
    bb = project.bb
    pid = project.id
    existing = {a["value"]: a["id"]
                for a in bb.list_assets(pid, type_="domain")}
    out: list[tuple[str, str, str]] = []
    for a in bb.list_assets(pid, type_="domain"):
        value = a["value"]
        bare = bare_hostname(value)
        if value in PLACEHOLDER_VALUES or (bare and detect_type(bare) != "domain"):
            out.append((a["id"], value, "delete"))
        elif bare and bare != value:
            out.append((a["id"], value,
                        "delete" if bare in existing else "normalize"))
    return out


def apply_project(store: ProjectStore, slug: str) -> tuple[int, int]:
    """落库。返回 (处理数, 跳过数)。"""
    project = store.open_project(slug)
    bb = project.bb
    pid = project.id
    done = skipped = 0
    try:
        for aid, value, action in plan_project(project):
            try:
                if action == "normalize":
                    bare = bare_hostname(value)
                    register_asset(bb, pid, bare, type_="domain",
                                   author=AUTHOR)  # 先登记裸 domain
                bb.delete_asset(aid, author=AUTHOR)  # 门控：叶子+无 finding
                done += 1
                print(f"  [{action}] {value[:70]}")
            except (ValueError, LookupError) as e:
                skipped += 1
                print(f"  [跳过] {value[:70]} — {str(e)[:100]}")
    finally:
        project.close()  # Windows 句柄不残留
    return done, skipped


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    store = ProjectStore()
    slugs = [args[0]] if args else [m["slug"] for m in store.list_projects()]
    total_done = total_skip = 0
    for slug in slugs:
        project = store.open_project(slug)
        try:
            plan = plan_project(project)
        finally:
            project.close()
        if not plan:
            continue
        print(f"\n== {slug}：{len(plan)} 条待处理 ==")
        if apply:
            d, s = apply_project(store, slug)
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
