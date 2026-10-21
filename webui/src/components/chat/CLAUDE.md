# components/chat/ — 会话流共享件（cc-haha 风格改造，2026-10-03）

> 直播间（`views/live/EventRow.tsx`）与智能体工作台（`views/AgentWorkbenchView.tsx`）
> **共用**的会话流呈现件。刻意不依赖 `.wb-*` CSS（那是工作台专属），一律 Tailwind
> 工具类，两处可直接复用。

## 文件

- `ActivityGroup.tsx` — **折叠活动摘要行**：一轮里的连续「思考/命令/工具」折成一行
  灰色摘要（`读取了 2 个文件，执行了一条命令，思考`）+ 右侧耗时，点开看明细。
  三条纪律对齐 cc-haha：①**运行中恒展开**（`live` prop），结束后折叠；②**单步不成组**
  （`steps.length<=1` 直接渲染 children，摘要行没信息量）；③展开态是**组件内 state**，
  不进父级折叠记忆（与 `EventRow` 的 `TurnRow.innerOpen` 同纪律，避免污染顶层 overrides）。
  摘要串与耗时算法在 `lib/activity.ts`（纯函数）。
- `FileCard.tsx` — **文件卡**：图标方块 + 文件名 + 语言徽章（`MARKDOWN`/`PYTHON`…）+
  相对路径，右侧「打开方式」下拉（radix `DropdownMenu`）四动作：系统默认程序打开 /
  资源管理器定位 / 复制路径 / 复制文件内容。`FileCardList` 是一组卡片的便捷壳。
  **`pid` 缺失时下拉不渲染**（审计抽屉等无项目上下文场景降级为只读展示，不报错）。
  动作经 `api.openProjectFile(pid, relPath, action)` 打 `POST /api/projects/{pid}/files/open`。
- `ThinkingBlock.tsx`（**2026-10-04**）— **思考块（单行折叠）**，对齐 cc-haha
  ThinkingBlock：🧠 + 标签（`思考中`/`已思考`）+ 一行预览 + ▸ 箭头；点开在缩进框里
  看全文（`max-h-72` 内部滚动，不撑破会话流）。预览口径在 `lib/activity.ts` 的
  `thinkingPreview`（流式中跟随尾部、稳定后回首行、短冒号标题让位下一行）。
  `active`=仍在流式（标签变色 + 预览跟随 + 展开态底部光标）。数据源=工作台消息的
  `ChatMessage.thinking`（v31 持久化）或实时 `chat.thinking.delta` 累加文本。
  **刻意不复用 `views/live/EventRow.tsx` 的 `ThinkingRow`**：那条是事件行（带时间戳/
  duration），本组件是消息行。
- `ChatStats.tsx` — **统计行**：`{tokens} tokens（含缓存） · 最后更新 {相对时间} · {N} 条消息`。
  导出 `fmtTokenCount`（847/1.2k/1.2m）与 `fmtRelative`（刚刚/N 分钟前…）。工作台传精确
  值（`thread.usage`），直播间传窗口内近似（`llm.usage` 事件求和 + `visible.length`）。
- `CandidateFlagCard.tsx`（**2026-10-20**）— **候选 flag 确认卡**：CTF 解题主控的子代理
  提交候选 flag（`verdict=pending`）时，后端唤醒主控说一段话（`core/chat/ctf_wake.py`），
  本卡贴在 composer 上方给出「复制 / 确认有效 / 标记为错误」。三处刻意设计：
  ① **flag 完整显示 + 独立复制钮**（不是徽章式点复制——对话流里没有台账表格的上下文提示，
  徽章点复制不好发现）；② **确认按钮写明后果**（确认 = 题目判为解出 + **通知本项目所有在跑的
  轮次尽快收工**——用词是「通知收工」不是「中止」：轮次会在下一个步边界读到事实、写完手上的
  结论再停，不会被掐在半个工具调用里）；③ **标记为错误强制填理由**（理由进台账后同串再提交被 `is_rejected` 拦下，不填等于
  让模型反复提交同一串）。数据源 `lib/useCandidateFlags.ts`（3s 轮询台账 + `wakeKey` 即重拉）——
  **取台账不取事件载荷**：事件里 flag 被截到 120 字符，拿它做复制会复制出半截串。
  徽章「可回环复算 / 仅形态判定」由 `flag.transform` 是否为空决定，这是人类判断「像不像答案」
  的唯一机械依据。
