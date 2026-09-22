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

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.agent.retention import omitted_note, retain
from core.agent.tools import AGENT_TOOLS, ToolDispatcher
from core.autonomy import record_llm_usage
from core.blackboard import Blackboard, TaskQueue
from core.llm.provider import LLMError
from core.runtime.gateway import ExecutionGateway
from core.skills import (
    SkillRegistry,
    SkillRouter,
    build_rules_preamble,
    load_kb_sources,
    load_task_types,
)
from core.skills.judge import judge_finding
from core.skills.kbindex import kb_module_hints, kb_route_hints
from core.skills.experts import expert_exists, load_expert
from core.skills.routeindex import read_kb_module, render_route_index_top

log = logging.getLogger(__name__)

# 空闲对话轮（2026-09-19）工具集：AGENT_TOOLS 去掉 finish——对话轮无任务可收尾，
# 纯文本回复即终止；其余工具照常（查黑板/知识库/资产回答人类提问）。
CHAT_TOOLS: list[dict[str, Any]] = [t for t in AGENT_TOOLS if t.get("name") != "finish"]

# 工具参数流截断的整轮重试上限（2026-09-20 事故修复）：GLM 长思考会话输出预算
# 耗尽/网关断流 → 工具参数 JSON 残缺。传输层流不可重放，只能在 agent 轮级重建
# 消息重发；上限 2 次，仍截断才放给 _fail_task_on_error。
_CHAT_TRUNC_RETRIES = 2

# ■ 收件箱订阅声明（2026-09-21）：kind → 消费场景。四处消费点（认领期 claim /
# 恢复期 resume / 步边界 step / 对话轮 chat）统一从 AgentSession._drain_inbox
# 取信，渲染顺序=表内声明序；新增 kind 必须先在此登记——未登记的私信滞留
# 收件箱红点可见，绝不被静默吞。
# - basis_stale（强制三选一）与 finding_update（发现增补）须任务上下文才能响应，
#   对话轮不消费（留收件箱等下一轮认领期/步边界）。
# - human_note 轮末语义（2026-09-19）：不在步边界打断思路，留到认领期/恢复期。
_INBOX_SUBSCRIPTIONS: dict[str, tuple[str, ...]] = {
    "basis_stale": ("claim", "resume", "step"),
    "finding_update": ("claim", "resume", "step"),
    "escalation_result": ("claim", "resume", "step", "chat"),
    "human_note": ("claim", "resume", "chat"),
    "agent_message": ("claim", "resume", "step", "chat"),
    "task_receipt": ("claim", "resume", "step", "chat"),
}


class _StepInterrupted(Exception):
    """■ 即点即停（2026-09-19）：LLM 调用等待中被人手中断——等待方即刻收到，
    不等响应返回。旁路线程随 HTTP 超时自行结束，结果丢弃。"""

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
   （任务 id + 关键观察）。知识库按「阶段/测试包」组织，每个测试包一册手册 +
   payloads/ 弹药目录。沉淀去向优先 **测试包手册**：新经验补进对应测试包 手册.md
   的「已验证路径」「坑」段；本任务整条 verified 攻击链/跑通 payload 值得复用时，
   提 kind=case 提案（成功案例.md 补段或 payloads/ 补弹药）——这是打穿路径的
   首选复用源；若新增了值得索引的测试点手册，再提一条 index 提案同步
   route_index.yaml（mode=edit 增补本域条目，条目 kb 路径带域前缀）。认为某技能
   粒度过粗时，可提 mode=suggest 的技能拆分建议提案
   （产建议文档，实际拆分由人执行）。英文快照原文不翻译、不覆盖；
   每个会话最多 3 条，禁止凑数。提案批准权在人类。
7. 产物落盘纪律：正式产物（POC/报告/有效载荷/验证有效的关键结果）一律
   bb_add_artifact（永久保存+进证据链）；临时中间文件用**相对路径**写当前工作
   目录（服务端已固定到本项目 scratch，可随时清理，系统 TEMP 已重定向到项目内）；
   向工作区外绝对路径写文件会被网关拒绝——不要重试同一写法。
8. 工具调用策略：无依赖的调用在同一轮**并行发出**（如同时查多个资产、多个 kb 模块）；
   专用工具优先于 run_cmd 拼命令（查黑板走 bb_query、查知识库走 kb_search/kb_open、
   查路由索引走 route_lookup）；大段正文按需取用（技能正文 skill_open），不要凭记忆复述文档。
9. 执行环境速查：cwd 已由服务端固定到本项目 scratch（host 与 wsl 均是）——命令一律
   **用相对路径，禁止手动 cd**（尤其不要拼 cd /mnt/...）。runtime 语义：host=宿主
   原生（Windows 上是 PowerShell，sed/awk 等 unix 语法在此不可用）；wsl=bash + unix
   工具链（sed/grep/objdump，进程启动与盘 IO 有开销，纯文本读取优先 host 原生）；
   docker/sandbox=不可信样本执行（铁律：样本绝不跑 host/wsl）。run_cmd 输出上限
   2000 字符（截断有标注，不要靠语义猜）；读工作区文件用 read_file（带行号、可分段）；
   超大工具结果自动落盘 spill/（回填含定位器，按提示分段取回）。
"""

# G3 结构化摘要压缩（2026-09-19，对齐 Claude Code /compact 与 HackSynth）：
# 旧历史不机械截断，让 LLM 按固定九要素骨架压成一段——用户原话逐字保留、
# 黑板对象引用 id 不内联内容（细节在黑板/事件流里，摘要只是索引）。
SUMMARY_SYSTEM = (
    "你是 Agent 会话压缩器：把执行历史压缩成一段摘要，供 Agent 无损续跑。"
    "只输出摘要正文（Markdown，中文），不要评论或客套。用户/人类引导的原话必须逐字保留，"
    "资产/发现/任务等黑板对象一律引用 id（如 as-xxxx、find-xxxx），不要内联全文。"
)

# fail 路径抢救收尾（2026-09-20，借鉴 Intentest arXiv:2609.07344 两段式降级：主阶段
# 失败后先经降级段抢救残余事实再回收）：任务异常终止前的一轮**无工具纯文本**提炼，
# 把已验证事实/失败方向落盘为 salvage 产物，接手者免重走。
SALVAGE_SYSTEM = (
    "你是故障抢救员：任务即将因异常终止，你没有任何工具、不能执行任何操作，"
    "只把执行历史里已获得的部分结论提炼成 Markdown 交接文本（中文，≤400 字）。"
    "只引用历史中真实出现过的黑板 id（如 find-xxxx、as-xxxx），绝不编造；"
    "历史太薄没有可抢救内容就直说，不要发挥。"
)


@dataclass
class AgentConfig:
    # E8：默认步数预算 200（角色 yaml 取 min 可更严；request_steps 可自助 +200）
    max_steps: int = 200
    # 对话轮步数上限（2026-09-20 会话窗对话化）：run_chat 取
    # min(dispatcher.max_steps, chat_max_steps)。24 够 Claude Code 式复合干活
    # （查黑板 2-3 + 跑命令 3-5 + bb_add_finding 登记结论 1-2 + 撰写回复），
    # 同时天然防对话轮无限膨胀；不做对话轮 request_steps 自助增补
    chat_max_steps: int = 24
    context_char_budget: int = 120_000
    # G3 结构化摘要压缩触发阈值（2026-09-19）：超过即把旧历史经 LLM 压成九要素
    # 摘要（近 8 条逐字保留）；硬上限仍由 context_char_budget 的机械 _trim 兜底
    context_summary_chars: int = 60_000
    stuck_after: int = 8          # 连续 N 步无进展 → 召唤策略顾问
    owner_tags: list[str] = field(default_factory=list)
    rule_profiles: dict | None = None     # F11：评级/owner 生效档案三态（None=缺省自动）
    # 角色增强（§6.6，由 yaml 注入；None = 不限制）
    max_noise: str | None = None          # 角色 default_noise：噪声预算上限（提示自陈，
                                          # 不再过滤认领——窗口无 role 限制，2026-09-18）
    allowed_tools: list[str] | None = None
    max_runtime: str | None = None        # host/wsl/docker/sandbox 最高等级
    lease_minutes: int = 30               # 认领/续租的租约 TTL
    lease_renew_seconds: int = 600        # 租约心跳间隔（TTL 的 1/3，留两次余量）


# 未声明 model_context 的模型假定上下文（token）。当前主力模型（GLM-5.3-Flash /
# deepseek-v4-flash / ark-code-latest）窗口 ≥256k；配小了会频繁触发摘要压缩
# （每次烧一步 LLM 调用还丢细节），宁可宽——真超窗由供应商报错兜底。
DEFAULT_MODEL_CONTEXT_TOKENS = 256_000


def apply_context_budget(config: AgentConfig, llm) -> None:
    """模型最大上下文（providers.json model_context，单位 token）→ 会话预算换算：
    context_char_budget ≈ tokens×2（中英混合启发：CJK≈1 token/字、EN≈1 token/4 字符），
    摘要压缩阈值取其一半。未声明（None/0）用 DEFAULT_MODEL_CONTEXT_TOKENS（256K）；
    切换模型后重跑即随新模型生效（__init__ 与切换端点共用）。
    只覆写仍处 dataclass 默认值的字段——显式传入的自定义预算（测试/未来 per-session
    配置）优先于模型推导。"""
    tokens = getattr(llm, "context_tokens", None) or DEFAULT_MODEL_CONTEXT_TOKENS
    d = {f.name: f.default for f in dc_fields(AgentConfig)}
    if config.context_char_budget == d["context_char_budget"]:
        config.context_char_budget = int(tokens) * 2
    if config.context_summary_chars == d["context_summary_chars"]:
        config.context_summary_chars = int(tokens)


def persisted_snapshot_path(artifacts_dir, sid: str) -> "Path | None":
    """快照落盘路径的模块级形态（API 层判 resumable 用）：
    <workspace>/<pid>/snapshots/<sid>.json；artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"{sid}.json"


def task_transcript_path(artifacts_dir, task_id: str) -> "Path | None":
    """C10 任务现场文件路径（模块级形态，API 层删任务清理用）：
    <workspace>/<pid>/snapshots/task-<tid>.json——与 E8 会话快照同目录。
    文件名由 task_id 确定性推导，agent 层读写无需查 DB 指针；
    artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"task-{task_id}.json"


def session_chat_path(artifacts_dir, sid: str) -> "Path | None":
    """会话级对话历史文件路径（2026-09-20 会话窗对话化）：
    <workspace>/<pid>/snapshots/chat-<sid>.json——与 task-<tid>.json 同目录
    （结构 {"session_id":..., "messages":[...]}，只存对话轮问答对）。
    无绑定任务的纯 chat 窗历史持久化用；artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"chat-{sid}.json"


def task_resume_path(artifacts_dir, task_id: str) -> "Path | None":
    """C6 任务键断点快照路径（模块级形态，API 层判 resume_mode/清理用）：
    <workspace>/<pid>/snapshots/task-<tid>.resume.json——**跨会话/跨角色复活凭证**。
    与 C10 task-<tid>.json（transcript 接手素材）同目录不同文件；
    不存 system（跨角色安全，system 由认领会话按角色重建）；
    artifacts_dir 为 None 返回 None。"""
    if artifacts_dir is None:
        return None
    return Path(artifacts_dir).parent / "snapshots" / f"task-{task_id}.resume.json"


def clear_task_resume(artifacts_dir, task_id: str) -> None:
    """C6 任务键快照清理（模块级，API 层复用）：done 消费/objective 不匹配降级/
    任务删除/reopen(drop_scene)/重启孤儿对账时调用；缺失静默、OSError 只 log。"""
    path = task_resume_path(artifacts_dir, task_id)
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("任务键快照清理失败（任务 %s）", task_id)


def _is_tool_result(m: dict) -> bool:
    """消息是否带 tool_result 块（C10 截断与 v0.64 快照尾部 sanitize 共用判定）。"""
    c = m.get("content")
    return (isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c))


def _tool_use_blocks(m: dict) -> list[dict]:
    c = m.get("content")
    if m.get("role") != "assistant" or not isinstance(c, list):
        return []
    return [b for b in c if isinstance(b, dict) and b.get("type") == "tool_use"]


def _tool_result_ids(m: dict) -> list[str]:
    c = m.get("content")
    if not isinstance(c, list):
        return []
    return [b.get("tool_use_id") for b in c
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and b.get("tool_use_id")]


