"""Agent 主循环测试：脚本化 LLM 驱动端到端（不触网）。

覆盖：工具分发全链路、网关拒绝改道、系统提示组装（红线/角色/能力清单）、
策略顾问卡死干预、会话收尾安全（未收尾任务自动 fail）、上下文裁剪。
"""

import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.agent import AgentConfig, AgentSession
from core.agent.loop import CHAT_TOOLS, _LeaseHeartbeat, sanitize_snapshot_tail
from core.blackboard import Blackboard, TaskQueue
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
    tq = TaskQueue(bb)
    yield bb, project, gw, tq, tmp_path
    bb.close()


def make_agent(env, llm, planner=None, config=None, role="_generalist", artifacts_dir=None,
               capabilities=("web",), enable_sediment=False):
    bb, project, gw, tq, tmp_path = env
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


def write_role(env, name, body):
    """测试夹具里写一个专家 yaml（expert-pool M2：运行时角色源=experts/）。"""
    _, _, _, _, tmp_path = env
    f = tmp_path / "packs" / "experts" / f"{name}.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


# ---------- 端到端：run_cmd → 发现 → finish ----------

def test_full_loop_run_cmd_finding_finish(env, monkeypatch):
    bb, project, gw, tq, _ = env
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
    summary = agent.run_task("测 x.com")
    assert summary == "干完了"
    assert len(bb.list_findings(project["id"])) == 1
    assert bb.list_assets(project["id"])[0]["value"] == "x.com"
    # 审计链：command 与 session.finished 都落了事件
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "command" in kinds and "session.finished" in kinds
    # 状态机对齐（2026-09-24）：正常收尾 DB status 回 idle，不残留 running
    assert bb.get_session(agent.session["id"])["status"] == "idle"
    # 系统提示包含红线与能力清单
    sys_prompt = llm.calls[0]["system"]
    assert "红线" in sys_prompt and "能力清单" in sys_prompt and "当前任务" in sys_prompt


def test_gateway_deny_feeds_back_not_crashes(env):
    """恶意样本跑 host → 网关拒绝 → 拒绝文本回填 → Agent 改道 sandbox → 成功。"""
    bb, project, gw, tq, _ = env
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
    summary = agent.run_task("分析样本")
    assert summary == "改道成功"
    # 拒绝落了 audit.deny；拒绝文本确实回给了 LLM
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "audit.deny" in kinds
    tool_msgs = [m for m in llm.calls[1]["messages"]
                 if isinstance(m.get("content"), list)
                 and any(b.get("type") == "tool_result" for b in m["content"])]
    assert any("[网关拒绝]" in b["content"]
               for m in tool_msgs for b in m["content"] if b.get("type") == "tool_result")


def test_finish_with_open_task_auto_fails(env):
    """会话结束时任务未收尾 → 自动 fail（防 lease 占坑，§6.4）。"""
    bb, project, gw, tq, _ = env
    task_id = tq.publish(project["id"], "长任务", created_by="human")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "没干完就走"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("做任务", task_id=task_id)  # run_task 自动认领 open 任务
    assert tq.get_task(task_id)["status"] == "failed"


def test_task_tools_complete_flow(env):
    bb, project, gw, tq, _ = env
    task_id = tq.publish(project["id"], "枚举子域", task_type="recon", created_by="human")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "枚举了 12 个"})]},
        # D6 收尾确认：首轮申报被清单确认拦截，零新增一轮后再次申报落定
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "枚举了 12 个"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "任务完成"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("枚举", task_id=task_id)
    assert tq.get_task(task_id)["status"] == "done"


# ---------- 策略顾问（§3 卡死干预） ----------

def test_advisor_invoked_when_stuck(env):
    bb, project, gw, tq, _ = env
    planner = ScriptedLLM([
        {"text": "建议：先查黑板去重，再试另一个攻击面。"},
    ])
    # 剧本：连续空转（纯文本停等）直到顾问触发，然后 finish
    script = [{"text": "……继续观察"} for _ in range(8)]
    script.append({"tool_use": [ScriptedLLM.tool_call("t9", "finish", {"summary": "完成"})]})
    llm = ScriptedLLM(script)
    cfg = AgentConfig(max_steps=15, stuck_after=3)
    agent = make_agent(env, llm, planner=planner, config=cfg)
    agent.run_task("空转测试")
    # 顾问被调用过，且其建议以 [策略顾问] 注入了主循环消息
    assert len(planner.script) == 0
    injected = any("[策略顾问]" in json.dumps(c["messages"], ensure_ascii=False)
                   for c in llm.calls)
    assert injected


def test_advisor_intervention_event_and_session_scope(env):
    """顾问可观测（2026-09-20）：①发言落 `advisor.intervention` 事件（此前只进
    messages，直播间只见 token 行不知顾问说了什么）；②顾问视野收窄到本会话事件
    （此前全项目 limit=30，多窗时顾问满眼别人的上下文，执行者正确地当噪声拒掉）。"""
    bb, project, gw, tq, _ = env
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

def test_d7_second_stuck_advisor_verdict_terminate(env):
    """D7（D1 修订）：第 1 轮卡死召唤顾问建议；第 2 轮卡死不再机械终止——
    改调顾问裁决模式。裁决 terminate → agent.stuck_escalate（trigger=
    advisor_terminate，waves=2）+ awaiting_human，任务 fail(resumable)。
    顾问共被调用 2 次（建议 + 裁决），裁决事件 advisor.verdict 留痕。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "会卡死的任务", created_by="human")
    verdict_json = json.dumps(
        {"decision": "terminate",
         "reason": "建议路径已被执行仍无新事实，继续无意义", "instruction": ""})
    planner = ScriptedLLM([
        {"text": "建议：换一个攻击面试试。"},
        {"text": verdict_json},
    ])
    llm = ScriptedLLM([{"text": "……继续观察"} for _ in range(6)])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    assert agent.run_task("卡死任务", task_id=tid) == ""
    assert len(planner.calls) == 2  # 建议 + 裁决
    task = tq.get_task(tid)
    assert task["status"] == "failed" and task["blocked_reason"] == "awaiting_human"
    verdicts = [e for e in bb.recent_events(project["id"])
                if e["kind"] == "advisor.verdict"]
    assert len(verdicts) == 1 and verdicts[0]["payload"]["decision"] == "terminate"
    esc = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "agent.stuck_escalate"]
    assert len(esc) == 1
    p = esc[0]["payload"]
    assert (p["waves"] == 2 and p["step"] == 6
            and p["last_progress_step"] == 3
            and p["trigger"] == "advisor_terminate"
            and "无新事实" in p["verdict_reason"])


def test_d1_progress_clears_stuck_waves(env):
    """D1：顾问介入一轮后，若 Agent 干出实质进展（task_plan 状态变化），
    卡死波次清零——再卡 8 步仍走第 1 轮顾问而不是升级停轮。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "波次清零任务", created_by="human")
    planner = ScriptedLLM([
        {"text": "建议一：先整理计划再动手。"},
        {"text": "建议二：换另一个攻击面。"},
    ])
    llm = ScriptedLLM([
        {"text": "……观察"}, {"text": "……观察"}, {"text": "……观察"},
        {"tool_use": [ScriptedLLM.tool_call(
            "p4", "task_plan", {"steps": [{"title": "按顾问建议换思路"}]})]},
        {"text": "……观察"}, {"text": "……观察"}, {"text": "……观察"},
        {"tool_use": [ScriptedLLM.tool_call("t8", "finish", {"summary": "收尾"})]},
    ])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3))
    agent.run_task("波次清零", task_id=tid)
    assert len(planner.calls) == 2  # 两次都走顾问，未升级
    assert tq.get_task(tid)["status"] == "failed"  # finish 后未收尾任务自动 fail
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.stuck_escalate"]


# ---------- stuck-convergence D9：活跃探索静默延长（2026-09-24） ----------

def test_d9_command_evolution_extends_without_advisor(env):
    """D9：窗内每步跑不同命令（命令演进信号）→ 到期静默延长、顾问零调用，
    agent.stuck_extend 事件留痕（stuck_after=3：第 3/6 步各延长一次）。"""
    bb, project, gw, tq, _ = env
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
    assert agent.run_task("逆向中") == "长任务完成"
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
    bb, project, gw, tq, tmp_path = env
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
    assert agent.run_task("读代码中") == "读完了"
    assert len(planner.calls) == 0
    extends = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "agent.stuck_extend"]
    assert [e["payload"]["extension"] for e in extends] == [1, 2]
    assert extends[0]["payload"]["signals"]["new_files"][0] == "target1.js"


def test_d9_repeated_commands_still_invoke_advisor(env):
    """D9：窗内全是同一条命令（unique=1）不满足演进 → 预检不延长、照常叫顾问
    （真卡死必须在第一个观察窗被抓到，不能被预检放过）。"""
    bb, project, gw, tq, _ = env
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
    agent.run_task("打转中")
    assert len(planner.calls) == 1
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.stuck_extend"]
    interventions = [e for e in bb.recent_events(project["id"])
                     if e["kind"] == "advisor.intervention"]
    assert len(interventions) == 1


def test_d9_extension_cap_then_advisor_chain(env):
    """D9：2 次静默延长用满后第 3 个窗不再延长——走顾问建议链（延长只推后
    不拆除断路器：顾问 ≤2 次、硬闸兜底）。"""
    bb, project, gw, tq, _ = env
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
    agent.run_task("长程逆向")
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
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "重复跑命令的任务", created_by="human")
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

    llm = ScriptedLLM([
        # 先过 A2 计划闸（否则 run_cmd 三连环被拒→E2 熔断，走不到卡死路径）
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "task_plan", {"steps": [{"title": "跑命令"}]})]},
        cmd_item("r1", "dup-cmd"), cmd_item("r2", "dup-cmd"),
        # 第 4 步先撞卡死召唤顾问（lps 被重置到 4），随后执行 uniq-cmd
        cmd_item("r3", "uniq-cmd"),
        {"text": "……"}, {"text": "……"},
        # 第 7 步第 2 轮卡死 → 升级（chat 仍消费本项，C1 分支收尾）
        {"text": "……"},
    ])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    agent.run_task("命令重复", task_id=tid)
    # 裁决 prompt：含上次建议原文 + 建议后实际执行的命令
    verdict_seen = json.dumps(planner.calls[1]["messages"], ensure_ascii=False)
    assert "不要重复跑同一条命令" in verdict_seen
    assert "dup-cmd" in verdict_seen and "uniq-cmd" in verdict_seen
    esc = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "agent.stuck_escalate"][-1]
    p = esc["payload"]
    assert p["step"] == 7 and p["last_progress_step"] == 4
    assert p["trigger"] == "advisor_terminate"
    assert p["repeated_commands"] == [{"cmd": "dup-cmd", "times": 2}]
    assert p["recent_commands"] == ["dup-cmd", "dup-cmd", "uniq-cmd"]


def test_d2_repeat_stats_and_advisor_prompt_injection(env):
    """D2：_command_repeat_stats 机械聚合完全相同命令 ×N（×≥2、按次数降序）；
    排行注入顾问 prompt，收敛性判断交 LLM。"""
    bb, project, gw, tq, _ = env
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
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "继续后仍卡死", created_by="human")
    continue_json = json.dumps(
        {"decision": "continue", "reason": "执行者在深挖 JS 未收口",
         "instruction": "停止 grep，立即把探针结论登记入黑板"})
    planner = ScriptedLLM([
        {"text": "建议：先收口已有探针。"},
        {"text": continue_json},
    ])
    llm = ScriptedLLM([{"text": "……继续观察"} for _ in range(9)])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    assert agent.run_task("继续硬闸", task_id=tid) == ""
    assert len(planner.calls) == 2  # 第 3 轮硬闸不再调顾问
    # 半强制裁决指令注入主循环
    injected = json.dumps(llm.calls, ensure_ascii=False)
    assert "[策略顾问·裁决：继续]" in injected
    assert "立即把探针结论登记入黑板" in injected
    verdict_ev = [e for e in bb.recent_events(project["id"])
                  if e["kind"] == "advisor.verdict"][-1]
    assert verdict_ev["payload"]["decision"] == "continue"
    task = tq.get_task(tid)
    assert task["status"] == "failed" and task["blocked_reason"] == "awaiting_human"
    esc = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "agent.stuck_escalate"][-1]
    p = esc["payload"]
    assert (p["waves"] == 3 and p["step"] == 9
            and p["last_progress_step"] == 6
            and p["trigger"] == "hard_backstop")


def test_d7_verdict_human_request(env):
    """D7：裁决 human（信息不足）→ 直接 awaiting_human 挂起，不产出
    stuck_escalate（非升级终止，是顾问主动请人），verdict 事件留痕。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "顾问判不了", created_by="human")
    human_json = json.dumps(
        {"decision": "human", "reason": "缺关键探针结果无法判断", "instruction": ""})
    planner = ScriptedLLM([
        {"text": "建议：补一个探针。"},
        {"text": human_json},
    ])
    llm = ScriptedLLM([{"text": "……继续观察"} for _ in range(6)])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    agent.run_task("请求人工", task_id=tid)
    assert len(planner.calls) == 2
    task = tq.get_task(tid)
    assert task["status"] == "failed" and task["blocked_reason"] == "awaiting_human"
    verdict_ev = [e for e in bb.recent_events(project["id"])
                  if e["kind"] == "advisor.verdict"][-1]
    assert verdict_ev["payload"]["decision"] == "human"
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.stuck_escalate"]


def test_d7_verdict_llm_failure_falls_back_terminate(env):
    """D7：裁决 LLM 调用抛异常 → 回落 terminate（宁严勿松，绝不回落继续）：
    stuck_escalate trigger=advisor_terminate，理由注明调用失败。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "顾问炸了", created_by="human")
    planner = ScriptedLLM([
        {"text": "建议：换思路。"},
        LLMError("planner down"),
    ])
    llm = ScriptedLLM([{"text": "……继续观察"} for _ in range(6)])
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=12, stuck_after=3),
                       artifacts_dir=tmp_path / "artifacts")
    agent.run_task("顾问异常", task_id=tid)
    assert len(planner.calls) == 2
    esc = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "agent.stuck_escalate"][-1]
    p = esc["payload"]
    assert p["trigger"] == "advisor_terminate" and p["waves"] == 2
    assert "调用失败" in p["verdict_reason"]
    assert tq.get_task(tid)["status"] == "failed"


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

def _closing_events(bb, pid):
    return [(e["payload"]["round"], e["payload"]["pathway"])
            for e in bb.recent_events(pid, limit=50)
            if e["kind"] == "agent.closing_confirm"]


def test_d1_command_windows_read_latest_not_earliest(env):
    """事件窗方向修复（task-97e1d5d6c597 真实事故复盘）：recent_events(limit=N)
    since_id=0 取的是**最早** N 条——长会话升级时 recent_commands / 重复排行 /
    顾问 digest 全看到会话开头（事故里人工看到的是开场 spill grep，不是卡点处的
    JS 分析）。统一改 tail=N 取最新窗（tail 升序返回，下游 [-cap:] 口径不变）。"""
    bb, project, gw, tq, _ = env
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


