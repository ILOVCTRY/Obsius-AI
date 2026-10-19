# core/ctf/

> CTF 轨专用内核（2026-10-19）。来源：对标 hxbai/hxctf 的自主交战引擎，只搬**无形态依赖的判定与抽取**，不搬调度/执行器。**零黑板依赖、纯 stdlib**——可单测、可被工具层与对话层共用。设计真相源见 [DESIGN.md「CTF 解题内核」](../../DESIGN.md)。

## 四模块

| 文件 | 职责 | 接线点 |
|------|------|--------|
| `flagjudge.py` | flag 判定：候选抽取 / 置信打分 / 变换回环 / 跨题家族 | `core/agent/tools.py::_tool_ctf_submit_flag`、`_flag_neighbor_owner` |
| `observe.py` | 观测结构化：nmap→端口、HTTP 信号、凭据四形态、salient 要点、**`extract_flags` 显式 flag 候选段**、易变值归一化 | `ToolDispatcher._finish_cmd_receipt` → `_with_ctf_observations`（`run_cmd` 回执尾） |
| `ammo.py` | 凭据弹药：产品指纹 → 厂商默认口令 → 变异 → `user:pass` 成对 | `core/agent/tools.py::_tool_ctf_ammo`（写 scratch 候选表） |
| `pacing.py` | 时长无关预算缩放（`Pacing` 值对象）+ endgame 收尾窗口 + `normalize_ctf_config`（`config.ctf` 写入口归一化） | `ChatTurn._pacing_context()` → 注入 `_challenge_context()`；`core/projects.py::update_config` |

## 配置写入口

`config.ctf = {deadline, tasks}` 由 `pacing.normalize_ctf_config` 归一（`core/projects.py::update_config` 调用，非法 422，空段剥键回落阶梯推断）。前端入口 = 黑板「CTF-Agent」tab 的「赛制时间」面板（`webui/src/views/Blackboard.tsx::CtfAgentPane`，`api.patchProjectConfig(pid, {ctf})`）。**改读路径（`_pacing_context`）必须同步改归一化**——否则非法值静默进盘、`int()` 吞成 0 后悄悄回退锚定题量，人看不出配置没生效。

## 纪律（改这里之前先读）

- **不做进程级单例**：`pacing.Pacing` 是值对象。多题并发下 A 题的 elapsed 不能影响 B 题的预算，任何 `global`/模块级可变状态都是缺陷。
- **只产候选、不做动作**：`ammo` 不执行爆破、不发网络请求；`observe` 不写黑板；`flagjudge` 不提交 flag。是否用、用多少条、写不写库由工具层与提示词纪律决定。
- **`observe.extract_flags` 只提示不判定**：它复用 `flagjudge.confident_flags` 三道闸列「flag 候选」段，但**不改任何状态**——是否算解出由 `challenge_flags` 台账（唯一真值）决定。改它别顺手去调 `record_flag`。
- **`_clean_twin` 不洗诱饵前缀**：`fake_flag{deadbeef99}` 截成 `flag{deadbeef99}` 会白送 +3 前缀分绕过三道闸（实测踩过）。孪生只处理**中性上下文**（`theflag{}`/`picoCTFflag{}`），`is_bait_prefix` 为真时直接返 None。
- **宁松勿误判的边界**：`roundtrip_verify` 遇到不认识的变换名返回 `None`（无法判定），不是 `False`；`observe` 抽不出信号返回空串，让回执保持原样。**硬判只留给明确违规**（诱饵/自然语言内层/跨题串号）。
- **反伪造口径**：「模型说自己证明了不算，证据链说了算」。`FLAGISH_FLOOR=2.0`、`MIN_INNER_LEN=3`、`DIFF_LINE_MIN=4` 这些阈值改动等于放宽闸门，须同时改测试与 DESIGN。
- `flag{example}` 这类「前缀正常 + 内层占位」的形态**必须被判拒**（原版 hxbai 只看前缀会漏）——见 `is_bait_inner` / `_inner_too_short`。

## 测试

- `tests/test_ctf_flagjudge.py`（23）/ `test_ctf_observe.py`（32）/ `test_ctf_ammo.py`（16）/ `test_ctf_pacing.py`（27）——本包纯函数全覆盖。
- 接线行为：`tests/test_ctf_submit_flag.py`（19，含 `run_cmd` 观测摘要附加与非 CTF 轨零变化）、`tests/test_ctf_ammo_tool.py`（14，含落盘/幂等/不泄 flag）、`tests/test_ctf_chat_pacing.py`（11，含 `_to_epoch` 两形态与 endgame 注入）、`tests/test_agent_repeat_guard.py`（8，原地打转提示，与 `observe` 共用 `_finish_cmd_receipt` 出口）。
- **接线测试要断言「输出里确实有东西」**（如 `scale=`、`[候选 N 条]`），不能只断言「没抛异常」——`_pacing_context()` 被 `except` 吞掉静默返空串时，弱断言仍然全绿而功能已死。
