"""主代理 Orchestrator（DESIGN.md §6.4）——普通 LLM 会话 + 特殊工具集，不直接干脏活。

职责四件套：
- 监控：每 tick 回收过期 lease（防会话挂死占坑），把队列空转/低产出摆到 LLM 面前
- 派生：新发现 → 新任务（LLM 决策，publish_task 落地）
- 开窗：唯一有权启动新 AI 会话的角色（角色白名单 + 会话数上限约束）
- 汇总：把项目态势写成简报，落 project.digest 事件（人类了解全局的入口）

一轮协调 = tick()：expire_leases → 态势收集 → LLM 决策循环（专用工具集）→ done
人类随时插手：直接向 TaskQueue 发布/取消任务即可，Orchestrator 只消费不垄断（§6.4）。
"""

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.autonomy import record_llm_usage
from core.blackboard import Blackboard, TaskQueue
from core.blackboard.tasks import _check_task_type
from core.skills.roles import load_role
from core.skills.taxonomy import GENERIC_TASK_TYPE, load_task_types, track_dir

log = logging.getLogger(__name__)


ORCH_SYSTEM_PROMPT = """你是项目主代理（Orchestrator），职责是监控、派生、开窗、汇总。
你不亲自执行任何命令或分析——脏活全部发布成任务，由 Agent 会话认领执行。

## 本轮态势
{overview}
{role_catalog}
{autonomy_notice}
## 可用工具
- publish_task：发布任务。task_type 必须是场景轨 task_types.yaml 已注册类型（未注册会被拒收——拼错的类型会让任务饿死），决定哪些角色能认领（先看角色目录与已有会话）；noise_budget 缺省取该类型注册表默认值；会产生噪声的动作（主动探测/执行样本）必须给 conflict_keys；任务若以某发现为依据（如「验证 find-x」），把该发现 id 填 refs——该发现事后被推翻时，执行者会立刻收到强制自评通知。
- spawn_session：开一个新 AI 会话（受角色白名单 {allowed_roles} 与上限 {max_sessions} 个约束）；按角色目录里的职责对号入座。
- write_digest：写项目简报（给人类看的全局摘要：进展/发现/风险/下一步）。
- done：结束本轮协调。

## 主代理纪律（硬规则）
1. 不执行命令、不写分析结论——一切经任务派发。
2. 遵循「分析-分解-分派」：复杂目标先在心里拆成自足的小任务，再按角色目录对号分派；
   publish_task 时在 scope 写明**建议认领角色**，并给初始 priority（0-9，整数；小者优先：
   被依赖的前置/为他人解阻塞的任务给 0-2，常规任务 3-5，可延后的 6-9）。
3. 新发现 severity>=medium 且 unverified 的，应派生验证任务，别让它烂在黑板里。
4. 开窗被拒（白名单/上限）即改道：复用现有会话或调整任务。
5. 距上次简报 >= {digest_every} 轮时，本轮必须 write_digest。
6. 决策完毕调用 done。
7. 任务拆解与分批（C1）：大目标（如全资产侦察）拆解为自足子任务——先发布父任务拿到
   task_id，再发布子任务并把 parent_id 指向它（**深度 1 层**：子任务不可再拆）；每轮
   发布 ≤{max_publish_per_tick} 个（分批 3-5 个/轮，按建议角色与优先级）；队列空退后
   下一轮 tick 续批（态势里有 uncovered 资产清单可对照发批）；资产全覆盖前不要 done。
"""


# L1（任务自动·开窗审批）系统提示追加段：spawn 不即时开窗，转人类审批
L1_AUTONOMY_NOTICE = """## 自主档位 L1（任务自动·开窗审批）
spawn_session 不会立刻开窗：请求进入人类审批收件箱，人类批准后系统自动建窗并开跑。
- reason 必须写清（为什么开这个角色、要它做什么），审批人只看得到 role+reason；
- 等待审批期间可继续 publish passive 任务，或 done 结束本轮；批准/拒绝结果下轮 tick 经事件可见。"""


# L0（全手动）系统提示追加段（批 6）：publish/spawn 只产提案，不写实体
L0_AUTONOMY_NOTICE = """## 自主档位 L0（全手动·提案模式）
你不能直接派任务或开窗：publish_task / spawn_session 只生成人类提案（orch.proposed 事件），
人类在事件流逐条「采纳」后才真正落地（任务以人类名义入队、窗由人类开）。
- 提案不消耗任何预算、不占会话上限，但参数校验照跑：task_type 必须是本轨注册类型、
  非 passive 仍须 conflict_keys、role 仍受白名单/上限约束，填错会被拒收；
- 一轮可提多条；write_digest 照常写简报；决策完毕 done。"""


