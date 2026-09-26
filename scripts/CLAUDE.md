# scripts/

> 演练 / 冒烟 / 启动脚本。均为真机驱动（真实 Ark 模型），不做单元测试覆盖。

## 脚本清单

| 脚本 | 用途 |
|------|------|
| `serve.py` | core API 启动入口（uvicorn，127.0.0.1:8420）；**v0.64 持 uvicorn Server 句柄**并注册 `POST /api/admin/shutdown`（置 should_exit 优雅退出，shutdown 钩子给在跑会话落断点快照）——端点只在本进程注册，库/测试用法不受影响。**桌面窗口壳（desktop-app-shell M1，2026-09-21）**：`--window` 起桌面窗口（pywebview/WebView2，`--debug` 右键开 DevTools）+ 子线程 uvicorn——**owner/attach 双语义**（探测端口：未在跑=owner 拉服务，最后一窗 closed → 进程内置 should_exit 优雅停机；已在跑=attach 附窗关窗只退自己，二次双击「启动平台（窗口）.bat」=再开一窗）；加载地址探测 `--url` 显式 > webui/dist 静态版 > Vite dev 5173（::1/IPv4 双探测）> 窗内指引占位页；WebView2 缺失注册表探测 → 弹窗提示 + 回退无窗（pywebview 未装同理）；**`os.chdir(项目根)` cwd 兜底是硬要求**（config/packs/workspaces 全 cwd 相对路径）；pythonw 下 stdout/stderr 为 None → 重定向 logs/serve-window.log（workspace-hygiene D2 归口）。M2：自动探测 webui/dist 传 `create_app(static_dir=)` 同源托管。**改 serve.py 后勿忘：真机僵尸 serve.py 残留会占 8420（TaskStop 只杀跟踪壳），先 `Get-CimInstance` 找出强杀再起新版** |
| `pack_doctor.py` | packs 静态体检（`core/skills/doctor.py` 的 CLI 壳）：列 error/warning/info；有 error 退出码 1（`--strict` 连 warning 也算失败），可挂 CI/钩子。当前真包零 error |
| `demo_agent.py` | 单会话 Agent 端到端演练（**pentest 轨 × web 包**，`_generalist` 角色：列目录→写发现→finish，验证软边界/路由/审计链） |
| `demo_orchestrator.py` | 编排闭环演练（**ctf 轨 × binary 包**：tick 开窗 → 并行 Worker → 派生 → digest）；`load_role("packs","ctf",role)`，Orchestrator 构造传 `packs_root/track` |
| `demo_pentest.py` | **pentest 轨 × web 包编排闭环演练**（DESIGN.md §6.4/§6.6）：靶标 `http://127.0.0.1:8085`（本地授权靶机 Pikachu，不通即退出）→ 人类预填资产 + net=real 审批 → recon 被动侦察 → tick 派生 exploit（recon/exploit 须在 tracks/pentest/task_types.yaml 注册）→ 兜底任务（conflict_keys 互斥示例）→ digest |
| `backfill_asset_tree.py` | 一次性回填（§5.2）：旧平铺资产挂成 host → service → url 树 + best-effort 探测页面 `<title>` + 打 `meta.scanned` 标（首跑 2026-09-13 已完成 pentest-pikachu） |
| `adopt_orphan_assets.py` | **存量孤儿资产通用补挂（2026-09-20，§5.2）**：全项目（或指定 slug）扫 parent_id=NULL 的 url/service，主机部命中既有 host/domain 即 `set_asset_parent` 补挂——匹配规则与 register_asset 同一套（IP→host，域名→同名 domain，缺父不造行）；**默认 dry-run 只打印计划，--apply 落库**；写路径全走 Blackboard 方法（author=demo-script(adopt)） |
| `normalize_dirty_domains.py` | **存量脏 domain 规范化（2026-09-24，配合资产规范化修复）**：domain 行值剥 scheme/尾斜/端口尾部——裸值已存在删脏行、不存在先 `register_asset` 登记裸 domain 再删，「主机名」等占位文本直接删；delete_asset 门控（叶子+无 finding）拒绝即跳过；默认 dry-run/`--apply`，author=demo-script(normalize-domains)。**坑：fake-ip 代理环境（198.18.0.0/15）会造出假 host 行，落库后须摘挂域名并删除**（已在 assessment-20260915-7d70 执行：137 行清完） |
| `rebuild_asset_trees.py` | **存量资产树重建（asset-tree-derived-clean M1+M2，2026-09-24，DESIGN §三）**：全项目 domain 经 DoH（默认 `https://223.5.5.5/dns-query`，避开 Clash fake-ip 198.18.0.0/15 与系统代理，取 CNAME 链）重新归并——解析命中 CDN（`cdn.is_cdn`）的根行无操作；非 CDN IP 挂/改挂 host 父；**有子资产的根行显式 tested_clean → reset 回 open**（根状态改由读时派生）；默认 dry-run 只打印计划，`--apply` 落库；写路径全走 Blackboard 方法（author=`demo-script(rebuild-trees)`，发 asset.reparent） |
| `reset_cursory_clean.py` | **粗略收口 tested_clean 回退（tested-clean-intent-backing，2026-09-25）**：按会话（默认 sess-05b9dc2b6783/sess-130528d12a81，`--sessions=` 覆盖）找 new=tested_clean 事件，把**当前仍为 tested_clean** 的资产经 set_asset_status 回退到事件 old 值（open/visited）；已被后续流转的不动；默认 dry-run/`--apply`，author=`demo-script(reset-clean)`。已在 assessment-20260915-7d70 执行：109 个全部回退 |
| `cleanup_findings.py` | **存量发现清洗（两阶段，2026-09-18）**：按 edu-rating 等评级口径对既有 findings 逐条重评——复用 Agent 判级同源口径（`build_rules_preamble` 规则注入 + `core.skills.judge.judge_finding`）；硬规则（info 停收直接删）先行，LLM 四选一裁定 keep/downgrade/to_intel（口径外转线索 intel+low）/delete，脚本侧后校验（目标级 ≥low、rank 不高于当前、basis 非空）非法回退 keep。默认 dry-run 写 plan JSON，人工审阅后 `--apply --plan <file>` 落库（**不重判**，author=`demo-script(re-rate)`）；默认项目 assessment-20260915-7d70（中原工学院），仅 pentest/redteam 轨 |
| `smoke_ark.py` | Ark LLM 连通性冒烟 |
| `import_kb.py` | 知识快照**一次性导入**（幂等可重跑）：Knowledge/ 下 ctf-skills 按类拆散进各能力包 `kb/`（附 MIT LICENSE）、src-strike 拍平进 web/kb（rules 随快照，4 篇平台规则完整版覆盖 pentest 轨 owners/，覆盖前自动备份 .history）；生成每包 `kb_sources.json`（单源 root=kb）与 kb/README.md（C3 起改写前备份，文案改为"本地基线可增改"）；`--force` 重建快照但**保全本地新增 md 与 `.history/`**（先挪临时目录再还原，上游同路径文件仍由上游胜出，stats.preserved 有记录），`--no-owners` 跳过规则覆盖，src-strike 在 Knowledge/ 缺失时兜底 E:\ILOVCTRY\SRC；单文件复制失败（如杀软隔离 exp 模板）只警告不中断。**K5（2026-09-20）**：`STRIKE_KB_REMAP` 表（49 篇测试点手册 → `webapp/<测试包>/手册.md` / `recon/methodology/手册.md` 落位 + `phase/vuln_class` 分面 frontmatter，映射表唯一定义处，migrate_kb_test_packages.py 从此 import） |
| `migrate_kb_test_packages.py` | **K5 一次性迁移（2026-09-20 已执行完毕）**：49 篇 `playbooks/知识库/*-test.md` 迁为测试包 `手册.md`——三步幂等：①引用改写（route_index/route.json/kb README/技能 SKILL.md/各级 redlines/kb playbooks/core-agent CLAUDE.md，三形态：全模块路径/相对路径/裸文件名带词边界防子串碰撞，排除 .history 与 refs）；②文件移动（复用 rename_kb_move 同源移动+补分面 frontmatter，走 writing 备份体系）；③叙述性 touch-up。复跑安全（旧文件不存在即跳过） |
| `eval_ctf.py` | **K8 CTF 评测 harness 骨架（2026-09-20）**：manifest 驱动跑 NYU CTF 子集——`docs/eval/` 下 manifest JSON（tasks 必填 id/type/objective/flag；**flag 与 manifest 不进 packs/kb，防基准污染**）→ 建 `eval-` 前缀 ctf 项目（autonomy L1）→ POST 任务 → 30s 轮询至终态/超期 → judge（findings+events 全文找 flag 子串，solved/fail/timeout，报告不回 flag 原文）→ `write_report` 落 docs/nyuctf-eval.md。纯标准库 urllib；**尚未首跑**（待冒烟 manifest，见 docs/nyuctf-eval.md 待办） |
| `install_ida_mcp.py` | 把 `tools/mcp/ida-pro-mcp/` vendor 件幂等装进 IDA 插件目录（`%APPDATA%\Hex-Rays\IDA Pro\plugins`，回退 PATH/常见安装路径/`~/.idapro`），写 `ida_mcp/.vendor_version` 指纹；纯标准库无 pip；默认幂等同步、`--refresh` 清装、`--status` 只读（详见 tools/mcp/CLAUDE.md） |

## 关键约定

- 统一骨架：`sys.path.insert(0, 项目根)` + `ProjectStore` 建项目（workspaces/ 下）+ `log()` 带 flush（Windows 控制台顺序输出）+ `--resume` 复用最近项目。
- 运行前 `$env:PYTHONIOENCODING="utf-8"`，用 `E:\Miniconda3\python.exe`。
- 审批演练约定（DESIGN.md §6.4）：脚本自批必须 `decided_by="demo-script(auto)"` + 事件 `author="demo"`，**不冒充人类审批**；`--wait-approval` 走 WebUI 人工批准。
- pentest 轨演练的任务 objective **自包含** `run_cmd(runtime/host, threat_class/trusted, net/real, approval_id)` 指引——Agent 看不到脚本变量，只能看到黑板。
- 绑定模型：`store.create_project(name, track, capabilities)`；AgentSession/Orchestrator 均显式传 `track=` + `capabilities=`（旧 domain 参数已移除）。
