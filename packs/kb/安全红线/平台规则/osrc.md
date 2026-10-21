# OPPO 安全应急响应中心（OSRC）平台规则（授权边界 + 操作红线 + 提交战术）

> **触发：** 资产属于 **OPPO / OSRC**（含自有域、移动 App 包名、测试域、第三方清单，或品牌 realme / OnePlus 资产）时，本文件**叠加生效**。与 `评级与价值口径/osrc.md`（判级条款）配套使用：本文件管「能不能打、怎么证明、怎么交」，评级文件管「报上去定什么级」。与通用规则冲突 → 以更严者为准。

## 范围

- **自有业务**（域名 / IP / App 属 OPPO，分核心与非核心）→ 核心 P0，非核心次之
- **自有测试环境**（域名含 `dev`/`test`，属开发测试用）→ P1
- **第三方业务** → **只收高危、严重**，且仅限清单，排最后

核心业务按品牌划分：

- **OPPO**：小布助手、即录、极速视频、乐划锁屏、浏览器、全局搜索、软件商店、视频、我的OPPO、小游戏、游戏助手、游戏中心、云服务、主题商店、查找手机、钱包、欢太金融、快应用中心、OPPO众包、社区、OPPO商城、智能家居、健康
- **realme**：realme商城、realme社区（国内 + 海外）
- **OnePlus**：一加账号、一加云服务、一加国内社区、一加海外商城、一加海外社区

自有业务域名：`*.oppo.com` `*.oppo.cn` `*.myoppo.com` `*.opposhop.cn` `*.coloros.com` `*.nearme.com.cn` `*.oppomobile.com` `*.oppofind.com` `*.myoas.com` `*.heytap.com` `*.heytapmobi.com` `*.realme.com` `*.realme.net` `*.wanyol.com` `*.myoas.net`

自有移动应用包名：`com.oppo.usercenter` `com.heytap.usercenter` `com.nearme.atlas` `com.nearme.wallet` `com.coloros.browser` `com.nearme.browser` `com.heytap.browser` `com.oppo.market` `com.heytap.market` `com.oppo.cloud` `com.heytap.cloud` `com.coloros.findphone.client` `com.coloros.findphone.client2` `com.coloros.findmyphone` `com.oppo.speechassist` `com.coloros.speechassist` `com.nearme.instant.platform` `com.coloros.backuprestore` `com.coloros.encryption` `com.coloros.securepay`

第三方清单：`id-sso.xiniaoyun.com` `sso-sa.yeahzee.com` `sso-me.yeahzee.com` `esign.myoas.com` `yezi-me.yeahzee.com` `yezi-sa.yeahzee.com`。嵌入 OPPO 自有系统的三方组件（smartbi、yonghong 报表）**按三方收**；无明显 OPPO 业务属性的一律以 OSRC 解释为准。**不在清单的三方不收，勿耗精力。**

## 起手必读（评分标准索引，按被测维度取用）

- **互联网应用**：https://security.oppo.com/cn/noticeDetail?notice_only_key=20221659515571953
- 隐私漏洞：`notice_only_key=20221659516819053`
- 终端安全：`notice_only_key=20221659516351716`
- IoT 安全：`notice_only_key=20221659518008209`
- 安全情报：`notice_only_key=20221659518882972`
- APP 恶意行为：`notice_only_key=20221659519111396`
- 外部威胁和情报处理流程：`notice_only_key=20201578453847498`

## 不收的漏洞类型

最近披露 0day（冷静期）、公开已知的公共文件/目录、已知易受攻击库无针对本实现打点描述、OPTIONS/TRACE 启用、登录/注销/未认证 CSRF、软件版本公开、注销后 Cookie 继续工作、表单自动完成、非敏感 Cookie 缺 HttpOnly/Secure、Self-XSS、自动化工具/扫描报告、协议标准问题、**用户名枚举（登录/忘记密码/注册）**、强制策略/账号锁、SSL/TLS/SSH 配置、不切实际复杂 clickjacking、SPF/DKIM/DMARC、密码恢复策略（重置链接过期/复杂度）、**公众可访问登录面板**、注册缺邮箱验证、内容欺骗/文本注入、缺安全头、混合内容、密码改后活跃会话、超链接注入、已知凭据库泄露的凭据、缺 crossdomain.xml 等策略文件/通配符、MITM（除密码等敏感外）、不影响业务的功能缺陷/乱页/路径遍历。

> 白话：**用户名枚举、后台弱口令本体、公众登录面板、账号锁、已知库无利用、缺安全头、0day 冷静期**——这些别当必打项、别指望钱；要打就打到「进去后有敏感面」那步才算有效。

