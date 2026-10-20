"""零 LLM 成本的观测结构化（借鉴 hxbai `engine/loop.py` 抽取器群，2026-10-19）。

**要解决的问题**：现在「从工具输出里提炼结构化事实」全靠模型自己调
`bb_add_finding` / `bb_add_asset`。代价有三：

1. **贵**——每抽一条事实烧一次 token（一次 nmap 输出可能藏着十几个服务）。
2. **漏**——模型偷懒就少抽，上下文吃紧时更是直接跳过。
3. **偏**——模型的转述带主观改写，`nmap` 报的 `22/tcp open ssh` 会被写成
   「好像开了 ssh」，后续会话再也拿不到准确的端口号。

hxbai 的做法是**机械先抽，模型只做决策**：3.7k 行的 loop.py 里过半是正则抽取器。
本模块把那批抽取器提炼成纯函数（零 IO、零依赖），由工具层在工具返回后调用，
把结构化结果附加到回执里（或落黑板）。

四个抽取面：

| 函数 | 输入 | 输出 |
|------|------|------|
| `parse_nmap` | nmap 原始输出 | `{host: {ports: [...], banners: {port: text}}}` |
| `parse_http_signals` | 任意响应文本 | `{status, server, powered_by, locations, urls, paths}` |
| `parse_credentials` | 任意文本 | `[{user, secret, raw}]`（含 base64 还原） |
| `salient_lines` | 任意长文本 | 信息量最高的若干行（供投影/摘要） |

外加两个基础设施：

- `normalize_volatile(text)`：把 Set-Cookie/nonce/UUID/hex/时间戳/随机串归成
  占位符——**否则「同一条命令跑两次」永远去重失败**，`tried` 表会被噪音塞满。
- `obs_signature(text)`：归一化后的短哈希，作为「这次观测是否新」的指纹。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re

__all__ = [
    "parse_nmap",
    "parse_http_signals",
    "parse_credentials",
    "salient_lines",
    "normalize_volatile",
    "obs_signature",
    "classify_vuln",
    "extract_access",
    "extract_flags",
    "summarize_output",
    "parse_linktypes",
    "parse_script_names",
    "VULN_CLASSES",
]

# ---------------- nmap ----------------

_NMAP_HOST_RX = re.compile(r"[Nn]map scan report for\s+(.+)")
_NMAP_SVC_RX = re.compile(r"\b(\d{1,5})/(?:tcp|udp)\s+open\s+(\S[^\r\n]{0,70})", re.I)
_IPV4_RX = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_HOSTNAME_RX = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def _host_of_scan_line(tail: str) -> str | None:
    """`1.2.3.4 (host.example)` / `host.example (1.2.3.4)` / 裸主机名。"""
    tail = (tail or "").strip()
    m = re.search(r"\((" + _IPV4_RX.pattern + r")\)", tail)
    if m:
        return m.group(1)
    tok = tail.split()[0] if tail.split() else ""
    if _IPV4_RX.fullmatch(tok) or _HOSTNAME_RX.fullmatch(tok):
        return tok
    return None


def parse_nmap(text: str, default_host: str = "") -> dict:
    """从 nmap 输出抽 host → 端口/服务横幅。

    返回 ``{host: {"ports": [22, 80], "banners": {80: "nginx 1.24.0"}}}``。
    支持多主机输出（`Nmap scan report for` 分段）；无 host 行时全部归
    ``default_host``（扫描单目标时常见）。

    **为什么不用模型抽**：一次全端口扫描的 open 行可达上百条，模型转述必丢细节，
    而端口号/版本号恰恰是后续 CVE 匹配的唯一依据。
    """
    result: dict[str, dict] = {}
    cur = (default_host or "").strip()
    for line in (text or "").splitlines():
        mh = _NMAP_HOST_RX.search(line)
        if mh:
            cur = _host_of_scan_line(mh.group(1)) or default_host
            continue
        for mp in _NMAP_SVC_RX.finditer(line):
            port = int(mp.group(1))
            if not (1 <= port <= 65535) or not cur:
                continue
            entry = result.setdefault(cur, {"ports": set(), "banners": {}})
            entry["ports"].add(port)
            banner = mp.group(2).strip()
            if banner:
                entry["banners"][port] = banner
    return {h: {"ports": sorted(e["ports"]), "banners": e["banners"]}
            for h, e in result.items()}


# ---------------- HTTP ----------------

_STATUS_RX = re.compile(r"(?m)^\s*HTTP/\d(?:\.\d)?\s+(\d{3})")
_SERVER_RX = re.compile(r"(?im)^(?:Server|X-Powered-By|X-Generator):\s*(.{1,100})$")
_LOCATION_RX = re.compile(r"(?im)^Location:\s*(/[^\s?#]{0,150})")
_URL_RX = re.compile(r"(https?)://([A-Za-z0-9_.\-]+)(?::(\d{1,5}))?(/[^\s\"'<>)\]]*)?", re.I)
_PATH_RX = re.compile(
    r"/(?:admin|login|api|upload|export|import|debug|actuator|swagger|graphql|"
    r"console|manage|backup|\.git|\.env|phpinfo|wp-admin|administrator)\b[^\s\"'<>]*", re.I)
_SET_COOKIE_RX = re.compile(r"(?im)^Set-Cookie:\s*([^=\s]+)=")
_TITLE_RX = re.compile(r"<title[^>]*>(.{1,120}?)</title>", re.I | re.S)


def parse_http_signals(text: str) -> dict:
    """从 HTTP 响应/抓包文本抽结构化信号。

    返回 ``{status, headers: {Server/X-Powered-By}, locations, urls, paths,
    cookies, title}``——只保留「能指向下一步」的东西，不搬运正文。
    """
    if not text:
        return {}
    out: dict = {}
    m = _STATUS_RX.search(text)
    if m:
        out["status"] = int(m.group(1))
    hdrs = {}
    for hm in _SERVER_RX.finditer(text):
        name, _, val = hm.group(0).partition(":")
        hdrs[name.strip()] = val.strip()[:100]
    if hdrs:
        out["headers"] = hdrs
    locs = sorted({lm.group(1) for lm in _LOCATION_RX.finditer(text)})
    if locs:
        out["locations"] = locs[:10]
    urls = []
    for um in _URL_RX.finditer(text):
        host = um.group(2)
        port = um.group(3)
        path = um.group(4) or "/"
        urls.append(f"{host}:{port}{path}" if port else f"{host}{path}")
    if urls:
        # 去重保序，截断防爆
        seen: set[str] = set()
        uniq = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
            if len(uniq) >= 20:
                break
        out["urls"] = uniq
    paths = sorted({pm.group(0) for pm in _PATH_RX.finditer(text)})
    if paths:
        out["paths"] = paths[:20]
    cookies = sorted({cm.group(1) for cm in _SET_COOKIE_RX.finditer(text)})
    if cookies:
        out["cookies"] = cookies[:10]
    tm = _TITLE_RX.search(text)
    if tm:
        out["title"] = re.sub(r"\s+", " ", tm.group(1)).strip()[:120]
    return out


# ---------------- 凭据 ----------------

_USER_SECRET_RX = re.compile(
    r"(?<![\w.-])([A-Za-z][A-Za-z0-9_.\-]{1,32})[ \t]*[:=][ \t]*"
    r"(\S{4,64})(?![\w.])")
_B64_RX = re.compile(r"[A-Za-z0-9+/]{8,}={0,2}$")
_CRED_CTX_RX = re.compile(
    r"(?i)\b(?:password|passwd|pwd|pass|secret|token|api[_-]?key|access[_-]?key|"
    r"credential|cred)\b")
# 明显不是凭据的键名（防止把 header 名/配置键/字段名本身当账号）
_NOT_USER = frozenset((
    "content", "accept", "connection", "cache", "pragma", "expires", "date",
    "vary", "etag", "age", "via", "allow", "link", "host", "origin", "referer",
    "user-agent", "accept-encoding", "accept-language", "content-type",
    "content-length", "transfer-encoding", "keep-alive", "upgrade", "server",
    "x-powered-by", "x-frame-options", "x-content-type-options", "strict-transport-security",
    "content-security-policy", "set-cookie", "location", "www-authenticate",
    "true", "false", "null", "none", "localhost", "utf-8", "utf8", "text",
    # 账号字段名本身（`user=root` 的 root 是账号，不是 `user:pass` 对）
    "user", "username", "login", "account", "uid", "owner", "realm", "domain",
    "hostname", "value", "flag", "database", "port", "path", "url", "type",
))

# 凭据语境键名（`password: admin:Pass` 里 password 是字段名，值是凭据本体）
_CRED_KEYS = frozenset((
    "password", "passwd", "pwd", "pass", "secret", "token", "api_key", "apikey",
    "api-key", "access_key", "accesskey", "credential", "cred", "auth", "key",
    "db_password", "admin_password",
    # 复数/变体（日志里常写 `credentials: admin:pass`）
    "credentials", "creds", "passwords", "secrets", "tokens", "passphrase",
    "db_pass", "dbpass", "db_pwd", "root_password", "user_password",
))

# 「像口令」的形态：含数字或特殊字符（纯字母单词更像普通值，需凭据语境才收）
_PWISH_RX = re.compile(r"[0-9!@#$%^&*_\-+=.]")

# URL 内嵌凭据：`mysql://root:root123@10.0.0.5:3306/db`（CTF 与配置泄露都常见）
_URL_CRED_RX = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.\-]{1,20}://([^/\s:@]{1,40}):([^/\s@]{1,64})@[^\s/]+")
# `Authorization: Basic dXNlcjpwYXNz` —— 单列一条，否则会被 header 名黑名单吃掉
_AUTH_BASIC_RX = re.compile(
    r"(?i)\bauthorization\s*:\s*(?:basic|bearer)\s+([A-Za-z0-9+/=]{8,})")
# 账号字段名（`user=root` / `login: admin`）——值本身是账号，不是 user:pass 对
_USER_KEYS = frozenset(("user", "username", "login", "account", "uid", "owner",
                        "user_name", "userid", "user_id"))
# 值被空白截断在 `admin:` 后，口令在同行后续（`credentials: admin: Passw0rd`）
_TAIL_PW_RX = re.compile(r"[ \t]*([^\s:]{4,64})")


def _b64_decode(secret: str) -> str | None:
    """把疑似 base64 的值还原成明文；不是 base64 或解不出可打印文本 → None。"""
    s = (secret or "").strip().rstrip("]").rstrip(",")
    if not _B64_RX.fullmatch(s) or len(s) < 8:
        return None
    try:
        dec = base64.b64decode(s, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not dec or not dec.isprintable():
        return None
    return dec


def _try_b64_secret(secret: str) -> str | None:
    """`dXNlcjpwYXNz` → `pass`：base64 里藏 `user:pass` 是 CTF 常见形态。

    有冒号时只返回口令部分（账号由调用方另取）；无冒号时整段即口令。
    """
    dec = _b64_decode(secret)
    if dec is None:
        return None
    return dec.split(":", 1)[1] if ":" in dec else dec


def _looks_like_flag(value: str) -> bool:
    """值是不是 flag 形态（`flag{…}` / `xxxctf{…}`）。

    用途：**别把 flag 当凭据**。实测踩过：`[+] decoded: ZmxhZ3thMWIy…` 这行
    ——键 `decoded` 无凭据语境，但值能 base64 还原成 `flag{a1b2c3d4e5f6}`，
    于是被当口令收下，回执里出现「[观测] 凭据 1 组: decoded:flag{…}」。
    两个后果都坏：① 模型可能把它当账号密码去试登录；② 答案以「凭据」名义
    进了 `ctf_ammo` 的输入面，与「候选表恒不含 flag」的口径相悖。
    """
    try:
        from core.ctf import flagjudge
        return bool(flagjudge.extract_candidates(value or ""))
    except Exception:  # noqa: BLE001 —— 判定失败按「不是 flag」处理（不误伤真凭据）
        return False


def parse_credentials(text: str, *, limit: int = 30) -> list[dict]:
    """从任意文本抽 `user:secret` / `user=secret` / URL 内嵌 三种形态的凭据。

    只在**同一行有凭据语境**（password/secret/token…）、secret 形态明显
    （含数字/符号，或 base64 解出 `user:pass`）时才收，避免把
    `Content-Type: text/html` 这类正常 header 当凭据。

    支持的落库形态：
    - ``password: admin:Passw0rd123``（字段名是凭据语境词）→ 拆出 `user=admin`；
    - ``admin: Passw0rd123`` → `user=admin`；
    - ``passwd=toor``（只有口令）→ `user=""`，由调用方配账号；
    - ``mysql://root:root123@db:3306/x`` → `user=root, secret=root123`；
    - ``login: admin password: Passw0rd123`` → 同行账号 + 口令自动配对。

    返回 ``[{user, secret, raw}]``，secret 已尝试 base64 还原。
    **flag 形态的值一律不收**（见 `_looks_like_flag`）——flag 不是凭据。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _push(user: str, pw: str, raw: str) -> bool:
        if len(pw) < 4 or pw in ("*", "-", "null", "None"):
            return True
        if _looks_like_flag(pw) or _looks_like_flag(user):
            return True
        dedup = ((user or "").lower(), pw)
        if dedup in seen:
            return True
        seen.add(dedup)
        out.append({"user": user or "", "secret": pw, "raw": raw[:200]})
        return len(out) < limit

    for line in (text or "").splitlines():
        # ⓪ `Authorization: Basic <b64>` —— 值里是 `user:pass`
        for am in _AUTH_BASIC_RX.finditer(line):
            blob = am.group(1)
            dec = _try_b64_secret(blob)
            if dec:
                u, sep, p = dec.partition(":")
                if not _push(u if sep else "", p if sep else dec, am.group(0)):
                    return out
        # ① URL 内嵌凭据（先抽，避免 `mysql://` 被当 key）
        url_spans: list[tuple[int, int]] = []
        for um in _URL_CRED_RX.finditer(line):
            url_spans.append((um.start(), um.end()))
            if not _push(um.group(1), um.group(2), um.group(0)):
                return out
        # ② `key: value` 扫描；`user=`/`login:` 记作待配对的账号
        has_ctx = bool(_CRED_CTX_RX.search(line))
        pending_user = ""
        for m in _USER_SECRET_RX.finditer(line):
            # 落在 URL 内嵌凭据里的片段（`mysql: //root:...`）已由 ① 处理
            if any(lo <= m.start() < hi for lo, hi in url_spans):
                continue
            key, secret = m.group(1), m.group(2)
            if secret.startswith("//"):  # scheme 尾部残留
                continue
            klow = key.lower()
            restored = _try_b64_secret(secret)
            if klow in _USER_KEYS:
                pending_user = secret
                continue
            if klow in _CRED_KEYS:
                # 字段名是凭据语境词：值里可能还有 `user:pass` 结构
                body = restored or secret
                if body.endswith(":") and not restored:
                    # `credentials: admin: Passw0rd123` —— 值被空白截断在 `admin:`
                    # 上，口令在行内后续（`_USER_SECRET_RX` 的 `\S{}` 跨不过空格）
                    tail = _TAIL_PW_RX.match(line, m.end())
                    if tail:
                        user = body.rstrip(":").strip()
                        if not _push(user, tail.group(1), m.group(0) + tail.group(0)):
                            return out
                        pending_user = ""
                        continue
                user, sep, pw = body.partition(":")
                if sep and user and pw:
                    if not _push(user, pw, m.group(0)):
                        return out
                elif not _push(pending_user, body, m.group(0)):
                    return out
                pending_user = ""
                continue
            if klow in _NOT_USER:
                continue
            # 无凭据语境时，只收 base64 能解出 user:pass、或值明显像口令的
            if restored is not None:
                pw = restored
            elif has_ctx or _PWISH_RX.search(secret):
                pw = secret
            else:
                continue
            if not _push(klow, pw, m.group(0)):
                return out
    return out


