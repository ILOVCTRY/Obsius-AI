# 方案：工具链注册表与项目自包含（tools/ 体系）

- **状态**：讨论收敛，待打磨（2026-09-21）
- **拍板记录**：见 §3（4 项决策已确认）
- **关联代码**：`core/runtime/gateway.py`（env 注入点 :163 `{**os.environ, TEMP/TMP 重定向}`）、`core/runtime/detector.py`（能力探测：PATH+常见安装目录）、`tools/`（现有 `decompiler/{ida,ghidra}` 脚本区 + `mcp/ida-pro-mcp`）、`webui/src/views/SettingsView.tsx`（MCP 配置区）、`core/tools/decompiler.py`（DECOMPILE_GUIDANCE 引导先例、Ghidra 需 JDK）
- **实施后**：定稿决策沉淀回 `DESIGN.md`，同步翻新 `tools/CLAUDE.md`；本文保留作方案背景

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

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 安装策略 | **全手动放置**：注册表只声明路径约定+下载指引，用户自行下载放置；面板显示就绪状态，**平台不自动下载**（网络环境自主可控） |
| 2 | Ghidra | **项目内自包含**：Ghidra zip + JRE（Temurin）入 tools/，反编译兜底不再依赖宿主机安装，Java 环境一并解决 |
| 3 | 容器侧工具 | **混合**：基础工具进本项目 Dockerfile 构建的安全工具镜像（也是项目自包含）+ 大件挂载 tools/ 卷；按工具逐个在注册表声明 |
| 4 | 体系结构 | **注册表驱动五层**：registry.json → 目录规范 → 网关注入 → 设置页工具面板+doctor → Agent 引导 |

## 4. 设计详述

### 4.1 工具注册表 tools/registry.json（入库）

```json
{
  "adb": {
    "label": "Android 调试桥", "kind": "binary",
    "bin": {"windows": "bin/adb/platform-tools/adb.exe"},
    "verify": ["adb", "version"],
    "guide": "https://developer.android.com/tools/releases/platform-tools 下载解压至 tools/bin/adb/",
    "domains": ["research", "pentest"]
  },
  "jre":     { "kind": "runtime", "bin": {"windows": "jre/temurin-17/bin/java.exe"} },
  "ghidra":  { "kind": "binary", "runtime": "jre", "bin": {"windows": "ghidra/support/analyzeHeadless.bat"} },
  "wappalyzer-data": { "kind": "data" },
  "ida":     { "kind": "external-probe" }
}
```

**kind 五类**：`binary`（便携可执行）/ `runtime`（JRE 等语言环境）/ `python-tool`（venv）/ `data`（指纹数据库等）/ `external-probe`（IDA 类：探测宿主机安装+引导，**永不进 tools/**）。sha256 可选字段：手动放置后校验提示，不匹配告警不拦截。

### 4.2 目录布局

```
tools/
├─ registry.json            # 注册表（入库）
├─ bin/<tool>/…             # 手动放置的便携工具（gitignore）
├─ jre/temurin-17/          # Java 运行时（gitignore）
├─ venv/                    # python 工具环境（gitignore）
├─ ghidra/                  # Ghidra 本体（gitignore，自包含决策落地）
├─ decompiler/{ida,ghidra}/ # 现有脚本区（入库，不动）
└─ mcp/ida-pro-mcp          # 现有（不动）
```

.gitignore 补条目；tools/CLAUDE.md 实施时同步翻新。

### 4.3 网关注入（宿主机零改动）

- env 组装处按 registry 注入：PATH 前置各工具 bin 目录、`JAVA_HOME=tools/jre/temurin-17`、PATH 追加 `jre/bin`——Agent 与工具都无感，宿主机全局不变；
- L0 host 直接注入；L1 WSL 沿现状 cmd 前缀思路；L2/L3 按 per-runtime 声明（决策 3 混合）。

### 4.4 设置页工具面板 + doctor

- 与 MCP **并列两翼**：MCP=在线服务型工具、registry=本地工具，合称工具体系；
- 面板每行：名称 / kind / 状态灯（就绪/缺失/异常）/ 版本（verify 输出）/ **安装指引**（guide 链接 + 目标路径提示）；
- 手动放置后「检测」按钮：跑 verify + 可选 sha256 校验；
- doctor 体检项：`tool-missing`（info，按 domains 相关轨提示）。

### 4.5 Agent 引导（沿用先例）

- 工具缺失 → 事件引导文本（DECOMPILE_GUIDANCE 模式）：官方下载页 + 放置路径 + Agent 降级策略（如反编译退纯静态分析）。

### 4.6 容器侧（决策 3）

- registry entry 带 per-runtime 字段：`{"l0": "bin/adb/…", "docker-image": "…", "docker-mount": "tools/jre"}`；
- 基础小工具进本项目安全工具镜像；大件（jre/ghidra）挂载进容器。

### 4.7 adb 设备型工具的特殊边界

- adb **二进制**项目内自带；**设备来源**（真机 USB / 网络 adb / 模拟器）属宿主机硬件环境——连接策略（网络 adb 地址配置等）另议，不塞进本方案。

### 4.8 衔接既有方案

- pentest 方案指纹三段式（#6 定稿）的「本地指纹包」= 本注册表 `data` 类首批成员；
- expert-pool 方案的专家 tools 字段最终引用注册表工具名。

## 5. 待打磨清单

1. registry.json 完整 schema 与 doctor 校验项（bin 指向存在性 / verify 可跑 / guide 非空）。
2. 首批工具清单与放置指南文案（JRE + Ghidra + adb + 指纹数据）。
3. decompiler.py 路径衔接：探测顺序改为 **registry 路径优先 → 宿主探测兜底**（Ghidra/JRE 自包含后本机路径即首选）。
4. 网关注入实现细节：每次 run 全量注入 vs 按命令检测涉及工具注入。
5. python-tool（venv）类是否首批需要。
6. 安全工具镜像（基础工具清单 + 构建脚本）。
7. WSL 侧注入方式细化。
8. 设备型工具（adb）连接策略。
9. external-probe 类（IDA）与注册表统一呈现的形态。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 注册表+检测**：registry.json schema + detector 升级注册表驱动 + doctor 体检。
- **M2 网关注入**：PATH/JAVA_HOME 注入 + JRE/Ghidra 落位自包含 + decompiler.py 衔接。
- **M3 设置页工具面板**：状态/指引/检测，与 MCP 并列。
- **M4 容器侧**：镜像构建 + per-runtime 声明（可后置）。
