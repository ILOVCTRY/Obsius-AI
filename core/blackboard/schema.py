"""黑板 SQLite schema（DESIGN.md §5.2 数据模型 v6 的 DDL 实现）。

设计要点：
- WAL 模式：多会话并发读、单写入口（core API）。
- findings / func_kb 用 UNIQUE 指纹去重，重复写入合并而非新增（§5.3）。
- chains 的节点必须引用既有 finding/func_kb/artifact，黑板层校验防幻觉（§5.2）。
- 所有实体带 author，形成审计链（§5.2 末）。
"""

import sqlite3

SCHEMA_VERSION = 18

# v17→v18（向指定会话直接发任务，DESIGN §6.4 定稿块 2026-09-20）：tasks 幂等补
# target_session（''=公共池；非空=仅该窗可认领——claim 硬门控、claim_next WHERE
# 过滤；发布即武装目标窗并单窗 kick，关窗退回公共池，reopen 清指派）。

# v16→v17（R4 逆向开发管线，DESIGN §9 R4 定稿块）：新表 blueprints 由 DDL 的
# IF NOT EXISTS 直接建表（无 ALTER，幂等）——逆向重建的开发蓝图：
# modules 为 JSON 数组 [{name, desc, func_addresses[], spec, notes,
# status(pending/analyzed/specd/tested)}]，status 流转 draft→reviewed→ready→
# building→built（ready 仅人类/审批可置，Agent 工具不暴露 status 入口）。
# 唯一写入口 Blackboard.create_blueprint 等方法。

# v14→v15（F6 浏览器能力，DESIGN §7）：新表 http_history 由 DDL 的 IF NOT EXISTS
# 直接建表（无 ALTER，幂等）——浏览器抓包/重发/爆破的请求响应历史，唯一写入口
# Blackboard.add_http_history；source 区分来源（browser/replay/intruder），
# batch_id 分组（爆破批次/重发单发），body 超 64KB 截断（body_truncated）、
# 二进制 base64（is_binary）。

# v13→v14（任务绑定角色：认领即换装，DESIGN §6.4 定稿块）：tasks 幂等补 role
# （建议认领角色 id；''=不限。会话窗保留底色角色，认领带 role 任务时按任务角色
# 换装执行——prompt/工具边界/噪声/运行时上限，跑完恢复底色）。

# v11→v12（发现分两类）：findings 幂等补 category（vuln=漏洞 / intel=有效发现·关键发现；
# 缺省 'vuln'，存量行不动——历史数据以 vuln_class/severity 语义自辨）。

# v10→v11（F11 评级落地）：findings 幂等补 rating_basis（判级依据，如
# 「rating:edu-rating 高危#2 任意文件覆盖写」；合并就高时随 severity 覆盖）。

# v9→v10（工作区隔离 W3）：artifacts 幂等补 meta（JSON 归属元数据
# {task_id?, session_id?}，bb_add_artifact 自动挂认领任务/会话；清单按 meta 过滤）。

# v8→v9（C10 任务上下文归任务所有）：tasks 幂等补 context（JSON：任务执行履历
# {transcript, attempts[]}，唯一写点 TaskQueue._finish；完整对话现场在
# <workspace>/<pid>/snapshots/task-<tid>.json，agent 层每步落盘）。

# v7→v8（C1 任务暂停语义统一，§6.1）：tasks 幂等补 blocked_reason（error|awaiting_human，缺省 error 向后兼容）。

# v6→v7（机制 1.1/1.4 协调底座，DESIGN §6.7 机制 1.1/1.4）：
# tasks 幂等补 workset/dedup_fp/wait_for/lease_cooldown_until 4 列；
# 新表 resource_leases（资源租约，键方案白名单见 core/blackboard/leases.py）。
# 运行期动态锁申请后置（发布期/认领期门控为主）。

