---

phase: webapp
vuln_class: [miniprogram, reverse]
title: 微信小程序运行时调试接入（WMPFDebugger）
summary: 小程序代码不落地明文，只能从运行时拿。判据是「WMPF 版本号能否对上适配表」——版本不对，后面全是白费。
---

# 微信小程序运行时调试接入（WMPFDebugger）

小程序包结构与解包见 [[web/小程序安全/wmp-package-readme.md|wmp 包结构]]；整体攻击面与 JS 层手法见 [[web/小程序安全/miniprogram-security-core.md|小程序安全核心]]；配套工具链见 [[web/小程序安全/e0e1wx-tools.md|e0e1wx 工具]]。

## 0. 判据速查

| 现象 | 说明 | 去哪节 |
|---|---|---|
| 有 `WeChatAppEx.exe` 进程 | WMPF 运行时在跑，可以接 CDP | §1 |
| 版本号不在适配表里 | 需要自己适配偏移，或降级微信 | §2 |
| `devtools://` 打开是空白 | 启动顺序反了，或 hook 没注入成功 | §3 |
| 只想看页面不想解包 | CDP 的 Sources 面板直接读加载后的 JS | §3 |

**核心前提**：小程序代码不走网络明文传输，本地磁盘上是加密的 `.wxapkg`。要读逻辑，只有两条路 —— 静态解密包，或运行时挂调试器。本篇讲后者。

## 1. 原理

微信开发者工具内置了 Remote Debug 能力，但加了若干限制。WMPFDebugger 做的事是：

```text
1. 在 WMPF 运行时里 hook 掉限制检查
2. 启动一个 debug server，让小程序运行时连上来
3. 在 62000 端口起一个 CDP 代理，把受限的协议补全成完整 Chrome DevTools Protocol
4. 于是标准 Chromium 的 devtools 前端可以直接连
```

关键在于第 3 步：**它把小程序运行时伪装成一个普通 Chromium 目标**，所以 Sources / Console / Network / Memory 面板全可用。

## 2. 版本适配

WMPF 的 hook 依赖**精确的二进制偏移**，版本一变就失效。适配表按版本号组织，新版本通常由社区补。

查本机版本：

```text
任务管理器 → 找到 WeChatAppEx 进程 → 右键 → 打开文件所在位置
→ 看路径里 RadiumWMPF 和 extracted 之间的数字，就是版本号
```

例如 `...\RadiumWMPF\19841\extracted\...` → 版本 19841。

要点：

- **版本必须精确匹配**，不是「大于等于」。差一个小版本，偏移全错，注入会崩渲染进程。
- 表里标 `unstable` 的版本能连上但会崩渲染进程，只适合快速看一眼 Console。
- 新版微信（> 4.x）的 WMPF 随安装包分发，升级微信即可。旧版（< 4.x）需要在搜索框输入 `:showcmdwnd`（**不回车**）调出命令窗，再执行 `/plugin set_grayvalue=202&check_update_force` 强制拉最新插件，重启微信生效。
- 自己适配的入口是项目里的 `ADAPTATION.md`，核心是找出新版本里那几个被 patch 的函数地址。没有二进制样本时只能提 issue 等社区。

## 3. 启动

```bash
git clone https://github.com/evi0s/WMPFDebugger
cd WMPFDebugger
yarn
npx ts-node src/index.ts
```

**启动顺序是最大的坑**：

```text
正确：起服务 → 打开小程序 → 打开 devtools
错误：起服务 → 打开 devtools → 打开小程序
```

顺序反了会连到一个空目标，表现为 devtools 页面全白。**只能杀掉服务重来**，热重连不生效。

连上：

```text
devtools://devtools/bundled/inspector.html?ws=127.0.0.1:62000
```

端口在 `src/index.ts` 的 `CDP_PORT` 里改。

连上之后能做什么：

| 面板 | 用途 |
|---|---|
| Sources | 读加载后的 JS（已解密），可直接下断点、改逻辑 |
| Console | 调 `wx.*` API、读全局对象、抓 storage |
| Network | 看所有请求（含 `wx.request` 的完整 body 与 header） |
| Memory | 搜堆里的敏感字符串 |
| Application | 看本地缓存、Storage、文件系统 |

**Sources 面板里搜关键字比解包快得多**：直接 Ctrl+Shift+F 搜 `appid`、`secret`、`token`、`https://`，比解开 `.wxapkg` 再翻文件省时间。

## 4. 内嵌浏览器（另一套）

微信内嵌浏览器（公众号文章、H5）的调试是另一个入口，限制更多，只能看页面不能挂完整 CDP。走项目的 `EXTENSION.md`，属于 workaround 级别，别期待完整调试体验。

## 5. 纪律

- **先确认版本号在适配表里，再动手**。这是唯一的前置条件，版本不对一切白搭。
- **启动顺序不能反**。服务 → 小程序 → devtools，反了只能重来。
- **优先用 Sources 搜索，而不是先解包**。运行时拿到的是解密后的代码，还带真实变量名（如果没混淆）。
- **Network 面板是被低估的**：`wx.request` 的 body、header、以及 `wx.uploadFile` 的完整内容都在里面，比抓包省事。
- **`unstable` 版本只用来快速看 Console**，不要在上面做长时间调试，渲染进程随时崩。
- 项目 `src/third-party` 下的代码来自 wechatdevtools，版权归腾讯，二次分发要注意许可。
