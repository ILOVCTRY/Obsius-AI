import type {
  Approval, Artifact, ArtifactUploadResponse, Asset, BBEvent, BinaryOverview, BinaryStrings,
  CachedFuncRow, CachedFunction, Chain, ChainLink, ChainNodeType, ChainStatus, ChainSummary,
  DecideApprovalResult, DoctorReport, DiscoveredModel, Finding, FindingPatchBody, FuncCreateBody, FuncEntry,
  FuncPatchBody, HistoryList, InboxMessage, Job, KbRead, KbRefHit, KbRenameResult,
  KbSearchHit, KbSourceTree,
  KbWriteResult, LlmProvider, McpServer, ModelInfo, IntelArticle, IntelBrief, IntelBriefMeta,
  IntelFeed, IntelOverview, IntelProfile, IntelVaultConfig, IntelVaultInfo, VaultNode,
  VaultSearchHit, IntelLearningProfile, IntelPlan, IntelPlanMeta,
  OwnerRule, PackRole, ProjectDetail, ProjectMeta,
  Proposal, ProposalOrigin, RoleCreateBody, RoleInfo, RouteHit,
  RoutePreviewBody, RoleUpdateBody, SampleUploadResponse, Session, SkillCreateBody, SkillDef,
  SkillDetail, SkillVocab, Task, TaskGraph, WritebackItem, XrefData,
} from "./types"
import type { Taxonomy } from "./taxonomy"

// core API 客户端——WebUI 是 core API 的平等消费者，不含业务逻辑（DESIGN.md §12）

/** 非 2xx：status 供分支（如 kb 删除 409 带 refs），data 保留结构化 detail */
export class ApiError extends Error {
  status: number
  data: unknown
  constructor(status: number, detail: unknown, statusText: string) {
    const message = typeof detail === "object" && detail !== null
      ? ((detail as { message?: unknown }).message as string) ?? JSON.stringify(detail)
      : String(detail ?? statusText)
    super(`${status}: ${message}`)
    this.status = status
    this.data = detail
  }
}

async function raise(r: Response): Promise<never> {
  let detail: unknown = r.statusText
  try {
    detail = (await r.json()).detail ?? detail
  } catch { /* 非 JSON 错误体 */ }
  throw new ApiError(r.status, detail, r.statusText)
}

// multipart 上传：绝不手动设 Content-Type，让浏览器自带 boundary
async function httpUpload<T>(path: string, form: FormData, method = "POST"): Promise<T> {
  const r = await fetch(path, { method, body: form })
  if (!r.ok) return raise(r)
  return r.json() as Promise<T>
}

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  })
  if (!r.ok) return raise(r)
  return r.json() as Promise<T>
}

