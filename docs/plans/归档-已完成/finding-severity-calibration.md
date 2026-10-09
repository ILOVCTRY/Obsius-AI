# 方案：漏洞上报防夸大（定级校准 + 就高约束）

- **状态**：**已实施（全层 P0–P3，2026-10-09）**——同日讨论收敛 → 用户批准「直接实施」→ 落地；全量 `1264 passed`，唯一 fail 为既有无关项 `tests/test_proxy_pool.py::test_end_to_end_forwarding`（干净树同样复现，Windows 线程/env 问题，与本方案无涉）
- **拍板记录**（用户 2026-10-09 两问）：
  | # | 决策点 | 结论 |
  |---|--------|------|
  | D1 | 改造范围 | **全层**（P0 自核对器 + P1 提示词清理 + P2 存储层；P3 报告层随本方案一并收） |
  | D2 | 自核对不通过的等级处置 | **降一档（保守）**——最多下调一个档位（high→medium），不按 judge 建议档暴跌 |
- **关联代码**：
  - `core/skills/judge.py`（`judge_finding` 共享判定面）、`core/agent/loop.py:1039-1059`（`_build_vuln_gate` C6 核对 hook 工厂）、`core/agent/tools.py:844-887`（`_tool_bb_add_finding` 门禁应用点）
  - `core/skills/rules.py:236-242`（评级硬指令注入）
  - `core/agent/tool_registry.py:356-514`（`bb_add_finding` 描述）、`:443-474`（impact/risk_assessment 字段描述）、`:1665-1675`（`request_authorization` rating_override）
  - `core/blackboard/store.py:30-34`（`FINDING_SEVERITIES`/`FINDING_CATEGORIES`）、`:93-125`（`_check_vuln_gates`）、`:198`（`SEVERITY_RANK`）、`:1809-1814`（合并就高）
  - `packs/experts/report-writer.yaml`（报告工程师 persona）、`packs/tracks/pentest/rules/rating/*.md`（评级口径）、`packs/tracks/pentest/phases/report.yaml`（报告阶段验收）
  - 既有同源范式：`scripts/cleanup_findings.py` `JUDGE_PROMPT`（keep/downgrade/to_intel 四选一 + 脚本侧后校验）

## 1. 问题

漏洞上报存在**夸大危害**：真实但危害有限的发现被报成高档（如低敏越权报 high/critical），且除人工 PATCH 外没有任何自动下行机制。用户要求检查并给改法。

## 2. 诊断：夸大从四个入口进来

夸大不是单点 bug，是「只升不降」在多层合力挤出来的。

**① 自核对器只判「够不够格算漏洞」，从不判「等级配不配得上证据」（最大缺口）**
`core/agent/loop.py:1039-1059` 的 C6 核对 hook 复用 `judge.py`，sys_prompt 只要求输出 `{"compliant": true|false}`，问的是「有可验证的安全问题吗」。而 `core/agent/tools.py:849-856` 已经把 `severity`、`has_poc`、`evidence_head`、`rating_basis` **全都喂给了它**——数据齐了，就是没问「等级」这件事。于是一个真漏洞但被高报的洞，一路放行进漏洞视图。

**② 评级硬指令是单向压力，没有反向刹车**
`core/skills/rules.py:236-242` 注入的「评级硬指令」只说「severity 必须按口径判级」，没有「证据不足以支撑高档就往下判」。再叠加 `packs/tracks/pentest/rules/rating/osrc.md` 里通篇价值/奖金导向（「核心业务价值≈测试环境 20~30 倍」「力气全砸高危+」「想拿大件就锁核心业务找 RCE/越权/SQLi 大面」）——模型被推着往高档靠。

**③ 字段描述诱导把危害写满**
`core/agent/tool_registry.py:443-446` 的 `impact`=「实际拿到什么数据/权限、影响面多大」、`:471-474` 的 `risk_assessment`=「CIA 后果」；`packs/experts/report-writer.yaml` persona 把定级写成「资产价值 × 判级条款」——把资产价值当乘数，天然抬级。

