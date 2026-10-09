"""黑板存储层（DESIGN.md §5）。

单一写入口原则：所有写操作走本模块（未来经 core API 暴露），
禁止旁路直写 SQLite。读操作全局开放（共享情报）。

返回值约定：统一 dict（sqlite3.Row 转换），ID 形如 `find-xxxxxxxx`。
写路径默认触发事件广播（kind 见各方法）。
"""

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from core.blackboard.events import EventBus
from core.blackboard.schema import init_schema

log = logging.getLogger(__name__)

NODE_TABLES = {"finding": "findings", "func_kb": "func_kb", "artifact": "artifacts"}

# 哨兵：区分"参数不传"与"传空值"（risk_tags=[] 语义=清空标签）
UNSET = object()

# findings 状态机白名单（研究轨 verified = 人工确认或带日志产物的动态验证）
FINDING_STATUSES = ("unverified", "verified", "false-positive")
FINDING_SEVERITIES = ("info", "low", "medium", "high", "critical")
# C6 发现分两类：vuln=漏洞（可验证的安全问题）/ intel=有效发现·关键发现（信息点、
# 防误报提示、合规提示、噪声管理等有价值的非漏洞结论）
FINDING_CATEGORIES = ("vuln", "intel")
VULNERABILITY_TYPES = (
    "未授权访问", "认证绕过", "水平越权", "垂直越权", "SQL 注入", "命令注入",
    "SSRF", "XSS", "文件读取", "文件上传", "路径穿越", "敏感信息泄露",
    "弱口令", "配置错误", "CSRF", "业务逻辑", "组件漏洞", "其他",
)


def validate_finding_pocs(pocs: Any) -> list[dict[str, str]]:
    """POC 只接受可复制的 HTTP 原始报文或 Python 脚本。"""
    if not isinstance(pocs, list):
        raise ValueError("pocs 必须是数组，每项为 {type: http|python, code}")
    out: list[dict[str, str]] = []
    for i, poc in enumerate(pocs, 1):
        if not isinstance(poc, dict):
            raise ValueError(f"POC 第 {i} 项必须是对象")
        kind, code = poc.get("type"), poc.get("code")
        if kind not in ("http", "python"):
            raise ValueError(f"POC 第 {i} 项 type 仅支持 http 或 python")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"POC 第 {i} 项 code 不能为空")
        out.append({"type": kind, "code": code.strip()})
    return out


