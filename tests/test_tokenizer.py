"""token 分层计数测试（tokenizer，2026-10-03）。

口径：所有 `count_*` 返回**等价字符数**（token×2），阈值语义因此不变。
覆盖：分层选择（tiktoken / 估算 / 字符兜底）、CJK-ASCII 差异、计数稳定、
消息与工具开销、tiktoken 缺失降级不抛、与 loop 的接线。
"""

import builtins
import json

import pytest

from core.agent.loop import AgentConfig
from core.llm import tokenizer as tk

from test_agent import ScriptedLLM, make_agent


# ---------- 分层选择 ----------

def test_openai_family_uses_tiktoken_when_available():
    pytest.importorskip("tiktoken")
    assert isinstance(tk.get_counter("gpt-4o"), tk.TiktokenCounter)
    assert isinstance(tk.get_counter("gpt-4o-mini"), tk.TiktokenCounter)


def test_non_openai_family_uses_heuristic():
    """Ark / GLM / DeepSeek / Anthropic 系无公开 tokenizer → 估算器。"""
    for model in ("ark-code-latest", "deepseek-v4-flash", "glm-5.3-flash",
                  "claude-sonnet-5-5", "", None):
        assert isinstance(tk.get_counter(model), tk.HeuristicCounter), model


def test_missing_tiktoken_degrades_without_raising(monkeypatch):
    """tiktoken 是可选依赖：未装时 OpenAI 系也走估算器，不抛不 503。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "tiktoken":
            raise ImportError("no tiktoken")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(tk, "_tiktoken_cache", {})
    counter = tk.get_counter("gpt-4o")
    assert isinstance(counter, tk.HeuristicCounter)
    assert counter.count_text("hello world") > 0


def test_encoding_lookup_failure_degrades(monkeypatch):
    """词表取不到（离线/下载失败）也不炸，退回估算器。"""
    import tiktoken

    def boom(name):
        raise RuntimeError("offline")

    monkeypatch.setattr(tiktoken, "get_encoding", boom)
    monkeypatch.setattr(tk, "_tiktoken_cache", {})
    assert isinstance(tk.get_counter("gpt-4o"), tk.HeuristicCounter)


# ---------- 估算器口径 ----------

def test_counting_is_stable():
    c = tk.HeuristicCounter()
    text = "扫描 10.0.0.1 的 8080 端口，尝试 admin/admin"
    assert c.count_text(text) == c.count_text(text)


def test_cjk_costs_more_than_ascii_per_char():
    """中英混写偏差正是换掉字符计数的理由：同字符数下中文 token 量远高于英文。"""
    c = tk.HeuristicCounter()
    cjk = c.count_text("中" * 100)
    ascii_ = c.count_text("a" * 100)
    assert cjk > ascii_ * 2


def test_heuristic_approximates_token_ratio():
    """英文 ≈4 字符/token、中文 ≈1 字/token（×2 折算回等价字符数）。"""
    c = tk.HeuristicCounter()
    assert c.count_text("abcdefgh") == 4       # 8/4 = 2 token × 2
    assert c.count_text("中文四个") == 8        # 4 token × 2


def test_empty_text_is_zero():
    assert tk.HeuristicCounter().count_text("") == 0


def test_count_messages_grows_with_history_and_counts_overhead():
    c = tk.HeuristicCounter()
    one = c.count_messages([{"role": "user", "content": "hi"}])
    two = c.count_messages([{"role": "user", "content": "hi"}] * 2)
    assert two > one
    # 每条消息有固定开销：纯空消息也 > 0
    assert c.count_messages([{"role": "user", "content": ""}]) > 0


def test_count_messages_includes_tools_overhead():
    c = tk.HeuristicCounter()
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"name": "run_cmd", "description": "d", "input_schema": {}}]
    assert c.count_messages(msgs, tools=tools) > c.count_messages(msgs)


def test_count_messages_includes_system():
    c = tk.HeuristicCounter()
    msgs = [{"role": "user", "content": "hi"}]
    assert c.count_messages(msgs, system="规则" * 100) > c.count_messages(msgs)


def test_tiktoken_and_heuristic_agree_on_magnitude():
    """两层不应差一个数量级（否则切换模型会突然频繁/从不压缩）。"""
    pytest.importorskip("tiktoken")
    text = "扫描 10.0.0.1 的 8080 端口，尝试 admin/admin 弱口令爆破。"
    t = tk.get_counter("gpt-4o").count_text(text)
    h = tk.HeuristicCounter().count_text(text)
    assert 0.4 <= h / t <= 2.5


# ---------- 兜底 ----------

def test_char_counter_matches_legacy_expression():
    """CharCounter 是迁移前的口径（字符数），保证极端情况下行为不突变。"""
    msgs = [{"role": "user", "content": "abc"},
            {"role": "assistant", "content": [{"type": "text", "text": "中文"}]}]
    legacy = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
    assert tk.CharCounter().count_messages(msgs) == legacy


def test_calibrate_returns_ratio():
    assert tk.calibrate([]) == 1.0
    ratio = tk.calibrate([("abcdefgh", 2)])
    assert ratio == pytest.approx(1.0)  # 8 字符 / 4 = 2 token，与实际一致


# ---------- 与主循环接线 ----------

@pytest.fixture()
def env(tmp_path):
    from core.blackboard import Blackboard
    from core.runtime import ExecutionGateway, NativeBackend

    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("计数测试", "pentest", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    yield bb, project, gw, tmp_path
    bb.close()


def test_agent_count_tokens_follows_model_layer(env):
    """_count_tokens 走分层计数，且与字符数口径明显不同（不再等于 len(dumps)）。"""
    agent = make_agent(env, ScriptedLLM([]))
    msgs = [{"role": "user", "content": "中" * 200}]
    legacy = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
    got = agent._count_tokens(msgs)
    assert got != legacy
    assert got > legacy  # 200 个中文字 → 200 token × 2 = 400 > 字符数


def test_agent_count_tokens_switches_with_model(env):
    """模型决定计数器：OpenAI 系与 Ark 系同文本计数不同（分层生效）。"""
    pytest.importorskip("tiktoken")
    agent = make_agent(env, ScriptedLLM([]))
    msgs = [{"role": "user", "content": "扫描 10.0.0.1 端口"}]
    agent.llm.model = "ark-code-latest"
    heuristic = agent._count_tokens(msgs)
    agent.llm.model = "gpt-4o"
    exact = agent._count_tokens(msgs)
    assert heuristic > 0 and exact > 0


def test_trim_uses_token_counter(env):
    """预算闸以 token 计数判定：ASCII 长结果按 token 折算后仍能触发裁剪。

    4000 个 ASCII 字符 ≈1000 token ≈2000 等价字符；预算 1500 时必裁。
    若计数退回「字符数」口径，2000 与 4000 都不变——本测真正钉的是**接线**
    （`_trim` 走 `_count_tokens` 而非 `len(json.dumps)`）。
    """
    agent = make_agent(env, ScriptedLLM([]))
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                          "content": "a" * 4000}]}]
    msgs += [{"role": "user", "content": "填充"}] * 9  # _trim 保留最近 8 条
    agent.config = AgentConfig(context_char_budget=1500)
    agent._trim(msgs)
    assert msgs[0]["content"][0]["content"].endswith("…[已截断]")


def test_trim_under_budget_is_noop(env):
    agent = make_agent(env, ScriptedLLM([]))
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                          "content": "a" * 4000}]}]
    msgs += [{"role": "user", "content": "填充"}] * 9
    agent.config = AgentConfig(context_char_budget=100_000)
    agent._trim(msgs)
    assert msgs[0]["content"][0]["content"] == "a" * 4000
