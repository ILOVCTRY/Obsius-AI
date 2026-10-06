"""Agent 主循环测试：脚本化 LLM 驱动端到端（不触网）。

任务机制退役后（2026-10-06）：执行驱动 = Team 成员执行语义
（`run_objective` → `AgentSession.run_team_execution`），不再有任务/队列/
认领/心跳/租约/计划闸/子任务发布等专测——这些机制已随旧任务系统删除。

覆盖：工具分发全链路与审计、网关拒绝改道、系统提示组装（红线/角色/能力清单）、
策略顾问卡死干预（建议/裁决/延长）、意图先行闸与意图纪律、上下文裁剪与摘要压缩、
思考流式、截断重试、会话控制（暂停/中断）、kb/技能/路由、spill、对话轮 run_chat、
read_file/search_files、蓝图、system 分块等通用机制。
"""

import json
import shutil
import threading
import time

import pytest

from core.agent import AgentConfig, AgentSession
from core.agent.execution import ExecutionContext
from core.agent.loop import CHAT_TOOLS, sanitize_snapshot_tail, session_chat_path
from core.blackboard import Blackboard
from core.blackboard.intents import declare_intent
from core.llm import LLMError
from core.llm.anthropic_compat import AnthropicCompatProvider
from core.runtime import ExecutionGateway, NativeBackend


# ---------- 测试基建 ----------

class ScriptedLLM(AnthropicCompatProvider):
    """按剧本回放 LLM 响应；记录每次收到的消息供断言。"""

    def __init__(self, script: list[dict]):
        super().__init__("https://fake", "key", "scripted", transport=lambda *a: (200, {}))
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, messages, *, system=None, tools=None, max_tokens=16384, temperature=None,
             on_thinking=None, on_text=None, should_cancel=None):
        # M1 prompt caching：system 可能是块数组（stable/dynamic 两块）——
        # calls 记录拼接字符串（旧断言兼容），blocks 原样另存供新断言
        sys_text = system
        if isinstance(system, list):
            sys_text = "\n\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in system)
        self.calls.append({"messages": json.loads(json.dumps(messages)), "system": sys_text,
                           "system_blocks": system,
                           "stream": on_thinking is not None,
                           "stream_text": on_text is not None})
        item = self.script.pop(0)
        if isinstance(item, Exception):  # 剧本可放异常实例（截断重试测试用）
            raise item
        # 思考/回复流式回放：deltas 逐段回调，每段后停一拍让主线程轮询把增量落成事件
        if item.get("deltas") and on_thinking is not None:
            for d in item["deltas"]:
                on_thinking(d)
                time.sleep(0.5)
            if should_cancel is not None and should_cancel():
                from core.llm import LLMError
                raise LLMError("已中断")
        if item.get("text_deltas") and on_text is not None:
            for d in item["text_deltas"]:
                on_text(d)
                time.sleep(0.5)
        content: list[dict] = []
        if "thinking" in item:
            content.append({"type": "thinking", "thinking": item["thinking"]})
        if "text" in item:
            content.append({"type": "text", "text": item["text"]})
        if "tool_use" in item:
            content.extend(item["tool_use"])
        stop = "tool_use" if "tool_use" in item else "end_turn"
        return self._parse({"content": content, "stop_reason": stop,
                            "usage": {"input_tokens": 1, "output_tokens": 1}})

    @staticmethod
    def tool_call(cid, name, args):
        return {"type": "tool_use", "id": cid, "name": name, "input": args}


class FakeDockerBackend:
    name = "sandbox"

    def run_once(self, image, cmd, *, net, sandbox, timeout):
        assert sandbox and net == "none"  # 恶意样本必须进 L3 且默认断网
        return type("O", (), {"exit_code": 0, "stdout": f"sandbox[{image}]: {cmd}",
                              "stderr": "", "timed_out": False, "meta": {}})()


@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("测试项目", "pentest", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend(), "sandbox": FakeDockerBackend()})
    yield bb, project, gw, tmp_path
    bb.close()


def run_objective(agent, objective, **kw):
    """任务机制退役后的执行驱动（替代旧 run_task）：以 Team 成员执行语义跑一个目标。"""
    return agent.run_team_execution(
        ExecutionContext(execution_id="exec-test", objective=objective, **kw))


def make_agent(env, llm, planner=None, config=None, role="_generalist", artifacts_dir=None,
               capabilities=("web",), enable_sediment=False):
    bb, project, gw, tmp_path = env
    packs = tmp_path / "packs"
    # 轨级演示技能（pack_set = capabilities ∪ {track} 内可见）
    (packs / "tracks" / "pentest" / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (packs / "tracks" / "pentest" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示技能\nkeywords: 测试\n---\n按步骤执行。",
        encoding="utf-8")
    experts = packs / "experts"
    experts.mkdir(parents=True, exist_ok=True)
    (experts / "_generalist.yaml").write_text(
        'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
    agent = AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                        planner_llm=planner, packs_root=packs, track="pentest",
                        capabilities=list(capabilities),
                        role=role, capability_prompt="## 能力清单\n- Docker: 可用",
                        config=config or AgentConfig(max_steps=10),
                        artifacts_dir=artifacts_dir,
                        enable_sediment=enable_sediment)
    if planner is None:
        # 对话即指令（2026-09-28）回归配套：未显式传 planner 时短路对话意图
        # 分类——分类器回落主 LLM 会多消费一条剧本，旧对话测试整体错位。
        # 要测分类器/对话转任务：显式传 planner=ScriptedLLM([...])。
        agent._classify_human_intent = lambda note_text: ("chat", "")
    return agent


def pass_intent_lead(*dispatchers) -> None:
    """意图先行闸测试旁路（口径 Y，2026-10-01）：把「会话首次实质动作已放行」标志
    钉死为 True，并包一层 dispatch——loop 每任务起点会复位该标志（每任务都要「先
    立意再动手」），故每次派发前重设，保证本用例聚焦其自身主题（网关/卡死/产物/
    运行时/上下文…）而不被意图纪律挡回。闸本身的行为由 test_intent_lead_gate_*
    专测覆盖（含无意图被拒/只读不受阻/协调原语放行/declare 后放行）。"""
    for d in dispatchers:
        d._intent_lead_passed = True
        if getattr(d, "_intent_lead_bypass_wrapped", False):
            continue
        original = d.dispatch

        def _dispatch(name, args, _original=original, _d=d):
            _d._intent_lead_passed = True
            return _original(name, args)

        d.dispatch = _dispatch
        d._intent_lead_bypass_wrapped = True


def write_role(env, name, body):
    """测试夹具里写一个专家 yaml（expert-pool M2：运行时角色源=experts/）。"""
    _, _, _, tmp_path = env
    f = tmp_path / "packs" / "experts" / f"{name}.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


# ---------- 端到端：run_cmd → 发现 → finish ----------

def test_full_loop_run_cmd_finding_finish(env, monkeypatch):
    bb, project, gw, _ = env
    # x.com 是真实域名：断网 hermetic（有 DNS 环境会自动挂载 host 资产打乱断言）
    from core.blackboard import assets as am

    def _no_dns(*a, **k):
        raise OSError("dns off")
    monkeypatch.setattr(am.socket, "getaddrinfo", _no_dns)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_add_asset",
                                            {"type": "domain", "value": "x.com"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "bb_add_finding",
                                            {"vuln_class": "info-leak", "title": "备份泄露",
                                             "severity": "low"})]},
        # 意图纪律：finish 撞未收尾意图首次被拦，二次 finish 放行（ack 机制）
        {"tool_use": [ScriptedLLM.tool_call("t4", "finish", {"summary": "干完了"})]},
        {"tool_use": [ScriptedLLM.tool_call("t5", "finish", {"summary": "干完了"})]},
    ])
    agent = make_agent(env, llm)
    declare_intent(bb, project["id"], "对 x.com 进行备份文件探测尝试",
                   author=agent.session["id"])  # 过 bb_add_finding 必挂意图门禁
    summary = run_objective(agent, "测 x.com")
    assert summary == "干完了"
    assert len(bb.list_findings(project["id"])) == 1
    assert bb.list_assets(project["id"])[0]["value"] == "x.com"
    # 审计链：command 与 command.result 都落了事件（team execution 不走 _finalize，
    # 无 session.finished——会话收尾由 worker 层管理）
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "command" in kinds and "command.result" in kinds
    # 系统提示包含红线与能力清单
    sys_prompt = llm.calls[0]["system"]
    assert "红线" in sys_prompt and "能力清单" in sys_prompt and "当前任务" in sys_prompt


def test_gateway_deny_feeds_back_not_crashes(env):
    """恶意样本跑 host → 网关拒绝 → 拒绝文本回填 → Agent 改道 sandbox → 成功。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "./sample", "runtime": "host",
                                             "threat_class": "malware_live"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "run_cmd",
                                            {"cmd": "id", "runtime": "sandbox",
                                             "threat_class": "malware_live"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "改道成功"})]},
    ])
    agent = make_agent(env, llm)
    pass_intent_lead(agent.dispatcher)  # 本测主题=网关拒绝改道，跳过意图先行闸
    summary = run_objective(agent, "分析样本")
    assert summary == "改道成功"
    # 拒绝落了 audit.deny；拒绝文本确实回给了 LLM
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "audit.deny" in kinds
    tool_msgs = [m for m in llm.calls[1]["messages"]
                 if isinstance(m.get("content"), list)
                 and any(b.get("type") == "tool_result" for b in m["content"])]
    assert any("[网关拒绝]" in b["content"]
               for m in tool_msgs for b in m["content"] if b.get("type") == "tool_result")


def test_advisor_invoked_when_stuck(env):
    bb, project, gw, _ = env
    planner = ScriptedLLM([
        {"text": "建议：先查黑板去重，再试另一个攻击面。"},
    ])
    # 剧本：连续只读空转（bb_query 不推进进展、非纯文本不终止）直到顾问触发，然后 finish
    def idle(cid):
        return {"tool_use": [ScriptedLLM.tool_call(cid, "bb_query", {"what": "events"})]}
    script = [idle(f"q{i}") for i in range(3)]
    script.append({"tool_use": [ScriptedLLM.tool_call("t9", "finish", {"summary": "完成"})]})
    llm = ScriptedLLM(script)
    cfg = AgentConfig(max_steps=15, stuck_after=3)
    agent = make_agent(env, llm, planner=planner, config=cfg)
    run_objective(agent, "空转测试")
    # 顾问被调用过，且其建议以 [策略顾问] 注入了主循环消息
    assert len(planner.script) == 0
    injected = any("[策略顾问]" in json.dumps(c["messages"], ensure_ascii=False)
                   for c in llm.calls)
    assert injected


def test_advisor_intervention_event_and_session_scope(env):
    """顾问可观测（2026-09-20）：①发言落 `advisor.intervention` 事件（此前只进
    messages，直播间只见 token 行不知顾问说了什么）；②顾问视野收窄到本会话事件
    （此前全项目 limit=30，多窗时顾问满眼别人的上下文，执行者正确地当噪声拒掉）。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([{"text": "建议：换一个未尝试的攻击面。"}])
    llm = ScriptedLLM([])
    agent = make_agent(env, llm, planner=planner, config=AgentConfig(max_steps=5))
    pid = project["id"]
    # 本会话与「别人会话」各留一条带标记的事件
    mine, other = agent.session["id"], "sess-someone-else"
    bb.append_event(pid, "tool.call", {"name": "run_cmd", "cmd": "MINE_MARKER"},
                    session_id=mine)
    bb.append_event(pid, "tool.call", {"name": "run_cmd", "cmd": "OTHER_MARKER"},
                    session_id=other)

    out = agent._advisor_prompt([], "目标")
    assert out.startswith("[策略顾问]") and "换一个未尝试的攻击面" in out
    # ① 事件落流：正文可见（直播间不再只有 token 行）
    ev = [e for e in bb.recent_events(pid) if e["kind"] == "advisor.intervention"]
    assert len(ev) == 1 and ev[-1]["payload"]["text"] == "建议：换一个未尝试的攻击面。"
    assert ev[-1]["session_id"] == mine
    # ② 视野收窄：digest 只含本会话事件
    seen = json.dumps(planner.calls[-1]["messages"], ensure_ascii=False)
    assert "MINE_MARKER" in seen and "OTHER_MARKER" not in seen


# ---------- stuck-convergence D1/D2：卡死波次升级 + 重复命令注入（2026-09-24 M1） ----------

