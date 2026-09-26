# 资产树归并与根状态自动派生（asset-tree-derived-clean）

> 用户 2026-09-24 提出的三项改造合一批：①同 IP（非 CDN）资产按 IP→域名→URL 归树；②根节点 clean 状态由子树自动派生、AI 不得显式设置，新资产入树自动破坏 clean；③黑板页资产过滤器支持域名。直接修正在 assessment-20260915-7d70 发现的「92 域名根路径单发测活即批量标 tested_clean」问题。

- **状态**：**已全量实施（M1+M2+M3，2026-09-24）**——决策已回写 `DESIGN.md`（§三「资产树归并、CDN 判定与根状态读时派生」），本文件已移入 `归档-已完成/`
- **拍板记录**：已收敛项见 §0；带 ❓ 的为待拍板项（文内给出推荐默认值）
- **关联代码**：
  - `core/blackboard/assets.py`：`register_asset`（:124，DNS 自动挂树 :189-201）、`resolve_ipv4`（:68）、`warm_dns`（:48）
  - `core/blackboard/store.py`：`set_asset_status`（:1207，六态状态机）、`upsert_asset`、`set_asset_parent`、`list_assets`（:1166）
  - `core/coverage.py`：`asset_terminal_state`（:30）、`coverage_report`（:52，收敛传播 :95-107）
  - `core/orchestrator/orchestrator.py`：uncovered/covered 口径（:796-802）、`done_count`（:837）
  - `webui/src/views/Blackboard.tsx`：`hostOptions`（:167-170）、子树展开过滤（:193-206）
  - `scripts/normalize_dirty_domains.py`、`scripts/adopt_orphan_assets.py`（存量处理先例）

## 实施记录（2026-09-24，M1+M2+M3 一批）

- **M1 归并+CDN**：`core/blackboard/cdn.py`（is_cdn：meta.cdn 覆盖>CNAME 后缀>
  IP CIDR；CdnLists；packs/data/cdn_ranges.json 基线 + config/cdn.json 增补，
  坏文件 ValueError fail-fast；default_cdn_lists 进程缓存）+ assets.py 接线
  （CDN 域名保持根行不建 host；DNS 漂移 set_asset_parent 改挂/摘挂 +
  asset.reparent 事件）。
- **M2 读时派生+存量**：`core/coverage.py` effective_status_map /
  attach_effective_status（叶子 explicit、有子父节点随全部孩子 settled 派生
  tested_clean/任一 open 则 open、新子自动破 clean、has_findings 沿树上传）+
  store.py 写入门（有子节点写 tested_clean → ValueError，na 不挡）+
  `scripts/rebuild_asset_trees.py`（DoH 223.5.5.5 避 fake-ip；CDN 无操作、
  有子脏根 reset-open；默认 dry-run，--apply；全经 Blackboard 方法）。
- **M3 过滤器**：Blackboard.tsx 资产筛选器放开 host/domain/url/service 四类
  按值搜索，子树展开逻辑不动。**同日口径修订（2026-09-24 晚，用户定稿）**：
  过滤器收窄为**只列 host/domain（IP/域名）**——「黑板过滤只能过滤域名和 IP」；
  子树展开逻辑保留，url/service 叶子 finding 仍随父根命中。
- **拍板补充**：D5 零子节点按推荐默认（叶子 explicit 不派生）；其余待拍板项
  按文内推荐默认直接实施。
- **测试**：test_cdn.py（8）+ test_coverage.py（12）+ test_blackboard.py
  （+1）+ test_rebuild_asset_trees.py（3，monkeypatch DNS 零触网）；
  全量回归 1071 passed。

## 0. 拍板记录

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 树形态 | **host(IP) 为根 → domain 子节点 → url/service 再挂**（用户提出）；机制大半已存在（register_asset 自动 DNS 挂树） |
| D2 | CDN 场景 | CDN 下**域名保持根行、不并入 IP**（共享 IP 不代表资产相关）；判定口径 ❓ 待拍板，推荐见 §2.1 |
| D3 | 根节点 clean | **读时自动派生**（effective_status），不由 AI 显式设置；`set_asset_status` 对有子资产节点写入终态一律拒绝 |
| D4 | 新增资产破坏 clean | 读时派生天然成立——新子节点非终态，根 effective 立即非 clean，无需事件联动 |
| D5 | 零子资产根节点 | ❓ 待拍板，推荐：**保持 AI 显式管理、不自动派生**（防「ping 通=干净」） |
| D6 | 存量数据 | 一次性回填脚本：重解析挂树（含 CDN 判定）+ 有子根行显式终态重置；默认 dry-run |
| D7 | 黑板过滤器 | 初版放开四类；**2026-09-24 晚修订：只列 host/domain（IP/域名），按值搜索；子树展开逻辑不动** |
| D8 | 批次切分 | M1（归并+CDN）+ M2（派生+存量）+ M3（过滤器）**一批落地** |

