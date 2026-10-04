"""Incremental local KB index.

The index is deliberately local and rebuildable.  It stores text needed for
search together with a small amount of provenance and presentation metadata;
the source tree remains the source of truth.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core.skills.rules import load_kb_sources

TEXT_SUFFIXES = {".md", ".py", ".txt", ".json", ".js", ".yml", ".yaml", ".dic", ".enc", ".dat"}
ATTACHMENT_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
INDEX_SUFFIXES = TEXT_SUFFIXES | ATTACHMENT_SUFFIXES
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:+/-]*")
_INDEX_LOCK = threading.RLock()
_SCHEMA_VERSION = "2"


def default_index_path(packs_root: str | Path) -> Path:
    root = Path(packs_root).resolve()
    return root.parent / "data" / "kb-index.db"


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
            for key, (src, path) in wanted.items():
                st = path.stat()
                old = existing.get(key)
                if old is not None and old[1] == st.st_mtime_ns and old[2] == st.st_size:
                    continue
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if old is not None and old[0] == digest:
                    continue
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
                con.execute("DELETE FROM kb_documents_fts WHERE path=?", (key,))
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
            for key in stale:
                con.execute("DELETE FROM kb_documents WHERE path=?", (key,))
                con.execute("DELETE FROM kb_documents_fts WHERE path=?", (key,))
                removed += 1
            con.execute("INSERT OR REPLACE INTO kb_meta(key,value) VALUES ('last_sync',?)", (str(time.time()),))
        finally:
            con.close()
        return {"added": added, "updated": updated, "removed": removed,
                "total": len(wanted)}

    @staticmethod
    def _match_terms(query: str) -> list[str]:
        terms: list[str] = []
        for token in _WORD_RE.findall(query.lower()):
            if len(token) > 1:
                terms.append(token)
        for token in re.findall(r"[\u4e00-\u9fff]+", query.lower()):
            if len(token) <= 4:
                terms.append(token)
            else:
                terms.extend(token[i:i + 2] for i in range(len(token) - 1))
        return list(dict.fromkeys(terms))

    def search(self, query: str, *, capabilities: list[str] | None = None,
               limit: int = 50, tag: str | None = None) -> list[dict[str, Any]]:
        self.sync(capabilities)
        terms = self._match_terms(query)
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
                clauses = []
                params = []
                for term in terms:
                    # 与 writing.search_kb 的正文 substring 契约保持一致：标题/路径
                    # 仅作展示，不应因为文件名命中而返回结果。
                    clauses.append("lower(d.content) LIKE ?")
                    params.append("%" + term + "%")
                sql = "SELECT d.*, 0.0 rank FROM kb_documents d WHERE " + " AND ".join(clauses)
                if caps:
                    sql += " AND d.cap IN (" + ",".join("?" for _ in caps) + ")"
                    params.extend(sorted(caps))
                if tag:
                    sql += " AND lower(d.tags) LIKE ?"
                    params.append("%" + tag.lower() + "%")
                sql += " ORDER BY d.layer DESC, d.relpath LIMIT ?"
                params.append(max(1, limit))
                rows = list(con.execute(sql, params))
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
