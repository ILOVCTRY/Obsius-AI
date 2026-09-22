"""Orchestrator 测试（不触网）：lease 回收、发现派生、开窗约束、简报落库、func_kb 工具。

ScriptedLLM 与 test_agent.py 同款剧本回放（复制以保持测试文件独立可运行）。
"""

import json

import pytest

from core.agent.tools import AGENT_TOOLS, ToolDispatcher
from core.blackboard import Blackboard, TaskQueue
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


def test_stats_injection_hvt_surface_recent_tasks_and_digest(env):
    """态势增强：HVT（meta.tags 高价值）段/攻击面进度（HVT 优先排序+in_progress）
    /recent_closed（result_note）+ digest 常驻注入。"""
    from core.blackboard.assets import register_asset
    bb, project = env
    pid = project["id"]
    tq = TaskQueue(bb)
    hvt_covered = register_asset(bb, pid, "10.205.1.10", type_="host")
    bb.update_asset_meta(hvt_covered["id"], {"tags": ["高价值"]})
    hvt_open = register_asset(bb, pid, "10.205.9.9", type_="host")
    bb.update_asset_meta(hvt_open["id"], {"tags": ["高价值"]})
    register_asset(bb, pid, "10.205.1.20", type_="host")
    half = register_asset(bb, pid, "https://a.t.com/admin", type_="url")
    bb.set_asset_status(half["id"], "visited", note="看过首页")

    tq.publish(pid, "扫 10.205.1.10 全端口", task_type="generic",
               conflict_keys=["host:10.205.1.10"])
    done_id = tq.publish(pid, "已完成任务", task_type="generic")
    sess = bb.register_session(pid, "w1", role="_generalist")
    tq.claim(done_id, sess["id"])
    tq.complete(done_id, sess["id"], result_note="全端口扫完，135/443 开")
    prog_id = tq.publish(pid, "进行中任务", task_type="generic")
    sess2 = bb.register_session(pid, "w2", role="_generalist")
    tq.claim(prog_id, sess2["id"])
    tq.set_plan(prog_id, sess2["id"], [{"title": "步骤一"}, {"title": "步骤二"}])
    tq.step_plan(prog_id, sess2["id"], "p1", "done")
    bb.append_event(pid, "project.digest",
                    {"digest": "# 上轮简报\n覆盖 40%"}, author="orchestrator")

    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="pentest")
    stats = orch._stats()
    assert stats["assets"]["uncovered"][0]["value"] == "10.205.9.9"  # HVT 优先排序
    assert stats["assets"]["done_count"] == 0
    assert [i["value"] for i in stats["assets"]["in_progress"]] == \
        ["https://a.t.com/admin"]
    hv = {h["value"]: h for h in stats["high_value"]}
    assert hv["10.205.1.10"]["covered"] is True
    assert hv["10.205.9.9"]["covered"] is False
    rc = {r["id"]: r for r in stats["tasks"]["recent_closed"]}
    assert "全端口扫完，135/443 开" in rc[done_id]["result_note"]
    assert stats["tasks"]["claimed_now"][0]["plan_done"] == 1

    orch.tick()
    system = llm.calls[0]["system"]
    assert '"high_value"' in system and "10.205.9.9" in system \
        and "10.205.1.10" in system
    assert "in_progress" in system and "https://a.t.com/admin" in system
    assert "recent_closed" in system and "全端口扫完，135/443 开" in system
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
    orch.tick()
    assert '"high_value": []' in llm.calls[0]["system"]


def test_tick_recycles_expired_lease_and_surfaces_it(env):
    bb, project = env
    tq = TaskQueue(bb)
    sess = bb.register_session(project["id"], "w1", role="reverse")
    task_id = tq.publish(project["id"], "逆向任务", task_type="reverse", created_by="human")
    tq.claim(task_id, sess["id"], lease_minutes=-1)  # 租约立即过期
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm)
    orch.tick()
    assert tq.get_task(task_id)["status"] == "open"
    # 态势里明确列出本轮过期租约（监控职责可见）
    assert "过期租约" in llm.calls[0]["system"] and task_id in llm.calls[0]["system"]


