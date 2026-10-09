# fir-proxy

一个基于 Python/Tkinter 的 HTTP、SOCKS4 和 SOCKS5 代理池工具，提供代理抓取、验证、筛选、轮换、本地代理服务和健康检查功能。

## 功能概览

- 图形化管理界面：代理列表、实时日志、区域筛选和质量筛选。
- 多来源获取代理：在线 API、文本列表和网页抓取。
- 代理验证：TCP 预检、延迟、速度、匿名度和地理位置检测。
- 本地服务：提供 HTTP 和 SOCKS5 两个本地入口。
- 手动和自动轮换：支持按区域、延迟和评分选择代理。
- URL 健康检查：可自定义检查间隔，并支持多个域名、IP 或 URL。
- 故障切换：当前代理无法连接目标时，自动验证其它代理并切换。
- 导入和导出：支持 TXT、JSON、CSV 等常用格式。

## 项目结构

```text
fir-proxy/
├─ main.py                         # GUI 主程序、设置窗口和任务调度
├─ cli.py                          # 面向 AI 和自动化脚本的命令行入口
├─ modules/
│  ├─ __init__.py                  # Python 包标识
│  ├─ fetcher.py                   # 在线代理源和网页抓取
│  ├─ checker.py                   # TCP、延迟、速度、匿名度及 URL 检查
│  ├─ rotator.py                   # 代理池、状态、评分和轮换策略
│  ├─ server.py                    # 本地 HTTP/SOCKS5 转发服务和故障切换
│  └─ asset_searcher.py            # Fofa、Hunter 等资产搜索接口
├─ hq.py                           # 独立命令行代理获取脚本
├─ xdl.py                          # 另一套独立命令行代理获取脚本
├─ config.json                     # GUI 配置文件，首次运行可自动生成
├─ requirements.txt                # Python 依赖
├─ img/                            # 项目图片资源
├─ 可用代理.txt                    # 外部整理的 SOCKS5 代理列表
├─ 可用代理2.txt                   # 外部整理的 SOCKS5 代理列表
└─ *.bak / *.patch / *-verification.txt
                                   # 修改备份、差异和验证记录
```

## 核心模块说明

### `main.py`

程序入口和 GUI 控制器 `ProxyPoolApp` 所在文件，主要负责：

1. 创建主窗口、代理表格、按钮和实时日志。
2. 打开设置窗口并保存 `config.json`。
3. 调度抓取、导入、验证、重测和导出任务。
4. 将验证结果同步到代理池和界面。
5. 启动、停止本地 HTTP/SOCKS5 服务。
6. 执行周期性 URL 健康检查。

### `modules/fetcher.py`

维护在线代理源列表，并通过线程池并发获取代理。返回的数据按 `http`、`socks4` 和 `socks5` 分类，供 `ProxyChecker` 继续验证。

### `modules/checker.py`

负责代理可用性和质量验证：

- `_pre_check_proxy()`：检查代理 TCP 端口是否可连接。
- `_full_check_proxy()`：检查延迟、匿名度、速度和地理位置。
- `check_proxy_url()`：通过指定代理访问健康检查地址。
- `validate_all()`：使用线程池批量验证代理。

### `modules/rotator.py`

线程安全地管理代理池：

- 保存代理地址、协议、评分、状态和地区。
- 维护当前代理和轮换索引。
- 根据地区、延迟和评分筛选代理。
- 记录 URL 健康检查的连续失败次数。
- 连续失败达到 5 次时移除代理。

### `modules/server.py`

实现本地代理服务：

- HTTP 服务默认监听 `127.0.0.1:1801`。
- SOCKS5 服务默认监听 `127.0.0.1:1800`。
- 将客户端请求转发到当前上游代理。
- 上游连接失败时，按评分验证其它代理。
- 找到可用代理后切换当前代理并继续当前连接。

### `modules/asset_searcher.py`

封装资产搜索接口。可在设置窗口中配置 Fofa、Hunter 的启用状态、查询语句、密钥和数量。

## URL 健康检查

打开：

```text
设置 → 通用设置 → 代理URL健康检查
```

支持配置：

- 启用或关闭健康检查。
- 自定义检查间隔，单位为秒，范围为 10～86400 秒。
- 每行填写一个域名、IP 或 URL。
- 也可以使用逗号或分号分隔多个地址。

示例：

```text
example.com
1.2.3.4
https://example.com/ping
http://1.2.3.4:8080/status
```

未填写协议的地址会默认按 `http://` 处理。

每个周期内，程序使用当前代理依次访问这些地址。某个地址访问失败时，会尝试其它可用代理访问该地址；验证成功后切换当前代理。当前代理的健康检查连续失败 5 次后会从代理池中剔除。

## 配置文件

`config.json` 位于项目根目录，程序从其它工作目录启动时也会读取和保存这一份配置。它主要包含以下配置：

