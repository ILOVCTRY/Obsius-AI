# 芋道 Yudao（ruoyi-vue-pro）框架未授权面

> 适用：目标为「芋道」/Yudao/ruoyi-vue-pro 系管理端（常见于高校 HPC/门户/后台，前端标题可能为 OGSP 等定制名）。
> 本轮来源：aisc.nju.edu.cn（南京大学 HPC 管理端 OGSP6），2026-10 实测。

## 1. 指纹识别

| 特征 | 说明 |
|---|---|
| `/_app.config.js` | Vite 构建的前端配置，含 `VITE_GLOB_API_URL`（默认 `/api/admin`）、`VITE_GLOB_OGMP_URL`、`VITE_GLOB_OGSP_URL` 等 |
| JS 内路径 | `/infra/config/get-site-config`、`/infra/config/get-password-strategy`、`/system/dict-data/list-all-simple`、`/system/auth/loginByPortal`、`/system/auth/sms-login`、`/oauth2/token` |
| 响应体 | `{"code":0,"message":null,"result":...}` / 鉴权失败 `424` 且 `result":"Full authentication is required to access this resource"` |
| 登录 | OAuth2 password grant 走 `POST /api/admin/oauth2/token`（Basic 客户端），或 `/system/auth/loginByPortal`（门户 SSO） |

前端 JS 提取端点：
```bash
curl -sk https://TARGET/assets/index-*.js | grep -oE '/[a-zA-Z0-9_/.-]+' | sort -u
curl -sk https://TARGET/_app.config.js
```

## 2. 未授权面（实测可复现）

### 2.1 infra/config 登录前白名单（低危，常被误报为高危）
`/api/admin/infra/config/` 下部分子接口属"登录前配置"，**未授权返回 200**：
- `get-site-config` → 站点名/logo/登录背景
- `get-password-strategy` → 密码长度/复杂度/重试锁定
- `get-forget-password` → 忘记密码开关
- `get-group-register` → 组注册开关

而**同前缀**的 `get-obj-by-key`、`get-value-by-key`、`get-config`、基路径 `/api/admin/infra/config` 均返回 **424 需鉴权**。

> 判级：仅站点名/密码策略等非敏感配置 → **低危**（edu-rating 低危#1）。只有返回数据库连接串/密钥/凭据/内部服务地址才升中高危。**不要因为"配置泄露"字样就报中危**。

### 2.2 oauth2/token 硬编码客户端凭据（低危）
前端 JS 明文：`VITE_OAUTH2_PASSWORD_CLIENT:"ogsp:ogsp"`、`VITE_PWD_ENC_KEY:"ogsp666666666666"`。
```bash
curl -sk -X POST -u "ogsp:ogsp" -d "grant_type=client_credentials" \
  https://TARGET/api/admin/oauth2/token
# 200 {"access_token":"...","token_type":"Bearer","expires_in":"43200"}
```
用该 token 访问业务接口 → **403** `WWW-Authenticate: Bearer error="insufficient_scope"`。
> 判级：凭据明文泄露+可签发 token 但 scope 不足未获数据 → **低危**。若该 client 竟可访问业务数据则升链。

### 2.3 Actuator 未授权（低危）
`/api/admin/actuator`（注意前缀）未授权 200，`_links.self.href` 泄露内部 host（如 `http://192.168.x.x/admin/actuator`）。子端点 `/env`、`/health`、`/mappings` 多为 404。
> 判级：仅根链接+内网 IP → **低危**。

## 3. 常见无效/干扰项（避免误判）

- **SPA 回落**：未匹配路径返回 200 + 首页 HTML（Content-Length 与首页一致），非真实端点，用 `-w %{size_download}` 比对首页长度排除。
- **`/ogmp-api/*` 静态占位**：未匹配路径可能返回固定 128 字节 base64 blob（每次相同 md5），非真实数据。
- 全局鉴权门：`/api/admin/**` 未带凭据统一 424，不要把它当成"接口存在"。

## 4. 测试顺序建议

1. 取 `/_app.config.js` + `index-*.js`，提取 `VITE_*` 与 API 前缀、端点清单；
2. 逐个测 `infra/config/get-*`（未授权）与 `oauth2/token`（client_credentials）；
3. 测 `/api/admin/actuator`；
4. 带 client token 试业务接口确认 scope；
5. 弱口令：password grant 若开启验证码+统一错误响应，收益低，≤10 候选即止。