def test_d6_zero_new_closes_after_confirmation(env):
    """D6：首次 complete 被清单确认拦截；一轮零新增后再次 complete 落定 done。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "收尾任务", created_by="human")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("c1", "complete_task",
                                            {"result_note": "完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("c2", "complete_task",
                                            {"result_note": "完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("f1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("收尾", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    assert _closing_events(bb, project["id"]) == [(1, ""), (1, "zero_new")]


def test_d6_new_output_continues_confirmation(env):
    """D6：确认轮内有新黑板写入 → 续干（进第 2 轮确认）；第 2 轮零新增才落定。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "确认轮补产出", created_by="human")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "task_plan", {"steps": [{"title": "先摸底"}]})]},
        {"tool_use": [ScriptedLLM.tool_call("c1", "complete_task",
                                            {"result_note": "申报"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "a1", "bb_add_asset", {"value": "http://10.5.5.5"})]},
        {"tool_use": [ScriptedLLM.tool_call("c2", "complete_task",
                                            {"result_note": "补完再申报"})]},
        {"tool_use": [ScriptedLLM.tool_call("c3", "complete_task",
                                            {"result_note": "无遗漏"})]},
        {"tool_use": [ScriptedLLM.tool_call("f1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("收尾", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    assert _closing_events(bb, project["id"]) == [
        (1, ""), (2, "new_output"), (2, "zero_new")]


def test_d6_dry_tail_bypasses_confirmation(env):
    """D6-B：complete 时最近 stuck_after−2=10 步零新增 → 干尾巴直接放行，
    不进确认轮（常态任务零额外成本）。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "干尾巴任务", created_by="human")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "task_plan", {"steps": [{"title": "摸底"}]})]},
        *[{"text": "……"} for _ in range(10)],
        {"tool_use": [ScriptedLLM.tool_call("c1", "complete_task",
                                            {"result_note": "干尾巴收尾"})]},
        {"tool_use": [ScriptedLLM.tool_call("f1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, artifacts_dir=tmp_path / "artifacts",
                       config=AgentConfig(max_steps=20))
    agent.run_task("干尾巴", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    assert _closing_events(bb, project["id"]) == [(0, "dry_tail")]
    assert agent.dispatcher.closing_round == 0  # 放行后确认态复位


def test_d6_dry_tail_and_stuck_window_do_not_race(env):
    """阈值错位无竞态：上次进展后干 10 步申报 complete → D6-B 放行落 done；
    另一任务（钉死 stuck_after=8）继续干到第 8 步 → 先撞 D1 顾问（任务不 done），
    complete 无机会。"""
    bb, project, gw, tq, tmp_path = env
    art = tmp_path / "artifacts"
    # 任务 A：10 步干尾巴（缺省 stuck_after=12 → dry_tail=10）→ D6 放行
    ta = tq.publish(project["id"], "干尾巴十步收尾", created_by="human")
    llm_a = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "task_plan", {"steps": [{"title": "摸底"}]})]},
        *[{"text": "……"} for _ in range(10)],
        {"tool_use": [ScriptedLLM.tool_call("c1", "complete_task",
                                            {"result_note": "十步收尾"})]},
        {"tool_use": [ScriptedLLM.tool_call("f1", "finish", {"summary": "完A"})]},
    ])
    agent_a = make_agent(env, llm_a, artifacts_dir=art,
                         config=AgentConfig(max_steps=20))
    agent_a.run_task("十步收尾", task_id=ta)
    assert tq.get_task(ta)["status"] == "done"
    assert _closing_events(bb, project["id"]) == [(0, "dry_tail")]

    # 任务 B：钉死 stuck_after=8，不申报 complete，连干 8 步 → 第 9 步先撞 D1 顾问
    tb = tq.publish(project["id"], "八步空转", created_by="human")
    planner_b = ScriptedLLM([{"text": "建议：立刻换攻击面。"}])
    llm_b = ScriptedLLM(
        [{"tool_use": [ScriptedLLM.tool_call(
            "p2", "task_plan", {"steps": [{"title": "摸底"}]})]}]
        + [{"text": "……"} for _ in range(8)])
    agent_b = make_agent(env, llm_b, planner=planner_b, artifacts_dir=art,
                         config=AgentConfig(max_steps=9, stuck_after=8))
    agent_b.run_task("八步空转", task_id=tb)
    assert len(planner_b.calls) == 1  # D1 先触发
    assert tq.get_task(tb)["status"] == "claimed"  # 预算暂停挂起，未被收尾
    assert not [(r, p) for r, p in _closing_events(bb, project["id"])
                if p not in ("dry_tail",)]
    # 用例收尾：预算暂停有意保留心跳（任务仍 claimed、默认 600s 续租间隔），
    # 不中止会泄漏守护线程，污染后续用例的全局 _heartbeat_alive() 孤儿断言
    agent_b._abort_current_task()


def test_d10_stuck_max_extensions_zero_still_invokes_advisor(env):
    """D10：stuck_max_extensions=0 → 演进命令不再静默延长，第一个观察窗照常
    召唤顾问（关闭延长不拆断路器）。"""
    bb, project, gw, tq, _ = env
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
    agent.run_task("打转中")
    assert len(planner.calls) == 1
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.stuck_extend"]
    assert len([e for e in bb.recent_events(project["id"])
                if e["kind"] == "advisor.intervention"]) == 1


def test_d10_stuck_max_extensions_one_then_advisor(env):
    """D10：上限=1 → 第 3 步延长 1 次，第 6 步延长用满走顾问链。"""
    bb, project, gw, tq, _ = env
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
    agent.run_task("长任务")
    extends = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "agent.stuck_extend"]
    assert [(e["payload"]["step"], e["payload"]["extension"]) for e in extends] == [(3, 1)]
    assert len(planner.calls) == 1
    assert len([e for e in bb.recent_events(project["id"])
                if e["kind"] == "advisor.intervention"]) == 1


def test_d10_closing_max_rounds_one_forces_release_on_new_output(env):
    """D10：closing_max_rounds=1 → 确认轮 1 内即使有新产出，也在再次申报时
    强制放行（round_cap），不进第 2 轮。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "cap1 收尾", created_by="human")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "task_plan", {"steps": [{"title": "先摸底"}]})]},
        {"tool_use": [ScriptedLLM.tool_call("c1", "complete_task",
                                            {"result_note": "申报"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "a1", "bb_add_asset", {"value": "http://10.5.5.5"})]},
        {"tool_use": [ScriptedLLM.tool_call("c2", "complete_task",
                                            {"result_note": "补完再申报"})]},
        {"tool_use": [ScriptedLLM.tool_call("f1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm,
                       config=AgentConfig(max_steps=12, closing_max_rounds=1))
    agent.run_task("收尾", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    assert _closing_events(bb, project["id"]) == [(1, ""), (1, "round_cap")]


def test_d10_closing_max_rounds_zero_first_claim_done(env):
    """D10：closing_max_rounds=0 → 首次 complete 申报直接放行（cap_zero，
    先于干尾巴判定），一轮确认都不进。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "cap0 收尾", created_by="human")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("c1", "complete_task",
                                            {"result_note": "直接完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("f1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm,
                       config=AgentConfig(max_steps=10, closing_max_rounds=0))
    agent.run_task("收尾", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    assert _closing_events(bb, project["id"]) == [(0, "cap_zero")]


# ---------- 上下文裁剪 ----------

def test_context_trimming(env):
    bb, project, gw, tq, _ = env

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
    agent.run_task("裁剪测试")
    # 网关 brief() 已截到 2000 字符/条，总历史远超 10k 预算 → _trim 必然把某次
    # 调用里的旧结果替换成占位（后续若被 G3 摘要整体替换也属正常压缩路径）
    truncated = [c for c in llm.calls
                 if "[已截断]" in json.dumps(c["messages"], ensure_ascii=False)]
    assert truncated


def test_context_summarization(env):
    """G3 结构化摘要压缩：过 context_summary_chars 阈值 → 旧历史经 LLM 压成
    一段摘要替换（近 8 条逐字保留），llm.compact 事件落流；机械 _trim 不触发。"""
    bb, project, gw, tq, _ = env

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
    agent.run_task("摘要压缩测试")
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
    bb, project, gw, tq, _ = env
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
    agent.run_task("小结果保留测试")
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


# ---------- 思考事件（DESIGN.md §12：llm.thinking 折叠摘要 + 展开全文） ----------

def test_thinking_lands_in_event_stream(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"thinking": "先看响应头指纹，再决定是否深入。",
         "tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("测一下")
    evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "llm.thinking"]
    assert len(evs) == 1
    assert "响应头指纹" in evs[0]["payload"]["thinking"]
    assert evs[0]["payload"]["step"] == 1
    assert evs[0]["session_id"] == agent.session["id"]


def test_no_thinking_no_event(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("测一下")
    assert not any(e["kind"] == "llm.thinking" for e in bb.recent_events(project["id"]))


# ---------- 思考流式（2026-09-19：llm.thinking.delta 增量 → 终稿清剪） ----------

def test_thinking_stream_deltas_then_final_prunes(env):
    """流式思考：增量事件文本递增（累计全文），终稿带 stream_id，且终稿落库后
    delta 行被清剪——审计只留终稿一条（delta 经 bus 捕获，直播窗口内可见）。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"thinking": "终稿思考内容。", "deltas": ["先" + "A" * 130, "后" + "B" * 130],
         "tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    seen: list[dict] = []
    unsub = bb.bus.subscribe(seen.append)
    try:
        agent.run_task("测一下")
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
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("测一下")
    evs = bb.recent_events(project["id"])
    assert not any(e["kind"] == "llm.thinking.delta" for e in evs)


# ---------- 工具参数流截断整轮重试（2026-09-20 事故修复） ----------

def test_truncated_stream_retries_whole_turn(env):
    """LLMError.truncated（工具参数流截断）→ 整轮重试：第一次截断、第二次成功，
    任务正常完成且重发消息与首轮一致（同一对话现场重建）。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        LLMError("工具参数流截断（stop_reason=max_tokens，已收 7 字符）",
                 truncated=True),
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    assert agent.run_task("测截断重试") == "完"
    assert len(llm.calls) == 2
    assert llm.calls[0]["messages"] == llm.calls[1]["messages"]


def test_truncated_stream_retries_exhausted_fails_task(env):
    """连续截断超上限（2 次）→ 异常穿出走 worker 异常退出兜底（已认领任务 fail），
    共 3 次调用。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([LLMError("截断", truncated=True)] * 3)
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "截断耗尽任务", created_by="human")
    with pytest.raises(LLMError, match="截断"):
        agent.run_task("截断耗尽任务", task_id=tid)
    assert len(llm.calls) == 3
    assert any(e["kind"] == "task.failed" for e in bb.recent_events(project["id"]))


def test_truncated_stream_final_retry_falls_back_to_non_stream(env):
    """连续截断：前两轮重试保持流式，最后一轮降级非流式（不带 on_thinking——
    网关 SSE 连续断流时整包响应不受影响）。非流式轮成功即任务正常完成。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        LLMError("工具参数流截断（stop_reason=tool_use，已收 80 字符）",
                 truncated=True),
        LLMError("工具参数流截断（stop_reason=tool_use，已收 80 字符）",
                 truncated=True),
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "非流式救回"})]},
    ])
    agent = make_agent(env, llm)
    assert agent.run_task("测截断降级") == "非流式救回"
    assert len(llm.calls) == 3
    assert [c["stream"] for c in llm.calls] == [True, True, False]


def test_plain_llm_error_no_retry(env):
    """非截断类 LLMError（网络重试耗尽等）不触发整轮重试——原样穿出。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([LLMError("网络错误（已重试 3 次）")])
    agent = make_agent(env, llm)
    with pytest.raises(LLMError, match="网络错误"):
        agent.run_task("测不重试")
    assert len(llm.calls) == 1


# ---------- 任务叙述落事件（2026-09-19 直播间终端化） ----------

def test_task_loop_narration_lands_as_agent_chat(env):
    """任务循环 assistant text 叙述落 agent.chat（带 step；有 tool_calls 的步也落）。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"text": "先探测 Web 指纹，再决定突破口。",
         "tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("测一下")
    evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "agent.chat"]
    assert len(evs) == 1
    p = evs[0]["payload"]
    assert p["text"] == "先探测 Web 指纹，再决定突破口。"
    assert p["step"] == 1 and p["session_id"] == agent.session["id"]


def test_task_loop_no_text_no_narration(env):
    """纯工具步（无 text 块）不落 agent.chat，避免空行噪音。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("测一下")
    assert not any(e["kind"] == "agent.chat" for e in bb.recent_events(project["id"]))


# ---------- 会话控制（DESIGN.md §3：暂停/恢复/中断） ----------

def test_pause_between_steps_and_resume_continues(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task",
                                            {"result_note": "侦察完成"})]},
        # D6：快照恢复后首次申报进确认轮，零新增再申报落定（complete 即委托收尾，
        # 会话中心化下不再跟 finish——返回值=complete 收尾注记）
        {"tool_use": [ScriptedLLM.tool_call("t2b", "complete_task",
                                            {"result_note": "干完了"})]},
    ])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "暂停演练任务", task_type="generic")
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            agent.request_pause()  # 步 1 结束的检查点生效
        return r
    llm.chat = chat

    summary = agent.run_task("暂停演练", task_id=tid)
    assert summary == ""                       # 暂停退出，不是任务总结
    task = tq.get_task(tid)
    assert task["status"] == "claimed"         # 未被误 fail（_finalize 守卫）
    assert agent._resume_state is not None
    assert agent._resume_state["task_id"] == tid
    assert agent._resume_state["next_step"] == 2
    assert agent.paused is True
    assert not any(e["kind"] == "session.finished"
                   for e in bb.recent_events(project["id"]))

    # 恢复（API resume 端点同款操作）：清标志 → run_next_task 从快照续跑
    agent._pause_req.clear()
    agent._abort_req.clear()
    agent.paused = False
    assert agent.run_session() == "干完了"
    assert tq.get_task(tid)["status"] == "done"
    # 会话中心化：委托收尾会话存活（无 session.finished），窗回待命接后续委托
    assert not any(e["kind"] == "session.finished"
                   for e in bb.recent_events(project["id"]))


def test_abort_fails_task_with_human_note(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "中断演练任务", task_type="generic")
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            agent.request_abort()
        return r
    llm.chat = chat

    assert agent.run_task("中断演练", task_id=tid) == ""
    task = tq.get_task(tid)
    assert task["status"] == "failed"
    assert "人工中断" in task["result_note"]
    evs = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "session.aborted" in evs
    assert "session.finished" not in evs       # 中断不走 _finalize，不重复收尾
    assert agent._abort_req.is_set() is False and agent._pause_req.is_set() is False
    # 一次性闸门：worker 下一次 run_next_task 返回 None 且不认领新任务
    assert agent.run_session() is None
    assert agent._stop_after_task is False


def test_abort_interrupts_blocked_llm_call_immediately(env):
    """■ 即点即停（2026-09-19）：LLM 调用阻塞中 request_abort → 不等响应返回
    （旧步边界语义要等当前步做完），任务立刻 fail「人工中断」；阻塞的旁路线程
    被放弃（放行后自行结束，结果丢弃）。"""
    bb, project, gw, tq, _ = env
    started, gate = threading.Event(), threading.Event()
    llm = ScriptedLLM([])  # 剧本空——中断后不会真正取到响应
    orig_chat = llm.chat

    def chat(messages, **kw):
        started.set()
        assert gate.wait(10), "gate 超时"
        return orig_chat(messages, **kw)

    llm.chat = chat
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "阻塞中断任务", task_type="generic")

    th = threading.Thread(target=lambda: agent.run_task("阻塞中断", task_id=tid))
    th.start()
    assert started.wait(5), "chat 未开始"
    agent.request_abort()  # LLM 调用阻塞中：不 set gate，立即中断
    th.join(5)
    assert not th.is_alive()  # 不等 gate 放行即退出
    gate.set()  # 放行被放弃的旁路线程，让它自然结束
    task = tq.get_task(tid)
    assert task["status"] == "failed"
    assert "人工中断" in task["result_note"]
    assert agent._abort_req.is_set() is False


def test_worker_never_claims_after_pause(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "排队任务", task_type="generic")
    agent.request_pause()
    assert agent.run_session() is None
    assert tq.get_task(tid)["status"] == "open"      # 不认领
    assert agent.paused is True
    assert bb.list_sessions(project["id"])[0]["status"] == "paused"  # 空闲路径直接生效


# ---------- bb_add_asset 自动挂载 + 重复合并（DESIGN.md §5.2） ----------

def test_add_asset_auto_mount_and_meta_merge(env, monkeypatch):
    # E6 起 domain 由平台自动 DNS 挂载——测试断网 hermetic（解析失败=独立行）
    from core.blackboard import assets as am

    def _no_dns(*a, **k):
        raise OSError("dns off")
    monkeypatch.setattr(am.socket, "getaddrinfo", _no_dns)
    bb, project, gw, tq, _ = env
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
    agent.run_task("自动挂载")
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

    bb, project, gw, tq, tmp_path = env
    artifacts_dir = tmp_path / "artifacts_out"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "t1", "bb_add_artifact",
            {"filename": "poc_sqli.py", "content": "import requests\n",
             "kind": "poc", "description": "SQL 注入 POC 脚本"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, artifacts_dir=artifacts_dir)
    agent.run_task("落 POC 产物")

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
    agent.run_task("缺产物目录")
    rows = agent.bb.conn.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE project_id=?",
        (agent.project_id,)).fetchone()
    assert rows["c"] == 0
    # 错误文本已回填给 LLM（工具结果消息里能看到提示）
    assert any("产物目录" in json.dumps(m, ensure_ascii=False) for m in llm.calls[1]["messages"])


def test_add_finding_with_poc_artifact(env):
    """bb_add_finding 透传 poc_artifact_id（evidence.poc 引 Python 脚本产物的场景）。"""
    bb, project, gw, tq, _ = env
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
    agent.run_task("带 POC 引用的发现")
    rows = [f for f in bb.list_findings(project["id"]) if f["vuln_class"] == "sqli"]
    assert len(rows) == 1
    assert rows[0]["poc_artifact_id"] == art_id
    assert rows[0]["status"] == "verified"


def test_add_finding_relates_to_passthrough_and_dangling_reported(env):
    """bb_add_finding 的 relates_to 顶层入参并入 evidence；悬空 id 回填错误不中断。"""
    bb, project, gw, tq, _ = env
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
    agent.run_task("强关系登记")
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
    bb, project, gw, tq, _ = env
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
    agent.run_task("发现修订")
    row = bb.get_finding(project["id"], fid)
    assert row["severity"] == "low" and row["category"] == "intel"
    assert row["rating_basis"].startswith("rating:edu-rating 低危#1")
    # t2 门禁拒收回填、t3 不存在错误回填，循环未中断
    back = json.dumps(llm.calls, ensure_ascii=False)
    assert "不再收录 severity=info" in back
    assert "发现不存在" in back


def test_delete_finding_tool_guards(env):
    """bb_delete_finding：verified 拒删、reason 必填、unverified+reason 删除成功。"""
    bb, project, gw, tq, _ = env
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
    agent.run_task("发现删除")
    assert bb.get_finding(project["id"], g) is None  # t3 成功删除
    assert bb.get_finding(project["id"], v) is not None  # t2 verified 拒删
    back = json.dumps(llm.calls, ensure_ascii=False)
    assert "删除必须说明原因" in back  # t1 无 reason 拒
    assert "verified 发现不可删除" in back


def test_add_artifact_poc_python_only(env):
    """kind=poc 仅限 Python（§5.2 纪律）：非 .py 拒绝（返回错误文本，不落盘）。"""
    bb, project, gw, tq, tmp_path = env
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
    agent.run_task("POC 语言纪律")
    rows = bb.conn.execute(
        "SELECT path FROM artifacts WHERE project_id=?", (project["id"],)).fetchall()
    assert [r["path"] for r in rows] == ["poc/poc.py"]  # ps1 被拒，只有 .py 落库
    assert (artifacts_dir / "poc" / "poc.py").is_file()
    assert not (artifacts_dir / "poc" / "poc.ps1").exists()


def test_kb_open_module(env):
    """kb_open：M0 合成源命中返回绝对路径 + kb.open 审计；不存在返回清单（防幻觉）；无 kb 域报错。"""
    bb, project, gw, tq, tmp_path = env
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
    agent.run_task("开知识库")
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
    agent2.run_task("幻觉模块名")
    result2 = json.dumps(llm2.calls[1]["messages"], ensure_ascii=False)
    assert "防幻觉" in result2 and "xss-test.md" in result2 and "web/sub/deep.md" in result2

    # .. 穿越直接拒绝
    llm_t = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "kb_open", {"module": "../../x.md"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent_t = make_agent(env, llm_t)
    agent_t.run_task("穿越")
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
    agent3.run_task("无知识库")
    result3 = json.dumps(llm3.calls[1]["messages"], ensure_ascii=False)
    assert "无知识库" in result3


# ---------- 角色软边界（DESIGN.md §6.6：tools / max_runtime / default_noise） ----------

def test_role_tools_whitelist_blocks(env):
    """角色 tools 白名单外的工具被拒（越界文本回填，循环不断）；收尾工具永远放行。"""
    bb, project, gw, tq, _ = env
    write_role(env, "scout",
               'name: scout\npersona: "侦察"\nskills: null\n'
               "tools: [bb_query]\ndefault_noise: passive\n")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "被拦后收工"})]},
    ])
    agent = make_agent(env, llm, role="scout")
    assert agent.run_task("越权命令") == "被拦后收工"
    tool_msgs = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "[越界拒绝]" in tool_msgs and "工具白名单" in tool_msgs
    # 没有真正执行 → 无 command 审计事件
    assert "command" not in [e["kind"] for e in bb.recent_events(project["id"])]


def test_role_max_runtime_blocks_level(env):
    """角色 max_runtime=host 时，docker/sandbox 等级运行时被拒（只可能更严）。"""
    write_role(env, "hostonly",
               'name: hostonly\npersona: "本机"\nmax_runtime: host\n')
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "id", "runtime": "sandbox",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "run_cmd",
                                            {"cmd": "id", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "降级成功"})]},
    ])
    agent = make_agent(env, llm, role="hostonly")
    assert agent.run_task("运行时越界") == "降级成功"
    msgs = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "[越界拒绝]" in msgs and "max_runtime=host" in msgs


# ---------- v23 任务默认运行时（TRAE 新壳 M3，2026-09-25） ----------

def test_preferred_runtime_edit_validates(env):
    """preferred_runtime 走 update_task 白名单：合法值/空串（重置）放行，
    非法值 ValueError；get_task 回读。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "可改默认运行时")
    assert tq.get_task(tid)["preferred_runtime"] == ""
    tq.update_task(tid, preferred_runtime="docker")
    assert tq.get_task(tid)["preferred_runtime"] == "docker"
    tq.update_task(tid, preferred_runtime="")  # 重置
    assert tq.get_task(tid)["preferred_runtime"] == ""
    with pytest.raises(ValueError, match="preferred_runtime"):
        tq.update_task(tid, preferred_runtime="kvm")


def test_preferred_runtime_fills_omitted_runtime(env):
    """run_cmd 省略 runtime：有任务默认 → 回填执行（command 审计事件带回填
    runtime）；无默认 → 错误文本要求显式传，不产生命令事件。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "默认 host")
    tq.update_task(tid, preferred_runtime="host")
    sid = "sess-" + "c" * 12
    from core.agent.tools import ToolDispatcher
    disp = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                          session_id=sid, author=sid)
    disp.current_task_id = tid
    out = disp.dispatch("run_cmd", {"cmd": "echo RT_MARKER",
                                    "threat_class": "trusted"})
    assert "RT_MARKER" in out and "[错误]" not in out
    cmd_events = [e for e in bb.recent_events(project["id"])
                  if e["kind"] == "command"]
    assert cmd_events and cmd_events[-1]["payload"]["runtime"] == "host"

    # 无默认的任务：拒绝并提示显式传
    tid2 = tq.publish(project["id"], "无默认")
    disp.current_task_id = tid2
    out2 = disp.dispatch("run_cmd", {"cmd": "echo NOID_EXEC",
                                     "threat_class": "trusted"})
    assert "未设置默认运行时" in out2
    assert not any(e["kind"] == "command"
                   and e["payload"].get("cmd") == "echo NOID_EXEC"
                   for e in bb.recent_events(project["id"]))


def test_preferred_runtime_notice_and_e2e(env):
    """端到端：任务带 preferred_runtime 认领后，prompt 含默认运行时提示；
    LLM 省略 runtime 的 run_cmd 照样执行（回填）。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "默认 sandbox")
    tq.update_task(tid, preferred_runtime="sandbox")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "task_plan", {"steps": [{"title": "跑命令"}]})]},
        # 省略 runtime：按任务默认回填（env 的 sandbox 后端=FakeDockerBackend）
        {"tool_use": [ScriptedLLM.tool_call(
            "r1", "run_cmd", {"cmd": "echo SANDBOX_MARKER",
                              "threat_class": "malware_live"})]},
        {"tool_use": [ScriptedLLM.tool_call("f1", "finish",
                                            {"summary": "默认执行成功"})]},
    ])
    agent = make_agent(env, llm)
    assert agent.run_task("默认运行时任务", task_id=tid) == "默认执行成功"
    first_msgs = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    assert "默认执行运行时=sandbox" in first_msgs
    cmd_events = [e for e in bb.recent_events(project["id"])
                  if e["kind"] == "command"]
    assert cmd_events[-1]["payload"]["runtime"] == "sandbox"
    assert "SANDBOX_MARKER" in cmd_events[-1]["payload"]["cmd"]


def test_role_default_noise_no_longer_filters_claim(env):
    """2026-09-18 窗口去 role 限制：default_noise=passive 的底色角色**不再过滤认领**，
    low 噪声任务照常认领（噪声上限只是系统提示自陈，边界随任务换装生效）。"""
    bb, project, gw, tq, _ = env
    write_role(env, "quiet",
               'name: quiet\npersona: "安静"\ndefault_noise: passive\n')
    # active 任务需要 conflict_keys（§6.2）；会话中心化：委托定窗本会话
    agent = make_agent(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task", {"result_note": "完"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task", {"result_note": "完"})]},
    ]), role="quiet")
    loud = tq.publish(project["id"], "主动打点", task_type="generic",
                      noise_budget="low", conflict_keys=["ip:10.0.0.9"], created_by="human",
                      target_session=agent.session["id"])
    assert agent.run_session() == "完"
    assert tq.get_task(loud)["status"] == "done"      # 无噪声过滤：low 任务被认领并完成


# ---------- A1：任务租约心跳（长任务防 TTL 过期被重领双跑） ----------

class _SlowFirstLLM(ScriptedLLM):
    """首次 chat 先跑 on_start 钩子、阻塞 delay 秒（留出租心跳窗口）、醒后跑 on_wake。"""

    def __init__(self, script, *, delay, on_start=None, on_wake=None):
        super().__init__(script)
        self.delay = delay
        self.on_start = on_start
        self.on_wake = on_wake

    def chat(self, messages, *, system=None, tools=None, max_tokens=4096, temperature=None,
             on_thinking=None, on_text=None, should_cancel=None):
        if not self.calls:
            if self.on_start:
                self.on_start()
            time.sleep(self.delay)
            if self.on_wake:
                self.on_wake()
        return super().chat(messages, system=system, tools=tools,
                            max_tokens=max_tokens, temperature=temperature)


def _heartbeat_alive():
    return [t for t in threading.enumerate()
            if t.name.startswith("lease-hb-") and t.is_alive()]


def test_lease_heartbeat_thread_renews_and_self_exits(env):
    """心跳直测：周期续租把 1 分钟短租约续到 ~30 分钟；任务消失（ClaimError）后自行退出。"""
    bb, project, gw, tq, _ = env
    sid = bb.register_session(project["id"], "S1")["id"]
    tid = tq.publish(project["id"], "被动分析")
    tq.claim(tid, sid, lease_minutes=1)

    hb = _LeaseHeartbeat(tq, sid, tid, 0.05)
    hb.start()
    time.sleep(0.25)  # 约 4-5 次续租
    lease = datetime.fromisoformat(tq.get_task(tid)["lease_until"])
    assert lease > datetime.now(timezone.utc) + timedelta(minutes=10)
    hb.stop()
    hb.join(timeout=2)
    assert not hb.is_alive()

    # 任务被人类删除后，下一周期续租抛 ClaimError → 心跳线程自退，绝不空转
    tq.delete(tid, by="human")
    hb2 = _LeaseHeartbeat(tq, sid, tid, 0.05)
    hb2.start()
    hb2.join(timeout=2)
    assert not hb2.is_alive()


def test_agent_heartbeat_renews_during_long_task_and_stops_on_finish(env):
    """端到端：多步长任务执行中租约被续（观测 lease_until 前进），收尾后心跳停止、租约清。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "慢任务")
    observed: dict[str, str] = {}
    llm = _SlowFirstLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task", {"result_note": "收工"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2b", "complete_task", {"result_note": "收工"})]},
    ], delay=0.3, on_wake=lambda: observed.update(lease=tq.get_task(tid)["lease_until"]))
    config = AgentConfig(max_steps=10, lease_minutes=1, lease_renew_seconds=0.05)
    agent = make_agent(env, llm, config=config)
    assert agent.run_task("慢任务", task_id=tid) == "收工"

    # 执行中（认领只给 1 分钟租约）观测到的租约已被续到 ~30 分钟后
    assert observed["lease"]
    assert datetime.fromisoformat(observed["lease"]) > \
        datetime.now(timezone.utc) + timedelta(minutes=10)
    assert tq.get_task(tid)["status"] == "done" and tq.get_task(tid)["lease_until"] is None
    assert agent._heartbeat is None and not _heartbeat_alive()


def test_agent_abort_stops_heartbeat_and_fails_task(env):
    """硬中断：步边界消费 abort → 任务 fail、心跳立即停止（不靠 ClaimError 自退）。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "将被中断的任务")
    llm = _SlowFirstLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "不应到这"})]},
    ], delay=0.2, on_start=None)
    config = AgentConfig(max_steps=10, lease_minutes=1, lease_renew_seconds=0.05)
    agent = make_agent(env, llm, config=config)
    llm.on_start = agent.request_abort  # 首个 LLM 步进行中置中断请求

    assert agent.run_task("将被中断的任务", task_id=tid) == ""
    row = tq.get_task(tid)
    assert row["status"] == "failed" and row["result_note"] == "人工中断"
    assert row["lease_until"] is None
    assert agent._heartbeat is None and not _heartbeat_alive()


def test_llm_exception_fails_task_and_stops_heartbeat(env):
    """worker 异常兜底：llm.chat 抛出（典型=传输层重试耗尽）穿出 _loop 时任务必须
    fail（含异常注记）且心跳停止——否则任务悬 claimed + 孤儿心跳续租占坑，
    expire_leases 永不回收，看板永久「执行中」（2026-09-17 真机事故根因）。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([])

    def boom(messages, **kw):
        raise RuntimeError("connection reset（重试 3 次耗尽）")
    llm.chat = boom
    config = AgentConfig(max_steps=10, lease_minutes=1, lease_renew_seconds=0.05)
    agent = make_agent(env, llm, config=config)
    # 委托定窗本会话（run_session 才会认领；complete 前主循环即炸）
    tid = tq.publish(project["id"], "将被 LLM 异常打断的任务",
                     target_session=agent.session["id"])

    with pytest.raises(RuntimeError):
        agent.run_session()  # 认领该任务后首步即炸
    row = tq.get_task(tid)
    assert row["status"] == "failed"
    assert "RuntimeError" in (row["result_note"] or "")
    assert row["lease_until"] is None  # 心跳已停，租约随收尾释放
    assert agent._heartbeat is None and not _heartbeat_alive()
    # 审计：task.failed 落事件
    assert any(e["kind"] == "task.failed"
               for e in bb.recent_events(project["id"]))


def test_salvage_on_error_saves_partial_conclusions(env):
    """fail 抢救收尾（2026-09-20，借鉴 Intentest 降级段）：worker 异常兜底 fail 前，
    用一轮无工具纯文本 LLM 把现场里的部分结论落盘为 salvage 产物——任务虽败，
    已验证事实/失败方向不陪葬。断言：artifact(kind=salvage) 落库 + salvage 产物
    文件落盘 + task.salvaged 事件 + 抢救调用 system=SALVAGE_SYSTEM 且无工具。"""
    bb, project, gw, tq, tmp_path = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "bb_add_asset",
                                            {"type": "domain", "value": "x.com"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_add_asset",
                                            {"type": "subdomain", "value": "dev.x.com"})]},
        RuntimeError("connection reset（重试 3 次耗尽）"),  # 主循环炸在这里
        {"text": "## 已确认事实\n- x.com 已登记（asset，见黑板）\n"
                 "## 失败/死路方向\n无\n## 接手建议\n续查 DNS CNAME。"},
    ])
    config = AgentConfig(max_steps=10, lease_minutes=1, lease_renew_seconds=0.05)
    agent = make_agent(env, llm, config=config,
                       artifacts_dir=str(tmp_path / "artifacts"))

    tid = tq.publish(project["id"], "异常中断但有结论的任务",
                     target_session=agent.session["id"])
    with pytest.raises(RuntimeError):
        agent.run_session()
    row = tq.get_task(tid)
    assert row["status"] == "failed"  # 抢救不影响 fail 本身
    arts = bb.list_artifacts(project["id"], task_id=tid)
    assert len(arts) == 1 and arts[0]["kind"] == "salvage"
    assert "connection reset" in arts[0]["description"]  # 描述含终止原因
    salvage_file = tmp_path / "artifacts" / "salvage" / f"salvage-{tid}.md"
    assert salvage_file.is_file()
    assert "已确认事实" in salvage_file.read_text(encoding="utf-8")
    ev = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.salvaged"]
    assert len(ev) == 1 and ev[-1]["payload"]["task_id"] == tid
    # 抢救调用形态：无工具（纯文本）、system 是抢救员提示、prompt 含终止原因
    last = llm.calls[-1]
    assert "抢救" in last["system"] and last["messages"][-1]["role"] == "user"
    assert "connection reset" in json.dumps(last["messages"], ensure_ascii=False)


def test_salvage_llm_failure_does_not_break_fail(env):
    """抢救 LLM 也炸（典型=配额故障）→ 静默跳过，fail 照常完成、无 salvage 产物。"""
    bb, project, gw, tq, tmp_path = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "bb_add_asset",
                                            {"type": "domain", "value": "y.com"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_add_asset",
                                            {"type": "domain", "value": "z.com"})]},
        RuntimeError("主循环炸"),
        RuntimeError("抢救 LLM 也炸（配额 429）"),
    ])
    config = AgentConfig(max_steps=10, lease_minutes=1, lease_renew_seconds=0.05)
    agent = make_agent(env, llm, config=config,
                       artifacts_dir=str(tmp_path / "artifacts"))

    tid = tq.publish(project["id"], "抢救也救不了的任务",
                     target_session=agent.session["id"])
    with pytest.raises(RuntimeError):
        agent.run_session()
    row = tq.get_task(tid)
    assert row["status"] == "failed" and "RuntimeError" in (row["result_note"] or "")
    assert agent._heartbeat is None
    assert bb.list_artifacts(project["id"]) == []
    assert not any(e["kind"] == "task.salvaged"
                   for e in bb.recent_events(project["id"]))
    # 现场清空：正常路径（含暂停）不留陈旧 salvage 现场
    assert agent._salvage_ctx is None


def test_deleted_claimed_task_aborts_at_control_point(env):
    """A1：claimed 任务在看板被取消，当前步工具做完后控制点立即感知 →
    按人工中断收尾（不调 fail，行已不存在）、会话 idle、心跳停。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "将被取消的任务")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "不应到这"})]},
    ])
    agent = make_agent(env, llm)
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            tq.delete(tid)  # 首步工具进行中删任务：必须等当前步做完
        return r
    llm.chat = chat

    assert agent.run_task("将被取消的任务", task_id=tid) == ""
    assert n["c"] == 1  # 第二步 LLM 未发生，控制点直接终止循环
    assert tq.get_task(tid) is None
    assert bb.list_sessions(project["id"])[0]["status"] == "idle"
    ev = [e for e in bb.recent_events(project["id"]) if e["kind"] == "session.aborted"]
    assert ev and ev[-1]["payload"]["note"] == "任务已被删除"
    assert agent._heartbeat is None and not _heartbeat_alive()
    # 没有对已删行 fail
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "task.failed" and e["payload"].get("task_id") == tid]
    # worker 一次性闸门退出，不认领新任务
    assert agent.run_session() is None


def test_snapshot_resume_after_task_deleted_claims_new(env):
    """A1：暂停快照里的任务在暂停期被删 → 丢快照，正常认领队列里的新任务。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "暂停后被删的任务")
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            agent.request_pause()
        return r
    llm.chat = chat

    assert agent.run_task("暂停后被删", task_id=tid) == ""
    assert agent._resume_state is not None
    tq.delete(tid)  # 暂停期人工取消
    other = tq.publish(project["id"], "新任务",
                       target_session=agent.session["id"])
    # 恢复后新任务的剧本（complete 即收尾，返回注记）
    llm.script.extend([
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task",
                                            {"result_note": "收工"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2b", "complete_task",
                                            {"result_note": "收工"})]},
    ])

    agent._pause_req.clear()
    agent._abort_req.clear()
    agent.paused = False
    assert agent.run_session() == "收工"  # 快照失效 → 认领新任务并跑完
    assert tq.get_task(tid) is None
    assert tq.get_task(other)["status"] == "done"
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "session.aborted"]  # 删任务不是中断，不应留 aborted


# ---------- A2：先规划后动手（task_plan/task_step + 计划闸） ----------

def _plan_dispatcher(env, session_name="planner", **kw):
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq, _ = env
    sid = bb.register_session(project["id"], session_name)["id"]
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                       session_id=sid, author=sid, **kw)
    tid = tq.publish(project["id"], "有计划的活", task_type="generic")
    tq.claim(tid, sid)
    d.current_task_id = tid
    return d, tid


def test_raw_arguments_wrapper_unwrapped(env):
    """raw_arguments 解包垫片（2026-09-26）：单键 raw_arguments 包裹（dict 或
    JSON 字符串）自动解包平铺重派；内层裸十六进制等已知退化先容错修复再执行
    （写成功直接正常返回，不回提示——ark-code-latest 调 bb_upsert_func 连发
    6 次全中的退化序列化，模型自己改不掉，须平台兜底）；修复不了的语法残缺
    → [参数格式] 精确指引。"""
    bb, project, gw, tq, _ = env
    from core.agent.tools import ToolDispatcher
    sid = "sess-" + "r" * 12
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                       session_id=sid, author=sid)
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
    bb, project, gw, tq, _ = env
    from core.agent.tools import ToolDispatcher
    sid = "sess-" + "v" * 12
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
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
    d2 = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                        session_id="sess-" + "w" * 12, author="x")
    for name, args in (("strings_search", {"binary": "b"}),
                       ("func_xrefs", {"binary": "b", "name": "f"}),
                       ("list_symbols", {"binary": "b"})):
        assert d2.dispatch(name, args).startswith("[未装配]")
    # 只读侦察：认领后无计划也不撞计划闸
    d3, tid = _plan_dispatcher(env, session_name="revsearch",
                               decompiler=_FakeDecomp())
    r = d3.dispatch("strings_search", {"binary": "b.exe"})
    assert not r.startswith("[计划闸]") and json.loads(r)["count"] == 1


