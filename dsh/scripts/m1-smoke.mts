// M1.1 冒烟（无 LLM 路径）：
// 引导 sec profile（剔除 dsh-web-app 层），验证威胁矩阵 + runCommand host 链路：
//   ①run_cmd 已注册 ②trusted/host 放行 ③untrusted/host 拒 ④非法 runtime 拒
//   ⑤非法 threat 按 malware_live 拒 ⑥net=real M1 拒 ⑦scratch/.tmp 隔离生效
//   ⑧wouldDeny 矩阵纯函数 ⑨brief 截断标注。
// 运行（在上游仓库根）：
//   pnpm exec tsx e:/ILOVCTRY/cyberstrike-pro/dsh/scripts/m1-smoke.mts
import { join } from 'node:path'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import {
  boot,
  readProfilePatches,
  createRuntimeResolution,
  type ProfileContext,
} from '@deepseek-ai/dsh-app-boot'
import { ToolCallId } from '@deepseek-ai/dsh-llm'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
const profileBootUrl = pathToFileURL(
  'E:/ILOVCTRY/deepseek-harness/apps/cli/src/profile-boot.ts',
).href
const { prepareProfile } = await import(profileBootUrl)

import type { RunCommandInput, SecCommandResult } from '../packages/sec/gateway/src/types.ts'
import { wouldDeny } from '../packages/sec/gateway/src/policy.ts'
import { buildBrief } from '../packages/sec/gateway/src/brief.ts'

interface SmokeShell {
  runCommand(input: RunCommandInput, signal?: AbortSignal): Promise<SecCommandResult>
}

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

// 与入口插件内 wsBound 同源：scratch=<profile>/workspace/scratch。
const wsRoot = join(profile.dir, 'workspace')
const HOST_BOUND = { scratch: join(wsRoot, 'scratch'), workspace: wsRoot, posix: false }
const DOCKER_BOUND = { scratch: '/workspace/scratch', workspace: '/workspace', posix: true }

const results: Record<string, unknown> = {}
let failures = 0
function check(name: string, condition: boolean, detail: unknown = null): void {
  results[name] = condition ? 'PASS' : { FAIL: detail }
  if (!condition) failures += 1
}
async function denyReason(input: RunCommandInput): Promise<string> {
  try {
    await (ctx.shell as unknown as SmokeShell).runCommand(input)
    return '<未拒绝>'
  } catch (error) {
    return error instanceof Error ? error.message : String(error)
  }
}
async function denyCode(input: RunCommandInput): Promise<{ code: string; message: string }> {
  try {
    await (ctx.shell as unknown as SmokeShell).runCommand(input)
    return { code: '<未拒绝>', message: '' }
  } catch (error) {
    const e = error as { code?: unknown; message?: string }
    return {
      code: typeof e.code === 'string' ? e.code : 'NO_CODE',
      message: e.message ?? String(error),
    }
  }
}

// M1.3：走工具注册面（经 tools/pre-execute 守卫）。
interface HookOutcome { isError: boolean; message: string; text: string }
let callSeq = 0
const hookAc = new AbortController()
async function hookRun(args: Record<string, unknown>): Promise<HookOutcome> {
  callSeq += 1
  const r = await ctx.tools.execute({
    signal: hookAc.signal,
    callId: ToolCallId(`m1.3-${callSeq}`),
    name: 'run_cmd',
    arguments: args,
  })
  return {
    isError: r.isError,
    message: r.error?.message ?? '',
    text: r.content.map(c => (c.type === 'text' ? c.text : '')).join(''),
  }
}

/** 拒因三方逐字一致：pre-execute 钩子 == runCommand 直调 == wouldDeny 纯函数。 */
async function denyTriple(
  name: string,
  args: { cmd: string; runtime?: string; threat_class?: string; net?: string | null },
  bound: { scratch: string; workspace: string; posix: boolean } | null,
): Promise<void> {
  const runtime = args.runtime ?? 'host'
  const threatClass = args.threat_class ?? 'trusted'
  const hook = await hookRun(args)
  const direct = await denyReason({
    cmd: args.cmd, runtime, threatClass, net: args.net ?? null,
  })
  const pure = wouldDeny({
    cmd: args.cmd, runtime, threatClass,
    net: args.net ?? null, workspace: bound,
  })
  check(
    name,
    hook.isError && hook.message !== '' && hook.message === direct && hook.message === pure,
    { hook: hook.message, direct, pure },
  )
}

