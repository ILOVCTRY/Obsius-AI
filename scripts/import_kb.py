"""知识快照一次性导入（DESIGN.md §4.5）。

把 Knowledge/ 下两套外部知识源原样融入正交包布局（快照不翻译、不进 registry、
不就地修改；AI 只能经 kb_open 按需打开）：

- ctf-skills（MIT，Lukasz Jagiello）：按类别拆散进各能力域 kb/<snapshot>/
  （web 域落 kb/web/refs/ctf-web，F15 结构），根 LICENSE 上收 packs/kb/licenses/
  （expert-pool M0，2026-09-21 树重组：能力包 kb 树已并入 packs/kb/<域>/ 全局单根，
  本脚本内的 kb_sources.json 生成与 capabilities/<cap>/kb 落点为旧布局遗产——
  重导前须先把输出目录改写到新树，或仅作素材参考）。
- src-strike（内部知识源，无 LICENSE，按项目主授权使用）：按 F15 重映射落
  web 域 kb（打法→playbooks/、资料→refs/、散篇→notes/，见 STRIKE_TOP_MAP）；
  其中 4 篇平台规则的完整版覆盖 tracks/pentest/rules/owners/（覆盖前自动
  备份到该目录 .history/）。

幂等：快照目录已存在默认跳过，--force 删除重建；生成文件（kb_sources.json、
kb/README.md、LICENSE、owners）每次按当前源状态刷新。

用法：
  E:\\Miniconda3\\python.exe scripts/import_kb.py [--knowledge Knowledge]
      [--packs packs] [--force] [--no-owners]
"""
from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ctf-skills/<技能目录> -> (能力包, 快照目录名)
# web 包 2026-09-19 F15 结构重做：ctf-web 快照落 refs/ctf-web（资料区）
CTF_MAP: list[tuple[str, str, str]] = [
    # K2 重排后：快照原件一律落 kb/refs/<snap>/ 保鲜（收编合并版在 kb/<domain>/，
    # 为迁移产物人工维护；--force 重建只动 refs/ 下的快照原件，绝不碰收编版）
    ("ctf-web", "web", "refs/ctf-web"),
    ("ctf-pwn", "binary", "refs/ctf-pwn"),
    ("ctf-reverse", "binary", "refs/ctf-reverse"),
    ("ctf-malware", "binary", "refs/ctf-malware"),
    ("ctf-crypto", "crypto", "refs/ctf-crypto"),
    ("ctf-forensics", "forensics", "refs/ctf-forensics"),
    ("ctf-osint", "forensics", "refs/ctf-osint"),
    ("ctf-misc", "misc", "refs/ctf-misc"),
    ("ctf-ai-ml", "misc", "refs/ctf-ai-ml"),
    ("ctf-writeup", "misc", "refs/ctf-writeup"),
]

# src-strike rules/<文件> -> pentest 轨 owners/<tag>.md（完整版覆盖摘编版）
OWNER_RULES: list[tuple[str, str]] = [
    ("edusrc-rules.md", "edusrc.md"),
    ("edu-rating-rules.md", "edu-rating.md"),
    ("osrc-oppo-rules.md", "osrc.md"),
    ("ysrc-ezviz-rules.md", "ysrc.md"),
]

# src-strike 快照排除（仓库脚手架/外部 agent 指令/重型运行时，均无知识价值或有
# 指令冲突风险——kb 内容会被 kb_open 喂给 Agent，宁严勿松）
STRIKE_EXCLUDE_TOP = {
    ".git", ".claude", "mcp-servers", "tools", "资产", "__pycache__",
}
STRIKE_EXCLUDE_FILES = {
    "AGENTS.md", "CLAUDE.md", ".mcp.json", ".gitignore",
}
COPY_EXCLUDE_DIRS = {".git", "__pycache__", "target", ".pytest_cache", "node_modules"}

# src-strike 顶层项 -> web/kb 内落点（F15 结构，DESIGN.md §4 定稿块）：
# 打法进 playbooks/、资料进 refs/；未映射顶层项收编 notes/（宁收编不复活旧平铺）
STRIKE_TOP_MAP = {
    "知识库": "playbooks/知识库",
    "poc": "refs/poc",
    "SKILL.md": "playbooks/SKILL.md",
}

