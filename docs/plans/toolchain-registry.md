# 方案：工具链注册表与项目自包含（tools/ 体系）

- **状态**：**M1 已实施（2026-09-23，见 DESIGN.md §七），剩 M2 基础环境自带+网关注入 / M3 设置页工具面板 / M4 容器侧 待排期**
- **拍板记录**：见 §3（6 项决策，第 1 项已修订）；M1 实施拍板见 §0
- **关联代码**：`core/runtime/gateway.py`（env 注入点 :163 `{**os.environ, TEMP/TMP 重定向}`）、`core/runtime/detector.py`（能力探测：PATH+常见安装目录）、`tools/`（现有 `decompiler/{ida,ghidra}` 脚本区 + `mcp/ida-pro-mcp`）、`webui/src/views/SettingsView.tsx`（MCP 配置区）、`core/tools/decompiler.py`（DECOMPILE_GUIDANCE 引导先例、Ghidra 需 JDK；IDA FALLBACK_ROOTS 兜底扫描先例 :67-73；Ghidra 仅 `shutil.which` 无兜底 :478,:484-487）、`config/tools.json`（新建：本机路径覆盖层，gitignore）、`启动平台.bat`（:22-24 现硬编码优先 `E:\Miniconda3\python.exe`——自带 python 引导的改造点）
- **实施后**：定稿决策沉淀回 `DESIGN.md`，同步翻新 `tools/CLAUDE.md`；本文保留作方案背景

## 0. 实施记录

### M1 注册表+自动检测（2026-09-23）

落地：`core/toolchain.py`（新模块，纯 stdlib）+ `tools/registry.json`（16 条目首批）+ `config/tools.json` 覆盖层（gitignore）+ 三消费方接线（detector/doctor/decompiler）+ `tests/test_toolchain.py`（24 用例）。

**实施拍板记录**（方案打磨未覆盖、实施时定的，均为常规决策）：

1. **四来源检测顺序**：config/tools.json paths → tools/ bin 规范位（kind=runtime 且 acquire=bundled 时 source=bundled，否则 downloaded）→ fallback glob → PATH（search 文件名清单）。§4.1 的三通道顺序补第 4 级 PATH 兜底（宿主 PATH 最低优先，宁用项目内/指认路径）。
2. **探测零副作用**：只查文件存在性（`exists`/`glob`），不试跑；verify 深检留 M3 面板。fallback glob 支持 `?:/` 前缀盘符通配（C-H 逐一尝试），字面量直接 exists、含通配符以锚点盘符为 base 逐段 glob。
3. **registry 校验 fail-fast**：坏条目（kind 非白名单 / verify·fallback·search·pip·domains 非数组 / bin 非平台键对象）`ValueError` 带条目名——入库声明出错是代码 bug 不该静默吞（仿 validate_verify_spec 精神）；缺文件给 `{}`。覆盖层相反：坏文件 `{}` 宁容错（仿 fofa config）。
4. **manifest.yaml 通道退役**：detector 原 `tools/**/manifest.yaml` probe 通道零使用者，registry 驱动后随之删除（`_load_simple_yaml` 移除）；`installed/` 目录暂保留不动（并入 `bin/` 属待拍板项）。
5. **doctor 体检**：`diagnose(packs_root, tools_root=None)` 可选参——`tool-missing`（info，detail 带 guide）/ `tool-registry-invalid`（error，坏结构显性化）/ `tool-registry-error`（warning，探测异常不阻断主流程）；tools_root=None 完全跳过（兼容既有单参调用）。
6. **decompiler 衔接**：`resolve_ida_headless()` 前置 registry 四来源（原生 PATH→glob 降为兜底）；新增 `resolve_ghidra_headless()`（registry → PATH，解 §2.1 错位②「Ghidra 装了探测不到」）；工厂 ghidra 腿传 `headless_cmd=resolve_ghidra_headless() or "analyzeHeadless"`。registry 命中的 fallback glob 与 `_IDA_INSTALL_GLOBS` 同源并存——registry 优先、原生兜底保留（FALLBACK_ROOTS 暂不迁入 registry，双通道冗余无害）。
7. **首批 registry 16 条目**：ida/ghidra（binary，fallback 覆盖本机实位）、jre/python（runtime，acquire=bundled 占位——M2 落位 tools/runtime/ 后即命中规范位）、jadx/apktool/adb（binary，search+download+guide）、nmap/httpx/nuclei/ffuf/sqlmap/masscan/hydra（pentest 域，search+download；masscan/hydra 注明 rateguard 规则空转）、impacket（python-tool，pip 声明，M2 venv 落位）、fpdb（data，dsh 收编首批）。jadx/apktool/adb 本机非标位（`D:/Dowload/workspace/tools/android-bin/*.cmd`）经 config/tools.json 指认收编。
8. **detector/doctor 消费均带覆盖层**：`probe_tools(tools_root, overrides=load_tool_overrides())`——config/tools.json 指认对能力清单与体检同时生效（与模块 docstring 四来源顺序一致）。

