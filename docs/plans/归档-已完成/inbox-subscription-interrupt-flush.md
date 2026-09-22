# 收件箱消费订阅声明化与中断落盘

> **状态**：已实施（2026-09-21。用户派活「开始补吧」，AskUserQuestion 圈定范围=订阅声明化＋中断落盘；全量回归 740/740，两处静默吞信与中断丢回复均以测试钉死）
> **关联代码**：`core/agent/loop.py`（四消费点 认领期:831 / 恢复期:927 / 对话轮:990 / 步边界:1519、notice 函数族 :1539-1700、`_chat_interruptible` :1146-1249、agent.chat 终稿落点 :1056 对话轮 / :1436 任务轮）、`core/blackboard/store.py`（`inbox_post` :444 / `inbox_drain` :475）
> **背景**：业界黑板对标（2026-09-21）可补项第一批。对标发现我方收件箱消费是「四处各赌各的」——四个调用点各自手工挑 kind 渲染，新增 kind 要改四处、漏一处就静默丢信；中断时半截输出直接丢弃。

## 拍板记录

| 决策点 | 结论 |
|---|---|
| 消费口径真相源 | 单张**订阅声明表**（kind → 场景元组）+ 单一 `_drain_inbox(scenario)` 入口；渲染顺序=表内声明序 |
| 恢复期漏 escalation_result | **补上**——现状恢复期全量 drain 但渲染五种唯独漏 esc，H3 批准回执暂停期间送达则恢复即被标记已读且永不显示，真丢失 bug |
| 认领期 basis_stale 被 stale_refs 非空门控 | **放开**——撤回路由同时挂任务标+发私信，但纯私信形态（任务行 stale_refs 已清）现状被吞；渲染条件与恢复期/步边界对齐（有内容就渲染），`_stale_alerted` 去重只对任务挂标部分生效 |
| run_chat 是否补 finding_update / basis_stale | **不补**（维持现状）——对话轮无任务上下文，强制三选一/发现增补注入了也无法响应；留收件箱红点，下轮认领期/步边界消费，不是丢失 |
| 未知 kind | **绝不静默吞**——四消费点全部改 only_kinds（从订阅表构造）；未登记 kind 滞留收件箱红点可见，新增 kind 必须先登记订阅表 |
| 中断落盘范围 | 对话轮（text_acc 现成）+ **任务轮叙述也攒**（on_text 恒传、只攒不发 delta）；thinking 残留 delta 维持现状（中断现场审计，不落终稿，2026-09-19 定稿不翻） |
| 中断落盘形态 | `agent.chat` 终稿 + `interrupted: true` 标记；对话轮带 stream_id 并清剪 delta（镜像成功路径）；任务轮带 step、文本截 2000（同终稿口径） |
| store 层改动 | **无**——`inbox_drain` 的 only_kinds 现成；`exclude_kinds` 参数保留（store API 通用过滤，测试覆盖），loop 侧不再使用 |

## 一、问题

### 1.1 收件箱消费四处各赌各的

现状矩阵（kind × 消费点）：

| kind | 认领期 :831 | 恢复期 :927 | 步边界 :1519 | 对话轮 :990 |
|---|---|---|---|---|
| basis_stale | ✅（门控 stale_refs 非空+未 alerted） | ✅ | ✅ | ❌ |
| finding_update | ✅ | ✅ | ✅ | ❌ |
| escalation_result | ✅ | ❌ **吞** | ✅ | ✅ |
| human_note | ✅ | ✅ | ❌（轮末语义，exclude_kinds） | ✅ |
| agent_message | ✅ | ✅ | ✅ | ✅ |
| task_receipt | ✅ | ✅ | ✅ | ✅ |

三处不一致：
- **恢复期吞 escalation_result**（真丢失）：恢复期全量 drain、渲染五种唯独漏 esc。
- **认领期吞纯私信 basis_stale**（真丢失）：渲染被 `if stale_refs and ...` 门控，任务挂标已清时私信形态被消费且不渲染。
- **口径三分裂**：全量（认领/恢复）vs exclude（步边界）vs only 白名单（对话轮）。新增 kind 必须同步四处，漏改即吞。

### 1.2 中断即丢半截回复

`_chat_interruptible` abort 置位即刻抛 `_StepInterrupted`：
- 对话轮：已流出的 `agent.chat.delta` 行留作审计但**没有终稿**——前端 turn 分组无收口回复，流式气泡悬死。
- 任务轮：叙述的半截彻底丢弃——`on_text` 只在对话轮传（stream_text=True），任务轮 text_acc 永远空。

