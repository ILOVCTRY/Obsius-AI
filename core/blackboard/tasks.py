"""任务队列（DESIGN.md §6）——多会话协调的核心原语。

关键规则：
- 认领互斥：noise_budget != passive 的任务带 conflict_keys（如 ["ip:1.2.3.4"]），
  同项目内 active 任务 conflict_keys 有交集则拒绝认领（§6.2 噪声预算 / WAF 对策）。
  passive 任务（逆向分析、被动收集）可任意共享。
- lease 租约：认领带 TTL，过期由 expire_leases() 回收为 open，防会话挂死占坑。
- 认领/完成/失败均落事件流，Orchestrator 消费这些事件做监控派生。
"""

import json
import re
from typing import Any, Iterable

from core.blackboard.store import Blackboard, new_id, now
from core.skills.taxonomy import GENERIC_TASK_TYPE

# 服务端自动抽取层：objective 正文里的 finding id（id 格式固定，零纪律也不漏）
FINDING_REF_RE = re.compile(r"find-[0-9a-f]{12}")


class ClaimError(Exception):
    """认领失败（已被认领 / active 互斥冲突）。"""


class TaskTypeError(ValueError):
    """task_type 不在场景轨注册表（task_types.yaml）内。拼错静默饿死的对侧防线。"""


