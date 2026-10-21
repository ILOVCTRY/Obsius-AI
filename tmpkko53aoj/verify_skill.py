import sys, tempfile
sys.path.insert(0, ".")
from pathlib import Path
from core.blackboard.store import Blackboard
from core.chat import store as chat_store
from core.chat.runtime import ChatTurn, ORCHESTRATOR_ID

tmp = Path(tempfile.mkdtemp())
bb = Blackboard(str(tmp / "bb.db"))
with bb._tx():
    bb.conn.execute(
        "INSERT INTO projects(id, name, domain, track, config, created_at)"
        " VALUES(?,?,?,?,?,datetime('now'))", ("p1", "测试", "p1", "ctf", "{}"))

# 建一个 triage 子专家线程（模拟 call_expert spawn 出来的）
th = chat_store.create_thread(bb, "p1", "triage")
turn = ChatTurn(bb=bb, llm=None, project_id="p1", thread_id=th["id"],
                packs_root="packs", track="ctf", capabilities=["web"],
                mcp_bridge=None, expert_names=[])

sysp = turn._system_prompt()
i_role = sysp.find("# 角色")
i_skill = sysp.find("技能清单与打开时机")
i_work = sysp.find("## 工作方式")
i_ctx = sysp.find("工作台委派上下文")
print(f"总长 {len(sysp)}")
print(f"角色 {i_role} | 技能清单 {i_skill} | 工作方式 {i_work} | 委派上下文 {i_ctx}")
print("顺序正确（技能清单在角色后、工作方式前）:", i_role < i_skill < i_work)
print()
print("=== 技能清单段 ===")
print(sysp[i_skill:i_skill + 900])

print("\n=== B2 软闸验证 ===")
turn._reset_skill_nudge()
# 1) 知识类工具：不提醒
print("kb_search ->", repr(turn._skill_first_nudge("kb_search"))[:40])
# 2) 执行类工具：提醒（首次）
n = turn._skill_first_nudge("run_cmd")
print("run_cmd 首次 ->", ("有提醒" if n else "无提醒"), "| 长度", len(n))
# 3) 第二次：不再提醒
print("run_cmd 再次 ->", ("有提醒" if turn._skill_first_nudge("run_cmd") else "无提醒（正确，只提醒一次）"))

# 4) 已 skill_open 过则不提醒
turn._reset_skill_nudge()
turn._skill_opened_round.add("总入口分诊")
print("已读技能后 run_cmd ->", ("有提醒" if turn._skill_first_nudge("run_cmd") else "无提醒（正确）"))

print("\n=== skill.routed 事件验证 ===")
evs = [e for e in bb.recent_events("p1", limit=50) if e["kind"] == "skill.routed"]
print("事件数:", len(evs))
if evs:
    p = evs[-1]["payload"]
    print("injected:", p.get("injected"))
    print("count:", p.get("count"), "| source:", p.get("source"), "| agent:", p.get("agent_id"))
bb.close()
