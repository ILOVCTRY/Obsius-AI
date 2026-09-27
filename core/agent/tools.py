"""Agent 工具面（DESIGN.md §3）。

Agent 没有裸 shell：run_cmd 经 gateway；黑板读写走 Blackboard；
任务认领/收尾走 TaskQueue。所有工具结果以文本回填（tool_result）。
"""

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from core.agent.retention import omitted_note, retain, spill_text
from core.blackboard import Blackboard, ClaimError, TaskQueue
from core.blackboard.assets import register_asset
from core.blackboard.attackpath import _intent_in_site, _subtree_ids
from core.blackboard.intents import (declare_intent as _declare_intent,
                                     close_intent as _close_intent,
                                     list_intents as _list_intents,
                                     reopen_intent as _reopen_intent)
from core.blackboard.store import UNSET, has_repro_evidence
from core.blackboard.tasks import dedup_fp
from core.runtime.gateway import ExecutionGateway, GatewayDenied
from core.skills import proposals
from core.skills.proposals import ProposalError
from core.skills.registry import SkillRegistry
from core.skills.experts import list_experts
from core.skills.routeindex import top_route_entries
from core.skills.rules import load_kb_sources
from core.skills.writing import resolve_kb, search_kb

# 收尾协议工具不在角色 tools 白名单管控内（它们是循环控制原语，不是能力）。
# request_steps（E8）同属控制原语：预算自助增补与收尾决策一样必须永远可达。
_CONTROL_TOOLS = {"complete_task", "fail_task", "finish", "request_steps",
                  # close_intent/reopen_intent（v3，2026-09-24）：意图必收尾是义务
                  # 不是能力——任何角色都必须能关闭/重开意图，恒放行
                  "close_intent", "reopen_intent"}

# 计划原语同样恒放行：任何角色认领任务后都必须能写/推进计划（A2 先规划后动手）
_PLAN_TOOLS = {"task_plan", "task_step", "task_reconcile",
               # declare_intent（v3）：意图就是规划产物，与写计划同级
               "declare_intent"}

# 计划闸（A2）：认领后计划为空时，这些只读/规划类工具可先调，其余一律引导先 task_plan。
# task_step 放行是为了让修订/纠正类回填不被自己的闸挡住（空计划下它会被服务端正常拒）。
_PLAN_PRE_ALLOWED = _PLAN_TOOLS | {
    "bb_query", "kb_open", "kb_search", "list_symbols", "decompile",
    # strings_search/func_xrefs 只读缓存检索（2026-09-27，侦察先行同 list_symbols）
    "strings_search", "func_xrefs",
    # read_file 只读工作区文件（侦察先行，2026-09-20）
    "read_file",
    # search_files 非 shell 的工作区内容检索（2026-09-24，替代计划前 run_cmd grep）
    "search_files",
    # skill_open 只是把技能手册加载进上下文（=读手册，不执行任何动作，2026-09-24）
    "skill_open",
    # F6 内置浏览器：只读侦察先行（导航/截图/读 DOM）；click/type/back 属实质动作受计划闸
    "browser_navigate", "browser_screenshot", "browser_content",
}

# 运行时等级（DESIGN.md §7）；角色 max_runtime = 允许的最高等级，只可能比网关策略更严
RUNTIME_RANK = {"host": 0, "wsl": 1, "docker": 2, "sandbox": 3}

# bb_query 闭集值域（bb-query-filters M1）：非法值显式 [错误]，不静默返回空。
# ⚠ 第二处值域声明，改 store 状态机/任务状态时必须同步：
#   findings severity → store.list_findings severity_rank；
#   assets status/type → store.set_asset_status 白名单 / register_asset 类型；
#   tasks status → tasks.py 状态写入点（open/claimed/done/failed/blocked/cancelled）。
_BB_SEVERITIES = ("info", "low", "medium", "high", "critical")
_BB_ASSET_STATUSES = ("open", "visited", "scanning", "tested_clean",
                      "budget_stop", "na")
_BB_TASK_STATUSES = ("open", "claimed", "done", "failed", "blocked", "cancelled")
_BB_ASSET_TYPES = ("host", "domain", "service", "url", "binary")

# H1 spill（2026-09-19，借鉴 dsh tool-output-spill）：超限工具结果全量落盘 +
# 有界预览。豁免两类：run_cmd 已有网关 brief 截断；kb/skill/route 打开类工具
# 的正文本身就是取用目的（落盘隔一层反而逼模型多绕一步）。
_SPILL_SKIP = {"run_cmd", "kb_open", "skill_open", "route_lookup"}
_SPILL_THRESHOLD = 8000  # 字符；生产 60k 摘要线之下，单结果不该占这么多

