"""临时调试脚本：复现 reopen 委托终态失败，dump 事件流（用完即删由用户决定）。"""
import sys
import time
import pathlib

sys.path.insert(0, r"E:\ILOVCTRY\cyberstrike-pro")

from fastapi.testclient import TestClient

from test_api import _chain_app
from test_orchestrator import ScriptedLLM as S

executor = [
    {"tool_use": [S.tool_call("a1", "fail_task",
                              {"result_note": "缺授权凭据，需要人类补充",
                               "blocked_reason": "awaiting_human"})]},
    {"tool_use": [S.tool_call("c1", "complete_task", {"result_note": "人工已解决"})]},
]
app = _chain_app(pathlib.Path(sys.argv[1]), [], executor)
with TestClient(app) as c:
    pid = c.post("/api/projects", json={"name": "L2", "track": "pentest",
                                        "capabilities": ["web"]}).json()["id"]
    # L2 化
    c.patch(f"/api/projects/{pid}/config",
            json={"config": {"autonomy": {"level": "L2"}}})
    sp = c.post(f"/api/projects/{pid}/agents",
                json={"role": "_generalist", "armed": True})
    manual_sid = sp.json()["id"]
    time.sleep(0.5)
    ep = c.post(f"/api/projects/{pid}/agents",
                json={"role": "_generalist", "armed": False})
    bound_sid = ep.json()["id"]
    from core.blackboard import TaskQueue
    bb = c.app.state.projects[pid].bb
    tid = TaskQueue(bb).publish(pid, "会挂起的委托", task_type="generic",
                                target_session=bound_sid)
    c.post(f"/api/agents/{bound_sid}/work")
    for _ in range(300):
        if TaskQueue(bb).get_task(tid)["status"] == "failed":
            break
        time.sleep(0.02)
    rr = c.post(f"/api/tasks/{tid}/reopen", json={"note": "凭证已补"})
    print("KICKED:", rr.json()["kicked"])
    time.sleep(1.0)
    for e in bb.recent_events(pid, limit=200):
        p = e.get("payload") or {}
        print(e["kind"], "| sess=", e.get("session_id"),
              "| ", {k: p[k] for k in ("task_id", "session_id", "by", "reason", "blocked_reason") if k in p})