**M2 衔接提示**：jre/python 的 bin 规范位声明已按 §4.2 布局写好（`runtime/jre-temurin-17/`、`runtime/python-3.13/`），落位即命中；网关注入点 gateway.py:163。

## 1. 愿景与背景

**用户原则（2026-09-21 原话）**：宿主机提供的是运行该项目必要的环境，其他内容尽量只依赖本项目的目录。

- 宿主机只供：Python / Node / 浏览器（平台自身运行所需）；
- 一切工具 + 语言运行时 → `tools/` **项目目录内自包含**（手动放置，portable 解压即用）；
- 不动宿主机 PATH / 系统环境 / 注册表。

**触发点**：设置页只有 MCP 无工具管理；后续要接 adb、指纹识别、Java 环境等；Ghidra 兜底需 JDK 的宿主依赖痛点。

## 2. 现状盘点（2026-09-21 核实）

- 网关 env 注入点已存在（gateway.py:163）——PATH/JAVA_HOME 注入有现成挂点；
- detector.py 已按「tools/ 清单哪些在 PATH + 常见安装目录」探测（三态灯/降级用）——升级为注册表驱动即可；
- tools/ 已有脚本区（decompiler/mcp），**二进制本体全依赖宿主机安装**；
- Ghidra 兜底需 JDK；DECOMPILE_GUIDANCE 已是「不可用→安装引导文本→Agent 降级」的先例；
- 设置页 SettingsView 有 MCP 区无工具区。

### 2.1 实测盘点（2026-09-22：host PATH / WSL / Miniconda 包 / 平台声明面四层对齐）

- **三处错位**（比缺工具更要紧）：
  1. **host `python` 指错人**——宿主 PATH 的 `python` 落在无关应用 venv（capstone/unicorn/pwn/elftools/yara/requests 全无），网关 env 继承宿主 PATH → Agent `run_cmd host python` 拿到的就是它；binary-rev 技能明文教「host python + capstone 写求解脚本」→ 当场 ImportError；
  2. **Ghidra 装了但平台看不见**——实际装于 `E:\Tools\ghidra_12.1.2_PUBLIC`，decompiler.py:478 只有 `shutil.which("analyzeHeadless")`、无 IDA 式兜底目录扫描（:67-73 仅服务 IDA）→ available()=False，反编译兜底腿实际是瘸的；且靠宿主 PATH 的 Jdk22 裸跑，无 JAVA_HOME 注入；
  3. **rateguard 空转**——masscan/hydra 有完整限速规则，工具本身双端全缺。
