"""Orchestrator 测试（不触网）：态势视图 / 阶段接线 / 只读查询 / 对话轮 /
build_team·execute 新能力 / 结构化 tick 结果 / 思考流式。

任务机制退役（2026-10-06）后：delegate / plan_work / cancel_task / requeue_task /
task_detail / 饿死告警 / 租约回收 / replan 等针对旧机制的用例已删除；本文件只保留
通用编排器行为（tick 状态机、_stats/_overview 视图、gate 预算闸、轨注册表拒收、
阶段接线、build_team/execute）。

ScriptedLLM 与 test_agent.py 同款剧本回放（复制以保持测试文件独立可运行）。
"""

import json
import types

import pytest

from core.agent.tools import AGENT_TOOLS, ToolDispatcher
from core.blackboard import Blackboard
from core.llm.anthropic_compat import AnthropicCompatProvider
from core.orchestrator import Orchestrator, OrchestratorConfig
from core.team import TeamStore


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


class StreamScriptLLM(ScriptedLLM):
    """带 thinking 增量回放的剧本 LLM（编排器思考流式验证）：chat 前把 deltas
    逐片喂给 on_thinking；剧本项可带 thinking 键（拼 thinking 块进响应）。"""

    def __init__(self, script, deltas):
        super().__init__(script)
        self.deltas = list(deltas)
        self.streamed = False

    def chat(self, messages, *, system=None, tools=None, max_tokens=4096,
             temperature=None, on_thinking=None, on_text=None, should_cancel=None):
        self.streamed = on_thinking is not None
        for d in self.deltas:
            if on_thinking is not None:
                on_thinking(d)
        self.calls.append({"messages": json.loads(json.dumps(messages)), "system": system})
        item = self.script.pop(0)
        if "text" in item:
            content = [{"type": "text", "text": item["text"]}]
            stop = "end_turn"
        else:
            content = list(item["tool_use"])
            if item.get("thinking"):
                content = [{"type": "thinking", "thinking": item["thinking"]}] + content
            stop = "tool_use"
        return self._parse({"content": content, "stop_reason": stop,
                            "usage": {"input_tokens": 1, "output_tokens": 1}})


@pytest.fixture()
def env(tmp_path):
    bb = Blackboard(str(tmp_path / "o.db"))
    project = bb.create_project("编排测试", "ctf")
    yield bb, project
    bb.close()


def make_orch(env, llm, factory=None, config=None, track=None, packs_root="packs",
              gate=None, on_task_published=None, state_loader=None, state_saver=None,
              heartbeat=None, autonomy_provider=None, meta_loader=None):
    bb, project = env
    return Orchestrator(project_id=project["id"], bb=bb, llm=llm,
                        session_factory=factory, config=config or OrchestratorConfig(),
                        packs_root=packs_root, track=track,
                        gate=gate, on_task_published=on_task_published,
                        state_loader=state_loader, state_saver=state_saver,
                        heartbeat=heartbeat, autonomy_provider=autonomy_provider,
                        meta_loader=meta_loader)


# ---------- build_team / execute（2026-10-06 任务机制退役后的新能力） ----------

def test_build_team_rejects_and_creates_draft(env):
    """build_team：空名/空名册/重复 key/非法角色/白名单外角色各拒收；合法名册建
    draft 团队（不启动、不派单）。"""
    bb, project = env
    orch = make_orch(env, ScriptedLLM([]), track="pentest")
    assert orch._tool_build_team("", [{"member_key": "a"}]).startswith("[拒绝]")
    assert orch._tool_build_team("x", []).startswith("[拒绝]")
    assert orch._tool_build_team("x", [{"member_key": "a"},
                                       {"member_key": "a"}]).startswith("[拒绝]")
    assert orch._tool_build_team("x", [{"member_key": "a", "role": "nope"}]).startswith("[拒绝]")
    # 白名单外的合法角色也被拒
    orch_wl = make_orch(env, ScriptedLLM([]), track="pentest",
                        config=OrchestratorConfig(allowed_roles=["recon"]))
    assert orch_wl._tool_build_team(
        "x", [{"member_key": "a", "role": "external-entry"}]).startswith("[拒绝]")
    # 合法：draft 团队登记进 teams 视图，未启动
    out = orch._tool_build_team(
        "外网小队", [{"member_key": "recon", "label": "侦察", "role": "recon",
                     "responsibility": "被动测绘"}], "打穿外网")
    assert out.startswith("team=") and "draft 未启动" in out
    teams = orch._teams_view()
    assert teams["total"] == 1
    assert teams["items"][0]["name"] == "外网小队" and teams["items"][0]["status"] == "draft"


def test_execute_spawns_session_and_runs(env):
    """execute：建 _generalist 会话 → run_team_execution（无 task）；落 session.spawned、
    计入 live_sessions，dispatcher 步数被 execute_max_steps 夹住。"""
    bb, project = env

    class FakeSession:
        def __init__(self, role):
            self.session = bb.register_session(project["id"], f"w-{role}", role=role)
            self.dispatcher = types.SimpleNamespace(max_steps=100)
            self.contexts: list = []

        def run_team_execution(self, ctx):
            self.contexts.append(ctx)
            return "验证完成：入口可复现"

    made: dict[str, FakeSession] = {}

    def factory(role):
        made[role] = FakeSession(role)
        return made[role]

    orch = make_orch(env, ScriptedLLM([]), factory=factory)
    out = orch._tool_execute("验证入口可复现")
    assert out.startswith("已亲自执行") and "入口可复现" in out
    assert made["_generalist"].contexts[0].objective == "验证入口可复现"
    assert len(orch.live_sessions) == 1
    assert made["_generalist"].dispatcher.max_steps == orch.config.execute_max_steps
    assert "session.spawned" in [e["kind"] for e in bb.recent_events(project["id"])]


def test_execute_gate_and_missing_factory(env):
    """execute 前置护栏：空 objective 拒收；gate 拒绝回填；无 session_factory 报错。"""
    orch = make_orch(env, ScriptedLLM([]), factory=lambda r: None,
                     gate=lambda a: "blocked" if a == "execute" else None)
    assert orch._tool_execute("").startswith("[拒绝]")
    assert orch._tool_execute("干点活").startswith("[拒绝]")
    orch2 = make_orch(env, ScriptedLLM([]))
    assert orch2._tool_execute("干点活").startswith("[错误]")


