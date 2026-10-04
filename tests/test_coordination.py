from core.blackboard.store import Blackboard
from core.coordination import CoordinationStore


def _project(bb, pid="p1"):
    with bb._tx():
        bb.conn.execute(
            "INSERT INTO projects(id,name,domain,track,config,created_at)"
            " VALUES(?,?,?,?,?,datetime('now'))",
            (pid, "协调测试", pid, "research", "{}"),
        )


def test_coordination_plan_task_dag(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "样本初步分析", "识别关键模块并验证行为")
    first = store.add_task("p1", plan["id"], "识别文件格式", role="static")
    second = store.add_task("p1", plan["id"], "定位关键函数", role="decompiler",
                            depends_on=[first["id"]])
    store.update_task("p1", first["id"], status="completed",
                      evidence=[{"kind": "artifact", "ref": "overview.json"}])
    overview = store.overview("p1")
    assert overview["active_plan_id"] is None
    assert overview["summary"]["completed"] == 1
    assert overview["plans"][0]["tasks"][1]["depends_on"] == [first["id"]]
    assert overview["plans"][0]["tasks"][1]["status"] == "ready"
    assert overview["plans"][0]["tasks"][0]["evidence"][0]["ref"] == "overview.json"


def test_coordination_preflight_and_ready_event(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "预检计划")
    first = store.add_task("p1", plan["id"], "根任务")
    store.set_plan_status("p1", plan["id"], "active")
    pf = store.preflight("p1", plan["id"])
    assert pf["dependencies"]["ready_count"] == 1
    assert not pf["blockers"]
    ready = [e for e in bb.recent_events("p1") if e["kind"] == "plan.node_ready"]
    assert ready and ready[-1]["payload"]["node_ids"] == [first["id"]]
    store.refresh_readiness("p1")
    assert len([e for e in bb.recent_events("p1") if e["kind"] == "plan.node_ready"]) == len(ready)


def test_coordination_bound_task_sync_and_auto_complete(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "绑定计划")
    first = store.add_task("p1", plan["id"], "执行入口")
    second = store.add_task("p1", plan["id"], "复核入口", depends_on=[first["id"]])
    store.set_plan_status("p1", plan["id"], "active")
    with bb._tx():
        bb.conn.execute("INSERT INTO tasks(id,project_id,objective,status,created_at,updated_at) VALUES(?,?,?,?,datetime('now'),datetime('now'))",
                        ("task-real", "p1", "执行入口", "open"))
    store.bind_task("p1", first["id"], "task-real")
    assert store.get_task("p1", first["id"])["task_id"] == "task-real"
    store.refresh_readiness("p1")
    assert store.get_task("p1", first["id"])["status"] == "running"
    with bb._tx():
        bb.conn.execute("UPDATE tasks SET status='done' WHERE id='task-real'")
    store.refresh_readiness("p1")
    assert store.get_task("p1", first["id"])["status"] == "completed"
    assert store.get_task("p1", second["id"])["status"] == "ready"


def test_coordination_set_dependencies_rejects_cycle(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "循环检查")
    first = store.add_task("p1", plan["id"], "一")
    second = store.add_task("p1", plan["id"], "二", depends_on=[first["id"]])
    try:
        store.set_dependencies("p1", first["id"], [second["id"]])
    except ValueError as exc:
        assert "循环" in str(exc)
    else:
        raise AssertionError("cycle should be rejected")


def test_coordination_rejects_unknown_dependency(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "计划")
    try:
        store.add_task("p1", plan["id"], "任务", depends_on=["ctask-missing"])
    except ValueError as exc:
        assert "依赖任务不存在" in str(exc)
    else:
        raise AssertionError("unknown dependency should be rejected")


