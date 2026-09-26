# 策略顾问配置前端设置 UI（stuck-convergence D10）

- **状态**：**已实施（2026-09-24，P1-P6 全量）**，归档至 `归档-已完成/`
- **实施修正**：`normalize_advisor({})` 返回 `{}`（剥键），非空 dict 才缺键补默认——如 `{"junk":1}` 仍归一为全默认。全量回归 1023 passed（基线 1011 +12）。
- **关联代码**：
  - `core/agent/loop.py`：`AgentConfig`（:154-176）、卡死分支（:1520-1540）、ToolDispatcher 构造（:432-444）
  - `core/agent/tools.py`：`ToolDispatcher.__init__`（:850-910）、`_closing_gate`（:1818 起）
  - `core/api/app.py`：`_registered_session_factory`（:1355 起）、PATCH config（:1616 附近）
  - `core/projects.py`：`ProjectStore.update_config`（:215-248）
  - `core/autonomy.py`：归一化函数（`normalize_rule_profiles` 等同模式）
  - `webui/src/views/SettingsView.tsx`：10 tab 结构（现 9 tab，:174-203）

## 0. 拍板记录

| 决策点 | 结论（2026-09-24 用户拍板） |
|--------|------------------------------|
| 暴露字段 | 四项全暴露：观察窗步数 stuck_after、D9 静默延长上限、D6 收尾确认轮上限、顾问模型项目级覆写 |
| 顾问总开关 | **不做**——维持 D8 撤回决定；waves≥2 机械硬闸是不可关闭的最终安全网 |
| 作用域 | **项目级**：新增 `config.advisor` 段，经现有 PATCH /api/projects/{pid}/config 写入，与 autonomy / rule_profiles 同模式 |
| 生效时机 | 工厂/会话构造期读取，**在跑会话不生效，下次新建会话窗生效**（UI 必须注明） |
| 不暴露项 | waves≥2 机械硬闸阈值（最终安全网）、`dry_tail_steps`（`stuck_after-2` 派生值） |

## 1. 背景

卡死/顾问机制（D1 波次链 + D6 收尾确认 + D7 裁决 + D9 活跃探索静默延长）全部参数当前为代码内缺省（`stuck_after=12`、D9 延长上限 2、D6 收尾轮 2、planner 跟随全局模型），人类无法按任务特征调节——例如逆向类长任务多的项目想放宽观察窗，快速巡检项目想更早介入。本方案把四个顾问参数下放到项目设置页，由人类按项目手动调节。

## 2. 设计

### 2.1 配置形态

- 项目配置顶层新增 `config.advisor` 段，经现有 `PATCH /api/projects/{pid}/config` 端点写入，`project.json` 与黑板 projects 行双写（复用 `ProjectStore.update_config` 既有机制），**不新增端点**。
- **段内整段替换**（与 rule_profiles 同口径，不走顶层浅合并）；段缺省 / `null` / `{}` = 剥键恢复全部代码缺省。
- schema：

| 字段 | 缺省 | 范围 | 语义 |
|------|------|------|------|
| `stuck_after` | 12 | 6-30 | 连续 N 步无黑板写入进展→召唤顾问 |
| `stuck_max_extensions` | 2 | 0-4 | D9 活跃探索静默延长上限；0=关闭静默延长 |
| `closing_max_rounds` | 2 | 0-3 | D6 收尾确认轮上限；0=首次申报即放行 |
| `provider` | 无 | 已配置供应商名 | 顾问模型供应商；缺省=跟随全局 planner |
| `model` | 无 | 该供应商模型名 | 须与 provider 同时出现；缺省=供应商默认模型 |

范围理由：stuck_after 下限 6（再短正常复合动作也频繁被打断）、上限 30（再长真打转被长期掩盖、硬闸时间线过长）；延长 0-4（每延一个窗都受最终硬闸约束）；收尾轮 0-3（>3 则确认本身烧 token）。

### 2.2 已知语义边界（UI/文档须注明）

- `planner_llm` 在会话内有三个消费点：顾问建议（loop.py:2337）、顾问裁决（loop.py:2429）、**任务收尾沉淀复盘（loop.py:2714）**。项目级模型覆写三者同时生效，标签写作"顾问/复盘模型"。
- Orchestrator（app.py:4671）继续用外层未覆写的 plan_llm，编排器自身不吃覆写；其经工厂开的 AgentSession 窗才吃。
- `apply_context_budget`（loop.py:185）只按 executor llm 算会话预算，重建的 planner 不参与。