## 1. 背景与问题

assessment-20260915-7d70 中 **152 个 domain 标 tested_clean，92 个由同一 Agent 会话两分钟内批量标完**，实际探测深度仅「根路径 GET（443/80 各一）」，其中 56 个根路径返回 488/403/404 即收口。根因有二：

1. **根（IP）行 status 是 AI 可写普通列**，服务端对 tested_clean 的强制只有 note 非空，无证据校验；
2. **存量树是断的**：fake-ip 清理、导入列映射错误等历史使大量 domain 行为 `parent_id=NULL` 根行，归纳结论直接写在域名行上，无法在「新资产入树」「平台出新洞」时被统一推翻。

## 2. 设计

### 2.1 树归并与 CDN 判定（M1）

新增纯逻辑模块 `core/blackboard/cdn.py`（零 bb 依赖，仿 assets.py 风格）：

- `is_cdn(ip, domain, cname=None) -> bool`，三信号任一命中即 CDN：
  1. **CNAME 后缀**命中清单（如 `.kunlun*.myqcloud.com`、`.cdn` 厂商后缀）；
  2. **IP 命中 CDN CIDR 清单**；
  3. （远期可选，本期不做）TLS 证书 SAN 跨域特征。
- 清单落点 ❓ 待拍板，推荐：**`packs/data/cdn_ranges.json` 随包基线**（provider/cidr[]/cname_suffix[]，与 fpdb_seed.json 同目录、同「数据文件」定位）+ `config/cdn.json` 用户增补覆盖层；doctor 加 JSON 解析体检（坏文件 fail-fast，同 route_index 口径）。
- **拿不准默认非 CDN 还是默认 CDN？** 推荐：信号不足时**不并**（域名保持根行，宁严勿绑），人工可用 `meta.cdn=true/false` 在资产行覆盖判定。

接线：

- `register_asset`：domain 解析成功后先过 `is_cdn`，命中则保持根行（不挂 host），其余流程不变；
- **DNS 漂移重挂**：解析结果 IP 变化（含 CDN 状态翻转）→ `set_asset_parent` 改挂 + `asset.reparent` 事件，旧宿主上的结论不带走；
- CDN host（IP）行若已被其它资产引用，保持现状不删。

### 2.2 根状态读时派生（M2，核心）

新增唯一口径函数（建议放 `core/coverage.py` 并由 API/编排器/前端数据接口共用）：

```
effective_status(asset, by_id, findings_by_asset) -> (status, basis)
```

规则：

- **叶子节点（无子资产）**：返回显式 status（现状完全不变）；
- **有子资产节点**：
  - 全部子节点 effective 为终态（tested_clean / na / dead_end）→ **tested_clean，basis=derived**；
  - 任一子节点非终态 → **open（basis=derived）**；
  - ❓ 混合是否显示最弱态（visited/scanning）待拍板，推荐直接 open（宁严）；
- host（IP）根的**自身端口面** = 每个已知开放端口须有 service 子节点或该 service 标 na，否则根不得派生 clean——取代现行「父借子收敛须自身 visited 佐证」的软规则（coverage.py:104）。

写限制：`set_asset_status` 服务端加门——**目标节点当前有子资产时，status=tested_clean 直接 ValueError**（「有子资产节点状态由子树派生，请流转子节点」）；visited/scanning/open 仍允许（标记根自身探测活动），但不影响 effective 终态。

消费者切换（全部改读 effective，不允许各写一套）：

- 黑板资产徽章 API 视图、coverage_report 终态计数；
- orchestrator uncovered 判定（:796-802）与 `done_count`（:837）；
- `check_target` 等只认资产存在性的消费面不受影响。

### 2.3 存量迁移（M2 配套脚本）

