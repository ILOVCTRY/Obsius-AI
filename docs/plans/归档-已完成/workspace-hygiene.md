# 工作区卫生治理：顶层目录契约与遗物收编（workspace-hygiene）

> `workspaces/` 与项目根 `artifacts/` 两个路径的「乱」定性与治理：给顶层目录立契约、把错位物归位、把遗物收编进回收站、给膨胀项装感知。

- **状态**：**已实施**（2026-09-23 用户批准后 M1+M2 同批落地；定稿决策回写 DESIGN.md §一「目录结构」与「顶层目录契约与卫生体检」）
- **拍板记录**：见 §3 决策表（D1-D7，均为路径归属常规决策，无未知性大的项）
- **关联代码**：`core/blackboard/campaign.py`（DEFAULT_DB_PATH + `_migrate_legacy_db`）、`core/api/app.py`（campaign 挂载 + `/api/workspace-hygiene`）、`scripts/serve.py`（logs/serve-window.log）、`core/projects.py`（ProjectStore/list_projects/回收站）、`.gitignore`

## 0 实施注记（2026-09-23，与原方案的偏差与补充）

1. **迁移触发口径收窄实现细节**：原方案写「`__init__` 惰性迁移」——实际实现为模块级 `_migrate_legacy_db(new_path)`，仅 `Path(path) == DEFAULT_DB_PATH` 时触发（显式传路径的一律不迁移，测试 fixture 天然豁免）。**关键修偏**：app 工厂原是显式传 `Path(workspace_root)/"campaign.db"`（不是无参调用），不修的话迁移永不触发——现改为传 `Path(workspace_root).parent / "data" / "campaign.db"`（workspace_root 同级 data/），真实环境（workspace_root="workspaces"）与测试（tmp workspace）双 hermetic，且路径恰等于 DEFAULT_DB_PATH 触发迁移。
2. **迁移顺序加固**：主库 rename 先行——主库失败=整体放弃留老位置；主库成功后 -wal/-shm 尽力而为，**失败绝不回退老路径**（老位置已无主库，回退会 sqlite 新建空库丢数据；-wal 缺失最多丢未 checkpoint 尾巴，宁可少不可空）。
3. **.gitignore 实际补 3 行**：`data/`、`logs/`、`artifacts/`——原方案第 4 行 `serve-window.log` 冗余（`.gitignore` 已有全局 `*.log`），未加。
4. **磁盘归档额外项**：项目根 `serve-window.log`（4MB，活跃使用中）一并 mv 进 `logs/`（当时 8420 未在跑无句柄锁）；`_full_run.log` 按既有约定留用户自删未动。
5. **测试面**：test_campaign.py 增 4 例（迁移成功数据保留 / 无老库直建 / rename 抛错降级老路径 / 显式路径跳过迁移）；test_api.py 增 2 例（hygiene 三段口径+零写副作用 / error 档阈值），全量 967 passed。
6. **体检立竿见影揪出盘点误判**：§1.1 原标「正规新式项目」的 proj-* × 4 实为建项残留空壳（无 project.json 无 blackboard.db，内容=空 browser-profile + 空 artifacts）——hygiene 端点首跑即报 stray，核实后 mv 进 .trash 收编（48 条含 artifacts-legacy+re1_probe×5+proj-*×4），workspaces 根自此零 stray。感知层价值当场验证。
7. **前端展示后置**（M2 原文）：`GET /api/workspace-hygiene` 端点已可用，设置页/项目列表告警条展示留待后续。
6. **前端展示后置**（M2 原文）：`GET /api/workspace-hygiene` 端点已可用，设置页/项目列表告警条展示留待后续。

## 1 现状盘点（逐项定性，2026-09-23 实测）

### 1.1 `workspaces/` 根（应只有：项目目录 + .trash + CLAUDE.md）

| 条目 | 实测 | 定性 |
|------|------|------|
| `campaign.db(-shm/-wal)` | 4096B+32K+52K，活跃更新 | **归属错位**——全局基础设施库写死在 workspaces 根（campaign.py:64 默认 `workspaces/campaign.db`），它不是项目 |
| `serve-stderr.log / serve-stdout.log` | 2026-09-17 | **服务日志遗物**——散落根目录 |
| `.serve-c.err / .serve-c.log` | 2026-09-14，隐藏文件形态 | 同上，更早一代 |
| `re1_probe_1 ~ re1_probe_5` | **0 字节空目录** ×5，2026-09-15 | **空壳遗物**——某次探测脚本残留 |
| `ctf` | **177MB**（其中 `.tmp/` 176M！） | 正规活跃项目，但 **.tmp 临时目录膨胀 176MB**——网关 TEMP 重定向只进不出，`scratch/clear` 从未对它用过；点开头目录 `du <dir>/*` 通配看不到，肉眼也容易漏 |
| `assessment-20260915-7d70` | 39MB，含 2 个 cleanup-plan JSON | 正规老项目（有 project.json），合法保留 |
| `proj-*` × 4 | 各 ~1.8MB | 正规新式项目，合法保留 |
| `.trash/` | **38 条 / 12MB** | 回收站只进不出，无大小感知（`list_trashed` API 有、UI 无感知） |

