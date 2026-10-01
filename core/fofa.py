"""FOFA 第三方中转客户端（cyberspace-mapping M2，2026-09-23）。

中转平台（docs/第三方fofo开发文档.txt）：API 与官方完全兼容，只换 base url——
主 https://fofoapi.com（更快）备 http://107.173.248.139:18999。错误签名四态：
「账号无效」=base url 没换（ConfigError）、「key 不存在」=key 错（AuthError）、
「已用完」=配额耗尽必须立即停止防封号（QuotaExhausted）、
「[官方错误信息] [code]」=官方侧问题可重试（OfficialRetryable）。

⚠ 信任边界：查询语句与 key 明文流经第三方中转——敏感项目慎用（前端需注记）。
transport 可注入（测试 fake 零触网）；默认 httpx 就地导入。
"""

import base64
import json
import re
from pathlib import Path

__all__ = ["DEFAULT_BASE_URL", "FALLBACK_BASE_URL", "SEARCH_FIELDS", "MAX_SIZE",
           "FofaError", "ConfigError", "AuthError", "QuotaExhausted",
           "OfficialRetryable", "FofaClient", "load_fofa_config",
           "save_fofa_config", "mask_key", "CONFIG_PATH"]

DEFAULT_BASE_URL = "https://fofoapi.com"
FALLBACK_BASE_URL = "http://107.173.248.139:18999"
# 展示字段白名单（官方兼容；不含 header/banner/cert——那三者 size 上限降到 2000）
SEARCH_FIELDS = ("ip", "port", "protocol", "host", "domain", "title", "product")
MAX_SIZE = 10000       # 中转单次上限（文档：默认最大输出 10000 条；展示字段白名单不含
                       # header/banner/cert，故不受其 2000 限制）。总翻页仍受 MAX_TOTAL 约束。
MAX_TOTAL = 10000      # 官方翻页总量上限（页*条≤1 万）

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "fofa.json"


class FofaError(Exception):
    """FOFA 中转错误基类（message 面向用户可直接展示）。"""


class ConfigError(FofaError):
    """「账号无效」——base url 没换成中转地址（打到了官方端点）。"""


class AuthError(FofaError):
    """「key 不存在」——key 填错（检查引号/空格）。"""


class QuotaExhausted(FofaError):
    """「已用完」——配额耗尽，必须立即停止请求防封号（API 层转 429）。"""


class OfficialRetryable(FofaError):
    """「[官方错误信息] [code]」——官方侧问题，等待或换备用端点重试。"""


def _classify(text: str) -> FofaError | None:
    """按错误文本签名分类（顺序敏感：「已用完」最危险最先判）。"""
    t = str(text or "")
    if "已用完" in t:
        return QuotaExhausted("FOFA 配额已用完——已停止查询，请续费或等待明日重置"
                              "（继续请求可能封号）")
    if "账号无效" in t:
        return ConfigError("FOFA「账号无效」——base url 未指向第三方中转"
                           f"（应为 {DEFAULT_BASE_URL}）")
    if "key 不存在" in t or "key不存在" in t:
        return AuthError("FOFA「key 不存在」——key 填错（检查是否带引号/空格）")
    if "[官方错误信息]" in t:
        return OfficialRetryable(f"FOFA 官方侧错误（可重试）: {t[:200]}")
    return None


