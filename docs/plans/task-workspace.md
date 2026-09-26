# 任务工作目录：一任务一现场（task-workspace）

> 当新开一个任务时，它的所有产物应该怎么安排——命令中间文件、正式产物、工具溢出全部归口到任务专属工作目录，随任务可追溯、可接手、可清理。

- **状态**：讨论收敛（2026-09-23，含完整设计；待用户过目后排期实施）
- **拍板记录**：见 §3 决策表（D1-D10，均为工程常规决策，无未知性大的项）
- **关联代码**：`core/runtime/gateway.py`（cwd 强制 scratch）、`core/agent/tools.py`（bb_add_artifact / read_file / spill）、`core/agent/loop.py`（STRICT_PROMPT_TAIL、任务现场路径族）、`core/blackboard/store.py`（artifacts 表 W3）、`core/api/app.py`（artifacts/content、scratch/clear）

## 1 背景与痛点

任务（task）是执行的本体（v0.72 一窗一任务），但任务产生的**文件产物**至今没有任务级归口：

1. **scratch 大杂烩**：网关把 host/wsl 命令 cwd 强制到 `<ws>/scratch/`（临时中间文件统一去处），多任务并行时所有任务的中间文件混堆一个目录——互相覆盖踩踏、分不清哪个任务产的、任务结束后无法按任务归档或清理。
2. **归属靠事后自觉**：正式产物靠 Agent 主动 `bb_add_artifact`（挂 meta.attribution `{session_id, task_id?}`），但命令直接落盘的文件（`nmap -oN out.txt`、`sqlmap --dump` 的 dump 目录、下载的 exp）没有登记就散在 scratch 里，finding 的 evidence 引不到，执行轨迹（traces）里有 command 事件却没有产物链接。
3. **产物目录按 kind 平铺**：`artifacts/poc/`、`artifacts/browser-shots/`、`artifacts/attachments/`……任务视角看不到「这个任务的全部产物」，跨任务同 kind 混在一起，聚合只能靠 meta JSON 过滤（`list_artifacts(task_id=)` 有能力但物理目录无分区）。
4. **跨会话接手现场断链（C10 缺口）**：任务 fail→reopen 换窗续跑时，transcript（对话现场）经 `snapshots/task-<tid>.json` 完整恢复，但任务跑出来的**文件现场**（扫描原始结果、dump、下载物）散在 scratch 大杂烩里，接手窗根本找不到——接手只接到了对话，没接到文件。
5. **清理风险**：`scratch/clear` 端点清空整个 scratch，但「可随时清理」的前提是没人知道哪些文件属于还在跑的任务——无归属导致清理要么不敢清（越积越多）要么误删在跑现场。

## 2 现状盘点（产物落点全景）

| 产物类别 | 现落点 | 归属 | 登记 |
|---------|--------|------|------|
| 命令临时中间文件 | `<ws>/scratch/`（cwd 强制，平铺） | ❌ 无归属 | 无 |
| 正式产物（bb_add_artifact） | `<ws>/artifacts/<kind>/<file>` | meta.attribution | artifacts 表+事件 |
| 浏览器截图 | `<ws>/artifacts/browser-shots/` | 同上 | ✓ |
| 浏览器下载 | `<ws>/artifacts/browser-downloads/` | ❌（池层无任务上下文） | 无 |
| 抓包 | http_history 表 | source/batch_id/session_id | ✓（黑板） |
| 工具结果溢出 spill | `<ws>/spill/<ts>-<tool>-<uuid>.txt` | ❌ 无归属 | 仅事件回填定位器 |
| salvage 挽救 | `<ws>/artifacts/salvage/` | artifacts 表 | ✓ |
| 任务现场/快照/续聊 | `<ws>/snapshots/{sid,task-<tid>,chat-<sid>}.json` | 任务键/会话键（确定性路径） | sessions.meta 指针 |
| 任务附件 | `<ws>/artifacts/attachments/` | artifacts 表 | ✓ |
| 反编译缓存/库 | `<ws>/artifacts/decompiler-cache|db、.ghidra-tmp` | 项目级系统产物 | 无需 |
| 系统 TEMP | `<ws>/.tmp/` | 系统级 | 无 |
| 样本输入 | `<ws>/samples/` | untrusted 只读语义 | assets 表 |

**另有一个现存形态不统一**：黑板 artifacts.path 两套前缀并存——attachments 落库带 `artifacts/` 前缀（`artifacts/attachments/<sha12>_<name>`），bb_add_artifact 不带（`<kind>/<file>`）；而 `GET /artifacts/content` 的 join 根是项目根（`base=proj.path`），两形态必有一类解析不到。本方案顺带修齐。

