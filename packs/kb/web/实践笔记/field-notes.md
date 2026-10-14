## 已验证路径：Spring Boot + Druid 监控台指纹识别

- 根路径 GET / 返回 Spring 默认 JSON 404 + `Vary: Origin` CORS 头，`/error` 返回 status=999，可确认后端为 Spring Boot。
- 探测 `/druid/login.html` 返回 200 且为 Druid 1.x 标准登录页，即可定型 Alibaba Druid StatViewServlet 监控台暴露。
- OPTIONS 请求返回 `ACAO:*` + `Allow` 全方法，佐证 nginx 反代行为。
- 同类图书馆站点若指纹不同（如汇文 OPAC 的 202.196.33.227:8080），不应复用本测试面矩阵，避免跨产品线误报。

## 坑

- TLS 通配证书 `*.zut.edu.cn` 已过期（2026-05-06），但站点仍可达；指纹识别时应忽略证书过期，不要据此判站点失效。