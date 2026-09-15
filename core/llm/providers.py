"""LLM 供应商管理（DESIGN.md §8，2026-09-13）——多供应商配置/发现/工厂。

config/providers.json：
    {"providers": [
        {"name": "ark-coding", "base_url": "...", "api_key": "",
         "models": ["ark-code-latest", ...], "enabled": true}, ...]}

约定：
- models[] 有序，**第一个 = 该供应商默认模型**；全局默认 = 第一个启用供应商的第一个模型。
- api_key 留空 → 回退 ARK_API_KEY/.env（resolve_api_key），ark-coding 的标准姿态。
- API 读出一律脱敏（masked()：has_key 布尔，不回传明文）；保存时空串=保持原 key。
- 端点均为 Anthropic /v1/messages 兼容协议（AnthropicCompatProvider）。
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from core.llm.anthropic_compat import AnthropicCompatProvider
from core.llm.ark import resolve_api_key

CONFIG_PATH = "config/providers.json"
NAME_RE = re.compile(r"^[\w][\w.-]{0,63}$")

# /v1/models 不支持时的降级探活候选（实测/官方环境已知模型；用户也可手填测活）
FALLBACK_CANDIDATES = [
    "ark-code-latest",
    "deepseek-v4-flash",
    "glm-5-3-flash-260828",
]

_SEED = {
    "providers": [
        {
            "name": "ark-coding",
            "base_url": "https://ark.cn-beijing.volces.com/api/coding",
            "api_key": "",  # 留空：走 ARK_API_KEY / .env
            "models": ["ark-code-latest", "deepseek-v4-flash"],
            "enabled": True,
        },
        {
            "name": "ark-plan",
            "base_url": "https://ark.cn-beijing.volces.com/api/plan",
            "api_key": "",  # 本机在设置页/配置文件中填入
            "models": ["ark-code-latest", "deepseek-v4-flash", "glm-5-3-flash-260828"],
            "enabled": True,
        },
    ]
}


class ProviderError(ValueError):
    """供应商配置错误（名称非法 / 模型为空 / 无启用供应商等）。"""


# ---------- HTTP（可注入，测试不触网） ----------

HttpGetter = Callable[[str, dict[str, str], float], tuple[int, dict[str, Any] | None]]


def _http_get(url: str, headers: dict[str, str], timeout: float = 20.0
              ) -> tuple[int, dict[str, Any] | None]:
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": {"message": raw[:300]}}


# ---------- 存储 ----------

class ProviderStore:
    def __init__(self, path: str | Path = CONFIG_PATH, *, seed: bool = True):
        self.path = Path(path)
        if seed and not self.path.is_file():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(_SEED, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return json.loads(json.dumps(_SEED["providers"]))  # 深拷贝种子
        data = json.loads(self.path.read_text(encoding="utf-8"))
        providers = data.get("providers", [])
        return providers

    def save(self, providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """整表保存（PUT 语义）。api_key 空串=沿用库中同名供应商原 key。返回落库后的明文表。"""
        normalized = [self._validate(p) for p in providers]
        if not any(p["enabled"] for p in normalized):
            raise ProviderError("至少保留一个启用供应商，否则 Agent 无模型可用")
        old = {p["name"]: p for p in self.load()}
        for p in normalized:
            if not p.get("api_key") and p["name"] in old:
                p["api_key"] = old[p["name"]].get("api_key", "")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"providers": normalized}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return normalized

    @staticmethod
    def _validate(p: dict[str, Any]) -> dict[str, Any]:
        name = str(p.get("name", "")).strip()
        if not NAME_RE.match(name):
            raise ProviderError(f"供应商名称非法（字母数字._-，1-64 字符）: {name!r}")
        base_url = str(p.get("base_url", "")).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ProviderError(f"{name}: base_url 须为 http(s) 地址")
        models = [str(m).strip() for m in p.get("models", []) if str(m).strip()]
        if not models:
            raise ProviderError(f"{name}: 至少勾选/填写一个模型")
        return {
            "name": name,
            "base_url": base_url,
            "api_key": str(p.get("api_key", "")),
            "models": models,
            "enabled": bool(p.get("enabled", True)),
        }

    def masked(self) -> list[dict[str, Any]]:
        """API 出参：不回传明文 key。"""
        out = []
        for p in self.load():
            q = {k: v for k, v in p.items() if k != "api_key"}
            q["has_key"] = bool(p.get("api_key"))
            out.append(q)
        return out

    def get(self, name: str) -> dict[str, Any]:
        for p in self.load():
            if p["name"] == name:
                return p
        raise ProviderError(f"供应商不存在: {name}")

    def default(self) -> dict[str, Any]:
        for p in self.load():
            if p.get("enabled", True):
                return p
        raise ProviderError("无启用供应商")

    def default_target(self) -> tuple[str, str]:
        p = self.default()
        return p["name"], p["models"][0]

    # ---------- 工厂 ----------

    def resolve_key(self, p: dict[str, Any]) -> str:
        if p.get("api_key"):
            return p["api_key"]
        try:
            return resolve_api_key()
        except ValueError as e:
            raise ProviderError(
                f"供应商 {p['name']} 未配置 api_key，且环境/.env 也无 ARK_API_KEY") from e

    def build(self, name: str | None = None, model: str | None = None
              ) -> AnthropicCompatProvider:
        """构造 provider 实例。name=None → 全局默认供应商；model=None → 其默认模型。"""
        p = self.default() if name is None else self.get(name)
        if not p.get("enabled", True):
            raise ProviderError(f"供应商 {p['name']} 已停用")
        model = model or p["models"][0]
        return AnthropicCompatProvider(
            base_url=p["base_url"], api_key=self.resolve_key(p), model=model)

    # ---------- 发现 / 探活 ----------

    def discover(self, name: str, *, http_get: HttpGetter | None = None,
                 prober: Callable[[str], bool] | None = None
                 ) -> dict[str, Any]:
        """按已保存供应商名发现模型（key 空→env/.env 回退）。"""
        p = self.get(name)
        probe = prober or (lambda model: self.probe(name, model))
        return self.discover_credentials(
            p["base_url"], self.resolve_key(p), saved_models=p.get("models", []),
            http_get=http_get, prober=probe)

    def discover_credentials(self, base_url: str, api_key: str, *,
                             saved_models: list[str] | None = None,
                             http_get: HttpGetter | None = None,
                             prober: Callable[[str], bool] | None = None
                             ) -> dict[str, Any]:
        """获取某端点支持的模型（未保存的新供应商也可用）。

        1) GET {base_url}/v1/models（Bearer）——ark-coding 支持，返回 data[]（过滤 Shutdown）。
        2) 404/不支持（ark-plan 实测）→ 候选清单 + 已保存模型逐个最小调用探活。
        返回 {listed: bool, models: [{id, status?}], probed: [...可用...]}。
        """
        getter = http_get or _http_get
        status, data = getter(
            f"{base_url.rstrip('/')}/v1/models",
            {"Authorization": f"Bearer {api_key}", "anthropic-version": "2023-06-01"},
        )
        if status == 200 and isinstance(data, dict) and isinstance(data.get("data"), list):
            models = []
            for m in data["data"]:
                mid = m.get("id")
                if not mid or m.get("status") == "Shutdown":
                    continue
                models.append({"id": mid, "status": m.get("status")})
            return {"listed": True, "models": models}
        if status not in (404, 405):
            raise ProviderError(
                f"模型列表接口返回 HTTP {status}: {(data or {}).get('error', '') if data else ''}")
        # 降级：候选探活
        probe = prober or (lambda model: _probe_safe(base_url, api_key, model))
        candidates: list[str] = []
        for m in FALLBACK_CANDIDATES + (saved_models or []):
            if m not in candidates:
                candidates.append(m)
        ok = [m for m in candidates if probe(m)]
        return {"listed": False, "models": [{"id": m} for m in ok], "probed": ok}

    def probe(self, name: str, model: str) -> bool:
        """单模型最小调用探活（/v1/messages，max_tokens=8）。"""
        p = self.get(name)
        probe_credentials(p["base_url"], self.resolve_key(p), model)
        return True


def probe_credentials(base_url: str, api_key: str, model: str) -> None:
    """手填测活：任意 base_url/key/model 组合，不通抛 ProviderError/LLMError。"""
    provider = AnthropicCompatProvider(
        base_url=base_url.rstrip("/"), api_key=api_key, model=model, timeout=30.0)
    provider.chat(
        [{"role": "user", "content": "reply exactly: OK"}], max_tokens=8)


def _probe_safe(base_url: str, api_key: str, model: str) -> bool:
    """候选探活用：不通返回 False，不抛异常（一次坏候选不影响整体发现）。"""
    try:
        probe_credentials(base_url, api_key, model)
        return True
    except Exception:  # noqa: BLE001
        return False
