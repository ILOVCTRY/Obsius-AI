"""战役记忆全局库（⑥ 借鉴 dsh campaign-memory，2026-09-19）。

与项目黑板无关的**全局**库：workspaces/campaign.db（黑板是 per-project 库，
跨项目记忆必须另建全局库，仿 core/intel 先例）。表 campaign_memory 不绑
project_id——project_id 只作溯源（召回时跨项目可见）。流量极低（任务 done
写一条 / 编排 tick 召回一次），单连接 + 写锁，不照抄 Blackboard 线程局部连接。

打分（recall）：关键词命中（title/content 子串）+ track/capability 匹配加成
+ 热度（usage_count）× 时间衰减（线性 30 天）——简版不引 FTS5，数据量小全量
内存打分足够；召回即计 usage（下次更靠前，经典热度飞轮）。
"""

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.blackboard.store import new_id  # 复用 <前缀>-<12hex> 约定

SCHEMA_VERSION = 1

MAX_CONTENT_LEN = 500      # 写入截断：打法摘要 ≤500 字
RECALL_SNIPPET_LEN = 300   # 召回注入截断：单条 ≤300 字
_DECAY_DAYS = 30           # 时间衰减窗口：30 天线性衰减到 0

_DDL = """
CREATE TABLE IF NOT EXISTS campaign_memory (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL DEFAULT '',  -- 溯源（不隔离，召回跨项目）
    track        TEXT NOT NULL DEFAULT '',
    capability   TEXT NOT NULL DEFAULT '',
    task_type    TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,             -- 打法摘要（写入截 500 字）
    tags         TEXT NOT NULL DEFAULT '[]',
    target_key   TEXT NOT NULL DEFAULT '',  -- 目标指纹（scope/conflict_keys 摘）
    usage_count  INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaign_track ON campaign_memory(track, capability);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _keywords(query: str) -> list[str]:
    """召回分词：≥2 位的字母数字下划线串 + 连续中文段（中文整段 LIKE 子串，
    简版不做 2-gram——campaign 文本是自己写的摘要，整段命中已够用）。"""
    return [w for w in re.findall(r"[A-Za-z0-9_]{2,}|[一-鿿]{2,}", query or "")]


class CampaignMemory:
    def __init__(self, path: str | Path = "workspaces/campaign.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 写入 ----

    def add(self, project_id: str, track: str, capability: str, task_type: str,
            title: str, content: str, target_key: str = "") -> dict:
        """沉淀一条打法（done 钩子调用）；content 截 500 字。"""
        row = {
            "id": new_id("camp"),
            "project_id": project_id,
            "track": (track or "")[:40],
            "capability": (capability or "")[:120],
            "task_type": (task_type or "")[:40],
            "title": (title or "").strip()[:200] or "(无标题)",
            "content": (content or "").strip()[:MAX_CONTENT_LEN],
            "target_key": (target_key or "")[:200],
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO campaign_memory(id,project_id,track,capability,"
                "task_type,title,content,target_key,created_at) "
                "VALUES(:id,:project_id,:track,:capability,:task_type,"
                ":title,:content,:target_key,:created_at)",
                {**row, "created_at": now()})
            self._conn.commit()
        return row

    # ---- 召回 ----

    def recall(self, query: str, track: str = "", capability: str = "",
               limit: int = 5, snippet_len: int = RECALL_SNIPPET_LEN) -> list[dict]:
        """按「关键词命中 + 轨/能力匹配 + 热度×时间衰减」打分取 top-N。

        返回条目带 score（诊断用）与截断后的 content（snippet_len）；命中即计
        usage（usage_count+1、last_used_at 刷新——下次同类任务更靠前）。"""
        kws = [k.lower() for k in _keywords(query)]
        rows = self._all()
        scored: list[tuple[float, dict]] = []
        for r in rows:
            hay = (r["title"] + "\n" + r["content"]).lower()
            score = 2.0 * sum(1 for k in kws if k in hay)
            if track and r["track"] == track:
                score += 1.5
            if capability and capability in (r["capability"] or ""):
                score += 1.0
            # 时间衰减：30 天线性衰减到 0；热度：封顶 10 次折半计
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(r["created_at"])).days
            except ValueError:
                age = _DECAY_DAYS
            decay = max(0.0, 1.0 - age / _DECAY_DAYS)
            score += decay * min(r["usage_count"], 10) * 0.5
            if score > 0:
                scored.append((score, r))
        # 分数升序排后整体反转：分数降序、同分新者优先（一次排序不破坏分数序）
        scored.sort(key=lambda t: (t[0], t[1]["created_at"]))
        scored.reverse()
        hits = []
        for score, r in scored[:limit]:
            d = dict(r)
            d["tags"] = _loads_tags(d["tags"])
            d["content"] = d["content"][:snippet_len]
            d["score"] = round(score, 2)
            hits.append(d)
        if hits:
            self.bump([h["id"] for h in hits])
        return hits

    def list_recent(self, limit: int = 50) -> list[dict]:
        """API 列表口径：按时间倒序全量浏览（无打分）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM campaign_memory ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = _loads_tags(d["tags"])
            out.append(d)
        return out

    def bump(self, ids: list[str]) -> None:
        """召回命中计数（热度飞轮）。"""
        with self._lock:
            for i in ids:
                self._conn.execute(
                    "UPDATE campaign_memory SET usage_count=usage_count+1, "
                    "last_used_at=? WHERE id=?", (now(), i))
            self._conn.commit()

    def _all(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM campaign_memory").fetchall()


def _loads_tags(raw: str) -> list[str]:
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
