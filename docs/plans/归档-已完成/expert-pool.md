# 方案：专家池 + 项目绑定专家（轨退规则层、能力包隐退）

- **状态**：**M0-M4 全部已实施（2026-09-21）**（2026-09-21 收敛；同日十条待打磨全部消化见 §5；同日晚 kb 树重组翻转定稿见 §4.5 并已落地：`packs/kb/` 五域单根 + 全局 route_index + kb_sources 退役；M1 专家池：`packs/experts/` 16 专家 yaml（§4.3 映射）+ `core/skills/experts.py` 加载面（load_expert 轨变体覆写 / expert_exists / list_experts）+ doctor 专家体检段；M2 项目绑定：project.json `experts` + caps_effective/allowed_roles 推导 + 全链路切 experts + roles/ 退役（详见 §6 M2 条目）；M3 专家 CRUD + 组队 UI、M4a 场景档 / M4b 知识继承 / M4c 看板默认视图均已实施（详见 §6 M3/M4 条目），全量回归 **766 passed**；定稿决策已回写 DESIGN.md §六「专家池与项目绑定（M1-M4）」）
- **拍板记录**：见 §3（6 项决策）+ §5（10 项打磨定稿）
- **关联代码**：`packs/tracks/*/roles/`（23 个角色 yaml）、`core/skills/roles.py`（极简平铺解析 + `_generalist` 回退）、`core/skills/router.py`（pack_set = 启用 caps ∪ track）、`core/skills/rules.py`（load_kb_sources / role_rules）、`core/skills/taxonomy.py`（project_binding / LEGACY_TRACK_MAP 先例）、`core/agent/loop.py` + `core/agent/tools.py`（caps 的全部消费点）、`core/api/app.py`（发布链路 role 校验 v14 / meta 视图）、`webui/src/views/ProjectsView.tsx`（capabilities 多选 UI）
- **实施后**：定稿决策沉淀回 `DESIGN.md`（§4.5 正交结构定稿块修订），本文保留作方案背景

## 1. 愿景与背景

从「项目 = capabilities(多选) × track(单选)」改为「**专家池 + 项目绑定专家**」：只维护一个专家池（专家 = 人格 + 技能集 + 工具偏好，跨场景复用资产）；项目开场**点将组队**，不再显式多选能力包；轨只留**规则层**（redlines / owners / rating / task_types / 产出语义）。收益：专家成为一等公民（可沉淀、可分享、按项目裁剪）；知识可见范围由绑定专家自然推导，不再人工对齐「选了哪些包」。

## 2. 现状盘点（2026-09-21 核实）

- **角色 23 个**：ctf 7（分诊/四解题手/reverse/_generalist）、pentest 5、redteam 7、research 4；字段全是 `name/description/persona/skills/task_types/default_noise/tools/max_runtime/max_steps` 平铺。
- **双轨镜像实锤**（persona 尾段差异、其余字段完全相同，共 4 对）：osint（redteam 版加「范围外先停手过 ROE」）、report-writer（redteam 版加「攻击链复盘+ROE 合规节」）、external-entry（redteam 版加「ROE 四要素/出 scope 停手」）、recon（redteam 版加「标注高价值攻击入口候选」）。**ctf/recon 是例外——它叫「分诊」（skills [file-triage, triage]），与侦察语义不同，不参与合并**。
- **_generalist ×4 差异**：仅 persona/default_noise（ctf·research=passive，pentest·redteam=medium+max_steps 200）/redteam persona 带 ROE 尾段。
- **caps 消费点全景**（20+ 处，全走同一入参）：AgentSession/Toolkit 构造、`pack_set = caps ∪ track`（路由候选集）、`load_kb_sources(caps)`（kb_open 多源解析）、`kb_module_hints / kb_route_hints / top_route_entries(caps, …)`（kb 提示与路由索引裁剪）、API route-preview、projects meta、workbench 判定（caps 含 binary）、前端 Blackboard 函数库页签/bindingBadge/创建页多选。
- **kb 物理布局**：每包 `kb/` + 单源 `kb_sources.json {id:<cap>-kb, root:"kb"}`，kb_open module 带快照前缀天然消歧；doctor `prefer_cap` 解跨包目录名碰撞。`route_index.yaml` 每包一份（五包覆盖），条目 tags 与角色 skills 交集裁剪。
- **轨技能**：ctf=triage、redteam=rt-×3、research=blueprint-rebuild、**pentest 无轨技能**。