# K5 测试包分类制（2026-09-20，DESIGN.md §4）：知识库/ 下的测试点手册按
# 「阶段/测试包/手册.md」三层落位，其余（README、打穿短表）留守 playbooks/知识库/。
# 表：手册 stem（去 .md）→ (测试包目录, phase, vuln_class 分面)；手册文件名统一改 手册.md。
# 迁移脚本 scripts/migrate_kb_test_packages.py 复用本表，保证两侧口径一致。
STRIKE_KB_REMAP = {
    "401-403-bypass": ("webapp/401-403-bypass", "webapp", ["auth-bypass"]),
    "agent-tool-exec-test": ("webapp/agent-tool-exec", "webapp", ["ai"]),
    "api-gateway-test": ("webapp/api-gateway", "webapp", ["api", "gateway"]),
    "authbypass-test": ("webapp/authbypass", "webapp", ["auth-bypass", "auth"]),
    "cache-poisoning-test": ("webapp/cache-poisoning", "webapp", ["cache"]),
    "captcha-ocr-test": ("webapp/captcha-ocr", "webapp", ["captcha"]),
    "clickjacking-test": ("webapp/clickjacking", "webapp", ["clickjacking"]),
    "cloud-ide-codex-rce-chain": ("webapp/cloud-ide-rce-chain", "webapp", ["rce", "chain"]),
    "cors-test": ("webapp/cors", "webapp", ["cors"]),
    "crlf-injection-test": ("webapp/crlf-injection", "webapp", ["crlf", "injection"]),
    "csp-bypass-test": ("webapp/csp-bypass", "webapp", ["csp"]),
    "csrf-test": ("webapp/csrf", "webapp", ["csrf"]),
    "csv-formula-injection-test": ("webapp/csv-formula-injection", "webapp", ["csv", "injection"]),
    "dangling-markup-test": ("webapp/dangling-markup", "webapp", ["xss", "markup"]),
    "dependency-confusion-test": ("webapp/dependency-confusion", "webapp", ["supply-chain"]),
    "deserialization-test": ("webapp/deserialization", "webapp", ["deserialization", "injection"]),
    "dns-rebinding-test": ("webapp/dns-rebinding", "webapp", ["dns", "ssrf"]),
    "dnslog-oob": ("webapp/dnslog-oob", "webapp", ["oob", "dnslog"]),
    "el-injection-test": ("webapp/el-injection", "webapp", ["ssti", "injection"]),
    "email-header-injection-test": ("webapp/email-header-injection", "webapp", ["smtp", "injection"]),
    "file-upload-test": ("webapp/file-upload", "webapp", ["file-upload"]),
    "ghost-bits-cast-test": ("webapp/ghost-bits-cast", "webapp", ["misc"]),
    "graphql-test": ("webapp/graphql", "webapp", ["graphql", "api"]),
    "hpp-test": ("webapp/hpp", "webapp", ["hpp"]),
    "http-host-header-test": ("webapp/host-header", "webapp", ["host-header"]),
    "http-smuggling-test": ("webapp/http-smuggling", "webapp", ["smuggling"]),
    "http2-attacks-test": ("webapp/http2-attacks", "webapp", ["http2", "smuggling"]),
    "idor-test": ("webapp/idor", "webapp", ["idor", "authz"]),
    "info-leak-test": ("webapp/info-leak", "webapp", ["info-leak"]),
    "injection-test": ("webapp/sqli", "webapp", ["sqli", "injection"]),
    "insecure-scm-test": ("webapp/insecure-scm", "webapp", ["info-leak", "scm"]),
    "jndi-injection-test": ("webapp/jndi-injection", "webapp", ["jndi", "injection", "rce"]),
    "js-reverse-guide": ("webapp/js-reverse", "webapp", ["reverse"]),
    "llm-security-test": ("webapp/llm-security", "webapp", ["ai", "llm"]),
    "logic-test": ("webapp/logic", "webapp", ["logic"]),
    "oauth-jwt-test": ("webapp/jwt", "webapp", ["jwt", "oauth", "auth"]),
    "open-redirect-test": ("webapp/open-redirect", "webapp", ["open-redirect"]),
    "path-traversal-lfi-test": ("webapp/path-traversal", "webapp", ["path-traversal", "lfi"]),
    "prototype-pollution-test": ("webapp/prototype-pollution", "webapp", ["prototype-pollution"]),
    "race-condition-test": ("webapp/race-condition", "webapp", ["race"]),
    "recon-methodology": ("recon/methodology", "recon", []),
    "ssrf-test": ("webapp/ssrf", "webapp", ["ssrf"]),
    "subdomain-takeover-test": ("webapp/subdomain-takeover", "webapp", ["takeover", "recon"]),
    "type-juggling-test": ("webapp/type-juggling", "webapp", ["type-juggling"]),
    "waf-bypass": ("webapp/waf-bypass", "webapp", ["waf"]),
    "websocket-test": ("webapp/websocket", "webapp", ["websocket"]),
    "xslt-injection-test": ("webapp/xslt-injection", "webapp", ["xslt", "injection"]),
    "xss-test": ("webapp/xss", "webapp", ["xss"]),
    "xxe-test": ("webapp/xxe", "webapp", ["xxe"]),
}
# 留守 playbooks/知识库/ 不迁的文件（stem 集合，反向校验用）
STRIKE_KB_STAY = {"README", "打穿短表"}


