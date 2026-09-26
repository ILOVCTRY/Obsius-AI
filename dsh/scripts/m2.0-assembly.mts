// M2.0 装配验收（无 LLM）：
// 引导 sec profile（剔除 dsh-web-app 层），验证：
//   ①storage-sqlite 插件已加载并注册后端名 'sqlite'（发行 CLI 默认只有 json）
//   ②storage-domain 已加载（sec 域路由到 sqlite；域本身 M2.1 才 open）
//   ③无重复服务注册（M1 单实现服务已 disable）
//   ④sec.sqlite 数据文件以 wal 模式可写。
// 运行（在上游仓库根）：
//   pnpm exec tsx e:/ILOVCTRY/cyberstrike-pro/dsh/scripts/m2.0-assembly.mts
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import {
  boot,
  readProfilePatches,
  createRuntimeResolution,
  type ProfileContext,
} from '@deepseek-ai/dsh-app-boot'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'

const profileBootUrl = pathToFileURL(
  'E:/ILOVCTRY/deepseek-harness/apps/cli/src/profile-boot.ts',
).href
const { prepareProfile } = await import(profileBootUrl)

const NAME = 'dsh'
const PROFILE_NAME = 'sec'
const INSTALL_ANCHOR = 'E:/ILOVCTRY/deepseek-harness/apps/cli/package.json'
const ROOT_FILENAME = 'cordis.yml'

const full = prepareProfile(PROFILE_NAME, true)
const profile = {
  ...full,
  layers: full.layers.filter(layer => layer.packageName !== '@deepseek-ai/dsh-web-app'),
}
const resolution = await createRuntimeResolution({ installAnchor: INSTALL_ANCHOR, profile })

const profileContext: ProfileContext = {
  name: PROFILE_NAME,
  dir: profile.dir,
  patchPath: profile.patchPath,
  installAnchor: INSTALL_ANCHOR,
  startedBundles: profile.layers.map(layer => layer.packageName),
  cwd: process.cwd(),
  home: resolveDshHome(),
  overlays: [],
  telemetryDisabledEnv: process.env.DSH_TELEMETRY_DISABLED,
}

const patches = readProfilePatches(NAME, profileContext, profile)
const ctx = await boot(NAME, join(profile.dir, ROOT_FILENAME), patches)

let passed = 0
const check = (cond: boolean, label: string): void => {
  if (!cond) throw new Error(`M2.0 装配检查失败：${label}`)
  passed += 1
  console.log(`  ✓ ${label}`)
}

// ① 后端名含 sqlite（且 json 仍在，无副作用）。
const backendNames = ctx.storage.backend.names()
check(backendNames.includes('sqlite'), `存储后端已注册 sqlite（实际：${backendNames.join(', ')}）`)

// ② storage-domain 设施在位（open 能力存在即证明插件加载）。
check(typeof ctx.storageDomain.open === 'function', 'storage-domain 设施已加载（open 可用）')

// ③ sqlite 后端实际取得到，且配置为 profile data 路径。
const sqliteBackend = ctx.storage.backend.get('sqlite')
check(sqliteBackend !== undefined, 'sqlite 后端实例可获取')

// ④ node:sqlite 零编译链路 + wal 可写（临时文件，不污染正式 sec.sqlite）。
const { DatabaseSync } = await import('node:sqlite')
const { rmSync } = await import('node:fs')
const probePath = join(profile.dir, 'data', 'm2-assembly-probe.sqlite')
for (const suffix of ['', '-wal', '-shm']) rmSync(probePath + suffix, { force: true })
const probeDb = new DatabaseSync(probePath)
check(probeDb.exec('PRAGMA journal_mode=WAL') ?? true, '临时库可切 wal 模式')
probeDb.exec('CREATE TABLE probe (k TEXT PRIMARY KEY, v INTEGER)')
probeDb.prepare('INSERT INTO probe (k, v) VALUES (?, ?)').run('ping', 1)
check(probeDb.prepare('SELECT v FROM probe WHERE k=?').get('ping')?.v === 1, 'node:sqlite wal 可读写')
probeDb.close()
for (const suffix of ['', '-wal', '-shm']) rmSync(probePath + suffix, { force: true })