def sanitize_snapshot_tail(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """v0.64 暂停请求即时落盘的尾部 sanitize：请求时刻 worker 可能停在半步
    （assistant(tool_use) 后 tool_result 只 append 了一部分），悬空 tool_use 会
    让续跑后的 LLM 调用被 API 拒。按 tool_use/tool_result 配对计数：尾部结果
    少于配对数（或 assistant 还没落任何 result）→ 截到最后一个完整边界；
    完整对原样保留。只读入参（调用方先浅拷贝），不修改 worker 现场。"""
    msgs = list(messages)
    i = len(msgs)
    while i > 0 and _is_tool_result(msgs[i - 1]):
        i -= 1                      # 尾部连续纯 tool_result 消息段的起点
    if i == len(msgs):
        # 尾部无 result：assistant(tool_use) 尚未落任何结果 → 悬空，截掉
        if msgs and _tool_use_blocks(msgs[-1]):
            msgs.pop()
        return msgs
    if i == 0 or not _tool_use_blocks(msgs[i - 1]):
        return msgs                 # result 段前无 assistant(tool_use)，结构原样保留
    use_ids = [b.get("id") for b in _tool_use_blocks(msgs[i - 1])]
    result_ids = [tid for m in msgs[i:] for tid in _tool_result_ids(m)]
    if len(result_ids) < len(use_ids):
        return msgs[:i - 1]         # 半步：丢弃悬空 assistant + 已落的部分 result
    return msgs


def _fmt_size(n: Any) -> str:
    """附件大小人读格式（_attachment_lines 用）。"""
    if not isinstance(n, int) or n < 0:
        return ""
    if n >= 1048576:
        return f"{n / 1048576:.1f}MB"
    if n >= 1024:
        return f"{n // 1024}KB"
    return f"{n}B"


def _attachment_lines(attachments: Any) -> list[str]:
    """附件清单行（2026-09-19 附件随发）：任务 context.attachments 与
    human_note payload.attachments 共用渲染——工作区相对路径 + 原始文件名，
    Agent 可直接 run_cmd 读取（读路径无 pathguard 拦截）。"""
    lines: list[str] = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or "").strip()
        if not path:
            continue
        name = str(a.get("name") or "").strip() or path
        size_s = _fmt_size(a.get("size"))
        lines.append(f"- 📎 {path}（{name}{('，' + size_s) if size_s else ''}）")
    return lines


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
        # 有界等线程退出：Event.wait 命中置位即返，正常毫秒级收摊；负载下若不 join，
        # 观测方（测试/收尾断言）在 is_alive() 上会滞后一拍误判未停
        self.join(timeout=2.0)