def test_d9_command_evolution_extends_without_advisor(env):
    """D9：窗内每步跑不同命令（命令演进信号）→ 到期静默延长、顾问零调用，
    agent.stuck_extend 事件留痕（stuck_after=3：第 3/6 步各延长一次）。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([])  # 空剧本：延长命中时一次都不该被调用
    script = [
        {"tool_use": [ScriptedLLM.tool_call(
            f"c{i}", "run_cmd",
            {"cmd": f"echo probe{i}", "runtime": "host", "threat_class": "trusted"})]}
        for i in range(1, 7)]
    script.append({"tool_use": [ScriptedLLM.tool_call(
        "cF", "finish", {"summary": "长任务完成"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=10, stuck_after=3))
    pass_intent_lead(agent.dispatcher)  # 本测主题=卡死检测，跳过意图先行闸
    assert run_objective(agent, "逆向中") == "长任务完成"
    assert len(planner.calls) == 0
    extends = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "agent.stuck_extend"]
    assert [e["payload"]["extension"] for e in extends] == [1, 2]
    assert [e["payload"]["step"] for e in extends] == [3, 6]
    # 预检在步开始执行：窗内只有前两步的命令（第 3 步尚未跑）
    assert extends[0]["payload"]["signals"]["unique_commands"] == 2


def test_d9_new_file_extends_without_advisor(env):
    """D9：连续读取本会话此前未读过的新文件（新文件信号）→ 静默延长，
    顾问零调用；file.read 路径相对 scratch 记录。"""
    bb, project, gw, tmp_path = env
    artifacts = tmp_path / "ws" / "artifacts"
    scratch = tmp_path / "ws" / "scratch"
    scratch.mkdir(parents=True)
    for i in range(1, 7):
        (scratch / f"target{i}.js").write_text(f"// file {i}\n", encoding="utf-8")
    planner = ScriptedLLM([])
    script = [
        {"tool_use": [ScriptedLLM.tool_call(
            f"r{i}", "read_file", {"path": f"target{i}.js"})]}
        for i in range(1, 7)]
    script.append({"tool_use": [ScriptedLLM.tool_call(
        "rF", "finish", {"summary": "读完了"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=10, stuck_after=3),
                       artifacts_dir=artifacts)
    assert run_objective(agent, "读代码中") == "读完了"
    assert len(planner.calls) == 0
    extends = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "agent.stuck_extend"]
    assert [e["payload"]["extension"] for e in extends] == [1, 2]
    assert extends[0]["payload"]["signals"]["new_files"][0] == "target1.js"


def test_d9_repeated_commands_still_invoke_advisor(env):
    """D9：窗内全是同一条命令（unique=1）不满足演进 → 预检不延长、照常叫顾问
    （真卡死必须在第一个观察窗被抓到，不能被预检放过）。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([{"text": "建议：换个攻击面。"}])
    script = [
        {"tool_use": [ScriptedLLM.tool_call(
            f"s{i}", "run_cmd",
            {"cmd": "echo same-command", "runtime": "host", "threat_class": "trusted"})]}
        for i in range(1, 4)]
    script.append({"tool_use": [ScriptedLLM.tool_call(
        "sF", "finish", {"summary": "收尾"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=8, stuck_after=3))
    run_objective(agent, "打转中")
    assert len(planner.calls) == 1
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.stuck_extend"]
    interventions = [e for e in bb.recent_events(project["id"])
                     if e["kind"] == "advisor.intervention"]
    assert len(interventions) == 1


def test_d9_extension_cap_then_advisor_chain(env):
    """D9：2 次静默延长用满后第 3 个窗不再延长——走顾问建议链（延长只推后
    不拆除断路器：顾问 ≤2 次、硬闸兜底）。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([{"text": "建议：按已知算法本地复现签名。"}])
    script = [
        {"tool_use": [ScriptedLLM.tool_call(
            f"c{i}", "run_cmd",
            {"cmd": f"echo probe{i}", "runtime": "host", "threat_class": "trusted"})]}
        for i in range(1, 10)]
    script.append({"tool_use": [ScriptedLLM.tool_call(
        "cF", "finish", {"summary": "收尾"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3))
    pass_intent_lead(agent.dispatcher)  # 本测主题=活跃探索延长，跳过意图先行闸
    run_objective(agent, "长程逆向")
    extends = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "agent.stuck_extend"]
    assert [e["payload"]["step"] for e in extends] == [3, 6]
    assert len(planner.calls) == 1  # 第 9 步延长上限用满，才叫顾问
    assert len([e for e in bb.recent_events(project["id"])
                if e["kind"] == "advisor.intervention"]) == 1


def test_d7_escalate_event_carries_command_stats(env):
    """D7+D2：裁决 terminate 后 stuck_escalate 事件 payload 带重复命令排行与
    最近命令摘要；且裁决调用的 prompt 含上次建议原文与建议后的实际命令——
    顾问凭「上次建议 + 后续事实」裁决，而非凭空二选一。"""
    bb, project, gw, tmp_path = env
    verdict_json = json.dumps(
        {"decision": "terminate", "reason": "路径已试尽", "instruction": ""})
    planner = ScriptedLLM([
        {"text": "建议：不要重复跑同一条命令。"},
        {"text": verdict_json},
    ])

    def cmd_item(cid, cmd):
        return {"tool_use": [ScriptedLLM.tool_call(
            cid, "run_cmd", {"cmd": cmd, "runtime": "host",
                              "threat_class": "trusted"})]}

    def idle(cid):
        return {"tool_use": [ScriptedLLM.tool_call(cid, "bb_query",
                                                    {"what": "events"})]}

    llm = ScriptedLLM([
        cmd_item("r1", "dup-cmd"), cmd_item("r2", "dup-cmd"),
        # 第 3 步先撞卡死召唤顾问（lps 被重置到 3），随后执行 uniq-cmd
        cmd_item("r3", "uniq-cmd"),
        idle("q4"), idle("q5"),
        # 第 6 步第 2 轮卡死 → 升级（C1 分支收尾）
        idle("q6"),
    ])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    pass_intent_lead(agent.dispatcher)  # 本测主题=顾问裁决统计，跳过意图先行闸
    run_objective(agent, "命令重复")
    # 裁决 prompt：含上次建议原文 + 建议后实际执行的命令
    verdict_seen = json.dumps(planner.calls[1]["messages"], ensure_ascii=False)
    assert "不要重复跑同一条命令" in verdict_seen
    assert "dup-cmd" in verdict_seen and "uniq-cmd" in verdict_seen
    esc = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "agent.stuck_escalate"][-1]
    p = esc["payload"]
    assert p["step"] == 6 and p["last_progress_step"] == 3
    assert p["trigger"] == "advisor_terminate"
    assert p["repeated_commands"] == [{"cmd": "dup-cmd", "times": 2}]
    assert p["recent_commands"] == ["dup-cmd", "dup-cmd", "uniq-cmd"]


def test_d2_repeat_stats_and_advisor_prompt_injection(env):
    """D2：_command_repeat_stats 机械聚合完全相同命令 ×N（×≥2、按次数降序）；
    排行注入顾问 prompt，收敛性判断交 LLM。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([{"text": "建议：避开重复路径。"}])
    agent = make_agent(env, ScriptedLLM([]), planner=planner)
    sid = agent.session["id"]
    for cmd, n in (("nmap -sS 10.0.0.1", 3), ("curl http://x", 1)):
        for _ in range(n):
            bb.append_event(project["id"], "command", {"cmd": cmd},
                            session_id=sid, author=sid)
    assert agent._command_repeat_stats() == [
        {"cmd": "nmap -sS 10.0.0.1", "times": 3}]
    out = agent._advisor_prompt([], "目标")
    assert out.startswith("[策略顾问]")
    seen = json.dumps(planner.calls[-1]["messages"], ensure_ascii=False)
    assert "最近重复命令" in seen and "×3 nmap -sS 10.0.0.1" in seen
    # 只出现一次的不进排行段（×1 行不存在；curl 在通用事件摘要里出现是正常的）
    assert "×1 " not in seen


# ---------- stuck-convergence D7：顾问裁决 + 硬闸兜底（2026-09-24） ----------

def test_d7_verdict_continue_then_hard_backstop(env):
    """D7：裁决 continue → 半强制新指令注入、重开 12 步窗（waves=2）；
    该窗再卡满 → 机械硬闸终止（trigger=hard_backstop，waves=3），不再问
    第三次顾问。全程有界：planner 仅 2 次调用（建议 + 裁决）。"""
    bb, project, gw, tmp_path = env
    continue_json = json.dumps(
        {"decision": "continue", "reason": "执行者在深挖 JS 未收口",
         "instruction": "停止 grep，立即把探针结论登记入黑板"})
    planner = ScriptedLLM([
        {"text": "建议：先收口已有探针。"},
        {"text": continue_json},
    ])
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call(
        f"q{i}", "bb_query", {"what": "events"})]} for i in range(9)])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    assert run_objective(agent, "继续硬闸") is None
    assert len(planner.calls) == 2  # 第 3 轮硬闸不再调顾问
    # 半强制裁决指令注入主循环
    injected = json.dumps(llm.calls, ensure_ascii=False)
    assert "[策略顾问·裁决：继续]" in injected
    assert "立即把探针结论登记入黑板" in injected
    verdict_ev = [e for e in bb.recent_events(project["id"])
                  if e["kind"] == "advisor.verdict"][-1]
    assert verdict_ev["payload"]["decision"] == "continue"
    esc = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "agent.stuck_escalate"][-1]
    p = esc["payload"]
    assert (p["waves"] == 3 and p["step"] == 9
            and p["last_progress_step"] == 6
            and p["trigger"] == "hard_backstop")


def test_d7_verdict_human_request(env):
    """D7：裁决 human（信息不足）→ 直接 awaiting_human 挂起，不产出
    stuck_escalate（非升级终止，是顾问主动请人），verdict 事件留痕。"""
    bb, project, gw, tmp_path = env
    human_json = json.dumps(
        {"decision": "human", "reason": "缺关键探针结果无法判断", "instruction": ""})
    planner = ScriptedLLM([
        {"text": "建议：补一个探针。"},
        {"text": human_json},
    ])
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call(
        f"q{i}", "bb_query", {"what": "events"})]} for i in range(6)])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    run_objective(agent, "请求人工")
    assert len(planner.calls) == 2
    verdict_ev = [e for e in bb.recent_events(project["id"])
                  if e["kind"] == "advisor.verdict"][-1]
    assert verdict_ev["payload"]["decision"] == "human"
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.stuck_escalate"]


def test_d7_verdict_llm_failure_falls_back_terminate(env):
    """D7：裁决 LLM 调用抛异常 → 回落 terminate（宁严勿松，绝不回落继续）：
    stuck_escalate trigger=advisor_terminate，理由注明调用失败。"""
    bb, project, gw, tmp_path = env
    planner = ScriptedLLM([
        {"text": "建议：换思路。"},
        LLMError("planner down"),
    ])
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call(
        f"q{i}", "bb_query", {"what": "events"})]} for i in range(6)])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    run_objective(agent, "顾问异常")
    assert len(planner.calls) == 2
    esc = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "agent.stuck_escalate"][-1]
    p = esc["payload"]
    assert p["trigger"] == "advisor_terminate" and p["waves"] == 2
    assert "调用失败" in p["verdict_reason"]


def test_d7_parse_verdict(env):
    """D7：裁决 JSON 解析——合法三选项照收；任何不合规（非 JSON / 非法
    decision / continue 缺 instruction）一律回落 terminate。"""
    agent = make_agent(env, ScriptedLLM([]), planner=ScriptedLLM([]))
    assert agent._parse_verdict("随手写的自然语言")[0] == "terminate"
    assert agent._parse_verdict(
        json.dumps({"decision": "stop"}))[0] == "terminate"
    assert agent._parse_verdict(
        json.dumps({"decision": "continue", "reason": "再试"}))[0] == "terminate"
    d, reason, instruction = agent._parse_verdict(json.dumps(
        {"decision": "continue", "reason": "建议被无视",
         "instruction": "改做 Y"}))
    assert d == "continue" and reason == "建议被无视" and instruction == "改做 Y"
    d, _, _ = agent._parse_verdict(json.dumps(
        {"decision": "HUMAN", "reason": "缺信息"}))
    assert d == "human"


# ---------- stuck-convergence D6：收尾确认轮（2026-09-24 M1） ----------

def test_d1_command_windows_read_latest_not_earliest(env):
    """事件窗方向修复（task-97e1d5d6c597 真实事故复盘）：recent_events(limit=N)
    since_id=0 取的是**最早** N 条——长会话升级时 recent_commands / 重复排行 /
    顾问 digest 全看到会话开头（事故里人工看到的是开场 spill grep，不是卡点处的
    JS 分析）。统一改 tail=N 取最新窗（tail 升序返回，下游 [-cap:] 口径不变）。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([{"text": "建议：换思路。"}])
    agent = make_agent(env, ScriptedLLM([]), planner=planner)
    sid = agent.session["id"]
    for i in range(105):  # 远超 30/50 事件窗
        bb.append_event(project["id"], "command", {"cmd": f"old-cmd-{i}"},
                        session_id=sid, author=sid)
    for cmd in ("recent-dup", "recent-dup", "recent-tail"):
        bb.append_event(project["id"], "command", {"cmd": cmd},
                        session_id=sid, author=sid)
    # 最近命令摘要=真正最新 5 条（修复前给的是 old-cmd-0 起首五条）
    assert agent._recent_command_summary() == [
        "old-cmd-103", "old-cmd-104", "recent-dup", "recent-dup", "recent-tail"]
    # 重复排行只数最新 100 事件窗
    assert agent._command_repeat_stats() == [
        {"cmd": "recent-dup", "times": 2}]
    # 顾问视野同样落在最新窗（看不到开场命令）
    agent._advisor_prompt([], "目标")
    seen = json.dumps(planner.calls[-1]["messages"], ensure_ascii=False)
    assert "recent-tail" in seen and "old-cmd-0" not in seen


def test_d10_stuck_max_extensions_zero_still_invokes_advisor(env):
    """D10：stuck_max_extensions=0 → 演进命令不再静默延长，第一个观察窗照常
    召唤顾问（关闭延长不拆断路器）。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([{"text": "建议：换攻击面。"}])
    script = [
        {"tool_use": [ScriptedLLM.tool_call(
            f"c{i}", "run_cmd",
            {"cmd": f"echo probe{i}", "runtime": "host", "threat_class": "trusted"})]}
        for i in range(1, 4)]
    script.append({"tool_use": [ScriptedLLM.tool_call(
        "cF", "finish", {"summary": "收尾"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=8, stuck_after=3,
                                          stuck_max_extensions=0))
    run_objective(agent, "打转中")
    assert len(planner.calls) == 1
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.stuck_extend"]
    assert len([e for e in bb.recent_events(project["id"])
                if e["kind"] == "advisor.intervention"]) == 1


def test_d10_stuck_max_extensions_one_then_advisor(env):
    """D10：上限=1 → 第 3 步延长 1 次，第 6 步延长用满走顾问链。"""
    bb, project, gw, _ = env
    planner = ScriptedLLM([{"text": "建议：本地复现签名。"}])
    script = [
        {"tool_use": [ScriptedLLM.tool_call(
            f"c{i}", "run_cmd",
            {"cmd": f"echo probe{i}", "runtime": "host", "threat_class": "trusted"})]}
        for i in range(1, 7)]
    script.append({"tool_use": [ScriptedLLM.tool_call(
        "cF", "finish", {"summary": "收尾"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=10, stuck_after=3,
                                          stuck_max_extensions=1))
    pass_intent_lead(agent.dispatcher)  # 本测主题=延长上限，跳过意图先行闸
    run_objective(agent, "长任务")
    extends = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "agent.stuck_extend"]
    assert [(e["payload"]["step"], e["payload"]["extension"]) for e in extends] == [(3, 1)]
    assert len(planner.calls) == 1
    assert len([e for e in bb.recent_events(project["id"])
                if e["kind"] == "advisor.intervention"]) == 1


def test_context_trimming(env):
    bb, project, gw, _ = env

    class BigHostBackend:
        name = "host"

        def execute(self, cmd, timeout=120.0, cwd=None, env=None, abort_event=None):
            return type("O", (), {"exit_code": 0, "stdout": "A" * 5000,
                                  "stderr": "", "timed_out": False,
                                  "interrupted": False, "meta": {}})()

    gw.backends["host"] = BigHostBackend()
    # 剧本给足余量（run×40 + finish）：G3 摘要调用也按序消费剧本（test_context_
    # summarization 的既定契约），触发次数随结果长度/阈值动态浮动；循环由
    # max_steps 收尾。
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(f"t{i}", "run_cmd",
                                            {"cmd": f"cmd{i}", "runtime": "host",
                                             "threat_class": "trusted"})]}
        for i in range(40)
    ] + [{"tool_use": [ScriptedLLM.tool_call("tz", "finish", {"summary": "ok"})]}])
    cfg = AgentConfig(max_steps=20, context_char_budget=10_000)
    agent = make_agent(env, llm, config=cfg)
    pass_intent_lead(agent.dispatcher)  # 本测主题=上下文裁剪，跳过意图先行闸
    run_objective(agent, "裁剪测试")
    # 网关 brief() 已截到 8000 字符/条（2026-10-01 由 2000 放宽），总历史远超
    # 10k 预算 → _trim 必然把某次调用里的旧结果替换成占位（后续若被 G3 摘要整体
    # 替换也属正常压缩路径）
    truncated = [c for c in llm.calls
                 if "[已截断]" in json.dumps(c["messages"], ensure_ascii=False)]
    assert truncated


def test_context_summarization(env):
    """G3 结构化摘要压缩：过 context_summary_chars 阈值 → 旧历史经 LLM 压成
    一段摘要替换（近 8 条逐字保留），llm.compact 事件落流；机械 _trim 不触发。"""
    bb, project, gw, _ = env

    class BigHostBackend:
        name = "host"

        def execute(self, cmd, timeout=120.0, cwd=None, env=None, abort_event=None):
            return type("O", (), {"exit_code": 0, "stdout": "B" * 20_000,
                                  "stderr": "", "timed_out": False,
                                  "interrupted": False, "meta": {}})()

    gw.backends["host"] = BigHostBackend()
    llm = ScriptedLLM([
        *[{"tool_use": [ScriptedLLM.tool_call(f"t{i}", "run_cmd",
                                              {"cmd": f"cmd{i}", "runtime": "host",
                                               "threat_class": "trusted"})]}
          for i in range(1, 5)],
        # 第 5 项被摘要压缩调用消费（ScriptedLLM 按调用顺序出剧本）
        {"text": "## 1. 任务目标与用户意图\n用户要测；已跑 cmd1..cmd4。"},
        {"tool_use": [ScriptedLLM.tool_call("tz", "finish", {"summary": "ok"})]},
    ])
    cfg = AgentConfig(max_steps=20, context_summary_chars=1_500,
                      context_char_budget=1_000_000)  # 机械 _trim 不触发
    agent = make_agent(env, llm, config=cfg)
    pass_intent_lead(agent.dispatcher)  # 本测主题=摘要压缩，跳过意图先行闸
    run_objective(agent, "摘要压缩测试")
    # 4 次任务 chat + 1 次摘要 chat + 1 次 finish chat
    assert len(llm.calls) == 6
    summ = [c for c in llm.calls
            if "请把以下 Agent 执行历史" in json.dumps(c["messages"], ensure_ascii=False)]
    assert len(summ) == 1
    hist = json.dumps(summ[0]["messages"], ensure_ascii=False)
    assert "[工具结果]" in hist and "cmd1" in hist          # 旧历史进摘要输入
    # 最后一次任务调用：开头是历史摘要 + 助手确认
    msgs = llm.calls[-1]["messages"]
    assert "历史摘要" in json.dumps(msgs[0], ensure_ascii=False)
    assert msgs[1]["role"] == "assistant"
    # 近端超长工具结果被截断兜底（压后总量仍超阈值时）——保证一次压回触发线以下，
    # 否则下一步立即再压成死循环（实测：保留段大 tool_result 压不下去，5 分钟连压 4 次）
    assert "[已截断]" in json.dumps(msgs, ensure_ascii=False)
    total_after = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
    # 压缩后接近触发线（结构性内容——摘要文本+tool_use 块——约 1.8k 不可再压；
    # 生产阈值 60k 下该下限可忽略，两道截断后必然回到线下）
    assert total_after < 2_000
    evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "llm.compact"]
    assert len(evs) == 1
    assert evs[0]["payload"]["summarized"] >= 3
    assert evs[0]["payload"]["chars_after"] < evs[0]["payload"]["chars_before"]


