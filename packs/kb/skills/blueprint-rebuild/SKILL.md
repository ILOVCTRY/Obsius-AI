---
name: blueprint-rebuild
description: R4 逆向开发管线：模块划分、蓝图写回、重建实现、容器自测对拍、组装集成
keywords: 蓝图, blueprint, 重建, reconstruct, 开发, 模块划分, 自测
features: is_elf, is_pe, packed_binary
task_types: blueprint, reconstruct, analyze, verify
mode: self-contained
---

# blueprint-rebuild —— 蓝图驱动的逆向重建（R4）

> 红线先读：research 轨 redlines 第 8-12 条（仅授权样本/生成代码一律 untrusted
> 容器执行/自测不过不得标 tested）。整体流程见 DESIGN.md §9 R4。

> **执行载体（2026-10-13 起，任务机制退役后）**：本管线走**智能体工作台主+子**——
> 主控 chat-orchestrator 编排、用 `call_expert` 扇出子专家执行；**不再有任务认领/
> 派单/派生 reconstruct 子任务**。入口=逆向工作台「蓝图」页签的按钮（建线程 + 下发
> 阶段 objective）：阶段一「生成蓝图」、阶段二「按模块深析」、阶段四「派发重建」
> （仅蓝图 status=building 时露），实际执行在工作台。
> **「模块」是并行单位**：阶段二/四把每个模块作为一个独立任务 `call_expert` 委派
> （单轮 ≤8，超过分批），任务描述必须自包含（写明模块名 + 锚点函数清单）。

## 阶段一：模块划分（主控委派，单子专家）

输入：headless 导出概览（`bb_query what=func` / `list_symbols` / `strings_search` / imports）。

1. 按**业务职能**聚类：网络通信 / 加密校验 / 文件持久化 / 许可校验 / UI…
   不按编译单元；已知库函数（libc/API 包装）标 noise 不进模块。
2. `bb_blueprint_create(name, goal, binary_sha256, modules=[…])` 建骨架；
   每模块必附 func_addresses 清单（hex 串）与业务推断理由（desc）。
3. **接口 spec 先行钉死**：每模块 spec 写清函数签名/数据结构/协议格式。
   接口先钉死是并行深析与组装不冲突的唯一保险。

## 阶段二：模块深析（主控按模块 `call_expert` 扇出，≤8/批）

1. 主控把**每个模块**作为一个独立任务委派；任务里自包含写明
   「模块名 + 该模块的 func_addresses 锚点清单」。
2. 子专家逐函数：**先查 func_kb 防重复** → decompile → `bb_upsert_func` 落结论。
3. **必须带 `module:<模块名>` risk_tag**——这是函数归属蓝图的机器可读锚点，
   汇总与重建都靠它检索。
4. 关键实现要点/业务逻辑发展 `bb_blueprint_update(module_name=…, notes=…)`
   写回；接口如果发现 spec 划错了，**写 notes 上报，勿自行改接口**。

## 阶段三：蓝图汇总（主控委派，单子专家）

各模块 done 后：读全部 func_kb（按 module 标签过滤）+ 各模块 spec，
`bb_blueprint_update(content_append=…)` 写数据流/接口表/算法/协议/状态机。
蓝图 status（reviewed/ready）由人类流转——勿申请、勿催。

## 阶段四：重建实现（主控按模块 `call_expert` 扇出）

1. 代码落 `<proj>/artifacts/rebuild/<module>/`（run_cmd 写，合法路径）。
   语言依蓝图，默认 Python。
2. **自测对拍**：依 func_kb 原实现伪码构造输入/输出向量，写自测脚本，
   run_cmd 以 `threat_class=untrusted` + 容器 runtime（docker/sandbox）执行。
   **生成代码一律 untrusted，绝不 host 直跑**（红线第 9 条）。
3. 对拍全过 → `bb_blueprint_update(module_name=…, module_status=tested)`；
   **不过就修到过，或在 notes 记录失败原因上报——不得虚标 tested**。

## 阶段五：组装集成（主控委派，单子专家）

全部模块 tested 后：拼入口/胶水代码（接口冲突人工兜底，有冲突先上报）→
容器整体冒烟 → 产出「蓝图 vs 实现」差异表报告（哪个函数简化了、哪个行为未复现），
经 `bb_add_artifact(kind="project")` 落为**文本记录**；产物本体留在
`<proj>/artifacts/rebuild/`（人类在文件树查看，**bb_add_artifact 只存文本、不上传文件**）。
人类终验后蓝图置 built。
