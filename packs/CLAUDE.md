# packs/

> 能力包 × 场景轨正交结构（DESIGN.md §4.5、§10 为唯一真相源）。
> **重构状态（2026-09-13）**：正交结构重构已落地——磁盘旧布局 `packs/<domain>/`（ctf/pentest）已迁空，core/测试/WebUI/demo 全部对齐新布局；旧 domain 仅保留**读取层**映射（project.json/旧 DB 行），新内容一律写 track + capabilities。

## 结构

```
packs/
├─ capabilities/<cap>/   # 能力包（多选）= 方法论技能 + 知识库，回答"怎么干"
│  ├─ pack.yaml          # {kind: capability, name, label, description}
│  ├─ skills/<name>/     # 中文薄路由技能（SKILL.md，frontmatter 标签路由）
│  ├─ kb/                # 快照知识库区：不进 registry、不翻译、不就地改
│  ├─ kb_sources.json    # 源登记 [{id, root(包内相对), recursive}]，kb_open 据此解析
│  └─ rules/*.md         # 能力级红线（glob 不递归，全量注入）
└─ tracks/<track>/       # 场景轨（单选）= 规则 + 角色 + 任务类型，回答"什么语境、产出什么"
   ├─ track.yaml
   ├─ roles/*.yaml       # _generalist 兜底（受保护）+ 角色：persona/description/skills/task_types/
   │                     #   default_noise/tools/max_runtime/max_steps（软边界，越界走审批）
   ├─ rules/{redlines.md, owners/<tag>.md, role-rules/<role>.md}
   ├─ task_types.yaml    # 合法 task_type 注册表（publish 校验，拼错即拒）
   └─ skills/<name>/     # 轨级流程技能（如 ctf 分诊）
```

## 包清单（v0.2，快照已于 2026-09-13 导入）

- 能力包：`web`（kb：ctf-web + src-strike）、`binary`（kb：ctf-pwn/reverse/malware；iot/ics/vehicular 为后续 kb 子模块）、`crypto`（ctf-crypto）、`forensics`（ctf-forensics + ctf-osint）、`misc`（ctf-misc/ctf-ai-ml/**ctf-writeup**）
- 场景轨：`ctf`（弱角色/flag/writeup）、`assessment`（强角色/owners 完整版/报告）、`research`（**2026-09-14 落地**：triage/reverse/analyze/verify 全 passive + reverse-analyst 角色 + 7 条 redlines，支撑 rev-generic 工作台）；`malware` 仍后置（依赖 L3+fakenet，未来复用 rev-generic profile）
- 再导入/更新快照只跑 `scripts/import_kb.py`（幂等，--force 重建）；勿手改 kb/ 内快照文件。
- 中文薄路由层现状（registry 收录，正文只做"特征→kb 快照模块路径"对照）：
  web=web-strike-entry/recon-asset-enum；binary=file-triage/binary-rev/binary-pwn；
  crypto=crypto-triage；forensics=forensics-triage；ctf 轨=triage（题目分诊，跨包路由）；
  misc 暂无薄路由（直接 kb_open ctf-misc/ctf-ai-ml/ctf-writeup）。

## 关键约定

- 项目 = capabilities(多选) × track(单选)；旧 project.json 的 `domain` 读取时映射（pentest→assessment+[web]，ctf→ctf+[binary]，reverse→research+[binary]）。
- research redlines 是硬纪律：样本 untrusted、headless 只解析不执行、三层数据不混淆、反编译前先查 func_kb、发现挂 binary 资产且 evidence 带 func_id/address、五类 category、无证据标 unverified、伪码不进事件流。
- 加逆向子类（破解/外挂/病毒…）= 加前端 workbench profile 声明 + 字典，后端 API 不分叉（DESIGN.md §4.5.5）。
- 路由候选集 = 启用 caps 技能 ∪ 轨技能，角色白名单再窄化；评分 特征/标签×3 > keywords×2 > 描述×1；`enabled:false` 不参与；file_features 与 platforms/formats/vuln_classes 标签同档。
- `rules/*.md` glob 不递归 → owners/、role-rules/ 天然不自动注入，由 owner 标签与绑定角色分别选择。
- kb/ 下 ctf-skills 类快照自带 SKILL.md 也不进注册表，只能经 kb_open 打开。
- 每包 `kb_sources.json` 为**单源** `{id:<cap>-kb, root:"kb"}`：kb_open 的 module 必须带快照名前缀（如 `ctf-web/auth-jwt.md`、`src-strike/知识库/idor-test.md`），多快照靠前缀天然消歧；源按包隔离，forensics 项目打不开 src-strike。
- 快照内可能缺个别文件（杀软实时隔离过 shellcode 生成器等），属已知现象；需要时从上游补回再 --force 重导。
- 技能修改：AI 只能 `propose_skill_edit` 落 .proposals/，人类审阅后应用（永不直写）。

## 坑与注意

- 样本执行必须 docker/sandbox（各轨红线）；宿主 docker 未启动时 Agent 应改道静态分析并标 unverified。
- 角色 skills 白名单可跨能力包引用，但名字必须存在（`scripts/pack_doctor.py` / `GET /api/packs/doctor` 查悬空引用，零 error 才健康）。
- 删除不物理抹除：角色/技能经 API 删除进同级 `.history/trash/`（带 UTC 时间戳，可恢复）；编辑备份两种命名（`<ts>[.n]_<file>` 与 `<file>.<ts>.bak`），历史端点两种都认；回滚前会自动再备份当前版。
- 五个能力包的 `rules/redlines.md` 已补齐（2026-09-16，E2：AI 起草**草案待人审**，见各文件头注）；内容修订直接改文件（.history 自动备份）。
