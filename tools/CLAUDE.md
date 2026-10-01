# tools/

> 工具体系（DESIGN.md §八）：**registry.json 注册表**（机器无关声明 + 四来源探测）+ 反编译脚本区 + MCP vendor 区。
> 之上的组合服务在 `core/tools/`（如 decompiler.py）；探测引擎在 `core/toolchain.py`，别放反。

## 目录结构

```
tools/
├─ registry.json          # 注册表单表（入库）：工具名 → {kind, bin, search, fallback, download, guide, note…}
├─ pentest-box/           # 渗透工具箱镜像构建区（pentest-tools-container-m0）：Dockerfile 三层
│                         # （基础/web 渗透/python 库）→ cyberstrike/pentest-box:0.1，
│                         # 构建 python scripts/build_pentest_box.py；网关 docker runtime 默认镜像
├─ runtime/               # 基础环境「开包自带」规范位（M2 落位 jre-temurin-17 / python-3.13；gitignore）
├─ bin/                   # 可下载/手动落位的便携工具规范位（M2 起；gitignore）
├─ py/android/            # 随仓自带 python 分析脚本（r0re 收编 5 件：分诊 runner / godot runner / 脱壳三件套）
├─ venv/                  # python 工具环境（建于自带 python，M2；gitignore）
├─ data/                  # 随包数据规范位（kind=data，见 data/CLAUDE.md）：fpdb/fpdb_seed.json
│                         # 本地指纹包首批（dsh 收编，83 规则+16 path_rules）
├─ decompiler/{ida,ghidra}/  # 反编译适配脚本区（入库）
├─ mcp/                   # 第三方 MCP 插件 vendor 区（见 mcp/CLAUDE.md）
└─ installed/             # 旧按需安装位（gitignore；并入 bin/ 属待拍板，落地前照旧）
```

## 注册表与四来源探测（toolchain-registry M1，2026-09-23）

- **registry.json 条目字段**：`kind`（binary/runtime/python-tool/data 白名单）/ `bin`（平台键 windows/linux/\*，相对 tools/ 规范位）/ `search`（PATH 探测文件名清单）/ `fallback`（常见安装目录 glob，`?:/` 前缀=盘符通配 C-H）/ `download`（官方下载页）/ `verify`（深检命令，M3 面板用）/ `pip`（python-tool 的 venv 包）/ `domains`（能力域）/ `guide`（缺失指引文案）/ `acquire: "bundled"`（开包自带）/ `runtime`（依赖的运行时，如 ghidra→jre）/ `note`。
- **四来源顺序**（core/toolchain.py `resolve_tool`，命中即止）：`config/tools.json` paths 指认 → tools/ bin 规范位（acquire=bundled 则 source=bundled，否则 downloaded）→ fallback glob → PATH。**探测只查文件存在性零副作用**（verify 试跑留 M3 面板）。
- **覆盖层 `config/tools.json`**（gitignore）：`{"jadx": {"paths": ["D:/.../jadx.cmd"]}}`——本机既有安装纳管；坏文件 `{}` 宁容错。registry 坏条目反而 fail-fast（ValueError，入库声明出错不静默吞）。
- **消费方**：detector（能力清单→Agent 系统提示）、doctor（tool-missing info / tool-registry-invalid error）、decompiler（ida/ghidra 解析前置 registry）。新增工具先查 registry 再加探测逻辑；**改 schema 须同步 core/toolchain.py 校验与 tests/test_toolchain.py**。

## 反编译脚本区

| 工具 | 状态 | 用途 |
|------|------|------|
| `decompiler/ida/` | ✅ 首选 | idat -A + IDAPython **v3 导出**（scripts/export_funcs.py）；库写 artifacts/decompiler-db/\<sha\>.i64；**写回** scripts/apply_names.py（对现有 .i64 跑，set_name/set_func_cmt + save_database） |
| `decompiler/ghidra/` | ✅ 兜底 | analyzeHeadless + Jython postScript 同契约导出；临时工程按调用隔离、用完即删 |

**v3 导出契约**（两脚本必须同构）：`{export_version:3, binary, meta{arch,bits,endian,imagebase,entry,filename}, sections[...], imports{...}, functions[{address(整数),name,size,calls[],pseudocode}], strings[{address(整数),string,length,type:"cstr"|"unicode",refs:[{func_addr,from_addr}]}]}`。四个增强段各自 try/except，失败给 `{"error":...}` 不阻断 functions；`export_version<3` 的缓存读时即删重导（升版必同步两脚本、decompiler.py、测试 canned）。字符串引用经 DataRefsTo/getReferencesTo 归属所在函数。ELF 导入模块名是 `.dynsym`，符号剥 `@@GLIBC_x.y` 后缀才能对齐 calls。

