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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core import phases
from core.coverage import coverage_report, effective_status_map
from core.autonomy import record_llm_usage
from core.blackboard import Blackboard, TaskQueue
from core.blackboard.tasks import _check_task_type, dedup_fp, target_keys_of, MAX_TASKS_PER_TARGET
from core.skills.experts import expert_exists, list_experts, load_expert
from core.skills.taxonomy import GENERIC_TASK_TYPE, load_task_types

log = logging.getLogger(__name__)


ORCH_SYSTEM_PROMPT = """你是项目主代理（Orchestrator），职责是监控、派生、开窗、汇总。
你不亲自执行任何命令或分析——脏活全部发布成任务，由 Agent 会话认领执行。

## 本轮态势
{overview}
{role_catalog}
{autonomy_notice}{goal_section}{phase_section}{mission_section}{campaign_section}
## 可用工具
- publish_task：发布任务。task_type 必须是场景轨 task_types.yaml 已注册类型（未注册会被拒收——拼错的类型会让任务饿死），决定哪些角色能认领（先看角色目录与已有会话）；noise_budget 缺省取该类型注册表默认值；会产生噪声的动作（主动探测/执行样本）必须给 conflict_keys；任务若以某发现为依据（如「验证 find-x」），把该发现 id 填 refs——该发现事后被推翻时，执行者会立刻收到强制自评通知。**任务即窗口（v0.71）**：发布成功后系统自动为该任务建立专属执行窗（按建议角色装配），无需也不应再为执行窗调 spawn_session。
- spawn_session：开一个新 AI 会话（受角色白名单 {allowed_roles} 与上限 {max_sessions} 个约束）；**仅用于纯侦查/纯对话辅助窗**（不挂任务，如常驻态势问答、交叉质询）；任务的执行窗由 publish_task 自动建立，不要用本工具代替。
- write_digest：写项目简报（给人类看的全局摘要：进展/发现/风险/下一步）。
- done：结束本轮协调。
- 只读查询四工具（task_detail / bb_overview / budget_status / session_list）：
  用于核对与决策——对执行者结论存疑时先 task_detail 拉全文再判断，不要只凭
  态势摘要下结论；**它们不用于替代派单执行**（你仍不亲自干活，查询是为了
  派得更准、验收得更实）。

## 主代理纪律（硬规则）
1. 不执行命令、不写分析结论——一切经任务派发。
2. 遵循「分析-分解-分派」：复杂目标先在心里拆成自足的小任务，再按角色目录对号分派；
   publish_task 用 **role 参数**结构化指定建议认领角色（本轨已注册角色 id；留空=不限，
   任务即窗口机制下，带角色的任务建专属执行窗时按该角色装配，一窗一任务），并给初始
   priority（0-9，整数；小者优先：被依赖的前置/为他人解阻塞的任务给 0-2，常规任务 3-5，
   可延后的 6-9）。
   注意同目标防碎闸：同一目标（IP/域名等）在队（open+claimed）任务达 {max_tasks_per_target} 个
   会被拒收，发新任务前先看态势里的 targets 分布、消化存量。
3. 新发现 severity>=medium 且 unverified 的，应派生验证任务，别让它烂在黑板里
   （category=intel 的有效发现/提示类除外——它们不是漏洞，无需验证复现）。
4. 开窗被拒（白名单/上限）即改道：复用现有会话或调整任务。
5. 距上次简报 >= {digest_every} 轮时，本轮必须 write_digest。
6. 决策完毕调用 done。
7. 任务拆解与分批（C1）：大目标（如全资产侦察）拆解为自足子任务——先发布父任务拿到
   task_id，再发布子任务并把 parent_id 指向它（**深度 1 层**：子任务不可再拆）；每轮
   发布 ≤{max_publish_per_tick} 个（分批 3-5 个/轮，按建议角色与优先级）；队列空退后
   下一轮 tick 续批（态势里有 uncovered 资产清单可对照发批）。长探测/扫描类 objective
   写明建议超时秒数（run_cmd 默认 120s，不提醒会被截杀，C3）。
8. 收敛判据（轨级行为语义）：mission 判据全部达成、或资产穷尽（uncovered=0 且无可推进
   发现）才 done——不要因为单轮零产出就提前收摊；对照上方行动边界段的判据清单逐条评估。
9. 生命周期收编（M4）：方向变更/目标已达成/前提失效 → cancel_task（reason 写清；
   在跑窗自动打断）；预算恢复/前提补齐/人类已解决挂起 → requeue_task 放回原任务
   （履历保留）——**不要取消后重发同款任务**（丢执行履历且污染发布去重）。
"""


# L1（任务自动·执行审批，v0.72）系统提示追加段：任务窗发布即建（待命），执行等人类批准
L1_AUTONOMY_NOTICE = """## 自主档位 L1（任务自动·执行审批）
publish_task 发布即自动建专属待命窗（待命不耗 LLM），并自动提「执行审批」（action 带 task_id）：
人类批准后系统启动该窗执行任务；批准前任务已入队、只是待命等待。
spawn_session（纯侦查/对话辅助窗，不挂任务）不会立刻开窗：请求进入人类审批收件箱，
人类批准后系统自动建窗并开跑。
- spawn_session 的 reason 必须写清（为什么开这个角色、要它做什么），审批人只看得到 role+reason；
- 等待审批期间可继续 publish passive 任务，或 done 结束本轮；批准/拒绝结果下轮 tick 经事件可见。"""


# L0（全手动）系统提示追加段（批 6）：publish/spawn 只产提案，不写实体
L0_AUTONOMY_NOTICE = """## 自主档位 L0（全手动·提案模式）
你不能直接派任务或开窗：publish_task / spawn_session / cancel_task / requeue_task
只生成人类提案（orch.proposed 事件），
人类在事件流逐条「采纳」后才真正落地（任务以人类名义入队、窗由人类开）。
- 提案不消耗任何预算、不占会话上限，但参数校验照跑：task_type 必须是本轨注册类型、
  非 passive 仍须 conflict_keys、role 仍受白名单/上限约束，填错会被拒收；
- 一轮可提多条；write_digest 照常写简报；决策完毕 done。"""


def mission_boundary_lines(track: str | None, config: dict | None) -> list[str]:
    """行动边界纯文本行（M5 D2，orchestrator-efficiency §0-10）：编排器
    _mission_section（系统提示注入）与 API approvals 出口 boundary 字段（审批卡
    人类对照当前边界审授权申请）同源——改这里两处一起变。redteam=ROE 四要素
    （缺要素给兜底提醒）；其余轨=验证上限一行。"""
    track = track or "pentest"
    if track == "redteam":
        roe = (config or {}).get("redteam_roe") or {}
        lines = [f"ROE 授权目标: {roe.get('targets', '-')}",
                 f"时间窗口: {roe.get('window', '-')}",
                 f"禁止事项: {roe.get('exclusions', '-')}",
                 f"授权人: {roe.get('approver', '-')}"]
        from core.autonomy import roe_complete
        if not roe_complete(roe):
            lines.append("⚠ ROE 四要素未核验齐全：本阶段行为按渗透测试上限兜底，"
                         "请提醒人类补全 ROE。")
        return lines
    return ["验证上限=影响证明级；禁驻留/持久化/横向/提权推进。"]


