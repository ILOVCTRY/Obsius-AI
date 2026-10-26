// core API 数据契约（与 core/api/app.py 响应对齐，宽松可选字段）

/** 思考强度七档（与 lib/effort.ts、core/llm/effort.py 同源，避免循环 import 只取类型） */
export type EffortGearName =
  | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra"

export interface ArtexIntent {
  id: string
  parent_id: string | null
  task_id: string
  objective: string
  constraints: Record<string, unknown>
  asset_ids: string[]
  /** 串行链前置 Intent id（依赖全 completed 才被调度；前置 failed/cancelled 时本 Intent 置 blocked） */
  depends_on: string[]
  expert: string
  priority: number
  status: string
  attempt: number
  result: string
  error: string
  created_at: string
  updated_at: string
}

export interface ArtexTaskGraph {
  task_id: string
  nodes: ArtexIntent[]
  edges: { parent_id: string; child_id: string }[]
  /** 注册目标（含各自已关联的证据）；老任务可能为空 */
  goals?: ArtexGoal[]
  /** 资产覆盖度（任务锚点分母 × 事实锚定分子）；老任务可能为空 */
  coverage?: ArtexCoverage
}

/** 资产覆盖度的单类型分桶（total=范围内该类型资产数，tested=已被事实锚定过的数）。 */
export interface ArtexCoverageByType {
  type: string
  total: number
  tested: number
}

/** 任务资产覆盖度（复刻上游 Coverage 结构，粗粒度参考指标，非精确度量）。 */
export interface ArtexCoverage {
  /** 覆盖度功能是否开启；false 时其余字段为零值 */
  enabled: boolean
  /** 任务范围锚点行数；0 → 范围未锚定 */
  scope_rows: number
  /** 分母来源：anchored=任务锚点 / inferred=由 Intent∪目标推断 */
  scope_source?: "anchored" | "inferred" | string
  /** 范围内资产数（分母，已删资产不计入） */
  denominator: number
  /** 已被探索图 fact 节点锚定过的资产数（分子，粗略下限） */
  tested: number
  /** 范围内尚未被事实锚定过的资产数 */
  untested?: number
  /** 覆盖度 0–1；分母 0 时为 null */
  pct: number | null
  by_type: ArtexCoverageByType[]
}

/** 未覆盖资产清单（可补测 backlog）分页。 */
export interface ArtexUntestedAssets {
  assets: { id: string; type: string; label: string; status: string }[]
  total: number
  type: string
}

export interface ArtexCoverageResponse {
  coverage: ArtexCoverage
  untested: ArtexUntestedAssets
}

/** 覆盖图节点：范围内资产标 in_scope=true，为连边拉入的范围外祖先标 false（灰）。 */
export interface ArtexCoverageGraphNode {
  key: string
  kind: string
  label: string
  tested: boolean
  in_scope: boolean
  asset_id: string
  status: string
  parent_id: string | null
}

export interface ArtexGoal {
  id: string
  task_id: string
  title: string
  content: string
  status: "open" | "met" | string
  asset_ids: string[]
  metadata: Record<string, unknown>
  /** proves 边连过来的证据（kind/title） */
  evidence?: { id: string; kind: string; title: string; note?: string }[]
}