# ---------------- 易变值归一化（去重基础设施） ----------------

_VOL_SUBS: tuple[tuple[re.Pattern, str], ...] = (
    # Set-Cookie 的值
    (re.compile(r"(?i)(set-cookie:\s*[^=\s]+=)[^\s;,]+"), r"\1<v>"),
    # nonce/csrf/token/session 等
    (re.compile(r"(?i)\b(nonce|csrf|xsrf|_?token|state|sid|session|etag|request-id|"
                r"authorization|bearer)(['\"]?\s*[:=]\s*)[^\s;,&'\"]+"), r"\1\2<v>"),
    # UUID
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    # 长 hex（哈希/地址）
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    # 日期 / HTTP 日期
    (re.compile(r"\b\d{4}-\d\d-\d\d([ T][\d:.]+)?\b"), "<date>"),
    (re.compile(r"(?i)\b(?:mon|tue|wed|thu|fri|sat|sun),\s+\d{1,2}\s+"
                r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}\s+"
                r"[\d:]+(?:\s*gmt|\s*utc)?"), "<httpdate>"),
    # 时间戳
    (re.compile(r"\b\d{10,}\b"), "<ts>"),
    # 查询参数值
    (re.compile(r"([?&][A-Za-z0-9_%.\-]{1,40}=)[^\s&#;]+"), r"\1<v>"),
    # 混合大小写+数字的随机串（长度 ≥8）
    (re.compile(r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{8,}\b"), "<rand>"),
)


def normalize_volatile(text: str) -> str:
    """把易变值替换成占位符，使「同一命令跑两次」的输出可比。

    没有这一步，任何「这条命令试过了」的去重表都会失效：`Set-Cookie:
    SESSION=abc123` 每次不同，`tried` 表会为同一条 curl 命令塞进无穷多行。
    """
    out = text or ""
    for rx, rep in _VOL_SUBS:
        out = rx.sub(rep, out)
    return out


def obs_signature(text: str, *, width: int = 16) -> str:
    """观测指纹：归一化 + 压空白后的 sha256 前缀。"""
    norm = re.sub(r"\s+", " ", normalize_volatile(text or "")).strip()
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:width]