# stuck-convergence D6（2026-09-23）：收尾确认轮上限——防确认本身拖收尾烧 token
_CLOSING_MAX_ROUNDS = 2

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
        "description": "执行命令。runtime 按威胁等级选择：docker=Linux 渗透工具箱"
                       "（bash + nmap/sqlmap/dirsearch/ffuf/python3 全套，workspace 挂载 "
                       "/workspace；能力清单显示 pentest-box 镜像就绪时渗透/扫描/文本命令"
                       "首选，未就绪回退 wsl）；host=Windows 特调（PowerShell，仅 Windows"
                       " 目标/命令）；wsl=bash 兜底；sandbox=恶意样本（唯一选择，"
                       "威胁等级不得虚报为 trusted）。落盘纪律见系统提示工具纪律。"
                       "输出上限 2000 字符，超出截断并标注——读大文件优先用 read_file "
                       "工具，或 run_cmd 用 grep -n 定位后小窗口（≤100 行）分段。",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的命令"},
                "runtime": {"type": "string", "enum": ["host", "wsl", "docker", "sandbox"],
                            "description": "可省略：省略时按本任务默认运行时（任务 chip 设的 "
                                           "preferred_runtime）执行；任务未设默认时必须显式传"},
                "threat_class": {"type": "string", "enum": ["trusted", "untrusted", "malware_live"]},
                "net": {"type": "string", "enum": ["none", "bridge", "real"],
                        "description": "仅 sandbox/docker 有效；real 需审批 id"},
                "approval_id": {"type": "string"},
                "timeout": {"type": "number", "description": "超时秒数（可选）"},
            },
            "required": ["cmd", "threat_class"],
        },
    },
    {
        "name": "read_file",
        "description": "只读工作区文件（2026-09-20 新增）：host 原生直接读，不经 WSL/"
                       "PowerShell（无命令执行面、无启动开销、无引号转义问题）。"
                       "path 相对 scratch（与 run_cmd cwd 一致）；返回带行号文本，"
                       "默认读 100 行、单次上限 400 行；offset=-N 读末尾 N 行。"
                       "大文件先用 search_files 按内容定位再按行段读，不要盲猜行号。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "相对 scratch 的路径（如 logs/run.log）；"
                                        "也收工作区内绝对路径"},
                "offset": {"type": "integer",
                           "description": "起始行号（1 起）；负数 N=读末尾 N 行"},
                "limit": {"type": "integer",
                          "description": "读取行数，缺省 100，上限 400"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_files",
        "description": "在工作区文本文件中按内容检索（2026-09-24 新增）：纯 Python "
                       "实现、不经 shell（无命令执行面、无注入风险），计划前也允许调"
                       "（等价只读侦察）。默认搜 scratch 及其余工作区目录；只搜本项目"
                       "工作区内文件，命中行以「相对路径:行号:内容」返回。适合在 "
                       "bb_query 溢出落盘文件里定位关键词，替代 run_cmd grep。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "检索模式；regex=true（默认）时为 Python 正则，"
                                           "否则为普通子串（大小写不敏感）"},
                "path": {"type": "string",
                         "description": "检索范围目录/文件，相对 scratch（如 ../spill）；"
                                        "缺省=整个工作区；也收工作区内绝对路径"},
                "regex": {"type": "boolean",
                          "description": "pattern 是否按正则解释，缺省 true；false=子串"},
                "glob": {"type": "string",
                         "description": "可选文件名通配（如 \"*.txt\"），缺省不限"},
                "max_results": {"type": "integer",
                                "description": "最多返回命中行数，缺省 100，上限 300"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "bb_add_asset",
        "description": "登记资产（host/domain/service/url/binary；type 省略按值自动识别，"
                       "识别不出拒收回填）。资产树自动挂载：url/service 含 IP 自动建 host，"
                       "domain 自动 DNS 解析挂 host。重报同值=合并 meta 不插重复行"
                       "（发布前先 bb_query 查重，勿重复登记）。meta.title=一句话简述；"
                       "meta.owner=平台标签（.edu.cn 系→edusrc 等，命中的平台规则会注入会话）。"
                       "扫描/测试状态走 bb_asset_status，不要写 meta.scanned。",
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
        "description": "流转资产扫描/测试状态（open→visited→scanning→tested_clean/"
                       "budget_stop/na）。访问≠测试：访问过标 visited、开始扫描标 scanning、"
                       "测完且无发现才标 tested_clean；预算/配额用尽被迫停手标 budget_stop、"
                       "确认不适用（如非目标协议/离线主机）标 na——三者一样必须带 note"
                       "（服务端强制），让「哪里没挖完、为什么」可对账。"
                       "tested_clean 服务端另强制死路意图背书：存在覆盖该资产的"
                       "closed/dead_end 意图（declare_intent→close_intent 带证据收尾；"
                       "批量面对父节点立一条意图覆盖子树），无背书直接标会被拒。"
                       "测出问题直接 bb_add_finding，不要自报状态。",
        "input_schema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["visited", "scanning", "tested_clean", "open",
                                    "budget_stop", "na"]},
                "note": {"type": "string",
                         "description": "tested_clean/budget_stop/na 必填："
                                        "测了什么/为什么停/为什么不适用"},
                "expected_revision": {"type": "integer",
                                      "description": "乐观锁：bb_query 读到的 rev 值。"
                                                     "多窗同时改同一资产时防覆盖，冲突回 [冲突]"},
            },
            "required": ["asset_id", "status"],
        },
    },
    {
        "name": "bb_add_finding",
        "description": "登记发现。复现步骤用 evidence.repro_steps="
                       "[{desc, type: http|python|cmd|image, code, expected, "
                       "artifact_id?, stability?, target?}]——desc/expected 必填，"
                       "code 填 HTTP 原始报文/python 脚本/bash 命令（type=image 时"
                       " artifact_id 必填指向图片产物、无 code），多步利用链按数组顺序；"
                       "渗透/红队轨 verified 门禁=至少一步 code（或 artifact_id）非空"
                       "且该步 expected 非空（旧结构 evidence.poc/pocs/poc_artifact_id "
                       "兼容但已 legacy）。危害描述 impact（影响事实：拿到什么/影响面）"
                       "与修复建议 remediation（可落地）直接落字段，报告按三件套渲染。"
                       "无证据 status=unverified（无证据不下结论，红线见规则段）；"
                       "注入评级口径（rule:rating:*）时 severity 必须按口径判级并填 "
                       "rating_basis（见评级硬指令段）。"
                       "CTF 轨语义：severity=线索级别（critical=关键突破/high=可行动线索/"
                       "其余=背景备查），vuln_class=线索类别；false-positive=死路"
                       "（evidence 写清原因与已尝试清单，防重走弯路）。"
                       "category 缺省自动判定（提示类或 info→intel，其余→vuln）。"
                       "解题脚本/writeup 用 bb_add_artifact 落产物。",
        "input_schema": {
            "type": "object",
            "properties": {
                "vuln_class": {"type": "string"},
                "title": {"type": "string"},
                "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
                "category": {"type": "string", "enum": ["vuln", "intel"],
                             "description": "缺省自动判定（提示类/info→intel，其余→vuln）；显式传参覆盖"},
                "status": {"type": "string", "enum": ["unverified", "verified", "false-positive"]},
                "target_asset_id": {"type": "string",
                                    "description": "挂到目标资产 id（URL 先 bb_add_asset 拿 id）"},
                "evidence": {"type": "object", "description": "证据对象；复现步骤"
                           " repro_steps=[{desc,type,code,expected,…}] 是 verified "
                           "门禁认可结构（desc/expected 必填，多步链按数组顺序）；"
                           "旧 poc/pocs 字段 legacy 兼容，勿再新写"},
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
                                    "description": "Python 脚本 POC 的产物 id（bb_add_artifact 返回；legacy，"
                                                   "复现步骤优先写 repro_steps+步骤内 artifact_id）"},
                "impact": {"type": "string",
                           "description": "危害描述（报告三件套）：影响事实——实际拿到什么"
                                          "数据/权限、影响面多大"},
                "remediation": {"type": "string",
                                "description": "修复建议（报告三件套）：可落地的修复措施"},
                "rating_basis": {"type": "string",
                                 "description": "判级依据（F11）：注入评级口径时必填，格式"
                                                "「规则名+条款+一句话依据」，如"
                                                "「rating:edu-rating 高危#2 任意文件覆盖写」"},
                "confidence": {"type": "number"},
                "dedup_key": {"type": "string"},
            },
            "required": ["vuln_class", "title"],
        },
    },
    {
        "name": "bb_update_finding",
        "description": "修订已有发现（只传要改的字段）。① 降级时 rating_basis 必须同给"
                       "「规则名+条款+一句话依据」，否则宁可不改；② 口径外内容（纯暴露面/"
                       "过期组件等）→ category=intel + severity=low 转有效线索，不要删除；"
                       "③ 误报/死路 → status=false-positive（触发撤回传播通知引用方）；"
                       "④ evidence 浅层合并（键级覆盖，列表键整键替换——补复现步骤请整组"
                       "传 repro_steps 全量）。"
                       "rating_basis/impact/remediation 不传=不动，传空串=清空。"
                       "渗透/红队轨不收 severity=info（服务端拒收，CTF 轨可用）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "severity": {"type": "string", "enum": ["info", "low", "medium", "high", "critical"]},
                "status": {"type": "string", "enum": ["unverified", "verified", "false-positive"]},
                "title": {"type": "string"},
                "vuln_class": {"type": "string"},
                "category": {"type": "string", "enum": ["vuln", "intel"],
                             "description": "vuln=漏洞 / intel=有效发现·线索"},
                "evidence": {"type": "object",
                             "description": "浅层合并：键级覆盖、未传键不动；repro_steps "
                                            "为列表键=整键替换，修订步骤须传全量数组"},
                "rating_basis": {"type": "string",
                                 "description": "判级依据：改 severity 时必填；空串=清空，不传=不动"},
                "impact": {"type": "string",
                           "description": "危害描述（报告三件套）：空串=清空，不传=不动"},
                "remediation": {"type": "string",
                                "description": "修复建议（报告三件套）：空串=清空，不传=不动"},
                "expected_revision": {"type": "integer",
                                      "description": "乐观锁：bb_query 读到的 rev 值。"
                                                     "多窗同时改同一发现时防覆盖，冲突回 [冲突]"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "bb_delete_finding",
        "description": "物理删除发现（按 finding_id，reason 必填——删错不可恢复）。"
                       "仅限垃圾/走查数据（重复噪声、无价值记录）；**verified 发现不可删除**；"
                       "误报走 bb_update_finding 改 status=false-positive；口径外内容"
                       "（暴露面等）走 bb_update_finding 转 intel 线索而非删除。",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "reason": {"type": "string",
                           "description": "删除原因（审计用，必填）"},
            },
            "required": ["finding_id", "reason"],
        },
    },
    {
        "name": "bb_add_artifact",
        "description": "落产物文件（POC 脚本/抓包/输出）：写项目产物目录 + sha256 落库。"
                       "kind='poc' 仅限 .py——报文打不稳的漏洞才用脚本复现。"
                       "kind='project' 限 R4 组装集成最终交付（整个重建程序的 zip 包）。"
                       "CTF：解题/复现脚本与 writeup 必须落 artifact，不写裸文件"
                       "（队友与续跑会话靠黑板找现场）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "文件名（纯文件名，不含路径；kind=poc 时必须 .py）"},
                "content": {"type": "string", "description": "文件全文"},
                "kind": {"type": "string", "enum": ["file", "poc", "capture", "project"]},
                "description": {"type": "string"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "bb_query",
        "description": "查黑板：findings / assets / events / tasks / func（函数知识库）/"
                       "blueprint（开发蓝图）/ site（单站全貌）。"
                       "**查某站点的资产+发现+意图现状，一律用 what=site**（一次返回"
                       "根资产子树全貌）——不要按 type 分片拉全量再本地过滤（host/url/"
                       "domain 各拉一把、条数多还漏看）。"
                       "findings 尽量带 target_asset_id 精确过滤；events 尽量带 kinds/"
                       "session_id；limit 传小值会截断漏看（漏看了仍要重查，得不偿失）。"
                       "逆向场景硬规则：反编译任何函数前必须先查 func 防重复劳动。",
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "enum": ["findings", "assets", "events", "tasks", "func",
                                  "blueprint", "site"]},
                "asset": {"type": "string",
                          "description": "site 查询必填：根资产 id 或精确 value"
                                         "（host/domain，如 202.196.32.142）——"
                                         "返回其子树全部资产+各自发现+相关意图+终态"},
                "blueprint_id": {"type": "string",
                                 "description": "blueprint 查询：单份蓝图（缺省列全部）"},
                "target_asset_id": {"type": "string",
                                    "description": "findings：只回挂在该资产上的发现"
                                                   "（精确匹配；查站点全貌请改用"
                                                   " what=site）"},
                "binary_sha256": {"type": "string", "description": "func 查询必填"},
                "address": {"type": ["integer", "string"],
                            "description": f"func 单点查询。{_ADDR_DESC}"},
                "risk_tag": {"type": "string", "description": "func 按风险标签过滤"},
                "type": {"type": "string",
                         "description": "assets 按类型过滤（host/domain/service/url/binary）"},
                "status": {"type": "string",
                           "description": "按状态过滤（语义随 what）："
                                          "assets=open/visited/scanning/tested_clean/"
                                          "budget_stop/na（并发会话可借此感知哪些目标"
                                          "正被扫）；tasks=open/claimed/done/failed/"
                                          "blocked/cancelled"},
                # findings 过滤（M1，底层 store 早已支持）
                "min_severity": {"type": "string", "enum": ["info", "low", "medium",
                                                            "high", "critical"],
                                 "description": "findings：不低于该严重级"
                                                "（high → high+critical）"},
                "verified_only": {"type": "boolean",
                                  "description": "findings：只回已验证（status=verified）"},
                "category": {"type": "string",
                             "description": "findings：按类别过滤（vuln=漏洞 / "
                                            "intel=有效发现·关键发现）"},
                "tag": {"type": "string",
                        "description": "assets：按 meta.tags 标签过滤（大小写不敏感）"},
                # events 过滤（M2）
                "kinds": {"type": "array", "items": {"type": "string"},
                          "description": "events：只回这些事件类型（如 "
                                         "[\"command\",\"finding.new\"]）"},
                "session_id": {"type": "string",
                               "description": "events：只回该会话落的事件"},
                # 六面统一 limit（M2；默认 events=50、其余全量，行为不变）
                "limit": {"type": "integer",
                          "description": "返回条数上限，1-200（events 默认 50；"
                                         "findings/assets/tasks/func/blueprint 默认全量）"},
            },
            "required": ["what"],
        },
    },
    {
        "name": "kb_open",
        "description": "打开知识库模块（能力包 kb/ 区，含测试包手册与快照）："
                       "返回文件绝对路径，随后按需 Read。"
                       ".md/.py/.txt/.json 均可打开（弹药脚本只是文本，执行仍须走 run_cmd）。"
                       "路径不存在会返回可用模块清单（照清单改选，禁止猜文件名、"
                       "禁止 .. 穿越）。禁止通读知识库目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {"type": "string",
                           "description": "知识库内相对路径，如 "
                                          "webapp/idor/手册.md（web 测试包）、"
                                          "refs/ctf-pwn/heap-fsop.md（快照原件）"},
            },
            "required": ["module"],
        },
    },
    {
        "name": "kb_search",
        "description": "按关键词全文检索知识库（大小写不敏感子串匹配）："
                       "返回 {path（即 kb_open 的 module 参数）, source, matches, snippet}。"
                       "多关键词空格分隔为 AND 语义（各词都命中的文件才返回），"
                       "零结果自动放宽为 OR（任一词命中）。"
                       "tag 可选：按 frontmatter 分面标签过滤（phase/vuln_class，"
                       "如 tag=jwt 只留打了该标签的手册）。"
                       "典型用法：kb_search 定位 → kb_open 细读。"
                       "中文专题用中文词、英文快照用英文词，必要时各试一次。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "检索关键词（子串匹配；空格分隔多关键词）"},
                "limit": {"type": "integer",
                          "description": "返回条数上限，默认 10，最大 50（可选）"},
                "tag": {"type": "string",
                        "description": "分面标签过滤（可选，phase/vuln_class 值，"
                                       "如 sqli、jwt、recon）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "propose_pack_edit",
        "description": "提一条知识库/技能/路由索引变更**提案**（只落 pending，批准权在人类，"
                       "禁止自行改文件；允许的三种情形、reason 证据要求与沉淀去向见系统提示"
                       "工具纪律）。每会话最多 3 条。"
                       "kind=skill mode=edit（改正文，target={skill_kind: capability|track,"
                       " owner: 包/轨名, name: 技能名}）或 mode=suggest（粒度过粗的技能提"
                       "「拆分建议」：content 为建议文档全文，批准后落技能目录 拆分建议.md，"
                       "结构变更由人执行）；kind=kb mode 可为 "
                       "edit/create/rename/delete，target={cap: 能力包名, path: 全局模块 "
                       "路径 <域>/<快照>/<文件>（.md/.py/.txt/.json，中文目录允许）, "
                       "new_path: 仅 rename}；"
                       "kind=case mode=edit|create（K6 成功链沉淀：本任务 verified 攻击链/"
                       "跑通 payload 落对应测试包 成功案例.md 补段或 payloads/ 补弹药，"
                       "target 同 kb）；kind=index mode=edit（增补全局 route_index.yaml 中"
                       "本域条目，kb 路径须带域前缀），target={cap: 能力包名}——"
                       "新增了值得索引的测试点手册时同步 route_index.yaml（content 为"
                       "本域条目全文）。"
                       "edit/create 必带 content 全文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["kb", "skill", "index", "case"]},
                "mode": {"type": "string",
                         "enum": ["edit", "create", "rename", "delete", "suggest"]},
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
        "name": "bb_blueprint_create",
        "description": "建开发蓝图骨架（R4 逆向重建中枢）：模块划分任务的产出。"
                       "modules 按业务职能聚类（网络通信/加密校验/文件持久化/许可校验…），"
                       "不按编译单元；已知库函数（libc/API 包装）不进模块；"
                       "每模块必附 func_addresses 清单与业务推断理由（desc）。"
                       "模块接口约定（spec 雏形：函数签名/数据结构/协议格式）在此先行钉死——"
                       "后续并行深析与组装全靠接口先行防冲突。同项目同样本下重名会拒收。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "goal": {"type": "string",
                         "description": "重建目标：要造一个什么业务逻辑的程序"},
                "binary_sha256": {"type": "string",
                                  "description": "目标样本 sha256（非单样本蓝图可空）"},
                "modules": {"type": "array",
                            "items": {"type": "object",
                                      "properties": {
                                          "name": {"type": "string"},
                                          "desc": {"type": "string",
                                                   "description": "业务职能与推断理由"},
                                          "func_addresses": {"type": "array",
                                                             "items": {"type": "string"},
                                                             "description": "hex 地址串"},
                                          "spec": {"type": "string"},
                                          "notes": {"type": "string"},
                                          "status": {"type": "string",
                                                     "enum": ["pending", "analyzed",
                                                              "specd", "tested"]},
                                      },
                                      "required": ["name"]}},
            },
            "required": ["name"],
        },
    },
    {
        "name": "bb_blueprint_update",
        "description": "更新开发蓝图（分区更新，别的会话实时可见）。三种用法："
                       "①模块深析写回：module_name + spec/notes/func_addresses/"
                       "module_status（spec=接口约定：函数签名/数据结构/协议格式；"
                       "自测不过不得标 module_status=tested）；"
                       "②整表重划分：modules_set 替换全部模块；"
                       "③汇总正文：content_append 增量追加或 content_md 整体替换"
                       "（数据流/接口表/算法/协议/状态机）。"
                       "蓝图整体 status（draft/reviewed/ready/building/built）"
                       "不归 Agent 管——reviewed/ready 由人类批准，勿申请。",
        "input_schema": {
            "type": "object",
            "properties": {
                "blueprint_id": {"type": "string"},
                "module_name": {"type": "string",
                                "description": "模块级写回时必填（须为既有模块名）"},
                "spec": {"type": "string", "description": "接口约定（函数签名/数据结构/协议格式）"},
                "notes": {"type": "string", "description": "关键实现要点/业务逻辑发展"},
                "desc": {"type": "string"},
                "func_addresses": {"type": "array", "items": {"type": "string"}},
                "module_status": {"type": "string",
                                  "enum": ["pending", "analyzed", "specd", "tested"],
                                  "description": "tested 必须容器自测通过后才可标"},
                "modules_set": {"type": "array",
                                "description": "整表替换模块数组（重划分，慎用）",
                                "items": {"type": "object"}},
                "content_append": {"type": "string",
                                   "description": "追加到蓝图正文末尾"},
                "content_md": {"type": "string",
                               "description": "整体替换蓝图正文"},
            },
            "required": ["blueprint_id"],
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
        "description": "列出二进制函数符号表（地址/名字/大小/是否已导出伪码）。"
                       "支持按名/按大小过滤——找关键函数先用它缩小范围，勿整表吞。",
        "input_schema": {
            "type": "object",
            "properties": {
                "binary": {"type": "string"},
                "name_contains": {"type": "string",
                                  "description": "按符号名子串过滤（大小写不敏感）"},
                "min_size": {"type": "integer",
                             "description": "只回大于等于该字节数的函数（过滤 stub/thunk）"},
            },
            "required": ["binary"],
        },
    },
    {
        "name": "strings_search",
        "description": "检索二进制字符串表（大小写不敏感子串），行带地址+引用函数。"
                       "逆向找提示/密钥/flag 线索第一步用它，不要用 run_cmd 直读缓存 JSON。",
        "input_schema": {
            "type": "object",
            "properties": {
                "binary": {"type": "string", "description": "二进制文件路径"},
                "pattern": {"type": "string",
                            "description": "子串过滤（大小写不敏感）；缺省=全量（会被 limit 截断）"},
                "limit": {"type": "integer",
                          "description": "最多返回条数（默认 200，上限 1000）"},
            },
            "required": ["binary"],
        },
    },
    {
        "name": "func_xrefs",
        "description": "查函数调用关系（callers/callees）。给 name 或 address 其一；"
                       "地址会先解析成函数名。判断关键函数被谁调/调了谁时用它。",
        "input_schema": {
            "type": "object",
            "properties": {
                "binary": {"type": "string", "description": "二进制文件路径"},
                "name": {"type": "string", "description": "按符号名查"},
                "address": {"type": ["integer", "string"],
                            "description": f"按函数入口地址查。{_ADDR_DESC}"},
            },
            "required": ["binary"],
        },
    },
    {
        "name": "task_plan",
        "description": "写下/修订当前任务的解决计划。**认领任务后、任何实质动作（run_cmd/"
                       "bb_add_* 等）之前必须先调一次**：3-8 个可验证的小步，按执行顺序。"
                       "实质动作步（利用/打点/写利用代码）必须带 refs 引用其依据的黑板对象"
                       "（如 finding:xxx 资产 f_id=1 的 SQL 注入），id 必须真实存在——服务端"
                       "校验，编造/悬空 id 会被拒。执行中情况变化可再调修订：想保留进度的"
                       "步带上原 id（状态/时间戳保留，标题可改），新步不带 id 由服务端发号，"
                       "不再需要的步直接省略；rev_reason 写修订原因。任意时刻至多一个 "
                       "doing——用 task_step 切换。",
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
                            "refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 5,
                                "description": "接地引用（kind:id）：这一步依据的黑板对象，"
                                               "如 finding:xxx、asset:yyy、artifact:zzz、"
                                               "func:www。实质动作步必须带（服务端校验存在"
                                               "性，编造/悬空 id 被拒）；纯侦察步可省。",
                            },
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
        "name": "task_reconcile",
        "description": "任务验收对账收口（发布时登记了 acceptance 分母的任务）：逐条交代"
                       "完成情况——met=已完成（note 附证据）/failed=已证实无法完成（note 附"
                       "原因）/blocked=受阻（note 附卡点）。全部条目收口前 complete_task "
                       "会被硬拦；诚实对账优先于「看起来做完了」。"
                       "注意：带 🔬 验证规格的条目由服务端验证器自动判定，自报 met/failed "
                       "会被拒绝——把工作做到位后直接重新 complete_task 触发重验即可；"
                       "确不适用才置 blocked（note 附原因）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "对账条目编号（complete 被拦时回执会列出）"},
                "state": {"type": "string", "enum": ["met", "failed", "blocked"]},
                "note": {"type": "string", "description": "证据/原因/卡点（强烈建议填写）"},
            },
            "required": ["item_id", "state"],
        },
    },
    {
        "name": "publish_task",
        "description": "把当前任务**分解**出一个子任务，派给其他会话/Worker 认领执行"
                       "（自动挂当前任务为 parent，created_by=本会话；没有认领任务时 parent 为空）。"
                       "规则同人类发任务：非 passive 必须给 conflict_keys（同目标 active 互斥）；"
                       "task_type 必须是本轨已注册类型（generic 恒合法）；"
                       "与既有 open/claimed 同目标任务重复发布被拒（返回 [复用]）——发布前先 bb_query 查任务。"
                       "分解纪律：仅当确需并行推进或不同专业技能才分解，能自己完成的禁止下包；"
                       "每任务最多分解 3 个子任务，同目标在队子任务过多也会被拒收（先消化存量）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "子任务目标（自足可执行：执行者看不到你的上下文）"},
                "task_type": {"type": "string", "description": "本轨注册表内的类型；缺省 generic"},
                "role": {"type": "string", "description": "建议认领角色（可选）：本轨 roles/ 已注册角色 id，"
                                                          "认领会话将按该角色换装执行；留空=不限"},
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
        "name": "bb_notify",
        "description": "私信其他会话窗（黑板异步协调，2026-09-20 会话窗对话化）："
                       "同步情报/移交工作/请求协助。to_session 定向单窗（优先），"
                       "to_role 广播该角色全部活跃窗（不含自己）；两者必须给一个。"
                       "收件方在步边界/认领期/对话轮注入，不实时打断对方工作"
                       "（异步模型，无同步对话接力）；不设频率硬限额，滥用在审计可见。",
        "input_schema": {
            "type": "object",
            "properties": {
                "to_session": {"type": "string",
                               "description": "收件会话 id（sess- 前缀，定向优先）"},
                "to_role": {"type": "string",
                            "description": "收件角色 id（广播该角色全部活跃窗；无 to_session 时生效）"},
                "kind": {"type": "string", "enum": ["intel", "handoff", "assist"],
                         "description": "intel=情报同步 handoff=工作移交 assist=协助请求；缺省 intel"},
                "text": {"type": "string",
                         "description": "正文（≤4000 字符超出截断）；黑板对象一律引用 id（find-/as-/task-）"},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "相关黑板对象 id 清单（可选）"},
            },
            "required": ["text"],
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
    {
        "name": "browser_navigate",
        "description": "内置浏览器导航（F6）。目标必须已在项目资产表登记"
                       "（host/domain/url 任一命中；未登记会被拒绝并提示先 bb_add_asset）。"
                       "成功返回最终 url/标题/状态码；页面流量已自动入抓包历史"
                       "（人类可在浏览器页查看/重发）。每个动作落审计。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "目标 url 或裸 host"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": "内置浏览器点击当前页元素（CSS 选择器或页面坐标 x/y）。"
                       "作用于当前会话页面，不重复白名单校验。",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器；与 x/y 二选一"},
                "x": {"type": "number", "description": "页面横坐标（与 y 搭配）"},
                "y": {"type": "number", "description": "页面纵坐标"},
            },
        },
    },
    {
        "name": "browser_type",
        "description": "内置浏览器向当前页输入框填文本（CSS 选择器定位，整值替换）。"
                       "凭据纪律：登录凭据只在会话内存使用，禁止把凭据写入黑板/任务/发现。",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器"},
                "text": {"type": "string", "description": "要填入的文本"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "内置浏览器对当前页单帧截图（PNG）。截图自动落 artifacts"
                       "（browser-shots/，挂当前任务归属）并回填路径，人类可在浏览器页查看。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_content",
        "description": "内置浏览器读取当前页 DOM 文本（inner_text，截 8KB），"
                       "用于 JS 渲染后的动态页面内容提取（curl 拿不到的部分）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_back",
        "description": "内置浏览器后退（历史上一页）。",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "route_lookup",
        "description": "按关键词查询测试点路由索引（认领时只注入最相关条目，其余在这里查）。"
                       "返回 测试点 → kb 模块路径；命中后用 kb_open 打开手册细读。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "测试点关键词（中英文皆可，如「文件上传」)"},
                "limit": {"type": "number", "description": "返回条数上限，缺省 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "skill_open",
        "description": "打开技能的全量正文（认领注入的是技能目录摘要，不整段注入）。"
                       "name 用当前命中技能名或 route_lookup/设置页看到的技能名。",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名（如 sqli-test）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "request_authorization",
        "description": "申请行为边界授权（M5 D2，orchestrator-efficiency）：发现受当前"
                       "授权边界限制打不下去时，向人类显式申请——不默默死路记账了事。"
                       "三类：scope_expand=扩大授权目标（新目标打之前先申请；批准后"
                       "自行 bb_add_asset 登记）；impact_escalate=影响证明升级（如从"
                       "探测升级到拿权限证明）；rating_override=突破收录口径（发现"
                       "真实影响但按评级规则到不了 vuln/high，申请按更高口径登记）。"
                       "**恒人类决策**（L2 也不自动批），结果投递回你的收件箱"
                       "（authorization_result/approval_rejected）。等待期间可继续"
                       "其他无依赖工作。",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["scope_expand", "impact_escalate", "rating_override"]},
                "scope_request": {"type": "string",
                                  "description": "申请内容：新目标清单/申请升级到的影响"
                                                 "证明等级/申请突破的口径与目标评级"},
                "justification": {"type": "string",
                                  "description": "为什么必须扩（审批人主要看这个）"},
                "evidence_finding_ids": {"type": "array", "items": {"type": "string"},
                                         "description": "支撑证据的 finding id 列表"},
            },
            "required": ["kind", "scope_request", "justification"],
        },
    },
    {
        "name": "request_escalation",
        "description": "申请一次性越界执行（deny-driven：仅当命令当前被策略拒绝或"
                       "超角色 max_runtime 软上限时才受理）。人类批准后该命令**只执行"
                       "一次**，结果投递回你的收件箱（下个步边界/空闲对话轮可见）。"
                       "工作区隔离、隔离等级（threat_class↔runtime）与限速纪律是红线"
                       "或自助项，不受理。等待期间可继续其他无依赖工作。",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "要执行的一条命令（原样执行）"},
                "runtime": {"type": "string", "enum": ["host", "wsl", "docker", "sandbox"]},
                "reason": {"type": "string",
                           "description": "为什么必须越界、不改道（审批人只看得到这个）"},
                "threat_class": {"type": "string", "enum": ["trusted", "untrusted"]},
                "net": {"type": "string", "enum": ["bridge", "real"],
                        "description": "real=真实网络（须审批的主因时填 real）"},
                "timeout": {"type": "number"},
            },
            "required": ["cmd", "runtime", "reason"],
        },
    },
    {
        "name": "declare_intent",
        "description": "声明意图（思考/规划产物）：把一句【可证伪假设】登记到链路图，"
                       "如「验证 /admin 是否存在未授权访问」。随后围绕它执行（http/"
                       "工具动作按时间归入该意图，执行层是图的展开细节）。"
                       "每个意图最终必须 close_intent 收尾为漏洞/发现/死路，不得悬挂。"
                       "basis_refs 写推导依据（从什么资产/发现逻辑推出本假设，"
                       "形如 finding:<id>/asset:<id>），引用必须已在本项目，悬空即拒。"
                       "同陈述的未关闭意图已存在 → 直接返回既有意图（merged=true）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "statement": {"type": "string",
                              "description": "一句可证伪假设（≤500 字，勿写动作清单）"},
                "target_asset_id": {"type": "string",
                                    "description": "意图针对的根目标资产 id（host/domain）"},
                "basis_refs": {
                    "type": "array",
                    "description": "推导依据引用：[\"asset:<id>\", \"finding:<id>\", …]——"
                                   "本意图从哪些已有事实逻辑推出（边=逻辑推导）",
                    "items": {"type": "string"},
                },
            },
            "required": ["statement"],
        },
    },
    {
        "name": "close_intent",
        "description": "收尾意图（意图必收尾，结果三选一）："
                       "① outcome=vuln——确认漏洞：finding_ids 至少一条已登记漏洞"
                       "（category=vuln、非误报）；② outcome=finding——有效发现："
                       "finding_ids 至少一条有效线索（category=intel、非误报），"
                       "一个意图可带多条同类发现（目录爆破 5 条=一次发现收尾）；"
                       "③ outcome=dead_end——死路：dead_reason 必填（什么证据排除了"
                       "假设、已试过什么）+ evidence_refs 至少一条 http:/event:/"
                       "artifact: 证据引用；死路必须零发现。"
                       "证据不足就保持 open（宁严勿松）；有新证据先 reopen_intent。",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["vuln", "finding", "dead_end"]},
                "finding_ids": {
                    "type": "array",
                    "description": "outcome=vuln/finding 时必填：收尾引用的发现 id",
                    "items": {"type": "string"},
                },
                "evidence_refs": {
                    "type": "array",
                    "description": "outcome=dead_end 时必填：证据引用"
                                   "[\"http:<历史id>\", \"event:<事件id>\", \"artifact:<id>\"]",
                    "items": {"type": "string"},
                },
                "dead_reason": {"type": "string",
                                "description": "outcome=dead_end 时必填：死因（什么证据排除假设）"},
            },
            "required": ["intent_id", "outcome"],
        },
    },
    {
        "name": "reopen_intent",
        "description": "重开已关闭意图：收尾所依据的漏洞被标误报、或出现新证据时调用。"
                       "清空收尾结论回到 open（证据引用保留），下游不级联推翻"
                       "（推导边只断开，已衍生的新意图独立存活）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent_id": {"type": "string"},
                "note": {"type": "string",
                          "description": "重开原因（新证据是什么）"},
            },
            "required": ["intent_id"],
        },
    },
]

