---
name: Web认证会话手法
description: 认证与会话攻击入口：登录/鉴权/JWT/OAuth/SAML/验证码/找回改绑等面的特征路由与对照表
keywords: 登录, 认证, 鉴权, jwt, oauth, saml, session, 会话, cookie, token, 弱口令, 爆破, 验证码, captcha, 找回密码, 改绑, 注册, 短信, otp, 2fa, 401, 403, 未授权
features: has_user_system, has_oauth, returns_401
vuln_classes: auth-bypass, jwt, oauth, saml, session, weak-credential
task_types: exploit, recon
mode: self-contained
---

# Web认证会话手法 —— 认证与会话攻击路由

> 分层纪律：本技能只做**认证面特征 → 手册对照**。方法论在 src-strike 快照
> （`web/漏洞分类/`），弹药在 `web/实战打法/`。按需 `kb_open(module=…)` 单篇，
> 禁止通读。红线提醒：弱口令默认 ≤10 候选慢速单发（领域红线 5）；
> 禁止登出/注销用户提供的登录态（领域红线 2）。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`web/漏洞分类/` 下） | 打法/弹药（`web/实战打法/` 下） |
|---|---|---|
| 登录页/鉴权逻辑整体 | `web/漏洞分类/认证绕过/authbypass-authentication-flaws.md` | `web/实战打法/任意 X 子授权——比 IDOR 更狠的权限维度漏洞.md` |
| 401/403 接口 | `web/漏洞分类/401与403绕过/401-403-bypass（禁开磨登录 HTML）.md`（path/METHOD/头现场改） | — |
| JWT（none/弱密钥/混淆） | `web/漏洞分类/JWT/OAuth、JWT、SAML 认证安全测试.md` | `web/实战打法/oauth-saml-jwt/12-JWT payload 库.md`、`web/实战打法/api-rest/12-JWT 安全(API 视角补充)payload 库.md` |
| OAuth 授权码/redirect | `web/漏洞分类/JWT/OAuth、JWT、SAML 认证安全测试.md` | `web/实战打法/oauth-saml-jwt/10-OAuth、开放重定向 payload 库.md` |
| SAML / SSO | `web/漏洞分类/JWT/OAuth、JWT、SAML 认证安全测试.md` | `web/实战打法/oauth-saml-jwt/11-SAML payload 库.md` |
| 认证杂项（session/cookie/记住我） | `web/漏洞分类/JWT/OAuth、JWT、SAML 认证安全测试.md` | `web/实战打法/oauth-saml-jwt/13-通用认证、会话、密码重置、2FA payload 库.md` |
| 验证码（图形/滑块/短信） | `web/漏洞分类/验证码识别/图形验证码 OCR 破解（ddddocr）— 解锁登录爆破链路.md` | — |
| 类型戏法/松散比较绕过 | `web/漏洞分类/类型混淆/PHP 类型混淆（松散比较）全手法.md` | — |
| 短信/邮件轰炸类 | `web/漏洞分类/业务逻辑/逻辑漏洞测试手册.md` | — |

## 纪律

- 先分清「登录页」与「业务 API 401」：前者走认证手册，后者先试 401-403 bypass。
- 改绑/改密类验证过了**立刻改回**，不留死局。
- 逻辑类漏洞（支付/优惠券）不在本技能——走 外网打点入口 的 has_pay 路由。
