"""异常订阅唤醒测试（对话化编排器 M4，§4.6）：collect 白名单扫描（锚点/冷却/
首启回看窗/多类聚合）、wake_brief_text 合成文案、chat_turn wake 落盘
（proactive+triggers，唤醒消息不落历史）、API 冒烟（orch_wake_check →
Job orchestrator-wake → proactive orch.chat → 锚点去重）。

ScriptedLLM 与 test_orchestrator.py 同款剧本回放（复制以保持测试文件独立可运行）。
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.blackboard import Blackboard
from core.llm.anthropic_compat import AnthropicCompatProvider
from core.orchestrator import Orchestrator, OrchestratorConfig


class ScriptedLLM(AnthropicCompatProvider):
    def __init__(self, script: list[dict]):
        super().__init__("https://fake", "key", "scripted", transport=lambda *a: (200, {}))
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, messages, *, system=None, tools=None, max_tokens=4096, temperature=None,
             on_thinking=None, on_text=None, should_cancel=None):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "system": system})
        item = self.script.pop(0)
        if "text" in item:
            return self._parse({"content": [{"type": "text", "text": item["text"]}],
                                "stop_reason": "end_turn",
                                "usage": {"input_tokens": 1, "output_tokens": 1}})
        return self._parse({"content": item["tool_use"], "stop_reason": "tool_use",
                            "usage": {"input_tokens": 1, "output_tokens": 1}})

    @staticmethod
    def tool_call(cid, name, args):
        return {"type": "tool_use", "id": cid, "name": name, "input": args}


@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "wake.db"))
    project = bb.create_project("唤醒测试", "ctf")
    yield bb, project
    bb.close()


def _age_events(bb, pid, kind, seconds):
    """append_event 无时间参数：测试造旧事件直接 UPDATE created_at（UTC ISO）。"""
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    with bb._tx():
        bb.conn.execute("UPDATE events SET created_at=? WHERE project_id=? AND kind=?",
                        (old, pid, kind))


def _wake_once(env, triggers):
    """跑一次唤醒轮（产生 proactive 锚点），返回编排器与简报文本。"""
    llm = ScriptedLLM([{"text": "已收到异常简报：团队执行收尾，建议复盘产出。"}])
    bb, project = env
    orch = Orchestrator(project_id=project["id"], bb=bb, llm=llm,
                        session_factory=None, config=OrchestratorConfig())
    brief = Orchestrator.wake_brief_text(triggers)
    orch.chat_turn(brief, wake=triggers)
    return orch, brief


def test_collect_lookback_window_no_anchor(env):
    """无锚点（首启）：只报 WAKE_LOOKBACK 窗内的白名单事件，不翻旧账。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "team.run.finished",
                    {"team_id": "team-old", "team_name": "旧队", "run_id": "r-old",
                     "status": "failed"}, author="sess-a")
    _age_events(bb, pid, "team.run.finished", seconds=3600)  # 超出 1800s 回看窗
    assert Orchestrator.collect_wake_triggers(bb, pid) == []
    bb.append_event(pid, "team.run.finished",
                    {"team_id": "team-new", "team_name": "新队", "run_id": "r-new",
                     "status": "completed"}, author="sess-a")
    trig = Orchestrator.collect_wake_triggers(bb, pid)
    assert [t["summary"] for t in trig] == ["团队「新队」执行收尾：completed"]
    assert trig[0]["kind"] == "team.run.finished"


def test_collect_team_run_finished_summary(env):
    """team.run.finished 摘要按 payload 契约合成：团队名（缺则回落 team_id）
    + 终态；编排器一眼看清是哪个团队、以何状态收尾。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "team.run.finished",
                    {"team_id": "team-abc", "team_name": "exploit 队",
                     "run_id": "r1", "status": "failed"}, author="sess-a")
    trig = Orchestrator.collect_wake_triggers(bb, pid)
    assert trig[0]["kind"] == "team.run.finished"
    assert trig[0]["summary"] == "团队「exploit 队」执行收尾：failed"
    # team_name 缺失 → 回落 team_id
    bb.append_event(pid, "team.run.finished",
                    {"team_id": "team-xyz", "run_id": "r2", "status": "completed"},
                    author="sess-a")
    trig2 = Orchestrator.collect_wake_triggers(bb, pid)
    assert [t["summary"] for t in trig2] == [
        "团队「exploit 队」执行收尾：failed",
        "团队「team-xyz」执行收尾：completed"]


def test_collect_anchor_id_and_cooldown(env):
    """有锚点：锚点前事件按 id 跳过；锚点后新事件在冷却窗内静默，过后可再报。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "team.run.finished",
                    {"team_id": "t1", "team_name": "一队", "run_id": "r1",
                     "status": "failed"}, author="sess-a")
    first = Orchestrator.collect_wake_triggers(bb, pid)
    assert len(first) == 1
    _wake_once(env, first)  # 产生 proactive 锚点（triggers=["team.run.finished"]）
    # 锚点前的旧事件：id <= anchor → 已被上次唤醒覆盖
    assert Orchestrator.collect_wake_triggers(bb, pid) == []
    # 锚点后新收尾：冷却窗（600s）内静默
    bb.append_event(pid, "team.run.finished",
                    {"team_id": "t2", "team_name": "二队", "run_id": "r2",
                     "status": "failed"}, author="sess-a")
    now = time.time() + 300
    assert Orchestrator.collect_wake_triggers(bb, pid, now=now) == []
    # 冷却窗过后：新事件可再触发
    now = time.time() + 700
    trig = Orchestrator.collect_wake_triggers(bb, pid, now=now)
    assert [t["summary"] for t in trig] == ["团队「二队」执行收尾：failed"]


