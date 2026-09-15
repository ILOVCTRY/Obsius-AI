# Vendored: mrexodia/ida-pro-mcp（IDA MCP 插件）

本目录是第三方插件的**仓库内快照**，由 `scripts/install_ida_mcp.py` 幂等复制到
IDA 用户插件目录。平台「仅依赖项目」：不要求用户另行 clone / pip 安装。

## 来源

- 上游：<https://github.com/mrexodia/ida-pro-mcp>（模块化新版，71 工具 + 24 资源）
- 复制日期：2026-09-14
- 来源本机路径：`%APPDATA%\Hex-Rays\IDA Pro\plugins\`（`ida_mcp.py` + `ida_mcp/`）
- 复制时**排除**：`ida_mcp/tests/`、所有 `__pycache__/`、`*.pyc`

## 依赖

**纯标准库，零 pip 依赖**。其 MCP/JSON-RPC/SSE 实现自带在 `ida_mcp/zeromcp/`
（`mcp.py` / `jsonrpc.py`），不装 zeromq、不装官方 mcp SDK。运行环境是 IDA 内嵌
Python（随 IDA 分发），与平台的 Miniconda 环境无关。

## 运行形态

- IDA 加载入口 `ida_mcp.py`（`PLUGIN_ENTRY`），Ctrl-Alt-M 或 Edit→Plugins→MCP 启动。
- 在 IDA 进程内起 HTTP server，默认 `http://127.0.0.1:13337`，MCP 端点 `/mcp`
  （streamable-http；initialize 响应为 SSE，会话 id 在响应头 `Mcp-Session-Id`）。
- 端口被占时自动 +1 试探到 +100；平台只认默认 13337（连不上即视为离线，不追端口）。

## 平台用到的工具（tools/list 动态发现，别名表在 core/tools/decompiler.py）

`decompile(addr)` / `list_funcs` / `rename(batch{func:[{addr,name}],allow_overwrite})`
/ `set_comments({items:[{addr,comment}]})` / `func_profile` / `xrefs_to` / `callees`
/ `entity_query` / `server_health` / `idb_save`。

## 升级方式

1. IDA 内更新插件（或从上游 release 替换 `%APPDATA%\Hex-Rays\IDA Pro\plugins\`）。
2. 重跑 `python scripts/install_ida_mcp.py --refresh`（或手动把新版复制覆盖本目录，
   仍排除 tests/__pycache__），更新本文件「复制日期」。
3. 若工具名/参数变化，同步改 `MCPBackend` 别名表与 fake-transport 测试后真机回归。

## 许可

遵循上游仓库许可证（见上游 LICENSE）；此处仅作本机私有平台的内部 vendoring，不分发。
