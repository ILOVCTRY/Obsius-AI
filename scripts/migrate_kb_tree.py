"""kb 物理树一次性迁移（expert-pool M0，2026-09-21 定稿 §4.5）。

capabilities/<cap>/kb/* → packs/kb/<cap>/*（全局单根，一级=能力域，与 caps 同键）；
五包 route_index.yaml 并为全局 packs/kb/route_index.yaml（条目 kb 加域前缀）；
route.json 留域内（值加域前缀）；CTF-SKILLS-LICENSE 五份去重上收 kb/licenses/；
技能与 kb 正文中「快照名打头」的文件级引用改写为「域名打头」（旧路径加域前缀）。

幂等三步：copy（源不动）→ 验证（全仓引用逐条解析存在）→ del 旧树。
默认 dry-run 只打印计划；--apply 才落盘。

用法：
  E:\\Miniconda3\\python.exe scripts/migrate_kb_tree.py            # dry-run
  E:\\Miniconda3\\python.exe scripts/migrate_kb_tree.py --apply    # 执行迁移
"""
from __future__ import annotations

import argparse
import filecmp
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.skills.routeindex import parse_entries, serialize_entries  # noqa: E402

_REF_RE_TMPL = r"""([`'"])((?:{snaps})/[^\s`'"\x27\x22（）)]+?\.(?:md|py|sh|txt|yaml|yml|json))\1"""
_MD_SUFFIX = (".md", ".py", ".sh", ".txt", ".yaml", ".yml", ".json")


def snap_domain_map_new(new_root: Path) -> dict[str, list[str]]:
    """从已迁好的新树反推快照名→域（kb/<域>/<快照>/ 一级子目录；旧树已删时用）。"""
    mapping: dict[str, list[str]] = {}
    for dom_dir in sorted(p for p in new_root.iterdir()
                          if p.is_dir() and p.name != "licenses"):
        for snap in sorted(p for p in dom_dir.iterdir()
                           if p.is_dir() and p.name != ".history"):
            mapping.setdefault(snap.name, []).append(dom_dir.name)
    return mapping


def snap_domain_map(packs: Path) -> dict[str, list[str]]:
    """迁移前快照名→域清单（kb/<snap> 一级目录；同名跨域=歧义名，按引用方域消解）。"""
    mapping: dict[str, list[str]] = {}
    caps = packs / "capabilities"
    for cap_dir in sorted(p for p in caps.iterdir() if p.is_dir()):
        kb = cap_dir / "kb"
        if not kb.is_dir():
            continue
        for snap in sorted(p for p in kb.iterdir() if p.is_dir() and p.name != ".history"):
            mapping.setdefault(snap.name, []).append(cap_dir.name)
    return mapping


def domain_of_file(path: Path, packs: Path) -> str | None:
    """引用文件所属域：新树 kb/<cap>/… → cap；capabilities/<cap>/… → cap；tracks → None。"""
    try:
        rel = path.resolve().relative_to(packs.resolve())
    except ValueError:
        return None
    if rel.parts[0] == "kb" and len(rel.parts) > 1:
        return rel.parts[1]
    if rel.parts[0] == "capabilities" and len(rel.parts) > 1:
        return rel.parts[1]
    return None


