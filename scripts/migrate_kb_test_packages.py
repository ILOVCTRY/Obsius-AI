# -*- coding: utf-8 -*-
"""K5 一次性迁移：playbooks/知识库/ 49 篇测试点手册 → 阶段/测试包/手册.md（2026-09-20）。

DESIGN.md §4「知识库『测试包』分类制」的实施件：
1. 引用改写（先于移动）：
   - 全模块形态 ``playbooks/知识库/<old>.md`` → ``<new>/手册.md``（route_index.yaml、
     route.json、6 个 web 技能、CLAUDE.md 示例等）；
   - 相对形态 ``知识库/<old>.md`` → ``<new>/手册.md``（playbooks/SKILL.md 等）；
   - 裸文件名 ``<old>.md``（带词边界，防 injection-test.md 吃掉 crlf-injection-test.md）
     → ``<new>/手册.md``，只作用于 kb 手册区/规则区/红线区——refs/ 快照不动。
2. 文件迁移：writing.rename_kb_move（同源移动）+ 内容头部补 phase/vuln_class
   frontmatter（升级项 C，write_kb_file 留备份）。
3. 留守：README.md、打穿短表.md 原位不动；refs/ 不参与重组。

映射表唯一定义在 scripts/import_kb.py 的 STRIKE_KB_REMAP（import_kb --force 重建
同一口径）。幂等：已迁移（旧文件不存在）则跳过移动，仅重跑引用改写。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from import_kb import STRIKE_KB_REMAP, STRIKE_KB_STAY  # noqa: E402
from core.skills import refs, writing  # noqa: E402

CAP = "web"
KB_ROOT = ROOT / "packs" / "capabilities" / CAP / "kb"
STAY_DIR = KB_ROOT / "playbooks" / "知识库"

# 词边界防误吃：injection-test.md ⊂ crlf-injection-test.md 等
_STEM_RE = {
    stem: re.compile(rf"(?<![A-Za-z0-9\-]){re.escape(stem)}\.md")
    for stem in STRIKE_KB_REMAP
}


def _new_module(stem: str) -> str:
    return f"{STRIKE_KB_REMAP[stem][0]}/手册.md"


def _frontmatter(stem: str) -> str:
    _dir, phase, vcls = STRIKE_KB_REMAP[stem]
    lines = ["---", f"phase: {phase}"]
    if vcls:
        lines.append(f"vuln_class: [{', '.join(vcls)}]")
    return "\n".join(lines) + "\n---\n"


def _iter_ref_files():
    """引用改写的作用面：packs 下 web 包相关文件 + 两份 CLAUDE.md。排除
    .history、refs/（快照原件不动）、DESIGN.md（历史叙述不改写）。"""
    out: list[Path] = []
    for pat in ("packs/capabilities/web/route_index.yaml",
                "packs/capabilities/web/kb/route.json",
                "packs/capabilities/web/kb/README.md",
                "packs/capabilities/web/rules/redlines.md",
                "packs/tracks/*/rules/redlines.md",
                "packs/capabilities/web/skills/*/SKILL.md",
                "packs/capabilities/web/kb/playbooks/**/*.md",
                "core/agent/CLAUDE.md",
                "packs/CLAUDE.md"):
        out.extend(p for p in ROOT.glob(pat) if p.is_file())
    seen, uniq = set(), []
    for f in out:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(f)
    return [f for f in uniq
            if ".history" not in f.parts and "refs" not in f.parts]


def rewrite_references() -> dict[str, int]:
    """三形态引用改写。每个文件先备份（backup_history），返回 {file: 替换数}。"""
    stats: dict[str, int] = {}
    for f in _iter_ref_files():
        text = orig = f.read_text(encoding="utf-8")
        for stem in STRIKE_KB_REMAP:
            new = _new_module(stem)
            # ① 全模块形态（playbooks/知识库/xxx.md）
            text = text.replace(f"playbooks/知识库/{stem}.md", new)
            # ② 相对形态（知识库/xxx.md；①已消化全模块形态）
            text = text.replace(f"知识库/{stem}.md", new)
            # ③ 裸文件名（词边界；手册互引/表格）
            text = _STEM_RE[stem].sub(new, text)
        if text != orig:
            writing.backup_history(f)
            f.write_text(text, encoding="utf-8")
            stats[str(f.relative_to(ROOT))] = sum(
                1 for a, b in zip(orig.splitlines(), text.splitlines()) if a != b)
    return stats


def move_manuals() -> list[str]:
    """逐篇 rename → 补 frontmatter。返回迁移日志。"""
    logs = []
    for stem, (new_dir, _phase, _v) in sorted(STRIKE_KB_REMAP.items()):
        old_rel = f"playbooks/知识库/{stem}.md"
        new_rel = _new_module(stem)
        if not (KB_ROOT / old_rel).is_file():
            logs.append(f"  跳过（不存在或已迁移）: {old_rel}")
            continue
        writing.rename_kb_move(ROOT / "packs", CAP, old_rel, new_rel)
        target = writing.resolve_kb(ROOT / "packs", CAP, new_rel)
        text = target.path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            writing.write_kb_file(ROOT / "packs", CAP, new_rel,
                                  _frontmatter(stem) + "\n" + text, create=False)
        logs.append(f"  迁移: {old_rel} -> {new_rel}")
    return logs


def touch_up_narratives() -> None:
    """自动改写覆盖不到的叙述性文字（结构描述与并列关系）。"""
    ri = ROOT / "packs" / "capabilities" / "web" / "route_index.yaml"
    t = ri.read_text(encoding="utf-8")
    t = t.replace(
        "# ---- 外网打点 · 渗透测试手册（playbooks/知识库，tags 限打点/横移/提权角色） ----",
        "# ---- 外网打点 · 测试包手册（webapp/<测试包>/手册.md，tags 限打点/横移/提权角色） ----")
    ri.write_text(t, encoding="utf-8")

    poc = KB_ROOT / "refs" / "poc" / "README.md"
    if poc.is_file():
        t = poc.read_text(encoding="utf-8")
        t = t.replace("`知识库/*-test.md` | 某一类漏洞的通用测试方法论",
                      "`webapp/<测试包>/手册.md` | 某一类漏洞的通用测试方法论（K5 测试包分类）")
        poc.write_text(t, encoding="utf-8")

    # 红线区裸引用改写后残留的「知识库 webapp/...」措辞顺一下
    for f in ROOT.glob("packs/tracks/*/rules/redlines.md"):
        t = f.read_text(encoding="utf-8")
        if "知识库 webapp/" in t:
            f.write_text(t.replace("知识库 webapp/", "测试包 webapp/"),
                         encoding="utf-8")
    for f in ROOT.glob("packs/capabilities/web/rules/redlines.md"):
        t = f.read_text(encoding="utf-8")
        if "知识库 webapp/" in t:
            f.write_text(t.replace("知识库 webapp/", "测试包 webapp/"),
                         encoding="utf-8")


def main() -> None:
    print("== K5 测试包分类迁移 ==")
    missing = [s for s in STRIKE_KB_REMAP
               if not (STAY_DIR / f"{s}.md").is_file()]
    moved = [s for s in STRIKE_KB_REMAP
             if (KB_ROOT / _new_module(s)).is_file()]
    if moved:
        print(f"[警告] 已存在新路径 {len(moved)} 篇（视为已完成，仅补引用改写）")
    print(f"待迁移 {len(STRIKE_KB_REMAP) - len(moved)} 篇；留守 "
          f"{sorted(STRIKE_KB_STAY)}；缺失 {missing or '无'}")
    if missing:
        raise SystemExit("上游文件缺失，中止（映射表与磁盘不一致）")
    print("-- 引用改写 --")
    for f, n in sorted(rewrite_references().items()):
        print(f"  {f}: ~{n} 行")
    print("-- 文件迁移 --")
    for ln in move_manuals():
        print(ln)
    print("-- 叙述性补改 --")
    touch_up_narratives()
    remain = sorted(p.name for p in STAY_DIR.glob("*.md")
                    if p.is_file())
    print(f"-- 留守 playbooks/知识库/: {remain}")


if __name__ == "__main__":
    main()