# ---------------- 显著性筛选 ----------------

# 「这一行携带下一步线索」的特征（顺序无关，命中任一即算显著）
_SALIENT_RX = re.compile(
    # HTTP 状态行 / 端口开放
    r"\b[1-5]\d\d\b\s+(?:OK|Found|Moved|Redirect|Forbidden|Unauthorized|Bad|Not Found|Error)"
    r"|\b\d{1,5}/(?:tcp|udp)\b|\bopen\b"
    # 响应头
    r"|Server:|X-Powered-By|WWW-Authenticate|Set-Cookie|Location:|Content-Type:"
    # 版本号
    r"|\bv?\d+\.\d+(?:\.\d+)?\b"
    # 错误/拒绝
    r"|error|exception|traceback|denied|refused|unauthorized|forbidden|timeout"
    # 敏感路径
    r"|/(?:admin|login|api|upload|export|import|debug|actuator|swagger|graphql|console|manage)"
    # 二进制/逆向
    r"|0x[0-9a-fA-F]{3,}|\.(?:text|data|bss|rodata|plt|got)\b|\bELF\b|\bPIE\b"
    r"|\b(?:opcode|instruction|disasm|decompil|bytecode|register|stack canary|nx|relro)\b"
    # 密码学
    r"|\b(?:xor|rc4|aes|des|rsa|base64|hex|keystream|cipher|plaintext|ciphertext|decrypt|decode|nonce|iv|md5|sha\d)\b"
    # 凭据
    r"|\b(?:password|passwd|secret|api[_-]?key|token|credential|private[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)\b"
    r"|\buid=\d+|\broot:x:0:0|[A-Za-z0-9_.\-]+@[A-Za-z0-9.\-]+:\d+"
    # 内网/横向
    r"|\b(?:10|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b(?:tunnel|pivot|reachable|internal host|jump host|lateral)\b"
    # 结论动词
    r"|\b(?:extracted|recovered|decrypted|decoded|cracked|bypassed|leaked|exfiltrated|ruled out|confirmed|obtained)\b"
    # flag
    r"|[A-Za-z][\w.\-]{1,30}\{[^}\r\n]{2,}\}",
    re.I)


