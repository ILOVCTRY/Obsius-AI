# 直播间会话维度分页与滑动窗口：会话页签打开即铺满、上翻自动加载、滚底裁头

## 状态

**已实施**（2026-09-23 同日落地，实战痛点修复批次③；随批次①② 一起全量回归）。改动面前端为主，后端仅一个参数透出。实施落地时回写 DESIGN.md §12 直播间段与 webui/CLAUDE.md。

## 实施记录（2026-09-23）

- 按定稿实施：app.py list_events 透传 session_id；api.ts eventsTail/eventsBefore 加可选 sid；useEvents.ts 签名 `(pid, sessionId?)` + cache 键 `${pid}:${sid}` 分叉 + WS 按 sid 过滤（游标恒前进）；LiveRoom.tsx dataSid 换算（`__all`/`__orch` 走全局源）+ 铺满视口循环 + 滑动窗口裁剪（MAX_DOM=300/PAGE=50/atBottomRef 贴底才裁）。
- **实施修正 1**：裁剪需写 hook 内部展示 state → useEvents 暴露 `trimDom(max, keep)` 出口（只裁组件 state 不动模块 cache，cache 恒为展示窗口超集，loadEarlier 可从缓存补回被裁头部）。
- **实施修正 2**：资产 id→value 反查映射的 WS 增量（asset.new）在会话源下只捕获本会话 → assets 拉取 effect 依赖加 dataSid（切页签重拉全量补齐跨页签映射）。
- **已知边界**：budgetPausedSids（E8「▶ 继续」chip）等跨会话事件派生在会话源下只见本会话——chip 只对 activeSession 显示、会话源水合窗口（300 条会话事件）覆盖远好于原全局 50 条、页签 paused 琥珀灯有 sessions 轮询 DB 稳定事实兜底，接受不改；tabStatus（paused/running/armed）均有 DB 稳定事实兜底不受影响，flowBump 有 3s 轮询兜底。
- 测试：tests/test_api.py `test_events_session_filter`（两会话各落数事件 tail/before_id 各取各的+不带参数全局流不变）；webui `npm run build` 零 TS 错误。

## 问题与根源

用户实测（中原工学院项目，已结束任务窗「任务窗对 222.22.91.」）：打开会话页签消息区近乎全空，只有底部「↑ 加载更早」按钮，手点多次才翻出自己会话的几行历史。

**根源=全局流分页 + 页签本地过滤的组合错位**：

- `useEvents`（useEvents.ts:128）按**项目全局**拉最新 50 条 + WS 增量，`loadEarlier` 每批也拉 50 条全局；
- 会话页签（LiveRoom.tsx:742）只对全局窗口做本地过滤 `e.session_id !== activeTab`——已结束会话最近无动静，全局最新 50 条里可能一条都没有 → 页签空白；
- 「加载更早」每批 50 条全局里筛出的该会话事件可能只有几条，已结束会话要连点很多次；上翻自动加载（onListScroll distTop<200）在内容不滚动时不触发（无 scroll 事件），空白页签只能手点。

## 定稿设计

**数据源从「全局拉取+本地过滤」改为「会话维度分页」**（发现：store.recent_events 的 `session_id` 参数 F8 早已支持〔store.py:858，会话级复盘取材〕，仅 API 路由与前端未接线）。

### 后端（一行透出）

- app.py `list_events`（:2728）加 `session_id: str | None = None` 查询参数，透传 `bb.recent_events`（store 已实现 `AND session_id=?`，tail/before_id/since_id 三形态全兼容）。

### 前端 useEvents 会话感知（核心）

