# packs/

> 能力包 × 场景轨正交结构（DESIGN.md §4.5、§10 为唯一真相源）。旧布局 `packs/<domain>/` 已迁空，core/测试/WebUI/demo 全部对齐新布局；旧 domain 仅保留**读取层**映射（project.json/旧 DB 行），新内容一律写 track + capabilities。

## 结构

```
packs/
├─ kb/                   # 知识库全局单根（M0）：一级=能力域 web/binary/crypto/forensics/misc/cloud
│  ├─ route_index.yaml   # 测试点路由索引全局单表：entries 条目 = point 测试点 / match 触发词 /
│  │                     #   kb 模块路径（全局形态 <域>/<快照>/<路径>）/ tags 可选技能标签；
│  │                     #   load_route_index 按域过滤视图注入；G1 起评分 Top-5
│  ├─ licenses/          # 快照上游 LICENSE 收编
│  └─ <cap>/…            # 各域 kb 树（不进 registry、不翻译、不就地改）
├─ capabilities/<cap>/   # 能力包（多选）= 方法论技能，回答"怎么干"
│  ├─ pack.yaml          # {kind: capability, name, label, description}
│  ├─ skills/<name>/     # cc 风格自包含技能：SKILL.md + references/scripts/examples/assets
│  └─ rules/*.md         # 能力级红线（glob 不递归，全量注入）
├─ data/                 # 随包基线数据：cdn_ranges.json=CDN 判定基线（增补走 config/cdn.json）
├─ experts/<id>.yaml     # 专家池（M2 起运行时唯一角色源）：扁平单文件，字段与旧角色 yaml 全兼容
│                        #   + tracks（可服务轨域，缺省=全轨）/ protected（仅 _generalist）/
│                        #   variant_<track>_*（轨变体字段级覆写）；skills 白名单可跨包引用
└─ tracks/<track>/       # 场景轨（单选）= 规则 + 任务类型，回答"什么语境、产出什么"
   ├─ track.yaml
   ├─ profiles/<id>.yaml # 场景档（M4a）：建项组队/规则/看板预设，平铺五件套
   │                     #   experts/rule_profiles_owners+rating/playbook/artifacts/knowledge
   │                     #   + board_view；建项物化即弃（D6）——此后只作预设不随项目联动
   ├─ phases/<phase>.yaml # 阶段剧本（当前仅 pentest 轨三阶段）：stem=阶段 id、name 中文显示；
   │                     #   goal/order/focus/gate 门指标/gate_types/tasks 剧本首发清单/next；
   │                     #   项目 config.phases 同名条目整体覆写；引擎 core/phases.py
   ├─ dimensions.yaml    # 测试维度清单（test-dimensions M1，当前仅 pentest 轨）：每个维度 =
   │                     #   id/name/applies_to（适用资产类型）/intent 意图模板/evidence_hint；
   │                     #   面覆盖 ⟺ 该面有 ≥1 条收尾意图且无 open 意图；项目 config.dimensions
   │                     #   同名 id 整体覆写；加载与判定 core/dimensions.py。后续演进取 skill.covers
   ├─ rules/{redlines.md, owners/<tag>.md, rating/<tag>.md, role-rules/<role>.md, templates/<tag>.md}
   │                     # F11：rating/ = 评级与价值口径（文件即规则，注入时带头注评级硬指令）；
   │                     #   owners/ 只留授权边界与操作红线；role-rules/<专家id>.md 按绑定专家匹配；
   │                     #   templates/ = 项目规则四段一体模板（rules-four-section M1，pilot
   │                     #   pentest/osrc；M2 接三级注入，现状运行时仍读 owners/rating 双文件）
   ├─ task_types.yaml    # 合法 task_type 注册表（publish 校验，拼错即拒）
   └─ skills/<name>/     # 轨级自包含流程技能（如 ctf 分诊）
```

## 包清单

