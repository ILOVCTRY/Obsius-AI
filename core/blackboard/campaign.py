"""战役记忆全局库（⑥ 借鉴 dsh campaign-memory，2026-09-19）。

与项目黑板无关的**全局**库：data/campaign.db（黑板是 per-project 库，
跨项目记忆必须另建全局库，仿 core/intel 先例）。表 campaign_memory 不绑
project_id——project_id 只作溯源（召回时跨项目可见）。流量极低（任务 done
写一条 / 编排 tick 召回一次），单连接 + 写锁，不照抄 Blackboard 线程局部连接。

打分（recall，M4 升级 2026-09-23）：查询切分复用 kbindex 的 2-gram+停用词
切段（跨写法命中：「办公自动化系统」×「OA 系统」靠 系统 等共段命中），
字段加权 title 命中 2.0 > 正文独有 1.0；轨/能力匹配加成；热度 log1p ×
时间衰减（线性 30 天）——简版不引 FTS5，数据量小全量内存打分足够；召回即
计 usage（下次更靠前，经典热度飞轮）。打分链防回归：
tests/fixtures/campaign-golden.yaml 黄金集进 pytest。
"""

import json
import logging
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.blackboard.store import new_id  # 复用 <前缀>-<12hex> 约定

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# 全局库落点（workspace-hygiene D1，2026-09-23）：data/ 归口全局运行时数据，
# 不再寄生 workspaces/ 根（其契约=只允许项目目录 + .trash + CLAUDE.md）。
# 相对路径相对进程 cwd（serve.py 以项目根启动，与旧默认同语义）。
DEFAULT_DB_PATH = Path("data") / "campaign.db"
_LEGACY_DB_PATH = Path("workspaces") / "campaign.db"
_DB_SIDECARS = ("-wal", "-shm")

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


def _segments(query: str) -> list[str]:
    """召回切分（M4）：复用 kbindex 的 2-gram+停用词切段（就地导入防环，
    检索链切分保持单一来源）——中文不再整段 LIKE，「办公自动化系统」↔
    「OA 系统」靠 系统 等共段命中；ASCII 词 ≥3 字符整词保留（oa 之类 2 字
    词的子串噪声大，随 kbindex 口径丢弃）。"""
    from core.skills.kbindex import _query_segments  # 就地导入防环
    return _query_segments(query)


def _migrate_legacy_db(new_path: Path) -> Path:
    """老默认位置 workspaces/campaign.db → data/ 惰性迁移；返回实际生效路径。

    仅默认路径构造时触发（显式传 path 的一律不迁移，测试 fixture 天然豁免）。
    老主库在场且新位置不存在才动。**主库 rename 先行**：失败（旧进程占用等）
    整体放弃本次迁移继续用老位置，下次启动重试（shutdown 僵尸场景天然自愈）；
    主库成功后 -wal/-shm 尽力而为——此时旧进程已不可能持句柄，失败也绝不回退
    老路径（老位置已无主库，回去会 sqlite 新建空库丢数据），-wal 缺失最多丢
    未 checkpoint 尾巴，宁可少不可空。
    """
    if not _LEGACY_DB_PATH.exists() or new_path.exists():
        return new_path
    moved_main = False
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        _LEGACY_DB_PATH.rename(new_path)
        moved_main = True
        for suffix in _DB_SIDECARS:
            src = Path(str(_LEGACY_DB_PATH) + suffix)
            if src.exists():
                src.rename(Path(str(new_path) + suffix))
        log.info("campaign.db 已从 workspaces/ 迁移到 %s", new_path)
        return new_path
    except OSError as e:
        log.warning("campaign.db 迁移失败（可能被旧进程占用），本次继续用 %s：%s",
                    _LEGACY_DB_PATH if not moved_main else new_path, e)
        return new_path if moved_main else _LEGACY_DB_PATH


class CampaignMemory:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = _migrate_legacy_db(Path(path)) if Path(path) == DEFAULT_DB_PATH else Path(path)
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
            title: str, content: str, target_key: str = "",
            tags: list[str] | None = None) -> dict:
        """沉淀一条打法（done 钩子调用）；content 截 500 字。tags 标注条目性质
        （M6 F1：死路记账条目 tags=["dead_end"]——召回注入侧按 tag 分组呈现，
        表结构零变更）。"""
        row = {
            "id": new_id("camp"),
            "project_id": project_id,
            "track": (track or "")[:40],
            "capability": (capability or "")[:120],
            "task_type": (task_type or "")[:40],
            "title": (title or "").strip()[:200] or "(无标题)",
            "content": (content or "").strip()[:MAX_CONTENT_LEN],
            "target_key": (target_key or "")[:200],
            "tags": json.dumps(tags or [], ensure_ascii=False),
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO campaign_memory(id,project_id,track,capability,"
                "task_type,title,content,target_key,tags,created_at) "
                "VALUES(:id,:project_id,:track,:capability,:task_type,"
                ":title,:content,:target_key,:tags,:created_at)",
                {**row, "created_at": now()})
            self._conn.commit()
        return row

    # ---- 召回 ----

    def recall(self, query: str, track: str = "", capability: str = "",
               limit: int = 5, snippet_len: int = RECALL_SNIPPET_LEN) -> list[dict]:
        """按「切段命中（title 2.0 / 正文独有 1.0 字段加权）+ 轨/能力匹配 +
        热度 log1p×时间衰减」打分取 top-N。

        返回条目带 score（诊断用）与截断后的 content（snippet_len）；命中即计
        usage（usage_count+1、last_used_at 刷新——下次同类任务更靠前）。"""
        segs = list(dict.fromkeys(_segments(query)))
        rows = self._all()
        scored: list[tuple[float, dict]] = []
        for r in rows:
            title = (r["title"] or "").lower()
            body = (r["content"] or "").lower()
            # M4 字段加权：title 命中 2.0 > 正文独有 1.0（title 是人工浓缩的
            # 打法名信号密；title/body 双现的段按 title 计不重复加分）
            t_hits = sum(1 for s in segs if s in title)
            c_hits = sum(1 for s in segs if s in body) - t_hits
            score = 2.0 * t_hits + 1.0 * c_hits
            if track and r["track"] == track:
                score += 1.5
            if capability and capability in (r["capability"] or ""):
                score += 1.0
            # 时间衰减：30 天线性衰减到 0；热度 log1p 压缩（experience-sedimentation
            # M2 马太修正：原 min(usage,10)*0.5 让高频条目恒霸榜——log 增长自然趋缓，
            # 新验证打法有机会上位；热 10 次≈5.9 vs 冷 1 次≈1.1，差距合理）
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(r["created_at"])).days
            except ValueError:
                age = _DECAY_DAYS
            decay = max(0.0, 1.0 - age / _DECAY_DAYS)
            score += decay * math.log1p(r["usage_count"]) * 1.5
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
