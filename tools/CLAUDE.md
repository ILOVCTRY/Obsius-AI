# tools/

> 原子工具封装层（DESIGN.md §7"工具目录"）：每个工具一个子目录 = manifest.yaml + 适配脚本。
> 之上的组合服务在 `core/tools/`（如 decompiler.py），别放反。

## 目录结构

```
tools/
├─ core/  recon/  exploit/  decompiler/  forensics/ ...   # 分类子目录，按需扩展
├─ mcp/                 # 第三方 MCP 插件 vendor 区（ida-pro-mcp 快照 + 一键安装，见 mcp/CLAUDE.md）
└─ installed/           # 按需安装的实际二进制（gitignore）
```

每个工具目录：

```
tools/<category>/<tool-name>/
├─ manifest.yaml      # name / category / probe（detector 探测命令）/ install（安装引导）
└─ scripts/           # headless 导出等适配脚本
```

## 当前内容

| 工具 | 状态 | 用途 |
|------|------|------|
| `decompiler/ida/` | ✅ 首选 | idat -A + IDAPython **v3 导出**（scripts/export_funcs.py）；库写 artifacts/decompiler-db/\<sha\>.i64；**写回** scripts/apply_names.py（对现有 .i64 跑，set_name/set_func_cmt + save_database） |
| `decompiler/ghidra/` | ✅ 兜底 | analyzeHeadless + Jython postScript 同契约导出；临时工程按调用隔离、用完即删 |

**v3 导出契约**（两脚本必须同构）：`{export_version:3, binary, meta{arch,bits,endian,imagebase,entry,filename}, sections[...], imports{...}, functions[{address(整数),name,size,calls[],pseudocode}], strings[{address(整数),string,length,type:"cstr"|"unicode",refs:[{func_addr,from_addr}]}]}`。四个增强段各自 try/except，失败给 `{"error":...}` 不阻断 functions；`export_version<3` 的缓存读时即删重导（升版必同步两脚本、decompiler.py、测试 canned）。字符串引用经 DataRefsTo/getReferencesTo 归属所在函数。ELF 导入模块名是 `.dynsym`，符号剥 `@@GLIBC_x.y` 后缀才能对齐 calls。

## 关键约定

- 分发策略：**核心内置 + 其余按需安装**（winget/scoop/pip/docker pull 引导），实际二进制进 `installed/`，不进 git。
- detector（core/runtime/detector.py）扫描 `tools/**/manifest.yaml` 的 `probe` 字段 → 能力清单注入 Agent 系统提示 + WebUI 能力面板。
- 反编译统一走 `core/tools/decompiler.py`：工厂 `build_headless_service(cache_dir, runner=gateway_runner, prefer=("ida","ghidra"), mcp_endpoint=...)` 默认装 MCP 实时桥（端点由 config/mcp.json 选路，缺省 127.0.0.1:13337）；**Agent 会话工厂刻意不传 mcp_endpoint**（无人值守不赌 GUI 当前库）。按 sha256 缓存；**Agent 不直接调 Ghidra/IDA**。
- 工具命令经执行网关（threat_class=trusted——反编译器只解析不执行样本）；headless 超时 900s（`gateway_runner(author="human", timeout=900)`）。

## 坑与注意（Windows 实证）

- **PowerShell 吃引号**：`powershell -Command` 会把 `-S"script out"` 改写成 `"-Sscript out"`；解法是首个 token 后插 `--%`（停止解析），已固化在 gateway_runner。路径约定不含 `%`。
- **IDA 9.3**：只有 `idat.exe`（无 idat64）；64 位目标产 `.i64` 且忽略写错的 -o 扩展名 → `-o` 只给无扩展主干，产物按 .i64→.idb 探测；detector 在 PATH 外扫 `C:\Program Files\IDA*` 等。
- **写回/拉名（P2）**：apply_names.py 与库内重导都直接对 **现有 .i64** 跑（参数末位给库路径、**不带 -o、不删库**；export() 会先删库重建，绝不能用于 pull，否则 GUI 手改名丢失）。`.id0/.id1/.id2/.nam/.til` 任一存在=GUI 开着该库→locked 绝不强写。in/out 临时件 `_apply_<uuid>.*.json` 落 db 同目录跑完即删。
- **IDA 9.3 命名标志在 `ida_name` 不在 `idc`**：`idc.SN_FORCE` 不存在（脚本 AttributeError、idat rc=1 且 stderr 空），写回用 `ida_name.SN_NOWARN|ida_name.SN_FORCE`。idat 是 GUI 子系统程序，rc=1 时控制台无输出——排障用脚本内 try/except 写 traceback 到日志文件。
- **导出脚本输出必须显式 UTF-8**：Windows 裸 `open(OUT,"w")` 默认 GBK，写回的中文注释会经 Hex-Rays 伪码 `//` 行进 `functions[].pseudocode`，重导即产出非 UTF-8 缓存，后端 `read_text(utf-8)` UnicodeDecodeError、pull Job error。IDA 用 `open(...,encoding="utf-8")`，Ghidra Jython 用 `io.open(...,encoding="utf-8")`（test_decompiler 有源码护栏）。
- manifest 的 probe 按空格分词执行（极简 YAML 解析不支持引号嵌套）。
- 容器内访问不了宿主 `/mnt/<盘>` 路径——headless 固定 host runtime。
- 改 v3 契约时两脚本 + `core/tools/decompiler.py`（EXPORT_VERSION）+ tests canned（test_api.V3 / test_decompiler.EXPORT）同步改。