def salient_lines(text: str, *, limit: int = 12, max_chars: int = 240) -> list[str]:
    """挑出信息量最高的若干行（去重、保序、单行截断）。

    用途：工具返回后的「要点提炼」、上下文投影的摘要段。**不是给模型看的
    全文摘要**——是把「下一步线索」从几百行噪音里捞出来。
    """
    if not text:
        return []
    picked: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or len(s) < 4:
            continue
        if not _SALIENT_RX.search(s):
            continue
        key = normalize_volatile(s)[:160]
        if key in seen:
            continue
        seen.add(key)
        picked.append(s[:max_chars])
        if len(picked) >= limit:
            break
    return picked


# ---------------- 漏洞类型归类 ----------------

# 关键词表 → vuln_class（与黑板 vuln_class 自由标签口径一致，不做枚举强约束）
_VULN_HINTS: tuple[tuple[str, re.Pattern], ...] = (
    ("cmdi", re.compile(r"command injection|cmdi|os command inject", re.I)),
    ("ssti", re.compile(r"\bssti\b|template injection", re.I)),
    ("sqli", re.compile(r"\bsqli\b|sql injection", re.I)),
    ("deserialization", re.compile(r"deserial|pickle|unmarshal|objectinputstream|ysoserial", re.I)),
    ("lfi", re.compile(r"\blfi\b|local file inclus|file inclusion|log poison|arbitrary file read", re.I)),
    ("pathtraversal", re.compile(r"path traversal|directory traversal|\.\./", re.I)),
    ("ssrf", re.compile(r"\bssrf\b|server-?side request", re.I)),
    ("fileupload", re.compile(r"file upload|webshell upload|arbitrary (?:file )?upload", re.I)),
    ("xxe", re.compile(r"\bxxe\b|xml external entit", re.I)),
    ("authbypass", re.compile(r"auth(?:entication)? bypass|login bypass|jwt forg|alg=none", re.I)),
    ("privesc", re.compile(r"privilege escalation|privesc|setuid|sudo abuse", re.I)),
    ("idor", re.compile(r"\bidor\b|insecure direct object", re.I)),
    ("xss", re.compile(r"\bxss\b|cross-?site script", re.I)),
    ("weakcred", re.compile(r"weak (?:password|cred)|default (?:password|cred)|brute[- ]?force", re.I)),
    ("infoleak", re.compile(r"info(?:rmation)? (?:leak|disclos)|sensitive data expos", re.I)),
    ("rce", re.compile(r"\brce\b|remote code exec|code execution|reverse shell|web ?shell", re.I)),
)
VULN_CLASSES = tuple(k for k, _ in _VULN_HINTS)

