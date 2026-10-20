"""Agent 工具面（DESIGN.md §3）。

Agent 没有裸 shell：run_cmd 经 gateway；黑板读写走 Blackboard；
任务认领/收尾走 TaskQueue。所有工具结果以文本回填（tool_result）。
"""

import hashlib
import inspect
import json
import logging
import os
import platform
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

from core.agent.retention import omitted_note, retain, spill_text
from core.agent.session_state import SessionState, state_proxy
# 工具唯一真相源（tool-registry，2026-10-03）：name/description/input_schema/group/
# flags 一处定义在 tool_registry.py，此处只 import 派生常量（导入名逐字保留，
# 外部消费方零改动）+ 本文件补 `_tool_<name>` 实现。
from core.agent.tool_registry import (AGENT_TOOLS, TOOL_GROUPS, _ADDR_DESC,
                                      _BB_QUERY_WHAT_FULL, _COLLAB_TOOLS,
                                      _CONTROL_TOOLS, _FILE_TOOLS,
                                      _INTENT_FLOW_TOOLS, _INTENT_PRE_ALLOWED,
                                      _KNOWLEDGE_EXTRA, _PLAN_PRE_ALLOWED,
                                      _PLAN_TOOLS, _SPILL_SKIP, REGISTRY,
                                      agent_tool_group, bb_query_what_for)
from core.blackboard import Blackboard
from core.blackboard.assets import register_asset, register_challenge
from core.blackboard.attackpath import _intent_in_site, _subtree_ids
from core.blackboard.intents import (declare_intent as _declare_intent,
                                     close_intent as _close_intent,
                                     delete_intent as _delete_intent,
                                     list_intents as _list_intents,
                                     reopen_intent as _reopen_intent)
from core.blackboard.store import UNSET, has_repro_evidence
from core.ctf import flagjudge, observe
from core.diagnostics.failog import record_tool_failure
from core.dimensions import load_track_dimensions
from core.runtime.gateway import ExecutionGateway, GatewayDenied
from core.runtime.recovery import (
    EnvironmentIssue,
    EnvironmentRecovery,
    EnvironmentUnavailable,
    RecoveryRequest,
    environment_issue_from_dict,
)
from core.runtime.policy import allowed_runtimes
from core.scope_policy import check_command_scope, check_project_scope
from core.skills import proposals
from core.skills.proposals import ProposalError

log = logging.getLogger(__name__)
from core.skills.registry import SkillRegistry
from core.skills.experts import list_experts
from core.skills.routeindex import top_route_entries
from core.skills.rules import load_kb_sources
from core.skills.refs import normalize_module
from core.skills.writing import resolve_kb, search_kb

# 意图内容按轨提示（2026-10-01，不硬拦——只给方向，靠模型自觉）：
# 同一条「目标→意图→发现」链路在不同轨登记不同内容的意图；给出本轨该写什么，
# 避免跨轨语料混入（逆向轨别写「SQL 注入假设」这种）。
_INTENT_SCOPE_HINTS = {
    "pentest": "本轨意图=攻击面/漏洞假设/利用路径（如「验证 /admin 是否未授权访问」）。",
    "redteam": "本轨意图=突破路径/权限提升/横向假设（如「验证 ssrf 能否打元数据取凭证」）。",
    "reverse": "本轨意图=函数/协议/样本行为假设（如「验证 sub_401000 是否为解密例程」）。",
    "research": "本轨意图=研究问题/验证点（如「验证该字段是否参与签名计算」）。",
    "ctf": "本轨意图=解题假设/突破口（如「验证该输入是否触发栈溢出」）。",
}


def _intent_scope_hint(track: str | None) -> str:
    hint = _INTENT_SCOPE_HINTS.get((track or "").strip())
    return ("\n[意图口径] " + hint) if hint else ""


# 资产越界闸（agent-path-intent-loop M3，2026-10-09）：被主控分配资产的子代理
# （ToolDispatcher.assigned_asset_ids 非空）只能在分配集内工作——**新资产仅通过
# bb_add_asset 上报，等主控派发后才能测**。本表列「引用具体资产 id 的工具 → 承载
# id 的参数名」；命中且引用了范围外 id → `[越界拒绝]`（与角色白名单同前缀，进
# _TOOL_FAIL_PREFIXES/HARD_REJECT_PREFIXES，连续硬拒走 E2 熔断）。
# bb_add_asset 不入表——它是唯一的「上报新资产」通道，恒放行；bb_query 也不入表
# （只读侦察不受限，正是子代理看全局黑板的手段，且其 target_asset_id 只是过滤条件）。
_ASSET_REF_TOOLS: dict[str, tuple[str, ...]] = {
    "bb_delete_asset": ("asset_id",),
    "bb_delete_assets": ("asset_ids",),
    "bb_asset_status": ("asset_id",),
    "bb_merge_assets": ("source_asset_id", "target_asset_id"),
    "bb_add_finding": ("target_asset_id",),
    "declare_intent": ("target_asset_id",),
}


def _asset_scope_violations(name: str, args: dict,
                            scope: list[str]) -> list[str]:
    """返回 `name` 的参数里引用了分配集外资产 id 的列表（空=不越界）。

    只认 `_ASSET_REF_TOOLS` 登记的参数名；宽容：缺参/非字符串/空串跳过（交给各
    handler 自身的参数校验，本闸只做边界，不重复做参数合法性）。"""
    params = _ASSET_REF_TOOLS.get(name)
    if not params:
        return []
    allowed = set(scope)
    out: list[str] = []
    for p in params:
        raw = args.get(p)
        vals = [raw] if isinstance(raw, str) else (
            raw if isinstance(raw, (list, tuple)) else [])
        for v in vals:
            aid = str(v or "").strip()
            if aid and aid not in allowed and aid not in out:
                out.append(aid)
    return out


# host·Windows 的 bash 风格连接符（2026-10-01）：PowerShell 5.x 不支持 `&&`/`||`
# （报 InvalidEndOfLine），但命令里带它们极其常见。检测到且策略允许 wsl 时，
# 自动把 runtime=host 改走 wsl（bash -lc），语义最准；不允许则回落明确报错。
_BASH_CONNECTOR_RE = re.compile(r"&&|\|\|")
_WSL_RUNTIME = "wsl"


def _host_is_windows() -> bool:
    return os.name == "nt" or platform.system() == "Windows"

# bb_query 闭集值域（bb-query-filters M1）：非法值显式 [错误]，不静默返回空。
# ⚠ 第二处值域声明，改 store 状态机时必须同步：
#   findings severity → store.list_findings severity_rank；
#   assets status/type → store.set_asset_status 白名单 / register_asset 类型。
_BB_SEVERITIES = ("info", "low", "medium", "high", "critical")
_BB_ASSET_STATUSES = ("open", "visited", "scanning", "tested_clean",
                      "budget_stop", "na")
_BB_ASSET_TYPES = ("host", "domain", "service", "url", "binary")
# what=assets 未显式传 limit 时的返回上限（2026-10-01 防瀑）：FOFA 大批导入后
# 全项目资产可达数千，meta 全文数 MB——默认截断，要全量显式传大 limit。
_BB_ASSETS_DEFAULT_LIMIT = 200
# 单次批量删除上限（2026-10-09）：bb_delete_assets 一次最多删这么多条，防一次
# 误删面过大；超出要求分批。清洗泛解析域名族这类几百条的活，2-3 批即可做完。
_BB_BATCH_DELETE_MAX = 200
# meta 精简白名单（2026-10-01）：默认只回这些常用键（长值截断），verbose=true 才给全文。
# package_id（2026-10-12）：分析包登记样本的资产行带出来源包 id——AI 据此 `bb_query
# what=package package_id=...` 读同包条目/依赖边（解决 exe 依赖同包 DLL 找不到）。
_BB_ASSET_META_KEYS = ("title", "fingerprint", "products", "owner", "source",
                       "protocol", "http_status", "primary_domain", "alias",
                       "filename", "cdn", "package_id")
_BB_ASSET_META_STR_MAX = 160
_BB_ASSET_META_LIST_MAX = 8

def _asset_row(a: dict, *, verbose: bool = False) -> dict:
    """资产行序列化（2026-10-01）：meta 默认精简（防大体积），verbose=true 给全文。"""
    row = {"id": a["id"], "type": a["type"], "value": a["value"],
           "parent_id": a.get("parent_id"), "status": a.get("status", "open")}
    meta = a.get("meta") or {}
    if verbose:
        row["meta"] = meta
        return row
    slim: dict = {}
    for k in _BB_ASSET_META_KEYS:
        if k not in meta:
            continue
        v = meta[k]
        if isinstance(v, str):
            slim[k] = v[:_BB_ASSET_META_STR_MAX]
        elif isinstance(v, list):
            slim[k] = v[:_BB_ASSET_META_LIST_MAX]
        else:
            slim[k] = v
    if slim:
        row["meta"] = slim
    return row

# H1 spill（2026-09-19，借鉴 dsh tool-output-spill）：超限工具结果全量落盘 +
# 有界预览。豁免打开类（kb/skill/route）——正文本身即取用目的（落盘隔一层反而
# 逼模型多绕一步）。run_cmd 不再豁免（2026-10-01）：回执 brief 放大后仍可能逼近
# 阈值，统一走 spill 兜底更稳。
_SPILL_SKIP = {"kb_open", "skill_open", "route_lookup"}
_SPILL_THRESHOLD = 32_000  # 字符（2026-10-01 由 8000 放宽）

# 读路径放开（2026-10-01）：宿主 read_file/search_files 不再限定工作区根——只有
# **写**（run_cmd 的 pathguard）才拦边界。容器/WSL 会话里模型拿到的是容器根路径
# （docker 挂 <ws>→/workspace、wsl 挂 /mnt/<盘符>），需映射回宿主真实路径才读得到。
_DOCKER_WS_MOUNT = "/workspace"          # 与 core.runtime.gateway 同约定


def _resolve_read_path(path: str, ws: Path, scratch: Path) -> Path:
    """读路径解析（2026-10-01 放宽）：
    ① 容器 `/workspace/…`（docker 挂 <ws>→/workspace）→ 宿主 `ws/…`；
    ② WSL `/mnt/<盘符>/…` → `<盘符>:\\…`；
    ③ 其余绝对路径原样；相对路径 → scratch。
    ①② 命中真实文件即返回，否则回退原样（交给上层出「文件不存在」）。注意：
    Windows 上 `/workspace/…` 这种「有根无盘符」路径 `is_absolute()` 为 False，
    故必须先于相对判定处理。"""
    posix = str(path).replace("\\", "/")
    if posix == _DOCKER_WS_MOUNT or posix.startswith(_DOCKER_WS_MOUNT + "/"):
        mapped = ws / posix[len(_DOCKER_WS_MOUNT):].lstrip("/")
        if mapped.exists():
            return mapped.resolve()
    m = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", posix)
    if m:  # WSL 默认挂载 /mnt/e/... → E:\...
        drive = m.group(1).upper() + ":\\"
        mapped = Path(drive + (m.group(2) or "").replace("/", "\\"))
        if mapped.exists():
            return mapped.resolve()
    p = Path(path)
    if p.is_absolute() or posix.startswith("/"):
        return p.resolve()
    return (scratch / p).resolve()


def _display_path(p: Path, ws: Path, scratch: Path) -> str:
    """展示用相对化（scratch 优先、ws 次之、否则原样绝对路径）——读放开后可能
    在工作区之外，`relative_to` 会 ValueError，故集中兜底。"""
    for root in (scratch, ws):
        try:
            return str(p.relative_to(root.resolve())).replace(os.sep, "/")
        except ValueError:
            continue
    return str(p)


# ---------- 附件解包（unpack_attachment，2026-10-19） ----------
# 背景：CTF 轨多题共跑时附件解压目录由 LLM 临时起名、unzip -o 只覆盖同名文件，
# 上轮分析残留整批混进 ls/file/md5sum 输出，把上下文淹掉。改为服务端确定性动作：
# 目录名 scratch/att-<sha12>/ 与附件一一对应，解压前清旧，解压后回结构化清单。
_ATT_SHA_RE = re.compile(r"^([0-9a-f]{12})_")
_ATT_DIR_PREFIX = "att-"
_ATT_DIR_RE = re.compile(r"^att-[0-9a-f]{12}$")
_UNPACK_MAX_ENTRIES = 2000        # zip 条目上限
_UNPACK_MAX_TOTAL = 512 << 20     # 解压后总大小上限（512MB）
_UNPACK_MAX_SINGLE = 128 << 20    # 单文件上限（128MB）
_UNPACK_LIST_CAP = 60             # 清单展示条目上限
_UNPACK_SKIP_NOTE_CAP = 10        # 跳过原因展示条数上限

# 魔数 → 类型粗判（清单展示用，不替代 file 命令）
_UNPACK_MAGIC_KINDS = (
    (b"PK\x03\x04", "zip 族（jar/apk/docx/xlsx）"),
    (b"\x7fELF", "ELF 可执行"),
    (b"MZ", "PE 可执行"),
    (b"\x89PNG\r\n\x1a\n", "PNG 图片"),
    (b"GIF87a", "GIF 图片"),
    (b"GIF89a", "GIF 图片"),
    (b"\xff\xd8\xff", "JPEG 图片"),
    (b"%PDF", "PDF 文档"),
    (b"ID3", "MP3 音频"),
    (b"\x1f\x8b", "gzip 压缩"),
    (b"BZh", "bzip2 压缩"),
    (b"\xfd7zXZ", "xz 压缩"),
    (b"SQLite format 3\x00", "SQLite 库"),
    (b"RIFF", "RIFF 容器（wav/avi）"),
    (b"\xca\xfe\xba\xbe", "Java class / Mach-O fat"),
    (b"\xfe\xed\xfa", "Mach-O 可执行"),
)
_UNPACK_EXT_KINDS = {
    ".py": "Python 脚本", ".txt": "文本", ".md": "Markdown", ".json": "JSON",
    ".html": "HTML", ".htm": "HTML", ".js": "JavaScript", ".sh": "shell 脚本",
    ".c": "C 源码", ".h": "C 头文件", ".cpp": "C++ 源码", ".java": "Java 源码",
    ".go": "Go 源码", ".rs": "Rust 源码", ".rb": "Ruby 脚本",
    ".php": "PHP 源码", ".asm": "汇编", ".s": "汇编",
    ".elf": "ELF 可执行", ".exe": "PE 可执行", ".dll": "PE 动态库",
    ".so": "ELF 动态库", ".gb": "Game Boy ROM", ".gba": "GBA ROM",
    ".nes": "NES ROM", ".pcap": "流量捕获", ".pcapng": "流量捕获",
    ".png": "PNG 图片", ".jpg": "JPEG 图片", ".jpeg": "JPEG 图片",
    ".gif": "GIF 图片", ".mp3": "MP3 音频", ".wav": "WAV 音频",
    ".wasm": "WebAssembly", ".pyc": "Python 字节码", ".class": "Java class",
    ".pem": "证书/密钥", ".der": "证书/密钥", ".csv": "CSV", ".xml": "XML",
    ".yaml": "YAML", ".yml": "YAML", ".log": "日志", ".ini": "配置",
    ".cfg": "配置", ".toml": "TOML", ".npy": "NumPy 数组",
}


def _human_size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f}MB"
    if n >= 1 << 10:
        return f"{n // (1 << 10)}KB"
    return f"{n}B"


def _attachment_sha12(p: Path) -> str:
    """附件 sha12：上传命名 `<sha12>_<name>` 优先取文件名；否则内容 sha256 前 12 位。"""
    m = _ATT_SHA_RE.match(p.name)
    if m:
        return m.group(1)
    h = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return hashlib.sha256(p.name.encode("utf-8")).hexdigest()[:12]
    return h.hexdigest()[:12]


def _guess_kind(p: Path) -> str:
    """文件类型粗判：魔数优先、扩展名兜底（解包清单展示用）。"""
    try:
        if p.stat().st_size <= 8 * (1 << 20):
            with open(p, "rb") as fh:
                head = fh.read(16)
            for magic, kind in _UNPACK_MAGIC_KINDS:
                if head.startswith(magic):
                    return kind
    except OSError:
        pass
    return _UNPACK_EXT_KINDS.get(p.suffix.lower(), "二进制/未知")


