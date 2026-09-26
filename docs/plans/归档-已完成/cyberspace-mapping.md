# 方案：网络空间测绘页（FOFA 资产查询内置 + 资产批量导入）

- **状态**：**已全量实施（M1+M2，2026-09-23）**——M1 文件导入 + M2 FOFA 查询同批落地；决策已回写 `DESIGN.md`（§12 页面清单「测绘」页 + §三 资产节批量导入语义）
- **拍板记录**：§3（用户定稿）+ §5（实施拍板，AI 夜间自主推进按业界惯例拍板）
- **需求来源**：2026-09-22 用户三输入——①新增资产导入功能 + 内置 FOFA 查询（结果可选导入）；②用户提供第三方 FOFA 中转 API（完整文档已存 `docs/第三方fofo开发文档.txt`，官方兼容仅换 base url）；③`中原工.xlsx` 264 行真实导出样例（可能有多列需适配，主取 ip+域名+端口+标题+指纹）
- **关联代码**：`core/blackboard/assets.py`（register_asset 统一登记入口：五类型识别/IP 挂载/domain 自动 DNS/去重合并——导入必经路径）、`core/api/app.py`（:1897-1990 资产 CRUD 端点；:519 MCP_CONFIG_PATH 配置文件读取先例）、`webui/src/views/Blackboard.tsx`（:42/:61/:70 黑板页 tab 组）、`webui/src/lib/api.ts`（:46 httpUpload 上传先例）、`.gitignore`（`*.local.json`/`config/providers.json` 密钥不入库先例）

## 0. 实施记录（2026-09-23）

- **M1 文件导入**：`core/assetimport.py`（嗅探/解析/归一化）+ `import_assets`（register_asset 单一入口 quiet=True + DNS 线程池预热批次内缓存 + 行路由 domain 先行）+ preview/import 两端点 + `MappingPane.tsx` 导入工作区 + 资产行来源徽章/指纹 chips + `asset.imported` 事件（带 batch_id）。
- **M2 FOFA 查询**：`core/fofa.py`（错误四态分类 + 主备切换 + transport 可注入）+ config GET/PUT、test、search 三端点（熔断 429/配置 400/其余 502）+ 查询工作区（勾选导入，既有灰显）+ 配置条（key 脱敏回显、空串不覆盖、测试连接）。
- **测试**：test_assetimport.py（15）+ test_fofa.py（16，fake transport 零触网）+ test_api 冒烟（6）；全量回归 912 passed。
- **安全**：`config/fofa.json` 已 .gitignore（git check-ignore 验证）；key 不入任何日志/文档。


## 1. 背景与定位

- **功能定位**：给项目黑板补「资产从哪来」的批量入口——网络空间测绘引擎（FOFA）直查 + 本地文件（xlsx/CSV）导入，产出统一落黑板资产表，供编排态势/链路图/渗透测试侦察阶段消费。
- **页面定位**：黑板页新增第 N 个页签「**网络空间测绘**」（发现/资产/全景 并列，tab value=`mapping`）。页签名不叫 FOFA——预留多引擎扩展（Quake/Hunter/ZoomEye 后置候选）。
- **明确不做**：不挂情报子系统（core/intel/ 是 RSS 简报类，测绘是资产 API 类，性质不同，用户拍板分开）；Agent 不开放查询（M3 后置候选，配额护栏另议）；`stats`/`host` 两个贵端点（固定烧 1000 配额）不接。

## 2. 现状盘点（2026-09-22 核实）

- **register_asset 全逻辑**（core/blackboard/assets.py）：`detect_type` 五类型（url/host(IPv4)/service(host:port)/domain/binary）；url/service 主机部为 IP → 自动挂 host；为域名 → **只挂既有 domain 行（不猜 DNS）**；domain → 自动 `resolve_ipv4` 挂 host + primary_domain/alias 标记；去重键 (type, value) 命中即合并（补挂父级 + meta merge）；逐行落 `asset.new` 事件。人工 POST /assets 与 Agent bb_add_asset 共用——**导入必须走它**。
- **资产表**：assets(id/project_id/type/value/parent_id/meta json/author/created_at)；meta 自由 dict（title/products/source 等约定落这里）；PATCH 可改挂父/合并 meta。
- **第三方中转 API 特性**（docs/第三方fofo开发文档.txt）：base url `https://fofoapi.com` 主 / `http://107.173.248.139:18999` 备；认证仅 `key`；调用格式与官方完全一致（`GET /api/v1/search/all?qbase64=&key=&fields=&size=&page=`，results 数组套数组）；**配额按返回条数消耗**；`info/my` 查余量**免费**；返回「已用完」必须立即停止否则可能封号；错误签名三态（「账号无效」=base url 没换 / 「key 不存在」=key 错 / `[官方错误信息] [code]` 前缀=官方侧可重试）；fields 含 header/banner/cert 时单次上限 10000→2000。
- **xlsx 样例实测**：264 行 × 4 列**无表头**，列序 [ip, 端口, 带协议 URL, 标题]；存在 ip/端口为 None 的稀疏行与前导空格脏数据；无指纹列需预留。
- **可复用基建**：httpUpload（前端 multipart）、config 文件读取惯例（缺失/损坏给空配置绝不 500）、openpyxl 在运行环境可用（pyproject 需显式加依赖）。