def test_collect_multi_kind_and_nonwhitelist(env):
    """多类触发同批聚合；白名单外事件（team.created 等）永不唤醒；
    team.run.finished 摘团队名+终态，budget 摘 note，gate 摘 summary。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "team.run.finished",
                    {"team_id": "t1", "team_name": "exploit 队", "run_id": "r1",
                     "status": "failed"}, author="sess-a")
    bb.append_event(pid, "budget.soft_warning",
                    {"scope": "tokens", "used": 80, "budget": 100, "pct": 0.8,
                     "note": "Token 用量已达预算 80%"}, author="system")
    bb.append_event(pid, "phase.gate_open",
                    {"from": "recon", "to": "pentest", "summary": "门指标达成"},
                    author="orchestrator")
    bb.append_event(pid, "team.created", {"team_id": "t9", "name": "新队"},
                    author="human")  # 白名单外
    bb.append_event(pid, "llm.usage", {"total_tokens": 1}, author="system")  # 白名单外
    trig = Orchestrator.collect_wake_triggers(bb, pid)
    kinds = {t["kind"] for t in trig}
    assert kinds == {"team.run.finished", "budget.soft_warning", "phase.gate_open"}
    by_kind = {t["kind"]: t for t in trig}
    assert by_kind["team.run.finished"]["summary"] == "团队「exploit 队」执行收尾：failed"
    assert "80%" in by_kind["budget.soft_warning"]["summary"]
    assert "门指标达成" in by_kind["phase.gate_open"]["summary"]


def test_collect_finding_new_severity_gate(env):
    """发现回喂（M4）：finding.new 仅 high/critical 唤醒；info/low/medium 静默
    （低档发现不改变打法，只堆唤醒噪声）。摘要带级别+标题+资产。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "finding.new",
                    {"finding_id": "f1", "title": "SQLi 登录口", "severity": "critical",
                     "vuln_class": "sqli", "target_asset_id": "asset-1"}, author="sess-a")
    bb.append_event(pid, "finding.new",
                    {"finding_id": "f2", "title": "版本泄露", "severity": "info",
                     "vuln_class": "infoleak", "target_asset_id": "asset-1"}, author="sess-a")
    bb.append_event(pid, "finding.new",
                    {"finding_id": "f3", "title": "中危", "severity": "medium",
                     "vuln_class": "xss"}, author="sess-a")
    trig = Orchestrator.collect_wake_triggers(bb, pid)
    assert [t["kind"] for t in trig] == ["finding.new"]
    assert trig[0]["summary"] == "[CRITICAL] SQLi 登录口（资产 asset-1）"


def test_collect_finding_new_high_and_severity_forms(env):
    """high 档同样唤醒；severity 大小写/空格容错；缺 title 回落 vuln_class；
    payload 缺 severity 一律不唤醒（宁少勿扰）。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "finding.new",
                    {"finding_id": "f1", "severity": " High ", "vuln_class": "rce"},
                    author="sess-a")
    bb.append_event(pid, "finding.new",
                    {"finding_id": "f2", "title": "无级别发现"}, author="sess-a")
    trig = Orchestrator.collect_wake_triggers(bb, pid)
    assert len(trig) == 1
    assert trig[0]["summary"] == "[HIGH] rce"


def test_collect_finding_merged_not_waking(env):
    """finding.merged（合并入既有发现）不在白名单——只有全新落库的发现唤醒。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "finding.merged",
                    {"finding_id": "f1", "title": "重复上报", "severity": "critical"},
                    author="sess-a")
    assert Orchestrator.collect_wake_triggers(bb, pid) == []


def test_wake_brief_text_composition():
    """合成消息：固定引导语 + 按类分行，team.run.finished 带摘要。"""
    triggers = [
        {"kind": "team.run.finished", "event_id": 1,
         "summary": "团队「exploit 队」执行收尾：failed", "ts": "x"},
        {"kind": "budget.soft_warning", "event_id": 3, "summary": "", "ts": "x"},
        {"kind": "phase.gate_open", "event_id": 4, "summary": "", "ts": "x"},
    ]
    text = Orchestrator.wake_brief_text(triggers)
    assert text.startswith("〔主动唤醒〕")
    assert "向人类简报现状" in text
    assert "团队执行收尾：团队「exploit 队」执行收尾：failed" in text
    assert "预算软警" in text and "80%" in text
    assert "阶段出口门满足" in text


