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
        with _INDEX_LOCK:
            return self._sync_locked(capabilities)

    def _sync_locked(self, capabilities: list[str] | None = None) -> dict[str, int]:
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
        self.sync(capabilities)
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
                # 只在 content 列匹配；title/summary/relpath 是展示元数据，不能
                # 改变正文搜索结果（否则 README.md 这类文件名会制造假命中）。
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
        finally:
            con.close()
        out = []
        for row in rows:
            text = row["content"]
            low = text.lower()
            positions = [low.find(t) for t in terms if low.find(t) >= 0]
            pos = min(positions) if positions else 0
            snippet = text[max(0, pos - 60):pos + 180].replace("\n", " ").strip()
            out.append({"path": row["relpath"], "source": row["source"],
                        "matches": sum(low.count(t) for t in terms),
                        "snippet": snippet, "kind": row["kind"],
                        "layer": row["layer"], "title": row["title"],
                        "is_attachment": bool(row["is_attachment"])})
        return out

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
                       limit: int) -> list[sqlite3.Row]:
        """IDF 加权覆盖率降级检索（严格 AND 零结果时使用）。

        ASCII 词元恒作硬约束；中文词元按 df 加权，泛词（df > 60% 语料）剔除，
        语料里完全不存在的词元（跨词边界 2-gram）自然被 df=0 排除。
        双门槛：命中权重占比 ≥ ``_COVERAGE_MIN`` **且** 命中词元个数占比
        ≥ ``_HIT_RATIO_MIN``——前者防泛词刷权重，后者防「只中一个词」蒙对。"""
        cap_list = sorted(caps)
        cap_sql = (" AND d.cap IN (" + ",".join("?" for _ in cap_list) + ")"
                   if cap_list else "")
        tag_sql = " AND lower(d.tags) LIKE ?" if tag else ""
        tag_params: list[Any] = ["%" + tag.lower() + "%"] if tag else []

        def _df(term: str) -> int:
            n = con.execute(
                "SELECT count(*) FROM kb_documents d WHERE lower(d.content) LIKE ?"
                + cap_sql + tag_sql,
                ["%" + term + "%"] + cap_list + tag_params).fetchone()[0]
            return int(n or 0)

        total_docs = con.execute("SELECT count(*) FROM kb_documents d WHERE 1=1"
                                 + cap_sql + tag_sql,
                                 cap_list + tag_params).fetchone()[0] or 0
        if not total_docs:
            return []

        live = [t for t in cjk_terms
                if 0 < _df(t) <= total_docs * self._GENERIC_DF_RATIO]
        if not live and not ascii_terms:
            return []

        # ASCII 词元权重 ×2：技术词（sql/jwt/ssti）特异性远高于中文 2-gram。
        weights = [(t, math.log(1 + total_docs / max(1, _df(t))) * 2.0)
                   for t in ascii_terms]
        weights += [(t, math.log(1 + total_docs / max(1, _df(t)))) for t in live]
        total_weight = sum(w for _, w in weights) or 1.0
        score_terms = [t for t, _ in weights]
        need_hits = max(1, math.ceil(len(score_terms) * self._HIT_RATIO_MIN))

        score_expr = "+".join(
            f"(CASE WHEN lower(d.content) LIKE ? THEN {w:.5f} ELSE 0 END)"
            for _, w in weights)
        hit_expr = "+".join(
            "(CASE WHEN lower(d.content) LIKE ? THEN 1 ELSE 0 END)"
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
        where = " AND ".join("lower(d.content) LIKE ?" for _ in ascii_terms)
        where += (" AND " if ascii_terms else "") + "(" + " OR ".join(
            "lower(d.content) LIKE ?" for _ in score_terms) + ")"
        sql = (f"SELECT d.*, ({score_expr}) AS cov, ({hit_expr}) AS hits, "
               f"({tf_expr}) AS tf "
               f"FROM kb_documents d WHERE {where}" + cap_sql + tag_sql
               + " ORDER BY cov DESC, tf DESC, d.layer DESC, d.relpath LIMIT ?")
        params = ([f"%{t}%" for t in score_terms]
                  + [f"%{t}%" for t in score_terms]
                  + list(score_terms)          # tf 用纯词元，不能带 LIKE 通配符
                  + [f"%{t}%" for t in ascii_terms]
                  + [f"%{t}%" for t in score_terms]
                  + cap_list + tag_params + [max(1, limit)])
        rows = list(con.execute(sql, params))
        return [r for r in rows
                if (r["cov"] or 0) / total_weight >= self._COVERAGE_MIN
                and (r["hits"] or 0) >= need_hits]
