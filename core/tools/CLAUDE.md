# core/tools/

> 组合服务层：把 `tools/` 里的原子工具适配脚本组合成 Agent/API 可调用的服务。
> 与 `tools/` 的分工：脚本/manifest 在 `tools/`，这里是 Python 服务与选路逻辑。

## 文件

## 文件

- `ida_mcp_manager.py` — **IDA-MCP 实例生命周期管理器（2026-09-20，DESIGN.md §9「MCP 按需拉起」）**：
  按 `(project_id, binary_sha256)` 管无窗口 idat 实例。`ensure(pid, binary, db_dir=)`：在线复用 →
  无 idat/锁占用失败一律 None（headless 降级，绝不抛 500）→ detached 拉起 `idat -A -S<bootstrap>
  -o<db_stem> <binary>`（**-S 无空格单参数**，端口走环境变量 `CYBERSTRIKE_IDA_MCP_PORT`）。
  就绪探活 180s 超时杀；空闲 600s reaper 自动关；上限 2 LRU；`shutdown_all` 平台 shutdown 兜关；
  `online_for_project` 供 overview 三态灯。拉起前查 DB_LOCK_EXTS（GUI 开着该库绝不抢）。
  bootstrap 在 `tools/mcp/ida-pro-mcp/bootstrap_mcp.py`（自有文件，同步队列设计见该文件头注释）。
- `decompiler.py` — 反编译组合服务（DESIGN.md §9），三个层次：

### 1. Headless 后端（全量客观缓存的唯一生产者）

- `IDAHeadlessBackend`：idat -A 跑 `tools/decompiler/ida/scripts/export_funcs.py`（v3 契约），
  库落 `artifacts/decompiler-db/<sha>.i64`；`apply_annotations()` 跑 apply_names.py 写回；
  `.id0/.id1/.id2/.nam/.til` 锁检测→locked。
- `GhidraHeadlessBackend`：analyzeHeadless 兜底，临时工程用完即删；无写回语义。
- `build_headless_service(cache_dir, runner=gateway_runner, mcp_endpoint=None, available=None)`
  装配服务；缓存 JSON 按 sha256 存 `artifacts/decompiler-cache/`，`EXPORT_VERSION` 不符读时即删重导。

### 2. MCPBackend（P2 实时桥，只点查不替代缓存）

- streamable-http 客户端，端点 loopback 校验（只许 127.0.0.1/localhost/::1，构造零网络）。
- 会话：initialize → 缓存响应头 `Mcp-Session-Id`（真机回 200 JSON，兼容 SSE 体）→
  notifications/initialized（202）；4xx 清 session 重握一次（`_rpc` 两轮 for 循环，
  **禁止持锁递归**，threading.Lock 不可重入）。
- 懒探活：`available()` 握手 1.5s 超时，结果 3s TTL 正负缓存，overview 4s 轮询不拖慢。
- 真机 tools/call 双重 JSON（zeromcp json.dumps 进 content[0].text）→ `_payload` 二次解码。
- 工具：decompile_at（单函数实时伪码）/xref_profile（func_profile 转 build_xrefs 同构，
  标 source=mcp）/writeback_items（rename 的参数是 `{"batch":{"func":[...],"allow_overwrite":true}}`
  + set_comments `{"items":[...]}`，注释替换语义；只改 GUI 内存不主动 idb_save；传输级失败返 None）。
- 红线：绝不做全量列表替换缓存；Agent 会话工厂不传 mcp_endpoint（无人值守不赌当前库）。

### 3. DecompilerService（选路）

- 读：列表/概览只走 headless；`live_decompile()`/`xrefs_for()` 缓存缺席且 MCP 在线才点查降级。
- 写：`writeback()` MCP 在线优先 writeback_items，返 None/离线再 headless apply_by_sha
  （五元组 ok/locked/no-db/no-tool/unsupported）。
- Agent 四接口（list_functions/decompile/annotate/xrefs）：list 与全量概览跳过 MCP；
  annotate 只落 sidecar 不直写 MCP。
- **Agent 检索面（2026-09-27）**：`strings_for(binary, q, limit)`（复用 build_strings，limit 默认 200 上限 1000 防淹没上下文）+ `xrefs_for_func(binary, name|address)`（地址先经缓存 `_find_func` 解析函数名再走 xrefs，MCP 在线优先实时）+ `list_functions` 加 `name_contains/min_size` 过滤——对应 Agent 工具 strings_search/func_xrefs/list_symbols 过滤参数（core/agent/tools.py），治 Agent run_cmd 直读全量缓存 JSON 的低效模式。
- **动态端点（2026-09-20）**：`DecompilerService(mcp_provider=callback)` / `build_headless_service(...,
  mcp_provider=)`——decompile 点查与 xrefs 每次先调 `provider(binary)` 取端点（接
  `IdaMcpManager.ensure`，按需拉起 IDA-MCP）；provider None/异常/同端点一律回退固定桥或
  headless 缓存，选路行为与现状不变。**红线不变**：全量概览跳过 MCP（不触发拉起）。
- **大样本防崩 + GUI IDA 轻缓存（2026-09-29，用户口径 >20MB 算大样本）**：
  ① `read_cached(sha)` 解析结果**单槽驻留**（`_parsed_cache`，mtime 失效，只驻留最近一个
  样本）——大缓存 JSON 的 re-parse 是 overview 4s 轮询 + 全部点查的公共热点；
  ② `import_ida_mcp_cache(sha, functions, binary_name, *, partial=False, total=None,
  next_offset=None)` 落 **v3 契约兼容轻量缓存**（meta.source="ida-mcp"，函数名/地址/大小
  全量，无伪码/calls/strings/imports——数据大头不落平台盘，点查走既有 MCP 实时降级），
  落盘后驻留失效；**partial=True（2026-09-30 断点续拉）**：meta 追加 `partial:true`/
  `total_functions`/`next_offset`——拉取中途每页落盘一次，停止后已拉部分立即可见生效，
  再拉时从 next_offset 续传（app.py 对账 total_functions 不一致则从头重拉）；拉完
  partial=False 落盘即清除三字段；
  ③ `list_functions` >500 行截断并提示用 `name_contains/min_size` 缩小范围（防数万函数
  dump 淹没 Agent 上下文）。
  ④ `MCPBackend.count_funcs()`（2026-09-30）：vendor 新增 `count_funcs` 微工具（毫秒级，
  不逐个建函数对象）返回当前库函数总数——拉取进度分母；旧插件无此工具/离线返 None，
  调用方退化为无分母进度。配套治本：vendor `list_funcs` 惰性分页（先取廉价地址表再只
  构建当页切片，原先每页全量重建整库 O(N×页数)，数万函数快约 50 倍）。
- 地址纪律：内部 int；出服务/事件一律小写 hex 串；MCP 入参 `_addr_arg` 统一 hex 串。

## 测试

- `tests/test_decompiler.py`：canned v3 JSON + FakeMCPTransport（routes 模拟 initialize/
  tools/call/4xx 重握）；改真机工具参数形状必须同步 fake 与断言（2026-09-14 踩过：
  fake 把 rename 漏 batch 的错误形状固化，单测全绿但真机 isError）。
