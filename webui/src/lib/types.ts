// core API 数据契约（与 core/api/app.py 响应对齐，宽松可选字段）

export interface ProjectMeta {
  id: string
  name: string
  /** 场景轨（单选）；旧项目 domain 由后端读取时透明映射 */
  track: string
  /** 知识可见能力包（多选）：有专家绑定时=caps_effective 推导面，未绑定=盘上绑定 */
  capabilities: string[]
  /** 盘上原始绑定能力包（设置页跟随项目缺省用；2026-09-24） */
  capabilities_bound?: string[]
  /** 专家组队（expert-pool M2）：空/缺省=存量直通（按轨全池），非空=绑定清单 */
  experts?: string[]
  created_at: string
  config?: Record<string, unknown>
  /** 宿主运行时实测（GET /api/projects/{pid} 返回；M3 runtime chip 可用性门） */
  capability?: HostCapability
}

/** 宿主能力清单（HostDetector.to_dict 形状） */
export interface HostCapability {
  docker: { available: boolean; detail: string }
  wsl: { available: boolean; detail: string }
  tools: { name: string; available: boolean; detail: string }[]
}

/** 项目自主配置（DESIGN §6.8，批 2；存于 project.config.autonomy，双写 project.json+黑板） */
export interface Autonomy {
  level: "L0" | "L1" | "L2"
  paused: boolean
  sessions_cap: number
  max_chain_ticks: number
  token_budget: number | null
  task_budget: number | null
  auto_derive?: boolean         // C2 自动派生开关（任务空时 L1 判跳续派；缺省关）
}

/** D10 策略顾问项目级配置（2026-09-24，存 project.config.advisor） */
export interface AdvisorConfig {
  stuck_after: number
  stuck_max_extensions: number
  closing_max_rounds: number
  provider?: string
  model?: string
}

/** L2 自动链视图（批 5，§6.8）；estranged=DB 活但本进程无标记（重启急停） */
export interface ChainState {
  active: boolean
  ticks: number
  auto_ticks_total: number
  estranged: boolean
}

/** GET /api/projects/{pid} 的 usage：autonomy 全字段 + 实时计数 */
export interface ProjectUsage extends Autonomy {
  active_sessions: number
  criteria_template?: string    // C2 所选判据模板名
  mission?: { text: string; criteria: string }
  redteam_roe?: { targets: string; window: string; exclusions: string; approver: string }
  /** R2（mode 退役→轨级）：redteam 轨 ROE 四要素是否核验齐全（false=行为按 pentest 上限兜底） */
  roe_complete?: boolean | null
  /** C2（2026-09-18）：mission 自动派生上次判定（last_result=published:n / empty / error:…） */
  derive?: { last_at: string; last_result: string }
  llm_calls: number
  tokens: { used: number; budget: number | null; pct: number | null
    /** M1 prompt caching 观测（2026-09-23）：缓存读/写 token 与命中率（分母 0 = 尚未打标） */
    cache_read?: number; cache_creation?: number; cache_hit?: number | null }
  tasks: { published: number; budget: number | null; pct: number | null }
  chain?: ChainState
  /** 会话 UI 风格（trae 视图 2026-09-28）：config.ui_style 透出；缺省 claude */
  ui_style?: "claude" | "trae"
  /** 编排 tick 运行中（trae 视图「正在规划下一步」状态行判定源） */
  orch_running?: boolean
}

export interface ProjectDetail extends ProjectMeta {
  task_stats: Record<string, number>
  findings: number
  assets: number
  usage: ProjectUsage
  capability: HostCapability
}

export interface BBEvent {
  id: number
  project_id: string
  session_id: string | null
  kind: string
  payload: Record<string, unknown>
  author: string
  created_at: string
}

export type TaskPlanStatus = "todo" | "doing" | "done" | "blocked"

export interface TaskPlanStep {
  id: string
  title: string
  status: TaskPlanStatus
  note?: string
  ts: string
}

export interface Task {
  id: string
  project_id: string
  scope: string
  task_type: string
  role?: string   // v14：建议认领角色（''/缺省=不限；认领即换装）
  target_session?: string   // v18：指派会话 → v0.71 起恒为专属执行窗（''/缺省=未绑窗）
  preferred_runtime?: string  // v23：任务默认运行时（''/缺省=未设；host/wsl/docker/sandbox）
  objective: string
  status: "open" | "claimed" | "done" | "failed"
  priority: number
  noise_budget: string
  conflict_keys: string[]
  parent_id: string | null
  created_by: string
  claimed_by: string | null
  result_note: string | null
  context_refs?: string[]
  stale_refs?: string[]
  plan: TaskPlanStep[]
  created_at: string
  updated_at: string
  resumable?: boolean  // C6：failed 恒 true（任何失败卡都可续跑）
  resume_mode?: "snapshot" | "transcript"  // C6：snapshot=⚡带现场续跑 / transcript=↩接手现场续跑
  workset?: string[]   // B1 工作集软声明（advisory，供避让不阻塞）
  wait_for?: string[]  // B2 被占资源键（open 行门控标记，claim_next 排除）
  blocked_reason?: "error" | "awaiting_human" | "aborted" | "cancelled"  // C1：fail 通道结构化原因（v8；aborted=人工中断 E12，cancelled=编排器/人类取消 M4）
  context?: TaskContext | null                  // C10 任务执行履历（v9，唯一写点 _finish）
}

// C10：任务执行履历——完整对话现场在 workspace 文件（task-<tid>.json），此处只存履历
export interface TaskAttempt {
  session_id: string
  session_name: string | null
  role: string | null
  outcome: "done" | "failed"
  result_note: string
  blocked_reason: "error" | "awaiting_human" | "aborted" | "cancelled" | null
  ended_at: string
}

export interface TaskContext {
  transcript: string | null
  attempts: TaskAttempt[]
  reconcile?: { id: number; text: string; state: "pending" | "met" | "failed" | "blocked"; note: string; verify?: Record<string, unknown>; by?: string }[]
  attachments?: AttachmentInfo[]  // 附件随发（2026-09-19）：publish 落库的附件清单（认领首条消息渲染 📎）
}

// 任务尝试树 v2（task-attempt-tree，2026-09-27 意图驱动改版）：
// 根=任务 → 意图（一句可证伪假设）→ 检验结果（发现/死路）→ 新发现下再长新意图。
// 后端现算零写入 GET /tree/{task_id}；nodes 平铺带 parent（""=挂根），前端组树。
// v1 的计划步主干+命令/工具动作叶已整体退役（用户拍板：树里不看命令）。
export type TaskTreeNode =
  | {
      kind: "intent"
      id: string
      parent: string
      statement: string
      status: "open" | "closed"
      outcome_type: string   // vuln / finding / dead_end（closed 时非空）
      dead_reason: string
      created_at: string
      closed_at: string | null
    }
  | {
      kind: "finding"
      id: string
      parent: string         // 挂的意图 id；""=游离（进兜底桶，仅历史数据）
      title: string
      severity: string
      status: string
      vuln_class: string
      created_at: string
    }
  | { kind: "bucket"; id: string; parent: string; title: string }  // "_orphan" 游离发现兜底桶

