# tested_clean 意图死路背书（资产「已测试干净」门禁收紧）

- **状态：已实施（2026-09-25），1133 passed；含存量回退（见 §0）**
- **关联代码**：
  - `core/blackboard/store.py` `set_asset_status`（tested_clean 门禁：note 非空 + 无子节点 + 死路意图背书）
  - `core/blackboard/intents.py`（意图死路收尾：dead_reason + evidence_refs ≥1 已强制；`dead_end_backing_target` 背书查询）
  - `core/coverage.py`（父节点 tested_clean 读时派生）
  - `core/agent/tools.py` `bb_asset_status`
  - `scripts/reset_cursory_clean.py`（存量粗略收口回退，已执行）
- **触发**：中原工学院项目 247 条 tested_clean 中，2026-09-24 两个会话批量粗略收口——
  sess-130528d12a81「80/443 各 1 GET 见登录页 → tested_clean」；sess-05b9dc2b6783
  「同模板 200，随批次收口」。平台侧这些资产零子节点、零 http_history、项目 intents 表为空。

## §0 实施修正（2026-09-25）

1. **D3 被用户开工指令推翻**：原「存量不审计」改为当日执行回退——新增
   `scripts/reset_cursory_clean.py`，109 个资产（81→open / 28→visited）全部
   回退，零跳过、脚本幂等（重跑 0 计划）。其余会话的 136 个 clean 未授权不动。
2. 背书查询助手收 **conn** 而非 bb（store 已在 `_tx()` 内，嵌套事务会炸）；
   子树关系实现为沿 parent_id **祖先链向上查**（比子树 BFS 更窄）。
3. 检查顺序：note（事务外）→ 行存在/乐观锁 → 同状态 no-op（背书检查之前，
   保存量兼容）→ 无子节点 → 死路意图背书。
4. 测试落点 tests/test_intents.py（+8：helper 直接/批次/非死路/旁支 + 状态机
   放行/拒/noop/na·budget_stop）；既有 5 文件 10+ 处直接标 clean 的剧本改走
   背书路径（test_coverage 加 `_mark_clean` 快捷路）。

## 拍板记录

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | tested_clean 写入口径 | **意图死路背书**：叶子资产标 tested_clean 时，服务端强制存在覆盖该资产的 closed/dead_end 意图；无背书 → ValueError 指引先 declare_intent→close_intent(dead_end) |
| D2 | 批量收口（站群/宿主「随主机收口」） | **同一意图覆盖批次**：意图 target=父节点（host/站群宿主/父域），其死路收尾为 target **子树内**叶子资产的 clean 背书（服务端校验子树关系）；不要求一 vhost 一意图 |
| D3 | 存量已标 tested_clean | **不自动审计/降级**（用户未选审计档）：门禁只拦新写，同状态 no-op 不受影响；存量可疑行由人工按需处理 |
| D4 | na / budget_stop | 不动：na=人工裁定不适用、budget_stop=被迫停手，维持 note 非空即可 |

## 1. 背书判定语义

`set_asset_status(asset_id, "tested_clean")` 在现有两道检查（note 非空、无子节点）
**之后**、UPDATE 之前，增加背书检查：

```
存在意图 i，同项目，status="closed" AND outcome_type="dead_end"，且：
  i.target_asset_id == asset_id            # 直接背书
  或 asset_id 位于 i.target_asset_id 的资产子树内   # 批次背书（D2）
```

- 意图死路在 `close_intent` 时已强制 dead_reason 非空 + evidence_refs ≥1 且引用真实
  存在（http/event/artifact）——「收尾必带证据」由意图层继承，本门禁不重复造证据语义。
- target_asset_id 为空的意图不参与背书。
- 子树关系沿 assets.parent_id 同项目内判定（父链不成环；实现用项目全量资产构建
  parent→children map 后 BFS，避免递归 SQL 的平台差异）。
- 找不到背书 → ValueError（文案=「tested_clean 须有死路意图背书：先 declare_intent
  声明可证伪假设，close_intent(outcome=dead_end) 带证据收尾后再标；批量面可对父节点
  立一条意图覆盖子树」）；工具层落 `[拒绝]`、API 422。

## 2. 落点与复用

- `core/blackboard/intents.py` 新增纯读助手
  `dead_end_backing_target(conn, project_id, asset_id) -> str | None`（**收 conn**——
  store 已在 `_tx()` 内，不能再开 bb 事务）：查项目全部 closed/dead_end 意图的
  target，逐一判定直接命中/子树包含，返回命中的意图 target_asset_id，无则 None。
- `store.set_asset_status` 调用该助手；注意检查点在「无子节点」检查之后（父节点
  tested_clean 本就拒绝，不进背书逻辑）。
- 不新增端点、不改 schema；coverage.py 无需改（父节点派生口径：子节点终态各自
  已带证据——tested_clean 有意图背书、na 人工、dead_end/finding 同理）。

## 3. 边界与不做项

- 同状态 no-op（已 tested_clean 再标）在背书检查之前返回 → 存量行不受影响（D3）。
- 资产挂有非 FP 发现时其语义终态本就是 finding（coverage 口径 terminal-finding），
  Agent 无需也不应再标 tested_clean；本次**不额外加拒收**（避免扩大改动面，留待
  实战中观察再议）。
- 不做 doctor 体检项、不做存量重置脚本（D3）。

## 4. 测试

在 tests/test_intents.py（或 test_blackboard.py）补：

1. 直接背书：意图 target=url 叶子且 dead_end 已收尾 → tested_clean 放行。
2. 批次背书：意图 target=host 死路收尾 → host 子树下 service/url 叶子标 clean 放行。
3. 无意图 / 意图仍 open / 意图 outcome=vuln·finding / 意图 target 为无关资产 → 拒。
4. 意图 target 为该资产父节点**之外**的旁支 → 不背书（子树边界）。
5. 同状态 no-op 无背书仍放行（存量兼容）。
6. na / budget_stop 无意图、仅带 note 仍放行（D4）。

## 5. 实施时文档回写

- DESIGN.md：资产状态机段补「tested_clean 须死路意图背书（直接/子树批次）」口径。
- core/blackboard/CLAUDE.md：store.py 条目补门禁、intents.py 条目补新助手。
- core/agent/CLAUDE.md：bb_asset_status 描述同步。
