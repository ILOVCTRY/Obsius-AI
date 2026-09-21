# wEngine WebVPN（武大系）指纹与改写算法速查

> 来源：zut.edu.cn wpn/nwpn 深度渗透实测沉淀（2026-09-18，任务 task-e39b895734cb 站点制）。
> 用途：识别 wEngine（武大系 WebVPN，厂商串 local.wrdtech.com / WRD Tech），与深信服 Sangfor、深澜 Srun 明确区分（实战中三者标题常被混标）。

## 指纹特征（命中 2 条即可定性）

| 特征 | 值 |
|---|---|
| 改写路径 | `/https/<token>/<path>`、`/http/<token>/<path>`（token=64+ 位连续 hex） |
| token 结构 | `hex(IV) ‖ hex(AES-128-CFB(key, iv, 明文=内网目标主机名[:port]))`；前 32 hex 即 IV |
| 默认 key/IV | `wrdvpnisthebest!`（16B；网关页注入 `__vpn_host_crypt_key` / `__vpn_host_crypt_iv` 同值公示） |
| Cookie 名 | `wengine_vpn_ticket<域名_换_下划线>=wrdvpn1-<32hex>`（如 wpn_zut_edu_cn） |
| 网关静态资源 | `/wengine-vpn/js/main.js?ver=<yyyymmdd>`（混淆 bundle，内嵌明文默认密钥 + `local.wrdtech.com`） |
| CAS 回调 | `/wengine-auth/login?cas_login=true`（对接金智 authserver） |
| 门户 API | `/user/portal/collections(/delete)`（需会话；无会话 302→/login） |

## 改写算法离线验证（本任务验证有效，零目标流量）

```python
from Crypto.Cipher import AES
key = iv = b"wrdvpnisthebest!"
# 解密：AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).decrypt(unhexlify(token[32:]))
# 伪造：encrypt(目标主机名.encode())，token = hexlify(iv)+hexlify(ct)，算法确定性（同输入同 token）
```

实测：`/login` 302 实时签发 token 解密=内网 CAS 主机名，与离线伪造逐字节一致。

## 无会话行为边界（防无效循环）

- 无会话请求改写路径：网关**统一收敛**到公网 CAS 登录页（200），无论 token 有效/伪造/乱码——继续对改写 handler 变花样发包无增量信息；
- 默认密钥可离线解密/伪造，但利用需有效会话（网关代理受会话约束）——纯密钥问题定「加密卫生」级，勿拔高；
- CAS（金智）service 参数白名单**渲染期强制**：篡改 service → 「应用未注册：不允许使用认证服务来认证您访问的目标应用」，零回显，开放重定向方向可排除；
- 改写页内网目标主机名可直接离线解出（默认密钥未换时），可作内网拓扑信息点但不构成未授权访问。

## 与其他 VPN 体系区分

- **深信服 Sangfor**：`/por/login_psw.csp`、`USE_NEW_PORTAL`、自签名 CN=sslvpn——另一套；
- **深澜 Srun**：网络计费认证系统，非 WebVPN——标题含「深澜」不等于 wEngine；
- 同校常见多 VPN 并存（如 vpn.*=Sangfor、wpn/nwpn= wEngine），站点归组按系统不按域名前缀。
