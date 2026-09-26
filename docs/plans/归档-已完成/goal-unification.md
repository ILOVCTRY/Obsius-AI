# 方案：goal 统一——阶段目标升格唯一目标判据层（「作战计划」弹层退役）

- **状态**：**已实施**（2026-09-22 用户拍板「可以，现在就实施」，跳过打磨直接落地）
- **拍板记录**：

| 决策点 | 结论 |
|--------|------|
| 「作战计划」弹层与「阶段目标」冲突怎么解 | 弹层退役，goal 升格唯一目标判据层 |
| mission 写入口 | 关闭（存量读兼容，判据解析居 goal 之下，不自动迁移） |
| ROE 编辑去向 | redteam 轨保留「🛡 行动边界」弹层（RoePopover，仅 ROE 四要素留档编辑） |
| auto_derive 开关去向 | 迁自主档弹层（🧠），deriveLamp 状态灯随迁，开启即触发一轮编排 |
| 判据优先级 | 四层：goal（meta.phase_goal）> mission 存量 > 用户模板 > 轨内置默认 |
| goal 清空回退 | goal.clear 后自然回退 mission/模板/内置链，无空转真空 |
| auto_derive 判跳 | 闸③改走 resolve_criteria(goal=…) 新源，闸序与防空转机制零改动 |
| campaign 召回 query | goal.text 优先（mission 存量回退） |
| 判据模板管理 | GoalEditor 加「应用模板」下拉（内置+用户模板填入判据框）；存/删模板 API 端点保留，管理 UI 后补 |

- **本方案缘起**：用户截图「🎯 设定阶段目标」与「◎ 作战计划」并存质疑冲突。分析确认「作战计划」弹层是混装体三职责——①行动边界/ROE（_mission_section 主体，mission.text 空也注入，不冲突必留）②mission.text/criteria（resolve_criteria 第一优先源 + auto_derive 判跳依据，冲突主体）③auto_derive 开关（迁自主档）。goal 闭环（对话化编排器 M2）已具备 text/criteria/人类确认/事件留痕全要素，唯一目标层归位。
- **关联代码**：`core/orchestrator/judgments.py`（resolve_criteria 加 goal 参数，source 增 "goal"）、`core/orchestrator/orchestrator.py`（_mission_section 删 mission.text/criteria 行只留行动边界；_campaign_section query 换 goal 优先）、`core/api/app.py`（_maybe_mission_auto_tick 闸③传 goal）、`core/autonomy.py`（mission 归一化注释注记写入口退役）、`webui/src/views/LiveRoom.tsx`（ModePopover→RoePopover / chip 区改造 / 自主档弹层加开关 / GoalEditor 升格+应用模板）、`webui/src/lib/types.ts`（Autonomy.auto_derive、PhaseGoal 注释）
- **实施记录（2026-09-22）**：
  - 判据四层解析落地，mission.derive 事件 criteria_source 反映实际来源（goal/mission/template/builtin）；
  - 前端三处迁移：🎯 作战计划 chip 退役（pentest/redteam 两轨），redteam 换 🛡 行动边界（RoePopover）；auto_derive checkbox + deriveLamp 灯迁 🧠 自主档弹层；GoalEditor 验收判据升格为主要编辑面（rows 5）+「应用模板」下拉；
  - 测试翻新：test_judgment_templates_crud（四层优先级+goal 清空回退 mission）、test_mission_view_in_stats_and_prompt（行动边界断言+mission 注入行退役断言+goal 注入）、新增 test_mission_poll_sweep_derives_from_goal（goal 路径判跳全链）；
  - **实施注记**：mission 文本/判据经 _stats 态势 JSON 仍对编排器可见（stats.mission 视图保留）——退役的是 mission_section 的注入行格式，测试断言据此区分； RoePopover 只 patch redteam_roe（不再回写整段 autonomy，避免快照覆写）。
- **不实施/后置**：用户判据模板的存/删 UI（GoalEditor 语义是「人类确认口径」，存模板按钮不合语义；judgment-templates CRUD 端点保留，管理面板后补）；存量 mission 项目无自动迁移（读兼容自衰减）。