export const api = {
  // 项目
  listProjects: () => http<ProjectMeta[]>("/api/projects"),
  createProject: (name: string, track: string, capabilities: string[] = []) =>
    http<ProjectMeta>("/api/projects", {
      method: "POST", body: JSON.stringify({ name, track, capabilities }),
    }),
  taxonomy: () => http<Taxonomy>("/api/taxonomy"),
  getProject: (pid: string) => http<ProjectDetail>(`/api/projects/${pid}`),
  patchProjectConfig: (pid: string, config: Record<string, unknown>) =>
    http<ProjectMeta>(`/api/projects/${pid}/config`, {
      method: "PATCH",
      body: JSON.stringify({ config }),
    }),
  deleteProject: (pid: string) =>
    http<{ status: string; trash_path: string }>(`/api/projects/${pid}`, { method: "DELETE" }),
  listRoles: (pid: string) => http<RoleInfo[]>(`/api/projects/${pid}/roles`),

  // 黑板（读）
  artifactContent: (pid: string, ref: string) =>
    http<{ id: string | null; path: string; kind: string; sha256: string; content: string }>(
      `/api/projects/${pid}/artifacts/content?ref=${encodeURIComponent(ref)}`),
  findings: (pid: string, opts?: { target_asset_id?: string; min_severity?: string; verified_only?: boolean }) => {
    const q = new URLSearchParams()
    if (opts?.target_asset_id) q.set("target_asset_id", opts.target_asset_id)
    if (opts?.min_severity) q.set("min_severity", opts.min_severity)
    if (opts?.verified_only) q.set("verified_only", "true")
    const qs = q.toString()
    return http<Finding[]>(`/api/projects/${pid}/findings${qs ? `?${qs}` : ""}`)
  },
  assets: (pid: string) => http<Asset[]>(`/api/projects/${pid}/assets`),
  funcs: (pid: string, sha?: string) =>
    http<FuncEntry[]>(`/api/projects/${pid}/funcs${sha ? `?binary_sha256=${sha}` : ""}`),
  events: (pid: string, sinceId = 0) =>
    http<BBEvent[]>(`/api/projects/${pid}/events?since_id=${sinceId}`),
  sessions: (pid: string) => http<Session[]>(`/api/projects/${pid}/sessions`),

  // 黑板（人机共写，author=human）
  addFinding: (pid: string, body: Partial<Finding> & { title: string }) =>
    http<Finding>(`/api/projects/${pid}/findings`, {
      method: "POST", body: JSON.stringify(body),
    }),
  addAsset: (pid: string, type: string, value: string, meta: Record<string, unknown> = {}) =>
    http<Asset>(`/api/projects/${pid}/assets`, {
      method: "POST", body: JSON.stringify({ type, value, meta }),
    }),

  // ---------- 逆向工作台（样本 / headless 缓存 / 人机共写） ----------
  uploadSample: (pid: string, file: File) => {
    const form = new FormData()
    form.append("file", file)
    return httpUpload<SampleUploadResponse>(`/api/projects/${pid}/samples`, form)
  },
  retryTriage: (pid: string, sha: string) =>
    http<{ job_id: string; sha: string }>(`/api/projects/${pid}/binaries/${sha}/triage`, { method: "POST" }),
  binaryOverview: (pid: string, sha: string) =>
    http<BinaryOverview>(`/api/projects/${pid}/binaries/${sha}/overview`),
  cachedFunctions: (pid: string, sha: string) =>
    http<CachedFuncRow[]>(`/api/projects/${pid}/binaries/${sha}/functions`),
  cachedFunction: (pid: string, sha: string, addr: string) =>
    http<CachedFunction>(`/api/projects/${pid}/binaries/${sha}/functions/${addr.replace(/^0x/, "")}`),
  binaryXrefs: (pid: string, sha: string, addr: string) =>
    http<XrefData>(`/api/projects/${pid}/binaries/${sha}/xrefs/${addr.replace(/^0x/, "")}`),
  binaryStrings: (pid: string, sha: string, q?: string) =>
    http<BinaryStrings>(
      `/api/projects/${pid}/binaries/${sha}/strings${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  openInIda: (pid: string, sha: string, addr?: string) =>
    http<{ opening: string; jumping: string | null }>(
      `/api/projects/${pid}/binaries/${sha}/open${addr ? `?addr=${encodeURIComponent(addr)}` : ""}`,
      { method: "POST" }),
  writeback: (pid: string, sha: string, items: WritebackItem[]) =>
    http<{ job_id: string; sha: string }>(
      `/api/projects/${pid}/binaries/${sha}/writeback`,
      { method: "POST", body: JSON.stringify({ items }) }),
  pullNames: (pid: string, sha: string) =>
    http<{ job_id: string; sha: string }>(
      `/api/projects/${pid}/binaries/${sha}/pull-names`, { method: "POST" }),
  createFunc: (pid: string, body: FuncCreateBody) =>
    http<FuncEntry>(`/api/projects/${pid}/funcs`, {
      method: "POST", body: JSON.stringify(body),
    }),
  patchFunc: (pid: string, funcId: string, body: FuncPatchBody) =>
    http<FuncEntry>(`/api/projects/${pid}/funcs/${funcId}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  patchFinding: (pid: string, findingId: string, body: FindingPatchBody) =>
    http<Finding>(`/api/projects/${pid}/findings/${findingId}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  uploadDebugLog: (pid: string, file: File) => {
    const form = new FormData()
    form.append("kind", "debug-log")
    form.append("file", file)
    return httpUpload<ArtifactUploadResponse>(`/api/projects/${pid}/artifacts/upload`, form)
  },

  // 攻击链（人工建链）
  chains: (pid: string) => http<ChainSummary[]>(`/api/projects/${pid}/chains`),
  createChain: (pid: string, body: { name: string; goal?: string }) =>
    http<Chain>(`/api/projects/${pid}/chains`, { method: "POST", body: JSON.stringify(body) }),
  chain: (pid: string, cid: string) =>
    http<Chain>(`/api/projects/${pid}/chains/${cid}`),
  updateChain: (pid: string, cid: string, body: { name?: string; goal?: string; status?: ChainStatus }) =>
    http<Chain>(`/api/projects/${pid}/chains/${cid}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  deleteChain: (pid: string, cid: string) =>
    http<{ deleted: string }>(`/api/projects/${pid}/chains/${cid}`, { method: "DELETE" }),
  addChainLink: (
    pid: string, cid: string,
    body: { node_type: ChainNodeType; node_id: string; edge_note?: string },
  ) =>
    http<ChainLink>(`/api/projects/${pid}/chains/${cid}/links`, {
      method: "POST", body: JSON.stringify(body),
    }),
  updateLinkNote: (pid: string, lid: string, edge_note: string) =>
    http<ChainLink>(`/api/projects/${pid}/chains/links/${lid}`, {
      method: "PATCH", body: JSON.stringify({ edge_note }),
    }),
  deleteChainLink: (pid: string, lid: string) =>
    http<{ chain_id: string; link_id: string }>(
      `/api/projects/${pid}/chains/links/${lid}`, { method: "DELETE" }),
  artifacts: (pid: string) => http<Artifact[]>(`/api/projects/${pid}/artifacts`),

  // 任务
  tasks: (pid: string) => http<Task[]>(`/api/projects/${pid}/tasks`),
  taskGraph: (pid: string) =>
    http<TaskGraph>(`/api/projects/${pid}/task-graph`),
  publishTask: (pid: string, body: {
    objective: string; scope?: string; task_type?: string; noise_budget?: string;
    priority?: number; conflict_keys?: string[]; refs?: string[];
    workset?: string[]; force?: boolean
  }) =>
    http<{ task_id: string; kicked: string[]; deduplicated?: boolean; existed_status?: string }>(
      `/api/projects/${pid}/tasks`, { method: "POST", body: JSON.stringify(body) }),
  updateTask: (taskId: string, body: {
    objective?: string; task_type?: string; noise_budget?: string;
    priority?: number; conflict_keys?: string[]
  }) =>
    http<Task>(`/api/tasks/${taskId}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  reopenTask: (taskId: string, note?: string) =>
    http<{ status: string }>(`/api/tasks/${taskId}/reopen`, {
      method: "POST", body: JSON.stringify({ note: note ?? "" }),
    }),
  // E12 意外终止续跑（限原会话）：reopen + 原会话载落盘快照 + 提交 worker
  resumeTask: (taskId: string) =>
    http<{ task_id: string; session_id: string; status: string }>(
      `/api/tasks/${taskId}/resume`, { method: "POST" }),
  deleteTask: (taskId: string) =>
    http<{ deleted: string }>(`/api/tasks/${taskId}`, { method: "DELETE" }),
  closeSession: (sid: string) =>
    http<{ status: string }>(`/api/sessions/${sid}/close`, { method: "POST" }),
  pauseSession: (sid: string) =>
    http<{ status: string }>(`/api/sessions/${sid}/pause`, { method: "POST" }),
  // E8：恢复可附引导语（随快照注入）与增补步数；预算暂停缺省自动 +200
  resumeSession: (sid: string, body?: { note?: string; extra_steps?: number }) =>
    http<{ status: string }>(`/api/sessions/${sid}/resume`, {
      method: "POST", body: body ? JSON.stringify(body) : undefined,
    }),
  abortSession: (sid: string) =>
    http<{ status: string }>(`/api/sessions/${sid}/abort`, { method: "POST" }),
  // E8 人工引导通道：human_note 私信直达会话，worker 步边界注入
  sessionNote: (sid: string, text: string) =>
    http<{ note_id: string; session_id: string }>(`/api/sessions/${sid}/note`, {
      method: "POST", body: JSON.stringify({ text }),
    }),
  sessionInbox: (sid: string, unread = false) =>
    http<InboxMessage[]>(`/api/sessions/${sid}/inbox${unread ? "?unread=true" : ""}`),
  readSessionInbox: (sid: string, ids?: string[]) =>
    http<{ marked: number }>(`/api/sessions/${sid}/inbox/read`, {
      method: "POST", body: JSON.stringify({ ids: ids ?? null }),
    }),

  // Agent / 编排
  models: () => http<ModelInfo>("/api/models"),
  spawnAgent: (pid: string, role: string, sessionName?: string, model?: string,
               provider?: string, maxSteps?: number) =>
    http<Session & { warning?: string; job_id?: string }>(`/api/projects/${pid}/agents`, {
      method: "POST",
      body: JSON.stringify({
        role, session_name: sessionName,
        model: model || undefined, provider: provider || undefined,
        max_steps: maxSteps || undefined,  // E8：开窗可调步数预算（缺省 200）
      }),
    }),
  switchAgentLlm: (sid: string, provider: string, model?: string) =>
    http<{ status: string; provider: string; model: string }>(`/api/agents/${sid}/llm`, {
      method: "POST", body: JSON.stringify({ provider, model: model || undefined }),
    }),
  agentWork: (sid: string) =>
    http<{ job_id: string }>(`/api/agents/${sid}/work`, { method: "POST" }),
  orchTick: (pid: string, opts: { allowed_roles?: string[]; max_sessions?: number } = {}) =>
    http<{ job_id: string }>(`/api/projects/${pid}/orchestrator/tick`, {
      method: "POST", body: JSON.stringify(opts),
    }),
  // A5：手动重排 open 任务优先级（任何自主档可用，不受 30s 去抖/预算闸限制）
  replanPriorities: (pid: string) =>
    http<{ job_id: string }>(`/api/projects/${pid}/orchestrator/replan-priorities`, {
      method: "POST",
    }),
  job: (jobId: string) => http<Job>(`/api/jobs/${jobId}`),

  // 审批
  approvals: (pid: string, status?: string) =>
    http<Approval[]>(
      `/api/projects/${pid}/approvals${status ? `?status=${status}` : ""}`),
  decideApproval: (aid: string, decision: "approved" | "rejected") =>
    http<DecideApprovalResult>(`/api/approvals/${aid}/decide`, {
      method: "POST", body: JSON.stringify({ decision }),
    }),

  // packs 管理（正交分类学 §4.5：角色/owners/任务类型属轨，Skill/红线轨与包各有一份）
  // 场景轨
  trackRoles: (track: string) => http<PackRole[]>(`/api/tracks/${track}/roles`),
  createTrackRole: (track: string, body: RoleCreateBody) =>
    http<{ status: string; file: string; cloned: string | null }>(
      `/api/tracks/${track}/roles`, { method: "POST", body: JSON.stringify(body) }),
  updateTrackRole: (track: string, name: string, body: RoleUpdateBody) =>
    http<{ status: string; file: string }>(`/api/tracks/${track}/roles/${name}`, {
      method: "PUT", body: JSON.stringify(body),
    }),
  deleteTrackRole: (track: string, name: string) =>
    http<{ status: string; file: string; trash: string }>(
      `/api/tracks/${track}/roles/${name}`, { method: "DELETE" }),
  trackSkills: (track: string) => http<SkillDef[]>(`/api/tracks/${track}/skills`),
  createTrackSkill: (track: string, body: SkillCreateBody) =>
    http<{ status: string; name: string; path: string }>(`/api/tracks/${track}/skills`, {
      method: "POST", body: JSON.stringify(body),
    }),
  trackSkillDetail: (track: string, name: string) =>
    http<SkillDetail>(`/api/tracks/${track}/skills/${name}`),
  updateTrackSkill: (track: string, name: string, content: string) =>
    http<{ status: string }>(`/api/tracks/${track}/skills/${name}`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  deleteTrackSkill: (track: string, name: string) =>
    http<{ status: string; name: string; trash: string }>(
      `/api/tracks/${track}/skills/${name}`, { method: "DELETE" }),
  setTrackSkillEnabled: (track: string, name: string, enabled: boolean) =>
    http<{ status: string; enabled: boolean }>(
      `/api/tracks/${track}/skills/${name}/enabled`, {
        method: "PATCH", body: JSON.stringify({ enabled }),
      }),
  trackRules: (track: string) => http<{ content: string; exists?: boolean }>(`/api/tracks/${track}/rules`),
  updateTrackRules: (track: string, content: string) =>
    http<{ status: string }>(`/api/tracks/${track}/rules`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  trackOwners: (track: string) => http<OwnerRule[]>(`/api/tracks/${track}/owners`),
  updateTrackOwner: (track: string, tag: string, content: string) =>
    http<{ status: string; tag: string }>(`/api/tracks/${track}/owners/${encodeURIComponent(tag)}`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  deleteTrackOwner: (track: string, tag: string) =>
    http<{ status: string; tag: string }>(`/api/tracks/${track}/owners/${encodeURIComponent(tag)}`, {
      method: "DELETE",
    }),
  // 能力包
  capSkills: (cap: string) => http<SkillDef[]>(`/api/capabilities/${cap}/skills`),
  createCapSkill: (cap: string, body: SkillCreateBody) =>
    http<{ status: string; name: string; path: string }>(`/api/capabilities/${cap}/skills`, {
      method: "POST", body: JSON.stringify(body),
    }),
  capSkillDetail: (cap: string, name: string) =>
    http<SkillDetail>(`/api/capabilities/${cap}/skills/${name}`),
  updateCapSkill: (cap: string, name: string, content: string) =>
    http<{ status: string }>(`/api/capabilities/${cap}/skills/${name}`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  deleteCapSkill: (cap: string, name: string) =>
    http<{ status: string; name: string; trash: string }>(
      `/api/capabilities/${cap}/skills/${name}`, { method: "DELETE" }),
  setCapSkillEnabled: (cap: string, name: string, enabled: boolean) =>
    http<{ status: string; enabled: boolean }>(
      `/api/capabilities/${cap}/skills/${name}/enabled`, {
        method: "PATCH", body: JSON.stringify({ enabled }),
      }),
  capRules: (cap: string) => http<{ content: string; exists?: boolean }>(`/api/capabilities/${cap}/rules`),
  updateCapRules: (cap: string, content: string) =>
    http<{ status: string }>(`/api/capabilities/${cap}/rules`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  routePreview: (body: RoutePreviewBody) =>
    http<RouteHit[]>("/api/skills/route-preview", {
      method: "POST", body: JSON.stringify(body),
    }),
  skillsVocab: () => http<SkillVocab>("/api/skills/vocab"),

  // kb 本地基线（C2/C3：源树/读写/改名联动/版本/引用）
  kbList: (cap: string) =>
    http<{ cap: string; sources: KbSourceTree[] }>(`/api/capabilities/${cap}/kb`),
  /** kb 正文搜索（大小写不敏感 substring，按命中次数降序） */
  kbSearch: (cap: string, q: string, limit = 50) =>
    http<{ cap: string; q: string; results: KbSearchHit[] }>(
      `/api/capabilities/${cap}/kb/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  kbRead: (cap: string, path: string) =>
    http<KbRead>(`/api/capabilities/${cap}/kb/file?path=${encodeURIComponent(path)}`),
  kbCreate: (cap: string, path: string, content: string) =>
    http<KbWriteResult>(`/api/capabilities/${cap}/kb/file`, {
      method: "POST", body: JSON.stringify({ path, content }),
    }),
  kbUpdate: (cap: string, path: string, content: string) =>
    http<KbWriteResult>(`/api/capabilities/${cap}/kb/file`, {
      method: "PUT", body: JSON.stringify({ path, content }),
    }),
  /** 409 detail 为 {message,refs}（ApiError.data）；force=true 放行 */
  kbDelete: (cap: string, path: string, force = false) =>
    http<KbWriteResult & { refs: KbRefHit[] }>(
      `/api/capabilities/${cap}/kb/file?path=${encodeURIComponent(path)}${force ? "&force=true" : ""}`,
      { method: "DELETE" }),
  kbRename: (cap: string, path: string, newPath: string) =>
    http<KbRenameResult>(`/api/capabilities/${cap}/kb/rename`, {
      method: "POST", body: JSON.stringify({ path, new_path: newPath }),
    }),
  kbVersions: (cap: string, path: string) =>
    http<{ path: string; versions: HistoryList["versions"] }>(
      `/api/capabilities/${cap}/kb/versions?path=${encodeURIComponent(path)}`),
  kbDiff: (cap: string, path: string, version: string) =>
    http<{ path: string; version: string; diff: string }>(
      `/api/capabilities/${cap}/kb/diff?path=${encodeURIComponent(path)}&version=${encodeURIComponent(version)}`),
  kbRollback: (cap: string, path: string, version: string) =>
    http<{ status: string; path: string; rolled_back: string; current_backed_up: string | null }>(
      `/api/capabilities/${cap}/kb/rollback?path=${encodeURIComponent(path)}&version=${encodeURIComponent(version)}`,
      { method: "POST" }),
  kbRefs: (cap: string, path: string) =>
    http<{ path: string; count: number; refs: KbRefHit[] }>(
      `/api/capabilities/${cap}/kb/refs?path=${encodeURIComponent(path)}`),

  // 统一变更提案（C4）
  createProposal: (body: {
    target: Proposal["target"]; mode: Proposal["mode"]; content?: string | null
    summary: string; reason: string; origin?: ProposalOrigin
    project?: string | null; session?: string | null; task?: string | null; evidence?: string
  }) =>
    http<Proposal>("/api/proposals", { method: "POST", body: JSON.stringify(body) }),
  proposals: (status?: "pending" | "approved" | "rejected") =>
    http<Proposal[]>(`/api/proposals${status ? `?status=${status}` : ""}`),
  proposal: (id: string) => http<Proposal>(`/api/proposals/${id}`),
  applyProposal: (id: string, decidedBy: "human" | "demo-script(auto)" = "human") =>
    http<{ id: string; result: unknown }>(`/api/proposals/${id}/apply`, {
      method: "POST", body: JSON.stringify({ decided_by: decidedBy }),
    }),
  rejectProposal: (id: string, note = "") =>
    http<{ id: string; status: string }>(`/api/proposals/${id}/reject`, {
      method: "POST", body: JSON.stringify({ decided_by: "human", note }),
    }),
  reviseProposal: (id: string, changes: {
    content?: string; summary?: string; reason?: string; new_path?: string
  }, note = "") =>
    http<Proposal>(`/api/proposals/${id}/revise`, {
      method: "POST", body: JSON.stringify({ by: "human", changes, note }),
    }),
  /** 复盘沉淀后台 Job（planner_llm 复盘会话产 pending 提案；无 key 503） */
  reviewProposals: (pid: string) =>
    http<{ job_id: string }>(`/api/projects/${pid}/review-proposals`, { method: "POST" }),
  // pack doctor + .history 版本管理（file = packs 内相对路径）
  packsDoctor: () => http<DoctorReport>("/api/packs/doctor"),
  historyList: (file: string) =>
    http<HistoryList>(`/api/packs/history?file=${encodeURIComponent(file)}`),
  historyDiff: (file: string, version: string) =>
    http<{ file: string; version: string; diff: string }>(
      `/api/packs/history/diff?file=${encodeURIComponent(file)}&version=${encodeURIComponent(version)}`),
  historyRollback: (file: string, version: string) =>
    http<{ status: string; rolled_back: string; current_backed_up: string | null }>(
      `/api/packs/history/rollback?file=${encodeURIComponent(file)}&version=${encodeURIComponent(version)}`,
      { method: "POST" }),
  mcpServers: () => http<{ servers: McpServer[] }>("/api/mcp"),
  updateMcpServers: (servers: McpServer[]) =>
    http<{ status: string; count: number }>("/api/mcp", {
      method: "PUT", body: JSON.stringify({ servers }),
    }),

  // LLM 供应商（DESIGN.md §8）
  llmProviders: () =>
    http<{ providers: LlmProvider[]; default: { provider: string; model: string } | null }>(
      "/api/llm/providers"),
  saveLlmProviders: (providers: LlmProvider[]) =>
    http<{ providers: LlmProvider[]; default: { provider: string; model: string } | null }>(
      "/api/llm/providers", {
        method: "PUT", body: JSON.stringify({ providers }),
      }),
  discoverLlm: (body: { name?: string; base_url?: string; api_key?: string }) =>
    http<{ listed: boolean; models: DiscoveredModel[]; probed?: string[] }>(
      "/api/llm/discover", { method: "POST", body: JSON.stringify(body) }),
  testLlmModel: (body: { name?: string; base_url?: string; api_key?: string; model: string }) =>
    http<{ ok: boolean; error?: string }>("/api/llm/test-model", {
      method: "POST", body: JSON.stringify(body),
    }),

  // 情报面板（E9，全局模块；与项目无关）
  intelOverview: () =>
    http<IntelOverview>("/api/intel/overview"),
  intelFeeds: () =>
    http<{ feeds: IntelFeed[] }>("/api/intel/feeds"),
  intelUpdateFeeds: (feeds: { name: string; url: string }[]) =>
    http<{ status: string; feeds: IntelFeed[] }>("/api/intel/feeds", {
      method: "PUT", body: JSON.stringify({ feeds }),
    }),
  intelProfile: () =>
    http<IntelProfile>("/api/intel/profile"),
  intelUpdateProfile: (profile: IntelProfile) =>
    http<{ status: string; profile: IntelProfile }>("/api/intel/profile", {
      method: "PUT", body: JSON.stringify(profile),
    }),
  intelFetch: () =>
    http<{ job_id: string }>("/api/intel/fetch", { method: "POST" }),
  intelBriefs: () =>
    http<{ briefs: IntelBriefMeta[] }>("/api/intel/briefs"),
  intelBrief: (date: string) =>
    http<IntelBrief>(`/api/intel/briefs/${date}`),
  intelArticles: (params?: { kind?: string; unread?: boolean; starred?: boolean; limit?: number }) => {
    const q = new URLSearchParams()
    if (params?.kind) q.set("kind", params.kind)
    if (params?.unread) q.set("unread", "true")
    if (params?.starred) q.set("starred", "true")
    if (params?.limit) q.set("limit", String(params.limit))
    const qs = q.toString()
    return http<{ articles: IntelArticle[] }>(`/api/intel/articles${qs ? `?${qs}` : ""}`)
  },
  intelMarkArticle: (id: string, patch: { read?: boolean; starred?: boolean }) =>
    http<IntelArticle>(`/api/intel/articles/${id}`, {
      method: "PATCH", body: JSON.stringify(patch),
    }),
  // E10：vault 接入 + 学习档案 + 周计划
  intelVault: () =>
    http<IntelVaultInfo>("/api/intel/vault"),
  intelUpdateVault: (vault: IntelVaultConfig) =>
    http<{ status: string; vault: IntelVaultConfig; index_job_id: string | null }>(
      "/api/intel/vault", { method: "PUT", body: JSON.stringify(vault) }),
  intelVaultIndex: () =>
    http<{ job_id: string }>("/api/intel/vault/index", { method: "POST" }),
  intelVaultTree: () =>
    http<{ configured: boolean; tree: VaultNode[] }>("/api/intel/vault/tree"),
  intelVaultSearch: (q: string) =>
    http<{ hits: VaultSearchHit[] }>(`/api/intel/vault/search?q=${encodeURIComponent(q)}`),
  intelLearningProfile: () =>
    http<IntelLearningProfile>("/api/intel/learning/profile"),
  intelLearningPlanGenerate: () =>
    http<{ job_id: string; week: string }>("/api/intel/learning/plan", { method: "POST" }),
  intelLearningPlan: (week?: string) =>
    http<IntelPlan>(`/api/intel/learning/plan${week ? `?week=${encodeURIComponent(week)}` : ""}`),
  intelLearningPlans: () =>
    http<{ plans: IntelPlanMeta[] }>("/api/intel/learning/plans"),
}

// 长耗时 Job 轮询（Agent work / orchestrator tick）
export async function pollJob(
  jobId: string,
  onUpdate: (job: Job) => void,
  intervalMs = 1500,
): Promise<Job> {
  for (;;) {
    const job = await api.job(jobId)
    onUpdate(job)
    if (job.status !== "running") return job
    await new Promise((res) => setTimeout(res, intervalMs))
  }
}

export function wsUrl(pid: string, sinceId: number): string {
  const proto = location.protocol === "https:" ? "wss" : "ws"
  return `${proto}://${location.host}/api/ws/projects/${pid}?since_id=${sinceId}`
}
