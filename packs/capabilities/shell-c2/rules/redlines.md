# shell-c2 能力包红线（草案待人审；架构位，内部设计见 DESIGN.md §6.9.1〔未实施〕）

1. **Agent 无裸 shell 约束不变**（§7）：一切对战果主机的命令经 run 网关 + 审计，无直连通道。
2. **长连接通道须经网关策略校验与全程审计**；会话建立/命令执行落事件流。
3. **仅 redteam 轨可挂载**；pentest/ctf/research 项目挂载 422（建项目校验强制）。
4. 战果 shell 清单属敏感资产：只入黑板 assets/meta，不入 findings 正文。
5. 本包当前为架构占位：无 skills/kb，内部设计实施时再议（依赖 F2 终端页 workspace 容器基建评审）。
