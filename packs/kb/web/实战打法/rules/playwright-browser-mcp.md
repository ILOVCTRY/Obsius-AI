# 浏览器控制：Playwright MCP（永久）

> 需要「打开/控制真实浏览器、点击、输入、截图、多步流程、JS 渲染页 / 需登录页」时，一律走 **Playwright MCP**，不要用 shell/`npx playwright` 脚本、裸 Chrome CDP、或内置 `web_fetch`/`open_page` 冒充交互控制。

## 入口

本工程唯一浏览器 MCP：**`playwright-claude`**（`.mcp.json` → `node bin/playwright-claude.mjs`，独占 9244 端口 + 独立 profile）。工具名为 **`mcp__playwright-claude__<tool>`**，常用：

- `mcp__playwright-claude__browser_navigate` — 打开 / 导航 URL
- `mcp__playwright-claude__browser_snapshot` — 可访问性快照（理解页面结构**优先**用它）
- `mcp__playwright-claude__browser_click` / `browser_type` / `browser_fill` / `browser_press_key` — 交互
- `mcp__playwright-claude__browser_take_screenshot` — 需要视觉确认时才用
- 探测辅助：`browser_evaluate` / `browser_network_requests` / `browser_files`

> 只认 `mcp__playwright-claude__*`；变更 `.mcp.json` 后需**重启 Claude Code 会话（或重连 MCP）**工具才出现（MCP 在会话启动时加载）。

规则：

1. 优先 **accessibility snapshot**（`browser_snapshot`）理解结构；要视觉才截图。
2. 多步任务**保持同一个浏览器会话**，不要反复重启。
3. MCP 未加载 / 工具不可用 → 明确说明并降级，不硬造浏览器脚本。

## 什么时候不用 Playwright MCP

- 纯静态拉取公开 URL（无交互）→ 内置 `web_fetch` / `web_search` 即可。
- 改文件、git、终端、代码分析 → 用内置工具。
- Playwright MCP 没加载 → 说明并降级（重连 MCP，或补浏览器能力）。

## 自检

- [ ] 是否用了 `mcp__playwright-claude__*` 真正控制浏览器？
- [ ] 是否避免了手搓 bash / `npx playwright` 一行流去驱动浏览器？

> 一句话：浏览器交互控制 → `mcp__playwright-claude__*`，别自己造。