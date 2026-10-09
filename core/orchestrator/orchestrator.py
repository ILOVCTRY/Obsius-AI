"""主代理 Orchestrator（DESIGN.md §6.4）——普通 LLM 会话 + 特殊工具集，不直接干脏活。

职责四件套：
- 监控：每 tick 回收过期 lease（防会话挂死占坑），把队列空转/低产出摆到 LLM 面前
- 派生：新发现 → 新任务（LLM 决策，delegate 落地）
- 开窗：唯一有权启动新 AI 会话的角色（角色白名单 + 会话数上限约束）
- 汇总：把项目态势写成简报，落 project.digest 事件（人类了解全局的入口）

一轮协调 = tick()：expire_leases → 态势收集 → LLM 决策循环（专用工具集）→ done
人类随时插手：直接向 TaskQueue 发布/取消任务即可，Orchestrator 只消费不垄断（§6.4）。
"""

import copy
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core import phases
from core.agent.execution import ExecutionContext
from core.coverage import coverage_report, effective_status_map
from core.autonomy import record_llm_usage
from core.blackboard import Blackboard
from core.blackboard.store import new_id
from core.llm.provider import assistant_message, tool_results_message
from core.llm.tokenizer import CHARS_PER_TOKEN, context_window_tokens, get_counter
from core.runtime.policy import RUNTIME_LEVELS, VALID_THREAT_CLASSES
from core.skills.experts import expert_exists, list_experts, load_expert
from core.skills.taxonomy import GENERIC_TASK_TYPE, load_task_types
from core.team.store import TeamStore  # 指挥组建队伍（Team/Member/Run direct execution）

log = logging.getLogger(__name__)


ORCH_SYSTEM_PROMPT = """你是项目主代理（指挥）：**组建队伍**（build_team）与统筹全局是你的核心
职责，也可**亲自执行**（execute）小范围验证/补刀；大块并行工作交给团队子代理或委派会话执行。

## 本轮态势
{overview}
{role_catalog}
{autonomy_notice}{goal_section}{phase_section}{mission_section}{campaign_section}
## 可用工具
- build_team：**组建 Agent 团队（你的核心职责）**——给团队名/共享目标/成员名册（成员 role 从本轨专家池选，留空=按职责现场定义的动态成员）。创建后是 draft，需人类在「指挥」页查看并配置（preflight + 四项确认）后才启动；本工具只登记 roster，不启动、不派单。
- execute：**指挥亲自执行**一件工作（自己下场，完整 Agent 工具面：命令/文件/黑板/知识库/浏览器…），产出落黑板。适合小范围验证/补刀/需要你自己判断的活；大块并行工作请 build_team 组队。受活跃窗上限约束。
- delegate：发布委托（像人类给 AI 发消息让他干活）。task_type 必须是场景轨 task_types.yaml 已注册类型（未注册会被拒收——拼错的类型会让任务饿死），决定哪些角色能认领（先看角色目录与已有会话）；noise_budget 缺省取该类型注册表默认值；会产生噪声的动作（主动探测/执行样本）必须给 conflict_keys；委托若以某发现为依据（如「验证 find-x」），把该发现 id 填 refs——该发现事后被推翻时，执行者会立刻收到强制自评通知。默认由系统选窗：优先复用空闲同角色窗，无合适窗则开新窗；force_new_window=true 强制新窗、target_session 指定窗；受角色白名单 {allowed_roles} 与活跃窗上限 {max_sessions} 约束。长探测/扫描类 objective 写明建议超时（run_cmd 默认 120s，C3）。
- cancel_task / requeue_task：生命周期收编（见纪律 9）。
- write_digest：写项目简报（给人类看的全局摘要：进展/发现/风险/下一步）。
- done：结束本轮协调。
- 只读查询四工具（task_detail / bb_overview / budget_status / session_list）：
  用于核对与决策——对执行者结论存疑时先 task_detail 拉全文再判断，不要只凭
  态势摘要下结论；查询是为了派得更准、验收得更实。

## 主代理纪律（硬规则）
1. 优先组建队伍（build_team）把大块/多角色工作分派出去；小范围验证/补刀/需要你亲自判断时 execute。团队子代理是独立会话，看不到你的态势与对话——成员的 responsibility 必须自包含。
2. 遵循「分析-分解-分派」：复杂目标先拆成自足的小任务，再按角色目录对号编入团队；
   每位成员写清目标/依据/完成定义与建议 runtime；组队受角色白名单 {allowed_roles} 与
   会话上限 {max_sessions} 约束。
3. 新发现 severity>=medium 且 unverified 的，应编入验证团队或亲自 execute 验证，别让它
   烂在黑板里（category=intel 的有效发现/提示类除外——它们不是漏洞，无需验证复现）。
4. 组队/执行被拒（白名单/上限）即改道：缩编、换角色或先收尾。
5. 距上次简报 >= {digest_every} 轮时，本轮必须 write_digest。
6. 决策完毕调用 done。
7. 团队创建后是 draft：人类在「指挥」页查看并配置、确认后才启动——你只负责组建与统筹，
   不要假设它已在跑；长探测/扫描类责任写明建议超时（run_cmd 默认 120s，不提醒会被截杀）。
8. 收敛判据（轨级行为语义）：mission 判据全部达成、或资产穷尽（uncovered=0 且无可推进
   发现）才 done——不要因为单轮零产出就提前收摊；对照上方行动边界段的判据清单逐条评估。
{delegation_discipline}"""


# L1（任务自动·执行审批，v0.72）系统提示追加段：委派发布即建（待命），执行等人类批准
L1_AUTONOMY_NOTICE = """## 自主档位 L1（任务自动·执行审批）
delegate 发布即自动建专属待命窗（待命不耗 LLM），并自动提「执行审批」（action 带 task_id）：
人类批准后系统启动该窗执行委托；批准前任务已入队、只是待命等待。
- 委托的 objective 必须自包含（执行者看不到你的态势与对话），写清目标/依据/完成定义；
- 等待审批期间可继续发 passive 委托，或 done 结束本轮；批准/拒绝结果下轮 tick 经事件可见。
- 委派审批积压达 max_publish_per_tick 时会停发新委派（先让人类消化，下轮恢复）——
  态势里见 pending_delegate 计数即「有审批卡排队」，别再提同款/同目标。"""