def test_l0_build_team_and_execute_propose_only(env):
    """L0 提案模式：build_team/execute 只发 orch.proposed，不建团队、不开窗。"""
    bb, project = env
    orch = make_orch(env, ScriptedLLM([]), track="pentest",
                     config=OrchestratorConfig(propose_only=True))
    out = orch._tool_build_team(
        "队", [{"member_key": "a", "role": "recon", "responsibility": "测绘"}], "目标")
    assert "已提案" in out and orch._proposals[0]["op"] == "build_team"
    out2 = orch._tool_execute("亲自做")
    assert "已提案" in out2 and orch._proposals[1]["op"] == "execute"
    assert orch._teams_view()["total"] == 0
    assert orch.live_sessions == {}


def test_stats_teams_and_recent_runs_view(env):
    """_stats 团队视图（任务退役后替代 tasks 段）：teams 名册 + recent_runs 状态。"""
    bb, project = env
    pid = project["id"]
    orch = make_orch(env, ScriptedLLM([]), track="pentest")
    out = orch._tool_build_team(
        "小队", [{"member_key": "recon", "role": "recon", "responsibility": "测绘"}], "目标")
    team_id = out.split("team=")[1].split()[0]
    store = TeamStore(bb)
    pf = store.preflight(pid, team_id)
    store.create_run_and_members(
        pid, team_id, revision=pf["revision"],
        confirmations={"members": True, "goal": True, "safety": True, "execution": True})
    stats = orch._stats()
    assert stats["teams"]["total"] == 1 and stats["teams"]["items"][0]["id"] == team_id
    assert stats["recent_runs"][0]["team_id"] == team_id


# ---------- 态势视图（_stats / _assets_view / bb_overview） ----------

def test_stats_injection_hvt_surface_and_digest(env):
    """态势增强：HVT（meta.tags 高价值）优先排序 / in_progress 半程 / high_value
    covered 标记 / digest 常驻注入（任务段已随任务机制退役）。"""
    from core.blackboard.assets import register_asset
    bb, project = env
    pid = project["id"]
    hvt_covered = register_asset(bb, pid, "10.205.1.10", type_="host")
    bb.update_asset_meta(hvt_covered["id"], {"tags": ["高价值"]})
    bb.set_asset_status(hvt_covered["id"], "na", note="人工裁定不测（已覆盖终态）")
    hvt_open = register_asset(bb, pid, "10.205.9.9", type_="host")
    bb.update_asset_meta(hvt_open["id"], {"tags": ["高价值"]})
    register_asset(bb, pid, "10.205.1.20", type_="host")
    half = register_asset(bb, pid, "https://a.t.com/admin", type_="url")
    bb.set_asset_status(half["id"], "visited", note="看过首页")
    bb.append_event(pid, "project.digest",
                    {"digest": "# 上轮简报\n覆盖 40%"}, author="orchestrator")

    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="pentest")
    stats = orch._stats()
    assert stats["assets"]["uncovered"][0]["value"] == "10.205.9.9"  # HVT 优先排序
    assert stats["assets"]["done_count"] == 0  # 无 tested_clean（covered 走 na 终态）
    assert [i["value"] for i in stats["assets"]["in_progress"]] == \
        ["https://a.t.com/admin"]
    hv = {h["value"]: h for h in stats["high_value"]}
    assert hv["10.205.1.10"]["covered"] is True
    assert hv["10.205.9.9"]["covered"] is False

    orch.tick()
    system = llm.calls[0]["system"]
    assert '"high_value"' in system and "10.205.9.9" in system \
        and "10.205.1.10" in system
    assert "in_progress" in system and "https://a.t.com/admin" in system
    assert "上一份简报（常驻" in system and "覆盖 40%" in system


def test_stats_no_hvt_no_section(env):
    """无高价值标签资产时 high_value 为空列表（资产本体仍走 uncovered 注入，属正常）。"""
    from core.blackboard.assets import register_asset
    bb, project = env
    register_asset(bb, project["id"], "10.0.0.1", type_="host")
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="pentest")
    stats = orch._stats()
    assert stats["high_value"] == []


def test_uncovered_excludes_na_assets(env):
    """na（人工裁定不测）同属终态，不进 uncovered（对齐 coverage.py
    TERMINAL_STATUSES）：此前 covered 状态集漏 na，虚增 uncovered_total。"""
    from core.blackboard.assets import register_asset
    bb, project = env
    pid = project["id"]
    na_a = register_asset(bb, pid, "10.0.0.2", type_="host")
    bb.set_asset_status(na_a["id"], "na", note="不对此资产测试")
    register_asset(bb, pid, "10.0.0.3", type_="host")
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="pentest")
    stats = orch._stats()
    vals = [u["value"] for u in stats["assets"]["uncovered"]]
    assert "10.0.0.3" in vals and "10.0.0.2" not in vals
    assert stats["assets"]["uncovered_total"] == 1
    orch.tick()
    assert '"high_value": []' in llm.calls[0]["system"]


def test_budget_status_null_is_unlimited(env):
    """预算缺省/null 的确定语义外显：*_effective=unlimited，
    不再裸抛 null 让调用方误读为「预算没接线」；不限预算不输出 used/remaining。"""
    bb, project = env
    orch = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]))
    out = json.loads(orch._tool_budget_status())
    assert out["token_budget_effective"] == "unlimited"
    assert out["task_budget_effective"] == "unlimited"
    assert "used" not in out["tokens"] and out.get("tasks_remaining") is None


def test_budget_status_positive_effective(env):
    """正整数预算：原值保留、effective=同值，输出 remaining/pct。"""
    bb, project = env
    pid = project["id"]
    bb.update_project_config(
        pid, {"autonomy": {"token_budget": 1000, "task_budget": 5}})
    orch = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]))
    out = json.loads(orch._tool_budget_status())
    assert out["token_budget"] == 1000 and out["token_budget_effective"] == 1000
    assert out["task_budget"] == 5 and out["task_budget_effective"] == 5
    assert out["tokens"]["used"] == 0 and out["tokens"]["remaining"] == 1000
    assert out["tokens"]["pct"] == 0 and out["tasks_remaining"] == 5


