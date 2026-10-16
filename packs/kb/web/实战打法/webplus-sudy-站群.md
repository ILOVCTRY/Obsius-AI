# Webplus / Sudy(思迪) 与 博达(Bodah) 网站群 CMS · 测试点

> 场景：`.edu.cn` 高校院系/职能站群。国内高校站群主要有三套：Wisedu Visual Site Builder 9（VS9，见 `wisedu-vsb9-网站群.md`）、**Webplus/Sudy(思迪)**、**博达(Bodah)**。三者指纹、路径、接口完全不同，**同一 IP 段下可混用**（不可假设「一站群一套 CMS」）。

## 一、Webplus / Sudy(思迪) 指纹

- 静态资源：`/_js/jquery.sudy.wp.visitcount.js`、`/_js/_portletPlugs/sudyNavi/`、`/_js2/loadStyle.js`、`/_js2/grayscale.js`
- 脚本属性：`<script ... sudy-wp-context="" sudy-wp-siteId="<数字>">`（siteId 是关键标识）
- 模板路径：`/_upload/tpl/<xx>/<xx>/<tplid>/template<tplid>/htmlRes/...`
- 样式加载：`sys-path="/_upload/site/1/style/49/49.css" site-path="/_upload/site/01/eb/<siteId>/style/361/361.css"`
- 页面 URL 形如 `/<colid>/list.htm`、`/main.htm`；存在 `/_visitcount?siteId=<n>&type=1&columnId=<n>`
- 首页常被下架为「网站维护中」静态页，但 `GET /main.htm`、`/<colid>/list.htm` 仍 200（**判可达性要看栏目页，别只看首页**）

## 二、Webplus 端点与后台（实测）

| 端点 | 说明 | 实测结果 |
|------|------|----------|
| `/_wp3services/generalQuery?Name=<q>` | 通用查询服务（JSON） | 未授权可达；查询名不可枚举（返回 `{"error":"非法请求，系统未提供Name=的通用查询！","result":false,"status":-2}`，HTTP 553） |
| `/_visitcount?siteId=&type=&columnId=` | 访问计数 | 未带参返回「非法请求」 |
| `/_visitcountdisplay?funType=0&columnIds=` | 栏目计数 | 同上 |
| `/_web/_search/api/searchCon/create.rst?_p=<base64>` | **站内搜索真实接口（JSON）** | 未授权可达；POST，body `searchInfo=<base64(JSON数组)>`；见 §2.1 |
| `/_web/search/doSearch.do?_p=<base64>` | 搜索表单提交入口（splgwh 型） | 302 → `/_web/_search/api/search/new.rst?_p=...&keyword=...&t=1` |
| `/_web/_search/api/search/new.rst?_p=<base64>` | 搜索页 | 浏览器/带 Referer 可达；直接 GET 常 403 |
| `/_web/_ids/` | 统一身份认证(IDSystem) | 任意子路径 301 → `main.psp`；`/_web/_ids/login/main.psp` 返回「访问地址不合法（001/003）」提示页，页面泄露 `ipAddress` 隐藏域 |
| `/_search/main.psp`、`/search/main.psp` | 搜索 | 200 提示页 / 410 |
| `/manage/main.psp`、`/system/main.psp` | **后台入口** | `/manage/main.psp` 302 → `/login.jsp?_p=<base64>`；`/system/main.psp` 410；`_p` 解码为 `as=<siteId>&p=1&m=N` |
| `/login.jsp` | 登录页 | 常 404（应用未部署/被网关遮蔽）→ 后台无可测登录面 |
| `/<path>/` 目录 | 目录 | 301 → `/<path>/main.psp`（.psp 后缀伪静态，无目录列举） |

- 后台登录页 `login.jsp?_p=...` 404 时，弱口令面不存在，勿硬撞。

### 2.1 站内搜索接口 `searchCon/create.rst` 与 `_p` 跨站检索（实测重点）

**调用要点（缺一不可）**：
1. `_p` 为 base64（`as=<siteId>&t=<tplid>&d=<colid>&p=1&m=SN&` 之类），直接从首页搜索表单 action 抄下来。
2. POST `Content-Type: application/x-www-form-urlencoded`，body `searchInfo=<base64(JSON数组)>`；数组元素形如 `{"field":"pageIndex","value":1},{"field":"group","value":0},{"field":"searchType","value":"1"},{"field":"keyword","value":"<关键词>"},{"field":"recommend","value":"1"},{"field":4..7,"value":""}`。
3. 必须带 `X-Requested-With: XMLHttpRequest` **和** `Referer`（指向同 `_p` 的 `new.rst`）；**缺 Referer 或关键词含中文而未正确 urlencode 时 403**（安恒 DBAppWAF 行为）。
4. `searchType`：`1`=网页、`2`=图片、`3`=附件，均可跨站检索。

