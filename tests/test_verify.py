"""独立验证器测试（independent-verification-audit M1）：
四策略判定 / 规格校验 / 脱敏红线 / 收尾钩子（failed 重跑 + Agent 自报拒绝）。
全程 fake executor/gateway，零触网零真命令。"""

import pytest

from core.blackboard import Blackboard, TaskQueue
from core.verify import evaluate, run_reconcile_verifications, validate_verify_spec


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "bb.db"))
    yield board
    board.close()


@pytest.fixture()
def pid(bb):
    return bb.create_project("验证测试", "ctf", ["binary"])["id"]


class FakeResult:
    def __init__(self, stdout="", stderr="", exit_code=0, timed_out=False):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out


class FakeGateway:
    """钩子测试注入用假网关：记录调用，按 fn 产出结果/抛异常。"""

    def __init__(self, fn=None):
        self.calls = []
        self.fn = fn or (lambda cmd: FakeResult(stdout="ok"))

    def run(self, cmd, runtime="host", **kw):
        self.calls.append({"cmd": cmd, "runtime": runtime, **kw})
        return self.fn(cmd)


# ---------- 规格校验 ----------

def test_validate_ok_all_strategies():
    assert validate_verify_spec({"strategy": "flag_capture", "src": "file:flag.txt",
                                 "match": "exact", "value": "CTF{x}"})["match"] == "exact"
    assert validate_verify_spec({"strategy": "flag_capture", "cmd": "type f",
                                 "value": "CTF{x}"})["match"] == "contains"  # 缺省
    assert validate_verify_spec({"strategy": "effect_proof", "checks": [
        {"cmd": "curl x", "expect": "contains", "value": "OK"},
        {"cmd": "ls", "expect": "exit0"}]})["checks"][1]["expect"] == "exit0"
    assert validate_verify_spec({"strategy": "poc_crash", "cmd": "./poc"})["detector"] == "signal"
    assert validate_verify_spec({"strategy": "oracle", "cmd": "python judge.py"})


def test_validate_rejects_bad_specs():
    with pytest.raises(ValueError, match="未知验证策略"):
        validate_verify_spec({"strategy": "vibes", "cmd": "x"})
    with pytest.raises(ValueError, match="未知键"):
        validate_verify_spec({"strategy": "oracle", "cmd": "x", "extra": 1})
    # flag_capture：src/cmd 二选一
    with pytest.raises(ValueError, match="二选一"):
        validate_verify_spec({"strategy": "flag_capture", "value": "v"})
    with pytest.raises(ValueError, match="二选一"):
        validate_verify_spec({"strategy": "flag_capture", "src": "file:a", "cmd": "b",
                              "value": "v"})
    with pytest.raises(ValueError, match="file:"):
        validate_verify_spec({"strategy": "flag_capture", "src": "http://x", "value": "v"})
    with pytest.raises(ValueError, match="正则"):
        validate_verify_spec({"strategy": "flag_capture", "cmd": "x", "match": "regex",
                              "value": "([bad"})
    with pytest.raises(ValueError, match="value"):
        validate_verify_spec({"strategy": "flag_capture", "cmd": "x"})
    # effect_proof
    with pytest.raises(ValueError, match="非空数组"):
        validate_verify_spec({"strategy": "effect_proof", "checks": []})
    with pytest.raises(ValueError, match="value 必填"):
        validate_verify_spec({"strategy": "effect_proof", "checks": [
            {"cmd": "x", "expect": "contains"}]})
    with pytest.raises(ValueError, match="未知键"):
        validate_verify_spec({"strategy": "effect_proof", "checks": [
            {"cmd": "x", "timeout": 5}]})
    # poc_crash / oracle
    with pytest.raises(ValueError, match="detector"):
        validate_verify_spec({"strategy": "poc_crash", "cmd": "x", "detector": "prayer"})
    with pytest.raises(ValueError, match="非空命令"):
        validate_verify_spec({"strategy": "oracle", "cmd": "  "})
    with pytest.raises(ValueError, match="verify 规格必须是对象"):
        validate_verify_spec("flag")


# ---------- 判定引擎（evaluate，fake executor） ----------

