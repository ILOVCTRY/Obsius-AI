"""⑥ 战役记忆（简版，跨项目）测试。

- 写入：add 截 500 字；done 钩子（campaign_hook）并联沉淀 + campaign.memory 事件。
- 召回：关键词命中打分、track/capability 加成、热度×时间衰减排序、截 300 字、
  召回即计 usage；跨项目可见（不隔离 project_id）。
- 编排：orchestrator 注入 campaign 后态势出现「既往战役打法」段；零命中/未注入为空。
"""
import pytest
from pathlib import Path

from core.blackboard import Blackboard, TaskQueue
from core.blackboard.campaign import CampaignMemory


@pytest.fixture()
def camp(tmp_path):
    c = CampaignMemory(tmp_path / "campaign.db")
    yield c
    c.close()


def test_add_truncates_content(camp):
    row = camp.add("proj-a", "pentest", "web", "recon",
                   "外网打法", "打" * 800, target_key="ip:1.2.3.4")
    got = camp.list_recent()[0]
    assert len(got["content"]) == 500
    assert got["title"] == "外网打法" and got["target_key"] == "ip:1.2.3.4"
    assert got["track"] == "pentest" and got["usage_count"] == 0
    assert row["id"].startswith("camp-")


def test_recall_keyword_track_and_usage_boost(camp):
    camp.add("proj-a", "pentest", "web", "recon", "nmap 全端口慢扫",
             "对 10.0.0.1 用 nmap -sV --max-rate 500 做服务指纹，避开全端口")
    camp.add("proj-b", "ctf", "crypto", "generic", "RSA 共模攻击",
             "两份密文 e=65537 相同模数，gcd 解私钥")
    camp.add("proj-c", "redteam", "web", "recon", "nmap 相关但轨不符",
             "nmap 另一条记录")
    # 命中「nmap」：pentest+web 轨加成最高
    hits = camp.recall("nmap 10.0.0.1 指纹", track="pentest", capability="web")
    assert hits[0]["title"] == "nmap 全端口慢扫"
    assert hits[0]["score"] > 2
    # 召回即计 usage
    assert camp.list_recent()[0]["usage_count"] == 1
    # 中文关键词整段匹配：RSA 记录按内容命中
    hits2 = camp.recall("共模攻击", track="ctf", capability="crypto")
    assert hits2[0]["title"] == "RSA 共模攻击"


def test_recall_recency_decay_and_usage_heat(camp):
    # 老记录先写（多召回几次攒热度），新记录后写：新记录凭 decay 压过老的热度
    old = camp.add("proj-a", "pentest", "web", "recon", "打法A", "sqlmap 拖库")
    new = camp.add("proj-a", "pentest", "web", "recon", "打法B", "sqlmap 拖库")
    for _ in range(6):  # 老记录攒热 6 次
        camp.recall("sqlmap", track="pentest")
    hits = camp.recall("sqlmap", track="pentest")
    assert [h["title"] for h in hits][:2] == ["打法B", "打法A"]
    # 热度足够时可反超：老记录再攒 4 次（log1p 马太修正后热 10 次≈+3.6，仍压过热 6 次≈+2.9）
    camp.bump([old["id"], old["id"], old["id"], old["id"]])
    hits2 = camp.recall("sqlmap", track="pentest")
    assert hits2[0]["title"] == "打法A"


def test_recall_truncates_snippet(camp):
    camp.add("proj-a", "pentest", "web", "recon", "长打法", "细" * 400)
    hits = camp.recall("打法", track="pentest", snippet_len=300)
    assert len(hits[0]["content"]) == 300
    # 库里原文不截
    assert len(camp.list_recent()[0]["content"]) == 400


def test_recall_zero_hit_and_cross_project(camp):
    assert camp.recall("毫不相关的查询词") == []
    camp.add("proj-a", "pentest", "web", "recon", "标题", "内容含 redis 未授权")
    # project_id 只是溯源：proj-b 项目照样召回 proj-a 沉淀（跨项目）
    assert len(camp.recall("redis")) == 1


def _make_agent_run_done(tmp_path, camp):
    """端到端：Agent 完成有 verified 产出的任务 → campaign_hook 沉淀 +
    campaign.memory 事件（experience-sedimentation M1：无产出 done 不写 campaign）。"""
    from core.agent import AgentConfig, AgentSession
    from test_agent import ScriptedLLM, _plan_call, _verified_finding_call

    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("战役项目", "pentest", ["web"])
    from core.runtime import ExecutionGateway, NativeBackend
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    tq = TaskQueue(bb)
    packs = tmp_path / "packs"
    (packs / "experts").mkdir(parents=True, exist_ok=True)
    (packs / "experts" / "_generalist.yaml").write_text(
        'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
    tid = tq.publish(project["id"], "打 10.0.0.1", created_by="human",
                     task_type="generic", scope="ip:10.0.0.1")
    llm = ScriptedLLM([
        {"tool_use": [_plan_call(), _verified_finding_call()]},
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                            {"result_note": "弱口令进后台拿 flag"})]},
        {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                            {"result_note": "弱口令进后台拿 flag"})]},
        {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收尾"})]},
    ])
    agent = AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                         packs_root=packs, track="pentest", capabilities=["web"],
                         role="_generalist", capability_prompt="",
                         config=AgentConfig(max_steps=10), campaign=camp)
    agent.run_task("打点", task_id=tid)
    assert tq.get_task(tid)["status"] == "done"
    rows = camp.list_recent()
    assert len(rows) == 1
    assert rows[0]["title"] == "打 10.0.0.1"
    assert "弱口令进后台" in rows[0]["content"]
    assert rows[0]["track"] == "pentest" and rows[0]["capability"] == "web"
    evs = [e for e in bb.recent_events(project["id"])
           if e["kind"] == "campaign.memory"]
    assert len(evs) == 1 and evs[0]["payload"]["id"] == rows[0]["id"]
    bb.close()