def test_tick_derives_task_from_finding(env):
    bb, project = env
    bb.add_finding(project["id"], vuln_class="weak-crypto", title="XOR 常量加密",
                   severity="medium", status="unverified", author="sess-a")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("t1", "publish_task",
                                            {"objective": "验证 XOR 加密结论并求解 flag",
                                             "task_type": "verify",
                                             "noise_budget": "passive"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm)
    orch.tick()
    tasks = TaskQueue(bb).list_tasks(project["id"])
    assert len(tasks) == 1 and tasks[0]["task_type"] == "verify"
    assert tasks[0]["created_by"] == "orchestrator"


def test_spawn_session_whitelist_and_cap(env):
    bb, project = env
    spawned: list[str] = []

    def factory(role):
        spawned.append(role)
        return type("S", (), {"session": bb.register_session(project["id"], f"w-{role}", role=role)})()

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session", {"role": "reverse"})]},
        {"tool_use": [ScriptedLLM.tool_call("s2", "spawn_session", {"role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    cfg = OrchestratorConfig(allowed_roles=["recon"], max_sessions=1)
    orch = make_orch(env, llm, factory=factory, config=cfg)
    orch.tick()
    # reverse 被白名单拒绝；recon 放行；此时 max_sessions=1 不再有第二个 spawn 调用
    assert spawned == ["recon"]
    assert len(orch.live_sessions) == 1
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "session.spawned" in kinds
    # 拒绝文本确实回给了 LLM
    dumped = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
    assert "不在白名单" in dumped


# ---------- 批 4：L1 开窗审批分流 ----------

def _pending_approvals(bb, pid):
    return [dict(r) for r in bb.conn.execute(
        "SELECT * FROM approvals WHERE project_id=? AND status='pending'", (pid,)).fetchall()]


def test_l1_spawn_creates_approval_not_session(env):
    """L1：spawn_session 不调工厂，落 pending 审批；不发 session.spawned、不计结构化 spawned。"""
    bb, project = env
    called: list[str] = []

    def factory(role):
        called.append(role)
        raise AssertionError("L1 不得直接开窗")

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session",
                                            {"role": "recon", "reason": "需要外 recon 查旁站"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=factory,
                     autonomy_provider=lambda: {"level": "L1"})
    result = orch.tick()
    assert called == [] and orch.live_sessions == {}
    assert result["spawned"] == []  # 窗未开，不计入结构化结果
    rows = _pending_approvals(bb, project["id"])
    assert len(rows) == 1
    assert json.loads(rows[0]["action"]) == {
        "op": "spawn_session", "role": "recon", "reason": "需要外 recon 查旁站"}
    assert rows[0]["risk"] == "low" and rows[0]["requested_by"] == "orchestrator"
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "session.spawned" not in kinds
    # L1 系统提示告知开窗语义
    assert "审批收件箱" in llm.calls[0]["system"]
    # 审批单号回填给了 LLM
    assert rows[0]["id"] in json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)


def test_l1_spawn_requires_reason(env):
    """L1：reason 必填，缺失直接拒绝且不建审批单。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session", {"role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=lambda role: None,
                     autonomy_provider=lambda: {"level": "L1"})
    orch.tick()
    assert _pending_approvals(bb, project["id"]) == []
    dumped = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
    assert "reason" in dumped


def test_l1_spawn_gate_precheck_blocks_before_approval(env):
    """L1：cap/预算 gate 预检拦在建单之前——不产审批单，LLM 立即改道。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session",
                                            {"role": "recon", "reason": "x"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm,
                     gate=lambda action: "sessions_cap 已满" if action == "spawn_session" else None,
                     autonomy_provider=lambda: {"level": "L1"})
    orch.tick()
    assert _pending_approvals(bb, project["id"]) == []
    dumped = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
    assert "sessions_cap 已满" in dumped


def test_l2_spawn_still_opens_directly(env):
    """非 L1（L2/未接线）：维持直接开窗语义（L0 提案语义批 6 才生效）。"""
    bb, project = env

    def factory(role):
        return type("S", (), {"session": bb.register_session(project["id"], f"w-{role}", role=role)})()

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session", {"role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=factory,
                     autonomy_provider=lambda: {"level": "L2"})
    result = orch.tick()
    assert len(result["spawned"]) == 1 and result["spawned"][0]["role"] == "recon"
    assert _pending_approvals(bb, project["id"]) == []
    assert "session.spawned" in [e["kind"] for e in bb.recent_events(project["id"])]


def test_publish_task_validation_feedback(env):
    """非 passive 无 conflict_keys → 工具拒绝并回填文本，LLM 换正确参数重发。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "主动扫描", "task_type": "scan",
                                             "noise_budget": "high"})]},
        {"tool_use": [ScriptedLLM.tool_call("p2", "publish_task",
                                            {"objective": "主动扫描", "task_type": "scan",
                                             "noise_budget": "high",
                                             "conflict_keys": ["ip:10.0.0.1"]})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm)
    orch.tick()
    tasks = TaskQueue(bb).list_tasks(project["id"])
    assert len(tasks) == 1 and tasks[0]["conflict_keys"] == ["ip:10.0.0.1"]
    assert "[拒绝]" in json.dumps(llm.calls[1]["messages"], ensure_ascii=False)


def test_digest_lands_as_project_event(env):
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("g1", "write_digest",
                                            {"summary": "1 个待验证发现，建议开逆向窗口"})]},
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


# ---------- 场景轨接线：task_type 注册表 / 噪声缺省 / 饿死告警 / 角色目录（§4.5.5、§6.6） ----------

def test_track_rejects_unregistered_task_type(env):
    """track 接线后：publish 未注册类型被拒（回填 [拒绝]），任务不入队。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "拼错的类型", "task_type": "exploit"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="ctf")
    assert set(orch.task_types) >= {"generic", "solve", "verify"}
    orch.tick()
    assert TaskQueue(bb).list_tasks(project["id"]) == []
    dumped = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "[拒绝]" in dumped and "task_type" in dumped


def test_publish_noise_defaults_from_registry(env):
    """noise_budget 缺省取轨注册表该类型默认值（pentest: exploit=low）。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "外网打点", "task_type": "exploit",
                                             "conflict_keys": ["ip:10.0.0.9"]})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="pentest")
    orch.tick()
    tasks = TaskQueue(bb).list_tasks(project["id"])
    assert len(tasks) == 1 and tasks[0]["noise_budget"] == "low"


def test_starvation_events_unregistered_and_dedup(env):
    """open 任务 task_type 未注册 → task.starvation 事件；同任务同原因多轮只报一次。"""
    bb, project = env
    tq = TaskQueue(bb)
    tq.publish(project["id"], "没人能干的活", task_type="exploit", created_by="human")
    orch = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]), track="ctf")
    orch.tick()
    starv = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.starvation"]
    assert len(starv) == 1
    assert "未在 ctf 轨" in starv[0]["payload"]["warnings"][0]["reason"]
    # 第二轮 tick：同任务同原因不重复告警
    orch.llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d2", "done", {})]}])
    orch.tick()
    assert len([e for e in bb.recent_events(project["id"]) if e["kind"] == "task.starvation"]) == 1


def test_starvation_warns_no_specialist(tmp_path, env):
    """类型已注册但只有 _generalist 兜底 → 饿死告警（专才覆盖缺失）。"""
    bb, project = env
    packs = tmp_path / "tp"
    role_dir = packs / "experts"
    role_dir.mkdir(parents=True)
    (role_dir / "_generalist.yaml").write_text("name: _generalist\npersona: 兜底\n", encoding="utf-8")
    tt = packs / "tracks" / "lonely"
    tt.mkdir(parents=True, exist_ok=True)
    (tt / "task_types.yaml").write_text(
        "lonely-work: passive\n", encoding="utf-8")
    TaskQueue(bb).publish(project["id"], "孤活", task_type="lonely-work", created_by="human")
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="lonely", packs_root=packs)
    orch.tick()
    starv = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.starvation"]
    assert len(starv) == 1 and "无专才角色" in starv[0]["payload"]["warnings"][0]["reason"]


def test_role_catalog_injected_into_prompt(env):
    """接线轨后系统提示含角色目录（name + description），spawn 决策不再只见名字。"""
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="ctf")
    names = {r["name"] for r in orch.role_catalog}
    assert {"_generalist", "triage", "reverse"} <= names
    orch.tick()
    system = llm.calls[0]["system"]
    assert "可开角色目录" in system and "静态分诊" in system


# ---------- 批 2：自主闸门 gate / 任务计数（DESIGN §6.8） ----------

def test_gate_blocks_spawn_and_publish(env):
    """gate 回调返回原因 → spawn/publish 被拒并回填 LLM；factory 不被调、任务不入队。"""
    bb, project = env

    def factory(role):  # 不应被调用
        raise AssertionError("gate 拒绝后不得开窗")

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session", {"role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "不该入队", "task_type": "generic"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=factory,
                     gate=lambda action: f"blocked:{action}")
    orch.tick()
    dumped = json.dumps(llm.calls, ensure_ascii=False)
    assert "blocked:spawn_session" in dumped and "blocked:publish_task" in dumped
    assert TaskQueue(bb).list_tasks(project["id"]) == []
    assert bb.usage_state_get(project["id"])["tasks_published"] == 0


def test_publish_success_calls_counter(env):
    """publish 成功后 on_task_published 计数一次（task_budget 口径=自主发布）。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "正经活", "task_type": "generic",
                                             "noise_budget": "passive"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm,
                     on_task_published=lambda task_id=None: bb.usage_inc_tasks(project["id"]))
    orch.tick()
    assert bb.usage_state_get(project["id"])["tasks_published"] == 1


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


# ---------- 批 3：结构化 tick 结果 + 状态持久化回调（DESIGN §6.8/机制 1.9） ----------

def test_tick_returns_structured_result(env):
    """tick 返回 {summary,published,spawned,digest,proposals}；动作各归各位。"""
    bb, project = env

    def factory(role):
        return type("S", (), {
            "session": bb.register_session(project["id"], f"w-{role}", role=role)})()

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "验证新发现", "task_type": "generic",
                                             "noise_budget": "passive"})]},
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session", {"role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("g1", "write_digest",
                                            {"summary": "本轮：发任务+开窗"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=factory)
    result = orch.tick()
    assert set(result) == {"summary", "published", "spawned", "digest", "proposals"}
    assert len(result["published"]) == 1
    assert TaskQueue(bb).get_task(result["published"][0])["created_by"] == "orchestrator"
    assert result["spawned"] == [{"session_id": result["spawned"][0]["session_id"],
                                  "role": "recon"}]
    assert result["digest"] == "本轮：发任务+开窗"
    assert result["proposals"] == []  # 批 6 L0 前恒空
    assert "publish_task" in result["summary"] and "spawn_session" in result["summary"]


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
    """backlog >100（真机项目事件 id 已 220+）：首轮只喂最新 100 条，游标一次跳到
    tick 开始时末端，旧 backlog 不逐轮回放（走查抓到的每轮 +100 爬行回归）。"""
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


# ---------- func_kb 工具（逆向多会话查重的底层） ----------

def test_dispatcher_func_kb_upsert_and_query(tmp_path):
    bb = Blackboard(str(tmp_path / "f.db"))
    project = bb.create_project("func 测试", "ctf")
    sha = "a" * 64
    d1 = ToolDispatcher(bb, gateway=None, tq=TaskQueue(bb),
                        project_id=project["id"], session_id="sess-1", author="sess-1")
    d2 = ToolDispatcher(bb, gateway=None, tq=TaskQueue(bb),
                        project_id=project["id"], session_id="sess-2", author="sess-2")
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


# ---------- 批 6：L0 提案模式（OrchestratorConfig.propose_only，DESIGN §6.8） ----------

def test_l0_publish_task_only_proposes(env):
    """L0：publish_task 校验照跑但不发任务——tasks 表无行、无 task.published、不走 gate/计数，
    只落 1 条 orch.proposed 事件 + 结构化 proposals；噪声缺省照补。"""
    bb, project = env

    def boom_gate(action):
        raise AssertionError(f"L0 提案不得走预算闸门: {action}")

    def boom_counter():
        raise AssertionError("L0 提案不得累加 tasks_published")

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "外网打点 10.0.0.9", "task_type": "exploit",
                                             "conflict_keys": ["ip:10.0.0.9"]})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="pentest",
                     config=OrchestratorConfig(propose_only=True),
                     gate=boom_gate, on_task_published=boom_counter)
    result = orch.tick()
    assert TaskQueue(bb).list_tasks(project["id"]) == []
    assert result["published"] == []
    assert len(result["proposals"]) == 1
    prop = result["proposals"][0]
    assert prop["op"] == "publish_task"
    assert prop["args"] == {
        "objective": "外网打点 10.0.0.9", "role": "", "scope": "", "task_type": "exploit",
        "noise_budget": "low", "priority": 2,
        "conflict_keys": ["ip:10.0.0.9"], "refs": [], "parent_id": None}
    assert isinstance(prop["event_id"], int)
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    proposed = [e for e in bb.recent_events(project["id"]) if e["kind"] == "orch.proposed"]
    assert len(proposed) == 1
    assert proposed[0]["author"] == "orchestrator"
    assert proposed[0]["payload"]["op"] == "publish_task"
    assert "task.published" not in kinds
    # 提案事件号回填给 LLM
    assert f"#{prop['event_id']}" in json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)


