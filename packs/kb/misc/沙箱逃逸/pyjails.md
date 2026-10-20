---
title: Python 沙箱逃逸
phase: misc
vuln_class: [sandbox-escape, python-jail, blacklist-bypass, audit-hook, restricted-execution]
summary: Python jail 先判断限制的是字符、AST、长度还是审计事件，再选对应家族的手法；黑名单式过滤几乎总能靠属性链、下标调用、反规范化或重入绕过。
---

# Python 沙箱逃逸

其他语言逃逸见 [[misc/沙箱逃逸/bashjails.md|Shell 沙箱逃逸]]；编码基础见 [[misc/编码解码/encodings-advanced.md|高级编码与专用格式判定]]。

## 0. 判据速查

| 限制类型 | 识别方法 | 对应家族 |
|---|---|---|
| 字符黑名单 | 逐字符试错 | 转义/NFKC/构造字符串 |
| AST 节点禁用 | 报错位置与类型 | 属性链/下标调用 |
| 禁止调用 `()` | 输入含括号即拒 | `__getitem__` 回调 |
| 禁止引号/等号 | 同上 | 装饰器绑定/名称取参 |
| 长度上限 | 长度反馈或截断 | 三段式长度重入 |
| 素数长度 | 仅报非素数 | 注释 padding 到素数 |
| 审计钩子 | 有 hook 但能 import | hook 变更/信号 trampoline |
| 输出 oracle | 回显真假 | 二分/线性探测 |

## 1. 侦察与枚举

先确认三件事：解释器版本、哪些表达式能求值、什么会触发拒绝。

```python
# 探测：报错信息就是最强的枚举器
().__class__.__mro__                      # 能走多远
[].__class__.__subclasses__()             # 是否有结果
getattr, str, chr, open, print            # 逐个名字试
```

- 报 `NameError`/`AttributeError` 说明过滤在求值后；报 `SyntaxError` 说明在 AST/编译前。
- 黑名单常见模式：禁 `__class__`、`import`、`eval`、`os`、`system`。命中模式要先确认是子串匹配还是词级匹配——子串匹配可用拼接/转义绕过。
- 输出 oracle 型（只回真假）用二分法探测长度、逐字符探测字符集、再按位置拼接，等价于“猜数字”。

## 2. 字符串与字面量构造

不能用引号/连接时，先把“字符串”造出来：

- 十六进制/八进制转义：`"\x6f\x73"` → `os`；`"\141\142"` → `ab`。
- `"%c" % 65` → `A`；数字本身可用重复元组长度拼出（`(~([]<[]))` 等）。
- NFKC 规范化：数学字母数字区（`U+1D400..U+1D7FF`）在**解析期**会被 NFKC 归一为 ASCII，可用于绕过源级黑名单；但运行期属性查找不归一，`getattr` 时仍报错——需显式 `unicodedata.normalize`。
- magic coding 注释：`# -*- coding: raw_unicode_escape -*-` 等可改变文件解码方式，间接注入字节。
- `dir()` 索引：`dir([]).index("__class__")` 得到被禁名在列表中的下标，再用 `getattr(obj, dir([])[i])` 取属性，全程不出现黑名单字样。

## 3. 经典逃逸手法

- **类层级链**：`().__class__.__bases__[0].__subclasses__()` 找 `os._wrap_close` 等持有 `__init__.__globals__` 的类，取其 globals 中的 `builtins`/`os`。
- **func_globals 链遍历**（PlaidCTF 经典）：从 `warnings.catch_warnings` 子类经 `__init__.__globals__` 拿到 `warnings`/`linecache`/`os` 模块链。索引会随版本变化，先枚举再取。
- **名称改写**：`__name__` 作为字符串键、getset_descriptor 取名字、类属性持久化（把载荷存到子类属性，下一轮提交再取用）可跨轮次绕过无状态限制。
- **compile/exec 旁路**：若 `eval` 被禁但 `compile`/`exec` 只是被字符串过滤，换等价调用或在 f-string 配置渲染时触发。
- **walrus 复位**：`while (x := ...)` 之类在表达式内重新绑定约束变量。
- **eval 拼接注入**（Codegate）：`eval("your." + input + "()")` 时输入 `dig(),eval(...)` 把任意代码注入元组；也可通过注册变量名携带载荷。

