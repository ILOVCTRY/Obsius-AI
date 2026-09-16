"""黑板 SQLite schema（DESIGN.md §5.2 数据模型 v6 的 DDL 实现）。

设计要点：
- WAL 模式：多会话并发读、单写入口（core API）。
- findings / func_kb 用 UNIQUE 指纹去重，重复写入合并而非新增（§5.3）。
- chains 的节点必须引用既有 finding/func_kb/artifact，黑板层校验防幻觉（§5.2）。
- 所有实体带 author，形成审计链（§5.2 末）。
"""

import sqlite3

SCHEMA_VERSION = 8

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
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    target_asset_id TEXT REFERENCES assets(id),
    vuln_class      TEXT NOT NULL DEFAULT '',  -- 渗透类；逆向发现留 ''（类别在 evidence.category 五类）
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'info',  -- info / low / medium / high / critical
    status          TEXT NOT NULL DEFAULT 'unverified',  -- unverified / verified / false-positive
    evidence        TEXT NOT NULL DEFAULT '{}',  -- JSON：引用 event、请求响应、截图
    poc_artifact_id TEXT REFERENCES artifacts(id),
    confidence      REAL NOT NULL DEFAULT 0.5,
    dedup_key       TEXT NOT NULL,        -- 指纹：vuln_class/dedup_key；无键时=自身 id（永不合并）
    author          TEXT NOT NULL DEFAULT 'system',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
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
    result_note   TEXT NOT NULL DEFAULT '',
    context_refs  TEXT NOT NULL DEFAULT '[]',  -- v3 JSON：任务依据的 finding id（显式 refs ∪ 正文自动抽取）
    stale_refs    TEXT NOT NULL DEFAULT '[]',  -- v3 JSON：已被推翻待自评的依据（撤回传播挂标，收尾后留审计）
    plan          TEXT NOT NULL DEFAULT '[]',  -- v6 JSON：认领者计划步 [{id,title,status,note,ts}]（A2 先规划后动手）
    workset       TEXT NOT NULL DEFAULT '[]',  -- v7 JSON：正在分析的目标集（advisory 软声明，不阻塞任何人，机制 1.1）
    dedup_fp      TEXT NOT NULL DEFAULT '',    -- v7：发布去重指纹（project+type+归一化 scope+objective 哈希，机制 1.1）
    wait_for      TEXT NOT NULL DEFAULT '[]',  -- v7 JSON：被占资源键（open 行门控标记，claim_next 排除，机制 1.4）
    lease_cooldown_until TEXT,                 -- v7：死锁牺牲者冷却（到期前 claim_next 跳过，机制 1.4）
    blocked_reason TEXT NOT NULL DEFAULT 'error',  -- v8：fail 通道结构化原因 error|awaiting_human（C1）
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
      resource_leases 由 DDL 的 IF NOT EXISTS 直接建表。"""
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