ORCH_TOOLS: list[dict[str, Any]] = [
    {
        "name": "publish_task",
        "description": "发布任务到队列，由匹配的 Agent 会话认领。",
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "任务目标（自包含，执行者看不到对话上下文）"},
                "task_type": {"type": "string", "description": "任务类型，必须是场景轨注册表内的合法类型"},
                "scope": {"type": "string", "description": "限定范围（如目标资产）"},
                "noise_budget": {"type": "string", "enum": ["passive", "low", "medium", "high"],
                                 "description": "缺省取 task_types.yaml 中该类型的默认噪声预算"},
                "conflict_keys": {"type": "array", "items": {"type": "string"},
                                  "description": "非 passive 必填，如 [\"binary:crackme.elf\"]"},
                "priority": {"type": "integer", "description": "0 最高，2 默认"},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "本任务依据的既有发现 id（find- 前缀）；"
                                        "依据被推翻时执行者会收到强制自评通知。正文里"
                                        "直接写 find-id 也会被服务端自动抽取，显式填写更准"},
                "parent_id": {"type": "string",
                              "description": "父任务 id（C1 任务拆解）：大目标拆解时先发布父任务，"
                                             "再把子任务的 parent_id 指向它（深度 1 层，子任务不可再拆）"},
            },
            "required": ["objective", "task_type"],
        },
    },
    {
        "name": "spawn_session",
        "description": "启动一个新 AI 会话（开窗）。role 决定领域人设与可认领任务类型。"
                       "提案前先确认现有会话不足以覆盖：有空闲/可唤醒的既有会话时"
                       "优先复用（开窗请求与任务认领存在赛跑——审批落地时任务可能已被"
                       "既有 worker 认领），仅当既有会话全部在忙且 open 任务无人认领时才开窗。",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "reason": {"type": "string",
                           "description": "开窗理由（为什么开这个角色、要它做什么）；"
                                          "L1 档必须填写——请求会进人类审批收件箱，"
                                          "审批人只看得到 role+reason"},
            },
            "required": ["role"],
        },
    },
    {
        "name": "write_digest",
        "description": "写项目简报并落黑板（project.digest 事件），人类了解全局的入口。",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
    {
        "name": "done",
        "description": "结束本轮协调。",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# A5：优先级重排专用一轮（手动按钮 / L2 自动去抖共用）。
# 只给一个工具、只改 open 行；逐行落 task.updated 审计，非法条目服务端跳过。
REPLAN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_priorities",
        "description": "批量重排待认领任务的优先级。只允许针对清单里 status=open 的任务；"
                       "未列出的任务保持原值。priority 为 0-9 的整数，小者优先。",
        "input_schema": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "priority": {"type": "integer",
                                         "description": "0-9 整数；0 最优先，2 常规默认"},
                        },
                        "required": ["task_id", "priority"],
                    },
                },
            },
            "required": ["updates"],
        },
    },
]


REPLAN_SYSTEM_PROMPT = """你是项目主代理（Orchestrator）的优先级重排器，本轮只做一件事：
依据当前态势重排 **待认领（open）** 任务的优先级，然后调用一次 set_priorities。

## 排序原则（0-9，小者优先）
- 0-2：他人任务的前置/能为 blocked 任务解阻塞/高危时效窗口（如目标即将下线）。
- 3-5：常规推进（默认 2-3；新验证发现按严重度收紧）。
- 6-9：可延后的清理、广度收集、nice-to-have。
- 已经 claimed/done/failed 的任务、清单外的 id 一律不要出现在 updates 里
  （服务端也会忽略，乱填只浪费配额）。
- 值与现值相同的任务不必提交；没有任何调整就调 set_priorities(updates=[])。
- 只调一次工具；不要发布任务、不要开窗、不要写简报。

## 待认领任务（仅这些可改）
{open_tasks}
{blocked}
{role_catalog}
"""


@dataclass
class OrchestratorConfig:
    max_steps: int = 12
    allowed_roles: list[str] | None = None  # None = 不限
    max_sessions: int = 4
    digest_every: int = 3   # 每 N 轮至少一份简报
    # C1 编排拆解：单轮发布硬闸（拆解/分批 3-5/轮；worker 空退事件驱动续批）
    max_publish_per_tick: int = 5
    # 批 6（§6.8）：L0 提案模式——publish_task/spawn_session 只发 orch.proposed
    # 事件、不写实体（校验照跑）；API 按实时档位 level=="L0" 注入。
    propose_only: bool = False