**④ 存储层棘轮 + 低档出口被封（副作用）**
`core/blackboard/store.py:1809-1814` 合并「severity 就高」——除人工 PATCH 外只升不降，反复重报会垫高。叠加 `store.py:93-109` 渗透/红队轨**全类别拒收 info**（2026-09-18 引入，本意防堆 info 充数），边界/疑似项没有低档去处，只能往 low/vuln 挤。

**旁证：反夸大纪律存在但不承重**
`packs/kb/web/playbooks/rules/vuln-report-format.md` 有很强的反夸大设计（L1~L3 判级硬序列、「防误报高危」专节、「证实危害=回显危害」），但它在 web kb 快照里，只能 `kb_open`/`skill_open` 按需打开，**不在系统提示里**，主路径读不到。

## 3. 定稿决策

见头部拍板记录 D1/D2。设计取向：**在「判定侧」加一道反向复验（降级），在「表述侧」去掉诱导，在「存储侧」解棘轮，在「报告侧」让既有纪律承重**——四处齐改，但不动 severity 的五档语义与既有 `rating_basis` 契约。

## 4. 设计详述

### 4.1 P0-A 自核对器升级为「合规 + 定级」双判（核心）

`_build_vuln_gate` 的判定 prompt 从二值 `compliant` 升级为四元，**对齐 `cleanup_findings.py` 既有范式**：

```
{
  "compliant": true|false,          // 是否够格算漏洞（现有语义，不变）
  "severity_ok": true|false,        // 所报 severity 是否被证据支撑（新增：反向复验）
  "suggested_severity": "low|medium|high|critical",  // severity_ok=false 时给出证据能支撑的档
  "reason": "一句话依据"
}
```

- prompt 增补一句反向复验指令：「用证据倒推——PoC/repro_steps 实际证实的危害只够低档就判低档；**证据不足以支撑所报等级时 severity_ok=false，宁低勿高**」；
- `judge_finding`（`core/skills/judge.py`）**保持不变**——它是通用解析面，输出 schema 由调用方 sys_prompt 控制；`cleanup_findings.py` 自带 sys_prompt 不受影响；
- **门禁应用点改造**（`_tool_bb_add_finding`，`tools.py:844-861`）：`vuln_gate` 返回从 `(ok, reason)` 改为 `(compliant, new_severity, reason)`——
  - `compliant=false` → category 降 intel（现有行为不变）；
  - `compliant=true` 且 `severity_ok=false` → **severity 降一档**（D2）：`new_severity = 比当前低一档`（`SEVERITY_RANK` 表），**下限 low**（渗透/红队轨 info 本就拒收）；同时 `rating_basis` 追加降级说明，保证 basis 证成新级别（复用 `cleanup_findings` 的「basis 必须证成当前 severity」口径）；
  - `compliant=true` 且 `severity_ok=true` → 原样登记；
  - judge 失败/无 JSON → `None` → 跳过放行（现有降级哲学不变，只回执标注）；
  - 回执（`tools.py:885-887`）增加 `severity_ok`/降级说明，供 agent 自检与人类审计；
- **`suggested_severity` 只作参考不直接落库**（D2 选降一档而非按建议档），但与「降一档后仍高于 suggested」的情况在回执里提示，供 agent 自愿再降。

**边界**：low 档且 `severity_ok=false`（证据连 low 都撑不起）→ 视为 `compliant=false`，降 intel。避免「降一档」在 low 处无路可走。

### 4.2 P0-B 评级硬指令补反向约束

`core/skills/rules.py:236-242` 的硬指令文案追加一句：

> 判级以**实证危害**为准；证据不足以支撑高档的，降到证据能支撑的档，**禁止就高凑档**。

与既有「必须按口径判级」并列，形成双向约束。

### 4.3 P1 提示词清理（去诱导）

