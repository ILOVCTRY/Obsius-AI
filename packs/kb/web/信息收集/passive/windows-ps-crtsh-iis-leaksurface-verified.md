# Windows 宿主被动侦察与 IIS 泄露面单发清单（zut.edu.cn 会话实测沉淀）

## 已验证路径

### 1. crt.sh 证书透明枚举（Windows PowerShell 宿主，被动无噪声）
```powershell
curl.exe -s "https://crt.sh/?q=%25.<domain>&output=json" -o "$env:TEMP\ct_<domain>.json"
$j = Get-Content "$env:TEMP\ct_<domain>.json" -Raw | ConvertFrom-Json
$names = foreach($e in $j){ $e.name_value -split '\n' }
$names | Sort-Object -Unique
```
- 实测：`%25.zut.edu.cn` 返回 91875 字节 JSON，去重后 TOTAL_UNIQUE=7（含 job.zut.edu.cn 等）。
- 验证用 `$LASTEXITCODE` + 文件字节数双重确认产出（EXIT=0 / 91875）。

### 2. vhost 专属绑定判定
- 同 IP 双端口业务分化（443=GBK 静态迎新指引页；80=Unity WebGL 虚拟仿真，2018 构建），IP 直连 80 返回通用 IIS 404 而非应用页 → 可判定该端口为域名专属 vhost。此差异法已验证，可复用于同架构站点。

### 3. ASP.NET 泄露面单发清单（≤10 候选，逐个单发，全负即记终态）
- trace.axd→403（trace 关闭）；elmah.axd、elmah.axd/detail、/elmah 未命中；web.config + 4 备份变体 + global.asax.bak 未命中；/css/→403 无目录列表；TRACE→501。
- customErrors=On 的旁证：404 为自绘页，响应体不含物理路径与 .NET 版本特征。全部候选清零后可直接关闭该面，不重复扫描。

## 坑

### PowerShell 宿主命令形态
- `curl` 是 Invoke-WebRequest 别名，`-o` 不落文件；`%errorlevel%` 是 cmd 语法，在 PS 中字面输出（首次执行回显 "EXIT=%errorlevel%" 字面量）。必须显式 `curl.exe` 并用 `$LASTEXITCODE` 判定成败。

### 目标特异死路（勿重试）
- IIS 7.5 短文件名 oracle：[a-z0-9] 全 36 字符 × `*~1*` 模式穷举零命中（find-302c50ae74f7）。该目标已生效短名保护或目录不满足条件，标记死路。
- 迎新指引页为 GBK 纯静态 HTML：无表单、无 aspx 直链，不存在可继续深入的注入/参数面；GBK 编码解析时注意按响应 charset 解码再提链。
