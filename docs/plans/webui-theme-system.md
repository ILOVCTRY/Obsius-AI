# WebUI 主题与配色系统

> **状态**：已实施（M0+M1+M2+M3，2026-10-03）——M4 预设/编辑器后置
> **关联代码**：`webui/src/index.css`（唯一 token 源）、`webui/src/views/SettingsView.tsx`（外观页签）、`webui/src/main.tsx`（首帧应用）、`webui/src/App.tsx`（顶栏主题入口候选）、`webui/CLAUDE.md` §风格

## 0. 拍板记录

| 决策点 | 结论 |
|--------|------|
| 本轮范围 | **M0+M1+M2 全做**（机制 + 主题1 + 硬编码全收敛）；M3 第二主题、M4 预设/编辑器后置 |
| 浅色主题 | 已实现（M3）：`[data-theme="light"]`，逐值映射生成 + 核心 token 手工校正 |
| 主题 1 命名 | id **`cyber-dark`**（= 当前这套配色，逐值提取，渲染零变化）；中文显示名「暗黑」 |
| 切换机制 | `document.documentElement.dataset.theme` + localStorage `ui.theme`（**非** `.dark` class——要支持 N 套主题而非明暗两态） |
| 默认主题 | 未设置时回落 `cyber-dark`；旧用户无 `ui.theme` 键 → 与现状完全一致 |

## 1. 现状盘点

| 项 | 现状 | 问题 |
|----|------|------|
| 框架 | Tailwind v4（`@theme inline`）+ shadcn 语义 token | — |
| 主题数 | **1 套**：`:root` 25 个 token，本身即暗色；`.dark` variant 已声明但**无任何切换** | 无法新增配色 |
| 主色 | 青色 `oklch(0.8 0.13 190)` | **硬编码 112 处**（index.css 内） |
| 表面色 | 面板底 0.19/0.24/0.29 等 | `index.css` 内 **176 处硬编码 oklch**（`:root` 之外） |
| 可视化色 | 画布节点/链/严重度 | tsx 内 **138 处 hex**（`AttackPath.tsx` 独占 66，GitHub-dark 调色板） |
| 状态色 | `--status-*` 6 个 | **只有前景色，无底色**；消费语法 `text-(--status-error)` |
| 切换入口 | 无 | localStorage 仅 `ui.nav-collapsed` / `ui.nav-more-open` / `ui.wb-findings-*` |

**核心结论**：当前配色**未被 token 化**——`--primary` 只在少数 shadcn 组件生效，绝大部分视觉（面板底、悬浮态、边框、状态点、画布节点）是散落字面量。直接加主题只会换一半、界面花掉。**前置工程是先把颜色收敛成 token**，这正是 M1/M2 的由来。

## 2. Token 分层设计

在现有 shadcn token 之上补四层（`index.css` 的 `:root` 内）。命名规则：语义优先、不绑颜色词（`--brand` 而非 `--cyan`），为浅色预留。

### 2.1 品牌层（吸收 112 处青）

| token | cyber-dark 值 | 用途 |
|-------|--------------|------|
| `--brand` | `oklch(0.8 0.13 190)` | 主强调（= 现 `--primary`，可互为别名） |
| `--brand-soft` | `oklch(0.7 0.14 190)` | 次级/描边 |
| `--brand-dim` | `oklch(0.62 0.14 190)` | 弱化 |
| `--brand-bg` | `oklch(0.8 0.13 190 / 0.1)` | 选中底/徽章底（现散落 `/0.08` `/0.1` `/0.12`） |
| `--brand-border` | `oklch(0.8 0.13 190 / 0.2)` | 选中描边（现散落 `/0.16` `/0.17` `/0.2` `/0.45`…） |
| `--brand-glow` | `oklch(0.8 0.13 190 / 0.4)` | 呼吸/发光（`wb-breathe` 等） |