export interface TaskTree {
  task: {
    id: string
    objective: string
    task_type: string
    status: Task["status"]
    priority: number
    claimed_by: string | null
    result_note: string
  }
  nodes: TaskTreeNode[]
  current: { intent_id: string | null; last_activity_ts: string | null }
  truncated_findings: boolean
}

// 单站攻击链路图 v3（website-attack-path-graph，2026-09-24）：
// 目标 → 意图（规划产物）→ 执行（展开层）→ 收尾：漏洞 | 发现 | 死路
export type AttackResult = "blocked" | "no_reaction" | "hint" | "found" | "skipped"

export interface AttackAttempt {
  id: string
  key: string                              // METHOD 路径模板?query键
  method: string
  path_template: string
  query_keys: string[]
  started_at: string | null
  ended_at: string | null
  request_count: number
  result: AttackResult
  status_codes: number[]
  finding_ids: string[]
  intent_id: string | null                 // 读时按意图存活窗归属（无主=null）
  session_ids: string[]
  sources: string[]
  representative: {
    method?: string; url?: string; status?: number | null
    resp_mime?: string; snippet?: string; history_id?: number
    title?: string; note?: string
  }
}

export type AttackNodeType = "target" | "subtarget" | "intent" | "finding"
export type AttackEdgeKind = "outcome" | "derive" | "bypass" | "exec"
export type IntentOutcome = "" | "vuln" | "finding" | "dead_end"

export interface AttackNode {
  id: string
  type: AttackNodeType
  // target / subtarget
  label?: string; asset_type?: string
  settled?: boolean                       // subtarget：子树意图全部收尾且至少一条 dead_end
  findings?: number                        // subtarget：子树非误报发现数
  // intent
  statement?: string
  // 意图 open/closed；子目标复用资产状态；finding 节点复用 finding 状态
  status?: "open" | "closed" | "unverified" | "verified" | "false-positive"
    | "visited" | "scanning" | "tested_clean" | "budget_stop" | "na"
  outcome?: IntentOutcome
  finding_ids?: string[]
  dead_reason?: string
  created_at?: string | null; closed_at?: string | null
  request_count?: number
  default_hidden?: boolean                 // closed dead_end：默认隐藏
  // finding
  title?: string; severity?: string
  category?: string                        // vuln=漏洞 / intel=有效发现
}

export interface AttackEdge {
  source: string
  target: string
  kind: AttackEdgeKind
}

export interface AttackPath {
  target: { id: string; type: string; value: string }
  nodes: AttackNode[]
  edges: AttackEdge[]
  attempts: AttackAttempt[]
  exec_edges: AttackEdge[]
  counts: {
    subtargets?: number   // 根的直接子资产数（2026-10-01 子目标层）
    intents: number; open: number; closed: number; dead_end: number
    findings: number; total_requests: number
  }
}

// 意图行（GET /api/projects/{pid}/intents，人类收尾核对）
export interface IntentInfo {
  id: string; project_id: string
  statement: string
  target_asset_id: string | null
  basis_refs: string[]
  status: "open" | "closed"
  outcome_type: IntentOutcome
  outcome_refs: string[]
  dead_reason: string
  evidence_refs: string[]
  author: string
  created_at: string; closed_at: string | null; updated_at: string
  revision: number
}

// 执行轨迹（execution-trace-chain M1，2026-09-22）：任务详情内嵌时间链（R1+R2 现算）
export type TraceStepKind = "skill" | "kb" | "tools" | "finding"

export interface TraceStep {
  kind: TraceStepKind
  ts: string
  ts_end?: string                          // tools 组专属
  // skill
  name?: string | null; hit?: boolean; score?: number | null; matched?: string[]
  // kb
  module?: string; source?: string | null; count?: number
  // tools 组
  ok?: number; fail?: number; cmds?: string[]
  // finding（服务端已富化）
  finding_id?: string; title?: string; severity?: string; status?: string
  vuln_class?: string; category?: string
}

export interface TaskTraceWindow {
  task_id: string; session_id: string
  lo: number; hi: number | null; open: boolean
}

export interface TaskTrace {
  task: {
    id: string; objective: string; task_type: string; status: string
    priority: number; claimed_by: string | null; result_note: string
  }
  windows: TaskTraceWindow[]
  steps: TraceStep[]
  idle: TraceStep[]                        // 游离段（未挂任务活动）
  truncated: boolean
}

// 打法效果榜（M3/R4，基于物化侧轨迹链）
export interface TraceEffectCombo {
  skill: string; kb: string; chains: number; verified_findings: number
}

export interface TraceEffect {
  trace_chains: number; validated_chains: number; combos: TraceEffectCombo[]
}

export type LlmProviderFormat = "openai-chat-completions" | "openai-responses" | "anthropic-messages"

export interface LlmProvider {
  name: string
  base_url: string
  format: LlmProviderFormat
  api_key?: string          // 仅写入；读出脱敏（只有 has_key）
  has_key?: boolean
  models: string[]
  enabled: boolean
  /** 每模型最大上下文（token，可选；空=用系统默认预算） */
  model_context?: Record<string, number>
  /** 思考链开关：true=显式开启；null/缺省=不写该字段，跟随网关缺省（ark 默认开） */
  thinking?: boolean | null
  /** 供应商专属 HTTP/HTTPS 代理；留空沿用系统代理环境 */
  proxy?: string | null
}

export interface ModelInfo {
  models: string[]
  providers: LlmProvider[]
  default: { provider: string; model: string } | null
}

/** 项目 executor 模型三态（TRAE 新壳 M3 模型 chip 数据源） */
export interface ExecutorLlmView {
  /** 路由/供应商缺省 */
  default: { provider: string; model: string } | null
  /** 项目覆写（null=跟随缺省） */
  override: { provider: string; model: string } | null
  /** 当前实际生效 */
  effective: { provider: string; model: string } | null
  touched_sessions?: string[]
}

export interface DiscoveredModel {
  id: string
  status?: string | null
}

export interface DiscoverResult {
  listed: boolean
  models: DiscoveredModel[]
  probed?: string[]
}

export type FindingCategory = "vuln" | "intel"

export interface Finding {
  id: string
  project_id: string
  vuln_class: string
  title: string
  severity: string
  /** F11 判级依据（如「rating:edu-rating 高危#2 任意文件覆盖写」），空=未标注 */
  rating_basis: string
  /** 收录格式三件套·危害描述（schema v20），空=待补充 */
  impact: string
  /** 收录格式三件套·修复建议（schema v20），空=待补充 */
  remediation: string
  summary: string
  affected_assets: string
  test_environment: string
  reproduction_steps: string
  verification_result: string
  risk_assessment: string
  pocs: FindingPoc[]
  status: string
  /** C6 分两类：vuln=漏洞 / intel=有效发现·关键发现（缺省迁移行=vuln） */
  category: FindingCategory
  evidence: Record<string, unknown>
  target_asset_id: string | null
  poc_artifact_id: string | null
  author: string
  created_at: string
}