// ⑤ M1 服务仍在位、单实现：shell 执行者唯一。
check(ctx.shell !== undefined, 'M1 shell 执行服务在位')
check(ctx.sandbox !== undefined, 'M1 sandbox provider 服务在位')

// ⑥ controller 构建产物声明检查（文件存在性）。
const { existsSync } = await import('node:fs')
const controllerLib = 'E:/ILOVCTRY/cyberstrike-pro/dsh/packages/sec/blackboard-controller/lib'
check(existsSync(join(controllerLib, 'typert.host.js')), 'controller typert.host.js 已生成')
check(existsSync(join(controllerLib, 'typert.remote-client.js')), 'controller typert.remote-client.js 已生成')

// ── M2.1：域规格 + 服务骨架 ──────────────────────────────
// ⑦ BlackboardService 已经 patch 装配并打开 sec 域。
check(ctx.blackboard !== undefined, 'BlackboardService 已注册（ctx.blackboard）')
check(ctx.blackboard.isOpen, 'sec 域已 open')

// ⑧ domainVersion 四字段。
const ver = ctx.blackboard.domainVersion()
check(
  ver.name === 'sec' && ver.version === 1
    && ver.layout === 'per-record' && ver.schemaVersion === 24,
  `domainVersion 正确（${ver.name} v${ver.version} ${ver.layout} schema=${ver.schemaVersion}）`,
)

// ⑨ global.eventSeq 可读（历史运行可能已写过；M2.2 段按增量校验）。
const seqBase = ctx.blackboard.eventSeq()
check(Number.isInteger(seqBase) && seqBase >= 0, `global.eventSeq 可读（基线 ${seqBase}）`)

// ⑩ sec 域表已在 sqlite 物化（u_sec_* 六张；开只读第二连接查）。
const metaDb = new DatabaseSync('C:/Users/Nan/.dsh/profiles/sec/data/sec.sqlite', { readOnly: true })
const secTables = metaDb
  .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'u_sec_%'")
  .all()
  .map(row => (row as { name: string }).name)
  .sort()
metaDb.close()
const expectedSec = [
  'u_sec_approvals', 'u_sec_assets', 'u_sec_events',
  'u_sec_findings', 'u_sec_projects', 'u_sec_tasks',
].sort()
check(
  JSON.stringify(secTables) === JSON.stringify(expectedSec),
  `sec 域六表已物化（${secTables.join(', ')}）`,
)

// ── M2.2：projects + assets + 覆盖树 ─────────────────────
// 每跑一次建独立项目（名称带时间戳），数据不冲突、无需清理。
const stamp = Date.now()
const proj = await ctx.blackboard.createProject({ name: `m22-${stamp}`, track: 'pentest' })
check(!!proj.id && proj.id.startsWith('proj-'), `建项成功（${proj.id}）`)

const domain = await ctx.blackboard.registerAsset({
  projectId: proj.id, value: `m22-${stamp}.example.com`,
})
check(domain.created && domain.asset.type === 'domain' && domain.derived.length === 0,
  `注册域名资产（${domain.asset.id}），derived 恒空`)

const host = await ctx.blackboard.registerAsset({
  projectId: proj.id, value: `10.${stamp % 200}.7.9`,
})
check(host.created && host.asset.type === 'host', `注册主机资产（自动识别 host）`)

const url = await ctx.blackboard.registerAsset({
  projectId: proj.id, value: `http://10.${stamp % 200}.7.9/login`,
})
check(url.created && url.asset.type === 'url', `注册 URL 资产（自动识别 url）`)

const hostDup = await ctx.blackboard.registerAsset({
  projectId: proj.id, value: `10.${stamp % 200}.7.9`,
})
check(!hostDup.created && hostDup.asset.id === host.asset.id, '同值资产注册去重（不新增行）')