def test_bb_overview_asset_filters(env):
    """bb_overview 资产过滤：type/status 枚举校验 + 精简清单
    matched_total/shown/items + limit 截断；非法枚举返回 [错误]。"""
    from core.blackboard.assets import register_asset
    bb, project = env
    pid = project["id"]
    register_asset(bb, pid, "10.0.0.1", type_="host")
    h2 = register_asset(bb, pid, "10.0.0.2", type_="host")
    bb.set_asset_status(h2["id"], "visited", note="看过")
    register_asset(bb, pid, "10.0.0.1:443", type_="service")  # 非 host 行验过滤
    orch = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]))

    by_type = json.loads(orch._tool_bb_overview("assets", asset_type="host")
                         .split("\n", 1)[1])
    assert by_type["matched_total"] == 2 and by_type["shown"] == 2
    assert {i["type"] for i in by_type["items"]} == {"host"}

    by_status = json.loads(orch._tool_bb_overview(
        "assets", asset_status="visited").split("\n", 1)[1])
    assert by_status["matched_total"] == 1 and by_status["items"][0]["id"] == h2["id"]

    limited = json.loads(orch._tool_bb_overview(
        "assets", asset_type="host", limit=1).split("\n", 1)[1])
    assert limited["matched_total"] == 2 and limited["shown"] == 1

    assert orch._tool_bb_overview("assets", asset_type="wat").startswith("[错误]")
    assert orch._tool_bb_overview("assets", asset_status="nope").startswith("[错误]")


def test_high_value_derived_from_verified_high_finding(env):
    """high_value 自动推导：承载 verified 且 severity≥high 的发现的资产自动进
    高价值段（derived=true），无 tag 也呈现；verified medium / unverified high 不推导。"""
    from core.blackboard.assets import register_asset
    bb, project = env
    pid = project["id"]
    a1 = register_asset(bb, pid, "10.0.1.1", type_="host")
    bb.add_finding(pid, title="RCE", severity="high", status="verified",
                   target_asset_id=a1["id"], author="s1")
    a2 = register_asset(bb, pid, "10.0.1.2", type_="host")
    bb.add_finding(pid, title="普通越权", severity="medium", status="verified",
                   target_asset_id=a2["id"], author="s2")
    a3 = register_asset(bb, pid, "10.0.1.3", type_="host")
    bb.add_finding(pid, title="待验 RCE", severity="critical", status="unverified",
                   target_asset_id=a3["id"], author="s3")
    orch = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]), track="pentest")
    hv = {h["id"]: h for h in orch._stats()["high_value"]}
    assert a1["id"] in hv and hv[a1["id"]]["derived"] is True
    assert hv[a1["id"]]["findings"][0]["title"] == "RCE"
    assert a2["id"] not in hv and a3["id"] not in hv


def test_assets_view_uncovered_and_by_type(env):
    """_stats 资产视图——by_type 计数 + 未覆盖清单（任务退役后覆盖口径只看资产
    状态：visited/scanning/tested_clean/na 算已覆盖，open 未覆盖）。"""
    bb, project = env
    a_cov = bb.upsert_asset(project["id"], "domain", "covered.com")["id"]
    a_unc = bb.upsert_asset(project["id"], "domain", "uncovered.com")["id"]
    a_vis = bb.upsert_asset(project["id"], "domain", "visited.com")["id"]
    bb.upsert_asset(project["id"], "host", "10.0.0.8")
    bb.set_asset_status(a_cov, "na", note="人工裁定不测（终态）")
    bb.set_asset_status(a_vis, "visited", note="已访问")
    orch = make_orch(env, ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]))
    stats = orch._stats()
    av = stats["assets"]
    assert av["by_type"]["domain"] == 3 and av["by_type"]["host"] == 1
    ids = {u["id"] for u in av["uncovered"]}
    assert a_unc in ids and a_cov not in ids and a_vis not in ids
    assert av["uncovered_total"] == 2  # a_unc + 10.0.0.8
    # visited/tested_clean 视为覆盖 → 不进 uncovered；scanning/na 同口径


def test_assets_view_coverage_summary(env):
    """B2 覆盖度对账并入 _assets_view（_stats 同源）：分组收敛行 + overall，
    「全景对账」类数数任务归零；对账失败不阻断其余态势段。
    D3：父收敛由子树读时派生——url 终态即逐层带出 host/domain。"""
    bb, project = env
    dom = bb.upsert_asset(project["id"], "domain", "site.com")["id"]
    host = bb.upsert_asset(project["id"], "host", "www.site.com", parent_id=dom)["id"]
    url = bb.upsert_asset(project["id"], "url", "http://www.site.com/a",
                          parent_id=host)["id"]
    orch = make_orch(env, ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]))
    cov = orch._stats()["assets"]["coverage"]
    assert cov["groups_done"] == "0/1" and cov["converged"] == "0/3"
    assert any(r.startswith("domain:site.com 收敛 0/3") and "未收口" in r
               for r in cov["by_group"])
    # url 终态 → host、domain 逐层派生收口（host/domain 本体 open 均不挡）
    # tested_clean 须死路意图背书（2026-09-25 门禁）
    from core.blackboard.intents import close_intent, declare_intent
    hid = bb.add_http_history(project["id"], source="browser", method="GET",
                              url="http://www.site.com/probe", status=404,
                              resp_body="")
    iid = declare_intent(bb, project["id"], "url 面无洞",
                         target_asset_id=url)["id"]
    close_intent(bb, project["id"], iid, "dead_end", dead_reason="探测无异常",
                 evidence_refs=[f"http:{hid}"])
    bb.set_asset_status(url, "tested_clean", note="测完")
    cov2 = orch._stats()["assets"]["coverage"]
    assert cov2["groups_done"] == "1/1" and cov2["converged"] == "3/3"
    assert any("已收口" in r for r in cov2["by_group"])


def test_mission_view_in_stats_and_prompt(env):
    """R2 轨级语义 + goal 统一：ROE 进 _stats 与系统提示（行动边界段）；
    mission 文本/判据行退役（目标与判据由 goal_section 承担，meta.phase_goal）。"""
    bb, project = env
    bb.update_project_config(project["id"], {
        "mission": {"text": "拿到域控", "criteria": "□ 拿到域管哈希\n□ 截图留证"},
        "redteam_roe": {"targets": "*.corp.local", "window": "w",
                          "exclusions": "工控段", "approver": "owner"},
    })
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="redteam",
                     meta_loader=lambda: {"phase_goal": {
                         "text": "本周打穿靶场", "criteria": ["□ goal 判据一"]}})
    stats = orch._stats()
    assert stats["mission"]["track"] == "redteam"
    assert stats["mission"]["roe"]["targets"] == "*.corp.local"
    orch.tick()
    system = llm.calls[0]["system"]
    # 行动边界段：ROE 四要素仍在；mission 注入行退役
    assert "行动边界：红队行动" in system and "*.corp.local" in system
    assert "- mission: 拿到域控" not in system and "- 判据清单" not in system
    # goal_section：阶段目标 + 判据注入（判据第一优先源）
    assert "本周打穿靶场" in system and "□ goal 判据一" in system