export interface Asset {
  id: string
  project_id: string
  type: string
  value: string
  parent_id: string | null
  status: string   // E7：持久化显式状态
  /** E7：资产树读时派生状态；父节点所有子节点收敛后可为 tested_clean */
  effective_status?: string
  status_basis?: "explicit" | "derived"
  settled?: boolean
  has_findings?: boolean
  meta: Record<string, unknown>
  author: string
  created_at: string
}

export interface FindingPoc {
  type: "http" | "python"
  code: string
}

export const VULNERABILITY_TYPES = [
  "未授权访问", "认证绕过", "水平越权", "垂直越权", "SQL 注入", "命令注入",
  "SSRF", "XSS", "文件读取", "文件上传", "路径穿越", "敏感信息泄露",
  "弱口令", "配置错误", "CSRF", "业务逻辑", "组件漏洞", "其他",
] as const

// 分析包（目录/压缩包/APK/AAB 的统一只读样本容器）
export interface SamplePackageTarget {
  target_id: string
  path: string
  format: string
  size: number
  sha256?: string
  candidate?: boolean
  analyzers: string[]
}

export interface SamplePackageEntry {
  path: string
  format: string
  platform?: string
  size: number
  sha256?: string
  candidate: boolean
}

export interface SamplePackageDependency {
  from: string
  to: string
  kind: string
}

export interface SamplePackage {
  package_id: string
  version_id: string
  manifest_sha256: string
  created_at: string
  file_count: number
  total_bytes: number
  origin?: { source_type?: string; filename?: string; file_count?: number; raw_sha256?: string }
  raw_sha256?: string | null
  targets: SamplePackageTarget[]
  entries?: SamplePackageEntry[]
  dependencies: SamplePackageDependency[]
  source_analysis?: Record<string, unknown> | null
  import_count?: number
  tree_ref?: string
}

export interface SampleTargetAnalysis {
  package_id: string
  version_id: string
  target: SamplePackageTarget
  analysis: Record<string, unknown> | null
  file_exists: boolean
  binary_sha256?: string
  binary_asset_id?: string
}

export interface SampleTargetAnalyzeResponse {
  job_id: string | null
  cached?: boolean
  target_id?: string
  report?: Record<string, unknown>
  binary_sha256?: string
  binary_asset_id?: string
}

export interface SamplePackageUploadSession {
  upload_id: string
  filename: string
  total_size: number
  total_chunks: number
  received_chunks?: number[]
  status: "uploading" | "complete" | "failed"
}

export interface SamplePackagePreview {
  path: string
  format: string
  size: number
  sha256?: string
  kind: "text" | "image" | "structure" | "binary"
  truncated: boolean
  preview_bytes: number
  content_url?: string | null
  media_type?: string
  text?: string
  hex_rows: { offset: number; hex: string; ascii: string }[]
  archive?: Record<string, unknown>
  dex?: Record<string, unknown>
}

export interface FuncNameHistory {
  name: string
  by: string
  at: string
}

export interface FuncEntry {
  id: string
  project_id: string
  binary_sha256: string
  /** hex 字符串（服务端出 API 一律 hex，UI 比较走 sameAddr） */
  address: string
  name: string
  analysis: string
  risk_tags: string[]
  name_history?: FuncNameHistory[]
  confidence: string
  analyzed_by: string
  updated_at: string
}

// ---------- 逆向工作台（headless 缓存三层数据中的客观层） ----------

export interface BinaryMeta {
  arch?: string
  bits?: number
  endian?: string
  imagebase?: string
  entry?: string
  filename?: string
  // ida-mcp 轻量缓存 meta（2026-09-30 断点续拉）：partial=部分缓存
  source?: string
  partial?: boolean
  total_functions?: number
  next_offset?: number
  /** 产出该缓存的引擎（ida / ghidra；2026-10-01 双模式一致性对账） */
  engine?: string
}

export interface BinarySection {
  name: string
  vaddr: string
  size: number
  perms?: string
  entropy?: number
  error?: string
}

export type ToolState = "installed" | "cached" | "off"

export interface BinaryOverview {
  sha: string
  asset_meta: Record<string, unknown>
  /** 样本配置的反编译引擎模式（ida / ghidra；2026-10-01 工作台双模式） */
  engine: string
  cached: boolean
  analysis_job?: {
    id: string
    status: "running"
    progress?: {
      phase?: string; done?: number; total?: number; discovered?: number; completed?: number; failed?: number
      stoppable?: boolean; eta_seconds?: number | null; elapsed_seconds?: number; rate_per_second?: number
    }
  } | null
  meta: BinaryMeta | null
  sections: BinarySection[] | { error: string } | null
  imports: Record<string, string[]> | null
  function_count: number
  analyzed_count: number
  risk_count: number
  /** 指向该样本资产的发现数（删除确认框列数量用；2026-10-01 样本删除） */
  findings_count: number
  /** 该样本的业务逻辑块数（删除确认框列数量用） */
  logic_blocks_count: number
  strings_count: number
  db_path: string | null
  tools: { ida: { state: ToolState }; ghidra: { state: ToolState }; mcp: { state: ToolState } }
}

/** GET .../binaries/{sha}/strings 行（v3 契约，address 为 hex 串） */
export interface BinaryStringRef {
  func: string
  func_name: string | null
  from: string
}

export interface BinaryString {
  address: string
  string: string
  length: number
  type: "cstr" | "unicode"
  n_refs: number
  refs: BinaryStringRef[]
}

export interface BinaryStrings {
  items: BinaryString[]
  truncated: boolean
}

/** GET .../binaries/{sha}/functions 精简行（address 为 hex 串） */
export interface CachedFuncRow {
  address: string
  name: string | null
  size: number
  has_pseudo: boolean
  n_calls: number
  status?: "pending" | "done" | "failed"
  error?: string
}

export interface CachedFunction {
  address: string
  name: string | null
  size: number
  calls: string[]
  pseudocode: string | null
  /** 缓存缺席由 MCP 实时取回时为 "mcp"；headless 缓存命中无此字段 */
  source?: string
  /** 按需详情（IDA 拉取样本自动 analyze_batch 拉取落盘）：反汇编行列表，超长截断 */
  disasm?: { lines: string[]; truncated?: boolean } | null
  disasm_pending?: boolean
}

export interface XrefRef {
  address: string | null // null=导入函数（缓存中无地址）
  name: string
}

export interface XrefData {
  address: string
  name: string | null
  callers: XrefRef[]
  callees: XrefRef[]
  /** 缓存缺席由 MCP func_profile 实时降级取回时为 "mcp" */
  source?: string
}

export interface SampleUploadResponse {
  cached: boolean
  job_id: string | null
  sha: string
  asset_id: string
}

export interface ArtifactUploadResponse {
  id: string
  path: string
  sha256: string
  size: number
}

/** 附件随发（2026-09-19）：上传落 artifact（kind=attachment），随任务/引导下发 */
export interface AttachmentInfo {
  id: string
  path: string
  name: string
  size: number
}

export interface FuncCreateBody {
  binary_sha256: string
  address: number | string
  name: string
  analysis?: string
}

/** PATCH funcs：risk_tags 传 [] 表示清空，省略/不传表示不动（显式 null 也不动） */
export interface FuncPatchBody {
  name?: string
  note?: string
  risk_tags?: string[]
}

