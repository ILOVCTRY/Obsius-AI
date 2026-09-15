# scripts/

> 演练 / 冒烟 / 启动脚本。均为真机驱动（真实 Ark 模型），不做单元测试覆盖。

## 脚本清单

| 脚本 | 用途 |
|------|------|
| `serve.py` | core API 启动入口（uvicorn，127.0.0.1:8420；WebUI 先起这个） |
| `pack_doctor.py` | packs 静态体检（`core/skills/doctor.py` 的 CLI 壳）：列 error/warning/info；有 error 退出码 1（`--strict` 连 warning 也算失败），可挂 CI/钩子。当前真包零 error |
| `demo_agent.py` | 单会话 Agent 端到端演练（**assessment 轨 × web 包**，`_generalist` 角色：列目录→写发现→finish，验证软边界/路由/审计链） |
| `demo_orchestrator.py` | 编排闭环演练（**ctf 轨 × binary 包**：tick 开窗 → 并行 Worker → 派生 → digest）；`load_role("packs","ctf",role)`，Orchestrator 构造传 `packs_root/track` |
| `demo_pentest.py` | **assessment 轨 × web 包编排闭环演练**（DESIGN.md §6.4/§6.6）：靶标 `http://127.0.0.1:8085`（本地授权靶机 Pikachu，不通即退出）→ 人类预填资产 + net=real 审批 → recon 被动侦察 → tick 派生 exploit（recon/exploit 须在 tracks/assessment/task_types.yaml 注册）→ 兜底任务（conflict_keys 互斥示例）→ digest |
| `backfill_asset_tree.py` | 一次性回填（§5.2）：旧平铺资产挂成 host → service → url 树 + best-effort 探测页面 `<title>` + 打 `meta.scanned` 标（首跑 2026-09-13 已完成 pentest-pikachu） |
| `smoke_ark.py` | Ark LLM 连通性冒烟 |
| `import_kb.py` | 知识快照**一次性导入**（幂等可重跑）：Knowledge/ 下 ctf-skills 按类拆散进各能力包 `kb/`（附 MIT LICENSE）、src-strike 拍平进 web/kb（rules 随快照，4 篇平台规则完整版覆盖 assessment 轨 owners/，覆盖前自动备份 .history）；生成每包 `kb_sources.json`（单源 root=kb）与 kb/README.md（C3 起改写前备份，文案改为"本地基线可增改"）；`--force` 重建快照但**保全本地新增 md 与 `.history/`**（先挪临时目录再还原，上游同路径文件仍由上游胜出，stats.preserved 有记录），`--no-owners` 跳过规则覆盖，src-strike 在 Knowledge/ 缺失时兜底 E:\ILOVCTRY\SRC；单文件复制失败（如杀软隔离 exp 模板）只警告不中断 |
| `install_ida_mcp.py` | 把 `tools/mcp/ida-pro-mcp/` vendor 件幂等装进 IDA 插件目录（`%APPDATA%\Hex-Rays\IDA Pro\plugins`，回退 PATH/常见安装路径/`~/.idapro`），写 `ida_mcp/.vendor_version` 指纹；纯标准库无 pip；默认幂等同步、`--refresh` 清装、`--status` 只读（详见 tools/mcp/CLAUDE.md） |

## 关键约定

- 统一骨架：`sys.path.insert(0, 项目根)` + `ProjectStore` 建项目（workspaces/ 下）+ `log()` 带 flush（Windows 控制台顺序输出）+ `--resume` 复用最近项目。
- 运行前 `$env:PYTHONIOENCODING="utf-8"`，用 `E:\Miniconda3\python.exe`。
- 审批演练约定（DESIGN.md §6.4）：脚本自批必须 `decided_by="demo-script(auto)"` + 事件 `author="demo"`，**不冒充人类审批**；`--wait-approval` 走 WebUI 人工批准。
- assessment 演练的任务 objective **自包含** `run_cmd(runtime/host, threat_class/trusted, net/real, approval_id)` 指引——Agent 看不到脚本变量，只能看到黑板。
- 绑定模型：`store.create_project(name, track, capabilities)`；AgentSession/Orchestrator 均显式传 `track=` + `capabilities=`（旧 domain 参数已移除）。
