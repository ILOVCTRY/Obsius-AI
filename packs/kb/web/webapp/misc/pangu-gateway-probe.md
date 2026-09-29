# Pangu 网关匿名探测与 Actuator 前缀绕过

## 场景
面向 Spring/Pangu 微服务网关的开放子域资产（如 xjapi 类）。网关常暴露 Swagger 文档与 Actuator 端点，且路由前缀可绕过过滤器。

## 已验证路径
1. 测活：HTTP 响应带 Pangu 网关特征（网关 401/404 页面、网关头）→ 确认为活站，进入匿名面深测。
2. Swagger 枚举：`GET /swagger-resources/` 匿名可读，返回 `group: pangu-open-api`；再拉 `GET /open_api/v2/api-docs?group=<group名>` 可枚举到 54 个端点列表，同时泄露内部主机名（intel）。
3. Actuator 前缀绕过：`/actuator` 直连 404 时，尝试加网关路由前缀 `/pangu/actuator`、`/pangu/` 前缀可绕过前缀过滤直达 Actuator 端点（vuln）。
4. 匿名面穷举边界：管理/OAuth/customization 等路径返回 401、敏感 Actuator 子路径 404 → 用 tested_clean 收口，不强行爆破。

证据锚点：find-7769d4f5118f（Swagger 54 端点匿名可读 + 内部主机名泄露 intel）；find-0e9fde6af8e7（Actuator `/pangu/` 前缀绕过 vuln）。

## 坑
- 父域服务端懒派生：子资产全终态后父域仍 open，手动 tested_clean 被服务端拒（本任务连续 7 次被拒）；含子资产父节点只能自动派生，应记 known exception 后等待异步派生，不要重复提交。
- 死 vhost 特征：nginx 死 vhost 会 HTTP/HTTPS 全路径 404/403，直接 tested_clean，无需深挖。
- 发现去重：先查黑板再落发现，重复登记会产生撤回噪声；同一资产先落黑板再收口。
