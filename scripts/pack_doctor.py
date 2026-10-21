"""pack doctor CLI：能力包/场景轨静态体检（core/skills/doctor.py 的命令行壳）。

三级结论：
- error   绑定断裂（角色引用不存在的技能、task_type 未注册、name 不一致、kb root 缺失）
- warning 软问题（引用已禁用技能、缺 redlines/task_types.yaml、kb 引用失效）
- info    编辑提示（孤儿技能、.history/trash 待清理）

退出码：有任何 error 为 1（CI/钩子可用），否则 0；--strict 时 warning 也算失败。

用法：
  E:\\Miniconda3\\python.exe scripts/pack_doctor.py [--packs packs] [--strict]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _argv() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="packs 静态体检")
    ap.add_argument("--packs", default="packs", help="packs 根目录（默认 packs）")
    ap.add_argument("--strict", action="store_true", help="有 warning 也以 1 退出")
    ap.add_argument("--list-long", type=int, nargs="?", const=400, default=None,
                    metavar="MIN_LINES",
                    help="只列出未配分段索引的长手册（默认 ≥400 行），按行数降序")
    return ap.parse_args()


def _list_long(packs: str, min_lines: int) -> int:
    """列出长手册与是否有同名 .idx.md —— 供分批补索引时对照。"""
    kb = Path(packs) / "kb"
    rows: list[tuple[int, str, bool]] = []
    for md in sorted(kb.rglob("*.md")):
        if md.name.endswith(".idx.md") or ".history" in md.parts:
            continue
        try:
            n = md.read_text(encoding="utf-8").count("\n") + 1
        except (OSError, UnicodeDecodeError):
            continue
        if n < min_lines:
            continue
        has = md.with_name(md.stem + ".idx.md").is_file()
        rows.append((n, md.relative_to(kb).as_posix(), has))
    rows.sort(reverse=True)
    no_idx = [r for r in rows if not r[2]]
    print(f"≥{min_lines} 行手册共 {len(rows)} 篇，其中未配索引 {len(no_idx)} 篇：\n")
    for n, rel, has in rows:
        print(f"  {'✓' if has else '✗'} {n:>6} 行  {rel}")
    return 0


def main(packs: str = "packs", strict: bool = False) -> int:
    from core.skills.doctor import diagnose

    rep = diagnose(packs)
    order = {"error": 0, "warning": 1, "info": 2}
    for issue in sorted(rep.issues,
                        key=lambda i: (order[i.level], i.code, i.target)):
        loc = f" {issue.target}" if issue.target else ""
        print(f"[{issue.level:<7}] {issue.code}{loc}")
        print(f"           {issue.message}")
    counts = rep.counts
    print(f"\n体检完成：{counts['error']} error / "
          f"{counts['warning']} warning / {counts['info']} info")
    if counts["error"] or (strict and counts["warning"]):
        return 1
    if not rep.issues:
        print("零问题。")
    return 0


if __name__ == "__main__":
    args = _argv()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if args.list_long is not None:
        sys.exit(_list_long(args.packs, args.list_long))
    sys.exit(main(args.packs, args.strict))