def test_plan_gate_blocks_until_planned(env):
    """空计划：实质工具被计划闸回填引导，命令不执行；只读工具放行；交计划后全放行。"""
    bb, project, gw, tq, _ = env
    d, tid = _plan_dispatcher(env)

    r = d.dispatch("run_cmd", {"cmd": "whoami", "runtime": "host", "threat_class": "trusted"})
    assert r.startswith("[计划闸]")
    assert not [e for e in bb.recent_events(project["id"]) if e["kind"] == "command"]
    assert "[计划闸]" not in d.dispatch("bb_query", {"what": "tasks"})
    assert "[计划闸]" not in d.dispatch("decompile", {"binary": "x.exe"})  # 未装配→[未装配]
    assert "[计划闸]" not in d.dispatch("list_symbols", {"binary": "x.exe"})
    # 收尾控制原语也不被闸挡住（允许直接放弃）
    assert "[计划闸]" not in d.dispatch("fail_task", {"result_note": "不干了"})
    assert tq.get_task(tid)["status"] == "failed"

    d2, tid2 = _plan_dispatcher(env, session_name="planner2")
    r = d2.dispatch("task_plan", {"steps": [{"title": "侦察"}, {"title": "利用"}]})
    assert "计划已记录" in r and "p1" in r and "p2" in r
    r = d2.dispatch("run_cmd", {"cmd": "whoami", "runtime": "host", "threat_class": "trusted"})
    assert not r.startswith("[计划闸]")
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "command" in kinds and "task.plan_set" in kinds


def test_plan_tools_always_allowed_by_role_whitelist(env):
    """task_plan/task_step 是协调原语：即使不在角色 tools 白名单内也恒放行。"""
    d, tid = _plan_dispatcher(env, allowed_tools=["bb_query"])
    assert "计划已记录" in d.dispatch("task_plan", {"steps": [{"title": "a"}]})
    r = d.dispatch("task_step", {"step_id": "p1", "status": "doing"})
    assert "p1 → doing" in r
    # 白名单外的实质工具：越界检查先于计划闸
    assert d.dispatch("bb_add_asset", {"type": "domain", "value": "x.com"}
                      ).startswith("[越界拒绝]")


def test_plan_step_tool_roundtrip(env):
    """task_step doing/done/blocked 全链路：自动转移、blocked 原因、完成计数。"""
    d, tid = _plan_dispatcher(env)
    d.dispatch("task_plan", {"steps": [{"title": "a"}, {"title": "b"}]})
    r = d.dispatch("task_step", {"step_id": "p1", "status": "doing"})
    assert "▶ p1" in r
    r = d.dispatch("task_step", {"step_id": "p2", "status": "doing"})
    assert "○ p1" in r and "▶ p2" in r  # p1 自动回 todo
    r = d.dispatch("task_step", {"step_id": "p2", "status": "blocked"})  # 无 note
    assert "blocked 必须" in r
    r = d.dispatch("task_step", {"step_id": "p2", "status": "blocked", "note": "等账号"})
    assert "阻塞：等账号" in r


def test_plan_gate_inactive_without_current_task(env):
    """无认领任务（直接给目标的旧式 run_task）不施加计划闸。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq, _ = env
    sid = bb.register_session(project["id"], "free")["id"]
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                       session_id=sid, author=sid)
    assert d.current_task_id is None
    r = d.dispatch("run_cmd", {"cmd": "whoami", "runtime": "host", "threat_class": "trusted"})
    assert not r.startswith("[计划闸]")


# ---------- tool.call 审计事件（2026-09-19「工具」tab） ----------

def _tool_calls(bb, pid):
    return [e for e in bb.recent_events(pid) if e["kind"] == "tool.call"]


def test_tool_call_event_success_and_gates(env):
    """普通工具调用落 tool.call（name/args/耗时/成败/结果头）；拒绝路径 ok=False 也落。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq, _ = env
    sid = bb.register_session(project["id"], "auditor")["id"]
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                       session_id=sid, author=sid)
    d.dispatch("bb_query", {"what": "tasks"})
    evs = _tool_calls(bb, project["id"])
    assert len(evs) == 1
    p = evs[0]["payload"]
    assert p["name"] == "bb_query" and p["args"] == {"what": "tasks"}
    assert p["ok"] is True and isinstance(p["duration_s"], (int, float))
    assert isinstance(p["result_head"], str) and p["result_head"]

    # 计划闸等拒绝路径同样可观测且 ok=False（run_cmd 虽被闸拦但不落 tool.call，见下测）
    d2, _tid = _plan_dispatcher(env, session_name="auditor2")
    r = d2.dispatch("bb_add_asset", {"type": "domain", "value": "x.com"})
    assert r.startswith("[计划闸]")
    evs2 = [e for e in _tool_calls(bb, project["id"]) if e["session_id"] != sid]
    assert len(evs2) == 1 and evs2[0]["payload"]["ok"] is False
    assert "[计划闸]" in evs2[0]["payload"]["result_head"]


def test_tool_call_skips_run_cmd_and_truncates_args(env):
    """run_cmd 不落 tool.call（gateway 已有 command/command.result）；长入参截断。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq, _ = env
    sid = bb.register_session(project["id"], "auditor3")["id"]
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
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

def test_agent_publish_task_forces_parent_and_author(env):
    """认领中发子任务：parent=当前任务、created_by=本会话；无认领任务时 parent 为空。"""
    d, tid = _plan_dispatcher(env, session_name="decomposer")
    sid = d.session_id
    d.dispatch("task_plan", {"steps": [{"title": "分解"}]})  # 过计划闸
    r = d.dispatch("publish_task", {"objective": "子任务：测 /api 注入", "task_type": "generic",
                                    "refs": ["find-0123456789ab"]})
    assert r.startswith("子任务已发布"), r
    child_id = r.split("子任务已发布: ")[1].split("（")[0]
    child = tq_get(env, child_id)
    assert child["parent_id"] == tid
    assert child["created_by"] == sid
    assert child["status"] == "open"
    assert "find-0123456789ab" in child["context_refs"]
    ev = [e for e in d.bb.recent_events(d.project_id)
          if e["kind"] == "task.published" and e["payload"]["task_id"] == child_id][0]
    assert ev["author"] == sid

    # 无当前任务（旧式直接目标会话）→ 顶层任务 parent 为空
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq2, _ = env
    sid2 = bb.register_session(project["id"], "free")["id"]
    d2 = ToolDispatcher(bb, gateway=gw, tq=tq2, project_id=project["id"],
                        session_id=sid2, author=sid2)
    r = d2.dispatch("publish_task", {"objective": "无 parent 的顶层任务"})
    top_id = r.split("子任务已发布: ")[1].split("（")[0]
    assert tq_get(env, top_id)["parent_id"] is None
    assert tq_get(env, top_id)["created_by"] == sid2


def tq_get(env, task_id):
    _, _, _, tq, _ = env
    return tq.get_task(task_id)


def test_agent_publish_task_obeys_plan_gate_and_role_limits(env):
    """publish_task 是实质工具：空计划被闸住；轨注册表外类型被拒；active 缺冲突键被拒。"""
    d, _ = _plan_dispatcher(env)
    # 空计划 → 计划闸（分解是计划的一部分，不在放行白名单）
    assert d.dispatch("publish_task", {"objective": "提前分解"}).startswith("[计划闸]")
    d.dispatch("task_plan", {"steps": [{"title": "分解"}]})

    # 非 passive 缺 conflict_keys → tq.publish 拒收
    r = d.dispatch("publish_task", {"objective": "打 IP", "noise_budget": "low"})
    assert r.startswith("[拒绝]") and "conflict_keys" in r

    # 轨注册表护栏：只认 recon / generic
    d.allowed_task_types = ["recon"]
    assert d.dispatch("publish_task", {"objective": "x", "task_type": "typo-type"}
                      ).startswith("[拒绝]")
    r = d.dispatch("publish_task", {"objective": "x", "task_type": "generic"})
    assert r.startswith("子任务已发布")


def test_agent_publish_task_empty_objective_rejected(env):
    d, _ = _plan_dispatcher(env)
    d.dispatch("task_plan", {"steps": [{"title": "分解"}]})
    r = d.dispatch("publish_task", {"objective": "   "})
    assert r.startswith("[错误]") and "objective" in r


# ---------- E8：步数预算与人工引导 ----------

def test_request_steps_gate_and_extension(env):
    """request_steps（E8）：剩余 >20 拒收防囤步；≤20 放行固定 +200 并落审计；
    未装配预算（max_steps=0）拒收。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq, _ = env
    sid = bb.register_session(project["id"], "budget")["id"]
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
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
    d2 = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                        session_id=sid2, author=sid2)
    assert d2.dispatch("request_steps", {}).startswith("[拒绝]")


def test_budget_exhaustion_pauses_not_fails_then_resumes(env):
    """步数耗尽（E8）：不再 fail——任务保持 claimed、会话 paused、快照 reason=budget、
    session.budget_paused 事件；恢复（API 同款 +200）后从断点续跑至完成。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "步数耗尽任务", task_type="generic")
    llm = ScriptedLLM([
        # E2 拒绝熔断（2026-09-22）：plan 为空时 run_cmd 被 [计划闸] 拒，连续 3 次
        # 会触发熔断挂起（任务 fail）——中间夹一次 bb_query（计划闸白名单）清零
        # 计数，保持本测「步数耗尽→暂停不 fail」的验证意图（命令也照旧不真执行）
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_query", {"what": "tasks"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "run_cmd",
                                            {"cmd": "c3", "runtime": "host",
                                             "threat_class": "trusted"})]},
        # 恢复后的续跑剧本（complete 即委托收尾，返回注记）
        {"tool_use": [ScriptedLLM.tool_call("t4", "complete_task",
                                            {"result_note": "干完了"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4b", "complete_task",
                                            {"result_note": "干完了"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=3))
    assert agent.run_task("步数耗尽演练", task_id=tid) == ""   # 暂停退出，非任务总结
    assert tq.get_task(tid)["status"] == "claimed"             # 不 fail（占坑语义保留给显式收尾）
    assert agent.paused is True
    assert agent._resume_state["reason"] == "budget"
    assert agent._resume_state["task_id"] == tid
    assert agent._resume_state["next_step"] == 3               # 断点=耗尽步
    assert agent._heartbeat is not None and agent._heartbeat.is_alive()  # 心跳保留
    evs = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "session.budget_paused" in evs and "session.finished" not in evs
    # 预算提醒已注入（剩余 <20 起每步边界）
    assert any("预算剩余" in json.dumps(c["messages"], ensure_ascii=False)
               for c in llm.calls)

    # 人类「继续」（resume 端点同款操作：+200 + 清标志 → run_next_task 续跑）
    old_max = agent.dispatcher.max_steps
    agent.dispatcher.max_steps = old_max + 200                 # resume 端点的默认增补
    agent._pause_req.clear()
    agent._abort_req.clear()
    agent.paused = False
    assert agent.run_session() == "干完了"
    assert tq.get_task(tid)["status"] == "done"
    # 扩展后的预算在同一会话跨任务生效
    assert agent.dispatcher.max_steps == old_max + 200


def test_budget_exhaustion_self_rescue_via_request_steps(env):
    """耗尽步号恢复且不加预算：循环仍给一轮对话，模型当场 request_steps 自救续跑。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "自救任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "run_cmd",
                                            {"cmd": "c2", "runtime": "host",
                                             "threat_class": "trusted"})]},
        # 恢复后耗尽断点：先自救申请，再收尾（complete 即委托收尾，返回注记）
        {"tool_use": [ScriptedLLM.tool_call("t3", "request_steps",
                                            {"reason": "还差最后一步"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "complete_task", {"result_note": "自救成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4b", "complete_task", {"result_note": "自救成功"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=2))
    assert agent.run_task("自救演练", task_id=tid) == ""
    agent._pause_req.clear()
    agent._abort_req.clear()
    agent.paused = False                                       # 不加预算直接恢复
    assert agent.run_session() == "自救成功"                  # request_steps +200 续上
    assert tq.get_task(tid)["status"] == "done"
    ev = [e for e in bb.recent_events(project["id"])
          if e["kind"] == "step.budget_extended"]
    assert ev and ev[-1]["payload"]["by"] == "agent"


def test_plan_gate_parallel_batch_counts_one_step_not_breaker(env):
    """2026-09-24 口径重构：一个并行批内 3 张计划闸拒绝票——
    教练计数只计 1 个模型步（阈值 2，不发强提示），硬熔断完全不触发；
    模型下一步照常能调 finish 收尾（旧逻辑当场 3 票熔断，没有改道机会）。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "并行撞闸任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [
            ScriptedLLM.tool_call("a", "run_cmd",
                                  {"cmd": "c1", "runtime": "host",
                                   "threat_class": "trusted"}),
            ScriptedLLM.tool_call("b", "run_cmd",
                                  {"cmd": "c2", "runtime": "host",
                                   "threat_class": "trusted"}),
            ScriptedLLM.tool_call("c", "run_cmd",
                                  {"cmd": "c3", "runtime": "host",
                                   "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("done", "complete_task",
                                            {"result_note": "改道完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("f", "finish",
                                            {"summary": "改道完成"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10, closing_max_rounds=0))
    assert agent.run_task("并行撞闸演练", task_id=tid) == "改道完成"
    assert tq.get_task(tid)["status"] == "done"
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "agent.reject_breaker" not in kinds and "agent.plan_nudge" not in kinds
    assert agent._plan_gate_count == 1 and agent._reject_streak == 0


def test_plan_gate_coaching_chain_nudge_then_block(env):
    """计划闸教练链：累计 2 个模型步撞闸 → 强提示注入 + plan-only 工具面收缩
    （agent.plan_nudge）；强提示后下一步仍不写计划（纯文本）→ 挂人
    （agent.plan_gate_block + fail awaiting_human，现场快照可续跑）。"""
    from core.agent.loop import _PLAN_ONLY_NUDGE
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "教练链任务", task_type="generic")

    def gated_cmd(cid):
        return {"tool_use": [ScriptedLLM.tool_call(
            cid, "run_cmd", {"cmd": cid, "runtime": "host",
                             "threat_class": "trusted"})]}

    llm = ScriptedLLM([
        gated_cmd("c1"), gated_cmd("c2"),
        {"text": "我再想想……"},
    ])
    agent = make_agent(env, llm)
    assert agent.run_task("教练链演练", task_id=tid) == ""
    task = tq.get_task(tid)
    assert task["status"] == "failed" and task["blocked_reason"] == "awaiting_human"
    kinds = {e["kind"] for e in bb.recent_events(project["id"])}
    assert "agent.plan_nudge" in kinds and "agent.plan_gate_block" in kinds
    assert "agent.reject_breaker" not in kinds
    # 第 3 次 chat（强提示后）消息里带强提示原文
    assert _PLAN_ONLY_NUDGE in json.dumps(llm.calls[2]["messages"], ensure_ascii=False)
    # plan-only 工具面收缩：只剩计划/控制原语
    agent.dispatcher.plan_only_mode = True
    names = {t["name"] for t in agent._task_tool_schemas()}
    assert "run_cmd" not in names and "task_plan" in names and "finish" in names


def test_hard_reject_breaker_per_model_step_with_reset(env):
    """硬熔断按模型步计：并行批 3 张硬拒绝票只计 1 步不熔断；夹一个无硬拒绝步
    清零；随后连续 3 个硬拒绝步才熔断（agent.reject_breaker streak=3）。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "硬熔断任务", task_type="generic")

    def malware_batch(cid, n=1):
        return {"tool_use": [
            ScriptedLLM.tool_call(f"{cid}-{i}", "run_cmd",
                                  {"cmd": f"evil-{i}", "runtime": "host",
                                   "threat_class": "malware_live"})
            for i in range(n)]}

    llm = ScriptedLLM([
        # 先过计划闸（否则 run_cmd 到不了网关，走教练链而非硬熔断）
        {"tool_use": [ScriptedLLM.tool_call(
            "p", "task_plan", {"steps": [{"title": "硬拒绝演练"}]})]},
        malware_batch("s1", 3),                                        # streak 1
        {"tool_use": [ScriptedLLM.tool_call("q", "bb_query",
                                            {"what": "tasks"})]},       # 清零
        malware_batch("s3"),                                           # streak 1
        malware_batch("s4"),                                           # streak 2
        malware_batch("s5"),                                           # streak 3 → 熔断
    ])
    agent = make_agent(env, llm)
    assert agent.run_task("硬熔断演练", task_id=tid) == ""
    task = tq.get_task(tid)
    assert task["status"] == "failed" and task["blocked_reason"] == "awaiting_human"
    br = [e for e in bb.recent_events(project["id"])
          if e["kind"] == "agent.reject_breaker"]
    assert len(br) == 1 and br[-1]["payload"]["streak"] == 3


def test_search_files_workspace_only_and_hits(env):
    """search_files（2026-09-24）：非 shell 工作区内容检索——
    正则/子串命中、越界路径拒绝、无命中回执；计划前可调（计划闸白名单）。"""
    bb, project, gw, tq, tmp_path = env
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
    # 越出工作区 → 拒绝
    assert d.dispatch("search_files",
                      {"pattern": "a", "path": "../../../../"}
                      ).startswith("[拒绝]")
    # 无命中
    assert d.dispatch("search_files",
                      {"pattern": "zzz-no-such-xyz", "path": "../spill"}
                      ).startswith("[无命中]")
    # 非法正则
    assert d.dispatch("search_files",
                      {"pattern": "([unclosed", "path": "../spill"}
                      ).startswith("[错误]")


def test_run_cmd_plan_gate_rejection_audited_and_skill_open_preallowed(env):
    """P3（2026-09-24）：run_cmd 被计划闸挡回时落 tool.call 审计（gated=true，
    ok=false）——此前这类拒绝事件流完全隐形；skill_open 空计划下允许先行。"""
    bb, project, gw, tq, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    tid = tq.publish(project["id"], "审计可见性任务", task_type="generic")
    tq.claim(tid, agent.session["id"])
    d = agent.dispatcher
    d.current_task_id = tid
    r = d.dispatch("run_cmd",
                    {"cmd": "grep x ../spill/y", "runtime": "host",
                     "threat_class": "trusted"})
    assert r.startswith("[计划闸]")
    calls = [e["payload"] for e in bb.recent_events(project["id"])
             if e["kind"] == "tool.call" and e["payload"]["name"] == "run_cmd"]
    assert calls and calls[-1]["gated"] is True and calls[-1]["ok"] is False
    # skill_open 只是读手册：空计划下不再被闸
    rs = d.dispatch("skill_open", {"name": "demo"})
    assert not rs.startswith("[计划闸]") and "按步骤执行" in rs


def test_bb_query_findings_filters_passthrough(env):
    """bb-query-filters M1：findings 的 min_severity/verified_only/category
    透传底层 store（能力早已实现、工具层没接出）。"""
    from core.blackboard.assets import register_asset
    bb, project, gw, tq, _ = env
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
    bb, project, gw, tq, _ = env
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


def test_bb_query_tasks_assets_filters_and_closed_set_errors(env):
    """M1：tasks status、assets tag 过滤；闭集非法值显式 [错误]
    （不静默返回空结果被误判为「没有」）。"""
    from core.blackboard.assets import register_asset
    bb, project, gw, tq, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    pid = project["id"]
    t1 = tq.publish(pid, "开着的任务", task_type="generic")
    t2 = tq.publish(pid, "另一个", task_type="generic")
    tq.claim(t2, agent.session["id"])
    tq.complete(t2, agent.session["id"], result_note="ok")
    register_asset(bb, pid, "10.0.0.1", "host",
                   meta={"tags": ["靶标"]}, quiet=True)
    register_asset(bb, pid, "10.0.0.2", "host", quiet=True)
    rows = lambda s: json.loads(s)
    # tasks status
    assert [t["id"] for t in rows(d.dispatch(
        "bb_query", {"what": "tasks", "status": "done"}))] == [t2]
    assert [t["id"] for t in rows(d.dispatch(
        "bb_query", {"what": "tasks", "status": "open"}))] == [t1]
    # assets tag（大小写不敏感）
    got = rows(d.dispatch("bb_query", {"what": "assets", "tag": "靶标"}))
    assert [a["value"] for a in got] == ["10.0.0.1"]
    # 闭集非法值
    assert d.dispatch("bb_query",
                      {"what": "findings", "min_severity": "urgent"}).startswith("[错误]")
    assert d.dispatch("bb_query",
                      {"what": "tasks", "status": "nope"}).startswith("[错误]")
    assert d.dispatch("bb_query",
                      {"what": "assets", "status": "nope"}).startswith("[错误]")
    assert d.dispatch("bb_query",
                      {"what": "assets", "type": "printer"}).startswith("[错误]")
    # 非法值不产生误报：open 任务仍在、资产仍全量
    assert len(rows(d.dispatch("bb_query", {"what": "tasks"}))) == 2
    assert len(rows(d.dispatch("bb_query", {"what": "assets"}))) == 2


def test_bb_query_events_kinds_and_limit(env):
    """M2：events 的 kinds + session_id 过滤端到端；六面统一 limit——
    events 走 tail、其余面应用层裁剪；非法 limit 钳 1-200。"""
    bb, project, gw, tq, _ = env
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


def test_human_note_deferred_to_round_end(env):
    """human_note 轮末语义（2026-09-19）：任务中途投递的引导**不**在步边界注入
    （不打断当前任务思路），留收件箱未读，延迟到下一轮认领期注入。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "被引导的任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "本轮完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "下轮收到引导"})]},
    ])
    agent = make_agent(env, llm)
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:  # 第 1 步期间投递引导（旧行为会在第 2 步边界注入）
            bb.post_human_note(project["id"], agent.session["id"], "优先看备份文件")
        return r
    llm.chat = chat

    assert agent.run_task("引导演练", task_id=tid) == "本轮完成"
    second = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "💬 人类引导" not in second and "优先看备份文件" not in second
    # 下一轮认领期 drain：开场消息即携带引导
    tid2 = tq.publish(project["id"], "下轮任务", task_type="generic")
    assert agent.run_task("下一轮", task_id=tid2) == "下轮收到引导"
    first = json.dumps(llm.calls[2]["messages"], ensure_ascii=False)
    assert "💬 人类引导" in first and "优先看备份文件" in first


def test_run_chat_replies_without_task(env):
    """空闲对话轮（2026-09-19）：无任务上下文直接对话——drain 未读 human_note →
    LLM 回复落 `agent.chat` 事件并返回文本；可带工具查黑板；finish 不在工具集。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("c1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"text": "你好，我是通用测试员。"},
    ])
    agent = make_agent(env, llm)
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
    bb, project, gw, tq, _ = env
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


def test_run_chat_task_continuation_with_context(env):
    """会话中心化（2026-09-25）：终态委托的上下文延续——委托 complete 收尾时
    「🧭 委托目标+收尾摘要」沉淀进会话级 chat-<sid>.json；随后无参 run_chat()
    从该文件载入既往对话作上下文（模型看得到任务目标与既往结论）；回复后问答对
    回写同一文件，下轮续聊/重启后仍带全上下文。"""
    bb, project, gw, tq, tmp_path = env
    artifacts = tmp_path / "proj" / "artifacts"
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "已在 p1 确认 strings 输出"})]},
        # D6：首次申报进确认轮，零新增再申报落定（沉淀摘要=收尾注记）
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "已在 p1 确认 strings 输出"})]},
        # 续聊回复
        {"text": "接着 p1 的结论，p2 反编译主函数。"},
    ])
    agent = make_agent(env, llm, artifacts_dir=artifacts)
    sid = agent.session["id"]
    tid = tq.publish(project["id"], "找 flag", task_type="generic",
                     target_session=sid)
    # 委托跑完：complete 即收尾，「委托目标+收尾摘要」沉淀进会话 chat 文件
    assert agent.run_session() == "已在 p1 确认 strings 输出"
    cpath = artifacts.parent / "snapshots" / f"chat-{sid}.json"
    seed = json.dumps(json.loads(cpath.read_text(encoding="utf-8")),
                      ensure_ascii=False)
    assert "找 flag" in seed and "strings 输出" in seed
    # 人类续聊：run_chat 无 task_id，上下文取会话 chat 文件
    bb.post_human_note(project["id"], sid, "继续 p2")
    reply = agent.run_chat()
    assert reply == "接着 p1 的结论，p2 反编译主函数。"
    # 上下文注入：本轮 messages 含任务目标 + 既往结论
    first = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
    assert "找 flag" in first and "strings 输出" in first
    # 问答对回写会话 chat 文件（下轮续聊/重启后仍带全上下文）
    st = json.loads(cpath.read_text(encoding="utf-8"))
    assert "继续 p2" in st["messages"][-2]["content"]  # 人类引导原文回写
    assert "p2 反编译主函数" in json.dumps(st["messages"][-1], ensure_ascii=False)
    # 回复照常落 agent.chat 事件
    chat_evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "agent.chat"]
    assert chat_evs and "p2" in chat_evs[-1]["payload"]["text"]