- 签名 `useEvents(pid, sessionId?: string | null)`（默认 null，App.tsx / ReverseWorkbench 现有调用点不动走全局）；
- cache 键：sessionId 非空用 `` `${pid}:${sid}` ``（每会话独立窗口缓存，SPA 内常驻），null 用现 pid 键——多实例共享机制不变；
- effect 依赖加 sessionId：切页签即换源（全局源 ⇄ 会话源），会话源首屏走 `eventsTail(pid, PAGE, sid)`；
- WS 增量：连接照旧（项目级单连接），sid 非空时 onmessage 按 `e.session_id === sid` 过滤后才入 buf/状态，**游标恒前进**（不属于本会话的事件只推游标不进窗口，断线重连回放不重复）；
- `eventsBefore(pid, beforeId, PAGE, sid)` 会话维度翻页，loadEarlier 其余逻辑（先吃缓存再网络、CACHE_LIMIT 裁剪降级）原样复用。

### 铺满视口（LiveRoom 层）

- effect 监听 events/loadedAll/loadingEarlier：`scrollHeight <= clientHeight + 余量` 且未取尽 → 继续 `loadEarlier()`，循环到铺满或取尽（每批 50，通常 1-3 轮）；loadingRef 防并发、取尽即停防死循环。

### 滑动窗口裁剪（用户点名的第三件事）

- DOM 窗口上限 `MAX_DOM = 300`（与 HYDRATE_LIMIT 同量级），裁剪粒度 PAGE=50；
- **触发=滚回底部附近**（column-reverse 贴底 |scrollTop|<200 且窗口 > MAX_DOM）→ 裁掉 events 头部一批——贴底视觉零跳动（column-reverse 滚动原点在底部，顶部内容缩减不位移）；
- 裁剪只动 `events` state（visible/items/shown/orchShown 全部派生瘦身）；**不动模块 cache**——再上翻时 loadEarlier 先吃缓存零网络补回，缓存超出 CACHE_LIMIT 才打网络；
- 裁掉的头部对 claimCursor 等游标扫描逻辑无影响（游标只前进）；与上翻加载互斥（在底部才裁，在顶部才加载），天然不抖动；
- `__all` / `__orch` 页签（数据源=全局）同样套用裁剪——长跑项目直播一整天 DOM 也有上界。

### 不变量

- 现有渲染管线零改动：visible→items（配对/分组）→shown 仅数据源变了；跨批边界的 command/command.result 配对与 turn 分组由 items 全量重算自愈（孤儿行在补齐批次后消失）；
- 会话页签语义与现状 visible 过滤一致（`e.session_id === activeTab`，项目级事件不进会话页签）；
- 模块缓存多实例共享、水合截断、CACHE_LIMIT 语义全部保持。

## 改动面

| 文件 | 改动 |
|----|----|
| `core/api/app.py` list_events | 加 session_id 参数透传（~1 行） |
| `webui/src/lib/api.ts` | eventsTail / eventsBefore 加可选 session_id |
| `webui/src/lib/useEvents.ts` | 签名加 sessionId + cache 键分叉 + WS 过滤 + tail/before 带参（~30 行） |
| `webui/src/views/LiveRoom.tsx` | activeTab→dataSid 换算传 hook + 铺满视口循环 + 滑动窗口裁剪（~40 行）；「↑ 加载更早」按钮保留为兜底（自动加载在途时显「加载中…」） |
| `tests/test_api.py`（或就近文件） | list_events session_id 过滤用例：两会话各落数事件，tail+before_id 带 session_id 各取各的，不带参数行为不变 |

## 验收（用户实操）

1. 打开已结束任务窗页签 → 历史自动加载**刚好铺满视口**，无需手点；
2. 滑轮上翻 → 距顶约 200px 自动补更早批次，位置不跳；
3. 滚回底部 → 更早消息自动缩减（DOM 恒 ≤ ~300 条），再上翻秒回（缓存补回）；
4. `__all` 页签与编排页签行为不回归；直播中新事件照常实时追加。

## 范围外（记打磨账）

- 会话窗口缓存的 LRU 上限（多会话驻留内存管理）——先靠 CACHE_LIMIT/键控隔离，实战反馈再调；
- 精细化虚拟列表（按行高动态测量+只渲染视口行）——滑动窗口已把 DOM 钉在上界，暂不引入虚拟化库；
- ReverseWorkbench 事件流如需会话维度分页，复用同一 hook 扩展（其调用点本次不动）。
