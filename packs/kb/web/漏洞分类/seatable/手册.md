---
name: SeaTable 在线表格协作平台
description: SeaTable（dtable）开源在线表格/协作平台的外网未授权面测试手册，含指纹、/api/v2.1/ 端点清单、注册/共享/外部应用鉴权特征与实测基线。
tags: [phase, vuln_class, seatable, unauth]
---

# SeaTable（dtable）未授权面测试手册

## 适用条件

SeaTable 是国内高校/企业常见的在线表格协作平台（如南京大学 `table.nju.edu.cn`「南大表格」）。
后端 = Django + DRF；前端 `dtable-web`；另有 `dtable-server`（Node/Express，socket.io）、`dtable-db`（Go，查询引擎）。
适用于外网对 SeaTable 实例做未授权/信息泄露面评估。

## 指纹与版本判定步骤

1. `GET /` → `302 /accounts/login/?next=/`。
2. 登录页 HTML：`og:title=SeaTable`、资源带版本 `?v=6.2.13`（**版本号直接读静态资源 query**）。
3. 响应头：`server: nginx`、`strict-transport-security`；CORS 常为 `access-control-allow-origin: *`。
4. 子服务探活：`/dtable-server/ping` → `pong`（Express，带 `x-powered-by`、`x-ratelimit-*`）；`/dtable-db/ping` → `{"ret":"pong"}`。

## 未授权接口探测清单（逐点 GET，观察是否 403/401）

```
/api/v2.1/admin/users/            # 403 Authentication credentials were not provided.
/api/v2.1/admin/dtables/          # 403
/api/v2.1/admin/organizations/    # 403
/api/v2.1/dtables/                # 403
/api/v2.1/workspaces/             # 403
/api/v2.1/groups/                 # 403
/api/v2.1/notifications/          # 403
/api2/account/info/               # 403
/api/v2.1/dtable/app-access-token/ # 403 {"error_msg":"Permission denied."} (GET only)
/dtable-server/api/v1/dtables/    # 403 You don't have permission to access.
/dtable-db/metrics                # 403 Invalid jwt token.
/api2/ping/                       # 200 "pong"（无信息）
/api2/auth-token/                 # 405 on GET; POST 登录
```

**判定口径**：DRF 接口未带凭据应为 `403 {"detail":"Authentication credentials were not provided."}`；
带伪造/空 Token → `401 {"detail":"Invalid token"}`。若某接口返回 200 + 业务 JSON 即为未授权泄露。

### 已试过无效的绕过
`/api/v2.1/admin/users/.`、`..;/`、`//`、`%2e`、大小写、`?format=json`；
`X-Forwarded-For: 127.0.0.1`、`X-Original-URL`、`X-Rewrite-URL` —— 均仍 403。

## 注册开放判定

- `GET /accounts/register/`、`/accounts/signup/`：**404 = 注册关闭**（高校普遍走统一身份认证）；
  200 = 注册开放 → 重点测「自注册后越权访问他人 base」。
- `/accounts/register`（无尾斜杠）可能 301→加斜杠，注意跟随。

## 共享 / 邀请链接

- 邀请链接 `/dtable/universal-app/links/<32hex>/` → 302 到
  `/external-apps/<uuid>/login/?from=invite_link&invite_link_token=<32hex>`，需统一身份认证。
- 相关 API（`/api/v2.1/dtable/universal-app/links/<token>/` 等）多为 404。
- **判定**：能无认证读到 base 行/列数据 = 未授权；仅跳登录 = 需认证。

## 登录面

- 网页：`POST /accounts/login/`（字段 `login`/`password`/`csrfmiddlewaretoken`）。
- API：`POST /api2/auth-token/`（form `username`/`password`；失败
  `400 {"non_field_errors":["Unable to login with provided credentials."]}`）。
- **无用户名枚举**：存在/不存在用户返回相同 non_field_errors（实测）。
- **next 参数**：登录页 HTML 实体转义，无反射 XSS；`/sso/?next=` 丢弃 next 只跳固定 `/oauth/login/?next=/`。
- OAuth：`/sso/` → `/oauth/login/` → 302 到统一认证 `authserver.<domain>/.../oauth2.0/authorize?client_id=...`。

## 其他面与坑

- `GET /custom-css/` 可能返回自定义 CSS（管理配置，无害）；`/media/` 禁列举（403）。
- `dtable-server`/`dtable-db` 无有效 JWT 统一 403，非未授权入口。
- 备份/源码/调试 `/.git/config`、`/backup.zip`、`/phpinfo.php`、`/__debug__/` 一般 404。

## 实测复现基线（NJU table.nju.edu.cn，SeaTable 6.2.13）

全部未授权路径 403/401；注册 404；登录面无 SQLi/枚举；上传/穿越/泄露面 404；结论=无未授权漏洞。