def test_run_chat_streams_reply_and_prunes_deltas(env):
    """对话化（2026-09-20）：run_chat 流式回复——ScriptedLLM 回放 text_deltas，
    主线程节流落 agent.chat.delta（累计全文、seq 递增、同 stream_id）；终稿
    agent.chat 带同 stream_id，落库后同流 delta 行被清剪（审计只留终稿）。"""
    bb, project, gw, tq, _ = env
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
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "c1", "declare_intent",
            {"statement": "后台存在默认弱口令 admin/admin（人类引导线索，"
                          "需实测登录验证）"})]},
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


def test_bb_add_finding_task_requires_own_intent(env):
    """2026-09-27 agent-loop 修复：意图门禁只对任务上下文生效——认领任务期间
    没有本会话 open 意图时登记发现仍被硬拒（任务尝试树不允许游离发现）。"""
    bb, project, gw, tq, _ = env
    from core.agent.tools import ToolDispatcher
    sidA = bb.register_session(project["id"], "intent-gate-a")["id"]
    dA = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                        session_id=sidA, author=sidA)
    tid = tq.publish(project["id"], "带意图纪律的任务", task_type="generic")
    tq.claim(tid, sidA)
    dA.current_task_id = tid
    dA.dispatch("task_plan", {"steps": [{"id": "p1", "title": "探测", "status": "todo"}]})  # 先过 A2 计划闸
    # 无本会话 open 意图 → 拒绝
    r = dA.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T",
                                       "severity": "low"})
    assert r.startswith("[拒绝]") and "open 意图" in r
    # 声明意图后放行（意图先行闸：声明后补 command 事件模拟执行动作）
    r = dA.dispatch("declare_intent", {"statement": "对目标进行备份探测尝试"})
    assert r.startswith("intent=")
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=sidA, author=sidA)
    r = dA.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T",
                                       "severity": "low"})
    assert r.startswith("finding=")
    # 他人意图不算数：B 撞 A 的同陈述，B 必须有自己的意图才能登记
    sidB = bb.register_session(project["id"], "intent-gate-b")["id"]
    dB = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                        session_id=sidB, author=sidB)
    tidB = tq.publish(project["id"], "B 的任务", task_type="generic")
    tq.claim(tidB, sidB)
    dB.current_task_id = tidB
    dB.dispatch("task_plan", {"steps": [{"id": "p1", "title": "探测", "status": "todo"}]})
    r = dB.dispatch("declare_intent", {"statement": "对目标进行备份探测尝试"})
    assert r.startswith("intent=") and "复用" not in r  # 跨作者不再合并
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=sidB, author=sidB)
    r = dB.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "B",
                                       "severity": "low"})
    assert r.startswith("finding=")


def test_bb_add_finding_intent_requires_execution(env):
    """2026-09-28 意图先行（三段式收紧）：发现是「侦察→declare→执行→产出」链路
    的检验产物——① 对话轮（无任务上下文）同样受门禁约束；② declare 后立即落发现
    （事后补票，实测 sess-b9a539e3ebfe declare→finding 仅隔 12 秒）被拒；③ 意图
    声明后有实质执行（command 事件）才放行；declare_intent 恒放行不会死锁。"""
    bb, project, gw, tq, _ = env
    from core.agent.tools import ToolDispatcher
    sid = bb.register_session(project["id"], "intent-exec")["id"]
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                       session_id=sid, author=sid)
    # ① 对话轮（无 current_task_id）无意图 → 拒（统一门禁，不再豁免）
    r = d.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T"})
    assert r.startswith("[拒绝]") and "open 意图" in r
    # ② declare 后立即落发现 = 事后补票 → 拒
    r = d.dispatch("declare_intent", {"statement": "验证 /admin 是否存在未授权访问"})
    assert r.startswith("intent=")
    r = d.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T"})
    assert r.startswith("[拒绝]") and "执行动作" in r
    # ③ 意图声明后有实质执行（command 事件，run_cmd 落的审计）→ 放行
    bb.append_event(project["id"], "command", {"cmd": "curl -s http://x/admin"},
                    session_id=sid, author=sid)
    r = d.dispatch("bb_add_finding", {"vuln_class": "info-leak", "title": "T"})
    assert r.startswith("finding=")


def test_finish_gate_scoped_to_own_intents(env):
    """2026-09-27 agent-loop 修复：finish 意图闸只拦**本会话**未收尾意图——
    他人会话遗留的 open 意图不得堵住本会话收尾（B 无法合法关闭 A 的意图，
    此前只能被逼二次 finish 绕过，文案还误导 B 去关别人的意图）。"""
    bb, project, gw, tq, _ = env
    from core.agent.tools import ToolDispatcher
    sidA = bb.register_session(project["id"], "finish-gate-a")["id"]
    dA = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                        session_id=sidA, author=sidA)
    declare_intent(bb, project["id"], "A 的遗留假设", author=sidA)  # A 遗留 open
    sidB = bb.register_session(project["id"], "finish-gate-b")["id"]
    dB = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
                        session_id=sidB, author=sidB)
    # B 无任何未收尾意图 → 首次 finish 直接放行（不被 A 的遗留堵住）
    r = dB.dispatch("finish", {"summary": "B 收工"})
    assert r == "会话即将结束" and dB.finished
    # A 自己有未收尾意图 → 首次 finish 被拦，二次放行
    r = dA.dispatch("finish", {"summary": "A 收工"})
    assert r.startswith("[拒绝]") and "未收尾" in r
    assert not dA.finished
    r = dA.dispatch("finish", {"summary": "A 收工"})
    assert r == "会话即将结束" and dA.finished


def test_run_chat_step_cap(env):
    """对话轮步数上限：min(dispatcher.max_steps, config.chat_max_steps)——
    chat_max_steps=3 时最多 3 次 LLM 调用即退出（剧本 3 轮工具调用，若上限
    失效第 4 次调用会 pop 空剧本直接崩测试）。"""
    bb, project, gw, tq, _ = env
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
    bb, project, gw, tq, tmp_path = env
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


def test_snapshot_persisted_and_rehydrates(env):
    """暂停快照落盘（E8）：workspace 文件 + sessions.meta 指针；服务重启 rehydrate
    载回快照置回 paused，resume 从断点续跑完成并清理快照。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "重启可续任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10),
                       artifacts_dir=tmp_path / "proj" / "artifacts")
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            agent.request_pause()
        return r
    llm.chat = chat

    assert agent.run_task("重启演练", task_id=tid) == ""
    sid = agent.session["id"]
    snap = tmp_path / "proj" / "snapshots" / f"{sid}.json"
    assert snap.is_file()
    persisted = json.loads(snap.read_text(encoding="utf-8"))
    assert persisted["task_id"] == tid and persisted["reason"] == "pause"
    meta = json.loads(bb.get_session(sid)["meta"])
    assert meta["resume_snapshot"] == snap.name

    # 服务重启：新 AgentSession 附着既有 sessions 行（rehydrate）
    row = bb.get_session(sid)
    llm2 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task",
                                            {"result_note": "续跑成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2b", "complete_task",
                                            {"result_note": "续跑成功"})]},
    ])
    reborn = AgentSession(
        project_id=project["id"], bb=bb, gateway=gw, llm=llm2,
        packs_root=tmp_path / "packs", track="pentest", capabilities=["web"],
        role="_generalist", capability_prompt="", config=AgentConfig(max_steps=10),
        artifacts_dir=tmp_path / "proj" / "artifacts", existing_session=row)
    assert reborn.paused is True
    assert reborn._resume_state is not None
    assert reborn._resume_state["task_id"] == tid
    assert reborn._resume_state["next_step"] == 2
    assert reborn.dispatcher.max_steps == 10                   # 暂停时预算随快照还原

    # 人类「继续」→ 从快照续跑 → 快照文件与指针清理
    reborn.paused = False
    assert reborn.run_session() == "续跑成功"
    assert tq.get_task(tid)["status"] == "done"
    assert not snap.exists()
    assert json.loads(bb.get_session(sid)["meta"])["resume_snapshot"] is None


def test_pause_request_persists_snapshot_immediately(env):
    """v0.64 暂停请求/停机即时落盘：worker 还在步内（未到边界）时
    pause_snapshot_now() 已双写会话键+任务键快照，任务保持 claimed；模拟断电
    （worker 永远停在下一步）后 rehydrate 回 paused，可直接断点续跑至完成——
    重启不再把任务 fail 成「待人工」。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "即时落盘任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10),
                       artifacts_dir=tmp_path / "proj" / "artifacts")
    orig_chat = llm.chat
    n = {"c": 0}
    entered = threading.Event()
    release = threading.Event()
    killed = {"v": False}
    hang = threading.Event()

    def chat(messages, **kw):
        n["c"] += 1
        if n["c"] == 1:
            entered.set()
            release.wait()
            return orig_chat(messages, **kw)
        if killed["v"]:
            hang.wait()  # 模拟断电：worker 永远停在第二步（守护线程被弃）
        return orig_chat(messages, **kw)
    llm.chat = chat

    th = threading.Thread(target=lambda: agent.run_task("即时落盘", task_id=tid),
                          daemon=True)
    th.start()
    assert entered.wait(5)
    # API 线程视角（pause 端点/停机钩子同款调用）：worker 尚在步 1 内
    assert agent.dispatcher.current_task_id == tid
    assert agent.pause_snapshot_now() is True
    sid = agent.session["id"]
    snap_dir = tmp_path / "proj" / "snapshots"
    snap = snap_dir / f"{sid}.json"
    task_snap = snap_dir / f"task-{tid}.resume.json"
    assert snap.is_file() and task_snap.is_file()
    persisted = json.loads(snap.read_text(encoding="utf-8"))
    assert persisted["task_id"] == tid and persisted["reason"] == "pause"
    assert persisted["next_step"] == 2
    assert json.loads(bb.get_session(sid)["meta"])["resume_snapshot"] == snap.name
    assert tq.get_task(tid)["status"] == "claimed"     # 不 fail 不动任务
    assert agent.paused is False                       # 不动 worker 生命周期

    # 模拟断电：worker 永远出不了第二步
    killed["v"] = True
    release.set()

    # 重启：rehydrate（清扫已豁免其 claimed 任务——本测试直接验快照链路）
    reborn = AgentSession(
        project_id=project["id"], bb=bb, gateway=gw,
        llm=ScriptedLLM([
            {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task",
                                                {"result_note": "即时快照续跑成功"})]},
            {"tool_use": [ScriptedLLM.tool_call("t2b", "complete_task",
                                                {"result_note": "即时快照续跑成功"})]},
        ]),
        packs_root=tmp_path / "packs", track="pentest", capabilities=["web"],
        role="_generalist", capability_prompt="", config=AgentConfig(max_steps=10),
        artifacts_dir=tmp_path / "proj" / "artifacts", existing_session=bb.get_session(sid))
    assert reborn.paused is True
    assert reborn._resume_state["task_id"] == tid
    assert reborn._resume_state["reason"] == "pause"
    assert reborn.paused and tq.get_task(tid)["status"] == "claimed"
    reborn.paused = False
    assert reborn.run_session() == "即时快照续跑成功"
    assert tq.get_task(tid)["status"] == "done"
    assert not snap.exists() and not task_snap.exists()   # 消费即清理（含任务键）


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
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call(
        "t1", "finish", {"summary": "完"})]}])
    agent = make_agent(env, llm)
    assert agent.run_task("现场清理") == "完"
    assert agent._live_state is None


