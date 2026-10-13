---
title: 跨版本符号迁移（LLM 比对）
summary: 旧版有符号+新版无符号时用 LLM 批量比对反汇编/伪代码，产出 YAML 符号映射表再批量写回 IDB；缺 PDB 内核/版本更新迁移场景
phase: reverse
vuln_class: [symbol-migration]
---

# 跨版本符号迁移（LLM 辅助 Binary Diff）

> 来源：reverse-skill 1.0.1 收编改编（2026-09-30，MIT，见 `../../licenses/`）；
> 原项目 SKILL.md 与 prompt-template 内容重复，本篇合一收编；工具调用已适配平台
> IDA MCP 工具面（decompile / list_funcs / rename / set_comments）。配合技能
> [binary-diff](../../capabilities/binary/skills/binary-diff/SKILL.md) 使用。

## 适用场景

1. **内核/驱动缺 PDB**：有旧版 ntoskrnl.exe 符号，新版 PDB 被下架，用旧版符号推导
   新版非导出函数地址
2. **程序更新后符号迁移**：逆向过的程序更新了，不想重逆，用旧版结果批量迁移
3. **保护机制更新**：旧版有完整逆向结果，新版快速定位同一函数的新偏移
4. 任何「有旧版符号 + 新版无符号」的二进制对比场景

## 与其他手段的分工

| 场景 | 用什么 |
|------|--------|
| 从零开始逆向一个二进制 | binary-rev 技能（IDA MCP / 静态工具） |
| 有旧版结果，迁移到新版 | **本篇** |
| 两个完全不同的二进制对比 | BinDiff / Diaphora（传统工具） |

成本对比（200 函数）：人工双窗口数小时；BinDiff 快但结构变化大时失效；
**LLM 批量比对 ~1 元、约 10 秒/函数、准确率高**。

## 核心原理

```text
旧版函数（有符号）          新版同一函数（无符号）
    ↓                              ↓
导出反汇编 + 伪代码          导出反汇编 + 伪代码
    ↓                              ↓
    └──────── LLM 结构化比对 ────────┘
                    ↓
         输出 YAML（符号映射表）
                    ↓
         程序化解析 → 批量应用到新版 IDB
```

关键点：prompt 固定模板程序化填充；输入输出格式确定、程序化解析；LLM 只负责
「看两段代码，找出对应关系」；token 成本极低。

## 平台工具面对接

| 原文工具 | 平台等价 | 说明 |
|---|---|---|
| idapro_disasm / 导出反汇编 | MCP `decompile`（伪代码）+ 静态工具反汇编 | 平台 decompile 按需自动拉起 IDA |
| 旧版符号清单 | `bb_query what=func binary_sha256=<旧sha>` | func_kb 里已有的命名函数就是旧版符号源 |
| 批量重命名 | MCP `rename`（batch{func:[{addr,name}],allow_overwrite}） | 或工作台 writeback 端点（func_kb→IDA，落 name_history） |
| set_comments | MCP `set_comments`（items:[{addr,comment}]） | vcall/struct 偏移落注释 |

平台化流程优势：迁移结果先落 func_kb（`bb_upsert_func`，地址+语义名+算法结论），
再经 writeback 批量写回 IDA——两条线（func_kb 主真相 + IDB 注释）同时保有。

## Prompt 模板（标准比对）

```text
I have disassembly outputs and procedure code of the same function.

This is the function for reference:

**Disassembly for Reference**
```c
{disasm_for_reference}
```

**Procedure code for Reference**
```c
{procedure_for_reference}
```

This is the function you need to reverse-engineering:

**Disassembly to reverse-engineering**
```c
{disasm_code}
```

**Procedure code to reverse-engineering**
```c
{procedure}
```

What you need to do is to collect all references to "{symbol_name_list}" in the
function you need to reverse-engineering and output those references as YAML.

Example:
```yaml
found_vcall: # 虚函数间接调用 / 虚表指针获取
  - insn_va: '0x180777700'          # 带 displacement 的指令
    insn_disasm: call [rax+68h]
    vfunc_offset: '0x68'
    func_name: ILoopMode_OnLoopActivate

found_call: # 对普通函数的直接调用
  - insn_va: '0x180888800'
    insn_disasm: call sub_180999900
    func_name: CLoopMode_RegisterEventMapInternal

found_funcptr: # 普通函数指针引用
  - insn_va: '0x180666600'
    insn_disasm: lea rdx, sub_15BC910
    funcptr_name: CLoopMode_OnClientPollNetworking

found_gv: # 全局变量引用
  - insn_va: '0x180444400'
    insn_disasm: mov rcx, cs:qword_180666600
    gv_name: g_pNetworkMessages

