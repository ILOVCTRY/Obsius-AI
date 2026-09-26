# Agent 工具目录前端视图

- **状态**：**已实施（2026-09-24，1030 passed）**
- **关联代码**：
  - `core/agent/tools.py`：工具总表 `AGENT_TOOLS`（:77）、`_CONTROL_TOOLS`（:32）、`_PLAN_TOOLS`（:35）
  - `core/api/app.py`：新增只读端点（设置页只读端点参考 GET /api/gateway/config）
  - `webui/src/views/SettingsView.tsx`：tab 结构（:174-203）

## 0. 拍板记录

| 决策点 | 结论（2026-09-24 用户拍板） |
|--------|------------------------------|
| 功能定位 | **只读工具目录**：把 Agent 工具从「仅后端 schema、前端不可见」变为设置页可浏览 |
| 显示粒度 | **全集静态目录**：名称/描述/参数，按类别分组；全局可见、不依赖项目 |
| 动态可用性 | 不做：不标注角色 yaml 白名单 / browser 依赖 / 网关 runtime 的实际可用性（后置观察） |
| 编辑能力 | 不做（纯展示；工具定义是代码，不经前端改） |

## 1. 背景

`AGENT_TOOLS`（tools.py:77，list[dict]）是 Agent 全部工具的唯一总表，每项含 `name`、`description`、`input_schema`（properties / required / enum）。它只经 `_task_tool_schemas`（loop.py:1659）发给 LLM，没有任何 API 端点对外暴露。人类无法在前端回答「平台给 Agent 提供了哪些工具、某工具要什么参数」。本方案补一个只读端点 + 设置页 tab。

## 2. 实施切分

### M1 后端只读端点（core/agent/tools.py、core/api/app.py）

1. `tools.py` 新增分组函数 `agent_tool_group(name: str) -> str`，纯静态推导：
   - 命中 `_CONTROL_TOOLS` → `控制`；命中 `_PLAN_TOOLS` → `计划`；
   - 前缀/集合规则覆盖其余：`run_cmd` → `执行`；`bb_*` → `黑板`；
     `kb_*`/`skill_open`/`route_lookup`/`propose_pack_edit`/`list_symbols`/`decompile` → `知识`；
     `browser_*` → `浏览器`；`read_file`/`search_files` → `文件`；兜底 `其他`。
   - 规则表带注释：新增工具时按前缀落组，未知工具兜底不报错。
2. `app.py` 新增 `GET /api/agent-tools`：返回
   `{"tools": [{"name","description","group","input_schema"} ...], "groups": [...]}`，
   数据直接取自 AGENT_TOOLS，启动期无 IO、无 pid、无敏感字段。

### M2 前端（webui/）

1. 新建 `src/components/settings/AgentToolsPane.tsx`：
   - 挂载时 `api.getAgentTools()`；顶部搜索框（名称/描述子串过滤，大小写不敏感）。
   - 按 group 分 section 渲染工具卡片：名称（等宽小字）+ 描述（text-xs）+ 参数表——
     遍历 `input_schema.properties`：参数名、`required` 标红星、type（array 等显示完整）、
     enum 展开为 chip 行、参数 description。
   - 无参数工具显「（无参数）」；全局可见，无 pid 空态不需要。
2. `src/views/SettingsView.tsx`：tab 数组在 `gateway` 后插 `["tools","工具"]`；
   对应加 `<TabsContent value="tools" ...><AgentToolsPane /></TabsContent>`。
3. `src/lib/api.ts` 加 `getAgentTools()`；`src/lib/types.ts` 加
   `AgentTool { name; description; group; input_schema: { properties: Record<string, AgentToolParam>; required?: string[] } }`
   与 `AgentToolParam { type; description?; enum?: string[] }`。

## 2.5 实施修正注记

- 新增「协作」组（方案外细化）：`publish_task`、`request_authorization`、`request_escalation` 三工具按原规则会落「其他」，实施时补 `_COLLAB_TOOLS` 显式归组。最终组序：执行/文件/黑板/知识/浏览器/协作/计划/控制。
- `tools.py` 落点：分组函数 `agent_tool_group` 与常量 `TOOL_GROUPS` 放在 AGENT_TOOLS 定义之后；端点测试断言「无工具落其他」防漏配。

## 3. 测试（tests/）

- tests/test_api.py：①GET /api/agent-tools 200，tools 条数 == len(AGENT_TOOLS)，每项 name/group/input_schema 齐全；
  ②抽 bb_query 校验其 what 参数 enum 含六种查询面；③groups 与工具分组一致、无工具落「其他」（防新工具漏配规则）。
- 分组函数用例（可并入上面端点用例或 tools 单测）：run_cmd→执行、bb_query→黑板、
  complete_task→控制、task_plan→计划、browser_click→浏览器、read_file→文件。

## 4. 验证

1. `PYTHONIOENCODING=utf-8 /e/Miniconda3/python.exe -m pytest tests -q`：**1030 passed 全绿零回归**（新增 2 用例）。
2. `cd webui && npm run build`：零 TS 错误。
3. 手工冒烟：设置→工具，搜索「bb_query」能定位、参数表完整；分组顺序正确；无项目打开时也可浏览。
4. 不 commit（固定模式）。

## 5. 风险点

1. 分组规则是 AGENT_TOOLS 之外的第二处名称知识——新工具若前缀不符会落「其他」，测试③专门拦这一点。
2. 端点返回的是 LLM 视角原始 schema，字段可能含 anyOf/type 数组——前端参数类型渲染要兜底字符串化，不能只按 string 处理。
3. 描述文案较长，卡片注意换行与宽度，不横向溢出。
