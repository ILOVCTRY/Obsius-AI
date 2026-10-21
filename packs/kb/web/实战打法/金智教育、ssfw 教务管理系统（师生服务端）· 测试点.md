# 金智教育、ssfw 教务管理系统（师生服务端）· 测试点

> 场景：`.edu.cn` 高校教务系统。江苏金智教育（CopyRight 2012-2013）「教务管理系统-师生服务端」，
> 上下文 `/ssfw/`，前端 jQuery1.8 + lovey + 自研 `.widgets` 组件框架，后端 Spring Security + IBM WebSphere。
> 常与金智 `jwglxt`（jw2）、`xk`（选课）、`ssm`（管理端）、统一身份认证 `authserver/ids` 混部同一 vhost 集群。

## 一、指纹与入口发现

- 标题 `教务管理系统-师生服务端`；页脚 `版权所有：CopyRight @江苏金智教育信息技术有限公司 2012-2013`
- JS：`/ssfw/resources/lovey/js/*`、`/ssfw/resources/js/ssfw/widgets/base/*`、`/ssfw/resources/js/showFavorMenu.js`
- 关键配置（登录弹窗 HTML 内联）：`var ssfwConfig = { contextPath:'/ssfw', comboboxUrl:'/ssfw/combobox.widgets',
  combotreeUrl, combotableUrl, photoDownloadUrl/photoUploadUrl:'/ssfw/photo.widgets', selectrangeUrl }`
- **坑：HTTPS 443 可能对所有路径统一 nginx 404**（只反代了别的 vhost）。真实应用常在 **HTTP 80**：
  根路径 200 返回 `<meta http-equiv="Refresh" content="0; URL=/ssfw/index.do">`。
  错误页 `Error 404: SRVE0190E: File not found` = IBM WebSphere 特征。**判可达性先看 80 端口**。

## 二、端点地图（实测）

| 端点 | 说明 | 实测 |
|------|------|------|
| `/ssfw/index.do` | 主页（框架页，iframe 载 welcome/index.do） | 200，无鉴权 |
| `/ssfw/login/ajaxlogin.do` | 登录弹窗（含 ssfwConfig 全量端点） | 200，无鉴权 |
| `/ssfw/j_spring_security_check` | 登录提交（POST j_username/j_password/validateCode） | 302 → `ajaxLoginResult.jsp?validateCodeError=true` |
| `/ssfw/jwcaptcha.do` | 图形验证码 | 200 image/jpeg，**强制** |
| `/ssfw/login/changepassword.do` | 修改密码弹窗 | 200，无鉴权（表单 action `/ssfw/change_password.do` opass/npass/rpass） |
| `/ssfw/combotable.widgets?baseType=<t>` | 通用表格数据 | 200 `{"data":[],"totalCount":0}`（需正确数据源名） |
| `/ssfw/attach/oneKeyDownload.widgets?serviceId=<n>` | **附件打包下载(ZIP)** | **200 无鉴权**，返回 ZIP；serviceId 可枚举整数 |
| `/ssfw/attach/downloadAttachment.widgets?attachmentId=<n>` | 单附件下载 | 200，body 可能为 0（无对象） |
| `/ssfw/ssm/index.ssm` | 管理端入口 | 302 → /ssfw/index.do（未登录） |

- `.widgets` 端点**部分映射**：combotable、attach.* 可达；combobox/combotree/photo/selectrange 可能 404（未映射）。
  404 页固定 310 字节 `页面不存在.`，可作爆破基线。
- 上传 action（JS 声明）`/ssfw/attach/uploadAttachment.widgets` 可能 **未映射(404)**——声明存在≠可用。

## 三、测试点

1. **未授权打包下载（本轮实测有效）**：`GET /ssfw/attach/oneKeyDownload.widgets?serviceId=1` 无 Cookie 返回
   `application/x-msdownload` ZIP。**uProxy 轮询多后端实例**，实例间状态不一致（有时含文件、有时空 ZIP、有时超时），
   须多次重复请求取证。若命中含真实附件实例 → 未授权读取业务数据，按 `rating:edu-rating` 判级。
2. **IDOR**：`downloadAttachment.widgets?attachmentId=<n>`、`oneKeyDownload?serviceId=<n>` 枚举小范围
   （先自己可读的、再 ±3），以 body 差分（大小/Content-Disposition/解包内容）证明。
3. **登录面**：`j_spring_security_check` 强制 `validateCode`，无验证码正确值无法评估凭据（禁止 OCR/爆破）。
4. **注入**：`combotable.widgets?baseType=`/`condition=` 参数；实测对引号等 payload 无布尔/报错差分（query 名未配置时不落库）。
5. **信息泄露**：`/ssfw/login/changepassword.do`、`/ssfw/login/ajaxlogin.do` 未授权可达，泄露全部接口契约。

## 四、坑

1. HTTPS 443 全 404 ≠ 站点死；务必看 80 端口与真实应用上下文。
2. 前端 JS 声明的 action 未必映射，404 与「无权限」要区分。
3. uProxy 后端轮询导致同一 URL 响应抖动，判级取证要多次重复。
4. 源 IP 有限速（~90-120s 自愈），务必慢速单发。