found_struct_offset: # 结构体偏移引用（虚函数指针不算，永远放 found_vcall）
  - insn_va: '0x1801BA12A'
    insn_disasm: mov rcx, [r14+58h]
    offset: '0x58'
    size: 8
    struct_name: CResourceService
    member_name: m_pEntitySystem
```

If nothing found, output an empty YAML. DO NOT output anything other than the
desired YAML. DO NOT collect unrelated symbols.
```

变量来源：`{disasm_for_reference}`/`{procedure_for_reference}`=旧版有符号导出；
`{disasm_code}`/`{procedure}`=新版无符号导出；`{symbol_name_list}`=从旧版提取的
待定位符号清单。

## 工作流五步

```text
Step 1: 准备数据
  - 旧版二进制加载到 IDA（有 PDB/符号），新版加载（无符号）
  - 找到两版本相同的锚点函数（导出函数、字符串引用等）

Step 2: 批量导出
  - 旧版：锚点函数的反汇编 + 伪代码（含符号名）
  - 新版：同一锚点函数的反汇编 + 伪代码（无符号名）

Step 3: LLM 比对
  - prompt 模板填充数据，调用 LLM API（推荐 deepseek 量大便宜，超大函数切 gpt）
  - 解析返回的 YAML

Step 4: 应用结果
  - found_call   → rename(addr=call_target, name=func_name)
  - found_vcall  → set_comments(addr=insn_va, "vcall: {func_name} @ +{offset}")
  - found_funcptr→ rename(addr=funcptr_target, name=funcptr_name)
  - found_gv     → rename(addr=gv_addr, name=gv_name)
  - found_struct_offset → set_comments(addr=insn_va, "{struct_name}.{member_name}")
  - 平台路径：先 bb_upsert_func 落 func_kb，再经 rename batch / writeback 写回 IDA

Step 5: 迭代
  - 第一轮迁移的函数成为新锚点，进入这些函数继续对比内部调用，重复至覆盖目标
```

### 锚点选择策略

| 锚点类型 | 可靠性 | 说明 |
|---------|--------|------|
| 导出函数 | 最高 | 名字不变，地址可能变 |
| 字符串引用 | 高 | 字符串内容不变，引用位置可能变 |
| 常量/魔数 | 中 | 特征值不变 |
| 代码模式 | 中 | 函数结构相似但地址全变 |

### 批量处理建议

- 每次比对 1 个函数（避免 context 爆炸）；10-20 并发提高速度；结果缓存防重复调用
- 中小函数（<200 行）用 deepseek；超大函数（>500 行）切 gpt-4o 或 claude
- 默认 DeepSeek，遇 context 超限或结果不准时自动升级

## 典型场景

### 场景 1：ntoskrnl.exe 缺 PDB

```text
已有：ntoskrnl.exe 10.0.26100.2000 + 完整 PDB
目标：ntoskrnl.exe 10.0.26100.2605（PDB 被下架）
需求：定位 PspSetCreateProcessNotifyRoutine 的新地址

1. 两版本都加载到 IDA
2. 找到导出函数 PsSetCreateProcessNotifyRoutine（两版本都有）
3. 旧版中它调用了 PspSetCreateProcessNotifyRoutine（有符号）
4. 新版中它调用了 sub_140822108（无符号）
5. LLM 一眼看出：sub_140822108 = PspSetCreateProcessNotifyRoutine
6. 批量应用
```

### 场景 2：应用更新后迁移

```text
已有：target.exe v1.0 完整逆向结果（200+ 函数已命名，在 func_kb）
目标：target.exe v1.1（符号全丢）
1. 从 func_kb 导出旧版已命名函数的反汇编+伪代码
2. 新版经导出函数/字符串找对应锚点
3. 批量调 LLM 比对 → 解析 YAML → bb_upsert_func + rename batch
4. 迭代深入
```

## 红线与注意事项

- **不要把整个二进制丢给 LLM**——一次只比对一个函数
- **锚点必须可靠**——锚点对错一环，后续全白费
- **结果需人工抽检**——LLM 非 100% 准确，关键符号要验证（调用关系/常量交叉印证）
- **缓存中间结果**——避免重复调用浪费 token
- **超大函数（>1000 行反汇编）**——拆分或用大 context 模型

## 依赖

| 工具 | 用途 | 可自动安装 |
|------|------|-----------|
| IDA Pro + 平台 IDA MCP | 导出反汇编/伪代码、批量 rename/注释 | ✗（商业软件，平台已集成） |
| Python + PyYAML | 脚本执行、解析 LLM 返回的 YAML | ✓（pip install pyyaml） |
| LLM API | 执行比对 | 需 API key |