dsh 对标结论（2026-09-21）：中断输出应落盘。

## 二、设计：订阅声明化

### 2.1 订阅表（唯一真相源）

```python
# ■ 收件箱订阅声明（2026-09-21）：kind → 消费场景。四处消费点（认领期/恢复期/
# 步边界/对话轮）统一从 _drain_inbox(scenario) 取信，渲染顺序=表内声明序；
# 新增 kind 必须先在此登记，未登记的私信滞留收件箱红点可见，绝不被静默吞。
_INBOX_SUBSCRIPTIONS: dict[str, tuple[str, ...]] = {
    "basis_stale":       ("claim", "resume", "step"),         # 强制三选一：须任务上下文，对话轮不消费
    "finding_update":    ("claim", "resume", "step"),         # 发现增补与任务现场相关，对话轮不消费
    "escalation_result": ("claim", "resume", "step", "chat"),  # H3 批准回执（恢复期补齐，原漏）
    "human_note":        ("claim", "resume", "chat"),         # 轮末语义：不在步边界打断（§3 轮末注入）
    "agent_message":     ("claim", "resume", "step", "chat"),
    "task_receipt":      ("claim", "resume", "step", "chat"),
}
```

场景名：`claim`（run_task 认领期）/ `resume`（快照恢复期）/ `step`（`_control_point` 步边界）/ `chat`（run_chat 空闲对话轮）。

### 2.2 单一入口 `_drain_inbox`

```python
def _drain_inbox(self, scenario: str, *, stale_refs: list[str] | None = None
                 ) -> tuple[list[tuple[str, str]], list[dict]]:
    kinds = tuple(k for k, scs in _INBOX_SUBSCRIPTIONS.items() if scenario in scs)
    drained = self.bb.inbox_drain(self.project_id, self.session["id"], only_kinds=kinds) if kinds else []
    out: list[tuple[str, str]] = []
    for kind in _INBOX_SUBSCRIPTIONS:              # 固定声明序
        if kind not in kinds:
            continue
        if kind == "basis_stale":                  # 特例：任务挂标现场补水 + 收件箱行合并渲染
            notice = self._basis_stale_notice(stale_refs or [], drained)
        else:                                      # 其余五种恰为 _<kind>_notice 命名约定
            notice = getattr(self, f"_{kind}_notice")(drained)
        if notice:
            out.append((kind, notice))
    return out, drained
```

返回 **(带 kind 标签的 notice 列表, 原始 drained 行)**——后者供 run_chat 提取 human_note 正文（note_text）与认领期 `_stale_alerted` 判定。

### 2.3 四消费点改写

- **认领期**：`notices, drained = self._drain_inbox("claim", stale_refs=stale_refs)`；逐条 append；`_stale_alerted` 判定改为「stale_refs 非空且本次渲染出了 basis_stale notice → add」——纯私信形态正常渲染、不进去重。
- **恢复期**：`notices, _ = self._drain_inbox("resume")`——esc 漏消费由此修复。
- **步边界**：`notices, _ = self._drain_inbox("step")`——human_note 排除由订阅表表达，exclude_kinds 调用退役；drain 从全量改 only_kinds（已知 kind 行为不变，未知 kind 从「吞」变「留」）。
- **对话轮**：`notices, drained = self._drain_inbox("chat")`；parts 空返 None 逻辑不变，note_text 从返回的 drained 提取。

### 2.4 与 DESIGN §3.四 的对齐

DESIGN 表述「信息式 4 kind：认领期+恢复期+步边界+run_chat」与代码现状本就不符（run_chat 白名单四 kind 不含 finding_update）——本设计以**订阅表为准**，回写 DESIGN 时修正为场景矩阵表述：各消费点只消费「该场景能响应」的 kind。

## 三、设计：中断落盘

### 3.1 挂点

`_chat_interruptible` 两处 abort 抛出点（轮询环 :1224、err+abort :1236）抛前调 `_flush_interrupted_reply`：