### 2.2 表面阶梯（吸收 176 处面板/边框色）

| token | cyber-dark 值 | 对应现状 |
|-------|--------------|----------|
| `--surface-0` | `oklch(0.105 0.012 245)` | 最深底（titlebar / kb-tree-search） |
| `--surface-1` | `oklch(0.135 0.014 250)` | 导航/侧栏（app-nav、settings-sidebar） |
| `--surface-2` | `oklch(0.19 0.014 250)` | 面板（= 现 `--card`、settings-select） |
| `--surface-3` | `oklch(0.24 0.018 250)` | 悬浮/次级（= 现 `--secondary`、nav hover） |
| `--surface-raised` | `oklch(0.2 0.015 250)` | 浮层（= 现 `--popover`） |
| `--overlay` | `oklch(0.17 0.018 245 / 0.98)` | 浮层底/下拉结果 |
| `--surface-border` | `oklch(0.29 0.018 250)` | 分隔（= 现 `--border`） |
| `--surface-border-strong` | `oklch(0.32 0.02 250)` | 强调分隔（app-nav border） |

> 原则：`--card`/`--popover`/`--border` 保留为 shadcn 兼容别名，指向 `--surface-*`，组件零改动。

### 2.3 可视化调色板（吸收 138 处 hex）

按语义分组，**不与状态色混用**（画布需要固定色相保证可辨识）：

| token | cyber-dark 值 | 用途 |
|-------|--------------|------|
| `--viz-sev-critical` | `#f85149` | 严重 |
| `--viz-sev-high` | `#ff7b72` | 高危 |
| `--viz-sev-medium` | `#d29922` | 中危 |
| `--viz-sev-low` | `#58a6ff` | 低危 |
| `--viz-sev-info` | `#8b949e` | 信息 |
| `--viz-node-target` | `#58a6ff` | 画布目标节点 |
| `--viz-node-finding` | `#3fb950` | 有效发现 |
| `--viz-node-deadend` | `#8b949e` | 死路/不适用 |
| `--viz-chain` | `#bc8cff` | 攻击链（events.tsx） |
| `--viz-edge` | `#30363d` | 边/描边 |
| `--viz-node-bg` | `#101722` | 节点底 |
| `--viz-code` | `#9fe6c8` | 内联代码 |

### 2.4 状态色扩展（补底色）

现 `--status-*` 只有前景色，保留不动；新增底色族供徽章/横幅用：

`--status-running-bg` / `--status-approval-bg` / `--status-error-bg` / `--status-idle-bg` / `--status-paused-bg` / `--status-ok-bg`（cyber-dark 取各自色相 `/ 0.12`）。

## 3. 主题机制

### 3.1 选择器

```css
:root, [data-theme="cyber-dark"] { /* 主题1：全量 token，逐值提取自当前 :root + 硬编码 */ }
[data-theme="light"]            { /* 未来：M3 预留，本轮不写 */ }
```

- `:root` 与 `[data-theme="cyber-dark"]` 共享同一块声明 → 未设置 `data-theme` 时也正确（默认即主题1）。
- `@custom-variant dark` 保留不动（shadcn 组件里的 `dark:` 前缀继续生效）；`data-theme` 与 `.dark` 并存互不干扰。

### 3.2 应用与持久化

1. **首帧前应用**（防 FOUC）：`index.html` 内联脚本读 `localStorage["ui.theme"]`，命中且合法即 `documentElement.dataset.theme = id`；`main.tsx` 兜底再设一次。
2. **切换**：`src/lib/theme.ts` 导出 `THEMES`（id/显示名/描述）+ `applyTheme(id)`（写 `dataset.theme` + localStorage）。
3. **入口**：`SettingsView` 新增「外观」页签（归 `navGroups` 的「工作流」组），卡片式主题选择器（色块预览 + 名称 + 当前标记）。

### 3.3 校验