## 3 设计定稿

### 3.1 核心决策表

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 任务目录位置 | `<ws>/artifacts/tasks/<task_id>/`（**不放顶层 `tasks/`**）——artifacts/ 语义放宽为「Agent 产物与任务现场总库」，所有产物消费端（content/download/附件/防穿越）根一致，rel 形态统一 |
| D2 | cwd 规则 | 认领任务（current_task_id 非空）→ cwd=任务目录；无任务（对话轮/空闲手动窗）→ cwd=`scratch/` 不变。gateway `run()` 加 `task_dir` 参数，workspace（写边界）**维持项目根一期不收窄** |
| D3 | pathguard 锚 | `workspace_escapes` 的 scratch 锚=当前 cwd（任务目录优先）——相对路径 `..` 上穿即拒（语义不变，锚移动）；绝对路径写工作区内仍放行（只拦写不拦读不变） |
| D4 | bb_add_artifact 分区 | 任务内 → `artifacts/tasks/<tid>/<kind>/<file>`；无任务 → `artifacts/<kind>/<file>`（原样）。**path 统一带 `artifacts/` 前缀**（项目根相对，与 attachments 既有形态对齐） |
| D5 | spill 跟任务 | 任务内 → `artifacts/tasks/<tid>/spill/`；无任务 → `artifacts/spill/`（从 `<ws>/spill/` 迁入 artifacts，存量不迁移只影响新落点）；回填定位器=实际落点的 cwd 相对路径 |
| D6 | read_file 锚 | 相对路径两段 fallback：先按 cwd 锚（任务目录/scratch）→ 不存在再按项目根锚（读附件 `artifacts/attachments/...`、spill 定位器、samples 均走第二段）；错误文案提示两种形态 |
| D7 | 生命周期 | 认领时惰性 mkdir（确定性推导 `artifacts/tasks/<tid>/`，无需 DB 指针，镜像 snapshots/task-<tid>.json 哲学）；**终态保留不自动清理**；`scratch/clear` 语义不变（只清 scratch+.tmp，任务目录不动）——scratch 从此只承载无任务临时文件，清理安全性自然提升 |
| D8 | 系统产物不任务化 | snapshots/、browser-profile/、.tmp/、decompiler-cache|db、salvage、attachments、browser-downloads 维持项目级 |
| D9 | prompt 同步 | STRICT_PROMPT_TAIL 第 7/9 条「本项目 scratch」改动态文案（任务内=任务工作目录）；纪律尾在 dynamic 块，按任务动态化不伤 prompt caching |
| D10 | 前端与接手 | M2：任务窗产物面板（`GET artifacts?task_id=` 已有能力 + 新增任务目录文件树端点，把「写了没登记」的文件也露出）；C10 接手注入「任务工作目录已有 N 文件」行 |

### 3.2 目录布局（定稿后）

```
workspaces/<slug>/
  project.json / blackboard.db
  samples/                     # untrusted 只读输入（不变）
  scratch/                     # 仅无任务命令的 cwd（对话轮/空闲手动窗），可随时清
  .tmp/                        # 系统 TEMP 重定向（不变，项目级）
  snapshots/                   # 会话快照/任务现场/续聊（不变，项目级）
  artifacts/                   # 产物与任务现场总库
    tasks/<task_id>/           # ★ 新增：任务工作目录（一任务一现场）
      <命令临时中间文件>        #   cwd 直落根
      spill/                   #   工具输出溢出（D5）
      poc/ screenshot/ …/      #   bb_add_artifact 按 kind 分区（D4）
    poc/ screenshot/ …/        # 无任务产物的 kind 平铺（原样保留）
    attachments/ salvage/ browser-shots/ browser-downloads/   # 项目级（不变）
    decompiler-cache/ decompiler-db/ .ghidra-tmp/             # 项目级（不变）
    spill/                     # 无任务 spill（从 <ws>/spill/ 迁入）
  browser-profile/             # 不变
```

### 3.3 关键机制

**cwd 选择**（`core/agent/tools.py` `_tool_run_cmd`）：模块级新助手 `task_workspace_path(artifacts_dir, task_id)`（镜像 `task_transcript_path`），`current_task_id` 非空即传 `gateway.run(..., task_dir=...)`。gateway 内：task_dir 非空 → cwd=task_dir（host/wsl，mkdir 兜底）、pathguard scratch 锚=task_dir；为空 → 现行为 scratch。docker/sandbox 零挂载本不落盘，task_dir 不生效（文档写明；容器侧目录对齐随 container-execution-architecture 方案走）。

