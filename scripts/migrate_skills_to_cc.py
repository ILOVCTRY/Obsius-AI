"""把旧的「技能薄路由 + packs/kb」迁移为 cc 风格自包含技能。

默认 dry-run：扫描 capabilities/*/skills/* 和 tracks/*/skills/*，找出 SKILL.md
中能解析到 packs/kb 的文件引用。使用 --apply 时：

* 资料复制到技能目录 references/<原路径>；
* SKILL.md 中的路径改为 references/<原路径>；
* kb_open(module=...) 改成 skill_open(path=...)；
* frontmatter 写入 mode: self-contained；
* 修改前把 SKILL.md 备份到同目录 .history/。

脚本只复制明确被技能引用的文件，不删除 packs/kb。没有匹配到的旧引用会
报告出来，供人工处理。它可以重复运行，已存在且内容相同的资源不会重写。
"""

from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path


_SKILL_GLOBS = ("capabilities/*/skills/*/SKILL.md", "tracks/*/skills/*/SKILL.md")
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_-]+(?:/[A-Za-z0-9_.一-鿿-]+)+\.(?:md|txt|py|json))"
)
_KB_OPEN_RE = re.compile(r"kb_open\s*\(\s*module\s*=\s*([\"'`])([^\"'`]+)\1\s*\)")


def _skills(root: Path, only: str | None) -> list[Path]:
    paths = [p for pattern in _SKILL_GLOBS for p in root.glob(pattern)]
    if only:
        paths = [p for p in paths if p.parent.name == only]
    return sorted(set(paths))


def _frontmatter_mode(text: str) -> str:
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    m = re.search(r"^mode:\s*(\S+)\s*$", parts[1], re.MULTILINE)
    return m.group(1).strip().lower() if m else ""


def _with_mode(text: str) -> str:
    if not text.startswith("---"):
        return "---\nmode: self-contained\n---\n\n" + text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    if re.search(r"^mode:\s*", parts[1], re.MULTILINE):
        return text
    return "---" + parts[1].rstrip() + "\nmode: self-contained\n---" + parts[2]


def _resolve(root: Path, skill: Path, ref: str) -> tuple[Path, str] | None:
    """Resolve global <cap>/... or legacy pack-relative reference."""
    candidates = [root / "kb" / ref]
    pack = skill.relative_to(root).parts[1]
    if "/" not in ref or not (root / "kb" / ref.split("/", 1)[0]).is_dir():
        candidates.append(root / "kb" / pack / ref)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        kb_root = (root / "kb").resolve()
        if kb_root in resolved.parents and resolved.is_file():
            return resolved, resolved.relative_to(kb_root).as_posix()
    return None


def migrate_skill(root: Path, skill_file: Path, apply: bool) -> tuple[int, list[str], bool]:
    text = skill_file.read_text(encoding="utf-8")
    pack = skill_file.relative_to(root).parts[1]

    # 首次迁移的旧版本曾把域内短路径写成 references/android/...，而资源
    # 实际按全局 kb 规范落在 references/binary/android/...。启动时顺手归一，
    # 让脚本可重复运行且不留下失效链接。
    def normalize_ref(match: re.Match[str]) -> str:
        rel = match.group(1)
        direct = skill_file.parent / "references" / rel
        qualified = skill_file.parent / "references" / pack / rel
        if direct.is_file():
            return match.group(0)
        if qualified.is_file():
            return f"references/{pack}/{rel}"
        return match.group(0)

    text = re.sub(r"references/((?!kb/)[A-Za-z0-9_.一-鿿/-]+\.(?:md|txt|py|json))",
                  normalize_ref, text)
    refs: set[str] = set()
    for match in _PATH_RE.finditer(text):
        if _resolve(root, skill_file, match.group(1)):
            refs.add(match.group(1))
    for match in _KB_OPEN_RE.finditer(text):
        if _resolve(root, skill_file, match.group(2)):
            refs.add(match.group(2))
    missing = []
    for match in _KB_OPEN_RE.finditer(text):
        if not _resolve(root, skill_file, match.group(2)):
            missing.append(match.group(2))
    if not refs and _frontmatter_mode(text) == "self-contained":
        return 0, missing, False
    changed = bool(refs) or _frontmatter_mode(text) != "self-contained"
    if not apply:
        return len(refs), missing, changed

    resource_root = skill_file.parent / "references"
    resolved_refs: dict[str, str] = {}
    for ref in sorted(refs):
        resolved = _resolve(root, skill_file, ref)
        if resolved is None:
            continue
        source, global_ref = resolved
        resolved_refs[ref] = global_ref
        target = resource_root / global_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != source.read_bytes():
            shutil.copy2(source, target)

    # 先改调用，再改普通路径引用，避免 module 参数先被替换后无法匹配。
    text = _KB_OPEN_RE.sub(
        lambda m: f"skill_open(path=\"references/{resolved_refs.get(m.group(2), m.group(2))}\")",
        text)
    for ref in sorted(resolved_refs, key=len, reverse=True):
        text = re.sub(rf"(?<!references/){re.escape(ref)}",
                      f"references/{resolved_refs[ref]}", text)
    text = _with_mode(text)

    history = skill_file.parent / ".history"
    history.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = history / f"{stamp}_SKILL.md"
    suffix = 1
    while backup.exists():
        backup = history / f"{stamp}.{suffix}_SKILL.md"
        suffix += 1
    shutil.copy2(skill_file, backup)
    skill_file.write_text(text, encoding="utf-8")
    return len(refs), missing, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packs", type=Path, default=Path(__file__).resolve().parents[1] / "packs")
    parser.add_argument("--skill", help="只迁移指定技能目录名")
    parser.add_argument("--apply", action="store_true", help="写入迁移结果；缺省只预览")
    args = parser.parse_args()
    root = args.packs.resolve()
    total = 0
    for skill_file in _skills(root, args.skill):
        count, missing, changed = migrate_skill(root, skill_file, args.apply)
        if count or changed or missing:
            action = "迁移" if args.apply else "发现"
            print(f"[{action}] {skill_file.relative_to(root)}: resources={count}")
            for item in missing:
                print(f"  [未解析引用] {item}")
            total += count
    print(f"总计资源引用: {total}；模式={'apply' if args.apply else 'dry-run'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
