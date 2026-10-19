"""凭据弹药管线（借鉴 hxbai `engine/ammo.py`，2026-10-19）。

**要解决的问题**：CTF 里的「弱口令/默认口令」题，模型现在的做法是凭记忆打几个
`admin/admin`、`admin/123456` 就放弃。真实可用的做法是把**题面线索**（产品名、
年份、域名、用户名）机械展开成一份几十到几百条的候选口令表——这件事不需要
LLM，只需要确定性变异。

hxbai 的做法（本模块照搬并扩展）：

1. **产品指纹** `harvest_products`：从响应头/版本号/中文厂商名里认出
   `weaver / seeyon / tomcat / nacos …`。只在有指纹语境（`Server:`、`v1.2.3`、
   `指纹`、`nuclei`…）时才认，避免把普通英文单词当产品。
2. **厂商默认口令** `vendor_default_creds`：`Product`、`product@2026`、
   `Product@1234`…（hxbai 原表 + 近 8 年）。
3. **上下文变异** `mutate`：把题面里出现的用户名/年份/公司名展开成
   `x`、`x123`、`x!`、`x@2025`、leet（`p@ssw0rd`）等。
4. **候选合成** `build_candidates`：线索优先、观测次之、已知凭据兜底，去重截断。

**本项目的扩展**：

- `default_users(product)` —— hxbai 只产密码，但真实爆破要 `user:pass` 成对；
  本模块内置各产品常见默认账号（`tomcat/tomcat`、`nacos/nacos`…），
  未知产品退回 `admin/root/administrator/test/guest`。
- `build_cred_pairs` / `render_creds` —— 直接产出 hydra/ffuf/自定义脚本能吃的
  `user:pass` 行，供工具层写进临时文件后交给执行器。

**口径**：本模块只产**候选**，不执行爆破、不做任何网络请求；是否用、用多少条、
要不要限速由工具层与提示词纪律决定。
"""

from __future__ import annotations

import datetime
import re

__all__ = [
    "harvest_tokens",
    "harvest_products",
    "vendor_default_creds",
    "default_users",
    "mutate",
    "build_candidates",
    "build_cred_pairs",
    "render_creds",
    "PRODUCTS",
    "MAX_CANDIDATES",
]

# 单次产出的候选上限：再多人工也看不完，且会把工具回执撑爆
MAX_CANDIDATES = 500

# 凭据上下文键名（`user=` / `login:` / `host=` …）
_CRED_CTX_RX = re.compile(
    r"(?:user(?:name)?|login|account|owner|uid|hostname|host|realm|domain|"
    r"server|x-powered-by|powered-by)\s*[:=]\s*([A-Za-z][A-Za-z0-9_.\-]{1,30})", re.I)
# 中文题面语境：`管理员叫 alice` / `用户名为 bob` / `账号：root`
# 中文 CTF 题面几乎不会写 `user=`，只写自然语言——不认这种形态等于线索全丢。
_ZH_CRED_CTX_RX = re.compile(
    r"(?:管理员|管理账号|用户名|用户|账号|账户|登录名|运维|维护人员|负责人)"
    r"\s*(?:叫做|叫|是|为|：|:|=)\s*"
    r"([A-Za-z][A-Za-z0-9_.\-]{1,30})")
_YEAR_RX = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
# flag 形态：绝不能被当口令候选（会把答案写进字典，等于自己泄题）
_FLAG_RX = re.compile(r"[A-Za-z][\w.\-]{1,30}\{[^}]{1,}\}")
_STOP = {"nginx", "apache", "iis", "http", "https", "html", "php", "tcp", "ssl",
         "tls", "true", "false", "none", "utf", "charset", "gzip"}
_LEET = str.maketrans({"e": "3", "a": "4", "o": "0", "i": "1", "s": "5"})

# 有默认口令问题的产品（CTF 常见 + 国产 OA/中间件）
PRODUCTS = frozenset({
    "weaver", "ecology", "seeyon", "landray", "thinkphp", "tomcat", "weblogic",
    "jboss", "ofbiz", "confluence", "jenkins", "gitlab", "grafana", "zabbix",
    "nexus", "solr", "jira", "wordpress", "joomla", "drupal", "phpmyadmin",
    "nacos", "harbor", "rancher", "minio", "kibana", "elastic", "1panel",
    "comfyui", "jumpserver", "kkfileview", "dahua", "hikvision", "sangfor",
    "activemq", "rabbitmq", "redis", "mongodb", "mysql", "postgres", "ftp",
    "ssh", "smtp", "ldap", "phpshe", "shopxo", "discuz", "typecho",
})
_PRODUCT_ALIASES = {
    "泛微": "weaver", "致远": "seeyon", "蓝凌": "landray", "深信服": "sangfor",
    "用友": "yonyou", "金蝶": "kingdee", "宝塔": "1panel", "海康": "hikvision",
    "大华": "dahua",
}
_PRODUCT_TOK_RX = re.compile(r"[A-Za-z][A-Za-z0-9]{2,20}")
# 产品指纹语境：没有这些线索时，普通英文单词不算产品
_FP_CUE = re.compile(
    r"\bv?\d+\.\d+"
    r"|server\s*[:=]|x-powered-by|powered[ -]?by"
    r"|\bversion\b|\brelease\b|\bbuild\b|banner"
    r"|\bOA\b|\bCMS\b|framework|webapp"
    r"|/\w+\.(?:php|jsp|jspx|do|action|aspx)"
    r"|指纹|fingerprint|nuclei|whatweb",
    re.I)