def test_abort_keeps_snapshot_and_task_resumable(env):
    """E12：人工中断不再销毁落盘快照——task.failed 带 resumable 标记、
    快照文件+meta 指针保留；reopen+claim 后 revive_snapshot 复活续跑至完成并清理。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "中断可续任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10),
                       artifacts_dir=tmp_path / "proj" / "artifacts")
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            agent.request_pause()   # 暂停 → 快照落盘（可续现场）
        return r
    llm.chat = chat

    assert agent.run_task("中断演练", task_id=tid) == ""
    sid = agent.session["id"]
    snap = tmp_path / "proj" / "snapshots" / f"{sid}.json"
    assert snap.is_file()

    # 暂停态被硬中断（abort 端点对无 job 会话同款路径）
    agent._abort_current_task()
    assert tq.get_task(tid)["status"] == "failed"
    failed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.failed"][-1]
    assert failed["payload"]["resumable"] is True
    assert failed["payload"]["note"] == "人工中断"
    assert snap.is_file()                                    # 现场保留（不再销毁）
    assert json.loads(bb.get_session(sid)["meta"])["resume_snapshot"] == snap.name
    assert agent.paused is False and agent._resume_state is None

    # 看板「▶ 续跑」（resume 端点同款编排）：reopen → claim → revive → run_next_task
    st = agent.revive_snapshot(tid)
    assert st is not None and st["task_id"] == tid
    tq.reopen(tid, by="human")
    tq.claim(tid, sid, lease_minutes=agent.config.lease_minutes)
    agent._stop_after_task = False                           # 清中断一次性闸门
    llm2 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task",
                                            {"result_note": "续跑成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2b", "complete_task",
                                            {"result_note": "续跑成功"})]},
    ])
    agent.llm = llm2
    assert agent.run_session() == "续跑成功"
    assert tq.get_task(tid)["status"] == "done"
    assert not snap.exists()                                 # 快照消费后清理
    assert json.loads(bb.get_session(sid)["meta"])["resume_snapshot"] is None


def test_abort_after_task_deleted_clears_snapshot(env):
    """E12：任务已被删除（看板取消）时中断——快照作废并清理，防孤儿文件。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "删除收尾任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10),
                       artifacts_dir=tmp_path / "proj" / "artifacts")
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            agent.request_pause()
        return r
    llm.chat = chat

    assert agent.run_task("删除演练", task_id=tid) == ""
    sid = agent.session["id"]
    snap = tmp_path / "proj" / "snapshots" / f"{sid}.json"
    assert snap.is_file()

    tq.delete(tid, by="human")                               # claimed 任务可物理删除（A1）
    agent._abort_current_task()
    aborted = [e for e in bb.recent_events(project["id"]) if e["kind"] == "session.aborted"][-1]
    assert aborted["payload"]["note"] == "任务已被删除"
    assert not snap.exists()                                 # 任务没了快照即作废
    assert json.loads(bb.get_session(sid)["meta"])["resume_snapshot"] is None


# ---------- E6/E7 资产登记统一入口与扫描/测试状态机 ----------

def _dispatcher(env, name="assets"):
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq, _ = env
    sid = bb.register_session(project["id"], name)["id"]
    return ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
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
    assert [a["id"] for a in q] == [aid] and q[0]["status"] == "tested_clean"
    q2 = json.loads(d.dispatch("bb_query", {"what": "assets", "type": "host", "status": "open"}))
    assert all(a["type"] == "host" and a["status"] == "open" for a in q2)


def test_awaiting_human_pauses_with_snapshot_then_reusable(env):
    """C1：fail_task(awaiting_human)——快照落盘 + 任务 fail（blocked_reason=
    awaiting_human，resumable）；会话不结束、worker 继续认领下一任务；
    revive 复活续跑完成；认领注入旧计划注记。"""
    bb, project, gw, tq, tmp_path = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "t0", "task_plan", {"steps": [{"title": "摸底"}]})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t1", "fail_task", {"result_note": "ROE 未核验，等人类",
                                 "blocked_reason": "awaiting_human"})]},
        # worker 继续认领 t2 并完成（会话不结束）
        {"tool_use": [ScriptedLLM.tool_call(
            "t2", "complete_task", {"result_note": "t2 完成"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t2b", "complete_task", {"result_note": "t2 完成"})]},
        # revive 续跑 t1 的剧本（complete 即收尾，返回注记）
        {"tool_use": [ScriptedLLM.tool_call(
            "t4", "complete_task", {"result_note": "t1 续跑成功"})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t4b", "complete_task", {"result_note": "t1 续跑成功"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10),
                       artifacts_dir=tmp_path / "proj" / "artifacts")
    t1 = tq.publish(project["id"], "等 ROE 的任务", task_type="generic",
                    target_session=agent.session["id"])
    t2 = tq.publish(project["id"], "不需要等待的任务", task_type="generic",
                    target_session=agent.session["id"])
    assert agent.run_session() == ""              # t1 挂起 → 空串，worker 继续认领
    assert agent.paused is False                     # 不进 paused（区别于 budget 暂停）
    row1 = tq.get_task(t1)
    assert row1["status"] == "failed" and row1["blocked_reason"] == "awaiting_human"
    sid = agent.session["id"]
    snap = tmp_path / "proj" / "snapshots" / f"{sid}.json"
    assert snap.is_file()                            # 现场保留（awaiting 快照）
    failed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.failed"][-1]
    assert failed["payload"]["resumable"] is True
    # worker 继续认领 t2（同一 job 内）；complete 即收尾，返回 t2 收尾注记
    assert agent.run_session() == "t2 完成"
    assert tq.get_task(t2)["status"] == "done"

    # 人类续跑 t1：revive → reopen → claim → run_next_task
    st = agent.revive_snapshot(t1)
    assert st is not None and st["reason"] == "awaiting"
    tq.reopen(t1, by="human", note="ROE 已核验")
    assert "人类补充（human）: ROE 已核验" in tq.get_task(t1)["result_note"]  # complete 覆盖前可见
    tq.claim(t1, sid, lease_minutes=agent.config.lease_minutes)
    agent._stop_after_task = False
    assert agent.run_session() == "t1 续跑成功"
    assert tq.get_task(t1)["status"] == "done"
    assert not snap.exists()


def test_reclaim_notice_injects_old_plan(env):
    """C1：重新认领曾执行过的任务（无快照/新会话）→ 注入旧计划注记（done 未经
    本会话验证不视为已验证），防新会话误读旧进度。"""
    bb, project, gw, tq, _ = env
    t1 = tq.publish(project["id"], "被接手的任务", task_type="generic")
    s1 = bb.register_session(project["id"], "S1")
    tq.claim(t1, s1["id"])
    tq.set_plan(t1, s1["id"], [{"title": "摸底"}])
    tq.fail(t1, s1["id"], "挂起", blocked_reason="awaiting_human")
    tq.reopen(t1, by="human")  # 放回后由新会话认领（清持有方）
    llm2 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "c1", "complete_task", {"result_note": "接手完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("c2", "finish", {"summary": "接手成功"})]},
    ])
    agent2 = make_agent(env, llm2)
    agent2.run_task("被接手的任务", task_id=t1)
    injected = json.dumps(llm2.calls[0]["messages"], ensure_ascii=False)
    assert "旧计划仅供参考" in injected and "p1 [todo] 摸底" in injected


def test_dead_end_notice_injected_on_claim(env):
    """C3 跨轨路标三级注入：认领时 FP 发现按 scope 精确/同 host 聚合/计数注入。"""
    bb, project, gw, tq, _ = env
    a = bb.upsert_asset(project["id"], "host", "10.0.0.8")["id"]
    fid = bb.add_finding(project["id"], "死路", "8080 端口无服务", target_asset_id=a,
                          status="false-positive", author="s0")["id"]
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "知道了"})]},
    ])
    agent = make_agent(env, llm)
    tq.publish(project["id"], "打 10.0.0.8 的 443", task_type="recon",
               target_session=agent.session["id"])
    agent.run_session()
    injected = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    assert "路标" in injected and "勿重走" in injected


# ---------- v14 认领即换装（任务绑定角色） ----------

RECON_YAML = 'name: 侦察专才\npersona: "侦察专才人设。"\ntools: [run_cmd]\n'


def test_persona_switch_on_claim_and_restore(env):
    """认领带 role 任务即换装：prompt 段/工具白名单/执行 persona 切到任务角色；
    跑完恢复底色；事件与 attempts 履历贯穿。"""
    bb, project, gw, tq, _ = env
    write_role(env, "recon", RECON_YAML)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "完成"})]},
    ])
    agent = make_agent(env, llm)
    assert agent.base_role_name == "_generalist"
    tid = tq.publish(project["id"], "侦察任务", role="recon",
                     target_session=agent.session["id"])
    agent.run_session()  # 窗口无 role 限制：底色窗直接认领 role 任务
    assert tq.get_task(tid)["status"] == "done"
    # 换装生效：system prompt 含任务角色 persona；工具白名单段切到角色集
    assert "侦察专才人设。" in llm.calls[0]["system"]
    assert "通用测试员。" not in llm.calls[0]["system"]
    assert "工具白名单 run_cmd" in llm.calls[0]["system"]
    assert agent.dispatcher.current_persona_role is None  # 收尾已恢复
    # 恢复底色（幂等出口后状态干净）
    assert agent.role_name == "_generalist"
    assert agent._persona_saved is None
    # 事件 + 履历
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "session.persona_switched" in kinds
    att = tq.get_task(tid)["context"]["attempts"][0]
    assert att["role"] == "recon"  # 实际执行 persona，而非底色


def test_no_role_task_runs_in_base_persona(env):
    """无 role 任务按底色跑：不换装、不发 persona_switched。"""
    bb, project, gw, tq, _ = env
    write_role(env, "recon", RECON_YAML)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task", {"result_note": "完"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task", {"result_note": "完"})]},
    ])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "普通任务",
                     target_session=agent.session["id"])
    agent.run_session()
    assert tq.get_task(tid)["status"] == "done"
    assert agent.role_name == "_generalist"
    assert agent._persona_saved is None
    assert not any(e["kind"] == "session.persona_switched"
                   for e in bb.recent_events(project["id"]))


def test_persona_restore_after_fail_and_idle_claim(env):
    """失败收尾恢复底色；run_next_task 顶部兜底闸——换装态空手认领也先复位。"""
    bb, project, gw, tq, _ = env
    write_role(env, "recon", RECON_YAML)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "fail_task", {"result_note": "炸了"})]},
    ])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "会炸的任务", role="recon",
                     target_session=agent.session["id"])
    agent.run_session()
    assert tq.get_task(tid)["status"] == "failed"
    assert agent.role_name == "_generalist"  # fail 路径恢复底色
    # 兜底闸：人为制造换装残留（模拟泄漏路径），下一次 run_next_task 前强制复位
    agent._persona_saved = {"role_name": "_generalist", "role": {}, "max_noise": None,
                            "allowed_tools": None, "max_runtime": None,
                            "dispatcher_allowed_tools": None,
                            "dispatcher_max_runtime": None}
    agent.role_name = "recon"
    agent.dispatcher.current_persona_role = "recon"
    assert agent.run_session() is None  # 队列空
    assert agent.role_name == "_generalist"
    assert agent.dispatcher.current_persona_role is None


