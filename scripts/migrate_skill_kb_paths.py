"""skill SKILL.md 路径口径迁移：references/ 副本 → kb module（skill-kb-path-guard）。

背景（2026-10-14 实测）：
- `packs/kb/skills/<skill>/references/` 是 kb 树的字节级副本（127 个文件 sha256 相同，
  另有 xxe/jndi-injection/el-injection 3 处已内容漂移）。
- SKILL.md 表里的路径有两类写法，都逃逸 doctor 校验：
  a) `references/web/webapp/sqli/手册.md`（skill 本地副本路径）
  b) `sqli.md` / `14-ssti.md`（裸名，隐式相对某个 kb 目录）
- 且表里写的是 `skill_open(path=…)`，但 skill_open **只能读技能自己的
  references/scripts/examples/assets**，够不到 kb → 迁移后须改 `kb_open(module=…)`。

本脚本：
1. 把 SKILL.md 里的路径重写为 kb module 口径（`web/漏洞分类/sqli/手册.md`）；
2. 把引用这些路径的 `skill_open(path=` 改 `kb_open(module=`；
3. 删除与 kb 正本字节相同的 references/ 副本（--apply 才动盘）。

默认 dry-run，只打印计划；`--apply` 落盘。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "packs" / "kb"
SKILLS = KB / "skills"

# 引用可能出现两种包裹：反引号 `` `references/...md` `` 与双引号
# `skill_open(path="references/...md")`。首轮迁移只扫了反引号，导致
# recon-asset-enum 的 3 条 `skill_open(path="…")` 引用漏改（副本删了、引用没改）。
_EXT = r"(?:md|py|yaml|yml|json|txt)"
_INDEX_EXT = {".md", ".py", ".yaml", ".yml", ".json", ".txt"}
_REF_RE = re.compile(r"`([^`\n]+?\.%s)`|\"([^\"\n]+?\.%s)\"" % (_EXT, _EXT))


def extract_refs(text: str) -> list[str]:
    """抽出 SKILL.md 里的路径型引用（反引号 / 双引号包裹），保序去重。"""
    out: list[str] = []
    for m in _REF_RE.finditer(text):
        ref = m.group(1) or m.group(2)
        if "://" in ref or " " in ref or ref.startswith("mcp__"):
            continue
        if ref not in out:
            out.append(ref)
    return out


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build_kb_index() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    by_sha: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for f in KB.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _INDEX_EXT:
            continue
        parts = f.relative_to(KB).parts
        if parts[0] == "skills" or ".history" in parts:
            continue
        rel = "/".join(parts)
        by_sha.setdefault(sha(f), []).append(rel)
        by_name.setdefault(f.name, []).append(rel)
    return by_sha, by_name


# 手工裁定表：kb 无「同形」正本（名字/尾段都不唯一），按语义人工指定落点。
# 这些是迁移后仅剩的、脚本无法自动判定的少数条目，逐条可审计。
OVERRIDES: dict[str, str] = {
    # pwn 临场笔记：域内 3 个同名（漏洞利用/逆向工程/赛事专题），取收编版
    "references/binary/pwn/field-notes.md": "binary/漏洞利用/field-notes.md",
    # 上游 ctf-crypto / ctf-misc 的 skill 索引（正文只留索引、支撑文件已不在）→ 落域 README
    "references/crypto/crypto/index.md": "crypto/README.md",
    "references/misc/misc/index.md": "misc/README.md",
    # writeup 方法论索引 → 收编版
    "references/misc/writeup/index.md": "misc/赛事专题/WP撰写/index.md",
}


def domain_of(skill_md: Path) -> str | None:
    """从技能 references/ 的首段推断所属 kb 方向（web/binary/…）。"""
    refs = skill_md.parent / "references"
    if refs.is_dir():
        for p in refs.iterdir():
            if p.is_dir() and p.name in {"web", "binary", "crypto", "forensics", "misc", "cloud"}:
                return p.name
    return None


def pick(cands: list[str], ref: str, dom: str | None) -> str:
    """从候选里挑正本：**非上游快照优先**（赛事专题=原件不是收编版）→ 尾段匹配 →
    同域 → 最短。"""
    tail = Path(ref).parts
    tail2 = "/".join(tail[-2:]) if len(tail) >= 2 else ref
    name = tail[-1]

    def seg_end(path: str, seg: str) -> bool:
        return path == seg or path.endswith("/" + seg)

    def score(p: str) -> tuple:
        snap = 1 if "赛事专题" in p else 0          # 上游快照排最后
        tail_match = 0 if seg_end(p, tail2) else (1 if seg_end(p, name) else 2)
        same_dom = 0 if (dom and p.startswith(dom + "/")) else 1
        return (snap, tail_match, same_dom, len(p), p)

    return sorted(cands, key=score)[0]


GENERIC_NAMES = {"index.md", "readme.md", "skill.md"}


def _seg_end(path: str, seg: str) -> bool:
    return path == seg or path.endswith("/" + seg)


def _choose(ref: str, cands: list[str], dom: str | None) -> str | None:
    """从候选里定正本；歧义返回 None（交人工）。

    规则：尾两段匹配优先（`xxe/手册.md` 命中 `web/漏洞分类/xxe/手册.md`）；
    否则仅当**非快照候选唯一**且文件名非通用名（index/README）才认；否则判歧义。
    """
    if not cands:
        return None
    tail = Path(ref).parts
    tail2 = "/".join(tail[-2:]) if len(tail) >= 2 else ref
    t2 = [p for p in cands if _seg_end(p, tail2)]
    if t2:
        return pick(t2, ref, dom)
    non_snap = [p for p in cands if "赛事专题" not in p]
    pool = non_snap or cands
    if len(pool) == 1 and Path(ref).name.lower() not in GENERIC_NAMES:
        return pool[0]
    return None


def resolve(skill_md: Path, ref: str, by_sha, by_name, dom) -> tuple[str | None, str]:
    """→ (kb module 路径 | None, 状态标签)。

    标签：ALREADY_KB / DUP->KB（字节相同）/ DRIFT->KB（副本已改，按名找正本，需复核）
    / BARE->KB（裸名补全）/ SKILL_ONLY（kb 无对应或歧义，交人工）。

    同名兜底**限定同域**——`index.md`/`README.md` 这类通用名跨域乱猜会指错正本。
    字节相同的 exact 匹配不受域限制（内容一致即强证据），但打分压低上游快照。
    """
    ref = ref.lstrip("/")
    if (KB / ref).is_file():
        return ref, "ALREADY_KB"
    if ref in OVERRIDES:
        return OVERRIDES[ref], "MANUAL->KB"

    local = None
    for base in (skill_md.parent, skill_md.parent / "references"):
        c = base / ref
        if c.is_file():
            local = c
            break

    def named(same_dom: bool) -> list[str]:
        hits = by_name.get(Path(ref).name, [])
        if same_dom and dom:
            return [p for p in hits if p.startswith(dom + "/")]
        return hits

    if local is not None:
        exact = by_sha.get(sha(local), [])
        if exact:
            return pick(exact, ref, dom), "DUP->KB"
        tgt = _choose(ref, named(True), dom)
        return (tgt, "DRIFT->KB") if tgt else (None, "SKILL_ONLY")

    # 裸名：先限同域；同域无解再跨域兜底（_choose 只在「唯一非快照且非通用名」时认，
    # 否则仍判歧义）。rt-privesc 的 `12-privesc.md` 就靠这条落到 web/后渗透/intranet-postexp/。
    tgt = _choose(ref, named(True), dom) or _choose(ref, named(False), dom)
    return (tgt, "BARE->KB") if tgt else (None, "SKILL_ONLY")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")
    args = ap.parse_args()

    by_sha, by_name = build_kb_index()
    stats: dict[str, int] = {}
    plans: list[str] = []

    for sk in sorted(SKILLS.glob("*/SKILL.md")):
        name = sk.parent.name
        dom = domain_of(sk)
        text = sk.read_text(encoding="utf-8")
        new_text = text
        rewrites: list[tuple[str, str, str]] = []

        # 去重：同一 ref 在表里可能出现多次
        manual: list[str] = []
        for ref in extract_refs(text):
            # 噪声过滤：扩展名清单（`.py/.sage/.txt`）与正文里的文件名提及（robots.txt）
            if ref.startswith("."):
                continue
            if "/" not in ref and Path(ref).name not in by_name:
                continue
            target, tag = resolve(sk, ref, by_sha, by_name, dom)
            stats[tag] = stats.get(tag, 0) + 1
            if tag == "SKILL_ONLY":
                manual.append(ref)
            elif target and target != ref:
                rewrites.append((ref, target, tag))

        # 逐条替换（长串优先，避免子串误伤）；两种包裹都要覆盖
        for ref, target, _ in sorted(rewrites, key=lambda x: -len(x[0])):
            new_text = new_text.replace(f"`{ref}`", f"`{target}`")
            new_text = new_text.replace(f'"{ref}"', f'"{target}"')
        # 打开方式：这些路径已指向 kb，须改 kb_open
        if rewrites:
            new_text = new_text.replace("skill_open(path=", "kb_open(module=")
            new_text = new_text.replace("`skill_open` 单篇", "`kb_open` 单篇")
            new_text = new_text.replace("`skill_open` 打开", "`kb_open` 打开")

        # references/ 下副本：字节与 kb 相同 → 待删；同名但内容已漂移 → 只报不删
        doomed: list[str] = []
        drift: list[str] = []
        refs = sk.parent / "references"
        if refs.is_dir():
            for f in refs.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(sk.parent).as_posix()
                if sha(f) in by_sha:
                    doomed.append(rel)
                elif by_name.get(f.name):
                    drift.append(rel)

        if rewrites or doomed or manual or drift:
            plans.append(f"\n## {name}（域={dom}）")
            for ref, target, tag in rewrites:
                plans.append(f"  [{tag}] `{ref}` → `{target}`")
            if manual:
                plans.append(f"  ⚠ 需人工（kb 无对应/歧义）{len(manual)} 条：")
                for ref in manual:
                    plans.append(f"    - `{ref}`")
            if doomed:
                plans.append(f"  删副本 {len(doomed)} 个：")
                for d in doomed[:6]:
                    plans.append(f"    - {d}")
                if len(doomed) > 6:
                    plans.append(f"    … 另 {len(doomed) - 6} 个")
            if drift:
                plans.append(f"  ⚠ 副本已漂移（kb 有同名，人工确认删除）{len(drift)} 个：")
                for d in drift[:6]:
                    plans.append(f"    - {d}")
                if len(drift) > 6:
                    plans.append(f"    … 另 {len(drift) - 6} 个")

        if args.apply and (new_text != text or doomed):
            sk.write_text(new_text, encoding="utf-8")
            for d in doomed:
                (sk.parent / d).unlink()
            # 清空的 references 目录顺手删掉
            if refs.is_dir() and not any(refs.rglob("*")):
                shutil.rmtree(refs, ignore_errors=True)

    print(f"模式：{'APPLY（已落盘）' if args.apply else 'DRY-RUN（未改动）'}")
    print(f"统计：{stats}")
    print("\n".join(plans))
    total_del = sum(1 for p in plans if "删副本" in p)
    print(f"\n涉及技能 {total_del} 个；改写条目 "
          f"{sum(stats.get(k, 0) for k in ('DUP->KB', 'DRIFT->KB', 'BARE->KB', 'MANUAL->KB'))} 条")


if __name__ == "__main__":
    main()
