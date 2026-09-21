# -*- coding: utf-8 -*-
"""CTF 评测基准回归 harness（2026-09-20，§17 K8 · 外部对标升级项 D）。

用途：对 CTF 轨做可重复的量化评测——开源侧全员接 NYU CTF Bench / CyBench，
本项目此前为零（见 docs/pentest-agent-benchmark.md §二）。本脚本是 harness 骨架：
经 core API 单一入口批量建 ctf 项目 → 发布任务 → 等待收敛 → 收 flag 判分 →
报告落 docs/nyuctf-eval.md。

用法：
    python scripts/eval_ctf.py --manifest docs/eval/nyuctf-subset.json \
        [--base-url http://127.0.0.1:8420] [--report docs/nyuctf-eval.md]

manifest（JSON）：
    {
      "name": "nyuctf-subset-2026",
      "capabilities": ["binary"],            # 评测项目挂的能力包
      "tasks": [
        {"id": "pwn-01", "type": "pwn",
         "objective": "题目描述……", "flag": "flag{...}",
         "scope": "题面附件名（可选）", "priority": 5}
      ]
    }

约定与防线：
- **防基准污染**：manifest 与 flag 不进 packs/kb（AI 不可见）；评测项目用独立
  命名前缀 `eval-`，跑完由人决定归档/删除（脚本不删项目）。
- 先小后大：先用 ≤5 题冒烟再放量；每题超时由 --deadline 统一控制。
- 判分口径：任务 findings/events 全文出现 manifest 里的 flag（精确子串）即
  solved；未收敛/超时=timeout；任务 failed=fail。报告只记结果不记 flag 原文。

[骨架] 会话拉起依赖编排器自动认领（项目 autonomy≥L1）；单题逐会话驱动、
更多输入源（附件上传/远程实例）为后续扩展，见 docs/nyuctf-eval.md 待办。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TASK_TERMINAL = {"done", "failed", "cancelled"}
PREFIX = "eval-"


def _req(base: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def load_manifest(path: str | Path) -> dict:
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    if not m.get("tasks"):
        raise SystemExit("manifest 缺 tasks")
    for t in m["tasks"]:
        for k in ("id", "type", "objective", "flag"):
            if not t.get(k):
                raise SystemExit(f"manifest 任务缺字段 {k}: {t}")
    return m


def run(manifest: dict, base: str, deadline_min: float) -> dict:
    now = datetime.now(timezone.utc)
    proj = _req(base, "POST", "/api/projects", {
        "name": f"{PREFIX}{manifest['name']}-{now:%Y%m%d%H%M%S}",
        "track": "ctf",
        "capabilities": manifest.get("capabilities") or ["binary"],
        "config": {"autonomy": "L1"},   # 自动链拉起 worker 认领
    })
    pid = proj["id"]
    print(f"[eval] 项目 {pid}")
    task_ids: dict[str, str] = {}
    for t in manifest["tasks"]:
        r = _req(base, "POST", f"/api/projects/{pid}/tasks", {
            "objective": t["objective"], "scope": t.get("scope"),
            "task_type": t["type"], "priority": t.get("priority", 5)})
        task_ids[r["id"]] = t["id"]
        print(f"[eval] 发布 {t['id']} -> {r['id']}")
    deadline = time.time() + deadline_min * 60
    while time.time() < deadline:
        rows = _req(base, "GET", f"/api/projects/{pid}/tasks")
        open_n = sum(1 for r in rows if r["status"] not in TASK_TERMINAL)
        print(f"[eval] 收敛中：未完成 {open_n}/{len(rows)}")
        if not open_n:
            break
        time.sleep(30)
    return judge(manifest, base, pid, task_ids)


def judge(manifest: dict, base: str, pid: str, task_ids: dict) -> dict:
    """判分：findings + events 全文找 flag。只回 solved 布尔与证据来源，不回 flag 原文。"""
    findings = _req(base, "GET", f"/api/projects/{pid}/findings")
    events = _req(base, "GET", f"/api/projects/{pid}/events?limit=200")
    ftext = json.dumps(findings, ensure_ascii=False)
    etext = json.dumps(events, ensure_ascii=False)
    results = []
    for t in manifest["tasks"]:
        tid = next((k for k, v in task_ids.items() if v == t["id"]), None)
        row = {"id": t["id"], "task_id": tid, "result": "fail"}
        if tid:
            status = next((r["status"] for r in
                           _req(base, "GET", f"/api/projects/{pid}/tasks")
                           if r["id"] == tid), "missing")
            hit_f = t["flag"] in ftext
            hit_e = t["flag"] in etext
            if hit_f or hit_e:
                row["result"] = "solved"
                row["evidence"] = "finding" if hit_f else "event"
            elif status == "failed":
                row["result"] = "fail"
            elif status not in TASK_TERMINAL:
                row["result"] = "timeout"
        results.append(row)
    solved = sum(1 for r in results if r["result"] == "solved")
    return {"project": pid, "total": len(results), "solved": solved,
            "results": results}


def write_report(out: dict, manifest: dict, report: str | Path) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# CTF 评测报告：{manifest['name']}", "",
        f"- 运行时间：{ts}；项目：`{out['project']}`（跑完请人工归档/删除）",
        f"- 结果：**{out['solved']}/{out['total']} solved**", "",
        "| 任务 | 任务 id | 结果 | 证据 |", "|---|---|---|---|",
    ]
    for r in out["results"]:
        lines.append(f"| {r['id']} | {r.get('task_id') or '-'} "
                     f"| {r['result']} | {r.get('evidence', '-')} |")
    lines += ["", "> flag 原文不入报告（防基准污染）；明细在评测项目黑板里。",
              "> manifest 放 `docs/eval/`（不进 packs/kb，AI 不可见）。"]
    Path(report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[eval] 报告 -> {report}")


def main() -> None:
    ap = argparse.ArgumentParser(description="CTF 评测基准回归 harness（K8）")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:8420")
    ap.add_argument("--deadline", type=float, default=60.0,
                    help="单轮收敛等待分钟数（默认 60）")
    ap.add_argument("--report", default=str(ROOT / "docs" / "nyuctf-eval.md"))
    args = ap.parse_args()
    manifest = load_manifest(args.manifest)
    out = run(manifest, args.base_url.rstrip("/"), args.deadline)
    write_report(out, manifest, args.report)
    sys.exit(0 if out["solved"] else 1)


if __name__ == "__main__":
    main()
