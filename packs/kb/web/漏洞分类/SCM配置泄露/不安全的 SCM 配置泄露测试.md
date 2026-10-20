---
phase: webapp
vuln_class: [info-leak, scm]
title: 不安全的 SCM 配置泄露测试
summary: 先按 `/.git/HEAD` → `/.svn` → `/.hg` → `/.bzr` 顺序探，再辨 403 与 404 的差别；进到 Git 产品网页则改走业务面。
---

> 探路径用 GET/HEAD，注意速率限制。要完整信息收集流程，先开 `web/信息收集/methodology/手册.md` 再深挖。

## 0. 判据速查

| 现象 | 判据 | 打法 |
|---|---|---|
| `/.git/HEAD` 回 `ref: refs/heads/xxx` | Git 仓库泄露 | git-dumper 全量拉 |
| `/.git/` 目录 403 但 `/.git/HEAD` 200 | **目录禁列但文件可读** | 逐文件抓，最高优先级 |
| `/.git/config` 有 `remote.origin.url` | 可能带内嵌凭据 | 读 config 与 `logs/HEAD` |
| `/.svn/entries` 200 | SVN < 1.7 | 解析 XML 拿路径与版本 |
| `/.svn/wc.db` 200 | SVN ≥ 1.7 | 下载 sqlite，查路径与校验和 |
| `/.hg/requires` 200 | Mercurial | mercurial dumper |
| `/.bzr/README` 200 | Bazaar | 手工走路径 |
| `/.DS_Store` 200 | 目录/文件名清单 | ds_store_exp 解析 |
| 路径全 404 | 该路径不存在或被边缘拦死 | 换路径，不纠缠 |

---

## 1. 首轮探针

```http
/.git/HEAD
/.git/config
/.svn/entries
/.svn/wc.db
/.hg/requires
/.bzr/README
/.DS_Store
/.env
```

一次打全，别一个个试——这几条成本极低，命中率不低。

---

## 2. Git 泄露

### 2.1 识别

```text
GET /.git/HEAD
→ ref: refs/heads/main          ← 有效仓库
```

- **`/.git/config`** —— 可能含 `remote.origin.url`、提交者身份、甚至内嵌账号密码。
- **`/.git/index`**、**`/.git/objects/`** —— 部分对象可访问即可还原源码。

### 2.2 403 与 404 的判别（关键）

```text
404            → 路径不存在，或被边缘彻底拦死
403 在 /.git/  → 目录可能存在但禁列，仍要试具体文件
```

403 在目录 + 200 在 `HEAD` = **强烈指示存在泄露**。这时别因为看到 403 就收手，逐文件打：

```http
/.git/HEAD
/.git/config
/.git/logs/HEAD
/.git/refs/heads/main
```

### 2.3 优先抓的文件

| 路径 | 价值 |
|---|---|
| `.git/config` | remote、凭据、hooks 路径 |
| `.git/logs/HEAD` | 提交历史、reflog 式泄漏 |
| `.git/refs/heads/*` | 分支尖、commit SHA |
| `.git/packed-refs` | 打包的分支/标签引用 |
| `.git/objects/**` | 用于还原的对象 blob |

### 2.4 还原工具

- **`arthaud/git-dumper`** —— 单文件可读时全量拉 `.git` 树，首选。
- **`internetwache/GitTools`** —— Dumper / Extractor / Finder，处理部分或损坏的 dump。
- **`WangYihang/GitHacker`** —— 标准 dumper 漏掉的边界情况。

---

## 3. SVN 泄露

### 3.1 按版本分流

```text
SVN < 1.7   → /.svn/entries    （XML/文本，列路径与版本）
SVN ≥ 1.7   → /.svn/wc.db      （SQLite 工作副本库）
```

```http
GET /.svn/entries HTTP/1.1
GET /.svn/wc.db HTTP/1.1
```

### 3.2 还原

