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
import threading
from collections import Counter

try:
    import jarray  # type: ignore
    PYGHIDRA = False
except ImportError:
    # Ghidra 12 uses PyGhidra/CPython instead of the removed Jython runtime.
    # Keep the old API shape for section entropy collection.
    from jpype import JArray, JByte
    PYGHIDRA = True

    class _JArrayCompat(object):
        @staticmethod
        def zeros(n, _kind):
            return JArray(JByte)(n)

    jarray = _JArrayCompat()
from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor

args = list(getScriptArgs())
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
        try:
            eps = syms.getExternalEntryPointIterator()
            if eps.hasNext():
                entry = hex(eps.next().getOffset())
        except Exception:
            # Older Ghidra builds expose this iterator through SymbolTree.
            try:
                eps = prog.getSymbolTree().getExternalEntryPointIterator()
                if eps.hasNext():
                    entry = hex(eps.next().getOffset())
            except Exception:
                pass
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
    # v3：已定义字符串 + 引用归属函数。Ghidra 12 removed
    # DefinedDataIterator.definedStrings(), so prefer Listing.getDefinedData()
    # and keep the old helper as a compatibility fallback.
    out = []
    refmgr = prog.getReferenceManager()
    iterator = None
    try:
        iterator = prog.getListing().getDefinedData(True)
    except Exception:
        try:
            from ghidra.program.util import DefinedDataIterator
            iterator = DefinedDataIterator.definedStrings(prog)
        except Exception:
            iterator = None
    if iterator is None:
        return out
    while iterator.hasNext():
        data = iterator.next()
        try:
            dtname = data.getDataType().getName().lower()
            val = data.getValue()
            # getDefinedData() includes integers, pointers and structs.  Keep
            # actual string data, plus custom string types whose value is text.
            is_text_type = ("string" in dtname or "unicode" in dtname
                            or "wide" in dtname or "wchar" in dtname
                            or "char" in dtname)
            if not is_text_type and not isinstance(val, str):
                continue
            if val is None:
                continue
        except Exception:
            continue
        addr = data.getMinAddress()
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
# 反汇编：args[5]="1" 时内嵌 disasm_lines；"2" 时写入 OUT.disasm/<address>.json
# sidecar。后者用于大样本，避免主 JSON 膨胀，同时让查看阶段直接读 Ghidra 结果。
WANT_DISASM = len(args) > 5 and str(args[5]) == "1"
# mode "2" writes instruction lines to one sidecar per function.  This keeps
# the main export compact for large samples while retaining a complete Ghidra
# result that can be read without starting Ghidra again.
DISASM_SIDECAR = len(args) > 5 and str(args[5]) == "2"
if DISASM_SIDECAR:
    WANT_DISASM = True
# args[6] is an optional comma-separated retry set.  When present, the
# analysis still opens the binary, but only these failed functions are
# decompiled; the caller merges the subset into the existing full cache.
RETRY_ADDRS = set()
if len(args) > 6 and args[6]:
    for tok in str(args[6]).split(","):
        try:
            RETRY_ADDRS.add(int(tok, 16))
        except (TypeError, ValueError):
            pass
DISASM_MAX_LINES = 5000 if DISASM_SIDECAR else 400
DISASM_DIR = None
if DISASM_SIDECAR:
    try:
        DISASM_DIR = os.path.splitext(OUT)[0] + ".disasm"
        if not os.path.isdir(DISASM_DIR):
            os.makedirs(DISASM_DIR)
    except Exception:
        DISASM_DIR = None

STOP_FLAG = [False]


def _stop_requested():
    if STOP_FLAG[0]:
        return True
    if STOP and os.path.exists(STOP):
        STOP_FLAG[0] = True
        return True
    return False


def _write_analysis_snapshot(mon):
    """Export functions discovered while Ghidra auto-analysis is still running.

    Ghidra's function table is populated incrementally, but the old exporter only
    read it after ``startAnalysis`` returned.  That made the workbench appear
    frozen at 0/0 for large binaries.  The snapshot intentionally contains only
    stable address/name/size fields; decompilation status is pending until the
    function phase begins.
    """
    if not PROGRESS:
        return
    try:
        current = []
        it = fm.getFunctions(True)
        while it.hasNext():
            f = it.next()
            try:
                current.append({"address": f.getEntryPoint().getOffset(),
                                "name": f.getName(),
                                "size": f.getBody().getNumAddresses(),
                                "calls": [], "status": "pending"})
            except Exception:
                pass
        done = int(mon.getProgress() or 0)
        total = int(mon.getMaximum() or 0)
        payload = {"phase": "analyzing", "done": done, "total": total,
                   "completed": 0, "discovered": len(current), "failed": 0}
        with io.open(PROGRESS, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        snapshot = {"export_version": 3, "binary": prog.getName(),
                    "functions": current, "partial": True, "stopped": False,
                    "total_functions": len(current),
                    "meta": {"partial": True, "live": True,
                             "discovered_functions": len(current),
                             "completed_functions": 0,
                             "failed_functions": 0}}
        live = OUT + ".live"
        tmp = live + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot, ensure_ascii=False))
        try:
            os.remove(live)
        except Exception:
            pass
        os.rename(tmp, live)
    except Exception:
        pass