**一任务一现场的收益链**：
- 多任务并行不再互踩（每任务独立 cwd）；
- 命令产物天然带任务归属（目录即归属，无需自觉登记）；
- C10 接手时新会话认领同任务 → cwd 同目录 → 文件现场直接可见（transcript 恢复对话 + cwd 恢复文件，双现场连续）；
- traces 的 command 事件已带 cwd 字段——任务目录路径自动进轨迹，产物链接免费获得。

**写边界不收窄的理由**（一期）：pathguard 现有「相对路径上穿即拒」在锚=cwd 后已阻止相对路径写任务目录外；绝对路径写工作区内（如写 scratch）保留给极少数合法场景（如向 samples 放文件由人类做、Agent 无理由绝对路径写别的任务目录）。收窄到任务目录留作后续可选强化，不做承诺。

**path 形态统一红利**：artifacts.path 全线统一为「项目工作区相对路径、带 artifacts/ 前缀」后，`/artifacts/content`（join 根=项目根）、`/artifacts/{aid}/download`、链节点选择器、`find_artifact_by_sha` 全部单形态解析，无前缀分叉。**存量不带前缀的旧行**：content 端点解析时旧形态（不带前缀且按项目根找不到）fallback join `artifacts_dir` 再试一次（一行兼容，不回改存量）。

## 4 实施切分

### M1 后端核心（一次落地）

1. `gateway.py`：`run()` 加 `task_dir` 参；cwd/mkdir/pathguard 锚按 D2/D3 调整；拒因文案「当前工作目录（scratch）」改中性表述；command 事件 cwd 字段自动携带（已有）。
2. `tools.py`：`_tool_run_cmd` 按 current_task_id 传 task_dir；`_tool_bb_add_artifact` 分区+path 带 `artifacts/` 前缀（D4）；`_maybe_spill` 跟任务（D5）+定位器形态；`_tool_read_file` 两段 fallback（D6）；`browser_screenshot` 落点跟随分区。
3. `loop.py`：`task_workspace_path` 助手；STRICT_PROMPT_TAIL 第 7/9 条动态文案（D9）；认领/续跑/接手路径 cwd 一致性。
4. `app.py`：`/artifacts/content` 旧行 fallback；`scratch/clear` 文案补「任务工作目录不受影响」。
5. 测试：gateway cwd/锚/host+wsl 双态/无 task_dir 向后兼容；tools 三工具双路径+冲突序号；read_file fallback；认领建目录/C10 接手同目录/对话轮 scratch；e2e 任务产物落任务目录；grep 既有「scratch」断言面修基线。

### M2 前端与现场增强（排期后置）

1. 任务目录文件树端点（walk+size+mtime，只读）+ 任务窗「产物」面板（登记产物+未登记文件清单）。
2. C10 接手注入「任务工作目录已有 N 文件（最近: …）」行。
3. 链节点选择器/黑板图 artifact 节点对新 path 形态的显示验证。

## 5 风险与对策

| 风险 | 对策 |
|------|------|
| artifacts.path 前缀分叉是现存问题，改形态可能破消费端 | D4 统一带前缀+content 端点旧形态 fallback+测试钉死 content/download 对任务产物可读 |
| 老项目/老任务无目录 | 惰性 mkdir（认领时+gateway 侧双兜底，与 scratch 同款） |
| 在跑任务中途升级（旧命令已写 scratch） | 不迁移存量；新认领起才进任务目录；scratch 旧文件等 scratch/clear |
| pathguard 测试基线锚 scratch | M1 先 grep 断言面，锚参数化后逐个适配（语义不变） |
| 对话轮 bb_add_artifact（无任务）行为变化 | 无任务路径零变化（kind 平铺原样），仅 path 加前缀 |
| Windows 路径长度 | `artifacts/tasks/task-<12hex>/<kind>/<file>` 远低于 MAX_PATH |

## 6 明确不做

- 任务目录自动清理/配额（终态保留，宁留勿删；项目删除整体走 .trash）。
- 写边界收窄到任务目录（后续可选强化）。
- browser-downloads 任务化（池层无任务上下文，属 F6 域）。
- docker/sandbox 容器内工作目录对齐（随 container-execution-architecture）。
- 存量文件物理迁移（只影响新落点）。