_ACCESS_TO_VULN = {
    "root": "privesc", "sudo": "privesc", "admin": "authbypass",
    "rce": "rce", "shell": "rce", "webshell": "rce", "code execution": "rce",
}
_ACCESS_RX = re.compile(r"\b(root|admin|rce|shell|code execution|webshell|sudo)\b", re.I)


def classify_vuln(text: str, access: str = "") -> str | None:
    """把任意文本归到 vuln_class（命中不了返回 None，绝不硬猜）。"""
    for key, rx in _VULN_HINTS:
        if rx.search(text or ""):
            return key
    return _ACCESS_TO_VULN.get((access or "").strip().lower())


def extract_access(text: str) -> str | None:
    """从文本里识别「拿到了什么权限」（root/admin/rce/shell…）。"""
    m = _ACCESS_RX.search(text or "")
    return m.group(1).lower() if m else None


# ---------------- 回执摘要（工具层消费入口） ----------------

# 摘要最多列多少条（端口/URL/凭据）——回执是给模型看的，不是全量清单
_SUMMARY_PORTS_MAX = 20
_SUMMARY_CREDS_MAX = 5
_SUMMARY_SALIENT_MAX = 8
_SUMMARY_FLAGS_MAX = 3


def extract_flags(text: str, *, limit: int = _SUMMARY_FLAGS_MAX) -> list[str]:
    """从工具输出里抽出「可提交」的 flag（复用 flagjudge 三道闸，按分排序）。

    **为什么单独成段**：`flag{…}` 原本只是 `salient_lines` 众多命中行之一，
    一次 200 行的 `strings`/`cat` 输出里要点只留 8 行，flag 极易被挤掉；而
    flag 恰恰是本轨唯一「必须看见」的信号——漏一眼就是整题空转。这里用
    `confident_flags`（自然语言内层 / 诱饵 / 刷屏同形簇 全部剔除）先筛，
    再显式列段，模型不必自己找。

    注意：**只做观测提示，不判定解出**——是否计入题目仍由 `challenge_flags`
    台账（唯一真值）决定，本函数不写任何状态。
    """
    if not text:
        return []
    try:
        from core.ctf import flagjudge
        cands = flagjudge.extract_candidates(text)
        picked = flagjudge.confident_flags(cands, need=limit)
    except Exception:  # noqa: BLE001 —— 抽取失败绝不阻断回执
        return []
    if not picked:
        return []
    # 稳定排序：置信分降序（confident_flags 已按此序截断，这里只为可读性固定）
    return sorted(picked, key=lambda t: (-flagjudge.flagishness(t), t))