def _write_progress():
    if not PROGRESS:
        return
    try:
        done = 0
        failed = 0
        for it in functions:
            if it.get("status") == "done" or "pseudocode" in it:
                done += 1
            elif it.get("status") == "failed":
                failed += 1
        payload = {"done": done, "completed": done, "discovered": len(functions),
                   "failed": failed, "total": len(functions),
                   "phase": "stopped" if STOP_FLAG[0] else "decompile"}
        with io.open(PROGRESS, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        # Keep a readable partial cache while headless is still running.  The
        # parent process atomically promotes this snapshot to the normal cache,
        # allowing the workbench to show completed functions immediately.
        live = OUT + ".live"
        snapshot = {"export_version": 3, "binary": prog.getName(),
                    "functions": functions, "partial": True,
                    "stopped": False, "total_functions": len(functions),
                    "meta": {"partial": True, "live": True,
                             "discovered_functions": len(functions),
                             "completed_functions": done,
                             "failed_functions": failed}}
        try:
            tmp = live + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, ensure_ascii=False))
            try:
                os.remove(live)
            except Exception:
                pass
            os.rename(tmp, live)
        except Exception:
            pass
    except Exception:
        pass


def _iter_function_instructions(f):
    """Return instruction objects for a function across Ghidra API variants."""
    listing = prog.getListing()
    out = []
    try:
        it = listing.getInstructions(f.getBody(), True)
        while it.hasNext():
            out.append(it.next())
        if out:
            return out
    except Exception:
        pass
    try:
        # Explicit min/max overload avoids a PyGhidra overload resolution bug
        # where passing an AddressSetView silently produces an empty iterator.
        it = listing.getInstructions(f.getBody().getMinAddress(),
                                     f.getBody().getMaxAddress(), True)
        while it.hasNext():
            out.append(it.next())
        if out:
            return out
    except Exception:
        pass
    try:
        it = listing.getCodeUnits(f.getBody().getMinAddress(),
                                  f.getBody().getMaxAddress(), True)
        while it.hasNext():
            out.append(it.next())
        if out:
            return out
    except Exception:
        pass
    # PyGhidra 12 can expose Function.getBody() as an AddressSetView that the
    # Listing iterator accepts but yields no elements. Walk its addresses and
    # ask Listing for the instruction at each address instead.
    try:
        ait = f.getBody().getAddresses(True)
        seen = set()
        while ait.hasNext():
            addr = ait.next()
            ins = listing.getInstructionAt(addr)
            if ins is not None:
                key = str(ins.getAddress())
                if key not in seen:
                    seen.add(key)
                    out.append(ins)
    except Exception:
        pass
    # Last-resort sequential walk. This handles packed functions where the
    # AddressSetView proxy cannot be passed through JPype at all.
    try:
        entry = f.getEntryPoint()
        cur = listing.getInstructionAt(entry)
        if cur is None:
            cur = listing.getCodeUnitAt(entry)
        if cur is None:
            cur = listing.getInstructionContaining(f.getEntryPoint())
        if cur is None:
            # Recreate the Address object from its numeric offset. This avoids
            # a JPype proxy mismatch seen with imported PE address objects.
            space = prog.getAddressFactory().getDefaultAddressSpace()
            entry = space.getAddress(entry.getOffset())
            cur = listing.getInstructionAt(entry)
            if cur is None:
                cur = listing.getCodeUnitAt(entry)
        end = f.getBody().getMaxAddress()
        seen = set()
        while cur is not None and cur.getAddress().compareTo(end) <= 0:
            key = str(cur.getAddress())
            if key in seen:
                break
            seen.add(key)
            out.append(cur)
            cur = listing.getInstructionAfter(cur)
    except Exception:
        pass
    return out


def _disasm_lines(f):
    """指令级反汇编行（`地址  指令`），单函数上限 DISASM_MAX_LINES。返回 (lines, truncated)。"""
    lines = []
    truncated = False
    try:
        instructions = _iter_function_instructions(f)
        for n, ins in enumerate(instructions):
            if n >= DISASM_MAX_LINES:
                truncated = True
                break
            # CodeUnit iterators may include data; retaining their textual form
            # is still more useful than returning an empty detail to the UI.
            try:
                addr = str(ins.getAddress())
            except Exception:
                addr = "?"
            try:
                text = str(ins)
            except Exception:
                try:
                    text = "%s %s" % (ins.getMnemonicString(),
                                       ins.getDefaultOperandRepresentation(0))
                except Exception:
                    text = "<instruction>"
            lines.append("%s  %s" % (addr, text))
    except Exception:
        pass
    return lines, truncated


def _java_values(value):
    """Yield values from Java iterators/collections under Jython and PyGhidra."""
    if value is None:
        return
    try:
        if hasattr(value, "hasNext") and hasattr(value, "next"):
            while value.hasNext():
                yield value.next()
            return
    except Exception:
        return
    try:
        for item in value:
            yield item
    except Exception:
        return


