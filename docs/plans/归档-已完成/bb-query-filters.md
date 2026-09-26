# bb_query 黑板查询过滤增强

- **状态**：**M1+M2 已实施（2026-09-24），M3 后置观察**（无排期，看实战是否仍频繁全量查询再立项）
- **关联代码**：
  - `core/agent/tools.py`：工具 schema `bb_query`（:333-361）、`_tool_bb_query`（:1445-1510）
  - `core/blackboard/store.py`：`list_findings`（:1524）、`list_assets`（:1166）、`recent_events`（:856）
  - `core/blackboard/tasks.py`：`TaskQueue.list_tasks`（:1424）

## 0. 拍板记录

| 决策点 | 结论（2026-09-24 用户拍板） |
|--------|------------------------------|
| 实施范围 | **M1 + M2 一批落地**：M1 接现成过滤能力（只改工具层）、M2 events kind 过滤+统一 limit（小改 store SQL） |
| M3 关键词搜索 | **后置观察**，本批不定 LIKE/FTS5；实战确认有痛点再立项 |
| events kind | 多值 `kinds`（一次可指定多种事件类型） |
| limit 默认值 | **默认不动**（events 仍 50、其余仍全量），只新增旋钮，避免漏数据 |
| 结构性可选项 | 不做：`what` 多值查询（结果复杂易爆上限）、字段投影 `fields=`（心智负担高） |

### 实施注记（2026-09-24 M1+M2 落地）

- 落地范围与拍板一致，无设计偏差；DESIGN.md 已回写「黑板查询过滤」节。
- **测试坑（审计自污染）**：`dispatch("bb_query")` 本身经 dispatch 统一包装落 `tool.call` 审计事件（name≠run_cmd 时无条件落），events 查询默认集会把自身审计事件查回来。events 相关断言必须用 `kinds=["command","finding.new"]` 锁定过程事件才能稳定。
- 闭集校验表（tools.py `_BB_SEVERITIES/_BB_ASSET_STATUSES/_BB_TASK_STATUSES/_BB_ASSET_TYPES`）= 第二处值域声明，已按风险点 4 加同步注释。
- 验证：全量回归 **1071 passed**；`npm run build` 零 TS 错误。

## 1. 背景

`bb_query` 六个查询面（findings/assets/events/tasks/func/blueprint）中，只有 assets 支持组合过滤；findings 仅能按资产过滤，tasks/blueprint 无过滤，events 固定取最新 50 条且不分 kind。多个高频过滤维度**底层 store 已实现、工具层没接出来**。

实际损耗：Agent 全量拉回 JSON → 占上下文 token；结果过大触发 spill 落盘（`_maybe_spill`）→ 再 read_file/grep 二次取回；过滤值拼错时静默返回 `[]`，易被误判为「没有」。本方案把 Agent 的取数方式从「全量拉回本地找」改为「按条件精确取数」。

## 2. 现状与缺口

| what | 已暴露过滤 | 底层有、工具未接 | 底层也没有 |
|------|-----------|------------------|------------|
| assets | type + status | tag（meta.tags，store.py:1167） | value 关键词 |
| findings | target_asset_id | min_severity、verified_only、category（store.py:1524-1531） | title 关键词 |
| tasks | 无 | status（tasks.py:1424） | objective 关键词 |
| events | 无（固定最新 50 条全 kind） | session_id、分页参数 | **kind 过滤（需改 store SQL）** |
| func | binary_sha256 + address + risk_tag | — | 函数名搜索 |
| blueprint | blueprint_id | — | status |

不暴露项维持现状：func/blueprint 的现有过滤已满足其查重主流程，本批不扩。

## 3. 实施切分

### M1 / M2（已实施 2026-09-24，从切分剔除）

M1 接现成过滤（findings min_severity/verified_only/category、tasks status、assets tag + 闭集校验）与 M2（events kinds IN 过滤+session_id、六面统一 limit）已落地，定稿与实施细节见 DESIGN.md「黑板查询过滤」节及文首实施注记。

### M3 关键词搜索（后置观察，本批不实施）

设想：assets.value / findings.title / tasks.objective 加 `q=`。单项目黑板量级下 `LIKE '%q%'` 可能够用；FTS5 需虚表+同步触发器+迁移。等 M1/M2 上线后看 Agent 是否仍频繁全量查询或 spill，再定方案。

## 4. 测试（tests/）

- tests/test_blackboard.py（或对应 store 测试文件）：①recent_events kinds 单值/多值过滤；②kinds 与 session_id/tail 组合；③空 kinds 语义不变。
- tests/test_agent.py：④findings 三个新过滤透传（构造不同 severity/verified/category 行各一条）；⑤tasks status、assets tag 过滤；⑥非法闭集值返回 `[错误]` 且不触库；⑦limit 裁剪（events tail 生效、其余面 rows 截断、非法值钳制）；⑧events kinds 端到端。
- 回归断言：不传任何新入参时，六个面的返回与现状完全一致（默认不动原则）。

## 5. 验证

1. `PYTHONIOENCODING=utf-8 /e/Miniconda3/python.exe -m pytest tests -q`：**1071 passed 全绿**（2026-09-24）。
2. `cd webui && npm run build`：零 TS 错误（本批无前端功能改动，仅回归确认）。
3. 新增测试：test_blackboard.py `test_recent_events_kinds_filter`；test_agent.py findings 过滤透传 / tasks·assets 过滤与闭集报错 / events kinds·limit 三个用例。
4. 不 commit（固定模式）。

## 6. 风险点

1. `recent_events` 被 WS 增量、前端分页等多处复用——kinds 默认 None 必须保证 SQL 零变化，新增分支只在显式传值时拼接。
2. tasks status enum 值域须以代码实际使用的状态串为准（先核 tasks.py 全部状态写入点），不要凭文档猜。
3. limit 在应用层裁剪大结果集不省查询本身的 IO；真正的省 token 靠 M1 过滤先收窄，文档不夸大。
4. 闭集校验表是第二处值域声明，注释标明须与 store/枚举定义同步。
