"""packs/ 受控写入（DESIGN.md §4：唯一写 packs 的地方）。

两层原语：

1. **进程写锁**：``pack_write_lock``（RLock）。设置页所有 packs 文件写（技能/角色/
   红线/owners/MCP/历史备份/trash/回滚/kb/提案应用）串行化，消除并发保存的丢失更新。
   RLock 让端点临界区内可再调备份/trash 辅助。只覆盖单进程 uvicorn（与 app.state
   会话表一致）；多进程部署需换文件锁/Postgres，当前明确不支持。

2. **kb 本地基线写入（C 批）**：知识快照从"只读文物"翻为"可增改的本地基线"
   （DESIGN §4，2026-09-14）。纪律由调用方与校验共同保证：英文快照原文不翻译、
   新增经验写新 md（rename/delete 走引用联动，见 core/skills/refs.py）。

所有 mutation 函数内部取锁；纯校验/解析函数不取锁。
"""

import json
import math
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from core.skills.matching import query_tokens
from core.skills.rules import KbSource, load_kb_sources
from core.skills.taxonomy import skill_dir

_PACK_WRITE_LOCK = threading.RLock()

# kb 文件硬上限（1 MiB：方法论文档足够，防误传大文件进 packs）
KB_MAX_BYTES = 1 << 20
# kb 允许的扩展名（K5 测试包分类制，DESIGN.md §4）：.md 手册参与 kbindex
# 全量索引（hints）；.py/.txt/.json 弹药只进 kb_search 全文与 kb_open 可读，
# 不进 hints（防脚本名噪声进提示行）。kb 内脚本只是文本，无执行面——
# 执行仍只经 run() 网关 + 沙箱（红线不变）。
# Text resources participate in search; binary attachments remain addressable
# resources and are listed with metadata but are never decoded as prose.
KB_TEXT_SUFFIXES = {".md", ".py", ".txt", ".json", ".js", ".yml", ".yaml", ".dic", ".enc", ".dat"}
KB_ATTACHMENT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
KB_FILE_SUFFIXES = KB_TEXT_SUFFIXES | KB_ATTACHMENT_SUFFIXES
# kb 根下平台自维护文件（非知识内容，树/搜索里不出现）
_KB_NON_CONTENT_FILES = {"route.json"}
#: 「全部」聚合视图哨兵 cap（2026-10-23）：知识库工作台默认打开全部方向。
#: 只在**只读列举/检索**口径被识别（list_kb / API 层 fan-out）；写入类接口
#: （create/update/rename/delete）必须落到具体方向目录，故不认哨兵——传进来
#: 会按「域不存在」422 拒收，这正是我们要的：宁可报错也不猜落点。
KB_ALL_CAP = "__all__"
# Windows 非法文件名字符（/ 是分隔符不算）。知识库来源文件名可能包含 Unicode
# 标点（例如 en dash ``–``），因此不能用过窄的 ASCII 文件名白名单限制既有资料。
_KB_FORBIDDEN_CHARS = set('<>:"|?*\x00')
_UTC_TS_FMT = "%Y%m%dT%H%M%SZ"


class KbError(ValueError):
    """kb 路径/内容非法（API 层映射 422）。"""


@contextmanager
def pack_write_lock():
    """packs/config 受控文件写的临界区（进程内单写者，可重入）。"""
    with _PACK_WRITE_LOCK:
        yield


# ---------- 通用受控文件原语（技能/角色/规则/owners/mcp） ----------

def _utc_ts() -> str:
    return time.strftime(_UTC_TS_FMT, time.gmtime())


def backup_history(path: Path) -> Path | None:
    """写入前留修改历史：同级 ``.history/<UTC时间戳>[.n]_<文件名>``。

    与设置页历史端点（回滚/diff）共用命名；同秒多次写入用 .n 避让，绝不覆盖既有版本。
    原文件不存在则无备份（返回 None）。调用方须已持锁（或在 pack_write_lock 内）。"""
    if not path.is_file():
        return None
    hist = path.parent / ".history"
    hist.mkdir(parents=True, exist_ok=True)
    ts = _utc_ts()
    dest = hist / f"{ts}_{path.name}"
    n = 1
    while dest.exists():
        dest = hist / f"{ts}.{n}_{path.name}"
        n += 1
    shutil.copy2(path, dest)
    return dest


