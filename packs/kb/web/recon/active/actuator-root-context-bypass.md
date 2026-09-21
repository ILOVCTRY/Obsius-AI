# Spring Boot Actuator 根上下文探测（绕过边缘 WAF 的 /actuator 前缀拦截）

## 场景

反向代理 / 边缘 WAF（如高校 ESOP 融合门户、Spring Cloud Gateway 前置 WAF）常对
`/actuator*` 路径做统一拦截（典型特征：固定长度 403 自定义错误页，本任务中为 769B，
对 `/.git/HEAD`、`/.env`、`/web.config` 返回同一页）。但后端 Spring Boot 应用可能把
management endpoints 直接挂在**根上下文**（management.endpoints.web.base-path=/ 或网关自身 actuator），
导致 WAF 规则被绕过。

## 本任务实证（zut.edu.cn，2026 年）

- `GET https://apis.zut.edu.cn/actuator/health` → 403 / 769B（WAF 拦截）
- `GET https://apis.zut.edu.cn/health` → 200 `{"status":"UP"}`
  Content-Type: `application/vnd.spring-boot.actuator.v1+json`（3/3 稳定）
- `GET /info` → 200 `{}`
- `GET /env /beans /mappings /configprops /metrics /routes` → 401 Spring Security JSON
  （`{"status":401,"error":"Unauthorized","message":"Full authentication is required..."}`）
  → 端点存在但需认证，仍记录为攻击面
- `GET /gateway/routes`、`/actuator/gateway/routes` → 404 → gateway 管理端点未暴露，
  CVE-2022-22947（Spring Cloud Gateway Actuator RCE）利用面大概率不存在

## 探测要点（小字典，单发低频，仅 GET）

1. `/actuator/health` 与 `/health` **都要打**；后者 200/401 即命中根上下文暴露。
2. 同一主机对 `.env`/`.git/HEAD` 返回**完全相同长度与正文的 403** = 边缘 WAF 特征页，
   不代表后端路径不存在，需换无前缀路径名复测。
3. 端点存在判定：Content-Type 含 `vnd.spring-boot.actuator`，或 401/403 响应体为
   Spring 标准错误 JSON（与 WAF 静态页区分）。
4. 根上下文最小端点集：`/health` `/info` `/env` `/beans` `/mappings`
   `/configprops` `/metrics` `/routes` `/loggers` `/threaddump`。
5. `/health`、`/info` 未授权通常只算低危信息泄露（定级 low/info）；
   真正高危是 `/env`（配置/密钥）、`/jolokia`、`/gateway/*`（22947）未授权——
   401 时记录为待验证攻击面交 exploit 轨，不尝试爆破/绕过写操作。

## 补测实证（apis.zut.edu.cn ESOP 网关，2026-09-18 会话）

- **端点状态四分法**（本会话 3/3 实测，建议并入"探测要点"使用）：
  - 200 = 未授权开放（看回包有无信息）；
  - 401 Spring Security JSON = 端点存在但需认证（记待验证攻击面）；
  - 404 Boot 错误 JSON（`{"timestamp":...,"status":404,...,"path":"..."}`）= 未映射；
  - **406 + `Content-Type: application/octet-stream` + 0 字节空体 = 已映射但被硬化**。
    实测对象 `/heapdump`：HEAD、GET(Accept: */*)、GET(Accept: application/octet-stream)
    三发均 406 空体（3/3），响应带头 `X-Application-Context: esop-gateway:8006`
    证明来自后端 Spring 而非边缘。此形态既不是 WAF 403 静态页也不是认证面，
    结论=端点存在但拒绝吐数据；**不做头部/编码绕过试探**，直接死路收账。
- **Boot 1.x / 2.x 端点名差异**：`/threaddump` 是 Boot 2.x 命名，Boot 1.x 用 `/dump`。
  实测 Boot 1.x 目标打 `/threaddump` → 404 属预期（命名自洽），不要误判为
  "存在未测端点"；对 Boot 1.x 目标最小端点集中该位置换 `/dump`。
- **Boot 1.x 确凿指纹**（免利用、看错误包即可定版）：
  - 错误 JSON `timestamp` 为毫秒整数（如 1789730447587）→ Boot 1.x；
    Boot 2.x 为 ISO 字符串（如 "2026-09-18T11:13:38.000+00:00"）。
  - 响应头 `X-Application-Context: <app>:<port>` → Boot 1.x（Boot 2.x 已移除该头）。
  - actuator 回包 Content-Type `application/vnd.spring-boot.actuator.v1+json` → Boot 1.x。
- **CVE 适用性口径**（指纹→判据，禁利用）：Boot 1.x + `X-Application-Context` 世代
  = Spring Cloud Netflix Zuul 网关，Spring Cloud Gateway 系 CVE
  （CVE-2022-22947/22946，均需 SCG + gateway actuator）**架构性不适用**；
  CVE-2018-1270（messaging）、CVE-2017-8046（data-rest）、CVE-2018-1196（devtools）
  需对应端点/端口证据，无证据不构造探测。

## 边界

- recon 阶段只做 GET 无害端点；禁止 POST `/actuator/gateway/refresh`、`/restart`、
  `/env`（会改状态），这些只能由 exploit 轨在明确授权下做。
- 遵循 EDUSRC 无害化：证明端点存在与认证状态即可，不读取业务数据。
