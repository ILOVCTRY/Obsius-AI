# crt.sh CT 日志被动子域枚举（Windows host / PowerShell）——已验证

## 适用场景
纯被动侦察（零交互噪声）下，枚举已知根域的历史证书子域。task-c61bf934d5c8（*.zut.edu.cn）端到端跑通。

## 已验证步骤
1. 查询（`%25` 为通配符 `%` 的 URL 编码）：
   ```
   curl.exe -s "https://crt.sh/?q=%25.zut.edu.cn&output=json" -o "$env:TEMP\crtsh_zut.json"; echo EXIT=$LASTEXITCODE
   ```
   实测约 5 秒返回 91KB JSON（EXIT=0，91875 字节）。
2. PowerShell 解析去重：
   ```
   $j = Get-Content "$env:TEMP\crtsh_zut.json" -Raw | ConvertFrom-Json
   $names = foreach($e in $j){ $e.name_value }   # name_value 可能含换行分隔多域，需再展开
   # ... | Sort-Object -Unique
   ```
   实测 TOTAL_UNIQUE=7，含 job.zut.edu.cn。

## 坑（本任务实际踩中）
- Windows host 下 PowerShell 中 `curl` 是 `Invoke-WebRequest` 别名，`-s/-o` 等参数不匹配；必须显式 `curl.exe`。
- `%errorlevel%`、`%TEMP%` 是 cmd 语法，PowerShell 中原样回显（出现过字面 `EXIT=%errorlevel%`）；应使用 `$LASTEXITCODE`、`$env:TEMP`。
- 首版命令 exit_code=0 但实际未取到数据——不能只看退出码，必须核对输出文件大小/内容。

## 覆盖局限与后续路径（本任务验证）
- CT 日志仅覆盖历史签发证书：crt.sh 仅得 7 个 unique，DoH 全量解析指纹最终收敛 67 域——crt.sh 只是单一来源，须与 DoH/SPF 记录等并用。
- 对枚举域做 CNAME 归类收敛云面：*.zut.edu.cn 云面终态仅 2 处 SaaS——job.zut.edu.cn=CNAME→school.goworkla.cn（goworkla 多租户 SaaS，边缘 82.156.223.241 TencentCVM）、mail.zut.edu.cn=CNAME→ssl.exmail.qq.com（腾讯企邮）。多租户 SaaS 边缘 IP 不计为校方自有云资产；本任务同时验证无 OSS/COS/OBS/MinIO/七牛/Aliyun 资产、无 AK/SK/LTAI/AKIA 存量。
- 邮件认证核查（verified：find-f8a17306c747，low，edu-rating 低危#3）：主域 DMARC p=none（仅监控不强制）+ SPF ~all（软失败）→ @zut.edu.cn 发件人可仿冒，属需用户交互兑现的钓鱼/凭证采集社工入口；DKIM 常见选择器 4/4 未发布——注意"未发布≠未配置"，不作断言，记待管理员核实。