## 3. 定稿决策（用户拍板 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 轨命运 | **轨留规则层**：redlines / owners / rating / task_types / 产出语义留在轨；**角色迁出**进专家池 |
| D2 | 能力包 | **隐退为知识组织单位**：项目不再显式多选包；绑定专家的技能集推导知识可见范围（粒度定稿=能力包，见 §4.4） |
| D3 | 先行项 | **kb 物理树重组独立先行**——专家池后动也不返工（2026-09-21 晚翻转定稿，见 §4.5） |
| D4 | 场景组织 | **轨不动**；业务线级→场景档（可选后续项）、挑战级→专家路由（CTF 现状即此，类别不建档） |
| D5 | 项目边界 | **目标定边界**：同目标/样本集一项目；同样本换场景重开项目走知识继承 |
| D6 | 档物化 | **快照**：创建时五件套物化进项目配置后即弃；模板升级不影响存量项目 |

## 4. 设计详述

### 4.1 专家 yaml schema（定稿，T1）

位置 `packs/experts/<id>.yaml`（与 capabilities/tracks 平级，扁平单文件）。**字段与现角色 yaml 全兼容**，新增四个语义段：

```yaml
name: 情报收集                # 中文显示名（stem=id，与角色中文化约定一致）
description: "OSINT 情报收集：仅公开来源，不触目标基础设施"
persona: "OSINT 情报收集专家：……"
skills: [recon-asset-enum]    # 跨包技能白名单（偏好加分 + 能力面推导），null=全量（仅 _generalist）
task_types: [recon, asset-enum]   # 现语义不变（偏好加分；doctor 按可服务轨注册表并集体检）
default_noise: passive
tools: null                   # 可选工具偏好
max_runtime: null
max_steps: null
tracks: [pentest, redteam]    # 【新】可服务轨域；缺省=全轨可用（组队 UI 按轨过滤）
protected: true               # 【新】仅 _generalist（受保护不可删，沿用现约定）
variant_redteam_description: "……（ROE 版差异）"     # 【新】轨变体：字段级覆写
variant_redteam_persona: "……（ROE 版差异）"
```

- **零解析器改动**：`variant_<track>_*` 就是普通平铺键，roles.py 极简解析照用。
- 技能名全局唯一假设沿用（doctor 现有 frontmatter name 一致性检查兜底；实施时补「专家 skills 引用的技能名跨包唯一归属」检查）。

### 4.2 轨变体机制（定稿，T2：字段级覆写）

选**persona 覆写**而非轨内别名——4 对镜像实锤差异全在 persona/description 尾段（skills/task_types/default_noise 完全相同）。加载时应用覆写，产出与现角色同形状的 dict，**下游 system 组装零改动**：

```python
def load_expert(packs_root, name, track) -> dict:
    e = _parse_flat(packs_root / "experts" / f"{name}.yaml")   # 不存在回退 _generalist（沿用现回退语义）
    for key in [k for k in e if k.startswith(f"variant_{track}_")]:
        e[key.removeprefix(f"variant_{track}_")] = e.pop(key)
    return e
```

### 4.3 迁移映射表（定稿，T3：23 文件 → 16 专家）

| 专家 id | name | 来源 | tracks | 变体 |
|---|---|---|---|---|
| recon | 侦察 | pentest/recon + redteam/recon | [pentest, redteam] | variant_redteam_persona（+高价值入口候选尾段） |
| osint | 情报收集 | pentest/osint + redteam/osint | [pentest, redteam] | variant_redteam_description + persona |
| external-entry | 外网打点 | pentest + redteam 同名 | [pentest, redteam] | variant_redteam_description + persona |
| report-writer | 报告工程师 | pentest + redteam 同名 | [pentest, redteam] | variant_redteam_description + persona |
| lateral | 内网横移 | redteam/lateral | [redteam] | — |
| privesc | 权限提升 | redteam/privesc | [redteam] | — |
| triage | 分诊 | **ctf/recon**（id 变更） | [ctf] | — |
| web-solver / pwn-solver / crypto-solver / forensics-solver | 各解题手 | ctf 同名 | [ctf] | — |
| reverse | 逆向解题 | ctf/reverse | [ctf] | —（与 reverse-analyst 暂分开：解题导向 vs 研究导向；合并候选登记观察） |
| reverse-analyst | 逆向分析师 | research 同名 | [research] | — |
| code-auditor | 代码审计 | research 同名 | [research] | — |
| rebuilder | 重组工程师 | research/rebuilder | [research] | — |
| _generalist | 通用 | **4 轨合并** | 全轨 | variants×4（ctf/research passive、pentest/redteam medium+max_steps 200、redteam persona 加 ROE 尾段），protected: true |

