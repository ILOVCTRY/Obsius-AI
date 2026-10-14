#!/usr/bin/env python
"""kb 树状化分类重构（2026-10-14）。

把 packs/kb/ 从「方向 → 英文平铺模块」重构为「方向 → 中文类目 → 子目录」，
refs/ 原料库拆入各类目。幂等：默认 dry-run 只打印计划；--apply 才落盘。

现状问题（盘点结论）：
- crypto/crypto、forensics/forensics、misc/misc 自套娃（知识深一层）；
- misc/refs/ctf-misc 与 misc/misc 近重复（6 同 4 异 + games-and-vms-2/3/4）；
- web 散落：recon-asset-enum/（1 篇）、post-exp/（README 空壳）、notes/（4 散篇）；
- binary/playbooks/ 仅一篇图片隐写.md（隐写归 forensics）；
- misc/refs/ctf-writeup（仅 SKILL.md）与 writeup/（index.md）同源两份；
- refs/ 原料库（WP 合集/payload/工具手册）与知识树混层；
- route.json/route_index.yaml/kbindex/refs/tool_registry/writing/doctor 全是旧路径。

目标树（方向 → 模块 → 子目录，模块层中文化，level-3 类目名不动）：
  web/       漏洞分类/ 信息收集/ 实战打法/ 后渗透/ 小程序安全/ 实践笔记/
             赛事专题/{题目知识,Writeup合集,靶场复盘} 载荷与字典/ 工具手册/ 行业视野/ PoC与模板/
  binary/    漏洞利用/ 逆向工程/ 移动安全/ dotnet逆向/ 终端对抗/ 恶意样本/
             赛事专题/{题目知识,Writeup合集} 载荷与字典/
  crypto/    非对称加密/ 对称加密/ 古典密码/ 格与后量子/ 随机数/ 高阶专题/
             实战打法/ 赛事专题/{题目知识,Writeup合集,靶场复盘} 载荷与字典/
  forensics/ 磁盘与内存/ 网络取证/ 隐写与信号/ 系统取证/ 其他取证/ 开源情报/
             实战打法/ 赛事专题/{题目知识,Writeup合集} 载荷与字典/
  misc/      编码解码/ 沙箱逃逸/ 协议异常/ 游戏与虚拟机/ 系统杂项/ 射频信号/ AI安全/
             赛事专题/{题目知识,Writeup合集,靶场复盘,WP撰写} 载荷与字典/
  cloud/     云原生/ 威胁检测/ 云安全知识/ 云厂商/（level-3 不动）

约定（沿用 import_kb 时代「收编版 vs 快照原件」语义）：
- 知识模块 = 收编版（可就地修订）；赛事专题/题目知识/ = ctf-skills 上游原件（不翻译）。
- 字节相同的原件副本去重删除（内容已在收编版，零损失）。

用法：
  python scripts/refactor_kb_tree.py           # dry-run
  python scripts/refactor_kb_tree.py --apply   # 执行
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "packs" / "kb"

# ---------------------------------------------------------------- 搬迁计划
# ① 提取移动（先于目录改名/撤销，避免被父目录改名裹走）
EXTRACT_DIR_MOVES: list[tuple[str, str]] = [
    ("web/playbooks/intranet-postexp", "web/后渗透/intranet-postexp"),
]

# ② 目录级 git mv（老目录 → 新目录；目标不得已存在）
DIR_MOVES: list[tuple[str, str]] = [
    # web
    ("web/webapp", "web/漏洞分类"),
    ("web/recon", "web/信息收集"),
    ("web/playbooks", "web/实战打法"),
    ("web/miniprogram", "web/小程序安全"),
    ("web/notes", "web/实践笔记"),
    # binary
    ("binary/pwn", "binary/漏洞利用"),
    ("binary/reverse", "binary/逆向工程"),
    ("binary/android", "binary/移动安全"),
    ("binary/dotnet", "binary/dotnet逆向"),
    ("binary/edr", "binary/终端对抗"),
    ("binary/malware", "binary/恶意样本"),
    # crypto / forensics / misc / cloud
    ("crypto/playbooks", "crypto/实战打法"),
    ("forensics/osint", "forensics/开源情报"),
    ("forensics/playbooks", "forensics/实战打法"),
    ("misc/ai-ml", "misc/AI安全"),
    ("cloud/native", "cloud/云原生"),
    ("cloud/detection", "cloud/威胁检测"),
    ("cloud/knowledge", "cloud/云安全知识"),
    ("cloud/vendors", "cloud/云厂商"),
    # refs → 各类目模块（题目知识=上游 ctf-skills 原件）
    ("web/refs/ctf-web", "web/赛事专题/题目知识/ctf-web"),
    ("web/refs/ctf-writeups", "web/赛事专题/Writeup合集"),
    ("web/refs/lab-writeups", "web/赛事专题/靶场复盘"),
    ("web/refs/payloader", "web/载荷与字典/payloader"),
    ("web/refs/payloads", "web/载荷与字典/payloads"),
    ("web/refs/dictionaries", "web/载荷与字典/dictionaries"),
    ("web/refs/tools", "web/工具手册"),
    ("web/refs/industry", "web/行业视野"),
    ("web/refs/poc", "web/PoC与模板/poc"),
    ("web/refs/templates", "web/PoC与模板/templates"),
    ("binary/refs/ctf-pwn", "binary/赛事专题/题目知识/ctf-pwn"),
    ("binary/refs/ctf-reverse", "binary/赛事专题/题目知识/ctf-reverse"),
    ("binary/refs/ctf-malware", "binary/赛事专题/题目知识/ctf-malware"),
    ("binary/refs/ctf-writeups", "binary/赛事专题/Writeup合集"),
    ("binary/refs/payloads", "binary/载荷与字典"),
    ("crypto/refs/ctf-crypto", "crypto/赛事专题/题目知识/ctf-crypto"),
    ("crypto/refs/ctf-writeups", "crypto/赛事专题/Writeup合集"),
    ("crypto/refs/lab-writeups", "crypto/赛事专题/靶场复盘"),
    ("crypto/refs/payloads", "crypto/载荷与字典"),
    ("forensics/refs/ctf-forensics", "forensics/赛事专题/题目知识/ctf-forensics"),
    ("forensics/refs/ctf-osint", "forensics/赛事专题/题目知识/ctf-osint"),
    ("forensics/refs/ctf-writeups", "forensics/赛事专题/Writeup合集"),
    ("forensics/refs/payloads", "forensics/载荷与字典"),
    ("misc/refs/ctf-misc", "misc/赛事专题/题目知识/ctf-misc"),
    ("misc/refs/ctf-ai-ml", "misc/赛事专题/题目知识/ctf-ai-ml"),
    ("misc/refs/ctf-writeups", "misc/赛事专题/Writeup合集"),
    ("misc/refs/lab-writeups", "misc/赛事专题/靶场复盘"),
    ("misc/refs/payloads", "misc/载荷与字典"),
]

# ③ 自套娃拆分（目录溶解为按类目的文件级移动；index.md 除外，走 INDEX_SPECIALS）
SPLIT_MOVES: list[tuple[str, str]] = [
    # crypto/crypto → 六个算法族模块
    ("crypto/crypto/rsa-attacks.md", "crypto/非对称加密/rsa-attacks.md"),
    ("crypto/crypto/dh-attacks.md", "crypto/非对称加密/dh-attacks.md"),
    ("crypto/crypto/ecc-attacks.md", "crypto/非对称加密/ecc-attacks.md"),
    ("crypto/crypto/modern-ciphers.md", "crypto/对称加密/modern-ciphers.md"),
    ("crypto/crypto/stream-ciphers.md", "crypto/对称加密/stream-ciphers.md"),
    ("crypto/crypto/classic-ciphers.md", "crypto/古典密码/classic-ciphers.md"),
    ("crypto/crypto/historical.md", "crypto/古典密码/historical.md"),
    ("crypto/crypto/lattice-and-lwe.md", "crypto/格与后量子/lattice-and-lwe.md"),
    ("crypto/crypto/post-quantum.md", "crypto/格与后量子/post-quantum.md"),
    ("crypto/crypto/prng.md", "crypto/随机数/prng.md"),
    ("crypto/crypto/prng-attacks.md", "crypto/随机数/prng-attacks.md"),
    ("crypto/crypto/zkp-and-advanced.md", "crypto/高阶专题/zkp-and-advanced.md"),
    ("crypto/crypto/advanced-math.md", "crypto/高阶专题/advanced-math.md"),
    ("crypto/crypto/exotic-crypto.md", "crypto/高阶专题/exotic-crypto.md"),
    # forensics/forensics → 五个取证类目
    ("forensics/forensics/disk-and-memory.md", "forensics/磁盘与内存/disk-and-memory.md"),
    ("forensics/forensics/disk-advanced.md", "forensics/磁盘与内存/disk-advanced.md"),
    ("forensics/forensics/disk-recovery.md", "forensics/磁盘与内存/disk-recovery.md"),
    ("forensics/forensics/windows.md", "forensics/磁盘与内存/windows.md"),
    ("forensics/forensics/network.md", "forensics/网络取证/network.md"),
    ("forensics/forensics/network-advanced.md", "forensics/网络取证/network-advanced.md"),
    ("forensics/forensics/steganography.md", "forensics/隐写与信号/steganography.md"),
    ("forensics/forensics/stego-advanced.md", "forensics/隐写与信号/stego-advanced.md"),
    ("forensics/forensics/stego-image.md", "forensics/隐写与信号/stego-image.md"),
    ("forensics/forensics/signals-and-hardware.md", "forensics/隐写与信号/signals-and-hardware.md"),
    ("forensics/forensics/peripheral-capture.md", "forensics/隐写与信号/peripheral-capture.md"),
    ("forensics/forensics/linux-forensics.md", "forensics/系统取证/linux-forensics.md"),
    ("forensics/forensics/3d-printing.md", "forensics/其他取证/3d-printing.md"),
    # misc/misc → 六个杂项类目
    ("misc/misc/encodings.md", "misc/编码解码/encodings.md"),
    ("misc/misc/encodings-advanced.md", "misc/编码解码/encodings-advanced.md"),
    ("misc/misc/bashjails.md", "misc/沙箱逃逸/bashjails.md"),
    ("misc/misc/pyjails.md", "misc/沙箱逃逸/pyjails.md"),
    ("misc/misc/dns.md", "misc/协议异常/dns.md"),
    ("misc/misc/games-and-vms.md", "misc/游戏与虚拟机/games-and-vms.md"),
    ("misc/misc/linux-privesc.md", "misc/系统杂项/linux-privesc.md"),
    ("misc/misc/rf-sdr.md", "misc/射频信号/rf-sdr.md"),
    ("misc/misc/ctfd-navigation.md", "misc/赛事专题/ctfd-navigation.md"),
]

# ④ 零散文件级移动（含 web-vulns 的 .idx.md 按类目落位）
FILE_MOVES: list[tuple[str, str]] = [
    ("web/post-exp/README.md", "web/后渗透/README.md"),
    ("binary/playbooks/图片隐写.md", "forensics/隐写与信号/图片隐写.md"),
    ("web/recon-asset-enum/crtsh-powershell-subdomain-enum.md",
     "web/信息收集/crtsh-powershell-subdomain-enum.md"),
    ("web/refs/cves.md", "web/漏洞分类/cves.md"),
    ("web/refs/compliance.md", "web/行业视野/compliance.md"),
    ("web/refs/pat-reference.md", "web/行业视野/pat-reference.md"),
    ("web/refs/web-recon/passive-recon-notes.md", "web/信息收集/passive-recon-notes.md"),
    # web-vulns 索引/速查表按类目落入
    ("web/refs/web-vulns/JWT.idx.md", "web/漏洞分类/jwt/JWT.idx.md"),
    ("web/refs/web-vulns/SQL.idx.md", "web/漏洞分类/sqli/SQL.idx.md"),
    ("web/refs/web-vulns/SSRF漏洞.idx.md", "web/漏洞分类/ssrf/SSRF漏洞.idx.md"),
    ("web/refs/web-vulns/PHP反序列化漏洞总结.idx.md",
     "web/漏洞分类/deserialization/PHP反序列化漏洞总结.idx.md"),
    ("web/refs/web-vulns/php代码审计.idx.md", "web/漏洞分类/source-audit/php代码审计.idx.md"),
    ("web/refs/web-vulns/命令执行.idx.md", "web/漏洞分类/injection/命令执行.idx.md"),
    ("web/refs/web-vulns/文件上传漏洞.idx.md", "web/漏洞分类/file-upload/文件上传漏洞.idx.md"),
    ("web/refs/web-vulns/文件包含.idx.md", "web/漏洞分类/path-traversal/文件包含.idx.md"),
    ("web/refs/web-vulns/PAYLOAD-CHEATSHEET.md", "web/载荷与字典/PAYLOAD-CHEATSHEET.md"),
    ("web/refs/web-vulns/README.md", "web/漏洞分类/README.md"),
    ("web/refs/web-vulns/CHANGELOG.md", "web/漏洞分类/CHANGELOG.md"),
    ("web/refs/web-vulns/AI-SEARCH-INDEX.md", "web/漏洞分类/AI-SEARCH-INDEX.md"),
    ("crypto/refs/web-vulns/压缩包总结.idx.md", "crypto/载荷与字典/压缩包总结.idx.md"),
    ("forensics/refs/web-vulns/图片隐写.idx.md", "forensics/隐写与信号/图片隐写.idx.md"),
    # ctf-writeup 技能两份同源拷贝合并
    ("misc/refs/ctf-writeup/SKILL.md", "misc/赛事专题/WP撰写/SKILL.md"),
    ("misc/writeup/index.md", "misc/赛事专题/WP撰写/index.md"),
]

# ⑤ index.md 特判：收编版目录里的 index.md 实为上游 ctf-<x> SKILL（与 refs 原件同源）；
# 字节相同 → 删收编版副本（原件已在赛事专题/题目知识）；不同 → 收编版落 SKILL-收编版.md。
INDEX_SPECIALS: list[tuple[str, str]] = [
    ("crypto/crypto/index.md", "crypto/赛事专题/题目知识/ctf-crypto/SKILL.md"),
    ("forensics/forensics/index.md", "forensics/赛事专题/题目知识/ctf-forensics/SKILL.md"),
    ("misc/misc/index.md", "misc/赛事专题/题目知识/ctf-misc/SKILL.md"),
]

# ⑥ 路由表里「整目录被打散」的值 → 展开为新模块目录清单
ROUTE_JSON_EXPAND: dict[str, dict[str, list[str]]] = {
    "crypto": {"crypto/crypto/": ["crypto/非对称加密/", "crypto/对称加密/",
                                  "crypto/古典密码/", "crypto/格与后量子/",
                                  "crypto/随机数/", "crypto/高阶专题/"]},
    "forensics": {"forensics/forensics/": ["forensics/磁盘与内存/", "forensics/网络取证/",
                                            "forensics/隐写与信号/", "forensics/系统取证/",
                                            "forensics/其他取证/"]},
    "misc": {"misc/misc/": ["misc/编码解码/", "misc/沙箱逃逸/", "misc/协议异常/",
                            "misc/游戏与虚拟机/", "misc/系统杂项/", "misc/射频信号/"]},
}

# 搬迁后应被 rmdir 的空壳目录（非空则报警，不强删）
EMPTY_DIRS = [
    "web/refs", "web/refs/web-vulns", "web/refs/web-recon", "web/recon-asset-enum",
    "web/post-exp", "binary/refs", "binary/playbooks",
    "crypto/refs", "crypto/crypto", "forensics/refs", "forensics/forensics",
    "misc/refs", "misc/refs/ctf-writeup", "misc/misc", "misc/writeup",
]

# ---------------------------------------------------------------- 工具


def git_mv(old: str, new: str) -> str:
    """git mv 保留历史；未跟踪文件回退 shutil.move。返回实际采取的方式。"""
    src, dst = KB / old, KB / new
    if not src.exists():
        return "skip"
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["git", "mv", old, new], cwd=KB, capture_output=True, text=True)
    if r.returncode == 0:
        return "git-mv"
    shutil.move(str(src), str(dst))
    return "move"


def git_rm(rel: str) -> bool:
    r = subprocess.run(["git", "rm", "-q", rel], cwd=KB, capture_output=True, text=True)
    if r.returncode == 0:
        return True
    p = KB / rel
    if p.is_file():
        p.unlink()
        return True
    return False


def build_path_map(applied: list[tuple[str, str]]) -> dict[str, str]:
    """已执行搬迁 → 旧路径串 → 新路径串（目录给带斜杠与裸名两种键，长的优先）。"""
    m: dict[str, str] = {}
    for old, new in applied:
        if (KB / old).is_dir() or old in {o for o, _ in DIR_MOVES} or old in {
                o for o, _ in EXTRACT_DIR_MOVES}:
            m[old + "/"] = new + "/"
            m[old] = new
        else:
            m[old] = new
    return m


_MD_LINK_RE = re.compile(r"\]\((\.\.?/[^)#\s]+?\.md)(#[^)]*)?\)")


def snapshot_rel_links() -> list[tuple[str, str, str]]:
    """搬迁前快照所有 md 的相对链接：(文件旧相对路径, 链接串, 目标旧相对路径)。

    只记 .md 目标；图片等非 md 资源若同模块搬迁则相对位置不变，不处理。"""
    out: list[tuple[str, str, str]] = []
    for f in sorted(KB.rglob("*.md")):
        if ".history" in f.parts:
            continue
        rel = f.relative_to(KB).as_posix()
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _MD_LINK_RE.finditer(text):
            target = os.path.normpath(
                os.path.join(os.path.dirname(rel), m.group(1)))
            out.append((rel, m.group(0), target.replace("\\", "/")))
    return out


def rewrite_rel_links(path_map: dict[str, str]) -> int:
    """按新位置重算相对链接；只改目标被搬迁过的链接。"""
    snaps = snapshot_rel_links()
    n = 0
    for old_rel, link, old_target in snaps:
        if old_target not in path_map:
            continue
        new_rel = path_map.get(old_rel, old_rel)
        if new_rel == old_rel:
            continue
        new_target = path_map[old_target]
        want = os.path.relpath(new_target, os.path.dirname(new_rel)).replace("\\", "/")
        if want == link:
            continue
        f = KB / new_rel
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        if link in text:
            f.write_text(text.replace(link, f"]({want})", 1), encoding="utf-8")
            n += 1
    return n


# ---------------------------------------------------------------- 引用改写

REWRITE_EXTS = {".md", ".py", ".yaml", ".yml", ".json", ".ts", ".tsx", ".txt", ".sh"}
REWRITE_SKIP_DIRS = {".git", "node_modules", "dist", ".venv", "__pycache__",
                     ".history", ".workbuddy-ai", ".pytest_cache", "target"}
REWRITE_SKIP_FILES = {"migrate_kb_tree.py", "package-lock.json"}  # 历史迁移脚本/锁文件


def rewrite_refs(path_map: dict[str, str]) -> tuple[int, list[str]]:
    """全仓文本里旧 kb 路径串 → 新串（最长键优先）。

    范围：core/ scripts/ webui/src/ packs/kb（.history 除外）/ tests/ docs/ 及根级文本。
    返回 (改动文件数, 仍含旧路径串的文件清单——应只剩历史叙事)。"""
    keys = sorted(path_map, key=len, reverse=True)
    roots = [ROOT / d for d in ("core", "scripts", "webui", "tests", "packs", "docs")
             if (ROOT / d).is_dir()]
    roots += [ROOT / f for f in ROOT.iterdir()
              if f.is_file() and f.suffix in REWRITE_EXTS]
    n = 0
    stale: list[str] = []
    for root in roots:
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix not in REWRITE_EXTS:
                continue
            if any(p in REWRITE_SKIP_DIRS for p in f.parts):
                continue
            if f.name in REWRITE_SKIP_FILES:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            new = text
            for k in keys:
                if k in new:
                    new = new.replace(k, path_map[k])
            if new != text:
                f.write_text(new, encoding="utf-8")
                n += 1
    # 残留扫描（仅 kb 正文与代码，.history 豁免）
    for root in [KB, ROOT / "core", ROOT / "scripts"]:
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix not in REWRITE_EXTS:
                continue
            if ".history" in f.parts or f.name in REWRITE_SKIP_FILES:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(k in text for k in keys):
                stale.append(f.relative_to(ROOT).as_posix())
    return n, stale


# ---------------------------------------------------------------- 路由表


def rewrite_route_json(path_map: dict[str, str]) -> list[str]:
    """六个方向 route.json：值串替换 + 打散目录展开；返回告警清单。"""
    warns: list[str] = []
    for cap_dir in sorted(p for p in KB.iterdir() if p.is_dir()
                          and p.name not in ("licenses", "skills") and (p / "route.json").is_file()):
        cap = cap_dir.name
        rp = cap_dir / "route.json"
        data = json.loads(rp.read_text(encoding="utf-8"))
        expand = ROUTE_JSON_EXPAND.get(cap, {})
        for key, values in data.items():
            new_values: list[str] = []
            for v in values:
                if v in expand:
                    new_values.extend(expand[v])
                    continue
                if v in path_map:
                    new_values.append(path_map[v])
                    continue
                hit = next((ok for ok in sorted(path_map, key=len, reverse=True)
                            if v.startswith(ok)), None)
                if hit:
                    new_values.append(path_map[hit] + v[len(hit):])
                else:
                    new_values.append(v)
                    warns.append(f"{cap}/route.json [{key}]: 未映射 {v}")
            data[key] = new_values
        rp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return warns


_RIDX_RE = re.compile(r"^(\s*kb:\s*)(\S+)\s*$")


def rewrite_route_index(path_map: dict[str, str]) -> tuple[list[str], list[str]]:
    """route_index.yaml：kb: 值替换；返回 (告警, 悬空) 清单。"""
    rp = KB / "route_index.yaml"
    lines = rp.read_text(encoding="utf-8").splitlines()
    warns: list[str] = []
    out: list[str] = []
    for line in lines:
        m = _RIDX_RE.match(line)
        if not m:
            out.append(line)
            continue
        old = m.group(2)
        if old in path_map:
            new = path_map[old]
        else:
            hit = next((ok for ok in sorted(path_map, key=len, reverse=True)
                        if old.startswith(ok)), None)
            if hit:
                new = path_map[hit] + old[len(hit):]
            else:
                new = old
                warns.append(f"route_index.yaml: 未映射 {old}")
        out.append(m.group(1) + new)
    rp.write_text("\n".join(out) + "\n", encoding="utf-8")
    dangling = [m.group(2) for line in out
                if (m := _RIDX_RE.match(line)) and not (KB / m.group(2)).exists()]
    return warns, dangling


# ---------------------------------------------------------------- 主流程


def plan() -> list[tuple[str, str, str]]:
    """全部搬迁项 → (old, new, kind)。"""
    items: list[tuple[str, str, str]] = []
    items += [(o, n, "提取目录") for o, n in EXTRACT_DIR_MOVES]
    items += [(o, n, "目录改名") for o, n in DIR_MOVES]
    items += [(o, n, "拆分文件") for o, n in SPLIT_MOVES]
    items += [(o, n, "零散文件") for o, n in FILE_MOVES]
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正落盘（缺省 dry-run）")
    args = ap.parse_args()

    items = plan()
    existing = [(o, n, k) for o, n, k in items if (KB / o).exists()]
    already = [(o, n, k) for o, n, k in items if not (KB / o).exists()]

    print(f"kb 树状化重构：待搬迁 {len(existing)} 项，已完成/不存在 {len(already)} 项")
    if not args.apply:
        for o, n, k in existing:
            print(f"  [{k}] {o}  →  {n}")
        print(f"\nindex.md 同源去重特判 {len(INDEX_SPECIALS)} 项；"
              f"空壳目录清理 {len(EMPTY_DIRS)} 个；"
              f"route.json 打散展开 {sum(len(v) for v in ROUTE_JSON_EXPAND.values())} 组")
        print("（dry-run，加 --apply 执行）")
        return 0

    applied: list[tuple[str, str]] = []
    for old, new, kind in existing:
        how = git_mv(old, new)
        applied.append((old, new))
        if how == "skip":
            print(f"  [跳过] {old}（不存在）")
        elif how == "git-mv":
            print(f"  [{kind}] git mv {old} → {new}")
        else:
            print(f"  [{kind}] move {old} → {new}（未跟踪，无 git 历史）")

    # index.md 同源特判（此时 refs 已就位，可比对 SKILL.md）
    for idx, skill in INDEX_SPECIALS:
        p, s = KB / idx, KB / skill
        if not p.is_file():
            continue
        if s.is_file() and filecmp.cmp(p, s, shallow=False):
            git_rm(idx)
            print(f"  [去重] {idx} 与 {skill} 字节相同，删收编版副本")
        else:
            dst = skill.replace("SKILL.md", "SKILL-收编版.md")
            git_mv(idx, dst)
            print(f"  [特判] {idx} 内容有别于上游原件 → {dst}")

    # 题目知识原件与收编版去重（字节相同删原件副本，内容零损失）
    n_dedup = 0
    for cap_dir in sorted(p for p in KB.iterdir() if p.is_dir()):
        root = cap_dir / "赛事专题" / "题目知识"
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.name == "SKILL.md" or f.name.startswith("SKILL-"):
                continue
            rel = f.relative_to(cap_dir)
            # 在同方向知识模块里找同名文件（排除 赛事专题 自身）
            same = [c for c in cap_dir.rglob(f.name)
                    if c.is_file() and "赛事专题" not in c.parts and ".history" not in c.parts]
            if same and any(filecmp.cmp(f, c, shallow=False) for c in same):
                git_rm(rel.as_posix())
                n_dedup += 1
    if n_dedup:
        print(f"  [去重] 题目知识×收编版本字节相同 {n_dedup} 个原件副本已删")

    # 空壳目录清理
    for d in EMPTY_DIRS:
        p = KB / d
        if not p.is_dir():
            continue
        try:
            p.rmdir()
            print(f"  [清理] 空目录 {d}")
        except OSError:
            print(f"  [警告] {d} 非空，保留待人工检查")

    # web/后渗透/README.md 指路修正（intranet-postexp 已同模块）
    pe = KB / "web/后渗透/README.md"
    if pe.is_file():
        t = pe.read_text(encoding="utf-8")
        t2 = t.replace("`playbooks/intranet-postexp/`", "`intranet-postexp/`")
        if t2 != t:
            pe.write_text(t2, encoding="utf-8")
            print("  [修正] web/后渗透/README.md 指路更新")

    path_map = build_path_map(applied)
    n_rel = rewrite_rel_links(path_map)
    print(f"引用改写：相对链接 {n_rel} 处")

    warns = rewrite_route_json(path_map)
    for w in warns:
        print(f"  [告警] {w}")
    iwarns, dangling = rewrite_route_index(path_map)
    for w in iwarns:
        print(f"  [告警] {w}")
    for d in dangling:
        print(f"  [悬空] route_index kb: {d}")

    n_ref, stale = rewrite_refs(path_map)
    print(f"引用改写：全仓路径串 {n_ref} 个文件；旧串残留 {len(stale)} 个文件"
          f"（.history/历史脚本豁免，其余需人工看）")
    for s in stale[:20]:
        print("   ", s)

    print("\n下一步（人工）：core/skills/writing.py kind 推断、doctor.py refs 豁免、"
          "refs/kbindex/tool_registry 文档示例、import_kb.py 落点表、"
          "各方向 README/index.md 与 CLAUDE.md、跑测试。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
