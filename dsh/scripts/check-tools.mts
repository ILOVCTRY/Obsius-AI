// M0 尖兵验证（无 LLM 路径）：
// 引导 sec profile，但剔除 dsh-web-app bundle 层（免起 HTTP 服务、免模型密钥），
// 直接枚举 ctx.tools 注册表，并本地执行一次 sec_hello。
// 运行（在上游仓库根，借其 node_modules 解析）：
//   pnpm exec tsx e:/ILOVCTRY/cyberstrike-pro/dsh/scripts/check-tools.mts
import { join } from 'node:path'
import {
  boot,
  readProfilePatches,
  createRuntimeResolution,
  type ProfileContext,
} from '@deepseek-ai/dsh-app-boot'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
// prepareProfile 属于 CLI 自身（不在 app-boot），直接引源码。
import { pathToFileURL } from 'node:url'
const profileBootUrl = pathToFileURL(
  'E:/ILOVCTRY/deepseek-harness/apps/cli/src/profile-boot.ts',
).href
const { prepareProfile } = await import(profileBootUrl)

const NAME = 'dsh'
const PROFILE_NAME = 'sec'
// 与上游 apps/cli/src/profile-boot.ts 的 INSTALL_ANCHOR 等价。
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
try {
  const names = ctx.tools.schemas().map(schema => schema.name)
  const secHello = ctx.tools.get('sec_hello')
  let executed: unknown = null
  if (secHello !== undefined) {
    // sec-toy 的 execute 不使用 exec 上下文。
    executed = await secHello.execute({ who: 'sec' }, undefined as never)
  }
  // M1.0：单实现 seam 应已替换为我们的类。
  console.log(JSON.stringify({
    bundlesLoaded: profile.layers.map(layer => layer.packageName),
    toolCount: names.length,
    toolNames: names,
    hasSecHello: names.includes('sec_hello'),
    executed,
    shell: {
      constructor: ctx.shell.constructor.name,
      sandboxMode: ctx.shell.sandboxMode,
    },
    sandbox: {
      constructor: ctx.sandbox.constructor.name,
    },
  }, null, 2))
} finally {
  await ctx.fiber.dispose()
  void resolution
}