- `TeamCard.tsx`（**2026-10-06**）— **会话内团队卡**：直播间「指挥」对话流里呈现 Team
  （`core/team` 域，任务机制退役后的新执行单元）。两形态对齐 cc-haha Agent Teams 参考图——
  待确认态（`draft`/`ready`）：`等待人工确认 · 尚未启动成员 · N 位成员` + 团队名 + 「查看并配置」；
  已建/运行态：图标 + 团队名（等宽）+「组建团队」徽章 + `Agent 团队 · N 名成员` + 「打开运行报告 ›」。
  纯展示件，数据由 `OrchChatPane` 从 `team.*` 事件 + 实时 `Team` 反查给出（回调交父级）。
  **两个回调落点**（父级 `LiveRoom` 挂载）：待确认态「查看并配置」→
  [`components/team/TeamConfigDialog.tsx`](../team/CLAUDE.md)（名册编辑 + preflight + 四项确认启动）；
  运行态「打开运行报告」→ `views/live/TeamRunReport.tsx`（Run 成员执行明细）。
  **旧 `CoordinationProposalCard` 已随协调 DAG 退役（2026-10-06）**，团队卡是其替代。
- `AttachmentChip.tsx`（**2026-10-20**）— **待发附件胶囊**：工作台 composer 与直播间插话框
  共用（`AttachmentChipRow` 是一组胶囊的壳，空列表渲染 null）。这次是**重做**：旧版两处各粘
  一份手写 `<span>` 方角小方块（`rounded-md border px-2 py-1`），图标一律 paperclip、文件名与
  字节同字号——而 composer 其余控件（agent 选择、`.wb-cap`、用量环、发送键）全是圆胶囊，
  只有附件方角，形态自成一族；也不看扩展名，任何文件长得一样（Nan 反馈「不够高级/太简陋/不协调」）。
  现口径：**圆胶囊 + 类型图标 + 三层信息**（类型色方牌 → 文件名 → 字节弱化），三态
  （`uploading`/`ready`/`error`）共用骨架只换状态位，避免宽度跳变；`error` 态红边 + 重试钮。
  图标与色档复用 `FileCard.tsx` 导出的 `FILE_ICONS`/`FILE_TONES`/`FILE_TONE_BG`（与文件卡同族）。
  **两个实测坑**：① 图标底不能用 `--accent`——五套配色里 accent 与背景同色系，`rose-light`
  亮底上灰牌直接溶进白底消失；必须用「类型色 12% 淡底」。② 字节数**不能用 `font-mono`**：
  9.5px 等宽字距散成「4 . 0  K B」，改 sans + `tabular-nums`。另：`dark:` 变体在本项目**是死的**
  （`@custom-variant dark (&:is(.dark *))` 但从不给 `<html>` 加 `.dark`，配色走 `data-theme`），
  别用 `dark:` 做亮暗分档。

## 约定与坑

- **数据源是纯函数，不在组件里解析**：活动摘要走 `lib/activity.ts` 的
  `buildActivitySegments`/`activityDurationMs`/`formatDuration`；文件目标走
  `lib/fileTargets.ts` 的 `targetsFromTool`/`targetsFromProse`/`mergeTargets`。
  组件只负责呈现——两处渲染层因此不会各自漂移出口径。
- **`ActivityStep.kind`**：`"thinking" | "command" | "tool"`。直播间由
  `StreamItem[]`（turn 的 process 组）映射，工作台由 `ChatMessage[]` 映射。
- **文件路径一律相对 scope 根**（`FileTarget.relPath`）——后端 `_open_scope_path`
  以项目工作区为基解析，packs/tools 下的绝对路径也接受（同一 `is_relative_to` 校验）。
- 未登记的工具在摘要里**原样显英文名**（`名字 (N)`），不隐藏信息——与 cc-haha 同策略。
- **文件目标抽取必须过 `looksLikePath`**（2026-10-04 修误报文件卡）：`targetsFromTool`
  的结果文本正则（`文件[:：]`/`path[:：]`…）捕获类**不排除中文标点**，`kb_open`/
  `skill_open` 正文里的「文件：」会把其后整句中文吞成「文件名」（曾把
  「；验证上限=影响证明级；…tested_clean」渲染成文件卡）。**参数路径（结构化、可信）
  不过滤，结果文本抽取一律过滤**——`looksLikePath` 要求无空白、长度 ≤300、basename
  带「.」且扩展名在已知表内（中文句子通常无 `.` 或扩展名不在表内，故被拒）。
