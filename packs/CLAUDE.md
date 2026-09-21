# packs/

> 能力包 × 场景轨正交结构（DESIGN.md §4.5、§10 为唯一真相源）。
> **重构状态（2026-09-13）**：正交结构重构已落地——磁盘旧布局 `packs/<domain>/`（ctf/pentest）已迁空，core/测试/WebUI/demo 全部对齐新布局；旧 domain 仅保留**读取层**映射（project.json/旧 DB 行），新内容一律写 track + capabilities。

## 结构

```
packs/
├─ kb/                   # 知识库全局单根（M0，2026-09-21）：一级=能力域 web/binary/crypto/forensics/misc
│  ├─ route_index.yaml   # 测试点路由索引全局单表（v0.65，§4 知识库四层）：entries 条目 = point 测试点 /
│  │                     #   match 触发词 / kb 模块路径（全局形态 <域>/<快照>/<路径>）/ tags 可选技能标签
│  │                     #   （与角色 skills 交集裁剪）；load_route_index 按域过滤视图注入；G1 起评分 Top-5
│  ├─ licenses/          # 快照上游 LICENSE 收编（CTF-SKILLS-LICENSE）
│  └─ <cap>/…            # 各域 kb 树（原 capabilities/<cap>/kb/ 原样搬迁）：不进 registry、不翻译、不就地改
├─ capabilities/<cap>/   # 能力包（多选）= 方法论技能，回答"怎么干"
│  ├─ pack.yaml          # {kind: capability, name, label, description}
│  ├─ skills/<name>/     # 中文薄路由技能（SKILL.md，frontmatter 标签路由）
│  └─ rules/*.md         # 能力级红线（glob 不递归，全量注入）
└─ tracks/<track>/       # 场景轨（单选）= 规则 + 角色 + 任务类型，回答"什么语境、产出什么"
   ├─ track.yaml
   ├─ roles/*.yaml       # _generalist 兜底（受保护）+ 角色：name（中文显示名，可≠文件 stem——
   │                     #   stem 才是角色 id）/description/persona/skills/task_types/
   │                     #   default_noise/tools/max_runtime/max_steps（软边界，越界走审批；
   │                     #   v0.63 起窗口认领零过滤——task_types/default_noise 仅随任务换装生效）
   ├─ rules/{redlines.md, owners/<tag>.md, rating/<tag>.md, role-rules/<role>.md}
   │                     # F11（2026-09-17）：rating/ = 评级与价值口径（判级条款+资产/目标价值分级，
   │                     #   文件即规则，注入时带头注评级硬指令）；osrc 价值节 §0/§1/§3/§4 与判级节
   │                     #   §5/§7/§8 已迁 rating/osrc.md，edu-rating 全文迁 rating/，owners/ 只留授权边界与操作红线
   ├─ task_types.yaml    # 合法 task_type 注册表（publish 校验，拼错即拒）
   └─ skills/<name>/     # 轨级流程技能（如 ctf 分诊）
```

## 包清单（v0.2，快照已于 2026-09-13 导入）

