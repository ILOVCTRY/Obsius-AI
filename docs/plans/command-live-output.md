# 命令实时输出：运行中命令的直播终端

> 实战盲区（2026-09-25 三次「会话无进度」排查实锤）：`run_cmd` 同步阻塞、
> 命令 stdout 只在进程结束后随 `command.result` 落库。一条 9 分钟的模块枚举
> 在前端全程只有「（无结果返回）」，用户无法区分「正在跑」与「卡死」。
> 让长命令的输出**按行实时上屏**，结束后收口为现有最终 IN/OUT。

- **状态**：已定稿（2026-09-25 用户拍板 D1-D3；D4-D8 为工程默认项，待排期实施）
- **拍板记录**：见 §2 决策表
- **关联代码**：
  - `core/runtime/backends.py`（`_execute_piped` line 56：三后端共用的唯一排水/轮询点——唯一注入点）
  - `core/runtime/gateway.py`（`run` line 105；command 事件 line 197、command.result line 232）
  - `core/blackboard/store.py`（`prune_thinking_deltas` line 906 / `prune_chat_deltas` line 917——清剪先例；`recent_events` line ~855）
  - `webui/src/views/LiveRoom.tsx`（pair 装配 line 764-829：按 call_id 单趟扫描）、`webui/src/views/live/EventRow.tsx`（`CommandPairRow` line 110、`StreamItem` line 13）、`webui/src/lib/events.tsx`（kind 标签映射）

## 1 背景与设计约束

执行链路现状：Agent → `run_cmd` → `gateway.run()` → backend `execute/run_once` →
`_execute_piped`（daemon 线程把 stdout/stderr 全量吃进 list，主线程 poll 等退出）
→ 返回 ExecOutcome → `command.result` 事件（stdout_head/stderr_head 各截 2000）。
输出在命令**结束的那一刻**才第一次可见。

三次排查的结论一致：命令都在正常跑（543s/390s/进行中，exit 0），不是平台 bug；
盲区是**可观测性**缺失。设计约束：

1. 黑板所有写操作经 store 单一入口——delta 事件也走 `append_event`，不新建写库路径。
2. 审计表最终必须干净：中间态事件在终稿后清剪（同 llm.thinking.delta 定稿模式）。
3. 单进程部署；WS 通道是「1s 轮询 recent_events → 推送」，不引入第二推送协议。
4. 非 TTY：docker run / wsl / host 子进程均无 TTY，不为此伪造 TTY（见 D7）。

## 2 设计定稿

### 2.1 决策表

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 传输通道 | **路线 A：落库 + 清剪**。新增 `command.output` 事件（payload 见 2.2），经现有「store.append_event → 1s 轮询 → WS → useEvents」通道推送，**API/WS 层零改动**；`command.result` 落地后照 `prune_thinking_deltas` 先例清剪同 call_id 的 delta。理由：断线重连（WS since_id 游标回放拼齐）、刷新、多观众、REST 回看全部自动正确；项目已有同构模式与测试约定。路线 B（进程内 broker 直发不落库 + ring buffer）的亚秒延迟与零 db 写在此场景无实际需求，已否决 |
| D2 | 收尾语义 | **替换为最终 IN/OUT**。`command.result` 到达后 LIVE 区不再渲染，展开只显示现有 stdout_head/stderr_head OUT 块；delta 行随即被 prune，刷新后表里不留中间态。实时终端是「运行中」的临时视图，不是审计产物 |
| D3 | 展示时机 | **展开命令时才显示**。折叠行维持单行摘要不挂实时内容；展开一条无 result 的命令时，在 IN 块下方挂 LIVE 终端区。不做运行中自动展开、不抢滚动焦点 |
| D4 | 行重组与节流 | `_drain` 的 `for chunk in pipe`（text=True 时 TextIOWrapper 迭代）**天然按行**（末行无 `\n` 也在结束时吐出）——行重组零逻辑。flush 双阈值：**距上次 flush ≥1.0s 或缓冲 ≥4096 字符**；无输出则不触发（不设独立定时器，没东西就没必要推）。命令结束**不做 final flush**：残留尾行由立刻到达的 command.result 承载（端到端多停 1-2s 后直接换最终结果），实现更简 |
| D5 | delta payload 形态 | **只带新增片段，不累计全文**：`{call_id, seq, out, err}`——out/err 为自上次 flush 的新增文本（可一边为空串），seq 为命令内单调序号。体积小；断线错过的片段由 WS 重连的 since_id 回放补齐，晚挂载靠事件流已加载部分（见 D8） |
| D6 | 容量护栏 | 单命令 delta **硬上限 2000 条**（≈33 分钟满速 1 条/秒）：超出后停发，最后一条带 `truncated:true`；result/prune 照常。防异常程序刷屏灌爆 events 表。前端 LIVE 区只渲染**累计尾 32KB**（拼接时裁头并标注「仅显示尾部」）——看实时运行关心的是尾部 |
| D7 | TTY / 缓冲 / ANSI | **不上伪 TTY**：`docker run -t` 会把 stdout/stderr 混成一股、`\r` 进度条与作业控制需专门处理、且改变信号语义。非 TTY 全缓冲问题靠纪律与镜像环境变量：①pentest-box Dockerfile 加 `ENV PYTHONUNBUFFERED=1`（镜像内 python 一律行缓冲，随本方案重建镜像）；②`python3 -u` / `stdbuf -oL -eL` 写进 run_cmd 工具描述引导；nmap/ffuf/sqlmap 等本就逐行写 stderr 不受影响。M1 对输出**剥离 ANSI 转义**（小正则 stripAnsi）不渲染颜色；真着色 M2 |
| D8 | 晚挂载语义 | 命令跑了一半才打开/展开：M1 LIVE 区只拼事件流已覆盖的片段（tail 50 + hydrate 300 窗口内的 output 事件）= 看到尾部实时，语义成立。M2 再评估是否加「按 call_id 查 output 历史」的专用回放（运行中事件尚未 prune，数据都在） |

