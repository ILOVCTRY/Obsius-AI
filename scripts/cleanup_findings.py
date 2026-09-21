# -*- coding: utf-8 -*-
"""存量发现清洗（两阶段）：按 edu-rating 评级口径对既有 findings 逐条重评。

背景：渗透/红队轨上线「info 全类别停收」（2026-09-18）与 edu-rating
「只认交互性实证」裁定后，存量发现里有 info 级残留与口径外内容（纯暴露面
如 WinRM/RDP 公网暴露、EOL 组件等）。本脚本复用 Agent 判级同源口径
（build_rules_preamble 规则注入 + core.skills.judge.judge_finding）逐条裁定：
    keep      保留（级别与条款相符，或拿不准——宁松勿误删）
    downgrade 有评级条款但级别虚高 → patch 到条款对应级别 + rating_basis
    to_intel  口径外（无任何漏洞条款对应）→ patch category=intel + low（转有效线索）
    delete    info 级（硬规则，不耗 LLM）/ 无保留价值的走查垃圾 → 物理删除

两阶段（apply 是破坏性操作，物理删除不进回收站）：
    1) dry-run（默认）：逐条打印裁定 + 汇总，裁定清单写 workspaces/<slug>/cleanup-plan-<ts>.json
    2) 人工审阅报告后：python scripts/cleanup_findings.py <slug> --apply --plan <plan.json>
       apply 只消费已确认的 plan 文件，**不重新调 LLM**（避免两次判定漂移）。

用法（项目根；Windows 控制台先 $env:PYTHONIOENCODING="utf-8"）：
    E:\\Miniconda3\\python.exe scripts\\cleanup_findings.py [项目slug，默认 assessment-20260915-7d70]
    E:\\Miniconda3\\python.exe scripts\\cleanup_findings.py <slug> --apply --plan workspaces/<slug>/cleanup-plan-<ts>.json

注意：跑前建议停该项目 worker（编排/会话写路径与清洗交错会互相看到半程状态）。
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.blackboard.store import SEVERITY_RANK
from core.llm import ArkCodingProvider, ModelRouter
from core.projects import ProjectStore
from core.skills.judge import judge_finding
from core.skills.rules import build_rules_preamble

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLUG = "assessment-20260915-7d70"  # 中原工学院（EDUSRC，pentest 轨）

JUDGE_PROMPT = """你是漏洞评级审核员。以下候选发现是渗透测试项目中已登记的历史记录，
请对照本项目的评级口径（edu-rating 等）逐条裁定处置：

- keep：级别与条款相符，或证据不足以判定（拿不准一律 keep，宁松勿误删）。
- downgrade：有评级条款对应但当前级别虚高（**只认交互性实证**——仅「端口公网开放
  + FOFA 佐证」不算，如 MinIO 未实测登进控制台不得按高危申报；EOL/暴露面本身无条款）。
  输出 severity=条款对应级别，basis=「rating:edu-rating <条款> <一句话依据>」。
- to_intel：口径外——内容有情报/线索价值但无任何漏洞条款对应（纯端口暴露面、
  过期组件/EOL、合规提示、信息泄露面等）→ 转有效线索（category=intel，severity=low）。
- delete：无保留价值的走查记录/重复噪声（拿不准不要 delete）。

只输出 JSON：
{"action": "keep"|"downgrade"|"to_intel"|"delete", "severity": "low|medium|high|critical", "basis": "rating:edu-rating <条款> <一句话>", "reason": "一句话"}