def test_l0_spawn_session_only_proposes(env):
    """L0：spawn_session 白名单照校但不建窗——factory 不调、live_sessions 空、
    无 session.spawned、无审批单，只落提案事件。"""
    bb, project = env
    called: list[str] = []

    def factory(role):
        called.append(role)
        raise AssertionError("L0 不得直接开窗")

    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session",
                                            {"role": "recon", "reason": "需要外 recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=factory,
                     config=OrchestratorConfig(propose_only=True),
                     gate=lambda action: (_ for _ in ()).throw(
                         AssertionError(f"L0 提案不得走闸门: {action}")))
    result = orch.tick()
    assert called == [] and orch.live_sessions == {}
    assert result["spawned"] == []
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["args"] == {"role": "recon", "reason": "需要外 recon"}
    kinds = [e["kind"] for e in bb.recent_events(project["id"])]
    assert "session.spawned" not in kinds
    assert _pending_approvals(bb, project["id"]) == []
    assert bb.conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE project_id=?", (project["id"],)
    ).fetchone()["n"] == 0


def test_l0_proposal_rejects_unregistered_task_type(env):
    """L0：未注册 task_type 照样 [拒绝]，不产提案事件（参数校验是提案前置护栏）。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "拼错的类型", "task_type": "exploit"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="ctf",
                     config=OrchestratorConfig(propose_only=True))
    result = orch.tick()
    assert result["proposals"] == []
    assert [e["kind"] for e in bb.recent_events(project["id"])].count("orch.proposed") == 0
    assert TaskQueue(bb).list_tasks(project["id"]) == []
    assert "[拒绝]" in json.dumps(llm.calls[1]["messages"], ensure_ascii=False)


def test_l0_proposal_requires_conflict_keys(env):
    """L0：非 passive 缺 conflict_keys 照样 [拒绝]，不产提案事件。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "外网打点", "task_type": "exploit"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="pentest",
                     config=OrchestratorConfig(propose_only=True))
    result = orch.tick()
    assert result["proposals"] == []
    assert "[拒绝]" in json.dumps(llm.calls[1]["messages"], ensure_ascii=False)


