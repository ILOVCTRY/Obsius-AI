# Webplus（思迪 / Sudy）CMS 黑盒打点手册

> 来源：2026-10 东南大学 wx.seu.edu.cn / bwc.seu.edu.cn 实测（EDUSRC）。
> 适用：`/_js/sudy-jquery-autoload.js`、`sudy-wp-siteId=`、`/_web/_search/api/`、
> `/_upload/tpl/<xx>/<xx>/<tplid>/`、`X-Application-Context: application`（Spring Boot 后端）。

## 1. 指纹

- HTML 含 `class="webplus-main"`、`/_js/sudy-jquery-autoload.js`、`sudy-wp-siteId=<id>`、
  `sudy-wp-context=""`、`/_upload/tpl/xx/xx/<tplid>/`。
- 后端 `X-Application-Context: application`（Spring Boot）；`Set-Cookie: <uuid>=<hex>`。
- `/_web/<any>/` 301 → `http://<host>/_web/<any>/main.psp`（PSP 通用处理器，非文件系统）。
- 后台：`/_web/_ids/login/main.psp`（登录控制台，常按客户端 IP 白名单，返回「访问地址不合法（003）」）。

## 2. 搜索接口（核心）

两段式：
- 结果页：`POST /_web/_search/api/search/new.rst?locale=zh_CN&_p=<b64url>`，
  body `keyword=xxx`；**必须带** `Referer: https://<host>/` + `X-Requested-With: XMLHttpRequest`，
  否则 403。
- 数据接口：`POST /_web/_search/api/searchCon/create.rst?_p=<b64url>`，
  body `searchInfo=base64(JSON数组)`，字段 `[{field:"pageIndex",value:"1"},{field:"group",value:"0"},
  {field:"searchType",value:"1|2|3"},{field:"keyword",value:"..."},{field:"recommend",value:"1"}]`。
  返回 JSON `{linkSiteId,total,pages,data}`，`data` 为结果 HTML（标题/创建者/链接）。

### 2.1 `_p` 参数 = base64url（`=`→`_`）明文
`_p` 解码 = `as=<siteId>&t=<tplid>&d=<dataid>&p=<page>&m=SN&`。
- `as` = 站点 ID，`m=SN` 站点内搜索；`m=SN&|bnnColumnVirtualName=<col>&f=<col>` 栏目搜索。
- **服务端未校验请求 Host 与 `as` 的一致性 → 跨站检索**：以 A 站身份改 `as` 检索 B 站内容
  （SEU 实测 as=1 主站 77495 条、as=50 保卫处、as=95 新闻网、as=117 研招……1~140+ 均可枚举）。
- 定级：返回的是各站**公开已发布文章** → 低危（edu-rating 低危#1）；但可用于枚举
  子站/栏目清单与攻击面测绘。

### 2.2 非注入结论
`keyword` 为参数化 LIKE（单引号→0 结果且无报错，`%`/`_` 通配正常）；布尔/UNION 载荷多被
前置 WAF 403。`_p` 的 as/t/d/f 加 SQL 语法无响应差异。**不要把它当 SQLi 打**。

## 3. 其它端点

- `/_visitcountdisplay?funType=0&columnIds=<col>_<site>&siteId=<site>`（POST）→ 访问计数。
- `/_wp3services/generalQuery?Name=<name>` → 通用查询，无合法 Name 返回
  HTTP 553 `{"error":"非法请求，系统未提供Name=的通用查询！"}`（服务名需爆破，40+ 常见名未果）。
- `/_web/_ids/login/api/logout/create.rst`（PSP 错误页 JS 中引用）。
- 附件：`/_upload/article/files/<uuid>/<uuid>.<ext>` 静态 UUID 文件，**无路径参数，无穿越点**。

## 4. WAF / 访问控制

- 长亭雷池（websaas.cn）：按 **User-Agent** 拦截（python-requests→403，浏览器 UA→200）；
  按路径/参数特征拦 `/actuator`、`/swagger`、SQL 关键字。
- 后台 IP 白名单：`/system/main.psp`、`/_web/_ids/*` 返回「访问地址不合法（003）」；
  应用信任 `X-Forwarded-For`（回显）但 **IP 白名单校验不可用 XFF 绕过**。
- `/actuator`、`/swagger`、`/WEB-INF/web.xml`、`/.git/config` → WAF 403。

## 5. 可测 / 不可测面清单（实测）

| 面 | 结论 |
|---|---|
| 未授权访问 | 搜索接口可跨站检索（低危）；generalQuery 需合法 Name |
| SQLi | 无（参数化 LIKE，载荷被 WAF 拦） |
| 弱口令 | 后台 IP 白名单，公网无登录面 |
| 文件上传 | 无上传点（展示型 CMS） |
| 路径穿越/文件读 | 无（附件静态 UUID） |
| 信息泄露 | 仅系统页泄露后端内网 IP（不计分） |
| XSS | 关键词在 JS 字符串内回显，未原样回显 |

## 6. 复现要点

浏览器 UA + 经代理访问；POST 搜索带 Referer + X-Requested-With；`_p` 用
`base64.b64encode(s).decode().replace("=","_")` 生成。