# ---- Agent 工具目录分组（agent-tools-view M1，2026-09-24）----
# 静态规则、无 IO：新增工具按下列集合/前缀落组；规则未覆盖会落「其他」，
# GET /api/agent-tools 的测试断言「无工具落其他」专门拦截漏配。
TOOL_GROUPS = ["执行", "文件", "黑板", "知识", "浏览器", "协作", "计划", "控制"]
_COLLAB_TOOLS = {"publish_task", "request_authorization", "request_escalation"}
_KNOWLEDGE_EXTRA = {"kb_open", "kb_search", "skill_open", "route_lookup",
                    "propose_pack_edit", "list_symbols", "decompile",
                    "strings_search", "func_xrefs"}
_FILE_TOOLS = {"read_file", "search_files"}


def agent_tool_group(name: str) -> str:
    if name in _CONTROL_TOOLS:
        return "控制"
    if name in _PLAN_TOOLS:
        return "计划"
    if name in _COLLAB_TOOLS:
        return "协作"
    if name == "run_cmd":
        return "执行"
    if name in _FILE_TOOLS:
        return "文件"
    if name.startswith("bb_"):
        return "黑板"
    if name.startswith("browser_"):
        return "浏览器"
    if name in _KNOWLEDGE_EXTRA:
        return "知识"
    return "其他"


