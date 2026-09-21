"""Obsidian vault 只读索引（E10，DESIGN.md §16.3）。

隐私红线（定稿，不可放松）：LLM 只看**元数据**（文件名/标题/frontmatter 标签/目录结构），
笔记正文永不出本机发给 LLM；正文入库（vault_notes.content）仅平台内本地搜索用。
平台绝不写回 vault——用户手工整理的笔记是圣域。
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from core.skills.registry import parse_frontmatter  # 复用极简 frontmatter 解析

SKIP_DIRS = {".obsidian", ".trash", ".git", ".history"}

_INLINE_TAG = re.compile(r"(?<![\w#])#([\w][\w/-]*)")
_HEADING = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def index_vault(root: str | Path) -> list[dict]:
    """遍历 vault 根下 *.md，产出元数据列表（path 为相对 POSIX 路径；含正文仅供本地搜索）。

    调用方负责把结果交给 IntelStore.replace_notes()；vault 不存在抛 FileNotFoundError。
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"vault 目录不存在：{root}")
    out: list[dict] = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        title = (fm.get("title") or "").strip()
        if not title:
            m = _HEADING.search(text)
            title = m.group(1).strip() if m else p.stem
        tags: list[str] = []
        raw_tags = fm.get("tags", "")
        if isinstance(raw_tags, str):
            for t in re.split(r"[,\s，、]+", raw_tags):
                t = t.strip().lstrip("#").strip("- ")
                if t:
                    tags.append(t)
        seen = {t.lower() for t in tags}
        for t in _INLINE_TAG.findall(text[:4000]):  # 内联 #tag（只扫前段，够用且省时）
            if t.lower() not in seen:
                seen.add(t.lower())
                tags.append(t)
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        # F5：结构化 frontmatter category 入索引（归一化小写；方向判定优先精确命中）
        category = str(fm.get("category") or "").strip().lower()
        out.append({"path": rel, "title": title[:200], "tags": tags[:20],
                    "category": category[:64],
                    "mtime": mtime.isoformat(timespec="seconds"),
                    "size": p.stat().st_size, "content": text})
    return out


def build_tree(notes: list[dict]) -> list[dict]:
    """平铺相对路径 → 嵌套树：目录节点 {name, children}，笔记叶子 {name, path, title, tags, mtime}。"""
    root: dict = {}
    for n in notes:
        parts = n["path"].split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part + "/", {})
        node[parts[-1]] = n

    def emit(d: dict) -> list[dict]:
        dirs = [{"name": k.rstrip("/"), "children": emit(v)}
                for k, v in d.items() if k.endswith("/")]
        files = [{"name": k, "path": v["path"], "title": v.get("title", ""),
                  "tags": v.get("tags", []), "mtime": v.get("mtime", "")}
                 for k, v in d.items() if not k.endswith("/")]
        return sorted(dirs, key=lambda x: x["name"]) + sorted(files, key=lambda x: x["name"])

    return emit(root)