def test_persona_missing_role_file_runs_defensively(env):
    """任务 role 对应文件不存在（发布后被删）：防御性按当前角色跑，不换装不炸。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task", {"result_note": "完"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task", {"result_note": "完"})]},
    ])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "幽灵角色任务", role="ghost-role",
                     target_session=agent.session["id"])
    agent.run_session()
    assert tq.get_task(tid)["status"] == "done"
    assert agent.role_name == "_generalist"
    assert not any(e["kind"] == "session.persona_switched"
                   for e in bb.recent_events(project["id"]))


# ---------- 路由增强（2026-09-18）：未命中 kb 兜底 / task_type 加分 / kb 提示行 / kb_search ----------

def _kb_env(tmp_path):
    """给 packs/kb/web 配 kb 源（中文 H1 + frontmatter title + 首行摘要）。"""
    kb = tmp_path / "packs" / "kb" / "web"
    (kb / "poc").mkdir(parents=True, exist_ok=True)
    (kb / "poc" / "越权检测.md").write_text(
        "# 越权检测方法\n本篇汇总越权检测的完整路径。\n"
        "X9BODY_MARKER 完整正文细节只随 kb_open 注入", encoding="utf-8")
    (kb / "poc" / "tduck.md").write_text(
        "---\ntitle: 问卷系统越权合集\n---\n正文内容", encoding="utf-8")


def test_skill_miss_injects_kb_sources(env):
    """K8（2026-09-29）：废弃 top-1 路由命中——全量注入白名单/启用技能描述 +
    kb 源清单；skill.routed 事件改记录注入清单（不再有 kb_hits）。"""
    bb, project, gw, tq, tmp_path = env
    _kb_env(tmp_path)
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("t1", "finish",
                                                           {"summary": "完"})]}])
    agent = make_agent(env, llm)
    agent.run_task("整理资产清单")  # generalist：注入全部启用技能描述
    system = llm.calls[0]["system"]
    assert "可用技能清单" in system and "- demo：" in system
    assert "可用知识库源" in system and "web-kb" in system
    routed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "skill.routed"]
    assert routed and routed[-1]["payload"]["name"] is None
    assert "demo" in routed[-1]["payload"]["injected"]
    assert "kb_hits" not in routed[-1]["payload"]


def test_task_type_bonus_and_scope_in_claim_path(env):
    """认领队列任务：全量描述注入含 exp 技能；route_query 拼 scope；
    skill.routed 事件带 task_type 与注入清单。"""
    bb, project, gw, tq, tmp_path = env
    exp = tmp_path / "packs" / "capabilities" / "web" / "skills" / "exp"
    exp.mkdir(parents=True)
    (exp / "SKILL.md").write_text(
        "---\nname: exp\ndescription: 专用利用技能\ntask_types: exploit\n---\n利用步骤。",
        encoding="utf-8")
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("t1", "finish",
                                                           {"summary": "完"})]}])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "处理工单", scope="target.com",
                     task_type="exploit", created_by="human",
                     target_session=agent.session["id"])
    agent.run_session()
    system = llm.calls[0]["system"]
    assert "- exp：" in system  # 全量描述注入含 exp
    routed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "skill.routed"]
    assert "target.com" in routed[-1]["payload"]["query"]  # route_query 拼 scope
    assert routed[-1]["payload"]["task_type"] == "exploit"
    assert "exp" in routed[-1]["payload"]["injected"]


def test_kb_module_hint_lines(env):
    """K8（2026-09-29）：不再注入 📚 提示行与 kb_hits——kb 靠模型主动
    kb_search/kb_open 检索；源清单仍注入。"""
    bb, project, gw, tq, tmp_path = env
    _kb_env(tmp_path)
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("t1", "finish",
                                                           {"summary": "完"})]}])
    agent = make_agent(env, llm)
    agent.run_task("排查问卷系统越权问题")
    system = llm.calls[0]["system"]
    assert "📚 相关知识库模块" not in system
    assert "可用知识库源" in system and "web-kb" in system
    routed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "skill.routed"]
    assert "kb_hits" not in routed[-1]["payload"]
    assert "injected" in routed[-1]["payload"]


def test_kb_search_tool(env):
    """kb_search：多源全文检索 → path 即 kb_open module（M0 全局形态带域前缀）；落 kb.search 审计；空/无命中分支。"""
    bb, project, gw, tq, tmp_path = env
    _kb_env(tmp_path)
    llm = ScriptedLLM([])
    agent = make_agent(env, llm)
    d = agent.dispatcher
    r = d.dispatch("kb_search", {"query": "越权"})
    assert "web/poc/tduck.md" in r and "kb_open 的 module 参数" in r
    assert "kb.search" in [e["kind"] for e in bb.recent_events(project["id"])]
    assert "[拒绝]" in d.dispatch("kb_search", {"query": "  "})
    assert "[无命中]" in d.dispatch("kb_search", {"query": "不存在的专题xyz"})


def test_kb_search_plan_gate_and_whitelist(env):
    """kb_search 与 kb_open 同类：计划闸恒放行；角色 tools 白名单非空且不含它则拒绝。"""
    bb, project, gw, tq, tmp_path = env
    _kb_env(tmp_path)
    llm = ScriptedLLM([])
    agent = make_agent(env, llm)
    d = agent.dispatcher
    # 计划闸放行：认领无计划任务后 kb_search 仍可调（只读侦察）
    tid = tq.publish(project["id"], "检索知识", created_by="human")
    tq.claim(tid, agent.session["id"])
    d.current_task_id = tid
    assert "[计划闸]" not in d.dispatch("kb_search", {"query": "越权"})
    # 角色白名单约束：tools 非空且不含 kb_search → 越界拒绝
    write_role(env, "narrow", 'name: narrow\npersona: "窄白名单。"\ntools: [run_cmd]\n')
    agent2 = make_agent(env, ScriptedLLM([]), role="narrow")
    r = agent2.dispatcher.dispatch("kb_search", {"query": "越权"})
    assert "[越界拒绝]" in r


# ---------- v0.65：测试点路由索引 / 卡壳召回 / done 自动沉淀 ----------

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
    _, _, _, _, tmp_path = env
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
    assert "可用知识库源" in ctx
    assert "- demo：" in ctx  # generalist 注入全部启用技能描述
    # 白名单角色：只注入白名单内技能
    skill_dir = env[4] / "packs" / "capabilities" / "web" / "skills" / "up"
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
    assert "可用知识库源" in ctx3


def test_route_lookup_and_skill_open_tools(env):
    """G1 只读查询工具：route_lookup 按关键词查索引（角色裁剪同口径）；
    skill_open 打开技能全量正文；未知名回可用清单防幻觉。"""
    _write_web_pack(env)
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
    out = d.dispatch("route_lookup", {"query": "文件上传"})
    assert "文件上传测试" in out and "kb_open" in out
    assert d.dispatch("route_lookup", {"query": "zzz无关键词"}) .startswith("[无命中]")
    assert d.dispatch("route_lookup", {"query": ""}).startswith("[拒绝]")
    out_skill = d.dispatch("skill_open", {"name": "demo"})  # 轨级 demo 技能
    assert "按步骤执行" in out_skill
    assert d.dispatch("skill_open", {"name": "nope"}).startswith("[防幻觉]")
    bb0, project0 = env[0], env[1]
    evs = [e["kind"] for e in bb0.recent_events(project0["id"])]
    # skill.open 审计事件落了（route_lookup 走 tool.call 通用审计）
    assert "skill.open" in evs


def test_skill_context_digest_not_full_body(env):
    """K8 渐进披露：全量描述注入只给 name+description 行，技能正文不整段进
    system——正文靠 skill_open 按需打开。"""
    _write_web_pack(env)
    skill_dir = env[4] / "packs" / "capabilities" / "web" / "skills" / "up"
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
    script = [{"text": "……继续观察"} for _ in range(8)]
    script.append({"tool_use": [ScriptedLLM.tool_call("t9", "finish", {"summary": "完成"})]})
    llm = ScriptedLLM(script)
    agent = make_agent(env, llm, planner=planner,
                       config=AgentConfig(max_steps=15, stuck_after=3))
    agent.run_task("文件上传点卡住了")
    seen = json.dumps(planner.calls, ensure_ascii=False)
    assert "文件上传测试手册" in seen
    assert any("[策略顾问]" in json.dumps(c["messages"], ensure_ascii=False)
               for c in llm.calls)


def _sediment_planner(payload_json):
    return ScriptedLLM([{"text": payload_json}])


class _LazySedimentPlanner(ScriptedLLM):
    """复盘员替身：chat 时现取黑板真实 verified finding id 拼提案 JSON——
    experience-sedimentation M2 的证据锚点校验要求 reason 引用本任务真实产出
    的 finding id，剧本写死 id 做不到。模板里 $FID 占位符被替换。"""

    def __init__(self, bb, pid, template):
        super().__init__([])
        self._bb, self._pid, self._template = bb, pid, template

    def chat(self, messages, **kw):
        self.calls.append({"messages": messages})
        fid = next(f["id"] for f in self._bb.list_findings(self._pid)
                   if f["status"] == "verified")
        return self._parse({"content": [{"type": "text",
                                         "text": self._template.replace("$FID", fid)}],
                            "stop_reason": "end_turn",
                            "usage": {"input_tokens": 1, "output_tokens": 1}})


def _plan_call(cid="p0"):
    """过计划闸（认领任务后实质工具须先有计划）。"""
    return ScriptedLLM.tool_call(cid, "task_plan", {"steps": [{"title": "验证并沉淀"}]})


def _verified_finding_call(cid="f1"):
    """经验沉淀条件测试辅助：登记一条 verified 发现的工具调用（pentest 轨
    verified 门禁=repro_steps 复现证据）。须先过计划闸（见 _plan_call）。"""
    return ScriptedLLM.tool_call(cid, "bb_add_finding", {
        "vuln_class": "broken-access", "title": "越权读取他人订单", "severity": "high",
        "status": "verified",
        "evidence": {"repro_steps": [
            {"desc": "改 id 请求他人资源", "type": "http",
             "code": "GET /api/order/2 HTTP/1.1", "expected": "200 返回他人订单数据"}]}})


_PROPOSAL_JSON = json.dumps({
    "kind": "kb", "mode": "create",
    "target": {"kind": "kb", "cap": "web", "path": "poc/新经验.md"},
    "content": "# 新经验\n\n## 已验证路径\n- 换 id 差分请求返回他人数据。\n",
    "summary": "差分法验证有效", "reason": "任务证据：$FID 换 id 命中"
}, ensure_ascii=False)


def test_sediment_auto_proposal_on_complete(env):
    """done 自动提案（M1 条件触发）：产出 verified 发现的任务 complete_task →
    planner 复盘产 kb 提案草稿（人审批），reason 引用真实 finding 证据锚点。"""
    from core.skills import proposals
    _write_web_pack(env)
    bb, project, gw, tq, tmp_path = env
    task_id = tq.publish(project["id"], "越权测试", task_type="exploit", created_by="human")
    planner = _LazySedimentPlanner(bb, project["id"], _PROPOSAL_JSON)
    llm = ScriptedLLM([
        {"tool_use": [_plan_call(), _verified_finding_call()]},
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "换 id 差分验证成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "换 id 差分验证成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, planner=planner, enable_sediment=True)
    declare_intent(bb, project["id"], "对订单接口进行越权差分尝试",
                   author=agent.session["id"])  # 过 bb_add_finding 必挂意图门禁
    # 意图先行闸（2026-09-28）：补 command 事件模拟意图后的执行动作
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=agent.session["id"], author=agent.session["id"])
    agent.run_task("越权", task_id=task_id)
    pending = proposals.list_proposals(tmp_path / "packs", "pending")
    assert len(pending) == 1 and pending[0]["origin"] == "agent"
    assert pending[0]["task"] == task_id and pending[0]["target"]["cap"] == "web"
    fid = next(f["id"] for f in bb.list_findings(project["id"])
               if f["status"] == "verified")
    assert fid in pending[0]["reason"]  # M2 证据锚点：真实 finding id 进 reason
    kinds = [e["payload"]["kind"] for e in
             map(dict, bb.recent_events(project["id"], limit=50))
             if e["kind"] == "proposal.created"]
    assert "kb" in kinds  # 审计事件落了（origin=sediment 在 payload 里）


def test_sediment_skill_proposal_on_complete(env):
    """K7 done 自动提案 → skill 打法沉淀（④ kind=skill）：既有技能补段走 edit，
    提案 target 落能力包技能，reason 引用真实 finding 证据锚点（与 kb 同口径）。"""
    from core.skills import proposals
    _write_web_pack(env)
    bb, project, gw, tq, tmp_path = env
    # fixture 补一个 web 能力包技能（sediment 的 skill 对账/存在性校验用）
    sk_dir = tmp_path / "packs" / "capabilities" / "web" / "skills" / "web-skill"
    sk_dir.mkdir(parents=True, exist_ok=True)
    (sk_dir / "SKILL.md").write_text(
        "---\nname: web-skill\ndescription: Web 注入与认证测试技能\n---\n\n"
        "# 打法\n现有正文。\n", encoding="utf-8")
    task_id = tq.publish(project["id"], "越权测试", task_type="exploit", created_by="human")
    skill_json = json.dumps({
        "kind": "skill", "mode": "edit",
        "target": {"kind": "skill", "skill_kind": "capability",
                   "owner": "web", "name": "web-skill"},
        "content": "---\nname: web-skill\ndescription: Web 注入与认证测试技能\n---\n\n"
                   "# 打法\n\n## 已验证路径\n- 换 id 差分请求返回他人数据。\n",
        "summary": "越权打法补已验证路径", "reason": "任务证据：$FID 换 id 命中"
    }, ensure_ascii=False)
    planner = _LazySedimentPlanner(bb, project["id"], skill_json)
    llm = ScriptedLLM([
        {"tool_use": [_plan_call(), _verified_finding_call()]},
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "换 id 差分验证成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "换 id 差分验证成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, planner=planner, enable_sediment=True)
    declare_intent(bb, project["id"], "对订单接口进行越权差分尝试",
                   author=agent.session["id"])  # 过 bb_add_finding 必挂意图门禁
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=agent.session["id"], author=agent.session["id"])
    agent.run_task("越权", task_id=task_id)
    pending = proposals.list_proposals(tmp_path / "packs", "pending")
    assert len(pending) == 1 and pending[0]["target"]["kind"] == "skill"
    assert pending[0]["target"]["skill_kind"] == "capability"
    assert pending[0]["target"]["name"] == "web-skill"
    assert pending[0]["mode"] == "edit"
    fid = next(f["id"] for f in bb.list_findings(project["id"])
               if f["status"] == "verified")
    assert fid in pending[0]["reason"]  # K7 证据锚点：skill 与 kb 同口径


def test_sediment_no_output_done_skips_review(env):
    """M1 条件触发（减产核心）：done 但无 verified 产出 → 复盘不跑（planner 零调用）。"""
    _write_web_pack(env)
    bb, project, gw, tq, _ = env
    task_id = tq.publish(project["id"], "常规巡检", task_type="generic", created_by="human")
    planner = _sediment_planner(_PROPOSAL_JSON)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "扫完了没发现"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "扫完了没发现"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, planner=planner, enable_sediment=True)
    agent.run_task("巡检", task_id=task_id)
    assert planner.calls == []  # 无产出 done 不烧 LLM
    assert tq.get_task(task_id)["status"] == "done"


def test_sediment_failure_review_on_fail_task(env):
    """M1 失败模式复盘：fail_task（error）→ 复盘跑且 instruction 带失败模式口径，
    提案 reason 引用 task id 证据锚点（aborted/awaiting_human 不触发）。"""
    from core.skills import proposals
    _write_web_pack(env)
    bb, project, gw, tq, tmp_path = env
    task_id = tq.publish(project["id"], "打不穿的点", task_type="exploit", created_by="human")
    fail_json = json.dumps({
        "kind": "kb", "mode": "create",
        "target": {"kind": "kb", "cap": "web", "path": "poc/避坑.md"},
        "content": "# 避坑\n\n## 坑\n- 该注入点有 WAF 指纹，换 payload 前先探测。\n",
        "summary": "WAF 指纹先探测", "reason": f"任务 {task_id} 归因：WAF 拦截非环境问题",
    }, ensure_ascii=False)
    planner = _sediment_planner(fail_json)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "fail_task",
                                            {"result_note": "payload 全被拦"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, planner=planner, enable_sediment=True)
    agent.run_task("试打", task_id=task_id)
    pending = proposals.list_proposals(tmp_path / "packs", "pending")
    assert len(pending) == 1
    assert "failed（error）" in pending[0]["evidence"]
    assert task_id in pending[0]["reason"]
    # 失败模式 instruction 确实注入（planner 收到的消息带避坑口径）
    assert "避坑教训" in json.dumps(planner.calls, ensure_ascii=False)
    # aborted（人工中断路径 tq.fail(blocked_reason=aborted)）不触发复盘
    t2 = tq.publish(project["id"], "被中断的", task_type="generic", created_by="human")
    tq.claim(t2, agent.session["id"])
    tq.fail(t2, agent.session["id"], "人工中断", blocked_reason="aborted")
    assert len(proposals.list_proposals(tmp_path / "packs", "pending")) == 1


def test_sediment_index_proposal_on_complete(env):
    """K4 沉淀回流：复盘产出 index 增补提案（planner 输出 kind=index）→
    落 pending index 提案，待人审批同步 route_index.yaml。"""
    from core.skills import proposals
    _write_web_pack(env)  # route_index.yaml 已存在 → index 只能 edit
    bb, project, gw, tq, tmp_path = env
    task_id = tq.publish(project["id"], "越权测试", task_type="exploit", created_by="human")
    index_json = json.dumps({
        "kind": "index", "mode": "edit",
        "target": {"kind": "index", "cap": "web"},
        "content": "entries:\n  - point: 越权测试\n    kb: web/poc/越权.md\n",
        "summary": "补越权条目", "reason": "任务验证有效",
    }, ensure_ascii=False)
    planner = _sediment_planner(index_json)
    llm = ScriptedLLM([
        {"tool_use": [_plan_call(), _verified_finding_call()]},
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "越权差分验证成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "越权差分验证成功"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, planner=planner, enable_sediment=True)
    declare_intent(bb, project["id"], "对订单接口进行越权差分尝试",
                   author=agent.session["id"])  # 过 bb_add_finding 必挂意图门禁
    # 意图先行闸（2026-09-28）：补 command 事件模拟意图后的执行动作
    bb.append_event(project["id"], "command", {"cmd": "probe"},
                    session_id=agent.session["id"], author=agent.session["id"])
    agent.run_task("越权", task_id=task_id)
    pending = proposals.list_proposals(tmp_path / "packs", "pending")
    assert len(pending) == 1
    assert pending[0]["target"]["kind"] == "index" and pending[0]["mode"] == "edit"


def test_sediment_none_and_silent_failure(env):
    """复盘输出 NONE / 校验拒绝 → 无提案、任务照常收尾（M1 后复盘仅在 verified
    产出任务触发，前置登记发现）。"""
    from core.skills import proposals
    _write_web_pack(env)
    bb, project, gw, tq, tmp_path = env
    t1 = tq.publish(project["id"], "任务一", task_type="generic", created_by="human")
    t2 = tq.publish(project["id"], "任务二", task_type="generic", created_by="human")
    planner = _sediment_planner("NONE")
    llm = ScriptedLLM([
        {"tool_use": [_plan_call(), _verified_finding_call()]},
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task", {"result_note": "无沉淀"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task", {"result_note": "无沉淀"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm, planner=planner, enable_sediment=True)
    agent.run_task("一", task_id=t1)
    assert proposals.list_proposals(tmp_path / "packs", "pending") == []
    # 非法提案 JSON（cap 不存在）→ 创建被拒静默跳过
    planner2 = _sediment_planner(_PROPOSAL_JSON.replace('"cap": "web"', '"cap": "nope"'))
    llm2 = ScriptedLLM([
        {"tool_use": [_plan_call(), _verified_finding_call()]},
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task", {"result_note": "x"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task", {"result_note": "x"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent2 = make_agent(env, llm2, planner=planner2, enable_sediment=True)
    agent2.run_task("二", task_id=t2)
    assert proposals.list_proposals(tmp_path / "packs", "pending") == []
    assert tq.get_task(t2)["status"] == "done"  # 收尾不受影响


def test_sediment_lite_review_long_note_once_per_session(env):
    """M6 F1 高质量复盘档：done 零产出但 result_note ≥500 字 → 复盘产提案
    （reason 引用任务 id 作证据锚点）；每会话上限 _SEDIMENT_LITE_CAP=1——同会话
    第二个长收尾任务不再触发（额度在产提案前占用）。"""
    from core.skills import proposals
    _write_web_pack(env)
    bb, project, gw, tq, tmp_path = env
    t1 = tq.publish(project["id"], "巡检一", task_type="generic", created_by="human")
    t2 = tq.publish(project["id"], "巡检二", task_type="generic", created_by="human")
    long_note = "收尾复盘：" + (
        "目标面已全部走查，没有可利用入口，口令复用不成立，后续应转向相邻资产与配置面。") * 14
    assert len(long_note) >= 500
    planner = _sediment_planner(json.dumps({
        "kind": "kb", "mode": "create",
        "target": {"kind": "kb", "cap": "web", "path": "poc/走查收尾.md"},
        "content": "# 走查收尾\n\n## 已验证路径\n- 全走查零产出时的收尾判断法。\n",
        "summary": "走查收尾方法论", "reason": f"任务证据：{t1} 零产出但收尾够长"
    }, ensure_ascii=False))
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("c1", "complete_task",
                                            {"result_note": long_note})]},
        {"tool_use": [ScriptedLLM.tool_call("c1b", "complete_task",
                                            {"result_note": long_note})]},
        {"tool_use": [ScriptedLLM.tool_call("c2", "finish", {"summary": "收尾一"})]},
        {"tool_use": [ScriptedLLM.tool_call("c3", "complete_task",
                                            {"result_note": long_note})]},
        {"tool_use": [ScriptedLLM.tool_call("c3b", "complete_task",
                                            {"result_note": long_note})]},
        {"tool_use": [ScriptedLLM.tool_call("c4", "finish", {"summary": "收尾二"})]},
    ])
    agent = make_agent(env, llm, planner=planner, enable_sediment=True)
    agent.run_task("巡检一", task_id=t1)
    agent.run_task("巡检二", task_id=t2)
    pending = proposals.list_proposals(tmp_path / "packs", "pending")
    assert len(pending) == 1 and pending[0]["task"] == t1
    # 消费侧直查判定：lite 额度=1，已用 → 第二个任务即使 verdict 也不放行
    assert agent._sediment_lite_used == 1


# ---------- H1：spill 落盘与豁免（借鉴 dsh tool-output-spill-files） ----------

def test_dispatch_spills_oversized_result(env):
    """超 8000 字符的工具结果：全量落 workspace/spill/，回填预览+定位器。"""
    bb, project, gw, tq, tmp_path = env
    agent = make_agent(env, ScriptedLLM([]), artifacts_dir=tmp_path / "artifacts")
    d = agent.dispatcher
    d._tool_big = lambda **k: "X" * 20000  # 假工具绕过 run_cmd 的 gateway brief
    out = d.dispatch("big", {})
    assert "[结果超限已落盘]" in out and "[省略" in out
    assert "完整内容" in out and "../spill/" in out  # scratch 相对路径定位器
    files = list((tmp_path / "spill").glob("*-big-*.txt"))  # E1 uuid 后缀（同秒防互覆）
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "X" * 20000  # 落盘是全量
    # tool.call 审计事件照落，result_head 即预览（不吞全量）
    ev = [e for e in bb.recent_events(project["id"]) if e["kind"] == "tool.call"]
    assert ev and ev[-1]["payload"]["name"] == "big"


def test_dispatch_spill_skip_kb_open(env):
    """kb_open 豁免：正文即取用目的，再长也不 spill（run_cmd/kb_open/skill_open/
    route_lookup 同一豁免清单）。"""
    bb, project, gw, tq, tmp_path = env
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
    bb, project, gw, tq, _ = env
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

def test_request_escalation_rejections(env):
    """四类不受理：本就允许/工作区隔离红线/限速自助/隔离等级红线 + 空理由。"""
    bb, project, gw, tq, tmp_path = env
    d = make_agent(env, ScriptedLLM([])).dispatcher
    # ① 当前策略允许 → 无需升级
    out = d.dispatch("request_escalation",
                     {"cmd": "echo hi", "runtime": "host", "reason": "试试"})
    assert out.startswith("[拒绝]") and "直接 run_cmd" in out
    # ② 工作区隔离是红线（需装配 artifacts_dir 才有工作区判定）
    d2 = make_agent(env, ScriptedLLM([]),
                    artifacts_dir=tmp_path / "art").dispatcher
    out = d2.dispatch("request_escalation",
                      {"cmd": "echo x > ../../evil.txt", "runtime": "host",
                       "reason": "写工作区外"})
    assert "工作区隔离" in out and "红线" in out
    # ③ 限速拒因可自助补参（拒因自带放行参数），不允许升级
    out = d.dispatch("request_escalation",
                     {"cmd": "nmap -p- 1.2.3.4", "runtime": "host",
                      "reason": "全端口"})
    assert "限速纪律" in out
    # ④ 隔离等级是红线（宁严勿松）：untrusted 不允许 host
    out = d.dispatch("request_escalation",
                     {"cmd": "echo hi", "runtime": "host",
                      "threat_class": "untrusted", "reason": "x"})
    assert "隔离等级策略" in out
    # ⑤ 空理由拒绝
    out = d.dispatch("request_escalation",
                     {"cmd": "echo hi", "runtime": "host", "reason": "  "})
    assert "升级理由" in out
    # 全程没有产生审批单
    assert bb.conn.execute("SELECT COUNT(*) c FROM approvals").fetchone()["c"] == 0


def test_request_escalation_net_real_creates_approval(env):
    """net=real：提交 high 风险审批单（kind=net_real），命令只等批准不执行。"""
    bb, project, gw, tq, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    out = agent.dispatcher.dispatch("request_escalation", {
        "cmd": "wget http://10.0.0.5/x", "runtime": "host", "net": "real",
        "reason": "需要真实出网验证 SSRF"})
    assert out.startswith("[已提交审批]")
    row = bb.conn.execute("SELECT * FROM approvals").fetchone()
    assert row is not None and row["status"] == "pending"
    assert row["risk"] == "high" and row["session_id"] == agent.session["id"]
    action = json.loads(row["action"])
    assert action["op"] == "escalation" and action["kind"] == "net_real"
    assert action["net"] == "real" and "SSRF" in action["reason"]


def test_request_escalation_role_runtime(env):
    """runtime 超角色 max_runtime 软上限：medium 审批单（kind=role_runtime）。"""
    bb, project, gw, tq, _ = env
    write_role(env, "limited",
               'name: limited\npersona: "受限。"\nmax_runtime: host\n')
    agent = make_agent(env, ScriptedLLM([]), role="limited")
    assert agent.dispatcher.max_runtime == "host"
    out = agent.dispatcher.dispatch("request_escalation", {
        "cmd": "echo hi", "runtime": "wsl", "reason": "需要 wsl 工具链"})
    assert out.startswith("[已提交审批]")
    row = bb.conn.execute("SELECT action, risk FROM approvals").fetchone()
    action = json.loads(row["action"])
    assert action["kind"] == "role_runtime" and row["risk"] == "medium"


def test_escalation_result_reaches_chat_round(env):
    """空闲对话轮注入升级回执：escalation_result 经收件箱 drain → 首消息拼回执
    → 纯文本回复落 agent.chat 事件。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([{"text": "已收到升级回执，出网验证通过。"}])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    bb.inbox_post(project["id"], sid, "escalation_result", "appr-1",
                  {"text": "[host] exit=0", "cmd": "wget http://x"})
    out = agent.run_chat()
    assert out == "已收到升级回执，出网验证通过。"
    first = json.dumps(llm.calls[0]["messages"][0], ensure_ascii=False)
    assert "升级命令已执行" in first and "exit=0" in first
    # 收件箱已消费 + 回复落事件流
    assert bb.inbox_list(project["id"], sid, unread_only=True) == []
    assert any(e["kind"] == "agent.chat"
               for e in bb.recent_events(project["id"]))