# 各产品默认账号（口令由 vendor_default_creds 生成）
_DEFAULT_USERS = {
    "weaver": ("sysadmin", "admin"),
    "ecology": ("sysadmin", "admin"),
    "seeyon": ("admin", "system"),
    "landray": ("admin", "sysadmin"),
    "tomcat": ("tomcat", "manager", "admin"),
    "weblogic": ("weblogic", "admin"),
    "jboss": ("admin", "jboss"),
    "ofbiz": ("admin",),
    "confluence": ("admin",),
    "jenkins": ("admin", "jenkins"),
    "gitlab": ("root",),
    "grafana": ("admin",),
    "zabbix": ("Admin", "admin"),
    "nexus": ("admin",),
    "solr": ("solr", "admin"),
    "jira": ("admin",),
    "wordpress": ("admin", "wordpress"),
    "joomla": ("admin",),
    "drupal": ("admin",),
    "phpmyadmin": ("root", "pma"),
    "nacos": ("nacos", "admin"),
    "harbor": ("admin",),
    "rancher": ("admin",),
    "minio": ("minioadmin", "admin"),
    "kibana": ("elastic", "kibana"),
    "elastic": ("elastic",),
    "1panel": ("admin",),
    "comfyui": ("admin",),
    "jumpserver": ("admin",),
    "kkfileview": ("admin",),
    "dahua": ("admin", "888888"),
    "hikvision": ("admin",),
    "sangfor": ("admin",),
    "activemq": ("admin",),
    "rabbitmq": ("guest", "admin"),
    "redis": ("redis", "default"),
    "mongodb": ("admin", "mongodb"),
    "mysql": ("root",),
    "postgres": ("postgres",),
    "ftp": ("ftp", "anonymous", "admin"),
    "ssh": ("root", "admin", "ubuntu"),
    "smtp": ("admin", "postmaster"),
    "ldap": ("admin", "cn=admin"),
    "discuz": ("admin",),
    "typecho": ("admin",),
    "phpshe": ("admin",),
    "shopxo": ("admin",),
    "thinkphp": ("admin",),
    "yonyou": ("admin",),
    "kingdee": ("admin", "administrator"),
}
_GENERIC_USERS = ("admin", "root", "administrator", "test", "guest", "user")


def _now_year(year: int | None) -> int:
    return int(year) if year else datetime.datetime.now().year


def _suffixes(year: int | None = None) -> tuple[str, ...]:
    """口令后缀表：常见弱后缀 + 近 3 年（含 `@年` 形态）。"""
    y = _now_year(year)
    years = tuple(str(y - d) for d in range(0, 3))
    return ("", "123", "1234", "12345", "123456", "!", "@123", "#123",
            "123!", "888888") + years + tuple("@" + v for v in years)


# ---------------- 抽取 ----------------

def harvest_tokens(text: str) -> list[str]:
    """抽「可当口令素材」的词：`user=` 后的值、中文语境里点名的账号、年份。

    中文题面（`提示：管理员叫 alice`）不会写 `user=`，故除英文语境键外还要
    认「管理员/用户名/账号 + 叫/是/为/：」这种自然语言线索。
    """
    if not text:
        return []
    text = _FLAG_RX.sub(" ", text)
    out: list[str] = []
    seen: set[str] = set()

    def _take(m: re.Match) -> None:
        t = m.group(1)
        low = t.lower()
        if low in _STOP or low in seen or len(t) < 2:
            return
        seen.add(low)
        out.append(t)

    for m in _CRED_CTX_RX.finditer(text):
        _take(m)
    for m in _ZH_CRED_CTX_RX.finditer(text):
        _take(m)
    for m in _YEAR_RX.finditer(text):
        y = m.group(0)
        if y not in seen:
            seen.add(y)
            out.append(y)
    return out


def harvest_products(text: str, extra: list[str] | None = None) -> list[str]:
    """认产品指纹（中英文别名 + 白名单词），需有指纹语境才认。"""
    found: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        p = (p or "").lower()
        if p and p in PRODUCTS and p not in seen:
            seen.add(p)
            found.append(p)

    def _scan(s: str) -> None:
        if not s or not _FP_CUE.search(s):
            return
        for zh, en in sorted(_PRODUCT_ALIASES.items()):
            if zh in s:
                _add(en)
        low = s.lower()
        for p in sorted(PRODUCTS):
            if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low):
                _add(p)

    _scan(" ".join(str(t) for t in (extra or [])))
    _scan(text or "")
    return found