def test_wake_brief_text_finding_group():
    """finding.new 成组渲染：一条引导行（条数+复判打法指引）+ 逐条 · 摘要，
    不与逐行 kind 行混杂。"""
    triggers = [
        {"kind": "finding.new", "event_id": 7,
         "summary": "[CRITICAL] SQLi 登录口（资产 asset-1）", "ts": "x"},
        {"kind": "finding.new", "event_id": 8,
         "summary": "[HIGH] RCE 上传点", "ts": "x"},
    ]
    text = Orchestrator.wake_brief_text(triggers)
    assert text.startswith("〔主动唤醒〕")
    assert "高危发现落库（2 条）" in text and "派生新意图" in text
    assert "  · [CRITICAL] SQLi 登录口（资产 asset-1）" in text
    assert "  · [HIGH] RCE 上传点" in text
    assert "finding.new：" not in text  # 不落逐行兜底格式


def test_chat_turn_wake_payload_and_history(env):
    """wake 轮落盘：orch.chat 带 proactive+triggers，唤醒简报不落历史
    （只进 LLM messages）。简报不落历史 ⇒ 下轮 _chat_history 的「开头连续
    assistant 跳过」规则会把唤醒回复一并跳过，后续轮从新 user 消息开始
    （唤醒轮=自包含简报，简报语境随轮消散，属定稿口径）。"""
    bb, project = env
    pid = project["id"]
    triggers = [{"kind": "team.run.finished", "event_id": 1,
                 "summary": "团队「exploit 队」执行收尾：failed", "ts": "x"}]
    orch, brief = _wake_once(env, triggers)
    chats = [e for e in bb.recent_events(pid) if e["kind"] == "orch.chat"]
    assert len(chats) == 1  # 唤醒简报（user）不落 orch.chat，只有 orch 回复
    payload = chats[0]["payload"]
    assert payload["role"] == "orch" and payload["proactive"] is True
    assert payload["triggers"] == ["team.run.finished"]
    # 简报确实进了本轮 LLM messages（唤醒轮自身语境完整）
    assert orch.llm.calls[0]["messages"][-1] == {"role": "user", "content": brief}
    # 下轮普通对话：简报不在历史，首条=新 user 消息
    llm2 = ScriptedLLM([{"text": "好的。"}])
    orch2 = Orchestrator(project_id=pid, bb=bb, llm=llm2,
                         session_factory=None, config=OrchestratorConfig())
    orch2.chat_turn("收到，不用处理")
    assert llm2.calls[0]["messages"] == [{"role": "user", "content": "收到，不用处理"}]
    # 普通回复不带 proactive 标记
    chats2 = [e for e in bb.recent_events(pid) if e["kind"] == "orch.chat"]
    assert len(chats2) == 2 and "proactive" not in chats2[1]["payload"]


def test_api_orch_wake_smoke(tmp_path):
    """API 冒烟：orch_wake_check 直调口——落 team.run.finished 事件后提交唤醒 Job，
    完成后事件流有 proactive orch.chat；再次直调因锚点覆盖返 empty。"""
    from fastapi.testclient import TestClient

    from core.api.app import create_app
    planner = ScriptedLLM([{"text": "简报：检测到一次团队执行收尾（exploit 队 failed），"
                                    "建议复盘产出后决定是否新建团队。"}])
    app = create_app(workspace_root=str(tmp_path / "workspaces"),
                     tools_root=None, executor_llm=ScriptedLLM([]),
                     planner_llm=planner,
                     providers_config=str(tmp_path / "providers.json"))
    with TestClient(app) as c:
        pid = c.post("/api/projects", json={"name": "唤醒冒烟", "track": "pentest",
                                            "capabilities": ["web"]}).json()["id"]
        bb = c.app.state.projects[pid].bb
        assert c.app.state.orch_wake_check(pid) == "empty"  # 无白名单事件
        bb.append_event(pid, "team.run.finished",
                        {"team_id": "team-x", "team_name": "exploit 队",
                         "run_id": "run-x", "status": "failed"}, author="sess-a")
        assert c.app.state.orch_wake_check(pid) == "submitted"
        # 等 runner 完成（pending 摘除发生在 finally，晚于 chat_turn 落盘）
        for _ in range(200):
            if pid not in app.state.orch_wake_pending:
                break
            time.sleep(0.05)
        assert pid not in app.state.orch_wake_pending
        chats = [e for e in bb.recent_events(pid) if e["kind"] == "orch.chat"]
        assert len(chats) == 1
        assert chats[0]["payload"]["proactive"] is True
        assert chats[0]["payload"]["triggers"] == ["team.run.finished"]
        assert "团队执行收尾" in chats[0]["payload"]["text"]
        # 锚点已覆盖该收尾事件：不重复唤醒
        assert c.app.state.orch_wake_check(pid) == "empty"
