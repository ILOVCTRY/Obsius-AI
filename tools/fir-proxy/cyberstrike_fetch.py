"""cyberstrike 代理池抓取 runner（平台侧薄包装）。

覆盖上游 ``ProxyFetcher`` 的源清单（**内置精选** + 可经 ``--sources`` 覆盖），其余抓取逻辑
复用上游 ``modules/fetcher.py``——**不改 fir-proxy 自身文件**（便于上游同步）。

为什么单独一个 runner：上游 ``cli.py fetch`` 的源清单硬编码在 ``ProxyFetcher`` 里（含已停服
的源与第三方自建 ``/fetch_all`` 口），无法配置。本 runner 只替换实例属性，不动上游代码。

用法（平台 ``core/proxy/pool.py::_run_cli`` 调用）::

    python cyberstrike_fetch.py [--protocol http --protocol socks5] \
        [--sources sources.json] [--limit 2000] -o out.json --output-format json --quiet

输出：``-o`` 指向的 JSON 数组 ``[{"protocol": ..., "proxy": "host:port"}, ...]``；
stdout 另发**进度行** ``{"event":"progress","phase":"fetch","done":k,"total":n}``（平台流式解析）。

独立进程运行（cwd 应为 fir-proxy 目录），**不 import 平台任何模块**。
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


# 内置精选源清单（仅覆盖「文本/JSON API 源」；网页爬虫见 DEFAULT_SCRAPERS）。
# 已剔除：openproxylist.xyz（已停服）、proxyscan.io（不稳）、以及 5 个第三方自建
# `:5000/:5010/fetch_all` 口（非权威、随时失效、由任意主机提供列表=供应链风险，
# 需 `--sources` 显式开启）。
DEFAULT_SOURCES: dict[str, list[str]] = {
    "http": [
        "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "https://www.proxy-list.download/api/v1/get?type=http",
        "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http",
    ],
    "https": [
        "https://www.proxy-list.download/api/v1/get?type=https",
    ],
    "socks4": [
        "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks4",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
        "https://www.proxy-list.download/api/v1/get?type=socks4",
    ],
    "socks5": [
        "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks5",
        "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "https://www.proxy-list.download/api/v1/get?type=socks5",
    ],
}

# 短名 → 上游 ProxyFetcher 的网页爬虫方法名（scraping_sources 按 ``func.__name__`` 过滤）。
SCRAPER_KEYS: dict[str, str] = {
    "free_proxy_list": "_scrape_free_proxy_list",
    "kxdaili": "_scrape_kxdaili",
    "66ip": "_scrape_66ip",
    "fatezero": "_scrape_fatezero",
    "kuaidaili": "_scrape_kuaidaili",
    "ip3366": "_scrape_ip3366",
    "89ip": "_scrape_89ip",
}
# 默认启用的爬虫（剔除表结构易变的 free-proxy-list.net；保留国内源，本机网络下更易命中）。
DEFAULT_SCRAPERS: list[str] = ["kxdaili", "66ip", "fatezero", "kuaidaili", "ip3366", "89ip"]


def build_sources(config: dict | None) -> tuple[dict[str, list[str]], list[str]]:
    """合并源清单配置 → ``(online_sources, scraper 短名列表)``。

    ``config`` 为 None 或缺键时用内置默认；``api`` 段按协议覆盖（该协议缺省则清空该协议
    的源，便于只跑某几类），``scrapers`` 段整体替换（``[]`` = 不跑爬虫）。
    """
    sources = {proto: list(urls) for proto, urls in DEFAULT_SOURCES.items()}
    scrapers = list(DEFAULT_SCRAPERS)
    if not isinstance(config, dict):
        return sources, scrapers
    api = config.get("api")
    if isinstance(api, dict):
        for proto, urls in api.items():
            if proto not in sources:
                continue
            sources[proto] = [str(u) for u in urls] if isinstance(urls, list) else []
    if isinstance(config.get("scrapers"), list):
        scrapers = [str(name) for name in config["scrapers"]]
    return sources, scrapers


def _load_sources(path: str | None) -> tuple[dict[str, list[str]], list[str]]:
    if not path:
        return build_sources(None)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[!] 源清单读取失败（改用内置默认）: {exc}", file=sys.stderr, flush=True)
        return build_sources(None)
    return build_sources(data if isinstance(data, dict) else None)


def _apply(fetcher, sources: dict[str, list[str]], scrapers: list[str]) -> None:
    """把源清单灌进上游 ProxyFetcher 实例（只改实例属性）。"""
    fetcher.online_sources = {proto: list(urls) for proto, urls in sources.items()}
    allowed = {SCRAPER_KEYS[name] for name in scrapers if name in SCRAPER_KEYS}
    fetcher.scraping_sources = [
        src for src in fetcher.scraping_sources
        if getattr(src.get("func"), "__name__", "") in allowed
    ]


def limit_records(fetched: dict[str, list[str]], protocols: list[str],
                  limit: int) -> list[dict]:
    """按协议**轮转取** ``limit`` 条（http/socks4/socks5 均匀，不偏向第一个协议）。

    ``limit<=0`` 全取（按协议分组）。
    """
    pools = {p: sorted(set(fetched.get(p, []))) for p in protocols}
    if limit <= 0:
        return [{"protocol": p, "proxy": addr} for p in protocols for addr in pools[p]]
    out: list[dict] = []
    idx = {p: 0 for p in protocols}
    while len(out) < limit:
        advanced = False
        for p in protocols:
            if idx[p] < len(pools[p]):
                out.append({"protocol": p, "proxy": pools[p][idx[p]]})
                idx[p] += 1
                advanced = True
                if len(out) >= limit:
                    break
        if not advanced:
            break
    return out


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _drain_fetch_progress(log_queue: "queue.Queue[str]", stop: threading.Event,
                          total: int) -> None:
    """把抓取器日志里的「每源终止行」折算成进度（每源恰好一条 ``[+]``/``[-]``/``[!]``）。"""
    done = 0
    last = -1
    while not stop.is_set():
        try:
            msg = log_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if msg[:3] in ("[+]", "[-]", "[!]"):
            done += 1
            if done != last:
                last = done
                _emit({"event": "progress", "phase": "fetch",
                       "done": min(done, total), "total": total})


def main(argv: list[str] | None = None) -> int:
    # Windows 默认控制台代码页（GBK）会把中文日志/源名写坏；两路强制 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        prog="cyberstrike_fetch.py",
        description="fir-proxy 代理池抓取 runner（可配置源清单）")
    parser.add_argument("-o", "--output", required=True, help="输出 JSON 文件路径")
    parser.add_argument("--output-format", default="json", help="兼容 cli.py 接口（仅 json）")
    parser.add_argument("--protocol", action="append",
                        choices=["http", "socks4", "socks5"], default=None)
    parser.add_argument("--sources", help="源清单覆盖 JSON（{api:{...}, scrapers:[...]}）")
    parser.add_argument("--limit", type=int, default=0,
                        help="返回条数上限（按协议轮转取）；0=不限")
    parser.add_argument("--quiet", action="store_true", help="兼容 cli.py 接口")
    parser.add_argument("--log-file", help="兼容 cli.py 接口（本 runner 不落日志文件）")
    args = parser.parse_args(argv)

    from modules.fetcher import ProxyFetcher

    sources, scrapers = _load_sources(args.sources)
    fetcher = ProxyFetcher()
    _apply(fetcher, sources, scrapers)

    protocols = args.protocol or ["http", "socks4", "socks5"]
    total_sources = (sum(len(urls) for urls in fetcher.online_sources.values())
                     + len(fetcher.scraping_sources))

    log_queue: "queue.Queue[str]" = queue.Queue()
    stop = threading.Event()
    drainer = threading.Thread(
        target=_drain_fetch_progress, args=(log_queue, stop, total_sources), daemon=True)
    drainer.start()
    try:
        fetched = fetcher.fetch_all(log_queue)
    finally:
        stop.set()
        drainer.join(timeout=1.0)
    _emit({"event": "progress", "phase": "fetch",
           "done": total_sources, "total": total_sources})

    records = limit_records(fetched, protocols, args.limit)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    _emit({
        "ok": True,
        "command": "fetch",
        "count": len(records),
        "by_protocol": {p: sum(1 for r in records if r["protocol"] == p) for p in protocols},
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
