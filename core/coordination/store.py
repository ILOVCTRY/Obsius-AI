"""多智能体协调的独立持久化边界。

协调计划与现有 chat/agent 线程解耦。当前版本提供计划与任务 DAG 的 CRUD，
后续调度器可以在不改工作台运行时的前提下消费这些任务。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


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
        """根据依赖刷新待执行状态，供协调页和未来调度器共用。"""
        rows = self.bb.conn.execute(
            "SELECT id,status,depends_on FROM coordination_tasks WHERE project_id=?",
            (project_id,)).fetchall()
        states = {row["id"]: row["status"] for row in rows}
        now = _now()
        changes: list[tuple[str, str]] = []
        for row in rows:
            if row["status"] in ("running", "completed", "failed"):
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
            if target != row["status"]:
                changes.append((target, row["id"]))
        if changes:
            with self.bb._tx():
                self.bb.conn.executemany(
                    "UPDATE coordination_tasks SET status=?,updated_at=? WHERE id=?",
                    [(status, now, task_id) for status, task_id in changes])

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
