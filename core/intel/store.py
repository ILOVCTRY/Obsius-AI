"""情报全局存储（E9/E10，DESIGN.md §16）：config/intel/intel.db（SQLite WAL）。

与项目黑板无关的**全局** DB（新模式：现全局配置仅 config/*.json）。
文章池 + 简报归档 + vault 笔记元数据索引 + 学习计划四张表；feeds.json / profile.json 由 config.py 管理。
流量极低（人工触发抓取/翻页），单连接 + 写锁即可，不照抄 Blackboard 的线程局部连接。
"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.blackboard.store import new_id  # 复用 <前缀>-<12hex> 约定

SCHEMA_VERSION = 4

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
    has_poc INTEGER NOT NULL DEFAULT 0,    -- 启发式判定有公开 POC / KEV 在野利用（证据合并后）
    poc_url TEXT NOT NULL DEFAULT '',      -- 最优 PoC 链接（KEV 无补充证据时为 ''）
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
CREATE TABLE IF NOT EXISTS vault_notes (          -- E10：vault 元数据索引（只读，全量重建语义）
    path TEXT PRIMARY KEY,         -- vault 根相对 POSIX 路径
    title TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '', -- JSON 数组
    category TEXT NOT NULL DEFAULT '', -- F5：frontmatter category（归一化小写；方向判定优先命中）
    mtime TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL DEFAULT '', -- 正文仅本地搜索用，绝不进 LLM 入参（隐私红线）
    indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_plans (       -- E10：周学习计划（周一起始，同周覆盖）
    week TEXT PRIMARY KEY,         -- YYYY-MM-DD（ISO 周起始日）
    content TEXT NOT NULL,         -- markdown 全文
    inputs TEXT NOT NULL DEFAULT '{}', -- 输入摘要统计（不含笔记正文）
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """按 PRAGMA 实测列集幂等迁移（不按版本号分支——9515da9 教训：并行编辑掉落
    ALTER + 启动无条件升版本号会掩盖缺列）。版本号 upsert 由调用方在迁移后执行。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    if "has_poc" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN has_poc INTEGER NOT NULL DEFAULT 0")
    if "poc_url" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN poc_url TEXT NOT NULL DEFAULT ''")
    vcols = {r[1] for r in conn.execute("PRAGMA table_info(vault_notes)")}
    if vcols and "category" not in vcols:  # F5：frontmatter category 入索引
        conn.execute("ALTER TABLE vault_notes ADD COLUMN category TEXT NOT NULL DEFAULT ''")


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
        _migrate(self._conn)
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 文章池 ----

    def upsert_articles(self, items: list[dict]) -> int:
        """按 URL 入池；已存在只升不降地补 has_poc/poc_url（NVD 后补 Exploit 标签的
        窗口差回填），不动打分/已读状态。返回新入池条数。"""
        n = 0
        with self._lock:
            cur = self._conn.cursor()
            for it in items:
                exists = cur.execute("SELECT 1 FROM articles WHERE url=?",
                                     (it["url"],)).fetchone() is not None
                if not exists:
                    n += 1
                cur.execute(
                    "INSERT INTO articles(id,url,title,source,kind,summary,"
                    "published_at,fetched_at,is_priority,has_poc,poc_url) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(url) DO UPDATE SET "
                    "has_poc=MAX(articles.has_poc, excluded.has_poc), "
                    "poc_url=CASE WHEN excluded.has_poc=1 AND articles.has_poc=0 "
                    "THEN excluded.poc_url ELSE articles.poc_url END",
                    (new_id("art"), it["url"], it["title"], it["source"], it["kind"],
                     it.get("summary", ""), it.get("published_at", ""), now(),
                     1 if it.get("is_priority") else 0,
                     1 if it.get("has_poc") else 0, it.get("poc_url", "")))
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

    # ---- vault 笔记元数据索引（E10，只读；正文仅本地搜索用） ----

    def replace_notes(self, notes: list[dict]) -> int:
        """全量重建索引（事务内清表重灌）。返回入索引篇数。"""
        with self._lock:
            self._conn.execute("DELETE FROM vault_notes")
            for n in notes:
                self._conn.execute(
                    "INSERT OR REPLACE INTO vault_notes"
                    "(path,title,tags,category,mtime,size,content,indexed_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (n["path"], n.get("title", ""),
                     json.dumps(n.get("tags", []), ensure_ascii=False),
                     n.get("category", ""),
                     n.get("mtime", ""), n.get("size", 0),
                     n.get("content", ""), now()))
            self._conn.commit()
        return len(notes)

    def list_notes(self, limit: int = 2000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,title,tags,category,mtime,size,indexed_at FROM vault_notes "
                "ORDER BY path LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.get("tags") or "[]")
            out.append(d)
        return out

    def search_notes(self, q: str, limit: int = 50) -> list[dict]:
        """本地全文搜索（title/tags/content LIKE）。返回元数据 + 命中 snippet，不返回全文。"""
        q = q.strip()
        if not q:
            return []
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT path,title,tags,mtime,size,content FROM vault_notes "
                "WHERE title LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' "
                "OR content LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
                (like, like, like, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.get("tags") or "[]")
            content = d.pop("content", "")
            pos = content.lower().find(q.lower())
            if pos < 0:  # 命中在 title/tags 而非正文
                d["snippet"] = content[:120]
            else:
                s = max(0, pos - 80)
                d["snippet"] = ("…" if s > 0 else "") + content[s:pos + len(q) + 80] + "…"
            out.append(d)
        return out

    def note_stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM vault_notes").fetchone()["c"]
            row = self._conn.execute(
                "SELECT MAX(indexed_at) m FROM vault_notes").fetchone()
        return {"notes": total, "last_indexed": row["m"] or ""}

    def platform_direction_counts(self) -> dict:
        """文章池按方向的已读/收藏计数（E10 学习档案平台侧来源）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT direction, COUNT(*) total, SUM(read) reads, SUM(starred) stars "
                "FROM articles WHERE direction!='' GROUP BY direction").fetchall()
        return {r["direction"]: {"notes": 0, "total": r["total"],
                                 "read": r["reads"] or 0, "starred": r["stars"] or 0}
                for r in rows}

    # ---- 周学习计划（E10） ----

    def save_plan(self, week: str, content: str, inputs: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO learning_plans(week,content,inputs,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(week) DO UPDATE SET content=excluded.content, "
                "inputs=excluded.inputs", (week, content,
                                           json.dumps(inputs, ensure_ascii=False), now()))
            self._conn.commit()

    def get_plan(self, week: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM learning_plans WHERE week=?",
                                     (week,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["inputs"] = json.loads(d.get("inputs") or "{}")
        return d

    def latest_plan(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT week FROM learning_plans ORDER BY week DESC LIMIT 1").fetchone()
        return self.get_plan(row["week"]) if row else None

    def list_plans(self) -> list[dict]:
        """归档列表（不含全文）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT week, inputs, created_at FROM learning_plans "
                "ORDER BY week DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["inputs"] = json.loads(d.get("inputs") or "{}")
            out.append(d)
        return out
