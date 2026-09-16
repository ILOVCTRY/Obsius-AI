"""Agent 工具面（DESIGN.md §3）。

Agent 没有裸 shell：run_cmd 经 gateway；黑板读写走 Blackboard；
任务认领/收尾走 TaskQueue。所有工具结果以文本回填（tool_result）。
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from core.blackboard import Blackboard, ClaimError, TaskQueue
from core.blackboard.assets import register_asset
from core.blackboard.tasks import dedup_fp
from core.runtime.gateway import ExecutionGateway, GatewayDenied
from core.skills import proposals
from core.skills.proposals import ProposalError
from core.skills.rules import load_kb_sources

# 收尾协议工具不在角色 tools 白名单管控内（它们是循环控制原语，不是能力）。
# request_steps（E8）同属控制原语：预算自助增补与收尾决策一样必须永远可达。
_CONTROL_TOOLS = {"complete_task", "fail_task", "finish", "request_steps"}

# 计划原语同样恒放行：任何角色认领任务后都必须能写/推进计划（A2 先规划后动手）
_PLAN_TOOLS = {"task_plan", "task_step"}

# 计划闸（A2）：认领后计划为空时，这些只读/规划类工具可先调，其余一律引导先 task_plan。
# task_step 放行是为了让修订/纠正类回填不被自己的闸挡住（空计划下它会被服务端正常拒）。
_PLAN_PRE_ALLOWED = _PLAN_TOOLS | {
    "bb_query", "kb_open", "list_symbols", "decompile",
}

# 运行时等级（DESIGN.md §7）；角色 max_runtime = 允许的最高等级，只可能比网关策略更严
RUNTIME_RANK = {"host": 0, "wsl": 1, "docker": 2, "sandbox": 3}

# 地址入参：hex 串（"0x401000"）/ 十进制串 / int 皆收，内部统一 int。
# 与 API 的 FuncCreateIn 同语义：int(str, 0)，0x 前缀走 hex、裸数字走十进制。
_ADDR_DESC = "函数地址，hex 字符串优先（如 \"0x401000\"）；十进制整数也收"


def _coerce_addr(value: Any) -> int:
    if isinstance(value, bool):  # bool 是 int 子类，显式拦住
        raise ValueError(f"非法地址: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip(), 0)
    raise ValueError(f"非法地址: {value!r}")

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "run_cmd",
        "description": "执行命令。runtime 按样本信任级别选择：host=静态分析/自有脚本；"
                       "wsl=半可信工具；docker=不可信代码；sandbox=恶意样本（唯一选择，"
                       "样本威胁等级不得虚报为 trusted）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的命令"},
                "runtime": {"type": "string", "enum": ["host", "wsl", "docker", "sandbox"]},
                "threat_class": {"type": "string", "enum": ["trusted", "untrusted", "malware_live"]},
                "net": {"type": "string", "enum": ["none", "bridge", "real"],
                        "description": "仅 sandbox/docker 有效；real 需审批 id"},
                "approval_id": {"type": "string"},
                "timeout": {"type": "number", "description": "超时秒数（可选）"},
            },
            "required": ["cmd", "runtime", "threat_class"],
        },
    },
    {
        "name": "bb_add_asset",
        "description": "登记资产（host/domain/service/url/binary），人工与 Agent 同一入口。"
                       "type 省略=按值自动识别（url/IPv4/host:port/完整域名/64hex，"
                       "识别不出拒收回填）。资产树：url/service 值里含 IP 自动建 host 并"
                       "挂载；domain 由平台自动 DNS 解析挂 host（解析失败独立成行），"
                       "同 IP 多域名自动标主域名/别名。重报同值资产 = 合并 meta 不插重复行"
                       "（回执会提示先 bb_query 查重——勿对同一目标重复登记、重复扫描）。"
                       "meta.title=一句话简述（资产页展示）；meta.owner=平台标签"
                       "（.edu.cn 系→edusrc；品牌/平台标注→对应 tag；无归属不打）——"
                       "命中的平台规则会注入会话。扫描/测试状态走 bb_asset_status"
                       "（访问≠测试），不要再写 meta.scanned。",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string",
                         "description": "host/domain/service/url/binary；省略或 auto=自动识别"},
                "value": {"type": "string"},
                "meta": {"type": "object",
                         "description": "title=一句话简述；owner=平台标签"},
                "parent_id": {"type": "string",
                              "description": "父资产 id（url/service 的 IP 主机部自动挂载，"
                                             "domain 自动 DNS；显式传可跳过自动逻辑）"},
            },
            "required": ["value"],
        },
    },
    {
        "name": "bb_asset_status",
        "description": "流转资产扫描/测试状态（白名单四态：open→visited→scanning→tested_clean）。"
                       "硬纪律：**访问≠测试**——访问过标 visited、开始扫描标 scanning、"
                       "测完且无发现才允许 tested_clean；tested_clean 必须带 note"
                       "（测了什么/怎么测，服务端强制，缺 note 拒收）。测出问题直接 "
                       "bb_add_finding（verified 发现由前端反查显「有发现」徽章，"
                       "不要自报状态），每次流转落 asset.status_changed 审计。",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["visited", "scanning", "tested_clean", "open"]},
                "note": {"type": "string",
                         "description": "tested_clean 必填：测了什么/怎么测"},
            },
            "required": ["asset_id", "status"],
        },
    },
    {
        "name": "bb_add_finding",
        "description": "登记发现。无证据时 status 必须为 unverified（红线：无证据不下结论）；"
                       "status=verified 必须已稳定复现（连续 3 次触发），evidence.poc 按"
                       "{type: http_raw|python|steps, http_raw?, artifact_id?, target, "
                       "stability: '3/3'} 约定落（§5.2）。"
                       "CTF 轨语义：severity 字段填线索级别——critical=关键突破（直接导向 flag/"
                       "大幅推进）、high=有效线索（可行动）、low/medium/info=背景信息（记录备查）；"
                       "vuln_class 填线索类别（信息点/隐写疑似/编码疑似/flag 候选…）；"
                       "status=false-positive=死路（已排除的方向，evidence 必写清原因与已尝试清单，"
                       "防重走弯路）。解题脚本/writeup 用 bb_add_artifact 落产物，不要写裸文件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "vuln_class": {"type": "string"},
                "title": {"type": "string"},
                "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
                "status": {"type": "string", "enum": ["unverified", "verified", "false-positive"]},
                "target_asset_id": {"type": "string",
                                    "description": "挂到目标资产 id（URL 先 bb_add_asset 拿 id）"},
                "evidence": {"type": "object", "description": "证据：请求响应摘要等；"
                           "verified 时必带 evidence.poc（复现方法，§5.2 约定）"},
                "relates_to": {
                    "type": "array",
                    "description": "强相关发现（攻击链强边）：[{finding_id, note}]，"
                                   "note 必填关联理由（如「同一注入点升级到 DBA」）；"
                                   "被引用发现必须已登记在本项目，否则 422；弱相关不要填",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_id": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["finding_id", "note"],
                    },
                },
                "poc_artifact_id": {"type": "string",
                                    "description": "Python 脚本 POC 的产物 id（bb_add_artifact 返回）"},
                "confidence": {"type": "number"},
                "dedup_key": {"type": "string"},
            },
            "required": ["vuln_class", "title"],
        },
    },
    {
        "name": "bb_add_artifact",
        "description": "落产物文件（POC 脚本 / 抓包 / 输出）：写项目产物目录 + sha256 落库。"
                       "Python 复现脚本用 kind='poc'（**仅限 .py**，其他语言拒绝）——"
                       "报文打不稳的漏洞才用脚本复现（§5.2）。"
                       "CTF 纪律：解题/复现脚本与 writeup **必须落 artifact**（复现必要的产物"
                       "才落，附一句『是什么/怎么得到』）——不要写裸文件，队友与续跑会话"
                       "靠黑板找现场。",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "文件名（纯文件名，不含路径；kind=poc 时必须 .py）"},
                "content": {"type": "string", "description": "文件全文"},
                "kind": {"type": "string", "enum": ["file", "poc", "capture"]},
                "description": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "bb_query",
        "description": "查黑板：findings / assets / events / tasks / func（函数知识库）。"
                       "逆向场景硬规则：反编译任何函数前必须先查 func 防重复劳动。",
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "enum": ["findings", "assets", "events", "tasks", "func"]},
                "target_asset_id": {"type": "string"},
                "binary_sha256": {"type": "string", "description": "func 查询必填"},
                "address": {"type": ["integer", "string"],
                            "description": f"func 单点查询。{_ADDR_DESC}"},
                "risk_tag": {"type": "string", "description": "func 按风险标签过滤"},
                "type": {"type": "string",
                         "description": "assets 按类型过滤（host/domain/service/url/binary）"},
                "status": {"type": "string",
                           "description": "assets 按扫描/测试状态过滤"
                                          "（open/visited/scanning/tested_clean）——"
                                          "并发会话可借此感知哪些目标正被扫"},
            },
            "required": ["what"],
        },
    },
    {
        "name": "kb_open",
        "description": "打开知识库模块（能力包 kb/ 快照区，如 ctf-skills、src-strike）："
                       "返回模块文件的绝对路径 + 阅读纪律，随后你用文件读取能力按需 Read。"
                       "技能正文给出特征→模块相对路径对照，命中后开对应模块，不预读；"
                       "路径不存在会返回全部可用模块清单（照清单改选，禁止猜文件名、"
                       "禁止 .. 穿越）。禁止通读知识库目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {"type": "string",
                           "description": "知识库内相对路径（带快照名前缀），如 "
                                          "ctf-web/auth-jwt.md、"
                                          "src-strike/知识库/idor-test.md、"
                                          "src-strike/references/playbooks/sqli.md"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "propose_pack_edit",
        "description": "提一条知识库/技能变更**提案**（只落 pending，批准权在人类，"
                       "禁止自行改文件）。仅限三种情形：文档互相矛盾、文档缺失、"
                       "某手法已在本任务验证有效；reason 必须附任务证据。每会话最多 3 条。"
                       "kind=skill 时 mode 只允许 edit 且 target={skill_kind: capability|track,"
                       " owner: 包/轨名, name: 技能名}；kind=kb 时 mode 可为 "
                       "edit/create/rename/delete，target={cap: 能力包名, path: 源内相对 "
                       ".md 路径（带快照名前缀，中文目录允许）, new_path: 仅 rename}；"
                       "edit/create 必带 content 全文；英文快照原文不翻译，新经验写新 md。",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["kb", "skill"]},
                "mode": {"type": "string",
                         "enum": ["edit", "create", "rename", "delete"]},
                "target": {"type": "object",
                           "description": "kb: {cap,path,new_path?}；"
                                          "skill: {skill_kind,owner,name}"},
                "content": {"type": "string",
                            "description": "edit/create 的文件全文（其余模式不传）"},
                "summary": {"type": "string", "description": "一句话摘要（≤300 字）"},
                "reason": {"type": "string",
                           "description": "为什么改 + 本任务证据（任务 id/关键观察）"},
            },
            "required": ["kind", "mode", "target", "summary", "reason"],
        },
    },
    {
        "name": "bb_upsert_func",
        "description": "写函数知识库（逆向防重复劳动）：同地址重复写入自动合并"
                       "（名称入演变史、analysis 追加、confidence 取最大）。"
                       "分析完一个函数就落一条，别的会话才能看见。",
        "input_schema": {
            "type": "object",
            "properties": {
                "binary_sha256": {"type": "string"},
                "address": {"type": ["integer", "string"], "description": _ADDR_DESC},
                "name": {"type": "string", "description": "你给函数起的名（含语义，如 check_flag）"},
                "analysis": {"type": "string", "description": "行为/算法结论"},
                "risk_tags": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
            },
            "required": ["binary_sha256", "address", "name"],
        },
    },
    {
        "name": "decompile",
        "description": "反编译（Ghidra/IDA/MCP 自动选路，结果缓存）。"
                       "硬规则：若该地址在 func_kb 已有分析结论，本工具直接返回缓存结论"
                       "（禁止重复反编译）；拿到伪码后必须 bb_upsert_func 落库。",
        "input_schema": {
            "type": "object",
            "properties": {
                "binary": {"type": "string", "description": "二进制文件路径"},
                "address": {"type": ["integer", "string"],
                            "description": f"函数入口地址；缺省=全量概览。{_ADDR_DESC}"},
                "name": {"type": "string", "description": "按符号名匹配"},
            },
            "required": ["binary"],
        },
    },
    {
        "name": "list_symbols",
        "description": "列出二进制函数符号表（地址/名字/大小/是否已导出伪码）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "binary": {"type": "string"},
            },
            "required": ["binary"],
        },
    },
    {
        "name": "task_plan",
        "description": "写下/修订当前任务的解决计划。**认领任务后、任何实质动作（run_cmd/"
                       "bb_add_* 等）之前必须先调一次**：3-8 个可验证的小步，按执行顺序。"
                       "执行中情况变化可再调修订：想保留进度的步带上原 id（状态/时间戳"
                       "保留，标题可改），新步不带 id 由服务端发号，不再需要的步直接省略；"
                       "rev_reason 写修订原因。任意时刻至多一个 doing——用 task_step 切换。",
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string",
                                   "description": "修订时填既有步 id（如 p1）以保留其状态；新步不填"},
                            "title": {"type": "string", "description": "这一步要做什么、怎么算做完"},
                        },
                        "required": ["title"],
                    },
                },
                "rev_reason": {"type": "string", "description": "修订原因（首次写不用填）"},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "task_step",
        "description": "推进计划步：开始一步置 doing（原 doing 自动回 todo，保证至多一个 "
                       "doing）；做完置 done；被阻塞置 blocked（note 必填阻塞原因与所需"
                       "外部条件）。step_id 用 task_plan 返回的 p1/p2…。",
        "input_schema": {
            "type": "object",
            "properties": {
                "step_id": {"type": "string"},
                "status": {"type": "string", "enum": ["todo", "doing", "done", "blocked"]},
                "note": {"type": "string", "description": "blocked 时必填：阻塞原因；其他状态可选备注"},
            },
            "required": ["step_id", "status"],
        },
    },
    {
        "name": "publish_task",
        "description": "把当前任务**分解**出一个子任务，派给其他会话/Worker 认领执行（分析-分解-分派）。"
                       "新任务自动挂当前任务为 parent（任务流图上的分解实线），created_by 为本会话，"
                       "无需也不能手填；没有认领任务时 parent 为空。规则同人类发任务："
                       "非 passive 必须给 conflict_keys（同目标 active 互斥，键会被服务端归一化校验）；"
                       "refs 可挂依据发现 id；priority 0-9（小者优先，默认 2）；task_type 必须是本轨 "
                       "task_types.yaml 已注册类型（generic 恒合法）。与既有 open/claimed 同目标任务"
                       "重复发布会被拒绝（返回 [复用] 与既有任务 id）——发布前先 bb_query 查任务。"
                       "分解是计划的一部分：先 task_plan 再发子任务。",
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "子任务目标（自足可执行：执行者看不到你的上下文）"},
                "task_type": {"type": "string", "description": "本轨注册表内的类型；缺省 generic"},
                "scope": {"type": "string", "description": "边界/授权范围（可选）"},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "依据发现 id（find- 前缀，可选）；正文里的 find- id 也会自动抽取"},
                "noise_budget": {"type": "string", "enum": ["passive", "low", "medium", "high"]},
                "priority": {"type": "integer", "description": "0-9，小者优先"},
                "conflict_keys": {"type": "array", "items": {"type": "string"},
                                  "description": "非 passive 必填（如 [\"ip:1.2.3.4\"]）；服务端归一化，非法拒收"},
                "workset": {"type": "array", "items": {"type": "string"},
                            "description": "工作集软声明（机制 1.1，可选）：正在分析的目标（如 0x401000 / url），供他人避让，不阻塞认领"},
            },
            "required": ["objective"],
        },
    },
    {
        "name": "complete_task",
        "description": "完成当前认领的任务（附结果摘要）。",
        "input_schema": {
            "type": "object",
            "properties": {"result_note": {"type": "string"}},
            "required": ["result_note"],
        },
    },
    {
        "name": "fail_task",
        "description": "当前任务失败收尾（附原因）。blocked_reason=error（缺省）＝真失败；"
                       "blocked_reason=awaiting_human＝卡在等待人类输入/决策（如 ROE 未核验、"
                       "缺授权凭据、需要人类在两个方案间拍板）——note 必须写清**需要人类做什么**，"
                       "现场快照会保留，人类处理后可从断点续跑，上下文不丢。宁严勿松："
                       "不确定就 error，awaiting_human 只用于确实需要人类才能继续的场景。",
        "input_schema": {
            "type": "object",
            "properties": {
                "result_note": {"type": "string",
                                 "description": "awaiting_human 时必写清需要人类做什么"},
                "blocked_reason": {"type": "string", "enum": ["error", "awaiting_human"],
                                    "description": "缺省 error"},
            },
            "required": ["result_note"],
        },
    },
    {
        "name": "finish",
        "description": "结束本次会话（提交最终总结）。任务未收尾时会被自动标记失败。",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
    {
        "name": "request_steps",
        "description": "申请步数预算增补（一次固定 +200）。仅当剩余步数 ≤20 时放行，"
                       "剩余充足时会被拒收（防未雨绸缪囤步数）。步数耗尽会话会自动"
                       "暂停等人类恢复，所以预算吃紧时请主动申请并说明理由。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "为何还需要更多步数（一句话，落审计）"},
            },
        },
    },
]


class ToolDispatcher:
    """工具分发中枢。持会话状态：当前任务、进度心跳（卡死检测的依据）。"""

    def __init__(self, bb: Blackboard, gateway: ExecutionGateway, tq: TaskQueue,
                 *, project_id: str, session_id: str, author: str,
                 decompiler=None, artifacts_dir=None,
                 packs_root: str | Path | None = None,
                 track: str | None = None,
                 capabilities: list[str] | None = None,
                 allowed_tools: list[str] | None = None,
                 max_runtime: str | None = None,
                 allowed_task_types: Iterable[str] | None = None,
                 max_steps: int = 0):
        self.bb = bb
        self.gateway = gateway
        self.tq = tq
        self.project_id = project_id
        self.session_id = session_id
        self.author = author
        self.decompiler = decompiler  # DecompilerService，缺省=未装配
        self.artifacts_dir = artifacts_dir  # 产物目录（bb_add_artifact 用），缺省=未装配
        self.packs_root = packs_root    # kb_open 解析 kb_sources 用，缺省=未装配
        self.track = track
        self.capabilities = capabilities or []
        # 角色 tools 白名单（§6.6 软边界）：None/空=不限；收尾协议工具永远放行
        self.allowed_tools = allowed_tools or None
        # 角色 max_runtime 运行时等级软上限（host/wsl/docker/sandbox，§6.6）：None=不限
        self.max_runtime = max_runtime if max_runtime in RUNTIME_RANK else None
        # 轨 task_types.yaml 注册表（A5 子代理发任务的类型护栏）：None=未接线不校验
        self.allowed_task_types = allowed_task_types
        # 会话步数预算（E8）：AgentSession 按角色收敛后的 max_steps 注入；request_steps
        # 增补写这里（_loop 的 range 上界同读），0 = 未装配（request_steps 拒收）
        self.max_steps = max_steps
        self.current_task_id: str | None = None
        self._step = 0
        self.last_progress_step = 0   # 最近一次实质进展的步号（卡死检测用）
        self.finished = False
        self.awaiting_human = False  # C1：fail_task(awaiting_human) 置位，_loop 收尾时落快照+fail
        self.summary = ""

    def set_step(self, n: int) -> None:
        self._step = n

    @property
    def step(self) -> int:
        """当前步号（E8：预算耗尽暂停时作恢复断点）。"""
        return self._step

    # ---------- 分发 ----------

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"[错误] 未知工具: {name}"
        if self.allowed_tools is not None and name not in self.allowed_tools \
                and name not in _CONTROL_TOOLS and name not in _PLAN_TOOLS:
            # 角色软边界（§6.6）：白名单外工具不执行；阶段 4 起可走 request_escalation
            # 申请一次性授权，当前由人类调整角色配置。
            return (f"[越界拒绝] 工具 {name} 不在本角色工具白名单内"
                    f"（允许: {', '.join(self.allowed_tools)}）。停止该方向或请人类调整角色配置。")
        if self.current_task_id and name not in _PLAN_PRE_ALLOWED \
                and name not in _CONTROL_TOOLS:
            # A2 先规划后动手：认领后计划为空时，实质工具一律引导先 task_plan。
            # 每次调度现查（修订/暂停恢复后状态以黑板为准），开销可忽略。
            task = self.tq.get_task(self.current_task_id)
            if task is not None and task.get("status") == "claimed" and not task.get("plan"):
                return ("[计划闸] 请先调 task_plan 写下本任务的解决计划（3-8 个可验证小步），"
                        "再开始实质动作。只读侦察（bb_query/kb_open/list_symbols/decompile）"
                        "允许先行，但 run_cmd、写黑板等须在计划之后。")
        try:
            return handler(**args)
        except GatewayDenied as e:
            # 拒绝不是异常终止：Agent 看到原因后改道（§7 拒绝必须改道）
            return f"[网关拒绝] {e}"
        except Exception as e:  # noqa: BLE001 —— 工具失败回填文本，循环不中断
            return f"[工具异常] {type(e).__name__}: {e}"

    # ---------- 各工具实现 ----------

    def _tool_run_cmd(self, cmd: str, runtime: str, threat_class: str,
                      net: str | None = None, approval_id: str | None = None,
                      timeout: float | None = None) -> str:
        # 角色 max_runtime 运行时等级软上限（§6.6）：只可能比网关 threat_class
        # 允许集更严，不可放松；阶段 4 起可 request_escalation(kind=runtime) 一次性授权
        if self.max_runtime is not None and runtime in RUNTIME_RANK \
                and RUNTIME_RANK[runtime] > RUNTIME_RANK[self.max_runtime]:
            return (f"[越界拒绝] runtime={runtime} 超过角色 max_runtime="
                    f"{self.max_runtime}（运行时软上限不可自行放松；"
                    "如确有必要请人类调整角色配置）")
        r = self.gateway.run(
            cmd, runtime, threat_class=threat_class,
            project_id=self.project_id, session_id=self.session_id,
            author=self.author, net=net, approval_id=approval_id,
            timeout=timeout,
        )
        return r.brief()

    def _tool_bb_add_asset(self, value: str, type: str = "auto",
                           meta: dict | None = None,
                           parent_id: str | None = None) -> str:
        # E6 统一登记入口：类型自动识别/去重合并/DNS 挂载/主域名标记全在
        # register_asset（人工 POST /assets 同路径）；非法类型 ValueError 回填
        try:
            r = register_asset(self.bb, self.project_id, value, type_=type,
                               parent_id=parent_id, meta=meta, author=self.author,
                               session_id=self.session_id)
        except ValueError as e:
            return f"[错误] {e}"
        self.last_progress_step = self._step
        reply = f"asset={r['id']} type={r['type']} value={r['value']} created={r['created']}"
        if r.get("host_id"):
            reply += f" host={r['host_id']}"
        # E6 ⑥ 防重扫：命中既有资产/既有 IP 时回执提示查重
        if not r["created"] or r.get("host_existed"):
            reply += ("\n[提示] 命中既有资产（同值或同 IP）：先 bb_query what=assets 查重，"
                      "不要对同一目标重复登记、重复扫描；确需补挂/补 meta 才复报。")
        return reply

    def _tool_bb_asset_status(self, asset_id: str, status: str,
                              note: str | None = None) -> str:
        a0 = self.bb.get_asset(asset_id)
        if a0 is None or a0.get("project_id") != self.project_id:
            return f"[错误] 资产不存在: {asset_id}"
        try:
            a = self.bb.set_asset_status(asset_id, status, note=note,
                                         author=self.author)
        except ValueError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return f"asset={asset_id} status={a['status']}"

    def _tool_bb_add_finding(self, vuln_class: str, title: str, severity: str = "info",
                             status: str = "unverified", evidence: dict | None = None,
                             relates_to: list | None = None,
                             target_asset_id: str | None = None,
                             poc_artifact_id: str | None = None,
                             confidence: float = 0.5, dedup_key: str | None = None) -> str:
        # relates_to 是顶层入参（LLM 不必懂 evidence 内部结构），并入 evidence 走
        # add_finding 的存在性/同项目校验（悬空/跨项目 ValueError → 回填错误不中断）
        ev = dict(evidence or {})
        if relates_to:
            ev["relates_to"] = relates_to
        r = self.bb.add_finding(
            self.project_id, vuln_class, title, severity=severity, status=status,
            evidence=ev, target_asset_id=target_asset_id,
            poc_artifact_id=poc_artifact_id,
            confidence=confidence, dedup_key=dedup_key, author=self.author,
        )
        self.last_progress_step = self._step
        return f"finding={r['id']} merged={r['merged']}"

    def _tool_bb_add_artifact(self, filename: str, content: str,
                              kind: str = "file", description: str = "") -> str:
        """落产物（§5.2）：写 <artifacts_dir>/<kind>/<filename> + sha256 落库。"""
        if not self.artifacts_dir:
            return "[错误] 会话未配置产物目录，无法落产物"
        # POC 脚本纪律（§5.2）：仅限 Python（.py），其他语言拒绝（语言统一才可审计可执行）
        if kind == "poc" and not filename.lower().endswith(".py"):
            return "[错误] kind=poc 仅限 Python 脚本（filename 必须 .py）；" \
                   "报文能稳触的漏洞直接把最小 HTTP 报文放 evidence.poc.http_raw"
        # 纯文件名：剥离路径分隔符防穿越；冲突加序号不覆盖
        safe = filename.replace("\\", "_").replace("/", "_").strip() or "artifact.bin"
        stem, dot, ext = safe.partition(".")
        suffix = f".{ext}" if dot else ""
        out_dir = Path(self.artifacts_dir) / kind
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / safe
        n = 2
        while path.exists():
            path = out_dir / f"{stem}-{n}{suffix}"
            n += 1
        path.write_text(content, encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        rel = f"{kind}/{path.name}"
        artifact_id = self.bb.add_artifact(self.project_id, rel, kind=kind,
                                           description=description, sha256=sha,
                                           author=self.author)
        self.bb.append_event(
            self.project_id, "artifact.new",
            {"artifact_id": artifact_id, "path": rel, "kind": kind, "sha256": sha},
            session_id=self.session_id, author=self.author)
        self.last_progress_step = self._step
        return f"artifact={artifact_id} path={rel} sha256={sha[:8]}"

    def _tool_bb_query(self, what: str, target_asset_id: str | None = None,
                       binary_sha256: str | None = None, address: int | str | None = None,
                       risk_tag: str | None = None, type: str | None = None,
                       status: str | None = None) -> str:
        if address is not None:
            try:
                address = _coerce_addr(address)
            except ValueError as e:
                return f"[错误] {e}"
        if what == "findings":
            rows = self.bb.list_findings(self.project_id, target_asset_id=target_asset_id)
            return json.dumps(
                [{"id": f["id"], "vuln_class": f["vuln_class"], "title": f["title"],
                  "severity": f["severity"], "status": f["status"], "confidence": f["confidence"]}
                 for f in rows], ensure_ascii=False)
        if what == "assets":
            rows = self.bb.list_assets(self.project_id, type_=type, status=status)
            return json.dumps(
                [{"id": a["id"], "type": a["type"], "value": a["value"],
                  "parent_id": a.get("parent_id"), "status": a.get("status", "open"),
                  "meta": a.get("meta", {})}
                 for a in rows], ensure_ascii=False)
        if what == "events":
            rows = self.bb.recent_events(self.project_id, limit=50)
            return json.dumps(
                [{"kind": e["kind"], "author": e["author"], "payload": e["payload"]}
                 for e in rows], ensure_ascii=False)
        if what == "tasks":
            rows = self.tq.list_tasks(self.project_id)
            return json.dumps(
                [{"id": t["id"], "objective": t["objective"], "status": t["status"],
                  "priority": t["priority"]} for t in rows], ensure_ascii=False)
        if what == "func":
            if not binary_sha256:
                return "[错误] func 查询必须提供 binary_sha256"
            if address is not None:
                row = self.bb.lookup_func(self.project_id, binary_sha256, address)
                if row is None:
                    return json.dumps({"known": False}, ensure_ascii=False)
                return json.dumps(
                    {"known": True, "name": row["name"], "analysis": row["analysis"],
                     "risk_tags": row["risk_tags"], "confidence": row["confidence"],
                     "analyzed_by": row["analyzed_by"]}, ensure_ascii=False)
            rows = (self.bb.list_funcs_by_risk(self.project_id, binary_sha256, risk_tag)
                    if risk_tag else self.bb.list_funcs(self.project_id, binary_sha256))
            return json.dumps(
                # 地址出 API/Agent 一律 hex 字符串（§9 地址纪律，JS/模型都不丢 64 位精度）
                [{"address": hex(int(f["address"])), "name": f["name"],
                  "risk_tags": f["risk_tags"],
                  "confidence": f["confidence"]} for f in rows], ensure_ascii=False)
        return f"[错误] 未知查询: {what}"

    def _tool_kb_open(self, module: str) -> str:
        """打开知识库模块（DESIGN.md §4/§4.5）：跨启用能力包的 kb_sources 多源解析。

        module = 源内相对路径（可含子目录）；resolve 后强制落在某源 root 内（防穿越）；
        递归可达由源的 recursive 标志控制；不存在则按源列出可选清单（防幻觉猜名）。
        注：阶段 2 再补 source 参数做同名消歧，当前按源顺序命中第一个。"""
        if not self.packs_root:
            return "[错误] 本会话未配置知识库（packs_root 缺失）"
        rel = module.strip().replace("\\", "/")
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            return "[拒绝] module 必须是知识库内相对路径（禁止绝对路径 / .. 穿越）"
        sources = load_kb_sources(self.packs_root, self.capabilities)
        if not sources:
            return "[错误] 启用的能力包未登记 kb_sources.json，本技能正文自足"
        for src in sources:
            target = (src.root / rel).resolve()
            if target != src.root and src.root not in target.parents:
                continue  # 防穿越：解析结果逃逸出源 root
            if not target.is_file():
                continue
            self.bb.append_event(
                self.project_id, "kb.open",
                {"source": src.id, "module": rel, "path": str(target)},
                session_id=self.session_id, author=self.author)
            self.last_progress_step = self._step
            return (f"知识库模块（源 {src.id}）: {target}\n"
                    "纪律：只 Read 上面这一个文件，按需取用；禁止通读知识库目录；"
                    "需要相关专题时按技能对照表另开对应文件（同样按需）。")
        lines = ["[防幻觉] 模块不存在: " + rel, "可用模块（照清单改选，禁止猜名）:"]
        for src in sources:
            if not src.root.is_dir():
                continue
            files = (src.root.rglob("*.md") if src.recursive
                     else src.root.glob("*.md"))
            # 排除备份/回收站（C3）：Agent 只能照清单选知识正文，防猜中 .history
            rels = sorted(str(f.relative_to(src.root)).replace("\\", "/")
                          for f in files
                          if ".history" not in f.relative_to(src.root).parts)[:50]
            lines.append(f"- 源 {src.id}: " + ("、".join(rels) if rels else "(空)"))
        return "\n".join(lines)

    # 每会话提案上限（DESIGN §4：防凑数/提案洪泛）
    PROPOSE_LIMIT_PER_SESSION = 3

    def _tool_propose_pack_edit(self, kind: str, mode: str, target: dict,
                                summary: str, reason: str,
                                content: str | None = None) -> str:
        """经验沉淀提案通道（C4）：只校验落 pending，绝不直接改文件；应用权在人类。"""
        if not self.packs_root:
            return "[错误] 本会话未配置 packs_root，无法提提案"
        pending = [p for p in proposals.list_proposals(self.packs_root, "pending")
                   if p.get("session") == self.session_id]
        if len(pending) >= self.PROPOSE_LIMIT_PER_SESSION:
            return (f"[拒绝] 每会话最多 {self.PROPOSE_LIMIT_PER_SESSION} 条提案"
                    f"（已有 {len(pending)} 条 pending："
                    f"{'、'.join(p['id'] for p in pending)}），等人类审批后再提")
        payload = {
            "target": {"kind": kind, **(target or {})},
            "mode": mode,
            "content": content,
            "summary": summary,
            "reason": reason,
            "project": self.project_id,
            "session": self.session_id,
            "task": self.current_task_id,
            "evidence": f"任务 {self.current_task_id or '(无任务)'}；"
                        f"reason 已附任务依据",
        }
        try:
            p = proposals.create_proposal(self.packs_root, payload, origin="agent")
        except ProposalError as e:
            return f"[拒绝] 提案非法，未落地: {e}"
        self.bb.append_event(
            self.project_id, "proposal.created",
            {"id": p["id"], "kind": kind, "mode": mode,
             "target": p["target"], "summary": p["summary"], "origin": "agent"},
            session_id=self.session_id, author=self.author)
        self.last_progress_step = self._step
        return (f"提案已落 pending（待人类审批）: {p['id']}\n"
                f"变更: {kind}/{mode} {p['target'].get('path') or p['target'].get('name')}\n"
                "注意：提案不会自动生效；人类批准前按现有文档继续工作。")

    def _tool_bb_upsert_func(self, binary_sha256: str, address: int | str, name: str,
                             analysis: str = "", risk_tags: list[str] | None = None,
                             confidence: float = 0.5) -> str:
        try:
            address = _coerce_addr(address)
        except ValueError as e:
            return f"[错误] {e}"
        r = self.bb.upsert_func(
            self.project_id, binary_sha256, address, name, analysis=analysis,
            risk_tags=risk_tags, confidence=confidence, analyzed_by=self.author)
        self.last_progress_step = self._step
        return f"func={r['id']} created={r['created']}"

    def _finish_or_report_deleted(self, finish, verb: str) -> str:
        """收尾任务；任务已被人类物理删除（§6.4）→ 友好提示，会话照常转向下一任务。"""
        tid = self.current_task_id
        try:
            finish(tid, self.session_id)
        except ValueError:
            if self.tq.get_task(tid) is not None:
                raise
            self.current_task_id = None
            self.last_progress_step = self._step
            return f"task={tid} 已被人类删除，无需收尾，继续认领下一个任务"
        self.current_task_id = None
        self.last_progress_step = self._step
        return f"task={tid} {verb}"

    # ---------- 计划（A2 先规划后动手） ----------

    @staticmethod
    def _render_plan(plan: list[dict[str, Any]]) -> str:
        icon = {"todo": "○", "doing": "▶", "done": "●", "blocked": "■"}
        return "\n".join(
            f"  {icon.get(s.get('status'), '?')} {s['id']} {s['title']}"
            + (f"（阻塞：{s['note']}）" if s.get("status") == "blocked" and s.get("note") else "")
            for s in plan
        )

    def _tool_task_plan(self, steps: list[dict[str, Any]],
                        rev_reason: str = "") -> str:
        if not self.current_task_id:
            return "[错误] 当前没有认领的任务，计划必须挂在认领任务上"
        try:
            plan = self.tq.set_plan(
                self.current_task_id, self.session_id, steps, rev_reason=rev_reason)
        except ClaimError as e:
            return f"[拒绝] {e}"
        done_n = sum(1 for s in plan if s.get("status") == "done")
        self.last_progress_step = self._step
        return (f"计划已{'修订' if rev_reason or any(s.get('status') != 'todo' for s in plan) else '记录'}，"
                f"共 {len(plan)} 步（已完成 {done_n}）。用 task_step 把开始的步置 doing、做完置 done：\n"
                + self._render_plan(plan))

    def _tool_task_step(self, step_id: str, status: str, note: str = "") -> str:
        if not self.current_task_id:
            return "[错误] 当前没有认领的任务"
        try:
            task = self.tq.step_plan(
                self.current_task_id, self.session_id, step_id, status, note)
        except ClaimError as e:
            return f"[拒绝] {e}"
        plan = task["plan"]
        if status in ("doing", "done"):
            self.last_progress_step = self._step
        return f"计划步 {step_id} → {status}。当前计划：\n" + self._render_plan(plan)

    # ---------- 分解派活（A5：子代理发布子任务） ----------

    def _tool_publish_task(self, objective: str, task_type: str = "generic",
                           scope: str = "", refs: list[str] | None = None,
                           noise_budget: str = "passive", priority: int = 2,
                           conflict_keys: list[str] | None = None,
                           workset: list[str] | None = None) -> str:
        """子代理分解子任务（A5）：parent 强制当前任务、created_by 强制本会话。

        入参里不接受 parent_id/created_by——分解关系由服务端按会话状态钉死，
        与撤回传播的 is_session 识别一致（created_by=sess-…）。
        机制 1.1：发布前按指纹查重，命中 open/claimed 同目标任务 → 复用不新建（防重复派活）。
        """
        objective = (objective or "").strip()
        if not objective:
            return "[错误] objective 不能为空"
        # 机制 1.1 发布去重：命中同指纹 open/claimed 任务 → 静默复用（编排/Agent 不重复派活）
        fp = dedup_fp(task_type, scope, objective)
        dup = self.tq.find_dedup_target(self.project_id, fp)
        if dup is not None:
            self.last_progress_step = self._step
            return (f"[复用] 已存在同目标任务 {dup['id']}（status={dup['status']}），"
                    f"本轮不重复发布；认领/避让请参考其 workset 与 conflict_keys")
        try:
            task_id = self.tq.publish(
                self.project_id, objective, scope=scope, task_type=task_type,
                noise_budget=noise_budget, priority=priority,
                conflict_keys=conflict_keys,
                parent_id=self.current_task_id,   # 无认领任务 → 顶层任务（parent 为空）
                created_by=self.session_id,
                allowed_types=self.allowed_task_types, refs=refs, workset=workset)
        except ValueError as e:  # TaskTypeError / 噪声冲突键 / 非法优先级 / 键归一化失败等
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        parent = self.current_task_id or "(无)"
        return (f"子任务已发布: {task_id}（parent={parent}，type={task_type}，"
                f"P{priority}，{noise_budget}）。其他会话可在认领队列看到并领取；"
                "你继续推进当前任务，不要自己抢领（当前任务收尾后若仍 open 才可认领）。")

    def _tool_complete_task(self, result_note: str) -> str:
        if not self.current_task_id:
            return "[错误] 当前没有认领的任务"
        return self._finish_or_report_deleted(
            lambda tid, sid: self.tq.complete(tid, sid, result_note), "已完成")

    def _tool_fail_task(self, result_note: str = "",
                        blocked_reason: str = "error") -> str:
        if not self.current_task_id:
            return "[错误] 当前没有认领的任务"
        if blocked_reason not in ("error", "awaiting_human"):
            return f"[拒绝] 非法 blocked_reason: {blocked_reason}"
        if blocked_reason == "awaiting_human":
            # C1：不在此处 fail——_loop 步边界检测 awaiting_human 后先落快照再
            # fail(blocked_reason=awaiting_human, resumable=True)，现场保留供
            # 「▶ 续跑」/「放回继续」；会话不结束，worker 继续认领下一个任务
            self.awaiting_human = True
            self.last_progress_step = self._step
            return ("任务已挂起等待人工输入（现场快照保留，人类处理后可从断点续跑）。"
                    "挂起在本步收尾生效；之后可继续认领其他任务。")
        return self._finish_or_report_deleted(
            lambda tid, sid: self.tq.fail(tid, sid, result_note), "已标记失败")

    def _tool_finish(self, summary: str) -> str:
        self.finished = True
        self.summary = summary
        return "会话即将结束"

    def _tool_request_steps(self, reason: str = "") -> str:
        """E8 自助加步：一次固定 +200；剩余 >20 拒收（防囤步数），落审计事件。"""
        if self.max_steps <= 0:
            return "[拒绝] 步数预算未装配，无法增补"
        remaining = self.max_steps - self._step
        if remaining > 20:
            return (f"[拒绝] 剩余 {remaining} 步 > 20，暂不允许增补"
                    "（防未雨绸缪囤步数；预算吃紧到 ≤20 步时再申请）")
        old = self.max_steps
        self.max_steps = old + 200
        self.bb.append_event(
            self.project_id, "step.budget_extended",
            {"session_id": self.session_id, "task_id": self.current_task_id,
             "old_max": old, "new_max": self.max_steps, "step": self._step,
             "remaining": remaining, "reason": (reason or "")[:200], "by": "agent"},
            session_id=self.session_id, author=self.author)
        return (f"步数预算已增补：{old} → {self.max_steps}。请继续规划收尾，"
                "优先完成当前任务再考虑新动作。")

    # ---------- 反编译组合服务（§9；func_kb 查重在这里机制级强制） ----------

    def _tool_decompile(self, binary: str, address: int | str | None = None,
                        name: str | None = None) -> str:
        if self.decompiler is None:
            return "[未装配] 本会话未接入反编译服务（DecompilerService），请用 run_cmd 手动静态分析"
        if address is not None:
            try:
                address = _coerce_addr(address)
            except ValueError as e:
                return f"[错误] {e}"
        sha = _file_sha(binary)
        if sha and address is not None:
            known = self.bb.lookup_func(self.project_id, sha, address)
            if known and known["analysis"]:
                self.last_progress_step = self._step
                return (f"[func_kb 命中，禁止重复反编译] {known['name']} @ {hex(address)}"
                        f"（by {known['analyzed_by']}，conf={known['confidence']}）\n"
                        f"{known['analysis']}\n"
                        f"需要更多细节可带 name 参数强制点查，或直接用此结论推进。")
        result = self.decompiler.decompile(binary, address=address, name=name)
        if not result.startswith("["):
            self.last_progress_step = self._step
            hint = (f"\n[硬规则] 若有结论请立即 bb_upsert_func（binary_sha256={sha[:16]}…，"
                    f"address={hex(address) if address else '见符号表'}）落库防重复劳动"
                    if sha else "")
            return result + hint
        return result

    def _tool_list_symbols(self, binary: str) -> str:
        if self.decompiler is None:
            return "[未装配] 本会话未接入反编译服务（DecompilerService）"
        result = self.decompiler.list_functions(binary)
        if not result.startswith("["):
            self.last_progress_step = self._step
        return result


def _file_sha(path: str) -> str | None:
    import hashlib
    from pathlib import Path
    try:
        h = hashlib.sha256()
        with open(Path(path), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None
