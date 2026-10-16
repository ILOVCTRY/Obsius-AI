"""生成 skill references 副本 → kb 正本 的迁移清单。

做法：给 kb 树（排除 skills/）建 sha256 → 路径 索引；再逐个扫描
packs/kb/skills/*/references/ 下的文件，命中同哈希即「字节相同副本」，可删、
并把 skill 表里的 references/... 路径改写为 kb 正本的 module 口径路径。
未命中的 = skill 独有，需人工判定去留。
"""
from __future__ import annotations
import hashlib
import pathlib
import re
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
KB = ROOT / "packs" / "kb"
SKILLS = KB / "skills"
OUT = ROOT / "scratch" / "kb-skill-migration.md"


def sha(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


# ---- 1) kb 正本索引（排除 skills/ 与 .history）----
kb_index: dict[str, list[str]] = defaultdict(list)
for f in KB.rglob("*"):
    if not f.is_file():
        continue
    parts = f.relative_to(KB).parts
    if parts[0] == "skills" or ".history" in parts:
        continue
    kb_index[sha(f)].append("/".join(parts))

# ---- 2) 逐 skill 扫描 references ----
rows: list[tuple[str, str, str, str, str]] = []  # skill, skill_rel, status, kb_path, note
summary: dict[str, int] = defaultdict(int)

for sk in sorted(SKILLS.glob("*/SKILL.md")):
    name = sk.parent.name
    refs = sk.parent / "references"
    if not refs.is_dir():
        continue
    for f in sorted(refs.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(sk.parent).as_posix()
        hits = kb_index.get(sha(f), [])
        if hits:
            # 优先挑与 skill 名/方向同族的正本
            hits_sorted = sorted(hits, key=lambda p: (len(p), p))
            best = hits_sorted[0]
            extra = "" if len(hits) == 1 else f"（+{len(hits)-1} 处同内容）"
            rows.append((name, rel, "DUP", best, extra))
            summary["DUP"] += 1
        else:
            rows.append((name, rel, "SKILL_ONLY", "", ""))
            summary["SKILL_ONLY"] += 1

# ---- 3) skill 表里引用的路径（口径体检）----
PATH_RE = re.compile(r"`([^`\s]+\.md)`")
path_rows: list[tuple[str, str, str]] = []
for sk in sorted(SKILLS.glob("*/SKILL.md")):
    name = sk.parent.name
    text = sk.read_text(encoding="utf-8")
    seen = set()
    for m in PATH_RE.finditer(text):
        p = m.group(1)
        if p in seen:
            continue
        seen.add(p)
        in_skill = (sk.parent / p).is_file()
        in_kb = (KB / p).is_file()
        if in_skill and not in_kb:
            verdict = "SKILL_LOCAL（doctor 看不见，非快照名打头）"
        elif in_kb and not in_skill:
            verdict = "KB（已合规）"
        elif in_kb and in_skill:
            verdict = "BOTH（双份）"
        else:
            verdict = "NEITHER（断链）"
        path_rows.append((name, p, verdict))

# ---- 4) 输出 ----
lines: list[str] = []
lines.append("# skill references 副本 → kb 正本 迁移清单\n")
lines.append(f"kb 正本 md 索引：{sum(len(v) for v in kb_index.values())} 个文件 / "
             f"{len(kb_index)} 个不同内容\n")
lines.append(f"skill 副本：DUP（字节相同，可删）= {summary['DUP']}，"
             f"SKILL_ONLY（独有，需判定）= {summary['SKILL_ONLY']}\n")

lines.append("\n## A. 字节相同副本（可直接删，路径改写为 kb 正本）\n")
lines.append("| skill | 副本路径 | → kb 正本 | 备注 |")
lines.append("|---|---|---|---|")
for r in rows:
    if r[2] == "DUP":
        lines.append(f"| {r[0]} | `{r[1]}` | `{r[3]}` | {r[4]} |")

lines.append("\n## B. skill 独有（kb 树无同内容，需判定去留）\n")
lines.append("| skill | 副本路径 |")
lines.append("|---|---|")
for r in rows:
    if r[2] == "SKILL_ONLY":
        lines.append(f"| {r[0]} | `{r[1]}` |")

lines.append("\n## C. SKILL.md 表内路径口径体检\n")
lines.append("| skill | 路径 | 判定 |")
lines.append("|---|---|---|")
for r in path_rows:
    lines.append(f"| {r[0]} | `{r[1]}` | {r[2]} |")

OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("wrote", OUT)
print("summary:", dict(summary))
print("path verdicts:", {v: sum(1 for r in path_rows if r[2] == v) for v in
      {r[2] for r in path_rows}})
