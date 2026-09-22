"""多会话协调演练：真实 Ark 模型驱动 Orchestrator + 并行 Worker（CTF 逆向场景）。

流程（DESIGN.md §6.4 全四职责演示）：
  人类预填黑板（附件资产）→ 发布分诊/逆向两个任务
  tick① 监控+开窗：Orchestrator 派生窗口（recon + reverse）
  并行 Worker：两会话同时认领执行（func_kb 查重防止重复分析）
  tick② 派生：根据新发现发布验证任务 → Worker 再执行
  tick③ 汇总：项目简报落 project.digest 事件

用法（项目根）：
    E:\\Miniconda3\\python.exe scripts\\demo_orchestrator.py
"""

import hashlib
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent import AgentConfig, AgentSession
from core.llm import ArkCodingProvider, ModelRouter
from core.orchestrator import Orchestrator, OrchestratorConfig
from core.projects import ProjectStore
from core.runtime import ExecutionGateway, HostDetector
from core.skills.experts import load_expert

ROOT = Path(__file__).resolve().parent.parent
WORKSPACES = ROOT / "workspaces"
FIXTURE = ROOT / "tests" / "fixtures" / "crackme" / "crackme.elf"


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    store = ProjectStore(WORKSPACES)
    resume = "--resume" in sys.argv
    if resume:
        # 续跑：打开最近创建的项目，跳过预填与任务发布（上次中断处继续 tick）
        metas = sorted(store.list_projects(), key=lambda m: m["created_at"], reverse=True)
        proj = store.open_project(metas[0]["slug"])
        log(f"[resume] 续跑项目 {proj.id}（{proj.path.name}）")
    else:
        # 创建项目（DESIGN.md §5.3 布局）；模拟人类把样本放进 samples/
        proj = store.create_project("CTF 逆向多会话演练", "ctf", ["binary"])
        shutil.copy(FIXTURE, proj.samples_dir / FIXTURE.name)
    bb = proj.bb
    pid = proj.id
    SAMPLE = proj.samples_dir / FIXTURE.name
    log(f"[项目] {pid}  样本: {SAMPLE.name}")

    # ---- 人机共写（§6.5）：人类预填已知信息，Agent 视为高置信 ----
    sha = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    if not resume:
        asset = bb.upsert_asset(pid, "binary", sha,
                                meta={"path": str(SAMPLE), "type": "ELF64"},
                                author="human")
        bb.add_artifact(pid, str(SAMPLE), kind="sample",
                        description="crackme ELF（XOR 校验）", sha256=sha, author="human")
        log(f"[人类] 附件已登记 asset={asset['id']} sha256={sha[:16]}…")

        # ---- 初始任务（人类发布） ----
        from core.blackboard import TaskQueue
        tq = TaskQueue(bb)
        tq.publish(pid,
                   f"对样本 {SAMPLE} 做静态分诊：file 类型识别 + strings 字符串提取，"
                   "把结论作为发现写入黑板（含建议下一步）。样本按 untrusted 处理。",
                   task_type="triage", created_by="human")
        tq.publish(pid,
                   f"逆向样本 {SAMPLE}（sha256={sha}）：定位校验函数、还原算法；"
                   "func_kb 先查后写（bb_query what=func / bb_upsert_func），"
                   "求解出正确输入并落发现。",
                   task_type="reverse", created_by="human")
        log("[人类] 已发布任务: triage ×1, reverse ×1")
    else:
        log("[resume] 跳过预填与任务发布")

    # ---- 公共设施 ----
    inventory = HostDetector().probe()
    router = ModelRouter()
    llm = ArkCodingProvider(model=router.model_for("executor"))
    planner = ArkCodingProvider(model=router.model_for("planner"))
    gateway = ExecutionGateway(bb=bb)

    def session_factory(role: str) -> AgentSession:
        r = load_expert("packs", role, "ctf")
        return AgentSession(
            project_id=pid, bb=bb, gateway=gateway,
            llm=llm, planner_llm=planner,
            packs_root="packs", track="ctf", capabilities=proj.capabilities,
            role=role,
            capability_prompt=inventory.to_prompt(),
            config=AgentConfig(max_steps=10),
        )

    orch = Orchestrator(project_id=pid, bb=bb, llm=planner,
                        session_factory=session_factory,
                        config=OrchestratorConfig(max_steps=10,
                                                  allowed_roles=["recon", "reverse"],
                                                  max_sessions=2, digest_every=2),
                        packs_root="packs", track="ctf")

    def run_workers() -> None:
        """所有活跃窗口并行跑 run_next_task 循环，直到队列为空。"""
        threads = []
        for sid, sess in list(orch.live_sessions.items()):
            def worker(s=sess, name=sid):
                while True:
                    got = s.run_next_task()
                    if got is None:
                        break
                    log(f"  [{name}] 完成任务")
            t = threading.Thread(target=worker, name=sid)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

    # ---- tick① 开窗 ----
    log("\n===== tick① 监控 + 开窗 =====")
    r = orch.tick()
    log(f"[orch] {r['summary']}（发布 {len(r['published'])}，开窗 {len(r['spawned'])}，"
        f"简报 {'有' if r['digest'] else '无'}）")
    log(f"[orch] 活跃窗口: {len(orch.live_sessions)}")

    log("\n===== 并行执行（func_kb 协作） =====")
    t0 = time.time()
    run_workers()
    log(f"[执行] 耗时 {time.time() - t0:.0f}s")

    # ---- tick② 派生 ----
    log("\n===== tick② 派生（新发现 → 验证任务） =====")
    r = orch.tick()
    log(f"[orch] {r['summary']}（发布 {len(r['published'])}，开窗 {len(r['spawned'])}，"
        f"简报 {'有' if r['digest'] else '无'}）")
    if any(t["status"] == "open" for t in orch.tq.list_tasks(pid)):
        log("[执行] 派生任务由现有窗口继续执行…")
        run_workers()

    # ---- tick③ 汇总 ----
    log("\n===== tick③ 汇总 =====")
    r = orch.tick()
    log(f"[orch] {r['summary']}（发布 {len(r['published'])}，开窗 {len(r['spawned'])}，"
        f"简报 {'有' if r['digest'] else '无'}）")

    # ---- 演练结果 ----
    log("\n===== 黑板终态 =====")
    log("[函数知识库 func_kb]")
    for f in bb.list_funcs(pid):
        log(f"  {f['address']:#x} {f['name']}  by {f['analyzed_by']}  conf={f['confidence']}")
        log(f"    └ {f['analysis'][:120]}")
    log("[发现]")
    for fd in bb.list_findings(pid):
        log(f"  {fd['severity']}/{fd['status']} {fd['vuln_class']}: {fd['title']}")
    log("[简报]")
    for e in bb.recent_events(pid):
        if e["kind"] == "project.digest":
            log("  " + e["payload"]["digest"][:400])
    log("[事件流]")
    for e in bb.recent_events(pid):
        log(f"  #{e['id']} [{e['kind']}] {e['author']}")
    bb.close()


if __name__ == "__main__":
    main()
