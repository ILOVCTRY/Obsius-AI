"""情报全局存储（E9，DESIGN.md §16）：config/intel/intel.db（SQLite WAL）。

与项目黑板无关的**全局** DB（新模式：现全局配置仅 config/*.json）。
文章池 + 简报归档两张表；feeds.json / profile.json 由 config.py 管理。
流量极低（人工触发抓取/翻页），单连接 + 写锁即可，不照抄 Blackboard 的线程局部连接。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.blackboard.store import new_id  # 复用 <前缀>-<12hex> 约定

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source TEXT NOT NULL,          -- 源名（feed 名 / nvd / kev / ghsa）
    kind TEXT NOT NULL,            -- cve | article
    summary TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    direction TEXT NOT NULL DEFAULT '',    -- 打分命中的主方向
    is_priority INTEGER NOT NULL DEFAULT 0,-- KEV / 有公开 POC 优先标
    score_detail TEXT NOT NULL DEFAULT '', -- JSON（llm/规则、明细）
    brief_date TEXT,               -- 已并入哪份简报
    read INTEGER NOT NULL DEFAULT 0,
    starred INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_kind ON articles(kind, score DESC);
CREATE TABLE IF NOT EXISTS briefs (
    date TEXT PRIMARY KEY,         -- YYYY-MM-DD（UTC）
    content TEXT NOT NULL,         -- markdown 全文
    stats TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IntelStore:
    def __init__(self, intel_dir: str | Path = "config/intel"):
        self.dir = Path(intel_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "intel.db"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_DDL)
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 文章池 ----

    def upsert_articles(self, items: list[dict]) -> int:
        """按 URL 幂等入池（已存在则跳过，不动打分/已读状态）。返回新入池条数。"""
        n = 0
        with self._lock:
            cur = self._conn.cursor()
            for it in items:
                r = cur.execute(
                    "INSERT OR IGNORE INTO articles(id,url,title,source,kind,summary,"
                    "published_at,fetched_at,is_priority) VALUES(?,?,?,?,?,?,?,?,?)",
                    (new_id("art"), it["url"], it["title"], it["source"], it["kind"],
                     it.get("summary", ""), it.get("published_at", ""), now(),
                     1 if it.get("is_priority") else 0))
                n += r.rowcount
            self._conn.commit()
        return n

    def unscored(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM articles WHERE score=0 AND score_detail='' "
                "ORDER BY fetched_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def apply_scores(self, scores: list[dict]) -> None:
        """scores: [{id, score, direction, is_priority?, score_detail}]"""
        with self._lock:
            cur = self._conn.cursor()
            for s in scores:
                cur.execute(
                    "UPDATE articles SET score=?, direction=?, score_detail=? "
                    "WHERE id=?",
                    (s["score"], s.get("direction", ""),
                     json.dumps(s.get("score_detail", {}), ensure_ascii=False), s["id"]))
                if s.get("is_priority") is not None:
                    cur.execute("UPDATE articles SET is_priority=? WHERE id=?",
                                (1 if s["is_priority"] else 0, s["id"]))
            self._conn.commit()

    def list_articles(self, *, kind: str | None = None, unread_only: bool = False,
                      starred_only: bool = False, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM articles WHERE 1=1"
        args: list = []
        if kind:
            sql += " AND kind=?"; args.append(kind)
        if unread_only:
            sql += " AND read=0"
        if starred_only:
            sql += " AND starred=1"
        sql += " ORDER BY is_priority DESC, score DESC, fetched_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def mark_article(self, article_id: str, *, read: bool | None = None,
                     starred: bool | None = None) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM articles WHERE id=?",
                                     (article_id,)).fetchone()
            if row is None:
                return None
            if read is not None:
                self._conn.execute("UPDATE articles SET read=? WHERE id=?",
                                   (1 if read else 0, article_id))
            if starred is not None:
                self._conn.execute("UPDATE articles SET starred=? WHERE id=?",
                                   (1 if starred else 0, article_id))
            self._conn.commit()
            return dict(self._conn.execute("SELECT * FROM articles WHERE id=?",
                                           (article_id,)).fetchone())

    # ---- 简报 ----

    def save_brief(self, date: str, content: str, stats: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO briefs(date,content,stats,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET content=excluded.content, "
                "stats=excluded.stats", (date, content,
                                         json.dumps(stats, ensure_ascii=False), now()))
            self._conn.commit()

    def get_brief(self, date: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM briefs WHERE date=?",
                                     (date,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["stats"] = json.loads(d.get("stats") or "{}")
        return d

    def list_briefs(self) -> list[dict]:
        """归档列表（不含全文）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT date, stats, created_at FROM briefs ORDER BY date DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stats"] = json.loads(d.get("stats") or "{}")
            out.append(d)
        return out

    def counts(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
            unread = self._conn.execute(
                "SELECT COUNT(*) c FROM articles WHERE read=0").fetchone()["c"]
            starred = self._conn.execute(
                "SELECT COUNT(*) c FROM articles WHERE starred=1").fetchone()["c"]
        return {"articles": total, "unread": unread, "starred": starred}