## 验证证明（收敛，别打狠）

- **越权**：禁批量；读/操作真实数据 ≤ **5 组**，数据**另作他用**
- **SQLi**：禁读表内数据，**证明存在即可**；UPDATE/DELETE/INSERT 类**禁自动化**
- **XSS**：证明危害即可；**禁**窃取他人 cookie / 隐私
- **RCE / shell**：只传**纯文本证明**、写明地址口令；**禁**读源码/敏感文件、**禁**删写命令
- **SSRF**：证明可回连/出网即可，**禁**横移打内网
- **隐私维度**：最小一条证明，不批量、不存留
- **App 维度**：抓包测接口越权/明文/敏感泄露为主；反编译产物仅本地分析、不上传不传播
- **IoT / 终端**：**禁**破坏性测试、**禁**刷固件变砖、**禁**远程操控他人设备

## 操作红线

自己手机号/邮箱、禁轰炸；禁物理/社工/非技术手段；禁内网渗透、主机提权；禁 DoS/DDOS/IIS 拒绝服务类；禁保存/分享经漏洞获取的数据，验完即删。

## 计奖与提交战术

- **同系统同类漏洞打包提交**：级不变、奖金提高；**分开提交 → 后面同类按重复处理**。别吐水管
- 同一链接多参数类似漏洞**合并**；同一漏洞源（同接口/同发布系统/泛域解析/多点未鉴权越权等）**只记第一个**；通用组件/SDK 多终端同问题只记第一个
- **组件漏洞（服务端 Tomcat/Apache/OpenSSL/第三方 SDK）**：公开 1 个月内若 OPPO 已知则不过；>1 月且 OPPO 不知情 → 给第一个提交者，**一级不高于中危**
- **组件漏洞（客户端 Android 原生/App 通用）**：公开 3 个月窗口同理，>3 月仍未修给第一个，**一般不高于中危**
- **Chromium 组件**：未发版已知 = 忽略；公告后 60 工作日内 = 忽略；60–90 工作日 = **低危**；>90 且能在 OPPO 浏览器复现 = 正常评级 → 挖浏览器别指望 Chromium 本体，只剩自有逻辑 / UXSS / 超期仍有效才值
- **0day**：只收 OPPO 产品/服务自身的
- 报告**一次写完整**（URL/包名 + 版本 + PoC + 复现链路 + 修复建议），无 PoC 无详细分析直接影响评定

## 收尾必交

测试完**必须告知**：系统内是否遗留文件/数据，并**详细注明删除办法**。

## 提交到平台（OSRC 表单字段）

> 两层分开：**内部落盘报告仍只认 `web/实战打法/rules/vuln-report-format.md`**（资产分析报告版式 + PoC 用原始 HTTP 报文），写入 `report/` 不变；本节省的是**把洞交到 OPPO 平台表单**时的填法。同一洞 = 内部报告（自己留底）＋ 平台表单（对外提交），各行其道，别互相顶掉。

- **漏洞名称**：`漏洞类型 + 受影响资产 + 风险能力`，例：`未授权访问 list 接口，可批量获取用户手机号`（与本地命名口径一致：越权/匿名未授权/可批量要写清）
- **漏洞等级 / 漏洞类型**：平台下拉选；等级对照 `评级与价值口径/osrc.md`（§5/§8 判级速查），不虚报、不降报
- **漏洞详情六段**：
  1. **漏洞摘要【必填】**：一句话讲清漏洞是什么 + 触发条件
  2. **受影响资产【必填】**：完整 URL/API 端点、应用版本、所属业务模块
  3. **复现手册**：① 测试环境【必填】= 操作系统/浏览器版本/测试工具（含版本号）/测试账号及对应权限；② 操作步骤【必填】= 审核人员仅凭此即可独立复现；③ 预期结果 & 实际结果【必填】= 正常逻辑 vs 异常表现
  4. **附件 POC【必填】**：请求数据包 / Payload / 截图 / 录屏 / 测试 EXP；若打包 ZIP，须放 `README.txt` 说明各文件对应步骤编号
  5. **风险影响评估【必填】**：可能的攻击行为、影响范围、安全后果（建议从机密性/完整性/可用性三角度）
  6. **修复建议【选填】**：原理性修复思路，**非**临时规避
- 平台审核大概率只凭「第 3 步操作步骤 + POC」复现 → PoC 沿用本地规范：**可整体复制的原始 HTTP 报文**，会话实值 + 替换说明都要写，截图/录屏与步骤编号对应
- 优质报告补贴（20–30%）：详情/原理/利用/修复写得清 + 有 PoC；无 PoC 别交
