---
title: 案例：DSL 虚拟机验证码系统逆向
summary: 滑块+583KB DSL VM（26 opcode）+WASM 验证码系统完整逆向；三方案成功率对比、DSL VM 逆向五步模式与踩坑表
phase: reverse
vuln_class: [captcha, dsl-vm, wasm]
---

# 案例：DSL 虚拟机验证码系统逆向

> 来源：reverse-skill 1.0.1 field-journal 收编（2026-09-30，MIT，见 `../../../licenses/`）。
> 案例日期 2026-07-05，原文脱敏。

## 目标实体

| 文件 | 说明 |
|------|------|
| nc.js（72KB） | Webpack 打包的滑块核心（9 modules，已 100% 逆向） |
| fireyejs.js（583KB） | **DSL VM 解释器**——26 opcode 自定义指令集，纯 JS 不是 WASM |
| awsc.js（9KB） | 模块加载器 |
| secaptcha.js（72KB） | 真正的 WASM 产物（emscripten，SharedArrayBuffer+Atomics） |
| et_f.js（262KB） | 另一种 DSL VM |

## 三方案成功率对比

| 方案 | 成功率 | 说明 |
|------|--------|------|
| A：Selenium + CDP 原生鼠标拖拽 | **高（推荐）** | `Input.dispatchMouseEvent` 发原生事件，绕检测 |
| B：Playwright 无头 Runner + HTTP API 暴露 token | 中 | 依赖 WASM 初始化环境 |
| C：纯 requests 协议验证 | 极低（勿用） | token 与浏览器 TLS JA3/IP/指纹强绑定 |

## 关键发现

1. **token 无法脱离浏览器**：DSL VM 生成的 token 提交时服务端校验上下文一致性
   （TLS JA3、IP、Cookie、Referer）
2. fireyejs.js 是 583KB 纯 JS 的自定义虚拟机（26 opcode 解释器循环），导出函数名
   被 VM 编码，真实导出经模块注册中心暴露
3. `initialize` 返回特定状态码才是滑块模式；返回 `success` 只是会话创建确认
4. 测试 appkey 不触发真实验证，必须用真实页面的 appkey

## 可复用模式

- **DSL VM 逆向五步**：`case 提取 → opcode 分类 → 常量表分析 → 函数追踪 → 导出提取`
- **验证码系统通用架构**：`入口JS → 模块加载器(WASM/DSL) → API 通信层 → 前端 UI → 服务端验证`
- **CDP 原生事件绕过**：`Input.dispatchMouseEvent` 与 WebDriver 指纹无关

## 工具链

Selenium+CDP（浏览器自动化+原生鼠标事件）、Playwright（无头+route 拦截）、
Node.js+Playwright（Runner 服务）、Python requests（纯 API——已验证失败）。
