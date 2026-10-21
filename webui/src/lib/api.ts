import type {
  Approval, Artifact, ArtifactUploadResponse, Asset, AttachmentInfo, BBEvent, BinaryOverview, BinaryStrings,
  BrowserState, BrowserStatus, BrowserSessionInfo, WorkspaceTreeResponse, HttpHistoryRow, InterceptState,
  Blueprint, BlueprintModuleStatus, BlueprintNodeStatus, BlueprintStatus,
  FeatureTree,
  IntruderPayloadSpec, IntruderTemplate, ReplayOptions, GmStatus,
  ProxyRecord, ProxyStatus, ProxyListResponse, ProxyConfigView,
  CachedFuncRow, CachedFunction, Chain, ChainLink, ChainNodeType, ChainStatus, ChainSummary,
  LogicBlock, LogicBlockSummary,
  DecideApprovalResult, DoctorReport, DiscoveredModel, Finding, FindingPatchBody, FuncCreateBody, FuncEntry,
  FuncPatchBody, HistoryList, InboxMessage, Job, KbRead, KbRefHit, KbRenameResult,
  KbSearchHit, KbSourceTree,
  KbWriteResult, LlmProvider, McpServer, ModelInfo, ExecutorLlmView,
  IntelArticle, IntelBrief, IntelBriefMeta,
  IntelFeed, IntelOverview, IntelProfile, IntelVaultConfig, IntelVaultInfo, VaultNode,
  VaultSearchHit, IntelLearningProfile, IntelPlan, IntelPlanMeta,
  Expert, ExpertBody, GatewayConfig, CapabilityInventory, OrchPersona, OwnerRule, PackRole, PhaseGoal, PhaseInfo, ProjectDetail, ProjectMeta, RatingRule,
  AgentToolsResponse,
  Proposal, ProposalOrigin, RoleInfo, RouteHit,
  KbIterationRules,
  RoutePreviewBody, Session, SkillCreateBody, SkillDef, TrackProfile,
  SkillDetail, SkillVocab, SkillCatalog, SkillFileEntry, SessionGraph, AttackPath, IntentInfo,
  IntentGraph,
  WritebackItem, XrefData,
  TraceEffect,
  FofaConfig, FofaTestResult, FofaSearchResult, FofaHistoryItem, ImportPreview, ImportSummary,
  FeishuConfig, FeishuTestResult,
  ChatAgent, ChatThread, ChatThreadDetail, ChatMcpServer, ContextUsage,
  DiagFailures, DiagSessions, DiagSystemPrompt,
  DiagLlmDump, DiagRequestMeta, DiagRequestDetail, DiagRedlineTest,
  DiagSkillUsage,
  Team, TeamMemberInput, TeamPreflight, TeamRun,
  SkillResource, ArtexPlanResult, ArtexTaskGraph, ArtexIntent, ArtexEngineStatus, ArtexActivityResponse,
  SamplePackage, SampleTargetAnalysis, SampleTargetAnalyzeResponse, SampleTargetRegisterResponse, SamplePackageUploadSession, SamplePackagePreview,
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
  // 204 No Content：空 body 无 JSON 可解——曾致删除/停止会话线程
  // （后端 204 端点）前端报 Unexpected end of JSON input
  if (r.status === 204) return undefined as T
  return r.json() as Promise<T>
}

