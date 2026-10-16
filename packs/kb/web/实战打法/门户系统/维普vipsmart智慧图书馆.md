# 维普 vipsmart / dalib 智慧图书馆门户 — 测试点

> 适用：高校/机构图书馆门户、学科空间、读者中心等使用维普 vipsmart（dalib3/dalib 系列）系统。

## 适用条件（指纹识别）

- 首页为 Vue SPA，`Server: volcclb`（火山引擎 LB）。
- HTML 引用 `vipsmart-dalib3-skin.css`、`cdn/public/js/ApplicationBuilder.js`、`window.APP_CODE`（scenemanage 等）、`fp.min.js`（fingerprintjs）。
- `ApplicationBuilder.js` 含配置：`.configureApiBase('https://<host>/gateway')`、`.configureCasBase('https://<sso>/cas')`、`OrgTokenLink: 'https://<host>/jwt/api/Auth/AccessToken'`、`orgCode: '<学校码>'`。
- 前端 `API_ENV_FORWARD_LIST` 定义逻辑接口到 `web`（读者端）/`mgr`（管理端）双命名空间映射：`/api/x` → `/web/api/x`（读者）/ `/mgr/api/x`（管理）。

## 复现步骤（API 面提取）

从 `cdn/public/js/common.js`（通常 1MB+）与 `ApplicationBuilder.js` 提取全部端点：
```bash
grep -ohE '\{origin:"/[a-zA-Z0-9_/-]+/api/[A-Za-z0-9_/.-]+"' common.js | sort -u
grep -ohE '"/api/[A-Za-z0-9_/.-]+"' *.js | tr -d '"' | sort -u
```
典型 app：usermanage（读者/用户）、appcenter、scenemanage、useridentify、articlesearch、insightresearch、subjectspace、workerspace、courseresourcecenter、docengine、smartopen、readerreport、datagateway。

## 复现步骤（匿名 BasicToken）

```http
POST /jwt/api/Auth/AccessToken HTTP/1.1
Host: <host>
Content-Type: application/json

{"orgCode":"<学校码>"}
```
- 无需任何凭据即返回 RS256 JWT（payload 仅 `{OrgCode,nbf,exp,iss:"SmartLibrary.IdentityCenter",aud:"WebApi"}`）+ refreshToken。
- **这是设计上的匿名浏览令牌，不是认证绕过**：仅能访问公开门户接口；读者 PII/管理接口仍 403。
- 令牌严格校验（仅收 orgCode，字段注入如 `{"orgCode":"x","UserId":"1"}` 返回「非法参数」400）。
- 判级口径：令牌本身不构成漏洞（低危 intel）；风险点为对外暴露内部网关 API 面。

## 复现步骤（网关鉴权矩阵）

用匿名令牌遍历端点矩阵：对每个端点 GET/POST 各打一次，按状态码分类：200+"statusCode":200 且 data 非空→可读；403→受保护；404→未部署；500→路由存在但方法/参数错误。

实测规律（vipsmart 系）：
- 公开可读(200)：`appcenter/web/api/baseinfo/getbaseinfo`（机构信息）、`subjectspace/api/front/getCollegeSpaceList`、`articlesearch/api/search-const/*`、`subjectspace/api/spaceStat/docAssetsByType`。
- 受保护(403)：`usermanage/api/reader/reader-info`、`reader-card-list-data`、`account-claim/list`、`user/is-see-sensitive-info`、`workerspace/api/space/front/*`。
- 未部署(404)：smartopen/*、readerreport/*、datagateway/*（前端有代码但网关未路由）。

## 已验证路径（重点检查项）

1. **mgr 命名空间是否被匿名令牌接受**：`/gateway/{app}/mgr/api/...` 若返回 200（而非 403），说明网关未按 mgr 前缀强制管理员鉴权（未授权访问弱点）。
2. **写接口**：`appcenter/api/baseinfo/savelink` 匿名令牌可写（返回新 GUID），无令牌 401 → 未授权写（低危）。
3. **硬编码密钥**：common.js 常含 `DECRYPT_KEY="<硬编码>"` + `DECRYPT_API_LIST=[读者 PII 接口]`，用于客户端解密读者证件/账户信息。是信息泄露线索（需结合 403 的读者接口判断可利用性）。
4. **Swagger UI 暴露**：`/gateway/{app}/` 与 `/gateway/{app}/swagger-ui/index.html` 常返回 Swagger UI（spec 本身需登录，但确认了 API 存在性）。
5. **actuator**：`/gateway/actuator/env` 通常 404（网关不透传）。

## 坑（注入 / SSRF / 穿越 / 认证）

- GUID 型 id 参数（如 `scene-detail/{guid}`）严格解析，SQL 注入返回「Unrecognized Guid format」，SLEEP 载荷被 WAF 403。
- url 参数（如 `personal-default-scene?url=`）经服务端路径校验拒绝（返回 499「路径为【...】」），无法 SSRF。
- `/file/`、`/cdn/` 的 `../` 变体由前置 nginx 400 拦截。
- 本域不承载口令校验，认证委托 CAS（`<sso>/cas`）；所有登录路由返回同一 SPA 外壳，无本地登录表单 → 无本地弱口令面（**不爆破 CAS**，红线禁止）。

## 判级参考（EDUSRC）

- 匿名 BasicToken 本身：低危 intel（公开数据）。
- mgr 命名空间被匿名令牌触达且返回敏感数据：中/高危（垂直越权）。
- savelink 等未授权写（非核心）：低危。
- 硬编码读者 PII 解密密钥 + 可读到密文：中危（敏感信息）。