```python
def _flush_interrupted_reply(self, stream_id: str, text_acc: list[str], step: int | None) -> None:
    """■ 中断落盘（2026-09-21）：abort 时半截回复不再丢弃——text_acc 有存货就落
    agent.chat 终稿（interrupted=true）。对话轮带 stream_id 并清剪 delta（turn 有
    收口回复）；任务轮带 step、截 2000 同终稿口径。thinking 残留 delta 维持现状
    （被中断思考的现场审计，不落终稿）。"""
    text = "".join(text_acc).strip()
    if not text:
        return
    payload: dict[str, Any] = {"session_id": self.session["id"], "interrupted": True,
                               "text": text if step is not None else text}
    if step is not None:
        payload["step"] = step
        payload["text"] = text[:2000]
    else:
        payload["stream_id"] = stream_id
    self.bb.append_event(self.project_id, "agent.chat", payload,
                         session_id=self.session["id"], author=self.session["id"])
    if step is None:
        try:
            self.bb.prune_chat_deltas(self.project_id, stream_id)
        except Exception:  # noqa: BLE001 —— 只多留过渡行
            log.exception("agent.chat.delta 清剪失败 stream_id=%s", stream_id)
```

`_chat_interruptible` 加 `step: int | None = None` 参数：`_loop_body` 调用处传当前步号、run_chat 调用处不传（payload 无 step → 前端 turn 收口为回复气泡）。

### 3.2 任务轮叙述攒缓冲

`_worker` 内 `on_text` 从「仅 stream_text=True 才传」放开为 **stream_capable 分支内恒传**：任务轮只攒不发——`_flush_text` 顶部加 `if not stream_text: return` 守卫，任务轮零 `agent.chat.delta` 事件（爆炸半径不变）。传输层自 2026-09-19 起就因 on_thinking 走流式（`AnthropicCompatProvider.stream_capable=True`，anthropic_compat.py:104），无新增传输风险。

### 3.3 范围边界（现状保持项）

- thinking 残留 delta 维持现状（中断现场审计，不落终稿）。
- 非 stream_capable / 非流式降级轮中断：text_acc 空，无落盘（无内容可捞）。
- 截断重试轮 text_acc 照旧清零（半截参数流是垃圾，不捞）。
- 完整响应已到但被 run_chat 顶部 guard 丢弃（abort 晚到毫秒窗）：维持丢弃，不落。
- LLM 异常（非 abort）路径不落——进待打磨。

## 四、实施切分

1. loop.py：订阅表 + `_drain_inbox` + 四消费点改写（含两处丢失修复）
2. loop.py：`_chat_interruptible` 加 step 参数、on_text 恒传、`_flush_text` 守卫、`_flush_interrupted_reply` + 两处挂点；两个调用点分别传/不传 step
3. 测试（见五）+ 全量回归
4. 回写 DESIGN.md §3（注入矩阵→订阅表口径；「中断即丢回复」→中断落盘）+ core/agent/CLAUDE.md + 方案文档头部改「已实施」

## 五、测试清单

test_agent.py 新增：
- `test_resume_drains_escalation_result`：恢复期场景 esc 私信被渲染（钉死丢失修复）
- `test_claim_renders_pure_inbox_basis_stale`：stale_refs 空但收件箱有 basis_stale 私信 → 认领期渲染
- `test_unknown_inbox_kind_not_swallowed`：未登记 kind 经认领期/步边界后仍未读
- `test_chat_scenario_leaves_task_kinds_unread`：run_chat 消费 human_note、finding_update 留收件箱
- `test_interrupted_reply_flushed_in_chat`：对话轮流式中 abort → `agent.chat{interrupted:true, stream_id}` 落库 + delta 清剪 + run_chat 返 None
- `test_interrupted_narrative_flushed_in_task`：任务轮叙述流式中 abort → 任务 fail「人工中断」+ `agent.chat{step, interrupted:true}` 落库 + 无 delta 行
- 既有 `test_run_chat_aborts_and_skips_when_not_idle`（:1607 断言「无 agent.chat」）兼容性确认：非流式剧本中断时 text_acc 空、flush no-op——断言应保持绿

存量行为翻转核对：任务轮 `llm.calls` 的 `stream_text` 标志 False→True（ScriptedLLM :36 记录）——grep 确认无测试断言该标志。

## 六、待打磨清单

- [ ] 前端 EventRow/turn 分组给 `interrupted: true` 的 agent.chat 加「已中断」标记（本批不做，后端先行；无标记时中断回复照常渲染为气泡/过程行）
- [ ] LLM 异常（连接断等非 abort）路径的半截落盘——可复用 `_flush_interrupted_reply`
- [ ] 完整响应已到但被 run_chat 顶部 guard 丢弃的场景是否落盘（abort 晚到毫秒窗）
- [ ] `_stale_alerted` 生命周期（会话级 set，重启即清）——现状重启后同任务重认领会重告警，可辩护（重启后上下文也丢了），暂不动