- 能力包：`web`（kb 已重做 2026-09-19 F15：顶级按作战阶段 `recon/{passive,active}/webapp/{authn,injection}/post-exp/playbooks/refs/notes`，打法与资料分居，`kb/route.json` 任务导航；**K5 测试包分类 2026-09-20**：49 篇测试点手册迁为 `webapp/<测试包>/手册.md` / `recon/methodology/手册.md`（README/打穿短表留守 `playbooks/知识库/`），手册带 `phase/vuln_class` 分面 frontmatter，payloads/ 弹药待 K6 沉淀自然生成；详见 DESIGN.md §4 定稿块）、`binary`（kb 2026-09-20 K2 重排：收编版 `pwn/reverse/malware` + 快照原件 `refs/ctf-*`；iot/ics/vehicular 为后续 kb 子模块）、`crypto`（收编版 `crypto/` + `refs/ctf-crypto`）、`forensics`（收编版 `forensics/` + `osint/` + `refs/ctf-*`）、`misc`（收编版 `misc/ai-ml/writeup` + `refs/ctf-*`）；四包切片已合并（`-2/-3` 编号文件并入基文件 `# Part N`）、快照 SKILL.md 改名 `index.md`；详见 DESIGN.md §4 定稿块与 §17 K2；**R3/R4 占位包**：`shell-c2`/`social`（红队扩展架构位 §6.9.1，**仅 redteam 轨可挂载**——建项目校验 422，无 skills/kb，内部设计〔未实施〕）
- 场景轨（**2026-09-17 R1 拆分已落地**）：`ctf`（弱角色/flag/writeup；**J 组 2026-09-20** +4 分类解题手 web-solver/pwn-solver/crypto-solver/forensics-solver，对标 CAI/EnIGMA 按类分工）、`pentest`（**渗透测试**：强角色/owners 完整版/报告，原 assessment 轨 rename 而来；旧 track 值经 LEGACY_TRACK_MAP 读兼容；**J 组 2026-09-20** +osint/report-writer + task_type=report:passive；**2026-09-21 红队向退场**：lateral/privesc 角色、lateral-move/intranet-recon/privesc-win-lin 轨技能、task_types privesc/lateral-movement/credential-access 删除——本轨 owners 规则明令禁内网渗透/主机提权，内容 redteam 轨全有镜像）、`redteam`（**红队行动**：roles 自 pentest 拷贝+persona 红队化，redlines 加 ROE 条目；**J 组 2026-09-20** +osint〔ROE 化〕/report-writer〔攻击链复盘+ROE 合规〕镜像）、`research`（显示名**逆向分析**，2026-09-20 由「研究」改；**2026-09-14 落地**：triage/reverse/analyze/verify 全 passive + reverse-analyst 角色 + 7 条 redlines，支撑 rev-generic 工作台；**R4 扩充 2026-09-20**：task_types 加 `blueprint`/`reconstruct`（全 passive）+ `rebuilder` 角色 + 轨技能 `blueprint-rebuild` + redlines 增至 12 条；**J 组** +code-auditor 代码审计）；`malware` 仍后置（依赖 L3+fakenet，未来复用 rev-generic profile）；J 组扩充依据=开源高频角色对标（DESIGN §17 J 组，后置：c2-operator/DFIR/云安全）
- 再导入/更新快照只跑 `scripts/import_kb.py`（幂等；strike 落点按 F15 重映射 playbooks/refs/notes，**知识库/ 手册按 `STRIKE_KB_REMAP` 迁 K5 测试包落位并补分面 frontmatter**，--force 原位覆盖不整体 rmtree）；勿手改 kb/ 内快照文件。
- 中文薄路由层现状（K1 后 14 个，registry 收录，正文只做"特征→kb 模块路径"对照表）：
  web=web-strike-entry/recon-asset-enum + K1 五专精（web-authn-session/web-injection/web-api-attack/web-client-side/web-post-exp）；
  binary=file-triage/binary-rev/binary-pwn；crypto=crypto-triage；forensics=forensics-triage；
  misc=misc-triage（K1）；ctf 轨=triage；
  redteam 轨=rt- 前缀三个（intranet-recon/lateral-move/privesc 的红队镜像，对照表指 web 包 intranet-postexp；pentest 侧对应三技能 2026-09-21 退场，新轨技能勿与既有重名）。

## 关键约定

