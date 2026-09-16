# 教育网（.edu.cn）目标被动侦察：源可达性与"前端 JS 挖真实入口"手法

> 来源：中原工学院 *.zut.edu.cn 侦察任务 task-1a62e488824f 实测（2026 年会话）。
> 仅记录本任务中**实测验证**的可达性结论与有效手法，供后续教育/SRC 侦察复用。

## 1. 被动源在境内教育网评估环境的可达性（本机实测，非通用真理）

| 源 | 结果 | 处理 |
|---|---|---|
| api.hackertarget.com/hostsearch | 可用，一次返回全部 host,ip（本任务 37 个子域） | **首选被动 DNS 源** |
| api.certspotter.com/v1/issuances | 可用（证书透明日志） | 与 hackertarget 互补去重 |
| web.archive.org CDX | **不可达**：DNS 被投毒返回 104.244.46.141(Twitter IP)，正确 IP(207.241.237.3) 上 TLS 退出码 35（SNI 阻断） | 放弃，勿反复重试 |
| otx.alienvault.com passive_dns | 401/403 "Anonymous access limited" | 需 API key，匿名放弃 |
| jldc.me/anubis | 301 跳 www，源站 Cloudflare 连接失败(exit 6) | 放弃 |
| rapiddns.io | 连接失败(exit 6) | 放弃 |
| DoH dns.alidns.com / doh.pub | 可用 | 用于还原被污染域名的真实 IP（--resolve 钉 IP 验证） |

教训：**海外历史/枚举源在境内出口常被 SNI 阻断或投毒，连通失败应立即改道**（hackertarget+CT 日志+境内 DoH），不要把重试当进度。

## 2. 核心有效手法：从"默认页/404 网关"里用前端 JS bundle 挖出真实系统

现象：多个入口根路径只返回默认欢迎页或 403/404（OpenResty/ESOP/nginx 默认页），**但根路径不可测 ≠ 没有系统**。

本任务两个实证：

1. **res1.zut.edu.cn**：根 301 → /xsxk/profile/index.html，页面是自研"学生选课"SPA。
   - 下载 index.html，发现 JS 全部引用另一个绝对域名 `https://xsxk.zut.edu.cn/...`——由此挖出**正式域名 xsxk**（res1 只是历史别名）。
   - 解析 `js/config.min.js`：`window.baseServiceUrl`、token header 名、忽略鉴权 URL 白名单 `/elective/clazz/list`、业务 code 510=初始密码强制改密。
   - 解析 `js/index.min.js`：完整接口 `/auth/login`、`/auth/captcha`、`/auth/logout`、`/elective/user`、`/elective/batch/confirm`；cryptojs.js 表明前端加密；主页面有 wssUrl。
   - 这些**全部从静态前端文件被动提取，零探测 payload**，直接产出后续水平越权/登录逻辑测试的精确接口清单。

2. **aias.zut.edu.cn**（Vite/RuoYi-Vue3 壳 AI 助手）：main.*.js 内提取到 /api/chat、/api/cas、/v2/digital/ping、/h5、/digital 等；拼合后实测 /api/chat/* 返回 CAS 401，据此判定"全接口登录后可达"，避免在网关默认页上空耗。

3. **图书馆博达站群页面**（lib）：页内业务 URL 直接带出 `http://202.196.33.227:8080/opac_two/...`（汇文 OPAC）与读秀/百链/超星等第三方库——业务流自然带出的内网/同段主机属锁面例外，可登记但需标注可达性（本例公网 8080 超时=ACL/仅校园网）。

### 操作要点（低噪声）

- 只 GET 静态资源：`/`、`index.html`、`/assets/index-*.js`、`main.*.js`、`config*.js`、`common.js`，本地正则提取：
  - `baseURL/baseUrl/baseServiceUrl/VITE_*`、`window.<CONST>=`
  - 引号包裹的绝对/相对路径：`/api/...`、`/auth/...`、`/elective/...`、`*.do?dispatch=`、`url:` 字面量
  - 页面内 `<script src>`/`<a href>` 的**绝对域名**（常暴露正式域名/历史别名/真实后端 IP:port）
- 区分边界反代与真实后端：DNS 解析对比 + Server 头；本任务发现 cube/archives 当前统一反代到站群 .138，旧会话记录的 Tomcat 8.5.37 后端已不公网直达——**过时指纹不硬凑 finding**。
- 提取到的接口在 recon 阶段只记录、不验证；需要验证的登录/越权面派生 external-entry。

## 3. 站群/系统指纹速记（本任务命中）

- 博达 VSB 网站群：`/system/resource/code/...`、`/system/resource/show/images/headv2.0/vsbscreen.min.js`、老 URL `xxx.jsp?urltype=tree.TreeTempUrl&wbtreeid=`；常把 Server 头伪装成 `none`；自定义 488 状态码做 WAF 拦截。
- 汇文 Libsys OPAC：`/opac_two/include/login_app.jsp`。
- Seafile：`/accounts/login/`、cookie `sfcsrftoken`/`sessionid`、标题 `<站名> Seafile`。
- 深信服 SSL VPN 新门户：`/por/`、`/com/64sys.js`、响应头 `USE_NEW_PORTAL:1`。
- wengine-auth 资源代理：cookie `wengine_new_ticket`、登录链 `/wengine-auth/login?cas_login=true` 跳 CAS。