### 2.2 事件契约

```
command.output（每次 flush 一条，session_id/author 随 command 同源）
{
  "call_id": "cmd-xxxxxxxxxxxx",
  "seq": 3,                 // 命令内 1 起单调
  "out": "new lines...",    // 自上次 flush 的 stdout 新增；无则 ""
  "err": "",                // 自上次 flush 的 stderr 新增
  // 仅第 2000 条："truncated": true
}
```

不新增事件订阅白名单/不触发任何编排逻辑（output 不进 inbox、不唤醒、不算探索信号——
D9 卡死预检读的是 command/command.result，口径不变）。

### 2.3 关键机制

**采集链**：`_execute_piped(proc, timeout, abort_event, on_output=None)`——
`_drain(pipe, sink, stream)` 在 `sink.append(chunk)` 后调
`on_output(stream, chunk)`（stream ∈ `"out"/"err"`；回调异常吞掉，排水绝不反向影响
执行）。三 backend（Native/WSL/Docker）透传同一参数，一处改动全覆盖。

**收集器（gateway 每命令一个 OutputCollector）**：

```
write(stream, chunk):           # drain 线程调，GIL 下 list.append 原子
    buf[stream] += chunk
    if 总字节 >= 4096 or now - last_flush >= 1.0: flush()
flush():
    if 两 buf 皆空 or 已发 >= 2000: return
    append_event("command.output", {call_id, seq: ++n, out, err})
    清空 buf、last_flush=now
```

命令结束后 collector 自然废弃（不 final flush，D4）。

**清剪**：`command.result` 的 append_event **之后**立刻调
`bb.prune_command_output(project_id, call_id)`，失败只 log 不影响结果。
中断（abort）/超时路径：result 照常落、prune 照常执行（实时区随超时结果收口）；
**BackendError / GatewayDenied 路径不调**——此时无 output 事件（Popen 失败先于排水），
若未来有残留 delta = 命令异常死亡的现场审计，同 thinking 中断语义。

## 3 实施切分

### M1 实时输出闭环（建议一次落地）

1. `core/runtime/backends.py`：
   - `_execute_piped` 加 `on_output: Callable[[str, str], None] | None = None`；
     `_drain` 加 stream 参数，append 后回调。
   - `NativeBackend.execute/_run`、`WSLBackend.execute`、`DockerBackend._docker/run_once/exec_in`
     签名加 on_output 透传（默认 None，现有调用方零改动）。
2. `core/blackboard/store.py`：新增
   ```python
   def prune_command_output(self, project_id: str, call_id: str) -> int:
       """命令实时输出清剪：command.result 落库后删同 call_id 的 command.output
       过渡行——语义同 prune_thinking_deltas；拒单/后端异常路径不调。"""
       with self._tx():
           cur = self.conn.execute(
               "DELETE FROM events WHERE project_id=? AND kind='command.output'"
               " AND json_extract(payload,'$.call_id')=?",
               (project_id, call_id))
           return cur.rowcount
   ```
