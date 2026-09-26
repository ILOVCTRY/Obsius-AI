## 已验证路径
- 使用 crt.sh 做子域枚举时，在 PowerShell 中必须显式调用 curl.exe，避免 curl 被 Invoke-WebRequest 别名拦截，导致输出与退出码不可用。
- 对静态引导页（如 HTTrack 镜像）沿注释追踪原动态源（如 .action），可快速判断 Java 旧系统是否退役，避免无谓 OGNL/爆破尝试。
- 对既有 verified low 对象先从前端 JS 提取端点，再做匿名只读差分，实现同一对象的升链，避免空转。

## 坑
- crt.sh 查询 *.zut.edu.cn 仅返回 7 个唯一域名，单一证书透明日志源可能严重低估资产面，需结合其他被动源交叉验证。
- PowerShell 中 curl 是 Invoke-WebRequest 的别名，混合使用 cmd 的 %errorlevel% 不会解析；应改用 curl.exe 并读取 $LASTEXITCODE。