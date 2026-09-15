"""LLM 层测试：响应解析、错误路径、工具回填、模型路由、密钥解析。全部走 fake transport，不触网。"""

import json

import pytest

from core.llm import AnthropicCompatProvider, ArkCodingProvider, LLMError, ModelRouter
from core.llm.routing import load_dotenv


def _anthropic_response(content_blocks, stop_reason="end_turn", usage=None):
    return {
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
        "model": "fake-model",
    }


def _fake_transport(response=None, status=200, capture=None):
    """返回一个记录请求并吐固定响应的 transport。"""
    def transport(url, headers, body):
        if capture is not None:
            capture.append({"url": url, "headers": headers, "body": json.loads(body)})
        return status, response if status == 200 else response
    return transport


# ---------- 响应解析 ----------

def test_parse_text_thinking_and_tool_use():
    captured = []
    transport = _fake_transport(
        _anthropic_response(
            [
                {"type": "thinking", "thinking": "先看 NX"},
                {"type": "text", "text": "我先用 checksec"},
                {"type": "tool_use", "id": "tc-1", "name": "run_cmd",
                 "input": {"cmd": "checksec a.out", "runtime": "sandbox"}},
            ],
            stop_reason="tool_use",
        ),
        capture=captured,
    )
    p = AnthropicCompatProvider("https://fake", "key", "m", transport=transport)
    r = p.chat([{"role": "user", "content": "分析这个 ELF"}],
               system="你是逆向专家",
               tools=[{"name": "run_cmd", "description": "执行", "input_schema": {"type": "object"}}])
    assert r.text == "我先用 checksec"
    assert r.thinking == "先看 NX"
    assert r.tool_calls[0].name == "run_cmd"
    assert r.tool_calls[0].arguments["runtime"] == "sandbox"
    assert r.stop_reason == "tool_use"
    assert r.usage.input_tokens == 10
    # 请求侧检查：system / tools / 协议头
    req = captured[0]
    assert req["url"].endswith("/v1/messages")
    assert req["headers"]["Authorization"] == "Bearer key"
    assert req["body"]["system"] == "你是逆向专家"
    assert req["body"]["tools"][0]["name"] == "run_cmd"


def test_error_raises_llmerror():
    transport = _fake_transport(
        {"error": {"code": "InvalidEndpointOrModel.NotFound", "message": "no such model"}},
        status=404,
    )
    p = AnthropicCompatProvider("https://fake", "key", "m", transport=transport)
    with pytest.raises(LLMError, match="404"):
        p.chat([{"role": "user", "content": "hi"}])


