# 目录枚举软 404 oracle（ASP.NET Core / IIS）

> 已验证路径，沉淀自 yqgx.zut.edu.cn 会话（task-252ba40b53f2）。场景：IIS/8.5 + ASP.NET Core，仅 80 单点。

## 问题：为什么 200 ≠ 命中

ASP.NET（MVC/Core）+ IIS 有两个常见陷阱，会让 dir-enum 工具全部误报：

1. **站点级统一回退（fallback 路由）**：不存在的路径不返回 404，而是
   `302 → /Home/Error?msg=<文件名不存在>`（跟随重定向后为 ~23745B 统一错误页）。
   工具把「跟随后的 200」当命中 → 100% 误报。
2. **IIS requestFiltering 隐藏段**：`/web.config`、`//bin/` 等被隐藏段规则拦截，
   返回 403/404.x（随配置变化）——这是加固表现，不是「文件不存在」，也不是真实命中。

## 已验证 oracle（单发、不跟随重定向）

```bash
curl -sS -o /dev/null -w '%{http_code} %{size_download}' http://target/<候选路径>
```

判定表：

| 响应 | 结论 |
|------|------|
| `302 + 0B`（Location: /Home/Error?msg=…） | 软 404，**未命中**，换下一个候选 |
| `200/206/403 + 实体内容` | 信号 → 人工核实内容真实性后再定性 |
| 跟随后得到统一 23745B 错误页 | 未命中（走了 fallback） |

PowerShell 等价：`Invoke-WebRequest -MaximumRedirection 0`（302 会抛异常，catch 后读 Location）。

## 坑

- 初筛阶段禁用浏览器/跟随重定向的扫描器：跟随后的统一页是 200，全量假阳性。
- IIS 隐藏段 403/404.x ≠ 命中；上报前必须拿到真实内容特征（源码片段/配置键名等）。
- 若对象 ID 为 GUID（无邻接性），IDOR 枚举天然死路——直接转权限差分验证
  （读/列表差分，最小伤害），不要浪费单发去枚举 GUID。

## 实证

- yqgx.zut.edu.cn：swagger/json 10 候选 + 目录/备份 14 候选全部经本 oracle 终止
  （302 软 404）；/web.config 与 //bin/ 命中 IIS 隐藏段过滤，归为加固非发现
  （死路记账 find-b997288eebb0）。全程零误报、零重复探测。