# v4→v5 给 orchestrator_state 补的编排状态列（批 3，DESIGN §6.8/机制 1.9）：
# (列名, ALTER 声明)；DDL 新库直接全列，旧库 _migrate 幂等 ALTER。
V5_ORCH_STATE_COLUMNS: list[tuple[str, str]] = [
    ("event_cursor", "INTEGER NOT NULL DEFAULT 0"),       # 已消费事件游标
    ("cycles", "INTEGER NOT NULL DEFAULT 0"),             # 累计 tick 轮数
    ("last_digest_cycle", "INTEGER NOT NULL DEFAULT -999"),
    ("chain_active", "INTEGER NOT NULL DEFAULT 0"),       # 以下 4 列批 5 自动链消费，先存
    ("chain_ticks", "INTEGER NOT NULL DEFAULT 0"),
    ("last_auto_tick_at", "TEXT NOT NULL DEFAULT ''"),
    ("auto_ticks_total", "INTEGER NOT NULL DEFAULT 0"),
    ("tick_owner", "TEXT NOT NULL DEFAULT ''"),           # tick 租约（900s TTL + 心跳）
    ("tick_lease_until", "TEXT NOT NULL DEFAULT ''"),
]

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    domain       TEXT NOT NULL,           -- 旧列（v1 平铺领域包）；新行写 track 值兜底
    track        TEXT NOT NULL DEFAULT '',-- v2 场景轨：ctf / assessment / research / malware
    capabilities TEXT NOT NULL DEFAULT '[]', -- v2 能力包多选 JSON：["web","binary",...]
    config       TEXT NOT NULL DEFAULT '{}',-- JSON：授权边界、cross_target、噪声预算等
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name       TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT '_generalist',
    status     TEXT NOT NULL DEFAULT 'idle',  -- idle / running / blocked / stopped
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- 多态资产树：domain → host → service → url，或 binary 等
CREATE TABLE IF NOT EXISTS assets (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    type       TEXT NOT NULL,           -- domain / host / service / url / binary / ...
    value      TEXT NOT NULL,
    parent_id  TEXT REFERENCES assets(id),
    status     TEXT NOT NULL DEFAULT 'open',
    meta       TEXT NOT NULL DEFAULT '{}',
    author     TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1,  -- H2 乐观锁：每次写 +1，写前可比对 expected_revision
    UNIQUE(project_id, type, value, parent_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    path        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'file',  -- file / poc / capture / decompile / ...
    description TEXT NOT NULL DEFAULT '',
    sha256      TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT 'system',
    meta        TEXT NOT NULL DEFAULT '{}',   -- 归属元数据（W3）：{task_id?, session_id?}
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    target_asset_id TEXT REFERENCES assets(id),
    vuln_class      TEXT NOT NULL DEFAULT '',  -- 渗透类；逆向发现留 ''（类别在 evidence.category 五类）
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'info',  -- info / low / medium / high / critical
    rating_basis    TEXT NOT NULL DEFAULT '',  -- 判级依据（F11）：规则名+条款+一句话依据
    status          TEXT NOT NULL DEFAULT 'unverified',  -- unverified / verified / false-positive
    category        TEXT NOT NULL DEFAULT 'vuln',  -- C6 分两类：vuln=漏洞 / intel=有效发现·关键发现
    evidence        TEXT NOT NULL DEFAULT '{}',  -- JSON：引用 event、请求响应、截图
    poc_artifact_id TEXT REFERENCES artifacts(id),
    confidence      REAL NOT NULL DEFAULT 0.5,
    dedup_key       TEXT NOT NULL,        -- 指纹：vuln_class/dedup_key；无键时=自身 id（永不合并）
    author          TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,  -- H2 乐观锁：每次写 +1，写前可比对 expected_revision
    UNIQUE(project_id, target_asset_id, dedup_key)
);

CREATE TABLE IF NOT EXISTS func_kb (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id),
    binary_sha256 TEXT NOT NULL,
    address       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    name_history  TEXT NOT NULL DEFAULT '[]',  -- JSON：重命名演变史（也是分析笔记）
    analysis      TEXT NOT NULL DEFAULT '',
    risk_tags     TEXT NOT NULL DEFAULT '[]',  -- JSON：stack-overflow / no-length-check ...
    confidence    REAL NOT NULL DEFAULT 0.5,
    analyzed_by   TEXT NOT NULL DEFAULT 'system',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(project_id, binary_sha256, address)
);