## 关键约定

- 分发策略（toolchain-registry 拍板）：**基础环境开包自带 + 其它工具可下载落位/配置路径纳管**；二进制本体不进 git（runtime/、bin/、venv/ gitignore），桌面打包经 scripts/build_exe.py 随包。
- 反编译统一走 `core/tools/decompiler.py`：工厂 `build_headless_service(cache_dir, runner=gateway_runner, prefer=("ida","ghidra"), mcp_endpoint=..., global_cache_dir=...)` 默认装 MCP 实时桥（端点由 config/mcp.json 选路，缺省 127.0.0.1:13337）；**Agent 会话工厂刻意不传 mcp_endpoint**（无人值守不赌 GUI 当前库）。按 sha256 缓存；**Agent 不直接调 Ghidra/IDA**。`global_cache_dir` 指 `data/decompiler-cache`（跨项目同 sha 零重导，大样本 P1，2026-09-30）。**大样本（P2，2026-09-30）**：样本 ≥20MB 时导出选路把 Ghidra 提到最前（多进程并行分片主产，postScript 第二参传 worker 数，脚本内 N 个 worker 各持一个 DecompInterface 并行反编译），普通样本维持 IDA 优先。
- 工具命令经执行网关（threat_class=trusted——反编译器只解析不执行样本）；headless 超时默认 3h（`HEADLESS_TIMEOUT = 3*3600`，大样本 P1 起，原 900s），可经 gitignore 覆盖层 `config/decompiler.json` 覆盖：`headless_timeout`（秒，`resolve_headless_timeout()`）/ `large_sample_bytes`（大样本阈值，与 app.py 20MB 同口径）/ `ghidra_workers`（Ghidra 并行 worker 数，默认 `max(1,CPU-1)`、夹 ≤16 内存保护）。

## 坑与注意（Windows 实证）

- **PowerShell 吃引号**：`powershell -Command` 会把 `-S"script out"` 改写成 `"-Sscript out"`；解法是首个 token 后插 `--%`（停止解析），已固化在 gateway_runner。路径约定不含 `%`。
- **IDA 9.3**：只有 `idat.exe`（无 idat64）；64 位目标产 `.i64` 且忽略写错的 -o 扩展名 → `-o` 只给无扩展主干，产物按 .i64→.idb 探测。
- **写回/拉名（P2）**：apply_names.py 与库内重导都直接对 **现有 .i64** 跑（参数末位给库路径、**不带 -o、不删库**；export() 会先删库重建，绝不能用于 pull，否则 GUI 手改名丢失）。`.id0/.id1/.id2/.nam/.til` 任一存在=GUI 开着该库→locked 绝不强写。in/out 临时件 `_apply_<uuid>.*.json` 落 db 同目录跑完即删。
- **IDA 9.3 命名标志在 `ida_name` 不在 `idc`**：`idc.SN_FORCE` 不存在（脚本 AttributeError、idat rc=1 且 stderr 空），写回用 `ida_name.SN_NOWARN|ida_name.SN_FORCE`。idat 是 GUI 子系统程序，rc=1 时控制台无输出——排障用脚本内 try/except 写 traceback 到日志文件。
- **导出脚本输出必须显式 UTF-8**：Windows 裸 `open(OUT,"w")` 默认 GBK，写回的中文注释会经 Hex-Rays 伪码 `//` 行进 `functions[].pseudocode`，重导即产出非 UTF-8 缓存，后端 `read_text(utf-8)` UnicodeDecodeError、pull Job error。IDA 用 `open(...,encoding="utf-8")`，Ghidra Jython 用 `io.open(...,encoding="utf-8")`（test_decompiler 有源码护栏）。
- **jadx/apktool/adb 是 .cmd 包装**（本机 `D:/Dowload/workspace/tools/android-bin/`）——fallback/PATH 找不到，靠 config/tools.json paths 指认。
- 容器内访问不了宿主 `/mnt/<盘>` 路径——headless 固定 host runtime。
- 改 v3 契约时两脚本 + `core/tools/decompiler.py`（EXPORT_VERSION）+ tests canned（test_api.V3 / test_decompiler.EXPORT）同步改。