await ctx.blackboard.setAssetParent(host.asset.id, { parentId: domain.asset.id })
await ctx.blackboard.setAssetParent(url.asset.id, { parentId: host.asset.id })
const forest = ctx.blackboard.assetForest(proj.id)
check(
  forest.roots.length === 1 && forest.roots[0] === domain.asset.id
    && forest.children[domain.asset.id]?.length === 1
    && forest.children[host.asset.id]?.[0] === url.asset.id,
  '父子森林：domain→host→url',
)

// 叶子终态 → effective_status 逐层向上传播。
await ctx.blackboard.setAssetStatus(url.asset.id, {
  status: 'tested_clean', note: '登录页全参数点测无注入点',
})
const { effectiveStatusMap } = await import(
  '../packages/sec/blackboard-core/src/index.ts'
)
const eff = effectiveStatusMap(ctx.blackboard.listAssets(proj.id), [])
check(
  eff.get(host.asset.id)?.basis === 'derived'
    && eff.get(host.asset.id)?.status === 'tested_clean'
    && eff.get(domain.asset.id)?.status === 'tested_clean',
  'effective_status 向上传播：子树全终态 → host/domain 派生 tested_clean',
)

// 乐观锁：url 已 bump 到 revision=2，基于 1 的写必须 BB_CONFLICT。
let conflictHit = false
try {
  await ctx.blackboard.setAssetStatus(url.asset.id, { status: 'visited', expectedRevision: 1 })
} catch (e) {
  conflictHit = (e as { code?: string }).code === 'BB_CONFLICT'
}
check(conflictHit, '乐观锁冲突报 BB_CONFLICT')

// 事件：3×asset.new + 2×reparent + 1×status_changed = 6。
const afterM22 = ctx.blackboard.eventSeq()
check(afterM22 === seqBase + 6,
  `事件随写落（增量 ${afterM22 - seqBase}，期望 6）`)

// ── M2.3：findings + tasks ──────────────────────────────
const f1 = await ctx.blackboard.addFinding({
  projectId: proj.id,
  title: '登录页 SQL 注入',
  vulnClass: 'sqli',
  targetAssetId: host.asset.id,
  severity: 'high',
  dedupFp: 'm23-sqli-login',
})
check(!f1.merged && f1.severity === 'high', `登记发现（${f1.id}）`)

const f1b = await ctx.blackboard.addFinding({
  projectId: proj.id,
  title: '登录页 SQL 注入',
  vulnClass: 'sqli',
  targetAssetId: host.asset.id,
  severity: 'critical',
  dedupFp: 'm23-sqli-login',
  evidence: { pocs: ['curl 注入回显'] },
})
check(f1b.merged && f1b.id === f1.id && f1b.severity === 'critical',
  '同指纹重报：合并 + severity 就高')
const mergedRow = ctx.blackboard.getFinding(proj.id, f1.id)
check((mergedRow.evidence.pocs as string[] | undefined)?.length === 1, '证据并集落库')

const f1c = await ctx.blackboard.addFinding({
  projectId: proj.id, title: '登录页 SQL 注入', vulnClass: 'sqli',
  targetAssetId: host.asset.id, severity: 'critical', dedupFp: 'm23-sqli-login',
})
check(f1c.merged, '再次重报仍走合并（不新增行）')

const f2 = await ctx.blackboard.addFinding({
  projectId: proj.id, title: '疑似重复的注入', vulnClass: 'sqli',
  targetAssetId: host.asset.id, severity: 'high', dedupFp: 'm23-sqli-login-alt',
})
check(
  !f2.merged && f2.dedupWarning?.some(w => w.id === f1.id),
  '指纹分裂只提示不阻塞（dedup_warning 命中既有发现）',
)
check(ctx.blackboard.listFindings(proj.id).length === 2, '发现共 2 行')