- **主力不缺**：IDA（`D:\Tools\IDA93`，兜底扫描可达）、nmap/httpx/nuclei/ffuf/sqlmap（host）、jadx/apktool/adb（host，但 jadx/apktool 在 `D:\Dowload\workspace\tools\android-bin` 非标位置）、Miniconda 逆向栈全套（capstone/unicorn/pwntools/elftools/yara/pycryptodome）、frida、WSL 侧 radare2/john/binwalk/dig/whois。
- **真空白**：内网套件 impacket/netexec/bloodhound/certipy **双端全缺**（payloader 内网手册 impacket 系引用 30+ 处，真打到内网阶段基本裸奔）；subfinder/upx host 缺；testssl/sslscan 缺（openssl 有）。
- **首批结论（喂 §5 待打磨 2）**：python/jre 开包自带 → venv 建于其上（capstone/unicorn/pwntools/impacket/certipy 一并装）+ Ghidra 经 fallback/可下载纳管 + 既有工具（ida/jadx/apktool/adb）配置路径收编；masscan/hydra 声明 + doctor 提示，不强行首批。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 安装策略 | ~~全手动放置~~ **已修订（2026-09-22）→ 见 5/6**：分通道获取 + 自动检测，替代「只手动放置 + 手动检测」 |
| 2 | Ghidra | **项目内自包含**：Ghidra zip + JRE（Temurin）入 tools/，反编译兜底不再依赖宿主机安装，Java 环境一并解决 |
| 3 | 容器侧工具 | **混合**：基础工具进本项目 Dockerfile 构建的安全工具镜像（也是项目自包含）+ 大件挂载 tools/ 卷；按工具逐个在注册表声明。~~大件挂载~~ **已修订（2026-09-23）→ 大件改「构建时 COPY 进镜像 + 增量补丁镜像迭代」，bind mount 仅限 L2 现场/缓存卷**（Docker Desktop Windows 文件共享层重 I/O 慢数倍 + L3「无宿主挂载」硬规则不容挂载；镜像矩阵与容器执行模型详见 [container-execution-architecture.md](container-execution-architecture.md)） |
| 4 | 体系结构 | **注册表驱动五层**：registry.json → 目录规范 → 网关注入 → 设置页工具面板+doctor → Agent 引导 |
| 5 | 基础运行时 | **python、java 等基础环境必须项目开包自带**（2026-09-22）：`tools/runtime/` 随发布包分发，不依赖宿主机安装；启动器优先用自带 python（现 `启动平台.bat:22-24` 硬编码 Miniconda 机器绑定，随之解放）；rev/pwn venv 建于自带 python 之上 |
| 6 | 其它工具获取与检测 | **可配置路径 + 可下载落位 tools/ + 自动检测**（2026-09-22）：①既有安装走可配置路径纳管（IDA/Jadx/adb/Ghidra 现装即刻可见）；②可下载工具落 tools/ 规范位置（手动放置与面板下载等价）；③启动时对两通道自动探测（替代「手动检测按钮」为主通道） |

## 4. 设计详述

### 4.1 工具注册表 tools/registry.json（入库）

```json
{
  "python":  { "kind": "runtime", "acquire": "bundled", "bin": {"windows": "runtime/python-3.11/python.exe"} },
  "jre":     { "kind": "runtime", "acquire": "bundled", "bin": {"windows": "runtime/jre-temurin-17/bin/java.exe"} },
  "ghidra":  { "kind": "binary", "runtime": "jre",
               "bin": {"windows": "ghidra/support/analyzeHeadless.bat"},
               "download": "https://github.com/NationalSecurityAgency/ghidra/releases/…",
               "fallback": ["?:/Tools/ghidra*/support/analyzeHeadless.bat", "?:/ghidra*/support/analyzeHeadless.bat"] },
  "ida":     { "kind": "binary", "verify": ["{bin}", "--version"], "domains": ["binary"],
               "fallback": ["C:/Program Files/IDA*/idat.exe", "D:/Tools/IDA*/idat.exe", "…迁移自 decompiler.py FALLBACK_ROOTS"] },
  "adb":     { "kind": "binary", "bin": {"windows": "bin/adb/platform-tools/adb.exe"},
               "verify": ["adb", "version"], "download": "https://developer.android.com/tools/releases/platform-tools",
               "domains": ["research", "pentest"] },
  "wappalyzer-data": { "kind": "data" }
}
```

