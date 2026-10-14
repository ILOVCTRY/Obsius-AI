# 匿名面收口：IIS/ASP.NET Core 302 软 404 Oracle 与端点语义核实

来源：task-252ba40b53f2（yqgx.zut.edu.cn / 202.196.34.250，IIS/8.5 + ASP.NET Core，仅 80 端口，全程只读单发无写操作）
关联产物：art-73e32a854eb9（软 404 oracle + 端点清单）

## 已验证路径
1. **先建软 404 oracle，再批量判死**：该站对所有未知路径统一返回 302 fallback。swagger/OpenAPI 10 候选 + 目录列表/备份 14 候选共 24 个全部命中同一 302 模式，据此一次判死收口。对 ASP.NET Core 站点，状态码不能单独作存在性判据——须先采样未知路径的 fallback 响应（Location/body 特征）作为基线，再逐一对比。
2. **匿名端点逐一核实语义，而非按字段敏感性报警**：10 个匿名可达端点核实为门户设计内公开（设备卡片、当前使用人 userName+开机时间、周预约网格、附件/公告组件、机时排行等）。设备详情页管理员实名+电话+邮箱判定为预约业务联系卡（业务设计意图），记 verified 非泄露。
3. **IDOR 收口判据**：对象 ID 为 GUID 无邻接性（顺序枚举不可行）+ 用户级对象需登录态 ⇒ 匿名面无 IDOR 实体，host/domain/url 三资产可下 tested_clean 终态。

## 坑
- **IIS 隐藏段过滤属正常加固**：/web.config 与 //bin/ 请求被 IIS 请求过滤（hidden segments）拦截，须记 FP/死路（find-b997288eebb0 即此处理），不可当存在性信号。
- **PowerShell 中 `curl` 是 Invoke-WebRequest 别名**（本会话 task-56f524382d15 实证）：cmd 写法 `echo EXIT=%errorlevel%` 在 PowerShell 下 %errorlevel% 不展开、字面输出；须显式调用 `curl.exe` 并用 `$LASTEXITCODE` 取返回码（改写后 EXIT=0，正常取回 crt.sh 91KB JSON）。