def _kb_frontmatter(stem: str) -> str:
    """K5 分面 frontmatter（升级项 C）：phase 单值 + vuln_class 行内列表。"""
    _dir, phase, vcls = STRIKE_KB_REMAP[stem]
    lines = ["---", f"phase: {phase}"]
    if vcls:
        lines.append(f"vuln_class: [{', '.join(vcls)}]")
    return "\n".join(lines) + "\n---\n"
# src-strike/references/<子项> -> 落点；playbooks 特殊：其子项上提一级进 playbooks/；
# 未映射子项（含 compliance.md 等散文件）落 refs/<名>
STRIKE_REF_MAP = {
    "methodology": "playbooks/methodology",
    "h1-reports": "refs/h1-reports",
    "dictionaries": "refs/dictionaries",
    "industry": "refs/industry",
    "payloader": "refs/payloader",
    "templates": "refs/templates",
}


def _stage_strike(strike_root: Path) -> list[tuple[Path, str]]:
    """src-strike 上游展开为文件级 (源文件, web/kb 内相对落点)。
    知识库/ 下命中 STRIKE_KB_REMAP 的手册按 K5 测试包分类改落
    `<阶段>/<测试包>/手册.md`（内容加 phase/vuln_class frontmatter，
    见 _import_strike），未命中（README/打穿短表）留守 playbooks/知识库/。"""
    staged: list[tuple[Path, str]] = []
    skill_root = strike_root / "skills" / "src-strike"

    def _walk(src: Path, rel_base: str, remap_kb: bool = False) -> None:
        if src.is_file():
            if src.name in STRIKE_EXCLUDE_FILES:
                return
            rel = rel_base
            if remap_kb and src.stem in STRIKE_KB_REMAP:
                rel = f"{STRIKE_KB_REMAP[src.stem][0]}/手册.md"
            staged.append((src, rel))
            return
        for child in sorted(src.iterdir()):
            if child.name in COPY_EXCLUDE_DIRS:
                continue
            _walk(child, f"{rel_base}/{child.name}" if rel_base else child.name,
                  remap_kb=remap_kb)

    for item in sorted(skill_root.iterdir()):
        if item.name in STRIKE_EXCLUDE_TOP or item.name in COPY_EXCLUDE_DIRS:
            continue
        if item.name == "references":
            for ref in sorted(item.iterdir()):
                if ref.name in COPY_EXCLUDE_DIRS:
                    continue
                if ref.name == "playbooks":  # 打法上提一级进 playbooks/
                    _walk(ref, "playbooks")
                elif ref.name in STRIKE_REF_MAP:
                    _walk(ref, STRIKE_REF_MAP[ref.name])
                else:
                    _walk(ref, f"refs/{ref.name}")
        elif item.name in STRIKE_TOP_MAP:
            _walk(item, STRIKE_TOP_MAP[item.name],
                  remap_kb=(item.name == "知识库"))
        else:  # 未知散篇/目录收编 notes/
            _walk(item, f"notes/{item.name}")

    rules_src = strike_root / "rules"
    if rules_src.is_dir():
        _walk(rules_src, "playbooks/rules")
    return staged


def _log(msg: str) -> None:
    print(msg, flush=True)


def _ignore_factory(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in COPY_EXCLUDE_DIRS}


def _safe_copy_factory(stats: dict):
    """逐文件容错复制：单个文件打不开（如杀软实时隔离了 shellcode 生成器）
    只记录不抛出，避免整个快照导入失败、其余内容静默缺失。"""
    def _copy(src, dst, *args, **kwargs):
        try:
            shutil.copy2(src, dst, *args, **kwargs)
        except OSError as e:
            stats["copy_errors"].append((str(src), str(e)))
            _log(f"  [警告] 单文件复制失败（已跳过，见摘要）: {src} —— {e}")
    return _copy


def _expected_upstream(rel: Path, roots: list[tuple[Path, str]]) -> bool:
    """dst 内相对文件 rel 是否来自上游。roots=[(上游根, dst 内前缀)]，
    前缀 "" 表示整棵同构。本地新增（含 kb API 新建经验 md）与 .history 不在此列。"""
    for src_root, prefix in roots:
        if prefix == "":
            if (src_root / rel).is_file():
                return True
        elif rel.parts and rel.parts[0] == prefix and len(rel.parts) > 1:
            if (src_root / Path(*rel.parts[1:])).is_file():
                return True
    return False


def _preserve_local(dst: Path, roots: list[tuple[Path, str]],
                    stats: dict) -> tempfile.TemporaryDirectory | None:
    """--force 重建前把 .history（本地版本/回收站）与上游没有的本地新增文件移到临时区。"""
    if not dst.is_dir():
        return None
    tmp = tempfile.TemporaryDirectory(prefix=".kb-preserve-", dir=str(dst.parent))
    n = 0
    for f in sorted(dst.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(dst)
        if rel.parts[0] == ".history" or not _expected_upstream(rel, roots):
            keep = Path(tmp.name) / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, keep)
            n += 1
    if n:
        stats.setdefault("preserved", []).append((str(dst), n))
        _log(f"  保护本地新增/历史版本 {n} 个文件，重建后还原: {dst}")
    return tmp


def _restore_local(tmp: tempfile.TemporaryDirectory | None, dst: Path) -> None:
    if tmp is None:
        return
    root = Path(tmp.name)
    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if f.is_file():
                target = dst / f.relative_to(root)
                if not target.exists():  # 上游同路径文件不被本地副本盖回
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, target)
    tmp.cleanup()


def _copy_snapshot(src: Path, dst: Path, force: bool, stats: dict) -> None:
    """复制一个快照目录；已存在则跳过（force 先删后拷，本地新增与 .history 自动保全）。"""
    preserved = None
    if dst.exists():
        if not force:
            stats["skipped"].append(str(dst))
            _log(f"  跳过（已存在，--force 可重建）: {dst}")
            return
        preserved = _preserve_local(dst, [(src, "")], stats)
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=_ignore_factory,
                    copy_function=_safe_copy_factory(stats))
    _restore_local(preserved, dst)
    stats["copied"].append(str(dst))
    _log(f"  复制: {src.name} -> {dst}")


def _find_ctf_root(knowledge: Path) -> Path | None:
    """源仓库实际多嵌套一层 ctf-skills-main/。"""
    for cand in (knowledge / "ctf-skills-main" / "ctf-skills-main",
                 knowledge / "ctf-skills-main"):
        if cand.is_dir() and (cand / "ctf-web").is_dir():
            return cand
    return None


def _find_strike_root(knowledge: Path, external_fallback: bool = True) -> Path | None:
    """Knowledge/src-strike 优先；不在则用 E 盘原始副本兜底（测试可关）。"""
    cands = [knowledge / "src-strike"]
    if external_fallback:
        cands.append(Path(r"E:\ILOVCTRY\SRC\src-strike"))
    for cand in cands:
        if cand.is_dir() and (cand / "skills" / "src-strike").is_dir():
            return cand
    return None


def _import_ctf(ctf_root: Path, packs: Path, force: bool, stats: dict) -> set[str]:
    """导入 ctf-skills；返回实际落到内容的能力包集合。"""
    caps: set[str] = set()
    _log("[ctf-skills] 开始")
    for src_name, cap, snap_name in CTF_MAP:
        src = ctf_root / src_name
        if not src.is_dir():
            _log(f"  [警告] 源缺失，跳过: {src}")
            continue
        _copy_snapshot(src, packs / "capabilities" / cap / "kb" / snap_name,
                       force, stats)
        caps.add(cap)
    # 每包附根 LICENSE（同一 MIT 文本，覆盖刷新）
    license_src = ctf_root / "LICENSE"
    if license_src.is_file():
        for cap in caps:
            kb = packs / "capabilities" / cap / "kb"
            shutil.copyfile(license_src, kb / "CTF-SKILLS-LICENSE")
        stats["license_caps"] = sorted(caps)
    return caps