export interface FindingPatchBody {
  status?: string
  evidence?: Record<string, unknown>
  /** F10 人工修订：title 非空 / severity 五档 / vuln_class 可空 */
  title?: string
  severity?: string
  vuln_class?: string
  impact?: string
  remediation?: string
  /** F11 判级依据：None=不动，空串=清空 */
  rating_basis?: string
  /** C6 分两类：vuln=漏洞 / intel=有效发现·关键发现 */
  category?: FindingCategory
  summary?: string
  affected_assets?: string
  test_environment?: string
  reproduction_steps?: string
  verification_result?: string
  risk_assessment?: string
  pocs?: FindingPoc[]
}

export interface JudgmentTemplates {
  builtin: Record<string, string>
  user: Record<string, string>
}

export interface Session {
  id: string
  project_id: string
  role: string
  name: string
  status: string
  started_at: string
  finished_at?: string | null
  /** v3：会话收件箱未读数（撤回传播等系统私信；与审批收件箱分设） */
  unread?: number
  /** F9 worker 启动制：armed=已启动自动接任务；running=worker 在跑 */
  worker_armed?: boolean
  worker_running?: boolean
  /** v0.71 任务即窗口：绑定的任务 id（''=无绑定手动窗） */
  bound_task_id?: string
  context_task_id?: string
  context_mode?: "review" | string
}

/** 会话收件箱私信（DESIGN §6.7 的 1.5/1.6；本切片只有系统投递的 basis_stale） */
export interface InboxMessage {
  id: string
  project_id: string
  to_session: string
  kind: "basis_stale" | string
  ref_id: string
  payload: {
    finding_id?: string
    title?: string
    vuln_class?: string
    by?: string
    note?: string
    [k: string]: unknown
  }
  created_at: string
  read_at?: string | null
}

export interface RoleInfo {
  role: string
  name: string
  description?: string | null
  persona?: string | null
  task_types: string[] | null
  tools?: string[] | null
  max_steps?: number | null
}

export interface Approval {
  id: string
  project_id: string
  session_id: string | null
  action: Record<string, unknown>
  risk: string
  /** M5 D2：行动边界全文（server 拼好，与编排器 _mission_section 同源），审批卡对照显示 */
  boundary?: string
  status: "pending" | "approved" | "rejected" | "consumed"
  requested_by: string
  decided_by?: string | null
  created_at: string
  decided_at?: string | null
}

// 会话协作流（会话中心化 M4）：编排器+会话窗节点；delegate/derive/inbox/dm 边
export interface SessionGraphNode {
  id: string
  kind: "orch" | "session"
  label: string
  name: string
  role: string
  status: string
  /** 窗内当前委托 objective */
  current?: string
  /** 窗内排队委托数 */
  queue?: number
}
export interface SessionGraphEdge {
  id: string
  source: string
  target: string
  kind: "delegate" | "derive" | "inbox" | "dm"
  refs: Record<string, unknown>[]
}
export interface SessionGraph {
  nodes: SessionGraphNode[]
  edges: SessionGraphEdge[]
}

// 审批 action.op 判别（批 4：spawn_session 走专属处理器，其余 op 只翻状态）
export interface SpawnSessionAction {
  op: "spawn_session"
  role: string
  reason?: string
}

// decide 响应：命中 op 处理器才带 executed；失败不回滚批准（executed:false + error）
export interface DecideApprovalResult {
  approval_id: string
  status: "approved" | "rejected"
  executed?: boolean
  session_id?: string
  job_id?: string
  exit_code?: number
  /** 批 5 触发点 E：批准后顺带唤醒的空闲窗 sid 列表 */
  kicked?: string[]
  /** 赛跑终检跳过（如任务已不处于待执行）：executed=true 但无实际动作 */
  skipped?: string
  error?: string
}

export interface Job {
  id: string
  kind: string
  status: "running" | "done" | "error"
  result: unknown
  error: string | null
  // 后端可变 dict 的引用，轮询时每次读到最新值：
  //  - IDA 拉取：pulled=已拉函数数，total=null=分母未知，rows=本页新增函数行；
  //  - headless 导出（大样本 P3）：done=已反编译函数数，total=函数总数，phase=阶段。
  meta?: {
    progress?: {
      pulled?: number
      total?: number | null
      rows?: CachedFuncRow[]
      done?: number
      phase?: string
    }
  }
}

// 编排一轮（orchestrator-tick job）的结构化结果（批 3，DESIGN §6.8/机制 1.9）
/** L0 提案（批 6）：校验过的动作不写实体，等人在事件流行内采纳 */
export interface OrchProposal {
  op: 'publish_task' | 'spawn_session'
  args: Record<string, unknown>
  event_id?: number
}

export interface OrchTickResult {
  summary: string                 // 本轮动作摘要
  published: string[]             // 自主发布的任务 id
  spawned: { session_id: string; role: string }[]
  digest: string | null           // 本轮是否写了项目简报
  proposals: OrchProposal[]       // 批 6：仅 L0 提案模式非空
}

// A5 重排优先级（orchestrator-replan job）的结构化结果
export interface ReplanUpdate { task_id: string; old: number; new: number }
export interface ReplanSkip { task_id: string; reason: string }
export interface ReplanResult {
  updated: ReplanUpdate[]         // 实际改了优先级的 open 任务（逐行审计）
  skipped: ReplanSkip[]           // claimed/done/乱 id/非法值/未变，逐条跳过
  note?: string                   // 无 open 任务空转时的说明（零 LLM）
  reason?: string                 // 触发原因（manual/human-publish/worker-*/...）
  error?: string                  // LLM/传输失败时的结构化错误
}

// ---------- IDA 双向写回（P2） ----------

/** POST writeback 单条：address 吃 hex 串；name/comment 至少一个 */
export interface WritebackItem {
  address: string
  name?: string
  comment?: string
}

export interface WritebackResultLine {
  address: string
  ok: boolean
  error?: string
}

/** Job result：ok/locked/no-db/no-tool/unsupported（非 ok 都是结构化降级不是 Job 失败） */
export interface WritebackResult {
  status: "ok" | "locked" | "no-db" | "no-tool" | "unsupported"
  results?: WritebackResultLine[] | Record<string, WritebackResultLine[]>
  applied?: number
  guidance?: string
  /** ok 时的实际通道：MCP 在线直写当前 IDA 库；缺省=headless 写 .i64 */
  channel?: string
}

export interface PullNameChange {
  address: string
  old_name: string | null
  new_name: string
}

export interface PullNamesResult {
  status: "ok" | "locked" | "no-db" | "no-tool" | "unsupported"
  changed?: PullNameChange[]
  guidance?: string
}

/** POST .../pull-ida-functions 结果（stopped=中途停止，已拉部分已生效，可断点续拉） */
export interface PullIdaFunctionsResult {
  status: "ok" | "no-mcp" | "stopped"
  sha?: string
  function_count?: number
  pulled?: number
  total?: number | null
  changed?: PullNameChange[]
  hint?: string
}

