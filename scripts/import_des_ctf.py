"""Import Des-CTF-Knowledge into the existing capability KB tree.

The source path is used only for maintenance provenance.  Runtime module
paths are organized by capability and resource type, without an upstream
repository label.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.skills.kbsearch import KbSearchIndex, default_index_path


TEXT_EXTS = {".md", ".py", ".txt", ".json", ".js", ".yml", ".yaml", ".dic", ".enc", ".dat"}


def _classify(text: str, name: str) -> str:
    value = (name + "\n" + text[:10000]).lower()
    groups = {
        "binary": ("pwn", "rop", "heap", "elf", "栈溢出", "堆利用", "逆向", "reverse", "ida", "汇编"),
        "crypto": ("rsa", "aes", "ecc", "密码学", "加密", "凯撒", "维吉尼亚", "crypto"),
        "forensics": ("取证", "volatility", "pcap", "流量分析", "内存镜像", "隐写", "stegan"),
        "misc": ("misc", "脑筋急转弯", "编码", "压缩包", "二维码", "音频隐写"),
        "web": ("sql", "xss", "ssrf", "ssti", "jwt", "命令执行", "文件上传", "文件包含",
                "反序列化", "php", "web", "注入", "rce"),
    }
    scores = {cap: sum(value.count(term) for term in terms) for cap, terms in groups.items()}
    return max(scores, key=scores.get) if max(scores.values()) else "misc"


def _files(source: Path):
    for path in sorted(source.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path.name.startswith("."):
            continue
        if path.suffix.lower() in TEXT_EXTS or path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            yield path


def _destination(source: Path, path: Path) -> tuple[str, Path]:
    rel = path.relative_to(source).as_posix()
    parts = path.relative_to(source).parts
    if parts and parts[0] == "工具使用":
        cap, base = "web", "refs/tools"
    elif parts and parts[0] == "CTF常用脚本及工具":
        cap, base = _classify(path.read_text(encoding="utf-8", errors="replace") if path.suffix.lower() in TEXT_EXTS else "", path.name), "refs/payloads"
    elif parts and parts[0] == "WP汇总":
        cap, base = _classify(path.read_text(encoding="utf-8", errors="replace") if path.suffix.lower() in TEXT_EXTS else "", path.name), "refs/lab-writeups"
    elif parts and parts[0] == "CTF大赛WP集合":
        text = path.read_text(encoding="utf-8", errors="replace") if path.suffix.lower() in TEXT_EXTS else ""
        cap, base = _classify(text, path.name), "refs/ctf-writeups"
    else:
        text = path.read_text(encoding="utf-8", errors="replace") if path.suffix.lower() in TEXT_EXTS else ""
        cap = _classify(text, path.name)
        core_names = {"sql.md", "命令执行.md", "文件包含.md", "文件上传漏洞.md",
                      "ssrf漏洞.md", "ssti.md", "jwt.md", "php代码审计.md",
                      "php反序列化漏洞总结.md", "图片隐写.md", "音频隐写.md", "压缩包总结.md"}
        base = "playbooks" if path.name.lower() in core_names else "refs/web-vulns"
    return cap, Path(base) / rel.replace("/", "__")


def import_tree(source: str | Path, packs: str | Path = "packs", *, force: bool = False) -> dict:
    source, packs = Path(source).resolve(), Path(packs).resolve()
    if not source.is_dir():
        raise ValueError(f"源目录不存在: {source}")
    copied: list[dict] = []
    for path in _files(source):
        cap, rel = _destination(source, path)
        target = packs / "kb" / cap / rel
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        raw = path.read_bytes()
        copied.append({"cap": cap, "path": rel.as_posix(), "source_path": path.relative_to(source).as_posix(),
                       "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    index = KbSearchIndex(packs)
    caps = sorted({x["cap"] for x in copied})
    if not caps:
        caps = [p.name for p in (packs / "kb").iterdir()
                if p.is_dir() and p.name != "licenses"] if (packs / "kb").is_dir() else []
    index.sync(caps)
    with sqlite3.connect(default_index_path(packs)) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS kb_provenance (
            path TEXT PRIMARY KEY, source_repo TEXT NOT NULL, source_path TEXT NOT NULL,
            license TEXT NOT NULL, sha256 TEXT NOT NULL, imported_at TEXT NOT NULL
        )""")
        stamp = datetime.now(timezone.utc).isoformat()
        provenance_rows = list(copied)
        if not provenance_rows:
            for path in _files(source):
                cap, rel = _destination(source, path)
                target = packs / "kb" / cap / rel
                if target.is_file():
                    raw = path.read_bytes()
                    provenance_rows.append({"cap": cap, "path": rel.as_posix(),
                                            "source_path": path.relative_to(source).as_posix(),
                                            "sha256": hashlib.sha256(raw).hexdigest()})
        for item in provenance_rows:
            con.execute("""INSERT OR REPLACE INTO kb_provenance
                (path,source_repo,source_path,license,sha256,imported_at)
                VALUES (?,?,?,?,?,?)""", (f"{item['cap']}/{item['path']}", "Des-CTF-Knowledge",
                item["source_path"], "MIT-or-upstream", item["sha256"], stamp))
    return {"copied": len(copied), "by_capability": {
        cap: sum(x["cap"] == cap for x in copied) for cap in sorted({x["cap"] for x in copied})}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?", default=r"开源优秀项目\CTF\Des-CTF-Knowledge-main")
    ap.add_argument("--packs", default="packs")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = import_tree(args.source, args.packs, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