def _import_strike(strike_root: Path, packs: Path, force: bool,
                   stats: dict, import_owners: bool) -> None:
    """导入 src-strike 到 web/kb/（F15 重映射：playbooks/refs/notes 三层，见 _stage_strike）。"""
    _log("[src-strike] 开始")
    dst = packs / "capabilities" / "web" / "kb"
    staged = _stage_strike(strike_root)  # 文件级 (源, 落点相对路径)
    rules_src = strike_root / "rules"

    # 跳过判定按 staged 落点是否已齐（dst 是 kb 根，恒存在，不能当完成标志）
    done = bool(staged) and all((dst / rel).is_file() for _src, rel in staged)
    if done and not force:
        stats["skipped"].append(str(dst))
        _log(f"  跳过（已导入，--force 可原位覆盖重建）: {dst}")
    else:
        # 覆盖式落盘：只写上游管辖落点（kb 根还含 route.json/refs 等非 strike
        # 内容，绝不整体 rmtree）；本地其它文件不受影响
        for src, rel in staged:
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if src.stem in STRIKE_KB_REMAP and src.suffix == ".md" \
                    and "知识库" in src.parent.name:
                # K5 测试包手册：内容加 phase/vuln_class frontmatter 再落盘
                text = src.read_text(encoding="utf-8")
                fm = _kb_frontmatter(src.stem)
                text = text if text.startswith("---") else fm + "\n" + text
                target.write_text(text, encoding="utf-8")
                _log(f"  复制(测试包+frontmatter): {src} -> {target}")
            else:
                _safe_copy_factory(stats)(src, target)
                _log(f"  复制: {src} -> {target}")
        stats["copied"].append(str(dst))

    # 平台规则完整版 -> pentest 轨 owners/（不随 --force 跳过，独立幂等）
    if import_owners:
        _merge_owners(rules_src, packs, stats)


def _merge_owners(rules_src: Path, packs: Path, stats: dict) -> None:
    owners_dir = packs / "tracks" / "pentest" / "rules" / "owners"
    history = owners_dir / ".history"
    for src_name, dst_name in OWNER_RULES:
        src = rules_src / src_name
        dst = owners_dir / dst_name
        if not src.is_file():
            _log(f"  [警告] owner 规则缺失: {src}")
            continue
        if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
            _log(f"  owners/{dst_name} 与源一致，无需覆盖")
            continue
        if dst.is_file():
            history.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = history / f"{dst_name}.{ts}.bak"
            shutil.copyfile(dst, backup)
            _log(f"  owners/{dst_name} 旧版备份 -> {backup}")
        owners_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        stats["owners"].append(dst_name)
        _log(f"  owners/{dst_name} 已用完整版覆盖")


def _write_kb_index(packs: Path, caps: set[str], sources_ok: dict[str, bool]) -> None:
    """每包生成 kb/README.md（中文快照索引，我们自己的维护文件，非快照内容）。"""
    index = {
        "web": [("recon", "信息收集：passive 被动 / active 主动"),
                ("webapp", "Web 漏洞族：authn 认证 / injection 注入 / 客户端"),
                ("post-exp", "后渗透（占位）"),
                ("notes", "实战散篇：网关陷阱 / 字段笔记"),
                ("playbooks", "src-strike 快照（内部知识源）· SRC 方法论/弹药/rules"),
                ("refs", "参考资料：h1-reports/字典/poc/cves + refs/ctf-web（ctf-skills MIT）")],
        "binary": [("pwn", "Pwn 收编版（快照原件在 refs/ctf-pwn）· 栈/堆/内核/沙箱/scripts"),
                   ("reverse", "逆向收编版（快照原件在 refs/ctf-reverse）· 语言/模式/工具/平台"),
                   ("malware", "恶意样本收编版（快照原件在 refs/ctf-malware）· PE/C2/混淆"),
                   ("refs", "ctf-skills（MIT）快照原件：ctf-pwn / ctf-reverse / ctf-malware")],
        "crypto": [("crypto", "密码学收编版（快照原件在 refs/ctf-crypto）· RSA/ECC/分组/格"),
                   ("refs", "ctf-skills（MIT）快照原件：ctf-crypto")],
        "forensics": [("forensics", "取证收编版（快照原件在 refs/ctf-forensics）· 磁盘/内存/流量/隐写"),
                      ("osint", "OSINT 收编版（快照原件在 refs/ctf-osint）"),
                      ("refs", "ctf-skills（MIT）快照原件：ctf-forensics / ctf-osint")],
        "misc": [("misc", "MISC 收编版（快照原件在 refs/ctf-misc）· jail/编码/游戏"),
                 ("ai-ml", "AI/ML 题收编版（快照原件在 refs/ctf-ai-ml）"),
                 ("writeup", "writeup 收尾方法论（快照原件在 refs/ctf-writeup）"),
                 ("refs", "ctf-skills（MIT）快照原件：ctf-misc / ctf-ai-ml / ctf-writeup")],
    }
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for cap in caps:
        kb = packs / "capabilities" / cap / "kb"
        lines = [
            f"# {cap} 能力包 · 知识库快照索引",
            "",
            "> 本目录是**外部知识原样快照区**：不进技能 registry、英文快照原文不翻译；",
            "> Agent 只能经 `kb_open(module=<快照名>/<包内相对路径>)` 按需打开单个文件。",
            "> 快照原文更新走 `scripts/import_kb.py` 重新导入。",
            ">",
            "> **本地基线（2026-09-14 起）**：本目录允许在 Skill 页就地修订与新建经验 md",
            "> （新经验写新文件，不覆盖翻译英文原文）；重新导入会保留本地新增文件与",
            "> `.history/` 版本历史，本 README 重写前也自动留版本备份。",
            "",
            f"导入日期（UTC）：{today}",
            "",
            "| 快照 | 来源与内容 |",
            "|---|---|",
        ]
        for name, desc in index.get(cap, []):
            mark = "" if (kb / name).is_dir() else "（本次未导入）"
            lines.append(f"| `{name}/`{mark} | {desc} |")
        lines += [
            "",
            "许可：ctf-skills 快照为 MIT（见 CTF-SKILLS-LICENSE）；src-strike 为内部",
            "知识源快照（无独立 LICENSE），仅限本项目授权使用，不外发。",
            "",
        ]
        readme = kb / "README.md"
        if readme.is_file():  # 重写前留版本到 .history，本地批注可找回
            from core.skills.writing import backup_history, pack_write_lock
            with pack_write_lock():
                backup_history(readme)
        readme.write_text("\n".join(lines), encoding="utf-8")


