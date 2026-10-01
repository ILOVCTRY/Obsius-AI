# Ghidra postScript（Jython）v3：全量导出函数表 + 伪码 + 字符串 → JSON
# 用法：analyzeHeadless <dir> <proj> -import <bin> -scriptPath tools/decompiler/ghidra/scripts \
#         -postScript export_funcs.py <out.json> -deleteProject
# 输出契约（DESIGN.md §9）：
#   {export_version:3, binary, meta, sections[], imports{dll:[fn]}, functions[], strings[]}
# meta/sections/imports/strings 为增强段，各自 try/except——失败给 {"error": ...}，
# 绝不阻断 functions 主路径。
import io
import json
import math
import os
from collections import Counter

import jarray
from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor

OUT = args[0]
prog = currentProgram
fm = prog.getFunctionManager()
monitor = ConsoleTaskMonitor()


def shannon_entropy(buf, n):
    if not n:
        return None
    counts = Counter()
    for i in range(n):
        counts[buf[i] & 0xFF] += 1  # jbyte 有符号
    return round(-sum((c / float(n)) * math.log(c / float(n), 2) for c in counts.values()), 3)


def collect_meta():
    # LanguageID 形如 "x86:LE:64:default" / "AARCH64:LE:64:v8A" / "ARM:LE:32:v8"
    toks = prog.getLanguageID().getIdAsString().split(":")
    proc, endian_tok, bits_tok = toks[0], toks[1], toks[2]
    bits = int(bits_tok)
    if proc == "x86":
        arch = "x86-64" if bits == 64 else "x86"
    elif proc == "AARCH64":
        arch = "arm64"
    elif proc == "ARM":
        arch = "arm"
    else:
        arch = proc.lower()
    entry = None
    # 外部入口点列表含动态导出，首个不一定是 e_entry；先找 _start 符号。
    syms = prog.getSymbolTable()
    for cand in ("_start", "start", "_start_proc"):
        it = syms.getSymbols(cand)
        if it is not None and it.hasNext():
            entry = hex(it.next().getAddress().getOffset())
            break
    if entry is None:
        eps = prog.getSymbolTree().getExternalEntryPointIterator()
        if eps.hasNext():
            entry = hex(eps.next().getOffset())
    return {
        "arch": arch,
        "bits": bits,
        "endian": "be" if endian_tok == "BE" else "le",
        "imagebase": hex(prog.getImageBase().getOffset()),
        "entry": entry,
        "filename": prog.getName(),
    }


def collect_sections():
    out = []
    mem = prog.getMemory()
    for block in mem.getBlocks():
        size = block.getSize()
        perms = (("r" if block.isRead() else "-")
                 + ("w" if block.isWrite() else "-")
                 + ("x" if block.isExecute() else "-"))
        entropy = None
        try:
            n = min(size, 1 << 20)
            buf = jarray.zeros(n, "b")
            got = mem.getBytes(block.getStart(), buf)
            entropy = shannon_entropy(buf, got if got and got > 0 else n)
        except Exception:
            entropy = None
        out.append({
            "name": block.getName(),
            "vaddr": hex(block.getStart().getOffset()),
            "size": size,
            "perms": perms,
            "entropy": entropy,
        })
    return out


def collect_imports():
    imports = {}
    ext = prog.getExternalManager()
    for lib in ext.getExternalLibraryNames():
        names = []
        for loc in ext.getExternalLocations(lib):
            label = loc.getLabel()
            if label:
                names.append(label)
            else:
                names.append("#%s" % loc.getOrdinal())
        imports[lib] = names
    return imports


def collect_strings():
    # v3：已定义字符串 + 引用归属函数（import 放函数内：缺失/版本差异时整段给 error）
    from ghidra.program.util import DefinedDataIterator

    out = []
    refmgr = prog.getReferenceManager()
    for data in DefinedDataIterator.definedStrings(prog):
        addr = data.getMinAddress()
        val = data.getValue()
        dtname = data.getDataType().getName().lower()
        stype = "unicode" if ("unicode" in dtname or "wide" in dtname
                              or "wchar" in dtname) else "cstr"
        refs = {}
        for ref in refmgr.getReferencesTo(addr):
            from_addr = ref.getFromAddress()
            func = fm.getFunctionContaining(from_addr)
            if func is not None:
                refs[func.getEntryPoint().getOffset()] = from_addr.getOffset()
        out.append({
            "address": addr.getOffset(),
            "string": ("" if val is None else str(val)),
            "length": data.getLength(),
            "type": stype,
            "refs": [{"func_addr": fea, "from_addr": fr}
                     for fea, fr in sorted(refs.items())],
        })
    return out


# P2/P3 大样本并行分片 + 协作式停止 + 进度（2026-09-30）：
# postScript 可选参数位：args[1]=worker 数，args[2]=进度文件，args[3]=停止标志文件，
# args[4]="1" 时内嵌每函数反汇编（小样本，见下 WANT_DISASM）。
# 缺参/非法一律回退：WORKERS=1（串行）、无进度/无停止。父进程写 args[3] 即请求协作式
# 停止——脚本在函数边界检查（非杀进程），停下后仍写出「已完成函数」的 v3 缓存（函数名/
# 地址全量，伪码只到停点），meta 标 partial/stopped，父进程自然返回。
WORKERS = 1
PROGRESS = None
STOP = None
if len(args) > 1:
    try:
        WORKERS = max(1, int(args[1]))
    except (TypeError, ValueError):
        WORKERS = 1
if len(args) > 2:
    PROGRESS = args[2]
if len(args) > 3:
    STOP = args[3]
# 脚本自驱分析（2026-10-01 工作台可中断）：args[4]="1" 时由本脚本跑完整自动分析
# （父进程用 analyzeHeadless -noanalysis 导入），配自定义 TaskMonitor 轮询停止文件
# → 分析阶段即可中断 + 上报进度（phase=analyzing）。
ANALYZE = len(args) > 4 and str(args[4]) == "1"
# 反汇编内嵌（2026-10-01 工作台 Ghidra 模式）：args[5]="1" 时随全量导出为每个函数补
# disasm_lines（单函数上限 DISASM_MAX_LINES，超出置 disasm_truncated）。仅小样本走此
# 路径（大样本由父进程改为持久工程按需反汇编，避免缓存膨胀）。
WANT_DISASM = len(args) > 5 and str(args[5]) == "1"
DISASM_MAX_LINES = 400

STOP_FLAG = [False]


def _stop_requested():
    if STOP_FLAG[0]:
        return True
    if STOP and os.path.exists(STOP):
        STOP_FLAG[0] = True
        return True
    return False


def _write_progress():
    if not PROGRESS:
        return
    try:
        done = 0
        for it in functions:
            if "pseudocode" in it:
                done += 1
        payload = {"done": done, "total": len(functions),
                   "phase": "stopped" if STOP_FLAG[0] else "decompile"}
        with io.open(PROGRESS, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def _disasm_lines(f):
    """指令级反汇编行（`地址  指令`），单函数上限 DISASM_MAX_LINES。返回 (lines, truncated)。"""
    lines = []
    truncated = False
    try:
        it = prog.getListing().getInstructions(f.getBody(), True)
        n = 0
        while it.hasNext():
            if n >= DISASM_MAX_LINES:
                truncated = True
                break
            ins = it.next()
            lines.append("%s  %s" % (ins.getAddress().toString(), ins.toString()))
            n += 1
    except Exception:
        pass
    return lines, truncated


def _enrich(f, dec, mon, item):
    # 分片重试（P3）：单函数反编译失败重试一次（瞬时解编译器故障自愈），仍失败则
    # 留 calls=[] 且无伪码，不拖垮整次导出。
    try:
        item["calls"] = sorted(set(c.getName() for c in f.getCalledFunctions(mon)))
    except Exception:
        pass
    if WANT_DISASM:
        try:
            item["disasm_lines"], item["disasm_truncated"] = _disasm_lines(f)
        except Exception:
            pass
    for _ in range(2):
        try:
            res = dec.decompileFunction(f, 60, mon)
            if res.decompileCompleted() and res.getDecompiledFunction() is not None:
                item["pseudocode"] = res.getDecompiledFunction().getC()
                return
        except Exception:
            pass


# ---- 脚本自驱分析（2026-10-01 可中断）：-noanalysis 导入后由本脚本跑分析 ----
_ANALYSIS_STOPPED = False
if ANALYZE:
    from ghidra.app.plugin.core.analysis import AutoAnalysisManager
    from ghidra.util.task import TaskMonitorAdapter

    class _StopMonitor(TaskMonitorAdapter):
        """自定义 TaskMonitor：轮询停止文件（分析阶段可中断）+ 节流上报分析进度。"""

        def __init__(self):
            TaskMonitorAdapter.__init__(self, True)  # cancelEnabled=True
            self._last = 0.0

        def isCancelled(self):
            return TaskMonitorAdapter.isCancelled(self) or _stop_requested()

        def cancel(self):
            TaskMonitorAdapter.cancel(self)

        def _report(self):
            if not PROGRESS:
                return
            import time as _t
            now = _t.time()
            if now - self._last < 1.0:
                return
            self._last = now
            try:
                payload = {"phase": "analyzing",
                           "done": int(self.getProgress() or 0),
                           "total": int(self.getMaximum() or 0)}
                with io.open(PROGRESS, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False))
            except Exception:
                pass

        def setMessage(self, msg):
            TaskMonitorAdapter.setMessage(self, msg)
            self._report()

        def setProgress(self, value):
            TaskMonitorAdapter.setProgress(self, value)
            self._report()

        def setMaximum(self, value):
            TaskMonitorAdapter.setMaximum(self, value)
            self._report()

    if PROGRESS:
        try:
            with io.open(PROGRESS, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"phase": "analyzing", "done": 0, "total": 0},
                                    ensure_ascii=False))
        except Exception:
            pass
    try:
        _mgr = AutoAnalysisManager.getAnalysisManager(prog)
        _mgr.startAnalysis(_StopMonitor())
    except Exception as _e:  # noqa: BLE001 —— 分析启动失败回退（不可中断但至少出结果）
        print("script-driven analysis failed, fallback analyzeAll: %s" % _e)
        try:
            analyzeAll(prog)
        except Exception as _e2:
            print("analyzeAll failed: %s" % _e2)
    _ANALYSIS_STOPPED = _stop_requested()