`src/lib/theme.ts` 维护 `THEME_IDS` 常量；`applyTheme` 对未知 id 回落 `cyber-dark`（防手改 localStorage 致白屏）。

## 4. 里程碑与验收

### M0 机制 + 主题1
- `index.css` 建 `[data-theme="cyber-dark"]` 全量 token 块（含四层新增），`:root` 指向它。
- `index.html` 内联首帧脚本；`src/ui/theme.ts`；`SettingsView` 外观页签。
- **验收**：无 `data-theme`、`=cyber-dark`、刷新后三种情形渲染与现状**逐像素一致**；切到未定义 id 回落不白屏。

### M1 去硬编码（CSS）
- `index.css` 内 176 处 oklch → 语义 token（`--brand*` / `--surface*` / `--status-*-bg`）。
- **验收**：`index.css` 中 `:root` 之外 `oklch(` 出现次数 = 0（仅 token 定义块内允许）；渲染与 M0 一致。

### M2 去硬编码（组件）
- 138 处 hex → `--viz-*`（`AttackPath.tsx` 66 / `TaskTree.tsx` 16 / `reverse/chains/*` / `events.tsx` 等）。
- React Flow 的 `style={{ background: ... }}` 改用 `var(--viz-*)`（内联样式可用 `var()`）。
- **验收**：`src/**/*.tsx|ts` 中 hex 颜色字面量 = 0；画布/链/任务树配色与现状一致。

### M3 第二主题（浅色）
- `[data-theme="light"]` 全量 token 覆盖（由 cyber-dark 逐值映射生成 + 核心 token 手工校正），`color-scheme: light`。
- `canvas.css` / `tree.css` 去硬编码 → `--viz-*`；组件/规则里的 `[color-scheme:dark]` 全部移除。
- **验收**：外观页出现两张可点卡片，切换即时生效并持久化；两套主题 token 集合一致。

### M3.1 第三主题（暖炭，`warm-dark`）
- 暖灰基底（中性 hue 70）+ 珊瑚橙强调（brand hue 40），对标 cc-haha 色调；同样由 cyber-dark 逐值重映射生成。
- 顺带修正 M1 引入的色偏：`--viz-sev-low` / `--viz-node-target` 误用紫（应为原 `#58a6ff` 蓝），三套主题统一修正。
- 外观页预览卡改为**每卡 `data-theme` 作用域**（预览渲染该主题真实配色，而非当前主题）。

### M4（后置）主题预设/编辑器
- 用户自定义主题（色相派生）、导出/导入。

## 5. 待打磨清单

- [x] 表面色/品牌透明度保留精确兼容 token，避免 cyber-dark 首版产生视觉漂移；后续主题可再合并档位。
- [x] 可视化色纳入主题 token（`--viz-*`），第二主题实现时统一调整对比度。
- [x] 外观页签归入「工作流」组，提供缩略色块预览+主题描述+当前标记。
- [ ] M3 第二主题的浅色对比度与严重度色相对照表。
- [ ] M4 用户自定义主题预设/编辑器。

## 6. 关联代码

| 位置 | 角色 |
|------|------|
| `webui/src/index.css` | 唯一 token 源；`:root` / `[data-theme=*]` / `.wb-*` / `.coord-*` |
| `webui/src/lib/theme.ts` | `THEMES` / `THEME_IDS` / `applyTheme` |
| `webui/index.html` | 首帧内联防 FOUC 脚本 |
| `webui/src/main.tsx` | 兜底应用主题 |
| `webui/src/views/SettingsView.tsx` | 「外观」页签（`navGroups` + `TabsContent`） |
| `webui/src/views/blackboard/AttackPath.tsx` | 最大 hex 源（66 处，M2 重点） |
| `webui/src/lib/events.tsx` | 事件标签色（`--viz-chain` 等） |
| `webui/CLAUDE.md` §风格 | 落定后回写（主题切换约定 + token 分层） |