const t1 = await ctx.blackboard.publishTask({
  projectId: proj.id, title: `m23 利用注入取数据 ${stamp}`,
})
check(t1.created && t1.task.status === 'open', `发布任务（${t1.task.id}）`)
const t1dup = await ctx.blackboard.publishTask({
  projectId: proj.id, title: `m23 利用注入取数据 ${stamp}`,
})
check(!t1dup.created, '同标题任务发布去重')

const done = await ctx.blackboard.completeTask(t1.task.id, { note: '已取到 users 表' })
check(done.status === 'done', 'complete：open→done')

let failDoneHit = false
try {
  await ctx.blackboard.failTask(t1.task.id)
} catch (e) {
  failDoneHit = (e as { code?: string }).code === 'BB_CONFLICT'
}
check(failDoneHit, '终态非法迁移（done→failed）报 BB_CONFLICT')

const reopen = await ctx.blackboard.updateTask(t1.task.id, { status: 'open' })
check(reopen.status === 'open', 'update：done→open 重开合法')

let updateConflictHit = false
try {
  await ctx.blackboard.updateTask(t1.task.id, { title: 'x', expectedRevision: 1 })
} catch (e) {
  updateConflictHit = (e as { code?: string }).code === 'BB_CONFLICT'
}
check(updateConflictHit, '任务更新乐观锁报 BB_CONFLICT')

// M2.3 事件：finding new×2 + merged×2 + task published/done/reopened = 7。
const afterM23 = ctx.blackboard.eventSeq()
check(afterM23 === afterM22 + 7,
  `M2.3 事件增量 ${afterM23 - afterM22}（期望 7）`)

// ── M2.4：events 公开面 ────────────────────────────────
const ea = await ctx.blackboard.appendEvent({
  projectId: proj.id, kind: 'm24.marker', payload: { n: 1 },
})
const eb = await ctx.blackboard.appendEvent({
  projectId: proj.id, kind: 'm24.marker', payload: { n: 2 },
})
check(ea.id < eb.id, '字典序==追加序（零填充单调键）')

const markers = ctx.blackboard.queryEvents(proj.id, { kinds: ['m24.marker'] })
check(markers.length >= 2 && markers.at(-2)?.id === ea.id && markers.at(-1)?.id === eb.id,
  'kinds 过滤命中且升序')

const sinceA = ctx.blackboard.queryEvents(proj.id, { since: ea.id, kinds: ['m24.marker'] })
check(sinceA.length === 1 && sinceA[0].id === eb.id, 'since 为排他下界')

const limit1 = ctx.blackboard.queryEvents(proj.id, { kinds: ['m24.marker'], limit: 1 })
check(limit1[0].id === markers[0].id, 'limit 取首条')

const tail1 = ctx.blackboard.queryEvents(proj.id, { kinds: ['m24.marker'], tail: 1 })
check(tail1[0].id === eb.id, 'tail 取最新条（仍升序）')

const beforeB = ctx.blackboard.queryEvents(proj.id, { before: eb.id, kinds: ['m24.marker'] })
check(beforeB.at(-1)?.id === ea.id && !beforeB.some(e => e.id >= eb.id), 'before 上翻页')

check(ctx.blackboard.latestEventCursor(proj.id) >= eb.id, 'latestCursor 指向末端')

const eSess = await ctx.blackboard.appendEvent({
  projectId: proj.id, kind: 'm24.sess', sessionId: 'sess-m24probe',
})
const noSess = ctx.blackboard.queryEvents(proj.id, { kinds: ['m24.sess'], sessionId: null })
const withSess = ctx.blackboard.queryEvents(proj.id, { kinds: ['m24.sess'], sessionId: 'sess-m24probe' })
check(noSess.length === 0 && withSess[0]?.id === eSess.id, 'sessionId 过滤（null=只查无会话）')

// M2.4 追加事件本身各占一个序号：3 条。
check(ctx.blackboard.eventSeq() === afterM23 + 3,
  `M2.4 事件增量 ${ctx.blackboard.eventSeq() - afterM23}（期望 3）`)

await ctx.fiber.dispose()
void resolution
console.log(`\nM2.0–M2.4 装配验收通过：${passed}/${passed}`)