# L0（全手动）系统提示追加段（批 6）：publish/spawn 只产提案，不写实体
L0_AUTONOMY_NOTICE = """## 自主档位 L0（全手动·提案模式）
你不能直接建队、执行或派任务：build_team / execute / delegate / cancel_task / requeue_task
只生成人类提案（orch.proposed 事件），
人类在事件流逐条「采纳」后才真正落地（任务以人类名义入队、窗由人类开）。
- 提案不消耗任何预算、不占会话上限，但参数校验照跑：task_type 必须是本轨注册类型、
  非 passive 仍须 conflict_keys、role 仍受白名单/上限约束，填错会被拒收；
- 一轮可提多条；write_digest 照常写简报；决策完毕 done。"""

# 自动渗透启动前研判模式（auto-attack 2026-09-28）：只读分析，产出渗透计划文本。
# 双保险：工具面只留只读查询+done（写类工具根本不下发），system 提示同步收紧。
ANALYZE_ONLY_NOTICE = """## 本轮：自动渗透启动前态势研判（只读·分析模式）
这是一轮启动前研判，不是常规协调轮：不要执行任何派单/开窗/取消动作
（delegate / cancel_task / requeue_task / write_digest 已全部禁用，调用会失败）。
请通读当前态势（阶段目标、资产、发现、死路、意图、任务与窗口状态），输出一份
渗透测试计划，内容包括：
1. 态势小结：已掌握什么、缺什么、当前瓶颈；
2. 建议攻击路径：按优先级列出 2-4 条候选路径，每条说明依据（引用发现 id）、
   预期产出与风险/噪声等级；
3. 首轮动作建议：开跑后第一轮应该派什么任务、为什么。
计划写完整、写具体——人类将根据这份计划决定是否启动自动渗透。
计划正文直接作为回复输出，然后 done 结束（done 不需要带任何参数）。"""


# 委托纪律（M1，orchestrator-coordination-fusion，2026-10-04）：借鉴 cc-haha 协调者
# 模式（coordinatorMode.ts）的四条纪律，tick 与对话轮共用同槽 {delegation_discipline}。
# 纯提示词注入（无控制流）——「综合不可下放 / 委托单自包含 / 续用 vs 新开 / 并行 fan-out」
# 是编排器写高质量委托单的软约束；硬约束（去重/闸门/上限）仍在服务端。
DELEGATION_DISCIPLINE = """
## 委托纪律（协调者协议）
- **综合是你的活，不可下放**：收到执行者回执后，先自己读懂（存疑就 task_detail 拉全文），
  落到具体资产/发现/文件，再写下一张委托单。**禁止**「基于你的发现继续…」「按你的判断
  修复…」这类把理解下放给执行者的委托——理解永远由你完成。
- **委托单必须自包含**：执行者是独立会话窗，**看不到本轮态势与你的对话**。每张委托单写清
  目标、依据（refs 填资产/发现 id）、完成定义与边界，否则执行者只能猜。
- **续用窗还是新开窗**（默认系统复用空闲同角色窗）：按上下文重叠度判断——研究恰好覆盖待改
  目标→续用；研究宽泛而实现聚焦→新开；修正/延伸刚做完的工作→续用；验证他人刚写的产出→
  新开（避免实现假设污染）；方向整体错误→新开（别把错误路径带进重试）。需要时用
  force_new_window=true 强制新开。
- **并行 fan-out**：只读研究类委托可一轮并行多发；写入类按目标/资产分区串行（同目标在队
  达上限会被拒收）。
"""


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
        "name": "build_team",
        "description": "组建一支 Agent 团队（Team）——指挥的核心职责。给出团队名、共享目标与"
                       "成员名册：成员的 role 从本轨专家池选（留空=按职责现场定义的动态成员）。"
                       "创建后团队处于 draft（未启动、未派单），需人类在「指挥」页查看并配置"
                       "（preflight + 四项确认）后才启动执行。适合需要多角色协作/并行的大块目标。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "团队名称（如「外网打点小队」）"},
                "goal_text": {"type": "string", "description": "团队共享目标（自包含，写清要达成什么）"},
                "members": {
                    "type": "array", "minItems": 1,
                    "description": "成员名册（至少一位）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "member_key": {"type": "string", "description": "成员标识（团队内唯一，如 recon/exploit）"},
                            "label": {"type": "string", "description": "成员显示名（如「侦察手」）"},
                            "responsibility": {"type": "string", "description": "该成员职责/执行目标（自包含，执行者看不到团队对话）"},
                            "role": {"type": "string", "description": "执行专家 id（本轨 experts/ 池内）；留空=按 responsibility 现场定义的动态成员"},
                            "runtime": {"type": "string", "enum": ["", "host", "wsl", "docker", "sandbox"], "description": "运行环境（缺省 host）"},
                            "threat_class": {"type": "string", "enum": ["trusted", "untrusted", "malware_live", "unknown"], "description": "威胁等级（缺省 trusted）"},
                            "max_steps": {"type": "integer", "description": "该成员步数预算（可选）"},
                        },
                        "required": ["member_key", "responsibility"],
                    },
                },
            },
            "required": ["name", "members"],
        },
    },
    {
        "name": "execute",
        "description": "指挥亲自执行一件工作（自己下场）：以完整 Agent 工具面（命令/文件/黑板/"
                       "知识库/浏览器…）按 objective 执行，产出落黑板。适合小范围验证、补刀、"
                       "需要指挥自己判断的活；大块并行工作请用 build_team 组队。受活跃窗上限约束。",
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "执行目标（自包含，写清要做什么、产出什么）"},
            },
            "required": ["objective"],
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


# A5 优先级重排已随任务机制退役（2026-10-06）：REPLAN_TOOLS / REPLAN_SYSTEM_PROMPT /
# replan_priorities 一并删除。


