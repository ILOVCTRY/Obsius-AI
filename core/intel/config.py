"""情报源配置（E9/E10，DESIGN.md §16）：feeds.json（源清单，人可编）+ profile.json（兴趣画像+vault 配置）。

全局 JSON 存储，仿 packs/.proposals/ 范本；首次访问自动种子默认源。
load/save_profile 保留未知键（手编扩展字段不被抹掉）。
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
    "vault": {"path": "", "enabled": False},  # E10：Obsidian vault 只读接入
}

DEFAULT_VAULT = dict(DEFAULT_PROFILE["vault"])

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
              "kind": "rss"} for f in feeds if str(f.get("url", "").strip())]
    p = Path(intel_dir) / "feeds.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"feeds": clean}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return clean


def load_profile(intel_dir: str | Path = "config/intel") -> dict:
    """已知键归一化合并默认值，**其余键原样保留**（手编扩展字段不丢）。"""
    raw = _load(Path(intel_dir) / "profile.json", DEFAULT_PROFILE)
    dirs = {**DEFAULT_PROFILE["directions"], **(raw.get("directions") or {})}
    vault = {**DEFAULT_VAULT, **(raw.get("vault") or {})}
    out = {k: v for k, v in raw.items()
           if k not in ("directions", "stage", "vault")}  # 未知键透传
    out.update({"directions": dirs, "stage": str(raw.get("stage", "")),
                "vault": {"path": str(vault.get("path", "")),
                          "enabled": bool(vault.get("enabled", False))}})
    return out


def save_profile(profile: dict, intel_dir: str | Path = "config/intel") -> dict:
    dirs = {**DEFAULT_PROFILE["directions"], **(profile.get("directions") or {})}
    vault = {**DEFAULT_VAULT, **(profile.get("vault") or {})}
    clean = {k: v for k, v in profile.items()
             if k not in ("directions", "stage", "vault")}  # 未知键保留
    clean.update({
        "directions": {k: max(0.0, min(float(v), 5.0)) for k, v in dirs.items()},
        "stage": str(profile.get("stage", ""))[:100],
        "vault": {"path": str(vault.get("path", "")).strip()[:500],
                  "enabled": bool(vault.get("enabled", False))},
    })
    p = Path(intel_dir) / "profile.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean
