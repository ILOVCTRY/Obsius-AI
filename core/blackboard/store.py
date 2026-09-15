"""黑板存储层（DESIGN.md §5）。

单一写入口原则：所有写操作走本模块（未来经 core API 暴露），
禁止旁路直写 SQLite。读操作全局开放（共享情报）。

返回值约定：统一 dict（sqlite3.Row 转换），ID 形如 `find-xxxxxxxx`。
写路径默认触发事件广播（kind 见各方法）。
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from core.blackboard.events import EventBus
from core.blackboard.schema import init_schema

NODE_TABLES = {"finding": "findings", "func_kb": "func_kb", "artifact": "artifacts"}

# 哨兵：区分"参数不传"与"传空值"（risk_tags=[] 语义=清空标签）
UNSET = object()

# findings 状态机白名单（研究轨 verified = 人工确认或带日志产物的动态验证）
FINDING_STATUSES = ("unverified", "verified", "false-positive")

# 攻击链状态机：假说 → 验证 → 已利用
CHAIN_STATUSES = ("hypothesis", "validated", "exploited")

# 严重度排序（severity 就高不就低）
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# 重复发现合并时做"按内容去重追加"的 evidence 列表键（§5.3 证据并集）
EVIDENCE_LIST_UNION_KEYS = ("pocs", "requests", "relates_to", "screenshots")


def _evidence_item_fp(item: Any) -> str:
    """列表证据项的去重指纹。relates_to 项按 finding_id+note（同一边不重复堆），
    其余 dict 按稳定 JSON，原子值按 JSON。"""
    if isinstance(item, dict):
        if "finding_id" in item:
            return f"rel:{item['finding_id']}|{item.get('note', '')}"
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def merge_finding_evidence(old: dict, new: dict) -> dict:
    """重复发现的证据并集语义（§5.3；A2 修复旧实现"新 evidence 整体丢弃"）：

    - 列表键（pocs/requests/relates_to/screenshots）：按内容去重追加，多来源并存；
    - notes 字符串：双方非空时分段追加（不覆盖前人笔记）；
    - 其余键：已有真值保留，空缺由新证据补入（不覆盖前人结论）。
    """
    merged = dict(old)
    for k, v in new.items():
        if k in EVIDENCE_LIST_UNION_KEYS and isinstance(v, list):
            base = merged.get(k)
            if not isinstance(base, list):
                base = []
            seen = {_evidence_item_fp(x) for x in base}
            for item in v:
                fp = _evidence_item_fp(item)
                if fp not in seen:
                    seen.add(fp)
                    base.append(item)
            merged[k] = base
        elif k == "notes" and isinstance(v, str) and v.strip():
            cur = merged.get(k)
            merged[k] = f"{cur.rstrip()}\n\n{v.strip()}" if isinstance(cur, str) and cur.strip() \
                else v.strip()
        elif merged.get(k) in (None, "", [], {}):
            merged[k] = v
    return merged


def validate_relates_to(conn: sqlite3.Connection, project_id: str,
                        evidence: dict | None) -> None:
    """校验 evidence.relates_to 强关系（E0，防幻觉，严格不静默丢）：

    - 形态必须是 [{finding_id: str, note?: str}, ...]；
    - 每个被引用 finding 必须存在且属于同一项目（无自引——新发现尚未入库）。

    非法形态/悬空/跨项目一律 ValueError（API → 422）。须在写事务内调用，
    读到的归属与后续写入同一事务视图。
    """
    rels = (evidence or {}).get("relates_to")
    if rels is None:
        return
    if not isinstance(rels, list):
        raise ValueError("evidence.relates_to 必须是 [{finding_id, note?}] 列表")
    for item in rels:
        if not isinstance(item, dict):
            raise ValueError("evidence.relates_to 每项必须是对象 {finding_id, note?}")
        fid = item.get("finding_id")
        if not isinstance(fid, str) or not fid:
            raise ValueError("evidence.relates_to 每项必须含非空字符串 finding_id")
        if not isinstance(item.get("note", ""), str):
            raise ValueError("evidence.relates_to.note 必须是字符串（关联理由）")
        owned = conn.execute(
            "SELECT 1 FROM findings WHERE id=? AND project_id=?", (fid, project_id),
        ).fetchone()
        if not owned:
            raise ValueError(
                f"relates_to 引用的发现不存在或不属于本项目: {fid}"
                "（先登记被引用发现，再建立强关系）")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {k: row[k] for k in row.keys()}


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class BlackboardClosedError(RuntimeError):
    """黑板已被 close_all() 关闭（项目删除中）：拒绝任何惰性重连。

    没有它，仍持有 Blackboard 引用的线程（WS 1s tick / 在飞轮询）会经
    conn property 立刻重开 sqlite 连接，在 Windows 上重新锁死 db 文件，
    导致删除目录的 rename 必败。要重开须由 ProjectStore 新建 Blackboard 实例。"""


class Blackboard:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()  # 每线程独立连接（sqlite3 连接非线程安全）
        self._conns: set[sqlite3.Connection] = set()  # 全量登记（close_all 用）
        self._write_lock = threading.Lock()  # 进程内写路径互斥（配合 BEGIN IMMEDIATE）
        self._closed = False  # close_all 后置位；之后 .conn 拒绝重连（删项目防复活）
        # 闸门锁：把"检查 _closed→新建连接→登记 _conns"与 close_all 互斥。
        # 否则线程通过 _closed 检查后、sqlite3.connect 前被切走，close_all 先执行，
        # 恢复后会建出未登记的孤儿连接（重新锁死文件）或在目录移走后 OperationalError。
        self._gate_lock = threading.Lock()
        main = self._new_conn()
        main.execute("PRAGMA journal_mode=WAL")  # 库级持久设置，建库时设一次即可
        init_schema(main)
        main.close()
        self.bus = EventBus()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """线程局部连接：并发读互不干扰（WebUI/多会话经线程池并发访问），
        写竞争由 WAL + busy_timeout + _tx 的 BEGIN IMMEDIATE 兜底。"""
        if self._closed:
            raise BlackboardClosedError(
                f"黑板已关闭（项目删除中），拒绝重连: {self.db_path}")
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        # 首建路径加锁双重检查：要么连接在 close_all 之前建好并会被它关到，
        # 要么 close_all 先到，这里抛 BlackboardClosedError——无孤儿连接窗口。
        with self._gate_lock:
            if self._closed:
                raise BlackboardClosedError(
                    f"黑板已关闭（项目删除中），拒绝重连: {self.db_path}")
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._new_conn()
                self._local.conn = conn
                self._conns.add(conn)
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
            self._conns.discard(conn)

    def close_all(self) -> None:
        """关闭所有线程的连接并置关闭闸门（Windows 删除项目目录前必须调；仅限已确认
        无会话运行时）。闸门置位后 .conn 拒绝惰性重连——否则仍持有本对象引用的
        WS tick/轮询线程会在毫秒级内重开连接锁死文件（删除竞态的根源）。
        需要重新访问须由 ProjectStore.open_project 新建实例，调用方还须在 API 层
        挡住删除窗口内的新请求（projects_closing 闸门）。"""
        with self._gate_lock:
            self._closed = True
            for c in list(self._conns):
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
            self._local = threading.local()

    @contextmanager
    def _tx(self):
        """单事务写。sqlite3 层面 BEGIN IMMEDIATE 串行化写入，天然多会话安全；
        进程内另加写锁防嵌套事务（同线程同连接）。"""
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    # ---------- 项目 / 会话 ----------

    def create_project(self, name: str, track: str,
                       capabilities: list[str] | None = None,
                       config: dict | None = None,
                       project_id: str | None = None) -> dict:
        """project_id 缺省自生成；经 core/projects.py 创建时传入同 id（project.json 与库一致）。

        v2：track=场景轨（单选），capabilities=能力包（多选）。旧 domain 列同步写
        track 值，旧代码读 domain 不炸；旧库（domain=pentest/ctf）由 get_project
        经 LEGACY_DOMAIN_MAP 读兼容。"""
        from core.skills.taxonomy import LEGACY_DOMAIN_MAP

        caps = sorted(capabilities or [])
        # 旧域名字符串误传进来时透明映射，写新值不写旧值
        if track in LEGACY_DOMAIN_MAP:
            track, caps_from_legacy = LEGACY_DOMAIN_MAP[track]
            caps = caps or caps_from_legacy
        proj = {
            "id": project_id or new_id("proj"),
            "name": name,
            "domain": track,  # 旧列：写轨名兜底
            "track": track,
            "capabilities": json.dumps(caps, ensure_ascii=False),
            "config": json.dumps(config or {}, ensure_ascii=False),
            "created_at": now(),
        }
        with self._tx():
            self.conn.execute(
                "INSERT INTO projects(id,name,domain,track,capabilities,config,created_at)"
                " VALUES(:id,:name,:domain,:track,:capabilities,:config,:created_at)",
                proj,
            )
        proj["capabilities"] = caps
        proj["config"] = config or {}
        return proj

    def get_project(self, project_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        d = _row_to_dict(row)
        if d:
            d["config"] = _loads(d["config"], {})
            d["capabilities"] = _loads(d.get("capabilities", "[]"), [])
            if not d.get("track"):  # v1 旧库：domain 映射
                from core.skills.taxonomy import LEGACY_DOMAIN_MAP
                mapped = LEGACY_DOMAIN_MAP.get(d.get("domain", ""))
                if mapped:
                    d["track"], d["capabilities"] = mapped[0], list(mapped[1])
                else:
                    d["track"] = d.get("domain") or "ctf"
            d["domain"] = d["track"]  # 兼容旧读取方
        return d

    def update_project_config(self, project_id: str, config: dict) -> dict:
        """整体替换 projects 行 config（JSON）；project.json 由 ProjectStore 双写。
        不存在返回 None。配置合法性（autonomy 归一化）由 core.autonomy 负责。"""
        with self._tx():
            cur = self.conn.execute(
                "UPDATE projects SET config=:config WHERE id=:id",
                {"id": project_id,
                 "config": json.dumps(config or {}, ensure_ascii=False)})
            if cur.rowcount == 0:
                raise LookupError(f"项目不存在: {project_id}")
        return self.get_project(project_id)  # type: ignore[return-value]

    # ---------- 编排器状态 / 用量记账（v4，DESIGN §6.8） ----------

    def usage_state_get(self, project_id: str) -> dict:
        """取用量计数行；从未记账则零值（不建行）。"""
        row = self.conn.execute(
            "SELECT * FROM orchestrator_state WHERE project_id=?",
            (project_id,)).fetchone()
        return _row_to_dict(row) or {
            "tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0,
            "tokens_cache_creation": 0, "llm_calls": 0,
            "tasks_published": 0, "budget_warned": 0,
        }

    def usage_add_llm(
        self, project_id: str, *, ti: int, to: int,
        cache_read: int, cache_creation: int, token_budget: int | None,
    ) -> dict:
        """原子累加一次 LLM 调用用量，并在同一事务内做 80% 软警告去重/复位。

        返回计数行 + budget_warn（本次是否新跨阈值，调用方据此发事件）。
        budget_warned 自愈：预算调大后用量回落到阈值以下即复位，之后可再报。"""
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO orchestrator_state(project_id, updated_at)"
                " VALUES(?, '')", (project_id,))
            self.conn.execute(
                "UPDATE orchestrator_state SET"
                " tokens_in=tokens_in+:ti, tokens_out=tokens_out+:to,"
                " tokens_cache_read=tokens_cache_read+:cr,"
                " tokens_cache_creation=tokens_cache_creation+:cc,"
                " llm_calls=llm_calls+1, updated_at=:now WHERE project_id=:pid",
                {"pid": project_id, "ti": ti, "to": to, "cr": cache_read,
                 "cc": cache_creation, "now": now()})
            row = _row_to_dict(self.conn.execute(
                "SELECT * FROM orchestrator_state WHERE project_id=?",
                (project_id,)).fetchone())
            used = (row["tokens_in"] + row["tokens_out"]
                    + row["tokens_cache_read"] + row["tokens_cache_creation"])
            warn = False
            if token_budget:
                crossed = used * 100 >= 80 * int(token_budget)
                if crossed and not row["budget_warned"]:
                    warn = True
                if crossed != bool(row["budget_warned"]):
                    self.conn.execute(
                        "UPDATE orchestrator_state SET budget_warned=:w WHERE project_id=:pid",
                        {"w": 1 if crossed else 0, "pid": project_id})
                    row["budget_warned"] = 1 if crossed else 0
        row["budget_warn"] = warn
        return row

    def usage_inc_tasks(self, project_id: str) -> int:
        """orchestrator 自主发布任务计数 +1（task_budget 口径；人手发布不计）。"""
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO orchestrator_state(project_id, updated_at)"
                " VALUES(?, '')", (project_id,))
            self.conn.execute(
                "UPDATE orchestrator_state SET tasks_published=tasks_published+1,"
                " updated_at=:now WHERE project_id=:pid",
                {"pid": project_id, "now": now()})
            row = self.conn.execute(
                "SELECT tasks_published FROM orchestrator_state WHERE project_id=?",
                (project_id,)).fetchone()
        return int(row["tasks_published"])

    def register_session(
        self, project_id: str, name: str, role: str = "_generalist", meta: dict | None = None
    ) -> dict:
        sess = {
            "id": new_id("sess"),
            "project_id": project_id,
            "name": name,
            "role": role,
            "status": "idle",
            "meta": json.dumps(meta or {}, ensure_ascii=False),
            "created_at": now(),
        }
        with self._tx():
            self.conn.execute(
                "INSERT INTO sessions(id,project_id,name,role,status,meta,created_at)"
                " VALUES(:id,:project_id,:name,:role,:status,:meta,:created_at)",
                sess,
            )
        return sess

    def list_sessions(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM session_inbox i"
            " WHERE i.to_session=s.id AND i.read_at IS NULL) AS unread"
            " FROM sessions s WHERE s.project_id=? ORDER BY s.created_at",
            (project_id,),
        ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    # ---------- 会话收件箱（§6.7 的 1.5/1.6：知会私信，与审批收件箱分设） ----------

    def inbox_post(
        self, project_id: str, to_session: str, kind: str, ref_id: str,
        payload: dict | None = None,
    ) -> bool:
        """投递一条私信。未读去重：同 (to_session, kind, ref_id) 已有未读行则不重发
        （部分唯一索引兜底）；读后可再次投递。返回是否新写入。
        本切片只有系统（撤回路由）调用，没有 Agent 自由消息工具。"""
        with self._tx():
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO session_inbox"
                "(id,project_id,to_session,kind,ref_id,payload,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (new_id("msg"), project_id, to_session, kind, ref_id,
                 json.dumps(payload or {}, ensure_ascii=False), now()),
            )
            return cur.rowcount > 0

    def inbox_list(self, project_id: str, session_id: str,
                   unread_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM session_inbox WHERE project_id=? AND to_session=?"
        params: list[Any] = [project_id, session_id]
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY created_at"
        out = []
        for r in self.conn.execute(sql, params):
            d = _row_to_dict(r) or {}
            d["payload"] = _loads(d.get("payload", "{}"), {})
            out.append(d)
        return out

    def inbox_drain(self, project_id: str, session_id: str) -> list[dict]:
        """取出全部未读并标记已读（worker 认领开场白/步边界注入用），原子完成。"""
        ts = now()
        with self._tx():
            rows = self.conn.execute(
                "SELECT * FROM session_inbox WHERE project_id=? AND to_session=?"
                " AND read_at IS NULL ORDER BY created_at",
                (project_id, session_id),
            ).fetchall()
            if rows:
                self.conn.execute(
                    "UPDATE session_inbox SET read_at=? WHERE project_id=? AND to_session=?"
                    " AND read_at IS NULL",
                    (ts, project_id, session_id),
                )
        out = []
        for r in rows:
            d = _row_to_dict(r) or {}
            d["payload"] = _loads(d.get("payload", "{}"), {})
            out.append(d)
        return out

    def inbox_mark_read(
        self, project_id: str, session_id: str, ids: list[str] | None = None,
    ) -> int:
        """标记已读（UI 红点消除）；ids=None 读全部，否则只标给定行（仍限本会话）。"""
        with self._tx():
            if ids is None:
                cur = self.conn.execute(
                    "UPDATE session_inbox SET read_at=? WHERE project_id=? AND to_session=?"
                    " AND read_at IS NULL",
                    (now(), project_id, session_id),
                )
            else:
                placeholders = ",".join("?" * len(ids))
                cur = self.conn.execute(
                    f"UPDATE session_inbox SET read_at=? WHERE project_id=? AND to_session=?"
                    f" AND read_at IS NULL AND id IN ({placeholders})",
                    (now(), project_id, session_id, *ids),
                )
            return cur.rowcount

    def close_session(self, session_id: str) -> dict:
        """关闭会话：status='closed' + session.closed 事件。
        关窗只是 UI 层隐藏页签 + 编排不再复用，黑板数据全部保留；不可逆需另开新窗。"""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {session_id}")
        if row["status"] == "closed":
            raise ValueError(f"会话已关闭: {session_id}")
        with self._tx():
            self.conn.execute(
                "UPDATE sessions SET status='closed' WHERE id=?", (session_id,))
        self.append_event(
            row["project_id"], "session.closed",
            {"session_id": session_id, "summary": "人类关闭会话窗口"},
            session_id=session_id, author="human")
        updated = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(updated) or {}

    def set_session_status(self, session_id: str, status: str) -> dict:
        """会话状态流转（§3 会话控制）。closed 只走 close_session，不在此开放。"""
        if status not in {"idle", "running", "paused", "blocked"}:
            raise ValueError(f"非法会话状态: {status}")
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {session_id}")
        if row["status"] == "closed":
            raise ValueError(f"会话已关闭: {session_id}")
        with self._tx():
            self.conn.execute(
                "UPDATE sessions SET status=? WHERE id=?", (status, session_id))
        updated = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(updated) or {}

    # ---------- 审批（§12 收件箱：创建/决策的唯一 core 入口） ----------

    def request_approval(
        self, project_id: str, action: dict, *, risk: str = "medium",
        requested_by: str, session_id: str | None = None,
    ) -> dict:
        """创建 pending 审批（net=real / 越界动作的请求入口）。返回含 id 的 dict。"""
        appr = {
            "id": new_id("appr"),
            "project_id": project_id,
            "session_id": session_id,
            "action": json.dumps(action, ensure_ascii=False),
            "risk": risk,
            "status": "pending",
            "requested_by": requested_by,
            "created_at": now(),
        }
        with self._tx():
            self.conn.execute(
                "INSERT INTO approvals(id,project_id,session_id,action,risk,status,"
                "requested_by,created_at)"
                " VALUES(:id,:project_id,:session_id,:action,:risk,:status,"
                ":requested_by,:created_at)",
                appr,
            )
        return appr

    def decide_approval(
        self, approval_id: str, decision: str, *,
        decided_by: str = "human", author: str = "human",
    ) -> dict:
        """审批决策（approved/rejected）：状态流转 + approval.{decision} 审计事件。
        decided_by/author 分离是为了演练脚本自批可标 demo-script(auto)，不冒充人类。"""
        if decision not in {"approved", "rejected"}:
            raise ValueError(f"非法决策: {decision}")
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise ValueError(f"审批不存在: {approval_id}")
        if row["status"] != "pending":
            raise ValueError(f"审批 {approval_id} 已处理（{row['status']}）")
        with self._tx():
            self.conn.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                (decision, decided_by, now(), approval_id))
        self.append_event(
            row["project_id"], f"approval.{decision}",
            {"approval_id": approval_id, "action": json.loads(row["action"]),
             "risk": row["risk"], "requested_by": row["requested_by"]},
            author=author)
        updated = self.conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return _row_to_dict(updated) or {}

    # ---------- 事件 ----------

    def append_event(
        self,
        project_id: str,
        kind: str,
        payload: dict | None = None,
        session_id: str | None = None,
        author: str = "system",
    ) -> int:
        """落库并广播。kind 约定：command / decision / finding.new / task.* / audit.deny ..."""
        created = now()
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO events(project_id,session_id,kind,payload,author,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    project_id,
                    session_id,
                    kind,
                    json.dumps(payload or {}, ensure_ascii=False),
                    author,
                    created,
                ),
            )
            event_id = cur.lastrowid
        event = {
            "id": event_id,
            "project_id": project_id,
            "session_id": session_id,
            "kind": kind,
            "payload": payload or {},
            "author": author,
            "created_at": created,
        }
        self.bus.publish(event)
        return event_id

    def recent_events(
        self, project_id: str, since_id: int = 0, limit: int = 200
    ) -> list[dict]:
        """增量拉取：id > since_id，升序。WS 断线重连回放也走这里。"""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
            (project_id, since_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r)
            assert d is not None
            d["payload"] = _loads(d["payload"], {})
            out.append(d)
        return out

    def latest_event_id(self, project_id: str) -> int:
        """项目事件末端 id（无事件返 0）。编排游标追赶 backlog 用，保证只前进不回放。"""
        row = self.conn.execute(
            "SELECT MAX(id) FROM events WHERE project_id=?", (project_id,)).fetchone()
        return int(row[0] or 0)

    # ---------- 资产 ----------

    def upsert_asset(
        self,
        project_id: str,
        type_: str,
        value: str,
        parent_id: str | None = None,
        meta: dict | None = None,
        author: str = "system",
    ) -> dict:
        """按 (project, type, value, parent) 去重，重复返回既有记录。"""
        with self._tx():
            row = self.conn.execute(
                "SELECT id FROM assets WHERE project_id=? AND type=? AND value IS ? AND parent_id IS ?",
                (project_id, type_, value, parent_id),
            ).fetchone()
            if row:
                return {"id": row["id"], "created": False}
            asset_id = new_id("asset")
            self.conn.execute(
                "INSERT INTO assets(id,project_id,type,value,parent_id,meta,author,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    asset_id,
                    project_id,
                    type_,
                    value,
                    parent_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                    author,
                    now(),
                ),
            )
        return {"id": asset_id, "created": True}

    def find_asset(self, project_id: str, type_: str, value: str) -> dict | None:
        """按 (project, type, value) 查既有资产（不看 parent）——重报合并路径用，
        避免去重键含 parent_id 导致同值资产插出多行。"""
        row = self.conn.execute(
            "SELECT * FROM assets WHERE project_id=? AND type=? AND value=?"
            " ORDER BY created_at LIMIT 1", (project_id, type_, value)).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row) or {}
        d["meta"] = _loads(d.get("meta"), {})
        return d

    def get_asset(self, asset_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        d = _row_to_dict(row)
        if d:
            d["meta"] = _loads(d["meta"], {})
        return d

    def set_asset_parent(self, asset_id: str, parent_id: str | None) -> dict:
        """改挂父资产（DESIGN.md §5.2 资产树）：parent_id=None = 摘挂为根行。
        校验：资产存在、父存在（None 除外）、不挂自己、沿 parent 链向上不得遇自己（防环）。
        upsert 去重键含 parent_id，补挂必须走本方法而不是重复 upsert。"""
        row = self.conn.execute(
            "SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise ValueError(f"资产不存在: {asset_id}")
        if row["parent_id"] == parent_id:  # 无变化：不发事件不空转
            return self.get_asset(asset_id)  # type: ignore[return-value]
        if parent_id == asset_id:
            raise ValueError("资产不能挂自己")
        if parent_id is not None:
            if self.conn.execute(
                    "SELECT 1 FROM assets WHERE id=?", (parent_id,)).fetchone() is None:
                raise ValueError(f"父资产不存在: {parent_id}")
            walker = parent_id
            while walker is not None:  # 防环：新祖先链上出现自己即拒绝
                if walker == asset_id:
                    raise ValueError("挂载会形成环（parent 链经过自身）")
                walker = self.conn.execute(
                    "SELECT parent_id FROM assets WHERE id=?", (walker,)
                ).fetchone()["parent_id"]
        with self._tx():
            self.conn.execute(
                "UPDATE assets SET parent_id=? WHERE id=?", (parent_id, asset_id))
        self.append_event(
            row["project_id"], "asset.reparent",
            {"asset_id": asset_id, "old_parent_id": row["parent_id"],
             "parent_id": parent_id},
            author="system")
        return self.get_asset(asset_id)  # type: ignore[return-value]

    def update_asset_meta(self, asset_id: str, patch: dict) -> dict:
        """合并更新资产 meta（title/scanned 等展示约定，§5.2）；不改 author/created_at。

        读-合并-写在单个 _tx() 内（A2：并发 PATCH 不丢键）。"""
        with self._tx():
            row = self.conn.execute(
                "SELECT meta FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise ValueError(f"资产不存在: {asset_id}")
            merged = {**_loads(row["meta"], {}), **patch}
            self.conn.execute(
                "UPDATE assets SET meta=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), asset_id))
        return self.get_asset(asset_id)  # type: ignore[return-value]

    def delete_asset(self, asset_id: str, author: str = "human") -> dict:
        """物理删除**叶子**资产（资产树整理/走查数据清理）。

        防护（ValueError→API 409）：仍被 finding.target_asset_id 引用，或存在子资产
        ——先删/改挂发现、先处理子节点。删资产不级联 findings。落 asset.deleted 审计事件。
        资产不存在抛 LookupError（→API 404）。
        """
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise LookupError(f"资产不存在: {asset_id}")
            ref = self.conn.execute(
                "SELECT id FROM findings WHERE target_asset_id=? LIMIT 1",
                (asset_id,)).fetchone()
            if ref is not None:
                raise ValueError(
                    f"资产 {row['value']} 仍被发现 {ref['id']} 引用，请先删除或改挂该发现")
            child = self.conn.execute(
                "SELECT id FROM assets WHERE parent_id=? LIMIT 1", (asset_id,)).fetchone()
            if child is not None:
                raise ValueError(f"资产 {row['value']} 存在子资产，请先处理子资产再删除")
            self.conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            snapshot = {"project_id": row["project_id"], "type": row["type"],
                        "value": row["value"], "parent_id": row["parent_id"]}
        self.append_event(
            snapshot["project_id"], "asset.deleted",
            {"asset_id": asset_id, "by": author, "type": snapshot["type"],
             "value": snapshot["value"], "parent_id": snapshot["parent_id"]},
            author=author)
        return {"id": asset_id, **snapshot}

    def list_assets(self, project_id: str, type_: str | None = None) -> list[dict]:
        sql = "SELECT * FROM assets WHERE project_id=?"
        params: list[Any] = [project_id]
        if type_:
            sql += " AND type=?"
            params.append(type_)
        sql += " ORDER BY created_at"
        out = []
        for r in self.conn.execute(sql, params):
            d = _row_to_dict(r) or {}
            d["meta"] = _loads(d.get("meta"), {})  # meta 列是 JSON 文本，读出解析回 dict
            out.append(d)
        return out

    def owner_tags(self, project_id: str) -> list[str]:
        """项目资产 meta.owner 去重清单（开窗注入 owner 规则的数据源，DESIGN.md §4）。"""
        tags: list[str] = []
        for r in self.conn.execute(
            "SELECT meta FROM assets WHERE project_id=?", (project_id,)
        ):
            meta = _loads(r["meta"], {})
            tag = meta.get("owner")
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    # ---------- 产物 ----------

    def add_artifact(
        self,
        project_id: str,
        path: str,
        kind: str = "file",
        description: str = "",
        sha256: str = "",
        author: str = "system",
    ) -> str:
        artifact_id = new_id("art")
        with self._tx():
            self.conn.execute(
                "INSERT INTO artifacts(id,project_id,path,kind,description,sha256,author,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (artifact_id, project_id, path, kind, description, sha256, author, now()),
            )
        return artifact_id

    def get_artifact(self, project_id: str, artifact_id: str) -> dict | None:
        """按 id 取产物（带项目隔离）；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE id=? AND project_id=?",
            (artifact_id, project_id),
        ).fetchone()
        return _row_to_dict(row)

    def list_artifacts(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    # ---------- 发现（findings，指纹去重合并） ----------

    def add_finding(
        self,
        project_id: str,
        vuln_class: str = "",
        title: str = "",
        target_asset_id: str | None = None,
        severity: str = "info",
        status: str = "unverified",
        evidence: dict | None = None,
        poc_artifact_id: str | None = None,
        confidence: float = 0.5,
        dedup_key: str | None = None,
        author: str = "system",
    ) -> dict:
        """重复发现（同 target+vuln_class+dedup_key）走证据并集（§5.3）：

        - evidence 列表键去重追加、notes 分段追加、其余键空缺补入（merge_finding_evidence）；
        - severity 就高、status 不因新报告降级（新报 verified 可升级）、confidence 取最大、
          poc_artifact_id 缺者回填；updated_at 刷新。
        返回 {"id":..., "merged": bool}。merged=True 表示命中既有记录。
        """
        # rev 发现的类别在 evidence.category（五类），vuln_class 可空；
        # 无任何去重键时不做合并（否则空 key 的发现会全并成一条）。
        key = dedup_key or vuln_class or None
        created = now()
        update_changes: list[str] = []  # A4：merge 分支的实质增补（事务后发 finding_update）
        with self._tx():
            # relates_to 强关系：悬空/跨项目/形态非法一律拒（E0，防幻觉 422）；
            # 合并分支里并集只追加本批新边，校验传入部分即可
            validate_relates_to(self.conn, project_id, evidence)
            row = None
            if key is not None:
                row = self.conn.execute(
                    "SELECT * FROM findings"
                    " WHERE project_id=? AND target_asset_id IS ? AND dedup_key=?",
                    (project_id, target_asset_id, key),
                ).fetchone()
            if row:
                # 合并目标自引防护（E0）：新证据若 relates_to 指向它将要并入的那条发现，
                # 并集后会产生自环边——视为引用错误拒掉，而不是悄悄落一条自边。
                incoming_rels = (evidence or {}).get("relates_to")
                if isinstance(incoming_rels, list) and any(
                    isinstance(r, dict) and r.get("finding_id") == row["id"]
                    for r in incoming_rels
                ):
                    raise ValueError(
                        f"relates_to 不能引用发现自身（该发现命中同 dedup 键合并到 {row['id']}）："
                        "升级结论请直接重报/修补原发现，自引无意义")
                # read-modify-write 全在本事务内（BEGIN IMMEDIATE 串行化，无丢失更新）
                old_ev = _loads(row["evidence"], {})
                old_poc_n = len(old_ev.get("pocs") or []) if isinstance(old_ev, dict) else 0
                old_rel_n = (
                    len(old_ev.get("relates_to") or []) if isinstance(old_ev, dict) else 0)
                merged_ev = merge_finding_evidence(old_ev, evidence or {})
                new_severity = row["severity"]
                if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(new_severity, 0):
                    new_severity = severity
                new_status = row["status"]
                if status == "verified":
                    new_status = "verified"
                new_poc = row["poc_artifact_id"] or poc_artifact_id
                # A4 变化检测（事务内算标志，事务后投递，多变化聚合一条 finding_update）
                if len(merged_ev.get("pocs") or []) > old_poc_n or (
                        not row["poc_artifact_id"] and poc_artifact_id):
                    update_changes.append("新增 POC")
                if new_severity != row["severity"]:
                    update_changes.append(f"严重度升至 {new_severity}")
                if row["status"] != "verified" and new_status == "verified":
                    update_changes.append("升级为已验证")
                if len(merged_ev.get("relates_to") or []) > old_rel_n:
                    update_changes.append("新增关联发现")
                self.conn.execute(
                    "UPDATE findings SET evidence=?, severity=?, status=?, poc_artifact_id=?,"
                    " confidence=MAX(confidence,?), updated_at=? WHERE id=?",
                    (json.dumps(merged_ev, ensure_ascii=False), new_severity, new_status,
                     new_poc, confidence, created, row["id"]),
                )
                finding_id, merged = row["id"], True
            else:
                finding_id = new_id("find")
                merged = False
                if key is None:  # 无去重键：以自身 id 为唯一指纹（列 NOT NULL，且永不合并）
                    key = finding_id
                self.conn.execute(
                    "INSERT INTO findings(id,project_id,target_asset_id,vuln_class,title,"
                    "severity,status,evidence,poc_artifact_id,confidence,dedup_key,author,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        finding_id,
                        project_id,
                        target_asset_id,
                        vuln_class,
                        title,
                        severity,
                        status,
                        json.dumps(evidence or {}, ensure_ascii=False),
                        poc_artifact_id,
                        confidence,
                        key,
                        author,
                        created,
                        created,
                    ),
                )
        self.append_event(
            project_id,
            "finding.new" if not merged else "finding.merged",
            {"finding_id": finding_id, "vuln_class": vuln_class, "severity": severity},
            author=author,
        )
        if merged and update_changes:  # 首次创建不通知；纯重复上报无变化不通知
            self._notify_finding_updates(project_id, finding_id, update_changes, author)
        return {"id": finding_id, "merged": merged}

    def list_findings(
        self,
        project_id: str,
        target_asset_id: str | None = None,
        min_severity: str | None = None,
        verified_only: bool = False,
    ) -> list[dict]:
        severity_rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        sql = "SELECT * FROM findings WHERE project_id=?"
        params: list[Any] = [project_id]
        if target_asset_id:
            sql += " AND target_asset_id=?"
            params.append(target_asset_id)
        if min_severity and min_severity in severity_rank:
            # min_severity 语义 = 不低于该级（如 high → high+critical）
            above = [s for s, r in severity_rank.items() if r >= severity_rank[min_severity]]
            sql += f" AND severity IN ({','.join('?' * len(above))})"
            params.extend(above)
        if verified_only:
            sql += " AND status='verified'"
        sql += " ORDER BY created_at"
        out = []
        for r in self.conn.execute(sql, params):
            d = _row_to_dict(r) or {}
            d["evidence"] = _loads(d["evidence"], {})
            out.append(d)
        return out

    def get_finding(self, project_id: str, finding_id: str) -> dict | None:
        """按 id 取发现（带项目隔离）；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM findings WHERE id=? AND project_id=?",
            (finding_id, project_id),
        ).fetchone()
        d = _row_to_dict(row)
        if d:
            d["evidence"] = _loads(d.get("evidence", "{}"), {})
        return d

    def patch_finding(
        self,
        project_id: str,
        finding_id: str,
        *,
        status: str | None = None,
        evidence: dict | None = None,
        author: str = "human",
    ) -> dict | None:
        """人类修订发现：status 走白名单（非法 ValueError），evidence 浅层 merge。
        发 finding.updated；不存在返回 None；无字段可改时原样返回。

        read-modify-write 整体在单个 _tx() 内（A2：消除锁外读→锁内写的丢失更新）。"""
        changed: list[str] = []
        new_status = ""
        old_status = ""
        do_update = False
        update_changes: list[str] = []  # A4：实质增补（verified 升级 / pocs/relates 增长）
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM findings WHERE id=? AND project_id=?",
                (finding_id, project_id),
            ).fetchone()
            if row is None:
                return None
            old_status = row["status"]
            old_ev = _loads(row["evidence"], {})
            old_poc_n = len(old_ev.get("pocs") or []) if isinstance(old_ev, dict) else 0
            old_rel_n = (
                len(old_ev.get("relates_to") or []) if isinstance(old_ev, dict) else 0)
            sets: list[str] = []
            params: list[Any] = []
            new_status = row["status"]
            if status is not None:
                if status not in FINDING_STATUSES:
                    raise ValueError(f"非法 finding status: {status}（允许 {FINDING_STATUSES}）")
                sets.append("status=?")
                params.append(status)
                new_status = status
                changed.append("status")
            if evidence is not None:
                # 强关系同样防幻觉：PATCH 携带 relates_to 时逐边校验存在性/同项目（E0）
                validate_relates_to(self.conn, project_id, evidence)
                # 人类 PATCH = 浅层 merge（键级覆盖；列表并集只发生在 add_finding 合并路径）
                merged = {**old_ev, **evidence}
                sets.append("evidence=?")
                params.append(json.dumps(merged, ensure_ascii=False))
                changed.append("evidence")
                # A4：列表键增长算实质增补（浅层 merge 下列表为整键覆盖，比长度即可）
                if len(merged.get("pocs") or []) > old_poc_n:
                    update_changes.append("新增 POC")
                if len(merged.get("relates_to") or []) > old_rel_n:
                    update_changes.append("新增关联发现")
            if sets:
                sets.append("updated_at=?")
                params.extend([now(), finding_id])
                self.conn.execute(
                    f"UPDATE findings SET {', '.join(sets)} WHERE id=?", params)
                do_update = True
        retracted = (
            do_update and old_status != "false-positive" and new_status == "false-positive"
        )
        if do_update and old_status != "verified" and new_status == "verified":
            update_changes.insert(0, "升级为已验证")
        if do_update:
            self.append_event(
                project_id, "finding.updated",
                {"finding_id": finding_id, "changed": changed, "status": new_status},
                author=author,
            )
        updated = self.get_finding(project_id, finding_id)
        if retracted:
            # 撤回传播（§6.7 的 1.6）：仅在非误报→误报的跳变瞬间触发一次，
            # 重复 PATCH 不重放。广播 + 四类目标私信/挂标见 _propagate_retraction。
            # 误报跳变只走 basis_stale，不发 finding_update（语义严格分开，A4）。
            self._propagate_retraction(project_id, finding_id, author, updated)
        elif do_update and update_changes:
            self._notify_finding_updates(project_id, finding_id, update_changes, author)
        return updated

    def delete_finding(
        self, project_id: str, finding_id: str, author: str = "human",
    ) -> dict | None:
        """物理删除发现（垃圾/走查数据清理）。**误报请走 PATCH status=false-positive**
        ——那会触发撤回传播；删除不触发。同事务级联：

        - 其他 findings.evidence.relates_to 中指向它的边逐条摘除（含历史悬空引用）；
        - tasks.context_refs/stale_refs 中摘除该 id（只摘引用，**不删任务**）；
        - chain_links 不级联：链详情既有「孤儿实体 deleted:true 占位」语义；
        - session_inbox 私信保留（payload 标题快照自包含，同「关窗私信留审计」）；
        - poc_artifact 不删（产物可能复用）。

        落 finding.deleted 审计事件（快照 + 摘边数 + 受影响任务）。不存在返 None。
        """
        affected_tasks: list[str] = []
        trimmed_rels = 0
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM findings WHERE id=? AND project_id=?",
                (finding_id, project_id)).fetchone()
            if row is None:
                return None
            ts = now()
            # 反向 relates_to 摘边（逐条 read-modify-write，全在本事务）
            for f in self.conn.execute(
                    "SELECT id,evidence FROM findings WHERE project_id=?", (project_id,)):
                ev = _loads(f["evidence"], {})
                rels = ev.get("relates_to")
                if not isinstance(rels, list):
                    continue
                kept = [r for r in rels
                        if not (isinstance(r, dict) and r.get("finding_id") == finding_id)]
                if len(kept) != len(rels):
                    ev["relates_to"] = kept
                    trimmed_rels += 1
                    self.conn.execute(
                        "UPDATE findings SET evidence=?, updated_at=? WHERE id=?",
                        (json.dumps(ev, ensure_ascii=False), ts, f["id"]))
            # 任务依据/挂标数组摘引用（任务本身的去留是独立决定，不由删 finding 代办）
            for t in self.conn.execute(
                    "SELECT id,context_refs,stale_refs FROM tasks WHERE project_id=?",
                    (project_id,)):
                ctx = _loads(t["context_refs"], [])
                stale = _loads(t["stale_refs"], [])
                nctx = [x for x in ctx if x != finding_id]
                nstale = [x for x in stale if x != finding_id]
                if nctx != ctx or nstale != stale:
                    self.conn.execute(
                        "UPDATE tasks SET context_refs=?, stale_refs=?, updated_at=? WHERE id=?",
                        (json.dumps(nctx, ensure_ascii=False),
                         json.dumps(nstale, ensure_ascii=False), ts, t["id"]))
                    affected_tasks.append(t["id"])
            self.conn.execute("DELETE FROM findings WHERE id=?", (finding_id,))
            snapshot = {"title": row["title"], "vuln_class": row["vuln_class"],
                        "severity": row["severity"], "status": row["status"],
                        "target_asset_id": row["target_asset_id"]}
        self.append_event(
            project_id, "finding.deleted",
            {"finding_id": finding_id, "by": author, **snapshot,
             "trimmed_relates_to": trimmed_rels, "affected_tasks": affected_tasks},
            author=author)
        return {"id": finding_id, **snapshot,
                "trimmed_relates_to": trimmed_rels, "affected_tasks": affected_tasks}

    def _propagate_retraction(
        self, project_id: str, finding_id: str, by: str, finding: dict | None = None,
    ) -> None:
        """发现被推翻时的引用方传播（DESIGN.md §6.7 的 1.6，系统侧唯一投递点）：

        1. 项目级广播 finding.retracted（事件流/WS 全员可见，主代理下轮 tick 可见）；
        2. 私聊（session_inbox + message.inbox，按 (to_session,ref,kind) 未读去重）：
           a) 被推翻 finding 的作者会话；b) relates_to 反向边下游 finding 的作者；
           c) context_refs 命中任务及其子树：claimed→认领者私信+stale_refs 挂标，
              open→只挂标（认领开场白必见），done/failed→作者私信不改行；
           d) 命中任务向上一级：父任务 claimed 时抄送父认领者（不挂标）。
        human/system 作者不私聊。closed 会话不投递（行留库，UI 不再显示）。
        """
        from core.blackboard.tasks import TaskQueue

        finding = finding or self.get_finding(project_id, finding_id)
        if finding is None:
            return
        evidence = finding.get("evidence") or {}
        payload = {
            "finding_id": finding_id,
            "title": finding.get("title", ""),
            "vuln_class": finding.get("vuln_class", ""),
            "by": by,
            "note": str(evidence.get("note", "")) if isinstance(evidence, dict) else "",
        }
        self.append_event(
            project_id, "finding.retracted",
            {"finding_id": finding_id, "title": payload["title"],
             "vuln_class": payload["vuln_class"], "by": by},
            author=by)

        alive = self._alive_sessions(project_id)

        def is_session(x: Any) -> bool:
            return isinstance(x, str) and x.startswith("sess-") and x in alive \
                and alive[x] != "closed"

        targets: set[str] = set()

        def add_target(sid: Any) -> None:
            if is_session(sid):
                targets.add(sid)  # type: ignore[arg-type]

        # a) 被推翻发现的作者会话
        add_target(finding.get("author"))
        # b) relates_to 反向边：以本发现为依据的下游发现作者
        for f in self.list_findings(project_id):
            rels = (f.get("evidence") or {}).get("relates_to") or []
            if any(isinstance(x, dict) and x.get("finding_id") == finding_id for x in rels):
                add_target(f.get("author"))

        # c) context_refs 命中任务 + parent_id 子树
        tq = TaskQueue(self)
        by_id, hits, subtree = self._finding_task_graph(tq, project_id, finding_id)
        for tid in subtree:
            t = by_id[tid]
            if t["status"] in ("open", "claimed"):
                tq.add_stale_ref(tid, finding_id)
            if t["status"] == "claimed":
                add_target(t.get("claimed_by"))
            elif t["status"] in ("done", "failed"):
                add_target(t.get("created_by"))
        # d) 命中任务向上一级：父任务在跑则抄送协调者（不挂 stale_refs）
        for tid in hits:
            parent = by_id.get(by_id[tid].get("parent_id") or "")
            if parent and parent["status"] == "claimed":
                add_target(parent.get("claimed_by"))

        for sid in sorted(targets):
            if self.inbox_post(project_id, sid, "basis_stale", finding_id, payload):
                self.append_event(
                    project_id, "message.inbox",
                    {"to_session": sid, "kind": "basis_stale",
                     "ref_id": finding_id, "title": payload["title"], "by": by},
                    session_id=sid, author="system")

    def _alive_sessions(self, project_id: str) -> dict[str, str]:
        """id→status 的未关窗会话映射（私信目标过滤用；closed 不投递）。"""
        return {
            r["id"]: r["status"]
            for r in self.conn.execute(
                "SELECT id,status FROM sessions WHERE project_id=?", (project_id,))
        }

    def _finding_task_graph(
        self, tq: Any, project_id: str, finding_id: str,
    ) -> tuple[dict[str, dict], set[str], set[str]]:
        """context_refs 命中任务集合 + 沿 parent_id 向下展开子树（撤回/更新传播共用）。

        返回 (by_id, hits, subtree)：hits=直接引用 finding 的任务，
        subtree=hits 及其全部后代任务。"""
        tasks = tq.list_tasks(project_id)
        by_id = {t["id"]: t for t in tasks}
        children: dict[str | None, list[dict]] = {}
        for t in tasks:
            children.setdefault(t.get("parent_id"), []).append(t)
        hits = {t["id"] for t in tasks if finding_id in (t.get("context_refs") or [])}
        subtree, stack = set(hits), list(hits)
        while stack:
            cur = stack.pop()
            for ch in children.get(cur, []):
                if ch["id"] not in subtree:
                    subtree.add(ch["id"])
                    stack.append(ch["id"])
        return by_id, hits, subtree

    def _notify_finding_updates(
        self, project_id: str, finding_id: str, changes: list[str], by: str,
    ) -> None:
        """发现实质增补/升级的信息式私信（A4，kind='finding_update'）。

        与撤回（basis_stale，强制三选一+stale_refs 挂标）严格分语义：本通知
        - 只给引用方**在跑任务**（含子树）的认领者，及命中任务上一级父认领者；
        - 不给 open/done/failed 任务、不给造成本次更新的作者本人、不给 closed 会话；
        - 不挂 stale_refs、不强制任何动作。
        多次增补在未读期间只此一条（(to_session,ref_id,kind) 未读唯一索引去重）。
        """
        if not changes:
            return
        from core.blackboard.tasks import TaskQueue

        finding = self.get_finding(project_id, finding_id)
        if finding is None:
            return
        tq = TaskQueue(self)
        by_id, hits, subtree = self._finding_task_graph(tq, project_id, finding_id)
        alive = self._alive_sessions(project_id)
        targets: set[str] = set()

        def add_target(sid: Any) -> None:
            if (isinstance(sid, str) and sid.startswith("sess-")
                    and sid in alive and alive[sid] != "closed"):
                targets.add(sid)

        for tid in subtree:
            if by_id[tid]["status"] == "claimed":
                add_target(by_id[tid].get("claimed_by"))
        for tid in hits:  # d) 命中任务向上一级：父任务在跑则抄送协调者
            parent = by_id.get(by_id[tid].get("parent_id") or "")
            if parent and parent["status"] == "claimed":
                add_target(parent.get("claimed_by"))
        targets.discard(by)  # 不给造成更新的本人投递（自报 POC 不需通知自己）

        title = finding.get("title", "")
        payload = {"finding_id": finding_id, "title": title, "changes": changes, "by": by}
        for sid in sorted(targets):
            if self.inbox_post(project_id, sid, "finding_update", finding_id, payload):
                self.append_event(
                    project_id, "message.inbox",
                    {"to_session": sid, "kind": "finding_update",
                     "ref_id": finding_id, "title": f"发现增补：{title}",
                     "changes": changes, "by": by},
                    session_id=sid, author="system")

    # ---------- 函数知识库（func_kb，逆向防重复劳动主力，§5.4） ----------

    def upsert_func(
        self,
        project_id: str,
        binary_sha256: str,
        address: int,
        name: str,
        analysis: str = "",
        risk_tags: list[str] | None = None,
        confidence: float = 0.5,
        analyzed_by: str = "system",
    ) -> dict:
        """按 (project, binary, address) 去重。既有条目：名称入演变史、confidence 取最大、
        analysis 非空时追加（不覆盖前人结论）。返回 {"id":..., "created": bool}。
        """
        created = now()
        with self._tx():
            row = self.conn.execute(
                "SELECT id, name_history, confidence, analysis FROM func_kb"
                " WHERE project_id=? AND binary_sha256=? AND address=?",
                (project_id, binary_sha256, address),
            ).fetchone()
            if row:
                history = _loads(row["name_history"], [])
                if name and name != _loads(history[-1]["name"] if history else "", None):
                    history.append({"name": name, "by": analyzed_by, "at": created})
                new_analysis = row["analysis"]
                if analysis and analysis not in new_analysis:
                    new_analysis = (new_analysis + "\n" + analysis).strip()
                self.conn.execute(
                    "UPDATE func_kb SET name=?, name_history=?, analysis=?, risk_tags=?,"
                    " confidence=MAX(confidence,?), analyzed_by=?, updated_at=? WHERE id=?",
                    (
                        name,
                        json.dumps(history, ensure_ascii=False),
                        new_analysis,
                        json.dumps(risk_tags or [], ensure_ascii=False),
                        confidence,
                        analyzed_by,
                        created,
                        row["id"],
                    ),
                )
                func_id, is_new = row["id"], False
            else:
                func_id = new_id("func")
                is_new = True
                self.conn.execute(
                    "INSERT INTO func_kb(id,project_id,binary_sha256,address,name,name_history,"
                    "analysis,risk_tags,confidence,analyzed_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        func_id,
                        project_id,
                        binary_sha256,
                        address,
                        name,
                        json.dumps([{"name": name, "by": analyzed_by, "at": created}], ensure_ascii=False),
                        analysis,
                        json.dumps(risk_tags or [], ensure_ascii=False),
                        confidence,
                        analyzed_by,
                        created,
                        created,
                    ),
                )
        return {"id": func_id, "created": is_new}

    def lookup_func(self, project_id: str, binary_sha256: str, address: int) -> dict | None:
        """Agent 反编译前的强制查询入口（§5.4 硬规则）。"""
        row = self.conn.execute(
            "SELECT * FROM func_kb WHERE project_id=? AND binary_sha256=? AND address=?",
            (project_id, binary_sha256, address),
        ).fetchone()
        d = _row_to_dict(row)
        if d:
            d["name_history"] = _loads(d["name_history"], [])
            d["risk_tags"] = _loads(d["risk_tags"], [])
        return d

    def list_funcs_by_risk(self, project_id: str, binary_sha256: str, risk_tag: str) -> list[dict]:
        """按 risk_tag 过滤（JSON 数组成员匹配）。"""
        rows = self.conn.execute(
            "SELECT * FROM func_kb WHERE project_id=? AND binary_sha256=? AND risk_tags LIKE ?",
            (project_id, binary_sha256, f'%"{risk_tag}"%'),
        ).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r) or {}
            d["name_history"] = _loads(d.get("name_history", "[]"), [])
            d["risk_tags"] = _loads(d.get("risk_tags", "[]"), [])
            out.append(d)
        return out

    def list_funcs(self, project_id: str, binary_sha256: str | None = None) -> list[dict]:
        """按二进制列出函数库（Agent 动手前查重的入口，§5.4）。"""
        sql = "SELECT * FROM func_kb WHERE project_id=?"
        params: list[Any] = [project_id]
        if binary_sha256:
            sql += " AND binary_sha256=?"
            params.append(binary_sha256)
        rows = self.conn.execute(sql + " ORDER BY address", params).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r) or {}
            d = self._func_row_to_dict(r) or {}
            out.append(d)
        return out

    @staticmethod
    def _func_row_to_dict(row: sqlite3.Row | None) -> dict | None:
        d = _row_to_dict(row)
        if d:
            d["name_history"] = _loads(d.get("name_history", "[]"), [])
            d["risk_tags"] = _loads(d.get("risk_tags", "[]"), [])
        return d

    def get_func(self, project_id: str, func_id: str) -> dict | None:
        """按 id 取函数知识条目（带项目隔离）；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM func_kb WHERE id=? AND project_id=?",
            (func_id, project_id),
        ).fetchone()
        return self._func_row_to_dict(row)

    def patch_func(
        self,
        project_id: str,
        func_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        risk_tags: Any = UNSET,
        author: str = "human",
    ) -> dict | None:
        """人类修订函数知识（不含 confidence——那是 AI 字段，人不动）：

        - name：改名，旧名进 name_history；
        - note：以 ``## 笔记 <utc> by <author>`` 分段**追加** analysis；
        - risk_tags：传 list（含 []）=全量替换；不传(UNSET)=不动。
        发 func.updated；不存在返回 None。
        """
        stamp = now()
        # read-modify-write 整体在单个 _tx() 内（A2：并发改名/追加笔记不丢历史）
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM func_kb WHERE id=? AND project_id=?",
                (func_id, project_id),
            ).fetchone()
            if row is None:
                return None
            history = _loads(row["name_history"], [])
            new_name = row["name"]
            changed: list[str] = []
            if name is not None and name != row["name"]:
                history.append({"name": name, "by": author, "at": stamp})
                new_name = name
                changed.append("name")
            analysis = row["analysis"] or ""
            if note and note.strip():
                section = f"## 笔记 {stamp} by {author}\n{note.strip()}"
                analysis = f"{analysis}\n\n{section}" if analysis else section
                changed.append("analysis")
            sets = ["name=?", "name_history=?", "analysis=?", "updated_at=?"]
            params: list[Any] = [
                new_name,
                json.dumps(history, ensure_ascii=False),
                analysis,
                stamp,
            ]
            if risk_tags is not UNSET:
                sets.append("risk_tags=?")
                params.append(json.dumps(list(risk_tags), ensure_ascii=False))
                changed.append("risk_tags")
            params.append(func_id)
            self.conn.execute(
                f"UPDATE func_kb SET {', '.join(sets)} WHERE id=?", params)
            binary_sha256, address = row["binary_sha256"], row["address"]
        self.append_event(
            project_id, "func.updated",
            {"func_id": func_id, "binary_sha256": binary_sha256,
             "address": hex(int(address)), "changed": changed},
            author=author,
        )
        return self.get_func(project_id, func_id)

    # ---------- 攻击链（chains，人工建链；节点必须引用本项目既有实体，防幻觉防跨项目） ----------

    def create_chain(self, project_id: str, name: str, goal: str = "", author: str = "human") -> str:
        chain_id = new_id("chain")
        with self._tx():
            self.conn.execute(
                "INSERT INTO chains(id,project_id,name,goal,status,created_at,updated_at)"
                " VALUES(?,?,?,?, 'hypothesis', ?, ?)",
                (chain_id, project_id, name, goal, now(), now()),
            )
        self.append_event(
            project_id, "chain.created",
            {"chain_id": chain_id, "name": name, "goal": goal, "status": "hypothesis"},
            author=author,
        )
        return chain_id

    def list_chains(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM chain_links l WHERE l.chain_id=c.id) AS link_count"
            " FROM chains c WHERE c.project_id=? ORDER BY c.updated_at DESC",
            (project_id,),
        ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    def _owned_chain(self, project_id: str, chain_id: str) -> sqlite3.Row | None:
        """链归属校验：跨项目访问等同不存在。"""
        return self.conn.execute(
            "SELECT * FROM chains WHERE id=? AND project_id=?", (chain_id, project_id)
        ).fetchone()

    def update_chain(
        self, project_id: str, chain_id: str, *,
        name: Any = UNSET, goal: Any = UNSET, status: Any = UNSET,
        author: str = "human",
    ) -> dict | None:
        row = self._owned_chain(project_id, chain_id)
        if row is None:
            return None
        if status is not UNSET and status not in CHAIN_STATUSES:
            raise ValueError(f"非法链状态: {status}，允许: {list(CHAIN_STATUSES)}")
        sets, args, changed = [], [], {}
        if name is not UNSET:
            sets.append("name=?"); args.append(name); changed["name"] = name
        if goal is not UNSET:
            sets.append("goal=?"); args.append(goal); changed["goal"] = goal
        if status is not UNSET:
            sets.append("status=?"); args.append(status); changed["status"] = status
        if not sets:
            return _row_to_dict(row)
        sets.append("updated_at=?"); args.append(now()); args.append(chain_id)
        with self._tx():
            self.conn.execute(f"UPDATE chains SET {', '.join(sets)} WHERE id=?", args)
        self.append_event(
            project_id, "chain.updated", {"chain_id": chain_id, **changed}, author=author
        )
        return self.get_chain(chain_id)

    def delete_chain(self, project_id: str, chain_id: str, author: str = "human") -> bool:
        row = self._owned_chain(project_id, chain_id)
        if row is None:
            return False
        with self._tx():
            self.conn.execute("DELETE FROM chain_links WHERE chain_id=?", (chain_id,))
            self.conn.execute("DELETE FROM chains WHERE id=?", (chain_id,))
        self.append_event(
            project_id, "chain.deleted", {"chain_id": chain_id, "name": row["name"]},
            author=author,
        )
        return True

    def add_chain_link(
        self, project_id: str, chain_id: str, node_type: str, node_id: str,
        edge_note: str = "", author: str = "human",
    ) -> str:
        """链必须属本项目（缺链抛 LookupError→API 404）；节点必须真实且属本项目
        （ValueError→API 422）。seq 线性自动追加，调用方不传。"""
        if self._owned_chain(project_id, chain_id) is None:
            raise LookupError(f"攻击链不存在: {chain_id}")
        if node_type not in NODE_TABLES:
            raise ValueError(f"非法节点类型: {node_type}，允许: {sorted(NODE_TABLES)}")
        table = NODE_TABLES[node_type]
        exists = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND project_id=?", (node_id, project_id)
        ).fetchone()
        if not exists:
            raise ValueError(
                f"攻击链节点不存在或不属本项目: {node_type}:{node_id}（禁止跨项目/引用未落库实体）"
            )
        link_id = new_id("clink")
        with self._tx():
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM chain_links WHERE chain_id=?",
                (chain_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            self.conn.execute(
                "INSERT INTO chain_links(id,chain_id,seq,node_type,node_id,edge_note,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (link_id, chain_id, seq, node_type, node_id, edge_note, now()),
            )
            self.conn.execute("UPDATE chains SET updated_at=? WHERE id=?", (now(), chain_id))
        self.append_event(
            project_id, "chain.link_added",
            {"chain_id": chain_id, "link_id": link_id, "seq": seq,
             "node_type": node_type, "node_id": node_id, "edge_note": edge_note},
            author=author,
        )
        return link_id

    def update_link_note(
        self, project_id: str, link_id: str, edge_note: str, author: str = "human"
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT l.* FROM chain_links l JOIN chains c ON l.chain_id=c.id"
            " WHERE l.id=? AND c.project_id=?", (link_id, project_id),
        ).fetchone()
        if row is None:
            return None
        with self._tx():
            self.conn.execute(
                "UPDATE chain_links SET edge_note=? WHERE id=?", (edge_note, link_id)
            )
            self.conn.execute(
                "UPDATE chains SET updated_at=? WHERE id=?", (now(), row["chain_id"])
            )
        self.append_event(
            project_id, "chain.updated",
            {"chain_id": row["chain_id"], "link_id": link_id, "edge_note": edge_note},
            author=author,
        )
        d = _row_to_dict(row) or {}
        d["edge_note"] = edge_note
        return d

    def delete_chain_link(self, project_id: str, link_id: str, author: str = "human") -> dict | None:
        """删后对该链剩余 link 按旧序重排 seq 1..n（线性序不留洞）。"""
        row = self.conn.execute(
            "SELECT l.* FROM chain_links l JOIN chains c ON l.chain_id=c.id"
            " WHERE l.id=? AND c.project_id=?", (link_id, project_id),
        ).fetchone()
        if row is None:
            return None
        chain_id = row["chain_id"]
        with self._tx():
            self.conn.execute("DELETE FROM chain_links WHERE id=?", (link_id,))
            remaining = self.conn.execute(
                "SELECT id FROM chain_links WHERE chain_id=? ORDER BY seq", (chain_id,)
            ).fetchall()
            for i, r in enumerate(remaining, start=1):
                self.conn.execute(
                    "UPDATE chain_links SET seq=? WHERE id=?", (i, r["id"])
                )
            self.conn.execute("UPDATE chains SET updated_at=? WHERE id=?", (now(), chain_id))
        self.append_event(
            project_id, "chain.link_removed",
            {"chain_id": chain_id, "link_id": link_id, "seq": row["seq"],
             "node_type": row["node_type"], "node_id": row["node_id"]},
            author=author,
        )
        return {"chain_id": chain_id, "link_id": link_id}

    def get_chain(self, chain_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM chains WHERE id=?", (chain_id,)).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row) or {}
        links = self.conn.execute(
            "SELECT * FROM chain_links WHERE chain_id=? ORDER BY seq", (chain_id,)
        ).fetchall()
        d["links"] = [_row_to_dict(l) or {} for l in links]
        return d
