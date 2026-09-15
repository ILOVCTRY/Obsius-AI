"""kb 引用扫描与改名联动（只读扫描 + 受控改写，C3）。

doctor 历史上认两种引用形态，本模块把它们统一成一个共享扫描器：

1. 显式调用：``kb_open(module="ctf-web/foo/bar.md")``；
2. 引号/反引号里的完整快照路径：`` `ctf-web/foo/bar.md` ``（仅当第一段是
   已导入快照名才认，普通路径不误报）。

扫描范围：全部技能 SKILL.md + 各能力包 kb 源内的 *.md（排除 .history）。
改名时对引用文件做**完整路径字面替换**，每个被改文件各自留备份；
相对链接（``](./x.md)`` 等）无法安全自动改写，列入 skipped_relative 由人处理。
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from core.skills import writing
from core.skills.registry import SkillRegistry
from core.skills.rules import load_kb_sources

_MODULE_SUFFIXES = r"(?:md|py|sh|txt|yaml|yml|json)"
_KBOPEN_RE = re.compile(r"kb_open\s*\(\s*module\s*=\s*[\"']([^\"']+)[\"']")
_QUOTED_REF_TMPL = r"""[`'\"]({snaps})/[^\s`'\"\）)]+\.(?:{suffixes})[`'\"]"""
_MD_LINK_RE = re.compile(r"\]\((\.\.?/[^)#\s]+\.md)(?:#[^)]*)?\)")


def snapshot_index(root: str | Path) -> dict[str, str]:
    """扫 capabilities/<cap>/kb/* 建 {快照顶层目录名: cap}（不硬编码清单，新增自动生效）。

    kb_sources 声明的其它 root 一并纳入（root 目录名→cap）。"""
    root = Path(root)
    index: dict[str, str] = {}
    caps = root / "capabilities"
    if not caps.is_dir():
        return index
    for cap_dir in sorted(p for p in caps.iterdir() if p.is_dir()):
        for src in load_kb_sources(root, [cap_dir.name]):
            if src.root.is_dir():
                for snap in sorted(p for p in src.root.iterdir() if p.is_dir()
                                   and p.name != ".history"):
                    index.setdefault(snap.name, cap_dir.name)
    return index


def _quoted_pattern(snaps: dict[str, str]) -> re.Pattern:
    return re.compile(_QUOTED_REF_TMPL.format(
        snaps="|".join(re.escape(s) for s in sorted(snaps, key=len, reverse=True)),
        suffixes=_MODULE_SUFFIXES))


def classify_module(module: str, snaps: dict[str, str],
                    caps_root: Path) -> tuple[str, str] | None:
    """单个模块引用 → (code, message)；有效引用返回 None（doctor 复用）。"""
    snap, _, rel = module.partition("/")
    if not rel:
        return None
    cap = snaps.get(snap)
    if cap is None:
        return ("kb-snapshot-unknown",
                f"快照未导入任何能力包的 kb/：{module}")
    if not (caps_root / cap / "kb" / snap / rel).is_file():
        return ("kb-module-broken",
                f"引用的 kb 模块不存在：{module}（应在 capabilities/{cap}/kb/）")
    return None


def extract_modules(text: str, snaps: dict[str, str]) -> list[str]:
    """从一段文档文本中抽出全部 kb 模块引用（去重保序，两种形态合一）。"""
    pat = _quoted_pattern(snaps)
    modules = [m.group(1) for m in _KBOPEN_RE.finditer(text)]
    modules += [m.group(0)[1:-1] for m in pat.finditer(text)]
    out: list[str] = []
    seen: set[str] = set()
    for module in modules:
        if module not in seen:
            seen.add(module)
            out.append(module)
    return out


def _kb_sources(root: Path) -> list[tuple[str, Path, bool]]:
    """[(cap, source_root, recursive)]，跨全部能力包（kb 文档互相引用时定位归属）。"""
    out = []
    caps = root / "capabilities"
    if caps.is_dir():
        for cap_dir in sorted(p for p in caps.iterdir() if p.is_dir()):
            for src in load_kb_sources(root, [cap_dir.name]):
                out.append((cap_dir.name, src.root, src.recursive))
    return out


def _iter_doc_files(root: Path):
    """生成 (path, kind, cap, rel)：kind=skill|kb；排除 .history。"""
    reg = SkillRegistry(root)
    reg.load()
    for sk in reg.all():
        yield sk.path, "skill", None, None
    for cap, src_root, recursive in _kb_sources(root):
        if not src_root.is_dir():
            continue
        iterator = src_root.rglob("*.md") if recursive else src_root.glob("*.md")
        for f in sorted(iterator):
            try:
                rel = f.relative_to(src_root)
            except ValueError:
                continue
            if ".history" in rel.parts:
                continue
            yield f, "kb", cap, rel


@dataclass
class RefHit:
    file: str          # packs 相对 posix
    kind: str          # skill | kb
    line: int          # 首次命中行（1 基；0 表示未定位）
    forms: list[str]   # 命中形态：kb_open | quoted

    def to_dict(self) -> dict:
        return {"file": self.file, "kind": self.kind, "line": self.line,
                "forms": self.forms}


def _rel_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def find_module_refs(packs_root: str | Path, module: str) -> list[RefHit]:
    """全仓扫描：哪些技能/kb 文档引用了指定模块（完整路径，两种形态都算）。"""
    root = Path(packs_root)
    snaps = snapshot_index(root)
    hits: list[RefHit] = []
    for f, kind, _cap, _rel in _iter_doc_files(root):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if module not in text:
            continue
        forms: list[str] = []
        line = 0
        for i, raw in enumerate(text.splitlines(), 1):
            if module in raw:
                line = line or i
                if _KBOPEN_RE.search(raw) and "kb_open" not in forms:
                    forms.append("kb_open")
        if line and not forms:
            forms.append("quoted")
        # 两种形态分别在不同行时补齐
        if module in text and _quoted_pattern(snaps).search(text) and "quoted" not in forms:
            forms.append("quoted")
        hits.append(RefHit(_rel_posix(f, root), kind, line, forms or ["quoted"]))
    return hits


def _relative_link_owners(root: Path, old_abs: Path) -> list[str]:
    """找用相对 markdown 链接指向旧文件的 kb 文档（这些无法整串替换，只能跳过）。"""
    skipped: list[str] = []
    old_abs = old_abs.resolve()
    for f, kind, _cap, _rel in _iter_doc_files(root):
        if kind != "kb":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _MD_LINK_RE.finditer(text):
            target = (f.parent / m.group(1)).resolve()
            if target == old_abs:
                skipped.append(_rel_posix(f, root))
                break
    return skipped


def rewrite_module(packs_root: str | Path, cap: str,
                   old_module: str, new_module: str) -> dict:
    """改名联动：在所有引用文件里把旧完整路径整串替换为新路径，被改文件各自备份。

    必须在 pack_write_lock 临界区内由端点连同 rename_kb_move 一起调用
    （本函数自身也取同一把 RLock，同线程可重入）。返回
    ``{updated:[{file, replacements}], skipped_relative:[files]}``。"""
    root = Path(packs_root)
    old_abs = writing.resolve_kb(root, cap, old_module).path
    updated: list[dict] = []
    with writing.pack_write_lock():
        for f, kind, fcap, frel in _iter_doc_files(root):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            count = text.count(old_module)
            if not count:
                continue
            if kind == "skill":
                writing.backup_history(f)
            else:
                _backup_kb_doc(root, fcap, frel)
            f.write_text(text.replace(old_module, new_module), encoding="utf-8")
            updated.append({"file": _rel_posix(f, root), "replacements": count})
        skipped = _relative_link_owners(root, old_abs)
    return {"updated": updated, "skipped_relative": skipped}


def _backup_kb_doc(root: Path, cap: str, rel: Path) -> str | None:
    """给 kb 文档留 kb-backups 布局的备份（与手工改写同一版本体系）。"""
    target = writing.resolve_kb(root, cap, rel.as_posix())
    if not target.path.is_file():
        return None
    dest = writing._kb_backup_path(target)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target.path, dest)
    return dest.name
