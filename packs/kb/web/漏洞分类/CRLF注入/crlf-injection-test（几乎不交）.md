---

phase: webapp
vuln_class: [crlf, injection]
title: CRLF 注入测试
summary: 纯 CRLF 头注入默认不写；Host 毒重置走 host-header 手册。
report_policy: rarely
---

# crlf-injection-test（几乎不交）

> 纯 CRLF 头注入默认不写。Host 毒重置走 `web/漏洞分类/Host头注入/Host 头注入与密码重置毒化.md`。Ghost Bits 邮件/走私用公式 `chr((k<<8)|T)`，见 `web/漏洞分类/幽灵位截断/Ghost Bits Cast 攻击测试.md`。