`scripts/rebuild_asset_trees.py`（默认 dry-run，`--apply` 落库，author=`demo-script(rebuild-trees)`）：

1. 全项目扫 `parent_id IS NULL` 的 domain → 重新解析 → `is_cdn` 判定 → `set_asset_parent` 挂树；
2. **有子资产根行**：显式 tested_clean 重置为 open + note 指迁移事件（让盘上值与 effective 一致，防绕过读口的消费者看到旧态）；
3. 叶子行一律不动；fake host 行处理沿用 normalize_dirty_domains 的经验（摘挂→删除）；
4. 环境坑：本机 Clash fake-ip（198.18.0.0/15）下重解析会造脏——脚本支持 `--resolver https://223.5.5.5/dns-query`（DoH，此前 Agent 已用此口径），默认不读系统代理。

### 2.4 黑板过滤器（M3，纯前端）

`Blackboard.tsx`：

- `hostOptions` → `assetOptions`：`assets.filter(type ∈ {host,domain,url,service})`，搜索按 value 子串；选项显示类型小标签（host 青/domain 蓝…），占位符改「搜索 IP/域名…」；
- 选中后子树展开（:193-206）与过滤逻辑零改动——已沿 parent_id 收全部后代。
- 放开后的资产下拉同时作为 `website-attack-path-graph.md` 单站攻击链图的目标选择器（host/domain 两类是图入口）。

## 3. 实施切分

| 里程碑 | 内容 | 依赖 |
|--------|------|------|
| M1 | `cdn.py` + cdn_ranges.json 基线 + register_asset 接线 + DNS 漂移重挂 | 无 |
| M2 | effective_status 派生 + 写入门 + 消费者切换 + 存量脚本（先 dry-run 后 --apply） | M1 |
| M3 | 前端资产过滤器 | 无（可与 M1/M2 同批） |

## 4. 测试（tests/）

- `tests/test_blackboard.py`（或新建 test_cdn.py）：①CIDR/CNAME 后缀命中；②拿不准不并；③人工 meta 覆盖；④DNS 漂移重挂+事件；⑤CDN domain 登记保持根行。
- `tests/test_coverage.py`：⑥全终态→根 derived clean；⑦混合/任一 open→根 open；⑧新增子节点立即破坏 clean；⑨na/dead_end 子节点不阻塞；⑩零子节点不派生；⑪端口面缺 service 根不 clean。
- `tests/test_agent.py`：⑫有子节点写 tested_clean 被 ValueError；⑬叶子节点流转不变。
- 存量脚本：⑭dry-run 输出与 --apply 幂等（tmp 项目 + monkeypatch DNS）。
- 前端无单测设施，手工冒烟（搜索域名→选中→只显该子树 findings）。

## 5. 验证

1. `E:\Miniconda3\python.exe -m pytest tests -q`：基线 1023+，全绿。
2. 在 assessment-20260915-7d70 拷贝上跑存量脚本：预期 56 个 488/403/404 域名所在根**自动掉回非 clean**，无需人工翻案；A 层（专窗深挖）根保持 clean。
3. `cd webui && npm run build`：零 TS 错误。
4. 不 commit（固定模式）。

## 6. 待用户拍板

1. **CDN 判定口径**：推荐 CNAME 后缀+CIDR 清单、拿不准不并、meta 可人工覆盖——是否同意？
2. **清单落点**：推荐 packs/data/cdn_ranges.json（随包基线）+ config/cdn.json（增补）。
3. **零子资产根节点**：推荐保持 AI 管理、不自动派生。
4. **有子根行混合态显示**：推荐直接 open（宁严），不显示 visited。

## 7. 风险

| 风险 | 对策 |
|------|------|
| CDN 清单不全致误并，结论错绑 | 拿不准不并 + 人工 meta 覆盖；清单可热更 |
| 多消费面各算一套口径 | effective_status 单一函数，coverage 为唯一事实源；出口快照测试 |
| 存量脚本在 fake-ip 环境造脏 host | 默认 dry-run + DoH resolver 选项 + 幂等 |
| 读时计算性能 | 单项目资产百级、coverage 已 O(n)；列表接口一次树遍历 |
| AI 绕过写入门（直接显式写其它状态伪装进度） | 终态只认真子树；徽章显 basis=derived/explicit |
