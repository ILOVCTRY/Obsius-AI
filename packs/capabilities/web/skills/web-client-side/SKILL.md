---
name: web-client-side
description: 客户端侧漏洞入口：XSS/CSRF/CSP/点击劫持/开放跳转/悬空标记/JS 逆向/CSV 公式注入的特征路由与对照表
keywords: xss, 跨站脚本, dom, csrf, cors, 点击劫持, clickjacking, 跳转, open redirect, 开放重定向, csp, 前端, js逆向, 加密参数, sign 逆向, csv, 公式注入, 悬空标记, dangle
vuln_classes: xss, csrf, cors, clickjacking, open-redirect, csp-bypass, dangling-markup, csv-injection, js-reverse
task_types: exploit
mode: self-contained
---

# web-client-side —— 客户端侧漏洞路由

> 分层纪律：本技能只做**前端面特征 → 手册对照**。方法论在 `playbooks/参考资料/`，
> 弹药在 `playbooks/`。按需 `skill_open(path=…)` 单篇，禁止通读。
> **红线提醒：CORS 在 SRC 语境永久不挖**（领域红线 6，references/web/webapp/cors/手册.md 仅资料勿开）。

## 特征 → 手册对照表（module 路径）

| 特征 | 方法论（`playbooks/参考资料/` 下） | 弹药（`playbooks/` 下） |
|---|---|---|
| XSS（反射/存储/DOM） | `references/web/webapp/xss/手册.md` | `xss/10-xss-by-type.md` |
| XSS 过滤绕过 | — | `xss/11-bypass.md` |
| XSS 利用（打cookie/组合） | — | `xss/12-exploitation.md` |
| CSRF | `references/web/webapp/csrf/手册.md` | `logic-flaws/10-csrf.md` |
| 点击劫持 | `references/web/webapp/clickjacking/手册.md` | `logic-flaws/12-clickjacking.md` |
| 开放跳转 | `references/web/webapp/open-redirect/手册.md` | — |
| 悬空标记/信息泄露 | `references/web/webapp/dangling-markup/手册.md`、`references/web/webapp/info-leak/手册.md` | — |
| CSP 绕过 | `references/web/webapp/csp-bypass/手册.md` | — |
| JS 逆向（加密参数/sign） | `references/web/webapp/js-reverse/手册.md` | — |
| CSV 公式注入 | `references/web/webapp/csv-formula-injection/手册.md` | — |
| CORS（仅资料，SRC 不挖） | `references/web/webapp/cors/手册.md`（勿开） | `rules/cors-vuln-report-priority.md` |

## 纪律

- 前端加密参数看不懂先 JS 逆向，别拿 payload 瞎撞。
- XSS 打点前先确认存储点与触达用户（管理员后台触达才有价值）。
- 纯反射 XSS 无触达场景按平台口径评估价值再投入。

