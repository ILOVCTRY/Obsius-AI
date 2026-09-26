# 方案：知识库路由注入链加固（匹配口径 / 可消费注入物 / K7 真使用口径）

- **状态**：**已全量实施（2026-09-24，一个批次 7 项）**（只读代码走查立项，一轮商议定稿；定稿决策已回写 DESIGN.md）
- **实施修正**：见文首「§0 实施修正注记」
- **拍板记录**：§3（3 项决策用户拍板，范围=7 项全修）
- **关联代码**：`core/skills/matching.py`（新增共享原语）、`core/skills/router.py`、`core/skills/kbindex.py`、`core/skills/routeindex.py`、`core/agent/loop.py`、`core/agent/tools.py`、`core/api/app.py`（`_route_zero_hit_issues`）
- **实施后**：定稿决策沉淀回 DESIGN.md，同步 core/skills、core/agent、core/api 三处 CLAUDE.md；本文保留作方案背景

## §0 实施修正注记

1. `kb_route_hints` 返回形状由方案设想的 `(key, paths)` 定稿为 **`(key, paths, dir_total)` 三元组**——`dir_total` 为命中目录前缀的总篇数，loop 渲染时追加「（目录共 N 篇，更多用 kb_search）」。
2. `_expand_prefix` 对声明目标物理不存在（既非文件也非目录）的情形**原样透传**而非吞掉：保持「坏了静默降级但不丢声明」的韧性，存在性由 doctor（route-index-kb-missing）体检兜底。
3. 新增测试 8 个（test_skills ×6、test_agent read_file ×1、test_api K7 端点 ×1），全量回归见 §5。

## 1. 愿景与背景

知识库向 Agent 的注入链共四层：

1. **技能路由**（router.py）：task_type +10 > 角色白名单 +5 > features/标签 ×3 > keywords ×2 > description ×1，取 Top；
2. **route.json 静态导航**（kbindex.kb_route_hints）：按任务类型/描述命中「键→前缀组」；
3. **kbindex 内容 hints**（kb_module_hints）：中文 2-gram + ASCII 整词；
4. **route_index.yaml Top-K**（routeindex.render_route_index_top，Top-5）。

增强层共同纪律：**坏了静默降级、不阻断主链**。

立项动机：只读走查 + 实测脚本发现，数据本身健康（98 条索引路径/tags 全部有效、54 个 route.json 值全部物理存在、doctor 无 error），但匹配口径与注入物可消费性存在确定性缺陷。

## 2. 现状盘点（2026-09-24 核实）

### 2.1 纯子串匹配无词边界

route.json 键、route_index match、router keywords 直接做大小写不敏感子串，2-3 字母英文词在英文任务描述里确定性误命中（实测复现）：

- `backup the whole stack and make a snapshot`：`make` 含 `ak`、`stack` 含 `sk` → 误注入云 AK/SK 导航；
- `explain the main domain in the email`：`main` 含 `ai` → 误命中 `ai|大模型|llm`；
- `open expense report for operations team`：`expense` 含 `pe` → 误注入「恶意样本 PE / .NET」。

注册表中同类短词有 60+ 个（ad/ai/ak/nc/so/vm/dom…），且误命中经 skill.routed 事件污染 K7 统计。

### 2.2 注入物不可消费

- route.json 的 25 个**目录前缀**（如 `cloud/native/k8s/`）不是合法 kb_open 参数（后缀白名单拒收），导航给了打不开的目标；
- kb_open 回执指示「Read 手册」，但 read_file 以工作区为唯一允许根，packs 内手册读不到——指示与工具互相打架。

### 2.3 K7 统计名实不符

`_route_zero_hit_issues` 的 `used` 集合只由「被注入」的 route_points 构建，实际语义是「从未被注入」；注入后被 Agent 忽略的条目永远报不出来。

## 3. 定稿决策（用户拍板 2026-09-24）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | read_file 与 kb_open 冲突 | **read_file 放行启用域 kb 源根**（仅 `packs/kb/<域>/`，不放开整个 packs；packs/experts 等仍拒） |
| 2 | route.json 目录前缀 | **渲染时展开为具体文件**（前 3 篇 + 目录总数提示），不改 route.json 数据形态 |
| 3 | 修复范围 | **7 项全修，一个批次** |

## 4. 设计详述

### 4.1 共享匹配原语 core/skills/matching.py

零内部依赖小模块，router / kbindex / routeindex 共用，避免互相 import：

```
term_matches(term: str, text_lower: str) -> bool
```

口径定稿：

- term 含中文 → **子串**匹配（中文无词边界，现行行为不变；混合词同口径）；
- term 纯 ASCII → **词边界正则** `(?<![a-z0-9])<escape>(?![a-z0-9])`：underscore/hyphen/标点视为边界，数字词（401/s3）同理；
- term 以 `*` 结尾（现行仅 `ret2*`/`house*`）→ **前缀语义**：ASCII 只保留 lookbehind（命中 ret2text/ret2shellcode、house-of），含中文退化为子串。

### 4.2 三处匹配面切换

- routeindex.score_entry：`w in query or query in w` → term_matches（CJK 双向子串因「文件上传」⊂「文件上传测试」自然成立）；
- kbindex.kb_route_hints：`alt in combined` → term_matches（含同义词扩展拼接段，逻辑不变）；
- router keywords 循环：`kw.lower() in q` → term_matches。权重/词表零改动（遵守「改路由词表先改 DESIGN.md」）。

### 4.3 目录前缀渲染时展开

kbindex 新增 `_expand_prefix(packs_root, prefix, cap=3)`：文件型原样返回；目录型先 `glob("*.md")`、空再 rglob，排除 .history，返回前 3 篇全局形态路径 + 目录总篇数。loop 的 route_block 列具体路径并提示总数；kb_hits 收集落到真实文件，retrieval_stats 对账更准。API `GET /api/capabilities/{cap}/kb/routes` 消费原始 route.json，行为不变。

### 4.4 K7 真使用口径

`_route_zero_hit_issues` 逐项目 db（sqlite mode=ro 只读旁路）同时收集：skill.routed 中 `route_index>0` 的 route_points（真注入过）与 kb.open 的 payload.module（手册真被打开）。point→kb 映射由 parse_entries 构建；point 被注入过且其 kb 在任一项目被打开过才算 used；报 `all_injected - used`，文案「曾被注入路由索引但对应手册从未被 kb_open 打开」。

### 4.5 打磨项

- 标题去歧义：routeindex 两处注入 head「🧭 测试点路由索引」→「📖 测试点手册索引」；「🧭 知识库任务导航」保留；
- loop 可用知识库源 JSON 加 `s.root.is_dir()` 过滤（占位域不登记）；
- 单文件解析爆炸半径（一处笔误全表空）：**维持 fail-fast**，doctor 立即报 route-index-bad-yaml，不改静默跳过，避免笔误被掩盖。

## 5. 验证

1. 全量回归：`E:\Miniconda3\python.exe -m pytest tests -q`（基线 812 passed + 新增 8 用例）。
2. 重跑误命中 PoC：`make/stack` 不命中 ak/sk、`main/email` 不命中 ai、`open expense` 不注入 pe；真任务（「凭证泄露 AK 泄露」、「快照恢复 RDS」）仍正常命中。
3. `GET /api/packs/doctor`：原 error/warning 集合不回归；K7 issue 判定与文案按真使用口径。
4. 端到端冒烟：启动平台，带 kb 的项目任务页导航段给出可直接 kb_open 的文件路径，kb_open 后 read_file 能读到手册正文。
