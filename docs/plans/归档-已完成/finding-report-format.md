# 方案：漏洞收录格式与报告渲染模板（危害描述 / 复现步骤 / 修复建议）

- **状态**：**已实施（M1+M2，2026-09-22；M3 报告链随 pentest-phased-workflow M4 合并实施）**（2026-09-21 收敛，一轮商议定稿；定稿决策已回写 DESIGN.md「三、黑板系统→漏洞收录格式」）
- **实施修正**：方案原文写 schema v18→v19，v19 已被执行轨迹链路（2026-09-22）占用，**实际 v19→v20**（findings 补 impact/remediation 两列）；实施中顺带修复 patch_finding 旧门禁漏查 `evidence.pocs` 列表的不一致（统一走 `has_repro_evidence`）
- **拍板记录**：§3（4 项决策用户拍板）
- **关联代码**：`core/blackboard/schema.py`（findings 表 DDL）、`core/blackboard/store.py`（`add_finding` 门禁 :34 / `merge_finding_evidence` :91 / `EVIDENCE_LIST_UNION_KEYS` :78）、`core/agent/tools.py`（`bb_add_finding` :161 / `bb_update_finding` :212 工具描述）、`webui/src/components/blackboard/FindingDetailDialog.tsx`（`pocsOf`/`reproText` 渲染 :46/:83）、`packs/tracks/pentest/roles/report-writer.yaml`（报告消费）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（「三、黑板系统」发现门禁段旁新增收录格式条目），同步 `core/blackboard/CLAUDE.md`；本文保留作方案背景

## 1. 愿景与背景

黑板上每条 vuln 发现自带「**危害描述 + 编号复现步骤 + 修复建议**」三件套，直接渲染为渗透报告「逐发现详情」节——报告工程师零脑补（只汇总黑板事实），复现者逐步可自证（每步带预期返回结果）。

源自两条线的汇合：

- **DesRedTeam 工具对比**（2026-09-21）：对方漏洞卡 = 判级 + 编号复现步骤（curl 命令清单）+ CWE 映射；我方 pocs[] 是证据堆叠、无步骤叙事；
- **报告消费链断点分析**：影响/修复建议无字段无约定（报告工程师 persona「绝不脑补」但黑板没落点）、复现步骤非一等公民（链式利用只能堆 POC 或塞自由文本）、报告消费口无契约。

**用户定稿模板（渲染规格，实施对齐此格式）**：

```markdown
# {漏洞名称}

## 危害描述
（影响事实：实际拿到什么 / 影响多大）

## 复现步骤
1. 步骤描述：
   ```http
   仅 http 报文
   ```
   预期返回结果：……

2. 步骤描述：
   ```python
   仅 python 脚本
   ```
   预期返回结果：……

（步骤类型三选：http 报文 / python 脚本 / bash 命令；可嵌图片证据）

## 修复建议
（可落地）
```

## 2. 现状盘点（2026-09-21）

- findings 表 v18：`vuln_class` 自由文本一词两义（pentest=漏洞类型 / CTF=线索类别）、`rating_basis` 一句话、`evidence` 自由 JSON；
- `evidence.pocs[]` = `{name?, type: http_raw|python|steps, http_raw?, artifact_id?, target?, stability?}`——**证据为纲**（多条 POC 独立堆叠），无步骤叙事、无预期结果；前端 `reproText` 现拼复现文稿，单报文 POC 可用、链式利用散；
- `requests` / `screenshots` 是**并集死键**（`EVIDENCE_LIST_UNION_KEYS` 认识、工具描述不教写、前端零渲染）——截图型证据无处可挂；
- verified 门禁只查「evidence.poc/pocs 或 poc_artifact_id 存在」，不问复现质量；「影响证明」无承载字段；
- 报告工程师（report-writer.yaml）persona 约定「逐发现详情：复现步骤/证据/影响/修复建议」，但消费契约只是 persona 文本。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 收录格式 | 报告模板三节定稿：危害描述 / 复现步骤（编号 + 代码块 + 预期返回结果）/ 修复建议 |
| 2 | 存储形态 | **A 结构化存储 + 渲染成模板**——数据层存结构，Markdown 只是渲染视图，渲染器三处共用（详情弹窗 / 一键复制文稿 / 报告逐发现详情）；否掉「直接存 Markdown 大字段」（格式漂移靠提示词拉、复制与校验都要解析文本） |
| 3 | 步骤类型 | **http / python / cmd 三类并存**（cmd 渲染 ```bash 围栏），枚举留扩展 |
| 4 | 图片证据 | 步骤可引用图片产物（artifact_id），渲染时嵌入 |

## 4. 设计详述

### 4.1 数据结构

**findings 新列**（schema v18→v19，`ALTER TABLE` 检缺列加，幂等）：

| 列 | 类型 | 语义 |
|---|---|---|
| `impact` | TEXT DEFAULT '' | 危害描述：影响事实（拿到什么数据/权限、影响面），pentest/redteam 轨 vuln 类报告必填，其他轨/类可空 |
| `remediation` | TEXT DEFAULT '' | 修复建议：可落地 |

**evidence.repro_steps[]**（新约定键，成为复现叙事的一等结构；旧 `pocs[]` 转 legacy）：

```json
{
  "desc": "步骤描述（必填，如「用 x 登录低权账号」）",
  "type": "http | python | cmd | image",
  "code": "代码块内容：HTTP 原始报文 / python 脚本 / bash 命令（image 步骤无）",
  "expected": "预期返回结果（必填，复现自证锚点；image 步骤可空）",
  "artifact_id": "可选：脚本产物 id 或图片产物 id（渲染时嵌入/附带）",
  "stability": "可选，如 \"3/3\"（连续 3 次全部触发）",
  "target": "可选"
}
```

- `type` 枚举：`http`（原始报文，```http 围栏）/ `python`（```python）/ `cmd`（命令行，```bash）/ `image`（无代码块，`artifact_id` 必填指向图片产物，渲染嵌入）。旧值 `http_raw` 读时映射 `http`、`steps` 视为纯文字步骤；
- 多步利用链 = 数组顺序即步骤顺序，渲染自动编号；
- **并集合并**：`repro_steps` 加入 `EVIDENCE_LIST_UNION_KEYS`（按内容指纹去重追加）——重复上报多路径并存、不重复堆步；人工修订走 patch evidence 浅层合并（列表键整键替换）改步骤；
- **旧数据兼容（读时降级）**：`pocsOf` 兼容函数加一层——`pocs[]` 项映射为单步骤 `{desc: name ?? '', type(映射), code: http_raw, expected: '', stability, target, artifact_id}`；前端/门禁统一走「步骤视图」，旧 verified 发现在新门禁下按兼容路径继续通过，不做一次性数据迁移。