def _write_kb_sources(packs: Path, caps: set[str]) -> None:
    """每包一个 kb_sources.json：单源 root=kb，module 路径自带快照名前缀天然消歧。"""
    for cap in caps:
        cap_dir = packs / "capabilities" / cap
        if not (cap_dir / "kb").is_dir():
            continue
        data = {"sources": [{"id": f"{cap}-kb", "root": "kb", "recursive": True}]}
        (cap_dir / "kb_sources.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(knowledge: str | Path = "Knowledge", packs: str | Path = "packs",
         force: bool = False, import_owners: bool = True,
         external_fallback: bool = True) -> dict:
    knowledge, packs = Path(knowledge), Path(packs)
    stats: dict = {"copied": [], "skipped": [], "owners": [], "license_caps": [],
                   "copy_errors": []}
    caps: set[str] = set()

    ctf_root = _find_ctf_root(knowledge)
    if ctf_root:
        caps |= _import_ctf(ctf_root, packs, force, stats)
    else:
        _log("[ctf-skills] 未找到源目录，跳过")

    strike_root = _find_strike_root(knowledge, external_fallback)
    if strike_root:
        _import_strike(strike_root, packs, force, stats, import_owners)
        caps.add("web")
    else:
        _log("[src-strike] 未找到源目录（Knowledge/ 与 E:\\ILOVCTRY\\SRC 均无），跳过")

    _write_kb_sources(packs, caps)
    _write_kb_index(packs, caps, {})
    _log("\n========== 导入摘要 ==========")
    _log(f"快照复制 {len(stats['copied'])} 个，跳过 {len(stats['skipped'])} 个；"
         f"owners 覆盖 {stats['owners']}；LICENSE 落包 {stats['license_caps']}")
    if stats["copy_errors"]:
        _log(f"[注意] {len(stats['copy_errors'])} 个文件复制失败（常见原因：杀软实时"
             f"隔离含 shellcode/exp 模板的脚本）：")
        for src, err in stats["copy_errors"]:
            _log(f"  - {src}")
    return stats


def _argv() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="知识快照导入（幂等）")
    ap.add_argument("--knowledge", default="Knowledge", help="知识原料根目录")
    ap.add_argument("--packs", default="packs", help="packs 根目录")
    ap.add_argument("--force", action="store_true",
                    help="快照目录已存在时删除重建（owners 不受此开关影响）")
    ap.add_argument("--no-owners", action="store_true",
                    help="不覆盖 pentest 轨 owners/ 平台规则")
    return ap.parse_args()


if __name__ == "__main__":
    args = _argv()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main(args.knowledge, args.packs, force=args.force,
         import_owners=not args.no_owners)