/** POST .../push-names-to-ida 结果（func_kb 有效命名反向写回 GUI IDA，只改内存库） */
export interface PushNamesToIdaResult {
  status: "ok" | "no-mcp"
  applied?: number
  results?: unknown
  hint?: string
}

// ---------- 攻击链（chains，人工建链 P2） ----------

export type ChainNodeType = "func_kb" | "finding" | "artifact"
export type ChainStatus = "hypothesis" | "validated" | "exploited"

/** 链节点的实体快照（API 层组装；实体已删=null，link.deleted=true） */
export interface ChainEntity {
  id: string
  // func_kb
  name?: string
  address?: string // hex
  risk_tags?: string[]
  // finding
  title?: string
  severity?: string
  status?: string
  category?: string | null
  // artifact
  kind?: string
  path?: string
  description?: string
}

export interface ChainLink {
  id: string
  chain_id: string
  seq: number
  node_type: ChainNodeType
  node_id: string
  edge_note: string
  created_at: string
  entity: ChainEntity | null
  deleted: boolean
}

export interface Chain {
  id: string
  project_id: string
  name: string
  goal: string
  status: ChainStatus
  created_at: string
  updated_at: string
  /** 仅详情端点带 */
  links?: ChainLink[]
}

/** 列表行：链头 + link_count */
export type ChainSummary = Chain & { link_count: number }

// ---- R4 逆向开发蓝图（DESIGN.md §9 R4） ----

/** 蓝图整体状态：只进不退；reviewed/ready 由人类流转（Agent 无入口） */
export type BlueprintStatus = "draft" | "reviewed" | "ready" | "building" | "built"
/** 模块状态：划分产出 pending → 深析 analyzed → spec 钉死 specd → 容器自测过 tested */
export type BlueprintModuleStatus = "pending" | "analyzed" | "specd" | "tested"

export interface BlueprintModule {
  name: string
  desc: string
  /** hex 地址串（与工作台 hexAddr 同口径） */
  func_addresses: string[]
  /** 接口约定：函数签名/数据结构/协议格式（并行深析与组装的防冲突锚） */
  spec: string
  notes: string
  status: BlueprintModuleStatus
}

export interface Blueprint {
  id: string
  project_id: string
  binary_sha256: string
  name: string
  goal: string
  status: BlueprintStatus
  content_md: string
  modules: BlueprintModule[]
  created_at: string
  updated_at: string
}

// ---- 业务逻辑块（逆向第四页签：函数协作/业务语义，人机共写） ----

/** 挂接函数快照：join func_kb 取当前名；func_name 空=函数已不在库（回退显示 hex） */
export interface LogicBlockFunc {
  address: string // hex 串
  func_id: string
  func_name: string
  /** 角色注：该函数在本块中的职责一句话 */
  role: string
  seq: number
}

export interface LogicBlock {
  id: string
  project_id: string
  binary_sha256: string
  name: string
  /** 业务逻辑描述（markdown） */
  description: string
  seq: number
  created_at: string
  updated_at: string
  /** 仅详情端点带；列表行用 func_count */
  funcs?: LogicBlockFunc[]
}

/** 列表行：块头 + 挂接数 */
export type LogicBlockSummary = Omit<LogicBlock, "funcs"> & { func_count: number }

export interface Artifact {
  id: string
  project_id: string
  path: string
  kind: string
  description: string
  sha256: string
  author: string
  /** 归属元数据（工作区隔离 W3）：{task_id?, session_id?}；附件另带 original_name/size */
  meta?: { task_id?: string; session_id?: string; original_name?: string; size?: number }
  created_at: string
}

// ---------- packs 管理（设置页） ----------

export interface PackRole {
  name?: string // yaml name 行 = 中文显示名（可 ≠ file stem；stem 才是角色 id）
  description?: string | null
  persona?: string | null
  skills: string[] | null
  task_types: string[] | null
  tools?: string[] | null
  max_steps?: number | null
  file: string // yaml 文件名（去 .yaml 后即角色 id，sessions.role/URL/日志标识）
}

/** 专家（expert-pool M1/M3，GET /api/experts 视图；数据源 packs/experts/<id>.yaml） */
export interface Expert {
  id: string // yaml stem = 专家 id（sessions.role 等标识沿用同一值域）
  name?: string | null // 中文显示名，缺省回退 id
  description?: string | null
  persona?: string | null
  tracks?: string[] | null // null/缺省 = 服务全部轨
  skills?: string[] | null // null = 全量专家（不限定）
  task_types?: string[] | null
  tools?: string[] | null
  max_steps?: number | null
  protected?: boolean // _generalist 兜底专家，拒删
  variants?: Record<string, Record<string, unknown>> // {track: {field: value}} 轨变体
  file?: string // "experts/<id>.yaml"（HistoryButton 用；virtual 虚拟单例无文件）
  kind?: "virtual" // 对话化编排器 M3：虚拟单例（id=orchestrator，不入 yaml 池、不认领任务）
}

/** 阶段目标（对话化编排器 M2，§4.3：meta.phase_goal，GET/PUT /projects/{pid}/goal；
 *  goal 统一后=唯一目标判据层，criteria 为判据第一优先源） */
export interface PhaseGoal {
  text: string
  criteria?: string[] // 验收判据（一行一条；判据四层优先级之首 goal>mission>模板>内置）
  phase?: string | null
  source: string // "chat"
  created_at: string
  confirmed_by: string // "human"
}

/** 编排器拟人身份（M3，§4.4：meta.orchestrator_persona） */
export interface OrchPersona {
  display_name: string
  persona: string
}

/** 分阶段工作流（pentest-phased-workflow，§四）：GET /projects/{pid}/phase。
 * 轨无阶段剧本时 enabled=false（其余字段缺省）。 */
export interface PhaseSpec {
  id: string
  name: string
  goal?: string
  order?: number
  focus?: Record<string, number> // 阶段配额：编排器派单配比建议（软引导）
  gate?: Record<string, number> // 出口门指标（min_assets/min_high_value/min_verified/idle_rounds）
  gate_types?: string[] // 被本阶段门拦的任务类型
  tasks?: { task_type: string; role?: string; objective: string; acceptance?: string }[]
  next?: string[]
}

/** 阶段全序项（阶段条步骤渲染用，按 order 排序） */
export interface PhaseSummary {
  id: string
  name: string
  order: number
}

export interface PhaseInfo {
  enabled: boolean
  phases?: PhaseSummary[]
  current?: string
  spec?: PhaseSpec
  gate?: {
    metrics: { assets: number; high_value: number; verified: number; idle_rounds: number }
    met: boolean
    unmet: string[] // 人读未达标明细（「资产 3/10」）
    forward: string[] // order 递增的前向目标（过门分流方向）
  }
  history?: { phase: string; entered_at: string; by: string; auto: boolean; reason?: string }[]
  notified?: string | null // gate_open_notified：过门动作已分流的 target
}

/** 专家写表单（POST/PUT /api/experts）：全字段提交式覆写，None/缺省=不落键（skills 缺键=全量专家语义） */
export interface ExpertBody {
  name?: string | null
  description?: string | null
  persona?: string | null
  tracks?: string[] | null
  skills?: string[] | null
  task_types?: string[] | null
  tools?: string[] | null
  max_steps?: number | null
  variants?: Record<string, Record<string, unknown>> | null
}