- **triage 的 id 变更注意**：存量 role="recon" 的 ctf 引用回退 _generalist（现回退语义不变），doctor 体检核对 ctf 轨 role-rules/ 与剧本引用后清档。
- 角色退役：M2 落地时 `tracks/*/roles/` 删除（git 历史可查）；`role-rules/<role>.md` 按专家 id 匹配保留（同名迁移，文件不动）。

### 4.4 能力面推导与兼容（定稿，T4：核心机制）

**专家面 = 绑定专家 skills 并集（含变体）∪ 轨技能**；**caps_effective = 专家面技能的所属能力包集合**（推导值）。

- **爆炸半径最小化**：caps 的 20+ 消费点**全部不改**——AgentSession/Toolkit 构造入口从「meta.capabilities 直传」换成「caps_effective 推导值」，下游 `load_kb_sources / pack_set / kb_module_hints / top_route_entries` 照旧。
- **知识可见范围粒度=能力包**（比 D2 原文「kb 前缀过滤」粗一档，但包本就定义为知识组织单位，实现等价且 doctor 体检链不变）。
- **任务可达性不裁剪**（v0.63 认领零过滤不变）；专家面裁剪的是**认知边界**（技能路由候选 + kb 可见 + route_index tags 裁剪——tags 与专家面交集，语义从「角色 skills 交集」平移）。
- **_generalist 被绑定时 skills=null → 全包可见**（与现兜底行为一致）。
- **存量项目零翻译**：meta 无 experts → caps_effective = meta.capabilities **直通**，不反推专家（反推不等价，违背 LEGACY 映射「行为等价」先例）；新建项目必须绑 ≥1 专家（创建校验 422，宁严勿松）。用户可在项目设置给旧项目换将，换后走专家面。
- **API meta 响应**补 `"capabilities": caps_effective`（推导值）+ `"experts": 绑定清单`——前端 Blackboard 函数库判定 / bindingBadge **零改**。

### 4.5 M0 kb 物理树重组（定稿 2026-09-21 晚：翻转「登记表合一」；**已实施 2026-09-21**）

**决策翻转**：打磨曾把 kb 合一降档为「登记表合一」（物理树不动，顾虑 .history 备份链与指针改写量）；用户拍板 **删除全部 .history/.trash/.bak 备份包袱（git 即版本史）、恢复物理大搬家**——登记表合一作废，物理树重组定稿如下。

#### 4.5.1 新树布局

```
packs/kb/                      # 全局单根（每包 kb_sources.json 退役）
├─ README.md                   # 总索引：各域一行简介 + 许可声明 + kb_open module 形态说明
├─ licenses/
│  └─ CTF-SKILLS-LICENSE       # 5 包同名许可文件去重为一份（src-strike「内部不外发」声明同落此处）
├─ route_index.yaml            # 全局一份：条目 kb 字段加域前缀（binary/pwn/overflow-basics.md）
├─ web/  binary/  crypto/  forensics/  misc/   # 一级=能力域（与 caps 同键）
│  ├─ route.json               # kb 任务导航留域内（域自治；kbindex 按启用 caps 合并时前缀化）
│  ├─ README.md                # 各域自述（原包 kb/README.md 平移）
│  └─ …域内结构原样平移（web 阶段制 / binary 题型制 / refs/ 快照原件）
└─ cloud/ …                    # 扩展位（dsh 收编落位；code-audit/ir 预留）
```

