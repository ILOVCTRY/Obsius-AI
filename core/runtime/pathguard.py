"""工作区路径守卫（DESIGN.md §7 工作区隔离与产物统一归置）。

静态提取命令文本中的**写目标**并判定是否逃逸工作区。设计取舍（2026-09-17 定稿）：
- 只拦「写」，读不拦（核心诉求=产物归置；读审计另行记录）；
- 硬拒绝落在网关层（gateway.run → GatewayDenied），Agent 收到指引后可改道重试；
- grep 族 `-o*`（only-matching）是输出开关不写文件，按命令词跟踪豁免
  （2026-09-23 误报修复：模式串前导 / 曾被当写目标拒「工作区逃逸」）；
  nmap 风格 -oG/-oN/-oX/-oA 写文件仍拦；
- 诚实边界：PowerShell/bash 任意构造（变量间接、编码、别名）无法静态穷尽，
  本模块是**强护栏不是沙箱**——真文件系统边界由持久 workspace 容器（E4b）承载。

纯函数无 IO：路径判定用 normpath/normcase，不触碰文件系统。
"""

import os
import posixpath
import re
from pathlib import PurePath

# PowerShell 写文件 cmdlet（小写匹配，token 去引号后比对）
_PS_WRITE_CMDS = {
    "out-file", "set-content", "add-content", "tee-object", "new-item",
    "export-csv", "export-clixml", "export-pfxcertificate", "set-variable",
}
# 参数式写目标（curl/wget/iwr；含 = 连写形式在提取时拆分）
_PARAM_WRITE_FLAGS = {"-o", "--output", "-o=", "--output=", "-o ", "-outfile",
                      "--outfile", "-o=", "--output-document", "-o="}
_FLAG_PREFIXES = ("-o", "--output", "-outfile", "--outfile")

# grep 族（-o/-oE/-oN/--only-matching = only-matching 输出开关，不写文件）
_GREP_CMDS = {"grep", "egrep", "fgrep", "zgrep", "rg", "ripgrep"}

# 重定向操作符：> >> 1> 2> 1>> 2>>（在 token 边界或 token 内）
_REDIRECT_RE = re.compile(r"(?:(?<=^)|(?<=\s))(\d?){1,2}?>{1,2}")

ALLOWED_SPECIAL_TARGETS = {"&1", "&2", "/dev/null", "nul", "nul.", "con", "$null"}


def _split_tokens(cmd: str) -> list[str]:
    """按空白切 token，尊重单/双引号（引号保留在 token 里，判定时再剥）。"""
    toks: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    for ch in cmd:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch.isspace():
            if cur:
                toks.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        toks.append("".join(cur))
    return toks


def _clean(tok: str) -> str:
    return tok.strip().strip("\"'").strip()


def _trim_shell_separator(target: str) -> str:
    """重定向目标在**引号外**的首个 shell 分隔符（; | &）处结束。
    2>/dev/null;、>f.txt|wc 这类无空格粘连写法整串是一个 token，不切掉会把
    /dev/null; 误判逃逸（2026-09-25 实战 23/43 条误拦实锤）。"""
    quote: str | None = None
    for i, ch in enumerate(target):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in ";|&":
            return target[:i]
    return target


def _is_flag(tok: str, posix: bool = False) -> bool:
    if posix:
        return tok.startswith("-")  # POSIX 下 /path 是路径不是 flag
    return tok.startswith("-") or tok.startswith("/")


def _cmd_word(low: str) -> str:
    """命令词归一：剥路径段与 .exe 后缀（/usr/bin/grep、grep.exe → grep）。"""
    w = re.split(r"[/\\]", low)[-1]
    return w[:-4] if w.endswith(".exe") else w