/** 场景档（expert-pool M4a，GET /api/tracks/{track}/profiles）：五件套 + 看板默认视图 */
export interface TrackProfile {
  id: string
  name?: string | null
  description?: string | null
  experts?: string[] // 组队预设（建项时物化进 meta.experts）
  rule_profiles_owners?: string[] | null
  rule_profiles_rating?: string[] | null
  board_view?: string | null // findings | assets | funcs | board
  playbook?: string | null
  artifacts?: string[] | null
  knowledge?: string[] | null
}

export interface SkillDef {
  name: string
  kind: "capability" | "track"
  pack: string // 能力包名或场景轨名
  description: string
  keywords: string[]
  features: string[]
  file_features: string[]
  platforms: string[]
  formats: string[]
  vuln_classes: string[]
  task_types: string[]
  required_tools: string[]
  enabled: boolean
  /** cc 风格技能：正文自包含，辅助资料位于技能目录资源子目录。 */
  mode?: "self-contained" | "legacy" | "thin-route" | string
  self_contained?: boolean
  resources?: string[]
  expert_refs?: string[]
  estimated_tokens?: number
}

export interface SkillDetail {
  name: string
  meta: Record<string, unknown>
  skill: SkillDef | null
  raw: string // SKILL.md 全文（含 frontmatter，可编辑）
  resources?: string[]
}

export interface SkillResource {
  path: string
  content: string
}

export interface SkillCatalogPackage {
  name: string
  label: string
  skills: SkillDef[]
  skill_count: number
  estimated_tokens: number
}

export interface SkillCatalog {
  capabilities: SkillCatalogPackage[]
  skill_count: number
  capability_count: number
  estimated_tokens: number
}

export interface SkillFileEntry {
  path: string
  name: string
  is_dir: boolean
  size: number
  modified: number
  kind: "text" | "image" | "binary"
  content?: string
  preview?: string
}

/** 路由评分分类明细（breakdown，C5） */
export interface RouteBreakdown {
  category: string
  label: string
  weight: number
  hits: string[]
  score: number
}

export interface RouteHit {
  name: string
  kind: string
  pack: string
  description: string
  enabled: boolean
  score: number
  matched: string[]
  breakdown?: RouteBreakdown[]
}

// ---------- kb 本地基线（C 批） ----------

export interface KbFile {
  path: string
  size: number
  mtime: string
  /** 展示标题（frontmatter title > # H1 > stem，kbindex 同口径；F15） */
  title: string
}

export interface KbSourceTree {
  id: string
  recursive: boolean
  root: string
  files: KbFile[]
}

/** kb 正文搜索命中（GET /kb/search，matches 为该文件命中次数） */
export interface KbSearchHit {
  path: string
  source: string
  matches: number
  snippet: string
  title?: string
  kind?: "playbook" | "pattern" | "case" | "ref" | string
  layer?: number
  is_attachment?: boolean
}

export interface KbRefHit {
  file: string
  kind: "skill" | "kb"
  line: number
  forms: string[]
}

export interface KbRead {
  cap: string
  path: string
  source: string
  size: number
  content: string
  refs: KbRefHit[]
}

export interface KbWriteResult {
  status: string
  path: string
  backup?: string | null
  created?: boolean
  trash?: string
}

export interface KbRenameResult {
  status: string
  old_path: string
  new_path: string
  updated: { file: string; replacements: number }[]
  skipped_relative: string[]
}

export type SkillVocab = Record<
  "keywords" | "features" | "file_features" | "platforms" |
  "formats" | "vuln_classes" | "task_types",
  { value: string; count: number }[]
>

// ---------- 统一变更提案（C4） ----------

export type ProposalKind = "kb" | "skill" | "case" | "pattern" | "playbook"
export type ProposalMode = "edit" | "create" | "rename" | "delete"
export type ProposalStatus = "pending" | "approved" | "rejected"
export type ProposalOrigin = "agent" | "review" | "human"

export interface ProposalTarget {
  kind: ProposalKind
  // kb
  cap?: string
  path?: string
  new_path?: string
  // skill
  skill_kind?: "capability" | "track"
  owner?: string
  name?: string
}

export interface ProposalRevision {
  at: string
  by: string
  note?: string
}

export interface ProposalLive {
  exists_now: boolean
  diff: string
  refs: KbRefHit[] | null
}

export interface Proposal {
  id: string
  status: ProposalStatus
  origin: ProposalOrigin
  project: string | null
  session: string | null
  task: string | null
  target: ProposalTarget
  mode: ProposalMode
  content: string | null
  summary: string
  reason: string
  evidence: string
  source_refs?: { cap: string; path: string; title?: string }[]
  revisions: ProposalRevision[]
  created_at: string
  decided_at: string | null
  decided_by: string | null
  decision_note: string | null
  /** 仅详情端点带（对照磁盘实时算） */
  live?: ProposalLive
}

export interface KbEvolutionSource {
  path: string
  source: string
  title?: string
  kind?: string
  layer?: number
  snippet?: string
  reason?: string
}

export interface KbEvolutionDraft {
  kind: "case" | "pattern" | "playbook"
  title: string
  path: string
  content: string
  summary: string
  sources: { cap: string; path: string; title?: string }[]
  path_adjusted?: boolean
  notice?: string
}

export interface ReviewProposalsResult {
  landed: { id: string; summary: string }[]
  rejected: { summary: string; error: string }[]
  error?: string
  raw_head?: string
}

/** POST /api/skills/route-preview 试算入参 */
export interface RoutePreviewBody {
  query?: string
  track?: string
  capabilities?: string[]
  role?: string
  features?: string[]
  file_features?: string[]
  labels?: string[]
  include_disabled?: boolean
}

export interface McpServer {
  name: string
  url: string
  transport: string
  enabled: boolean
  domains: string[]
  command?: string | null // stdio 传输用
  args?: string[]
}

export interface OwnerRule {
  tag: string
  content: string
}

/** F11 评级与价值口径（tracks/<track>/rules/rating/<tag>.md），形状同 OwnerRule */
export type RatingRule = OwnerRule

/** F11 rule_profiles 项目级生效档案（config.rule_profiles）：
 * 纯显式（2026-10-01 去三态）：owners/rating 都只收字符串清单，勾哪个生效哪个，
 * 未配/空=不注入；旧 owners="*"（自动全注入）已退役。 */
export interface RuleProfiles {
  owners?: string[]
  rating?: string[]
}

// ---------- 阶段 3B：CRUD / doctor / 历史版本 ----------

/** POST 新建技能向导 */
export interface SkillCreateBody {
  name: string
  description?: string
  keywords?: string[]
  features?: string[]
  file_features?: string[]
  platforms?: string[]
  formats?: string[]
  vuln_classes?: string[]
  task_types?: string[]
}

export interface DoctorIssue {
  level: "error" | "warning" | "info"
  code: string
  target: string
  message: string
}

export interface DoctorReport {
  issues: DoctorIssue[]
  counts: { error: number; warning: number; info: number }
}

