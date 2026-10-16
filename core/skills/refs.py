"""kb 引用扫描与改名联动（只读扫描 + 受控改写，C3；M0 全局形态 2026-09-21）。

doctor 历史上认两种引用形态，本模块把它们统一成一个共享扫描器：

1. 显式调用：``kb_open(module="web/漏洞分类/idor/手册.md")``；
2. 引号/反引号里的完整路径：`` `web/漏洞分类/idor/手册.md` ``（仅当第一段是
   域名或已导入快照名才认，普通路径不误报）；
3. Obsidian wikilink：``[[web/漏洞分类/idor/手册|别名]]`` / ``[[web/…/手册]]``
   （2026-10-14，vault 已切 `newLinkFormat=absolute`，Obsidian 落盘即全路径
   wikilink）。目标可省扩展名，按 ``.md`` 补全；``#锚点`` 与 ``|别名`` 仅作
   展示层，不参与解析。**只认全路径**——裸名 wikilink（``[[手册]]``）在 kb 里
   天然歧义（52 个 `手册.md`），一律不解析、不报错。

扫描范围：全部技能 SKILL.md + packs/kb/<域>/ 源内的 *.md（排除 .history）。
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
from core.skills.taxonomy import list_directions

_MODULE_SUFFIXES = r"(?:md|py|sh|txt|yaml|yml|json)"
_KBOPEN_RE = re.compile(r"kb_open\s*\(\s*module\s*=\s*[\"']([^\"']+)[\"']")
_QUOTED_REF_TMPL = r"""[`'\"]({snaps})/[^\s`'\"\）)]+\.(?:{suffixes})[`'\"]"""
_MD_LINK_RE = re.compile(r"\]\((\.\.?/[^)#\s]+\.md)(?:#[^)]*)?\)")
# Obsidian wikilink：目标 + 可选锚点 + 可选别名
_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_KNOWN_EXT_RE = re.compile(r"\.(?:%s)$" % _MODULE_SUFFIXES, re.I)


def snapshot_index(root: str | Path) -> dict[str, str]:
    """扫 packs/kb/<域>/* 建 {首段名: 域}（expert-pool M0）。

    快照顶层目录名→域 + 域名→自身（后者让引用扫描/正则认得全局形态
    `<域>/…` 打头的引用）；同名跨域取首见——旧形态引用的消歧主要靠
    classify_module 的 prefer_cap（引用方域优先）。"""
    root = Path(root)
    index: dict[str, str] = {}
    kb = root / "kb"
    if not kb.is_dir():
        return index
    for dom_dir in sorted(p for p in kb.iterdir()
                          if p.is_dir() and p.name != "licenses"):
        for snap in sorted(p for p in dom_dir.iterdir() if p.is_dir()
                           and p.name != ".history"):
            index.setdefault(snap.name, dom_dir.name)
        index.setdefault(dom_dir.name, dom_dir.name)
    return index


def _quoted_pattern(snaps: dict[str, str]) -> re.Pattern:
    return re.compile(_QUOTED_REF_TMPL.format(
        snaps="|".join(re.escape(s) for s in sorted(snaps, key=len, reverse=True)),
        suffixes=_MODULE_SUFFIXES))


def wikilink_target(inner: str) -> str:
    """wikilink 目标 → kb module 路径。省扩展名时按 ``.md`` 补全。"""
    t = (inner or "").strip()
    if not _KNOWN_EXT_RE.search(t):
        t += ".md"
    return t


def normalize_module(ref: str) -> str:
    """把任意引用形态归一成 kb module 路径（kb_open / doctor 两侧共用）。

    认：反引号/单双引号包裹（`` `a/b.md` ``、`` "a/b.md" ``）、Obsidian
    wikilink（``[[a/b/c|别名]]``）。其余原样返回（首尾空白与 ``/`` 归一）。
    wikilink 目标可省扩展名；**非 wikilink 形态不补扩展名**——那会掩盖原有的
    「模块不存在」错误面。
    """
    s = (ref or "").strip().strip("`'\"").strip()
    m = _WIKILINK_RE.search(s)
    if m:
        s = wikilink_target(m.group(1))
    return s.replace("\\", "/").lstrip("/")


def classify_module(module: str, snaps: dict[str, str],
                    packs_root: Path, prefer_cap: str | None = None,
                    ) -> tuple[str, str] | None:
    """单个模块引用 → (code, message)；有效引用返回 None（doctor 复用）。

    M0 全局形态 `<域>/<快照>/<路径>` 与旧快照形态 `<快照>/<路径>` 都认：
    候选域顺序 = 引用方域（prefer_cap）→ 首段若为域目录 → snaps 消歧域；
    逐域走 writing.resolve_kb（剥域/域内相对/存在性同口径）。"""
    module = normalize_module(module)
    first, _, rel = module.partition("/")
    if not rel:
        return None
    root = Path(packs_root)
    doms = list_directions(root)
    order: list[str] = []
    for d in (prefer_cap, first if first in doms else None,
              snaps.get(first)):
        if d and d in doms and d not in order:
            order.append(d)
    if not order:
        return ("kb-snapshot-unknown",
                f"快照未导入任何能力域的 kb/：{module}")
    for dom in order:
        try:
            if writing.resolve_kb(root, dom, module).path.is_file():
                return None
        except writing.KbError:
            continue
    return ("kb-module-broken",
            f"引用的 kb 模块不存在：{module}")


def extract_modules(text: str, snaps: dict[str, str]) -> list[str]:
    """从一段文档文本中抽出全部 kb 模块引用（去重保序，三种形态合一）。"""
    pat = _quoted_pattern(snaps)
    modules = [m.group(1) for m in _KBOPEN_RE.finditer(text)]
    modules += [m.group(0)[1:-1] for m in pat.finditer(text)]
    modules += [wikilink_target(m.group(1))
                for m in _WIKILINK_RE.finditer(text)]
    out: list[str] = []
    seen: set[str] = set()
    for module in modules:
        if module not in seen:
            seen.add(module)
            out.append(module)
    return out


def _kb_sources(root: Path) -> list[tuple[str, Path, bool]]:
    """[(方向, source_root, recursive)]，跨全部 kb 方向（kb 文档互相引用时定位归属）。"""
    out = []
    for direction in list_directions(root):
        for src in load_kb_sources(root, [direction]):
            out.append((direction, src.root, src.recursive))
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
    forms: list[str]   # 命中形态：kb_open | quoted | wikilink

    def to_dict(self) -> dict:
        return {"file": self.file, "kind": self.kind, "line": self.line,
                "forms": self.forms}


def _rel_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def find_module_refs(packs_root: str | Path, module: str) -> list[RefHit]:
    """全仓扫描：哪些技能/kb 文档引用了指定模块（完整路径，三种形态都算）。"""
    root = Path(packs_root)
    snaps = snapshot_index(root)
    hits: list[RefHit] = []
    for f, kind, _cap, _rel in _iter_doc_files(root):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        # 预筛：字面命中，或有某个 wikilink 归一后命中（否则整份文件跳过）
        if module not in text and not any(
                wikilink_target(m.group(1)) == module
                for m in _WIKILINK_RE.finditer(text)):
            continue
        forms: list[str] = []
        line = 0
        for i, raw in enumerate(text.splitlines(), 1):
            links = {wikilink_target(m.group(1))
                     for m in _WIKILINK_RE.finditer(raw)}
            hit = module in raw or module in links
            if not hit:
                continue
            line = line or i
            if _KBOPEN_RE.search(raw):
                if "kb_open" not in forms:
                    forms.append("kb_open")
            elif module in links:
                if "wikilink" not in forms:
                    forms.append("wikilink")
        if not line:
            continue
        if not forms:
            forms.append("quoted")
        hits.append(RefHit(_rel_posix(f, root), kind, line, forms))
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


def _splice_wikilinks(text: str, old_module: str, new_module: str) -> tuple[str, int]:
    """把指向 old_module 的 wikilink 目标换成 new_module，保留锚点/别名。

    ``[[web/a/手册|手册]]`` → ``[[web/a/手册二|手册]]``（目标省扩展名也认）。
    返回 (新文本, 替换数)。"""
    n = 0

    def _rep(m: re.Match) -> str:
        nonlocal n
        whole = m.group(0)
        target = m.group(1)
        if wikilink_target(target) != old_module:
            return whole
        n += 1
        return whole.replace(target, new_module, 1)

    return _WIKILINK_RE.sub(_rep, text), n


def rewrite_module(packs_root: str | Path, cap: str,
                   old_module: str, new_module: str) -> dict:
    """改名联动：在所有引用文件里把旧完整路径整串替换为新路径，被改文件各自备份。

    Wikilink 目标单独 splice（保留别名/锚点），其余走整串字面替换。
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
            text, n_links = _splice_wikilinks(text, old_module, new_module)
            count = text.count(old_module)
            if not count and not n_links:
                continue
            text = text.replace(old_module, new_module)
            if kind == "skill":
                writing.backup_history(f)
            else:
                _backup_kb_doc(root, fcap, frel)
            f.write_text(text, encoding="utf-8")
            updated.append({"file": _rel_posix(f, root),
                            "replacements": count + n_links})
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