- **一级=能力域，与 caps 同键**：专家池的 caps_effective 白名单过滤直接落在一级目录——`load_kb_sources(packs_root, capabilities)` 的 capabilities 参数即白名单入口，语义平移。
- **域内自治**：不统一各域组织风格（web 阶段制 / binary 题型制是 K5/K2 刚定的最优解）；refs 快照原件**留域内**（收编版-原件配对关系不拆散）。
- **kb_open module = 树相对路径全局唯一**：`web/webapp/authn/auth-jwt.md`、`binary/pwn/overflow-basics.md`——跨包顶层目录名碰撞自然消失。

#### 4.5.2 统一迁移规则（一条规则走天下）

`capabilities/<cap>/kb/<rel>` → `kb/<cap>/<rel>`；一切引用改写 = 「旧路径加域前缀」：

- kb 文件本体（5 包约 700 文件，含 refs/ 快照原件）；
- route_index 条目 kb 字段（web 54 + binary 9 + crypto 8 + forensics 8 + misc 7 条）合并为全局一份；
- 薄路由技能正文「特征→kb 模块」对照表指针（14 技能）；
- 各域 route.json 模块前缀（`"pwn/"` → `"binary/pwn/"`，文件本体留域内——消费方 kbindex 按启用 caps 合并、构建时知道域名可前缀化，撞键自然消解）；
- 成功案例 / 各域 README 索引 / `import_kb.py` STRIKE_KB_REMAP 落点 / `tests/test_skills.py` 路径断言。

改写复用 `refs.py rewrite_module`（全路径整串替换先例）批量跑。**历史事件流/审计文本不改**（留痕即历史）；运行时旧 module 404 → 服务端回可选清单自愈。

#### 4.5.3 消费面改动（收敛设计）

- **收敛点两个函数，消费点零改**：`rules.load_kb_sources(packs_root, capabilities)` 签名与返回形状不变，内部改构造合成源 `[KbSource(id=cap, root="kb/<cap>")]`——tools.py kb_open / kbindex / refs 的引用全部不动；`writing.resolve_kb` 拼路径基准从 `capabilities/<cap>/` 换 `packs/`。
- **直连点**：kbindex.py 读 route.json 两处路径（→ `kb/<cap>/route.json` + 模块前缀化）；API kb 端点 cap 参数语义=域（路径形态不变）；webui KbTree **零改**（cap=域同名）；doctor（kb_sources root 检查退役、prefer_cap 体检退役、route-index-kb-missing 改新树）；import_kb.py REMAP 落点。
- **退役清单**：各包 kb_sources.json、各包 route_index.yaml（并入全局）、doctor prefer_cap；`writing._KB_NON_CONTENT_FILES`（route.json）平移保留。
- **验收**：pytest 全绿 + doctor 零新增告警 + kb_open / route_lookup / KbTree 冒烟。

### 4.6 发布链路改造（M2）

- `role_exists(root, track, name)` → **`expert_exists(root, name, track)`**：池内存在 且 track ∈ 该专家 tracks 域。
- **allowed_roles = 绑定专家清单**（发布/开窗 role 值域收窄到本队，宁严勿松）；load 缺失回退 _generalist 沿用。
- `load_role` 调用点（loop system 组装）改 `load_expert`；doctor 体检扩展：专家 yaml 字段校验（skills 引用存在/跨包唯一、task_types 越各轨注册表并集、tracks 合法轨名、variant_<track>_ 的 track 合法、role-rules 悬空）。
- 编排器 spawn_session(role)/剧本 tasks[].role（pentest 方案 M3）值域同步为专家 id。

### 4.7 前端组队 UI（M3）

- 创建页：track 单选 → **专家多选**（按 tracks 过滤轨域，分组=通用/轨专属，含搜索与 persona 一行预览）→ 创建；能力包多选 UI 退役。
- 项目设置「组队」页：换将（增删专家，即时生效于下轮会话构造）；显示当前专家面推导出的能力包（只读徽章）。
- bindingBadge 改显 `track + 专家数`。

### 4.8 后置项预研（方向定稿，真做时再细化）

- **场景档（M4a）**：五件套=组队(experts 预设)/规则模板/剧本/产物清单/知识范围；专家池 schema 定稿后反推档 yaml；每轨内置 0~3 个，快照物化即弃。
- **知识继承（M4b）**：建项目可选源项目继承同 binary_sha256 的 func_kb/蓝图/样本资产；只新增不覆盖（源项目只读）；UI=创建页折叠区。
- **看板默认视图（M4c）**：档声明 vs 项目配置，随档 yaml 一并定。