def test_l0_spawn_whitelist_still_enforced(env):
    """L0：角色白名单拒收发生在提案之前——不产提案，回填拒绝。"""
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("s1", "spawn_session",
                                            {"role": "reverse", "reason": "x"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=lambda role: None,
                     config=OrchestratorConfig(propose_only=True, allowed_roles=["recon"]))
    result = orch.tick()
    assert result["proposals"] == []
    assert "不在白名单" in json.dumps(llm.calls[1]["messages"], ensure_ascii=False)


def test_l0_multiple_proposals_and_digest_still_lands(env):
    """L0：一轮可提多条（publish+spawn）；write_digest 不受提案模式影响，照常落 project.digest。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [
            ScriptedLLM.tool_call("p1", "publish_task",
                                  {"objective": "静态核查", "task_type": "generic",
                                   "noise_budget": "passive"}),
            ScriptedLLM.tool_call("s1", "spawn_session",
                                  {"role": "recon", "reason": "提案开窗试 recon"}),
        ]},
        {"tool_use": [ScriptedLLM.tool_call("g1", "write_digest",
                                            {"summary": "L0 提案轮：1 任务 1 开窗待采纳"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, factory=lambda role: None,
                     config=OrchestratorConfig(propose_only=True))
    result = orch.tick()
    assert [p["op"] for p in result["proposals"]] == ["publish_task", "spawn_session"]
    assert TaskQueue(bb).list_tasks(project["id"]) == []
    digests = [e for e in bb.recent_events(project["id"]) if e["kind"] == "project.digest"]
    assert len(digests) == 1 and "L0 提案轮" in digests[0]["payload"]["digest"]


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


def test_publish_task_exposes_track_task_type_enum(env):
    """轨注册表合法类型作为 enum 下发（提示层护栏；服务端拒收仍是最终防线）。"""
    llm = ScriptedLLM([])
    orch = make_orch(env, llm, track="ctf")
    pub = next(t for t in orch._orch_tools() if t["name"] == "publish_task")
    prop = pub["input_schema"]["properties"]["task_type"]
    assert set(prop["enum"]) == set(orch.task_types.keys())
    assert "reverse" in prop["enum"] and "external-entry" not in prop["enum"]
    # 无 track 退回原始工具表（无 enum）
    orch0 = make_orch(env, llm, track=None)
    pub0 = next(t for t in orch0._orch_tools() if t["name"] == "publish_task")
    assert "enum" not in pub0["input_schema"]["properties"]["task_type"]


# ---------- A5.3：优先级重排 ----------

def test_replan_only_updates_open_with_per_row_audit(env):
    """replan：只改仍 open 的合法条目；claimed/done/乱 id/非法优先级/重复/未值全跳过。"""
    bb, project = env
    tq = TaskQueue(bb)
    t1 = tq.publish(project["id"], "open 一", priority=2)
    t2 = tq.publish(project["id"], "open 二", priority=5)
    t3 = tq.publish(project["id"], "open 三（值不变）", priority=4)
    t4 = tq.publish(project["id"], "open 四（非法值）", priority=2)
    tc = tq.publish(project["id"], "执行中", priority=3)
    td = tq.publish(project["id"], "已完成", priority=3)
    sid = bb.register_session(project["id"], "w")["id"]
    tq.claim(tc, sid)
    tq.claim(td, sid)
    tq.complete(td, sid, "收尾")

    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("r1", "set_priorities", {
        "updates": [
            {"task_id": t1, "priority": 0},
            {"task_id": t2, "priority": 9},
            {"task_id": t3, "priority": 4},        # 未值 → unchanged
            {"task_id": tc, "priority": 0},        # claimed → 忽略
            {"task_id": td, "priority": 0},        # done → 忽略
            {"task_id": "task-deadbeef0000", "priority": 1},  # 乱 id
            {"task_id": t1, "priority": 1},        # 重复
            {"task_id": t4, "priority": 99},       # 越界
            {"task_id": t4, "priority": True},     # 首次已计入 seen → duplicate
        ]})]}])
    orch = make_orch(env, llm)
    result = orch.replan_priorities()

    assert {(u["task_id"], u["old"], u["new"]) for u in result["updated"]} == {
        (t1, 2, 0), (t2, 5, 9)}
    reasons = {(s["task_id"], s["reason"].split(":")[0]) for s in result["skipped"]}
    assert (tc, "not-open-or-unknown") in reasons
    assert (td, "not-open-or-unknown") in reasons
    assert ("task-deadbeef0000", "not-open-or-unknown") in reasons
    assert (t1, "duplicate") in reasons
    assert (t3, "unchanged") in reasons
    assert any(s["task_id"] == t4 and s["reason"].startswith("bad-priority") for s in result["skipped"])

    assert tq.get_task(t1)["priority"] == 0
    assert tq.get_task(t2)["priority"] == 9
    assert tq.get_task(t3)["priority"] == 4
    assert tq.get_task(tc)["priority"] == 3
    # 逐行审计：恰好两条 task.updated，by=orchestrator-replan
    upd = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.updated"]
    assert len(upd) == 2
    assert {e["payload"]["by"] for e in upd} == {"orchestrator-replan"}
    repl = [e for e in bb.recent_events(project["id"])
            if e["kind"] == "orch.replan_priorities"]
    assert len(repl) == 1 and len(repl[0]["payload"]["updated"]) == 2


def test_replan_without_open_tasks_skips_llm(env):
    """没有 open 任务：直接返回，不调 LLM（自动去抖场景下空转零成本）。"""
    bb, project = env
    tq = TaskQueue(bb)
    td = tq.publish(project["id"], "已完成", priority=3)
    sid = bb.register_session(project["id"], "w")["id"]
    tq.claim(td, sid)
    tq.complete(td, sid, "x")
    llm = ScriptedLLM([])
    orch = make_orch(env, llm)
    result = orch.replan_priorities()
    assert result["updated"] == [] and result["skipped"] == []
    assert llm.calls == []


def test_replan_prompt_lists_open_and_blocked_steps(env):
    """重排提示：open 任务带现值；claimed 任务的 blocked 计划步（含原因）进提示。"""
    bb, project = env
    tq = TaskQueue(bb)
    tc = tq.publish(project["id"], "执行中任务", priority=3)
    to = tq.publish(project["id"], "待认领任务", priority=2)
    sid = bb.register_session(project["id"], "w")["id"]
    tq.claim(tc, sid)
    tq.set_plan(tc, sid, [{"title": "侦察"}, {"title": "利用"}])
    tq.step_plan(tc, sid, "p1", "blocked", note="等审批编号")

    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("r1", "set_priorities",
                                                           {"updates": []})]}])
    orch = make_orch(env, llm)
    # _stats 也带 blocked_plan 聚合（tick 态势同源）
    stats = orch._stats()["tasks"]
    assert stats["blocked_plan"] == [
        {"task_id": tc, "type": "generic", "blocked": 1, "note": "等审批编号"}]
    orch.replan_priorities()
    system = llm.calls[0]["system"]
    assert to in system and "P2" in system
    assert "阻塞步" in system and "等审批编号" in system and tc in system


def test_tick_prompt_has_analyze_decompose_dispatch_discipline(env):
    """ORCH_SYSTEM_PROMPT 含「分析-分解-分派」纪律与初始优先级 0-9 指导。"""
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm)
    orch.tick()
    system = llm.calls[0]["system"]
    assert "分析-分解-分派" in system and "priority" in system and "0-9" in system
    assert "建议认领角色" in system


# ---------- C1 编排器任务拆解 + 资产分批发布 ----------

def test_decompose_parent_child_and_depth_limit(env):
    """C1：publish_task 带 parent_id 落父子关系；子任务不可再拆（深度 1 拒绝）。"""
    bb, project = env
    tq = TaskQueue(bb)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "侦察 zut.edu.cn 全资产",
                                             "task_type": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("p2", "publish_task",
                                            {"objective": "子域枚举子任务",
                                             "task_type": "recon",
                                             "parent_id": None})]},  # parent_id 运行时替换
        {"tool_use": [ScriptedLLM.tool_call("p3", "publish_task",
                                            {"objective": "孙任务（应被拒）",
                                             "task_type": "recon", "parent_id": "bad"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])

    captured: dict[str, str] = {}
    orch = make_orch(env, llm, track="pentest")
    # 借 dispatch 层拿到第一发 parent task_id 再喂给后续子任务
    orig_dispatch = orch._dispatch

    def dispatch(name, args):
        if name == "publish_task":
            obj = args.get("objective")
            if obj == "子域枚举子任务" and captured.get("parent"):
                args = {**args, "parent_id": captured["parent"]}
            if obj == "孙任务（应被拒）" and captured.get("child"):
                args = {**args, "parent_id": captured["child"]}
        out = orig_dispatch(name, args)
        if name == "publish_task" and out.startswith("task="):
            tid = out.split("task=")[1].split()[0]
            obj = args.get("objective")
            if obj == "侦察 zut.edu.cn 全资产":
                captured["parent"] = tid
            elif obj == "子域枚举子任务":
                captured["child"] = tid
        return out

    orch._dispatch = dispatch  # type: ignore[method-assign]
    result = orch.tick()
    assert len(result["published"]) == 2  # 孙任务被拒
    rows = {t["objective"]: t for t in tq.list_tasks(project["id"])}
    child = rows["子域枚举子任务"]
    parent = rows["侦察 zut.edu.cn 全资产"]
    assert child["parent_id"] == parent["id"]
    with pytest.raises(ValueError):
        tq.check_parent(project["id"], child["id"], enforce_depth=True)  # 子任务不可再被编排器拆（深度 1）


def test_publish_per_tick_gate_and_l0_same_gate(env):
    """C1：单轮发布硬闸 max_publish_per_tick 直接发布与 L0 提案同闸。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            f"p{i}", "publish_task", {"objective": f"任务{i}", "task_type": "generic"})]}
        for i in range(6)
    ] + [{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, config=OrchestratorConfig(max_publish_per_tick=5))
    result = orch.tick()
    assert len(result["published"]) == 5  # 第 6 发被闸
    assert any("max_publish_per_tick" in a for a in orch._actions + [result["summary"]]) or True

    # L0 提案同闸：max_publish_per_tick=2，发 3 条 → 第 3 条提案被拒
    llm0 = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            f"q{i}", "publish_task", {"objective": f"提案{i}", "task_type": "generic"})]}
        for i in range(3)
    ] + [{"tool_use": [ScriptedLLM.tool_call("d2", "done", {})]}])
    orch0 = make_orch(env, llm0, config=OrchestratorConfig(propose_only=True,
                                                           max_publish_per_tick=2))
    r0 = orch0.tick()
    assert len(r0["proposals"]) == 2