### 4.2 门禁联动（pentest/redteam 轨）

- verified 门禁新口径（vuln 类）：**`repro_steps` 非空且至少一步 `code` 非空（或 `artifact_id` 非空）且该步 `expected` 非空**；或旧结构 `evidence.poc/pocs/poc_artifact_id`（兼容存量）。报错文案引导「按复现步骤登记（desc+code+expected）」；
- `impact`/`remediation` **不进门禁**——写入放行，报告前由 report 阶段剧本校验三件套齐备（见 4.4），避免登记门槛过高压制 unverified→verified 的流转；
- `info` 全类别拒收、`rating_basis` 就高覆盖等现行门禁不动。

### 4.3 渲染器（模板规格，三处共用）

```
# {title}  [{severity}/{status}]
## 危害描述
{impact}
## 复现步骤
1. {desc}：
   ```http|python|bash
   {code}
   ```
   预期返回结果：{expected}
   （image 步骤：嵌入图片产物）
2. ……
## 修复建议
{remediation}
```

- **前端详情弹窗**：结构化原生组件渲染（非整段 Markdown）——步骤列表、代码块带 CopyButton（单块复制）、预期结果独立样式、图片内嵌；「一键复制复现文稿」= 上述模板全文（升级现有 `reproText`）；
- **报告「逐发现详情」节** = 对每条 vuln 类（verified 优先）发现套此渲染——报告工程师 persona 指向渲染器输出，不自行发明结构；
- 渲染规格即本节代码块，实施以前端组件 + 报告输出双实现对齐。

### 4.4 报告消费链（衔接 pentest-phased-workflow M4）

- `report-writer.yaml` persona 微调：逐发现详情直接按渲染器三节模板组织，影响与修复建议取 `impact`/`remediation` 字段，不现编；
- report 阶段剧本（pentest 方案 M4 排期时一并实施）：出报告前校验每条入报 vuln 发现三件套齐备（`impact`/`remediation` 非空 + `repro_steps` 合规），缺项出回补清单——「宁严勿松」在报告侧的延伸，不卡黑板写入。

## 5. 实施记录（M1+M2 已实施，2026-09-22）

- **M1 数据层（已落地）**：schema **v20**（原文写 v19，被轨迹链占用顺延；findings 补 `impact`/`remediation` 两列）+ `add_finding`/`patch_finding` 支持 `impact`/`remediation`（None=不动/空串=清空，合并旧值非空保留、空缺补入）+ `validate_repro_steps`（全轨写入口）+ `has_repro_evidence`（verified 门禁判定，add/patch 共用，顺带修 patch 漏查 pocs 的旧不一致）+ `repro_steps` 进并集键 + `bb_add_finding`/`bb_update_finding` 工具描述与入参改写。测试：`test_verified_gate_repro_steps` / `test_repro_steps_validation` / `test_repro_steps_union_merge_and_impact_remediation`（全量 794 passed）。
- **M2 前端（已落地）**：FindingDetailDialog 三段渲染（危害描述/复现步骤/修复建议，空节「（待补充）」）+ `reproStepsOf` 统一步骤视图（repro_steps 优先、旧 pocs 映射追加）+ image 步嵌图 / 其余 artifact 步 ScriptBlock / 文稿含围栏代码块与预期结果 + 编辑表单 impact/remediation textarea + 「复制复现文稿」；`hasPoc` 扩为任意复现证据。`npm run build` 零 TS 错误。
- **M3 报告链（后置）**：report-writer persona 微调 + report 阶段三件套校验（随 pentest 方案 M4 排期合并实施）。

## 6. 边界与未覆盖（独立讨论，勿混入本方案）

- `vuln_class` 受控词表 + CWE 映射（缺点③）——词表设计独立讨论；
- 判级依据结构化回链（rating_basis 不可点击/不可校验，缺点④）；
- 跨资产同类漏洞聚合（单资产挂点 vs 全站同类洞，缺点⑦）；
- SRC 报送状态流转（提交/收录/定级反馈，缺点⑨）；
- intel 类发现格式：复现步骤结构对 intel 同样适用（线索核实过程），`impact`/`remediation` 留空——不强制。