CREATE TABLE IF NOT EXISTS chains (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name       TEXT NOT NULL,
    goal       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'hypothesis',  -- hypothesis / validated / exploited
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_links (
    id         TEXT PRIMARY KEY,
    chain_id   TEXT NOT NULL REFERENCES chains(id),
    seq        INTEGER NOT NULL,
    node_type  TEXT NOT NULL,            -- finding / func_kb / artifact
    node_id    TEXT NOT NULL,
    edge_note  TEXT NOT NULL DEFAULT '', -- 这条边为什么成立（如"溢出可覆盖返回地址"）
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id),
    scope         TEXT NOT NULL DEFAULT '',          -- 如 ip:1.2.3.4 / domain:x.com / binary:sha
    task_type     TEXT NOT NULL DEFAULT 'generic',   -- recon / exploit / analyze / privesc ...
    objective     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',      -- open / claimed / done / failed（不要的任务物理删除，见 tasks.py delete；历史库可能残留 cancelled 行）
    priority      INTEGER NOT NULL DEFAULT 2,        -- 0(P0) 最高
    noise_budget  TEXT NOT NULL DEFAULT 'passive',   -- passive / low / medium / high
    conflict_keys TEXT NOT NULL DEFAULT '[]',        -- JSON：active 任务的互斥键（如 ip 值）
    claimed_by    TEXT REFERENCES sessions(id),
    lease_until   TEXT,
    parent_id     TEXT REFERENCES tasks(id),
    created_by    TEXT NOT NULL DEFAULT 'human',     -- human / orchestrator / session-x
    role          TEXT NOT NULL DEFAULT '',          -- v14：建议认领角色 id（''=不限；认领即换装）
    target_session TEXT NOT NULL DEFAULT '',         -- v18：指派会话（''=公共池；非空=仅该窗可认领）
    result_note   TEXT NOT NULL DEFAULT '',
    context_refs  TEXT NOT NULL DEFAULT '[]',  -- v3 JSON：任务依据的 finding id（显式 refs ∪ 正文自动抽取）
    stale_refs    TEXT NOT NULL DEFAULT '[]',  -- v3 JSON：已被推翻待自评的依据（撤回传播挂标，收尾后留审计）
    plan          TEXT NOT NULL DEFAULT '[]',  -- v6 JSON：认领者计划步 [{id,title,status,note,ts}]（A2 先规划后动手）
    workset       TEXT NOT NULL DEFAULT '[]',  -- v7 JSON：正在分析的目标集（advisory 软声明，不阻塞任何人，机制 1.1）
    dedup_fp      TEXT NOT NULL DEFAULT '',    -- v7：发布去重指纹（project+type+归一化 scope+objective 哈希，机制 1.1）
    wait_for      TEXT NOT NULL DEFAULT '[]',  -- v7 JSON：被占资源键（open 行门控标记，claim_next 排除，机制 1.4）
    lease_cooldown_until TEXT,                 -- v7：死锁牺牲者冷却（到期前 claim_next 跳过，机制 1.4）
    blocked_reason TEXT NOT NULL DEFAULT 'error',  -- v8：fail 通道结构化原因 error|awaiting_human（C1）
    context       TEXT NOT NULL DEFAULT '{}',      -- v9 JSON：任务执行履历 {transcript, attempts[]}（C10 跨会话接手，唯一写点 _finish）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- v7 资源租约（机制 1.4，DESIGN §6.7.1）：认领任务时由 conflict_keys 转写授予；
-- 键方案白名单归一化在 leases.py；有效性 = JOIN tasks（claimed 且租约未过期）判定，
-- 免心跳双写；任务收尾/删除/租约过期时释放行并重校验 wait_for 等待者。
CREATE TABLE IF NOT EXISTS resource_leases (
    project_id   TEXT NOT NULL REFERENCES projects(id),
    resource_key TEXT NOT NULL,             -- 归一化键：ip:/host:/domain:/url:/binary:/func:/tool:/user:
    mode         TEXT NOT NULL,             -- X 独占 / S 共享（passive→S、active→X）
    task_id      TEXT NOT NULL,
    session_id   TEXT,
    granted_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, resource_key, task_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_leases_key ON resource_leases(project_id, resource_key);

-- v3 会话收件箱（DESIGN.md §6.7 的 1.5/1.6）：知会类私信，与审批收件箱严格分设。
-- 只有系统写，没有 Agent 自由消息工具；kind：basis_stale（撤回强制自评）/
-- finding_update（v6 A4：实质增补，信息式）。
CREATE TABLE IF NOT EXISTS session_inbox (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    to_session  TEXT NOT NULL,               -- 不设外键：会话关窗后私信仍留审计
    kind        TEXT NOT NULL,               -- basis_stale / finding_update
    ref_id      TEXT NOT NULL,               -- 被推翻的 finding id
    payload     TEXT NOT NULL DEFAULT '{}',  -- JSON：标题/推翻人/备注
    created_at  TEXT NOT NULL,
    read_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_inbox_unread
    ON session_inbox(to_session, read_at);
-- 未读去重：同一收件人对同一 ref 的未读私信只此一条；读后再次撤回可重新投递
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_inbox_open_uniq
    ON session_inbox(to_session, ref_id, kind) WHERE read_at IS NULL;

-- 编排器状态/用量计数（v4 批 2：token/任务计数；v5 批 3 补游标/轮数/tick 租约等，
-- 见 DESIGN §6.8 与机制 1.9）。每项目一行，首次记账或拿租约时惰性创建。
CREATE TABLE IF NOT EXISTS orchestrator_state (
    project_id            TEXT PRIMARY KEY REFERENCES projects(id),
    tokens_in             INTEGER NOT NULL DEFAULT 0,
    tokens_out            INTEGER NOT NULL DEFAULT 0,
    tokens_cache_read     INTEGER NOT NULL DEFAULT 0,
    tokens_cache_creation INTEGER NOT NULL DEFAULT 0,
    llm_calls             INTEGER NOT NULL DEFAULT 0,
    tasks_published       INTEGER NOT NULL DEFAULT 0,  -- 只计 orchestrator 自主发布
    budget_warned         INTEGER NOT NULL DEFAULT 0,  -- token 80% 软警告去重（回落复位）
    event_cursor          INTEGER NOT NULL DEFAULT 0,  -- v5：已消费事件 id
    cycles                INTEGER NOT NULL DEFAULT 0,  -- v5：累计 tick 轮数
    last_digest_cycle     INTEGER NOT NULL DEFAULT -999,
    chain_active          INTEGER NOT NULL DEFAULT 0,  -- v5：批 5 自动链状态先存后用
    chain_ticks           INTEGER NOT NULL DEFAULT 0,
    last_auto_tick_at     TEXT NOT NULL DEFAULT '',
    auto_ticks_total      INTEGER NOT NULL DEFAULT 0,
    tick_owner            TEXT NOT NULL DEFAULT '',    -- v5：tick 租约所有者（''=空闲）
    tick_lease_until      TEXT NOT NULL DEFAULT '',
    last_replan_at        TEXT NOT NULL DEFAULT '',    -- v6：L2 自动重排节流时间戳（A5）
    last_derive_at        TEXT NOT NULL DEFAULT '',    -- v13：mission 自动派生上次判定时间（2026-09-18）
    last_derive_result    TEXT NOT NULL DEFAULT '',    -- v13：上次判定结果 published:n / empty / error:…
    updated_at            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    session_id TEXT,
    kind       TEXT NOT NULL,            -- command / decision / finding.new / task.claimed / audit.deny ...
    payload    TEXT NOT NULL DEFAULT '{}',
    author     TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);

CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    session_id  TEXT,
    action      TEXT NOT NULL,           -- JSON：请求的具体动作
    risk        TEXT NOT NULL DEFAULT 'medium',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    requested_by TEXT NOT NULL,
    decided_by  TEXT,                    -- human / orchestrator
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

-- v15 浏览器抓包/重发/爆破历史（F6，DESIGN §7）：所有明文 HTTP 交互统一入此表；
-- 写入口只有 Blackboard.add_http_history（黑板唯一写入口红线）。
CREATE TABLE IF NOT EXISTS http_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL,
    session_id     TEXT,                       -- 来源会话（AI 会话 id / human-<uuid>）
    task_id        TEXT,                       -- AI 动作挂认领任务；人类=NULL
    source         TEXT NOT NULL DEFAULT 'browser',  -- browser / replay / intruder
    batch_id       TEXT NOT NULL DEFAULT '',   -- 爆破批次分组；重发填自身批 id
    meta           TEXT NOT NULL DEFAULT '{}', -- JSON：payload/改写说明等附加信息
    method         TEXT NOT NULL,
    url            TEXT NOT NULL,
    status         INTEGER,
    req_headers    TEXT NOT NULL DEFAULT '{}', -- JSON
    req_body       TEXT,
    resp_headers   TEXT NOT NULL DEFAULT '{}', -- JSON
    resp_body      TEXT,                       -- 文本原样 / 二进制 base64(is_binary) / 超 64KB 截断
    resp_mime      TEXT NOT NULL DEFAULT '',
    body_truncated INTEGER NOT NULL DEFAULT 0,
    is_binary      INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_http_history_project ON http_history(project_id, id);
CREATE INDEX IF NOT EXISTS idx_http_history_batch ON http_history(project_id, batch_id);

-- v17 逆向开发蓝图（R4，DESIGN §9 R4 定稿块）：从分析产物重建程序的中枢——
-- 模块划分任务产骨架、模块深析写回 spec/notes、蓝图汇总产 content_md，
-- ready 后派生 reconstruct 重建子任务。一个样本一个项目可有多份蓝图（重划分）。
CREATE TABLE IF NOT EXISTS blueprints (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    binary_sha256  TEXT NOT NULL DEFAULT '',   -- 目标样本 sha256（''=非单样本蓝图）
    name           TEXT NOT NULL,
    goal           TEXT NOT NULL DEFAULT '',   -- 重建目标（要造一个什么样的程序）
    status         TEXT NOT NULL DEFAULT 'draft',  -- draft/reviewed/ready/building/built
    content_md     TEXT NOT NULL DEFAULT '',   -- 蓝图正文：数据流/接口表/算法/协议/状态机
    modules        TEXT NOT NULL DEFAULT '[]', -- JSON 数组：[{name,desc,func_addresses[],spec,notes,status}]
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(project_id, binary_sha256, name)
);
CREATE INDEX IF NOT EXISTS idx_blueprints_project ON blueprints(project_id, id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移（旧 demo 库仍可打开）：
    - v1→v2：projects 补 track/capabilities 列；
    - v2→v3：tasks 补 context_refs/stale_refs 列（session_inbox 由 DDL 的
      IF NOT EXISTS 直接建表）；
    - v3→v4：orchestrator_state 由 DDL 的 IF NOT EXISTS 直接建表（批 2 用量计数）；
    - v4→v5：旧库的 orchestrator_state 幂等 ALTER 补编排状态/租约 9 列（批 3）；
    - v5→v6：tasks 幂等补 plan 列（A2），orchestrator_state 补 last_replan_at（A5）。
    - v6→v7：tasks 幂等补 workset/dedup_fp/wait_for/lease_cooldown_until（机制 1.1/机制 1.4），
      resource_leases 由 DDL 的 IF NOT EXISTS 直接建表。
    - v7→v8：tasks 幂等补 blocked_reason（C1）。
    - v8→v9：tasks 幂等补 context（C10 任务执行履历）。
    - v9→v10：artifacts 幂等补 meta（W3 产物归属 {task_id?, session_id?}）。
    - v10→v11：findings 幂等补 rating_basis（F11 判级依据，空串=未标注）。
    - v14→v15：http_history 由 DDL 的 IF NOT EXISTS 直接建表（F6 浏览器抓包/重发/爆破），
      无 ALTER，旧库打开即建。
    - v15→v16：assets/findings 幂等补 revision（H2 乐观锁：多窗并发改同一行时
      写前比对 expected_revision，防丢失更新；每次写 +1）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    if "track" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN track TEXT NOT NULL DEFAULT ''")
    if "capabilities" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN capabilities TEXT NOT NULL DEFAULT '[]'")
    task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "context_refs" not in task_cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN context_refs TEXT NOT NULL DEFAULT '[]'")
    if "stale_refs" not in task_cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN stale_refs TEXT NOT NULL DEFAULT '[]'")
    if "plan" not in task_cols:  # v6（A2 先规划后动手）
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN plan TEXT NOT NULL DEFAULT '[]'")
    for col in ("workset", "dedup_fp", "wait_for"):  # v7（机制 1.1/1.4 协调底座）
        if col not in task_cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'")
    if "lease_cooldown_until" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN lease_cooldown_until TEXT")
    if "blocked_reason" not in task_cols:  # v8（C1 暂停语义统一）
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT 'error'")
    if "context" not in task_cols:  # v9（C10 任务执行履历：{transcript, attempts[]}）
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
    if "role" not in task_cols:  # v14（任务绑定角色：认领即换装）
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN role TEXT NOT NULL DEFAULT ''")
    if "target_session" not in task_cols:  # v18（指派任务：认领门控）
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN target_session TEXT NOT NULL DEFAULT ''")
    art_cols = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
    if art_cols and "meta" not in art_cols:  # v10（W3 产物归属元数据）
        conn.execute("ALTER TABLE artifacts ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
    fin_cols = {r[1] for r in conn.execute("PRAGMA table_info(findings)")}
    if fin_cols and "rating_basis" not in fin_cols:  # v11（F11 判级依据）
        conn.execute(
            "ALTER TABLE findings ADD COLUMN rating_basis TEXT NOT NULL DEFAULT ''")
    if fin_cols and "category" not in fin_cols:  # v12（发现分两类：vuln/intel）
        conn.execute(
            "ALTER TABLE findings ADD COLUMN category TEXT NOT NULL DEFAULT 'vuln'")
    for tbl in ("assets", "findings"):  # v16（H2 乐观锁 revision 列）
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
        if cols and "revision" not in cols:
            conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    os_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orchestrator_state'")}
    if os_tables:
        os_cols = {r[1] for r in conn.execute("PRAGMA table_info(orchestrator_state)")}
        for name, decl in V5_ORCH_STATE_COLUMNS:
            if name not in os_cols:
                conn.execute(
                    f"ALTER TABLE orchestrator_state ADD COLUMN {name} {decl}")
        if "last_replan_at" not in os_cols:  # v6（A5 自动重排节流）
            conn.execute(
                "ALTER TABLE orchestrator_state ADD COLUMN"
                " last_replan_at TEXT NOT NULL DEFAULT ''")
        for col in ("last_derive_at", "last_derive_result"):  # v13（mission 派生结果）
            if col not in os_cols:
                conn.execute(
                    f"ALTER TABLE orchestrator_state ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    _migrate(conn)
    # 版本号随迁移 upsert（旧库 meta 里可能留着 1/2/3/4）
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