export interface HistoryVersion {
  version: string
  ts: string
  size: number
  mtime: string
}

export interface HistoryList {
  file: string
  exists: boolean
  versions: HistoryVersion[]
}

// ---------- 情报面板（E9，DESIGN.md §16；全局模块，与项目无关） ----------

export interface IntelArticle {
  id: string
  url: string
  title: string
  source: string
  kind: string // cve | article
  summary: string
  published_at: string
  fetched_at: string
  score: number
  direction: string
  is_priority: number // 1 = KEV/优先标
  score_detail: string // JSON 字符串：{by: llm|rule|priority, hot?}
  brief_date: string | null
  read: number
  starred: number
}

export interface IntelBriefMeta {
  date: string
  stats: Record<string, unknown>
  created_at: string
}

export interface IntelBrief extends IntelBriefMeta {
  content: string // markdown 全文
}

export interface IntelFeed {
  name: string
  url: string
  kind: string
}

export interface IntelProfile {
  directions: Record<string, number>
  stage: string
}

export interface IntelOverview {
  counts: { articles: number; unread: number; starred: number }
  today: IntelBrief | null
  top_unread: IntelArticle[]
}

// ---------- E10：Obsidian vault 接入 + 学习档案 + 周计划（DESIGN.md §16.3） ----------

export interface IntelVaultConfig {
  path: string
  enabled: boolean
}

export interface IntelVaultInfo extends IntelVaultConfig {
  configured: boolean
  notes: number
  last_indexed: string
}

/** GET /api/intel/vault/tree 节点：目录 {name, children}，笔记叶子 {name, path, title, tags, mtime} */
export interface VaultNode {
  name: string
  path?: string
  title?: string
  tags?: string[]
  mtime?: string
  children?: VaultNode[]
}

export interface VaultSearchHit {
  path: string
  title: string
  tags: string[]
  mtime: string
  snippet: string
}

export interface IntelLearningProfile {
  declared: { directions: Record<string, number>; stage: string }
  vault: {
    total: number
    by_direction: Record<string, { notes: number; last_active: string }>
    /** F5：高频笔记主题 tag 频次（cap 15，仅元数据） */
    top_tags?: { tag: string; count: number }[]
  }
  platform: Record<string, { notes: number; total: number; read: number; starred: number }>
}

export interface IntelPlanMeta {
  week: string
  inputs: Record<string, unknown> // {by: llm|template, week, brief_date?, articles?}
  created_at: string
}

export interface IntelPlan extends IntelPlanMeta {
  content: string
}

// ---------- F6 内置浏览器（DESIGN.md §7；pentest/redteam 轨专属） ----------

export interface BrowserStatus {
  playwright_installed: boolean
  chromium_installed: boolean
  install_cmd: string
  instances: { project_id: string; sessions: BrowserSessionInfo[] }[]
}

export interface BrowserSessionInfo {
  sid: string
  owner: string
  task_id: string | null
  url: string | null
  title: string | null
  origin?: "human" | "agent"
  paused?: boolean
  last_action_at?: number | null
}

export type WorkspaceTreeKind = "file" | "dir"

export interface WorkspaceTreeNode {
  name: string
  path: string
  kind: WorkspaceTreeKind
  size?: number
  children?: WorkspaceTreeNode[]
}

export interface WorkspaceTreeResponse {
  root: string
  nodes: WorkspaceTreeNode[]
  truncated: boolean
  limits: { max_depth: number; max_nodes: number; max_children: number }
}

export interface BrowserState {
  sessions: BrowserSessionInfo[]
  profile_dir: string
  running: boolean
  playwright_installed: boolean
  track: string
}

/** http_history 行（列表 body 截短 200 预览；全量走 detail） */
export interface HttpHistoryRow {
  id: number
  project_id: string
  session_id: string | null
  task_id: string | null
  source: "browser" | "replay" | "intruder"
  batch_id: string
  meta: Record<string, unknown>
  method: string
  url: string
  status: number | null
  req_headers: Record<string, string>
  req_body: string | null
  resp_headers: Record<string, string>
  resp_body: string | null
  resp_mime: string
  body_truncated: boolean
  is_binary: boolean
  duration_ms: number | null
  created_at: string
}

/** 导航/重发/爆破目标未登记资产时的 422 detail（前端一键登记用） */
export interface AssetMissingDetail {
  reason: string
  host: string
  asset_missing: boolean
}

/** 爆破 payload 集：list=候选串 / range=整数区间 */
export interface IntruderPayloadSpec {
  position: string
  type: "list" | "range"
  values?: string[]
  start?: number
  stop?: number
  step?: number
}

/** 爆破模板：HTTP 请求四要素，url/headers/body 中用 §标记§ 占位 */
export interface IntruderTemplate {
  method: string
  url: string
  headers?: Record<string, string>
  body?: string | null
}

/** F6-v3 拦截挂起包（仅人工浏览流量；快照形态，raw 为后端渲染的完整报文） */
export interface InterceptPending {
  hold_id: string
  direction: "request" | "response"
  method: string
  url: string
  status: number | null
  raw: string
  editable: boolean
  is_binary: boolean
  size: number
  created_at: number
  expires_in_ms: number
}

/** 拦截快照：两开关 + 挂起列表 */
export interface InterceptState {
  request_enabled: boolean
  response_enabled: boolean
  pending: InterceptPending[]
}


// ---- 网关策略快照（gateway-config-view M1，DESIGN §7）----

export interface GatewayRuntimeLevel {
  name: string
  level: number
  label: string
  desc: string
}
export interface GatewayThreatRow {
  threat_class: string
  allowed: string[]
  note: string
}
export interface GatewayRateRule {
  tool: string
  requirement: string
  params: string[]
  hint: string
}
export interface GatewayConfig {
  runtime_levels: GatewayRuntimeLevel[]
  threat_matrix: GatewayThreatRow[]
  net_modes: { modes: string[]; default: string; note: string }
  pathguard_rules: string[]
  rate_rules: GatewayRateRule[]
  exec_params: { default_timeout: number; sandbox_image: string }
}

/** Agent 工具目录（GET /api/agent-tools，agent-tools-view 2026-09-24） */
export interface AgentToolParam {
  type?: string | string[]
  description?: string
  enum?: string[]
  items?: { type?: string; enum?: string[] }
}
export interface AgentTool {
  name: string
  description: string
  group: string
  input_schema: {
    type?: string
    properties: Record<string, AgentToolParam>
    required?: string[]
  }
}
export interface AgentToolsResponse {
  groups: string[]
  tools: AgentTool[]
}

/** 宿主能力探测（GET /api/projects/{pid}.capability 与 POST /api/gateway/probe 同构） */
export interface ProbeRow {
  name?: string
  available: boolean
  detail: string
}
export interface CapabilityInventory {
  docker: ProbeRow
  wsl: ProbeRow
  tools: ProbeRow[]
}

// ---------- 网络空间测绘（cyberspace-mapping M1+M2，2026-09-23） ----------

/** FOFA 中转配置（GET/PUT /api/fofa/config；key 永远脱敏回显，前4后4） */
export interface FofaConfig {
  base_url: string
  key: string
  key_set: boolean
}

