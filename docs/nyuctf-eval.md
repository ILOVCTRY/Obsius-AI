# CTF 评测基准回归（§17 K8 · 外部对标升级项 D）

> 状态：**harness 骨架已落（scripts/eval_ctf.py），基线数据待跑**。
> 本文是 K8 的落点文档：评测方法、manifest 约定、结果登记表与待办。
> 对标依据见 [pentest-agent-benchmark.md](pentest-agent-benchmark.md) §二
> （开源侧全员量化评测、本项目此前为零；NYU CTF Bench / CyBench 是 CTF 轨的
> 标准基准集）。

## 方法

- harness：`scripts/eval_ctf.py`——manifest（JSON：任务 id/type/objective/flag）
  → 经 core API 建 `eval-` 前缀 ctf 项目（autonomy L1）→ 批量发布任务 →
  轮询收敛 → findings/events 全文找 flag 判分 → 报告回写本文。
- **防基准污染**：manifest 与 flag 放 `docs/eval/`（不进 packs/kb，AI 不可见）；
  报告只记 solved/fail/timeout 与证据来源，不记 flag 原文；评测项目跑完由人
  归档/删除。
- **先小后大**：首跑 ≤5 题（冒烟，验证 harness 与判分口径），再放量到
  NYU CTF Bench 子集 / CyBench 子集。

## manifest 约定

```json
{
  "name": "nyuctf-subset-2026",
  "capabilities": ["binary"],
  "tasks": [
    {"id": "pwn-01", "type": "pwn", "objective": "题面描述…",
     "flag": "flag{...}", "scope": "附件名（可选）", "priority": 5}
  ]
}
```

## 结果登记表（每跑一行）

| 日期 | manifest | solved/total | 项目 | 备注 |
|---|---|---|---|---|
| — | — | 尚未首跑 | — | 冒烟集待整理（≤5 题） |

## 待办

- [ ] 整理冒烟 manifest（≤5 题：pwn/rev/misc 各 1-2），首跑并登记结果
- [ ] 附件输入：题目附件经 samples 上传接口挂进任务（harness `--attach`）
- [ ] 远程实例输入：动态 flag 的 pwn 题需要 `nc` 实例对接（判分改为回显比对）
- [ ] CyBench 子集适配（类型映射：forensics/misc/rev → 本轨 task_type）