export const api = {
  // 项目
  listProjects: () => http<ProjectMeta[]>("/api/projects"),
  // expert-pool M3：capabilities 多选退役，改专家组队（profile=场景档、inherit_from=知识继承源，M4）
  createProject: (name: string, track: string, experts: string[] = [],
                  extra?: { profile?: string | null; inherit_from?: string | null }) =>
    http<ProjectMeta>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, track, experts, ...extra }),
    }),
  taxonomy: () => http<Taxonomy>("/api/taxonomy"),
  getProject: (pid: string) => http<ProjectDetail>(`/api/projects/${pid}`),
  patchProjectConfig: (pid: string, config: Record<string, unknown>) =>
    http<ProjectMeta>(`/api/projects/${pid}/config`, {
      method: "PATCH",
      body: JSON.stringify({ config }),
    }),
  // 换将（M2）：PATCH experts，空清单=剥键恢复存量直通
  patchProjectExperts: (pid: string, experts: string[]) =>
    http<ProjectMeta>(`/api/projects/${pid}/experts`, {
      method: "PATCH", body: JSON.stringify({ experts }),
    }),
  deleteProject: (pid: string) =>
    http<{ status: string; trash_path: string }>(`/api/projects/${pid}`, { method: "DELETE" }),
  // 回收站（DESIGN.md §5.3）：列出已删项目 / 一键恢复（同名后端自动加后缀）
  listTrashedProjects: () => http<ProjectMeta[]>("/api/projects/trash"),
  restoreProject: (trashName: string) =>
    http<{ project_id: string | null; slug: string; status: string }>(
      `/api/projects/${encodeURIComponent(trashName)}/restore`, { method: "POST" }),
  // 回收站终点（2026-10-18）：永久删除（物理抹除）/ 清除项目内文件仅留名字墓碑
  purgeTrashedProject: (trashName: string) =>
    http<{ status: string; trash_name: string }>(
      `/api/projects/trash/${encodeURIComponent(trashName)}`, { method: "DELETE" }),
  clearTrashedProject: (trashName: string) =>
    http<{ status: string; trash_name: string; removed_entries: number;
            freed_bytes: number; kept: string[]; purged_at: string }>(
      `/api/projects/trash/${encodeURIComponent(trashName)}/clear`, { method: "POST" }),
  listRoles: (pid: string) => http<RoleInfo[]>(`/api/projects/${pid}/roles`),

  // 黑板（读）
  artifactContent: (pid: string, ref: string) =>
    http<{ id: string | null; path: string; kind: string; sha256: string; content: string }>(
      `/api/projects/${pid}/artifacts/content?ref=${encodeURIComponent(ref)}`),
  findings: (pid: string, opts?: { target_asset_id?: string; min_severity?: string; verified_only?: boolean; category?: string }) => {
    const q = new URLSearchParams()
    if (opts?.target_asset_id) q.set("target_asset_id", opts.target_asset_id)
    if (opts?.min_severity) q.set("min_severity", opts.min_severity)
    if (opts?.verified_only) q.set("verified_only", "true")
    if (opts?.category) q.set("category", opts.category)
    const qs = q.toString()
    return http<Finding[]>(`/api/projects/${pid}/findings${qs ? `?${qs}` : ""}`)
  },
  assets: (pid: string, filter?: { tag?: string }) => {
    const q = new URLSearchParams()
    if (filter?.tag) q.set("tag", filter.tag)
    const qs = q.toString()
    return http<Asset[]>(`/api/projects/${pid}/assets${qs ? `?${qs}` : ""}`)
  },
  patchAsset: (_pid: string, assetId: string, body: { meta?: Record<string, unknown>; parent_id?: string | null }) =>
    http<Asset>(`/api/assets/${assetId}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  // 物理删除叶子资产（有子资产 → 409，文案直接展示）。cascade=true（Web UI 默认）时
  // 把 target_asset_id 指向该资产的发现一并删除，响应多带 findings_deleted。
  deleteAsset: (pid: string, assetId: string, cascade = false) =>
    http<{ deleted: string; id: string; findings_deleted?: string[] }>(
      `/api/projects/${pid}/assets/${assetId}${cascade ? "?cascade=true" : ""}`, {
        method: "DELETE",
      }),
  funcs: (pid: string, sha?: string) =>
    http<FuncEntry[]>(`/api/projects/${pid}/funcs${sha ? `?binary_sha256=${sha}` : ""}`),
  events: (pid: string, sinceId = 0) =>
    http<BBEvent[]>(`/api/projects/${pid}/events?since_id=${sinceId}`),
  // 直播间分页（2026-09-17）：tail=最新 N 条（首屏不全量回放）；before_id=更早一页（升序）；
  // sid 可选=会话维度分页（2026-09-23 直播间会话窗口）
  eventsTail: (pid: string, limit = 50, sid?: string) =>
    http<BBEvent[]>(`/api/projects/${pid}/events?tail=${limit}${sid ? `&session_id=${encodeURIComponent(sid)}` : ""}`),
  eventsBefore: (pid: string, beforeId: number, limit = 50, sid?: string) =>
    http<BBEvent[]>(`/api/projects/${pid}/events?before_id=${beforeId}&limit=${limit}${sid ? `&session_id=${encodeURIComponent(sid)}` : ""}`),
  // 按类型全量拉取（2026-09-26）：编排器对话历史用——首屏只水合尾部 300 条，
  // 长跑项目 orch.chat 落窗外，须按 kind 单独拉全
  eventsByKind: (pid: string, kinds: string, limit = 2000) =>
    http<BBEvent[]>(`/api/projects/${pid}/events?kinds=${encodeURIComponent(kinds)}&limit=${limit}`),
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

  // ---------- CTF 题目（challenge-asset v38 / 题目表单 v47，2026-10-19） ----------
  challenges: (pid: string) => http<import("./types").ChallengeSummary[]>(`/api/projects/${pid}/challenges`),
  /** 建题：name 可空（空名按题面/入口内容指纹生成稳定 value，AI 之后改 title 只动展示层）。 */
  addChallenge: (pid: string, body: import("./types").ChallengeCreateBody) =>
    http<import("./types").ChallengeSummary>(`/api/projects/${pid}/challenges`, {
      method: "POST", body: JSON.stringify(body),
    }),
  patchChallenge: (assetId: string, body: import("./types").ChallengePatchBody) =>
    http<import("./types").ChallengeSummary>(`/api/challenges/${assetId}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  // ---------- CTF flag 台账（challenge-flag-ledger v47） ----------
  challengeFlags: (assetId: string) =>
    http<import("./types").ChallengeFlagsResponse>(`/api/challenges/${assetId}/flags`),
  addChallengeFlag: (assetId: string, body: { flag: string; source?: string; verdict?: string }) =>
    http<import("./types").ChallengeFlag>(`/api/challenges/${assetId}/flags`, {
      method: "POST", body: JSON.stringify(body),
    }),
  /** 人工裁决：accepted（确认）/ rejected（**标记为错误**）/ pending。 */
  decideChallengeFlag: (assetId: string, flagId: string, body: { verdict: string; reason?: string }) =>
    http<import("./types").ChallengeFlag>(`/api/challenges/${assetId}/flags/${flagId}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  abandonChallenge: (assetId: string, abandoned = true) =>
    http<import("./types").ChallengeSummary>(`/api/challenges/${assetId}/abandon`, {
      method: "POST", body: JSON.stringify({ abandoned }),
    }),
  startCtfAgent: (assetId: string, body: { provider?: string; model?: string } = {}) =>
    http<{ job_id: string; no_submit: boolean; provider: string; model: string }>(
      `/api/challenges/${assetId}/ctf-agent`, { method: "POST", body: JSON.stringify(body) }),
  ctfAgentJob: (jobId: string) => http<Job>(`/api/jobs/${jobId}`),

  // ---------- 网络空间测绘（cyberspace-mapping M1+M2） ----------
  fofaConfig: () => http<FofaConfig>("/api/fofa/config"),
  saveFofaConfig: (body: { base_url?: string; key?: string }) =>
    http<FofaConfig>("/api/fofa/config", { method: "PUT", body: JSON.stringify(body) }),
  // info_my 免费；ok=false 时 error 文案可直接展示
  fofaTest: () => http<FofaTestResult>("/api/fofa/test", { method: "POST" }),
  // 消耗等量配额：size 由调用方显式选择并提示；成功后服务端落查询历史
  fofaSearch: (pid: string, query: string, size: number, page = 1) =>
    http<FofaSearchResult>(`/api/projects/${pid}/fofa/search`, {
      method: "POST", body: JSON.stringify({ query, size, page }),
    }),
  // 查询历史：列表轻量（不含 rows）；单条全量恢复（rows 现算 existing）；删除=删结果文件
  fofaHistory: (pid: string) =>
    http<{ items: FofaHistoryItem[] }>(`/api/projects/${pid}/fofa/history`),
  fofaHistoryGet: (pid: string, hid: string) =>
    http<FofaSearchResult & FofaHistoryItem>(`/api/projects/${pid}/fofa/history/${hid}`),
  fofaHistoryDelete: (pid: string, hid: string) =>
    http<{ deleted: string }>(`/api/projects/${pid}/fofa/history/${hid}`, { method: "DELETE" }),
  fofaHistoryClear: (pid: string) =>
    http<{ cleared: boolean }>(`/api/projects/${pid}/fofa/history`, { method: "DELETE" }),

  // ---------- 飞书连接器（feishu-push v1：确认的中危及以上漏洞自动推送） ----------
  feishuConfig: () => http<FeishuConfig>("/api/integrations/feishu"),
  // 半幅更新：webhook_url 含 * / secret 空串 = 不修改（防脱敏回显误覆盖）
  feishuSaveConfig: (body: Record<string, unknown>) =>
    http<FeishuConfig>("/api/integrations/feishu", { method: "PUT", body: JSON.stringify(body) }),
  // 测试连接：同步发测试卡；ok=false 时 detail 可直接展示
  feishuTest: () => http<FeishuTestResult>("/api/integrations/feishu/test", { method: "POST" }),
  assetImportPreview: (pid: string, file: File) => {
    const form = new FormData()
    form.append("file", file)
    return httpUpload<ImportPreview>(`/api/projects/${pid}/assets/import/preview`, form)
  },
  assetImport: (pid: string, body: { source: string; rows: unknown[]; mapping?: string[] }) =>
    http<ImportSummary>(`/api/projects/${pid}/assets/import`, {
      method: "POST", body: JSON.stringify(body),
    }),

  // ---------- 逆向工作台（样本 / headless 缓存 / 人机共写） ----------
  // 注：单样本上传入口已下线（2026-10-13）——样本统一走「样本分析」页
  // （分析包文件树 → 对 PE/ELF/Mach-O 目标「登记为资产」）。后端 /samples 端点保留
  // 作兼容/测试用，前端不再调用。
  // 分析包：单文件、压缩包或浏览器目录上传；目录路径通过 relative_paths 保留
  samplePackages: (pid: string) =>
    http<SamplePackage[]>(`/api/projects/${pid}/sample-packages`),
  currentSamplePackage: (pid: string) =>
    http<SamplePackage & { undo_available?: boolean }>(`/api/projects/${pid}/sample-packages/current`),
  deleteSamplePackage: (pid: string) =>
    http<{ deleted: string; removed_assets: number; removed_files: number }>(
      `/api/projects/${pid}/sample-packages`, { method: "DELETE" }),
  samplePackagePreview: (pid: string, path: string) =>
    http<SamplePackagePreview>(`/api/projects/${pid}/sample-packages/preview?path=${encodeURIComponent(path)}`),
  samplePackageContentUrl: (pid: string, path: string) =>
    `/api/projects/${pid}/sample-packages/content?path=${encodeURIComponent(path)}`,
  samplePackage: (pid: string, packageId: string, versionId?: string) =>
    http<SamplePackage>(`/api/projects/${pid}/sample-packages/${packageId}${versionId ? `?version_id=${encodeURIComponent(versionId)}` : ""}`),
  uploadSamplePackage: (pid: string, file: File) => {
    const form = new FormData()
    form.append("file", file)
    return httpUpload<SamplePackage>(`/api/projects/${pid}/sample-packages`, form)
  },
  uploadSamplePackageFiles: (pid: string, files: File[]) => {
    const form = new FormData()
    for (const file of files) {
      form.append("files", file, file.name)
      form.append("relative_paths", (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name)
    }
    return httpUpload<SamplePackage>(`/api/projects/${pid}/sample-packages`, form)
  },
  appendSamplePackageFiles: (pid: string, files: File[]) => {
    const form = new FormData()
    for (const file of files) {
      form.append("files", file, file.name)
      form.append("relative_paths", (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name)
    }
    return httpUpload<SamplePackage>(`/api/projects/${pid}/sample-packages/files`, form)
  },
  moveSamplePackageEntry: (pid: string, path: string, targetPath: string) =>
    http<SamplePackage>(`/api/projects/${pid}/sample-packages/move`, {
      method: "POST", body: JSON.stringify({ path, target_path: targetPath }),
    }),
  deleteSamplePackageEntry: (pid: string, path: string) =>
    http<SamplePackage>(`/api/projects/${pid}/sample-packages/entries?path=${encodeURIComponent(path)}`, { method: "DELETE" }),
  undoSamplePackage: (pid: string) =>
    http<SamplePackage>(`/api/projects/${pid}/sample-packages/undo`, { method: "POST" }),
  uploadSamplePackageResumable: async (pid: string, file: File,
                                       onProgress?: (done: number, total: number) => void) => {
    const chunkSize = 8 * 1024 * 1024
    const totalChunks = Math.ceil(file.size / chunkSize)
    const session = await http<SamplePackageUploadSession>(
      `/api/projects/${pid}/sample-packages/uploads`, {
        method: "POST",
        body: JSON.stringify({ filename: file.name, total_size: file.size,
          total_chunks: totalChunks, source_type: "auto" }),
      })
    const status = await http<SamplePackageUploadSession>(
      `/api/projects/${pid}/sample-packages/uploads/${session.upload_id}`)
    const received = new Set(status.received_chunks ?? [])
    for (let index = 0; index < totalChunks; index += 1) {
      if (!received.has(index)) {
        const start = index * chunkSize
        const response = await fetch(
          `/api/projects/${pid}/sample-packages/uploads/${session.upload_id}/chunks/${index}`,
          { method: "PUT", headers: { "Content-Type": "application/octet-stream" },
            body: file.slice(start, Math.min(file.size, start + chunkSize)) })
        if (!response.ok) return raise(response)
      }
      onProgress?.(index + 1, totalChunks)
    }
    return http<SamplePackage>(
      `/api/projects/${pid}/sample-packages/uploads/${session.upload_id}/complete`,
      { method: "POST" })
  },
  selectSampleTargets: (pid: string, packageId: string, versionId: string, targetIds: string[]) =>
    http<{ package_id: string; version_id: string; target_ids: string[] }>(
      `/api/projects/${pid}/sample-packages/${packageId}/versions/${versionId}/targets`,
      { method: "POST", body: JSON.stringify({ target_ids: targetIds }) }),
  sampleTarget: (pid: string, packageId: string, versionId: string, targetId: string) =>
    http<SampleTargetAnalysis>(
      `/api/projects/${pid}/sample-packages/${packageId}/versions/${versionId}/targets/${targetId}`),
  analyzeSampleTarget: (pid: string, packageId: string, versionId: string, targetId: string, engine = "ida") =>
    http<SampleTargetAnalyzeResponse>(
      `/api/projects/${pid}/sample-packages/${packageId}/versions/${versionId}/targets/${targetId}/analyze`,
      { method: "POST", body: JSON.stringify({ engine }) }),
  // 登记为资产（2026-10-12）：把包内 PE/ELF/Mach-O 目标登记为 binary 黑板资产，
  // **不复制文件**——资产 meta 写包坐标（package_id/version_id/target_id/path），
  // 后续到逆向工作台分析（sha=binary_sha256）。幂等（重复登记只更新 meta）。
  registerSampleTargetAsset: (pid: string, packageId: string, versionId: string, targetId: string) =>
    http<SampleTargetRegisterResponse>(
      `/api/projects/${pid}/sample-packages/${packageId}/versions/${versionId}/targets/${targetId}/register`,
      { method: "POST" }),
  retryTriage: (pid: string, sha: string) =>
    http<{ job_id: string; sha: string }>(`/api/projects/${pid}/binaries/${sha}/triage`, { method: "POST" }),
  // 停止进行中的 headless 全量导出（大样本 P3，协作式：当前函数反编译完停下并保留已导出部分）
  cancelTriage: (pid: string, sha: string) =>
    http<{ cancelling: boolean; hint?: string }>(
      `/api/projects/${pid}/binaries/${sha}/triage/cancel`, { method: "POST" }),
  binaryOverview: (pid: string, sha: string) =>
    http<BinaryOverview>(`/api/projects/${pid}/binaries/${sha}/overview`),
  setBinaryEngine: (pid: string, sha: string, engine: "ida" | "ghidra") =>
    http<{ status: string; sha: string; engine: string }>(
      `/api/projects/${pid}/binaries/${sha}/engine`,
      { method: "PUT", body: JSON.stringify({ engine }) }),
  // 从项目移除样本（硬级联：func_kb/findings/logic_blocks/链边 + 资产行 + 磁盘产物）
  deleteSample: (pid: string, sha: string) =>
    http<{ deleted: string; removed_files: number; funcs: number; findings: number;
           logic_blocks: number; chain_links: number }>(
      `/api/projects/${pid}/binaries/${sha}`, { method: "DELETE" }),
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
  // GUI IDA MCP → 轻量函数清单缓存（无伪码）+ 有效命名 diff 回拉
  pullIdaFunctions: (pid: string, sha: string) =>
    http<{ job_id: string; sha: string }>(
      `/api/projects/${pid}/binaries/${sha}/pull-ida-functions`, { method: "POST" }),
  // 停止进行中的 IDA 拉取（已拉部分保持生效，可断点续拉）
  cancelPullIdaFunctions: (pid: string, sha: string) =>
    http<{ cancelling: boolean; hint?: string }>(
      `/api/projects/${pid}/binaries/${sha}/pull-ida-functions/cancel`, { method: "POST" }),
  // 反向：func_kb 有效命名批量写回 GUI IDA 当前库（只改内存，不落盘）
  pushNamesToIda: (pid: string, sha: string) =>
    http<{ job_id: string; sha: string }>(
      `/api/projects/${pid}/binaries/${sha}/push-names-to-ida`, { method: "POST" }),
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
  deleteFinding: (pid: string, findingId: string) =>
    http<{ deleted: string }>(`/api/projects/${pid}/findings/${findingId}`, { method: "DELETE" }),
  uploadDebugLog: (pid: string, file: File) => {
    const form = new FormData()
    form.append("kind", "debug-log")
    form.append("file", file)
    return httpUpload<ArtifactUploadResponse>(`/api/projects/${pid}/artifacts/upload`, form)
  },
  // 附件随发（2026-09-19）：≤64MB 任意类型，落 artifact（kind=attachment，同 sha 服务端去重）
  uploadAttachment: (pid: string, file: File) => {
    const form = new FormData()
    form.append("file", file)
    return httpUpload<AttachmentInfo>(`/api/projects/${pid}/attachments`, form)
  },
  // 附件下载（原始文件名经 content-disposition）
  downloadArtifact: async (pid: string, aid: string) => {
    const r = await fetch(`/api/projects/${pid}/artifacts/${aid}/download`)
    if (!r.ok) return raise(r)
    const cd = r.headers.get("content-disposition") ?? ""
    const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(cd)
    const name = m ? decodeURIComponent(m[1]) : aid
    const blob = await r.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = name
    a.click()
    URL.revokeObjectURL(url)
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

  // 蓝图（R4 逆向开发管线）
  blueprints: (pid: string) => http<Blueprint[]>(`/api/projects/${pid}/blueprints`),
  createBlueprint: (pid: string, body: {
    name: string; goal?: string; binary_sha256?: string; content_md?: string;
    feature_tree_id?: string
  }) =>
    http<Blueprint>(`/api/projects/${pid}/blueprints`, {
      method: "POST", body: JSON.stringify(body),
    }),
  updateBlueprint: (pid: string, bid: string, body: {
    name?: string; goal?: string; status?: BlueprintStatus;
    content_md?: string; content_append?: string; feature_tree_id?: string
  }) =>
    http<Blueprint>(`/api/projects/${pid}/blueprints/${bid}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  patchBlueprintModule: (pid: string, bid: string, moduleName: string, body: {
    desc?: string; spec?: string; notes?: string;
    func_addresses?: string[]; status?: BlueprintModuleStatus
  }) =>
    http<Blueprint>(`/api/projects/${pid}/blueprints/${bid}/modules/${encodeURIComponent(moduleName)}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),

  // 功能树（v36：分析阶段产物，独立于蓝图，可被多蓝图复用）
  featureTrees: (pid: string, sha?: string) => {
    const q = sha ? `?binary_sha256=${encodeURIComponent(sha)}` : ""
    return http<FeatureTree[]>(`/api/projects/${pid}/feature-trees${q}`)
  },
  featureTree: (pid: string, tid: string) =>
    http<FeatureTree>(`/api/projects/${pid}/feature-trees/${tid}`),
  createFeatureTree: (pid: string, body: { binary_sha256?: string; name?: string }) =>
    http<FeatureTree>(`/api/projects/${pid}/feature-trees`, {
      method: "POST", body: JSON.stringify(body),
    }),
  addFeatureNode: (pid: string, tid: string, body: {
    name: string; parent_id?: string | null; desc?: string;
    func_addresses?: string[]; status?: BlueprintNodeStatus
  }) =>
    http<FeatureTree>(`/api/projects/${pid}/feature-trees/${tid}/nodes`, {
      method: "POST", body: JSON.stringify(body),
    }),
  patchFeatureNode: (pid: string, tid: string, nid: string, body: {
    name?: string; desc?: string; status?: BlueprintNodeStatus;
    parent_id?: string | null; func_addresses?: string[]
  }) =>
    http<FeatureTree>(`/api/projects/${pid}/feature-trees/${tid}/nodes/${encodeURIComponent(nid)}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  deleteFeatureNode: (pid: string, tid: string, nid: string) =>
    http<FeatureTree>(`/api/projects/${pid}/feature-trees/${tid}/nodes/${encodeURIComponent(nid)}`, {
      method: "DELETE",
    }),

  // 业务逻辑块（逆向第四页签：函数协作/业务语义，人机共写）
  logicBlocks: (pid: string, sha?: string) => {
    const q = sha ? `?binary_sha256=${encodeURIComponent(sha)}` : ""
    return http<LogicBlockSummary[]>(`/api/projects/${pid}/logic-blocks${q}`)
  },
  logicBlock: (pid: string, lbid: string) =>
    http<LogicBlock>(`/api/projects/${pid}/logic-blocks/${lbid}`),
  createLogicBlock: (pid: string, body: {
    name: string; description?: string; binary_sha256?: string;
    feature_node_id?: string
  }) =>
    http<LogicBlock>(`/api/projects/${pid}/logic-blocks`, {
      method: "POST", body: JSON.stringify(body),
    }),
  updateLogicBlock: (pid: string, lbid: string, body: {
    name?: string; description?: string; seq?: number; feature_node_id?: string
  }) =>
    http<LogicBlock>(`/api/projects/${pid}/logic-blocks/${lbid}`, {
      method: "PATCH", body: JSON.stringify(body),
    }),
  deleteLogicBlock: (pid: string, lbid: string) =>
    http<{ deleted: string }>(`/api/projects/${pid}/logic-blocks/${lbid}`, {
      method: "DELETE",
    }),
  addLogicBlockFunc: (pid: string, lbid: string, body: {
    address: string; role?: string
  }) =>
    http<LogicBlock>(`/api/projects/${pid}/logic-blocks/${lbid}/funcs`, {
      method: "POST", body: JSON.stringify(body),
    }),
  updateLogicBlockFunc: (pid: string, lbid: string, address: string, role: string) =>
    http<LogicBlock>(
      `/api/projects/${pid}/logic-blocks/${lbid}/funcs?address=${encodeURIComponent(address)}`,
      { method: "PATCH", body: JSON.stringify({ role }) },
    ),
  removeLogicBlockFunc: (pid: string, lbid: string, address: string) =>
    http<LogicBlock>(
      `/api/projects/${pid}/logic-blocks/${lbid}/funcs?address=${encodeURIComponent(address)}`,
      { method: "DELETE" },
    ),
  artifacts: (pid: string, filter?: { task_id?: string; session_id?: string }) => {
    const q = new URLSearchParams()
    if (filter?.task_id) q.set("task_id", filter.task_id)
    if (filter?.session_id) q.set("session_id", filter.session_id)
    const qs = q.toString()
    return http<Artifact[]>(`/api/projects/${pid}/artifacts${qs ? `?${qs}` : ""}`)
  },
  // 工作区隔离（W3）：清空 scratch + .tmp（正式产物 artifacts/ 不动）
  clearScratch: (pid: string) =>
    http<{ removed: number; failed: string[] }>(`/api/projects/${pid}/scratch/clear`, { method: "POST" }),
  workspaceTree: (pid: string) =>
    http<WorkspaceTreeResponse>(`/api/projects/${pid}/workspace/tree`),
  // 本地文件动作（会话流文件卡，2026-10-03）：resolve/open/reveal/content 四动作，
  // 后端 scope 白名单=项目工作区 ∪ packs ∪ tools（越界 422）
  openProjectFile: (pid: string, path: string, action: "resolve" | "open" | "reveal" | "content") =>
    http<{ status: string; action: string; abs_path: string; content?: string; truncated?: boolean }>(
      `/api/projects/${pid}/files/open`,
      { method: "POST", body: JSON.stringify({ path, action }) }),

  // 单站攻击链路图 v3（website-attack-path-graph，2026-09-24）：
  // 目标 → 意图 → 收尾（漏洞/发现/死路），执行层展开
  attackPath: (pid: string, target: string) =>
    http<AttackPath>(`/api/projects/${pid}/attack-path?target=${encodeURIComponent(target)}`),
  // 项目级意图推导图（2026-10-13，逆向轨「意图」subnav 链路视角）：
  // 样本 → 意图 → 收尾（发现|死路），只读逻辑层（无执行层）
  intentGraph: (pid: string) =>
    http<IntentGraph>(`/api/projects/${pid}/intent-graph`),
  // 意图清单（人类收尾核对，?status=open/closed）
  intents: (pid: string, status?: string) =>
    http<IntentInfo[]>(`/api/projects/${pid}/intents${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  // 人类否决收尾：漏洞被证伪/有新证据 → 重开意图
  reopenIntent: (pid: string, intentId: string, note = "") =>
    http<IntentInfo>(`/api/projects/${pid}/intents/${encodeURIComponent(intentId)}/reopen`, {
      method: "POST", body: JSON.stringify({ note }),
    }),
  // 打法效果榜（M3/R4，物化侧统计）
  traceEffect: (pid: string, top = 20) =>
    http<TraceEffect>(`/api/projects/${pid}/trace-effect?top=${top}`),
  closeSession: (sid: string) =>
    http<{ status: string }>(`/api/sessions/${sid}/close`, { method: "POST" }),
  // 物理删除会话窗：仅 closed 可删（活窗先 close），黑板行级清除，events 留审计
  deleteSession: (sid: string) =>
    http<{ id: string; deleted: boolean }>(`/api/sessions/${sid}`, { method: "DELETE" }),
  pauseSession: (sid: string) =>
    http<{ status: string }>(`/api/sessions/${sid}/pause`, { method: "POST" }),
  // E8：恢复可附引导语（随快照注入）与增补步数；预算暂停缺省自动 +200
  resumeSession: (sid: string, body?: { note?: string; extra_steps?: number }) =>
    http<{ status: string }>(`/api/sessions/${sid}/resume`, {
      method: "POST", body: body ? JSON.stringify(body) : undefined,
    }),
  abortSession: (sid: string) =>
    http<{ status: string }>(`/api/sessions/${sid}/abort`, { method: "POST" }),
  // 会话中心化 M4：会话协作流（编排器+窗，delegate/derive/inbox/dm）
  sessionGraph: (pid: string) =>
    http<SessionGraph>(`/api/projects/${pid}/session-graph`),
  // 会话中心化（§4.4）：中途换智能体——会话行身份更新 + 热换装，历史/黑板全保留
  switchSessionRole: (sid: string, role: string) =>
    http<Session>(`/api/sessions/${sid}/role`, {
      method: "POST", body: JSON.stringify({ role }),
    }),
  // C2 指挥编排器：一次性目标指令（**deprecated**：对话窗全替代，端点仅存兼容 CLI）
  orchDirective: (pid: string, text: string) =>
    http<{ event_id: number; job_id: string; status: string }>(
      `/api/projects/${pid}/orchestrator/directive`, { method: "POST", body: JSON.stringify({ text }) }),
  // 对话化编排器（M1，§6.4）：与编排器对话——插队轮；busy 409 不排队（编排器正在思考）
  orchChat: (pid: string, text: string) =>
    http<{ job_id: string }>(`/api/projects/${pid}/orchestrator/chat`, {
      method: "POST", body: JSON.stringify({ text }),
    }),
  // 指挥对话手动压缩（直播间「指挥」页签 /compact，2026-10-06）：旧 orch.chat 压成
  // 摘要落 orch.compact，后续对话从摘要续起。巡检/对话在跑 → 409。
  compactOrchChat: (pid: string) =>
    http<{ status: string; before_msgs?: number; after_msgs?: number; summarized?: number; reason?: string }>(
      `/api/projects/${pid}/orchestrator/compact`, { method: "POST" }),
  // 指挥上下文用量快照（指挥页签 /context，2026-10-06）
  orchContext: (pid: string) =>
    http<ContextUsage>(`/api/projects/${pid}/orchestrator/context`),
  // M2 goal 闭环：阶段目标确认/清空（GET/PUT /goal）+ M3 拟人身份（PUT persona）
  projectGoal: (pid: string) =>
    http<{ phase_goal: PhaseGoal | null; persona: OrchPersona | null }>(
      `/api/projects/${pid}/goal`),
  setGoal: (pid: string, body: { text: string; criteria?: string[]; phase?: string | null }) =>
    http<{ status: string; goal?: PhaseGoal }>(`/api/projects/${pid}/goal`, {
      method: "PUT", body: JSON.stringify(body),
    }),
  setOrchPersona: (pid: string, body: { display_name: string; persona: string }) =>
    http<{ status: string; persona?: OrchPersona }>(
      `/api/projects/${pid}/orchestrator/persona`, {
        method: "PUT", body: JSON.stringify(body),
      }),
  // 分阶段工作流（pentest-phased-workflow M4）：阶段条数据源 + 人工流转
  // （人工最终不强制门；目标限当前阶段 next 清单内，违规 422）
  projectPhase: (pid: string) =>
    http<PhaseInfo>(`/api/projects/${pid}/phase`),
  transitionPhase: (pid: string, to: string, reason?: string) =>
    http<{ from: string; to: string; published: string[] }>(
      `/api/projects/${pid}/phase`, {
        method: "POST",
        body: JSON.stringify({ to, ...(reason ? { reason } : {}) }),
      }),
  // C2 判据模板：内置 + 用户自定义（全局）
  judgmentTemplates: () =>
    http<{ builtin: Record<string, string>; user: Record<string, string> }>(`/api/judgment-templates`),
  saveJudgmentTemplates: (templates: Record<string, string>) =>
    http<{ saved: number }>(`/api/judgment-templates`, { method: "PUT", body: JSON.stringify(templates) }),
  deleteJudgmentTemplate: (name: string) =>
    http<{ deleted: string }>(`/api/judgment-templates/${encodeURIComponent(name)}`, { method: "DELETE" }),
  // E8 人工引导通道：human_note 私信直达会话，worker 步边界注入
  // （2026-09-19 支持附件随发：attachment_ids 经 API 层校验后并入 inbox payload）
  sessionNote: (sid: string, text: string, attachmentIds: string[] = []) =>
    http<{ note_id: string; session_id: string; wake?: "resumed" | "kicked" | "queued" | "deferred" }>(`/api/sessions/${sid}/note`, {
      method: "POST",
      body: JSON.stringify({ text, attachment_ids: attachmentIds.length ? attachmentIds : undefined }),
    }),
  // 会话手动压缩上下文（/compact，2026-10-06）：把 chat-<sid>.json 旧历史经
  // LLM 压成摘要后重写，后续对话轮从压缩后历史起跑。执行中 409。
  compactSession: (sid: string) =>
    http<{ status: string; before_msgs?: number; after_msgs?: number; summarized?: number; reason?: string }>(
      `/api/sessions/${sid}/compact`, { method: "POST" }),
  // 上下文用量快照（/context，2026-10-06）：窗口分母=供应商 model_context（缺省 256K）
  sessionContext: (sid: string) =>
    http<ContextUsage>(`/api/sessions/${sid}/context`),
  sessionInbox: (sid: string, unread = false) =>
    http<InboxMessage[]>(`/api/sessions/${sid}/inbox${unread ? "?unread=true" : ""}`),
  readSessionInbox: (sid: string, ids?: string[]) =>
    http<{ marked: number }>(`/api/sessions/${sid}/inbox/read`, {
      method: "POST", body: JSON.stringify({ ids: ids ?? null }),
    }),

  // Agent / 编排
  models: () => http<ModelInfo>("/api/models"),
  // 项目级 executor 模型覆写（TRAE 新壳 M3，2026-09-25）
  executorLlm: (pid: string) => http<ExecutorLlmView>(
    `/api/projects/${pid}/executor-llm`),
  setExecutorLlm: (pid: string, provider: string, model?: string) =>
    http<ExecutorLlmView & { touched_sessions: string[] }>(
      `/api/projects/${pid}/executor-llm`, {
        method: "PUT", body: JSON.stringify({ provider, model: model ?? null }),
      }),
  resetExecutorLlm: (pid: string) =>
    http<ExecutorLlmView & { touched_sessions: string[] }>(
      `/api/projects/${pid}/executor-llm`, { method: "DELETE" }),
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
  agentWork: (sid: string, taskId?: string) =>
    http<{ job_id?: string; session_id: string; already_running?: boolean }>(
      `/api/agents/${sid}/work${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ""}`, { method: "POST" }),
  orchTick: (pid: string, opts: { allowed_roles?: string[]; max_sessions?: number;
    analyze_only?: boolean; budget_ticks?: number } = {}) =>
    http<{ job_id: string }>(`/api/projects/${pid}/orchestrator/tick`, {
      method: "POST", body: JSON.stringify(opts),
    }),
  // auto-attack（2026-09-28）：人工停止 L2 自动链（停链不降档，在跑任务不受影响）
  stopAutoAttack: (pid: string) =>
    http<{ stopped: boolean }>(`/api/projects/${pid}/orchestrator/auto-attack/stop`, {
      method: "POST",
    }),
  // 强制接管（2026-09-28 人工救济）：清 tick 租约，卡死编排轮的补救出口
  forceAcquireTick: (pid: string) =>
    http<{ released: boolean }>(`/api/projects/${pid}/orchestrator/tick/force-acquire`, {
      method: "POST",
    }),
  // A5 手动重排已随任务机制退役（2026-10-06）：端点/方法一并删除。
  job: (jobId: string) => http<Job>(`/api/jobs/${jobId}`),

  // 审批
  approvals: (pid: string, status?: string) =>
    http<Approval[]>(
      `/api/projects/${pid}/approvals${status ? `?status=${status}` : ""}`),
  decideApproval: (aid: string, decision: "approved" | "rejected") =>
    http<DecideApprovalResult>(`/api/approvals/${aid}/decide`, {
      method: "POST", body: JSON.stringify({ decision }),
    }),

  // Team direct execution（core/team；2026-10-06）：团队卡 / 查看并配置 / 运行报告
  teams: (pid: string) => http<Team[]>(`/api/projects/${pid}/teams`),
  team: (pid: string, teamId: string) => http<Team>(`/api/projects/${pid}/teams/${teamId}`),
  createTeam: (pid: string, body: { name: string; goal_text?: string; members?: TeamMemberInput[]; config?: Record<string, unknown> }) =>
    http<Team>(`/api/projects/${pid}/teams`, { method: "POST", body: JSON.stringify(body) }),
  patchTeam: (pid: string, teamId: string, body: { name?: string; goal_text?: string; members?: TeamMemberInput[]; config?: Record<string, unknown> }) =>
    http<Team>(`/api/projects/${pid}/teams/${teamId}`, { method: "PATCH", body: JSON.stringify(body) }),
  teamPreflight: (pid: string, teamId: string) =>
    http<TeamPreflight>(`/api/projects/${pid}/teams/${teamId}/preflight`),
  startTeam: (pid: string, teamId: string, body: { preflight_revision: string; confirmations: Record<string, boolean> }) =>
    http<{ run: TeamRun; job_id: string; status: string }>(
      `/api/projects/${pid}/teams/${teamId}/start`, { method: "POST", body: JSON.stringify(body) }),
  cancelTeam: (pid: string, teamId: string, body: { run_id: string; reason?: string }) =>
    http<TeamRun>(`/api/projects/${pid}/teams/${teamId}/cancel`, { method: "POST", body: JSON.stringify(body) }),
  teamRuns: (pid: string, teamId: string) =>
    http<TeamRun[]>(`/api/projects/${pid}/teams/${teamId}/runs`),
  teamRun: (pid: string, teamId: string, runId: string) =>
    http<TeamRun>(`/api/projects/${pid}/teams/${teamId}/runs/${runId}`),

  // packs 管理（正交分类学 §4.5：owners/任务类型属轨，Skill/红线轨与包各有一份）
  // 场景轨
  // 专家池（expert-pool M3：packs/experts/ 单文件池，角色写端点已退役 410）
  // 带 pid 时响应尾部追加 virtual 编排器条目（name 取该项目 meta 拟人显示名）
  listExperts: (pid?: string) =>
    http<Expert[]>(`/api/experts${pid ? `?pid=${encodeURIComponent(pid)}` : ""}`),
  getExpert: (id: string) => http<Expert>(`/api/experts/${id}`),
  createExpert: (id: string, body: ExpertBody) =>
    http<{ status: string; id: string; file: string }>("/api/experts", {
      method: "POST", body: JSON.stringify({ id, ...body }),
    }),
  updateExpert: (id: string, body: ExpertBody) =>
    http<{ status: string; id: string; file: string }>(`/api/experts/${id}`, {
      method: "PUT", body: JSON.stringify(body),
    }),
  deleteExpert: (id: string) =>
    http<{ status: string; trash: string }>(`/api/experts/${id}`, { method: "DELETE" }),
  // 场景档（M4a：只读清单，建项页预填组队/看板视图）
  trackProfiles: (track: string) => http<TrackProfile[]>(`/api/tracks/${track}/profiles`),
  trackRoles: (track: string) => http<PackRole[]>(`/api/tracks/${track}/roles`),
  trackRules: (track: string) => http<{ content: string; exists?: boolean }>(`/api/tracks/${track}/rules`),
  updateTrackRules: (track: string, content: string) =>
    http<{ status: string }>(`/api/tracks/${track}/rules`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  // 平台规则（渗透独有，全局单份；文件即规则）
  platformRules: () => http<OwnerRule[]>(`/api/platform-rules`),
  updatePlatformRule: (tag: string, content: string) =>
    http<{ status: string; tag: string }>(`/api/platform-rules/${encodeURIComponent(tag)}`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  deletePlatformRule: (tag: string) =>
    http<{ status: string; tag: string }>(`/api/platform-rules/${encodeURIComponent(tag)}`, {
      method: "DELETE",
    }),
  // F11 评级与价值口径（渗透独有，全局单份；注入带判级硬指令）
  ratings: () => http<RatingRule[]>(`/api/ratings`),
  updateRating: (tag: string, content: string) =>
    http<{ status: string; tag: string }>(`/api/ratings/${encodeURIComponent(tag)}`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  deleteRating: (tag: string) =>
    http<{ status: string; tag: string }>(`/api/ratings/${encodeURIComponent(tag)}`, {
      method: "DELETE",
    }),
  // 技能（扁平 packs/kb/skills/<name>）
  skills: () => http<SkillDef[]>(`/api/skills`),
  createSkill: (body: SkillCreateBody) =>
    http<{ status: string; name: string; path: string }>(`/api/skills`, {
      method: "POST", body: JSON.stringify(body),
    }),
  skillDetail: (name: string) => http<SkillDetail>(`/api/skills/${name}`),
  skillResource: (name: string, resource: string) =>
    http<SkillResource>(`/api/skills/${name}/resources/${resource.split("/").map(encodeURIComponent).join("/")}`),
  updateSkill: (name: string, content: string) =>
    http<{ status: string }>(`/api/skills/${name}`, {
      method: "PUT", body: JSON.stringify({ content }),
    }),
  deleteSkill: (name: string) =>
    http<{ status: string; name: string; trash: string }>(
      `/api/skills/${name}`, { method: "DELETE" }),
  setSkillEnabled: (name: string, enabled: boolean) =>
    http<{ status: string; enabled: boolean }>(
      `/api/skills/${name}/enabled`, {
        method: "PATCH", body: JSON.stringify({ enabled }),
      }),
  routePreview: (body: RoutePreviewBody) =>
    http<RouteHit[]>("/api/skills/route-preview", {
      method: "POST", body: JSON.stringify(body),
    }),
  skillsVocab: () => http<SkillVocab>("/api/skills/vocab"),
  skillCatalog: (pid?: string | null) => http<SkillCatalog>(`/api/skills/catalog${pid ? `?pid=${encodeURIComponent(pid)}` : ""}`),
  skillFiles: (name: string) =>
    http<{ root: string; files: SkillFileEntry[] }>(`/api/skills/${name}/files`),
  skillFile: (name: string, path: string) =>
    http<SkillFileEntry>(`/api/skills/${name}/files/content?path=${encodeURIComponent(path)}`),
  createSkillFile: (name: string, body: { path: string; content?: string; is_dir?: boolean }) =>
    http<{ status: string; path: string }>(`/api/skills/${name}/files`, { method: "POST", body: JSON.stringify(body) }),
  updateSkillFile: (name: string, body: { path: string; content: string }) =>
    http<{ status: string; path: string }>(`/api/skills/${name}/files`, { method: "PUT", body: JSON.stringify(body) }),
  renameSkillFile: (name: string, path: string, newPath: string) =>
    http<{ status: string; path: string }>(`/api/skills/${name}/files/rename`, { method: "POST", body: JSON.stringify({ path, new_path: newPath }) }),
  deleteSkillFile: (name: string, path: string) =>
    http<{ status: string; path: string }>(`/api/skills/${name}/files?path=${encodeURIComponent(path)}`, { method: "DELETE" }),

  // kb 本地基线（C2/C3：源树/读写/改名联动/版本/引用）
  kbList: (cap: string) =>
    http<{ cap: string; sources: KbSourceTree[] }>(`/api/capabilities/${cap}/kb`),
  /** kb 正文搜索（大小写不敏感 substring，按命中次数降序） */
  kbSearch: (cap: string, q: string, limit = 50) =>
    http<{ cap: string; q: string; results: KbSearchHit[] }>(
      `/api/capabilities/${cap}/kb/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  kbRoutes: (cap: string) =>
    http<{ cap: string; routes: Record<string, string[]> }>(
      `/api/capabilities/${cap}/kb/routes`),
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
    source_refs?: { cap: string; path: string; title?: string }[]
  }) =>
    http<Proposal>("/api/proposals", { method: "POST", body: JSON.stringify(body) }),
  proposals: (status?: "pending" | "approved" | "rejected") =>
    http<Proposal[]>(`/api/proposals${status ? `?status=${status}` : ""}`),
  proposal: (id: string) => http<Proposal>(`/api/proposals/${id}`),
  /** 知识库迭代触发规则（与后端判定同源；pid 给出时附带该项目开关状态） */
  proposalRules: (pid?: string | null) =>
    http<KbIterationRules>(`/api/proposals/rules${pid ? `?pid=${encodeURIComponent(pid)}` : ""}`),
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
  /** F8 会话级复盘后台 Job（planner_llm 复盘该会话跑过的任务，产 pending 提案；无 key 503） */
  sessionReview: (sid: string) =>
    http<{ job_id: string }>(`/api/sessions/${sid}/review`, { method: "POST" }),
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

  // 网关策略快照（gateway-config-view M1，DESIGN §7）：只读快照 + 手动重探测
  gatewayConfig: () => http<GatewayConfig>("/api/gateway/config"),
  // Agent 工具目录（只读静态全集+分组，设置页「工具」tab）
  agentTools: () => http<AgentToolsResponse>("/api/agent-tools"),
  gatewayProbe: () =>
    http<CapabilityInventory>("/api/gateway/probe", { method: "POST" }),

  // LLM 供应商（DESIGN.md §8）
  llmProviders: () =>
    http<{ providers: LlmProvider[]; default: { provider: string; model: string } | null;
           default_provider: string | null }>(
      "/api/llm/providers"),
  saveLlmProviders: (providers: LlmProvider[], defaultProvider?: string) =>
    http<{ providers: LlmProvider[]; default: { provider: string; model: string } | null;
           default_provider: string | null }>(
      "/api/llm/providers", {
        method: "PUT", body: JSON.stringify(
          defaultProvider !== undefined ? { providers, default_provider: defaultProvider } : { providers }),
      }),
  discoverLlm: (body: { name?: string; base_url?: string; api_key?: string; format?: LlmProvider["format"]; proxy?: string | null }) =>
    http<{ listed: boolean; models: DiscoveredModel[]; probed?: string[] }>(
      "/api/llm/discover", { method: "POST", body: JSON.stringify(body) }),
  testLlmModel: (body: { name?: string; base_url?: string; api_key?: string; format?: LlmProvider["format"]; model: string; proxy?: string | null }) =>
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

  // ---------- F6 内置浏览器（pentest/redteam 轨；F6-v3 去会话化——后端自动
  // human-main 隐式会话，前端零会话 UI；爆破/重发为人类 UI 专属） ----------
  browserStatus: () => http<BrowserStatus>("/api/browser/status"),
  browserState: (pid: string) =>
    http<BrowserState>(`/api/projects/${pid}/browser/state`),
  browserDesktopAttach: (pid: string, body: { cdp_url: string; sid: string; owner?: string }) =>
    http<BrowserSessionInfo>(`/api/projects/${pid}/browser/desktop/attach`, {
      method: "POST", body: JSON.stringify(body),
    }),
  browserDesktopDetach: (pid: string, sid: string) =>
    http<{ detached: boolean; sid: string }>(`/api/projects/${pid}/browser/desktop/detach`, {
      method: "POST", body: JSON.stringify({ sid, paused: false }),
    }),
  browserNavigate: (pid: string, url: string, sid?: string) =>
    http<{ final_url: string; title: string; status: number; target_host?: string; duration_ms: number }>(
      `/api/projects/${pid}/browser/navigate`,
      { method: "POST", body: JSON.stringify({ url, sid }) }),
  browserAction: (pid: string, body: {
    action: "click" | "type" | "back"
    selector?: string; text?: string; x?: number; y?: number; sid?: string
  }) =>
    http<{ final_url?: string; title?: string; content?: string; truncated?: boolean }>(
      `/api/projects/${pid}/browser/action`,
      { method: "POST", body: JSON.stringify(body) }),
  browserTakeover: (pid: string, sid: string, paused: boolean) =>
    http<BrowserSessionInfo>(`/api/projects/${pid}/browser/takeover`, {
      method: "POST", body: JSON.stringify({ sid, paused }),
    }),
  browserScreenshot: (pid: string) =>
    http<{ png: string; ts: number }>(`/api/projects/${pid}/browser/screenshot`),
  browserHistory: (pid: string, opts?: {
    since_id?: number; limit?: number; batch_id?: string; source?: string; session_id?: string
  }) => {
    const q = new URLSearchParams()
    if (opts?.since_id) q.set("since_id", String(opts.since_id))
    if (opts?.limit) q.set("limit", String(opts.limit))
    if (opts?.batch_id) q.set("batch_id", opts.batch_id)
    if (opts?.source) q.set("source", opts.source)
    if (opts?.session_id) q.set("session_id", opts.session_id)
    const qs = q.toString()
    return http<HttpHistoryRow[]>(`/api/projects/${pid}/browser/history${qs ? `?${qs}` : ""}`)
  },
  browserHistoryRow: (pid: string, rowId: number) =>
    http<HttpHistoryRow>(`/api/projects/${pid}/browser/history/${rowId}`),
  clearBrowserHistory: (pid: string, batchId?: string) =>
    http<{ removed: number }>(
      `/api/projects/${pid}/browser/history${batchId ? `?batch_id=${encodeURIComponent(batchId)}` : ""}`,
      { method: "DELETE" }),
  /** 202 返回 {job_id, run_id}，pollJob 轮询；job.result = 重发结果 HttpHistoryRow。
   *  F6-v3：原始报文 raw（解析在后端）或 capture_id 模板。
   *  2026-10-07：新增传输选项（强制HTTPS/重定向/代理/体长/跳过校验/国密TLS）。 */
  browserReplay: (pid: string, body: {
    capture_id?: number; raw?: string
  } & ReplayOptions) =>
    http<{ job_id: string; run_id: string }>(`/api/projects/${pid}/browser/replay`, {
      method: "POST", body: JSON.stringify(body),
    }),
  /** 中断在跑的单条重发（httpx=放弃等待，国密 sidecar=kill 子进程）；不在运行 404 */
  replayStop: (pid: string, runId: string) =>
    http<{ stopped: boolean }>(
      `/api/projects/${pid}/browser/replay/${encodeURIComponent(runId)}/stop`,
      { method: "POST" }),
  /** 国密 TLS 通道能力探测（工作台据此置灰开关；available=false 绝不静默降级） */
  gmStatus: (pid: string) =>
    http<GmStatus>(`/api/projects/${pid}/browser/gm-status`),
  /** F6-v3 拦截（仅人工浏览流量可挂起）：快照 / 开关 / 裁决 */
  browserIntercept: (pid: string) =>
    http<InterceptState>(`/api/projects/${pid}/browser/intercept`),
  browserInterceptToggle: (pid: string, direction: "request" | "response", enabled: boolean) =>
    http<InterceptState>(`/api/projects/${pid}/browser/intercept/toggle`, {
      method: "POST", body: JSON.stringify({ direction, enabled }),
    }),
  browserInterceptDecide: (pid: string, holdId: string, body: {
    action: "forward" | "drop"; raw?: string
  }) =>
    http<{ decided: boolean; hold_id: string; action: string }>(
      `/api/projects/${pid}/browser/intercept/${encodeURIComponent(holdId)}/decide`,
      { method: "POST", body: JSON.stringify(body) }),
  /** 爆破（人类 UI 专属，Agent 无发起入口）：202 + batch_id，结果按 batch 拉历史 */
  browserIntruder: (pid: string, body: {
    template: IntruderTemplate; payloads: IntruderPayloadSpec[]
    concurrency?: number; rate_per_sec?: number; max_requests?: number
    proxy?: string
  }) =>
    http<{ job_id: string; batch_id: string; max_concurrency: number }>(
      `/api/projects/${pid}/browser/intruder`,
      { method: "POST", body: JSON.stringify(body) }),
  intruderStop: (pid: string, batchId: string) =>
    http<{ stopped: boolean }>(
      `/api/projects/${pid}/browser/intruder/${encodeURIComponent(batchId)}/stop`,
      { method: "POST" }),

  // ---------- 代理池（fir-proxy 托管，2026-10-07 → 2026-10-11 全局单池） ----------
  // 全局池：进程级单 serve + 单池，所有项目共享；路由无 pid。
  proxyStatus: () =>
    http<ProxyStatus>(`/api/proxy/status`),
  proxyStart: () =>
    http<ProxyStatus>(`/api/proxy/start`, { method: "POST" }),
  proxyStop: () =>
    http<{ running: boolean }>(`/api/proxy/stop`, { method: "POST" }),
  proxyList: () =>
    http<ProxyListResponse>(`/api/proxy/proxies`),
  proxyRotate: () =>
    http<{ ok: boolean; current: ProxyRecord | null }>(
      `/api/proxy/rotate`, { method: "POST" }),
  /** 精确切换到指定代理（host:port；2026-10-15 访问测试「可达即切换」入口） */
  proxyUse: (proxy: string) =>
    http<{ ok: boolean; current: ProxyRecord | null }>(
      `/api/proxy/use`, { method: "POST", body: JSON.stringify({ proxy }) }),
  proxyAdd: (records: ProxyRecord[]) =>
    http<{ added: number; pool_size: number }>(`/api/proxy/add`,
      { method: "POST", body: JSON.stringify({ records }) }),
  proxyRemove: (addresses: string[]) =>
    http<{ removed: number; pool_size: number }>(`/api/proxy/remove`,
      { method: "POST", body: JSON.stringify({ addresses }) }),
  proxySelect: (body: {
    limit?: number; region?: string | null; max_latency_ms?: number | null; include_failed?: boolean
  }) =>
    http<{ proxies: ProxyRecord[] }>(`/api/proxy/select`,
      { method: "POST", body: JSON.stringify(body) }),
  /** 从在线源抓取入池（202：Job 轮询）。`limit` = 本次抓取条数上限（0=不限；省略=用后端配置）。 */
  proxyFetch: (protocols: string[] = [], limit?: number) =>
    http<{ job_id: string }>(`/api/proxy/fetch`,
      { method: "POST", body: JSON.stringify({ protocols, limit }) }),
  /** 批量验证池内代理（202：Job 轮询） */
  proxyValidate: (workers = 100) =>
    http<{ job_id: string }>(`/api/proxy/validate`,
      { method: "POST", body: JSON.stringify({ workers }) }),
  /** 指定网站可达性/WAF 封禁检测（202：Job 轮询；只读不改池） */
  proxyCheckUrl: (body: { url: string; workers?: number; limit?: number;
                          probe_waf?: boolean }) =>
    http<{ job_id: string }>(`/api/proxy/check-url`,
      { method: "POST", body: JSON.stringify(body) }),
  /** 代理池可配项（验证基准站等）读取 */
  proxyConfigGet: () =>
    http<ProxyConfigView>(`/api/proxy/config`),
  /** 代理池可配项写回（写 config/proxy.json + 热更新，下轮验证/抓取即生效） */
  proxyConfigPut: (body: {
    validate_target?: string
    validate_target_https?: string | null
    validate_target_b?: string | null
    validate_timeout_s?: number
    prune_latency_ms?: number | null
    fetch_limit?: number
  }) =>
    http<ProxyConfigView>(`/api/proxy/config`,
      { method: "PUT", body: JSON.stringify(body) }),

  // ---------- ARTEX Planner 与任务图 ----------
  artexPlan: (pid: string, body: { goal: string; task_id?: string; asset_ids?: string[]; allowed_experts?: string[]; start_engine?: boolean }) =>
    http<ArtexPlanResult>(`/api/projects/${pid}/artex/plan`, {
      method: "POST", body: JSON.stringify(body),
    }),
  artexTaskList: (pid: string, limit = 100) =>
    http<import("./types").ArtexTaskListResponse>(`/api/projects/${pid}/artex/tasks?limit=${limit}`),
  artexTaskGraph: (pid: string, taskId: string) =>
    http<ArtexTaskGraph>(`/api/projects/${pid}/artex/tasks/${encodeURIComponent(taskId)}`),
  artexTaskRuntime: (pid: string, taskId: string) =>
    http<import("./types").ArtexTaskRuntime>(`/api/projects/${pid}/artex/tasks/${encodeURIComponent(taskId)}/runtime`),
  artexTaskCoverage: (pid: string, taskId: string, type = "", limit = 50, offset = 0) =>
    http<import("./types").ArtexCoverageResponse>(
      `/api/projects/${pid}/artex/tasks/${encodeURIComponent(taskId)}/coverage?type=${encodeURIComponent(type)}&limit=${limit}&offset=${offset}`),
  artexTaskCoverageGraph: (pid: string, taskId: string) =>
    http<{ task_id: string; nodes: import("./types").ArtexCoverageGraphNode[]; edges: { src: string; dst: string }[] }>(
      `/api/projects/${pid}/artex/tasks/${encodeURIComponent(taskId)}/coverage/graph`),
  artexTaskScope: (pid: string, taskId: string,
                   body: { asset_ids?: string[]; remove?: boolean; note?: string }) =>
    http<{ changed: boolean; result?: { added?: string[]; existing?: string[]; missing?: string[]; removed?: number }; coverage: import("./types").ArtexCoverage }>(
      `/api/projects/${pid}/artex/tasks/${encodeURIComponent(taskId)}/scope`, {
      method: "POST", body: JSON.stringify({ asset_ids: body.asset_ids ?? [], remove: body.remove ?? false, note: body.note ?? "" }),
    }),
  artexExploration: (pid: string, taskId?: string) =>
    http<import("./types").ArtexExplorationGraph>(`/api/projects/${pid}/artex/exploration${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ""}`),
  artexExplorationNode: (pid: string, nodeId: string) =>
    http<{ node: import("./types").ArtexExplorationNode; edges: import("./types").ArtexExplorationEdge[] }>(`/api/projects/${pid}/artex/exploration/${encodeURIComponent(nodeId)}`),
  artexActivity: (pid: string, taskId?: string, sinceId = 0) =>
    http<ArtexActivityResponse>(`/api/projects/${pid}/artex/activity?since_id=${sinceId}${taskId ? `&task_id=${encodeURIComponent(taskId)}` : ""}`),
  artexActivityStreamUrl: (pid: string, taskId?: string, sinceId = 0) =>
    `/api/projects/${pid}/artex/activity/stream?since_id=${sinceId}${taskId ? `&task_id=${encodeURIComponent(taskId)}` : ""}`,
  artexEngineStatus: (pid: string, taskId?: string) =>
    http<ArtexEngineStatus>(`/api/projects/${pid}/artex/engine${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ""}`),
  artexEngineStart: (pid: string) =>
    http<{ project_id: string; running: boolean }>(`/api/projects/${pid}/artex/engine/start`, { method: "POST" }),
  artexEngineStop: (pid: string) =>
    http<{ project_id: string; running: boolean; drained: boolean; active_workers: number }>(`/api/projects/${pid}/artex/engine/stop`, { method: "POST" }),
  artexIntentControl: (pid: string, intentId: string, action: "pause" | "resume" | "cancel" | "retry") =>
    http<ArtexIntent>(`/api/projects/${pid}/artex/intents/${encodeURIComponent(intentId)}/control`, {
      method: "POST", body: JSON.stringify({ action }),
    }),
  artexWorkerControl: (pid: string, intentId: string, action: "kill" | "steer", message = "") =>
    http<ArtexIntent>(`/api/projects/${pid}/artex/intents/${encodeURIComponent(intentId)}/worker-control`, {
      method: "POST", body: JSON.stringify({ action, message }),
    }),

  // ---------- 智能体工作台（K9，2026-09-29）：独立轻量对话运行时 ----------
  chatAgents: (pid: string) =>
    http<ChatAgent[]>(`/api/chat/agents?pid=${encodeURIComponent(pid)}`),
  chatThreads: (pid: string, agentId?: string) =>
    http<ChatThread[]>(
      `/api/projects/${pid}/chat/threads${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ""}`),
  chatThreadCreate: (pid: string, agentId: string, title?: string,
                     provider?: string, model?: string, webSearch?: boolean) =>
    http<ChatThread>(`/api/projects/${pid}/chat/threads`, {
      method: "POST",
      body: JSON.stringify({ agent_id: agentId, title: title ?? null,
                             provider: provider || null, model: model || null,
                             web_search: !!webSearch }),
    }),
  chatThread: (tid: string, afterId = 0) =>
    http<ChatThreadDetail>(`/api/chat/threads/${tid}?after_id=${afterId}`),
  chatThreadDelete: (tid: string) =>
    http<void>(`/api/chat/threads/${tid}`, { method: "DELETE" }),
  // 线程级模型覆写（v37，2026-10-13）：设置/重置该线程后续轮次所用模型。
  chatThreadLlm: (tid: string, provider: string, model?: string) =>
    http<ChatThread>(`/api/chat/threads/${tid}/llm`, {
      method: "PUT", body: JSON.stringify({ provider, model: model || null }),
    }),
  chatThreadLlmReset: (tid: string) =>
    http<ChatThread>(`/api/chat/threads/${tid}/llm`, { method: "DELETE" }),
  // 线程级联网搜索开关（v43，web-search-toggle，2026-10-19）：主控开 =
  // spawn 子代理默认继承；不进专家工具勾选。
  chatThreadWebSearch: (tid: string, enabled: boolean) =>
    http<ChatThread>(`/api/chat/threads/${tid}/web-search`, {
      method: "PUT", body: JSON.stringify({ enabled }),
    }),
  chatSend: (tid: string, text: string,
             refs?: { skills: string[]; mcps: string[] } | null,
             attachmentIds: string[] = []) =>
    http<{ status: string; thread_id: string }>(
      `/api/chat/threads/${tid}/messages`, { method: "POST", body: JSON.stringify({ text, refs: refs ?? null, attachment_ids: attachmentIds.length ? attachmentIds : undefined }) }),
  chatStop: (tid: string) =>
    http<void>(`/api/chat/threads/${tid}/stop`, { method: "POST" }),
  chatMcp: (pid: string) =>
    http<{ servers: ChatMcpServer[] }>(`/api/chat/mcp?pid=${encodeURIComponent(pid)}`),

  // ---------- 诊断（diagnostics-log，2026-10-14）：会话审计 + 失败日志 ----------
  // 会话清单（chat=对话线程 / agent=执行会话，带消息数与失败数）；
  // 消息正文分流：chat 走 api.chatThread(tid)，agent 走 api.eventsTail(pid, n, sid)。
  diagSessions: (pid: string) =>
    http<DiagSessions>(`/api/projects/${pid}/diagnostics/sessions`),
  // 失败日志（倒序最新在前）：kind=tool|mcp；name=工具名子串；author=来源子串
  // （author/session_id/thread_id 任一命中——按会话筛失败直接传会话 id）。
  diagFailures: (pid: string, opts?: { kind?: "tool" | "mcp"; name?: string; author?: string; limit?: number }) => {
    const q = new URLSearchParams()
    if (opts?.kind) q.set("kind", opts.kind)
    if (opts?.name) q.set("name", opts.name)
    if (opts?.author) q.set("author", opts.author)
    if (opts?.limit) q.set("limit", String(opts.limit))
    const s = q.toString()
    return http<DiagFailures>(
      `/api/projects/${pid}/diagnostics/failures${s ? `?${s}` : ""}`)
  },
  // 技能/知识库使用统计（skill-usage，2026-10-21）：注入 vs 真打开、kb 零命中率。
  diagSkills: (pid: string, limit = 200) =>
    http<DiagSkillUsage>(`/api/projects/${pid}/diagnostics/skills?limit=${limit}`),
  // 会话系统提示词快照（v39）：该会话最近一次实际下发的完整 system 正文。
  // 无快照 → 后端 404（前端按「尚无快照」提示，不视为错误）。
  diagSystemPrompt: (pid: string, sessionId: string) =>
    http<DiagSystemPrompt>(
      `/api/projects/${pid}/diagnostics/system-prompt`
      + `?session_id=${encodeURIComponent(sessionId)}`),

  // 请求体留痕（diagnostics-request-body，2026-10-17）：进程级开关 + 按会话取清单/全文
  diagLlmDump: () => http<DiagLlmDump>("/api/diagnostics/llm-dump"),
  setDiagLlmDump: (enabled: boolean) =>
    http<DiagLlmDump>("/api/diagnostics/llm-dump",
                      { method: "PUT", body: JSON.stringify({ enabled }) }),
  diagRequests: (pid: string, sessionId: string, limit = 20) =>
    http<{ enabled: boolean; dir: string; records: DiagRequestMeta[] }>(
      `/api/projects/${pid}/diagnostics/requests`
      + `?session_id=${encodeURIComponent(sessionId)}&limit=${limit}`),
  diagRequest: (pid: string, sessionId: string, rid: string) =>
    http<DiagRequestDetail>(
      `/api/projects/${pid}/diagnostics/requests/${encodeURIComponent(rid)}`
      + `?session_id=${encodeURIComponent(sessionId)}`),
  // 红线一键自检（diagnostics-redline-test，2026-10-17）：拿该会话真实 system 正文
  // + canary 打一发，判定「注没注入 / 送没送到模型」。provider/model 缺省=跟随会话。
  redlineTest: (pid: string, sessionId: string, provider?: string, model?: string) =>
    http<DiagRedlineTest>(
      `/api/projects/${pid}/diagnostics/redline-test`,
      { method: "POST", body: JSON.stringify({ session_id: sessionId, provider, model }) }),

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

// F6-v2 浏览器实时画面流：帧推送（服务端→客户端）+ 接管输入（客户端→服务端）
export function browserWsUrl(pid: string, sid?: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws"
  return `${proto}://${location.host}/api/projects/${pid}/browser/ws${sid ? `?sid=${encodeURIComponent(sid)}` : ""}`
}