def test_campaign_hook_writes_on_done(tmp_path):
    camp = CampaignMemory(tmp_path / "campaign.db")
    try:
        _make_agent_run_done(tmp_path, camp)
    finally:
        camp.close()


def test_campaign_no_output_done_skips_write(tmp_path):
    """M1 条件写入：done 但无 verified 产出 → campaign 不写（常规操作退役）。"""
    from core.agent import AgentConfig, AgentSession
    from test_agent import ScriptedLLM

    camp = CampaignMemory(tmp_path / "campaign.db")
    bb = Blackboard(str(tmp_path / "a.db"))
    try:
        project = bb.create_project("战役项目", "pentest", ["web"])
        from core.runtime import ExecutionGateway, NativeBackend
        gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
        tq = TaskQueue(bb)
        packs = tmp_path / "packs"
        (packs / "experts").mkdir(parents=True, exist_ok=True)
        (packs / "experts" / "_generalist.yaml").write_text(
            'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
        tid = tq.publish(project["id"], "常规巡检", created_by="human",
                         task_type="generic", scope="ip:10.0.0.2")
        llm = ScriptedLLM([
            {"tool_use": [ScriptedLLM.tool_call(
                "t1", "complete_task", {"result_note": "扫完了没发现"})]},
            {"tool_use": [ScriptedLLM.tool_call(
                "t1b", "complete_task", {"result_note": "扫完了没发现"})]},
            {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收尾"})]},
        ])
        agent = AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                             packs_root=packs, track="pentest", capabilities=["web"],
                             role="_generalist", capability_prompt="",
                             config=AgentConfig(max_steps=10), campaign=camp)
        agent.run_task("巡检", task_id=tid)
        assert tq.get_task(tid)["status"] == "done"
        assert camp.list_recent() == []
        evs = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "campaign.memory"]
        assert evs == []
    finally:
        camp.close()
        bb.close()


# ---------- M6 F1 死路记账档（orchestrator-efficiency §0-11） ----------

def _fp_finding_call(cid="fp1"):
    """FP-only 死路档测试辅助：登记一条 false-positive 发现（status 结构化口径）。"""
    from test_agent import ScriptedLLM
    return ScriptedLLM.tool_call(cid, "bb_add_finding", {
        "vuln_class": "sqli", "title": "union 注入走不通", "status": "false-positive",
        "severity": "low", "evidence": {"note": "waf 全拦，无旁路"}})