def _safe_zip_target(dest: Path, name: str) -> Path | None:
    """zip slip 防护：拒绝绝对路径/盘符/`..` 穿越/符号链接语义的条目名，
    返回限定在 dest 内的目标路径；不安全返回 None。"""
    posix = str(name).replace("\\", "/")
    if posix.startswith("/") or re.match(r"^[A-Za-z]:", posix):
        return None
    parts = [x for x in posix.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        return None
    try:
        target = dest.joinpath(*parts).resolve()
        target.relative_to(dest.resolve())
    except (OSError, ValueError):
        return None
    return target

# stuck-convergence D6（2026-09-23）：收尾确认轮上限——防确认本身拖收尾烧 token
_CLOSING_MAX_ROUNDS = 2

# 地址入参：hex 串（"0x401000"）/ 十进制串 / int 皆收，内部统一 int。
# 与 API 的 FuncCreateIn 同语义：int(str, 0)，0x 前缀走 hex、裸数字走十进制。
# 说明文案 `_ADDR_DESC` 定义在 tool_registry.py（工具 schema 要用，见顶部 import）。


def _coerce_addr(value: Any) -> int:
    if isinstance(value, bool):  # bool 是 int 子类，显式拦住
        raise ValueError(f"非法地址: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip(), 0)
    raise ValueError(f"非法地址: {value!r}")


class ToolDispatcher:
    """工具分发中枢。持会话状态：当前任务、进度心跳（卡死检测的依据）。"""

    #: C6 漏洞核对 hook（AgentSession 按 track 注入；None=不核对）：
    #: category=vuln 登记前调用 gate(draft) -> (compliant, new_severity, note)：
    #: compliant=False → 降 intel；compliant=True 且 new_severity 非空 → 定级校准降一档。
    vuln_gate: Callable[[dict], tuple[bool, str | None, str]] | None = None

    #: v0.65 done 自动提案 hook（AgentSession 注入 planner LLM 复盘；None=关闭）：
    #: complete_task 成功后 hook(task_id, result_note)、fail_task 真失败后
    #: hook(task_id, result_note, failure=True)（experience-sedimentation M1）——
    #: 条件判定在 hook 内部（_sediment_verdict），产经验沉淀提案草稿。
    sediment_hook: Callable[..., None] | None = None

    #: ⑥ 战役记忆写入 hook（AgentSession 注入全局库 CampaignMemory；None=关闭）：
    #: complete_task 成功后调用 hook(task_id, result_note) 沉淀打法（跨项目召回）。
    campaign_hook: Callable[[str, str], None] | None = None

    def __init__(self, bb: Blackboard, gateway: ExecutionGateway,
                 *, project_id: str, session_id: str, author: str,
                 decompiler=None, artifacts_dir=None, browser=None,
                 packs_root: str | Path | None = None,
                 track: str | None = None,
                 capabilities: list[str] | None = None,
                 allowed_tools: list[str] | None = None,
                 allowed_task_types: Iterable[str] | None = None,
                 max_steps: int = 0,
                 stuck_after: int = 12,
                 closing_max_rounds: int = _CLOSING_MAX_ROUNDS,
                 abort_event: threading.Event | None = None,
                 role_skills: list[str] | None = None,
                 allowed_roles: list[str] | None = None,
                 assigned_asset_ids: list[str] | None = None,
                 thread_id: str = "",
                 intent_free: bool = False,
                 environment_id: str | None = None,
                 recovery: EnvironmentRecovery | None = None,
                 supports_vision: bool = False):
        self.bb = bb
        self.gateway = gateway
        self.project_id = project_id
        self.session_id = session_id
        self.environment_id = (environment_id or "").strip() or None
        # 环境恢复器必须由上层显式装配；未装配时不会尝试任何安装/构建。
        self.recovery = recovery
        self.author = author
        # CTF 题目归属（challenge-asset v38，2026-10-14）：对话轮（chat runtime）
        # 传入真实 thread id，供 declare/兜底按「一个主控线程只发一道题」取题目节点；
        # 任务链会话（AgentSession）不传 → ''，该路径无题目概念。
        self.thread_id = (thread_id or "").strip()
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
        # 资产越界约束（agent-path-intent-loop M2，2026-10-09）：主控 call_expert
        # 划给本子专家的资产 id 范围；None/空=不启用（任务链/主控/未分配委派不受限）。
        # 非空时，_ASSET_REF_TOOLS 命中的工具若引用范围外资产 → [越界拒绝]。
        self.assigned_asset_ids = [str(x).strip()
                                   for x in (assigned_asset_ids or [])
                                   if str(x).strip()] or None
        # 轨 task_types.yaml 注册表（A5 子代理发任务的类型护栏）：None=未接线不校验
        self.allowed_task_types = allowed_task_types
        # 意图豁免（2026-10-17，激进主控子代理 probe_mode）：True=跳过意图先行闸
        # 与「发现必挂意图」双检查。激进主控派出的快速子任务是「单点几分钟跑完」
        # 的探针——为一次快检先建意图链路再收尾反而稀释速度；工具面侧已同步
        # 剔除 declare/close_intent 与 bb_query（runtime.py _PROBE_EXCLUDED_TOOLS），
        # 不放行这两道闸会让探针的 run_cmd/bb_add_finding 全被打回。任务链
        # （AgentSession）、chat-orchestrator 子专家、人工/API 路径一律不豁免。
        self.intent_free = bool(intent_free)
        # vision 通道（2026-10-19）：本会话的模型能否看图片。由上层从
        # provider 实例（`llm.supports_vision`，源自 providers.json 的
        # `vision: true`）注入——Dispatcher 自己不持 llm 引用（工具层不该知道
        # 用哪个模型），只认这个布尔。False → view_image 只回元信息 + OCR 指引。
        self.supports_vision = bool(supports_vision)
        # 会话步数预算（E8）：AgentSession 按角色收敛后的 max_steps 注入；request_steps
        # 增补写这里（_loop 的 range 上界同读），0 = 未装配（request_steps 拒收）
        self.max_steps = max_steps
        # session-state 收敛（2026-10-03）：任务/闸门状态统一落共享 SessionState
        #（本类独立使用时自建，AgentSession 构造后以同一实例覆盖）。下方同名
        # property 代理保证既有读写点与测试断言零改动；字段语义与每任务复位
        # 归属见 core/agent/session_state.py。此处仅留本类专属的实例字段。
        self._state = SessionState()
        # v14 认领即换装：当前任务实际执行 persona（None=按会话底色角色）；
        # 换装/恢复由 AgentSession._apply_task_persona/_restore_base_persona 维护
        self.current_persona_role: str | None = None
        self._step = 0
        # 本会话 unpack_attachment 解压过的附件工作目录名（scratch/att-<sha12>，
        # 2026-10-19）：search_files 默认大范围检索只扫这些目录，别的题/别的会话
        # 解压出来的工作副本不会被误扫进上下文。
        self._unpack_dirs: set[str] = set()
        # D6 收尾确认（2026-09-23）：complete 申报后先对照产出清单确认无遗漏
        self.dry_tail_steps = max(1, int(stuck_after) - 2)  # 干尾巴阈值=stuck_after-2，与 D1 窗错位
        # D10（2026-09-24）：收尾确认轮上限项目级可配（0=首次申报即放行）
        self.closing_max_rounds = (
            closing_max_rounds if isinstance(closing_max_rounds, int)
            and not isinstance(closing_max_rounds, bool) else _CLOSING_MAX_ROUNDS)

    # ---------- 状态代理（session-state 收敛，2026-10-03） ----------
    # 原属性名全部保留（读写点与测试断言零改动），存储落共享 SessionState。
    # current_task_id/last_progress_step：非每任务复位，由收尾/中断路径显式管理。
    # finished/awaiting_human/summary/delegation_*：会话收尾标志（每任务复位）。
    # plan_only_mode：教练（每任务复位）。
    # closing_round/closing_last_progress：D6 收尾确认轮状态（每任务复位）。

    @property
    def state(self) -> SessionState:
        return self._state

    current_task_id = state_proxy("current_task_id")
    last_progress_step = state_proxy("last_progress_step")
    finished = state_proxy("finished")
    awaiting_human = state_proxy("awaiting_human")
    _finish_open_intents_ack = state_proxy("finish_open_intents_ack")
    summary = state_proxy("summary")
    delegation_just_finished = state_proxy("delegation_just_finished")
    last_delegation_note = state_proxy("last_delegation_note")
    plan_only_mode = state_proxy("plan_only_mode")
    closing_round = state_proxy("closing_round")
    closing_last_progress = state_proxy("closing_last_progress")

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
        ok = not result.startswith(_TOOL_FAIL_PREFIXES)
        if name != "run_cmd" or gated:
            try:
                payload = {"name": name, "args": _truncate_args(args),
                           "step": self._step,  # 轮次号（观测对齐 command 事件；非计划步 id）
                           "ok": ok,
                           "duration_s": round(time.perf_counter() - t0, 2),
                           "result_head": result[:400]}
                if gated:
                    payload["gated"] = True
                self.bb.append_event(
                    self.project_id, "tool.call", payload,
                    session_id=self.session_id, author=self.author)
            except Exception:  # noqa: BLE001 —— 审计事件绝不影响工具返回
                pass
        if not ok:
            # 失败日志（diagnostics-log，2026-10-14）：tool.call 带 ok=False 混在
            # 全量调用里难筛，另落独立 tool.failed 供诊断页查询。chat 线程的
            # dispatcher 同样走本入口（author=chat-<tid12>，source 自动分流）。
            # mcp__ 失败在 chat runtime._dispatch 单独记 mcp.failed（任务链无 MCP）。
            record_tool_failure(
                self.bb, self.project_id, name=name, args=args, error=result,
                session_id=self.session_id, author=self.author, step=self._step)
        return result

    def _maybe_spill(self, name: str, result: str) -> str:
        """H1 spill（借鉴 dsh tool-output-spill-files）：结果超阈值时全量落
        workspace/spill/，回填「head+tail 预览 + 精确省略计数 + 定位器 + 检索提示」
        ——有损但可找回（与 G3 摘要正交：摘要有损不可逆，spill 按需可取回）。
        未装配 workspace / 落盘失败一律原样返回（尽力而为，绝不阻断工具链）。

        **带图结果（ToolOutput，vision 通道 2026-10-19）直接放行**：spill 走的是
        「落盘 + 回填预览字符串」，返回值必是 str → ``.images`` 当场蒸发。而图片
        结果本身文本只有一两百字，根本到不了阈值；真到了也只可能是误判，宁可
        让它超限也不能静默丢图（模型会以为「我看过了」其实什么都没收到）。"""
        if name in _SPILL_SKIP or getattr(result, "images", None) \
                or len(result) <= _SPILL_THRESHOLD:
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
        # handler 名：注册表优先（ToolSpec.handler，缺省 `_tool_<name>`），
        # 未注册名回落命名约定——保留运行期补挂工具的实现口子（测试替身用）。
        # 注册表内的 51 个工具由文件末尾导入期自检保证 handler 真实存在。
        spec = REGISTRY.get(name)
        handler = getattr(self, spec.handler if spec is not None else f"_tool_{name}", None)
        if handler is None:
            return f"[错误] 未知工具: {name}"
        # v23（TRAE 新壳 M3）：run_cmd 必须显式传 runtime；任务机制退役（2026-10-06）
        # 后不再有「任务默认运行时」回填。
        if name == "run_cmd" and args.get("runtime") is None:
            return ("[错误] run_cmd 省略了 runtime：请显式传 runtime"
                    "（host/wsl/docker/sandbox，按目标与能力清单选择）")
        if self.allowed_tools is not None and name not in self.allowed_tools \
                and name not in _CONTROL_TOOLS and name not in _PLAN_TOOLS:
            # 角色软边界（§6.6）：白名单外工具不执行，由人类调整角色配置放开。
            return (f"[越界拒绝] 工具 {name} 不在本角色工具白名单内"
                    f"（允许: {', '.join(self.allowed_tools)}）。停止该方向或请人类调整角色配置。")
        # 资产越界闸（agent-path-intent-loop M3，2026-10-09）：主控分配的资产范围内
        # 才能操作。命中 `_ASSET_REF_TOOLS` 且引用了分配集外的资产 id → 拒绝并指路
        # 「bb_add_asset 上报等派发」。宽容：参数缺失/非字符串/空串一律放行，交给
        # 各 handler 自身的参数校验（本闸只做边界，不重复做参数合法性）。
        if self.assigned_asset_ids:
            out_of_scope = _asset_scope_violations(
                name, args, self.assigned_asset_ids)
            if out_of_scope:
                return ("[越界拒绝] 资产越界：本会话只被分配了 "
                        + ", ".join(self.assigned_asset_ids)
                        + f"，但你引用了范围外的资产 {', '.join(out_of_scope)}。"
                        "新发现的资产先用 bb_add_asset 上报，等主控派发后再测；"
                        "直接测试/登记范围外资产会与其它子代理重复。")
        # CTF 轨禁 tested_clean（ctf-no-tested-clean，2026-10-20）——**必须在意图
        # 先行闸之前**判，原因见下。
        #
        # **为什么需要这道闸**（用户实测真问题）：CTF 轨的终态判据是 **flag
        # 台账**（`artex_runtime._ctf_task_state` 注释明写「题目不是待测资产、
        # flag 也不是 fact，资产覆盖度与 fact 计数在 CTF 轨都是无意义数字」），
        # 「资产测干净」在 CTF 轨不对应任何东西。但工具面里 `bb_asset_status`
        # 一直在（`_CTF_CORE_TOOLS` + 六个 CTF 专家 yaml），描述还写着「适用面
        # 全覆盖标 tested_clean」——模型解完题自然想去标，然后必然撞两道门禁：
        # 题目行有子资产→「父节点由子树派生」拒；转标附件→「须死路背书」拒
        # （而它刚解出 flag，本来就没有死路）。于是为凑一条 dead_end 还得再查
        # event id 补证据引用——三四个轮次白烧，对终态判定零贡献。
        #
        # **为什么位置必须在意图先行闸之前**：闸先返回的话，模型看到的是「先
        # declare_intent 再来」，于是它会乖乖声明一条意图、然后再撞同一个
        # 「CTF 轨没有这个概念」——多绕一轮，且被误导成「问题出在没声明意图」。
        # 这个工具在 CTF 轨是**整体无意义**的（不是「需要先立意才能用」），
        # 所以要先说清这一点。
        #
        # 拒绝文案必须**给出正确出口**（只说「不行」会让模型继续试别的状态）：
        # 解出→close_intent(finding)；解不出→close_intent(dead_end)；
        # 整题放弃→ctf_abandon。
        if name == "bb_asset_status" and self.track == "ctf" \
                and str(args.get("status") or "").strip() == "tested_clean":
            return ("[拒绝] CTF 轨没有「资产测干净（tested_clean）」这个概念——"
                    "本题终态由 **flag 台账**判定，不由资产状态判定。"
                    "按你的实际处境选一个：① 已解出 → `close_intent("
                    "outcome=\"finding\", finding_ids=[那条 flag 留痕发现])` 收尾意图即可"
                    "（ctf_submit_flag 已入台账，无需再标任何资产状态）；"
                    "② 某条思路不成立 → `close_intent(outcome=\"dead_end\", "
                    "dead_reason=...)`，题还能继续解；"
                    "③ 整道题放弃 → `ctf_abandon(reason=...)`。"
                    "**不要**再对题目或其附件标 tested_clean。")
        # 意图先行闸（口径 Y，2026-10-01；2026-10-09 升级为**每意图级**）：
        # **任何**实质执行动作前都必须有 open 意图——先 declare_intent 把方向落成
        # 一句可证伪假设，再 run_cmd/写黑板。逐动作都查（不再是一次性放行标志）：
        # plan→execute→plan 自循环里，每条意图收尾后再执行必须先声明下一条，
        # 事后声明（先干活再补票，实测 sess-b9a539e3ebfe declare 与 finding 仅隔
        # 12 秒）在结构上不可能。人类/系统路径（author 非 sess-/chat-）不经
        # ToolDispatcher，不受限；declare_intent 在 _INTENT_PRE_ALLOWED 内恒放行，
        # 全轨可先声明不会死锁。
        # intent_free（激进主控子代理，2026-10-17）：探针不声明意图，本闸整体
        # 跳过——与其工具面上没有 declare_intent（调了也被白名单拒）配套，
        # 否则探针的 run_cmd 会被「没有 open 意图」打回、快检直接瘫痪。
        # 拒绝用 [拒绝] 前缀（前缀纪律：进 _TOOL_FAIL_PREFIXES/HARD_REJECT_PREFIXES，
        # 连续 3 步硬拒走 E2 熔断挂人——照搬口径即无需新增前缀分类）。
        if self.author.startswith(("sess-", "chat-")) and not self.intent_free \
                and name not in _INTENT_PRE_ALLOWED:
            has_open = self.bb.conn.execute(
                "SELECT 1 FROM intents WHERE project_id=? AND status='open'"
                " AND author=? LIMIT 1",
                (self.project_id, self.author)).fetchone() is not None
            if not has_open:
                return ("[拒绝] 意图先行闸：本会话当前没有 open 意图，实质动作被拦下"
                        "——先 declare_intent(statement=\"对 <资产> 进行 <什么尝试>\""
                        ", target_asset_id=<资产id>) 把方向落成一句可证伪假设，再围绕"
                        "它执行；上一条意图已收尾时同样要先声明下一条，再动手。只读"
                        "侦察（bb_query/kb_open/kb_search/list_symbols/decompile/"
                        "disasm/read_file/search_files/browser_navigate 等）不受本闸"
                        "限制。"
                        + _intent_scope_hint(self.track))
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
        except TypeError as e:
            # 参数幻觉自纠锚点（2026-09-30）：参数名拼错/多传时附上真实参数清单，
            # 模型下一轮可直接改参重试——裸 TypeError 只会引发连环猜参数
            # （实测：bb_query 被传 what=files+sha256 连错两处，无清单则空转）。
            hint = ""
            if "keyword argument" in str(e) or "positional argument" in str(e):
                try:
                    names = list(inspect.signature(handler).parameters)
                    hint = f"（{name} 的合法参数: {', '.join(names)}）"
                except (ValueError, TypeError):
                    pass
            return f"[工具异常] {type(e).__name__}: {e}{hint}"
        except Exception as e:  # noqa: BLE001 —— 工具失败回填文本，循环不中断
            return f"[工具异常] {type(e).__name__}: {e}"

    # ---------- 各工具实现 ----------

    def _scope_expand_request(self, target: str, reason: str) -> str:
        """越界目标的统一人工授权出口；同一会话/目标已有 pending 时复用。"""
        target = str(target or "").strip()[:500]
        action = {
            "op": "authorization", "kind": "scope_expand",
            "scope_request": target,
            "scope_value": target,
            "justification": reason[:1000],
            "task_id": self.current_task_id, "session_id": self.session_id,
        }
        pending = None
        rows = self.bb.conn.execute(
            "SELECT id, action FROM approvals WHERE project_id=? AND session_id=? "
            "AND status='pending' ORDER BY created_at DESC",
            (self.project_id, self.session_id),
        ).fetchall()
        for row in rows:
            try:
                if json.loads(row["action"]).get("scope_request") == target:
                    pending = row
                    break
            except (TypeError, ValueError):
                continue
        if pending:
            approval_id = pending["id"]
        else:
            approval_id = self.bb.request_approval(
                self.project_id, action, risk="high", requested_by=self.author,
                session_id=self.session_id)["id"]
        if self.author.startswith("sess-"):
            self.awaiting_human = True
        return (f"[范围需人工确认] 目标 {target} 超出项目授权范围，已提交 scope_expand 审批"
                f" approval_id={approval_id}；当前方向暂停，批准后请先登记目标再继续。")

    def _tool_run_cmd(self, cmd: str, threat_class: str,
                      runtime: str | None = None,
                      net: str | None = None, approval_id: str | None = None,
                      timeout: float | None = None) -> str:
        # runtime 缺省（LLM 未传）由 _dispatch_once 按任务 preferred_runtime 回填；
        # 走到这里仍为 None = 无任务默认，拒绝执行
        scope_verdict = check_command_scope(self.bb, self.project_id, cmd)
        if not scope_verdict.allowed:
            return self._scope_expand_request(
                scope_verdict.host or "命令中的网络目标",
                scope_verdict.reason)
        if runtime is None:
            return ("[错误] run_cmd 缺少 runtime：请显式指定 host/wsl/docker/sandbox"
                    "（任务默认运行时未设置）")
        # bash 风格连接符兼容（2026-10-01）：宿主机 Windows 上 host 走 PowerShell
        # 5.x，`&&`/`||` 直接语法错（InvalidEndOfLine）。命令里带它们极常见，
        # 且 host→wsl 属同级（WSL 信任级=宿主机），故在策略允许时自动改走 wsl。
        # 策略不允许（threat_class 挡下）则回落明确报错，不越权。
        wsl_note = ""
        if runtime == "host" and _host_is_windows() and _BASH_CONNECTOR_RE.search(cmd):
            if self._wsl_allowed(threat_class):
                runtime = _WSL_RUNTIME
                wsl_note = ("[已自动改用 wsl] 检测到 bash 连接符（&&/||），"
                            "宿主机 PowerShell 不支持；本命令在 wsl(bash -lc) 下执行。\n")
            else:
                return ("[错误] 命令含 bash 连接符（&&/||），但宿主机 Windows 的 "
                        "host 运行时是 PowerShell，不支持该语法；当前策略又不允许 "
                        "wsl（受 threat_class 限制）。请改用 PowerShell "
                        "写法（`;` 顺序执行、`if ($?) {}` 条件）或拆成多条命令，"
                        "或请人类放宽运行时上限。")
        workspace = Path(self.artifacts_dir).parent if self.artifacts_dir else None
        run_kwargs = {
            "threat_class": threat_class,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "author": self.author,
            "environment_id": self.environment_id,
            "net": net,
            "approval_id": approval_id,
            "timeout": timeout,
            "abort_event": self.abort_event,
            "workspace": workspace,
            "step": self._step,
        }
        def _request() -> RecoveryRequest:
            return RecoveryRequest(
                runtime=runtime, threat_class=threat_class,
                project_id=self.project_id, session_id=self.session_id,
                author=self.author, environment_id=self.environment_id,
                net=net, approval_id=approval_id, timeout=timeout,
                abort_event=self.abort_event, step=self._step)

        def _recover_and_retry(issue: EnvironmentIssue) -> str:
            # 环境故障与策略拒绝分离：仅在显式装配恢复器时走一次受控修复，
            # 恢复器自身复用相同的 gateway/runtime/threat/workspace/net/approval。
            if self.recovery is None:
                return (wsl_note + f"[环境错误] {issue.code}: {issue.message}；"
                        "未装配自动恢复器，请修复环境后重试")
            recovered = self.recovery.recover(issue, _request())
            if not recovered.ok:
                return wsl_note + recovered.brief()
            # 恢复成功后只重试原命令一次；不把恢复动作当作原命令成功。
            try:
                retry = self.gateway.run(cmd, runtime, **run_kwargs)
            except EnvironmentUnavailable as retry_error:
                return (wsl_note + f"[环境恢复后仍失败] {retry_error.issue.code}: "
                        f"{retry_error.issue.message}")
            except GatewayDenied as retry_error:
                # 恢复动作成功也不能越过原命令的策略/审批限制。
                return wsl_note + f"[网关拒绝] {retry_error}"
            retry_issue = environment_issue_from_dict(
                retry.environment_error, command=cmd, runtime=runtime)
            if retry_issue is not None:
                return (wsl_note + f"[环境恢复后仍失败] {retry_issue.code}: "
                        f"{retry_issue.message}")
            return (wsl_note + "[环境恢复后重试]\n"
                    + self._cmd_receipt(cmd, retry))

        try:
            r = self.gateway.run(cmd, runtime, **run_kwargs)
        except EnvironmentUnavailable as e:
            return _recover_and_retry(e.issue)
        except GatewayDenied as e:
            # 保持原有策略拒绝语义；不能触发环境安装。
            return f"[网关拒绝] {e}"
        issue = environment_issue_from_dict(
            r.environment_error, command=cmd, runtime=runtime)
        if issue is not None:
            # 普通非零 ExecOutcome 由 gateway 以 environment_error 返回，而不是抛
            # 异常；必须与 BackendError 走完全相同的恢复/重试路径。
            return _recover_and_retry(issue)
        return wsl_note + self._cmd_receipt(cmd, r)

    def _cmd_receipt(self, cmd: str, r: Any) -> str:
        """run_cmd 回执：brief(8000) 预览 + **原始 stdout 全量 spill**。

        为什么必须在这里补一次 spill：dispatcher 的 `_maybe_spill` 阈值是
        32,000，而回执已被 `brief(8000)` 压到 8000 以下 → 永远触发不了。
        后果是 stdout 超过 8000 的部分**既没落盘也拿不回来**，模型被迫重跑
        命令重取（tshark 反复重新解析同一份大 pcapng 就是这么来的），而
        brief 那句「请缩小窗口分段读」对一次性进程输出是空话——窗口缩小输出
        还是那么多，被切掉的部分没有任何路径能取回。
        """
        receipt = self._finish_cmd_receipt(cmd, r.brief(8000))
        return self._spill_cmd_stream(receipt, r)

    # stdout/stderr 原文落盘阈值：与 brief 上限一致，改 brief 时同步
    _CMD_STREAM_SPILL_AT = 8000

    def _spill_cmd_stream(self, receipt: str, r: Any) -> str:
        """把被 brief 切掉的 stdout/stderr 原文落盘，回执补定位器。

        只读、失败即原样返回（不阻断工具链）；未装配 artifacts_dir 时退回并
        提示改用重定向——**不要建议重跑命令**。
        """
        stdout = getattr(r, "stdout", "") or ""
        stderr = getattr(r, "stderr", "") or ""
        if max(len(stdout), len(stderr)) <= self._CMD_STREAM_SPILL_AT:
            return receipt
        notes = []
        if not self.artifacts_dir:
            # 落不了盘也别给「分段读」的假承诺：窗口法对进程输出无效，
            # 唯一可行的是让命令自己把输出写文件。
            return (receipt + "\n[stdout 已被截断且无法落盘] 无法分段取回，"
                    "请改用重定向：run_cmd 跑 `命令 > out.txt 2>&1`，"
                    "再用 read_file 读文件。")
        ws = Path(self.artifacts_dir).parent
        for stream, label in ((stdout, "run_cmd_stdout"),
                              (stderr, "run_cmd_stderr")):
            if len(stream) <= self._CMD_STREAM_SPILL_AT:
                continue
            path = spill_text(stream, label, ws / "spill")
            if path is None:
                continue
            try:
                rel = os.path.relpath(path, ws / "scratch").replace(os.sep, "/")
            except ValueError:  # Windows 跨盘等边缘：退回绝对路径
                rel = str(path)
            notes.append(f"{label.rsplit('_', 1)[1]} 全量 {len(stream)} 字符 → {rel}")
        if not notes:
            return receipt
        return (receipt + "\n[输出全量已落盘] " + "；".join(notes) +
                "。剩余部分用 read_file(path=…, offset/limit 分段读) 或 "
                "search_files(pattern=关键词) 定位，**不要重跑命令重取**；"
                "以后大输出优先让命令自己写文件（`命令 > out.txt`）再读。")

    def _finish_cmd_receipt(self, cmd: str, receipt: str) -> str:
        """run_cmd 回执统一出口：原地打转提示 → CTF 观测摘要。

        两个后处理都是**只读、零 LLM 成本、失败即原样返回**的附加信息；集中到
        一个出口，避免恢复/重试分支漏挂（此前 `_with_ctf_observations` 就只在
        主路径调用，环境恢复后的重试回执不带观测摘要）。
        """
        return self._with_ctf_observations(self._with_repeat_guard(cmd, receipt))

    # 「原地打转」提示的阈值：第 N 次跑同一条命令时开始提示
    _REPEAT_WARN_AT = 3
    _REPEAT_HARD_AT = 5
    # 统计窗口（本会话最近多少条 command 事件）——CTF 单题轮次通常远小于此
    _REPEAT_WINDOW = 120

    def _with_repeat_guard(self, cmd: str, receipt: str) -> str:
        """同一条命令反复执行 → 在回执尾部点明次数（chat 轨尤其需要）。

        **要解决的问题**：`AgentSession` 侧有 `_command_repeat_stats`，但那只喂给
        策略顾问（多步无进展才触发）；**对话线程（CTF 主控/子专家）没有任何
        反馈面**——模型可以对着同一道题把 `curl 'http://t/?id=1'` 跑十几遍，
        每轮都觉得自己在「再确认一次」，用户只看到 token 在烧。

        这里在回执末尾机械计数并直说：「这条你已经跑了 N 次」。**不阻断、不
        改命令**——是不是该换思路由模型判断，本函数只提供它自己看不见的事实
        （模型没有跨轮的精确命令计数器）。

        只按**完全相同**的命令串计数（不做归一化/相似度）：`?id=1` 与 `?id=2`
        是不同尝试，误并会冤枉正常枚举。计数取本会话最近 `_REPEAT_WINDOW` 条
        command 事件，含本次（gateway 在执行时已落事件）。
        """
        if not cmd or not receipt:
            return receipt
        sid = str(getattr(self, "session_id", "") or "")
        if not sid:
            return receipt
        try:
            events = self.bb.recent_events(
                self.project_id, tail=self._REPEAT_WINDOW,
                session_id=sid, kinds=["command"])
        except Exception:  # noqa: BLE001 —— 计数失败绝不阻断工具链
            return receipt
        target = cmd.strip()
        times = sum(1 for e in events
                    if str((e.get("payload") or {}).get("cmd", "")).strip() == target)
        if times < self._REPEAT_WARN_AT:
            return receipt
        head = target.replace("\n", " ")[:160]
        if times >= self._REPEAT_HARD_AT:
            note = (f"\n\n[原地打转] 这条命令在本会话已执行 **{times} 次**（完全相同）："
                    f"`{head}`\n"
                    "同样的输入只会得到同样的输出——**继续跑它不会带来新信息**。"
                    "换一个角度：换参数/换路径/换编码、换工具（换 scanner/换 payload），"
                    "或先回到题面重新读线索。若已确认此路不通，把否定结论落进黑板"
                    "（bb_add_finding，category=intel）再换方向。")
        else:
            note = (f"\n\n[重复执行] 这条命令本会话已跑 {times} 次（完全相同）：`{head}`"
                    "——若输出无变化，说明该方向已探尽，考虑换参数或换思路。")
        return receipt + note

    def _with_ctf_observations(self, receipt: str) -> str:
        """CTF 轨：在工具回执尾部附「机械抽取的观测摘要」（ctf-observe，2026-10-19）。

        **为什么**：一次 nmap/ffuf/curl 的输出几十到几百行，模型转述时必丢
        `22/tcp open ssh` 这种关键细节，上下文吃紧时干脆跳过不抽。这里用零 LLM
        成本的正则抽取（`core.ctf.observe`）把端口/URL/凭据/要点先落成结构化
        摘要——模型只需在此基础上判断，不必从噪音里自己捞。

        非 CTF 轨、无输出、抽不出信号 → 原样返回（回执零变化）。
        摘要本身**不写黑板**：写不写、写什么仍由模型经 bb_add_finding 决定。
        """
        if self.track != "ctf" or not receipt:
            return receipt
        try:
            summary = observe.summarize_output(receipt)
        except Exception:  # noqa: BLE001 —— 抽取失败绝不阻断工具链
            return receipt
        if not summary:
            return receipt
        return receipt + "\n\n" + summary

    def _wsl_allowed(self, threat_class: str) -> bool:
        """wsl 是否被网关威胁策略允许（自动降级前置条件）。"""
        return _WSL_RUNTIME in allowed_runtimes(threat_class)

    def _tool_read_file(self, path: str, offset: int = 1, limit: int = 100) -> str:
        """只读文件（2026-09-20）：host 原生 Python open，不经 WSL/PowerShell
        ——无命令执行面、无引号转义、无启动开销；pathguard 只拦写不受影响。
        读路径放开（2026-10-01）：不再限定工作区根（只有写才拦边界）；容器
        `/workspace/…` 与 WSL `/mnt/<盘符>/…` 绝对路径自动映射回宿主真实路径。
        cat -n 风格带行号；单行 >2000 字符切尾标注；offset=-N 读末尾 N 行（tail）。"""
        if not self.artifacts_dir:
            return "[错误] 未装配工作区（artifacts_dir），read_file 不可用"
        ws = Path(self.artifacts_dir).parent
        scratch = ws / "scratch"
        p = _resolve_read_path(path, ws, scratch)
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
        limit = max(1, min(int(limit), 2000))
        if start > total:
            return (f"[错误] offset={offset} 超出文件范围（共 {total} 行）；"
                    "负数 offset 表示读末尾 N 行")
        sel = lines[start - 1:start - 1 + limit]
        width = len(str(start + len(sel) - 1))
        out = []
        for i, line in enumerate(sel, start=start):
            if len(line) > 2000:
                line = line[:2000] + f"…[行截断：共 {len(line)} 字符]"
            out.append(f"{str(i).rjust(width)}\t{line}")
        body = "\n".join(out)
        end = start + len(sel) - 1
        if end < total:
            body += f"\n…[共 {total} 行，当前显示 {start}-{end}；继续读调大 offset]"
        # D9：成功读取落 file.read 轻事件（只记路径/步号，不存内容）——
        # 卡死预检凭「在读新文件」识别活跃探索；审计事件失败不得影响工具返回
        try:
            self.bb.append_event(
                self.project_id, "file.read",
                {"path": _display_path(p, ws, scratch)[:200], "step": self._step},
                session_id=self.session_id, author=self.author)
        except Exception:  # noqa: BLE001
            pass
        return body

    def _tool_view_image(self, path: str, max_edge: int = 0,
                         note: str = "") -> str:
        """【看图】把图片送进视觉通道（vision 通道，2026-10-19）。

        动机（真事故）：`deepseek-v4.1-flash` 所在网关**支持图片输入**，但此前
        全链路没有图像通道——附件只渲染成 `📎 <路径>` 文本行
        （`chat/runtime.py` run()），`read_file` 是 `read_text(errors="replace")`
        读 PNG 只得乱码，`_chat_messages/_response_input` 还会把 image 块抹成空串，
        providers.json 也没有能力声明。于是模型唯一可行的路就是 KB 教的
        `tesseract`/`zbarimg`——「老是用 OCR」不是模型笨，是工程侧没给它眼睛。

        返回 `ToolOutput`（str 子类）：文本是给人读的元信息，`.images` 挂真正的
        image 块，由 provider 层翻译成 image_url / input_image。

        **降级红线**（对齐工具失败约定）：非图片/超限/未声明 vision 一律回
        `[错误]` 或纯文本 + 明确下一步，**绝不抛异常**（工具失败不该炸整轮）。
        """
        if not self.artifacts_dir:
            return "[错误] 未装配工作区（artifacts_dir），view_image 不可用"
        from core import media

        ws = Path(self.artifacts_dir).parent
        scratch = ws / "scratch"
        p = _resolve_read_path(path, ws, scratch)
        got = media.read_image(p)
        if isinstance(got, str):
            return f"[错误] {got}"
        data, info = got

        # 供应商没声明视觉能力 → 不出图。往不支持的网关塞图片块会被 400 打死
        # 整轮，代价远大于「这次没看到图」。回填元信息 + 该走的路。
        if not getattr(self, "supports_vision", False):
            return (
                f"已识别图片：{info.describe()}（{_display_path(p, ws, scratch)}）\n"
                "[提示] 当前供应商未声明视觉能力（providers.json 的 `vision: true`），"
                "无法把图片送进模型。请改走命令行分析：\n"
                "- 文字/二维码：`tesseract <图> stdout` / `zbarimg -q <图>`\n"
                "- 元信息：`exiftool <图>`；结构：`file`、`xxd | head`\n"
                "- 隐写：`binwalk -e`、`zsteg`、`steghide info`、位平面脚本\n"
                "（若确认该供应商其实能看图，在设置里给它加 vision: true 后重试）")

        max_edge = int(max_edge) if isinstance(max_edge, int) and max_edge > 0 \
            else media.DEFAULT_MAX_EDGE
        max_edge = min(max_edge, 4096)   # 再大只是烧 token，不增信息量
        if info.edge > max_edge:
            scaled = media.downscale(data, info, max_edge)
            if isinstance(scaled, str):
                return f"[错误] {scaled}"
            data, info = scaled

        b64 = media.to_b64(data)
        if len(b64) > media.DEFAULT_MAX_B64:
            return (f"[错误] 图片 base64 后 {media._human(len(b64))} 超过上限 "
                    f"{media._human(media.DEFAULT_MAX_B64)}：先用 run_cmd 缩图/切片"
                    "（ImageMagick `convert -resize`），再 view_image")

        # D9 同款轻事件：落 file.read（读图也是「在读新文件」的活跃探索证据）
        rel = _display_path(p, ws, scratch)
        try:
            self.bb.append_event(
                self.project_id, "file.read",
                {"path": rel[:200], "step": self._step, "kind": "image"},
                session_id=self.session_id, author=self.author)
        except Exception:  # noqa: BLE001 —— 审计失败不影响工具返回
            pass

        from core.llm.provider import ToolOutput, image_block
        head = f"已附上图片：{info.describe()}（{rel}）"
        if str(note or "").strip():
            head += f"\n关注点：{str(note).strip()[:300]}"
        head += "\n（图片本体已随本条结果送入你的视觉通道，直接看，不要 OCR）"
        return ToolOutput(head, [image_block(b64, info.media_type)])

    def _tool_unpack_attachment(self, path: str, clean: bool = True) -> str:
        """附件 zip 服务端解包（2026-10-19）。

        动机：CTF 轨多题共跑/同题重开时，LLM 用 run_cmd 自选目录名拼
        `unzip -o`——只覆盖同名文件，上轮分析残留整批混进 ls 输出，把上下文
        淹掉且常伴 `md5sum: disassembly: Is a directory` exit=1。本工具把
        「解压到哪、怎么清、怎么列」收敛为服务端确定性动作：
        ① 目录固定 scratch/att-<sha12>/（与附件一一对应，附件行直接给路径）；
        ② 解压前清掉目标目录旧残留（clean=true）；
        ③ zipfile 安全解压（zip slip / 加密 / 超限全拒）+ 回结构化清单。
        """
        if not self.artifacts_dir:
            return "[错误] 未装配工作区（artifacts_dir），unpack_attachment 不可用"
        raw = str(path or "").strip()
        if not raw:
            return ("[错误] path 不能为空：传附件 zip 的 /workspace/... 路径"
                    "（附件行原样给出）或只传文件名")
        ws = Path(self.artifacts_dir).parent
        scratch = ws / "scratch"
        p = _resolve_read_path(raw, ws, scratch)
        if not p.is_file() and "/" not in raw and "\\" not in raw:
            # 宽容：只传文件名时自动去附件平铺目录找
            cand = ws / "artifacts" / "attachments" / Path(raw).name
            if cand.is_file():
                p = cand
        if not p.is_file():
            return (f"[错误] 附件不存在: {raw}（相对 scratch 解析；附件行给的是"
                    " /workspace/... 形态，原样传入即可；也可只传文件名）")
        try:
            with open(p, "rb") as fh:
                if fh.read(4) != b"PK\x03\x04":
                    return "[错误] 不是 zip 文件（文件头缺 PK 魔数；仅支持 .zip）"
        except OSError as e:
            return f"[错误] 读取附件失败: {e}"
        sha12 = _attachment_sha12(p)
        dirname = f"{_ATT_DIR_PREFIX}{sha12}"
        dest = scratch / dirname
        # 清旧残留：同题重开/同附件重复解压时，unzip -o 式只覆盖会留下上轮文件
        removed = 0
        if clean and dest.exists():
            try:
                removed = sum(1 for f in dest.rglob("*") if f.is_file())
                shutil.rmtree(dest)
            except OSError as e:
                return f"[错误] 清理目标目录旧残留失败: {e}"
        dest.mkdir(parents=True, exist_ok=True)
        self._unpack_dirs.add(dirname)
        try:
            zf = zipfile.ZipFile(p)
        except (zipfile.BadZipFile, OSError) as e:
            return f"[错误] zip 打开失败: {e}"
        skipped: list[str] = []
        with zf:
            infos = zf.infolist()
            if len(infos) > _UNPACK_MAX_ENTRIES:
                return (f"[错误] zip 条目过多（{len(infos)} > "
                        f"{_UNPACK_MAX_ENTRIES}），拒解压——哪有这种题包，"
                        "先人工确认")
            total = sum(i.file_size for i in infos)
            if total > _UNPACK_MAX_TOTAL:
                return (f"[错误] zip 解压后总大小超限（{_human_size(total)} > "
                        f"512MB），拒解压（疑压缩炸弹）")
            for i in infos:
                if i.flag_bits & 0x1:
                    skipped.append(f"{i.filename}（加密条目）")
                    continue
                mode = i.external_attr >> 16
                if mode and (mode & 0o170000) == 0o120000:
                    skipped.append(f"{i.filename}（符号链接）")
                    continue
                if i.file_size > _UNPACK_MAX_SINGLE:
                    skipped.append(f"{i.filename}（单文件 >128MB）")
                    continue
                target = _safe_zip_target(dest, i.filename)
                if target is None:
                    skipped.append(f"{i.filename}（路径穿越/绝对路径）")
                    continue
                if i.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(i) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        # 结构化清单（浅优先：顶层文件在前，子目录折叠为一行汇总）
        files = sorted((f for f in dest.rglob("*") if f.is_file()),
                       key=lambda f: (len(f.relative_to(dest).parts),
                                      str(f.relative_to(dest)).lower()))
        total_size = sum(f.stat().st_size for f in files)
        head = (f"[解压完成] scratch/{dirname}/（{len(files)} 个文件，"
                f"共 {_human_size(total_size)}")
        if removed:
            head += f"；已清理上轮残留 {removed} 个文件"
        head += "）"
        lines = [head]
        shown = files[:_UNPACK_LIST_CAP]
        width = max((len(str(f.relative_to(dest))) for f in shown), default=0)
        for f in shown:
            rel = str(f.relative_to(dest)).replace(os.sep, "/")
            lines.append(f"  {rel.ljust(width)}  {_human_size(f.stat().st_size):>8}  {_guess_kind(f)}")
        if len(files) > len(shown):
            lines.append(f"  …（另有 {len(files) - len(shown)} 个文件未列出，"
                         "read_file/ls 按需取）")
        if skipped:
            note = "；".join(skipped[:_UNPACK_SKIP_NOTE_CAP])
            more = (f" 等 {len(skipped)} 条" if len(skipped) > _UNPACK_SKIP_NOTE_CAP
                    else "")
            lines.append(f"[跳过] {note}{more}")
        lines.append(f"后续读取: read_file/search_files 用相对 scratch 路径 "
                     f"{dirname}/... （未指定 path 的默认检索已收窄到本目录）")
        body = "\n".join(lines)
        try:
            self.bb.append_event(
                self.project_id, "file.unpack",
                {"dir": dirname, "files": len(files), "bytes": total_size,
                 "cleaned": removed, "source": p.name, "step": self._step},
                session_id=self.session_id, author=self.author)
        except Exception:  # noqa: BLE001 —— 审计事件绝不影响工具返回
            pass
        return body

    # 检索时跳过的目录名（命中即剪枝，不跟随——产物/依赖/临时目录，不是侦察对象）
    _SEARCH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".build-venv",
                         "build", "dist", ".pytest_cache"}
    # 单文件读取上限（2026-10-01 由 5MB 放宽）：超大文件不做逐行正则
    _SEARCH_MAX_FILE = 50 * 1024 * 1024

    def _tool_search_files(self, pattern: str, path: str | None = None,
                           regex: bool = True, glob: str | None = None,
                           max_results: int = 100) -> str:
        """工作区内容检索（2026-09-24）：非 shell 的 grep 替代——
        - 读路径放开（2026-10-01）：不再限定工作区根（只有写才拦边界）；容器
          `/workspace/…` 与 WSL `/mnt/<盘符>/…` 映射回宿主真实路径；path 省略时默认工作区；
        - 纯 Python 逐行匹配（re 正则 / 子串大小写不敏感），无命令执行面；
        - 跳过二进制类后缀与 VCS/依赖目录，单文件 ≤50MB；
        - 命中行截 1000 字符，最多 1000 条；另落 file.search 轻事件（D9 活跃探索识别）。"""
        if not self.artifacts_dir:
            return "[错误] 未装配工作区（artifacts_dir），search_files 不可用"
        if not isinstance(pattern, str) or not pattern:
            return "[错误] pattern 必须为非空字符串"
        ws = Path(self.artifacts_dir).parent.resolve()
        scratch = (ws / "scratch").resolve()
        root = _resolve_read_path(path, ws, scratch) if path else ws
        try:
            root = root.resolve()
        except OSError:
            return f"[错误] 路径不存在: {path}（相对 scratch 解析）"
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
            cap = max(1, min(int(max_results), 1000))
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
            # 大范围检索（未显式指定 path / 指向 scratch 或工作区根）时，剪掉
            # **别的附件解包副本**（scratch/att-* 且非本会话 unpack 过）——
            # 多题共跑时这些工作副本会被误扫进命中，把上下文淹掉（2026-10-19）。
            # 显式 path 指进某个 att-* 目录时不受限（点名要看就给它看）。
            broad = (not path) or root == ws or root == scratch

            def _keep(p: Path) -> bool:
                parts = p.relative_to(ws).parts
                if any(part in self._SEARCH_SKIP_DIRS for part in parts):
                    return False
                if (broad and len(parts) >= 2 and parts[0] == "scratch"
                        and _ATT_DIR_RE.match(parts[1] or "")
                        and parts[1] not in self._unpack_dirs):
                    return False
                return name_filter(p.name)

            files = (p for p in root.rglob("*") if p.is_file() and _keep(p))
        hits: list[str] = []
        scanned = 0
        truncated = False
        for f in files:
          try:
            if f.stat().st_size > self._SEARCH_MAX_FILE:
                continue
            scanned += 1
            shown = _display_path(f, ws, scratch)
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if match(line.rstrip("\n")):
                        hits.append(f"{shown}:{lineno}: {line.strip()[:1000]}")
                        if len(hits) >= cap:
                            truncated = True
                            break
            if truncated:
                break
          except OSError:
            continue
        if not hits:
            return (f"[无命中] pattern={pattern[:100]}（扫描 {scanned} 个文件；"
                    f"范围 {_display_path(root, ws, scratch)}）")
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
        # register_asset（人工 POST /assets 同路径）；越界目标自动转 scope_expand。
        scope_verdict = check_project_scope(self.bb, self.project_id, value)
        if not scope_verdict.allowed:
            return self._scope_expand_request(value, scope_verdict.reason)
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

    # ---------- CTF 题目（challenge-asset，v38，2026-10-14） ----------

    def _challenge_fallback_asset(self) -> str | None:
        """CTF 轨：本线程归属的题目资产 id；无则按线程标题自动补建。否则 None。

        「一个主控线程只发一道题」——线程 ↔ 题目天然 1:1，故发现/意图的**缺省
        锚点就是线程题目节点**。本方法是 `declare_intent` / `bb_add_finding` 未
        显式给 `target_asset_id` 时的兜底：

        1. 线程（或祖先线程）已绑定 → 直接用；
        2. 未绑定但取得到线程标题 → 按标题自动补建一道题并绑回本线程——这正是
           「资产为空时 AI 自己登记题目」：第一次落东西的时刻就是题目诞生的
           时刻，那一刻已有上下文，题目名不会空；
        3. 都失败 → None，由调用方维持原有拒绝口径（**不编造归属**）。

        沿 `parent_thread_id` 向上解析（子专家与主控同题）。仅在**有 thread_id
        的对话轮**发生，任务链会话（`thread_id=''`）恒 None——非 CTF 轨也恒 None。
        """
        if self.track != "ctf" or not self.thread_id:
            return None
        from core.chat.store import resolve_challenge_asset_id, update_thread
        try:
            cid = resolve_challenge_asset_id(self.bb, self.thread_id)
        except Exception:  # noqa: BLE001 —— 兜底解析失败退回原口径，不炸工具链
            return None
        if cid:
            return cid
        try:
            row = self.bb.conn.execute(
                "SELECT title FROM chat_threads WHERE id=?",
                (self.thread_id,)).fetchone()
            title = str(row["title"] or "").strip() if row is not None else ""
            if not title:
                return None
            r = register_challenge(self.bb, self.project_id, title,
                                   thread_id=self.thread_id,
                                   author=self.author,
                                   session_id=self.session_id)
            cid = str(r.get("id") or "").strip()
            if cid:
                update_thread(self.bb, self.thread_id, challenge_asset_id=cid)
        except Exception:  # noqa: BLE001
            return None
        return cid or None

    def _tool_bb_declare_challenge(self, name: str,
                                   description: str = "",
                                   tags: list | None = None) -> str:
        """CTF 轨：登记/复用一道题目，并绑定到当前线程（challenge-asset v38）。

        用户把「题目描述 + 附件」一起丢进输入框、黑板还没有对应题目节点时调用。
        绑完之后，本线程后续的 `declare_intent` / `bb_add_finding` 不必再传
        `target_asset_id`，会自动归到这道题下。附件本身不用登记（走题目子资产
        通道）。同一道题重复调用 = 复用既有节点，不会插重复行。
        """
        if self.track != "ctf":
            return ("[拒绝] bb_declare_challenge 仅 CTF 轨可用"
                    "（渗透/红队/研究轨的目标是站点/样本，不是题目）")
        tid = (self.thread_id or "").strip()
        if not tid:
            return "[拒绝] bb_declare_challenge 只在对话轮（带 thread_id）内可用"
        try:
            r = register_challenge(self.bb, self.project_id, name,
                                   description=description, tags=tags,
                                   thread_id=tid, author=self.author,
                                   session_id=self.session_id)
        except ValueError as e:
            return f"[错误] {e}"
        cid = str(r.get("id") or "").strip()
        try:
            from core.chat.store import update_thread
            update_thread(self.bb, tid, challenge_asset_id=cid)
        except Exception:  # noqa: BLE001 —— 绑不上不阻断：资产已建，后续可重绑
            pass
        self.last_progress_step = self._step
        reply = (f"challenge={cid} name={r['value']} created={r['created']}"
                 f" thread={tid}（已绑定到本线程）")
        if r["created"]:
            reply += ("\n后续本线程的 declare_intent/bb_add_finding 不必再填"
                      " target_asset_id，会自动归到这道题下；附件也不必登记"
                      "（走本题目的子资产通道）。附件 zip 用 "
                      "unpack_attachment(path=\"/workspace/artifacts/attachments/"
                      "<文件名>\") 解压到 scratch/att-<sha12>/（服务端清旧残留+"
                      "安全解压+返回清单），之后 read_file/search_files 用 "
                      "att-<sha12>/... 相对路径读；不要用 run_cmd 拼 unzip。")
        else:
            reply += ("\n[提示] 命中既有同名题目，已复用并改绑到本线程"
                      "（不插重复行）。")
        return reply

    def _tool_ctf_submit_flag(self, flag: str, evidence: str = "",
                              source: str = "", transform: str = "",
                              challenge_id: str = "",
                              candidate: bool = False) -> str:
        """判定并登记一个 flag（ctf-flagjudge，2026-10-19）。

        **为什么需要这个工具**：此前「这道题解出来没有」完全靠模型自述——它会
        把题面里的 `flag{example}` 当答案、把 `{% csrf_token %}` 当 flag、把
        别的题的 flag 当本题答案，也没人验证「解码得到 flag」这句是不是真的。
        本工具把 hxbai 的三道机械判定搬进来：

        1. **格式置信**（`flagjudge.flagishness`）：可信前缀 + 内层熵 + 诱饵惩罚；
        2. **变换回环**（`flagjudge.roundtrip_verify`）：给了 source+transform 时
           验证「flag 按该变换编码回去确实出现在 source 里」——真解码必然回环，
           瞎猜不会；
        3. **跨题防串**（`flagjudge.neighbor_owner`）：本题内层与**其它题目**
           已登记 flag 互为子串 → 判为串题拒收。

        判定通过后落 **flag 台账**（`challenge_flags` 表，challenge-flag-ledger
        v47）——flag 不再是 findings：findings 的「正式漏洞五项必填契约」是为渗透
        结论设计的，CTF flag 不是漏洞，此前只能硬写套话过闸。台账里 AI 判定通过
        落 ``verdict='pending'``（**候选**，待人工确认）。

        **候选不计解出**（2026-10-20 方案 A）：题目状态由台账派生，但只有
        ``accepted``（人工确认）才计入 solved——``pending`` 再多也不算。此前
        pending 也计入，导致 AI 提交一条假 flag 就把题目判成 solved、调度层立刻
        停派，**题目被一条假答案提前终止**（用户实测 2/1 显示「已拿满」）。
        无人值守场景下用 ``ctf_conclude`` 主动收尾停止派工（但状态仍是 partial）。

        ``candidate=true`` 走**候选线索**通道：只落一条 intel finding（过程留痕），
        **不进台账**、不判为解出——线索不是答案。注意它与 ``pending`` 不同：
        ``pending`` 是「形态判定通过的 flag，等人工确认」，``candidate=true`` 是
        「连 flag 形态都不满足的线索」（管理员密码/API key 之类）。

        **本工具不联网提交**，只做本地判定与登记。
        """
        if self.track != "ctf":
            return ("[拒绝] ctf_submit_flag 仅 CTF 轨可用"
                    "（渗透/红队/研究轨没有 flag 概念）")
        raw = str(flag or "").strip()
        if not raw:
            return "[拒绝] flag 不能为空"
        # 模型常把整行输出连上下文一起传进来——先抽候选，抽不到再用原文
        cands = flagjudge.extract_candidates(raw)
        token = raw
        if cands:
            ranked = sorted(cands, key=lambda c: (-flagjudge.flagishness(c), c))
            token = ranked[0]
        # ① 格式置信
        score = flagjudge.flagishness(token)
        if flagjudge.natural_language_inner(token):
            return (f"[拒绝] {token} 的内层是自然语言（≥4 个 CJK 字或 ≥3 个英文单词）"
                    "——这是题面里的示例占位符，不是真 flag。请提供实际解出的值。")
        # 归属先定（后面要用题目声明的 flag 格式加权、也要查「已否决清单」）
        cid = str(challenge_id or "").strip() or (self._challenge_fallback_asset() or "")
        declared_format = self._challenge_flag_format(cid) if cid else ""
        if declared_format and self._flag_matches_format(token, declared_format):
            # 题目声明了格式且本 flag 匹配 → 加权（**只加不减**：声明是提示不是
            # 约束，真实比赛里题面写 flag{} 实际是 DASCTF{} 的情况很常见，
            # 硬卡格式会误杀）。
            score += flagjudge.FORMAT_MATCH_BONUS
        if not candidate and score < flagjudge.FLAGISH_FLOOR:
            why = ("前缀是诱饵词（fake/example/test/dummy…）" if flagjudge.is_bait_prefix(token)
                   else "前缀不可信或内层信息量不足")
            return (f"[拒绝] {token} 置信分 {score:.1f} < 阈值 "
                    f"{flagjudge.FLAGISH_FLOOR:.1f}（{why}）——不像正式 flag。"
                    "若这是管理员密码/API key 这类**候选线索**，加 candidate=true 再报"
                    "（会作为 intel 线索登记，不判为解出）。")
        # ② 变换回环
        if source and transform:
            rt = flagjudge.roundtrip_verify(token, source, transform)
            if rt is False:
                return (f"[拒绝] 回环验证失败：把 {token} 按 {transform} 编码回去后"
                        "**没有**出现在你给的 source 里——说明这个 flag 不是从那段数据"
                        "解出来的（解错了或猜的）。请重新核对解码过程。")
        # ③ 跨题防串（同项目其它题目已登记的 flag）——先定归属，好把本题排除
        owner = self._flag_neighbor_owner(token, self_code=cid)
        if owner:
            return (f"[拒绝] 跨题串号：{token} 与题目 {owner} 已登记的 flag 内层高度"
                    "相似（互为子串）——这是别题的答案，不是本题的。多题并发时"
                    "共享目录/上游扫描结果里常混入别题 flag，请回到本题自己的线索。")
        # 归属：显式 challenge_id > 本线程题目（自动补建）
        if not cid:
            return ("[拒绝] 无法确定这道题：先 bb_declare_challenge 登记题目"
                    "（或显式传 challenge_id），flag 必须挂到具体题目上。")
        asset = self.bb.get_asset(cid)
        if asset is None or asset.get("project_id") != self.project_id:
            return f"[错误] 题目不存在: {cid}"
        ch_name = str(asset.get("value") or cid)
        # ④ 人工否决清单：这串被标过「错误 flag」→ 直接拦，别重复烧轮次
        from core.blackboard.challenge_flags import is_rejected, record_flag
        prior = is_rejected(self.bb, self.project_id, cid, token)
        if prior is not None:
            reason = str(prior.get("verdict_reason") or "").strip()
            return (f"[拒绝] {token} 已被人工标记为**错误 flag**"
                    + (f"（理由：{reason}）" if reason else "")
                    + "——不要再提交同一串。请回题目重新找线索；"
                    "若确认人工判断有误，让用户在题目卡片上撤销该否决。")
        brief = (evidence or "").strip()
        steps = [{
            "desc": f"解题路径（{ch_name}）",
            "type": "cmd",
            "code": brief[:2000] or f"提交 flag: {token}",
            "expected": token,
            "target": ch_name,
        }]
        ev = {
            "repro_steps": steps,
            "flag": token,
            "flag_confidence": round(score, 2),
        }
        if source and transform:
            ev["flag_transform"] = {"transform": transform,
                                    "source_head": source[:500]}
        if candidate:
            # 候选线索：只落 intel finding（过程留痕），**不进 flag 台账**、
            # 不判为解出。线索不是答案——进了台账就会把题目算成 solved。
            try:
                r = self.bb.add_finding(
                    self.project_id, "flag-candidate", f"[候选 flag] {token}",
                    severity="high", status="unverified", evidence=ev,
                    target_asset_id=cid, author=self.author,
                    summary=brief[:2000], category="intel", track=self.track)
            except ValueError as e:
                return f"[拒绝] {e}"
            self.last_progress_step = self._step
            return ("[已登记] " + " · ".join([
                f"flag={token}", f"confidence={score:.1f}",
                f"challenge={cid}（{ch_name}）",
                f"finding={r['id']} merged={r['merged']}",
                "按**候选线索**登记（intel/unverified，不进 flag 台账、不判为解出）"
                "——确认是正式 flag 后重发本工具（不加 candidate）"]))
        # 正式 flag：① 进 flag 台账（唯一真值，题目状态由它派生）
        try:
            ledger = record_flag(
                self.bb, self.project_id, cid, token,
                flag_format=declared_format, confidence=score,
                source=brief[:2000], transform=str(transform or ""),
                judged_by="ai", verdict="pending", author=self.author,
                session_id=str(self.session_id or ""))
        except ValueError as e:
            return f"[拒绝] {e}"
        if ledger.get("rejected"):
            return (f"[拒绝] {token} 已被人工标记为错误 flag，不予登记。")
        # ② 同时落一条**过程留痕** finding（intel/unverified）——不受正式漏洞
        # 五项必填契约约束（那是渗透结论的要求，CTF flag 不是漏洞）。
        finding_id = ""
        try:
            r = self.bb.add_finding(
                self.project_id, "flag-capture", f"解出 flag：{token}",
                severity="critical", status="unverified", evidence=ev,
                target_asset_id=cid, author=self.author,
                summary=(f"{ch_name}：按 CTF 解题路径取得 flag {token}。"
                         + (f"路径：{brief[:1200]}" if brief else "")),
                category="intel", track=self.track)
            finding_id = str(r.get("id") or "")
        except ValueError as e:  # noqa: BLE001 —— 留痕失败不影响台账（台账才是真值）
            log.warning("flag 过程留痕 finding 落库失败: %s", e)
        if finding_id:
            try:
                from core.blackboard.challenge_flags import set_finding_id
                set_finding_id(self.bb, self.project_id, str(
                    (ledger.get("row") or {}).get("id") or ""), finding_id)
            except Exception:  # noqa: BLE001
                pass
        self.last_progress_step = self._step
        quota = self._challenge_flag_quota(cid)
        got = self._challenge_accepted_count(cid)
        # 候选数单独数（pending 不计解出，2026-10-20 方案 A）
        try:
            from core.blackboard.challenge_flags import candidate_flags
            n_cand = len(candidate_flags(self.bb, self.project_id, cid))
        except Exception:  # noqa: BLE001 —— 计数失败不影响回执主体
            n_cand = 0
        notes = [f"flag={token}", f"confidence={score:.1f}",
                 f"challenge={cid}（{ch_name}）",
                 f"flag_id={(ledger.get('row') or {}).get('id')}",
                 f"已确认 {got}/{quota}（候选 {n_cand} 条待人工确认）"]
        if source and transform:
            rt = flagjudge.roundtrip_verify(token, source, transform)
            notes.append("回环验证=" + ("通过" if rt else "无法判定（变换名未识别）"))
        else:
            # **证据强度必须说清**：没有 transform 就没跑回环，这条只是形态判定。
            notes.append("**仅形态判定通过，未做变换回环**"
                         "（下次带 source+transform 可加固证据）")
        if got >= quota:
            notes.append("**已确认 flag 已达配额，调度层将不再为它派 Worker**")
        elif n_cand and got + n_cand >= quota:
            # 关键：候选满了不等于解出。这行是「别收尾，去确认」的提示。
            notes.append(f"候选已够 {got + n_cand}/{quota}，但**候选不计解出**——"
                         "若确认答案已拿到，用 ctf_conclude 主动收尾（停止派工）；"
                         "否则继续探索，别因为「数字看起来满了」就停")
        else:
            notes.append(f"还需 {quota - got} 条已确认（多 flag 题目，继续找）")
        notes.append("已入 flag 台账（verdict=pending 候选，等人工确认；"
                     "用户可在题目卡片上确认或标记为错误）")
        return "[已登记] " + " · ".join(notes)

    def _challenge_flag_format(self, challenge_id: str) -> str:
        """题目声明的 flag 格式（meta.flag_format，''=未声明）。"""
        if not challenge_id:
            return ""
        try:
            asset = self.bb.get_asset(challenge_id) or {}
            meta = asset.get("meta") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            return str((meta or {}).get("flag_format") or "").strip()
        except Exception:  # noqa: BLE001 —— 读不到就不加权，不阻断提交
            return ""

    @staticmethod
    def _flag_matches_format(token: str, declared: str) -> bool:
        """flag 前缀是否匹配题目声明的格式（``flag{}`` → 前缀 ``flag``）。"""
        head = str(declared or "").split("{", 1)[0].strip().lower()
        if not head:
            return False
        return str(token or "").split("{", 1)[0].strip().lower() == head

    def _challenge_flag_quota(self, challenge_id: str) -> int:
        try:
            from core.blackboard.challenge_flags import challenge_flag_quota
            return challenge_flag_quota(self.bb, self.project_id, challenge_id)
        except Exception:  # noqa: BLE001
            return 1

    def _challenge_accepted_count(self, challenge_id: str) -> int:
        try:
            from core.blackboard.challenge_flags import accepted_flags
            return len(accepted_flags(self.bb, self.project_id, challenge_id))
        except Exception:  # noqa: BLE001
            return 0

    def _tool_ctf_ammo(self, obs_text: str, hint_text: str = "",
                       users: list | None = None,
                       known_creds: list | None = None,
                       pairs: bool = False,
                       limit: int = 200) -> str:
        """CTF 轨：生成弱口令/默认口令候选字典（ctf-ammo，2026-10-19）。

        **为什么需要**：弱口令题里模型的典型失败是「凭记忆打 admin/admin、admin/123456
        就放弃」。真正有效的做法是把**题面线索**（产品名/年份/管理员名字/公司名）
        机械展开成几十到几百条候选——这件事不需要 LLM，只需要确定性变异
        （`core.ctf.ammo`）。本工具是那条管线的唯一出口。

        产出写进 `scratch/ctf-ammo-<tag>.txt` 并返回**相对 scratch 的路径**：
        run_cmd 的 cwd 恒为 scratch，拿到路径即可直接喂 hydra/ffuf。
        **不执行爆破、不发网络请求**；候选表恒不含 flag 形态串（防自己泄题）。
        """
        if self.track != "ctf":
            return "[拒绝] ctf_ammo 仅 CTF 轨可用（其它轨道的弱口令走 skill 打法）"
        obs = str(obs_text or "")
        hint = str(hint_text or "")
        if not (obs.strip() or hint.strip() or (users or []) or (known_creds or [])):
            return ("[拒绝] 至少给一条线索：obs_text（观测原文）/ hint_text（题面提示）"
                    "/ users / known_creds——没有素材时生成的只有通用弱口令，价值很低")
        try:
            from core.ctf import ammo
            lim = max(1, min(int(limit or 200), ammo.MAX_CANDIDATES))
            prods = ammo.harvest_products(hint) + ammo.harvest_products(obs)
            if pairs:
                rows = ammo.build_cred_pairs(obs, users=[str(u) for u in (users or [])] or None,
                                             hint_text=hint,
                                             known_creds=[str(c) for c in (known_creds or [])] or None,
                                             year=None, limit=lim)
            else:
                rows = ammo.build_candidates(obs, hint_text=hint,
                                             known_creds=[str(c) for c in (known_creds or [])] or None,
                                             year=None, limit=lim)
        except Exception as e:  # noqa: BLE001 —— 生成失败如实回文本，不炸工具链
            return f"[工具异常] ctf_ammo 生成失败: {type(e).__name__}: {e}"
        if not rows:
            return ("[无命中] 没能从这些线索里合成候选（产品指纹需语境，如 `Server:`/版本号/"
                    "`指纹`；中文题面可直接把「管理员叫 xxx」放进 hint_text）")
        # 落盘：路径必须相对 scratch（run_cmd 的 cwd），否则模型要自己换算
        rel = ""
        if self.artifacts_dir:
            try:
                scratch = Path(self.artifacts_dir).parent / "scratch"
                scratch.mkdir(parents=True, exist_ok=True)
                tag = hashlib.sha256((obs + hint).encode("utf-8", "replace")).hexdigest()[:8]
                name = f"ctf-ammo-{tag}.txt"
                header = ("候选口令（ctf-ammo 生成；"
                          + ("user:pass 成对" if pairs else "纯口令")
                          + f"；产品指纹={','.join(prods) or '无'}"
                          + "；本表不含 flag）")
                (scratch / name).write_text(ammo.render_creds(rows, header=header),
                                            encoding="utf-8")
                rel = name
            except Exception:  # noqa: BLE001 —— 落盘失败仍回候选，不阻断
                rel = ""
        head = [f"[候选 {len(rows)} 条]" + (f" 已写入 scratch/{rel}" if rel else ""),
                f"产品指纹: {', '.join(prods) or '未识别（线索里没有指纹语境）'}"]
        if pairs:
            head.append("用法示例: hydra -L users.txt -P " + (rel or "<本文件>")
                        + " <目标> <服务>（先确认目标授权范围）")
        else:
            head.append("用法示例: 配合已知用户名逐条尝试；或自行拼 user:pass")
        # 口径必须落在回执里（不只在工具描述里）：模型读到「已生成」很容易误以为
        # 「已经打过了」，把一次候选生成当成一次成功的爆破。
        head.append("（本工具只生成候选表，**不执行爆破、不发任何网络请求**；"
                    "是否尝试、试多少条、要不要限速由你决定）")
        head.append("候选（前 20 条，按价值排序）：")
        head.extend(f"  {i}. {r}" for i, r in enumerate(rows[:20], 1))
        if len(rows) > 20:
            head.append(f"  …共 {len(rows)} 条，完整表见 scratch/{rel or '<未落盘>'}") if rel \
                else head.append(f"  …共 {len(rows)} 条")
        return "\n".join(head)

    def _tool_ctf_abandon(self, challenge_id: str = "",
                          abandoned: bool = True,
                          reason: str = "") -> str:
        """CTF 轨：放弃整道题 / 撤销放弃（ctf-abandon，2026-10-20）。

        **为什么需要这个工具**（用户实测真问题）：`set_abandoned` 此前只挂在
        HTTP API（`core/api/app.py` 的 `POST /api/challenges/{id}/abandon`，
        `author="human"`），**Agent 侧没有任何工具能表达「这题我不解了」**。
        于是模型想收尾时的唯一可见出口是 `bb_asset_status("tested_clean")`，
        而 CTF 轨根本不该标那个（终态判据是 flag 台账，见 `_ctf_task_state`
        注释）——它必然撞门禁（题目行「父节点由子树派生」拒、附件子资产
        「须死路背书」拒），再为了凑 dead_end 去查 event id，三四个轮次白烧。
        补上这个出口，模型才有「合法地停」的地方。

        **语义边界（别和 close_intent 混）**：本工具是**整题级**判定——
        放弃后调度层不再为该题派 Worker。单条假设不成立是 `close_intent`
        (outcome="dead_end")，题还能继续解。两者不可互换：把「一条路死了」
        当成「整题放弃」会让多方向题被误弃。
        """
        if self.track != "ctf":
            return ("[拒绝] ctf_abandon 仅 CTF 轨可用"
                    "（其它轨道的「不再测」是 bb_asset_status 的 tested_clean/na）")
        cid = str(challenge_id or "").strip() or (self._challenge_fallback_asset() or "")
        if not cid:
            return ("[拒绝] 无法确定这道题：先 bb_declare_challenge 登记题目"
                    "（或显式传 challenge_id）")
        asset = self.bb.get_asset(cid)
        if asset is None or asset.get("project_id") != self.project_id:
            return f"[错误] 题目不存在: {cid}"
        if str(asset.get("type") or "") != "challenge":
            return (f"[拒绝] {cid} 不是题目（type={asset.get('type')}）"
                    "——放弃是题目级动作，不能对附件/靶标这类子资产用。"
                    "子资产「不再看」直接不碰即可，无需登记。")
        from core.blackboard.challenge_flags import (challenge_flag_summary,
                                                     set_abandoned)
        try:
            set_abandoned(self.bb, self.project_id, cid,
                          abandoned=bool(abandoned), author=self.author)
        except ValueError as e:
            return f"[拒绝] {e}"
        # 回执带题目现状：模型据此判断「还能不能救」——已拿到的 flag 放弃后
        # 仍然计入（放弃=不继续投入，不是清空结论），这点必须说清否则它会以为
        # 放弃把进度也扔了。
        try:
            st = (challenge_flag_summary(self.bb, self.project_id, [cid]) or {}).get(cid) or {}
        except Exception:  # noqa: BLE001 —— 汇总失败不影响放弃动作
            st = {}
        name = str(asset.get("value") or cid)
        note = str(reason or "").strip()
        if abandoned:
            parts = [f"题目={cid}（{name}）已标记**放弃**",
                     f"已登记 flag {int(st.get('accepted') or 0)}/"
                     f"{int(st.get('quota') or 0)} 条保留（放弃不清空结论）",
                     "调度层不再为该题派新 Worker"]
            if note:
                parts.append(f"放弃原因已留痕：{note[:300]}")
            parts.append("如需继续解，用 ctf_abandon(abandoned=false) 撤销")
            return " · ".join(parts)
        return (f"题目={cid}（{name}）已**撤销放弃**，可以继续解题"
                + (f"（原放弃原因：{note[:200]}）" if note else ""))

    def _tool_ctf_conclude(self, challenge_id: str = "",
                           concluded: bool = True,
                           reason: str = "") -> str:
        """CTF 轨：主动收尾（停止派工） / 撤销收尾（ctf-conclude，2026-10-20）。

        **为什么需要这个工具**（用户裁决方案 A 的配套）：把 `pending` 从
        solved 派生里摘出去之后，「AI 判定通过」只能把题目停在 `partial`——
        必须等人点确认。有人值守时这是对的（人工确认是强证据，也是唯一能
        拦住假 flag 的闸）；但**无人值守通宵跑题会全部卡在 pending 白烧预算**：
        明明解出来了，没人点按钮，调度层就一直派 Worker 重跑同一道题。

        所以需要一个**显式收尾**出口：主控看过证据、判定「答案已经拿到」→
        置 `meta.concluded`，调度层停派工。但**不把它伪装成 solved**——
        状态仍是 partial，前端仍显示「待人工确认」。收尾只表达「别再派了」，
        不表达「这条 flag 是对的」；后者只有人能判。

        **与 `ctf_abandon` 的边界**（两个出口都不能混用）：
        - 收尾 = 做完了，等人工确认（`meta.concluded`，状态 partial）；
        - 放弃 = 不做了，可能没解出（`meta.abandoned`，状态 abandoned）。
        把「解出来了但没人确认」当成放弃，会让 coverage 把它算成没解出；
        把「解不出来」当成收尾，会让复盘误以为这题已经拿下。

        **不强制要求已有 accepted flag**：那正是本工具存在的理由（accepted
        只有人能落）。但要求**至少提交过一条 flag**——一条都没提交就收尾，
        几乎必然是模型自嗨（「我看懂了，就是 flag」），必须拦住并指回
        ctf_submit_flag。
        """
        if self.track != "ctf":
            return ("[拒绝] ctf_conclude 仅 CTF 轨可用"
                    "（其它轨道的「这一轮做完了」就是直接给结论，无需单独登记）")
        cid = str(challenge_id or "").strip() or (self._challenge_fallback_asset() or "")
        if not cid:
            return ("[拒绝] 无法确定这道题：先 bb_declare_challenge 登记题目"
                    "（或显式传 challenge_id）")
        asset = self.bb.get_asset(cid)
        if asset is None or asset.get("project_id") != self.project_id:
            return f"[错误] 题目不存在: {cid}"
        if str(asset.get("type") or "") != "challenge":
            return (f"[拒绝] {cid} 不是题目（type={asset.get('type')}）"
                    "——收尾是题目级动作，不能对附件/靶标这类子资产用。")
        from core.blackboard.challenge_flags import (challenge_flag_summary,
                                                     set_concluded)
        name = str(asset.get("value") or cid)
        try:
            st = (challenge_flag_summary(self.bb, self.project_id, [cid]) or {}).get(cid) or {}
        except Exception:  # noqa: BLE001 —— 汇总失败不阻塞收尾
            st = {}
        got = int(st.get("accepted") or 0)
        n_cand = int(st.get("candidate") or 0)
        quota = int(st.get("quota") or 0)
        # 收尾前必须「至少提交过一条」：一条都没有 = 没有任何可复核的答案，
        # 此时收尾等于把题目静默弃置（既不 solved 也不再跑），最坏的一种结局。
        if concluded and got + n_cand == 0:
            return (f"[拒绝] {cid}（{name}）还没有任何 flag 记录——"
                    "收尾只适用于「答案已提交、等人工确认」的情况。"
                    "如果确实解出来了，先用 ctf_submit_flag 提交；"
                    "如果解不出来，用 ctf_abandon(reason=...) 明确放弃；"
                    "如果只是某条思路不通，用 close_intent(outcome=\"dead_end\")，题还能继续解。")
        try:
            set_concluded(self.bb, self.project_id, cid,
                          concluded=bool(concluded), reason=reason,
                          author=self.author)
        except ValueError as e:
            return f"[拒绝] {e}"
        note = str(reason or "").strip()
        if not concluded:
            return (f"题目={cid}（{name}）已**撤销收尾**，调度层可以重新为该题派工"
                    + (f"（原收尾理由：{note[:200]}）" if note else ""))
        parts = [f"题目={cid}（{name}）已**收尾**，调度层不再为该题派新 Worker",
                 f"已确认 {got}/{quota} 条、候选 {n_cand} 条待人工确认"
                 "（收尾不改变这个数字）"]
        if got < quota:
            # 关键：收尾 ≠ 解出。必须说清「谁还能推翻这个判断、怎么推翻」，
            # 否则模型会把它当成「我已经解出来了」的收尾凭证。
            parts.append("**注意：这不算「已解出」**——题目仍是待人工确认；"
                         "只有用户在题目卡片上点确认，候选 flag 才转 accepted、"
                         "状态才变 solved")
        if note:
            parts.append(f"收尾理由已留痕：{note[:300]}")
        parts.append("如需重新开跑，用 ctf_conclude(concluded=false) 撤销")
        return " · ".join(parts)

    def _flag_neighbor_owner(self, flag: str, self_code: str = "") -> str | None:
        """跨题串号检测：本项目**其它题目**子树内已登记的 flag 里，找与本 flag
        内层高度相似（互为子串，长度 ≥8）的一条，返回其题目名（无命中 None）。

        ``self_code`` 是本次提交归属的题目 id——它自己的旧 flag 不算串号
        （同题重报要走并入而不是被拦）。读路径只走 `list_findings` + `list_assets`
        （纯只读），不落任何写。异常一律返回 None——防串是加分项，绝不能因为
        它把正常登记打回。
        """
        try:
            from core.blackboard.attackpath import _subtree_ids
            assets = self.bb.list_assets(self.project_id)
            findings = self.bb.list_findings(self.project_id)
            registry: dict[str, str] = {}
            # 主源：flag 台账（v47 起 flag 的唯一真值）。**含 rejected**——被人工
            # 否决的串同样属于那道题，别题再拿它提交仍然算串号。
            try:
                ledger = self.bb.conn.execute(
                    "SELECT challenge_id, flag FROM challenge_flags WHERE project_id=?",
                    (self.project_id,)).fetchall()
                for row in ledger:
                    val = str(row["flag"] or "").strip()
                    if val:
                        registry.setdefault(val, str(row["challenge_id"] or ""))
            except Exception:  # noqa: BLE001 —— 旧库无台账时退回 findings 扫描
                pass
            # 兼容源：v47 之前落在 findings 里的 flag（存量未迁移时仍要防串）。
            for cid in [a["id"] for a in assets
                        if a.get("type") == "challenge" and not a.get("parent_id")]:
                subtree = _subtree_ids(assets, cid)
                for f in findings:
                    if f.get("target_asset_id") not in subtree:
                        continue
                    val = str((f.get("evidence") or {}).get("flag") or "").strip()
                    if val:
                        registry.setdefault(val, cid)
            if not registry:
                return None
            owner = flagjudge.neighbor_owner(flag, registry, self_code=self_code)
            if not owner:
                return None
            row = self.bb.get_asset(owner)
            return str((row or {}).get("value") or owner)
        except Exception:  # noqa: BLE001 —— 防串失败按无命中
            return None

    def _tool_bb_delete_asset(self, asset_id: str) -> str:
        asset = self.bb.get_asset(asset_id)
        if asset is None or asset.get("project_id") != self.project_id:
            return f"[错误] 资产不存在: {asset_id}"
        if asset.get("type") == "binary":
            return "[拒绝] binary 样本不能用 bb_delete_asset，请使用样本删除流程"
        try:
            deleted = self.bb.delete_asset(asset_id, author=self.author)
        except LookupError as e:
            return f"[错误] {e}"
        except ValueError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return (f"asset.deleted={deleted['id']} type={deleted['type']} "
                f"value={deleted['value']}")

    def _tool_bb_delete_assets(self, asset_ids: list | str) -> str:
        """批量删除非 binary 叶子资产（语义同 bb_delete_asset，一次一批）。

        动机（2026-10-09 事故）：资产清洗要删几百条泛解析域名时，模型只能用
        bb_query 分页 + 逐条 bb_delete_asset，陷入「怎么翻页 / 怎么枚举」的推理
        死循环（实测单步思考 5.2 万字符、该线程 222 个 LLM 步）。批量原语把
        「枚举 + 删除」压成一次调用。

        逐条尝试、不整批回滚：单条失败（有子资产 / 被 finding 引用 / binary /
        不存在）只记进 failures，其余照删——资产清洗场景下整批回滚会让一条挡路
        的资产卡死全部进度。单次上限 `_BB_BATCH_DELETE_MAX`，超出要求分批。
        """
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        seen: set[str] = set()
        ids = [x for x in (str(v).strip() for v in (asset_ids or []))
               if x and not (x in seen or seen.add(x))]
        if not ids:
            return "[错误] asset_ids 不能为空（传资产 id 数组，先用 bb_query what=assets 取 id）"
        if len(ids) > _BB_BATCH_DELETE_MAX:
            return (f"[拒绝] 单次最多删 {_BB_BATCH_DELETE_MAX} 个（收到 {len(ids)}）"
                    f"——分批调用，避免一次误删面过大")
        deleted: list[dict] = []
        failed: list[dict] = []
        stop_reason = ""
        for aid in ids:
            # 长批次可被人类中止打断（dispatcher 侧的中止事件，None=未接入），
            # 已删的不回滚——与 run_cmd 走同一个 abort_event。
            if self.abort_event is not None and self.abort_event.is_set():
                stop_reason = "已中止，剩余未处理"
                break
            asset = self.bb.get_asset(aid)
            if asset is None or asset.get("project_id") != self.project_id:
                failed.append({"asset_id": aid, "error": "资产不存在"})
                continue
            if asset.get("type") == "binary":
                failed.append({"asset_id": aid,
                               "error": "binary 样本不能用 bb_delete_assets，请走样本删除流程"})
                continue
            try:
                row = self.bb.delete_asset(aid, author=self.author)
            except (LookupError, ValueError) as e:
                failed.append({"asset_id": aid, "error": str(e)})
                continue
            deleted.append({"asset_id": row["id"], "value": row["value"]})
        if deleted:
            self.last_progress_step = self._step
        return json.dumps({
            "deleted": len(deleted),
            "failed": len(failed),
            "deleted_assets": deleted[:50],
            "deleted_truncated": max(0, len(deleted) - 50),
            "failures": failed[:50],
            "failures_truncated": max(0, len(failed) - 50),
            "note": ("失败项多为「有子资产 / 被 finding 引用 / binary / 不存在」"
                     "——先 bb_query 查清再处理；剩余条目可再调一批"),
            "stopped": stop_reason,
        }, ensure_ascii=False)

    def _tool_bb_merge_assets(self, source_asset_id: str, target_asset_id: str,
                              reason: str = "") -> str:
        if not reason or not reason.strip():
            return "[拒绝] 合并必须提供 reason，说明认定两个资产为同一实体的证据"
        source = self.bb.get_asset(source_asset_id)
        target = self.bb.get_asset(target_asset_id)
        if source is None or source.get("project_id") != self.project_id:
            return f"[错误] 源资产不存在: {source_asset_id}"
        if target is None or target.get("project_id") != self.project_id:
            return f"[错误] 目标资产不存在: {target_asset_id}"
        if source.get("type") == "binary" or target.get("type") == "binary":
            return "[拒绝] binary 样本不能通过 bb_merge_assets 合并，请使用样本生命周期流程"
        try:
            merged = self.bb.merge_assets(
                self.project_id, source_asset_id, target_asset_id,
                author=self.author, reason=reason,
            )
        except LookupError as e:
            return f"[错误] {e}"
        except ValueError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return (f"asset.merged source={merged['source_asset_id']} "
                f"target={merged['target_asset_id']} "
                f"findings={merged['findings_moved']} "
                f"merged={merged.get('findings_merged', 0)} "
                f"intents={merged['intents_moved']} "
                f"children={merged['children_moved']} alias="
                f"{merged['source_type']}:{merged['source_value']}")

    def _tool_bb_asset_status(self, asset_id: str, status: str,
                              note: str | None = None,
                              expected_revision: int | None = None,
                              tested_what: str | None = None,
                              viewpoint: str | None = None,
                              why_no_finding: str | None = None) -> str:
        a0 = self.bb.get_asset(asset_id)
        if a0 is None or a0.get("project_id") != self.project_id:
            return f"[错误] 资产不存在: {asset_id}"
        # 注：CTF 轨禁 tested_clean 的判定在 `_dispatch_once` 入口（**意图先行闸
        # 之前**，ctf-no-tested-clean 2026-10-20）——放这里会被意图先行闸先截，
        # 模型只会看到「先去声明意图」，被误导成「问题出在没立意」。
        # H2 乐观锁：本地预检给独立 [冲突] 前缀（store 层仍兜底同判）
        if expected_revision is not None and \
                int(a0.get("revision") or 1) != int(expected_revision):
            return (f"[冲突] 资产已被他人修改（当前 revision={a0.get('revision')}，"
                    f"请求基于 {expected_revision}）——先 bb_query 现查再重试")
        # tested_clean 四问门禁（2026-09-29，sess-1d692817a5d0 误判复盘）：答案
        # 结构化落 detail→asset.status_changed 事件全文可对账；note 截 200 只留
        # 摘要，四问全文在 detail。
        detail = None
        if status == "tested_clean":
            missing = [k for k, v in (("tested_what", tested_what),
                                      ("viewpoint", viewpoint),
                                      ("why_no_finding", why_no_finding))
                       if not (v and str(v).strip())]
            if missing:
                return ("[拒绝] tested_clean 须回答四问（逐资产独立判定，"
                        "「同模板/基线一致」不等于已测试）：①tested_what 你对该资产"
                        "自己测了什么（路径/方法/响应特征清单）；②viewpoint 探测视角"
                        "（docker 出口/直连/浏览器，遇 WAF/WebVPN 拦截页须说明排除"
                        "依据）；③why_no_finding 为何是「无发现」而非「没测到」、"
                        "还剩什么可立的新意图；④另有直接围绕本资产的 closed/dead_end"
                        f" 意图背书（服务端校验）。缺：{','.join(missing)}")
            detail = {"tested_what": str(tested_what).strip(),
                      "viewpoint": str(viewpoint).strip(),
                      "why_no_finding": str(why_no_finding).strip()}
            if not (note and note.strip()):
                note = "；".join(f"{k}：{v[:80]}" for k, v in detail.items())
        try:
            a = self.bb.set_asset_status(asset_id, status, note=note,
                                         author=self.author,
                                         expected_revision=expected_revision,
                                         detail=detail)
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
                             summary: str = "", affected_assets: str = "",
                             test_environment: str = "", reproduction_steps: str = "",
                             verification_result: str = "", risk_assessment: str = "",
                             pocs: list[dict] | None = None,
                             category: str | None = None) -> str:
        # CTF 题目兜底（challenge-asset v38）：未显式给锚点时，把发现归到本线程
        # 的题目节点下（无则按线程标题自动补建）——「一个主控线程只发一道题」。
        # 显式传了 target_asset_id 的完全不受影响（含按附件/子资产精确归属）。
        if not (target_asset_id and str(target_asset_id).strip()):
            fb = self._challenge_fallback_asset()
            if fb:
                target_asset_id = fb
        # relates_to 是顶层入参（LLM 不必懂 evidence 内部结构），并入 evidence 走
        # add_finding 的存在性/同项目校验（悬空/跨项目 ValueError → 回填错误不中断）
        ev = dict(evidence or {})
        if relates_to:
            ev["relates_to"] = relates_to
        # 发现必挂意图 + 意图先行（2026-09-28 三段式收紧，前身为 2026-09-27 必挂
        # 意图）：发现是「侦察→declare_intent→执行→产出」链路的检验产物，既不
        # 允许游离，也不允许「事后补票」——活干完了才 declare 意图紧接着落发现
        # （实测 sess-b9a539e3ebfe：declare 与 finding 仅隔 12 秒，意图沦为过闸
        # 仪式而非事前规划）。两道检查对 Agent 会话统一生效（含对话轮——
        # declare_intent 恒放行不会死锁；人类/系统路径不经此工具，不受限；store
        # 层不设闸，测试/人工 PATCH 零感知）：
        # ① 本会话须有 open 意图；
        # ② 意图声明之后须有执行动作——command 事件（run_cmd，run_cmd 自身不落
        #    tool.call）或非流程类 tool.call（排除 _INTENT_FLOW_TOOLS）。以
        #    events.id（自增行序）为时间线基准，不受秒级时间戳同秒歧义影响。
        # 覆盖作者扩到对话链（2026-10-01 intent-tools-chat）：对话子专家 author
        #   = chat-<threadid>，此前不受门禁 → 对话产出发现不落链路图；对话链同样
        #   要求「先声明意图再登记发现」，与任务管线同纪律。
        # intent_free（激进主控子代理，2026-10-17）：探针没有 declare_intent
        # 工具，若不豁免本检查，探针的任何观察都会被「没有 open 意图」打回
        # （且错误文案会指回它没有的工具，纯空转烧步）。豁免后结论直接落黑板，
        # 归属仍走 target_asset_id / CTF 题目兜底。
        if self.author.startswith(("sess-", "chat-")) and not self.intent_free:
            row = self.bb.conn.execute(
                "SELECT id FROM intents WHERE project_id=? AND status='open'"
                " AND author=? ORDER BY created_at LIMIT 1",
                (self.project_id, self.author)).fetchone()
            if row is None:
                self.last_progress_step = self._step
                return ("[拒绝] 本会话当前没有 open 意图，不允许游离登记发现——"
                        "先 declare_intent(statement=\"对 <对象> 进行 <什么尝试>…\")"
                        " 声明假设，再围绕它执行并登记发现；"
                        "意图最终须 close_intent 收尾（漏洞/发现/死路）")
            declared_ev = self.bb.conn.execute(
                "SELECT id FROM events WHERE session_id=? AND kind='intent.declared'"
                " AND json_extract(payload,'$.intent_id')=? ORDER BY id DESC LIMIT 1",
                (self.author, row["id"])).fetchone()
            marks = ",".join("?" * len(_INTENT_FLOW_TOOLS))
            acted = None
            if declared_ev is not None:
                acted = self.bb.conn.execute(
                    "SELECT 1 FROM events WHERE session_id=? AND id>? AND ("
                    "kind='command' OR (kind='tool.call' AND"
                    " json_extract(payload,'$.name') NOT IN (" + marks + ")))"
                    " LIMIT 1",
                    (self.author, declared_ev["id"], *_INTENT_FLOW_TOOLS)).fetchone()
            if acted is None:
                self.last_progress_step = self._step
                return ("[拒绝] 意图 " + row["id"] + " 声明后还没有任何执行动作，"
                        "不能登记发现——先围绕假设干活（run_cmd 验证/http 请求/"
                        "bb_add_artifact 存证等），有了结果再来 bb_add_finding；"
                        "发现是检验的产物，不是意图的附赠。若这是侦察阶段的直接"
                        "观察，请先做一次核实动作（复核请求/查证）再登记。")
        # 判级依据硬闸（2026-10-16，rating-basis-gate）：**仅当项目勾选了
        # 「评级与价值口径（rating）」**时，漏洞类登记必须带 rating_basis——
        # 空则打回（不落库），agent 补填后重报。与注入同源（rating_profile_active：
        # 勾选且文件存在才生效）；未勾选 rating 的项目不叠加判级口径，不强制。
        # 动机：rating_basis 此前是**纯提示层软约束**（schema 可选 + store 不校验），
        # 工作台对话路径又没接 vuln_gate → 某次生成没照做就静默落空（2026-10-16
        # 实测：上游渠道异常期同一批 4 个兄弟线程 3 个漏填）。人工/API 路径不经本
        # 工具，不受影响（store 层仍不设闸，测试/人工 PATCH 零感知）。
        eff_category = category or ("intel" if severity == "info" else "vuln")
        if eff_category == "vuln" and not str(rating_basis or "").strip() \
                and self._rating_profile_active():
            self.last_progress_step = self._step
            return ("[拒绝] 本项目已勾选「评级与价值口径（rating）」，漏洞登记必须带"
                    "判级依据（rating_basis）——请对照注入的评级口径补「规则名+条款+"
                    "一句话依据」（如 `rating:edu-rating 高危#3 可直接批量盗取用户身份"
                    "权限`）后重报（同一条重发即可，不会重复登记）。若确实够不上漏洞"
                    "档（纯暴露面/信息提示），改 category=intel 登记。")
        # 判级依据**引用**校验（rating-basis-gate v2，2026-10-17）：非空之上再要求
        # 真指向本项目生效口径——旧副本名（edu-rating-rules.md）与报告格式里的判级表
        # 都不算数，一律打回补正。
        if eff_category == "vuln" and str(rating_basis or "").strip() \
                and self._rating_profile_active():
            bad = self._check_rating_basis(rating_basis)
            if bad:
                self.last_progress_step = self._step
                return bad
        # C6 漏洞核对 hook：AI 登记漏洞前自我对照红线/评级规则——
        # 双判：不合规降级 intel（不进漏洞视图）；合规但定级虚高 → 降一档
        # （finding-severity-calibration P0，治夸大）；核对失败降级跳过（不阻断）。
        gate_note = ""
        if category == "vuln" and self.vuln_gate is not None:
            try:
                compliant, new_severity, reason = self.vuln_gate({
                    "title": title, "vuln_class": vuln_class, "severity": severity,
                    "status": status,
                    # has_poc=复现证据判定（收录格式新口径 repro_steps + 旧结构兼容）
                    "has_poc": has_repro_evidence(evidence, poc_artifact_id),
                    "evidence_head": json.dumps(evidence or {}, ensure_ascii=False)[:600],
                    "rating_basis": rating_basis,
                })
                if not compliant:
                    category = "intel"  # 不符合规则 → 不进漏洞，降级有效发现
                    gate_note = f" 漏洞核对：✗ 未通过（{reason}）"
                elif new_severity:
                    # 定级校准降一档：basis 追加降级说明，保证 basis 证成新级别
                    severity = new_severity
                    note = f"自核对降级：{reason}"
                    rating_basis = (f"{rating_basis.rstrip('；;')}；{note}"
                                    if rating_basis.strip() else note)
                    gate_note = f" 漏洞核对：⚠ {reason}"
                else:
                    gate_note = f" 漏洞核对：✓ 通过（{reason}）"
            except Exception as e:  # noqa: BLE001 —— 核对失败降级跳过
                gate_note = f" 漏洞核对跳过：{e}"
        r = self.bb.add_finding(
            self.project_id, vuln_class, title, severity=severity, status=status,
            evidence=ev, target_asset_id=target_asset_id,
            poc_artifact_id=poc_artifact_id,
            confidence=confidence, dedup_key=dedup_key, author=self.author,
            rating_basis=rating_basis, impact=impact, remediation=remediation,
            summary=summary, affected_assets=affected_assets,
            test_environment=test_environment, reproduction_steps=reproduction_steps,
            verification_result=verification_result, risk_assessment=risk_assessment,
            pocs=pocs,
            category=category, track=self.track,
        )
        self.last_progress_step = self._step
        # M5 D1 疑似重复警告（orchestrator-efficiency §0-9）：同链+同类但指纹
        # 不同——只提示不阻塞；警告进工具返回值（单次按需），不落事件无频控。
        # finding-dedup-chain（2026-10-15）：列表带旧条目 dedup_key 与锚点，提示
        # 改为可执行——同一漏洞拿旧 key 重报自动并入，别自造新键。
        warn = ""
        dw = r.get("dedup_warning")
        if dw:
            listed = "；".join(
                f"{d['id']}(dedup_key={d.get('dedup_key') or '?'},「{d['title'][:36]}」)"
                for d in dw)
            warn = (f"\n[疑似重复] 同一端点链上已有同类发现：{listed}——"
                    "**若是同一漏洞**，不要自造新 dedup_key："
                    "用上面任一条的 dedup_key + 它的 target_asset_id 重报，"
                    "系统会自动并入（证据并集、等级就高）而不是新增一行；"
                    "或改用 bb_update_finding(finding_id=该 id) 补证据/改等级。"
                    "确属不同端点的独立问题，坚持新增即可。")
        if r.get("challenge_merged"):
            # CTF 题目级并入（一题一条，challenge-dedup 2026-10-17）：同题目已有登记，
            # 本次结论已并入链内最早那条，不再新增行。后续补证据/改结论用 bb_update_finding。
            warn += (f"\n[已并入题目条目] {r['id']}（CTF 一题一条：该题目已有登记，"
                     "本次结论并入既有条目——证据并集、severity 就高、status 只升）——"
                     f"**同一道题不要再单独 bb_add_finding**；补充证据/修正结论请用 "
                     f"bb_update_finding(finding_id={r['id']})。")
        if r.get("chain_merged"):
            warn += (f"\n[已并入既有发现] {r['id']}（同一端点链上的同类条目，本轮锚点粗细不同）——"
                     "本轮结论已并入该发现（证据并集、severity 就高、status 只升），"
                     "未产生新行。")
        elif r.get("chain_ambiguous"):
            warn += (f"\n[疑似重复·未合并] 同一端点链上有 {r['chain_ambiguous']} 条同类条目、"
                     "无法判定该并入哪条，故保守新建一条——请核对后用 "
                     "bb_update_finding 收敛，勿继续堆叠。")
        if r.get("severity_raise_blocked"):
            warn += ("\n[就高被拒] 本次上报想抬高等级但未带新判级依据"
                     "（rating_basis）或新复现证据，已保留原级别——如需上调，"
                     "请补证据/依据后重报（finding-severity-calibration P2）")
        # 回显生效判级依据（合并就高后可能与本报不同）与去重指纹，供 agent 自检/复用
        return (f"finding={r['id']} merged={r['merged']} severity={r['severity']} "
                f"category={r.get('category') or 'vuln'} "
                f"dedup_key={r.get('dedup_key') or '(空)'} "
                f"rating_basis={r.get('rating_basis') or '(空)'}{gate_note}{warn}")

    def _rating_profiles(self) -> dict | None:
        """项目 config.rule_profiles（评级口径档案）；读取失败 → None。"""
        try:
            cfg = self.bb.get_project(self.project_id).get("config") or {}
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            return (cfg or {}).get("rule_profiles") if isinstance(cfg, dict) else None
        except Exception:  # noqa: BLE001 —— 读取失败按无档案
            return None

    def _rating_profile_active(self) -> bool:
        """本项目是否**勾选并激活**了「评级与价值口径（rating）」——判级依据硬闸的门禁。

        与 `build_rules_preamble` 注入同源（`core.skills.rules.rating_profile_active`）：
        项目 config.rule_profiles.rating 勾选且对应 `安全红线/评级与价值口径/<tag>.md`
        存在才为真。读取/解析失败一律按未激活返回 False——硬闸宁松勿误拒（漏放一次
        依据，好过把没勾 rating 的项目也卡住）。"""
        try:
            from core.skills.rules import rating_profile_active
            return rating_profile_active(self.packs_root, self.track,
                                         self._rating_profiles())
        except Exception:  # noqa: BLE001 —— 判定失败按未激活
            return False

    def _check_rating_basis(self, basis: str | None) -> str:
        """判级依据**引用**校验（rating-basis-gate v2，2026-10-17）→ 空串=通过。

        非空闸只挡漏填，挡不住乱填：实测有子专家写「评级与价值口径/edus-
        rating-rules.md」（文件不存在、引用的是已废止旧副本）照样落库。本层要求
        basis 真的指向本项目生效口径（`rating:<tag>` 或生效 tag 名）。校验自身
        异常一律放行（宁松勿误拒），返回带 `[拒绝]` 前缀的打回文案。"""
        try:
            from core.skills.rules import validate_rating_basis
            ok, why = validate_rating_basis(
                self.packs_root, self.track, self._rating_profiles(), basis)
        except Exception:  # noqa: BLE001 —— 校验失败放行
            return ""
        return "" if ok else f"[拒绝] {why}"

    # ---------- 意图（渗透链路图 v3，2026-09-24） ----------

    def _tool_declare_intent(self, statement: str,
                              target_asset_id: str | None = None,
                              dimension: str | None = None,
                              basis_refs: list[str] | None = None) -> str:
        # 资产锚点门禁（2026-10-01，仅 Agent 会话）：意图必须有资产锚点
        # （target_asset_id 或 basis_refs 含 asset:<id>），否则成"游离意图"——
        # 既落不到链路图子目标下，dead_end 收尾也无法给任何资产背书 tested_clean。
        anchor_refs = [b for b in (basis_refs or [])
                       if isinstance(b, str)
                       and (b.startswith("asset:") or b.startswith("asset-"))]
        if not (target_asset_id and str(target_asset_id).strip()) and not anchor_refs:
            # CTF 题目兜底（challenge-asset v38）：线程题目节点即缺省锚点——
            # 一个主控线程只发一道题，意图与发现都归到这道题下。
            fallback = self._challenge_fallback_asset()
            if fallback:
                target_asset_id = fallback
            else:
                return ("[拒绝] declare_intent 必须绑定资产锚点：填 target_asset_id"
                        "（针对的资产 id），或在 basis_refs 里至少给一条"
                        " asset:<资产id>。游离意图落不到链路图子目标下，"
                        "其 dead_end 收尾也无法为资产背书 tested_clean——"
                        "先 bb_query/bb_asset 确认你要测的资产 id，再声明意图。"
                        + _intent_scope_hint(self.track))
        # 测试面归属（v34）：填了必须对本轨合法；本轨无面清单时一律拒（防拼错
        # 静默丢面归属）。未填放行（可选，向后兼容）。
        dim = dimension.strip() if isinstance(dimension, str) else ""
        if dim:
            dims = load_track_dimensions(self.packs_root, self.track)
            valid = {d["id"] for d in dims}
            if dim not in valid:
                return (f"[拒绝] declare_intent 的 dimension={dim!r} 不是本轨合法"
                        f"测试面；可用：{sorted(valid) or '（本轨无面清单，勿填）'}")
        r = _declare_intent(self.bb, self.project_id, statement,
                            target_asset_id=target_asset_id,
                            dimension=dim,
                            basis_refs=basis_refs, author=self.author)
        self.last_progress_step = self._step
        teach = ""
        if r.get("ref_corrections"):
            teach = ("；已自动归一引用：" + "；".join(r["ref_corrections"])
                     + "（<id> 须是完整 id——自带 asset-/find- 前缀，"
                       "后续请直接写完整形态）")
        if r.get("merged"):
            return (f"[复用] intent={r['id']} status=open"
                    "（同陈述意图已存在，在它下面继续执行并收尾）" + teach
                    + _intent_scope_hint(self.track))
        return (f"intent={r['id']} status=open basis_refs={len(r.get('basis_refs') or [])}"
                "——围绕它执行，最终必须 close_intent 收尾" + teach
                + _intent_scope_hint(self.track))

    def _tool_close_intent(self, intent_id: str, outcome: str,
                           finding_ids: list[str] | None = None,
                           evidence_refs: list[str] | None = None,
                           dead_reason: str = "") -> str:
        r = _close_intent(self.bb, self.project_id, intent_id, outcome,
                          finding_ids=finding_ids, evidence_refs=evidence_refs,
                          dead_reason=dead_reason, author=self.author,
                          track=self.track)
        self.last_progress_step = self._step
        refs = len(r.get("outcome_refs") or [])
        teach = ""
        if r.get("ref_corrections"):
            teach = ("；已自动归一引用：" + "；".join(r["ref_corrections"])
                     + "（<id> 须是完整 id——后续请直接写完整形态）")
        return (f"intent={intent_id} status=closed outcome={outcome} "
                f"findings={refs}" + teach)

    def _tool_reopen_intent(self, intent_id: str, note: str = "") -> str:
        r = _reopen_intent(self.bb, self.project_id, intent_id,
                           author=self.author, note=note)
        self.last_progress_step = self._step
        return f"intent={intent_id} status={r['status']}（已重开，需重新收尾）"

    def _tool_bb_delete_intent(self, intent_id: str, reason: str = "") -> str:
        try:
            r = _delete_intent(self.bb, self.project_id, intent_id,
                               author=self.author, reason=reason)
        except LookupError as e:
            return f"[错误] {e}"
        except ValueError as e:
            return f"[拒绝] {e}"
        self.last_progress_step = self._step
        return (f"intent={r['id']} 已物理删除（{r['statement'][:80]}）")

    def _tool_bb_update_finding(self, finding_id: str, severity: str | None = None,
                                status: str | None = None, title: str | None = None,
                                vuln_class: str | None = None,
                                evidence: dict | None = None,
                                category: str | None = None,
                                rating_basis: str | None = None,
                                impact: str | None = None,
                                remediation: str | None = None,
                                summary: str | None = None,
                                affected_assets: str | None = None,
                                test_environment: str | None = None,
                                reproduction_steps: str | None = None,
                                verification_result: str | None = None,
                                risk_assessment: str | None = None,
                                pocs: list[dict] | None = None,
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
        for key, value in {
            "summary": summary, "affected_assets": affected_assets,
            "test_environment": test_environment, "reproduction_steps": reproduction_steps,
            "verification_result": verification_result, "risk_assessment": risk_assessment,
            "pocs": pocs,
        }.items():
            if value is not None:
                kwargs[key] = value
        if not kwargs:
            return "[拒绝] 未提供任何要修改的字段"
        # 判级依据引用校验（v2，2026-10-17）：改判级依据时与登记同口径——写旧副本名
        # 或报告格式里的判级表一律打回（实测降级路径曾借此把依据写成不存在的文件）。
        if rating_basis is not None and str(rating_basis).strip():
            eff_cat = category or (old.get("category") or "vuln")
            if eff_cat == "vuln" and self._rating_profile_active():
                bad = self._check_rating_basis(rating_basis)
                if bad:
                    return bad
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
                f"trimmed_relates_to={r.get('trimmed_relates_to', 0)}")

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

    def _ctf_challenge_scope(self) -> dict | None:
        """CTF 跨题隔离面（challenge-isolation，2026-10-19）：本题 vs 其它题目。

        薄封装 `core.chat.store.challenge_isolation_scope`（与 chat runtime 的
        注入侧同源，口径只有一份）。返回 ``{"other_assets", "other_roots",
         "other_sessions", "self_challenge"}``；非 CTF 轨 / 任务链会话
        （thread_id=''）/ 项目只有本题 → ``None``（=不过滤，行为与改动前一致）。

        **动机**（用户实测）：多题共跑时 ``bb_query what=findings`` 回的是
        **全项目**发现——一道全新题刚上手，AI 顺手一查就翻到别的题的结论，
        上下文被污染、推理被带偏。详见 store 侧文档。
        """
        if self.track != "ctf" or not self.thread_id:
            return None
        from core.chat.store import challenge_isolation_scope
        return challenge_isolation_scope(self.bb, self.project_id, self.thread_id)

    def _tool_bb_query(self, what: str, target_asset_id: str | None = None,
                       binary_sha256: str | None = None, address: int | str | None = None,
                       risk_tag: str | None = None, type: str | None = None,
                       status: str | None = None, blueprint_id: str | None = None,
                       min_severity: str | None = None, verified_only: bool = False,
                       category: str | None = None, tag: str | None = None,
                       kinds: list[str] | str | None = None,
                       session_id: str | None = None,
                       asset: str | None = None,
                       limit: int | None = None,
                       verbose: bool = False,
                       offset: int | None = None,
                       sha256: str | None = None,
                       package_id: str | None = None,
                       tree_id: str | None = None,
                       block_id: str | None = None,
                       cross_challenge: bool = False) -> str:
        # 参数名容错（2026-09-30）：模型高频把 binary_sha256 简写成 sha256——
        # 别名归一而非裸 TypeError（截图案例：what=files+sha256 连错两处）
        binary_sha256 = binary_sha256 or sha256
        # what 面按轨裁剪（track-query-split，2026-10-13）：异家族面在 schema 里
        # 已不可见（AgentSession/chat runtime 下发前裁）；模型仍硬传（旧 prompt
        # 缓存/幻觉）时显式拒绝——宁可报错也不静默执行或静默空结果。
        _allowed_what = bb_query_what_for(self.track)
        if what not in _allowed_what:
            if what in _BB_QUERY_WHAT_FULL:
                return (f"[错误] what={what} 本轨不可用（{self.track or '未配轨'} 轨允许: "
                        f"{', '.join(_allowed_what)}）——按本轨面查询；列已上传样本用 "
                        f"what=assets type=binary，返回行的 value 即样本 sha256")
            return (f"[错误] 未知查询: {what}（允许: {', '.join(_allowed_what)}；"
                    f"列已上传样本用 what=assets type=binary，返回行的 value 即样本 sha256）")
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
        # limit 钳 1-200（与 kb_search 同口径：非法值钳制不报错）
        if limit is not None:
            try:
                limit = max(1, min(200, int(limit)))
            except (TypeError, ValueError):
                limit = None
        # offset 钳 ≥0（assets 分页起点，2026-10-09）：非法值当未传
        if offset is not None:
            try:
                offset = max(0, int(offset))
            except (TypeError, ValueError):
                offset = None
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
                              "dedup_key": f.get("dedup_key", ""),
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
            # CTF 跨题隔离（challenge-isolation，2026-10-19）：默认只回本题
            # （+未归属任何题目）的发现，别题的结论整批挡在上下文之外——
            # 用户实测「全新题目一上手就 bb_query findings，翻到其它题的
            # finding，上下文被污染」。显式 cross_challenge=true 才看全项目。
            # **显式 target_asset_id 不裁**（与 events 的显式 session_id 同口径）：
            # 模型点名某个资产 = 明确意图，不该被默认策略静默吞掉。
            if not cross_challenge and not (target_asset_id or "").strip():
                scope = self._ctf_challenge_scope()
                if scope is not None:
                    excluded = scope["other_assets"]
                    rows = [f for f in rows
                            if str(f.get("target_asset_id") or "") not in excluded]
            if limit is not None:
                rows = rows[:limit]
            # dedup_key/target_asset_id 一并回传（finding-dedup-chain，2026-10-15）：
            # 升级/验证既有漏洞时需要拿旧指纹按原 key 重报自动并入，而不是自造新键
            # 裂出新行。celebration-replay：之前查不到 key，只能瞎造。
            return json.dumps(
                [{"id": f["id"], "vuln_class": f["vuln_class"], "title": f["title"],
                  "severity": f["severity"], "status": f["status"],
                  "confidence": f["confidence"],
                  "dedup_key": f["dedup_key"],
                  "target_asset_id": f["target_asset_id"]}
                 for f in rows], ensure_ascii=False)
        if what == "assets":
            rows = self.bb.list_assets(
                self.project_id, type_=type, status=status, tag=tag)
            # CTF 跨题隔离（challenge-isolation，2026-10-19）：别题的**子资产**
            # （附件/入口/样本）不回传——一道新题上手先 bb_query assets，翻到
            # 别的题的 pcap/镜像清单只会误导路由。别题的**题目行**保留：AI
            # 需要知道项目里还有哪些题（否则会误以为只有自己这道，甚至重复建题）。
            # 显式传 asset（点名查某个子树）时不裁——那是明确意图。
            if not cross_challenge and not (asset or "").strip():
                scope = self._ctf_challenge_scope()
                if scope is not None:
                    hide = scope["other_assets"] - scope["other_roots"]
                    rows = [a for a in rows if a["id"] not in hide]
            # asset 过滤（2026-10-01）：收 asset（id 或精确 value，大小写不敏感）
            # 时只回该资产**子树内**的资产（与 what=site 的资产口径一致，但**不**
            # 附带 findings/intents，且保留 meta）——修「按域名查却拿到全项目全量」。
            # 找不到 → [错误] 引导（勿静默返回全量）。
            if asset is not None and str(asset).strip():
                key = str(asset).strip()
                all_assets = self.bb.list_assets(self.project_id)
                root = next((a for a in all_assets if a["id"] == key), None)
                if root is None:  # id 未命中 → 精确 value（host/domain 优先）
                    vals = [a for a in all_assets
                            if (a.get("value") or "").strip().lower() == key.lower()]
                    vals.sort(key=lambda a: 0 if a.get("type") in ("host", "domain") else 1)
                    root = vals[0] if vals else None
                if root is None:
                    sample = sorted({a["value"] for a in all_assets
                                     if a.get("type") in ("host", "domain")})[:8]
                    return (f"[错误] 找不到资产: {asset}"
                            f"（可先 what=assets 列清单，host/domain 如: "
                            f"{', '.join(sample)}…）")
                subtree = _subtree_ids(all_assets, root["id"])
                rows = [a for a in rows if a["id"] in subtree]
            # 默认 limit（2026-10-01 防瀑）：不传时默认 200（此前=全量，FOFA 导入
            # 几千条资产时 meta 全文可达数 MB）——要全量显式传大 limit。
            # 分页（2026-10-09）：offset 从 created_at 固定序里取窗口——枚举一大批
            # 资产（清洗泛解析族）时逐页取 id，再交 bb_delete_assets 批量删，不必
            # 靠「边删边看前 N 条」反推分页位置（那正是推理死循环的来源）。
            cap = limit if limit is not None else _BB_ASSETS_DEFAULT_LIMIT
            total = len(rows)
            start = offset or 0
            rows = rows[start:start + cap]
            return json.dumps(
                {"counts": {"total": total, "offset": start,
                            "returned": len(rows),
                            "has_more": start + len(rows) < total},
                 "hint": ("资产的域名/站点现状用 what=site（一次返回子树+发现+意图）；"
                          "列清单/按类型筛用 what=assets；枚举大批量用 offset+limit "
                          "逐页取（has_more=false 即到底）；meta 默认精简，"
                          "需要全文传 verbose=true；返回被截断时传更大 limit。"),
                 "assets": [_asset_row(a, verbose=verbose) for a in rows]},
                ensure_ascii=False)
        if what == "events":
            rows = self.bb.recent_events(
                self.project_id, tail=limit if limit is not None else 50,
                kinds=kinds, session_id=session_id)
            # CTF 跨题隔离（challenge-isolation，2026-10-19）：别题线程落的事件
            # （command/tool.call/finding.new…）默认不回传——查事件时看到别的题
            # 跑过什么命令，等于把别人的解题过程倒进本题上下文。显式传
            # session_id（自己指定会话）或 cross_challenge=true 时不裁。
            if not cross_challenge and not (session_id or "").strip():
                scope = self._ctf_challenge_scope()
                if scope is not None:
                    hidden = scope["other_sessions"]
                    if hidden:
                        rows = [e for e in rows
                                if str(e.get("session_id") or "") not in hidden]
            # id/session_id/created_at 增量带出（M2）：查事件即可知归属，不必反查落盘
            return json.dumps(
                [{"id": e["id"], "kind": e["kind"], "author": e["author"],
                  "session_id": e.get("session_id"), "created_at": e["created_at"],
                  "payload": e["payload"]}
                 for e in rows], ensure_ascii=False)
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
        if what == "package":
            return self._bb_query_package(package_id)
        if what == "feature_tree":
            # 功能树（v36 分析阶段产物）：列树/单棵树。add/update 节点前的查重面
            # ——树按 binary_sha256 筛（空串=项目级树），节点清单同名/同职责比对。
            if tree_id:
                row = self.bb.get_feature_tree(self.project_id, tree_id)
                if row is None:
                    return f"[错误] 功能树不存在: {tree_id}"
                return json.dumps(row, ensure_ascii=False)
            rows = self.bb.list_feature_trees(
                self.project_id, binary_sha256=binary_sha256)
            if limit is not None:
                rows = rows[:limit]
            return json.dumps(
                [{"id": t["id"], "name": t.get("name", ""),
                  "binary_sha256": t.get("binary_sha256", ""),
                  "nodes": [{"id": n["id"], "name": n["name"],
                             "parent_id": n.get("parent_id"),
                             "status": n.get("status", "pending"),
                             "desc": (n.get("desc") or "")[:120],
                             "func_addresses": n.get("func_addresses", [])}
                            for n in t.get("nodes", [])]}
                 for t in rows], ensure_ascii=False)
        if what == "logic_block":
            # 业务逻辑块（v26）：列块/单块。建块前的查重面——块名同项目同样本唯一，
            # 重名 create 会被拒收；查到同名块应走 bb_logic_block_update 增补。
            if block_id:
                row = self.bb.get_logic_block(self.project_id, block_id)
                if row is None:
                    return f"[错误] 业务逻辑块不存在: {block_id}"
                return json.dumps(row, ensure_ascii=False)
            rows = self.bb.list_logic_blocks(
                self.project_id, binary_sha256=binary_sha256)
            if limit is not None:
                rows = rows[:limit]
            return json.dumps(
                [{"id": b["id"], "name": b["name"],
                  "binary_sha256": b.get("binary_sha256", ""),
                  "feature_node_id": b.get("feature_node_id", ""),
                  "func_count": b.get("func_count", 0),
                  "desc": (b.get("description") or "")[:120]}
                 for b in rows], ensure_ascii=False)
        # 顶部入口闸已保证 what ∈ 本轨允许面；走到这里=新增了面忘配轨表——
        # 显式报错兜底，绝不静默返回 None。
        return (f"[错误] 查询面未实现: {what}（本轨允许: "
                f"{', '.join(bb_query_what_for(self.track))}）")

    def _bb_query_package(self, package_id: str | None = None) -> str:
        """分析包全貌（2026-10-12）：列当前（或指定）分析包的条目/依赖边/内容树根。

        供 AI 定位同包文件——分析包登记样本（`bb_query what=assets type=binary` 行里
        `meta.package_id`）的兄弟文件（如 exe 依赖的 DLL）都在同一内容树里。返回的
        `entries[].path` 是**包内相对路径**，`tree_ref` 是项目内容树绝对路径，AI 用
        `read_file <tree_ref>/<path>` 直接读同包文件；`dependencies` 给导入名→同包路径
        的边。读 `<工作区>/sample_packages/` 元数据（与 SamplePackageStore 同源，只读）。
        缺工作区/无分析包/清单损坏 → 结构化提示串（不抛）。
        """
        if not self.artifacts_dir:
            return "[错误] 本会话未装配工作区，无法读取分析包"
        meta_root = Path(self.artifacts_dir).parent / "sample_packages"
        pid = str(package_id or "").strip()
        version_id = ""
        if not pid:
            try:
                pointer = json.loads(
                    (meta_root / "current.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return ("[错误] 当前项目没有分析包（先到「样本分析」上传文件/文件夹；"
                        "或用 what=assets type=binary 看已登记样本的 meta.package_id）")
            pid = str(pointer.get("package_id") or "")
            version_id = str(pointer.get("version_id") or "")
        if not re.fullmatch(r"pkg-[0-9a-f]{16}", pid):
            return f"[错误] 非法 package_id: {pid}"
        versions = meta_root / pid / "versions"
        paths = ([versions / version_id / "manifest.json"] if version_id
                 else sorted(versions.glob("*/manifest.json"), reverse=True))
        manifest = None
        for mp in paths:
            if mp.is_file():
                try:
                    manifest = json.loads(mp.read_text(encoding="utf-8"))
                    break
                except (OSError, ValueError):
                    continue
        if not isinstance(manifest, dict):
            return f"[错误] 分析包不存在或无清单: {pid}"
        entries = manifest.get("entries") or []
        cap = 200
        return json.dumps({
            "package_id": manifest.get("package_id") or pid,
            "version_id": manifest.get("version_id") or version_id,
            "tree_ref": manifest.get("tree_ref"),
            "file_count": manifest.get("file_count"),
            "total_bytes": manifest.get("total_bytes"),
            "origin": manifest.get("origin"),
            "dependencies": manifest.get("dependencies") or [],
            "entries": entries[:cap],
            "entries_truncated": len(entries) > cap,
        }, ensure_ascii=False)

    def _tool_kb_open(self, module: str) -> str:
        """打开知识库模块并直接返回正文（DESIGN.md §4/§4.5）。

        module 全局形态 `<域>/<快照>/<路径>`（首段=启用域时锁定该域），
        域内相对形态向后兼容；writing.resolve_kb 负责存在性消歧与防穿越；
        不存在则按启用域列出可选清单（防幻觉猜名）。
        额外接受 Obsidian wikilink 口径（`[[web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）|手册]]`、
        `[[web/…/手册]]`）与反引号/引号包裹——refs.normalize_module 统一归一。

        成功时返回文件正文，不再返回本地路径或要求另行调用 read_file；文件路径
        仅写入 kb.open 审计事件。"""
        if not self.packs_root:
            return "[错误] 本会话未配置知识库（packs_root 缺失）"
        rel = normalize_module(module)
        if not rel:
            return "[错误] module 不能为空"
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
            try:
                body = t.path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return f"[错误] 知识库模块读取失败: {e}"
            self.bb.append_event(
                self.project_id, "kb.open",
                {"source": t.source.id, "module": rel, "path": str(t.path),
                 "chars": len(body)},
                session_id=self.session_id, author=self.author)
            self.last_progress_step = self._step
            return body
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
                 "再用 kb_open 直接读取正文）:"]
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

    def _tool_skill_open(self, name: str, path: str | None = None) -> str:
        """打开自包含技能正文或其同目录资源。

        资源路径严格限制在 cc 约定子目录，不能穿越到其他技能、packs/kb
        或项目工作区。旧技能仍可读取 SKILL.md，迁移期间不破坏存量任务。
        `path` 与 kb_open 同口径收口：反引号/引号包裹与 Obsidian wikilink
        （`[[references/x.md|别名]]`）都先过 refs.normalize_module 再校验。"""
        if not self.packs_root:
            return "[错误] 本会话未配置 packs_root（skill_open 不可用）"
        if self._skill_registry is None:
            self._skill_registry = SkillRegistry(self.packs_root)
            self._skill_registry.load()
        assert self._skill_registry is not None
        sk = self._skill_registry.get((name or "").strip())
        whitelist = self.role_skills
        if sk is None or not sk.enabled or (whitelist is not None and sk.name not in whitelist):
            names = sorted(s.name for s in self._skill_registry.all()
                           if s.enabled and (whitelist is None or s.name in whitelist))
            return ("[防幻觉] 技能不存在或不可用: " + (name or "")
                    + "\n可用技能: " + ("、".join(names) if names else "（无）"))
        try:
            if path:
                rel = normalize_module(path)
                parts = Path(rel).parts
                if not rel or rel.startswith("/") or ".." in parts \
                        or not parts or parts[0] not in {"references", "scripts", "examples", "assets"}:
                    return "[错误] 技能资源路径非法：只能读取 references/scripts/examples/assets 下的文件"
                target = (sk.root / Path(rel)).resolve()
                root = sk.root.resolve()
                allowed = {p.resolve() for p in sk.resources}
                if root not in target.parents or target not in allowed:
                    return f"[错误] 技能资源不存在或不可读: {rel}"
                body = target.read_text(encoding="utf-8", errors="replace")
                opened = rel
            else:
                body = sk.body()
                opened = "SKILL.md"
        except OSError as e:
            return f"[错误] 技能文件读取失败: {e}"
        self.bb.append_event(
            self.project_id, "skill.open",
            {"name": sk.name, "path": opened, "chars": len(body)},
            session_id=self.session_id, author=self.author)
        self.last_progress_step = self._step
        if path:
            return f"技能 {sk.name} 资源 {opened}:\n\n{body}"
        resources = [p.relative_to(sk.root).as_posix() for p in sk.resources]
        manifest = "\n".join(f"- {item}" for item in resources) or "（无附属资源）"
        return (f"技能 {sk.name} SKILL.md:\n\n{body}\n\n"
                f"同目录可用资源（用 skill_open(path=...) 按需读取）：\n{manifest}")

    # 每会话提案上限（DESIGN §4：防凑数/提案洪泛）
    PROPOSE_LIMIT_PER_SESSION = 3

    def _tool_propose_pack_edit(self, kind: str, mode: str, target: dict,
                                summary: str, reason: str,
                                content: str | None = None) -> str:
        """经验沉淀提案通道（C4）：只校验落 pending，绝不直接改文件；应用权在人类。

        **项目级开关门禁（2026-10-15，Nan 报 bug：关闭「知识库迭代」时主控仍在
        提提案）**：本工具是 Agent 主动提案的唯一入口，与轮末自动提案
        （kbiteration.run_iteration）同口径读 ``config.kb_iteration.enabled``——
        关闭即拒。此前只有轮末路径有门禁、工具路径裸奔，是 bug 根因。"""
        if not self.packs_root:
            return "[错误] 本会话未配置 packs_root，无法提提案"
        try:
            from core.skills import kbiteration
            proj = self.bb.get_project(self.project_id)
            if not kbiteration.is_enabled((proj or {}).get("config")):
                return ("[拒绝] 本项目未开启「知识库迭代」（变更提案页右上角开关）："
                        "关闭时 Agent 不生成任何提案，不必重试；确需沉淀请让人类开启后再提")
        except Exception:  # noqa: BLE001 —— 读开关失败按关闭处理，绝不放行
            return "[拒绝] 读取「知识库迭代」开关失败，按已关闭处理，未落提案"
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
                                  modules: list[dict] | None = None,
                                  feature_tree_id: str = "") -> str:
        try:
            r = self.bb.create_blueprint(
                self.project_id, name, goal=goal, binary_sha256=binary_sha256,
                modules=modules, feature_tree_id=feature_tree_id, author=self.author)
        except ValueError as e:
            return f"[错误] {e}"
        self.last_progress_step = self._step
        names = "、".join(m["name"] for m in r["modules"])
        mod_note = f"（{names}）" if names else ""
        ft_note = f" feature_tree={r.get('feature_tree_id', '')}" if r.get("feature_tree_id") else ""
        return (f"blueprint={r['id']} status=draft modules={len(r['modules'])}{mod_note}"
                f"{ft_note}"
                "——继续 bb_blueprint_update 补 spec/notes，整体 status 由人类流转")

    def _tool_bb_blueprint_update(self, blueprint_id: str,
                                  module_name: str | None = None,
                                  spec: str | None = None,
                                  notes: str | None = None,
                                  desc: str | None = None,
                                  func_addresses: list[str] | None = None,
                                  module_status: str | None = None,
                                  modules_set: list[dict] | None = None,
                                  feature_tree_id: str | None = None,
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
                    modules_set=modules_set if modules_set is not None else UNSET,
                    feature_tree_id=feature_tree_id if feature_tree_id is not None else UNSET)
        except LookupError as e:
            return f"[错误] {e}"
        except ValueError as e:
            return f"[错误] {e}"
        if r is None:
            return f"[错误] 蓝图不存在: {blueprint_id}"
        self.last_progress_step = self._step
        return f"blueprint={r['id']} updated（modules={len(r['modules'])}）"

    def _tool_bb_feature_tree_create(self, binary_sha256: str = "",
                                     name: str = "") -> str:
        """建/取该样本的功能树（每项目每样本唯一）。分析阶段先建树再下钻，
        避免重复分析；后续蓝图可引用 feature_tree_id 作为施工图纸。"""
        try:
            r = self.bb.create_feature_tree(
                self.project_id, binary_sha256=binary_sha256,
                name=name, author=self.author)
        except ValueError as e:
            return f"[错误] {e}"
        self.last_progress_step = self._step
        return (f"feature_tree={r['id']} nodes={len(r.get('nodes', []))}"
                "——用 bb_feature_node add 增功能节点（Agent 不可改 status，验收是人类职责）")

    def _tool_bb_feature_node(self, tree_id: str, op: str,
                              name: str = "", node_id: str = "",
                              parent_id: str | None = None,
                              desc: str = "",
                              func_addresses: list[str] | None = None) -> str:
        """功能树节点操作（分析阶段产物）。op=add/update/delete。
        - add: 必传 name，parent_id=None 为根；返回新节点 id
        - update: 必传 node_id，可改 name/desc/parent_id/func_addresses
          （Agent **不可**改 status——通过验收是人类职责）
        - delete: 必传 node_id，级联删除子节点"""
        bb = self.bb
        try:
            if op == "add":
                r = bb.add_feature_node(
                    self.project_id, tree_id, name, parent_id=parent_id,
                    desc=desc, func_addresses=func_addresses or [],
                    author=self.author)
                nid = next(n["id"] for n in r["nodes"]
                           if n["name"] == name and n.get("parent_id") == parent_id)
                return f"node={nid} added to feature_tree={tree_id}"
            if op == "update":
                if not node_id:
                    return "[错误] update 需 node_id"
                kw: dict[str, Any] = {}
                if name:
                    kw["name"] = name
                if desc != "":
                    kw["desc"] = desc
                if parent_id is not None or parent_id == "":
                    kw["parent_id"] = parent_id or None
                if func_addresses is not None:
                    kw["func_addresses"] = func_addresses
                if not kw:
                    return "[错误] update 至少需一个可改字段（name/desc/parent_id/func_addresses）"
                bb.update_feature_node(
                    self.project_id, tree_id, node_id,
                    author=self.author, **kw)
                return f"node={node_id} updated"
            if op == "delete":
                if not node_id:
                    return "[错误] delete 需 node_id"
                bb.delete_feature_node(
                    self.project_id, tree_id, node_id, author=self.author)
                return f"node={node_id} deleted（含子节点）"
            return f"[错误] 未知 op: {op}（允许 add/update/delete）"
        except LookupError as e:
            return f"[错误] {e}"
        except ValueError as e:
            return f"[错误] {e}"

    def _tool_bb_logic_block_create(self, name: str, binary_sha256: str,
                                    description: str = "",
                                    funcs: list[dict] | None = None,
                                    feature_node_id: str = "") -> str:
        try:
            r = self.bb.create_logic_block(
                self.project_id, name, description=description,
                binary_sha256=binary_sha256, feature_node_id=feature_node_id,
                author=self.author)
            fails: list[str] = []
            for f in funcs or []:
                try:
                    self.bb.add_logic_block_func(
                        self.project_id, r["id"], f.get("address", ""),
                        str(f.get("role", "")), author=self.author)
                except ValueError as e:  # 未入库/重复挂接：不中断，逐条报告
                    fails.append(f"{f.get('address')}: {e}")
        except ValueError as e:
            return f"[错误] {e}"
        self.last_progress_step = self._step
        head = f"logic_block={r['id']} funcs={len(r['funcs'])}"
        if fails:
            head += "；部分挂接失败——" + "；".join(fails)
        return head + "——继续 bb_logic_block_update 补描述/增删挂接，人类在「业务逻辑」页签可见"

    def _tool_bb_logic_block_update(self, block_id: str,
                                    description: str | None = None,
                                    add_funcs: list[dict] | None = None,
                                    func_roles: list[dict] | None = None,
                                    remove_addresses: list[str] | None = None,
                                    feature_node_id: str | None = None) -> str:
        try:
            r = self.bb.get_logic_block(self.project_id, block_id)
            if r is None:
                return f"[错误] 业务块不存在: {block_id}"
            changed: list[str] = []
            if description is not None:
                self.bb.update_logic_block(
                    self.project_id, block_id, description=description,
                    author=self.author)
                changed.append("description")
            if feature_node_id is not None:
                self.bb.update_logic_block(
                    self.project_id, block_id, feature_node_id=feature_node_id,
                    author=self.author)
                changed.append(f"feature_node={feature_node_id or '(none)'}")
            for f in add_funcs or []:
                try:
                    self.bb.add_logic_block_func(
                        self.project_id, block_id, f.get("address", ""),
                        str(f.get("role", "")), author=self.author)
                    changed.append(f"+{f.get('address')}")
                except ValueError as e:
                    changed.append(f"[拒绝] {f.get('address')}: {e}")
            for f in func_roles or []:
                row = self.bb.update_logic_block_func(
                    self.project_id, block_id, f.get("address", ""),
                    str(f.get("role", "")), author=self.author)
                changed.append(f"role@{f.get('address')}" if row is not None
                               else f"[未挂接] {f.get('address')}")
            for a in remove_addresses or []:
                row = self.bb.remove_logic_block_func(
                    self.project_id, block_id, a, author=self.author)
                changed.append(f"-{a}" if row is not None else f"[未挂接] {a}")
        except ValueError as e:
            return f"[错误] {e}"
        self.last_progress_step = self._step
        return f"logic_block={block_id} updated（{'; '.join(changed)}）"

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

    def _tool_finish(self, summary: str) -> str:
        # 意图纪律①（宁严勿松）：有未收尾意图首次 finish 拦截，列清单要求收尾；
        # 二次 finish 放行（客观无法收尾时须在 summary 写明）。
        # 2026-09-27 agent-loop 修复：只查**本会话**的未收尾意图——此前按全项目
        # 扫，A 会话遗留的 open 意图会堵住 B 会话的 finish（B 无法合法关闭 A 的
        # 意图，只能被逼二次 finish 绕过，报错文案还误导 B 去关别人的意图）。
        if not self._finish_open_intents_ack:
            try:
                from core.blackboard.intents import list_intents as _list_open
                open_intents = [it for it in _list_open(
                    self.bb, self.project_id, status="open")
                    if it.get("author") == self.author]
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
        """服务层成功=JSON 数据/伪码文本；引导文本以 [反编译器不可用]/[错误]/[未分析] 开头。"""
        return not result.startswith(("[反编译器不可用]", "[错误]", "[未分析]"))

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

    def _tool_disasm(self, binary: str, name: str | None = None,
                     address: int | str | None = None) -> str:
        if self.decompiler is None:
            return self._DECOMPILER_UNWIRED
        if address is not None:
            try:
                address = _coerce_addr(address)
            except ValueError as e:
                return f"[错误] {e}"
        result = self.decompiler.disasm(binary, address=address, name=name)
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

    def _tool_browser_replay(self, raw: str | None = None, capture_id: int | None = None,
                             proxy: str | None = None, force_https: bool = False,
                             follow_redirects: bool = True, insecure: bool = False,
                             gm_tls: bool = False, timeout_s: float = 15.0,
                             body_max_bytes: int | None = None) -> str:
        """重放一条请求（F6 重放；Agent 侧入口，2026-10-07 放开红线）。

        与人类重放台共用 `ReplayClient`；结果入 http_history（source=replay）。
        """
        if self.browser is None:
            return self._NO_BROWSER_TOOL
        if not raw and capture_id is None:
            return "[拒绝] 需给 raw（原始报文）或 capture_id（抓包记录）之一"
        from core.browser.pool import browser_available
        if not browser_available():
            return self._NO_BROWSER_TOOL
        from core.browser.replay import ReplayClient, ReplayOptions
        # tools_root 仅国密 sidecar 解析用（缺二进制=国密不可用，绝不降级）
        client = ReplayClient(self.bb, config=self.browser.config,
                              tools_root="tools")
        opts = ReplayOptions(force_https=force_https, follow_redirects=follow_redirects,
                             proxy=proxy or None, insecure=insecure, gm_tls=gm_tls,
                             timeout_s=timeout_s, body_max_bytes=body_max_bytes)
        try:
            row = client.replay(self.project_id, capture_id=capture_id, raw=raw,
                                session_id=self.session_id, author=self.session_id,
                                opts=opts, stop_event=self.abort_event)
        except Exception as e:  # noqa: BLE001 —— 失败也入库，转文本不炸循环
            return f"[错误] 重放失败: {e}"
        self.last_progress_step = self._step
        body = row.get("resp_body") or ""
        resp_headers = row.get("resp_headers") or {}
        headers_text = "\n".join(f"{k}: {v}" for k, v in resp_headers.items())
        if not headers_text:
            headers_text = "(无响应头)"
        trunc_note = " [正文已按 body_max_bytes 截断，数据库中仅保留前缀]" if row.get("body_truncated") else ""
        binary_note = " [二进制内容已编码保存]" if row.get("is_binary") else ""
        return (f"重放完成: {row.get('method')} {row.get('url')} → "
                f"状态={row.get('status')} 耗时={row.get('duration_ms')}ms "
                f"mime={row.get('resp_mime') or '-'} 行id={row.get('id')}"
                f"{' [经代理 ' + proxy + ']' if proxy else ''}\n"
                f"响应头（完整）:\n{headers_text}\n"
                f"响应体（已存储全文）{trunc_note}{binary_note}:\n{body}")

    def _tool_browser_intruder(self, template: dict, payloads: list,
                               concurrency: int = 5, rate_per_sec: float = 10.0,
                               max_requests: int | None = None,
                               proxy: str | None = None) -> str:
        """HTTP 爆破（F6 Intruder；Agent 侧入口，2026-10-07 放开红线）。"""
        if self.browser is None:
            return self._NO_BROWSER_TOOL
        from core.browser.pool import browser_available
        if not browser_available():
            return self._NO_BROWSER_TOOL
        from core.browser.replay import Intruder
        import uuid
        batch_id = f"in-{uuid.uuid4().hex[:12]}"
        intruder = Intruder(self.bb, config=self.browser.config)
        try:
            r = intruder.run(self.project_id, template, payloads,
                             batch_id=batch_id, concurrency=concurrency,
                             rate_per_sec=rate_per_sec, max_requests=max_requests,
                             proxy=proxy or None, stop_event=self.abort_event,
                             session_id=self.session_id, author=self.session_id)
        except Exception as e:  # noqa: BLE001
            return f"[错误] 爆破失败: {e}"
        self.last_progress_step = self._step
        return (f"爆破完成: 批次={r['batch_id']} 总={r['total']} 完成={r['done']} "
                f"失败={r['failed']} 中断={r['stopped']}"
                f"{' [经代理 ' + proxy + ']' if proxy else ''}\n"
                f"结果已入抓包历史（按 batch_id={r['batch_id']} 拉取）。")

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
                f"状态={r.get('status')} 目标host={r.get('target_host') or '-'} "
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

    def _tool_web_search(self, query: str, num: int = 0,
                         engines: str = "", lang: str = "") -> str:
        """联网搜索（2026-10-19，web-search-tool）：SearXNG（可选）→ DuckDuckGo
        HTML（首选免 Key）→ Bing（带退化闸）→ ddgs 的四后端自动降级
        （core/websearch.py）。

        - 结果回填「标题/URL/摘要」编号列表（单条摘要截 300、总量截 8000 字符，
          更大量级由 dispatch 层 H1 spill 兜底）；
        - 空结果是**合法检索结果**（`[无命中]`，不进失败前缀表）；后端全失败回
          `[错误]`（附启用任一一端的指引），意图先行闸豁免口径同 browser_navigate；
        - 落 web.search 审计事件（D9 活跃探索同源 file.search 口径）。"""
        if not isinstance(query, str) or not query.strip():
            return "[错误] query 必须为非空字符串"
        query = query.strip()[:500]
        try:
            n = int(num) if num else 0
        except (TypeError, ValueError):
            n = 0
        from core.websearch import search_web
        try:
            results, backend = search_web(query, num=n or None,
                                          engines=engines, language=lang)
        except Exception as e:  # noqa: BLE001 —— WebSearchError 及一切兜底：工具异常不抛断循环
            return f"[错误] 联网搜索失败: {e}"
        if not results:
            return (f"[无命中] query={query[:100]}（backend={backend}；"
                    "可换关键词/engines，深读页面用 browser_navigate）")
        lines = [f"[web_search] backend={backend} query={query[:100]} "
                 f"命中={len(results)}"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title'][:200]}")
            lines.append(f"   {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet'][:300]}")
        body = "\n".join(lines)
        if len(body) > 8000:
            body = body[:8000] + "\n…[总量超 8000 字符已截断]"
        try:
            self.bb.append_event(
                self.project_id, "web.search",
                {"query": query[:200], "backend": backend, "hits": len(results),
                 "step": self._step},
                session_id=self.session_id, author=self.author)
        except Exception:  # noqa: BLE001
            pass
        return body


# tool.call 成败判定：失败回填的已知前缀（bb_query 空结果返回 JSON "[]" 也以 "[" 开头，
# 故不能用 startswith("[") 一刀切；[无命中] 是合法空检索结果，不算失败）
_TOOL_FAIL_PREFIXES = ("[错误]", "[拒绝]", "[越界拒绝]", "[计划闸]",
                       "[网关拒绝]", "[工具异常]", "[防幻觉]", "[冲突]",
                       "[参数格式]", "[未分析]",
                       # 环境类回填是「本轮没跑成」而非业务成功；但**不进**
                       # HARD_REJECT_PREFIXES——环境故障不该触发拒绝熔断挂起人工。
                       "[环境错误]", "[环境恢复未完成]", "[环境恢复后仍失败]")

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


# ---------------- 导入期自检（fail-fast） ----------------
# 注册表是工具的唯一真相源，实现是本文件的 `_tool_<name>` 方法——两者漂移
# （新增工具只注册没实现、改名漏改）此前要到运行时调用才暴露，现提前到导入期。
_missing = [s.handler for s in REGISTRY.values()
            if not callable(getattr(ToolDispatcher, s.handler, None))]
if _missing:
    raise RuntimeError(
        f"工具注册表与实现不一致：ToolDispatcher 缺少 {_missing}；"
        f"请补 `_tool_<name>` 方法或修正 ToolSpec.handler")
del _missing
