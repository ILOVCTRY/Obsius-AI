# 苏迪(Sudy)/Webplus 高校网站群识别与外网打法

> 来源：2026-10 南开大学 international.nankai.edu.cn / gh.nankai.edu.cn 实测（EDUSRC）
> 补充：2026-10 同济大学 bridge.tongji.edu.cn 实测（EDUSRC）
> 适用条件：国内高校 `.edu.cn` 大量使用苏迪(Sudy) 网站群（Webplus 产品线）

## 一、适用条件与指纹（一眼识别）

- 路径特征：`/_upload/`（模板/文章附件）、`/_css/_system/system.css`、`/_js/_portletPlugs/sudyNavi/`、`/_js/jquery.sudy.wp.visitcount.js`、`/_js/sudy-jquery-autoload.js`
- 接口特征：`/_visitcount?siteId=..&type=..&columnId=..`、`/_web/_search/api/search/new.rst`、`/_web/_search/api/searchCon/create.rst`、`/_wp3services/generalQuery`、`/_web/_ids/login/api/...`（IDS 单点登录）
- URL 模式：栏目 `/<数字>/list.htm`；文章 `/YYYY/MMDD/c<栏目>a<id>/page.htm`；`/system/` 与 `/admin/` 均 **302 → /system/main.psp**（**通配**，任何不存在的 `/xxx/` 都这样跳）
- 模板资源：`/_upload/tpl/xx/xx/xx/templateNNN/`、`/_upload/site/N/style/`
- 搜索页 HTML 标题常含 **“Webplus 搜索”**（产品名实锤）
- 页面头部常带安全网关头：`X-Protected-By: NKSOC`（南开 SOC 网关）
- 后端组件指纹：搜索接口 `/_web/_search/api/searchCon/create.rst` 由 **Apache Tomcat** 提供（405 错误页尾部带 `Apache Tomcat/8.5.75`），响应头 `X-Application-Context: application`（Spring Boot），`Allow: POST`；`/_wp3services/generalQuery` 返回 `X-APISIX-Upstream-Status`。
- 网关层：http→https 的 301 由 **openresty + APISIX** 下发（页面 footer 含 `Powered by apisix.apache.org`）；APISIX 对 `*.git`/`*.env`/`*.svn` 等敏感文件名直接返回自己的 404（146B，nginx 风格），可用来区分“网关拒绝”与“后端拒绝”。

## 二、坑：WAF 拦截页尺寸区分（防误报）

| 响应 | 含义 |
|---|---|
| 403 + **45173B**（页面含“长亭 SafeLine 阻断”） | 长亭 SafeLine WAF 拦截，**非端点存在** |
| 403 + 199/196B 纯文本 | Apache 目录访问禁止（真实 403） |
| 404 + 196B / 404 + 549B（`404 Not Found ******`） | 真 404 |
| 404 + 118B（`<center><h1>404 Not Found</h1></center>`，nginx） | APISIX 网关层 404（敏感文件名全局规则） |
| 410 + “访问地址无效，system找不到对应的栏目” | Webplus 假路径（`/system/main.psp` 等） |
| 301 + 34B `htm file not found ,redirecting...` | Webplus 通配跳转（任何不存在的 `/xxx/` → `/xxx/main.psp`），**非端点存在** |

**判据**：看到 403 且体积 ≈45KB 一律按 WAF 拦截处理，不要当“端点存在”。SafeLine 会拦 `*.zip`/`.git`/`.svn`/`www.zip` 等敏感文件名与 SQL 关键字(`and/select/union`)。

## 三、已验证路径：静态化站群前台 → 通常无洞

苏迪站群前台默认**静态化发布**：首页/栏目/文章均为 `.htm` 静态页，动态面极少。外网可达的动态接口通常只有：

