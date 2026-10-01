---
name: binary-diff
description: 跨版本符号迁移：旧版有符号+新版无符号（内核缺 PDB/程序更新/保护更新）时用 LLM 批量比对反汇编与伪代码，产出 YAML 映射表再批量写回 IDB 与 func_kb
keywords: binary diff, 符号迁移, bindiff, pdb, 符号, 版本更新, 内核, ntoskrnl, llm, 比对, 无符号
file_features: dotnet, no_pdb
task_types: reverse, analyze, verify
---

# binary-diff —— 跨版本符号迁移（LLM 辅助比对）

## 适用场景

- 旧版 ntoskrnl/驱动有 PDB 符号、新版 PDB 被下架 → 推导新版非导出函数地址
- 逆向过的程序更新了 → 旧 func_kb 结果批量迁移到新版
- BinDiff/Diaphora 结构变化大时失效的场景 → LLM 按函数粒度比对

不适用：从零逆向（用 binary-rev）；两个完全不同二进制的整体对比（用 BinDiff 工具）。

## 手册对照表（特征 → 打开哪篇，用 kb_open）

| 场景/特征 | 手册 |
|---|---|
| 想知道怎么比、prompt 模板、五步工作流 | `binary/reverse/sym-diff.md`（原理+模板+平台 MCP 工具对接+锚点策略） |
| 要选 LLM / 估算成本 / 大函数处理 | `binary/reverse/sym-diff.md`（批量建议与 LLM 选型段） |
| 迁移结果怎么落地 | `binary/reverse/sym-diff.md`（Step 4：先 bb_upsert_func 落 func_kb，再 rename batch / set_comments 写回 IDA） |
| 典型案例（ntoskrnl 缺 PDB / 应用更新迁移） | `binary/reverse/sym-diff.md`（场景段） |
| Go 程序源码级恢复（非符号迁移） | `binary/reverse/cases/go-tls-proxy-source-recovery.md`（GoReSym 管线） |

## 红线与收尾

- 一次只比对一个函数；锚点不可靠时先修锚点再批量
- 关键符号人工抽检（调用关系/常量交叉印证）；缓存中间结果
- 完成后：符号先落 func_kb（bb_upsert_func，地址+语义名+结论），再批量写回 IDA
  （rename batch / set_comments）——两条线同时保有
