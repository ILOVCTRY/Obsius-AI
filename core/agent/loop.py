"""Agent 主循环（DESIGN.md §3）。

单 Agent + Skill 上下文注入：
  系统提示 = 领域红线(规则链) + 能力清单(detector) + 角色人设 + 任务目标 + 工具纪律
  循环 = LLM 调用 → 工具分发 → 结果回填 → （卡死则召唤策略顾问） → 直至 finish；
  步数耗尽不再自动 fail，而是自动步数暂停（E8）：快照 + 任务保持 claimed，等人类恢复
上下文预算：超限裁剪旧工具输出（防淹没全局目标）。
会话收尾安全：显式结束会话时任务未收尾自动 fail（防 lease 占坑，§6.4）。
会话控制（§3）：暂停/恢复/中断检查点一律落在 LLM 步之间；暂停存 messages 快照，
恢复从快照续跑当前任务；中断任务 fail（人工中断）不回队列。
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.agent.tools import AGENT_TOOLS, ToolDispatcher
from core.autonomy import record_llm_usage
from core.blackboard import Blackboard, TaskQueue
from core.runtime.gateway import ExecutionGateway
from core.skills import (
    SkillRegistry,
    SkillRouter,
    build_rules_preamble,
    load_kb_sources,
    load_task_types,
)
from core.skills.roles import load_role

log = logging.getLogger(__name__)

STRICT_PROMPT_TAIL = """
## 工具纪律（硬规则）
1. 一切命令经 run_cmd，runtime 按威胁等级选择；被网关拒绝后改道，不得重试同一动作。
2. 动手前先 bb_query 看黑板——别人可能已做过（去重是硬规则）。
3. **认领任务后第一件事是 task_plan 写下解决计划（3-8 个可验证小步）**，之后才允许
   run_cmd / bb_add_* 等实质动作（服务端有计划闸，未交计划会被回填引导）；情况变化时
   再调 task_plan 修订（保留进度的步带原 id，rev_reason 写原因），每开始/完成一步用
   task_step 置 doing/done——任意时刻至多一个 doing，被阻塞置 blocked 必写原因。
4. 发现即落 bb_add_finding；无证据 status=unverified。
5. 卡住时如实 fail_task，不要空转。
6. 经验沉淀只走 propose_pack_edit 提案（绝不直接改技能/知识库）：仅限三种情形——
   文档互相矛盾、文档缺失、某手法已在本任务中验证有效；reason 必须附任务证据
   （任务 id + 关键观察）。英文快照原文不翻译；新经验写成**新 md 文件**提案，
   不覆盖/不翻译英文原文；每个会话最多 3 条，禁止凑数。提案批准权在人类。
