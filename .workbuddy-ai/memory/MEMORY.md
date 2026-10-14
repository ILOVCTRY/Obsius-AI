# cyberstrike-pro — 项目长期记忆

## 前端（webui/）

- **改完必须重建**：`cd webui && npm run build`（= `tsc -b && vite build`）。平台**dist 静态优先**，不重建看不到改动。构建会清空 `dist/assets` 旧 hash 包。
- 样式集中在 `webui/src/index.css` 的 `.wb-*` 段（智能体工作台）；会话流部分已迁 Tailwind 工具类。
- 动效统一尊重 `prefers-reduced-motion`（index.css 末尾有一段集中 `transition: none !important`，新增带 transition 的类要登记进去）。
- 文档同步约定：改 `views/*` 或 `components/*` 时同步 `webui/CLAUDE.md` 对应条目（该项目 CLAUDE.md 是逐文件清单式，很细）。

## 后端

- 黑板 schema 版本：`core/blackboard/schema.py` 的 `SCHEMA_VERSION` + 顶部版本注释 + `_migrate()` 幂等 ALTER。**改版本号必须同步 `tests/test_blackboard.py` 里硬编码的 `== <N>` 断言**（有两处：`test_schema_v7_migration`、`test_schema_v11_migration_idempotent`）。
- 迁移写法惯例：`if cols and "x" not in cols: ALTER TABLE ... ADD COLUMN x TEXT NOT NULL DEFAULT ''`（幂等、老库开即补、历史行不回填）。
- 本地 Python：项目自带 `.venv`（`./.venv/Scripts/python.exe`），测试 `./.venv/Scripts/python.exe -m pytest tests/... -q`。

## 本机环境已知坑

- FastAPI 0.141.1 / Starlette 1.6.0 已移除 `FastAPI.add_event_handler` → **所有走 `create_app()` 的 API 层测试在本机必报 `AttributeError`**（约 7 个，如 `test_chat_workbench.py::test_chat_agents_and_thread_api`）。属环境不兼容，非代码缺陷；store/黑板/runtime 层测试正常。
