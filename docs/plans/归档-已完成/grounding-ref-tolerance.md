# 计划步接地引用容错：kind 前缀歧义规范化

## 状态

**已实施**（2026-09-23 同日落地，实战痛点修复批次①）：`_resolve_step_refs` 加 `_REF_ID_PREFIX` 映射（**实施修正：kind 与 id 前缀非同名**——finding→`find-`/artifact→`art-`/blueprint→`bp-` 按实情映射，asset/func/task 同名；event 为自增整数无前缀不补）+ 精确未命中补前缀重试一次 + 规范化串落盘（去重按规范化串，两种写法同对象归一）+ 报错文案两处修正（格式示例教真实形态、悬空提示教两种拼法）。tests/test_blackboard.py `test_plan_step_refs_grounding` 扩四断言（裸短 id 补前缀命中落盘完整形态/完整 id 不二次补/补后仍悬空照拒/event 裸整数 id 直通），128 passed。定稿决策沉淀 core/blackboard/CLAUDE.md 计划模型行。

## 拍板记录

| 决策点 | 结论 |
|----|----|
| 修复取向 | **方案 A：服务端容错规范化**（B 只改报错提示被否——换个模型还会犯） |
| 动手时点 | 先落方案文档不动代码（用户指示），与其他修复批次一起排期 |

## 关联代码

- `core/blackboard/tasks.py:821-853`（`_REF_TABLES` + `_resolve_step_refs`——校验与报错）
- `core/agent/loop.py:1529` 附近（E2 拒绝熔断——三连拒挂起的触发器，本方案防的是被它拦下的源头）

## 1. 事故（2026-09-23 task-0805336bbffc，16 秒三连拒熔断）

编排器发布的 external-entry 任务认领后 16 秒熔断挂起：Agent（deepseek-v4-flash）调 `task_plan` 写计划，5 步里 4 步 refs 写 `["asset:784aac03d41c"]`，校验器精确匹配 `SELECT 1 FROM assets WHERE id='784aac03d41c'` 未命中 → 拒 → 重试同错 ×3 → E2 拒绝熔断 + 任务 fail。

**目标对象真实存在**（`asset-784aac03d41c` = host 202.196.32.120，同项目）——不是幻觉不是跨项目，是 id 写法歧义。

## 2. 根因：格式约定互相打架

黑板完整 id 自带与 kind 同名的前缀（`asset-<12hex>`），接地引用格式又是 `<kind>:<id>`：

- Agent 写 `asset:784aac03d41c`——语义上完全自然（kind=asset + 裸 hex），**LLM 最可能的拼法**
- 校验器要求 oid 为完整 id（`asset:asset-784aac03d41c`）——精确匹配零容忍

两个帮凶使 Agent 无法自救：

1. **报错示例本身教错**：`tasks.py:837` 格式报错示例 `如 finding:f1a2…` 就是裸短 id；
2. **报错提示自我矛盾**：`先 bb_query 查真实对象 id`——Agent 查到的真实 id 就是 `asset-784aac03d41c`，按「kind:id」语义拆掉前缀写回，查一万次也一样错。

## 3. 方案 A：服务端容错规范化

`_resolve_step_refs` 精确未命中时**自动补 kind 前缀重试一次**：

```python
hit = conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND project_id=?", (oid, project_id)).fetchone()
if hit is None and not oid.startswith(kind + "-"):
    oid2 = f"{kind}-{oid}"
    hit = conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND project_id=?", (oid2, project_id)).fetchone()
    if hit is not None:
        oid = oid2   # 规范化：后续 out 存 f"{kind}:{oid}" 完整前缀串
```

同时修两处话术：

- 格式报错示例改真实形态：`应为 kind:id 且 id 为完整黑板 id（如 asset:asset-784aac03d41c）`；
- 悬空报错补一句正确拼法提示，灭掉「查了也错」的死循环。

**两种写法都通，一次改动消灭整类失败，任何模型都不再栽。**

## 4. 实施注意

- `_REF_TABLES` 各 kind 与 id 前缀一一对应需实施时核对（asset/finding/artifact/func/event/task/blueprint → `new_id` 的 `<前缀>-` 约定，func_kb 表 kind=func）；个别不一致的 kind 按实情映射。
- `out` 存规范化串，下游（plan 渲染/门判定）无感。
- 测试：新增悬空→补前缀命中用例；报错文案断言更新；回归 test_phases / test_skills 相关断言。
