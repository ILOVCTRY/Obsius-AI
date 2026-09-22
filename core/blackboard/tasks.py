"""任务队列（DESIGN.md §6）——多会话协调的核心原语。

关键规则：
- 认领互斥：noise_budget != passive 的任务带 conflict_keys（如 ["ip:1.2.3.4"]），
  同项目内 active 任务 conflict_keys 有交集则拒绝认领（§6.2 噪声预算 / WAF 对策）。
  passive 任务（逆向分析、被动收集）可任意共享。
- lease 租约：认领带 TTL，过期由 expire_leases() 回收为 open，防会话挂死占坑。
- 认领/完成/失败均落事件流，Orchestrator 消费这些事件做监控派生。
"""

import ipaddress
import json
import logging
import re
from typing import Any, Iterable
from urllib.parse import urlsplit

from core.blackboard import leases
from core.blackboard.store import Blackboard, new_id, now
from core.skills.taxonomy import GENERIC_TASK_TYPE

log = logging.getLogger(__name__)

# 服务端自动抽取层：objective 正文里的 finding id（id 格式固定，零纪律也不漏）
FINDING_REF_RE = re.compile(r"find-[0-9a-f]{12}")

# v14 同 target 防碎闸：同一目标的在队（open+claimed）任务达到该数即拒绝再发布
# （防模型把一个 IP/域名碎成过多子任务；人类 API 可 force 旁路，编排/Agent 不可）
MAX_TASKS_PER_TARGET = 4

_TARGET_SCHEMES = ("ip:", "host:", "domain:")


class ClaimError(Exception):
    """认领失败（已被认领 / active 互斥冲突）。"""


class TaskTypeError(ValueError):
    """task_type 不在场景轨注册表（task_types.yaml）内。拼错静默饿死的对侧防线。"""


def dedup_fp(task_type: str, scope: str, objective: str) -> str:
    """机制 1.1 发布去重指纹：task_type + 归一化 scope（折叠空白+小写）+ 规范化 objective
    （折叠空白）的 sha256 截短。scope 支持分号分隔多键，逐段归一后排序拼接。"""
    import hashlib

    norm_scope = ";".join(sorted(
        p.strip().lower() for p in re.split(r"[;；]", str(scope)) if p.strip()))
    norm_obj = " ".join(str(objective).split())
    raw = f"{str(task_type).strip().lower()}|{norm_scope}|{norm_obj}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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


def target_keys_of(scope: str, conflict_keys: list[str] | None) -> set[str]:
    """任务涉及的「目标」归一化键集合（v14 同 target 防碎闸与编排态势聚合共用，
    纯函数无 DB 访问）。

    - conflict_keys 非空：经 leases.normalize_keys 归一化后取 ip:/host:/domain: 键，
      url: 键额外派生 host:<hostname>（同主机其他任务合并计数）；
      binary:/func:/tool:/user: 不算目标（防碎闸针对目标资产维度）。
    - conflict_keys 为空（passive）：scope 切段（;/，/空白）逐段识别——IPv4 → ip:、
      http(s) URL → host:、含点裸域（末段须为字母 TLD）→ domain:；识别不出忽略。
    """
    keys: set[str] = set()
    if conflict_keys:
        for k in leases.normalize_keys(conflict_keys):
            if k.startswith(_TARGET_SCHEMES):
                keys.add(k)
            elif k.startswith("url:"):
                host = k[4:].split("/", 1)[0]
                if host:
                    keys.add(f"host:{host}")
        return keys
    for seg in re.split(r"[;；,，\s]+", str(scope or "")):
        seg = seg.strip().rstrip(".")
        if not seg:
            continue
        try:
            keys.add(f"ip:{ipaddress.ip_address(seg)}")
            continue
        except ValueError:
            pass
        try:
            parts = urlsplit(seg)
            if parts.scheme in {"http", "https"} and parts.hostname:
                keys.add(f"host:{parts.hostname.lower()}")
                continue
        except ValueError:
            pass
        low = seg.lower()
        if ("." in seg and all(ch.isalnum() or ch in ".-" for ch in low)
                and low.rsplit(".", 1)[-1].isalpha()):
            keys.add(f"domain:{low}")
    return keys


def render_attempts_lines(attempts: list[dict]) -> list[str]:
    """C10 任务执行履历 → 中文行列表（agent 接手提示与任务窗摘要共用渲染；
    纯函数无 DB 访问）。空履历返回空列表。"""
    lines: list[str] = []
    for i, a in enumerate(attempts or [], 1):
        who = a.get("session_name") or a.get("session_id") or "?"
        outcome = a.get("outcome") or "?"
        when = (a.get("ended_at") or "")[:19]
        extra = f"（{a['blocked_reason']}）" if a.get("blocked_reason") else ""
        note = (a.get("result_note") or "").splitlines()[0][:120] if a.get("result_note") else ""
        lines.append(f"- {chr(0x2460 + i - 1)} {who} {outcome}{extra}"
                     f"{('：' + note) if note else ''} @{when}")
    return lines


def _initial_context(attachments: list[dict] | None,
                     acceptance: list[str] | None) -> dict:
    """tasks.context 初始载荷（attachments 附件清单 + reconcile 对账分母）。

    reconcile 条目 id 从 1 起（task_reconcile 按 id 收口）；空 acceptance 不落键
    （旧任务无对账，complete 直通）。"""
    ctx: dict = {"attachments": list(attachments or [])}
    items = [{"id": i + 1, "text": str(t).strip()[:300], "state": "pending", "note": ""}
             for i, t in enumerate(acceptance or []) if str(t).strip()]
    if items:
        ctx["reconcile"] = items
    return ctx