export interface ArtexExplorationNode {
  id: string
  project_id: string
  task_id: string
  intent_id?: string | null
  kind: "goal" | "intent" | "fact" | "finding" | "hint" | string
  /** 渲染期派生（artex-exploration-canvas）：goal 分 begin/goal，fact 分 fact/summary。
   *  老 API 可能缺该字段，前端按 metadata.source 反推（见 ExplorationCanvas.roleOf）。 */
  role?: "begin" | "goal" | "intent" | "fact" | "summary" | "finding" | "hint" | string
  /** 兜底汇总事实默认折叠（与显式结论重复）；老数据无此字段时按 source 判断。 */
  collapsed_by_default?: boolean
  title: string
  content: string
  status: string
  confidence: number
  asset_ids: string[]
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface ArtexExplorationEdge {
  id: string
  project_id: string
  from_node_id: string
  to_node_id: string
  relation: "spawns" | "yields" | "derived_from" | "proves" | string
  /** 边分档（artex-exploration-canvas）：spine=主脊（参与分层）/ reverse=依据 /
   *  system=系统挂链（默认隐藏）。缺字段时前端按 system 处理。 */
  category?: "spine" | "reverse" | "system" | string
  metadata: Record<string, unknown>
  created_at: string
}

export interface ArtexExplorationGraph {
  project_id: string
  task_id?: string | null
  nodes: ArtexExplorationNode[]
  edges: ArtexExplorationEdge[]
}

export interface ArtexTaskRuntime {
  project_id: string
  task_id: string
  status: "exploring" | "draining" | "goal_reached" | "needs_review" | "cancelled" | string
  goal: string
  metadata: {
    reason?: string
    counts?: Record<string, number>
    fact_count?: number
    /** 任务级预算超限原因（artex-taskclock）：deadline 或 Intent 数上限。 */
    limit_reason?: string
    goal_met?: boolean
    goal_met_reason?: string
  }
  created_at: string
  updated_at: string
}

export interface ArtexTaskSummary {
  task_id: string
  objective: string
  status: string
  /** 目标驱动的任务级运行态（exploring/draining/goal_reached/needs_review/cancelled） */
  runtime_status?: string
  counts: Record<string, number>
  intent_count: number
  goal_count?: number
  goal_met_count?: number
  goal_open_count?: number
  goals?: ArtexGoal[]
  /** 资产覆盖度（任务列表内联，供卡片显示进度） */
  coverage?: ArtexCoverage
  updated_at: string
  running: boolean
}

export interface ArtexTaskListResponse {
  tasks: ArtexTaskSummary[]
}

export interface ArtexActivityEvent {
  id: number
  kind: string
  payload: Record<string, unknown>
  author?: string
  created_at: string
}

export interface ArtexActivityResponse {
  events: ArtexActivityEvent[]
  latest_id: number
}

export interface ArtexEngineStatus {
  project_id: string
  running: boolean
  active_workers: number
  counts: Record<string, number>
  active: ArtexIntent[]
  task_id?: string | null
}

export interface ArtexPlanResult {
  root: ArtexIntent
  intents: ArtexIntent[]
  count: number
}

export interface ProjectMeta {
  id: string
  name: string
  /** 场景轨（单选）；旧项目 domain 由后端读取时透明映射 */
  track: string
  /** kb 可见方向：有专家绑定时=专家 knowledge 面推导，未绑定=盘上绑定 */
  directions: string[]
  /** 盘上原始绑定（设置页跟随项目缺省用） */
  directions_bound?: string[]
  /** 专家组队（expert-pool M2）：空/缺省=存量直通（按轨全池），非空=绑定清单 */
  experts?: string[]
  created_at: string
  config?: Record<string, unknown>
  /** 宿主运行时实测（GET /api/projects/{pid} 返回；M3 runtime chip 可用性门） */
  capability?: HostCapability
  /** 回收站条目（GET /api/projects/trash）：目录名=恢复句柄（<slug>-<UTC>） */
  trash_name?: string
  /** 墓碑戳（clear 后落 project.json）：项目内文件已抹除，只剩名字信息，不可恢复 */
  trash_purged_at?: string
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
  /** 资产数（level-two-counts 口径，2026-10-16）：资产树只取
   *  「根节点 → 二级子节点 → 三级 URL」，**只数二级子节点**（深度恰为 1）；
   *  根是分组容器、三级 URL 只是二级的明细，都不计。 */
  assets: number
  /** 其中「已测干净」（effective_status == tested_clean）的条数 */
  assets_clean?: number
  /** 已测干净/总资产 的整数百分比（total=0 → 0） */
  assets_clean_pct?: number
  usage: ProjectUsage
  capability: HostCapability
}

/** 顶栏 CTF 题目进度聚合（2026-10-22）：`solved` 与 `total` 同出
 *  `GET /api/projects/{pid}/challenges` 的**同一份清单**——分子分母必须同源同 scope，
 *  否则「已解出 2/3」会因两次请求落在不同时刻而自相矛盾。
 *  `solved` 直接用后端的 `ChallengeSummary.solved`（由 flag 台账派生、候选不计），
 *  **不在前端重新推导**——黑板 `ChallengeStateBadge` 用的就是它，两边口径必须一致。 */
export interface CtfProgressStats {
  /** 题目总数（CTF 轨；其他轨该清单为空，顶栏不显示这项） */
  total: number
  /** 其中已解出的题目数 */
  solved: number
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
  dimension: string          // v34：所属测试面 id，''=未标面
  status: "open" | "closed"
  outcome_type: IntentOutcome
  outcome_refs: string[]
  dead_reason: string
  evidence_refs: string[]
  author: string
  created_at: string; closed_at: string | null; updated_at: string
  revision: number
}

// 项目级意图推导图（GET /api/projects/{pid}/intent-graph，2026-10-13 逆向轨）：
// 样本 → 意图 → 收尾（发现|死路），只读逻辑层（无执行层 attempts）。
//
// `direction`（ctf-direction-ledger，2026-10-25）：CTF 主控派出去的一条**方向组**。
// 它**不是** intent——intent 是「可证伪假设、必须收尾为 vuln/finding/dead_end」，
// 而方向组是「派过的一条活」，可能只是执行失败或被人工删线程。两层分开才能让
// 「发现」这个词保持证据含义。边 `direction` 只从题目（sample）指向方向组。
export type IntentGraphNodeType = "sample" | "intent" | "finding" | "direction"
export type IntentGraphEdgeKind = "derive" | "outcome" | "bypass" | "direction"

export interface IntentGraphNode {
  id: string
  type: IntentGraphNodeType
  // sample：label 优先样本文件名、退回 sha 前 12；asset_type 区分 binary 与非 binary 锚点
  label?: string
  asset_type?: string
  value?: string
  // intent
  statement?: string
  status?: "open" | "closed" | "unverified" | "verified" | "false-positive"
  outcome?: IntentOutcome
  finding_ids?: string[]
  dead_reason?: string
  dimension?: string
  author?: string
  created_at?: string | null
  closed_at?: string | null
  default_hidden?: boolean            // closed dead_end：默认隐藏
  sample_id?: string | null
  // finding
  title?: string
  severity?: string
  category?: string
  vuln_class?: string
  // direction（CTF 方向组）：subgoal 是方向原文，attempts 是累计执行次数，
  // last_run 只给最近一次尝试（重试细节去工作台开子线程看）
  subgoal?: string
  expert?: string
  attempts?: number
  flags_got?: number
  terminal?: boolean
  last_summary?: string
  last_run?: {
    status: string
    sub_thread_id: string
    at: string
  }
}

export interface IntentGraphEdge {
  source: string
  target: string
  kind: IntentGraphEdgeKind
}

export interface IntentGraph {
  nodes: IntentGraphNode[]
  edges: IntentGraphEdge[]
  counts: {
    samples: number; intents: number; open: number
    closed: number; dead_end: number; findings: number
    directions?: number                 // 仅在存在方向组时出现
  }
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
  /** GPT 思考强度；开启思考链时发送给 OpenAI 兼容接口 */
  thinking_effort?: EffortGearName | null
  /** 供应商专属 HTTP/HTTPS 代理；留空沿用系统代理环境 */
  proxy?: string | null
  /** system 兼容通道（2026-10-17）：该网关丢弃 system 字段时，system 前置于首条 user 消息 */
  system_in_user?: boolean
  /**
   * 视觉能力（vision 通道，2026-10-19）：勾选=该供应商的模型支持图片输入。
   * 决定 `view_image` 是出图还是只回文本 + OCR 引导；未声明=不支持（宁缺勿炸，
   * 往不支持的网关塞图片块会被 400 打死整轮）。
   */
  vision?: boolean
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

/** CTF 题目卡片（challenge-flag-ledger v47）：题目行 + 附件/发现计数 + **派生状态**。
 *
 * `state` 由 flag 台账派生（不是独立字段）：untouched / running / partial /
 * solved / abandoned。`flags` 含 rejected——人工裁决界面要看到被否决的那条
 * 才能撤销。
 */
export interface ChallengeFlag {
  id: string
  challenge_id: string
  flag: string
  flag_format: string
  confidence: number
  source: string
  transform: string
  judged_by: "ai" | "human"
  verdict: "pending" | "accepted" | "rejected"
  verdict_reason: string
  author: string
  finding_id: string
  /** 本行对应题面「第几条 flag」的提示（0 基；-1 = 未指定，确认时按顺序推断）。
   * 与题目级 `flag_hints[hint_index]` 对齐（flag-hint，2026-10-21） */
  hint_index: number
  created_at: string
  updated_at: string
}

export type ChallengeState = "untouched" | "running" | "partial" | "solved" | "abandoned"

export interface ChallengeSummary extends Asset {
  description: string
  title: string
  /** 题面原文（用户说的 hit） */
  brief: string
  entry_hint: string
  /** 题目方向：web/pwn/crypto/reverse/misc… */
  direction: string
  tags: string[]
  flag_format: string
  flag_count: number
  /** 每条 flag 的题面提示（`meta.flag_hints`，一行一条，与序号 0 基对齐）。
   * 确认一条 flag 后要把这句提示告诉 agent——flag 原文本身看不出方向，
   * 「需要 xxx 管理员后台密码为 flag」这类话才是指路的那句（flag-hint，2026-10-21） */
  flag_hints: string[]
  state: ChallengeState
  solved: boolean
  accepted_flags: number
  /** 候选（pending，AI 判定通过待人工确认）——**不计 solved**（方案 A，2026-10-20） */
  candidate_flags: number
  rejected_flags: number
  running_workers: number
  abandoned: boolean
  /** 主控主动收尾（ctf_conclude）：不再派工，但**不是**解出 */
  concluded: boolean
  /** 同题内前缀互不相同的 accepted flag（非空 = 至少一条是假的） */
  flag_conflict: ChallengeFlag[]
  /** accepted 数超过 quota（多为用户下调 flag_count，仅作异常提示） */
  over_quota: boolean
  flags: ChallengeFlag[]
  attachment_count: number
  attachments: { id: string; type: string; value: string }[]
  finding_count: number
  intent_count: number
}

export interface ChallengeCreateBody {
  /** 可空：空名按题面/入口内容指纹生成稳定 value（AI 之后改 title 只动展示层）。 */
  name?: string
  title?: string
  brief?: string
  entry_hint?: string
  direction?: string
  tags?: string[]
  flag_format?: string
  flag_count?: number
  /** 每条 flag 的题面提示（一行一条，序号与 flag 一一对应；空行=该序号无提示，
   * 中间的空行必须保留否则后面全错位）——flag-hint，2026-10-21 */
  flag_hints?: string[]
  /** 入口点子资产（web 入口 URL / 附件） */
  entries?: { value: string; type?: string; meta?: Record<string, unknown> }[]
  thread_id?: string
}

export interface ChallengePatchBody {
  name?: string
  title?: string
  brief?: string
  entry_hint?: string
  direction?: string
  tags?: string[]
  flag_format?: string
  flag_count?: number
  /** 空数组 = 清空全部提示（后端把空列表当「删键」） */
  flag_hints?: string[]
}

export interface ChallengeFlagsResponse {
  challenge_id: string
  flags: ChallengeFlag[]
  /** 题目级「每条 flag 的题面提示」（`meta.flag_hints`，与 flag 序号 0 基对齐）。
   * 台账行只有 `hint_index`，提示正文在题目 meta 上，故这里一并返回 */
  flag_hints: string[]
  state: {
    state: ChallengeState
    solved: boolean
    accepted: number
    /** 候选（pending）——不计 solved，前端单独显示 */
    candidate: number
    rejected: number
    quota: number
    abandoned: boolean
    /** 主控主动收尾：停派工但不等于解出 */
    concluded: boolean
    running: number
    /** 同题内前缀互不相同的 accepted flag（非空 = 冲突，需人工裁决） */
    conflict: ChallengeFlag[]
    over_quota: boolean
  }
}

export interface FindingPoc {
  type: "http" | "python" | "image"
  code: string
  /** image 类型（截图证据）：引用上传后的产物（artifact）id（附件通道落 kind=attachment）；
   * code 填空串。渲染在 POC 段嵌图；新格式 image 项不进 legacyPocSteps，避免与复现步骤重复展示 */
  artifact_id?: string
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
  origin?: { source_type?: string; filename?: string; file_count?: number; raw_sha256?: string; directory?: string }
  raw_sha256?: string | null
  targets: SamplePackageTarget[]
  entries?: SamplePackageEntry[]
  dependencies: SamplePackageDependency[]
  source_analysis?: Record<string, unknown> | null
  import_count?: number
  tree_ref?: string
  /** 关联目录包（2026-10-24）：内容树就是用户自选的外部文件夹，原地索引、不复制。
   *  这类包的文件树编辑一律禁用（改内容请直接在磁盘上改后重新选择）。 */
  linked?: boolean
  /** 执行工作区根：关联目录包时=该目录，否则=项目工作区。 */
  exec_workspace?: string
}

/** 「选择分析包」的应用内目录浏览器返回项（GET .../workspace/dirs）。 */
export interface DirEntry {
  name: string
  path: string
  kind: "dir"
  entry_count: number | null
  root: boolean
  is_project?: boolean
  is_workspace?: boolean
}

export interface DirListing {
  path: string
  parent: string | null
  roots: boolean
  entries: DirEntry[]
  truncated?: boolean
}

/** 关联目录分析包（POST .../sample-packages/link）→ 后台 Job，轮询取结果。 */
export interface SamplePackageLinkResponse {
  job_id: string
  directory: string
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

// 登记为资产（2026-10-12）：包内 PE/ELF/Mach-O 目标登记为 binary 黑板资产（不复制文件）
export interface SampleTargetRegisterResponse {
  binary_sha256: string
  binary_asset_id: string
  target_id: string
  path: string
  filename: string
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
  tools?: string[] | null
  /** MCP server 白名单；缺省/null = 跟随全局已启用 server */
  mcp_servers?: string[] | null
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

export interface CtfAgentJobResult {
  exit_code: number
  status: string
  flags: string[]
  output: string
  no_submit: boolean
  provider?: string
  model?: string
}

export interface Job {
  id: string
  kind: string
  status: "running" | "done" | "error"
  result: unknown
  error: string | null
  // 后端可变 dict 的引用，轮询时每次读到最新值：
  //  - IDA 拉取：pulled=已拉函数数，total=null=分母未知，rows=本页新增函数行；
  //  - headless 导出（大样本 P3）：done=已反编译函数数，total=函数总数，phase=阶段；
  //  - 代理池抓取/验证（2026-10-08）：phase=fetch/check/prune，done/total，
  //    eta_seconds=预计剩余秒（null=未知），elapsed_seconds/rate_per_second/message。
  meta?: {
    progress?: {
      pulled?: number
      total?: number | null
      rows?: CachedFuncRow[]
      done?: number
      phase?: string
      eta_seconds?: number | null
      elapsed_seconds?: number
      rate_per_second?: number
      message?: string
    }
  }
}

// 编排一轮（orchestrator-tick job）的结构化结果（批 3，DESIGN §6.8/机制 1.9）
/** L0 提案（批 6）：校验过的动作不写实体，等人在事件流行内采纳。
 *  任务机制退役（2026-10-06）后 op 仅剩「组建队伍 / 亲自执行」两类。 */
export interface OrchProposal {
  op: 'build_team' | 'execute'
  args: Record<string, unknown>
  event_id?: number
}

export interface OrchTickResult {
  summary: string                 // 本轮动作摘要
  published: string[]             // 自主发布的任务 id（任务机制退役后恒空，键保留以对齐后端）
  spawned: { session_id: string; role: string }[]
  teams: { team_id: string; name: string; member_count: number }[]  // 本轮组建的 Team
  digest: string | null           // 本轮是否写了项目简报
  proposals: OrchProposal[]       // 批 6：仅 L0 提案模式非空
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
/** 功能树节点状态：pending=未验收 / passed=人类验收通过（绿色卡片） */
export type BlueprintNodeStatus = "pending" | "passed"

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

/** 功能树节点（v36 独立表）：单向树，parent_id=null 为根 */
export interface FeatureTreeNode {
  id: string
  name: string
  parent_id: string | null
  status: BlueprintNodeStatus
  desc: string
  func_addresses: string[]
}

/** 功能树（分析阶段产物，独立于蓝图，可被多蓝图复用为施工图纸） */
export interface FeatureTree {
  id: string
  project_id: string
  binary_sha256: string
  name: string
  nodes: FeatureTreeNode[]
  created_at: string
  updated_at: string
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
  feature_tree_id: string  // v36 引用功能树（施工图纸），''=未关联
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
  feature_node_id: string  // v36 挂到功能树某节点下钻，''=未挂
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
  tools?: string[] | null
  /** MCP server 白名单；缺省/null = 跟随全局已启用 server */
  mcp_servers?: string[] | null
  max_steps?: number | null
  file: string // yaml 文件名（去 .yaml 后即角色 id，sessions.role/URL/日志标识）
}

/** 专家（expert-pool M1/M3，GET /api/experts 视图；数据源 packs/experts/<id>.yaml） */
export interface Expert {
  id: string // yaml stem = 专家 id（sessions.role 等标识沿用同一值域）
  name?: string | null // 中文显示名，缺省回退 id
  description?: string | null
  persona?: string | null
  tracks?: string[] | null // 可服务轨域；**空/null = 不启用**（2026-10-25 反转，原口径是空=全轨）
  skills?: string[] | null // null = 全量专家（不限定）
  /** kb 方向白名单；null 或 [] 都表示不限方向（两态字段） */
  knowledge?: string[] | null
  tools?: string[] | null
  /** 控制器类智能体的**可勾选候选全集**（后端下发）。
   *  必须含 `artex_*` / `ctf_*` / `todo_write` / `call_expert`——它们不在
   *  `AGENT_TOOLS` 里（走专用 dispatch 分支），不额外下发就永远勾不到。
   *  设置页把它与 AGENT_TOOLS 合并成勾选清单。非控制器为 null。 */
  tools_runtime?: string[] | null
  /** true = 这是控制器（候选集需合并 tools_runtime）。
   *  **不是只读**：勾选照常提交，yaml 的 `tools` 是控制器的真实开关。 */
  tools_controller?: boolean
  /** MCP server 白名单；缺省/null = 跟随全局已启用 server */
  mcp_servers?: string[] | null
  max_steps?: number | null
  protected?: boolean // _generalist 兜底专家，拒删
  /** 免意图闸（仅 worker）：true = 跳过「意图先行闸」与「发现必挂意图」。
   *  **设置页没有对应控件**，但后端在 PUT 未带该键时继承盘上原值——
   *  前端只需原样透传读到的值，不要凭空构造 false（那会显式关掉它）。 */
  intent_free?: boolean
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
  gate?: Record<string, number> // 出口门指标（min_assets/min_verified/idle_rounds）
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
    metrics: { assets: number; verified: number; idle_rounds: number }
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
  /** kb 方向白名单；null 或 [] 都表示不限方向（两态字段） */
  knowledge?: string[] | null
  // task_types 已退役（2026-10-21）：不提交，后端也不再序列化该键。
  tools?: string[] | null
  /** MCP server 白名单；缺省/null = 跟随全局已启用 server */
  mcp_servers?: string[] | null
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
  /** 展示分组（后端按命名约定推断：分诊/Web 攻击/二进制与逆向/…；纯展示，不参与加载） */
  group?: string
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

export interface SkillCatalog {
  skills: SkillDef[]
  skill_count: number
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

export type ProposalKind = "kb" | "skill" | "index" | "case" | "pattern" | "playbook"
export type ProposalMode = "edit" | "create" | "rename" | "delete" | "suggest"
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

export interface ReviewProposalsResult {
  landed: { id: string; summary: string }[]
  rejected: { summary: string; error: string }[]
  error?: string
  raw_head?: string
}

/** 知识库迭代触发规则（GET /api/proposals/rules）——与后端判定同源。 */
export interface KbIterationRule {
  key: string
  label: string
  detail: string
}

export interface KbIterationChannel {
  key: string
  label: string
  detail: string
}

export interface KbIterationRules {
  /** 项目开关当前状态；未传 pid 时为 null */
  enabled: boolean | null
  switch: {
    key: string
    label: string
    scope: string
    off_meaning: string
  }
  channels: KbIterationChannel[]
  rules: KbIterationRule[]
  limits: { per_session_proposals: number; note: string }
  landing: string
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
  published_day?: string
  via?: string
  // Google News RSS <source url>：发布方站点根地址，解析中转链接失败时的兜底入口
  source_site?: string
  ranking_score?: number
  date_known?: boolean
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

export interface IntelSearchHistoryItem {
  id: string
  query: string
  cache_key: string
  total: number
  created_at: string
  meta: { backend?: string; window_label?: string; errors?: string[] }
}

export interface IntelProfile {
  directions: Record<string, number>
  stage: string
  keywords?: { text: string; added_at: string }[]
  search?: Record<string, unknown>
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

/** 重发传输选项（2026-10-07 重放工作台，全量对齐 Yakit 式 Repeater 控件）。
 *  默认值即「与既有行为一致」：跟随重定向、无代理、httpx、证书校验开。 */
export interface ReplayOptions {
  force_https?: boolean
  follow_redirects?: boolean
  proxy?: string | null
  body_max_bytes?: number | null
  insecure?: boolean
  gm_tls?: boolean
  timeout_s?: number
  server_name?: string | null
}

/** 国密 TLS 通道能力探测（重放工作台据此置灰开关；available=false 时绝不静默降级成普通 TLS） */
export interface GmStatus {
  available: boolean
  path: string | null
  guide: string
}

/** 代理池（fir-proxy 托管，2026-10-07） */
export interface ProxyRecord {
  proxy: string
  protocol?: string | null
  location?: string | null
  latency?: number | null
  speed?: number | null
  anonymity?: string | null
  score?: number | null
  status?: string | null
  /** HTTPS 能力探测（2026-10-11）：经 CONNECT 转发 https 是否通；null/缺=未探测。 */
  https_ok?: boolean | null
  /** 内容注入/劫持型出口标志（2026-10-11）：两目标回同一响应体→true，已判 Failed 剔除。 */
  hijacked?: boolean | null
}

/** 池按协议分桶计数（后端 `protocol_counts`；https 是能力探测非协议类目）。 */
export interface ProxyCounts {
  http: number
  socks: number
  https: number
  total: number
}

/** 抓取/验证 Job 的进度快照（后端 `job.meta.progress` 可变 dict，轮询即最新）。 */
export interface ProxyJobProgress {
  phase?: string
  done?: number
  total?: number
  eta_seconds?: number | null
  elapsed_seconds?: number
  rate_per_second?: number
  message?: string
}

export interface ProxyStatus {
  running: boolean
  endpoint: { http: string; socks5: string; control: string } | null
  current: ProxyRecord | null
  count: number
  working: number
  regions: Record<string, number>
  uptime_s?: number
  pool_size: number
  /** 按协议分桶计数（http / socks=socks4+socks5 / https 能力）；前端过滤与阈值展示用。 */
  counts?: ProxyCounts
  /** 运行中的抓取/验证 Job（切页回来重挂轮询用；无则 null）。 */
  job?: { id: string; kind: string; progress?: ProxyJobProgress } | null
}

export interface ProxyListResponse {
  source: "serve" | "file"
  proxies: ProxyRecord[]
}

/** 指定网站可达性/WAF 封禁检测的单条结果（2026-10-12）。 */
export interface ProxyUrlCheckRow {
  proxy: string
  protocol?: string | null
  ok: boolean                 // 拿到响应
  status_code?: number | null
  latency?: number | null     // 秒
  blocked: boolean            // 被判该站封禁（比对直连基线）
  reason?: string             // 结论说明
}

/** 指定网站检测结果（后端 `ProxyPool.check_url`）。 */
export interface ProxyCheckUrlResult {
  url: string
  total: number
  reachable: number
  blocked: number
  usable: { proxy: string; protocol?: string | null; latency?: number | null;
            status_code?: number | null }[]
  results: ProxyUrlCheckRow[]
  baseline: { ok: boolean; status_code?: number | null; error?: string } | null
}

/** 代理池可配项（验证基准站等，`GET/PUT /api/proxy/config`）。 */
export interface ProxyConfigView {
  validate_target: string
  validate_target_https: string | null
  validate_target_b: string | null
  validate_timeout_s: number
  prune_latency_ms: number | null
  /** 单次抓取条数上限（2026-10-15）；0=不限。前端「抓取条数」输入框的初值来源。 */
  fetch_limit: number
  defaults: {
    validate_target: string
    validate_target_https: string | null
    validate_target_b: string | null
    validate_timeout_s: number
    prune_latency_ms: number | null
    fetch_limit: number
  }
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

/** 飞书连接器（feishu-push v1）：确认的中危及以上漏洞 → 自定义机器人 webhook */
export interface FeishuPushRecord {
  ts: string
  ok: boolean
  detail: string
  project?: string
  finding_id?: string
  title?: string
  severity?: string
  trigger?: string
}

export interface FeishuStats {
  attached: string[]
  pushed_total: number
  worker: {
    running: boolean
    queue_depth: number
    queue_cap: number
    sent: number
    failed: number
    dropped: number
    consecutive_failures: number
    degraded: boolean
    recent: FeishuPushRecord[]
  } | null
}

export interface FeishuConfig {
  enabled: boolean
  webhook_url: string                     // 脱敏回显（含 * = 未修改）
  webhook_set: boolean
  secret_set: boolean
  min_severity: string
  min_severity_choices: string[]
  projects: string[]                      // 空 = 全部项目
  notify_on_escalation: boolean
  include_poc: boolean
  rate_limit_per_min: number
  timeout_s: number
  retry: number
  base_url: string
  stats: FeishuStats
}

export interface FeishuTestResult {
  ok: boolean
  detail: string
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
  // 累计口径（2026-10-13 缓存命中率）：跨步自加；当步键 input/cache_read/
  // cache_creation 为最近一步。累计命中率 = cache_read_total / input_total。
  input_total?: number
  cache_read_total?: number
  cache_creation_total?: number
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

/** 上下文用量快照（直播间 /context，2026-10-06；会话与指挥同结构） */
export interface ContextUsage {
  /** 窗口分母（token）：供应商 model_context，缺省 256K */
  window: number
  /** 当前占用（token）：最近一次 LLM 调用的 input，无则估算 */
  used: number
  pct: number
  /** 85% 自动压缩阈值（token） */
  threshold: number
  source: "measured" | "estimated"
  breakdown: { system: number; tools: number; messages: number }
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
  /** 线程级模型覆写（schema v37，2026-10-13）：''=跟随全局 executor 默认 */
  provider?: string
  model?: string
  /**
   * 线程级思考强度（schema v48，thinking-effort，2026-10-21）：七档
   * （minimal/low/medium/high/xhigh/max/ultra）。''=跟随该供应商配置。
   */
  thinking_effort?: string
  /** CTF 题目归属（v38，2026-10-14）：绑定 challenge 资产 id */
  challenge_asset_id?: string
  /** 联网搜索开关（v43，web-search-toggle，2026-10-19）：主控开 = spawn 子代理默认继承 */
  web_search?: boolean
  /**
   * 待人工确认的候选 flag 条数（2026-10-22）。
   *
   * 由列表接口 `GET /api/projects/{pid}/chat/threads` 按该线程绑定的题目
   * 批量派生（`challenge_flag_quota` 口径的 `candidate`，**只算 verdict=pending**）。
   * 0/缺失 = 无待确认事项。**不持久化**——它是台账的派生值，不是线程字段，
   * 所以只在这条列表响应里出现，单独 `GET /chat/threads/{id}` 不带。
   */
  pending_flags?: number
  created_at: string
  updated_at: string
}

export interface ChatToolCall {
  id: string
  name: string
  args: Record<string, unknown>
}

/**
 * 系统注入消息的派生标记（2026-10-24）。
 *
 * 后端 `store.list_messages` 按文案开头的白名单标签（`〔系统补达〕` 等）派生，
 * **不落库**。人类消息没有该字段——判定「是不是系统消息」只看它存不存在，
 * 前端**不复制前缀表**（两处各写一份必然漂移：本次事故就是后端注释写着
 * 「前端会识别」而前端从未实现）。
 *
 * `body` 是剥掉标签后的正文，渲染 Markdown 用它；`content` 仍是审计原文。
 */
export interface ChatMessageOrigin {
  kind: "system"
  /** 给人看的短标签（「系统补达」「系统唤醒」「人工确认」「主动唤醒」） */
  label: string
  /** 剥掉 `〔标签〕` 后的正文 */
  body: string
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
  /** 仅系统注入的 user 行有；人类发言与 assistant/tool 行无此键。 */
  origin?: ChatMessageOrigin
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

// ---------- 诊断（diagnostics-log，2026-10-14）：会话审计 + 失败日志 ----------

/** 统一会话行：chat=工作台对话线程；agent=任务链执行会话（消息=events 事件流）。 */
export interface DiagSession {
  kind: "chat" | "agent"
  id: string
  name: string
  agent_id: string
  status: string
  /** chat=chat_messages 条数；agent=events 条数 */
  msg_count: number
  /** 归属本会话的 tool.failed / mcp.failed 条数 */
  fail_count: number
  updated_at: string
  created_at: string
  parent_thread_id: string
  provider: string
  model: string
}

export interface DiagSessions {
  chat: DiagSession[]
  agent: DiagSession[]
}

/** 失败日志行（blackboard events 行直出，payload 已解析）。 */
export interface DiagFailure {
  id: number
  project_id: string
  session_id: string | null
  kind: "tool.failed" | "mcp.failed"
  payload: Record<string, unknown>
  author: string
  created_at: string
}

export interface DiagFailures {
  failures: DiagFailure[]
}

/** 会话系统提示词快照（diagnostics-system-prompt，schema v39）：该会话**最近一次
 *  实际下发给 LLM 的完整 system 正文**。快照自 v39 起落库，历史会话无快照。 */
export interface DiagSystemPrompt {
  session_id: string
  project_id: string
  /** chat=工作台对话线程 / agent=任务链执行会话 */
  kind: string
  agent_id: string
  text: string
  chars: number
  tokens: number
  sha256: string
  updated_at: string
}

/** 技能使用统计（diagnostics-skills，2026-10-21）：Agent 到底有没有用技能/知识库。 */
export interface DiagSkillRow {
  name: string
  /** 本轮被注入系统提示的次数 */
  injected: number
  /** 正文真被 skill_open 的次数 */
  opened: number
  /** 打开率 = opened / injected；从未注入过为 null */
  opened_rate: number | null
}

export interface DiagSkillUsage {
  skills: DiagSkillRow[]
  totals: {
    skill_open: number
    skill_injected: number
    distinct_skills_opened: number
    kb_search: number
    kb_open: number
    /** kb.search 零命中次数（知识缺口信号） */
    kb_search_zero: number
    kb_zero_rate: number | null
  }
  events_scanned: number
}

/** 跨供应商兜底记录行（provider-failover，2026-10-21）。
 *
 *  三类 kind 共用一张表：`llm.provider_failed`（每家失败一条，带 category/status/
 *  耗时）、`llm.failover`（换家 from→to）、`llm.failover.exhausted`（全链耗尽）。
 *  category 与后端 `retry.classify_provider_error` 同源。 */
export interface DiagFailoverRow {
  id: number
  project_id: string
  session_id: string | null
  kind: "llm.provider_failed" | "llm.failover" | "llm.failover.exhausted"
  payload: Record<string, unknown>
  author: string
  created_at: string
}

export interface DiagFailoverSummary {
  /** category → 次数（只在 llm.provider_failed 上累计） */
  by_category: Record<string, number>
  by_provider: Record<string, number>
  /** 换家次数 */
  switches: number
  /** 全链耗尽（真正停止）次数 */
  exhausted: number
}

export interface DiagFailover {
  rows: DiagFailoverRow[]
  summary: DiagFailoverSummary
  kinds: string[]
}

/** 请求体留痕开关（diagnostics-request-body，2026-10-17）：进程级开关，默认关。 */
export interface DiagLlmDump {
  enabled: boolean
  /** 落盘根目录（logs/llm_requests） */
  dir: string
  sessions: number
  records: number
  bytes: number
}

/** 请求体清单行（只含统计，正文走详情接口）。 */
export interface DiagRequestMeta {
  rid: string
  ts: string
  provider: string
  model: string
  bytes: number
  truncated: boolean
  messages: number
  /** role=tool / tool_result 块条数 —— 「上一轮工具输出有没有带上去」的直接证据 */
  tool_results: number
  system_chars: number
  system_first: boolean
  /** system 走 user-content 兼容通道（网关丢 system 字段，providers.json system_in_user） */
  system_in_user: boolean
  tools: number
  /** 真实出网字节是否与本地 payload 逐字节相同（false=SDK 重新序列化，字段仍一致） */
  wire_match: boolean
  /** 真实出网字节数（httpx 层录到；0=未录到） */
  wire_bytes: number
  wire_sha256: string
}

/** 请求体全文：request 是真正发往服务端的 body（原样）。 */
export interface DiagRequestDetail extends DiagRequestMeta {
  format: string
  url: string
  sha256: string
  session_id?: string
  project_id?: string
  request: Record<string, unknown>
  /** 每次尝试的真实出网字节指纹（重试会多条） */
  wire?: Array<{ bytes: number; sha256: string }>
}

/** 红线一键自检结果（diagnostics-redline-test，2026-10-17） */
export interface DiagRedlineTest {
  session_id: string
  /** no_system / not_injected / channel_broken / ok / inconclusive / probe_failed / error */
  verdict: string
  summary: string
  error?: string
  /** snapshot=系统提示快照 / record=请求体记录 / none=都没拿到 */
  system_source?: "snapshot" | "record" | "none"
  system_chars?: number
  /** 本地注入：规则标记命中情况 */
  local?: { status: "full" | "partial" | "none"; found: string[]; missing: string[]; chars: number }
  /** 实际探测用的供应商 */
  probe?: { provider: string; model: string; format: string; system_in_user: boolean }
  /** canary 通道测试（决定性证据） */
  channel?: { ok: boolean; canary: string; reply_head: string; note?: string; error?: string
              /** 回复正文为空（思考预算吃满），不能据此判通道断 */
              empty_reply?: boolean }
  /** 内容引用（辅证；未命中不能判否） */
  content?: { ok: boolean | null; needle: string; reply_head: string; note?: string; error?: string }
  elapsed_ms?: number
}

// ---------- Team direct execution（core/team，schema v32；2026-10-06） ----------
// 独立于旧 tasks 与 coordination DAG 的团队域：Team（roster）→ Run（执行快照）→ 成员会话。
// 后端行结构直出（team_runs/team_run_members 等），前端只做展示与轻编辑。
export type TeamStatus =
  | "draft" | "ready" | "starting" | "running" | "partial_failed"
  | "cancelling" | "completed" | "cancelled" | "failed"
export type TeamMemberStatus =
  | "active" | "pending" | "creating" | "running" | "completed" | "failed" | "cancelled"

export interface TeamMember {
  id: string
  team_id: string
  member_key: string
  label: string
  responsibility: string
  role: string
  provider: string
  model: string
  runtime: string
  threat_class: string
  max_steps: number | null
  status: TeamMemberStatus
  created_at: string
  updated_at: string
}

export interface Team {
  id: string
  project_id: string
  name: string
  goal_text: string
  status: TeamStatus
  created_by: string
  revision: number
  config: Record<string, unknown>
  created_at: string
  updated_at: string
  members: TeamMember[]
}

export interface TeamRunMember {
  id: string
  run_id: string
  member_id: string
  member_key: string
  objective: string
  role: string
  provider: string
  model: string
  runtime: string
  threat_class: string
  max_steps: number | null
  session_id: string | null
  execution_id: string
  status: TeamMemberStatus
  error: string
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface TeamRun {
  id: string
  project_id: string
  team_id: string
  status: TeamStatus
  confirmation_revision: string
  cancel_reason: string
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  members: TeamRunMember[]
}

export interface TeamPreflight {
  team: Team
  members: TeamMember[]
  revision: string
  safety: { track: string; roe: Record<string, unknown>; autonomy: Record<string, unknown> }
  capacity: { running: number; requested: number; cap: number }
  blockers: { code: string; message: string }[]
}

/** 成员编辑入参（PATCH 整体替换 roster 时逐条提交） */
export interface TeamMemberInput {
  member_key: string
  label?: string
  responsibility?: string
  role?: string
  provider?: string
  model?: string
  runtime?: string
  threat_class?: string
  max_steps?: number | null
}


