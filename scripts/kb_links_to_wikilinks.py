"""kb 文档内链 → Obsidian 全路径 wikilink（[kb-links-to-wikilinks]）。

背景（2026-10-14）：
- `packs/kb` 已注册为 Obsidian vault，`newLinkFormat` 已改成绝对路径 + wikilink，
  Obsidian 今后写入的链接形态是 `[[web/漏洞分类/sqli/手册|手册]]`。
- 但 kb 里现存的是历史 md 相对链接（`](./x.md)`、`](../a/b.md)`、`x.md`）——
  这些链接在 Obsidian 里虽能解析，却是"相对名"，**一旦文件改名就断**，而且与
  Obsidian 新写入的形态不一致（同一 vault 两种口径）。
- 读取层（refs.normalize_module / kb_open / doctor）只认全路径；相对 md 链接
  由 refs._MD_LINK_RE 单独列为 skipped_relative，说明它一直是"不可自动改写"的
  二等公民——本脚本把存量一次性抹平。

口径：
1. **只转能解析的**：目标必须解析成 vault（`packs/kb`）内的真实文件；转不了的
   原样保留并进 broken 报告，绝不猜。
2. **形态**：`[文本](web/a/b.md#锚)` → `[[web/a/b.md#锚|文本]]`；文本缺省取文件名
   主干。目标省不省 `.md` 不影响——`refs.wikilink_target` 会补。
3. **不动这些**：
   - 代码围栏（``` / ~~~）与行内代码（`` `...` ``）里的内容——kb 里有大量
     `[x](y)` 形态的 Lua/Python/正则/JSON 文本，误改即坏档；
   - 图片 `![alt](x.png)`（wikilink 不承载图片）；
   - 外链 / `mailto:` / 纯锚点；
   - `*/题目知识/*` 上游快照原件（与上游 ctf-skills 仓库保持字节一致，
     更新走 `scripts/import_kb.py --force`，改写会被冲掉）；
   - `.obsidian/` 与 `*/.history/`。
4. 默认 dry-run 只出报告；`--apply` 才落盘。

用后跑 `pytest tests/test_kb_links_wikilink.py` 与 `doctor` 复核。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "packs" / "kb"

# 上游快照原件目录名：与上游仓库字节一致，禁止改写
_SKIP_SEGMENTS = {"题目知识"}
# 判"像路径"的宽松规则：有扩展名（.utf8/.p 之类也命中，交给存在性校验兜底）
_PATHISH = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
# 旧布局的技能正文链接：`../../capabilities/<域>/skills/<名>/SKILL.md` 与
# `../../../tracks/<轨>/skills/<名>/SKILL.md`——能力包扁平化后同一份正文在
# `packs/kb/skills/<名>/SKILL.md`（就在 vault 内）。前缀可去、名字不变，故可确定性修复。
_LEGACY_SKILL = re.compile(
    r"^(?:capabilities/[^/]+|tracks/[^/]+|capabilities|tracks)/skills/([^/]+)/SKILL\.md$")


def _iter_docs(kb: Path | None = None):
    """遍历要改写的 vault 文档（跳过上游快照/.obsidian/.history）。"""
    kb = (kb or KB).resolve()
    for p in sorted(kb.rglob("*.md")):
        rel = p.relative_to(kb).as_posix()
        if rel.startswith(".obsidian") or "/.history/" in rel:
            continue
        parts = rel.split("/")
        if any(s in _SKIP_SEGMENTS for s in parts):
            continue
        yield p, rel


def iter_prose(text: str):
    """产出『非代码』的行：排除 ``` / ~~~ 围栏内的行（护栏测试也用它）。"""
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield line


def _convert_line(line: str, src: Path, stats: dict,
                  kb: Path) -> str:
    """转换一行里的 md 链接；行内代码里的链接不动。

    注意：不能简单地把行内代码整段遮掉再找链接——kb 里有大量
    ``[`11-lateral.md`](11-lateral.md)`` 这种**标签本身带行内代码**的链接，
    遮掉代码段就会把 `](target)` 一起遮没，链接漏转。这里改成：先在原文上
    找链接，再把"整条落在行内代码里 / `](` 段落在行内代码里"的链接剔除。
    """
    codes = [(m.start(), m.end()) for m in _INLINE_CODE.finditer(line)]

    def _in_code(a: int, b: int) -> bool:
        return any(cs <= a and b <= ce for cs, ce in codes)

    def _rep(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        target = target.strip("<>")
        path_part = target.partition("#")[0]
        if "://" in target or not path_part:
            return m.group(0)
        if not _PATHISH.search(path_part) \
                and not (src.parent / path_part).is_file():
            return m.group(0)       # 散文里的假链接（Lua .utf8、正则等）
        stats["seen"] += 1
        wl = _to_wikilink(label, target, src, kb)
        if wl is None:
            stats["broken"] += 1
            return m.group(0)
        stats["converted"] += 1
        return wl

    out: list[str] = []
    pos = 0
    for m in _MD_LINK.finditer(line):
        brk = m.start() + m.group(0).index("](")
        if _in_code(m.start(), m.end()) or _in_code(brk, m.end()):
            continue                    # 链接本体在行内代码里 → 不碰
        out.append(line[pos:m.start()])
        out.append(_rep(m))
        pos = m.end()
    out.append(line[pos:])
    return "".join(out)


def _to_wikilink(text: str, target: str, src: Path,
                 kb: Path | None = None) -> str | None:
    """组装 wikilink；解析不到 vault 内文件时返回 None（调用方保留原样）。"""
    kb = (kb or KB).resolve()
    path_part, _, anchor = target.partition("#")
    if not path_part:
        return None
    cand = (src.parent / path_part.lstrip("/")).resolve()
    if not cand.is_file():
        # 旧布局技能正文链接：确定性回填到 skills/<名>/SKILL.md
        m = _LEGACY_SKILL.match(re.sub(r"^(?:\.\./)+", "", path_part))
        if m:
            cand = (kb / "skills" / m.group(1) / "SKILL.md").resolve()
    if not cand.is_file():
        return None
    try:
        vault_rel = cand.relative_to(kb).as_posix()
    except ValueError:
        return None          # 指向 vault 之外，转不了
    if "/.history/" in vault_rel or vault_rel.startswith(".obsidian/"):
        return None
    alias = text.strip().strip("`").strip() or Path(vault_rel).stem
    shown = vault_rel if not anchor else f"{vault_rel}#{anchor}"
    return f"[[{shown}|{alias}]]"


def convert_text(body: str, src: Path, stats: dict,
                 kb: Path | None = None) -> tuple[str, list[str]]:
    """转换单个文件正文 → (新正文, 断链清单)。围栏内的行原样带回。

    行切分必须用 ``split("\n")`` 而不是 ``splitlines()``：后者会把末尾换行
    "吃掉"，再用 ``"\\n".join`` 拼回来就丢了文件尾换行——首轮迁移正是这么
    把几百个文件的 EOF 换行抹掉的（git 会把每个文件都算成改动）。"""
    out_lines: list[str] = []
    broken: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        before = stats["broken"]
        out_lines.append(_convert_line(line, src, stats, kb or KB))
        if stats["broken"] > before:
            broken.append(line.strip()[:120])
    return "\n".join(out_lines), broken


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真正落盘（默认 dry-run 只出报告）")
    args = ap.parse_args()

    stats = {"seen": 0, "converted": 0, "broken": 0}
    touched: list[str] = []
    all_broken: list[str] = []
    for src, rel in _iter_docs():
        before = stats["converted"]
        body = src.read_text(encoding="utf-8")
        new_body, broken = convert_text(body, src, stats)
        if new_body == body:
            continue
        if args.apply:
            src.write_text(new_body, encoding="utf-8")
        touched.append(f"{rel}（{stats['converted'] - before} 条）")
        all_broken.extend(f"{rel}: {b}" for b in broken)

    print(f"模式：{'APPLY（已落盘）' if args.apply else 'DRY-RUN（未改动）'}")
    print(f"参与转换判定 {stats['seen']} 条；转换 {stats['converted']} 条；"
          f"保留/断链 {stats['broken']} 条")
    print(f"涉及文件 {len(touched)} 个")
    if all_broken:
        print(f"\n=== 未转换（解析不到 vault 内文件，需人工）{len(all_broken)} 条 ===")
        for b in all_broken:
            print("  " + b)


if __name__ == "__main__":
    main()
