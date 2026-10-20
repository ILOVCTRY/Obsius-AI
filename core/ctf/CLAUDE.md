# core/ctf/

> CTF 轨专用内核（2026-10-19）。来源：对标 hxbai/hxctf 的自主交战引擎，只搬**无形态依赖的判定与抽取**，不搬调度/执行器。**零黑板依赖、纯 stdlib**——可单测、可被工具层与对话层共用。设计真相源见 [DESIGN.md「CTF 解题内核」](../../DESIGN.md)。

## 九模块

| 文件 | 职责 | 接线点 |
|------|------|--------|
| `flagjudge.py` | flag 判定：候选抽取 / 置信打分 / 变换回环 / 跨题家族 | `core/agent/tools.py::_tool_ctf_submit_flag`、`_flag_neighbor_owner` |
| `observe.py` | 观测结构化：nmap→端口、HTTP 信号、凭据四形态、salient 要点、**`extract_flags` 显式 flag 候选段**、易变值归一化、**跨回执抽取（`parse_linktypes`/`parse_script_names`）** | `ToolDispatcher._finish_cmd_receipt` → `_with_ctf_observations`（`run_cmd` 回执尾）；跨回执抽取由 `CtfSession.record` 调用 |
| `ammo.py` | 凭据弹药：产品指纹 → 厂商默认口令 → 变异 → `user:pass` 成对 | `core/agent/tools.py::_tool_ctf_ammo`（写 scratch 候选表） |
| `pacing.py` | 时长无关预算缩放（`Pacing` 值对象）+ endgame 收尾窗口 + `normalize_ctf_config`（`config.ctf` 写入口归一化） | `ChatTurn._pacing_context()` → 注入 `_challenge_context()`；`core/projects.py::update_config` |
| `scheduler.py` | **放弃决策**：13 条可解释判据 → `revive`/`requeue`/`drop` + 人话理由 | `core/ctf/session.py::CtfSession.plan/record` |
| `lease.py` | **方向互斥**：子目标 → 归一方向键 → 单持有者 TTL 租约 | `CtfSession.claims`（`plan` 里认领、`record` 里释放） |
| `attest.py` | **目标机自证**：nonce + sha256 挑战，判「执行原语是真的吗」 | `CtfSession.record`（识别执行声明 → 出题 → 下轮回执核验） |
| `fold.py` | **上下文降级**：四级阶梯（full→compact→salient→headline）+ 必保段 + 中文 token 估算 | `CtfSession.folded_context()` |
| `session.py` | **四者合流**：方向准入（含**无条件首轮分诊闸**）→ 派发计划 → 轮次记账 → 复核视图（分诊结论置顶 + **跨回执矛盾警示** + 耗时列） | `core/chat/runtime.py::_tool_ctf_dispatch` / `_tool_ctf_review`（`ctf-controller` 主控专用） |

## 配置写入口

`config.ctf = {deadline, tasks}` 由 `pacing.normalize_ctf_config` 归一（`core/projects.py::update_config` 调用，非法 422，空段剥键回落阶梯推断）。前端入口 = 黑板「CTF-Agent」tab 的「赛制时间」面板（`webui/src/views/Blackboard.tsx::CtfAgentPane`，`api.patchProjectConfig(pid, {ctf})`）。**改读路径（`_pacing_context`）必须同步改归一化**——否则非法值静默进盘、`int()` 吞成 0 后悄悄回退锚定题量，人看不出配置没生效。

## 纪律（改这里之前先读）

