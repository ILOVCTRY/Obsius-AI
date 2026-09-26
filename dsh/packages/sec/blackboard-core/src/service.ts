/**
 * BlackboardService —— sec 黑板唯一写入口（服务键 'blackboard'）。
 * 所有写操作经 serialize() 承诺链串行（等价 Python 写锁）；
 * 底层只经 KvTable 的 put/update/delete，域句柄不对外暴露写。
 * 业务逻辑在 *-api.ts 纯函数模块，本类只做装配 + 薄委托。
 */
import type { Context } from '@deepseek-ai/cordis'
import { Service } from '@deepseek-ai/cordis'
import type { Domain } from '@deepseek-ai/dsh-storage-domain'
import { SEC_DOMAIN } from './domain'
import { now } from './ids'
import { BlackboardError } from './error'
import type { Project, Asset, Finding, Task, SecEvent, Approval } from './types'
import type { SecTables, WriteContext } from './tables'
import * as projectsApi from './projects-api'
import * as assetsApi from './assets-api'
import * as findingsApi from './findings-api'
import * as tasksApi from './tasks-api'
import * as eventsQueryApi from './events-query'
import * as approvalsApi from './approvals-api'
import { parseInput } from './validation'

/** 黑板域版本信息（M2 验收点：域版本）。 */
export interface DomainVersionInfo {
  /** 域名称。 */
  name: string
  /** defineDomain version 盖章。 */
  version: number
  /** 域布局。 */
  layout: 'per-record'
  /** 对齐的 Python 黑板 schema 版本。 */
  schemaVersion: number
}

/** 当前对齐的 Python 黑板 schema 版本。 */
export const SCHEMA_VERSION = 24

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** sec 黑板唯一写入口。 */
    blackboard: BlackboardService
  }
}

/** sec 黑板服务：独占 sec 域；业务面经薄方法委托给 *-api。 */
export class BlackboardService extends Service {
  /** 打开的 sec 域（私有：外部拿不到写句柄）。 */
  private domain?: Domain<typeof SEC_DOMAIN>

  /** 写串行链尾；每条 settle（reject 被吞），链永不断。 */
  private writeChain: Promise<void> = Promise.resolve()

  /**
   * @param ctx - Host 上下文。
   */
  constructor(ctx: Context) {
    super(ctx, 'blackboard')
  }

  /** 装配期打开 sec 域；yield 的析构在 fiber 卸载时关域。 */
  async *[Service.init](): AsyncGenerator<() => void, void, void> {
    this.domain = await this.ctx.storageDomain.open(SEC_DOMAIN)
    yield () => {
      void this.domain?.close()
      this.domain = undefined
    }
  }

  /**
   * 串行执行一段写工作：等价 Python `_tx()` 的进程写锁语义。
   * 多记录写 + eventSeq 推进必须在同一 work 内原子排队。
   * @param work - 返回 Promise 的写工作。
   * @returns work 的结果。
   */
  serialize<T>(work: (io: WriteContext) => Promise<T>): Promise<T> {
    const run = (): Promise<T> => work(this.writeContext())
    const result = this.writeChain.then(run)
    this.writeChain = result.then(noop, noop)
    return result
  }

  /**
   * 落一条事件（直写，调用方必须已在 serialize work 内——不自行串行化，
   * 否则在 writeChain 上等自己造成死锁）：global.eventSeq+1、单调零填充键。
   */
  private async writeEvent(
    projectId: string,
    kind: string,
    payload: Record<string, unknown>,
    sessionId: string | null = null,
  ): Promise<SecEvent> {
    const seq = this.domain!.global.get().eventSeq + 1
    await this.domain!.global.set({ eventSeq: seq })
    const id = `evt-${String(seq).padStart(12, '0')}`
    const event: SecEvent = {
      id,
      project_id: projectId,
      kind,
      payload,
      session_id: sessionId,
      created_at: now(),
    }
    await this.domain!.table('events').put(id, event)
    return event
  }

  /** 组装写工作上下文（表袋 + global + 事件直写口）。 */
  private writeContext(): WriteContext {
    const d = this.domain!
    const tables: SecTables = {
      projects: d.table('projects'),
      assets: d.table('assets'),
      findings: d.table('findings'),
      tasks: d.table('tasks'),
      events: d.table('events'),
      approvals: d.table('approvals'),
    }
    return {
      tables,
      global: d.global,
      appendEvent: (pid, kind, payload, sid = null) => this.writeEvent(pid, kind, payload, sid),
    }
  }

  /**
   * 读取黑板域版本信息。
   * @returns 域名称 / 版本 / 布局 / schema 版本。
   */
  domainVersion(): DomainVersionInfo {
    return {
      name: SEC_DOMAIN.name,
      version: SEC_DOMAIN.version,
      layout: SEC_DOMAIN.layout ?? 'per-record',
      schemaVersion: SCHEMA_VERSION,
    }
  }

  /** 域是否已打开（冒烟用）。 */
  get isOpen(): boolean {
    return this.domain !== undefined
  }

  /**
   * 读取当前事件序号（global.eventSeq；只读不推进）。
   * @returns 已分配的最大事件序号。
   */
  eventSeq(): number {
    return this.domain!.global.get().eventSeq
  }

  // ── projects 五法 ──────────────────────────────────────
  createProject(raw: unknown): Promise<Project> {
    return this.serialize(io => projectsApi.createProject(io, raw))
  }
  getProject(id: string): Project {
    return projectsApi.getProject(this.writeContext(), id)
  }
  listProjects(): Project[] {
    return projectsApi.listProjects(this.writeContext())
  }
  updateProjectConfig(id: string, raw: unknown): Promise<Project> {
    return this.serialize(io => projectsApi.updateProjectConfig(io, id, raw))
  }
  updateProjectTrack(id: string, raw: unknown): Promise<Project> {
    return this.serialize(io => projectsApi.updateProjectTrack(io, id, raw))
  }