def test_retry_on_transient_status_then_success(monkeypatch):
    """429/5xx 瞬时故障自动重试，恢复后成功——一次抖动不该杀死编排。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    statuses = iter([500, 429, 200])
    n_calls = {"n": 0}

    def transport(url, headers, body):
        n_calls["n"] += 1
        status = next(statuses)
        if status == 200:
            return 200, _anthropic_response([{"type": "text", "text": "ok"}])
        return status, {"error": {"message": "transient"}}

    p = AnthropicCompatProvider("https://fake", "key", "m", transport=transport)
    r = p.chat([{"role": "user", "content": "hi"}])
    assert r.text == "ok" and n_calls["n"] == 3


def test_retry_exhausts_then_raises(monkeypatch):
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    monkeypatch.setattr("core.llm.anthropic_compat.MAX_RETRIES", 2)

    def transport(url, headers, body):
        return 503, {"error": {"message": "down"}}

    p = AnthropicCompatProvider("https://fake", "key", "m", transport=transport)
    with pytest.raises(LLMError, match="503"):
        p.chat([{"role": "user", "content": "hi"}])


def test_retry_on_timeout(monkeypatch):
    """网络超时同样重试（演练中真实发生的故障形态）。"""
    monkeypatch.setattr("core.llm.anthropic_compat.RETRY_BACKOFF", 0)
    calls = {"n": 0}

    def transport(url, headers, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return 200, _anthropic_response([{"type": "text", "text": "recovered"}])

    p = AnthropicCompatProvider("https://fake", "key", "m", transport=transport)
    assert p.chat([{"role": "user", "content": "hi"}]).text == "recovered"


def test_tool_result_message_roundtrip():
    """工具结果回填：Agent 循环必须能把 tool_result 拼回消息流（含 is_error）。"""
    from core.llm import ToolCall
    p = AnthropicCompatProvider("https://fake", "key", "m", transport=_fake_transport({}))
    tc = ToolCall(id="tc-9", name="run_cmd", arguments={"cmd": "id"})
    msg = p.tool_result_message(tc, "uid=0(root)", is_error=False)
    assert msg["content"][0]["tool_use_id"] == "tc-9"
    err = p.tool_result_message(tc, "权限不足", is_error=True)
    assert err["content"][0]["is_error"] is True


# ---------- 模型路由 ----------

def test_router_defaults_and_override():
    r = ModelRouter()
    assert r.model_for("executor") == "ark-code-latest"
    assert r.model_for("classifier") == "deepseek-v4-flash"
    r2 = ModelRouter(models={"executor": "glm-5-3-flash-260828"})
    assert r2.model_for("executor") == "glm-5-3-flash-260828"
    with pytest.raises(ValueError, match="未知模型角色"):
        ModelRouter(models={"ceo": "x"})


def test_router_config_file(tmp_path):
    cfg = tmp_path / "llm.json"
    cfg.write_text(json.dumps({"classifier": "glm-5-3-flash-260828"}), encoding="utf-8")
    r = ModelRouter(config_path=cfg)
    assert r.model_for("classifier") == "glm-5-3-flash-260828"
    assert r.model_for("planner") == "ark-code-latest"  # 未覆盖角色保持默认


def test_router_target_for_provider_dict(tmp_path):
    """llm.json 文件级覆写：裸模型名→(None, model)；{provider,model}→(provider, model)。"""
    cfg = tmp_path / "llm.json"
    cfg.write_text(json.dumps({
        "executor": {"provider": "ark-plan", "model": "glm-5-3-flash-260828"},
        "planner": "deepseek-v4-flash",
    }), encoding="utf-8")
    r = ModelRouter(config_path=cfg)
    assert r.target_for("executor") == ("ark-plan", "glm-5-3-flash-260828")
    assert r.target_for("planner") == (None, "deepseek-v4-flash")
    assert r.target_for("classifier") is None  # 未覆写 → 走全局默认


# ---------- 供应商管理（DESIGN.md §8） ----------

def test_provider_store_seed_mask_default(tmp_path):
    from core.llm.providers import ProviderStore
    s = ProviderStore(tmp_path / "providers.json")
    assert [p["name"] for p in s.load()] == ["ark-coding", "ark-plan"]
    assert all("api_key" not in p for p in s.masked())  # 读出脱敏
    assert s.default_target() == ("ark-coding", "ark-code-latest")


def test_provider_store_save_validation_and_key_keep(tmp_path):
    from core.llm.providers import ProviderError, ProviderStore
    s = ProviderStore(tmp_path / "providers.json")
    base = [
        {"name": "ark-coding", "base_url": "https://x/api/", "api_key": "",
         "models": ["m1"], "enabled": True},
        {"name": "ark-plan", "base_url": "https://y/api", "api_key": "secret",
         "models": ["m2", "m3"], "enabled": False},
    ]
    s.save(base)
    # 空 key 保存 → 沿用原 key
    again = [dict(p, api_key="") for p in base]
    s.save(again)
    assert s.get("ark-plan")["api_key"] == "secret"
    # base_url 去尾斜杠；plan 停用中，默认仍是首个启用的 coding
    assert s.get("ark-coding")["base_url"] == "https://x/api"
    assert s.default_target() == ("ark-coding", "m1")
    # 校验三连
    with pytest.raises(ProviderError):
        s.save([dict(base[0], name="bad name"), dict(base[1], enabled=True)])
    with pytest.raises(ProviderError):
        s.save([dict(base[0], models=[]), dict(base[1], enabled=True)])
    with pytest.raises(ProviderError):
        s.save([dict(base[0], enabled=False), dict(base[1], enabled=False)])


def test_provider_store_build(tmp_path, monkeypatch):
    from core.llm.providers import ProviderError, ProviderStore
    monkeypatch.setenv("ARK_API_KEY", "env-key")
    s = ProviderStore(tmp_path / "providers.json")
    p = s.build()  # 全局默认（ark-coding，key 空 → env）
    assert p.base_url.endswith("/api/coding") and p.model == "ark-code-latest"
    assert p.api_key == "env-key"
    s.save([{"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
             "models": ["a", "b"], "enabled": True}])
    p2 = s.build("ark-coding", "b")
    assert p2.api_key == "k1" and p2.model == "b"
    with pytest.raises(ProviderError):
        s.build("nope")
    # 停用 coding（plan 仍启用）→ build 拒、默认转到 plan
    s.save([
        {"name": "ark-coding", "base_url": "https://x/api", "api_key": "k1",
         "models": ["a"], "enabled": False},
        {"name": "ark-plan", "base_url": "https://y/api", "api_key": "",
         "models": ["p"], "enabled": True},
    ])
    with pytest.raises(ProviderError):
        s.build("ark-coding")  # 已停用
    assert s.default_target() == ("ark-plan", "p")


def test_provider_discover_listed_and_fallback(tmp_path):
    """发现：/v1/models 命中→过滤 Shutdown；404→候选探活；401→报错。"""
    from core.llm.providers import ProviderError, ProviderStore
    s = ProviderStore(tmp_path / "providers.json")

    def http_listed(url, headers, timeout=20.0):
        return 200, {"data": [
            {"id": "live-model", "status": "Running"},
            {"id": "dead-model", "status": "Shutdown"},
        ]}

    out = s.discover("ark-coding", http_get=http_listed)
    assert out["listed"] is True and [m["id"] for m in out["models"]] == ["live-model"]

    def http_404(url, headers, timeout=20.0):
        return 404, None

    prober = lambda model: model == "deepseek-v4-flash"  # noqa: E731
    out = s.discover("ark-coding", http_get=http_404, prober=prober)
    assert out["listed"] is False and out["probed"] == ["deepseek-v4-flash"]

    def http_401(url, headers, timeout=20.0):
        return 401, {"error": {"message": "bad key"}}

    with pytest.raises(ProviderError, match="401"):
        s.discover("ark-coding", http_get=http_401)


# ---------- 密钥解析 ----------

def test_load_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# 注释\nARK_API_KEY=ark-test-123\nEMPTY=\n", encoding="utf-8")
    vals = load_dotenv(env)
    assert vals["ARK_API_KEY"] == "ark-test-123"
    assert "EMPTY" not in vals or vals["EMPTY"] == ""


def test_ark_key_resolution_order(tmp_path, monkeypatch):
    """显式参数 > 环境变量 > .env（DESIGN.md 密钥不入库）。"""
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    from core.llm.ark import resolve_api_key
    # 1) .env 兜底
    (tmp_path / ".env").write_text("ARK_API_KEY=ark-from-dotenv\n", encoding="utf-8")
    assert resolve_api_key() == "ark-from-dotenv"
    # 2) 环境变量优先于 .env
    monkeypatch.setenv("ARK_API_KEY", "ark-from-env")
    assert resolve_api_key() == "ark-from-env"
    # 3) 显式参数最高
    assert resolve_api_key("ark-explicit") == "ark-explicit"
    # 4) 全无 → 报错
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    (tmp_path / ".env").unlink()
    with pytest.raises(ValueError, match="ARK_API_KEY"):
        resolve_api_key()


def test_ark_provider_defaults():
    captured = []
    transport = _fake_transport(_anthropic_response([{"type": "text", "text": "ok"}]), capture=captured)
    p = ArkCodingProvider(api_key="ark-x", transport=transport, model="deepseek-v4-flash")
    p.chat([{"role": "user", "content": "hi"}])
    assert captured[0]["url"].startswith("https://ark.cn-beijing.volces.com/api/coding")
    assert captured[0]["body"]["model"] == "deepseek-v4-flash"