def test_role_catalog_injected_into_prompt(env):
    """接线轨后系统提示含角色目录（name + description），组队决策不再只见名字。"""
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="ctf")
    names = {r["name"] for r in orch.role_catalog}
    assert {"_generalist", "triage", "reverse"} <= names
    orch.tick()
    system = llm.calls[0]["system"]
    assert "可开角色目录" in system and "静态分诊" in system


def test_stats_findings_sessions_context_budget(env):
    """findings 只进 top 20（verified/exploited 优先 → severity 降序）+
    findings_total/findings_truncated 字段；closed 会话出清只留活跃 + sessions_closed 计数。"""
    bb, project = env
    pid = project["id"]
    # 25 条：2 open high（必进）+ 1 verified low（verified 优先必进）+ 22 open low（挤出）
    for i in range(2):
        bb.add_finding(pid, "sqli", f"高危注入 {i}", severity="high", dedup_key=f"dk-h{i}")
    bb.add_finding(pid, "info-leak", "已验证低危", severity="low", status="verified",
                   dedup_key="dk-v")
    for i in range(22):
        bb.add_finding(pid, "info-leak", f"低危 {i}", severity="low", dedup_key=f"dk-l{i}")
    # 会话：2 活跃 + 3 已关
    for i in range(2):
        bb.register_session(pid, f"live-{i}")
    for i in range(3):
        s = bb.register_session(pid, f"gone-{i}")
        bb.close_session(s["id"])

    orch = Orchestrator(project_id=pid, bb=bb, llm=ScriptedLLM([]),
                        config=OrchestratorConfig(), packs_root="packs", track="ctf")
    stats = orch._stats()

    assert len(stats["findings"]) == 20
    assert stats["findings_total"] == 25 and stats["findings_truncated"] is True
    ids = {f["id"] for f in stats["findings"]}
    # verified 与 high 优先保进
    assert next(f["id"] for f in bb.list_findings(pid) if f["status"] == "verified") in ids
    highs = {f["id"] for f in bb.list_findings(pid) if f["severity"] == "high"}
    assert highs <= ids
    # 排序：verified/exploited 在前、severity 降序
    top = stats["findings"]
    assert top[0]["status"] == "verified" or top[0]["severity"] in ("critical", "high")

    assert len(stats["sessions"]) == 2
    assert all(s["status"] != "closed" for s in stats["sessions"])
    assert stats["sessions_closed"] == 3


def test_overview_event_window_excludes_observation_kinds(env):
    """事件窗剔除纯观测 kind（llm.usage/llm.thinking.delta）——游标照推不重放，
    有信息量事件照常进窗。"""
    bb, project = env
    pid = project["id"]
    orch = Orchestrator(project_id=pid, bb=bb, llm=ScriptedLLM([]),
                        config=OrchestratorConfig(), packs_root="packs", track="ctf")
    bb.append_event(pid, "llm.usage", {"total_tokens": 1})
    bb.append_event(pid, "llm.thinking.delta", {"text": "x"})
    bb.append_event(pid, "task.published", {"task_id": "t1"})
    bb.append_event(pid, "llm.usage", {"total_tokens": 2})

    overview = orch._overview()
    assert "task.published" in overview
    assert "llm.usage" not in overview and "llm.thinking.delta" not in overview
    # 游标推进到 tip：下轮不再回放
    assert orch._last_event_id == bb.latest_event_id(pid)


# ---------- 结构化 tick 结果 / 状态持久化 ----------

