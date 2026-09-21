"""⑤ 任务完成对账（硬拦）测试。

- 登记：publish(acceptance=...) 写 context.reconcile（不动表结构，零迁移）。
- 拦截：存在 pending 条目时 complete 抛 ValueError + 落 task.reconcile_blocked 事件。
- 收口：task_reconcile 逐条置 met/failed/blocked 后 complete 放行。
- 人工绕过：fail/reopen 不检对账（人工通道天然豁免）。
- 边界：非持有者/未认领任务、非法 state、空白条目不入表。
"""
import pytest

from core.blackboard import Blackboard, ClaimError, TaskQueue


@pytest.fixture()
def bb(tmp_path):
    board = Blackboard(str(tmp_path / "reconcile.db"))
    yield board
    board.close()


@pytest.fixture()
def project(bb):
    return bb.create_project("对账项目", "pentest", ["web"])


def _claim(tq: TaskQueue, bb: Blackboard, project, name="S1"):
    sess = bb.register_session(project["id"], name)
    tid = tq.publish(project["id"], "带验收的任务", created_by="human",
                     acceptance=["确认 10.0.0.1 的 80 端口服务指纹", "核对登录页是否存在弱口令"])
    tq.claim(tid, sess["id"])
    return sess, tid


def test_publish_registers_reconcile_entries(bb, project):
    """acceptance 登记为 context.reconcile（trim、去空、截 300）；无验收不出键。"""
    tq = TaskQueue(bb)
    _, tid = _claim(tq, bb, project)
    rec = tq.get_task(tid)["context"]["reconcile"]
    assert [(e["id"], e["state"]) for e in rec] == [(1, "pending"), (2, "pending")]
    assert rec[0]["text"] == "确认 10.0.0.1 的 80 端口服务指纹"
    # 无验收条目：context 不含 reconcile 键（attachments 先例，键缺省）
    tid2 = tq.publish(project["id"], "无验收任务", created_by="human")
    assert "reconcile" not in tq.get_task(tid2)["context"]
    # 全空白条目等价于无验收
    tid3 = tq.publish(project["id"], "空验收任务", created_by="human",
                      acceptance=["", "   "])
    assert "reconcile" not in tq.get_task(tid3)["context"]


def test_complete_blocked_with_pending_entries(bb, project):
    """分母未收口 → complete 抛错列条目，落 task.reconcile_blocked 事件；任务保持 claimed。"""
    tq = TaskQueue(bb)
    sess, tid = _claim(tq, bb, project)
    with pytest.raises(ValueError, match="完成对账未收口"):
        tq.complete(tid, sess["id"], "自报完成")
    # 条目逐一点名 + 收口指引
    evs = [e for e in bb.recent_events(project["id"]) if e["kind"] == "task.reconcile_blocked"]
    assert len(evs) == 1
    pending = evs[0]["payload"]["pending"]
    assert {p["id"] for p in pending} == {1, 2}
    assert "80 端口" in pending[0]["text"]
    assert tq.get_task(tid)["status"] == "claimed"  # 拦截不改状态


def test_reconcile_then_complete_passes(bb, project):
    """逐条收口（met/failed/blocked 均算收口）后 complete 放行；note 落条目。"""
    tq = TaskQueue(bb)
    sess, tid = _claim(tq, bb, project)
    rec = tq.set_reconcile_state(tid, sess["id"], 1, "met", "nmap -sV 确认 nginx")
    assert rec[0]["state"] == "met" and rec[0]["note"] == "nmap -sV 确认 nginx"
    tq.set_reconcile_state(tid, sess["id"], 2, "blocked", "登录页需要验证码，挂人工")
    tq.complete(tid, sess["id"], "条目 1 达成，条目 2 受阻移交人工")  # 不再抛错
    assert tq.get_task(tid)["status"] == "done"


def test_reconcile_guards(bb, project):
    """非法 state / 不存在条目 / 未认领任务持有人不符 → 拒绝且不改状态。"""
    tq = TaskQueue(bb)
    sess, tid = _claim(tq, bb, project)
    with pytest.raises(ValueError):  # 非法 state
        tq.set_reconcile_state(tid, sess["id"], 1, "done")
    with pytest.raises(ValueError):  # 条目不存在
        tq.set_reconcile_state(tid, sess["id"], 9, "met")
    with pytest.raises(ClaimError):  # 非持有者
        other = bb.register_session(project["id"], "S2")
        tq.set_reconcile_state(tid, other["id"], 1, "met")
    assert all(e["state"] == "pending" for e in tq.get_task(tid)["context"]["reconcile"])


def test_fail_and_reopen_bypass_reconcile(bb, project):
    """人工通道不检对账：fail 直接落，reopen 后新会话接手收口后完成。"""
    tq = TaskQueue(bb)
    sess, tid = _claim(tq, bb, project)
    tq.fail(tid, sess["id"], "人工判停")  # 不抛错
    assert tq.get_task(tid)["status"] == "failed"
    tq.reopen(tid)
    sess2 = bb.register_session(project["id"], "S3")
    tq.claim(tid, sess2["id"])
    with pytest.raises(ValueError):  # 新尝试仍受对账约束
        tq.complete(tid, sess2["id"], "想混过去")
    tq.set_reconcile_state(tid, sess2["id"], 1, "met")
    tq.set_reconcile_state(tid, sess2["id"], 2, "failed", "弱口令不存在")
    tq.complete(tid, sess2["id"], "条目 2 诚实报未达成")
    assert tq.get_task(tid)["status"] == "done"