severity/basis 仅 downgrade 时必填且 basis 不得为空。"""


def log(msg: str) -> None:
    print(msg, flush=True)


def plan_path_for(slug: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "workspaces" / slug / f"cleanup-plan-{ts}.json"


def build_plan(proj, llm) -> list[dict]:
    """逐条裁定，返回 plan 条目列表（含 finding 快照）。"""
    bb, pid = proj.bb, proj.id
    rules = build_rules_preamble(
        ROOT / "packs", track=proj.track, capabilities=proj.capabilities,
        owner_tags=bb.owner_tags(pid),
        rule_profiles=(bb.get_project(pid).get("config") or {}).get("rule_profiles"))

    plan: list[dict] = []
    for f in bb.list_findings(pid):
        sev, cat = f["severity"], f.get("category") or "vuln"
        entry = {"finding_id": f["id"], "snapshot": {k: f.get(k) for k in
                 ("title", "severity", "category", "status", "vuln_class",
                  "rating_basis", "evidence")}, "action": "keep",
                 "severity": None, "basis": None, "reason": "", "flag": ""}
        if sev == "info":  # 硬规则先行：info 停收，直接删（不耗 LLM）
            entry.update(action="delete", reason="硬规则：pentest/redteam 轨 info 停收")
        else:
            v = judge_finding(llm, rules_text=rules, draft={
                "title": f["title"], "vuln_class": f["vuln_class"],
                "severity": sev, "category": cat, "status": f["status"],
                "rating_basis": f.get("rating_basis") or "",
                "evidence": f.get("evidence"),
            }, sys_prompt=JUDGE_PROMPT)
            if v is None:
                entry.update(action="keep", reason="", flag="[LLM 输出非法/失败 → 回退 keep]")
            else:
                act = str(v.get("action") or "keep")
                if act not in ("keep", "downgrade", "to_intel", "delete"):
                    act = "keep"
                    entry["flag"] = "[非法 action → 回退 keep]"
                new_sev = str(v.get("severity") or "").strip().lower()
                basis = str(v.get("basis") or "").strip()
                # 后校验（不信任 LLM）：降级目标必须 ≥low、不高于当前级、basis 非空
                if act == "downgrade" and (
                        new_sev not in SEVERITY_RANK or new_sev == "info"
                        or SEVERITY_RANK[new_sev] >= SEVERITY_RANK[sev]
                        or not basis):
                    act = "keep"
                    entry["flag"] = "[非法降级目标/basis 缺失 → 回退 keep]"
                entry.update(action=act, severity=new_sev if act == "downgrade" else None,
                             basis=basis if act == "downgrade" else None,
                             reason=str(v.get("reason") or ""))
        plan.append(entry)
        log(f"  {f['id']}  [{sev}/{cat}] {f['title'][:40]} → {entry['action']}"
            f"{' → ' + entry['severity'] if entry['severity'] else ''}"
            f"{'  ' + entry['flag'] if entry['flag'] else ''}")
    return plan


def apply_plan(proj, plan_file: Path) -> None:
    bb, pid = proj.bb, proj.id
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    entries = data["entries"] if isinstance(data, dict) else data
    n = {"downgrade": 0, "to_intel": 0, "delete": 0, "skipped": 0}
    for e in entries:
        fid, act = e["finding_id"], e.get("action")
        try:
            if act == "delete":
                bb.delete_finding(pid, fid, author="demo-script(re-rate)")
                n["delete"] += 1
            elif act in ("downgrade", "to_intel"):
                # to_intel 口径（§5.2 定稿）：category=intel + severity=low（线索级），
                # 与 downgrade（保留 intel/vuln 归属、降到条款级）区分
                bb.patch_finding(pid, fid,
                                 severity=e.get("severity") or "low",
                                 rating_basis=e.get("basis"),
                                 category="intel" if act == "to_intel" else None,
                                 track=proj.track, author="demo-script(re-rate)")
                n[act] += 1
            else:
                n["skipped"] += 1
        except (ValueError, LookupError) as ex:
            n["skipped"] += 1
            log(f"  [跳过] {fid} ({act}): {ex}")
    log(f"[apply 完成] 降级 {n['downgrade']} / 转线索 {n['to_intel']} / "
        f"删除 {n['delete']} / 跳过(keep+异常) {n['skipped']}")


def main() -> None:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    plan_file = None
    plan_val_idx = argv.index("--plan") + 1 if "--plan" in argv else None
    if plan_val_idx is not None and plan_val_idx < len(argv):
        plan_file = Path(argv[plan_val_idx])
    # slug = 第一个位置参数（排除 --plan 的取值位）
    pos = [i for i, a in enumerate(argv) if not a.startswith("--")
           and i != plan_val_idx]
    slug = argv[pos[0]] if pos else DEFAULT_SLUG

    proj = ProjectStore(ROOT / "workspaces").open_project(slug)
    if proj.track not in ("pentest", "redteam"):
        log(f"[退出] 项目轨为 {proj.track}——本脚本仅用于 pentest/redteam 轨存量清洗。")
        proj.close()
        return

    if apply and plan_file:
        log(f"[apply] {slug} ← {plan_file.name}")
        apply_plan(proj, plan_file)
    else:
        log(f"[dry-run] {slug}（track={proj.track}）逐条裁定中（真实 LLM，逐条消耗配额）…")
        router = ModelRouter()
        llm = ArkCodingProvider(model=router.model_for("executor"))
        plan = build_plan(proj, llm)
        counts: dict[str, int] = {}
        for e in plan:
            counts[e["action"]] = counts.get(e["action"], 0) + 1
        log("[汇总] " + " / ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            + f"（共 {len(plan)} 条）")
        out = plan_path_for(slug)
        out.write_text(json.dumps(
            {"slug": slug, "generated_at": datetime.now(timezone.utc).isoformat(),
             "entries": plan}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[plan 已写] {out}\n"
            f"人工审阅后执行：python scripts\\cleanup_findings.py {slug} "
            f"--apply --plan {out}")
    proj.close()


if __name__ == "__main__":
    main()