def validate_formal_vuln(*, status: str, category: str, title: str,
                         vuln_class: str, severity: str, target_asset_id: str | None,
                         report: dict[str, str], pocs: list[dict[str, str]]) -> None:
    if status != "verified" or category != "vuln":
        return
    required = {
        "漏洞名称": title, "漏洞等级": severity, "漏洞类型": vuln_class,
        "受影响资产": target_asset_id or report["affected_assets"],
        "漏洞摘要": report["summary"], "测试环境": report["test_environment"],
        "操作步骤": report["reproduction_steps"],
        "验证结果": report["verification_result"],
        "风险影响评估": report["risk_assessment"], "POC": pocs,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise ValueError("正式漏洞缺少必填项：" + "、".join(missing))


def _check_vuln_gates(*, category: str, severity: str, status: str,
                      evidence: dict | None, poc_artifact_id: str | None,
                      track: str | None = None) -> None:
    """C6 漏洞门禁（防误报，宁严勿松）：category=vuln 的发现必须满足红线结构化条款，
    违例 raise ValueError（Agent 回填条款引用改道；人工路径同样拦截）。
    仅渗透/红队轨生效（track 参数；CTF/研究轨黑板不拦）。

    ① 漏洞类（category=vuln）severity=info 拒收（2026-09-18 起）——漏洞只收
       low..critical；**finding-severity-calibration（2026-10-09）收窄**：intel
       类 info 放行——「够不上 low 的观察」有低档去处，边界/疑似项不必往
       low/vuln 挤（防往上挤加压，矫正夸大）；
    ② status=verified → 必须带复现证据——收录格式新口径（finding-report-format
       2026-09-22）：evidence.repro_steps 至少一步 code（或 artifact_id）非空且
       该步 expected 非空；或旧结构（evidence.poc/pocs 或 poc_artifact_id，兼容
       存量直通）——红线「无证据不下结论」硬化为门禁；unverified 不拦（待验证
       是合法初态）。"""
    if track not in ("pentest", "redteam"):
        return
    if category != "vuln":
        return
    if severity == "info":
        # 2026-09-18 起漏洞类（category=vuln）不收 info；finding-severity-
        # calibration（2026-10-09）收窄为只拒 vuln 类——intel 类 info 放行，
        # 「够不上 low 的观察」有低档去处，边界/疑似项不必往 low/vuln 挤。
        raise ValueError(
            "收录门禁：渗透/红队轨漏洞类（category=vuln）不再收录 severity=info"
            "——有评级条款的按 low 并补充交互性实证登记，口径外信息（暴露面/"
            "合规提示）按 intel 线索登记（severity 可到 info）。"
            "可复制示例（漏洞按 low + 交互实证）："
            '{"category":"vuln","severity":"low","status":"unverified",'
            '"title":"…","evidence":{"repro_steps":[{"desc":"发送请求并观察回显",'
            '"type":"http","code":"GET /x HTTP/1.1\\nHost: target",'
            '"expected":"响应头回显注入点"}]}}；'
            "若只是信息性提示（非漏洞）则改 category=intel")
    if status == "verified":
        if not has_repro_evidence(evidence, poc_artifact_id):
            raise ValueError(
                "漏洞门禁：verified 漏洞必须带复现证据——按复现步骤登记"
                "（evidence.repro_steps：[{desc, type, code, expected}]，至少一步"
                " code/artifact_id 非空且该步预期结果 expected 非空），或旧结构 "
                "poc/pocs/poc_artifact_id（legacy）——红线「无证据不下结论」；"
                "先稳定复现再登记，或先按 unverified/有效发现登记。"
                "可复制最小示例："
                '{"category":"vuln","severity":"high","status":"verified",'
                '"title":"…","evidence":{"repro_steps":[{"desc":"重放 PoC 观察回显",'
                '"type":"cmd","code":"python poc.py --target …",'
                '"expected":"命令输出 uid=0(root)"}]}}'
                "（每步 desc 必填，且至少一步同时有 code/artifact_id 与 expected）")


# 收录格式（finding-report-format M1，2026-09-22）：evidence.repro_steps 步骤类型
# 白名单——http（原始报文）/ python（脚本）/ cmd（命令行，渲染 ```bash 围栏）/
# image（无代码块，artifact_id 必填指向图片产物，渲染嵌入）；旧值 http_raw 读时
# 映射 http、steps 视为纯文字步骤（写入不再产生）。
REPRO_STEP_TYPES = ("http", "python", "cmd", "image")
# 写入口宽容集：白名单 + 旧值（读时降级映射，不拒存量形态重写）
_REPRO_STEP_TYPES_WRITE = REPRO_STEP_TYPES + ("http_raw", "steps")


def validate_repro_steps(evidence: dict | None) -> None:
    """校验 evidence.repro_steps 复现步骤结构（收录格式一等结构，宁严勿松）：
    形态必须为 [{desc, type?, code?, expected?, artifact_id?, stability?, target?}]——
    desc 必填非空字符串（步骤描述）；type 白名单 {http,python,cmd,image}+
    旧值 {http_raw,steps}（image 步骤 artifact_id 必填）；expected/code 等须为
    字符串。非法一律 ValueError（API 422 / Agent 工具回填 [拒绝]）。"""
    steps = (evidence or {}).get("repro_steps")
    if steps is None:
        return
    if not isinstance(steps, list):
        raise ValueError(
            "evidence.repro_steps 必须是步骤数组 [{desc, type, code, expected, …}]")
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise ValueError(f"repro_steps 第 {i} 步必须是对象"
                             " {desc, type?, code?, expected?, artifact_id?, …}")
        desc = s.get("desc")
        if not isinstance(desc, str) or not desc.strip():
            raise ValueError(f"repro_steps 第 {i} 步缺非空 desc（步骤描述）")
        t = s.get("type")
        if t is not None and t != "" and t not in _REPRO_STEP_TYPES_WRITE:
            raise ValueError(
                f"repro_steps 第 {i} 步非法 type: {t}（允许 {REPRO_STEP_TYPES}）")
        if t == "image" and not s.get("artifact_id"):
            raise ValueError(f"repro_steps 第 {i} 步 type=image 必须带 artifact_id"
                             "（指向图片产物，渲染时嵌入）")
        for k in ("code", "expected", "stability", "target"):
            if s.get(k) is not None and not isinstance(s[k], str):
                raise ValueError(f"repro_steps 第 {i} 步 {k} 必须是字符串")


def has_repro_evidence(evidence: dict | None, poc_artifact_id: str | None = None) -> bool:
    """verified 门禁的证据判定（收录格式新口径 + 旧结构兼容，add/patch 共用）：
    - 新口径：evidence.repro_steps 非空，且至少一步 code（或 artifact_id）非空
      且该步 expected 非空（复现自证锚点）；
    - 旧结构兼容（存量直通）：evidence.poc（dict）/ evidence.pocs（list）/
      poc_artifact_id。
    """
    if poc_artifact_id:
        return True
    if not isinstance(evidence, dict):
        return False
    if isinstance(evidence.get("poc"), dict) and evidence["poc"]:
        return True
    if isinstance(evidence.get("pocs"), list) and evidence["pocs"]:
        return True
    steps = evidence.get("repro_steps")
    if not isinstance(steps, list):
        return False
    for s in steps:
        if not isinstance(s, dict):
            continue
        has_body = bool(str(s.get("code") or "").strip() or s.get("artifact_id"))
        if has_body and str(s.get("expected") or "").strip():
            return True
    return False

# 攻击链状态机：假说 → 验证 → 已利用
CHAIN_STATUSES = ("hypothesis", "validated", "exploited")

# 严重度排序（severity 就高不就低）
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def downgrade_severity(severity: str) -> str | None:
    """降一档（finding-severity-calibration P0 定级校准用，全项目唯一定义处）：
    critical→high→medium→low。low 已到地板（渗透/红队轨 info 拒收，low 是最低收录档）
    → 返 None，由调用方按「连最低档都撑不起」处理（转 intel）。未知值返 None。"""
    rank = SEVERITY_RANK.get(str(severity).strip().lower())
    if rank is None or rank <= SEVERITY_RANK["low"]:
        return None
    for name, r in SEVERITY_RANK.items():
        if r == rank - 1:
            return name
    return None

# 重复发现合并时做"按内容去重追加"的 evidence 列表键（§5.3 证据并集）；
# repro_steps（收录格式复现步骤）同列——多来源步骤并存、按内容指纹去重，
# 重复上报不重复堆步（人工修订步骤走 patch evidence 浅层合并整键替换）
EVIDENCE_LIST_UNION_KEYS = ("pocs", "repro_steps", "requests", "relates_to",
                            "screenshots")


def _evidence_item_fp(item: Any) -> str:
    """列表证据项的去重指纹。relates_to 项按 finding_id+note（同一边不重复堆），
    其余 dict 按稳定 JSON，原子值按 JSON。"""
    if isinstance(item, dict):
        if "finding_id" in item:
            return f"rel:{item['finding_id']}|{item.get('note', '')}"
        return json.dumps(item, ensure_ascii=False, sort_keys=True)
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def merge_finding_evidence(old: dict, new: dict) -> dict:
    """重复发现的证据并集语义（§5.3；A2 修复旧实现"新 evidence 整体丢弃"）：

    - 列表键（pocs/repro_steps/requests/relates_to/screenshots）：按内容去重追加，
      多来源并存；
    - notes 字符串：双方非空时分段追加（不覆盖前人笔记）；
    - 其余键：已有真值保留，空缺由新证据补入（不覆盖前人结论）。
    """
    merged = dict(old)
    for k, v in new.items():
        if k in EVIDENCE_LIST_UNION_KEYS and isinstance(v, list):
            base = merged.get(k)
            if not isinstance(base, list):
                base = []
            seen = {_evidence_item_fp(x) for x in base}
            for item in v:
                fp = _evidence_item_fp(item)
                if fp not in seen:
                    seen.add(fp)
                    base.append(item)
            merged[k] = base
        elif k == "notes" and isinstance(v, str) and v.strip():
            cur = merged.get(k)
            merged[k] = f"{cur.rstrip()}\n\n{v.strip()}" if isinstance(cur, str) and cur.strip() \
                else v.strip()
        elif merged.get(k) in (None, "", [], {}):
            merged[k] = v
    return merged


def validate_relates_to(conn: sqlite3.Connection, project_id: str,
                        evidence: dict | None) -> None:
    """校验 evidence.relates_to 强关系（E0，防幻觉，严格不静默丢）：

    - 形态必须是 [{finding_id: str, note?: str}, ...]；
    - 每个被引用 finding 必须存在且属于同一项目（无自引——新发现尚未入库）。

    非法形态/悬空/跨项目一律 ValueError（API → 422）。须在写事务内调用，
    读到的归属与后续写入同一事务视图。
    """
    rels = (evidence or {}).get("relates_to")
    if rels is None:
        return
    if not isinstance(rels, list):
        raise ValueError("evidence.relates_to 必须是 [{finding_id, note?}] 列表")
    for item in rels:
        if not isinstance(item, dict):
            raise ValueError("evidence.relates_to 每项必须是对象 {finding_id, note?}")
        fid = item.get("finding_id")
        if not isinstance(fid, str) or not fid:
            raise ValueError("evidence.relates_to 每项必须含非空字符串 finding_id")
        if not isinstance(item.get("note", ""), str):
            raise ValueError("evidence.relates_to.note 必须是字符串（关联理由）")
        owned = conn.execute(
            "SELECT 1 FROM findings WHERE id=? AND project_id=?", (fid, project_id),
        ).fetchone()
        if not owned:
            raise ValueError(
                f"relates_to 引用的发现不存在或不属于本项目: {fid}"
                "（先登记被引用发现，再建立强关系）")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {k: row[k] for k in row.keys()}


def _loads(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class BlackboardClosedError(RuntimeError):
    """黑板已被 close_all() 关闭（项目删除中）：拒绝任何惰性重连。

    没有它，仍持有 Blackboard 引用的线程（WS 1s tick / 在飞轮询）会经
    conn property 立刻重开 sqlite 连接，在 Windows 上重新锁死 db 文件，
    导致删除目录的 rename 必败。要重开须由 ProjectStore 新建 Blackboard 实例。"""


class Blackboard:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()  # 每线程独立连接（sqlite3 连接非线程安全）
        self._conns: set[sqlite3.Connection] = set()  # 全量登记（close_all 用）
        self._write_lock = threading.Lock()  # 进程内写路径互斥（配合 BEGIN IMMEDIATE）
        self._closed = False  # close_all 后置位；之后 .conn 拒绝重连（删项目防复活）
        # 闸门锁：把"检查 _closed→新建连接→登记 _conns"与 close_all 互斥。
        # 否则线程通过 _closed 检查后、sqlite3.connect 前被切走，close_all 先执行，
        # 恢复后会建出未登记的孤儿连接（重新锁死文件）或在目录移走后 OperationalError。
        self._gate_lock = threading.Lock()
        main = self._new_conn()
        main.execute("PRAGMA journal_mode=WAL")  # 库级持久设置，建库时设一次即可
        init_schema(main)
        main.close()
        self.bus = EventBus()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        """线程局部连接：并发读互不干扰（WebUI/多会话经线程池并发访问），
        写竞争由 WAL + busy_timeout + _tx 的 BEGIN IMMEDIATE 兜底。"""
        if self._closed:
            raise BlackboardClosedError(
                f"黑板已关闭（项目删除中），拒绝重连: {self.db_path}")
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        # 首建路径加锁双重检查：要么连接在 close_all 之前建好并会被它关到，
        # 要么 close_all 先到，这里抛 BlackboardClosedError——无孤儿连接窗口。
        with self._gate_lock:
            if self._closed:
                raise BlackboardClosedError(
                    f"黑板已关闭（项目删除中），拒绝重连: {self.db_path}")
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._new_conn()
                self._local.conn = conn
                self._conns.add(conn)
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
            self._conns.discard(conn)

    def close_all(self) -> None:
        """关闭所有线程的连接并置关闭闸门（Windows 删除项目目录前必须调；仅限已确认
        无会话运行时）。闸门置位后 .conn 拒绝惰性重连——否则仍持有本对象引用的
        WS tick/轮询线程会在毫秒级内重开连接锁死文件（删除竞态的根源）。
        需要重新访问须由 ProjectStore.open_project 新建实例，调用方还须在 API 层
        挡住删除窗口内的新请求（projects_closing 闸门）。"""
        with self._gate_lock:
            self._closed = True
            for c in list(self._conns):
                try:
                    c.close()
                except Exception:
                    pass
            self._conns.clear()
            self._local = threading.local()

    @contextmanager
    def _tx(self):
        """单事务写。sqlite3 层面 BEGIN IMMEDIATE 串行化写入，天然多会话安全；
        进程内另加写锁防嵌套事务（同线程同连接）。"""
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    # ---------- 项目 / 会话 ----------

    def create_project(self, name: str, track: str,
                       capabilities: list[str] | None = None,
                       config: dict | None = None,
                       project_id: str | None = None) -> dict:
        """project_id 缺省自生成；经 core/projects.py 创建时传入同 id（project.json 与库一致）。

        v2：track=场景轨（单选），capabilities=能力包（多选）。旧 domain 列同步写
        track 值，旧代码读 domain 不炸；旧库（domain=pentest/ctf）由 get_project
        经 LEGACY_DOMAIN_MAP 读兼容。"""
        from core.skills.taxonomy import LEGACY_DOMAIN_MAP

        caps = sorted(capabilities or [])
        # 旧域名字符串误传进来时透明映射，写新值不写旧值
        if track in LEGACY_DOMAIN_MAP:
            track, caps_from_legacy = LEGACY_DOMAIN_MAP[track]
            caps = caps or caps_from_legacy
        proj = {
            "id": project_id or new_id("proj"),
            "name": name,
            "domain": track,  # 旧列：写轨名兜底
            "track": track,
            "capabilities": json.dumps(caps, ensure_ascii=False),
            "config": json.dumps(config or {}, ensure_ascii=False),
            "created_at": now(),
        }
        with self._tx():
            self.conn.execute(
                "INSERT INTO projects(id,name,domain,track,capabilities,config,created_at)"
                " VALUES(:id,:name,:domain,:track,:capabilities,:config,:created_at)",
                proj,
            )
        proj["capabilities"] = caps
        proj["config"] = config or {}
        return proj

    def get_project(self, project_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        d = _row_to_dict(row)
        if d:
            d["config"] = _loads(d["config"], {})
            d["capabilities"] = _loads(d.get("capabilities", "[]"), [])
            if not d.get("track"):  # v1 旧库：domain 映射
                from core.skills.taxonomy import LEGACY_DOMAIN_MAP
                mapped = LEGACY_DOMAIN_MAP.get(d.get("domain", ""))
                if mapped:
                    d["track"], d["capabilities"] = mapped[0], list(mapped[1])
                else:
                    d["track"] = d.get("domain") or "ctf"
            d["domain"] = d["track"]  # 兼容旧读取方
        return d

    def update_project_config(self, project_id: str, config: dict) -> dict:
        """整体替换 projects 行 config（JSON）；project.json 由 ProjectStore 双写。
        不存在返回 None。配置合法性（autonomy 归一化）由 core.autonomy 负责。"""
        with self._tx():
            cur = self.conn.execute(
                "UPDATE projects SET config=:config WHERE id=:id",
                {"id": project_id,
                 "config": json.dumps(config or {}, ensure_ascii=False)})
            if cur.rowcount == 0:
                raise LookupError(f"项目不存在: {project_id}")
        return self.get_project(project_id)  # type: ignore[return-value]

    def update_project_track(self, project_id: str, track: str,
                             capabilities: list[str] | None = None) -> dict:
        """更新 projects 行场景轨（R1 轨退役迁移用）；domain 列同步写 track 值兜底。
        project.json 由调用方（ProjectStore/迁移脚本）双写；capabilities 省略时不改。
        不存在抛 LookupError。track 合法性由调用方经 project_binding 保证。"""
        caps_json = (json.dumps(sorted(capabilities), ensure_ascii=False)
                     if capabilities is not None else None)
        set_clause = "track=:track, domain=:track"
        if caps_json is not None:
            set_clause += ", capabilities=:caps"
        with self._tx():
            cur = self.conn.execute(
                f"UPDATE projects SET {set_clause} WHERE id=:id",
                {"id": project_id, "track": track,
                 **({"caps": caps_json} if caps_json is not None else {})})
            if cur.rowcount == 0:
                raise LookupError(f"项目不存在: {project_id}")
        return self.get_project(project_id)  # type: ignore[return-value]

    # ---------- 编排器状态 / 用量记账（v4，DESIGN §6.8） ----------

    def usage_state_get(self, project_id: str) -> dict:
        """取用量计数行；从未记账则零值（不建行）。"""
        row = self.conn.execute(
            "SELECT * FROM orchestrator_state WHERE project_id=?",
            (project_id,)).fetchone()
        return _row_to_dict(row) or {
            "tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0,
            "tokens_cache_creation": 0, "llm_calls": 0,
            "tasks_published": 0, "budget_warned": 0,
        }

    def usage_add_llm(
        self, project_id: str, *, ti: int, to: int,
        cache_read: int, cache_creation: int, token_budget: int | None,
    ) -> dict:
        """原子累加一次 LLM 调用用量，并在同一事务内做 80% 软警告去重/复位。

        返回计数行 + budget_warn（本次是否新跨阈值，调用方据此发事件）。
        budget_warned 自愈：预算调大后用量回落到阈值以下即复位，之后可再报。"""
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO orchestrator_state(project_id, updated_at)"
                " VALUES(?, '')", (project_id,))
            self.conn.execute(
                "UPDATE orchestrator_state SET"
                " tokens_in=tokens_in+:ti, tokens_out=tokens_out+:to,"
                " tokens_cache_read=tokens_cache_read+:cr,"
                " tokens_cache_creation=tokens_cache_creation+:cc,"
                " llm_calls=llm_calls+1, updated_at=:now WHERE project_id=:pid",
                {"pid": project_id, "ti": ti, "to": to, "cr": cache_read,
                 "cc": cache_creation, "now": now()})
            row = _row_to_dict(self.conn.execute(
                "SELECT * FROM orchestrator_state WHERE project_id=?",
                (project_id,)).fetchone())
            used = (row["tokens_in"] + row["tokens_out"]
                    + row["tokens_cache_read"] + row["tokens_cache_creation"])
            warn = False
            if token_budget:
                crossed = used * 100 >= 80 * int(token_budget)
                if crossed and not row["budget_warned"]:
                    warn = True
                if crossed != bool(row["budget_warned"]):
                    self.conn.execute(
                        "UPDATE orchestrator_state SET budget_warned=:w WHERE project_id=:pid",
                        {"w": 1 if crossed else 0, "pid": project_id})
                    row["budget_warned"] = 1 if crossed else 0
        row["budget_warn"] = warn
        return row

    def usage_inc_tasks(self, project_id: str) -> int:
        """orchestrator 自主发布任务计数 +1（task_budget 口径；人手发布不计）。"""
        with self._tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO orchestrator_state(project_id, updated_at)"
                " VALUES(?, '')", (project_id,))
            self.conn.execute(
                "UPDATE orchestrator_state SET tasks_published=tasks_published+1,"
                " updated_at=:now WHERE project_id=:pid",
                {"pid": project_id, "now": now()})
            row = self.conn.execute(
                "SELECT tasks_published FROM orchestrator_state WHERE project_id=?",
                (project_id,)).fetchone()
        return int(row["tasks_published"])

    def register_session(
        self, project_id: str, name: str, role: str = "_generalist", meta: dict | None = None
    ) -> dict:
        sess = {
            "id": new_id("sess"),
            "project_id": project_id,
            "name": name,
            "role": role,
            "status": "idle",
            "meta": json.dumps(meta or {}, ensure_ascii=False),
            "created_at": now(),
        }
        with self._tx():
            self.conn.execute(
                "INSERT INTO sessions(id,project_id,name,role,status,meta,created_at)"
                " VALUES(:id,:project_id,:name,:role,:status,:meta,:created_at)",
                sess,
            )
        return sess

    def list_sessions(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM session_inbox i"
            " WHERE i.to_session=s.id AND i.read_at IS NULL) AS unread"
            " FROM sessions s WHERE s.project_id=? ORDER BY s.created_at",
            (project_id,),
        ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    # ---------- 会话收件箱（§6.7 的 1.5/1.6：知会私信，与审批收件箱分设） ----------

    def inbox_post(
        self, project_id: str, to_session: str, kind: str, ref_id: str,
        payload: dict | None = None,
    ) -> bool:
        """投递一条私信。未读去重：同 (to_session, kind, ref_id) 已有未读行则不重发
        （部分唯一索引兜底）；读后可再次投递。返回是否新写入。
        本切片只有系统（撤回路由）调用，没有 Agent 自由消息工具。"""
        with self._tx():
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO session_inbox"
                "(id,project_id,to_session,kind,ref_id,payload,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (new_id("msg"), project_id, to_session, kind, ref_id,
                 json.dumps(payload or {}, ensure_ascii=False), now()),
            )
            return cur.rowcount > 0

    def inbox_list(self, project_id: str, session_id: str,
                   unread_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM session_inbox WHERE project_id=? AND to_session=?"
        params: list[Any] = [project_id, session_id]
        if unread_only:
            sql += " AND read_at IS NULL"
        sql += " ORDER BY created_at"
        out = []
        for r in self.conn.execute(sql, params):
            d = _row_to_dict(r) or {}
            d["payload"] = _loads(d.get("payload", "{}"), {})
            out.append(d)
        return out

    def inbox_drain(self, project_id: str, session_id: str,
                    exclude_kinds: tuple[str, ...] = (),
                    only_kinds: tuple[str, ...] = ()) -> list[dict]:
        """取出全部未读并标记已读（worker 认领开场白/步边界注入/空闲对话轮用），
        原子完成。

        exclude_kinds（2026-09-19 轮末语义）：指定 kind 的未读私信滞留收件箱
        不取走——步边界 drain 用它排除 human_note（人类引导延迟到下一轮认领期
        /恢复期注入，不在任务中途打断思路），其余调用不传参行为不变。
        only_kinds（2026-09-19 空闲对话轮）：反向过滤，只取指定 kind——对话轮
        drain 用它只取 human_note（basis_stale/finding_update 留给任务认领注入）。
        两参互斥，同时传 ValueError。"""
        if exclude_kinds and only_kinds:
            raise ValueError("inbox_drain: exclude_kinds 与 only_kinds 互斥")
        ts = now()
        where = "project_id=? AND to_session=? AND read_at IS NULL"
        params: list = [project_id, session_id]
        if exclude_kinds:
            where += " AND kind NOT IN (" + ",".join("?" * len(exclude_kinds)) + ")"
            params += list(exclude_kinds)
        if only_kinds:
            where += " AND kind IN (" + ",".join("?" * len(only_kinds)) + ")"
            params += list(only_kinds)
        with self._tx():
            rows = self.conn.execute(
                f"SELECT * FROM session_inbox WHERE {where} ORDER BY created_at",
                params,
            ).fetchall()
            if rows:
                self.conn.execute(
                    f"UPDATE session_inbox SET read_at=? WHERE {where}"
                    " AND id IN (" + ",".join("?" * len(rows)) + ")",
                    [ts, *params, *[r["id"] for r in rows]],
                )
        out = []
        for r in rows:
            d = _row_to_dict(r) or {}
            d["payload"] = _loads(d.get("payload", "{}"), {})
            out.append(d)
        return out

    def inbox_mark_read(
        self, project_id: str, session_id: str, ids: list[str] | None = None,
    ) -> int:
        """标记已读（UI 红点消除）；ids=None 读全部，否则只标给定行（仍限本会话）。"""
        with self._tx():
            if ids is None:
                cur = self.conn.execute(
                    "UPDATE session_inbox SET read_at=? WHERE project_id=? AND to_session=?"
                    " AND read_at IS NULL",
                    (now(), project_id, session_id),
                )
            else:
                placeholders = ",".join("?" * len(ids))
                cur = self.conn.execute(
                    f"UPDATE session_inbox SET read_at=? WHERE project_id=? AND to_session=?"
                    f" AND read_at IS NULL AND id IN ({placeholders})",
                    (now(), project_id, session_id, *ids),
                )
            return cur.rowcount

    def close_session(self, session_id: str) -> dict:
        """关闭会话：status='closed' + session.closed 事件。
        关窗只是 UI 层隐藏页签 + 编排不再复用，黑板数据全部保留；不可逆需另开新窗。"""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {session_id}")
        if row["status"] == "closed":
            raise ValueError(f"会话已关闭: {session_id}")
        with self._tx():
            # 运行期标记随关窗清掉（2026-09-24）：closed 窗残留 worker_armed=true
            # 是状态垃圾（活跃计数不看它，但 session_list/meta 语义失真）；
            # close_pending 是排水关窗的临时标记，同样不进终态。
            meta = _loads(row["meta"], {})
            if "worker_armed" in meta or "close_pending" in meta:
                meta.pop("worker_armed", None)
                meta.pop("close_pending", None)
                self.conn.execute(
                    "UPDATE sessions SET status='closed', meta=? WHERE id=?",
                    (json.dumps(meta, ensure_ascii=False), session_id))
            else:
                self.conn.execute(
                    "UPDATE sessions SET status='closed' WHERE id=?", (session_id,))
        self.append_event(
            row["project_id"], "session.closed",
            {"session_id": session_id, "summary": "人类关闭会话窗口"},
            session_id=session_id, author="human")
        updated = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(updated) or {}

    def delete_session(self, session_id: str, author: str = "human") -> dict:
        """物理删除会话窗（2026-09-26 侧栏悬停删除）：仅 closed 可删——活窗必须
        先走 API 层正规关窗。行级清除 sessions + 收件箱私信。
        任务机制退役（2026-10-06）后无 tasks 引用需清。session.deleted 事件即审计。
        不可逆。"""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {session_id}")
        if row["status"] != "closed":
            raise ValueError(f"会话未关闭（{row['status']}），先关窗再删除")
        project_id = row["project_id"]
        with self._tx():
            self.conn.execute(
                "DELETE FROM session_inbox WHERE to_session=?", (session_id,))
            self.conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self.append_event(
            project_id, "session.deleted",
            {"session_id": session_id, "name": row["name"], "role": row["role"],
             "summary": f"人类删除会话窗「{row['name']}」"},
            session_id=session_id, author=author)
        return {"id": session_id, "deleted": True}

    def set_session_status(self, session_id: str, status: str) -> dict:
        """会话状态流转（§3 会话控制）。closed 只走 close_session，不在此开放。"""
        if status not in {"idle", "running", "paused", "blocked"}:
            raise ValueError(f"非法会话状态: {status}")
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {session_id}")
        if row["status"] == "closed":
            raise ValueError(f"会话已关闭: {session_id}")
        with self._tx():
            self.conn.execute(
                "UPDATE sessions SET status=? WHERE id=?", (status, session_id))
        updated = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(updated) or {}

    def set_session_role(self, session_id: str, role: str) -> dict:
        """会话级换人留痕（会话中心化 §4.4，2026-09-25）：更新会话行当前身份。
        对话历史/黑板不动；closed 窗拒绝。换装动作与事件由 AgentSession / API 侧
        完成，本方法只落「当前身份」事实。"""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {session_id}")
        if row["status"] == "closed":
            raise ValueError(f"会话已关闭: {session_id}")
        with self._tx():
            self.conn.execute(
                "UPDATE sessions SET role=? WHERE id=?", (role, session_id))
        updated = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(updated) or {}

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(row)

    def set_session_meta(self, session_id: str, patch: dict) -> dict:
        """合并更新会话 meta（JSON 列；E8 暂停快照指针等运行期状态）。

        读-合并-写在单个 _tx() 内（并发 PATCH 不丢键）；值可为 None（键留存、语义清除）。"""
        with self._tx():
            row = self.conn.execute(
                "SELECT meta FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise ValueError(f"会话不存在: {session_id}")
            merged = {**_loads(row["meta"], {}), **patch}
            self.conn.execute(
                "UPDATE sessions SET meta=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), session_id))
        sess = self.get_session(session_id) or {}
        sess["meta"] = _loads(sess.get("meta"), {})
        return sess

    def post_human_note(self, project_id: str, to_session: str, text: str,
                        attachments: list[dict] | None = None) -> dict | None:
        """人类引导私信（E8 人工引导通道）：kind=human_note，作者=human。

        ref_id 用唯一 note id（不参与未读去重，连发多条各自投递）；落 message.inbox
        审计事件（作者 human，页签红点/已读全复用）。会话不存在抛 ValueError，
        已关闭返回 None（不投递）；成功返回 {id, text}。
        attachments（2026-09-19 附件随发）：[{id,path,name,size}]（API 层已校验
        artifact 存在且 kind=attachment），随 payload 投递，agent 层 _human_note_notice
        渲染成 📎 附件行；text 与 attachments 至少一项非空由调用方保证。"""
        row = self.conn.execute(
            "SELECT status FROM sessions WHERE id=?", (to_session,)).fetchone()
        if row is None:
            raise ValueError(f"会话不存在: {to_session}")
        if row["status"] == "closed":
            return None
        note_id = new_id("note")
        if not self.inbox_post(project_id, to_session, "human_note", note_id,
                               {"text": text,
                                "attachments": list(attachments or [])}):
            return None
        self.append_event(
            project_id, "message.inbox",
            {"to_session": to_session, "kind": "human_note", "ref_id": note_id,
             "title": text[:80] or (f"📎 附件×{len(attachments or [])}"
                                    if attachments else ""),
             "text": text,  # 2026-09-20 对话窗：事件带全文（title 保留向后兼容）
             "by": "human"},
            session_id=to_session, author="human")
        return {"id": note_id, "kind": "human_note", "text": text}

    def post_agent_message(
        self, project_id: str, from_session: str, to_session: str,
        subkind: str, text: str, refs: list[str] | None = None,
    ) -> dict | None:
        """Agent 私信（2026-09-20 会话窗对话化，§17 B1 落地）：会话窗之间的知会
        通道，kind=agent_message 单一收件箱 kind，三分类进 payload.subkind——
        intel（情报同步：新攻击面/新发现线索）/ handoff（工作移交：谁接手什么）/
        assist（协助请求：要数据/要复核）。三分类只影响前端样式与统计口径，
        投递语义一致；后续加分类零迁移。

        仍走黑板/收件箱**异步模型**（DESIGN G 组定稿：不做同步对话接力）——收件
        方在认领期/步边界/对话轮 drain 注入。from/to 不存在抛 ValueError；to 已
        关闭返回 None；text 截 4000 字符防事件表膨胀；ref_id 用唯一 msg id（不
        参与未读去重，连发多条各自投递）。落 message.inbox 审计事件（author=
        from_session，payload 全文）；成功返回 {id, subkind, text}。"""
        if subkind not in {"intel", "handoff", "assist"}:
            raise ValueError(f"未知 agent_message 分类: {subkind}")
        for sid_ in (from_session, to_session):
            row = self.conn.execute(
                "SELECT status FROM sessions WHERE id=?", (sid_,)).fetchone()
            if row is None:
                raise ValueError(f"会话不存在: {sid_}")
        if self.conn.execute(
                "SELECT status FROM sessions WHERE id=?", (to_session,)
        ).fetchone()["status"] == "closed":
            return None
        msg_id = new_id("msg")
        payload = {"subkind": subkind, "from": from_session,
                   "text": (text or "")[:4000], "refs": list(refs or [])}
        if not self.inbox_post(project_id, to_session, "agent_message",
                               msg_id, payload):
            return None
        self.append_event(
            project_id, "message.inbox",
            {"to_session": to_session, "kind": "agent_message", "ref_id": msg_id,
             **payload, "by": from_session},
            session_id=to_session, author=from_session)
        return {"id": msg_id, "kind": "agent_message", **payload}

    def list_active_sessions_by_role(self, project_id: str, role: str) -> list[dict]:
        """按角色列活跃会话（bb_notify to_role 广播的送达名单）：
        status != 'closed'，created_at 升序。role 为空串返回空列表（防全量误广播）。"""
        if not role:
            return []
        rows = self.conn.execute(
            "SELECT id, role, name, status FROM sessions"
            " WHERE project_id=? AND role=? AND status!='closed'"
            " ORDER BY created_at",
            (project_id, role),
        ).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    # ---------- 审批（§12 收件箱：创建/决策的唯一 core 入口） ----------

    def request_approval(
        self, project_id: str, action: dict, *, risk: str = "medium",
        requested_by: str, session_id: str | None = None,
    ) -> dict:
        """创建 pending 审批（net=real / 越界动作的请求入口）。返回含 id 的 dict。"""
        appr = {
            "id": new_id("appr"),
            "project_id": project_id,
            "session_id": session_id,
            "action": json.dumps(action, ensure_ascii=False),
            "risk": risk,
            "status": "pending",
            "requested_by": requested_by,
            "created_at": now(),
        }
        with self._tx():
            self.conn.execute(
                "INSERT INTO approvals(id,project_id,session_id,action,risk,status,"
                "requested_by,created_at)"
                " VALUES(:id,:project_id,:session_id,:action,:risk,:status,"
                ":requested_by,:created_at)",
                appr,
            )
        # 审批请求落事件（2026-09-28 内联审批卡）：此前请求只写表不落事件，直播流里
        # 「会话在等审批」不可见，决策只能去审批收件箱。作者=requested_by（编排委派
        # 进编排页签，worker 会话请求进所属会话页签）；摘要=action 去 op 后的紧凑
        # JSON 截断——卡片副标题一行可读，完整 action 在审批收件箱/事件 JSON 里。
        op = str(action.get("op") or "unknown")
        summary = json.dumps(
            {k: v for k, v in action.items() if k != "op"}, ensure_ascii=False)
        if len(summary) > 160:
            summary = summary[:157] + "..."
        self.append_event(
            project_id, "approval.requested",
            {"approval_id": appr["id"], "op": op, "summary": summary,
             "risk": risk, "requested_by": requested_by},
            session_id=session_id, author=requested_by)
        return appr

    def decide_approval(
        self, approval_id: str, decision: str, *,
        decided_by: str = "human", author: str = "human",
    ) -> dict:
        """审批决策（approved/rejected）：状态流转 + approval.{decision} 审计事件。
        decided_by/author 分离是为了演练脚本自批可标 demo-script(auto)，不冒充人类。"""
        if decision not in {"approved", "rejected"}:
            raise ValueError(f"非法决策: {decision}")
        row = self.conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise ValueError(f"审批不存在: {approval_id}")
        if row["status"] != "pending":
            raise ValueError(f"审批 {approval_id} 已处理（{row['status']}）")
        with self._tx():
            self.conn.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=?",
                (decision, decided_by, now(), approval_id))
        self.append_event(
            row["project_id"], f"approval.{decision}",
            {"approval_id": approval_id, "action": json.loads(row["action"]),
             "risk": row["risk"], "requested_by": row["requested_by"]},
            author=author)
        updated = self.conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return _row_to_dict(updated) or {}

    # ---------- 事件 ----------

    def append_event(
        self,
        project_id: str,
        kind: str,
        payload: dict | None = None,
        session_id: str | None = None,
        author: str = "system",
    ) -> int:
        """落库并广播。kind 约定：command / decision / finding.new / task.* / audit.deny ..."""
        created = now()
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO events(project_id,session_id,kind,payload,author,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    project_id,
                    session_id,
                    kind,
                    json.dumps(payload or {}, ensure_ascii=False),
                    author,
                    created,
                ),
            )
            event_id = cur.lastrowid
        event = {
            "id": event_id,
            "project_id": project_id,
            "session_id": session_id,
            "kind": kind,
            "payload": payload or {},
            "author": author,
            "created_at": created,
        }
        self.bus.publish(event)
        return event_id

    def recent_events(
        self, project_id: str, since_id: int = 0, limit: int = 200,
        session_id: str | None = None,
        before_id: int | None = None, tail: int = 0,
        kinds: list[str] | None = None,
        exclude_kinds: list[str] | None = None,
    ) -> list[dict]:
        """增量拉取：id > since_id，升序。WS 断线重连回放也走这里。

        before_id 非 None：向前翻页——取 id < before_id 的最后 limit 条（升序返回），
        直播间「上翻加载更早」分页用；tail>0：只取最新 tail 条（升序），直播间首屏
        增量加载用，不再全量回放历史（2026-09-17）。两者优先于 since_id。
        session_id 非 None 时只取该会话落的事件（会话级复盘取材，F8）。
        kinds 非空时只取这些事件类型（bb-query-filters M2，与上面三分支正交）；
        exclude_kinds 非空时剔除这些事件类型（orch-context-budget，2026-09-27——
        编排器事件窗剔除纯观测 kind，游标照推不重放）。"""
        sql = "SELECT * FROM events WHERE project_id=?"
        args: list = [project_id]
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(session_id)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        if exclude_kinds:
            sql += f" AND kind NOT IN ({','.join('?' * len(exclude_kinds))})"
            args.extend(exclude_kinds)
        desc = False
        if tail > 0:
            desc, limit = True, tail
        elif before_id is not None:
            sql += " AND id<?"
            args.append(before_id)
            desc = True
        else:
            sql += " AND id>?"
            args.append(since_id)
        sql += f" ORDER BY id {'DESC' if desc else 'ASC'} LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        if desc:
            rows = list(reversed(rows))
        out = []
        for r in rows:
            d = _row_to_dict(r)
            assert d is not None
            d["payload"] = _loads(d["payload"], {})
            out.append(d)
        return out

    def latest_event_id(self, project_id: str) -> int:
        """项目事件末端 id（无事件返 0）。编排游标追赶 backlog 用，保证只前进不回放。"""
        row = self.conn.execute(
            "SELECT MAX(id) FROM events WHERE project_id=?", (project_id,)).fetchone()
        return int(row[0] or 0)

    def prune_thinking_deltas(self, project_id: str, stream_id: str) -> int:
        """思考流式增量行清剪（2026-09-19）：终稿 llm.thinking 落库后删掉同流
        的 llm.thinking.delta 过渡行——审计只留终稿一条，事件表与流式化之前
        一样干净；中断/异常路径不调用（残留 delta = 被中断思考的现场审计）。"""
        with self._tx():
            cur = self.conn.execute(
                "DELETE FROM events WHERE project_id=? AND kind='llm.thinking.delta'"
                " AND json_extract(payload,'$.stream_id')=?",
                (project_id, stream_id))
            return cur.rowcount

    def prune_chat_deltas(self, project_id: str, stream_id: str) -> int:
        """回复流式增量行清剪（2026-09-20 对话窗）：终稿 agent.chat 落库后删掉
        同流的 agent.chat.delta 过渡行——语义同 prune_thinking_deltas；中断/
        异常路径不调用（残留 delta = 被中断回复的现场审计）。"""
        with self._tx():
            cur = self.conn.execute(
                "DELETE FROM events WHERE project_id=? AND kind='agent.chat.delta'"
                " AND json_extract(payload,'$.stream_id')=?",
                (project_id, stream_id))
            return cur.rowcount

    def prune_chat_thread_deltas(self, project_id: str, thread_id: str) -> int:
        """智能体工作台流式增量清剪（2026-10-04）：一轮正常收尾后删该线程的
        chat.delta / chat.thinking.delta 过渡行——终稿全文在 chat_messages，事件
        只承担实时可见；不删则每条携带累计全文的 delta 行会持续膨胀事件表。
        异常/中断路径不调用（残留 delta = 被中断思考的现场审计，同
        prune_thinking_deltas 纪律）。"""
        with self._tx():
            cur = self.conn.execute(
                "DELETE FROM events WHERE project_id=? AND kind IN"
                " ('chat.delta','chat.thinking.delta')"
                " AND json_extract(payload,'$.thread_id')=?",
                (project_id, thread_id))
            return cur.rowcount

    # ---------- HTTP 历史（v15，F6：浏览器抓包/重发/爆破统一入库） ----------

    def add_http_history(
        self, project_id: str, *, source: str, method: str, url: str,
        session_id: str | None = None, task_id: str | None = None,
        batch_id: str = "", meta: dict | None = None,
        status: int | None = None, req_headers: dict | None = None,
        req_body: str | None = None, resp_headers: dict | None = None,
        resp_body: str | None = None, resp_mime: str = "",
        body_truncated: bool = False, is_binary: bool = False,
        duration_ms: int | None = None,
    ) -> int:
        """写入一条 HTTP 交互历史。source：browser（Playwright 拦截）/
        replay（重发）/ intruder（爆破）。唯一写入口（黑板写红线）；
        body 截断/二进制归一化由调用方（core/browser/capture.py）完成。"""
        if source not in ("browser", "replay", "intruder"):
            raise ValueError(f"非法 http_history source: {source}")
        with self._tx():
            cur = self.conn.execute(
                "INSERT INTO http_history(project_id,session_id,task_id,source,batch_id,"
                "meta,method,url,status,req_headers,req_body,resp_headers,resp_body,"
                "resp_mime,body_truncated,is_binary,duration_ms,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project_id, session_id, task_id, source, batch_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                    method, url, status,
                    json.dumps(req_headers or {}, ensure_ascii=False),
                    req_body,
                    json.dumps(resp_headers or {}, ensure_ascii=False),
                    resp_body, resp_mime,
                    1 if body_truncated else 0,
                    1 if is_binary else 0,
                    duration_ms, now(),
                ),
            )
            return int(cur.lastrowid)

    def list_http_history(
        self, project_id: str, *, since_id: int = 0, limit: int = 100,
        batch_id: str | None = None, source: str | None = None,
        session_id: str | None = None,
    ) -> list[dict]:
        """增量拉取（id > since_id 升序，同 recent_events 游标模式）。
        返回行 body 默认截短预览（full=False 语义恒定；全量走 get_http_history）。"""
        sql = "SELECT * FROM http_history WHERE project_id=?"
        args: list = [project_id]
        if since_id > 0:
            sql += " AND id>?"
            args.append(since_id)
        if batch_id is not None:
            sql += " AND batch_id=?"
            args.append(batch_id)
        if source is not None:
            sql += " AND source=?"
            args.append(source)
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(session_id)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.conn.execute(sql, args).fetchall():
            d = _row_to_dict(r)
            assert d is not None
            d["meta"] = _loads(d["meta"], {})
            d["req_headers"] = _loads(d["req_headers"], {})
            d["resp_headers"] = _loads(d["resp_headers"], {})
            d["req_body"] = (d["req_body"] or "")[:200]
            d["resp_body"] = (d["resp_body"] or "")[:200]
            d["body_truncated"] = bool(d["body_truncated"])
            d["is_binary"] = bool(d["is_binary"])
            out.append(d)
        return out

    def get_http_history(self, project_id: str, row_id: int) -> dict | None:
        """单条全量（body 不截短；跨项目 id 一律 None 防 id 越权拉取）。"""
        r = self.conn.execute(
            "SELECT * FROM http_history WHERE id=? AND project_id=?",
            (row_id, project_id)).fetchone()
        if r is None:
            return None
        d = _row_to_dict(r)
        assert d is not None
        d["meta"] = _loads(d["meta"], {})
        d["req_headers"] = _loads(d["req_headers"], {})
        d["resp_headers"] = _loads(d["resp_headers"], {})
        d["body_truncated"] = bool(d["body_truncated"])
        d["is_binary"] = bool(d["is_binary"])
        return d

    def clear_http_history(
        self, project_id: str, *, batch_id: str | None = None,
        before_id: int | None = None,
    ) -> int:
        """清空/按批清/清 id<before_id 的旧行，返回删除行数。"""
        sql = "DELETE FROM http_history WHERE project_id=?"
        args: list = [project_id]
        if batch_id is not None:
            sql += " AND batch_id=?"
            args.append(batch_id)
        if before_id is not None:
            sql += " AND id<?"
            args.append(before_id)
        with self._tx():
            cur = self.conn.execute(sql, args)
            return cur.rowcount

    # ---------- 资产 ----------

    def upsert_asset(
        self,
        project_id: str,
        type_: str,
        value: str,
        parent_id: str | None = None,
        meta: dict | None = None,
        author: str = "system",
    ) -> dict:
        """按 (project, type, value, parent) 去重，重复返回既有记录。"""
        with self._tx():
            row = self.conn.execute(
                "SELECT id FROM assets WHERE project_id=? AND type=? AND value IS ? AND parent_id IS ?",
                (project_id, type_, value, parent_id),
            ).fetchone()
            if row:
                return {"id": row["id"], "created": False}
            asset_id = new_id("asset")
            self.conn.execute(
                "INSERT INTO assets(id,project_id,type,value,parent_id,meta,author,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    asset_id,
                    project_id,
                    type_,
                    value,
                    parent_id,
                    json.dumps(meta or {}, ensure_ascii=False),
                    author,
                    now(),
                ),
            )
        return {"id": asset_id, "created": True}

    def find_asset(self, project_id: str, type_: str, value: str) -> dict | None:
        """按 (project, type, value) 查既有资产（不看 parent）——重报合并路径用，
        避免去重键含 parent_id 导致同值资产插出多行。"""
        row = self.conn.execute(
            "SELECT * FROM assets WHERE project_id=? AND type=? AND value=?"
            " ORDER BY created_at LIMIT 1", (project_id, type_, value)).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row) or {}
        d["meta"] = _loads(d.get("meta"), {})
        return d

    def get_asset(self, asset_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        d = _row_to_dict(row)
        if d:
            d["meta"] = _loads(d["meta"], {})
        return d

    def set_asset_parent(self, asset_id: str, parent_id: str | None) -> dict:
        """改挂父资产（DESIGN.md §5.2 资产树）：parent_id=None = 摘挂为根行。
        校验：资产存在、父存在（None 除外）、不挂自己、沿 parent 链向上不得遇自己（防环）。
        upsert 去重键含 parent_id，补挂必须走本方法而不是重复 upsert。"""
        row = self.conn.execute(
            "SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise ValueError(f"资产不存在: {asset_id}")
        if row["parent_id"] == parent_id:  # 无变化：不发事件不空转
            return self.get_asset(asset_id)  # type: ignore[return-value]
        if parent_id == asset_id:
            raise ValueError("资产不能挂自己")
        if parent_id is not None:
            if self.conn.execute(
                    "SELECT 1 FROM assets WHERE id=?", (parent_id,)).fetchone() is None:
                raise ValueError(f"父资产不存在: {parent_id}")
            walker = parent_id
            while walker is not None:  # 防环：新祖先链上出现自己即拒绝
                if walker == asset_id:
                    raise ValueError("挂载会形成环（parent 链经过自身）")
                walker = self.conn.execute(
                    "SELECT parent_id FROM assets WHERE id=?", (walker,)
                ).fetchone()["parent_id"]
        with self._tx():
            self.conn.execute(
                "UPDATE assets SET parent_id=? WHERE id=?", (parent_id, asset_id))
        self.append_event(
            row["project_id"], "asset.reparent",
            {"asset_id": asset_id, "old_parent_id": row["parent_id"],
             "parent_id": parent_id},
            author="system")
        return self.get_asset(asset_id)  # type: ignore[return-value]

    def update_asset_meta(self, asset_id: str, patch: dict) -> dict:
        """合并更新资产 meta（title/scanned 等展示约定，§5.2）；不改 author/created_at。

        读-合并-写在单个 _tx() 内（A2：并发 PATCH 不丢键）。"""
        with self._tx():
            row = self.conn.execute(
                "SELECT meta FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise ValueError(f"资产不存在: {asset_id}")
            merged = {**_loads(row["meta"], {}), **patch}
            self.conn.execute(
                "UPDATE assets SET meta=?, revision=revision+1 WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), asset_id))
        return self.get_asset(asset_id)  # type: ignore[return-value]

    def delete_asset(self, asset_id: str, author: str = "human") -> dict:
        """物理删除**叶子**资产（资产树整理/走查数据清理）。

        防护（ValueError→API 409）：仍被 finding.target_asset_id 引用，或存在子资产
        ——先删/改挂发现、先处理子节点。删资产不级联 findings。落 asset.deleted 审计事件。
        资产不存在抛 LookupError（→API 404）。
        """
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise LookupError(f"资产不存在: {asset_id}")
            ref = self.conn.execute(
                "SELECT id FROM findings WHERE target_asset_id=? LIMIT 1",
                (asset_id,)).fetchone()
            if ref is not None:
                raise ValueError(
                    f"资产 {row['value']} 仍被发现 {ref['id']} 引用，请先删除或改挂该发现")
            child = self.conn.execute(
                "SELECT id FROM assets WHERE parent_id=? LIMIT 1", (asset_id,)).fetchone()
            if child is not None:
                raise ValueError(f"资产 {row['value']} 存在子资产，请先处理子资产再删除")
            self.conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            snapshot = {"project_id": row["project_id"], "type": row["type"],
                        "value": row["value"], "parent_id": row["parent_id"]}
        self.append_event(
            snapshot["project_id"], "asset.deleted",
            {"asset_id": asset_id, "by": author, "type": snapshot["type"],
             "value": snapshot["value"], "parent_id": snapshot["parent_id"]},
            author=author)
        return {"id": asset_id, **snapshot}

    def merge_assets(
        self,
        project_id: str,
        source_asset_id: str,
        target_asset_id: str,
        *,
        author: str = "system",
        reason: str = "",
    ) -> dict:
        """将源资产合并到目标资产，并保留源资产为目标的别名。

        合并是单事务操作：发现、意图锚点和子资产引用都会迁移，源资产随后被删除。
        binary 必须走 delete_binary 等样本生命周期流程，不能通过通用资产合并破坏分析
        结果。目标若在源资产子树内也拒绝，避免移动父节点时留下孤儿树。

        **同键发现折并**（merge-assets-finding-union，2026-10-09）：源资产上的发现
        若与目标资产上某条发现同 `dedup_key`（撞 UNIQUE(project_id,target_asset_id,
        dedup_key)），不再整单拒绝，而是按 add_finding 的 §5.3 并集语义把源发现
        **折并**进目标发现（保留目标行 id），并迁移全项目对源行的引用后删除源行；
        其余源发现照常重挂。折并逐条落 `finding.merged` 事件，`asset.merged` 记
        `findings_merged` 计数。子资产重复、binary、后代目标仍拒绝。
        """
        if source_asset_id == target_asset_id:
            raise ValueError("源资产和目标资产不能相同")
        with self._tx():
            source = self.conn.execute(
                "SELECT * FROM assets WHERE id=? AND project_id=?",
                (source_asset_id, project_id)).fetchone()
            target = self.conn.execute(
                "SELECT * FROM assets WHERE id=? AND project_id=?",
                (target_asset_id, project_id)).fetchone()
            if source is None:
                raise LookupError(f"源资产不存在或不属于本项目: {source_asset_id}")
            if target is None:
                raise LookupError(f"目标资产不存在或不属于本项目: {target_asset_id}")
            if source["type"] == "binary" or target["type"] == "binary":
                raise ValueError("binary 样本不能通过通用资产合并，请使用样本生命周期流程")

            # 目标是源的后代时，直接删除源会让目标及其兄弟节点失去稳定的树关系。
            walker = target["parent_id"]
            while walker is not None:
                if walker == source_asset_id:
                    raise ValueError("目标资产是源资产的后代，不能合并；请保留更高层资产")
                parent = self.conn.execute(
                    "SELECT parent_id FROM assets WHERE id=? AND project_id=?",
                    (walker, project_id)).fetchone()
                walker = parent["parent_id"] if parent else None

            source_findings = self.conn.execute(
                "SELECT * FROM findings WHERE project_id=? AND target_asset_id=?",
                (project_id, source_asset_id)).fetchall()
            # 折并计划（merge-assets-finding-union，2026-10-09）：源发现若与目标资产
            # 上某条发现同 dedup_key → 折并（§5.3 并集语义并入该行、删源行）；其余
            # 迁移（重挂 target_asset_id）。源侧 dedup_key 自身唯一、目标侧同键唯一
            # ⇒ 1:1 折并，绝无一对多。此前遇同键即整单拒绝（设计自锁），与
            # add_finding「同键即同一发现、自动并集」语义不一致，现抹平。
            folds: list[tuple[sqlite3.Row, sqlite3.Row]] = []
            moves: list[str] = []
            for sf in source_findings:
                existing = self.conn.execute(
                    "SELECT * FROM findings WHERE project_id=? AND target_asset_id=? "
                    "AND dedup_key=? LIMIT 1",
                    (project_id, target_asset_id, sf["dedup_key"]),
                ).fetchone()
                if existing is not None:
                    folds.append((sf, existing))
                else:
                    moves.append(sf["id"])

            children = self.conn.execute(
                "SELECT id, type, value FROM assets WHERE project_id=? AND parent_id=?",
                (project_id, source_asset_id)).fetchall()
            child_conflicts: list[str] = []
            for child in children:
                existing = self.conn.execute(
                    "SELECT id FROM assets WHERE project_id=? AND type=? AND value=? "
                    "AND parent_id=? AND id!=? LIMIT 1",
                    (project_id, child["type"], child["value"], target_asset_id,
                     source_asset_id),
                ).fetchone()
                if existing is not None:
                    child_conflicts.append(existing["id"])
            if child_conflicts:
                raise ValueError(
                    "合并会造成子资产重复，未执行: " + ", ".join(child_conflicts[:5]))

            source_meta = _loads(source["meta"], {})
            target_meta = _loads(target["meta"], {})
            aliases = target_meta.get("aliases")
            if not isinstance(aliases, list):
                aliases = []
            aliases.append({
                "id": source["id"], "type": source["type"], "value": source["value"],
                "parent_id": source["parent_id"], "status": source["status"],
                "meta": source_meta,
            })
            target_meta["aliases"] = aliases

            ts = now()
            # 折并执行顺序：并入目标行 → 迁移全项目对源行的引用 → 删源行 → 重挂余下
            fold_map: dict[str, str] = {}
            fold_events: list[dict] = []
            for sf, tf in folds:
                self._fold_finding_into_target(tf, sf)
                fold_map[sf["id"]] = tf["id"]
                fold_events.append({
                    "finding_id": tf["id"], "merged_from": sf["id"],
                    "title": sf["title"], "vuln_class": sf["vuln_class"],
                    "severity": sf["severity"], "status": sf["status"],
                })
            if folds:
                self._migrate_finding_refs(project_id, fold_map, ts)
                self.conn.execute(
                    "DELETE FROM findings WHERE project_id=? AND id IN (%s)"
                    % ",".join("?" * len(fold_map)),
                    (project_id, *fold_map.keys()))
            if moves:
                self.conn.execute(
                    "UPDATE findings SET target_asset_id=?, revision=revision+1 "
                    "WHERE project_id=? AND target_asset_id=?",
                    (target_asset_id, project_id, source_asset_id),
                )

            intent_rows = self.conn.execute(
                "SELECT id, target_asset_id, basis_refs, outcome_refs"
                " FROM intents WHERE project_id=?",
                (project_id,),
            ).fetchall()
            intents_moved = 0
            for intent in intent_rows:
                changed = False
                new_target = intent["target_asset_id"]
                if new_target == source_asset_id:
                    new_target = target_asset_id
                    changed = True
                refs = _loads(intent["basis_refs"], [])
                if not isinstance(refs, list):
                    refs = []
                new_refs: list[Any] = []
                for ref in refs:
                    replacement = ref
                    if ref == f"asset:{source_asset_id}":
                        replacement = f"asset:{target_asset_id}"
                    elif isinstance(ref, str) and ref.startswith("finding:") \
                            and ref[8:] in fold_map:
                        replacement = "finding:" + fold_map[ref[8:]]
                    if replacement != ref:
                        changed = True
                    if replacement not in new_refs:
                        new_refs.append(replacement)
                outcome = _loads(intent["outcome_refs"], [])
                if not isinstance(outcome, list):
                    outcome = []
                new_outcome: list[Any] = []
                for ref in outcome:
                    replacement = (
                        fold_map.get(ref, ref) if isinstance(ref, str) else ref)
                    if replacement != ref:
                        changed = True
                    if replacement not in new_outcome:
                        new_outcome.append(replacement)
                if changed:
                    self.conn.execute(
                        "UPDATE intents SET target_asset_id=?, basis_refs=?,"
                        " outcome_refs=?, revision=revision+1, updated_at=?"
                        " WHERE id=? AND project_id=?",
                        (new_target, json.dumps(new_refs, ensure_ascii=False),
                         json.dumps(new_outcome, ensure_ascii=False), now(),
                         intent["id"], project_id),
                    )
                    intents_moved += 1

            if children:
                self.conn.execute(
                    "UPDATE assets SET parent_id=?, revision=revision+1 "
                    "WHERE project_id=? AND parent_id=?",
                    (target_asset_id, project_id, source_asset_id),
                )
            self.conn.execute(
                "UPDATE assets SET meta=?, revision=revision+1 WHERE id=?",
                (json.dumps(target_meta, ensure_ascii=False), target_asset_id),
            )
            self.conn.execute(
                "DELETE FROM assets WHERE id=? AND project_id=?",
                (source_asset_id, project_id),
            )

            result = {
                "source_asset_id": source_asset_id,
                "target_asset_id": target_asset_id,
                "source_type": source["type"],
                "source_value": source["value"],
                "findings_moved": len(moves),
                "findings_merged": len(folds),
                "intents_moved": intents_moved,
                "children_moved": len(children),
                "alias": {"type": source["type"], "value": source["value"]},
            }
        self.append_event(
            project_id, "asset.merged",
            {**result, "reason": (reason or "")[:500], "by": author},
            author=author,
        )
        # 折并逐条留痕（发现级审计：哪条并进了哪条），与原资产合并事件分离
        for fe in fold_events:
            self.append_event(
                project_id, "finding.merged",
                {**fe, "source_asset_id": source_asset_id,
                 "target_asset_id": target_asset_id, "reason": "asset.merged"},
                author=author,
            )
        return result

    def _fold_finding_into_target(self, target_row: sqlite3.Row,
                                  source_row: sqlite3.Row) -> None:
        """把 source 发现按 §5.3 并集语义折并进 target 发现（保留 target 行 id，
        就地更新、revision+1）。语义对齐 add_finding 合并分支：证据并集（自环边
        清理）、severity 就高（rating_basis 随就高）、status 只升、confidence 取
        MAX、报告三件套/正文旧非空保留补空、pocs 按内容指纹去重并集。两条既有行
        各自过门禁入库，折并不复校（避免历史 verified 行被溯及拒绝）。"""
        merged_ev = merge_finding_evidence(
            _loads(target_row["evidence"], {}), _loads(source_row["evidence"], {}))
        tid = target_row["id"]
        rels = merged_ev.get("relates_to")
        if isinstance(rels, list):  # 并集后不得引用目标行自身（自环）
            merged_ev["relates_to"] = [
                r for r in rels
                if not (isinstance(r, dict) and r.get("finding_id") == tid)]
        new_severity = target_row["severity"]
        new_basis = target_row["rating_basis"]
        if SEVERITY_RANK.get(source_row["severity"], 0) > \
                SEVERITY_RANK.get(new_severity, 0):
            new_severity = source_row["severity"]
            new_basis = source_row["rating_basis"]
        new_status = target_row["status"]
        if target_row["status"] == "verified" or source_row["status"] == "verified":
            new_status = "verified"
        new_poc = target_row["poc_artifact_id"] or source_row["poc_artifact_id"]
        old_pocs = _loads(target_row["pocs"], [])
        old_pocs = old_pocs if isinstance(old_pocs, list) else []
        src_pocs = _loads(source_row["pocs"], [])
        src_pocs = src_pocs if isinstance(src_pocs, list) else []
        merged_pocs = list(old_pocs)
        fps = {json.dumps(p, ensure_ascii=False, sort_keys=True) for p in old_pocs}
        for p in src_pocs:
            fp = json.dumps(p, ensure_ascii=False, sort_keys=True)
            if fp not in fps:
                fps.add(fp)
                merged_pocs.append(p)
        self.conn.execute(
            "UPDATE findings SET evidence=?, severity=?, rating_basis=?, status=?,"
            " poc_artifact_id=?, impact=?, remediation=?, summary=?, affected_assets=?,"
            " test_environment=?, reproduction_steps=?, verification_result=?,"
            " risk_assessment=?, pocs=?, confidence=MAX(confidence,?), updated_at=?,"
            " revision=revision+1 WHERE id=? AND project_id=?",
            (json.dumps(merged_ev, ensure_ascii=False), new_severity, new_basis,
             new_status, new_poc,
             target_row["impact"] or source_row["impact"],
             target_row["remediation"] or source_row["remediation"],
             target_row["summary"] or source_row["summary"],
             target_row["affected_assets"] or source_row["affected_assets"],
             target_row["test_environment"] or source_row["test_environment"],
             target_row["reproduction_steps"] or source_row["reproduction_steps"],
             target_row["verification_result"] or source_row["verification_result"],
             target_row["risk_assessment"] or source_row["risk_assessment"],
             json.dumps(merged_pocs, ensure_ascii=False),
             source_row["confidence"], now(), tid, target_row["project_id"]),
        )

    def _migrate_finding_refs(self, project_id: str, fold_map: dict[str, str],
                              ts: str) -> None:
        """折并删源发现行前，把全项目对它的引用改指目标发现行：chain_links 的
        finding 节点（node_id）+ 各 findings.evidence.relates_to 的 finding_id。
        改写后按内容指纹去重、丢弃改动后指向自身的自环边。intents 的
        basis_refs（finding:）/outcome_refs 引用在 merge_assets 意图循环内随资产
        引用一并迁移，此处不重复。"""
        for src_id, tgt_id in fold_map.items():
            self.conn.execute(
                "UPDATE chain_links SET node_id=?"
                " WHERE node_type='finding' AND node_id=?",
                (tgt_id, src_id))
            for f in self.conn.execute(
                    "SELECT id,evidence FROM findings WHERE project_id=?",
                    (project_id,)):
                ev = _loads(f["evidence"], {})
                rels = ev.get("relates_to")
                if not isinstance(rels, list):
                    continue
                new_rels: list[Any] = []
                seen: set[str] = set()
                changed = False
                for r in rels:
                    if isinstance(r, dict) and r.get("finding_id") == src_id:
                        r = {**r, "finding_id": tgt_id}
                        changed = True
                    if isinstance(r, dict) and r.get("finding_id") == f["id"]:
                        changed = True  # 自环丢弃
                        continue
                    fp = _evidence_item_fp(r)
                    if fp in seen:
                        changed = True  # 重复边去重
                        continue
                    seen.add(fp)
                    new_rels.append(r)
                if changed:
                    ev["relates_to"] = new_rels
                    self.conn.execute(
                        "UPDATE findings SET evidence=?, updated_at=? WHERE id=?",
                        (json.dumps(ev, ensure_ascii=False), ts, f["id"]))

    def delete_binary(self, project_id: str, sha: str,
                      author: str = "human") -> dict:
        """物理删除一个样本（binary 资产）及其全部项目内依赖（2026-10-01 工作台）。

        硬级联（单 _tx 内，顺序即依赖）：chain_links（指向该样本 func_kb / finding
        的边）→ logic_block_funcs / logic_blocks（binary_sha256=sha）→ findings
        （target_asset_id=该 asset）→ func_kb（binary_sha256=sha）→ assets 行。
        资产不存在抛 LookupError（→API 404）；项目级逻辑块（binary_sha256=''）不动。
        磁盘产物（样本本体/缓存/DB/Ghidra 工程）由 API 层另行清理，不在此处理。
        落 binary.deleted 审计事件（含计数快照）。
        """
        with self._tx():
            asset = self.conn.execute(
                "SELECT * FROM assets WHERE project_id=? AND type='binary' AND value=?"
                " ORDER BY created_at LIMIT 1", (project_id, sha)).fetchone()
            if asset is None:
                raise LookupError(f"样本资产不存在: {sha}")
            asset_id = asset["id"]
            meta = _loads(asset["meta"], {})
            func_ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM func_kb WHERE project_id=? AND binary_sha256=?",
                (project_id, sha)).fetchall()]
            finding_ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM findings WHERE target_asset_id=?",
                (asset_id,)).fetchall()]
            block_ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM logic_blocks WHERE project_id=? AND binary_sha256=?",
                (project_id, sha)).fetchall()]
            n_links = 0
            if func_ids or finding_ids:
                clauses: list[str] = []
                params: list[Any] = [project_id]
                if func_ids:
                    clauses.append("(node_type='func_kb' AND node_id IN (%s))"
                                   % ",".join("?" * len(func_ids)))
                    params.extend(func_ids)
                if finding_ids:
                    clauses.append("(node_type='finding' AND node_id IN (%s))"
                                   % ",".join("?" * len(finding_ids)))
                    params.extend(finding_ids)
                n_links = self.conn.execute(
                    "DELETE FROM chain_links WHERE chain_id IN"
                    " (SELECT id FROM chains WHERE project_id=?) AND (%s)"
                    % " OR ".join(clauses), params).rowcount or 0
            if block_ids:
                self.conn.execute(
                    "DELETE FROM logic_block_funcs WHERE block_id IN (%s)"
                    % ",".join("?" * len(block_ids)), block_ids)
            self.conn.execute(
                "DELETE FROM logic_blocks WHERE project_id=? AND binary_sha256=?",
                (project_id, sha))
            self.conn.execute(
                "DELETE FROM findings WHERE target_asset_id=?", (asset_id,))
            self.conn.execute(
                "DELETE FROM func_kb WHERE project_id=? AND binary_sha256=?",
                (project_id, sha))
            self.conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            snapshot = {
                "asset_id": asset_id, "sha": sha,
                "filename": meta.get("filename"),
                "funcs": len(func_ids), "findings": len(finding_ids),
                "logic_blocks": len(block_ids), "chain_links": n_links,
            }
        self.append_event(
            project_id, "binary.deleted", {**snapshot, "by": author},
            author=author)
        return snapshot

    def list_assets(self, project_id: str, type_: str | None = None,
                    status: str | None = None, tag: str | None = None) -> list[dict]:
        """tag 过滤（编排器态势增强）：meta.tags 数组包含该标签的资产（大小写不敏感）。"""
        sql = "SELECT * FROM assets WHERE project_id=?"
        params: list[Any] = [project_id]
        if type_:
            sql += " AND type=?"
            params.append(type_)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at"
        out = []
        for r in self.conn.execute(sql, params):
            d = _row_to_dict(r) or {}
            d["meta"] = _loads(d.get("meta"), {})  # meta 列是 JSON 文本，读出解析回 dict
            if tag is not None:
                tags = [str(t).strip().lower()
                        for t in (d["meta"].get("tags") or []) if str(t).strip()]
                if tag.strip().lower() not in tags:
                    continue
            out.append(d)
        return out

    def latest_digest(self, project_id: str) -> dict | None:
        """最新一份 project.digest（编排器态势常驻注入用）；无则 None。"""
        row = self.conn.execute(
            "SELECT id, payload, created_at FROM events "
            "WHERE project_id=? AND kind='project.digest' "
            "ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        if row is None:
            return None
        try:
            payload = _loads(row["payload"], {})
        except Exception:  # noqa: BLE001
            payload = {}
        return {"event_id": row["id"], "digest": str(payload.get("digest") or ""),
                "created_at": row["created_at"]}

    def set_asset_status(self, asset_id: str, status: str, note: str | None = None,
                         author: str = "system",
                         expected_revision: int | None = None,
                         detail: dict | None = None) -> dict:
        """资产扫描/测试状态机（E7，§5.2；2026-09-19 扩六态）：复活 assets.status 死列。

        白名单六态 open/visited/scanning/tested_clean/budget_stop/na（借鉴 dsh
        AttackAtlas 覆盖四态），非法值抛 ValueError；
        **tested_clean/budget_stop/na 必带 note**（服务端强制——测了什么/为什么停/
        为什么不适用，防 AI 虚标干净）；
        同状态重复流转是 no-op（不 bump revision）；每次实际流转落 asset.status_changed
        审计事件。detail（2026-09-29 四问答案，Agent 工具层强制）：tested_what/
         viewpoint/why_no_finding 全文并入事件 payload 供事后对账（note 只截 200）。
        expected_revision（H2 乐观锁）：非空时与行 revision 比对，不符抛
        ValueError（多窗并发改同一资产防丢失更新——冲突方重新读取后再改）。
        「有发现」不由 AI 标：verified findings 由前端反查显徽章，结论以 findings 为准。
        资产不存在抛 LookupError（→API 404）。
        """
        if status not in ("open", "visited", "scanning", "tested_clean",
                          "budget_stop", "na"):
            raise ValueError(
                f"非法资产状态: {status}（open/visited/scanning/tested_clean/budget_stop/na）")
        if status in ("tested_clean", "budget_stop", "na") and not (note and note.strip()):
            raise ValueError(f"{status} 必须附 note（测了什么/为什么停/为什么不适用）")
        with self._tx():
            row = self.conn.execute(
                "SELECT project_id, status, revision FROM assets WHERE id=?",
                (asset_id,)).fetchone()
            if row is None:
                raise LookupError(f"资产不存在: {asset_id}")
            if expected_revision is not None and \
                    int(row["revision"] or 1) != int(expected_revision):
                raise ValueError(
                    f"乐观锁冲突：资产已被他人修改（当前 revision={row['revision']}，"
                    f"请求基于 {expected_revision}）——请重新读取后再改")
            if row["status"] == status:
                return self.get_asset(asset_id)  # type: ignore[return-value]
            # asset-tree-derived-clean M2（D3）：有子资产的节点，tested_clean 由
            # 子树全部终态读时派生，AI/人工显式写入一律拒绝（na 不挡——人工裁定）
            if status == "tested_clean":
                child = self.conn.execute(
                    "SELECT 1 FROM assets WHERE parent_id=? LIMIT 1",
                    (asset_id,)).fetchone()
                if child is not None:
                    raise ValueError(
                        "该资产存在子资产：父节点 tested_clean 由子树全部终态自动派生，"
                        "请流转子节点（不适用的面可对子节点标 na）")
                # tested-clean-intent-backing（2026-09-25；2026-10-01 改读链路图口径）：
                # 叶子 clean 须有直接围绕该资产的死路意图背书——祖先链批次覆盖
                # 已移除（一条父节点「基线一致」死路意图曾批量误标 12 个活站，
                # sess-1d692817a5d0）；死路收尾自带 dead_reason + 证据门禁，防粗略判净。
                # 2026-10-01：口径与攻击链路图同源（子目标级）——以本资产为根的
                # 子树内意图须**全部收尾**且至少一条 dead_end（见 intents.py）。
                from core.blackboard.intents import dead_end_backing_target
                if dead_end_backing_target(
                        self.conn, row["project_id"], asset_id) is None:
                    raise ValueError(
                        "tested_clean 须有死路意图背书（读链路图口径）：以本资产"
                        "为根的子目标下，意图须**全部收尾**且至少一条 dead_end——"
                        "对本资产 declare_intent 声明可证伪假设，close_intent"
                        "(outcome=dead_end) 带证据收尾后再标；如有 open 意图先收尾。"
                        "（每子目标独立立意，意图越多测得越全面；禁止父节点一条"
                        "意图批量覆盖子树）")
            self.conn.execute(
                "UPDATE assets SET status=?, revision=revision+1 WHERE id=?",
                (status, asset_id))
        self.append_event(
            row["project_id"], "asset.status_changed",
            {"asset_id": asset_id, "old": row["status"], "new": status,
             "note": (note or "")[:200], "by": author,
             **({"detail": detail} if detail else {})},
            author=author)
        return self.get_asset(asset_id)  # type: ignore[return-value]

    def owner_tags(self, project_id: str) -> list[str]:
        """项目资产 meta.owner 去重清单（开窗注入 owner 规则的数据源，DESIGN.md §4）。"""
        tags: list[str] = []
        for r in self.conn.execute(
            "SELECT meta FROM assets WHERE project_id=?", (project_id,)
        ):
            meta = _loads(r["meta"], {})
            tag = meta.get("owner")
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    # ---------- 产物 ----------

    def add_artifact(
        self,
        project_id: str,
        path: str,
        kind: str = "file",
        description: str = "",
        sha256: str = "",
        author: str = "system",
        meta: dict | None = None,
    ) -> str:
        import json as _json
        artifact_id = new_id("art")
        with self._tx():
            self.conn.execute(
                "INSERT INTO artifacts(id,project_id,path,kind,description,sha256,author,meta,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (artifact_id, project_id, path, kind, description, sha256, author,
                 _json.dumps(meta or {}, ensure_ascii=False), now()),
            )
        return artifact_id

    def get_artifact(self, project_id: str, artifact_id: str) -> dict | None:
        """按 id 取产物（带项目隔离）；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE id=? AND project_id=?",
            (artifact_id, project_id),
        ).fetchone()
        return _row_to_dict(row)

    def find_artifact_by_sha(self, project_id: str, sha256: str,
                             kind: str) -> dict | None:
        """按内容指纹取产物（附件上传去重用，2026-09-19）：同项目同 kind 同
        sha256 的最早一行；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM artifacts WHERE project_id=? AND kind=? AND sha256=?"
            " ORDER BY created_at, id LIMIT 1",
            (project_id, kind, sha256),
        ).fetchone()
        return _row_to_dict(row)

    def list_artifacts(self, project_id: str, *, task_id: str | None = None,
                       session_id: str | None = None) -> list[dict]:
        """产物清单（工作区隔离 W3）：可按归属元数据过滤（meta JSON 列，v10）。"""
        rows = self.conn.execute(
            "SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        import json as _json
        out: list[dict] = []
        for r in rows:
            d = _row_to_dict(r) or {}
            if task_id is not None or session_id is not None:
                try:
                    meta = _json.loads(d.get("meta") or "{}")
                except (ValueError, TypeError):
                    meta = {}
                if task_id is not None and meta.get("task_id") != task_id:
                    continue
                if session_id is not None and meta.get("session_id") != session_id:
                    continue
            out.append(d)
        return out

    # ---------- 发现（findings，指纹去重合并） ----------

    def add_finding(
        self,
        project_id: str,
        vuln_class: str = "",
        title: str = "",
        target_asset_id: str | None = None,
        severity: str = "info",
        status: str = "unverified",
        evidence: dict | None = None,
        poc_artifact_id: str | None = None,
        confidence: float = 0.5,
        dedup_key: str | None = None,
        author: str = "system",
        rating_basis: str = "",
        impact: str = "",
        remediation: str = "",
        summary: str = "",
        affected_assets: str = "",
        test_environment: str = "",
        reproduction_steps: str = "",
        verification_result: str = "",
        risk_assessment: str = "",
        pocs: list[dict] | None = None,
        category: str | None = None,
        track: str | None = None,
    ) -> dict:
        """重复发现（同 target+vuln_class+dedup_key）走证据并集（§5.3）：

        - evidence 列表键去重追加、notes 分段追加、其余键空缺补入（merge_finding_evidence）；
          evidence.repro_steps（收录格式复现步骤）在并集键内按内容指纹去重；
        - severity 就高、status 不因新报告降级（新报 verified 可升级）、confidence 取最大、
          poc_artifact_id 缺者回填；updated_at 刷新。
        - rating_basis（F11 判级依据）随 severity 就高覆盖：新报级别更高 → 取新报
          （空=清空，basis 必须证成当前 severity）；不高于旧级 → 保留旧值。
        - impact/remediation（收录格式三件套·危害描述/修复建议，v20）：合并分支
          旧值非空保留、空缺由新报补入（不覆盖前人结论）。
        - category（C6 分两类，全手动选）：vuln=漏洞 / intel=有效发现·关键发现；
          未传兜底 vuln；合并分支保留既有 category（同类发现不因重报换类）。
        - track（C6 漏洞门禁）：pentest/redteam 轨过漏洞门禁（vuln+info 拒、
          verified 须带复现证据——repro_steps 或旧结构 POC）；CTF/研究轨不拦。
        返回 {"id":..., "merged": bool, "rating_basis":..., "category":...}。
        merged=True 表示命中既有记录。
        """
        # F11 severity 白名单硬化：strip+lower 归一容错后必须命中五档（add 与 patch 同规）
        severity = str(severity).strip().lower()
        if severity not in FINDING_SEVERITIES:
            raise ValueError(f"非法 severity: {severity}（允许 {FINDING_SEVERITIES}）")
        # C6 category（全手动选）：显式传参生效；未传时按 severity 兜底——
        # info 本就不能是漏洞（门禁①），归 intel 才能让默认登记畅通
        if category is None:
            category = "intel" if severity == "info" else "vuln"
        if category not in FINDING_CATEGORIES:
            raise ValueError(f"非法 category: {category}（允许 {FINDING_CATEGORIES}）")
        clean_pocs = validate_finding_pocs(pocs or [])
        # C6 漏洞门禁（仅渗透/红队轨）：vuln+info 拒、vuln+verified 无复现证据拒（宁严勿松）
        _check_vuln_gates(category=category, severity=severity, status=status,
                          evidence=evidence, poc_artifact_id=poc_artifact_id,
                          track=track)
        # 收录格式复现步骤结构校验（写入口全轨生效：坏结构宁拒不存，读侧零兜底）
        validate_repro_steps(evidence)
        # rev 发现的类别在 evidence.category（五类），vuln_class 可空；
        # 无任何去重键时不做合并（否则空 key 的发现会全并成一条）。
        key = dedup_key or vuln_class or None
        created = now()
        update_changes: list[str] = []  # A4：merge 分支的实质增补（事务后发 finding_update）
        merged_verified = False  # 执行轨迹钩子（R3）：merge 分支升 verified 后重物化
        with self._tx():
            # relates_to 强关系：悬空/跨项目/形态非法一律拒（E0，防幻觉 422）；
            # 合并分支里并集只追加本批新边，校验传入部分即可
            validate_relates_to(self.conn, project_id, evidence)
            row = None
            severity_raise_blocked = False  # 就高被拒标志（P2，仅合并分支可能置真）
            if key is not None:
                row = self.conn.execute(
                    "SELECT * FROM findings"
                    " WHERE project_id=? AND target_asset_id IS ? AND dedup_key=?",
                    (project_id, target_asset_id, key),
                ).fetchone()
            if row:
                # 合并目标自引防护（E0）：新证据若 relates_to 指向它将要并入的那条发现，
                # 并集后会产生自环边——视为引用错误拒掉，而不是悄悄落一条自边。
                incoming_rels = (evidence or {}).get("relates_to")
                if isinstance(incoming_rels, list) and any(
                    isinstance(r, dict) and r.get("finding_id") == row["id"]
                    for r in incoming_rels
                ):
                    raise ValueError(
                        f"relates_to 不能引用发现自身（该发现命中同 dedup 键合并到 {row['id']}）："
                        "升级结论请直接重报/修补原发现，自引无意义")
                # read-modify-write 全在本事务内（BEGIN IMMEDIATE 串行化，无丢失更新）
                old_ev = _loads(row["evidence"], {})
                old_poc_n = len(old_ev.get("pocs") or []) if isinstance(old_ev, dict) else 0
                old_steps_n = (
                    len(old_ev.get("repro_steps") or []) if isinstance(old_ev, dict) else 0)
                old_rel_n = (
                    len(old_ev.get("relates_to") or []) if isinstance(old_ev, dict) else 0)
                merged_ev = merge_finding_evidence(old_ev, evidence or {})
                new_severity = row["severity"]
                # F11：basis 随 severity 就高覆盖（新报更高=取新报含空清空；否则保留旧值）
                new_basis = row["rating_basis"]
                if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(new_severity, 0):
                    # finding-severity-calibration P2（2026-10-09）：渗透/红队轨就高
                    # 须带新判级依据（rating_basis 非空）或新复现证据，否则保留旧级
                    # ——防「反复重报、空口垫高」。CTF/研究轨合并语义不变。
                    if track not in ("pentest", "redteam") or rating_basis.strip() \
                            or has_repro_evidence(evidence, poc_artifact_id):
                        new_severity = severity
                        new_basis = rating_basis
                    else:
                        severity_raise_blocked = True
                new_status = row["status"]
                if status == "verified":
                    new_status = "verified"
                new_poc = row["poc_artifact_id"] or poc_artifact_id
                # impact/remediation（v20 三件套）：旧值非空保留、空缺由新报补入
                new_impact = row["impact"] or impact
                new_remediation = row["remediation"] or remediation
                report = {
                    "summary": row["summary"] or summary.strip(),
                    "affected_assets": row["affected_assets"] or affected_assets.strip(),
                    "test_environment": row["test_environment"] or test_environment.strip(),
                    "reproduction_steps": row["reproduction_steps"] or reproduction_steps.strip(),
                    "verification_result": row["verification_result"] or verification_result.strip(),
                    "risk_assessment": row["risk_assessment"] or risk_assessment.strip(),
                }
                old_pocs = _loads(row["pocs"], [])
                old_pocs = old_pocs if isinstance(old_pocs, list) else []
                merged_pocs = list(old_pocs)
                poc_fps = {json.dumps(p, ensure_ascii=False, sort_keys=True) for p in old_pocs}
                for poc in clean_pocs:
                    fp = json.dumps(poc, ensure_ascii=False, sort_keys=True)
                    if fp not in poc_fps:
                        merged_pocs.append(poc)
                        poc_fps.add(fp)
                # Add submissions that include any structured report data are held
                # to the formal report contract. Legacy verified records can still
                # receive ordinary edits without retroactive migration.
                if any(report.values()) or clean_pocs:
                    validate_formal_vuln(
                        status=new_status, category=row["category"], title=title or row["title"],
                        vuln_class=vuln_class or row["vuln_class"], severity=new_severity,
                        target_asset_id=target_asset_id or row["target_asset_id"],
                        report=report, pocs=merged_pocs)
                # A4 变化检测（事务内算标志，事务后投递，多变化聚合一条 finding_update）
                if len(merged_ev.get("pocs") or []) > old_poc_n or (
                        not row["poc_artifact_id"] and poc_artifact_id):
                    update_changes.append("新增 POC")
                if len(merged_ev.get("repro_steps") or []) > old_steps_n:
                    update_changes.append("新增复现步骤")
                if new_severity != row["severity"]:
                    update_changes.append(f"严重度升至 {new_severity}")
                if row["status"] != "verified" and new_status == "verified":
                    update_changes.append("升级为已验证")
                    merged_verified = True
                if len(merged_ev.get("relates_to") or []) > old_rel_n:
                    update_changes.append("新增关联发现")
                if len(merged_pocs) > len(old_pocs):
                    update_changes.append("新增内嵌 POC")
                self.conn.execute(
                    "UPDATE findings SET evidence=?, severity=?, rating_basis=?, status=?,"
                    " poc_artifact_id=?, impact=?, remediation=?, summary=?, affected_assets=?,"
                    " test_environment=?, reproduction_steps=?, verification_result=?,"
                    " risk_assessment=?, pocs=?,"
                    " confidence=MAX(confidence,?), updated_at=?,"
                    " revision=revision+1 WHERE id=?",
                    (json.dumps(merged_ev, ensure_ascii=False), new_severity, new_basis,
                     new_status, new_poc, new_impact, new_remediation,
                     report["summary"], report["affected_assets"], report["test_environment"],
                     report["reproduction_steps"], report["verification_result"],
                     report["risk_assessment"], json.dumps(merged_pocs, ensure_ascii=False),
                     confidence,
                     created, row["id"]),
                )
                finding_id, merged = row["id"], True
                category = row["category"]  # C6：合并保留既有分类（同类发现不因重报换类）
            else:
                report = {
                    "summary": summary.strip(),
                    "affected_assets": affected_assets.strip(),
                    "test_environment": test_environment.strip(),
                    "reproduction_steps": reproduction_steps.strip(),
                    "verification_result": verification_result.strip(),
                    "risk_assessment": risk_assessment.strip(),
                }
                if any(report.values()) or clean_pocs:
                    validate_formal_vuln(
                        status=status, category=category, title=title, vuln_class=vuln_class,
                        severity=severity, target_asset_id=target_asset_id,
                        report=report, pocs=clean_pocs)
                finding_id = new_id("find")
                merged = False
                if key is None:  # 无去重键：以自身 id 为唯一指纹（列 NOT NULL，且永不合并）
                    key = finding_id
                self.conn.execute(
                    "INSERT INTO findings(id,project_id,target_asset_id,vuln_class,title,"
                    "severity,rating_basis,status,category,impact,remediation,summary,"
                    "affected_assets,test_environment,reproduction_steps,verification_result,"
                    "risk_assessment,pocs,evidence,"
                    "poc_artifact_id,confidence,dedup_key,author,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        finding_id,
                        project_id,
                        target_asset_id,
                        vuln_class,
                        title,
                        severity,
                        rating_basis,
                        status,
                        category,
                        impact.strip(),
                        remediation.strip(),
                        report["summary"], report["affected_assets"],
                        report["test_environment"], report["reproduction_steps"],
                        report["verification_result"], report["risk_assessment"],
                        json.dumps(clean_pocs, ensure_ascii=False),
                        json.dumps(evidence or {}, ensure_ascii=False),
                        poc_artifact_id,
                        confidence,
                        key,
                        author,
                        created,
                        created,
                    ),
                )
                new_basis = rating_basis
                new_severity = severity  # 生效级（合并分支已算就高值；首报=本报归一值）
        self.append_event(
            project_id,
            "finding.new" if not merged else "finding.merged",
            {"finding_id": finding_id, "vuln_class": vuln_class, "severity": new_severity,
             "rating_basis": new_basis, "category": category,
             # live-stream-ux C1（2026-09-23）：事件行人话渲染——title/资产归属/验证状态
             "title": title, "target_asset_id": target_asset_id, "status": status},
            author=author,
            # Agent 产出的 finding 才有会话归属（author=会话 id）；人工/系统写入不标
            session_id=author if isinstance(author, str) and author.startswith("sess-") else None,
        )
        if merged and update_changes:  # 首次创建不通知；纯重复上报无变化不通知
            self._notify_finding_updates(project_id, finding_id, update_changes, author)
        # 任务机制退役（2026-10-06）：执行轨迹链重物化（R3）随任务机制一并退役。
        out = {"id": finding_id, "merged": merged, "severity": new_severity,
               "rating_basis": new_basis, "category": category,
               "severity_raise_blocked": severity_raise_blocked}
        # M5 D1 疑似重复警告（orchestrator-efficiency §0-9）：同目标+同类
        # （vuln_class）但 dedup_key 不同 = 可能被不同指纹分裂的重复——只提示不阻塞
        # （Agent 可坚持新增）；合并分支（同 key 已自动并集）不算；无 target 或
        # vuln_class 空（逆向发现留 ''，同目标多函数发现是常态）不查。
        if target_asset_id and vuln_class:
            dups = self.conn.execute(
                "SELECT id, title FROM findings WHERE project_id=? AND"
                " target_asset_id IS ? AND vuln_class=? AND dedup_key != ?"
                " AND id != ? ORDER BY created_at DESC LIMIT 3",
                (project_id, target_asset_id, vuln_class, key, finding_id)).fetchall()
            if dups:
                out["dedup_warning"] = [{"id": d["id"], "title": d["title"]}
                                        for d in dups]
        # 阶段四 M2 黑板发布/订阅（2026-09-28）：新 finding 强关联广播——命中在跑
        # 目标任务的资产值 → 给认领会话发 agent_message(intel) 情报私信（非本人、
        # 未关闭、任务 open/claimed）。多会话协作核心：A 的发现主动喂给做同目标
        # 任务的 B，不再等 B 下次认领才看到。cap 目标数防风暴。
        if not merged:
            try:
                self._broadcast_finding_intel(project_id, finding_id, title,
                                              target_asset_id, author)
            except Exception:  # noqa: BLE001 —— 广播失败不影响登记主路径
                log.exception("finding intel 广播失败 finding=%s", finding_id)
        return out

    def _broadcast_finding_intel(
        self, project_id: str, finding_id: str, title: str,
        target_asset_id: str | None, by: str,
    ) -> None:
        """M2 强关联广播：新 finding 的资产值命中在跑（open/claimed）目标任务
        objective/scope → 向认领会话发 agent_message(intel)。排除作者本人与
        closed 会话；每任务目标 cap 5 条，防多会话频繁登记引发广播风暴。
        仅 Agent 产出的 finding（author=sess- 会话）广播——post_agent_message
        校验 from_session 必须存在，人工/系统登记无真实来源会话，跳过。
        纯增强：任何失败静默降级（登记主路径已返回）。"""
        if not target_asset_id:
            return
        if not (isinstance(by, str) and by.startswith("sess-")):
            return  # 人工/系统登记不广播（无真实 from 会话）
        try:
            asset = self.conn.execute(
                "SELECT value FROM assets WHERE id=? AND project_id=?",
                (target_asset_id, project_id)).fetchone()
        except Exception:  # noqa: BLE001
            asset = None
        if asset is None or not str(asset["value"] or "").strip():
            return
        av = str(asset["value"]).strip().lower()
        alive = self._alive_sessions(project_id)
        targets: list[str] = []
        # 任务机制退役（2026-10-06）：工作单元改由 Team 成员承接，广播目标改按
        # team_run_members 的 objective 命中同目标资产的在跑会话。
        try:
            rows = self.conn.execute(
                "SELECT rm.session_id, rm.objective FROM team_run_members rm"
                " JOIN team_runs r ON r.id=rm.run_id"
                " WHERE r.project_id=? AND rm.status IN ('running','creating')"
                " AND rm.session_id IS NOT NULL",
                (project_id,)).fetchall()
        except Exception:  # noqa: BLE001
            return
        for r in rows:
            if len(targets) >= 5:
                break
            blob = str(r["objective"] or "").lower()
            if av not in blob:
                continue
            sid = r["session_id"]
            if sid == by:
                continue  # 不给自己广播（登记方自然知道自己产出了什么）
            if not (isinstance(sid, str) and sid in alive and alive[sid] != "closed"):
                continue
            if sid not in targets:
                targets.append(sid)
        if not targets:
            return
        for sid in targets:
            try:
                self.post_agent_message(
                    project_id, by, sid, "intel",
                    f"🧠 黑板新发现（同目标）：{title[:80]}（{finding_id}）——"
                    "你的任务涉及该目标，先读此结论再动手，避免重复验证/重踩。")
            except Exception:  # noqa: BLE001 —— 单目标失败不影响其余
                log.exception("intel 广播投递失败 to=%s", sid)

    def list_findings(
        self,
        project_id: str,
        target_asset_id: str | None = None,
        min_severity: str | None = None,
        verified_only: bool = False,
        category: str | None = None,
    ) -> list[dict]:
        severity_rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        sql = "SELECT * FROM findings WHERE project_id=?"
        params: list[Any] = [project_id]
        if target_asset_id:
            sql += " AND target_asset_id=?"
            params.append(target_asset_id)
        if min_severity and min_severity in severity_rank:
            # min_severity 语义 = 不低于该级（如 high → high+critical）
            above = [s for s, r in severity_rank.items() if r >= severity_rank[min_severity]]
            sql += f" AND severity IN ({','.join('?' * len(above))})"
            params.extend(above)
        if verified_only:
            sql += " AND status='verified'"
        if category:  # C6 分两类过滤：vuln=漏洞 / intel=有效发现·关键发现
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY created_at"
        out = []
        for r in self.conn.execute(sql, params):
            d = _row_to_dict(r) or {}
            d["evidence"] = _loads(d["evidence"], {})
            d["pocs"] = _loads(d.get("pocs", "[]"), [])
            out.append(d)
        return out

    def get_finding(self, project_id: str, finding_id: str) -> dict | None:
        """按 id 取发现（带项目隔离）；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM findings WHERE id=? AND project_id=?",
            (finding_id, project_id),
        ).fetchone()
        d = _row_to_dict(row)
        if d:
            d["evidence"] = _loads(d.get("evidence", "{}"), {})
            d["pocs"] = _loads(d.get("pocs", "[]"), [])
        return d

    def patch_finding(
        self,
        project_id: str,
        finding_id: str,
        *,
        status: str | None = None,
        evidence: dict | None = None,
        title: str | None = None,
        severity: str | None = None,
        vuln_class: str | None = None,
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
        category: str | None = None,
        track: str | None = None,
        author: str = "human",
        expected_revision: int | None = None,
    ) -> dict | None:
        """人类修订发现：status 走白名单（非法 ValueError），evidence 浅层 merge。
        F10（§6.5 人工修订）：title 非空 / severity 五档白名单（strip+lower 归一容错）/
        vuln_class 自由文本可空；F11：rating_basis None=不动、空串=清空；收录格式
        （v20）：impact/remediation None=不动、空串=清空（不进门禁，报告侧校验齐备）
        ——**纯字段编辑零打扰**（不触发撤回传播与 finding_update
        私信，update_changes 只认 pocs/复现步骤/relates 增长与 verified 升级）。
        dedup_key 不重算（定稿）：指纹=创建时刻语义；改类别后同主题新发现仍并入旧指纹条目。
        expected_revision（H2 乐观锁）：非空时与行 revision 比对，不符抛 ValueError
        （多窗并发改同一发现防丢失更新——冲突方重新读取后再改）。
        发 finding.updated（新字段进 changed）；不存在返回 None；无字段可改时原样返回。

        read-modify-write 整体在单个 _tx() 内（A2：消除锁外读→锁内写的丢失更新）。"""
        changed: list[str] = []
        new_status = ""
        old_status = ""
        do_update = False
        update_changes: list[str] = []  # A4：实质增补（verified 升级 / pocs/relates 增长）
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM findings WHERE id=? AND project_id=?",
                (finding_id, project_id),
            ).fetchone()
            if row is None:
                return None
            if expected_revision is not None and \
                    int(row["revision"] or 1) != int(expected_revision):
                raise ValueError(
                    f"乐观锁冲突：发现已被他人修改（当前 revision={row['revision']}，"
                    f"请求基于 {expected_revision}）——请重新读取后再改")
            old_status = row["status"]
            old_ev = _loads(row["evidence"], {})
            old_poc_n = len(old_ev.get("pocs") or []) if isinstance(old_ev, dict) else 0
            old_steps_n = (
                len(old_ev.get("repro_steps") or []) if isinstance(old_ev, dict) else 0)
            old_rel_n = (
                len(old_ev.get("relates_to") or []) if isinstance(old_ev, dict) else 0)
            sets: list[str] = []
            params: list[Any] = []
            clean_pocs = validate_finding_pocs(pocs) if pocs is not None else None
            new_status = row["status"]
            if status is not None:
                if status not in FINDING_STATUSES:
                    raise ValueError(f"非法 finding status: {status}（允许 {FINDING_STATUSES}）")
                sets.append("status=?")
                params.append(status)
                new_status = status
                changed.append("status")
            if evidence is not None:
                # 强关系同样防幻觉：PATCH 携带 relates_to 时逐边校验存在性/同项目（E0）
                validate_relates_to(self.conn, project_id, evidence)
                # 复现步骤结构校验（收录格式，写入口全轨生效）
                validate_repro_steps(evidence)
                # 人类 PATCH = 浅层 merge（键级覆盖；列表并集只发生在 add_finding 合并路径）
                merged = {**old_ev, **evidence}
                sets.append("evidence=?")
                params.append(json.dumps(merged, ensure_ascii=False))
                changed.append("evidence")
                # A4：列表键增长算实质增补（浅层 merge 下列表为整键覆盖，比长度即可）
                if len(merged.get("pocs") or []) > old_poc_n:
                    update_changes.append("新增 POC")
                if len(merged.get("repro_steps") or []) > old_steps_n:
                    update_changes.append("新增复现步骤")
                if len(merged.get("relates_to") or []) > old_rel_n:
                    update_changes.append("新增关联发现")
            if title is not None:  # F10 人工修订：strip 后非空（空标题→直接删除该发现）
                t = str(title).strip()
                if not t:
                    raise ValueError("title 不能为空（空标题请直接删除该发现）")
                sets.append("title=?")
                params.append(t)
                changed.append("title")
            if severity is not None:
                s = str(severity).strip().lower()  # 归一容错
                if s not in FINDING_SEVERITIES:
                    raise ValueError(f"非法 severity: {severity}（允许 {FINDING_SEVERITIES}）")
                sets.append("severity=?")
                params.append(s)
                changed.append("severity")
            if vuln_class is not None:  # 自由文本可空（rev 轨发现 vuln_class='' 合法）
                sets.append("vuln_class=?")
                params.append(str(vuln_class).strip())
                changed.append("vuln_class")
            if rating_basis is not None:  # F11：None=不动，空串=清空（basis 必须证成当前 severity）
                sets.append("rating_basis=?")
                params.append(str(rating_basis).strip())
                changed.append("rating_basis")
            if impact is not None:  # v20 三件套·危害描述：None=不动，空串=清空（不进门禁）
                sets.append("impact=?")
                params.append(str(impact).strip())
                changed.append("impact")
            if remediation is not None:  # v20 三件套·修复建议：None=不动，空串=清空
                sets.append("remediation=?")
                params.append(str(remediation).strip())
                changed.append("remediation")
            report_changes = {
                "summary": summary,
                "affected_assets": affected_assets,
                "test_environment": test_environment,
                "reproduction_steps": reproduction_steps,
                "verification_result": verification_result,
                "risk_assessment": risk_assessment,
            }
            report_values: dict[str, str] = {}
            for col, value in report_changes.items():
                report_values[col] = str(row[col] or "") if value is None else str(value).strip()
                if value is not None:
                    sets.append(f"{col}=?")
                    params.append(report_values[col])
                    changed.append(col)
            stored_pocs = _loads(row["pocs"], [])
            if not isinstance(stored_pocs, list):
                stored_pocs = []
            effective_pocs = stored_pocs if clean_pocs is None else clean_pocs
            if clean_pocs is not None:
                sets.append("pocs=?")
                params.append(json.dumps(clean_pocs, ensure_ascii=False))
                changed.append("pocs")
            if category is not None:  # C6 分两类：vuln=漏洞 / intel=有效发现·关键发现
                c = str(category).strip().lower()
                if c not in FINDING_CATEGORIES:
                    raise ValueError(f"非法 category: {category}（允许 {FINDING_CATEGORIES}）")
                sets.append("category=?")
                params.append(c)
                changed.append("category")
            # C6 漏洞门禁（渗透/红队轨）：
            # ① 显式把 severity 改成 info → 拒（2026-09-18 起 info 全类别停收；
            #    只拦显式传 severity——纯标题/备注变更不受影响）
            # ② 分类/严重度变更后 vuln 不可为 info（漏洞只收 low~critical）
            # ③ 改判 verified → 必须带复现证据（repro_steps 或旧结构 POC，与 add 同口径）
            # FP 出口（status=FP）不受门禁影响（误报是合法出口）；纯标题/备注变更放行
            if track in ("pentest", "redteam"):
                if severity is not None and str(severity).strip().lower() == "info":
                    raise ValueError(
                        "收录门禁：渗透/红队轨不再收录 severity=info——口径外信息"
                        "（暴露面/合规提示）请删除或保留 intel 线索原级别")
                eff_cat = category if category is not None else (row["category"] or "vuln")
                eff_sev = severity if severity is not None else row["severity"]
                if (category is not None or severity is not None) \
                        and eff_cat == "vuln" and eff_sev == "info":
                    raise ValueError(
                        "漏洞门禁：severity=info 不能登记为漏洞（info 是信息提示）——"
                        "按「有效发现 intel」登记，或补充证据后提升严重度")
                if status is not None and status == "verified":
                    eff_ev = merged if evidence is not None else old_ev
                    if not effective_pocs and not has_repro_evidence(eff_ev, row["poc_artifact_id"]):
                        raise ValueError(
                            "漏洞门禁：verified 漏洞必须带复现证据——按复现步骤登记"
                            "（evidence.repro_steps：[{desc, type, code, expected}]，"
                            "至少一步 code/artifact_id 非空且该步 expected 非空），"
                            "或旧结构 poc/pocs/poc_artifact_id（legacy）——红线"
                            "「无证据不下结论」；先稳定复现再登记，"
                            "或先按 unverified/有效发现登记")
            effective_status = status if status is not None else row["status"]
            effective_category = category if category is not None else row["category"]
            effective_severity = severity if severity is not None else row["severity"]
            effective_title = title if title is not None else row["title"]
            effective_vuln_class = vuln_class if vuln_class is not None else row["vuln_class"]
            upgrading_to_verified = status == "verified" and row["status"] != "verified"
            editing_structured_report = any(
                value is not None for value in (
                    summary, affected_assets, test_environment, reproduction_steps,
                    verification_result, risk_assessment, pocs))
            # A legacy evidence-only upgrade is still supported. Once the new
            # report fields or embedded POCs are supplied, the record adopts the
            # complete structured contract.
            structured_upgrade = upgrading_to_verified and editing_structured_report
            if structured_upgrade or (row["status"] == "verified" and editing_structured_report):
                validate_formal_vuln(
                    status=effective_status, category=effective_category,
                    title=effective_title, vuln_class=effective_vuln_class,
                    severity=effective_severity, target_asset_id=row["target_asset_id"],
                    report=report_values, pocs=effective_pocs)
            if sets:
                sets.append("updated_at=?")
                sets.append("revision=revision+1")
                params.extend([now(), finding_id])
                self.conn.execute(
                    f"UPDATE findings SET {', '.join(sets)} WHERE id=?", params)
                do_update = True
        retracted = (
            do_update and old_status != "false-positive" and new_status == "false-positive"
        )
        if do_update and old_status != "verified" and new_status == "verified":
            update_changes.insert(0, "升级为已验证")
        if do_update:
            self.append_event(
                project_id, "finding.updated",
                {"finding_id": finding_id, "changed": changed, "status": new_status},
                author=author,
            )
        updated = self.get_finding(project_id, finding_id)
        if retracted:
            # 撤回传播（§6.7 的 1.6）：仅在非误报→误报的跳变瞬间触发一次，
            # 重复 PATCH 不重放。广播 + 四类目标私信/挂标见 _propagate_retraction。
            # 误报跳变只走 basis_stale，不发 finding_update（语义严格分开，A4）。
            self._propagate_retraction(project_id, finding_id, author, updated)
        elif do_update and update_changes:
            self._notify_finding_updates(project_id, finding_id, update_changes, author)
        if do_update and old_status != "verified" and new_status == "verified":
            # 执行轨迹重物化（R3）：任务机制退役（2026-10-06）后任务轨迹链一并退役。
            pass
        return updated

    def delete_finding(
        self, project_id: str, finding_id: str, author: str = "human",
    ) -> dict | None:
        """物理删除发现（垃圾/走查数据清理）。**误报请走 PATCH status=false-positive**
        ——那会触发撤回传播；删除不触发。同事务级联：

        - 其他 findings.evidence.relates_to 中指向它的边逐条摘除（含历史悬空引用）；
        - chain_links 不级联：链详情既有「孤儿实体 deleted:true 占位」语义；
        - session_inbox 私信保留（payload 标题快照自包含，同「关窗私信留审计」）；
        - poc_artifact 不删（产物可能复用）。

        落 finding.deleted 审计事件（快照 + 摘边数）。不存在返 None。
        """
        trimmed_rels = 0
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM findings WHERE id=? AND project_id=?",
                (finding_id, project_id)).fetchone()
            if row is None:
                return None
            ts = now()
            # 反向 relates_to 摘边（逐条 read-modify-write，全在本事务）
            for f in self.conn.execute(
                    "SELECT id,evidence FROM findings WHERE project_id=?", (project_id,)):
                ev = _loads(f["evidence"], {})
                rels = ev.get("relates_to")
                if not isinstance(rels, list):
                    continue
                kept = [r for r in rels
                        if not (isinstance(r, dict) and r.get("finding_id") == finding_id)]
                if len(kept) != len(rels):
                    ev["relates_to"] = kept
                    trimmed_rels += 1
                    self.conn.execute(
                        "UPDATE findings SET evidence=?, updated_at=? WHERE id=?",
                        (json.dumps(ev, ensure_ascii=False), ts, f["id"]))
            self.conn.execute("DELETE FROM findings WHERE id=?", (finding_id,))
            snapshot = {"title": row["title"], "vuln_class": row["vuln_class"],
                        "severity": row["severity"], "status": row["status"],
                        "target_asset_id": row["target_asset_id"]}
        self.append_event(
            project_id, "finding.deleted",
            {"finding_id": finding_id, "by": author, **snapshot,
             "trimmed_relates_to": trimmed_rels},
            author=author)
        return {"id": finding_id, **snapshot, "trimmed_relates_to": trimmed_rels}

    def _propagate_retraction(
        self, project_id: str, finding_id: str, by: str, finding: dict | None = None,
    ) -> None:
        """发现被推翻时的引用方传播（DESIGN.md §6.7 的 1.6，系统侧唯一投递点）：

        1. 项目级广播 finding.retracted（事件流/WS 全员可见，主代理下轮 tick 可见）；
        2. 私聊（session_inbox + message.inbox，按 (to_session,ref,kind) 未读去重）：
           a) 被推翻 finding 的作者会话；b) relates_to 反向边下游 finding 的作者。
        human/system 作者不私聊。closed 会话不投递（行留库，UI 不再显示）。
        任务机制退役（2026-10-06）后不再按任务 context_refs/parent 传播。
        """
        finding = finding or self.get_finding(project_id, finding_id)
        if finding is None:
            return
        evidence = finding.get("evidence") or {}
        payload = {
            "finding_id": finding_id,
            "title": finding.get("title", ""),
            "vuln_class": finding.get("vuln_class", ""),
            "by": by,
            "note": str(evidence.get("note", "")) if isinstance(evidence, dict) else "",
        }
        self.append_event(
            project_id, "finding.retracted",
            {"finding_id": finding_id, "title": payload["title"],
             "vuln_class": payload["vuln_class"], "by": by},
            author=by)

        alive = self._alive_sessions(project_id)

        def is_session(x: Any) -> bool:
            return isinstance(x, str) and x.startswith("sess-") and x in alive \
                and alive[x] != "closed"

        targets: set[str] = set()

        def add_target(sid: Any) -> None:
            if is_session(sid):
                targets.add(sid)  # type: ignore[arg-type]

        # a) 被推翻发现的作者会话
        add_target(finding.get("author"))
        # b) relates_to 反向边：以本发现为依据的下游发现作者
        for f in self.list_findings(project_id):
            rels = (f.get("evidence") or {}).get("relates_to") or []
            if any(isinstance(x, dict) and x.get("finding_id") == finding_id for x in rels):
                add_target(f.get("author"))

        # c/d) 任务机制退役：不再按任务 context_refs/parent 子树传播。

        for sid in sorted(targets):
            if self.inbox_post(project_id, sid, "basis_stale", finding_id, payload):
                self.append_event(
                    project_id, "message.inbox",
                    {"to_session": sid, "kind": "basis_stale",
                     "ref_id": finding_id, "title": payload["title"], "by": by},
                    session_id=sid, author="system")

    def _alive_sessions(self, project_id: str) -> dict[str, str]:
        """id→status 的未关窗会话映射（私信目标过滤用；closed 不投递）。"""
        return {
            r["id"]: r["status"]
            for r in self.conn.execute(
                "SELECT id,status FROM sessions WHERE project_id=?", (project_id,))
        }

    def _notify_finding_updates(
        self, project_id: str, finding_id: str, changes: list[str], by: str,
    ) -> None:
        """发现实质增补/升级的信息式私信（A4，kind='finding_update'）。

        任务机制退役（2026-10-06）后无任务引用方追踪——本通知恒无目标，保留
        方法签名供 add_finding/patch_finding 调用（不投递）。"""
        return

    # ---------- 函数知识库（func_kb，逆向防重复劳动主力，§5.4） ----------

    def upsert_func(
        self,
        project_id: str,
        binary_sha256: str,
        address: int,
        name: str,
        analysis: str = "",
        risk_tags: list[str] | None = None,
        confidence: float = 0.5,
        analyzed_by: str = "system",
    ) -> dict:
        """按 (project, binary, address) 去重。既有条目：名称入演变史、confidence 取最大、
        analysis 非空时追加（不覆盖前人结论）。返回 {"id":..., "created": bool}。
        """
        created = now()
        with self._tx():
            row = self.conn.execute(
                "SELECT id, name_history, confidence, analysis FROM func_kb"
                " WHERE project_id=? AND binary_sha256=? AND address=?",
                (project_id, binary_sha256, address),
            ).fetchone()
            if row:
                history = _loads(row["name_history"], [])
                if name and name != _loads(history[-1]["name"] if history else "", None):
                    history.append({"name": name, "by": analyzed_by, "at": created})
                new_analysis = row["analysis"]
                if analysis and analysis not in new_analysis:
                    new_analysis = (new_analysis + "\n" + analysis).strip()
                self.conn.execute(
                    "UPDATE func_kb SET name=?, name_history=?, analysis=?, risk_tags=?,"
                    " confidence=MAX(confidence,?), analyzed_by=?, updated_at=? WHERE id=?",
                    (
                        name,
                        json.dumps(history, ensure_ascii=False),
                        new_analysis,
                        json.dumps(risk_tags or [], ensure_ascii=False),
                        confidence,
                        analyzed_by,
                        created,
                        row["id"],
                    ),
                )
                func_id, is_new = row["id"], False
            else:
                func_id = new_id("func")
                is_new = True
                self.conn.execute(
                    "INSERT INTO func_kb(id,project_id,binary_sha256,address,name,name_history,"
                    "analysis,risk_tags,confidence,analyzed_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        func_id,
                        project_id,
                        binary_sha256,
                        address,
                        name,
                        json.dumps([{"name": name, "by": analyzed_by, "at": created}], ensure_ascii=False),
                        analysis,
                        json.dumps(risk_tags or [], ensure_ascii=False),
                        confidence,
                        analyzed_by,
                        created,
                        created,
                    ),
                )
        return {"id": func_id, "created": is_new}

    def lookup_func(self, project_id: str, binary_sha256: str, address: int) -> dict | None:
        """Agent 反编译前的强制查询入口（§5.4 硬规则）。"""
        row = self.conn.execute(
            "SELECT * FROM func_kb WHERE project_id=? AND binary_sha256=? AND address=?",
            (project_id, binary_sha256, address),
        ).fetchone()
        d = _row_to_dict(row)
        if d:
            d["name_history"] = _loads(d["name_history"], [])
            d["risk_tags"] = _loads(d["risk_tags"], [])
        return d

    def list_funcs_by_risk(self, project_id: str, binary_sha256: str, risk_tag: str) -> list[dict]:
        """按 risk_tag 过滤（JSON 数组成员匹配）。"""
        rows = self.conn.execute(
            "SELECT * FROM func_kb WHERE project_id=? AND binary_sha256=? AND risk_tags LIKE ?",
            (project_id, binary_sha256, f'%"{risk_tag}"%'),
        ).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r) or {}
            d["name_history"] = _loads(d.get("name_history", "[]"), [])
            d["risk_tags"] = _loads(d.get("risk_tags", "[]"), [])
            out.append(d)
        return out

    def list_funcs(self, project_id: str, binary_sha256: str | None = None) -> list[dict]:
        """按二进制列出函数库（Agent 动手前查重的入口，§5.4）。"""
        sql = "SELECT * FROM func_kb WHERE project_id=?"
        params: list[Any] = [project_id]
        if binary_sha256:
            sql += " AND binary_sha256=?"
            params.append(binary_sha256)
        rows = self.conn.execute(sql + " ORDER BY address", params).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r) or {}
            d = self._func_row_to_dict(r) or {}
            out.append(d)
        return out

    @staticmethod
    def _func_row_to_dict(row: sqlite3.Row | None) -> dict | None:
        d = _row_to_dict(row)
        if d:
            d["name_history"] = _loads(d.get("name_history", "[]"), [])
            d["risk_tags"] = _loads(d.get("risk_tags", "[]"), [])
        return d

    def get_func(self, project_id: str, func_id: str) -> dict | None:
        """按 id 取函数知识条目（带项目隔离）；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM func_kb WHERE id=? AND project_id=?",
            (func_id, project_id),
        ).fetchone()
        return self._func_row_to_dict(row)

    def patch_func(
        self,
        project_id: str,
        func_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        risk_tags: Any = UNSET,
        author: str = "human",
    ) -> dict | None:
        """人类修订函数知识（不含 confidence——那是 AI 字段，人不动）：

        - name：改名，旧名进 name_history；
        - note：以 ``## 笔记 <utc> by <author>`` 分段**追加** analysis；
        - risk_tags：传 list（含 []）=全量替换；不传(UNSET)=不动。
        发 func.updated；不存在返回 None。
        """
        stamp = now()
        # read-modify-write 整体在单个 _tx() 内（A2：并发改名/追加笔记不丢历史）
        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM func_kb WHERE id=? AND project_id=?",
                (func_id, project_id),
            ).fetchone()
            if row is None:
                return None
            history = _loads(row["name_history"], [])
            new_name = row["name"]
            changed: list[str] = []
            if name is not None and name != row["name"]:
                history.append({"name": name, "by": author, "at": stamp})
                new_name = name
                changed.append("name")
            analysis = row["analysis"] or ""
            if note and note.strip():
                section = f"## 笔记 {stamp} by {author}\n{note.strip()}"
                analysis = f"{analysis}\n\n{section}" if analysis else section
                changed.append("analysis")
            sets = ["name=?", "name_history=?", "analysis=?", "updated_at=?"]
            params: list[Any] = [
                new_name,
                json.dumps(history, ensure_ascii=False),
                analysis,
                stamp,
            ]
            if risk_tags is not UNSET:
                sets.append("risk_tags=?")
                params.append(json.dumps(list(risk_tags), ensure_ascii=False))
                changed.append("risk_tags")
            params.append(func_id)
            self.conn.execute(
                f"UPDATE func_kb SET {', '.join(sets)} WHERE id=?", params)
            binary_sha256, address = row["binary_sha256"], row["address"]
        self.append_event(
            project_id, "func.updated",
            {"func_id": func_id, "binary_sha256": binary_sha256,
             "address": hex(int(address)), "changed": changed},
            author=author,
        )
        return self.get_func(project_id, func_id)

    # ---------- 攻击链（chains，人工建链；节点必须引用本项目既有实体，防幻觉防跨项目） ----------

    def create_chain(self, project_id: str, name: str, goal: str = "", author: str = "human",
                     origin: str = "manual") -> str:
        """origin：manual=人工策划 / trace=任务轨迹自动物化（traces.py，board_graph 只画 manual）。"""
        chain_id = new_id("chain")
        with self._tx():
            self.conn.execute(
                "INSERT INTO chains(id,project_id,name,goal,status,origin,created_at,updated_at)"
                " VALUES(?,?,?,?, 'hypothesis', ?, ?, ?)",
                (chain_id, project_id, name, goal, origin, now(), now()),
            )
        self.append_event(
            project_id, "chain.created",
            {"chain_id": chain_id, "name": name, "goal": goal, "status": "hypothesis",
             "origin": origin},
            author=author,
        )
        return chain_id

    def list_chains(self, project_id: str, origin: str | None = None) -> list[dict]:
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM chain_links l WHERE l.chain_id=c.id) AS link_count"
               " FROM chains c WHERE c.project_id=?")
        params: list = [project_id]
        if origin is not None:  # v19：轨迹自动链与人工链分流（前端人工链视图默认只看 manual）
            sql += " AND c.origin=?"
            params.append(origin)
        sql += " ORDER BY c.updated_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) or {} for r in rows]

    def _owned_chain(self, project_id: str, chain_id: str) -> sqlite3.Row | None:
        """链归属校验：跨项目访问等同不存在。"""
        return self.conn.execute(
            "SELECT * FROM chains WHERE id=? AND project_id=?", (chain_id, project_id)
        ).fetchone()

    def update_chain(
        self, project_id: str, chain_id: str, *,
        name: Any = UNSET, goal: Any = UNSET, status: Any = UNSET,
        author: str = "human",
    ) -> dict | None:
        row = self._owned_chain(project_id, chain_id)
        if row is None:
            return None
        if status is not UNSET and status not in CHAIN_STATUSES:
            raise ValueError(f"非法链状态: {status}，允许: {list(CHAIN_STATUSES)}")
        sets, args, changed = [], [], {}
        if name is not UNSET:
            sets.append("name=?"); args.append(name); changed["name"] = name
        if goal is not UNSET:
            sets.append("goal=?"); args.append(goal); changed["goal"] = goal
        if status is not UNSET:
            sets.append("status=?"); args.append(status); changed["status"] = status
        if not sets:
            return _row_to_dict(row)
        sets.append("updated_at=?"); args.append(now()); args.append(chain_id)
        with self._tx():
            self.conn.execute(f"UPDATE chains SET {', '.join(sets)} WHERE id=?", args)
        self.append_event(
            project_id, "chain.updated", {"chain_id": chain_id, **changed}, author=author
        )
        return self.get_chain(chain_id)

    def delete_chain(self, project_id: str, chain_id: str, author: str = "human") -> bool:
        row = self._owned_chain(project_id, chain_id)
        if row is None:
            return False
        with self._tx():
            self.conn.execute("DELETE FROM chain_links WHERE chain_id=?", (chain_id,))
            self.conn.execute("DELETE FROM chains WHERE id=?", (chain_id,))
        self.append_event(
            project_id, "chain.deleted", {"chain_id": chain_id, "name": row["name"]},
            author=author,
        )
        return True

    def add_chain_link(
        self, project_id: str, chain_id: str, node_type: str, node_id: str,
        edge_note: str = "", author: str = "human",
    ) -> str:
        """链必须属本项目（缺链抛 LookupError→API 404）；节点必须真实且属本项目
        （ValueError→API 422）。seq 线性自动追加，调用方不传。"""
        if self._owned_chain(project_id, chain_id) is None:
            raise LookupError(f"攻击链不存在: {chain_id}")
        if node_type not in NODE_TABLES:
            raise ValueError(f"非法节点类型: {node_type}，允许: {sorted(NODE_TABLES)}")
        table = NODE_TABLES[node_type]
        exists = self.conn.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND project_id=?", (node_id, project_id)
        ).fetchone()
        if not exists:
            raise ValueError(
                f"攻击链节点不存在或不属本项目: {node_type}:{node_id}（禁止跨项目/引用未落库实体）"
            )
        link_id = new_id("clink")
        with self._tx():
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM chain_links WHERE chain_id=?",
                (chain_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            self.conn.execute(
                "INSERT INTO chain_links(id,chain_id,seq,node_type,node_id,edge_note,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (link_id, chain_id, seq, node_type, node_id, edge_note, now()),
            )
            self.conn.execute("UPDATE chains SET updated_at=? WHERE id=?", (now(), chain_id))
        self.append_event(
            project_id, "chain.link_added",
            {"chain_id": chain_id, "link_id": link_id, "seq": seq,
             "node_type": node_type, "node_id": node_id, "edge_note": edge_note},
            author=author,
        )
        return link_id

    def update_link_note(
        self, project_id: str, link_id: str, edge_note: str, author: str = "human"
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT l.* FROM chain_links l JOIN chains c ON l.chain_id=c.id"
            " WHERE l.id=? AND c.project_id=?", (link_id, project_id),
        ).fetchone()
        if row is None:
            return None
        with self._tx():
            self.conn.execute(
                "UPDATE chain_links SET edge_note=? WHERE id=?", (edge_note, link_id)
            )
            self.conn.execute(
                "UPDATE chains SET updated_at=? WHERE id=?", (now(), row["chain_id"])
            )
        self.append_event(
            project_id, "chain.updated",
            {"chain_id": row["chain_id"], "link_id": link_id, "edge_note": edge_note},
            author=author,
        )
        d = _row_to_dict(row) or {}
        d["edge_note"] = edge_note
        return d

    def delete_chain_link(self, project_id: str, link_id: str, author: str = "human") -> dict | None:
        """删后对该链剩余 link 按旧序重排 seq 1..n（线性序不留洞）。"""
        row = self.conn.execute(
            "SELECT l.* FROM chain_links l JOIN chains c ON l.chain_id=c.id"
            " WHERE l.id=? AND c.project_id=?", (link_id, project_id),
        ).fetchone()
        if row is None:
            return None
        chain_id = row["chain_id"]
        with self._tx():
            self.conn.execute("DELETE FROM chain_links WHERE id=?", (link_id,))
            remaining = self.conn.execute(
                "SELECT id FROM chain_links WHERE chain_id=? ORDER BY seq", (chain_id,)
            ).fetchall()
            for i, r in enumerate(remaining, start=1):
                self.conn.execute(
                    "UPDATE chain_links SET seq=? WHERE id=?", (i, r["id"])
                )
            self.conn.execute("UPDATE chains SET updated_at=? WHERE id=?", (now(), chain_id))
        self.append_event(
            project_id, "chain.link_removed",
            {"chain_id": chain_id, "link_id": link_id, "seq": row["seq"],
             "node_type": row["node_type"], "node_id": row["node_id"]},
            author=author,
        )
        return {"chain_id": chain_id, "link_id": link_id}

    def get_chain(self, chain_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM chains WHERE id=?", (chain_id,)).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row) or {}
        links = self.conn.execute(
            "SELECT * FROM chain_links WHERE chain_id=? ORDER BY seq", (chain_id,)
        ).fetchall()
        d["links"] = [_row_to_dict(l) or {} for l in links]
        return d

    # ---------- 蓝图（R4 逆向开发管线，DESIGN §9 R4） ----------

    # 蓝图状态流转白名单：只进不退；draft→reviewed→ready 三步是人类/审批职责
    # （Agent 工具不暴露 status 入口），ready→building→built 由重建侧驱动。
    BLUEPRINT_STATUSES = ("draft", "reviewed", "ready", "building", "built")
    BLUEPRINT_TRANSITIONS = {
        "draft": {"reviewed"},
        "reviewed": {"ready"},
        "ready": {"building"},
        "building": {"built"},
        "built": set(),
    }
    # 模块状态：pending 划分产出 → analyzed 深析完成 → specd 接口 spec 钉死 →
    # tested 容器自测通过（自测不过不得标 tested，纪律进 redlines）
    BLUEPRINT_MODULE_STATUSES = ("pending", "analyzed", "specd", "tested")

    @staticmethod
    def _normalize_modules(modules: Any) -> list[dict]:
        """modules JSON 归一化：必须是对象数组，每项有非空 name；未知字段丢弃、
        缺省补齐。供 create/modules_set/模块 patch 共用。"""
        if not isinstance(modules, list):
            raise ValueError("modules 必须是数组（每项含非空 name）")
        out: list[dict] = []
        seen: set[str] = set()
        for m in modules:
            if not isinstance(m, dict) or not str(m.get("name", "")).strip():
                raise ValueError("modules 每项必须是含非空 name 的对象")
            name = str(m["name"]).strip()
            if name in seen:
                raise ValueError(f"模块名重复: {name}")
            seen.add(name)
            addrs = m.get("func_addresses", [])
            if not isinstance(addrs, list):
                raise ValueError(f"模块 {name} 的 func_addresses 必须是数组")
            out.append({
                "name": name,
                "desc": str(m.get("desc", "")),
                "func_addresses": [str(a) for a in addrs],
                "spec": str(m.get("spec", "")),
                "notes": str(m.get("notes", "")),
                "status": m.get("status", "pending"),
            })
            if out[-1]["status"] not in Blackboard.BLUEPRINT_MODULE_STATUSES:
                raise ValueError(
                    f"模块 {name} 非法状态: {out[-1]['status']}，"
                    f"允许: {list(Blackboard.BLUEPRINT_MODULE_STATUSES)}")
        return out

    def _owned_blueprint(self, project_id: str, bp_id: str) -> sqlite3.Row | None:
        """蓝图归属校验：跨项目访问等同不存在。"""
        return self.conn.execute(
            "SELECT * FROM blueprints WHERE id=? AND project_id=?", (bp_id, project_id)
        ).fetchone()

    @staticmethod
    def _bp_row_to_dict(row: sqlite3.Row | None) -> dict | None:
        d = _row_to_dict(row)
        if d is not None:
            d["modules"] = _loads(d.get("modules", "[]"), [])
        return d

    def create_blueprint(
        self, project_id: str, name: str, goal: str = "",
        binary_sha256: str = "", modules: Any = None,
        content_md: str = "", author: str = "human",
    ) -> dict:
        name = str(name).strip()
        if not name:
            raise ValueError("蓝图 name 不能为空")
        mods = self._normalize_modules(modules if modules is not None else [])
        bp_id = new_id("bp")
        with self._tx():
            try:
                self.conn.execute(
                    "INSERT INTO blueprints(id,project_id,binary_sha256,name,goal,"
                    "status,content_md,modules,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,'draft',?,?,?,?)",
                    (bp_id, project_id, str(binary_sha256), name, goal,
                     content_md, json.dumps(mods, ensure_ascii=False), now(), now()),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(
                    f"同项目同样本下蓝图重名: {name}（换名或复用既有蓝图）") from e
        self.append_event(
            project_id, "blueprint.created",
            {"blueprint_id": bp_id, "name": name, "goal": goal,
             "binary_sha256": binary_sha256, "modules": len(mods), "status": "draft"},
            author=author,
        )
        d = self.get_blueprint(project_id, bp_id)
        assert d is not None
        return d

    def get_blueprint(self, project_id: str, bp_id: str) -> dict | None:
        row = self._owned_blueprint(project_id, bp_id)
        return self._bp_row_to_dict(row)

    def list_blueprints(self, project_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM blueprints WHERE project_id=? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
        return [self._bp_row_to_dict(r) or {} for r in rows]

    def update_blueprint_content(
        self, project_id: str, bp_id: str, *,
        content_md: Any = UNSET, content_append: Any = UNSET,
        goal: Any = UNSET, name: Any = UNSET,
        modules_set: Any = UNSET, author: str = "human",
    ) -> dict | None:
        """蓝图分区更新。content_append=追加正文（深析/汇总增量写）；content_md=整体
        替换；modules_set=整表替换模块（重划分）。Agent 侧经 tools 调本方法——
        status 不在此处（走 set_blueprint_status，Agent 无入口）。"""
        row = self._owned_blueprint(project_id, bp_id)
        if row is None:
            return None
        sets, args, changed = [], [], {}
        if name is not UNSET:
            if not str(name).strip():
                raise ValueError("蓝图 name 不能为空")
            sets.append("name=?"); args.append(str(name).strip()); changed["name"] = str(name).strip()
        if goal is not UNSET:
            sets.append("goal=?"); args.append(str(goal)); changed["goal"] = str(goal)
        if content_md is not UNSET:
            sets.append("content_md=?"); args.append(str(content_md)); changed["content_md"] = True
        if content_append is not UNSET and str(content_append):
            sets.append("content_md=?")
            args.append(str(row["content_md"]) + str(content_append))
            changed["content_appended_chars"] = len(str(content_append))
        if modules_set is not UNSET:
            mods = self._normalize_modules(modules_set)
            sets.append("modules=?")
            args.append(json.dumps(mods, ensure_ascii=False))
            changed["modules"] = len(mods)
        if not sets:
            return self._bp_row_to_dict(row)
        sets.append("updated_at=?"); args.append(now()); args.append(bp_id)
        with self._tx():
            self.conn.execute(
                f"UPDATE blueprints SET {', '.join(sets)} WHERE id=?", args)
        self.append_event(
            project_id, "blueprint.updated", {"blueprint_id": bp_id, **changed},
            author=author,
        )
        return self.get_blueprint(project_id, bp_id)

    def update_blueprint_module(
        self, project_id: str, bp_id: str, module_name: str, *,
        desc: Any = UNSET, spec: Any = UNSET, notes: Any = UNSET,
        func_addresses: Any = UNSET, status: Any = UNSET,
        author: str = "human",
    ) -> dict | None:
        """模块级 set（事务内读改写整个 modules JSON）：深析 agent 写回
        spec/notes/status 的单一入口。模块不存在抛 LookupError（API 404）。"""
        row = self._owned_blueprint(project_id, bp_id)
        if row is None:
            return None
        mods = _loads(row["modules"], [])
        target = next((m for m in mods if m.get("name") == module_name), None)
        if target is None:
            raise LookupError(f"模块不存在: {module_name}（现有: "
                              f"{[m.get('name') for m in mods]}）")
        changed: dict[str, Any] = {"module": module_name}
        if desc is not UNSET:
            target["desc"] = str(desc); changed["desc"] = True
        if spec is not UNSET:
            target["spec"] = str(spec); changed["spec"] = True
        if notes is not UNSET:
            target["notes"] = str(notes); changed["notes"] = True
        if func_addresses is not UNSET:
            if not isinstance(func_addresses, list):
                raise ValueError("func_addresses 必须是数组")
            target["func_addresses"] = [str(a) for a in func_addresses]
            changed["func_addresses"] = len(target["func_addresses"])
        if status is not UNSET:
            if status not in self.BLUEPRINT_MODULE_STATUSES:
                raise ValueError(
                    f"模块非法状态: {status}，允许: {list(self.BLUEPRINT_MODULE_STATUSES)}")
            target["status"] = status; changed["module_status"] = status
        with self._tx():
            self.conn.execute(
                "UPDATE blueprints SET modules=?, updated_at=? WHERE id=?",
                (json.dumps(mods, ensure_ascii=False), now(), bp_id))
        self.append_event(
            project_id, "blueprint.updated", {"blueprint_id": bp_id, **changed},
            author=author,
        )
        return self.get_blueprint(project_id, bp_id)

    def set_blueprint_status(
        self, project_id: str, bp_id: str, status: str, author: str = "human",
    ) -> dict | None:
        """状态流转（白名单校验，只进不退）。Agent 工具不暴露本入口——
        draft→reviewed→ready 是人类/审批职责；API 层 author='human'。"""
        row = self._owned_blueprint(project_id, bp_id)
        if row is None:
            return None
        if status not in self.BLUEPRINT_STATUSES:
            raise ValueError(
                f"非法蓝图状态: {status}，允许: {list(self.BLUEPRINT_STATUSES)}")
        cur = row["status"]
        if status != cur and status not in self.BLUEPRINT_TRANSITIONS[cur]:
            raise ValueError(
                f"蓝图状态不可从 {cur} 流转到 {status}（白名单: "
                f"{sorted(self.BLUEPRINT_TRANSITIONS[cur]) or '无——已终态'}）")
        with self._tx():
            self.conn.execute(
                "UPDATE blueprints SET status=?, updated_at=? WHERE id=?",
                (status, now(), bp_id))
        self.append_event(
            project_id, "blueprint.status_changed",
            {"blueprint_id": bp_id, "name": row["name"], "from": cur, "to": status},
            author=author,
        )
        return self.get_blueprint(project_id, bp_id)

    # ---------- 业务逻辑块（逆向第四页签：函数协作/业务语义，人机共写） ----------

    @staticmethod
    def _parse_addr(address: Any) -> int:
        """地址入参解析：int 或 "0x…" 串（前端以 hex 串为准——JS Number 无法安全
        表示 64 位地址）。"""
        if isinstance(address, bool) or address is None:
            raise ValueError(f"非法函数地址: {address!r}")
        try:
            if isinstance(address, int):
                return int(address)
            s = str(address).strip()
            return int(s, 16) if s.lower().startswith("0x") else int(s, 10)
        except ValueError:
            raise ValueError(f"非法函数地址: {address!r}") from None

    def _owned_logic_block(self, project_id: str, lb_id: str) -> sqlite3.Row | None:
        """业务块归属校验：跨项目访问等同不存在。"""
        return self.conn.execute(
            "SELECT * FROM logic_blocks WHERE id=? AND project_id=?", (lb_id, project_id)
        ).fetchone()

    def _logic_block_funcs(self, lb_id: str) -> list[dict]:
        """挂接函数出口：join func_kb 取当前函数名（可能已被改名），address 出口
        为 hex 串。"""
        rows = self.conn.execute(
            "SELECT lbf.address, lbf.role, lbf.seq, kb.id AS func_id, kb.name AS func_name"
            " FROM logic_block_funcs lbf"
            " JOIN logic_blocks lb ON lb.id=lbf.block_id"
            " LEFT JOIN func_kb kb ON kb.project_id=lb.project_id"
            "   AND kb.binary_sha256=lb.binary_sha256 AND kb.address=lbf.address"
            " WHERE lbf.block_id=? ORDER BY lbf.seq, lbf.rowid",
            (lb_id,),
        ).fetchall()
        return [
            {
                "address": hex(int(r["address"])),
                "func_id": r["func_id"] or "",
                "func_name": r["func_name"] or "",
                "role": r["role"] or "",
                "seq": int(r["seq"]),
            }
            for r in rows
        ]

    def create_logic_block(
        self, project_id: str, name: str, description: str = "",
        binary_sha256: str = "", seq: int = 0, author: str = "human",
    ) -> dict:
        name = str(name).strip()
        if not name:
            raise ValueError("业务块 name 不能为空")
        lb_id = new_id("lb")
        with self._tx():
            try:
                self.conn.execute(
                    "INSERT INTO logic_blocks(id,project_id,binary_sha256,name,description,"
                    "seq,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (lb_id, project_id, str(binary_sha256), name,
                     str(description), int(seq), now(), now()),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(
                    f"同项目同样本下业务块重名: {name}（换名或复用既有块）") from e
        self.append_event(
            project_id, "logic_block.created",
            {"logic_block_id": lb_id, "name": name, "binary_sha256": binary_sha256},
            author=author,
        )
        d = self.get_logic_block(project_id, lb_id)
        assert d is not None
        return d

    def get_logic_block(self, project_id: str, lb_id: str) -> dict | None:
        row = self._owned_logic_block(project_id, lb_id)
        if row is None:
            return None
        d = _row_to_dict(row) or {}
        d["funcs"] = self._logic_block_funcs(lb_id)
        return d

    def list_logic_blocks(
        self, project_id: str, binary_sha256: str | None = None
    ) -> list[dict]:
        """列表按 seq 排（同序按创建先后）；带 func_count 供列表徽标，不嵌 funcs。"""
        sql = "SELECT * FROM logic_blocks WHERE project_id=?"
        args: list[Any] = [project_id]
        if binary_sha256 is not None:
            sql += " AND binary_sha256=?"
            args.append(binary_sha256)
        sql += " ORDER BY seq, created_at"
        rows = self.conn.execute(sql, args).fetchall()
        out: list[dict] = []
        for r in rows:
            d = _row_to_dict(r) or {}
            d["func_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM logic_block_funcs WHERE block_id=?", (r["id"],)
            ).fetchone()[0]
            out.append(d)
        return out

    def update_logic_block(
        self, project_id: str, lb_id: str, *,
        name: Any = UNSET, description: Any = UNSET, seq: Any = UNSET,
        author: str = "human",
    ) -> dict | None:
        """分区更新（UNSET 哨兵：未传不覆盖）。"""
        row = self._owned_logic_block(project_id, lb_id)
        if row is None:
            return None
        sets, args, changed = [], [], {}
        if name is not UNSET:
            if not str(name).strip():
                raise ValueError("业务块 name 不能为空")
            sets.append("name=?")
            args.append(str(name).strip())
            changed["name"] = str(name).strip()
        if description is not UNSET:
            sets.append("description=?")
            args.append(str(description))
            changed["description"] = True
        if seq is not UNSET:
            sets.append("seq=?")
            args.append(int(seq))
            changed["seq"] = int(seq)
        if not sets:
            return self.get_logic_block(project_id, lb_id)
        sets.append("updated_at=?")
        args.append(now())
        args.append(lb_id)
        with self._tx():
            try:
                self.conn.execute(
                    f"UPDATE logic_blocks SET {', '.join(sets)} WHERE id=?", args)
            except sqlite3.IntegrityError as e:
                raise ValueError(
                    f"同项目同样本下业务块重名: {changed.get('name')}") from e
        self.append_event(
            project_id, "logic_block.updated",
            {"logic_block_id": lb_id, **changed}, author=author,
        )
        return self.get_logic_block(project_id, lb_id)

    def delete_logic_block(
        self, project_id: str, lb_id: str, author: str = "human"
    ) -> bool:
        row = self._owned_logic_block(project_id, lb_id)
        if row is None:
            return False
        with self._tx():
            # 连接未开 foreign_keys pragma，级联手动做
            self.conn.execute("DELETE FROM logic_block_funcs WHERE block_id=?", (lb_id,))
            self.conn.execute("DELETE FROM logic_blocks WHERE id=?", (lb_id,))
        self.append_event(
            project_id, "logic_block.deleted",
            {"logic_block_id": lb_id, "name": row["name"]}, author=author,
        )
        return True

    def add_logic_block_func(
        self, project_id: str, lb_id: str, address: Any, role: str = "",
        author: str = "human",
    ) -> dict | None:
        """挂接函数（块详情选择器 / 分析视图快捷钮双入口共用，Agent 走工具）。
        地址必须是 func_kb 中本块同样本的已知函数——防幻觉防跨项目（照
        add_chain_link 纪律）。项目级块（未绑样本）不挂函数。seq 自动追加。"""
        row = self._owned_logic_block(project_id, lb_id)
        if row is None:
            raise LookupError(f"业务块不存在: {lb_id}")
        if not row["binary_sha256"]:
            raise ValueError("项目级块（未绑样本）不挂函数——请建样本级业务块")
        addr = self._parse_addr(address)
        if self.lookup_func(project_id, row["binary_sha256"], addr) is None:
            raise ValueError(
                f"函数 0x{addr:x} 未登记在 func_kb（样本 "
                f"{str(row['binary_sha256'])[:12]}…）——先在分析视图入库再挂接")
        link_id = new_id("lbf")
        with self._tx():
            m = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM logic_block_funcs WHERE block_id=?",
                (lb_id,),
            ).fetchone()["m"]
            try:
                self.conn.execute(
                    "INSERT INTO logic_block_funcs(id,block_id,address,role,seq,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (link_id, lb_id, addr, str(role), int(m) + 1, now()),
                )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"函数 0x{addr:x} 已挂接在本块") from e
            self.conn.execute(
                "UPDATE logic_blocks SET updated_at=? WHERE id=?", (now(), lb_id))
        self.append_event(
            project_id, "logic_block.func_added",
            {"logic_block_id": lb_id, "address": hex(addr), "role": str(role)},
            author=author,
        )
        return self.get_logic_block(project_id, lb_id)

    def remove_logic_block_func(
        self, project_id: str, lb_id: str, address: Any, author: str = "human"
    ) -> dict | None:
        """摘除函数；删后剩余挂接按旧序重排 seq 1..n（照 delete_chain_link）。"""
        row = self._owned_logic_block(project_id, lb_id)
        if row is None:
            return None
        addr = self._parse_addr(address)
        gone = self.conn.execute(
            "SELECT id FROM logic_block_funcs WHERE block_id=? AND address=?",
            (lb_id, addr),
        ).fetchone()
        if gone is None:
            return None
        with self._tx():
            self.conn.execute(
                "DELETE FROM logic_block_funcs WHERE block_id=? AND address=?",
                (lb_id, addr))
            remaining = self.conn.execute(
                "SELECT id FROM logic_block_funcs WHERE block_id=? ORDER BY seq",
                (lb_id,),
            ).fetchall()
            for i, r in enumerate(remaining, start=1):
                self.conn.execute(
                    "UPDATE logic_block_funcs SET seq=? WHERE id=?", (i, r["id"]))
            self.conn.execute(
                "UPDATE logic_blocks SET updated_at=? WHERE id=?", (now(), lb_id))
        self.append_event(
            project_id, "logic_block.func_removed",
            {"logic_block_id": lb_id, "address": hex(addr)}, author=author,
        )
        return self.get_logic_block(project_id, lb_id)

    def update_logic_block_func(
        self, project_id: str, lb_id: str, address: Any, role: str,
        author: str = "human",
    ) -> dict | None:
        """改角色注（该函数在本块中的职责一句话）。挂接不存在返回 None。"""
        row = self._owned_logic_block(project_id, lb_id)
        if row is None:
            return None
        addr = self._parse_addr(address)
        cur = self.conn.execute(
            "SELECT id FROM logic_block_funcs WHERE block_id=? AND address=?",
            (lb_id, addr),
        ).fetchone()
        if cur is None:
            return None
        with self._tx():
            self.conn.execute(
                "UPDATE logic_block_funcs SET role=? WHERE id=?", (str(role), cur["id"]))
            self.conn.execute(
                "UPDATE logic_blocks SET updated_at=? WHERE id=?", (now(), lb_id))
        self.append_event(
            project_id, "logic_block.func_updated",
            {"logic_block_id": lb_id, "address": hex(addr), "role": str(role)},
            author=author,
        )
        return self.get_logic_block(project_id, lb_id)
