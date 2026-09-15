# core/tools/

> 组合服务层：把 `tools/` 里的原子工具适配脚本组合成 Agent/API 可调用的服务。
> 与 `tools/` 的分工：脚本/manifest 在 `tools/`，这里是 Python 服务与选路逻辑。

## 文件

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
- 地址纪律：内部 int；出服务/事件一律小写 hex 串；MCP 入参 `_addr_arg` 统一 hex 串。

## 测试

- `tests/test_decompiler.py`：canned v3 JSON + FakeMCPTransport（routes 模拟 initialize/
  tools/call/4xx 重握）；改真机工具参数形状必须同步 fake 与断言（2026-09-14 踩过：
  fake 把 rename 漏 batch 的错误形状固化，单测全绿但真机 isError）。