- **不做进程级单例**：`pacing.Pacing`、`lease.ClaimTable`、`session.CtfSession` 都是**值/实例对象**。多题并发下 A 题的 elapsed 与 A 会话的方向占用不能影响 B——任何 `global`/模块级可变状态都是缺陷。
- **只产候选/判定、不做动作**：`ammo` 不执行爆破、不发网络请求；`observe` 不写黑板；`flagjudge` 不提交 flag；`scheduler`/`attest`/`fold` 不派工。是否用、用多少条、写不写库由工具层与提示词纪律决定。
- **`observe.extract_flags` 只提示不判定**：它复用 `flagjudge.confident_flags` 三道闸列「flag 候选」段，但**不改任何状态**——是否算解出由 `challenge_flags` 台账（唯一真值）决定。改它别顺手去调 `record_flag`。
- **`_clean_twin` 不洗诱饵前缀**：`fake_flag{deadbeef99}` 截成 `flag{deadbeef99}` 会白送 +3 前缀分绕过三道闸（实测踩过）。孪生只处理**中性上下文**（`theflag{}`/`picoCTFflag{}`），`is_bait_prefix` 为真时直接返 None。
- **宁松勿误判的边界**：`roundtrip_verify` 遇到不认识的变换名返回 `None`（无法判定），不是 `False`；`observe` 抽不出信号返回空串，让回执保持原样；`attest.scan` 的 `no_target_vector` 是 inconclusive 不是 reject（交互式 shell 看不到载体，硬判会误杀真战果）。**硬判只留给明确违规**（诱饵/自然语言内层/跨题串号/摘要只在自述里）。
- **反伪造口径**：「模型说自己证明了不算，证据链说了算」。`FLAGISH_FLOOR=2.0`、`MIN_INNER_LEN=3`、`DIFF_LINE_MIN=4`、`attest.DIGEST_HEX_LEN=16` 这些阈值改动等于放宽闸门，须同时改测试与 DESIGN。
- **`requeue` ≠ `drop`**：前者是「这题还有救，但先让别人上」（30 分钟 hungry 让位），后者是「这题没救」（60 分钟零产出上限 / 8 轮封顶）。合并它们会让前端与复盘分不清「暂时让位」和「判定死亡」——`Verdict.frees_slot` 两者都真，但 `should_continue` 都假，唯一区分是 `action`。
- **方向键归一宁可漏判不可误判**：`lease.direction_key` 只做确定性字面归一（剥动作词/填充词/噪声、同义映射、复数归一、排序去重），**不做模糊合并**。「文件上传绕过 WAF」与「文件上传漏洞」必须不同键——误锁死并发（一个方向永远没人做）比重复劳动（浪费一个槽位）贵得多。
- **派发文本必须先剥壳再算键（2026-10-20 事故）**：`_task_text()` 给 worker 的 task 带【方向】/【要求】模板，**不能直接喂 `direction_key()`**——模板里的「只做这个方向」「结论以 [命中] 开头」「用 ctf_submit_flag 登记」全被当成方向词，于是「计划里给的 task 原样派发」100% 被 `allowed()` 判成越权，`ctf-controller` 完全不可用。统一入口 `session.key_of()`（先 `subgoal_of()` 剥壳）；`plan`/`allowed`/`record`/`_resolve_key` **四处必须同一口径**，任何一处单独用 `direction_key()` 都会让 state 键、租约键、记账键三者分叉。
- **`allowed()` 要同时看计划与当前状态**：一轮并发派多个 worker，先返回的那个在 `record()` 里把方向判死时 `_last_plan` 仍是旧的；只查 `plan.keys` 等于「判死了还在派」。`requeue` 必须放行（让位不是判死），只拒 `dropped`/`solved`。
- **`_resolve_key` 多义不猜**：方向族匹配（`⊆`）命中多个 state 时按新方向处理，宁可不记账也不把结果记到错的题上。`_resolve_key_locked` 供已持锁的 `_record_locked` 调用——`self._lock` 是普通 `Lock`，**不可重入**，包装层再加锁会死锁。
- **`fold` 的必保段与强信号不可砍**：`mandatory=True` 段（flag 台账）任何级别都不消失；强信号条目（凭据/flag/执行）在 headline 级也保留。砍掉它们等于让主控忘记已拿几分、下一轮重新去扫目录，反而更费 token。
- **`fold.estimate_tokens` 不能照抄 `len//4`**：那是英文口径，中文一字约一 token。按 `len//4` 估会把中文投影低估 3-4 倍，预算形同虚设——降级永不触发，超窗照样发生。
- **证伪是结论不是进展（2026-10-20）**：`[未命中] … 该方向证伪` 里有「命中」二字，旧口径被 `_WEAK_PROGRESS_RX` 判成弱进展 → 方向反复被 `bounded_patience` 续投，**证伪永远不收敛**。`record()` 里 `_REFUTED_RX` **必须先于**弱进展匹配，命中即置 `refuted` 且**不置 `weak`**（顺序反了就是原 bug）。`scheduler.decide` 的 ⑧'「已证伪 → drop」必须在 ⑨ `min_passes` 地板**之前**——放在地板之后等于「未命中也要再试两轮」。唯一例外：`correct > 0` 的多 flag 题不因单方向证伪 drop（整题还有戏）。
- **首轮无条件分诊（2026-10-20，P0-5）**：分诊未得出结论且 `rounds <= _TRIAGE_MAX_ROUNDS` 时，整批方向若无一条命中 `_TRIAGE_RX`（`分诊|triage|题型|识别/判断/确认+是|魔数|题面|file|载体`）→ **整批拦下**（`plan.triage_blocked`）。不能只看轮次：旧实现第 1 轮分诊完成后，第 2 轮仍因 `rounds <= 3` 被拦，且分诊方向按通用 scheduler 保持 `open`，正是「分诊续接又被硬拦」事故。`triage_concluded()` 只认明确载体/格式结论（pcapng/linktype/ZIP/USB/图片/音频/无附件/纯交互等），`[未命中] file 不存在` 不解锁；结论后状态置 `retired`（使命完成，不代表题目已解出）、释放租约、立即解除闸。三条硬约束：① **宽进**；② **上界是安全阀**，3 轮后必放行；③ **被拦不消耗槽位**。
- **`_fmt_dur` 只做粗粒度**：`45s`/`12m`/`1.4h`，负值一律 `0s`（不许输出 `-5s`）。耗时是**可观测性**，绝不能顺手当判据——时间判据只在 `scheduler` 里。
- **展示一律用 `dir_label(st)`，不许直接展示 `st.key`**（2026-10-20）：键是 `direction_key()` 的 2-gram 归一串，中文会切出跨词碎片（「先分诊：file 魔数…」→ `file 先分 关键 分诊 判断 断载 载体 键句 面关 题型 题面 魔数`），模型认不出那是自己刚派过的方向，会当「另一套版本」重复派工。键只用于去重/互斥，不给人看。`ClaimResult.describe(label)` 同理（不传 label 回落键）。回归 `test_dir_label_uses_original_text` / `test_render_table_shows_original_text_not_key`。
- **跨回执矛盾只报不判（2026-10-20，P2）**：`linktype_conflicts()` / `duplicate_artifacts()` 都是**可观测性**，不许改判据（`test_p2_signals_do_not_change_verdict` 钉死）。两条判据都是「≥2 个不同值/方向」——单条回执语法完全合法，矛盾只在**并置**时出现。
  - **linktype 不认裸数字**：`0x000000dc` 可能是任意偏移量，误报会把正常回执判成矛盾，而**假警报会训练模型忽略真警报**。只认显式声明（`linktype 220` / `Encapsulation type: … (220)` / `LINKTYPE_XXX (249)` / `link_type=249`）；间隔上限 80 字符（tshark 标准措辞中间有 46 字符，40 会漏掉最常见那条）。**代码不替模型判谁对**——它不知道哪个值是真的，自动挑一个等于赌。
  - **重复产物不能按名字也不能按语义类别判**：复盘里 `solve_usb_pcapng.py` / `usbpcap_extract.py` / `parse.py` 三个名字全不一样、语义词分属 solve/extract/parse 三档。最终判据退到最朴素的一条——**「≥2 个不同方向都写了脚本」本身就是方向重叠的强信号**（正常情况一个方向写完就解出来了）。同一方向写三版是正常迭代，不算。