  // ── assets 十法 ────────────────────────────────────────
  registerAsset(raw: unknown) {
    return this.serialize(io => assetsApi.registerAsset(io, raw))
  }
  getAsset(id: string): Asset {
    return assetsApi.getAsset(this.writeContext(), id)
  }
  findAsset(projectId: string, type: string, value: string): Asset {
    return assetsApi.findAsset(this.writeContext(), projectId, type, value)
  }
  listAssets(projectId: string, raw: unknown = {}): Asset[] {
    return assetsApi.listAssets(this.writeContext(), projectId, raw)
  }
  setAssetParent(assetId: string, raw: unknown): Promise<Asset> {
    return this.serialize(io => assetsApi.setAssetParent(io, assetId, raw))
  }
  updateAssetMeta(assetId: string, raw: unknown): Promise<Asset> {
    return this.serialize(io => assetsApi.updateAssetMeta(io, assetId, raw))
  }
  setAssetStatus(assetId: string, raw: unknown): Promise<Asset> {
    return this.serialize(io => assetsApi.setAssetStatus(io, assetId, raw))
  }
  deleteAsset(assetId: string, raw: unknown = {}) {
    return this.serialize(io => assetsApi.deleteAsset(io, assetId, raw))
  }
  assetForest(projectId: string) {
    return assetsApi.assetForest(this.writeContext(), projectId)
  }
  assetCoverage(projectId: string) {
    return assetsApi.assetCoverage(this.writeContext(), projectId)
  }

  // ── findings 三法 ──────────────────────────────────────
  addFinding(raw: unknown): Promise<findingsApi.FindingAddResult> {
    return this.serialize(io => findingsApi.addFinding(io, raw))
  }
  getFinding(projectId: string, id: string): Finding {
    return findingsApi.getFinding(this.writeContext(), projectId, id)
  }
  listFindings(projectId: string, raw: unknown = {}): Finding[] {
    return findingsApi.listFindings(this.writeContext(), projectId, raw)
  }

  // ── tasks 六法 ─────────────────────────────────────────
  publishTask(raw: unknown): Promise<tasksApi.TaskPublishResult> {
    return this.serialize(io => tasksApi.publishTask(io, raw))
  }
  getTask(projectId: string, id: string): Task {
    return tasksApi.getTask(this.writeContext(), projectId, id)
  }
  listTasks(projectId: string, raw: unknown = {}): Task[] {
    return tasksApi.listTasks(this.writeContext(), projectId, raw)
  }
  updateTask(taskId: string, raw: unknown): Promise<Task> {
    return this.serialize(io => tasksApi.updateTask(io, taskId, raw))
  }
  completeTask(taskId: string, raw: unknown = {}): Promise<Task> {
    return this.serialize(io => tasksApi.completeTask(io, taskId, raw))
  }
  failTask(taskId: string, raw: unknown = {}): Promise<Task> {
    return this.serialize(io => tasksApi.failTask(io, taskId, raw))
  }

  // ── events 三法 ────────────────────────────────────────
  appendEvent(raw: unknown): Promise<SecEvent> {
    return this.serialize(async io => {
      const input = parseInput(eventsQueryApi.EventAppendInput, raw)
      if (!io.tables.projects.get(input.projectId)) {
        throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${input.projectId}`)
      }
      return this.writeEvent(
        input.projectId,
        input.kind,
        input.payload ?? {},
        input.sessionId ?? null,
      )
    })
  }
  queryEvents(projectId: string, raw: unknown = {}): SecEvent[] {
    const io = this.writeContext()
    if (!io.tables.projects.get(projectId)) {
      throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${projectId}`)
    }
    const projectEvents = [...io.tables.events.entries()]
      .map(([, v]) => v)
      .filter(e => e.project_id === projectId)
    return eventsQueryApi.queryEvents(projectEvents, raw)
  }
  latestEventCursor(projectId: string): string {
    const io = this.writeContext()
    if (!io.tables.projects.get(projectId)) {
      throw new BlackboardError('BB_NOT_FOUND', `项目不存在: ${projectId}`)
    }
    const projectEvents = [...io.tables.events.entries()]
      .map(([, v]) => v)
      .filter(e => e.project_id === projectId)
    return eventsQueryApi.latestEventCursor(projectEvents)
  }

  // ── approvals 四法 + 运行时核验二法 ────────────────────
  requestApproval(raw: unknown): Promise<Approval> {
    return this.serialize(io => approvalsApi.requestApproval(io, raw))
  }
  getApproval(id: string): Approval {
    return approvalsApi.getApproval(this.writeContext(), id)
  }
  listApprovals(projectId: string, raw: unknown = {}): Approval[] {
    return approvalsApi.listApprovals(this.writeContext(), projectId, raw)
  }
  decideApproval(approvalId: string, raw: unknown): Promise<Approval> {
    return this.serialize(io => approvalsApi.decideApproval(io, approvalId, raw))
  }

  /**
   * net=real 放行核验（供执行网关经 ctx.get('blackboard') 调用，非 Remote 动词）：
   * 经 serialize 排队，返回已批准且 action.kind=net.real 的最小形状。
   */
  verifyNetReal(approvalId: string): Promise<approvalsApi.NetRealApproval> {
    return this.serialize(io => approvalsApi.verifyNetReal(io, approvalId))
  }

  /** 消费审批（一次性；供执行网关跑完命令后调用）。 */
  consumeApproval(approvalId: string): Promise<void> {
    return this.serialize(io => approvalsApi.consumeApproval(io, approvalId))
  }
}

const noop = (): void => {}