def summarize_output(text: str, *, limit: int = _SUMMARY_SALIENT_MAX) -> str:
    """把一次工具输出压成「结构化信号摘要」块（空=无信号，调用方不附加）。

    工具层在 `run_cmd` 返回后调用本函数，把机械抽取的端口/URL/凭据/要点附在
    回执尾部——模型不必再从几百行噪音里自己找，也不会漏掉 `22/tcp open ssh`
    这种只有机器才看得准的细节。

    输出形态（只列有内容的段，全部单行，便于模型一眼扫完）::

        [观测] 端口 2 个: 10.0.0.5 → 22,80
        [观测] URL: a.com/admin, a.com/api
        [观测] 凭据: admin:Passw0rd123（1 组，须自行验证有效性）
        [观测] flag 候选: flag{a1b2c3d4e5f6}（1 个，须核实后提交）
        [观测] 要点: 22/tcp open ssh · Server: nginx/1.24.0
    """
    if not text:
        return ""
    parts: list[str] = []
    hosts = parse_nmap(text)
    if hosts:
        chunks = []
        for host, info in list(hosts.items())[:_SUMMARY_PORTS_MAX]:
            ports = ",".join(str(p) for p in info["ports"])
            chunks.append(f"{host} → {ports}")
        parts.append(f"[观测] 端口: {'; '.join(chunks)}")
    http = parse_http_signals(text)
    urls = (http.get("urls") or [])[:5]
    paths = (http.get("paths") or [])[:5]
    if urls or paths:
        shown = ", ".join(urls or paths)
        parts.append(f"[观测] 路径/URL: {shown}")
    creds = parse_credentials(text, limit=_SUMMARY_CREDS_MAX)
    if creds:
        shown = ", ".join(
            (f"{c['user']}:{c['secret']}" if c["user"] else c["secret"])
            for c in creds)
        parts.append(f"[观测] 凭据 {len(creds)} 组: {shown}"
                     "（机械抽取，有效性须自行验证）")
    flags = extract_flags(text)
    if flags:
        parts.append(f"[观测] flag 候选 {len(flags)} 个: {', '.join(flags)}"
                     "（须核实真伪，正式提交用 ctf_submit_flag）")
    salient = salient_lines(text, limit=limit)
    if salient:
        parts.append("[观测] 要点: " + " · ".join(s[:120] for s in salient))
    return "\n".join(parts)


