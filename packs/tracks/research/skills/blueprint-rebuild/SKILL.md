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

## 阶段一：模块划分（task_type=blueprint，单会话）

输入：headless 导出概览（`bb_query what=func` / functions 清单 / strings / imports）。

1. 按**业务职能**聚类：网络通信 / 加密校验 / 文件持久化 / 许可校验 / UI…
   不按编译单元；已知库函数（libc/API 包装）标 noise 不进模块。
2. `bb_blueprint_create(name, goal, binary_sha256, modules=[…])` 建骨架；
   每模块必附 func_addresses 清单（hex 串）与业务推断理由（desc）。
3. **接口 spec 先行钉死**：每模块 spec 写清函数签名/数据结构/协议格式。
   V1 无任务级依赖调度——接口先钉死是并行深析与组装不冲突的唯一保险。

## 阶段二：模块深析（task_type=reverse，按模块并行）

1. 认领后 `bb_query what=blueprint` 找到自己的模块，逐函数：
   **先查 func_kb 防重复** → decompile → `bb_upsert_func` 落结论。
2. **必须带 `module:<模块名>` risk_tag**——这是函数归属蓝图的机器可读锚点，
   汇总与重建都靠它检索。
3. 关键实现要点/业务逻辑发展 `bb_blueprint_update(module_name=…, notes=…)`
   写回；接口如果发现 spec 划错了，**写 notes 上报，勿自行改接口**。

## 阶段三：蓝图汇总（task_type=blueprint）

各模块 done 后：读全部 func_kb（按 module 标签过滤）+ 各模块 spec，
`bb_blueprint_update(content_append=…)` 写数据流/接口表/算法/协议/状态机。
蓝图 status（reviewed/ready）由人类流转——勿申请、勿催。

## 阶段四：重建实现（task_type=reconstruct，每模块一个）

1. 代码落 `<proj>/artifacts/rebuild/<module>/`（run_cmd 写，合法路径）。
   语言依蓝图，默认 Python。
2. **自测对拍**：依 func_kb 原实现伪码构造输入/输出向量，写自测脚本，
   run_cmd 以 `threat_class=untrusted` + 容器 runtime（docker/sandbox）执行。
   **生成代码一律 untrusted，绝不 host 直跑**（红线第 9 条）。
3. 对拍全过 → `bb_blueprint_update(module_name=…, module_status=tested)`；
   **不过就修到过，或在 notes 记录失败原因上报——不得虚标 tested**。

## 阶段五：组装集成（task_type=reconstruct）

全部模块 tested 后：拼入口/胶水代码（接口冲突人工兜底，有冲突先上报）→
容器整体冒烟 → zip 产物 `bb_add_artifact(kind="project")` + 产出蓝图 vs 实现
差异表报告（哪个函数简化了、哪个行为未复现）。人类终验后蓝图置 built。
