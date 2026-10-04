"""多智能体协调的独立持久化边界。

协调计划与现有 chat/agent 线程解耦。当前版本提供计划与任务 DAG 的 CRUD，
后续调度器可以在不改工作台运行时的前提下消费这些任务。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


PLAN_STATUSES = ("draft", "active", "paused", "completed")
TASK_STATUSES = ("pending", "ready", "running", "blocked", "completed", "failed")
OBJECT_KINDS = ("function", "string", "xref", "behavior", "evidence", "artifact")
CONFLICT_STATUSES = ("open", "resolved", "dismissed")
VERIFICATION_STATUSES = ("passed", "needs_evidence", "conflict", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class CoordinationStore:
    """协调域的 SQLite 存储适配器。

    ``bb`` 只作为项目数据库连接提供者；表名使用独立前缀，避免和现有
    智能体、任务、会话表混用。
    """

    def __init__(self, bb):
        self.bb = bb
        self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.bb._tx():
            self.bb.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS coordination_plans (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    objective TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    config TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_coord_plans_project
                    ON coordination_plans(project_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS coordination_tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL REFERENCES coordination_plans(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 50,
                    depends_on TEXT NOT NULL DEFAULT '[]',
                    evidence TEXT NOT NULL DEFAULT '[]',
                    task_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_coord_tasks_plan
                    ON coordination_tasks(plan_id, priority, created_at);
                CREATE TABLE IF NOT EXISTS coordination_objects (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    plan_id TEXT,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    object_ref TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_coord_objects_project
                    ON coordination_objects(project_id, kind, updated_at DESC);
                CREATE TABLE IF NOT EXISTS coordination_object_artifacts (
                    object_id TEXT NOT NULL REFERENCES coordination_objects(id) ON DELETE CASCADE,
                    artifact_ref TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'supports',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (object_id, artifact_ref)
                );
                CREATE TABLE IF NOT EXISTS coordination_conflicts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    left_object_id TEXT NOT NULL REFERENCES coordination_objects(id) ON DELETE CASCADE,
                    right_object_id TEXT NOT NULL REFERENCES coordination_objects(id) ON DELETE CASCADE,
                    field TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_coord_conflicts_project
                    ON coordination_conflicts(project_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS coordination_verifications (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL NOT NULL DEFAULT 0,
                    issues TEXT NOT NULL DEFAULT '[]',
                    followup_task_ids TEXT NOT NULL DEFAULT '[]',
                    checked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_coord_verifications_task
                    ON coordination_verifications(project_id, task_id, checked_at DESC);
                """)
            # M3（orchestrator-coordination-fusion，2026-10-04）：计划节点绑定真实
            # tasks.id（编排器派单后回填）。存量库 CREATE IF NOT EXISTS 不会补列 →
            # 幂等 ALTER（PRAGMA 守卫）。
            cols = {r[1] for r in self.bb.conn.execute(
                "PRAGMA table_info(coordination_tasks)").fetchall()}
            if "task_id" not in cols:
                self.bb.conn.execute(
                    "ALTER TABLE coordination_tasks"
                    " ADD COLUMN task_id TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _decode(row: Any) -> dict[str, Any]:
        item = dict(row)
        for key in ("config", "depends_on", "evidence", "data"):
            if key not in item:
                continue
            try:
                item[key] = json.loads(item[key] or ("{}" if key in ("config", "data") else "[]"))
            except (TypeError, ValueError):
                item[key] = {} if key in ("config", "data") else []
        return item

    @staticmethod
    def _confidence(value: float | int | None) -> float:
        try:
            score = float(0.5 if value is None else value)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence 必须是 0 到 1 之间的数字") from exc
        if not 0 <= score <= 1:
            raise ValueError("confidence 必须是 0 到 1 之间的数字")
        return round(score, 4)

    def _object_artifacts(self, object_id: str) -> list[dict[str, str]]:
        rows = self.bb.conn.execute(
            "SELECT artifact_ref,relation,created_at FROM coordination_object_artifacts"
            " WHERE object_id=? ORDER BY created_at", (object_id,)).fetchall()
        return [dict(row) for row in rows]

    def _with_object_artifacts(self, row: Any) -> dict[str, Any]:
        item = self._decode(row)
        item["artifact_refs"] = self._object_artifacts(item["id"])
        return item

    def _latest_verification(self, project_id: str, task_id: str) -> dict[str, Any] | None:
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_verifications WHERE project_id=? AND task_id=?"
            " ORDER BY checked_at DESC LIMIT 1", (project_id, task_id)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ("issues", "followup_task_ids"):
            try:
                item[key] = json.loads(item[key] or "[]")
            except (TypeError, ValueError):
                item[key] = []
        return item

    def preflight(self, project_id: str, plan_id: str) -> dict[str, Any]:
        """生成启动前服务端快照；只读，不改变计划状态。"""
        plan = self.get_plan(project_id, plan_id)
        self.refresh_readiness(project_id)
        plan = self.get_plan(project_id, plan_id)
        tasks = plan["tasks"]
        statuses = {t["id"]: t["status"] for t in tasks}
        roots = [t for t in tasks if not t.get("depends_on")]
        unresolved = []
        for task in tasks:
            for dep in task.get("depends_on") or []:
                if dep not in statuses:
                    unresolved.append({"task_id": task["id"], "dependency": dep})
        cycle = False
        visiting: set[str] = set(); visited: set[str] = set()
        graph = {t["id"]: set(t.get("depends_on") or []) for t in tasks}
        def walk(node: str) -> None:
            nonlocal cycle
            if node in visiting:
                cycle = True; return
            if node in visited:
                return
            visiting.add(node)
            for dep in graph.get(node, set()):
                if dep in graph:
                    walk(dep)
            visiting.remove(node); visited.add(node)
        for node in graph:
            walk(node)
        project = self.bb.get_project(project_id) or {}
        config = project.get("config") or {}
        mission = config.get("mission") if isinstance(config.get("mission"), dict) else {}
        roe = config.get("redteam_roe") if isinstance(config.get("redteam_roe"), dict) else {}
        blockers: list[dict[str, Any]] = []
        if plan["status"] == "completed":
            blockers.append({"code": "completed", "severity": "error", "message": "计划已经完成"})
        if not tasks:
            blockers.append({"code": "empty_plan", "severity": "error", "message": "计划没有节点"})
        if cycle or unresolved:
            blockers.append({"code": "invalid_dag", "severity": "error", "message": "计划依赖图存在循环或未解析依赖"})
        return {
            "plan_id": plan_id, "status": plan["status"],
            "revision": plan["updated_at"], "plan": plan,
            "dependencies": {"task_count": len(tasks), "root_count": len(roots),
                             "ready_count": sum(t["status"] == "ready" for t in tasks),
                             "blocked_count": sum(t["status"] == "blocked" for t in tasks),
                             "cycle": cycle, "unresolved": unresolved},
            "safety": {"track": project.get("track") or "pentest", "mission": mission,
                       "roe": {"targets": roe.get("targets", ""), "window": roe.get("window", ""),
                               "exclusions": roe.get("exclusions", ""), "approver": roe.get("approver", ""),
                               "complete": all(roe.get(k) for k in ("targets", "window", "exclusions", "approver")) if roe else None},
                       "autonomy": config.get("autonomy") or {}},
            "blockers": blockers,
        }

    def communications(self, project_id: str, *, session_id: str | None = None,
                       unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        """项目级通信聚合，直接查 session_inbox，避免前端 N+1。"""
        sql = "SELECT * FROM session_inbox WHERE project_id=?"
        args: list[Any] = [project_id]
        if session_id:
            sql += " AND to_session=?"; args.append(session_id)
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(max(1, min(500, int(limit))))
        rows = []
        for r in self.bb.conn.execute(sql, args).fetchall():
            item = dict(r)
            item["payload"] = json.loads(item.get("payload") or "{}")
            item["unread"] = item.get("read_at") is None
            rows.append(item)
        return rows

    def mark_communications_read(self, project_id: str, ids: list[str] | None = None,
                                 session_ids: list[str] | None = None) -> int:
        """批量标记项目通信已读，范围始终限制在 project_id。"""
        with self.bb._tx():
            where = "project_id=? AND read_at IS NULL"; args: list[Any] = [project_id]
            if ids:
                where += " AND id IN (" + ",".join("?" * len(ids)) + ")"; args.extend(ids)
            if session_ids:
                where += " AND to_session IN (" + ",".join("?" * len(session_ids)) + ")"; args.extend(session_ids)
            cur = self.bb.conn.execute(f"UPDATE session_inbox SET read_at=? WHERE {where}", [now(), *args])
            return cur.rowcount

    def reset_failed_nodes(self, project_id: str, plan_id: str) -> list[str]:
        """将失败节点重置为 ready（仅无未完成依赖者），保留真实 task/证据履历。"""
        plan = self.get_plan(project_id, plan_id)
        states = {t["id"]: t["status"] for t in plan["tasks"]}
        ids: list[str] = []
        with self.bb._tx():
            for task in plan["tasks"]:
                if task["status"] != "failed":
                    continue
                deps = task.get("depends_on") or []
                if all(states.get(dep) == "completed" for dep in deps):
                    self.bb.conn.execute(
                        "UPDATE coordination_tasks SET status='ready',task_id='',updated_at=?"
                        " WHERE id=? AND project_id=?", (_now(), task["id"], project_id))
                    ids.append(task["id"])
        return ids

    def timeline(self, project_id: str, plan_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """聚合计划节点相关事件，供 LiveRoom 回看，不受最近事件窗口限制。"""
        self.get_plan(project_id, plan_id)
        rows = self.bb.conn.execute(
            "SELECT id,kind,payload,session_id,author,created_at FROM events"
            " WHERE project_id=? AND (kind LIKE 'coordination.%' OR kind IN"
            " ('delegation.posted','task.done','task.failed','plan.node_ready'))"
            " ORDER BY id DESC LIMIT ?", (project_id, max(1, min(500, int(limit)))))
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if payload.get("plan_id") == plan_id or payload.get("coord_task_id"):
                out.append({**dict(row), "payload": payload})
        return out

    def overview(self, project_id: str) -> dict[str, Any]:
        self.refresh_readiness(project_id)
        plans = self.bb.conn.execute(
            "SELECT * FROM coordination_plans WHERE project_id=?"
            " ORDER BY updated_at DESC", (project_id,)).fetchall()
        tasks = self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE project_id=?"
            " ORDER BY plan_id, priority, created_at", (project_id,)).fetchall()
        plan_rows = [self._decode(row) for row in plans]
        task_rows = [self._decode(row) for row in tasks]
        for task in task_rows:
            task["verification"] = self._latest_verification(project_id, task["id"])
        objects = [self._with_object_artifacts(row) for row in self.bb.conn.execute(
            "SELECT * FROM coordination_objects WHERE project_id=?"
            " ORDER BY updated_at DESC", (project_id,)).fetchall()]
        conflicts = [dict(row) for row in self.bb.conn.execute(
            "SELECT * FROM coordination_conflicts WHERE project_id=?"
            " ORDER BY updated_at DESC", (project_id,)).fetchall()]
        by_plan: dict[str, list[dict[str, Any]]] = {}
        for task in task_rows:
            by_plan.setdefault(task["plan_id"], []).append(task)
        for plan in plan_rows:
            plan["tasks"] = by_plan.get(plan["id"], [])
            counts: dict[str, int] = {}
            for task in plan["tasks"]:
                counts[task["status"]] = counts.get(task["status"], 0) + 1
            plan["task_counts"] = counts
        active = next((p for p in plan_rows if p["status"] == "active"), None)
        return {
            "plans": plan_rows,
            "objects": objects,
            "conflicts": conflicts,
            "active_plan_id": active["id"] if active else None,
            "summary": {
                "plans": len(plan_rows),
                "tasks": len(task_rows),
                "running": sum(t["status"] == "running" for t in task_rows),
                "blocked": sum(t["status"] == "blocked" for t in task_rows),
                "completed": sum(t["status"] == "completed" for t in task_rows),
                "objects": len(objects),
                "conflicts": sum(c["status"] == "open" for c in conflicts),
            },
        }

    def refresh_readiness(self, project_id: str) -> None:
        """刷新计划节点状态，供协调页与编排器共用。

        M3（orchestrator-coordination-fusion，2026-10-04）扩展：
        ① **tasks → coordination 单向同步**——节点绑定 `task_id` 后按真实任务状态
           映射（done→completed / failed→failed / claimed·open→running）；
        ② 依赖派生 ready/blocked（原逻辑，仅对未派单/未终态行）；
        ③ active 计划全部节点 completed → 计划自动置 completed。
        严格单向：coordination 永不反向改 tasks。
        """
        rows = self.bb.conn.execute(
            "SELECT id,plan_id,status,depends_on,task_id FROM coordination_tasks"
            " WHERE project_id=?", (project_id,)).fetchall()
        now = _now()
        states = {row["id"]: row["status"] for row in rows}
        changes: list[tuple[str, str]] = []
        # ① 绑定任务单向同步（tasks → 节点）
        bound = {row["id"]: row["task_id"] for row in rows if row["task_id"]}
        task_status: dict[str, str] = {}
        if bound:
            ids = list(dict.fromkeys(bound.values()))
            marks = ",".join("?" * len(ids))
            for tr in self.bb.conn.execute(
                    f"SELECT id,status FROM tasks WHERE id IN ({marks})",
                    ids).fetchall():
                task_status[tr["id"]] = tr["status"]
        sync_map = {"done": "completed", "failed": "failed",
                    "claimed": "running", "open": "running"}
        for node_id, real_tid in bound.items():
            mapped = sync_map.get(task_status.get(real_tid, ""))
            if mapped and mapped != states[node_id]:
                states[node_id] = mapped
                changes.append((mapped, node_id))
        # ② 依赖派生（未派单/未终态行）
        for row in rows:
            cur = states[row["id"]]
            if cur in ("running", "completed", "failed"):
                continue
            try:
                deps = json.loads(row["depends_on"] or "[]")
            except (TypeError, ValueError):
                deps = []
            dep_states = [states.get(dep) for dep in deps]
            if any(state in ("failed", "blocked") for state in dep_states):
                target = "blocked"
            elif all(state == "completed" for state in dep_states):
                target = "ready"
            else:
                target = "blocked" if deps else "ready"
            if target != cur:
                states[row["id"]] = target
                changes.append((target, row["id"]))
        if changes:
            with self.bb._tx():
                self.bb.conn.executemany(
                    "UPDATE coordination_tasks SET status=?,updated_at=? WHERE id=?",
                    [(status, now, node_id) for status, node_id in changes])
        # ②-b M3（2026-10-04）：新就绪节点 → 发 plan.node_ready（编排器唤醒白名单
        # 消费，驱动「节点完成→自动派依赖节点」）。幂等——refresh 本身幂等，仅在
        # 状态**跃迁到 ready** 时发一次，重复调用不再触发。
        ready_ids = {node_id for status, node_id in changes if status == "ready"}
        if ready_ids:
            ready_by_plan: dict[str, list[str]] = {}
            for row in rows:
                if row["id"] in ready_ids:
                    ready_by_plan.setdefault(row["plan_id"], []).append(row["id"])
            for plan_id, node_ids in ready_by_plan.items():
                try:
                    self.bb.append_event(
                        project_id, "plan.node_ready",
                        {"plan_id": plan_id, "node_ids": node_ids},
                        author="coordination")
                except Exception:  # noqa: BLE001 —— 观测事件失败不影响状态刷新
                    log.exception("plan.node_ready 事件落库失败 plan=%s", plan_id)
        # ③ active 计划全部节点 completed → 自动完成（确定性，零 LLM）
        plan_status = {r["id"]: r["status"] for r in self.bb.conn.execute(
            "SELECT id,status FROM coordination_plans WHERE project_id=?",
            (project_id,)).fetchall()}
        by_plan: dict[str, list[str]] = {}
        for row in rows:
            by_plan.setdefault(row["plan_id"], []).append(states[row["id"]])
        done_plans = [pid for pid, sts in by_plan.items()
                      if plan_status.get(pid) == "active" and sts
                      and all(s == "completed" for s in sts)]
        if done_plans:
            with self.bb._tx():
                self.bb.conn.executemany(
                    "UPDATE coordination_plans SET status='completed',updated_at=?"
                    " WHERE id=?", [(now, pid) for pid in done_plans])

    def bind_task(self, project_id: str, task_id: str, real_task_id: str) -> dict[str, Any]:
        """把计划节点绑定到真实任务并置 running（编排器派单后回填，M3）。
        只写协调域，不改 tasks。节点不存在抛 LookupError。"""
        row = self.bb.conn.execute(
            "SELECT id FROM coordination_tasks WHERE id=? AND project_id=?",
            (task_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调任务不存在")
        now = _now()
        with self.bb._tx():
            self.bb.conn.execute(
                "UPDATE coordination_tasks SET task_id=?,status='running',updated_at=?"
                " WHERE id=? AND project_id=?",
                (real_task_id, now, task_id, project_id))
        return self._decode(self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE id=?", (task_id,)).fetchone())

    def unmet_dependencies(self, project_id: str, task_id: str) -> list[str]:
        """返回未满足（状态非 completed）的依赖节点 id 清单；空=就绪。
        节点不存在抛 LookupError。调用方应先用 refresh_readiness 刷新状态。"""
        row = self.bb.conn.execute(
            "SELECT depends_on FROM coordination_tasks WHERE id=? AND project_id=?",
            (task_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调任务不存在")
        try:
            deps = json.loads(row["depends_on"] or "[]")
        except (TypeError, ValueError):
            deps = []
        if not deps:
            return []
        marks = ",".join("?" * len(deps))
        rows = self.bb.conn.execute(
            f"SELECT id,status FROM coordination_tasks WHERE id IN ({marks})",
            deps).fetchall()
        done = {r["id"] for r in rows if r["status"] == "completed"}
        return [d for d in deps if d not in done]

    def plan_view(self, project_id: str) -> dict[str, Any] | None:
        """轻量：当前 active 计划 + 节点清单（供编排器态势注入，不载 objects/conflicts）。
        先 refresh_readiness 保证状态新鲜；无 active 计划返 None。"""
        self.refresh_readiness(project_id)
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_plans WHERE project_id=? AND status='active'"
            " ORDER BY updated_at DESC LIMIT 1", (project_id,)).fetchone()
        if row is None:
            return None
        plan = self._decode(row)
        plan["tasks"] = [self._decode(r) for r in self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE plan_id=?"
            " ORDER BY priority,created_at", (plan["id"],)).fetchall()]
        return plan

    def create_plan(self, project_id: str, name: str, objective: str = "") -> dict[str, Any]:
        name = name.strip()
        if not name:
            raise ValueError("计划名称不能为空")
        now = _now()
        plan_id = _id("coord")
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO coordination_plans"
                " (id,project_id,name,objective,status,config,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, project_id, name[:120], objective.strip()[:2000],
                 "draft", "{}", now, now))
        return self.get_plan(project_id, plan_id)

    def get_plan(self, project_id: str, plan_id: str) -> dict[str, Any]:
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_plans WHERE id=? AND project_id=?",
            (plan_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调计划不存在")
        plan = self._decode(row)
        plan["tasks"] = [self._decode(r) for r in self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE plan_id=?"
            " ORDER BY priority, created_at", (plan_id,)).fetchall()]
        for task in plan["tasks"]:
            task["verification"] = self._latest_verification(project_id, task["id"])
        return plan

    def get_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        """按节点 id 读取计划任务（编排器绑定真实 tasks 前置校验）。"""
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE id=? AND project_id=?",
            (task_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调任务不存在")
        item = self._decode(row)
        item["verification"] = self._latest_verification(project_id, task_id)
        return item

    def add_object(self, project_id: str, *, kind: str, name: str = "",
                   object_ref: str = "", data: dict[str, Any] | None = None,
                   source: str = "", confidence: float | int | None = None,
                   plan_id: str | None = None, task_id: str | None = None,
                   artifact_refs: list[str] | None = None) -> dict[str, Any]:
        """登记结构化协调产物，统一承载函数、字符串、xref、行为和证据。"""
        if kind not in OBJECT_KINDS:
            raise ValueError("kind 必须是 function/string/xref/behavior/evidence/artifact")
        if not str(name or object_ref).strip():
            raise ValueError("结构化对象必须有 name 或 object_ref")
        score = self._confidence(confidence)
        now = _now()
        object_id = _id("cobj")
        refs = list(dict.fromkeys(str(ref).strip() for ref in (artifact_refs or []) if str(ref).strip()))
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO coordination_objects"
                " (id,project_id,plan_id,task_id,kind,name,object_ref,data,source,confidence,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (object_id, project_id, plan_id, task_id, kind, str(name).strip()[:240],
                 str(object_ref).strip()[:500], json.dumps(data or {}, ensure_ascii=False),
                 str(source).strip()[:160], score, now, now))
            self.bb.conn.executemany(
                "INSERT INTO coordination_object_artifacts"
                " (object_id,artifact_ref,relation,created_at) VALUES(?,?,?,?)",
                [(object_id, ref, "supports", now) for ref in refs])
        return self.get_object(project_id, object_id)

    def get_object(self, project_id: str, object_id: str) -> dict[str, Any]:
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_objects WHERE id=? AND project_id=?",
            (object_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调对象不存在")
        return self._with_object_artifacts(row)

    def list_objects(self, project_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is not None and kind not in OBJECT_KINDS:
            raise ValueError("非法对象类型")
        if kind:
            rows = self.bb.conn.execute(
                "SELECT * FROM coordination_objects WHERE project_id=? AND kind=?"
                " ORDER BY updated_at DESC", (project_id, kind)).fetchall()
        else:
            rows = self.bb.conn.execute(
                "SELECT * FROM coordination_objects WHERE project_id=?"
                " ORDER BY updated_at DESC", (project_id,)).fetchall()
        return [self._with_object_artifacts(row) for row in rows]

    def add_conflict(self, project_id: str, *, left_object_id: str,
                     right_object_id: str, summary: str, field: str = "") -> dict[str, Any]:
        if left_object_id == right_object_id:
            raise ValueError("冲突对象不能相同")
        for object_id in (left_object_id, right_object_id):
            self.get_object(project_id, object_id)
        if not summary.strip():
            raise ValueError("冲突说明不能为空")
        now = _now()
        conflict_id = _id("cconf")
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO coordination_conflicts"
                " (id,project_id,left_object_id,right_object_id,field,summary,status,resolution,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,'open','',?,?)",
                (conflict_id, project_id, left_object_id, right_object_id,
                 field.strip()[:120], summary.strip()[:2000], now, now))
        return self.get_conflict(project_id, conflict_id)

    def get_conflict(self, project_id: str, conflict_id: str) -> dict[str, Any]:
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_conflicts WHERE id=? AND project_id=?",
            (conflict_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调冲突不存在")
        return dict(row)

    def update_conflict(self, project_id: str, conflict_id: str,
                        *, status: str, resolution: str = "") -> dict[str, Any]:
        if status not in CONFLICT_STATUSES:
            raise ValueError("非法冲突状态")
        self.get_conflict(project_id, conflict_id)
        now = _now()
        with self.bb._tx():
            self.bb.conn.execute(
                "UPDATE coordination_conflicts SET status=?,resolution=?,updated_at=?"
                " WHERE id=? AND project_id=?",
                (status, resolution.strip()[:2000], now, conflict_id, project_id))
        return self.get_conflict(project_id, conflict_id)

    def add_task(self, project_id: str, plan_id: str, title: str,
                 description: str = "", role: str = "", priority: int = 50,
                 depends_on: list[str] | None = None) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("任务标题不能为空")
        plan = self.get_plan(project_id, plan_id)
        deps = list(dict.fromkeys(str(x) for x in (depends_on or []) if str(x).strip()))
        known = {t["id"] for t in plan["tasks"]}
        unknown = [x for x in deps if x not in known]
        if unknown:
            raise ValueError("依赖任务不存在: " + ", ".join(unknown))
        now = _now()
        task_id = _id("ctask")
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO coordination_tasks"
                " (id,project_id,plan_id,title,description,role,status,priority,depends_on,evidence,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, project_id, plan_id, title[:160], description.strip()[:4000],
                 role.strip()[:80], "pending", max(0, min(100, int(priority))),
                 json.dumps(deps, ensure_ascii=False), "[]", now, now))
            self.bb.conn.execute(
                "UPDATE coordination_plans SET updated_at=? WHERE id=?", (now, plan_id))
        return self._decode(self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE id=?", (task_id,)).fetchone())

    def set_dependencies(self, project_id: str, task_id: str,
                         depends_on: list[str]) -> dict[str, Any]:
        """设置计划节点依赖；仅允许同计划节点且拒绝环，供 plan_work 使用。"""
        row = self.bb.conn.execute(
            "SELECT plan_id FROM coordination_tasks WHERE id=? AND project_id=?",
            (task_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调任务不存在")
        deps = list(dict.fromkeys(str(x) for x in depends_on if str(x).strip()))
        if task_id in deps:
            raise ValueError("计划节点不能依赖自身")
        known_rows = self.bb.conn.execute(
            "SELECT id,depends_on FROM coordination_tasks WHERE project_id=? AND plan_id=?",
            (project_id, row["plan_id"])).fetchall()
        known = {r["id"] for r in known_rows}
        if any(dep not in known for dep in deps):
            raise ValueError("依赖任务不存在")
        graph = {r["id"]: set(json.loads(r["depends_on"] or "[]"))
                 for r in known_rows}
        graph[task_id] = set(deps)
        def visit(node: str, stack: set[str], seen: set[str]) -> None:
            if node in stack:
                raise ValueError("计划依赖图存在循环")
            if node in seen:
                return
            stack.add(node)
            for dep in graph.get(node, set()):
                visit(dep, stack, seen)
            stack.remove(node); seen.add(node)
        seen: set[str] = set()
        for node in graph:
            visit(node, set(), seen)
        now = _now()
        with self.bb._tx():
            self.bb.conn.execute(
                "UPDATE coordination_tasks SET depends_on=?,updated_at=? WHERE id=? AND project_id=?",
                (json.dumps(deps, ensure_ascii=False), now, task_id, project_id))
        return self.get_task(project_id, task_id)

    def update_task(self, project_id: str, task_id: str,
                    *, status: str | None = None, role: str | None = None,
                    evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE id=? AND project_id=?",
            (task_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调任务不存在")
        if status is not None and status not in TASK_STATUSES:
            raise ValueError("非法任务状态")
        now = _now()
        sets: list[str] = ["updated_at=?"]
        args: list[Any] = [now]
        if status is not None:
            sets.append("status=?"); args.append(status)
        if role is not None:
            sets.append("role=?"); args.append(role.strip()[:80])
        if evidence is not None:
            sets.append("evidence=?"); args.append(json.dumps(evidence[:50], ensure_ascii=False))
        args.append(task_id)
        with self.bb._tx():
            self.bb.conn.execute(f"UPDATE coordination_tasks SET {', '.join(sets)} WHERE id=?", args)
        return self._decode(self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE id=?", (task_id,)).fetchone())

    def _create_followup_task(self, project_id: str, plan_id: str, title: str,
                              description: str) -> str:
        """创建验证补充任务；同一计划中存在未完成同名任务时复用。"""
        existing = self.bb.conn.execute(
            "SELECT id FROM coordination_tasks WHERE project_id=? AND plan_id=?"
            " AND title=? AND status NOT IN ('completed','failed') LIMIT 1",
            (project_id, plan_id, title)).fetchone()
        if existing:
            return existing["id"]
        now = _now()
        task_id = _id("ctask")
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO coordination_tasks"
                " (id,project_id,plan_id,title,description,role,status,priority,depends_on,evidence,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, project_id, plan_id, title[:160], description[:4000],
                 "verification", "ready", 20, "[]", "[]", now, now))
        return task_id

    def verify_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        """验证任务完成条件，并自动生成证据补充/冲突复核任务。"""
        row = self.bb.conn.execute(
            "SELECT * FROM coordination_tasks WHERE id=? AND project_id=?",
            (task_id, project_id)).fetchone()
        if row is None:
            raise LookupError("协调任务不存在")
        task = self._decode(row)
        objects = self.bb.conn.execute(
            "SELECT id,kind,name,object_ref FROM coordination_objects"
            " WHERE project_id=? AND task_id=?",
            (project_id, task_id)).fetchall()
        object_ids = {r["id"] for r in objects}
        evidence_objects = [r for r in objects if r["kind"] in ("evidence", "artifact")]
        task_evidence = task.get("evidence") if isinstance(task.get("evidence"), list) else []
        issues: list[dict[str, Any]] = []
        followups: list[str] = []
        if not evidence_objects and not task_evidence:
            issues.append({"kind": "evidence", "message": "任务没有关联证据或产物"})
            followups.append(self._create_followup_task(
                project_id, task["plan_id"], f"补充证据：{task['title']}",
                f"为任务「{task['title']}」补充可核验的证据对象或产物引用，再提交验证。"))
        if object_ids:
            marks = ",".join("?" * len(object_ids))
            conflict_rows = self.bb.conn.execute(
                "SELECT id,summary,left_object_id,right_object_id FROM coordination_conflicts"
                f" WHERE project_id=? AND status='open' AND"
                f" (left_object_id IN ({marks}) OR right_object_id IN ({marks}))",
                (project_id, *object_ids, *object_ids)).fetchall()
        else:
            conflict_rows = []
        for conflict in conflict_rows:
            issues.append({"kind": "conflict", "conflict_id": conflict["id"],
                           "message": conflict["summary"]})
            followups.append(self._create_followup_task(
                project_id, task["plan_id"], f"复核冲突：{conflict['summary']}",
                f"复核协调对象冲突 {conflict['id']}，补充证据后解决或驳回冲突。"))
        status = "passed" if not issues else (
            "conflict" if any(i["kind"] == "conflict" for i in issues)
            else "needs_evidence")
        score = 1.0 if status == "passed" else max(0.0, 1.0 - 0.5 * len(issues))
        checked = _now()
        verification_id = _id("cver")
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO coordination_verifications"
                " (id,project_id,plan_id,task_id,status,score,issues,followup_task_ids,checked_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (verification_id, project_id, task["plan_id"], task_id, status, score,
                 json.dumps(issues, ensure_ascii=False), json.dumps(followups), checked))
            if status == "passed":
                self.bb.conn.execute(
                    "UPDATE coordination_tasks SET status='completed',updated_at=?"
                    " WHERE id=? AND project_id=?", (checked, task_id, project_id))
        return {
            "id": verification_id, "project_id": project_id,
            "plan_id": task["plan_id"], "task_id": task_id,
            "status": status, "score": score, "issues": issues,
            "followup_task_ids": followups, "checked_at": checked,
            "task_status": "completed" if status == "passed" else task["status"],
        }

    def verify_plan(self, project_id: str, plan_id: str) -> dict[str, Any]:
        self.get_plan(project_id, plan_id)
        task_ids = [r["id"] for r in self.bb.conn.execute(
            "SELECT id FROM coordination_tasks WHERE project_id=? AND plan_id=?"
            " AND role != 'verification' ORDER BY priority,created_at",
            (project_id, plan_id)).fetchall()]
        results = [self.verify_task(project_id, task_id) for task_id in task_ids]
        passed = bool(results) and all(r["status"] == "passed" for r in results)
        if passed:
            self.set_plan_status(project_id, plan_id, "completed")
        return {"plan_id": plan_id, "status": "completed" if passed else "incomplete",
                "passed": sum(r["status"] == "passed" for r in results),
                "total": len(results), "results": results}

    def set_plan_status(self, project_id: str, plan_id: str, status: str) -> dict[str, Any]:
        if status not in PLAN_STATUSES:
            raise ValueError("非法计划状态")
        self.get_plan(project_id, plan_id)
        now = _now()
        with self.bb._tx():
            self.bb.conn.execute(
                "UPDATE coordination_plans SET status=?,updated_at=? WHERE id=? AND project_id=?",
                (status, now, plan_id, project_id))
        return self.get_plan(project_id, plan_id)