def test_context_summarization_keeps_small_recent_verbatim(env):
    """压后总量已在阈值内 → 近段工具结果逐字保留（不引入截断标记）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        *[{"tool_use": [ScriptedLLM.tool_call(f"t{i}", "run_cmd",
                                              {"cmd": f"cmd{i}", "runtime": "host",
                                               "threat_class": "trusted"})]}
          for i in range(1, 5)],
        {"text": "## 1. 任务目标与用户意图\n用户要测；已跑 cmd1..cmd4。"},
        {"tool_use": [ScriptedLLM.tool_call("tz", "finish", {"summary": "ok"})]},
    ])
    cfg = AgentConfig(max_steps=20, context_summary_chars=120_000,
                      context_char_budget=1_000_000)
    agent = make_agent(env, llm, config=cfg)
    run_objective(agent, "小结果保留测试")
    msgs = llm.calls[-1]["messages"]
    assert "[已截断]" not in json.dumps(msgs, ensure_ascii=False)


def test_context_summarization_skips_small_history(env):
    """G3 边界：历史没超过阈值 / 消息太少 → 不调用摘要 LLM，直接跳过。"""
    agent = make_agent(env, ScriptedLLM([{"text": "不该被消费"}]))
    assert agent._maybe_summarize([{"role": "user", "content": "hi"}]) is False
    agent2 = make_agent(env, ScriptedLLM([]),
                        config=AgentConfig(max_steps=5, context_summary_chars=3_000))
    # 消息数 ≤ keep_recent+2 → 不触发
    small = [{"role": "user", "content": "x" * 5000},
             {"role": "assistant", "content": "y"},
             {"role": "user", "content": "z"}]
    assert agent2._maybe_summarize(small) is False
    assert len(agent2.llm.script) == 0  # 剧本未被消费


def test_compact_session_chat_rewrites_persistent_history(env):
    """手动持久压缩（/compact，2026-10-06）：把 chat-<sid>.json 的旧问答对经 LLM
    压成摘要后**重写文件**（摘要头 + 近 8 条），后续对话从压缩后历史续起；落
    llm.compact 事件（manual=True）。历史过短 noop 且不烧 LLM。"""
    bb, project, gw, tmp_path = env
    artifacts = tmp_path / "artifacts"
    llm = ScriptedLLM([{"text": "## 1. 任务目标与用户意图\n用户要测；已跑若干步。"}])
    agent = make_agent(env, llm, artifacts_dir=artifacts)
    sid = agent.session["id"]
    path = session_chat_path(artifacts, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    msgs: list[dict] = []
    for i in range(10):  # 10 轮问答 = 20 条
        msgs.append({"role": "user", "content": f"问题 {i}"})
        msgs.append({"role": "assistant", "content": f"回答 {i}"})
    path.write_text(json.dumps({"session_id": sid, "messages": msgs},
                               ensure_ascii=False), encoding="utf-8")

    r = agent.compact_session_chat()
    assert r["status"] == "compacted"
    assert r["before_msgs"] == 20 and r["summarized"] == 12
    assert r["after_msgs"] == 10  # 摘要头 2 条 + 近 8 条
    out = json.loads(path.read_text(encoding="utf-8"))["messages"]
    assert len(out) == 10
    assert "历史摘要" in out[0]["content"]
    assert out[1]["role"] == "assistant"
    assert out[2] == {"role": "user", "content": "问题 6"}  # 旧区 12 条被替换
    evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "llm.compact"]
    assert len(evs) == 1
    assert evs[0]["payload"]["manual"] is True
    assert evs[0]["payload"]["summarized"] == 12

    # 过短：历史 ≤ keep_recent+2 → noop，文件不动，剧本零消费
    llm2 = ScriptedLLM([])
    agent2 = make_agent(env, llm2, artifacts_dir=artifacts)
    sid2 = agent2.session["id"]
    path2 = session_chat_path(artifacts, sid2)
    path2.write_text(json.dumps({"session_id": sid2,
                                 "messages": [{"role": "user", "content": "hi"}]},
                                ensure_ascii=False), encoding="utf-8")
    r2 = agent2.compact_session_chat()
    assert r2["status"] == "noop" and r2["reason"] == "too_short"
    assert len(llm2.script) == 0

    # 无历史文件 → noop(empty)
    agent3 = make_agent(env, ScriptedLLM([]), artifacts_dir=artifacts)
    assert agent3.compact_session_chat() == {
        "status": "noop", "reason": "empty", "before_msgs": 0}


# ---------- 思考事件（DESIGN.md §12：llm.thinking 折叠摘要 + 展开全文） ----------

def test_thinking_lands_in_event_stream(env):
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"thinking": "先看响应头指纹，再决定是否深入。",
         "tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    run_objective(agent, "测一下")
    evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "llm.thinking"]
    assert len(evs) == 1
    assert "响应头指纹" in evs[0]["payload"]["thinking"]
    assert evs[0]["payload"]["step"] == 1
    assert evs[0]["session_id"] == agent.session["id"]


def test_no_thinking_no_event(env):
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    run_objective(agent, "测一下")
    assert not any(e["kind"] == "llm.thinking" for e in bb.recent_events(project["id"]))


# ---------- 思考流式（2026-09-19：llm.thinking.delta 增量 → 终稿清剪） ----------

def test_thinking_stream_deltas_then_final_prunes(env):
    """流式思考：增量事件文本递增（累计全文），终稿带 stream_id，且终稿落库后
    delta 行被清剪——审计只留终稿一条（delta 经 bus 捕获，直播窗口内可见）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"thinking": "终稿思考内容。", "deltas": ["先" + "A" * 130, "后" + "B" * 130],
         "tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    seen: list[dict] = []
    unsub = bb.bus.subscribe(seen.append)
    try:
        run_objective(agent, "测一下")
    finally:
        unsub()
    deltas = [e for e in seen if e["kind"] == "llm.thinking.delta"]
    assert len(deltas) == 2, "两拍增量应各落一条 delta 事件"
    # 累计全文自愈：每条 delta payload.thinking = 此前所有片段拼接
    assert deltas[0]["payload"]["thinking"] == "先" + "A" * 130
    assert deltas[-1]["payload"]["thinking"] == "先" + "A" * 130 + "后" + "B" * 130
    assert deltas[-1]["payload"]["seq"] > deltas[0]["payload"]["seq"]
    assert len({d["payload"]["stream_id"] for d in deltas}) == 1
    finals = [e for e in seen if e["kind"] == "llm.thinking"]
    assert len(finals) == 1
    assert finals[0]["payload"]["thinking"] == "终稿思考内容。"
    assert finals[0]["payload"]["stream_id"] == deltas[0]["payload"]["stream_id"]
    # 终稿落库后 delta 行被清剪（recent_events 直查 DB）
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "llm.thinking.delta"]


def test_thinking_stream_no_deltas_no_delta_events(env):
    """非流式路径（模型不吐 thinking / llm 未声明 stream_capable）不落 delta 行。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    run_objective(agent, "测一下")
    evs = bb.recent_events(project["id"])
    assert not any(e["kind"] == "llm.thinking.delta" for e in evs)


# ---------- 工具参数流截断整轮重试（2026-09-20 事故修复） ----------

def test_truncated_stream_retries_whole_turn(env):
    """LLMError.truncated（工具参数流截断）→ 整轮重试：第一次截断、第二次成功，
    任务正常完成且重发消息与首轮一致（同一对话现场重建）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        LLMError("工具参数流截断（stop_reason=max_tokens，已收 7 字符）",
                 truncated=True),
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    assert run_objective(agent, "测截断重试") == "完"
    assert len(llm.calls) == 2
    assert llm.calls[0]["messages"] == llm.calls[1]["messages"]


def test_truncated_stream_retries_exhausted_raises(env):
    """连续截断超上限（2 次重试）→ 异常穿出（worker 层兜底），共 3 次调用。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([LLMError("截断", truncated=True)] * 3)
    agent = make_agent(env, llm)
    with pytest.raises(LLMError, match="截断"):
        run_objective(agent, "截断耗尽任务")
    assert len(llm.calls) == 3


def test_truncated_stream_final_retry_falls_back_to_non_stream(env):
    """连续截断：前两轮重试保持流式，最后一轮降级非流式（不带 on_thinking——
    网关 SSE 连续断流时整包响应不受影响）。非流式轮成功即任务正常完成。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        LLMError("工具参数流截断（stop_reason=tool_use，已收 80 字符）",
                 truncated=True),
        LLMError("工具参数流截断（stop_reason=tool_use，已收 80 字符）",
                 truncated=True),
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "非流式救回"})]},
    ])
    agent = make_agent(env, llm)
    assert run_objective(agent, "测截断降级") == "非流式救回"
    assert len(llm.calls) == 3
    assert [c["stream"] for c in llm.calls] == [True, True, False]


