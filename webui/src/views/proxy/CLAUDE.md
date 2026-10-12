# webui/src/views/proxy/

> 代理池视图（fir-proxy 托管，2026-10-07）：顶级导航「工具」组 `代理池`。单文件 `ProxyPoolView.tsx`。

## 职责

项目级 serve（本地 HTTP + SOCKS5 入口）+ 池记录管理：起停服务 / 轮换 IP / 在线抓取 / 验证全部 / 粘贴导入 / 移除 / 实时当前代理 / **访问测试（指定网站，2026-10-12）**。

## 数据链

- REST 3s 轮询 `api.proxyStatus` + `api.proxyList`（两请求并行；`list` 运行中取 serve 实时池、否则读池文件）。
- 抓取/验证是 **202 Job**：`api.proxyFetch` / `api.proxyValidate` → 复用 `lib/api.ts` 的共享 `pollJob(jobId, onUpdate)`（无上限，同 IntelSourcePane/FuzzerView；**勿再手写 120 次上限循环**），done 后读 `job.result` 显示「抓取 N · 验证 N · 清理 M · 入池 K」/「验证完成：可用 N · 清理 M」。
- **进度条 + ETA（2026-10-08）**：`pollJob` 的 `onUpdate(job)` 读 `job.meta.progress`（后端可变 dict 引用）→ `setProgress`；`ProxyProgressBar` 渲染相位（抓取/验证/清理）+ 进度条（`done/total`，分母未知时显 `message` 不确定态）+ `预计剩余`（`eta_seconds`）+ `已用`（`elapsed_seconds`）；`fmtEta` 秒→中文时长。完成/出错由 `runJob` 收尾清 `progress`、落回 `jobNote` 回执。
- **切页不丢进度（2026-10-08）**：`refresh()` 读到的 `/proxy/status` 带 `job{id,kind,progress}`（运行中才有）→ effect 据此**重挂 `pollJob`**（`jobRef` 防重复挂载；已跟踪同一 id 即跳过）。`runJob(jobId,kind)` 是**唯一**的「轮询→收尾回执」入口，首次发起（按钮）与切页重挂共用；`noteFor(kind,result)` 出回执文案。
- **抓取/验证都会剔除失效代理**（后端 `auto_validate`/`prune` 默认 true）：抓取后自动验证新批次；`status≠Working` 或延迟 > `prune_latency_ms`（默认 5s）的直接从池移除。**验证口径=单目标连通性**（后端 `cyberstrike_check.py` 经代理 GET `validate_target`〔默认 `http://www.baidu.com`〕，通即 `Working`）——前端只消费结果，不感知判据。
- **网站设置弹窗（2026-10-12）**：工具条「网站设置」按钮（`Settings2`）→ `ProxyConfigDialog`：配置**验证基准网站**三字段（连通性基准〔必填〕/HTTPS 能力探测/交叉验证第二目标，均可留空关闭探测）+「恢复默认」+「保存」→ `api.proxyConfigGet/Put`（写 `config/proxy.json` + 热更新，保存即提示「下一轮验证/抓取即生效」）。「验证全部」「在线抓取后自动验证」都用这里的基准。
- **访问测试弹窗（2026-10-12）**：工具条「访问测试」按钮（`ShieldAlert`，池非空才可点）→ `CheckUrlDialog`（复用 `components/ui/dialog.tsx`）：URL 输入 + 「检测 WAF/封禁」勾选（默认开）+ 开始测试 → `api.proxyCheckUrl` 提交 202 Job → 局部 `pollJob` 复用 `ProxyProgressBar` 显进度 → 结果区：汇总（共测/可达/被封禁/直连基线）+ **可用代理**列表（未被封禁，`<code>` chips 带延迟）+ 结果表（代理/协议/状态码/延迟/结论徽章〔可达·绿／被封禁·红／不可达·灰 `CheckVerdict`〕/说明）。**只报告不改池**，关闭弹窗仅清本地 state。
- 端点：`/api/proxy/{status,start,stop,proxies,rotate,add,remove,select,fetch,validate,check-url}`（**全局，无 pid**）。
- **协议过滤（2026-10-11）**：工具条右侧 chips `全部/HTTP/HTTPS/SOCKS`（`useState<ProxyFilter>`）；`shown` 经 `useMemo` 过滤——`http`=协议类目、`socks`=socks4+socks5、**`https`=`https_ok===true`**（能力探测）。状态条显示 `status.counts`（`{http,socks,https,total}`）——阈值目标对读者可见；表格 https_ok 行加 `HTTPS` 徽章。
- **全局视图渲染位置**：`App.tsx` 仅首页分支渲染（`needsProject:false` → 项目内被 `!item.needsProject` 过滤掉），故根节点恒带 `pt-9`（首页无顶栏，让位拖动区）。池落 `data/proxy/pool.json`（所有项目共享），旧 per-project 池首启由后端并入。

## 约定与坑

- **服务未启动也能看池**（读池文件），但轮换/取实时当前代理需服务在跑（`running=false` 时按钮置灰）。
- 池空/依赖缺失 → 后端 **503 结构化**，本页直显 `err` 文案，**不静默降级**。
- 导入解析 `parseProxies`（`protocol://host:port` / `protocol,host:port` / `host:port`，`#` 注释，按 protocol+address 去重）——与后端 `ProxyRotator.add_proxy` 的「按 proxy 地址去重」口径不同（前端先滤重减少往返）。
- 样式沿用 `views/fuzzer` 的工具条 + 表格壳；颜色一律走 `--status-*` token（勿硬编码）。
