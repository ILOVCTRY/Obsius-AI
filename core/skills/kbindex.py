"""kb 中文标题轻量索引（2026-09-18，skill 路由增强 · 方向 C）。

背景：kb 文件名多为英文（blind-sql-injection.md），任务 objective 是中文——
纯文件名匹配几乎永远命中不了。本模块扫描 kb 源树，每个 md 提取
「frontmatter title > 第一个 # H1 > 文件名 stem」作为标题，外加 K3 加深的
两级信号：H2 段落标题（tokens 一并纳入，命中可定位到节）与一行摘要
（首个正文行截断，hints 注入让 Agent 不开文件也知道讲什么）。路由时用
objective(+scope) 的 2-gram 切词对索引做轻量匹配，命中模块以
「📚 相关知识库模块」提示行注入（只给路径与段落，Agent 自己 kb_open）。

kb 不进 skill 路由候选集（registry 口径不变）；本索引只服务提示行。
缓存：模块级 {key: (entries, mtimes)} + mtime 快检——每次调用 stat 源树
（数百文件毫秒级），增/改/删任一不一致即重建，kb 编辑后当任务即生效。

任务导航路由（2026-09-19，借鉴 dsh refs/README 路由表）：各能力包
kb/route.json 静态声明「关键词|task_type → 模块前缀」，2-gram 索引之外的
确定性导航层——任务认领时先查路由（键子串命中 task_type+query），命中组
以「🧭 知识库任务导航」注入；无 route.json 的包静默降级走既有 hints。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from core.skills.registry import parse_frontmatter
from core.skills.rules import load_kb_sources

# K3 段落级索引：整文件读取但设上限（H2 标题可能出现在任何位置；
# 477 篇 × 大多几十 KB，mtime 快检通过不重扫，重建一次毫秒-百毫秒级）
_FILE_CAP_BYTES = 512 * 1024
# 每模块收录的 H2 段落标题上限（防超大文档刷爆索引）
_SECTION_CAP = 60
# 每模块一行摘要的截断长度
_SUMMARY_CHARS = 90
# 匹配提示行条数上限（宁少勿滥，误报只给路径成本极低）
DEFAULT_HINT_CAP = 4

# 停用 2-gram：任务描述高频泛词，命中了也不代表 kb 相关
_STOP_GRAMS = {
    "测试", "任务", "检查", "尝试", "分析", "验证", "处理", "操作",
    "问题", "相关", "进行", "一个", "可能", "需要", "使用", "通过",
}

# 中文段 2-gram 用的切分（与 re.split 同字符类）
_SPLIT_RE = re.compile(r"[^\w一-鿿]+")
_ASCII_WORD_RE = re.compile(r"^[a-z0-9_\-.]{3,}$")


@dataclass
class KbEntry:
    source_id: str
    module: str      # 源内相对路径（posix，与 kb_open 的 module 参数同口径）
    title: str       # 展示标题：frontmatter title > 第一个 # H1 > 文件名 stem
    tokens: str      # 匹配串：title + 文件名 stem + H2 段落标题（lower）
    mtime: float
    summary: str = ""    # K3：一行摘要（首个正文行截断），hints 注入让 AI 不开文件也知道讲什么
    sections: tuple[str, ...] = ()   # K3：H2 段落标题（段落级命中定位）
    facets: tuple[str, ...] = ()     # K5 升级项 C：frontmatter 分面标签（phase/vuln_class，
                                     #   lower；只供 search_kb 过滤，不进 tokens 防提示噪声）


def parse_facets(text: str) -> set[str]:
    """从文档头部（可含 frontmatter）提取分面标签集合（lower）。

    K5 升级项 C（Anthropic-Cybersecurity-Skills「正文单轴+多分面元数据」模式）：
    frontmatter `phase:` / `vuln_class:` 单值或行内列表均可——
    `phase: webapp`、`vuln_class: [sqli, injection]`。"""
    fm, _body = _split_frontmatter(text)
    out: set[str] = set()
    for key in ("phase", "vuln_class"):
        raw = str(fm.get(key) or "").strip()
        if not raw:
            continue
        raw = raw.strip("[]")
        for item in raw.split(","):
            item = item.strip().strip("\"'").lower()
            if item:
                out.add(item)
    return out


def _extract_title(head: str, stem: str) -> str:
    """frontmatter title > 第一个 # H1 > 文件名 stem。frontmatter 不闭合按无处理。"""
    title = ""
    body = head
    if head.startswith("---"):
        parts = head.split("---", 2)
        if len(parts) == 3:
            try:
                title = str(parse_frontmatter(head).get("title") or "")
            except Exception:
                title = ""
            body = parts[2]
    if not title:
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if m:
            title = m.group(1).strip()
    return (title or stem).strip()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter dict(可空), 去掉 frontmatter 的正文)。不闭合按无处理。"""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                return parse_frontmatter(text), parts[2]
            except Exception:
                return {}, parts[2]
    return {}, text


def _extract_sections(body: str) -> tuple[str, ...]:
    """正文里全部 H2 标题（去重保序，cap 限流）。"""
    out: list[str] = []
    for m in re.finditer(r"^##\s+(.+?)\s*$", body, re.MULTILINE):
        h = m.group(1).strip()
        if h and h not in out:
            out.append(h)
        if len(out) >= _SECTION_CAP:
            break
    return tuple(out)


def _extract_summary(body: str) -> str:
    """一行摘要：第一个非空正文行（剥掉 markdown 前缀符号）截断。

    frontmatter title > H1 之后的第一句话往往就是「本篇讲什么」——
    中文旅馆（src-strike 知识库）是中文句子，英文快照是英文首行，都可用。
    """
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"^[\>\-\*\d\.\[\]\(\)`\|]+", "", s).strip()
        if len(s) <= 1:
            continue
        return s[:_SUMMARY_CHARS]
    return ""


def build_kb_index(packs_root: str | Path,
                   capabilities: list[str] | None) -> list[KbEntry]:
    """扫描启用能力包的 kb 源树（排除 .history）。

    K5 索引分层口径：只索引 .md（标题/段落/摘要 hints）；.py/.txt/.json 弹药
    不进索引与 hints，只在 kb_search 全文与 kb_open 可读（writing.KB_FILE_SUFFIXES）。"""
    entries: list[KbEntry] = []
    for src in load_kb_sources(packs_root, capabilities):
        if not src.root.is_dir():
            continue
        iterator = src.root.rglob("*.md") if src.recursive else src.root.glob("*.md")
        for f in sorted(iterator):
            rel = f.relative_to(src.root)
            if ".history" in rel.parts:
                continue
            try:
                stat = f.stat()
                size = stat.st_size
                read_n = min(size, _FILE_CAP_BYTES)
                text = f.open("rb").read(read_n).decode("utf-8", errors="replace")
            except OSError:
                continue
            stem = f.stem
            fm, body = _split_frontmatter(text)
            title = (str(fm.get("title") or "") or _extract_title(text, stem)).strip()
            sections = _extract_sections(body)
            entries.append(KbEntry(
                source_id=src.id,
                module=f"{src.root.name}/{rel.as_posix()}",  # 全局形态 <域>/<快照>/<路径>（M0）
                title=title,
                tokens=f"{title} {stem} {' '.join(sections)}".lower(),
                mtime=stat.st_mtime,
                summary=_extract_summary(body),
                sections=sections,
                facets=tuple(sorted(parse_facets(text))),
            ))
    return entries


def _query_segments(text: str) -> list[str]:
    """query 切段：ASCII 整词保留，中文段再切 2-gram（去停用）。"""
    segs: list[str] = []
    for seg in _SPLIT_RE.split((text or "").lower()):
        if not seg:
            continue
        if _ASCII_WORD_RE.match(seg):
            segs.append(seg)
            continue
        if re.search(r"[一-鿿]", seg):
            grams = [seg[i:i + 2] for i in range(len(seg) - 1)]
            segs.extend(g for g in grams if g not in _STOP_GRAMS)
    return segs


def match_kb_index_detailed(index: list[KbEntry], text: str,
                            cap: int = DEFAULT_HINT_CAP,
                            ) -> list[tuple[KbEntry, str]]:
    """K3：与 match_kb_index 同评分，另返回每个命中的「相关段落」。

    段落判定：query 段落在 entry 某个 H2 标题（lower）里命中则记该标题
    （取第一个命中的，多段命中时靠前的信息密度更高）——hints 行借此把
    「去看这篇」升级为「去看这篇的哪一节」。无段落命中则记空串。
    """
    segs = _query_segments(text)
    if not segs:
        return []
    scored: list[tuple[float, str, KbEntry, str]] = []
    for e in index:
        score = 0.0
        section = ""
        token_l = e.tokens
        title_l = e.title.lower()
        for seg in set(segs):
            if seg not in token_l:
                continue
            score += 1.0
            if seg in title_l:
                score += 1.0
            if not section:
                for h in e.sections:
                    if seg in h.lower():
                        section = h
                        break
        if score > 0:
            scored.append((-score, e.module, e, section))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [(e, sec) for _, _, e, sec in scored[:max(1, cap)]]


def match_kb_index(index: list[KbEntry], text: str,
                   cap: int = DEFAULT_HINT_CAP) -> list[KbEntry]:
    """2-gram/整词对 tokens 做子串匹配。score=命中的不同原始段个数，
    命中落在 title（而非仅文件名）再 +1；排序 (-score, module)。
    （K3 兼容包装：detailed 版返回段落定位。）"""
    return [e for e, _ in match_kb_index_detailed(index, text, cap=cap)]


# ---------- 缓存（mtime 快检） ----------

_CACHE: dict[tuple[str, tuple[str, ...]], tuple[list[KbEntry], dict[str, int]]] = {}
_CACHE_LOCK = threading.Lock()


def _tree_mtimes(packs_root: str | Path,
                 capabilities: list[str] | None) -> dict[str, int]:
    """缓存戳：每文件 st_mtime_ns<<20 | size（mtime 同刻度快速覆写由 size 消歧）。"""
    mtimes: dict[str, int] = {}
    for src in load_kb_sources(packs_root, capabilities):
        if not src.root.is_dir():
            continue
        iterator = src.root.rglob("*.md") if src.recursive else src.root.glob("*.md")
        for f in iterator:
            rel = f.relative_to(src.root)
            if ".history" in rel.parts:
                continue
            try:
                st = f.stat()
                mtimes[f"{src.id}/{rel.as_posix()}"] = (st.st_mtime_ns << 20) | st.st_size
            except OSError:
                continue
    return mtimes


def kb_module_hints(packs_root: str | Path, capabilities: list[str] | None,
                    query_text: str, cap: int = DEFAULT_HINT_CAP
                    ) -> list[tuple[KbEntry, str]]:
    """带缓存的对外口：mtime 快检不一致才重建索引，然后匹配 query_text。
    路由未命中/命中都调它——hint 行让 Agent 知道 kb 里有相关专题。
    （K3：返回 (entry, 相关段落) 对，段落可能为空串。）"""
    key = (str(packs_root), tuple(capabilities or []))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        mtimes = _tree_mtimes(packs_root, capabilities)
        if cached is None or cached[1] != mtimes:
            cached = (build_kb_index(packs_root, capabilities), mtimes)
            _CACHE[key] = cached
    return match_kb_index_detailed(cached[0], query_text, cap=cap)


# ---------- 任务导航路由（kb/route.json 静态路由，2026-09-19） ----------

_ROUTE_CACHE: dict[tuple[str, tuple[str, ...]],
                   tuple[tuple[tuple[int, int], ...], dict[str, list[str]]]] = {}
DEFAULT_ROUTE_CAP = 3


def load_kb_routes(packs_root: str | Path,
                   capabilities: list[str] | None) -> dict[str, list[str]]:
    """各启用能力域 kb/<域>/route.json 合并为 {路由键: [模块前缀]}（M0 后值带域前缀）。

    route.json 顶层对象：键=「中文关键词|备选|task_type_id」多选一（| 分隔），
    值=全局模块前缀（目录带尾 / 或文件路径）。缺文件/坏 JSON/域未启用
    一律静默降级（返回空）——路由层是提示增强，永不阻断主链。

    缓存戳 = 各文件 (st_mtime_ns, size) 元组（2026-09-20 由 mtime 之和升级：
    快速覆写可落在同一时钟刻度，mtime 和不变 → 陈旧缓存；size 维度消歧）。"""
    root = Path(packs_root)
    stamp: list[tuple[int, int]] = []
    for c in capabilities or []:
        p = root / "kb" / c / "route.json"
        try:
            st = p.stat()
            stamp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((-1, -1))
    stamp_t = tuple(stamp)
    key = (str(packs_root), tuple(capabilities or []))
    with _CACHE_LOCK:
        cached = _ROUTE_CACHE.get(key)
        if cached is not None and cached[0] == stamp_t:
            return cached[1]
    routes: dict[str, list[str]] = {}
    for c in capabilities or []:
        p = root / "kb" / c / "route.json"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, list):
                    prefixes = [str(x) for x in v if isinstance(x, (str, int))]
                    if prefixes:
                        routes.setdefault(k, []).extend(prefixes)
    _ROUTE_CACHE[key] = (stamp_t, routes)
    return routes


def kb_route_hints(packs_root: str | Path, capabilities: list[str] | None,
                   task_type: str | None, query_text: str,
                   cap: int = DEFAULT_ROUTE_CAP) -> list[tuple[str, list[str]]]:
    """route.json 键匹配（子串，多选一 | 分隔）task_type+query。
    返回 [(命中键, 前缀列表)]，最多 cap 组（确定性优先于 2-gram hints）。"""
    routes = load_kb_routes(packs_root, capabilities)
    if not routes:
        return []
    combined = f"{task_type or ''} {query_text or ''}".lower()
    if not combined.strip():
        return []
    hits: list[tuple[str, list[str]]] = []
    for k, prefixes in routes.items():
        if any(alt.strip() and alt.strip().lower() in combined
               for alt in k.split("|")):
            hits.append((k, prefixes))
            if len(hits) >= max(1, cap):
                break
    return hits
