"""Team direct execution 持久化。

Team/Member/Run 是独立于旧 tasks 与 coordination DAG 的运行时对象。
本模块只写 Team 相关表；不创建旧任务，不调用 TaskQueue。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from core.autonomy import autonomy_of
from core.blackboard.store import new_id, now
from core.runtime.policy import RUNTIME_LEVELS, VALID_THREAT_CLASSES, allowed_runtimes
from core.skills.experts import expert_exists

log = logging.getLogger(__name__)

TEAM_STATUSES = ("draft", "ready", "starting", "running", "partial_failed", "cancelling", "completed", "cancelled", "failed")
MEMBER_STATUSES = ("active", "pending", "creating", "running", "completed", "failed", "cancelled")
TERMINAL_RUN_STATUSES = {"completed", "cancelled", "failed"}


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for key in ("config", "roe_snapshot"):
        if key in item:
            try:
                item[key] = json.loads(item[key] or "{}")
            except (TypeError, ValueError):
                item[key] = {}
    return item


def _revision(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()[:24]


class TeamStore:
    """Team 领域 SQLite 适配器。唯一写入 Team/Run/audit 表。"""

    def __init__(self, bb, *, packs_root: str | None = None):
        self.bb = bb
        self.packs_root = packs_root

    def _emit(self, project_id: str, kind: str, payload: dict[str, Any], *, author: str = "orchestrator") -> None:
        """团队生命周期事件（2026-10-06）：指挥页签按 team.* 事件在对话流里渲染团队卡。

        **必须在 `_tx()` 之外调用**（`append_event` 自带事务，`_tx` 不可重入）。
        事件留痕失败不影响业务写路径（与项目内既有事件钩子同纪律）。"""
        try:
            self.bb.append_event(project_id, kind, payload, author=author)
        except Exception:  # noqa: BLE001 - 事件失败只记日志，不阻断团队写路径
            log.exception("team 事件落库失败 kind=%s team=%s", kind, payload.get("team_id"))

    def _run_ref(self, run_member_id: str) -> dict[str, Any] | None:
        """run_member → 事件所需的 project/team/run/member 上下文（读，不写）。"""
        row = self.bb.conn.execute(
            "SELECT rm.member_key AS member_key, rm.run_id AS run_id, r.team_id AS team_id,"
            " r.project_id AS project_id, t.name AS team_name"
            " FROM team_run_members rm"
            " JOIN team_runs r ON r.id=rm.run_id"
            " JOIN teams t ON t.id=r.team_id WHERE rm.id=?",
            (run_member_id,),
        ).fetchone()
        return dict(row) if row else None

    def _assert_team(self, project_id: str, team_id: str) -> None:
        row = self.bb.conn.execute(
            "SELECT 1 FROM teams WHERE id=? AND project_id=?", (team_id, project_id)
        ).fetchone()
        if row is None:
            raise LookupError(f"Team 不存在: {team_id}")

    def _team(self, project_id: str, team_id: str) -> dict[str, Any]:
        row = self.bb.conn.execute(
            "SELECT * FROM teams WHERE id=? AND project_id=?", (team_id, project_id)
        ).fetchone()
        if row is None:
            raise LookupError(f"Team 不存在: {team_id}")
        item = _decode(row) or {}
        item["members"] = self.list_members(project_id, team_id)
        return item

    def get_team(self, project_id: str, team_id: str) -> dict[str, Any]:
        return self._team(project_id, team_id)

    def list_teams(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.bb.conn.execute(
            "SELECT id FROM teams WHERE project_id=? ORDER BY updated_at DESC,id", (project_id,)
        ).fetchall()
        return [self._team(project_id, row["id"]) for row in rows]

    def list_members(self, project_id: str, team_id: str) -> list[dict[str, Any]]:
        self._assert_team(project_id, team_id)
        return [dict(row) for row in self.bb.conn.execute(
            "SELECT * FROM team_members WHERE team_id=? ORDER BY created_at,id", (team_id,)
        ).fetchall()]

    @staticmethod
    def _normalize_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(members, list) or not members:
            raise ValueError("Team 至少需要一个成员")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(members):
            if not isinstance(raw, dict):
                raise ValueError(f"成员 {index + 1} 必须是对象")
            key = str(raw.get("member_key") or raw.get("id") or "").strip()
            if not key:
                raise ValueError(f"成员 {index + 1} 缺少 member_key")
            if key in seen:
                raise ValueError(f"成员 key 重复: {key}")
            seen.add(key)
            runtime = str(raw.get("runtime") or "").strip().lower()
            threat = str(raw.get("threat_class") or "trusted").strip() or "trusted"
            if runtime and runtime not in RUNTIME_LEVELS:
                raise ValueError(f"非法 runtime: {runtime}")
            if threat not in VALID_THREAT_CLASSES:
                raise ValueError(f"非法 threat_class: {threat}")
            max_steps = raw.get("max_steps")
            if max_steps is not None and (isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0):
                raise ValueError("max_steps 必须是正整数")
            result.append({
                "member_key": key[:120],
                "label": str(raw.get("label") or raw.get("name") or key).strip()[:160],
                "responsibility": str(raw.get("responsibility") or raw.get("description") or raw.get("objective") or "").strip()[:4000],
                "role": str(raw.get("role") or "").strip()[:120],
                "provider": str(raw.get("provider") or "").strip()[:120],
                "model": str(raw.get("model") or "").strip()[:240],
                "runtime": runtime,
                "threat_class": threat,
                "max_steps": max_steps,
            })
        return result

    def _insert_members(self, team_id: str, members: list[dict[str, Any]], ts: str) -> None:
        for member in members:
            self.bb.conn.execute(
                "INSERT INTO team_members(id,team_id,member_key,label,responsibility,role,provider,model,runtime,threat_class,max_steps,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("member"), team_id, member["member_key"], member["label"], member["responsibility"], member["role"], member["provider"], member["model"], member["runtime"], member["threat_class"], member["max_steps"], "active", ts, ts),
            )

    def create_team(self, project_id: str, *, name: str, goal_text: str = "", members: list[dict[str, Any]] | None = None, config: dict[str, Any] | None = None, created_by: str = "human") -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Team 名称不能为空")
        roster = self._normalize_members(members or [])
        ts = now()
        team_id = new_id("team")
        with self.bb._tx():
            if self.bb.conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise LookupError(f"项目不存在: {project_id}")
            self.bb.conn.execute(
                "INSERT INTO teams(id,project_id,name,goal_text,status,created_by,revision,config,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (team_id, project_id, name, str(goal_text or "").strip(), "draft", created_by, 1, _dump(config or {}), ts, ts),
            )
            self._insert_members(team_id, roster, ts)
        self._emit(project_id, "team.created", {
            "team_id": team_id, "name": name, "goal_text": str(goal_text or "").strip(),
            "status": "draft", "member_count": len(roster),
            "members": [{"member_key": m["member_key"], "label": m["label"], "role": m["role"]} for m in roster],
        }, author=created_by)
        return self._team(project_id, team_id)

    def update_team(self, project_id: str, team_id: str, *, name: str | None = None, goal_text: str | None = None, config: dict[str, Any] | None = None, members: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        current = self._team(project_id, team_id)
        if current["status"] not in {"draft", "ready"}:
            raise ValueError("仅 draft/ready Team 可编辑")
        roster = self._normalize_members(members) if members is not None else None
        sets: list[str] = []
        args: list[Any] = []
        if name is not None:
            value = str(name).strip()
            if not value:
                raise ValueError("Team 名称不能为空")
            sets.append("name=?"); args.append(value)
        if goal_text is not None:
            sets.append("goal_text=?"); args.append(str(goal_text).strip())
        if config is not None:
            sets.append("config=?"); args.append(_dump(config))
        ts = now()
        with self.bb._tx():
            if roster is not None:
                self.bb.conn.execute("DELETE FROM team_members WHERE team_id=?", (team_id,))
                self._insert_members(team_id, roster, ts)
            if sets or roster is not None:
                sets.extend(("revision=revision+1", "updated_at=?")); args.append(ts)
                args.extend((team_id, project_id))
                self.bb.conn.execute(f"UPDATE teams SET {', '.join(sets)} WHERE id=? AND project_id=?", args)
        updated = self._team(project_id, team_id)
        self._emit(project_id, "team.updated", {
            "team_id": team_id, "name": updated["name"], "goal_text": updated["goal_text"],
            "status": updated["status"], "revision": updated["revision"],
            "member_count": len(updated["members"]),
        }, author="human")
        return updated

    def _project_config(self, project_id: str) -> tuple[dict[str, Any], str]:
        row = self.bb.conn.execute("SELECT config,track FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise LookupError(f"项目不存在: {project_id}")
        try:
            config = json.loads(row["config"] or "{}")
        except (TypeError, ValueError):
            config = {}
        return (config if isinstance(config, dict) else {}), str(row["track"] or "")

    def preflight(self, project_id: str, team_id: str) -> dict[str, Any]:
        team = self._team(project_id, team_id)
        config, track = self._project_config(project_id)
        members = team["members"]
        blockers: list[dict[str, str]] = []
        if team["status"] not in {"draft", "ready"}:
            blockers.append({"code": "team_state", "message": f"Team 当前状态不可启动: {team['status']}"})
        autonomy = autonomy_of(config, track=track)
        if autonomy["paused"]:
            blockers.append({"code": "project_paused", "message": "项目当前已暂停"})
        cap = autonomy["sessions_cap"]
        running = self.bb.conn.execute("SELECT COUNT(*) FROM sessions WHERE project_id=? AND status='running'", (project_id,)).fetchone()[0]
        if running + len(members) > cap:
            blockers.append({"code": "session_cap", "message": f"会话上限不足: running={running}, requested={len(members)}, cap={cap}"})
        for member in members:
            if member["role"] and self.packs_root and not expert_exists(self.packs_root, member["role"], track):
                blockers.append({"code": "role_unavailable", "message": f"成员 {member['member_key']} 的角色不可用: {member['role']}"})
            if member["runtime"] and member["runtime"] not in allowed_runtimes(member["threat_class"]):
                blockers.append({"code": "runtime_threat_mismatch", "message": f"成员 {member['member_key']} 的 runtime 与 threat_class 不匹配"})
        safety = {"track": track, "roe": config.get("redteam_roe") or {}, "autonomy": autonomy}
        revision = _revision({"team": {k: team[k] for k in ("id", "name", "goal_text", "revision", "config")}, "members": members, "safety": safety, "capacity": {"running": running, "cap": cap}})
        return {"team": team, "members": members, "revision": revision, "safety": safety, "capacity": {"running": running, "requested": len(members), "cap": cap}, "blockers": blockers}

    def create_run_and_members(self, project_id: str, team_id: str, *, revision: str, confirmations: dict[str, bool]) -> dict[str, Any]:
        if not all(confirmations.get(key) is True for key in ("members", "goal", "safety", "execution")):
            raise ValueError("请确认成员、共同目标、安全边界及执行")
        pf = self.preflight(project_id, team_id)
        if revision != pf["revision"]:
            raise RuntimeError("Team 在确认后已变化，请重新预检")
        if pf["blockers"]:
            raise ValueError("Team 未通过执行前检查")
        ts = now(); run_id = new_id("run")
        reused_run_id: str | None = None
        with self.bb._tx():
            active = self.bb.conn.execute("SELECT id FROM team_runs WHERE team_id=? AND status IN ('starting','running','partial_failed','cancelling') ORDER BY created_at DESC LIMIT 1", (team_id,)).fetchone()
            if active:
                # BEGIN IMMEDIATE 下检查，避免并发 start 创建两个 Run。
                reused_run_id = active["id"]
            else:
                self.bb.conn.execute("INSERT INTO team_runs(id,project_id,team_id,status,confirmation_revision,created_at,updated_at,started_at) VALUES(?,?,?,?,?,?,?,?)", (run_id, project_id, team_id, "starting", revision, ts, ts, ts))
                for member in pf["members"]:
                    execution_id = new_id("exec")
                    self.bb.conn.execute("INSERT INTO team_run_members(id,run_id,member_id,member_key,objective,role,provider,model,runtime,threat_class,max_steps,status,execution_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id("run-member"), run_id, member["id"], member["member_key"], member["responsibility"], member["role"], member["provider"], member["model"], member["runtime"], member["threat_class"], member["max_steps"], "pending", execution_id, ts, ts))
                    self.bb.conn.execute("INSERT INTO execution_audits(execution_id,project_id,source,team_id,run_id,member_id,objective,role,runtime,threat_class,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (execution_id, project_id, "team_direct", team_id, run_id, member["id"], member["responsibility"], member["role"], member["runtime"], member["threat_class"], "pending", ts, ts))
                self.bb.conn.execute("UPDATE teams SET status='starting',updated_at=? WHERE id=?", (ts, team_id))
        if reused_run_id is not None:
            return self.get_run(project_id, team_id, reused_run_id)
        self._emit(project_id, "team.run.started", {
            "team_id": team_id, "team_name": pf["team"]["name"], "run_id": run_id,
            "status": "starting", "member_count": len(pf["members"]),
            "members": [{"member_key": m["member_key"], "label": m["label"], "role": m["role"],
                         "objective": m["responsibility"]} for m in pf["members"]],
        }, author="human")
        return self.get_run(project_id, team_id, run_id)

    def get_run(self, project_id: str, team_id: str, run_id: str) -> dict[str, Any]:
        row = self.bb.conn.execute("SELECT * FROM team_runs WHERE id=? AND project_id=? AND team_id=?", (run_id, project_id, team_id)).fetchone()
        if row is None:
            raise LookupError(f"Team Run 不存在: {run_id}")
        result = _decode(row) or {}
        result["members"] = [dict(r) for r in self.bb.conn.execute("SELECT * FROM team_run_members WHERE run_id=? ORDER BY created_at,id", (run_id,)).fetchall()]
        return result

    def list_runs(self, project_id: str, team_id: str) -> list[dict[str, Any]]:
        """团队的历史 Run（新→旧）。运行报告页据此取最新一次执行。"""
        self._assert_team(project_id, team_id)
        rows = self.bb.conn.execute(
            "SELECT id FROM team_runs WHERE project_id=? AND team_id=? ORDER BY created_at DESC,id DESC",
            (project_id, team_id),
        ).fetchall()
        return [self.get_run(project_id, team_id, row["id"]) for row in rows]

    def claim_member(self, run_member_id: str) -> dict[str, Any] | None:
        with self.bb._tx():
            cur = self.bb.conn.execute("UPDATE team_run_members SET status='creating',updated_at=? WHERE id=? AND status='pending'", (now(), run_member_id))
            if cur.rowcount != 1:
                return None
            return dict(self.bb.conn.execute("SELECT * FROM team_run_members WHERE id=?", (run_member_id,)).fetchone())

    def attach_session(self, run_member_id: str, session_id: str) -> None:
        ts = now()
        with self.bb._tx():
            self.bb.conn.execute("UPDATE team_run_members SET session_id=?,status='running',started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND session_id IS NULL", (session_id, ts, ts, run_member_id))
            row = self.bb.conn.execute("SELECT execution_id FROM team_run_members WHERE id=?", (run_member_id,)).fetchone()
            if row:
                self.bb.conn.execute("UPDATE execution_audits SET session_id=?,status='running',updated_at=? WHERE execution_id=?", (session_id, ts, row["execution_id"]))
        ref = self._run_ref(run_member_id)
        if ref:
            self._emit(ref["project_id"], "team.member.updated", {
                "team_id": ref["team_id"], "team_name": ref["team_name"], "run_id": ref["run_id"],
                "member_id": run_member_id, "member_key": ref["member_key"],
                "status": "running", "session_id": session_id,
            })

    def mark_member(self, run_member_id: str, status: str, *, error: str = "", outcome: str = "") -> None:
        if status not in MEMBER_STATUSES:
            raise ValueError(f"非法成员状态: {status}")
        ts = now()
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT execution_id FROM team_run_members WHERE id=?", (run_member_id,)).fetchone()
            self.bb.conn.execute("UPDATE team_run_members SET status=?,error=?,finished_at=CASE WHEN ? IN ('completed','failed','cancelled') THEN COALESCE(finished_at,?) ELSE finished_at END,updated_at=? WHERE id=?", (status, error[:1000], status, ts, ts, run_member_id))
            if row:
                self.bb.conn.execute("UPDATE execution_audits SET status=?,outcome=?,updated_at=? WHERE execution_id=?", (status, outcome[:2000], ts, row["execution_id"]))
        ref = self._run_ref(run_member_id)
        if ref:
            self._emit(ref["project_id"], "team.member.updated", {
                "team_id": ref["team_id"], "team_name": ref["team_name"], "run_id": ref["run_id"],
                "member_id": run_member_id, "member_key": ref["member_key"],
                "status": status, "error": error[:1000],
            })

    def reconcile_run(self, project_id: str, team_id: str, run_id: str) -> dict[str, Any]:
        run = self.get_run(project_id, team_id, run_id)
        statuses = [member["status"] for member in run["members"]]
        if any(status in {"pending", "creating", "running"} for status in statuses):
            target = "running" if any(status in {"creating", "running"} for status in statuses) else "starting"
        elif all(status == "cancelled" for status in statuses):
            target = "cancelled"
        elif any(status == "failed" for status in statuses):
            target = "partial_failed" if any(status == "completed" for status in statuses) else "failed"
        else:
            target = "completed"
        ts = now()
        with self.bb._tx():
            self.bb.conn.execute("UPDATE team_runs SET status=?,finished_at=CASE WHEN ? IN ('completed','cancelled','failed') THEN COALESCE(finished_at,?) ELSE finished_at END,updated_at=? WHERE id=?", (target, target, ts, ts, run_id))
            self.bb.conn.execute("UPDATE teams SET status=?,updated_at=? WHERE id=?", (target, ts, team_id))
        if target in TERMINAL_RUN_STATUSES:
            name_row = self.bb.conn.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()
            self._emit(project_id, "team.run.finished", {
                "team_id": team_id, "team_name": str(name_row["name"]) if name_row else "",
                "run_id": run_id, "status": target, "member_count": len(run["members"]),
            })
        return self.get_run(project_id, team_id, run_id)

    def cancel_run(self, project_id: str, team_id: str, run_id: str, reason: str = "") -> dict[str, Any]:
        run = self.get_run(project_id, team_id, run_id)
        if run["status"] in TERMINAL_RUN_STATUSES:
            return run
        ts = now()
        with self.bb._tx():
            self.bb.conn.execute("UPDATE team_runs SET status='cancelling',cancel_reason=?,updated_at=? WHERE id=?", (reason[:1000], ts, run_id))
            self.bb.conn.execute("UPDATE team_run_members SET status='cancelled',error=CASE WHEN status IN ('pending','creating') THEN 'cancelled before start' ELSE error END,finished_at=CASE WHEN status IN ('pending','creating') THEN ? ELSE finished_at END,updated_at=? WHERE run_id=? AND status IN ('pending','creating')", (ts, ts, run_id))
            self.bb.conn.execute("UPDATE execution_audits SET status='cancelled',outcome=?,updated_at=? WHERE run_id=? AND status='pending'", (reason[:1000], ts, run_id))
        return self.reconcile_run(project_id, team_id, run_id)
