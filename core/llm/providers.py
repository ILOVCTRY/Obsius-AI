"""LLM 供应商管理（DESIGN.md §8，2026-09-13）——多供应商配置/发现/工厂。

config/providers.json：
    {"providers": [
        {"name": "ark-coding", "base_url": "...", "api_key": "",
         "models": ["ark-code-latest", ...], "enabled": true}, ...]}

可选字段（2026-09-30 上下文治理 ct-7，均为 provider 级；缺省走运行时模块常量）：
- "model_window": 1048566      硬窗口（优先于按模型的 "model_context"）
- "ctx_soft_budget": 512000    有效软上限（支持 1M≠在 1M 最好，超此主动压缩）
- "summarizer_model": "<model>" 专用摘要模型（缺省用会话模型）
- "proxy": "http://127.0.0.1:7890" 可选供应商专属 HTTP/HTTPS 代理；缺省沿用系统代理环境

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
from urllib.parse import urlsplit

from core.llm.anthropic_compat import AnthropicCompatProvider, CLIENT_USER_AGENT
from core.llm.openai_compat import CHAT_COMPLETIONS, RESPONSES, OpenAICompatProvider
from core.llm.ark import resolve_api_key

ANTHROPIC_MESSAGES = "anthropic-messages"
DEFAULT_FORMAT = CHAT_COMPLETIONS
SUPPORTED_FORMATS = {DEFAULT_FORMAT, RESPONSES, ANTHROPIC_MESSAGES}

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
            # 上下文治理（可选，2026-09-30 ct-7）：硬窗口/软上限/专用摘要模型；
            # 省略即用运行时模块常量（1048566 / 512000 / 会话模型）
            "model_window": 1048566,
            "ctx_soft_budget": 512000,
        },
    ]
}


class ProviderError(ValueError):
    """供应商配置错误（名称非法 / 模型为空 / 无启用供应商等）。"""


# ---------- HTTP（可注入，测试不触网） ----------

HttpGetter = Callable[[str, dict[str, str], float], tuple[int, dict[str, Any] | None]]


def _http_get(url: str, headers: dict[str, str], timeout: float = 20.0,
              proxy: str | None = None
              ) -> tuple[int, dict[str, Any] | None]:
    req = urllib.request.Request(url, method="GET")
    for k, v in {"User-Agent": CLIENT_USER_AGENT, "Accept": "application/json", **headers}.items():
        req.add_header(k, v)
    try:
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            response = opener.open(req, timeout=timeout)
        else:
            response = urllib.request.urlopen(req, timeout=timeout)
        with response as resp:
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

    def _read_default_provider(self) -> str | None:
        """JSON 顶层 default_provider（用户可选的全局默认供应商）；未设置返回 None。"""
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        v = data.get("default_provider")
        return str(v) if v else None

    def default_provider_name(self) -> str | None:
        """已设置的默认供应商名（UI 下拉用）；未设置返回 None。"""
        return self._read_default_provider()

    def save(self, providers: list[dict[str, Any]],
             default_provider: str | None = None) -> list[dict[str, Any]]:
        """整表保存（PUT 语义）。api_key 空串=沿用库中同名供应商原 key。
        default_provider 给定时必须命中提交清单中的**启用**供应商（否则 ProviderError）；
        缺省沿用已存设置。返回落库后的明文表。"""
        normalized = [self._validate(p) for p in providers]
        if not any(p["enabled"] for p in normalized):
            raise ProviderError("至少保留一个启用供应商，否则 Agent 无模型可用")
        if default_provider is not None:
            hit = next((p for p in normalized if p["name"] == default_provider), None)
            if hit is None:
                raise ProviderError(f"默认供应商不存在: {default_provider}")
            if not hit["enabled"]:
                raise ProviderError(f"默认供应商 {default_provider} 已停用，不能设为默认")
        old = {p["name"]: p for p in self.load()}
        for p in normalized:
            if not p.get("api_key") and p["name"] in old:
                p["api_key"] = old[p["name"]].get("api_key", "")
        if default_provider is None:
            default_provider = self._read_default_provider()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {"providers": normalized}
        if default_provider:
            payload["default_provider"] = default_provider
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
        protocol = str(p.get("format") or DEFAULT_FORMAT).strip()
        if protocol not in SUPPORTED_FORMATS:
            raise ProviderError(f"{name}: 不支持的兼容格式: {protocol}")
        # 每模型最大上下文（token，可选）：models 保持纯字符串列表不破坏兼容；
        # 非法项（非正数/超大/未勾选的模型）静默剔除，宁少勿滥
        raw_ctx = p.get("model_context") or {}
        ctx: dict[str, int] = {}
        if isinstance(raw_ctx, dict):
            for m, v in raw_ctx.items():
                m = str(m).strip()
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                if m in models and 0 < n <= 10_000_000:
                    ctx[m] = n
        out = {
            "name": name,
            "base_url": base_url,
            "api_key": str(p.get("api_key", "")),
            "format": protocol,
            "models": models,
            "enabled": bool(p.get("enabled", True)),
            "model_context": ctx,
        }
        # 思考链开关（2026-09-30）：显式 True/False 才落字段；None/缺失=不写，
        # 运行时按网关缺省猜（base_url 含 ark 默认开）。此前本函数白名单把
        # thinking 抹掉——设置页保存一次即丢，思考链静默消失
        thinking = p.get("thinking")
        if thinking is not None:
            out["thinking"] = bool(thinking)
        # 上下文治理（2026-09-30 ct-7）：同 thinking——显式才落字段，否则设置页
        # 保存一次即被白名单抹掉（此前 thinking 就是这么静默消失的）。三者均为
        # 可选，缺省时运行时用模块常量兜底：
        #   model_window     硬窗口（provider 级，优先于按模型的 model_context）
        #   ctx_soft_budget  有效软上限（支持 1M≠在 1M 最好，超此主动压缩）
        #   summarizer_model  专用摘要模型（缺省用会话模型）
        mw = p.get("model_window")
        if mw is not None:
            try:
                n = int(mw)
            except (TypeError, ValueError):
                n = 0
            if 0 < n <= 10_000_000:
                out["model_window"] = n
        sb = p.get("ctx_soft_budget")
        if sb is not None:
            try:
                n = int(sb)
            except (TypeError, ValueError):
                n = 0
            if 0 < n <= 10_000_000:
                out["ctx_soft_budget"] = n
        sm = str(p.get("summarizer_model") or "").strip()
        if sm:
            out["summarizer_model"] = sm
        proxy = str(p.get("proxy") or "").strip()
        if proxy:
            parsed_proxy = urlsplit(proxy)
            if (parsed_proxy.scheme not in {"http", "https"}
                    or not parsed_proxy.hostname):
                raise ProviderError(f"{name}: proxy 须为 http(s) 地址")
            out["proxy"] = proxy.rstrip("/")
        return out

    def masked(self) -> list[dict[str, Any]]:
        """API 出参：不回传明文 key。"""
        out = []
        for p in self.load():
            q = {k: v for k, v in p.items() if k != "api_key"}
            q["format"] = p.get("format", DEFAULT_FORMAT)
            q["has_key"] = bool(p.get("api_key"))
            out.append(q)
        return out

    def get(self, name: str) -> dict[str, Any]:
        for p in self.load():
            if p["name"] == name:
                return p
        raise ProviderError(f"供应商不存在: {name}")

    def default(self) -> dict[str, Any]:
        """全局默认供应商：用户显式选择的 default_provider 优先（须启用），
        未设置/已失效 → 第一个启用供应商（旧行为兜底）。"""
        named = self._read_default_provider()
        if named:
            for p in self.load():
                if p["name"] == named:
                    if p.get("enabled", True):
                        return p
                    break  # 选中的供应商已停用 → 兜底第一个启用
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

    def build(self, name: str | None = None, model: str | None = None):
        """构造与供应商兼容格式匹配的 provider 实例。"""
        p = self.default() if name is None else self.get(name)
        if not p.get("enabled", True):
            raise ProviderError(f"供应商 {p['name']} 已停用")
        model = model or p["models"][0]
        # 思考开关（2026-09-19 直播间终端化）：供应商条目 "thinking": true/false 显式控制；
        # 缺省时 Ark coding 网关默认开启（思考链路可见），其余网关默认关。模型不支持时
        # anthropic_compat 层 400 去参降级兜底，不会炸调用。
        thinking = p.get("thinking")
        if thinking is None:
            thinking = "ark" in str(p.get("base_url", "")).lower()
        # 硬窗口优先取 provider 级 model_window（2026-09-30 ct-7），否则按模型粒度
        # model_context[model]；两者皆空 → None（运行时回落 _MODEL_WINDOW_FALLBACK）。
        window = p.get("model_window") or (p.get("model_context") or {}).get(model)
        kwargs = {
            "base_url": p["base_url"], "api_key": self.resolve_key(p), "model": model,
            "enable_thinking": bool(thinking), "context_tokens": window,
            "ctx_soft_budget": p.get("ctx_soft_budget"),
            "summarizer_model": p.get("summarizer_model"),
            "proxy": p.get("proxy"),
        }
        if p.get("format", DEFAULT_FORMAT) == ANTHROPIC_MESSAGES:
            return AnthropicCompatProvider(**kwargs)
        return OpenAICompatProvider(format=p.get("format", DEFAULT_FORMAT), **kwargs)

    # ---------- 发现 / 探活 ----------

    def discover(self, name: str, *, http_get: HttpGetter | None = None,
                 prober: Callable[[str], bool] | None = None
                 ) -> dict[str, Any]:
        """按已保存供应商名发现模型（key 空→env/.env 回退）。"""
        p = self.get(name)
        probe = prober or (lambda model: self.probe(name, model))
        return self.discover_credentials(
            p["base_url"], self.resolve_key(p), format=p.get("format", DEFAULT_FORMAT),
            saved_models=p.get("models", []), http_get=http_get, prober=probe,
            proxy=p.get("proxy"))

    def discover_credentials(self, base_url: str, api_key: str, *,
                             format: str = DEFAULT_FORMAT,
                             saved_models: list[str] | None = None,
                             proxy: str | None = None,
                             http_get: HttpGetter | None = None,
                             prober: Callable[[str], bool] | None = None
                             ) -> dict[str, Any]:
        """获取某端点支持的模型（未保存的新供应商也可用）。

        1) GET {base_url}/v1/models（Bearer）——ark-coding 支持，返回 data[]（过滤 Shutdown）。
        2) 404/不支持（ark-plan 实测）→ 候选清单 + 已保存模型逐个最小调用探活。
        返回 {listed: bool, models: [{id, status?}], probed: [...可用...]}。
        """
        headers = {"Authorization": f"Bearer {api_key}"}
        if format == ANTHROPIC_MESSAGES:
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        if http_get is None:
            status, data = _http_get(f"{base_url.rstrip('/')}/v1/models", headers,
                                     proxy=proxy)
        else:
            status, data = http_get(f"{base_url.rstrip('/')}/v1/models", headers)
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
        probe = prober or (lambda model: _probe_safe(base_url, api_key, model, format,
                                                      proxy=proxy))
        candidates: list[str] = []
        for m in FALLBACK_CANDIDATES + (saved_models or []):
            if m not in candidates:
                candidates.append(m)
        ok = [m for m in candidates if probe(m)]
        return {"listed": False, "models": [{"id": m} for m in ok], "probed": ok}

    def probe(self, name: str, model: str) -> bool:
        p = self.get(name)
        probe_credentials(p["base_url"], self.resolve_key(p), model,
                          format=p.get("format", DEFAULT_FORMAT), proxy=p.get("proxy"))
        return True


def probe_credentials(base_url: str, api_key: str, model: str, *, format: str = DEFAULT_FORMAT,
                      proxy: str | None = None) -> None:
    """手填测活：任意 base_url/key/model 组合，不通抛 ProviderError/LLMError。"""
    if format == ANTHROPIC_MESSAGES:
        provider = AnthropicCompatProvider(
            base_url=base_url.rstrip("/"), api_key=api_key, model=model, timeout=30.0,
            proxy=proxy)
    else:
        provider = OpenAICompatProvider(
            base_url=base_url.rstrip("/"), api_key=api_key, model=model,
            format=format, timeout=30.0, proxy=proxy)
    provider.chat(
        [{"role": "user", "content": "reply exactly: OK"}], max_tokens=8)


def _probe_safe(base_url: str, api_key: str, model: str, format: str = DEFAULT_FORMAT,
                *, proxy: str | None = None) -> bool:
    """候选探活用：不通返回 False，不抛异常（一次坏候选不影响整体发现）。"""
    try:
        probe_credentials(base_url, api_key, model, format=format, proxy=proxy)
        return True
    except Exception:  # noqa: BLE001
        return False
