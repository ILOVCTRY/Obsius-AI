# 资产合并：同键发现改为并集（merge-assets finding union）

> **状态**：已实施（2026-10-09，方案 B 落地；见下方 §0-bis 实施记录）
> **关联代码**：`core/blackboard/store.py`（`merge_assets` / `_fold_finding_into_target` / `_migrate_finding_refs` / `add_finding` / `merge_finding_evidence`）、`core/blackboard/schema.py`、`core/agent/tools.py`（`_tool_bb_merge_assets`）、`core/agent/tool_registry.py`（`bb_merge_assets`）、`tests/test_blackboard.py`、`core/blackboard/CLAUDE.md`
> **一句话**：`bb_merge_assets` 遇到「源资产与目标资产各有一条同 `dedup_key` 的发现」时不再整单拒绝，而是按 `add_finding` 同口径把源发现**折并**进目标发现（证据并集、severity 就高），再照常完成合并。

## 0-bis. 实施记录（2026-10-09）

按方案 B 落地，与方案零偏差：

- `merge_assets` 冲突块改为折并/迁移计划；新增私有方法 `_fold_finding_into_target`（row→row 并集，复用 `merge_finding_evidence` + `SEVERITY_RANK`）与 `_migrate_finding_refs`（`chain_links.node_id` + 各发现 `evidence.relates_to`，改写后指纹去重、丢自环）。
- 意图循环扩展：`basis_refs` 的 `finding:` 前缀与 `outcome_refs` 随 `asset:` 引用一并迁移（单次 UPDATE）。
- 逐条落 `finding.merged` 事件；`asset.merged` payload 增 `findings_merged`；`findings_moved` 收紧为「仅迁移」计数。
- `_tool_bb_merge_assets` 回执加 `merged=N`；`tool_registry` 描述去「发现去重键冲突…会拒绝」。
- 测试：`test_merge_assets_rejects_binary_and_descendant`（去冲突子用例）+ 新增 3 例（折并、混合、引用迁移）；`tests/test_blackboard.py tests/test_intents.py tests/test_tool_registry.py = 175 passed`、`tests/test_agent.py tests/test_attackpath.py tests/test_kb_proposals.py = 161 passed`。
- 文档回写：`core/blackboard/CLAUDE.md` 坑条、`core/agent/CLAUDE.md` 工具清单已同步。
- §7 待打磨项落实：①私信 `_notify_finding_updates` 为 no-op 桩（任务机制退役后恒无投递目标），故**不接**，只落事件；②`findings_moved` 口径变更消费方仅 `_tool_bb_merge_assets` 回执（已同步）；③源侧 author 不留痕（进 `finding.merged` 事件）；④title 保留目标侧（源侧 title 进事件）。

## 0. 拍板记录

| 决策点 | 结论 | 出处 |
|--------|------|------|
| 根因定性 | 设计自锁：`merge_assets` 的冲突前置校验 vs `UNIQUE(project_id,target_asset_id,dedup_key)`；`add_finding` 同键能并、`merge_assets` 同键却拒，语义不一致 | 2026-10-09 会话 |
| 方向 | **方案 B**：`merge_assets` 遇同键发现走并集（对齐 `add_finding`），而非拒绝 | 用户拍板 |
| 折并语义 | 目标资产上的那条为主条（保留 id），源发现证据并集并入后被删除 | 本方案 |
| 默认行为 | 无新增参数：默认即并集（`merge_assets` 本就不可逆、已要求 reason） | 本方案 |
| binary / 子资产重复 | 维持原拒绝语义，不动 | 本方案 |
| 未采纳 | A 数据侧手工预去重（本次绕行解，不治本）；C 簿记型发现不落 findings（架构改动大，另立方案）；D 冲突处理开关（B 已够，不引入旋钮） | 2026-10-09 会话 |

## 1. 背景与问题