def test_tick_returns_structured_result(env):
    """tick 返回 {summary,published,spawned,teams,digest,proposals}；动作各归各位
    （任务退役后 published 恒空，组队走 teams）。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("b1", "build_team", {
            "name": "外网小队", "goal_text": "打穿外网",
            "members": [{"member_key": "recon", "label": "侦察",
                         "responsibility": "被动测绘"}]})]},
        {"tool_use": [ScriptedLLM.tool_call("g1", "write_digest",
                                            {"summary": "本轮：建队"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm)
    result = orch.tick()
    assert set(result) == {"summary", "published", "spawned", "teams", "digest", "proposals"}
    assert result["published"] == [] and result["spawned"] == []
    assert len(result["teams"]) == 1 and result["teams"][0]["name"] == "外网小队"
    assert result["digest"] == "本轮：建队"
    assert result["proposals"] == []
    assert "build_team" in result["summary"] and "write_digest" in result["summary"]


def test_tick_analyze_only_reads_and_reports(env):
    """研判轮（analyze_only）：写类工具根本不下发（硬约束，调不了而非不许调）；
    最后一条 assistant 文本捕获为 result.analysis（done 前的完整计划胜出中途草稿）。"""
    llm = ScriptedLLM([
        {"text": "草稿：初步研判……"},
        {"text": "## 态势小结\n已控 1 台。\n## 建议路径\n① 横向移动。"},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, config=OrchestratorConfig(analyze_only=True))
    names = {t["name"] for t in orch._orch_tools()}
    assert "build_team" not in names and "execute" not in names
    assert "write_digest" not in names and "done" in names
    assert "bb_overview" in names
    result = orch.tick()
    assert result["published"] == [] and result["spawned"] == []
    assert "建议路径" in result["analysis"] and "① 横向移动" in result["analysis"]
    assert "草稿" not in result["analysis"]


def test_tick_state_persists_across_instances(env):
    """游标/轮数/简报轮经 loader/saver 跨实例持久：第二实例 cycles=2，overview
    只见游标之后的新事件（不重复消费），digest_every 判定跨重启生效。"""
    bb, project = env
    persisted: dict = {}

    def loader():
        return dict(persisted)

    def saver(**fields):
        persisted.update(fields)

    orch1 = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("g1", "write_digest",
                                            {"summary": "首轮简报"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ]), state_loader=loader, state_saver=saver)
    r1 = orch1.tick()
    assert persisted["cycles"] == 1
    assert persisted["last_digest_cycle"] == 1

    # 两轮之间来一个新事件（tick1 自身事件同样在游标后，下轮可见——既有口径）
    bb.append_event(project["id"], "custom.after_tick1", {"n": 42}, author="human")

    llm2 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch2 = make_orch(env, llm2, state_loader=loader, state_saver=saver)
    r2 = orch2.tick()
    assert orch2.cycles == 2
    assert orch2.last_digest_cycle == 1  # 首轮简报轮跨实例带回
    assert persisted["cycles"] == 2
    assert "custom.after_tick1" in llm2.calls[0]["system"] and "42" in llm2.calls[0]["system"]
    assert r2["digest"] is None  # 本轮没写简报
    cursor_after_2 = persisted["event_cursor"]
    assert cursor_after_2 >= 1

    # 第三轮无新事件：游标之后为空（证明游标确实跨实例装载/落盘，不重复消费）
    llm3 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch3 = make_orch(env, llm3, state_loader=loader, state_saver=saver)
    orch3.tick()
    assert orch3.cycles == 3
    # tick3 仍会前进（消费 tick2 自身 chat 后的 llm.usage，既有口径），但
    # custom 事件已在 tick2 消费、绝不回头重现——游标跨实例装载/落盘的关键证据
    assert persisted["event_cursor"] >= cursor_after_2
    assert "custom.after_tick1" not in llm3.calls[0]["system"]
    assert "42" not in llm3.calls[0]["system"]


def test_tick_cursor_jumps_to_tip_over_large_backlog(env):
    """backlog >100：首轮只喂最新 100 条，游标一次跳到 tick 开始时末端，
    旧 backlog 不逐轮回放（走查抓到的每轮 +100 爬行回归）。"""
    bb, project = env
    last_id = 0
    for i in range(105):
        last_id = bb.append_event(project["id"], "custom.old", {"n": i}, author="human")
    assert bb.latest_event_id(project["id"]) == last_id
    persisted: dict = {}
    llm1 = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch1 = make_orch(env, llm1, state_loader=lambda: dict(persisted),
                      state_saver=lambda **f: persisted.update(f))
    orch1.tick()
    sys1 = llm1.calls[0]["system"]
    assert persisted["event_cursor"] == last_id  # 一次跳到末端，不是 +100
    assert '"n": 104' in sys1 and '"n": 0' not in sys1  # 最新窗口喂 LLM，最旧事件不喂
    # 第二轮：105 条旧事件全部不回放（只可能见 tick1 自身 chat 后的 llm.usage）
    llm2 = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch2 = make_orch(env, llm2, state_loader=lambda: dict(persisted),
                      state_saver=lambda **f: persisted.update(f))
    orch2.tick()
    assert "custom.old" not in llm2.calls[0]["system"]


def test_tick_heartbeat_called_each_llm_step(env):
    """每个 LLM 步前调 heartbeat（API 层绑定 tick 租约续租），tick 结束不调用。"""
    beats: list[int] = []
    llm = ScriptedLLM([
        {"text": "先想想"},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, heartbeat=lambda: beats.append(1))
    orch.tick()
    assert len(beats) == 2  # 两次 chat，两次心跳


def test_digest_lands_as_project_event(env):
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("g1", "write_digest",
                                            {"summary": "1 个待验证发现，建议组队核查"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm)
    orch.tick()
    digests = [e for e in bb.recent_events(project["id"]) if e["kind"] == "project.digest"]
    assert len(digests) == 1
    assert digests[0]["payload"]["digest"].startswith("1 个待验证发现")
    assert digests[0]["author"] == "orchestrator"


def test_digest_due_forced_in_prompt(env):
    """digest_every=1 且从未写过简报 → 系统提示要求本轮必须 write_digest。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("g1", "write_digest", {"summary": "s"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, config=OrchestratorConfig(digest_every=1))
    orch.tick()
    assert "必须 write_digest" in llm.calls[0]["system"]


def test_chat_usage_is_recorded(env):
    """orch 每次 chat 的用量落 llm.usage 事件（ScriptedLLM 剧本回 1+1 token）。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm)
    orch.tick()
    usages = [e for e in bb.recent_events(project["id"]) if e["kind"] == "llm.usage"]
    assert len(usages) == 1
    assert usages[0]["payload"]["source"] == "orchestrator"
    assert usages[0]["payload"]["input_tokens"] == 1


def test_l0_autonomy_notice_in_system_prompt(env):
    """L0：系统提示经 {autonomy_notice} 槽注入提案模式说明。"""
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, config=OrchestratorConfig(propose_only=True))
    orch.tick()
    system = llm.calls[0]["system"]
    assert "L0（全手动·提案模式）" in system
    assert "orch.proposed" in system and "采纳" in system


# ---------- func_kb 工具（逆向多会话查重的底层） ----------

def test_dispatcher_func_kb_upsert_and_query(tmp_path):
    bb = Blackboard(str(tmp_path / "f.db"))
    project = bb.create_project("func 测试", "ctf")
    sha = "a" * 64
    d1 = ToolDispatcher(bb, gateway=None,
                        project_id=project["id"], session_id="sess-1", author="sess-1")
    d2 = ToolDispatcher(bb, gateway=None,
                        project_id=project["id"], session_id="sess-2", author="sess-2")
    d1._intent_lead_passed = d2._intent_lead_passed = True  # 本测主题=func kb 读写，跳过意图先行闸
    r1 = d1.dispatch("bb_upsert_func",
                     {"binary_sha256": sha, "address": 0x1189, "name": "sub_1189",
                      "analysis": "长度校验 21"})
    assert "created=True" in r1
    # 会话 2 对同一地址补充分析 → 合并而非新建（防重复劳动）
    r2 = d2.dispatch("bb_upsert_func",
                     {"binary_sha256": sha, "address": 0x1189, "name": "check_flag",
                      "analysis": "逐字节 XOR 0x5A 后与密文比较", "risk_tags": ["crypto"]})
    assert "created=False" in r2
    q = json.loads(d2.dispatch("bb_query", {"what": "func", "binary_sha256": sha,
                                            "address": 0x1189}))
    assert q["known"] and q["name"] == "check_flag"
    assert q["analysis"].endswith("逐字节 XOR 0x5A 后与密文比较")
    listed = json.loads(d1.dispatch("bb_query", {"what": "func", "binary_sha256": sha}))
    assert len(listed) == 1 and listed[0]["risk_tags"] == ["crypto"]
    # 列表出口地址一律 hex 字符串（§9 地址纪律）
    assert listed[0]["address"] == "0x1189"
    # 入参 int / hex 串皆收（同一地址，命中幂等）
    q2 = json.loads(d2.dispatch("bb_query",
                                {"what": "func", "binary_sha256": sha, "address": "0x1189"}))
    assert q2["known"] and q2["name"] == "check_flag"
    bad = d1.dispatch("bb_upsert_func",
                      {"binary_sha256": sha, "address": "not-addr", "name": "x"})
    assert bad.startswith("[错误]")
    # AGENT_TOOLS 已含 bb_upsert_func（schema 就位）
    assert any(t["name"] == "bb_upsert_func" for t in AGENT_TOOLS)
    bb.close()


# ---------- 对话化编排器（M1/M2/M3：对话插队轮 / goal 闭环 / 拟人） ----------

def test_chat_turn_replies_logs_and_heartbeats(env):
    """对话轮：文本回复落 orch.chat（author=orchestrator）+ 记账 source=orchestrator-chat
    + 每步前 heartbeat 续租。"""
    bb, project = env
    beats: list[int] = []
    llm = ScriptedLLM([{"text": "编排器在线，当前无未覆盖资产。"}])
    orch = make_orch(env, llm, heartbeat=lambda: beats.append(1))
    result = orch.chat_turn("在吗？")
    assert result["reply"].startswith("编排器在线")
    chats = [e for e in bb.recent_events(project["id"]) if e["kind"] == "orch.chat"]
    assert len(chats) == 1
    assert chats[0]["payload"]["role"] == "orch"
    assert chats[0]["author"] == "orchestrator"
    assert chats[0]["payload"]["tool_trace"] == []
    assert len(beats) == 1
    usages = [e for e in bb.recent_events(project["id"]) if e["kind"] == "llm.usage"]
    assert usages and usages[-1]["payload"]["source"] == "orchestrator-chat"


def test_chat_turn_can_build_team_via_tools(env):
    """对话轮工具面与 tick 同源：build_team 照走（组 draft 团队），tool_trace 记录。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("b1", "build_team", {
            "name": "对话建队", "goal_text": "目标",
            "members": [{"member_key": "a", "responsibility": "干活"}]})]},
        {"text": "已组建 1 支 draft 团队。"},
    ])
    orch = make_orch(env, llm)
    result = orch.chat_turn("组个队")
    assert orch._teams_view()["total"] == 1
    chats = [e for e in bb.recent_events(project["id"]) if e["kind"] == "orch.chat"]
    assert [t["name"] for t in chats[-1]["payload"]["tool_trace"]] == ["build_team"]
    assert result["reply"].startswith("已组建")