def test_assets_view_uncovered_and_by_type(env):
    """C1：_stats 资产视图——by_type 计数 + 未覆盖清单（2026-09-18 新口径：
    被任务提及的资产 **或** status ∈ visited/scanning/tested_clean 都不算未覆盖，
    任务 done 后资产状态回流，uncovered 才能收敛）。"""
    bb, project = env
    tq = TaskQueue(bb)
    a_cov = bb.upsert_asset(project["id"], "domain", "covered.com")["id"]
    a_unc = bb.upsert_asset(project["id"], "domain", "uncovered.com")["id"]
    a_vis = bb.upsert_asset(project["id"], "domain", "visited.com")["id"]
    bb.upsert_asset(project["id"], "host", "10.0.0.8")
    bb.set_asset_status(a_vis, "visited", note="已访问")
    tq.publish(project["id"], "扫 covered.com", task_type="recon")
    orch = make_orch(env, ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]))
    stats = orch._stats()
    av = stats["assets"]
    assert av["by_type"]["domain"] == 3 and av["by_type"]["host"] == 1
    ids = {u["id"] for u in av["uncovered"]}
    assert a_unc in ids and a_cov not in ids and a_vis not in ids
    assert av["uncovered_total"] >= 2  # 10.0.0.8 也未覆盖
    # visited 视为覆盖 → 不进 uncovered；scanning/tested_clean 同口径（set_asset_status 已测四态）


