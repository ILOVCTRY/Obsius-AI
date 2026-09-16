// core API 数据契约（与 core/api/app.py 响应对齐，宽松可选字段）

export interface ProjectMeta {
  id: string
  name: string
  /** 场景轨（单选）；旧项目 domain 由后端读取时透明映射 */
  track: string
  /** 能力包（多选） */
  capabilities: string[]
  created_at: string
  config?: Record<string, unknown>
}

/** 项目自主配置（DESIGN §6.8，批 2；存于 project.config.autonomy，双写 project.json+黑板） */
export interface Autonomy {
  level: "L0" | "L1" | "L2"
  paused: boolean
  sessions_cap: number
  max_chain_ticks: number
  token_budget: number | null
  task_budget: number | null
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
  mode?: "pentest" | "redteam"  // C2 作战模式（§6.9）
  mission?: { text: string; criteria: string }
  redteam_roe?: { targets: string; window: string; exclusions: string; approver: string }
  llm_calls: number
  tokens: { used: number; budget: number | null; pct: number | null }
  tasks: { published: number; budget: number | null; pct: number | null }
  chain?: ChainState
}

export interface ProjectDetail extends ProjectMeta {
  task_stats: Record<string, number>
  findings: number
  assets: number
  usage: ProjectUsage
  capability: Record<string, unknown>
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
  resumable?: boolean  // E12：failed 行派生（原会话落盘快照在 → 看板可「带现场续跑」）
  workset?: string[]   // B1 工作集软声明（advisory，供避让不阻塞）
  wait_for?: string[]  // B2 被占资源键（open 行门控标记，claim_next 排除）
  blocked_reason?: "error" | "awaiting_human"  // C1：fail 通道结构化原因（v8）
}

// A3 直播间任务流图
export interface TaskGraphSession {
  id: string
  name: string
  status: string
}

export interface TaskGraphNode {
  id: string
  objective: string
  task_type: string
  status: Task["status"]
  priority: number
  noise_budget: string
  parent_id: string | null
  claimed_by: string | null
  plan: TaskPlanStep[]
  updated_at?: string
  session: TaskGraphSession | null
}

export interface TaskGraphEdgeRef {
  kind: "basis_stale" | "finding_update" | string
  ref_id: string
  title: string
}

export interface TaskGraphEdge {
  id: string
  source: string
  target: string
  kind: "parent" | "inbox" | "suggest"
  refs?: TaskGraphEdgeRef[]
}

export interface TaskGraph {
  nodes: TaskGraphNode[]
  edges: TaskGraphEdge[]
}

export interface LlmProvider {
  name: string
  base_url: string
  api_key?: string          // 仅写入；读出脱敏（只有 has_key）
  has_key?: boolean
  models: string[]
  enabled: boolean
}

export interface ModelInfo {
  models: string[]
  providers: LlmProvider[]
  default: { provider: string; model: string } | null
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

export interface Finding {
  id: string
  project_id: string
  vuln_class: string
  title: string
  severity: string
  status: string
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
  status: string   // E7：open / visited / scanning / tested_clean
  meta: Record<string, unknown>
  author: string
  created_at: string
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
  cached: boolean
  meta: BinaryMeta | null
  sections: BinarySection[] | { error: string } | null
  imports: Record<string, string[]> | null
  function_count: number
  analyzed_count: number
  risk_count: number
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
}

export interface CachedFunction {
  address: string
  name: string | null
  size: number
  calls: string[]
  pseudocode: string | null
  /** 缓存缺席由 MCP 实时取回时为 "mcp"；headless 缓存命中无此字段 */
  source?: string
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
  default_noise?: string | null
  tools?: string[] | null
  max_runtime?: string | null
  max_steps?: number | null
}

export interface Approval {
  id: string
  project_id: string
  session_id: string | null
  action: Record<string, unknown>
  risk: string
  status: "pending" | "approved" | "rejected"
  requested_by: string
  decided_by?: string | null
  created_at: string
  decided_at?: string | null
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
  /** 批 5 触发点 E：批准后顺带唤醒的空闲窗 sid 列表 */
  kicked?: string[]
  error?: string
}

export interface Job {
  id: string
  kind: string
  status: "running" | "done" | "error"
  result: unknown
  error: string | null
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

export interface Artifact {
  id: string
  project_id: string
  path: string
  kind: string
  description: string
  sha256: string
  author: string
  created_at: string
}

// ---------- packs 管理（设置页） ----------

export interface PackRole {
  name?: string
  description?: string | null
  persona?: string | null
  skills: string[] | null
  task_types: string[] | null
  default_noise?: string | null
  tools?: string[] | null
  max_runtime?: string | null
  max_steps?: number | null
  file: string // yaml 文件名（去 .yaml 后即角色名）
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
}

export interface SkillDetail {
  name: string
  meta: Record<string, unknown>
  skill: SkillDef | null
  raw: string // SKILL.md 全文（含 frontmatter，可编辑）
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

export type ProposalKind = "kb" | "skill"
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
  revisions: ProposalRevision[]
  created_at: string
  decided_at: string | null
  decided_by: string | null
  decision_note: string | null
  /** 仅详情端点带（对照磁盘实时算） */
  live?: ProposalLive
}

export interface ReviewProposalsResult {
  landed: { id: string; summary: string }[]
  rejected: { summary: string; error: string }[]
  error?: string
  raw_head?: string
}

/** PUT /api/tracks/{track}/roles/{name} 表单（未提交字段保留原值；列表显式 null=白名单关闭） */
export interface RoleUpdateBody {
  description?: string | null
  persona?: string | null
  skills?: string[] | null
  task_types?: string[] | null
  default_noise?: string | null
  tools?: string[] | null
  max_runtime?: string | null
  max_steps?: number | null
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

// ---------- 阶段 3B：CRUD / doctor / 历史版本 ----------

/** POST 新建角色（clone_from 缺省=空白模板） */
export interface RoleCreateBody {
  name: string
  clone_from?: string | null
}

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
  vault: { total: number; by_direction: Record<string, { notes: number; last_active: string }> }
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