def test_flag_capture_file_src(tmp_path):
    (tmp_path / "flag.txt").write_text("junk prefix CTF{real} tail", encoding="utf-8")
    spec = {"strategy": "flag_capture", "src": "file:flag.txt",
            "match": "contains", "value": "CTF{real}"}
    assert evaluate(spec, executor=lambda c: FakeResult(), workspace=tmp_path)["passed"] is True
    spec["match"] = "exact"
    assert evaluate(spec, executor=lambda c: FakeResult(), workspace=tmp_path)["passed"] is False
    (tmp_path / "flag.txt").write_text("CTF{real}", encoding="utf-8")
    assert evaluate(spec, executor=lambda c: FakeResult(), workspace=tmp_path)["passed"] is True
    spec["match"] = "regex"
    spec["value"] = r"CTF\{real\}"
    assert evaluate(spec, executor=lambda c: FakeResult(), workspace=tmp_path)["passed"] is True
    # 文件缺失 → fail-closed
    spec["src"] = "file:nope.txt"
    v = evaluate(spec, executor=lambda c: FakeResult(), workspace=tmp_path)
    assert v["passed"] is False and "不存在" in v["evidence_head"]


def test_flag_capture_traversal_fail_closed(tmp_path):
    spec = {"strategy": "flag_capture", "src": "file:../escape.txt",
            "match": "contains", "value": "x"}
    v = evaluate(spec, executor=lambda c: FakeResult(), workspace=tmp_path)
    assert v["passed"] is False and "被拒" in v["evidence_head"]


def test_flag_capture_cmd_src_masking(tmp_path):
    secret = "CTF{s3cr3t_fl4g}"
    spec = {"strategy": "flag_capture", "cmd": "cat flag", "match": "contains",
            "value": secret}
    v = evaluate(spec, executor=lambda c: FakeResult(stdout=f"junk {secret} junk"),
                 workspace=tmp_path)
    assert v["passed"] is True
    # 脱敏红线：命中与否都不回显原文，只露长度/哈希前缀
    assert secret not in v["evidence_head"]
    assert "sha256:" in v["evidence_head"] and "exit=" in v["evidence_head"]


def test_effect_proof_checks(tmp_path):
    def executor(cmd):
        return FakeResult(stdout="SERVICE OK", exit_code=0 if "alive" in cmd else 1)
    spec = {"strategy": "effect_proof", "checks": [
        {"cmd": "ping alive", "expect": "exit0"},
        {"cmd": "curl status", "expect": "contains", "value": "SERVICE OK"},
        {"cmd": "curl ver", "expect": "regex", "value": r"v\d+\.\d+"}]}
    # 第三项输出不匹配正则 → 失败时点名第几项，不回显输出原文
    v = evaluate(spec, executor=executor, workspace=tmp_path)
    assert v["passed"] is False and "3/3" in v["evidence_head"]
    spec["checks"][2]["value"] = "SERVICE"
    spec["checks"][2]["expect"] = "contains"
    v = evaluate(spec, executor=executor, workspace=tmp_path)
    assert v["passed"] is True and "3 项检查全过" in v["evidence_head"]
    # 第一项 exit≠0 → 拦
    spec2 = {"strategy": "effect_proof", "checks": [{"cmd": "die", "expect": "exit0"}]}
    assert evaluate(spec2, executor=executor, workspace=tmp_path)["passed"] is False


def test_poc_crash_detectors(tmp_path):
    sig = {"strategy": "poc_crash", "cmd": "./poc", "detector": "signal"}
    assert evaluate(sig, executor=lambda c: FakeResult(exit_code=-11),
                    workspace=tmp_path)["passed"] is True
    assert evaluate(sig, executor=lambda c: FakeResult(exit_code=3221225477),
                    workspace=tmp_path)["passed"] is True  # Windows 0xC0000005
    assert evaluate(sig, executor=lambda c: FakeResult(exit_code=0),
                    workspace=tmp_path)["passed"] is False
    # 超时不算崩溃
    r = evaluate(sig, executor=lambda c: FakeResult(timed_out=True), workspace=tmp_path)
    assert r["passed"] is False and "超时" in r["evidence_head"]
    # asan 特征扫 stdout+stderr
    asan = {"strategy": "poc_crash", "cmd": "./poc", "detector": "asan"}
    assert evaluate(asan, executor=lambda c: FakeResult(
        stderr="ERROR: AddressSanitizer: SEGV on unknown address"),
        workspace=tmp_path)["passed"] is True
    assert evaluate(asan, executor=lambda c: FakeResult(stdout="all good"),
                    workspace=tmp_path)["passed"] is False