def test_chat_turn_is_readonly_over_tick_state(env):
    """插队轮只读边界：不写 state_saver、不推进 event_cursor、不计 cycles、
    不消费 C2 指令；下一轮 tick 照常看到全部。"""
    bb, project = env
    pid = project["id"]
    saved: dict = {}

    def saver(**fields):
        saved.update(fields)

    bb.append_event(pid, "orch.directive", {"text": "先做 A"}, author="human")
    llm = ScriptedLLM([{"text": "收到，情况如下。"}])
    orch = make_orch(env, llm, track="ctf", state_loader=lambda: {},
                     state_saver=saver)
    orch.chat_turn("现在什么情况？")
    assert saved == {}
    assert orch.cycles == 0 and orch._last_event_id == 0
    kinds = [e["kind"] for e in bb.recent_events(pid)]
    assert "orch.directive.done" not in kinds
    # 下一个 tick 照常消费：指令进提示、游标落盘
    orch.llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch.tick()
    tick_sys = orch.llm.calls[0]["system"]
    assert "先做 A" in tick_sys
    assert saved.get("cycles") == 1


def test_chat_history_window_cap_40(env):
    """上下文 = 最近 40 条 orch.chat + 本次 user 消息在末尾；最旧被窗口挤掉。"""
    bb, project = env
    pid = project["id"]
    for i in range(42):
        bb.append_event(pid, "orch.chat", {"role": "human", "text": f"旧消息{i}"},
                        author="human")
    llm = ScriptedLLM([{"text": "ok"}])
    orch = make_orch(env, llm)
    orch.chat_turn("最新一条")
    msgs = llm.calls[0]["messages"]
    assert len(msgs) == 41
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "旧消息2"
    assert msgs[-1] == {"role": "user", "content": "最新一条"}


