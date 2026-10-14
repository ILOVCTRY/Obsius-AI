"""修复树状化重构引入的相对链接断链（2026-10-14）。

背景：重构搬迁/去重后，kb 树内「裸兄弟名」链接（如 `(overflow-basics.md)`）
出现 140 处新断链——目标文件被去重删除（内容与收编版相同）或迁去别的模块。
本脚本把这类链接改写为指向 kb 树内存续正本的相对路径。

安全规则：
1. 只修「新树断、基线通」的链接（基线参照 ../kb-baseline worktree @ 0e739be）；
   基线就断的（上游死链等 152 处）一律不碰。
2. 候选=kb 树内同名文件（排除 skills/ 内部副本与源文件自身目录）；
   选择顺序：同方向 > 非快照（排除 赛事专题/题目知识/）> 唯一。
3. 不唯一则跳过并报告，绝不猜。
4. .history/ 与 skills/*/references/ 副本不在本轮范围。
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

KB = Path(__file__).resolve().parents[1] / "packs" / "kb"
# 基线 worktree 在仓库外：E:\ILOVCTRY\kb-baseline（checkpoint 0e739be）
BASE = KB.parents[2] / "kb-baseline" / "packs" / "kb"
LINK_RE = re.compile(
    r"\]\((\.{1,2}/|[\w一-鿿][\w一-鿿 ._/-]*?/)?"
    r"[\w一-鿿][\w一-鿿 ._-]*\.(md|py|json|ya?ml|txt|sh|png|jpe?g|gif|zip)\)")


def write_retry(p: Path, text: str) -> None:
    """Windows 中文路径瞬时竞态退避重试。"""
    for i in range(10):
        try:
            p.write_text(text, encoding="utf-8")
            return
        except OSError:
            if i == 9:
                raise
            time.sleep(0.3)


def broken_links(md: Path) -> list[str]:
    try:
        text = md.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for m in LINK_RE.finditer(text):
        link = m.group(0)[2:-1]
        if link.startswith(("http", "/", "#", "mailto:")):
            continue
        if not (md.parent / link).exists():
            out.append(link)
    return out


def in_kb_tree(p: Path) -> bool:
    rel = p.relative_to(KB).as_posix()
    return ".history" not in rel.split("/") and not rel.startswith("skills/")


def main() -> int:
    # 1. 基线断链集（按源文件名聚合）
    base_broken: dict[str, set[str]] = defaultdict(set)
    if not BASE.is_dir():
        print(f"[错误] 基线 worktree 不存在: {BASE}")
        return 1
    for md in BASE.rglob("*.md"):
        if ".history" in md.parts:
            continue
        for l in broken_links(md):
            base_broken[md.name].add(l)

    # 2. kb 树 basename 索引（排除 skills 副本）
    by_name: dict[str, list[Path]] = defaultdict(list)
    for f in KB.rglob("*"):
        if f.is_file() and in_kb_tree(f):
            by_name[f.name].append(f)

    def pick(src: Path, link: str) -> Path | None:
        cands = [c for c in by_name.get(Path(link).name, [])
                 if c.parent != src.parent and c != src]
        if not cands:
            return None
        if len(cands) > 1:
            sdir = src.parent.relative_to(KB).parts[0]
            same_dir = [c for c in cands
                        if c.relative_to(KB).parts[0] == sdir]
            if same_dir:
                cands = same_dir
            live = [c for c in cands
                    if "题目知识" not in c.relative_to(KB).parts]
            if live:
                cands = live
        if len(cands) == 1:
            return cands[0]
        return None  # 二义/无候选：不猜

    fixed_files = 0
    fixed_links = 0
    skipped: list[tuple[str, str]] = []
    for md in sorted(KB.rglob("*.md")):
        if not in_kb_tree(md):
            continue
        text = md.read_text(encoding="utf-8")
        dirty = False
        for link in broken_links(md):
            if link in base_broken.get(md.name, set()):
                continue  # 基线就断，不碰
            tgt = pick(md, link)
            if tgt is None:
                skipped.append((md.relative_to(KB).as_posix(), link))
                continue
            rel = Path(os.path.relpath(tgt, md.parent)).as_posix()
            old = f"]({link})"
            new = f"]({rel})"
            if text.count(old):
                text = text.replace(old, new)
                dirty = True
                fixed_links += 1
        if dirty:
            write_retry(md, text)
            fixed_files += 1
            print(f"  修复 {md.relative_to(KB).as_posix()}")

    print(f"\n完成：{fixed_files} 个文件、{fixed_links} 处链接；"
          f"跳过（无唯一候选）{len(skipped)} 处")
    for f, l in skipped:
        print(f"  [跳过] {f} -> {l}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