def mask_key(key: str) -> str:
    """key 脱敏：前 4 后 4 保留，中段全 *；≤8 位全 *；空串返回空。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "*" * len(k)
    return k[:4] + "*" * (len(k) - 8) + k[-4:]


def load_fofa_config(path) -> dict:
    """读配置（缺失/坏 JSON/非 dict → {}，绝不抛——GET 端点永不 500）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_fofa_config(path, cfg: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")


def _split_products(v: str) -> list[str]:
    parts = re.split(r"[,;，；|]+", str(v or ""))
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out[:20]


class FofaClient:
    """中转客户端。transport 可注入：对象需带 ``get(url, params=, timeout=)``
    返回含 ``status_code`` / ``text`` / ``json()`` 的响应（镜像 httpx 接口）。"""

    def __init__(self, base_url: str, key: str, timeout: float = 20.0,
                 transport=None):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.key = (key or "").strip()
        self.timeout = timeout
        self._transport = transport

    @classmethod
    def from_config(cls, path, timeout: float = 20.0, transport=None):
        cfg = load_fofa_config(path)
        return cls(cfg.get("base_url") or DEFAULT_BASE_URL,
                   cfg.get("key") or "", timeout=timeout, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    # ---- 请求与主备切换 ----

    def _request(self, base: str, path: str, params: dict):
        if self._transport is not None:
            return self._transport.get(base + path, params=params,
                                       timeout=self.timeout)
        import httpx  # 就地导入：未装 httpx 时其余功能（config/mask）仍可用
        return httpx.get(base + path, params=params, timeout=self.timeout)

    def _get(self, path: str, params: dict) -> dict:
        """主→备切换：网络错误 / HTTP 非 200 / 响应非 JSON / OfficialRetryable
        换备用重试一次；Config/Auth/Quota 语义错误直接抛（换端点无用，配额
        耗尽更是必须立即停）。两次都失败抛最后一次错误。"""
        bases = [self.base_url]
        fb = FALLBACK_BASE_URL
        if fb not in bases:
            bases.append(fb)
        last: Exception = FofaError("FOFA 请求失败")
        for base in bases:
            try:
                resp = self._request(base, path, params)
            except Exception as e:  # noqa: BLE001 — 网络层错误（超时/连接等）→ 备用
                last = FofaError(f"FOFA 网络错误（{base}）: {e}")
                continue
            if resp.status_code != 200:
                last = FofaError(f"FOFA HTTP {resp.status_code}（{base}）")
                continue
            try:
                data = resp.json()
            except Exception:
                # 中转偶发明文错误（非 JSON）：签名可识别的直接判死
                cls = _classify(resp.text)
                if cls is not None and not isinstance(cls, OfficialRetryable):
                    raise cls
                last = FofaError(f"FOFA 响应非 JSON（{base}）: {(resp.text or '')[:120]}")
                continue
            err = self._check_error(data)
            if err is None:
                return data
            if isinstance(err, OfficialRetryable):
                last = err
                continue
            raise err
        raise last

    @staticmethod
    def _check_error(data) -> FofaError | None:
        if not isinstance(data, dict):
            return FofaError(f"FOFA 响应格式异常: {str(data)[:120]}")
        if not data.get("error"):
            return None
        msg = str(data.get("errmsg") or data.get("msg") or "")
        if not msg:
            return FofaError("FOFA 未知错误（error=true 无 errmsg）")
        return _classify(msg) or FofaError(f"FOFA 错误: {msg[:200]}")

    # ---- 业务接口 ----

    def search(self, query: str, size: int = 100, page: int = 1) -> dict:
        """查询（消耗配额）：qbase64 编码 + size clamp(1..MAX_SIZE=10000) + page clamp
        （页×条≤官方 1 万上限）。返回 {total, size, page, rows:[…, products]}，
        行字段按响应回显 fields 序归一化（缺失回退请求序）。"""
        if not self.key:
            raise ConfigError("FOFA 未配置 key——先在「网络空间测绘」设置里填入")
        size = max(1, min(int(size or 100), MAX_SIZE))
        page = max(1, min(int(page or 1), max(1, MAX_TOTAL // size)))
        params = {
            "qbase64": base64.b64encode(query.encode("utf-8")).decode("ascii"),
            "key": self.key,
            "fields": ",".join(SEARCH_FIELDS),
            "size": size,
            "page": page,
        }
        data = self._get("/api/v1/search/all", params)
        order = [f.strip() for f in str(data.get("fields") or "").split(",")
                 if f.strip()] or list(SEARCH_FIELDS)
        rows: list[dict] = []
        for arr in data.get("results") or []:
            rec = {k: ("" if v is None else v) for k, v in zip(order, arr)}
            rec["products"] = _split_products(str(rec.pop("product", "") or ""))
            rows.append(rec)
        total = data.get("size")
        return {"total": total if isinstance(total, int) else len(rows),
                "size": size, "page": page, "rows": rows}

    def info_my(self) -> dict:
        """用户信息（免费不耗配额，「测试连接」用）。宽松提取剩余/到期：
        兼容顶层与 fofa_info 嵌套两种形态，字段名不认识就给 None 不报错。"""
        if not self.key:
            raise ConfigError("FOFA 未配置 key——先在「网络空间测绘」设置里填入")
        data = self._get("/api/v1/info/my", {"key": self.key})
        nested = data.get("fofa_info")
        info = nested if isinstance(nested, dict) else data
        remain = None
        for k in ("fofa_num", "remain", "remainder", "rest", "quota"):
            if isinstance(info.get(k), (int, float)):
                remain = info[k]
                break
        expire = None
        for k in ("expire_time", "expire", "expire_at", "valid_until",
                  "deadline"):
            if info.get(k):
                expire = str(info[k])
                break
        return {"raw": data, "remain": remain, "expire": expire}