| 位置 | 改法 |
|------|------|
| `tool_registry.py:443-446` `impact` | 描述改为「影响事实（**以已实证的为准**）：实际拿到什么数据/权限；未实证的影响标注推测或留空」 |
| `tool_registry.py:471-474` `risk_assessment` | 描述补「只写已证实路径上的 CIA 后果，未实证的链条不得当既成事实」 |
| `report-writer.yaml` 两处 persona | 去掉「资产价值 × 判级条款」，改为「资产价值仅用于**优先级排序**，不抬高定级」 |
| `packs/tracks/pentest/rules/rating/*.md` 价值/奖金段 | 顶部加使用注记：「本节价值分级用于**选战场**（值不值得打），**不用于抬高定级**；定级只认 §判级速查表」 |
| `tool_registry.py:1672` `rating_override` | 保留（是既有显式出口），描述补一句「申请即需人类显式批准，不得作为常规抬级手段」 |

### 4.4 P2 存储层解棘轮（有行为改动，需回归）

**A. 合并「就高」加约束**（`store.py:1809-1814`）
现状：新报 rank > 旧 → severity 取新报（rating_basis 同时覆盖）。改为：**就高须带新证据或新依据**——新报 `rating_basis` 非空**或** `has_repro_evidence(evidence, poc_artifact_id)` 为真，才接受上行；否则保留旧级并在返回值标注「就高被拒（无新证据）」。防「反复重报空口垫高」。

**B. 恢复低档出口**（`store.py:93-109`，待打磨）
现状：渗透/红队轨 info 全类别拒收。建议收窄为：**只拒 `category=vuln` 的 info**；`category=intel` 的 info 放行（信息线索本就是「够不上 low 的观察」的正当归属）。这样边界/疑似项有低档去处，不必往 low/vuln 挤。CTF 轨语义不动。
> 此项改变既有门禁语义，须同步 `tests/test_blackboard.py` 相关断言，列入待打磨清单确认。

**C. 人工下行通道**：WebUI `FindingDetailDialog` 的 severity 下拉 + `PATCH /findings` 已是人工降级出口（`FINDING_SEVERITIES` 五档），**不动**，仅在方案里明确它是「人工复核下行」的正式通道。

### 4.5 P3 报告层：让既有反夸大纪律承重

- 把 kb 快照里 `vuln-report-format.md` 的三段核心纪律（**L1~L3 判级硬序列**、**「防误报高危」逐条**、**「证实危害=回显危害」**）提炼进一个**会被注入**的位置——候选落点：`packs/tracks/pentest/rules/rating/` 下新增 `dedup-and-severity-discipline.md`（随评级口径注入），或并入 `report.yaml` 的 acceptance；
- `report.yaml` acceptance 补一条：出报告前对每条入报 vuln 发现做**证据↔等级**复验（等级高于 `impact`/`repro_steps` 实证面的降级或回补），与 4.1 同源口径。

## 5. 待打磨清单

1. **降一档的落库路径**：是门禁处直接降后调 `add_finding`，还是先按原级落库再 `patch_finding` 降？（后者留痕更全，前者少一次写）
2. **P2-B info 放行范围**：确认「只拒 vuln 类 info」是否会影响既有 info 清洗口径与 `cleanup_findings.py` 的硬规则（该脚本目前对 info 直接 delete）。
3. **P2-A 就高约束的判定键**：`has_repro_evidence` 是否足够，还是要求「新报 severity 高于旧时 rating_basis 必须非空」这一更严口径？
4. **测试面**：`tests/test_agent.py`（C6 gate 用例是否断言二元组）、`tests/test_blackboard.py`（info 门禁/合并就高断言）需同步；新增 judge 四元返回的解析用例。
5. **P3 注入落点**：新增 rating 文件 vs 并入 report.yaml acceptance vs 并入 report-writer persona——三选一，避免同一纪律三处各写一份漂移。
6. **`suggested_severity` 回执处理**：降一档后仍高于 suggested 时，是仅提示还是二次降级？

## 6. 边界与未覆盖（独立讨论，勿混入本方案）

- `vuln_class` 受控词表 + CWE 映射；
- `rating_basis` 结构化回链（可点击/可校验）；
- 跨资产同类漏洞聚合；
- SRC 报送状态流转；
- `request_authorization` 三 kind 的整体重审（本方案只加一句描述约束）。