def test_plain_llm_error_no_retry(env):
    """非截断类 LLMError（网络重试耗尽等）不触发整轮重试——原样穿出。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([LLMError("网络错误（已重试 3 次）")])
    agent = make_agent(env, llm)
    with pytest.raises(LLMError, match="网络错误"):
        run_objective(agent, "测不重试")
    assert len(llm.calls) == 1


# ---------- 任务叙述落事件（2026-09-19 直播间终端化） ----------

def test_abort_interrupts_blocked_llm_call_immediately(env):
    """■ 即点即停（2026-09-19）：LLM 调用阻塞中 request_abort → 不等响应返回
    （旧步边界语义要等当前步做完），立即中断收尾；阻塞的旁路线程被放弃
    （放行后自行结束，结果丢弃）。"""
    bb, project, gw, _ = env
    started, gate = threading.Event(), threading.Event()
    llm = ScriptedLLM([])  # 剧本空——中断后不会真正取到响应
    orig_chat = llm.chat

    def chat(messages, **kw):
        started.set()
        assert gate.wait(10), "gate 超时"
        return orig_chat(messages, **kw)

    llm.chat = chat
    agent = make_agent(env, llm)

    th = threading.Thread(target=lambda: run_objective(agent, "阻塞中断"))
    th.start()
    assert started.wait(5), "chat 未开始"
    agent.request_abort()  # LLM 调用阻塞中：不 set gate，立即中断
    th.join(5)
    assert not th.is_alive()  # 不等 gate 放行即退出
    gate.set()  # 放行被放弃的旁路线程，让它自然结束
    assert "session.aborted" in [e["kind"] for e in bb.recent_events(project["id"])]
    assert agent._abort_req.is_set() is False


def test_add_asset_auto_mount_and_meta_merge(env, monkeypatch):
    # E6 起 domain 由平台自动 DNS 挂载——测试断网 hermetic（解析失败=独立行）
    from core.blackboard import assets as am

    def _no_dns(*a, **k):
        raise OSError("dns off")
    monkeypatch.setattr(am.socket, "getaddrinfo", _no_dns)
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "bb_add_asset",
                                            {"type": "url", "value": "http://10.0.0.8/login"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_add_asset",
                                            {"type": "url", "value": "http://10.0.0.8/login",
                                             "meta": {"title": "登录页", "scanned": True}})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "bb_add_asset",
                                            {"type": "domain", "value": "corp.cn"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    run_objective(agent, "自动挂载")
    assets = bb.list_assets(project["id"])
    hosts = [a for a in assets if a["type"] == "host"]
    assert len(hosts) == 1 and hosts[0]["value"] == "10.0.0.8"   # host 自动建
    urls = [a for a in assets if a["type"] == "url"]
    assert len(urls) == 1                                        # 重报合并不插重复行
    assert urls[0]["parent_id"] == hosts[0]["id"]                # 自动挂载到 host
    assert urls[0]["meta"]["title"] == "登录页" and urls[0]["meta"]["scanned"] is True
    domains = [a for a in assets if a["type"] == "domain"]
    assert domains[0]["parent_id"] is None                       # 域名不猜 DNS，不自动挂


# ---------- bb_add_artifact 产物落盘 + finding POC 引用（批次 3） ----------

def test_add_artifact_writes_file_and_sha256(env):
    import hashlib

    bb, project, gw, tmp_path = env
    artifacts_dir = tmp_path / "artifacts_out"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "t1", "bb_add_artifact",
            {"filename": "poc_sqli.py", "content": "import requests\n",
             "kind": "poc", "description": "SQL 注入 POC 脚本"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, artifacts_dir=artifacts_dir)
    pass_intent_lead(agent.dispatcher)  # 本测主题=产物落盘，跳过意图先行闸
    run_objective(agent, "落 POC 产物")

    path = artifacts_dir / "poc" / "poc_sqli.py"
    assert path.read_text(encoding="utf-8") == "import requests\n"
    rows = bb.conn.execute(
        "SELECT * FROM artifacts WHERE project_id=?", (project["id"],)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "poc" and row["path"] == "poc/poc_sqli.py"
    assert row["author"] == agent.session["id"]
    assert row["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    events = bb.recent_events(project["id"])
    assert any(e["kind"] == "artifact.new" and e["payload"]["artifact_id"] == row["id"]
               for e in events)


def test_add_artifact_without_dir_reports_error(env):
    """未装配 artifacts_dir → 返回错误文本，循环不断。"""
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "bb_add_artifact",
                                            {"filename": "x.py", "content": "1"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)  # 不传 artifacts_dir
    run_objective(agent, "缺产物目录")
    rows = agent.bb.conn.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE project_id=?",
        (agent.project_id,)).fetchone()
    assert rows["c"] == 0
    # 错误文本已回填给 LLM（工具结果消息里能看到提示）
    assert any("产物目录" in json.dumps(m, ensure_ascii=False) for m in llm.calls[1]["messages"])


def test_add_finding_with_poc_artifact(env):
    """bb_add_finding 透传 poc_artifact_id（evidence.poc 引 Python 脚本产物的场景）。"""
    bb, project, gw, _ = env
    art_id = bb.add_artifact(project["id"], "poc/poc.py", kind="poc",
                             sha256="a" * 64, author="test")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "t1", "bb_add_finding",
            {"vuln_class": "sqli", "title": "登录框注入", "severity": "high",
             "status": "verified", "poc_artifact_id": art_id,
             "evidence": {"poc": {"type": "python", "stability": "3/3"}}})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    declare_intent(bb, project["id"], "对登录框进行 sql 注入尝试",
                   author=agent.session["id"])  # 过 bb_add_finding 必挂意图门禁
    # 意图先行闸（2026-09-28）：意图声明后须有执行动作——补 command 事件模拟验证
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=agent.session["id"], author=agent.session["id"])
    run_objective(agent, "带 POC 引用的发现")
    rows = [f for f in bb.list_findings(project["id"]) if f["vuln_class"] == "sqli"]
    assert len(rows) == 1
    assert rows[0]["poc_artifact_id"] == art_id
    assert rows[0]["status"] == "verified"


def test_add_finding_relates_to_passthrough_and_dangling_reported(env):
    """bb_add_finding 的 relates_to 顶层入参并入 evidence；悬空 id 回填错误不中断。"""
    bb, project, gw, _ = env
    base = bb.add_finding(project["id"], "info-leak", "指纹信息")["id"]
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "t1", "bb_add_finding",
            {"vuln_class": "sqli", "title": "注入升级", "severity": "low",
             "relates_to": [{"finding_id": base, "note": "同目标升级"}]})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t2", "bb_add_finding",
            {"vuln_class": "xss", "title": "幻觉强边", "severity": "low",
             "relates_to": [{"finding_id": "find-deadbeef", "note": "悬空"}]})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "完"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    declare_intent(bb, project["id"], "对注入点进行升级利用尝试",
                   author=agent.session["id"])  # 过 bb_add_finding 必挂意图门禁
    # 意图先行闸（2026-09-28）：补 command 事件模拟意图后的执行动作
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=agent.session["id"], author=agent.session["id"])
    run_objective(agent, "强关系登记")
    rows = {f["vuln_class"]: f for f in bb.list_findings(project["id"])}
    assert rows["sqli"]["evidence"]["relates_to"] == [
        {"finding_id": base, "note": "同目标升级"}]
    # 悬空强边整笔 finding 不落（ValueError 在写入前）
    assert "xss" not in rows
    # 错误文本回填给 LLM，循环未中断（finish 正常收尾）
    assert any("不存在或不属于本项目" in json.dumps(m, ensure_ascii=False)
               for m in llm.calls[2]["messages"])


def test_update_finding_tool_downgrade_and_gates(env):
    """bb_update_finding：降级+rating_basis 落库；显式改 info 被门禁拒绝回填；
    不存在的发现回填错误。"""
    bb, project, gw, _ = env
    fid = bb.add_finding(project["id"], "exposure", "WinRM 公网暴露",
                         severity="high", track="pentest")["id"]
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "t1", "bb_update_finding",
            {"finding_id": fid, "severity": "low", "category": "intel",
             "rating_basis": "rating:edu-rating 低危#1 非核心数据"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t2", "bb_update_finding",
            {"finding_id": fid, "severity": "info"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t3", "bb_update_finding",
            {"finding_id": "find-deadbeef", "severity": "low"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    run_objective(agent, "发现修订")
    row = bb.get_finding(project["id"], fid)
    assert row["severity"] == "low" and row["category"] == "intel"
    assert row["rating_basis"].startswith("rating:edu-rating 低危#1")
    # t2 门禁拒收回填、t3 不存在错误回填，循环未中断
    back = json.dumps(llm.calls, ensure_ascii=False)
    assert "不再收录 severity=info" in back
    assert "发现不存在" in back


def test_delete_finding_tool_guards(env):
    """bb_delete_finding：verified 拒删、reason 必填、unverified+reason 删除成功。"""
    bb, project, gw, _ = env
    v = bb.add_finding(project["id"], "sqli", "已验证注入", severity="high",
                       status="verified", poc_artifact_id=None,
                       evidence={"poc": {"type": "steps", "stability": "3/3"}},
                       track="pentest")["id"]
    g = bb.add_finding(project["id"], "noise", "走查垃圾", severity="low",
                       track="pentest")["id"]
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "t1", "bb_delete_finding", {"finding_id": g})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t2", "bb_delete_finding", {"finding_id": v, "reason": "想删"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t3", "bb_delete_finding", {"finding_id": g, "reason": "重复噪声"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    run_objective(agent, "发现删除")
    assert bb.get_finding(project["id"], g) is None  # t3 成功删除
    assert bb.get_finding(project["id"], v) is not None  # t2 verified 拒删
    back = json.dumps(llm.calls, ensure_ascii=False)
    assert "删除必须说明原因" in back  # t1 无 reason 拒
    assert "verified 发现不可删除" in back


def test_add_artifact_poc_python_only(env):
    """kind=poc 仅限 Python（§5.2 纪律）：非 .py 拒绝（返回错误文本，不落盘）。"""
    bb, project, gw, tmp_path = env
    artifacts_dir = tmp_path / "artifacts_out"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "bb_add_artifact",
                                            {"filename": "poc.ps1", "content": "Write-Host 1",
                                             "kind": "poc"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_add_artifact",
                                            {"filename": "poc.py", "content": "print(1)",
                                             "kind": "poc"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, artifacts_dir=artifacts_dir)
    pass_intent_lead(agent.dispatcher)  # 本测主题=POC 语言纪律，跳过意图先行闸
    run_objective(agent, "POC 语言纪律")
    rows = bb.conn.execute(
        "SELECT path FROM artifacts WHERE project_id=?", (project["id"],)).fetchall()
    assert [r["path"] for r in rows] == ["poc/poc.py"]  # ps1 被拒，只有 .py 落库
    assert (artifacts_dir / "poc" / "poc.py").is_file()
    assert not (artifacts_dir / "poc" / "poc.ps1").exists()


def test_kb_open_module(env):
    """kb_open：M0 合成源命中返回绝对路径 + kb.open 审计；不存在返回清单（防幻觉）；无 kb 域报错。"""
    bb, project, gw, tmp_path = env
    # M0：单域单源（packs/kb/web，恒递归含子目录）
    kb = tmp_path / "packs" / "kb" / "web"
    (kb / "sub").mkdir(parents=True)
    (kb / "xss-test.md").write_text("# XSS 测试", encoding="utf-8")
    (kb / "sub" / "deep.md").write_text("深层模块", encoding="utf-8")

    # 命中（全局形态带域前缀，含递归深层路径）
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "kb_open", {"module": "web/sub/deep.md"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    run_objective(agent, "开知识库")
    result = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "deep.md" in result               # 返回了模块绝对路径（JSON 内 \\ 转义）
    assert "知识库模块（域 web）" in result and "禁止通读" in result
    events = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "kb.open" in events

    # 模块不存在 → 返回可用清单（递归源含深层模块）
    llm2 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "kb_open", {"module": "no-such.md"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent2 = make_agent(env, llm2)
    run_objective(agent2, "幻觉模块名")
    result2 = json.dumps(llm2.calls[1]["messages"], ensure_ascii=False)
    assert "防幻觉" in result2 and "xss-test.md" in result2 and "web/sub/deep.md" in result2

    # .. 穿越直接拒绝
    llm_t = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "kb_open", {"module": "../../x.md"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent_t = make_agent(env, llm_t)
    run_objective(agent_t, "穿越")
    result_t = json.dumps(llm_t.calls[1]["messages"], ensure_ascii=False)
    assert "拒绝" in result_t
    # 穿越尝试不落 kb.open（只统计本会话事件，前两个 agent 的命中事件已在流中）
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "kb.open" and e["session_id"] == agent_t.session["id"]]

    # 域无 kb 目录 → 错误文本不断循环
    shutil.rmtree(kb)
    llm3 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "kb_open", {"module": "x.md"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent3 = make_agent(env, llm3)
    run_objective(agent3, "无知识库")
    result3 = json.dumps(llm3.calls[1]["messages"], ensure_ascii=False)
    assert "无知识库" in result3


# ---------- 角色软边界（DESIGN.md §6.6：tools） ----------

def test_role_tools_whitelist_blocks(env):
    """角色 tools 白名单外的工具被拒（越界文本回填，循环不断）；收尾工具永远放行。"""
    bb, project, gw, _ = env
    write_role(env, "scout",
               'name: scout\npersona: "侦察"\nskills: null\n'
               "tools: [bb_query]\n")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "被拦后收工"})]},
    ])
    agent = make_agent(env, llm, role="scout")
    assert run_objective(agent, "越权命令") == "被拦后收工"
    tool_msgs = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "[越界拒绝]" in tool_msgs and "工具白名单" in tool_msgs
    # 没有真正执行 → 无 command 审计事件
    assert "command" not in [e["kind"] for e in bb.recent_events(project["id"])]


# ---------- v23 任务默认运行时（TRAE 新壳 M3，2026-09-25） ----------

def test_raw_arguments_wrapper_unwrapped(env):
    """raw_arguments 解包垫片（2026-09-26）：单键 raw_arguments 包裹（dict 或
    JSON 字符串）自动解包平铺重派；内层裸十六进制等已知退化先容错修复再执行
    （写成功直接正常返回，不回提示——ark-code-latest 调 bb_upsert_func 连发
    6 次全中的退化序列化，模型自己改不掉，须平台兜底）；修复不了的语法残缺
    → [参数格式] 精确指引。"""
    bb, project, gw, _ = env
    from core.agent.tools import ToolDispatcher
    sid = "sess-" + "r" * 12
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid)
    pass_intent_lead(d)  # 本测主题=raw_arguments 垫片，跳过意图先行闸
    inner = {"type": "domain", "value": "wrap.com"}
    # dict 形包裹：解包后正常执行
    r = d.dispatch("bb_add_asset", {"raw_arguments": dict(inner)})
    assert "[错误]" not in r and "[工具异常]" not in r and "[参数格式]" not in r
    # JSON 字符串形包裹：同样解包
    r = d.dispatch("bb_add_asset", {"raw_arguments": json.dumps(inner)})
    assert "[错误]" not in r and "[工具异常]" not in r
    assert "wrap.com" in d.dispatch("bb_query", {"what": "assets"})
    # 内层裸十六进制（实测事故形态）：容错修复后照常执行成功
    r = d.dispatch("bb_upsert_func", {"raw_arguments":
                   '{"binary_sha256":"deadbeef","address":0x401160,'
                   '"name":"main_check","analysis":"裸 0x 地址在字符串外"}'})
    assert "[参数格式]" not in r and "[工具异常]" not in r and "func=" in r
    # 字符串内的 0x 不受修复影响；内层是 JSON 标量而非对象 → 指引
    r = d.dispatch("bb_add_asset",
                   {"raw_arguments": json.dumps({"type": "domain",
                                                 "value": "0xdeadbeef.com"})})
    assert "[错误]" not in r and "[参数格式]" not in r
    # 修复不了的语法残缺 → [参数格式] 精确指引
    r = d.dispatch("bb_add_asset", {"raw_arguments": '{"type": "domain",,}'})
    assert r.startswith("[参数格式]") and "平铺" in r
    # 内层是 JSON 标量 → 同样指引
    r = d.dispatch("bb_add_asset", {"raw_arguments": json.dumps("oops")})
    assert r.startswith("[参数格式]") and "平铺" in r


# ---------- 逆向检索工具面（2026-09-27：strings_search / func_xrefs / list_symbols 过滤） ----------

class _FakeDecomp:
    """最小 DecompilerService 桩：只覆盖新工具面用到的三个方法。"""

    def list_functions(self, binary, name_contains=None, min_size=None):
        rows = [{"address": "0x1000", "name": "main", "size": 293, "pseudocode": True},
                {"address": "0x1100", "name": "sub_1100", "size": 12, "pseudocode": False}]
        if name_contains:
            rows = [r for r in rows if name_contains.lower() in r["name"].lower()]
        if min_size:
            rows = [r for r in rows if r["size"] >= min_size]
        return json.dumps(rows)

    def strings_for(self, binary, q=None, limit=200):
        items = [{"address": "0x2000", "string": "Mht!^okHGfdCbn!@4t>", "refs": []}]
        if q:
            items = [i for i in items if q.lower() in i["string"].lower()]
        return json.dumps({"count": len(items), "total_matched": len(items),
                           "truncated": False, "items": items})

    def xrefs_for_func(self, binary, address=None, name=None):
        return json.dumps({"function": name or hex(address or 0),
                           "callers": ["main"], "callees": []})


def test_reverse_search_tools(env):
    """strings_search/func_xrefs 正常回填 JSON；list_symbols 过滤参数透传；
    未装配回 [未装配]；三个只读工具都在计划闸预放行集。"""
    bb, project, gw, _ = env
    from core.agent.tools import ToolDispatcher
    sid = "sess-" + "v" * 12
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid, decompiler=_FakeDecomp())
    r = json.loads(d.dispatch("strings_search",
                              {"binary": "b.exe", "pattern": "MHT"}))
    assert r["count"] == 1 and r["items"][0]["address"] == "0x2000"  # 大小写不敏感
    r = json.loads(d.dispatch("func_xrefs", {"binary": "b.exe", "name": "check"}))
    assert r["callers"] == ["main"]
    r = json.loads(d.dispatch("func_xrefs", {"binary": "b.exe", "address": "0x1189"}))
    assert r["function"] == "0x1189"  # 字符串地址经 _coerce_addr 转整数传给服务层
    r = json.loads(d.dispatch("list_symbols",
                              {"binary": "b.exe", "name_contains": "MAIN"}))
    assert [x["name"] for x in r] == ["main"]
    # 未装配
    d2 = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                        session_id="sess-" + "w" * 12, author="x")
    for name, args in (("strings_search", {"binary": "b"}),
                       ("func_xrefs", {"binary": "b", "name": "f"}),
                       ("list_symbols", {"binary": "b"})):
        assert d2.dispatch(name, args).startswith("[未装配]")
    # 只读侦察工具不受意图先行闸限制（无 open 意图也可先行）
    d3 = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                        session_id="sess-" + "x" * 12, author="x",
                        decompiler=_FakeDecomp())
    r = d3.dispatch("strings_search", {"binary": "b.exe"})
    assert "意图先行闸" not in r and json.loads(r)["count"] == 1


def _tool_calls(bb, pid):
    return [e for e in bb.recent_events(pid) if e["kind"] == "tool.call"]


def test_tool_call_event_success_and_gates(env):
    """普通工具调用落 tool.call（name/args/耗时/成败/结果头）；拒绝路径 ok=False 也落。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, _ = env
    sid = bb.register_session(project["id"], "auditor")["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid)
    d.dispatch("bb_query", {"what": "events"})
    evs = _tool_calls(bb, project["id"])
    assert len(evs) == 1
    p = evs[0]["payload"]
    assert p["name"] == "bb_query" and p["args"] == {"what": "events"}
    assert p["ok"] is True and isinstance(p["duration_s"], (int, float))
    assert isinstance(p["result_head"], str) and p["result_head"]

    # 拒绝路径同样可观测且 ok=False（未知工具名 → [错误] 前缀，审计仍落）
    d2 = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                        session_id="sess-" + "z" * 12, author="z")
    r = d2.dispatch("no_such_tool", {"x": 1})
    assert r.startswith("[错误]")
    evs2 = [e for e in _tool_calls(bb, project["id"]) if e["session_id"] != sid]
    assert len(evs2) == 1 and evs2[0]["payload"]["ok"] is False
    assert "[错误]" in evs2[0]["payload"]["result_head"]


def test_tool_call_skips_run_cmd_and_truncates_args(env):
    """run_cmd 不落 tool.call（gateway 已有 command/command.result）；长入参截断。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, _ = env
    sid = bb.register_session(project["id"], "auditor3")["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid)
    declare_intent(bb, project["id"], "对目标执行登记尝试", author=sid)  # 过必挂意图门禁
    d.dispatch("run_cmd", {"cmd": "whoami", "runtime": "host", "threat_class": "trusted"})
    assert _tool_calls(bb, project["id"]) == []

    d.dispatch("bb_add_finding", {"title": "长" * 500, "severity": "low",
                                  "detail": "x" * 500})
    evs = _tool_calls(bb, project["id"])
    assert len(evs) == 1
    a = evs[0]["payload"]["args"]
    assert len(a["title"]) < 500 and a["title"].endswith("(截断)")
    assert a["severity"] == "low"  # 短值原样

    # 未知工具：拒绝路径落事件 ok=False
    d.dispatch("no_such_tool", {"x": 1})
    evs = _tool_calls(bb, project["id"])
    assert len(evs) == 2 and evs[-1]["payload"]["ok"] is False
    assert "[错误]" in evs[-1]["payload"]["result_head"]


# ---------- A5.1：子代理分解发任务（parent/created_by 服务端钉死） ----------

def test_request_steps_gate_and_extension(env):
    """request_steps（E8）：剩余 >20 拒收防囤步；≤20 放行固定 +200 并落审计；
    未装配预算（max_steps=0）拒收。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, _ = env
    sid = bb.register_session(project["id"], "budget")["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid, max_steps=40)
    d.set_step(5)   # 剩余 35 > 20 → 拒
    r = d.dispatch("request_steps", {"reason": "想囤点步数"})
    assert r.startswith("[拒绝]") and "35" in r and d.max_steps == 40
    d.set_step(25)  # 剩余 15 → 放行
    r = d.dispatch("request_steps", {"reason": "深度扫描未完"})
    assert "240" in r and d.max_steps == 240
    ev = [e for e in bb.recent_events(project["id"])
          if e["kind"] == "step.budget_extended"]
    assert ev and ev[-1]["payload"]["old_max"] == 40
    assert ev[-1]["payload"]["new_max"] == 240
    assert ev[-1]["payload"]["by"] == "agent"

    # 未装配预算的裸 dispatcher → 拒收不崩
    sid2 = bb.register_session(project["id"], "nobudget")["id"]
    d2 = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                        session_id=sid2, author=sid2)
    assert d2.dispatch("request_steps", {}).startswith("[拒绝]")


