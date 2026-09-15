"""知识快照一次性导入（DESIGN.md §4.5）。

把 Knowledge/ 下两套外部知识源原样融入正交包布局（快照不翻译、不进 registry、
不就地修改；AI 只能经 kb_open 按需打开）：

- ctf-skills（MIT，Lukasz Jagiello）：按类别拆散进各能力包 kb/<snapshot>/，
  根 LICENSE 复制为每包 kb/CTF-SKILLS-LICENSE。
- src-strike（内部知识源，无 LICENSE，按项目主授权使用）：拍平
  skills/src-strike/* → capabilities/web/kb/src-strike/，rules/ 17 篇随快照保存；
  其中 4 篇平台规则的完整版覆盖 tracks/assessment/rules/owners/（覆盖前自动
  备份到该目录 .history/）。

幂等：快照目录已存在默认跳过，--force 删除重建；生成文件（kb_sources.json、
kb/README.md、LICENSE、owners）每次按当前源状态刷新。

用法：
  E:\\Miniconda3\\python.exe scripts/import_kb.py [--knowledge Knowledge]
      [--packs packs] [--force] [--no-owners]
"""
from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ctf-skills/<技能目录> -> (能力包, 快照目录名)
CTF_MAP: list[tuple[str, str, str]] = [
    ("ctf-web", "web", "ctf-web"),
    ("ctf-pwn", "binary", "ctf-pwn"),
    ("ctf-reverse", "binary", "ctf-reverse"),
    ("ctf-malware", "binary", "ctf-malware"),
    ("ctf-crypto", "crypto", "ctf-crypto"),
    ("ctf-forensics", "forensics", "ctf-forensics"),
    ("ctf-osint", "forensics", "ctf-osint"),
    ("ctf-misc", "misc", "ctf-misc"),
    ("ctf-ai-ml", "misc", "ctf-ai-ml"),
    ("ctf-writeup", "misc", "ctf-writeup"),
]

# src-strike rules/<文件> -> assessment 轨 owners/<tag>.md（完整版覆盖摘编版）
OWNER_RULES: list[tuple[str, str]] = [
    ("edusrc-rules.md", "edusrc.md"),
    ("edu-rating-rules.md", "edu-rating.md"),
    ("osrc-oppo-rules.md", "osrc.md"),
    ("ysrc-ezviz-rules.md", "ysrc.md"),
]

# src-strike 快照排除（仓库脚手架/外部 agent 指令/重型运行时，均无知识价值或有
# 指令冲突风险——kb 内容会被 kb_open 喂给 Agent，宁严勿松）
STRIKE_EXCLUDE_TOP = {
    ".git", ".claude", "mcp-servers", "tools", "资产", "__pycache__",
}
STRIKE_EXCLUDE_FILES = {
    "AGENTS.md", "CLAUDE.md", ".mcp.json", ".gitignore",
}
COPY_EXCLUDE_DIRS = {".git", "__pycache__", "target", ".pytest_cache", "node_modules"}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _ignore_factory(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in COPY_EXCLUDE_DIRS}


def _safe_copy_factory(stats: dict):
    """逐文件容错复制：单个文件打不开（如杀软实时隔离了 shellcode 生成器）
    只记录不抛出，避免整个快照导入失败、其余内容静默缺失。"""
    def _copy(src, dst, *args, **kwargs):
        try:
            shutil.copy2(src, dst, *args, **kwargs)
        except OSError as e:
            stats["copy_errors"].append((str(src), str(e)))
            _log(f"  [警告] 单文件复制失败（已跳过，见摘要）: {src} —— {e}")
    return _copy


def _expected_upstream(rel: Path, roots: list[tuple[Path, str]]) -> bool:
    """dst 内相对文件 rel 是否来自上游。roots=[(上游根, dst 内前缀)]，
    前缀 "" 表示整棵同构。本地新增（含 kb API 新建经验 md）与 .history 不在此列。"""
    for src_root, prefix in roots:
        if prefix == "":
            if (src_root / rel).is_file():
                return True
        elif rel.parts and rel.parts[0] == prefix and len(rel.parts) > 1:
            if (src_root / Path(*rel.parts[1:])).is_file():
                return True
    return False