## 5. 打磨定稿记录（2026-09-21 十条全部消化）

| # | 打磨点 | 定稿 |
|---|--------|------|
| 1 | 专家 yaml schema | §4.1：角色字段全兼容 + tracks/protected/variant_<track>_* 三段新增，零解析器改动 |
| 2 | 轨变体机制 | **字段级覆写**（§4.2）——4 对镜像实锤差异全在 persona/description，加载时覆写产出同形状 dict，下游零改 |
| 3 | 角色迁移映射 | §4.3：23 → 16（4 对镜像合并、_generalist 四合一、**ctf/recon 独立为 triage 专家**非合并） |
| 4 | 路由候选集 | **专家面 = 绑定专家 skills 并集 ∪ 轨技能**；caps_effective 推导后下游 caps 消费点全不改（§4.4）；_generalist 绑定=全可见 |
| 5 | 前端组队 UI | §4.7：创建页专家多选（按轨域过滤）+ 项目设置换将页 + caps 多选退役；API meta 补推导 capabilities 使前端其余零改 |
| 6 | kb 合一迁移 | **翻转定稿为物理树重组**（§4.5，2026-09-21 晚）：登记表合一作废；新树 `packs/kb/` 一级=能力域；route_index 全局一份、route.json 留域内；kb_sources.json / prefer_cap 退役；迁移=「旧路径加域前缀」一条规则 |
| 7 | 实施节奏 | §6 M0→M4：登记表合一 → 专家池 → 项目绑定 → 前端 → 后置项 |
| 8 | 知识继承 | §4.8 方向定稿（同 sha 继承/只增不覆盖/源只读），细则真做时定 |
| 9 | 档五件套 | M4a 后置，专家池 schema 定稿后反推（本方案 §4.1 即反推依据） |
| 10 | 看板默认视图 | M4c 后置，随档 yaml 一并定 |

## 6. 实施切分建议（已定稿，待用户排期）

