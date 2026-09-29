"""core API（DESIGN.md：黑板所有写操作经 core API 单一入口的 HTTP 化）。

职责边界：
- 只做「HTTP/WS ↔ core 模块调用」的翻译，业务规则全部在 blackboard / tasks /
  gateway / agent / orchestrator 里，这里零业务逻辑。
- 黑板写路径 = Blackboard 方法；Agent 无裸 shell 约束在 gateway 层强制，API 不放水。
- 长耗时动作（跑 Agent、orchestrator tick）→ 后台线程 Job，立即返回 job_id 轮询。

本模块是唯一允许 import fastapi 的地方（核心引擎保持零依赖）。
"""

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketState
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator, model_validator

from core import autonomy
from core import phases as phases_mod
from core.browser import BrowserConfig, BrowserPool, BrowserError
from core.browser.pool import browser_available, chromium_available
from core.browser.replay import Intruder, ReplayClient
from core.agent import AgentConfig, AgentSession
from core.agent.loop import clear_task_resume, persisted_snapshot_path, task_resume_path, task_transcript_path
from core.blackboard import TaskQueue
from core.blackboard.assets import _is_ip, clean_host, import_assets, register_asset
from core.coverage import attach_effective_status
from core.blackboard.graph import board_graph, session_graph
from core.blackboard.attackpath import build_attack_path
from core.blackboard.intents import list_intents, reopen_intent
from core.blackboard import traces, tasktree
from core.blackboard.store import Blackboard, BlackboardClosedError
from core.blackboard.tasks import dedup_fp, render_attempts_lines
from core import assetimport
from core import fofa as fofa_mod
from core.intel import config as intel_config
from core.intel import vault as intel_vault
from core.intel.intel_service import (
    compose_weekly_plan as intel_weekly_plan,
    learning_profile as intel_learning_profile,
    run_refresh as intel_run_refresh,
    week_start as intel_week_start,
)
from core.intel.store import IntelStore
from core.llm import ModelRouter, ProviderError, ProviderStore, probe_credentials
from core.llm.providers import CONFIG_PATH as PROVIDERS_CONFIG_PATH
from core.llm.routing import AVAILABLE_MODELS, KNOWN_ROLES
from core.orchestrator import Orchestrator, OrchestratorConfig
from core.orchestrator import state as orch_state
from core.orchestrator import judgments
from core.projects import Project, ProjectStore
from core.runtime import ExecutionGateway, HostDetector
from core.runtime.policy import RUNTIME_LEVELS
from core.skills import proposals as proposals_mod
from core.skills import refs as refs_mod
from core.skills import writing
from core.skills.doctor import diagnose
from core.skills.experts import (
    allowed_roles as expert_allowed_roles,
)
from core.skills.experts import (
    _parse_expert as _parse_expert_yaml,
    caps_effective,
    expert_exists,
    list_experts,
    load_expert,
)
from core.skills.profiles import BOARD_VIEWS, load_profile, load_track_profiles, profile_snapshot
from core.skills.registry import SkillRegistry, parse_frontmatter
from core.skills.router import SkillRouter
from core.skills.taxonomy import (
    LEGACY_DOMAIN_MAP,
    capability_dir,
    list_packs,
    load_task_types,
    track_dir,
)
from core.skills.proposals import ProposalError, ProposalStateError
from core.skills.writing import pack_write_lock

log = logging.getLogger("core.api")

# 批 5（§6.8）：L2 自动 tick 最小间隔；测试可 monkeypatch 为 0 走全链。
# 触发点踩在间隔内时不丢弃，而是起一个 wait job 睡满后重入闸门（防链搁浅）。
AUTO_TICK_MIN_INTERVAL = 10.0

# A5（§6.4）：L2 自动优先级重排去抖窗口。人/子代理连发任务 30s 内合并成一轮
# planner；手动按钮不受此限。踩窗口同样起 wait job 睡满重入，触发不丢。
REPLAN_MIN_INTERVAL = 30.0

# 任务窗调度器 sweep（v0.71 任务即窗口，§6.8）：绑定段/启动段除事件触发点外
# 的兜底轮询周期——绑定失败（cap 满/预算硬闸/LLM 未就绪）的任务靠 sweep 重试。
SCHEDULE_POLL_INTERVAL = 60.0

# 工作区卫生体检膨胀阈值（workspace-hygiene D6）：候选目录（.tmp/scratch/spill/
# browser-profile）超过 100MB 报 warning、超 500MB 升 error；模块级常量供测试
# monkeypatch（测试造 KB 级小文件配低阈值）。
HYGIENE_BLOAT_MB = 100.0
HYGIENE_BLOAT_ERROR_MB = 500.0

# FOFA 中转配置文件路径（cyberspace-mapping M2）：模块级常量供测试 monkeypatch
# （真实 config/fofa.json 含 key，不入库，测试绝不触达）。
FOFA_CONFIG_PATH = fofa_mod.CONFIG_PATH


def _loads_meta(art: dict) -> dict:
    """artifacts.meta TEXT → dict（_row_to_dict 不解析 JSON 列；旧数据无 meta）。"""
    m = art.get("meta")
    if isinstance(m, dict):
        return m
    try:
        return json.loads(m) if m else {}
    except (TypeError, ValueError):
        return {}


# ---------------- 请求模型 ----------------

class ProjectIn(BaseModel):
    name: str
    track: str = "ctf"                       # 场景轨（单选）
    capabilities: list[str] = Field(default_factory=list)  # 能力包（多选）
    experts: list[str] = Field(default_factory=list)  # 绑定专家（expert-pool M2；M3 起创建页提交，空=存量直通）
    profile: str | None = None               # 场景档 id（M4a：五件套快照物化，D6 物化即弃）
    inherit_from: str | None = None          # 知识继承源项目 id/slug（M4b：只增不覆盖，源只读）
    config: dict = Field(default_factory=dict)
    domain: str | None = None                # 兼容旧客户端：pentest/ctf 透明映射


class ConfigPatchIn(BaseModel):
    """PATCH 项目 config：顶层键浅合并；本批只消费 autonomy 段（§6.8）。"""
    config: dict = Field(default_factory=dict)


class ExpertsPatchIn(BaseModel):
    """换将（expert-pool M2）：绑定专家清单整体替换；空清单=解绑存量直通。"""
    experts: list[str] = Field(default_factory=list)


class ExpertSaveIn(BaseModel):
    """专家池写模型（expert-pool M3，packs/experts/ 单文件池 CRUD）。
    全字段提交式覆写（表单即最终态）；None/空 = 不落键（平铺约定 = 不过滤语义，
    skills 缺键即全量专家）。variants={track: {field: value}} 序列化为
    variant_<track>_<field> 平铺键。"""
    name: str | None = None
    description: str | None = None
    persona: str | None = None
    tracks: list[str] | None = None
    skills: list[str] | None = None
    task_types: list[str] | None = None
    default_noise: str | None = None
    tools: list[str] | None = None
    max_runtime: str | None = None
    max_steps: int | None = None
    variants: dict[str, dict[str, Any]] | None = None


class ExpertCreateIn(ExpertSaveIn):
    id: str


class ReopenIn(BaseModel):
    """C1：放回/「已解决，放回继续」的人类补充说明（写进任务行 result_note 落审计）。
    C6：drop_scene=True = 丢弃现场从零重做（unlink 任务键断点快照与 C10 transcript）。"""
    note: str = ""
    drop_scene: bool = False


class CancelTaskIn(BaseModel):
    """M4 C1：人工取消任务的原因（task.cancelled 事件审计用）。"""
    reason: str = ""


class DirectiveIn(BaseModel):
    """C2 指挥编排器：人类一次性目标指令（自动触发一轮编排，最高优先落实）。"""
    text: str


class OrchChatIn(BaseModel):
    """对话化编排器（M1，§6.4）：与编排器对话（插队轮，busy 409 不排队）。"""
    text: str


class PhaseGoalIn(BaseModel):
    """阶段目标（M2，§4.3）：确认落盘 meta.phase_goal；text 为空 = 清空重议。"""
    text: str = ""
    criteria: list[str] = []
    phase: str | None = None


class OrchPersonaIn(BaseModel):
    """编排器拟人身份（M3，§4.4）：display_name 贯穿前端，persona 只注入对话轮。"""
    display_name: str = ""
    persona: str = ""


class PhaseTransitionIn(BaseModel):
    """分阶段工作流（M2）：人工流转阶段（目标限当前阶段剧本 next 清单内）。"""
    to: str
    reason: str = ""


class AcceptanceIn(BaseModel):
    """验收条目结构化形态（独立验证 M1）：text + 可选 verify 规格（原样透传
    publish，发布期由 validate_verify_spec 校验，未知键/缺键 422）。"""
    text: str
    verify: dict | None = None


class TaskIn(BaseModel):
    objective: str
    task_type: str = "generic"
    role: str = ""                     # v14：建议认领角色（''=不限；认领即换装）
    target_session: str = ""           # v0.71 弃用（任务即窗口：发布即自动建专属窗，
                                       # 绑定移交 _bind_task_window；字段保留兼容旧前端但忽略）
    scope: str = ""
    noise_budget: str | None = None   # 缺省 = 轨注册表该类型的默认噪声
    priority: int = 2
    conflict_keys: list[str] | None = None
    parent_id: str | None = None
    refs: list[str] | None = None   # 任务依据的 finding id（显式层；正文 find-id 自动抽取）
    workset: list[str] | None = None   # 机制 1.1 工作集软声明（advisory，不阻塞认领）
    attachment_ids: list[str] = []     # 2026-09-19 附件随发：kind=attachment 的 artifact id
    acceptance: list[str | AcceptanceIn] | None = None
    # ⑤ 验收条目（存 context.reconcile；全收口才可 complete）。独立验证 M1：条目
    # 支持 str 或 {text, verify}——verify 规格由验证器在 complete 时自动判定
    # （core/verify.py 四策略），Agent 自报 met/failed 被拒（宁严勿松）
    force: bool = False                # 机制 1.1：True 跳过发布去重（人类"仍要发布"确认后）


class InboxRead(BaseModel):
    """标记会话收件箱已读：ids=None/缺省 = 全部已读；否则只标给定私信行。"""
    ids: list[str] | None = None


class TaskPatch(BaseModel):
    """任务编辑（PATCH，exclude_unset：不传的字段不动）。open/failed 全字段可改；
    claimed（执行中）仅放行 role（v0.71 任务即窗口：中途改角色立即热换装）。"""
    objective: str | None = None
    task_type: str | None = None
    noise_budget: str | None = None
    priority: int | None = None
    conflict_keys: list[str] | None = None
    role: str | None = None
    preferred_runtime: str | None = None  # v23：任务默认运行时（''=重置；open/failed 可改）


class FindingIn(BaseModel):
    # 渗透发现用 vuln_class；逆向发现可留空——类别在 evidence.category 五类里
    vuln_class: str = ""
    title: str
    target_asset_id: str | None = None
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"  # F11 白名单
    rating_basis: str = ""  # F11 判级依据（注入评级口径时必填）
    impact: str = ""  # 收录格式三件套·危害描述（v20，不进门禁）
    remediation: str = ""  # 收录格式三件套·修复建议（v20，不进门禁）
    status: str = "unverified"
    category: Literal["vuln", "intel"] | None = None  # C6 分两类；缺省按 vuln_class/severity 自动判
    evidence: dict = Field(default_factory=dict)
    dedup_key: str | None = None


class AssetIn(BaseModel):
    type: str = "auto"  # auto=按值自动识别（E6）；识别不出 422 提示手选
    value: str
    meta: dict = Field(default_factory=dict)
    parent_id: str | None = None  # 资产树挂载（DESIGN.md §5.2）：domain/service/url 挂 host 下


class AssetPatchIn(BaseModel):
    """PATCH /api/assets/{aid}：parent_id / meta 二选一或同传；
    exclude_unset 区分「不传（不动）」与「传 null（摘挂）」。"""
    parent_id: str | None = None
    meta: dict | None = None


class FofaConfigIn(BaseModel):
    """PUT /api/fofa/config：key 传空串/缺省 = 不修改（防回显误覆盖）；"""
    base_url: str | None = None
    key: str | None = None


class FofaSearchIn(BaseModel):
    """POST /api/projects/{pid}/fofa/search：FOFA 语法原样（如
    `domain="example.com"`）；size 单次消耗等量配额，前端提示后选。"""
    query: str
    size: int = 100
    page: int = 1


class AssetImportIn(BaseModel):
    """POST /api/projects/{pid}/assets/import：mapping 给出 → rows 为表格二维
    数组先 normalize_rows；mapping 缺省 → rows 为已归一化行 dict（FOFA 勾选/
    手工 JSON 导入）。"""
    source: str = "manual"  # xlsx/csv/fofa/manual
    rows: list[Any] = Field(default_factory=list)
    mapping: list[str] | None = None


class FuncCreateIn(BaseModel):
    """POST /api/projects/{pid}/funcs：为「仅在 headless 缓存里、func_kb 无行」
    的函数建行（人写笔记前的补建）。address 接受 int 或 hex 字符串（64 位地址
    不能走 JS Number，服务端 int(str, 0) 可吃任意大 hex）。"""
    binary_sha256: str
    address: int | str
    name: str
    analysis: str = ""

    @field_validator("binary_sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("binary_sha256 必须是 64 位 hex")
        return v.lower()

    @field_validator("address")
    @classmethod
    def _addr(cls, v: int | str) -> int:
        try:
            return int(str(v), 0)
        except ValueError as e:
            raise ValueError("address 需为整数或 0x 前缀的 hex 字符串") from e


class FuncPatchIn(BaseModel):
    """PATCH funcs/{id}：改名 / 追笔记 / risk_tags 全量替换（null 与不传=不动）。"""
    name: str | None = None
    note: str | None = None
    risk_tags: list[str] | None = None


class FindingPatchIn(BaseModel):
    """PATCH findings/{id}：状态机 + evidence 浅层 merge + F10 人工修订 + C6 分类。"""
    status: str | None = None
    evidence: dict | None = None
    title: str | None = None
    severity: str | None = None
    vuln_class: str | None = None
    category: str | None = None
    rating_basis: str | None = None  # F11：None=不动，空串=清空
    impact: str | None = None  # 收录格式三件套·危害描述：None=不动，空串=清空
    remediation: str | None = None  # 收录格式三件套·修复建议：None=不动，空串=清空


class ChainIn(BaseModel):
    """POST chains：人工建链（P2 不做 Agent 建链）。"""
    name: str
    goal: str = ""


class ChainPatchIn(BaseModel):
    name: str | None = None
    goal: str | None = None
    status: str | None = None


class BlueprintModuleIn(BaseModel):
    """POST blueprints：模块条目（R4 逆向开发蓝图）。"""
    name: str
    desc: str = ""
    func_addresses: list[str] = []
    spec: str = ""
    notes: str = ""
    status: str = "pending"


class BlueprintIn(BaseModel):
    """POST blueprints：人工/人类代录建蓝图（Agent 走 bb_blueprint_create 工具）。"""
    name: str
    goal: str = ""
    binary_sha256: str = ""
    modules: list[BlueprintModuleIn] = []
    content_md: str = ""


class BlueprintPatchIn(BaseModel):
    """PATCH blueprints：人类流转 status（draft→reviewed→ready→building→built 白名单）
    / 改名 / 改 goal / 补正文。Agent 无此入口（工具不暴露 status）。"""
    name: str | None = None
    goal: str | None = None
    status: str | None = None
    content_md: str | None = None
    content_append: str | None = None


class BlueprintModulePatchIn(BaseModel):
    """PATCH blueprint 模块：人类修订模块卡片（深析写回走 Agent 工具）。"""
    desc: str | None = None
    spec: str | None = None
    notes: str | None = None
    func_addresses: list[str] | None = None
    status: str | None = None


class ChainLinkIn(BaseModel):
    node_type: str
    node_id: str
    edge_note: str = ""


class LogicBlockIn(BaseModel):
    """POST logic-blocks：人工建业务逻辑块（Agent 走 bb_logic_block_* 工具）。"""
    name: str
    description: str = ""
    binary_sha256: str = ""
    seq: int = 0


class LogicBlockPatchIn(BaseModel):
    """PATCH logic-blocks：人类修订块名/描述/排序（Agent 补描述走工具）。"""
    name: str | None = None
    description: str | None = None
    seq: int | None = None


class LogicBlockFuncIn(BaseModel):
    """POST logic-blocks/{lbid}/funcs：挂接函数。address 吃 int/0x hex/十进制串
    （前端以 hex 串为准，JS Number 无法安全表示 64 位地址）。"""
    address: int | str
    role: str = ""


class LogicBlockFuncPatchIn(BaseModel):
    """PATCH funcs 挂接：改角色注（address 经 query 传）。"""
    role: str


class LinkNoteIn(BaseModel):
    edge_note: str


class WritebackItemIn(BaseModel):
    """POST binaries/{sha}/writeback 单条：func_kb 命名/注释回写 .i64。

    address 吃 int/0x hex/十进制串（64 位地址不能走 JS Number）；name/comment 至少一条。
    """
    address: int | str
    name: str | None = None
    comment: str | None = None

    @field_validator("address")
    @classmethod
    def _addr(cls, v: int | str) -> int:
        try:
            n = int(str(v), 0)
        except ValueError as e:
            raise ValueError("address 需为整数或 0x 前缀的 hex 字符串") from e
        if n < 0:
            raise ValueError("address 不可为负数")
        return n

    @model_validator(mode="after")
    def _has_payload(self):
        if not (self.name or self.comment):
            raise ValueError("每条至少提供 name 或 comment")
        if self.name is not None and not self.name.strip():
            self.name = None
        if self.comment is not None and not self.comment.strip():
            self.comment = None
        if self.name and len(self.name) > 200:
            raise ValueError("name 过长（上限 200）")
        if self.comment and len(self.comment) > 8000:
            raise ValueError("comment 过长（上限 8000）")
        if not (self.name or self.comment):
            raise ValueError("每条至少提供 name 或 comment")
        return self


class WritebackIn(BaseModel):
    items: list[WritebackItemIn] = Field(min_length=1, max_length=500)


class SkillUpdateIn(BaseModel):
    content: str  # SKILL.md 全文（含 frontmatter）


class SkillCreateIn(BaseModel):
    """新建技能向导：name + frontmatter 常用字段，正文给中文薄路由模板。"""
    name: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    file_features: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    vuln_classes: list[str] = Field(default_factory=list)
    task_types: list[str] = Field(default_factory=list)


class SkillEnabledIn(BaseModel):
    enabled: bool


class FileContentIn(BaseModel):
    content: str  # 红线等单文件全文


class RoutePreviewIn(BaseModel):
    """路由试算器：track+capabilities 定候选集，role 再窄化；domain 仅旧客户端兼容。"""
    query: str = ""
    track: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    role: str | None = None
    features: list[str] = Field(default_factory=list)
    file_features: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    include_disabled: bool = False
    domain: str | None = None


# ---- kb 本地基线（C2） ----

class KbWriteIn(BaseModel):
    path: str
    content: str


class KbRenameIn(BaseModel):
    path: str
    new_path: str


# ---- 统一提案（C4） ----

class ProposalIn(BaseModel):
    """提一条变更提案（只落 pending）。target.kind=kb 时给 cap/path[/new_path]；
    kind=skill 时给 skill_kind/owner/name。"""
    target: dict
    mode: str
    content: str | None = None
    summary: str
    reason: str
    origin: str = "human"
    project: str | None = None
    session: str | None = None
    task: str | None = None
    evidence: str = ""

    @field_validator("origin")
    @classmethod
    def _origin_ok(cls, v: str) -> str:
        if v not in {"agent", "review", "human"}:
            raise ValueError("origin 仅 agent|review|human")
        return v


class ProposalDecisionIn(BaseModel):
    decided_by: str = "human"
    note: str = ""

    @field_validator("decided_by")
    @classmethod
    def _by_ok(cls, v: str) -> str:
        if v not in {"human", "demo-script(auto)"}:
            raise ValueError("decided_by 仅 human 或显式 demo-script(auto)")
        return v


class ProposalReviseIn(BaseModel):
    by: str = "human"
    changes: dict = Field(default_factory=dict)
    note: str = ""


class McpServerIn(BaseModel):
    name: str
    url: str = ""                          # http 传输用；stdio 留空
    transport: str = "streamable-http"     # streamable-http | stdio
    enabled: bool = True
    domains: list[str] = Field(default_factory=list)
    command: str | None = None             # stdio 用：可执行命令
    args: list[str] = Field(default_factory=list)  # stdio 用：命令参数


class McpConfigIn(BaseModel):
    servers: list[McpServerIn]


class ChatThreadIn(BaseModel):
    agent_id: str
    title: str | None = None


class ChatMessageIn(BaseModel):
    text: str
    refs: dict[str, list[str]] | None = None  # 人类引用指定 {skills:[], mcps:[]}


# ---------------- packs 管理辅助（设置页：角色 / Skill / 红线 / MCP） ----------------

_NAME_RE = re.compile(r"^[\w][\w.-]{0,63}$")  # 名称白名单（防穿越；\w Unicode-aware 会放行中文——技能/包名等沿用）
MCP_CONFIG_PATH = Path("config/mcp.json")     # 与 llm.json 同级（cwd = 项目根）

# MCP server 允许声明的领域；http server 只连本机 loopback（红线，见 decompiler._is_loopback_url）
_MCP_TRANSPORTS_HTTP = ("streamable-http", "http", "http-stream")
_MCP_DOMAIN_WHITELIST = {"pentest", "reverse", "binary"}


def _load_mcp_config() -> dict:
    """读 config/mcp.json；缺失/损坏给空配置（调用方回默认端点，绝不 500）。"""
    try:
        if MCP_CONFIG_PATH.is_file():
            data = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _pack_history_backup(path: Path) -> Path | None:
    """写入前留修改历史：.history/<UTC时间戳>_<文件名>（设置页可编辑文件的回滚依据）。
    返回备份路径（原文件不存在则无备份）。"""
    if not path.is_file():
        return None
    with pack_write_lock():  # 同秒 .n 避让与 packs 写锁同一临界区，防并发备份互相覆盖
        hist = path.parent / ".history"
        hist.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dest = hist / f"{ts}_{path.name}"
        n = 1
        while dest.exists():  # 同秒多次写入避让，绝不覆盖既有版本（回滚往返也依赖它）
            dest = hist / f"{ts}.{n}_{path.name}"
            n += 1
        shutil.copy2(path, dest)
    return dest


# 历史版本两种命名：API 写入用 <ts>[.n]_<file>；import_kb owners 覆盖用 <file>.<ts>.bak
_HIST_TS = r"\d{8}T\d{6}Z"


def _trash_move(src: Path) -> Path:
    """删除不物理抹除：移入同级 .history/trash/<名>.<UTC时间戳>[.n]（文件带 .bak 后缀）。

    同秒二次删除（如同名角色删了又建再删）用序号避让，绝不覆盖既有回收件。"""
    with pack_write_lock():  # 同秒 .n 避让与 packs 写锁同一临界区
        trash = src.parent / ".history" / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        is_dir = src.is_dir()
        tail = "" if is_dir else ".bak"
        dest = trash / f"{src.name}.{ts}{tail}"
        n = 1
        while dest.exists():
            dest = trash / f"{src.name}.{ts}.{n}{tail}"
            n += 1
        shutil.move(str(src), str(dest))
    return dest


def _history_versions(path: Path) -> list[dict]:
    """列某现行文件的全部历史版本（兼容两种备份命名），按时间倒序。"""
    hist = path.parent / ".history"
    if not hist.is_dir():
        return []
    pat_api = re.compile(rf"({_HIST_TS})(?:\.\d+)?_{re.escape(path.name)}$")
    pat_bak = re.compile(rf"{re.escape(path.name)}\.({_HIST_TS})\.bak$")
    out: list[dict] = []
    for f in hist.iterdir():
        if not f.is_file():
            continue
        m = pat_api.fullmatch(f.name) or pat_bak.fullmatch(f.name)
        if not m:
            continue
        st = f.stat()
        out.append({"version": f.name, "ts": m.group(1),
                    "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))})
    out.sort(key=lambda v: v["ts"], reverse=True)
    return out


def _resolve_managed_file(app: Any, file_rel: str) -> Path:
    """历史端点的 packs 相对路径解析：只许 capabilities/tracks 下、逐段白名单、
    resolve 后必须仍在 packs_root 内（防穿越双保险）。"""
    if not file_rel or "\x00" in file_rel:
        raise HTTPException(422, "非法文件路径")
    rel = Path(file_rel)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise HTTPException(422, "非法文件路径：只接受 packs 内相对路径")
    if rel.parts[0] not in {"capabilities", "tracks"}:
        raise HTTPException(422, "非法文件路径：只接受 capabilities/ 或 tracks/ 下文件")
    for seg in rel.parts:
        _check_name(seg, "路径段")
    root = Path(app.state.packs_root).resolve()
    path = (root / rel).resolve()
    if os.path.commonpath([str(root), str(path)]) != str(root):
        raise HTTPException(422, "非法文件路径：越出 packs 根")
    return path


_SKILL_FRONTMATTER_LIST_KEYS = (
    "keywords", "features", "file_features", "platforms",
    "formats", "vuln_classes", "task_types",
)


def _dump_new_skill(name: str, body: "SkillCreateIn") -> str:
    """新建技能的中文薄路由模板：frontmatter 带向导字段，正文给「观察→kb_open」骨架。"""
    lines = ["---", f"name: {name}"]
    if body.description:
        lines.append(f"description: {body.description}")
    for key in _SKILL_FRONTMATTER_LIST_KEYS:
        vals = [v.strip() for v in getattr(body, key) if v.strip()]
        if vals:
            lines.append(f"{key}: " + ", ".join(vals))
    lines += [
        "---", "",
        f"# {name}", "",
        "> 中文薄路由技能：正文只写「观察到什么特征 → kb_open 打开哪个快照模块」的对照与",
        "> 最短纪律；方法论细节留在 kb/ 英文快照（不进 registry、不通读、不就地修改）。",
        "", "## 适用场景", "",
        "- （什么任务/什么特征下应被路由到本技能）", "",
        "## 观察 → 开模块", "",
        "| 观察到的特征 | kb_open 模块 |",
        "|---|---|",
        "| （例：保护组合 NX+Canary+PIE） | `kb_open(module=\"<快照名>/<模块>.md\")` |",
        "", "## 红线与收尾", "",
        "- 只读参考快照；POC/产物一律落黑板 artifact，不写回技能与 kb/。",
        "",
    ]
    return "\n".join(lines)


def _set_skill_enabled(path: Path, enabled: bool) -> Path | None:
    """enabled 快速开关：只动 frontmatter 的 enabled 行，正文与其它字段原样保留。
    返回切换前的 .history 备份路径（None=文件先前不存在，不会发生）。

    无该行且要禁用时插在 name 行之后；启用时把值改回 true（缺省语义即启用，
    但显式 true 比删行更能在 .history 里留下可读痕迹）。"""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise HTTPException(422, "技能文件缺少 frontmatter，无法切换 enabled")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise HTTPException(422, "技能文件 frontmatter 不闭合")
    head, body = parts[1], parts[2]
    lines = head.splitlines()
    hit = False
    for i, line in enumerate(lines):
        if re.match(r"^enabled\s*:", line):
            lines[i] = f"enabled: {'true' if enabled else 'false'}"
            hit = True
    if not hit and not enabled:
        idx = next((i for i, line in enumerate(lines)
                    if re.match(r"^name\s*:", line)), -1)
        lines.insert(idx + 1, "enabled: false")
    new_head = "\n".join(lines)
    if head.startswith("\n") and not new_head.startswith("\n"):
        new_head = "\n" + new_head
    if head.endswith("\n") and not new_head.endswith("\n"):
        new_head += "\n"
    with pack_write_lock():
        backup = _pack_history_backup(path)
        path.write_text(f"---{new_head}---{body}", encoding="utf-8")
    return backup


def _check_name(value: str, label: str) -> None:
    if not _NAME_RE.fullmatch(value):
        raise HTTPException(422, f"非法{label}: {value}")


def _cap_path(app: Any, cap: str, *parts: str) -> Path:
    """packs/capabilities/<cap>/... 路径校验（各段过白名单，防穿越）。"""
    _check_name(cap, "能力包")
    for p in parts:
        _check_name(p, "名称")
    return capability_dir(app.state.packs_root, cap).joinpath(*parts)


def _track_path(app: Any, track: str, *parts: str) -> Path:
    """packs/tracks/<track>/... 路径校验（各段过白名单，防穿越）。"""
    _check_name(track, "场景轨")
    for p in parts:
        _check_name(p, "名称")
    return track_dir(app.state.packs_root, track).joinpath(*parts)


class AgentIn(BaseModel):
    role: str = "_generalist"
    session_name: str | None = None
    model: str | None = None  # per-session 覆盖 executor 模型（DESIGN.md §8）；None=供应商默认
    provider: str | None = None  # 供应商名（config/providers.json）；None=全局默认供应商
    max_steps: int = 200  # E8：默认步数预算（角色 yaml 取 min 可更严；request_steps 可自助 +200）
    # worker 启动制（F9）：人手开窗默认不接任务（armed=False，点「跑任务队列」启动）；
    # 编排/审批开窗走 armed=True 路径。armed=True 且 L1/L2 未暂停才触发触发点 C。
    armed: bool = False

    @field_validator("max_steps")
    @classmethod
    def _check_max_steps(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_steps 须为正整数")
        return v


class LlmProviderIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""        # 读出脱敏；保存空串=保持原 key
    models: list[str] = Field(default_factory=list)
    enabled: bool = True
    # 每模型最大上下文（token，可选；非法/未勾选项由 _validate 静默剔除）
    model_context: dict[str, int] = Field(default_factory=dict)


class ProvidersIn(BaseModel):
    providers: list[LlmProviderIn]
    default_provider: str | None = None  # 用户可选的全局默认供应商（须为启用中的供应商）


class DiscoverIn(BaseModel):
    name: str | None = None       # 已保存供应商
    base_url: str | None = None   # 未保存的新供应商：直接用地址+key 发现
    api_key: str | None = None


class TestModelIn(BaseModel):
    name: str | None = None       # 已保存供应商：用其 base_url/key（api_key 字段可覆盖）
    base_url: str | None = None   # 未保存的新供应商：直接给地址+key 测
    api_key: str | None = None
    model: str


class SwitchLlmIn(BaseModel):
    provider: str
    model: str | None = None


class TickIn(BaseModel):
    allowed_roles: list[str] | None = None
    max_sessions: int = 4
    digest_every: int = 3
    max_steps: int = 12
    # auto-attack 研判轮（2026-09-28）：只分析不派活，产出落 orch.auto_attack.analyzed
    analyze_only: bool = False
    budget_ticks: int | None = None  # 研判时人类预选的链轮数预算（回显进 analyzed 事件）


class ApprovalDecisionIn(BaseModel):
    decision: str  # approved / rejected

    @field_validator("decision")
    @classmethod
    def _check(cls, v: str) -> str:
        if v not in {"approved", "rejected"}:
            raise ValueError("decision 只能是 approved / rejected")
        return v


class SessionResumeIn(BaseModel):
    """E8：恢复暂停会话的可选参数——引导语随快照注入；步数增补仅对
    预算暂停（reason=budget）生效，缺省自动 +200（与 request_steps 同增量）。"""
    note: str | None = None
    extra_steps: int | None = None


class SessionNoteIn(BaseModel):
    text: str = ""                     # 2026-09-19 起可空：纯附件引导（attachment_ids 非空）
    attachment_ids: list[str] = []     # 附件随发：kind=attachment 的 artifact id


class SessionRoleIn(BaseModel):
    role: str                          # 会话中心化：换人目标专家 id（§4.4）


class IntelFeedItem(BaseModel):
    name: str = ""
    url: str


class IntelFeedsIn(BaseModel):
    feeds: list[IntelFeedItem]


class IntelProfileIn(BaseModel):
    directions: dict[str, float]
    stage: str = ""
    vault: dict | None = None  # E10：透传给 save_profile 归一化（path/enabled）


class IntelVaultIn(BaseModel):
    path: str = ""
    enabled: bool = False


class IntelArticlePatch(BaseModel):
    read: bool | None = None
    starred: bool | None = None


# ---------------- Job 注册表（长耗时动作） ----------------

class JobRegistry:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[], Any], *, meta: dict | None = None,
               on_done: Callable[[dict], Any] | None = None) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {"id": job_id, "kind": kind, "status": "running",
                                  "result": None, "error": None, "submitted_at": time.time(),
                                  "meta": meta or {}}

        def runner():
            try:
                result = fn()
                with self._lock:
                    self._jobs[job_id].update(status="done", result=result)
            except Exception as e:  # noqa: BLE001 —— 失败也要可轮询
                with self._lock:
                    self._jobs[job_id].update(status="error", error=f"{type(e).__name__}: {e}")
            # 状态翻完后再回调（批 5 续链评估靠它：worker 在 tick 收尾期间秒退时
            # A 触发会因 tick job 仍 running 而跳过，由 tick 自己的 on_done 兜底）
            if on_done is not None:
                try:
                    on_done(self.get(job_id))
                except Exception:  # noqa: BLE001 —— 回调异常不得影响 job 状态
                    log.exception("job on_done 回调失败 job=%s", job_id)

        threading.Thread(target=runner, daemon=True, name=f"job-{job_id}").start()
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            return dict(self._jobs.get(job_id)) if job_id in self._jobs else None

    def all_jobs(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]


# ---------------- App 工厂 ----------------

def create_app(
    workspace_root: str = "workspaces",
    packs_root: str = "packs",
    tools_root: str | None = "tools",
    *,
    executor_llm: Any | None = None,
    planner_llm: Any | None = None,
    providers_config: str = PROVIDERS_CONFIG_PATH,
    intel_dir: str | Path = "config/intel",
    mission_poll_interval: float = 60.0,
    sediment_proposals: bool = True,
    static_dir: str | Path | None = None,
) -> FastAPI:
    """executor_llm / planner_llm 缺省时按供应商配置（config/providers.json）+
    llm.json 文件级覆写构建 provider；测试可注入假 provider。
    两者都为 None 且无法构建 → Agent 相关端点返回 503。
    mission_poll_interval：mission 自动派生兜底轮询秒数（0=不启动，测试用）。
    sediment_proposals：v0.65 done 自动提案开关——剧本式 planner 的测试必须关
    （复盘 chat 会额外消费 planner 剧本项导致编排错位）。
    static_dir：vite build 产物目录（webui/dist）——非 None 时挂 SPA 同源静态托管
    （desktop-app-shell M2，DESIGN §1）：真实文件直出、其余 GET 回 index.html
    （history fallback）；/api、/docs、/openapi.json 不受兜底影响。"""

    app = FastAPI(title="obsius core API", version="0.1")

    def _persist_live_snapshots_on_shutdown() -> list[str]:
        """v0.64 优雅停机钩子：对所有在跑任务且未暂停的会话即时落断点快照
        （pause_snapshot_now，尾部 sanitize）——没点暂停就被关的后端，重启后
        同样由清扫归位 paused + 豁免其 claimed 任务，点「继续」断点续跑。
        行状态不动（worker 线程随进程消亡，清扫按 resume_snapshot 指针归位）。
        返回落盘成功的 sid 列表；挂 app.state 供测试直调。"""
        done: list[str] = []
        for agent in app.state.agents.values():
            try:
                if (agent.dispatcher.current_task_id is not None
                        and not agent.paused and agent.pause_snapshot_now()):
                    done.append(agent.session["id"])
            except Exception:  # noqa: BLE001
                log.exception("停机快照落盘失败（会话 %s）",
                              agent.session.get("id"))
        return done

    app.state.shutdown_persist = _persist_live_snapshots_on_shutdown
    app.add_event_handler("shutdown", _persist_live_snapshots_on_shutdown)

    @app.exception_handler(BlackboardClosedError)
    async def _bb_closed_handler(request, exc):  # noqa: ANN001
        # 删除窗口内在飞的读/写请求（连接被 close_all 关闭）→ 409 而非 500
        return JSONResponse(status_code=409,
                            content={"detail": "项目正在删除中，请稍后刷新"})

    store = ProjectStore(workspace_root)
    app.state.store = store
    # ⑥ 战役记忆全局库（data/campaign.db，workspace-hygiene D1 归位全局运行时数据；
    # 仿 intel 全局 DB 先例）。落 workspace_root 同级 data/（workspaces/ 契约只收项目），
    # 构造内含老位置 workspaces/campaign.db 惰性迁移；测试传 tmp workspace_root 时库
    # 落 tmp 同级，保持 hermetic。
    from core.blackboard.campaign import CampaignMemory
    app.state.campaign = CampaignMemory(
        Path(workspace_root).parent / "data" / "campaign.db")
    app.state.projects: dict[str, Project] = {}      # pid -> Project（连接复用）
    # 删除中的项目闸门：删除窗口内 _project 拒绝重入（防 pop 缓存后被轮询/open_project
    # 重建 Blackboard 实例重新锁死 db）；WS tick 见到即自行退出。
    app.state.projects_closing: set[str] = set()
    app.state.agents: dict[str, AgentSession] = {}   # sid -> AgentSession
    # 批 5（§6.8）：本进程已启动的 L2 自动链 pid 集合——重启=急停，DB chain_active
    # 持久但无此标记的链不得自动续（_maybe_auto_tick 落 orch.chain_stopped{restart}）。
    app.state.active_chains: set[str] = set()
    app.state.jobs = JobRegistry()
    app.state.inventory = HostDetector().probe(tools_root=tools_root)
    app.state.tools_root = tools_root  # gateway-config-view：probe 刷新端点复用
    app.state.packs_root = packs_root
    app.state.llm_store = ProviderStore(providers_config)
    # 研究工作台反编译服务（pid -> DecompilerService，项目级复用）；
    # rev_service_factory 供测试注入假后端（签名 factory(proj) -> service）
    app.state.rev_services: dict[str, Any] = {}
    app.state.rev_service_factory = None
    # IDA 拉取停止信号（sha -> threading.Event，页间检查点；job 结束残留无害，
    # 下次 submit 覆盖——2026-09-30 断点续拉）
    app.state.pull_cancel: dict[str, threading.Event] = {}
    # 情报面板（E9，全局模块）：惰性建 IntelStore（首访问情报端点才落 config/intel/）；
    # intel_getter / intel_llm 为测试注入口（None = urllib 真抓 / classifier 路由）
    app.state.intel_dir = str(intel_dir)
    app.state.intel: IntelStore | None = None
    app.state.intel_getter = None
    app.state.intel_llm = None
    # C2 mission 自动派生：per-pid 防空转状态（{pid: {tip, empty}}）+ 判据模板目录
    app.state.mission_derive: dict[str, dict[str, Any]] = {}
    # llm.error 事件节流记账（"{pid}:{错误文案}" -> monotonic 时间，2026-09-18）
    app.state.llm_error_seen: dict[str, float] = {}
    app.state.judgments_dir = Path("config")
    # F6 内置浏览器：每项目常驻实例池（懒启动；缺 playwright 全链降级不 500，
    # config/browser.json 缺失=全默认）；爆破批次 stop 事件表 batch_id -> Event
    app.state.browser_pool = BrowserPool(
        workspace_root, bb_getter=lambda p: _project(p).bb,
        config=BrowserConfig.from_file("config/browser.json"))
    app.state.intruder_runs: dict[str, threading.Event] = {}

    def _browser_shutdown() -> None:
        app.state.browser_pool.close_all()

    app.add_event_handler("shutdown", _browser_shutdown)

    # IDA-MCP 实例管理器（2026-09-20 按需拉起+空闲关，DESIGN.md §7）：Agent 点查
    # decompile/xrefs 时按 (项目, 样本) 拉起无窗口 idat 并连其 MCP 端点，空闲
    # 10min 自动关，实例上限 2（LRU），shutdown 兜关；db 与 headless 分诊同落盘
    # （接线时按项目传 db_dir）。无 IDA 环境整体降级 no-op（ensure 恒 None）。
    from core.tools.ida_mcp_manager import IdaMcpManager
    app.state.ida_mcp_manager = IdaMcpManager()

    def _ida_mcp_shutdown() -> None:
        app.state.ida_mcp_manager.shutdown_all()

    app.add_event_handler("shutdown", _ida_mcp_shutdown)

    def _llms():
        exec_llm = executor_llm
        plan_llm = planner_llm
        if exec_llm is None:
            try:
                llm_store: ProviderStore = app.state.llm_store
                router = ModelRouter()

                def _build(role: str):
                    # llm.json 覆写（裸模型名→默认供应商；{provider,model}→指定）；无覆写→全局默认
                    t = router.target_for(role)
                    if t is None:
                        return llm_store.build()
                    prov, model = t
                    return llm_store.build(prov, model)

                exec_llm = _build("executor")
                if plan_llm is None:
                    plan_llm = _build("planner")
            except Exception as e:  # noqa: BLE001 —— 无 key/无启用供应商时 Agent 端点不可用
                raise HTTPException(503, f"LLM 未就绪（{e}），Agent/编排端点不可用") from e
        return exec_llm, plan_llm

    def _project(pid: str) -> Project:
        if pid in app.state.projects_closing:
            raise HTTPException(409, "项目正在删除中，请稍后刷新")
        proj = app.state.projects.get(pid)
        if proj is None:
            try:
                proj = store.open_project(pid)
            except FileNotFoundError:
                raise HTTPException(404, f"项目不存在: {pid}")
            _sweep_restarted_project(proj)
            app.state.projects[pid] = proj
        return proj

    def _sweep_restarted_project(proj: Project) -> None:
        """重启纪律（§3）：单进程部署下进程重启=内存态清零，项目在本进程首次
        打开时归位陈旧状态——① 孤儿 claimed 任务统一 fail(awaiting_human
        「后端重启，任务中断」，看板出「待人工/放回继续」，不自动重跑；
        v0.64 豁免：持有落盘暂停快照的会话其 claimed 任务保留，等「继续」
        断点续跑)；② 排水未竟（close_pending）的会话补关窗（关窗请求是人
        下达的，照常执行）；③ 陈旧 running/blocked 会话行按 meta 归位：有落盘
        快照回 paused 可续跑、指针悬空（文件缺）清指针回 idle；paused 是合法
        持久态不动；④ 重启即急停（2026-09-28）：全部 worker_armed 一律解除
        ——armed 是常驻自动接活的持久开关，不清则 kick 链把陈旧会话当待命窗
        重新拉起（青灯常亮+后台烧 LLM）；人工「跑任务队列/▶继续」重新点亮；
        ⑤ 会话继续钮（2026-09-28）：有落盘快照（现场可续）的会话**一律**归位
        paused（不限 running/blocked）——idle+快照+claimed 组合此前落盲区
        （灰点无继续钮、任务无续跑钮、跑队列撞暂停闸空退，现场卡死）。"""
        bb = proj.bb
        # v0.64 暂停快照豁免：claimed 任务属于「resume_snapshot 指针 + 快照文件
        # 双双在场」的会话（暂停/预算/停机自动暂停）时不 fail——保留 claimed 等
        # 「▶ 继续」从断点续跑（run_next_task 快照分支要求 status=="claimed"）。
        # 指针在但文件缺失视为无快照：不豁免、行归 idle，防「paused 但载不回
        # 快照、任务悬挂」的死态。
        snap_dir = Path(proj.path) / "snapshots"
        keep: set[str] = set()
        for row in bb.list_sessions(proj.id):
            if row.get("status") == "closed":
                continue
            meta = json.loads(row["meta"]) if isinstance(row["meta"], str) \
                else (row["meta"] or {})
            ptr = meta.get("resume_snapshot")
            if ptr and (snap_dir / str(ptr)).is_file():
                keep.add(row["id"])
        TaskQueue(bb).fail_interrupted_claims(proj.id, keep_claimed_by=keep)
        # C6 孤儿对账：任务键快照指向的任务行已不存在 → 清理（防孤儿文件永久残留；
        # done/failed/open 任务的快照按生命周期保留——fail 可续跑、done 留复盘）
        if snap_dir.is_dir():
            tq_all = TaskQueue(bb)
            for f in snap_dir.glob("task-*.resume.json"):
                tid = f.name[len("task-"):-len(".resume.json")]
                if not tid or tq_all.get_task(tid) is None:
                    try:
                        f.unlink()
                    except OSError:
                        log.warning("孤儿任务键快照清理失败（任务 %s）", tid)
        for row in bb.list_sessions(proj.id):
            if row.get("status") == "closed":
                continue
            meta = json.loads(row["meta"]) if isinstance(row["meta"], str) \
                else (row["meta"] or {})
            try:
                # 2026-09-28 重启即急停：worker_armed 是「常驻自动接活」持久开关，
                # 进程重启后内存 worker 全灭，残留 armed 会让任何 kick 触发点把
                # 陈旧 running 会话当待命窗重新拉起（页签青灯常亮 + 后台自动烧
                # LLM）——一律解除武装；恢复=人工「跑任务队列」/「▶继续」（两者
                # 都会重新点亮 armed，恢复流程不受影响）
                if meta.get("worker_armed"):
                    bb.set_session_meta(row["id"], {"worker_armed": False})
                if meta.get("close_pending"):
                    bb.close_session(row["id"])
                    continue  # 已关窗，不再归位
                # 2026-09-28 会话继续钮（清扫归位盲区修复）：有落盘快照（现场可续）
                # 的会话一律归位 paused——此前只对 running/blocked 行按快照归位，
                # idle+快照+claimed 任务的组合（重启前已 idle 但留有暂停快照，
                # fail_interrupted_claims 按快照豁免保留 claimed）落进盲区：会话
                # 灰点无「▶继续」、任务 claimed 无「⚡续跑」、跑任务队列又撞
                # rehydrate 暂停闸空退，现场白白卡死。归 paused 后前端自然显示
                # 「已暂停」+「▶继续」，resume_session 载快照从断点续跑
                ptr = meta.get("resume_snapshot")
                if ptr and (snap_dir / str(ptr)).is_file():
                    bb.set_session_status(row["id"], "paused")
                elif row.get("status") in {"running", "blocked"}:
                    if ptr:  # 指针悬空（文件缺失）→ 清掉再归 idle
                        bb.set_session_meta(row["id"], {"resume_snapshot": None})
                    bb.set_session_status(row["id"], "idle")
            except ValueError:
                continue  # 并发首开时已被另一路径归位

    def _tq(pid: str) -> TaskQueue:
        return TaskQueue(_project(pid).bb)

    def _rev_service(proj: Project):
        """研究工作台的 headless 反编译服务（项目级复用）。

        项目独立目录：缓存在 artifacts/decompiler-cache，IDA 库在 artifacts/decompiler-db，
        Ghidra 临时工程在 artifacts/.ghidra-tmp（均不进 samples/ 的 untrusted 只读语义）。
        headless 是可信解析（只解析不执行样本），走 host + 网关审计，超时 900s。
        """
        cached_svc = app.state.rev_services.get(proj.id)
        if cached_svc is not None:
            return cached_svc
        factory = app.state.rev_service_factory
        if factory is not None:
            svc = factory(proj)
        else:
            from core.tools.decompiler import (
                build_headless_service, gateway_runner, select_mcp_endpoint)

            gateway = ExecutionGateway(bb=proj.bb)
            runner = gateway_runner(
                gateway, project_id=proj.id, session_id="rev-workbench",
                author="human", timeout=900, workspace=proj.path)
            svc = build_headless_service(
                proj.artifacts_dir / "decompiler-cache",
                runner=runner,
                ida_db_dir=proj.artifacts_dir / "decompiler-db",
                ghidra_tmp_dir=proj.artifacts_dir / ".ghidra-tmp",
                # MCP 实时桥：config/mcp.json 逆向域 http server，无配置默认
                # http://127.0.0.1:13337/mcp 懒探活（Ctrl-Alt-M 起插件即亮灯）
                mcp_endpoint=select_mcp_endpoint(_load_mcp_config()),
            )
        app.state.rev_services[proj.id] = svc
        return svc

    # ---------- 逆向工作台辅助（研究轨 rev profile，DESIGN.md §12） ----------

    SAMPLE_MAX_BYTES = 256 * 1024 * 1024
    # 大样本阈值（2026-09-29 用户口径）：>20MB headless 全量导出（自动分析+
    # 全量反编译）耗时可能很久——前端「开始分析」二次确认并建议走 IDA 拉取
    SAMPLE_LARGE_BYTES = 20 * 1024 * 1024
    DEBUGLOG_MAX_BYTES = 16 * 1024 * 1024
    ATTACHMENT_MAX_BYTES = 64 * 1024 * 1024   # 直播间输入行附件随发（2026-09-19）
    PACKER_ENTROPY = 7.2
    _BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
    _WIN_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)")

    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _safe_upload_name(raw: str) -> str:
        """上传文件名清洗：basename + 去非法字符/控制符/NUL + Windows 保留名 + 限长。"""
        name = os.path.basename((raw or "").replace("\\", "/")).strip()
        name = _BAD_NAME_CHARS.sub("_", name).strip(" .")
        if not name or _WIN_RESERVED.match(name):
            raise HTTPException(422, f"非法文件名: {raw!r}")
        if len(name) > 120:
            p = Path(name)
            name = p.stem[: 120 - len(p.suffix)] + p.suffix
        return name

    def _inside(proj: Project, *parts: str) -> Path:
        """项目内相对路径 → resolve；越界 422（防穿越，样本/产物共用）。"""
        base = Path(proj.path).resolve()
        target = (base / Path(*parts)).resolve()
        if not target.is_relative_to(base):
            raise HTTPException(422, f"路径越界: {parts}")
        return target

    def _spool_upload(file: UploadFile, max_bytes: int) -> tuple[tempfile.SpooledTemporaryFile, str, int]:
        """流式落临时文件 + 边写边算 sha256；超限 413、空文件 422。"""
        h = hashlib.sha256()
        size = 0
        spool = tempfile.SpooledTemporaryFile(max_size=1 << 22)
        while True:
            chunk = file.file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                spool.close()
                raise HTTPException(413, f"文件超过 {max_bytes // (1024 * 1024)}MB 上限")
            h.update(chunk)
            spool.write(chunk)
        if size == 0:
            spool.close()
            raise HTTPException(422, "空文件")
        spool.seek(0)
        return spool, h.hexdigest(), size

    def _triage_running(pid: str, sha: str) -> bool:
        return any(
            j["status"] == "running"
            and j["meta"].get("project_id") == pid
            and j["meta"].get("triage_sha") == sha
            for j in app.state.jobs.all_jobs()
        )

    def _run_binary_triage(pid: str, sha: str, rel_path: str) -> dict:
        """零 LLM 后台分诊：headless 全量导出→缓存；无工具结构化降级（不报错）。"""
        from core.tools.decompiler import DECOMPILE_GUIDANCE

        proj = _project(pid)
        bb, svc = proj.bb, _rev_service(proj)
        if not svc.headless_backends():
            payload = {"sha": sha, "reason": "no-tool", "guidance": DECOMPILE_GUIDANCE}
            bb.append_event(pid, "binary.triage_failed", payload, author="human")
            return {"status": "no-tool", **payload}
        try:
            data, info = svc.export_to_cache(str(_inside(proj, rel_path)))
        except Exception as e:  # noqa: BLE001 —— Job 失败也要留黑板痕迹
            bb.append_event(pid, "binary.triage_failed",
                            {"sha": sha, "reason": "export-failed",
                             "error": f"{type(e).__name__}: {e}"[:400]},
                            author="human")
            raise
        funcs = data.get("functions") or []
        sections = data.get("sections") if isinstance(data.get("sections"), list) else []
        imports = data.get("imports") if isinstance(data.get("imports"), dict) else {}
        max_ent = max((s.get("entropy") or 0.0) for s in sections) if sections else None
        packer = max_ent is not None and max_ent > PACKER_ENTROPY
        rel_db = _db_rel(proj, sha)
        asset = bb.find_asset(pid, "binary", sha)
        if asset:
            bb.update_asset_meta(asset["id"], {"triage": {
                "backend": info.get("name"),
                "function_count": len(funcs),
                "packer_suspect": packer,
                "max_entropy": max_ent,
                "import_modules": len(imports),
                "import_count": sum(len(v) for v in imports.values()),
                "db_path": rel_db,
                "triaged_at": _utc_now(),
            }})
        bb.append_event(pid, "binary.triaged",
                        {"sha": sha, "backend": info.get("name"),
                         "function_count": len(funcs), "packer_suspect": packer},
                        author="human")
        return {"status": "ok", "sha": sha, "function_count": len(funcs),
                "backend": info.get("name")}

    def _submit_triage(proj: Project, sha: str, rel_path: str) -> str:
        if _triage_running(proj.id, sha):
            raise HTTPException(409, "该样本正在分诊中")
        return app.state.jobs.submit(
            "binary-triage",
            lambda: _run_binary_triage(proj.id, sha, rel_path),
            meta={"project_id": proj.id, "triage_sha": sha},
        )

    def _rev_job_running(pid: str, sha: str, kind: str) -> bool:
        """同项目同样本的同类 rev Job 防重（triage/writeback/pull-names）。"""
        return any(
            j["status"] == "running" and j["kind"] == kind
            and j["meta"].get("project_id") == pid and j["meta"].get("sha") == sha
            for j in app.state.jobs.all_jobs()
        )

    def _run_binary_writeback(pid: str, sha: str, items: list[dict]) -> dict:
        """func_kb → IDA .i64 写回（零 LLM）。非 ok 状态结构化返回，前端按状态给文案。"""
        proj = _project(pid)
        bb, svc = proj.bb, _rev_service(proj)
        res = svc.writeback(sha, items)
        if res.get("status") == "ok":
            bb.append_event(pid, "binary.annotated",
                            {"sha": sha, "applied": res.get("applied", 0)},
                            author="human")
        return res

    def _submit_writeback(proj: Project, sha: str, items: list[dict]) -> str:
        if _rev_job_running(proj.id, sha, "binary-writeback"):
            raise HTTPException(409, "该样本正在写回中")
        return app.state.jobs.submit(
            "binary-writeback",
            lambda: _run_binary_writeback(proj.id, sha, items),
            meta={"project_id": proj.id, "sha": sha},
        )

    def _run_pull_names(pid: str, sha: str) -> dict:
        """IDA GUI 手改名 → func_kb：库内重导（不删库）→ 纯函数 diff → patch_func 入史。"""
        from core.tools.decompiler import diff_pulled_names

        proj = _project(pid)
        bb, svc = proj.bb, _rev_service(proj)
        res = svc.refresh_db_cache(sha)
        if res.get("status") != "ok":
            return res  # locked / no-db / no-tool / unsupported：无事件，前端提示
        changed = diff_pulled_names(res["data"], bb.list_funcs(pid, sha))
        for ch in changed:
            bb.patch_func(pid, ch["func_id"], name=ch["new_name"], author="ida-pull")
        pairs = [{"address": c["address"], "old_name": c["old_name"],
                  "new_name": c["new_name"]} for c in changed]
        bb.append_event(pid, "binary.names_pulled", {"sha": sha, "changed": pairs},
                        author="human")
        return {"status": "ok", "changed": pairs}

    def _submit_pull_names(proj: Project, sha: str) -> str:
        if _rev_job_running(proj.id, sha, "binary-pull-names"):
            raise HTTPException(409, "该样本正在同步改名中")
        return app.state.jobs.submit(
            "binary-pull-names",
            lambda: _run_pull_names(proj.id, sha),
            meta={"project_id": proj.id, "sha": sha},
        )

    PULL_PAGE = 1000  # list_funcs 每页函数数（每页刷一次进度 + 落一次部分缓存）

    def _run_pull_ida_functions(pid: str, sha: str, progress: dict | None = None,
                                cancel: threading.Event | None = None) -> dict:
        """GUI IDA MCP → 轻量缓存（断点续拉，2026-09-30）：count_funcs 先拿总数
        做进度分母（旧插件无此工具→None，进度退化为无分母）；list_funcs 分页
        遍历（配惰性分页插件，1000/页），每页 progress 更新（job meta 可变 dict
        → 前端轮询可见）+ 部分缓存落盘（左栏渐进长出）；停止/翻页失败保住已拉
        部分（partial 缓存），再点拉取从 next_offset 续传（count_funcs 对账，
        总数变了自动从头）。diff 回拉在完成与停止时都做（IDA 人工名 > AI 名 >
        自动名，自动名绝不覆盖）。"""
        from core.tools.decompiler import MCPBackend, diff_pulled_names

        proj = _project(pid)
        bb, svc = proj.bb, _rev_service(proj)
        if svc.mcp is None or not svc.mcp_online():
            return {"status": "no-mcp",
                    "hint": "未检测到 IDA MCP server——在 IDA 中打开样本后按 "
                            "Ctrl-Alt-M 启动插件（仅连本机 127.0.0.1:13337）"}
        total = svc.mcp.count_funcs()

        # 断点续拉：已有 ida-mcp 部分缓存 → 从 next_offset 继续；总数对账防
        # 错位（IDA 重分析/连了别的库）——不一致时旧部分不可信，自动从头重拉
        functions: list[dict] = []
        offset, restarted = 0, False
        data = svc.read_cached(sha)
        if data is not None:
            meta = data.get("meta") or {}
            if meta.get("source") == "ida-mcp" and meta.get("partial"):
                if total is not None and meta.get("total_functions") != total:
                    restarted = True
                else:
                    functions = list(data.get("functions") or [])
                    offset = int(meta.get("next_offset") or len(functions))

        stopped = False
        while True:
            if cancel is not None and cancel.is_set():
                stopped = True  # 已拉部分上轮已落盘，直接收尾
                break
            pages = svc.mcp.call_tool(MCPBackend.T_LIST_FUNCS, {"queries": [
                {"offset": offset, "count": PULL_PAGE, "filter": ""}]})
            rows = pages[0].get("data") if isinstance(pages, list) and pages \
                and isinstance(pages[0], dict) else None
            if rows is None:
                if offset == 0 and not functions:
                    return {"status": "no-mcp",
                            "hint": "IDA MCP 在线但 list_funcs 不可用"
                                    "（插件版本过旧？）"}
                stopped = True  # 中途翻页失败：保住已拉部分
                break
            for f in rows:
                try:  # vendor Function 是 hex 字符串；坏行跳过不拖垮整次拉取
                    functions.append({"address": int(str(f.get("addr")), 16),
                                      "name": str(f.get("name") or ""),
                                      "size": int(str(f.get("size") or "0x0"), 16)})
                except (ValueError, TypeError):
                    continue
            nxt = pages[0].get("next_offset")
            done = nxt is None or nxt <= offset or len(functions) > 500_000
            binary_name = (bb.find_asset(pid, "binary", sha)
                           or {}).get("meta", {}).get("filename") or ""
            # 每页落盘：partial 缓存（渐进可见 + 断点）；完成页 partial=False
            svc.import_ida_mcp_cache(sha, functions, binary_name=binary_name,
                                     partial=not done, total=total,
                                     next_offset=None if done else int(nxt))
            if progress is not None:
                progress.update(pulled=len(functions), total=total)
            if done:
                break
            offset = int(nxt)

        data = svc.read_cached(sha) or {"functions": functions}
        changed = diff_pulled_names(data, bb.list_funcs(pid, sha))
        for ch in changed:
            bb.patch_func(pid, ch["func_id"], name=ch["new_name"],
                          author="ida-pull")
        pairs = [{"address": c["address"], "old_name": c["old_name"],
                  "new_name": c["new_name"]} for c in changed]
        bb.append_event(pid, "binary.pulled_from_ida",
                        {"sha": sha, "function_count": len(functions),
                         "changed": pairs, "stopped": stopped}, author="human")
        if stopped:
            pos = f"{len(functions)}/{total}" if total is not None \
                else f"{len(functions)}"
            return {"status": "stopped", "sha": sha, "pulled": len(functions),
                    "total": total, "changed": pairs,
                    "hint": f"已停止：已拉 {pos} 个函数（已生效），"
                            "再次拉取将从断点继续"}
        res = {"status": "ok", "sha": sha, "function_count": len(functions),
               "total": total, "changed": pairs}
        if restarted:
            res["hint"] = "IDA 库与上次拉取不一致（函数总数变了），已自动从头重拉"
        return res

    def _submit_pull_ida_functions(proj: Project, sha: str) -> str:
        if _rev_job_running(proj.id, sha, "binary-pull-ida-functions"):
            raise HTTPException(409, "该样本正在从 IDA 拉取中")
        # progress 是可变 dict：塞进 job meta（引用共享），Job 循环里逐页 update
        # → 前端 GET /api/jobs/{id} 轮询自动带出，零新增端点；cancel Event 页间检查
        progress: dict = {"pulled": 0, "total": None}
        cancel = threading.Event()
        app.state.pull_cancel[sha] = cancel
        return app.state.jobs.submit(
            "binary-pull-ida-functions",
            lambda: _run_pull_ida_functions(proj.id, sha, progress, cancel),
            meta={"project_id": proj.id, "sha": sha, "progress": progress},
        )

    def _run_push_names_to_ida(pid: str, sha: str) -> dict:
        """func_kb 有效命名 → GUI IDA（反向，2026-09-29）：AI/人工分析成果批量
        rename 写回当前库（自动名不推）；writeback_items 只改 GUI 内存库不落盘
        ——完成后提示用户在 IDA 里保存（库存活由用户掌控，与 writeback 同纪律）。"""
        from core.tools.decompiler import is_auto_name

        proj = _project(pid)
        bb, svc = proj.bb, _rev_service(proj)
        if svc.mcp is None or not svc.mcp_online():
            return {"status": "no-mcp",
                    "hint": "未检测到 IDA MCP server——在 IDA 中打开样本后按 "
                            "Ctrl-Alt-M 启动插件（仅连本机 127.0.0.1:13337）"}
        items = [{"address": hex(int(f["address"])), "name": f["name"]}
                 for f in bb.list_funcs(pid, sha)
                 if f.get("name") and not is_auto_name(f["name"])]
        if not items:
            return {"status": "ok", "applied": 0,
                    "hint": "func_kb 无有效命名（只有自动名）——无可同步"}
        res = svc.mcp.writeback_items(items)
        if res is None:
            return {"status": "no-mcp", "hint": "IDA MCP 写回失败（离线/拒连）"}
        bb.append_event(pid, "binary.names_pushed",
                        {"sha": sha, "applied": res.get("applied", 0)},
                        author="human")
        return {"status": "ok", "applied": res.get("applied", 0),
                "results": res.get("results") or [],
                "hint": "已写入 IDA 当前库（内存）——请在 IDA 中保存数据库落盘"}

    def _submit_push_names_to_ida(proj: Project, sha: str) -> str:
        if _rev_job_running(proj.id, sha, "binary-push-names"):
            raise HTTPException(409, "该样本正在同步命名中")
        return app.state.jobs.submit(
            "binary-push-names",
            lambda: _run_push_names_to_ida(proj.id, sha),
            meta={"project_id": proj.id, "sha": sha},
        )

    def _db_rel(proj: Project, sha: str) -> str | None:
        """IDA 数据库相对路径（9.x .i64/.idb 按目标位宽，在 decompiler-db/ 下）。"""
        d = Path(proj.artifacts_dir) / "decompiler-db"
        for ext in (".i64", ".idb"):
            p = d / f"{sha}{ext}"
            if p.exists():
                return str(p.relative_to(proj.path)).replace("\\", "/")
        return None

    def _parse_hex_addr(addr: str) -> int:
        try:
            return int(addr, 16)
        except ValueError as e:
            raise HTTPException(422, f"地址需为 hex: {addr}") from e

    def _hex_func_view(row):
        """func_kb 出口视图：address int→hex 字符串（JS Number 无 64 位精度，§9 地址纪律）。

        只补响应不改库内表示；入参 int/hex 兼收（FuncCreateIn 校验器）。
        """
        if isinstance(row, dict) and isinstance(row.get("address"), int):
            return {**row, "address": hex(row["address"])}
        return row

    def _require_cache(proj: Project, sha: str) -> dict:
        data = _rev_service(proj).read_cached(sha)
        if data is None:
            raise HTTPException(409, "样本尚未完成 headless 分诊（缓存缺席）")
        return data

    def _tool_lamp(proj: Project, backend_name: str, cached: bool) -> str:
        """三态：installed=后端可用；cached=仅缓存可读；off。"""
        if any(b.name == backend_name for b in _rev_service(proj).headless_backends()):
            return "installed"
        return "cached" if cached else "off"

    def _task_type_table(pid: str) -> dict[str, str]:
        """项目轨的 task_type 注册表（{type: 默认噪声}，含内置 generic）。"""
        return load_task_types(app.state.packs_root, _project(pid).track)

    def _registered_session_factory(pid: str, exec_llm, plan_llm):
        """开窗工厂：返回即注册进 app.state.agents（编排开窗与人开窗同路径，
        杜绝只在 Orchestrator 内存态存活的孤儿窗——孤儿窗点跑队列 404、暂停失效）。

        existing_session 给定时走 rehydrate（服务重启后从黑板 sessions 行附着，
        不新建行）；角色 yaml 按当盘文件重载，纯内存的暂停快照无法恢复。"""
        proj = _project(pid)
        inventory = app.state.inventory

        def factory(role: str, session_name: str | None = None,
                    existing_session: dict | None = None,
                    max_steps: int | None = None) -> AgentSession:
            from core.tools.decompiler import build_headless_service, gateway_runner

            gateway = ExecutionGateway(bb=proj.bb)
            r = load_expert(app.state.packs_root, role, proj.track)
            # 轨级行为语义（R2，§6.9 mode 退役）：redteam 轨注入红队语义 + ROE 摘要
            # （ROE 未核验齐全=按 pentest 上限兜底+提示补全）；其余轨注入影响证明级上限。
            cfg = proj.bb.get_project(pid)["config"] or {}
            from core.autonomy import roe_complete
            track = proj.track
            roe = cfg.get("redteam_roe") or {}
            mode_prompt = ""
            if track == "redteam" and roe_complete(roe):
                mode_prompt = (
                    "## 行动边界：红队行动（redteam 轨）\n"
                    "本会话在红队 ROE 授权范围内行动：允许主动利用未认领目标（§6.3 第 3 级"
                    "在 ROE 范围内放开），以打穿 mission 判据为目标；仍禁：超出 ROE 目标、"
                    "破坏性毁伤、安全红线（审批/审计照常）。\n"
                    f"- ROE 授权目标: {roe.get('targets', '-')}\n"
                    f"- 时间窗口: {roe.get('window', '-')}\n"
                    f"- 禁止事项: {roe.get('exclusions', '-')}\n"
                    f"- 授权人: {roe.get('approver', '-')}\n")
            elif track == "redteam":
                mode_prompt = (
                    "## 行动边界：红队行动（redteam 轨，ROE 未核验）\n"
                    "ROE 四要素尚未填写齐全，本会话行为按渗透测试上限兜底："
                    "验证上限=影响证明级（如 SQL 注入读敏感表/RCE 一次性回显）；"
                    "禁驻留/持久化/横向/提权推进；请提醒人类在直播间补全 ROE 以解锁红队行动。")
            else:
                mode_prompt = (
                    "## 行动边界：渗透测试（pentest 轨）\n"
                    "验证上限=影响证明级（如 SQL 注入读敏感表/RCE 一次性回显）；"
                    "禁驻留/持久化/横向/提权推进；主动利用未认领目标默认禁止（发现即上报）。")
            # D10 策略顾问项目级配置（2026-09-24）：缺省段补代码默认；三整数
            # 透传 AgentConfig；provider/model 覆写本会话 planner（顾问建议/裁决/
            # 收尾复盘三消费点同时生效）。必须在 factory() 内层——外层会污染
            # Orchestrator 自身的 plan_llm。
            from core.autonomy import ADVISOR_DEFAULTS
            adv = {**ADVISOR_DEFAULTS, **(cfg.get("advisor") or {})}
            session_plan_llm = plan_llm
            if adv.get("provider") and plan_llm is not None:
                try:
                    session_plan_llm = app.state.llm_store.build(
                        adv["provider"], adv.get("model"))
                except Exception as e:  # noqa: BLE001
                    # 供应商后变更（删除/停用/删模型）致坏值 → 静默回退全局 planner，
                    # 绝不开窗失败
                    log.warning(
                        "项目顾问模型覆写构建失败，回退全局 planner: pid=%s %s", pid, e)
                    session_plan_llm = plan_llm
            # 项目 executor 覆写（TRAE 新壳 M3，2026-09-25）：cfg.executor_llm
            # 非空时本项目新开/重附着窗的 executor 按覆写构建，坏值静默回退全局
            # executor（与 advisor 同口径；在跑窗的即时切换走专用 PUT 端点）。
            exec_ov = cfg.get("executor_llm") or {}
            session_exec_llm = exec_llm
            if isinstance(exec_ov, dict) and str(exec_ov.get("provider") or "").strip():
                try:
                    session_exec_llm = app.state.llm_store.build(
                        str(exec_ov["provider"]).strip(),
                        str(exec_ov.get("model") or "") or None)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "项目 executor 覆写构建失败，回退全局 executor: pid=%s %s", pid, e)
                    session_exec_llm = exec_llm
            agent = AgentSession(
                project_id=pid, bb=proj.bb, gateway=gateway,
                llm=session_exec_llm, planner_llm=session_plan_llm,
                enable_sediment=sediment_proposals,
                packs_root=app.state.packs_root,
                # expert-pool M2（§4.4）：capabilities=caps_effective 推导值——
                # 有绑定→专家面（绑定专家 skills 并集∪轨技能的所属包），无绑定→盘上直通；
                # allowed_roles=绑定专家清单（发布链 publish_task 校验数据源）
                track=proj.track,
                capabilities=caps_effective(
                    app.state.packs_root, proj.track, proj.experts,
                    fallback=proj.capabilities),
                role=role,
                allowed_roles=expert_allowed_roles(
                    app.state.packs_root, proj.track, proj.experts),
                session_name=session_name or r.get("name") or role,
                capability_prompt=(inventory.to_prompt() + "\n" + mode_prompt).strip(),
                # 角色 yaml 的 default_noise/tools/max_runtime/max_steps 在
                # AgentSession 内消费（只可能更严）；这里只给全局/动态部分
                # （窗口不设 role 限制：角色 task_types 不再注入，认领无过滤）
                config=AgentConfig(max_steps=max_steps or 200,
                                   owner_tags=proj.bb.owner_tags(pid),
                                   rule_profiles=cfg.get("rule_profiles"),
                                   stuck_after=adv["stuck_after"],
                                   stuck_max_extensions=adv["stuck_max_extensions"],
                                   closing_max_rounds=adv["closing_max_rounds"]),
                artifacts_dir=proj.artifacts_dir,
                existing_session=existing_session,
                campaign=app.state.campaign,
            )
            # 会话 id 就绪后再装配反编译服务（命令经网关审计；IDA 优先、Ghidra 兜底）。
            # mcp_provider：Agent 点查 decompile/xrefs 时按需拉起无窗口 idat+MCP
            # （2026-09-20），空闲自动关、失败一律 None 降级 headless 缓存；
            # 全量概览仍走 headless 导出（红线不破）。
            agent.dispatcher.decompiler = build_headless_service(
                proj.artifacts_dir / "decompiler-cache",
                runner=gateway_runner(gateway, project_id=pid,
                                      session_id=agent.session["id"],
                                      author=agent.session["id"], timeout=900,
                                      workspace=proj.path),
                ida_db_dir=proj.artifacts_dir / "decompiler-db",
                ghidra_tmp_dir=proj.artifacts_dir / ".ghidra-tmp",
                mcp_provider=lambda binary: app.state.ida_mcp_manager.ensure(
                    pid, binary,
                    db_dir=proj.artifacts_dir / "decompiler-db"),
            )
            # F6 内置浏览器：渗透/红队/CTF 轨注入实例池（v0.70：CTF Web 题需真实
            # 浏览器渲染 reCAPTCHA/JS 挑战；轨外不注入 → browser_* no-tool
            # 降级；工具 schema 仍全量下发，同 decompiler 语义，不做动态工具面）
            if proj.track in ("pentest", "redteam", "ctf"):
                agent.dispatcher.browser = app.state.browser_pool
            app.state.agents[agent.session["id"]] = agent
            return agent

        return factory

    def _ensure_agent(pid: str, sid: str) -> AgentSession:
        """取在册会话；内存态缺失（服务重启/历史孤儿窗）时从黑板 sessions 行
        rehydrate 一个同角色 AgentSession 并注册。claimed 任务无快照不续跑，
        已在项目首开时统一 fail(awaiting_human)（§3 重启纪律）。"""
        agent = app.state.agents.get(sid)
        if agent is not None:
            return agent
        proj = _project(pid)
        row = next((s for s in proj.bb.list_sessions(pid) if s["id"] == sid), None)
        if row is None:
            raise HTTPException(404, f"会话不存在: {sid}")
        if row.get("status") == "closed":
            raise HTTPException(404, f"会话已关窗: {sid}")
        exec_llm, plan_llm = _llms()
        agent = _registered_session_factory(pid, exec_llm, plan_llm)(
            row["role"], existing_session=row)
        # 重启后无 worker 线程：running/paused 都是无消费者的陈旧行状态。E8：落盘
        # 快照在手（_resume_state 已载回且 paused=True）→ 保持 paused 可恢复；
        # 无快照回 idle（claimed 任务已在项目首开时统一 fail，§3 重启纪律）。
        if row.get("status") in {"running", "paused", "blocked"}:
            proj.bb.set_session_status(
                sid, "paused" if agent._resume_state is not None else "idle")
        return agent

    # ---------- 项目 ----------

    def _expert_meta_view(view: dict) -> dict:
        """meta 读视图补专家绑定推导值（expert-pool M2，§4.4）：
        "experts" = 绑定清单；"capabilities" = caps_effective（有绑定→专家面推导，
        无绑定→盘上 capabilities 直通）；"capabilities_bound" = 盘上原始绑定包
        （设置页「能力包跟随项目」缺省用——effective 在 _generalist 项目=全部包，
        取 [0] 会错落到 binary）。仅响应层，盘上 meta 不改。"""
        experts = [e for e in (view.get("experts") or []) if str(e).strip()]
        return {**view, "experts": experts,
                "capabilities_bound": list(view.get("capabilities") or []),
                "capabilities": caps_effective(
                    app.state.packs_root, view.get("track") or "ctf", experts,
                    fallback=view.get("capabilities") or [])}

    @app.get("/api/projects")
    def list_projects():
        return [_expert_meta_view(m) for m in store.list_projects()]

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn):
        # 旧客户端只传 domain（pentest/ctf）：透明映射为 track+caps（读兼容写新值）
        sent = body.model_fields_set
        track = body.track if "track" in sent else None
        caps = body.capabilities if "capabilities" in sent else []
        if body.domain:
            mapped = LEGACY_DOMAIN_MAP.get(body.domain)
            if mapped:
                track = track or mapped[0]
                caps = caps or list(mapped[1])
            else:
                track = track or body.domain
        track = track or "ctf"
        # 校验轨/能力包真实存在
        valid_tracks = {t["name"] for t in list_packs(app.state.packs_root, "track")}
        valid_caps = {c["name"] for c in list_packs(app.state.packs_root, "capability")}
        if track not in valid_tracks:
            raise HTTPException(422, f"非法场景轨: {track}（合法: {sorted(valid_tracks)}）")
        bad_caps = sorted(set(caps) - valid_caps)
        if bad_caps:
            raise HTTPException(422, f"非法能力包: {bad_caps}（合法: {sorted(valid_caps)}）")
        # 红队扩展包挂载限制（R3/R4，§6.9.1）：仅 redteam 轨可挂载
        redteam_only = {"shell-c2", "social"}
        off_track = sorted(set(caps) & redteam_only) if track != "redteam" else []
        if off_track:
            raise HTTPException(
                422, f"能力包 {off_track} 仅限 redteam 轨挂载（§6.9.1 合规红线）")
        # 场景档（expert-pool M4a，§4.8/D6）：档提供组队预设与五件套缺省，
        # 请求体显式值优先；物化进项目配置后即弃（模板升级不影响存量项目）。
        profile = None
        if body.profile:
            profile = load_profile(app.state.packs_root, track, body.profile.strip())
            if profile is None:
                avail = [p["id"] for p in load_track_profiles(app.state.packs_root, track)]
                raise HTTPException(
                    422, f"场景档不存在: {body.profile}（{track} 轨内置: {avail}）")
            bv = profile.get("board_view")
            if bv and bv not in BOARD_VIEWS:
                raise HTTPException(422, f"场景档 board_view 非法: {bv}（合法: {BOARD_VIEWS}）")
        # 专家绑定校验（expert-pool M2，§4.4）：须在池内且可服务该轨（422）；
        # 选档未显式传组队时用档内预设兜底（M3 前端总是提交定稿清单）
        experts = list(body.experts) if body.experts else \
            [str(e) for e in (profile.get("experts") or [])] if profile else []
        bad_experts = [e for e in experts
                       if not expert_exists(app.state.packs_root, str(e).strip(), track)]
        if bad_experts:
            pool = list_experts(app.state.packs_root, track)
            raise HTTPException(
                422, f"非法专家: {sorted(set(bad_experts))}"
                     f"（{track} 轨可用: {pool}）")
        # 知识继承源预检（M4b）：先 404 再建项目，避免建了项目才发现源不存在
        if body.inherit_from:
            try:
                store.open_project(body.inherit_from.strip())
            except FileNotFoundError as e:
                raise HTTPException(404, str(e))
        # 五件套物化（D6）：rule_profiles/board_view 立即生效，playbook/artifacts/
        # knowledge（暂未对接机制）随 config.profile 快照留档不丢
        cfg = dict(body.config)
        if profile is not None:
            cfg.setdefault("profile", {**profile_snapshot(profile),
                                       "materialized_at": _utc_now()})
            owners, rating = profile.get("rule_profiles_owners"), profile.get("rule_profiles_rating")
            if "rule_profiles" not in body.config and (owners is not None or rating is not None):
                cfg["rule_profiles"] = {k: v for k, v in
                                        (("owners", owners), ("rating", rating)) if v is not None}
            if profile.get("board_view"):
                cfg.setdefault("board_view", {"default": profile["board_view"]})
        proj = store.create_project(body.name, track, caps, cfg, experts=experts)
        app.state.projects[proj.id] = proj
        # 分阶段工作流（M1，D3）：轨有阶段剧本 → 创建即登记初始阶段（不发剧本
        # 任务——mission/目标商议前不发静态任务，首发随显式流转触发；publish
        # ≠起跑，起跑仍按自主档/人工）
        _book = phases_mod.load_track_phases(app.state.packs_root, proj.track,
                                             proj.meta.get("config") or {})
        if _book:
            try:
                phases_mod.enter_phase(proj, phases_mod.default_phase_id(_book),
                                       by="system", packs_root=app.state.packs_root,
                                       publish=False,
                                       reason="项目创建进入初始阶段")
            except Exception:  # noqa: BLE001 —— 初始登记失败不回滚建项
                log.exception("初始阶段进入失败 pid=%s", proj.id)
        # 知识继承（M4b）：创建后复制（只增不覆盖）；失败不回滚建项，响应里带错误
        out = _expert_meta_view(proj.view_meta)
        if body.inherit_from:
            try:
                out["inherited"] = store.inherit_knowledge(body.inherit_from.strip(), proj)
            except (ValueError, OSError, sqlite3.Error) as e:
                log.warning("知识继承失败 %s -> %s: %s", body.inherit_from, proj.id, e)
                out["inherit_error"] = str(e)
        return out

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        proj = _project(pid)
        # 僵尸 running 清扫（与任务侧 estranged「重启急停」同构）：进程重启后
        # 执行轮次随旧进程消失，DB status=running 残留会让工作台永久「执行中」
        # ——新进程 chat_running 为空集，扫到的 running 必是僵尸，归位+落中断
        # 消息（无僵尸时 no-op，小表查询开销可忽略）
        from core.chat import store as chat_store
        recovered = chat_store.recover_running_threads(proj.bb)
        if recovered:
            log.info("chat 僵尸线程清扫: %s", recovered)
        tq = TaskQueue(proj.bb)
        tasks = tq.list_tasks(pid)
        stats: dict[str, int] = {}
        for t in tasks:
            stats[t["status"]] = stats.get(t["status"], 0) + 1
        # C1：awaiting_human 任务计数——供顶栏铃铛红点（只计数，不混 approval 表）
        stats["awaiting_human"] = sum(
            1 for t in tasks
            if t["status"] == "failed" and t.get("blocked_reason") == "awaiting_human")
        usage = autonomy.usage_view(proj.bb, pid)
        # 批 5（§6.8）：L2 链状态给直播间；estranged=DB 活但本进程无标记（重启急停）
        cst = orch_state.load_or_create(proj.bb, pid)
        usage["chain"] = {
            "active": bool(cst["chain_active"]),
            "ticks": int(cst["chain_ticks"]),
            "auto_ticks_total": int(cst["auto_ticks_total"]),
            "estranged": bool(cst["chain_active"]) and pid not in app.state.active_chains,
        }
        # 会话 UI 风格（trae 视图 2026-09-28）：config.ui_style 透出给前端切换钮
        usage["ui_style"] = str((proj.view_meta.get("config") or {}).get("ui_style") or "claude")
        # 编排 tick 运行中（trae 视图「正在规划下一步」状态行判定源）
        usage["orch_running"] = any(
            j["kind"] == "orchestrator-tick" and j["status"] == "running"
            for j in app.state.jobs.all_jobs()
            if j.get("meta", {}).get("project_id") == pid)
        return {**_expert_meta_view(proj.view_meta), "task_stats": stats,
                "findings": len(proj.bb.list_findings(pid)),
                "assets": len(proj.bb.list_assets(pid)),
                "usage": usage,
                "capability": app.state.inventory.to_dict()}

    @app.patch("/api/projects/{pid}/config")
    def patch_project_config(pid: str, body: ConfigPatchIn):
        """改项目配置（本批消费 autonomy：L0/L1/L2、paused、cap、预算；§6.8）。
        project.json + 黑板 projects 行双写；非法 autonomy 422；闸门下次重读即生效。"""
        proj = _project(pid)
        try:
            meta = store.update_config(pid, body.config, project=proj)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return meta

    @app.patch("/api/projects/{pid}/experts")
    def patch_project_experts(pid: str, body: ExpertsPatchIn):
        """换将（expert-pool M2，§4.4）：重写绑定专家清单，下轮会话构造即生效
        （构造链实时读 meta）。校验专家在池内且可服务项目轨（422）；空清单=解绑
        恢复存量直通态。响应带推导后的 meta 视图（capabilities=caps_effective）。"""
        proj = _project(pid)
        bad = [e for e in body.experts
               if not expert_exists(app.state.packs_root, str(e).strip(), proj.track)]
        if bad:
            pool = list_experts(app.state.packs_root, proj.track)
            raise HTTPException(
                422, f"非法专家: {sorted(set(bad))}（{proj.track} 轨可用: {pool}）")
        return _expert_meta_view(store.update_experts(pid, body.experts, project=proj))

    def _project_busy(pid: str) -> str | None:
        """项目是否在运行中（删除前置检查）。忙则返回原因，空闲返回 None。"""
        for j in app.state.jobs.all_jobs():
            if j["status"] == "running" and j["meta"].get("project_id") == pid:
                return "有 Agent/编排任务正在运行"
        proj = _project(pid)
        TaskQueue(proj.bb).expire_leases()  # 过期租约先回收，避免误判
        n = proj.bb.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND status='claimed'",
            (pid,)).fetchone()["n"]
        return f"仍有 {n} 个任务被认领（先取消或等会话收尾）" if n else None

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        """回收站式删除（DESIGN.md §5.3）：整目录移入 workspaces/.trash/，不物理删除。"""
        proj = _project(pid)
        if pid in app.state.projects_closing:
            raise HTTPException(409, "项目正在删除中")
        reason = _project_busy(pid)
        if reason:
            raise HTTPException(409, f"项目删除被拒绝：{reason}")
        app.state.projects_closing.add(pid)
        try:
            # 摘缓存后由 store.delete_project 统一 close_all()+close()（双层闸门）：
            # 之后在飞轮询/WS tick 既拿不到新实例（_project 409），也无法用旧 proj
            # 引用重开黑板（Blackboard/Project 双层 BlackboardClosedError）——
            # 这是 Windows 下 db 文件不被重新锁死、rename 成功的关键。
            app.state.projects.pop(pid, None)
            # F6：先焚毁浏览器实例（chromium 占用 profile 目录会锁死 Windows 删除）
            app.state.browser_pool.close_project(pid)
            try:
                trash_path = store.delete_project(pid, project=proj)
            except FileNotFoundError:
                # 并发删除（双击/双标签）已先一步移走目录
                raise HTTPException(404, f"项目不存在: {pid}") from None
            except PermissionError as e:  # 重试耗尽仍有句柄（外部进程占用）
                raise HTTPException(
                    422,
                    "项目目录被占用，无法移入回收站；请关闭该项目的所有页面标签后重试: "
                    f"{e}") from e
            return {"project_id": pid, "status": "trashed", "trash_path": str(trash_path)}
        finally:
            app.state.projects_closing.discard(pid)

    # ---------- 内置浏览器（F6，DESIGN §7：抓包/人类驱动/重发/爆破） ----------

    class BrowserNavIn(BaseModel):
        url: str

    class BrowserActionIn(BaseModel):
        action: Literal["click", "type", "back"]
        selector: str | None = None
        text: str | None = None
        x: float | None = None
        y: float | None = None

    class ReplayIn(BaseModel):
        capture_id: int | None = None
        raw: str | None = None             # 原始请求报文（F6-v3：与 capture_id 二选一）

    class IntruderIn(BaseModel):
        template: dict                    # {method,url,headers,body}，含 §POS§ 标记
        payloads: list[dict]              # [{position,type:"list"|"range",values|start/stop/step}]
        concurrency: int = 5
        rate_per_sec: float = 10.0
        max_requests: int | None = None

    def _browser_inst(pid: str):
        """浏览器实例（缺 playwright → 503 结构化，不 500）。"""
        if not browser_available():
            raise HTTPException(
                503, 'playwright 未安装：请人工执行 pip install -e ".[browser]" '
                     '&& playwright install chromium 后重启平台')
        return app.state.browser_pool.get_instance(pid)

    @staticmethod
    def _browser_http_error(e: BrowserError) -> HTTPException:
        msg = str(e)
        code = 409 if "上限" in msg else 422
        return HTTPException(code, msg)

    @app.get("/api/browser/status")
    def browser_status():
        """依赖探测 + 实例概况（全程 try/except，任何异常按未安装返回，不 500）。"""
        try:
            installed = browser_available()
            return {
                "playwright_installed": installed,
                "chromium_installed": chromium_available() if installed else False,
                "install_cmd": 'pip install -e ".[browser]" && playwright install chromium',
                "instances": app.state.browser_pool.status(),
            }
        except Exception:  # noqa: BLE001
            return {"playwright_installed": False, "chromium_installed": False,
                    "install_cmd": 'pip install -e ".[browser]" && playwright install chromium',
                    "instances": []}

    @app.get("/api/projects/{pid}/browser/state")
    def browser_state(pid: str):
        proj = _project(pid)
        inst = app.state.browser_pool.get_instance(pid)
        return {**inst.status(),
                "playwright_installed": browser_available(),
                "track": proj.track}

    @app.get("/api/projects/{pid}/browser/screenshot")
    def browser_screenshot(pid: str):
        inst = _browser_inst(pid)
        try:
            sid = inst.ensure_human_session()["sid"]
            png = inst.screenshot(sid)
        except BrowserError as e:
            raise _browser_http_error(e) from e
        import base64 as _b64
        return {"png": _b64.b64encode(png).decode("ascii"), "ts": time.time()}

    @app.post("/api/projects/{pid}/browser/navigate")
    def browser_navigate(pid: str, body: BrowserNavIn):
        """人类导航（隐式会话 human-main；与 AI 同池同白名单；拒绝 422 带 host
        供前端一键登记资产）。"""
        from core.browser.policy import check_target
        proj = _project(pid)
        inst = _browser_inst(pid)
        verdict = check_target(proj.bb, pid, body.url,
                               domain_scope=app.state.browser_pool.config.domain_scope)
        if not verdict.allowed:
            raise HTTPException(422, detail={
                "reason": verdict.reason, "host": verdict.host,
                "asset_missing": True})
        try:
            sid = inst.ensure_human_session()["sid"]
            return inst.navigate(sid, body.url)
        except BrowserError as e:
            raise _browser_http_error(e) from e

    @app.post("/api/projects/{pid}/browser/action")
    def browser_action(pid: str, body: BrowserActionIn):
        inst = _browser_inst(pid)
        try:
            sid = inst.ensure_human_session()["sid"]
            return inst.act(sid, body.action, selector=body.selector,
                            text=body.text, x=body.x, y=body.y)
        except BrowserError as e:
            raise _browser_http_error(e) from e

    @app.get("/api/projects/{pid}/browser/history")
    def browser_history(pid: str, since_id: int = 0, limit: int = 100,
                        batch_id: str | None = None, source: str | None = None,
                        session_id: str | None = None):
        return _project(pid).bb.list_http_history(
            pid, since_id=since_id, limit=limit, batch_id=batch_id,
            source=source, session_id=session_id)

    @app.get("/api/projects/{pid}/browser/history/{row_id}")
    def browser_history_row(pid: str, row_id: int):
        row = _project(pid).bb.get_http_history(pid, row_id)
        if row is None:
            raise HTTPException(404, f"抓包记录不存在: {row_id}")
        return row

    @app.delete("/api/projects/{pid}/browser/history")
    def browser_history_clear(pid: str, batch_id: str | None = None):
        n = _project(pid).bb.clear_http_history(pid, batch_id=batch_id)
        return {"removed": n}

    @app.post("/api/projects/{pid}/browser/replay", status_code=202)
    def browser_replay(pid: str, body: ReplayIn):
        """重发（人类 UI；AI 无发起入口）。原始报文 raw 或 capture_id 模板。
        Job 化返回 job_id 轮询。"""
        proj = _project(pid)
        client = ReplayClient(proj.bb, config=app.state.browser_pool.config)

        def _run():
            return client.replay(pid, capture_id=body.capture_id,
                                 raw=body.raw, author="human")

        job_id = app.state.jobs.submit("browser-replay", _run,
                                       meta={"project_id": pid})
        return {"job_id": job_id}

    @app.post("/api/projects/{pid}/browser/intruder", status_code=202)
    def browser_intruder(pid: str, body: IntruderIn):
        """爆破（**人类 UI 专属**——Agent 无任何发起入口，红线见 DESIGN §7）。
        服务端生成 batch_id + stop 事件；结果逐请求入 http_history 按 batch 拉取。"""
        proj = _project(pid)
        batch_id = f"in-{uuid.uuid4().hex[:12]}"
        stop = threading.Event()
        app.state.intruder_runs[batch_id] = stop
        intruder = Intruder(proj.bb, config=app.state.browser_pool.config)
        cfg = app.state.browser_pool.config

        def _run():
            try:
                return intruder.run(pid, body.template, body.payloads,
                                    batch_id=batch_id,
                                    concurrency=body.concurrency,
                                    rate_per_sec=body.rate_per_sec,
                                    max_requests=body.max_requests,
                                    stop_event=stop, author="human")
            finally:
                app.state.intruder_runs.pop(batch_id, None)

        job_id = app.state.jobs.submit(
            "browser-intruder", _run,
            meta={"project_id": pid, "batch_id": batch_id})
        return {"job_id": job_id, "batch_id": batch_id,
                "max_concurrency": cfg.intruder_max_concurrency}

    @app.post("/api/projects/{pid}/browser/intruder/{batch_id}/stop")
    def browser_intruder_stop(pid: str, batch_id: str):
        stop = app.state.intruder_runs.get(batch_id)
        if stop is None:
            raise HTTPException(404, f"爆破批次不在运行中: {batch_id}")
        stop.set()
        return {"stopped": True}

    # ---------- F6-v3 拦截（仅人工隐式会话 human-main 流量可挂起裁决） ----------

    @app.get("/api/projects/{pid}/browser/intercept")
    def browser_intercept(pid: str):
        """挂起包快照 + 两开关状态（2s 轮询数据源）。"""
        return _browser_inst(pid)._intercept.snapshot()

    class InterceptToggleIn(BaseModel):
        direction: Literal["request", "response"]
        enabled: bool

    @app.post("/api/projects/{pid}/browser/intercept/toggle")
    def browser_intercept_toggle(pid: str, body: InterceptToggleIn):
        """翻转拦截开关；关闭时该方向全部挂起包自动放行原文（hub 内处理）。"""
        hub = _browser_inst(pid)._intercept
        hub.toggle(body.direction, body.enabled)
        return hub.snapshot()

    class InterceptDecideIn(BaseModel):
        action: Literal["forward", "drop"]
        raw: str | None = None    # 放行（改后）：完整报文；缺省=放行原文

    @app.post("/api/projects/{pid}/browser/intercept/{hold_id}/decide")
    def browser_intercept_decide(pid: str, hold_id: str, body: InterceptDecideIn):
        """裁决一个挂起包。顺序：hub 取 hold → raw 解析 → 改后 URL 过
        check_target → hub.decide（resolve 回 route 协程）。挂起包继续等的
        失败（解析错/目标未登记）以 422 返回，可修正后重提。"""
        from core.browser.httpmsg import parse_raw_request, parse_raw_response
        from core.browser.policy import check_target
        inst = _browser_inst(pid)
        hub = inst._intercept
        # 锁内取（不 pop——裁决失败要继续等）；KeyError→404（已裁决/已超时同理）
        hold = next((h for h in hub.snapshot()["pending"]
                     if h["hold_id"] == hold_id), None)
        if hold is None:
            raise HTTPException(404, f"拦截包不存在或已裁决: {hold_id}")
        mods = None
        if body.action == "forward" and body.raw is not None and body.raw.strip():
            if not hold["editable"]:
                raise HTTPException(422, "二进制 body 不可编辑（只能放行原文或丢弃）")
            try:
                if hold["direction"] == "request":
                    parsed = parse_raw_request(body.raw, base_url=hold["url"])
                    mods = {"method": parsed["method"], "url": parsed["url"],
                            "headers": parsed["headers"], "body": parsed["body"]}
                else:
                    parsed = parse_raw_response(body.raw)
                    mods = {"status": parsed["status"],
                            "headers": parsed["headers"], "body": parsed["body"]}
            except ValueError as e:
                raise HTTPException(422, f"报文解析失败: {e}") from e
            # 改后 URL 变化 → 过白名单（未登记 422 asset_missing，挂起包继续等）
            if hold["direction"] == "request" and parsed["url"] != hold["url"]:
                proj = _project(pid)
                verdict = check_target(
                    proj.bb, pid, parsed["url"],
                    domain_scope=app.state.browser_pool.config.domain_scope)
                if not verdict.allowed:
                    raise HTTPException(422, detail={
                        "reason": verdict.reason, "host": verdict.host,
                        "asset_missing": True})
        try:
            hub.decide(hold_id, body.action, mods)
        except KeyError as e:
            raise HTTPException(404, f"拦截包不存在或已裁决: {hold_id}") from e
        return {"decided": True, "hold_id": hold_id, "action": body.action}


    # ---------- 黑板（读开放 / 写走单一入口） ----------

    @app.get("/api/projects/{pid}/findings")
    def list_findings(pid: str, target_asset_id: str | None = None,
                      min_severity: str | None = None, verified_only: bool = False,
                      category: str | None = None):
        return _project(pid).bb.list_findings(
            pid, target_asset_id=target_asset_id, min_severity=min_severity,
            verified_only=verified_only, category=category)

    @app.post("/api/projects/{pid}/findings", status_code=201)
    def add_finding(pid: str, body: FindingIn):
        proj = _project(pid)
        if body.target_asset_id:
            owned = proj.bb.conn.execute(
                "SELECT 1 FROM assets WHERE id=? AND project_id=?",
                (body.target_asset_id, pid)).fetchone()
            if not owned:
                raise HTTPException(422, f"资产不存在于本项目: {body.target_asset_id}")
        try:
            return proj.bb.add_finding(
                pid, body.vuln_class, body.title, target_asset_id=body.target_asset_id,
                severity=body.severity, status=body.status, evidence=body.evidence,
                dedup_key=body.dedup_key, author="human", rating_basis=body.rating_basis,
                impact=body.impact, remediation=body.remediation,
                category=body.category, track=proj.track)
        except ValueError as e:  # relates_to 悬空/跨项目等（E0）
            raise HTTPException(422, str(e)) from e

    @app.get("/api/projects/{pid}/assets")
    def list_assets(pid: str, type: str | None = None, status: str | None = None,
                    tag: str | None = None):
        bb = _project(pid).bb
        rows = bb.list_assets(pid, type_=type, status=status, tag=tag)
        # effective 派生须基于全量树：带过滤参数时子资产可能不在 rows，
        # 用全量资产算好后按 rows 顺序挂回（binary 等非 coverage 类型原样返回）
        base = rows if not (type or status or tag) else bb.list_assets(pid)
        decorated = {a["id"]: a for a in
                     attach_effective_status(base, bb.list_findings(pid))}
        return [decorated[a["id"]] for a in rows if a["id"] in decorated]

    @app.post("/api/projects/{pid}/assets", status_code=201)
    def add_asset(pid: str, body: AssetIn):
        """人工登记资产（E6 统一入口）：与 Agent bb_add_asset 同走 register_asset——
        类型自动识别/去重合并（修人工路径同值插重复行）/DNS 挂载/主域名标记。"""
        try:
            return register_asset(_project(pid).bb, pid, body.value, type_=body.type,
                                  parent_id=body.parent_id, meta=body.meta,
                                  author="human")
        except ValueError as e:
            raise HTTPException(422, str(e))

    # ---------- 网络空间测绘（cyberspace-mapping M2，2026-09-23） ----------

    @app.get("/api/fofa/config")
    def get_fofa_config():
        cfg = fofa_mod.load_fofa_config(FOFA_CONFIG_PATH)
        key = str(cfg.get("key") or "")
        return {"base_url": cfg.get("base_url") or fofa_mod.DEFAULT_BASE_URL,
                "key": fofa_mod.mask_key(key), "key_set": bool(key.strip())}

    @app.put("/api/fofa/config")
    def put_fofa_config(body: FofaConfigIn):
        cfg = fofa_mod.load_fofa_config(FOFA_CONFIG_PATH)
        if body.base_url is not None:
            cfg["base_url"] = body.base_url.strip() or fofa_mod.DEFAULT_BASE_URL
        if body.key:  # 空串/缺省 = 不修改（防回显脱敏值误覆盖真 key）
            cfg["key"] = body.key.strip()
        fofa_mod.save_fofa_config(FOFA_CONFIG_PATH, cfg)
        return get_fofa_config()

    @app.post("/api/fofa/test")
    def test_fofa():
        """测试连接：info_my 免费不耗配额。失败不抛（200 + ok:false），
        错误文案直接可展示。"""
        client = fofa_mod.FofaClient.from_config(FOFA_CONFIG_PATH)
        if not client.configured:
            raise HTTPException(400, "FOFA 未配置 key——先填入并保存")
        try:
            info = client.info_my()
        except fofa_mod.FofaError as e:
            return {"ok": False, "error": str(e), "base_url": client.base_url}
        return {"ok": True, "remain": info["remain"], "expire": info["expire"],
                "base_url": client.base_url}

    # ---------- FOFA 查询历史（2026-09-28：查询结果持久化 + 历史即保持） ----------
    # 动因：查询结果此前仅存前端组件局部 state，切导航/切页签即丢且后端零持久化。
    # 现查询成功即落 <项目>/fofa_history/<id>.json（含全量 rows），前端面板挂载时
    # 自动恢复最近一次——切页/刷新都不丢；历史可单删/清空，删除=删文件（连带物理
    # 保存）。保存尽力而为（失败不阻断查询返回，配额照扣）。

    _FOFA_HIST_MAX = 50  # FIFO 上限：文件名时间前缀天然有序，超限淘汰最旧

    def _fofa_annotate_rows(proj, raw_rows: list[dict]) -> list[dict]:
        """existing 三锚点标注（domain/service/host 是否已在黑板）——search 与
        历史恢复共用：历史里存原始 rows，恢复时现算标注（资产可能已导入，
        灰显状态要新鲜）。锚点镜像 import_assets 登记语义（2026-09-29 精确化）：
        按 clean_host(host or domain)（=导入 _host_of 同口径）算实际会被登记的
        值——是域名就精确查该子域（只入过根域不再灰显子域行），是 IP 归
        service/host 锚点不查 domain。"""
        rows = []
        for row in raw_rows:
            ip = str(row.get("ip") or "").strip()
            port = str(row.get("port") or "").strip()
            host = str(row.get("host") or "").strip()
            domain = str(row.get("domain") or "").strip()
            h = clean_host(host or domain)
            dom = h if h and not _is_ip(h) else ""
            # service 锚点镜像导入分支：ip:port，缺 ip 用域名 host:port
            svc = (f"{ip}:{port}" if ip and port
                   else (f"{h}:{port}" if port and h and not _is_ip(h) else ""))
            rows.append({**row, "existing": {
                "domain": bool(dom and proj.bb.find_asset(proj.id, "domain", dom)),
                "service": bool(svc and proj.bb.find_asset(proj.id, "service", svc)),
                "host": bool(ip and proj.bb.find_asset(proj.id, "host", ip)),
            }})
        return rows

    def _fofa_hist_save(proj, query: str, size: int, total: int,
                        raw_rows: list[dict]) -> dict | None:
        """查询成功后落历史（尽力而为，任何异常返回 None 不上抛）。"""
        try:
            d = Path(proj.path) / "fofa_history"
            d.mkdir(parents=True, exist_ok=True)
            hid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            rec = {"id": hid, "query": query, "size": size, "total": total,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "rows": raw_rows}
            (d / f"{hid}.json").write_text(
                json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            # FIFO 按 mtime 排序（hid 时间前缀秒级，同秒多条时文件名字典序≠写入序）
            files = sorted(d.glob("*.json"),
                           key=lambda f: f.stat().st_mtime)
            for f in files[:-_FOFA_HIST_MAX]:
                f.unlink(missing_ok=True)
            return {k: rec[k] for k in ("id", "query", "size", "total", "ts")}
        except Exception:
            log.exception("FOFA 查询历史保存失败（不影响查询返回）")
            return None

    def _fofa_hist_path(proj, hid: str) -> Path:
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", hid or ""):
            raise HTTPException(422, "历史 id 非法")
        return Path(proj.path) / "fofa_history" / f"{hid}.json"

    @app.get("/api/projects/{pid}/fofa/history")
    def fofa_history_list(pid: str):
        """轻量列表（不含 rows，按时间倒序）——面板挂载拉取，最近一条用于自动恢复。"""
        proj = _project(pid)
        d = Path(proj.path) / "fofa_history"
        items = []
        if d.is_dir():
            # mtime 倒序（最新在前；文件名时间前缀秒级，同秒多条时不可靠）
            for f in sorted(d.glob("*.json"),
                            key=lambda f: f.stat().st_mtime, reverse=True):
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                    items.append({k: rec[k] for k in ("id", "query", "size", "total", "ts")})
                except (OSError, ValueError, KeyError):
                    continue  # 损坏记录跳过，不阻塞列表
        return {"items": items}

    @app.get("/api/projects/{pid}/fofa/history/{hid}")
    def fofa_history_get(pid: str, hid: str):
        """单条全量（恢复用）：rows 现算 existing 标注。"""
        proj = _project(pid)
        f = _fofa_hist_path(proj, hid)
        if not f.is_file():
            raise HTTPException(404, "历史记录不存在")
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HTTPException(500, f"历史记录损坏: {e}") from e
        return {**rec, "rows": _fofa_annotate_rows(proj, rec.get("rows") or [])}

    @app.delete("/api/projects/{pid}/fofa/history/{hid}")
    def fofa_history_delete(pid: str, hid: str):
        """删单条记录（连带查询结果物理文件）。"""
        proj = _project(pid)
        f = _fofa_hist_path(proj, hid)
        if f.is_file():
            f.unlink(missing_ok=True)
        return {"deleted": hid}

    @app.delete("/api/projects/{pid}/fofa/history")
    def fofa_history_clear(pid: str):
        """清空全部历史（删整个目录）。"""
        proj = _project(pid)
        shutil.rmtree(Path(proj.path) / "fofa_history", ignore_errors=True)
        return {"cleared": True}

    @app.post("/api/projects/{pid}/fofa/search")
    def fofa_search(pid: str, body: FofaSearchIn):
        """FOFA 查询（消耗等量配额）：结果逐行标注 existing（domain/service/host
        三锚点是否已在黑板），前端灰显免重复导入。错误分流：配额耗尽 429（必须
        立即停）、key/base url 配置类 400、其余中转错误 502。成功后落查询历史
        （历史即保持：切页/刷新由前端从历史自动恢复）。"""
        proj = _project(pid)
        client = fofa_mod.FofaClient.from_config(FOFA_CONFIG_PATH)
        if not client.configured:
            raise HTTPException(400, "FOFA 未配置 key——先在「网络空间测绘」设置里填入")
        q = body.query.strip()
        if not q:
            raise HTTPException(422, "查询语句为空")
        try:
            res = client.search(q, size=body.size, page=body.page)
        except fofa_mod.QuotaExhausted as e:
            raise HTTPException(429, str(e)) from e
        except (fofa_mod.ConfigError, fofa_mod.AuthError) as e:
            raise HTTPException(400, str(e)) from e
        except fofa_mod.FofaError as e:
            raise HTTPException(502, str(e)) from e
        # 查询入口统一清洗 host（clean_host 幂等：剥 scheme/「:端口」尾）——
        # FOFA 的 host 字段常带「https://x」「x:8080」脏形态，清洗后展示/灰显/
        # 落历史/导入四处同口径；存量旧历史文件不回写（恢复原样展示，导入链自清洗）
        clean_rows = [{**r, "host": clean_host(str(r.get("host") or ""))}
                      for r in res["rows"]]
        rows = _fofa_annotate_rows(proj, clean_rows)
        saved = _fofa_hist_save(proj, q, body.size, res["total"], clean_rows)
        return {**res, "rows": rows, "history_id": (saved or {}).get("id")}

    @app.post("/api/projects/{pid}/assets/import/preview")
    def asset_import_preview(pid: str, file: UploadFile = File(...)):
        """导入预览（multipart）：解析 + 列映射嗅探建议 + 全量行回传（前端内存
        持有，确认导入时随 mapping 回传——服务端不存文件状态）。"""
        _project(pid)
        suffix = Path(file.filename or "").suffix
        data = file.file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "文件过大（>10MB）")
        try:
            parsed = assetimport.parse_table(data, suffix)
        except assetimport.XlsxUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"filename": file.filename, "header": parsed["header"],
                "columns": parsed["columns"], "rows": parsed["rows"],
                "total_rows": parsed["total_rows"], "truncated": parsed["truncated"]}

    @app.post("/api/projects/{pid}/assets/import")
    def asset_import(pid: str, body: AssetImportIn):
        """批量导入（register_asset 单一入口 quiet=True，整批单条
        asset.imported 汇总事件）：mapping 给出 → 表格二维数组先归一化；
        缺省 → rows 为已归一化 dict 行（FOFA 勾选/手工 JSON）。"""
        proj = _project(pid)
        source = (body.source or "").strip().lower()
        if source not in ("xlsx", "csv", "fofa", "manual"):
            raise HTTPException(422, f"source 非法: {source}（限 xlsx/csv/fofa/manual）")
        if len(body.rows) > assetimport.MAX_IMPORT_ROWS:
            raise HTTPException(422,
                                f"单批导入超上限（>{assetimport.MAX_IMPORT_ROWS} 行）")
        skipped = 0
        if body.mapping is not None:
            try:
                norm_rows, skipped = assetimport.normalize_rows(
                    body.rows, body.mapping)
            except (TypeError, KeyError, ValueError) as e:
                raise HTTPException(422, f"行格式无法归一化: {e}") from e
        else:
            norm_rows = [r for r in body.rows if isinstance(r, dict)]
            if len(norm_rows) != len(body.rows):
                raise HTTPException(422, "rows 含非对象行（dict 形态导入需逐行对象）")
        if not norm_rows:
            raise HTTPException(422, "没有可导入的行（全空或映射后为空）")
        summary = import_assets(proj.bb, pid, norm_rows, source, author="human")
        summary["skipped_parse"] = skipped
        return summary

    @app.get("/api/projects/{pid}/artifacts/content")
    def artifact_content(pid: str, ref: str):
        """按 artifact id 或项目内相对路径读产物文本内容（只读；WebUI POC 弹窗用）。

        解析顺序：artifacts 表按 id → 表按 path → 裸路径（旧数据 evidence.poc_artifact
        只有路径字符串）。防穿越：resolve 后必须仍落在项目目录内；debug-log 上限
        4MB（动态验证日志），其余 256KB。
        """
        proj = _project(pid)
        row = proj.bb.conn.execute(
            "SELECT id, path, kind, sha256 FROM artifacts WHERE (id=? OR path=?) AND project_id=?",
            (ref, ref, pid)).fetchone()
        if row:
            rel, kind, sha, art_id = row["path"], row["kind"], row["sha256"], row["id"]
        else:
            rel, kind, sha, art_id = ref, "file", "", None
        base = Path(proj.path).resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base):
            raise HTTPException(422, f"产物路径越界: {rel}")
        if not target.is_file():
            raise HTTPException(404, f"产物文件不存在: {rel}")
        # debug-log（x64dbg 断点日志回流）放宽到 4MB，其余产物 256KB
        max_bytes = 4 * 1024 * 1024 if kind == "debug-log" else 256 * 1024
        if target.stat().st_size > max_bytes:
            raise HTTPException(413, f"产物过大（>{max_bytes // 1024}KB），请到项目目录查看: {rel}")
        return {
            "id": art_id, "path": rel, "kind": kind, "sha256": sha,
            "content": target.read_text(encoding="utf-8", errors="replace"),
        }

    @app.patch("/api/assets/{asset_id}")
    def patch_asset(asset_id: str, body: AssetPatchIn):
        """资产更新（改挂父行 / 合并 meta，§5.2）：upsert 键含 parent_id，补挂必须走更新。"""
        bb = _project(_pid_of_asset(asset_id)).bb
        changes = body.model_dump(exclude_unset=True)
        try:
            if "parent_id" in changes:  # 传 null = 摘挂为根行
                bb.set_asset_parent(asset_id, body.parent_id)
            if "meta" in changes:       # 传 {} = 无操作，传键值 = 合并
                bb.update_asset_meta(asset_id, body.meta or {})
        except ValueError as e:
            raise HTTPException(422, str(e))
        asset = bb.get_asset(asset_id)
        if asset is None:
            raise HTTPException(404, f"资产不存在: {asset_id}")
        return asset

    @app.delete("/api/projects/{pid}/assets/{asset_id}")
    def delete_asset(pid: str, asset_id: str):
        """物理删除叶子资产（资产树整理/走查清理）：仍被 finding 引用或有子资产→409。"""
        bb = _project(pid).bb
        if not bb.conn.execute(
                "SELECT 1 FROM assets WHERE id=? AND project_id=?",
                (asset_id, pid)).fetchone():
            raise HTTPException(404, f"资产不存在于本项目: {asset_id}")
        try:
            row = bb.delete_asset(asset_id, author="human")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"deleted": asset_id, **row}

    def _pid_of_asset(asset_id: str) -> str:
        for proj in store.list_projects():
            try:
                p = _project(proj["id"])
            except HTTPException:
                continue
            if p.bb.conn.execute(
                    "SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone():
                return proj["id"]
        raise HTTPException(404, f"资产不存在: {asset_id}")

    @app.get("/api/projects/{pid}/sessions")
    def list_sessions(pid: str):
        # F9：补 worker 状态灯数据——armed（meta）+ worker_running（内存 jobs）
        rows = _project(pid).bb.list_sessions(pid)
        for row in rows:
            meta = row.get("meta")
            meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
            row["worker_armed"] = bool(meta.get("worker_armed"))
            # v0.71 任务即窗口：绑定任务 id（前端延续模式徽章/页签上下文用）
            row["bound_task_id"] = meta.get("bound_task_id") or ""
            row["worker_running"] = _session_job_running(row["id"])
        return rows

    @app.get("/api/projects/{pid}/funcs")
    def list_funcs(pid: str, binary_sha256: str | None = None):
        rows = _project(pid).bb.list_funcs(pid, binary_sha256)
        return [_hex_func_view(r) for r in rows]

    # ---------- 逆向工作台（样本 / headless 缓存 / 人机共写） ----------

    @app.post("/api/projects/{pid}/samples", status_code=202)
    def upload_sample(pid: str, file: UploadFile = File(...)):
        """样本上传（multipart，≤256MB，流式 sha）→ binary 资产登记。

        样本是 untrusted 输入：只写 samples/，平台绝不在任何路径执行它；
        反编译是 trusted 解析（idat/analyzeHeadless 只解析不执行）。
        2026-09-29：上传不再自动投 headless 分诊——大样本分析很久，由用户
        点「开始分析」确认后触发（POST /binaries/{sha}/triage）或走
        「从 IDA 拉取函数」MCP 通道。job_id 恒为 None（前端 awaitJob 已兼容）。
        """
        proj = _project(pid)
        name = _safe_upload_name(file.filename or "sample.bin")
        samples_dir = _inside(proj, "samples")
        samples_dir.mkdir(parents=True, exist_ok=True)
        spool, sha, size = _spool_upload(file, SAMPLE_MAX_BYTES)
        final = samples_dir / name
        if final.exists():  # 重名：sha 短缀避让
            p = Path(name)
            final = samples_dir / f"{p.stem}.{sha[:8]}{p.suffix}"
        with open(final, "wb") as fh:
            shutil.copyfileobj(spool, fh)
        spool.close()
        rel = str(final.relative_to(proj.path)).replace("\\", "/")
        meta = {"filename": Path(name).name, "size": size, "path": rel,
                "uploaded_at": _utc_now()}
        asset = proj.bb.upsert_asset(pid, "binary", sha, meta=meta, author="human")
        if not asset["created"]:  # 同 sha 重传：刷新文件名/路径等 meta，不插新行
            proj.bb.update_asset_meta(asset["id"], meta)
        svc = _rev_service(proj)
        if svc.read_cached(sha) is not None:
            return {"cached": True, "job_id": None, "sha": sha, "asset_id": asset["id"]}
        # 2026-09-29：不再自动投分诊——分析动作由用户显式确认（大样本 headless
        # 可能很久）；重传已分析样本上方 read_cached 短路照旧直接可用
        return {"cached": False, "job_id": None, "sha": sha, "asset_id": asset["id"]}

    @app.post("/api/projects/{pid}/binaries/{sha}/triage", status_code=202)
    def retry_triage(pid: str, sha: str):
        proj = _project(pid)
        asset = proj.bb.find_asset(pid, "binary", sha)
        if asset is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        rel = (asset.get("meta") or {}).get("path")
        if not rel:
            raise HTTPException(422, "资产缺少样本路径（早期数据，请重新上传）")
        _inside(proj, rel)
        return {"job_id": _submit_triage(proj, sha, rel), "sha": sha}

    @app.get("/api/projects/{pid}/binaries/{sha}/overview")
    def binary_overview(pid: str, sha: str):
        """样本条首屏：缓存增强段 + 覆盖率 + 工具三态灯。缓存缺席也 200（cached:false）。"""
        proj = _project(pid)
        asset = proj.bb.find_asset(pid, "binary", sha)
        if asset is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        svc = _rev_service(proj)
        data = svc.read_cached(sha)
        kb = proj.bb.list_funcs(pid, sha)
        return {
            "sha": sha,
            "asset_meta": asset.get("meta") or {},
            "cached": data is not None,
            "meta": (data or {}).get("meta"),
            "sections": (data or {}).get("sections"),
            "imports": (data or {}).get("imports"),
            "strings_count": (len((data or {}).get("strings"))
                              if isinstance((data or {}).get("strings"), list) else 0),
            "function_count": len((data or {}).get("functions") or []),
            "analyzed_count": len(kb),
            "risk_count": sum(1 for f in kb if f.get("risk_tags")),
            "db_path": _db_rel(proj, sha),
            "tools": {
                "ida": {"state": _tool_lamp(proj, "ida-headless", data is not None)},
                "ghidra": {"state": _tool_lamp(proj, "ghidra-headless", data is not None)},
                # 真探活（1.5s 超时/3s TTL 懒缓存）：在线=installed 青灯；离线 off。
                # 人手 13337 实例或平台按需拉起的样本实例（2026-09-20）任一在线即亮
                "mcp": {"state": ("installed" if (
                    svc.mcp_online() or app.state.ida_mcp_manager.online_for_project(pid)
                ) else "off")},
            },
        }

    @app.get("/api/projects/{pid}/binaries/{sha}/functions")
    def cached_functions(pid: str, sha: str):
        """精简函数行（不带伪码）；地址一律 hex 字符串（JS Number 精度）。"""
        proj = _project(pid)
        data = _require_cache(proj, sha)
        return [
            {"address": hex(int(f["address"])), "name": f.get("name"),
             "size": f.get("size", 0), "has_pseudo": bool(f.get("pseudocode")),
             "n_calls": len(f.get("calls") or [])}
            for f in data.get("functions", [])
        ]

    @app.get("/api/projects/{pid}/binaries/{sha}/functions/{addr}")
    def cached_function(pid: str, sha: str, addr: str):
        """缓存命中走 v3 缓存；缓存缺席时 MCP 在线则实时取当前 IDA 库伪码（source=mcp），
        MCP 也没有才 409。"""
        proj = _project(pid)
        svc = _rev_service(proj)
        want = _parse_hex_addr(addr)
        data = svc.read_cached(sha)
        if data is not None:
            for f in data.get("functions", []):
                if int(f["address"]) == want:
                    return {"address": hex(int(f["address"])), "name": f.get("name"),
                            "size": f.get("size", 0), "calls": f.get("calls") or [],
                            "pseudocode": f.get("pseudocode")}
            raise HTTPException(404, f"缓存中无此函数: {addr}")
        live = svc.live_decompile(want)
        if live is not None:
            return {"address": hex(want), "name": None, "size": 0, "calls": [],
                    "pseudocode": live["pseudocode"], "source": "mcp"}
        raise HTTPException(409, "样本尚未完成 headless 分诊（缓存缺席）；"
                                "可在 IDA 中 Ctrl-Alt-M 启动 MCP 后实时读取，或先完成分诊")

    @app.get("/api/projects/{pid}/binaries/{sha}/xrefs/{addr}")
    def cached_xrefs(pid: str, sha: str, addr: str):
        from core.tools.decompiler import build_xrefs

        proj = _project(pid)
        svc = _rev_service(proj)
        want = _parse_hex_addr(addr)
        data = svc.read_cached(sha)
        if data is not None:
            x = build_xrefs(data, want)
            if x is None:
                raise HTTPException(404, f"缓存中无此函数: {addr}")
            return x
        # 缓存缺席：MCP 在线走 func_profile 实时降级（xrefs_for 内含选路）
        x = svc.xrefs_for(sha, want)
        if x is None:
            raise HTTPException(409, "样本尚未完成 headless 分诊（缓存缺席）；"
                                    "可在 IDA 中 Ctrl-Alt-M 启动 MCP 后实时读取，或先完成分诊")
        return x

    @app.get("/api/projects/{pid}/binaries/{sha}/strings")
    def cached_strings(pid: str, sha: str, q: str = Query(default="", max_length=200)):
        """字符串表（v3 契约）；缓存缺席 409，?q= 子串过滤，5000 行截断。"""
        from core.tools.decompiler import build_strings

        proj = _project(pid)
        data = _require_cache(proj, sha)
        strings = data.get("strings")
        if isinstance(strings, dict):  # 导出时该段失败的 {"error": ...}
            raise HTTPException(409, f"字符串段不可用: {strings.get('error', '导出失败')}")
        return build_strings(data, q or None)

    @app.post("/api/projects/{pid}/binaries/{sha}/open")
    def open_in_ida(pid: str, sha: str, addr: str | None = Query(default=None)):
        """detached 启动 IDA GUI 打开分诊数据库。?addr=0x.. 时在 db 同目录写一次性
        跳转脚本 _jump_<sha8>.py（auto_wait + idc.jumpto），以无引号无空格的相对
        -S 名 + cwd=db 目录启动，规避 Windows list2cmdline 的引号转义坑。"""
        from core.tools.decompiler import resolve_ida_gui

        proj = _project(pid)
        if proj.bb.find_asset(pid, "binary", sha) is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        rel_db = _db_rel(proj, sha)
        if not rel_db:
            raise HTTPException(409, "尚无 IDA 数据库，请先完成 headless 分诊")
        gui = resolve_ida_gui()
        if not gui:
            raise HTTPException(422, "未找到 IDA GUI（ida64/ida 不在 PATH 或常见安装目录）")
        db_path = _inside(proj, rel_db)

        jumping = None
        cwd = str(Path(gui).parent)
        ida_args = [gui, str(db_path)]
        if addr is not None:
            try:
                addr_int = int(addr, 0)
            except ValueError:
                raise HTTPException(422,
                                    f"addr 需为 0x 前缀 hex 或十进制整数: {addr}")
            if addr_int < 0:
                raise HTTPException(422, "addr 不可为负数")
            # 每次重写（同一 sha 的跳址脚本复用文件名）
            script_name = f"_jump_{sha[:8]}.py"
            (db_path.parent / script_name).write_text(
                "# 平台生成的一次性跳址脚本（open?addr=）；完成后可删\n"
                "import ida_auto\n"
                "import idc\n"
                "ida_auto.auto_wait()\n"
                f"idc.jumpto({addr_int})\n",
                encoding="utf-8")
            ida_args = [gui, f"-S{script_name}", str(db_path)]
            cwd = str(db_path.parent)
            jumping = hex(addr_int)

        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(ida_args, creationflags=flags,
                             close_fds=True, cwd=cwd)
        else:
            subprocess.Popen(["nohup", *ida_args], start_new_session=True,
                             close_fds=True, cwd=cwd)
        return {"opening": rel_db, "jumping": jumping}

    @app.post("/api/projects/{pid}/binaries/{sha}/writeback", status_code=202)
    def binary_writeback(pid: str, sha: str, body: WritebackIn):
        """func_kb 命名/注释 headless 写回 .i64（Job，900s）。

        锁文件存在（GUI 开着）返回 done{status:"locked"} 不强写；
        成功发 binary.annotated（只放成功条数，注释全文不进事件）。
        """
        proj = _project(pid)
        if proj.bb.find_asset(pid, "binary", sha) is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        items = [{"address": it.address,
                  **({"name": it.name} if it.name else {}),
                  **({"comment": it.comment} if it.comment else {})}
                 for it in body.items]
        return {"job_id": _submit_writeback(proj, sha, items), "sha": sha}

    @app.post("/api/projects/{pid}/binaries/{sha}/pull-names", status_code=202)
    def binary_pull_names(pid: str, sha: str):
        """把 IDA GUI 里手改的函数名拉回 func_kb（Job）：库内重导→diff→改名入 name_history。"""
        proj = _project(pid)
        if proj.bb.find_asset(pid, "binary", sha) is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        return {"job_id": _submit_pull_names(proj, sha), "sha": sha}

    @app.post("/api/projects/{pid}/binaries/{sha}/pull-ida-functions",
              status_code=202)
    def pull_ida_functions(pid: str, sha: str):
        """从 GUI IDA 拉取函数清单（Job，2026-09-29）：MCP list_funcs 分页 →
        轻量缓存（函数名/地址/大小，无伪码）+ 有效命名 diff 回拉 func_kb。
        前置：IDA 已打开样本并启动 MCP 插件（三态灯 MCP 亮）。"""
        proj = _project(pid)
        if proj.bb.find_asset(pid, "binary", sha) is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        return {"job_id": _submit_pull_ida_functions(proj, sha), "sha": sha}

    @app.post("/api/projects/{pid}/binaries/{sha}/pull-ida-functions/cancel")
    def cancel_pull_ida_functions(pid: str, sha: str):
        """停止进行中的 IDA 拉取（2026-09-30 断点续拉）：Event 页间检查点生效，
        已拉部分保持生效（partial 缓存），再点拉取从断点继续。无在跑 job 幂等。"""
        proj = _project(pid)
        if proj.bb.find_asset(pid, "binary", sha) is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        ev = app.state.pull_cancel.get(sha)
        if ev is None or ev.is_set():
            return {"cancelling": False, "hint": "没有进行中的拉取"}
        ev.set()
        return {"cancelling": True}

    @app.post("/api/projects/{pid}/binaries/{sha}/push-names-to-ida",
              status_code=202)
    def push_names_to_ida(pid: str, sha: str):
        """func_kb 有效命名 → GUI IDA（反向，Job）：批量 rename 写当前库，
        只改内存不落盘（完成后提示在 IDA 保存）。"""
        proj = _project(pid)
        if proj.bb.find_asset(pid, "binary", sha) is None:
            raise HTTPException(404, f"样本资产不存在: {sha}")
        return {"job_id": _submit_push_names_to_ida(proj, sha), "sha": sha}

    @app.post("/api/projects/{pid}/funcs", status_code=201)
    def create_func(pid: str, body: FuncCreateIn):
        """为仅缓存函数补建 func_kb 行（人写笔记的前置）；同 (sha,addr) 已存在则幂等返回。"""
        bb = _project(pid).bb
        existing = bb.lookup_func(pid, body.binary_sha256, int(body.address))
        if existing:
            return _hex_func_view(existing)
        rid = bb.upsert_func(pid, body.binary_sha256, int(body.address), body.name,
                             analysis=body.analysis, analyzed_by="human")
        return _hex_func_view(
            bb.lookup_func(pid, body.binary_sha256, int(body.address)) or {"id": rid["id"]})

    @app.patch("/api/projects/{pid}/funcs/{func_id}")
    def patch_func(pid: str, func_id: str, body: FuncPatchIn):
        changes = body.model_dump(exclude_unset=True)
        if changes.get("risk_tags") is None:  # 显式 null = 不动（清空请传 []）
            changes.pop("risk_tags", None)
        row = _project(pid).bb.patch_func(pid, func_id, author="human", **changes)
        if row is None:
            raise HTTPException(404, f"函数知识条目不存在: {func_id}")
        return _hex_func_view(row)

    @app.patch("/api/projects/{pid}/findings/{finding_id}")
    def patch_finding(pid: str, finding_id: str, body: FindingPatchIn):
        changes = body.model_dump(exclude_unset=True)
        proj = _project(pid)
        try:
            row = proj.bb.patch_finding(pid, finding_id, author="human",
                                        track=proj.track, **changes)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, f"发现不存在: {finding_id}")
        return row

    @app.delete("/api/projects/{pid}/findings/{finding_id}")
    def delete_finding(pid: str, finding_id: str):
        """物理删除发现（垃圾/走查数据清理）：store 同事务摘除反向 relates_to 边与
        任务 refs 引用；链边保留走孤儿占位、私信保留审计。误报请走 PATCH（撤回传播）。"""
        row = _project(pid).bb.delete_finding(pid, finding_id, author="human")
        if row is None:
            raise HTTPException(404, f"发现不存在: {finding_id}")
        return {"deleted": finding_id, **row}

    # ---------- 攻击链（人工建链；节点快照在 API 层组装，store 层保真实+项目归属） ----------

    def _entity_snapshot(bb: Blackboard, pid: str, node_type: str, node_id: str) -> dict | None:
        """链节点的实体快照（孤儿实体已删→None，调用方标 deleted:true）。"""
        if node_type == "finding":
            f = bb.get_finding(pid, node_id)
            if f is None:
                return None
            return {"id": f["id"], "title": f["title"], "severity": f["severity"],
                    "status": f["status"], "category": (f.get("evidence") or {}).get("category")}
        if node_type == "func_kb":
            f = bb.get_func(pid, node_id)
            if f is None:
                return None
            return {"id": f["id"], "name": f["name"], "address": hex(int(f["address"])),
                    "risk_tags": f.get("risk_tags") or []}
        if node_type == "artifact":
            a = bb.get_artifact(pid, node_id)
            if a is None:
                return None
            return {"id": a["id"], "kind": a["kind"], "path": a["path"],
                    "description": a.get("description", "")}
        return None

    def _chain_detail(bb: Blackboard, pid: str, cid: str) -> dict | None:
        chain = bb.get_chain(cid)
        if chain is None or chain.get("project_id") != pid:
            return None
        for link in chain["links"]:
            snap = _entity_snapshot(bb, pid, link["node_type"], link["node_id"])
            link["entity"] = snap
            link["deleted"] = snap is None
        return chain

    @app.get("/api/projects/{pid}/chains")
    def list_chains(pid: str, origin: str | None = None):
        # v19 origin 过滤：manual=人工链（默认视图）/ trace=任务轨迹自动链（沉淀侧消费）
        return _project(pid).bb.list_chains(pid, origin=origin)

    @app.get("/api/projects/{pid}/trace/{task_id}")
    def get_task_trace(pid: str, task_id: str):
        """执行轨迹（M1，R1+R2 现算零写入）：任务区间切分 + 过程聚合时间链。"""
        trace = traces.build_task_trace(_project(pid).bb, pid, task_id)
        if trace is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        return trace

    @app.get("/api/projects/{pid}/trace-effect")
    def get_trace_effect(pid: str, top: int = 20):
        """打法效果榜（M3，R4 基于物化侧）：轨迹链 (skill × kb) × verified finding。"""
        return traces.effect_stats(_project(pid).bb, pid, top=max(1, min(top, 50)))

    @app.get("/api/projects/{pid}/tree/{task_id}")
    def get_task_tree(pid: str, task_id: str):
        """任务尝试树 v2（task-attempt-tree，现算零写入）：目标 → 意图 → 检验结果，
        新发现下长新意图；发现归属 outcome_refs>存活窗>游离兜底。"""
        tree = tasktree.build_task_tree(_project(pid).bb, pid, task_id)
        if tree is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        return tree


    @app.get("/api/projects/{pid}/retrieval-stats")
    def get_retrieval_stats(pid: str):
        """检索对账三象限（retrieval-upgrade M3，2026-09-23）：提示×打开×verified
        产出离线对账，missed 清单是同义词/route_index 增补的信号源（与 M2 互喂）。"""
        return traces.retrieval_stats(_project(pid).bb, pid)

    @app.post("/api/projects/{pid}/chains", status_code=201)
    def create_chain(pid: str, body: ChainIn):
        bb = _project(pid).bb
        cid = bb.create_chain(pid, body.name, body.goal, author="human")
        return _chain_detail(bb, pid, cid)

    @app.get("/api/projects/{pid}/chains/{cid}")
    def get_chain(pid: str, cid: str):
        detail = _chain_detail(_project(pid).bb, pid, cid)
        if detail is None:
            raise HTTPException(404, f"攻击链不存在: {cid}")
        return detail

    @app.patch("/api/projects/{pid}/chains/{cid}")
    def update_chain(pid: str, cid: str, body: ChainPatchIn):
        bb = _project(pid).bb
        try:
            row = bb.update_chain(pid, cid, author="human",
                                  **body.model_dump(exclude_unset=True))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, f"攻击链不存在: {cid}")
        return _chain_detail(bb, pid, cid)

    @app.delete("/api/projects/{pid}/chains/{cid}")
    def delete_chain(pid: str, cid: str):
        if not _project(pid).bb.delete_chain(pid, cid, author="human"):
            raise HTTPException(404, f"攻击链不存在: {cid}")
        return {"deleted": cid}

    @app.post("/api/projects/{pid}/chains/{cid}/links", status_code=201)
    def add_chain_link(pid: str, cid: str, body: ChainLinkIn):
        bb = _project(pid).bb
        try:
            bb.add_chain_link(
                pid, cid, body.node_type, body.node_id, body.edge_note, author="human")
        except LookupError as e:  # 链不存在/跨项目 → 404
            raise HTTPException(404, str(e)) from e
        except ValueError as e:  # 节点非法/不存在/跨项目 → 422
            raise HTTPException(422, str(e)) from e
        return _chain_detail(bb, pid, cid)["links"][-1]

    @app.patch("/api/projects/{pid}/chains/links/{link_id}")
    def update_link_note(pid: str, link_id: str, body: LinkNoteIn):
        row = _project(pid).bb.update_link_note(pid, link_id, body.edge_note, author="human")
        if row is None:
            raise HTTPException(404, f"链节点不存在: {link_id}")
        return row

    @app.delete("/api/projects/{pid}/chains/links/{link_id}")
    def delete_chain_link(pid: str, link_id: str):
        row = _project(pid).bb.delete_chain_link(pid, link_id, author="human")
        if row is None:
            raise HTTPException(404, f"链节点不存在: {link_id}")
        return row

    # ---------- 蓝图（R4 逆向开发管线，DESIGN §9 R4） ----------

    @app.get("/api/projects/{pid}/blueprints")
    def list_blueprints(pid: str):
        return _project(pid).bb.list_blueprints(pid)

    @app.post("/api/projects/{pid}/blueprints", status_code=201)
    def create_blueprint(pid: str, body: BlueprintIn):
        bb = _project(pid).bb
        try:
            return bb.create_blueprint(
                pid, body.name, goal=body.goal, binary_sha256=body.binary_sha256,
                modules=[m.model_dump() for m in body.modules],
                content_md=body.content_md, author="human")
        except ValueError as e:  # 重名/模块非法 → 422
            raise HTTPException(422, str(e)) from e

    @app.get("/api/projects/{pid}/blueprints/{bid}")
    def get_blueprint(pid: str, bid: str):
        row = _project(pid).bb.get_blueprint(pid, bid)
        if row is None:
            raise HTTPException(404, f"蓝图不存在: {bid}")
        return row

    @app.patch("/api/projects/{pid}/blueprints/{bid}")
    def patch_blueprint(pid: str, bid: str, body: BlueprintPatchIn):
        bb = _project(pid).bb
        kw = body.model_dump(exclude_unset=True)
        status = kw.pop("status", None)
        try:
            if kw:
                row = bb.update_blueprint_content(pid, bid, author="human", **kw)
            else:
                row = bb.get_blueprint(pid, bid)
            if row is not None and status:
                row = bb.set_blueprint_status(pid, bid, status, author="human")
        except ValueError as e:  # 非法状态/不可流转 → 422
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, f"蓝图不存在: {bid}")
        return row

    @app.patch("/api/projects/{pid}/blueprints/{bid}/modules/{module_name}")
    def patch_blueprint_module(pid: str, bid: str, module_name: str,
                               body: BlueprintModulePatchIn):
        bb = _project(pid).bb
        try:
            row = bb.update_blueprint_module(
                pid, bid, module_name, author="human",
                **body.model_dump(exclude_unset=True))
        except LookupError as e:  # 模块不存在 → 404
            raise HTTPException(404, str(e)) from e
        except ValueError as e:  # 模块状态非法 → 422
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, f"蓝图不存在: {bid}")
        return row

    # ---------- 业务逻辑块（逆向第四页签：函数协作/业务语义，人机共写） ----------

    @app.get("/api/projects/{pid}/logic-blocks")
    def list_logic_blocks(pid: str, binary_sha256: str | None = None):
        return _project(pid).bb.list_logic_blocks(pid, binary_sha256=binary_sha256)

    @app.post("/api/projects/{pid}/logic-blocks", status_code=201)
    def create_logic_block(pid: str, body: LogicBlockIn):
        bb = _project(pid).bb
        try:
            return bb.create_logic_block(
                pid, body.name, description=body.description,
                binary_sha256=body.binary_sha256, seq=body.seq, author="human")
        except ValueError as e:  # 空名/同项目同样本重名 → 422
            raise HTTPException(422, str(e)) from e

    @app.get("/api/projects/{pid}/logic-blocks/{lbid}")
    def get_logic_block(pid: str, lbid: str):
        row = _project(pid).bb.get_logic_block(pid, lbid)
        if row is None:
            raise HTTPException(404, f"业务块不存在: {lbid}")
        return row

    @app.patch("/api/projects/{pid}/logic-blocks/{lbid}")
    def patch_logic_block(pid: str, lbid: str, body: LogicBlockPatchIn):
        try:
            row = _project(pid).bb.update_logic_block(
                pid, lbid, author="human", **body.model_dump(exclude_unset=True))
        except ValueError as e:  # 空名/重名 → 422
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, f"业务块不存在: {lbid}")
        return row

    @app.delete("/api/projects/{pid}/logic-blocks/{lbid}")
    def delete_logic_block(pid: str, lbid: str):
        if not _project(pid).bb.delete_logic_block(pid, lbid, author="human"):
            raise HTTPException(404, f"业务块不存在: {lbid}")
        return {"deleted": lbid}

    @app.post("/api/projects/{pid}/logic-blocks/{lbid}/funcs", status_code=201)
    def add_logic_block_func(pid: str, lbid: str, body: LogicBlockFuncIn):
        bb = _project(pid).bb
        try:
            return bb.add_logic_block_func(
                pid, lbid, body.address, body.role, author="human")
        except LookupError as e:  # 块不存在/跨项目 → 404
            raise HTTPException(404, str(e)) from e
        except ValueError as e:  # 地址非法/未入库/重复挂接/项目级块 → 422
            raise HTTPException(422, str(e)) from e

    @app.patch("/api/projects/{pid}/logic-blocks/{lbid}/funcs")
    def patch_logic_block_func(pid: str, lbid: str, body: LogicBlockFuncPatchIn,
                               address: str = ""):
        """改挂接函数的角色注。address 走 query（推荐 0x hex 串；query 无 int/str
        联合语义，统一交给 store 层 _parse_addr 解析）。"""
        if not address:
            raise HTTPException(422, "缺 address（0x hex 串）")
        try:
            row = _project(pid).bb.update_logic_block_func(
                pid, lbid, address, body.role, author="human")
        except ValueError as e:  # 地址非法 → 422
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, f"业务块或挂接不存在: {lbid}")
        return row

    @app.delete("/api/projects/{pid}/logic-blocks/{lbid}/funcs")
    def delete_logic_block_func(pid: str, lbid: str, address: str = ""):
        if not address:
            raise HTTPException(422, "缺 address（0x hex 串）")
        try:
            row = _project(pid).bb.remove_logic_block_func(
                pid, lbid, address, author="human")
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if row is None:
            raise HTTPException(404, f"业务块或挂接不存在: {lbid}")
        return row

    @app.get("/api/projects/{pid}/artifacts")
    def list_artifacts(pid: str, task_id: str | None = None,
                       session_id: str | None = None):
        """产物只读列表（攻击链节点选择器数据源；工作区隔离 W3 起可按归属过滤）。"""
        return _project(pid).bb.list_artifacts(pid, task_id=task_id,
                                               session_id=session_id)

    @app.post("/api/projects/{pid}/scratch/clear")
    def clear_scratch(pid: str):
        """清空项目自由工作区（scratch + .tmp，工作区隔离 W3）。正式产物 artifacts/ 不动。"""
        proj = _project(pid)
        removed = 0
        failed: list[str] = []
        for name in ("scratch", ".tmp"):
            d = Path(proj.path) / name
            if not d.exists():
                continue
            for p in sorted(d.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                    elif p.is_dir() and not any(p.iterdir()):
                        p.rmdir()
                    else:
                        continue
                    removed += 1
                except OSError:
                    failed.append(p.name)
        return {"removed": removed, "failed": failed[:10]}

    @app.post("/api/projects/{pid}/artifacts/upload", status_code=201)
    def upload_debug_log(pid: str, kind: str = Form(...), file: UploadFile = File(...)):
        """调试日志回流（x64dbg 动态验证凭据）：multipart ≤16MB，kind 仅 debug-log。"""
        if kind != "debug-log":
            raise HTTPException(422, "该入口只接受 kind=debug-log")
        proj = _project(pid)
        name = _safe_upload_name(file.filename or "debug.log")
        spool, sha, size = _spool_upload(file, DEBUGLOG_MAX_BYTES)
        out_dir = _inside(proj, "artifacts", "debug-log")
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"{sha[:12]}_{name}"
        with open(final, "wb") as fh:
            shutil.copyfileobj(spool, fh)
        spool.close()
        rel = str(final.relative_to(proj.path)).replace("\\", "/")
        art_id = proj.bb.add_artifact(pid, rel, kind="debug-log",
                                      description=name, sha256=sha, author="human")
        return {"id": art_id, "path": rel, "sha256": sha, "size": size}

    def _attachment_refs(pid: str, ids: list[str] | None) -> list[dict]:
        """附件随发（2026-09-19）共用校验：id 必须存在且 kind=attachment（否则
        422，严格不静默丢），返回渲染用精简清单（落 tasks.context.attachments
        与 human_note payload.attachments，agent 层渲染 📎 行）。"""
        refs: list[dict] = []
        for aid in (ids or []):
            art = _project(pid).bb.get_artifact(pid, aid)
            if art is None or art.get("kind") != "attachment":
                raise HTTPException(422, f"附件不存在或类型不符: {aid}")
            meta = _loads_meta(art)
            refs.append({
                "id": aid, "path": art["path"],
                "name": str(meta.get("original_name") or Path(art["path"]).name),
                "size": int(meta.get("size") or 0),
            })
        return refs

    @app.post("/api/projects/{pid}/attachments", status_code=201)
    def upload_attachment(pid: str, file: UploadFile = File(...)):
        """通用附件上传（2026-09-19 直播间输入行「+」随发附件）：任意类型 ≤64MB，
        落 artifacts/attachments/<sha12>_<name> 并注册 artifact 行 kind=attachment
        （meta.original_name/size 供渲染与下载命名）。同项目同内容（sha256）去重
        返回既有行，不产生重复 artifact。"""
        proj = _project(pid)
        name = _safe_upload_name(file.filename or "attachment.bin")
        spool, sha, size = _spool_upload(file, ATTACHMENT_MAX_BYTES)
        dup = proj.bb.find_artifact_by_sha(pid, sha, kind="attachment")
        if dup is not None:
            spool.close()
            meta = _loads_meta(dup)
            return {"id": dup["id"], "path": dup["path"], "name": name,
                    "size": int(meta.get("size") or size), "sha256": sha}
        out_dir = _inside(proj, "artifacts", "attachments")
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"{sha[:12]}_{name}"
        with open(final, "wb") as fh:
            shutil.copyfileobj(spool, fh)
        spool.close()
        rel = str(final.relative_to(proj.path)).replace("\\", "/")
        art_id = proj.bb.add_artifact(
            pid, rel, kind="attachment", description=name, sha256=sha,
            meta={"original_name": name, "size": size}, author="human")
        return {"id": art_id, "path": rel, "name": name, "size": size, "sha256": sha}

    @app.get("/api/projects/{pid}/artifacts/{aid}/download")
    def download_artifact(pid: str, aid: str):
        """产物/附件二进制下载（此前只有文本 content 只读口，≤256KB 截断——
        附件是 exe/elf/图片等二进制，必须走 FileResponse 全量）。"""
        proj = _project(pid)
        art = proj.bb.get_artifact(pid, aid)
        if art is None:
            raise HTTPException(404, f"产物不存在: {aid}")
        target = _inside(proj, art["path"])
        if not target.is_file():
            raise HTTPException(404, "产物文件缺失")
        name = str(_loads_meta(art).get("original_name")
                   or Path(art["path"]).name)
        return FileResponse(target, filename=name)

    @app.get("/api/projects/{pid}/events")
    def list_events(pid: str, since_id: int = 0, limit: int = 200,
                    before_id: int | None = None, tail: int = 0,
                    session_id: str | None = None, kinds: str | None = None):
        # tail/before_id：直播间首屏增量 + 上翻分页（不全量回放，2026-09-17）；
        # session_id：会话维度分页（2026-09-23 直播间会话窗口，store 层 F8 既支持）；
        # kinds：逗号分隔事件类型过滤（编排器对话历史按 kind=orch.chat 全量拉取用，
        # 2026-09-26——首屏只水合尾部 300 条，长跑项目 orch.chat 落窗外显空）
        kind_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
        return _project(pid).bb.recent_events(
            pid, since_id=since_id, limit=limit,
            session_id=session_id, before_id=before_id, tail=tail, kinds=kind_list)

    # ---------- 审批收件箱（§12 一等公民：越界/net:real 等待批动作） ----------

    @app.get("/api/projects/{pid}/approvals")
    def list_approvals(pid: str, status: str | None = None):
        proj = _project(pid)
        sql = "SELECT * FROM approvals WHERE project_id=?"
        params: list[Any] = [pid]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        # M5 D2：行动边界全文随审批卡出口（§0-10）——人类在审批卡里对照当前
        # 边界审授权/升级申请；server 拼好（与编排器 _mission_section 同源），
        # 前端存在才渲染。项目级常量，N 条重复 N 次可接受。
        from core.orchestrator.orchestrator import mission_boundary_lines
        proj_row = proj.bb.get_project(pid)
        boundary = "；".join(mission_boundary_lines(proj_row.get("track"), proj_row["config"]))
        # action 库存 JSON 字符串，出口解析为对象（与前端 types.ts Approval.action 对齐）
        out = []
        for r in proj.bb.conn.execute(sql, params).fetchall():
            d = dict(r)
            d["boundary"] = boundary
            try:
                d["action"] = json.loads(d["action"])
            except (TypeError, ValueError, KeyError):
                pass
            out.append(d)
        return out

    def _exec_approved_spawn_session(
        bb: Blackboard, pid: str, action: dict, approval_id: str,
    ) -> dict:
        """批准 spawn_session（v0.72 双语义分流）：
        - action 带 task_id（L1 执行审批单，专属窗已由调度器绑定段建好）：批准=
          **启动该任务窗**——窗在且非 closed → 武装+起跑；窗被人工关闭/绑定丢失
          → 重绑新窗起跑；任务已非 open（审批等待期被删/终态）→ 视为已处理
          直接通过（赛跑终检）。
        - action 不带 task_id（编排器 _tool_spawn_session 纯开窗单）：当场建
          无绑侦查窗并起跑（sessions_cap 预检+赛跑终检保留）。
        任何失败抛异常，由 decide 统一落 approval.exec_failed，不回滚批准。"""
        proj = _project(pid)
        auto = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
        tq = TaskQueue(bb)
        bind_to = str(action.get("task_id") or "")
        if bind_to:
            task = tq.get_task(bind_to)
            if task is None or task["status"] != "open":
                return {"session_id": None, "job_id": None,
                        "skipped": "任务已不处于待执行（审批等待期被处理）"}
            sid = task.get("target_session") or ""
            row = bb.get_session(sid) if sid else None
            if row is not None and row.get("status") != "closed":
                # 专属窗在：批准即启动（不新建窗）
                try:
                    agent = _ensure_agent(pid, sid)  # LLM 未就绪 503 → exec_failed
                except HTTPException as e:
                    raise RuntimeError(e.detail) from e
                if agent.paused:
                    return {"session_id": sid, "job_id": None,
                            "skipped": "窗口处于暂停态，未自动起跑（恢复后点「跑」）"}
                bb.set_session_meta(sid, {"worker_armed": True, "close_pending": None})
                job_id = _submit_worker(pid, agent, auto=True, origin="approval-spawn")
                bb.append_event(
                    pid, "session.spawned",
                    {"role": row.get("role") or "", "session_id": sid,
                     "approval_id": approval_id, "started_task": bind_to},
                    author="orchestrator")
                return {"session_id": sid, "job_id": job_id}
            # 窗被人工关闭/绑定丢失：重绑新窗再起跑
            if sid:
                try:
                    tq.unassign_session(sid)  # 仅动 open 行，终态不受影响
                except Exception:  # noqa: BLE001
                    log.exception("批准重绑前 unassign 失败 task=%s", bind_to)
            new_sid = _bind_task_window(pid, task, reason="approval-spawn:rebind")
            if not new_sid:
                raise RuntimeError("重绑执行窗失败（sessions_cap 满或预算硬闸）")
            try:
                agent = _ensure_agent(pid, new_sid)
            except HTTPException as e:
                raise RuntimeError(e.detail) from e
            bb.set_session_meta(new_sid, {"worker_armed": True, "close_pending": None})
            job_id = _submit_worker(pid, agent, auto=True, origin="approval-spawn")
            bb.append_event(
                pid, "session.spawned",
                {"role": action.get("role") or "", "session_id": new_sid,
                 "approval_id": approval_id, "started_task": bind_to},
                author="orchestrator")
            return {"session_id": new_sid, "job_id": job_id}
        # ---- 无 task_id：编排器纯开窗单（现状逻辑：建无绑侦查窗） ----
        active = autonomy.count_active_sessions(bb, pid)
        if active >= auto["sessions_cap"]:
            raise RuntimeError(
                f"活跃会话已达项目上限 sessions_cap={auto['sessions_cap']}"
                f"（当前 {active} 个非 closed 会话）；请先关窗或调高上限后重新申请")
        # 赛跑终检：审批等待期可能已无待执行任务（空窗不占 sessions_cap）
        open_n = bb.conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND status='open'",
            (pid,)).fetchone()["n"]
        if open_n == 0:
            raise RuntimeError("无待执行任务，未建窗")
        role = (action.get("role") or "").strip()
        if not role:
            raise RuntimeError("审批 action 缺少 role")
        try:
            exec_llm, plan_llm = _llms()
        except HTTPException as e:  # 无可用 key：503 转为执行失败，不回滚批准
            raise RuntimeError(e.detail) from e
        factory = _registered_session_factory(pid, exec_llm, plan_llm)
        try:
            agent = factory(role)  # 角色 yaml 缺失抛 FileNotFoundError
        except FileNotFoundError as e:
            raise RuntimeError(str(e)) from e
        sid = agent.session["id"]
        bb.set_session_meta(sid, {"worker_armed": True})  # F9：编排开窗默认已启动
        bb.append_event(
            pid, "session.spawned",
            {"role": role, "session_id": sid, "approval_id": approval_id},
            author="orchestrator")
        job_id = _submit_worker(pid, agent, auto=True, origin="approval-spawn")
        return {"session_id": sid, "job_id": job_id}

    def _exec_approved_escalation(
        bb: Blackboard, pid: str, action: dict, approval_id: str,
    ) -> dict:
        """H3 批准 request_escalation 后当场执行一次（deny-driven 一次性升级）：
        gateway.run 直跑（net=real 走 approval 校验，跑完即 consumed，同单不可复用），
        结果经收件箱 escalation_result 回流请求会话——下个步边界/空闲对话轮注入。
        与 spawn_session 同纪律：op 白名单字典分派；执行失败落 approval.exec_failed
        不回滚批准。escalation op 的唯一生产方是 Agent 的 request_escalation 工具
        （人类 API 也可建，op 字段照填），编排器不生产。"""
        cmd = str(action.get("cmd") or "").strip()
        runtime = str(action.get("runtime") or "")
        if not cmd or runtime not in RUNTIME_LEVELS:
            raise RuntimeError(f"escalation action 不合法：cmd/runtime 缺失或非法（{action}）")
        proj = _project(pid)
        sid = bb.conn.execute(
            "SELECT session_id FROM approvals WHERE id=?", (approval_id,)).fetchone()
        sid = sid["session_id"] if sid else None
        gateway = ExecutionGateway(bb=bb)
        r = gateway.run(
            cmd, runtime,
            threat_class=str(action.get("threat_class") or "trusted"),
            project_id=pid, session_id=sid, author=f"approval:{approval_id}",
            net=action.get("net"),
            timeout=float(action["timeout"]) if action.get("timeout") else None,
            approval_id=approval_id,
            workspace=proj.path,
        )
        brief = r.brief(limit=4000)
        if sid:
            ok = bb.inbox_post(pid, sid, "escalation_result", approval_id,
                               {"text": brief, "cmd": cmd, "runtime": runtime,
                                "exit_code": r.exit_code})
            if ok:  # 事件流同步可见（message.inbox 通用卡，title=命令头）
                bb.append_event(
                    pid, "message.inbox",
                    {"to_session": sid, "kind": "escalation_result",
                     "ref_id": approval_id, "title": f"🛫 升级命令已执行：{cmd[:60]}",
                     "by": "system"},
                    session_id=sid, author="system")
            # 回执踢醒：armed 且空闲 → 立即跑对话轮消费回执（同 note 端点语义）
            try:
                meta_raw = (bb.get_session(sid) or {}).get("meta")
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                if meta.get("worker_armed") and not _session_job_running(sid):
                    _submit_worker(pid, _ensure_agent(pid, sid), origin="escalation-result")
            except Exception:  # noqa: BLE001 —— kick 失败不影响回执投递
                log.exception("escalation 回执 kick 失败 approval=%s", approval_id)
        return {"exit_code": r.exit_code, "session_id": sid}

    def _exec_approved_phase_transition(
            bb: Blackboard, pid: str, action: dict, approval_id: str) -> dict:
        """批准阶段流转审批单（分阶段工作流 M2）：批准即流转。生产方唯一=API 层
        `_phase_gate_check` 的 L1 分流。目标阶段缺失/等待期人工已流转过 → 视为
        已处理直接通过（审批终检语义，不落 exec_failed）。"""
        proj = _project(pid)
        book = _phase_book_of(proj)
        to = str(action.get("to") or "").strip()
        if to not in book:
            return {"skipped": f"目标阶段不存在: {to}"}
        cur = phases_mod.current_spec(book, proj.meta or {})
        if cur and cur[0] == to:
            return {"skipped": "已处于目标阶段"}
        idle = int(orch_state.load_or_create(bb, pid)["derive_idle_rounds"])
        r = phases_mod.enter_phase(proj, to, by="approval",
                                   packs_root=app.state.packs_root,
                                   reason=f"审批 {approval_id} 批准", idle_rounds=idle)
        _schedule(pid, reason="phase-entered")
        return {"phase": to, "published": r["published"]}

    def _interrupt_claimed_window(pid: str, sid: str | None) -> None:
        """打断任务持有窗（M4 C1 cancel 链路共用：人工端点 + 审批处理器）。
        request_abort 让在跑步尽快收口（随后 fail 撞 ClaimError 被吞=任务已取消，
        _abort_current_task 有先例）；空闲窗（job 已退）直接 _abort_current_task
        清标志——与 /abort 端点同原语。**只打断不关窗**：窗保持待命可接新任务。
        会话不在注册表（重启后陈旧认领）→ 跳过（任务行已取消，无东西在跑）。"""
        if not sid:
            return
        agent = app.state.agents.get(sid)
        if agent is None:
            return
        agent.request_abort()
        if not _session_job_running(sid):
            agent._abort_current_task()

    def _exec_approved_cancel_task(
            bb: Blackboard, pid: str, action: dict, approval_id: str) -> dict:
        """批准取消任务审批单（M4 C1）：任务转 failed（blocked_reason=cancelled）
        + 打断在跑窗。任务缺失/已终态（等待期被人处理过）→ 视为已处理跳过。"""
        tid = str(action.get("task_id") or "")
        tq = TaskQueue(bb)
        row = tq.get_task(tid)
        if row is None or row["project_id"] != pid:
            return {"skipped": f"任务不存在: {tid}"}
        if row["status"] not in ("open", "claimed"):
            return {"skipped": f"任务状态为 {row['status']}，视为已处理"}
        res = tq.cancel_task(tid, by="approval",
                             reason=f"审批 {approval_id} 批准：{action.get('reason', '')}")
        _interrupt_claimed_window(pid, res.get("claimed_by"))
        # 键名用 task_status——decide 响应的 status 键承载批准决定，勿覆写
        return {"task_id": tid, "task_status": "failed"}

    def _exec_approved_requeue_task(
            bb: Blackboard, pid: str, action: dict, approval_id: str) -> dict:
        """批准放回审批单（M4 C1）：failed/awaiting_human → open（履历保留，
        原绑定窗优先续跑）。任务缺失/非 failed → 视为已处理跳过。"""
        tid = str(action.get("task_id") or "")
        tq = TaskQueue(bb)
        row = tq.get_task(tid)
        if row is None or row["project_id"] != pid:
            return {"skipped": f"任务不存在: {tid}"}
        if row["status"] != "failed":
            return {"skipped": f"任务状态为 {row['status']}，视为已处理"}
        tq.reopen(tid, by="approval")
        _schedule(pid, reason="task-requeued")
        return {"task_id": tid, "task_status": "open"}

    def _exec_approved_authorization(
            bb: Blackboard, pid: str, action: dict, approval_id: str) -> dict:
        """批准授权申请审批单（M5 D2，orchestrator-efficiency §0-10）：**纯回流**
        ——批准即人类授权，处理器不做任何平台动作（scope_expand 后 Agent 自行
        bb_add_asset 登记、rating_override 后按更高口径重新登记/patch）；结果经
        收件箱 authorization_result 回流提交会话。恒 human 决策在架构上已成立
        （Agent 产的审批单只有人类 decide），L2 无自动批路径。"""
        sid = str(action.get("session_id") or "")
        if sid:
            try:
                ok = bb.inbox_post(
                    pid, sid, "authorization_result", approval_id,
                    {"op": "authorization", "kind": action.get("kind"),
                     "scope_request": str(action.get("scope_request") or "")[:300],
                     "approved": True})
                if ok:  # 事件流同步可见（与 escalation 回执同款 message.inbox 卡）
                    bb.append_event(
                        pid, "message.inbox",
                        {"to_session": sid, "kind": "authorization_result",
                         "ref_id": approval_id,
                         "title": f"✅ 授权申请已批准（{action.get('kind')}）",
                         "by": "system"},
                        session_id=sid, author="system")
            except Exception:  # noqa: BLE001 —— 回流失败不影响批准
                log.exception("authorization 回流失败 approval=%s", approval_id)
        return {"notified": sid or None}

    def _exec_approved_delegate_window(
        bb: Blackboard, pid: str, action: dict, approval_id: str,
    ) -> dict:
        """批准 delegate_window（L1 委派开窗，2026-09-25 会话中心化）：开窗
        （armed）+ 写入委托（target_session=新窗）+ 带活起跑——批准即「窗+活」
        一次到位。cap 赛跑终检、dedup 终检；任何失败抛异常落 exec_failed。"""
        proj = _project(pid)
        tq = TaskQueue(bb)
        active = autonomy.count_active_sessions(bb, pid)
        auto = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
        if active >= auto["sessions_cap"]:
            raise RuntimeError(
                f"活跃会话已达项目上限 sessions_cap={auto['sessions_cap']}"
                f"（当前 {active} 个非 closed 会话）；请先关窗或调高上限后重新申请")
        objective = str(action.get("objective") or "").strip()
        if not objective:
            raise RuntimeError("delegate_window action 缺 objective")
        task_type = str(action.get("task_type") or "generic")
        scope = str(action.get("scope") or "")
        role = str(action.get("role") or "").strip()
        if tq.find_dedup_target(pid, dedup_fp(task_type, scope, objective)) is not None:
            return {"skipped": "审批等待期已存在同指纹委托，未重复开窗"}
        try:
            exec_llm, plan_llm = _llms()
        except HTTPException as e:
            raise RuntimeError(e.detail) from e
        factory = _registered_session_factory(pid, exec_llm, plan_llm)
        try:
            agent = factory(role or "_generalist")
        except FileNotFoundError as e:
            raise RuntimeError(str(e)) from e
        sid = agent.session["id"]
        bb.set_session_meta(sid, {"worker_armed": True})
        bb.append_event(
            pid, "session.spawned",
            {"role": role or "_generalist", "session_id": sid,
             "origin": "approval-delegate", "approval_id": approval_id},
            session_id=sid, author="orchestrator")
        try:
            task_id = tq.publish(
                pid, objective, scope=scope, task_type=task_type,
                noise_budget=str(action.get("noise_budget") or "passive"),
                priority=int(action.get("priority") or 2),
                conflict_keys=action.get("conflict_keys") or None,
                created_by="orchestrator",
                allowed_types=_task_type_table(pid).keys(),
                refs=action.get("refs") or None,
                role=role, target_session=sid)
        except ValueError as e:
            raise RuntimeError(str(e)) from e
        bb.append_event(
            pid, "delegation.posted",
            {"task_id": task_id, "objective": objective, "task_type": task_type,
             "created_by": "orchestrator",
             "role": role, "new_window": True, "approval_id": approval_id},
            session_id=sid, author="orchestrator")
        job_id = _submit_worker(pid, agent, auto=True, origin="approval-delegate")
        return {"session_id": sid, "task_id": task_id, "job_id": job_id}

    # 审批 op 处理器白名单（批 4，红线）：批准后动作只准字典分派，绝不 eval。
    # delegate_window 的唯一生产方=Orchestrator 的 L1 委派分流；spawn_session
    # 为旧版待决单兼容保留；escalation（H3）唯一生产方=Agent 的
    # request_escalation 工具；phase_transition 唯一生产方=API 层
    # _phase_gate_check 的 L1 分流——各 op 生产方不混用。
    _APPROVAL_OP_HANDLERS = {
        "delegate_window": _exec_approved_delegate_window,
        "spawn_session": _exec_approved_spawn_session,
        "escalation": _exec_approved_escalation,
        "phase_transition": _exec_approved_phase_transition,
        "cancel_task": _exec_approved_cancel_task,
        "requeue_task": _exec_approved_requeue_task,
        "authorization": _exec_approved_authorization,
    }

    @app.post("/api/approvals/{approval_id}/decide")
    def decide_approval(approval_id: str, body: ApprovalDecisionIn):
        proj_bb = _bb_of_approval(approval_id)
        try:
            row = proj_bb.decide_approval(approval_id, body.decision)  # 人类决策（默认 decided_by=human）
        except ValueError as e:
            raise HTTPException(404 if "不存在" in str(e) else 422, str(e))
        result: dict[str, Any] = {"approval_id": approval_id, "status": body.decision}
        # 批 4：approved 且 action.op 命中白名单才执行处理器；
        # rejected / 无 op / 未知 op 维持旧语义——只翻状态（rejected 由 orch 下轮经事件改道）
        if body.decision == "rejected":
            # M5 D2 搭车（§0-10）：escalation/authorization 被拒后回流提交会话
            # （此前 rejected 无回流=Agent 空等）；回流失败不影响拒绝
            action = row.get("action")
            if isinstance(action, str):
                try:
                    action = json.loads(action)
                except ValueError:
                    action = None
            op = action.get("op") if isinstance(action, dict) else None
            sid = str((action or {}).get("session_id") or "") if isinstance(action, dict) else ""
            if op in ("escalation", "authorization") and sid:
                try:
                    proj_bb.inbox_post(
                        row["project_id"], sid, "approval_rejected", approval_id,
                        {"op": op, "kind": (action or {}).get("kind")})
                except Exception:  # noqa: BLE001
                    log.exception("拒绝回流失败 approval=%s", approval_id)
        if body.decision == "approved":
            action = row.get("action")
            if isinstance(action, str):
                try:
                    action = json.loads(action)
                except ValueError:
                    action = None
            op = action.get("op") if isinstance(action, dict) else None
            handler = _APPROVAL_OP_HANDLERS.get(op) if op else None
            if handler is not None:
                try:
                    result.update(executed=True,
                                  **handler(proj_bb, row["project_id"], action, approval_id))
                except Exception as e:  # noqa: BLE001 —— 执行失败不回滚批准
                    err = f"{type(e).__name__}: {e}"
                    proj_bb.append_event(
                        row["project_id"], "approval.exec_failed",
                        {"approval_id": approval_id, "op": op, "error": err},
                        author="system")
                    result.update(executed=False, error=err)
                else:
                    # 触发点 E（批 5）：新窗的 job 已由处理器提交；再唤醒其他空闲窗
                    # 认领同轮任务（L1/L2 未暂停；新窗被 _session_job_running 去重）。
                    try:
                        ep = row["project_id"]
                        ecfg = _auto_cfg(ep)
                        if ecfg["level"] in {"L1", "L2"} and not ecfg["paused"]:
                            result["kicked"] = _kick_workers(ep)
                    except Exception:  # noqa: BLE001 —— kick 失败不影响批准结果
                        log.exception("审批后 kick 失败 approval=%s", approval_id)
        return result

    def _bb_of_approval(approval_id: str) -> Blackboard:
        for proj in store.list_projects():
            try:
                p = _project(proj["id"])
            except HTTPException:
                continue
            if p.bb.conn.execute(
                    "SELECT 1 FROM approvals WHERE id=?", (approval_id,)).fetchone():
                return p.bb
        raise HTTPException(404, f"审批不存在: {approval_id}")

    # ---------- 任务（人类插手通道 §6.4） ----------

    @app.get("/api/projects/{pid}/tasks")
    def list_tasks(pid: str, status: str | None = None):
        rows = _tq(pid).list_tasks(pid, status=status)
        proj = _project(pid)
        for r in rows:
            # C6：failed 卡恒可续跑（resumable 恒真）；resume_mode 派生——
            # 任务键断点快照在=snapshot（⚡ 带现场续跑），否则=transcript（↩ 接手现场续跑）
            if r.get("status") == "failed":
                r["resumable"] = True
                r["resume_mode"] = _resume_mode_of(proj, r["id"])
            else:
                r["resumable"] = False
        return rows

    @app.get("/api/projects/{pid}/task-graph")
    def get_task_graph(pid: str):
        # 退役过渡（task-attempt-tree M2，2026-09-27）：TaskFlow 已由任务树替代，
        # 端点先回 410 一版，下版连同 graph.task_graph 函数与相关测试一并删除。
        raise HTTPException(410, "task-graph 已退役：请改用 GET /tree/{task_id}（任务尝试树）")

    @app.get("/api/projects/{pid}/session-graph")
    def get_session_graph(pid: str):
        # 会话中心化 M4：节点=编排器+会话窗；delegate/derive/inbox/dm 边（见 graph.session_graph）
        return session_graph(_project(pid).bb, pid)

    @app.get("/api/projects/{pid}/board-graph")
    def get_board_graph(pid: str):
        # 黑板链路图（2026-09-20）：五类对象类型分层 DAG；边口径见 graph.board_graph docstring
        return board_graph(_project(pid).bb, pid)

    @app.get("/api/projects/{pid}/attack-path")
    def get_attack_path(pid: str, target: str):
        # 单站攻击链路图 v3（2026-09-24）：目标→意图→漏洞/发现/死路，只读
        try:
            return build_attack_path(_project(pid).bb, pid, target)
        except LookupError:
            raise HTTPException(404, "目标资产不存在")

    @app.get("/api/projects/{pid}/intents")
    def get_intents(pid: str, status: str | None = None):
        # 意图清单（人类侧，供收尾核对）；?status=open/closed 过滤
        proj = _project(pid)
        return list_intents(proj.bb, pid, status=status)

    @app.post("/api/projects/{pid}/intents/{iid}/reopen")
    def post_reopen_intent(pid: str, iid: str, payload: dict | None = None):
        # 人类否决收尾：漏洞被证伪/有新证据 → 重开意图（AI 须重新收尾）
        try:
            return reopen_intent(_project(pid).bb, pid, iid, author="human",
                                 note=(payload or {}).get("note", ""))
        except LookupError:
            raise HTTPException(404, "意图不存在")

    @app.post("/api/projects/{pid}/tasks", status_code=201)
    def publish_task(pid: str, body: TaskIn):
        table = _task_type_table(pid)
        noise = body.noise_budget or table.get(body.task_type, "passive")
        tq = _tq(pid)
        att_refs = _attachment_refs(pid, body.attachment_ids)  # 坏 id 422（先于 dedup）
        # 分阶段工作流（M2，§4.4 双层拦截之二）：入场门未过时 gate_types 内类型
        # 422 带原因（编排器派单侧是第一层；认领侧不拦；人工流转阶段是放行阀）
        proj = _project(pid)
        _blocked = phases_mod.gate_block_reason(
            proj.bb, pid, proj.meta, proj.track, app.state.packs_root,
            task_type=body.task_type,
            idle_rounds=int(orch_state.load_or_create(proj.bb, pid)["derive_idle_rounds"]))
        if _blocked:
            raise HTTPException(422, _blocked)
        # 机制 1.1 发布去重：同指纹（type+归一化 scope+objective）命中 open/claimed →
        # 返回 200 + deduplicated，前端确认框"仍要发布"后带 force 重发才真发
        if not body.force:
            dup = tq.find_dedup_target(
                pid, dedup_fp(body.task_type, body.scope, body.objective))
            if dup is not None:
                return JSONResponse(status_code=200, content={
                    "task_id": dup["id"], "deduplicated": True,
                    "existed_status": dup["status"], "kicked": []})
        try:
            acceptance = body.acceptance
            if acceptance is not None:
                # 独立验证 M1：结构化条目归一化为 dict（str 原样），verify 规格的
                # 校验在 publish→_initial_context（validate_verify_spec，违例 422）
                acceptance = [a if isinstance(a, str) else
                              {"text": a.text, **({"verify": a.verify} if a.verify else {})}
                              for a in acceptance]
            task_id = tq.publish(
                pid, body.objective, scope=body.scope, task_type=body.task_type,
                noise_budget=noise, priority=body.priority,
                conflict_keys=body.conflict_keys, created_by="human",
                allowed_types=table.keys(), refs=body.refs, workset=body.workset,
                attachments=att_refs, acceptance=acceptance,
                parent_id=body.parent_id,
                role=body.role,   # 委托建议角色（开窗/分派挑专家用；非强制换装）
                # expert-pool M2（§4.6）：值域=绑定专家清单；未绑定=按轨过滤的池
                allowed_roles=(expert_allowed_roles(
                    app.state.packs_root, _project(pid).track, _project(pid).experts)
                    if _project(pid).track else None),
                bypass_target_guard=body.force,  # force 旁路 dedup 与同 target 闸
                # 会话中心化：委托归属窗（人在某会话窗发活时由前端写入）；
                # ''=未指派，不自动起跑，交编排器重新委派
                target_session=body.target_session)
        except ValueError as e:
            raise HTTPException(422, str(e))
        # 触发点 D（A5）：L2 下人类发委托后去抖重排（30s 合并一轮）
        _maybe_replan(pid, reason="human-publish")
        # 会话中心化：不再自动建专属窗。target_session 有效 → 委托进该窗队列；
        # 人类显式委托=武装授权（未武装窗当场武装），无 worker 在跑即手动起跑
        # （manual override，不受挡位/paused 约束）；窗忙=排队，当前活干完自动
        # 接。目标窗已关/不存在 → 退回未指派，交编排器重新委派。
        session_id = body.target_session or None
        if session_id:
            srow = proj.bb.get_session(session_id)
            if srow is None or srow.get("status") == "closed":
                try:
                    tq.unassign_session(session_id)
                except Exception:  # noqa: BLE001
                    log.exception("目标窗失效退回失败 task=%s", task_id)
                session_id = None
            else:
                try:
                    task = tq.get_task(task_id)
                    proj.bb.append_event(
                        pid, "delegation.posted",
                        {"task_id": task_id,
                         "objective": (task or {}).get("objective", body.objective),
                         "task_type": body.task_type, "created_by": "human"},
                        session_id=session_id, author=session_id)
                    proj.bb.set_session_meta(
                        session_id, {"worker_armed": True, "close_pending": None})
                    if not _session_job_running(session_id):
                        _submit_worker(
                            pid, _ensure_agent(pid, session_id),
                            origin="human-delegate")
                except Exception:  # noqa: BLE001 —— 起跑失败不拖垮发布
                    log.exception("委托起跑失败 pid=%s task=%s", pid, task_id)
        return {"task_id": task_id, "kicked": [], "deduplicated": False,
                "session_id": session_id}

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: str, body: TaskPatch):
        pid = _pid_of_task(task_id)  # 不存在 → 404
        tq = TaskQueue(_project(pid).bb)
        cur = tq.get_task(task_id)
        changes = body.model_dump(exclude_unset=True)
        # v0.71 任务即窗口：claimed（执行中）仅放行 role——中途改角色立即热换装；
        # objective 等仍不可改（已固化进在跑会话上下文），done（战果）不可编辑
        if cur["status"] == "claimed" and set(changes) - {"role"}:
            raise HTTPException(409, "执行中任务仅可修改角色（其余字段已固化进会话上下文）")
        if cur["status"] not in {"open", "failed", "claimed"}:
            raise HTTPException(409, f"任务状态为 {cur['status']}，不可编辑")
        if "role" in changes and str(changes["role"] or "").strip():
            if not expert_exists(app.state.packs_root,
                                 str(changes["role"]).strip(), _project(pid).track):
                raise HTTPException(422, f"专家不在池内或不可服务该轨: {changes['role']}")
        try:
            updated = tq.update_task(
                task_id, by="human",
                allowed_types=_task_type_table(pid).keys(),
                **changes)
        except ValueError as e:
            raise HTTPException(422, str(e))
        # v0.71：执行中任务改了角色 → 对在跑会话立即热换装（prompt 下个步进生效；
        # 会话未在内存时吞掉——worker 恢复路径会按任务行重换装）
        if (cur["status"] == "claimed" and "role" in changes
                and updated.get("claimed_by")):
            try:
                _ensure_agent(pid, updated["claimed_by"]).apply_role_change(updated)
            except Exception:  # noqa: BLE001
                log.exception("改角色热换装失败 task=%s", task_id)
        return updated

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, body: CancelTaskIn | None = None):
        """人工取消任务（M4 C1 人工口，编排器 L2 直执与审批处理器的同源写口）：
        open/claimed → failed（blocked_reason=cancelled）+ 打断在跑窗（复用
        /abort 原语，**窗不关**——保持待命可接新任务）。已终态 409。"""
        pid = _pid_of_task(task_id)
        tq = TaskQueue(_project(pid).bb)
        try:
            res = tq.cancel_task(task_id, by="human",
                                 reason=(body.reason if body else "") or "人工取消")
        except ValueError as e:
            raise HTTPException(409, str(e))
        _interrupt_claimed_window(pid, res.get("claimed_by"))
        return {"task_id": task_id, "status": "failed",
                "interrupted": bool(res.get("claimed_by"))}

    @app.post("/api/tasks/{task_id}/reopen")
    def reopen_task(task_id: str, body: ReopenIn | None = None):
        pid = _pid_of_task(task_id)
        tq = TaskQueue(_project(pid).bb)
        drop = bool(body and body.drop_scene)  # C6：丢弃现场从零重做
        try:
            tq.reopen(task_id, by="human",
                      note=(body.note if body else "") or "",
                      scene="dropped" if drop else "kept")
        except ValueError as e:
            raise HTTPException(409, str(e))
        if drop:
            proj = _project(pid)
            clear_task_resume(proj.artifacts_dir, task_id)
            transcript = task_transcript_path(proj.artifacts_dir, task_id)
            if transcript is not None:
                try:
                    transcript.unlink()
                except OSError:
                    log.warning("丢弃现场：transcript 清理失败（任务 %s）", task_id)
        # 触发点 D（C1 放回，与 publish 同口径）：L1/L2 未暂停时唤醒空闲 armed
        # worker 认领放回的任务——worker 在队列空时已退出（空队列零成本退），
        # 不 kick 则放回的任务永久悬 open 无人认领。paused/L0 时排队等恢复
        # （响应带 kicked 供前端提示）。
        kicked: list[str] = []
        cfg = _auto_cfg(pid)
        if cfg["level"] in {"L1", "L2"} and not cfg["paused"]:
            kicked = _kick_workers(pid)
            _schedule(pid, reason="task-reopen")  # v0.71 调度器：原窗保留绑定，closed 才重绑
        return {"task_id": task_id, "status": "open", "kicked": kicked}

    def _session_meta(bb, sid: str) -> dict:
        """读会话 meta（JSON 文本→dict；损坏/缺失→{}）。"""
        raw = (bb.get_session(sid) or {}).get("meta")
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except ValueError:
                return {}
        return raw or {}

    def _spawn_session_for_task(pid: str, task: dict, *, armed: bool = True,
                                role: str | None = None):
        """C6：为任务新建执行会话（角色沿用原认领者，读不到→通用角色；
        v0.71 绑窗器传 role 显式覆写——按任务绑定角色建窗）。armed 决定认领语义。
        返回 (agent, sid)。sessions_cap 超限 409。"""
        proj = _project(pid)
        bb = proj.bb
        auto = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
        if autonomy.count_active_sessions(bb, pid) >= auto["sessions_cap"]:
            raise HTTPException(
                409, f"活跃会话已达项目上限 sessions_cap={auto['sessions_cap']}；"
                     "请先关窗或在直播间调高上限")
        exec_llm, plan_llm = _llms()
        if role is None:
            role = "_generalist"
            if task.get("claimed_by"):
                origin_sess = bb.get_session(task["claimed_by"])
                if origin_sess and origin_sess.get("role"):
                    role = origin_sess["role"]
        factory = _registered_session_factory(pid, exec_llm, plan_llm)
        try:
            agent = factory(role, session_name=f"任务窗·{task['objective'][:12]}")
        except FileNotFoundError as e:
            raise HTTPException(422, str(e))
        sid = agent.session["id"]
        # v24（会话中心化）：任务即窗语义改为 target_session 单绑（bind_session 落），
        # 不再写 meta.bound_task_id；spawn_task_id 保留服务 resume 复盘窗幂等
        bb.set_session_meta(sid, {"worker_armed": armed,
                                  "spawn_task_id": task["id"]})
        return agent, sid

    def _bind_task_window(pid: str, task: dict, reason: str = "schedule") -> str | None:
        """v0.71 任务即窗口：为 open 任务建专属执行窗（armed=false 待命不耗 LLM）
        并双向绑定（tasks.target_session ↔ sessions.meta.bound_task_id）。

        失败（cap 满/预算硬闸/LLM 未就绪）返回 None——任务保持无绑，由调度器
        sweep 重试；绝不抛异常拖垮发布路径。绑定竞态（已被并发绑定，bind_session
        ValueError）→ 关掉刚建的窗防孤儿。"""
        try:
            proj = _project(pid)
            bb = proj.bb
            if autonomy.hard_block_reason(bb, pid, "spawn_session"):
                return None
            role = (task.get("role") or "").strip()
            if role and not expert_exists(app.state.packs_root, role, proj.track):
                role = ""  # 专家缺失/不服务该轨 → 底色 _generalist（同自动补窗口径）
            agent, sid = _spawn_session_for_task(
                pid, task, armed=False, role=role or "_generalist")
        except HTTPException as e:  # cap 409 / 角色 yaml 422 / LLM 503
            log.warning("任务绑窗失败 pid=%s task=%s: %s", pid, task.get("id"), e.detail)
            return None
        try:
            TaskQueue(bb).bind_session(task["id"], sid, by=reason)
        except ValueError:  # 已被并发绑定/非 open → 回收孤儿窗
            try:
                _do_close_session(sid)
            except Exception:  # noqa: BLE001
                log.exception("回收孤儿绑窗失败 sid=%s", sid)
            return None
        bb.append_event(
            pid, "session.spawned",
            {"role": role or "_generalist", "session_id": sid,
             "origin": "task-window", "task_id": task["id"], "reason": reason},
            session_id=sid, author="system")
        return sid

    def _resume_mode_of(proj: Project, task_id: str) -> str:
        """C6：resume_mode 派生——任务键断点快照存在=snapshot（⚡ 带现场续跑）；
        否则=transcript（↩ 接手现场续跑：C10 末 60 条+attempts 履历，任何 failed 卡可用）。"""
        p = task_resume_path(proj.artifacts_dir, task_id)
        return "snapshot" if p is not None and p.exists() else "transcript"

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str):
        """C6 失败任务跨会话完整续跑（取代 E12 限原会话语义）：
        resume_mode=snapshot（任务键断点快照在）→ 原会话可复用则 revive 原会话，
        否则新建 armed 任务窗——认领即复活（messages 整体+next_step/max_steps 断点，
        消费即删）；resume_mode=transcript → 接手现场续跑（C10 末 60 条+attempts）。
        budget 快照缺省 +200。失败任务恒可续跑（不再 404/409 原会话门槛）。"""
        pid = _pid_of_task(task_id)
        proj = _project(pid)
        tq = TaskQueue(proj.bb)
        task = tq.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        if task["status"] != "failed":
            raise HTTPException(409, f"任务状态为 {task['status']}，仅失败任务可续跑")
        mode = _resume_mode_of(proj, task_id)
        sid = task.get("claimed_by")
        orig_row = proj.bb.get_session(sid) if sid else None
        # 原窗就近接手（2026-09-23 resume-origin-window 定稿：原窗存活即原窗跑，
        # 对齐 reopen v0.71「失败任务归原绑定窗」既有定稿——此前只认 snapshot 模式，
        # transcript 恒新窗，LiveRoom 失败窗点续跑任务飘走旧窗空挂）。
        reusable = bool(sid and orig_row
                        and orig_row.get("status") != "closed")

        if reusable:
            agent = _ensure_agent(pid, sid)
            if _session_job_running(sid):
                raise HTTPException(409, "原会话有任务在跑，稍后再续跑")
            if mode == "snapshot":
                # ⚡ 带现场续跑（E12 路径）：revive 快照 → reopen+claim → 断点续跑
                st = agent.revive_snapshot(task_id)
                if st is None:
                    raise HTTPException(409, "原会话快照不可复活，请改用「放回」重新派发")
                try:
                    tq.reopen(task_id, by="human", scene="kept")
                    tq.claim(task_id, sid, lease_minutes=agent.config.lease_minutes)
                except ValueError as e:
                    raise HTTPException(409, str(e))
                if st.get("reason") == "budget":
                    old = agent.dispatcher.max_steps
                    agent.dispatcher.max_steps = old + 200
                    proj.bb.append_event(
                        pid, "step.budget_extended",
                        {"session_id": sid, "task_id": task_id,
                         "old_max": old, "new_max": old + 200, "by": "human"},
                        session_id=sid, author="human")
            else:
                # ↩ 接手现场续跑：现场就在本窗（C10 任务现场文件归任务所有，
                # 零搬运）；认领由 worker 首轮 run_next_task 执行（与跨会话
                # 新窗分支同时点，run_task 接手路径重建末 60 条上下文）
                try:
                    tq.reopen(task_id, by="human", scene="kept")
                except ValueError as e:
                    raise HTTPException(409, str(e))
            agent._stop_after_task = False  # 清中断一次性闸门，否则 worker 领任务前即退出
            agent._pause_req.clear()
            agent._abort_req.clear()
            proj.bb.set_session_status(sid, "running")
            _submit_worker(pid, agent, origin="human-resume")
            return {"task_id": task_id, "session_id": sid,
                    "status": "resumed", "resume_mode": mode}

        # 跨会话：新建 armed 任务窗（snapshot=认领即复活；transcript=接手现场续跑）
        agent, new_sid = _spawn_session_for_task(pid, task, armed=True)
        try:
            tq.reopen(task_id, by="human", scene="kept")
            # 会话中心化：委托随窗迁移——旧窗已关，原 target（若有）先退回再
            # 绑新窗（worker run_session 只取 target=本窗 的委托）
            row_now = tq.get_task(task_id)
            old_target = ((row_now or {}).get("target_session") or "")
            if old_target and old_target != new_sid:
                tq.unassign_session(old_target)
            tq.bind_session(task_id, new_sid, by="resume-spawn")
        except ValueError as e:
            raise HTTPException(409, str(e))
        _submit_worker(pid, agent, origin="task-resume")
        return {"task_id": task_id, "session_id": new_sid,
                "status": "resumed", "resume_mode": mode}

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str):
        pid = _pid_of_task(task_id)
        proj = _project(pid)
        tq = TaskQueue(proj.bb)
        try:
            tq.delete(task_id, by="human")
        except ValueError as e:
            raise HTTPException(409, str(e))
        transcript = task_transcript_path(proj.artifacts_dir, task_id)  # C10 现场随任务删
        if transcript is not None:
            try:
                transcript.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("任务现场文件清理失败（任务 %s）", task_id)  # 孤儿文件无行引用，永不载入
        clear_task_resume(proj.artifacts_dir, task_id)  # C6 任务键断点快照随任务删
        return {"deleted": task_id}

    def _pid_of_task(task_id: str) -> str:
        for proj in store.list_projects():
            try:
                p = _project(proj["id"])
            except HTTPException:
                continue
            if p.bb.conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                return proj["id"]
        raise HTTPException(404, f"任务不存在: {task_id}")

    def _task_context_digest(bb: Blackboard, pid: str, task: dict) -> str:
        """任务上下文摘要（F9 任务窗 human_note 用；C10 起补历次尝试履历）：
        目标/状态/结果注记/blocked_reason/plan 前 10 步/context_refs 命中发现前 10 条。"""
        task_id = task["id"]
        lines = [f"📋 任务上下文（双击任务流卡片开窗，task={task_id}）",
                 f"目标：{task['objective'][:500]}",
                 f"状态：{task['status']}"]
        if task.get("result_note"):
            lines.append(f"结果注记：{task['result_note'][:500]}")
        if task.get("blocked_reason"):
            lines.append(f"受阻原因：{task['blocked_reason']}")
        plan_steps = task.get("plan") or []
        if plan_steps:
            lines.append("计划步：" + "；".join(
                f"{s.get('id')} {s.get('title')}({s.get('status')})"
                for s in plan_steps[:10]))
        attempts = (task.get("context") or {}).get("attempts") or []
        if attempts:
            lines.append(f"历次尝试（共 {len(attempts)} 次）：")
            lines += render_attempts_lines(attempts)
        refs = task.get("context_refs") or []
        if refs:
            by_id = {f["id"]: f for f in bb.list_findings(pid)}
            hits = [by_id[r] for r in refs if r in by_id][:10]
            if hits:
                lines.append("相关发现：")
                for f in hits:
                    lines.append(
                        f"- {f.get('vuln_class', '?')} [{f.get('severity', '?')}] "
                        f"{(f.get('title') or '')[:120]} ({f['id']})")
        return "\n".join(lines)

    @app.post("/api/tasks/{task_id}/spawn-window")
    def spawn_task_window(task_id: str):
        """F9 任务窗：双击任务卡直开窗（四态闭环，2026-09-23 放开非终态）：
        - open 无绑 = 手动补绑待命窗（_bind_task_window，armed=false 不起跑，
          起跑走跑队列/挡位），cap 满 409（人手开窗同口径）；
        - open/claimed 已绑 = 幂等挂回该窗；claimed 窗已关 = 409 引导放回；
        - done/failed = 复盘/续研开新窗（armed=False；上下文经 human_note 注入，
          worker 首个控制点 drain）。幂等：同任务已有非 closed 任务窗直接返回。"""
        pid = _pid_of_task(task_id)
        proj = _project(pid)
        bb = proj.bb
        tq = TaskQueue(bb)
        task = tq.get_task(task_id)
        if task is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        status = task["status"]
        if status in {"open", "claimed"}:
            sid = task.get("target_session") or ""
            row = bb.get_session(sid) if sid else None
            if row is not None and row.get("status") != "closed":
                return {"session_id": sid, "created": False}
            if status == "claimed":
                raise HTTPException(
                    409, "任务的执行窗已关闭；请先「放回」任务再重新派发")
            auto = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
            active = autonomy.count_active_sessions(bb, pid)
            if active >= auto["sessions_cap"]:
                raise HTTPException(
                    409, f"活跃会话已达项目上限 sessions_cap={auto['sessions_cap']}"
                         f"（当前 {active} 个非 closed 会话）；请先关窗或调高上限后重试")
            new_sid = _bind_task_window(pid, task, reason="human:spawn-window")
            if not new_sid:
                # 竞态兜底：预检与绑窗之间调度器 sweep 抢先绑上 → 挂回即可
                fresh = tq.get_task(task_id)
                sid2 = (fresh.get("target_session") or "") if fresh else ""
                row2 = bb.get_session(sid2) if sid2 else None
                if row2 is not None and row2.get("status") != "closed":
                    return {"session_id": sid2, "created": False}
                raise HTTPException(
                    503, "补绑执行窗失败（预算硬闸或 LLM 未就绪），稍后重试")
            return {"session_id": new_sid, "created": True}
        if status not in {"done", "failed"}:
            raise HTTPException(422, f"任务状态为 {status}，无法开窗")
        # 幂等：已有该任务的任务窗（非 closed）→ 直接挂回
        for row in bb.list_sessions(pid):
            if row.get("status") == "closed":
                continue
            meta = row.get("meta")
            meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
            if meta.get("spawn_task_id") == task_id:
                return {"session_id": row["id"], "created": False}
        # sessions_cap 与人手开窗同效
        auto = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
        active = autonomy.count_active_sessions(bb, pid)
        if active >= auto["sessions_cap"]:
            raise HTTPException(
                409, f"活跃会话已达项目上限 sessions_cap={auto['sessions_cap']}"
                     f"（当前 {active} 个非 closed 会话）；请先关窗或在直播间调高上限")
        exec_llm, plan_llm = _llms()
        # 角色沿用原认领者（读不到/会话已关 → 通用角色）
        role = "_generalist"
        if task.get("claimed_by"):
            origin_sess = bb.get_session(task["claimed_by"])
            if origin_sess and origin_sess.get("role"):
                role = origin_sess["role"]
        factory = _registered_session_factory(pid, exec_llm, plan_llm)
        try:
            agent = factory(role, session_name=f"任务窗·{task['objective'][:12]}")
        except FileNotFoundError as e:
            raise HTTPException(422, str(e))
        sid = agent.session["id"]
        bb.set_session_meta(sid, {"spawn_task_id": task_id, "worker_armed": False})
        # 上下文注入：E8 human_note 通道，worker 起跑后首个控制点 drain
        text = _task_context_digest(bb, pid, task)
        try:
            bb.post_human_note(pid, sid, text)
        except ValueError:  # noqa: BLE001 —— 刚创建的会话不会不存在；防御性吞掉
            pass
        bb.append_event(
            pid, "session.spawned",
            {"role": role, "session_id": sid, "origin": "task-window",
             "task_id": task_id},
            session_id=sid, author="system")
        return {"session_id": sid, "created": True}

    def _session_job_running(sid: str) -> bool:
        return any(j["status"] == "running" and j["kind"] == "agent-work"
                   and j["meta"].get("session_id") == sid
                   for j in app.state.jobs.all_jobs())

    def _do_close_session(sid: str) -> dict:
        """关窗收尾（§6.4 人类插手通道；F9 从 close_session 抽出供排水复用）：
        status='closed' + session.closed 事件；编排不再复用该窗口（从
        app.state.agents 摘除），黑板数据保留。调用方须先确认无在跑 worker
        （排水路径由 worker 在任务收尾后自调，天然满足）。"""
        agent = app.state.agents.get(sid)
        if agent is not None and agent._resume_state:  # 暂停快照任务仍 claimed → 先收尾防占坑
            tid = agent._resume_state.get("task_id")
            if tid:
                try:
                    TaskQueue(_project(agent.project_id).bb).fail(
                        tid, sid, "会话关闭，任务未完成")
                except Exception:  # noqa: BLE001
                    pass
        if agent is not None:
            # E12：closed 会话不可 rehydrate（_ensure_agent 404），快照必成孤儿 → 显式清理
            agent._clear_snapshot()
        pid = _pid_of_session(sid)
        try:
            # v18 关窗退回公共池：该窗指派的 open 任务清 target_session（closed 窗
            # 永远认领不到任务，不退回就是永久饿死）；失败不阻关窗。
            TaskQueue(_project(pid).bb).unassign_session(sid)
        except Exception:  # noqa: BLE001
            log.exception("unassign_session 失败 sid=%s（关窗继续）", sid)
        try:
            _project(pid).bb.close_session(sid)
        except ValueError as e:
            raise HTTPException(422, str(e))
        app.state.agents.pop(sid, None)
        try:
            # v0.71 任务即窗口：关待执行窗后其 open 任务已退回无绑，
            # 自动挡立即调度重绑新窗（L0/暂停下留待 sweep/下次触发）
            _schedule(pid, "session-closed")
        except Exception:  # noqa: BLE001
            log.exception("关窗后调度失败 pid=%s", pid)
        return {"session_id": sid, "status": "closed"}

    @app.post("/api/sessions/{sid}/close")
    def close_session(sid: str):
        """关窗=结束会话（2026-09-19 改硬中断，F9 优雅排水退役）：worker 在跑时
        request_abort + 置 close_pending——当前步做完任务 fail（人工中断，快照
        保留可续跑），worker 退出循环后经 close_pending 检查点自关；空闲时立即
        关。前端页签 × 是唯一入口（会话控制组已删）。"""
        pid = _pid_of_session(sid)
        if _session_job_running(sid):
            bb = _project(pid).bb
            _ensure_agent(pid, sid).request_abort()
            bb.set_session_meta(sid, {"close_pending": True, "worker_armed": False})
            bb.append_event(
                pid, "session.work_state",
                {"session_id": sid, "armed": False, "close_pending": True,
                 "abort": True},
                session_id=sid, author="human")
            return {"session_id": sid, "status": "closing"}
        return _do_close_session(sid)

    @app.delete("/api/sessions/{sid}")
    def delete_session(sid: str):
        """物理删除会话窗（2026-09-26 新壳侧栏悬停删除）：仅 closed 可删——活窗
        必须先 POST /close 正规关窗（排水/摘 agents/open 任务退回公共池都在关窗
        路径上）；这里只做黑板行级清除，events 留审计（session.deleted）。不可逆。"""
        pid = _pid_of_session(sid)
        try:
            return _project(pid).bb.delete_session(sid)
        except ValueError as e:
            raise HTTPException(422, str(e))

    # ---------- 会话控制（DESIGN.md §3：暂停/恢复/中断） ----------

    @app.post("/api/sessions/{sid}/pause")
    def pause_session(sid: str):
        """软暂停：worker 在 LLM 步边界退出。
        空闲会话（无 job 且无认领任务）：已武装只解除武装（F9，不落 paused 不发
        session.paused）；未武装无事可暂停 → 409（修「空闲点暂停也落已暂停」）。"""
        pid = _pid_of_session(sid)
        bb = _project(pid).bb
        agent = _ensure_agent(pid, sid)
        if agent.paused:
            raise HTTPException(409, "会话已处于暂停态")
        job_running = _session_job_running(sid)
        if not job_running and agent.dispatcher.current_task_id is None:
            meta_raw = (bb.get_session(sid) or {}).get("meta")
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
            if not meta.get("worker_armed"):
                raise HTTPException(409, "会话空闲，无需暂停")
            bb.set_session_meta(sid, {"worker_armed": False})
            bb.append_event(pid, "session.work_state",
                            {"session_id": sid, "armed": False},
                            session_id=sid, author="human")
            return {"session_id": sid, "status": "idle", "disarmed": True}
        agent.request_pause()
        if job_running:
            # v0.64：暂停请求即时落盘断点快照（尾部 sanitize）——软暂停要等步
            # 边界才由 worker 落盘，期间关后端会丢现场；先落一份兜底，worker 到
            # 边界后以干净现场覆盖（幂等）。
            agent.pause_snapshot_now()
        if not job_running:
            agent._enter_paused()  # 认领了任务但 worker 已退（孤儿认领）：无线程消费检查点，直接落 paused
        # F9：暂停 = 解除武装（armed 是自动接任务的常驻开关；显式「跑」才重新点亮）
        bb.set_session_meta(sid, {"worker_armed": False})
        return {"session_id": sid, "status": "paused"}

    @app.post("/api/sessions/{sid}/resume")
    def resume_session(sid: str, body: SessionResumeIn | None = None):
        """恢复：清控制标志；有快照则起新 agent-work job 续跑被暂停的任务。
        E8：可附引导语（随快照注入 user 消息）；预算暂停缺省自动 +200 步
        （extra_steps 可覆盖，0=不增补），并落 step.budget_extended 审计。"""
        agent = _ensure_agent(_pid_of_session(sid), sid)
        if not agent.paused:
            raise HTTPException(409, "会话未处于暂停态")
        agent.paused = False
        agent._pause_req.clear()
        agent._abort_req.clear()
        pid = agent.project_id
        bb = _project(pid).bb
        st = agent._resume_state
        if st is not None and st.get("reason") == "budget":
            extra = 200 if body is None or body.extra_steps is None else body.extra_steps
            if extra > 0:
                old = agent.dispatcher.max_steps
                agent.dispatcher.max_steps = old + extra
                bb.append_event(
                    pid, "step.budget_extended",
                    {"session_id": sid, "task_id": st.get("task_id"),
                     "old_max": old, "new_max": old + extra, "by": "human"},
                    session_id=sid, author="human")
        if body and body.note and st is not None:
            st["messages"].append(
                {"role": "user", "content": f"💬 人类引导：{body.note.strip()}"})
        bb.append_event(pid, "session.resumed", {"session_id": sid},
                        session_id=sid, author="human")
        # F9：恢复是显式「跑」动作 → 重新点亮武装（有快照继续跑，无快照回到待命可接单）
        bb.set_session_meta(sid, {"worker_armed": True, "close_pending": None})
        if st is not None:
            bb.set_session_status(sid, "running")
            _submit_worker(pid, agent, origin="human-resume")
        else:
            bb.set_session_status(sid, "idle")  # 空闲暂停：无快照可续，回到待命
        return {"session_id": sid, "status": "running"}

    @app.post("/api/sessions/{sid}/abort")
    def abort_session(sid: str):
        """硬中断：当前步做完 → 任务 fail（人工中断）→ 会话空闲。"""
        agent = _ensure_agent(_pid_of_session(sid), sid)
        agent.request_abort()
        if not _session_job_running(sid):
            agent._abort_current_task()  # 暂停态/空闲：没有线程会消费检查点
        return {"session_id": sid, "status": "aborted"}

    # ---------- 会话收件箱（§6.7 的 1.5/1.6：知会私信，独立于审批收件箱） ----------

    @app.get("/api/sessions/{sid}/inbox")
    def session_inbox(sid: str, unread: bool = False):
        pid = _pid_of_session(sid)
        return _project(pid).bb.inbox_list(pid, sid, unread_only=unread)

    @app.post("/api/sessions/{sid}/inbox/read")
    def session_inbox_read(sid: str, body: InboxRead | None = None):
        pid = _pid_of_session(sid)
        n = _project(pid).bb.inbox_mark_read(pid, sid, body.ids if body else None)
        return {"marked": n}

    @app.post("/api/sessions/{sid}/note", status_code=201)
    def post_session_note(sid: str, body: SessionNoteIn):
        """人工引导通道（E8）：human_note 私信直达会话，worker 步边界 drain
        注入「💬 人类引导：…」user 消息（不打断当前工具调用）；暂停期投递的
        引导在恢复随快照一并注入。页签红点/已读/事件流审计全复用。
        返回 wake（2026-09-27）=后端实际处置：resumed=暂停会话被引导唤醒走
        恢复语义；kicked=空闲踢对话轮；queued=窗内有活滞留收件箱等注入；
        deferred=轮进行中维持轮末注入。"""
        pid = _pid_of_session(sid)
        text = body.text.strip()
        att_refs = _attachment_refs(pid, body.attachment_ids)
        if not text and not att_refs:
            raise HTTPException(422, "引导内容不能为空（文本与附件至少一项）")
        try:
            r = _project(pid).bb.post_human_note(pid, sid, text,
                                                 attachments=att_refs)
        except ValueError as e:
            raise HTTPException(404, str(e))
        if r is None:
            raise HTTPException(409, "会话已关闭，无法投递引导")
        # 空闲对话轮（2026-09-19；v24 会话中心化口径）：无 worker 在跑时——armed
        # 窗一律踢（自动接单语义）；未武装窗仅当**窗内无 open/claimed 委托**才踢
        # （宁严勿松：不能让一条聊天消息替未批准的委托起跑），窗内有活则引导滞留
        # 收件箱、等窗起跑轮注入。轮进行中投递的引导维持轮末注入。
        #
        # 暂停会话（2026-09-27 修「引导石沉大海」）：此前 armed 窗被暂停时不看
        # paused 直接踢 worker——rehydrate 载回快照即 paused=True，run_session
        # 暂停闸静默空退（无事件无日志无 LLM），引导滞留收件箱永无回应。人工
        # 引导本身即显式人手动作 → 走 ▶继续 恢复语义：引导已入收件箱，恢复轮
        # drain 随注；预算暂停比照 resume 缺省 +200 步（chat 轮步数取
        # min(dispatcher, chat_max_steps)，不增补则连回应步都没有）。
        resp = {"note_id": r["id"], "session_id": sid}
        if not _session_job_running(sid):
            agent = _ensure_agent(pid, sid)
            if agent.paused:
                agent.paused = False
                agent._pause_req.clear()
                agent._abort_req.clear()
                bb = _project(pid).bb
                st = agent._resume_state
                if st is not None and st.get("reason") == "budget":
                    old = agent.dispatcher.max_steps
                    agent.dispatcher.max_steps = old + 200
                    bb.append_event(
                        pid, "step.budget_extended",
                        {"session_id": sid, "task_id": st.get("task_id"),
                         "old_max": old, "new_max": old + 200, "by": "human-note"},
                        session_id=sid, author="human")
                bb.append_event(pid, "session.resumed",
                                {"session_id": sid, "by": "human-note"},
                                session_id=sid, author="human")
                # F9：恢复是显式「跑」动作 → 重新点亮武装（比照 resume 端点）
                bb.set_session_meta(sid, {"worker_armed": True, "close_pending": None})
                if st is not None:
                    bb.set_session_status(sid, "running")
                _submit_worker(pid, agent, origin="human-note-resume")
                resp["wake"] = "resumed"
                return resp
            tq = TaskQueue(_project(pid).bb)
            chat_safe = bool(_session_meta(_project(pid).bb, sid)
                             .get("worker_armed")) \
                or not tq.session_has_live_work(pid, sid)
            if chat_safe:
                _submit_worker(pid, agent, origin="human-note")
                resp["wake"] = "kicked"
            else:
                resp["wake"] = "queued"
        else:
            resp["wake"] = "deferred"
        return resp

    @app.post("/api/sessions/{sid}/role")
    def switch_session_role(sid: str, body: SessionRoleIn):
        """会话级换智能体（会话中心化 §4.4，2026-09-25）：会话行身份更新 +
        对在内存会话热换装（下个步边界重建 system/工具白名单）——对话历史与
        黑板全保留、新身份跨委托持续。closed 窗 409；专家不在池 422。"""
        pid = _pid_of_session(sid)
        role = body.role.strip()
        if not role:
            raise HTTPException(422, "role 不能为空")
        proj = _project(pid)
        if not expert_exists(app.state.packs_root, role, proj.track):
            raise HTTPException(422, f"专家不在池内或不可服务该轨: {role}")
        try:
            row = proj.bb.set_session_role(sid, role)
        except ValueError as e:
            raise HTTPException(409, str(e))
        agent = app.state.agents.get(sid)
        if agent is not None:
            try:
                agent.switch_session_role(role)
            except Exception:  # noqa: BLE001
                log.exception("会话级换人热换装失败 sid=%s", sid)
        return row

    def _pid_of_session(sid: str) -> str:
        for proj in store.list_projects():
            try:
                p = _project(proj["id"])
            except HTTPException:
                continue
            if p.bb.conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                return proj["id"]
        raise HTTPException(404, f"会话不存在: {sid}")

    # ---------- Agent 会话（开窗） ----------

    @app.get("/api/projects/{pid}/roles")
    def list_roles(pid: str):
        """专家清单（WebUI 开窗下拉 / 编排 allowed_roles 多选的数据源）。
        expert-pool M2：数据源=experts/ 池按项目轨过滤，响应键保持角色视图兼容
        （role=专家 id，name=中文显示名），前端零改动；M3 换将页跟进。"""
        track = _project(pid).track
        out = []
        for name in list_experts(app.state.packs_root, track):
            e = load_expert(app.state.packs_root, name, track)
            out.append({"role": name, "name": e.get("name") or name,
                        "description": e.get("description"),
                        "persona": e.get("persona"),
                        "task_types": e.get("task_types"),
                        "default_noise": e.get("default_noise"),
                        "tools": e.get("tools"),
                        "max_runtime": e.get("max_runtime"),
                        "max_steps": e.get("max_steps")})
        return out

    # ---------------- 网关策略快照（gateway-config-view M1，2026-09-23）----------------
    # 单一事实源仍是代码：本端点做代码→JSON 映射，改规则必须走代码+测试，页面永不漂移。

    @app.get("/api/gateway/config")
    def gateway_config():
        """执行网关策略快照（设置页「网关」页签数据源，DESIGN §7）。
        runtime/threat/net 直出 policy 常量；pathguard 为手工语义摘要（测试断言
        条数防遗漏）；rate_rules 直出 rateguard.RATE_RULES 表。"""
        from core.runtime import pathguard, policy, rateguard
        runtime_levels = [
            {"name": "host", "level": 0, "label": "宿主原生",
             "desc": "本项目自身代码、静态分析"},
            {"name": "wsl", "level": 1, "label": "WSL2 半可信",
             "desc": "与宿主同信任级（不可信代码不因 wsl 降险）"},
            {"name": "docker", "level": 2, "label": "普通容器",
             "desc": "不可信代码默认环境"},
            {"name": "sandbox", "level": 3, "label": "加固沙箱",
             "desc": "活体恶意样本专用（L3+fakenet）"},
        ]
        threat_matrix = [
            {"threat_class": tc, "allowed": sorted(policy.allowed_runtimes(tc)),
             "note": note}
            for tc, note in [
                ("trusted", "可信：本项目自身代码与静态分析"),
                ("untrusted", "不可信：来源不明代码/下载工具，只允许容器"),
                ("unknown", "未知按恶意样本处理（宁严勿松，§7 安全默认值）"),
                ("malware_live", "活体恶意样本：仅 L3 加固沙箱"),
            ]
        ]
        pathguard_rules = [
            "只拦写不拦读（核心诉求=产物归置，读另行审计）",
            "PowerShell 写 cmdlet 清单：out-file / set-content / add-content / "
            "tee-object / new-item / export-csv / export-clixml / "
            "export-pfxcertificate / set-variable",
            "参数式写目标同拦：curl/wget/iwr 的 -o / --output / -OutFile 及 "
            "nmap 风格 -oG/-oN 连写、bash tee",
            "重定向 > >> 1> 2> 1>> 2>> 同拦",
            "豁免特殊目标：&1 &2 /dev/null nul con $null",
            "变量间接静态不可判：~ 一律视为逃逸；$env: / %VAR% 放行"
            "（TEMP/TMP 已被网关重定向进项目）",
            "相对路径按 cwd=scratch 解析，越出 scratch 即拒；正式产物走 bb_add_artifact",
        ]
        return {
            "runtime_levels": runtime_levels,
            "threat_matrix": threat_matrix,
            "net_modes": {"modes": ["none", "fakenet", "real"],
                          "default": policy.DEFAULT_NET_MODE,
                          "note": "real 永不默认，须人工审批；fakenet 为后续里程碑"},
            "pathguard_rules": pathguard_rules,
            "rate_rules": [
                {"tool": tool, "requirement": r["requirement"],
                 "params": r["params"], "hint": r["hint"]}
                for tool, r in rateguard.RATE_RULES.items()
            ],
            "exec_params": {"default_timeout": 120,
                            "sandbox_image": "python:3.11-alpine"
                            "（占位，专用分析镜像后续经 tools/ 管理）"},
        }

    @app.post("/api/gateway/probe")
    def gateway_probe():
        """手动重跑宿主能力探测，替换 app.state.inventory 并返回新清单
        （结构与 GET /api/projects/{pid}.capability 完全一致）。同步执行，
        最坏 ~20s（docker+wsl 各 10s 超时），仅用户主动点击触发；刷新即生效
        ——后续新开窗经 app.state.inventory 引用取到新清单。"""
        app.state.inventory = HostDetector().probe(tools_root=app.state.tools_root)
        return json.loads(app.state.inventory.to_json())

    @app.get("/api/agent-tools")
    def agent_tools():
        """Agent 工具目录（设置页「工具」页签数据源，agent-tools-view 2026-09-24）：
        直出 AGENT_TOOLS 静态全集 + 静态分组；无项目依赖、无 IO、无敏感字段。"""
        from core.agent.tools import (
            AGENT_TOOLS, TOOL_GROUPS, agent_tool_group)
        return {
            "groups": TOOL_GROUPS,
            "tools": [
                {"name": t["name"], "description": t["description"],
                 "group": agent_tool_group(t["name"]),
                 "input_schema": t.get(
                     "input_schema", {"type": "object", "properties": {}})}
                for t in AGENT_TOOLS
            ],
        }

    @app.get("/api/models")
    def list_models():
        """模型清单（开窗下拉数据源）：路由默认 ∪ 实测可用清单（DESIGN.md §8）。
        providers/default 供两级下拉（供应商→模型）。"""
        try:
            router = ModelRouter()
            vals = {router.model_for(r) for r in KNOWN_ROLES}
        except Exception:  # noqa: BLE001 —— 配置损坏时仍返回实测清单
            vals = set()
        vals.update(AVAILABLE_MODELS)
        llm_store: ProviderStore = app.state.llm_store
        providers = llm_store.masked()
        for p in providers:
            vals.update(p["models"])
        try:
            dname, dmodel = llm_store.default_target()
            default = {"provider": dname, "model": dmodel}
        except ProviderError:
            default = None
        return {"models": sorted(vals), "providers": providers, "default": default}

    # ---------- LLM 供应商管理（§8） ----------

    @app.get("/api/llm/providers")
    def list_llm_providers():
        llm_store: ProviderStore = app.state.llm_store
        try:
            dname, dmodel = llm_store.default_target()
            default = {"provider": dname, "model": dmodel}
        except ProviderError:
            default = None
        return {"providers": llm_store.masked(), "default": default,
                "default_provider": llm_store.default_provider_name()}

    @app.put("/api/llm/providers")
    def save_llm_providers(body: ProvidersIn):
        llm_store: ProviderStore = app.state.llm_store
        try:
            llm_store.save([m.model_dump() for m in body.providers],
                           default_provider=body.default_provider)
        except ProviderError as e:
            raise HTTPException(422, str(e))
        return list_llm_providers()

    @app.post("/api/llm/discover")
    def discover_llm_models(body: DiscoverIn):
        """获取供应商支持的模型：先试 /v1/models；不支持则候选清单探活（真实最小调用）。
        已保存供应商给 name；未保存的新供应商给 base_url+api_key。"""
        llm_store: ProviderStore = app.state.llm_store
        try:
            if body.name:
                return llm_store.discover(body.name)
            if body.base_url and body.api_key:
                return llm_store.discover_credentials(body.base_url, body.api_key)
            raise ProviderError("须提供已保存供应商 name，或 base_url+api_key")
        except ProviderError as e:
            raise HTTPException(422, str(e))
        except Exception as e:  # noqa: BLE001 —— 探活网络错误等
            raise HTTPException(502, f"模型发现失败: {e}") from e

    @app.post("/api/llm/test-model")
    def test_llm_model(body: TestModelIn):
        """手填模型测活（200 + ok 布尔，错误内联返回，供 UI 就地提示）。"""
        llm_store: ProviderStore = app.state.llm_store
        try:
            if body.name:
                p = llm_store.get(body.name)
                base_url = p["base_url"]
                api_key = body.api_key or llm_store.resolve_key(p)
            else:
                if not body.base_url or not body.api_key:
                    return {"ok": False, "error": "未保存供应商须填 base_url 与 api_key"}
                base_url, api_key = body.base_url, body.api_key
            probe_credentials(base_url, api_key, body.model)
        except ProviderError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True}

    @app.post("/api/agents/{sid}/llm")
    def switch_agent_llm(sid: str, body: SwitchLlmIn):
        """在跑会话动态切换供应商/模型：替换 provider 引用，下一次 LLM 调用即生效。
        经 _ensure_agent（2026-09-19）：后端重启内存注册表空时按黑板行 rehydrate，
        与其他会话控制端点同语义——修「重启后切模型 404 会话不存在」。"""
        agent = _ensure_agent(_pid_of_session(sid), sid)
        try:
            new_llm = app.state.llm_store.build(body.provider, body.model)
        except ProviderError as e:
            raise HTTPException(422, str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"供应商/模型不可用（{e}）") from e
        agent.llm = new_llm
        # 模型声明上下文随切换即时生效（与开窗同口径，loop.apply_context_budget）
        from core.agent.loop import apply_context_budget
        apply_context_budget(agent.config, new_llm)
        agent.bb.append_event(
            agent.project_id, "llm.switched",
            {"session_id": sid, "provider": body.provider, "model": new_llm.model},
            session_id=sid, author="human")
        return {"status": "ok", "provider": body.provider, "model": new_llm.model}

    # ---------- 项目级 executor 模型覆写（TRAE 新壳 M3，2026-09-25） ----------

    def _default_executor_target() -> dict | None:
        """路由缺省 executor（ModelRouter 覆写 ∪ ProviderStore 默认）。"""
        try:
            t = ModelRouter().target_for("executor")
            if t is not None:
                return {"provider": t[0], "model": t[1]}
        except Exception:  # noqa: BLE001 —— 配置损坏时回落供应商默认
            pass
        try:
            dname, dmodel = app.state.llm_store.default_target()
            return {"provider": dname, "model": dmodel}
        except ProviderError:
            return None

    def _executor_override_of(pid: str) -> dict | None:
        cfg = _project(pid).bb.get_project(pid)["config"] or {}
        ov = cfg.get("executor_llm")
        if isinstance(ov, dict) and str(ov.get("provider") or "").strip():
            return {"provider": str(ov["provider"]).strip(),
                    "model": str(ov.get("model") or "").strip()}
        return None

    def _apply_executor_llm_live(pid: str, new_llm, *, provider: str,
                                 model: str, scope: str) -> list[str]:
        """把新 llm 装到本项目全部在内存的会话上（下次 LLM 调用即生效；
        与全局 llm 跨会话共享同实例的既有口径一致），落一条 llm.switched 审计。"""
        from core.agent.loop import apply_context_budget
        touched: list[str] = []
        for sid, agent in list(app.state.agents.items()):
            if agent.project_id != pid:
                continue
            agent.llm = new_llm
            apply_context_budget(agent.config, new_llm)
            touched.append(sid)
        if touched:
            _project(pid).bb.append_event(
                pid, "llm.switched",
                {"session_ids": touched, "provider": provider, "model": model,
                 "scope": scope}, author="human")
        return touched

    @app.get("/api/projects/{pid}/executor-llm")
    def get_project_executor_llm(pid: str):
        """executor 模型三态视图：default=路由缺省 / override=项目覆写 /
        effective=当前实际（chip 默认值/覆盖/重置的数据源）。"""
        default = _default_executor_target()
        override = _executor_override_of(pid)
        return {"default": default, "override": override,
                "effective": override or default}

    @app.put("/api/projects/{pid}/executor-llm")
    def set_project_executor_llm(pid: str, body: SwitchLlmIn):
        """项目 executor 覆写：先实测构建供应商/模型（坏值 422 不落盘），
        经项目 config 单一写口持久化（project.json+黑板双写），并对在内存会话
        即时换装。"""
        proj = _project(pid)
        try:
            new_llm = app.state.llm_store.build(body.provider, body.model)
        except ProviderError as e:
            raise HTTPException(422, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"供应商/模型不可用（{e}）") from e
        override = {"provider": body.provider, "model": new_llm.model}
        store.update_config(pid, {"executor_llm": override}, project=proj)
        touched = _apply_executor_llm_live(
            pid, new_llm, provider=body.provider, model=new_llm.model,
            scope="project-executor-override")
        return {"status": "ok", "override": override, "effective": override,
                "touched_sessions": touched}

    @app.delete("/api/projects/{pid}/executor-llm")
    def reset_project_executor_llm(pid: str):
        """重置：剥 config.executor_llm（恢复跟随路由缺省），在内存会话换装回
        缺省 executor。"""
        proj = _project(pid)
        store.update_config(pid, {"executor_llm": None}, project=proj)
        default = _default_executor_target()
        new_llm = None
        if default is not None:
            try:
                new_llm = app.state.llm_store.build(
                    default["provider"], default["model"] or None)
            except Exception:  # noqa: BLE001 —— 缺省构建失败保留各窗现状
                new_llm = None
        touched: list[str] = []
        if new_llm is not None and default is not None:
            touched = _apply_executor_llm_live(
                pid, new_llm, provider=default["provider"],
                model=default["model"], scope="project-executor-reset")
        return {"status": "ok", "default": default, "override": None,
                "effective": default, "touched_sessions": touched}

    @app.post("/api/projects/{pid}/agents", status_code=201)
    def spawn_agent(pid: str, body: AgentIn):
        proj = _project(pid)
        # sessions_cap 是资源硬上限（§6.8）：人手开窗与编排开窗同效，超限 409
        row = proj.bb.get_project(pid)
        auto = autonomy.autonomy_of(row["config"], track=proj.track)
        active = autonomy.count_active_sessions(proj.bb, pid)
        if active >= auto["sessions_cap"]:
            raise HTTPException(
                409, f"活跃会话已达项目上限 sessions_cap={auto['sessions_cap']}"
                     f"（当前 {active} 个非 closed 会话）；请先关窗或在直播间调高上限")
        exec_llm, plan_llm = _llms()
        if body.provider or body.model:
            try:
                # 仅给 model → 默认供应商；仅给 provider → 其默认模型
                exec_llm = app.state.llm_store.build(body.provider, body.model)
            except ProviderError as e:
                raise HTTPException(422, str(e)) from e
            except Exception as e:  # noqa: BLE001 —— key 缺失等
                raise HTTPException(503, f"供应商/模型不可用（{e}）") from e
        factory = _registered_session_factory(pid, exec_llm, plan_llm)
        try:
            agent = factory(body.role, session_name=body.session_name,
                            max_steps=body.max_steps)
        except FileNotFoundError as e:
            raise HTTPException(422, str(e))
        # 触发点 C（批 5；F9 改 armed 闸）：L1/L2 未暂停且显式 armed 时开窗即自动起
        # 一个 worker（空队列零成本退）；人手开窗默认不接任务，点「跑任务队列」启动；
        # L0 不自起；paused 时只开窗不消费。
        extra: dict[str, Any] = {}
        proj.bb.set_session_meta(agent.session["id"], {"worker_armed": body.armed})
        # 会话中心化：人开窗与编排开窗同审计（此前 POST /agents 无 session.spawned，
        # 事件流查不到人开的窗）；先于 worker 起跑事件。
        proj.bb.append_event(
            pid, "session.spawned",
            {"role": agent.session["role"], "session_id": agent.session["id"],
             "origin": "human"},
            session_id=agent.session["id"], author="human")
        if body.armed and auto["level"] in {"L1", "L2"} and not auto["paused"]:
            extra["job_id"] = _submit_worker(
                pid, agent, auto=True, origin="human-spawn")  # 触发点 C
        # 预算对人手动作仅警告不拦截（硬闸只拦编排自主动作，§6.8）
        warning = autonomy.human_warning(proj.bb, pid)
        if warning:
            extra["warning"] = warning
        return {**agent.session, **extra, "worker_armed": body.armed}

    @app.get("/api/agents")
    def list_agents():
        return [a.session for a in app.state.agents.values()]

    @app.post("/api/agents/{sid}/work")
    def run_agent_work(sid: str):
        # F9 启动口：显式点亮武装（清排水标记）+ 起一个 worker；已在跑则去重不重复起
        pid = _pid_of_session(sid)
        bb = _project(pid).bb
        bb.set_session_meta(sid, {"worker_armed": True, "close_pending": None})
        if _session_job_running(sid):
            return {"session_id": sid, "already_running": True}
        # 重启后/历史孤儿窗：内存未命中时按黑板 sessions 行 rehydrate 再开跑
        agent = _ensure_agent(pid, sid)
        job_id = _submit_worker(pid, agent, origin="human-work")
        return {"job_id": job_id, "session_id": sid}

    def _worker_loop(agent: AgentSession, *, manual: bool = False,
                     tail: dict | None = None) -> Callable[[], int]:
        pid = agent.project_id
        tail = tail if tail is not None else {}

        def _paused() -> bool:
            # autonomy.paused 实时重读：在跑 worker 做完当前任务后不再认领新任务
            try:
                return bool(autonomy.autonomy_of(
                    agent.bb.get_project(pid)["config"], track=agent.track)["paused"])
            except Exception:  # noqa: BLE001 —— 读配置失败不该杀死 worker，照常认领
                return False

        def run() -> int:
            done = 0
            stopped_by_pause = False
            while True:
                # F9 优雅关窗：排水标记命中（close_pending）→ 不再接活，自关后退出。
                # 检查点在委托收尾之后、下一轮会话轮之前——当前委托完整跑完。
                try:
                    meta_raw = (agent.bb.get_session(agent.session["id"])
                                or {}).get("meta")
                    meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                    if meta.get("close_pending"):
                        _do_close_session(agent.session["id"])
                        break
                except Exception:  # noqa: BLE001 —— 读 meta 失败不该杀死 worker
                    pass
                # paused 只约束自动消费（C/kick/批准即跑）；人显式「跑队列」/恢复
                # 是人工 override，暂停下照常接活（DESIGN §6.8 行为表：人手动作照常）
                if not manual and _paused():
                    stopped_by_pause = True
                    break
                try:
                    # 会话轮（2026-09-25 会话中心化）：有委托干活 / 无委托对话，
                    # 工具面一致；委托做完 while 再入自动接窗内下一件；都没有
                    # 返回 None 空退、窗回待命。
                    turned = agent.run_session()
                    if turned is None:
                        break
                except Exception as exc:  # noqa: BLE001 —— 委托级兜底在 _fail_task_on_error
                    # （起跑/收尾阶段的意外异常也不得静默杀死 worker 线程：
                    #   委托悬 claimed + 孤儿心跳续租，看板永远「执行中」）
                    # 2026-09-26：真实异常文案+会话归属进 llm.error——429 配额
                    # 耗尽杀死的对话轮此前只留笼统文案且无 session_id，用户侧
                    # 表现为「已发送→正在回复→石沉大海」。
                    log.exception("worker 循环异常退出 pid=%s sid=%s", pid, agent.session["id"])
                    _emit_llm_error(pid, "worker", exc, session_id=agent.session["id"])
                    break
                done += 1
            # 批 5（§6.8）：自动 worker 遇暂停退出 → 停链；队列空退出
            # （last_claim_idle）→ 触发点 A 尝试续 L2 链（闸门②会拦住 paused 链）。
            if stopped_by_pause:
                _stop_chain(pid, "paused")
            elif agent.last_claim_idle:
                _maybe_mission_auto_tick(pid, reason=f"worker-idle:{agent.session['id']}")
                _maybe_auto_tick(pid, reason=f"worker-idle:{agent.session['id']}")
                # 自动补窗（2026-09-20）：空退≠黑板无 open——role-bound 任务当前
                # worker 认领不了会留在队列，正好在此按任务角色补窗
                _schedule(pid, reason=f"worker-idle:{agent.session['id']}")  # v0.71 调度器
                # A5：L2 下队列空转也是重排时机（子代理可能刚发了子任务）。
                # 尾部触发若被在跑编排动作挤掉（busy），才让 on_done 补一次——
                # 否则同一事件在 on_done 再踩 30s 节流，会凭空排长命 wait job
                # 占住编排槽（人工 tick 会被误 409、自动链被堵 30s）。
                tail["replan_busy"] = _maybe_replan(
                    pid, reason=f"worker-idle:{agent.session['id']}") == "busy"
            return done
        return run

    def _maybe_auto_resume(pid: str, agent: AgentSession) -> None:
        """C4：L2 档步数耗尽（budget_paused）自动续跑——不等人工，缺省 +200 步。
        闸（每次实时重读）：档位 ==L2、全局未暂停、token 预算未硬阻断（token 即
        总闸，不另设次数上限）；任一不满足保持 paused 等人工「▶ 继续」。
        awaiting_human 挂起不受此路径影响（C1 宁严勿松：不自动处置）。"""
        try:
            st = agent._resume_state
            if not (agent.paused and st and st.get("reason") == "budget"):
                return
            cfg = _auto_cfg(pid)
            if cfg["level"] != "L2" or cfg["paused"]:
                return
            tok = autonomy.usage_view(agent.bb, pid).get("tokens", {})
            if tok.get("pct") is not None and tok["pct"] >= 100:
                return  # token 预算硬闸：保持暂停等人工
            sid = agent.session["id"]
            agent.paused = False
            agent._pause_req.clear()
            agent._abort_req.clear()
            old = agent.dispatcher.max_steps
            agent.dispatcher.max_steps = old + 200
            agent.bb.append_event(
                pid, "step.budget_extended",
                {"session_id": sid, "task_id": st.get("task_id"),
                 "old_max": old, "new_max": old + 200, "by": "l2-auto"},
                session_id=sid, author="l2-auto")
            agent.bb.append_event(
                pid, "session.resumed", {"session_id": sid, "by": "l2-auto"},
                session_id=sid, author="l2-auto")
            agent.bb.set_session_status(sid, "running")
            agent.bb.set_session_meta(sid, {"worker_armed": True})  # F9：自动续跑=已启动
            _submit_worker(pid, agent, auto=True, origin="l2-auto-resume")
        except Exception:  # noqa: BLE001 —— 自动续跑失败保持暂停，等人工
            log.exception("C4 L2 自动续跑失败 pid=%s", pid)

    def _submit_worker(pid: str, agent: AgentSession, **extra_meta) -> str:
        """agent-work job 的唯一提交口（批 5）：run() 末尾的 A 触发可能因自身 job 仍
        running 被软去重跳过，这里统一挂 on_done 在状态翻 done 后再评估一次续链，
        兜住「worker 在 tick 收尾期间秒退」的搁浅竞态。meta.auto=True 为自动消费
        （受 paused 约束）；人显式跑队列/恢复（无 auto）不受 paused 认领约束。"""
        sid = agent.session["id"]
        manual = not bool(extra_meta.get("auto"))
        tail = {"replan_busy": False}  # 重排尾部触发是否被在跑编排动作挤掉
        return app.state.jobs.submit(
            "agent-work", _worker_loop(agent, manual=manual, tail=tail),
            meta={"project_id": pid, "session_id": sid, **extra_meta},
            on_done=lambda _j: (
                _maybe_auto_resume(pid, agent),  # C4：L2 下 budget_paused 自动续跑
                _maybe_mission_auto_tick(pid, reason=f"worker-done:{sid}"),  # C2 L1 自动派生
                _maybe_auto_tick(pid, reason=f"worker-done:{sid}"),
                _schedule(pid, reason=f"worker-done:{sid}"),  # v0.71 调度器：终态释放名额起跑队头
                # 重排只补「尾部被挤掉」的那一次；已提交/已起 wait 的不重复触发
                _maybe_replan(pid, reason=f"worker-done:{sid}")
                if tail["replan_busy"] else None,
            ))

    # ---------- L2 全自动链（批 5，DESIGN §6.8：事件驱动，无调度器/无轮询） ----------

    def _auto_cfg(pid: str) -> dict:
        proj = _project(pid)
        return autonomy.autonomy_of(proj.bb.get_project(pid)["config"], track=proj.track)

    def _worker_jobs_running(pid: str) -> list[dict]:
        return [j for j in app.state.jobs.all_jobs()
                if j["status"] == "running" and j["kind"] == "agent-work"
                and j["meta"].get("project_id") == pid]

    def _orch_jobs_running(pid: str) -> bool:
        """tick（手动/自动）/ 重排 / 节流 wait job 统一软去重（A5 含 replan 两类）。"""
        return any(j["status"] == "running"
                   and j["meta"].get("project_id") == pid
                   and j["kind"] in {"orchestrator-tick", "orchestrator-auto-tick",
                                     "orchestrator-auto-wait",
                                     "orchestrator-replan", "orchestrator-replan-wait"}
                   for j in app.state.jobs.all_jobs())

    def _start_chain(pid: str) -> bool:
        """L2 链启动（幂等）：chain_active=1/ticks=0 + 本进程标记 + chain_started。
        DB 活但本进程无标记 = 重启急停后的手动恢复：chain_ticks 清零重算预算，
        以 manual_recovery 重新发 chain_started（否则旧 ticks 可能立即撞顶）。"""
        bb = _project(pid).bb
        st = orch_state.load_or_create(bb, pid)
        if pid in app.state.active_chains and st["chain_active"]:
            return False  # 真幂等：本进程链已活
        reason = ("manual_recovery"
                  if st["chain_active"] and pid not in app.state.active_chains
                  else "manual_tick")
        app.state.active_chains.add(pid)
        orch_state.save_fields(bb, pid, chain_active=1, chain_ticks=0,
                               last_auto_tick_at="")
        bb.append_event(pid, "orch.chain_started",
                        {"reason": reason}, author="orchestrator")
        return True

    def _stop_chain(pid: str, reason: str, **extra) -> bool:
        """链停止（幂等）：清 chain_active/进程标记 + orch.chain_stopped{reason,...}。"""
        try:
            bb = _project(pid).bb
        except HTTPException:
            return False
        st = orch_state.load_or_create(bb, pid)
        if not st["chain_active"] and pid not in app.state.active_chains:
            return False
        # 顺序关键：先落 DB 不活、后摘进程标记。若反过来，两步之间别的线程做重启
        # 判定会看到「标记缺 + DB 仍活」误落 restart（本顺序的中间态是「标记在 +
        # DB 不活」，闸门安全 return，不会误判）。
        orch_state.save_fields(bb, pid, chain_active=0)
        app.state.active_chains.discard(pid)
        bb.append_event(pid, "orch.chain_stopped",
                        {"reason": reason, "chain_ticks": int(st["chain_ticks"]), **extra},
                        author="orchestrator")  # 链停止=编排器事件（前端 __orch 页签按 author 收录）
        return True

    def _kick_workers(pid: str) -> list[str]:
        """给每个没有在跑 worker 的非关闭/非暂停武装会话提交一个 agent-work job。
        无委托无消息的会话轮零成本空退。返回新提交的 sid 列表。
        F9 armed 闸：未启动（meta.worker_armed 非 true）的窗不自动接活——所有
        自动唤醒（D/E/B/tick）统一在此被拦。
        会话中心化（2026-09-25）：不再有「无绑定/绑定终态跳过」——武装 idle 窗
        都可能有窗内排队委托或待回应消息，一律踢；终态「续聊窗」=普通待命窗。"""
        proj = _project(pid)
        submitted: list[str] = []
        for row in proj.bb.list_sessions(pid):
            sid = row["id"]
            if row.get("status") in {"closed", "paused"} or _session_job_running(sid):
                continue
            meta = row.get("meta")
            meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
            if not meta.get("worker_armed"):
                continue  # F9：未启动，不自动接活
            try:
                agent = _ensure_agent(pid, sid)  # 重启后 rehydrate；陈旧 running→idle
            except HTTPException:
                continue
            if agent.paused:
                continue
            _submit_worker(pid, agent, auto=True, origin="kick")
            submitted.append(sid)
        return submitted

    def _auto_tick_age(st: dict) -> float | None:
        """距上一个自动 tick 的秒数；从未自动 tick 返回 None（不节流）。"""
        raw = st.get("last_auto_tick_at") or ""
        if not raw:
            return None
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds()
        except ValueError:
            return None

    def _emit_llm_error(pid: str, source: str, err: Any,
                        session_id: str | None = None) -> None:
        """LLM 调用失败落 llm.error 事件（2026-09-18：此前只有 job error+后端 log，
        额度/限流对用户完全不可见）。同 pid+错误文案 60s 节流防重试风暴刷屏；
        文案命中配额特征加 kind_hint=quota（前端据此提示）。
        session_id（2026-09-26）：带上归属会话——新壳会话流按 session_id 圈定，
        不带则对话轮的死亡对用户仍不可见（只能翻事件流）。"""
        msg = str(err)[:300]
        key = f"{pid}:{msg}"
        now_t = time.monotonic()
        if now_t - app.state.llm_error_seen.get(key, 0.0) < 60.0:
            return
        app.state.llm_error_seen[key] = now_t
        hint = "quota" if re.search(
            r"429|quota|insufficient|余额|配额|rate.?limit", msg, re.I) else ""
        try:
            _project(pid).bb.append_event(
                pid, "llm.error",
                {"source": source, "error": msg,
                 **({"kind_hint": hint} if hint else {})},
                session_id=session_id, author="system")
        except Exception:  # noqa: BLE001 —— 事件落库失败不掩盖原始错误
            log.exception("llm.error 事件落库失败 pid=%s", pid)

    def _maybe_mission_auto_tick(pid: str, reason: str, *,
                                 require_idle: bool = False) -> None:
        """C2 mission 自动派生（§6.9）：L1 档专属补位——auto_derive 开启 + 判据存在
        （goal>mission>模板>内置，永远有判据）+ worker 空退 → 自动编排一轮派生下一批
        任务（开窗仍走 L1 审批）。
        L2 不走此路径（既有自动链已覆盖）。闸全部实时重读；防空转：上轮派生
        tick 零发布且此后无新事件 → 跳过，直到黑板有变化。
        require_idle=True（轮询触发专用）：额外要求任务队列无 open/claimed 行——
        claimed 说明 worker 在跑，其收尾会自然触发，轮询不得抢跑。"""
        try:
            proj = _project(pid)
        except HTTPException:
            return
        try:
            bb = proj.bb
            if require_idle:
                open_rows = [t for t in TaskQueue(bb).list_tasks(pid)
                             if t["status"] in ("open", "claimed")]
                if open_rows:
                    return
            cfg = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
            # 闸①开关显式开启
            if not cfg.get("auto_derive"):
                return
            # 闸②档位：仅 L1 补位（L2 由既有自动链覆盖；L0 全手动）
            if cfg["level"] != "L1" or cfg["paused"]:
                return
            # 闸③判据存在（四层解析：goal > mission 存量 > 所选模板 > 轨内置默认）
            resolved = judgments.resolve_criteria(
                bb.get_project(pid)["config"], app.state.judgments_dir,
                track=proj.track, goal=(proj.meta or {}).get("phase_goal"))
            # 闸④token 预算硬阻
            tok = autonomy.usage_view(bb, pid).get("tokens", {})
            if tok.get("pct") is not None and tok["pct"] >= 100:
                return
            # 闸⑤防空转：上轮派生零发布且黑板无新事件 → 跳过（有新事件即重置）
            st = app.state.mission_derive.setdefault(pid, {"tip": 0, "empty": False})
            tip = bb.latest_event_id(pid)
            if st["empty"] and tip <= st["tip"]:
                return
            st["tip"] = tip
            st["empty"] = True  # tick 发布了任务则由其 on_done 复位
            owner = f"mission-derive-{uuid.uuid4().hex}"
            try:
                orch_state.acquire_tick_lease(bb, pid, owner)
            except orch_state.TickLeaseError:
                return
            try:
                orch = _build_orchestrator(pid, TickIn(), owner)
            except HTTPException:
                orch_state.release_tick_lease(bb, pid, owner)
                return
            bb.append_event(
                pid, "mission.derive",
                {"reason": reason, "criteria_source": resolved["source"]},
                author="orchestrator")

            def _run_mission_tick() -> dict:
                try:
                    result = orch.tick()
                    _post_tick(pid, result, manual=False)
                    return result
                except Exception as exc:
                    _emit_llm_error(pid, "orchestrator", exc)
                    raise
                finally:
                    orch_state.release_tick_lease(bb, pid, owner)

            def _mission_on_done(job: dict) -> None:
                """派生 tick 收尾：结果落 orchestrator_state（last_derive_*，状态灯
                消费）+ mission.derive.result 事件（事件流可见）。error → st.empty
                复位允许下次触发重试（轮询 60s 兜底）；零发布 → 置空转标记（配合
                闸⑤防 LLM 空转循环）。"""
                try:
                    if job.get("status") == "error":
                        err = str(job.get("error") or "unknown")[:200]
                        orch_state.save_fields(
                            bb, pid, last_derive_at=datetime.now(timezone.utc).isoformat(),
                            last_derive_result=f"error:{err}")
                        bb.append_event(pid, "mission.derive.result",
                                        {"error": err}, author="orchestrator")
                        st["empty"] = False
                        return
                    result = job.get("result") or {}
                    published = result.get("published") or []
                    orch_state.save_fields(
                        bb, pid, last_derive_at=datetime.now(timezone.utc).isoformat(),
                        last_derive_result=f"published:{len(published)}")
                    bb.append_event(pid, "mission.derive.result",
                                    {"published": len(published),
                                     "spawned": len(result.get("spawned") or [])},
                                    author="orchestrator")
                    if not published:
                        st["empty"] = True
                        st["tip"] = bb.latest_event_id(pid)
                    else:
                        st["empty"] = False
                except Exception:  # noqa: BLE001 —— on_done 不能炸
                    log.exception("mission 派生收尾失败 pid=%s", pid)

            app.state.jobs.submit("orchestrator-tick", _run_mission_tick,
                                  meta={"project_id": pid},
                                  on_done=lambda _j: (_mission_on_done(_j),
                                                      _maybe_auto_tick(pid, reason=f"mission-done:{pid}"),
                                                      None)[-1])
        except Exception:  # noqa: BLE001 —— 后台线程不能炸
            log.exception("mission 自动派生失败 pid=%s", pid)

    def _mission_poll_sweep() -> None:
        """mission 自动派生兜底轮询（2026-09-18）：判跳原本纯事件驱动
        （worker-idle/worker-done 两个触发点），编排停摆（worker 全部收尾）后
        「任务空自动派生」永不判跳——这里每 mission_poll_interval 秒扫一次全项目，
        队列空 + auto_derive + L1 未暂停 → 补一次判跳（require_idle=True）。
        预算/防空转/租约闸全部在 _maybe_mission_auto_tick 内复用；异常全吞。"""
        try:
            for meta in store.list_projects():
                pid = meta["id"]
                try:
                    if pid in app.state.projects_closing:
                        continue
                    proj = _project(pid)
                    cfg = autonomy.autonomy_of(
                        proj.bb.get_project(pid)["config"], track=proj.track)
                    if (not cfg.get("auto_derive") or cfg["level"] != "L1"
                            or cfg["paused"]):
                        continue
                    _maybe_mission_auto_tick(pid, reason="poll-idle",
                                             require_idle=True)
                except HTTPException:
                    continue  # 删除闸门 409 / LLM 未就绪 503 等直接跳过
                except Exception:  # noqa: BLE001 —— 单项目失败不拖垮 sweep
                    log.exception("mission 轮询判跳失败 pid=%s", pid)
        except Exception:  # noqa: BLE001 —— 后台线程不能炸
            log.exception("mission 轮询 sweep 失败")

    app.state.mission_poll_sweep = _mission_poll_sweep  # 测试直调口
    if mission_poll_interval > 0:
        def _poll_loop() -> None:
            while True:
                time.sleep(mission_poll_interval)
                _mission_poll_sweep()
        threading.Thread(target=_poll_loop, name="mission-poll",
                         daemon=True).start()

    # v0.71 任务窗调度 sweep（绑定段/启动段的兜底轮询）挂在 _schedule 定义之后
    # （见 _schedule_request_approval 后）——此处仅有注释锚点。

    def _maybe_auto_tick(pid: str, reason: str) -> None:
        """自动续 tick 的唯一入口（触发点 A）：只判闸门 + submit job，不直接跑 LLM。
        防失控七闸顺序见 DESIGN §6.8；任何异常吞掉记 log（后台线程不能炸）。"""
        try:
            proj = _project(pid)
        except HTTPException:
            return
        try:
            bb = proj.bb
            cfg = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
            # 闸①档位重读：降级直接停链
            if cfg["level"] != "L2":
                if orch_state.load_or_create(bb, pid)["chain_active"]:
                    _stop_chain(pid, "level_changed")
                return
            # 闸②暂停
            if cfg["paused"]:
                _stop_chain(pid, "paused")
                return
            st = orch_state.load_or_create(bb, pid)
            # 重启=急停：DB 活但本进程无标记 → 落一次 restart 停链，绝不自动续
            if pid not in app.state.active_chains:
                if st["chain_active"]:
                    _stop_chain(pid, "restart")
                return
            if not st["chain_active"]:
                return
            # 闸⑦预算硬闸：两类自主动作都被拦才停（LLM 还能改道时照常续）
            if (autonomy.hard_block_reason(bb, pid, "publish_task")
                    and autonomy.hard_block_reason(bb, pid, "spawn_session")):
                _stop_chain(pid, "budget_blocked")
                return
            # 还有 worker 在跑：最后一个退出的 worker 才续，防过早 tick 误判收敛
            if _worker_jobs_running(pid):
                return
            # 闸⑤软去重：tick / wait job 在跑（硬去重 = 下面 job 内抢 tick 租约）
            if _orch_jobs_running(pid):
                return
            # 闸③链预算
            if int(st["chain_ticks"]) >= int(cfg["max_chain_ticks"]):
                _stop_chain(pid, "max_chain_ticks")
                return
            # 闸⑥节流：踩间隔内 → wait job 睡满重入（不丢触发，防链搁浅）
            age = _auto_tick_age(st)
            if age is not None and age < AUTO_TICK_MIN_INTERVAL:
                delay = AUTO_TICK_MIN_INTERVAL - age
                # 重入必须挂 on_done（runner 内重入会因 wait job 自身仍 running
                # 被 _orch_jobs_running 挡住而搁浅）；睡满时 age 必达标，直接续 tick
                app.state.jobs.submit(
                    "orchestrator-auto-wait",
                    _auto_wait_runner(delay),
                    meta={"project_id": pid, "delay": round(delay, 2)},
                    on_done=lambda _j, _r=reason:
                        _maybe_auto_tick(pid, reason=f"{_r}:throttled"))
                return
            app.state.jobs.submit(
                "orchestrator-auto-tick", _auto_tick_runner(pid, reason),
                meta={"project_id": pid, "reason": reason},
                on_done=lambda _j: _maybe_auto_tick(pid, reason="auto-tick-done"))
        except Exception:  # noqa: BLE001
            log.exception("_maybe_auto_tick 判定异常 pid=%s reason=%s", pid, reason)

    # 调度决策互斥：HTTP/on_done/sweep 多线程触发时串行化。RLock 而非 Lock——
    # 锁内有合法递归链：_schedule → _bind_task_window → 绑定竞态回收孤儿窗
    # _do_close_session → _schedule("session-closed")，同线程重入须放行。
    _schedule_lock = threading.RLock()

    def _schedule(pid: str, reason: str) -> None:
        """会话窗调度器（会话中心化，docs/plans/session-centric-orchestration.md
        §4；2026-09-25）——机械规则、不经编排 LLM，单段：

        窗内队列读时派生。对每个「有 open 委托的目标窗」：closed/已删 → 委托
        退回未指派（unassign；**不自动开窗重绑**，交编排器重新委派）；paused/
        worker 在跑 → 跳过；未武装：L2 武装+起跑、L1 等审批；已武装 idle →
        补起 worker。并发口径=活跃窗去重数 < max_concurrent_tasks（一窗串行
        一件，窗内其余排队）。L0/暂停整段跳过（人工「跑队列」=手动 override
        不经此函数）。

        **无 target_session 的 open 行不处理**（L0 提案残留/关窗退回——不自动
        起跑，交编排器重新委派）。触发点=publish/委派端点、worker-idle、
        worker on_done、post-tick + SCHEDULE_POLL_INTERVAL sweep 兜底。任何
        异常吞掉记 log。"""
        try:
            proj = _project(pid)
        except HTTPException:
            return
        _schedule_lock.acquire()
        try:
            bb = proj.bb
            cfg = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
            if cfg["level"] not in {"L1", "L2"} or cfg["paused"]:
                return
            tq = TaskQueue(bb)
            tasks = tq.list_tasks(pid)
            limit = int(cfg["max_concurrent_tasks"])
            # 在跑 worker 窗快照（认领间隙/收尾期都算占槽；worker-idle 自触时
            # 自己=这形态，不计它会超卖）。
            running_sids = {j["meta"].get("session_id")
                            for j in app.state.jobs.all_jobs()
                            if j["kind"] == "agent-work" and j["status"] == "running"
                            and j["meta"].get("project_id") == pid}
            # 活跃窗=有 claimed 委托的窗 + 有在跑 worker 的窗（去重计数）
            claimed_sids = {t.get("target_session") for t in tasks
                            if t["status"] == "claimed" and t.get("target_session")}
            active_n = len(claimed_sids | running_sids)
            # 候选窗：open 委托的 target_session 去重，按每窗队首
            # （priority, created_at）排序——窗内队列顺序即 session_queue 口径。
            head_by_sid: dict[str, tuple] = {}
            for t in tasks:
                if t["status"] != "open" or not t.get("target_session"):
                    continue
                key = (t["priority"], t["created_at"], t["id"])
                if t["target_session"] not in head_by_sid \
                        or key < head_by_sid[t["target_session"]]:
                    head_by_sid[t["target_session"]] = key
            for sid in sorted(head_by_sid, key=lambda s: head_by_sid[s]):
                if active_n >= limit:
                    break
                row = bb.get_session(sid)
                if row is None or row.get("status") == "closed":
                    # 目标窗已关：委托退回未指派，交编排器重新委派（不自动开窗）
                    try:
                        tq.unassign_session(sid)
                    except Exception:  # noqa: BLE001
                        log.exception("关窗退回失败 sid=%s", sid)
                    continue
                if row.get("status") == "paused" or sid in running_sids:
                    continue
                meta = row.get("meta")
                meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
                if not meta.get("worker_armed"):
                    if cfg["level"] != "L2":
                        continue  # L1 待命窗等审批，不自动武装
                    try:
                        agent = _ensure_agent(pid, sid)
                    except HTTPException:
                        continue
                    if agent.paused:
                        continue
                    bb.set_session_meta(sid, {"worker_armed": True, "close_pending": None})
                    _submit_worker(pid, agent, auto=True, origin="schedule")
                else:
                    # 已武装待命窗（崩溃回收/worker 早退）：补起 worker
                    try:
                        agent = _ensure_agent(pid, sid)
                    except HTTPException:
                        continue
                    if agent.paused:
                        continue
                    _submit_worker(pid, agent, auto=True, origin="schedule")
                active_n += 1
        except Exception:  # noqa: BLE001
            log.exception("_schedule 调度异常 pid=%s reason=%s", pid, reason)
        finally:
            _schedule_lock.release()

    def _schedule_request_approval(bb, pid: str, task: dict, reason: str) -> None:
        """L1 档执行审批单（v0.72 语义：窗已由绑定段建好，批准=启动该窗执行
        任务）；同一任务已有 pending 审批不重复提交（按 action 含 task_id 匹配）。"""
        pending = bb.conn.execute(
            "SELECT COUNT(*) AS n FROM approvals "
            "WHERE project_id=? AND status='pending' AND action LIKE ?",
            (pid, f"%{task['id']}%")).fetchone()["n"]
        if pending:
            return
        role = (task.get("role") or "").strip() or "_generalist"
        bb.request_approval(
            pid,
            {"op": "spawn_session", "role": role, "task_id": task["id"],
             "reason": f"启动任务执行窗：{task['objective'][:40]}（触发：{reason}）"},
            risk="low", requested_by="auto-spawn")

    # v0.71 任务窗调度 sweep（绑定段/启动段的兜底轮询）：绑定失败（cap 满/
    # 预算硬闸/LLM 未就绪）的任务靠它重试；app.state.schedule_sweep 留测试直调口
    app.state.schedule_sweep = _schedule  # 测试直调口（_schedule(pid, reason)）
    def _schedule_sweep() -> None:
        for meta in store.list_projects():
            pid = meta["id"]
            try:
                if pid in app.state.projects_closing:
                    continue
                _schedule(pid, "poll-sweep")
            except Exception:  # noqa: BLE001 —— 单项目失败不拖垮 sweep
                log.exception("任务窗调度 sweep 失败 pid=%s", pid)
    app.state.schedule_sweep_all = _schedule_sweep
    def _sched_poll_loop() -> None:
        while True:
            time.sleep(SCHEDULE_POLL_INTERVAL)
            _schedule_sweep()
    threading.Thread(target=_sched_poll_loop, name="task-window-sweep",
                     daemon=True).start()

    def _auto_wait_runner(delay: float) -> Callable[[], dict]:
        def run() -> dict:
            time.sleep(max(0.0, delay))
            return {"waited": round(delay, 2)}
        return run

    def _build_orchestrator(pid: str, body: TickIn, owner: str,
                            analyze_only: bool = False) -> Orchestrator:
        """手动 tick 与自动 tick 共用同一套 Orchestrator 装配（鸭子回调注入）。"""
        proj = _project(pid)
        exec_llm, plan_llm = _llms()
        factory = _registered_session_factory(pid, exec_llm, plan_llm)
        # 批 6：L0=提案模式（publish/spawn 校验照跑但不写实体，只发 orch.proposed）
        lvl = autonomy.autonomy_of(proj.bb.get_project(pid)["config"],
                                   track=proj.track)["level"]
        orch = Orchestrator(project_id=pid, bb=proj.bb, llm=plan_llm,
                            session_factory=factory,
                            config=OrchestratorConfig(
                                max_steps=body.max_steps,
                                allowed_roles=body.allowed_roles,
                                max_sessions=body.max_sessions,
                                digest_every=body.digest_every,
                                propose_only=(lvl == "L0"),
                                analyze_only=analyze_only),
                            packs_root=app.state.packs_root, track=proj.track,
                            campaign=app.state.campaign)
        # 复用进程内已开窗口（跨 tick 保活）
        orch.live_sessions = {sid: a for sid, a in app.state.agents.items()
                              if a.project_id == pid}

        def _gate(action: str) -> str | None:
            return autonomy.hard_block_reason(_project(pid).bb, pid, action)

        def _on_published(task_id: str | None = None) -> None:
            _project(pid).bb.usage_inc_tasks(pid)
            # 会话中心化：委托在 _tool_delegate 内已带 target_session 写入（选窗
            # /开窗先行）。此处只负责起跑——idle 武装窗提 worker；未武装窗
            # （L2 自动开的窗由工厂武装；L1 开窗走审批不到这）_schedule 按
            # 挡位武装+起跑；窗忙=窗内排队，worker 会自动接。失败不回滚委托，
            # worker-done/sweep 兜底。
            if not task_id:
                return
            try:
                bb = _project(pid).bb
                t = TaskQueue(bb).get_task(task_id)
                sid = (t or {}).get("target_session") if t else ""
                if not t or not sid:
                    return
                srow = bb.get_session(sid)
                if srow is None or srow.get("status") in {"closed", "paused"} \
                        or _session_job_running(sid):
                    return
                meta_raw = srow.get("meta")
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                if meta.get("worker_armed"):
                    _submit_worker(pid, _ensure_agent(pid, sid),
                                   auto=True, origin="orch-delegate")
                else:
                    _schedule(pid, reason="orch-delegate")
            except Exception:  # noqa: BLE001
                log.exception("编排委派起跑失败 task=%s", task_id)

        orch.gate = _gate
        orch.on_task_published = _on_published
        orch.autonomy_provider = lambda: autonomy.autonomy_of(
            _project(pid).bb.get_project(pid)["config"], track=_project(pid).track)
        orch.state_loader = lambda: orch_state.load_or_create(proj.bb, pid)
        orch.state_saver = lambda **fields: orch_state.save_fields(proj.bb, pid, **fields)
        orch.heartbeat = lambda: orch_state.renew_tick_lease(proj.bb, pid, owner)
        # 对话化编排器（M1/M2）：goal_section/persona 段实时读 project.json meta
        orch.meta_loader = lambda: _project(pid).meta or {}
        return orch

    # ---------- 分阶段工作流：过门分流（M2，§6.8 自主档） ----------

    def _phase_gate_check(pid: str) -> None:
        """阶段门已达 → 过门动作按自主档分流（每次 tick 出口调用）。
        L0=事件提示（人工点「进入渗透测试」）；L1=审批单（批准即流转）；
        L2=自动流转。同目标已分流过（gate_open_notified）不重复。
        B3 单一事实源：无论过门与否都把评估结论写 project meta
        phase_gate_state——注入侧 _phase_section 只读该状态不重算，消除
        「注入用上轮 idle、判定在轮末」的矛盾窗口；动作侧 gate_block_reason
        保持 publish 时现算确定性。内容不变跳过写盘（save_gate_state 内判）。"""
        proj = _project(pid)
        book = _phase_book_of(proj)
        if not book:
            return
        cur = phases_mod.current_spec(book, proj.meta or {})
        if cur is None:
            return
        cur_id, spec = cur
        gate = spec.get("gate") or {}
        if not gate:
            phases_mod.save_gate_state(
                proj, {"phase": cur_id, "gate": False, "passed": False, "unmet": []})
            return
        idle = int(orch_state.load_or_create(proj.bb, pid)["derive_idle_rounds"])
        metrics = phases_mod.gate_metrics(proj.bb, pid, idle_rounds=idle)
        met, unmet = phases_mod.evaluate_gate(gate, metrics)
        phases_mod.save_gate_state(
            proj, {"phase": cur_id, "gate": True, "passed": met, "unmet": unmet})
        fwd = phases_mod.forward_targets(book, spec)
        if not fwd:
            return
        target = fwd[0]
        if phases_mod.read_state(proj.meta or {})["notified"] == target:
            return  # 已分流过（等人工/等审批），抵达目标阶段时重置
        if not met:
            return
        cfg = _auto_cfg(pid)
        summary = (f"阶段流转：{spec['name']} → {book[target]['name']}"
                   f"（资产 {metrics['assets']}、高价值 {metrics['high_value']}、"
                   f"verified {metrics['verified']}、空闲 {metrics['idle_rounds']} 轮）")
        if cfg["level"] == "L2":
            phases_mod.enter_phase(proj, target, by="orchestrator", auto=True,
                                   packs_root=app.state.packs_root,
                                   reason="门指标达成自动流转", idle_rounds=idle)
            _schedule(pid, reason="phase-entered")
            return
        if cfg["level"] == "L1":
            pending = proj.bb.conn.execute(
                "SELECT action FROM approvals WHERE project_id=? AND status='pending'",
                (pid,)).fetchall()
            for row in pending:
                try:
                    a = json.loads(row["action"])
                except ValueError:
                    continue
                if isinstance(a, dict) and a.get("op") == "phase_transition" \
                        and a.get("to") == target:
                    return  # 同目标审批单在途，不重复提
            proj.bb.request_approval(
                pid, {"op": "phase_transition", "from": cur_id, "to": target,
                      "summary": summary, "metrics": metrics},
                risk="low", requested_by="orchestrator")
        else:
            proj.bb.append_event(pid, "phase.gate_open",
                                 {"from": cur_id, "to": target, "summary": summary,
                                  "metrics": metrics}, author="orchestrator")
        phases_mod.set_gate_notified(proj, target)

    def _phase_post_tick(pid: str, result: dict) -> None:
        """tick 出口的阶段维护：空闲轮计数（零发布 +1 / 有发布复位）+ 过门分流。
        先于挡位闸——L0 项目也要事件提示。"""
        proj = _project(pid)
        published = bool(result.get("published"))
        idle = int(orch_state.load_or_create(proj.bb, pid)["derive_idle_rounds"])
        orch_state.save_fields(
            proj.bb, pid, derive_idle_rounds=0 if published else idle + 1)
        _phase_gate_check(pid)

    app.state.phase_gate_check = _phase_gate_check  # 测试直调口
    app.state.phase_post_tick = _phase_post_tick    # 测试直调口

    # ---------- 异常订阅唤醒（对话化编排器 M4，§4.6） ----------

    app.state.orch_wake_pending: set[str] = set()  # 已提交唤醒待完成的项目（防重复）

    def _maybe_orch_wake(pid: str) -> str:
        """白名单事件扫描 → 提交自起对话轮（Job orchestrator-wake）。
        返回 submitted/duplicate/empty；租约占用/构造失败在 runner 内静默放弃
        （宁少勿扰），唤醒轮完成才摘 pending（期间重复触发不再提交）。"""
        proj = _project(pid)
        triggers = Orchestrator.collect_wake_triggers(proj.bb, pid)
        if not triggers:
            return "empty"
        if pid in app.state.orch_wake_pending:
            return "duplicate"
        app.state.orch_wake_pending.add(pid)

        def run() -> dict:
            owner = f"wake-{uuid.uuid4().hex}"
            try:
                bb = _project(pid).bb
            except Exception:  # noqa: BLE001 —— 项目已删：直接放弃
                app.state.orch_wake_pending.discard(pid)
                return {"skipped": "gone"}
            try:
                orch_state.acquire_tick_lease(bb, pid, owner)
            except orch_state.TickLeaseError:
                return {"skipped": "lease"}  # tick/对话在跑：静默跳过
            try:
                try:
                    orch = _build_orchestrator(pid, TickIn(), owner)
                except Exception:  # noqa: BLE001 —— LLM 缺席等：放弃不重试
                    log.exception("唤醒轮构造编排器失败 pid=%s", pid)
                    return {"skipped": "build"}
                try:
                    return orch.chat_turn(
                        Orchestrator.wake_brief_text(triggers), wake=triggers)
                except Exception as exc:  # noqa: BLE001
                    _emit_llm_error(pid, "orchestrator", exc)
                    return {"error": str(exc)[:200]}
            finally:
                try:
                    orch_state.release_tick_lease(bb, pid, owner)
                except Exception:  # noqa: BLE001
                    log.exception("唤醒轮释放租约失败 pid=%s", pid)
                app.state.orch_wake_pending.discard(pid)

        app.state.jobs.submit("orchestrator-wake", run, meta={"project_id": pid})
        return "submitted"

    app.state.orch_wake_check = _maybe_orch_wake  # 测试直调口

    def _post_tick(pid: str, result: dict, *, manual: bool) -> None:
        """触发点 B（手动/自动 tick 共用后处理）：自动 kick + L2 链状态机。

        零产出：自动 tick=收敛停链；手动 tick 仅在链已活跃时收敛停链。
        有产出：确保链启动并 kick；全项目零非 closed 会话 → no_sessions 停链。"""
        # 分阶段工作流（M2）：空闲轮计数 + 过门分流（先于挡位闸，异常不炸链）
        try:
            _phase_post_tick(pid, result)
        except HTTPException:
            return  # 项目删除中/不存在
        except Exception:  # noqa: BLE001 —— 阶段护栏故障不阻断链后处理
            log.exception("阶段过门分流失败 pid=%s", pid)
        cfg = _auto_cfg(pid)
        # 异常订阅唤醒（M4）：paused 不打扰，L0/L1/L2 全唤醒（挡位闸之前）
        if not cfg["paused"]:
            try:
                _maybe_orch_wake(pid)
            except Exception:  # noqa: BLE001 —— 唤醒失败不阻断链后处理
                log.exception("异常唤醒提交失败 pid=%s", pid)
        if cfg["level"] not in {"L1", "L2"} or cfg["paused"]:
            return
        _kick_workers(pid)
        _schedule(pid, reason="post-tick")  # v0.71 调度器：tick 刚发布的任务建窗起跑
        if cfg["level"] != "L2":
            return
        produced = bool(result.get("published") or result.get("spawned"))
        if not produced:
            if not manual or orch_state.load_or_create(_project(pid).bb, pid)["chain_active"]:
                _stop_chain(pid, "converged")
            return
        _start_chain(pid)
        if autonomy.count_active_sessions(_project(pid).bb, pid) == 0:
            _stop_chain(pid, "no_sessions")

    def _auto_tick_runner(pid: str, reason: str) -> Callable[[], dict]:
        def run() -> dict:
            proj = _project(pid)
            bb = proj.bb
            owner = f"auto-{uuid.uuid4().hex}"
            try:
                orch_state.acquire_tick_lease(bb, pid, owner)
            except orch_state.TickLeaseError:
                return {"skipped": "lease"}  # 闸⑤硬去重：不报错不计数
            try:
                cfg = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
                if cfg["level"] != "L2":
                    _stop_chain(pid, "level_changed")
                    return {"skipped": "level_changed"}
                if cfg["paused"]:
                    _stop_chain(pid, "paused")
                    return {"skipped": "paused"}
                st = orch_state.load_or_create(bb, pid)
                if not st["chain_active"] or pid not in app.state.active_chains:
                    if st["chain_active"]:
                        _stop_chain(pid, "restart")
                    return {"skipped": "chain_inactive"}
                # 拿租约成功才计数（抢租约的 loser 不污染 chain_ticks）
                orch_state.increment_counters(bb, pid, chain_ticks=1, auto_ticks_total=1)
                orch_state.save_fields(
                    bb, pid,
                    last_auto_tick_at=datetime.now(timezone.utc).isoformat())
                orch = _build_orchestrator(pid, TickIn(), owner)
                try:
                    result = orch.tick()
                except Exception as e:  # noqa: BLE001 —— 异常停链但不炸线程
                    err = f"{type(e).__name__}: {e}"
                    log.exception("自动 tick 执行失败 pid=%s", pid)
                    _stop_chain(pid, "error", error=err)
                    return {"error": err}
                _post_tick(pid, result, manual=False)
                return result
            finally:
                orch_state.release_tick_lease(bb, pid, owner)
        return run

    # ---------- A5：L2 自动去抖优先级重排（手动端点共用 runner） ----------

    def _replan_age(st: dict) -> float | None:
        """距上次重排（手动/自动统一计时）的秒数；从未重排返回 None（不节流）。"""
        raw = st.get("last_replan_at") or ""
        if not raw:
            return None
        try:
            return (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds()
        except ValueError:
            return None

    def _maybe_replan(pid: str, reason: str) -> str:
        """自动重排唯一入口（触发点 A worker 空退 / D 人发任务）：L2 & 未暂停 &
        无编排 job 在跑 & 距上次 >=30s；踩窗口起 wait job 睡满重入（触发不丢）。
        只判闸门 + submit job；任何异常吞掉记 log（后台线程不能炸）。
        返回状态：submitted/waiting/busy/skipped/error（调用方据此决定是否补触发）。"""
        try:
            proj = _project(pid)
        except HTTPException:
            return "skipped"
        try:
            bb = proj.bb
            cfg = autonomy.autonomy_of(bb.get_project(pid)["config"], track=proj.track)
            # 自动重排仅 L2（手动按钮不走这里，不受档位限制）
            if cfg["level"] != "L2" or cfg["paused"]:
                return "skipped"
            if _orch_jobs_running(pid):  # tick/重排/wait 统一去重（硬去重=租约）
                return "busy"
            age = _replan_age(orch_state.load_or_create(bb, pid))
            if age is not None and age < REPLAN_MIN_INTERVAL:
                delay = REPLAN_MIN_INTERVAL - age
                # 同 auto-tick：重入必须挂 on_done（runner 内同步重入会被自身 wait job 挡住）
                app.state.jobs.submit(
                    "orchestrator-replan-wait", _auto_wait_runner(delay),
                    meta={"project_id": pid, "delay": round(delay, 2)},
                    on_done=lambda _j, _r=reason:
                        _maybe_replan(pid, reason=f"{_r}:throttled"))
                return "waiting"
            app.state.jobs.submit(
                "orchestrator-replan", _replan_runner(pid, reason, manual=False),
                meta={"project_id": pid, "reason": reason, "auto": True},
                # 重排 job 曾占住编排去重闸，收尾后补评估一次续 tick，防链搁浅
                on_done=lambda _j: _maybe_auto_tick(pid, reason="replan-done"))
            return "submitted"
        except Exception:  # noqa: BLE001
            log.exception("_maybe_replan 判定异常 pid=%s reason=%s", pid, reason)
            return "error"

    def _replan_runner(pid: str, reason: str, *, manual: bool) -> Callable[[], dict]:
        def run() -> dict:
            proj = _project(pid)
            bb = proj.bb
            owner = f"replan-{uuid.uuid4().hex}"
            try:
                orch_state.acquire_tick_lease(bb, pid, owner)
            except orch_state.TickLeaseError:
                return {"skipped": "lease"}  # 硬去重：不报错
            try:
                if not manual:
                    cfg = autonomy.autonomy_of(bb.get_project(pid)["config"],
                                               track=proj.track)
                    if cfg["level"] != "L2" or cfg["paused"]:
                        return {"skipped": "level_or_paused"}
                    # 节流复核：wait 睡满后理论必过；多 wait 竞态/时钟异常兜底
                    age = _replan_age(orch_state.load_or_create(bb, pid))
                    if age is not None and age < REPLAN_MIN_INTERVAL:
                        return {"skipped": "throttled"}
                orch = _build_orchestrator(pid, TickIn(), owner)
                try:
                    result = orch.replan_priorities()
                except Exception as e:  # noqa: BLE001 —— LLM/传输失败不炸后台线程
                    err = f"{type(e).__name__}: {e}"
                    log.exception("自动重排执行失败 pid=%s", pid)
                    return {"error": err, "reason": reason}
                # 只有真的发生了 LLM 轮才刷新去抖计时；无 open 空转（note）零成本，
                # 若也计时会让紧随其后的正常触发凭空排长命 wait job 占住编排槽。
                if "note" not in result:
                    orch_state.save_fields(
                        bb, pid,
                        last_replan_at=datetime.now(timezone.utc).isoformat())
                result["reason"] = reason
                return result
            finally:
                orch_state.release_tick_lease(bb, pid, owner)
        return run

    # ---------- Orchestrator ----------

    @app.post("/api/projects/{pid}/orchestrator/chat")
    def orchestrator_chat(pid: str, body: OrchChatIn):
        """对话化编排器（M1，§6.4）：与编排器对话——插队轮。租约同步 acquire
        （busy → 409 不排队），人类消息落 orch.chat {role:"human"}，回复由
        chat_turn 落 {role:"orch", text(截2000), tool_trace}；工具面与 tick 全
        闸门同源（dedup/gate/L0 提案/L1 审批），对话轮只读态势不推进游标。"""
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "消息不能为空")
        proj = _project(pid)
        owner = f"chat-{uuid.uuid4().hex}"
        try:
            orch_state.acquire_tick_lease(proj.bb, pid, owner)
        except orch_state.TickLeaseError as e:
            raise HTTPException(409, "编排器正在思考（巡检/对话/重排任一在跑），稍后再发") from e
        try:
            orch = _build_orchestrator(pid, TickIn(), owner)
        except HTTPException:
            orch_state.release_tick_lease(proj.bb, pid, owner)
            raise
        proj.bb.append_event(
            pid, "orch.chat", {"role": "human", "text": text[:2000]}, author="human")

        def _run_chat() -> dict:
            try:
                return orch.chat_turn(text)
            except Exception as exc:
                _emit_llm_error(pid, "orchestrator", exc)
                raise
            finally:
                orch_state.release_tick_lease(proj.bb, pid, owner)

        job_id = app.state.jobs.submit(
            "orchestrator-chat", _run_chat, meta={"project_id": pid})
        return {"job_id": job_id}

    @app.get("/api/projects/{pid}/goal")
    def get_project_goal(pid: str):
        """阶段目标 + 编排器拟人身份（M2/M3 前端数据源；读 project.json meta）。"""
        proj = _project(pid)
        meta = proj.meta or {}
        return {"phase_goal": meta.get("phase_goal"),
                "persona": meta.get("orchestrator_persona")}

    @app.put("/api/projects/{pid}/goal")
    def set_project_goal(pid: str, body: PhaseGoalIn):
        """阶段目标确认（M2，§4.3）：写 meta.phase_goal + goal.confirm 事件
        （payload 带全文快照，变更历史=事件流可回放）；text 为空 = 清空重议
        （goal.clear）。criteria 是人话验收口径（LLM 评估用），不做机读联动。"""
        text = body.text.strip()
        proj = _project(pid)
        if not text:
            store.update_phase_goal(pid, None, project=proj)
            proj.bb.append_event(pid, "goal.clear", {}, author="human")
            return {"status": "cleared"}
        goal = {
            "text": text[:2000],
            "criteria": [str(c).strip()[:300]
                         for c in (body.criteria or []) if str(c).strip()][:20],
            "phase": (body.phase or "").strip()[:40] or None,
            "source": "chat",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_by": "human",
        }
        store.update_phase_goal(pid, goal, project=proj)
        proj.bb.append_event(pid, "goal.confirm", {"goal": goal}, author="human")
        return {"status": "confirmed", "goal": goal}

    @app.put("/api/projects/{pid}/orchestrator/persona")
    def set_orchestrator_persona(pid: str, body: OrchPersonaIn):
        """编排器拟人身份（M3，§4.4）：meta.orchestrator_persona={display_name,
        persona}；两字段全空 = 恢复缺省（剥键）。persona 只注入对话轮系统提示。"""
        proj = _project(pid)
        display = body.display_name.strip()
        persona = body.persona.strip()
        if not display and not persona:
            store.update_orchestrator_persona(pid, None, project=proj)
            return {"status": "cleared"}
        persona_val = {"display_name": display[:40] or "编排器",
                       "persona": persona[:2000]}
        store.update_orchestrator_persona(pid, persona_val, project=proj)
        return {"status": "ok", "persona": persona_val}

    # ---------- 分阶段工作流（pentest-phased-workflow M1/M2） ----------

    def _phase_book_of(proj: Project) -> dict[str, dict]:
        """项目轨阶段剧本（轨级默认 + 项目 config.phases 覆写；空=未启用）。"""
        return phases_mod.load_track_phases(
            app.state.packs_root, proj.track, (proj.meta or {}).get("config"))

    @app.get("/api/projects/{pid}/phase")
    def get_project_phase(pid: str):
        """阶段工作流状态（M4 前端阶段条数据源）：当前阶段/门进度/历史/分流标记。"""
        proj = _project(pid)
        book = _phase_book_of(proj)
        if not book:
            return {"enabled": False}
        cur_id, spec = phases_mod.current_spec(book, proj.meta or {})
        st = phases_mod.read_state(proj.meta or {})
        idle = int(orch_state.load_or_create(proj.bb, pid)["derive_idle_rounds"])
        metrics = phases_mod.gate_metrics(proj.bb, pid, idle_rounds=idle)
        met, unmet = phases_mod.evaluate_gate(spec.get("gate") or {}, metrics)
        return {"enabled": True, "current": cur_id, "spec": spec,
                # 全阶段序（M4 阶段条渲染三阶段步骤条用，按 order 排序）
                "phases": [{"id": _id, "name": book[_id].get("name") or _id,
                            "order": int(book[_id].get("order") or 0)}
                           for _id in sorted(book, key=lambda k: int(book[k].get("order") or 0))],
                "gate": {"metrics": metrics, "met": met, "unmet": unmet,
                         "forward": phases_mod.forward_targets(book, spec)},
                "history": st["history"], "notified": st["notified"]}

    @app.post("/api/projects/{pid}/phase")
    def transition_project_phase(pid: str, body: PhaseTransitionIn):
        """人工流转阶段（人工最终——不走门；目标限当前阶段剧本 next 清单内）。
        剧本首发任务发布后 _schedule 建专属窗（起跑与否按自主档）。"""
        proj = _project(pid)
        book = _phase_book_of(proj)
        if not book:
            raise HTTPException(
                422, f"项目轨 {proj.track} 无阶段剧本，阶段工作流未启用")
        cur_id, spec = phases_mod.current_spec(book, proj.meta or {})
        to = body.to.strip()
        if to not in book:
            raise HTTPException(422, f"目标阶段不存在: {to}（本轨阶段: {sorted(book)}）")
        if to not in (spec.get("next") or []):
            raise HTTPException(
                422, f"不允许的流转: {cur_id} → {to}（允许: {spec.get('next') or []}）")
        idle = int(orch_state.load_or_create(proj.bb, pid)["derive_idle_rounds"])
        r = phases_mod.enter_phase(proj, to, by="human",
                                   packs_root=app.state.packs_root,
                                   reason=body.reason.strip()[:200], idle_rounds=idle)
        _schedule(pid, reason="phase-entered")  # 首发任务建窗（起跑按挡位/人工）
        return {"from": r["from"], "to": to, "published": r["published"],
                "spec": r["spec"]}

    @app.post("/api/projects/{pid}/orchestrator/directive", deprecated=True)
    def orchestrator_directive(pid: str, body: DirectiveIn):
        """C2 指挥编排器（§6.4）：人类一次性目标指令——落 orch.directive 事件
        （最高优先注入下一轮 tick）+ 自动触发一轮编排。持久方向走 mission。
        **deprecated（对话化编排器 M1，2026-09-21）**：对话窗全替代——UI 入口已删，
        本端点仅保留兼容 CLI/脚本；存量未消费指令仍由 tick 消费。"""
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "指令不能为空")
        proj = _project(pid)
        event_id = proj.bb.append_event(
            pid, "orch.directive",
            {"text": text[:2000], "status": "pending"}, author="human")
        # 自动触发一轮编排（复用手动 tick 同款租约/job 路径）
        owner = f"directive-{uuid.uuid4().hex}"
        try:
            orch_state.acquire_tick_lease(proj.bb, pid, owner)
        except orch_state.TickLeaseError as e:
            raise HTTPException(409, str(e)) from e
        try:
            orch = _build_orchestrator(pid, TickIn(), owner)
        except HTTPException:
            orch_state.release_tick_lease(proj.bb, pid, owner)
            raise

        def _run_directive_tick() -> dict:
            try:
                result = orch.tick()
                _post_tick(pid, result, manual=False)
                return result
            finally:
                orch_state.release_tick_lease(proj.bb, pid, owner)

        job_id = app.state.jobs.submit(
            "orchestrator-tick", _run_directive_tick, meta={"project_id": pid},
            on_done=lambda _j: _maybe_auto_tick(pid, reason="directive-done"))
        return {"event_id": event_id, "job_id": job_id, "status": "dispatched"}

    @app.get("/api/judgment-templates")
    def list_judgment_templates():
        """C2 判据模板：内置（渗透/红队默认）+ 用户自定义（config/judgment_templates.json）。"""
        return {"builtin": judgments.BUILTIN_TEMPLATES,
                "user": judgments.load_user_templates(app.state.judgments_dir)}

    @app.put("/api/judgment-templates")
    def save_judgment_templates(body: dict):
        """整表保存用户模板（{name: criteria}）；空名/空判据条目剔除。"""
        judgments.save_user_templates(app.state.judgments_dir, body or {})
        return {"saved": len(judgments.load_user_templates(app.state.judgments_dir))}

    @app.delete("/api/judgment-templates/{name}")
    def delete_judgment_template(name: str):
        templates = judgments.load_user_templates(app.state.judgments_dir)
        if name not in templates:
            raise HTTPException(404, f"模板不存在: {name}")
        del templates[name]
        judgments.save_user_templates(app.state.judgments_dir, templates)
        return {"deleted": name}

    @app.post("/api/projects/{pid}/orchestrator/tick")
    def orchestrator_tick(pid: str, body: TickIn):
        proj = _project(pid)
        # tick 租约（机制 1.9）：同步先拿，他人租约未过期直接 409，不进 job/不构造 LLM；
        # 崩溃不释放也会在 900s TTL 后自然到期。owner 仅释放自己的租约。
        owner = f"tick-{uuid.uuid4().hex}"
        try:
            orch_state.acquire_tick_lease(proj.bb, pid, owner)
        except orch_state.TickLeaseError as e:
            # 409 带运行时长（2026-09-28，从最近 tick.started 事件算）：让「慢」
            # 和「死」可分辨——正常研判/链轮几分钟内完，超 15 分钟基本可判卡死。
            # 整段异常护栏：观测增强绝不能反过来把 409 炸成 500（教训：list_events
            # 方法名不存在 → AttributeError → 500，即「链自旋收敛：500」横幅的来源）
            since = ""
            try:
                tip = proj.bb.latest_event_id(pid)
                for ev in reversed(proj.bb.recent_events(
                        pid, since_id=max(0, tip - 200), limit=200)):
                    if ev["kind"] == "orch.tick.started":
                        try:
                            mins = int((datetime.now(timezone.utc)
                                        - datetime.fromisoformat(ev["created_at"]))
                                       .total_seconds() // 60)
                            since = f"已运行 {mins} 分钟（启动于 {ev['created_at'][11:16]} UTC）"
                        except ValueError:
                            pass
                        break
            except Exception:  # noqa: BLE001 —— 推导失败只降级为无时长文案
                log.exception("409 时长推导失败（不影响 409 本身）")
            raise HTTPException(
                409, "已有编排 tick 在执行"
                + (f"（{since}）" if since else "（租约 900s TTL；进程崩溃会自然到期）")
                + "。编排页签可看进度；确认卡死可在编排页签「⚡ 强制接管」后重启") from e
        try:
            # 503（无 LLM key）等同步失败先释放租约
            orch = _build_orchestrator(pid, body, owner, analyze_only=body.analyze_only)
        except HTTPException:
            orch_state.release_tick_lease(proj.bb, pid, owner)
            raise
        # 编排开始有痕（2026-09-18）：事件流实时可见，配合作战计划面板的运行时长提示
        proj.bb.append_event(
            pid, "orch.tick.started",
            {"reason": "analyze" if body.analyze_only else "manual"},
            author="orchestrator")

        def _run_tick() -> dict:
            try:
                result = orch.tick()
                if body.analyze_only:
                    # auto-attack 研判轮（2026-09-28）：计划全文落 analyzed 事件
                    # （流内呈现「分析报告 + 开跑按钮」的载体）。不调 _post_tick——
                    # 分析不产出任务，不能误启自动链；确认开跑由前端二次触发普通 tick。
                    proj.bb.append_event(pid, "orch.auto_attack.analyzed", {
                        "budget_ticks": body.budget_ticks,
                        "summary": str(result.get("analysis")
                                       or result.get("summary") or ""),
                    }, author="orchestrator")
                else:
                    _post_tick(pid, result, manual=True)  # 触发点 B：kick + L2 链状态机
                return result
            except Exception as exc:
                _emit_llm_error(pid, "orchestrator", exc)
                raise
            finally:
                orch_state.release_tick_lease(proj.bb, pid, owner)

        # tick 状态翻 done 后再评估一次续链：worker 可能在 tick 收尾期间已空退
        # （其 A 触发会因 tick job 仍 running 而跳过），由这里兜底，防链搁浅。
        job_id = app.state.jobs.submit(
            "orchestrator-tick", _run_tick, meta={"project_id": pid},
            on_done=lambda _j: _maybe_auto_tick(pid, reason="tick-done"))
        return {"job_id": job_id}

    @app.post("/api/projects/{pid}/orchestrator/tick/force-acquire")
    def orchestrator_tick_force_acquire(pid: str):
        """强制接管（2026-09-28 人工救济）：无条件清 tick 租约，卡死轮的补救
        出口——409 长时间不解除（心跳停/时长异常）时，清租约即可重新点火。
        旧轮若仍存活，其产出照常落事件（任务发布由编排器判重），短暂双跑窗口
        为已知代价；orch.tick.forced 落事件留痕。"""
        proj = _project(pid)
        if not orch_state.force_release_tick_lease(proj.bb, pid):
            raise HTTPException(409, "当前无 tick 租约可接管")
        proj.bb.append_event(pid, "orch.tick.forced",
                             {"by": "human"}, author="orchestrator")
        return {"released": True}

    @app.post("/api/projects/{pid}/orchestrator/auto-attack/stop")
    def orchestrator_auto_attack_stop(pid: str):
        """自动渗透人工停止（auto-attack 2026-09-28）：停 L2 链不降档——与
        档位变更/暂停同走 _stop_chain（幂等），orch.chain_stopped{reason:human}
        落事件；在跑 worker 任务不受影响（链只管编排轮的自动续转）。"""
        _project(pid)
        if not _stop_chain(pid, "human"):
            raise HTTPException(409, "自动链当前未在运行")
        return {"stopped": True}

    @app.post("/api/projects/{pid}/tasks/{tid}/report")
    def task_report_generate(pid: str, tid: str):
        """任务报告（trae 视图 2026-09-28）：done 任务 → plan_llm 生成 md 任务状况
        报告，落 task.report 事件持久化（trae 条目内随时查看；事件幂等查重）。
        前端在 task.done 后触发；生成走后台 job，失败可重试（job error 可见）。"""
        proj = _project(pid)
        t = TaskQueue(proj.bb).get_task(tid)
        if not t or t.get("project_id") != pid:
            raise HTTPException(404, f"任务不存在: {tid}")
        if t["status"] != "done":
            raise HTTPException(409, f"仅 done 任务生成报告（当前 {t['status']}）")
        for r in proj.bb.conn.execute(
                "SELECT id, payload FROM events WHERE project_id=? AND kind='task.report'",
                (pid,)).fetchall():
            try:
                if (json.loads(r["payload"]) or {}).get("task_id") == tid:
                    return {"existing": True, "event_id": r["id"]}
            except ValueError:
                continue
        sid = str(t.get("target_session") or "")
        rows = proj.bb.conn.execute(
            "SELECT kind, payload FROM events WHERE project_id=? AND session_id=?"
            " AND kind IN ('tool.call','command','finding.new')"
            " ORDER BY id DESC LIMIT 80", (pid, sid)).fetchall()
        trace: list[dict] = []
        for r in reversed(rows):
            try:
                trace.append({"kind": r["kind"], **(json.loads(r["payload"]) or {})})
            except ValueError:
                continue
        try:
            attempts = (json.loads(t.get("context") or "{}") or {}).get("attempts") or []
        except ValueError:
            attempts = []
        _exec_llm, plan_llm = _llms()

        def _gen() -> dict:
            # job 内复查：并发触发时后到者直接复用先到者的事件（幂等兜底）
            for r in proj.bb.conn.execute(
                    "SELECT id, payload FROM events WHERE project_id=? AND kind='task.report'",
                    (pid,)).fetchall():
                try:
                    if (json.loads(r["payload"]) or {}).get("task_id") == tid:
                        return {"task_id": tid, "deduped": True}
                except ValueError:
                    continue
            material = {
                "task": {k: t.get(k) for k in ("id", "objective", "scope", "task_type",
                                               "status", "result_note", "created_at",
                                               "updated_at")},
                "attempts": attempts,
                "recent_actions": trace,
            }
            prompt = (
                "你是渗透测试团队的任务报告员。请根据以下 JSON 任务执行记录，写一份中文"
                " Markdown 任务状况报告，固定四节：\n"
                "## 任务概览（目标/范围/类型）\n"
                "## 执行过程（时间线叙述，引用关键命令/工具及其结果）\n"
                "## 结果与产出（结论、登记的发现、产物）\n"
                "## 遗留与建议（未竟事项、下一步建议）\n"
                "只输出 Markdown 正文，不寒暄、不编造记录里没有的事实。\n"
                "```json\n" + json.dumps(material, ensure_ascii=False)[:24000] + "\n```")
            resp = plan_llm.chat(
                [{"role": "user", "content": prompt}],
                system="你是渗透测试团队的报告员：输出严谨、克制、只基于给定记录。")
            record_llm_usage(proj.bb, pid, resp.usage, source="task-report",
                             session_id=sid or None, model=getattr(plan_llm, "model", ""))
            blocks = resp.raw.get("content", [])
            if isinstance(blocks, list):
                report = "\n".join(
                    b.get("text", "") for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text").strip()
            else:
                report = str(blocks).strip()
            if not report:
                raise RuntimeError("报告生成为空（LLM 无文本输出）")
            proj.bb.append_event(
                pid, "task.report",
                {"task_id": tid, "session_id": sid, "report": report[:16000]},
                session_id=sid, author="orchestrator")
            return {"task_id": tid, "chars": len(report)}

        job_id = app.state.jobs.submit("task-report", _gen, meta={"project_id": pid})
        return {"job_id": job_id, "status": "generating"}

    @app.post("/api/projects/{pid}/orchestrator/replan-priorities")
    def orchestrator_replan_priorities(pid: str):
        """A5 手动重排优先级：人类动作不受档位/30s 去抖/预算闸限制（DESIGN §6.4）。
        tick 租约占用 → 409；后台 Job 执行，job.result={updated,skipped}。"""
        proj = _project(pid)
        owner = f"replan-{uuid.uuid4().hex}"
        try:
            orch_state.acquire_tick_lease(proj.bb, pid, owner)
        except orch_state.TickLeaseError as e:
            raise HTTPException(
                409, "已有编排动作在执行（tick/重排租约 900s TTL；进程崩溃会自然到期）") from e
        try:
            # 503（无 planner key）等同步失败先释放租约
            orch = _build_orchestrator(pid, TickIn(), owner)
        except HTTPException:
            orch_state.release_tick_lease(proj.bb, pid, owner)
            raise

        def _run_replan() -> dict:
            try:
                result = orch.replan_priorities()
                # 空转（无 open，零 LLM）不刷去抖计时，与自动 runner 口径一致
                if "note" not in result:
                    orch_state.save_fields(
                        proj.bb, pid,
                        last_replan_at=datetime.now(timezone.utc).isoformat())
                if result.get("error"):
                    _emit_llm_error(pid, "replan", result["error"])
                result["reason"] = "manual"
                return result
            finally:
                orch_state.release_tick_lease(proj.bb, pid, owner)

        job_id = app.state.jobs.submit(
            "orchestrator-replan", _run_replan,
            meta={"project_id": pid, "reason": "manual", "auto": False},
            on_done=lambda _j: _maybe_auto_tick(pid, reason="replan-done"))
        return {"job_id": job_id}

    # ---------- 分类学与包管理（设置页：能力包 / 场景轨 / 角色 / Skill / 红线） ----------
    # 修改即时生效于「下次开窗」（角色/技能在会话构造时加载），在跑的会话不受影响。

    @app.get("/api/taxonomy")
    def get_taxonomy():
        """能力包 × 场景轨目录 + 各轨任务类型注册表（新建项目向导的数据源，§4.5）。"""
        return {
            "capabilities": list_packs(app.state.packs_root, "capability"),
            "tracks": list_packs(app.state.packs_root, "track"),
            "task_types": {
                t["name"]: load_task_types(app.state.packs_root, t["name"])
                for t in list_packs(app.state.packs_root, "track")
            },
        }

    # ---- Skill 通用序列化 ----

    def _skill_json(sk) -> dict:
        return {
            "name": sk.name, "kind": sk.kind, "pack": sk.pack,
            "description": sk.description, "keywords": sk.keywords,
            "features": sk.features, "file_features": sk.file_features,
            "platforms": sk.platforms, "formats": sk.formats,
            "vuln_classes": sk.vuln_classes, "task_types": sk.task_types,
            "required_tools": sk.required_tools, "enabled": sk.enabled,
        }

    def _loaded_registry() -> SkillRegistry:
        reg = SkillRegistry(app.state.packs_root)
        reg.load()
        return reg

    def _get_skill(kind: str, owner: str, skill_name: str):
        path = (_cap_path(app, owner, "skills", skill_name, "SKILL.md")
                if kind == "capability"
                else _track_path(app, owner, "skills", skill_name, "SKILL.md"))
        if not path.is_file():
            label = "能力包" if kind == "capability" else "场景轨"
            raise HTTPException(404, f"技能不存在: {label} {owner}/{skill_name}")
        reg = _loaded_registry()
        sk = next((s for s in reg.all()
                   if s.kind == kind and s.pack == owner and s.name == skill_name), None)
        return path, sk

    # ---- 能力包：Skill 列表/读写 + 红线 ----

    @app.get("/api/capabilities/{cap}/skills")
    def list_cap_skills(cap: str):
        _check_name(cap, "能力包")
        reg = _loaded_registry()
        return [_skill_json(sk) for sk in reg.all()
                if sk.kind == "capability" and sk.pack == cap]

    @app.get("/api/capabilities/{cap}/skills/{skill_name}")
    def get_cap_skill(cap: str, skill_name: str):
        path, sk = _get_skill("capability", cap, skill_name)
        return {"name": skill_name, "meta": parse_frontmatter(path.read_text(encoding="utf-8")),
                "skill": _skill_json(sk) if sk else None,
                "raw": path.read_text(encoding="utf-8")}

    @app.put("/api/capabilities/{cap}/skills/{skill_name}")
    def update_cap_skill(cap: str, skill_name: str, body: SkillUpdateIn):
        path, _ = _get_skill("capability", cap, skill_name)
        meta = parse_frontmatter(body.content)
        if meta.get("name") != skill_name:
            raise HTTPException(422, f"frontmatter name（{meta.get('name')!r}）必须与技能名（{skill_name}）一致")
        with pack_write_lock():
            _pack_history_backup(path)
            path.write_text(body.content, encoding="utf-8")
        return {"status": "ok"}

    @app.post("/api/capabilities/{cap}/skills", status_code=201)
    def create_cap_skill(cap: str, body: SkillCreateIn):
        """向导新建能力包技能（薄路由模板）。重名 409、非法名 422、能力包不存在 404。"""
        _check_name(cap, "能力包")
        _check_name(body.name, "技能名")
        cdir = capability_dir(app.state.packs_root, cap)
        if not cdir.is_dir():
            raise HTTPException(404, f"能力包不存在: {cap}")
        dest = cdir / "skills" / body.name
        with pack_write_lock():  # 存在性检查+建目录+写文件同一临界区（防并发重名）
            if dest.exists():
                raise HTTPException(409, f"技能已存在: {cap}/{body.name}")
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text(_dump_new_skill(body.name, body), encoding="utf-8")
        return {"status": "ok", "name": body.name, "path": f"capabilities/{cap}/skills/{body.name}"}

    @app.delete("/api/capabilities/{cap}/skills/{skill_name}")
    def delete_cap_skill(cap: str, skill_name: str):
        """删除技能：整目录移入 skills/.history/trash/ 可恢复。"""
        path = _cap_path(app, cap, "skills", skill_name)
        with pack_write_lock():
            if not path.is_dir():
                raise HTTPException(404, f"技能不存在: capabilities/{cap}/skills/{skill_name}")
            dest = _trash_move(path)
        return {"status": "ok", "name": skill_name, "trash": dest.name}

    @app.patch("/api/capabilities/{cap}/skills/{skill_name}/enabled")
    def toggle_cap_skill(cap: str, skill_name: str, body: SkillEnabledIn):
        """enabled 快速开关：只改 frontmatter，正文不动，自动留 .history 备份。"""
        path, _ = _get_skill("capability", cap, skill_name)
        backup = _set_skill_enabled(path, body.enabled)
        return {"status": "ok", "name": skill_name, "enabled": body.enabled,
                "backup": backup.name if backup else None}

    @app.get("/api/capabilities/{cap}/rules")
    def get_cap_rules(cap: str):
        path = _cap_path(app, cap, "rules", "redlines.md")
        # 缺失是常态（doctor 的 missing-redlines 预警），返回 200+exists:false，不走 404 噪声
        if not path.is_file():
            return {"content": "", "exists": False}
        return {"content": path.read_text(encoding="utf-8"), "exists": True}

    @app.put("/api/capabilities/{cap}/rules")
    def update_cap_rules(cap: str, body: FileContentIn):
        path = _cap_path(app, cap, "rules", "redlines.md")
        with pack_write_lock():
            _pack_history_backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.content, encoding="utf-8")
        return {"status": "ok"}

    # ---- 场景轨：Skill 列表/读写 ----

    @app.get("/api/tracks/{track}/skills")
    def list_track_skills(track: str):
        _check_name(track, "场景轨")
        reg = _loaded_registry()
        return [_skill_json(sk) for sk in reg.all()
                if sk.kind == "track" and sk.pack == track]

    @app.get("/api/tracks/{track}/skills/{skill_name}")
    def get_track_skill(track: str, skill_name: str):
        path, sk = _get_skill("track", track, skill_name)
        return {"name": skill_name, "meta": parse_frontmatter(path.read_text(encoding="utf-8")),
                "skill": _skill_json(sk) if sk else None,
                "raw": path.read_text(encoding="utf-8")}

    @app.put("/api/tracks/{track}/skills/{skill_name}")
    def update_track_skill(track: str, skill_name: str, body: SkillUpdateIn):
        path, _ = _get_skill("track", track, skill_name)
        meta = parse_frontmatter(body.content)
        if meta.get("name") != skill_name:
            raise HTTPException(422, f"frontmatter name（{meta.get('name')!r}）必须与技能名（{skill_name}）一致")
        with pack_write_lock():
            _pack_history_backup(path)
            path.write_text(body.content, encoding="utf-8")
        return {"status": "ok"}

    @app.post("/api/tracks/{track}/skills", status_code=201)
    def create_track_skill(track: str, body: SkillCreateIn):
        """向导新建轨级技能（薄路由模板）。重名 409、非法名 422、场景轨不存在 404。"""
        _check_name(track, "场景轨")
        _check_name(body.name, "技能名")
        tdir = track_dir(app.state.packs_root, track)
        if not tdir.is_dir():
            raise HTTPException(404, f"场景轨不存在: {track}")
        dest = tdir / "skills" / body.name
        with pack_write_lock():  # 存在性检查+建目录+写文件同一临界区（防并发重名）
            if dest.exists():
                raise HTTPException(409, f"技能已存在: {track}/{body.name}")
            dest.mkdir(parents=True)
            (dest / "SKILL.md").write_text(_dump_new_skill(body.name, body), encoding="utf-8")
        return {"status": "ok", "name": body.name, "path": f"tracks/{track}/skills/{body.name}"}

    @app.delete("/api/tracks/{track}/skills/{skill_name}")
    def delete_track_skill(track: str, skill_name: str):
        """删除轨级技能：整目录移入 skills/.history/trash/ 可恢复。"""
        path = _track_path(app, track, "skills", skill_name)
        with pack_write_lock():
            if not path.is_dir():
                raise HTTPException(404, f"技能不存在: tracks/{track}/skills/{skill_name}")
            dest = _trash_move(path)
        return {"status": "ok", "name": skill_name, "trash": dest.name}

    @app.patch("/api/tracks/{track}/skills/{skill_name}/enabled")
    def toggle_track_skill(track: str, skill_name: str, body: SkillEnabledIn):
        """enabled 快速开关：只改 frontmatter，正文不动，自动留 .history 备份。"""
        path, _ = _get_skill("track", track, skill_name)
        backup = _set_skill_enabled(path, body.enabled)
        return {"status": "ok", "name": skill_name, "enabled": body.enabled,
                "backup": backup.name if backup else None}

    # ---- 场景轨：红线 / 角色 / owner 规则 / 任务类型注册表 ----

    @app.get("/api/tracks/{track}/rules")
    def get_track_rules(track: str):
        path = _track_path(app, track, "rules", "redlines.md")
        # 缺失是常态（doctor 的 missing-redlines 预警），返回 200+exists:false，不走 404 噪声
        if not path.is_file():
            return {"content": "", "exists": False}
        return {"content": path.read_text(encoding="utf-8"), "exists": True}

    @app.put("/api/tracks/{track}/rules")
    def update_track_rules(track: str, body: FileContentIn):
        path = _track_path(app, track, "rules", "redlines.md")
        with pack_write_lock():
            _pack_history_backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.content, encoding="utf-8")
        return {"status": "ok"}

    @app.get("/api/tracks/{track}/roles")
    def list_track_roles(track: str):
        """专家池清单（expert-pool M2：roles/ 退役，数据源切 experts/ 按轨过滤，
        响应形状与旧角色视图兼容——前端只读消费零改动；写端点退役返 410，
        专家管理 UI 随 M3 落地）。"""
        _check_name(track, "场景轨")
        out = []
        for name in list_experts(app.state.packs_root, track):
            e = load_expert(app.state.packs_root, name, track)
            out.append({"name": e.get("name") or name,
                        "description": e.get("description"),
                        "persona": e.get("persona"),
                        "skills": e.get("skills"),
                        "task_types": e.get("task_types"),
                        "default_noise": e.get("default_noise"),
                        "tools": e.get("tools"),
                        "max_runtime": e.get("max_runtime"),
                        "max_steps": e.get("max_steps"),
                        "file": name})
        return out

    @app.put("/api/tracks/{track}/roles/{role_name}")
    def update_track_role(track: str, role_name: str, body: dict):
        raise HTTPException(
            410, "角色已退役（expert-pool M2）：专家由 packs/experts/ 单文件池管理，"
                 "专家管理前端随 M3 落地")

    @app.post("/api/tracks/{track}/roles")
    def create_track_role(track: str, body: dict):
        raise HTTPException(
            410, "角色已退役（expert-pool M2）：专家由 packs/experts/ 单文件池管理，"
                 "专家管理前端随 M3 落地")

    @app.delete("/api/tracks/{track}/roles/{role_name}")
    def delete_track_role(track: str, role_name: str):
        raise HTTPException(
            410, "角色已退役（expert-pool M2）：专家由 packs/experts/ 单文件池管理，"
                 "管理端点见 /api/experts（M3）")

    # ---- 专家池管理（expert-pool M3）：packs/experts/ 单文件池 CRUD ----

    def _experts_base() -> Path:
        return Path(app.state.packs_root) / "experts"

    def _expert_view(name: str) -> dict:
        """专家全字段读视图（管理 UI 编辑回填；raw 平铺解析，不应用轨变体）。
        variant_<track>_<field> 重组回 {track: {field: value}}。"""
        path = _experts_base() / f"{name}.yaml"
        raw = _parse_expert_yaml(path)
        variants: dict[str, dict] = {}
        for k, v in raw.items():
            if k.startswith("variant_"):
                tr, _, field = k[len("variant_"):].partition("_")
                variants.setdefault(tr, {})[field] = v
        return {"id": name,
                "name": raw.get("name") or name,
                "description": raw.get("description"),
                "persona": raw.get("persona"),
                "tracks": raw.get("tracks"),
                "skills": raw.get("skills"),
                "task_types": raw.get("task_types"),
                "default_noise": raw.get("default_noise"),
                "tools": raw.get("tools"),
                "max_runtime": raw.get("max_runtime"),
                "max_steps": raw.get("max_steps"),
                "protected": bool(raw.get("protected")),
                "variants": variants,
                "file": f"experts/{name}.yaml"}

    def _validate_expert_body(body: ExpertSaveIn) -> None:
        """写端点前置校验（宁严勿松，doctor 同口径前移）：tracks 合法轨、
        variants 轨名/字段名、max_steps 正整数、skills 注册表存在
        （doctor expert-skill-missing）、task_types 越各轨注册表并集
        （doctor expert-tasktype-unregistered）。"""
        valid_tracks = {t["name"] for t in list_packs(app.state.packs_root, "track")}
        bad = sorted(set(body.tracks or []) - valid_tracks)
        if bad:
            raise HTTPException(422, f"非法场景轨: {bad}（合法: {sorted(valid_tracks)}）")
        for vt, fields in (body.variants or {}).items():
            if vt not in valid_tracks:
                raise HTTPException(422, f"轨变体非法场景轨: {vt}（合法: {sorted(valid_tracks)}）")
            if not isinstance(fields, dict) or not fields:
                raise HTTPException(422, f"轨变体 {vt} 必须是非空字段对象")
            if any(not re.fullmatch(r"[a-z_]+", str(k)) for k in fields):
                raise HTTPException(422, f"轨变体 {vt} 字段名非法（仅小写字母/下划线）")
        if body.max_steps is not None and body.max_steps <= 0:
            raise HTTPException(422, "max_steps 必须为正整数")
        if body.skills:
            reg = _loaded_registry()
            unknown = sorted({s for s in body.skills if reg.get(s.strip()) is None})
            if unknown:
                raise HTTPException(422, f"未知技能: {unknown}（先建技能或修正拼写）")
        if body.task_types:
            known: set[str] = set()
            for t in valid_tracks:
                known |= set(load_task_types(app.state.packs_root, t))
            unknown = sorted(set(body.task_types) - known)
            if unknown:
                raise HTTPException(422, f"未注册任务类型: {unknown}（各轨注册表并集: {sorted(known)}）")

    def _dump_expert_yaml(body: ExpertSaveIn) -> str:
        """专家 yaml 序列化（平铺约定：key: value、内联列表 [a, b]、空值不落键
        = null 语义）。值含 ": " 或行内注释风险时双引号包裹（解析端剥外层引号）。"""
        def _emit(key: str, val: Any, lines: list[str]) -> None:
            if val is None or val == "" or val == []:
                return
            if isinstance(val, list):
                lines.append(f"{key}: [{', '.join(str(v).strip() for v in val)}]")
                return
            s = str(val).strip()
            if ": " in s or " #" in s or s.startswith(("'", '"', "[", "{", "-", "?", "!", "&", "*")):
                s = f'"{s}"'
            lines.append(f"{key}: {s}")
        lines: list[str] = []
        for key in ("name", "description", "tracks", "persona", "skills",
                    "task_types", "default_noise", "tools", "max_runtime", "max_steps"):
            _emit(key, getattr(body, key), lines)
        for vt in sorted(body.variants or {}):
            for field in sorted(body.variants[vt] or {}):
                _emit(f"variant_{vt}_{field}", body.variants[vt][field], lines)
        return "\n".join(lines) + "\n"

    @app.get("/api/experts")
    def list_all_experts(pid: str | None = None):
        """全池清单（管理 UI；不按轨过滤，tracks 字段由前端分组/过滤）。
        对话化编排器 M3（§4.4）：恒追加虚拟单例 {id:"orchestrator", kind:"virtual"}
        ——不入 experts/*.yaml 文件池、不认领任务不执行命令、删不掉（运行态注册，
        名字/persona 存项目 meta）；绑定页/组队选择器/管理面板按 kind 过滤。
        带 pid 时 name 取该项目 meta.orchestrator_persona.display_name。"""
        base = _experts_base()
        rows = [] if not base.is_dir() else [
            _expert_view(p.stem) for p in sorted(base.glob("*.yaml"))]
        display = "编排器"
        if pid:
            try:
                persona = (_project(pid).meta or {}).get("orchestrator_persona") or {}
                display = str(persona.get("display_name") or "编排器")
            except HTTPException:
                pass  # 未知 pid 宽容：虚拟条目仍以缺省名返回
        rows.append({"id": "orchestrator", "name": display, "kind": "virtual",
                     "description": "编排器虚拟专家（不认领任务、不执行命令）",
                     "tracks": None, "protected": True})
        return rows

    @app.get("/api/experts/{expert_id}")
    def get_expert(expert_id: str):
        path = _experts_base() / f"{expert_id}.yaml"
        if not path.is_file():
            raise HTTPException(404, f"专家不存在: {expert_id}")
        return _expert_view(expert_id)

    @app.post("/api/experts", status_code=201)
    def create_expert(body: ExpertCreateIn):
        """新建专家 yaml。重名 409；id 强制 ASCII slug（文件名/命令引用面）。"""
        eid = body.id.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", eid):
            raise HTTPException(422, f"非法专家 id: {eid}（小写字母/数字/连字符，≤48 字符）")
        _validate_expert_body(body)
        base = _experts_base()
        with pack_write_lock():  # 存在性检查+写文件同一临界区（防并发重名）
            dest = base / f"{eid}.yaml"
            if dest.exists():
                raise HTTPException(409, f"专家已存在: {eid}")
            payload = body.model_copy(update={"name": body.name or eid})
            base.mkdir(parents=True, exist_ok=True)
            dest.write_text(_dump_expert_yaml(payload), encoding="utf-8")
        return {"status": "ok", "id": eid, "file": f"experts/{eid}.yaml"}

    @app.put("/api/experts/{expert_id}")
    def update_expert(expert_id: str, body: ExpertSaveIn):
        """全字段覆写（表单即最终态）。_generalist 可编辑不可删。"""
        path = _experts_base() / f"{expert_id}.yaml"
        _validate_expert_body(body)
        with pack_write_lock():
            if not path.is_file():
                raise HTTPException(404, f"专家不存在: {expert_id}")
            _pack_history_backup(path)
            path.write_text(_dump_expert_yaml(body), encoding="utf-8")
        return {"status": "ok", "id": expert_id, "file": f"experts/{expert_id}.yaml"}

    @app.delete("/api/experts/{expert_id}")
    def delete_expert(expert_id: str):
        """删除专家：移入 experts/.history/trash/ 可恢复；protected 拒删。"""
        path = _experts_base() / f"{expert_id}.yaml"
        with pack_write_lock():
            if not path.is_file():
                raise HTTPException(404, f"专家不存在: {expert_id}")
            if _parse_expert_yaml(path).get("protected"):
                raise HTTPException(409, f"受保护专家不可删除: {expert_id}")
            dest = _trash_move(path)
        return {"status": "ok", "id": expert_id, "trash": dest.name}

    # ---- 场景档（expert-pool M4a）：tracks/<track>/profiles/ 只读清单 ----

    @app.get("/api/tracks/{track}/profiles")
    def list_track_profiles_ep(track: str):
        """轨内置场景档清单（创建页选档数据源；档案=文件，写走 packs 直改）。"""
        _check_name(track, "场景轨")
        return load_track_profiles(app.state.packs_root, track)

    @app.get("/api/tracks/{track}/task-types")
    def get_track_task_types(track: str):
        """轨任务类型注册表（task_types.yaml + 内置 generic）。"""
        _check_name(track, "场景轨")
        return load_task_types(app.state.packs_root, track)

    # ---- 平台规则 owners（文件即规则：删文件=停用、新建=扩展；meta.owner 命中才注入） ----

    @app.get("/api/tracks/{track}/owners")
    def list_track_owners(track: str):
        base = _track_path(app, track, "rules", "owners")
        if not base.is_dir():
            return []
        return [{"tag": f.stem, "content": f.read_text(encoding="utf-8")}
                for f in sorted(base.glob("*.md"))]

    @app.put("/api/tracks/{track}/owners/{tag}")
    def update_track_owner(track: str, tag: str, body: FileContentIn):
        _check_name(tag, "owner 标签")
        path = _track_path(app, track, "rules", "owners", f"{tag}.md")
        with pack_write_lock():
            _pack_history_backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.content, encoding="utf-8")
        return {"status": "ok", "tag": tag}

    @app.delete("/api/tracks/{track}/owners/{tag}")
    def delete_track_owner(track: str, tag: str):
        path = _track_path(app, track, "rules", "owners", f"{tag}.md")
        with pack_write_lock():
            if not path.is_file():
                raise HTTPException(404, f"平台规则不存在: {tag}")
            _pack_history_backup(path)  # 删除也留回滚件
            path.unlink()
        return {"status": "ok", "tag": tag}

    # ---- 评级与价值口径 ratings（F11，同 owners「文件即规则」哲学；
    #      注入带判级硬指令，rating_basis 落 findings.rating_basis） ----

    @app.get("/api/tracks/{track}/ratings")
    def list_track_ratings(track: str):
        base = _track_path(app, track, "rules", "rating")
        if not base.is_dir():
            return []
        return [{"tag": f.stem, "content": f.read_text(encoding="utf-8")}
                for f in sorted(base.glob("*.md"))]

    @app.put("/api/tracks/{track}/ratings/{tag}")
    def update_track_rating(track: str, tag: str, body: FileContentIn):
        _check_name(tag, "评级标签")
        path = _track_path(app, track, "rules", "rating", f"{tag}.md")
        with pack_write_lock():
            _pack_history_backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.content, encoding="utf-8")
        return {"status": "ok", "tag": tag}

    @app.delete("/api/tracks/{track}/ratings/{tag}")
    def delete_track_rating(track: str, tag: str):
        path = _track_path(app, track, "rules", "rating", f"{tag}.md")
        with pack_write_lock():
            if not path.is_file():
                raise HTTPException(404, f"评级规则不存在: {tag}")
            _pack_history_backup(path)  # 删除也留回滚件
            path.unlink()
        return {"status": "ok", "tag": tag}

    # ---- pack doctor（角色/技能/知识源静态体检，三级 error/warning/info） ----

    def _route_zero_hit_issues() -> list[dict]:
        """K7 路由效果追踪（2026-09-20；2026-09-24 修正为真使用口径）：扫
        workspaces 各项目 blackboard.db——skill.routed 的 route_points（真注入过，
        route_index>0）与 kb.open 的 module（手册真被打开）逐项目配对；point 被
        注入过且其 kb 在任一项目被打开过才算 used，否则 → info issue。

        旧实现 used 只由 route_points 构建，实际报的是「从未被注入」，注入后
        被忽略的条目永远报不出来。

        只读旁路（直接 sqlite mode=ro，不经 Blackboard），扫不动就静默跳过——
        doctor 的静态体检永远不能被动态统计拖垮。"""
        from core.skills.routeindex import parse_entries
        from core.skills.doctor import Issue, _LEVEL_ORDER
        root = Path(app.state.packs_root)
        index_file = root / "kb" / "route_index.yaml"
        if not index_file.is_file():
            return []
        try:
            point_kb = {e.point: e.kb for e in parse_entries(
                index_file.read_text(encoding="utf-8"))}
        except Exception:  # noqa: BLE001 —— 坏索引由静态体检报，本段跳过
            return []
        all_injected: set[str] = set()
        used: set[str] = set()
        ws = Path(workspace_root)
        for proj_file in ws.glob("*/project.json"):
            db = proj_file.parent / "blackboard.db"
            if not db.is_file():
                continue
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    routed = conn.execute(
                        "SELECT payload FROM events WHERE kind='skill.routed'"
                    ).fetchall()
                    opened_rows = conn.execute(
                        "SELECT payload FROM events WHERE kind='kb.open'"
                    ).fetchall()
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                continue

            def _payloads(rows):
                out = []
                for (raw,) in rows:
                    try:
                        p = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(p, dict):
                        out.append(p)
                return out

            injected = set()
            for p in _payloads(routed):
                # 只统计真的注入过路由索引条目的事件（route_index>0）
                if int(p.get("route_index") or 0) > 0:
                    pts = p.get("route_points")
                    if isinstance(pts, list):
                        injected.update(str(x) for x in pts if x)
            opened = {str(p.get("module") or "").strip().replace("\\", "/")
                      for p in _payloads(opened_rows)}
            all_injected.update(injected)
            for point in injected:
                kb = point_kb.get(point)
                if kb and kb in opened:
                    used.add(point)
        issues: list[Issue] = []
        for point in sorted(all_injected - used):
            dom = (point_kb.get(point) or "").split("/", 1)[0]
            issues.append(Issue(
                "info", "route-index-zero-hit",
                f"kb/route_index.yaml:{dom}",
                f"测试点「{point}」→ {point_kb.get(point)}：曾被注入路由索引"
                "但对应手册从未被 kb_open 打开（提示被忽略，考虑精简触发词"
                "或合并条目）"))
        return [i.__dict__ for i in sorted(
            issues, key=lambda x: (_LEVEL_ORDER[x.level], x.code, x.target))]

    def _kb_cleanup_issues() -> list[dict]:
        """经验沉淀 M3「kb 模块待清理建议」段：跨项目聚合 kb 模块三象限反馈
        （traces.kb_module_feedback 同源口径）——长期打开但从无 verified 贡献
        （正向零）提示人工复核；真失败任务窗内打开多次（甲负向反馈）标「优先」。
        阈值：opened≥3 或 negative≥2 才报（打开且失败≠手册误导，试错正常，
        宁少勿滥）；处置（降权/归档/删除）一律走提案制人工决策，只呈现不自动动。

        只读旁路照抄 _route_zero_hit_issues 先例（sqlite mode=ro，扫不动静默跳过）。"""
        from core.blackboard.traces import kb_module_feedback
        from core.skills.doctor import Issue, _LEVEL_ORDER
        agg: dict[str, dict] = {}
        ws = Path(workspace_root)
        for proj_file in ws.glob("*/project.json"):
            db = proj_file.parent / "blackboard.db"
            if not db.is_file():
                continue
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute("SELECT id FROM projects").fetchall()
                    for r in rows:
                        for m, d in kb_module_feedback(conn, r["id"]).items():
                            a = agg.setdefault(
                                m, {"opened": 0, "positive": 0, "negative": 0})
                            for k in a:
                                a[k] += d.get(k, 0)
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                continue
        issues: list[Issue] = []
        for m, d in sorted(agg.items()):
            if d["positive"] > 0:
                continue  # 有 verified 贡献，不在清理视野
            if d["opened"] < 3 and d["negative"] < 2:
                continue
            priority = d["negative"] >= 2
            issues.append(Issue(
                "info", "kb-cleanup-candidate", f"kb:{m}",
                f"kb 模块 {m}：{'正向零贡献且负向多次（优先复核）' if priority else '正向零贡献'}"
                f"——累计打开 {d['opened']} 次 / 真失败任务中打开 {d['negative']} 次 / "
                "verified 贡献 0。仅提示人工复核（打开且失败≠手册误导，试错正常）；"
                "降权/归档/删除走提案制人工决策"))
        return [i.__dict__ for i in sorted(
            issues, key=lambda x: (_LEVEL_ORDER[x.level], x.code, x.target))]

    @app.get("/api/packs/doctor")
    def packs_doctor():
        report = diagnose(app.state.packs_root,
                          tools_root=app.state.tools_root).to_dict()
        try:  # K7 零命中统计只增补 info，失败不拖垮静态体检
            existing = {x["target"] for x in report["issues"]}
            extra = [i for i in _route_zero_hit_issues() if i["target"] not in existing]
            report["issues"] += extra
            report["counts"]["info"] += len(extra)
            report["issues"].sort(key=lambda x: ({"error": 0, "warning": 1, "info": 2}[x["level"]],
                                                 x["code"], x["target"]))
        except Exception:  # noqa: BLE001
            pass
        try:  # 经验沉淀 M3：kb 模块待清理建议（info，失败不拖垮静态体检）
            extra = [i for i in _kb_cleanup_issues()
                     if i["target"] not in {x["target"] for x in report["issues"]}]
            report["issues"] += extra
            report["counts"]["info"] += len(extra)
            report["issues"].sort(key=lambda x: ({"error": 0, "warning": 1, "info": 2}[x["level"]],
                                                 x["code"], x["target"]))
        except Exception:  # noqa: BLE001
            pass
        return report

    # ---- .history 版本管理（列版本 / 看 diff / 一键回滚；兼容两种备份命名） ----

    @app.get("/api/packs/history")
    def packs_history_list(file: str = Query(..., description="packs 内相对路径")):
        path = _resolve_managed_file(app, file)
        versions = _history_versions(path)
        return {"file": file, "exists": path.is_file(), "versions": versions}

    @app.get("/api/packs/history/diff")
    def packs_history_diff(file: str = Query(...), version: str = Query(...)):
        path = _resolve_managed_file(app, file)
        _check_name(version, "版本")
        backup = path.parent / ".history" / version
        if not backup.is_file() or os.path.commonpath(
                [str((path.parent / ".history").resolve()),
                 str(backup.resolve())]) != str((path.parent / ".history").resolve()):
            raise HTTPException(404, f"历史版本不存在: {version}")
        old_lines = backup.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = (path.read_text(encoding="utf-8").splitlines(keepends=True)
                     if path.is_file() else [])
        diff = "".join(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"{version}（历史版）", tofile=f"{path.name}（当前）"))
        return {"file": file, "version": version, "diff": diff}

    @app.post("/api/packs/history/rollback")
    def packs_history_rollback(file: str = Query(...), version: str = Query(...)):
        """一键回滚：先把当前版再备份一次（保证可往返），再用历史版覆盖。"""
        path = _resolve_managed_file(app, file)
        _check_name(version, "版本")
        hist = path.parent / ".history"
        backup = hist / version
        if not backup.is_file() or os.path.commonpath(
                [str(hist.resolve()), str(backup.resolve())]) != str(hist.resolve()):
            raise HTTPException(404, f"历史版本不存在: {version}")
        with pack_write_lock():  # 回滚=备份当前+覆盖，须与其他写互斥防覆盖
            current_backup = _pack_history_backup(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, path)
        return {"status": "ok", "file": file, "rolled_back": version,
                "current_backed_up": current_backup.name if current_backup else None}

    # ---- 路由试算器（调试 skill 命中：调 SkillRouter 真评分） ----

    @app.post("/api/skills/route-preview")
    def route_preview(body: RoutePreviewIn):
        track = body.track
        if not track and body.domain:  # 旧客户端兼容
            track = LEGACY_DOMAIN_MAP.get(body.domain, (body.domain, []))[0]
        packs = (set(body.capabilities) | {track}) if track else None
        role_skills = None
        if body.role and track:
            try:
                role_skills = load_expert(
                    app.state.packs_root, body.role, track).get("skills")
            except FileNotFoundError:
                raise HTTPException(404, f"专家不存在: {track}/{body.role}")
        hits = SkillRouter(registry=_loaded_registry()).route(
            query=body.query, features=body.features,
            file_features=body.file_features, labels=body.labels,
            role_skills=role_skills, packs=packs, top_k=10,
            include_disabled=body.include_disabled)
        return [{"name": h.skill.name, "kind": h.skill.kind, "pack": h.skill.pack,
                 "description": h.skill.description, "enabled": h.skill.enabled,
                 "score": h.score, "matched": h.matched,
                 "breakdown": h.breakdown} for h in hits]

    # ---- 词汇表聚合（七类 frontmatter 字段 + 使用计数，前端 datalist 补全） ----

    @app.get("/api/skills/vocab")
    def skills_vocab():
        fields = ("keywords", "features", "file_features", "platforms",
                  "formats", "vuln_classes", "task_types")
        vocab: dict[str, dict[str, int]] = {f: {} for f in fields}
        for sk in _loaded_registry().all():
            for f in fields:
                for v in getattr(sk, f, None) or []:
                    vocab[f][v] = vocab[f].get(v, 0) + 1
        return {f: [{"value": k, "count": c} for k, c in sorted(
                    vocab[f].items(), key=lambda kv: (-kv[1], kv[0]))]
                for f in fields}

    # ---- kb 本地基线（C2：列举/读/新建/改写/删除/改名联动/版本/引用） ----
    # 纪律：中文多级目录允许，限 .md，1 MiB 上限；写经 writing.py 同一把 packs 锁。

    def _kb_error_map(fn, *args, status: int = 422, **kwargs):
        try:
            return fn(*args, **kwargs)
        except writing.KbError as e:
            raise HTTPException(status, str(e))

    @app.get("/api/capabilities/{cap}/kb")
    def kb_list(cap: str):
        return {"cap": cap, "sources": _kb_error_map(
            writing.list_kb, app.state.packs_root, cap)}

    @app.get("/api/capabilities/{cap}/kb/routes")
    def kb_routes(cap: str):
        """kb 任务导航路由表（kb/route.json 原样；前端 KbTree「按任务类型」分组）。"""
        from core.skills.kbindex import load_kb_routes
        return {"cap": cap, "routes": load_kb_routes(
            app.state.packs_root, [cap])}

    @app.get("/api/campaign-memory")
    def campaign_memory(limit: int = Query(50, ge=1, le=200)):
        """⑥ 战役记忆全局库浏览（按时间倒序；召回注入在编排 tick 态势里）。"""
        return {"items": app.state.campaign.list_recent(limit)}

    @app.get("/api/workspace-hygiene")
    def workspace_hygiene():
        """工作区卫生体检（workspace-hygiene D6，纯只读不处置）：①workspaces 根
        陌生条目（无 project.json 的目录/散文件，白名单 .trash/CLAUDE.md——
        campaign.db 迁移失败降级留老位置时从这里可见）；②各项目膨胀目录
        （.tmp/scratch/spill/browser-profile 超阈值报条目+大小）；③.trash 规模。
        处置入口指向已有能力（scratch/clear、回收站手动移回），端点零写副作用。"""
        def _du_mb(path: Path) -> float:
            total = 0
            for p in path.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    continue
            return total / (1024 * 1024)

        ws = Path(workspace_root)
        strays: list[dict] = []
        projects: list[dict] = []
        try:
            entries = sorted(ws.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for entry in entries:
            if entry.name == ".trash":
                continue
            if entry.is_dir():
                if (entry / "project.json").is_file():
                    bloat = []
                    for d in (".tmp", "scratch", "spill", "browser-profile"):
                        p = entry / d
                        if not p.is_dir():
                            continue
                        mb = _du_mb(p)
                        if mb >= HYGIENE_BLOAT_MB:
                            bloat.append({
                                "dir": d, "mb": round(mb, 1),
                                "level": "error" if mb >= HYGIENE_BLOAT_ERROR_MB
                                else "warning"})
                    projects.append({"slug": entry.name, "bloat": bloat})
                else:
                    strays.append({"name": entry.name, "kind": "dir", "size": 0})
            else:
                if entry.name == "CLAUDE.md":
                    continue  # 契约内合法文件
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                strays.append({"name": entry.name, "kind": "file", "size": size})
        trash_dir = ws / ".trash"
        trash_count, trash_mb = 0, 0.0
        if trash_dir.is_dir():
            try:
                trash_count = sum(1 for _ in trash_dir.iterdir())
            except OSError:
                pass
            trash_mb = round(_du_mb(trash_dir), 1)
        bloat_rows = [b for pr in projects for b in pr["bloat"]]
        return {
            "root": str(ws),
            "strays": strays,
            "projects": projects,
            "trash": {"count": trash_count, "mb": trash_mb},
            "summary": {
                "strays": len(strays),
                "bloats": len(bloat_rows),
                "errors": sum(1 for b in bloat_rows if b["level"] == "error"),
            },
        }

    @app.get("/api/capabilities/{cap}/kb/search")
    def kb_search(cap: str, q: str = Query(""), limit: int = Query(50, ge=1, le=200),
                  tag: str | None = Query(None, description="分面标签过滤"
                      "（K5 升级项 C，phase/vuln_class 值，如 sqli、jwt、recon）")):
        return {"cap": cap, "q": q, "tag": tag, "results": _kb_error_map(
            writing.search_kb, app.state.packs_root, cap, q, limit, tag=tag)}

    @app.get("/api/capabilities/{cap}/kb/file")
    def kb_read(cap: str, path: str = Query(...)):
        target = _kb_error_map(writing.resolve_kb, app.state.packs_root, cap, path)
        if not target.path.is_file():
            raise HTTPException(404, f"kb 文件不存在: {target.module}")
        hits = refs_mod.find_module_refs(app.state.packs_root, target.module)
        return {"cap": cap, "path": target.module, "source": target.source.id,
                "size": target.path.stat().st_size,
                "content": target.path.read_text(encoding="utf-8"),
                "refs": [h.to_dict() for h in hits]}

    @app.post("/api/capabilities/{cap}/kb/file", status_code=201)
    def kb_create(cap: str, body: KbWriteIn):
        target = _kb_error_map(writing.resolve_kb, app.state.packs_root, cap, body.path)
        if target.path.exists():
            raise HTTPException(409, f"文件已存在: {target.module}（改写请用 PUT）")
        out = _kb_error_map(writing.write_kb_file, app.state.packs_root, cap,
                            body.path, body.content, create=True)
        return {"status": "ok", **out}

    @app.put("/api/capabilities/{cap}/kb/file")
    def kb_update(cap: str, body: KbWriteIn):
        target = _kb_error_map(writing.resolve_kb, app.state.packs_root, cap, body.path)
        if not target.path.is_file():
            raise HTTPException(404, f"文件不存在: {target.module}（新建请用 POST）")
        out = _kb_error_map(writing.write_kb_file, app.state.packs_root, cap,
                            body.path, body.content, create=False)
        return {"status": "ok", **out}

    @app.delete("/api/capabilities/{cap}/kb/file")
    def kb_delete(cap: str, path: str = Query(...), force: bool = False):
        target = _kb_error_map(writing.resolve_kb, app.state.packs_root, cap, path)
        if not target.path.is_file():
            raise HTTPException(404, f"文件不存在: {target.module}")
        hits = refs_mod.find_module_refs(app.state.packs_root, target.module)
        if hits and not force:
            raise HTTPException(
                409, {"message": f"文件被 {len(hits)} 处引用，默认拒绝删除；"
                                 "确认后带 force=true 强删（引用会悬空，请先改名联动或修文档）",
                      "refs": [h.to_dict() for h in hits]})
        out = _kb_error_map(writing.delete_kb_file, app.state.packs_root, cap, path)
        return {"status": "ok", **out, "refs": [h.to_dict() for h in hits]}

    @app.post("/api/capabilities/{cap}/kb/rename")
    def kb_rename(cap: str, body: KbRenameIn):
        """同源改名 + 全仓引用完整路径替换（被改文件各自备份），同一临界区完成。"""
        try:
            with pack_write_lock():
                move = writing.rename_kb_move(
                    app.state.packs_root, cap, body.path, body.new_path)
                cascade = refs_mod.rewrite_module(
                    app.state.packs_root, cap, body.path, body.new_path)
        except writing.KbError as e:
            raise HTTPException(422, str(e))
        return {"status": "ok", **move, **cascade}

    @app.get("/api/capabilities/{cap}/kb/versions")
    def kb_versions(cap: str, path: str = Query(...)):
        return {"path": path,
                "versions": _kb_error_map(
                    writing.kb_versions, app.state.packs_root, cap, path)}

    @app.get("/api/capabilities/{cap}/kb/diff")
    def kb_diff(cap: str, path: str = Query(...), version: str = Query(...)):
        try:
            target = writing.resolve_kb(app.state.packs_root, cap, path)
            backup = writing.kb_version_path(app.state.packs_root, cap, path, version)
        except writing.KbError as e:
            raise HTTPException(422, str(e))
        if not backup.is_file():
            raise HTTPException(404, f"历史版本不存在: {version}")
        old_lines = backup.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = (target.path.read_text(encoding="utf-8").splitlines(keepends=True)
                     if target.path.is_file() else [])
        return {"path": path, "version": version,
                "diff": "".join(difflib.unified_diff(
                    old_lines, new_lines,
                    fromfile=f"{version}（历史版）", tofile=f"{path}（当前）"))}

    @app.post("/api/capabilities/{cap}/kb/rollback")
    def kb_rollback(cap: str, path: str = Query(...), version: str = Query(...)):
        try:
            out = writing.rollback_kb_file(
                app.state.packs_root, cap, path, version)
        except writing.KbError as e:
            # 版本不存在语义上是 404；路径/版本名非法才 422
            if "不存在" in str(e):
                raise HTTPException(404, str(e))
            raise HTTPException(422, str(e))
        return {"status": "ok", **out}

    @app.get("/api/capabilities/{cap}/kb/refs")
    def kb_refs(cap: str, path: str = Query(...)):
        target = _kb_error_map(writing.resolve_kb, app.state.packs_root, cap, path)
        hits = refs_mod.find_module_refs(app.state.packs_root, target.module)
        return {"path": target.module, "count": len(hits),
                "refs": [h.to_dict() for h in hits]}

    # ---- 统一变更提案（C4：skill edit / kb 四模式，只落 pending，人类审批） ----

    def _proposal_event_payload(p: dict, **extra) -> dict:
        return {"id": p["id"], "kind": p["target"].get("kind"),
                "mode": p["mode"], "origin": p.get("origin"),
                "target": p["target"], "summary": p.get("summary"), **extra}

    @app.post("/api/proposals", status_code=201)
    def proposal_create(body: ProposalIn):
        payload = body.model_dump(exclude={"origin"})
        try:
            p = proposals_mod.create_proposal(
                app.state.packs_root, payload, origin=body.origin)
        except (ProposalError, writing.KbError) as e:
            raise HTTPException(422, str(e))
        if body.project:
            try:
                _project(body.project).bb.append_event(
                    body.project, "proposal.created",
                    _proposal_event_payload(p), author=body.origin)
            except HTTPException:
                pass
        return p

    @app.get("/api/proposals")
    def proposal_list(status: str | None = Query(None)):
        if status is not None and status not in {"pending", "approved", "rejected"}:
            raise HTTPException(422, f"非法状态过滤: {status}")
        return proposals_mod.list_proposals(app.state.packs_root, status)

    @app.get("/api/proposals/{pid}")
    def proposal_detail(pid: str):
        try:
            return proposals_mod.proposal_detail(app.state.packs_root, pid)
        except ProposalStateError as e:
            raise HTTPException(404, str(e))

    def _get_proposal_or_404(pid: str) -> dict:
        try:
            return proposals_mod.get_proposal(app.state.packs_root, pid)
        except ProposalStateError as e:
            raise HTTPException(404 if "不存在" in str(e) else 409, str(e))

    @app.post("/api/proposals/{pid}/apply")
    def proposal_apply(pid: str, body: ProposalDecisionIn):
        p = _get_proposal_or_404(pid)
        try:
            out = proposals_mod.apply_proposal(
                app.state.packs_root, pid, body.decided_by)
        except ProposalStateError as e:
            raise HTTPException(409, str(e))
        except (ProposalError, writing.KbError) as e:
            raise HTTPException(422, f"应用前复核失败: {e}")
        if p.get("project"):
            try:
                _project(p["project"]).bb.append_event(
                    p["project"], "proposal.applied",
                    _proposal_event_payload(p, decided_by=body.decided_by),
                    author=body.decided_by)
            except HTTPException:
                pass
        return out

    @app.post("/api/proposals/{pid}/reject")
    def proposal_reject(pid: str, body: ProposalDecisionIn):
        p = _get_proposal_or_404(pid)
        try:
            out = proposals_mod.reject_proposal(
                app.state.packs_root, pid, body.decided_by, body.note)
        except ProposalStateError as e:
            raise HTTPException(409, str(e))
        if p.get("project"):
            try:
                _project(p["project"]).bb.append_event(
                    p["project"], "proposal.rejected",
                    _proposal_event_payload(p, decided_by=body.decided_by,
                                            note=body.note),
                    author=body.decided_by)
            except HTTPException:
                pass
        return out

    @app.post("/api/proposals/{pid}/revise")
    def proposal_revise(pid: str, body: ProposalReviseIn):
        _get_proposal_or_404(pid)
        try:
            return proposals_mod.revise_proposal(
                app.state.packs_root, pid, body.by, body.changes, body.note)
        except ProposalStateError as e:
            raise HTTPException(409, str(e))
        except (ProposalError, writing.KbError) as e:
            raise HTTPException(422, str(e))

    # ---- 复盘沉淀（C4）：planner_llm 后台复盘任务 → 逐条校验落 pending 提案 ----

    @app.post("/api/sessions/{sid}/review")
    def session_review_job(sid: str):
        # F8 会话级复盘：单个会话对它跑过的任务复盘（取代项目级 review-proposals）
        pid = _pid_of_session(sid)  # 不存在的会话 → 404
        proj = _project(pid)
        sess = proj.bb.get_session(sid)
        _exec, plan_llm = _llms()  # 无 key/无启用供应商 → 503
        if plan_llm is None:
            raise HTTPException(503, "planner LLM 未配置，复盘沉淀不可用")

        def _run() -> dict:
            bb, root = proj.bb, app.state.packs_root
            tq = _tq(pid)
            # 会话级事件流（kb.open/命令/路由/任务生命周期/发现），天然有界
            events = bb.recent_events(pid, session_id=sid, limit=300)
            kb_opens = [{"ts": e.get("created_at", ""),
                         "module": e.get("payload", {}).get("module"),
                         "source": e.get("payload", {}).get("source")}
                        for e in events if e.get("kind") == "kb.open"]
            cmd_log = [{"kind": e.get("kind"), "ts": e.get("created_at", ""),
                        "detail": json.dumps(e.get("payload", {}), ensure_ascii=False)[:200]}
                       for e in events
                       if e.get("kind") in ("command", "command.result", "skill.routed")]
            task_events = {e.get("payload", {}).get("task_id")
                           for e in events
                           if e.get("kind") == "task.claimed"} - {None}
            # 任务集 = claimed_by（done/failed 行保留最后认领者）∪ 事件回放 task.claimed
            tasks_view = [{"objective": t.get("objective", "")[:300],
                           "task_type": t.get("task_type"),
                           "status": t.get("status"),
                           "result_note": (t.get("result_note") or "")[:300]}
                          for t in tq.list_tasks(pid)
                          if t.get("claimed_by") == sid or t.get("id") in task_events]
            findings_view = [{"title": f.get("title", ""),
                              "vuln_class": f.get("vuln_class", ""),
                              "severity": f.get("severity"),
                              "status": f.get("status")}
                             for f in bb.list_findings(pid) if f.get("author") == sid][:50]
            # 显式文件清单：LLM 只能在清单内给路径，防猜名（越界提案校验时也会被拒）
            # expert-pool M2：能力面按 caps_effective 推导（专家绑定项目同口径）
            eff_caps = caps_effective(
                app.state.packs_root, proj.track, proj.experts,
                fallback=proj.capabilities)
            file_list: dict[str, list[str]] = {}
            for cap in eff_caps:
                try:
                    file_list[cap] = [f["path"]
                                      for src in writing.list_kb(root, cap)
                                      for f in src["files"]]
                except writing.KbError:
                    continue
            skills_list = [f"{s.kind}/{s.pack}/{s.name}"
                           for s in _loaded_registry().all()]
            system_msg = (
                "你是安全行动复盘编辑。基于一次会话执行的证据（该会话认领的任务、跑过的"
                "命令、引用过的文档、产出的发现），提出对知识库/技能文档的沉淀提案。"
                "严格规则：(1) 仅限三种情形——文档互相矛盾、文档缺失、手法已被本次任务"
                "验证有效；(2) 路径只能从给出的文件清单里选，禁止猜路径；"
                "新经验一律 create 新 .md 文件，禁止覆盖/翻译英文原文；技能只许 edit；"
                "(3) 每条必须在 reason 附任务证据；(4) 无值得沉淀的内容就返回空列表。"
                '只输出 JSON：{"proposals":[{"kind":"kb|skill","mode":"edit|create|rename|delete",'
                '"target":{...},"content":"...","summary":"≤300字","reason":"证据"}]}')
            user_msg = json.dumps(
                {"capabilities": eff_caps,
                 "session": {"id": sid, "role": sess.get("role")},
                 "kb_files": file_list, "skills": skills_list,
                 "kb_open_sequence": kb_opens[-50:],
                 "command_log": cmd_log[-100:],
                 "tasks": tasks_view, "findings": findings_view},
                ensure_ascii=False)
            resp = plan_llm.chat(
                [{"role": "user", "content": user_msg}], system=system_msg)
            text = (resp.text or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\n?", "", text).strip()
                text = re.sub(r"\n?```$", "", text).strip()
            m = re.search(r"\{.*\}", text, re.S)
            try:
                data = json.loads(m.group(0) if m else text)
            except (json.JSONDecodeError, ValueError):
                return {"landed": [], "rejected": [],
                        "error": "LLM 输出不是合法 JSON", "raw_head": text[:500]}
            landed, rejected = [], []
            for item in (data.get("proposals") or [])[:10]:
                target = item.get("target") if isinstance(item, dict) else None
                if not isinstance(target, dict):
                    rejected.append({"summary": str(item)[:120],
                                     "error": "target 不是对象"})
                    continue
                payload = {"target": {"kind": item.get("kind"), **target},
                           "mode": item.get("mode"),
                           "content": item.get("content"),
                           "summary": item.get("summary", ""),
                           "reason": item.get("reason", ""),
                           "project": pid, "task": item.get("task"),
                           "evidence": "复盘 Job：基于本会话命令/文档引用/任务成败/findings"}
                try:
                    p = proposals_mod.create_proposal(root, payload, origin="review")
                except (ProposalError, writing.KbError) as e:
                    rejected.append({"summary": str(item.get("summary"))[:120],
                                     "error": str(e)})
                    continue
                bb.append_event(pid, "proposal.created",
                                _proposal_event_payload(p), author="review")
                landed.append({"id": p["id"], "summary": p["summary"]})
            return {"landed": landed, "rejected": rejected}

        return {"job_id": app.state.jobs.submit(
            "session-review", _run, meta={"project_id": pid, "session_id": sid})}

    # ---- MCP 配置层（运行时工具桥后续批次；本端点只管 config/mcp.json） ----

    @app.get("/api/mcp")
    def get_mcp():
        if not MCP_CONFIG_PATH.is_file():
            return {"servers": []}
        return json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))

    @app.put("/api/mcp")
    def update_mcp(body: McpConfigIn):
        from core.tools.decompiler import _is_loopback_url

        names = [s.name for s in body.servers]
        if len(names) != len(set(names)):
            raise HTTPException(422, "MCP server 名称重复")
        for s in body.servers:
            if not _NAME_RE.fullmatch(s.name):
                raise HTTPException(422, f"非法 MCP server 名称: {s.name}")
            bad_domains = [d for d in s.domains if d not in _MCP_DOMAIN_WHITELIST]
            if bad_domains:
                raise HTTPException(
                    422, f"MCP server {s.name} 含未登记领域: {bad_domains}"
                         f"（白名单 {sorted(_MCP_DOMAIN_WHITELIST)}；新域消费时先在 app.py 登记）")
            is_http = s.transport in _MCP_TRANSPORTS_HTTP
            if is_http:
                if not s.url:
                    raise HTTPException(422, f"MCP server {s.name} 为 http 传输但 url 为空")
                if not _is_loopback_url(s.url):
                    # 红线：平台只连本机 MCP（IDA 进程内插件），不连远端
                    raise HTTPException(422, f"MCP server {s.name} 的 url 必须是本机 loopback 地址: {s.url}")
            elif s.transport == "stdio" and not s.command:
                raise HTTPException(422, f"MCP server {s.name} 为 stdio 传输但 command 为空")
        with pack_write_lock():  # config/ 受控写与 packs 同锁（设置页写串行化）
            MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _pack_history_backup(MCP_CONFIG_PATH)
            MCP_CONFIG_PATH.write_text(
                json.dumps({"servers": [s.model_dump() for s in body.servers]},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        return {"status": "ok", "count": len(body.servers)}

    # ---- 智能体工作台（K9，2026-09-29）：新独立轻量对话运行时 ----
    # 与会话窗/任务队列解耦：线程/消息存 chat_threads/chat_messages（core/chat/store.py），
    # 轮次由 core/chat/runtime.py 的 ChatTurn 跑（主控轻专家重，call_expert spawn
    # 持久子线程）。流式走 chat.delta/chat.tool/chat.message 事件（现有事件管道）。

    def _chat_agents(pid: str) -> list[dict]:
        proj = _project(pid)
        from core.skills.experts import load_expert
        rows = [{"id": "chat-orchestrator", "kind": "orchestrator"}]
        for eid in list_experts(app.state.packs_root, proj.track):
            if eid in {"_generalist", "chat-orchestrator"}:
                continue
            rows.append({"id": eid, "kind": "expert"})
        out = []
        for r in rows:
            try:
                e = load_expert(app.state.packs_root, r["id"], proj.track)
                out.append({**r, "name": e.get("name", r["id"]),
                            "description": e.get("description", "")})
            except FileNotFoundError:
                continue
        return out

    def _chat_bridge(proj):
        bridges = getattr(app.state, "chat_mcp_bridges", None)
        if bridges is None:
            bridges = {}
            app.state.chat_mcp_bridges = bridges
        bridge = bridges.get(proj.id)
        if bridge is None:
            from core.chat.mcp_bridge import MCPBridge
            domains = sorted({*proj.capabilities, proj.track})
            bridge = MCPBridge(MCP_CONFIG_PATH, domains=domains)
            bridges[proj.id] = bridge
        return bridge

    def _chat_running_set() -> set:
        running = getattr(app.state, "chat_running", None)
        if running is None:
            running = set()
            app.state.chat_running = running
        return running

    def _chat_abort_event(tid: str, *, create: bool = False) -> threading.Event | None:
        """每线程一个中止事件（stop 端点 set / ChatTurn 轮询）。
        轮次收尾弹出；create=False 且不存在时返回 None（线程没在跑）。"""
        events = getattr(app.state, "chat_abort_events", None)
        if events is None:
            events = {}
            app.state.chat_abort_events = events
        if create:
            ev = events.get(tid)
            if ev is None:
                ev = threading.Event()
                events[tid] = ev
            return ev
        return events.get(tid)

    @app.get("/api/chat/agents")
    def chat_agents(pid: str):
        return _chat_agents(pid)

    @app.get("/api/chat/mcp")
    def chat_mcp_status(pid: str):
        return {"servers": _chat_bridge(_project(pid)).status()}

    @app.get("/api/projects/{pid}/chat/threads")
    def chat_threads_list(pid: str, agent_id: str | None = None):
        from core.chat import store as chat_store
        return chat_store.list_threads(_project(pid).bb, pid, agent_id=agent_id)

    @app.post("/api/projects/{pid}/chat/threads", status_code=201)
    def chat_thread_create(pid: str, body: ChatThreadIn):
        from core.chat import store as chat_store
        agents = {a["id"] for a in _chat_agents(pid)}
        if body.agent_id not in agents:
            raise HTTPException(422, f"未知智能体: {body.agent_id}")
        return chat_store.create_thread(_project(pid).bb, pid, body.agent_id,
                                        title=body.title or "")

    @app.get("/api/chat/threads/{tid}")
    def chat_thread_detail(tid: str, after_id: int = 0):
        from core.chat import store as chat_store
        thread = chat_store.get_thread(_project(_pid_of_chat(tid)).bb, tid)
        if thread is None:
            raise HTTPException(404, f"线程不存在: {tid}")
        return {"thread": thread,
                "messages": chat_store.list_messages(
                    _project(thread["project_id"]).bb, tid, after_id=after_id)}

    @app.delete("/api/chat/threads/{tid}", status_code=204)
    def chat_thread_delete(tid: str):
        from core.chat import store as chat_store
        pid = _pid_of_chat(tid)
        if tid in _chat_running_set():
            raise HTTPException(409, "线程正在执行中，无法删除")
        if not chat_store.delete_thread(_project(pid).bb, tid):
            raise HTTPException(404, f"线程不存在: {tid}")

    def _pid_of_chat(tid: str) -> str:
        """线程 id → 项目 id（chat-<hex> 主键全局唯一，按索引表反查）。"""
        from core.chat import store as chat_store
        row = store_bb_conn(tid)
        if row is None:
            raise HTTPException(404, f"线程不存在: {tid}")
        return row

    def store_bb_conn(tid: str) -> str | None:
        # chat_threads 无项目索引缓存时直接扫各打开项目（工作台场景项目数有限）
        for p in app.state.projects.values():
            try:
                row = p.bb.conn.execute(
                    "SELECT project_id FROM chat_threads WHERE id=?",
                    (tid,)).fetchone()
            except Exception:  # noqa: BLE001
                continue
            if row is not None:
                return row["project_id"]
        return None

    @app.post("/api/chat/threads/{tid}/messages", status_code=202)
    def chat_send(tid: str, body: ChatMessageIn):
        """发消息即起跑一轮（后台线程）；流式经 chat.delta/chat.tool/chat.message
        事件（GET /api/projects/{pid}/events 或项目 WS）。同线程并发发送 409。"""
        from core.chat import store as chat_store
        from core.chat.runtime import ChatTurn, ORCHESTRATOR_ID
        pid = _pid_of_chat(tid)
        proj = _project(pid)
        thread = chat_store.get_thread(proj.bb, tid)
        if thread is None:
            raise HTTPException(404, f"线程不存在: {tid}")
        running = _chat_running_set()
        if tid in running or thread.get("status") == "running":
            raise HTTPException(409, "上一轮仍在执行中，稍候再发")
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "消息不能为空")
        exec_llm, _plan = _llms()
        agents = {a["id"] for a in _chat_agents(pid)}
        expert_names = sorted(agents) if thread["agent_id"] == ORCHESTRATOR_ID else []
        running.add(tid)
        abort_ev = _chat_abort_event(tid, create=True)
        abort_ev.clear()

        def _run() -> None:
            try:
                cfg = proj.bb.get_project(pid)["config"] or {}
                # 重装备工厂（与任务链会话工厂同款材料）：decompiler 每线程一
                # 实例（runner 走自建 gateway 审计、ida_db/ghidra_tmp 落项目
                # artifacts），browser 按轨注入共享池（轨外 None → no-tool 降级）
                from core.tools.decompiler import build_headless_service, gateway_runner

                def _decompiler_factory(session_id: str, author: str):
                    return build_headless_service(
                        proj.artifacts_dir / "decompiler-cache",
                        runner=gateway_runner(ExecutionGateway(bb=proj.bb),
                                              project_id=pid,
                                              session_id=session_id,
                                              author=author, timeout=900,
                                              workspace=proj.path),
                        ida_db_dir=proj.artifacts_dir / "decompiler-db",
                        ghidra_tmp_dir=proj.artifacts_dir / ".ghidra-tmp",
                        mcp_provider=lambda binary: app.state.ida_mcp_manager.ensure(
                            pid, binary,
                            db_dir=proj.artifacts_dir / "decompiler-db"))

                turn = ChatTurn(
                    bb=proj.bb, llm=exec_llm, project_id=pid, thread_id=tid,
                    packs_root=app.state.packs_root, track=proj.track,
                    capabilities=caps_effective(
                        app.state.packs_root, proj.track, proj.experts,
                        fallback=proj.capabilities),
                    mcp_bridge=_chat_bridge(proj),
                    expert_names=expert_names,
                    abort_event=abort_ev,
                    owner_tags=proj.bb.owner_tags(pid),
                    rule_profiles=cfg.get("rule_profiles"),
                    artifacts_dir=proj.artifacts_dir,
                    browser_pool=(app.state.browser_pool
                                  if proj.track in ("pentest", "redteam", "ctf")
                                  else None),
                    decompiler_factory=_decompiler_factory)
                turn.run(text, refs=body.refs)
            except Exception as e:  # noqa: BLE001 —— 状态已在 ChatTurn.run 归位
                log.exception("chat 轮后台执行失败 thread=%s", tid)
            finally:
                running.discard(tid)
                events = getattr(app.state, "chat_abort_events", None)
                if events is not None:
                    events.pop(tid, None)

        threading.Thread(target=_run, name=f"chat-{tid[-12:]}",
                         daemon=True).start()
        return {"status": "running", "thread_id": tid}

    @app.post("/api/chat/threads/{tid}/stop", status_code=204)
    def chat_stop(tid: str):
        """中止执行中的轮次：置中止事件 → ChatTurn 在步间/流式帧/工具分发点
        退出并落「已停止」说明（幂等：线程没在跑则 409）。"""
        if tid not in _chat_running_set():
            raise HTTPException(409, "线程未在执行中")
        ev = _chat_abort_event(tid, create=True)
        ev.set()

    # ---------- 情报面板（E9，全局模块，DESIGN.md §16；与项目黑板无关） ----------

    def _intel() -> IntelStore:
        if app.state.intel is None:
            app.state.intel = IntelStore(app.state.intel_dir)
        return app.state.intel

    def _intel_classifier():
        """classifier 小模型（打分/简报）；任何构建失败返回 None → 规则降级，不 503。"""
        if app.state.intel_llm is not None:
            return app.state.intel_llm
        try:
            router = ModelRouter()
            t = router.target_for("classifier")
            llm_store: ProviderStore = app.state.llm_store
            return llm_store.build(*t) if t else llm_store.build()
        except Exception:  # noqa: BLE001 —— 无 key/无供应商时情报功能照常（降级）
            return None

    @app.get("/api/intel/overview")
    def intel_overview():
        store = _intel()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        return {"counts": store.counts(), "today": store.get_brief(today),
                "top_unread": store.list_articles(unread_only=True, limit=5)}

    @app.get("/api/intel/feeds")
    def intel_feeds():
        return {"feeds": intel_config.load_feeds(app.state.intel_dir)}

    @app.put("/api/intel/feeds")
    def intel_update_feeds(body: IntelFeedsIn):
        with pack_write_lock():
            _pack_history_backup(Path(app.state.intel_dir) / "feeds.json")
            feeds = intel_config.save_feeds(
                [f.model_dump() for f in body.feeds], app.state.intel_dir)
        return {"status": "ok", "feeds": feeds}

    @app.get("/api/intel/profile")
    def intel_profile():
        return intel_config.load_profile(app.state.intel_dir)

    @app.put("/api/intel/profile")
    def intel_update_profile(body: IntelProfileIn):
        with pack_write_lock():
            _pack_history_backup(Path(app.state.intel_dir) / "profile.json")
            prof = intel_config.save_profile(body.model_dump(), app.state.intel_dir)
        return {"status": "ok", "profile": prof}

    @app.post("/api/intel/fetch")
    def intel_fetch_job():
        _intel()  # 404 前置：先确保存储就绪再提交 Job

        def _run() -> dict:
            return intel_run_refresh(
                _intel(), intel_config.load_profile(app.state.intel_dir),
                intel_config.load_feeds(app.state.intel_dir),
                llm=_intel_classifier(), getter=app.state.intel_getter)

        return {"job_id": app.state.jobs.submit("intel-refresh", _run)}

    @app.get("/api/intel/briefs")
    def intel_briefs():
        return {"briefs": _intel().list_briefs()}

    @app.get("/api/intel/briefs/{date}")
    def intel_brief(date: str):
        b = _intel().get_brief(date)
        if b is None:
            raise HTTPException(404, f"无 {date} 简报")
        return b

    @app.get("/api/intel/articles")
    def intel_articles(kind: str | None = None, unread: bool = False,
                       starred: bool = False, limit: int = 50):
        limit = max(1, min(limit, 200))
        return {"articles": _intel().list_articles(
            kind=kind, unread_only=unread, starred_only=starred, limit=limit)}

    @app.patch("/api/intel/articles/{article_id}")
    def intel_mark_article(article_id: str, body: IntelArticlePatch):
        row = _intel().mark_article(article_id, read=body.read, starred=body.starred)
        if row is None:
            raise HTTPException(404, f"文章不存在: {article_id}")
        return row

    # ---- E10：Obsidian vault 只读接入 + 学习档案 + 周计划（§16.3；正文不出本机） ----

    def _intel_profile_full() -> dict:
        return intel_config.load_profile(app.state.intel_dir)

    @app.get("/api/intel/vault")
    def intel_vault_info():
        prof = _intel_profile_full()
        info = _intel().note_stats()
        info.update(prof.get("vault", {}))
        info["configured"] = bool(prof.get("vault", {}).get("path"))
        return info

    @app.put("/api/intel/vault")
    def intel_update_vault(body: IntelVaultIn):
        prof = _intel_profile_full()
        prof["vault"] = {"path": body.path, "enabled": body.enabled}
        with pack_write_lock():  # profile.json 与画像同文件同锁同备份
            _pack_history_backup(Path(app.state.intel_dir) / "profile.json")
            prof = intel_config.save_profile(prof, app.state.intel_dir)
        job_id = None
        if prof["vault"]["enabled"] and prof["vault"]["path"]:
            job_id = _submit_vault_index(prof["vault"]["path"])
        return {"status": "ok", "vault": prof["vault"], "index_job_id": job_id}

    def _submit_vault_index(vault_path: str) -> str:
        def _run() -> dict:
            notes = intel_vault.index_vault(vault_path)
            n = _intel().replace_notes(notes)
            return {"indexed": n, "elapsed": None}

        return app.state.jobs.submit("vault-index", _run)

    @app.post("/api/intel/vault/index")
    def intel_vault_index_job():
        vpath = _intel_profile_full().get("vault", {}).get("path", "")
        if not vpath:
            raise HTTPException(422, "未配置 vault 路径（设置页 → 情报源）")
        return {"job_id": _submit_vault_index(vpath)}

    @app.get("/api/intel/vault/tree")
    def intel_vault_tree():
        notes = _intel().list_notes()
        if not notes:
            return {"configured": bool(_intel_profile_full().get("vault", {}).get("path")),
                    "tree": []}
        return {"configured": True, "tree": intel_vault.build_tree(notes)}

    @app.get("/api/intel/vault/search")
    def intel_vault_search(q: str = ""):
        return {"hits": _intel().search_notes(q)}  # 元数据+snippet，不返回全文

    @app.get("/api/intel/learning/profile")
    def intel_learning_profile_ep():
        return intel_learning_profile(_intel(), _intel_profile_full())

    @app.post("/api/intel/learning/plan")
    def intel_learning_plan_job():
        _intel()  # 404 前置
        week = intel_week_start()

        def _run() -> dict:
            store = _intel()
            prof = _intel_profile_full()
            agg = intel_learning_profile(store, prof)
            brief = store.get_brief(time.strftime("%Y-%m-%d", time.gmtime()))
            if brief is None:
                briefs = store.list_briefs()
                if briefs:
                    brief = store.get_brief(briefs[0]["date"])
            articles = store.list_articles(kind="article", limit=15)
            content, stats = intel_weekly_plan(agg, brief, articles, week,
                                               llm=_intel_classifier())
            # 隐私红线：inputs 只存统计，不存笔记正文
            store.save_plan(week, content, stats)
            return stats

        return {"job_id": app.state.jobs.submit("learning-plan", _run),
                "week": week}

    @app.get("/api/intel/learning/plan")
    def intel_learning_plan_ep(week: str | None = None):
        plan = _intel().get_plan(week) if week else _intel().latest_plan()
        if plan is None:
            raise HTTPException(404, "尚无学习计划")
        return plan

    @app.get("/api/intel/learning/plans")
    def intel_learning_plans_ep():
        return {"plans": _intel().list_plans()}

    # ---------- Job 轮询 ----------

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"任务不存在: {job_id}")
        return job

    # ---------- WebSocket 事件流 ----------

    @app.websocket("/api/ws/projects/{pid}")
    async def ws_events(ws: WebSocket, pid: str, since_id: int = 0,
                        tick_s: int = 5):
        await ws.accept()

        async def _reject() -> None:
            # 1008 = policy violation：项目删除中/已删除，前端据此停止重连
            if ws.application_state != WebSocketState.DISCONNECTED:
                await ws.close(code=1008)

        if pid in app.state.projects_closing:
            await _reject()
            return
        try:
            proj = await run_in_threadpool(_project, pid)
        except HTTPException:  # 项目已删除（404）或删除中（409）
            await _reject()
            return
        cursor = since_id
        # 应用层心跳（2026-09-29 消息刷新不及时复盘）：WS 名为推送实为 1s 查库，
        # 空转期零帧——半开连接（服务端→客户端方向静默死）时浏览器收不到 close
        # 帧、自身不发探测，连接永远 OPEN 但不打货，前端无限期卡住（切页面
        # 重挂载新建 WS 才恢复）。空闲满 tick_s 拍发一条不落库的 ws.tick 帧：
        # 前端看门狗据此区分「管道死」（20s 无任何帧→判死重连）与「健康静默」
        # （长命令执行期 tick 照常到达，不误判）。
        tick_s = max(1, min(tick_s, 60))
        idle_loops = 0
        try:
            while True:
                if pid in app.state.projects_closing:
                    await _reject()
                    return
                try:
                    events = await run_in_threadpool(
                        proj.bb.recent_events, pid, cursor, 100)
                except BlackboardClosedError:
                    # 删除路由 close_all 了旧实例：退出，不要重连复活连接
                    await _reject()
                    return
                except Exception:
                    # tick 正执行时连接被 close_all 关掉（sqlite 操作已关闭连接）
                    # 只在删除窗口内吞掉并优雅 1008；其他场景照常冒泡，不遮 bug
                    if pid in app.state.projects_closing:
                        await _reject()
                        return
                    raise
                if events:
                    idle_loops = 0
                for e in events:
                    await ws.send_json(e)
                    cursor = e["id"]
                if not events:
                    idle_loops += 1
                    if idle_loops >= tick_s:
                        await ws.send_json(
                            {"kind": "ws.tick", "ts": time.time()})
                        idle_loops = 0
                    await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            return

    # ---- F6-v2 浏览器实时画面流（CDP screencast → WS → 前端回传接管输入） ----

    @app.websocket("/api/projects/{pid}/browser/ws")
    async def ws_browser(ws: WebSocket, pid: str):
        """F6-v3：人工隐式会话（human-main）实时画面流——前端零会话概念，
        会话在服务端懒创建。"""
        await ws.accept()

        async def _close(code: int) -> None:
            # 1008=项目删除中/已删（前端停重连）；1011=浏览器侧不可用（依赖缺失）
            if ws.application_state != WebSocketState.DISCONNECTED:
                await ws.close(code=code)

        if pid in app.state.projects_closing:
            await _close(1008)
            return
        try:
            await run_in_threadpool(_project, pid)
        except HTTPException:  # 项目已删除（404）或删除中（409）
            await _close(1008)
            return
        if not browser_available():
            await ws.send_json({"type": "error",
                                "message": "playwright 未安装：pip install -e \".[browser]\" "
                                           "&& playwright install chromium"})
            await _close(1011)
            return
        inst = app.state.browser_pool.get_instance(pid)
        try:
            sid = await run_in_threadpool(inst.ensure_human_session)
            sid = sid["sid"]
        except BrowserError:
            await ws.send_json({"type": "error", "message": "浏览器会话创建失败"})
            await _close(1011)
            return

        frames: asyncio.Queue = asyncio.Queue(maxsize=30)

        def _put(q: asyncio.Queue, item: dict) -> None:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:  # 丢帧保最后，不背压浏览
                try:
                    q.get_nowait()
                    q.put_nowait(item)
                except Exception:  # noqa: BLE001
                    pass

        def _sink(frame: dict) -> None:
            # 在实例 loop 线程被调 → 桥回本（uvicorn）事件循环
            inst._loop.call_soon_threadsafe(_put, frames, frame)

        await run_in_threadpool(inst.attach_screencast, sid, _sink)

        async def _sender() -> None:
            while True:
                frame = await frames.get()
                await ws.send_json({"type": "frame", **frame})

        send_task = asyncio.create_task(_sender())
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "input":
                    kind = str(msg.get("kind", ""))
                    kw = {k: v for k, v in msg.items()
                          if k not in ("type", "kind")}
                    # 接管输入异步注入（不阻塞帧推送）；红线校验在 human_input 内
                    async def _inject(kind=kind, kw=kw):
                        try:
                            await run_in_threadpool(inst.human_input, sid, kind, **kw)
                        except Exception:  # noqa: BLE001 —— 注入失败静默（画面即真相）
                            pass
                    asyncio.create_task(_inject())
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            try:
                await run_in_threadpool(inst.detach_screencast, sid, _sink)
            except Exception:  # noqa: BLE001 —— 实例已停等：清理降级
                pass

    # SPA 同源静态托管（desktop-app-shell M2，DESIGN §1）：前端本就走相对路径
    # （/api/* 与 location.host 拼 WS），同源后零改动；Vite dev 代理仅开发期保留。
    # 挂载点在 return 前=路由注册序最后，全部 API/WS/docs 路由先匹配，兜底不遮蔽。
    if static_dir is not None:
        dist = Path(static_dir)
        index_html = dist / "index.html"
        if not index_html.is_file():
            raise ValueError(f"static_dir 缺 index.html：{dist}")

        @app.get("/{full_path:path}", include_in_schema=False)
        def _spa_fallback(full_path: str) -> FileResponse:
            top = full_path.split("/", 1)[0]
            # API/Swagger 例外不兜底（未知 API GET 回 404 而非 index.html，别坑调试）
            if top in ("api", "docs", "redoc") or full_path == "openapi.json":
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = (dist / full_path).resolve()
            # 防穿越（路径参数解码后可能含 ..\ 等分隔符）：resolve 后必须仍在产物目录内
            if (full_path
                    and str(candidate).startswith(str(dist.resolve()) + os.sep)
                    and candidate.is_file()):
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app