def test_chat_history_roles_and_lead_orch_skip(env):
    """human→user / orch→assistant 组装；开头连续 orch 消息跳过（首条必 user）。"""
    bb, project = env
    pid = project["id"]
    bb.append_event(pid, "orch.chat", {"role": "orch", "text": "孤立回复"},
                    author="orchestrator")
    bb.append_event(pid, "orch.chat", {"role": "human", "text": "第一问"}, author="human")
    bb.append_event(pid, "orch.chat", {"role": "orch", "text": "第一答"},
                    author="orchestrator")
    llm = ScriptedLLM([{"text": "第二答"}])
    orch = make_orch(env, llm)
    orch.chat_turn("第二问")
    msgs = llm.calls[0]["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "第一问"), ("assistant", "第一答"), ("user", "第二问")]


def test_orch_compact_persists_summary_and_trims_history(env):
    """手动持久压缩（/compact，2026-10-06）：旧 orch.chat 压成摘要落 orch.compact，
    后续 _chat_history 从「摘要 + 截止之后的近期消息」起跑；历史过短 noop 不烧 LLM。"""
    bb, project = env
    pid = project["id"]
    ids: list[int] = []
    for i in range(6):  # 6 轮 = 12 条
        ids.append(bb.append_event(pid, "orch.chat", {"role": "human", "text": f"问{i}"},
                                   author="human"))
        ids.append(bb.append_event(pid, "orch.chat", {"role": "orch", "text": f"答{i}"},
                                   author="orchestrator"))
    llm = ScriptedLLM([{"text": "## 摘要\n人类要测 X。"}])
    orch = make_orch(env, llm)
    res = orch.compact_chat(keep_recent=8)
    assert res["status"] == "compacted"
    assert res["before_msgs"] == 12 and res["summarized"] == 4
    evs = [e for e in bb.recent_events(pid) if e["kind"] == "orch.compact"]
    assert len(evs) == 1
    assert evs[0]["payload"]["cutoff_id"] == ids[3]

    # 后续对话轮：摘要前置 + 截止之后的消息（问2…答5）+ 本次提问
    llm2 = ScriptedLLM([{"text": "新答"}])
    orch.llm = llm2
    orch.chat_turn("新问")
    msgs = llm2.calls[0]["messages"]
    assert msgs[0]["role"] == "user" and "历史摘要" in msgs[0]["content"]
    assert msgs[1] == {"role": "assistant", "content": "已读摘要，继续。"}
    assert msgs[2] == {"role": "user", "content": "问2"}
    assert msgs[-1] == {"role": "user", "content": "新问"}

    # 过短 noop（新项目，零历史）不烧 LLM
    pid2 = bb.create_project("空项目", "ctf")["id"]
    llm3 = ScriptedLLM([])
    orch2 = Orchestrator(project_id=pid2, bb=bb, llm=llm3,
                         config=OrchestratorConfig(), packs_root="packs")
    assert orch2.compact_chat()["status"] == "noop"
    assert len(llm3.script) == 0


def test_orch_context_usage_and_autocompact(env):
    """指挥上下文用量（/context，2026-10-06）：窗口=供应商 model_context（缺省 256K）；
    占用取最近编排 llm.usage 的 input（无则估算）；达 85% → 持久压缩落 orch.compact。"""
    from core.autonomy import record_llm_usage
    from core.llm.provider import Usage
    bb, project = env
    pid = project["id"]
    llm = ScriptedLLM([{"text": "摘要"}])
    orch = make_orch(env, llm)

    u = orch.context_usage()
    assert u["window"] == 256_000 and u["source"] == "estimated"
    b = u["breakdown"]
    assert b["system"] + b["tools"] + b["messages"] == u["used"]

    llm.context_tokens = 1000
    record_llm_usage(bb, pid, Usage(input_tokens=900, output_tokens=3),
                     source="orchestrator-chat", session_id=None, model="m")
    u2 = orch.context_usage()
    assert u2["window"] == 1000 and u2["used"] == 900 and u2["threshold"] == 850

    # 阈值未到 → 不压缩
    record_llm_usage(bb, pid, Usage(input_tokens=100, output_tokens=1),
                     source="orchestrator-chat", session_id=None, model="m")
    orch._maybe_autocompact()
    assert not [e for e in bb.recent_events(pid) if e["kind"] == "orch.compact"]

    # 达阈值 + 有历史 → 自动压缩
    for i in range(6):
        bb.append_event(pid, "orch.chat", {"role": "human", "text": f"问{i}"},
                        author="human")
        bb.append_event(pid, "orch.chat", {"role": "orch", "text": f"答{i}"},
                        author="orchestrator")
    record_llm_usage(bb, pid, Usage(input_tokens=900, output_tokens=3),
                     source="orchestrator-chat", session_id=None, model="m")
    orch._maybe_autocompact()
    evs = [e for e in bb.recent_events(pid) if e["kind"] == "orch.compact"]
    assert len(evs) == 1


def test_delegation_discipline_injected_both_prompts(env):
    """委托纪律段注入 tick 与对话轮系统提示；提示词不再出现已退役工具名
    publish_task/spawn_session，真实工具名 build_team/execute 在工具段可见。"""
    bb, project = env
    llm = ScriptedLLM([{"text": "好"}])
    orch = make_orch(env, llm)
    orch.chat_turn("在吗")
    chat_sys = llm.calls[0]["system"]
    assert "委托纪律" in chat_sys and "综合是你的活" in chat_sys
    orch.llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch.tick()
    tick_sys = orch.llm.calls[0]["system"]
    assert "委托纪律" in tick_sys and "委托单必须自包含" in tick_sys
    for sys in (chat_sys, tick_sys):
        assert "publish_task" not in sys and "spawn_session" not in sys
        assert "build_team" in sys and "execute" in sys  # 真实工具名在工具段可见


def test_goal_and_persona_injection_chat_and_tick(env):
    """goal_section 进 tick+chat 系统提示；persona 只进 chat（tick 不需要脸）。"""
    bb, project = env
    meta = {"phase_goal": {"text": "本周打穿靶场 3 台主机",
                           "criteria": ["拿到 flag", "截图留证"],
                           "phase": "initial-access", "source": "chat"},
            "orchestrator_persona": {"display_name": "老编", "persona": "说话直接，先给结论"}}
    llm = ScriptedLLM([{"text": "好的，记住了。"}])
    orch = make_orch(env, llm, meta_loader=lambda: meta)
    orch.chat_turn("阶段目标是什么？")
    chat_sys = llm.calls[0]["system"]
    assert "当前阶段目标" in chat_sys and "本周打穿靶场 3 台主机" in chat_sys
    assert "拿到 flag" in chat_sys and "阶段: initial-access" in chat_sys
    assert "你的身份" in chat_sys and "先给结论" in chat_sys
    orch.llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch.tick()
    tick_sys = orch.llm.calls[0]["system"]
    assert "当前阶段目标" in tick_sys and "本周打穿靶场 3 台主机" in tick_sys
    assert "你的身份" not in tick_sys  # persona 不进 tick


# ---------- 分阶段工作流（pentest-phased-workflow M1+M2） ----------

def _phase_packs(root) -> str:
    """最小 pentest 阶段剧本 packs（与 test_phases.py 夹具同构，保持本文件独立可跑）。"""
    ph = root / "tracks" / "pentest" / "phases"
    ph.mkdir(parents=True)
    (ph / "recon.yaml").write_text(
        "name: 信息收集\ngoal: 摸清资产面\norder: 1\n"
        "gate:\n  min_assets: 10\n  min_high_value: 1\n  idle_rounds: 2\n"
        "gate_types: [exploit]\nnext: [pentest]\n", encoding="utf-8")
    (ph / "pentest.yaml").write_text(
        "name: 渗透测试\ngoal: 产出 verified 发现\norder: 2\ngate:\n  min_verified: 1\n"
        "next: [recon, report]\n", encoding="utf-8")
    # report 阶段必须存在：pentest 的前向目标按 order 递增过滤，缺 report 时
    # pentest 的 fwd=[]，门进度注入段整段不出现
    (ph / "report.yaml").write_text(
        "name: 报告\ngoal: 汇总报告\norder: 3\nnext: [pentest]\n", encoding="utf-8")
    (root / "tracks" / "pentest" / "task_types.yaml").write_text(
        "recon: passive\nasset-enum: passive\nexploit: low\nreport: passive\n",
        encoding="utf-8")
    ex = root / "experts"
    ex.mkdir()
    for eid in ("_generalist", "external-entry"):
        (ex / f"{eid}.yaml").write_text(
            f"name: {eid}\ndescription: 测试专家\ntracks: [pentest]\n", encoding="utf-8")
    return str(root)


def test_phase_section_injection(env, tmp_path):
    """{phase_section} 槽：阶段名/goal/配比进 tick 系统提示；门进度读 meta 的
    phase_gate_state（B3 单一事实源，注入不重算）；meta 未接线=空段。"""
    packs = _phase_packs(tmp_path / "packs")
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="pentest", packs_root=packs,
                     meta_loader=lambda: {"current_phase": "recon"})
    orch.tick()
    system = llm.calls[0]["system"]
    assert "当前处于「信息收集」（recon）" in system
    assert "阶段目标：摸清资产面" in system
    # B3：meta 无 phase_gate_state（如存量项目首轮）→ 待校准，不再现算
    assert "待编排校准" in system
    # 状态驱动：passed=False → 显示状态里的 unmet（黑板现算结果不影响注入）
    base = {"current_phase": "recon",
            "phase_gate_state": {"phase": "recon", "gate": True, "passed": False,
                                 "unmet": ["assets 3/10", "idle 0/2"]}}
    orch2 = make_orch(env, llm, track="pentest", packs_root=packs,
                      meta_loader=lambda: dict(base))
    section = orch2._phase_section()
    assert "未过（assets 3/10；idle 0/2）" in section
    assert "exploit 类任务会被拒收" in section
    # passed=True → 等待流转
    passed = {**base, "phase_gate_state": {**base["phase_gate_state"],
                                           "passed": True, "unmet": []}}
    orch3 = make_orch(env, llm, track="pentest", packs_root=packs,
                      meta_loader=lambda: passed)
    assert "已过门" in orch3._phase_section()
    # 状态属于别的阶段（阶段不符）→ 待校准
    stale = {"current_phase": "pentest",
             "phase_gate_state": {"phase": "recon", "gate": True, "passed": True,
                                  "unmet": []}}
    orch4 = make_orch(env, llm, track="pentest", packs_root=packs,
                      meta_loader=lambda: stale)
    assert "待编排校准" in orch4._phase_section()
    # meta 未接线（None loader）→ 空段
    orch5 = make_orch(env, ScriptedLLM([{"tool_use": []}]), track="pentest",
                      packs_root=packs)
    assert orch5._phase_section() == ""