def default_users(product: str) -> list[str]:
    """该产品的常见默认账号；未知产品退回通用账号表。"""
    p = (product or "").lower()
    users = _DEFAULT_USERS.get(p)
    if users:
        return list(users)
    # 产品名本身常被当账号（`nacos/nacos`、`tomcat/tomcat`）
    return [p] + list(_GENERIC_USERS) if p else list(_GENERIC_USERS)


# ---------------- 生成 ----------------

def vendor_default_creds(product: str, *, year: int | None = None) -> list[str]:
    """厂商默认口令候选（大小写两版 + 常见后缀 + 近 8 年年份）。"""
    p = (product or "").lower()
    if not p:
        return []
    P = p[:1].upper() + p[1:]
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        if s and s not in seen and not _FLAG_RX.search(s):
            seen.add(s)
            out.append(s)

    for s in (p, P, p + "123", P + "123", p + "@123", P + "@123",
              p + "@1234", P + "@1234", p + "_admin", "admin@" + p,
              "admin", p + "!"):
        _add(s)
    y0 = _now_year(year)
    for y in (str(y0 - d) for d in range(0, 8)):
        _add(P + "@" + y)
        _add(p + "@" + y)
        _add(P + y)
        _add(p + y)
    return out


def mutate(base: str, *, year: int | None = None) -> list[str]:
    """把一个词展开成口令变体（后缀、首字母大写、leet）。"""
    if not base:
        return []
    low = base.lower()
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        if s and s not in seen:
            seen.add(s)
            out.append(s)

    for suf in _suffixes(year):
        _add(low + suf)
    _add(base[:1].upper() + base[1:])
    _add(low.translate(_LEET))
    return out


def build_candidates(obs_text: str, *, hint_text: str | None = None,
                     known_creds: list[str] | None = None,
                     product_tokens: list[str] | None = None,
                     year: int | None = None,
                     limit: int = MAX_CANDIDATES) -> list[str]:
    """合成口令候选表。

    优先级：题面线索（hint_text）> 已知凭据 > 观测里的产品指纹 > 观测里的词。
    高优先级的排在前面，执行器按序试到 limit 就停——所以顺序即价值。
    """
    out: list[str] = []
    seen: set[str] = set()
    limit = max(1, int(limit or MAX_CANDIDATES))

    def _add(s: str) -> None:
        if s and s not in seen and not _FLAG_RX.search(s) and len(out) < limit:
            seen.add(s)
            out.append(s)

    if hint_text:
        for prod in harvest_products(hint_text):
            for cand in vendor_default_creds(prod, year=year):
                _add(cand)
        for tok in harvest_tokens(hint_text):
            for cand in mutate(tok, year=year):
                _add(cand)
    for cred in (known_creds or []):
        # 允许 `user:pass` 或裸口令两种形态，只取口令部分
        pw = cred.split(":", 1)[1] if ":" in cred else cred
        _add(pw.strip())
    for prod in harvest_products(obs_text, extra=product_tokens):
        for cand in vendor_default_creds(prod, year=year):
            _add(cand)
    for tok in harvest_tokens(obs_text):
        for cand in mutate(tok, year=year):
            _add(cand)
    return out


def build_cred_pairs(obs_text: str, *, users: list[str] | None = None,
                     hint_text: str | None = None,
                     known_creds: list[str] | None = None,
                     product_tokens: list[str] | None = None,
                     year: int | None = None,
                     limit: int = MAX_CANDIDATES) -> list[str]:
    """产出 `user:pass` 对（本模块相对 hxbai 的扩展）。

    账号来源：显式 ``users`` > 观测/线索里认出的产品的默认账号 > 通用账号。
    为控制组合爆炸，按「账号 × 口令」笛卡尔积取前 limit 条，且优先把
    `admin` 与产品同名账号排前面。
    """
    pws = build_candidates(obs_text, hint_text=hint_text, known_creds=known_creds,
                           product_tokens=product_tokens, year=year,
                           limit=limit)
    if not pws:
        return []
    if users:
        us = [u.strip() for u in users if u and u.strip()]
    else:
        us = []
        for prod in (harvest_products(hint_text or "")
                     + harvest_products(obs_text, extra=product_tokens)):
            for u in default_users(prod):
                if u not in us:
                    us.append(u)
        if not us:
            us = list(_GENERIC_USERS)
    out: list[str] = []
    for u in us:
        for p in pws:
            out.append(f"{u}:{p}")
            if len(out) >= limit:
                return out
    return out


def render_creds(pairs, *, header: str = "") -> str:
    """把 `user:pass` 列表渲染成文件内容（每行一条，可选注释头）。"""
    lines = [p for p in (pairs or []) if p and ":" in p]
    body = "\n".join(lines)
    if header:
        return f"# {header}\n{body}\n" if body else f"# {header}\n"
    return body + "\n" if body else ""