def test_oracle_json_and_masking(tmp_path):
    spec = {"strategy": "oracle", "cmd": "python judge.py"}
    v = evaluate(spec, executor=lambda c: FakeResult(stdout='{"pass": true, "detail": "x"}'),
                 workspace=tmp_path)
    assert v["passed"] is True
    v = evaluate(spec, executor=lambda c: FakeResult(stdout='{"pass": false}'),
                 workspace=tmp_path)
    assert v["passed"] is False
    # 带前后缀日志也能抠出 JSON；detail 永不回显（可能含 flag）
    v = evaluate(spec, executor=lambda c: FakeResult(
        stdout='[INFO] judging\n{"pass": true, "detail": "CTF{s3cr3t}"}'),
        workspace=tmp_path)
    assert v["passed"] is True and "CTF{s3cr3t}" not in v["evidence_head"]
    # 非法输出 fail-closed
    v = evaluate(spec, executor=lambda c: FakeResult(stdout="I judge it good"),
                 workspace=tmp_path)
    assert v["passed"] is False and "JSON" in v["evidence_head"]


# ---------- 收尾钩子（run_reconcile_verifications，fake gateway 注入） ----------

def _claimed_task(bb, pid, acceptance, name="W1"):
    tq = TaskQueue(bb)
    sess = bb.register_session(pid, name)["id"]
    tid = tq.publish(pid, "完成验收", noise_budget="passive", acceptance=acceptance)
    tq.claim(tid, sess)
    return tq, sess, tid


def test_hook_fail_blocks_complete_then_reverify_passes(bb, pid, tmp_path):
    """flag 文件不对 → complete 被拦（state=failed + verify.result 事件）；
    修正后重新 complete → 验证器重跑通过 → done。"""
    tq, sess, tid = _claimed_task(bb, pid, [
        {"text": "拿到真 flag", "verify": {"strategy": "flag_capture",
                                            "src": "file:flag.txt",
                                            "match": "contains", "value": "CTF{real}"}},
    ])
    (tmp_path / "flag.txt").write_text("CTF{wrong}", encoding="utf-8")
    with pytest.raises(ValueError, match="独立验证"):
        tq.complete(tid, sess)
    ent = tq.get_task(tid)["context"]["reconcile"][0]
    assert ent["state"] == "failed" and ent["by"] == "verifier"
    assert "未命中" in ent["note"] and "CTF{wrong}" not in ent["note"]
    evs = [e for e in bb.recent_events(pid) if e["kind"] == "verify.result"]
    assert len(evs) == 1 and evs[0]["payload"]["passed"] is False
    assert evs[0]["payload"]["strategy"] == "flag_capture"
    assert evs[0]["author"] == "verifier"

    (tmp_path / "flag.txt").write_text("junk CTF{real}", encoding="utf-8")
    tq.complete(tid, sess)  # 重跑通过，不再拦
    task = tq.get_task(tid)
    assert task["status"] == "done"
    assert task["context"]["reconcile"][0]["state"] == "met"
    assert len([e for e in bb.recent_events(pid)
                if e["kind"] == "verify.result"]) == 2


def test_hook_cmd_spec_platform_identity(bb, pid):
    """cmd 型规格经注入网关执行：host + trusted + author=verifier + 工作区隔离。"""
    spec = {"strategy": "effect_proof",
            "checks": [{"cmd": "curl -s http://target", "expect": "contains",
                        "value": "PWNED"}]}
    tq, sess, tid = _claimed_task(bb, pid, [{"text": "拿下首页", "verify": spec}])
    fake = FakeGateway(lambda cmd: FakeResult(stdout="...PWNED..."))
    results = run_reconcile_verifications(tq, tid, sess, gateway=fake)
    assert results[0]["passed"] is True
    call = fake.calls[0]
    assert call["runtime"] == "host" and call["threat_class"] == "trusted"
    assert call["author"] == "verifier" and call["project_id"] == pid
    assert call["session_id"] == sess