# 对话化编排器（M1，§4.2）：对话插队轮系统提示——复用 tick 态势组装槽 +
# goal_section + persona_section，纪律段换成对话口径（可对话/可发布/走闸门/
# goal 变更须人类确认）。发布走与 tick 完全相同的工具闸门（dedup/gate/L0 提案/
# L1 审批），对话只是起草方式变了。
CHAT_SYSTEM_PROMPT = """你是项目主代理（指挥），现在处于**对话轮**：人类正在与你直接交流。
你可以回答问题、商议目标、给出计划；需要动手时用工具——组建队伍（build_team）、亲自执行
（execute）、发布委托与开窗走全部闸门（预算硬闸/去重/L0 提案/L1 审批，与巡检 tick 同源），
被拒就向人类转述原因。

## 当前态势
{overview}
{role_catalog}
{autonomy_notice}{goal_section}{phase_section}{mission_section}{campaign_section}{persona_section}
## 对话纪律
1. 用人类的语言简洁作答，结论先行；问下一步计划时用上方态势作答（门没过就说还差什么）。
2. 执行者看不到对话上下文：delegate 的 objective 必须自包含；系统会自动选窗
   （优先复用空闲同角色窗）或开新窗。
3. 当人类要求组建 Agent 团队、合理分工或按计划协作时，用 **build_team** 组建团队：
   给团队名、共享目标与成员名册（成员 role 从本轨专家池选，留空=按职责现场定义的
   动态成员），成员的 responsibility 写清各自执行目标。build_team 只登记 draft 团队，
   **不启动、不派单**——回复中说明分工与安全边界，人类在「指挥」页确认（preflight +
   四项确认）后才会启动执行。
4. 阶段目标（goal）的变更须由人类确认：你可以在对话里给出结构化草案
   （text/criteria/phase），由人类在编排页签 goal 条确认落盘；未确认前不要当作已生效。
5. 回答完毕调用 done 结束本轮；纯问答（无需动手）直接 done。
{delegation_discipline}"""

# 对话轮 LLM 步上限（插队轮不跑长决策链；发布分批语义不适用——人类在场可连续对话）
CHAT_MAX_STEPS = 8

# 对话历史手动压缩（/compact，2026-10-06）：与 Agent 侧同精神——旧 orch.chat 消息经
# LLM 压成一段摘要，后续对话轮从「摘要 + 近期消息」起跑（对话历史原本固定取最近 40
# 条，长对话下早期关键决策会被挤出窗口；摘要把它们沉淀下来）。
ORCH_COMPACT_SYSTEM = (
    "你是项目主代理对话历史压缩器：把指挥与人类的既往对话压缩成一段摘要，供后续"
    "对话续接。只保留事实与决策：人类原话中的目标/约束**逐字保留**、已确认的方案与"
    "行动边界、已组建的团队/已下达的执行、待办与未决问题。黑板对象一律引用 id"
    "（如 find-xxxx、as-xxxx、team-xxxx），不要内联全文。中文输出，Markdown，不要客套。"
)

