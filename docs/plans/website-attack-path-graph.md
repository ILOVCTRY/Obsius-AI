# 单站攻击链路图（website-attack-path-graph）

> 用户 2026-09-24 第 4 点：「对一个网站的攻击要有攻击链路图。如对 xxx 网站进行攻击，每次测试行为都要有登记和结果……大致图，不一定要精确到每一步。」
> 同日定稿翻转：「**图这样做，思考，规划，执行。意图就是规划的内容，每个意图必须收尾，要么是发现，要么是漏洞，要么是死路。**」——图的主脊从「攻击尝试时间图」升级为「目标 → 意图（规划产物）→ 收尾」三段模型（v3）。

- **状态**：**v3（M1+M2+意图层）已实施（2026-09-24，决策回写 DESIGN.md §三）；M4 跨站泳道联动仍后置**——等 vuln-chain-graph M1 provides/requires 人工确认链边数据落位
- **关联代码与数据**：
  - `core/blackboard/intents.py`：意图写入口（declare/close/reopen + normalize_refs，模块函数经 `bb._tx()`，不旁路单一写入口）
  - `intents` 表（schema **v22**，DDL IF NOT EXISTS 零 ALTER）：statement/target_asset_id/basis_refs/status/outcome_type/outcome_refs/dead_reason/evidence_refs/revision
  - `core/blackboard/attackpath.py`：`build_attack_path` 纯只读组装（intents 走 intents.list_intents；assets/findings/history 鸭子依赖）
  - Agent 工具 declare_intent（计划组）/ close_intent / reopen_intent（控制组）；finish 门禁；五处快照带 open_intents（core/agent/loop.py、tools.py）
  - `webui/src/views/blackboard/AttackPath.tsx`：v3 画布——主脊卡 + 点意图展开执行层尝试条 + 侧栏原始请求 + 人类重开钮
  - `http_history`（schema v15）/ findings.category（vuln/intel，v12）/ traces.py

## §0 实施修正注记（2026-09-24，v3 落地相对拍板的偏差）

1. **数据落点**：意图独立轻表 `intents`（不复用 tasks/chains），零 ALTER 建表，schema 21→**22**；迁移护栏 test_schema_v7/v11 同步钉 22 并断言 intents 表。
2. **attackpath 取意图**：初稿写「bb 鸭子依赖 list_intents」，实际 intents.py 是模块函数族、Blackboard 不挂该方法——已改为 attackpath 直接 `from core.blackboard.intents import list_intents`（真实黑板集成测试曾以 AttributeError 暴露此缺口）。
3. **执行层归属是读时属性**：attempt.intent_id 不落库，按意图存活窗 `[created_at, closed_at]`（open 意图尾端 +∞）现算；重叠窗归**最新声明**的意图；无时间的合成节点按收尾发现（finding_owner）归属。
4. **exec 边不进主脊 edges**：执行层相邻边单独出在 `exec_edges`（kind='exec'，按意图桶分，含无主桶）；主脊 edges 只有 outcome/derive/bypass。
5. **死路隐藏是渲染口径**：节点与 derive 边照常返回（intent 节点带 default_hidden），bypass 边服务端预复合；前端开关仅在死路计数 >0 时出现。
6. **DAG 断言范围**：Kahn 只校验主脊边；bypass 是路径复合、明确排除；端点不在节点集同样 AssertionError。
7. **finish 门禁例外**：首次 finish 有 open 意图直接拒绝；**允许第二次 finish 强制结束**（防模型无法收尾时人类被永久堵住），拒收只发一次不重复打扰。
8. **reopen 保留 evidence_refs**：事实引用不删；清 outcome_type/outcome_refs/dead_reason/closed_at；下游不级联重开。
9. **测试增量**：test_intents.py 新增 41；test_attackpath.py 18→31（全量 1071→**1125 passed**）；前端构建零 TS 错误。

## 拍板记录

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 现有「漏洞链 / 有效发现链」两套**链路渲染图** | **舍弃**：FindingsCanvas 退役、旧画布五件套删除 |
| D2 | 替代形态 | 发现页「链路」子视图改为单站攻击链路图；默认只显**一个指定 IP/域名**，未选显目标选择引导 |
| D3 | 硬切换开关 / catView 过滤 | **保留不动**（只弃图不弃功能）；图内容不受类别开关影响 |
| D4 | 全景 BoardGraphCanvas | 不动，总览/纵深两层 |
| D5 | findings.category | 保留照旧 |
| D6 | 跨站成链显示范围 | 默认单站；**仅当本站漏洞利用实际用到另站漏洞**才带出被依赖站泳道，链边取 vuln-chain-graph 人工确认边（M4 后置） |
| D7 | 图方向 | **严格有向无环**，恒向右，重试折叠不生回边 |
| D8 | **图的语义模型**（用户翻转定稿） | 主脊 ``目标 → 意图（规划产物·可证伪假设）→ 执行（动作组）→ 收尾：漏洞 \| 有效发现 \| 死路``；产出物可回流出新意图/新目标 |
| D9 | 意图是什么 | 一句**可证伪假设**；open/closed；目标=host/domain 资产根；意图不复用 tasks/chains，独立 intents 表 |
| D10 | 四条硬规则 | ①意图必收尾 ②收尾必带证据 ③边仍是逻辑推导（basis_refs；服务端 Kahn DAG） ④死路是意图关闭态（持久、默认隐藏+bypass、新证据重开） |
| D11 | 默认口径 | 一意图可挂多条同类发现；死路要求零发现；关闭可重开（FP/新证据→open，**下游不级联**）；AI 可自收尾，人类可驳回/重开 |
| D12 | 执行层怎么上图 | http_history/工具调用**读时**按意图存活窗归属（不改造写入链）；时间序尝试图保留为展开细节（点意图卡展开） |