def _check_task_type(task_type: str, allowed_types: Iterable[str] | None) -> None:
    """allowed_types 为轨注册表；None = 调用方未接线（不校验）。generic 恒合法。"""
    if (
        allowed_types is not None
        and task_type != GENERIC_TASK_TYPE
        and task_type not in allowed_types
    ):
        raise TaskTypeError(
            f"未注册的 task_type: {task_type!r}；请先在场景轨 task_types.yaml 注册"
        )


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class TaskQueue:
    def __init__(self, bb: Blackboard):
        self.bb = bb

    # ---------- 发布 ----------

    def publish(
        self,
        project_id: str,
        objective: str,
        scope: str = "",
        task_type: str = "generic",
        noise_budget: str = "passive",
        priority: int = 2,
        conflict_keys: list[str] | None = None,
        parent_id: str | None = None,
        created_by: str = "human",
        allowed_types: Iterable[str] | None = None,
        refs: list[str] | None = None,
    ) -> str:
        """发布任务。created_by: human / orchestrator / session-x。

        conflict_keys 示例：渗透 active 任务填 ["ip:1.2.3.4", "domain:x.com"]——
        携带同一 IP 键的 active 任务彼此互斥；passive 任务忽略此字段。
        allowed_types：轨 task_types.yaml 注册表（由调用方按项目 track 注入），
        未知 task_type 直接拒收——拼写错误不再静默饿死（DESIGN.md §4.5.5）。
        refs：任务依据的 finding id（显式层，orch 工具/人发任务可填）；
        服务端同时从 objective 正文自动抽取 find- 标识（自动层），并集去重后
        入 context_refs——撤回传播据此反向定位（DESIGN.md §6.7 的 1.6）。
        """
        if noise_budget not in {"passive", "low", "medium", "high"}:
            raise ValueError(f"非法 noise_budget: {noise_budget}")
        if noise_budget != "passive" and not conflict_keys:
            raise ValueError("非 passive 任务必须提供 conflict_keys（active 互斥的依据）")
        _check_task_type(task_type, allowed_types)
        context_refs = sorted({*(refs or []), *FINDING_REF_RE.findall(objective)})
        task_id = new_id("task")
        ts = now()
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO tasks(id,project_id,scope,task_type,objective,status,priority,"
                "noise_budget,conflict_keys,parent_id,created_by,context_refs,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    project_id,
                    scope,
                    task_type,
                    objective,
                    priority,
                    noise_budget,
                    json.dumps(conflict_keys or [], ensure_ascii=False),
                    parent_id,
                    created_by,
                    json.dumps(context_refs, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
        self.bb.append_event(
            project_id,
            "task.published",
            {"task_id": task_id, "objective": objective, "task_type": task_type},
            author=created_by,
        )
        return task_id

    # ---------- 认领 ----------

    def claim(self, task_id: str, session_id: str, lease_minutes: int = 30) -> None:
        """认领任务。失败抛 ClaimError（调用方据此让 Agent 改道，见 §6.2 硬规则）。"""
        from datetime import datetime, timedelta, timezone

        lease_until = (
            datetime.now(timezone.utc) + timedelta(minutes=lease_minutes)
        ).isoformat(timespec="seconds")
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ClaimError(f"任务不存在: {task_id}")
            if row["status"] != "open":
                raise ClaimError(f"任务 {task_id} 状态为 {row['status']}，不可认领")
            if row["noise_budget"] != "passive":
                keys = set(_loads(row["conflict_keys"], []))
                others = self.bb.conn.execute(
                    "SELECT id, conflict_keys FROM tasks"
                    " WHERE project_id=? AND status='claimed' AND noise_budget!='passive' AND id!=?",
                    (row["project_id"], task_id),
                ).fetchall()
                for other in others:
                    if keys & set(_loads(other["conflict_keys"], [])):
                        raise ClaimError(
                            f"认领被拒：任务 {task_id} 与 {other['id']} 的 conflict_keys 交叠"
                            f"（同目标 active 任务互斥，见 DESIGN.md §6.2）"
                        )
            self.bb.conn.execute(
                "UPDATE tasks SET status='claimed', claimed_by=?, lease_until=?, updated_at=?"
                " WHERE id=?",
                (session_id, lease_until, now(), task_id),
            )
        self.bb.append_event(
            row["project_id"],
            "task.claimed",
            {"task_id": task_id, "session_id": session_id, "lease_until": lease_until},
            session_id=session_id,
        )

    _NOISE_RANK = {"passive": 0, "low": 1, "medium": 2, "high": 3}

    def claim_next(
        self,
        project_id: str,
        session_id: str,
        allowed_task_types: list[str] | None = None,
        lease_minutes: int = 30,
        max_noise: str | None = None,
    ) -> str | None:
        """Worker 循环入口：按优先级（P0 最高）认领第一个 open 任务；无匹配返回 None。

        allowed_task_types 即角色过滤（§6.6：角色只能看到/认领 task_types 白名单）；
        max_noise 为角色 default_noise 上限——高于角色噪声预算的任务不可认领
        （default_noise 死字段修复后的真正生效点）。
        """
        sql = "SELECT id FROM tasks WHERE project_id=? AND status='open'"
        params: list[Any] = [project_id]
        if allowed_task_types is not None:
            sql += f" AND task_type IN ({','.join('?' * len(allowed_task_types))})"
            params.extend(allowed_task_types)
        if max_noise in self._NOISE_RANK:
            sql += (
                " AND CASE noise_budget WHEN 'passive' THEN 0 WHEN 'low' THEN 1"
                " WHEN 'medium' THEN 2 WHEN 'high' THEN 3 END <= ?"
            )
            params.append(self._NOISE_RANK[max_noise])
        sql += " ORDER BY priority, created_at"
        for row in self.bb.conn.execute(sql, params).fetchall():
            try:
                self.claim(row["id"], session_id, lease_minutes)
                return row["id"]
            except ClaimError:
                continue  # 被别人抢了/互斥，试下一个
        return None

    # ---------- 收尾 ----------

    def complete(self, task_id: str, session_id: str, result_note: str = "") -> None:
        self._finish(task_id, session_id, "done", result_note)

    def fail(self, task_id: str, session_id: str, result_note: str = "") -> None:
        self._finish(task_id, session_id, "failed", result_note)

    def _finish(self, task_id: str, session_id: str, status: str, result_note: str) -> None:
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["claimed_by"] != session_id:
                raise ClaimError(f"任务 {task_id} 不由会话 {session_id} 持有，无权收尾")
            self.bb.conn.execute(
                "UPDATE tasks SET status=?, result_note=?, lease_until=NULL, updated_at=?"
                " WHERE id=?",
                (status, result_note, now(), task_id),
            )
            stale_refs = _loads(row["stale_refs"], [])
        self.bb.append_event(
            row["project_id"],
            f"task.{status}",
            {"task_id": task_id, "session_id": session_id, "note": result_note},
            session_id=session_id,
        )
        if status == "done" and stale_refs:
            # 撤回传播复核钩子：带着被推翻依据完成的任务必须显事件（L0 给人看，
            # L1+ 主代理下轮 tick 可据此派生复查任务）
            self.bb.append_event(
                row["project_id"], "task.basis_stale_done",
                {"task_id": task_id, "session_id": session_id,
                 "stale_refs": stale_refs, "note": result_note},
                session_id=session_id, author=session_id)

    def add_stale_ref(self, task_id: str, ref_id: str) -> bool:
        """撤回传播挂标：把被推翻 finding 幂等并入任务 stale_refs。
        仅 open/claimed 可挂（done/failed 是战果，只给作者发私信，不改行）。
        返回是否实际新增。"""
        with self.bb._tx():
            row = self.bb.conn.execute(
                "SELECT status, stale_refs FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["status"] not in ("open", "claimed"):
                return False
            refs = _loads(row["stale_refs"], [])
            if ref_id in refs:
                return False
            refs.append(ref_id)
            self.bb.conn.execute(
                "UPDATE tasks SET stale_refs=?, updated_at=? WHERE id=?",
                (json.dumps(refs, ensure_ascii=False), now(), task_id))
        return True

    # ---------- 计划（A2 先规划后动手，DESIGN §6.1） ----------

    _PLAN_STATUSES = {"todo", "doing", "done", "blocked"}
    _PLAN_STEP_ID_RE = re.compile(r"^p(\d+)$")

    def set_plan(
        self,
        task_id: str,
        session_id: str,
        steps: list[dict[str, Any]],
        rev_reason: str = "",
    ) -> list[dict[str, Any]]:
        """写/修订认领任务的执行计划（仅认领者，claimed 态）。

        steps 元素 {"title": str, "id"?: "p<n>"}：带既有 id 的步保留原状态/时间戳
        （标题可更新），新步服务端发号 p<n+1>；缺省的旧步丢弃（doing 被删则自然
        无 doing，需重新 task_step）。首次写落 task.plan_set，再调落
        task.plan_revised（含旧计划快照+修订原因）。返回新计划。
        """
        if not isinstance(steps, list) or not steps:
            raise ValueError("计划至少包含一个步骤")
        if len(steps) > 20:
            raise ValueError("计划步骤最多 20 个")
        ts = now()
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["status"] != "claimed" or row["claimed_by"] != session_id:
                raise ClaimError(f"任务 {task_id} 不由会话 {session_id} 持有，无权写计划")
            old_plan = _loads(row["plan"], [])
            old_by_id = {s["id"]: s for s in old_plan if isinstance(s, dict) and "id" in s}
            max_n = 0
            for s in old_plan:
                m = self._PLAN_STEP_ID_RE.match(str(s.get("id", "")))
                if m:
                    max_n = max(max_n, int(m.group(1)))
            new_plan: list[dict[str, Any]] = []
            for item in steps:
                if not isinstance(item, dict):
                    raise ValueError("计划步必须是对象")
                title = str(item.get("title", "")).strip()
                if not title:
                    raise ValueError("计划步 title 不能为空")
                step_id = item.get("id")
                if isinstance(step_id, str) and step_id in old_by_id:
                    kept = dict(old_by_id[step_id])
                    kept["title"] = title
                    new_plan.append(kept)
                else:
                    max_n += 1
                    new_plan.append({"id": f"p{max_n}", "title": title,
                                     "status": "todo", "ts": ts})
            self.bb.conn.execute(
                "UPDATE tasks SET plan=?, updated_at=? WHERE id=?",
                (json.dumps(new_plan, ensure_ascii=False), ts, task_id),
            )
            project_id = row["project_id"]
            is_first = not old_plan
        if is_first:
            self.bb.append_event(
                project_id, "task.plan_set",
                {"task_id": task_id, "session_id": session_id, "plan": new_plan},
                session_id=session_id,
            )
        else:
            self.bb.append_event(
                project_id, "task.plan_revised",
                {"task_id": task_id, "session_id": session_id, "plan": new_plan,
                 "old_plan": old_plan, "rev_reason": rev_reason},
                session_id=session_id,
            )
        return new_plan

    def step_plan(
        self,
        task_id: str,
        session_id: str,
        step_id: str,
        status: str,
        note: str = "",
    ) -> dict[str, Any]:
        """推进计划步状态（仅认领者）。

        切 doing 时旧 doing 自动回 todo（事件 auto_demoted 记录转移，保证任意时刻
        至多一个 doing）；blocked 必须带 note 说明阻塞原因。返回更新后的任务行。
        """
        if status not in self._PLAN_STATUSES:
            raise ValueError(f"非法计划步状态: {status}")
        note = (note or "").strip()
        if status == "blocked" and not note:
            raise ValueError("计划步置为 blocked 必须在 note 说明阻塞原因")
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["status"] != "claimed" or row["claimed_by"] != session_id:
                raise ClaimError(f"任务 {task_id} 不由会话 {session_id} 持有，无权改计划")
            plan = _loads(row["plan"], [])
            step = next((s for s in plan if s.get("id") == step_id), None)
            if step is None:
                raise ValueError(f"计划步不存在: {step_id}（先 task_plan 修订，再 task_step）")
            auto_demoted: list[str] = []
            if status == "doing":
                for s in plan:
                    if s.get("id") != step_id and s.get("status") == "doing":
                        s["status"] = "todo"
                        auto_demoted.append(s["id"])
            step["status"] = status
            if note:
                step["note"] = note
            # ts 是步的创建时间（修订据此保留），状态推进时间走 task.step 事件
            self.bb.conn.execute(
                "UPDATE tasks SET plan=?, updated_at=? WHERE id=?",
                (json.dumps(plan, ensure_ascii=False), now(), task_id),
            )
            project_id = row["project_id"]
        self.bb.append_event(
            project_id, "task.step",
            {"task_id": task_id, "session_id": session_id,
             "step_id": step_id, "status": status, "note": note,
             "auto_demoted": auto_demoted},
            session_id=session_id,
        )
        return self.get_task(task_id)  # type: ignore[return-value]

    # ---------- 人类管理（§6.4 插手通道：编辑 / 删除 / 失败放回） ----------

    _UPDATABLE = {"objective", "task_type", "noise_budget", "priority", "conflict_keys"}
    _NOISE_VALUES = {"passive", "low", "medium", "high"}

    def update_task(
        self, task_id: str, by: str = "human", *,
        allowed_types: Iterable[str] | None = None, **changes: Any,
    ) -> dict:
        """编辑任务。仅 open/failed 可改：claimed 的 objective 已固化进在跑会话
        上下文（改了造成看板与会话脱节），done 是战果。failed 改完仍是 failed，
        需再调 reopen() 才回到待认领。返回更新后的任务行。"""
        bad = set(changes) - self._UPDATABLE
        if bad:
            raise ValueError(f"不可编辑字段: {sorted(bad)}")
        if not changes:
            got = self.get_task(task_id)
            if got is None:
                raise ValueError(f"任务不存在: {task_id}")
            return got
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["status"] not in {"open", "failed"}:
                raise ValueError(
                    f"任务 {task_id} 状态为 {row['status']}，不可编辑（仅待认领/失败可改）")
            merged = dict(row)
            merged["conflict_keys"] = _loads(merged["conflict_keys"], [])
            merged.update(changes)
            if not str(merged["objective"]).strip():
                raise ValueError("objective 不能为空")
            if "task_type" in changes:
                _check_task_type(str(merged["task_type"]), allowed_types)
            if merged["noise_budget"] not in self._NOISE_VALUES:
                raise ValueError(f"非法 noise_budget: {merged['noise_budget']}")
            if not isinstance(merged["priority"], int) or not 0 <= merged["priority"] <= 9:
                raise ValueError("priority 须为 0-9 的整数")
            if merged["noise_budget"] != "passive" and not merged["conflict_keys"]:
                raise ValueError("非 passive 任务必须提供 conflict_keys（active 互斥的依据）")
            sets: list[str] = []
            params: list[Any] = []
            for col in ("objective", "task_type", "noise_budget", "priority"):
                if col in changes:
                    sets.append(f"{col}=?")
                    params.append(merged[col])
            if "conflict_keys" in changes:
                sets.append("conflict_keys=?")
                params.append(json.dumps(merged["conflict_keys"], ensure_ascii=False))
            sets.append("updated_at=?")
            params.extend([now(), task_id])
            self.bb.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
            project_id = row["project_id"]
            event_changes = {
                k: (merged[k] if k != "conflict_keys" else list(merged["conflict_keys"]))
                for k in changes
            }
        self.bb.append_event(
            project_id, "task.updated",
            {"task_id": task_id, "by": by, "changes": event_changes}, author=by,
        )
        return self.get_task(task_id)  # type: ignore[return-value]

    def reopen(self, task_id: str, by: str = "human") -> None:
        """失败任务放回待认领（failed→open）：清持有方/租约；result_note 保留在库
        （看板 open 卡片不渲染，失败原因仍可从 task.failed 事件追溯）。仅 failed 可放回。"""
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["status"] != "failed":
                raise ValueError(f"任务 {task_id} 状态为 {row['status']}，仅失败任务可放回")
            self.bb.conn.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, lease_until=NULL, updated_at=?"
                " WHERE id=?",
                (now(), task_id),
            )
            project_id = row["project_id"]
        self.bb.append_event(
            project_id, "task.reopened", {"task_id": task_id, "by": by}, author=by,
        )

    def delete(self, task_id: str, by: str = "human") -> None:
        """删除任务（物理删除，队列不留存；定稿 2026-09-13，A1 起四态皆可删）。

        - open/claimed/done/failed 全部可删（战果不再强保留，task.deleted 快照即审计）。
        - claimed 任务删除：conflict_keys/lease 随行走立即释放；持有会话在下一个
          步边界感知（AgentSession._task_gone），当前步做完即按中断收尾，
          complete/fail 工具另有「任务已删除」友好文本兜底同一步内的收尾竞态。
        - 有子任务（parent_id 指向本任务）→ 拒绝，先处理子任务。
        - 行不留，但删除前落 task.deleted 事件（含字段快照+操作人），审计可追溯。
        """
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            child = self.bb.conn.execute(
                "SELECT 1 FROM tasks WHERE parent_id=? LIMIT 1", (task_id,)).fetchone()
            if child is not None:
                raise ValueError(f"任务 {task_id} 存在子任务，请先处理子任务再删除")
            snapshot = dict(row)
            self.bb.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            project_id, was_status = row["project_id"], row["status"]
        self.bb.append_event(
            project_id, "task.deleted",
            {"task_id": task_id, "by": by, "was_status": was_status,
             "objective": snapshot["objective"], "task_type": snapshot["task_type"],
             "noise_budget": snapshot["noise_budget"], "priority": snapshot["priority"],
             "conflict_keys": _loads(snapshot["conflict_keys"], []),
             "plan": _loads(snapshot.get("plan", "[]"), [])},
            author=by,
        )

    def renew_lease(self, task_id: str, session_id: str, lease_minutes: int = 30) -> None:
        from datetime import datetime, timedelta, timezone

        lease_until = (
            datetime.now(timezone.utc) + timedelta(minutes=lease_minutes)
        ).isoformat(timespec="seconds")
        with self.bb._tx():
            cur = self.bb.conn.execute(
                "UPDATE tasks SET lease_until=?, updated_at=?"
                " WHERE id=? AND claimed_by=? AND status='claimed'",
                (lease_until, now(), task_id, session_id),
            )
            if cur.rowcount == 0:
                raise ClaimError(f"续租失败：任务 {task_id} 未由 {session_id} 持有")

    def expire_leases(self) -> list[str]:
        """回收过期租约 → 任务回到 open。Orchestrator 周期调用（§6.4 监控）。"""
        expired: list[str] = []
        ts = now()
        with self.bb._tx():
            rows = self.bb.conn.execute(
                "SELECT id, project_id, claimed_by FROM tasks"
                " WHERE status='claimed' AND lease_until IS NOT NULL AND lease_until < ?",
                (ts,),
            ).fetchall()
            for r in rows:
                self.bb.conn.execute(
                    "UPDATE tasks SET status='open', claimed_by=NULL, lease_until=NULL,"
                    " updated_at=? WHERE id=?",
                    (ts, r["id"]),
                )
                expired.append(r["id"])
        if expired:
            proj = self.bb.conn.execute(
                "SELECT project_id FROM tasks WHERE id=?", (expired[0],)
            ).fetchone()
            if proj:
                self.bb.append_event(
                    proj["project_id"],
                    "task.lease_expired",
                    {"task_ids": expired},
                    author="orchestrator",
                )
        return expired

    # ---------- 查询 ----------

    def list_tasks(self, project_id: str, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM tasks WHERE project_id=?"
        params: list[Any] = [project_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY priority, created_at"
        out = []
        for r in self.bb.conn.execute(sql, params):
            d = dict(r)
            d["conflict_keys"] = _loads(d["conflict_keys"], [])
            d["context_refs"] = _loads(d.get("context_refs", "[]"), [])
            d["stale_refs"] = _loads(d.get("stale_refs", "[]"), [])
            d["plan"] = _loads(d.get("plan", "[]"), [])
            out.append(d)
        return out

    def get_task(self, task_id: str) -> dict | None:
        row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["conflict_keys"] = _loads(d["conflict_keys"], [])
        d["context_refs"] = _loads(d.get("context_refs", "[]"), [])
        d["stale_refs"] = _loads(d.get("stale_refs", "[]"), [])
        d["plan"] = _loads(d.get("plan", "[]"), [])
        return d