/** wouldDeny 纯函数放行断言（不真执行，避免 nmap 等命令真跑）。 */
function pureAllow(
  name: string,
  cmd: string,
  runtime = 'host',
  bound: { scratch: string; workspace: string; posix: boolean } | null = HOST_BOUND,
): void {
  const reason = wouldDeny({ cmd, runtime, workspace: bound })
  check(name, reason === null, reason)
}

try {
  const shell = ctx.shell as unknown as SmokeShell

  // ① run_cmd 已注册
  check('run_cmd 已注册', ctx.tools.get('run_cmd') !== undefined)

  // ①' 生产 web 层必须禁掉裸 shell 工具（本引导剔除 web-app，故直接校验其 patch）
  const webAppPatch = readFileSync(
    'E:/ILOVCTRY/deepseek-harness/packages/bundle/web-app/cordis.patch.yml', 'utf8')
  check('生产层禁用 tool-bash/tool-pwsh',
    /- id: tool-bash\s*\n\s*disabled: true/.test(webAppPatch)
    && /- id: tool-pwsh\s*\n\s*disabled: true/.test(webAppPatch),
    'web-app patch 缺禁用条目')

  // ② trusted/host 放行
  const okRun = await shell.runCommand({ cmd: 'Write-Output sec-m1-ok', runtime: 'host', threatClass: 'trusted' })
  check('trusted/host 放行',
    okRun.ok && okRun.stdout.includes('sec-m1-ok') && okRun.exitCode === 0,
    { ok: okRun.ok, stdout: okRun.stdout, stderr: okRun.stderr })

  // ③ untrusted/host 拒
  const untrustedReason = await denyReason({ cmd: 'Write-Output x', runtime: 'host', threatClass: 'untrusted' })
  check('untrusted/host 拒', untrustedReason.includes('策略拒绝'), untrustedReason)

  // ④ 非法 runtime 拒
  const badRuntimeReason = await denyReason({ cmd: 'echo x', runtime: 'solaris', threatClass: 'trusted' })
  check('非法 runtime 拒', badRuntimeReason.includes('未知 runtime'), badRuntimeReason)

  // ⑤ 非法 threat 归 malware_live → host 被拒
  const badThreatReason = await denyReason({ cmd: 'echo x', runtime: 'host', threatClass: 'bogus' })
  check('非法 threat 按 malware_live 拒',
    // 归一化结果体现在允许集收敛为 sandbox（拒因文案与 Python 单源一致）。
    badThreatReason.includes('策略拒绝') && badThreatReason.includes('允许集: sandbox'),
    badThreatReason)

  // ⑥ net=real M1 拒（M2 注记）
  const realReason = await denyReason({ cmd: 'echo x', runtime: 'host', threatClass: 'trusted', net: 'real' })
  check('net=real M1 拒', realReason.includes('M2'), realReason)

  // ⑦ scratch/.tmp 隔离生效
  const isoRun = await shell.runCommand({
    cmd: '(Get-Location).Path; $env:TEMP',
    runtime: 'host', threatClass: 'trusted',
  })
  const norm = isoRun.stdout.replace(/\\/g, '/').toLowerCase()
  check('cwd=scratch', norm.includes('profiles/sec/workspace/scratch'), isoRun.stdout)
  check('TEMP=.tmp', norm.includes('profiles/sec/workspace/.tmp'), isoRun.stdout)

  // ⑦' 按 daemon 实时状态：停→SANDBOX_UNAVAILABLE 零降级；就绪→真跑成功
  let daemonUp: boolean
  {
    const { spawnSync } = await import('node:child_process')
    daemonUp = spawnSync('docker', ['version', '--format', '{{.Server.Version}}'], {
      windowsHide: true, timeout: 10_000,
    }).status === 0
  }
  if (daemonUp) {
    const dockerUpRun = await shell.runCommand({
      cmd: 'echo m1-daemon-up', runtime: 'docker', threatClass: 'untrusted',
    })
    check('docker daemon 就绪→真跑成功',
      dockerUpRun.ok && dockerUpRun.stdout.includes('m1-daemon-up'),
      { stdout: dockerUpRun.stdout, stderr: dockerUpRun.stderr })
    const sandboxUpRun = await shell.runCommand({
      cmd: 'echo m1-sandbox-up', runtime: 'sandbox', threatClass: 'untrusted',
    })
    check('sandbox daemon 就绪→真跑成功',
      sandboxUpRun.ok && sandboxUpRun.stdout.includes('m1-sandbox-up'),
      { stdout: sandboxUpRun.stdout, stderr: sandboxUpRun.stderr })
  } else {
    const dockerDown = await denyCode({ cmd: 'uname -a', runtime: 'docker', threatClass: 'trusted' })
    check('docker daemon 停→UNAVAILABLE',
      dockerDown.code === 'SANDBOX_UNAVAILABLE' && dockerDown.message.includes('daemon'),
      dockerDown)
    const sandboxDown = await denyCode({ cmd: 'echo x', runtime: 'sandbox', threatClass: 'untrusted' })
    check('sandbox daemon 停→UNAVAILABLE',
      sandboxDown.code === 'SANDBOX_UNAVAILABLE', sandboxDown)
  }
  // malware_live 走 docker（L2）必须被矩阵拒绝，不触沙箱
  const malwareL2 = await denyReason({ cmd: 'echo x', runtime: 'docker', threatClass: 'malware_live' })
  check('malware_live 拒 L2', malwareL2.includes('策略拒绝'), malwareL2)

  // ⑧ wouldDeny 矩阵纯函数（三威胁类 × 关键 runtime）
  check('trusted→host/wsl 放行',
    wouldDeny({ cmd: 'x', runtime: 'host' }) === null
    && wouldDeny({ cmd: 'x', runtime: 'wsl' }) === null)
  check('untrusted→docker/sandbox 放行',
    wouldDeny({ cmd: 'x', runtime: 'docker', threatClass: 'untrusted' }) === null
    && wouldDeny({ cmd: 'x', runtime: 'sandbox', threatClass: 'untrusted' }) === null)
  check('malware_live→仅 sandbox',
    wouldDeny({ cmd: 'x', runtime: 'sandbox', threatClass: 'malware_live' }) === null
    && wouldDeny({ cmd: 'x', runtime: 'docker', threatClass: 'malware_live' })?.includes('策略拒绝') === true)
  check('unknown→malware_live',
    wouldDeny({ cmd: 'x', runtime: 'host', threatClass: 'unknown' })?.includes('允许集: sandbox') === true
    && wouldDeny({ cmd: 'x', runtime: 'sandbox', threatClass: 'unknown' }) === null)
  check('net=fakenet M1 拒',
    wouldDeny({ cmd: 'x', runtime: 'host', net: 'fakenet' })?.includes('M2') === true)

  // ⑨ brief 截断标注
  const longText = '好'.repeat(3000)
  const briefLong = buildBrief({
    runtime: 'host', exitCode: 0, stdout: longText, stderr: '',
    timedOut: false, interrupted: false,
  })
  check('stdout 截断标注',
    briefLong.includes('stdout 已截断：2000/3000'), briefLong.slice(-120))
  const briefShort = buildBrief({
    runtime: 'host', exitCode: 1, stdout: '', stderr: 'boom',
    timedOut: false, interrupted: false,
  })
  check('短输出不截断',
    briefShort.includes('[host] exit=1') && briefShort.includes('[stderr]') && !briefShort.includes('已截断'),
    briefShort)

  // ⑩ M1.3 pathguard：拒例三方逐字一致（host 风味）
  await denyTriple('pathguard 拒 Windows 绝对路径',
    { cmd: 'Write-Output x > C:/Windows/Temp/sec-m1-a' }, HOST_BOUND)
  await denyTriple('pathguard 拒 UNC 路径',
    { cmd: 'Write-Output x > \\\\server\\share\\a' }, HOST_BOUND)
  await denyTriple('pathguard 拒 ~ 路径',
    { cmd: 'Write-Output x > ~/.x' }, HOST_BOUND)
  await denyTriple('pathguard 拒相对路径上穿',
    { cmd: 'Write-Output x > ../../m1-outside.txt' }, HOST_BOUND)
  await denyTriple('pathguard 拒 -o 写盘',
    { cmd: 'nmap -o C:/Windows/Temp/m1-outside 10.0.0.1' }, HOST_BOUND)
  await denyTriple('pathguard 拒 Out-File',
    { cmd: 'Out-File D:/m1-x' }, HOST_BOUND)
  await denyTriple('pathguard 拒 tee',
    { cmd: 'Get-Date | tee E:/m1-tee' }, HOST_BOUND)
  await denyTriple('pathguard 拒 nmap -oG',
    { cmd: 'nmap -oG C:/Windows/Temp/x 10.0.0.1' }, HOST_BOUND)
  // docker posix 风味（拒于 daemon 探测之前，无需镜像）
  await denyTriple('pathguard 拒容器写 /etc',
    { cmd: 'echo x > /etc/passwd', runtime: 'docker', threat_class: 'untrusted' }, DOCKER_BOUND)

  // ⑪ M1.3 pathguard：放例（纯函数，不真执行）
  pureAllow('pathguard 放相对路径写', 'echo x > a.log')
  pureAllow('pathguard 放 $ 变量目标', 'echo x > $m1var')
  pureAllow('pathguard 放 % 变量目标', 'echo x > %TMP%/x')
  pureAllow('pathguard 放 &1', 'echo x 2>&1')
  pureAllow('pathguard 放 /dev/null（引号外截断）', 'echo err 2>/dev/null; echo done')
  pureAllow('pathguard 放 NUL', 'echo x > NUL')
  pureAllow('pathguard 放 $null', 'Write-Output x > $null')
  pureAllow('pathguard 放 URL', 'echo x > https://example.com/f')
  pureAllow('pathguard 放 grep -o', 'grep -o pat f.txt')
  pureAllow('pathguard 放 rg -o', 'rg -o pat f')
  pureAllow('pathguard 放引号内分隔符', "echo x > 'a;b.log'")
  pureAllow('pathguard 放容器内 scratch 绝对路径',
    'echo x > /workspace/scratch/a.log', 'docker', DOCKER_BOUND)
  pureAllow('pathguard 放容器内相对路径', 'echo x > a.log', 'docker', DOCKER_BOUND)

  // ⑫ M1.3 rateguard：拒例三方逐字一致 + 放行配方
  const nmapFull = await hookRun({ cmd: 'nmap -p- 10.0.0.1' })
  await denyTriple('rateguard 拒 nmap -p-', { cmd: 'nmap -p- 10.0.0.1' }, HOST_BOUND)
  check('rateguard nmap 拒因带配方',
    nmapFull.isError && nmapFull.message.includes('--max-rate'), nmapFull.message)
  await denyTriple('rateguard 拒 nmap 0-65535',
    { cmd: 'nmap -p0-65535 10.0.0.1' }, HOST_BOUND)
  await denyTriple('rateguard 拒 nmap -65535',
    { cmd: 'nmap -p-65535 10.0.0.1' }, HOST_BOUND)
  await denyTriple('rateguard 拒 masscan 高速',
    { cmd: 'masscan 10.0.0.0/24 --rate 5000' }, HOST_BOUND)
  const masscanMsg = await denyReason({ cmd: 'masscan x --rate 5000' })
  check('rateguard masscan 拒因带阈值', masscanMsg.includes('1000'), masscanMsg)
  await denyTriple('rateguard 拒 ffuf 无限速',
    { cmd: 'ffuf -u http://x -w words.txt' }, HOST_BOUND)
  check('rateguard ffuf 拒因带通用配方',
    (await denyReason({ cmd: 'ffuf -u x -w w' })).includes('-rl 50'), 'ffuf')
  await denyTriple('rateguard 拒 hydra 无并发',
    { cmd: 'hydra -l admin -P p.txt ssh://10.0.0.1' }, HOST_BOUND)
  check('rateguard hydra 拒因带配方',
    (await denyReason({ cmd: 'hydra -l a -P p ssh://x' })).includes('-t'), 'hydra')

  // ⑬ M1.3 rateguard：放例
  pureAllow('rateguard 放 nmap -T2', 'nmap -p- -T2 10.0.0.1')
  pureAllow('rateguard 放 nmap --max-rate', 'nmap -p- --max-rate 100 10.0.0.1')
  pureAllow('rateguard 放 nmap --max-parallelism', 'nmap -p- --max-parallelism 10 10.0.0.1')
  pureAllow('rateguard 放 nmap --scan-delay', 'nmap -p- --scan-delay 50ms 10.0.0.1')
  pureAllow('rateguard 放 masscan --rate 1000', 'masscan x --rate 1000')
  pureAllow('rateguard 放 masscan --rate 500', 'masscan x --rate 500')
  pureAllow('rateguard 放 ffuf -rate', 'ffuf -u http://x -w w -rate 50')
  pureAllow('rateguard 放 ffuf -rl', 'ffuf -u http://x -w w -rl 20')
  pureAllow('rateguard 放 ffuf -t（旧版限并发）', 'ffuf -u http://x -w w -t 5')
  pureAllow('rateguard 放 hydra -t 4', 'hydra -l a -P p ssh://x -t 4')

  // ⑭ pre-execute 合规命令真放行（host，写 scratch）
  const guardAllow = await hookRun({
    cmd: 'Write-Output guard-ok > guard-m1.log', runtime: 'host', threat_class: 'trusted',
  })
  check('pre-execute 合规命令放行',
    !guardAllow.isError && guardAllow.text.includes('[host] exit=0'), guardAllow)

  console.log(JSON.stringify({ results, failures }, null, 2))
} finally {
  await ctx.fiber.dispose()
  void resolution
}
if (failures > 0) process.exit(1)
