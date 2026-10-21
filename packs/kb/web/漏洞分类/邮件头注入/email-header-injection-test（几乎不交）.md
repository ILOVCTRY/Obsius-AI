---

phase: webapp
vuln_class: [smtp, injection]
title: 邮件头注入测试
summary: 邮件头注入没进号/没改密默认不写；重置链走 authbypass 手册。
report_policy: rarely
---

# email-header-injection-test（几乎不交）

> 邮件头注入没进号/没改密默认不写。重置链走 `web/漏洞分类/认证绕过/认证绕过（登录-会话-密码重置-2FA）.md` + `web/漏洞分类/Host头注入/Host 头注入与密码重置毒化.md`。