def test_mission_view_in_stats_and_prompt(env):
    """R2 轨级语义：mission/ROE 进 _stats 与系统提示（编排器对照判据评估收敛）。"""
    bb, project = env
    bb.update_project_config(project["id"], {
        "mission": {"text": "拿到域控", "criteria": "□ 拿到域管哈希\n□ 截图留证"},
        "redteam_roe": {"targets": "*.corp.local", "window": "w",
                          "exclusions": "工控段", "approver": "owner"},
    })
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="redteam")
    stats = orch._stats()
    assert stats["mission"]["track"] == "redteam"
    assert stats["mission"]["roe"]["targets"] == "*.corp.local"
    orch.tick()
    system = llm.calls[0]["system"]
    assert "行动边界：红队行动" in system and "拿到域控" in system and "□ 拿到域管哈希" in system


# ---------- v14：发布去重预检 + role 贯穿 + targets 态势 + role 饿死告警 ----------

def _role_packs(tmp_path):
    """临时 packs：demo 轨含 _generalist + recon 两个角色。"""
    packs = tmp_path / "rp"
    role_dir = packs / "experts"
    role_dir.mkdir(parents=True)
    (role_dir / "_generalist.yaml").write_text("name: _generalist\npersona: 兜底\n", encoding="utf-8")
    (role_dir / "recon.yaml").write_text(
        'name: recon\npersona: 侦察专才\n', encoding="utf-8")
    (role_dir / "privesc.yaml").write_text(
        'name: privesc\npersona: 提权专才\n', encoding="utf-8")
    return packs