## 3. 定稿决策（用户拍板 2026-09-22）

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 页面落点 | 黑板页新增「网络空间测绘」页签，**不挂情报子系统**；查询/导入/配置全部页签内自持 |
| 2 | 导入链路 | 一律走 register_asset 统一登记入口（五类型识别/去重合并/DNS 挂载全复用），零旁路 |
| 3 | key 安全 | FOFA key 为敏感凭据：只存 `config/fofa.json`（.gitignore 加一行），不入库不入 git 不出现在任何方案/代码文件；前端展示脱敏 |

## 4. 设计详述

### 4.1 页面信息架构（webui「网络空间测绘」页签）

- **顶部工具行**：引擎徽章（M2 仅 FOFA，预留多引擎下拉）+ 配置状态（未配置→引导卡「填入 FOFA key」；已配置→余量徽章 + 设置按钮弹配置弹窗）。
- **配置弹窗**（页签内自持，不进设置页）：key（保存后脱敏显示前4后4）/ base url（默认 `https://fofoapi.com`，可改备用）/ 默认 size；「测试连接」按钮 → info/my 实时测活显示（剩余配额 / VIP 等级 / 到期时间）。
- **查询工作区**：FOFA 语法输入框（原生语法直填，如 `domain="zut.edu.cn"`）+ 常用语法速查折叠面板 + size 选择（100/500/1000）+ **消耗提示**（「本次预计消耗≈N 条配额，当前余量 M」）→ 查询 → 结果表：复选框列（**已存在资产行灰显默认不勾**，服务端标注 new/existing）+ ip/端口/协议/域名/标题/指纹列 →「导入选中」→ 回执 toast（N 新建 / m 合并 / k 失败）。
- **导入工作区**：文件选择/拖拽（.xlsx/.csv）→ 列映射预览表（自动嗅探建议 + 每列下拉可改：忽略/ip/端口/域名/URL/标题/指纹，多列可归指纹）+ 行预览（前 50，稀疏行标注）→「确认导入」→ 回执 toast。
- **黑板联动**：导入产物在「资产」页签可见（title 副行/指纹 chips/来源徽章，见 4.6）；链路图资产节点零改动自动出现。

### 4.2 core/fofa.py（新模块，纯客户端零 bb 依赖）

- **职责**：请求组装（qbase64 编码、fields 白名单、size clamp）+ httpx 调用 + 双 base url 主备自动切换（主失败/「账号无效」签名 → 换备用重试一次）+ 响应行归一化 + 错误分类。
- **错误分类**（异常类型区分，API 层转文案）：`QuotaExhausted`（「已用完」→ 熔断，前端红条警告）/ `AuthError`（key 错）/ `ConfigError`（base url 没换）/ `OfficialRetryable`（`[官方错误信息]` 前缀，重试 1 次）/ 网络超时。
- **配额护栏**（宁严勿松）：size 单次硬上限 1000（10000 全量留给配置文件手动开）；fields 固定白名单 `ip,port,protocol,host,domain,title,product`（不含 header/banner/cert，保住 10000 档上限且响应小）；查询前不强制预检但 UI 显示余量（info/my 免费）；`info_my()` 独立方法免费调用。
- **多引擎预留**：单文件起步不做 core/mapping/ 目录（YAGNI），Quake 等真实接入时再目录化。

### 4.3 导入链路（core/blackboard/assets.py 扩展 + 新解析器）

- **register_asset 加 `quiet=False` 参数**（本方案对现有代码的唯一侵入）：quiet=True 抑制逐行 asset.new 事件——批量导入 264 行不刷屏事件流。
- **`import_assets(bb, pid, rows, source, author="human")`**（新函数，assets.py 内）：
  - rows 为归一化行 `{ip?, port?, host?, url?, title?, products?}`；
  - **导入排序**：domain 行先于 url 行（register_asset 对 url 只挂既有 domain，域名先行提高挂载率）；importer 对 url 行的域名部分若无既有 domain 行，可先补登 domain（其内部自动 DNS 挂 host，解析失败不阻塞——现有容错）；
  - ip+port 无域名 → service（自动挂 host）；仅 ip → host；有指纹 → meta.products（列表，逗号/分号拆分）；title → meta.title；来源 → meta.source（"fofa"/"xlsx"）+ meta.imported_at；
  - 返回 `{created, merged, failed: [{index, reason}]}`；落**单条汇总事件** `asset.imported` {source, total, created, merged, failed_count, author}。