## 3. 实施切分

### P1 后端归一化（core/autonomy.py、core/projects.py）

- `core/autonomy.py` 新增（复用现有 `_bounded_int`，天然拒绝 bool）：
  ```python
  ADVISOR_DEFAULTS = {"stuck_after": 12, "stuck_max_extensions": 2, "closing_max_rounds": 2}
  ADVISOR_RANGES = {"stuck_after": (6, 30), "stuck_max_extensions": (0, 4), "closing_max_rounds": (0, 3)}
  ```
  新增 `normalize_advisor(raw)`：None/{}→{}（调用方剥键，对齐 normalize_rule_profiles）；非 dict→ValueError；三整数键缺键补默认、越界 ValueError（API→422）；`provider` 若在须非空字符串；`model` 若在须非空且与 provider 同时出现；未知键自动剥除。
- `core/projects.py` `update_config`（:236 rule_profiles 分支后）加 advisor 分支：仅当 PATCH 含 advisor 时归一化，结果非空写入、空则剥键。双写与内存 meta 同步走现有机制。

### P2 参数化后端执行链（core/agent/tools.py、core/agent/loop.py）

- **tools.py**：模块常量 `_CLOSING_MAX_ROUNDS=2`（:56）保留作默认唯一来源；`ToolDispatcher.__init__` 加参数 `closing_max_rounds: int = _CLOSING_MAX_ROUNDS`，存 `self.closing_max_rounds`；引用点 :1838、:1901/:1908 改实例值。
  - **cap=0 语义坑**：现有流程 round==0 先进干尾巴判定再进确认轮，cap=0 不会真"零确认"。必须在 `_closing_gate`（:1818）round==0 分支**最前面**加 `if self.closing_max_rounds <= 0: 落 cap_zero 事件; return None`（放在干尾巴判定之前）。
- **loop.py**：从 tools 导入 `_CLOSING_MAX_ROUNDS`；AgentConfig 在 stuck_after 后加 `stuck_max_extensions: int = _STUCK_MAX_EXTENSIONS`（常量 :66 保留）、`closing_max_rounds: int = _CLOSING_MAX_ROUNDS`；D9 引用 :1527 改 `self.config.stuck_max_extensions`；ToolDispatcher 构造（:432-444）透传 `closing_max_rounds=self.config.closing_max_rounds`。dry_tail 自动跟随 stuck_after 派生（stuck_after≥6 ⇒ dry_tail≥4）。

### P3 工厂消费 config.advisor（core/api/app.py `_registered_session_factory` :1355）

全部 6 个建窗点（:1465/2849/3289/3542/3978/4667）都经此工厂，改一处全覆盖。在内层 `factory()` 体内（cfg 已在 :1373 读取）：

- `adv = {**ADVISOR_DEFAULTS, **(cfg.get("advisor") or {})}`；AgentConfig（:1419）补传 stuck_after / stuck_max_extensions / closing_max_rounds。
- planner 项目级覆写（**必须在内层 factory()，不能放外层**）：
  ```python
  session_plan_llm = plan_llm
  if adv.get("provider") and plan_llm is not None:
      try:
          session_plan_llm = app.state.llm_store.build(adv["provider"], adv.get("model"))
      except Exception as e:
          log.warning("项目顾问模型覆写构建失败，回退全局 planner: pid=%s %s", pid, e)
          session_plan_llm = plan_llm   # 供应商后变更致坏值→静默回退，绝不开窗失败
  ```
  AgentSession 调用改 `planner_llm=session_plan_llm`。
- 保存时不硬校验 provider/model 存在（projects 层不依赖 ProviderStore）；供应商配置后来变更（删除/停用/删模型）→ 构建失败兜底恰好正确。

### P4 前端（webui/）