### 1.2 项目根 `artifacts/`（整目录是遗物）

| 条目 | 实测 | 定性 |
|------|------|------|
| `poc_*.ps1` ×3、`gen_openapi_audit.py`、`routes_inventory.tsv`、`_e1_auth_scan.txt`、`d2-walk/` | 2026-09-13~16 | **早期调试遗物且被 git 误跟踪**——`git ls-files` 显示全部在版本库里（.gitignore 的 `workspaces/` 挡不住已跟踪文件）；现网代码无任何路径指向项目根 artifacts/（产物全是 per-project `workspaces/<slug>/artifacts/`），纯历史残留 |

### 1.3 相邻确认（不扩大战线，仅记录）

- 项目根还有 `serve-window.log`（serve.py 写死 `_ROOT/serve-window.log`，活跃使用中）与 `_full_run.log`（会话临时文件，既有约定留用户自删）——日志类应在 `logs/`。
- `.gitignore` 缺 `data/`、`logs/`、`artifacts/`、`serve-window.log`。

## 2 根因

1. **顶层无契约**：workspaces/ 的 CLAUDE.md 写了约定，但代码不设防——campaign.py 把全局库、旧版 serve 把日志、探测脚本把空目录都塞进了根。
2. **全局数据无归口**：campaign.db 是全局运行时数据，却寄生在「项目容器」目录里。
3. **临时目录只进不出**：`.tmp`（TEMP 重定向）与 scratch 不同，从未纳入任何清理入口的默认心智（`scratch/clear` 虽覆盖 .tmp 但无人知道要用）。
4. **git 卫生缺位**：早期调试产物被 add 进库，.gitignore 事后补救对已跟踪文件无效。
5. **膨胀无感知**：176MB 的 .tmp 藏在点开头目录里，无任何体检报告它。

## 3 设计定稿

### 3.1 决策表

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | campaign.db 归位 | 默认路径改 `data/campaign.db`（**新建顶层 `data/`**，全局运行时数据归口，.gitignore 整目录忽略）；`CampaignMemory.__init__` 惰性迁移：老路径 `workspaces/campaign.db` 存在且新路径不存在 → 三件套 rename 迁移（**mv 非删除**），占用/失败降级留老位置 + log 警告不炸启动 |
| D2 | 服务日志归口 | 新建顶层 `logs/`；serve.py `serve-window.log` → `logs/serve-window.log`；workspaces/ 根四份历史日志（serve-stdout/stderr.log、.serve-c.log/.err）一次性 mv 归档进 `logs/archive/` |
| D3 | 项目根 artifacts/ 处置 | 磁盘整目录 `mv` 进 `workspaces/.trash/artifacts-legacy-<ts>/`（回收站惯例，可手动移回）；**git 解跟踪**：`git rm -r --cached artifacts`（只动索引不删磁盘文件）+ `.gitignore` 补 `artifacts/`——git 步骤属状态变更，实施时与用户确认后一并做或随下次授权 commit 落地 |
| D4 | 空壳目录处置 | `re1_probe_1~5` mv 进 `workspaces/.trash/`（0 字节，零风险） |
| D5 | .tmp 膨胀治理 | 不做自动删除（宁严勿松，现场哲学）；`POST /projects/{pid}/scratch/clear` 文案与文档明确「覆盖 scratch + .tmp」**已具备**——补 doctor/hygiene 感知（D6）引导使用；自动老化（>N 天清理）后置不做承诺 |
| D6 | 膨胀与陌生条目感知 | 新轻端点 `GET /api/workspace-hygiene`：①workspaces 根陌生条目（无 project.json 的目录、散文件，白名单排除 .trash/CLAUDE.md）②各项目 `.tmp/scratch/spill/browser-profile` 超 100MB 报条目+大小 ③`.trash` 条目数与总大小。只报告不处置，处置入口指向已有能力（scratch/clear、手动移回） |
| D7 | .gitignore 补齐 | 加 `data/`、`logs/`、`artifacts/`、`serve-window.log`（防遗物再入库） |