# ---------------- 跨回执一致性：linktype ----------------

# **要解决的问题**（P2-1，2026-10-20 复盘）：同一道 USB 流量题，两个 worker
# 分别报「linktype 220」和「linktype 249」，主控两条都信，于是按两个不同的
# 链路层布局各解一遍——其中至少一次是白跑的。
#
# **为什么这是「跨回执」而不是「单条校验」**：单条回执里 `linktype 220` 语法
# 完全合法，无从判断对错。矛盾只在**两条回执并置**时才出现。故本函数只做
# **抽取**，判定交给 `session`（它才有跨轮次视野）。
#
# 判据刻意收窄——只认显式声明，不认「看起来像」：
#   - `linktype 220` / `link type: 220` / `link_type=220`
#   - `Encapsulation type: ... (220)`（tshark `-V` 的标准措辞）
#   - `LINKTYPE_USB_LINUX_MMAPPED (220)`
# **不认**裸数字（`0x000000dc` 之类）：那可能是任意偏移量，误报会把正常回执
# 判成矛盾，比漏报危险得多（假警报会训练模型忽略真警报）。
#
# 间隔上限 80 而非 40：tshark 的 `Encapsulation type: USB packets with Linux
# header and padding (220)` 中间有 46 个字符，40 会漏掉这条最常见的措辞。
# 但**不许跨行**、**不许中途出现括号**——中途遇到 `(` 说明那是另一组数字。
_LINKTYPE_RX = re.compile(
    r"(?:link[\s_-]*type|encapsulation\s+type|LINKTYPE_[A-Z0-9_]+)"
    r"[^0-9\r\n(]{0,80}\(?\s*(\d{1,3})\s*\)?",
    re.I)


