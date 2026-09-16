"""Agent 主循环测试：脚本化 LLM 驱动端到端（不触网）。

覆盖：工具分发全链路、网关拒绝改道、系统提示组装（红线/角色/能力清单）、
策略顾问卡死干预、会话收尾安全（未收尾任务自动 fail）、上下文裁剪。
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from core.agent import AgentConfig, AgentSession
from core.agent.loop import _LeaseHeartbeat
from core.blackboard import Blackboard, TaskQueue
from core.llm.anthropic_compat import AnthropicCompatProvider
from core.runtime import ExecutionGateway, NativeBackend


# ---------- 测试基建 ----------

class ScriptedLLM(AnthropicCompatProvider):
    """按剧本回放 LLM 响应；记录每次收到的消息供断言。"""

    def __init__(self, script: list[dict]):
        super().__init__("https://fake", "key", "scripted", transport=lambda *a: (200, {}))
        self.script = list(script)
        self.calls: list[dict] = []

    def chat(self, messages, *, system=None, tools=None, max_tokens=4096, temperature=None):
        self.calls.append({"messages": json.loads(json.dumps(messages)), "system": system})
        item = self.script.pop(0)
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
    project = bb.create_project("测试项目", "assessment", ["web"])
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend(), "sandbox": FakeDockerBackend()})
    tq = TaskQueue(bb)
    yield bb, project, gw, tq, tmp_path
    bb.close()


def make_agent(env, llm, planner=None, config=None, role="_generalist", artifacts_dir=None,
               capabilities=("web",)):
    bb, project, gw, tq, tmp_path = env
    packs = tmp_path / "packs"
    # 轨级演示技能（pack_set = capabilities ∪ {track} 内可见）
    (packs / "tracks" / "assessment" / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (packs / "tracks" / "assessment" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 演示技能\nkeywords: 测试\n---\n按步骤执行。",
        encoding="utf-8")
    roles = packs / "tracks" / "assessment" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "_generalist.yaml").write_text(
        'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
    return AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                        planner_llm=planner, packs_root=packs, track="assessment",
                        capabilities=list(capabilities),
                        role=role, capability_prompt="## 能力清单\n- Docker: 可用",
                        config=config or AgentConfig(max_steps=10),
                        artifacts_dir=artifacts_dir)


def write_role(env, name, body):
    """测试夹具里写一个轨角色 yaml。"""
    _, _, _, _, tmp_path = env
    f = tmp_path / "packs" / "tracks" / "assessment" / "roles" / f"{name}.yaml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")


# ---------- 端到端：run_cmd → 发现 → finish ----------

def test_full_loop_run_cmd_finding_finish(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "bb_add_asset",
                                            {"type": "domain", "value": "x.com"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "bb_add_finding",
                                            {"vuln_class": "info-leak", "title": "备份泄露",
                                             "severity": "low"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "finish", {"summary": "干完了"})]},
    ])
    agent = make_agent(env, llm)
    summary = agent.run_task("测 x.com")
    assert summary == "干完了"
    assert len(bb.list_findings(project["id"])) == 1
    assert bb.list_assets(project["id"])[0]["value"] == "x.com"
    # 审计链：command 与 session.finished 都落了事件
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "command" in kinds and "session.finished" in kinds
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


# ---------- 上下文裁剪 ----------

def test_context_trimming(env):
    bb, project, gw, tq, _ = env

    class BigHostBackend:
        name = "host"

        def execute(self, cmd, timeout=120.0, cwd=None, env=None):
            return type("O", (), {"exit_code": 0, "stdout": "A" * 5000,
                                  "stderr": "", "timed_out": False, "meta": {}})()

    gw.backends["host"] = BigHostBackend()
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(f"t{i}", "run_cmd",
                                            {"cmd": f"cmd{i}", "runtime": "host",
                                             "threat_class": "trusted"})]}
        for i in range(12)
    ] + [{"tool_use": [ScriptedLLM.tool_call("tz", "finish", {"summary": "ok"})]}])
    cfg = AgentConfig(max_steps=20, context_char_budget=10_000)
    agent = make_agent(env, llm, config=cfg)
    agent.run_task("裁剪测试")
    # 网关 brief() 已截到 2000 字符/条，12 条 ≈ 25k > 10k 预算 → 旧结果应被截断
    last = llm.calls[-1]["messages"]
    truncated = [b for m in last if isinstance(m.get("content"), list)
                 for b in m["content"]
                 if b.get("type") == "tool_result" and "[已截断]" in b.get("content", "")]
    assert truncated


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


# ---------- 会话控制（DESIGN.md §3：暂停/恢复/中断） ----------

def test_pause_between_steps_and_resume_continues(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task",
                                            {"result_note": "侦察完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "干完了"})]},
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
    assert agent.run_next_task() == "干完了"
    assert tq.get_task(tid)["status"] == "done"
    assert any(e["kind"] == "session.finished"
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
    assert agent.run_next_task() is None
    assert agent._stop_after_task is False


def test_worker_never_claims_after_pause(env):
    bb, project, gw, tq, _ = env
    llm = ScriptedLLM([])
    agent = make_agent(env, llm)
    tid = tq.publish(project["id"], "排队任务", task_type="generic")
    agent.request_pause()
    assert agent.run_next_task() is None
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
    ])
    agent = make_agent(env, llm)
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
            {"vuln_class": "sqli", "title": "注入升级",
             "relates_to": [{"finding_id": base, "note": "同目标升级"}]})]},
        {"tool_use": [ScriptedLLM.tool_call(
            "t2", "bb_add_finding",
            {"vuln_class": "xss", "title": "幻觉强边",
             "relates_to": [{"finding_id": "find-deadbeef", "note": "悬空"}]})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("强关系登记")
    rows = {f["vuln_class"]: f for f in bb.list_findings(project["id"])}
    assert rows["sqli"]["evidence"]["relates_to"] == [
        {"finding_id": base, "note": "同目标升级"}]
    # 悬空强边整笔 finding 不落（ValueError 在写入前）
    assert "xss" not in rows
    # 错误文本回填给 LLM，循环未中断（finish 正常收尾）
    assert any("不存在或不属于本项目" in json.dumps(m, ensure_ascii=False)
               for m in llm.calls[2]["messages"])


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
    """kb_open：多源命中返回绝对路径 + kb.open 审计；不存在返回清单（防幻觉）；未登记报错。"""
    bb, project, gw, tq, tmp_path = env
    # 两个源：main 递归（含子目录）、flat 非递归
    kb_main = tmp_path / "kb-main"
    (kb_main / "sub").mkdir(parents=True)
    (kb_main / "xss-test.md").write_text("# XSS 测试", encoding="utf-8")
    (kb_main / "sub" / "deep.md").write_text("深层模块", encoding="utf-8")
    kb_flat = tmp_path / "kb-flat"
    kb_flat.mkdir()
    (kb_flat / "only.md").write_text("flat", encoding="utf-8")
    cap_dir = tmp_path / "packs" / "capabilities" / "web"
    cap_dir.mkdir(parents=True, exist_ok=True)
    sources_file = cap_dir / "kb_sources.json"

    def write_sources():
        sources_file.write_text(json.dumps({"sources": [
            {"id": "main", "root": str(kb_main), "recursive": True},
            {"id": "flat", "root": str(kb_flat), "recursive": False},
        ]}, ensure_ascii=False), encoding="utf-8")

    write_sources()

    # 命中（含递归深层路径）
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "kb_open", {"module": "sub/deep.md"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent = make_agent(env, llm)
    agent.run_task("开知识库")
    result = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "deep.md" in result               # 返回了模块绝对路径（JSON 内 \\ 转义）
    assert "main" in result and "禁止通读" in result
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
    assert "防幻觉" in result2 and "xss-test.md" in result2 and "sub/deep.md" in result2

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

    # 未登记 kb_sources → 错误文本不断循环
    sources_file.unlink()
    llm3 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "kb_open", {"module": "x.md"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent3 = make_agent(env, llm3)
    agent3.run_task("无知识库")
    result3 = json.dumps(llm3.calls[1]["messages"], ensure_ascii=False)
    assert "未登记" in result3


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


def test_role_default_noise_filters_claim(env):
    """角色 default_noise=passive：claim_next 不认领 low 噪声任务，passive 任务可认领。"""
    bb, project, gw, tq, _ = env
    write_role(env, "quiet",
               'name: quiet\npersona: "安静"\ndefault_noise: passive\n')
    # active 任务需要 conflict_keys（§6.2）
    loud = tq.publish(project["id"], "主动打点", task_type="generic",
                      noise_budget="low", conflict_keys=["ip:10.0.0.9"], created_by="human")
    agent = make_agent(env, ScriptedLLM([]), role="quiet")
    assert agent.run_next_task() is None
    assert tq.get_task(loud)["status"] == "open"      # 高噪声任务不被被动角色认领
    quiet = tq.publish(project["id"], "被动收集", task_type="generic",
                       noise_budget="passive", created_by="human")
    llm2 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task", {"result_note": "done"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "完"})]},
    ])
    agent.llm = llm2
    assert agent.run_next_task() == "完"
    assert tq.get_task(quiet)["status"] == "done"
    assert tq.get_task(loud)["status"] == "open"      # loud 仍在队列


# ---------- A1：任务租约心跳（长任务防 TTL 过期被重领双跑） ----------

class _SlowFirstLLM(ScriptedLLM):
    """首次 chat 先跑 on_start 钩子、阻塞 delay 秒（留出租心跳窗口）、醒后跑 on_wake。"""

    def __init__(self, script, *, delay, on_start=None, on_wake=None):
        super().__init__(script)
        self.delay = delay
        self.on_start = on_start
        self.on_wake = on_wake

    def chat(self, messages, *, system=None, tools=None, max_tokens=4096, temperature=None):
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
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task", {"result_note": "done"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "收工"})]},
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
    assert agent.run_next_task() is None


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
    other = tq.publish(project["id"], "新任务")
    # 恢复后新任务的剧本
    llm.script.extend([
        {"tool_use": [ScriptedLLM.tool_call("t2", "complete_task",
                                            {"result_note": "新任务完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "收工"})]},
    ])

    agent._pause_req.clear()
    agent._abort_req.clear()
    agent.paused = False
    assert agent.run_next_task() == "收工"  # 快照失效 → 认领新任务并跑完
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
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "c1", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "run_cmd",
                                            {"cmd": "c2", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "run_cmd",
                                            {"cmd": "c3", "runtime": "host",
                                             "threat_class": "trusted"})]},
        # 恢复后的续跑剧本
        {"tool_use": [ScriptedLLM.tool_call("t4", "complete_task",
                                            {"result_note": "补步后完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t5", "finish", {"summary": "干完了"})]},
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
    assert agent.run_next_task() == "干完了"
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
        # 恢复后耗尽断点：先自救申请，再收尾
        {"tool_use": [ScriptedLLM.tool_call("t3", "request_steps",
                                            {"reason": "还差最后一步"})]},
        {"tool_use": [ScriptedLLM.tool_call("t4", "complete_task", {"result_note": "完"})]},
        {"tool_use": [ScriptedLLM.tool_call("t5", "finish", {"summary": "自救成功"})]},
    ])
    agent = make_agent(env, llm, config=AgentConfig(max_steps=2))
    assert agent.run_task("自救演练", task_id=tid) == ""
    agent._pause_req.clear()
    agent._abort_req.clear()
    agent.paused = False                                       # 不加预算直接恢复
    assert agent.run_next_task() == "自救成功"                  # request_steps +200 续上
    assert tq.get_task(tid)["status"] == "done"
    ev = [e for e in bb.recent_events(project["id"])
          if e["kind"] == "step.budget_extended"]
    assert ev and ev[-1]["payload"]["by"] == "agent"


def test_human_note_injected_at_step_boundary(env):
    """human_note（E8）：步边界 drain 注入「💬 人类引导」user 消息，不打断工具调用。"""
    bb, project, gw, tq, _ = env
    tid = tq.publish(project["id"], "被引导的任务", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "run_cmd",
                                            {"cmd": "whoami", "runtime": "host",
                                             "threat_class": "trusted"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收到引导"})]},
    ])
    agent = make_agent(env, llm)
    orig_chat = llm.chat
    n = {"c": 0}

    def chat(messages, **kw):
        n["c"] += 1
        r = orig_chat(messages, **kw)
        if n["c"] == 1:
            bb.post_human_note(project["id"], agent.session["id"], "优先看备份文件")
        return r
    llm.chat = chat

    assert agent.run_task("引导演练", task_id=tid) == "收到引导"
    second = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "💬 人类引导" in second and "优先看备份文件" in second


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
                                            {"result_note": "重启后完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "续跑成功"})]},
    ])
    reborn = AgentSession(
        project_id=project["id"], bb=bb, gateway=gw, llm=llm2,
        packs_root=tmp_path / "packs", track="assessment", capabilities=["web"],
        role="_generalist", capability_prompt="", config=AgentConfig(max_steps=10),
        artifacts_dir=tmp_path / "proj" / "artifacts", existing_session=row)
    assert reborn.paused is True
    assert reborn._resume_state is not None
    assert reborn._resume_state["task_id"] == tid
    assert reborn._resume_state["next_step"] == 2
    assert reborn.dispatcher.max_steps == 10                   # 暂停时预算随快照还原

    # 人类「继续」→ 从快照续跑 → 快照文件与指针清理
    reborn.paused = False
    assert reborn.run_next_task() == "续跑成功"
    assert tq.get_task(tid)["status"] == "done"
    assert not snap.exists()
    assert json.loads(bb.get_session(sid)["meta"])["resume_snapshot"] is None


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
                                            {"result_note": "复活后完成"})]},
        {"tool_use": [ScriptedLLM.tool_call("t3", "finish", {"summary": "续跑成功"})]},
    ])
    agent.llm = llm2
    assert agent.run_next_task() == "续跑成功"
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
    assert d.dispatch("bb_asset_status",
                      {"asset_id": aid, "status": "hacked"}).startswith("[拒绝]")
    assert d.dispatch("bb_asset_status",
                      {"asset_id": "asset-000000000000", "status": "visited"}).startswith("[错误]")
    for st in ("visited", "scanning"):
        assert d.dispatch("bb_asset_status", {"asset_id": aid, "status": st}).startswith("asset=")
    assert d.dispatch("bb_asset_status",
                      {"asset_id": aid, "status": "tested_clean",
                       "note": "手测 4 个入口"}).startswith("asset=")
    ev = [e for e in d.bb.recent_events(d.project_id)
          if e["kind"] == "asset.status_changed"]
    assert [e["payload"]["new"] for e in ev] == ["visited", "scanning", "tested_clean"]

    q = json.loads(d.dispatch("bb_query", {"what": "assets", "status": "tested_clean"}))
    assert [a["id"] for a in q] == [aid] and q[0]["status"] == "tested_clean"
    q2 = json.loads(d.dispatch("bb_query", {"what": "assets", "type": "host", "status": "open"}))
    assert all(a["type"] == "host" and a["status"] == "open" for a in q2)