def _preserve_local(dst: Path, roots: list[tuple[Path, str]],
                    stats: dict) -> tempfile.TemporaryDirectory | None:
    """--force 重建前把 .history（本地版本/回收站）与上游没有的本地新增文件移到临时区。"""
    if not dst.is_dir():
        return None
    tmp = tempfile.TemporaryDirectory(prefix=".kb-preserve-", dir=str(dst.parent))
    n = 0
    for f in sorted(dst.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(dst)
        if rel.parts[0] == ".history" or not _expected_upstream(rel, roots):
            keep = Path(tmp.name) / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, keep)
            n += 1
    if n:
        stats.setdefault("preserved", []).append((str(dst), n))
        _log(f"  保护本地新增/历史版本 {n} 个文件，重建后还原: {dst}")
    return tmp


def _restore_local(tmp: tempfile.TemporaryDirectory | None, dst: Path) -> None:
    if tmp is None:
        return
    root = Path(tmp.name)
    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if f.is_file():
                target = dst / f.relative_to(root)
                if not target.exists():  # 上游同路径文件不被本地副本盖回
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
    tmp.cleanup()


def _copy_snapshot(src: Path, dst: Path, force: bool, stats: dict) -> None:
    """复制一个快照目录；已存在则跳过（force 先删后拷，本地新增与 .history 自动保全）。"""
    preserved = None
    if dst.exists():
        if not force:
            stats["skipped"].append(str(dst))
            _log(f"  跳过（已存在，--force 可重建）: {dst}")
            return
        preserved = _preserve_local(dst, [(src, "")], stats)
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=_ignore_factory,
                    copy_function=_safe_copy_factory(stats))
    _restore_local(preserved, dst)
    stats["copied"].append(str(dst))
    _log(f"  复制: {src.name} -> {dst}")


def _find_ctf_root(knowledge: Path) -> Path | None:
    """源仓库实际多嵌套一层 ctf-skills-main/。"""
    for cand in (knowledge / "ctf-skills-main" / "ctf-skills-main",
                 knowledge / "ctf-skills-main"):
        if cand.is_dir() and (cand / "ctf-web").is_dir():
            return cand
    return None


def _find_strike_root(knowledge: Path, external_fallback: bool = True) -> Path | None:
    """Knowledge/src-strike 优先；不在则用 E 盘原始副本兜底（测试可关）。"""
    cands = [knowledge / "src-strike"]
    if external_fallback:
        cands.append(Path(r"E:\ILOVCTRY\SRC\src-strike"))
    for cand in cands:
        if cand.is_dir() and (cand / "skills" / "src-strike").is_dir():
            return cand
    return None


def _import_ctf(ctf_root: Path, packs: Path, force: bool, stats: dict) -> set[str]:
    """导入 ctf-skills；返回实际落到内容的能力包集合。"""
    caps: set[str] = set()
    _log("[ctf-skills] 开始")
    for src_name, cap, snap_name in CTF_MAP:
        src = ctf_root / src_name
        if not src.is_dir():
            _log(f"  [警告] 源缺失，跳过: {src}")
            continue
        _copy_snapshot(src, packs / "capabilities" / cap / "kb" / snap_name,
                       force, stats)
        caps.add(cap)
    # 每包附根 LICENSE（同一 MIT 文本，覆盖刷新）
    license_src = ctf_root / "LICENSE"
    if license_src.is_file():
        for cap in caps:
            kb = packs / "capabilities" / cap / "kb"
            shutil.copyfile(license_src, kb / "CTF-SKILLS-LICENSE")
        stats["license_caps"] = sorted(caps)
    return caps