class TaskQueue:
    def __init__(self, bb: Blackboard):
        self.bb = bb

    # ---------- 发布 ----------

    def check_parent(self, project_id: str, parent_id: str | None,
                     enforce_depth: bool = False) -> None:
        """C1 编排拆解：父子关系校验。坏父（不存在/跨项目——外键兜底）三写路径
        共用强制；**深度 1 层限制仅编排器路径强制**（enforce_depth=True）——
        撤回传播（机制 1.6/1.7）依赖任意深度 parent_id 子树，人类显式建深树不受限。
        违例抛 ValueError（API 422 / 工具回填 [拒绝]）。"""
        if not parent_id:
            return
        row = self.bb.conn.execute(
            "SELECT project_id, parent_id FROM tasks WHERE id=?", (parent_id,)).fetchone()
        if row is None or row["project_id"] != project_id:
            raise ValueError(f"父任务不存在或跨项目: {parent_id}")
        if enforce_depth and row["parent_id"]:
            raise ValueError(
                f"任务 {parent_id} 已是拆解子任务（编排拆解深度 1 层上限），不可再被拆解")

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
        workset: list[str] | None = None,
        attachments: list[dict] | None = None,
        parent_depth_limit: int | None = None,
        role: str = "",
        allowed_roles: Iterable[str] | None = None,
        max_children_per_parent: int | None = None,
        bypass_target_guard: bool = False,
        acceptance: list[str] | None = None,
        target_session: str = "",
    ) -> str:
        """发布任务。created_by: human / orchestrator / session-x。

        conflict_keys 示例：渗透 active 任务填 ["ip:1.2.3.4", "domain:x.com"]——
        携带同一 IP 键的 active 任务彼此互斥；passive 任务的键 = S 共享租约（机制 1.4）。
        键经服务端白名单归一化（leases.normalize_key），非法抛 ValueError。
        allowed_types：轨 task_types.yaml 注册表（由调用方按项目 track 注入），
        未知 task_type 直接拒收——拼写错误不再静默饿死（DESIGN.md §4.5.5）。
        refs：任务依据的 finding id（显式层，orch 工具/人发任务可填）；
        服务端同时从 objective 正文自动抽取 find- 标识（自动层），并集去重后
        入 context_refs——撤回传播据此反向定位（DESIGN.md §6.7 的 1.6）。
        workset（机制 1.1）：正在分析的目标集（advisory 软声明，不阻塞任何人，
        认领/派生/UI 可见，供避让）。
        role（v14 任务绑定角色）：建议认领角色 id，''=不限；认领会话按任务角色
        换装执行（v0.63 认领零过滤，role 仅偏好排序）。allowed_roles 为本轨已注册角色 id 集合
        （由调用方注入，None=不校验）。
        max_children_per_parent（v14 A5 限闸，仅 Agent 分解路径传）：父任务子任务
        数上限，超限拒绝——防模型偷懒层层下包。
        bypass_target_guard（v14 同 target 防碎闸旁路，仅人类 force 传）：同一目标
        的在队（open+claimed）任务达 MAX_TASKS_PER_TARGET 后拒绝再发布。
        attachments（2026-09-19 附件随发）：[{id,path,name,size}]（API 层已校验
        artifact 存在且 kind=attachment），原样落 tasks.context.attachments，
        认领首条消息由 agent 层渲染成 📎 附件清单。
        acceptance（2026-09-19 完成对账，借鉴 dsh stage-gate 覆盖度分母）：
        验收条目清单（一行一条），落 tasks.context.reconcile=[{id,text,state,note}]；
        complete 前必须逐条收口（task_reconcile 置 met/failed/blocked），未收口
        硬拦 done（task.reconcile_blocked 事件）——防模型虚报完成。
        target_session（2026-09-20 指派任务，DESIGN.md §6.4）：任务认领门控，
        ''=公共池（任意窗可认领）；非空=仅该窗可认领（claim 硬校验抛 ClaimError、
        claim_next WHERE 过滤）。store 层不校验会话存在性，归调用方（与 role 同口径）。
        """
        if noise_budget not in {"passive", "low", "medium", "high"}:
            raise ValueError(f"非法 noise_budget: {noise_budget}")
        if noise_budget != "passive" and not conflict_keys:
            raise ValueError("非 passive 任务必须提供 conflict_keys（active 互斥的依据）")
        _check_task_type(task_type, allowed_types)
        role = str(role or "").strip()
        if role and allowed_roles is not None and role not in allowed_roles:
            raise ValueError(
                f"未注册的 role: {role!r}；请使用本轨 roles/ 已注册角色 id（或留空不限）")
        if parent_id:
            self.check_parent(project_id, parent_id,
                              enforce_depth=parent_depth_limit is not None)  # 坏父共用；深度限编排器
        if parent_id and parent_depth_limit is not None:
            # 深度上限（编排器=1）：父任务自身必须仍是顶层（无 parent）——
            # 即拆解只允许「顶层父 + 子」两层；父已是被拆解的子任务 → 拒
            anc, cur = 0, parent_id
            while cur:
                row = self.bb.conn.execute(
                    "SELECT parent_id FROM tasks WHERE id=?", (cur,)).fetchone()
                cur = row["parent_id"] if row else None
                if cur:
                    anc += 1
            if anc >= parent_depth_limit:
                raise ValueError(
                    f"编排拆解深度 {parent_depth_limit} 层上限：父任务已是被拆解的子任务，不可再挂子任务")
        if parent_id and max_children_per_parent is not None:
            # v14 A5 限闸：每父任务子任务数上限（Agent 分解路径传 3）——防模型
            # 把简单任务下包成一大串子任务
            n_children = self.bb.conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE parent_id=?", (parent_id,)).fetchone()["n"]
            if n_children >= max_children_per_parent:
                raise ValueError(
                    f"父任务 {parent_id} 已挂 {n_children} 个子任务"
                    f"（上限 {max_children_per_parent}）——请自己完成或收敛，不要继续下包")
        norm_keys = leases.normalize_keys(conflict_keys) if conflict_keys else []
        target_keys = target_keys_of(scope, conflict_keys)  # v14 同 target 防碎闸用
        if workset and not isinstance(workset, list):
            raise ValueError("workset 须为字符串数组")
        context_refs = sorted({*(refs or []), *FINDING_REF_RE.findall(objective)})
        fp = dedup_fp(task_type, scope, objective)
        task_id = new_id("task")
        ts = now()
        with self.bb._tx():
            if not bypass_target_guard and target_keys:
                # v14 同 target 防碎闸：同一目标的在队（open+claimed）任务达阈值即拒
                # （同一事务内计数，与并发发布/认领原子；O(在队任务数) 扫描可接受）
                n_target = 0
                for r in self.bb.conn.execute(
                    "SELECT scope, conflict_keys FROM tasks"
                    " WHERE project_id=? AND status IN ('open','claimed')", (project_id,),
                ).fetchall():
                    if target_keys & target_keys_of(r["scope"], _loads(r["conflict_keys"], [])):
                        n_target += 1
                if n_target >= MAX_TASKS_PER_TARGET:
                    raise ValueError(
                        f"同目标 {'、'.join(sorted(target_keys))} 在队（open+claimed）任务已达 "
                        f"{n_target} 个（阈值 {MAX_TASKS_PER_TARGET}）——目标正在被碎成过多任务；"
                        "请先消化存量或合并范围")
            # 发布期冲突检查（机制 1.4）：active(X) 键被有效租约持有 → open 行写 wait_for
            # 门控标记（claim_next 排除，等待不占线程），不阻塞发布本身。
            wait: list[str] = []
            if noise_budget != "passive" and norm_keys:
                held = leases.active_leases_for_keys(self.bb, project_id, norm_keys)
                wait = sorted({h["resource_key"] for h in held
                               if leases.keys_conflict("X", h["mode"])})
            self.bb.conn.execute(
                "INSERT INTO tasks(id,project_id,scope,task_type,objective,status,priority,"
                "noise_budget,conflict_keys,parent_id,created_by,role,target_session,"
                "context_refs,workset,dedup_fp,wait_for,context,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",  # noqa: E501 -- 19 ? + 'open' = 20 列
                (
                    task_id,
                    project_id,
                    scope,
                    task_type,
                    objective,
                    priority,
                    noise_budget,
                    json.dumps(norm_keys, ensure_ascii=False),
                    parent_id,
                    created_by,
                    role,
                    str(target_session or ""),
                    json.dumps(context_refs, ensure_ascii=False),
                    json.dumps(sorted(str(w) for w in (workset or [])), ensure_ascii=False),
                    fp,
                    json.dumps(wait, ensure_ascii=False),
                    json.dumps(_initial_context(attachments, acceptance),
                               ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
        self.bb.append_event(
            project_id,
            "task.published",
            {"task_id": task_id, "objective": objective, "task_type": task_type,
             "role": role,
             "target_session": str(target_session or ""),  # v18 指派（''=公共池，前端看板 chip）
             "attachments": [a.get("name", "") for a in (attachments or [])]},  # 附件随发：事件流可见（本体在 context.attachments）
            author=created_by,
        )
        return task_id

    def find_dedup_target(self, project_id: str, fp: str) -> dict | None:
        """机制 1.1：按指纹找既有 open/claimed 任务（发布去重的查询半步）。"""
        row = self.bb.conn.execute(
            "SELECT id FROM tasks WHERE project_id=? AND dedup_fp=?"
            " AND status IN ('open','claimed') ORDER BY created_at LIMIT 1",
            (project_id, fp),
        ).fetchone()
        return self.get_task(row["id"]) if row else None

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
            if row["target_session"] and row["target_session"] != session_id:
                # v18 指派门控：任务已指派给其他窗口，本窗不可认领（事务内直接抛，
                # 无副作用不回滚任何写）
                raise ClaimError(
                    f"任务 {task_id} 已指派给窗口 {row['target_session']}，本窗不可认领")
            # 机制 1.4：冲突统一收集（快路径 active 交叠 + 资源租约），事务内写 wait_for
            # 门控标记并保持 open，事务提交后在事务外抛 ClaimError（线程不阻塞，
            # 标记不被回滚）。无冲突 → 授予租约行 + 置 claimed。
            lease_blockers: list[str] = []
            blocker_by: str | None = None
            norm_keys = leases.normalize_keys(_loads(row["conflict_keys"], []))
            if row["noise_budget"] != "passive" and norm_keys:
                keys = set(norm_keys)
                others = self.bb.conn.execute(
                    "SELECT id, conflict_keys FROM tasks"
                    " WHERE project_id=? AND status='claimed' AND noise_budget!='passive' AND id!=?",
                    (row["project_id"], task_id),
                ).fetchall()
                for other in others:
                    overlap = keys & set(leases.normalize_keys(
                        _loads(other["conflict_keys"], [])))
                    if overlap:
                        lease_blockers = sorted(overlap)
                        blocker_by = other["id"]
                        break
            mode = leases.mode_for(row["noise_budget"])
            if not lease_blockers and norm_keys:
                held = [h for h in leases.active_leases_for_keys(
                    self.bb, row["project_id"], norm_keys, exclude_task_id=task_id)
                    if leases.keys_conflict(mode, h["mode"])]
                if held:
                    lease_blockers = sorted({h["resource_key"] for h in held})
                    blocker_by = held[0]["task_id"]
            if lease_blockers:
                self.bb.conn.execute(
                    "UPDATE tasks SET wait_for=?, updated_at=? WHERE id=?",
                    (json.dumps(lease_blockers, ensure_ascii=False), now(), task_id))
            else:
                for key in norm_keys:
                    self.bb.conn.execute(
                        "INSERT INTO resource_leases"
                        "(project_id,resource_key,mode,task_id,session_id,granted_at)"
                        " VALUES(?,?,?,?,?,?)"
                        " ON CONFLICT(project_id,resource_key,task_id) DO UPDATE SET"
                        " mode=excluded.mode, session_id=excluded.session_id,"
                        " granted_at=excluded.granted_at",
                        (row["project_id"], key, mode, task_id, session_id, now()))
                self.bb.conn.execute(
                    "UPDATE tasks SET status='claimed', claimed_by=?, lease_until=?,"
                    " wait_for='[]', updated_at=? WHERE id=?",
                    (session_id, lease_until, now(), task_id),
                )
        if lease_blockers:
            raise ClaimError(
                f"任务 {task_id} 认领被拒：资源 {'、'.join(lease_blockers)} 已被"
                f" {blocker_by} 占用（保持 open 等待释放，DESIGN.md §6.7.1 资源租约）")
        self.bb.append_event(
            row["project_id"],
            "task.claimed",
            {"task_id": task_id, "session_id": session_id, "lease_until": lease_until,
             "created_by": row["created_by"],  # 前端编排页签按发布者关联任务全生命周期
             "role": row["role"]},  # v14 任务绑定角色（认领即换装，前端任务卡 🎭 chip）
            session_id=session_id,
        )

    def claim_next(
        self,
        project_id: str,
        session_id: str,
        lease_minutes: int = 30,
        session_role: str | None = None,
        only_task: str | None = None,
        assigned_only: bool = False,
    ) -> str | None:
        """Worker 循环入口：按优先级（P0 最高）认领第一个 open 任务；无匹配返回 None。

        窗口不设 role 限制（2026-09-18 用户定稿：任务才是本体）——认领无类型/噪声
        过滤，任务绑定的 role 经「认领即换装」在认领后生效。
        session_role 仅作**偏好排序**（非过滤）：底色匹配或 role='' 的任务排前，
        减少无谓换装；任何窗都能立即认领任何任务。
        v0.71 任务即窗口：only_task 非空时只认领该任务（绑定窗一窗一任务的核心闸，
        配合 target_session 门控锁死归属——SQL 侧过滤是唯一无竞态的写法）。
        v0.72 全局一窗一任务：assigned_only=True 时公共池认领机制退役——SQL 门控
        收窄为 `AND target_session=?`（target_session='' 的任务不可见），无绑定
        手动窗专用挡位：手动窗永不认领公共池任务，每个任务必有专属窗。
        """
        if assigned_only:
            sql = ("SELECT id, wait_for, lease_cooldown_until, role, created_at FROM tasks"
                   " WHERE project_id=? AND status='open'"
                   " AND target_session=?")  # v0.72：只见显式指派给本窗的任务
        else:
            sql = ("SELECT id, wait_for, lease_cooldown_until, role, created_at FROM tasks"
                   " WHERE project_id=? AND status='open'"
                   " AND (target_session='' OR target_session=?)")  # v18：指派门控（仅目标窗见指派任务）
        params: list[Any] = [project_id, session_id]
        if only_task:
            sql += " AND id=?"
            params.append(only_task)
        if session_role is None:
            sql += " ORDER BY CASE WHEN target_session=? THEN 0 ELSE 1 END, priority, created_at"
            params.append(session_id)
        else:
            sql += (" ORDER BY CASE WHEN target_session=? THEN 0 ELSE 1 END,"
                    " CASE WHEN role = '' OR role = ? THEN 0 ELSE 1 END,"
                    " priority, created_at")
            params.extend([session_id, session_role])
        # 机制 1.4：预过滤——wait_for 键仍被有效租约持有的行、死锁牺牲者冷却未到的行
        # 直接跳过（等待不占线程；键已空闲的 stale wait_for 仍走正常认领路径）
        held_union: set[str] = set()
        for keys in leases.held_keys_by_task(self.bb, project_id).values():
            held_union |= keys
        ts = now()
        for row in self.bb.conn.execute(sql, params).fetchall():
            if row["lease_cooldown_until"] and row["lease_cooldown_until"] > ts:
                continue
            wf = _loads(row["wait_for"], [])
            if wf and (set(wf) & held_union):
                continue
            try:
                self.claim(row["id"], session_id, lease_minutes)
                return row["id"]
            except ClaimError:
                continue  # 被别人抢了/互斥，试下一个
        return None

    # ---------- 收尾 ----------

    def complete(self, task_id: str, session_id: str, result_note: str = "",
                 persona_role: str | None = None) -> None:
        self._check_reconcile(task_id, session_id)
        self._finish(task_id, session_id, "done", result_note, persona_role=persona_role)

    # ---------- 完成对账（2026-09-19，借鉴 dsh stage-gate 分母对账） ----------

    def _check_reconcile(self, task_id: str, session_id: str) -> None:
        """done 硬闸：context.reconcile 有 pending 条目 → 拒绝完成并落
        task.reconcile_blocked 事件（仿计划闸口径，Agent 补收口或显式置
        failed/blocked 后重试）。fail/reopen 不检——人工/失败绕过天然成立。"""
        row = self.bb.conn.execute(
            "SELECT project_id, context, claimed_by FROM tasks WHERE id=?",
            (task_id,)).fetchone()
        if row is None or row["claimed_by"] != session_id:
            return  # 权限/存在性由 _finish 统一报错，这里只管自己的闸
        entries = (_loads(row["context"], {}) or {}).get("reconcile") or []
        pending = [e for e in entries if e.get("state") == "pending"]
        if not pending:
            return
        items = "\n".join(f"  {e['id']}. {e['text']}" for e in pending)
        self.bb.append_event(
            row["project_id"], "task.reconcile_blocked",
            {"task_id": task_id, "session_id": session_id,
             "pending": [{"id": e["id"], "text": e["text"]} for e in pending]},
            session_id=session_id, author=session_id)
        raise ValueError(
            "[计划闸] 完成对账未收口：以下验收条目还没有逐条交代（发布时登记的分母）：\n"
            f"{items}\n"
            "用 task_reconcile 逐条收口后再 complete：met=已完成（note 附证据）、"
            "failed=已证实无法完成（note 附原因）、blocked=受阻（note 附卡点）。"
            "不要虚报完成。")

    def set_reconcile_state(self, task_id: str, session_id: str, item_id: int,
                            state: str, note: str = "") -> list[dict]:
        """收口一条验收对账条目（仅任务持有者；claimed 态）。返回收口后的全表。"""
        if state not in ("met", "failed", "blocked"):
            raise ValueError(f"非法对账状态: {state}（met/failed/blocked）")
        with self.bb._tx():
            row = self.bb.conn.execute(
                "SELECT status, claimed_by, context FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["claimed_by"] != session_id:
                raise ClaimError(f"任务 {task_id} 不由会话 {session_id} 持有，无权收口")
            if row["status"] != "claimed":
                raise ValueError(f"任务 {task_id} 状态 {row['status']}，仅认领中可收口")
            ctx = _loads(row["context"], {}) or {}
            entries = ctx.get("reconcile") or []
            ent = next((e for e in entries if e.get("id") == item_id), None)
            if ent is None:
                raise ValueError(f"对账条目不存在: {item_id}"
                                 f"（共 {len(entries)} 条；本任务未登记对账分母则无需收口）")
            ent["state"] = state
            ent["note"] = (note or "").strip()[:300]
            self.bb.conn.execute(
                "UPDATE tasks SET context=?, updated_at=? WHERE id=?",
                (json.dumps({**ctx, "reconcile": entries}, ensure_ascii=False),
                 now(), task_id))
        return entries

    def fail(self, task_id: str, session_id: str, result_note: str = "",
             resumable: bool = False, blocked_reason: str = "error",
             persona_role: str | None = None) -> None:
        """E12：resumable=True 表示中断保留了落盘快照（task.failed 事件带标记，
        看板 failed 卡出「▶ 续跑」——reopen+原会话载快照复活）。
        C1：blocked_reason 结构化失败原因——error（真失败）| awaiting_human（等
        人类输入，保留现场可续跑，看板出「待人工」徽章与「已解决，放回继续」）。
        persona_role（v14）：实际执行 persona（换装中=任务角色，底色=底色角色），
        attempts 履历区分「会话底色」与「本单实际角色」。"""
        if blocked_reason not in {"error", "awaiting_human"}:
            raise ValueError(f"非法 blocked_reason: {blocked_reason}")
        self._finish(task_id, session_id, "failed", result_note,
                     extra={"resumable": True} if resumable else None,
                     blocked_reason=blocked_reason, persona_role=persona_role)

    def _release_and_revalidate(self, project_id: str, task_id: str) -> None:
        """机制 1.4：释放任务的全部资源租约行，并重校验本项目 open 行的 wait_for——
        键已全部空闲的等待者清门控标记（重新可被 claim_next 认领）。
        必须在调用方的 _tx() 内执行。"""
        self.bb.conn.execute("DELETE FROM resource_leases WHERE task_id=?", (task_id,))
        held_union: set[str] = set()
        for keys in leases.held_keys_by_task(self.bb, project_id).values():
            held_union |= keys
        for r in self.bb.conn.execute(
            "SELECT id, wait_for FROM tasks WHERE project_id=? AND status='open'"
            " AND wait_for != '[]'", (project_id,),
        ).fetchall():
            wf = _loads(r["wait_for"], [])
            if wf and not (set(wf) & held_union):
                self.bb.conn.execute(
                    "UPDATE tasks SET wait_for='[]', updated_at=? WHERE id=?",
                    (now(), r["id"]))

    def _finish(self, task_id: str, session_id: str, status: str, result_note: str,
                extra: dict | None = None, blocked_reason: str | None = None,
                persona_role: str | None = None) -> None:
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["claimed_by"] != session_id:
                raise ClaimError(f"任务 {task_id} 不由会话 {session_id} 持有，无权收尾")
            # C10：任务执行履历——每次收尾追加一条 attempt（cap 20），并登记现场
            # 文件名（transcript 由 agent 层每步落盘，此处只做声明性登记）。
            # context 列唯一写点就是这里（complete/fail/awaiting_human/重启清扫
            # 全部收尾路径都收敛于 _finish）。
            ctx = _loads(row["context"] if "context" in row.keys() else "{}", {})
            if not isinstance(ctx, dict):
                ctx = {}
            attempts = ctx.get("attempts") or []
            sess = self.bb.conn.execute(
                "SELECT name, role FROM sessions WHERE id=?", (session_id,)).fetchone()
            attempts.append({
                "session_id": session_id,
                "session_name": (sess["name"] if sess else None),
                "role": persona_role or (sess["role"] if sess else None),  # v14 实际执行 persona
                "outcome": status,
                "result_note": (result_note or "")[:500],
                "blocked_reason": blocked_reason if status == "failed" else None,
                "ended_at": now(),
            })
            ctx["attempts"] = attempts[-20:]
            ctx["transcript"] = f"task-{task_id}.json"
            self.bb.conn.execute(
                "UPDATE tasks SET status=?, result_note=?, lease_until=NULL,"
                " blocked_reason=?, context=?, updated_at=? WHERE id=?",
                (status, result_note, blocked_reason or "error",
                 json.dumps(ctx, ensure_ascii=False), now(), task_id),
            )
            self._release_and_revalidate(row["project_id"], task_id)  # 机制 1.4 释放+重校验
            stale_refs = _loads(row["stale_refs"], [])
        payload = {"task_id": task_id, "session_id": session_id, "note": result_note,
                   "created_by": row["created_by"]}  # 前端编排页签按发布者关联任务全生命周期
        if status == "failed":
            payload["blocked_reason"] = blocked_reason or "error"
        if extra:
            payload.update(extra)
        self.bb.append_event(
            row["project_id"],
            f"task.{status}",
            payload,
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
        # 子任务回执（2026-09-20 会话窗对话化，P3 多智能体协调）：本任务有 parent
        # 且父任务已由其他活跃窗认领 → 私信 task_receipt 到父窗——子任务完成/失败
        # 自动回执，父 agent 收件箱注入派生结果摘要（异步模型，不打断父窗工作）。
        # ref_id 复用 task_id：同一子任务未读期间重复收尾（reopen 重跑）不重复投递；
        # findings=本会话（作者视角）最近登记的发现 cap 10。整段只 log 不影响收尾。
        try:
            parent_id = row["parent_id"] if "parent_id" in row.keys() else None
            if parent_id:
                prow = self.bb.conn.execute(
                    "SELECT claimed_by FROM tasks WHERE id=?", (parent_id,)).fetchone()
                to_sid = prow["claimed_by"] if prow else None
                if to_sid and to_sid != session_id:
                    srow = self.bb.conn.execute(
                        "SELECT status FROM sessions WHERE id=?", (to_sid,)).fetchone()
                    if srow and srow["status"] != "closed":
                        finds = [
                            {"id": r["id"], "title": r["title"], "severity": r["severity"]}
                            for r in self.bb.conn.execute(
                                "SELECT id, title, severity FROM findings"
                                " WHERE project_id=? AND author=?"
                                " ORDER BY created_at DESC LIMIT 10",
                                (row["project_id"], session_id)).fetchall()
                        ]
                        rcpt = {
                            "task_id": task_id, "status": status,
                            "objective": (row["objective"] or "")[:200],
                            "result_note": (result_note or "")[:500],
                            "findings": finds,
                            "blocked_reason": blocked_reason if status == "failed" else None,
                        }
                        if self.bb.inbox_post(row["project_id"], to_sid,
                                              "task_receipt", task_id, rcpt):
                            self.bb.append_event(
                                row["project_id"], "message.inbox",
                                {"to_session": to_sid, "kind": "task_receipt",
                                 "ref_id": task_id, **rcpt, "by": session_id},
                                session_id=to_sid, author=session_id)
        except Exception:  # noqa: BLE001 —— 回执失败不影响收尾主路径
            log.exception("子任务回执投递失败 task=%s", task_id)
        # 执行轨迹物化（execution-trace-chain R3，2026-09-22）：任务收尾 done 时把
        # 会话事件轨迹物化进 origin='trace' 自动链（trace_ref 幂等，重跑安全）。
        # 惰性 import 防循环依赖；失败只 log 不挡收尾（同上 task_receipt 先例）。
        if status == "done":
            try:
                from core.blackboard import traces
                traces.materialize_task_trace(self.bb, row["project_id"], task_id,
                                              author=session_id)
            except Exception:  # noqa: BLE001 —— 物化失败不影响收尾主路径
                log.exception("任务轨迹物化失败 task=%s", task_id)

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

    # 意图接地（2026-09-20，借鉴 Intentest arXiv:2609.07344「意图边受前驱事实约束」）：
    # 计划步 refs 引用的黑板对象表——id 必须真实存在且同项目，编造/悬空构造上被拒
    # （外部 payload 注入的指令拿不出真实 id，就无法成为计划里的实质动作步）。
    _REF_TABLES = {
        "asset": "assets", "finding": "findings", "artifact": "artifacts",
        "func": "func_kb", "event": "events", "task": "tasks",
        "blueprint": "blueprints",
    }

    def _resolve_step_refs(self, project_id: str, refs: Any) -> list[str]:
        """校验计划步接地引用：格式 <kind>:<id>、对象存在且同项目。返回规范化列表。"""
        if refs is None:
            return []
        if not isinstance(refs, list) or len(refs) > 5:
            raise ValueError("refs 必须是 ≤5 个引用的数组")
        out: list[str] = []
        for r in refs:
            if not isinstance(r, str) or ":" not in r:
                raise ValueError(
                    f"接地引用格式非法: {r!r}（应为 kind:id，如 finding:f1a2…）")
            kind, _, oid = r.partition(":")
            kind, oid = kind.strip(), oid.strip()
            table = self._REF_TABLES.get(kind)
            if not table or not oid:
                raise ValueError(
                    f"接地引用类型不支持: {kind}:（可用: {', '.join(self._REF_TABLES)}）")
            hit = self.bb.conn.execute(
                f"SELECT 1 FROM {table} WHERE id=? AND project_id=?",
                (oid, project_id)).fetchone()
            if hit is None:
                raise ValueError(
                    f"接地引用悬空（不存在或跨项目）: {kind}:{oid}"
                    "——先 bb_query 查真实对象 id，或先 bb_add_asset/bb_add_finding 登记")
            if r not in out:
                out.append(r)
        return out

    def set_plan(
        self,
        task_id: str,
        session_id: str,
        steps: list[dict[str, Any]],
        rev_reason: str = "",
    ) -> list[dict[str, Any]]:
        """写/修订认领任务的执行计划（仅认领者，claimed 态）。

        steps 元素 {"title": str, "id"?: "p<n>", "refs"?: ["<kind>:<id>"…]}：
        带既有 id 的步保留原状态/时间戳（标题可更新），新步服务端发号 p<n+1>；
        缺省的旧步丢弃（doing 被删则自然无 doing，需重新 task_step）。
        refs=接地引用（意图接地 2026-09-20）：格式 <kind>:<id>（asset/finding/
        artifact/func/event/task/blueprint），≤5 条，对象必须存在且同项目——
        实质动作步（利用/打点/写利用代码）应引用其依据的已验证事实，编造/
        悬空 id 服务端拒绝（反意图漂移 + 反提示注入的机制级约束）。
        首次写落 task.plan_set，再调落 task.plan_revised（含旧计划快照+修订
        原因）。返回新计划。
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
            project_id = row["project_id"]
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
                refs = self._resolve_step_refs(project_id, item.get("refs"))
                step_id = item.get("id")
                if isinstance(step_id, str) and step_id in old_by_id:
                    kept = dict(old_by_id[step_id])
                    kept["title"] = title
                    if refs:
                        kept["refs"] = refs
                    else:
                        kept.pop("refs", None)
                    new_plan.append(kept)
                else:
                    max_n += 1
                    step = {"id": f"p{max_n}", "title": title,
                            "status": "todo", "ts": ts}
                    if refs:
                        step["refs"] = refs
                    new_plan.append(step)
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

    _UPDATABLE = {"objective", "task_type", "noise_budget", "priority", "conflict_keys",
                  "role"}  # v0.71：role 可改（任务即窗口——中途改角色立即热换装）
    _NOISE_VALUES = {"passive", "low", "medium", "high"}

    def update_task(
        self, task_id: str, by: str = "human", *,
        allowed_types: Iterable[str] | None = None, **changes: Any,
    ) -> dict:
        """编辑任务。仅 open/failed 可改：claimed 的 objective 已固化进在跑会话
        上下文（改了造成看板与会话脱节），done 是战果。failed 改完仍是 failed，
        需再调 reopen() 才回到待认领。返回更新后的任务行。
        v0.71 例外：claimed 态**仅放行 role**——任务绑定角色中途可改，API 层
        改完对在跑会话热换装（apply_role_change）。"""
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
            if row["status"] == "claimed" and set(changes) - {"role"}:
                raise ValueError(
                    f"任务 {task_id} 执行中仅可修改角色（objective 等字段已固化进在跑会话上下文）")
            if row["status"] not in {"open", "failed", "claimed"}:
                raise ValueError(
                    f"任务 {task_id} 状态为 {row['status']}，不可编辑（仅待执行/执行中/失败可改）")
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
            if merged["conflict_keys"]:
                # 机制 1.4：编辑后的键重新归一化（非法 422），归一化结果回写
                merged["conflict_keys"] = leases.normalize_keys(merged["conflict_keys"])
            # 机制 1.1：编辑触及指纹要素时重算发布去重指纹
            merged["dedup_fp"] = dedup_fp(str(merged["task_type"]),
                                          str(merged["scope"]), str(merged["objective"]))
            sets: list[str] = []
            params: list[Any] = []
            for col in ("objective", "task_type", "noise_budget", "priority", "role"):
                if col in changes:
                    sets.append(f"{col}=?")
                    params.append(merged[col])
            if "conflict_keys" in changes:
                sets.append("conflict_keys=?")
                params.append(json.dumps(merged["conflict_keys"], ensure_ascii=False))
            sets.append("dedup_fp=?")
            params.append(merged["dedup_fp"])
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

    def reopen(self, task_id: str, by: str = "human", note: str = "",
               scene: str = "kept") -> None:
        """失败任务放回待认领（failed→open）：清持有方/租约；result_note 保留在库
        （看板 open 卡片不渲染，失败原因仍可从 task.failed 事件追溯）。仅 failed 可放回。
        C1：note = 人类补充说明（如「ROE 已核验」），追加进 result_note 并随
        task.reopened 事件落审计——认领会话在旧计划注入提示中可见。
        C6：scene=dropped（放回时勾选「丢弃现场」）随事件带出——现场文件的
        unlink 由 API 层执行（store 层不碰文件系统），默认 kept 保留现场。
        v0.71 任务即窗口：**保留 target_session**——失败任务归原绑定窗（窗是
        该任务的延续现场，原窗重跑接手履历最完整）；原窗已被人工关闭时由
        调度器启动段发现「open+绑定指向 closed 窗」自动重绑新窗。"""
        with self.bb._tx():
            row = self.bb.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            if row["status"] != "failed":
                raise ValueError(f"任务 {task_id} 状态为 {row['status']}，仅失败任务可放回")
            result_note = row["result_note"] or ""
            if note.strip():
                result_note = (result_note + "\n" if result_note else "") + \
                    f"人类补充（{by}）: {note.strip()}"
            self.bb.conn.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, lease_until=NULL,"
                " result_note=?, updated_at=? WHERE id=?",
                (result_note, now(), task_id),
            )
            project_id = row["project_id"]
            created_by = row["created_by"]
        self.bb.append_event(
            project_id, "task.reopened",
            {"task_id": task_id, "by": by, "note": note.strip(), "scene": scene,
             "created_by": created_by},  # 前端编排页签按发布者关联任务全生命周期
            author=by,
        )

    def bind_session(self, task_id: str, session_id: str, by: str = "system") -> None:
        """v0.71 任务即窗口：把 open 任务绑定到专属执行窗（写 target_session）。

        仅 open 且尚未绑定（target_session=''）的任务可绑——rowcount=0 抛
        ValueError（已绑/非 open，绑定竞态防御，调用方据此回收刚建的孤儿窗）。
        事务外落 task.window_bound 事件（前端看板 chip 与审计）。
        """
        with self.bb._tx():
            row = self.bb.conn.execute(
                "SELECT project_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            cur = self.bb.conn.execute(
                "UPDATE tasks SET target_session=?, updated_at=?"
                " WHERE id=? AND status='open' AND target_session=''",
                (session_id, now(), task_id))
            if cur.rowcount == 0:
                raise ValueError(f"任务 {task_id} 已绑定或非 open，不可重复绑定")
            project_id = row["project_id"]
        self.bb.append_event(
            project_id, "task.window_bound",
            {"task_id": task_id, "session_id": session_id, "by": by},
            author=by,
        )

    def unassign_session(self, session_id: str) -> list[str]:
        """v18 关窗退回公共池：该会话指派的所有 open 任务清 target_session。

        仅动 open 行——claimed 行租约仍有效（持有窗继续跑，或 expire_leases
        回收为 open 后保留指派、由目标窗再认领，语义正确）。返回被清空指派的
        任务 id 列表；每个任务在事务外落 task.updated 事件（前端 events.tsx
        既有标签直接可见），reason=session-closed 标明来源。
        """
        with self.bb._tx():
            rows = self.bb.conn.execute(
                "SELECT id, project_id FROM tasks"
                " WHERE target_session=? AND status='open'", (session_id,)).fetchall()
            affected = [(r["id"], r["project_id"]) for r in rows]
            if affected:
                self.bb.conn.execute(
                    "UPDATE tasks SET target_session='', updated_at=?"
                    " WHERE target_session=? AND status='open'",
                    (now(), session_id))
        for task_id, project_id in affected:
            self.bb.append_event(
                project_id, "task.updated",
                {"task_id": task_id, "by": "system",
                 "changes": {"target_session": session_id},
                 "reason": "session-closed"},  # 原指派窗口已关闭，任务退回公共池
                author="system",
            )
        return [tid for tid, _ in affected]

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
            self._release_and_revalidate(project_id=row["project_id"],
                                         task_id=task_id)  # 机制 1.4 释放+重校验
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

    # ---------- 机制 1.4 六防死锁之 6：wait-for 图环检测（安全网） ----------

    def detect_wait_for_deadlock(self, project_id: str) -> list[str]:
        """wait-for 图环检测兜底。发布期/认领期门控下环在构造上不可达（claimed
        任务不再等待），本函数是运行期动态锁（后置）与人工强占等未来路径、
        以及实现 bug 的安全网。

        图：wait_for 非空的任务 → 持有其被占键的租约任务（resource_leases 全量行，
        不做有效性过滤——安全网要看的是不一致状态）。检出环 → 牺牲者=环内
        created_at 最晚的任务：清 wait_for + 冷却 5 分钟（claim_next 跳过）+
        落 lock.deadlock_victim 审计。返回牺牲者 task_id 列表。"""
        from datetime import datetime, timedelta, timezone

        with self.bb._tx():
            waiting = {
                r["id"]: (set(_loads(r["wait_for"], [])), str(r["created_at"]))
                for r in self.bb.conn.execute(
                    "SELECT id, wait_for, created_at FROM tasks"
                    " WHERE project_id=? AND wait_for != '[]'", (project_id,))
            }
            if len(waiting) < 2:
                return []
            holders: dict[str, set[str]] = {}
            for r in self.bb.conn.execute(
                "SELECT resource_key, task_id FROM resource_leases WHERE project_id=?",
                (project_id,),
            ):
                holders.setdefault(r["resource_key"], set()).add(r["task_id"])
            # 边：等待任务 → 占它键的任务（不含自环）
            edges: dict[str, set[str]] = {}
            for wid, (keys, _ts) in waiting.items():
                for k in keys:
                    for h in holders.get(k, ()):
                        if h != wid:
                            edges.setdefault(wid, set()).add(h)
            # DFS 找环；每个环选 created_at 最晚者为牺牲者
            color: dict[str, int] = {}
            stack: list[str] = []
            cycles: list[list[str]] = []

            def dfs(node: str) -> None:
                color[node] = 1
                stack.append(node)
                for nxt in edges.get(node, ()):
                    if color.get(nxt, 0) == 0:
                        dfs(nxt)
                    elif color.get(nxt) == 1:
                        i = stack.index(nxt)
                        cycles.append(stack[i:])
                stack.pop()
                color[node] = 2

            for n in list(edges):
                if color.get(n, 0) == 0:
                    dfs(n)
            victims: list[str] = []
            cooldown = (datetime.now(timezone.utc) + timedelta(minutes=5)
                        ).isoformat(timespec="seconds")
            for cycle in cycles:
                victim = max(cycle, key=lambda tid: waiting[tid][1])
                victims.append(victim)
                self.bb.conn.execute(
                    "UPDATE tasks SET wait_for='[]', lease_cooldown_until=?, updated_at=?"
                    " WHERE id=?",
                    (cooldown, now(), victim),
                )
        for v in victims:
            self.bb.append_event(
                project_id, "lock.deadlock_victim",
                {"task_id": v, "note": "wait-for 环检测牺牲者：清 wait_for 并冷却 5 分钟"},
                author="orchestrator",
            )
        return victims

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
        """回收过期租约 → 任务回到 open。Orchestrator 周期调用（§6.4 监控）。
        机制 1.4：过期任务的资源租约行随行释放，并重校验等待者。"""
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
                self._release_and_revalidate(r["project_id"], r["id"])
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

    def fail_interrupted_claims(self, project_id: str,
                                result_note: str = "后端重启，任务中断",
                                keep_claimed_by: frozenset[str] = frozenset(),
                                ) -> list[str]:
        """重启纪律（§3）：单进程部署下进程重启=所有 worker 线程消失，项目在
        本进程首次打开时全部 claimed 均为孤儿——统一转 failed(blocked_reason=
        awaiting_human)，看板出「⏸ 待人工」徽章与「✅ 已解决，放回继续」，由人工
        决定放回重跑或删除；**不自动回 open 重跑**（半执行任务重跑可能重复产生
        噪声/动作，安全默认宁严勿松）。逐任务落 task.failed 审计
        （session_id=原持有者），资源租约行随收尾释放。
        v0.64：`keep_claimed_by` 中的会话（持有落盘暂停快照者）其 claimed 任务
        **跳过不 fail**——保留 claimed 等「▶ 继续」断点续跑（API 层清扫负责
        校验指针+文件在场后传入）。"""
        interrupted: list[str] = []
        for row in self.list_tasks(project_id, status="claimed"):
            if not row.get("claimed_by"):
                continue
            if row["claimed_by"] in keep_claimed_by:
                continue
            try:
                self.fail(row["id"], row["claimed_by"], result_note,
                          blocked_reason="awaiting_human")
                interrupted.append(row["id"])
            except ClaimError:
                continue  # 并发首开时已被另一路径收尾
        return interrupted

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
            d["workset"] = _loads(d.get("workset", "[]"), [])
            d["wait_for"] = _loads(d.get("wait_for", "[]"), [])
            d["context"] = _loads(d.get("context", "{}"), {})
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
        d["workset"] = _loads(d.get("workset", "[]"), [])
        d["wait_for"] = _loads(d.get("wait_for", "[]"), [])
        d["context"] = _loads(d.get("context", "{}"), {})
        return d
