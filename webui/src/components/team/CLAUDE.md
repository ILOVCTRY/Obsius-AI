# components/team/ — 团队域前端件（core/team 消费面，2026-10-06）

> 直播间「指挥」页签的 Team（Team/Member/Run）交互面。数据源=`lib/api.ts` 的 `teams*`
> 客户端（`core/api/app.py` 的 `/projects/{pid}/teams*` 端点），不依赖 `.wb-*` CSS。

## 文件

- `TeamConfigDialog.tsx` — **「查看并配置」弹窗**（对齐 cc-haha `AgentTeamsPlanCard` 编辑弹窗）：
  进入时并行拉 `GET /teams/{id}` + `GET /teams/{id}/preflight`；展示成员 roster（名册 key/显示名/
  角色/模型/runtime/threat_class/max_steps/职责）可增删改 + 团队名/共享目标 + preflight 结果
  （场景轨/会话容量/revision/blockers）。动作：**保存名册**（`PATCH`，revision 乐观锁由后端校验，
  保存后自动重拉 preflight）+ **确认并启动**（四项确认全勾 → `POST start`，携带 `preflight.revision`
  与 `confirmations{members,goal,safety,execution}`）。仅 `draft`/`ready` 可编辑/启动（后端 409）。

## 约定与坑

- **`member_key` 是名册主键**：后端 `UNIQUE(team_id, member_key)`，重复即 422。新增成员前端自动
  生成 `member-<n>`，用户可改。
- **保存会改 revision**：`preflight.revision` 是「名册+目标+安全+容量」的 sha256 摘要，任何保存后
  旧 revision 失效——启动前必须用最新 preflight 的 revision，故保存后强制重拉。
- **blockers 非空禁启动**：preflight 有 blockers 时按钮禁用（后端也会 ValueError→422）。
- 运行环境枚举：runtime ∈ {host, wsl, docker, sandbox}（空=默认）；threat_class ∈
  {trusted, untrusted, malware_live, unknown}。与 `core/runtime/policy.py` 对齐，改动需同步。
