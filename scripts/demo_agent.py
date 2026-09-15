"""端到端演示：真实 Ark 模型 + 真实网关执行 + 黑板审计。

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\demo_agent.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import AgentConfig, AgentSession
from core.blackboard import TaskQueue
from core.llm import ArkCodingProvider, ModelRouter
from core.projects import ProjectStore
from core.runtime import ExecutionGateway, HostDetector

WORKSPACES = Path(__file__).resolve().parent.parent / "workspaces"


def main() -> None:
    store = ProjectStore(WORKSPACES)
    proj = store.create_project("端到端演示", "assessment", ["web"],
                                config={"cross_target": "ask-orchestrator"})
    bb = proj.bb
    pid = proj.id

    # 能力清单（detector 实测）
    detector = HostDetector()
    inventory = detector.probe()
    print("[detector]\n" + inventory.to_prompt() + "\n")

    # 模型路由：executor / classifier
    router = ModelRouter()
    llm = ArkCodingProvider(model=router.model_for("executor"))
    planner = ArkCodingProvider(model=router.model_for("planner"))

    gateway = ExecutionGateway(bb=bb)
    agent = AgentSession(
        project_id=pid, bb=bb, gateway=gateway,
        llm=llm, planner_llm=planner,
        track="assessment", capabilities=["web"], role="_generalist",
        capability_prompt=inventory.to_prompt(),
        config=AgentConfig(max_steps=8),
    )

    objective = (
        "演示任务：用 run_cmd(host, trusted) 列出项目根目录的文件清单，"
        "然后把『本目录是一个 AI 安全平台项目』作为一条 info 级发现写入黑板，"
        "最后 finish 总结。全程两三个工具调用内完成。"
    )
    summary = agent.run_task(objective)
    print(f"\n[会话总结] {summary}")

    print("\n[黑板事件流]")
    for e in bb.recent_events(pid):
        head = str(e["payload"])[:100]
        print(f"  #{e['id']} [{e['kind']}] {e['author']}: {head}")

    print("\n[发现]")
    for f in bb.list_findings(pid):
        print(f"  {f['id']} {f['severity']}/{f['status']} {f['vuln_class']}: {f['title']}")
    bb.close()


if __name__ == "__main__":
    main()