# 轮末自动压缩阈值（/context，2026-10-06）：指挥上下文占用达窗口 85% 即在对话轮
# 结束后持久压缩（与手动 /compact 同语义）。窗口分母 = 供应商 model_context（缺省 256K）。
ORCH_AUTOCOMPACT_RATIO = 0.85

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
    # 批 6（§6.8）：L0 提案模式——delegate/cancel_task/requeue_task 只发 orch.proposed
    # 事件、不写实体（校验照跑）；API 按实时档位 level=="L0" 注入。
    propose_only: bool = False
    # 自动渗透研判模式（auto-attack 2026-09-28）：工具面只留只读查询+done，
    # 产出为渗透计划文本（result.analysis）——启动前「先分析后确认」的后半段。
    analyze_only: bool = False
    # 指挥亲自执行（2026-10-06）：execute 工具单次执行的步数上限——tick 内同步
    # 执行，防一次跑满默认预算把编排轮拖死。
    execute_max_steps: int = 40


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
        # level=="L1" 时 delegate 转人工审批而非直接开窗。None=脚本/旧测试
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
        self._teams: list[dict[str, Any]] = []  # 指挥组建的队伍（本轮）
        self._publish_count = 0  # C1：单轮发布计数（tick 每轮重置）
        self._proposals: list[dict[str, Any]] = []  # 批 6：L0 提案 {op,args,event_id}
        self._digest: str | None = None

    def _orch_tools(self) -> list[dict[str, Any]]:
        """工具表副本：把本轨合法 task_type 作为 enum 下发（让 LLM 一次填对，
        服务端注册表拒收仍是最终护栏）；无 track 时退回原表。
        M2（2026-09-22）：追加只读查询四工具（与轨无关，无 enum 注入需求）。
        auto-attack 研判模式（2026-09-28）：只下发只读查询 + done——写类工具
        （delegate/cancel_task/requeue_task/write_digest）根本不出现在工具面，
        LLM 想调也调不了（硬约束，比提示词约束可靠）。"""
        base = ORCH_TOOLS + ORCH_QUERY_TOOLS
        if self.config.analyze_only:
            return [t for t in ORCH_QUERY_TOOLS if t["name"] != "done"] + [
                t for t in ORCH_TOOLS if t["name"] == "done"]
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
        _tool_delegate 硬拒（此处只预告）。轨无阶段剧本/meta 未接线=空段。"""
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
                lines.append("- 重心配额建议（软引导，delegate 类型配比向此倾斜）："
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
            hv = " ".join(a["value"] for a in self._stats().get("high_value", []))
            query = (goal_text or mission) + " " + hv
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

    def _overview(self) -> str:
        stats = self._stats()
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
            "\n\n## 自上轮以来的新事件\n" + ("\n".join(event_lines) or "  （无）"))
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
        不注入也不消费 C2 指令（存量指令仍由 tick 消费）。"""
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
        findings = self.bb.list_findings(self.project_id)
        sessions = self.bb.list_sessions(self.project_id)
        cfg = self.bb.get_project(self.project_id)["config"] or {}
        mission_view: dict[str, Any] = {"track": self.track or "pentest"}
        if isinstance(cfg.get("mission"), dict):
            mission_view["mission"] = cfg["mission"]
        if isinstance(cfg.get("redteam_roe"), dict):
            mission_view["roe"] = cfg["redteam_roe"]
        assets_view = self._assets_view()
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
        recent_runs = self._recent_runs_view()
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
            "teams": self._teams_view(),
            "findings": [{"id": f["id"], "vuln_class": f["vuln_class"], "title": f["title"][:60],
                          "severity": f["severity"], "status": f["status"],
                          "category": f.get("category") or "vuln"} for f in findings_top],
            "findings_total": len(findings),
            "findings_truncated": len(findings) > len(findings_top),
            "sessions": [{"id": s["id"], "role": s["role"], "status": s["status"]}
                         for s in sessions_live],
            "sessions_closed": len(sessions) - len(sessions_live),
            "live_windows": len(self.live_sessions),
            "approvals": self._approvals_view(),
            **recent_runs,
            **assets_view,
        }

    def _teams_view(self) -> dict:
        """指挥态势的团队视图（2026-10-06 任务退役后）：Team 名册 + 最近 Run 状态。
        替代旧 tasks 段——编排器据此判断「有没有在跑的队伍 / 上一轮跑成什么样」。"""
        rows = self.bb.conn.execute(
            "SELECT id,name,status,goal_text FROM teams WHERE project_id=?"
            " ORDER BY updated_at DESC,id", (self.project_id,)).fetchall()
        items = [{"id": r["id"], "name": r["name"], "status": r["status"],
                  "goal": (r["goal_text"] or "")[:120]} for r in rows[:20]]
        return {"total": len(rows), "items": items}

    def _recent_runs_view(self) -> dict:
        """最近 Team Run（新→旧 cap 20）与运行中成员——「做得怎么样」的 Team 口径。"""
        rows = self.bb.conn.execute(
            "SELECT r.id,r.team_id,r.status,r.created_at,r.updated_at,"
            " (SELECT COUNT(*) FROM team_run_members m WHERE m.run_id=r.id) AS members"
            " FROM team_runs r WHERE r.project_id=?"
            " ORDER BY r.created_at DESC, r.id DESC LIMIT 20", (self.project_id,)).fetchall()
        recent_runs = [{"id": r["id"], "team_id": r["team_id"], "status": r["status"],
                        "members": r["members"],
                        "ended_at": r["updated_at"] or r["created_at"]} for r in rows]
        running = self.bb.conn.execute(
            "SELECT rm.id,rm.member_key,rm.status,rm.objective,rm.run_id"
            " FROM team_run_members rm JOIN team_runs r ON r.id=rm.run_id"
            " WHERE r.project_id=? AND rm.status IN ('creating','running')"
            " ORDER BY rm.created_at DESC LIMIT 20", (self.project_id,)).fetchall()
        running_now = [{"id": r["id"], "member_key": r["member_key"], "status": r["status"],
                        "objective": (r["objective"] or "")[:80], "run_id": r["run_id"]}
                       for r in running]
        return {"recent_runs": recent_runs, "running_members": running_now}

    def _assets_view(self) -> dict:
        """C1 资产视图 + 态势增强（HVT 标签/攻击面进度）：**三态**（asset-tri-state，
        2026-10-09）——未测试(`open`) / 已访问(`visited`) / 已测试干净(`tested_clean`)。

        出口两桶（主控派发判据）+ 一计数：
        - `untested`   未测试清单（open，cap 30 带 id，HVT 优先排序）——从没碰过；
        - `in_progress` 已访问清单（visited，cap 20）——碰过、未测尽（**含带洞资产**，
          「有发现 ≠ 测干净」）；
        - `clean_count` 已测试干净计数——只给数不给清单（防重复派）。
        covered = 已测试干净；`high_value.covered` 语义随三态收紧。状态一律读时派生
        （父节点显式 status 被子树覆盖，加子自动破 clean）。"""
        assets = self.bb.list_assets(self.project_id)
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

        def _state_of(a: dict) -> str:
            eff = eff_map.get(a["id"])
            return eff["status"] if eff else (a.get("status") or "open")

        # 三态分桶（对账面资产）：未测试 open / 已访问 visited / 已测干净 tested_clean
        untested = [a for a in targetable if _state_of(a) == "open"]
        visited = [a for a in targetable if _state_of(a) == "visited"]
        clean_ids = {a["id"] for a in targetable if _state_of(a) == "tested_clean"}
        untested.sort(key=lambda a: (a["id"] not in hvt_ids, 0))  # HVT 优先
        visited.sort(key=lambda a: (a["id"] not in hvt_ids, 0))
        covered_ids = clean_ids
        in_progress = [
            {"id": a["id"], "type": a["type"], "value": a["value"][:60],
             "status": "visited",
             "has_findings": bool(eff_map.get(a["id"], {}).get("has_findings"))}
            for a in visited[:20]
        ]
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
                # 三态（2026-10-09）：未测试 / 已访问 / 已测试干净
                "untested_total": len(untested),
                "untested": [{"id": a["id"], "type": a["type"], "value": a["value"][:60]}
                             for a in untested[:30]],
                "in_progress_total": len(visited),
                "in_progress": in_progress,               # 已访问（未测尽，含带洞）
                "clean_count": by_status.get("tested_clean", 0),   # 已测干净（只给数）
                # 兼容键：covered = 已测试干净（旧 uncovered ↔ 新 untested，旧 done_count ↔ 新 clean_count）
                "done_count": by_status.get("tested_clean", 0),
                "uncovered_total": len(untested),
                "uncovered": [{"id": a["id"], "type": a["type"], "value": a["value"][:60]}
                              for a in untested[:30]],
                "coverage": coverage_view,
            },
            "_hvt_ids": hvt_ids,        # 内部复用（_stats 组装 high_value 段），出口前剔除
            "_covered_ids": covered_ids,
        }

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
        self._finished = False
        self._summary = ""
        self._analysis_text = ""  # auto-attack 研判模式：最后一条 assistant 文本=渗透计划
        self._actions = []
        self._published: list[str] = []
        self._spawned: list[dict[str, str]] = []
        self._teams = []
        self._proposals = []
        self._digest: str | None = None
        self._publish_count = 0  # C1：单轮发布硬闸计数（直接发布与 L0 提案同闸）
        overview = self._overview()
        pending_directives = self._pending_directives()  # C2：人类指令随 tick 消费后标 done
        self._consumed_directives = list(pending_directives)
        auto = self.autonomy_provider() if self.autonomy_provider is not None else None
        # 批 6：config.propose_only（API 按 L0 注入）优先；无 provider 的单测也可直配
        # auto-attack：研判模式优先级最高（只读分析，覆盖一切档位提示）
        if self.config.analyze_only:
            autonomy_notice = ANALYZE_ONLY_NOTICE
        elif self.config.propose_only:
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
            goal_section=self._goal_section(),
            phase_section=self._phase_section(),
            mission_section=self._mission_section(),
            campaign_section=self._campaign_section(),
            delegation_discipline=DELEGATION_DISCIPLINE,
        )
        messages: list[dict[str, Any]] = [{
            "role": "user",
            "content": "第 %d 轮协调开始。请决策本轮动作（监控/派生/开窗/汇总），完成后 done。"
                       % self.cycles,
        }]
        if self.config.analyze_only:
            # auto-attack 研判轮：指令换成态势分析（产出即计划文本，人类确认后开跑）
            messages = [{
                "role": "user",
                "content": "自动渗透启动前研判：请按系统提示中的研判要求，通读当前态势"
                           "并输出完整渗透测试计划，写完后 done 结束。",
            }]
        for _step in range(1, self.config.max_steps + 1):
            if self.heartbeat is not None:
                try:
                    self.heartbeat()
                except Exception:  # noqa: BLE001 —— 续租失败不阻断编排
                    log.exception("tick 租约心跳失败")
            skwargs, stream_id = self._stream_kwargs()
            resp = self.llm.chat(messages, system=system, tools=self._orch_tools(),
                                 **skwargs)
            record_llm_usage(
                self.bb, self.project_id, resp.usage, source="orchestrator",
                session_id=None, model=getattr(self.llm, "model", ""))
            self._emit_thinking(resp, _step,
                                "analyze" if self.config.analyze_only else "tick",
                                stream_id=stream_id)
            messages.append(assistant_message(resp))
            if self.config.analyze_only and resp.text.strip():
                self._analysis_text = resp.text.strip()
            if not resp.tool_calls:
                messages.append({"role": "user", "content": "（请调用工具执行动作，或 done 结束本轮）"})
                continue
            tool_results = []
            for tc in resp.tool_calls:
                result = self._dispatch(tc.name, tc.arguments)
                tool_results.append(self.llm.tool_result_message(tc, result)["content"][0])
            messages.append(tool_results_message(tool_results))
            if self._finished:
                return self._finish_tick()
        return self._finish_tick(exhausted=True)

    # ---------- 异常订阅唤醒（对话化编排器 M4，§4.6） ----------

    # 白名单 kind → 冷却窗秒数。只有这里列出的事件才可能唤醒编排器。
    # 2026-10-06 任务退役：task.failed / task.starvation 触发器移除；团队执行失败由
    # team.run.finished（终态）唤醒编排器复盘。
    # 2026-10-09 发现回喂（agent-path-intent-loop M4）：finding.new 进白名单——
    # 高危产出即时唤醒，编排器不必等到下轮 tick 才看到（被动注入见 _stats）。
    # **仅 serious 档触发**（见 WAKE_FINDING_SEVERITIES）：一个项目可产几十条
    # info/low 记录，全量触发会把唤醒轮烧成噪声；「改变打法」的是 high/critical。
    WAKE_TRIGGERS: dict[str, float] = {
        "team.run.finished": 600.0,    # 团队 Run 收尾（完成/失败/取消）→ 唤醒复盘
        "budget.soft_warning": 3600.0,  # 预算 80% 软警（低频，1h 冷却）
        "phase.gate_open": 600.0,      # 阶段出口门满足（五触发之一：goal 阶段门）
        "finding.new": 300.0,          # 高危发现落库 → 唤醒复判是否派生/加派（5min 冷却）
    }
    # finding.new 的严重度闸门：只有这些档位才唤醒（payload.severity）。
    WAKE_FINDING_SEVERITIES = ("high", "critical")
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
            if kind == "finding.new":  # payload 见 store.add_finding 事件契约
                # 严重度闸门：只有 serious 档唤醒（低档发现不改变打法，只堆噪声）。
                sev = str(p.get("severity") or "").strip().lower()
                if sev not in cls.WAKE_FINDING_SEVERITIES:
                    continue
                title = str(p.get("title") or p.get("vuln_class") or "未命名发现")
                aid = str(p.get("target_asset_id") or "").strip()
                summary = (f"[{sev.upper()}] {title}"
                           + (f"（资产 {aid}）" if aid else ""))
            elif kind == "team.run.finished":  # payload={team_id,team_name,run_id,status}
                summary = (f"团队「{p.get('team_name') or p.get('team_id') or ''}」"
                           f"执行收尾：{p.get('status') or ''}")
            else:  # budget.soft_warning=note、phase.gate_open=summary
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
        found = [t for t in triggers if t["kind"] == "finding.new"]
        for t in triggers:
            kind = t["kind"]
            if kind == "finding.new":
                continue  # 成组渲染（见下），避免逐条刷屏
            if kind == "team.run.finished":
                lines.append(f"- 团队执行收尾：{t['summary']}——请复盘产出、"
                             "决定是否新建团队或亲自 execute 补刀")
            elif kind == "budget.soft_warning":
                lines.append("- 预算软警：LLM 用量已达 80%，请评估消耗与剩余工作量")
            elif kind == "phase.gate_open":
                lines.append("- 阶段出口门满足：当前阶段门指标达标，可考虑流转下一阶段")
            else:
                lines.append(f"- {kind}：{t['summary']}")
        if found:
            lines.append(f"- 高危发现落库（{len(found)} 条）：请复判打法——"
                         "是否据此派生新意图/加派子专家深挖、或修正 HVT 与收敛判据。")
            for t in found:
                lines.append(f"  · {t['summary']}")
        return "\n".join(lines)

    # ---------- 对话插队轮（对话化编排器 M1，§4.2） ----------

    def _chat_history(self) -> list[dict[str, Any]]:
        """对话上下文 = events 表 orch.chat 最近 40 条按序组装（单一来源，零新表
        零文件）。human→user / orch→assistant；开头连续的 orch 消息跳过（保证
        messages 首条是 user）。

        手动压缩（/compact，2026-10-06）：最近一条 orch.compact 事件给出摘要 +
        截止 event id——只回放 id 之后的消息，摘要作为首条 user 消息前置。"""
        cutoff, summary = 0, ""
        row = self.bb.conn.execute(
            "SELECT payload FROM events WHERE project_id=? AND kind='orch.compact'"
            " ORDER BY id DESC LIMIT 1", (self.project_id,)).fetchone()
        if row is not None:
            try:
                p = json.loads(row["payload"])
                cutoff = int(p.get("cutoff_id") or 0)
                summary = str(p.get("summary") or "").strip()
            except (ValueError, TypeError):
                cutoff, summary = 0, ""
        rows = self.bb.conn.execute(
            "SELECT payload FROM events WHERE project_id=? AND kind='orch.chat'"
            " AND id>? ORDER BY id DESC LIMIT 40",
            (self.project_id, cutoff)).fetchall()
        out: list[dict[str, Any]] = []
        if summary:
            out.append({"role": "user",
                        "content": f"[历史摘要]（此前对话已压缩；原始记录见事件流）\n{summary}"})
            out.append({"role": "assistant", "content": "已读摘要，继续。"})
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

    def _summarize_chat(self, lines: list[str]) -> str:
        """把「指挥 ↔ 人类」既往对话文本行交 LLM 压成摘要；失败/空返回空串。"""
        if not lines or self.llm is None:
            return ""
        prompt = ("请把以下「指挥 ↔ 人类」的既往对话压缩成摘要（Markdown，中文）：\n"
                  "1. 人类提出的目标与约束（原话逐字保留）\n"
                  "2. 已确认的方案与行动边界\n"
                  "3. 已组建的团队 / 已下达的执行（引用 id）\n"
                  "4. 待办与未决问题\n\n待压缩对话：\n" + "\n".join(lines))
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}],
                                 system=ORCH_COMPACT_SYSTEM)
        except Exception:  # noqa: BLE001 —— 压缩失败不阻断（宁可不压）
            log.exception("指挥对话历史压缩失败")
            return ""
        try:
            # source 独立（orchestrator-compact）：/context 取"最近占用"时排除摘要调用
            record_llm_usage(self.bb, self.project_id, resp.usage,
                             source="orchestrator-compact", session_id=None,
                             model=getattr(self.llm, "model", ""))
        except Exception:  # noqa: BLE001
            log.exception("指挥对话压缩用量记账失败")
        return "".join(b.get("text", "") for b in (resp.raw or {}).get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text").strip()

    def compact_chat(self, *, keep_recent: int = 8) -> dict[str, Any]:
        """手动持久压缩对话历史（/compact，2026-10-06）：把较早的 orch.chat 消息经
        LLM 压成一段摘要，落 orch.compact 事件（摘要 + 截止 event id）；后续
        `_chat_history` 从「摘要 + 截止之后的近期消息」起跑。历史过短 noop 不烧
        LLM；摘要失败/空不落事件（宁可不压不破坏现场）。返回 {status, ...}。"""
        rows = self.bb.conn.execute(
            "SELECT id, payload FROM events WHERE project_id=? AND kind='orch.chat'"
            " ORDER BY id DESC LIMIT 60", (self.project_id,)).fetchall()
        msgs: list[tuple[int, str, str]] = []
        for r in reversed(rows):
            try:
                payload = json.loads(r["payload"])
            except ValueError:
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            role = "user" if payload.get("role") == "human" else "assistant"
            msgs.append((int(r["id"]), role, text))
        before = len(msgs)
        if before <= keep_recent + 2:
            return {"status": "noop", "reason": "too_short", "before_msgs": before}
        cut = before - keep_recent
        old = msgs[:cut]
        if len(old) < 4:
            return {"status": "noop", "reason": "too_short", "before_msgs": before}
        lines = [f"[{'人类' if role == 'user' else '指挥'}] {text}"
                 for _id, role, text in old]
        summary = self._summarize_chat(lines)
        if not summary:
            return {"status": "error", "reason": "summarize_failed",
                    "before_msgs": before}
        cutoff_id = old[-1][0]
        after = len(msgs) - cut + 2  # 摘要头 2 条 + 近期消息
        self.bb.append_event(
            self.project_id, "orch.compact",
            {"summary": summary, "cutoff_id": cutoff_id, "before_msgs": before,
             "after_msgs": after, "summarized": len(old)},
            session_id=None, author="orchestrator")
        return {"status": "compacted", "before_msgs": before, "after_msgs": after,
                "summarized": len(old), "summary": summary[:200]}

    # ---------- 上下文用量（/context，2026-10-06） ----------

    def _latest_ctx_input_tokens(self) -> int | None:
        """最近一次编排 LLM 调用（巡检 tick / 对话轮）的输入占用（token）=
        input + cache_read + cache_creation。无记录返回 None。"""
        rows = self.bb.conn.execute(
            "SELECT payload FROM events WHERE project_id=? AND kind='llm.usage'"
            " AND author='orchestrator' ORDER BY id DESC LIMIT 20",
            (self.project_id,)).fetchall()
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except ValueError:
                continue
            if str(p.get("source") or "") not in ("orchestrator", "orchestrator-chat"):
                continue  # 排除摘要压缩调用（orchestrator-compact）
            return (int(p.get("input_tokens") or 0)
                    + int(p.get("cache_read_tokens") or 0)
                    + int(p.get("cache_creation_tokens") or 0))
        return None

    def _ctx_breakdown_chars(self) -> tuple[int, int, int]:
        """(system, tools, messages) 的等价字符数估算（供指挥 /context 分段）。"""
        sys_chars = 0
        try:
            sys_chars = (len(CHAT_SYSTEM_PROMPT) + len(self._overview_for_chat())
                         + len(self._role_catalog_prompt()) + len(self._goal_section())
                         + len(self._phase_section()) + len(self._mission_section())
                         + len(self._persona_section()))
        except Exception:  # noqa: BLE001 —— 估算失败退 0，不影响主流程
            log.exception("指挥上下文用量：系统提示估算失败")
        tools_chars = len(json.dumps(self._orch_tools(), ensure_ascii=False))
        msgs_chars = get_counter(getattr(self.llm, "model", None)).count_messages(
            self._chat_history())
        return sys_chars, tools_chars, msgs_chars

    def context_usage(self) -> dict[str, Any]:
        """指挥上下文用量快照（/context，2026-10-06）：窗口 = 供应商 model_context
        （缺省 256K）；占用取最近一次编排 LLM 调用的真实 input（无则估算）。
        消费方：直播间指挥页签 /context 浮层 + 对话轮末 85% 自动压缩。"""
        window = context_window_tokens(self.llm)
        threshold = int(window * ORCH_AUTOCOMPACT_RATIO)
        sys_chars, tools_chars, msgs_chars = self._ctx_breakdown_chars()
        est_chars = max(1, sys_chars + tools_chars + msgs_chars)
        measured = self._latest_ctx_input_tokens()
        if measured is not None and measured > 0:
            used, source = measured, "measured"
        else:
            used, source = max(1, est_chars // CHARS_PER_TOKEN), "estimated"
        scale = used / est_chars
        b_sys = round(sys_chars * scale)
        b_tools = round(tools_chars * scale)
        return {
            "window": window,
            "used": used,
            "pct": round(used / window, 4) if window else 0.0,
            "threshold": threshold,
            "source": source,
            "breakdown": {"system": b_sys, "tools": b_tools,
                          "messages": max(0, used - b_sys - b_tools)},
        }

    def _maybe_autocompact(self) -> None:
        """对话轮末自动压缩（/context 阈值，2026-10-06）：一轮结束后若指挥上下文
        占用 ≥85% 窗口 → 持久压缩（与手动 /compact 同语义）。整段吞异常，不阻断对话。"""
        try:
            usage = self.context_usage()
            if usage["used"] < usage["threshold"]:
                return
            res = self.compact_chat()
            if res.get("status") == "compacted":
                log.info("指挥轮末自动压缩 used=%s/%s", usage["used"], usage["window"])
        except Exception:  # noqa: BLE001
            log.exception("指挥轮末自动压缩失败（忽略）")

    @staticmethod
    def _assistant_text(raw: dict) -> str:
        """取一轮 assistant 回复里的纯文本块（与工具调用并存时只取 text）。"""
        blocks = (raw or {}).get("content") or []
        return "".join(
            str(b.get("text") or "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text").strip()

    def _emit_thinking(self, resp, step: int, source: str,
                       stream_id: str = "") -> None:
        """编排器思考终稿落库：resp.thinking 非空时落 llm.thinking（author=
        orchestrator），带 stream_id 时清剪该流的 llm.thinking.delta 过渡行
        （worker loop._emit_final_thinking 同型——审计只留终稿一条）。"""
        thinking = getattr(resp, "thinking", None)
        if not (thinking and str(thinking).strip()):
            return
        try:
            self.bb.append_event(
                self.project_id, "llm.thinking",
                {"thinking": str(thinking)[:4000], "step": step, "source": source,
                 **({"stream_id": stream_id} if stream_id else {})},
                session_id=None, author="orchestrator")
            if stream_id:
                try:
                    self.bb.prune_thinking_deltas(self.project_id, stream_id)
                except Exception:  # noqa: BLE001 —— 清剪失败只多留过渡行
                    log.exception("编排器 llm.thinking.delta 清剪失败")
        except Exception:  # noqa: BLE001 —— 观测性事件，失败不影响编排
            log.exception("编排器 llm.thinking 落库失败 step=%s", step)

    def _stream_kwargs(self) -> tuple[dict[str, Any], str]:
        """思考流式上下文（2026-09-28 选项B，Trae/CC 式逐字进度）：stream_capable
        的 llm 返回带 on_thinking 的 chat kwargs——回调攒增量，节流（≥120 新字符
        或距上次 ≥1.0s，worker loop 同款阈值）落 llm.thinking.delta（累计全文+
        seq+stream_id，author=orchestrator；编排 tick 单线程顺序执行，回调内直接
        落库无并发写风险，无需 worker 的旁路线程）。非流式 llm 返回 ({}, "")——
        终稿思考仍由 _emit_thinking 落库，进度退化为步进式。"""
        if not getattr(self.llm, "stream_capable", False):
            return {}, ""
        stream_id = uuid.uuid4().hex[:12]
        buf: list[str] = []
        pub = {"chars": 0, "seq": 0, "at": 0.0}

        def _on_thinking(delta: str) -> None:
            buf.append(delta)
            text = "".join(buf)
            now = time.monotonic()
            grown = len(text) - pub["chars"]
            if not text or (grown < 120 and (now - pub["at"] < 1.0 or grown <= 0)):
                return
            pub["chars"] = len(text)
            pub["seq"] += 1
            pub["at"] = now
            try:
                self.bb.append_event(
                    self.project_id, "llm.thinking.delta",
                    {"stream_id": stream_id, "thinking": text,
                     "seq": pub["seq"]},
                    session_id=None, author="orchestrator")
            except Exception:  # noqa: BLE001 —— 观测事件失败不影响编排
                log.exception("编排器 llm.thinking.delta 落库失败")

        return {"on_thinking": _on_thinking}, stream_id

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
            delegation_discipline=DELEGATION_DISCIPLINE,
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
            skwargs, stream_id = self._stream_kwargs()
            resp = self.llm.chat(messages, system=system, tools=self._orch_tools(),
                                 **skwargs)
            record_llm_usage(
                self.bb, self.project_id, resp.usage, source="orchestrator-chat",
                session_id=None, model=getattr(self.llm, "model", ""))
            self._emit_thinking(resp, _step, "chat", stream_id=stream_id)
            messages.append(assistant_message(resp))
            step_text = resp.text.strip()
            if step_text:
                reply = step_text
            if not resp.tool_calls:
                break  # 纯文本回复 = 回答完毕（对话轮与 tick 不同：文本即答案）
            tool_results = []
            for tc in resp.tool_calls:
                result = self._dispatch(tc.name, tc.arguments)
                tool_trace.append({
                    "name": tc.name,
                    "args": json.dumps(tc.arguments, ensure_ascii=False)[:200],
                    "result": str(result)[:200],
                })
                tool_results.append(self.llm.tool_result_message(tc, result)["content"][0])
            messages.append(tool_results_message(tool_results))
            if self._finished:
                break
        if reply:
            payload: dict[str, Any] = {
                "role": "orch", "text": reply[:2000], "tool_trace": tool_trace,
            }
            if wake:
                payload["proactive"] = True
                payload["triggers"] = [t["kind"] for t in wake]
            self.bb.append_event(
                self.project_id, "orch.chat", payload, author="orchestrator")
        # 轮末自动压缩（/context 阈值）：上下文占用 ≥85% 窗口 → 持久压缩
        self._maybe_autocompact()
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
        out: dict[str, Any] = {
            "summary": self._summary or "；".join(self._actions) or fallback,
            "published": list(self._published),
            "spawned": list(self._spawned),
            "teams": list(self._teams),
            "digest": self._digest,
            "proposals": list(self._proposals),  # 批 6：仅 L0 propose_only 非空
        }
        if self.config.analyze_only:
            # auto-attack 研判轮（2026-09-28）：渗透计划全文；非研判轮不带此键
            # （tick 结果键集合是既有契约，严格断言不收多余键）
            out["analysis"] = getattr(self, "_analysis_text", "")
        return out

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

    # ---------- 指挥组建队伍 / 亲自执行（2026-10-06 任务机制退役后） ----------

    def _tool_build_team(self, name: str, members: list[dict[str, Any]],
                         goal_text: str = "") -> str:
        """组建 Team（draft，不启动）：成员 role 从本轨专家池选（留空=动态成员）。
        人类在「指挥」页 preflight + 四项确认后才启动——本工具只登记 roster。"""
        name = str(name or "").strip()
        if not name:
            return "[拒绝] 团队名称不能为空"
        if not isinstance(members, list) or not members:
            return "[拒绝] 团队至少需要一名成员"
        roster: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(members):
            if not isinstance(raw, dict):
                return f"[拒绝] 成员 {index + 1} 必须是对象"
            key = str(raw.get("member_key") or raw.get("id") or "").strip()
            if not key:
                return f"[拒绝] 成员 {index + 1} 缺少 member_key"
            if key in seen:
                return f"[拒绝] 成员 key 重复: {key}"
            seen.add(key)
            role = str(raw.get("role") or "").strip()
            if role and self.track and not expert_exists(self.packs_root, role, self.track):
                return (f"[拒绝] 成员 {key} 的角色不在本轨专家池: {role!r}；"
                        "留空 role 可创建按职责定义的动态成员")
            if role and self.config.allowed_roles is not None \
                    and role not in self.config.allowed_roles:
                return f"[拒绝] 角色 {role!r} 不在白名单 {self.config.allowed_roles}"
            runtime = str(raw.get("runtime") or "").strip().lower()
            if runtime and runtime not in RUNTIME_LEVELS:
                return f"[拒绝] 成员 {key} 非法 runtime: {runtime}"
            threat = str(raw.get("threat_class") or "trusted").strip() or "trusted"
            if threat not in VALID_THREAT_CLASSES:
                return f"[拒绝] 成员 {key} 非法 threat_class: {threat}"
            roster.append({
                "member_key": key,
                "label": str(raw.get("label") or raw.get("name") or key).strip(),
                "responsibility": str(raw.get("responsibility") or raw.get("description") or "").strip(),
                "role": role,
                "runtime": runtime,
                "threat_class": threat,
                "max_steps": raw.get("max_steps"),
            })
        if self.config.propose_only:
            return self._propose("build_team", {
                "name": name, "goal_text": str(goal_text or "").strip(), "members": roster})
        try:
            team = TeamStore(self.bb, packs_root=str(self.packs_root)).create_team(
                self.project_id, name=name, goal_text=str(goal_text or "").strip(),
                members=roster, created_by="orchestrator")
        except (ValueError, LookupError) as e:
            return f"[拒绝] {e}"
        self._teams.append({"team_id": team["id"], "name": team["name"],
                            "member_count": len(roster)})
        return (f"team={team['id']} 已组建（{len(roster)} 名成员，draft 未启动）："
                "请在「指挥」页查看并配置，人类确认（preflight + 四项确认）后才会启动执行")

    def _tool_execute(self, objective: str) -> str:
        """指挥亲自执行：建会话 → run_team_execution（无 task，完整 Agent 工具面）。
        tick 内同步执行，步数上限 config.execute_max_steps（防一次跑满拖死编排轮）。"""
        objective = str(objective or "").strip()
        if not objective:
            return "[拒绝] objective 不能为空"
        if self.config.propose_only:
            return self._propose("execute", {"objective": objective})
        if self.session_factory is None:
            return "[错误] 未配置 session_factory，无法亲自执行"
        if self.gate is not None:
            reason = self.gate("execute")
            if reason:
                return f"[拒绝] {reason}"
        if len(self.live_sessions) >= self.config.max_sessions:
            return (f"[拒绝] 活跃会话已达上限 {self.config.max_sessions}，请先收尾再亲自执行")
        try:
            sess = self.session_factory("_generalist")
        except FileNotFoundError as e:
            return f"[拒绝] {e}"
        except Exception as e:  # noqa: BLE001
            return f"[错误] 建会话失败: {type(e).__name__}: {e}"
        sid = sess.session["id"]
        self.live_sessions[sid] = sess
        self._spawned.append({"session_id": sid, "role": "_generalist"})
        self.bb.append_event(
            self.project_id, "session.spawned",
            {"role": "_generalist", "session_id": sid, "origin": "orchestrator-execute"},
            session_id=sid, author="orchestrator")
        dispatcher = getattr(sess, "dispatcher", None)
        if dispatcher is not None and hasattr(dispatcher, "max_steps"):
            try:
                dispatcher.max_steps = min(int(dispatcher.max_steps), self.config.execute_max_steps)
            except (TypeError, ValueError):
                pass
        context = ExecutionContext(
            execution_id=new_id("exec"), session_id=sid, objective=objective,
            role="_generalist")
        try:
            result = sess.run_team_execution(context)
        except Exception as e:  # noqa: BLE001
            return f"[错误] 执行异常: {type(e).__name__}: {e}"
        text = str(result or "").strip()
        return (f"已亲自执行（session={sid}）：{text[:600]}" if text
                else f"已亲自执行（session={sid}）：本轮未产出结论")

    # ---------- 只读查询四工具（M2 orchestrator-efficiency A1，2026-09-22） ----------

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
                av = self._assets_view()
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

    def _approvals_view(self) -> dict:
        """态势审批段：待审批卡计数 + 摘要（cap 10）——LLM 收敛判据把「审批排队」
        当作在途工作（授权/阶段流转等人类决策尚未落定）。"""
        rows = self.bb.conn.execute(
            "SELECT id, action, risk FROM approvals WHERE project_id=? AND status='pending'"
            " ORDER BY created_at DESC LIMIT 10", (self.project_id,)).fetchall()
        out = []
        for r in rows:
            try:
                action = json.loads(r["action"] or "{}")
            except (TypeError, ValueError):
                action = {}
            out.append({"id": r["id"], "op": action.get("op") or "",
                        "objective": str(action.get("objective") or "")[:80],
                        "risk": r["risk"] or "medium"})
        return {"pending_total": len(out), "pending": out}

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