# ---------- M6 F1：campaign 注入死路分组 ----------

def test_campaign_section_dead_end_grouping(env):
    """死路条目（tags 含 dead_end）单独成「⚠ 既往死路」组防误当正面经验；
    正向打法照旧带热度标；recall 池不拆（同一次召回分组呈现）。"""
    import tempfile
    from pathlib import Path
    from core.blackboard.campaign import CampaignMemory
    bb, project = env
    camp = CampaignMemory(str(Path(tempfile.mkdtemp()) / "campaign.db"))
    try:
        camp.add(project["id"], "pentest", "web", "exploit",
                 title="后台弱口令字典打法", content="自建字典+延时重试命中后台")
        camp.add(project["id"], "pentest", "web", "recon",
                 title="union 注入此路不通", content="waf 全拦无旁路",
                 tags=["dead_end"])
        orch = Orchestrator(project_id=project["id"], bb=bb, llm=ScriptedLLM([]),
                            config=OrchestratorConfig(), packs_root="packs",
                            track="pentest", campaign=camp)
        section = orch._campaign_section()
        assert "既往战役打法" in section
        assert "后台弱口令字典打法" in section
        assert "⚠ 既往死路" in section
        assert "union 注入此路不通" in section
        # 分组正确：死路条目在组头之后；正向条目带热标、死路行不带
        lines = section.splitlines()
        dead_idx = next(i for i, ln in enumerate(lines) if "⚠ 既往死路" in ln)
        normal_before = any("后台弱口令字典打法" in ln for ln in lines[:dead_idx])
        dead_after = any("union 注入此路不通" in ln for ln in lines[dead_idx:])
        assert normal_before and dead_after
        assert any("热" in ln for ln in lines[:dead_idx] if "后台弱口令" in ln)
        assert not any("·热" in ln for ln in lines[dead_idx:])
    finally:
        camp.close()


# ---------- orch-context-budget：态势全量段裁剪 ----------

def test_orch_thinking_stream_delta_and_final_prune(env):
    """编排器思考流式：stream_capable llm 的 tick 传 on_thinking——增量节流落
    llm.thinking.delta（累计全文+seq+stream_id）；带 thinking 终稿落 llm.thinking
    （带 stream_id）后清剪同流 delta（审计只留终稿一条）。"""
    bb, project = env
    pid = project["id"]
    llm = StreamScriptLLM(
        [{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})],
          "thinking": "编排终稿思考"}],
        deltas=["A" * 130, "B" * 5])  # 第二片 <120 且 <1.0s → 节流不发布
    orch = make_orch(env, llm)
    orch.tick()
    evs = bb.recent_events(pid, limit=200)
    assert llm.streamed is True
    finals = [e for e in evs if e["kind"] == "llm.thinking"]
    assert len(finals) == 1
    fp = finals[0]["payload"]
    assert "编排终稿思考" in fp["thinking"] and fp["source"] == "tick"
    assert fp["stream_id"] and finals[0]["author"] == "orchestrator"
    # 终稿带 stream_id 落库后同流 delta 清剪（审计只留终稿）
    assert not [e for e in evs if e["kind"] == "llm.thinking.delta"]


def test_orch_thinking_stream_delta_payload_shape(env):
    """delta 载荷形态：{stream_id(12hex), thinking=累计全文, seq 递增}；无
    thinking 终稿时不清剪（delta 留作过程证据）。"""
    bb, project = env
    pid = project["id"]
    llm = StreamScriptLLM(
        [{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}],
        deltas=["x" * 130, "y" * 130])  # 两片均越过 120 阈值 → seq 1、2 都发布
    orch = make_orch(env, llm)
    orch.tick()
    deltas = [e for e in bb.recent_events(pid, limit=200)
              if e["kind"] == "llm.thinking.delta"]
    assert [d["payload"]["seq"] for d in deltas] == [1, 2]
    assert deltas[0]["payload"]["thinking"] == "x" * 130
    assert deltas[1]["payload"]["thinking"] == "x" * 130 + "y" * 130  # 累计全文
    assert len({d["payload"]["stream_id"] for d in deltas}) == 1
    assert len(deltas[0]["payload"]["stream_id"]) == 12
    assert all(d["author"] == "orchestrator" for d in deltas)


def test_orch_non_stream_llm_no_thinking_events(env):
    """非流式 llm（未声明 stream_capable）：不传 on_thinking、全程零思考事件
    ——编排轮不因 llm 能力差异变形（进度退化为步进式）。"""
    bb, project = env
    pid = project["id"]
    llm = StreamScriptLLM(
        [{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}], deltas=["z" * 200])
    llm.stream_capable = False
    orch = make_orch(env, llm)
    orch.tick()
    evs = bb.recent_events(pid, limit=200)
    assert llm.streamed is False
    assert not [e for e in evs if e["kind"] in ("llm.thinking", "llm.thinking.delta")]