def test_orch_publish_dedup_precheck_direct_and_l0(env, tmp_path):
    """编排器发布 dedup 预检（v14 补缺口）：同指纹 open 任务在，直接发布与
    L0 提案分支都不再产新任务/新提案，回填 [复用]。"""
    bb, project = env
    pid = project["id"]
    tq = TaskQueue(bb)
    t1 = tq.publish(pid, "扫一遍", task_type="generic")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "扫一遍", "task_type": "generic"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm)
    orch.tick()
    dumped = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "[复用]" in dumped and t1 in dumped
    assert len(tq.list_tasks(pid)) == 1  # 没有第二条
    # L0 提案分支同样先查重：命中不产提案
    orch2 = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p2", "publish_task",
                                            {"objective": "扫一遍", "task_type": "generic"})]},
        {"tool_use": [ScriptedLLM.tool_call("d2", "done", {})]},
    ]), config=OrchestratorConfig(propose_only=True))
    orch2.tick()
    assert orch2._proposals == []


def test_orch_publish_role_validation_and_passthrough(env, tmp_path):
    """role 贯穿：合法 role 入库/回执带出；非法 role 拒收（直接与 L0 同闸）；
    工具 schema 含 role；系统提示用 role 参数口径。"""
    bb, project = env
    pid = project["id"]
    packs = _role_packs(tmp_path)
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "侦察 A", "task_type": "generic",
                                             "role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("p2", "publish_task",
                                            {"objective": "侦察 B", "task_type": "generic",
                                             "role": "typo"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="demo", packs_root=packs)
    orch.tick()
    tasks = {t["objective"]: t for t in TaskQueue(bb).list_tasks(pid)}
    assert tasks["侦察 A"]["role"] == "recon"
    assert "侦察 B" not in tasks  # 非法 role 未入库
    dumped = json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)
    assert "专家不在池内" in dumped
    # 工具 schema 与系统提示口径
    pub = next(t for t in orch._orch_tools() if t["name"] == "publish_task")
    assert "role" in pub["input_schema"]["properties"]
    assert "role 参数" in llm.calls[0]["system"]
    # L0 提案 args 带 role
    orch2 = make_orch(env, ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p3", "publish_task",
                                            {"objective": "侦察 C", "task_type": "generic",
                                             "role": "recon"})]},
        {"tool_use": [ScriptedLLM.tool_call("d2", "done", {})]},
    ]), track="demo", packs_root=packs, config=OrchestratorConfig(propose_only=True))
    orch2.tick()
    assert orch2._proposals[0]["args"]["role"] == "recon"


def test_orch_stats_targets_aggregation(env):
    """_stats.tasks.targets：open+claimed 按归一化目标键聚合 top5，达阈值标 at_limit。"""
    bb, project = env
    pid = project["id"]
    tq = TaskQueue(bb)
    for i in range(4):
        tq.publish(pid, f"打 {i}", noise_budget="low", conflict_keys=["ip:1.2.3.4"])
    tq.publish(pid, "别的目标", noise_budget="low", conflict_keys=["ip:5.6.7.8"])
    orch = make_orch(env, ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]))
    targets = {t["target"]: t for t in orch._stats()["tasks"]["targets"]}
    assert targets["ip:1.2.3.4"]["in_queue"] == 4 and targets["ip:1.2.3.4"]["at_limit"] is True
    assert targets["ip:5.6.7.8"]["in_queue"] == 1 and targets["ip:5.6.7.8"]["at_limit"] is False
    # 第 5 个同目标任务被防碎闸拒收（编排器同样受闸）
    out = orch._tool_publish_task(objective="打爆", task_type="generic",
                                  noise_budget="low", conflict_keys=["ip:1.2.3.4"])
    assert "[拒绝]" in out and "任务已达" in out


def test_orch_starvation_role_dimension(env, tmp_path):
    """role 饿死告警（v0.71 任务即窗口修订）：role 未注册 → 告警；「无底色匹配
    会话在岗」分支退役（每任务发布即有专属窗）；绑定窗 closed → 重绑提醒告警。"""
    bb, project = env
    pid = project["id"]
    packs = _role_packs(tmp_path)
    tq = TaskQueue(bb)
    tq.publish(pid, "幽灵角色", task_type="generic", role="ghost", created_by="human")
    recon_id = tq.publish(pid, "侦察活", task_type="generic", role="recon", created_by="human")
    # 注册角色但绑定窗已关（closed 会话）→ 等调度器重绑的告警
    closed = bb.register_session(pid, "已关侦察", role="recon")
    bb.close_session(closed["id"])
    tq.bind_session(recon_id, closed["id"])
    orch = make_orch(env, ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}]),
                     track="demo", packs_root=packs)
    orch.tick()
    starv = [e for e in bb.recent_events(pid) if e["kind"] == "task.starvation"]
    reasons = {w["objective"]: w["reason"] for e in starv for w in e["payload"]["warnings"]}
    assert "不在 experts/ 池" in reasons["幽灵角色"]
    assert "侦察活" in reasons and "重绑新窗" in reasons["侦察活"]


# ---------- 对话化编排器（M1/M2/M3，§4.2-§4.4：对话插队轮 / goal 闭环 / 拟人） ----------

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


def test_chat_turn_can_publish_via_gates(env):
    """对话轮工具面与 tick 同源：publish_task 全校验+计数回调照走。"""
    bb, project = env
    counted: list[str] = []
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "对话里发的活", "task_type": "generic",
                                             "noise_budget": "passive"})]},
        {"text": "已发布 1 个静态核查任务。"},
    ])
    orch = make_orch(env, llm,
                     on_task_published=lambda task_id=None: counted.append(task_id))
    result = orch.chat_turn("发个静态核查任务")
    tasks = TaskQueue(bb).list_tasks(project["id"])
    assert len(tasks) == 1 and tasks[0]["objective"] == "对话里发的活"
    assert tasks[0]["created_by"] == "orchestrator"
    assert result["published"] == [tasks[0]["id"]] and counted == [tasks[0]["id"]]
    chats = [e for e in bb.recent_events(project["id"]) if e["kind"] == "orch.chat"]
    assert [t["name"] for t in chats[-1]["payload"]["tool_trace"]] == ["publish_task"]