class ToolDispatcher:
    """工具分发中枢。持会话状态：当前任务、进度心跳（卡死检测的依据）。"""

    #: C6 漏洞核对 hook（AgentSession 按 track 注入；None=不核对）：
    #: category=vuln 登记前调用 gate(draft) -> (ok, reason)，不合格降级 intel。
    vuln_gate: Callable[[dict], tuple[bool, str]] | None = None

    #: v0.65 done 自动提案 hook（AgentSession 注入 planner LLM 复盘；None=关闭）：
    #: complete_task 成功后 hook(task_id, result_note)、fail_task 真失败后
    #: hook(task_id, result_note, failure=True)（experience-sedimentation M1）——
    #: 条件判定在 hook 内部（_sediment_verdict），产经验沉淀提案草稿。
    sediment_hook: Callable[..., None] | None = None

    #: ⑥ 战役记忆写入 hook（AgentSession 注入全局库 CampaignMemory；None=关闭）：
    #: complete_task 成功后调用 hook(task_id, result_note) 沉淀打法（跨项目召回）。
    campaign_hook: Callable[[str, str], None] | None = None

    def __init__(self, bb: Blackboard, gateway: ExecutionGateway, tq: TaskQueue,
                 *, project_id: str, session_id: str, author: str,
                 decompiler=None, artifacts_dir=None, browser=None,
                 packs_root: str | Path | None = None,
                 track: str | None = None,
                 capabilities: list[str] | None = None,
                 allowed_tools: list[str] | None = None,
                 max_runtime: str | None = None,
                 allowed_task_types: Iterable[str] | None = None,
                 max_steps: int = 0,
                 stuck_after: int = 12,
                 closing_max_rounds: int = _CLOSING_MAX_ROUNDS,
                 abort_event: threading.Event | None = None,
                 role_skills: list[str] | None = None,
                 allowed_roles: list[str] | None = None):
        self.bb = bb
        self.gateway = gateway
        self.tq = tq
        self.project_id = project_id
        self.session_id = session_id
        self.author = author
        # ■ 即点即停（2026-09-19）：AgentSession 的 abort 事件——run_cmd 执行中
        # 置位即杀进程树（None=不接入，保持步边界语义）
        self.abort_event = abort_event
        self.decompiler = decompiler  # DecompilerService，缺省=未装配
        self.artifacts_dir = artifacts_dir  # 产物目录（bb_add_artifact 用），缺省=未装配
        # F6 内置浏览器实例池（BrowserPool）：None=未接入（轨外/测试）→ browser_* 全 no-tool；
        # 接入后依赖仍可能缺（playwright 未装）→ 动作时回填安装指引，不 500
        self.browser = browser
        self.packs_root = packs_root    # kb_open 解析全局 kb 树用，缺省=未装配
        self.track = track
        self.capabilities = capabilities or []
        # 角色 skills 白名单（G1：route_lookup 按其裁剪路由索引条目；None=全可见）
        self.role_skills = role_skills or None
        # 发布链 role 值域（expert-pool M2，§4.6）：绑定专家清单；None=按轨过滤的池
        self.allowed_roles = list(allowed_roles) if allowed_roles else None
        # skill_open 用技能注册表（懒加载缓存；与 AgentSession.registry 同口径）
        self._skill_registry = None
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
        # v14 认领即换装：当前任务实际执行 persona（None=按会话底色角色）；
        # 换装/恢复由 AgentSession._apply_task_persona/_restore_base_persona 维护
        self.current_persona_role: str | None = None
        self._step = 0
        self.last_progress_step = 0   # 最近一次实质进展的步号（卡死检测用）
        self.finished = False
        self.awaiting_human = False  # C1：fail_task(awaiting_human) 置位，_loop 收尾时落快照+fail
        # 意图纪律①：finish 首次撞未收尾意图被拦后置 True（二次 finish 才放行）
        self._finish_open_intents_ack = False
        self.summary = ""
        # 会话中心化（2026-09-25）：委托真收尾（complete/fail/删除）置位——_loop
        # 步边界据此结束本轮（会话不结束），worker while 再入会话轮接窗内下一件；
        # 收尾申报被 D6 确认闸打回不置位。last_delegation_note=收尾摘要（沉淀进
        # 会话对话历史，供后续对话轮带上下文）。
        self.delegation_just_finished = False
        self.last_delegation_note = ""
        # 计划闸教练模式（2026-09-24）：同一任务 2 个模型步撞计划闸后置 True——
        # LLM 工具面收缩到计划/控制原语；仍不写计划 → 下一模型步末 awaiting_human。
        # 计划落黑板即清；进程重启后为 False（引导从头再来，宁可宽一轮）。
        self.plan_only_mode = False
        # D6 收尾确认（2026-09-23）：complete 申报后先对照产出清单确认无遗漏
        self.dry_tail_steps = max(1, int(stuck_after) - 2)  # 干尾巴阈值=stuck_after-2，与 D1 窗错位
        # D10（2026-09-24）：收尾确认轮上限项目级可配（0=首次申报即放行）
        self.closing_max_rounds = (
            closing_max_rounds if isinstance(closing_max_rounds, int)
            and not isinstance(closing_max_rounds, bool) else _CLOSING_MAX_ROUNDS)
        self.closing_round = 0          # 0=未在确认；1/2=当前确认轮序号
        self.closing_last_progress = 0  # 本轮发起时的 last_progress_step（零新增判定基线）

    def set_step(self, n: int) -> None:
        self._step = n

    @property
    def step(self) -> int:
        """当前步号（E8：预算耗尽暂停时作恢复断点）。"""
        return self._step

    # ---------- 分发 ----------

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        """统一入口（2026-09-19「工具」tab）：每次调用落 tool.call 审计事件
        （工具名/截断入参/耗时/成败/结果头），拒绝与计划闸路径同样可观测。
        run_cmd 正常执行时除外——执行网关已落 command/command.result 配对，不重复；
        但 run_cmd 被闸口挡回（计划闸/越界，未进网关、无 command 事件）时照落审计
        （gated=true）——2026-09-24 前这类拒绝在事件流完全隐形。"""
        t0 = time.perf_counter()
        result = self._dispatch_once(name, args)
        result = self._maybe_spill(name, result)  # H1：超限结果落盘+预览，防淹没上下文
        gated = name == "run_cmd" and result.startswith(REJECT_PREFIXES)
        if name != "run_cmd" or gated:
            try:
                payload = {"name": name, "args": _truncate_args(args),
                           "step": self._step,  # 轮次号（观测对齐 command 事件；非计划步 id）
                           "ok": not result.startswith(_TOOL_FAIL_PREFIXES),
                           "duration_s": round(time.perf_counter() - t0, 2),
                           "result_head": result[:400]}
                if gated:
                    payload["gated"] = True
                self.bb.append_event(
                    self.project_id, "tool.call", payload,
                    session_id=self.session_id, author=self.author)
            except Exception:  # noqa: BLE001 —— 审计事件绝不影响工具返回
                pass
        return result

    def _maybe_spill(self, name: str, result: str) -> str:
        """H1 spill（借鉴 dsh tool-output-spill-files）：结果超阈值时全量落
        workspace/spill/，回填「head+tail 预览 + 精确省略计数 + 定位器 + 检索提示」
        ——有损但可找回（与 G3 摘要正交：摘要有损不可逆，spill 按需可取回）。
        未装配 workspace / 落盘失败一律原样返回（尽力而为，绝不阻断工具链）。"""
        if name in _SPILL_SKIP or len(result) <= _SPILL_THRESHOLD:
            return result
        if not self.artifacts_dir:
            return result
        ws = Path(self.artifacts_dir).parent
        path = spill_text(result, name, ws / "spill")
        if path is None:
            return result
        kept, omitted = retain(result, head=2000, tail=500)
        # 定位器给 scratch 相对路径（run_cmd cwd=scratch，host/wsl 通吃）
        try:
            rel = os.path.relpath(path, ws / "scratch").replace(os.sep, "/")
        except ValueError:  # Windows 跨盘等边缘：退回绝对路径
            rel = str(path)
        return (f"{kept}\n{omitted_note(omitted)}\n"
                f"[结果超限已落盘] 全文共 {len(result)} 字符，完整内容: {rel}\n"
                f"用 read_file 分段读取（带行号；offset=-N 可读末尾），"
                f"或 search_files(pattern=关键词, path={str(Path(rel).parent).replace(os.sep, '/')}) "
                f"定位，不要整读。")

    def _dispatch_once(self, name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"[错误] 未知工具: {name}"
        # v23（TRAE 新壳 M3）：run_cmd 省略 runtime → 按任务默认运行时回填；
        # 任务未设默认 → 要求显式传参（回填值照样过下方 max_runtime 软上限闸）。
        if name == "run_cmd" and args.get("runtime") is None:
            pref = ""
            if self.current_task_id:
                cur_task = self.tq.get_task(self.current_task_id)
                if cur_task is not None:
                    pref = str(cur_task.get("preferred_runtime") or "")
            if pref:
                args["runtime"] = pref
            else:
                return ("[错误] run_cmd 省略了 runtime，但本任务未设置默认运行时："
                        "请显式传 runtime（host/wsl/docker/sandbox，按目标与能力清单选择）")
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
                        "再开始实质动作。只读侦察（bb_query/kb_open/kb_search/"
                        "list_symbols/decompile）允许先行，但 run_cmd、写黑板等须在计划之后。")
        # raw_arguments 解包垫片（2026-09-26）：部分模型在长文本参数上会把全部参数
        # 包成 {"raw_arguments": "<JSON 字符串>"}（实测 ark-code-latest 调
        # bb_upsert_func 连发 6 次全中），平铺解包的裸 TypeError 只会让模型空转重试。
        # 原则：能解就解、能修就修、修好照常执行成功返回；实在到不了 handler
        # 才回 [参数格式] 提示（写入类工具幂等，多修多执行无副作用）。
        if set(args.keys()) == {"raw_arguments"}:
            raw = args["raw_arguments"]
            if isinstance(raw, str):
                raw = _loads_lenient_json(raw)
            if raw is None:
                return (f"[参数格式] {name} 的参数被包在 raw_arguments 里，且内容"
                        f"不是合法 JSON（已尝试裸十六进制自动修复仍失败——常见于 "
                        f"0x… 没加引号之外的语法残缺）。请以顶级平铺参数直接调用 "
                        f"{name}，不要嵌套任何外层对象。")
            if not isinstance(raw, dict):
                return (f"[参数格式] {name} 的 raw_arguments 应为平铺参数对象，"
                        f"实际收到 {type(raw).__name__}——请以顶级平铺参数直接调用 "
                        f"{name}，不要嵌套。")
            args = raw
        try:
            return handler(**args)
        except GatewayDenied as e:
            # 拒绝不是异常终止：Agent 看到原因后改道（§7 拒绝必须改道）
            return f"[网关拒绝] {e}"
        except Exception as e:  # noqa: BLE001 —— 工具失败回填文本，循环不中断
            return f"[工具异常] {type(e).__name__}: {e}"

    # ---------- 各工具实现 ----------

    def _tool_run_cmd(self, cmd: str, threat_class: str,
                      runtime: str | None = None,
                      net: str | None = None, approval_id: str | None = None,
                      timeout: float | None = None) -> str:
        # runtime 缺省（LLM 未传）由 _dispatch_once 按任务 preferred_runtime 回填；
        # 走到这里仍为 None = 无任务默认，拒绝执行
        if runtime is None:
            return ("[错误] run_cmd 缺少 runtime：请显式指定 host/wsl/docker/sandbox"
                    "（任务默认运行时未设置）")
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
            timeout=timeout, abort_event=self.abort_event,
            workspace=Path(self.artifacts_dir).parent if self.artifacts_dir else None,
            step=self._step,
        )
        return r.brief()

    def _tool_read_file(self, path: str, offset: int = 1, limit: int = 100) -> str:
        """只读工作区文件（2026-09-20）：host 原生 Python open，不经 WSL/PowerShell
        ——无命令执行面、无引号转义、无启动开销；pathguard 只拦写不受影响。
        只许读本项目工作区内（防越权读宿主任意文件）；cat -n 风格带行号；
        单行 >500 字符切尾标注；offset=-N 读末尾 N 行（tail 语义）。"""
        if not self.artifacts_dir:
            return "[错误] 未装配工作区（artifacts_dir），read_file 不可用"
        ws = Path(self.artifacts_dir).parent
        scratch = ws / "scratch"
        p = Path(path)
        if not p.is_absolute():
            p = scratch / p
        p = p.resolve()
        # 允许根 = 本项目工作区 + 启用域 kb 源根（packs/kb/<域>/，只读——
        # kb_open 回执指示 Read 手册；packs 其余目录仍拒）
        allowed_roots = [ws.resolve()]
        if self.packs_root:
            allowed_roots += [s.root.resolve()
                              for s in load_kb_sources(self.packs_root,
                                                       self.capabilities)
                              if s.root.is_dir()]

        def _within(root: Path) -> bool:
            try:
                p.relative_to(root)
                return True
            except ValueError:
                return False

        if not any(_within(r) for r in allowed_roots):
            return f"[拒绝] 只允许读本项目工作区或知识库内文件（path={path}）"
        if not p.is_file():
            return (f"[错误] 文件不存在: {path}（相对 scratch 解析；"
                    "可用 run_cmd ls/dir 查看目录，注意 host 是 PowerShell、wsl 才有 ls）")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"[错误] 读取失败: {e}"
        lines = text.splitlines()
        total = len(lines)
        # tail 语义：offset=-N 读末尾 N 行
        start = offset if offset > 0 else max(1, total + offset + 1)
        limit = max(1, min(int(limit), 400))
        if start > total:
            return (f"[错误] offset={offset} 超出文件范围（共 {total} 行）；"
                    "负数 offset 表示读末尾 N 行")
        sel = lines[start - 1:start - 1 + limit]
        width = len(str(start + len(sel) - 1))
        out = []
        for i, line in enumerate(sel, start=start):
            if len(line) > 500:
                line = line[:500] + f"…[行截断：共 {len(line)} 字符]"
            out.append(f"{str(i).rjust(width)}\t{line}")
        body = "\n".join(out)
        end = start + len(sel) - 1
        if end < total:
            body += f"\n…[共 {total} 行，当前显示 {start}-{end}；继续读调大 offset]"
        # D9：成功读取落 file.read 轻事件（只记路径/步号，不存内容）——
        # 卡死预检凭「在读新文件」识别活跃探索；审计事件失败不得影响工具返回
        try:
            try:
                shown = p.relative_to(scratch.resolve())
            except ValueError:
                try:
                    shown = p.relative_to(ws.resolve())
                except ValueError:
                    shown = p
            self.bb.append_event(
                self.project_id, "file.read",
                {"path": str(shown)[:200], "step": self._step},
                session_id=self.session_id, author=self.author)
        except Exception:  # noqa: BLE001
            pass
        return body

    # 检索时跳过的目录名（命中即剪枝，不跟随——产物/依赖/临时目录，不是侦察对象）
    _SEARCH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".build-venv",
                         "build", "dist", ".pytest_cache"}
    # 单文件读取上限：超大文件不做逐行正则（防喂入巨型单行文件造成卡顿）
    _SEARCH_MAX_FILE = 5 * 1024 * 1024

    def _tool_search_files(self, pattern: str, path: str | None = None,
                           regex: bool = True, glob: str | None = None,
                           max_results: int = 100) -> str:
        """工作区内容检索（2026-09-24）：非 shell 的 grep 替代——
        - 只搜本项目工作区内（resolve 后必须位于 ws 根之下，防越权扫宿主任意文件）；
        - 纯 Python 逐行匹配（re 正则 / 子串大小写不敏感），无命令执行面；
        - 跳过二进制类后缀与 VCS/依赖目录，单文件 ≤5MB；
        - 命中行截 300 字符，最多 300 条；另落 file.search 轻事件（D9 活跃探索识别）。"""
        if not self.artifacts_dir:
            return "[错误] 未装配工作区（artifacts_dir），search_files 不可用"
        if not isinstance(pattern, str) or not pattern:
            return "[错误] pattern 必须为非空字符串"
        ws = Path(self.artifacts_dir).parent.resolve()
        scratch = (ws / "scratch").resolve()
        root = Path(path) if path else ws
        if not root.is_absolute():
            root = scratch / root
        try:
            root = root.resolve()
            root.relative_to(ws)
        except (ValueError, OSError):
            return f"[拒绝] 只允许检索本项目工作区内路径（path={path}）"
        if not root.exists():
            return f"[错误] 路径不存在: {path}（相对 scratch 解析）"
        try:
            if regex:
                rx = re.compile(pattern)
                match = rx.search
            else:
                low = pattern.lower()
                def match(s: str) -> bool:  # type: ignore[misc]
                    return low in s.lower()
        except re.error as e:
            return f"[错误] 非法正则: {e}"
        try:
            cap = max(1, min(int(max_results), 300))
        except (TypeError, ValueError):
            cap = 100
        name_filter: Callable[[str], bool]
        if glob:
            from fnmatch import fnmatch
            name_filter = lambda n: fnmatch(n, glob)  # noqa: E731
        else:
            name_filter = lambda n: True  # noqa: E731

        files: Iterable[Path]
        if root.is_file():
            files = [root]
        else:
            files = (p for p in root.rglob("*")
                     if p.is_file()
                     and not any(part in self._SEARCH_SKIP_DIRS
                                 for part in p.relative_to(ws).parts)
                     and name_filter(p.name))
        hits: list[str] = []
        scanned = 0
        truncated = False
        for f in files:
          try:
            if f.stat().st_size > self._SEARCH_MAX_FILE:
                continue
            scanned += 1
            shown = f
            try:
                shown = f.relative_to(scratch)
            except ValueError:
                try:
                    shown = f.relative_to(ws)
                except ValueError:
                    pass
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if match(line.rstrip("\n")):
                        hits.append(f"{str(shown).replace(os.sep, '/')}:{lineno}: "
                                    f"{line.strip()[:300]}")
                        if len(hits) >= cap:
                            truncated = True
                            break
            if truncated:
                break
          except OSError:
            continue
        if not hits:
            return (f"[无命中] pattern={pattern[:100]}（扫描 {scanned} 个文件；"
                    f"范围 {str(root.relative_to(ws)) if root != ws else '.'}）")
        body = "\n".join(hits)
        if truncated or scanned:
            body += (f"\n…[共 {len(hits)} 条命中，扫描 {scanned} 个文件"
                     + ("；已达上限，缩小 path/glob 范围或加长 pattern" if truncated else "")
                     + "]")
        try:
            self.bb.append_event(
                self.project_id, "file.search",
                {"pattern": pattern[:200], "path": str(path)[:200] if path else None,
                 "hits": len(hits), "step": self._step},
                session_id=self.session_id, author=self.author)
        except Exception:  # noqa: BLE001
            pass
        return body

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
                              note: str | None = None,
                              expected_revision: int | None = None) -> str:
        a0 = self.bb.get_asset(asset_id)
        if a0 is None or a0.get("project_id") != self.project_id:
            return f"[错误] 资产不存在: {asset_id}"
        # H2 乐观锁：本地预检给独立 [冲突] 前缀（store 层仍兜底同判）
        if expected_revision is not None and \
                int(a0.get("revision") or 1) != int(expected_revision):
            return (f"[冲突] 资产已被他人修改（当前 revision={a0.get('revision')}，"
                    f"请求基于 {expected_revision}）——先 bb_query 现查再重试")
        try:
            a = self.bb.set_asset_status(asset_id, status, note=note,
                                         author=self.author,
                                         expected_revision=expected_revision)
        except ValueError as e:
            if "乐观锁冲突" in str(e):
                return f"[冲突] {e}"
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return f"asset={asset_id} status={a['status']} rev={a.get('revision')}"

    def _tool_bb_add_finding(self, vuln_class: str, title: str, severity: str = "info",
                             status: str = "unverified", evidence: dict | None = None,
                             relates_to: list | None = None,
                             target_asset_id: str | None = None,
                             poc_artifact_id: str | None = None,
                             confidence: float = 0.5, dedup_key: str | None = None,
                             rating_basis: str = "", impact: str = "",
                             remediation: str = "",
                             category: str | None = None) -> str:
        # relates_to 是顶层入参（LLM 不必懂 evidence 内部结构），并入 evidence 走
        # add_finding 的存在性/同项目校验（悬空/跨项目 ValueError → 回填错误不中断）
        ev = dict(evidence or {})
        if relates_to:
            ev["relates_to"] = relates_to
        # 发现必挂意图（任务尝试树 v2 门禁，2026-09-27）：发现是意图假设的检验
        # 产物，不允许游离——Agent（sess-）登记前本会话必须有 open 意图；
        # 人类/系统路径不经此工具，不受限。store 层不设闸（测试/人工 PATCH 零感知）。
        if self.author.startswith("sess-"):
            has_open = self.bb.conn.execute(
                "SELECT 1 FROM intents WHERE project_id=? AND status='open'"
                " AND author=? LIMIT 1", (self.project_id, self.author)).fetchone()
            if has_open is None:
                self.last_progress_step = self._step
                return ("[拒绝] 本会话当前没有 open 意图，不允许游离登记发现——"
                        "先 declare_intent(statement=\"对 <对象> 进行 <什么尝试>…\")"
                        " 声明假设，再围绕它执行并登记发现；"
                        "意图最终须 close_intent 收尾（漏洞/发现/死路）")
        # C6 漏洞核对 hook：AI 登记漏洞前自我对照红线/评级规则——
        # 不合格降级 intel（不进漏洞视图）；核对失败降级跳过（不阻断）。
        gate_note = ""
        if category == "vuln" and self.vuln_gate is not None:
            try:
                ok, reason = self.vuln_gate({
                    "title": title, "vuln_class": vuln_class, "severity": severity,
                    "status": status,
                    # has_poc=复现证据判定（收录格式新口径 repro_steps + 旧结构兼容）
                    "has_poc": has_repro_evidence(evidence, poc_artifact_id),
                    "evidence_head": json.dumps(evidence or {}, ensure_ascii=False)[:600],
                    "rating_basis": rating_basis,
                })
                gate_note = (f" 漏洞核对：{'✓ 通过' if ok else '✗ 未通过（' + reason + '）'}")
                if not ok:
                    category = "intel"  # 不符合规则 → 不进漏洞，降级有效发现
            except Exception as e:  # noqa: BLE001 —— 核对失败降级跳过
                gate_note = f" 漏洞核对跳过：{e}"
        r = self.bb.add_finding(
            self.project_id, vuln_class, title, severity=severity, status=status,
            evidence=ev, target_asset_id=target_asset_id,
            poc_artifact_id=poc_artifact_id,
            confidence=confidence, dedup_key=dedup_key, author=self.author,
            rating_basis=rating_basis, impact=impact, remediation=remediation,
            category=category, track=self.track,
        )
        self.last_progress_step = self._step
        # M5 D1 疑似重复警告（orchestrator-efficiency §0-9）：同目标同类但指纹
        # 不同——只提示不阻塞；警告进工具返回值（单次按需），不落事件无频控
        warn = ""
        dw = r.get("dedup_warning")
        if dw:
            listed = "；".join(f"{d['id']}({d['title'][:40]})" for d in dw)
            warn = (f"\n[疑似重复] 同目标已有同类发现：{listed}——"
                    "若是同一问题请 bb_update_finding 补证据而非新开条目；"
                    "确属独立问题坚持新增即可。")
        # 回显生效判级依据（合并就高后可能与本报不同），供 agent 自检
        return (f"finding={r['id']} merged={r['merged']} severity={r['severity']} "
                f"category={r.get('category') or 'vuln'} "
                f"rating_basis={r.get('rating_basis') or '(空)'}{gate_note}{warn}")

    # ---------- 意图（渗透链路图 v3，2026-09-24） ----------

    def _tool_declare_intent(self, statement: str,
                              target_asset_id: str | None = None,
                              basis_refs: list[str] | None = None) -> str:
        r = _declare_intent(self.bb, self.project_id, statement,
                            target_asset_id=target_asset_id,
                            basis_refs=basis_refs, author=self.author)
        self.last_progress_step = self._step
        if r.get("merged"):
            return (f"[复用] intent={r['id']} status=open"
                    "（同陈述意图已存在，在它下面继续执行并收尾）")
        return (f"intent={r['id']} status=open basis_refs={len(r.get('basis_refs') or [])}"
                "——围绕它执行，最终必须 close_intent 收尾")

    def _tool_close_intent(self, intent_id: str, outcome: str,
                           finding_ids: list[str] | None = None,
                           evidence_refs: list[str] | None = None,
                           dead_reason: str = "") -> str:
        r = _close_intent(self.bb, self.project_id, intent_id, outcome,
                          finding_ids=finding_ids, evidence_refs=evidence_refs,
                          dead_reason=dead_reason, author=self.author)
        self.last_progress_step = self._step
        refs = len(r.get("outcome_refs") or [])
        return f"intent={intent_id} status=closed outcome={outcome} findings={refs}"

    def _tool_reopen_intent(self, intent_id: str, note: str = "") -> str:
        r = _reopen_intent(self.bb, self.project_id, intent_id,
                           author=self.author, note=note)
        self.last_progress_step = self._step
        return f"intent={intent_id} status={r['status']}（已重开，需重新收尾）"

    def _tool_bb_update_finding(self, finding_id: str, severity: str | None = None,
                                status: str | None = None, title: str | None = None,
                                vuln_class: str | None = None,
                                evidence: dict | None = None,
                                category: str | None = None,
                                rating_basis: str | None = None,
                                impact: str | None = None,
                                remediation: str | None = None,
                                expected_revision: int | None = None) -> str:
        """修订已有发现（F11/C6）：降级、转 intel 线索、标误报等，走 patch_finding
        单一写入口（track 感知门禁——渗透/红队轨显式改 info 会被拒并回填）。"""
        old = self.bb.get_finding(self.project_id, finding_id)
        if old is None:
            return f"[错误] 发现不存在: {finding_id}"
        kwargs: dict = {}
        if severity is not None:
            kwargs["severity"] = severity
        if status is not None:
            kwargs["status"] = status
        if title is not None:
            kwargs["title"] = title
        if vuln_class is not None:
            kwargs["vuln_class"] = vuln_class
        if evidence is not None:
            kwargs["evidence"] = evidence
        if category is not None:
            kwargs["category"] = category
        if rating_basis is not None:
            kwargs["rating_basis"] = rating_basis
        if impact is not None:
            kwargs["impact"] = impact
        if remediation is not None:
            kwargs["remediation"] = remediation
        if not kwargs:
            return "[拒绝] 未提供任何要修改的字段"
        try:
            r = self.bb.patch_finding(self.project_id, finding_id,
                                      track=self.track, author=self.author,
                                      expected_revision=expected_revision, **kwargs)
        except ValueError as e:
            if "乐观锁冲突" in str(e):
                return f"[冲突] {e}"
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return (f"finding={r['id']} changed=[{','.join(kwargs)}] "
                f"severity={r['severity']} category={r.get('category') or 'vuln'} "
                f"rating_basis={r.get('rating_basis') or '(空)'} "
                f"rev={r.get('revision')}")

    def _tool_bb_delete_finding(self, finding_id: str, reason: str = "") -> str:
        """物理删除发现（垃圾/走查数据清理）：verified 拒删（先降级或人工处理）、
        reason 必填审计；误报请走 bb_update_finding（PATCH FP 触发撤回传播）。"""
        if not reason.strip():
            return "[拒绝] 删除必须说明原因（reason）"
        old = self.bb.get_finding(self.project_id, finding_id)
        if old is None:
            return f"[错误] 发现不存在: {finding_id}"
        if old["status"] == "verified":
            return "[拒绝] verified 发现不可删除——如判定误报走 bb_update_finding 改 false-positive"
        r = self.bb.delete_finding(self.project_id, finding_id, author=self.author)
        if r is None:
            return f"[错误] 发现不存在: {finding_id}"
        self.last_progress_step = self._step
        return (f"deleted={finding_id} reason={reason.strip()[:100]} "
                f"trimmed_relates_to={r.get('trimmed_relates_to', 0)} "
                f"affected_tasks={r.get('affected_tasks', 0)}")

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
        # 产物归属（工作区隔离 W3）：认领任务的产物自动挂 task_id，会话产物挂 session_id
        attribution = {"session_id": self.session_id}
        if self.current_task_id:
            attribution["task_id"] = self.current_task_id
        artifact_id = self.bb.add_artifact(self.project_id, rel, kind=kind,
                                           description=description, sha256=sha,
                                           author=self.author, meta=attribution)
        self.bb.append_event(
            self.project_id, "artifact.new",
            {"artifact_id": artifact_id, "path": rel, "kind": kind, "sha256": sha,
             **attribution},
            session_id=self.session_id, author=self.author)
        self.last_progress_step = self._step
        return f"artifact={artifact_id} path={rel} sha256={sha[:8]}"

    def _tool_bb_query(self, what: str, target_asset_id: str | None = None,
                       binary_sha256: str | None = None, address: int | str | None = None,
                       risk_tag: str | None = None, type: str | None = None,
                       status: str | None = None, blueprint_id: str | None = None,
                       min_severity: str | None = None, verified_only: bool = False,
                       category: str | None = None, tag: str | None = None,
                       kinds: list[str] | str | None = None,
                       session_id: str | None = None,
                       asset: str | None = None,
                       limit: int | None = None) -> str:
        if address is not None:
            try:
                address = _coerce_addr(address)
            except ValueError as e:
                return f"[错误] {e}"
        # 闭集校验（M1）：非法值显式报错，防拼错参数静默空结果被误判为「没有」
        if what == "findings" and min_severity is not None \
                and min_severity not in _BB_SEVERITIES:
            return (f"[错误] 非法 min_severity: {min_severity}"
                    f"（允许: {', '.join(_BB_SEVERITIES)}）")
        if what == "assets":
            if type is not None and type not in _BB_ASSET_TYPES:
                return (f"[错误] 非法 type: {type}"
                        f"（允许: {', '.join(_BB_ASSET_TYPES)}）")
            if status is not None and status not in _BB_ASSET_STATUSES:
                return (f"[错误] 非法 assets status: {status}"
                        f"（允许: {', '.join(_BB_ASSET_STATUSES)}）")
        if what == "tasks" and status is not None and status not in _BB_TASK_STATUSES:
            return (f"[错误] 非法 tasks status: {status}"
                    f"（允许: {', '.join(_BB_TASK_STATUSES)}）")
        # limit 钳 1-200（与 kb_search 同口径：非法值钳制不报错）
        if limit is not None:
            try:
                limit = max(1, min(200, int(limit)))
            except (TypeError, ValueError):
                limit = None
        # kinds 容错：模型偶发传单字符串，包成列表
        if isinstance(kinds, str):
            kinds = [kinds]
        if what == "site":
            # 单站全貌（2026-09-26）：根资产子树 + 各自发现 + 相关意图一把返回。
            # 动机：agent 曾按 type 分片全量拉（host/service/url/domain 各一把）
            # 再本地过滤，一个站点核实烧近 20 次查询——这里一次到位。
            if not asset or not str(asset).strip():
                return "[错误] site 查询必须提供 asset（根资产 id 或精确 value，host/domain）"
            assets = self.bb.list_assets(self.project_id)
            key = str(asset).strip()
            root = next((a for a in assets if a["id"] == key), None)
            if root is None:  # id 未命中 → 精确 value（大小写不敏感），host/domain 优先
                vals = [a for a in assets
                        if (a.get("value") or "").strip().lower() == key.lower()]
                vals.sort(key=lambda a: 0 if a.get("type") in ("host", "domain") else 1)
                root = vals[0] if vals else None
            if root is None:
                sample = sorted({a["value"] for a in assets
                                 if a.get("type") in ("host", "domain")})[:8]
                return (f"[错误] 找不到资产: {asset}"
                        f"（可先 what=assets 选定，host/domain 如: {', '.join(sample)}…）")
            subtree = _subtree_ids(assets, root["id"])
            site_assets = [a for a in assets if a["id"] in subtree]
            findings = [f for f in self.bb.list_findings(self.project_id)
                        if f.get("target_asset_id") in subtree]
            intents = [it for it in _list_intents(self.bb, self.project_id)
                       if _intent_in_site(it, subtree)]
            if limit is not None:  # 钳制过的 limit 分别作用于三张清单
                site_assets = site_assets[:limit]
                findings = findings[:limit]
                intents = intents[:limit]
            return json.dumps({
                "hint": "单站全貌一次拉全；核对站点现状用 what=site，勿按 type 分片全量拉。",
                "root": {"id": root["id"], "type": root["type"],
                         "value": root["value"], "status": root.get("status", "open")},
                "counts": {
                    "assets": len(site_assets), "findings": len(findings),
                    "intents": len(intents),
                    "open_intents": sum(1 for it in intents
                                        if it.get("status") == "open"),
                },
                "assets": [{"id": a["id"], "type": a["type"], "value": a["value"],
                            "parent_id": a.get("parent_id"),
                            "status": a.get("status", "open")} for a in site_assets],
                "findings": [{"id": f["id"], "title": f.get("title", ""),
                              "severity": f.get("severity", "info"),
                              "status": f.get("status", "unverified"),
                              "category": f.get("category", "vuln"),
                              "vuln_class": f.get("vuln_class", ""),
                              "target_asset_id": f.get("target_asset_id")}
                             for f in findings],
                "intents": [{"id": it["id"], "status": it.get("status", "open"),
                             "outcome": it.get("outcome_type") or None,
                             "statement": (it.get("statement") or "")[:160],
                             "target_asset_id": it.get("target_asset_id"),
                             "outcome_refs": it.get("outcome_refs") or [],
                             "dead_reason": (it.get("dead_reason") or "")[:120],
                             "created_at": it.get("created_at"),
                             "closed_at": it.get("closed_at")}
                            for it in intents],
            }, ensure_ascii=False)
        if what == "findings":
            rows = self.bb.list_findings(
                self.project_id, target_asset_id=target_asset_id,
                min_severity=min_severity, verified_only=bool(verified_only),
                category=category)
            if limit is not None:
                rows = rows[:limit]
            return json.dumps(
                [{"id": f["id"], "vuln_class": f["vuln_class"], "title": f["title"],
                  "severity": f["severity"], "status": f["status"], "confidence": f["confidence"]}
                 for f in rows], ensure_ascii=False)
        if what == "assets":
            rows = self.bb.list_assets(
                self.project_id, type_=type, status=status, tag=tag)
            if limit is not None:
                rows = rows[:limit]
            return json.dumps(
                [{"id": a["id"], "type": a["type"], "value": a["value"],
                  "parent_id": a.get("parent_id"), "status": a.get("status", "open"),
                  "meta": a.get("meta", {})}
                 for a in rows], ensure_ascii=False)
        if what == "events":
            rows = self.bb.recent_events(
                self.project_id, tail=limit if limit is not None else 50,
                kinds=kinds, session_id=session_id)
            # id/session_id/created_at 增量带出（M2）：查事件即可知归属，不必反查落盘
            return json.dumps(
                [{"id": e["id"], "kind": e["kind"], "author": e["author"],
                  "session_id": e.get("session_id"), "created_at": e["created_at"],
                  "payload": e["payload"]}
                 for e in rows], ensure_ascii=False)
        if what == "tasks":
            rows = self.tq.list_tasks(self.project_id, status=status)
            if limit is not None:
                rows = rows[:limit]
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
            if limit is not None:
                rows = rows[:limit]
            return json.dumps(
                # 地址出 API/Agent 一律 hex 字符串（§9 地址纪律，JS/模型都不丢 64 位精度）
                [{"address": hex(int(f["address"])), "name": f["name"],
                  "risk_tags": f["risk_tags"],
                  "confidence": f["confidence"]} for f in rows], ensure_ascii=False)
        if what == "blueprint":
            if blueprint_id:
                row = self.bb.get_blueprint(self.project_id, blueprint_id)
                if row is None:
                    return f"[错误] 蓝图不存在: {blueprint_id}"
                return json.dumps(row, ensure_ascii=False)
            rows = self.bb.list_blueprints(self.project_id)
            if limit is not None:
                rows = rows[:limit]
            return json.dumps(
                [{"id": b["id"], "name": b["name"], "goal": b["goal"],
                  "binary_sha256": b["binary_sha256"], "status": b["status"],
                  "modules": [{"name": m["name"], "desc": m["desc"],
                               "status": m["status"],
                               "func_addresses": m["func_addresses"]}
                              for m in b["modules"]]}
                 for b in rows], ensure_ascii=False)
        return f"[错误] 未知查询: {what}"

    def _tool_kb_open(self, module: str) -> str:
        """打开知识库模块（DESIGN.md §4/§4.5）：kb 全局单根多域解析（expert-pool M0）。

        module 全局形态 `<域>/<快照>/<路径>`（首段=启用域时锁定该域），
        域内相对形态向后兼容；writing.resolve_kb 存在性消歧+防穿越；
        不存在则按启用域列出可选清单（防幻觉猜名）。"""
        if not self.packs_root:
            return "[错误] 本会话未配置知识库（packs_root 缺失）"
        rel = module.strip().replace("\\", "/")
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            return "[拒绝] module 必须是知识库内相对路径（禁止绝对路径 / .. 穿越）"
        sources = load_kb_sources(self.packs_root, self.capabilities)
        if not sources:
            return "[错误] 启用能力域无知识库目录，本技能正文自足"
        # 全局形态首段=启用域 → 锁定该域解析（跨域同名快照天然消歧）；
        # 否则按启用域顺序逐域尝试（域内相对形态）
        doms = [s.root.name for s in sources]
        first = p.parts[0] if p.parts else ""
        try_doms = [first] if first in doms else doms
        for d in try_doms:
            try:
                t = resolve_kb(self.packs_root, d, rel)
            except Exception:  # noqa: BLE001 —— 校验类失败换下一域
                continue
            if not t.path.is_file():
                continue
            self.bb.append_event(
                self.project_id, "kb.open",
                {"source": t.source.id, "module": rel, "path": str(t.path)},
                session_id=self.session_id, author=self.author)
            self.last_progress_step = self._step
            return (f"知识库模块（域 {d}）: {t.path}\n"
                    "纪律：只 Read 上面这一个文件，按需取用；禁止通读知识库目录；"
                    "需要相关专题时按技能对照表另开对应文件（同样按需）。")
        lines = ["[防幻觉] 模块不存在: " + rel, "可用模块（照清单改选，禁止猜名）:"]
        for src in sources:
            if not src.root.is_dir():
                continue
            files = (src.root.rglob("*.md") if src.recursive
                     else src.root.glob("*.md"))
            # 排除备份/回收站（C3）：Agent 只能照清单选知识正文，防猜中 .history
            # 清单给全局形态（M0：域名打头，与 kb_open/route_index 口径一致）
            dom = src.root.name
            rels = sorted(f"{dom}/{f.relative_to(src.root).as_posix()}"
                          for f in files
                          if ".history" not in f.relative_to(src.root).parts)[:50]
            lines.append(f"- 域 {dom}: " + ("、".join(rels) if rels else "(空)"))
        return "\n".join(lines)

    def _tool_kb_search(self, query: str, limit: int = 10,
                        tag: str | None = None) -> str:
        """kb 关键词全文检索（2026-09-18 路由增强 · 方向 A）：包装 writing.search_kb
        逐启用能力包检索后合并。返回 path 即 kb_open 的 module 参数（同口径），
        落 kb.search 审计事件（与 kb.open 对称）。K5：tag 走 frontmatter 分面过滤。"""
        q = (query or "").strip()
        if not q:
            return "[拒绝] query 不能为空（可换中英文关键词各试一次）"
        if not self.packs_root:
            return "[错误] 本会话未配置知识库（packs_root 缺失）"
        try:
            limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            limit = 10
        caps = self.capabilities or []
        rows: list[dict] = []
        for cap in caps:
            try:
                for r in search_kb(self.packs_root, cap, q, limit=limit, tag=tag):
                    # M0：path 加域前缀成全局 module 形态（kb_open 直接可用）
                    r["path"] = f"{cap}/{r['path']}"
                    rows.append(r)
            except Exception:  # 单域检索失败不拖垮整体
                continue
        rows.sort(key=lambda r: (-r["matches"], r["path"]))
        rows = rows[:limit]
        self.bb.append_event(
            self.project_id, "kb.search",
            {"query": q[:200], "hits": len(rows)},
            session_id=self.session_id, author=self.author)
        if not rows:
            return (f"[无命中] 知识库中没有包含 {q!r} 的文档。"
                    "可换中英文关键词各试一次，或按技能正文对照表用 kb_open 直接开模块。")
        lines = [f"kb 检索 {q!r} 命中 {len(rows)} 篇（path 即 kb_open 的 module 参数，"
                 "先 kb_open 再 Read 细读）:"]
        for r in rows:
            snippet = (r.get("snippet") or "")[:120]
            lines.append(f"- {r['path']}（源 {r['source']}，命中 {r['matches']}）\n"
                         f"  {snippet}")
        self.last_progress_step = self._step
        return "\n".join(lines)

    def _tool_route_lookup(self, query: str, limit: int = 8) -> str:
        """测试点路由索引查询（G1，只读）：认领注入只带 Top-K 最相关条目，
        其余方向经本工具按需查——条目经角色 skills 白名单裁剪（与注入同口径）。
        命中后 Agent 用 kb_open 打开对应手册，与全表注入时代同一闭环。"""
        q = (query or "").strip()
        if not q:
            return "[拒绝] query 不能为空（可换中英文关键词各试一次）"
        if not self.packs_root:
            return "[错误] 本会话未配置 packs_root（route_lookup 不可用）"
        try:
            limit = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            limit = 8
        try:
            hits, total = top_route_entries(self.packs_root, self.capabilities,
                                            self.role_skills, query=q, top_k=limit)
        except Exception as e:  # noqa: BLE001 —— 索引坏了不影响工具返回
            return f"[错误] 路由索引读取失败（按空处理）: {e}"
        if total == 0:
            return "路由索引无本会话启用域的条目（直接按通用方法执行）"
        if not hits:
            return (f"[无命中] 索引共 {total} 条但没有匹配 {q!r} 的——"
                    "换更短/更通用的中英文关键词再试（如「upload」「提权」）。")
        lines = [f"路由索引命中 {len(hits)}/{total} 条（kb 路径用 kb_open 打开细读）:"]
        lines.extend(f"- {e.point} → kb_open(\"{e.kb}\")" for e in hits)
        self.last_progress_step = self._step
        return "\n".join(lines)

    def _tool_skill_open(self, name: str) -> str:
        """打开技能全量正文（G1 渐进披露：认领只注入目录摘要，正文按需取）。
        只放行当前 pack_set（启用能力包 ∪ 轨）内 enabled 技能；未知名回可用清单防幻觉。"""
        if not self.packs_root:
            return "[错误] 本会话未配置 packs_root（skill_open 不可用）"
        if self._skill_registry is None:
            self._skill_registry = SkillRegistry(self.packs_root)
            self._skill_registry.load()
        assert self._skill_registry is not None
        sk = self._skill_registry.get((name or "").strip())
        pack_set = set(self.capabilities) | {self.track} if self.track else set(self.capabilities)
        if sk is None or sk.pack not in pack_set or not sk.enabled:
            names = sorted(s.name for s in self._skill_registry.all()
                           if s.pack in pack_set and s.enabled)
            return ("[防幻觉] 技能不存在或不可用: " + (name or "")
                    + "\n可用技能: " + ("、".join(names) if names else "（无）"))
        try:
            body = sk.body()
        except OSError as e:
            return f"[错误] 技能正文读取失败: {e}"
        self.bb.append_event(
            self.project_id, "skill.open",
            {"name": sk.name, "pack": sk.pack, "chars": len(body)},
            session_id=self.session_id, author=self.author)
        self.last_progress_step = self._step
        return (f"技能 {sk.name}（{sk.pack}）全量正文:\n\n{body}")

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

    def _tool_bb_blueprint_create(self, name: str, goal: str = "",
                                  binary_sha256: str = "",
                                  modules: list[dict] | None = None) -> str:
        try:
            r = self.bb.create_blueprint(
                self.project_id, name, goal=goal, binary_sha256=binary_sha256,
                modules=modules, author=self.author)
        except ValueError as e:
            return f"[错误] {e}"
        self.last_progress_step = self._step
        names = "、".join(m["name"] for m in r["modules"])
        mod_note = f"（{names}）" if names else ""
        return (f"blueprint={r['id']} status=draft modules={len(r['modules'])}{mod_note}"
                "——继续 bb_blueprint_update 补 spec/notes，整体 status 由人类流转")

    def _tool_bb_blueprint_update(self, blueprint_id: str,
                                  module_name: str | None = None,
                                  spec: str | None = None,
                                  notes: str | None = None,
                                  desc: str | None = None,
                                  func_addresses: list[str] | None = None,
                                  module_status: str | None = None,
                                  modules_set: list[dict] | None = None,
                                  content_append: str | None = None,
                                  content_md: str | None = None) -> str:
        try:
            if module_name is not None:
                kw: dict[str, Any] = {}
                if spec is not None:
                    kw["spec"] = spec
                if notes is not None:
                    kw["notes"] = notes
                if desc is not None:
                    kw["desc"] = desc
                if func_addresses is not None:
                    kw["func_addresses"] = func_addresses
                if module_status is not None:
                    kw["status"] = module_status
                r = self.bb.update_blueprint_module(
                    self.project_id, blueprint_id, module_name,
                    author=self.author, **kw)
            else:
                if module_status is not None:
                    return ("[拒绝] 蓝图整体 status 不归 Agent 管"
                            "（module_status 才是模块级状态，需配 module_name）")
                r = self.bb.update_blueprint_content(
                    self.project_id, blueprint_id, author=self.author,
                    content_md=content_md if content_md is not None else UNSET,
                    content_append=content_append if content_append is not None else UNSET,
                    modules_set=modules_set if modules_set is not None else UNSET)
        except LookupError as e:
            return f"[错误] {e}"
        except ValueError as e:
            return f"[错误] {e}"
        if r is None:
            return f"[错误] 蓝图不存在: {blueprint_id}"
        self.last_progress_step = self._step
        return f"blueprint={r['id']} updated（modules={len(r['modules'])}）"

    def _finish_or_report_deleted(self, finish, verb: str) -> str:
        """收尾委托；委托已被人类物理删除（§6.4）→ 友好提示，会话照常转向下一委托。"""
        tid = self.current_task_id
        try:
            finish(tid, self.session_id, persona_role=self.current_persona_role)
        except ValueError:
            if self.tq.get_task(tid) is not None:
                raise
            self.current_task_id = None
            self.last_progress_step = self._step
            self.delegation_just_finished = True
            return f"task={tid} 已被人类删除，无需收尾，继续认领下一个委托"
        self.current_task_id = None
        self.last_progress_step = self._step
        self.delegation_just_finished = True
        return f"task={tid} {verb}"

    # ---------- 计划（A2 先规划后动手） ----------

    @staticmethod
    def _render_plan(plan: list[dict[str, Any]]) -> str:
        icon = {"todo": "○", "doing": "▶", "done": "●", "blocked": "■"}
        return "\n".join(
            f"  {icon.get(s.get('status'), '?')} {s['id']} {s['title']}"
            + (f"⇐ {','.join(s['refs'])}" if s.get("refs") else "")
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
        except ValueError as e:
            # 意图接地：refs 悬空/格式坏（服务端机制级校验，2026-09-20）
            return f"[拒绝] 计划步接地校验未通过：{e}"
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

    def _tool_task_reconcile(self, item_id: int, state: str, note: str = "") -> str:
        """⑤ 任务完成对账：逐条置验收条目状态（met/failed/blocked）。
        全部收口前 complete_task 被硬拦（tasks.py _check_reconcile）。"""
        if not self.current_task_id:
            return "[错误] 当前没有认领的任务"
        try:
            entries = self.tq.set_reconcile_state(
                self.current_task_id, self.session_id, int(item_id), state, note)
        except ClaimError as e:
            return f"[拒绝] {e}"
        except ValueError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        label = {"met": "✅ 已达成", "failed": "❌ 未达成", "blocked": "⛔ 受阻"}
        done = sum(1 for e in entries if e["state"] != "pending")
        rows = "\n".join(
            f"  {label.get(e['state'], e['state'])} #{e['id']} {e['text']}"
            + (f"（{e['note']}）" if e["note"] else "")
            for e in entries)
        return f"验收条目 #{item_id} → {label.get(state, state)}。已收口 {done}/{len(entries)}：\n{rows}"

    # ---------- 分解派活（A5：子代理发布子任务） ----------

    def _tool_publish_task(self, objective: str, task_type: str = "generic",
                           role: str = "",
                           scope: str = "", refs: list[str] | None = None,
                           noise_budget: str = "passive", priority: int = 2,
                           conflict_keys: list[str] | None = None,
                           workset: list[str] | None = None) -> str:
        """子代理分解子任务（A5）：parent 强制当前任务、created_by 强制本会话。

        入参里不接受 parent_id/created_by——分解关系由服务端按会话状态钉死，
        与撤回传播的 is_session 识别一致（created_by=sess-…）。
        机制 1.1：发布前按指纹查重，命中 open/claimed 同目标任务 → 复用不新建（防重复派活）。
        v14 双闸：分解深度 1 层（对称编排器）+ 每父任务子任务 ≤3（超限回填拒绝，
        防模型偷懒层层下包）；同 target 防碎闸内核同样生效（ValueError 统一回填）。
        role（v14）：建议认领角色，认领会话按该角色换装执行。
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
                allowed_types=self.allowed_task_types, refs=refs, workset=workset,
                role=role,
                # expert-pool M2（§4.6）：绑定专家清单优先（工厂注入）；
                # 未注入=按轨过滤的池内专家（等价退役前 list_roles 行为）
                allowed_roles=(self.allowed_roles
                               or (list_experts(self.packs_root, self.track)
                                   if self.packs_root and self.track else None)),
                parent_depth_limit=1,             # v14：分解深度 1 层（子任务不可再拆）
                max_children_per_parent=3,        # v14：每父任务子任务上限（防偷懒下包）
            )
        except ValueError as e:  # TaskTypeError / 超限 / 噪声冲突键 / 非法 role / 键归一化失败等
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        parent = self.current_task_id or "(无)"
        return (f"子任务已发布: {task_id}（parent={parent}，type={task_type}，"
                f"P{priority}，{noise_budget}）。系统将自动为该任务建立专属执行窗"
                "（调度器按建议角色装配并按并发上限起跑）；"
                "你继续推进当前任务，不要自己抢领（当前任务收尾后若仍 open 才可认领）。")

    def _tool_bb_notify(self, text: str, to_session: str = "", to_role: str = "",
                        kind: str = "intel", refs: list[str] | None = None) -> str:
        """bb_notify（2026-09-20 会话窗对话化，§17 B1 挂账落地）：Agent 私信其他窗。
        to_session 定向优先；否则 to_role 广播该角色全部活跃窗（不含自己）；
        两者皆空 [错误]。不设频率硬限额（定稿约束）——滥用经 tool.call 审计可见、
        人类直播间直接处置。ValueError（会话不存在/未知分类）回填 [错误] 不中断。"""
        text = (text or "").strip()
        if not text:
            return "[错误] text 不能为空"
        try:
            if to_session:
                r = self.bb.post_agent_message(
                    self.project_id, self.session_id, to_session, kind, text, refs=refs)
                if r is None:
                    return f"[错误] 收件会话 {to_session} 已关闭，私信未送达"
                return f"已送达 {to_session}（{kind}）msg={r['id']}"
            if to_role:
                targets = self.bb.list_active_sessions_by_role(self.project_id, to_role)
                ok, skip = 0, 0
                for t in targets:
                    sid_ = t["id"]
                    if sid_ == self.session_id:
                        continue  # 不自寄
                    if self.bb.post_agent_message(
                            self.project_id, self.session_id, sid_, kind, text,
                            refs=refs) is None:
                        skip += 1  # 目标在遍历间隙被关闭：跳过不失败
                    else:
                        ok += 1
                if ok == 0:
                    return f"[错误] 角色 {to_role} 无活跃收件窗（或全部已关闭）"
                return (f"已广播角色 {to_role}（{kind}）：送达 {ok} 窗"
                        + (f"，跳过 {skip}" if skip else ""))
            return "[错误] to_session 与 to_role 至少填一项"
        except ValueError as e:
            return f"[错误] {e}"

    def _close_browser_session(self) -> None:
        """F6-v3：任务结束自动清除本会话的浏览器 Page（下次工具调用
        _browser_pair 幂等重建）。收尾路径任何异常静默——绝不炸主循环。"""
        try:
            if self.browser is not None:
                self.browser.get_instance(self.project_id).close_session(self.session_id)
        except Exception:  # noqa: BLE001
            pass

    # ---------- D6 收尾收敛确认（stuck-convergence，2026-09-23） ----------

    def reset_closing(self) -> None:
        """收尾确认状态随任务复位（_loop_body 开头调用）。"""
        self.closing_round = 0
        self.closing_last_progress = 0

    def _closing_event(self, round_no: int, result_note: str,
                       pathway: str = "") -> None:
        """确认轮留痕（任务仍 claimed、事件照常落库，可观测）。"""
        try:
            self.bb.append_event(
                self.project_id, "agent.closing_confirm",
                {"session_id": self.session_id, "task_id": self.current_task_id,
                 "round": round_no, "step": self._step,
                 "pathway": pathway, "result_note_head": (result_note or "")[:200]},
                session_id=self.session_id, author=self.author)
        except Exception:  # noqa: BLE001 —— 留痕失败不影响确认流程
            pass

    def _closing_gate(self, result_note: str) -> str | None:
        """complete 闸：返回非空=本次申报被拦截（文本回填模型）；None=放行真收尾。

        - 首次申报（round=0）：最近 ``dry_tail_steps`` 步零新增（干尾巴）→ 直接
          放行（常态任务零额外成本）；否则进入确认轮 1，注入产出清单。
        - 确认轮内再申报：该轮零新增黑板写入（以 last_progress_step 未推进为
          等价机械信号，非模型自报）→ 饱和放行；有新增且未到 2 轮上限 → 再确认
          一轮；已到上限 → 强制放行（封顶 2 轮，宁结束不烧）。"""
        if self.closing_round == 0:
            # D10 cap=0（2026-09-24）：必须在干尾巴判定之前——否则 cap=0 不产生
            # 真"零确认"语义（首次申报仍可能被拦进确认轮）
            if self.closing_max_rounds <= 0:
                self._closing_event(0, result_note, pathway="cap_zero")
                return None
            if self._step - self.last_progress_step >= self.dry_tail_steps:
                self._closing_event(0, result_note, pathway="dry_tail")
                return None
            self.closing_round = 1
            self.closing_last_progress = self.last_progress_step
            self._closing_event(1, result_note)
            return self._closing_prompt()
        if self.last_progress_step <= self.closing_last_progress:
            self._closing_event(self.closing_round, result_note,
                                pathway="zero_new")
            return None
        if self.closing_round >= self.closing_max_rounds:
            self._closing_event(self.closing_round, result_note,
                                pathway="round_cap")
            return None
        self.closing_round += 1
        self.closing_last_progress = self.last_progress_step
        self._closing_event(self.closing_round, result_note, pathway="new_output")
        return self._closing_prompt()

    def _closing_prompt(self) -> str:
        """收尾确认清单：findings/assets/artifacts + plan 完成度（全黑板现成数据）。"""
        pid = self.project_id
        tid = self.current_task_id
        sections: list[str] = []
        try:
            findings = self.bb.list_findings(pid)[:15]
            if findings:
                rows = "\n".join(
                    f"  - [{f.get('status', '?')}/{f.get('severity', '?')}] "
                    f"{f.get('title', '')[:80]}（{f.get('id')}）"
                    for f in findings)
                sections.append(f"已登记发现（{len(findings)}，最新在前）：\n{rows}")
        except Exception:  # noqa: BLE001
            pass
        try:
            assets = self.bb.list_assets(pid)[:15]
            if assets:
                rows = "\n".join(
                    f"  - {a.get('type', '?')} {a.get('value', '')[:80]}"
                    f"（{a.get('id')}）"
                    for a in assets)
                sections.append(f"已登记资产（{len(assets)}）：\n{rows}")
        except Exception:  # noqa: BLE001
            pass
        try:
            artifacts = self.bb.list_artifacts(pid, task_id=tid)[:10]
            if artifacts:
                rows = "\n".join(
                    f"  - {a.get('kind', '?')} "
                    f"{(a.get('description') or os.path.basename(a.get('path', '')))[:80]}"
                    f"（{a.get('id')}）"
                    for a in artifacts)
                sections.append(f"本任务产物（{len(artifacts)}）：\n{rows}")
        except Exception:  # noqa: BLE001
            pass
        plan_line = "计划完成度：本任务未登记计划"
        try:
            task = self.tq.get_task(tid) if tid else None
            plan = (task or {}).get("plan") or []
            if plan:
                done = sum(1 for s in plan if s.get("status") == "done")
                blocked = sum(1 for s in plan if s.get("status") == "blocked")
                icon = {"todo": "○", "doing": "▶", "done": "●", "blocked": "■"}
                rows = "\n".join(
                    f"  {icon.get(s.get('status'), '?')} {s.get('id')} "
                    f"{s.get('title', '')[:60]}" for s in plan)
                plan_line = (f"计划完成度：{done}/{len(plan)} done"
                             + (f"，{blocked} blocked" if blocked else "")
                             + f"\n{rows}")
        except Exception:  # noqa: BLE001
            pass
        inventory = "\n\n".join(sections) if sections else "（当前黑板无已登记产出）"
        return (
            f"[收尾确认 · 第 {self.closing_round}/{self.closing_max_rounds} 轮]\n"
            "对照任务目标，以下是已登记的全部产出：\n"
            f"{inventory}\n\n{plan_line}\n\n"
            "请自查：对照目标还有遗漏的攻击面、证据或验收项吗？\n"
            "- 有遗漏 → 直接继续调用工具补齐（确认轮内有新黑板写入即视为续干，"
            "完成后重新申报，进入下一轮确认）。\n"
            "- 无遗漏 → 再次调用 complete_task（一轮零新增即落定 done）。\n"
            f"确认轮最多 {self.closing_max_rounds} 轮；期间任务仍 claimed，事件照常记录。")

    def _tool_complete_task(self, result_note: str) -> str:
        if not self.current_task_id:
            return "[错误] 当前没有认领的任务"
        # D6：未饱和先对照清单确认，干尾巴/零新增/封顶才放行真收尾
        gate = self._closing_gate(result_note)
        if gate is not None:
            return gate
        self.reset_closing()
        tid = self.current_task_id
        result = self._finish_or_report_deleted(
            lambda t, sid, **kw: self.tq.complete(t, sid, result_note, **kw), "已完成")
        if result.startswith("task="):
            self.last_delegation_note = result_note[:2000]
        self._close_browser_session()
        # v0.65 沉淀飞轮：任务 done → 复盘验证过的有效手法，自动产提案草稿
        # （hook 内部全静默，任何失败不影响收尾回执）
        if result.startswith("task=") and self.sediment_hook is not None:
            try:
                self.sediment_hook(tid, result_note)
            except Exception:  # noqa: BLE001
                pass
        # ⑥ 战役记忆：done 并联沉淀打法进全局库（跨项目召回）；失败绝不影响收尾
        if result.startswith("task=") and self.campaign_hook is not None:
            try:
                self.campaign_hook(tid, result_note)
            except Exception:  # noqa: BLE001
                pass
        # C6 生命周期：任务 done → 任务键断点快照消费完毕，清理防孤儿
        try:
            from core.agent.loop import clear_task_resume  # 延迟导入避开 tools↔loop 环
            clear_task_resume(self.artifacts_dir, tid)
        except Exception:  # noqa: BLE001 —— 清理失败只留垃圾文件
            pass
        return result

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
        tid = self.current_task_id  # 先取（finish 内部清 current_task_id）
        result = self._finish_or_report_deleted(
            lambda t, sid, **kw: self.tq.fail(t, sid, result_note, **kw), "已标记失败")
        if result.startswith("task="):
            self.last_delegation_note = result_note[:2000]
        self._close_browser_session()  # awaiting_human 不清（断点续跑保页面现场）
        # experience-sedimentation M1：真失败（error）→ 失败模式复盘（只提炼避坑
        # 教训）；aborted/awaiting_human 分支在上方即返回，天然不进复盘。
        if result.startswith("task=") and self.sediment_hook is not None:
            try:
                self.sediment_hook(tid, result_note, failure=True)
            except Exception:  # noqa: BLE001
                pass
        return result

    def _tool_finish(self, summary: str) -> str:
        # 意图纪律①（宁严勿松）：有未收尾意图首次 finish 拦截，列清单要求收尾；
        # 二次 finish 放行（客观无法收尾时须在 summary 写明）。
        if not self._finish_open_intents_ack:
            try:
                from core.blackboard.intents import list_intents as _list_open
                open_intents = _list_open(self.bb, self.project_id, status="open")
            except Exception:  # noqa: BLE001
                open_intents = []
            if open_intents:
                self._finish_open_intents_ack = True
                lines = "\n".join(
                    f"- {it['statement']}（{it['id']}）"
                    for it in open_intents[:10])
                return (f"[拒绝] 还有 {len(open_intents)} 个意图未收尾，不能结束会话：\n"
                        f"{lines}\n逐个 close_intent 收尾（漏洞/发现/死路）；确有客观原因"
                        "无法收尾，再次 finish 并在 summary 写明原因。")
        self.finished = True
        self.summary = summary
        self._close_browser_session()
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

    def _tool_request_escalation(self, cmd: str, runtime: str, reason: str,
                                 threat_class: str = "trusted",
                                 net: str | None = None,
                                 timeout: float | None = None) -> str:
        """H3 deny-driven 一次性升级（借鉴 dsh escalation）：仅受理两类——
        ①net=real（真实网络，网关本就要求审批）；②runtime 超角色 max_runtime
        软上限（§6.6 越界走审批）。工作区隔离/隔离等级/限速是红线或自助项不受理。
        审批批准后由 API 层执行一次（gateway 消费 approval → consumed），结果经
        收件箱 escalation_result 回流。"""
        if not reason.strip():
            return "[拒绝] 必须说明升级理由（reason）——审批人只看得到它"
        if threat_class not in ("trusted", "untrusted"):
            return "[拒绝] threat_class 只收 trusted/untrusted（malware_live 不开放升级）"
        deny = self.gateway.would_deny(cmd, runtime, threat_class=threat_class,
                                       net=net,
                                       workspace=(Path(self.artifacts_dir).parent
                                                  if self.artifacts_dir else None))
        if deny:
            if deny.startswith("工作区隔离"):
                return (f"[拒绝] 工作区隔离是红线不可升级：{deny}")
            if deny.startswith("限速纪律"):
                return (f"[拒绝] 限速拒绝可自助解决（拒因自带放行参数），不允许升级：{deny}")
            return (f"[拒绝] 隔离等级策略是红线（宁严勿松），不可升级：{deny}")
        eff_net = net or ("none" if runtime == "sandbox" else "bridge")
        role_escalation = (self.max_runtime is not None and runtime in RUNTIME_RANK
                           and RUNTIME_RANK[runtime] > RUNTIME_RANK[self.max_runtime])
        if eff_net != "real" and not role_escalation:
            return "[拒绝] 该命令当前策略允许且未超角色上限——直接 run_cmd 即可，无需升级。"
        kind = "net_real" if eff_net == "real" else "role_runtime"
        appr = self.bb.request_approval(
            self.project_id,
            {"op": "escalation", "kind": kind, "cmd": cmd, "runtime": runtime,
             "threat_class": threat_class, "net": eff_net,
             "reason": reason.strip()[:500], "task_id": self.current_task_id},
            risk="high" if kind == "net_real" else "medium",
            requested_by=self.author, session_id=self.session_id)
        return (f"[已提交审批] approval_id={appr['id']}（{kind}，risk={appr['risk']}）："
                f"{cmd}\n人类批准后命令只执行一次，结果将投递回你的收件箱"
                "（🛫 升级命令已执行）；等待期间可继续其他无依赖工作。")

    _AUTH_KINDS = ("scope_expand", "impact_escalate", "rating_override")

    def _tool_request_authorization(self, kind: str, scope_request: str,
                                    justification: str,
                                    evidence_finding_ids: list | None = None) -> str:
        """申请行为边界授权（M5 D2，orchestrator-efficiency §0-10）：三类行为边界
        申请——scope_expand（扩大授权目标）/ impact_escalate（影响证明升级）/
        rating_override（突破收录口径）。恒人类决策（L2 也不自动批）；批准后
        **无平台动作**（scope_expand 批准后自行 bb_add_asset 登记，rating_override
        批准后按更高口径重新登记/patch），回执经收件箱回流。这是「打不上去」的
        显式出口——替代默默死路记账。"""
        if kind not in self._AUTH_KINDS:
            return f"[拒绝] 非法 kind: {kind}（只收 {'/'.join(self._AUTH_KINDS)}）"
        if not scope_request.strip():
            return "[拒绝] 必须写清 scope_request（申请什么：新目标/升级等级/口径）"
        if not justification.strip():
            return "[拒绝] 必须说明 justification——审批人主要看它"
        appr = self.bb.request_approval(
            self.project_id,
            {"op": "authorization", "kind": kind,
             "scope_request": scope_request.strip()[:500],
             "justification": justification.strip()[:1000],
             "evidence_finding_ids": [str(x) for x in (evidence_finding_ids or [])][:10],
             "task_id": self.current_task_id, "session_id": self.session_id},
            risk="high", requested_by=self.author, session_id=self.session_id)
        hint = {"scope_expand": "批准后请自行 bb_add_asset 登记新目标（登记后即可按预算作业）",
                "impact_escalate": "批准后按批准的影响证明等级继续作业",
                "rating_override": "批准后按批准的口径重新登记/patch 该发现"}[kind]
        return (f"[已提交审批] approval_id={appr['id']}（authorization/{kind}，risk=high）\n"
                f"{hint}；结果将投递回你的收件箱，等待期间可继续其他无依赖工作。")

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

    _DECOMPILER_UNWIRED = "[未装配] 本会话未接入反编译服务（DecompilerService），请用 run_cmd 手动静态分析"

    @staticmethod
    def _decompile_result_ok(result: str) -> bool:
        """服务层成功=JSON 数据/伪码文本；引导文本以 [反编译器不可用]/[错误] 开头。"""
        return not result.startswith(("[反编译器不可用]", "[错误]"))

    def _tool_list_symbols(self, binary: str, name_contains: str | None = None,
                           min_size: int | None = None) -> str:
        if self.decompiler is None:
            return self._DECOMPILER_UNWIRED
        result = self.decompiler.list_functions(binary, name_contains=name_contains,
                                                min_size=min_size)
        if self._decompile_result_ok(result):
            self.last_progress_step = self._step
        return result

    def _tool_strings_search(self, binary: str, pattern: str | None = None,
                             limit: int | None = None) -> str:
        if self.decompiler is None:
            return self._DECOMPILER_UNWIRED
        result = self.decompiler.strings_for(binary, q=pattern, limit=limit or 200)
        if self._decompile_result_ok(result):
            self.last_progress_step = self._step
        return result

    def _tool_func_xrefs(self, binary: str, name: str | None = None,
                         address: int | str | None = None) -> str:
        if self.decompiler is None:
            return self._DECOMPILER_UNWIRED
        if address is not None:
            try:
                address = _coerce_addr(address)
            except ValueError as e:
                return f"[错误] {e}"
        result = self.decompiler.xrefs_for_func(binary, address=address, name=name)
        if self._decompile_result_ok(result):
            self.last_progress_step = self._step
        return result

    # ---------- 内置浏览器（F6，DESIGN §7；装配点 app 层 _agent_factory 轨门控） ----------

    _NO_BROWSER_TOOL = ('[no-tool] 浏览器能力未接入。仅渗透/红队/CTF 轨启用（v0.70）；依赖缺失时请人工执行：'
                        'pip install -e ".[browser]" && playwright install chromium，'
                        '重启平台后可用（当前可改用 run_cmd curl）')

    def _browser_pair(self):
        """未装配/依赖缺失返回 (None, 提示文本)；否则 (BrowserInstance, sid)。"""
        if self.browser is None:
            return None, self._NO_BROWSER_TOOL
        from core.browser.pool import browser_available
        if not browser_available():
            return None, self._NO_BROWSER_TOOL
        inst = self.browser.get_instance(self.project_id)
        inst.open_session(self.session_id, self.session_id)
        inst.set_task_id(self.session_id, self.current_task_id)
        return inst, self.session_id

    def _tool_browser_navigate(self, url: str) -> str:
        inst, sid = self._browser_pair()
        if inst is None:
            return sid  # 提示文本
        from core.browser.pool import BrowserError
        try:
            r = inst.navigate(sid, url)
        except BrowserError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return (f"已导航: {r['final_url']} 标题={r.get('title') or '-'} "
                f"状态={r.get('status')} 目标host={r['target_host']} "
                f"耗时={r['duration_ms']}ms\n"
                "页面流量已入抓包历史（人类可查看/重发）；"
                "可用 browser_content 提取渲染后文本、browser_screenshot 留证。")

    def _tool_browser_click(self, selector: str | None = None,
                            x: float | None = None, y: float | None = None) -> str:
        inst, sid = self._browser_pair()
        if inst is None:
            return sid
        from core.browser.pool import BrowserError
        try:
            r = inst.act(sid, "click", selector=selector, x=x, y=y)
        except BrowserError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return f"已点击: 当前页 {r['final_url']} 标题={r.get('title') or '-'}"

    def _tool_browser_type(self, selector: str, text: str) -> str:
        inst, sid = self._browser_pair()
        if inst is None:
            return sid
        from core.browser.pool import BrowserError
        try:
            r = inst.act(sid, "type", selector=selector, text=text)
        except BrowserError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return f"已填入 {selector}（文本已审计脱敏）；当前页 {r['final_url']}"

    def _tool_browser_back(self) -> str:
        inst, sid = self._browser_pair()
        if inst is None:
            return sid
        from core.browser.pool import BrowserError
        try:
            r = inst.act(sid, "back")
        except BrowserError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return f"已后退: 当前页 {r['final_url']} 标题={r.get('title') or '-'}"

    def _tool_browser_content(self) -> str:
        inst, sid = self._browser_pair()
        if inst is None:
            return sid
        from core.browser.pool import BrowserError
        try:
            r = inst.act(sid, "content")
        except BrowserError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        head = r.get("content") or ""
        note = "\n[截断]" if r.get("truncated") else ""
        return f"当前页 DOM 文本{note}:\n{head}"

    def _tool_browser_screenshot(self) -> str:
        inst, sid = self._browser_pair()
        if inst is None:
            return sid
        from core.browser.pool import BrowserError
        try:
            png = inst.screenshot(sid)
        except BrowserError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        rel = None
        if self.artifacts_dir:
            import hashlib as _hashlib
            import time as _time
            out_dir = Path(self.artifacts_dir) / "browser-shots"
            out_dir.mkdir(parents=True, exist_ok=True)
            base = _time.strftime("%Y%m%d-%H%M%S")
            path = out_dir / f"{base}.png"
            n = 2
            while path.exists():
                path = out_dir / f"{base}-{n}.png"
                n += 1
            path.write_bytes(png)
            sha = _hashlib.sha256(png).hexdigest()
            rel = f"browser-shots/{path.name}"
            attribution = {"session_id": self.session_id}
            if self.current_task_id:
                attribution["task_id"] = self.current_task_id
            artifact_id = self.bb.add_artifact(
                self.project_id, rel, kind="screenshot", description="浏览器截图",
                sha256=sha, author=self.author, meta=attribution)
            self.bb.append_event(
                self.project_id, "artifact.new",
                {"artifact_id": artifact_id, "path": rel, "kind": "screenshot",
                 "sha256": sha, **attribution},
                session_id=self.session_id, author=self.author)
        size_kb = len(png) // 1024
        if rel:
            return f"截图已落产物: artifact path={rel} size={size_kb}KB（浏览器页可查看）"
        return f"截图完成 size={size_kb}KB（会话未配置产物目录，未落盘）"


# tool.call 成败判定：失败回填的已知前缀（bb_query 空结果返回 JSON "[]" 也以 "[" 开头，
# 故不能用 startswith("[") 一刀切；[无命中] 是合法空检索结果，不算失败）
_TOOL_FAIL_PREFIXES = ("[错误]", "[拒绝]", "[越界拒绝]", "[计划闸]",
                       "[网关拒绝]", "[工具异常]", "[防幻觉]", "[冲突]",
                       "[参数格式]")

# E2 拒绝熔断检测集（orchestrator-efficiency，2026-09-22；2026-09-24 口径重构）：
# 拒绝分两类——
# - 教练类（计划闸）：提示先写计划，按模型步累计，走专项强提示→工具面收缩，
#   不进硬熔断（plan-gate-breaker-refine）。
# - 硬拒绝类（越界/网关/服务端拒收）：连续 ≥3 个**模型步**含硬拒绝 = 撞闸循环
#   （行为错误），熔断挂起等人工（loop.py _REJECT_BREAK_LIMIT）。
# 区别于 [错误]（业务正常失败，文件不存在等不熔断）。
# **新增拒绝路径必须用这几个前缀回填**（统一前缀纪律），私开新前缀会绕过熔断与
# 审计判定。
PLAN_GATE_PREFIX = "[计划闸]"
HARD_REJECT_PREFIXES = ("[越界拒绝]", "[网关拒绝]", "[拒绝]")
REJECT_PREFIXES = (PLAN_GATE_PREFIX,) + HARD_REJECT_PREFIXES


def _truncate_args(args: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    """tool.call 审计入参截断：长字符串值截 limit 字符防事件表膨胀（完整值在展开
    的工具结果与业务数据里，事件只留定位线索）。结构异常时兜底字符串化。"""
    try:
        return {k: (f"{v[:limit]}…(截断)" if isinstance(v, str) and len(v) > limit
                    else v) for k, v in args.items()}
    except Exception:  # noqa: BLE001
        return {"_raw": str(args)[:500]}


def _repair_bare_hex_json(s: str) -> str | None:
    """容错修复 LLM 工具参数最常见的一类非法 JSON：字符串外裸十六进制（实测
    ark-code-latest 退化输出 `"address":0x401160` 未加引号）。逐字符扫，只改
    字符串外的 0x… 字面量（转十进制 int，address 类入参本就收 int|str，语义
    等价）；其余语法错误不动。没有改动返回 None（表示修不了）。"""
    out: list[str] = []
    i, n = 0, len(s)
    in_str = False
    changed = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        m = re.match(r"0[xX][0-9a-fA-F]+", s[i:])
        if m and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] in '_."')):
            out.append(str(int(m.group(0), 16)))
            changed = True
            i += len(m.group(0))
            continue
        out.append(c)
        i += 1
    return "".join(out) if changed else None


def _loads_lenient_json(s: str) -> Any | None:
    """json.loads 失败时做一轮已知退化修复再试（当前只治裸十六进制）；
    仍失败返回 None。"""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fixed = _repair_bare_hex_json(s)
    if fixed is None:
        return None
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


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
