"""测试点路由索引（2026-09-18，知识库四层重构 · v0.65）。

全局单表 `packs/kb/route_index.yaml`（expert-pool M0，2026-09-21）：条目=测试点 →
kb 模块（**全局形态 `<域>/<快照>/<路径>`**，域=首段）+ 触发词 + 可选技能标签。
任务认领时按启用域过滤后注入（紧凑一行一条），Agent 发现测试点
（如文件上传）即可按索引 kb_open 对应手册，不必靠试错。

角色裁剪：条目 `tags`（技能名列表）与角色 skills 白名单取交集，
交集非空才可见；无 tags 条目全角色可见。

解析为极简行式（与 roles.py 同哲学，零 PyYAML 依赖）：仅支持
`entries:` 下嵌一层 `- key: value` 条目，字段平铺，列表用内联 `[a, b]`。
解析失败 log + 返空——索引永远不影响路由主链。

缓存：{key: (entries, mtime)}，每次调用 stat 索引文件，mtime 变化即重读
（M0 后键仍含 cap——缓存的是域过滤视图，全局文件单份共享）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from core.skills.writing import resolve_kb

log = logging.getLogger(__name__)

#: 单条目允许的字段（防手写笔误静默丢字段）
_FIELDS = {"point", "match", "kb", "tags"}


@dataclass
class IndexEntry:
    point: str                     # 测试点名（人读，如「文件上传测试」）
    kb: str                        # kb_open 模块路径（源内相对，posix）
    match: list[str] = field(default_factory=list)   # 触发词（供召回/高亮）
    tags: list[str] = field(default_factory=list)    # 技能名标签（角色裁剪）


def _parse_inline_list(raw: str) -> list[str]:
    """`[a, b]` → ["a", "b"]；空/无括号按单词处理。剥引号与行内注释。"""
    raw = raw.split("#", 1)[0].strip()
    if raw.startswith("[") and raw.endswith("]"):
        items = raw[1:-1].split(",")
    else:
        items = [raw] if raw else []
    return [i.strip().strip("\"'") for i in items if i.strip().strip("\"'")]


def parse_entries(text: str) -> list[IndexEntry]:
    """极简行式解析 route_index.yaml。仅支持 entries: 下一层条目。"""
    entries: list[IndexEntry] = []
    cur: dict | None = None
    in_entries = False
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "entries:":
            in_entries = True
            continue
        if not in_entries:
            continue
        if stripped.startswith("- "):
            cur = {}
            entries.append(cur)
            stripped = stripped[2:].strip()
            if not stripped:
                continue
        elif stripped.startswith("-"):
            # 仅「-」独占一行的写法
            cur = {}
            entries.append(cur)
            continue
        elif line[:1] not in (" ", "\t"):
            in_entries = False   # 离开 entries 块（顶层其他键）
            continue
        if cur is None:
            raise ValueError(f"第 {lineno} 行字段出现在条目外: {stripped}")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if key not in _FIELDS:
            raise ValueError(f"第 {lineno} 行未知字段: {key}")
        if key in ("match", "tags"):
            cur[key] = _parse_inline_list(value)
        else:
            cur[key] = value.strip("\"'")
    out: list[IndexEntry] = []
    for i, c in enumerate(entries, 1):
        missing = {"point", "kb"} - {k for k, v in c.items() if v}
        if missing:
            raise ValueError(f"第 {i} 条缺必填字段: {'、'.join(sorted(missing))}")
        out.append(IndexEntry(point=c["point"], kb=c["kb"],
                              match=c.get("match") or [],
                              tags=c.get("tags") or []))
    return out


def serialize_entries(entries: list[IndexEntry]) -> str:
    """parse_entries 的逆（紧凑行式，往返兼容）；M0 全局表域归并写回用。"""
    lines = ["entries:"]
    for e in entries:
        lines.append(f"  - point: {e.point}")
        if e.match:
            lines.append("    match: [" + ", ".join(e.match) + "]")
        lines.append(f"    kb: {e.kb}")
        if e.tags:
            lines.append("    tags: [" + ", ".join(e.tags) + "]")
    return "\n".join(lines) + "\n"


# ---------- 缓存（mtime 快检，照 kbindex 模式） ----------

_CACHE: dict[tuple[str, str], tuple[list[IndexEntry], tuple[int, int]]] = {}
_CACHE_LOCK = threading.Lock()


def _file_stat_key(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return (-1, -1)


def load_route_index(packs_root: str | Path, cap: str) -> list[IndexEntry]:
    """读全局 route_index.yaml 中 kb 首段==cap 的条目（M0 域过滤视图）。

    全局单表 packs/kb/route_index.yaml；文件缺省=空索引，解析失败=空。"""
    path = Path(packs_root) / "kb" / "route_index.yaml"
    if not path.is_file():
        return []
    key = (str(packs_root), cap)
    stat_key = _file_stat_key(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[1] == stat_key:
            return cached[0]
    try:
        all_entries = parse_entries(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 —— 索引坏了不影响路由主链
        log.warning("route_index.yaml 解析失败（%s）: %s", path, e)
        all_entries = []
    entries = [e for e in all_entries if e.kb.split("/", 1)[0] == cap]
    _CACHE[key] = (entries, stat_key)
    return entries


def visible(entry: IndexEntry, role_skills: list[str] | None) -> bool:
    """角色裁剪：tags 空=全角色可见；否则与角色 skills 白名单有交集才可见。
    role_skills=None（角色未设白名单）=全可见。"""
    if not entry.tags or role_skills is None:
        return True
    return bool(set(entry.tags) & set(role_skills))


def render_route_index(packs_root: str | Path, capabilities: list[str] | None,
                       role_skills: list[str] | None = None) -> str:
    """聚合各启用能力包索引 → 裁剪 → 渲染紧凑注入段。无条目返回 ""。

    全表版（G1 起仅供 doctor/调试/单测用）；认领注入走 render_route_index_top。"""
    lines: list[str] = []
    for cap in capabilities or []:
        for e in load_route_index(packs_root, cap):
            if not visible(e, role_skills):
                continue
            lines.append(f"- {e.point} → {e.kb}")
    if not lines:
        return ""
    return ("\n\n## 🧭 测试点路由索引（发现对应测试点时，先 kb_open 打开对应"
            "手册再动手，走已验证路径避免试错）\n" + "\n".join(lines))


# ---------- G1 Top-K 注入（2026-09-19，§4 渐进披露） ----------

def score_entry(e: IndexEntry, query: str) -> int:
    """条目与任务 query 的相关性：match 触发词双向子串命中 ×2，point 命中 ×1。
    双向是因为 query 与 point 长短不定（「文件上传」⊂「文件上传测试」）。"""
    if not query:
        return 0
    s = 0
    for w in e.match:
        if w and (w in query or query in w):
            s += 2
    if e.point and (e.point in query or query in e.point):
        s += 1
    return s


def top_route_entries(packs_root: str | Path, capabilities: list[str] | None,
                      role_skills: list[str] | None = None,
                      query: str = "", top_k: int = 5,
                      ) -> tuple[list[IndexEntry], int]:
    """聚合可见条目 → 按 query 评分取 Top-K。返回 (命中条目, 可见总条数)。
    同分按原表顺序稳定排序；评分为 0 的条目不入选。"""
    all_e: list[IndexEntry] = []
    for cap in capabilities or []:
        for e in load_route_index(packs_root, cap):
            if visible(e, role_skills):
                all_e.append(e)
    scored = [(score_entry(e, query), i, e) for i, e in enumerate(all_e)]
    hits = [e for s, _i, e in sorted((t for t in scored if t[0] > 0),
                                     key=lambda t: (-t[0], t[1]))][:max(1, top_k)]
    return hits, len(all_e)


def render_route_index_top(packs_root: str | Path, capabilities: list[str] | None,
                           role_skills: list[str] | None = None,
                           query: str = "", top_k: int = 5,
                           ) -> tuple[str, int, int, list[str]]:
    """Top-K 注入段（G1：全表 222 行 → 最相关 3-5 行，其余经 route_lookup 工具查询）。

    返回 (注入文本, 注入条数, 可见总条数, 命中测试点名列表)——点名列表供 K7
    路由效果追踪（skill.routed 事件回填使用标记，doctor 报长期零命中条目）。
    总条数 0 → ("", 0, 0, [])；无匹配 → 只剩指引行（Agent 仍知道索引存在、怎么查）。"""
    hits, total = top_route_entries(packs_root, capabilities, role_skills,
                                    query=query, top_k=top_k)
    if total == 0:
        return "", 0, 0, []
    head = ("\n\n## 🧭 测试点路由索引（发现对应测试点时，先 kb_open 打开对应手册"
            "再动手，走已验证路径避免试错；下面只列与本任务最相关的条目）")
    lines = [f"- {e.point} → {e.kb}" for e in hits]
    if not hits:
        return (head + f"\n（索引共 {total} 条，无与本任务关键词匹配的——需要时用 "
                "route_lookup 工具按关键词查询，如 {\"query\": \"文件上传\"}）",
                0, total, [])
    tail = ""
    if total > len(hits):
        tail = (f"\n……其余 {total - len(hits)} 条略：需要其他方向手册时用 "
                "route_lookup 工具按关键词查询。")
    points = [e.point for e in hits]
    return head + "\n" + "\n".join(lines) + tail, len(hits), total, points


def read_kb_module(packs_root: str | Path, capabilities: list[str] | None,
                   module: str, max_chars: int = 800) -> str:
    """按 kb_open 同口径定位并读模块正文（截断）。找不到返回 ""。
    供卡壳顾问召回共用；resolve 失败/读盘失败静默返空。"""
    for cap in capabilities or []:
        try:
            target = resolve_kb(packs_root, cap, module)
        except Exception:  # noqa: BLE001
            continue
        if target.path.is_file():
            try:
                text = target.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
            return text[:max_chars]
    return ""
