"""情报源配置（E9，DESIGN.md §16）：feeds.json（源清单，人可编）+ profile.json（兴趣画像）。

全局 JSON 存储，仿 packs/.proposals/ 范本；首次访问自动种子默认源。
"""

import json
from pathlib import Path

DEFAULT_FEEDS = [
    {"name": "安全客", "url": "https://api.anquanke.com/data/v1/rss", "kind": "rss"},
    {"name": "FreeBuf", "url": "https://www.freebuf.com/feed", "kind": "rss"},
    {"name": "看雪", "url": "https://www.kanxue.com/rss.php", "kind": "rss"},
    {"name": "先知社区", "url": "https://xz.aliyun.com/feed", "kind": "rss"},
    {"name": "PortSwigger Research", "url": "https://portswigger.net/blog/rss", "kind": "rss"},
    {"name": "arXiv cs.CR", "url": "https://rsshub.app/arxiv/cs.cr", "kind": "rss"},
]

# 七方向定稿（§16）：权重默认 1.0，stage 为学习阶段声明（E10 档案用）
DEFAULT_PROFILE = {
    "directions": {
        "web": 1.0, "ai": 1.0, "vehicle": 1.0, "reverse": 1.0,
        "android": 1.0, "pwn": 1.0, "forensics": 1.0,
    },
    "stage": "",
}

DIRECTIONS = tuple(DEFAULT_PROFILE["directions"])


def _load(path: Path, seed: dict) -> dict:
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(seed))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(seed))  # 坏文件兜底回种子（不覆写盘上文件）


def load_feeds(intel_dir: str | Path = "config/intel") -> list[dict]:
    return _load(Path(intel_dir) / "feeds.json", {"feeds": DEFAULT_FEEDS})["feeds"]


def save_feeds(feeds: list[dict], intel_dir: str | Path = "config/intel") -> list[dict]:
    clean = [{"name": str(f.get("name", "")).strip()[:50],
              "url": str(f.get("url", "")).strip()[:500],
              "kind": "rss"} for f in feeds if str(f.get("url", "")).strip()]
    p = Path(intel_dir) / "feeds.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"feeds": clean}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return clean


def load_profile(intel_dir: str | Path = "config/intel") -> dict:
    prof = _load(Path(intel_dir) / "profile.json", DEFAULT_PROFILE)
    # 补缺方向（profile.json 手编漏项时兜底）
    dirs = {**DEFAULT_PROFILE["directions"], **prof.get("directions", {})}
    return {"directions": dirs, "stage": str(prof.get("stage", ""))}


def save_profile(profile: dict, intel_dir: str | Path = "config/intel") -> dict:
    dirs = {**DEFAULT_PROFILE["directions"], **profile.get("directions", {})}
    clean = {"directions": {k: max(0.0, min(float(v), 5.0)) for k, v in dirs.items()},
             "stage": str(profile.get("stage", ""))[:100]}
    p = Path(intel_dir) / "profile.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean
