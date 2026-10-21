# run_cmd 网关预展开 $变量 陷阱与规避（B5 实测）

> 2026-09-17，zut.edu.cn B5 批次实测经验。适用于所有经 `run_cmd` 执行 shell 命令的会话。

## 现象

经 `run_cmd`（runtime=wsl/host）提交的 bash 命令中，**shell 变量会在到达 bash 之前被外层执行层预展开**：

```bash
bash -lc 'UA="Mozilla/5.0"; for u in https://a/ https://b/; do curl -A "$UA" "$u"; done'
```

实测结果：`for u in` 循环跑了两次，但 curl 收到的 URL 与 UA 为**空串**（变量在外层已被展开为空）。
本会话中一次 8 路径目录枚举因此全部打到根路径（8 发无效请求，200/3141 全同），浪费频控预算且无产出。

## 规避（验证有效）

1. **禁用 shell 变量**：所有参数字面量内联展开。
2. **批量/循环逻辑改用 `python3 - <<PYEOF ... PYEOF` heredoc**（无 `$` 内容则完全安全），
   用 urllib/ssl 完成带频控、异常分类（HTTPError/URLError）、标题与属性提取的枚举，输出单行/路径。
3. TLS 证书信息继续用 `openssl s_client | openssl x509 -noout`，无变量即可。
4. DoH 双源交叉验证用独立 curl 命令逐条写，URL 无变量。

## 额外观察

- `bash -lc '...'` 外层单引号 + 内层双引号的结构本身**可正常到达 bash**（循环、sleep、重定向均执行），
  仅 `$var` 引用被吃——所以"命令没跑"与"变量没值"要区分：脚本跑了，参数空了。
- wsl runtime 下 `getent hosts`/`getent ahostsv4|v6` 可用于双栈 DNS 核实；
  IPv6 无路由时表现为**立即** connect 失败（curl exit 7），而 IPv4 被防火墙 DROP 时为**超时**（exit 28）——
  两者可区分主机级 ACL 与本机路由缺失。
