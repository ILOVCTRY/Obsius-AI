# Wisedu Visual SiteBuilder 9（VS9）网站群系统 · 测试点

> 场景：`.edu.cn` 高校官网 / 政府门户大量使用 Wisedu 网站群（Visual SiteBuilder 9，简称 VS9）。

## 适用条件
- 源码注释 `<!--Announced by Visual SiteBuilder 9-->`、静态资源 `/_sitegray/_sitegray.js`、`index.vsb.css`、页面脚本 `/system/resource/js/counter.js`。
- 页面调用 `_jsq_(treeid,'/index.jsp',newsid,owner)`、`_addDynClicks(clicktype,owner,clickid)`；含 `CustomerNO:` 十六进制串。
- 常见前置 `X-Protected-By` 头 / 雷池 SafeLine WAF。

## 已验证路径（未授权接口）
- `/system/resource/code/news/click/dynclicks.jsp?clickid=<id>&owner=<owner>&clicktype=wbnews` → 文章真实点击量（纯数字）。
- `/system/resource/code/news/click/dynclicksbatch.jsp?clickids=<id,id>&owner=&clicktype=wbnews` → 逗号批量返回 `829,2817`。
- `/system/resource/code/news/click/addclicktimes.jsp?wburlid=&owner=&type=wburl` → 增加计数（写，勿刷）。
- `/system/resource/code/datainput.jsp` → 访问统计落库。
- `owner` 从页面 `_showDynClicks`/`_addDynClicks` 调用第二参数提取（公开值）；clicktype 取 wbnews/wburl/wbimage。

## 复现步骤
1. 取首页，`grep -oE '_addDynClicks\([^)]*\)'` 提取 owner 与 clickid。
2. `curl -sk "https://<host>/system/resource/code/news/click/dynclicks.jsp?clickid=<id>&owner=<owner>&clicktype=wbnews"` → 观察是否回显真实计数。
3. 注入判据（无 WAF 时）：`id` 与 `id+1` 计数不同；引号闭合后布尔真假差分。

## 坑（WAF 与响应区分）
- dynclicks 系列 clickid/owner 为历史数字型 SQL 注入点；前置 WAF 时 and/or/union/select/order by/group by/xor/`--`/`../` 一律 403（SafeLine 拦截页 ~45KB 含 `EventID`）。
- 三类响应务必区分：①403+45KB 含 EventID = WAF 主动拦截 payload；②200+~965B「由于网络或安全的原因…」= 直连 IP 被网关临时限制（高频扫描所致），**非端点不存在**；③200+1693B「内容不存在」= 真实 404。
- 高频直连后 `/system/` 路径会统一变 965B 提示页，须改用代理池出口复核；同一请求经代理能返回真实数据/真实 404。

## 其它测试点
- 后台：/system/index.jsp（鉴权提示）、/wcm/app/login.jsp、/wcm/index.jsp。
- 附件：/__local/<x>/<xx>/<xx>/<hash>.<ext>；编辑器：/ueditor/jsp/controller.jsp、/ewebeditor/、/kindeditor/。

## 判级提示（EDUSRC）
- 点击量等公开计数 → 不构成漏洞，category=intel。
- 注入可实证 → rating:edu-rating 高危#3；后台弱口令可达并登录 → 高危#5。

## 实战补充（2026-10-14 同济 acem/ageing 实测）
- **搜索接口 base64 关键词绕过 WAF**：搜索表单提交到 `/ssjgy.jsp?wbtreeid=<id>`，参数 `lucenenewssearchkey` 走 Lucene 全文检索，服务端会 base64 解码后再检索。明文关键词含 `and/or/sleep/union/select` 等词会被 WAF 关键词拦截（返回 965B「由于网络或安全的原因…」拦截页），但把关键词 **base64 编码**后提交即放行并正常返回结果页（实测 `test`→15430B、`同济大学`→22338B 结果页）。→ 任何经 WAF 拦截的 VSB9 明文参数，优先试 base64 编码绕过。
- **当前版本 dynclicks/ssjgy 参数已强制纯整数，注入判死路**：`dynclicks.jsp?clickid=1121` → `918`；`clickid=1121*1` / `1121+0` / `1121-0` / `1121.0` / `1121)` 等非纯整数一律确定性返回常量 `3`（≠1121，证明服务端仅接受 `[0-9]+`，无运算/拼接）。`ssjgy.jsp?wbtreeid=1001` 正常；`1001+0` / `1000+1` / `1001*1` / `1001.0` / `1001'` / `1002` 一律返回 `栏目不存在!`（63B）。→ 见到 VSB9 的 clickid/owner/wbtreeid 不要急着判注入，先做「非整数是否返回常量/栏目不存在」的判别，若被严格校验即收 dead_end。
- **后台门与权限登录页（未授权）**：`/system/index.jsp` 返回 912B「您需要登录后才可访问系统」（后台根，鉴权门）；`/system/resource/code/auth/auth.jsp` 返回 2989B「权限登录页」表单（字段 `wbaccount` / `wbpassword` / `actiontype=login`，POST 提交；错误凭据回 3041B「密码错误!」，可用于弱口令判定，注意单账号≤10、慢速单发）。`/system/manage/*`、`/system/admin.jsp` 等不存在的后台路径会回落到同一 912B 门页，勿误判为真实文件。
- **未授权点击计数（intel 级）**：两站均确认 `dynclicks.jsp`（单条，如 `clickid=1121&owner=<公开owner>&clicktype=wbnews`→`918`）、`dynclicksbatch.jsp`（批量→`75,123,44`）、`clicktimes.jsp`（JSON→`{"wbshowtimes":123,...}`）无需登录即返回真实数据，owner 为页面公开值。按 edu-rating 属公开计数、不构成漏洞，登记 category=intel。
- **上传/编辑器端点未部署即 404**：`/system/resource/code/blob/{uploadfile,uploadFileForCkeditor,upload,blobfileupload,blobadd}.jsp`、`/system/resource/js/uploadify/upload.jsp`、`/ueditor/jsp/controller.jsp`、`/ewebeditor/`、`/ckeditor/`、`/kindeditor/` 全部 404；`/__local/` 为只读静态图片目录（目录列举 403，`../` 穿越读 WEB-INF 均 404/400）。故无未授权上传/穿越面。