def test_budget_exhaustion_pauses_not_fails(env):
    """步数耗尽（E8）：不再 fail——会话 paused、快照 reason=budget、
    session.budget_paused 事件；不落 session.finished（会话存活待命）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "bb_query", {"what": "events"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_query", {"what": "events"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "bb_query", {"what": "events"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=3))
    assert run_objective(agent, "步数耗尽演练") is None   # 暂停退出，非任务总结
    assert agent.paused is True
    assert agent._resume_state["reason"] == "budget"
    assert agent._resume_state["task_id"] is None         # 任务机制退役：无任务现场
    assert agent._resume_state["next_step"] == 3          # 断点=耗尽步
    evs = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "session.budget_paused" in evs and "session.finished" not in evs
    # 预算提醒已注入（剩余 <20 起每步边界）
    assert any("预算剩余" in json.dumps(c["messages"], ensure_ascii=False)
               for c in llm.calls)


def test_budget_exhaustion_self_rescue_via_request_steps(env):
    """步数耗尽前自救：循环每轮重读 max_steps——模型在剩余 ≤20 时调
    request_steps +200，上界前移、同轮续跑至完成。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "bb_query", {"what": "events"})]},
        # 第 2 步（剩余 0 ≤20）：当场申请增补
        {"tool_use": [ScriptedLLM.tool_call("t2", "request_steps",
                                            {"reason": "还差最后一步"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "bb_query", {"what": "events"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "finish", {"summary": "自救成功"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=2))
    assert run_objective(agent, "自救演练") == "自救成功"  # request_steps +200 续上
    ev = [e for e in bb.recent_events(project["id"])
          if e["kind"] == "step.budget_extended"]
    assert ev and ev[-1]["payload"]["by"] == "agent"


def test_hard_reject_breaker_per_model_step_with_reset(env):
    """硬熔断按模型步计：并行批 3 张硬拒绝票只计 1 步不熔断；夹一个无硬拒绝步
    清零；随后连续 3 个硬拒绝步才熔断（agent.reject_breaker streak=3）。"""
    bb, project, gw, _ = env

    def malware_batch(cid, n=1):
        return {"tool_use": [
            ScriptedLLM.tool_call(f"{cid}-{i}", "run_cmd",
                                  {"cmd": f"evil-{i}", "runtime": "host",
                                   "threat_class": "malware_live"})
            for i in range(n)]}

    llm = ScriptedLLM([
        malware_batch("s1", 3),                                        # streak 1
        {"tool_use": [ScriptedLLM.tool_call("q", "bb_query",
                                            {"what": "events"})]},      # 清零
        malware_batch("s3"),                                           # streak 1
        malware_batch("s4"),                                           # streak 2
        malware_batch("s5"),                                           # streak 3 → 熔断
    ])
    agent = make_agent(env, llm)
    pass_intent_lead(agent.dispatcher)  # 本测主题=硬熔断，跳过意图先行闸
    assert run_objective(agent, "硬熔断演练") is None
    br = [e for e in bb.recent_events(project["id"])
          if e["kind"] == "agent.reject_breaker"]
    assert len(br) == 1 and br[-1]["payload"]["streak"] == 3


def test_search_files_hits_and_read_anywhere(env):
    """search_files（2026-09-24 / 读路径放开 2026-10-01）：正则/子串命中、工作区
    外路径也可搜（不再拒绝）、无命中回执；计划前可调（计划闸白名单）。"""
    bb, project, gw, tmp_path = env
    agent = make_agent(env, ScriptedLLM([]), artifacts_dir=tmp_path / "artifacts")
    d = agent.dispatcher
    spill = tmp_path / "spill"
    spill.mkdir(parents=True)
    (spill / "dump.txt").write_text(
        "noise line\nwx.zut.edu.cn 命中行\n", encoding="utf-8")
    r = d.dispatch("search_files",
                    {"pattern": r"wx\.zut", "path": "../spill"})
    assert "dump.txt:2" in r and "wx.zut.edu.cn" in r
    # 子串大小写不敏感
    r2 = d.dispatch("search_files",
                     {"pattern": "WX.ZUT", "regex": False, "path": "../spill"})
    assert "dump.txt:2" in r2
    # 读路径放开（2026-10-01）：工作区外目录不再拒绝（用显式目录保持扫描有界）
    out = tmp_path.parent / f"outside-{tmp_path.name}"
    out.mkdir(exist_ok=True)
    (out / "o.txt").write_text("outside-hit-xyz", encoding="utf-8")
    r3 = d.dispatch("search_files", {"pattern": "outside-hit-xyz", "path": str(out)})
    assert "o.txt" in r3 and not r3.startswith("[拒绝]")
    # 无命中
    assert d.dispatch("search_files",
                      {"pattern": "zzz-no-such-xyz", "path": "../spill"}
                      ).startswith("[无命中]")
    # 非法正则
    assert d.dispatch("search_files",
                      {"pattern": "([unclosed", "path": "../spill"}
                      ).startswith("[错误]")


def test_run_cmd_bash_connector_degrade_to_wsl(env, monkeypatch):
    """bash 连接符兼容（2026-10-01）：host·Windows 遇 &&/|| 且策略允许 wsl 时，
    自动改走 wsl（bash -lc）并在回执注明；纯单条命令不触发。"""
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pass_intent_lead(d)  # 本测主题=连接符降级，跳过意图先行闸
    import core.agent.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_host_is_windows", lambda: True)
    seen = {}

    def fake_run(cmd, runtime, **kw):
        seen["runtime"] = runtime
        from core.runtime.gateway import ExecutionResult
        return ExecutionResult(ok=True, exit_code=0, stdout="ok", stderr="",
                               runtime=runtime, duration_s=0.1)

    monkeypatch.setattr(gw, "run", fake_run)
    r = d.dispatch("run_cmd", {"cmd": "cd /tmp && ls", "runtime": "host",
                               "threat_class": "trusted"})
    assert seen["runtime"] == "wsl" and "[已自动改用 wsl]" in r
    # 无连接符：仍按 host 执行，不加注记
    d.dispatch("run_cmd", {"cmd": "id", "runtime": "host", "threat_class": "trusted"})
    assert seen["runtime"] == "host"


def test_run_cmd_bash_connector_policy_denied_falls_back(env, monkeypatch):
    """策略不允许 wsl 时（threat_class=untrusted 只放 docker/sandbox），
    host+Windows 遇 && 不降级——回落明确报错引导，不越权。"""
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pass_intent_lead(d)  # 本测主题=连接符拒绝，跳过意图先行闸
    import core.agent.tools as tools_mod
    monkeypatch.setattr(tools_mod, "_host_is_windows", lambda: True)
    r = d.dispatch("run_cmd", {"cmd": "cd /tmp && ls", "runtime": "host",
                               "threat_class": "untrusted"})
    assert r.startswith("[错误]") and "PowerShell" in r and "wsl" in r


def test_bb_query_findings_filters_passthrough(env):
    """bb-query-filters M1：findings 的 min_severity/verified_only/category
    透传底层 store（能力早已实现、工具层没接出）。"""
    from core.blackboard.assets import register_asset
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pid = project["id"]
    a1 = register_asset(bb, pid, "1.2.3.4", "host", quiet=True)["id"]
    bb.add_finding(pid, "暴露面", "RDP 暴露", severity="high",
                   target_asset_id=a1, category="vuln")
    bb.add_finding(pid, "服务指纹", "Banner", severity="low",
                   target_asset_id=a1, status="verified", category="vuln",
                   evidence={"repro_steps": [{
                       "desc": "抓取服务 Banner",
                       "type": "http",
                       "code": "curl -i http://1.2.3.4/",
                       "expected": "响应头含服务 Banner"}]})
    bb.add_finding(pid, "关键发现", "有东西", severity="medium", category="intel")
    rows = lambda s: json.loads(s)
    # min_severity=high → 只回 high+critical
    assert [f["title"] for f in rows(d.dispatch(
        "bb_query", {"what": "findings", "min_severity": "high"}))] == ["RDP 暴露"]
    # verified_only
    assert [f["title"] for f in rows(d.dispatch(
        "bb_query", {"what": "findings", "verified_only": True}))] == ["Banner"]
    # category
    assert [f["title"] for f in rows(d.dispatch(
        "bb_query", {"what": "findings", "category": "intel"}))] == ["有东西"]
    # target_asset_id 组合
    assert len(rows(d.dispatch(
        "bb_query", {"what": "findings", "target_asset_id": a1}))) == 2
    # 默认行为不变：不带新参数 = 全量 3 条，输出字段集合不变
    got = rows(d.dispatch("bb_query", {"what": "findings"}))
    assert len(got) == 3
    assert set(got[0]) == {"id", "vuln_class", "title", "severity",
                           "status", "confidence"}


def test_bb_query_site_subtree_aggregate(env):
    """单站全貌查询（2026-09-26）：what=site 一次返回根资产子树+各自发现+
    相关意图——替代按 type 分片全量拉回本地过滤（实测一个站点核实曾烧
    近 20 次 bb_query）。asset 支持 id 或精确 value（host/domain 优先）。"""
    from core.blackboard.assets import register_asset
    from core.blackboard.intents import close_intent, declare_intent
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pid = project["id"]
    host = register_asset(bb, pid, "5.6.7.8", "host", quiet=True)["id"]
    url1 = register_asset(bb, pid, "http://5.6.7.8/admin", "url",
                          parent_id=host, quiet=True)["id"]
    other = register_asset(bb, pid, "9.9.9.9", "host", quiet=True)["id"]
    f1 = bb.add_finding(pid, "暴露面", "后台暴露", severity="high",
                        target_asset_id=url1, category="vuln")
    bb.add_finding(pid, "无关", "别站发现", severity="low",
                   target_asset_id=other, category="vuln")
    it = declare_intent(bb, pid, "假设后台存在弱口令",
                        target_asset_id=url1, author="tester")
    close_intent(bb, pid, it["id"], outcome="vuln", finding_ids=[f1["id"]],
                 author="tester")
    # 旁站意图不应混入
    declare_intent(bb, pid, "假设旁站有洞", target_asset_id=other, author="tester")
    rows = lambda s: json.loads(s)
    # 按 value 解析（host/domain 优先）与按 id 解析等价
    by_val = rows(d.dispatch("bb_query", {"what": "site", "asset": "5.6.7.8"}))
    by_id = rows(d.dispatch("bb_query", {"what": "site", "asset": host}))
    assert by_val["root"]["id"] == by_id["root"]["id"] == host
    # 子树资产：host + url，不含旁站；发现/意图只收子树相关
    assert [a["value"] for a in by_val["assets"]] == ["5.6.7.8", "http://5.6.7.8/admin"]
    assert [f["id"] for f in by_val["findings"]] == [f1["id"]]
    assert [i["id"] for i in by_val["intents"]] == [it["id"]]
    assert by_val["counts"] == {"assets": 2, "findings": 1, "intents": 1,
                                "open_intents": 0}
    assert by_val["intents"][0]["outcome"] == "vuln"
    assert by_val["hint"]
    # 找不到资产 → 显式错误（附可选值样例），不静默空
    r = d.dispatch("bb_query", {"what": "site", "asset": "no.such.host"})
    assert r.startswith("[错误]") and "5.6.7.8" in r
    # 缺 asset 参数 → 显式错误
    assert d.dispatch("bb_query", {"what": "site"}).startswith("[错误]")


def test_bb_query_assets_asset_filter_and_slim_meta(env):
    """2026-10-01：what=assets 支持 asset 子树过滤（修「按域名查拿到全量」）、
    meta 默认精简（verbose=true 才全量）、未传 limit 默认 200 防瀑。"""
    from core.blackboard.assets import register_asset
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pid = project["id"]
    host = register_asset(bb, pid, "10.0.0.1", "host", quiet=True)["id"]
    dom = register_asset(bb, pid, "site.com", "domain",
                         meta={"title": "示例站", "source": "fofa",
                               "products": [f"p{i}" for i in range(20)]},
                         quiet=True)["id"]
    register_asset(bb, pid, "http://site.com/a", "url", quiet=True)
    register_asset(bb, pid, "10.0.0.9", "host", quiet=True)  # 旁支
    r = lambda s: json.loads(s)
    # asset 过滤：只回该域子树（domain + url），不含旁支 host
    q = r(d.dispatch("bb_query", {"what": "assets", "asset": "site.com"}))
    vals = {a["value"] for a in q["assets"]}
    assert vals == {"site.com", "http://site.com/a"}
    assert q["counts"]["total"] == 2 and q["counts"]["returned"] == 2
    # meta 精简：products 截到 8 项；verbose=true 给全 20 项
    domrow = next(a for a in q["assets"] if a["id"] == dom)
    assert len(domrow["meta"]["products"]) == 8
    qv = r(d.dispatch("bb_query", {"what": "assets", "asset": dom, "verbose": True}))
    assert len(next(a for a in qv["assets"] if a["id"] == dom)["meta"]["products"]) == 20
    # 找不到资产 → [错误] 引导，不静默返回全量
    assert d.dispatch("bb_query",
                      {"what": "assets", "asset": "nope.com"}).startswith("[错误]")
    # 无过滤时返回全项目计数（>= 上述 4 个显式登记；url 自动挂载可能补节点）
    qall = r(d.dispatch("bb_query", {"what": "assets"}))
    assert qall["counts"]["total"] >= 4
    assert qall["counts"]["total"] > q["counts"]["total"]  # 全量 > 子树


def test_bb_query_assets_default_limit(env):
    """未传 limit 时 assets 默认截断到 _BB_ASSETS_DEFAULT_LIMIT，防大体积落盘。"""
    from core.blackboard.assets import register_asset
    from core.agent.tools import _BB_ASSETS_DEFAULT_LIMIT
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pid = project["id"]
    for i in range(_BB_ASSETS_DEFAULT_LIMIT + 20):
        register_asset(bb, pid, f"10.1.{i // 256}.{i % 256}", "host", quiet=True)
    q = json.loads(d.dispatch("bb_query", {"what": "assets"}))
    assert q["counts"]["total"] == _BB_ASSETS_DEFAULT_LIMIT + 20
    assert q["counts"]["returned"] == _BB_ASSETS_DEFAULT_LIMIT


def test_bb_query_events_kinds_and_limit(env):
    """M2：events 的 kinds + session_id 过滤端到端；六面统一 limit——
    events 走 tail、其余面应用层裁剪；非法 limit 钳 1-200。"""
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pid = project["id"]
    s1 = agent.session["id"]
    s2 = bb.register_session(pid, "S2")["id"]
    for sid in (s1, s2):
        bb.append_event(pid, "command", {"sid": sid}, session_id=sid, author=sid)
        bb.append_event(pid, "finding.new", {"sid": sid}, session_id=sid, author=sid)
    rows = lambda s: json.loads(s)
    # kinds 多值 + session_id
    got = rows(d.dispatch("bb_query",
                          {"what": "events", "kinds": ["command"], "session_id": s1}))
    assert len(got) == 1 and got[0]["payload"]["sid"] == s1
    assert got[0]["session_id"] == s1 and "created_at" in got[0] and "id" in got[0]
    got = rows(d.dispatch("bb_query",
                          {"what": "events", "kinds": ["command", "finding.new"]}))
    assert len(got) == 4
    # events limit 生效（tail）；dispatch 审计会产生 tool.call，用 kinds 锁定过程事件
    got = rows(d.dispatch("bb_query", {"what": "events", "kinds": ["command", "finding.new"],
                                        "limit": 2}))
    assert len(got) == 2
    # events 默认 limit 仍 50；同上用 kinds 过滤掉 dispatch 自身的 tool.call 审计
    assert len(rows(d.dispatch("bb_query", {"what": "events",
                                            "kinds": ["command", "finding.new"]}))) == 4
    # findings limit 应用层裁剪
    for i in range(5):
        bb.add_finding(pid, f"x{i}", f"F{i}", severity="low")
    assert len(rows(d.dispatch("bb_query", {"what": "findings", "limit": 2}))) == 2
    # 非法 limit 钳制（0→1、500→200、"x"→忽略）
    assert len(rows(d.dispatch("bb_query", {"what": "events", "limit": 0}))) == 1
    got = rows(d.dispatch("bb_query", {"what": "findings", "limit": 9999}))
    assert len(got) <= 200
    assert len(rows(d.dispatch(
        "bb_query", {"what": "findings", "limit": "oops"}))) == 5


def test_run_chat_replies_without_task(env):
    """空闲对话轮（2026-09-19）：无任务上下文直接对话——drain 未读 human_note →
    LLM 回复落 `agent.chat` 事件并返回文本；可带工具查黑板；finish 不在工具集。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("c1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"text": "你好，我是通用测试员。"},
    ])
    agent = make_agent(env, llm)
    pass_intent_lead(agent.dispatcher)  # 本测主题=对话轮，跳过意图先行闸
    sid = agent.session["id"]
    bb.post_human_note(project["id"], sid, "介绍下自己")
    reply = agent.run_chat()
    assert reply == "你好，我是通用测试员。"
    # 回复落事件流（💬 Agent 回复）
    chat_evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "agent.chat"]
    assert chat_evs and "通用测试员" in chat_evs[-1]["payload"]["text"]
    # 工具真的执行了（command 事件），消息历史带 tool_result
    assert "command" in [e["kind"] for e in bb.recent_events(project["id"])]
    assert json.dumps(llm.calls[1]["messages"], ensure_ascii=False).count("tool_result") >= 1
    # 收件箱 human_note 已被 drain
    unread = bb.inbox_list(project["id"], sid, unread_only=True)
    assert all(r["kind"] != "human_note" for r in unread)
    # 任务轮互斥：对话轮没动 dispatcher.current_task_id / 不产生任务事件
    assert agent.dispatcher.current_task_id is None
    # finish 不在对话轮工具集
    names = [t["name"] for t in CHAT_TOOLS]
    assert "finish" not in names and "run_cmd" in names


def test_run_chat_aborts_and_skips_when_not_idle(env):
    """对话轮 guard：暂停/中断置位时不消费引导（留收件箱）；对话中被中断则
    放弃回复（drain 已消费）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([{"text": "不该被消费"}])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    # 暂停态：不 drain、不回复
    agent.request_pause()
    bb.post_human_note(project["id"], sid, "等恢复")
    assert agent.run_chat() is None
    assert [r["kind"] for r in bb.inbox_list(project["id"], sid, unread_only=True)] == ["human_note"]
    agent.paused = False
    agent._pause_req.clear()
    # 对话中中断：置位 abort → 回复丢弃、标志被清
    orig_chat = llm.chat

    def chat(messages, **kw):
        agent.request_abort()
        return orig_chat(messages, **kw)

    llm.chat = chat
    assert agent.run_chat() is None
    assert not agent._abort_req.is_set()
    assert not [e for e in bb.recent_events(project["id"]) if e["kind"] == "agent.chat"]


def test_run_chat_streams_reply_and_prunes_deltas(env):
    """对话化（2026-09-20）：run_chat 流式回复——ScriptedLLM 回放 text_deltas，
    主线程节流落 agent.chat.delta（累计全文、seq 递增、同 stream_id）；终稿
    agent.chat 带同 stream_id，落库后同流 delta 行被清剪（审计只留终稿）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"text_deltas": ["甲" * 200, "乙" * 200], "text": "甲" * 200 + "乙" * 200},
    ])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    bb.post_human_note(project["id"], sid, "说说思路")
    # 旁路记录 delta（终稿后会被清剪，只能落库瞬间抓）
    seen: list[tuple[str, dict]] = []
    orig_append = bb.append_event

    def append(pid, kind, payload, **kw):
        seen.append((kind, dict(payload)))
        return orig_append(pid, kind, payload, **kw)

    bb.append_event = append
    reply = agent.run_chat()
    assert reply and "甲" in reply and "乙" in reply
    deltas = [p for k, p in seen if k == "agent.chat.delta"]
    assert len(deltas) >= 2  # 两段各 200 字 ≥80 阈值，0.5s 间隔必各落一拍
    seqs = [d["seq"] for d in deltas]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # 严格递增
    assert len({d["stream_id"] for d in deltas}) == 1            # 同流
    assert deltas[-1]["text"].startswith(deltas[0]["text"])       # 累计全文
    assert len(deltas[-1]["text"]) > len(deltas[0]["text"])
    finals = [p for k, p in seen if k == "agent.chat"]
    assert finals and finals[-1].get("stream_id") == deltas[0]["stream_id"]
    # 终稿落库后清剪：事件表不再有该流 delta 行
    assert not [e for e in bb.recent_events(project["id"], limit=500)
                if e["kind"] == "agent.chat.delta"]


def test_run_chat_registers_finding(env):
    """对话轮登记发现同样走意图纪律（2026-09-28 三段式收紧统一生效含对话轮）：
    declare_intent → 执行动作 → bb_add_finding——发现是检验的产物，不允许
    游离登记（2026-09-27 的对话轮豁免已被三段式收紧移除，declare_intent 恒
    放行不会死锁）。author=会话 id（P4 图上「对话产出」徽章数据源），
    无任务上下文不挂任务。"""
    bb, project, gw, _ = env
    aid = bb.upsert_asset(project["id"], "host", "admin.example.com")["id"]
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "c1", "declare_intent",
            {"statement": "后台存在默认弱口令 admin/admin（人类引导线索，"
                          "需实测登录验证）",
             "target_asset_id": aid})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "c2", "run_cmd", {"cmd": "whoami", "runtime": "host",
                              "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "c3", "bb_add_finding",
            {"vuln_class": "weak_password", "title": "后台弱口令 admin/admin",
             "severity": "high", "evidence": {"note": "登录成功且返回管理面板"}})]},
        {"text": "已确认后台存在弱口令并登记 finding。"},
    ])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    bb.post_human_note(project["id"], sid, "试试 admin/admin 能不能登后台")
    reply = agent.run_chat()
    assert "弱口令" in reply
    fs = bb.list_findings(project["id"])
    assert len(fs) == 1
    assert fs[0]["author"] == sid
    assert fs[0]["title"] == "后台弱口令 admin/admin"
    # prompt 纪律断言（沿用首轮 system）
    sys = llm.calls[0]["system"]
    assert "不登记发现" not in sys
    assert "bb_add_finding" in sys


def test_bb_add_finding_intent_requires_execution(env):
    """2026-09-28 意图先行（三段式收紧）：发现是「侦察→declare→执行→产出」链路
    的检验产物——① 对话轮（无任务上下文）同样受门禁约束；② declare 后立即落发现
    （事后补票，实测 sess-b9a539e3ebfe declare→finding 仅隔 12 秒）被拒；③ 意图
    声明后有实质执行（command 事件）才放行；declare_intent 恒放行不会死锁。"""
    bb, project, gw, _ = env
    from core.agent.tools import ToolDispatcher
    sid = bb.register_session(project["id"], "intent-exec")["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid)
    aid = bb.upsert_asset(project["id"], "url", "http://x/admin")["id"]
    # ① 对话轮（无 current_task_id）无意图 → 拒（统一门禁，不再豁免）
    r = d.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T"})
    assert r.startswith("[拒绝]") and "open 意图" in r
    # ② declare 后立即落发现 = 事后补票 → 拒
    r = d.dispatch("declare_intent", {"statement": "验证 /admin 是否存在未授权访问",
                                      "target_asset_id": aid})
    assert r.startswith("intent=")
    r = d.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T"})
    assert r.startswith("[拒绝]") and "执行动作" in r
    # ③ 意图声明后有实质执行（command 事件，run_cmd 落的审计）→ 放行
    bb.append_event(project["id"], "command", {"cmd": "curl -s http://x/admin"},
                    session_id=sid, author=sid)
    r = d.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T"})
    assert r.startswith("finding=")


def test_declare_intent_requires_asset_anchor(env):
    """2026-10-01：declare_intent 硬门禁——必须有资产锚点（target_asset_id 或
    basis_refs 含 asset:<id>），否则游离意图落不到链路图子目标下、无法背书
    tested_clean，直接拒绝。"""
    bb, project, gw, _ = env
    from core.agent.tools import ToolDispatcher
    sid = bb.register_session(project["id"], "intent-anchor")["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid)
    aid = bb.upsert_asset(project["id"], "host", "10.0.0.5")["id"]
    # 无锚点 → 拒
    r = d.dispatch("declare_intent", {"statement": "对目标进行探测"})
    assert r.startswith("[拒绝]") and "资产锚点" in r
    # target_asset_id 锚点 → 放行
    r = d.dispatch("declare_intent", {"statement": "假设一", "target_asset_id": aid})
    assert r.startswith("intent=")
    # 仅 basis_refs 的 asset: 锚点 → 放行
    r = d.dispatch("declare_intent", {"statement": "假设二",
                                      "basis_refs": [f"asset:{aid}"]})
    assert r.startswith("intent=")
    # 只有 finding: 依据但无 asset: 锚点 → 仍拒（finding 不是资产锚点）
    r = d.dispatch("declare_intent", {"statement": "假设三",
                                      "basis_refs": ["finding:find-xxx"]})
    assert r.startswith("[拒绝]") and "资产锚点" in r


def test_intent_lead_gate_blocks_then_releases(env):
    """意图先行闸（口径 Y，2026-10-01）：会话第一次实质动作前必须有 open 意图——
    无意图时 run_cmd 被 [拒绝] 拦下并指路 declare_intent；只读侦察不受阻；
    declare_intent 之后实质动作放行。"""
    bb, project, gw, _ = env
    from core.agent.tools import ToolDispatcher
    sid = bb.register_session(project["id"], "lead-gate")["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid)
    aid = bb.upsert_asset(project["id"], "host", "10.0.0.11")["id"]
    # ① 无 open 意图 → 实质动作被拦，且指路 declare_intent
    r = d.dispatch("run_cmd", {"cmd": "whoami", "runtime": "host",
                               "threat_class": "trusted"})
    assert r.startswith("[拒绝]") and "意图先行闸" in r and "declare_intent" in r
    assert not [e for e in bb.recent_events(project["id"]) if e["kind"] == "command"]
    # ② 只读侦察不受本闸限制（不落 [拒绝]）
    assert "意图先行闸" not in d.dispatch("list_symbols", {"binary": "x.exe"})
    assert "意图先行闸" not in d.dispatch("read_file", {"path": "x"})
    # ③ 先立意（资产锚点）→ 实质动作放行
    r = d.dispatch("declare_intent", {"statement": "对 10.0.0.11 进行探测",
                                      "target_asset_id": aid})
    assert r.startswith("intent=")
    r = d.dispatch("run_cmd", {"cmd": "whoami", "runtime": "host",
                               "threat_class": "trusted"})
    assert "意图先行闸" not in r
    assert "command" in [e["kind"] for e in bb.recent_events(project["id"])]


def test_intent_lead_gate_allows_coordination_and_asset(env):
    """放行面：多代理协调（bb_notify）、授权申请、资产登记
    （declare_intent 锚点前置）不经本闸——否则编排/立意两处都会死锁。"""
    bb, project, gw, _ = env
    from core.agent.tools import ToolDispatcher
    sid = bb.register_session(project["id"], "lead-allow")["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid)
    assert "意图先行闸" not in d.dispatch("bb_add_asset", {"value": "10.0.0.12"})
    r = d.dispatch("request_authorization", {
        "kind": "scope_expand", "scope_request": "追加 target.com 子域",
        "justification": "需要更广授权面"})
    assert "意图先行闸" not in r
    assert "意图先行闸" not in d.dispatch(
        "bb_notify", {"text": "集合", "kind": "note"})


def test_run_chat_step_cap(env):
    """对话轮步数上限：min(dispatcher.max_steps, config.chat_max_steps)——
    chat_max_steps=3 时最多 3 次 LLM 调用即退出（剧本 3 轮工具调用，若上限
    失效第 4 次调用会 pop 空剧本直接崩测试）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("c1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("c2", "run_cmd",
                                            {"cmd": "c2", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("c3", "run_cmd",
                                            {"cmd": "c3", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(chat_max_steps=3))
    sid = agent.session["id"]
    bb.post_human_note(project["id"], sid, "随便聊")
    assert agent.run_chat() is None  # 上限内没给出文字回复 → 空退
    assert len(llm.calls) == 3


def test_run_chat_session_history_persists(env):
    """对话化（2026-09-20）：非绑定窗对话历史存 chat-<sid>.json——第一轮问答
    回写文件，第二轮载入上下文（模型看得到上轮问答），跨轮、跨重启连续。"""
    bb, project, gw, tmp_path = env
    artifacts = tmp_path / "proj" / "artifacts"
    llm = ScriptedLLM([
        {"text": "第一轮：目标站是WordPress。"},
        {"text": "接着上轮：WordPress 建议跑 wpscan。"},
    ])
    agent = make_agent(env, llm, artifacts_dir=artifacts)
    sid = agent.session["id"]
    bb.post_human_note(project["id"], sid, "识别下CMS")
    assert agent.run_chat() == "第一轮：目标站是WordPress。"
    cpath = artifacts.parent / "snapshots" / f"chat-{sid}.json"
    assert cpath.exists()
    st = json.loads(cpath.read_text(encoding="utf-8"))
    assert st["session_id"] == sid and len(st["messages"]) == 2
    bb.post_human_note(project["id"], sid, "那下一步呢")
    assert agent.run_chat() == "接着上轮：WordPress 建议跑 wpscan。"
    # 第二轮首轮 messages 含上轮问答全文
    first = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "识别下CMS" in first and "目标站是WordPress" in first


def test_snapshot_tail_sanitize():
    """v0.64 快照尾部 sanitize：半步悬空 tool_use/tool_result 被截到上一完整边界。"""
    tool_use_a = {"type": "tool_use", "id": "a", "name": "run_cmd", "input": {}}
    tool_use_b = {"type": "tool_use", "id": "b", "name": "run_cmd", "input": {}}
    tool_use_c = {"type": "tool_use", "id": "c", "name": "run_cmd", "input": {}}
    result = lambda i: {"role": "user", "content": [  # noqa: E731
        {"type": "tool_result", "tool_use_id": i, "content": "ok"}]}
    base = [
        {"role": "user", "content": "目标"},
        {"role": "assistant", "content": [tool_use_a]},
        result("a"),
    ]
    # 完整对不动
    out = sanitize_snapshot_tail(base + [
        {"role": "assistant", "content": [tool_use_b]}, result("b")])
    assert out[-1] == result("b") and len(out) == 5
    # 半步：assistant(tool_use b,c) 后只落了 b 的 result → 截到 result("a") 为止
    out = sanitize_snapshot_tail(base + [
        {"role": "assistant", "content": [tool_use_b, tool_use_c]}, result("b")])
    assert len(out) == 3 and out[-1] == result("a")
    # 纯文本尾不动
    out = sanitize_snapshot_tail(base + [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}])
    assert len(out) == 4


def test_live_state_cleared_after_run(env):
    """_loop 退出（含正常收尾）必清 _live_state，防陈旧现场被快照。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call(
        "t1", "finish", {"summary": "完"})]}])
    agent = make_agent(env, llm)
    assert run_objective(agent, "现场清理") == "完"
    assert agent._live_state is None


def _dispatcher(env, name="assets"):
    from core.agent.tools import ToolDispatcher
    bb, project, gw, _ = env
    sid = bb.register_session(project["id"], name)["id"]
    return ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                          session_id=sid, author=sid)


def test_bb_add_asset_unified_entry_and_dedup_hint(env):
    """E6：bb_add_asset 走 register_asset 统一入口——类型自动识别、url 含 IP
    自动挂 host、重报合并并回执防重扫提示；识别不出拒收回填。"""
    d = _dispatcher(env)
    r = d.dispatch("bb_add_asset", {"value": "https://10.9.9.9/login"})
    assert "type=url" in r and "created=True" in r and "host=" in r
    aid = r.split("asset=")[1].split()[0]

    r2 = d.dispatch("bb_add_asset", {"value": "https://10.9.9.9/login",
                                     "meta": {"title": "登录页"}})
    assert f"asset={aid}" in r2 and "created=False" in r2
    assert "命中既有资产" in r2 and "bb_query" in r2      # E6 ⑥ 防重扫提示

    r3 = d.dispatch("bb_add_asset", {"value": "怪值无类型"})
    assert r3.startswith("[错误]") and "手选" in r3


def test_bb_delete_and_merge_asset_tools(env):
    """黑板资产删除/合并工具走项目边界并返回迁移回执。"""
    d = _dispatcher(env, "asset-maintenance")
    target = d.dispatch("bb_add_asset", {"type": "host", "value": "10.30.0.1"})
    source = d.dispatch("bb_add_asset", {"type": "domain", "value": "app.internal"})
    target_id = target.split("asset=")[1].split()[0]
    source_id = source.split("asset=")[1].split()[0]

    assert d.dispatch("bb_delete_asset", {"asset_id": target_id}).startswith("asset.deleted=")
    # 源仍可操作；重新登记目标后验证合并回执与别名删除。
    target = d.dispatch("bb_add_asset", {"type": "host", "value": "10.30.0.1"})
    target_id = target.split("asset=")[1].split()[0]
    merged = d.dispatch("bb_merge_assets", {
        "source_asset_id": source_id,
        "target_asset_id": target_id,
        "reason": "同一内部服务的域名与解析主机",
    })
    assert merged.startswith("asset.merged ")
    assert d.bb.get_asset(source_id) is None
    assert d.bb.get_asset(target_id)["meta"]["aliases"]

    binary_id = d.bb.upsert_asset(d.project_id, "binary", "c" * 64)["id"]
    assert d.dispatch("bb_delete_asset", {"asset_id": binary_id}).startswith("[拒绝]")


def test_bb_add_asset_domain_dns_mount(env, monkeypatch):
    """E6 ③⑤：domain 由平台自动 DNS 解析挂 host（Agent 侧无需显式传 parent_id）。"""
    from core.blackboard import assets as am
    monkeypatch.setattr(am.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.10.10.10", 0))])
    d = _dispatcher(env, "dns")
    r = d.dispatch("bb_add_asset", {"value": "app.corp.example"})
    assert "type=domain" in r and "host=" in r
    # 同 IP 第二个域名复用 host 并命中既有资产提示
    r2 = d.dispatch("bb_add_asset", {"value": "dev.corp.example"})
    assert "命中既有资产" in r2


def test_bb_asset_status_tool_and_query_filters(env):
    """E7：bb_asset_status 流转（tested_clean 必带 note）；bb_query assets
    增 status/type 过滤并返回 status。"""
    d = _dispatcher(env, "status")
    pass_intent_lead(d)  # 本测主题=资产状态流转，跳过意图先行闸
    r = d.dispatch("bb_add_asset", {"value": "10.2.2.2"})
    aid = r.split("asset=")[1].split()[0]

    assert d.dispatch("bb_asset_status",
                      {"asset_id": aid, "status": "tested_clean"}).startswith("[拒绝]")
    # 四问门禁（2026-09-29）：tested_clean 缺任一问 → 拒绝并引导补答
    r4q = d.dispatch("bb_asset_status",
                     {"asset_id": aid, "status": "tested_clean",
                      "tested_what": "测了 4 个入口", "viewpoint": "docker 出口"})
    assert r4q.startswith("[拒绝]") and "why_no_finding" in r4q
    assert d.dispatch("bb_asset_status",
                      {"asset_id": aid, "status": "hacked"}).startswith("[拒绝]")
    assert d.dispatch("bb_asset_status",
                      {"asset_id": "asset-000000000000", "status": "visited"}).startswith("[错误]")
    for st in ("visited", "scanning"):
        assert d.dispatch("bb_asset_status", {"asset_id": aid, "status": st}).startswith("asset=")
    # tested_clean 须死路意图背书（2026-09-25 门禁；2026-09-29 收紧为逐资产）
    from core.blackboard.intents import close_intent, declare_intent
    hid = d.bb.add_http_history(d.project_id, source="browser", method="GET",
                                url="http://10.2.2.2/probe", status=404,
                                resp_body="")
    iid = declare_intent(d.bb, d.project_id, "该主机无洞假设",
                         target_asset_id=aid)["id"]
    close_intent(d.bb, d.project_id, iid, "dead_end",
                 dead_reason="4 个入口探测均无异常",
                 evidence_refs=[f"http:{hid}"])
    assert d.dispatch("bb_asset_status",
                      {"asset_id": aid, "status": "tested_clean",
                       "note": "手测 4 个入口",
                       "tested_what": "GET / /login /admin /api 各 1 发，404/200",
                       "viewpoint": "docker 出口直连，无拦截页",
                       "why_no_finding": "入口全探、无异常响应；暂无可立新意图"
                       }).startswith("asset=")
    # 无背书的另一资产仍被拒（四问齐备但意图不围绕该资产）
    no_backing = d.dispatch("bb_add_asset", {"value": "10.2.2.3"})
    nid = no_backing.split("asset=")[1].split()[0]
    assert d.dispatch("bb_asset_status",
                      {"asset_id": nid, "status": "tested_clean",
                       "note": "无背书",
                       "tested_what": "GET / 200", "viewpoint": "docker 出口",
                       "why_no_finding": "首页正常"}).startswith("[拒绝]")
    ev = [e for e in d.bb.recent_events(d.project_id)
          if e["kind"] == "asset.status_changed"]
    assert [e["payload"]["new"] for e in ev] == ["visited", "scanning", "tested_clean"]

    q = json.loads(d.dispatch("bb_query", {"what": "assets", "status": "tested_clean"}))
    assert [a["id"] for a in q["assets"]] == [aid]
    assert q["assets"][0]["status"] == "tested_clean"
    q2 = json.loads(d.dispatch("bb_query", {"what": "assets", "type": "host", "status": "open"}))
    assert all(a["type"] == "host" and a["status"] == "open" for a in q2["assets"])


def _kb_env(tmp_path):
    """给 packs/kb/web 配 kb 源（中文 H1 + frontmatter title + 首行摘要）。"""
    kb = tmp_path / "packs" / "kb" / "web"
    (kb / "poc").mkdir(parents=True, exist_ok=True)
    (kb / "poc" / "越权检测.md").write_text(
        "# 越权检测方法\n本篇汇总越权检测的完整路径。\n"
        "X9BODY_MARKER 完整正文细节只随 kb_open 注入", encoding="utf-8")
    (kb / "poc" / "tduck.md").write_text(
        "---\ntitle: 问卷系统越权合集\n---\n正文内容", encoding="utf-8")


def test_skill_context_uses_self_contained_skills(env):
    """cc 风格：注入技能目录清单，不把全局 packs/kb 作为默认上下文。"""
    bb, project, gw, tmp_path = env
    _kb_env(tmp_path)
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("t1", "finish",
                                                           {"summary": "完"})]}])
    agent = make_agent(env, llm)
    run_objective(agent, "整理资产清单")  # generalist：注入全部启用技能描述
    system = llm.calls[0]["system"]
    assert "可用技能清单" in system and "- demo：" in system
    assert "可用知识库源" not in system
    assert "skill_open" in system
    routed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "skill.routed"]
    assert routed and routed[-1]["payload"]["name"] is None
    assert "demo" in routed[-1]["payload"]["injected"]
    assert "kb_hits" not in routed[-1]["payload"]


def test_kb_module_hint_lines(env):
    """cc 风格：不注入全局知识库提示或源清单；旧资料仍可主动检索。"""
    bb, project, gw, tmp_path = env
    _kb_env(tmp_path)
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("t1", "finish",
                                                           {"summary": "完"})]}])
    agent = make_agent(env, llm)
    run_objective(agent, "排查问卷系统越权问题")
    system = llm.calls[0]["system"]
    assert "📚 相关知识库模块" not in system
    assert "可用知识库源" not in system
    routed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "skill.routed"]
    assert "kb_hits" not in routed[-1]["payload"]
    assert "injected" in routed[-1]["payload"]


def test_kb_search_tool(env):
    """kb_search：多源全文检索 → path 即 kb_open module（M0 全局形态带域前缀）；落 kb.search 审计；空/无命中分支。"""
    bb, project, gw, tmp_path = env
    _kb_env(tmp_path)
    llm = ScriptedLLM([])
    agent = make_agent(env, llm)
    d = agent.dispatcher
    r = d.dispatch("kb_search", {"query": "越权"})
    assert "web/poc/tduck.md" in r and "kb_open 的 module 参数" in r
    assert "kb.search" in [e["kind"] for e in bb.recent_events(project["id"])]
    assert "[拒绝]" in d.dispatch("kb_search", {"query": "  "})
    assert "[无命中]" in d.dispatch("kb_search", {"query": "不存在的专题xyz"})


_INDEX_YAML = (
    "entries:\n"
    "  - point: 文件上传测试\n"
    "    match: [文件上传, upload]\n"
    "    kb: web/poc/文件上传.md\n"
    "    tags: [demo]\n"
    "  - point: 越权检测\n"
    "    kb: web/poc/越权.md\n"
)


def _write_web_pack(env, index_yaml=_INDEX_YAML):
    """tmp packs 里建 web 域 kb 源与全局 route_index.yaml（M0 单表）。"""
    _, _, _, tmp_path = env
    (tmp_path / "packs" / "capabilities" / "web").mkdir(parents=True, exist_ok=True)
    kb = tmp_path / "packs" / "kb" / "web"
    (kb / "poc").mkdir(parents=True, exist_ok=True)
    (kb / "poc" / "文件上传.md").write_text(
        "# 文件上传测试手册\n先测后缀黑白名单，再测解析漏洞。\n", encoding="utf-8")
    (kb / "poc" / "越权.md").write_text(
        "# 越权检测\n换 id 做差分对比。\n", encoding="utf-8")
    if index_yaml is not None:
        (tmp_path / "packs" / "kb" / "route_index.yaml").write_text(
            index_yaml, encoding="utf-8")
    return kb


def test_skill_context_no_route_index_inject(env):
    """K8（2026-09-29）：不再注入 📖 测试点索引/route_lookup 指引——kb 靠
    模型主动检索；技能全量描述按角色白名单裁剪（交集空 → 无可用技能）。"""
    _write_web_pack(env)
    agent = make_agent(env, ScriptedLLM([]))  # _generalist：skills=null 全量
    ctx = agent.skill_context_for("处理文件上传")
    assert "📖 测试点手册索引" not in ctx
    assert "route_lookup" not in ctx
    assert "可用知识库源" not in ctx
    assert "- demo：" in ctx  # generalist 注入全部启用技能描述
    # 白名单角色：只注入白名单内技能
    skill_dir = env[3] / "packs" / "capabilities" / "web" / "skills" / "up"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: up\ndescription: 上传测试\n---\n步骤。", encoding="utf-8")
    write_role(env, "strike", 'name: 打点\nskills: [demo]\n')
    agent2 = make_agent(env, ScriptedLLM([]), role="strike")
    ctx2 = agent2.skill_context_for("处理文件上传")
    assert "- demo：" in ctx2
    assert "- up：" not in ctx2  # 白名单外技能不注入
    # 白名单交集为空 → 无可用技能
    write_role(env, "recon", 'name: 侦察\nskills: [别的技能]\n')
    agent3 = make_agent(env, ScriptedLLM([]), role="recon")
    ctx3 = agent3.skill_context_for("处理文件上传")
    assert "（当前无可用技能）" in ctx3
    assert "可用知识库源" not in ctx3


def test_route_lookup_and_skill_open_tools(env):
    """G1 只读查询工具：route_lookup 按关键词查索引（角色裁剪同口径）；
    skill_open 打开技能全量正文；未知名回可用清单防幻觉。"""
    _write_web_pack(env)
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    out = d.dispatch("route_lookup", {"query": "文件上传"})
    assert "文件上传测试" in out and "kb_open" in out  # legacy route index remains compatible
    assert d.dispatch("route_lookup", {"query": "zzz无关键词"}) .startswith("[无命中]")
    assert d.dispatch("route_lookup", {"query": ""}).startswith("[拒绝]")
    out_skill = d.dispatch("skill_open", {"name": "demo"})  # 轨级 demo 技能
    assert "按步骤执行" in out_skill
    resource = env[3] / "packs" / "tracks" / "pentest" / "skills" / "demo" / "references"
    resource.mkdir()
    (resource / "method.md").write_text("自包含方法正文", encoding="utf-8")
    out_resource = d.dispatch("skill_open", {"name": "demo", "path": "references/method.md"})
    assert "自包含方法正文" in out_resource
    assert d.dispatch("skill_open", {"name": "demo", "path": "../SKILL.md"}).startswith("[错误]")
    assert d.dispatch("skill_open", {"name": "nope"}).startswith("[防幻觉]")
    bb0, project0 = env[0], env[1]
    evs = [e["kind"] for e in bb0.recent_events(project0["id"])]
    # skill.open 审计事件落了（route_lookup 走 tool.call 通用审计）
    assert "skill.open" in evs


def test_skill_context_digest_not_full_body(env):
    """K8 渐进披露：全量描述注入只给 name+description 行，技能正文不整段进
    system——正文靠 skill_open 按需打开。"""
    _write_web_pack(env)
    skill_dir = env[3] / "packs" / "capabilities" / "web" / "skills" / "up"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: up\ndescription: 上传测试\nkeywords: 上传\n---\n"
        "## 适用场景\n上传点打测。\n## 步骤\n先黑白名单。\n## 坑\n解析漏洞别漏。\n"
        + "正文密度填充。" * 40, encoding="utf-8")
    agent = make_agent(env, ScriptedLLM([]))
    ctx = agent.skill_context_for("上传测试怎么打")
    assert "- up：上传测试" in ctx          # 全量描述注入含 up（name+description）
    assert "先黑白名单" not in ctx           # 正文不整段注入
    assert "正文密度填充" not in ctx
    assert "skill_open" in ctx             # 渐进披露指针


def test_kb_recall_for_advisor(env):
    """卡壳召回：对话尾部命中手册 → 摘要注入；不相关/无 kb → 空串静默。"""
    _write_web_pack(env)
    agent = make_agent(env, ScriptedLLM([]))
    recall = agent._kb_recall_for_advisor(
        [{"role": "user", "content": "发现了文件上传点，试了几种后缀都被拦"}])
    assert "知识库相关经验" in recall and "文件上传测试手册" in recall
    assert agent._kb_recall_for_advisor(
        [{"role": "user", "content": "zzz 无关内容"}]) == ""


def test_advisor_with_kb_recall_still_recovers(env):
    """召回接入顾问主链：stuck 触发时 planner 收到手册摘要，流程不断。"""
    _write_web_pack(env)
    planner = ScriptedLLM([{"text": "建议：按文件上传手册先测后缀黑白名单。"}])
    script = [{"tool_use": [ScriptedLLM.tool_call(
        f"q{i}", "bb_query", {"what": "events"})]} for i in range(3)]
    script.append({"tool_use": [ScriptedLLM.tool_call("t9", "finish", {"summary": "完成"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=15, stuck_after=3))
    run_objective(agent, "文件上传点卡住了")
    seen = json.dumps(planner.calls, ensure_ascii=False)
    assert "文件上传测试手册" in seen
    assert any("[策略顾问]" in json.dumps(c["messages"], ensure_ascii=False)
               for c in llm.calls)


def test_dispatch_spills_oversized_result(env):
    """超 spill 阈值（32000，2026-10-01 由 8000 放宽）的工具结果：全量落
    workspace/spill/，回填预览+定位器。"""
    bb, project, gw, tmp_path = env
    agent = make_agent(env, ScriptedLLM([]), artifacts_dir=tmp_path / "artifacts")
    d = agent.dispatcher
    pass_intent_lead(d)  # 本测主题=超限落盘，跳过意图先行闸
    d._tool_big = lambda **k: "X" * 40000  # 假工具绕过 run_cmd 的 gateway brief
    out = d.dispatch("big", {})
    assert "[结果超限已落盘]" in out and "[省略" in out
    assert "完整内容" in out and "../spill/" in out  # scratch 相对路径定位器
    files = list((tmp_path / "spill").glob("*-big-*.txt"))  # E1 uuid 后缀（同秒防互覆）
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "X" * 40000  # 落盘是全量
    # tool.call 审计事件照落，result_head 即预览（不吞全量）
    ev = [e for e in bb.recent_events(project["id"]) if e["kind"] == "tool.call"]
    assert ev and ev[-1]["payload"]["name"] == "big"


def test_dispatch_spill_skip_kb_open(env):
    """kb_open 豁免：正文即取用目的，再长也不 spill（kb_open/skill_open/
    route_lookup 同一豁免清单；run_cmd 自 2026-10-01 不再豁免）。"""
    bb, project, gw, tmp_path = env
    agent = make_agent(env, ScriptedLLM([]), artifacts_dir=tmp_path / "artifacts")
    d = agent.dispatcher
    d._tool_kb_open = lambda module: "Y" * 20000
    out = d.dispatch("kb_open", {"module": "x"})
    assert out == "Y" * 20000
    assert not (tmp_path / "spill").exists()


# ---------- H1：剪枝先行（借鉴 dsh pruning-before-summary） ----------

def test_prune_before_summary_mechanical_only(env):
    """老区超长 tool_result 机械剪（head 400 + tail 200 + 省略注脚）即可回到
    触发线时，不动用 LLM 摘要——llm.compact 落 summarized=0/pruned=N。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([{"text": "摘要调用不该发生"}])
    agent = make_agent(env, llm, config=AgentConfig(
        max_steps=5, context_summary_chars=10_000, context_char_budget=1_000_000))
    msgs: list[dict] = [{"role": "user", "content": "目标"}]
    for i in range(7):  # len=15，cut=15-8=7，old=前 7 条（含 i=0 的大结果）
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"c{i}", "name": "run_cmd", "input": {}}]})
        big = "R" * 30000 if i == 0 else "短结果"
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"c{i}", "content": big}]})
    assert agent._maybe_summarize(msgs) is True
    pruned_text = msgs[2]["content"][0]["content"]
    assert len(pruned_text) < 1000 and "省略 29400 字符" in pruned_text
    assert llm.calls == []  # 机械剪即达标，LLM 剧本未被消费
    compacts = [e for e in bb.recent_events(project["id"])
                if e["kind"] == "llm.compact"]
    assert len(compacts) == 1
    assert compacts[0]["payload"]["summarized"] == 0
    assert compacts[0]["payload"]["pruned"] == 1


