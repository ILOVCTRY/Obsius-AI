---

phase: webapp
vuln_class: [hpp]
title: HTTP 参数污染（HPP）测试
summary: HTTP 参数污染本身不交；污染导致越权/注入按对应洞写。
report_policy: rarely
---

# hpp-test（几乎不交）

> HTTP 参数污染本身不交。污染导致越权/注入按那个洞写，走 `web/漏洞分类/越权/越权漏洞（IDOR-权限绕过）.md` / `web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）.md`。
