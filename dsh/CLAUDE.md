# dsh/

cyberstrike-pro 适配 **DeepSeek Harness (dsh)** 的插件工作区。方案：[../docs/plans/security-dsh-distribution.md](../docs/plans/security-dsh-distribution.md)。**M0 尖兵 + M1 网关底座均已落地（2026-09-26，未提交）**。

## 关键事实

- 上游 dsh 在仓库外：`E:\ILOVCTRY\deepseek-harness`，钉版 commit **477b4f4**；不改上游源码，代码全在本目录。
- profile 名 **sec**（dsh-base + dsh-web-app），目录 `$DSH_HOME/profiles/sec/`；外部包经 `pnpm dsh plugin --profile sec add <包路径>` 链入，加载行写 profile 的 `cordis.patch.yml`。
- `@deepseek-ai/*` 一律 peerDependencies（profile 侧 hoist 解析），禁自带副本；tsdown external 屏蔽，防双副本。
- 工具链坑：TS6 不自动加载 @types/*（tsconfig `types:["node"]`）；tsdown `fixedExtension:false` 出 .js；build=`tsdown && tsc`；tsx 跑源码。

## 目录与包

- `sec-toy/` — M0 最小样例（工具 `sec_hello`），长期保留。
- `packages/sec/gateway/`（`@sec/gateway`）— M1 入口插件：装配 `SecShellExecutor`（替 ctx.shell，继承 LocalBashExecutor）+ `DockerSandboxProvider`（替 ctx.sandbox）+ `tools/pre-execute` 守卫。模块：policy（威胁矩阵/wouldDeny 拒因单源）、docker-provider（三态探测+Ttl 缓存）、executor（host/wsl/docker/sandbox 四分支）、pathguard（只拦写）、rateguard（静态限速）、brief（2000 截断标注）。
- `packages/sec/gateway-tools/`（`@sec/gateway-tools`）— `run_cmd` 工具：模型唯一命令口；参数 cmd*/runtime/threat_class/net/approval_id/timeout(秒)。
- `scripts/check-tools.mts`（枚举工具+seam 类）、`m1-smoke.mts`（无 LLM 全矩阵+守卫，61 检查）、`m1-docker-smoke.mts`（真实 docker，12 检查）。上游根目录跑 `pnpm exec tsx <绝对路径>`。

## M1 关键约定与坑

- 威胁矩阵 L0 host < L1 wsl < L2 docker < L3 sandbox；trusted 全等级、untrusted 仅 docker/sandbox、unknown/非法→malware_live 仅 sandbox；net M1 仅 none，real/fakenet 归 M2。
- docker 加固 `--rm --network none --memory 512m --pids-limit 64 --cpus 1.0 --security-opt no-new-privileges --cap-drop ALL`；L2 挂 `<ws>:/workspace`+`-w /workspace/scratch`，L3 零挂载。daemon/镜像缺失→`SANDBOX_UNAVAILABLE`，绝不降级 host。
- 容器内 nmap 无 raw socket：用 `-sT`；**别加 -T2**（sneaky 间隔拖到数分钟），`--max-rate` 即有效节流。镜像 ffuf 1.1 无速率旗标，`-t` 限并发（新版用 -rl/-rate）。
- 拒因三方必须逐字一致：pre-execute 钩子 == runCommand 兜底 == wouldDeny 纯函数。
- 生产层（web-app patch）已 disable tool-bash/tool-pwsh；冒烟引导剔除 web-app 故工具列表里仍见 pwsh，属预期。
- 改 Service 类一律重启进程（HMR 只安全用于工具/钩子）；TaskStop 留 node 孙进程，按端口/PID 强杀（netstat 的 PID 可能错，用 CIM Win32_Process 按命令行找）。

## Ark 模型接入

- base 的 `llm-deepseek-api-key` 即 Anthropic Messages 协议；profile patch 覆盖 llm-deepseek：baseURL=`https://ark.cn-beijing.volces.com/api/coding`、apiKeyEnv=ARK_API_KEY、模型 `ark-code-latest`、maxTokens=131072（Ark 硬上限）。
- key 启动注入：`export ARK_API_KEY="$(grep '^ARK_API_KEY=' <项目>/.env | cut -d= -f2- | tr -d '\r')"`；只在仓库外 .env 与进程环境。