def test_campaign_dead_end_fp_only(tmp_path):
    """done + FP-only → campaign 写 tags=["dead_end"] 负知识（content 取收尾
    result_note）、不产 kb 复盘、事件 payload 带 dead_end=True。"""
    from core.agent import AgentConfig, AgentSession
    from test_agent import ScriptedLLM, _plan_call

    camp = CampaignMemory(tmp_path / "campaign.db")
    bb = Blackboard(str(tmp_path / "a.db"))
    try:
        project = bb.create_project("战役项目", "pentest", ["web"])
        from core.runtime import ExecutionGateway, NativeBackend
        gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
        tq = TaskQueue(bb)
        packs = tmp_path / "packs"
        (packs / "experts").mkdir(parents=True, exist_ok=True)
        (packs / "experts" / "_generalist.yaml").write_text(
            'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
        tid = tq.publish(project["id"], "打注入点", created_by="human",
                         task_type="exploit", scope="ip:10.0.0.3")
        llm = ScriptedLLM([
            {"tool_use": [_plan_call(), _fp_finding_call()]},
            {"tool_use": [ScriptedLLM.tool_call(
                "t1", "complete_task", {"result_note": "waf 全拦无旁路，此路不通"})]},
            {"tool_use": [ScriptedLLM.tool_call(
                "t1b", "complete_task", {"result_note": "waf 全拦无旁路，此路不通"})]},
            {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收尾"})]},
        ])
        agent = AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                             packs_root=packs, track="pentest", capabilities=["web"],
                             role="_generalist", capability_prompt="",
                             config=AgentConfig(max_steps=10), campaign=camp)
        # 判定口径直查：FP-only → campaign 写、不复盘、dead_end
        agent.run_task("打点", task_id=tid)
        verdict = agent._sediment_verdict(tid)
        assert verdict["campaign"] and verdict["dead_end"] and not verdict["review"]
        assert tq.get_task(tid)["status"] == "done"
        rows = camp.list_recent()
        assert len(rows) == 1
        assert rows[0]["tags"] == ["dead_end"]
        assert "waf 全拦" in rows[0]["content"]
        evs = [e for e in bb.recent_events(project["id"])
               if e["kind"] == "campaign.memory"]
        assert len(evs) == 1 and evs[0]["payload"]["dead_end"] is True
    finally:
        camp.close()
        bb.close()


def test_campaign_dead_end_fallback_title(tmp_path):
    """死路收尾为空 → content 取 FP finding title 兜底（〔死路〕前缀，负知识必须
    有内容可召回）。"""
    from core.agent import AgentConfig, AgentSession
    from test_agent import ScriptedLLM, _plan_call

    camp = CampaignMemory(tmp_path / "campaign.db")
    bb = Blackboard(str(tmp_path / "a.db"))
    try:
        project = bb.create_project("战役项目", "pentest", ["web"])
        from core.runtime import ExecutionGateway, NativeBackend
        gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
        tq = TaskQueue(bb)
        packs = tmp_path / "packs"
        (packs / "experts").mkdir(parents=True, exist_ok=True)
        (packs / "experts" / "_generalist.yaml").write_text(
            'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
        tid = tq.publish(project["id"], "打注入点", created_by="human",
                         task_type="exploit", scope="ip:10.0.0.4")
        llm = ScriptedLLM([
            {"tool_use": [_plan_call(), _fp_finding_call()]},
            {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
                                                {"result_note": ""})]},
            {"tool_use": [ScriptedLLM.tool_call("t1b", "complete_task",
                                                {"result_note": ""})]},
            {"tool_use": [ScriptedLLM.tool_call("t2", "finish", {"summary": "收尾"})]},
        ])
        agent = AgentSession(project_id=project["id"], bb=bb, gateway=gw, llm=llm,
                             packs_root=packs, track="pentest", capabilities=["web"],
                             role="_generalist", capability_prompt="",
                             config=AgentConfig(max_steps=10), campaign=camp)
        agent.run_task("打点", task_id=tid)
        rows = camp.list_recent()
        assert len(rows) == 1 and rows[0]["tags"] == ["dead_end"]
        assert rows[0]["content"] == "〔死路〕union 注入走不通"
    finally:
        camp.close()
        bb.close()


# ---------- workspace-hygiene D1：默认路径 data/ + 老位置惰性迁移（2026-09-23） ----------

def test_default_path_migrates_legacy(tmp_path, monkeypatch):
    """老位置 workspaces/campaign.db 在场 → 默认构造迁移到 data/，数据保留。"""
    monkeypatch.chdir(tmp_path)
    old = CampaignMemory("workspaces/campaign.db")  # 老位置预置含数据
    old.add("proj-a", "pentest", "web", "recon", "老打法", "内容X")
    old.close()
    c = CampaignMemory()  # 默认构造触发迁移
    assert c.path == Path("data/campaign.db")
    assert [r["title"] for r in c.list_recent()] == ["老打法"]
    assert not Path("workspaces/campaign.db").exists()
    assert Path("data/campaign.db").exists()
    c.close()


def test_default_path_fresh_build(tmp_path, monkeypatch):
    """无老库 → data/ 直建零迁移副作用。"""
    monkeypatch.chdir(tmp_path)
    c = CampaignMemory()
    assert c.list_recent() == []
    assert Path("data/campaign.db").exists()
    c.close()


def test_default_path_migration_locked_falls_back(tmp_path, monkeypatch):
    """老库被占用（rename 抛 OSError）→ 降级继续用老路径，数据完好不炸。"""
    monkeypatch.chdir(tmp_path)
    old = CampaignMemory("workspaces/campaign.db")
    old.add("proj-b", "ctf", "binary", "pwn", "占用场景", "内容Y")
    old.close()

    def boom(self, target):  # noqa: ANN001 —— 模拟旧进程持句柄
        raise OSError(13, "占用")
    monkeypatch.setattr(Path, "rename", boom)
    try:
        c = CampaignMemory()
        assert c.path == Path("workspaces/campaign.db")
        assert [r["title"] for r in c.list_recent()] == ["占用场景"]
    finally:
        monkeypatch.undo()
    c.close()


def test_explicit_path_skips_migration(tmp_path, monkeypatch):
    """显式传路径一律不迁移（测试 fixture 与注入用法天然豁免）。"""
    monkeypatch.chdir(tmp_path)
    Path("workspaces").mkdir()
    (Path("workspaces") / "campaign.db").write_bytes(b"junk-not-sqlite")
    c = CampaignMemory(tmp_path / "explicit.db")
    assert Path("workspaces/campaign.db").read_bytes() == b"junk-not-sqlite"
    c.close()