def parse_linktypes(text: str) -> list[int]:
    """抽取文本里显式声明的 pcapng linktype 值（去重、保序、限合理区间）。

    只做抽取不做判断：``[220, 249]`` 是「两个都报了」，是不是矛盾由调用方
    结合「是否同一文件/同一题」判定。
    """
    out: list[int] = []
    for m in _LINKTYPE_RX.finditer(text or ""):
        try:
            v = int(m.group(1))
        except (TypeError, ValueError):
            continue
        # pcapng 的 LinkType 是 16 位；但实际用到的都在 300 以内。
        # 上限放宽到 1024 挡住「(2024)」这种年份误抽。
        if not (1 <= v <= 1024):
            continue
        if v not in out:
            out.append(v)
    return out


# ---------------- 跨回执一致性：重复产物 ----------------

# **要解决的问题**（P2-2，2026-10-20 复盘）：同一道题里三个 worker 各写了一遍
# USB 解析器（`solve_usb.py` / `usbpcap_extract.py` / `parse_usb.py`），主控全程
# 不知道——因为每个 worker 只看自己的回执。这是**方向重叠的事后铁证**。
#
# **为什么只做事后信号、不做拦截**：发现重复时活已经干完了，拦不住任何东西。
# 它的价值在两处：① 事后复盘调 `_TRIAGE_RX` 的覆盖面；② 本轮把「已有哪些脚本」
# 注入给后续 worker，避免第四遍。
#
# 判据：`<名字>.py` / `<名字>.sh`，名字**必须**带脚本语义词（solve/parse/
# extract/decode/carve/dump/...），与 `chat/runtime.py::_REUSE_SCRIPT_RX` 同源。
# 不认 `a.py`/`main.py` 这种泛指——否则随便一个临时文件都被当「重复产物」。
_SCRIPT_NAME_RX = re.compile(
    # 前缀断言**不能**含 `/`：否则 `/workspace/scratch/solve_x.py` 这种带路径的
    # 产物名整条漏掉——而回执里最常出现的正是带路径的形式。`\w` 已含 `_`，
    # 故 `my_solve.py` 不会从 `solve.py` 处二次匹配（那是同一个名字的后半）。
    r"(?<![\w.-])"
    r"((?:[A-Za-z0-9_\-]*[_\-.])?"
    r"(?:solve|solver|extract|extractor|decode|decoder|parse|parser|"
    r"carve|dump|recover|unpack|exp|exploit|poc)"
    r"(?:[_\-.][A-Za-z0-9_\-]+)*"
    r"\.(?:py|sh))",
    re.I)


def parse_script_names(text: str) -> list[str]:
    """从回执里抽「有语义的脚本名」（去重、保序、只取 basename）。

    `parse_script_names('写了 solve_usb.py 和 main.py')` → `['solve_usb.py']`：
    `main.py` 是泛指，不算产物。
    """
    out: list[str] = []
    for m in _SCRIPT_NAME_RX.finditer(text or ""):
        name = m.group(1)
        if name.lower() in ("main.py", "test.py", "a.py", "b.py"):
            continue
        if name not in out:
            out.append(name)
    return out