**kind 收敛四类**：`binary`（可执行）/ `runtime`（python、jre 等基础环境）/ `python-tool`（venv）/ `data`（指纹数据库等）；**external-probe 退役**——「外部安装、永不进 tools/」的 IDA 类改由获取字段表达（fallback/paths），不再是 kind 语义。

**获取三通道（拍板 5/6，per-entry 正交字段）**：

- `acquire: "bundled"`——**开包自带**：python/jre 等基础运行时随发布包分发（gitignore + 打包脚本打入，衔接 desktop-app-shell M3）；
- `download`——可下载源（url）：面板一键下载或手动下载放置到 `bin` 规范位，两者等价；
- 本机路径覆盖层 **`config/tools.json`**（gitignore，仿 config/mcp.json 模式）：`{"ida": {"paths": ["D:/Tools/IDA93/idat.exe"]}, "ghidra": {"paths": ["E:/Tools/ghidra_12.1.2_PUBLIC/…"]}}`——既有安装纳管；registry 内 `fallback` 存候选 glob（入库，机器无关）。

**检测顺序**：config/tools.json paths → tools/ `bin` 规范位 → fallback 候选 → 缺失（面板标注 + doctor）。sha256 可选字段保留：下载/放置后校验提示，不匹配告警不拦截。

### 4.2 目录布局

```
tools/
├─ registry.json            # 注册表（入库）
├─ runtime/python-3.11/     # 基础环境·开包自带（拍板 5；gitignore，发布包打入）
├─ runtime/jre-temurin-17/  # 基础环境·开包自带（Ghidra 的 java 一并解决）
├─ bin/<tool>/…             # 可下载/手动落位的便携工具（gitignore）
├─ venv/                    # python 工具环境（建于自带 python；gitignore）
├─ ghidra/                  # Ghidra 可下载落位（gitignore，自包含决策落地）
├─ data/fpdb/               # 指纹包（已落位，dsh-kb-sourcing）
├─ decompiler/{ida,ghidra}/ # 现有脚本区（入库，不动）
└─ mcp/ida-pro-mcp          # 现有（不动）
```

.gitignore 补条目；tools/CLAUDE.md 实施时同步翻新（现行 `installed/` 并入 `bin/`——该合并属待拍板决策点，落地前 installed/ 照旧）。

### 4.3 网关注入（宿主机零改动）

- env 组装处按 registry 注入：PATH 前置各工具 bin 目录、`JAVA_HOME=tools/runtime/jre-temurin-17`、PATH 追加 `jre/bin`、**PATH 前置自带 `runtime/python-3.11/` 与 `venv/Scripts`**（解 2026-09-22 错位①：宿主 PATH 的 `python` 落在无关 venv、无逆向栈）——Agent 与工具都无感，宿主机全局不变；
- L0 host 直接注入；L1 WSL 沿现状 cmd 前缀思路；L2/L3 按 per-runtime 声明（决策 3 混合）。

### 4.4 设置页工具面板 + doctor

- 与 MCP **并列两翼**：MCP=在线服务型工具、registry=本地工具，合称工具体系；
- 面板每行：名称 / kind / 状态灯（就绪/缺失/异常）/ 版本（verify 输出）/ **安装指引**（guide 链接 + 目标路径提示）；
- **自动检测**（拍板 6）：启动时按「config/tools.json 路径 → tools/ 规范位 → fallback 候选」对每条目探测；面板保留「重新检测」按钮走 POST probe 刷新通道（同 gateway-config-view 方案），不再以手动触发为主；
- 每行标注**获取来源**：开包自带 / 配置路径 / 已下载 / 缺失——缺失给 guide 指引或一键下载（落 `bin` 规范位；一键下载是否首批见待打磨 10）；
- doctor 体检项：`tool-missing`（info，按 domains 相关轨提示）。

### 4.5 Agent 引导（沿用先例）

- 工具缺失 → 事件引导文本（DECOMPILE_GUIDANCE 模式）：官方下载页 + 放置路径 + Agent 降级策略（如反编译退纯静态分析）。