## 1. v3 语义模型（实施版）

```
目标(target)                意图(intent)                 执行(attempts)              收尾
┌─────────┐  derive   ┌──────────────────┐  读时存活窗   ┌──────────────┐
│ host/   │ ────────→ │ 可证伪假设(open) │ ←──────────→ │ 测试点尝试组 │
│ domain  │ basis_refs├──────────────────┤  [created,   │ (时间序,可折叠)│
│ 资产根  │ ◄──────── │ closed: 三选一   │   closed]    └──────────────┘
└─────────┘  逻辑推导  └──────┬───────────┘
                             │ outcome
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         漏洞 vuln      有效发现 intel    死路 dead_end
     (category=vuln)   (category=intel)  (死因+证据;默认隐藏)
```

**收尾必带证据（宁严勿松，证据不足保持 open）**：

| 收尾 | 证据要求 |
|------|----------|
| vuln | finding_ids ≥1：存在、同项目、非 FP、**category=vuln** |
| finding | finding_ids ≥1：同上，**category=intel** |
| dead_end | dead_reason 非空（什么证据排除假设、试过什么）+ evidence_refs ≥1（http/event/artifact） |

**FP 规则**：误报发现不能支撑 vuln/finding 收尾（错误文案直接指引走 dead_end）；FP 本身即「假设被证据否定」，按死路关闭。

**意图必收尾的运行面**：Agent 五处快照（建任务/chat/恢复等）带 open_intents 清单；`finish` 首次有未收尾意图→拒绝并列出清单，第二次允许；续跑注入「🧾 收尾提醒」。doctor 是静态 packs 体检，不挂运行时状态。

## 2. 执行层：读时归属（不改造写入链）

1. 测试点归一沿用 M1：method+路径模板（`/user/123→/user/{id}`、UUID/长 hex 折叠）+ query 键排序去重；同点 30min 窗折叠；跨任务跨会话不切割。
2. 五档结果 found/hint/blocked/no_reaction（skipped 枚举保留不自动判定）；found=挂非 FP finding。
3. 归属窗 `[created_at, closed_at]`：窗内执行归该意图；多窗重叠→**最新声明者胜**（执行者焦点）；无时间的 curl 缺口合成节点→按其收尾发现反查 owner。
4. exec 边：同意图桶内时间相邻尝试相连（展开细节）；无意图尝试在 None 桶相邻。
5. 站点边界=目标资产全后代子树；意图入图条件=target_asset_id 在子树，或 basis_refs 中资产/发现在子树。
6. 死路节点 default_hidden；`_bypass_edges` 为其入边×出边复合穿通边（已有直连边不重复造）。

## 3. API 与工具

- GET `/api/projects/{pid}/attack-path?target=<asset_id>`：返回 `{target, nodes, edges, attempts, exec_edges, counts}`；GET `/api/projects/{pid}/intents?status=`；POST `/api/projects/{pid}/intents/{iid}/reopen`。
- Agent：`declare_intent(statement, target_asset_id?, basis_refs?)`（计划组）、`close_intent(intent_id, outcome, finding_ids?/evidence_refs?/dead_reason?)`、`reopen_intent(intent_id, note?)`（控制组）。
- 全部写经 intents.py → `bb._tx()`；事件 intent.declared/closed/reopened（sess- 作者带 session_id）。

## 4. 里程碑

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 | attackpath 测试点归一 + 端点 + 拆旧画布 | ✅ |
| M2 | AttackPath.tsx 目标引导/单站图/原始请求侧栏 | ✅ |
| 意图层（v3） | intents 表 v22 + 三工具 + finish 门禁 + 主脊卡/执行层展开 + 人类重开 | ✅ 2026-09-24 |
| M4 | 跨站泳道联动（D6） | 后置，待 vuln-chain-graph M1 |

## 5. 历史设计背景（已被 D8 翻转，留档）

v3 之前方案以「攻击尝试（attempt）」为主节点、按时间相邻连边；M1+M2 已按此实施。
v3 保留其全部归一化逻辑但**降级为执行层展开细节**：尝试不再是主脊节点，而是
意图存活窗内的动作组；时间只影响布局与归属，逻辑推导边（derive/outcome）才是主脊。

其余历史讨论（curl 侦察入口统一、skipped 态、command 事件解析）维持原结论：
M1/意图层只用 http_history + finding 键合成；curl 侦察经 run_cmd 的命令流量
不解析（无 finding 的 curl 探测不出图，仍可在事件流/traces 查到）。

## 6. 风险

| 风险 | 对策 |
|------|------|
| 意图悬挂不收尾 | 快照清单 + finish 门禁 + 续跑提醒（二次 finish 为人工兜底） |
| 证据不足硬收尾 | 服务端存在性/类别/FP 三重校验，不满足保持 open（宁严勿松） |
| 重开下游脏 | reopen 只改自身、不级联；evidence_refs 保留可追溯 |
| 长项目渲染体积 | 主脊只渲染意图/发现（数十），执行层按展开懒取；死路默认隐藏 |
| 误连跨站 | 只认人工确认 provides∩requires（M4 红线） |