ORCH_TOOLS: list[dict[str, Any]] = [
    {
        "name": "delegate",
        "description": "向某个会话窗委派一件委托（像人类给 AI 发一条消息让他干活）。"
                       "默认由系统选窗：优先复用「空闲且干过同类委托/与目标有关联"
                       "上下文」的窗，无合适窗则开新窗；也可用 target_session 指定"
                       "窗、force_new_window=true 强制开窗。窗空闲 → 委托进窗后起跑；"
                       "窗忙 → 进该窗队列，当前活干完自动接下一件。长探测/扫描/"
                       "口令喷洒类 objective 请写明建议超时（如「nmap 全端口建议 "
                       "run_cmd timeout 传 600」）——run_cmd 默认 120s，长命令不提醒"
                       "会被截杀（C3）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "委托内容（自包含）"},
                "task_type": {"type": "string",
                              "description": "委托类型，必须是场景轨 task_types.yaml "
                                             "注册表内的合法类型",
                              "default": "generic"},
                "role": {"type": "string",
                         "description": "建议执行专家（可选）：本轨 experts/ 池内专家 "
                                        "id；选窗时优先匹配该专家的窗，开窗时按它装配；"
                                        "留空=不限。执行者中途可被人类换人接手"},
                "target_session": {"type": "string",
                                   "description": "指定委派给已有会话窗（窗 id）；不传="
                                                  "系统按复用规则选窗/开窗"},
                "force_new_window": {"type": "boolean",
                                     "description": "true=不复用窗、强制开新窗",
                                     "default": False},
                "scope": {"type": "string", "description": "限定范围（如目标资产）"},
                "noise_budget": {"type": "string", "enum": ["passive", "low", "medium", "high"],
                                 "description": "缺省取 task_types.yaml 中该类型的默认噪声预算"},
                "conflict_keys": {"type": "array", "items": {"type": "string"},
                                  "description": "非 passive 必填，如 [\"ip:1.2.3.4\"]；"
                                                 "跨窗 active 委托键交集自动排队等待"},
                "priority": {"type": "integer", "description": "0 最高，2 默认"},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "本委托依据的既有发现 id（find- 前缀）；依据"
                                        "被推翻时执行者会收到强制自评通知。正文里直接"
                                        "写 find-id 也会被服务端自动抽取，显式填写更准"},
            },
            "required": ["objective"],
        },
    },
    {
        "name": "cancel_task",
        "description": "取消任务（open/claimed → failed）：被更高优先级方向取代、"
                       "目标已达成、前提失效时用——方向性收编，不占执行窗。claimed "
                       "任务的在跑窗会被打断（现场快照保留可续跑）。reason 必填"
                       "（审计与父子任务回执依据）。预算被泡掉的任务请用 requeue_task "
                       "而不是取消后再重发（避免丢履历）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string",
                           "description": "取消原因（写清为什么，人类在事件流审计）"},
            },
            "required": ["task_id", "reason"],
        },
    },
    {
        "name": "requeue_task",
        "description": "把 failed/awaiting_human 任务放回待认领（清认领持有，"
                       "attempts 履历保留，原绑定窗优先续跑）——预算恢复、前提补齐、"
                       "人类解决挂起原因后重试用；不要取消后重发同款任务（丢履历且"
                       "污染 dedup）。",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
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


# M2 编排器只读查询工具（orchestrator-efficiency A1，2026-09-22）：零写权红线——
# 四工具全是只读 bb 查询（_dispatch 层按 _tool_ 命名自动接线，无任何写实体路径）。
# 补齐「编排器验证不了执行者结论」缺口：态势注入是聚合+截断的被动只读，
# 本工具面给按 id 拉详情的主动通道（渐进披露：态势看摘要，存疑拉全文）。
ORCH_QUERY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "task_detail",
        "description": "按 id 拉取单个任务的全量详情：objective/scope 全文、执行现场"
                       " context（计划、完成对账、历次尝试 attempts）、result_note 全文"
                       "（态势里只有 300 字截断）。用于核对执行者结论——不要凭态势摘要下判断。",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "bb_overview",
        "description": "黑板分区总览（只读）：assets=资产终态覆盖分布；findings=分级统计"
                       "+最新 20 条全标题；events=最近 50 条事件（payload 截 300 字）。"
                       "资产多时用 asset_type/asset_status/limit 过滤，勿整表拉取。",
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["all", "assets", "findings", "events"],
                            "description": "缺省 all 全量"},
                "asset_type": {"type": "string",
                               "enum": ["host", "domain", "url", "service", "binary"],
                               "description": "按资产类型过滤（仅 assets/all 分区）"},
                "asset_status": {"type": "string",
                                 "enum": ["open", "visited", "scanning", "tested_clean",
                                          "budget_stop", "na"],
                                 "description": "按资产状态过滤（仅 assets/all 分区）"},
                "limit": {"type": "integer",
                          "description": "过滤命中条数上限，缺省 50（防全量回传烧 token）"},
            },
        },
    },
    {
        "name": "budget_status",
        "description": "项目预算用量（只读）：token 四项用量与预算余量、自主发布任务数"
                       "与任务预算、80% 软警状态。补齐「余量只能被动等 80% 警告事件」缺口。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "session_list",
        "description": "会话窗清单（只读）：各窗状态/角色/绑定任务/未读数/最后活动时间。",
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


# 对话化编排器（M1，§4.2）：对话插队轮系统提示——复用 tick 态势组装槽 +
# goal_section + persona_section，纪律段换成对话口径（可对话/可发布/走闸门/
# goal 变更须人类确认）。发布走与 tick 完全相同的工具闸门（dedup/gate/L0 提案/
# L1 审批），对话只是起草方式变了。
CHAT_SYSTEM_PROMPT = """你是项目主代理（Orchestrator），现在处于**对话轮**：人类正在与你直接交流。
你可以回答问题、商议目标、给出计划；需要动手时用工具——发布任务与开窗走全部闸门
（预算硬闸/去重/L0 提案/L1 审批，与巡检 tick 同源），被拒就向人类转述原因。

## 当前态势
{overview}
{role_catalog}
{autonomy_notice}{goal_section}{phase_section}{mission_section}{campaign_section}{persona_section}
## 对话纪律
1. 用人类的语言简洁作答，结论先行；问下一步计划时用上方态势作答（门没过就说还差什么）。
2. 执行者看不到对话上下文：publish_task 的 objective 必须自包含；任务即窗口机制
   会自动建专属执行窗，不要为执行窗调 spawn_session。
3. 阶段目标（goal）的变更须由人类确认：你可以在对话里给出结构化草案
   （text/criteria/phase），由人类在编排页签 goal 条确认落盘；未确认前不要当作已生效。
4. 回答完毕调用 done 结束本轮；纯问答（无需动手）直接 done。
"""

# 对话轮 LLM 步上限（插队轮不跑长决策链；发布分批语义不适用——人类在场可连续对话）
CHAT_MAX_STEPS = 8

# 编排器事件窗剔除的纯观测 kind（orch-context-budget，2026-09-27）：
# llm.usage=流量计费行、llm.thinking.delta=思考流式碎片——对派单决策零信息量，
# 实测占事件总量 25%+ 却挤占 40 行窗口坑位；游标照推不重放，细节需要走 bb_overview。
_ORCH_EVENT_EXCLUDE = ("llm.usage", "llm.thinking.delta")


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
        on_task_published: Callable[..., None] | None = None,  # v0.71：可收 task_id
        state_loader: Callable[[], dict] | None = None,
        state_saver: Callable[..., None] | None = None,
        heartbeat: Callable[[], None] | None = None,
        autonomy_provider: Callable[[], dict] | None = None,
        campaign=None,  # ⑥ 战役记忆全局库（CampaignMemory；None=不召回）
        meta_loader: Callable[[], dict] | None = None,  # 对话化 M1/M2：读 project.json meta（goal/persona）
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
        self.campaign = campaign  # ⑥ 战役记忆（tick 态势召回；None=关闭）
        # 对话化编排器（M1/M2，§6.4）：meta_loader 实时返回 project.json meta
        # （phase_goal 阶段目标 / orchestrator_persona 拟人身份）；None=不注入
        # （脚本/旧测试兼容，_goal_section/_persona_section 返空段）。
        self.meta_loader = meta_loader
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
        服务端注册表拒收仍是最终护栏）；无 track 时退回原表。
        M2（2026-09-22）：追加只读查询四工具（与轨无关，无 enum 注入需求）。"""
        base = ORCH_TOOLS + ORCH_QUERY_TOOLS
        if not self.track:
            return base
        tools = copy.deepcopy(base)
        for t in tools:
            if t["name"] == "delegate":
                prop = t["input_schema"]["properties"]["task_type"]
                prop["enum"] = list(self.task_types.keys())
                prop["description"] += (
                    "；本轨合法值（enum）：" + "、".join(self.task_types.keys()))
        return tools

    def _load_role_catalog(self) -> list[dict]:
        """专家池按轨过滤的 id+description（expert-pool M2：roles/ 退役后
        spawn 开窗决策的数据源切 experts/）。"""
        if not self.track:
            return []
        out: list[dict] = []
        for name in list_experts(self.packs_root, self.track):
            data = load_expert(self.packs_root, name, self.track)
            out.append({"name": name,
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

    def _goal_section(self) -> str:
        """阶段目标注入（对话化编排器 M2，§4.3）：meta.phase_goal（人类确认过）
        以「当前阶段目标」段注入 tick 与对话轮系统提示——槽序 C2 指令（overview 内）
        > goal_section > mission_section（mission=行动边界，goal=当下）。"""
        meta = self.meta_loader() if self.meta_loader is not None else {}
        goal = (meta or {}).get("phase_goal") or {}
        text = str(goal.get("text") or "").strip()
        if not text:
            return ""
        lines = ["## 当前阶段目标（人类确认）", f"- {text[:500]}"]
        for c in goal.get("criteria") or []:
            if str(c).strip():
                lines.append(f"  □ {str(c).strip()[:200]}")
        phase = str(goal.get("phase") or "").strip()
        if phase:
            lines.append(f"- 阶段: {phase}")
        return "\n".join(lines) + "\n"

    def _phase_section(self) -> str:
        """阶段工作流注入（pentest-phased-workflow M1，§4.1 重心配额）：当前阶段
        + goal + focus 配比建议 + 出口门进度——tick 与对话轮同槽（{phase_section}）。
        重心配比是软引导（任何阶段可发任何类型）；门未过时 gate_types 内类型由
        _tool_publish_task 硬拒（此处只预告）。轨无阶段剧本/meta 未接线=空段。"""
        if self.meta_loader is None or not self.track:
            return ""
        try:
            meta = self.meta_loader() or {}
            book = phases.load_track_phases(self.packs_root, self.track,
                                            meta.get("config"))
            cur = phases.current_spec(book, meta)
            if cur is None:
                return ""
            pid, spec = cur
            lines = [f"## 阶段工作流：当前处于「{spec['name']}」（{pid}）",
                     f"- 阶段目标：{spec['goal'] or '（未声明）'}"]
            if spec["focus"]:
                lines.append("- 重心配额建议（软引导，publish_task 类型配比向此倾斜）："
                             + "、".join(f"{k}×{v}" for k, v in spec["focus"].items()))
            fwd = phases.forward_targets(book, spec)
            gate = spec.get("gate") or {}
            if gate and fwd:
                # B3 单一事实源：只读 meta 的 phase_gate_state（API 层 _phase_gate_check
                # 每轮评估后落盘、enter_phase 流转顺写新阶段），此处不重算——
                # 消除「注入 tick 开始用上轮 idle、判定 tick 结束 idle 已 +1」矛盾窗口；
                # 派单撞门的实时拒绝仍由 _phase_gate_reject 现算（动作侧确定性不变）。
                st = phases.read_gate_state(meta)
                head = f"- 出口门（进入「{book[fwd[0]]['name']}」）："
                if st is None or st.get("phase") != pid:
                    lines.append(head + "门进度待编排校准（下轮编排评估后显示）")
                elif st.get("passed"):
                    lines.append(head + "已过门——等待阶段流转（按自主档分流，勿自行重复推进）")
                else:
                    unmet = st.get("unmet") or []
                    lines.append(head + f"未过（{'；'.join(unmet)}）"
                                 + (f"——{'/'.join(spec['gate_types'])} 类任务会被拒收"
                                    if spec["gate_types"] else ""))
            return "\n".join(lines) + "\n"
        except Exception:  # noqa: BLE001 —— 阶段段绝不阻断编排
            log.exception("阶段工作流段组装失败（跳过）")
            return ""

    def _persona_section(self) -> str:
        """拟人身份注入（M3，§4.4）：meta.orchestrator_persona 只进**对话轮**
        系统提示（tick 巡检不需要脸）；display_name 由前端贯穿页签/气泡。"""
        meta = self.meta_loader() if self.meta_loader is not None else {}
        p = (meta or {}).get("orchestrator_persona") or {}
        persona = str(p.get("persona") or "").strip()
        if not persona:
            return ""
        return f"## 你的身份\n{persona[:600]}\n"

    def _mission_section(self) -> str:
        """行动边界注入（R2 轨级行为语义，goal 统一后本段只管边界不管目标）：
        redteam ROE 四要素（缺 ROE 时按 pentest 上限兜底提醒）；pentest 给一行
        上限提醒。目标与判据由 goal_section 承担（meta.phase_goal，§4.3）。
        边界正文与 API approvals 出口 boundary 同源（mission_boundary_lines）。"""
        cfg = self.bb.get_project(self.project_id)["config"] or {}
        track = self.track or "pentest"
        label = "红队行动" if track == "redteam" else "渗透测试"
        body = mission_boundary_lines(track, cfg)
        lines = [f"## 行动边界：{label}（{track} 轨）"] + [f"- {x}" for x in body]
        if track == "redteam":
            lines.append("- ⚠ 边界即红线：授权申请走 request_authorization，勿自行越界。")
        return "\n".join(lines) + "\n"

    def _campaign_section(self) -> str:
        """⑥ 战役记忆召回（简版，跨项目）：既往打法 top-5 注入态势——
        关键词取阶段目标 goal 文本（mission 存量回退）+ 高价值目标/同目标负载；
        track/capability 优先；热度×时间衰减排序，单条截 300 字。campaign 未注入或零命中给空段。"""
        if self.campaign is None:
            return ""
        try:
            proj = self.bb.get_project(self.project_id)
            meta = self.meta_loader() if self.meta_loader is not None else {}
            goal_text = str(((meta or {}).get("phase_goal") or {}).get("text") or "")
            mission = str(((proj["config"] or {}).get("mission") or {}).get("text") or "")
            caps = proj.get("capabilities") or []
            targets = self._target_load(self.tq.list_tasks(self.project_id))
            hv = " ".join(a["value"] for a in self._stats().get("high_value", []))
            query = (goal_text or mission) + " " + hv + " " + " ".join(
                t["target"] for t in targets[:5])
            hits = self.campaign.recall(
                query, track=self.track or "", capability=caps[0] if caps else "",
                limit=5)
            if not hits:
                return ""
            # experience-sedimentation M2 元信息行：campaign 条件写入后（只收
            # verified 产出的任务），注入本项目的已验证产出量给编排器参照，
            # 零产出项目看到跨项目打法时知道本项目还什么都没验证过
            try:
                row = self.bb.conn.execute(
                    "SELECT COUNT(*) AS n FROM findings WHERE project_id=?"
                    " AND status='verified'", (self.project_id,)).fetchone()
                n_verified = row["n"]
            except Exception:  # noqa: BLE001
                n_verified = 0
            # M6 F1（§0-11）：死路条目（tags 含 dead_end）单独成组——负知识防重走，
            # 不能混在「打法」里误导成正面经验；recall 池不拆，组内照旧打分排序
            normal = [h for h in hits if "dead_end" not in (h.get("tags") or [])]
            dead = [h for h in hits if "dead_end" in (h.get("tags") or [])]
            lines = ["## 既往战役打法（跨项目记忆，仅参考——贴合当前目标再采用）",
                     f"- 本项目 verified 发现 {n_verified} 个（campaign 收 verified"
                     " 产出/exploited 链打法与死路记账负知识）"]
            for h in normal:
                lines.append(f"- [{h['track'] or '?'}·{h['capability'] or '?'}·热{h['usage_count']}] "
                             f"{h['title'][:80]}\n  {h['content'][:300]}")
            if dead:
                lines.append("- ⚠ 既往死路（勿重走；确需重走先确认前提已变化）：")
                for h in dead:
                    lines.append(f"- [{h['track'] or '?'}·{h['capability'] or '?'}] "
                                 f"{h['title'][:80]}\n  {h['content'][:300]}")
            return "\n".join(lines) + "\n"
        except Exception:  # noqa: BLE001 —— 记忆召回绝不阻断编排
            log.exception("战役记忆召回失败（跳过）")
            return ""

    def _overview(self, expired_leases: list[str]) -> str:
        stats = self._stats()
        self._last_stats_starvation = stats["tasks"]["starvation"]
        # 游标追赶：未消费 backlog 超过窗口（100）时只喂最新 100 条，但游标一次跳到
        # 当前末端——旧 backlog 永不逐轮回放（走查发现：从 0 起每轮 +100 的爬行 bug）。
        tip = self.bb.latest_event_id(self.project_id)
        if tip - self._last_event_id > 100:
            new_events = self.bb.recent_events(
                self.project_id, since_id=tip - 100, limit=100,
                exclude_kinds=_ORCH_EVENT_EXCLUDE)
        else:
            new_events = self.bb.recent_events(
                self.project_id, since_id=self._last_event_id, limit=100,
                exclude_kinds=_ORCH_EVENT_EXCLUDE)
        self._last_event_id = max(tip, self._last_event_id)
        # A2 截断放宽（orchestrator-efficiency，2026-09-22）：事件行 payload
        # [:120]→[:300]——编排器反馈事件行过短无法判读，全文兜底走 bb_overview
        event_lines = [
            f"  #{e['id']} [{e['kind']}] {e['author']}: {json.dumps(e['payload'], ensure_ascii=False)[:300]}"
            for e in new_events[-40:]
        ]
        out = json.dumps(stats, ensure_ascii=False, indent=1) + (
            "\n\n## 本轮过期租约（已回收为 open）\n- " + "\n- ".join(expired_leases)
            if expired_leases else ""
        ) + "\n\n## 自上轮以来的新事件\n" + ("\n".join(event_lines) or "  （无）")
        directives = self._pending_directives()
        if directives:
            out += "\n\n## ⚠ 人类指令（最高优先，本轮优先落实）"
            for d in directives:
                out += f"\n- {d['text']}"
        # 态势增强：最近一份简报常驻（digest 滑出事件窗口后不再丢失阶段性总结）
        latest = self.bb.latest_digest(self.project_id)
        if latest and latest["digest"].strip():
            out += ("\n\n## 上一份简报（常驻，写于 "
                    + latest["created_at"][:10] + "）\n"
                    + latest["digest"][:2000])
        return out

    def _overview_for_chat(self) -> str:
        """对话轮只读态势（对话化 M1，§4.2）：stats 全量 + 固定最近 100 条事件窗
        （**不推进 event_cursor**）+ 最新简报常驻。与 tick 的 _overview 差异：
        不注入过期租约（对话轮不做回收动作）、不注入也不消费 C2 指令（存量指令
        仍由 tick 消费）、不写 _last_stats_starvation（不触发饿死告警事件）。"""
        stats = self._stats()
        tip = self.bb.latest_event_id(self.project_id)
        new_events = self.bb.recent_events(
            self.project_id, since_id=tip - 100, limit=100,
            exclude_kinds=_ORCH_EVENT_EXCLUDE)
        event_lines = [
            f"  #{e['id']} [{e['kind']}] {e['author']}: {json.dumps(e['payload'], ensure_ascii=False)[:300]}"
            for e in new_events[-40:]
        ]
        out = json.dumps(stats, ensure_ascii=False, indent=1) + (
            "\n\n## 最近事件（只读窗口，不推进游标）\n" + ("\n".join(event_lines) or "  （无）"))
        latest = self.bb.latest_digest(self.project_id)
        if latest and latest["digest"].strip():
            out += ("\n\n## 上一份简报（常驻，写于 "
                    + latest["created_at"][:10] + "）\n" + latest["digest"][:2000])
        return out

    def _pending_directives(self) -> list[dict]:
        """C2 指挥编排器：捞未处理的 orch.directive 事件（有 done 标记的排除）。
        人类一次性目标指令——最高优先注入态势。"""
        rows = self.bb.conn.execute(
            "SELECT id, payload FROM events WHERE project_id=? AND kind='orch.directive'"
            " ORDER BY id DESC LIMIT 20",
            (self.project_id,)).fetchall()
        done_ids = {json.loads(r["payload"]).get("event_id") for r in self.bb.conn.execute(
            "SELECT payload FROM events WHERE project_id=? AND kind='orch.directive.done'",
            (self.project_id,)).fetchall()}
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except ValueError:
                continue
            text = str(payload.get("text") or "").strip()
            if text and r["id"] not in done_ids:
                out.append({"event_id": r["id"], "text": text[:300]})
        return out

    def _stats(self) -> dict:
        tasks = self.tq.list_tasks(self.project_id)
        by_status: dict[str, int] = {}
        for t in tasks:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        findings = self.bb.list_findings(self.project_id)
        sessions = self.bb.list_sessions(self.project_id)
        open_tasks = [t for t in tasks if t["status"] == "open"]
        starvation = self._starvation_warnings(open_tasks, sessions)
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
        cfg = self.bb.get_project(self.project_id)["config"] or {}
        mission_view: dict[str, Any] = {"track": self.track or "pentest"}
        if isinstance(cfg.get("mission"), dict):
            mission_view["mission"] = cfg["mission"]
        if isinstance(cfg.get("redteam_roe"), dict):
            mission_view["roe"] = cfg["redteam_roe"]
        assets_view = self._assets_view(tasks)
        hvt_ids: set = assets_view.pop("_hvt_ids")
        covered_ids: set = assets_view.pop("_covered_ids")
        # 高价值目标段（态势增强）：两类来源并集——
        # ①meta.tags 含「高价值」（人工标注，既有口径）；
        # ②自动推导（2026-09-24）：承载 verified 且 severity≥high 的发现的资产——
        #   实证高危的资产不该在面板缺席（此前只认 tag，verified HIGH 无入口呈现）。
        # 即使被任务覆盖，未到 tested_clean 就持续呈现（"挖完"口径= tested_clean）。
        by_asset: dict[str, list[dict]] = {}
        for f in findings:
            if f.get("target_asset_id"):
                by_asset.setdefault(f["target_asset_id"], []).append(f)
        sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        derived_ids: set[str] = set()
        for aid, fs in by_asset.items():
            if any(f.get("status") == "verified"
                   and f.get("severity") in ("high", "critical") for f in fs):
                derived_ids.add(aid)
        hv_ids_all = hvt_ids | derived_ids
        high_value = []
        if hv_ids_all:
            for a in self.bb.list_assets(self.project_id):
                if a["id"] not in hv_ids_all:
                    continue
                rel = sorted(by_asset.get(a["id"], []),
                             key=lambda f: sev_rank.get(f.get("severity"), 9))[:3]
                high_value.append({
                    "id": a["id"], "type": a["type"], "value": a["value"][:60],
                    "status": a.get("status") or "open",
                    "covered": a["id"] in covered_ids,
                    "derived": a["id"] in derived_ids and a["id"] not in hvt_ids,
                    "findings": [{"id": f["id"], "title": f["title"][:50],
                                  "severity": f["severity"], "status": f["status"]}
                                 for f in rel],
                })
        recent_tasks = self._recent_tasks_view(tasks)
        # 上下文预算（orch-context-budget，2026-09-27）：findings/sessions 全量段
        # 随项目线性膨胀失控——findings 只进 top 20（verified/exploited 优先 →
        # severity 降序），closed 会话出清；计数行给参照，细节走 bb_overview 按需拉。
        findings_top = sorted(
            findings,
            key=lambda f: (0 if f["status"] in ("verified", "exploited") else 1,
                           sev_rank.get(f.get("severity"), 9)))[:20]
        sessions_live = [s for s in sessions if s["status"] != "closed"]
        return {
            "mission": mission_view,
            "high_value": high_value,
            "tasks": {
                "by_status": by_status,
                "open": [{"id": t["id"], "type": t["task_type"], "priority": t["priority"],
                          "noise_budget": t["noise_budget"], "created_at": t["created_at"],
                          "role": t.get("role") or "",
                          "objective": t["objective"][:80]}
                         for t in open_tasks],
                # v14 同目标负载分布（top5）：发布前对照，防把一个目标碎成过多任务
                "targets": self._target_load(tasks),
                "blocked_plan": blocked_plan,
                "starvation": starvation,
                **recent_tasks,
            },
            "findings": [{"id": f["id"], "vuln_class": f["vuln_class"], "title": f["title"][:60],
                          "severity": f["severity"], "status": f["status"],
                          "category": f.get("category") or "vuln"} for f in findings_top],
            "findings_total": len(findings),
            "findings_truncated": len(findings) > len(findings_top),
            "sessions": [{"id": s["id"], "role": s["role"], "status": s["status"]}
                         for s in sessions_live],
            "sessions_closed": len(sessions) - len(sessions_live),
            "live_windows": len(self.live_sessions),
            **assets_view,
        }

    def _assets_view(self, tasks: list[dict]) -> dict:
        """C1 资产视图 + 态势增强（HVT 标签/攻击面进度）：by_type/by_status 计数 +
        未覆盖清单（cap 30 带 id，HVT 优先排序）+ 半程资产（visited/scanning cap 20）——
        供编排器分批发批对照；判定为提示层，真护栏 = conflict_keys 互斥。
        未覆盖 = host/domain/url/service 资产**既未被任何 open/claimed 任务的
        conflict_keys/scope/objective 文本提及、状态也还是 open**——
        visited/scanning（半程）/tested_clean（"挖完"口径，访问≠测试）都算已覆盖
        （2026-09-18 对齐判据文案「含 visited/scanning 状态排除」；旧口径只看任务
        文本，任务 done 后资产回流 uncovered 导致判据永不收敛）。"""
        assets = self.bb.list_assets(self.project_id)
        # asset-tree-derived-clean M2：状态以读时派生为准（父节点显式 status 被子树覆盖）
        eff_map = effective_status_map(assets, self.bb.list_findings(self.project_id))
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        targetable = []
        hvt_ids: set[str] = set()
        for a in assets:
            by_type[a["type"]] = by_type.get(a["type"], 0) + 1
            eff = eff_map.get(a["id"])
            st = eff["status"] if eff else (a.get("status") or "open")
            by_status[st] = by_status.get(st, 0) + 1
            if a["type"] in ("host", "domain", "url", "service"):
                targetable.append(a)
            tags = [str(t).strip().lower() for t in (a.get("meta") or {}).get("tags") or []]
            if "高价值" in tags:
                hvt_ids.add(a["id"])
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

        # covered（M2 新口径）：effective settled（全终态/挂 finding 收口）或
        # 半程（visited/scanning）或任务文本命中。零子根行的显式 tested_clean
        # 照常算 covered（D5，AI 管理语义不变）。
        def _status_covered(a: dict) -> bool:
            eff = eff_map.get(a["id"])
            if eff is None:
                return (a.get("status") or "open") in (
                    "visited", "scanning", "tested_clean", "na")
            return eff["settled"] or eff["status"] in ("visited", "scanning")

        covered_ids = {
            a["id"] for a in targetable
            if _status_covered(a) or any(k in covered_text for k in _asset_keys(a))
        }
        uncovered = [a for a in targetable if a["id"] not in covered_ids]
        # 排序：HVT 优先 → 未访问（open，此时 uncovered 里只剩 open 态）
        uncovered.sort(key=lambda a: (a["id"] not in hvt_ids, 0))
        in_progress = [
            {"id": a["id"], "type": a["type"], "value": a["value"][:60],
             "status": eff_map[a["id"]]["status"]}
            for a in targetable
            if a["id"] in eff_map and eff_map[a["id"]]["status"] in ("visited", "scanning")
        ][:20]
        # B2 覆盖度对账（M3）：分组收敛摘要——终态含 finding 挂链/dead_end/na，
        # 组内子资产传播收敛，uncovered open 优先。「全景对账」类数数任务归零；
        # 对账失败不阻断态势注入。
        try:
            cov = coverage_report(self.bb, self.project_id, assets=assets)
            cov_groups = []
            for g in cov["by_group"]:
                row = f"{g['group']} 收敛 {g['converged']}/{g['total']}"
                if g["done"]:
                    row += "（已收口）"
                elif g["uncovered"]:
                    row += " · 未收口: " + "、".join(
                        f"{u['type']}:{u['value'][:40]}({u['state']})"
                        for u in g["uncovered"][:5])
                cov_groups.append(row)
            coverage_view = {
                "groups_done": f"{cov['overall']['groups_done']}/{cov['overall']['groups']}",
                "converged": f"{cov['overall']['converged']}/{cov['overall']['assets']}",
                "by_group": cov_groups,
            }
        except Exception:  # noqa: BLE001
            log.exception("覆盖度对账组装失败（coverage 段跳过）")
            coverage_view = None
        return {
            "assets": {
                "total": len(assets), "by_type": by_type, "by_status": by_status,
                "done_count": by_status.get("tested_clean", 0),  # "挖完"口径 = tested_clean
                "uncovered_total": len(uncovered),
                "uncovered": [{"id": a["id"], "type": a["type"], "value": a["value"][:60]}
                              for a in uncovered[:30]],
                "in_progress": in_progress,
                "coverage": coverage_view,
            },
            "_hvt_ids": hvt_ids,        # 内部复用（_stats 组装 high_value 段），出口前剔除
            "_covered_ids": covered_ids,
        }

    def _target_load(self, tasks: list[dict]) -> list[dict]:
        """v14 同目标负载分布：open+claimed 任务按 target_keys_of 归一化目标键聚合
        （ip:/host:/domain:，url 派生 host 键，passive 无 conflict_keys 时 scope 切段识别），
        top5 注入态势。发布前对照——同目标在队达 MAX_TASKS_PER_TARGET（4）个会被拒收。"""
        load: dict[str, int] = {}
        for t in tasks:
            if t["status"] not in ("open", "claimed"):
                continue
            for k in target_keys_of(t.get("scope") or "", t.get("conflict_keys") or []):
                load[k] = load.get(k, 0) + 1
        ranked = sorted(load.items(), key=lambda kv: -kv[1])[:5]
        return [{"target": k, "in_queue": n, "at_limit": n >= MAX_TASKS_PER_TARGET}
                for k, n in ranked]

    def _recent_tasks_view(self, tasks: list[dict]) -> dict:
        """态势增强（记忆缺口补强）：最近收尾（done/failed，含 result_note 收尾摘要）
        与执行中（claimed，计划进度）任务——上轮/近几轮"做得怎么样"不再只靠事件窗口碎片。"""
        closed = [t for t in tasks if t["status"] in ("done", "failed")]
        closed.sort(key=lambda t: t.get("updated_at") or t.get("created_at") or "",
                    reverse=True)
        recent_closed = [
            {"id": t["id"], "type": t["task_type"], "status": t["status"],
             # A2 截断放宽（orchestrator-efficiency，2026-09-22）：[:100]→[:300]；
             # 全文兜底走 task_detail
             "result_note": (t.get("result_note") or "")[:300],
             "ended_at": t.get("updated_at") or t.get("created_at") or ""}
            for t in closed[:30]
        ]
        claimed_now = [
            {"id": t["id"], "type": t["task_type"],
             "claimed_by": t.get("claimed_by") or "",
             "objective": (t.get("objective") or "")[:60],
             "plan_done": sum(1 for s in (t.get("plan") or [])
                              if s.get("status") == "done"),
             "plan_total": len(t.get("plan") or [])}
            for t in tasks if t["status"] == "claimed"
        ]
        return {"recent_closed": recent_closed, "claimed_now": claimed_now}

    def _starvation_warnings(self, open_tasks: list[dict],
                             sessions: list[dict] | None = None) -> list[dict]:
        """饿死检测（§6.6）：open 任务 ①task_type 未在轨注册表（拼错/漏配）
        ②注册表有但无专才角色声明可认领（只有 _generalist 兜底）。
        _generalist 的 task_types=null（不过滤）不计入专才覆盖。
        v14 role 维度：带 role 的 open 任务 ③role 未注册。
        v0.71 任务即窗口修订：④「无底色匹配会话在岗」分支退役——每个任务发布
        即有专属执行窗（绑定模型）；新增 ⑤open 任务绑定指向 closed 会话（绑窗
        被人工关掉）——提醒等待调度器重绑新窗。"""
        if not self.track:
            return []
        covered: dict[str, list[str]] = {}
        for r in self.role_catalog:
            if r["name"] == "_generalist":
                continue
            data = load_expert(self.packs_root, r["name"], self.track)
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
        if sessions is not None:
            names = {c["name"] for c in self.role_catalog}
            closed_sids = {s["id"] for s in sessions
                           if (s.get("status") or "") == "closed"}
            for t in open_tasks:
                r = (t.get("role") or "").strip()
                if r and r not in names:
                    reason = f"role {r!r} 不在 experts/ 池（认领后无法换装）"
                elif t.get("target_session") and t["target_session"] in closed_sids:
                    # v0.71 任务即窗口：绑定窗被人工关闭，任务悬 open 等 scheduler 重绑
                    reason = ("任务绑定的执行窗已关闭，等待调度器重绑新窗"
                              "（或人工在看板放回/重新发布）")
                else:
                    continue
                warnings.append({"task_id": t["id"], "task_type": t["task_type"],
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
        pending_directives = self._pending_directives()  # C2：人类指令随 tick 消费后标 done
        self._consumed_directives = list(pending_directives)
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
            max_tasks_per_target=MAX_TASKS_PER_TARGET,
            goal_section=self._goal_section(),
            phase_section=self._phase_section(),
            mission_section=self._mission_section(),
            campaign_section=self._campaign_section(),
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

    # ---------- 异常订阅唤醒（对话化编排器 M4，§4.6） ----------

    # 白名单 kind → 冷却窗秒数。只有这里列出的事件才可能唤醒编排器；
    # 绑窗关闭复用 task.starvation（调度器重绑分支），不单独设 kind。
    WAKE_TRIGGERS: dict[str, float] = {
        "task.failed": 600.0,          # 任务失败聚合（一次唤醒合并锚点后全部失败）
        "task.starvation": 600.0,      # 新饿死告警（含绑窗关闭待重绑）
        "budget.soft_warning": 3600.0,  # 预算 80% 软警（低频，1h 冷却）
        "phase.gate_open": 600.0,      # 阶段出口门满足（五触发之一：goal 阶段门）
    }
    WAKE_LOOKBACK = 1800.0   # 首启回看窗：无历史唤醒锚点时只看最近 30 分钟事件
    WAKE_MAX_EVENTS = 300    # 扫描上限：锚点之后最多回看多少条事件
    WAKE_CHAT_WINDOW = 200   # 在 orch.chat 里找 proactive 锚点的回看条数

    @staticmethod
    def _event_epoch(s: Any) -> float:
        """事件 created_at（UTC ISO）→ epoch 秒；解析失败返 0.0（永不触发冷却）。"""
        try:
            return datetime.fromisoformat(
                str(s).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return 0.0

    @classmethod
    def collect_wake_triggers(
        cls, bb: Any, project_id: str, *, now: float | None = None,
    ) -> list[dict[str, Any]]:
        """扫描白名单事件，返回待唤醒触发清单 [{kind, event_id, summary, ts}]。

        纯函数（零新表零列）：唤醒锚点 = 最近 proactive orch.chat 事件——
        - 无锚点：只报 WAKE_LOOKBACK 窗内的白名单事件（首启防翻旧账）；
        - 有锚点：`id <= anchor_id` 跳过（上次唤醒已覆盖）+ `now - ts < 冷却窗`
          跳过（冷却期内静默，事件攒着冷却后随下次唤醒一并简报）。"""
        if now is None:
            now = time.time()
        # 1) 找各 kind 的最新唤醒锚点（proactive orch.chat 事件的 triggers 字段）
        anchors: dict[str, tuple[int, float]] = {}
        chat_rows = bb.conn.execute(
            "SELECT id, payload, created_at FROM events"
            " WHERE project_id=? AND kind='orch.chat'"
            " ORDER BY id DESC LIMIT ?", (project_id, cls.WAKE_CHAT_WINDOW)).fetchall()
        for r in chat_rows:
            try:
                p = json.loads(r["payload"])
            except ValueError:
                continue
            if not p.get("proactive"):
                continue
            ts = cls._event_epoch(r["created_at"])
            for k in p.get("triggers") or []:
                if isinstance(k, str):
                    anchors.setdefault(k, (int(r["id"]), ts))
        # 2) 旧→新扫白名单事件
        out: list[dict[str, Any]] = []
        kinds = list(cls.WAKE_TRIGGERS)
        marks = ",".join("?" * len(kinds))
        rows = bb.conn.execute(
            f"SELECT id, kind, payload, created_at FROM events"
            f" WHERE project_id=? AND kind IN ({marks})"
            f" ORDER BY id DESC LIMIT ?",
            (project_id, *kinds, cls.WAKE_MAX_EVENTS)).fetchall()
        for r in reversed(rows):
            kind = r["kind"]
            anchor = anchors.get(kind)
            if anchor is not None:
                if int(r["id"]) <= anchor[0]:
                    continue  # 锚点前的事件：上次唤醒已覆盖
                if now - anchor[1] < cls.WAKE_TRIGGERS[kind]:
                    continue  # 冷却窗内：静默攒着
            else:
                if now - cls._event_epoch(r["created_at"]) > cls.WAKE_LOOKBACK:
                    continue  # 无锚点且超出首启回看窗：不翻旧账
            try:
                p = json.loads(r["payload"])
            except ValueError:
                p = {}
            if kind == "task.starvation":  # payload={warnings:[{task_id,objective,reason}]}
                parts: list[str] = []
                for w in (p.get("warnings") or [])[:5]:
                    if isinstance(w, dict):
                        line = f"{w.get('reason') or ''}（{w.get('objective') or ''}）"
                        if line.strip("（） ") and line not in parts:
                            parts.append(line)
                summary = "；".join(parts)
            else:  # task.failed/budget.soft_warning=note、phase.gate_open=summary
                summary = str(p.get("note") or p.get("summary")
                              or p.get("reason") or "")
            out.append({"kind": kind, "event_id": r["id"],
                        "summary": summary[:160], "ts": r["created_at"]})
        return out

    @classmethod
    def wake_brief_text(cls, triggers: list[dict[str, Any]]) -> str:
        """把触发清单合成一条 user 消息（只进 LLM messages，不落 orch.chat 历史）。"""
        lines = ["〔主动唤醒〕以下异常事件达到白名单触发条件，请向人类简报现状并给出"
                 "处理建议；若无需处理请明确说明。"]
        for t in triggers:
            kind = t["kind"]
            if kind == "task.failed":
                lines.append(f"- 任务失败：{t['summary']}")
            elif kind == "task.starvation":
                lines.append(f"- 饿死/重绑告警：{t['summary']}")
            elif kind == "budget.soft_warning":
                lines.append("- 预算软警：LLM 用量已达 80%，请评估消耗与剩余任务量")
            elif kind == "phase.gate_open":
                lines.append("- 阶段出口门满足：当前阶段门指标达标，可考虑流转下一阶段")
            else:
                lines.append(f"- {kind}：{t['summary']}")
        return "\n".join(lines)

    # ---------- 对话插队轮（对话化编排器 M1，§4.2） ----------

    def _chat_history(self) -> list[dict[str, Any]]:
        """对话上下文 = events 表 orch.chat 最近 40 条按序组装（单一来源，零新表
        零文件）。human→user / orch→assistant；开头连续的 orch 消息跳过（保证
        messages 首条是 user）。"""
        rows = self.bb.conn.execute(
            "SELECT payload FROM events WHERE project_id=? AND kind='orch.chat'"
            " ORDER BY id DESC LIMIT 40", (self.project_id,)).fetchall()
        out: list[dict[str, Any]] = []
        for r in reversed(rows):
            try:
                payload = json.loads(r["payload"])
            except ValueError:
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            role = "user" if payload.get("role") == "human" else "assistant"
            out.append({"role": role, "content": text})
        while out and out[0]["role"] != "user":
            out.pop(0)
        return out

    @staticmethod
    def _assistant_text(raw: dict) -> str:
        """取一轮 assistant 回复里的纯文本块（与工具调用并存时只取 text）。"""
        blocks = (raw or {}).get("content") or []
        return "".join(
            str(b.get("text") or "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text").strip()

    def chat_turn(
        self, text: str, *, wake: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """一轮对话插队轮：人类消息 → LLM 工具循环（ORCH_TOOLS 全闸门同源）→
        回复落 orch.chat {role:"orch", text(截2000), tool_trace}。

        wake 非空 = 异常订阅唤醒轮（M4 §4.6）：同一条循环，仅落盘 payload 加
        proactive=True + triggers（前端 🔔 徽章与下次唤醒锚点双用途）；
        text 由调用方经 wake_brief_text 合成，只进 messages 不落历史。

        与 tick 的边界（打磨定稿 #2，全部只读）：
        - 态势走 _overview_for_chat（固定最近 100 条事件窗，**不推进 event_cursor**）；
        - 不计 cycles、不动 last_digest_cycle（digest_every 节奏只数 tick 轮次）、
          不消费 C2 指令、不落饿死告警事件、**不写 state_saver**；
        - 租约由 API 层 acquire/release；本方法每步 heartbeat 续租（未注入则跳过）。
        返回 {reply, published, spawned, digest, proposals}。"""
        self._finished = False
        self._actions = []
        self._published: list[str] = []
        self._spawned: list[dict[str, str]] = []
        self._proposals = []
        self._digest: str | None = None
        self._publish_count = 0
        tool_trace: list[dict[str, str]] = []
        auto = self.autonomy_provider() if self.autonomy_provider is not None else None
        if self.config.propose_only:
            autonomy_notice = L0_AUTONOMY_NOTICE
        elif (auto or {}).get("level") == "L1":
            autonomy_notice = L1_AUTONOMY_NOTICE
        else:
            autonomy_notice = ""
        system = CHAT_SYSTEM_PROMPT.format(
            overview=self._overview_for_chat(),
            role_catalog=self._role_catalog_prompt(),
            autonomy_notice=autonomy_notice,
            goal_section=self._goal_section(),
            phase_section=self._phase_section(),
            mission_section=self._mission_section(),
            campaign_section=self._campaign_section(),
            persona_section=self._persona_section(),
        )
        messages = self._chat_history()
        messages.append({"role": "user", "content": text})
        reply = ""
        for _step in range(1, CHAT_MAX_STEPS + 1):
            if self.heartbeat is not None:
                try:
                    self.heartbeat()
                except Exception:  # noqa: BLE001 —— 续租失败不阻断对话
                    log.exception("对话轮租约心跳失败")
            resp = self.llm.chat(messages, system=system, tools=self._orch_tools())
            record_llm_usage(
                self.bb, self.project_id, resp.usage, source="orchestrator-chat",
                session_id=None, model=getattr(self.llm, "model", ""))
            messages.append({"role": "assistant", "content": resp.raw.get("content", [])})
            step_text = self._assistant_text(resp.raw)
            if step_text:
                reply = step_text
            if not resp.tool_calls:
                break  # 纯文本回复 = 回答完毕（对话轮与 tick 不同：文本即答案）
            for tc in resp.tool_calls:
                result = self._dispatch(tc.name, tc.arguments)
                tool_trace.append({
                    "name": tc.name,
                    "args": json.dumps(tc.arguments, ensure_ascii=False)[:200],
                    "result": str(result)[:200],
                })
                messages.append(self.llm.tool_result_message(tc, result))
            if self._finished:
                break
        if reply:
            payload: dict[str, Any] = {
                "role": "orch", "text": reply[:2000], "tool_trace": tool_trace}
            if wake:
                payload["proactive"] = True
                payload["triggers"] = [t["kind"] for t in wake]
            self.bb.append_event(
                self.project_id, "orch.chat", payload, author="orchestrator")
        return {
            "reply": reply,
            "published": list(self._published),
            "spawned": list(self._spawned),
            "digest": self._digest,
            "proposals": list(self._proposals),
        }

    def _finish_tick(self, *, exhausted: bool = False) -> dict[str, Any]:
        """落盘持久游标/轮数并组装结构化结果（两条 tick 出口共用）。
        C2：本轮消费的人类指令标 done（orch.directive.done 事件）——下轮不再注入。"""
        for d in getattr(self, "_consumed_directives", []):
            try:
                self.bb.append_event(
                    self.project_id, "orch.directive.done",
                    {"event_id": d["event_id"]}, author="orchestrator")
            except Exception:  # noqa: BLE001
                log.exception("指令 done 标记失败")
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

    # ---------- 只读查询四工具（M2 orchestrator-efficiency A1，2026-09-22） ----------

    def _tool_task_detail(self, task_id: str) -> str:
        """按 id 拉任务全量：get_task 出口已解析 context（plan/reconcile/attempts），
        result_note 全文不截断——核对执行者结论的唯一可信通道。"""
        task = self.tq.get_task(task_id)
        if task is None or task.get("project_id") != self.project_id:
            return f"[错误] 任务不存在或不在本项目: {task_id}"
        out = {k: task.get(k) for k in (
            "id", "status", "task_type", "role", "objective", "scope",
            "claimed_by", "blocked_reason", "result_note", "created_at",
            "updated_at", "plan", "context")}
        return json.dumps(out, ensure_ascii=False, indent=1)

    def _tool_bb_overview(self, section: str = "all", *,
                          asset_type: str = "", asset_status: str = "",
                          limit: int = 0) -> str:
        """黑板分区总览。assets 复用 _assets_view（剔除内部键）；findings 给
        分级统计+最新 20 条全标题；events 给尾部 50 条（payload 截 300）。
        asset_type/asset_status/limit 给定时 assets 分区换成精简过滤清单
        （不回传全量覆盖视图，省 token）。"""
        section = (section or "all").strip().lower()
        if section not in {"all", "assets", "findings", "events"}:
            return f"[错误] 未知 section: {section}（可选 all/assets/findings/events）"
        asset_filtered = bool(asset_type or asset_status)
        limit = limit if (isinstance(limit, int) and limit > 0) else 50
        parts: list[str] = []
        if section in ("all", "assets"):
            if asset_filtered:
                if asset_type and asset_type not in ("host", "domain", "url", "service", "binary"):
                    return f"[错误] 非法 asset_type: {asset_type}"
                if asset_status and asset_status not in (
                        "open", "visited", "scanning", "tested_clean", "budget_stop", "na"):
                    return f"[错误] 非法 asset_status: {asset_status}"
                rows = self.bb.list_assets(self.project_id,
                                           type_=asset_type or None,
                                           status=asset_status or None)
                shown = [{"id": a["id"], "type": a["type"], "value": a["value"][:80],
                          "status": a.get("status") or "open"}
                         for a in rows[:limit]]
                parts.append("## 资产过滤清单\n" + json.dumps(
                    {"filter": {"type": asset_type or None, "status": asset_status or None},
                     "matched_total": len(rows), "shown": len(shown), "items": shown},
                    ensure_ascii=False, indent=1))
            else:
                tasks = self.tq.list_tasks(self.project_id)
                av = self._assets_view(tasks)
                av.pop("_hvt_ids", None)
                av.pop("_covered_ids", None)
                parts.append("## 资产覆盖\n" + json.dumps(av, ensure_ascii=False, indent=1))
        if section in ("all", "findings"):
            stats = self.bb.conn.execute(
                "SELECT severity, COUNT(*) AS n FROM findings WHERE project_id=?"
                " GROUP BY severity", (self.project_id,)).fetchall()
            ver = self.bb.conn.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE project_id=?"
                " AND status='verified'", (self.project_id,)).fetchone()
            recent = self.bb.conn.execute(
                "SELECT id, title, severity, status, category, created_at"
                " FROM findings WHERE project_id=?"
                " ORDER BY created_at DESC LIMIT 20", (self.project_id,)).fetchall()
            parts.append("## 发现\n" + json.dumps(
                {"by_severity": {r["severity"]: r["n"] for r in stats},
                 "verified_total": ver["n"],
                 "recent": [dict(r) for r in recent]},
                ensure_ascii=False, indent=1))
        if section in ("all", "events"):
            evs = self.bb.recent_events(self.project_id, tail=50)
            lines = [
                f"  #{e['id']} [{e['kind']}] {e['author']}: "
                f"{json.dumps(e['payload'], ensure_ascii=False)[:300]}"
                for e in evs
            ]
            parts.append("## 事件尾部 50 条\n" + ("\n".join(lines) or "  （无）"))
        return "\n\n".join(parts)

    def _tool_budget_status(self) -> str:
        """预算用量（gate 同数据源：orchestrator_state 用量行 + 项目 config 的
        autonomy 预算项；不做闸门判定——闸门仍是 API 注入的 gate 回调）。"""
        usage = self.bb.usage_state_get(self.project_id)
        auto: dict[str, Any] = {}
        row = self.bb.conn.execute(
            "SELECT config FROM projects WHERE id=?", (self.project_id,)).fetchone()
        if row and row["config"]:
            try:
                auto = (json.loads(row["config"]) or {}).get("autonomy") or {}
            except Exception:  # noqa: BLE001
                auto = {}
        used = (usage.get("tokens_in") or 0) + (usage.get("tokens_out") or 0)
        tb_raw = auto.get("token_budget")
        tkb_raw = auto.get("task_budget")
        # null 显式解释为「不限」（2026-09-24）：此前裸抛 null，调用方（含 LLM）
        # 普遍误读为「预算没接线」。原值保留 + *_effective 给确定语义。
        tb_eff = tb_raw if (isinstance(tb_raw, int) and tb_raw > 0) else "unlimited"
        tkb_eff = tkb_raw if (isinstance(tkb_raw, int) and tkb_raw > 0) else "unlimited"
        out: dict[str, Any] = {
            "tokens": {
                "in": usage.get("tokens_in") or 0, "out": usage.get("tokens_out") or 0,
                "cache_read": usage.get("tokens_cache_read") or 0,
                "cache_creation": usage.get("tokens_cache_creation") or 0,
                "llm_calls": usage.get("llm_calls") or 0,
            },
            "token_budget": tb_raw,
            "token_budget_effective": tb_eff,   # unlimited=不限（硬闸不拦、80% 警不发）
            "tasks_published": usage.get("tasks_published") or 0,
            "task_budget": tkb_raw,
            "task_budget_effective": tkb_eff,   # unlimited=不限（只计自主发布）
            "budget_warned": bool(usage.get("budget_warned")),
        }
        tb = out["token_budget"]
        if isinstance(tb, int) and tb > 0:
            out["tokens"]["used"] = used
            out["tokens"]["remaining"] = max(tb - used, 0)
            out["tokens"]["pct"] = round(used / tb, 4)
        tkb = out["task_budget"]
        if isinstance(tkb, int) and tkb > 0:
            out["tasks_remaining"] = max(tkb - out["tasks_published"], 0)
        return json.dumps(out, ensure_ascii=False, indent=1)

    def _tool_session_list(self) -> str:
        """会话窗清单：状态/角色/绑定任务/未读数/最后活动时间（事件表 GROUP BY
        一条 SQL，无 N+1）。步数为会话内存态，此处不展示。"""
        sessions = self.bb.list_sessions(self.project_id)
        last_act = {
            r["session_id"]: r["last_at"] for r in self.bb.conn.execute(
                "SELECT session_id, MAX(created_at) AS last_at FROM events"
                " WHERE project_id=? AND session_id IS NOT NULL AND session_id!=''"
                " GROUP BY session_id", (self.project_id,)).fetchall()
        }
        out = []
        for s in sessions:
            meta = s.get("meta")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta or "{}")
                except Exception:  # noqa: BLE001
                    meta = {}
            out.append({
                "id": s.get("id"), "name": s.get("name"), "role": s.get("role"),
                "status": s.get("status"),
                "bound_task_id": (meta or {}).get("bound_task_id"),
                "worker_armed": (meta or {}).get("worker_armed"),
                "unread": s.get("unread") or 0,
                "last_event_at": last_act.get(s.get("id")),
            })
        return json.dumps({"sessions": out}, ensure_ascii=False, indent=1)

    def _tool_delegate(
        self, objective: str, task_type: str = "generic", role: str = "",
        target_session: str = "", force_new_window: bool = False,
        scope: str = "", noise_budget: str | None = None,
        conflict_keys: list[str] | None = None, priority: int = 2,
        refs: list[str] | None = None, parent_id: str | None = None,
    ) -> str:
        """向会话窗委派委托（docs/plans/session-centric-orchestration.md §4.3）。
        选窗：target_session 指定 > 复用同类 idle 武装窗 > 开新窗；L1 开窗走
        审批（批准=开窗+写入委托+带活起跑），L2 直接开窗；窗忙且被指定 →
        委托进窗内队列。"""
        # C1 单轮委派硬闸：分批 3-5/轮，防一轮刷爆（与提案同闸）
        if self._publish_count >= self.config.max_publish_per_tick:
            return (f"[拒绝] 本轮委派已达上限 max_publish_per_tick="
                    f"{self.config.max_publish_per_tick}；分批委派——本轮先 done，"
                    "窗空退后下一轮 tick 续批（态势含 uncovered 资产清单）")
        # role 前置校验（拼错专家不静默回退）；无 track 不校验
        role = (role or "").strip()
        if role and self.track and not expert_exists(self.packs_root, role, self.track):
            return (f"[拒绝] 专家不在池内或不可服务本轨: {role!r}；"
                    f"请使用 experts/ 池内专家 id（或留空不限）")
        # 角色白名单（config.allowed_roles 配置时生效，None=不限）：开窗前拦，
        # 不提审批、不建窗（track 过滤已由 expert_exists 把关）
        if role and self.config.allowed_roles is not None \
                and role not in self.config.allowed_roles:
            return (f"[拒绝] 角色 {role!r} 不在白名单 {self.config.allowed_roles}；"
                    "请改用白名单内专家或先调整配置")
        # 去重：命中同指纹 open/claimed 委托 → 复用不新建
        fp = dedup_fp(task_type, scope, objective)
        dup = self.tq.find_dedup_target(self.project_id, fp)
        if dup is not None:
            return (f"[复用] 已存在同目标委托 {dup['id']}（status={dup['status']}），"
                    f"本轮不重复委派；避让请参考其 conflict_keys")
        # 分阶段入场门（直接委派与提案同闸；发布 API 422 是第二层）
        gate_msg = self._phase_gate_reject(task_type)
        if gate_msg:
            return gate_msg
        # task_type 注册校验前置（与 L0 提案同护栏）：未注册直接拒绝，不开窗、
        # 不提审批、不发提案（否则会留下「窗建了、委托写不进」的孤儿窗）
        if self.track:
            try:
                _check_task_type(task_type, self.task_types.keys())
            except ValueError as e:
                return f"[拒绝] {e}"
        # 父子关系 + 编排拆解深度 1 层前置（与 tq.publish 同护栏）：开窗前拦
        if parent_id:
            try:
                self.tq.check_parent(self.project_id, parent_id, enforce_depth=True)
            except ValueError as e:
                return f"[拒绝] {e}"
        # L0 提案模式：校验照跑、不写实体，只发 orch.proposed
        if self.config.propose_only:
            if noise_budget is None:
                noise_budget = self.task_types.get(task_type, "passive")
            if noise_budget not in {"passive", "low", "medium", "high"}:
                return f"[拒绝] 非法 noise_budget: {noise_budget}"
            if noise_budget != "passive" and not conflict_keys:
                return "[拒绝] 非 passive 委托必须提供 conflict_keys（active 互斥的依据）"
            try:
                _check_task_type(task_type,
                                 self.task_types.keys() if self.track else None)
            except ValueError as e:
                return f"[拒绝] {e}"
            self._publish_count += 1
            return self._propose("delegate", {
                "objective": objective, "role": role, "scope": scope,
                "task_type": task_type,
                "noise_budget": noise_budget, "priority": priority,
                "conflict_keys": conflict_keys or [], "refs": refs or [],
                "target_session": target_session,
                "force_new_window": force_new_window,
                "parent_id": parent_id})
        # 自主预算硬闸（回调实时重读项目配置）
        if self.gate is not None:
            reason = self.gate("delegate")
            if reason:
                return f"[拒绝] {reason}"
        # 噪声缺省 = 注册表该类型默认值；轨未接线回退 passive
        if noise_budget is None:
            noise_budget = self.task_types.get(task_type, "passive")
        if noise_budget not in {"passive", "low", "medium", "high"}:
            return f"[拒绝] 非法 noise_budget: {noise_budget}"
        # active 委托冲突键前置校验（与 tq.publish / L0 提案同护栏）：开窗前拦，
        # 不留「窗开了、委托写不进」的孤儿窗
        if noise_budget != "passive" and not conflict_keys:
            return "[拒绝] 非 passive 委托必须提供 conflict_keys（active 互斥的依据）"
        # 同目标防碎闸前置（镜像 tasks.py 事务内计数）：达阈值不开窗、不提审批
        target_keys = target_keys_of(scope, conflict_keys)
        if target_keys:
            n_target = 0
            for r in self.bb.conn.execute(
                "SELECT scope, conflict_keys FROM tasks"
                " WHERE project_id=? AND status IN ('open','claimed')",
                (self.project_id,),
            ).fetchall():
                try:
                    row_keys = json.loads(r["conflict_keys"] or "[]")
                except ValueError:
                    row_keys = []
                if target_keys & target_keys_of(r["scope"], row_keys):
                    n_target += 1
            if n_target >= MAX_TASKS_PER_TARGET:
                return (f"[拒绝] 同目标 {'、'.join(sorted(target_keys))} 在队"
                        f"（open+claimed）任务已达 {n_target} 个（阈值 "
                        f"{MAX_TASKS_PER_TARGET}）——请先消化存量或合并范围")
        # ---- 选窗 ----
        sid = (target_session or "").strip()
        if sid:
            srow = self.bb.get_session(sid)
            if srow is None or srow.get("status") == "closed":
                return f"[拒绝] 指定会话窗不存在或已关闭: {sid}"
            if srow.get("status") == "paused":
                return f"[拒绝] 会话窗 {sid} 处于暂停态，暂不接委托"
        elif not force_new_window:
            sid = self._pick_reusable_window(role, task_type)
        created = False
        if not sid:
            auto = self.autonomy_provider() if self.autonomy_provider is not None else None
            if auto is not None and auto.get("level") == "L1":
                # L1 开窗审批：批准后处理器开窗 + 写入委托 + 带活起跑
                appr = self.bb.request_approval(
                    self.project_id,
                    {"op": "delegate_window", "role": role or "_generalist",
                     "objective": objective, "task_type": task_type,
                     "scope": scope, "noise_budget": noise_budget,
                     "conflict_keys": conflict_keys or [], "priority": priority,
                     "refs": refs or []},
                    risk="low" if noise_budget == "passive" else "medium",
                    requested_by="orchestrator")
                self._publish_count += 1
                return (f"已提交委派审批 {appr['id']}（开 {role or '通用'} 窗执行）："
                        "人类批准后系统自动开窗、写入委托并带活起跑；本轮可继续其他"
                        "决策或 done，审批结果下轮 tick 经事件可见")
            if self.session_factory is None:
                return "[错误] 未配置 session_factory，无法开窗"
            if len(self.live_sessions) >= self.config.max_sessions:
                return (f"[拒绝] 活跃会话已达上限 {self.config.max_sessions}，"
                        "请复用现有会话")
            try:
                sess = self.session_factory(role or "_generalist")
            except FileNotFoundError as e:
                return f"[拒绝] {e}"
            except Exception as e:  # noqa: BLE001
                return f"[错误] 开窗失败: {type(e).__name__}: {e}"
            sid = sess.session["id"]
            self.live_sessions[sid] = sess
            created = True
            self._spawned.append({"session_id": sid, "role": role or "_generalist"})
            # 开窗即事件（与 API 侧 _bind_task_window 同口径）：审计/新壳会话流据此呈现
            self.bb.append_event(
                self.project_id, "session.spawned",
                {"role": role or "_generalist", "session_id": sid,
                 "origin": "orchestrator-delegate"},
                session_id=sid, author="orchestrator")
        # ---- 写委托（归属窗已定） ----
        try:
            task_id = self.tq.publish(
                self.project_id, objective, scope=scope, task_type=task_type,
                noise_budget=noise_budget, priority=priority,
                conflict_keys=conflict_keys, created_by="orchestrator",
                allowed_types=(self.task_types.keys() if self.track else None),
                refs=refs, role=role, target_session=sid, parent_id=parent_id,
                parent_depth_limit=1)
        except ValueError as e:
            return f"[拒绝] {e}"
        self._publish_count += 1
        self._published.append(task_id)
        # 委托事件：会话流呈现「🧭 编排器委派」
        self.bb.append_event(
            self.project_id, "delegation.posted",
            {"task_id": task_id, "objective": objective, "task_type": task_type,
             "created_by": "orchestrator", "role": role, "new_window": created},
            session_id=sid, author="orchestrator")
        # 回调（API 侧）：usage 计数 + idle 窗起跑 / 忙窗排队
        if self.on_task_published is not None:
            try:
                self.on_task_published(task_id)
            except Exception:  # noqa: BLE001
                log.exception("tasks_published 回调失败")
        return (f"task={task_id} 已委派给会话 {sid}（{task_type}/{noise_budget}"
                + (f"/role={role}" if role else "")
                + ("，新窗" if created else "，复用窗")
                + "）：窗空闲已起跑 / 窗忙已排队")

    def _pick_reusable_window(self, role: str, task_type: str) -> str:
        """选复用窗（保守规则）：armed + idle、role 给定时窗角色匹配、且干过同
        task_type 终态委托；多个按最近终态时间取新。无 → ''（交调用方开窗）。"""
        sessions = {s["id"]: s for s in self.bb.list_sessions(self.project_id)}
        last_done: dict[str, str] = {}
        for t in self.tq.list_tasks(self.project_id):
            if t.get("target_session") and t["status"] in {"done", "failed"} \
                    and t.get("task_type") == task_type:
                wsid = t["target_session"]
                ts = t.get("updated_at") or t.get("created_at") or ""
                if wsid not in last_done or ts > last_done[wsid]:
                    last_done[wsid] = ts
        candidates: list[tuple[str, str]] = []
        for wsid, row in sessions.items():
            if row.get("status") != "idle" or wsid not in last_done:
                continue
            if role and row.get("role") != role:
                continue
            meta = row.get("meta")
            meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
            if not meta.get("worker_armed"):
                continue
            candidates.append((last_done[wsid], wsid))
        return sorted(candidates)[-1][1] if candidates else ""

    def _phase_gate_reject(self, task_type: str) -> str:
        """入场门校验（M2）：被拦返回 [拒绝] 原因串，放行返回空串。
        meta/state 未接线（脚本/旧测试）一律放行；校验自身异常也放行
        （门是工作流护栏非安全边界，发布 API 侧还有第二层拦截兜底）。"""
        if self.meta_loader is None or not self.track:
            return ""
        try:
            meta = self.meta_loader() or {}
            idle = 0
            if self.state_loader is not None:
                try:
                    idle = int(self.state_loader().get("derive_idle_rounds", 0))
                except Exception:  # noqa: BLE001
                    idle = 0
            reason = phases.gate_block_reason(
                self.bb, self.project_id, meta, self.track, self.packs_root,
                task_type=task_type, idle_rounds=idle)
            return f"[拒绝] {reason}" if reason else ""
        except Exception:  # noqa: BLE001
            log.exception("阶段入场门校验失败（放行）")
            return ""

    def _tool_cancel_task(self, task_id: str, reason: str = "") -> str:
        """取消委托（M4 C1，orchestrator-efficiency）：open/claimed → failed
        （blocked_reason=cancelled）。自主档三分流：L0 提案 / L1 审批卡 /
        L2 直接执行 + 打断在跑窗。打断复用人工中断原语 request_abort（当前步
        做完收口，现场快照保留）——窗保持待命可复用，cancel ≠ 关窗。"""
        task = self.tq.get_task(task_id)
        if task is None or task["project_id"] != self.project_id:
            return f"[错误] 任务不存在: {task_id}"
        reason = (reason or "").strip()
        if not reason:
            return "[拒绝] 取消必须填 reason（写清为什么，人类在事件流审计）"
        if task["status"] not in ("open", "claimed"):
            return (f"[拒绝] 任务 {task_id} 状态为 {task['status']}"
                    f"（{task.get('blocked_reason') or 'error'}），仅 open/claimed 可取消；"
                    "failed/awaiting_human 请用 requeue_task 放回")
        if self.config.propose_only:
            return self._propose("cancel_task", {"task_id": task_id, "reason": reason})
        auto = self.autonomy_provider() if self.autonomy_provider is not None else None
        if auto is not None and auto.get("level") == "L1":
            appr = self.bb.request_approval(
                self.project_id,
                {"op": "cancel_task", "task_id": task_id,
                 "objective": (task["objective"] or "")[:120], "reason": reason},
                risk="low", requested_by="orchestrator")
            return (f"已提交取消审批 {appr['id']}：人类批准后任务转 failed（cancelled）"
                    "并打断在跑窗；本轮可继续其他决策或 done")
        res = self.tq.cancel_task(task_id, by="orchestrator", reason=reason)
        self._interrupt_window(res.get("claimed_by"))
        tail = "；在跑窗已打断（快照保留可续跑）" if res.get("claimed_by") else ""
        return f"task={task_id} 已取消（cancelled）{tail}"

    def _tool_requeue_task(self, task_id: str) -> str:
        """放回待认领（M4 C1）：failed/awaiting_human → open（reopen 原语——
        awaiting_human 本就是 failed+blocked_reason 档；attempts 履历保留，
        target_session 保留=原绑定窗优先续跑）。自主档三分流同 cancel_task。"""
        task = self.tq.get_task(task_id)
        if task is None or task["project_id"] != self.project_id:
            return f"[错误] 任务不存在: {task_id}"
        if task["status"] != "failed":
            return (f"[拒绝] 任务 {task_id} 状态为 {task['status']}，"
                    "仅 failed/awaiting_human 可放回")
        if self.config.propose_only:
            return self._propose("requeue_task", {"task_id": task_id})
        auto = self.autonomy_provider() if self.autonomy_provider is not None else None
        if auto is not None and auto.get("level") == "L1":
            appr = self.bb.request_approval(
                self.project_id,
                {"op": "requeue_task", "task_id": task_id,
                 "objective": (task["objective"] or "")[:120],
                 "blocked_reason": task.get("blocked_reason") or "error"},
                risk="low", requested_by="orchestrator")
            return (f"已提交放回审批 {appr['id']}：人类批准后任务回到待认领"
                    "（原绑定窗优先续跑）；本轮可继续其他决策或 done")
        self.tq.reopen(task_id, by="orchestrator")
        return "task={} 已放回待认领（原绑定窗优先续跑，调度器自动重开窗）".format(task_id)

    def _interrupt_window(self, sid: str | None) -> None:
        """打断在跑窗（M4 cancel 链路）：request_abort 让当前步尽快收口——
        在跑窗走 _abort_current_task（fail 撞 ClaimError 被吞、会话空闲），
        空闲窗标志在下次检查点自清（loop.py 无任务只清标志先例）。
        只打断不关窗：窗保持待命可接新任务。失败只 log——任务行已取消，
        主语义已达成，打断失败不回滚。"""
        if not sid:
            return
        sess = self.live_sessions.get(sid)
        if sess is None:
            return
        try:
            sess.request_abort()
        except Exception:  # noqa: BLE001
            log.exception("打断在跑窗失败 sid=%s", sid)

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
