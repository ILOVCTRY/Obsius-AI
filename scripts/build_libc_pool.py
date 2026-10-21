#!/usr/bin/env python3
"""build_libc_pool.py —— 把主流 glibc 版本预置进 pentest-box 镜像（离线可用）。

## 为什么要做

CTF pwn 题几乎都要「认 libc 版本 → 取 system/str_bin_sh 偏移」。原方案是让 agent
在容器里现场下载，实测有两个致命问题：

1. **跨境链路慢且不可靠**：libc.rip 下载实测 10~20KB/s，2MB 要 2~3 轮续传；
   github.com 直接不通（TCP 443 超时）。agent 为此烧掉过 4~5 轮工具调用。
2. **静默残缺**：`curl -m 60` 超时后若把超时调大，exit=0 但文件只有 818931 字节
   （正常 1.9MB），直到 pwntools 解析才炸 `ELFParseError`。

所以把「主流版本的 libc.so.6」直接烘进镜像，解题时**零网络**查表。

## 数据来源（两条，互为补充）

- **Ubuntu**：`archive.ubuntu.com` / `old-releases.ubuntu.com` / `security.ubuntu.com`
  的 `pool/main/g/glibc/` 目录列表。覆盖 2.3 → 2.44，含大量补丁版（-0ubuntuX.Y）。
  实测归档站比 libc.rip 快一个数量级（2.5MB / 6~11s）。
- **Debian**：`deb.debian.org/debian/pool/main/g/glibc/`，主池只有 bookworm 起的
  几个版本（2.31/2.36/2.41/2.43/2.44），但 CTF 里 Debian 靶机同样常见。

**版本号一律从目录列表解析，绝不猜**：猜 `libc6_2.31-0ubuntu9.9` 会 404，
实际盘上是 `-0ubuntu9` 和 `-0ubuntu9.18`。

## 提取要点（踩过的坑）

- `dpkg-deb -x` 解包后取 `lib/x86_64-linux-gnu/libc.so.6`。
- **2.34 及以前它是软链** → `libc-2.31.so`，`find -name "libc-*.so"` 能拿到；
  **2.35 起改成实体文件**，`find -name "libc-*.so"` 会**返回空**（本轮踩过）。
  正确做法是 `find -name "libc.so.6"` 再 `realpath` 跟随软链。
- 只保留 `libc.so.6` 本体（1.7~2.3MB），deb 解包后立刻删，不进镜像。

## 输出布局

    /opt/libc-db/<glibc版本>/libc.so.6        例：/opt/libc-db/2.31/libc.so.6
    /opt/libc-db/<glibc版本>/libc-2.31-0ubuntu9.so   带完整发行版号的副本（同名软链）
    /opt/libc-db/index.tsv                    版本 / buildid / 大小 / 来源
    /opt/libc-db/INDEX.md                     人读清单

同一 glibc 主版本有多个补丁版时（如 2.31 有 -0ubuntu9 和 -0ubuntu9.18），
**目录名用主版本、文件名带完整发行版号**，并把「基础版」（最短的发行版号）
作为 `libc.so.6` 软链目标——因为 CTF 题最常配的就是发行版初始版本。

用法：
    python scripts/build_libc_pool.py                 # 全量拉取到 tools/pentest-box/libc-db/
    python scripts/build_libc_pool.py --glibc 2.23 2.27 2.31 2.35 2.39
    python scripts/build_libc_pool.py --debian-only
    python scripts/build_libc_pool.py --list          # 只列源上有什么，不下载
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "pentest-box" / "libc-db"

UBUNTU_MIRRORS = (
    "http://archive.ubuntu.com/ubuntu",
    "http://security.ubuntu.com/ubuntu",
    "http://old-releases.ubuntu.com/ubuntu",
)
DEBIAN_MIRRORS = ("https://deb.debian.org/debian",)

# 只留 2.19 以后的版本：2.3~2.9 是 2005 年前的东西，CTF 里基本不出现，
# 且这些包里的 libc 还是 i386 时代的布局，提取后对现代 pwntools 意义有限。
MIN_MINOR = 19

_RX_DEB = re.compile(r"(libc6_[0-9][^\"']*_amd64\.deb)")
_RX_VER = re.compile(r"^libc6_([0-9][^-]*)-([^_]*)_amd64\.deb$")


def _get(url: str, timeout: int = 60) -> bytes | None:
    try:
        req = Request(url, headers={"User-Agent": "cyberstrike-libc-pool/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001 —— 单个镜像失败不该终止整体
        return None


def discover() -> list[dict]:
    """扫所有镜像的 pool 目录，返回 [{distro, ver, rel, url}]，按 glibc 版本去重。"""
    found: dict[tuple[str, str], dict] = {}
    for distro, mirrors in (("ubuntu", UBUNTU_MIRRORS), ("debian", DEBIAN_MIRRORS)):
        for base in mirrors:
            html = _get(f"{base}/pool/main/g/glibc/", timeout=90)
            if not html:
                print(f"  [!] 目录列表拿不到: {base}", file=sys.stderr)
                continue
            text = html.decode("utf-8", "replace")
            n = 0
            for m in _RX_DEB.finditer(text):
                fn = m.group(1)
                vm = _RX_VER.match(fn)
                if not vm:
                    continue
                ver, rel = vm.group(1), vm.group(2)
                try:
                    minor = int(ver.split(".")[1])
                except (IndexError, ValueError):
                    continue
                if minor < MIN_MINOR:
                    continue
                key = (distro, ver + "-" + rel)
                if key in found:
                    continue
                found[key] = {
                    "distro": distro, "ver": ver, "rel": rel,
                    "filename": fn, "url": f"{base}/pool/main/g/glibc/{fn}",
                }
                n += 1
            print(f"  {distro:6s} {base:46s} +{n}")
    return sorted(found.values(),
                  key=lambda d: (_vkey(d["ver"]), d["distro"], d["rel"]))


def _vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", v))


def _pick_base(cands: list[dict]) -> dict:
    """同 glibc 版本多个补丁版时，选「基础版」：发行版号最短的那个。

    CTF 靶机多数跑发行版初始 glibc（`2.31-0ubuntu9`），而不是打满补丁的
    `-0ubuntu9.18`。两者符号偏移一致，但用基础版当 `libc.so.6` 更符合预期。
    """
    return min(cands, key=lambda d: (len(d["rel"]), d["rel"]))


def _extract(deb: Path, dest_dir: Path, want_name: str) -> tuple[bool, str]:
    """解包 deb 并取出 libc.so.6（跟随软链）。返回 (ok, buildid)。"""
    # **每个包一个独立临时目录**：曾用固定的 dest/_x，并发时两个包互相
    # rmtree → `OSError: Directory not empty: 'gconv'`（deb 里 gconv 子目录多，
    # 删除期间另一个线程正在写）。用 mkdtemp 彻底隔离。
    tmp = Path(tempfile.mkdtemp(prefix="_x", dir=str(dest_dir)))
    try:
        r = subprocess.run(["dpkg-deb", "-x", str(deb), str(tmp)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"dpkg-deb 失败: {(r.stderr or '').strip()[:120]}"
        # **必须找 libc.so.6**：2.35+ 它是实体文件，找 libc-*.so 会落空
        hits = list(tmp.rglob("libc.so.6"))
        if not hits:
            return False, "包里没有 libc.so.6"
        src = hits[0]
        real = src.resolve() if src.is_symlink() else src
        if not real.is_file():
            return False, "libc.so.6 指向的文件不存在"
        # 完整性：readelf 的 rc 不可靠（截断也返回 0），必须看 stderr
        chk = subprocess.run(["readelf", "-S", str(real)],
                             capture_output=True, text=True)
        if (chk.stderr or "").strip():
            return False, f"ELF 不完整: {(chk.stderr or '').strip().splitlines()[0]}"
        shutil.copy2(real, dest_dir / want_name)
        buildid = ""
        bi = subprocess.run(["readelf", "-n", str(real)],
                            capture_output=True, text=True)
        m = re.search(r"Build ID:\s*([0-9a-f]+)", bi.stdout or "")
        if m:
            buildid = m.group(1)
        return True, buildid
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fetch_one(item: dict) -> dict:
    """下载一个 deb → 提取 libc.so.6 → 落 /opt 布局的宿主目录。"""
    gdir = OUT / item["ver"]
    gdir.mkdir(parents=True, exist_ok=True)
    name = f"libc-{item['ver']}-{item['rel']}.so"
    target = gdir / name
    if target.is_file() and target.stat().st_size > 1 << 20:
        return {**item, "ok": True, "skipped": True,
                "size": target.stat().st_size, "buildid": ""}
    with tempfile.TemporaryDirectory() as td:
        deb = Path(td) / "p.deb"
        blob = _get(item["url"], timeout=180)
        if not blob or len(blob) < 100_000:
            return {**item, "ok": False, "err": f"下载失败/过小({len(blob or b'')}B)"}
        deb.write_bytes(blob)
        ok, info = _extract(deb, gdir, name)
        if not ok:
            return {**item, "ok": False, "err": info}
    return {**item, "ok": True, "size": target.stat().st_size, "buildid": info}


def write_index(rows: list[dict]) -> None:
    """写 index.tsv + INDEX.md + base.txt（每个版本的基础版文件名）。

    **不在这里建 `libc.so.6` 软链**（2026-10-20 踩坑）：宿主侧建好的软链会让
    `docker build` 直接失败 —— `ERROR: invalid file request libc-db/2.19/libc.so.6`
    （BuildKit 在 Windows 上下文里不接受符号链接条目）。改由 Dockerfile 的
    RUN 在镜像内 `ln -s`，宿主目录只放实体 .so 文件 + base.txt 清单。
    """
    ok_rows = [r for r in rows if r.get("ok")]
    by_ver: dict[str, list[dict]] = {}
    for r in ok_rows:
        by_ver.setdefault(r["ver"], []).append(r)
    lines = ["glibc\tdistro\trelease\tbuildid\tsize\tfile"]
    base_lines = []
    for ver in sorted(by_ver, key=_vkey):
        cands = sorted(by_ver[ver], key=lambda d: (d["distro"], len(d["rel"]), d["rel"]))
        base = _pick_base(cands)
        base_lines.append(f"{ver}\tlibc-{base['ver']}-{base['rel']}.so")
        for c in cands:
            lines.append(f"{ver}\t{c['distro']}\t{c['rel']}\t{c.get('buildid','')}"
                         f"\t{c.get('size',0)}\tlibc-{c['ver']}-{c['rel']}.so")
    (OUT / "index.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Dockerfile 用这个文件建 libc.so.6 软链（镜像内建，避开 BuildKit 限制）
    (OUT / "base.txt").write_text("\n".join(base_lines) + "\n", encoding="utf-8")

    md = ["# 预置 libc 清单（libc-db）", "",
          "容器内路径：`/opt/libc-db/<glibc版本>/libc.so.6`。",
          "查偏移优先用 `libc-db <版本>`（包装脚本），别手搓 pwntools。", "",
          "| glibc | 版本数 | 发行版 | 基础版文件 |", "|---|---|---|---|"]
    for ver in sorted(by_ver, key=_vkey):
        cands = sorted(by_ver[ver], key=lambda d: (d["distro"], len(d["rel"]), d["rel"]))
        base = _pick_base(cands)
        distros = "/".join(sorted({c["distro"] for c in cands}))
        md.append(f"| {ver} | {len(cands)} | {distros} | "
                  f"`libc-{base['ver']}-{base['rel']}.so` |")
    (OUT / "INDEX.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="预置多版本 libc 到 pentest-box 镜像")
    ap.add_argument("--glibc", nargs="*", default=None,
                    help="只处理这些 glibc 版本（如 2.23 2.31 2.35），缺省=全部")
    ap.add_argument("--distro", choices=("all", "ubuntu", "debian"), default="all")
    ap.add_argument("--list", action="store_true", help="只列源上有哪些，不下载")
    ap.add_argument("--jobs", type=int, default=6, help="并发下载数（默认 6）")
    ap.add_argument("--max-per-version", type=int, default=3,
                    help="每个 glibc 版本最多留几个补丁版（默认 3，0=不限）")
    args = ap.parse_args()

    if shutil.which("dpkg-deb") is None:
        print("[!] 需要 dpkg-deb。Windows 宿主上请在容器内跑：")
        print("    docker run --rm -v $PWD:/w -w /w cyberstrike/pentest-box:0.2 \\")
        print("      python3 scripts/build_libc_pool.py")
        return 3

    print("[i] 扫描镜像源目录列表 …")
    items = discover()
    if args.distro != "all":
        items = [d for d in items if d["distro"] == args.distro]
    if args.glibc:
        want = set(args.glibc)
        items = [d for d in items if d["ver"] in want]
    if args.max_per_version > 0:
        # 每版留「基础版 + 最新的几个补丁版」：基础版对上靶机，补丁版覆盖安全更新过的
        grouped: dict[tuple[str, str], list[dict]] = {}
        for d in items:
            grouped.setdefault((d["distro"], d["ver"]), []).append(d)
        kept: list[dict] = []
        for key, group in grouped.items():
            group.sort(key=lambda d: (len(d["rel"]), d["rel"]))
            kept.extend(group[: args.max_per_version])
        items = kept

    by_ver: dict[str, int] = {}
    for d in items:
        by_ver[d["ver"]] = by_ver.get(d["ver"], 0) + 1
    print(f"[i] 待处理 {len(items)} 个包，覆盖 {len(by_ver)} 个 glibc 版本：")
    for v in sorted(by_ver, key=_vkey):
        print(f"      {v:6s} × {by_ver[v]}")
    if args.list:
        for d in items:
            print(f"  {d['ver']:6s} {d['distro']:6s} {d['rel']:22s} {d['url']}")
        return 0
    if not items:
        print("[!] 没有匹配的包（检查 --glibc 参数或网络）")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {ex.submit(fetch_one, d): d for d in items}
        for fut in as_completed(futs):
            r = fut.result()
            rows.append(r)
            done += 1
            tag = "skip" if r.get("skipped") else ("OK  " if r.get("ok") else "FAIL")
            extra = (f"{r.get('size', 0) / 1048576:.2f}MB" if r.get("ok")
                     else r.get("err", ""))
            print(f"  [{done}/{len(items)}] {tag} {r['ver']:6s} {r['rel']:22s} {extra}")

    write_index(rows)
    ok = sum(1 for r in rows if r.get("ok"))
    total = sum(r.get("size", 0) for r in rows if r.get("ok"))
    print(f"\n[i] 完成 {ok}/{len(rows)}，落盘 {total / 1048576:.1f}MB → {OUT}")
    print(f"[i] 清单：{OUT / 'INDEX.md'}")
    failed = [r for r in rows if not r.get("ok")]
    if failed:
        print(f"[!] {len(failed)} 个失败（多为源上已撤档，非致命）：")
        for r in failed[:10]:
            print(f"      {r['ver']} {r['rel']}: {r.get('err')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
