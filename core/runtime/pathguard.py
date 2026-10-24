"""工作区路径守卫（DESIGN.md §7 工作区隔离与产物统一归置）。

静态提取命令文本中的**写目标**并判定是否逃逸工作区。设计取舍（2026-09-17 定稿）：
- 只拦「写」，读不拦（核心诉求=产物归置；读审计另行记录）；
- 硬拒绝落在网关层（gateway.run → GatewayDenied），Agent 收到指引后可改道重试；
- 重定向目标在引号外的 shell 分隔符处截断（; | & 2026-09-25 实战 23/43 条
  误拦实锤；`)` 2026-09-29 补——`$(curl … 2>/dev/null)` 子壳右括号紧贴重定向
  目标提取出伪路径 /dev/null) 绕过白名单，三连拒致 E2 熔断挂起）；
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

#: 重定向操作符：> >> 1> 2> 1>> 2>>（在 token 边界或 token 内）
_REDIRECT_RE = re.compile(r"(?:(?<=^)|(?<=\s))(\d?){1,2}?>{1,2}")

ALLOWED_SPECIAL_TARGETS = {"&1", "&2", "/dev/null", "nul", "nul.", "con", "$null"}

#: 会把 stdin/heredoc **当代码执行**的命令词（2026-10-23，heredoc 判据）。
#: 判据来源：这些命令把 heredoc 正文当**程序**读；反之 awk/grep/sed/cat/jq 等
#: 把 stdin 当**数据**读，正文里的 `> /etc/passwd` 永远只是一串字符。
_EXECUTOR_WORDS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "busybox", "mksh",
    "python", "python2", "python3", "py", "perl", "ruby", "node", "nodejs",
    "php", "lua", "rscript", "tclsh", "powershell", "pwsh",
    "source", "eval", ".",
})

#: 执行前的包装词：跳过它们继续找真正的解释器（`| sudo bash`、`| env bash`、
#: `| timeout 5 bash`、`| xargs -I{} bash`）。只跳过**自己不吃参数值**的，
#: 所以 timeout/nice/xargs 这类带数值参数的也要跳（下一个 token 是参数就继续跳）。
_EXEC_WRAPPERS = frozenset({
    "sudo", "doas", "env", "nohup", "time", "timeout", "nice", "ionice",
    "xargs", "stdbuf", "command", "exec", "builtin", "runuser", "su",
})


def _executes_stdin(host_line: str, posix: bool) -> bool:
    """这一行的 heredoc 正文**是否会被当成代码执行**。

    逐段找**真正的命令词**：跳过前导参数、跳过包装词（sudo/env/timeout…）
    **以及它们的参数**，第一个非包装词就是命令词。
    不看整行任意位置——否则 `cat <<EOF > /tmp/x.sh` 里 `x.sh` 之类噪音会误判。

    `timeout 5 bash` 的 `5` 是 timeout 的参数、不是命令词，所以包装词后要
    继续跳到「下一个词」并**再判一次包装词**，直到遇到真命令词为止。
    """
    for seg in _split_segments(host_line):
        toks = [_clean(t) for t in _split_tokens(seg)]
        idx = 0
        while idx < len(toks):
            c = toks[idx]
            if not c or c.startswith("<") or c.startswith(">"):
                idx += 1
                continue
            # 这里只跳「以 - 开头」的参数，**不能**用 `_is_flag`：非 posix 下
            # `_is_flag` 还会把 `/x` 当 flag，会把 `/bin/bash` 这种命令词跳过。
            if c.startswith("-"):
                idx += 1
                continue
            word = _cmd_word(c.lower())
            if word in _EXECUTOR_WORDS:
                return True
            if word in _EXEC_WRAPPERS:
                # 跳过该包装词自己的参数（数值/`VAR=VAL`/非路径 token），
                # 再继续找真命令词
                idx += 1
                while idx < len(toks):
                    nxt = toks[idx]
                    if not nxt or nxt.startswith("<") or nxt.startswith(">"):
                        idx += 1
                        continue
                    if nxt.startswith("-"):
                        idx += 1
                        continue
                    if _cmd_word(nxt.lower()) in _EXECUTOR_WORDS:
                        return True
                    # 数值参数（timeout 5 / nice -n 10 已由 - 分支跳掉）继续跳；
                    # 遇到明显的非执行命令词就认定它是这条命令本身
                    if re.fullmatch(r"\d+(?:\.\d+)?", nxt) or "=" in nxt:
                        idx += 1
                        continue
                    break
                continue
            break              # 段内首个真命令词不是解释器 → 正文只是数据
    return False


def _line_index(starts: list[int], pos: int) -> int:
    """字符偏移 → 行号（starts 为各行起始偏移，升序）。"""
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= pos:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _find_heredoc_markers(text: str) -> list[tuple[int, int, str, bool]]:
    """找引号外的 heredoc 标记，返回 [(start, end, 定界词, 是否剥离前导 tab)]。

    三条**必须**的排除（漏一条就会把命令剩余部分吞掉，导致真逃逸漏判）：
    - `<<<` 是 herestring（把字符串喂给 stdin），不是 heredoc；
    - `$((1<<2))` 里的 `<<` 是左移；整段 `$(( … ))` 跳过；
    - 定界词只认字母/下划线开头（bash 虽允许纯数字，但 `$((a<<b))` 与它
      无法区分，且实际手册/脚本里几乎不用数字定界词）。
    """
    out: list[tuple[int, int, str, bool]] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if text.startswith("$((", i):
            # 跳过算术展开（内部 << 是左移）
            depth, j = 0, i + 1
            while j < n:
                if text.startswith("((", j):
                    depth += 1
                    j += 2
                    continue
                if text.startswith("))", j):
                    depth -= 1
                    j += 2
                    if depth <= 0:
                        break
                    continue
                j += 1
            i = j
            continue
        if text.startswith("<<<", i):
            i += 3
            continue
        if text.startswith("<<", i):
            j = i + 2
            strip_tabs = False
            if j < n and text[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and text[j] in " \t":
                j += 1
            q: str | None = None
            if j < n and text[j] in "\"'":
                q = text[j]
                j += 1
            k = j
            while k < n and (text[k].isalnum() or text[k] == "_"):
                k += 1
            word = text[j:k]
            ok = bool(word) and (word[0].isalpha() or word[0] == "_")
            if ok and q:
                if k < n and text[k] == q:
                    k += 1
                else:
                    ok = False        # 引号没闭合，不是合法 heredoc
            if ok:
                out.append((i, k, word, strip_tabs))
                i = k
                continue
        i += 1
    return out


def _split_segments(line: str) -> list[str]:
    """按引号外的 `|` `;` `&&` `||` 切段（用于找各段命令词）。"""
    segs: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch in ";|&":
            segs.append("".join(cur))
            cur = []
            i += 1
            while i < n and line[i] in ";|&":
                i += 1
            continue
        cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return segs


def split_heredocs(cmd: str) -> tuple[str, list[tuple[str, str, str]]]:
    """把命令拆成 (**外层命令**, [(定界词, 正文, 起始行)])。

    heredoc 正文是**数据**，不是命令——正文里的 `> /etc/passwd` 在
    `cat <<EOF > a.sh` 里只是一串字符，永远不会被执行。原实现把整段按同一
    套判据扫，于是：**正文被误当逃逸拒**，而 `cat <<EOF | bash`（正文真的
    会执行）却和普通写在同一个桶里判（2026-10-23 实测）。

    未闭合（找不到定界符行）时**原样返回、不做任何剥离**——宁可漏剥不可错剥：
    错剥会把命令剩余部分整个吞掉，那才是真漏判逃逸。

    嵌套形态按 bash 语义处理：正文里出现的 `<<X` 是**字面量**，不再开新 heredoc
    （顺序扫描、进入正文后只找定界符行）。
    """
    markers = _find_heredoc_markers(cmd)
    if not markers:
        return cmd, []
    lines = cmd.split("\n")
    starts: list[int] = []
    pos = 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1

    by_line: dict[int, list[tuple[str, bool]]] = {}
    for mk_start, _mk_end, word, tabs in markers:
        by_line.setdefault(_line_index(starts, mk_start), []).append((word, tabs))

    keep: list[str] = []
    bodies: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        host = lines[i]
        keep.append(host)
        specs = by_line.get(i)
        i += 1
        if not specs:
            continue
        for word, tabs in specs:
            body_lines: list[str] = []
            found = False
            while i < len(lines):
                cur = lines[i]
                probe = cur.lstrip("\t") if tabs else cur
                if probe == word:
                    found = True
                    i += 1
                    break
                body_lines.append(cur)
                i += 1
            if not found:
                return cmd, []      # 定界符未闭合 → 整体放弃剥离
            bodies.append((word, "\n".join(body_lines), host))
    return "\n".join(keep), bodies



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
    """重定向目标在**引号外**的首个 shell 分隔符（; | & )）处结束。
    2>/dev/null;、>f.txt|wc 这类无空格粘连写法整串是一个 token，不切掉会把
    /dev/null; 误判逃逸（2026-09-25 实战 23/43 条误拦实锤）；
    `)` 同理——`$(curl … 2>/dev/null)` 子壳右括号紧贴重定向目标是 bash
    高频惯用法，不切掉会提取出伪路径 /dev/null) 绕过 /dev/null 白名单
    （2026-09-29 实战 sess-948e9ba771eb 三连拒致 E2 熔断挂起实锤）。
    截断只会让提取目标更准（>/tmp/x) → /tmp/x 仍判逃逸），无新旁路。"""
    quote: str | None = None
    for i, ch in enumerate(target):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in ";|&)":
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
    allow_posix_tmp: bool = False,
) -> list[str]:
    """返回命令中会写到工作区外的目标列表；空列表=放行。

    判定规则（2026-09-17 定稿）：
    - 绝对路径（盘符 / UNC / 前导 / / ~）：不在 workspace 内 → 逃逸；
    - 相对路径：按 cwd=scratch 解析后必须仍在 scratch 内（防 .. 上穿；
      正式产物走 bb_add_artifact，不鼓励相对写到 scratch 外）；
    - `$env:` / `${}` 变量目标无法静态解析 → 放行（TEMP/TMP 已被网关重定向进项目，
      主要逃逸向量已覆盖；残余风险由审计与容器终态承接）；
    - 特殊目标（&1 &2 /dev/null NUL $null）放行；
    - **heredoc 正文默认不扫描**（2026-10-23）：正文是数据，`cat <<EOF > a.sh`
      里写的 `> /etc/passwd` 只是一串字符、永不执行。只有正文**会被当代码执行**
      （`<<EOF | bash`、`bash <<EOF`、`python3 <<EOF`、`source /dev/stdin` 等）
      时才按同一套判据扫描正文——那才是真逃逸。
    """
    escapes: list[str] = []
    seen: set[str] = set()
    outer, bodies = split_heredocs(cmd)
    # 外层命令（剥离正文后）+ 会执行的正文，都走同一套判据
    scan_inputs: list[str] = [outer]
    for _word, body, host in bodies:
        if body and _executes_stdin(host, posix):
            scan_inputs.append(body)
    for text in scan_inputs:
        for raw in scan_write_targets(text, posix=posix):
            t = _trim_shell_separator(_clean(raw))
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
            # 剥离后仍可能残留引号/反引号（`\`cmd\`` 与未闭合引号），再清一道：
            # 只影响比对，不影响「是否允许」的判据。
            t = t.strip("\"'`").strip()
            if not t:
                continue
            # ~ 家目录：一律视为逃逸（工作区外）
            if t.startswith("~"):
                if t not in seen:
                    escapes.append(t)
                    seen.add(t)
                continue
            is_abs = False
            if re.match(r"^[A-Za-z]:[\\/]", t):
                is_abs = True
            elif t.startswith("\\\\"):
                is_abs = True
            elif t.startswith("/") and not posix:
                # Windows PowerShell 下 /foo 解析到当前驱动器根 = 工作区外
                if t not in seen:
                    escapes.append(t)
                    seen.add(t)
                continue
            if posix and t.startswith("/"):
                if allow_posix_tmp and (t == "/tmp" or t.startswith("/tmp/")):
                    continue
                is_abs = True
            if is_abs:
                # 同一目标多次出现只报一次（回执不重复抖串）
                if t in seen:
                    continue
                if posix:
                    # WSL 侧：workspace 传入 wsl 路径（/mnt/...）
                    if not _inside(t, workspace, posix=True):
                        escapes.append(t)
                        seen.add(t)
                else:
                    if not _inside(t, workspace, posix=False):
                        escapes.append(t)
                        seen.add(t)
                continue
            # 相对路径：按 scratch 解析，越出 scratch 即拒
            if t in seen:
                continue
            if posix:
                resolved = posixpath.normpath(posixpath.join(scratch, t))
                if not _inside(resolved, scratch, posix=True):
                    escapes.append(t)
                    seen.add(t)
            else:
                resolved = os.path.normcase(os.path.normpath(os.path.join(scratch, t)))
                if not _inside(resolved, scratch, posix=False):
                    escapes.append(t)
                    seen.add(t)
    return escapes


def windows_to_wsl_path(path: str | PurePath) -> str:
    r"""E:\workspaces\a\scratch → /mnt/e/workspaces/a/scratch（WSL 默认挂载约定）。"""
    p = str(path).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        drive = p[0].lower()
        p = f"/mnt/{drive}{p[2:]}"
    return p