实战入口：资产清理时把 `webvpnehall.scsw.edu.cn`（asset-5b5933fa9b22）并入 `webvpn.scsw.edu.cn`（asset-9ffb7cada549，同 WebVPN 实体、同解析 IP），被 `合并被拒：会造成发现去重键冲突。find-3d8893257cfc …` 拦下——两条 `vuln_class=dns-entity-dedup` 的**簿记型发现**各挂一个资产，合并后同 `(project, target, dedup_key)` 撞唯一约束，被前置校验整单拒绝。

> 讽刺点：正是 recon 用来标记「这两个域名是同一实体」的那两条去重发现，挡住了按该同一性merge 两个资产的动作。资产清理里这类折并不可避免，每次手工拆键不可持续。

## 2. 现状（代码事实）

- 表约束：`findings UNIQUE(project_id, target_asset_id, dedup_key)`（`schema.py:211`）；`dedup_key TEXT NOT NULL`，无键时 = 自身 id（永不合并）。
- 去重键取值：`key = dedup_key or vuln_class or None`（`store.py:1787`）——两条 `dns-entity-dedup` 发现因此同键。
- `merge_assets` 前置校验（`store.py:1350-1364`）：源侧每条发现的 `dedup_key` 去目标侧找同键行，命中即 `raise ValueError("合并会造成发现去重键冲突，未执行: …")`。
- 通过校验后：`UPDATE findings SET target_asset_id=目标 WHERE target_asset_id=源`（`store.py:1396-1399`）整体重挂。**若允许同键，此处直接踩唯一约束**——所以前置拒绝是"宁严勿松"的兜底，不是 bug。
- 对照语义：`add_finding` 命中同 `(target, key)` 走**证据并集**（`merge_finding_evidence`，`store.py:1821`），返回 `merged=True`。**新增能并、合并资产却拒**，是本次要抹平的不一致。

## 3. 设计（M1）

改动集中在 `merge_assets`：把「冲突即拒」换成「冲突即折并」。

### 3.1 折并计划先算

替换 `store.py:1350-1364` 的冲突块：

1. 取源侧全部发现（`id, dedup_key`）。
2. 逐条在目标侧按 `dedup_key` 查同键行：
   - 命中 → 记入 `folds = [(source_finding, target_finding)]`（**折并**，源行将删除）；
   - 未命中 → 记入 `moves`（**迁移**，源行重挂，沿用现状）。
3. 不再因命中断言失败。

> 1:1 保证：源侧 `dedup_key` 自身唯一、目标侧同键唯一 ⇒ 每条源发现至多折并到一条目标发现，无一对多。

### 3.2 折并一条（row→row 并集）

目标行 **保留 id**，就地长（语义 = `add_finding` 合并分支，但并的是两个既有行而非"新提交"）：

| 字段 | 折并规则 |
|------|----------|
| `evidence` | `merge_finding_evidence(target_ev, source_ev)`（列表键按内容指纹并集、notes 分段追加、其余键旧值优先补空） |
| `severity` | `SEVERITY_RANK` 就高 |
| `rating_basis` | 随 severity 就高：就高来自源侧则取源侧 basis，否则保留目标侧 |
| `status` | 任一 `verified` ⇒ `verified`（不降级） |
| `confidence` | `MAX(目标, 源)` |
| `poc_artifact_id` | 目标侧空则由源侧补 |
| `pocs`(列) | 按内容指纹并集 |
| impact / remediation / summary / affected_assets / test_environment / reproduction_steps / verification_result / risk_assessment | 目标侧非空保留、空缺由源侧补入（不覆盖既有结论） |
| `category` / `title` / `vuln_class` / `dedup_key` / `author` / `created_at` | **保留目标行**（规范侧）；源侧取值见 §3.4 事件 |
| `updated_at` | 刷新；`revision+1` |

### 3.3 引用迁移（关键，勿漏）

源发现行将被删除，须先把全项目对它的引用改指到目标发现——**沿用 merge_assets 对资产引用 `asset:<id>` 的既有做法**：

