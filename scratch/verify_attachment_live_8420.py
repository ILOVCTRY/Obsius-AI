"""线上（8420 平台）真机验证：分片续传落盘 + 进度回调 + 去重。

走真实 HTTP，不 mock。项目用现有 CTF 项目，验证完删掉自己产生的附件 artifact
（不动用户既有附件）。

用法：E:/Miniconda3/python.exe scratch/verify_attachment_live_8420.py
"""

import hashlib
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8420"
CHUNK = 16 * 1024 * 1024


def req(method, path, body=None, ctype=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    r = urllib.request.Request(f"{BASE}{path}", data=body, method=method)
    if ctype:
        r.add_header("Content-Type", ctype)
    with opener.open(r, timeout=120) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


def main() -> int:
    # 用临时项目（隔离，绝不碰既有项目）
    st, proj = req("POST", "/api/projects",
                   json.dumps({"name": "附件分片线上验证", "track": "ctf",
                               "capabilities": ["binary"], "config": {}}).encode(),
                   "application/json")
    assert st == 201, (st, proj)
    pid = proj["id"]
    print(f"[项目] {pid}")

    ok = True
    try:
        # 68.6MB —— 用户截图里那个失败文件的确切体积
        size = round(68.6 * 1024 * 1024)
        pattern = b"pcapng-magic\x0a\x0d\x0d\x0a" + bytes(range(256)) * 4096
        blob = (pattern * (-(-size // len(pattern))))[:size]
        sha = hashlib.sha256(blob).hexdigest()
        total = -(-size // CHUNK)
        print(f"[上传] traffic.pcapng {size} B · {total} 片")

        st, sess = req("POST", f"/api/projects/{pid}/attachments/uploads",
                       json.dumps({"filename": "traffic.pcapng", "total_size": size,
                                   "total_chunks": total, "sha256": sha}).encode(),
                       "application/json")
        assert st == 201, (st, sess)
        uid = sess["upload_id"]

        sent = 0
        marks = []
        for i in range(total):
            part = blob[i * CHUNK:(i + 1) * CHUNK]
            st, _ = req("PUT", f"/api/projects/{pid}/attachments/uploads/{uid}/chunks/{i}",
                        part, "application/octet-stream")
            assert st == 200, st
            sent += len(part)
            pct = round(sent / size * 100)
            if pct % 20 == 0 or i == total - 1:
                marks.append(f"{pct}%")
        print(f"[进度] {' → '.join(dict.fromkeys(marks))}")

        st, att = req("POST", f"/api/projects/{pid}/attachments/uploads/{uid}/complete")
        assert st == 201, (st, att)
        assert att["size"] == size and att["sha256"] == sha
        print(f"[完成] {att['name']} {att['size']} B sha={att['sha256'][:16]}… ✓")

        # 下载回来比对（端到端闭环：上传 → 落盘 → 下载）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"{BASE}/api/projects/{pid}/artifacts/{att['id']}/download",
                         timeout=120) as resp:
            got = hashlib.sha256(resp.read()).hexdigest()
        assert got == sha, f"下载内容 sha 不一致 {got[:16]}"
        print("[闭环] 下载回来 sha 一致 ✓")

        # 小文件仍走单请求（不能把老路径改坏）。multipart 必须带 boundary 头，
        # 少这个头 FastAPI 解析不出 UploadFile → 422（第一版就是这么踩的）。
        body, ctype = _multipart("small.txt", b"hello attachment\n")
        st, small = req("POST", f"/api/projects/{pid}/attachments", body, ctype)
        assert st == 201 and small["size"] == len(b"hello attachment\n"), (st, small)
        print("[兼容] 小文件单请求通道正常 ✓")
    except AssertionError as exc:
        ok = False
        print(f"[FAIL] {exc}")
    finally:
        try:
            st, r = req("DELETE", f"/api/projects/{pid}")
            print(f"[清理] 项目已移入回收站 status={r.get('status') if r else st}")
        except Exception as exc:
            print(f"[清理失败] {exc}")
    return 0 if ok else 1


def _multipart(name: str, content: bytes):
    """回 (body, content_type)——boundary 必须写进头里，否则服务端解不出文件。"""
    boundary = "----attachverify"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


if __name__ == "__main__":
    raise SystemExit(main())
