# Seafile Web API 匿名指纹与外网单发侦察（已验证）

来源：task-d601145b35d7（2026-09-19 外网实测）；CT 枚举部分来自同项目 task-56f524382d15 的命令结果。

## 已验证路径

### 1. CT 日志枚举
- `curl.exe -s "https://crt.sh/?q=%25.zut.edu.cn&output=json" -o "$env:TEMP\crtsh_zut.json"`：`%25` 为通配符 URL 编码，91KB JSON 约 5s 返回，exit 0。
- 解析：`ConvertFrom-Json` 后逐记录取 `name_value` 按换行拆分再去重 → 唯一子域 7 个（含 job.zut.edu.cn）。

### 2. Seafile API 匿名指纹（零凭据、单发）
- 钉扎解析：`--resolve seafile.zut.edu.cn:443:202.196.32.115`，单 IP 单发，避免多 A 记录漂移。
- `/api2/ping/` → HTTP 200，body `pong`：匿名可达，可作 Seafile API 面存活与指纹确认。
- `/api2/auth/ping/` → 匿名 403；携带伪 token → 401 Invalid token：无需凭据即可区分匿名拒绝层与 token 校验层，确认鉴权边界存在。
- 全程未登录、未爆破、未猜凭据，各端点单发，噪声预算内。

### 3. 资产状态机终态纪律
- host 202.196.32.115 / domain seafile.zut.edu.cn / url-443 / url-8080 / service:8080 五资产均带 verified 发现 → 终态一律 visited+findings，不得标 tested_clean；终态已同步 find-3c34c9b6d0a3 死路记账矩阵。
- url-8080 由 open 流转为 visited：端口 open 状态需在拿到实测响应后才升级为 visited。

## 坑（PowerShell 环境）
- `curl` 在 PowerShell 中是 `Invoke-WebRequest` 别名，`curl -s ...` 语义错误 → 必须显式调 `curl.exe`（修正后 exit 0、文件 91875 字节落地）。
- `echo EXIT=%errorlevel%` 是 cmd 语法，PowerShell 下原样字面输出不展开 → 用 `$LASTEXITCODE`。