- `intents.basis_refs`：`"finding:<src>"` → `"finding:<tgt>"`（去重）。
- `intents.outcome_refs`：`src` → `tgt`（去重）。
- `chain_links`：`node_type='finding' AND node_id=src` → `node_id=tgt`。
- `findings.evidence.relates_to[].finding_id`（全项目扫描各发现 evidence 的 JSON）：`src` → `tgt`。
- **自环清理**：折并时若目标行并集后的 `relates_to` 出现 `finding_id == 目标行 id`（源侧原本引用目标、折并后成自环），丢弃该边。

> `finding_updates.ref_id`（`basis_stale`/`finding_update` 历史行）为**审计留痕，不迁移**——历史指向当时的事实。

### 3.4 事务与事件

- 全部步骤在 `merge_assets` 既有单 `_tx()` 内（读-改-写同事务，无丢失更新）。
- 折并完成、删源行前/后，逐条落 **`finding.merged`** 事件（复用 add_finding 合并分支同 kind）：
  `{finding_id: 目标id, merged_from: 源id, source_asset_id, target_asset_id, severity, reason: "asset.merged"}`。
  用于发现级审计留痕（哪条并进了哪条）。
- `asset.merged` 事件 payload 增 `findings_merged`（折并计数）；`findings_moved` 语义收紧为**仅迁移**计数（现为源侧全量，需同步改口径与消费方）。
- 私信 `_notify_finding_updates`：折并有实质增补时复用（与 add_finding 合流通知同口径）；**首版可只落事件不私信**，标为待打磨项。

### 3.5 返回值与工具回执

- `merge_assets` 返回 dict 增 `findings_merged: int`（并保留 `findings_moved`）。
- `_tool_bb_merge_assets`（`tools.py:728-732`）汇总串增 `merged=N`。
- `tool_registry.py` 的 `bb_merge_assets` description（`265-272`）：删「发现去重键冲突…会拒绝」，改为「同键发现自动并集并入目标发现」。

## 4. 边界与坑

- **binary 仍拒**（`store.py:1337`）、**子资产重复仍拒**（`1366-1381`）——本次不动。
- **折并可能改变发现历史**（两行并一行），靠 §3.4 事件留痕；severity/status 只升不降，无信息丢失（源侧原文进并集 evidence/notes）。
- **`vuln_class` 不同的同键对**：保留目标 `vuln_class/title`，源侧值记入事件（同键即声明同一实体，以规范侧为准）。
- **源侧无发现 / 无同键**：行为与现状完全一致（纯迁移），零回归面。
- **`findings_moved` 口径变更**会影响既有断言与前端/回执显示，需全量 grep 消费方。

## 5. 测试

- 改 `tests/test_blackboard.py::test_merge_assets_rejects_collision_binary_and_descendant`：拆分——「去重键冲突」子用例改为**断言合并成功**且折并正确（源行消失、目标行证据并集、severity 就高、`findings_merged==1`）；binary / 后代两个 `pytest.raises` 保留。
- 新增：
  - 折并留痕：`finding.merged` 事件 + `asset.merged.findings_merged`。
  - 引用迁移：`intents.basis_refs`/`outcome_refs`/`chain_links`/跨发现 `relates_to` 的 `src→tgt` 改指 + 自环清理。
  - `findings_moved` 只计迁移、`findings_merged` 只计折并。
- 回归：`tests/test_blackboard.py`、`tests/test_agent.py`、意图/链路图相关用例。

## 6. 文档回写（实施时）

- `core/blackboard/CLAUDE.md`：`merge_assets` 行由「…发现去重键冲突…会拒绝」改为「…同键发现按 add_finding 口径并集折并（§5.3），binary/子资产重复仍拒」。
- `core/agent/CLAUDE.md` 工具清单 `bb_merge_assets` 一句同步。
- `DESIGN.md`：实施落地时回写 §黑板（merge_assets 语义）——**定稿前不写 DESIGN.md、不挂账**（本方案先行）。

## 7. 待打磨清单

1. 折并是否触发 `_notify_finding_updates` 私信（复用 vs 仅事件）。
2. `findings_moved` 口径变更的前端/回执消费方全量盘点。
3. 源侧发现 author 是否需在目标行留痕（如 `evidence.merged_authors`）。
4. 折并后目标行 `title` 是否需拼接源 title（现方案保留目标）。