- **`anantshri/svn-extractor`** —— 自动抽取。
- **手工**：下 `wc.db` → `sqlite3` 查 `PRAGMA table_info`、文件路径与校验和 → 若 `/.svn/pristine/` 可读，按校验和取原始 blob。

---

## 4. Mercurial / Bazaar / 其他

### 4.1 Mercurial

```http
GET /.hg/requires HTTP/1.1
GET /.hg/store/ HTTP/1.1
```

`/.hg/requires` 是很小的文本，列出仓库特性，命中即确认。还原用 `sahildhar/mercurial_source_code_dumper`。

### 4.2 Bazaar

```text
/.bzr/README
/.bzr/branch-format
```

### 4.3 macOS `.DS_Store`

编码目录与文件名清单，离线解析：

```text
gehaxelt/ds-store
lijiejie/ds_store_exp
```

### 4.4 备份与配置残留

按应用根目录和命名习惯调整：

```text
/.env
/backup.zip
/backup.tar.gz
/wwwroot.rar
/backup.sql
/config.php.bak
/.config.php.swp
```

### 4.5 Nginx 配置的误判信号

```nginx
location /.git { deny all; }
```

这种配置可能让 `/.git/` 返回 403，而具体子路径仍可读或另被拒。**403 能确认路由存在**，务必与"路径不存在"的 404 区分——这是决定要不要继续逐文件抓的唯一依据。

---

## 5. 决策树

```text
1. 打 /.git/HEAD → 命中 ref: refs/heads/ ？
   → git-dumper / GitTools / GitHacker；读 config 与 logs/HEAD 找密钥
2. 否则打 /.svn/wc.db 或 entries → 命中？
   → svn-extractor，或手工 wc.db + pristine 还原
3. 否则打 /.hg/requires → 命中？
   → mercurial dumper
4. 否则打 /.bzr/README → 命中？
   → Bazaar 工具或手工走路径
5. 并行：抓 /.DS_Store、/.env、应用根与上级目录的常见备份扩展名
6. 状态码解读：目录 403 + 具体文件 200 → 最高优先级，逐文件抽
7. 已经进了 Git 产品网页（不是扫 /.git 的阶段）→ 转 §6
```

---

## 6. 活的 Git 产品（与 §1 路径泄露并列，不互相取代）

`/.git/HEAD` 是仓库文件漏到公网。进了工蜂 / GitLab / 自研 Git 网页之后，攻击面在业务里：

- **README、Wiki、议题、评论** —— 按存储型 XSS 打；有桌面客户端再看它是否把同一段 HTML 当特权页渲染，见 `web/漏洞分类/XSS/XSS测试.md` §8。
- **加组员 / 邀请** —— 顺序 `user_id` + 批量写，见 `web/漏洞分类/越权/越权漏洞（IDOR-权限绕过）.md` §4 / §8，不是新洞种。
- **克隆 / 仓库列表不按人隔离** —— 你的仓出现在对方客户端里，只是把上面那条 XSS 送过去。

没前端、只摸到裸 `.git` → 仍走 §1–§5，别为了客户端空等。

---

## 7. 纪律

1. **403 不等于不存在**。目录 403 + 文件 200 才是最有价值的状态组合。
2. **先探全再深挖**。七条首轮探针一次打完，再按命中项展开。
3. **`.git/config` 和 `.git/logs/HEAD` 优先于 objects**。前者直接出凭据与历史，后者要工具还原。
4. **SVN 先看版本**。`entries` 与 `wc.db` 对应不同大版本，打错文件等于没打。
5. **备份文件扩展名要按目标技术栈换**（PHP 的 `.bak`/`.swp`、Java 的 `.war`、.NET 的 `web.config.bak`）。
6. **进了 Git 网页就换面**，别还停在扫路径；反过来，没前端也别硬等客户端。
7. 报告只认 `rules/vuln-report-format.md`。
