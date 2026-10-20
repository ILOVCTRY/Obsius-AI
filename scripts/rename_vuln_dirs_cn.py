#!/usr/bin/env python
"""漏洞分类目录中文化（一次性迁移，2026-10-20）。

把 `packs/kb/web/漏洞分类/<英文slug>/` 改成 `<中文名>/`，并同步全仓引用：
  - packs/kb/**/*.md / *.json / *.yaml（wikilink、反引号路径、route.json）
  - packs/kb/route_index.yaml
  - packs/experts/*.yaml
  - core/**/*.py / *.md（docstring 里的示例路径）
  - tests/**（golden fixture 与硬编码断言）

只替换**带 `漏洞分类/` 前缀的完整路径**，不碰 `tags: [sqli]`、反引号语义标签
这类裸 slug——它们不是路径，改了会破坏语义。

用法：
    python scripts/rename_vuln_dirs_cn.py --dry-run
    python scripts/rename_vuln_dirs_cn.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VULN_ROOT = ROOT / "packs" / "kb" / "web" / "漏洞分类"

# 英文 slug -> 中文目录名
MAPPING: dict[str, str] = {
    "401-403-bypass": "401与403绕过",
    "agent-tool-exec": "AI代理工具执行",
    "api-gateway": "API网关",
    "authbypass": "认证绕过",
    "authn": "认证与会话",
    "cache-poisoning": "缓存投毒",
    "captcha-ocr": "验证码识别",
    "clickjacking": "点击劫持",
    "cloud-ide-rce-chain": "云IDE远程执行链",
    "control-gap": "控制缺失审计",
    "cors": "跨域配置错误",
    "crlf-injection": "CRLF注入",
    "csp-bypass": "CSP绕过",
    "csrf": "跨站请求伪造",
    "csv-formula-injection": "CSV公式注入",
    "dangling-markup": "悬垂标记注入",
    "dependency-confusion": "依赖混淆",
    "deserialization": "反序列化",
    "dns-rebinding": "DNS重绑定",
    "dnslog-oob": "DNS外带",
    "el-injection": "表达式注入",
    "email-header-injection": "邮件头注入",
    "file-upload": "文件上传",
    "ghost-bits-cast": "幽灵位截断",
    "graphql": "GraphQL",
    "host-header": "Host头注入",
    "hpp": "HTTP参数污染",
    "http-smuggling": "HTTP请求走私",
    "http2-attacks": "HTTP2攻击",
    "idor": "越权",
    "info-leak": "信息泄露",
    "injection": "注入类",
    "insecure-scm": "SCM配置泄露",
    "jndi-injection": "JNDI注入",
    "js-reverse": "JS逆向",
    "jwt": "JWT",
    "llm-security": "大模型安全",
    "logic": "业务逻辑",
    "misc": "网关与杂项",
    "open-redirect": "开放重定向",
    "path-traversal": "路径穿越",
    "prototype-pollution": "原型链污染",
    "race-condition": "条件竞争",
    "seatable": "SeaTable平台",
    "source-audit": "源码审计",
    "sqli": "SQL注入",
    "ssrf": "SSRF",
    "subdomain-takeover": "子域接管",
    "taint-analysis": "污点分析",
    "type-juggling": "类型混淆",
    "waf-bypass": "WAF绕过",
    "websocket": "WebSocket",
    "xslt-injection": "XSLT注入",
    "xss": "XSS",
    "xxe": "XXE",
    "期刊采编-e-tiller": "期刊采编平台",
}

# 扫描范围（相对 ROOT）。scripts/ 不扫：本文件的 MAPPING 表会自毁。
SCAN_DIRS = ("packs", "core", "tests")
SCAN_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".ts", ".tsx"}
SKIP_PARTS = {".history", "node_modules", ".tmp_pytest", "dist", "__pycache__"}


def _iter_targets() -> list[Path]:
    out: list[Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in SCAN_SUFFIXES:
                continue
            if SKIP_PARTS & set(p.parts):
                continue
            out.append(p)
    return out


def _build_replacer() -> tuple[re.Pattern, dict[str, str]]:
    """只匹配 `漏洞分类/<slug>` 这一形态，长 slug 优先防前缀截断。"""
    slugs = sorted(MAPPING, key=len, reverse=True)
    alt = "|".join(re.escape(s) for s in slugs)
    # 前面允许 `漏洞分类/` 或 `漏洞分类\`（Windows 反斜杠写法）
    pat = re.compile(r"(漏洞分类[/\\])(" + alt + r")(?![A-Za-z0-9_\-])")
    return pat, {s: MAPPING[s] for s in slugs}


def _plan() -> tuple[list[tuple[Path, Path]], list[tuple[Path, int]]]:
    """返回 (目录改名清单, 文件改写清单)。"""
    dir_renames: list[tuple[Path, Path]] = []
    for slug, cn in MAPPING.items():
        src = VULN_ROOT / slug
        if src.is_dir():
            dir_renames.append((src, VULN_ROOT / cn))

    pat, _ = _build_replacer()
    file_hits: list[tuple[Path, int]] = []
    for p in _iter_targets():
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        n = len(pat.findall(text))
        if n:
            file_hits.append((p, n))
    return dir_renames, file_hits


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true",
                    help="不留 .bak（默认留，便于回滚）")
    args = ap.parse_args()

    dir_renames, file_hits = _plan()
    total = sum(n for _, n in file_hits)
    print(f"目录改名: {len(dir_renames)} 个")
    print(f"引用改写: {len(file_hits)} 个文件 / {total} 处")
    print()
    for p, n in sorted(file_hits, key=lambda t: -t[1])[:15]:
        print(f"  {n:4d}  {p.relative_to(ROOT)}")
    if len(file_hits) > 15:
        print(f"  ... 其余 {len(file_hits) - 15} 个文件")

    if args.dry_run:
        print("\n[dry-run] 未做任何修改。")
        return 0

    # 1) 先改文件内容（此时旧目录还在，路径仍可读）
    pat, _ = _build_replacer()
    changed = 0
    for p, _n in file_hits:
        text = p.read_text(encoding="utf-8")
        new = pat.sub(lambda m: m.group(1) + MAPPING[m.group(2)], text)
        if new == text:
            continue
        if not args.no_backup:
            shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
        p.write_text(new, encoding="utf-8")
        changed += 1
    print(f"\n[1/2] 已改写 {changed} 个文件")

    # 2) 再改目录
    done = 0
    for src, dst in dir_renames:
        if dst.exists():
            print(f"  !! 目标已存在，跳过: {dst.relative_to(ROOT)}")
            continue
        src.rename(dst)
        done += 1
    print(f"[2/2] 已改名 {done} 个目录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