def _called_functions(f, mon):
    try:
        return list(_java_values(f.getCalledFunctions(mon)))
    except Exception:
        return []


def _enrich(f, dec, mon, item):
    # 分片重试（P3）：单函数反编译失败重试一次（瞬时解编译器故障自愈），仍失败则
    # 留 calls=[] 且无伪码，不拖垮整次导出。
    try:
        item["calls"] = sorted(set(c.getName() for c in _called_functions(f, mon)))
        # Some PE/packed functions do not expose calls through the high-level
        # Function API. Fall back to instruction references so xref data is not
        # silently reduced to an empty list.
        if not item["calls"]:
            names = set()
            try:
                refs = prog.getReferenceManager()
                for ins in _iter_function_instructions(f):
                    ref_it = refs.getReferencesFrom(ins.getAddress())
                    while ref_it.hasNext():
                        ref = ref_it.next()
                        try:
                            if not ref.getReferenceType().isCall():
                                continue
                        except Exception:
                            pass
                        dest = ref.getToAddress()
                        callee = fm.getFunctionAt(dest)
                        if callee is None:
                            callee = fm.getFunctionContaining(dest)
                        if callee is not None and callee.getEntryPoint() != f.getEntryPoint():
                            names.add(callee.getName())
                item["calls"] = sorted(names)
            except Exception as exc:
                # Keep the function result, but leave a diagnostic for the
                # exporter log instead of silently hiding a broken xref path.
                print("xref fallback failed %s: %s" % (f.getName(), exc))
    except Exception:
        pass
    if WANT_DISASM:
        try:
            lines, truncated = _disasm_lines(f)
            if DISASM_DIR is not None:
                sidecar = os.path.join(DISASM_DIR, hex(f.getEntryPoint().getOffset()) + ".json")
                with io.open(sidecar, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"lines": lines, "truncated": truncated},
                                        ensure_ascii=False))
            else:
                item["disasm_lines"], item["disasm_truncated"] = lines, truncated
        except Exception:
            pass
    for _ in range(2):
        try:
            res = dec.decompileFunction(f, 60, mon)
            if res.decompileCompleted() and res.getDecompiledFunction() is not None:
                item["pseudocode"] = res.getDecompiledFunction().getC()
                item["status"] = "done"
                return
        except Exception:
            pass
    item["status"] = "failed"
    item["error"] = "伪代码生成失败"


# ---- 脚本自驱分析（2026-10-01 可中断）：-noanalysis 导入后由本脚本跑分析 ----
_ANALYSIS_STOPPED = False
if ANALYZE:
    from ghidra.app.plugin.core.analysis import AutoAnalysisManager
    if not PYGHIDRA:
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
                               "total": int(self.getMaximum() or 0),
                               "completed": 0, "failed": 0}
                    with io.open(PROGRESS, "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(payload, ensure_ascii=False))
                    _write_analysis_snapshot(self)
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
        _analysis_monitor = ConsoleTaskMonitor() if PYGHIDRA else _StopMonitor()
        _analysis_reporter_done = threading.Event()

        def _analysis_reporter():
            while not _analysis_reporter_done.is_set():
                _write_analysis_snapshot(_analysis_monitor)
                _analysis_reporter_done.wait(1.0)

        _analysis_reporter_thread = threading.Thread(target=_analysis_reporter)
        if PROGRESS:
            _analysis_reporter_thread.start()
        try:
            _mgr.startAnalysis(_analysis_monitor)
        finally:
            _analysis_reporter_done.set()
            if PROGRESS:
                try:
                    _analysis_reporter_thread.join(3)
                except Exception:
                    pass
    except Exception as _e:  # noqa: BLE001 —— 分析启动失败回退（不可中断但至少出结果）
        print("script-driven analysis failed, fallback analyzeAll: %s" % _e)
        try:
            analyzeAll(prog)
        except Exception as _e2:
            print("analyzeAll failed: %s" % _e2)
    _ANALYSIS_STOPPED = _stop_requested()

funcs = list(fm.getFunctions(True))
if RETRY_ADDRS:
    funcs = [f for f in funcs
             if f.getEntryPoint().getOffset() in RETRY_ADDRS]
# 基础项预填充（P3）：地址/名/大小先全量落好，worker 只补 calls/伪码——协作式停止时
# 函数清单仍是完整体（名字/地址全量），仅部分缺伪码，便于「停止保留部分」立即可见。
functions = []
for f in funcs:
    try:
        functions.append({"address": f.getEntryPoint().getOffset(),
                          "name": f.getName(),
                          "size": f.getBody().getNumAddresses(),
                          "calls": [], "status": "pending"})
    except Exception:
        functions.append({"address": 0, "name": "", "size": 0, "calls": [],
                          "status": "failed", "error": "函数信息读取失败"})

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
try:
    os.remove(OUT + ".live")
except Exception:
    pass
print("exported %d functions -> %s" % (len(functions), OUT))