def backup_and_write(path: Path, content: str) -> Path | None:
    """通用"备份→全文写"（技能/提案 skill 应用等）；返回备份路径（新建时为 None）。

    父目录自动创建。整段在写锁临界区内。"""
    with pack_write_lock():
        backup = backup_history(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return backup


def trash_move(src: Path) -> Path:
    """删除不物理抹除：移入同级 ``.history/trash/<名>.<UTC时间戳>[.n](.bak)``。

    同秒二次删除用序号避让。调用方须已持锁。"""
    trash = src.parent / ".history" / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    ts = _utc_ts()
    is_dir = src.is_dir()
    tail = "" if is_dir else ".bak"
    dest = trash / f"{src.name}.{ts}{tail}"
    n = 1
    while dest.exists():
        dest = trash / f"{src.name}.{ts}.{n}{tail}"
        n += 1
    shutil.move(str(src), str(dest))
    return dest


# ---------- kb 目标解析与校验 ----------

@dataclass
class KbTarget:
    cap: str
    source: KbSource
    rel: Path            # 源 root 内相对路径（posix 语义，可多级）
    path: Path           # resolve 后的绝对路径

    @property
    def module(self) -> str:
        return self.rel.as_posix()


def _validate_rel(rel_path: str) -> Path:
    """kb 模块相对路径校验：非空、非绝对、无 ..、限 KB_FILE_SUFFIXES
    （K5：.md 手册 + .py/.txt/.json 弹药）、中文多级目录允许、
    拒绝 Windows 非法字符与空字节；不允许落到 .history（备份区永不接受编辑）。"""
    if not rel_path or not rel_path.strip():
        raise KbError("路径不能为空")
    p = Path(rel_path.strip().replace("\\", "/"))
    if p.is_absolute() or not p.parts or ".." in p.parts:
        raise KbError("必须是知识库内相对路径（禁止绝对路径 / .. 穿越）")
    if p.parts[0] == ".history":
        raise KbError(".history 是备份区，不可直接编辑/新建")
    for seg in p.parts:
        if not seg or seg in {".", ""} or any(c in seg for c in _KB_FORBIDDEN_CHARS):
            raise KbError(f"非法路径段: {seg!r}")
    if p.suffix.lower() not in KB_FILE_SUFFIXES:
        raise KbError("知识库只接受文本资源或常见附件文件")
    name = p.stem
    # 允许 Unicode 字母、数字和标点，但拒绝 Windows 控制字符/保留字符，
    # 以及 Windows 不接受的尾随空格或句点。路径段已先做分隔符归一化。
    if (not name or len(name) > 128 or name in {".", ".."}
            or any(ord(c) < 32 or c in _KB_FORBIDDEN_CHARS for c in name)
            or name[-1] in {" ", "."}):
        raise KbError(f"文件名不合法: {p.name!r}")
    return p


def kb_directions_for(packs_root: str | Path, cap: str) -> list[str]:
    """cap → 参与列举/检索的 kb 方向列表。

    `cap == KB_ALL_CAP` 时展开为**盘上全部** kb 方向（`list_directions`），
    否则单元素直通。只读口径专用；写入口径不认哨兵（见 KB_ALL_CAP 注释）。
    """
    if cap == KB_ALL_CAP:
        from core.skills.taxonomy import list_directions  # 就地导入防环
        return list_directions(packs_root)
    return [cap]


def _sources(packs_root: str | Path, cap: str) -> list[KbSource]:
    sources = load_kb_sources(packs_root, [cap])
    if not sources:
        # 文案必须给**合法值**与**高频错法**（2026-10-21 Nan 报 bug）：
        # 只说「packs/kb/forensics-kb/ 不存在」时模型只能猜（试 forensics_kb？），
        # 而真实原因是它照抄了 kb_search 的「源」字段（KbSource.id = `<域>-kb`）。
        # 报错点名这条，模型才能一次改对。
        from core.skills.taxonomy import KB_DIRECTIONS
        legal = "、".join(KB_DIRECTIONS)
        hint = ""
        if cap.endswith("-kb"):
            hint = (f" 你传的 {cap!r} 是**内部源标识**（KbSource.id），不是目录名；"
                    f"应改为 {cap[:-3]!r}。")
        raise KbError(
            f"能力域 {cap!r} 不存在（packs/kb/{cap}/ 未找到）。{hint}"
            f"合法 cap 取 path 的第一段：{legal}。")
    return sources


def resolve_kb(packs_root: str | Path, cap: str, rel_path: str) -> KbTarget:
    """把 (能力域, 模块路径) 解析为落盘目标（expert-pool M0）。

    module 支持两形态，存在性消歧、全局形态优先：
    - 全局 ``<域>/<快照>/<路径>``（首段==cap，剥域后按源内相对解析）——kb_open 主形态；
    - 域内相对 ``<快照>/<路径>``（首段≠cap 或单段）——向后兼容旧引用。
    已存在文件按落点归属源；新文件默认落第一个源（全局形态剥域后落盘，
    KbTarget.rel 恒为源内相对）。resolve+parents 双保险防穿越。"""
    rel = _validate_rel(rel_path)
    root = Path(packs_root)
    sources = _sources(root, cap)
    # 全局形态（首段==域）剥域为源内相对；与原样形态都按存在性归属
    candidates: list[Path] = []
    if len(rel.parts) > 1 and rel.parts[0] == cap:
        candidates.append(Path(*rel.parts[1:]))
    candidates.append(rel)
    for cand in candidates:
        for src in sources:
            resolved = (src.root / cand).resolve()
            if resolved.is_file() and src.root in resolved.parents:
                return KbTarget(cap=cap, source=src, rel=cand, path=resolved)
    # 新文件默认落登记的第一个源（现网均为单源 root=kb/<域>）
    chosen = sources[0]
    chosen_rel = candidates[0]
    resolved = (chosen.root / chosen_rel).resolve()
    try:
        if resolved != chosen.root and chosen.root not in resolved.parents:
            raise KbError("路径越出知识库根")
    except OSError as e:
        raise KbError(f"非法路径: {e}") from e
    return KbTarget(cap=cap, source=chosen, rel=chosen_rel, path=resolved)


def _validate_content(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise KbError("内容不能为空")
    if len(content.encode("utf-8")) > KB_MAX_BYTES:
        raise KbError(f"单文件超过 {KB_MAX_BYTES // 1024} KiB 上限")
    if "\x00" in content:
        raise KbError("内容含空字节")
    return content


# ---------- kb 列举/读 ----------

def _iter_kb_files(src: KbSource):
    """遍历一个 kb 源的全部内容文件（K5：.md/.py/.txt/.json，排除 .history 与
    route.json 等平台自维护文件）。"""
    if not src.root.is_dir():
        return
    iterator = src.root.rglob("*") if src.recursive else src.root.glob("*")
    for f in sorted(iterator):
        if not f.is_file():
            continue
        rel = f.relative_to(src.root)
        if ".history" in rel.parts:
            continue
        if f.suffix.lower() not in KB_FILE_SUFFIXES:
            continue
        if f.name in _KB_NON_CONTENT_FILES and f.parent == src.root:
            continue
        yield f


def list_kb(packs_root: str | Path, cap: str) -> list[dict]:
    """列某能力包知识库：每个源一棵树（K5：.md/.py/.txt/.json，排除 .history），
    按 posix 相对路径排序。

    每文件带 title（kbindex 同口径：frontmatter title > 第一个 # H1 > 文件名 stem），
    供前端树行显中文标题——kb 树重做后文件名不再含编号语义（DESIGN.md §4 F15）。

    ``cap == KB_ALL_CAP``（「全部」聚合视图，2026-10-23）：跨全部 kb 方向合并为
    **单棵源树**，文件 path 带方向前缀（= kb_open 的全局 module 形态
    `<域>/<快照>/<路径>`）。三点理由：

    1. 前端树的 key/选中态都是 path 字符串，跨域同名路径（如 `实战打法/SQL.md`
       同时存在于 web 与 misc）不加前缀必然撞 key；
    2. 前缀与 `route.json` 的导航前缀同形，点「任务导航」过滤树照常生效；
    3. 选中后由前端按首段还原方向，编辑器/改名/删除仍走具体方向接口。
    """
    from core.skills.kbindex import _extract_title  # kbindex 不反向依赖 writing，就地导入防环

    head_bytes = 2048  # title/H1 都在文件头部，读首部即可（kbindex 段落索引才需全文）

    directions = kb_directions_for(packs_root, cap)
    merged = len(directions) > 1
    out = []
    for direction in directions:
        for src in _sources(Path(packs_root), direction):
            files: list[dict] = []
            for f in _iter_kb_files(src):
                st = f.stat()
                head = f.open("rb").read(head_bytes).decode("utf-8", errors="replace")
                rel = f.relative_to(src.root).as_posix()
                files.append({"path": f"{direction}/{rel}" if merged else rel,
                              "size": st.st_size,
                              "title": _extract_title(head, f.stem),
                              "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(st.st_mtime))})
            if not files:
                continue
            out.append({"id": src.id if not merged else f"{direction}-kb",
                        "recursive": src.recursive,
                        "root": src.root.name, "files": files})
    if merged:
        # 合并为单源（前端 `sources.length > 1` 才显源标题；聚合视图靠 path 首段
        # 区分方向，多源反而把同一批文件拆成 6 段重复表头）
        return [{"id": "all-kb", "recursive": True, "root": "全部",
                 "files": [f for s in out for f in s["files"]]}]
    return out


# ---------- kb 正文搜索 ----------

# 单文件命中计数上限（排序够用，防超大文档刷爆计数）
_KB_SEARCH_COUNT_CAP = 20
# snippet 首处命中前后保留的字符数
_KB_SNIPPET_CHARS = 60
# 中文查询降级检索的阈值（与 kbsearch 同口径，见该模块 _COVERAGE_MIN 注释）
_KB_COVERAGE_MIN = 0.34
_KB_GENERIC_DF_RATIO = 0.6
_KB_HIT_RATIO_MIN = 0.6


def _kb_scan(packs_root: str | Path, cap: str, needles: list[str],
             require_all: bool) -> list[dict]:
    """单遍 kb 源树扫描：require_all=True 为 AND（各词都命中才算），
    False 为 OR（任一词命中即收）。snippet 锚定最先出现的命中词。"""
    out: list[dict] = []
    for src in _sources(Path(packs_root), cap):
        for f in _iter_kb_files(src):
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            low = text.lower()
            counts = [low.count(n) for n in needles]
            if require_all and not all(counts):
                continue
            if not any(counts):
                continue
            # 锚点=正文中最先出现的命中词（snippet 从它展开）
            firsts = [(low.find(n), i) for i, n in enumerate(needles) if counts[i]]
            pos, ai = min(firsts)
            anchor = needles[ai]
            start = max(0, pos - _KB_SNIPPET_CHARS)
            end = min(len(text), pos + len(anchor) + _KB_SNIPPET_CHARS)
            out.append({"path": f.relative_to(src.root).as_posix(),
                        "source": src.id,
                        "matches": min(sum(counts), _KB_SEARCH_COUNT_CAP),
                        "snippet": text[start:end].replace("\n", " ").strip()})
    return out


def _kb_scan_coverage(packs_root: str | Path, cap: str, ascii_terms: list[str],
                      cjk_terms: list[str]) -> list[dict]:
    """IDF 加权覆盖率扫描（严格 AND 零结果时的降级路径，2026-10-19 中文修复）。

    与 ``kbsearch.KbSearchIndex._coverage_rows`` 同口径：ASCII 词元硬约束，
    中文词元按 df 加权、泛词剔除，命中权重和 ≥ ``_KB_COVERAGE_MIN`` 且命中
    词元数 ≥ ``_KB_HIT_RATIO_MIN`` 才收。legacy 扫描器只在小语料（测试夹具/
    索引不可用）时生效，故用单遍内存统计实现 df，不做 SQL 聚合。"""
    sources = list(_sources(Path(packs_root), cap))
    docs: list[tuple[str, str, str]] = []   # (relpath, source_id, low_text)
    for src in sources:
        for f in _iter_kb_files(src):
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            docs.append((f.relative_to(src.root).as_posix(), src.id, text.lower()))
    if not docs:
        return []
    total = len(docs)

    def _df(term: str) -> int:
        return sum(1 for _, _, low in docs if term in low)

    live = [t for t in cjk_terms if 0 < _df(t) <= total * _KB_GENERIC_DF_RATIO]
    if not live and not ascii_terms:
        return []
    weights = [(t, math.log(1 + total / max(1, _df(t))) * 2.0) for t in ascii_terms]
    weights += [(t, math.log(1 + total / max(1, _df(t)))) for t in live]
    total_weight = sum(w for _, w in weights) or 1.0
    need_hits = max(1, math.ceil(len(weights) * _KB_HIT_RATIO_MIN))

    out: list[dict] = []
    for relpath, source_id, low in docs:
        if not all(t in low for t in ascii_terms):
            continue
        matched = [(t, w) for t, w in weights if t in low]
        if len(matched) < need_hits:
            continue
        cov = sum(w for _t, w in matched)
        if cov / total_weight < _KB_COVERAGE_MIN:
            continue
        counts = [(low.count(t), t) for t, _w in matched]
        anchor = max(counts, key=lambda c: c[0])[1]
        pos = low.find(anchor)
        start = max(0, pos - _KB_SNIPPET_CHARS)
        end = min(len(low), pos + len(anchor) + _KB_SNIPPET_CHARS)
        out.append({"path": relpath, "source": source_id,
                    "matches": min(sum(c for c, _t in counts), _KB_SEARCH_COUNT_CAP),
                    "snippet": low[start:end].replace("\n", " ").strip(),
                    "_cov": cov})
    # cov 只记「命中与否」：同一批词元都命中的文档 cov 必然完全相等，仅按它
    # 排序会退化成 relpath 字母序（与索引侧同一缺陷）。命中次数作次级键。
    out.sort(key=lambda r: (-r["_cov"], -r["matches"], r["path"]))
    for row in out:
        row.pop("_cov", None)
    return out


def _facets_of(packs_root: Path, cap: str, module: str) -> set[str]:
    """读单文件的 frontmatter 分面标签（phase/vuln_class，K5 升级项 C），
    归一成小写集合；无 frontmatter/读盘失败返空集。"""
    from core.skills.kbindex import parse_facets  # 就地导入防环
    try:
        target = resolve_kb(packs_root, cap, module)
        if not target.path.is_file():
            return set()
        head = target.path.open("rb").read(2048).decode("utf-8", errors="replace")
        return parse_facets(head)
    except (KbError, OSError):
        return set()


def search_kb(packs_root: str | Path, cap: str, q: str, limit: int = 50,
              tag: str | None = None) -> list[dict]:
    """kb 正文搜索（只读，与 list_kb 同一源遍历口径）。

    大小写不敏感 substring；kb 文件量级为数百篇 × ≤1 MiB，全量遍历即可，不建索引。
    K3 多关键词：切词后 AND 语义（各词都命中才算），零结果按词元类型分两路放宽：
    - 纯 ASCII 查询 → OR 回退（任一词命中，按总命中数排）——「jwt none 验证」
      类组合查询先精确后放宽（保持既有行为）；
    - 含中文查询 → **IDF 加权覆盖率降级**（2026-10-19 中文修复）。自然语言问句
      （「如何测试文件上传漏洞」）切出的 2-gram 必然含跨词边界的死词元，全 AND
      等于要求用户背关键词；纯 OR 又会让虚词把无关文档拉满。改按词元稀有度加权，
      命中权重和 ≥ ``_KB_COVERAGE_MIN`` 才收，ASCII 词元仍作硬约束。
    K5 分面过滤：tag 非空时只留 frontmatter phase/vuln_class 命中该标签的文件
    （正交于目录单轴的跨轴检索，Anthropic-Cybersecurity-Skills 模式）。
    每命中文件回 {path, source, matches, snippet}；按命中次数降序、同分按路径，
    cap limit。空 q 返回空列表（交由调用方决定是否提示）。"""
    # Real packs use the incremental FTS5 index.  Small fixture trees and
    # explicitly missing index directories retain the deterministic legacy
    # scanner, which keeps this API useful during tests and recovery.
    try:
        from core.skills.kbsearch import KbSearchIndex
        if Path(packs_root).resolve().name == "packs":
            rows = KbSearchIndex(packs_root).search(
                q, capabilities=[cap], limit=limit, tag=tag)
            # An empty FTS result must fall through to the legacy scanner:
            # fixtures and CJK/OR queries can be valid even when FTS tokenization
            # produces no row.
            if rows:
                return rows
    except Exception:  # index is an optimization; never block KB access
        pass
    ascii_terms, cjk_terms = query_tokens(q)
    needles = ascii_terms + cjk_terms
    if not needles:
        # 退化：切词把整句都判成虚词（如只问「怎么办」）时，仍按空格原样搜，
        # 免得把用户的明确输入当成空查询吞掉。
        needles = [t for t in (q or "").strip().lower().split() if t]
    if not needles:
        return []
    out = _kb_scan(packs_root, cap, needles, require_all=True)
    if not out:
        if cjk_terms:
            # 含中文查询：覆盖率降级是**唯一**放宽路径。不能再走整串 OR——
            # 中文词元本来就多（一句问句切出 6-8 个），整串 OR 等于「中一个
            # 泛词就算命中」，任何查询都能刷出满屏结果，「无命中」语义消失。
            out = _kb_scan_coverage(packs_root, cap, ascii_terms, cjk_terms)
        elif len(needles) > 1:
            # 纯 ASCII 查询保持既有 K3 行为：AND 零结果 → OR 回退
            out = _kb_scan(packs_root, cap, needles, require_all=False)
    if tag and tag.strip():
        t = tag.strip().lower()
        root = Path(packs_root)
        out = [r for r in out if t in _facets_of(root, cap, r["path"])]
    out.sort(key=lambda r: (-r["matches"], r["path"]))
    for row in out:
        rel = row.get("path", "")
        # 2026-10-14 树状化：模块层中文化，kind 按新模块名前缀判层
        # （实战打法/后渗透=打法，赛事专题=案例，载荷/工具=参考；patterns/cases
        # 为历史类目名保留兜底）
        row["kind"] = next((k for p, k in (
            ("实战打法/", "playbook"), ("后渗透/", "playbook"),
            ("patterns/", "pattern"), ("cases/", "case"),
            ("赛事专题/", "case"), ("refs/", "ref"),
            ("载荷与字典/", "ref"), ("工具手册/", "ref"),
        ) if rel.startswith(p)), "reference")
        row["layer"] = {"playbook": 4, "pattern": 3, "case": 2,
                         "ref": 1, "reference": 1}.get(row["kind"], 1)
    out.sort(key=lambda r: (-r.get("layer", 1), -r["matches"], r["path"]))
    return out[:max(1, limit)]


# ---------- kb 备份布局（kb/<root>/.history/kb-backups 与 kb-trash） ----------

def _kb_history_root(target: KbTarget) -> Path:
    return target.source.root / ".history"


def _flatten(rel: Path) -> str:
    return "__".join(rel.parts)


def _kb_backup_path(target: KbTarget) -> Path:
    base = _kb_history_root(target) / "kb-backups"
    flat = _flatten(target.rel)
    ts = _utc_ts()
    dest = base / f"{ts}__{flat}.bak"
    n = 1
    while dest.exists():
        dest = base / f"{ts}.{n}__{flat}.bak"
        n += 1
    return dest


def _kb_trash_path(target: KbTarget) -> Path:
    base = _kb_history_root(target) / "kb-trash"
    flat = _flatten(target.rel)
    ts = _utc_ts()
    dest = base / f"{flat}.{ts}.bak"
    n = 1
    while dest.exists():
        dest = base / f"{flat}.{ts}.{n}.bak"
        n += 1
    return dest


# ---------- kb 写/删/改名/版本 ----------

def write_kb_file(packs_root: str | Path, cap: str, rel_path: str, content: str,
                  *, create: bool) -> dict:
    """新建（POST，create=True，文件必须不存在）或全文改写（PUT，create=False，必须存在）。

    写前留备份到 kb/.history/kb-backups/。返回 {path, backup}。"""
    text = _validate_content(content)
    with pack_write_lock():
        target = resolve_kb(packs_root, cap, rel_path)
        exists = target.path.is_file()
        if create and exists:
            raise KbError(f"文件已存在: {target.module}（改写请用 PUT）")
        if not create and not exists:
            raise KbError(f"文件不存在: {target.module}（新建请用 POST）")
        backup = None
        if exists:
            dest = _kb_backup_path(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target.path, dest)
            backup = dest.name
        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_text(text, encoding="utf-8")
        return {"path": target.module, "backup": backup, "created": not exists}


def delete_kb_file(packs_root: str | Path, cap: str, rel_path: str) -> dict:
    """删除 kb 文件 → 移入 kb/.history/kb-trash/（可恢复，不物理抹除）。"""
    with pack_write_lock():
        target = resolve_kb(packs_root, cap, rel_path)
        if not target.path.is_file():
            raise KbError(f"文件不存在: {target.module}")
        dest = _kb_trash_path(target)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target.path), str(dest))
        return {"path": target.module, "trash": dest.name}


def rename_kb_move(packs_root: str | Path, cap: str, old_rel: str, new_rel: str) -> dict:
    """同源内移动 kb 文件（不备份文件自身——内容不变，回退=再改名；引用文件的备份
    与替换由 refs.rewrite_module 负责，见 C3）。新路径必须不存在。"""
    with pack_write_lock():
        old = resolve_kb(packs_root, cap, old_rel)
        new = resolve_kb(packs_root, cap, new_rel)
        if old.source.root != new.source.root:
            raise KbError("改名仅限同一知识库源内（跨源移动不支持）")
        if not old.path.is_file():
            raise KbError(f"文件不存在: {old.module}")
        if new.path.exists():
            raise KbError(f"目标已存在: {new.module}")
        new.path.parent.mkdir(parents=True, exist_ok=True)
        old.path.rename(new.path)
        return {"old_path": old.module, "new_path": new.module}


# ---------- 技能目录写/删（提案 skill create/delete，2026-10-14） ----------
# 技能是扁平目录 packs/kb/skills/<name>/SKILL.md（registry 单层 glob 扫描）。
# 新建=建目录+写 SKILL.md；删除=整目录移入 skills/.history/skills-trash/（可恢复，
# 与 delete_kb_file 同款「不物理抹除」纪律）；两者都只在提案 apply 内被调用。

# 技能目录名白名单：registry 以 frontmatter name 为键，且 name 必须等于目录名，
# 故目录名要能被 registry 扫到又无路径注入面（禁 / \ .. 与前后空白）。
#
# 2026-10-19 中文支持：旧实现是纯 ASCII 白名单（`[A-Za-z0-9]…`），与 API 层
# `app._NAME_RE`（`\w` 开头，Unicode-aware，放行中文）口径不一致——设置页能建
# 出 `skills/注入测试/`，但提案 create/delete 走本函数时被判「非法技能名」，
# 同一个技能一半操作能跑一半不能。现统一为 Unicode 词字符（`\w` 含中文/日文/
# 假名等），仍禁路径分隔符与 Windows 非法字符，并拒绝 `.`/`..` 与尾随空格/句点
# （Windows 会静默裁剪尾随点空格，导致目录名与 frontmatter name 失配）。
_SKILL_NAME_RE = re.compile(r"^[\w][\w.-]{0,63}$", re.UNICODE)
_SKILL_NAME_FORBIDDEN = set('<>:"|?*\\/\x00')


def _validate_skill_name(name: str) -> str:
    """技能名归一与校验（create/delete/提案校验共用唯一口径）。"""
    n = (name or "").strip()
    if not n or len(n) > 64 or not _SKILL_NAME_RE.fullmatch(n):
        raise KbError(f"非法技能名: {name!r}（限字母/数字/中文等词字符与 . _ -，"
                      "须词字符开头，≤64 字符）")
    if any(c in _SKILL_NAME_FORBIDDEN for c in n) or n[-1] in {" ", "."}:
        raise KbError(f"非法技能名: {name!r}（含路径分隔符/Windows 非法字符，"
                      "或以空格/句点结尾）")
    return n


def resolve_skill_dir(packs_root: str | Path, name: str) -> Path:
    """技能目录（packs/kb/skills/<name>），name 非法抛 KbError。"""
    return skill_dir(packs_root, _validate_skill_name(name))


def create_skill(packs_root: str | Path, name: str, content: str) -> dict:
    """新建技能目录 + SKILL.md（提案 skill create 应用）。目录必须不存在；
    frontmatter name 必须与目录名一致（registry 以 name 为键，错位会造成双名/覆盖）。"""
    text = _validate_content(content)
    root = resolve_skill_dir(packs_root, name)
    from core.skills.registry import parse_frontmatter
    meta = parse_frontmatter(text)
    if (meta.get("name") or "").strip() != root.name:
        raise KbError(f"frontmatter name 必须与技能目录名一致: {root.name!r}")
    with pack_write_lock():
        if root.exists():
            raise KbError(f"技能已存在: {root.name}")
        root.mkdir(parents=True, exist_ok=False)
        (root / "SKILL.md").write_text(text, encoding="utf-8")
        return {"path": f"skills/{root.name}/SKILL.md", "created": True}


def delete_skill(packs_root: str | Path, name: str) -> dict:
    """删除技能目录 → 移入 skills/.history/skills-trash/（可恢复，不物理抹除）。
    .history 是隐藏目录且多一层，registry 的单层 glob 扫不到，删除即下线。"""
    root = resolve_skill_dir(packs_root, name)
    with pack_write_lock():
        if not (root / "SKILL.md").is_file():
            raise KbError(f"技能不存在: {root.name}")
        trash = root.parent / ".history" / "skills-trash"
        trash.mkdir(parents=True, exist_ok=True)
        dest = trash / f"{root.name}.{_utc_ts()}"
        n = 1
        while dest.exists():
            dest = trash / f"{root.name}.{_utc_ts()}.{n}"
            n += 1
        shutil.move(str(root), str(dest))
        return {"path": f"skills/{root.name}", "trash": dest.name}


def kb_versions(packs_root: str | Path, cap: str, rel_path: str) -> list[dict]:
    """列某 kb 文件的历史版本（kb-backups 内，按时间倒序）。"""
    target = resolve_kb(packs_root, cap, rel_path)
    base = _kb_history_root(target) / "kb-backups"
    if not base.is_dir():
        return []
    flat = _flatten(target.rel)
    # <ts>[.n]__<flat>.bak
    pat = re.compile(r"(\d{8}T\d{6}Z)(?:\.\d+)?__" + re.escape(flat) + r"\.bak$")
    out = []
    for f in base.iterdir():
        m = pat.fullmatch(f.name)
        if m and f.is_file():
            st = f.stat()
            out.append({"version": f.name, "ts": m.group(1), "size": st.st_size,
                        "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(st.st_mtime))})
    out.sort(key=lambda v: v["ts"], reverse=True)
    return out


def kb_version_path(packs_root: str | Path, cap: str, rel_path: str, version: str) -> Path:
    """取版本文件的绝对路径（非法版本名/越界 → KbError，不存在由调用方判 404）。"""
    target = resolve_kb(packs_root, cap, rel_path)
    if not re.fullmatch(r"\d{8}T\d{6}Z(?:\.\d+)?__[\w .()\-]+\.bak", version):
        raise KbError(f"非法版本名: {version}")
    base = (target.source.root / ".history" / "kb-backups").resolve()
    vp = (base / version).resolve()
    if base not in vp.parents:
        raise KbError("版本路径越界")
    return vp


def rollback_kb_file(packs_root: str | Path, cap: str, rel_path: str, version: str) -> dict:
    """回滚：先把当前版再备份一次（可往返），再用历史版覆盖（文件被删后回滚=恢复）。"""
    with pack_write_lock():
        vp = kb_version_path(packs_root, cap, rel_path, version)
        if not vp.is_file():
            raise KbError(f"历史版本不存在: {version}")
        target = resolve_kb(packs_root, cap, rel_path)
        backup = None
        if target.path.is_file():
            dest = _kb_backup_path(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target.path, dest)
            backup = dest.name
        target.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vp, target.path)
        return {"path": target.module, "rolled_back": version, "current_backed_up": backup}


# ---------- 技能文件定位（提案 skill 应用复用；零 fastapi 依赖） ----------

def skill_file_path(packs_root: str | Path, skill_name: str) -> Path:
    """技能 SKILL.md 绝对路径（扁平：packs/kb/skills/<name>/SKILL.md）。不做存在性承诺。"""
    return skill_dir(packs_root, skill_name) / "SKILL.md"


def json_dump_atomic(path: Path, data: dict) -> None:
    """原子 JSON 落盘（同卷 tmp+replace），调用方负责 pack_write_lock 临界区。"""
    _json_dump(path, data)


def _json_dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