- `/_web/_search/api/searchCon/create.rst`：站内搜索 API。`_p` 参数是 **base64**（字母表 `/+=` 被替换为 `_-`，去掉 padding 时用 `_` 补齐）。解码示例：`YXM9MjI2JnQ9NDMzJmQ9MTU3OCZwPTEmbT1TTiY_` → `as=226&t=433&d=1578&p=1&m=SN&`。返回 JSON `{"total":"<N>", ...}`。实测：`_p` 内 `as/t/d` 与 `keyword/searchType/group/recommend/pageIndex/filter_writer` 加单引号 / `and 1=1|1=2` / `order by` / 时间盲注 `sleep(5)`/`benchmark` **全部无差异**——参数被安全处理，非注入点。同济 bridge 同型接口（keyword 加单引号/`and 1=1|1=2`/`sleep`）亦无稳定差分；未授权请求最终落到 `403 "抱歉，您没有权限访问该页面"` 错误页（该错误页含隐藏字段泄露内网 IP，属 EDUSRC 忽略项）。
- `/_wp3services/generalQuery`：通用查询服务，需有效 `Name` 参数。无有效 Name 返回 `503 {"error":"非法请求，系统未提供Name=的通用查询！","result":false,"status":-2}`。无参数即无数据，非未授权。
- `/_visitcount`：访问统计，带 Referer 才返回，内容为计数。

前台**没有**：登录表单、用户/账户/资源对象、文件上传点、接受 URL/路径的服务端参数、客户端可控序列化 cookie。→ unauth/authz/weakpass/upload/rce/traversal/ssrf/deserial 面基本为空；sqli/xss 仅在搜索接口，且实测无洞。

## 四、复现要点：真正有戏的方向（静态前台之外）

1. **管理后台**：苏迪后台通常**不挂在同一 vhost 的 `/system`**（那是假路径）。真实后台多为独立域名/端口（如 `webplus.nankai.edu.cn`、`:8080`），或仅校内访问（vhost 限制）。需做子域名/端口发现，不要在前台 `/system` 上耗时间。
2. **后台跳校级统一身份认证（新增实测）**：同济 bridge.tongji.edu.cn 的 `/system/main.psp` 与 `/api/main.psp` 稳定 302 → 校级 IAM OAuth2：
   `https://iam.tongji.edu.cn/idp/oauth2/authorize?grant_type=authorization_code&response_type=code&scope=base_user&client_id=SYS20230097&redirect_uri=https%3A%2F%2Fjgdw.tongji.edu.cn%2F_DoAuthLogin&state=...`
   → 说明：①后台受学校统一认证保护，**无本地口令登录面**（弱口令面为空，对 IAM 做口令测试属越界且违反红线）；②暴露 OAuth `client_id` 与回调域（本例 `jgdw.tongji.edu.cn`，即真实后台落点）；③真实后台在回调域，属**另一资产**，需单独授权后再测。
3. **`_upload` 附件目录**：历史漏洞常见「附件上传绕过」「`_upload` 目录列目录」。若目录列表开启（`/_upload/` 返回 200 目录索引）可枚举；多数目标关闭（403 或 Webplus 通配 301）。
4. **SSO/IDS 端点**：`/_web/_ids/login/api/*` 若可访问，测认证绕过/用户枚举。同济 bridge 该路径返回网关 404，不可达。
5. **弱口令**：找到真实后台（含本地口令表单）后再做，≤10 候选慢速单发。
6. **注意**：`nankai.17gz.org` 等外部 SSO 域**出 scope**，不要测。

## 五、步骤：复核出口假象 + 源 IP 封禁（新增实测）

- 高校站群常前置 NKSOC/WebVPN/APISIX，直连出口高频访问 `/system/` 可能被限流返回小页（非端点不存在）。下结论前用 **fir-proxy 代理池** 复核同一路径的状态码与体积：直连与多代理一致方可定论；HTTPS 经代理握手被断（SSLEOF/RemoteDisconnected）时以直连基线为准。
- **源 IP 频率封禁（新增实测）**：同济 tongji.edu.cn 前置 APISIX 带**基于源 IP 的请求频率限制/封禁**。执行 dirsearch（~700 请求，`--max-rate=10`）后，本机直连出口 IP 被临时封禁：目标 TCP:443 连接直接超时（curl 返回 000），而同一出口访问 `example.com` 正常（200），封禁持续 >10 分钟未恢复；目标本身在线（经 fir-proxy 代理仍可访问）。
  → **打法**：对苏迪/高校站群做目录扫描务必**低速率**（≤10 req/s，甚至更低），并**预先准备好代理通道**（fir-proxy 池）以便被封后继续复核；被封后先确认是“目标封我”而非“目标挂了”（对照 `example.com` 连通性 + 代理可达性）。