## 4. 受限语法家族

### 4.1 无括号 `()`

核心思路：把“调用”改写为“下标”。

```text
class X: __getitem__ = <要执行的 gadget>
X()["payload"]     # 下标即调用
```

先给自定义类（或可变容器）绑定 `__getitem__` 为 `eval`/`__import__('os').system`（用 `staticmethod` 避免隐式 self），即可在不输入括号的情况下执行。赋等号本身也被禁时用装饰器路径绑定（见 4.2）。

### 4.2 无引号无等号（装饰器链）

目标是在不能使用 `()`/引号/`=` 时拿到真实 builtins。手法：

1. `function.__name__` 提供字符串键；
2. getset_descriptor 提取对象名字；
3. 经 `__loader__` 拿到解释器真实 globals，从而取得未污染的 `__builtins__["__import__"]`；
4. 装饰器在定义时求值，可把上述链条折叠进不带等号的表达式。

### 4.3 单点号

只允许一个 `.` 时，把该点用于获取 `__getattribute__`（如 `"".__getattribute__`），之后用 `get(obj, "attr")` 形式替代所有属性访问，最终仍可走到 `os.system`。

### 4.4 素数长度

要求 `len(code)` 为素数时：先构造核心 payload，再用注释（`#...`）或 `;pass` padding 到最近的素数。若构造函数的字符集也受限，可用 `__reduce_ex__` 等 pickle 风格 gadget，并把载荷用十六进制转义压缩。

## 5. 审计钩子（PEP 551/578）逃逸

当沙箱用 audit hook 拦截 `exec`/`import` 等事件时，攻击的是“钩子本身”或“事件覆盖面”：

1. **Python 层钩子变更**：钩子列表在 C 结构里（`_PyRuntime.audit_hooks`）不可直接枚举，但可改写钩子函数对象——覆盖闭包 cell 翻转其内部标志，或替换其 `__code__` 使其静默。
2. **eval 后生命周期 `__repr__`**：`eval("R()")` 只触发 `exec`/`eval` 审计而不触发 `exec` 事件；随后 `print(result)` 调用 `R.__repr__` 执行 shell——审计从未看到 exec。
3. **信号处理 trampoline**：`signal.signal(SIGALRM, handler)` 只被记为信号注册；到期触发 handler 执行命令，绕过 exec 审计。
4. **原生 `_PyRuntime` 清零**：通过 `ctypes.pythonapi` 定位导出的 `_PyRuntime`，把 `audit_hooks` 链表头写空，使后续 exec 无审计。

判断顺序：能否 import ctypes/signal → 钩子是 Python 对象还是 C 安装 → 是否有求值后自动调用 `__repr__`/析构的路径。

## 6. 重入与长度重估

若服务器按长度过滤后重新 `eval`，且能触发“重新读取输入”，可三段式抬高预算：第一段定义别名并重入（`i=input;f(i())`），第二段在新一轮里放大限制参数（`M=99;f(i())`），第三段用新预算执行 RCE。关键是服务器对每轮输入都做一次 `eval`/过滤，而不是只过滤一次。

Quine+上下文检测可用于判断当前在哪个执行环境（哪些 globals 存在），只在正确的上下文里投放载荷。多阶段类属性持久化则适合“每轮状态重置”的场景。

## 7. 其他入口

- **`.pyc`/magic 文件与 ReDoS**：若服务读入可匹配魔术注释的文件，可用灾难性回溯消耗资源或改变解析路径。
- **环境变量/子进程**：能影响 `PYTHONSTARTUP`、`BASH_ENV` 等即等同执行。
- **受限编辑器**（如 vim）：`K` 触发 man、`:!sh` 逃逸；`help()`/`breakpoint()` 未过滤时会进入 pdb，可 `!import os` 执行。

## 8. 纪律

每步先确认“拒绝发生在哪一层”（字符、AST、编译、求值、审计、系统调用），再选家族手法。构造 payload 时逐段验证：报错信息、返回值、副作用。所有利用仅用于授权环境；修复建议应指向结构性问题（白名单而非黑名单、禁用属性链入口、长度与审计在 C 层强制执行、过滤后再求值需重新审查）。