**越权/未授权面**：`_p` 的 `as=<siteId>` **未校验站点归属**——可在 A 站提交 B 站的 siteId，返回 B 站文章（标题/URL/创建者姓名/时间/栏目）。
- 实测：在 rwxy(siteId=16) 提交 `as=383` → 返回 splgwh/news.seu.edu.cn 文章；反向 splgwh 提交 `as=16` → rwxy 文章。
- **聚合返回的触发条件是「特定有效的主站 siteId」，不是「as 缺失或超大」**：NJU media 实测 `as` 缺失(空)→HTTP 403（WAF 拦）；`as=99999`→`total=0`；而 `as=2`（主站/门户）→返回**全站群聚合** 427681 条（远超单站，含 sme.nju.edu.cn 等子站文章），可枚举站群内容。故枚举时直接试主站 siteId（SEU≈1、NJU≈2）与已知子站 siteId。
- 注：NJU 实测该聚合与 `keyword` 无关（关键词=身份证/密码/学号 均返回同一 427681 条全量聚合），`searchType`=1/2/3 亦返回同一数据集，未额外泄露附件路径。
- 属未授权跨租户读取（多租户隔离绕过），按 `rating:edu-rating` 低危#1 判（非核心数据）。

**注入/其他面**：`keyword` 注入单引号/`and 1=1`/内联注释 → 返回 total=0 无报错（无注入证据）；`${7*7}`/`{{7*7}}` 无模板求值；`<script>` 类载荷命中的「回显」多为 WAF 403 拦截页回显请求 URL，非应用反射。

- `generalQuery` 的 `Name` 参数**不能**作为注入点：查询名不可枚举 + 带空格/引号/等号 payload 即被前置 WAF 拦（返回约 15KB 混淆 JS 拦截页）。

## 三、博达(Bodah) 站群指纹

- 应用上下文：`/<appctx>/`（如 `/njdx/`），JS：`/<ctx>/front/ui/boshan/ui.js?v=v4.0.2`（`data-appurl="/njdx"`）
- UI 组件：`/<ctx>/front/ui/` 下 `jquery/`、`layer/`、`template/`、`umeditor/`（编辑器）、`jwplayer/`、`echarts/`、`pdf/`
- 文件服务：`/DFS//file/YYYY/MM/DD/<name>?iid=<n>`、`/DFS//template/<tplid>/images/...`
- 文章 URL：`/<channel>/<sub>/<yyyymmdd>/i<infoid>.html`
- 列表页由 `/<ctx>/front/ui/page/page.js` 驱动，`initPage()` 的 `listUrl` 默认 `'/<ctx>/openapi/t/info/list.do'`（**从 page.js 提取真实接口**）

## 四、博达端点（实测）

| 端点 | 说明 | 实测结果 |
|------|------|----------|
| `/<ctx>/openapi/t/info/list.do` | 文章列表(JSON) | 未授权可达，返回 `{"pages":0,"infolist":[]}`；siteId 1-100、columnId/treeId/pagesize/pageno/hasPage、GET/POST JSON 均未取出数据（需正确业务参数） |
| `/<ctx>/openapi/t/info/detail.do?id=<n>` | 文章详情(JSON) | 未授权可达，返回 `{"detail":[]}` |
| `/<ctx>/front/authorized/read.do?infoid=<n>` | 受限文章读取 | 200 + 「500 操作异常」提示页 |
| `/<ctx>/main/preview.do?infoId=<n>` | 预览 | 302 |
| `/DFS/` | 文件服务 | 目录 403；`../`、`%2e%2e`、`..%5c`、绝对路径、`http://` 变体均 403(WAF) 或 302，未证实穿越/SSRF |
| `/<ctx>/front/ui/umeditor/jsp/{imageUp,controller}.jsp` | 编辑器上传 | GET/POST 均 302（组件不在应用上下文，仅 `*.js` 静态可达） |

- 博达在 `/njdx` 等上下文内**大量路径统一返回同一「404 页面不存在」提示页**（如 801B）——目录爆破必须先用该基线过滤，否则全站「命中」。

## 五、判级提示（EDUSRC）

- 仅暴露端点/接口可达但取不到数据 → `category=intel`（如本手册的 generalQuery、openapi 空结果）。
- `searchCon/create.rst` 的 `_p` 跨站检索能取到其他子站真实文章（标题/创建者/时间）→ 未授权跨租户读取，`category=vuln`，按 `rating:edu-rating` 低危#1（非核心数据；若命中身份证/密码等强敏感字段再升中危#2）。
- 若 openapi/read.do 能取出真实文章（尤其含个人信息）→ 未授权数据读取，按 `rating:edu-rating` 判级。
- 后台弱口令可登录 → 高危#5。

## 六、坑

1. **不要假设同 IP 段一套 CMS**：本轮 202.119.32.7 下 hbwang=Webplus、ltx=博达，指纹各异。
2. Webplus 首页「维护中」≠ 站点不可用，栏目页 `/main.htm`、`/<colid>/list.htm` 常正常。
3. Webplus 后台入口 302 的 `_p` 是 base64（`as=siteId&p=1&m=N`），但目标 `login.jsp` 常 404，勿把 302 当「后台可访问」。
4. 博达 openapi 类接口带特殊字符会被 WAF 拦（混淆 JS 页），需区分「WAF 拦截」与「真实 404」。
5. **搜索接口必须带 `Referer` + `X-Requested-With`**，否则 `searchCon/create.rst` 一律 403；中文关键词要 urlencode。WAF 的 403 拦截页会**回显请求 URL**，别把它误判成反射型 XSS。
6. `generalQuery` 返回 HTTP **553**（非标准码）且 body 是 JSON，判「不可用」时别看状态码当 404。
7. **WAF 按 User-Agent 拦截**：python-requests/curl 默认 UA 常被 403，统一改用浏览器 UA 再测（NJU 实测）。