### 3.2 治理后目标形态

```
项目根/
  data/campaign.db            # 全局战役记忆（D1 迁入，含老位置惰性迁移）
  logs/serve-window.log       # 服务日志（D2 迁入）
  logs/archive/               # 历史散落日志归档
  workspaces/                 # ★ 契约：只允许 项目目录 + .trash/ + CLAUDE.md
    <slug>/...                #   正规项目
    .trash/                   #   回收站（38 条 → +artifacts-legacy +re1_probe_*，大小可见）
  artifacts/                  # 消失（遗物进 .trash；目录名由 .gitignore 兜底防复活）
```

### 3.3 关键机制

**campaign.db 惰性迁移**：迁移逻辑在 `CampaignMemory.__init__`（create_app 构造期、worker 线程未起，无并发窗口）；三件套逐个 `Path.rename`，任一失败（占用/跨盘）即整体放弃并 log 警告——库继续用老路径工作，下次启动重试。测试：老路径在场 → 迁移 + 新路径读写一致；老路径缺席 → 直建新路径零副作用；老路径被占用（mock rename 抛错）→ 留老位置不炸。

**hygiene 端点口径**：陌生条目判定 = `workspaces/` 下一级条目，目录无 `project.json` 且非 `.trash`、或任意散文件 → `stray`（warning）；项目膨胀 = 四个候选目录（.tmp/scratch/spill/browser-profile）`du` 超 `100MB` → `bloat`（warning，超 500MB 升 error）；trash = 条目数+总大小 → `info`。纯只读扫描（os.scandir + 按需 du），一次调用 < 1s 量级（不做全树递归，膨胀目录按一级子项聚合）。

**契约防复发**：D1/D2 迁移后 workspaces 根不再有任何合法非项目写入者；D6 体检兜底感知新杂物的出现。ProjectStore 建项目 slug 判重已有，不另设白名单硬闸（避免把合法工具脚本建项目的路径堵死——demo 脚本走 ProjectStore 建的是正规项目）。

## 4 实施切分

### M1 迁移与收编（一次落地）

1. `campaign.py` 默认路径 + 惰性迁移 + 测试。
2. `serve.py` 日志路径改 logs/。
3. 磁盘归档操作（mv）：项目根 artifacts/ → .trash/artifacts-legacy-<ts>/；re1_probe_1~5 → .trash/；workspaces 根四份历史日志 → logs/archive/。
4. `.gitignore` 补 4 行（D7）；git 解跟踪步骤（D3 后半）按拍板执行。
5. `workspaces/CLAUDE.md` 与项目根文档同步（新契约）；相关目录 CLAUDE.md（core/blackboard、scripts）同步 campaign/logs 路径变更。
6. 全量回归（campaign 路径变化触及 test_campaign；serve 日志触及无断言）。

### M2 感知层（排期后置）

1. `GET /api/workspace-hygiene` 端点 + 测试。
2. 前端展示（设置页或项目列表顶部告警条）——后置观察，端点先行即可用。

## 5 风险与对策

| 风险 | 对策 |
|------|------|
| campaign.db 迁移时文件被占用（旧进程残留 shm/wal） | rename 失败整体放弃留老位置 + log 警告，下次启动重试；shutdown-zombie 场景天然自愈（僵尸死后迁移成功） |
| git rm --cached 误删磁盘文件 | `--cached` 只动索引，磁盘文件原样；且整目录已先 mv 进 .trash 有备份 |
| mv 归档后某脚本仍写老路径 | 全库 grep 老路径引用逐一核对（campaign.py/serve.py 是仅有的两个写死处，已核实）；hygiene 体检兜底 |
| 用户误把 .trash 当可删垃圾（含刚归档的遗物） | 归档命名带 `-legacy-<ts>` 语义；CLAUDE.md 写明恢复方式（手动移回） |
| logs/、data/ 新顶层目录与用户自有目录（Cyber-pro/、Knowledge/）观感混淆 | 文档目录结构图写明；.gitignore 兜底，不进版本库 |

## 6 明确不做

- 自动删除/自动老化任何产物（.tmp 自动清理后置，仅报告 + 已有 scratch/clear 入口）。
- .trash 自动清空（回收站只进不出维持，感知交给 D6）。
- 存量项目内部布局治理（项目内 scratch/artifacts 归口是 task-workspace 方案的域，本方案不越界）。
- assessment-20260915-7d70 等正规老项目的任何处置（合法保留，去留由用户）。
- Cyber-pro/、Knowledge/、开源优秀项目/ 等用户自有目录（不在本方案域内）。