def _import_strike(strike_root: Path, packs: Path, force: bool,
                   stats: dict, import_owners: bool) -> None:
    """拍平导入 src-strike 到 web/kb/src-strike/。"""
    _log("[src-strike] 开始")
    dst = packs / "capabilities" / "web" / "kb" / "src-strike"
    staged: list[tuple[Path, Path]] = []  # (src, dst) 先收集后统一落盘

    skill_root = strike_root / "skills" / "src-strike"
    for item in sorted(skill_root.iterdir()):
        if item.name in COPY_EXCLUDE_DIRS:
            continue
        staged.append((item, dst / item.name))

    rules_src = strike_root / "rules"
    if rules_src.is_dir():
        staged.append((rules_src, dst / "rules"))

    exists = dst.exists()
    if exists and not force:
        stats["skipped"].append(str(dst))
        _log(f"  跳过（已存在，--force 可重建）: {dst}")
    else:
        # 上游同构根：skills/src-strike/* 拍平到 dst/*、rules/ 整目录到 dst/rules
        upstream_roots = [(skill_root, ""), (rules_src, "rules")]
        preserved = _preserve_local(dst, upstream_roots, stats) if exists else None
        if exists:
            shutil.rmtree(dst)
        dst.mkdir(parents=True, exist_ok=True)
        for src, target in staged:
            if src.is_dir():
                shutil.copytree(src, target, ignore=_ignore_factory,
                                copy_function=_safe_copy_factory(stats))
            else:
                shutil.copyfile(src, target)
            _log(f"  复制: {src} -> {target}")
        _restore_local(preserved, dst)
        stats["copied"].append(str(dst))

    # 平台规则完整版 -> assessment 轨 owners/（不随 --force 跳过，独立幂等）
    if import_owners:
        _merge_owners(rules_src, packs, stats)


def _merge_owners(rules_src: Path, packs: Path, stats: dict) -> None:
    owners_dir = packs / "tracks" / "assessment" / "rules" / "owners"
    history = owners_dir / ".history"
    for src_name, dst_name in OWNER_RULES:
        src = rules_src / src_name
        dst = owners_dir / dst_name
        if not src.is_file():
            _log(f"  [警告] owner 规则缺失: {src}")
            continue
        if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
            _log(f"  owners/{dst_name} 与源一致，无需覆盖")
            continue
        if dst.is_file():
            history.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = history / f"{dst_name}.{ts}.bak"
            shutil.copyfile(dst, backup)
            _log(f"  owners/{dst_name} 旧版备份 -> {backup}")
        owners_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        stats["owners"].append(dst_name)
        _log(f"  owners/{dst_name} 已用完整版覆盖")


def _write_kb_index(packs: Path, caps: set[str], sources_ok: dict[str, bool]) -> None:
    """每包生成 kb/README.md（中文快照索引，我们自己的维护文件，非快照内容）。"""
    index = {
        "web": [("ctf-web", "ctf-skills（MIT）· Web 题方法论与脚本"),
                ("src-strike", "src-strike 快照（内部知识源）· SRC 方法论/弹药/poc/rules")],
        "binary": [("ctf-pwn", "ctf-skills（MIT）· Pwn"),
                   ("ctf-reverse", "ctf-skills（MIT）· 逆向"),
                   ("ctf-malware", "ctf-skills（MIT）· 恶意样本分析")],
        "crypto": [("ctf-crypto", "ctf-skills（MIT）· 密码学")],
        "forensics": [("ctf-forensics", "ctf-skills（MIT）· 取证/流量/内存"),
                      ("ctf-osint", "ctf-skills（MIT）· OSINT")],
        "misc": [("ctf-misc", "ctf-skills（MIT）· MISC"),
                 ("ctf-ai-ml", "ctf-skills（MIT）· AI/ML 题"),
                 ("ctf-writeup", "ctf-skills（MIT）· writeup 收尾方法论")],
    }
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for cap in caps:
        kb = packs / "capabilities" / cap / "kb"
        lines = [
            f"# {cap} 能力包 · 知识库快照索引",
            "",
            "> 本目录是**外部知识原样快照区**：不进技能 registry、英文快照原文不翻译；",
            "> Agent 只能经 `kb_open(module=<快照名>/<包内相对路径>)` 按需打开单个文件。",
            "> 快照原文更新走 `scripts/import_kb.py` 重新导入。",
            ">",
            "> **本地基线（2026-09-14 起）**：本目录允许在 Skill 页就地修订与新建经验 md",
            "> （新经验写新文件，不覆盖翻译英文原文）；重新导入会保留本地新增文件与",
            "> `.history/` 版本历史，本 README 重写前也自动留版本备份。",
            "",
            f"导入日期（UTC）：{today}",
            "",
            "| 快照 | 来源与内容 |",
            "|---|---|",
        ]
        for name, desc in index.get(cap, []):
            mark = "" if (kb / name).is_dir() else "（本次未导入）"
            lines.append(f"| `{name}/`{mark} | {desc} |")
        lines += [
            "",
            "许可：ctf-skills 快照为 MIT（见 CTF-SKILLS-LICENSE）；src-strike 为内部",
            "知识源快照（无独立 LICENSE），仅限本项目授权使用，不外发。",
            "",
        ]
        readme = kb / "README.md"
        if readme.is_file():  # 重写前留版本到 .history，本地批注可找回
            from core.skills.writing import backup_history, pack_write_lock
            with pack_write_lock():
                backup_history(readme)
        readme.write_text("\n".join(lines), encoding="utf-8")


