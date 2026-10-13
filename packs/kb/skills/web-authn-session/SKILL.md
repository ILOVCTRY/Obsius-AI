---
name: web-authn-session
description: 认证与会话攻击入口：登录/鉴权/JWT/OAuth/SAML/验证码/找回改绑等面的特征路由与对照表
keywords: 登录, 认证, 鉴权, jwt, oauth, saml, session, 会话, cookie, token, 弱口令, 爆破, 验证码, captcha, 找回密码, 改绑, 注册, 短信, otp, 2fa, 401, 403, 未授权
features: has_user_system, has_oauth, returns_401
vuln_classes: auth-bypass, jwt, oauth, saml, session, weak-credential
task_types: exploit, recon
mode: self-contained
---

# web-authn-session —— 认证与会话攻击路由

> 分层纪律：本技能只做**认证面特征 → 手册对照**。方法论在 src-strike 快照
> （`playbooks/参考资料/`），弹药在 `playbooks/`。按需 `skill_open(path=…)` 单篇，
> 禁止通读。红线提醒：弱口令默认 ≤10 候选慢速单发（领域红线 5）；
> 禁止登出/注销用户提供的登录态（领域红线 2）。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`playbooks/参考资料/` 下） | 打法/弹药（`playbooks/` 下） |
|---|---|---|
| 登录页/鉴权逻辑整体 | `references/web/webapp/authbypass/手册.md` | `arbitrary-x-authz.md` |
| 401/403 接口 | `references/web/webapp/401-403-bypass/手册.md`（path/METHOD/头现场改） | — |
| JWT（none/弱密钥/混淆） | `references/web/webapp/jwt/手册.md` | `oauth-saml-jwt/12-jwt.md`、`api-rest/12-jwt-api.md` |
| OAuth 授权码/redirect | `references/web/webapp/jwt/手册.md` | `oauth-saml-jwt/10-oauth-redirect.md` |
| SAML / SSO | `references/web/webapp/jwt/手册.md` | `oauth-saml-jwt/11-saml.md` |
| 认证杂项（session/cookie/记住我） | `references/web/webapp/jwt/手册.md` | `oauth-saml-jwt/13-auth-misc.md` |
| 验证码（图形/滑块/短信） | `references/web/webapp/captcha-ocr/手册.md` | — |
| 类型戏法/松散比较绕过 | `references/web/webapp/type-juggling/手册.md` | — |
| 短信/邮件轰炸类 | `references/web/webapp/logic/手册.md` | — |

## 纪律

- 先分清「登录页」与「业务 API 401」：前者走认证手册，后者先试 401-403 bypass。
- 改绑/改密类验证过了**立刻改回**，不留死局。
- 逻辑类漏洞（支付/优惠券）不在本技能——走 web-strike-entry 的 has_pay 路由。
