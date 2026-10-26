"""CTF 方向组 / 执行尝试 / 摘要批次的持久账本（ctf-direction-ledger，2026-10-25）。

**要解决的问题**（用户复盘现场）：`CtfSession.dirs`/`rounds` 只是 `ChatTurn`
实例内存。新一轮聊天新建 `ChatTurn` → 新建 `CtfSession` → 方向记账全空，
`ctf_review` 报「尚无方向记账」，而上一轮明明派过好几条；`rounds` 归零还会
让「无条件首轮分诊」闸**重新拦一次**深挖（复盘里 WP 深挖被拦就是这个）。

**为什么不复用现有两张表**：

- `intents` 的语义是「可证伪假设及其收尾（vuln/finding/dead_end）」，要求证据
  链；把「派了一条方向、它跑完了」硬塞进去，会让「发现」这个词贬值。而且
  `close_intent` 的收尾类型里没有「这轮执行失败/被人工删除」。
- `artex_intents` / 探索图是 ARTEX Planner 的任务生命周期。只有主控真的绑定了
  真实 ARTEX 任务时才该进那套系统（`runtime.py::_tool_call_expert` 已有这条
  判据），普通 CTF 派发旁路写进去会造出没有 Planner、没有目标的假任务。

**三层记录**：

1. **方向组**（`ctf_direction_groups`）——一个**逻辑解题方向**。同一方向重试、
   换专家、`ask_expert` 续跑都挂在同一组下，不重复计组。
2. **执行尝试**（`ctf_direction_runs`）——每次真实 spawn 一条：queued →
   running → completed/failed/cancelled，带子线程 id 与回执。
3. **摘要批次**（`ctf_summary_batches` + `ctf_summary_items`）——每新增
   `BATCH_SIZE` 个**终态**方向组形成一批。`ctf_summary_items.group_id` 上的
   唯一约束就是幂等键：同一组永远只能进一个批次，重复触发不会重复摘要。

**摘要只做投影**：它把相似方向归并成一段可读文字，**不删除、不合并**底层
方向组、执行尝试、线程消息、产物或 flag 台账。任何一条都能回溯到原文。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.blackboard.store import _row_to_dict, new_id, now

log = logging.getLogger(__name__)

# 方向组状态（与 `core/ctf/session.py` 的 STATUS_* 一一对应）。这里单独定义
# 是为了账本模块不反向依赖 session（session 要 import 它）。
GROUP_OPEN = "open"
GROUP_REQUEUED = "requeued"
GROUP_SOLVED = "solved"
GROUP_DROPPED = "dropped"
GROUP_RETIRED = "retired"

# **只有终态组**才计入「每新增 N 组触发一轮摘要」。open/requeued 仍可能续投，
# 把它们计入会让摘要切在半成品上。
TERMINAL_GROUP_STATUSES = frozenset({GROUP_SOLVED, GROUP_DROPPED, GROUP_RETIRED})

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

SUMMARY_PENDING = "pending"
SUMMARY_RUNNING = "running"
SUMMARY_COMPLETED = "completed"
SUMMARY_FAILED = "failed"

# 摘要触发粒度（用户裁决：不是固定 5 组就停，而是**每新增 5 组**触发一轮）。
BATCH_SIZE = 5

# 方向组进摘要时，回执展示长度上限（只影响摘要文本，不影响原文）
_SUMMARY_RECEIPT_CHARS = 240

# 摘要里「相似方向」的判据：**确定性集合相似度**，不引入语义模型。
#
# 为什么是 Jaccard 而不是「包含关系」：方向组之间不像死路判定那样要求单向包含
# （`session._shadow_of` 用它防复活），这里要的是「这两条讲的差不多」。
# 阈值 0.5 刻意偏高——宁可少合并，也不要把「文件上传」和「文件下载」并成一条。
_MERGE_JACCARD = 0.5


def _group_key(key: str, subgoal: str) -> str:
    """方向组的稳定键。

    归一键可能为空（抽不出实义词）——空串在多条方向上会互相顶掉（UNIQUE
    冲突），故对空键退化成原文摘要。**不做模糊归一**：键的语义由
    `core/ctf/lease.py::direction_key` 统一决定，这里只保证非空且稳定。
    """
    k = str(key or "").strip()
    if k:
        return k
    text = " ".join(str(subgoal or "").split())
    if not text:
        return "untitled"
    return "raw:" + str(abs(hash(text)) % (10 ** 12))


class CtfLedger:
    """某主控线程的 CTF 调度账本（读写都走黑板 SQLite）。

    线程维度是**必须**的：同一项目里两个 CTF 主控线程各解各的题，方向与批次
    不能串。`thread_id` 就是主控线程 id。
    """

    def __init__(self, bb, project_id: str, thread_id: str):
        self.bb = bb
        self.project_id = str(project_id or "")
        self.thread_id = str(thread_id or "")

    # ---------- 轮次（跨轮恢复分诊闸） ----------

    def rounds(self) -> int:
        row = self.bb.conn.execute(
            "SELECT rounds FROM ctf_session_state WHERE project_id=? AND thread_id=?",
            (self.project_id, self.thread_id)).fetchone()
        return int(row["rounds"]) if row else 0

    def set_rounds(self, n: int) -> None:
        value = max(0, int(n or 0))
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO ctf_session_state(project_id, thread_id, rounds, updated_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(project_id, thread_id) DO UPDATE SET"
                " rounds=excluded.rounds, updated_at=excluded.updated_at",
                (self.project_id, self.thread_id, value, now()))

    # ---------- 方向组 ----------

    def ensure_group(self, *, subgoal: str, key: str = "", expert: str = "",
                     need: int | None = None, status: str = GROUP_OPEN,
                     first_at: float = 0.0) -> dict:
        """按 (project, thread, 归一键) upsert 一个方向组，返回整行。

        **不覆盖已有状态**：同一方向重派时只补专家/need，状态与计数留在原行
        ——否则「重试一次就把已判死的方向复活」。
        """
        gkey = _group_key(key, subgoal)
        existing = self.get_group_by_key(gkey)
        if existing is not None:
            fields: list[str] = []
            args: list[Any] = []
            if expert and expert != existing.get("expert"):
                fields.append("expert=?")
                args.append(expert)
            if need is not None and need != existing.get("need"):
                fields.append("need=?")
                args.append(int(need))
            if fields:
                fields.append("updated_at=?")
                args.extend([now(), existing["id"]])
                with self.bb._tx():
                    self.bb.conn.execute(
                        f"UPDATE ctf_direction_groups SET {', '.join(fields)} WHERE id=?",
                        args)
                return self.get_group(existing["id"]) or existing
            return existing
        gid = new_id("ctfg")
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO ctf_direction_groups("
                " id, project_id, thread_id, challenge_id, key, subgoal, expert,"
                " need, status, terminal, passes, correct, flags_got, last_summary,"
                " first_epoch, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,0,0,0,'',?,?,?)",
                (gid, self.project_id, self.thread_id, self.challenge_id(),
                 gkey, str(subgoal or "").strip(), str(expert or ""),
                 int(need) if need else None, status,
                 1 if status in TERMINAL_GROUP_STATUSES else 0,
                 float(first_at or 0.0), now(), now()))
        return self.get_group(gid) or {"id": gid, "key": gkey, "subgoal": subgoal}

    def challenge_id(self) -> str:
        """本主控线程绑定的题目（''=未绑定）。账本按题目归档，跨题不混。"""
        try:
            row = self.bb.conn.execute(
                "SELECT challenge_asset_id FROM chat_threads WHERE id=?",
                (self.thread_id,)).fetchone()
            return str((row["challenge_asset_id"] if row else "") or "")
        except Exception:  # noqa: BLE001 —— 归属读不到不该阻断记账
            return ""

    def get_group(self, group_id: str) -> dict | None:
        row = self.bb.conn.execute(
            "SELECT * FROM ctf_direction_groups WHERE project_id=? AND id=?",
            (self.project_id, str(group_id or ""))).fetchone()
        return _row_to_dict(row) if row else None

    def get_group_by_key(self, key: str) -> dict | None:
        row = self.bb.conn.execute(
            "SELECT * FROM ctf_direction_groups"
            " WHERE project_id=? AND thread_id=? AND key=?",
            (self.project_id, self.thread_id, str(key or ""))).fetchone()
        return _row_to_dict(row) if row else None

    def list_groups(self) -> list[dict]:
        rows = self.bb.conn.execute(
            "SELECT * FROM ctf_direction_groups"
            " WHERE project_id=? AND thread_id=? ORDER BY rowid ASC",
            (self.project_id, self.thread_id)).fetchall()
        return [dict(r) for r in rows]

    def update_group(self, group_id: str, **fields) -> None:
        """更新方向组的记账字段。``status`` 变化会自动同步 ``terminal`` 位。"""
        if not fields:
            return
        if "status" in fields:
            fields["terminal"] = (1 if str(fields["status"]) in TERMINAL_GROUP_STATUSES
                                  else 0)
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        args = list(fields.values()) + [str(group_id)]
        with self.bb._tx():
            self.bb.conn.execute(
                f"UPDATE ctf_direction_groups SET {cols} WHERE id=?", args)

    def sync_from_state(self, st) -> dict | None:
        """把 `DirectionState` 的当前值写回账本（**唯一写入口**）。

        记账失败不抛：调度是主流程，账本坏了不该让主控停摆。
        """
        try:
            key = getattr(st, "key", "") or ""
            group = self.get_group_by_key(_group_key(key, getattr(st, "subgoal", "")))
            if group is None:
                group = self.ensure_group(
                    subgoal=getattr(st, "subgoal", ""), key=key,
                    expert=getattr(st, "expert", ""),
                    need=getattr(st, "need", None),
                    first_at=getattr(st, "first_at", 0.0) or 0.0)
            self.update_group(
                group["id"],
                subgoal=str(getattr(st, "subgoal", "") or ""),
                expert=str(getattr(st, "expert", "") or ""),
                status=str(getattr(st, "status", GROUP_OPEN) or GROUP_OPEN),
                passes=int(getattr(st, "passes", 0) or 0),
                correct=int(getattr(st, "correct", 0) or 0),
                flags_got=int(getattr(st, "flags_got", 0) or 0),
                last_summary=str(getattr(st, "last_summary", "") or ""))
            return self.get_group(group["id"])
        except Exception:  # noqa: BLE001 —— 账本故障不阻断调度
            log.exception("ctf 方向组同步失败 key=%s", getattr(st, "key", ""))
            return None

    # ---------- 执行尝试 ----------

    def start_run(self, group_id: str, *, expert: str = "", task: str = "") -> str:
        """登记一次执行尝试（queued）。真正 spawn 之后用 `attach_run` 置 running。

        **queued 与 running 分开**：`call_expert` 里 spawn 可能抛错（专家 yaml
        缺失、线程构造失败）。只有 queued 记录时失败路径能明确标 failed，不必
        假装它跑过。
        """
        rid = new_id("ctfr")
        with self.bb._tx():
            self.bb.conn.execute(
                "INSERT INTO ctf_direction_runs("
                " id, project_id, thread_id, group_id, sub_thread_id, expert, task,"
                " status, result, error, started_at, finished_at, created_at)"
                " VALUES(?,?,?,?,'',?,?,?,'','',NULL,NULL,?)",
                (rid, self.project_id, self.thread_id, str(group_id),
                 str(expert or ""), str(task or "")[:4000], RUN_QUEUED, now()))
        return rid

    def attach_run(self, run_id: str, sub_thread_id: str) -> None:
        """把已 spawn 的子线程挂到这次尝试上，并置 running。"""
        if not run_id:
            return
        with self.bb._tx():
            self.bb.conn.execute(
                "UPDATE ctf_direction_runs SET sub_thread_id=?, status=?,"
                " started_at=COALESCE(started_at, ?) WHERE id=?",
                (str(sub_thread_id or ""), RUN_RUNNING, now(), run_id))

    def finish_run(self, run_id: str, status: str, *, result: str = "",
                   error: str = "") -> None:
        if not run_id:
            return
        if status not in (RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED):
            raise ValueError(f"非法执行终态: {status}")
        with self.bb._tx():
            self.bb.conn.execute(
                "UPDATE ctf_direction_runs SET status=?, result=?, error=?,"
                " finished_at=? WHERE id=?",
                (status, str(result or "")[:20000], str(error or "")[:2000],
                 now(), run_id))

    def runs_of(self, group_id: str) -> list[dict]:
        rows = self.bb.conn.execute(
            "SELECT * FROM ctf_direction_runs WHERE project_id=? AND group_id=?"
            " ORDER BY rowid ASC", (self.project_id, str(group_id))).fetchall()
        return [dict(r) for r in rows]

    def group_for_sub_thread(self, sub_thread_id: str) -> dict | None:
        """子线程 → 它所属的方向组（`ask_expert` 续跑要挂回原组）。"""
        row = self.bb.conn.execute(
            "SELECT g.* FROM ctf_direction_runs r"
            " JOIN ctf_direction_groups g ON g.id = r.group_id"
            " WHERE r.project_id=? AND r.sub_thread_id=?"
            " ORDER BY r.rowid DESC LIMIT 1",
            (self.project_id, str(sub_thread_id or ""))).fetchone()
        return _row_to_dict(row) if row else None

    # ---------- 摘要批次 ----------

    def pending_terminal_groups(self) -> list[dict]:
        """**未被任何批次收进去的**终态方向组（按创建顺序）。"""
        rows = self.bb.conn.execute(
            "SELECT g.* FROM ctf_direction_groups g"
            " LEFT JOIN ctf_summary_items i ON i.group_id = g.id"
            " WHERE g.project_id=? AND g.thread_id=? AND g.terminal=1"
            "   AND i.group_id IS NULL ORDER BY g.rowid ASC",
            (self.project_id, self.thread_id)).fetchall()
        return [dict(r) for r in rows]

    def ensure_batches(self) -> list[dict]:
        """每攒够 `BATCH_SIZE` 个未入批的终态组，原子地切一个批次。

        一次调用可能切出**多个**批次（跨轮补账时一次攒了 10 组 → 两个批次），
        每批独立可重试。

        **并发安全**：整段在一个 `BEGIN IMMEDIATE` 事务里；`ctf_summary_items`
        对 `group_id` 有 UNIQUE 约束，即使两个线程同时判断「够 5 个」，也会有
        一方在插入时冲突回滚，不会把同一组切进两批。
        """
        created: list[dict] = []
        while True:
            try:
                batch = self._slice_one_batch()
            except Exception:  # noqa: BLE001 —— 切批失败不阻断调度
                log.exception("ctf 摘要批次创建失败 thread=%s", self.thread_id)
                return created
            if batch is None:
                return created
            created.append(batch)

    def _slice_one_batch(self) -> dict | None:
        with self.bb._tx():
            rows = self.bb.conn.execute(
                "SELECT g.* FROM ctf_direction_groups g"
                " LEFT JOIN ctf_summary_items i ON i.group_id = g.id"
                " WHERE g.project_id=? AND g.thread_id=? AND g.terminal=1"
                "   AND i.group_id IS NULL ORDER BY g.rowid ASC LIMIT ?",
                (self.project_id, self.thread_id, BATCH_SIZE)).fetchall()
            if len(rows) < BATCH_SIZE:
                return None
            seq_row = self.bb.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM ctf_summary_batches"
                " WHERE project_id=? AND thread_id=?",
                (self.project_id, self.thread_id)).fetchone()
            seq = int(seq_row["m"]) + 1
            bid = new_id("ctfb")
            ids = [str(r["id"]) for r in rows]
            self.bb.conn.execute(
                "INSERT INTO ctf_summary_batches("
                " id, project_id, thread_id, challenge_id, seq, status,"
                " group_ids, summary, error, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?, '', '', ?, ?)",
                (bid, self.project_id, self.thread_id, self.challenge_id(), seq,
                 SUMMARY_PENDING, json.dumps(ids), now(), now()))
            for gid in ids:
                self.bb.conn.execute(
                    "INSERT INTO ctf_summary_items(batch_id, group_id, project_id)"
                    " VALUES(?,?,?)", (bid, gid, self.project_id))
        return {"id": bid, "seq": seq, "group_ids": ids, "status": SUMMARY_PENDING}

    def list_batches(self) -> list[dict]:
        rows = self.bb.conn.execute(
            "SELECT * FROM ctf_summary_batches"
            " WHERE project_id=? AND thread_id=? ORDER BY seq ASC",
            (self.project_id, self.thread_id)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["group_ids"] = json.loads(d.get("group_ids") or "[]")
            except (TypeError, ValueError):
                d["group_ids"] = []
            out.append(d)
        return out

    def run_pending_batches(self) -> list[dict]:
        """把 pending 批次逐个生成摘要；单批失败只标记该批，**不推进水位**。

        「水位」在这里不需要单独的计数器：`ctf_summary_items` 已经把「哪些组
        已经被处理过」钉死。失败批次保持 `failed`，组仍算「已入批」（不重复
        切批），由 `retry_failed_batches()` 或人工重跑——不能因为摘要失败就
        把方向组重新切一遍（那会造出重复批次）。
        """
        done: list[dict] = []
        for batch in self.list_batches():
            if batch.get("status") != SUMMARY_PENDING:
                continue
            done.append(self.process_batch(batch["id"]))
        return done

    def process_batch(self, batch_id: str) -> dict:
        """生成单个批次的摘要（幂等：已完成批次直接返回，不重算）。"""
        batch = self._get_batch(batch_id)
        if batch is None:
            return {"id": batch_id, "status": "missing"}
        if batch.get("status") == SUMMARY_COMPLETED:
            return batch
        self._set_batch(batch_id, status=SUMMARY_RUNNING, error="")
        try:
            groups = [g for g in (self.get_group(gid)
                                  for gid in batch.get("group_ids") or []) if g]
            text = summarize_groups(groups, seq=batch.get("seq") or 0)
            self._set_batch(batch_id, status=SUMMARY_COMPLETED, summary=text, error="")
        except Exception as e:  # noqa: BLE001 —— 摘要失败留痕，不抛给调度
            log.exception("ctf 摘要批次生成失败 id=%s", batch_id)
            self._set_batch(batch_id, status=SUMMARY_FAILED, error=str(e))
        return self._get_batch(batch_id) or {"id": batch_id}

    def retry_failed_batches(self) -> list[dict]:
        out = []
        for b in self.list_batches():
            if b.get("status") == SUMMARY_FAILED:
                self._set_batch(b["id"], status=SUMMARY_PENDING, error="")
                out.append(self.process_batch(b["id"]))
        return out

    def _get_batch(self, batch_id: str) -> dict | None:
        row = self.bb.conn.execute(
            "SELECT * FROM ctf_summary_batches WHERE project_id=? AND id=?",
            (self.project_id, str(batch_id))).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["group_ids"] = json.loads(d.get("group_ids") or "[]")
        except (TypeError, ValueError):
            d["group_ids"] = []
        return d

    def _set_batch(self, batch_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        args = list(fields.values()) + [str(batch_id)]
        with self.bb._tx():
            self.bb.conn.execute(
                f"UPDATE ctf_summary_batches SET {cols} WHERE id=?", args)

    # ---------- 复核视图 ----------

    def review_lines(self, *, limit: int = 3) -> list[str]:
        """给 `ctf_review` 追加的段落（无内容时返回空列表，不塞空壳）。

        **不含任何判据**：账本只描述「派过什么、跑了几次、摘要说了什么」，
        放弃/续投仍由 `CtfSession` 的 scheduler 决定。
        """
        lines: list[str] = []
        try:
            groups = self.list_groups()
        except Exception:  # noqa: BLE001
            return lines
        if not groups:
            return lines
        # 一条聚合查询算执行次数：方向的组数在真实题目上可达几十个，逐个
        # `runs_of()` 会变成 N+1，而复核是每轮都调的。
        try:
            row = self.bb.conn.execute(
                "SELECT COUNT(*) AS n FROM ctf_direction_runs"
                " WHERE project_id=? AND thread_id=?",
                (self.project_id, self.thread_id)).fetchone()
            runs = int(row["n"]) if row else 0
        except Exception:  # noqa: BLE001
            runs = 0
        terminal = sum(1 for g in groups if g.get("terminal"))
        lines.append("")
        lines.append("### 持久账本（跨轮可恢复；上一轮的派发不会因为新开一轮而消失）")
        lines.append(f"- 方向组 {len(groups)} 个（终态 {terminal}）、"
                     f"执行尝试 {runs} 次；状态与回执已落库。")
        try:
            batches = self.list_batches()
        except Exception:  # noqa: BLE001
            batches = []
        if batches:
            for b in batches[-limit:]:
                ids = "、".join(str(x)[-6:] for x in b.get("group_ids") or [])
                lines.append(f"- 摘要批次 #{b.get('seq')}（{b.get('status')}）："
                             f"覆盖组 …{ids}")
            last = batches[-1]
            if last.get("status") == SUMMARY_COMPLETED and last.get("summary"):
                lines.append("")
                lines.append("#### 最近一轮压缩摘要（**投影，不替代原文**；"
                             "要看原始回执用 ask_expert）")
                lines.append(str(last["summary"])[:2000])
            elif last.get("status") == SUMMARY_FAILED:
                lines.append(f"- 最近批次摘要失败：{last.get('error') or '未知错误'}"
                             "（原始方向组与回执未受影响）")
        else:
            pending = len(self.pending_terminal_groups())
            lines.append(f"- 摘要进度：{pending}/{BATCH_SIZE}"
                         f"（每新增 {BATCH_SIZE} 个终态方向组触发一轮）")
        return lines


# ---------- 摘要生成（纯函数，可单测） ----------


def _tokens(text: str) -> set[str]:
    """方向组 → 比较用 token 集合。

    直接用账本里的归一键（`direction_key` 产物）切词：它已经剥过动作词、
    做过同义归一，比原文分词稳定得多。
    """
    return {t for t in str(text or "").split() if t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _clusters(groups: list[dict]) -> list[list[dict]]:
    """按 token Jaccard 做确定性聚类（并查集，不依赖顺序随机性）。"""
    toks = [_tokens(g.get("key") or "") for g in groups]
    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            if _jaccard(toks[i], toks[j]) >= _MERGE_JACCARD:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    buckets: dict[int, list[dict]] = {}
    for i, g in enumerate(groups):
        buckets.setdefault(find(i), []).append(g)
    return list(buckets.values())


def summarize_groups(groups: list[dict], *, seq: int = 0) -> str:
    """把一批方向组压成可读摘要（**纯投影**，不写库、不改原始记录）。

    输出包含三部分：逐组一行事实、相似组归类、以及「怎么用这份摘要」的口径
    说明。**不写判据性结论**（如「这条方向该放弃」）——那是 scheduler 的活，
    摘要里出现判断会让模型把压缩文本当成事实依据。
    """
    if not groups:
        return f"（批次 #{seq} 无可摘要的方向组）"
    lines = [f"**批次 #{seq}**：{len(groups)} 个终态方向组"]
    for g in groups:
        receipt = str(g.get("last_summary") or "").replace("\n", " ").strip()
        if len(receipt) > _SUMMARY_RECEIPT_CHARS:
            receipt = receipt[:_SUMMARY_RECEIPT_CHARS] + "…"
        stats = f"执行 {g.get('passes') or 0} 次"
        if g.get("flags_got"):
            stats += f"、flag {g['flags_got']}"
        lines.append(f"- [{g.get('status')}] {str(g.get('subgoal') or '')[:120]}"
                     f"（{stats}）" + (f"：{receipt}" if receipt else ""))
    clusters = [c for c in _clusters(groups) if len(c) > 1]
    if clusters:
        lines.append("")
        lines.append("**相似方向（仅归类，原始组与回执都保留）**：")
        for c in clusters:
            names = "；".join(str(x.get("subgoal") or "")[:60] for x in c)
            statuses = "、".join(sorted({str(x.get("status") or "") for x in c}))
            lines.append(f"- {len(c)} 条（{statuses}）：{names}")
    lines.append("")
    lines.append("**使用口径**：本摘要只是压缩投影，不是新事实；引用具体命令/偏移/"
                 "flag 时回到原线程（ask_expert）或 flag 台账核对。")
    return "\n".join(lines)