- `flag{example}` 这类「前缀正常 + 内层占位」的形态**必须被判拒**（原版 hxbai 只看前缀会漏）——见 `is_bait_inner` / `_inner_too_short`。

## 测试

- `tests/test_ctf_flagjudge.py`（23）/ `test_ctf_observe.py`（40，含 8 例跨回执抽取）/ `test_ctf_ammo.py`（16）/ `test_ctf_pacing.py`（27）/ `test_ctf_scheduler.py`（30）/ `test_ctf_lease.py`（39）/ `test_ctf_attest.py`（35）/ `test_ctf_fold.py`（38）/ `test_ctf_session.py`（74，含 4 例证伪收敛 + 10 例首轮分诊/退役 + 4 例耗时 + 6 例跨回执一致性 + 3 例可读方向名）——本包纯函数全覆盖。
- 接线行为：`tests/test_ctf_submit_flag.py`（19，含 `run_cmd` 观测摘要附加与非 CTF 轨零变化）、`tests/test_ctf_ammo_tool.py`（14，含落盘/幂等/不泄 flag）、`tests/test_ctf_chat_pacing.py`（11，含 `_to_epoch` 两形态与 endgame 注入）、`tests/test_chat_ctf_controller.py`（45，含工具面双边同步、越权派发闸、**计划 task 原样派发**、无执行工具、**`ctf_inspect` 只给魔数不给全文**、首轮分诊端到端、冲突主动上报）、`tests/test_agent_repeat_guard.py`（8，原地打转提示，与 `observe` 共用 `_finish_cmd_receipt` 出口）。
- **接线测试要断言「输出里确实有东西」**（如 `scale=`、`[候选 N 条]`、`准派 N 个`），不能只断言「没抛异常」——`_pacing_context()` / `_ctf_session_for()` 被 `except` 吞掉静默返空串时，弱断言仍然全绿而功能已死。
- **键口径的测试必须走真实工具面**：直接 `sess.plan()` + `sess.allowed()` 测不到模板壳（测试自己传的是纯 subgoal），2026-10-20 那个「主控完全不可用」的 bug 就是这么全绿逃逸的。回归必须 `_tool_ctf_dispatch()` → 取 `_last_plan.granted[0]["task"]` → `_tool_call_expert()`，复刻模型复制粘贴的真实路径。