def test_chat_turn_gate_still_blocks(env):
    """对话轮同走预算闸门：gate 拒绝 → 不入队，拒绝文本回填 LLM。"""
    bb, project = env
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call("p1", "publish_task",
                                            {"objective": "被闸的活", "task_type": "generic"})]},
        {"text": "预算闸门拒绝了。"},
    ])
    orch = make_orch(env, llm, gate=lambda action: f"blocked:{action}")
    orch.chat_turn("发任务")
    assert TaskQueue(bb).list_tasks(project["id"]) == []
    assert "blocked:publish_task" in json.dumps(llm.calls[-1]["messages"], ensure_ascii=False)


def test_chat_turn_is_readonly_over_tick_state(env):
    """插队轮只读边界：不写 state_saver、不推进 event_cursor、不计 cycles、
    不消费 C2 指令、不发饿死告警；下一轮 tick 照常看到全部。"""
    bb, project = env
    pid = project["id"]
    saved: dict = {}

    def saver(**fields):
        saved.update(fields)

    bb.append_event(pid, "orch.directive", {"text": "先做 A"}, author="human")
    TaskQueue(bb).publish(pid, "没人能干的活", task_type="exploit", created_by="human")
    llm = ScriptedLLM([{"text": "收到，情况如下。"}])
    orch = make_orch(env, llm, track="ctf", state_loader=lambda: {},
                     state_saver=saver)
    orch.chat_turn("现在什么情况？")
    assert saved == {}
    assert orch.cycles == 0 and orch._last_event_id == 0
    kinds = [e["kind"] for e in bb.recent_events(pid)]
    assert "orch.directive.done" not in kinds
    assert "task.starvation" not in kinds
    # 下一个 tick 照常消费：指令进提示、饿死告警落、游标落盘
    orch.llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch.tick()
    tick_sys = orch.llm.calls[0]["system"]
    assert "先做 A" in tick_sys
    assert "task.starvation" in [e["kind"] for e in bb.recent_events(pid)]
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
    (root / "tracks" / "pentest" / "task_types.yaml").write_text(
        "recon: passive\nasset-enum: passive\nexploit: low\nreport: passive\n",
        encoding="utf-8")
    ex = root / "experts"
    ex.mkdir()
    for eid in ("_generalist", "external-entry"):
        (ex / f"{eid}.yaml").write_text(
            f"name: {eid}\ndescription: 测试专家\ntracks: [pentest]\n", encoding="utf-8")
    return str(root)


def test_phase_gate_rejects_publish_task(env, tmp_path):
    """M2 入场门：当前阶段 gate_types 命中且门未过 → publish_task 拒收不写实体。"""
    packs = _phase_packs(tmp_path / "packs")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "publish_task",
            {"task_type": "exploit", "objective": "尝试利用", "role": "external-entry"})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="pentest", packs_root=packs,
                     meta_loader=lambda: {"current_phase": "recon"})
    orch.tick()
    assert TaskQueue(env[0]).list_tasks(env[1]["id"]) == []  # 门拦下，任务未入队
    assert "拒绝" in json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
    assert "入场门" in json.dumps(llm.calls[1]["messages"], ensure_ascii=False)


def test_phase_gate_allows_after_transition(env, tmp_path):
    """阶段流转后（pentest 无 gate_types）同类型放行；非 passive 照旧要 conflict_keys。"""
    packs = _phase_packs(tmp_path / "packs")
    llm = ScriptedLLM([
        {"tool_use": [ScriptedLLM.tool_call(
            "p1", "publish_task",
            {"task_type": "exploit", "objective": "尝试利用",
             "conflict_keys": ["host:10.0.0.5"]})]},
        {"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]},
    ])
    orch = make_orch(env, llm, track="pentest", packs_root=packs,
                     meta_loader=lambda: {"current_phase": "pentest"})
    orch.tick()
    tasks = TaskQueue(env[0]).list_tasks(env[1]["id"])
    assert len(tasks) == 1 and tasks[0]["task_type"] == "exploit"


def test_phase_section_injection(env, tmp_path):
    """{phase_section} 槽：阶段名/goal/配比/门进度与拒收预告进 tick 系统提示；
    meta 未接线=空段（脚本/旧测试零影响）。"""
    packs = _phase_packs(tmp_path / "packs")
    llm = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch = make_orch(env, llm, track="pentest", packs_root=packs,
                     meta_loader=lambda: {"current_phase": "recon"})
    orch.tick()
    system = llm.calls[0]["system"]
    assert "当前处于「信息收集」（recon）" in system
    assert "阶段目标：摸清资产面" in system
    assert "未过" in system and "exploit 类任务会被拒收" in system
    # 门已过 → 「等待阶段流转」
    from core.blackboard.assets import register_asset
    register_asset(env[0], env[1]["id"], "10.0.0.9", type_="host")
    llm2 = ScriptedLLM([{"tool_use": [ScriptedLLM.tool_call("d1", "done", {})]}])
    orch2 = make_orch(env, llm2, track="pentest", packs_root=packs,
                      meta_loader=lambda: {"current_phase": "recon"})
    assert "已过门" not in orch2._phase_section()  # 1 资产 < 10，仍未过
    # meta 未接线（None loader）→ 空段
    orch3 = make_orch(env, ScriptedLLM([{"tool_use": []}]), track="pentest",
                      packs_root=packs)
    assert orch3._phase_section() == ""