def test_hook_gateway_denied_fail_closed(bb, pid):
    from core.runtime.gateway import GatewayDenied
    tq, sess, tid = _claimed_task(bb, pid, [
        {"text": "越网验证", "verify": {"strategy": "oracle", "cmd": "curl evil"}}])
    fake = FakeGateway(lambda cmd: (_ for _ in ()).throw(
        GatewayDenied("策略拒绝：验证命令被网关拦下", runtime="host")))
    with pytest.raises(ValueError, match="独立验证"):
        run_reconcile_verifications(tq, tid, sess, gateway=fake)
    ent = tq.get_task(tid)["context"]["reconcile"][0]
    assert ent["state"] == "failed" and "被拒" in ent["note"]


def test_agent_cannot_self_report_verify_entry(bb, pid):
    """红线：verify 条目 met/failed 由验证器写，Agent 自报被拒；blocked 放行。"""
    tq, sess, tid = _claimed_task(bb, pid, [
        {"text": "拿到真 flag", "verify": {"strategy": "flag_capture", "cmd": "cat f",
                                           "value": "CTF{real}"}},
        "普通条目（无验证规格）"])
    with pytest.raises(ValueError, match="验证器"):
        tq.set_reconcile_state(tid, sess, 1, "met", note="我说我拿到了")
    with pytest.raises(ValueError, match="验证器"):
        tq.set_reconcile_state(tid, sess, 1, "failed", note="我说做不了")
    entries = tq.set_reconcile_state(tid, sess, 1, "blocked", note="目标已下线，不适用")
    assert entries[0]["state"] == "blocked"
    # 普通条目照旧自报
    tq.set_reconcile_state(tid, sess, 2, "met", note="done")
    assert tq.get_task(tid)["context"]["reconcile"][1]["state"] == "met"
    # blocked 跳过验证 → complete 直通（人类可审计的不适用出口）
    tq.complete(tid, sess)
    assert tq.get_task(tid)["status"] == "done"


def test_hook_skips_non_verify_and_unclaimed(bb, pid):
    tq, sess, tid = _claimed_task(bb, pid, ["普通条目"])
    # 无 verify 条目 → 钩子零成本直返，普通 complete 语义不变
    assert run_reconcile_verifications(tq, tid, sess, gateway=FakeGateway()) == []
    tq.set_reconcile_state(tid, sess, 1, "met")
    tq.complete(tid, sess)
    assert tq.get_task(tid)["status"] == "done"
    # 非持有者/未认领 → 钩子不越权判定
    tq2_sess = bb.register_session(pid, "W2")["id"]
    tid2 = tq.publish(pid, "再来一单", noise_budget="passive",
                      acceptance=[{"text": "x", "verify": {"strategy": "oracle",
                                                           "cmd": "j"}}])
    assert run_reconcile_verifications(tq, tid2, tq2_sess, gateway=FakeGateway()) == []


def test_publish_invalid_spec_rejected(bb, pid):
    """发布期校验：坏规格 ValueError（publish→422/[拒绝]），任务不入库。"""
    tq = TaskQueue(bb)
    with pytest.raises(ValueError, match="未知键"):
        tq.publish(pid, "坏规格", noise_budget="passive", acceptance=[
            {"text": "x", "verify": {"strategy": "oracle", "cmd": "j", "hack": True}}])
    rows = [t for t in tq.list_tasks(pid) if t["objective"] == "坏规格"]
    assert rows == []


def test_publish_mixed_acceptance_shapes(bb, pid):
    """str 与 {text, verify} 混排：id 连续、纯文本条目不带 verify 键。"""
    tq = TaskQueue(bb)
    tid = tq.publish(pid, "混排", noise_budget="passive", acceptance=[
        "第一条",
        {"text": "第二条带验证", "verify": {"strategy": "flag_capture", "cmd": "x",
                                            "value": "v"}},
        {"text": "第三条纯文本对象"},
    ])
    entries = tq.get_task(tid)["context"]["reconcile"]
    assert [e["id"] for e in entries] == [1, 2, 3]
    assert "verify" not in entries[0] and "verify" not in entries[2]
    assert entries[1]["verify"]["strategy"] == "flag_capture"
