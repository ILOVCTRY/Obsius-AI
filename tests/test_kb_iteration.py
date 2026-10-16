"""知识库迭代（kb-iteration，2026-10-14）验证。

分两层（与 test_challenge_asset.py 同口径）：
- **store / 逻辑层**（不依赖 create_app，本机必跑）：技能新建/删除提案的校验与应用
  （删除进回收站）、项目级开关归一化、规则信号与触发判据、run_iteration 端到端
  （假 LLM 生成提案落 pending）。
- **API 层**：PATCH 项目 config 消费 kb_iteration——依赖 create_app，本机
  FastAPI/Starlette 环境不兼容时自动 skip（见 MEMORY.md 已知坑）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from core.blackboard.store import Blackboard
from core.skills import kbiteration as ki
from core.skills import proposals as pm
from core.skills import writing
from core.skills.registry import SkillRegistry

SKILL_MD = (
    "---\nname: new-skill\ndescription: 新技能\nkeywords: 测试\n---\n"
    "# 新技能\n\n## 步骤\n1. 做点什么\n"
)


@pytest.fixture()
def board(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    pid = bb.create_project("迭代项目", "pentest")["id"]
    try:
        yield bb, pid
    finally:
        bb.close()


def _packs(tmp_path):
    """最小 packs 根：一个已存在的技能 + 一个 kb 域（供文件清单）。"""
    packs = tmp_path / "packs"
    sk = packs / "kb" / "skills" / "old-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: old-skill\ndescription: 旧\n---\n# 旧技能\n", encoding="utf-8")
    kb = packs / "kb" / "web" / "漏洞分类"
    kb.mkdir(parents=True)
    (kb / "sqli.md").write_text("# SQL 注入\n", encoding="utf-8")
    return packs


# ---------- 1. 开关归一化 ----------

def test_normalize_kb_iteration():
    assert ki.normalize_kb_iteration(None) == {"enabled": False}
    assert ki.normalize_kb_iteration({}) == {"enabled": False}
    assert ki.normalize_kb_iteration({"enabled": True}) == {"enabled": True}
    with pytest.raises(ValueError):
        ki.normalize_kb_iteration({"enabled": "yes"})
    with pytest.raises(ValueError):
        ki.normalize_kb_iteration([1])


def test_is_enabled_reads_project_config():
    assert ki.is_enabled(None) is False
    assert ki.is_enabled({"kb_iteration": {"enabled": True}}) is True
    assert ki.is_enabled({"kb_iteration": {"enabled": "bad"}}) is False  # 非法按关


# ---------- 2. 技能新建提案 ----------

def test_skill_create_proposal_and_apply(tmp_path):
    packs = _packs(tmp_path)
    p = pm.create_proposal(packs, {
        "target": {"kind": "skill", "name": "new-skill"},
        "mode": "create", "content": SKILL_MD,
        "summary": "沉淀新技能", "reason": "本轮验证有效 find-abc123def456",
    }, origin="agent")
    assert p["status"] == "pending"
    # 应用前：磁盘无此技能
    assert not (packs / "kb" / "skills" / "new-skill").exists()
    pm.apply_proposal(packs, p["id"], "human")
    assert (packs / "kb" / "skills" / "new-skill" / "SKILL.md").is_file()
    reg = SkillRegistry(packs)
    reg.load()
    assert reg.get("new-skill") is not None


def test_skill_create_rejects_existing_and_bad_name(tmp_path):
    packs = _packs(tmp_path)
    with pytest.raises(pm.ProposalError):
        pm.create_proposal(packs, {
            "target": {"kind": "skill", "name": "old-skill"},
            "mode": "create", "content": SKILL_MD.replace("new-skill", "old-skill"),
            "summary": "s", "reason": "r",
        })
    with pytest.raises(pm.ProposalError):
        pm.create_proposal(packs, {
            "target": {"kind": "skill", "name": "../evil"},
            "mode": "create", "content": SKILL_MD,
            "summary": "s", "reason": "r",
        })


def test_skill_create_rejects_frontmatter_mismatch(tmp_path):
    packs = _packs(tmp_path)
    with pytest.raises(pm.ProposalError):
        pm.create_proposal(packs, {
            "target": {"kind": "skill", "name": "new-skill"},
            "mode": "create", "content": SKILL_MD.replace("name: new-skill",
                                                          "name: other-name"),
            "summary": "s", "reason": "r",
        })


# ---------- 3. 技能删除提案（进回收站） ----------

def test_skill_delete_proposal_moves_to_trash(tmp_path):
    packs = _packs(tmp_path)
    p = pm.create_proposal(packs, {
        "target": {"kind": "skill", "name": "old-skill"},
        "mode": "delete", "summary": "删除过时技能", "reason": "内容已被替代",
    }, origin="agent")
    pm.apply_proposal(packs, p["id"], "human")
    # 原目录消失、回收站有备份、注册表不再列出
    assert not (packs / "kb" / "skills" / "old-skill").exists()
    trash = packs / "kb" / "skills" / ".history" / "skills-trash"
    assert trash.is_dir() and any(trash.iterdir())
    reg = SkillRegistry(packs)
    reg.load()
    assert reg.get("old-skill") is None


def test_skill_delete_missing_rejected(tmp_path):
    packs = _packs(tmp_path)
    with pytest.raises(pm.ProposalError):
        pm.create_proposal(packs, {
            "target": {"kind": "skill", "name": "no-such"},
            "mode": "delete", "summary": "s", "reason": "r",
        })


# ---------- 4. 规则信号与触发判据 ----------

def test_should_trigger_rules():
    assert ki.should_trigger({"repeat_failures": 2})[0] is True
    assert ki.should_trigger({"new_web_vulns": 1})[0] is True
    assert ki.should_trigger({"tool_failures": 3})[0] is True
    assert ki.should_trigger({"kb_refs": 2, "tool_failures": 2})[0] is True
    assert ki.should_trigger({"actions": 30})[0] is True
    assert ki.should_trigger({"actions": 3, "tool_failures": 1})[0] is False


def test_signals_of_counts_events_and_web_findings(board):
    bb, pid = board
    bb.append_event(pid, "tool.failed", {"name": "nmap"},
                    session_id="chat-x", author="chat-x")
    bb.append_event(pid, "tool.failed", {"name": "nmap"},
                    session_id="chat-x", author="chat-x")
    bb.append_event(pid, "skill.open", {"name": "old-skill"},
                    session_id="chat-x", author="chat-x")
    bb.append_event(pid, "command", {"cmd": "nmap"},
                    session_id="chat-x", author="chat-x")
    bb.add_finding(pid, vuln_class="sqli", title="登录处 SQL 注入",
                   status="verified", author="chat-x")
    sig = ki.signals_of(bb, pid, since_event_id=0)
    assert sig["tool_failures"] == 2
    assert sig["repeat_failures"] == 2
    assert sig["kb_refs"] == 1
    assert sig["actions"] == 1
    assert sig["new_web_vulns"] == 1


# ---------- 5. run_iteration 端到端（假 LLM） ----------

class _Resp:
    def __init__(self, text):
        self.text = text
        self.raw = {}


class _FakeLLM:
    def __init__(self, text):
        self._text = text
        self.calls = 0

    def chat(self, messages, system=None):
        self.calls += 1
        return _Resp(self._text)


def test_run_iteration_skips_without_signal(board, tmp_path):
    bb, pid = board
    packs = _packs(tmp_path)
    llm = _FakeLLM("{}")
    out = ki.run_iteration(bb, pid, packs_root=packs, llm=llm,
                           session_id="chat-x", author="chat-x",
                           thread_id="t1", capabilities=["web"],
                           since_event_id=0)
    assert out["triggered"] is False
    assert llm.calls == 0  # 无信号不调 LLM


def test_run_iteration_lands_proposal_on_signal(board, tmp_path):
    bb, pid = board
    packs = _packs(tmp_path)
    # 制造信号：同工具连续失败 2 次
    for _ in range(2):
        bb.append_event(pid, "tool.failed", {"name": "sqlmap"},
                        session_id="chat-x", author="chat-x")
    llm = _FakeLLM(json.dumps({"proposals": [{
        "kind": "skill", "mode": "create",
        "target": {"name": "new-skill"}, "content": SKILL_MD,
        "summary": "沉淀新技能", "reason": "sqlmap 反复失败，补技能",
    }]}))
    out = ki.run_iteration(bb, pid, packs_root=packs, llm=llm,
                           session_id="chat-x", author="chat-x",
                           thread_id="t1", capabilities=["web"],
                           since_event_id=0)
    assert out["triggered"] is True
    assert len(out["landed"]) == 1
    # 提案已落 pending，但未改磁盘
    pend = pm.list_proposals(packs, "pending")
    assert len(pend) == 1 and pend[0]["mode"] == "create"
    assert not (packs / "kb" / "skills" / "new-skill").exists()


def test_run_iteration_never_raises(board, tmp_path):
    bb, pid = board
    packs = _packs(tmp_path)

    class _Boom:
        def chat(self, messages, system=None):
            raise RuntimeError("LLM 炸了")

    for _ in range(2):
        bb.append_event(pid, "tool.failed", {"name": "x"},
                        session_id="chat-x", author="chat-x")
    out = ki.run_iteration(bb, pid, packs_root=packs, llm=_Boom(),
                           session_id="chat-x", author="chat-x",
                           thread_id="t1", capabilities=["web"],
                           since_event_id=0)
    # 红线：迭代是旁路，LLM 炸了也绝不向外抛
    assert out["landed"] == []
    assert "error" in out


# ---------- 6. 工具面（chat 模块开放 propose_pack_edit） ----------

def test_propose_pack_edit_open_to_chat_module():
    from core.chat.runtime import _EXPERT_EXCLUDED, _ORCH_BASE_TOOLS
    assert "propose_pack_edit" in _ORCH_BASE_TOOLS      # 主控
    assert "propose_pack_edit" not in _EXPERT_EXCLUDED  # 子专家也可


# ---------- 7. API 层（本机环境不兼容时 skip） ----------

@pytest.fixture()
def client(tmp_path):
    from core.api.app import create_app
    try:
        app = create_app(workspace_root=str(tmp_path / "workspaces"),
                         tools_root=None, executor_llm=None, planner_llm=None,
                         providers_config=str(tmp_path / "providers.json"))
    except AttributeError:  # 本机 FastAPI/Starlette 环境不兼容（见 MEMORY.md）
        pytest.skip("create_app 在本机不可用（FastAPI add_event_handler 已移除）")
    with TestClient(app) as c:
        yield c


def test_patch_project_config_kb_iteration(client):
    r = client.post("/api/projects",
                    json={"name": "迭代", "track": "pentest", "capabilities": ["web"]})
    pid = r.json()["id"]
    r = client.patch(f"/api/projects/{pid}/config",
                     json={"config": {"kb_iteration": {"enabled": True}}})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["kb_iteration"]["enabled"] is True
    # 非法值 422
    r = client.patch(f"/api/projects/{pid}/config",
                     json={"config": {"kb_iteration": {"enabled": "yes"}}})
    assert r.status_code == 422
