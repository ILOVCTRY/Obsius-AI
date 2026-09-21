# tools/mcp/

> 第三方 MCP 插件的**仓库内 vendor 区**。目标：逆向工作台的 MCP 实时桥「仅依赖项目」，
> 用户不必另装 pip 包或 clone 仓库。运行时客户端不在这里，在 `core/tools/decompiler.py`
> 的 `MCPBackend`（DESIGN.md §9「MCP 路线」）。

## 目录

```
tools/mcp/
├─ ida-pro-mcp/          # mrexodia/ida-pro-mcp 快照（勿手改，升级=重新复制）
│  ├─ VENDOR.md          # 上游/日期/依赖/升级方式（改快照先看它）
│  ├─ ida_mcp.py         # IDA 插件入口（PLUGIN_ENTRY，Ctrl-Alt-M，127.0.0.1:13337）
│  ├─ bootstrap_mcp.py   # ★ 自有文件：idat 无窗口 MCP bootstrap（IdaMcpManager 拉起件，
│  │                     #   自建同步队列；修改走 core/tools/ida_mcp_manager.py 联动）
│  └─ ida_mcp/           # 实现：api_*.py 71 工具 + zeromcp/（自带 MCP/SSE，零 pip）
└─ CLAUDE.md
```

复制时**排除** `tests/`、`__pycache__/`、`*.pyc`（详见 VENDOR.md）。

## 安装（一键）

`python scripts/install_ida_mcp.py` —— 幂等把 `ida-pro-mcp/` 两件复制进 IDA 用户插件目录
（`%APPDATA%\Hex-Rays\IDA Pro/plugins`，回退 IDA 安装目录 plugins），写 `.vendor_version`。
**不做 pip 安装**（插件纯标准库）。装完在 IDA 里 Edit→Plugins→MCP（Ctrl-Alt-M）起服务。
`--refresh` 用本目录快照覆盖目标（升级/修复），`--status` 看当前安装版本。

## 运行形态（接手须知）

- 插件在 **IDA 进程内**起 HTTP server，端点 `http://127.0.0.1:13337/mcp`，
  streamable-http：真机 `initialize` 回 **200 JSON**（客户端兼容 SSE 体），会话 id 在
  响应头 `Mcp-Session-Id`，之后请求须回带该头并发 `notifications/initialized`（服务端 202）；
  缺/失效 session 回 400，客户端清 session 重握一次。tools/call 结果被服务端二次
  `json.dumps` 进 `content[0].text`，MCPBackend 必须再 json.loads 一次。
- 真机工具名是 `decompile / list_funcs / rename(batch) / set_comments(items) /
  func_profile / xrefs_to / callees / entity_query / server_health / idb_save`——
  不是旧客户端猜的 decompile_function。别名表与 schema 对齐在 `MCPBackend`。
- `rename` 签名是 `rename(batch: RenameBatch)`，参数须再包一层：
  `{"batch":{"func":[{"addr":"0x..","name":".."}],"allow_overwrite":true}}`（漏 batch
  真机回 isError「missing required parameters: ['batch']」；2026-09-14 走查实测修复，
  假 transport 单测曾把错误形状固化——改形状先对齐 vendor api_modify.py）。
  `set_comments` 用 `{"items":[{"addr":"0x..","comment":".."}]}`——**替换非追加**：
  对函数入口同时重写行注释与函数注释（set_cmt+set_func_cmt），与 headless apply_names
  语义一致（func_kb 唯一真相源，每次带完整注释）。`decompile` 入参单个 addr 字符串、
  返 `{addr,code,error?}`。

## 红线（不可越）

- **只连 127.0.0.1**；不做 stdio。平台可**按需拉起本地 idat 实例**（`IdaMcpManager`，专属
  端口 13338+，空闲 10min 自动关）并连其 http 端点——拉起件是本目录 `bootstrap_mcp.py`
  （自有文件，不动 vendor 任何文件）；仍不连远程、不连人手 GUI 之外的 stdio 进程
  （config 里 stdio server 与本桥无关）。
- MCP 只做**实时点查/实时写当前 GUI 库**：写回 annotate、缓存缺席的单函数 decompile、
  xref 降级。**绝不用 MCP 全量函数列表替换 headless 缓存**（三层数据纪律不变）。
- MCP 在线是 headless 的**增强不是替代**：断了一切降级回 headless，三态灯变灰，不报 500。
- vendor 件当只读第三方代码：不在里面加平台逻辑，bug 在 `MCPBackend` 侧适配/隔离。