# ---------- H3：request_escalation（deny-driven 一次性升级） ----------

def test_request_authorization_kinds_and_rejections(env):
    """三 kind 各建 high 审批单（op=authorization，payload 带 scope_request/
    justification/证据 finding/task/session）；非法 kind 与空必填项拒收。"""
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    for kind, scope in [("scope_expand", "追加 target.com 子域进授权范围"),
                        ("impact_escalate", "需要证明到接管管理员会话的影响级"),
                        ("rating_override", "按口径应评 critical，申请突破收录上限")]:
        out = d.dispatch("request_authorization", {
            "kind": kind, "scope_request": scope, "justification": "打不上去，证据见黑板",
            "evidence_finding_ids": [f"find-{i:012d}" for i in range(12)]})
        assert out.startswith("[已提交审批]") and f"authorization/{kind}" in out, out
    rows = bb.conn.execute(
        "SELECT * FROM approvals ORDER BY created_at").fetchall()
    assert len(rows) == 3
    kinds = []
    for row in rows:
        assert row["status"] == "pending" and row["risk"] == "high"
        assert row["session_id"] == agent.session["id"]
        action = json.loads(row["action"])
        assert action["op"] == "authorization"
        kinds.append(action["kind"])
        assert action["task_id"] is None and action["session_id"] == agent.session["id"]
        # 证据 finding cap 10
        assert len(action["evidence_finding_ids"]) == 10
    assert kinds == ["scope_expand", "impact_escalate", "rating_override"]
    # 拒收路径：非法 kind / 空 scope_request / 空 justification
    assert d.dispatch("request_authorization", {
        "kind": "wildcard", "scope_request": "x", "justification": "y"}).startswith("[拒绝]")
    assert d.dispatch("request_authorization", {
        "kind": "scope_expand", "scope_request": "  ", "justification": "y"}).startswith("[拒绝]")
    assert d.dispatch("request_authorization", {
        "kind": "scope_expand", "scope_request": "x", "justification": ""}).startswith("[拒绝]")
    assert bb.conn.execute("SELECT COUNT(*) c FROM approvals").fetchone()["c"] == 3