class AgentSession:
    def __init__(
        self,
        *,
        project_id: str,
        bb: Blackboard,
        gateway: ExecutionGateway,
        llm: Any,                       # LLMProvider（executor 模型）
        planner_llm: Any | None = None,  # 策略顾问（planner 模型，缺省禁用）
        gate_llm: Any | None = None,    # C6 漏洞核对 hook 的 LLM（缺省=不挂 hook）
        packs_root: str | Path = "packs",
        track: str = "ctf",
        capabilities: list[str] | None = None,
        role: str = "_generalist",
        allowed_roles: list[str] | None = None,  # 发布链 publish_task 值域（expert-pool M2，§4.6）
        session_name: str | None = None,
        capability_prompt: str = "",
        config: AgentConfig | None = None,
        decompiler=None,
        artifacts_dir: str | Path | None = None,
        existing_session: dict | None = None,
        enable_sediment: bool = False,  # v0.65：done 自动提案开关（app 工厂按配置开；直构默认关）
        campaign=None,                  # ⑥ 战役记忆全局库（CampaignMemory；None=不沉淀）
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
        apply_context_budget(self.config, self.llm)  # 模型声明上下文 → 预算换算（无声明=默认）
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None

        role_data = load_expert(self.packs_root, role, track)
        # 专家缺失时 load_expert 已回退 _generalist；role-rules/会话名按实际专家走
        self.role_name = role if (
            self.packs_root / "experts" / f"{role}.yaml").is_file() \
            else "_generalist"
        self.role = role_data
        self._apply_role_limits(role_data)
        # v14 认领即换装（DESIGN §6.4 定稿块）：底色角色=开窗选定，永不改；
        # 认领带 role 的任务时按任务角色换装（_apply_task_persona），跑完恢复。
        self.base_role_name = self.role_name
        self.base_role_data = role_data
        self._persona_saved: dict | None = None  # 换装现场（None=当前即底色）
        # v0.71 任务即窗口：中途改角色热换装——prompt 段下个步进重建的脏标记
        # 与最近一次 system 的 skill_context 快照（重建时复用，避免重跑路由）
        self._persona_dirty = False
        self._last_skill_context = ""

        if existing_session is not None:
            # rehydrate（服务重启/孤儿窗）：附着既有 sessions 行，不新建、不重置状态。
            # 角色 yaml 仍按当盘文件重载（软边界可能已变）；E8 起落盘的暂停快照
            # 会经 sessions.meta 指针载回（见 _load_persisted_snapshot）。
            self.session = dict(existing_session)
        else:
            self.session = bb.register_session(
                project_id, session_name or f"{track}/{self.role_name}", role=self.role_name)
        self.tq = TaskQueue(bb)
        # 会话控制面（DESIGN.md §3）：API 线程只置 Event，worker 线程消费——
        # 事件对象须先于 dispatcher 创建（■ 即点即停 2026-09-19：abort_event
        # 传给 dispatcher，run_cmd 执行中置位即杀进程树）
        self._pause_req = threading.Event()
        self._abort_req = threading.Event()
        self.dispatcher = ToolDispatcher(
            bb, gateway, self.tq,
            project_id=project_id, session_id=self.session["id"], author=self.session["id"],
            decompiler=decompiler, artifacts_dir=artifacts_dir,
            packs_root=packs_root, track=track, capabilities=self.capabilities,
            allowed_tools=self.config.allowed_tools, max_runtime=self.config.max_runtime,
            allowed_task_types=load_task_types(self.packs_root, track).keys(),
            max_steps=self.config.max_steps,
            abort_event=self._abort_req,
            role_skills=self.role.get("skills"),
            allowed_roles=allowed_roles,
        )
        self.registry: SkillRegistry | None = None
        self.capability_prompt = capability_prompt
        # C6 漏洞核对 hook（仅渗透/红队轨接线）：AI 触发 category=vuln 时，
        # 用本会话 LLM 对照红线/评级规则自我核对——不合格降级有效发现，不进漏洞视图。
        if track in ("pentest", "redteam") and gate_llm is not None:
            self.dispatcher.vuln_gate = self._build_vuln_gate(gate_llm)
        # v0.65 done 自动提案：任务完成时复盘验证过的有效手法 → 自动产提案草稿
        # （planner_llm 缺席或 enable_sediment=False 不接线；人类审批后进 kb 并更新索引）
        if planner_llm is not None and enable_sediment:
            self.dispatcher.sediment_hook = self._sediment_proposal
        # ⑥ 战役记忆（简版）：任务 done 后沉淀打法进全局库（跨项目召回；
        # 纯确定性拼接不需要 LLM，campaign 注入即接线）
        self.campaign = campaign
        if campaign is not None:
            self.dispatcher.campaign_hook = self._campaign_memory

        # 会话控制面（DESIGN.md §3）：API 线程只置 Event，worker 线程在步边界消费
        # （_pause_req/_abort_req 已上移到 dispatcher 构造前——■ 即点即停要传 abort_event）
        self._resume_state: dict[str, Any] | None = None
        # 快照 = {system, messages, objective, task_id, next_step}——暂停时保存，恢复续跑
        # v0.64：_loop 在册的当前任务现场（system/messages 引用/objective）——暂停请求
        # 时刻 API 线程据此即时落盘，关后端不再丢「暂停未到步边界」窗口期的现场。
        self._live_state: dict[str, Any] | None = None
        # fail 抢救收尾（2026-09-20）：_loop 入口在册的任务现场（objective/messages
        # 引用），异常穿出后供 _salvage_attempt 提炼部分结论；正常退出即清
        self._salvage_ctx: dict[str, Any] | None = None
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
            if self._resume_state is not None:
                self.paused = True

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

    # ---------- 认领即换装（v14 任务绑定角色，DESIGN §6.4 定稿块） ----------

    def _apply_task_persona(self, task: dict | None = None,
                            task_id: str | None = None) -> None:
        """认领带 role 的任务时按任务角色换装（动态 persona）。

        换装四面：角色 prompt 段/技能路由白名单（self.role/self.role_name，build_system_prompt
        按调用时点取值）+ config.{max_noise, allowed_tools, max_runtime}（经 _apply_role_limits
        收敛）+ dispatcher 对应边界。**不动 max_steps**（会话级资源，E8）。窗口不设
        role 限制（2026-09-18）：认领无角色过滤，本方法即任务 role 的唯一生效点。
        幂等：无 role / 与当前执行角色一致 → no-op；
        角色文件缺失（发布后被删）→ 防御性按当前角色跑。"""
        want = str((task or {}).get("role") or "").strip()
        if not want or want == self.role_name:
            return
        if not expert_exists(self.packs_root, want, self.track):
            return
        saved = {
            "role_name": self.role_name, "role": self.role,
            "max_noise": self.config.max_noise,
            "allowed_tools": self.config.allowed_tools,
            "max_runtime": self.config.max_runtime,
            "dispatcher_allowed_tools": self.dispatcher.allowed_tools,
            "dispatcher_max_runtime": self.dispatcher.max_runtime,
        }
        rd = load_expert(self.packs_root, want, self.track)
        self.role_name, self.role = want, rd
        self.config.max_noise = None
        self.config.allowed_tools = None
        self.config.max_runtime = None
        self._apply_role_limits(rd)
        self.dispatcher.allowed_tools = self.config.allowed_tools
        self.dispatcher.max_runtime = self.config.max_runtime
        self.dispatcher.current_persona_role = want
        self._persona_saved = saved
        self.bb.append_event(
            self.project_id, "session.persona_switched",
            {"session_id": self.session["id"], "task_id": task_id or (task or {}).get("id"),
             "from": saved["role_name"], "to": want},
            session_id=self.session["id"], author=self.session["id"])

    def _restore_base_persona(self) -> None:
        """恢复底色 persona（幂等复位口，v14 换装状态机统一出口）。"""
        self._persona_dirty = False  # v0.71：换装层已整体复位，脏标记作废（防串任务）
        if self._persona_saved is None:
            return
        s = self._persona_saved
        self._persona_saved = None
        self.role_name = s["role_name"]
        self.role = s["role"]
        self.config.max_noise = s["max_noise"]
        self.config.allowed_tools = list(s["allowed_tools"]) if s["allowed_tools"] else None
        self.config.max_runtime = s["max_runtime"]
        self.dispatcher.allowed_tools = (
            list(s["dispatcher_allowed_tools"]) if s["dispatcher_allowed_tools"] else None)
        self.dispatcher.max_runtime = s["dispatcher_max_runtime"]
        self.dispatcher.current_persona_role = None

    def apply_role_change(self, task: dict) -> None:
        """v0.71 任务即窗口：任务绑定角色中途修改（claimed 态仅放行 role），立即
        热换装——config/dispatcher 执行边界即时生效，prompt 段在下个步边界由
        _loop_body 重建（_persona_dirty）。

        **不得直接重跑 _apply_task_persona**：已在换装中（_persona_saved 非空）
        时它会重存底色，恢复时会回到中间角色（底色丢失）。正确姿势：已换装→
        只更新换装层；未换装（任务原无 role 被中途改出 role）→ 走标准换装路径。
        角色文件缺失/同名 → no-op（与认领换装同口径）。"""
        task_id = (task or {}).get("id") or self.dispatcher.current_task_id
        want = str((task or {}).get("role") or "").strip()
        if not want or want == self.role_name:
            return
        if not expert_exists(self.packs_root, want, self.track):
            return
        prev = self.role_name
        if self._persona_saved is None:
            self._apply_task_persona(task, task_id)
        else:
            rd = load_expert(self.packs_root, want, self.track)
            self.role_name, self.role = want, rd
            self.config.max_noise = None
            self.config.allowed_tools = None
            self.config.max_runtime = None
            self._apply_role_limits(rd)
            self.dispatcher.allowed_tools = self.config.allowed_tools
            self.dispatcher.max_runtime = self.config.max_runtime
            self.dispatcher.current_persona_role = want
        self._persona_dirty = True
        self.bb.append_event(
            self.project_id, "session.persona_switched",
            {"session_id": self.session["id"], "task_id": task_id,
             "from": prev, "to": want, "reason": "role-change"},
            session_id=self.session["id"], author=self.session["id"])

    # ---------- 系统提示 ----------

    def build_system_prompt(self, objective: str, skill_context: str = "") -> str:
        parts = [
            build_rules_preamble(
                self.packs_root, track=self.track, capabilities=self.capabilities,
                owner_tags=self.config.owner_tags, role=self.role_name,
                rule_profiles=self.config.rule_profiles),
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
                          task_id: str | None = None,
                          task_type: str | None = None,
                          scope: str | None = None) -> str:
        """入口技能路由：角色白名单窄化 → 评分最高技能正文 + 知识库源登记。

        候选集 = 项目启用能力包技能 ∪ 场景轨技能（§4.5），角色 skills 白名单再窄化。
        每个任务落一条 skill.routed 审计事件（未命中 name=null，C5 可观测）。

        2026-09-18 路由增强：
        - task_type 加分项：认领队列任务时传 task.task_type，frontmatter
          task_types 命中 +10（router.route）——确定性信号 > 文本巧合。
        - scope 拼路由 query（G2）：objective 进 system prompt 不变，只有
          路由匹配串拼 scope（个别 scope 带中文描述时救匹配）。
        - 未命中不再返回空串：仍注入 kb 源清单 + kb_search 指引（此前 Agent
          完全不知道 kb 存在，kb 永远不会被打开）。
        - 两分支都注入「📚 相关知识库模块」提示行（kbindex 中文标题索引，
          只给路径不注入正文，Agent 自己 kb_open）。
        E8 会话键快照恢复路径不重路由（回放暂停时点 system，快照自洽）。"""
        if self.registry is None:
            self.load_skills()
        assert self.registry is not None
        role_skills = self.role.get("skills")
        pack_set = set(self.capabilities) | {self.track}
        route_query = f"{query}\n{scope}".strip() if scope else (query or "")
        router = SkillRouter(self.registry)
        hits = router.route(query=route_query, features=features,
                            file_features=file_features,
                            role_skills=role_skills, packs=pack_set, top_k=1,
                            task_type=task_type)
        # kb 提示行两分支共用（kbindex：中文标题+段落级索引，mtime 快检缓存）
        try:
            hint_pairs = kb_module_hints(self.packs_root, self.capabilities,
                                         route_query)
        except Exception:  # 索引失败不影响技能路由主链
            hint_pairs = []
        hint_block = ""
        if hint_pairs:
            lines = []
            for e, sec in hint_pairs:
                row = f"- {e.module} —— {e.title}"
                if e.summary:
                    row += f"｜{e.summary}"
                if sec:
                    row += f"（相关段落：{sec}）"
                lines.append(row)
            hint_block = (
                "\n\n## 📚 相关知识库模块（可能相关；用 kb_open 按路径打开，"
                "段落命中可直接跳读该节，不相关则忽略）\n" + "\n".join(lines))
        # 🧭 kb 任务导航（2026-09-19 借鉴 dsh refs/README 路由表）：route.json
        # 静态路由，确定性键匹配（task_type+query 子串），先于 2-gram hints 注入
        try:
            route_hits = kb_route_hints(self.packs_root, self.capabilities,
                                        task_type, route_query)
        except Exception:  # 路由失败不影响技能路由主链
            route_hits = []
        route_block = ""
        if route_hits:
            rlines = "\n".join(f"- 「{k}」→ {', '.join(ps)}" for k, ps in route_hits)
            route_block = (
                "\n\n## 🧭 知识库任务导航（按任务类型静态路由；kb_open 打开）\n"
                f"{rlines}")
        # 🧭 测试点路由索引 Top-K 注入（G1 2026-09-19：全表 222 行 → 最相关 3-5 行，
        # 其余经 route_lookup 工具查询；条目 tags 与角色白名单裁剪口径不变）
        try:
            index_block, index_count, index_total, route_points = render_route_index_top(
                self.packs_root, self.capabilities, role_skills, query=route_query)
        except Exception:  # 索引失败不影响技能路由主链
            index_block, index_count, index_total, route_points = "", 0, 0, []
        sources = [{"id": s.id, "domain": s.root.name, "root": str(s.root)}
                   for s in load_kb_sources(self.packs_root, self.capabilities)]
        source_json = json.dumps(sources, ensure_ascii=False)
        kb_hits = [e.module for e, _sec in hint_pairs]
        kb_hits += [p for _, ps in route_hits for p in ps]
        if not hits:
            self.bb.append_event(
                self.project_id, "skill.routed",
                {"name": None, "score": 0, "matched": [], "task_id": task_id,
                 "query": route_query[:200], "task_type": task_type,
                 "kb_hits": kb_hits, "route_index": index_count,
                 "route_total": index_total, "route_points": route_points},
                session_id=self.session["id"], author=self.session["id"])
            return (
                "未命中技能指引（按通用方法执行）。需要方法细节时可用 kb_search "
                "工具按关键词全文检索知识库。\n\n"
                f"## 可用知识库源（kb_open 用全局 module=<域>/<快照>/<路径> 打开，禁止通读）\n{source_json}"
                f"{route_block}{hint_block}{index_block}"
            )
        top = hits[0]
        sk = top.skill
        self.bb.append_event(
            self.project_id, "skill.routed",
            {"name": sk.name, "kind": sk.kind, "pack": sk.pack, "score": top.score,
             "matched": top.matched, "breakdown": top.breakdown,
             "task_id": task_id, "query": route_query[:200],
             "task_type": task_type, "kb_hits": kb_hits,
             "route_index": index_count, "route_total": index_total,
             "route_points": route_points},
            session_id=self.session["id"], author=self.session["id"])
        return (
            f"{self._skill_digest(sk)}\n\n"
            f"## 可用知识库源（kb_open 用全局 module=<域>/<快照>/<路径> 打开，禁止通读）\n{source_json}"
            f"{route_block}{hint_block}{index_block}"
        )

    def _skill_digest(self, sk: Any) -> str:
        """命中技能的渐进披露摘要（G1）：正文不整段进 system——给目录 + skill_open
        指针；无标题结构的短正文降级为首段截断。"""
        try:
            body = sk.body()
        except OSError:
            body = ""
        if not body.strip():
            return f"当前命中技能: {sk.name}（{sk.description}；无正文）"
        headings = [l.strip().lstrip("#").strip()
                    for l in body.splitlines()
                    if l.strip().startswith("#") and l.strip().lstrip("#").strip()]
        if headings:
            toc = "\n".join(f"- {h}" for h in headings[:40])
            return (f"当前命中技能: {sk.name}（{sk.description}；正文 {len(body)} 字，"
                    f"目录如下——动手前用 skill_open(\"{sk.name}\") 打开全量）\n{toc}")
        cut = body[:600]
        more = "…（正文截断，用 skill_open(\"%s\") 打开全量）" % sk.name \
            if len(body) > 600 else ""
        return f"当前命中技能: {sk.name}（{sk.description}）\n\n{cut}{more}"

    # ---------- C6 漏洞核对 hook（AI 登记漏洞前自我对照红线/评级规则） ----------

    def _build_vuln_gate(self, gate_llm: Any):
        """漏洞核对 hook 工厂：Agent 触发 category=vuln 登记时，先让 LLM 对照
        红线/评级规则自我核对——不合格降级 intel（不进漏洞视图）。
        LLM 失败/不可用 → 降级跳过（照常登记、回执标注），与 F11 降级哲学一致。
        LLM 调用面复用 core.skills.judge.judge_finding（与存量清洗脚本同口径）。"""
        def gate(draft: dict) -> tuple[bool, str]:
            sys_p = (
                "你是漏洞合规审核员。候选发现要登记为「漏洞」，请对照以下红线与"
                "评级规则核对其是否够格：有可验证的安全问题、方向未被禁止、"
                "证据链成立。证据不足/方向被禁/属于信息提示或合规提示而非漏洞 → "
                "compliant=false。只输出 JSON："
                '{"compliant": true|false, "reason": "一句话依据"}')
            v = judge_finding(gate_llm, rules_text=build_rules_preamble(
                self.packs_root, track=self.track, capabilities=self.capabilities,
                owner_tags=self.config.owner_tags,
                rule_profiles=self.config.rule_profiles),
                draft=draft, sys_prompt=sys_p)
            if v is None:
                return True, "核对跳过：审核响应无 JSON"
            return bool(v.get("compliant")), str(v.get("reason") or "未通过漏洞标准核对")
        return gate

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
        old_plan_notice: str | None = None  # C1：重认领旧任务的计划/发现注入
        dead_end_notice: str | None = None  # C3：跨轨路标三级注入
        resumed: dict[str, Any] | None = None  # C6：任务键断点快照（认领即复活）
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
            # C6 认领即复活：任务键断点快照命中（objective 未被改）→ 跳过
            # handover/old_plan/dead_end/objective 注入链（快照历史里已有）。
            resumed = self._load_task_resume(task_id, task)
            if resumed is None:  # 无快照/快照被清（objective 被改降级）→ 常规注入链
                old_plan_notice = self._old_plan_notice(task)
                dead_end_notice = self._dead_end_notice(task)
            # v14 认领即换装：任务带 role 且 ≠ 当前执行角色 → 先换装再建 system prompt
            #（build_system_prompt/_role_block/skill_context_for 按调用时点取值，全链生效）
            self._apply_task_persona(task, task_id)
        skill_ctx = self.skill_context_for(
            objective, features, file_features, task_id=task_id,
            # 认领路径增强：task_type 加分 + scope 拼路由 query（task 行已取过）
            task_type=(task or {}).get("task_type") if task_id else None,
            scope=(task or {}).get("scope") if task_id else None)
        self._last_skill_context = skill_ctx  # v0.71：热换装重建 system 时复用
        system = self.build_system_prompt(objective, skill_ctx)
        # C6：复活时 messages 整体取快照（不与 transcript 拼接，防 tool_use/result
        # 错位）；否则 C10 transcript 接手（末 60 条）。E8 resume 路径不经 run_task，互斥。
        transcript_msgs: list[dict[str, Any]] = []
        if task_id and resumed is None:
            transcript_msgs = self._load_task_transcript(task_id)
        messages: list[dict[str, Any]] = list(resumed["messages"]) if resumed \
            else list(transcript_msgs)
        start_step = int(resumed["next_step"]) if resumed else 1
        if resumed is not None:
            self.dispatcher.max_steps = int(resumed["max_steps"])  # 预算断点还原
            clear_task_resume(self.artifacts_dir, task_id or "")  # 消费即删
        # 认领期消费收件箱（2026-09-19 轮末语义：human_note 不再于步边界中途
        # 注入，延迟到此处/恢复期——认领即拿到上一轮期间投递的人类引导）。
        # 口径统一走订阅声明表 _INBOX_SUBSCRIPTIONS（2026-09-21）。basis_stale
        # （强制三选一）合并任务挂标（open 任务只挂标无私信，按 stale_refs 现场
        # 补水）与收件箱私信两条来源；_stale_alerted 去重只对任务挂标部分生效
        # ——纯私信形态（任务行 stale_refs 已清）不再被 stale_refs 门控吞掉。
        if task_id:
            notices, _drained = self._drain_inbox("claim", stale_refs=stale_refs)
            for _kind, notice in notices:
                messages.append({"role": "user", "content": notice})
            if stale_refs and any(k == "basis_stale" for k, _ in notices):
                self._stale_alerted.add(task_id)
        if resumed is not None:
            # ⚡ 续跑标记：历史工具结果属旧现场不可重放，需重新取证
            messages.append({"role": "user", "content":
                f"⚡ 已从中断现场续跑（第 {resumed['next_step']} 步起；"
                "历史工具结果属旧现场不可重放，需重新取证）。"})
        else:
            handover = (self._handover_notice(task, bool(transcript_msgs))
                        if task_id else None)  # C10：第 N 次尝试接手提示
            if handover:
                messages.append({"role": "user", "content": handover})
            if old_plan_notice:
                messages.append({"role": "user", "content": old_plan_notice})
            if dead_end_notice:
                messages.append({"role": "user", "content": dead_end_notice})
            att_lines = _attachment_lines(
                ((task or {}).get("context") or {}).get("attachments")) \
                if task_id else []
            if att_lines:  # 附件随发（2026-09-19）：发布时经 API 层校验的 artifact 引用
                messages.append({"role": "user", "content":
                    "📎 任务附件（可用 run_cmd 读取，只读勿改写，工作区相对路径）：\n"
                    + "\n".join(att_lines)})
            messages.append({"role": "user", "content": objective})
        if task_id:
            self._checkpoint_task_transcript(messages, objective)  # 认领即有现场
        try:
            summary = self._loop(system, messages, objective, start_step=start_step)
        except Exception as e:
            # 兜底：异常穿出主循环（典型=LLM 传输层重试耗尽）必须收尾任务并停心跳，
            # 否则任务悬在 claimed + 孤儿心跳续租占坑（本进程内永久「执行中」）。
            self._fail_task_on_error(e)
            raise
        if summary is None:  # 暂停（心跳保留，继续占任务）/中断（_abort_current_task 已停心跳）
            return ""
        self._finalize()
        return summary

    def run_next_task(self, only_task: str | None = None,
                      assigned_only: bool = False) -> str | None:
        """Worker 循环入口：认领下一个匹配角色 task_types 的任务并执行。
        暂停/中断请求 → 不领新任务并落状态；有快照 → 优先续跑被暂停的任务。
        C10：快照续跑（E8）不经 run_task，与任务现场（transcript）接手路径天然互斥。
        v0.71 任务即窗口：only_task 非空 = 绑定窗只认领自己绑定的任务（一窗一
        任务核心闸，SQL 侧过滤无竞态）；跑完即空，不进公共池续单。
        v0.72 全局一窗一任务：assigned_only=True（无绑定手动窗挡位）——公共池
        认领机制退役，只认领显式指派给本窗的任务（实际恒空）。"""
        self._restore_base_persona()  # v14 兜底闸：任何遗漏恢复路径不得带 persona 认领新任务
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
            # C6 生命周期：E8 会话键恢复消费时同步删任务键快照（防「恢复后又 fail」
            # 时 rewind 到旧暂停点——现场以最新一次暂停为准）
            clear_task_resume(self.artifacts_dir, st.get("task_id") or "")
            task = self.tq.get_task(st["task_id"])
            if (task and task["claimed_by"] == self.session["id"]
                    and task["status"] == "claimed"):
                self.dispatcher.current_task_id = st["task_id"]
                # v14：重启/恢复后先按任务行重换装（快照 system 生成于暂停时点、
                # 已含任务角色 prompt 段；此处补齐 config/dispatcher 执行边界一致）。
                # 同进程暂停恢复时 persona 未摘 → 幂等 no-op。
                self._apply_task_persona(task, st["task_id"])
                if self._heartbeat is None:  # 兜底：暂停期心跳本应保留，缺失则补起
                    self._start_heartbeat(st["task_id"])
                # E8：暂停期积压的私信（含 human_note 人类引导 / agent_message 私信 /
                # task_receipt 回执，2026-09-20 对话化）随快照恢复一并注入；口径统一
                # 走订阅声明表（2026-09-21）——escalation_result 原漏渲染（全量
                # drain 吞掉不显示），本起补齐。
                notices, _dr = self._drain_inbox("resume")
                for _kind, notice in notices:
                    st["messages"].append({"role": "user", "content": notice})
                try:
                    summary = self._loop(st["system"], st["messages"], st["objective"],
                                         start_step=st["next_step"])
                except Exception as e:
                    self._fail_task_on_error(e)
                    raise
                if summary is None:
                    return None          # 恢复后立刻又被暂停/中断
                self._finalize()
                return summary
            # 快照失效（租约被回收/他人持有）：丢弃快照，落到正常认领
        self.last_claim_idle = False  # 进入认领：暂停/中断早退路径不得残留旧 True
        task_id = self.tq.claim_next(
            self.project_id, self.session["id"],
            lease_minutes=self.config.lease_minutes,
            session_role=self.base_role_name,  # 偏好排序：底色匹配任务优先，无过滤
            only_task=only_task,  # v0.71：绑定窗锁死只认领自己任务
            assigned_only=assigned_only)  # v0.72：手动窗不进公共池（认领机制退役）
        if task_id is None:
            self.last_claim_idle = True  # 批 5：仅这种退出才允许触发 L2 续 tick
            return None
        task = self.tq.get_task(task_id)
        try:
            return self.run_task(task["objective"], task_id=task_id)
        except Exception as e:
            # 覆盖 run_task 内认领后、_loop 前的异常窗口（_fail_task_on_error 幂等，
            # _loop 内已兜过则此处只重复停心跳）
            self._fail_task_on_error(e)
            raise

    def run_chat(self, task_id: str | None = None) -> str | None:
        """空闲对话轮（2026-09-19，E8 引导通道补完）：队列无任务可认领且收件箱有
        未读 human_note 时，直接对话——drain 引导 → LLM 循环（可带工具查黑板/
        知识库/资产）→ 纯文本回复落 `agent.chat` 事件并返回文本。

        v0.71 延续模式（2026-09-20）：task_id 非空（绑定窗终态续聊）时先从 C10
        任务现场文件（task-<tid>.json，每步落盘）载入末 60 条既往对话作为上下文，
        回合结束把本次问答**回写现场文件**——续聊跨轮、跨重启不丢上下文。

        会话窗对话化（2026-09-20 一期）：非绑定窗历史改存会话级 chat-<sid>.json
        （同样末 60 条、轮末回写问答对）；回复流式（stream_text=True，
        agent.chat.delta 过渡行 + 终稿清剪）；步数上限
        min(dispatcher.max_steps, config.chat_max_steps)；干活纪律放开——有价值的
        阶段性结论可 bb_add_finding 入图（对话轮产 finding 无任务上下文 → 无
        basis 边，图上以 targets/relates_to 连通，DESIGN §6）。

        返回 None = 无引导可回应 / 暂停·中断置位不消费 / 对话中被中断（drain 掉
        的引导已消费、回复丢弃）。与任务轮互斥点：不认领任务、不落快照、不起
        心跳、不动 dispatcher.current_task_id / finished / awaiting_human；
        finish 不在 CHAT_TOOLS，纯文本回复即终止。worker 在本方法返回非 None 后
        continue 再认领——空闲期连发多条引导逐轮回应，天然成连续对话。"""
        if self._abort_req.is_set() or self._pause_req.is_set() or self.paused:
            return None  # 暂停/中断/暂停态：不消费引导（留给任务轮/恢复期）
        # 收件箱消费口径统一走订阅声明表 _INBOX_SUBSCRIPTIONS（2026-09-21）：对话
        # 轮只取无任务上下文也能响应的 kind——basis_stale（强制三选一）与
        # finding_update（发现增补）留收件箱，等下一轮认领期/步边界消费。
        notices, drained = self._drain_inbox("chat")
        parts = [p for _k, p in notices]
        if not parts:
            return None
        note_text = "\n".join(
            str((r.get("payload") or {}).get("text", "")).strip()
            for r in drained if r.get("kind") == "human_note").strip()
        # 历史上下文分流（2026-09-20 对话化）：绑定窗=任务现场文件（v0.71）；
        # 非绑定窗=会话级 chat 文件。sanitize_snapshot_tail 截掉末尾悬空 tool_use
        # 半对防 API 拒；损坏/缺失 → 空降级，行为同首轮
        history: list[dict[str, Any]] = []
        hist_label = "本会话"
        if task_id:
            history = sanitize_snapshot_tail(self._load_task_transcript(task_id))
            hist_label = "本任务"
        else:
            history = sanitize_snapshot_tail(self._load_session_chat())
        if history:
            chat_mode = (f"延续模式：以下是{hist_label}既往对话记录（截尾保留），"
                         "接着此上下文回应人类引导；可继续用工具查黑板/读工作区文件。"
                         "不认领任务、不改资产终态；有价值的阶段性结论随手 bb_add_finding"
                         " 登记（带 evidence，relates_to 串起推导链自动上图；没验证过的"
                         "标 unverified）。干完必须给人类文字结论。回答简洁，直接回应人类。")
        else:
            chat_mode = ("与人类直接对话（Claude Code 式工作窗）：回答引导/提问，需要时"
                         "用工具查黑板（资产/发现/事件/知识库）或跑命令核实再作答。"
                         "不认领任务、不改资产终态；有价值的阶段性结论随手 bb_add_finding"
                         " 登记（带 evidence，relates_to 串起推导链自动上图；没验证过的"
                         "标 unverified）。干完必须给人类文字结论。回答简洁，直接回应人类。")
        system = self.build_system_prompt(chat_mode, self.skill_context_for(note_text))
        messages: list[dict[str, Any]] = list(history)
        messages.append({"role": "user", "content": "\n".join(parts)})
        final_text: str | None = None
        max_steps = min(self.dispatcher.max_steps, self.config.chat_max_steps)
        step = 0
        while step < max_steps:
            if self._abort_req.is_set():
                self._abort_req.clear()  # 无任务：只清标志，不走 _abort_current_task
                return None  # drain 已消费，回复丢弃（abort 语义：本轮作废）
            step += 1
            try:
                resp = self._chat_interruptible(messages, system=system,
                                                tools=CHAT_TOOLS, stream_text=True)
            except _StepInterrupted:
                # 对话中被中断（LLM 等待期间置位）：半截回复已由
                # _flush_interrupted_reply 落盘（interrupted=true），引导已 drain
                self._abort_req.clear()
                return None
            if self._abort_req.is_set():
                # 对话中被中断（LLM 调用期间置位，含用户 ■ 硬中断）：回复丢弃
                self._abort_req.clear()
                return None
            self._record_usage(resp, source="agent", llm_obj=self.llm)
            self._emit_final_thinking(resp, step, None)
            messages.append({"role": "assistant", "content": resp.raw.get("content", [])})
            if not resp.tool_calls:
                text = "".join(
                    b.get("text", "") for b in resp.raw.get("content", [])
                    if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:  # 回复落事件流（💬 Agent 回复），空文本防死循环直接退
                    sid = resp.raw.get("_stream_id") if isinstance(resp.raw, dict) else None
                    self.bb.append_event(
                        self.project_id, "agent.chat",
                        {"session_id": self.session["id"], "text": text,
                         **({"stream_id": sid} if sid else {})},
                        session_id=self.session["id"], author=self.session["id"])
                    if sid:
                        try:
                            self.bb.prune_chat_deltas(self.project_id, sid)
                        except Exception:  # noqa: BLE001 —— 只多留过渡行
                            log.exception("agent.chat.delta 清剪失败 stream_id=%s", sid)
                final_text = text or None
                break
            for tc in resp.tool_calls:
                result_text = self.dispatcher.dispatch(tc.name, tc.arguments)
                messages.append(self.llm.tool_result_message(tc, result_text))
            self._trim(messages)
        if final_text:
            # 问答对回写历史（下轮续聊/重启后仍带全上下文）；本轮带工具的中间步
            # 不回写（只留问答对）。绑定窗→任务现场文件（v0.71 原样）；
            # 非绑定窗→会话级 chat 文件（2026-09-20 对话化新增）
            if task_id:
                self._append_chat_to_transcript(task_id, parts, final_text)
            else:
                self._append_chat_to_session(parts, final_text)
        return final_text

    def _append_chat_to_transcript(self, task_id: str, notes: list[str],
                                   reply: str) -> None:
        """延续模式：把一轮续聊问答追加进 task-<tid>.json 现场文件（原子写，
        失败降级不影响回复送达）。messages 只追加 user/assistant 两条。"""
        path = task_transcript_path(self.artifacts_dir, task_id)
        if path is None or not path.exists():
            return
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(st, dict) or st.get("task_id") != task_id \
                    or not isinstance(st.get("messages"), list):
                return
            st["messages"].append({"role": "user", "content": "\n".join(notes)})
            st["messages"].append({"role": "assistant", "content": reply})
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            log.exception("续聊问答回写任务现场失败（任务 %s）", task_id)

    def _load_session_chat(self) -> list[dict[str, Any]]:
        """会话级对话历史（2026-09-20 对话化）：非绑定窗 run_chat 的上下文来源。
        防御同 _load_task_transcript：JSON 损坏/结构异常 → 空列表降级；只取末
        60 条（更早内容以黑板 finding/事件为准）。"""
        path = session_chat_path(self.artifacts_dir, self.session["id"])
        if path is None or not path.exists():
            return []
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        msgs = st.get("messages") if isinstance(st, dict) else None
        if not isinstance(msgs, list):
            return []
        return [m for m in msgs if isinstance(m, dict)][-60:]

    def _append_chat_to_session(self, notes: list[str], reply: str) -> None:
        """把一轮对话问答追加进 chat-<sid>.json（2026-09-20 对话化；原子写，
        文件不存在则创建，120 条滚动上限，失败降级不影响回复送达）。messages
        只追加 user/assistant 两条，与任务现场文件同构（sanitize_snapshot_tail
        可直接消费）。"""
        path = session_chat_path(self.artifacts_dir, self.session["id"])
        if path is None:
            return
        try:
            if path.exists():
                st = json.loads(path.read_text(encoding="utf-8"))
                if not (isinstance(st, dict) and isinstance(st.get("messages"), list)):
                    st = {"session_id": self.session["id"], "messages": []}
            else:
                st = {"session_id": self.session["id"], "messages": []}
            st["messages"].extend([
                {"role": "user", "content": "\n".join(notes)},
                {"role": "assistant", "content": reply},
            ])
            if len(st["messages"]) > 120:
                st["messages"] = st["messages"][-120:]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            log.exception("会话对话历史回写失败（会话 %s）", self.session["id"])

    def _chat_interruptible(self, messages: list[dict[str, Any]], *, system: str,
                            tools: list[dict[str, Any]],
                            stream_text: bool = False, step: int | None = None):
        """■ 即点即停（2026-09-19）：chat 在旁路线程跑，主线程每 0.3s 查 abort——
        置位即刻抛 _StepInterrupted 放弃等待（旁路线程 daemon 随 HTTP 超时自行
        结束，结果丢弃）；正常完成原样返回响应，LLM 自身异常原样抛出。
        思考流式（2026-09-19）：llm 声明 stream_capable 时带 on_thinking/should_cancel
        ——旁路线程只攒 delta 缓冲，主线程轮询间隙节流落 llm.thinking.delta 事件
        （累计全文+seq，前端按 stream_id 组装成一行滚动思考）；should_cancel 接
        _abort_req，中断时流式连接即刻掐断。返回的响应带 _stream_id 供终稿落库
        关联（llm.thinking payload.stream_id → 清剪 delta 行）。
        回复流式（2026-09-20 对话化）：stream_text=True 时同机制带 on_text，
        节流落 agent.chat.delta（累计全文+seq，节流阈值 ≥80 新字符或 1.0s）——
        任务轮叙述不流式（爆炸半径控制），只有对话轮开。终稿 agent.chat 带
        stream_id，落库后清剪该流 delta 行。
        回复缓冲恒攒（2026-09-21 中断落盘）：on_text 在流式分支内恒传（任务轮也
        攒，只是 _flush_text 被 stream_text 守卫不落 delta 事件）——abort 时
        _flush_interrupted_reply 把半截回复落 agent.chat 终稿（interrupted=true；
        对话轮带 stream_id 清剪 delta，任务轮带 step 截 2000）。
        截断整轮重试（2026-09-20 事故修复）：LLMError.truncated（工具参数流截断）
        时重建消息整轮重发 ≤_CHAT_TRUNC_RETRIES 次——传输层流不可重放，只能在
        轮级重试；重试轮 thinking/text 缓冲清零重流（stream_id/seq 延续，终稿落库
        时统一清剪 delta，审计不留中间轮）。最后一轮重试降级**非流式**（不带
        on_thinking/on_text，走 L 层内建非流式路径）：网关 SSE 稳定性差导致连续
        截断时整包响应不受流断影响——流式轮耗尽才降级，思考行照常由终稿落库。"""
        box: dict[str, Any] = {}
        stream_id = uuid.uuid4().hex[:12]
        acc: list[str] = []  # thinking delta 缓冲（daemon 写 / 主线程读，GIL 下 append 原子）
        pub = {"chars": 0, "seq": 0, "at": 0.0}  # 上次发布状态（节流）
        text_acc: list[str] = []  # 回复 delta 缓冲（对话轮 stream_text=True 才有写入方）
        pub_text = {"chars": 0, "seq": 0, "at": 0.0}

        def _worker() -> None:
            try:
                kwargs: dict[str, Any] = {"system": system, "tools": tools}
                if stream_rounds_left > 0 and getattr(self.llm, "stream_capable", False):
                    kwargs["on_thinking"] = acc.append
                    kwargs["should_cancel"] = self._abort_req.is_set
                    # on_text 恒传（2026-09-21 中断落盘）：任务轮只攒不发 delta，
                    # abort 时半截叙述有货可捞；对话轮由 _flush_text 节流发布。
                    kwargs["on_text"] = text_acc.append
                box["resp"] = self.llm.chat(messages, **kwargs)
            except BaseException as e:  # noqa: BLE001 —— 原样转抛给等待方
                box["err"] = e

        def _flush_thinking() -> None:
            """节流发布增量：≥120 新字符，或文本有变且距上次 ≥1.0s（0.3s 轮询天然限频）。"""
            text = "".join(acc)
            now = time.monotonic()
            grown = len(text) - pub["chars"]
            if not text or (grown < 120 and (now - pub["at"] < 1.0 or grown <= 0)):
                return
            pub["chars"] = len(text)
            pub["seq"] += 1
            pub["at"] = now
            self.bb.append_event(
                self.project_id, "llm.thinking.delta",
                {"stream_id": stream_id, "thinking": text, "seq": pub["seq"]},
                session_id=self.session["id"], author=self.session["id"])

        def _flush_text() -> None:
            """回复增量节流发布（2026-09-20 对话化）：≥80 新字符，或有变化且距上次
            ≥1.0s——比 thinking 阈值低（回复短、期望近打字机观感）。任务轮
            （stream_text=False）只攒不发（2026-09-21 中断落盘：缓冲供 abort 时
            _flush_interrupted_reply 捞半截叙述）。"""
            if not stream_text:
                return
            text = "".join(text_acc)
            now = time.monotonic()
            grown = len(text) - pub_text["chars"]
            if not text or (grown < 80 and (now - pub_text["at"] < 1.0 or grown <= 0)):
                return
            pub_text["chars"] = len(text)
            pub_text["seq"] += 1
            pub_text["at"] = now
            self.bb.append_event(
                self.project_id, "agent.chat.delta",
                {"stream_id": stream_id, "text": text, "seq": pub_text["seq"]},
                session_id=self.session["id"], author=self.session["id"])

        resp = None
        stream_rounds_left = _CHAT_TRUNC_RETRIES  # 最后一轮降级非流式（整包响应无 SSE 断流）
        for attempt in range(_CHAT_TRUNC_RETRIES + 1):
            box.clear()
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            while t.is_alive():
                if self._abort_req.is_set():
                    self._flush_interrupted_reply(stream_id, text_acc, step)
                    raise _StepInterrupted()
                t.join(0.3)
                _flush_thinking()
                _flush_text()
            _flush_thinking()  # 收尾最后一拍（不足节流阈值的尾差也发出去，终稿随后到达）
            _flush_text()
            if "err" not in box:
                resp = box["resp"]
                break
            err = box["err"]
            if self._abort_req.is_set():
                # 流被 should_cancel 掐断（LLMError「已中断」等）：先捞半截回复再抛
                self._flush_interrupted_reply(stream_id, text_acc, step)
                raise err
            if (not (isinstance(err, LLMError) and getattr(err, "truncated", False))
                    or attempt >= _CHAT_TRUNC_RETRIES):
                raise err
            log.warning("LLM 工具参数流截断，整轮重试 %d/%d（stream_id=%s）: %s",
                        attempt + 1, _CHAT_TRUNC_RETRIES, stream_id, err)
            acc.clear()  # 重试轮思考重新累计（前端按最新 seq 的累计全文覆盖显示）
            pub["chars"] = 0
            text_acc.clear()
            pub_text["chars"] = 0
            if stream_rounds_left > 0:
                stream_rounds_left -= 1
                if stream_rounds_left == 0:
                    log.warning("截断重试耗尽流式轮，最后一轮降级非流式（stream_id=%s）", stream_id)
        resp.raw["_stream_id"] = stream_id  # 终稿落库关联 + 清剪 delta 用
        return resp

    def _flush_interrupted_reply(self, stream_id: str, text_acc: list[str],
                                 step: int | None) -> None:
        """■ 中断落盘（2026-09-21）：abort 时半截回复不再丢弃——text_acc 有存货就
        落 agent.chat 终稿（interrupted=true 标记，前端可辨识）。对话轮（step=None）
        带 stream_id 并清剪该流 delta——turn 分组有收口回复、审计只留一条；任务轮
        带 step、文本截 2000（同任务叙述终稿口径）。thinking 残留 delta 维持现状
        （被中断思考的现场审计，不落终稿，2026-09-19 定稿）。"""
        text = "".join(text_acc).strip()
        if not text:
            return
        payload: dict[str, Any] = {"session_id": self.session["id"],
                                   "interrupted": True}
        if step is not None:
            payload["step"] = step
            payload["text"] = text[:2000]
        else:
            payload["text"] = text
            payload["stream_id"] = stream_id
        self.bb.append_event(
            self.project_id, "agent.chat", payload,
            session_id=self.session["id"], author=self.session["id"])
        if step is None:
            try:
                self.bb.prune_chat_deltas(self.project_id, stream_id)
            except Exception:  # noqa: BLE001 —— 清剪失败只多留过渡行
                log.exception("agent.chat.delta 清剪失败 stream_id=%s", stream_id)

    # ---------- 内部 ----------

    def _emit_final_thinking(self, resp, step: int, duration_s: float | None) -> None:
        """终稿 llm.thinking（带 stream_id）+ 清剪该流的 llm.thinking.delta 过渡行
        ——审计仍只留终稿一条，事件表与流式化之前一样干净。中断/LLM 异常路径不
        走到这里（残留 delta = 被中断思考的现场审计，前端照常组装显示）。"""
        if not (resp.thinking and resp.thinking.strip()):
            return
        sid = resp.raw.get("_stream_id") if isinstance(resp.raw, dict) else None
        self.bb.append_event(
            self.project_id, "llm.thinking",
            {"thinking": resp.thinking, "step": step, "duration_s": duration_s,
             **({"stream_id": sid} if sid else {})},
            session_id=self.session["id"], author=self.session["id"])
        if sid:
            try:
                self.bb.prune_thinking_deltas(self.project_id, sid)
            except Exception:  # noqa: BLE001 —— 清剪失败只多留过渡行，不影响主流程
                log.exception("llm.thinking.delta 清剪失败 stream_id=%s", sid)

    def _dead_end_notice(self, task: dict) -> str | None:
        """C3 跨轨路标三级注入（§5.2 定稿）：已排除方向/死路（status=false-positive
        findings）注入认领会话——① scope/资产精确匹配全文（cap 5）② 同 host 聚合
        一行动态摘要（跨端口可见、未覆盖目标显形）③ 项目级计数 + 查询纪律。
        渗透=已排除攻击路径、CTF=死路线索、逆向=已排除假设。"""
        fps = [f for f in self.bb.list_findings(self.project_id)
               if f["status"] == "false-positive"]
        if not fps:
            return None
        assets = {a["id"]: a for a in self.bb.list_assets(self.project_id)}

        def host_val(aid: str | None) -> str:
            a = assets.get(aid)
            while a:
                if a["type"] == "host":
                    return a["value"]
                a = assets.get(a.get("parent_id") or "")
            return ""

        scope_text = ((task.get("scope") or "") + " " +
                      (task.get("objective") or "")).lower()
        exact: list[str] = []
        by_host: dict[str, list[str]] = {}
        for f in fps:
            av = (assets.get(f.get("target_asset_id")) or {}).get("value", "").lower()
            hv = host_val(f.get("target_asset_id"))
            blob = (f["title"] + " " + json.dumps(f.get("evidence", {}),
                                                  ensure_ascii=False)[:200]).lower()
            line = f"⛔ {f['title']}"
            if av and (av in scope_text or (hv and hv in scope_text)):
                exact.append(line)
            elif hv:
                by_host.setdefault(hv, []).append(f["title"][:40])
            else:
                by_host.setdefault("(其他)", []).append(f["title"][:40])
        lines: list[str] = ["🚫 路标——以下方向已被排除（勿重走，动手前先 bb_query 查死路）："]
        for f in exact[:5]:
            lines.append(f"  {f}")
        for hv, titles in list(by_host.items())[:10]:
            lines.append(f"  ⛔ {hv}：已排除 {len(titles)} 条（{'、'.join(titles[:2])}…）"
                         if len(titles) > 2 else
                         f"  ⛔ {hv}：已排除 {len(titles)} 条（{'、'.join(titles)}）")
        if len(exact) > 5:
            lines.append(f"  （另有 {len(exact) - 5} 条精确匹配路标未展开）")
        return "\n".join(lines)

    def _old_plan_notice(self, task: dict) -> str | None:
        """C1：重新认领曾执行过的任务时，注入旧计划与依据发现摘要——
        done 步骤带真实性注记（未经本会话验证，不视为已验证），防新会话误读。"""
        old_plan = task.get("plan") or []
        if not old_plan:
            return None
        lines = ["♻ 该任务曾被执行过并中途停止（现重新认领）。以下旧计划仅供参考——"
                 "done 步骤未经你验证，重做前先核实，不要盲目沿用："]
        for s in old_plan:
            line = f"- {s.get('id')} [{s.get('status')}] {s.get('title')}"
            if s.get("note"):
                line += f"（{s['note']}）"
            lines.append(line)
        fsums: list[str] = []
        for fid in (task.get("context_refs") or [])[:5]:
            try:
                f = self.bb.get_finding(self.project_id, fid)
            except Exception:  # noqa: BLE001
                f = None
            if f:
                fsums.append(f"- {fid} [{f['status']}/{f['severity']}] {f['title']}")
        if fsums:
            lines.append("该任务依据的发现摘要：")
            lines += fsums
        return "\n".join(lines)

    def _handover_notice(self, task: dict, has_transcript: bool) -> str | None:
        """C10 跨会话接手提示：有现场或履历才注入。对上面的对话现场做元说明
        （可续做、已有结论不必重查；历史工具结果属于当时现场不可重放），
        并注入历次尝试履历（outcome/result_note/blocked_reason，黑板侧落库）。"""
        ctx = task.get("context") or {}
        attempts = ctx.get("attempts") or []
        if not has_transcript and not attempts:
            return None
        n = len(attempts) + 1
        lines: list[str] = []
        if has_transcript:
            lines.append(
                f"🔁 第 {n} 次尝试接手：以上对话现场来自此前执行（可直接续做，"
                "已有结论不必重查）；注意历史里的工具结果属于当时现场，"
                "不可重放，需要时重新取证。")
        if attempts:
            lines.append(f"本任务共 {n - 1} 次历史尝试：")
            from core.blackboard.tasks import render_attempts_lines
            lines += render_attempts_lines(attempts)
        return "\n".join(lines) if lines else None

    def _loop(self, system: str, messages: list[dict[str, Any]], objective: str,
              start_step: int = 1) -> str | None:
        """薄包装：在册/注销 `_live_state`（v0.64 暂停请求即时落盘的现场源），
        主体见 `_loop_body`。退出（含暂停/中断/异常）必清 None，防陈旧现场被快照。
        另在册 `_salvage_ctx`（fail 抢救收尾用）：messages 与 _live_state 同为就地
        变异的同一列表，异常穿出后引用仍有效——正常退出（含暂停/即点即停）立即
        清除，只有异常路径保留给 `_fail_task_on_error` 消费。"""
        self._live_state = {"system": system, "messages": messages,
                            "objective": objective}
        self._salvage_ctx = {"objective": objective, "messages": messages}
        try:
            result = self._loop_body(system, messages, objective, start_step)
        finally:
            self._live_state = None
        self._salvage_ctx = None
        return result

    def _loop_body(self, system: str, messages: list[dict[str, Any]], objective: str,
                   start_step: int = 1) -> str | None:
        """返回 None = 暂停/中断退出（调用方不得 finalize）；其余返回任务总结。

        步数上界读 dispatcher.max_steps（E8）：request_steps 增补写在那里，
        本会话内跨任务生效。"""
        max_steps = self.dispatcher.max_steps
        step = start_step
        # 每任务复位收尾标志（潜伏 bug 修复：finish 后同会话再认领的任务会在
        # 首步命中 stale finished → 返回旧总结并被 _finalize 误标失败）
        self.dispatcher.finished = False
        self.dispatcher.awaiting_human = False
        self.dispatcher.summary = ""
        # while 而非 range（E8）：request_steps 在步内增补预算后，循环上界随之
        # 前移——耗尽轮当场自救（步号不增）也成立，不会因 range 预计算被截断。
        while step <= max_steps:
            if self._persona_dirty:
                # v0.71 中途改角色热换装：下个步进前重建 system prompt
                #（config/dispatcher 边界已在 apply_role_change 即时生效）
                system = self.build_system_prompt(objective, self._last_skill_context)
                self._persona_dirty = False
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
            _chat_t0 = time.monotonic()
            try:
                resp = self._chat_interruptible(messages, system=system,
                                                tools=AGENT_TOOLS, step=step)
            except _StepInterrupted:
                # ■ 即点即停（2026-09-19）：LLM 调用等待中被人手中断——不等本步
                # 完成，立刻按硬中断收尾（任务 fail「人工中断」，快照保留可续跑）。
                # usage/thinking 不落（响应已丢弃）；半截叙述已由 _flush_interrupted_reply
                # 在抛出前落盘（2026-09-21 中断落盘）。
                self._abort_current_task()
                return None
            chat_s = round(time.monotonic() - _chat_t0, 3)
            self._record_usage(resp, source="agent", llm_obj=self.llm)
            # 思考终稿落事件流（DESIGN.md §12）：折叠一行摘要、展开看全文；
            # duration_s = 整次 chat 墙钟（含网络+生成），前端显示「思考 Ns」；
            # 流式增量行（llm.thinking.delta）由 _emit_final_thinking 一并清剪
            self._emit_final_thinking(resp, step, chat_s)
            messages.append({"role": "assistant", "content": resp.raw.get("content", [])})
            # 任务叙述落事件流（2026-09-19 直播间终端化，与 run_chat 的 agent.chat 对称）：
            # assistant 在工具调用之间说的话=「做了什么/进度」叙述行；有 tool_calls 的步也落
            # （叙述常出现在调用前）。截 2000 字符防事件表膨胀；键用 step 不用 step_id
            # （避免撞 A2 计划事件的前端摘要分支）。
            _narr = "".join(b.get("text", "") for b in resp.raw.get("content", [])
                            if isinstance(b, dict) and b.get("type") == "text")
            if _narr.strip():
                self.bb.append_event(
                    self.project_id, "agent.chat",
                    {"session_id": self.session["id"], "text": _narr[:2000], "step": step},
                    session_id=self.session["id"], author=self.session["id"])
            if not resp.tool_calls:
                # 纯文本回复：视为停等，提示其用 finish 或继续干活
                messages.append({"role": "user",
                                 "content": "（请继续执行：调用工具干活，或调用 finish 结束并总结）"})
            else:
                for tc in resp.tool_calls:
                    result_text = self.dispatcher.dispatch(tc.name, tc.arguments)
                    messages.append(self.llm.tool_result_message(tc, result_text))
                self._trim(messages)
                # G3：过阈值先 LLM 摘要压缩，_trim 硬上限兜底；任务已收尾不再压缩
                if not self.dispatcher.finished:
                    self._maybe_summarize(messages)
            self._checkpoint_task_transcript(messages, objective)  # C10 每步落盘任务现场
            if getattr(self.dispatcher, "awaiting_human", False):
                # C1：Agent 自主挂起（awaiting_human）——快照落盘 + 任务 fail
                # （blocked_reason=awaiting_human，resumable=True），现场保留，
                # 人类可经看板「▶ 续跑」（E12 revive）或「✅ 已解决，放回继续」承接。
                # 会话不结束（不走 finished/_finalize），worker 继续认领下一个任务。
                self.dispatcher.awaiting_human = False
                self._resume_state = {
                    "system": system, "messages": messages, "objective": objective,
                    "task_id": self.dispatcher.current_task_id,
                    "next_step": step + 1, "max_steps": self.dispatcher.max_steps,
                    "reason": "awaiting",
                }
                self._persist_snapshot()
                try:
                    self.tq.fail(
                        self.dispatcher.current_task_id, self.session["id"],
                        self.dispatcher.summary or "等待人工输入",
                        resumable=True, blocked_reason="awaiting_human",
                        persona_role=self.dispatcher.current_persona_role)
                except Exception:  # noqa: BLE001
                    log.exception("awaiting_human fail 失败")
                self.dispatcher.current_task_id = None
                self._resume_state = None  # 快照只留在磁盘（内存态泄漏会被下轮 run_next_task 误消费清盘）
                self._stop_heartbeat()
                return None  # 任务收尾已处理（同暂停/中断语义，不走 _finalize）
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
        # 口径统一走订阅声明表 _INBOX_SUBSCRIPTIONS（2026-09-21）：human_note 轮末
        # 语义（不在任务中途打断思路，留收件箱等认领期/恢复期注入）由订阅表表达；
        # 未登记 kind 滞留不吞。
        notices, _dr = self._drain_inbox("step")
        for _kind, notice in notices:
            messages.append({"role": "user", "content": notice})
        self._checkpoint_task_transcript(messages, objective)  # C10：注入的私信/引导也进现场
        return None

    def _drain_inbox(self, scenario: str, *, stale_refs: list[str] | None = None
                     ) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
        """■ 收件箱订阅声明化（2026-09-21）：按场景消费收件箱的唯一入口。

        消费口径只认模块级 _INBOX_SUBSCRIPTIONS 声明表：scenario ∈ {claim, resume,
        step, chat} 决定取哪些 kind（only_kinds 从表构造——未登记 kind 滞留收件箱
        红点可见，绝不被静默吞）；渲染顺序=表内声明序；除 basis_stale（签名不同，
        需任务挂标 stale_refs 现场补水）外，渲染器恰为 _<kind>_notice 命名约定。

        返回 ([(kind, notice 文本), …], 原始 drained 行)——后者供调用方提取
        human_note 正文（run_chat 的 note_text）等。"""
        kinds = tuple(k for k, scs in _INBOX_SUBSCRIPTIONS.items() if scenario in scs)
        drained: list[dict[str, Any]] = \
            self.bb.inbox_drain(self.project_id, self.session["id"], only_kinds=kinds) \
            if kinds else []
        out: list[tuple[str, str]] = []
        for kind in _INBOX_SUBSCRIPTIONS:  # 固定声明序
            if kind not in kinds:
                continue
            if kind == "basis_stale":
                notice = self._basis_stale_notice(stale_refs or [], drained)
            else:
                notice = getattr(self, f"_{kind}_notice")(drained)
            if notice:
                out.append((kind, notice))
        return out, drained

    def _human_note_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「人类引导」消息（E8，kind='human_note'）：信息式注入；多条按序
        各占一行。payload.attachments（2026-09-19 附件随发）渲染成 📎 行——
        纯附件无文字的引导同样成立。注入点=认领期/恢复期（轮末语义，
        2026-09-19 起）：不在任务中途打断思路。"""
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "human_note":
                continue
            payload = r.get("payload") or {}
            text = str(payload.get("text", "")).strip()
            if text:
                lines.append(f"- {text}")
            lines.extend(_attachment_lines(payload.get("attachments")))
        if not lines:
            return None
        return "\n".join(["💬 人类引导："] + lines)

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

    def _escalation_result_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「升级命令回执」消息（H3，kind='escalation_result'）：request_escalation
        经人类批准执行后，结果经收件箱回流——信息式注入，Agent 拿到回执继续原路线。"""
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "escalation_result":
                continue
            p = r.get("payload") or {}
            cmd = str(p.get("cmd", ""))[:200]
            out = str(p.get("text", ""))[:2500]
            lines.append(f"🛫 升级命令已执行（人类批准，一次性）：{cmd}\n{out}")
        if not lines:
            return None
        return "\n---\n".join(lines)

    def _agent_message_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「Agent 私信」消息（2026-09-20 会话窗对话化，§17 B1，kind='agent_message'）：
        会话窗间知会（intel/handoff/assist 三分类，异步模型不做同步接力）——信息式
        注入，收件方按需回应不强制。payload.from 是发件窗会话 id，这里解析出
        会话显示名便于识别。"""
        labels = {"intel": "情报", "handoff": "移交", "assist": "协助请求"}
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "agent_message":
                continue
            p = r.get("payload") or {}
            sub = str(p.get("subkind") or "intel")
            text = str(p.get("text", "")).strip()
            head = f"- [{labels.get(sub, sub)}] 来自 {self._session_label(str(p.get('from', '')))}：{text}"
            refs = p.get("refs") or []
            if refs:
                head += f"（引用：{', '.join(str(x) for x in refs[:5])}）"
            lines.append(head)
        if not lines:
            return None
        return "\n".join(["📨 Agent 私信（信息式；涉你路线可自行调整，assist 类尽快回应）："] + lines)

    def _task_receipt_notice(self, inbox_rows: list[dict[str, Any]]) -> str | None:
        """拼「子任务回执」消息（2026-09-20 P3 多智能体协调，kind='task_receipt'）：
        派生任务收尾自动回执父窗——信息式注入，父 agent 拿到摘要继续原路线。"""
        lines: list[str] = []
        for r in inbox_rows:
            if r.get("kind") != "task_receipt":
                continue
            p = r.get("payload") or {}
            ok = p.get("status") == "done"
            head = (f"{'✅' if ok else '❌'} 子任务{'完成' if ok else '失败'}"
                    f" {p.get('task_id', '')}「{p.get('objective', '')}」")
            note = str(p.get("result_note", "")).strip()
            if note:
                head += f"：{note}"
            if not ok and p.get("blocked_reason"):
                head += f"（失败原因：{p['blocked_reason']}）"
            finds = p.get("findings") or []
            if finds:
                head += "\n  相关发现：" + "；".join(
                    f"{f.get('severity', '?')}「{f.get('title', '')}」"
                    for f in finds if isinstance(f, dict))
            lines.append(head)
        if not lines:
            return None
        return "\n---\n".join(lines)

    def _session_label(self, sid: str) -> str:
        """会话 id → 显示名（未知/异常回退原 id）。"""
        if not sid:
            return "?"
        try:
            row = self.bb.conn.execute(
                "SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()
            if row and row["name"]:
                return f"{row['name']}({sid})"
        except Exception:  # noqa: BLE001 —— 显示名失败不致信令丢失
            pass
        return sid

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
        return persisted_snapshot_path(self.artifacts_dir, self.session["id"])

    def _persist_snapshot(self, st: dict[str, Any] | None = None) -> None:
        """暂停快照落盘：写 workspace 文件并在 sessions.meta 存指针。
        C6 双写：有任务时同步落任务键 `task-<tid>.resume.json`（**不含 system**，
        跨角色安全）——任何 failed 卡可跨会话/跨角色「⚡ 带现场续跑」；再次暂停
        覆盖前移断点。落盘失败只降级为纯内存快照（本进程内 resume 仍可用）。
        v0.64：st 缺省取 self._resume_state（步边界暂停路径）；显式传入供
        pause_snapshot_now 直接落盘（不碰 _resume_state，worker 生命周期不受扰）。"""
        if st is None:
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
        # C6 任务键双写（覆盖前移断点）：快照不含 system——认领会话按角色重建
        task_id = st.get("task_id")
        if not task_id:
            return  # 纯 objective 直跑无任务，不落任务键
        task_path = task_resume_path(self.artifacts_dir, task_id)
        if task_path is None:
            return
        try:
            task_path.write_text(
                json.dumps({k: v for k, v in st.items() if k != "system"},
                           ensure_ascii=False),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.exception("任务键快照双写失败（任务 %s）", task_id)

    def pause_snapshot_now(self) -> bool:
        """v0.64 暂停请求即时落盘（关「暂停未到步边界就关后端」的窗口期）：
        从 _loop 在册的 `_live_state` 构建断点快照（尾部 sanitize 掉半步悬空的
        tool_use/tool_result），经 _persist_snapshot 双写会话键 + 任务键文件。
        **不动行状态、不置 paused、不碰 _resume_state**——worker 生命周期不受扰：
        后端活着，步边界 `_enter_paused` 会以干净现场覆盖；后端死了，这份快照
        兜底（重启清扫按 resume_snapshot 指针归位 paused + 豁免其 claimed 任务）。
        幂等可重复调用；无在跑现场（空闲/已到步边界暂停）返回 False 不动作。"""
        live = self._live_state
        if not live or self.dispatcher.current_task_id is None:
            return False
        st = {
            "system": live["system"],
            "messages": sanitize_snapshot_tail(live["messages"]),
            "objective": live["objective"],
            "task_id": self.dispatcher.current_task_id,
            "next_step": self.dispatcher.step + 1,
            "max_steps": self.dispatcher.max_steps,
            "reason": "pause",
        }
        self._persist_snapshot(st)
        return True

    # ---------- 任务现场落盘（C10：上下文归任务所有，跨会话接手） ----------

    def _checkpoint_task_transcript(self, messages: list[dict[str, Any]],
                                    objective: str) -> None:
        """C10 每步任务现场落盘：写 <snapshots>/task-<tid>.json（temp+replace
        原子替换，防崩溃撕裂/并发读者读到半截）。只认 current_task_id——无任务
        （纯 objective 直跑）不落盘；失败降级不影响主循环。"""
        task_id = self.dispatcher.current_task_id
        path = task_transcript_path(self.artifacts_dir, task_id or "") if task_id else None
        if path is None:
            return  # 无任务（纯 objective 直跑）不落盘；防 task-.json 空名残file（走查发现）
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"task_id": task_id, "objective": objective,
                 "session_id": self.session["id"], "messages": messages,
                 "updated_at": datetime.now(timezone.utc).isoformat()},
                ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            log.exception("任务现场落盘失败（任务 %s）", task_id)

    def _load_task_resume(self, task_id: str, task: dict) -> dict | None:
        """C6 认领即复活：读任务键断点快照（task-<tid>.resume.json）。
        objective 匹配 → 返回快照（messages 整体/next_step/max_steps 还原，
        由调用方消费即删）；objective 被改/损坏/任务不符 → 删快照、返回 None
        （降级 C10 transcript 接手）。"""
        path = task_resume_path(self.artifacts_dir, task_id)
        if path is None or not path.exists():
            return None
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = None
        if (not isinstance(st, dict) or st.get("task_id") != task_id
                or not isinstance(st.get("messages"), list)):
            clear_task_resume(self.artifacts_dir, task_id)  # 损坏/不符 → 清理降级
            return None
        if str(st.get("objective") or "").strip() != \
                str(task.get("objective") or "").strip():
            clear_task_resume(self.artifacts_dir, task_id)  # objective 被改 → 降级接手
            return None
        if not isinstance(st.get("max_steps"), int) or st["max_steps"] <= 0:
            clear_task_resume(self.artifacts_dir, task_id)
            return None
        return st

    def _load_task_transcript(self, task_id: str) -> list[dict[str, Any]]:
        """C10 跨会话接手：读任务现场文件为初始对话历史。防御点：JSON 损坏/
        task_id 不符/messages 结构异常 → 空列表降级（履历仍经接手提示注入）；
        只取末尾 60 条（≈30 步完整对话，更早内容以黑板 finding/事件为准）。"""
        path = task_transcript_path(self.artifacts_dir, task_id)
        if path is None:
            return []
        try:
            st = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(st, dict) or st.get("task_id") != task_id:
            return []
        msgs = st.get("messages")
        if not isinstance(msgs, list):
            return []
        msgs = [m for m in msgs
                if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
                and m.get("content") is not None]
        if not msgs:
            return []

        omitted = len(msgs) - 60
        if omitted > 0:
            msgs = msgs[-60:]
            # 截断边界不得拆开 tool_use/tool_result 对（首条若是 tool_result，
            # 其配对的 tool_use 已在被省略段里，API 会拒）——继续丢弃到非 result 为止
            while msgs and _is_tool_result(msgs[0]):
                msgs.pop(0)
                omitted += 1
            msgs = [{"role": "user",
                     "content": f"（此前 {omitted} 条对话历史已省略，"
                                "完整结论以黑板 finding/事件为准）"}] + msgs
        return msgs

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

    def revive_snapshot(self, task_id: str) -> dict | None:
        """E12：人工中断后从落盘快照复活（限原会话）——刷新会话行取最新快照
        指针，载回 _resume_state 并校验任务匹配；不匹配/无快照返回 None。
        任务的 reopen/claim 与 worker 提交由 API 层编排（POST /tasks/{tid}/resume）。"""
        try:
            row = self.bb.get_session(self.session["id"])
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            self.session = dict(row)  # 中断可能发生在本进程外：meta 指针要现读
        st = self._load_persisted_snapshot()
        if st is None or st.get("task_id") != task_id:
            return None
        self._resume_state = st
        self.paused = False
        return st

    def _task_gone(self) -> bool:
        """A1：当前任务行是否已从看板删除（claimed 任务可被人工取消，步边界感知）。"""
        task_id = self.dispatcher.current_task_id
        return bool(task_id) and self.tq.get_task(task_id) is None

    def _abort_current_task(self) -> None:
        """硬中断收尾：任务 fail（人工中断，不回队列）→ 会话空闲 + 审计。
        任务行已被删除（看板取消）时跳过 fail——task.deleted 事件即审计。
        E12：人工中断**保留落盘快照**（任务续跑凭证，看板 failed 卡可「▶ 续跑」
        原会话复活）；仅任务已删除/无快照时清理，防孤儿文件。
        C6：resumable 修复——原会话键快照**或**任务键断点快照任一存在即可续跑
        （旧实现只看会话键，暂停后中断的孤儿场景会误判不可续跑）。"""
        task_id = self.dispatcher.current_task_id
        if task_id is None and self._resume_state:
            task_id = self._resume_state.get("task_id")  # 空闲暂停态被中断：快照任务也要收尾
        self._resume_state = None
        task_alive = bool(task_id) and self.tq.get_task(task_id) is not None
        resumable = False
        if task_alive:
            path = self._snapshot_path()
            resumable = ((path is not None and path.exists())  # E8 会话键快照
                         or task_resume_path(self.artifacts_dir,
                                             task_id) is not None)  # C6 任务键快照
        if not resumable:
            self._clear_snapshot()  # 无任务/任务已删/无落盘快照：清理防孤儿
            clear_task_resume(self.artifacts_dir, task_id or "")
        self.paused = False
        self._pause_req.clear()
        self._abort_req.clear()
        self._stop_after_task = True  # 让 worker 循环退出，不再领新任务
        self._stop_heartbeat()
        note = "人工中断"
        if task_id:
            if not task_alive:
                note = "任务已被删除"
            else:
                try:
                    self.tq.fail(task_id, self.session["id"], "人工中断",
                                 resumable=resumable,
                                 persona_role=self.dispatcher.current_persona_role)
                except Exception:  # noqa: BLE001
                    log.exception("中断 fail 任务失败")
            self.dispatcher.current_task_id = None
        self._restore_base_persona()  # v14：硬中断恢复底色（下一单按新任务重新换装）
        self.dispatcher._close_browser_session()  # F6-v3：任务结束清浏览器 Page
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
        """策略顾问（§3）：规划上下文看执行摘要，给换思路建议——不是换 Agent。
        v0.65 卡壳召回：用对话尾部做 query 检索知识库，把最相关 1-2 篇手册摘要
        注入顾问视野——优先参考已验证路径，避免顾问建议重复试错。
        2026-09-20 两修：①视野收窄到**本会话**事件（此前全项目 limit=30，多窗时
        顾问满眼别人的上下文，执行者正确地把建议当跨上下文噪声拒掉，白烧一次
        调用）；②顾问发言落 `advisor.intervention` 事件——此前只进 messages，
        直播间只见 token 行不见说了什么（人工无法判断顾问干了什么、建议是否
        被采纳）。"""
        events = self.bb.recent_events(self.project_id, limit=30,
                                       session_id=self.session["id"])
        digest = json.dumps(
            [{"kind": e["kind"], "payload_head": json.dumps(e["payload"], ensure_ascii=False)[:120]}
             for e in events], ensure_ascii=False)
        kb_recall = self._kb_recall_for_advisor(messages)
        try:
            resp = self.planner_llm.chat(
                [{"role": "user", "content":
                  f"目标: {objective}\n最近事件摘要: {digest}\n"
                  f"{kb_recall}\n执行已多步无进展。给出 3 条以内换思路建议，"
                  "直接可执行。若知识库经验段有已验证路径，优先参考。"}],
                system="你是策略顾问，负责打破执行僵局。简洁、具体、不重复已失败路径。")
            advice = resp.text
            self._record_usage(resp, source="planner", llm_obj=self.planner_llm)
        except Exception as e:  # noqa: BLE001
            log.warning("策略顾问调用失败: %s", e)
            advice = "（顾问不可用）回顾黑板去重情况，换一个未尝试的攻击面。"
        self.bb.append_event(
            self.project_id, "advisor.intervention",
            {"text": advice[:2000]},
            session_id=self.session["id"], author=self.session["id"])
        return f"[策略顾问]\n{advice}"

    def _kb_recall_for_advisor(self, messages: list[dict], cap: int = 2,
                               chars: int = 800) -> str:
        """卡壳召回（v0.65）：对话尾部文本 → kbindex 匹配 top 手册 → 截摘要。
        任一环节失败/无命中返空串，顾问流程不受影响。"""
        if not self.packs_root:
            return ""
        try:
            tail_texts: list[str] = []
            for m in messages[-8:]:
                content = m.get("content")
                if isinstance(content, str):
                    tail_texts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") in (
                                "text", "tool_result"):
                            tail_texts.append(str(block.get("content", "")))
            query = " ".join(tail_texts)[-600:]
            if not query.strip():
                return ""
            hints = kb_module_hints(self.packs_root, self.capabilities, query,
                                    cap=cap)
            parts: list[str] = []
            for h, sec in hints:
                body = read_kb_module(self.packs_root, self.capabilities,
                                      h.module, max_chars=chars)
                if body:
                    loc = f"，相关段落：{sec}" if sec else ""
                    parts.append(
                        f"### {h.title}（kb_open: {h.module}{loc}）\n{body}")
            if not parts:
                return ""
            return ("## 知识库相关经验（优先参考已验证路径，避免重复试错）\n"
                    + "\n\n".join(parts))
        except Exception as e:  # noqa: BLE001 —— 召回失败静默降级
            log.warning("卡壳 kb 召回失败（忽略）: %s", e)
            return ""

    # ---------- v0.65 done 自动提案（T4 沉淀飞轮） ----------

    def _campaign_memory(self, task_id: str, result_note: str) -> None:
        """⑥ 战役记忆写入（complete_task 并联 hook）：result_note + 本次任务
        期间新增 findings 摘成 ≤500 字打法写全局库（跨项目召回）；项目事件流
        append campaign.memory（双写不保强一致：先全局库，事件失败仅记日志）。"""
        task = self.tq.get_task(task_id) or {}
        # 本次任务期间的发现（简版口径：按作者/时间不严格界定，取最近 5 条；
        # 打法摘要重在「怎么打的」，发现明细本就在项目黑板可查）
        findings = self.bb.list_findings(self.project_id)[-5:]
        flines = "\n".join(
            f"- [{f.get('severity', 'info')}] {f.get('title', '')[:80]}"
            for f in findings if f.get("title"))
        content = (f"收尾：{result_note[:300]}\n"
                   + (f"产出发现：\n{flines}" if flines else "")).strip()
        try:
            row = self.campaign.add(
                project_id=self.project_id, track=self.track,
                capability="/".join(self.capabilities),
                task_type=task.get("task_type", ""),
                title=task.get("objective", "")[:200] or task_id,
                content=content,
                target_key=(task.get("scope") or "")[:200])
        except Exception as e:  # noqa: BLE001
            log.warning("战役记忆写入失败（跳过）: %s", e)
            return
        try:
            self.bb.append_event(
                self.project_id, "campaign.memory",
                {"id": row["id"], "title": row["title"],
                 "content_head": row["content"][:120]},
                session_id=self.session["id"], author=self.session["id"])
        except Exception:  # noqa: BLE001 —— 事件失败不回滚全局库
            log.warning("campaign.memory 事件落库失败（全局库已写入）")

    def _sediment_proposal(self, task_id: str, result_note: str) -> None:
        """任务 done 后自动复盘（§4 沉淀飞轮）：planner LLM 复盘执行过程，验证过
        的有效手法/踩坑 → 产 kb 提案草稿（origin=agent，人批准后进知识库并更新索引）。
        输出 NONE / 任何失败都静默跳过——绝不影响任务收尾；与 AI 自主提案共用
        每会话 3 条 pending 上限（不挤占自主额度）。"""
        from core.skills import proposals  # 延迟导入避开 tools↔loop 环
        if not self.packs_root:
            return
        try:
            pending = [p for p in proposals.list_proposals(self.packs_root, "pending")
                       if p.get("session") == self.session["id"]]
            if len(pending) >= ToolDispatcher.PROPOSE_LIMIT_PER_SESSION:
                return
        except Exception:  # noqa: BLE001
            return
        events = self.bb.recent_events(self.project_id, limit=30)
        digest = json.dumps(
            [{"kind": e["kind"], "payload_head": json.dumps(e["payload"], ensure_ascii=False)[:120]}
             for e in events], ensure_ascii=False)
        caps = "、".join(self.capabilities) or "(无)"
        instruction = (
            "复盘任务执行过程：是否验证了可复用的有效手法、踩坑或更优路径？\n"
            "有则只输出一个提案 JSON（无其他文字），三类去向三选一：\n"
            '① kb 打法沉淀：{"kind": "kb", "mode": "create" 或 "edit", '
            '"target": {"kind": "kb", "cap": "能力包", "path": "kb内相对路径.md"}, '
            '"content": "提案文件全文", "summary": "一句话", '
            '"reason": "任务证据（任务id+关键观察）"}\n'
            '② index 增补：本任务验证的测试点在 kb 中有对应手册、且全局 route_index.yaml '
            '缺本域这条目 → {"kind": "index", "mode": "edit", '
            '"target": {"kind": "index", "cap": "能力包"}, '
            '"content": "本域条目全文（kb 路径带域前缀，如 web/webapp/…/手册.md）", "summary": "…", '
            '"reason": "…"}\n'
            '③ case 成功链沉淀（K6）：本任务整条 verified 攻击链/跑通 payload 值得复用 →'
            ' {"kind": "case", "mode": "edit"（对应测试包 成功案例.md 补「已验证路径」段）'
            ' 或 "create"（payloads/ 下新建弹药文件 .md/.py/.txt/.json）, '
            '"target": {"kind": "case", "cap": "能力包", "path": "测试包内相对路径"}, '
            '"content": "文件全文", "summary": "…", "reason": "…"}\n'
            f"cap 只能取本会话能力包之一（{caps}）；kb 新经验补对应测试包手册的"
            "「已验证路径」「坑」段或写成 payloads/ 弹药；英文快照原文不覆盖不翻译。"
            "无则只输出 NONE。")
        try:
            resp = self.planner_llm.chat(
                [{"role": "user", "content":
                  f"任务: {task_id}\n结果: {result_note[:500]}\n"
                  f"最近事件摘要: {digest}\n\n{instruction}"}],
                system="你是经验沉淀复盘员：只提炼本任务中验证过的事实，"
                       "不臆测、不复述常识。")
            self._record_usage(resp, source="planner", llm_obj=self.planner_llm)
            text = (resp.text or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("done 复盘调用失败（跳过沉淀）: %s", e)
            return
        if not text or text.upper().startswith("NONE"):
            return
        text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text,
                      flags=re.MULTILINE).strip()
        try:
            payload = json.loads(text)
            tgt = payload.get("target") or {}
            # K4：复盘沉淀允许 kb / index / case 三类去向（index 增补走 edit；其余一律按 kb 处理）
            kind = payload.get("kind") or tgt.get("kind") or "kb"
            if kind not in {"kb", "index", "case"}:
                kind = "kb"
            payload["kind"] = kind
            tgt["kind"] = kind
            payload["target"] = tgt
            payload["project"] = self.project_id
            payload["session"] = self.session["id"]
            payload["task"] = task_id
            payload["evidence"] = f"任务 {task_id}（done 自动复盘沉淀）"
            p = proposals.create_proposal(self.packs_root, payload, origin="agent")
        except Exception as e:  # noqa: BLE001  # JSONDecodeError/ProposalError 均静默
            log.warning("done 自动提案未落地（跳过）: %s", e)
            return
        self.bb.append_event(
            self.project_id, "proposal.created",
            {"id": p["id"], "kind": p["target"].get("kind"),
             "mode": p["mode"], "target": p["target"], "summary": p["summary"],
             "origin": "sediment"},
            session_id=self.session["id"], author=self.session["id"])

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

    def _maybe_summarize(self, messages: list[dict[str, Any]], *,
                         keep_recent: int = 8) -> bool:
        """G3 结构化摘要压缩（2026-09-19）：历史超过 context_summary_chars 时，
        把旧消息经 LLM 按九要素骨架压成一段摘要替换（近 keep_recent 条逐字保留；
        tool_use/tool_result 配对不拆——切割点回退到非 tool_result 消息）。
        就地生效（messages[:] = …，_live_state/快照引用同一列表不受影响）。
        摘要失败静默返回 False（机械 _trim 仍是硬上限兜底）。"""
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        threshold = min(self.config.context_summary_chars,
                        self.config.context_char_budget)
        if total <= threshold or len(messages) <= keep_recent + 2:
            return False
        cut = len(messages) - keep_recent
        while cut > 0 and _is_tool_result(messages[cut]):
            cut -= 1  # 边界不得落在 tool_result 上（防拆散 tool_use/result 配对）
        old = messages[:cut]
        if len(old) < 4:
            return False  # 没多少可压的，不值得一次 LLM 调用
        # H1 剪枝先行（借鉴 dsh pruning-before-summary）：旧区间超长 tool_result
        # 先机械剪枝（head 400 + tail 200 + 精确省略计数），常可不动用 LLM 就回到
        # 触发线下——摘要有损不可逆，剪枝只是收紧预览，二者正交。
        pruned = 0
        for m in old:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    c = str(b.get("content", ""))
                    if len(c) > 1000:
                        kept, om = retain(c, head=400, tail=200)
                        if om:
                            b["content"] = kept + omitted_note(om)
                            pruned += 1
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        if total <= threshold:
            self.bb.append_event(
                self.project_id, "llm.compact",
                {"before_msgs": len(messages), "after_msgs": len(messages),
                 "summarized": 0, "pruned": pruned, "chars_before": total,
                 "chars_after": total},
                session_id=self.session["id"], author=self.session["id"])
            return True
        lines: list[str] = []
        for m in old:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        lines.append(f"[{role}] {b.get('text', '')}")
                    elif b.get("type") == "tool_result":
                        lines.append(f"[工具结果] {str(b.get('content', ''))[:300]}")
                    elif b.get("type") == "tool_use":
                        args = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
                        lines.append(f"[调用工具] {b.get('name')}({args})")
            else:
                lines.append(f"[{role}] {content}")
        prompt = (
            "请把以下 Agent 执行历史压缩成摘要，九个要素各一小节（无内容的省略）：\n"
            "1. 任务目标与用户意图（用户引导/人类原话**逐字保留**）\n"
            "2. 关键技术概念与目标信息\n"
            "3. 涉及的黑板对象（资产/发现/任务 id 列表，只引用 id）\n"
            "4. 已尝试的步骤与结果（含失败/死路方向——防重走）\n"
            "5. 遇到的错误与修复\n"
            "6. 问题解决过程要点\n"
            "7. 待办与未完成方向\n"
            "8. 当前进行到哪一步\n"
            "9. 建议的下一步\n\n"
            "待压缩历史：\n" + "\n".join(lines))
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}],
                                 system=SUMMARY_SYSTEM)
        except Exception:  # noqa: BLE001 —— 压缩失败不影响主循环
            log.exception("上下文摘要压缩失败（跳过本次）")
            return False
        self._record_usage(resp, source="agent", llm_obj=self.llm)
        text = "".join(b.get("text", "") for b in resp.raw.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text").strip()
        if not text:
            return False
        head = (f"[历史摘要（原 {len(old)} 条消息由系统压缩；黑板对象只存 id，"
                f"细节用 bb_query/kb_open 现查）]\n")
        replaced = [{"role": "user", "content": head + text},
                    {"role": "assistant", "content": "已读摘要，从上一步继续。"}]
        before_msgs = len(messages)
        messages[:] = replaced + messages[cut:]
        # 压完仍超触发线（近 8 条里的大 tool_result 原样保留压不下去）→ 对保留段
        # 超长 tool_result 机械截断兜底（2000 一道、仍超再 200 一道；生产阈值 60k
        # 量级下必回线下，摘要文本/tool_use 块等结构性内容为不可压下限）。
        # 不截就会下一步立即再触发压缩，形成「压一次烧一步 LLM、细节越压越少」的
        # 死循环（实测 5 分钟连压 4 次，chars_after 63k-99k 全部高于 60k 触发线）。
        for cap in (2000, 200):
            total_after = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
            if total_after <= threshold:
                break
            for m in messages:
                if m.get("role") == "user" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_result" \
                                and len(str(b.get("content", ""))) > cap:
                            b["content"] = str(b["content"])[:cap] + "…[已截断]"
        self.bb.append_event(
            self.project_id, "llm.compact",
            {"before_msgs": before_msgs, "after_msgs": len(messages),
             "summarized": len(old), "chars_before": total,
             "chars_after": sum(len(json.dumps(m, ensure_ascii=False))
                                for m in messages)},
            session_id=self.session["id"], author=self.session["id"])
        return True

    def _salvage_attempt(self, reason: str) -> None:
        """fail 路径抢救收尾（2026-09-20，借鉴 Intentest 降级段）：worker 异常兜底
        fail 前，用一轮**无工具纯文本** LLM 把现场尾部的部分结论落盘为 salvage
        产物——任务虽败，已验证事实/失败方向不陪葬，接手者免重走。
        全程 try/except 全吞：抢救失败绝不影响 fail 本身；人工中止（即点即停，
        _abort_current_task）与 budget pause 不走这里。"""
        ctx = self._salvage_ctx
        self._salvage_ctx = None
        if not ctx:
            return
        messages = ctx.get("messages") or []
        if len(messages) < 4:
            return  # 现场太薄，没什么可抢救
        task_id = self.dispatcher.current_task_id
        lines: list[str] = []
        for m in messages[-14:]:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        lines.append(f"[{role}] {b.get('text', '')}")
                    elif b.get("type") == "tool_result":
                        lines.append(f"[工具结果] {str(b.get('content', ''))[:200]}")
                    elif b.get("type") == "tool_use":
                        args = json.dumps(b.get("input", {}), ensure_ascii=False)[:200]
                        lines.append(f"[调用工具] {b.get('name')}({args})")
            else:
                lines.append(f"[{role}] {content}")
        prompt = (
            f"任务「{ctx.get('objective', '')}」因异常即将被终止（原因：{reason}）。"
            "以下是执行历史的尾部。请抢救目前已获得的部分结论，只输出 Markdown 正文，"
            "分三节：## 已确认事实（只写已验证结论，黑板对象引用 id）\n"
            "## 失败/死路方向（避免接手者重走）\n## 接手建议（下一步从哪里续）。\n\n"
            "执行历史尾部：\n" + "\n".join(lines))
        try:
            resp = self.llm.chat([{"role": "user", "content": prompt}],
                                 system=SALVAGE_SYSTEM)
        except Exception:  # noqa: BLE001 —— 抢救 LLM 也炸（如配额故障）不影响 fail
            log.exception("salvage 抢救 LLM 调用失败（跳过）task=%s", task_id)
            return
        try:
            self._record_usage(resp, source="agent", llm_obj=self.llm)
            text = "".join(b.get("text", "") for b in resp.raw.get("content", [])
                           if isinstance(b, dict) and b.get("type") == "text").strip()
            if not text or not self.artifacts_dir:
                return
            out_dir = Path(self.artifacts_dir) / "salvage"
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = task_id or self.session["id"]
            path = out_dir / f"salvage-{stem}.md"
            n = 2
            while path.exists():
                path = out_dir / f"salvage-{stem}-{n}.md"
                n += 1
            path.write_text(
                f"# 抢救收尾：{ctx.get('objective', '')}\n\n终止原因：{reason}\n\n{text}",
                encoding="utf-8")
            rel = f"salvage/{path.name}"
            meta = {"session_id": self.session["id"]}
            if task_id:
                meta["task_id"] = task_id
            artifact_id = self.bb.add_artifact(
                self.project_id, rel, kind="salvage",
                description=f"异常终止前的部分结论抢救（{reason[:120]}）",
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                author=self.session["id"], meta=meta)
            self.bb.append_event(
                self.project_id, "task.salvaged",
                {"task_id": task_id, "artifact_id": artifact_id, "path": rel,
                 "reason": reason[:200]},
                session_id=self.session["id"], author=self.session["id"])
            log.info("salvage 抢救落盘 task=%s artifact=%s", task_id, artifact_id)
        except Exception:  # noqa: BLE001
            log.exception("salvage 抢救落盘失败（不影响 fail）task=%s", task_id)

    def _fail_task_on_error(self, exc: BaseException) -> None:
        """Worker 异常兜底：llm.chat 传输层重试耗尽 / 未预期异常穿出 _loop 时，
        任务绝不能悬在 claimed——① 停心跳（否则孤儿心跳线程继续每 10 分钟续租，
        租约永不过期、expire_leases 不回收，任务在本进程内永久卡「执行中」）；
        ② salvage 抢救收尾（2026-09-20）：把现场里的部分结论落盘为产物再 fail；
        ③ 任务 fail(error) 落审计（看板出失败卡可人工放回）；④ 清 current_task_id。
        幂等：current_task_id 已清时只停心跳。"""
        self._stop_heartbeat()
        task_id = self.dispatcher.current_task_id
        if task_id:
            note = f"worker 异常退出（{type(exc).__name__}: {exc}）"[:400]
            self._salvage_attempt(note)
            try:
                self.tq.fail(task_id, self.session["id"], note,
                             persona_role=self.dispatcher.current_persona_role)
            except Exception:  # noqa: BLE001
                log.exception("异常兜底 fail 任务失败 task=%s", task_id)
            self.dispatcher.current_task_id = None
        self._restore_base_persona()  # v14：异常兜底恢复底色（幂等）
        self.dispatcher._close_browser_session()  # F6-v3：任务结束清浏览器 Page

    def _finalize(self) -> None:
        """会话收尾：任务未收尾 → fail（防 lease 占坑）；落 session.finished 事件。"""
        self._salvage_ctx = None  # 会话收尾不做抢救（_fail_task_on_error 专属）
        self._stop_heartbeat()
        if self.dispatcher.current_task_id:
            try:
                self.tq.fail(self.dispatcher.current_task_id, self.session["id"],
                             "会话结束但任务未收尾，自动标记失败",
                             persona_role=self.dispatcher.current_persona_role)
            except Exception:  # noqa: BLE001
                log.exception("自动 fail 任务失败")
        self._restore_base_persona()  # v14：会话结束恢复底色（防状态残留）
        self.dispatcher._close_browser_session()  # F6-v3：会话结束清浏览器 Page
        self.bb.append_event(
            self.project_id, "session.finished",
            {"session_id": self.session["id"], "summary": self.dispatcher.summary[:500]},
            session_id=self.session["id"], author=self.session["id"],
        )
