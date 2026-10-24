"""查 13 条 unknown 错误前是否伴随 chat.retry（连接类）。"""
import glob
import json
import sqlite3


def main():
    for p in glob.glob("workspaces/*/blackboard.db"):
        uri = "file:" + p.replace("\\", "/") + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True)
            ev = db.execute(
                "select id, kind, payload, created_at from events order by id"
            ).fetchall()
        except Exception:
            continue
        for i, (_eid, kind, payload, ts) in enumerate(ev):
            if kind != "chat.error":
                continue
            try:
                d = json.loads(payload)
            except Exception:
                continue
            if d.get("category") != "unknown":
                continue
            if "status" not in (d.get("message") or ""):
                continue
            window = [x for x in ev[max(0, i - 30):i] if x[1] == "chat.retry"]
            retries = [json.loads(x[2]) for x in window]
            sts = [r.get("status") for r in retries]
            print(f"{ts}  {p.split(chr(92))[-2]}  retries={len(retries)} "
                  f"statuses={sts}")


if __name__ == "__main__":
    main()