## 7. 实施记录（全层，2026-10-09）

### 7.1 落地清单

| 层 | 改动 | 文件 |
|----|------|------|
| P0-A | `_build_vuln_gate` 双判：prompt 输出 `{compliant, severity_ok, suggested_severity, reason}`；返回 `(compliant, new_severity\|None, note)`；合规但 `severity_ok=false` → 降一档（下限 low，low 撑不起转 intel） | `core/agent/loop.py`（hook）+ `core/agent/tools.py`（应用点，`severity`/`rating_basis` 就地改写 + 回执） |
| P0-A 支撑 | 新增 `downgrade_severity(sev)`（全项目唯一定义处） | `core/blackboard/store.py` |
| P0-B | 评级硬指令追加「定级以实证危害为准…禁止就高凑档」 | `core/skills/rules.py` |
| P1 | impact/risk_assessment 描述改「以已实证的为准」；`report-writer.yaml` 两处 persona 去「资产价值 ×」；两轨 `osrc.md` 加「价值用于选战场、不用于抬级」定级纪律注记；`rating_override` 描述加「不得作为常规抬级手段」；`bb_update_finding` 描述 info 口径同步 | `tool_registry.py`、`report-writer.yaml`、`packs/tracks/{pentest,redteam}/rules/rating/osrc.md` |
| P2-A | 合并就高加约束：**渗透/红队轨**上行须带新判级依据或新复现证据，否则保留旧级；返回 `severity_raise_blocked`，工具层回填 `[就高被拒]` | `core/blackboard/store.py`、`core/agent/tools.py` |
| P2-B | info 门禁收窄为**只拒 `category=vuln`**（intel 类 info 放行）；`cleanup_findings.py` 硬规则同步 | `core/blackboard/store.py`、`scripts/cleanup_findings.py` |
| P3 | 报告阶段 acceptance 补「定级复验（证据↔等级）」；persona 同口径 | `packs/tracks/pentest/phases/report.yaml`、`report-writer.yaml` |

### 7.2 待打磨清单逐条定案

1. **降一档落库路径** → **门禁处直接降后调 `add_finding`**（少一次写；留痕靠回执 + `tool.call` 审计双份）。
2. **info 放行范围** → 只拒 vuln 类；`cleanup_findings.py` 硬规则同步为「只删 vuln 类 info」（存量 info 行无 category → 读作 vuln，仍照删，行为保持）。CTF 轨不动。
3. **P2-A 就高判定键** → `rating_basis` 非空 **OR** `has_repro_evidence`；**关键：作用域限 `track ∈ {pentest, redteam}`**——通用合并「就高」语义（`track=None` 的护栏测试 `test_finding_merge_*` / relates_to 升级 / repro_steps 并集）一字未动，避免反转既有定稿行为。
4. **测试面** → `test_blackboard.py`（info 门禁两条断言按新语义改写）、`test_agent.py`（info 拒收改用 vuln 类发现）共 3 处；全量 1264 passed。
5. **P3 注入落点** → **report.yaml acceptance + report-writer persona**（均在报告链消费面，恒被消费）；**不新增 rating 文件**——rating 文件须项目 `rule_profiles.rating` 显式列出才注入，新增会永不生效。
6. **suggested_severity 回执处理** → **仅降一档，不按 suggested 二次降级**（保守，D2）；回执含「定级校准 x→y（依据）」。

### 7.3 本次未覆盖（边界）

- **patch 路径（`bb_update_finding`）不过 C6 gate**——保留人工 F10 修订零阻断；agent 借 patch 抬级本方案未拦（合并上行已被 P2-A 覆盖，纯 patch 上行未拦）。
- **对话链（`ChatTurn`）未接线 `vuln_gate`**——既有行为，未变（对话产出的 vuln 发现不经定级校准）。
- kb 快照 `vuln-report-format.md` 的 L1~L3 判级序列原文仍在，P3 只把等价纪律提进注入面，未搬原文。