### 4.6 容器侧（决策 3，2026-09-23 随 container-execution-architecture 修订）

- registry entry 带 per-runtime 字段：`{"l0": "runtime/python-3.11/python.exe", "l2": {image 内路径}, "l3": …}`——**容器内 runtime ≠ 自带 Windows runtime**（`tools/runtime/` 便携版进不了 Linux 容器），容器侧 python/依赖来自镜像 base 与构建时 pip，requirements 一份两侧各装；
- 工具进镜像走**构建时 COPY**（构建脚本从 tools/ 目录 COPY，项目自包含目标不变）+ **增量补丁镜像**迭代（FROM 旧镜像叠层）；bind mount 仅限 L2 现场/缓存卷，工具本体不挂载；
- 镜像矩阵四件：`cyberstrike-tools`（L2）/ `sandbox-min`（L3 加固）/ `wine-sandbox`（L3 PE 动态，SYS_PTRACE 显性化）/ fakenet sidecar——归属 container-execution-architecture 方案；
- 探测：镜像构建时落 `tools-manifest.json` 进镜像，detector 对 L2/L3 读 manifest 不 exec 探测。

### 4.7 adb 设备型工具的特殊边界

- adb **二进制**项目内自带；**设备来源**（真机 USB / 网络 adb / 模拟器）属宿主机硬件环境——连接策略（网络 adb 地址配置等）另议，不塞进本方案。

### 4.8 衔接既有方案

- pentest 方案指纹三段式（#6 定稿）的「本地指纹包」= 本注册表 `data` 类首批成员；
- expert-pool 方案的专家 tools 字段最终引用注册表工具名。

## 5. 待打磨清单

1. registry.json 完整 schema 与 doctor 校验项（bin 指向存在性 / verify 可跑 / guide 非空 / acquire 合法值 / fallback glob 语义）。
2. 首批清单与文案定稿——2026-09-22 盘点已给候选：python/jre 自带、venv 包清单（capstone/unicorn/pwntools/elftools/yara/impacket/certipy）、ghidra（download+fallback）、ida/jadx/apktool/adb（config paths 收编现装位置）；config/tools.json 首批内容。
3. decompiler.py 路径衔接：探测顺序改为 **registry 路径优先 → fallback 兜底**；FALLBACK_ROOTS（:67-73）迁入 registry；**Ghidra 补 fallback/paths 通道**（解「装了探测不到」错位②）。
4. 网关注入实现细节：每次 run 全量注入 vs 按命令检测涉及工具注入。
5. venv 建立时机与「开包自带」边界：打包时预置 vs 首启/面板一键 pip（需网络一次）；embeddable python 无 pip 的引导补齐。
6. 安全工具镜像（基础工具清单 + 构建脚本）。
7. WSL 侧注入方式细化。
8. 设备型工具（adb）连接策略。
9. 启动器引导顺序：启动平台.bat/serve.py 优先 `tools/runtime/python-3.11`、缺失回退 Miniconda/PATH（现 :22-24 硬编码顺序随之改造；衔接 desktop-app-shell M3 打包）。
10. 面板一键下载是否首批（网络自主可控 vs 便利；代理/镜像设置归属）。

## 6. 实施切分（M1 已实施剔除，剩待办）

- **M2 基础环境自带+网关注入**：python/jre 落 tools/runtime/（发布包打包衔接 desktop-app-shell M3）+ venv 建立与包清单 + PATH/JAVA_HOME/venv 注入（decompiler 衔接已在 M1 提前做完）。
- **M3 设置页工具面板**：状态/获取来源标注/指引/一键下载（视待打磨 10；verify 深检一并落面板），与 MCP 并列。
- **M4 容器侧**：镜像构建（cyberstrike-tools/sandbox-min，构建时 COPY）+ registry per-runtime 声明 + 镜像内 manifest 探测；执行模型/生命周期/防逃逸见 [container-execution-architecture.md](container-execution-architecture.md)（可后置，其 M1-M2 与本里程碑协同）。
