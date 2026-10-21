---
name: Web客户端手法
description: 客户端侧漏洞入口：XSS/CSRF/CSP/点击劫持/开放跳转/悬空标记/JS 逆向/CSV 公式注入的特征路由与对照表
keywords: xss, 跨站脚本, dom, csrf, cors, 点击劫持, clickjacking, 跳转, open redirect, 开放重定向, csp, 前端, js逆向, 加密参数, sign 逆向, csv, 公式注入, 悬空标记, dangle
vuln_classes: xss, csrf, cors, clickjacking, open-redirect, csp-bypass, dangling-markup, csv-injection, js-reverse
task_types: exploit
mode: self-contained
---

# Web客户端手法 —— 客户端侧漏洞路由

> 分层纪律：本技能只做**前端面特征 → 手册对照**。方法论在 `web/漏洞分类/`，
> 弹药在 `web/实战打法/`。按需 `kb_open(module=…)` 单篇，禁止通读。
> **红线提醒：CORS 在 SRC 语境永久不挖**（领域红线 6，references/web/webapp/cors/手册.md 仅资料勿开）。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`web/漏洞分类/` 下） | 弹药（`web/实战打法/` 下） |
|---|---|---|
| XSS（反射/存储/DOM） | `web/漏洞分类/XSS/XSS 测试手册.md` | `web/实战打法/xss/10-XSS 三大类型 payload 库.md` |
| XSS 过滤绕过 | — | `web/实战打法/xss/11-XSS 绕过 payload 库.md` |
| XSS 利用（打cookie/组合） | — | `web/实战打法/xss/12-XSS 利用 payload 库.md` |
| CSRF | `web/漏洞分类/跨站请求伪造/CSRF 测试.md` | `web/实战打法/logic-flaws/10-CSRF 跨站请求伪造 payload 库.md` |
| 点击劫持 | `web/漏洞分类/点击劫持/clickjacking-test（缺头不写）.md` | `web/实战打法/logic-flaws/12-点击劫持 payload 库.md` |
| 开放跳转 | `web/漏洞分类/开放重定向/开放重定向全手法（按校验方式选绕过）.md` | — |
| 悬空标记/信息泄露 | `web/漏洞分类/悬垂标记注入/dangling-markup-test（几乎不交）.md`、`web/漏洞分类/信息泄露/信息泄露测试手册.md` | — |
| CSP 绕过 | `web/漏洞分类/CSP绕过/csp-bypass-test（几乎不交）.md` | — |
| JS 逆向（加密参数/sign） | `web/漏洞分类/JS逆向/JS 逆向配合接口挖掘指南.md` | — |
| CSV 公式注入 | `web/漏洞分类/CSV公式注入/csv-formula-injection-test（几乎不交）.md` | — |
| CORS（仅资料，SRC 不挖） | `web/漏洞分类/跨域配置错误/cors（仅技术资料 · SRC 禁用）.md`（勿开） | `web/实战打法/rules/CORS（永久 · 不挖）.md` |

## 纪律

- 前端加密参数看不懂先 JS 逆向，别拿 payload 瞎撞。
- XSS 打点前先确认存储点与触达用户（管理员后台触达才有价值）。
- 纯反射 XSS 无触达场景按平台口径评估价值再投入。