3. `core/runtime/gateway.py`：
   - 实现 OutputCollector（2.3）：1.0s/4096 双阈值、2000 条硬上限（末条 truncated）。
   - 仅当 `self.bb and project_id` 时构造（无黑板链路=纯后端调用，回调置 None）；
     `_dispatch` 加 on_output=collector.write。
   - command.result 落库后调 `prune_command_output`（try/except log）。
4. `tools/pentest-box/Dockerfile`：加 `ENV PYTHONUNBUFFERED=1`；
   `scripts/build_pentest_box.py` 重建镜像（tag 不变 0.1）。run_cmd 工具描述补
   「要看实时输出：python3 -u / stdbuf -oL -eL」一行。
5. 前端：
   - `lib/events.tsx`：kind 映射加 `command.output`（muted 兜底标签「⋯ 实时输出」，
     仅孤儿行会走到；正常被 pair 吸收）。
   - `views/LiveRoom.tsx` 装配段：`command.output` 按 call_id 找到 pair（out 或
     turn.process，复用现有 call_id→位置索引），把 out/err 按 seq 累加到
     pair 上新增字段 `live?: { out: string; err: string }`（尾裁 32KB，
     out/err 各自裁并置 truncated 标记）。StreamItem pair 类型加 `live?`。
   - `views/live/EventRow.tsx`：`CommandPairRow` 加 live 参数——展开且
     **result 不存在**且 live 非空：IN 块后挂「LIVE」区（out pre + err pre，
     max-h-64 内滚，stripAnsi 后渲染；ref 贴底自动滚）；live 空维持
     「（无结果返回）」。result 存在完全不渲染 LIVE（D2）。
   - stripAnsi 小工具放 lib（覆盖 CSI `\x1b\[...[@-~]` 与 OSC `\x1b\].*?\x07`
     两类常见序列）。
6. 测试：
   - `_execute_piped` 回调：`echo a; echo b >&2` → 顺序断言 ("out","a\n")、
     ("err","b\n")；回调抛异常不影响执行结果。
   - gateway 链路：慢速脚本（echo+sleep）跑在 Fake/真 backend → command.output
     事件带同 call_id/递增 seq/out 片段；result 落库后 events 中无 command.output。
   - 4096 阈值：一次输出 >4KB 不等 1s 即 flush（多条 delta）；2000 上限：
     满速刷超限后停发且末条 truncated:true。
   - store `prune_command_output`：两条 call_id 混杂只删目标。
   - 回归：全量 pytest + `npm run build` 零 TS 错误。
7. 手工冒烟矩阵：① docker 长命令（nmap/sqlmap 或 `for i in $(seq 1 60); do echo $i; sleep 1; done`）
   展开后逐秒上屏、贴底滚；② 结束瞬间 LIVE 换最终 OUT；③ 中途断线重连不错行；
   ④ host/wsl 各跑一次；⑤ 含 ANSI 的输出（`grep --color`）无乱码；⑥ 异常刷屏
   程序在 2000 条处止住不灌库。

### M2 后置（按实战反馈排期，不挂账）

1. ANSI 真着色：转义序列映射主题色渲染（M1 只剥离）。
2. 晚挂载专用回放：展开运行中命令时按 call_id 拉 output 历史，从头还原（D8）。
3. LIVE 区用户上滚时暂停贴底（M1 强制贴尾）。

### 明确不做

- 伪 TTY / pty 模拟（\r 进度条、作业控制、交互式 shell 直播）。
- 进程内 broker 直发（路线 B，已否决）。
- delta 全文永久保留（终稿必清剪）。
- 运行中自动展开 LIVE、自动切换页签。

## 4 容量与风险

- 负载：9 分钟 1 行/秒 ≈ 540 条事件，payload 仅新增行（几十字节/条），全命
  <100KB；1 次/秒 WAL 写，低于 thinking.delta 已验证负载。WS/useEvents 的
  100ms 批 flush 合批无压力。
- 端到端延迟：排水 chunk（行结束即达）+ 最多 1s flush + 1s WS 轮询 = **1-2s**，
  对命令执行场景足够。
- 风险点：①on_output 回调跨线程（drain→collector，append 原子 + 不抛异常）；
  ②prune 与 WS 推送竞态——delta 推给前端后被删不影响（前端已持快照，result
  到达即收口）；③非 TTY 全缓冲程序 LIVE 区不动是程序行为非平台故障，靠 D7
  纪律与 PYTHONUNBUFFERED 缓解，UI 不做假进度。