- **M0 kb 物理树重组**：迁移脚本（文件搬家 + 指针批量改写 + route_index 并全局 + route.json 前缀化）+ 消费面切换（§4.5.3 两个收敛函数与直连点）+ 退役 kb_sources.json / prefer_cap + doctor 适配 + 验收（pytest 全绿 + doctor 零新增 + kb_open/KbTree 冒烟；先行独立可上线）。
- **M1 专家池**：experts/*.yaml 16 个落盘（§4.3 映射）+ load_expert/variant 覆写 + expert_exists + doctor 体检扩展 + 单测（**已实施 2026-09-21**：全量回归 752 passed + doctor 真实树零新增告警 + 加载面冒烟；运行时认领/发布仍走 roles/，M2 切换）。
- **M2 项目绑定**：project.json `experts` 字段 + caps_effective 推导 + 构造链/发布链路改造 + 存量直通兼容 + roles/ 退役 + API meta 补推导值。**（已实施 2026-09-21）**：
  - `core/skills/experts.py` 补推导面：`expert_skills`（绑定专家技能并集，任一 skills=null→None 两义性由 caps_effective 先判空绑定消解）/ `caps_effective`（空绑定→fallback 存量直通；全量专家→capabilities/ 全集；并集→capability 类技能所属包；绑定专家 yaml 全缺→空面宁严勿松）/ `all_capability_packs` / `allowed_roles`（绑定清单优先，未绑定=按轨全池）。
  - `core/projects.py`：Project.experts property + create_project(experts=)（非空才写键=存量零翻译）+ `update_experts` 换将（空清单剥键恢复直通态；黑板行无此列，单一真相源 project.json）。
  - `core/api/app.py`：创建/PATCH `/api/projects/{pid}/experts` 换将端点（专家存在性+轨校验 422 提示可用池）；meta 响应层装饰 `_expert_meta_view`（补 experts + 推导 capabilities）；注册式工厂 caps_effective + allowed_roles 贯穿 AgentSession→ToolDispatcher（publish_task 兜底全池）；workbench/sediment 判定改推导值；GET 两 roles 端点数据源切 `list_experts`（PackRole 兼容形状零改前端）；角色写端点（POST/PUT/DELETE）退役返 **410**（专家管理 UI 随 M3）。
  - `core/agent/loop.py` + `tools.py` + `core/orchestrator/orchestrator.py`：全部 load_role/role_exists/list_roles 调用点切 experts 口径（认领即换装 expert_exists 校验、发布 role 前置校验、饿死统计、_generalist 兜底不变）。
  - **roles/ 退役**：`packs/tracks/*/roles/` 23 个 yaml git rm；`core/skills/roles.py` 模块保留（docstring 标退役）供 `_parse_inline_value` 复用与历史脚本。
  - 测试：存量夹具迁移 experts/ 口径 + M2 新增面（推导单测 test_experts、创建 422/换将/meta 推导/工厂 caps/发布 allowed_roles test_api），全量回归 **754 passed**。
- **M3 前端**：创建页组队选择器 + 换将页 + caps 多选退役 + bindingBadge。**（已实施 2026-09-21，同批含专家 CRUD 后端）**：
  - `core/api/app.py` 专家五端点 `GET /api/experts`（全池视图）/ `GET|POST|PUT|DELETE /api/experts/{id}`——**全字段提交式覆写**（None/空=不落键，skills 缺键=全量专家语义）；写前置校验 doctor 同口径前移（id slug `[a-z0-9][a-z0-9-]{0,47}`、tracks/skills/task_types/variants 合法性、max_steps 正整数 → 422）；重名 409、protected 拒删 409、缺失 404；写走 pack_write_lock + _pack_history_backup + _trash_move 三件套；返回 `{status,id,file}` / `{status,id,trash}`。
  - 前端（webui）：创建页 **caps 多选退役→专家多选**（按轨过滤分组「通用/轨专属」、搜索、persona 一行预览、默认预选 _generalist；空选=按轨全池直通）；`bindingBadge(track, experts)` 改「轨 · 专家×N」（ProjectsView 列表 + App 顶栏）；直播间编排器态 👥组队弹层（轨过滤多选 + PATCH 换将 + 推导能力包只读徽章回执）；设置页「角色」tab 换「专家」ExpertsPane（全池管理 + HistoryButton，tracks 不选=null 全轨）；ExpertCreateDialog（slug + 显示名）。
- **M4（后置）**：场景档 / 知识继承 / 看板默认视图。**（已实施 2026-09-21，M4a/b/c 同批）**：
  - **M4a 场景档**：`core/skills/profiles.py` loader + `packs/tracks/<track>/profiles/<id>.yaml` 平铺五件套（experts/rule_profiles_owners+rule_profiles_rating/playbook/artifacts/knowledge）+ board_view；**D6 物化即弃**——建项选中档一次性物化（experts→meta.experts、rule_profiles/board_view→config 立即生效、playbook/artifacts/knowledge 随 config.profile 快照留档带 materialized_at，改档不联动存量项目）；档提供缺省、请求体显式值优先；8 个内置档（ctf full-squad/solo-generalist、pentest standard-engagement/recon-first、redteam full-chain/initial-access、research rev-workbench/code-audit）；`GET /api/tracks/{track}/profiles` 只读清单；doctor 增 profile-expert-missing（error）/profile-board-view-unknown（warning）。
  - **M4b 知识继承**：POST /projects body `inherit_from=<源项目 id>`（源预检 404 先于建项）；`core/projects.py inherit_knowledge` 三段复制**只增不覆盖**——binary 资产（find_asset 同 sha 判重，copy2 到目标 samples/ + meta.path 更新 + inherited_from 标记）、func_kb（(project,sha,address) 判重）、蓝图（(project,sha,name) 判重，非 draft 状态尽力保留）；源只读不动；失败不阻建项（响应 `inherit_error`），成功带 `inherited={binaries,func_kb,blueprints,skipped}`；UI=创建页折叠区源项目下拉。
  - **M4c 看板默认视图**：`config.board_view={"default": "findings|assets|funcs|board"}`；前端 Blackboard 受控 tabs（defaultView 不在可用集合回退 findings）。
  - 测试：场景档物化/继承/CRUD/校验全链路新增面，全量回归 **766 passed**；`npm run build` 零 TS 错误。
