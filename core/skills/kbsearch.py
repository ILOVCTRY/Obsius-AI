"""Incremental local KB index.

The index is deliberately local and rebuildable.  It stores text needed for
search together with a small amount of provenance and presentation metadata;
the source tree remains the source of truth.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core.skills.matching import query_tokens
from core.skills.rules import load_kb_sources

TEXT_SUFFIXES = {".md", ".py", ".txt", ".json", ".js", ".yml", ".yaml", ".dic", ".enc", ".dat"}
ATTACHMENT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
INDEX_SUFFIXES = TEXT_SUFFIXES | ATTACHMENT_SUFFIXES
_INDEX_LOCK = threading.RLock()
_SCHEMA_VERSION = "2"
#: 已完成建表/WAL 切换的库路径（进程级）。`journal_mode=WAL` 与 `CREATE TABLE
#: IF NOT EXISTS` 都是幂等的持久化操作，每连接重跑只会把 open+close 从 3ms 拖到
#: 400ms+（见 `_connect` 注释）。测试里每个 tmp_path 一个库，key 天然隔离。
_SCHEMA_READY: set[str] = set()

#: `search()` 内隐式 sync 的节流窗口（秒）。知识库是人手改的文件，30 秒内的
#: 变更延迟不可感知；而 sync 要遍历 800+ 文件并开一次连接，实测 730ms——
#: 对「动手前必做」的 kb_search 来说，这 0.7s 就是模型不爱用它的现实理由。
#: **显式 `sync()` 调用不受此限**（写库方要立即生效时照常全量同步）。
_SYNC_TTL_S = 30.0
#: (db_path, 方向元组) → 上次 sync 完成时间。
_LAST_SYNC: dict[tuple[str, tuple[str, ...]], float] = {}


def default_index_path(packs_root: str | Path) -> Path:
    root = Path(packs_root).resolve()
    return root.parent / "data" / "kb-index.db"


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# 单条 SQL 的绑定变量上限（SQLite 默认 999，取 400 留足余量）
_DELETE_CHUNK = 400


def _delete_fts_rows(con: sqlite3.Connection, keys) -> None:
    """按 path 批量删除 FTS 行——一条 `IN` 只扫一次，替代「每行一条 DELETE」。

    **为什么必须批量**：`kb_documents_fts.path` 是 UNINDEXED 列，FTS5 对非 rowid 条件
    的 DELETE 只能**全表扫描** content 影子表。逐行删 N 条 = 扫 N 次，且每次都要扫过
    已经插入的行 → O(N²)。实测 643 篇 web（12 MB 正文）：逐行删 3.86s / 批量删 0.46s
    （8.4x）；线上 1796 篇、235 MB 的库放大到 ~33s 每个方向，而一次 kb_search 要遍历
    6 个方向 → 用户实测单个工具调用卡住 4 分钟（2026-10-14 排查）。
    """
    keys = [k for k in keys if k]
    for chunk in _chunks(keys, _DELETE_CHUNK):
        con.execute("DELETE FROM kb_documents_fts WHERE path IN (%s)"
                    % ",".join("?" * len(chunk)), chunk)


def _layer(rel: str) -> int:
    head = rel.split("/", 2)[0].lower()
    return {"playbooks": 4, "patterns": 3, "cases": 2, "refs": 1}.get(head, 1)


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    out: dict[str, str] = {}
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            out[key.strip()] = value.strip().strip("[]").strip("\"'")
    return out


def _title(text: str, path: Path) -> str:
    fm = _frontmatter(text)
    if fm.get("title"):
        return fm["title"]
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _summary(text: str) -> str:
    body = text.split("---", 2)[-1] if text.startswith("---") and text.count("---") >= 2 else text
    for line in body.splitlines():
        value = line.strip().lstrip(">- *").strip()
        if value and not value.startswith("#"):
            return value[:240]
    return ""


def _iter_files(packs_root: Path, capabilities: list[str] | None):
    for src in load_kb_sources(packs_root, capabilities):
        if not src.root.is_dir():
            continue
        iterator = src.root.rglob("*") if src.recursive else src.root.glob("*")
        for path in sorted(iterator):
            if not path.is_file() or ".history" in path.relative_to(src.root).parts:
                continue
            if path.suffix.lower() not in INDEX_SUFFIXES:
                continue
            yield src, path


class KbSearchIndex:
    def __init__(self, packs_root: str | Path, db_path: str | Path | None = None):
        self.packs_root = Path(packs_root).resolve()
        self.db_path = Path(db_path) if db_path else default_index_path(self.packs_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=60.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=60000")
        # **建表/切 WAL 每个进程只做一次**（2026-10-21 性能修复）：`journal_mode=WAL`
        # 是**持久化**在库文件里的设置，`CREATE TABLE IF NOT EXISTS` 也是幂等的，
        # 每连接重跑一遍毫无收益，却要为写事务拿锁、生成 WAL，导致 `close()` 时
        # checkpoint 一次——实测单次 open+close 从 3ms 涨到 400~470ms。而
        # `search()` 一次要开两个连接（sync 一个、查询一个），光这一项就吃掉近 1s，
        # 是「kb_search 要 1.4s」的主因。
        key = str(self.db_path)
        first = key not in _SCHEMA_READY
        if first:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
            CREATE TABLE IF NOT EXISTS kb_documents (
                path TEXT PRIMARY KEY, cap TEXT NOT NULL, source TEXT NOT NULL,
                relpath TEXT NOT NULL, layer INTEGER NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, summary TEXT NOT NULL, tags TEXT NOT NULL,
                content TEXT NOT NULL, sha256 TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL, is_attachment INTEGER NOT NULL DEFAULT 0,
                indexed_at REAL NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_documents_fts USING fts5(
                path UNINDEXED, title, summary, tags, content,
                tokenize='unicode61'
            );
            CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            _SCHEMA_READY.add(key)
        version = con.execute("SELECT value FROM kb_meta WHERE key='schema_version'").fetchone()
        if version is None or version[0] != _SCHEMA_VERSION:
            # The source tree is authoritative.  A version bump must invalidate
            # derived rows so changes to kind/layer extraction cannot leave mixed
            # metadata in the local cache.
            con.execute("DELETE FROM kb_documents_fts")
            con.execute("DELETE FROM kb_documents")
            con.execute("INSERT OR REPLACE INTO kb_meta(key,value) VALUES ('schema_version',?)",
                        (_SCHEMA_VERSION,))
            con.commit()
        return con

    def sync(self, capabilities: list[str] | None = None) -> dict[str, int]:
        """显式全量同步（写库方在改动后调用，永远真的扫盘）。"""
        with _INDEX_LOCK:
            out = self._sync_locked(capabilities)
            if capabilities:
                _LAST_SYNC[(str(self.db_path), tuple(sorted(capabilities)))] = time.time()
            return out

    def _sync_throttled(self, capabilities: list[str] | None) -> None:
        """`search()` 用的隐式同步：TTL 内直接跳过。

        为什么只在隐式路径节流：显式 `sync()` 是「我刚改了库，立刻生效」的语义，
        节流会让它变成空操作（测试里「写文件 → sync → 断言新增」会直接红）。
        而 `search()` 只是「顺手保证索引不太旧」，30 秒的陈旧度对人工编辑的
        知识库没有影响。
        """
        if not capabilities:
            return
        key = (str(self.db_path), tuple(sorted(capabilities)))
        if time.time() - _LAST_SYNC.get(key, 0.0) < _SYNC_TTL_S:
            return
        with _INDEX_LOCK:
            # 双检：并发进来的第二个调用者不该再扫一遍
            if time.time() - _LAST_SYNC.get(key, 0.0) < _SYNC_TTL_S:
                return
            self._sync_locked(capabilities)
            _LAST_SYNC[key] = time.time()

    def _sync_locked(self, capabilities: list[str] | None = None) -> dict[str, int]:
        # **空方向保护（2026-10-21）**：`capabilities` 为空/None 时
        # `load_kb_sources()` 返回零个源 → `wanted` 为空 → 下面的 stale 计算会把
        # **全库**当成过期清掉（实测一次 `sync(None)` 删掉 1559 篇）。而 `search()`
        # 每次都会先调 `sync(capabilities)`，任何没有方向的会话/调用方只要搜一次
        # 就会把共享索引清空——这是数据丢失级隐患，不是「调用方用错参数」。
        #
        # 语义上「没有启用方向」= **本次不索引任何域**，而不是「磁盘上什么都不要了」；
        # 索引是全局共享的（按 path 存多域），单次调用无权代表全库做删除决定。
        # 故直接返回零变更，保留既有索引（检索时 `caps` 过滤仍会正确排除其他域）。
        if not capabilities:
            return {"added": 0, "updated": 0, "removed": 0, "total": 0,
                    "skipped": "no_directions"}
        wanted: dict[str, tuple[Any, Path]] = {}
        for src, path in _iter_files(self.packs_root, capabilities):
            rel = path.relative_to(src.root).as_posix()
            wanted[f"{src.root.name}/{rel}"] = (src, path)
        added = updated = removed = 0
        con = self._connect()
        try:
          with con:
            existing = {r["path"]: (r["sha256"], r["mtime_ns"], r["size"])
                        for r in con.execute("SELECT path,sha256,mtime_ns,size FROM kb_documents")}
            # 第一遍：只判定「哪些 key 需要重写」（stat 先过，mtime/size 变了才读盘算哈希）。
            # 必须先把待写集合定下来，才能在第二遍**一次性**删掉它们的旧 FTS 行——
            # 见下方 _delete_fts_rows 的性能注释。
            pending: list[tuple[str, Any, Path, Any, str, bytes]] = []
            for key, (src, path) in wanted.items():
                st = path.stat()
                old = existing.get(key)
                if old is not None and old[1] == st.st_mtime_ns and old[2] == st.st_size:
                    continue
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if old is not None and old[0] == digest:
                    continue
                pending.append((key, src, path, st, digest, raw))
            # 第二遍：批量清旧 FTS 行（只清真正会被重写的），再逐行写入。
            _delete_fts_rows(con, [k for k, *_ in pending if k in existing])
            for key, src, path, st, digest, raw in pending:
                is_attachment = path.suffix.lower() not in TEXT_SUFFIXES
                text = raw.decode("utf-8", errors="replace") if not is_attachment else ""
                fm = _frontmatter(text)
                rel = path.relative_to(src.root).as_posix()
                kind = fm.get("kind") or next((x[:-1] for x in ("playbooks", "patterns", "cases", "refs") if rel.startswith(x + "/")), "ref")
                tags = ",".join(x.strip() for x in fm.get("vuln_class", "").split(",") if x.strip())
                row = (key, src.root.name, src.id, rel, _layer(rel), kind,
                       _title(text, path) if text else path.name,
                       _summary(text), tags, text, digest, st.st_mtime_ns, st.st_size,
                       int(is_attachment), time.time())
                con.execute("""INSERT OR REPLACE INTO kb_documents
                    (path,cap,source,relpath,layer,kind,title,summary,tags,content,
                     sha256,mtime_ns,size,is_attachment,indexed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row)
                con.execute("INSERT INTO kb_documents_fts(path,title,summary,tags,content) VALUES (?,?,?,?,?)",
                            (key, row[6], row[7], row[8], row[9]))
                updated += int(key in existing)
                added += int(key not in existing)
            requested_caps = set(capabilities or [])
            stale = set(existing) - set(wanted)
            if requested_caps:
                stale = {key for key in stale if key.split("/", 1)[0] in requested_caps}
            _delete_fts_rows(con, stale)
            for key in stale:
                con.execute("DELETE FROM kb_documents WHERE path=?", (key,))
                removed += 1
            con.execute("INSERT OR REPLACE INTO kb_meta(key,value) VALUES ('last_sync',?)", (str(time.time()),))
        finally:
            con.close()
        return {"added": added, "updated": updated, "removed": removed,
                "total": len(wanted)}

    @staticmethod
    def _match_terms(query: str) -> list[str]:
        """查询 → 检索词元（与 writing.search_kb 同口径，见 matching.query_tokens）。

        2026-10-19 中文修复：旧实现只切 2-gram 且不剔虚词，「如何测试文件上传
        漏洞」被切成 如何/何测/测试/试反/… 七个词元，全 AND 必然零命中（跨词
        边界的 2-gram 在语料里根本不存在）。现改为共用切词口径。"""
        ascii_tokens, cjk_tokens = query_tokens(query)
        return ascii_tokens + cjk_tokens

    def search(self, query: str, *, capabilities: list[str] | None = None,
               limit: int = 50, tag: str | None = None) -> list[dict[str, Any]]:
        self._sync_throttled(capabilities)
        ascii_terms, cjk_terms = query_tokens(query)
        terms = ascii_terms + cjk_terms
        if not terms:
            return []
        caps = set(capabilities or [])
        rows: list[sqlite3.Row] = []
        con = self._connect()
        try:
            # FTS gives fast ranking for Latin/tool terms.  CJK and mixed queries
            # fall back to LIKE so Chinese filenames and prose retain old behavior.
            has_cjk = any("\u4e00" <= c <= "\u9fff" for c in query)
            if not has_cjk:
                # 纯 ASCII 精确查询仍只匹配正文；避免单词恰好出现在文件名就当正文命中。
                match = " AND ".join(
                    'content : "' + t.replace('"', '""') + '"' for t in terms)
                sql = """SELECT d.*, bm25(kb_documents_fts, 1.0, 2.0, 1.5, 1.0, 1.0) rank
                         FROM kb_documents_fts f JOIN kb_documents d ON d.path=f.path
                         WHERE kb_documents_fts MATCH ?"""
                params: list[Any] = [match]
                if caps:
                    sql += " AND d.cap IN (" + ",".join("?" for _ in caps) + ")"
                    params.extend(sorted(caps))
                if tag:
                    sql += " AND lower(d.tags) LIKE ?"
                    params.append("%" + tag.lower() + "%")
                sql += " ORDER BY rank LIMIT ?"
                params.append(max(1, limit))
                rows = list(con.execute(sql, params))
            if not rows:
                # 严格 AND：所有词元都必须在正文出现。
                clauses = []
                for term in terms:
                    # 与 writing.search_kb 的正文 substring 契约保持一致：标题/路径
                    # 仅作展示，不应因为文件名命中而返回结果。
                    clauses.append("lower(d.content) LIKE ?")
                # 命中次数作主排序键（与 matches 口径一致）：单/少词元查询下所有
                # 命中文档条件相同，只按 layer/relpath 排会退化成字母序，把最相关
                # 的文档排到后面（「提权」实测 12-privesc.md 命中 36 次却排第 5）。
                tf_expr = "+".join(
                    "(LENGTH(lower(d.content))"
                    f"-LENGTH(REPLACE(lower(d.content),?, '')))/{max(1, len(t))}"
                    for t in terms)
                sql = (f"SELECT d.*, 0.0 rank, ({tf_expr}) AS tf "
                       "FROM kb_documents d WHERE " + " AND ".join(clauses))
                # tf 的 REPLACE 要纯词元（带 % 通配符会恒为 0），且参数顺序须
                # 先于 WHERE 的 LIKE 占位符——SELECT 子句在语句里排在前面。
                params = list(terms) + [f"%{t}%" for t in terms]
                if caps:
                    sql += " AND d.cap IN (" + ",".join("?" for _ in caps) + ")"
                    params.extend(sorted(caps))
                if tag:
                    sql += " AND lower(d.tags) LIKE ?"
                    params.append("%" + tag.lower() + "%")
                sql += " ORDER BY tf DESC, d.layer DESC, d.relpath LIMIT ?"
                params.append(max(1, limit))
                rows = list(con.execute(sql, params))
            if not rows and cjk_terms:
                # 严格 AND 零结果 → **IDF 加权覆盖率降级**（2026-10-19 中文修复）。
                # 旧行为是直接放弃（writing.search_kb 再回退整串 OR，中文问句下
                # 噪声极大）。自然语言问句必然带虚词与跨词边界 2-gram，要求 100%
                # 命中等于要求用户背关键词。改为：按「词元在语料中的稀有度」加权，
                # 只留 ≥_COVERAGE_MIN 权重的词元参与 OR，再按覆盖权重排序。
                # ASCII 词元是硬约束（用户明确打的技术词，必须命中）。
                rows = self._coverage_rows(con, query, ascii_terms, cjk_terms,
                                           caps, tag, limit)
            elif 0 < len(rows) < self._FALLBACK_MERGE_BELOW and cjk_terms:
                # **少量命中也要降级**（2026-10-21）：严格 AND 只要非零就短路，
                # 曾让「zip 附件 分诊 文件类型」只返回 1 条（文件上传漏洞，完全
                # 不对口）——因为那一条恰好同时含 4 个词元。模型看到 1 条不相关
                # 结果就认为「库里没有」，从此不再查。降级结果**追加在后面**：
                # 严格命中置信度更高，保留原顺序，只补召回。
                extra = self._coverage_rows(
                    con, query, ascii_terms, cjk_terms, caps, tag, limit,
                    min_coverage=self._MERGE_COVERAGE_MIN, min_hits=1)
                seen = {r["path"] for r in rows}
                rows = list(rows) + [r for r in extra if r["path"] not in seen]
                rows = rows[:max(1, limit)]
        finally:
            con.close()
        out = []
        for row in rows:
            text = row["content"]
            low = text.lower()
            title = str(row["title"] or "").lower()
            searchable_fields = {
                "content": low,
                "title": title,
                "summary": str(row["summary"] or "").lower(),
                "tags": str(row["tags"] or "").lower(),
                "path": str(row["relpath"] or "").lower(),
            }
            matched_terms = [t for t in terms
                             if any(t in value for value in searchable_fields.values())]
            title_terms = [t for t in matched_terms if t in title]
            summary_terms = [t for t in matched_terms if t in searchable_fields["summary"]]
            tags_terms = [t for t in matched_terms if t in searchable_fields["tags"]]
            relevance_score = (
                len(matched_terms)
                + 5 * len(title_terms)
                + 3 * len(summary_terms)
                + 3 * len(tags_terms)
                + 0.01 * sum(low.count(t) for t in matched_terms)
            )
            # Keep the historical matches contract (正文出现次数); field-aware ranking
            # is exposed separately for the Agent wrapper.
            positions = [low.find(t) for t in terms if low.find(t) >= 0]
            pos = min(positions) if positions else 0
            snippet = text[max(0, pos - 60):pos + 180].replace("\n", " ").strip()
            out.append({"path": row["relpath"], "source": row["source"],
                        "matches": sum(low.count(t) for t in terms),
                        "relevance_score": relevance_score,
                        "matched_terms": matched_terms,
                        "snippet": snippet, "kind": row["kind"],
                        "layer": row["layer"], "title": row["title"],
                        "is_attachment": bool(row["is_attachment"])})
        return out

    #: 严格 AND 命中数低于该值时，**同时**跑覆盖率降级并追加结果（2026-10-21）。
    #: 取 3 的实测依据：分诊类查询（「zip 附件 分诊 文件类型」）严格 AND 只中 1 条
    #: 且完全不对口，而降到 1 条时模型会判定「库里没有」直接放弃；≥3 条时命中
    #: 通常已经覆盖真实意图，再追加只会引入噪声。
    _FALLBACK_MERGE_BELOW = 3

    #: 合并路径的覆盖率下限（比 ``_COVERAGE_MIN`` 松）。理由：合并发生在严格 AND
    #: **已有**结果之后，高置信项已在最前，这里只补「部分命中」的候选；仍用 0.34
    #: 会把「伪加密 压缩包」这种两词查询卡死（只中一个词 = 0.5 能过，但四词查询
    #: 只中一个 = 0.25 过不去，而分诊查询恰恰多是三四个词）。
    _MERGE_COVERAGE_MIN = 0.15

    #: 降级检索时词元权重覆盖率下限：命中词元的 IDF 权重之和 ≥ 全部候选权重
    #: 的这个比例才入选。0.34 来自实测——自然语言问句里通常 1/3 是虚词噪声，
    #: 再高会把「文件上传绕过」这类真查询挤掉，再低则泛词重新污染结果。
    _COVERAGE_MIN = 0.34
    #: 语料中超过该比例的文档都含某词元 → 泛词，降级阶段剔除（无区分度）。
    _GENERIC_DF_RATIO = 0.6
    #: 命中词元数下限（占候选词元数的比例）：只中 1 个泛词不算命中。
    #: 「不存在的专题xyz」这类查询切出的词元全是常见词，靠这条挡成零结果。
    _HIT_RATIO_MIN = 0.6

    def _coverage_rows(self, con: sqlite3.Connection, query: str,
                       ascii_terms: list[str], cjk_terms: list[str],
                       caps: set[str], tag: str | None,
                       limit: int, *,
                       min_coverage: float | None = None,
                       min_hits: int | None = None) -> list[sqlite3.Row]:
        """IDF 加权覆盖率降级检索（严格 AND 零结果时使用）。

        ASCII 词元恒作硬约束；中文词元按 df 加权，泛词（df > 60% 语料）剔除，
        语料里完全不存在的词元（跨词边界 2-gram）自然被 df=0 排除。
        双门槛：命中权重占比 ≥ ``_COVERAGE_MIN`` **且** 命中词元个数占比
        ≥ ``_HIT_RATIO_MIN``——前者防泛词刷权重，后者防「只中一个词」蒙对。

        ``min_coverage`` / ``min_hits`` 供**合并路径**放宽用：那时严格 AND 已有
        结果排在前面，这里只补召回，不需要再守「必须中大半词元」——两个词的
        查询守 0.6 等于要求全中，合并就退化成重复查询（实测「伪加密 压缩包」
        只回 1 条就是这个原因）。"""
        cap_list = sorted(caps)
        cap_sql = (" AND d.cap IN (" + ",".join("?" for _ in cap_list) + ")"
                   if cap_list else "")
        tag_sql = " AND lower(d.tags) LIKE ?" if tag else ""
        tag_params: list[Any] = ["%" + tag.lower() + "%"] if tag else []

        # IDF 以正文统计，避免标题/路径泛词稀释正文分布；ASCII 缩写额外检查元数据，
        # 使文件名/标题中的产品名可以参与召回。中文字段召回仍由下方候选查询完成。
        _all_terms = list(dict.fromkeys(list(ascii_terms) + list(cjk_terms)))
        _search_text = "lower(d.content || ' ' || d.title || ' ' || d.summary || ' ' || d.tags || ' ' || d.relpath)"
        _body_text = "lower(d.content)"
        _df_map: dict[str, int] = {}
        _meta_df_map: dict[str, int] = {}
        if _all_terms:
            _exprs = ", ".join(
                f"SUM(CASE WHEN {_body_text} LIKE ? THEN 1 ELSE 0 END)"
                for _ in _all_terms)
            _row = con.execute(
                "SELECT " + _exprs + " FROM kb_documents d WHERE 1=1"
                + cap_sql + tag_sql,
                ["%" + t + "%" for t in _all_terms] + cap_list + tag_params
            ).fetchone()
            _df_map = {t: int(_row[i] or 0) for i, t in enumerate(_all_terms)}
            _meta_exprs = ", ".join(
                f"SUM(CASE WHEN {_search_text} LIKE ? THEN 1 ELSE 0 END)"
                for _ in _all_terms)
            _meta_row = con.execute(
                "SELECT " + _meta_exprs + " FROM kb_documents d WHERE 1=1"
                + cap_sql + tag_sql,
                ["%" + t + "%" for t in _all_terms] + cap_list + tag_params
            ).fetchone()
            _meta_df_map = {t: int(_meta_row[i] or 0) for i, t in enumerate(_all_terms)}
        for term in ascii_terms:
            if _df_map.get(term, 0) == 0:
                _row = con.execute(
                    "SELECT count(*) FROM kb_documents d WHERE "
                    + _search_text + " LIKE ?" + cap_sql + tag_sql,
                    ["%" + term + "%"] + cap_list + tag_params).fetchone()
                _df_map[term] = int(_row[0] or 0)
        _df = lambda t: _df_map.get(t, 0)  # noqa: E731

        total_docs = con.execute("SELECT count(*) FROM kb_documents d WHERE 1=1"
                                 + cap_sql + tag_sql,
                                 cap_list + tag_params).fetchone()[0] or 0
        if not total_docs:
            return []

        live = [t for t in cjk_terms
                if 0 < _meta_df_map.get(t, _df(t)) <= total_docs * self._GENERIC_DF_RATIO]
        # ASCII 与中文词元都作为分层召回线索，不把组合查询中的所有词强制要求
        # 共现于同一篇文档。ASCII 词提高权重；df=0 的缩写/新术语不进入分母，
        # 因而不会把整条混合查询清零。纯 ASCII 精确/OR 契约由上层路径保留。
        known_ascii = [t for t in ascii_terms if _df(t) > 0]
        # 含 ASCII 缩写但一个都没有索引证据时，仍保持“技术词未知不可凭泛词猜中”
        # 的安全护栏；只有存在至少一个已知 ASCII 词，未知缩写才作为可缺失线索降级。
        # 若全部 ASCII 词都未知，保留既有安全护栏，避免 jwt 等技术词缺失时
        # 仅凭中文泛词误召回；当存在已知 ASCII 词时，其他未知缩写不再阻断。
        if ascii_terms and not known_ascii:
            return []
        candidate_terms = known_ascii + live
        if not candidate_terms:
            return []

        effective_df = lambda t: _df(t) or _meta_df_map.get(t, 0)  # noqa: E731
        weights = [(t, math.log(1 + total_docs / max(1, effective_df(t))) * 2.0)
                   for t in known_ascii]
        weights += [(t, math.log(1 + total_docs / max(1, effective_df(t)))) for t in live]
        total_weight = sum(w for _, w in weights) or 1.0
        score_terms = [t for t, _ in weights]
        if min_hits is None:
            # 组合查询采用召回优先：单篇命中一条有效线索即可入候选，排序由 IDF、
            # 正文/元数据字段权重决定；全未知词仍由 candidate_terms 空集护栏拒绝。
            need_hits = 1
        else:
            need_hits = max(1, min_hits)
        need_cov = 0.0 if min_coverage is None else min_coverage

        body_score_expr = "+".join(
            f"(CASE WHEN {_body_text} LIKE ? THEN {w:.5f} ELSE 0 END)"
            for _, w in weights)
        score_expr = "+".join(
            f"(CASE WHEN {_search_text} LIKE ? THEN {w:.5f} ELSE 0 END)"
            for _, w in weights)
        hit_expr = "+".join(
            f"(CASE WHEN {_search_text} LIKE ? THEN 1 ELSE 0 END)"
            for _ in score_terms)
        body_hit_expr = "+".join(
            f"(CASE WHEN {_body_text} LIKE ? THEN 1 ELSE 0 END)"
            for _ in score_terms)
        # 词频（tf）：cov 只记「命中与否」，同一批词元都命中的文档 cov 必然完全
        # 相等——「如何测试文件上传漏洞」实测 10 篇全是 cov=9.544/hits=5，排序
        # 于是退化成按 relpath 字母序，真正讲该主题的《文件上传漏洞.md》被排到
        # 第 3 位（`layer` 在现网恒为 1，原来那句 `d.layer DESC` 也救不回来）。
        # 用 REPLACE 差值数出现次数作次级键，与 legacy 扫描器按命中次数排序的
        # 既有口径一致（matches 大者优先），且必须在 SQL 里参与 ORDER BY——只在
        # Python 侧重排会让 LIMIT 先按字母序截断，正确结果直接被切掉。
        tf_expr = "+".join(
            "(LENGTH(lower(d.content))"
            f"-LENGTH(REPLACE(lower(d.content),?, '')))/{max(1, len(t))}"
            for t in score_terms)
        where = "(" + " OR ".join(f"{_search_text} LIKE ?" for _ in score_terms) + ")"
        sql = (f"SELECT d.*, ({body_score_expr}) AS body_cov, ({score_expr}) AS cov, "
               f"({hit_expr}) AS hits, ({body_hit_expr}) AS body_hits, ({tf_expr}) AS tf "
               f"FROM kb_documents d WHERE {where}" + cap_sql + tag_sql
               + " ORDER BY body_cov DESC, cov DESC, tf DESC, d.layer DESC, d.relpath LIMIT ?")
        params = ([f"%{t}%" for t in score_terms]
                  + [f"%{t}%" for t in score_terms]
                  + [f"%{t}%" for t in score_terms]
                  + [f"%{t}%" for t in score_terms]
                  + list(score_terms)          # tf 用纯词元，不能带 LIKE 通配符
                  + [f"%{t}%" for t in score_terms]
                  + cap_list + tag_params + [max(1, limit)])
        rows = list(con.execute(sql, params))
        filtered = [r for r in rows
                    if (r["cov"] or 0) / total_weight >= need_cov
                    and (r["hits"] or 0) >= need_hits]
        # 合并正文高相关候选与标题/路径补充候选，正文结果保持在前面。
        meta_sql = (f"SELECT d.* FROM kb_documents d WHERE {where}" + cap_sql + tag_sql
                    + " ORDER BY d.layer DESC, d.relpath LIMIT ?")
        meta_rows = list(con.execute(meta_sql,
            [f"%{t}%" for t in score_terms] + cap_list + tag_params + [max(1, limit)]))
        seen = {r["path"] for r in filtered}
        return filtered + [r for r in meta_rows if r["path"] not in seen]