"""


@dataclass
class AgentConfig:
    # E8：默认步数预算 200（角色 yaml 取 min 可更严；request_steps 可自助 +200）
    max_steps: int = 200
    context_char_budget: int = 120_000
    stuck_after: int = 8          # 连续 N 步无进展 → 召唤策略顾问
    task_types: list[str] | None = None   # Worker 角色过滤（认领任务时）
    owner_tags: list[str] = field(default_factory=list)
    # 角色增强（§6.6，由 yaml 注入；None = 不限制）
    max_noise: str | None = None          # 角色 default_noise：噪声上限，认领过滤
    allowed_tools: list[str] | None = None
    max_runtime: str | None = None        # host/wsl/docker/sandbox 最高等级
    lease_minutes: int = 30               # 认领/续租的租约 TTL
    lease_renew_seconds: int = 600        # 租约心跳间隔（TTL 的 1/3，留两次余量）


class _LeaseHeartbeat(threading.Thread):
    """任务租约守护心跳（§6.4）：认领后周期 renew_lease，防长任务超过 TTL 被回收双跑。

    - pause 期间继续续租（任务仍被会话占有，快照恢复后接着跑）；
    - 任务 complete/fail/abort/被人类删除时由会话 stop()；
    - 续租抛 ClaimError（任务已不属于本会话）→ 自行退出；其他异常记下日志下一周期再试，
      绝不抛进主线程。
    """

    def __init__(self, tq: TaskQueue, session_id: str, task_id: str, interval_seconds: int):
        super().__init__(daemon=True, name=f"lease-hb-{session_id[:12]}-{task_id[:12]}")
        self.tq = tq
        self.session_id = session_id
        self.task_id = task_id
        self.interval = max(0.05, float(interval_seconds))
        self._stop_evt = threading.Event()

    def run(self) -> None:
        from core.blackboard.tasks import ClaimError

        while not self._stop_evt.wait(self.interval):
            try:
                self.tq.renew_lease(self.task_id, self.session_id)
            except ClaimError:
                log.info("租约心跳退出：任务 %s 已不由会话 %s 持有",
                         self.task_id, self.session_id)
                return
            except Exception:  # noqa: BLE001 —— 心跳是安全网，瞬时失败下一周期补偿
                log.warning("租约续租失败（任务 %s），下一周期重试", self.task_id, exc_info=True)

    def stop(self) -> None:
        self._stop_evt.set()


class AgentSession:
    def __init__(
        self,
        *,
        project_id: str,
        bb: Blackboard,
        gateway: ExecutionGateway,
        llm: Any,                       # LLMProvider（executor 模型）
        planner_llm: Any | None = None,  # 策略顾问（planner 模型，缺省禁用）
        packs_root: str | Path = "packs",
        track: str = "ctf",
        capabilities: list[str] | None = None,
        role: str = "_generalist",
        session_name: str | None = None,
        capability_prompt: str = "",
        config: AgentConfig | None = None,
        decompiler=None,
        artifacts_dir: str | Path | None = None,
        existing_session: dict | None = None,
    ):
        self.project_id = project_id
        self.bb = bb
        self.gateway = gateway
        self.llm = llm
        self.planner_llm = planner_llm
        self.packs_root = Path(packs_root)
        self.track = track
        self.capabilities = capabilities or []
        self.config = config or AgentConfig()
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None

        role_data = load_role(self.packs_root, track, role)
        # 角色文件缺失时 load_role 已回退 _generalist；role-rules/会话名按实际角色走
        self.role_name = role if (
            self.packs_root / "tracks" / track / "roles" / f"{role}.yaml").is_file() \
            else "_generalist"
        self.role = role_data
        self._apply_role_limits(role_data)

        if existing_session is not None:
            # rehydrate（服务重启/孤儿窗）：附着既有 sessions 行，不新建、不重置状态。
            # 角色 yaml 仍按当盘文件重载（软边界可能已变）；E8 起落盘的暂停快照
            # 会经 sessions.meta 指针载回（见 _load_persisted_snapshot）。
            self.session = dict(existing_session)
        else:
            self.session = bb.register_session(
                project_id, session_name or f"{track}/{self.role_name}", role=self.role_name)
        self.tq = TaskQueue(bb)
        self.dispatcher = ToolDispatcher(
            bb, gateway, self.tq,
            project_id=project_id, session_id=self.session["id"], author=self.session["id"],
            decompiler=decompiler, artifacts_dir=artifacts_dir,
            packs_root=packs_root, track=track, capabilities=self.capabilities,
            allowed_tools=self.config.allowed_tools, max_runtime=self.config.max_runtime,
            allowed_task_types=load_task_types(self.packs_root, track).keys(),
            max_steps=self.config.max_steps,
        )
        self.registry: SkillRegistry | None = None
        self.capability_prompt = capability_prompt

        # 会话控制面（DESIGN.md §3）：API 线程只置 Event，worker 线程在步边界消费
        self._pause_req = threading.Event()
        self._abort_req = threading.Event()
        self._resume_state: dict[str, Any] | None = None
        # 快照 = {system, messages, objective, task_id, next_step}——暂停时保存，恢复续跑
        self.paused = False            # 暂停态（含快照暂停与空闲暂停），恢复/中断后复位
        self._stop_after_task = False  # 硬中断后让 worker 循环退出的一次性闸门
        # 批 5（§6.8）：worker 退出原因信号——True=最后一次 claim 队列为空（可触发
        # L2 续 tick）；暂停/中断退出保持 False。run_next_task 每次认领前置 False。
        self.last_claim_idle = False
        self._heartbeat: _LeaseHeartbeat | None = None  # 当前任务的租约心跳
        # 撤回传播：已在首条消息告过警的任务（防续跑/重入重复告警）
        self._stale_alerted: set[str] = set()
        if existing_session is not None:
            # E8：暂停快照已落盘 → 载回 _resume_state 并置回暂停态
            #（服务重启后 resume 仍可从快照+步数断点续跑；无快照行为同旧版）
            self._resume_state = self._load_persisted_snapshot()

    # ---------- 租约心跳（A1：长任务防 30 分钟 TTL 过期被重领双跑） ----------

    def _start_heartbeat(self, task_id: str) -> None:
        self._stop_heartbeat()
        hb = _LeaseHeartbeat(
            self.tq, self.session["id"], task_id, self.config.lease_renew_seconds)
        self._heartbeat = hb
        hb.start()

    def _stop_heartbeat(self) -> None:
        hb, self._heartbeat = self._heartbeat, None
        if hb is not None:
            hb.stop()

    def _apply_role_limits(self, role_data: dict) -> None:
        """角色 yaml 增强字段 → AgentConfig（§6.6）。

        角色限制只可能比全局/工厂配置更严：max_steps 取 min；tools/max_runtime/
        default_noise 以角色值为准（null/缺省 = 不加该层过滤）。"""
        max_steps = role_data.get("max_steps")
        if isinstance(max_steps, int) and max_steps > 0:
            self.config.max_steps = min(self.config.max_steps, max_steps)
        tools = role_data.get("tools")
        if tools:  # 非空列表才限制（null/[] = 不限制）
            self.config.allowed_tools = list(tools)
        max_runtime = role_data.get("max_runtime")
        if max_runtime in {"host", "wsl", "docker", "sandbox"}:
            self.config.max_runtime = max_runtime
        if role_data.get("default_noise") in {"passive", "low", "medium", "high"}:
            self.config.max_noise = role_data["default_noise"]

    # ---------- 系统提示 ----------

    def build_system_prompt(self, objective: str, skill_context: str = "") -> str:
        parts = [
            build_rules_preamble(
                self.packs_root, track=self.track, capabilities=self.capabilities,
                owner_tags=self.config.owner_tags, role=self.role_name),
            self._role_block(),
        ]
        if self.capability_prompt:
            parts.append(self.capability_prompt)
        if skill_context:
            parts.append(f"# 技能指引\n{skill_context}")
        parts.append(f"# 当前任务\n{objective}")
        parts.append(STRICT_PROMPT_TAIL)
        return "\n\n".join(parts)

    def _role_block(self) -> str:
        """角色块：职责（description）+ 人设（persona）+ 软边界自陈。"""
        lines = ["# 角色"]
        if self.role.get("description"):
            lines.append(f"职责：{self.role['description']}")
        if self.role.get("persona"):
            lines.append(str(self.role["persona"]))
        bounds = []
        if self.config.max_noise:
            bounds.append(f"噪声预算上限 {self.config.max_noise}")
        if self.config.max_runtime:
            bounds.append(f"运行时上限 {self.config.max_runtime}")
        if self.config.allowed_tools is not None:
            bounds.append(f"工具白名单 {', '.join(self.config.allowed_tools)}")
        if bounds:
            lines.append("软边界（越界须停止并请人类处理）：" + "；".join(bounds))
        return "\n".join(lines)

    def load_skills(self) -> None:
        self.registry = SkillRegistry(self.packs_root)
        self.registry.load()

    def skill_context_for(self, query: str, features: list[str] | None = None,
                          file_features: list[str] | None = None,
                          task_id: str | None = None) -> str:
        """入口技能路由：角色白名单窄化 → 评分最高技能正文 + 知识库源登记。

        候选集 = 项目启用能力包技能 ∪ 场景轨技能（§4.5），角色 skills 白名单再窄化。
        每个任务落一条 skill.routed 审计事件（未命中 name=null，C5 可观测）。"""
        if self.registry is None:
            self.load_skills()
        assert self.registry is not None
        role_skills = self.role.get("skills")
        pack_set = set(self.capabilities) | {self.track}
        router = SkillRouter(self.registry)
        hits = router.route(query=query, features=features, file_features=file_features,
                            role_skills=role_skills, packs=pack_set, top_k=1)
        if not hits:
            self.bb.append_event(
                self.project_id, "skill.routed",
                {"name": None, "score": 0, "matched": [], "task_id": task_id,
                 "query": (query or "")[:200]},
                session_id=self.session["id"], author=self.session["id"])
            return ""
        top = hits[0]
        sk = top.skill
        self.bb.append_event(
            self.project_id, "skill.routed",
            {"name": sk.name, "pack": sk.pack, "score": top.score,
             "matched": top.matched, "breakdown": top.breakdown,
             "task_id": task_id, "query": (query or "")[:200]},
            session_id=self.session["id"], author=self.session["id"])
        sources = [{"id": s.id, "root": str(s.root)}
                   for s in load_kb_sources(self.packs_root, self.capabilities)]
        source_json = json.dumps(sources, ensure_ascii=False)
        return (
            f"当前命中技能: {sk.name}（{sk.description}）\n\n{sk.body()}\n\n"
            f"## 可用知识库源（kb_open 按源内相对路径打开，禁止通读）\n{source_json}"
        )

    # ---------- 运行 ----------

    def request_pause(self) -> None:
        """请求软暂停（DESIGN.md §3）：当前 LLM 步做完即停，恢复后从快照续跑。"""
        self._pause_req.set()

    def request_abort(self) -> None:
        """请求硬中断：当前步做完 → 任务 fail（人工中断）→ 会话空闲。"""
        self._abort_req.set()
        self._pause_req.clear()  # 中断优先，避免同一检查点被当成暂停

    def run_task(self, objective: str, features: list[str] | None = None,
                 file_features: list[str] | None = None,
                 task_id: str | None = None) -> str:
        """执行一个目标（任务）。task_id 给定时：open 则自动认领，已被他人持有则拒绝。
        返回空串 = 暂停/中断退出（任务收尾已由检查点处理，不得再走 _finalize）。"""
        stale_refs: list[str] = []
        if task_id:
            task = self.tq.get_task(task_id)
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")
            if task["claimed_by"] != self.session["id"]:
                if task["status"] == "open":
                    self.tq.claim(task_id, self.session["id"],
                                  lease_minutes=self.config.lease_minutes)
                    task = self.tq.get_task(task_id)
                else:
                    raise ValueError(
                        f"任务 {task_id} 由 {task['claimed_by']} 持有（{task['status']}），"
                        f"本会话不可执行")
            # 撤回传播（§6.7 的 1.6）：认领/接手时若依据已被推翻，首条消息必带
            # 强制自警告警（open 任务只挂标无私信，这里按 stale_refs 现场补水）。
            # 同会话同任务只告一次，暂停快照续跑不重复（快照里已有告警）。
            stale_refs = list(task.get("stale_refs") or [])
            self.dispatcher.current_task_id = task_id
            self._start_heartbeat(task_id)
        system = self.build_system_prompt(
            objective, self.skill_context_for(objective, features, file_features,
                                              task_id=task_id))
        messages: list[dict[str, Any]] = []
        if stale_refs and task_id not in self._stale_alerted:
            drained = self.bb.inbox_drain(self.project_id, self.session["id"])
            notice = self._basis_stale_notice(stale_refs, drained)
            if notice:
                messages.append({"role": "user", "content": notice})
                self._stale_alerted.add(task_id)
            update_notice = self._finding_update_notice(drained)
            if update_notice:
                messages.append({"role": "user", "content": update_notice})
        messages.append({"role": "user", "content": objective})
        summary = self._loop(system, messages, objective)
        if summary is None:  # 暂停（心跳保留，继续占任务）/中断（_abort_current_task 已停心跳）
            return ""
        self._finalize()
        return summary

    def run_next_task(self) -> str | None:
        """Worker 循环入口：认领下一个匹配角色 task_types 的任务并执行。
        暂停/中断请求 → 不领新任务并落状态；有快照 → 优先续跑被暂停的任务。"""
        if self._abort_req.is_set():
            self._abort_current_task()   # 空闲路径：无任务则只清标志 + 落审计
            return None
        if self._pause_req.is_set():
            self._pause_req.clear()
            self._enter_paused()         # 任务间暂停：无快照，恢复后正常认领
            return None
        if self._stop_after_task or self.paused:
            self._stop_after_task = False
            return None                  # 中断收尾闸门 / 暂停态不领新任务
        if self._resume_state is not None:
            st, self._resume_state = self._resume_state, None
            self._clear_snapshot()  # 快照已消费（文件+指针清理；失败留垃圾不影响主流程）
            task = self.tq.get_task(st["task_id"])
            if (task and task["claimed_by"] == self.session["id"]
                    and task["status"] == "claimed"):
                self.dispatcher.current_task_id = st["task_id"]
                if self._heartbeat is None:  # 兜底：暂停期心跳本应保留，缺失则补起
                    self._start_heartbeat(st["task_id"])
                # E8：暂停期积压的私信（含 human_note 人类引导）随快照恢复一并注入
                drained = self.bb.inbox_drain(self.project_id, self.session["id"])
                for notice in (self._basis_stale_notice([], drained),
                               self._finding_update_notice(drained),
                               self._human_note_notice(drained)):
                    if notice:
                        st["messages"].append({"role": "user", "content": notice})
                summary = self._loop(st["system"], st["messages"], st["objective"],
                                     start_step=st["next_step"])
                if summary is None:
                    return None          # 恢复后立刻又被暂停/中断
                self._finalize()
                return summary
            # 快照失效（租约被回收/他人持有）：丢弃快照，落到正常认领
        self.last_claim_idle = False  # 进入认领：暂停/中断早退路径不得残留旧 True
        task_id = self.tq.claim_next(
            self.project_id, self.session["id"],
            allowed_task_types=self.config.task_types,
            lease_minutes=self.config.lease_minutes,
            max_noise=self.config.max_noise)
        if task_id is None:
            self.last_claim_idle = True  # 批 5：仅这种退出才允许触发 L2 续 tick
            return None
        task = self.tq.get_task(task_id)
        return self.run_task(task["objective"], task_id=task_id)

    # ---------- 内部 ----------

    def _loop(self, system: str, messages: list[dict[str, Any]], objective: str,
              start_step: int = 1) -> str | None:
        """返回 None = 暂停/中断退出（调用方不得 finalize）；其余返回任务总结。

        步数上界读 dispatcher.max_steps（E8）：request_steps 增补写在那里，
        本会话内跨任务生效。"""
        max_steps = self.dispatcher.max_steps
        step = start_step
        # while 而非 range（E8）：request_steps 在步内增补预算后，循环上界随之
        # 前移——耗尽轮当场自救（步号不增）也成立，不会因 range 预计算被截断。
        while step <= max_steps:
            self.dispatcher.set_step(step)
            # 步数感知（E8）：剩余 <20 步起每步边界注入提醒——模型对预算无感是
            # 步数耗尽事故的第一根因；预算吃紧时由模型自行 request_steps 或收尾。
            remaining = max_steps - step
            if remaining < 20:
                messages.append({"role": "user", "content":
                    f"⏳ 预算剩余 {remaining} 步，请规划收尾；如确需更多步数，"
                    "调 request_steps 申请增补（一次 +200，剩余 ≤20 步才放行）。"})
            if self._stuck(step):
                messages.append({"role": "user", "content": self._advisor_prompt(messages, objective)})
                self.dispatcher.last_progress_step = step  # 顾问干预后重置观察窗
            resp = self.llm.chat(messages, system=system, tools=AGENT_TOOLS)
            self._record_usage(resp, source="agent", llm_obj=self.llm)
            if resp.thinking and resp.thinking.strip():
                # 思考过程落事件流（DESIGN.md §12）：折叠一行摘要、展开看全文
                self.bb.append_event(
                    self.project_id, "llm.thinking",
                    {"thinking": resp.thinking, "step": step},
                    session_id=self.session["id"], author=self.session["id"])
            messages.append({"role": "assistant", "content": resp.raw.get("content", [])})
            if not resp.tool_calls:
                # 纯文本回复：视为停等，提示其用 finish 或继续干活
                messages.append({"role": "user",
                                 "content": "（请继续执行：调用工具干活，或调用 finish 结束并总结）"})
            else:
                for tc in resp.tool_calls:
                    result_text = self.dispatcher.dispatch(tc.name, tc.arguments)
                    messages.append(self.llm.tool_result_message(tc, result_text))
                self._trim(messages)
            if self.dispatcher.finished:
                return self.dispatcher.summary
            ctrl = self._control_point(system, messages, objective, step)
            if ctrl is not None:
                return None  # 检查点消费了暂停/中断（§3 会话控制）
            step += 1
            max_steps = self.dispatcher.max_steps  # 步内 request_steps 增补 → 上界前移
        # 步数耗尽（E8）：不再自动 fail——快照 + 自动步数暂停，任务保持 claimed、
        # 心跳继续，等人类在直播间「继续」（恢复时可附引导语/追加预算）。
        self._budget_pause(system, messages, objective, self.dispatcher.max_steps)
        return None

    # ---------- 会话控制（DESIGN.md §3：暂停/恢复/中断） ----------

    def _control_point(self, system: str, messages: list[dict[str, Any]],
                       objective: str, step: int) -> str | None:
        """LLM 步边界检查点。返回 None=继续；否则消费请求并落状态，_loop 以 None 退出。"""
        if self._abort_req.is_set():
            self._abort_current_task()
            return "aborted"
        if self._pause_req.is_set():
            self._pause_req.clear()
            self._resume_state = {
                "system": system, "messages": messages, "objective": objective,
                "task_id": self.dispatcher.current_task_id, "next_step": step + 1,
                "max_steps": self.dispatcher.max_steps,  # E8：暂停时预算随快照走
                "reason": "pause",
            }
            self._enter_paused()
            return "paused"
        if self._task_gone():
            # A1：任务在看板被删除（claimed 步边界取消）——当前步已做完，按中断收尾，
            # 不再调 fail（行已不存在，task.deleted 即审计）。
            self._abort_current_task()
            return "aborted"
        # 系统私信（不打断当前工具调用；drain 已原子标记已读）：下一个步边界注入。
        # 撤回（basis_stale，强制三选一）与增补（finding_update，信息式）严格分语义。
        fresh = self.bb.inbox_drain(self.project_id, self.session["id"])
        notice = self._basis_stale_notice([], fresh)
        if notice:
            messages.append({"role": "user", "content": notice})
        update_notice = self._finding_update_notice(fresh)
        if update_notice:
            messages.append({"role": "user", "content": update_notice})
        note_notice = self._human_note_notice(fresh)
        if note_notice:
            messages.append({"role": "user", "content": note_notice})
        return None

    def _human_note_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「人类引导」消息（E8，kind='human_note'）：信息式注入，不打断当前
        工具调用；多条按序各占一行。"""
        notes = [str((r.get("payload") or {}).get("text", "")).strip()
                 for r in inbox_rows if r.get("kind") == "human_note"]
        notes = [n for n in notes if n]
        if not notes:
            return None
        return "\n".join(["💬 人类引导："] + [f"- {n}" for n in notes])

    def _finding_update_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「发现增补」信息式消息（A4，kind='finding_update'）：不强制任何动作。
        未读去重已保证同 ref 至多一条，这里仍按 ref 防御性聚合。"""
        items: dict[str, dict[str, Any]] = {}
        for row in inbox_rows:
            if row.get("kind") != "finding_update" or not row.get("ref_id"):
                continue
            items.setdefault(row["ref_id"], row.get("payload") or {})
        if not items:
            return None
        lines = [
            "ℹ 发现增补通知（信息式，无需中断当前工作；若增补内容影响你的路线可自行调整）：",
        ]
        for ref, p in items.items():
            head = f"- {ref}「{p.get('title', '')}」"
            changes = p.get("changes") or []
            if changes:
                head += f"：{ '、'.join(str(c) for c in changes) }"
            lines.append(head)
        return "\n".join(lines)

    def _basis_stale_notice(
        self, task_refs: list[str], inbox_rows: list[dict[str, Any]],
    ) -> str | None:
        """拼「依据被推翻」强制自评消息。task_refs 从 finding 行现场补水
        （open 任务认领时无私信行）；inbox_rows 是撤回路由投递的私信（信息更全）。
        同一 ref 优先用私信 payload；返回 None=本会话没有待告内容。"""
        items: dict[str, dict[str, Any]] = {}
        for ref in task_refs:
            f = self.bb.get_finding(self.project_id, ref)
            if f is None or f.get("status") != "false-positive":
                continue
            ev = f.get("evidence") or {}
            items[ref] = {
                "finding_id": ref, "title": f.get("title", ""),
                "vuln_class": f.get("vuln_class", ""),
                "by": "人工/系统复核", "note": str(ev.get("note", "")),
            }
        for row in inbox_rows:
            if row.get("kind") != "basis_stale" or not row.get("ref_id"):
                continue
            ref = row["ref_id"]
            p = row.get("payload") or {}
            items.setdefault(ref, p)
            if p.get("by"):
                items[ref]["by"] = p["by"]
        if not items:
            return None
        lines = [
            "⚠ 依据撤回（系统强制自评，DESIGN §6.7 的 1.6）",
            "本任务引用的以下发现刚被标记为误报：",
        ]
        for it in items.values():
            head = f"- {it.get('finding_id', '')}「{it.get('title', '')}」"
            if it.get("vuln_class"):
                head += f"（{it['vuln_class']}）"
            lines.append(head)
            meta = []
            if it.get("by"):
                meta.append(f"推翻人：{it['by']}")
            note = str(it.get("note", "")).strip()
            if note:
                meta.append(f"误报理由：{note}")
            if meta:
                lines.append("  " + "；".join(meta))
        lines.append(
            "你必须立即自评并在下一步明确三选一：① 带理由继续（说明为何你的结论"
            "不依赖该依据仍成立）；② 调用 fail_task 终止本任务；③ 改道其他攻击面。")
        return "\n".join(lines)

    def _enter_paused(self) -> None:
        """落 paused 状态 + 审计事件。任务保持 claimed，恢复后从快照（如有）续跑。
        E8：有快照时同步落盘（workspace 文件 + sessions.meta 指针），重启可恢复。"""
        self.paused = True
        try:
            self.bb.set_session_status(self.session["id"], "paused")
        except Exception:  # noqa: BLE001
            log.exception("set_session_status(paused) 失败")
        self._persist_snapshot()
        self.bb.append_event(
            self.project_id, "session.paused",
            {"session_id": self.session["id"], "task_id": self.dispatcher.current_task_id},
            session_id=self.session["id"], author=self.session["id"])

    def _budget_pause(self, system: str, messages: list[dict[str, Any]],
                      objective: str, max_steps: int) -> None:
        """步数耗尽自动暂停（E8）：复用软暂停设施，快照标 reason=budget，
        next_step 停在耗尽步（恢复不增补预算时也至少还能走一轮对话，
        模型可当场 request_steps 自救）。任务保持 claimed、心跳继续。"""
        self._resume_state = {
            "system": system, "messages": messages, "objective": objective,
            "task_id": self.dispatcher.current_task_id,
            "next_step": self.dispatcher.step,  # 耗尽步号 = 恢复断点
            "max_steps": max_steps, "reason": "budget",
        }
        self._enter_paused()
        self.bb.append_event(
            self.project_id, "session.budget_paused",
            {"session_id": self.session["id"], "task_id": self.dispatcher.current_task_id,
             "max_steps": max_steps},
            session_id=self.session["id"], author=self.session["id"])

    # ---------- 暂停快照落盘（E8：修纯内存不恢复缺口） ----------

    def _snapshot_path(self) -> Path | None:
        """快照文件路径：<workspace>/<pid>/snapshots/<sid>.json；无 artifacts_dir
        （部分测试/直跑）时返回 None = 保持纯内存。"""
        if self.artifacts_dir is None:
            return None
        return self.artifacts_dir.parent / "snapshots" / f"{self.session['id']}.json"

    def _persist_snapshot(self) -> None:
        """暂停快照落盘：写 workspace 文件并在 sessions.meta 存指针。
        落盘失败只降级为纯内存快照（本进程内 resume 仍可用），不阻断暂停。"""
        st = self._resume_state
        path = self._snapshot_path()
        if st is None or path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({**st, "session_id": self.session["id"]},
                           ensure_ascii=False),
                encoding="utf-8")
            self.bb.set_session_meta(self.session["id"],
                                     {"resume_snapshot": path.name})
        except Exception:  # noqa: BLE001
            log.exception("暂停快照落盘失败（会话 %s）", self.session["id"])

    def _load_persisted_snapshot(self) -> dict | None:
        """rehydrate：按 sessions.meta 指针读回暂停快照；命中即置回暂停态。
        文件缺失/损坏 → 返回 None（走旧版无快照路径，任务靠租约过期回队列）。"""
        path = self._snapshot_path()
        if path is None:
            return None
        meta = self.session.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        if not isinstance(meta, dict) or not meta.get("resume_snapshot"):
            return None
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("暂停快照读取失败（会话 %s）", self.session["id"])
            return None
        if not isinstance(st, dict) or "messages" not in st:
            return None
        if isinstance(st.get("max_steps"), int) and st["max_steps"] > 0:
            self.dispatcher.max_steps = st["max_steps"]  # 暂停时的预算随快照还原
        self.paused = True
        return st

    def _clear_snapshot(self) -> None:
        """快照已消费（续跑/中断）：删文件 + 清 meta 指针。失败只留垃圾文件，
        不影响主流程（下次暂停会覆盖同名文件）。"""
        path = self._snapshot_path()
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("暂停快照文件清理失败（会话 %s）", self.session["id"])
        try:
            self.bb.set_session_meta(self.session["id"], {"resume_snapshot": None})
        except Exception:  # noqa: BLE001
            log.exception("暂停快照指针清理失败（会话 %s）", self.session["id"])

    def _task_gone(self) -> bool:
        """A1：当前任务行是否已从看板删除（claimed 任务可被人工取消，步边界感知）。"""
        task_id = self.dispatcher.current_task_id
        return bool(task_id) and self.tq.get_task(task_id) is None

    def _abort_current_task(self) -> None:
        """硬中断收尾：任务 fail（人工中断，不回队列）→ 会话空闲 + 审计。
        任务行已被删除（看板取消）时跳过 fail——task.deleted 事件即审计。"""
        task_id = self.dispatcher.current_task_id
        if task_id is None and self._resume_state:
            task_id = self._resume_state.get("task_id")  # 空闲暂停态被中断：快照任务也要收尾
        self._resume_state = None
        self._clear_snapshot()  # E8：中断即丢弃落盘快照（文件+指针）
        self.paused = False
        self._pause_req.clear()
        self._abort_req.clear()
        self._stop_after_task = True  # 让 worker 循环退出，不再领新任务
        self._stop_heartbeat()
        note = "人工中断"
        if task_id:
            if self.tq.get_task(task_id) is None:
                note = "任务已被删除"
            else:
                try:
                    self.tq.fail(task_id, self.session["id"], "人工中断")
                except Exception:  # noqa: BLE001
                    log.exception("中断 fail 任务失败")
            self.dispatcher.current_task_id = None
        try:
            self.bb.set_session_status(self.session["id"], "idle")
        except Exception:  # noqa: BLE001
            log.exception("set_session_status(idle) 失败")
        self.bb.append_event(
            self.project_id, "session.aborted",
            {"session_id": self.session["id"], "task_id": task_id, "note": note},
            session_id=self.session["id"], author=self.session["id"])

    def _stuck(self, step: int) -> bool:
        return (self.planner_llm is not None
                and step - self.dispatcher.last_progress_step >= self.config.stuck_after)

    def _advisor_prompt(self, messages: list[dict], objective: str) -> str:
        """策略顾问（§3）：规划上下文看执行摘要，给换思路建议——不是换 Agent。"""
        events = self.bb.recent_events(self.project_id, limit=30)
        digest = json.dumps(
            [{"kind": e["kind"], "payload_head": json.dumps(e["payload"], ensure_ascii=False)[:120]}
             for e in events], ensure_ascii=False)
        try:
            resp = self.planner_llm.chat(
                [{"role": "user", "content":
                  f"目标: {objective}\n最近事件摘要: {digest}\n"
                  "执行已多步无进展。给出 3 条以内换思路建议，直接可执行。"}],
                system="你是策略顾问，负责打破执行僵局。简洁、具体、不重复已失败路径。")
            advice = resp.text
            self._record_usage(resp, source="planner", llm_obj=self.planner_llm)
        except Exception as e:  # noqa: BLE001
            log.warning("策略顾问调用失败: %s", e)
            advice = "（顾问不可用）回顾黑板去重情况，换一个未尝试的攻击面。"
        return f"[策略顾问]\n{advice}"

    def _record_usage(self, resp: Any, *, source: str, llm_obj: Any) -> None:
        """每次 chat 后用量记账（§6.8）：累加 orchestrator_state + llm.usage 事件；
        全 0 用量（ScriptedLLM/厂商未回）在底层跳过。记账失败不阻断主循环。"""
        try:
            record_llm_usage(
                self.bb, self.project_id, resp.usage, source=source,
                session_id=self.session["id"], model=getattr(llm_obj, "model", ""))
        except Exception:  # noqa: BLE001
            log.exception("用量记账失败")

    def _trim(self, messages: list[dict[str, Any]]) -> None:
        """上下文预算：超限则把旧 tool_result 内容替换为占位（保留结构）。"""
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        if total <= self.config.context_char_budget:
            return
        for m in messages[:-8]:  # 保留最近 8 条完整
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for block in m["content"]:
                    if block.get("type") == "tool_result" and len(block.get("content", "")) > 200:
                        block["content"] = block["content"][:200] + "…[已截断]"

    def _finalize(self) -> None:
        """会话收尾：任务未收尾 → fail（防 lease 占坑）；落 session.finished 事件。"""
        self._stop_heartbeat()
        if self.dispatcher.current_task_id:
            try:
                self.tq.fail(self.dispatcher.current_task_id, self.session["id"],
                             "会话结束但任务未收尾，自动标记失败")
            except Exception:  # noqa: BLE001
                log.exception("自动 fail 任务失败")
        self.bb.append_event(
            self.project_id, "session.finished",
            {"session_id": self.session["id"], "summary": self.dispatcher.summary[:500]},
            session_id=self.session["id"], author=self.session["id"],
        )
