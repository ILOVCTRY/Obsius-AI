"""知识快照一次性导入（DESIGN.md §4.5）。

把 Knowledge/ 下外部知识源按新树类目融入 kb 树：

- ctf-skills（MIT，Lukasz Jagiello）：**已于 2026-10-21 停用导入**。原按类别拆散进
  各方向 `kb/<方向>/赛事专题/题目知识/` 的 71 篇英文快照已按判据式规范全部融入各域
  中文模块并删除，全库不再保留英文原文。CTF_MAP 保留为空壳以防 `--force` 重建该目录
  把消化成果冲掉；若确需恢复英文原文，只能从备份手动放回，不再走本脚本。
- src-strike（内部知识源，无 LICENSE，按项目主授权使用）：按新树类目重映射落
  web 方向（打法→实战打法/、资料→载荷与字典/行业视野/PoC与模板/、散篇→实践笔记/，
  见 STRIKE_TOP_MAP）；其中 4 篇平台规则的完整版覆盖 安全红线/平台规则/
  （覆盖前自动备份到该目录 .history/）。

幂等：快照目录已存在默认跳过，--force 删除重建；owners 每次按当前源状态刷新。
注意：方向 README.md 为策展文档，本脚本不再生成/覆盖（2026-10-14 起）。

用法：
  python scripts/import_kb.py [--knowledge Knowledge]
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

# ctf-skills/<技能目录> -> (方向, 快照目录名)
# 2026-10-14 树状化重构时快照原件一律落 kb/<方向>/赛事专题/题目知识/<snap>/。
# 2026-10-21 英文快照消化：CTF_MAP 清空——71 篇英文已全部融入各域中文模块并删除。
# 保留空壳（而不是删掉整块逻辑）是为了让 `--force` 走到这里只打印「已停用」，
# 绝不会重建 `题目知识/` 目录把消化成果冲掉。
CTF_MAP: list[tuple[str, str, str]] = [
    # ("ctf-web", "web", "赛事专题/题目知识/ctf-web"),
    # ...（已停用，见 docstring）
]

# 唯一保留的 ctf-skills 落点：ctf-writeup 是收编版（非英文快照），继续生效。
WRITEUP_MAP: list[tuple[str, str, str]] = [
    ("ctf-writeup", "misc", "赛事专题/WP撰写"),
]

# src-strike rules/<文件> -> pentest 轨 owners/<tag>.md（完整版覆盖摘编版）
OWNER_RULES: list[tuple[str, str]] = [
    ("edusrc-rules.md", "edusrc.md"),
    ("edu-rating-rules.md", "edu-rating.md"),
    ("osrc-oppo-rules.md", "osrc.md"),
    ("ysrc-ezviz-rules.md", "ysrc.md"),
]

# 已人工演进为正本、源仓库版本落后的 owners 文件：--force 也**不覆盖**（2026-10-17）。
# edu-rating.md 是判级口径唯一正本（kb 副本已收口成指针），源仓库那份仍是旧版
# （中危 3 条 / 无「忽略清单」），覆盖即静默回退 → 只提示人工合并。
PRESERVE_OWNERS: dict[str, str] = {
    "edu-rating.md": "判级口径唯一正本（2026-10-17 收口，源仓库版本落后）",
}

# src-strike 快照排除（仓库脚手架/外部 agent 指令/重型运行时，均无知识价值或有
# 指令冲突风险——kb 内容会被 kb_open 喂给 Agent，宁严勿松）
STRIKE_EXCLUDE_TOP = {
    ".git", ".claude", "mcp-servers", "tools", "资产", "__pycache__",
}
STRIKE_EXCLUDE_FILES = {
    "AGENTS.md", "CLAUDE.md", ".mcp.json", ".gitignore",
}
COPY_EXCLUDE_DIRS = {".git", "__pycache__", "target", ".pytest_cache", "node_modules"}

# src-strike 顶层项 -> web 方向 kb 内落点（2026-10-14 树状化后类目）：
# 打法进 实战打法/、资料进 载荷与字典//行业视野//PoC与模板/；未映射顶层项收编 实践笔记/
STRIKE_TOP_MAP = {
    "知识库": "实战打法/知识库",
    "poc": "PoC与模板/poc",
    "SKILL.md": "实战打法/SKILL.md",
}

# K5 测试包分类制（2026-09-20，DESIGN.md §4）：知识库/ 下的测试点手册按
# 「阶段/测试包/手册.md」三层落位，其余（README、打穿短表）留守 实战打法/知识库/。
# 表：手册 stem（去 .md）→ (测试包目录, phase, vuln_class 分面)；手册文件名统一改 手册.md。
# 迁移脚本 scripts/migrate_kb_test_packages.py 复用本表，保证两侧口径一致。
# 2026-10-14：webapp/ → 漏洞分类/、recon/ → 信息收集/。
STRIKE_KB_REMAP = {
    "401-403-bypass": ("漏洞分类/401-403-bypass", "webapp", ["auth-bypass"]),
    "agent-tool-exec-test": ("漏洞分类/agent-tool-exec", "webapp", ["ai"]),
    "api-gateway-test": ("漏洞分类/api-gateway", "webapp", ["api", "gateway"]),
    "authbypass-test": ("漏洞分类/authbypass", "webapp", ["auth-bypass", "auth"]),
    "cache-poisoning-test": ("漏洞分类/cache-poisoning", "webapp", ["cache"]),
    "captcha-ocr-test": ("漏洞分类/captcha-ocr", "webapp", ["captcha"]),
    "clickjacking-test": ("漏洞分类/clickjacking", "webapp", ["clickjacking"]),
    "cloud-ide-codex-rce-chain": ("漏洞分类/cloud-ide-rce-chain", "webapp", ["rce", "chain"]),
    "cors-test": ("漏洞分类/cors", "webapp", ["cors"]),
    "crlf-injection-test": ("漏洞分类/crlf-injection", "webapp", ["crlf", "injection"]),
    "csp-bypass-test": ("漏洞分类/csp-bypass", "webapp", ["csp"]),
    "csrf-test": ("漏洞分类/csrf", "webapp", ["csrf"]),
    "csv-formula-injection-test": ("漏洞分类/csv-formula-injection", "webapp", ["csv", "injection"]),
    "dangling-markup-test": ("漏洞分类/dangling-markup", "webapp", ["xss", "markup"]),
    "dependency-confusion-test": ("漏洞分类/dependency-confusion", "webapp", ["supply-chain"]),
    "deserialization-test": ("漏洞分类/deserialization", "webapp", ["deserialization", "injection"]),
    "dns-rebinding-test": ("漏洞分类/dns-rebinding", "webapp", ["dns", "ssrf"]),
    "dnslog-oob": ("漏洞分类/dnslog-oob", "webapp", ["oob", "dnslog"]),
    "el-injection-test": ("漏洞分类/el-injection", "webapp", ["ssti", "injection"]),
    "email-header-injection-test": ("漏洞分类/email-header-injection", "webapp", ["smtp", "injection"]),
    "file-upload-test": ("漏洞分类/file-upload", "webapp", ["file-upload"]),
    "ghost-bits-cast-test": ("漏洞分类/ghost-bits-cast", "webapp", ["misc"]),
    "graphql-test": ("漏洞分类/graphql", "webapp", ["graphql", "api"]),
    "hpp-test": ("漏洞分类/hpp", "webapp", ["hpp"]),
    "http-host-header-test": ("漏洞分类/host-header", "webapp", ["host-header"]),
    "http-smuggling-test": ("漏洞分类/http-smuggling", "webapp", ["smuggling"]),
    "http2-attacks-test": ("漏洞分类/http2-attacks", "webapp", ["http2", "smuggling"]),
    "idor-test": ("漏洞分类/idor", "webapp", ["idor", "authz"]),
    "info-leak-test": ("漏洞分类/info-leak", "webapp", ["info-leak"]),
    "injection-test": ("漏洞分类/sqli", "webapp", ["sqli", "injection"]),
    "insecure-scm-test": ("漏洞分类/insecure-scm", "webapp", ["info-leak", "scm"]),
    "jndi-injection-test": ("漏洞分类/jndi-injection", "webapp", ["jndi", "injection", "rce"]),
    "js-reverse-guide": ("漏洞分类/js-reverse", "webapp", ["reverse"]),
    "llm-security-test": ("漏洞分类/llm-security", "webapp", ["ai", "llm"]),
    "logic-test": ("漏洞分类/logic", "webapp", ["logic"]),
    "oauth-jwt-test": ("漏洞分类/jwt", "webapp", ["jwt", "oauth", "auth"]),
    "open-redirect-test": ("漏洞分类/open-redirect", "webapp", ["open-redirect"]),
    "path-traversal-lfi-test": ("漏洞分类/path-traversal", "webapp", ["path-traversal", "lfi"]),
    "prototype-pollution-test": ("漏洞分类/prototype-pollution", "webapp", ["prototype-pollution"]),
    "race-condition-test": ("漏洞分类/race-condition", "webapp", ["race"]),
    "recon-methodology": ("信息收集/methodology", "recon", []),
    "ssrf-test": ("漏洞分类/ssrf", "webapp", ["ssrf"]),
    "subdomain-takeover-test": ("漏洞分类/subdomain-takeover", "webapp", ["takeover", "recon"]),
    "type-juggling-test": ("漏洞分类/type-juggling", "webapp", ["type-juggling"]),
    "waf-bypass": ("漏洞分类/waf-bypass", "webapp", ["waf"]),
    "websocket-test": ("漏洞分类/websocket", "webapp", ["websocket"]),
    "xslt-injection-test": ("漏洞分类/xslt-injection", "webapp", ["xslt", "injection"]),
    "xss-test": ("漏洞分类/xss", "webapp", ["xss"]),
    "xxe-test": ("漏洞分类/xxe", "webapp", ["xxe"]),
}
# 留守 实战打法/知识库/ 不迁的文件（stem 集合，反向校验用）
STRIKE_KB_STAY = {"README", "打穿短表"}


def _kb_frontmatter(stem: str) -> str:
    """K5 分面 frontmatter（升级项 C）：phase 单值 + vuln_class 行内列表。"""
    _dir, phase, vcls = STRIKE_KB_REMAP[stem]
    lines = ["---", f"phase: {phase}"]
    if vcls:
        lines.append(f"vuln_class: [{', '.join(vcls)}]")
    return "\n".join(lines) + "\n---\n"
# src-strike/references/<子项> -> 落点（2026-10-14 树状化后类目）；
# 未映射子项（含 compliance.md 等散文件）落 行业视野/
STRIKE_REF_MAP = {
    "methodology": "实战打法/methodology",
    "h1-reports": "赛事专题/h1-reports",
    "dictionaries": "载荷与字典/dictionaries",
    "industry": "行业视野/industry",
    "payloader": "载荷与字典/payloader",
    "templates": "PoC与模板/templates",
}


def _stage_strike(strike_root: Path) -> list[tuple[Path, str]]:
    """src-strike 上游展开为文件级 (源文件, web 方向 kb 内相对落点)。
    知识库/ 下命中 STRIKE_KB_REMAP 的手册按 K5 测试包分类改落
    `<阶段>/<测试包>/手册.md`（内容加 phase/vuln_class frontmatter，
    见 _import_strike），未命中（README/打穿短表）留守 实战打法/知识库/。"""
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
                if ref.name in STRIKE_REF_MAP:
                    _walk(ref, STRIKE_REF_MAP[ref.name])
                else:
                    _walk(ref, f"行业视野/{ref.name}")
        elif item.name in STRIKE_TOP_MAP:
            _walk(item, STRIKE_TOP_MAP[item.name],
                  remap_kb=(item.name == "知识库"))
        else:  # 未知散篇/目录收编 实践笔记/
            _walk(item, f"实践笔记/{item.name}")

    rules_src = strike_root / "rules"
    if rules_src.is_dir():
        _walk(rules_src, "实战打法/rules")
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
    """源仓库实际多嵌套一层 ctf-skills-main/。

    识别凭据不再只看 `ctf-web`：2026-10-21 英文快照停用后，源里可能只剩
    `ctf-writeup`（收编版）与根 `LICENSE`。任一标识在即视为合法源根。
    """
    for cand in (knowledge / "ctf-skills-main" / "ctf-skills-main",
                 knowledge / "ctf-skills-main"):
        if cand.is_dir() and ((cand / "ctf-web").is_dir()
                              or (cand / "ctf-writeup").is_dir()
                              or (cand / "LICENSE").is_file()):
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
    """导入 ctf-skills；返回实际落到内容的方向集合。

    2026-10-21 起英文快照已全部消化并删除，CTF_MAP 为空 → 本函数只做两件事：
    ① 打日志说明已停用；② 仍导入 ctf-writeup 收编版（它不是英文快照，继续生效）。
    绝不在这里重建 `赛事专题/题目知识/`——那会把消化成果冲掉。
    """
    caps: set[str] = set()
    if not CTF_MAP:
        _log("[ctf-skills] 英文快照导入已停用（2026-10-21 已消化进各域中文模块）")
    else:  # pragma: no cover —— 仅人工恢复英文原文时才走到
        _log("[ctf-skills] 开始（注意：CTF_MAP 非空，会重建题目知识目录）")
        for src_name, cap, snap_name in CTF_MAP:
            src = ctf_root / src_name
            if not src.is_dir():
                _log(f"  [警告] 源缺失，跳过: {src}")
                continue
            _copy_snapshot(src, packs / "kb" / cap / snap_name, force, stats)
            caps.add(cap)
    # ctf-writeup 是收编版（非英文快照），独立导入
    for src_name, cap, snap_name in WRITEUP_MAP:
        src = ctf_root / src_name
        if not src.is_dir():
            _log(f"  [警告] 源缺失，跳过: {src}")
            continue
        _copy_snapshot(src, packs / "kb" / cap / snap_name, force, stats)
        caps.add(cap)
    # 根 LICENSE 上收 packs/kb/licenses/（同一 MIT 文本，覆盖刷新）
    license_src = ctf_root / "LICENSE"
    if license_src.is_file():
        lic_dir = packs / "kb" / "licenses"
        lic_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_src, lic_dir / license_src.name)
        stats["license_caps"] = sorted(caps)
    return caps


def _import_strike(strike_root: Path, packs: Path, force: bool,
                   stats: dict, import_owners: bool) -> None:
    """导入 src-strike 到 web 方向 kb（新树类目重映射，见 _stage_strike）。"""
    _log("[src-strike] 开始")
    dst = packs / "kb" / "web"
    staged = _stage_strike(strike_root)  # 文件级 (源, 落点相对路径)
    rules_src = strike_root / "rules"

    # 跳过判定按 staged 落点是否已齐（dst 是方向根，恒存在，不能当完成标志）
    done = bool(staged) and all((dst / rel).is_file() for _src, rel in staged)
    if done and not force:
        stats["skipped"].append(str(dst))
        _log(f"  跳过（已导入，--force 可原位覆盖重建）: {dst}")
    else:
        # 覆盖式落盘：只写上游管辖落点（方向根还含 route.json/README 等非 strike
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
    # 2026-10-14：owners 现居 packs/kb/安全红线/平台规则/（轨红线并入 kb 根后的位置）
    owners_dir = packs / "kb" / "安全红线" / "平台规则"
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
        if dst.is_file() and dst_name in PRESERVE_OWNERS:
            _log(f"  owners/{dst_name} 跳过覆盖：{PRESERVE_OWNERS[dst_name]}；"
                 f"如需同步请人工合并 {src}")
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
