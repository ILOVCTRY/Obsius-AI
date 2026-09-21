"""⑥ 战役记忆（简版，跨项目）测试。

- 写入：add 截 500 字；done 钩子（campaign_hook）并联沉淀 + campaign.memory 事件。
- 召回：关键词命中打分、track/capability 加成、热度×时间衰减排序、截 300 字、
  召回即计 usage；跨项目可见（不隔离 project_id）。
- 编排：orchestrator 注入 campaign 后态势出现「既往战役打法」段；零命中/未注入为空。
"""
import pytest

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
    # 热度足够时可反超：老记录再攒 4 次（10 次封顶，热 5.0 > 新记录 decay 1.0*0）
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
    """端到端：Agent 完成任务 → campaign_hook 沉淀 + campaign.memory 事件。"""
    from core.agent import AgentConfig, AgentSession
    from test_agent import ScriptedLLM

    bb = Blackboard(str(tmp_path / "a.db"))
    project = bb.create_project("战役项目", "pentest", ["web"])
    from core.runtime import ExecutionGateway, NativeBackend
    gw = ExecutionGateway(bb=bb, backends={"host": NativeBackend()})
    tq = TaskQueue(bb)
    packs = tmp_path / "packs"
    (packs / "tracks" / "pentest" / "roles").mkdir(parents=True, exist_ok=True)
    (packs / "tracks" / "pentest" / "roles" / "_generalist.yaml").write_text(
        'name: _generalist\npersona: "通用测试员。"\n', encoding="utf-8")
    tid = tq.publish(project["id"], "打 10.0.0.1", created_by="human",
                     task_type="generic", scope="ip:10.0.0.1")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "complete_task",
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
