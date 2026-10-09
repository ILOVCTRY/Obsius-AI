# 资产三态（asset-tri-state）

> **状态：已实施（2026-10-09，同日定稿并落地）**。设计讨论脉络见对话（态势该怎么
> 做的讨论收尾于本方案）。决策已回写 DESIGN.md §三「资产三态」/§四「覆盖度对账」。

**关联代码**：`core/coverage.py`（`is_settled`/`tri_state_of`/`asset_terminal_state`/
`coverage_report`/`effective_status_map`）、`core/orchestrator/orchestrator.py`
（`_assets_view`）、`tests/test_coverage.py`、`tests/test_orchestrator.py`、
`tests/test_api.py`。

**拍板记录**：

| # | 决策点 | 结论 |
|---|---|---|
| D1 | 三态词表 | `open`(未测试)/`visited`(已访问)/`tested_clean`(已测干净)，与 DB status 同名 |
| D2 | 有发现 = 已测干净？ | **否**——`terminal-finding` 不再是收口（本次核心修复） |
| D3 | `scanning` | 并入 `visited`（读时归一，零迁移；写入门仍接受） |
| D4 | `na` / `dead_end` | 均归 clean（测过了，结论「没什么可测」/「此路不通」） |
| D5 | `budget_stop` | 算 `visited`（预算停 ≠ 测完） |
| D6 | 显式 clean 后补洞 | 读时降级 `visited`（发现压过显式 clean），不跑迁移 |
| D7 | 加子破 clean | 保留（父随子树读时派生；加未测子 → 父掉出 clean） |
| D8 | 主控态势注入 | **同日已落地**：`coverage.situation_snapshot` 三态分桶 + `chat/runtime._situation_context()` 注入主控 system |



## 一、要解决的问题

现有口径（`core/coverage.py` `asset_terminal_state`）把**有 finding 挂链**也算
「收口」：

```python
if non_fp:
    return "terminal-finding"          # 被当作终态
...
settled = ts.startswith("terminal")    # ← terminal-finding 也被算 settled
```

后果是方向性错误，不是精度问题：

1. **带洞资产被标「已测尽」**：host 11 个适用面只测了 1 个、出 1 个高危 →
   `terminal-finding` → `settled=True` → 从 uncovered 消失 → 恰恰是最该继续深挖
   的资产被判定为「测完了」。
2. **父节点派生出自相矛盾的行**：子节点出洞 → `settled=True` → 父节点
   `all_settled` → 派生 `tested_clean`，同时 `has_findings=True`（沿树上传）——
   `{status: "tested_clean", has_findings: true}`，前端渲染「已测试干净」徽章，
   可它挂着一个洞。
3. **后补的洞作废不了显式 clean**：叶子显式标 `tested_clean`（落库）后又挂洞，
   叶子走 explicit 分支，永远显示 clean。

## 二、定稿口径：三态

| 状态 | 落库值 | 判据 |
|---|---|---|
| **未测试** | 不设（`open`） | 从没碰过 |
| **已访问** | `visited` | 碰过 **∧**（有未覆盖适用面 **∨** 有洞） |
| **已测试干净** | `tested_clean` | 碰过 **∧** 适用面全覆盖 **∧** 无洞 |

要点：

- **有发现 ≠ 已测试干净（本次核心）**。`terminal-finding` 仍是独立一味（供
  `has_findings` 识别），但**不计入收口**。收口味只剩 `tested_clean / na /
  dead_end`（`na` 归 clean——测过了，结论是「没什么可测」；`dead_end` 归 clean
  ——此路不通）。
- **`scanning` 归并进 `visited`**。派发是一批一批派、非任务认领，不存在孤儿半程
  （`active` 标识主控不需要）。写入门仍**接受** `scanning` 与存量行，读时一律归一
  为 `visited`——零迁移。
- **发现压过显式 clean（读时降级）**：叶子显式 `tested_clean` 但挂着非 FP 发现 →
  读时降级 `visited`、`settled=False`。不跑存量迁移，历史行不动。
- **加子破 clean（已有，本次保留）**：父节点加一个未测子 → 读时派生自动掉出 clean。

## 三、实现（`core/coverage.py`）

- 新增 `is_settled(flavor)`：收口味判定（`_SETTLING_FLAVORS`，**不含
  `terminal-finding`**）。
- 新增 `tri_state_of(flavor)`：对账态 → 三态（收口味→`tested_clean`、
  `terminal-finding`→`visited`、`visited`→`visited`、其余→`open`）。
- `asset_terminal_state`：`scanning` 不再单独返回（并入 `visited`）；其余判定顺序
  不变（状态机终态 > 死路标记 > 发现挂链 > 半程 > open）。
- `effective_status_map`（**授权读时派生**）：
  - 叶子：`own_findings = any(f.status != "false-positive")`（**直接查发现列表，不由
    `ts` 反推**——`asset_terminal_state` 里状态机终态压过发现，显式 clean 带洞时取不到
    `terminal-finding`，这是实施中踩到并修掉的坑）；`status = visited if own_findings
    else tri_state_of(ts)`、`settled = (status == tested_clean)`、`has_findings =
    own_findings`。→ 显式 clean 带洞自动降级。
  - 有子：`has_findings = any(子)`；若 `has_findings` → `visited`；否则全子
    `tested_clean` → `tested_clean`；全子 `open` → `open`（父未测）；否则
    `visited`。`basis=derived`。
- `coverage_report`：
  - `is_converged` 用 `is_settled` 替 `startswith("terminal")`——带洞资产不再收敛，
    留在 uncovered。
  - 组内 `terminal` 计 = 收口数（已测干净）；`uncovered[].state` 输出三态。
  - 组内 open 优先排序不变。

## 四、消费方对齐

- `orchestrator._assets_view()`：出口按三态重排——`untested`（未测试清单）/
  `in_progress`（已访问，未测尽，**含带洞资产**）/ `clean_count`（已测干净，只给
  计数防重复派）。`_covered_ids` = 已测干净（`high_value.covered` 语义随之收紧）。
  `by_status` 三态计数。
- `api` 资产列表装饰（`attach_effective_status`）：字段不变，语义随三态。
- 主控（workbench chat-orchestrator）态势注入（**D8，同日落地**）：
  `core/coverage.py::situation_snapshot` 纯函数三态分桶（untested/visited 清单 cap +
  `clean_ids` + `counts`，与 `effective_status_map` 同口径）；`chat/runtime.py::_situation_context()`
  渲染成「## 项目资产态势」段注入主控 system（每轮重投影、读取失败降级占位），含派发
  纪律（优先未测试 / 已访问续深挖 / 已测干净勿重派 / `call_expert(asset_ids=...)` 划范围）。
  子专家仍走 `_mission_context`（分配资产台账 + 维度面），两者对称。

## 五、不做

- 不跑存量迁移：`effective_status_map` 是读时派生，派生 clean 不落库；叶子显式
  clean 带洞由读时降级兜。历史行一行不动。
- 不动 `na`/`budget_stop` 的写入口径（`na` 人工裁定；`budget_stop` 算 `visited`，
  宁严勿松）。
- 不改 `SETTLED` 之外的状态机写入门（父节点显式 `tested_clean` 仍被 store 拒）。