funcs = list(fm.getFunctions(True))
# 基础项预填充（P3）：地址/名/大小先全量落好，worker 只补 calls/伪码——协作式停止时
# 函数清单仍是完整体（名字/地址全量），仅部分缺伪码，便于「停止保留部分」立即可见。
functions = []
for f in funcs:
    try:
        functions.append({"address": f.getEntryPoint().getOffset(),
                          "name": f.getName(),
                          "size": f.getBody().getNumAddresses(),
                          "calls": []})
    except Exception:
        functions.append({"address": 0, "name": "", "size": 0, "calls": []})

import threading

_reporter_done = threading.Event()


def _reporter():
    while not _reporter_done.is_set():
        _write_progress()
        _reporter_done.wait(1.0)


_reporter_thread = threading.Thread(target=_reporter)
if PROGRESS:
    _reporter_thread.start()

try:
    if _ANALYSIS_STOPPED:
        pass  # 分析阶段被停止：仅保留已识别函数清单，不再反编译（partial）
    elif WORKERS > 1 and len(functions) > 1:
        # Jython 的 threading 映射到 Java 线程；重活在 Java 侧（decompileFunction）
        # 执行时会释放监视器，故并行有效。work 分配用原子游标，避免静态分片的长尾。
        cursor = [0]
        lock = threading.Lock()

        def _worker():
            dec = DecompInterface()
            dec.openProgram(prog)
            mon = ConsoleTaskMonitor()
            while not _stop_requested():
                lock.acquire()
                try:
                    i = cursor[0]
                    cursor[0] += 1
                finally:
                    lock.release()
                if i >= len(functions):
                    break
                _enrich(funcs[i], dec, mon, functions[i])
            try:
                dec.dispose()
            except Exception:
                pass

        threads = [threading.Thread(target=_worker)
                   for _ in range(min(WORKERS, len(functions)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        dec = DecompInterface()
        dec.openProgram(prog)
        for i in range(len(functions)):
            if _stop_requested():
                break
            _enrich(funcs[i], dec, monitor, functions[i])
finally:
    _reporter_done.set()
    if PROGRESS:
        try:
            _reporter_thread.join(3)
        except Exception:
            pass
    _write_progress()  # 末次进度（STOP_FLAG 已定）

STOPPED = STOP_FLAG[0]

result = {"export_version": 3, "binary": prog.getName(), "functions": functions,
          "partial": STOPPED, "stopped": STOPPED, "total_functions": len(functions)}
for key, fn in (("meta", collect_meta), ("sections", collect_sections),
                ("imports", collect_imports), ("strings", collect_strings)):
    try:
        result[key] = fn()
    except Exception as e:
        result[key] = {"error": "%s: %s" % (type(e).__name__, e)}
# 停止标记并入 meta（collect_meta 成功时为 dict；失败时靠顶层 partial/stopped 兜底）
if isinstance(result.get("meta"), dict) and "error" not in result["meta"]:
    result["meta"]["partial"] = STOPPED
    result["meta"]["stopped"] = STOPPED
    result["meta"]["total_functions"] = len(functions)

# 必须显式 UTF-8（Jython 用 io.open）：Windows host 裸 open 默认 GBK，
# 人工中文命名/注释会写成非 UTF-8 字节，后端读缓存即 UnicodeDecodeError。
with io.open(OUT, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(result, ensure_ascii=False))
print("exported %d functions -> %s" % (len(functions), OUT))
