"""Find recurring reviewed Cases and create pending Pattern proposals.

This is deliberately deterministic: it groups approved Case documents by their
frontmatter vulnerability class and requires at least two independent cases.
The resulting Pattern is a proposal for human review, never a direct write.
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

from core.skills import proposals


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out: dict[str, str] = {}
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip().strip("[]\"'")
    return out


def _slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value.lower()).strip("-")
    return value[:80] or "recurring-technique"


def _cases(packs: Path, cap: str):
    root = packs / "kb" / cap / "cases"
    for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        fm = _frontmatter(text)
        if fm.get("kind") != "case" or fm.get("status", "approved") not in {"approved", "verified"}:
            continue
        tags = [x.strip().lower() for x in fm.get("vuln_class", "").split(",") if x.strip()]
        if tags:
            yield path, text, tags


def propose_patterns(packs: str | Path = "packs", *, cap: str = "web",
                     min_cases: int = 5, limit: int = 10) -> dict:
    packs_path = Path(packs).resolve()
    groups: dict[str, list[Path]] = defaultdict(list)
    for path, _text, tags in _cases(packs_path, cap):
        for tag in tags:
            groups[tag].append(path)
    pending = {(p.get("target") or {}).get("path") for p in proposals.list_proposals(packs_path, "pending")
               if (p.get("target") or {}).get("kind") == "pattern"}
    created: list[str] = []
    candidates = []
    for tag, paths in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(paths) < max(2, min_cases):
            continue
        target = f"patterns/{_slug(tag)}.md"
        if target in pending or (packs_path / "kb" / cap / target).is_file():
            continue
        candidates.append({"tag": tag, "cases": [p.relative_to(packs_path).as_posix() for p in paths],
                           "target": target})
        if len(candidates) >= max(1, limit):
            break
    for item in candidates:
        content = "\n".join([
            "---", "kind: pattern", "status: draft", f"vuln_class: [{item['tag']}]",
            "derived_from:", *[f"  - {p}" for p in item["cases"]], "---", "",
            f"# 通用模式：{item['tag']}", "", "## 共同原理",
            "待人工根据关联 Case 核对并补充跨案例的不变量。", "", "## 适用条件",
            f"至少出现 {len(item['cases'])} 个已审核案例，标签为 `{item['tag']}`。", "",
            "## 资料边界", "该文件是自动发现的 Pattern 候选，审批前不得作为主方法论使用。", "",
        ])
        proposal = proposals.create_proposal(
            packs_path,
            {"target": {"kind": "pattern", "cap": cap, "path": item["target"]},
             "mode": "create", "content": content,
             "summary": f"从 {len(item['cases'])} 个案例发现重复模式：{item['tag']}",
             "reason": "周期性 Case 聚类候选，需人工确认共同原理与适用边界",
             "evidence": ", ".join(item["cases"])},
            origin="review")
        created.append(proposal["id"])
    return {"candidates": candidates, "proposals": created}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packs", default="packs")
    ap.add_argument("--cap", default="web")
    ap.add_argument("--min-cases", type=int, default=5)
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    import json
    print(json.dumps(propose_patterns(args.packs, cap=args.cap, min_cases=args.min_cases,
                                      limit=args.limit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