- **能力包**：`web`（kb 顶级按作战阶段 `recon/{passive,active}/webapp/{authn,injection}/post-exp/playbooks/refs/notes`；**K5** 测试点手册迁为 `webapp/<测试包>/手册.md`，带 `phase/vuln_class` 分面 frontmatter）、`binary`（`pwn/reverse/malware` + 快照原件 `refs/ctf-*`；**android-kb-sourcing** `android/` 子域 4 篇手册 + cases/；iot/ics/vehicular 后续）、`crypto`、`forensics`（含 `osint/`）、`misc`（含 `ai-ml/writeup`）、**`cloud`**（dsh-kb-sourcing 新建第 6 包：pack.yaml + 薄路由技能 `cloud-entry` + `rules/redlines.md` 草案；kb 落 `packs/kb/cloud/` 四分类快照 61 篇；版权声明双落点 NOTICE.md+pack.yaml）。四包切片已合并（`-2/-3` 并入基文件 `# Part N`）、快照 SKILL.md 改名 `index.md`。**R3/R4 占位包**：`shell-c2`/`social`（**仅 redteam 轨可挂载**，建项目校验 422，无 skills/kb，内部设计未实施）。
- **场景轨**：`ctf`（弱角色/flag/writeup；+4 分类解题手 web-solver/pwn-solver/crypto-solver/forensics-solver）、`pentest`（强角色/owners 完整版/报告，原 assessment rename；旧 track 值经 LEGACY_TRACK_MAP 读兼容；+osint/report-writer；**红队向退场**——lateral/privesc 角色与配套轨技能删除，本轨 owners 明令禁内网渗透/主机提权）、`redteam`（roles 自 pentest 拷贝+persona 红队化，redlines 加 ROE 条目；+osint/report-writer 镜像）、`research`（显示名**逆向分析**；triage/reverse/analyze/verify 全 passive + reverse-analyst 角色 + 12 条 redlines，支撑 rev-generic 工作台；R4 加 blueprint/reconstruct + rebuilder 角色 + 轨技能 blueprint-rebuild；+code-auditor 代码审计）、`malware` 后置（依赖 L3+fakenet）。
- **再导入/更新快照**只跑 `scripts/import_kb.py`（幂等；`--force` 原位覆盖不整体 rmtree）；勿手改 kb/ 内快照文件。
- **专家池（M2 起运行时唯一角色源）**：17 专家按 expert-pool 方案 §4.3 迁移落盘——recon/osint/external-entry/report-writer 四对镜像合并（base=pentest 全文 + `variant_redteam_*` 差异覆写）、lateral/privesc 自 redteam 迁入、triage/web/pwn/crypto/forensics-solver/reverse 属 ctf 轨、reverse-analyst/code-auditor/rebuilder 属 research 轨、_generalist（四合一 protected，各带轨变体）、`cloud-security`（云安全，pentest 轨）。加载/推导面 `core/skills/experts.py`（load_expert 轨变体覆写 / expert_exists / list_experts 轨过滤 / expert_skills + caps_effective + allowed_roles）。**M2：`tracks/*/roles/` 23 yaml 已退役删除，角色写端点 410；M3：专家 CRUD 五端点 `/api/experts` 已落地**。
- **场景档（M4a）**：`tracks/<track>/profiles/<id>.yaml` 平铺五件套 + board_view，loader `core/skills/profiles.py`；当前 8 个内置档：ctf full-squad/solo-generalist、pentest standard-engagement/recon-first、redteam full-chain/initial-access、research rev-workbench/code-audit；建项物化即弃（D6）。
- **Skill 清单现状**：registry 收录的技能均为 cc 风格自包含目录；`SKILL.md` 是完整工作流入口，详细资料按需放在同目录 `references/`、`scripts/`、`examples/`、`assets/`。web=web-strike-entry/recon-asset-enum + K1 五专精（authn-session/injection/api-attack/client-side/post-exp）+ **web-source-audit**（源码白盒审计入口，绑 code-auditor）；binary=file-triage/binary-rev/binary-pwn/**android-rev**（file_features is_apk/has_native_lib/has_jni/godot_engine/packed_so——分诊声明须与 frontmatter 同词）；crypto=crypto-triage；forensics=forensics-triage；misc=misc-triage；cloud=cloud-entry（8 项情景 features）；ctf 轨=triage；redteam 轨=rt- 前缀三个（intranet-recon/lateral-move/privesc，对照表指 web 包 intranet-postexp；新轨技能勿与既有重名）。

## 关键约定

- **cc 风格技能迁移**：新技能必须自包含；`SKILL.md` 写完整方法、判断、红线和验收，长资料/示例/辅助脚本放同目录子目录。Agent 用 `skill_open(name=..., path=...)` 按需读取。现有引用 `packs/kb` 的技能自动以 `legacy` 兼容读取，迁移完成后不再新增薄路由或知识库依赖。
- 项目 = track(单选) × **绑定专家（`experts` 字段）**；运行时知识可见范围=caps_effective 推导（专家面∪轨技能的所属包），能力包隐退为知识组织单位；无绑定存量项目直通 meta.capabilities 零翻译；旧 project.json 的 `domain` 读取时映射；旧 track 值 `assessment` → pentest（LEGACY_TRACK_MAP，盘不改）。
- research redlines 是硬纪律：样本 untrusted、headless 只解析不执行、三层数据不混淆、反编译前先查 func_kb、发现挂 binary 资产且 evidence 带 func_id/address、五类 category、无证据标 unverified、伪码不进事件流；**R4 追加 5 条（共 12 条）**：仅授权样本/禁分发/禁绕过付费反作弊、生成代码一律 untrusted 容器执行、模块划分按业务职能+接口 spec 先钉死、`module:<名>` risk_tag 归属、自测不过不得标 tested。
- 加逆向子类（破解/外挂/病毒…）= 加前端 workbench profile 声明 + 字典，后端 API 不分叉（DESIGN.md §4.5.5）。
- 路由候选集 = 启用 caps（=推导面）技能 ∪ 轨技能，专家白名单为偏好加分（白名单外仍可命中）；评分 特征/标签×3 > task_type+10 > 角色偏好+5 > keywords×2 > 描述×1；`enabled:false` 不参与；file_features 与 platforms/formats/vuln_classes 标签同档。
- `kb/route_index.yaml`：**全局单表（M0）**，条目 kb 路径必须真实存在（doctor `route-index-kb-missing` 抓悬空）。**注入语义 G1 化**：system 不再整表注入——按会话上下文评分取 Top-5，其余条目 Agent 可用 route_lookup 工具按需查；命中技能正文改摘要+skill_open 指针。**K7 效果追踪**：skill.routed 回填 `route_points`，doctor 后处理报 `route-index-zero-hit` info。维护=人工直改或 index 提案（index 只能 edit 增补），doctor 体检兜底。
- `rules/*.md` glob 不递归 → owners/、role-rules/ 天然不自动注入，由 owner 标签与绑定专家分别选择。
- `packs/kb/` 下历史快照自带的 SKILL.md 不进 registry。`kb_search`、`kb_open`、`route_lookup` 仍保留为 legacy 兼容工具；新 Skill 必须优先使用自身目录的 `skill_open` 和附属资源，不再把全局 kb 当作默认上下文。
- **M0 合成源**：kb 源不再登记文件（kb_sources.json 退役）——`load_kb_sources` 按启用域合成 `[packs/kb/<cap>]` 恒递归，目录不存在=该域无 kb。kb_open 的 module 用**全局形态** `<域>/<快照>/<路径>`，域内相对形态兼容；多快照靠快照名前缀消歧；域按源隔离（web 项目打不开 binary 域）。
- 快照内可能缺个别文件（杀软实时隔离过 shellcode 生成器等），属已知现象；需要时从上游补回再 --force 重导。
- 技能修改：AI 只能 `propose_skill_edit` 落 .proposals/，人类审阅后应用（永不直写）。

## 坑与注意

- 样本执行必须 docker/sandbox（各轨红线）；宿主 docker 未启动时 Agent 应改道静态分析并标 unverified。
- 专家 skills 白名单可跨能力包引用，但名字必须存在（`scripts/pack_doctor.py` / `GET /api/packs/doctor` 查悬空引用，零 error 才健康）；redteam 的 lateral/privesc 专家绑 rt- 轨技能（白名单=加分偏好，不裁剪可达性；pentest 侧同名角色及配套轨技能已退场）。
- 删除不物理抹除：技能经 API 删除进同级 `.history/trash/`（可恢复；**角色 CRUD 已随 M2 退役 410**，专家由 packs/experts/ 单文件池管理）；编辑备份两种命名（`<ts>[.n]_<file>` 与 `<file>.<ts>.bak`），历史端点两种都认；回滚前会自动再备份当前版。
- 五个能力包的 `rules/redlines.md` 已补齐（AI 起草**草案待人审**，见各文件头注）；内容修订直接改文件（.history 自动备份）。