- 项目 = capabilities(多选) × track(单选)；旧 project.json 的 `domain` 读取时映射（R1 后 pentest→pentest+[web]，ctf→ctf+[binary]，reverse→research+[binary]）；旧 track 值 `assessment` → pentest（LEGACY_TRACK_MAP，盘不改）。
- research redlines 是硬纪律：样本 untrusted、headless 只解析不执行、三层数据不混淆、反编译前先查 func_kb、发现挂 binary 资产且 evidence 带 func_id/address、五类 category、无证据标 unverified、伪码不进事件流；**R4 追加 5 条（2026-09-20，共 12 条）**：仅授权样本/禁分发/禁绕过付费反作弊、生成代码一律 untrusted 容器执行（V1 不跑原样本）、模块划分按业务职能+接口 spec 先钉死（spec 错了写 notes 上报勿自行改）、`module:<名>` risk_tag 归属、自测不过不得标 tested。
- 加逆向子类（破解/外挂/病毒…）= 加前端 workbench profile 声明 + 字典，后端 API 不分叉（DESIGN.md §4.5.5）。
- 路由候选集 = 启用 caps 技能 ∪ 轨技能，角色白名单为偏好加分（v0.65 降级，白名单外仍可命中）；评分 特征/标签×3 > task_type+10 > 角色偏好+5 > keywords×2 > 描述×1；`enabled:false` 不参与；file_features 与 platforms/formats/vuln_classes 标签同档。
- `kb/route_index.yaml`（v0.65）：**全局单表（M0）**，条目 kb 路径全局形态（`web/webapp/file-upload/手册.md`）；load_route_index 按启用域过滤视图（web 54 / binary 9 / crypto·forensics·misc 骨架 8/8/7 条，web 条目 tags 挂技能白名单）。条目 kb 路径必须真实存在（doctor `route-index-kb-missing` 抓悬空）。**注入语义 G1 化（2026-09-19）**：system 不再整表注入——按会话上下文评分取 Top-5（条目多少不影响提示词开销），其余条目 Agent 可用 route_lookup 工具按需查；命中技能正文也改摘要+skill_open 指针，不整段进 system。**K7 效果追踪（2026-09-20）**：skill.routed 事件回填 `route_points`，`GET /api/packs/doctor` 后处理报 `route-index-zero-hit` info（长期注入过却从未被使用的条目，供精简）。维护=人工直改或 index 提案（index 只能 edit 增补——M0 全局单表永在），doctor 体检兜底。
- `rules/*.md` glob 不递归 → owners/、role-rules/ 天然不自动注入，由 owner 标签与绑定角色分别选择。
- kb/ 下 ctf-skills 类快照自带 SKILL.md 也不进注册表，只能经 kb_open 打开。
- **M0 合成源（2026-09-21）**：kb 源不再登记文件（kb_sources.json 退役）——`load_kb_sources` 按启用域合成 `[packs/kb/<cap>]` 恒递归，目录不存在=该域无 kb。kb_open 的 module 用**全局形态** `<域>/<快照>/<路径>`（如 `web/webapp/authn/auth-jwt.md`），域内相对形态（`webapp/authn/auth-jwt.md`）兼容；多快照靠快照名前缀消歧；域按源隔离，web 项目打不开 binary 域。
- 快照内可能缺个别文件（杀软实时隔离过 shellcode 生成器等），属已知现象；需要时从上游补回再 --force 重导。
- 技能修改：AI 只能 `propose_skill_edit` 落 .proposals/，人类审阅后应用（永不直写）。

## 坑与注意

- 样本执行必须 docker/sandbox（各轨红线）；宿主 docker 未启动时 Agent 应改道静态分析并标 unverified。
- 角色 skills 白名单可跨能力包引用，但名字必须存在（`scripts/pack_doctor.py` / `GET /api/packs/doctor` 查悬空引用，零 error 才健康）；redteam 的 lateral/privesc 角色绑 rt- 轨技能（白名单=加分偏好，不裁剪可达性；pentest 侧同名角色及配套轨技能 2026-09-21 退场，红队向内容只在 redteam 轨）。
- 删除不物理抹除：角色/技能经 API 删除进同级 `.history/trash/`（带 UTC 时间戳，可恢复）；编辑备份两种命名（`<ts>[.n]_<file>` 与 `<file>.<ts>.bak`），历史端点两种都认；回滚前会自动再备份当前版。
- 五个能力包的 `rules/redlines.md` 已补齐（2026-09-16，E2：AI 起草**草案待人审**，见各文件头注）；内容修订直接改文件（.history 自动备份）。