# ---------- M5 D2：request_authorization（行为边界授权申请，orchestrator-efficiency §0-10） ----------

def test_request_authorization_kinds_and_rejections(env):
    """三 kind 各建 high 审批单（op=authorization，payload 带 scope_request/
    justification/证据 finding/task/session）；非法 kind 与空必填项拒收。"""
    bb, project, gw, tq, _ = env
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
    bb, project, gw, tq, _ = env
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


def test_authorization_and_rejection_notices_reach_chat(env):
    """批准/拒绝双回流进对话轮：authorization_result 与 approval_rejected 经收件箱
    drain → 首消息注入 notice → 回复落事件流、收件箱清空。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([{"text": "收到，按批准边界继续。"}])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    bb.inbox_post(project["id"], sid, "authorization_result", "appr-1",
                  {"op": "authorization", "kind": "scope_expand",
                   "scope_request": "追加 target.com 子域", "approved": True})
    out = agent.run_chat()
    assert out == "收到，按批准边界继续。"
    first = json.dumps(llm.calls[0]["messages"][0], ensure_ascii=False)
    assert "授权申请已批准" in first and "scope_expand" in first
    # 拒绝回流：换路提示，escalation/authorization 两种 op 都能渲染
    llm2 = ScriptedLLM([{"text": "明白，换路。"}])
    agent2 = make_agent(env, llm2)
    sid2 = agent2.session["id"]
    bb.inbox_post(project["id"], sid2, "approval_rejected", "appr-2",
                  {"op": "escalation", "kind": "net_real"})
    bb.inbox_post(project["id"], sid2, "approval_rejected", "appr-3",
                  {"op": "authorization", "kind": "rating_override"})
    out2 = agent2.run_chat()
    assert out2 == "明白，换路。"
    first2 = json.dumps(llm2.calls[0]["messages"][0], ensure_ascii=False)
    assert "升级命令申请被人类拒绝" in first2
    assert "授权申请被人类拒绝" in first2 and "（未附理由）" in first2
    assert bb.inbox_list(project["id"], sid2, unread_only=True) == []


# ---------- P3 多智能体协调：bb_notify 私信 / agent_message 注入 / 子任务回执 ----------

def test_bb_notify_tool_target_and_broadcast(env):
    """bb_notify（B1）：to_session 定向优先；to_role 广播活跃同角色窗（跳过自己
    与已关闭）；两者皆空/text 空/不存在/已关闭 → [错误]；统一落 tool.call 审计。"""
    from core.agent.tools import ToolDispatcher
    bb, project, gw, tq, _ = env
    s1 = bb.register_session(project["id"], "发信窗")["id"]
    peer = bb.register_session(project["id"], "收信窗")["id"]
    closed = bb.register_session(project["id"], "已关窗")["id"]
    bb.close_session(closed)
    d = ToolDispatcher(bb, gateway=gw, tq=tq, project_id=project["id"],
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


def test_agent_message_injected_at_step_boundary_and_claim(env):
    """agent_message 注入（P3.4）：任务中途到达的私信在**步边界**信息式注入
    （与 human_note 轮末语义不同——私信与任务现场相关）；空闲期到达的私信随
    下一轮认领期 drain 进开场。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "被私信的任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "本轮完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "下轮收到私信"})]},
    ])
    agent = make_agent(env, llm)
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

    assert agent.run_task("私信演练", task_id=tid) == "本轮完成"
    second = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "📨 Agent 私信" in second and "swagger" in second
    # 空闲期到达的私信随下一轮认领期注入
    bb.post_agent_message(project["id"], peer, agent.session["id"],
                          "assist", "把 80 端口结果发我一份")
    tid2 = tq.publish(project["id"], "下轮任务", task_type="generic")
    assert agent.run_task("下一轮", task_id=tid2) == "下轮收到私信"
    first = json.dumps(llm.calls[2]["messages"], ensure_ascii=False)
    assert "📨 Agent 私信" in first and "80 端口" in first


def test_run_chat_consumes_agent_message(env):
    """对话轮消费私信（P3.4）：run_chat only_kinds 扩 4 kind——agent_message 注入
    对话轮（含 subkind 中文标注），回复后收件箱清空。"""
    bb, project, gw, tq, _ = env
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


def test_task_receipt_reaches_parent_chat(env):
    """端到端回执链（P3）：子窗收尾 → task_receipt 投父窗收件箱 → 父窗空闲
    run_chat 对话轮收到 ✅ 完成摘要与发现清单。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([{"text": "子任务结论已收录，接管链已上图。"}])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    parent = tq.publish(project["id"], "父任务：打 target.com", task_type="generic")
    tq.claim(parent, sid)
    child = tq.publish(project["id"], "子任务：子域枚举", task_type="generic",
                       parent_id=parent, created_by=sid)
    peer = bb.register_session(project["id"], "子窗")["id"]
    tq.claim(child, peer)
    bb.add_finding(project["id"], "info-leak", "悬空 CNAME 可接管",
                   severity="high", author=peer)
    tq.complete(child, peer, "枚举完成，接管成立")
    out = agent.run_chat()
    assert out and "上图" in out
    first = json.dumps(llm.calls[0]["messages"][0], ensure_ascii=False)
    assert "✅ 子任务完成" in first and "子域枚举" in first and "悬空 CNAME" in first
    assert bb.inbox_list(project["id"], sid, unread_only=True) == []


# ---------- 收件箱订阅声明化 + 中断落盘（2026-09-21） ----------

def test_resume_drains_escalation_result(env):
    """恢复期补齐 escalation_result（订阅声明化）：暂停期送达的升级回执随快照
    恢复注入——原实现全量 drain 但渲染漏 esc，回执被标记已读后静默吞掉。"""
    bb, project, gw, tq, tmp_path = env
    tid = tq.publish(project["id"], "恢复收回执任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=10),
                       artifacts_dir=tmp_path / "proj" / "artifacts")
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            agent.request_pause()   # 暂停 → 快照落盘（可续现场）
        return r
    llm.chat = chat

    assert agent.run_task("恢复演练", task_id=tid) == ""
    sid = agent.session["id"]
    # 暂停态硬中断（E12 同款：任务 failed「人工中断」、快照+指针保留）
    agent._abort_current_task()
    agent._pause_req.clear()  # request_abort 才会清暂停请求；此处手动清以放行续跑
    assert tq.get_task(tid)["status"] == "failed"
    # 暂停期间送达的升级回执（API 层批准执行后投递同款形态）
    bb.inbox_post(project["id"], sid, "escalation_result", "appr-r1",
                  {"text": "[host] exit=0", "cmd": "wget http://x"})
    st = agent.revive_snapshot(tid)
    assert st is not None
    tq.reopen(tid, by="human")
    tq.claim(tid, sid, lease_minutes=agent.config.lease_minutes)
    agent._stop_after_task = False  # 清中断一次性闸门（E12 同款）
    llm2 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "续跑收到回执"})]},
    ])
    agent.llm = llm2
    assert agent.run_session() == "续跑收到回执"
    first = json.dumps(llm2.calls[0]["messages"], ensure_ascii=False)
    assert "升级命令已执行" in first and "exit=0" in first


def test_claim_renders_pure_inbox_basis_stale(env):
    """认领期纯私信 basis_stale 不再被 stale_refs 门控吞掉（订阅声明化）：任务行
    无挂标（stale_refs 空）但收件箱有撤回私信 → 认领期照常渲染强制自评三选一。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "收撤回私信任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "完成"})]},
    ])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    # 纯私信形态：任务行 stale_refs 为空，只有收件箱行（撤回路由投递的 payload）
    bb.inbox_post(project["id"], sid, "basis_stale", "find-abc123def456",
                  {"finding_id": "find-abc123def456", "title": "弱口令后台",
                   "vuln_class": "weak-cred", "by": "human", "note": "口令已轮换"})
    assert agent.run_task("纯私信演练", task_id=tid) == "完成"
    first = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    assert "依据撤回" in first and "弱口令后台" in first and "三选一" in first


def test_unknown_inbox_kind_not_swallowed(env):
    """未登记 kind 绝不被静默吞（订阅声明化）：四消费点 only_kinds 从订阅表构造
    ——未登记 kind 经认领期+步边界后仍滞留收件箱未读（红点可见，提示先登记）。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "未知kind任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完成"})]},
    ])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    bb.inbox_post(project["id"], sid, "future_kind", "ref-x", {"text": "未来私信"})
    assert agent.run_task("未知kind演练", task_id=tid) == "完成"
    unread = bb.inbox_list(project["id"], sid, unread_only=True)
    assert [r["kind"] for r in unread] == ["future_kind"]


def test_chat_scenario_leaves_task_kinds_unread(env):
    """对话轮场景口径（订阅声明化）：run_chat 只消费无任务上下文也能响应的 kind
    ——finding_update/basis_stale 留收件箱未读，下一轮认领期再消费（不是丢失）。"""
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([{"text": "收到引导。"},
                       {"tool_use": [ScriptedLLM.tool_call("t1", "finish",
                                                           {"summary": "补收完成"})]}])
    agent = make_agent(env, llm)
    sid = agent.session["id"]
    bb.post_human_note(project["id"], sid, "请复核 8080")
    bb.inbox_post(project["id"], sid, "finding_update", "find-abc123def456",
                  {"finding_id": "find-abc123def456", "title": "弱口令后台"})
    bb.inbox_post(project["id"], sid, "basis_stale", "find-def456abc123",
                  {"finding_id": "find-def456abc123", "title": "旧依据"})
    assert agent.run_chat() == "收到引导。"
    unread = sorted(r["kind"] for r in bb.inbox_list(project["id"], sid, unread_only=True))
    assert unread == ["basis_stale", "finding_update"]
    # 下一轮认领期补收：两条都进开场（信息式 + 强制三选一）
    tid = tq.publish(project["id"], "补收任务", task_type="generic")
    assert agent.run_task("补收演练", task_id=tid) == "补收完成"
    first = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "弱口令后台" in first and "依据撤回" in first


def test_interrupted_reply_flushed_in_chat(env):
    """中断落盘（对话轮）：流式回复中途 abort——半截回复落 agent.chat 终稿
    （interrupted=true + stream_id，无 step），run_chat 返 None，turn 有收口回复。"""
    bb, project, gw, tq, _ = env
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


def test_interrupted_narrative_flushed_in_task(env):
    """中断落盘（任务轮）：叙述流式中 abort——半截叙述落 agent.chat（step=1 +
    interrupted=true），任务 fail「人工中断」；任务轮不发 delta 事件（只攒不发）。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "叙述中断任务", task_type="generic")
    llm = ScriptedLLM([
        {"text_deltas": ["第一步", "正在分析"], "text": "第一步正在分析",
         "tool_use": [ScriptedLLM.tool_call("t1", "finish", {"summary": "不该到达"})]},
    ])
    agent = make_agent(env, llm)
    orig_chat = llm.chat
    fired = {"v": False}

    def chat(messages, **kw):
        on_text = kw.get("on_text")
        if on_text is not None:
            def wrapped(d):
                on_text(d)
                if not fired["v"]:
                    fired["v"] = True
                    agent.request_abort()
            kw["on_text"] = wrapped
        return orig_chat(messages, **kw)
    llm.chat = chat

    assert agent.run_task("叙述中断演练", task_id=tid) == ""
    row = tq.get_task(tid)
    assert row["status"] == "failed" and row["result_note"] == "人工中断"
    finals = [e for e in bb.recent_events(project["id"]) if e["kind"] == "agent.chat"]
    assert len(finals) == 1
    p = finals[0]["payload"]
    assert p["interrupted"] is True and p["step"] == 1 and "第一步" in p["text"]
    assert not [e for e in bb.recent_events(project["id"])
                if e["kind"] == "agent.chat.delta"]


# ---------- 蓝图工具（R4 逆向开发管线） ----------

def test_blueprint_tools_create_update_and_status_guard(env):
    """bb_blueprint_create/update 全链路：建骨架 → 模块写回 → 整表/正文；
    蓝图整体 status 不暴露给 Agent（拒绝引导走人类）。"""
    bb, project, gw, tq, _ = env
    agent = make_agent(env, ScriptedLLM([]))
    d = agent.dispatcher
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
    bb, project, gw, tq, _ = env
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

def _read_dispatcher(env):
    bb, project, gw, tq, tmp_path = env
    artifacts = tmp_path / "ws" / "artifacts"
    scratch = tmp_path / "ws" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    d, _tid = _plan_dispatcher(env, session_name="reader", artifacts_dir=artifacts)
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
    f.write_text("\n".join(f"l{i}" for i in range(1, 31)) + "\n" + "x" * 900,
                 encoding="utf-8")
    r = d.dispatch("read_file", {"path": "log.txt", "offset": -3})
    assert "31\t" in r and "l29" in r                        # 末尾 N 行（tail 语义）
    assert "行截断" in r and "共 900 字符" in r               # 超长行切尾标注


def test_read_file_rejects_missing_and_outside(env):
    d, scratch, artifacts = _read_dispatcher(env)
    r = d.dispatch("read_file", {"path": "nope.txt"})
    assert r.startswith("[错误]") and "不存在" in r
    (scratch.parent.parent / "outside.txt").write_text("secret", encoding="utf-8")
    r = d.dispatch("read_file", {"path": "../../outside.txt"})   # 工作区外
    assert r.startswith("[拒绝]")
    (scratch / "tiny.txt").write_text("only\n", encoding="utf-8")
    r = d.dispatch("read_file", {"path": "tiny.txt", "offset": 99})
    assert r.startswith("[错误]") and "共 1 行" in r          # 越界报总行数


def test_read_file_kb_root_allowed_but_packs_else_rejected(env):
    """route-injection-hardening：read_file 放行启用域 kb 源根（kb_open 回执指示
    Read 手册）；packs 其余目录（experts/）仍拒；工作区文件照旧可读。"""
    bb, project, gw, tq, tmp_path = env
    artifacts = tmp_path / "ws" / "artifacts"
    agent = make_agent(env, ScriptedLLM([]), artifacts_dir=artifacts)
    d = agent.dispatcher
    packs = tmp_path / "packs"
    kb_md = packs / "kb" / "web" / "poc" / "handbook.md"
    kb_md.parent.mkdir(parents=True, exist_ok=True)
    kb_md.write_text("1\t手册正文第一行\n第二行", encoding="utf-8")
    # kb 根内文件：绝对路径可读
    r = d.dispatch("read_file", {"path": str(kb_md)})
    assert "手册正文第一行" in r and not r.startswith("[拒绝]")
    # packs/experts 仍拒（只放行 kb 源根，不放整个 packs）
    expert = packs / "experts" / "_generalist.yaml"
    r = d.dispatch("read_file", {"path": str(expert)})
    assert r.startswith("[拒绝]")
    # 未启用域（binary）的 kb 根不放行
    bin_md = packs / "kb" / "binary" / "notes.md"
    bin_md.parent.mkdir(parents=True, exist_ok=True)
    bin_md.write_text("binary notes", encoding="utf-8")
    assert d.dispatch("read_file", {"path": str(bin_md)}).startswith("[拒绝]")
    # 工作区文件不受影响
    scratch = tmp_path / "ws" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "ws.txt").write_text("workspace file", encoding="utf-8")
    assert "workspace file" in d.dispatch("read_file", {"path": "ws.txt"})


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