- 新建 `src/components/settings/AdvisorPane.tsx`，Props `{ pid?: string | null }`：
  - 无 pid：空态卡片"顾问配置按项目保存，请从具体项目内打开"（仿 GatewayPane pid 守卫）。
  - 数据：`api.getProject(pid)` 取 config.advisor；`api.models()` 取供应商/模型清单。pid 切换用 `let alive` 竞态守卫。
  - 三个数字行（Input type=number w-20，内联整数+区间校验，非法禁用保存）：观察窗步数（6-30）、静默延长上限（0-4，注 0=关闭）、收尾确认轮（0-3，注 0=申报即放行）。模板：BudgetPopover.tsx:117-192（数字校验）、RuleProfilesEditor（RulesPane.tsx:288 附近，本地 state+Saved 读写闭环）。
  - 模型覆写：供应商 select（首项 ""=跟随全局 planner）+ 模型 select（供应商空时 disabled，首项 ""=供应商默认模型）。
  - 保存（整段替换语义）：三值全缺省且无覆写→`patchProjectConfig(pid, { advisor: null })`（即"恢复默认"，必须显式发 null，不能靠省略）；否则发归一化整段。成功显 Saved，422 行内显 detail，保存后重拉确认。
  - 文案：下次新建会话窗生效；模型覆写同时作用于顾问建议/裁决与收尾复盘、编排器不受影响；第 3 轮机械硬闸不可关闭。
- `src/views/SettingsView.tsx`：tab 数组（:174-177）在 rules 与 llm 之间插 `["advisor","顾问"]`（9→10）；:196 后加 `<TabsContent value="advisor" ...><AdvisorPane pid={pid} /></TabsContent>`。
- `src/lib/types.ts`：加 `AdvisorConfig { stuck_after; stuck_max_extensions; closing_max_rounds; provider?; model? }`。api.ts 不改（patchProjectConfig 通用签名）。

### P5 测试（基线 1011，预期约 1022）

- tests/test_projects.py（仿 :243/:269 rule_profiles 用例）：①归一化矩阵（None/{}→{}、缺键补默认、边界合法、未知键剥除、provider/model 组合）；②错误矩阵（越界、bool/浮点、非 dict、model 无 provider、空串）；③update_config 双写一致、部分提交、非法 ValueError、null/{} 剥键。
- tests/test_agent.py（复用 ScriptedLLM/env/make_agent）：④`stuck_max_extensions=0` 演进命令仍叫顾问、无 extend；⑤上限=1 时 extend 仅 1 次、第 2 窗走顾问；⑥`closing_max_rounds=1` 序列放行行为；⑦`closing_max_rounds=0` 首次 complete 直接 done、pathway=cap_zero。
- tests/test_api.py（按 :581 内联模式自建 create_app 注入假 LLM，不触网）：⑧PATCH advisor 后开窗，断言 session config 三字段与 dispatcher.closing_max_rounds；⑨monkeypatch llm_store.build 返回标记对象，断言 planner_llm 被覆写、无覆写项目不变；⑩build 抛错时开窗仍 200 且回退；⑪Orchestrator 走外层 plan_llm 不吃覆写。

### P6 文档（实施落地时）

- DESIGN.md：卡死收敛段追加 D10 定稿（schema/范围、工厂消费、planner 重建与失败兜底、三消费点与 Orchestrator 边界、下次建窗生效）。
- CLAUDE.md（就地精简，≤80 行约束）：core/agent/CLAUDE.md（AgentConfig 两字段、D9 上限配置化、D6 cap=0）；core/api/CLAUDE.md（advisor 归一化+工厂透传，零增行）；core/CLAUDE.md（normalize_advisor）；webui/CLAUDE.md（10 tab、顾问条目）。
- docs/plans/stuck-convergence.md：决策表 D8 后加交叉引用一行指向本文。

## 4. 验证

1. `PYTHONIOENCODING=utf-8 /e/Miniconda3/python.exe -m pytest tests -q`：基线 1011，预期约 1022 全绿；默认值不变，旧 D6/D9 用例零回归。
2. `cd webui && npm run build`：零 TS 错误。
3. 手工冒烟（重启 serve.py 后）：项目内 设置→顾问，改三数字+选模型保存，项目 config.advisor 可见；非法值拦截并显 422；恢复默认剥键；新开窗验证生效、在跑窗不受影响。
4. 不 commit（固定模式）。

## 5. 风险点

1. cap=0 不在 _closing_gate 最早处放行 → 零确认语义失效（P2 已处理，测试⑦兜底）。
2. planner 覆写写在工厂外层 → 污染 Orchestrator；必须在内层 factory()。
3. claimed 未收尾的 agent 用例结束须显式 `_abort_current_task()`，心跳线程泄漏（stuck-convergence 文档已记的坑）。
4. 默认值统一引用 ADVISOR_DEFAULTS / 模块常量，禁止在工厂硬编码 12/2/2，避免四处漂移。
5. 前端状态色只用 `bg-(--status-x)` 括号写法（webui/CLAUDE.md 已记 Tailwind 坑）。