class Orchestrator:
    """主代理。session_factory(role) -> AgentSession 由调用方注入（决定 LLM/网关/领域包）。"""

    def __init__(
        self,
        *,
        project_id: str,
        bb: Blackboard,
        llm: Any,
        session_factory: Callable[[str], Any] | None = None,
        config: OrchestratorConfig | None = None,
        packs_root: str | Path = "packs",
        track: str | None = None,
        gate: Callable[[str], str | None] | None = None,
        on_task_published: Callable[[], None] | None = None,
        state_loader: Callable[[], dict] | None = None,
        state_saver: Callable[..., None] | None = None,
        heartbeat: Callable[[], None] | None = None,
        autonomy_provider: Callable[[], dict] | None = None,
    ):
        self.project_id = project_id
        self.bb = bb
        self.tq = TaskQueue(bb)
        self.llm = llm
        self.session_factory = session_factory
        self.config = config or OrchestratorConfig()
        # 批 2 自主闸门（DESIGN §6.8）：gate(action) 每次实时重读项目配置，
        # 返回拒绝原因（sessions_cap / token·task 预算硬闸）；None=不接线（脚本/旧测试）。
        # on_task_published 在自主发任务成功后计数（task_budget 只计自主发布）。
        self.gate = gate
        self.on_task_published = on_task_published
        # 批 3 状态持久化/心跳（DESIGN §6.8、机制 1.9）：state_loader 取持久行，
        # state_saver 落盘 event_cursor/cycles/last_digest_cycle，heartbeat 在每个
        # LLM 步续租 tick 租约；None=脚本/旧测试不接线，纯内存行为不变。
        self.state_loader = state_loader
        self.state_saver = state_saver
        self.heartbeat = heartbeat
        # 批 4（§6.8）：autonomy_provider 实时返回归一化 autonomy dict；
        # level=="L1" 时 spawn_session 转人工审批而非直接开窗。None=脚本/旧测试
        # 不接线（直接开窗），本模块不 import core.autonomy。
        self.autonomy_provider = autonomy_provider
        self.packs_root = Path(packs_root)
        self.track = track  # 项目场景轨：任务类型注册表 / 角色目录 / 饿死检测的数据源
        # 轨注册表（{type: 默认噪声}，含内置 generic）；无 track 时仅 generic
        self.task_types = (load_task_types(self.packs_root, track)
                           if track else {GENERIC_TASK_TYPE: "passive"})
        self.role_catalog = self._load_role_catalog()
        self.live_sessions: dict[str, Any] = {}   # session_id -> AgentSession
        self._last_event_id = 0
        self.cycles = 0
        self.last_digest_cycle = -999
        self._finished = False
        self._summary = ""
        self._actions: list[str] = []
        # 批 3：每 tick 的结构化动作收集（tick 开头重置；构造器预置以便单测直调工具）
        self._published: list[str] = []
        self._spawned: list[dict[str, str]] = []
        self._publish_count = 0  # C1：单轮发布计数（tick 每轮重置）
        self._proposals: list[dict[str, Any]] = []  # 批 6：L0 提案 {op,args,event_id}
        self._digest: str | None = None
        self._warned_starvation: dict[tuple[str, str], dict] = {}
        self._last_stats_starvation: list[dict] = []

    def _orch_tools(self) -> list[dict[str, Any]]:
        """工具表副本：把本轨合法 task_type 作为 enum 下发（让 LLM 一次填对，
        服务端注册表拒收仍是最终护栏）；无 track 时退回原表。"""
        if not self.track:
            return ORCH_TOOLS
        tools = copy.deepcopy(ORCH_TOOLS)
        for t in tools:
            if t["name"] == "publish_task":
                prop = t["input_schema"]["properties"]["task_type"]
                prop["enum"] = list(self.task_types.keys())
                prop["description"] += (
                    "；本轨合法值（enum）：" + "、".join(self.task_types.keys()))
        return tools

    def _load_role_catalog(self) -> list[dict]:
        """扫轨 roles/*.yaml 的 name+description（spawn 开窗决策不再只见角色名）。"""
        if not self.track:
            return []
        base = track_dir(self.packs_root, self.track) / "roles"
        out: list[dict] = []
        if not base.is_dir():
            return out
        for f in sorted(base.glob("*.yaml")):
            data = load_role(self.packs_root, self.track, f.stem)
            out.append({"name": f.stem,
                        "description": str(data.get("description") or "")})
        return out

    def _role_catalog_prompt(self) -> str:
        if not self.role_catalog:
            return ""
        lines = ["## 可开角色目录（spawn 按职责对号入座）"]
        for r in self.role_catalog:
            desc = r["description"] or "（无职责说明）"
            lines.append(f"- {r['name']}: {desc}")
        return "\n".join(lines)

    # ---------- 态势收集 ----------

    def _overview(self, expired_leases: list[str]) -> str:
        stats = self._stats()
        self._last_stats_starvation = stats["tasks"]["starvation"]
        # 游标追赶：未消费 backlog 超过窗口（100）时只喂最新 100 条，但游标一次跳到
        # 当前末端——旧 backlog 永不逐轮回放（走查发现：从 0 起每轮 +100 的爬行 bug）。
        tip = self.bb.latest_event_id(self.project_id)
        if tip - self._last_event_id > 100:
            new_events = self.bb.recent_events(self.project_id, since_id=tip - 100, limit=100)
        else:
            new_events = self.bb.recent_events(
                self.project_id, since_id=self._last_event_id, limit=100)
        self._last_event_id = max(tip, self._last_event_id)
        event_lines = [
            f"  #{e['id']} [{e['kind']}] {e['author']}: {json.dumps(e['payload'], ensure_ascii=False)[:120]}"
            for e in new_events[-40:]
        ]
        return json.dumps(stats, ensure_ascii=False, indent=1) + (
            "\n\n## 本轮过期租约（已回收为 open）\n- " + "\n- ".join(expired_leases)
            if expired_leases else ""
        ) + "\n\n## 自上轮以来的新事件\n" + ("\n".join(event_lines) or "  （无）")

    def _stats(self) -> dict:
        tasks = self.tq.list_tasks(self.project_id)
        by_status: dict[str, int] = {}
        for t in tasks:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        findings = self.bb.list_findings(self.project_id)
        sessions = self.bb.list_sessions(self.project_id)
        open_tasks = [t for t in tasks if t["status"] == "open"]
        starvation = self._starvation_warnings(open_tasks)
        # A5：claimed 任务的 blocked 计划步——编排器据此派生解阻塞任务/重排优先级
        blocked_plan: list[dict] = []
        for t in tasks:
            if t["status"] != "claimed":
                continue
            blocked_steps = [s for s in (t.get("plan") or []) if s.get("status") == "blocked"]
            if blocked_steps:
                blocked_plan.append({
                    "task_id": t["id"], "type": t["task_type"],
                    "blocked": len(blocked_steps),
                    "note": (blocked_steps[0].get("note") or "")[:120],
                })
        return {
            "tasks": {
                "by_status": by_status,
                "open": [{"id": t["id"], "type": t["task_type"], "priority": t["priority"],
                          "noise_budget": t["noise_budget"], "created_at": t["created_at"],
                          "objective": t["objective"][:80]}
                         for t in open_tasks],
                "blocked_plan": blocked_plan,
                "starvation": starvation,
            },
            "findings": [{"id": f["id"], "vuln_class": f["vuln_class"], "title": f["title"][:60],
                          "severity": f["severity"], "status": f["status"]} for f in findings],
            "sessions": [{"id": s["id"], "role": s["role"], "status": s["status"]}
                         for s in sessions],
            "live_windows": len(self.live_sessions),
            **self._assets_view(tasks),
        }

    def _assets_view(self, tasks: list[dict]) -> dict:
        """C1 资产视图：by_type/by_status 计数 + 未覆盖清单（cap 30 带 id）——
        供编排器分批发批对照；判定为提示层，真护栏 = conflict_keys 互斥。
        未覆盖 = host/domain/url/service 资产未被任何 open/claimed 任务的
        conflict_keys/scope/objective 文本提及。"""
        assets = self.bb.list_assets(self.project_id)
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        targetable = []
        for a in assets:
            by_type[a["type"]] = by_type.get(a["type"], 0) + 1
            st = (a.get("status") or "open")
            by_status[st] = by_status.get(st, 0) + 1
            if a["type"] in ("host", "domain", "url", "service"):
                targetable.append(a)
        covered_text = ""
        for t in tasks:
            if t["status"] not in ("open", "claimed"):
                continue
            covered_text += " ".join([
                *(t.get("conflict_keys") or []), t.get("scope") or "",
                t.get("objective") or ""]).lower() + "\n"
        def _asset_keys(a: dict) -> list[str]:
            v = a["value"].lower()
            if a["type"] == "url":
                v = v.split("://", 1)[-1].rstrip("/")
            return [v]

        uncovered = [
            a for a in targetable
            if not any(k in covered_text for k in _asset_keys(a))
        ]
        return {
            "assets": {
                "total": len(assets), "by_type": by_type, "by_status": by_status,
                "uncovered_total": len(uncovered),
                "uncovered": [{"id": a["id"], "type": a["type"], "value": a["value"][:60]}
                              for a in uncovered[:30]],
            },
        }

    def _starvation_warnings(self, open_tasks: list[dict]) -> list[dict]:
        """饿死检测（§6.6）：open 任务 ①task_type 未在轨注册表（拼错/漏配）
        ②注册表有但无专才角色声明可认领（只有 _generalist 兜底）。
        _generalist 的 task_types=null（不过滤）不计入专才覆盖。"""
        if not self.track:
            return []
        covered: dict[str, list[str]] = {}
        for r in self.role_catalog:
            if r["name"] == "_generalist":
                continue
            data = load_role(self.packs_root, self.track, r["name"])
            for tt in data.get("task_types") or []:
                covered.setdefault(tt, []).append(r["name"])
        warnings: list[dict] = []
        for t in open_tasks:
            tt = t["task_type"]
            if tt != GENERIC_TASK_TYPE and tt not in self.task_types:
                reason = f"task_type {tt!r} 未在 {self.track} 轨 task_types.yaml 注册"
            elif tt != GENERIC_TASK_TYPE and not covered.get(tt):
                reason = f"task_type {tt!r} 无专才角色可认领（仅 _generalist 兜底）"
            else:
                continue
            warnings.append({"task_id": t["id"], "task_type": tt,
                             "objective": t["objective"][:80], "reason": reason})
        return warnings

    def _emit_starvation_events(self, warnings: list[dict]) -> None:
        """饿死告警落事件流；同任务同原因只报一次（任务消失后解除，复发可再报）。"""
        current = {(w["task_id"], w["reason"]): w for w in warnings}
        self._warned_starvation = {
            k: v for k, v in self._warned_starvation.items() if k in current}
        fresh = [current[k] for k in current.keys() - self._warned_starvation.keys()]
        self._warned_starvation.update(current)
        if fresh:
            self.bb.append_event(
                self.project_id, "task.starvation", {"warnings": fresh},
                author="orchestrator")

    # ---------- tick ----------

    def tick(self) -> dict[str, Any]:
        """一轮协调：装载持久状态 → 回收过期租约 → 态势收集 → LLM 决策循环
        → done → 落盘游标/轮数。返回结构化结果
        {summary, published, spawned, digest, proposals}（proposals 仅 L0 提案模式非空）。"""
        if self.state_loader is not None:
            state = self.state_loader()
            self._last_event_id = int(state.get("event_cursor", 0))
            self.cycles = int(state.get("cycles", 0))
            self.last_digest_cycle = int(state.get("last_digest_cycle", -999))
        self.cycles += 1
        expired = self.tq.expire_leases()
        if expired:
            log.info("回收过期租约: %s", expired)
        self._finished = False
        self._summary = ""
        self._actions = []
        self._published: list[str] = []
        self._spawned: list[dict[str, str]] = []
        self._proposals = []
        self._digest: str | None = None
        self._publish_count = 0  # C1：单轮发布硬闸计数（直接发布与 L0 提案同闸）
        overview = self._overview(expired)
        self._emit_starvation_events(self._last_stats_starvation)
        auto = self.autonomy_provider() if self.autonomy_provider is not None else None
        # 批 6：config.propose_only（API 按 L0 注入）优先；无 provider 的单测也可直配
        if self.config.propose_only:
            autonomy_notice = L0_AUTONOMY_NOTICE
        elif (auto or {}).get("level") == "L1":
            autonomy_notice = L1_AUTONOMY_NOTICE
        else:
            autonomy_notice = ""
        system = ORCH_SYSTEM_PROMPT.format(
            overview=overview,
            role_catalog=self._role_catalog_prompt(),
            autonomy_notice=autonomy_notice,
            allowed_roles=self.config.allowed_roles or "不限",
            max_sessions=self.config.max_sessions,
            digest_every=self.config.digest_every,
            max_publish_per_tick=self.config.max_publish_per_tick,
        )
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": "第 %d 轮协调开始。请决策本轮动作（监控/派生/开窗/汇总），完成后 done。"
                       % self.cycles,
        }]
        for _step in range(1, self.config.max_steps + 1):
            if self.heartbeat is not None:
                try:
                    self.heartbeat()
                except Exception:  # noqa: BLE001 —— 续租失败不阻断编排
                    log.exception("tick 租约心跳失败")
            resp = self.llm.chat(messages, system=system, tools=self._orch_tools())
            record_llm_usage(
                self.bb, self.project_id, resp.usage, source="orchestrator",
                session_id=None, model=getattr(self.llm, "model", ""))
            messages.append({"role": "assistant", "content": resp.raw.get("content", [])})
            if not resp.tool_calls:
                messages.append({"role": "user", "content": "（请调用工具执行动作，或 done 结束本轮）"})
                continue
            for tc in resp.tool_calls:
                result = self._dispatch(tc.name, tc.arguments)
                messages.append(self.llm.tool_result_message(tc, result))
            if self._finished:
                return self._finish_tick()
        return self._finish_tick(exhausted=True)

    def _finish_tick(self, *, exhausted: bool = False) -> dict[str, Any]:
        """落盘持久游标/轮数并组装结构化结果（两条 tick 出口共用）。"""
        if self.state_saver is not None:
            try:
                self.state_saver(
                    event_cursor=self._last_event_id,
                    cycles=self.cycles,
                    last_digest_cycle=self.last_digest_cycle)
            except Exception:  # noqa: BLE001 —— 落盘失败不丢本轮已落地的实体
                log.exception("编排状态落盘失败")
        fallback = "（步数上限，本轮未显式 done）" if exhausted else "（本轮无动作）"
        return {
            "summary": self._summary or "；".join(self._actions) or fallback,
            "published": list(self._published),
            "spawned": list(self._spawned),
            "digest": self._digest,
            "proposals": list(self._proposals),  # 批 6：仅 L0 propose_only 非空
        }

    # ---------- A5：优先级重排（手动 / L2 自动去抖共用） ----------

    def replan_priorities(self) -> dict[str, Any]:
        """一次性 planner 轮重排 open 任务优先级。

        - 只认 set_priorities 工具调用；无 open 任务直接返回，不调 LLM。
        - 严格校验：task_id 必须存在且仍 open、priority 必须 0-9 的非布尔整数；
          非法/重复/未变条目跳过且不中断；逐行 tq.update_task（每行落 task.updated 审计）。
        - 返回 {updated:[{task_id,old,new}], skipped:[{task_id,reason}]}。
        """
        if self.heartbeat is not None:
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001 —— 续租失败不阻断重排
                log.exception("replan 租约心跳失败")
        tasks = self.tq.list_tasks(self.project_id)
        open_tasks = [t for t in tasks if t["status"] == "open"]
        if not open_tasks:
            return {"updated": [], "skipped": [], "note": "无 open 任务，跳过重排"}

        blocked_lines = [
            f"  - {b['task_id']}（{b['type']}）有 {b['blocked']} 个阻塞步：{b['note']}"
            for b in self._stats()["tasks"]["blocked_plan"]
        ]
        open_lines = [
            f"  - {t['id']} [{t['task_type']}/{t['noise_budget']}] P{t['priority']} "
            f"created={t['created_at']}：{t['objective'][:100]}"
            for t in open_tasks
        ]
        system = REPLAN_SYSTEM_PROMPT.format(
            open_tasks="\n".join(open_lines),
            blocked=("\n## 执行中任务的阻塞步（优先解阻塞）\n" + "\n".join(blocked_lines))
                    if blocked_lines else "",
            role_catalog=self._role_catalog_prompt(),
        )
        resp = self.llm.chat(
            [{"role": "user", "content": "请按排序原则重排待认领任务优先级，调用一次 set_priorities。"}],
            system=system, tools=REPLAN_TOOLS)
        record_llm_usage(
            self.bb, self.project_id, resp.usage, source="orchestrator",
            session_id=None, model=getattr(self.llm, "model", ""))

        raw_updates: list[dict[str, Any]] = []
        for tc in getattr(resp, "tool_calls", None) or []:
            if tc.name != "set_priorities":
                continue
            raw_updates.extend((tc.arguments or {}).get("updates") or [])

        open_map = {t["id"]: t for t in open_tasks}
        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for u in raw_updates:
            tid = u.get("task_id") if isinstance(u, dict) else None
            pr = u.get("priority") if isinstance(u, dict) else None
            if not isinstance(tid, str):
                skipped.append({"task_id": str(tid)[:40], "reason": "bad-task_id"})
                continue
            if tid in seen:
                skipped.append({"task_id": tid, "reason": "duplicate"})
                continue
            seen.add(tid)
            task = open_map.get(tid)
            if task is None:
                # 不存在 / claimed / done / failed：一律忽略（LLM 快照可能过期）
                skipped.append({"task_id": tid, "reason": "not-open-or-unknown"})
                continue
            # bool 是 int 子类，显式拦住（True/False 不是合法优先级）
            if isinstance(pr, bool) or not isinstance(pr, int) or not 0 <= pr <= 9:
                skipped.append({"task_id": tid, "reason": f"bad-priority:{pr!r}"})
                continue
            if pr == task["priority"]:
                skipped.append({"task_id": tid, "reason": "unchanged"})
                continue
            try:
                row = self.tq.update_task(
                    tid, by="orchestrator-replan",
                    allowed_types=(self.task_types.keys() if self.track else None),
                    priority=pr)
            except ValueError as e:  # 并发被改状态等：跳过不中断
                skipped.append({"task_id": tid, "reason": f"update-rejected:{e}"[:120]})
                continue
            updated.append({"task_id": tid, "old": task["priority"], "new": row["priority"]})
        if updated:
            self.bb.append_event(
                self.project_id, "orch.replan_priorities",
                {"updated": updated, "skipped_n": len(skipped)}, author="orchestrator")
        return {"updated": updated, "skipped": skipped}

    # ---------- 工具分发 ----------

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"[错误] 未知工具: {name}"
        try:
            self._actions.append(
                f"{name}({', '.join(f'{k}={json.dumps(v, ensure_ascii=False)[:60]}' for k, v in args.items())})")
            return handler(**args)
        except Exception as e:  # noqa: BLE001
            return f"[工具异常] {type(e).__name__}: {e}"

    def _propose(self, op: str, args: dict[str, Any]) -> str:
        """批 6 L0：校验通过的动作不写实体，只发 orch.proposed 事件供人类行内采纳。"""
        event_id = self.bb.append_event(
            self.project_id, "orch.proposed", {"op": op, "args": args},
            author="orchestrator")
        self._proposals.append({"op": op, "args": args, "event_id": event_id})
        return (f"已提案 #{event_id}（{op}）：不写实体，等待人类在事件流「采纳」；"
                "本轮可继续提案或 done。")

    def _tool_publish_task(
        self, objective: str, task_type: str, scope: str = "",
        noise_budget: str | None = None, conflict_keys: list[str] | None = None,
        priority: int = 2, refs: list[str] | None = None,
        parent_id: str | None = None,
    ) -> str:
        # C1 单轮发布硬闸：直接发布与 L0 提案同闸（拆解/分批 3-5/轮，防一轮刷爆队列）
        if self._publish_count >= self.config.max_publish_per_tick:
            return (f"[拒绝] 本轮发布已达上限 max_publish_per_tick="
                    f"{self.config.max_publish_per_tick}；分批发布——本轮先 done，"
                    "队列空退后下一轮 tick 续批（态势含 uncovered 资产清单）")
        # 批 6 L0 提案模式：校验照跑（噪声/conflict_keys/注册表），但不走预算闸、
        # 不发布、不计数，只发 orch.proposed（人采纳时以 created_by=human 走 POST /tasks）
        if self.config.propose_only:
            if noise_budget is None:
                noise_budget = self.task_types.get(task_type, "passive")
            if noise_budget not in {"passive", "low", "medium", "high"}:
                return f"[拒绝] 非法 noise_budget: {noise_budget}"
            if noise_budget != "passive" and not conflict_keys:
                return "[拒绝] 非 passive 任务必须提供 conflict_keys（active 互斥的依据）"
            try:
                _check_task_type(task_type,
                                 self.task_types.keys() if self.track else None)
                if parent_id:
                    self.tq.check_parent(self.project_id, parent_id,
                                         enforce_depth=True)  # 编排拆解深度 1
            except ValueError as e:
                return f"[拒绝] {e}"
            self._publish_count += 1
            return self._propose("publish_task", {
                "objective": objective, "scope": scope, "task_type": task_type,
                "noise_budget": noise_budget, "priority": priority,
                "conflict_keys": conflict_keys or [], "refs": refs or [],
                "parent_id": parent_id})
        # 自主预算硬闸（§6.8）：每闸门经 gate 回调重读项目配置（task_budget/token_budget）
        if self.gate is not None:
            reason = self.gate("publish_task")
            if reason:
                return f"[拒绝] {reason}"
        # 噪声缺省 = 注册表该类型默认值（§4.5.5）；轨未接线时回退 passive
        if noise_budget is None:
            noise_budget = self.task_types.get(task_type, "passive")
        try:
            task_id = self.tq.publish(
                self.project_id, objective, scope=scope, task_type=task_type,
                noise_budget=noise_budget, priority=priority,
                conflict_keys=conflict_keys, created_by="orchestrator",
                allowed_types=(self.task_types.keys() if self.track else None),
                refs=refs, parent_id=parent_id, parent_depth_limit=1)
        except ValueError as e:
            return f"[拒绝] {e}"
        self._publish_count += 1
        self._published.append(task_id)
        if self.on_task_published is not None:
            try:
                self.on_task_published()
            except Exception:  # noqa: BLE001 —— 计数失败不回滚已发布任务
                log.exception("tasks_published 计数失败")
        return f"task={task_id} 已发布（{task_type}/{noise_budget}）"

    def _tool_spawn_session(self, role: str, reason: str = "") -> str:
        if self.config.allowed_roles is not None and role not in self.config.allowed_roles:
            return f"[拒绝] 角色 {role} 不在白名单 {self.config.allowed_roles}（开窗约束，§6.4）"
        if len(self.live_sessions) >= self.config.max_sessions:
            return f"[拒绝] 活跃会话已达上限 {self.config.max_sessions}，请复用现有会话"
        # 批 6 L0 提案模式：白名单/上限照校，sessions_cap/预算不走（不占资源；
        # 人采纳时 POST /agents 的 409/警告兜底），只发 orch.proposed。
        if self.config.propose_only:
            return self._propose("spawn_session",
                                 {"role": role, "reason": (reason or "").strip()})
        # 项目级持久上限 sessions_cap + token 预算硬闸（§6.8；回调内实时重读配置）。
        # L1 预检同样走这里：cap/预算已超时不建审批单，LLM 立即改道。
        if self.gate is not None:
            blocked = self.gate("spawn_session")
            if blocked:
                return f"[拒绝] {blocked}"
        # L1（任务自动·开窗审批，批 4）：不调工厂，转人工审批；批准后由
        # decide 端点的 op 处理器建窗+开跑。L2/未接线维持直接开窗；
        # L0 在上面的 propose_only 分支只发提案（批 6）。
        auto = self.autonomy_provider() if self.autonomy_provider is not None else None
        if auto is not None and auto.get("level") == "L1":
            if not (reason or "").strip():
                return ("[拒绝] L1 开窗必须填 reason：审批人只看得到 role+reason，"
                        "请写清为什么开这个角色、要它做什么")
            appr = self.bb.request_approval(
                self.project_id,
                {"op": "spawn_session", "role": role,
                 "reason": (reason or "").strip()},
                risk="low", requested_by="orchestrator")
            return (f"已提交开窗审批 {appr['id']}（role={role}）：人类批准后系统自动建窗"
                    "并提交 agent-work 开跑；本轮可继续 publish passive 任务或 done，"
                    "审批结果下轮 tick 经事件可见")
        if self.session_factory is None:
            return "[错误] 未配置 session_factory，无法开窗"
        try:
            sess = self.session_factory(role)
        except Exception as e:  # noqa: BLE001
            return f"[错误] 开窗失败: {type(e).__name__}: {e}"
        self.live_sessions[sess.session["id"]] = sess
        self._spawned.append({"session_id": sess.session["id"], "role": role})
        self.bb.append_event(
            self.project_id, "session.spawned",
            {"role": role, "session_id": sess.session["id"]}, author="orchestrator")
        return f"session={sess.session['id']} role={role} 已启动"

    def _tool_write_digest(self, summary: str) -> str:
        self.bb.append_event(
            self.project_id, "project.digest",
            {"digest": summary, "stats": self._stats()}, author="orchestrator")
        self.last_digest_cycle = self.cycles
        self._digest = summary
        return "简报已落黑板（project.digest）"

    def _tool_done(self) -> str:
        self._finished = True
        return "本轮结束"