def test_add_finding_dedup_warning_text(env):
    """D1 警告进工具返回值：同目标同类不同指纹 → 返回串含 [疑似重复] 与旧条目 id。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    declare_intent(bb, project["id"], "对目标资产进行注入参数探测尝试",
                   author=agent.session["id"])  # 过 bb_add_finding 必挂意图门禁
    # 意图先行闸（2026-09-28）：补 command 事件模拟意图后的执行动作
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=agent.session["id"], author=agent.session["id"])
    a = bb.upsert_asset(project["id"], "domain", "a.com")["id"]
    d.dispatch("bb_add_finding", {"vuln_class": "sqli", "title": "id 注入",
                                  "severity": "low",
                                  "target_asset_id": a, "dedup_key": "k-time"})
    out = d.dispatch("bb_add_finding", {"vuln_class": "sqli", "title": "order 注入",
                                        "severity": "low",
                                        "target_asset_id": a, "dedup_key": "k-order"})
    assert "[疑似重复]" in out and "id 注入" in out
    old = next(f["id"] for f in bb.list_findings(project["id"]) if f["title"] == "id 注入")
    assert old in out


def test_bb_notify_tool_target_and_broadcast(env):
    """bb_notify（B1）：to_session 定向优先；to_role 广播活跃同角色窗（跳过自己
    与已关闭）；两者皆空/text 空/不存在/已关闭 → [错误]；统一落 tool.call 审计。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, _ = env
    s1 = bb.register_session(project["id"], "发信窗")["id"]
    peer = bb.register_session(project["id"], "收信窗")["id"]
    closed = bb.register_session(project["id"], "已关窗")["id"]
    bb.close_session(closed)
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=s1, author=s1)
    r = d.dispatch("bb_notify", {"to_session": peer, "kind": "intel",
                                 "text": "8080 有 swagger 未鉴权"})
    assert r.startswith("已送达"), r
    rows = bb.inbox_list(project["id"], peer)
    assert rows and rows[0]["kind"] == "agent_message"
    assert rows[0]["payload"]["text"] == "8080 有 swagger 未鉴权"
    assert rows[0]["payload"]["from"] == s1
    # to_role 广播：活跃同角色送达（closed 查询即滤掉、自己循环内跳过不计数）
    r = d.dispatch("bb_notify", {"to_role": "_generalist", "kind": "assist",
                                 "text": "谁有靶机出口权限？"})
    assert "已广播" in r and "送达 1 窗" in r, r
    last = bb.inbox_list(project["id"], peer)[-1]["payload"]
    assert last["subkind"] == "assist" and last["from"] == s1
    assert bb.inbox_list(project["id"], s1) == []  # 自己不收
    assert bb.inbox_list(project["id"], closed) == []  # 已关闭不投
    # 错误路径
    assert d.dispatch("bb_notify", {"text": "   "}).startswith("[错误]")
    assert d.dispatch("bb_notify", {"text": "x"}).startswith("[错误]")
    assert d.dispatch("bb_notify", {"to_session": "sess-nope", "text": "x"}).startswith("[错误]")
    assert d.dispatch("bb_notify", {"to_session": closed, "text": "x"}).startswith("[错误]")
    assert d.dispatch("bb_notify", {"to_role": "no-role", "text": "x"}).startswith("[错误]")
    # 审计：dispatch 统一落 tool.call（payload.name=工具名）
    assert any(e["kind"] == "tool.call" and e["payload"].get("name") == "bb_notify"
               for e in bb.recent_events(project["id"]))


