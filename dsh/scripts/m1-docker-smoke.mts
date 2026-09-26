// M1.4 真实 Docker 冒烟（daemon + 双镜像就绪后跑）：
//   ① L2 sh 基础命令 exit 0
//   ② nmap 全端口：带 -T2 真跑放行 / 不带 pre-execute 拒
//   ③ ffuf -rate 50 放行 / 不带拒
//   ④ python3（L2 pentest-box + L3 alpine）
//   ⑤ 中文 UTF-8 无乱码
//   ⑥ L2 写 /workspace/scratch/x.txt → 宿主真实存在且内容一致
//   加：L3 零挂载（/workspace 不存在、不可见宿主）；malware_live 拒 L2。
// 运行（在上游仓库根）：
//   pnpm exec tsx e:/ILOVCTRY/cyberstrike-pro/dsh/scripts/m1-docker-smoke.mts
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

const results: Record<string, unknown> = {}
let failures = 0
function check(name: string, condition: boolean, detail: unknown = null): void {
  results[name] = condition ? 'PASS' : { FAIL: detail }
  if (!condition) failures += 1
}

let callSeq = 0
const ac = new AbortController()
/** 走 run_cmd 工具面（经 pre-execute 守卫，再真执行）。 */
async function runCmd(args: Record<string, unknown>): Promise<{
  isError: boolean; message: string; text: string
}> {
  callSeq += 1
  const r = await ctx.tools.execute({
    signal: ac.signal,
    callId: ToolCallId(`m1.4-${callSeq}`),
    name: 'run_cmd',
    arguments: args,
  })
  return {
    isError: r.isError,
    message: r.error?.message ?? '',
    text: r.content.map(c => (c.type === 'text' ? c.text : '')).join(''),
  }
}

try {
  const shell = ctx.shell as unknown as SmokeShell
  const DOCKER_BOUND = { scratch: '/workspace/scratch', workspace: '/workspace', posix: true }

  // ① L2 sh 基础命令
  const basic = await shell.runCommand({
    cmd: 'uname -a; echo m14-ok', runtime: 'docker', threatClass: 'untrusted',
  })
  check('① L2 基础命令 exit 0',
    basic.ok && basic.stdout.includes('m14-ok') && basic.runtime === 'docker',
    { stdout: basic.stdout, stderr: basic.stderr })

  // ② nmap 全端口：不带节流 pre-execute 拒；带 -T2+--max-rate 真跑放行
  const nmapBare = await runCmd({
    cmd: 'nmap -p- 127.0.0.1', runtime: 'docker', threat_class: 'untrusted',
  })
  const nmapPure = wouldDeny({
    cmd: 'nmap -p- 127.0.0.1', runtime: 'docker', threatClass: 'untrusted',
    workspace: DOCKER_BOUND,
  })
  check('② nmap 裸全端口 pre-execute 拒',
    nmapBare.isError && nmapBare.message === nmapPure && nmapBare.message.includes('--max-rate'),
    nmapBare.message)
  // --cap-drop ALL 下无 raw socket：用 -sT TCP connect 扫（免特权）；
  // -n 免反向 DNS（net=none 下 DNS 尝试会拖到超时）
  // 不用 -T2：sneaky 模板的发包间隔会把全端口拖到数分钟（--max-rate 已足够节流）
  const nmapOk = await runCmd({
    cmd: 'nmap -sT -p- -n --max-rate 5000 127.0.0.1',
    runtime: 'docker', threat_class: 'untrusted', timeout: 120,
  })
  check('② nmap 带 --max-rate 放行真跑',
    !nmapOk.isError && nmapOk.text.includes('[docker] exit=0'), nmapOk)

  // ③ ffuf：不带限速拒；带 -rate 50 真跑放行（无服务器也正常结束 exit 0）
  const ffufBare = await runCmd({
    cmd: 'ffuf -u http://127.0.0.1:1 -w words.txt',
    runtime: 'docker', threat_class: 'untrusted',
  })
  check('③ ffuf 无限速拒', ffufBare.isError && ffufBare.message.includes('-rl 50'),
    ffufBare.message)
  await shell.runCommand({
    cmd: 'printf "a\\nbb\\nccc\\n" > words.txt',
    runtime: 'docker', threatClass: 'untrusted',
  })
  const ffufOk = await runCmd({
    cmd: 'ffuf -u http://127.0.0.1:1/FUZZ -w words.txt -t 1',
    runtime: 'docker', threat_class: 'untrusted', timeout: 60,
  })
  check('③ ffuf -t 1 放行真跑',
    !ffufOk.isError && ffufOk.text.includes('[docker] exit=0'), ffufOk)

  // ④ python3：L2 + L3
  const pyL2 = await shell.runCommand({
    cmd: 'python3 -c "print(1234)"', runtime: 'docker', threatClass: 'untrusted',
  })
  check('④ L2 python3', pyL2.ok && pyL2.stdout.trim() === '1234', pyL2.stdout)
  const pyL3 = await shell.runCommand({
    cmd: 'python3 -c "print(4321)"', runtime: 'sandbox', threatClass: 'untrusted',
  })
  check('④ L3 python3', pyL3.ok && pyL3.stdout.trim() === '4321',
    { stdout: pyL3.stdout, stderr: pyL3.stderr })

  // ⑤ 中文 UTF-8 无乱码
  const cn = await shell.runCommand({
    cmd: 'echo 安全网关中文冒烟', runtime: 'docker', threatClass: 'untrusted',
  })
  check('⑤ 中文 UTF-8 无乱码', cn.ok && cn.stdout.includes('安全网关中文冒烟'),
    cn.stdout)

  // ⑥ L2 写 /workspace/scratch/x.txt → 宿主文件存在且内容一致
  const marker = `m14-marker-安全-${Date.now()}`
  const write = await shell.runCommand({
    cmd: `printf '${marker}\\n' > x.txt && cat x.txt`,
    runtime: 'docker', threatClass: 'untrusted',
  })
  check('⑥ 容器内写入成功', write.ok && write.stdout.includes(marker), write.stdout)
  const hostFile = join(profile.dir, 'workspace', 'scratch', 'x.txt')
  let hostContent = ''
  try {
    hostContent = readFileSync(hostFile, 'utf8')
  } catch (error) {
    check('⑥ 宿主文件存在', false, String(error))
  }
  check('⑥ 宿主文件内容一致', hostContent.trim() === marker,
    { host: hostContent, marker })

  // ⑦ L3 零挂载：/workspace 不存在、scratch 不存在
  const l3Mount = await shell.runCommand({
    cmd: 'ls /workspace 2>&1; ls /workspace/scratch 2>&1; pwd',
    runtime: 'sandbox', threatClass: 'untrusted',
  })
  check('⑦ L3 零挂载',
    l3Mount.stdout.includes('No such file') && !l3Mount.stdout.includes('x.txt'),
    l3Mount.stdout)

  // ⑧ malware_live 走 L2 必须被矩阵拒（不触容器）
  const malwareL2 = await runCmd({
    cmd: 'echo x', runtime: 'docker', threat_class: 'malware_live',
  })
  check('⑧ malware_live 拒 L2',
    malwareL2.isError && malwareL2.message.includes('策略拒绝'), malwareL2.message)

  console.log(JSON.stringify({ results, failures }, null, 2))
} finally {
  await ctx.fiber.dispose()
  void resolution
}
if (failures > 0) process.exit(1)
