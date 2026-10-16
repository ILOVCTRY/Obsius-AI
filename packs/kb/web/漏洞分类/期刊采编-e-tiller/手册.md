# 勤云科技 E-Tiller 期刊采编系统 测试点手册

> 来源：2026-10 东南大学两站实测（`chinaelectrondevices.seu.edu.cn`、`jeee.seu.edu.cn`，EDUSRC）。
> 适用：`.edu.cn` 各高校/编辑部站点，指纹命中「勤云科技」「e-tiller.com」「期刊采编」时按本篇路由。

## 适用条件（指纹）

| 特征 | 值 |
|---|---|
| 首页 | `/` → 302 → `/ch/index.aspx` |
| Cookie | `ASP.NET_SessionId`（ASP.NET WebForms），部分站点额外 `EtRpk` |
| 外链/页脚 | `http://www.e-tiller.com`、`勤云科技发展有限公司` |
| 目录 | `/ch/`（前台）、`/uploadfile/`、`reader/`、`author/`、`auditor/` |
| 静态 | `js/RSA.js`、`js/BigInt.js`、`js/Barrett.js`、`js/md5.min.js`、`js/checkform.js` |

## 已验证路径（端点清单）

### 前台（未登录可达，多为公开元数据）
- `ch/reader/simple_key_query.aspx?page=&field=&key=&fromTime=&toTime=&order=`（field 取值 title/en_title/user_real_name/key_word/abstract/article_file_no/年份）
- `ch/reader/key_query.aspx`、`issue_list.aspx?year_id=&quarter_id=`、`issue_browser.aspx`
- `ch/reader/view_abstract.aspx?file_no=&flag=`、`view_news.aspx?news_id=`、`merge_abstract.aspx?file_no=`
- `ch/reader/more_news_list.aspx?category_id=`、`more_hotest_article_list.aspx`、`query_recent_article_list.aspx`、`rss.aspx`
- `ch/reader/create_pdf.aspx?file_no=&year_id=&quarter_id=&falg=1`
- `ch/first_menu.aspx?parent_id=<数字>`
- **`download_upload_file.aspx?file_name=/uploadfile/...`**（根目录，无鉴权文件下载处理器）

### 登录入口（RSA+验证码）
- 作者 `ch/author/login.aspx`；审稿 `ch/auditor/login.aspx`；编辑/管理员 `ch/login.aspx`
- 密码：`encryptedString(RSAKeyPair("010001","",<n>), 明文+"#"+md5(明文))` → `encrypted_pwd`
- 验证码：`ValidateImage.aspx`，**服务端强制**
- 找回密码：`ch/get_password.aspx?user_type=author|...`

## 实测坑与防护（2026-10 东南大学）

| 面 | 结论 |
|---|---|
| reader 参数 SQL 注入 | 参数直达 SQL（单引号触发异常/过滤），但应用层 `input_invalid` 过滤（302 `/input_invalid.aspx?msg=<hash>`）+ SafeDog WAF 403 双拦，未绕过 |
| `download_upload_file.aspx` 任意文件下载 | 无鉴权但含 `..` 路径一律判「此文件格式无效」；绝对路径 WAF 403；扩展名白名单放行 doc/docx/pdf/jpg/png/zip/rar/xml/xls/xlsx/ppt，拦 txt/config/aspx/ini/log/bak/mdb/csv |
| 未授权后台 | `/ch/author/*`、`/ch/auditor/*`、`/ch/editor/*`、`/ch/manage/*` 均 302 到 404/GenericErrorPage |
| 弱口令 | RSA+服务端强制验证码，无差异化回显，不可离线判定 |
| 上传（稿件） | 需作者登录，未授权不可触达 |

## WAF 特征
- 安全狗 SafeDog：不存在路径返回 SafeDog 404 页（部分伪装 200，body 含 `404.safedog.cn`）；SQL 关键字 → 403。
- UA 粒度拦截：直连 HTTP 常 403，浏览器 UA + 慢速单发正常；HTTPS 直连最稳。

## 复现步骤建议
1. 直连 HTTPS 取 `/ch/index.aspx` 确认指纹与版本。
2. 参数注入先打单引号，看是 302 `input_invalid`（应用层过滤）还是 SQL 报错。
3. `download_upload_file.aspx` 先测真实文件确认逻辑，再测 `..`/绝对路径/扩展名白名单。
4. 弱口令勿盲爆（验证码门禁）。