def test_agent_message_injected_at_step_boundary(env):
    """agent_message 注入（P3.4）：执行中途到达的私信在**步边界**信息式注入
    （与 human_note 轮末语义不同——私信与现场相关）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "本轮完成"})]},
    ])
    agent = make_agent(env, llm)
    pass_intent_lead(agent.dispatcher)  # 本测主题=私信步边界注入，跳过意图先行闸
    peer = bb.register_session(project["id"], "peer")["id"]
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:  # 第 1 步期间投递私信 → 第 2 步边界注入
            bb.post_agent_message(project["id"], peer, agent.session["id"],
                                  "intel", "8080 有 swagger 未鉴权")
        return r
    llm.chat = chat

    assert run_objective(agent, "私信演练") == "本轮完成"
    second = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "📨 Agent 私信" in second and "swagger" in second


def test_run_chat_consumes_agent_message(env):
    """对话轮消费私信（P3.4）：run_chat only_kinds 扩 4 kind——agent_message 注入
    对话轮（含 subkind 中文标注），回复后收件箱清空。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([{"text": "收到，8080 的 swagger 我这就复核。"}])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    peer = bb.register_session(project["id"], "peer")["id"]
    bb.post_agent_message(project["id"], peer, sid, "intel", "8080 有 swagger 未鉴权")
    out = agent.run_chat()
    assert out and "复核" in out
    first = json.dumps(llm.calls[0]["messages"][0], ensure_ascii=False)
    assert "📨 Agent 私信" in first and "情报" in first and "swagger" in first
    assert bb.inbox_list(project["id"], sid, unread_only=True) == []


def test_unknown_inbox_kind_not_swallowed(env):
    """未登记 kind 绝不被静默吞（订阅声明化）：消费点 only_kinds 从订阅表构造
    ——未登记 kind 经步边界后仍滞留收件箱未读（红点可见，提示先登记）。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完成"})]},
    ])
    agent = make_agent(env, llm)
    pass_intent_lead(agent.dispatcher)  # 本测主题=未登记 kind 不吞，跳过意图先行闸
    sid = agent.session["id"]
    bb.inbox_post(project["id"], sid, "future_kind", "ref-x", {"text": "未来私信"})
    assert run_objective(agent, "未知kind演练") == "完成"
    unread = bb.inbox_list(project["id"], sid, unread_only=True)
    assert [r["kind"] for r in unread] == ["future_kind"]


def test_interrupted_reply_flushed_in_chat(env):
    """中断落盘（对话轮）：流式回复中途 abort——半截回复落 agent.chat 终稿
    （interrupted=true + stream_id，无 step），run_chat 返 None，turn 有收口回复。"""
    bb, project, gw, _ = env
    llm = ScriptedLLM([
        {"text_deltas": ["分析", "结论一半"], "text": "分析结论一半"},
    ])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    bb.post_human_note(project["id"], sid, "说说思路")
    orig_chat = llm.chat
    fired = {"v": False}

    def chat(messages, **kw):
        on_text = kw.get("on_text")
        if on_text is not None:
            def wrapped(d):
                on_text(d)
                if not fired["v"]:
                    fired["v"] = True
                    agent.request_abort()   # 首个 text delta 后置中断
            kw["on_text"] = wrapped
        return orig_chat(messages, **kw)
    llm.chat = chat

    assert agent.run_chat() is None
    finals = [e for e in bb.recent_events(project["id"]) if e["kind"] == "agent.chat"]
    assert len(finals) == 1
    p = finals[0]["payload"]
    assert p["interrupted"] is True and "分析" in p["text"]
    assert p.get("step") is None and p.get("stream_id")
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.chat.delta"]  # delta 已清剪（镜像成功路径）


def test_blueprint_tools_create_update_and_status_guard(env):
    """bb_blueprint_create/update 全链路：建骨架 → 模块写回 → 整表/正文；
    蓝图整体 status 不暴露给 Agent（拒绝引导走人类）。"""
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pass_intent_lead(d)  # 本测主题=蓝图工具，跳过意图先行闸
    out = d.dispatch("bb_blueprint_create", {
        "name": "聊天客户端", "goal": "重建客户端",
        "binary_sha256": "abc123",
        "modules": [{"name": "网络通信", "desc": "TCP",
                     "func_addresses": ["0x401000"]},
                    {"name": "加密校验", "desc": "AES"}]})
    assert out.startswith("blueprint=") and "网络通信" in out
    bid = out.split("blueprint=")[1].split()[0]
    out = d.dispatch("bb_blueprint_update", {
        "blueprint_id": bid, "module_name": "网络通信",
        "spec": "def send(pkt): ...", "notes": "心跳 30s",
        "module_status": "specd"})
    assert "updated" in out
    bp = bb.get_blueprint(project["id"], bid)
    mod = next(m for m in bp["modules"] if m["name"] == "网络通信")
    assert mod["spec"] == "def send(pkt): ..." and mod["status"] == "specd"
    # 正文追加（汇总任务用）
    out = d.dispatch("bb_blueprint_update", {
        "blueprint_id": bid, "content_append": "## 数据流\n客户端→服务端 AES 包"})
    assert "updated" in out
    assert "AES" in bb.get_blueprint(project["id"], bid)["content_md"]
    # 未知模块 → [错误]
    out = d.dispatch("bb_blueprint_update", {
        "blueprint_id": bid, "module_name": "没有的模块", "notes": "x"})
    assert out.startswith("[错误]") and "模块不存在" in out
    # 整体 status 不可由 Agent 直改
    out = d.dispatch("bb_blueprint_update", {
        "blueprint_id": bid, "module_status": "tested"})
    assert out.startswith("[拒绝]")
    # 整体流转白名单兜底：Agent 侧无从触达（工具无 status 入口），
    # 人类侧经 store 直改验证白名单仍成立
    with pytest.raises(ValueError, match="不可从 draft"):
        bb.set_blueprint_status(project["id"], bid, "building")
    # 重名蓝图拒收（同项目同样本同名）
    out = d.dispatch("bb_blueprint_create",
                     {"name": "聊天客户端", "binary_sha256": "abc123"})
    assert out.startswith("[错误]") and "重名" in out


def test_bb_query_blueprint(env):
    """bb_query what=blueprint：列表给摘要（模块名/状态/地址），单份给全文。"""
    bb, project, gw, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    bp = bb.create_blueprint(project["id"], "查询样例", modules=[
        {"name": "许可校验", "func_addresses": ["0x401000"]}])
    out = d.dispatch("bb_query", {"what": "blueprint"})
    rows = json.loads(out)
    assert rows[0]["id"] == bp["id"] and rows[0]["modules"][0]["name"] == "许可校验"
    out = d.dispatch("bb_query", {"what": "blueprint", "blueprint_id": bp["id"]})
    full = json.loads(out)
    assert full["content_md"] == "" and full["goal"] == ""
    out = d.dispatch("bb_query", {"what": "blueprint", "blueprint_id": "bp-nope"})
    assert out.startswith("[错误]")


# ---------- read_file 工具（2026-09-20：只读工作区文件，host 原生直读） ----------

def _plan_dispatcher(env, session_name="planner", **kw):
    """任务机制退役后的裸分发器夹具：建会话 + ToolDispatcher，旁路意图先行闸
    （本组用例聚焦各自主题，不被意图纪律挡回）。返回 (dispatcher, session_id)。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, _ = env
    sid = bb.register_session(project["id"], session_name)["id"]
    d = ToolDispatcher(bb, gateway=gw, project_id=project["id"],
                       session_id=sid, author=sid, **kw)
    pass_intent_lead(d)
    return d, sid


def _read_dispatcher(env):
    bb, project, gw, tmp_path = env
    artifacts = tmp_path / "ws" / "artifacts"
    scratch = tmp_path / "ws" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    d, _sid = _plan_dispatcher(env, session_name="reader", artifacts_dir=artifacts)
    return d, scratch, artifacts


def test_read_file_lines_and_windows(env):
    d, scratch, _ = _read_dispatcher(env)
    f = scratch / "disasm.txt"
    f.write_text("\n".join(f"line-{i}" for i in range(1, 51)), encoding="utf-8")
    r = d.dispatch("read_file", {"path": "disasm.txt"})
    assert "1\tline-1" in r and "50\tline-50" in r          # cat -n 风格
    r = d.dispatch("read_file", {"path": "disasm.txt", "offset": 10, "limit": 5})
    assert "10\tline-10" in r and "14\tline-14" in r and "line-15" not in r
    assert "共 50 行" in r and "10-14" in r                  # 续读标记带总行数


def test_read_file_tail_and_long_line(env):
    d, scratch, _ = _read_dispatcher(env)
    f = scratch / "log.txt"
    f.write_text("\n".join(f"l{i}" for i in range(1, 31)) + "\n" + "x" * 2500,
                 encoding="utf-8")
    r = d.dispatch("read_file", {"path": "log.txt", "offset": -3})
    assert "31\t" in r and "l29" in r                        # 末尾 N 行（tail 语义）
    assert "行截断" in r and "共 2500 字符" in r              # 超长行切尾标注（>2000）


def test_read_file_missing_and_read_anywhere(env):
    """read 路径放开（2026-10-01）：工作区外文件也可读（只有写才拦边界）；
    缺文件 / 越界 offset 仍报错。"""
    d, scratch, artifacts = _read_dispatcher(env)
    r = d.dispatch("read_file", {"path": "nope.txt"})
    assert r.startswith("[错误]") and "不存在" in r
    (scratch.parent.parent / "outside.txt").write_text("secret", encoding="utf-8")
    r = d.dispatch("read_file", {"path": "../../outside.txt"})   # 工作区外
    assert "secret" in r and not r.startswith("[拒绝]")          # 放开后可读
    (scratch / "tiny.txt").write_text("only\n", encoding="utf-8")
    r = d.dispatch("read_file", {"path": "tiny.txt", "offset": 99})
    assert r.startswith("[错误]") and "共 1 行" in r          # 越界报总行数


def test_read_file_read_anywhere_including_packs(env):
    """read 路径放开（2026-10-01）：packs 内（kb 各域 / experts）与未启用域 kb 根
    一律可读——不再有根白名单；工作区相对路径照旧。"""
    bb, project, gw, tmp_path = env
    artifacts = tmp_path / "ws" / "artifacts"
    agent = make_agent(env, ScriptedLLM([]), artifacts_dir=artifacts)
    d = agent.dispatcher
    packs = tmp_path / "packs"
    kb_md = packs / "kb" / "web" / "poc" / "handbook.md"
    kb_md.parent.mkdir(parents=True, exist_ok=True)
    kb_md.write_text("1\t手册正文第一行\n第二行", encoding="utf-8")
    assert "手册正文第一行" in d.dispatch("read_file", {"path": str(kb_md)})
    # 未启用域（binary）的 kb 根：放开后同样可读
    bin_md = packs / "kb" / "binary" / "notes.md"
    bin_md.parent.mkdir(parents=True, exist_ok=True)
    bin_md.write_text("binary notes", encoding="utf-8")
    assert "binary notes" in d.dispatch("read_file", {"path": str(bin_md)})
    # packs/experts：不再拒绝（文件存在则可读）
    expert = packs / "experts" / "_generalist.yaml"
    expert.parent.mkdir(parents=True, exist_ok=True)
    expert.write_text("generalist", encoding="utf-8")
    assert "generalist" in d.dispatch("read_file", {"path": str(expert)})
    # 工作区文件不受影响
    scratch = tmp_path / "ws" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "ws.txt").write_text("workspace file", encoding="utf-8")
    assert "workspace file" in d.dispatch("read_file", {"path": "ws.txt"})


def test_read_file_resolves_container_and_wsl_paths(env):
    """容器/WSL 绝对路径映射（2026-10-01）：docker 会话里模型拿到的是 /workspace/…
    （挂 <ws>）、wsl 拿到 /mnt/<盘符>/…——都要映射回宿主真实路径才读得到。"""
    from core.runtime.pathguard import windows_to_wsl_path
    d, scratch, _ = _read_dispatcher(env)
    (scratch / "venus").mkdir(parents=True, exist_ok=True)
    f = scratch / "venus" / "fuzz.out"
    f.write_text("crash-0x1234", encoding="utf-8")
    # docker：/workspace/scratch/venus/fuzz.out → <ws>/scratch/venus/fuzz.out
    r = d.dispatch("read_file", {"path": "/workspace/scratch/venus/fuzz.out"})
    assert "crash-0x1234" in r and not r.startswith("[拒绝]")
    # wsl：/mnt/<盘符>/… → <盘符>:\…
    r2 = d.dispatch("read_file", {"path": windows_to_wsl_path(str(f))})
    assert "crash-0x1234" in r2 and not r2.startswith("[拒绝]")


# ---------- M1 prompt caching：system 拆块（2026-09-23） ----------

def test_system_blocks_split_stable_dynamic(env):
    """system 拆 stable/dynamic 两块：stable=规则链+角色+能力清单（打
    cache_control ephemeral 断点）；dynamic=技能指引+当前任务+纪律尾。
    拼接回 str 与 build_system_prompt 全等——纯拆分，组装内容零变化。"""
    ag = make_agent(env, ScriptedLLM([]))
    blocks = ag.build_system_blocks("扫描 80 端口", skill_context="技能正文片段")
    assert len(blocks) == 2
    stable, dynamic = blocks
    assert stable["type"] == "text" and dynamic["type"] == "text"
    assert stable["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in dynamic
    assert "能力清单" in stable["text"] and "角色" in stable["text"]
    assert "当前任务" in dynamic["text"] and "扫描 80 端口" in dynamic["text"]
    assert "技能指引" in dynamic["text"]
    assert stable["text"] + "\n\n" + dynamic["text"] == \
        ag.build_system_prompt("扫描 80 端口", skill_context="技能正文片段")


def test_system_blocks_dynamic_changes_keep_stable_prefix(env):
    """同会话跨任务换 objective/skill_context：stable 块字节不变（缓存前缀
    命中的前提），dynamic 块随任务变化。"""
    ag = make_agent(env, ScriptedLLM([]))
    b1 = ag.build_system_blocks("任务一")
    b2 = ag.build_system_blocks("任务二", skill_context="不同技能")
    assert b1[0]["text"] == b2[0]["text"]
    assert b1[1]["text"] != b2[1]["text"]