```json
{
  "general": {
    "validation_threads": 100,
    "failure_threshold": 3,
    "auto_retest_enabled": false,
    "auto_retest_interval": 10,
    "url_health_enabled": false,
    "url_health_interval_seconds": 60,
    "url_health_urls": "https://www.baidu.com"
  },
  "auto_fetch": {
    "fofa": { "enabled": false, "key": "", "query": "", "size": 500 },
    "hunter": { "enabled": false, "key": "", "query": "", "size": 100 }
  }
}
```

配置文件中的密钥属于敏感信息，分享项目时应先清空。

## 安装和运行

建议使用 Python 3.10 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

## 使用流程

1. 点击“导入代理”，选择 TXT、JSON 或其它支持的代理列表。
2. 或点击“获取代理”，从在线来源抓取代理。
3. 等待 TCP 和质量验证完成。
4. 在代理列表中选择代理，或点击“轮换IP”。
5. 点击“启动服务”，将其它软件配置为：
   - HTTP：`127.0.0.1:1801`
   - SOCKS5：`127.0.0.1:1800`
6. 如需自动切换，在设置中开启 URL 健康检查并填写测试地址。

## CLI 用法（推荐给 AI、脚本和自动化任务）

新增的 `cli.py` 不依赖 Tkinter 界面，统一从标准输入/文件读取代理，并把结果输出为机器可读 JSON。网络任务默认使用 JSONL：每一行都是一个完整 JSON 事件，包含 `start`、`result`、`progress` 或最后的汇总对象；运行日志写入 stderr，并自动保存到 `logs/cli-*.log`。这样 AI 可以边读取边处理，也可以只解析最后一行汇总。

查看全部命令：

```powershell
python cli.py --help
python cli.py validate --help
```

### 代理输入格式

TXT 每行一个代理，支持协议前缀和 `#` 注释：

```text
http://127.0.0.1:8080
socks5://10.0.0.2:1080
192.0.2.10:3128
```

JSON 支持数组，元素可以是 `{"protocol": "socks5", "proxy": "1.2.3.4:1080"}`，也可以是 `{"url": "socks5://1.2.3.4:1080"}`。CSV 支持 `protocol,proxy` 列，也兼容 GUI 导出的 CSV。

### 常用命令

从在线源抓取代理。默认 stdout 是 JSONL，`--output` 会把代理记录另存为 JSON、CSV 或 TXT：

```powershell
python cli.py fetch --protocol socks5 -o fetched.json
```

批量验证代理并保存验证结果：

```powershell
python cli.py validate -i fetched.json --workers 50 --timeout 5 -o validated.json
```

通过代理检查一个或多个目标 URL：

```powershell
python cli.py check-url -i validated.json `
  --url https://www.baidu.com `
  --url https://example.com `
  -o health.json
```

筛选评分最高的可用代理。`--limit` 可以返回多个结果，`--region` 和 `--max-latency-ms` 可继续收窄条件：

```powershell
python cli.py select -i validated.json --limit 5 --max-latency-ms 3000
```

把杂乱列表标准化为 JSON：

```powershell
python cli.py normalize -i proxies.txt -o normalized.json
```

启动本地 HTTP 和 SOCKS5 服务。服务默认监听 `127.0.0.1:1801` 和 `127.0.0.1:1800`，按 `Ctrl+C` 停止：

```powershell
python cli.py serve -i validated.json
```

启用按请求轮换和目标失败自动切换：

```powershell
python cli.py serve -i validated.json `
  --request-rotation-count 10 `
  --target-failover-threshold 3
```

### AI 调用约定

- 成功时进程退出码为 `0`；输入或参数错误为 `2`；运行时异常为 `1`。
- 网络命令的最后一行始终是汇总 JSON，通常包含 `ok`、`command`、`count`、`output` 和 `log_file`。`validate` 会为每个输入代理返回终态，TCP 预检失败项会标记为 `failure_stage: "tcp_precheck"`。
- 不指定 `--output` 时，最终汇总中会内嵌 `proxies` 或 `results`；指定后可直接读取输出文件。
- 需要安静运行时加 `--quiet`；需要固定日志位置时加 `--log-file path/to/run.log`。
- `-i -` 可从 stdin 读取 TXT、JSON、JSONL 或 CSV，适合管道和 Agent 工具调用：

```powershell
Get-Content proxies.txt | python cli.py normalize -i - -o normalized.json
```

原有的 `hq.py` 和 `xdl.py` 仍可独立运行，但它们主要用于兼容旧流程；新的自动化调用建议统一使用 `cli.py`。

## 注意事项

- 免费代理稳定性、速度和生命周期差异很大，建议导入后先完整验证。
- HTTPS 请求通常只能根据连接建立结果判断目标域名是否可达，无法读取加密页面内容。
- 健康检查 URL 应选择稳定、响应较快的地址，避免因目标站点临时限流导致误切换。
- 当前本地服务只处理通过 `127.0.0.1:1800/1801` 进入的流量。
- `*.bak`、`*.patch` 和 `*-verification.txt` 是开发过程产生的备份与验证资料，不是运行必需文件。