def rewrite_refs(text: str, path: Path, snap_map: dict[str, list[str]],
                 packs: Path) -> tuple[str, list[str]]:
    """文件级引用改写：`` `pwn/x.md` `` → `` `binary/pwn/x.md` ``（旧路径加域前缀）。

    只动引号/反引号包裹的文件级引用（kb_open 调用与目录列举短引用不动）；
    refs 等歧义快照名按引用文件所属域消解；tracks 文件遇歧义名报警保留原文。"""
    snaps = sorted(snap_map, key=len, reverse=True)
    pat = re.compile(_REF_RE_TMPL.format(snaps="|".join(re.escape(s) for s in snaps)))
    fdom = domain_of_file(path, packs)
    unresolved: list[str] = []

    def _sub(m: re.Match) -> str:
        quote, ref = m.group(1), m.group(2)
        snap = ref.split("/", 1)[0]
        doms = snap_map.get(snap) or []
        if len(doms) == 1:
            dom = doms[0]
        elif fdom and fdom in doms:
            dom = fdom
        else:
            unresolved.append(ref)
            return m.group(0)
        return f"{quote}{dom}/{ref}{quote}"

    return pat.sub(_sub, text), unresolved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs", default=str(ROOT / "packs"))
    ap.add_argument("--apply", action="store_true", help="真正落盘（缺省 dry-run）")
    ap.add_argument("--refs-only", action="store_true",
                    help="只跑 ⑤引用改写+⑥验证（迁移主体已完成后重跑用；快照映射从新树反推）")
    args = ap.parse_args()
    packs = Path(args.packs)
    caps = packs / "capabilities"
    new_root = packs / "kb"
    cap_list = sorted(p.name for p in caps.iterdir() if p.is_dir())

    snap_map = (snap_domain_map_new(new_root) if args.refs_only
                else snap_domain_map(packs))
    print("快照名→域映射：")
    for s, doms in sorted(snap_map.items()):
        print(f"  {s:<24} → {'、'.join(doms)}" + ("（歧义：按引用方域）" if len(doms) > 1 else ""))

    plan_copy: list[tuple[Path, Path]] = []
    for cap in cap_list:
        kb = caps / cap / "kb"
        if not kb.is_dir():
            continue
        for f in sorted(kb.rglob("*")):
            if ".history" in f.parts or not f.is_file():
                continue
            plan_copy.append((f, new_root / cap / f.relative_to(kb)))

    licenses = [caps / c / "kb" / "CTF-SKILLS-LICENSE" for c in cap_list
                if (caps / c / "kb" / "CTF-SKILLS-LICENSE").is_file()]
    lic_master = licenses[0] if licenses else None

    # route_index 全局合并
    index_blocks: list[str] = []
    for cap in cap_list:
        idx = caps / cap / "route_index.yaml"
        if not idx.is_file():
            continue
        entries = parse_entries(idx.read_text(encoding="utf-8"))
        for e in entries:
            e.kb = f"{cap}/{e.kb}"
        index_blocks.append(serialize_entries(entries))
    global_index = new_root / "route_index.yaml"
    n_index = sum(b.count("\n  - ") for b in index_blocks)

    # route.json 前缀化计划（留域内）
    import json
    route_plan: list[tuple[Path, dict]] = []
    for cap in cap_list:
        rp = caps / cap / "kb" / "route.json"
        if not rp.is_file():
            continue
        data = json.loads(rp.read_text(encoding="utf-8"))
        prefixed = {k: [f"{cap}/{str(x)}" for x in v] for k, v in data.items()}
        route_plan.append((new_root / cap / "route.json", prefixed))

    # 引用改写计划
    rewrite_targets: list[Path] = []
    if args.refs_only:
        # 重跑模式：只动新树 kb 正文（技能/rules 已人工修复为域打头，
        # 不进目标——域打头引用首段恰是快照名（crypto/crypto/…）会被叠加二次前缀）
        for cap in cap_list:
            rewrite_targets += sorted((new_root / cap).rglob("*.md"))
    else:
        for pat in ("capabilities/*/skills/*/SKILL.md", "tracks/*/skills/*/SKILL.md",
                    "tracks/*/rules/**/*.md", "capabilities/*/rules/**/*.md"):
            rewrite_targets += sorted(packs.glob(pat))
        rewrite_targets += [dst for _src, dst in plan_copy if dst.suffix == ".md"]

    print(f"\n计划：搬 {len(plan_copy)} 文件 → kb/<cap>/；"
          f"全局 route_index {n_index} 条；route.json 前缀化 {len(route_plan)} 包；"
          f"引用改写扫描 {len(rewrite_targets)} 文件；LICENSE 去重 {len(licenses)} 份")
    if not args.apply:
        print("（dry-run，加 --apply 执行）")
        return 0

    # 1) copy（源不动）
    if not args.refs_only:
        for src, dst in plan_copy:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        print(f"① 已复制 {len(plan_copy)} 文件")

        if lic_master is not None:
            lic_dst = new_root / "licenses" / lic_master.name
            lic_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(lic_master, lic_dst)
            for extra in licenses[1:]:
                if not filecmp.cmp(lic_master, extra, shallow=False):
                    print(f"⚠ LICENSE 不一致：{extra}")
            print(f"② LICENSE 上收 kb/licenses/（比对 {len(licenses)} 份）")

        for dst, data in route_plan:
            dst.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        print(f"③ route.json 前缀化 {len(route_plan)} 包（留域内）")

        if index_blocks:
            text = index_blocks[0] if len(index_blocks) == 1 else (
                "entries:\n" + "\n".join(
                    b.split("\n", 1)[1].strip("\n") for b in index_blocks) + "\n")
            global_index.write_text(text, encoding="utf-8")
        print(f"④ 全局 route_index.yaml {n_index} 条（kb 加域前缀）")

    # ⑤ 引用改写（只动快照名打头的文件级引用）
    n_files = n_refs = 0
    all_unresolved: list[str] = []
    for f in rewrite_targets:
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new_text, unresolved = rewrite_refs(text, f, snap_map, packs)
        all_unresolved += [f"{f.name}: {u}" for u in unresolved]
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
            n_files += 1
            n_refs += len(re.findall(_REF_RE_TMPL.format(
                snaps="|".join(re.escape(s) for s in sorted(snap_map, key=len, reverse=True))),
                text)) - len(unresolved)
    print(f"⑤ 引用改写：{n_files} 文件 / {n_refs} 处")
    if all_unresolved:
        print(f"⚠ 歧义快照名无法定位引用方域（保留原文，人工处理 {len(all_unresolved)} 处）:")
        for u in all_unresolved[:20]:
            print("   ", u)

    # ⑥ 验证（双检查）：①旧形态（快照名打头）应零残留（unresolved 保留项除外）；
    # ②新形态（域名打头）逐条解析 new_root/<ref> 存在
    pat_old = re.compile(_REF_RE_TMPL.format(
        snaps="|".join(re.escape(s) for s in sorted(snap_map, key=len, reverse=True))))
    pat_new = re.compile(r"""([`'"])((?:%s)/[^\s`'"\x27\x22（）)]+?\.(?:md|py|sh|txt|yaml|yml|json))\1"""
                         % "|".join(re.escape(c) for c in cap_list))
    bad: list[str] = []
    scan_targets = [f for f in rewrite_targets if f.is_file() and f.suffix == ".md"]
    if not args.refs_only:
        scan_targets += [dst for _s, dst in plan_copy if dst.suffix == ".md"]
    seen: set[str] = set()
    unresolved_names = {u.split(": ", 1)[1] for u in all_unresolved}
    for f in scan_targets:
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in pat_old.finditer(text):
            ref = m.group(2)
            if ref in unresolved_names:
                continue
            if f"{f.name}:{ref}" not in seen:
                seen.add(f"{f.name}:{ref}")
                bad.append(f"[残留旧形态] {f} → {ref}")
        for m in pat_new.finditer(text):
            ref = m.group(2)
            if "<" in ref:  # 模板占位符（如 web/playbooks/<类型>.md），非真实引用
                continue
            if not (new_root / ref).is_file():
                bad.append(f"[悬空] {f} → {ref}")
    if bad:
        print(f"✗ 验证失败 {len(bad)} 处引用悬空（旧树保留，不删除）：")
        for b in bad[:30]:
            print("   ", b)
        return 1
    print(f"⑥ 验证通过：全部引用落新树有效")

    # ⑦ del 旧树
    if args.refs_only:
        print("完成（refs-only：仅改写+验证）。")
        return 0
    for cap in cap_list:
        kb = caps / cap / "kb"
        if kb.is_dir():
            shutil.rmtree(kb)
        for f in (caps / cap / "route_index.yaml", caps / cap / "kb_sources.json"):
            if f.is_file():
                f.unlink()
    print(f"⑦ 旧树已删（capabilities/<cap>/kb、route_index.yaml、kb_sources.json）")
    print("完成。下一步：代码切换（load_kb_sources 合成源等，见 expert-pool.md §4.5.3）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