def _write_kb_sources(packs: Path, caps: set[str]) -> None:
    """每包一个 kb_sources.json：单源 root=kb，module 路径自带快照名前缀天然消歧。"""
    for cap in caps:
        cap_dir = packs / "capabilities" / cap
        if not (cap_dir / "kb").is_dir():
            continue
        data = {"sources": [{"id": f"{cap}-kb", "root": "kb", "recursive": True}]}
        (cap_dir / "kb_sources.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(knowledge: str | Path = "Knowledge", packs: str | Path = "packs",
         force: bool = False, import_owners: bool = True,
         external_fallback: bool = True) -> dict:
    knowledge, packs = Path(knowledge), Path(packs)
    stats: dict = {"copied": [], "skipped": [], "owners": [], "license_caps": [],
                   "copy_errors": []}
    caps: set[str] = set()

    ctf_root = _find_ctf_root(knowledge)
    if ctf_root:
        caps |= _import_ctf(ctf_root, packs, force, stats)
    else:
        _log("[ctf-skills] 未找到源目录，跳过")

    strike_root = _find_strike_root(knowledge, external_fallback)
    if strike_root:
        _import_strike(strike_root, packs, force, stats, import_owners)
        caps.add("web")
    else:
        _log("[src-strike] 未找到源目录（Knowledge/ 与 E:\\ILOVCTRY\\SRC 均无），跳过")

    _write_kb_sources(packs, caps)
    _write_kb_index(packs, caps, {})
    _log("\n========== 导入摘要 ==========")
    _log(f"快照复制 {len(stats['copied'])} 个，跳过 {len(stats['skipped'])} 个；"
         f"owners 覆盖 {stats['owners']}；LICENSE 落包 {stats['license_caps']}")
    if stats["copy_errors"]:
        _log(f"[注意] {len(stats['copy_errors'])} 个文件复制失败（常见原因：杀软实时"
             f"隔离含 shellcode/exp 模板的脚本）：")
        for src, err in stats["copy_errors"]:
            _log(f"  - {src}")
    return stats


def _argv() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="知识快照导入（幂等）")
    ap.add_argument("--knowledge", default="Knowledge", help="知识原料根目录")
    ap.add_argument("--packs", default="packs", help="packs 根目录")
    ap.add_argument("--force", action="store_true",
                    help="快照目录已存在时删除重建（owners 不受此开关影响）")
    ap.add_argument("--no-owners", action="store_true",
                    help="不覆盖 assessment 轨 owners/ 平台规则")
    return ap.parse_args()


if __name__ == "__main__":
    args = _argv()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main(args.knowledge, args.packs, force=args.force,
         import_owners=not args.no_owners)