def test_coordination_structured_objects_artifacts_and_conflicts(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "证据汇总")
    left = store.add_object(
        "p1", kind="function", name="decrypt_config", object_ref="0x401000",
        source="static", confidence=0.82, plan_id=plan["id"],
        data={"address": "0x401000"}, artifact_refs=["artifact:overview.json"])
    right = store.add_object(
        "p1", kind="function", name="decrypt_config", object_ref="0x402000",
        source="dynamic", confidence=0.61, plan_id=plan["id"])
    conflict = store.add_conflict(
        "p1", left_object_id=left["id"], right_object_id=right["id"],
        field="address", summary="静态与动态分析定位到不同地址")
    assert left["kind"] == "function"
    assert left["artifact_refs"][0]["artifact_ref"] == "artifact:overview.json"
    assert conflict["status"] == "open"
    resolved = store.update_conflict("p1", conflict["id"], status="resolved", resolution="动态样本重新验证")
    assert resolved["status"] == "resolved" and "重新验证" in resolved["resolution"]
    overview = store.overview("p1")
    assert overview["summary"]["objects"] == 2
    assert overview["summary"]["conflicts"] == 0


def test_coordination_verification_needs_evidence_and_deduplicates_followup(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "验证计划")
    task = store.add_task("p1", plan["id"], "提取配置")

    first = store.verify_task("p1", task["id"])
    second = store.verify_task("p1", task["id"])

    assert first["status"] == "needs_evidence"
    assert len(first["followup_task_ids"]) == 1
    assert second["status"] == "needs_evidence"
    assert second["followup_task_ids"] == first["followup_task_ids"]
    tasks = store.get_plan("p1", plan["id"])["tasks"]
    assert len(tasks) == 2
    assert next(t for t in tasks if t["id"] == first["followup_task_ids"][0])["title"] == "补充证据：提取配置"


def test_coordination_verification_conflict_creates_review_task(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "冲突复核")
    task = store.add_task("p1", plan["id"], "定位入口")
    left = store.add_object("p1", kind="function", name="main",
                            plan_id=plan["id"], task_id=task["id"])
    right = store.add_object("p1", kind="function", name="main_alt",
                             plan_id=plan["id"])
    store.add_object("p1", kind="evidence", name="trace",
                     plan_id=plan["id"], task_id=task["id"])
    conflict = store.add_conflict(
        "p1", left_object_id=left["id"], right_object_id=right["id"],
        field="address", summary="入口地址不一致")

    result = store.verify_task("p1", task["id"])

    assert result["status"] == "conflict"
    assert result["issues"][0]["conflict_id"] == conflict["id"]
    assert len(result["followup_task_ids"]) == 1
    followup = next(t for t in store.get_plan("p1", plan["id"])["tasks"]
                    if t["id"] == result["followup_task_ids"][0])
    assert followup["title"] == "复核冲突：入口地址不一致"


def test_coordination_verification_passes_and_completes_task(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "通过验证")
    task = store.add_task("p1", plan["id"], "确认样本")
    store.add_object("p1", kind="artifact", name="report.json",
                     plan_id=plan["id"], task_id=task["id"])

    result = store.verify_task("p1", task["id"])

    assert result["status"] == "passed"
    assert result["task_status"] == "completed"
    assert store.get_plan("p1", plan["id"])["tasks"][0]["status"] == "completed"


def test_coordination_plan_verification_completes_only_when_all_pass(tmp_path):
    bb = Blackboard(str(tmp_path / "bb.db"))
    _project(bb)
    store = CoordinationStore(bb)
    plan = store.create_plan("p1", "计划闭环")
    first = store.add_task("p1", plan["id"], "任务一")
    second = store.add_task("p1", plan["id"], "任务二")
    store.add_object("p1", kind="evidence", name="evidence-1",
                     plan_id=plan["id"], task_id=first["id"])

    incomplete = store.verify_plan("p1", plan["id"])
    assert incomplete["status"] == "incomplete"
    assert incomplete["passed"] == 1
    assert store.get_plan("p1", plan["id"])["status"] == "draft"

    store.add_object("p1", kind="evidence", name="evidence-2",
                     plan_id=plan["id"], task_id=second["id"])
    complete = store.verify_plan("p1", plan["id"])
    assert complete["status"] == "completed"
    assert complete["passed"] == 2
    assert store.get_plan("p1", plan["id"])["status"] == "completed"
