# WSL 命令参数 $ 被吞：wsl.exe 包装层剥引号，改 --exec 直通

## 状态

**已实施**（2026-09-23 用户拍板「开工」随快捷修复包落地：`WSLBackend.execute` argv 加 `--exec`；真链路冒烟 `echo 'a$b'`/awk `$1`/变量展开/单行 120KB grep 四用例全绿；全量回归 971 passed）。

## 拍板记录

| 决策点 | 结论 |
|----|----|
| 吞点定位 | 确认：`wsl.exe bash -lc <cmd>` 形态下 wsl.exe 命令行包装层剥引号（非网关、非 pathguard） |
| 修复形态 | `--exec` argv 直通，全命令形状实验全绿（TMPDIR 前缀 / --cd / -lc 语义均兼容）；2026-09-23 实施 |

## 关联代码

- `core/runtime/backends.py:118-127`（`WSLBackend.execute`——argv 组装处，唯一改动点）
- `core/runtime/gateway.py:164-167`（wsl 分支 TMPDIR 前缀拼接——不涉及，实验证实兼容）

## 1. 现象

Agent 报告「`$` 被网关传输层吞掉」，被迫改用 wsl 内 python3+pymssql 避开 shell 变量。平台同款代码实验复现实锤（host 链完好，仅 wsl 链）：

| 命令（bash -lc） | 期望 | 实得 |
|----|----|----|
| `echo 'a$b'`（单引号字面） | `a$b` | `a` |
| `x=hi; echo [$x]` | `[hi]` | `[]` |
| `awk '{print $1}'`（管道） | `w1` | `w1 w2` |
| `echo 'pid=$$'`（单引号内 $$） | `pid=$$` | `pid=930`（bash PID） |
| `echo 'end$'`（$ 无后随字符） | `end$` | `end$`（存活） |

## 2. 定位实验

- Windows 侧预设 `XTEST=WINVAL` 后 `[$XTEST]` → `[]`：**不是** wsl.exe 按 Windows 环境展开——是 Linux 侧 bash 展开未定义变量
- 单引号内 `$$` 被 bash 展开成 PID：**单引号根本没到 bash 手里**——wsl.exe 把命令行重新引用转发时剥掉了引号，bash 拿到裸命令按自身规则展开 `$var`（Linux 环境无此变量 → 空）
- `wsl --exec bash -c "echo 'a$b'"` → `a$b` 原样：`--exec` 走 argv 直通不经包装层
- `\$` 转义碰巧能过（依赖内部行为，不作数）

## 3. 修复方案

`WSLBackend.execute` argv 加 `--exec`（一行改动）：

```python
argv = ["wsl.exe"]
if self.distro: argv += ["-d", self.distro]
if cwd: argv += ["--cd", cwd]
argv += ["--exec", "bash", "-lc", cmd]   # 原为 ["bash", "-lc", cmd]
```

`--exec` 后的参数 argv 直通给 Linux 侧 bash，引号保真。实验已验证兼容全部网关命令形状：赋值+展开 / 单引号字面 / 行尾 `$` / awk `$1 $2`（管道与 herestring）/ TMPDIR export 前缀 / `--cd` cwd 落点 / `-lc` 登录语义。

- gateway 的 TMPDIR 前缀（`export TMPDIR=…; cmd`）整串仍是 bash -lc 单参数，无影响。
- `pathguard` / `rateguard` 只读命令串，无涉。
- Agent 侧自救姿势（改 python3 脚本绕 shell 层）在修复后不再是必需，但仍是好习惯（少一层 shell 语义）。
- 回归：tests 中 WSLBackend argv 断言（如有）需同步；用本表命令形状做冒烟。
