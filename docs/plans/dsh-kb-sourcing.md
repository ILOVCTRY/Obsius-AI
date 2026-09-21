# 方案：能力包内容补源——dsh 知识库收编（cloud / miniapp / 指纹包）

- **状态**：讨论收敛，待打磨（2026-09-21）
- **拍板记录**：见 §3（5 项决策已确认）
- **关联代码**：`packs/capabilities/web/`（K5 作战阶段组织 + 薄路由范式先例）、`packs/capabilities/binary/`（K2「收编版 + refs/ 快照原件」先例）、`packs/tracks/pentest/roles/`、`scripts/pack_doctor.py`（route-index-kb-missing 体检）
- **素材库**：`开源优秀项目/渗透测试/dsh-redteam-model-main`（MIT © 2026 SeaOf0；10 模式 × refs 知识库，中文原生）
- **与 [pentest-phased-workflow.md](pentest-phased-workflow.md) 咬合**：本方案即其 **M3「专家与内容」的内容专题**——D5 拍板「配套能力包另开打磨专题」的落点，先行不阻塞 M1/M2 机制实施。
- **与 [expert-pool.md](expert-pool.md) 咬合（2026-09-21 冲突排查后对齐；**M0 已实施 2026-09-21**）**：①kb 落位按其 **M0 物理树重组**新形态（`packs/kb/cloud/`——注意 M0 一级=既有五能力域，cloud 域新建时机随本方案实施，kb_sources.json 已退役不再创建）；②`cloud-security` 载体按其实施状态定（roles/ 未退役→轨角色 yaml；退役后→专家池 `experts/cloud-security.yaml`），见 D5。

## 1. 愿景与背景

pentest 方案 D5 留下两个能力包缺口（云安全、小程序）与指纹三段式的「本地指纹包」空白。用户探明 dsh 库后指定收编补源：MIT 许可无法律障碍；中文原生与我方文档语言一致；vendors/knowledge 类篇目近乎零平台术语、可直接快照；唯一需要改写的只有 playbook 类技能（混 dsh 平台机制）。

## 2. 素材盘点（2026-09-21 核实）

### 2.1 十模式 × 我方落点总览

| dsh 模式 | 规模 | 对应落点 | 本方案处置 |
|---|---|---|---|
| cloud-security | 78 文件 / 605KB md | **新建 cloud 包**（D5 缺口一） | **全量收编** |
| pentest 的 miniprogram/ | 5 篇（另 mobile 5 篇归 android-kb-sourcing） | miniapp（D5 缺口二） | **种子收编** |
| asset-mapping | fpdb_seed.json + s0-s7 管线 | pentest 定稿 #6「本地指纹包」 | **首批收编** |
| pentest 其余（web 38 + zh 25 等） | 109 篇 / 2.3MB | web 包查漏 | 只登记不收编 |
| binary-analysis（16 分类）/ ctf-solver（11 分类）/ code-audit（9 分类）/ incident-response / attack-defense / av-evasion | 各 0.6-4.8MB | binary/ctf 查漏；code-audit、IR 我方无对应包 | 只登记不收编（Android 专项补源另立 [android-kb-sourcing.md](android-kb-sourcing.md)，其 mobile 5 篇归并该方案） |

### 2.2 cloud-security 细拆（收编主体）

