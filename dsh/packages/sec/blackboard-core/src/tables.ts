/**
 * 表句柄结构类型（逻辑模块经此消费表，不直接碰 Domain）。
 */
import type { Project, Asset, Finding, Task, SecEvent, Approval } from './types'

/** KvTable 结构子集（对齐 storage-domain KvTable）。 */
export interface KvTableLike<K, V> {
  get(key: K): V | undefined
  entries(): IterableIterator<[K, V]>
  keys(): IterableIterator<K>
  readonly size: number
  put(key: K, value: V): Promise<void>
  delete(key: K): Promise<boolean>
  update(key: K, fn: (current: V) => V): Promise<V>
}

/** Domain global 结构。 */
export interface GlobalLike<G> {
  get(): G
  set(value: G): Promise<void>
}

/** sec 域六表句柄袋。 */
export interface SecTables {
  projects: KvTableLike<string, Project>
  assets: KvTableLike<string, Asset>
  findings: KvTableLike<string, Finding>
  tasks: KvTableLike<string, Task>
  events: KvTableLike<string, SecEvent>
  approvals: KvTableLike<string, Approval>
}

/**
 * 写工作上下文：逻辑模块在 serialize work 内使用。
 * appendEvent 直写（不再串行化——调用方已持链）。
 */
export interface WriteContext {
  tables: SecTables
  global: GlobalLike<{ eventSeq: number }>
  appendEvent: (
    projectId: string,
    kind: string,
    payload: Record<string, unknown>,
    sessionId?: string | null,
  ) => Promise<SecEvent>
}