def scan_write_targets(cmd: str, posix: bool = False) -> list[str]:
    """提取命令中的写目标 token（原始串，含引号）。启发式：宁可多报不漏报。

    posix=True 时按 bash 语义解析（/path 是绝对路径不是 flag，如 WSL 命令）。
    2026-09-23 增命令词跟踪：管道/分号重置命令边界，当前段命令词属于
    grep 族时豁免 -o* 写判定（only-matching 不写文件）。
    """
    targets: list[str] = []
    toks = _split_tokens(cmd)
    i = 0
    cur_cmd: str | None = None  # 当前段命令词（管道/分号重置；grep 族豁免用）
    while i < len(toks):
        raw = toks[i]
        low = _clean(raw).lower()

        # ⓪ 命令边界跟踪：管道/分号/逻辑符重置（含 |grep 连写形态，& 兼顾
        #    cmd1&&cmd2 与 URL 查询串的保守退化）；每段首个非 flag token 记为命令词
        if low in {"|", "||", "&&", ";"}:
            cur_cmd = None
        elif "|" in raw or ";" in raw or "&" in raw:
            seg = _clean(re.split(r"[|;&]", raw)[-1]).lower()
            cur_cmd = _cmd_word(seg) if seg and not _is_flag(seg, posix=posix) else None
        elif cur_cmd is None and not _is_flag(_clean(raw), posix=posix):
            cur_cmd = _cmd_word(low)

        # ① 重定向：> >> 1> 2> 1>>（独立 token 或 token 内，如 ">out.txt"、"2>err"）
        stripped = raw.lstrip("0123456789")  # 剥流号前缀 1> 2>
        if stripped.startswith(">"):
            rest = stripped[1:].lstrip(">")
            rest = _trim_shell_separator(rest)
            if rest.strip("\"'"):
                targets.append(rest)
            elif i + 1 < len(toks):
                targets.append(toks[i + 1])
            i += 1
            continue

        # ② PowerShell 写 cmdlet / 参数式写 flag（含 nmap 风格 -oG/-oN 连写、
        #    curl -o/--output、iwr -OutFile、--output=path 连写）：目标在下一个非 flag token
        #    low.lstrip("-").startswith("o") 同时覆盖 -o / --output / -outfile / -oG
        if low in _PS_WRITE_CMDS or low.lstrip("-").startswith("o"):
            # grep 族豁免（2026-09-23 误报修复）：-o/-oE/-oN/--only-matching 是
            # only-matching 输出开关不写文件；nmap 风格 -oG/-oN 写文件仍拦
            if not (cur_cmd in _GREP_CMDS and low.startswith("-")):
                if "=" in low:
                    val = _clean(raw).split("=", 1)[1]
                    if val:
                        targets.append(val)
                elif i + 1 < len(toks):
                    nxt = toks[i + 1]
                    if not _is_flag(_clean(nxt), posix=posix):
                        targets.append(nxt)
            i += 1
            continue

        # ③ bash tee / tee -a：目标在后续非 flag token
        if low == "tee" or low.startswith("tee "):
            j = i + 1
            while j < len(toks) and _is_flag(_clean(toks[j]), posix=posix):
                j += 1
            if j < len(toks):
                targets.append(toks[j])
            i += 1
            continue

        i += 1
    return targets


def _normcase(p: str, posix: bool) -> str:
    if posix:
        return posixpath.normpath(p)
    return os.path.normcase(os.path.normpath(p))


def _inside(child: str, parent: str, posix: bool) -> bool:
    child_n = _normcase(child, posix)
    parent_n = _normcase(parent, posix)
    if posix:
        return child_n == parent_n or child_n.startswith(parent_n.rstrip("/") + "/")
    sep = os.sep
    return child_n == parent_n or child_n.startswith(parent_n.rstrip(sep) + sep)


def workspace_escapes(
    cmd: str,
    *,
    scratch: str,
    workspace: str,
    posix: bool = False,
) -> list[str]:
    """返回命令中会写到工作区外的目标列表；空列表=放行。

    判定规则（2026-09-17 定稿）：
    - 绝对路径（盘符 / UNC / 前导 / / ~）：不在 workspace 内 → 逃逸；
    - 相对路径：按 cwd=scratch 解析后必须仍在 scratch 内（防 .. 上穿；
      正式产物走 bb_add_artifact，不鼓励相对写到 scratch 外）；
    - `$env:` / `${}` 变量目标无法静态解析 → 放行（TEMP/TMP 已被网关重定向进项目，
      主要逃逸向量已覆盖；残余风险由审计与容器终态承接）；
    - 特殊目标（&1 &2 /dev/null NUL $null）放行。
    """
    escapes: list[str] = []
    for raw in scan_write_targets(cmd, posix=posix):
        t = _clean(raw)
        if not t or t in ALLOWED_SPECIAL_TARGETS:
            continue
        if t.startswith("-"):  # 又一个 flag，不是目标
            continue
        if "://" in t:  # URL
            continue
        if "$" in t:    # 变量间接，静态不可判（TEMP 已重定向，放行）
            continue
        if "%" in t and not posix:  # cmd 风格变量同理
            continue
        # ~ 家目录：一律视为逃逸（工作区外）
        if t.startswith("~"):
            escapes.append(t)
            continue
        is_abs = False
        if re.match(r"^[A-Za-z]:[\\/]", t):
            is_abs = True
        elif t.startswith("\\\\"):
            is_abs = True
        elif t.startswith("/") and not posix:
            # Windows PowerShell 下 /foo 解析到当前驱动器根 = 工作区外
            escapes.append(t)
            continue
        if posix and t.startswith("/"):
            is_abs = True
        if is_abs:
            if posix:
                # WSL 侧：workspace 传入 wsl 路径（/mnt/...）
                if not _inside(t, workspace, posix=True):
                    escapes.append(t)
            else:
                if not _inside(t, workspace, posix=False):
                    escapes.append(t)
            continue
        # 相对路径：按 scratch 解析，越出 scratch 即拒
        if posix:
            resolved = posixpath.normpath(posixpath.join(scratch, t))
            if not _inside(resolved, scratch, posix=True):
                escapes.append(t)
        else:
            resolved = os.path.normcase(os.path.normpath(os.path.join(scratch, t)))
            if not _inside(resolved, scratch, posix=False):
                escapes.append(t)
    return escapes


def windows_to_wsl_path(path: str | PurePath) -> str:
    r"""E:\workspaces\a\scratch → /mnt/e/workspaces/a/scratch（WSL 默认挂载约定）。"""
    p = str(path).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        p = f"/mnt/{drive}{p[2:]}"
    return p