- **refs/knowledge/ 5 篇**：六厂商 metadata 端点速查（附录 A，含「阿里云 100.100.100.200 非 169.254」级实战细节）、云 API 只读探测纪律（附录 B）、IAM 策略语言速查、工具卡、ATT&CK Cloud 矩阵——无平台术语，即用。
- **refs/vendors/ 36 篇**：六厂商（AWS/Azure/GCP/**阿里/腾讯/华为**）× 各 6 篇（计算/网络/对象存储/IAM/RDS/SSRF 专项），每篇 7-13KB，结构统一（攻击面→只读探测命令→配置缺陷利用→提权持久化→检测要点）。国内厂商覆盖正对 SRC。
- **refs/native/ 16 篇**：CI-CD / 容器 / K8s / Serverless 四场景各 3-5 篇。
- **refs/detection/ 4 篇**：蓝队视角（检测规则设计等），辅助定位。
- **skills/cloud-playbook/SKILL.md 32.7KB**：七门门禁 C1-C7 + 攻击路径主线（身份→权限→资源→影响）+ 战果扩大战法（凭证循环/信任链横向/提级序）——底子好但**混 dsh 平台机制，唯一改写件**。
- `agent.cordis.yml`（46KB）：纯 dsh 平台机制（preset/realm/registry），**不收编**。

### 2.3 asset-mapping 细拆

- `fpdb_seed.json`：种子指纹库，规则制（`loc: body/title/header` + `kws` AND 语义 + `path_rules` 探测确认词），注释标明「签名均经红队实战项目验证命中；全量库 5600+ 规则 / 4200+ 产品用 `scripts/fpdb_update.py` 重建」。含 RuoYi 等国内组件指纹。
- `scripts/s0_expand.py … s7_excel.py`：七段资产测绘管线（展开→scope→引擎→子域→存活→去重→指纹→报表），Python 实现，可对照我方 asset-enum 任务形态取用。

## 3. 定稿决策（用户确认 2026-09-21）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 收编范围 | **cloud 全量 + miniapp 种子 + 指纹包首批**；其余模式只在本方案登记为可选补源清单，不收编 |
| D2 | cloud 包落位 | 新建 `packs/capabilities/cloud/`（pack.yaml / skills / rules）；kb 落 **expert-pool M0 新树** `packs/kb/cloud/`，保留 dsh 四分类原样快照（knowledge/native/vendors/detection）——M0 未实施前暂落 `capabilities/cloud/kb/` 随 M0 统一迁移（「旧路径加域前缀」一条规则覆盖） |
| D3 | 技能形态 | 薄路由改编照 web-strike-entry 范式——cloud-playbook 剥离平台机制、只留知识战法与路由表 |
| D4 | 指纹包 | fpdb_seed.json 落 tools/ 体系 `data` 类首批成员（衔接 [toolchain-registry.md](toolchain-registry.md)） |
| D5 | 角色绑定 | `cloud-security` 绑 pentest 轨——**载体按 expert-pool 实施状态定**：roles/ 未退役 → `packs/tracks/pentest/roles/cloud-security.yaml`（并补进 expert-pool §4.3 迁移映射表）；roles/ 已退役 → 专家池 `packs/experts/cloud-security.yaml`（tracks:[pentest]），**不再新建轨角色**（miniapp 角色等种子扩充后再挂，同理） |

## 4. 设计详述

### 4.1 cloud 包目录映射表（核心交付，按 expert-pool M0 新树形态）

```
packs/capabilities/cloud/          # 活性内容（技能=注册表、提案制）
├─ pack.yaml                       # {kind: capability, name: cloud, label: 云安全,
│                                  #  description: "云平台与云原生攻防：六厂商服务 + K8s/容器/Serverless/CI-CD（收编自 dsh-redteam-model，MIT）"}
├─ skills/cloud-entry/SKILL.md     # ← cloud-playbook 改编薄路由（唯一改写件，见 §4.2）
└─ rules/redlines.md               # 云上红线（AI 起草草案待人审，见待打磨 #5）

packs/kb/cloud/                    # 快照区：原样搬运、不翻译、不就地改（M0 树重组定稿位；
├─ README.md                       #   M0 未实施前暂落 capabilities/cloud/kb/，随迁移脚本并入）
│  ├─ （← refs/README.md，自带 61 篇索引表）
├─ knowledge/                      # ← refs/knowledge/   5 篇
├─ native/                         # ← refs/native/     16 篇（cicd/container/k8s/serverless）
├─ vendors/                        # ← refs/vendors/    36 篇（aliyun/aws/azure/gcp/huawei/tencent × 6）
├─ detection/                      # ← refs/detection/   4 篇（蓝队辅助视角）
└─ route.json                      # kb 任务导航留域内（expert-pool §4.5.1 定稿），前缀化 cloud/
```

- **不再创建 kb_sources.json**（expert-pool M0 退役项）；kb_open module=树相对路径 `cloud/knowledge/…`、`cloud/vendors/aliyun/oss.md`。
- route_index 条目进**全局** `packs/kb/route_index.yaml`（草案见 §4.3，kb 路径加 `cloud/` 域前缀）；M0 未实施前落包内 `capabilities/cloud/route_index.yaml` 随 M0 并入。

### 4.2 cloud-entry SKILL.md 改编要点

**保留（纯知识战法）**：攻击路径主线（身份→权限→资源→影响四要素闭环 + 可到达性证明）；高价值目标对照表（KMS=域控级 / IdP=堡垒机级 / 组织根=域控 2.0 / 「能造账号的权限」）；六源凭证入口清单（git 历史 / CI secrets / 元数据 / 前端硬编码指纹 AKIA·LTAI / 客户端配置 / 桶内备份）；提级序（身份面>控制面>密钥面>数据面）；凭证循环与信任链横向（跨账号→跨服务→跨云同构错误复用）。

**剥离 / 改写（dsh 平台机制）**：

| dsh 概念 | 处置 |
|---|---|
| 七门门禁 C1-C7（gates_list / route-boost 信封） | 删——我方自主档审批 + 任务验收承担；只读 API 优先、破坏性先确认改写为正文纪律条 |
| operation-state 台账 / 覆盖度算术对账 | 删——我方 task acceptance + 判据承担 |
| `creds-cloud.txt` / `attack-paths.csv` 工件 | 改为黑板 artifact 登记（findings/资产照常走 bb_add_finding） |
| 环境还原登记 environment-restore.md | 改写为「持久化动作逐项上报待人工确认」（对齐我方审批语义） |

**薄路由正文重建**：特征→kb 模块对照表（拿到 AK/SK → `knowledge/cloud-api-readonly-probing.md` + `vendors/<厂商>/iam.md`；元数据 SSRF → `knowledge/metadata-service-endpoints.md`；K8s 集群暴露 → `native/k8s/01-…`；桶公开 → `vendors/<厂商>/oss.md` 类）+ 反空转规则（凭据失效才换入口面、同构错误跨云复测优先）。frontmatter：keywords（云/oss/桶/iam/ak/sk/k8s/容器/ssrf/元数据/提权…）、task_types: [asset-enum, exploit]。

### 4.3 route_index 首批条目草案（kb 路径为 cloud 域内相对路径；进全局表时加 `cloud/` 前缀，实施时逐一核实存在）

| point | match | kb |
|---|---|---|
| 元数据 SSRF | ssrf, 元数据, metadata, imds, 169.254 | knowledge/metadata-service-endpoints.md |
| AK/SK 泄露利用 | accesskey, ak/sk, 凭证泄露, lta, akia | knowledge/cloud-api-readonly-probing.md |
| IAM 权限提权 | iam, ram, cam, 提权, assume-role | knowledge/iam-policy-language-cheatsheet.md |
| 对象存储配置缺陷 | oss, s3, cos, obs, 桶, bucket, 公开读 | vendors/aliyun/oss.md（厂商篇六选一，路由条目单路径——主厂商先行，其余靠 SKILL 路由表） |
| 云控制台接管 | 控制台, console, 接管 | vendors/aliyun/ssrf-console.md |
| K8s 集群攻防 | k8s, kubernetes, rbac, pod, 集群 | native/k8s/01-cluster-exposure-mapping.md |
| 容器逃逸 | 容器, docker逃逸, container, escape | native/container/01-container-escape-paths.md |
| CI/CD 攻击面 | cicd, 流水线, pipeline, jenkins, github action | native/cicd/01-pipeline-attack-surface.md |
| Serverless | serverless, 函数计算, lambda, faas | native/serverless/01-function-permission-trigger-abuse.md |

无 tags 全角色可见（首批不做角色裁剪）；doctor `route-index-kb-missing` 兜底。

### 4.4 miniapp 种子落位

pentest 模式 `refs/miniprogram/` 5 篇并入 web 包 `kb/miniprogram/` 子域（kb_sources 单源不受影响，前缀天然消歧），miniapp 独立包等内容攒够再拆（YAGNI）。**mobile 5 篇改归 [android-kb-sourcing.md](android-kb-sourcing.md) 承接**（2026-09-21 打磨定稿：Android/移动端知识集中 binary 包 android 子域，渗透向篇目 refs/ 快照纪律照旧）。D5 的 miniapp 角色 yaml 同步后置。

### 4.5 指纹包落位

- `fpdb_seed.json` → `tools/registry.json` `data` 类首批成员（bin 指向 `tools/data/fpdb/fpdb_seed.json`，guide 注明来源与 `fpdb_update.py` 全量重建路径）——衔接 toolchain-registry M1 的 schema；其 M1 未落地前可先物理落 `tools/data/fpdb/` 临时位。
- 消费侧：asset-enum / 逐站评级任务在侦察阶段经网关读指纹库（具体注入形态——工具卡 vs kb——待打磨 #4）。
- s0-s7 管线脚本**暂不收编**（我方 asset-enum 任务形态已自洽，管线可作对照参考留素材库原位）。

### 4.6 收编纪律（对照 K2/K5 先例）

- 快照区纪律：不翻译、不就地改、发现问题走「上游补回再重导」；收编区头注标来源库与 MIT 许可（版权声明落点待打磨 #7）。
- 红线对照：dsh 文内自带「只读 API 优先 / 破坏性操作人工确认 / 速率与账单意识 / 禁对非授权桶匿名枚举 / 元数据探测限授权实例」——提炼为 cloud 包 `rules/redlines.md` 草案（AI 起草待人审，E2 惯例），与 pentest 轨 owners 边界不冲突（owners 管授权范围，包红线管云上操作纪律）。
- doctor 体检：route_index kb 路径真实存在、skills 白名单名字存在、pack.yaml 字段齐全。

## 5. 待打磨清单

1. cloud-entry SKILL.md 改编全文稿（§4.2 是要点非成文）——剥离清单逐条落实后成文。
2. route_index 首批条目定稿：kb 路径逐一核实、match 词表补全、对象存储多厂商篇的路由策略（单路径 vs 建 vendors 索引页）。
3. cloud 包 `rules/redlines.md` 草案成文（从 dsh 各篇纪律段提炼 + 对照 pentest 轨 owners）。
4. 指纹库消费形态：网关工具（指纹查询命令）vs kb 文档挂载 vs asset-enum 任务 prompt 注入位置；**fpdb_seed.json 与 wappalyzer-data（toolchain-registry §4.1 data 类首批、pentest 指纹三段式第二段）的关系**——并存两个 data 条目 vs 合并为单指纹库（fpdb=规则制可探测、wappalyzer=特征数据集，形态不同，倾向并存+消费链分工），随 toolchain-registry 待打磨 #2 一并定。
5. detection/ 4 篇蓝队内容是否在 kb README 标注「辅助视角」防攻防混淆。
6. miniapp 后续扩充来源（dsh pentest/mobile 之外的素材渠道）与独立包触发条件。
7. MIT 版权声明落点（pack.yaml description 内注 vs kb/NOTICE.md）。
8. 其余模式补源登记表（binary-analysis 16 分类 / code-audit 9 分类 / incident-response / attack-defense / av-evasion——各对应我方哪个包或新包，留档不实施）。

## 6. 实施切分建议（打磨定稿后由用户排期）

- **M1 cloud 包收编**：kb 快照搬运 + cloud-entry 改编 + route_index + pack.yaml/kb_sources + doctor 全绿。
- **M2 角色与种子**：pentest 轨 `cloud-security` 角色 yaml 绑定 + web 包 kb/miniprogram/ 子域落位。
- **M3 指纹包**：fpdb_seed.json 落位 + registry data 类声明 + asset-enum 消费衔接（可与 toolchain-registry M1 合并实施）。