export interface FofaTestResult {
  ok: boolean
  remain?: number | null
  expire?: string | null
  base_url: string
  error?: string
}

/** FOFA 查询行（fields 白名单序归一化 + existing 三锚点既有标注） */
export interface FofaSearchRow {
  ip: string
  port: string
  protocol: string
  host: string
  domain: string
  title: string
  products: string[]
  existing: { domain: boolean; service: boolean; host: boolean }
}

export interface FofaSearchResult {
  total: number
  size: number
  page: number
  rows: FofaSearchRow[]
  history_id?: string | null
}

/** FOFA 查询历史（轻量列表项，不含 rows——点开单条才拉全量恢复） */
export interface FofaHistoryItem {
  id: string
  query: string
  size: number
  total: number
  ts: string
}

/** 资产导入预览（parse_table + 列映射嗅探建议） */
export interface ImportPreview {
  filename: string | null
  header: string[] | null
  columns: { kind: string; confidence: number }[]
  rows: string[][]
  total_rows: number
  truncated: boolean
}

/** 导入汇总（asset.imported 事件同构） */
export interface ImportSummary {
  batch_id: string
  source: string
  total: number
  created: number
  merged: number
  skipped: number
  failed_count: number
  failed: { index: number; reason: string }[]
  author: string
  skipped_parse?: number
}


// ---------- 智能体工作台（K9，2026-09-29） ----------

export interface ChatAgent {
  id: string
  kind: "orchestrator" | "expert"
  name: string
  description: string
}

export interface ChatTodoItem {
  id: string
  title: string
  status: "pending" | "in_progress" | "completed"
}

export interface ChatUsage {
  input?: number
  output?: number
  steps?: number
  cache_read?: number
  cache_creation?: number
  ctx_limit?: number
  ctx_soft?: number
  compaction?: {
    compacted: boolean
    level?: number
    masked?: number
    cached?: boolean
    est?: number
    budget?: number
  }
  breakdown?: {
    system: number
    refs: number
    tools: number
    messages: number
  }
}

export interface ChatThreadError {
  category: string          // network / rate_limit / auth / quota / context / bad_request / unknown
  title: string             // 分类标题（如「网络中断」）
  message: string           // 原始异常文案（技术细节）
  hint: string              // 下一步建议
}

export interface ChatThread {
  id: string
  project_id: string
  agent_id: string
  title: string
  status: "idle" | "running" | "error"
  parent_thread_id: string | null
  spawned_task: string
  todo: ChatTodoItem[]
  usage?: ChatUsage | null
  /** 最近一轮失败的结构化错误（status=error 时存在；新轮开始清空） */
  error?: ChatThreadError | null
  created_at: string
  updated_at: string
}

export interface ChatToolCall {
  id: string
  name: string
  args: Record<string, unknown>
}

export interface ChatMessage {
  id: number
  thread_id: string
  role: "user" | "assistant" | "tool"
  content: string
  /** assistant 行：该步推理正文（schema v31 持久化；''=无）。仅展示，不参与 LLM 重放。 */
  thinking?: string
  tool_calls: ChatToolCall[]
  tool_use_id: string
  created_at: string
}

export interface ChatThreadDetail {
  thread: ChatThread
  messages: ChatMessage[]
  /** 项目工作区绝对路径（会话流文件卡把工具参数里的绝对路径落回相对路径用） */
  work_dir?: string
}

export interface ChatMcpTool {
  name: string
  description: string
  input_schema: Record<string, unknown>
}

export interface ChatMcpServer {
  name: string
  transport: string
  domains: string[]
  online: boolean
  tools: ChatMcpTool[]
  session_scoped?: boolean
}

// ---------- 多智能体协调（独立协调域） ----------
export type CoordinationPlanStatus = "draft" | "active" | "paused" | "completed"
export type CoordinationTaskStatus = "pending" | "ready" | "running" | "blocked" | "completed" | "failed"

export interface CoordinationTask {
  id: string
  project_id: string
  plan_id: string
  title: string
  description: string
  role: string
  status: CoordinationTaskStatus
  priority: number
  depends_on: string[]
  evidence: Record<string, unknown>[]
  /** M3：计划节点绑定的真实 tasks.id；空=尚未派单 */
  task_id: string
  created_at: string
  updated_at: string
  verification?: CoordinationVerification | null
}

export interface CoordinationPlan {
  id: string
  project_id: string
  name: string
  objective: string
  status: CoordinationPlanStatus
  config: Record<string, unknown>
  created_at: string
  updated_at: string
  tasks: CoordinationTask[]
  task_counts?: Record<string, number>
}

export interface CoordinationPlanProposalNode {
  id: string
  title: string
  description?: string
  role: string
  priority: number
  status: CoordinationTaskStatus
  depends_on: string[]
}

export interface CoordinationPlanProposal {
  plan_id: string
  status: CoordinationPlanStatus
  name: string
  objective: string
  nodes: CoordinationPlanProposalNode[]
}

export interface CoordinationOverview {
  plans: CoordinationPlan[]
  active_plan_id: string | null
  objects: CoordinationObject[]
  conflicts: CoordinationConflict[]
  summary: { plans: number; tasks: number; running: number; blocked: number; completed: number; objects: number; conflicts: number }
}
export interface CoordinationPreflight {
  plan_id: string
  status: CoordinationPlanStatus
  revision: string
  plan: CoordinationPlan
  dependencies: { task_count: number; root_count: number; ready_count: number; blocked_count: number; cycle: boolean; unresolved: { task_id: string; dependency: string }[] }
  safety: { track: string; mission: Record<string, unknown>; roe: Record<string, unknown>; autonomy: Record<string, unknown> }
  blockers: { code: string; severity: "error" | "warning"; message: string }[]
}
export interface CoordinationCommunication {
  id: string
  project_id: string
  to_session: string
  kind: string
  ref_id: string
  payload: Record<string, unknown>
  created_at: string
  read_at?: string | null
  unread?: boolean
}

export type CoordinationObjectKind = "function" | "string" | "xref" | "behavior" | "evidence" | "artifact"
export interface CoordinationObject {
  id: string
  project_id: string
  plan_id: string | null
  task_id: string | null
  kind: CoordinationObjectKind
  name: string
  object_ref: string
  data: Record<string, unknown>
  source: string
  confidence: number
  artifact_refs: { artifact_ref: string; relation: string; created_at: string }[]
  created_at: string
  updated_at: string
}
export type CoordinationConflictStatus = "open" | "resolved" | "dismissed"
export interface CoordinationConflict {
  id: string
  project_id: string
  left_object_id: string
  right_object_id: string
  field: string
  summary: string
  status: CoordinationConflictStatus
  resolution: string
  created_at: string
  updated_at: string
}
export type CoordinationVerificationStatus = "passed" | "needs_evidence" | "conflict" | "failed"
export interface CoordinationVerification {
  id: string
  project_id: string
  plan_id: string
  task_id: string
  status: CoordinationVerificationStatus
  score: number
  issues: { kind: string; message: string; conflict_id?: string }[]
  followup_task_ids: string[]
  checked_at: string
}