- **列映射嗅探**（新文件 `core/assetimport.py`）：表头名匹配（ip/IP/域名/host/端口/port/标题/title/指纹/product…）→ 无表头时逐列内容投票（≥80% 行命中：IPv4 正则→ip；纯数字 1-65535→port；scheme://→url；含点无 scheme→domain；首个文本列→title）；每列产出 `{kind, confidence}`，UI 下拉可改写；稀疏行容错（全空跳过、单字段缺失降级登记）；CSV 走 stdlib 恒可用，**openpyxl import-guard 降级**（未装 → xlsx 预览端点 503 + 安装指引，仿 browser extra 惯例；pyproject api extra 加 `openpyxl>=3.1`）。

### 4.4 API 端点（core/api/app.py 新增 5 个）

| 端点 | 方法 | 语义 |
|---|---|---|
| `/api/fofa/config` | GET/PUT | 读（key 脱敏）/写 config/fofa.json；读取仿 mcp.json 惯例缺失给空配置绝不 500 |
| `/api/fofa/test` | POST | info/my 实时测活（免费）：余量/到期；未配置 400 带指引 |
| `/api/projects/{pid}/fofa/search` | POST | {query, size, page} → 归一化行 + 每行标注 existing + 余量回显；size 服务端 clamp；熔断异常转 429+文案 |
| `/api/projects/{pid}/assets/import/preview` | POST | multipart 上传 → 解析 + 列映射建议 + 行预览（**不落库**） |
| `/api/projects/{pid}/assets/import` | POST | {source, rows≤5000} → import_assets → 回执明细 |

- 日志红线：key 不进任何日志/错误响应全文；search 端点对 query 不做白名单限制（语法自由，配额靠 size clamp 护）。

### 4.5 配置与安全

- **`config/fofa.json`**：`{base_url, key, size_default}`；.gitignore 加 `config/fofa.json` 一行（同 providers.json 待遇）。
- **信任边界注记**（方案与页面各写一次）：查询内容与返回资产清单明文经第三方中转（HTTPS 传输但第三方可见）——敏感项目慎用；平台仅是调用方，不为其存储行为背书。
- key 展示脱敏（前4后4）；编辑时输入框留空 = 不修改（防误覆盖）。

### 4.6 资产页签展示增强（导入产物可见性）

- 资产行：`meta.title` 副行/tooltip；`meta.products` 指纹 chips（截断 +N）；`meta.source` 来源徽章（fofa 🛰 / 导入 📥）。
- 事件流（LiveRoom）：`asset.imported` 加 FILTERS 条目（汇总行，显示「N 新建/m 合并」摘要）。
- 资产页签筛选暂不加 source 维度（后置观察）。

## 5. 待打磨清单（实施时已全部拍板，2026-09-23）

1. **M1/M2 实施顺序** → ✅ 同批连做 M1→M2（import_assets 链路 M2 复用，一次回归覆盖）；
2. **批量导入 DNS 解析策略** → ✅ 线程池预热（16 workers）填充批次内缓存（`_DNS_WARM`，import_assets 结束即清）；**不做进程级负缓存**——DNS 恢复后域名要能挂上；
3. **size 默认值与硬上限** → ✅ 默认 100，硬上限 1000（中转单次限制）；page×size≤1 万官方翻页上限同步 clamp；
4. **url 行补登 domain 的 DNS 失败率** → ✅ 维持现有语义：解析失败域名行独立登记不阻塞；挂载率待真实数据观察，不预设；
5. **指纹多列归并规则** → ✅ 全部 products 列保序去重归并（`_split_products`，cap 20）；FOFA product 字段同法拆分；
6. **title/products 展示密度** → ✅ title 落库截 200 字符，前端 CSS truncate + tooltip；指纹 chips ≤3 +「+N」；
7. **结果表列排序/二次筛选** → ✅ 不做（结果 ≤1000 行场景下观察后再议）；
8. **asset.imported 事件批次 id** → ✅ 带 `batch_id`（`imp-` 前缀），行 meta 同记 `import_batch`——按批次撤销导入的预留锚点。

## 6. 实施切分建议（待用户排期）

- **M1 文件导入**：core/assetimport.py 列嗅探 + import_assets + quiet 参数 + preview/import 两端点 + 前端导入工作区 + 资产行展示增强 + asset.imported 事件——不依赖外部 API，独立可上；
- **M2 FOFA 查询**：core/fofa.py + config/fofa.json + config/test/search 三端点 + 前端查询工作区与配置弹窗 + 依赖项 openpyxl 落 pyproject——依赖 M1 的 import_assets（查询导入复用同链路）；
- **M3 后置候选**：Agent 工具化（fofa_search/fofa_import 工具 + 配额护栏）、pentest recon 阶段剧本集成、多引擎（Quake/Hunter）扩展、按批次撤销导入。

依赖关系：M1 → M2 松依赖（导入链路复用）；M3 全部独立后置。

测试面：test_assetimport.py（列嗅探表头/无表头/稀疏/多列/CSV）、test_fofa.py（qbase64/主备切换/错误分类四态/熔断/归一化，mock httpx）、test_api 冒烟（clamp/白名单/回执明细）、前端 npm run build 零 TS 错误。
