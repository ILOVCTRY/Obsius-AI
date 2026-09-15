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


di = DecompInterface()
di.openProgram(prog)

functions = []
for f in fm.getFunctions(True):
    item = {
        "address": f.getEntryPoint().getOffset(),
        "name": f.getName(),
        "size": f.getBody().getNumAddresses(),
        "calls": sorted(set(c.getName() for c in f.getCalledFunctions(monitor))),
    }
    try:
        res = di.decompileFunction(f, 60, monitor)
        if res.decompileCompleted() and res.getDecompiledFunction() is not None:
            item["pseudocode"] = res.getDecompiledFunction().getC()
    except Exception:
        pass
    functions.append(item)

result = {"export_version": 3, "binary": prog.getName(), "functions": functions}
for key, fn in (("meta", collect_meta), ("sections", collect_sections),
                ("imports", collect_imports), ("strings", collect_strings)):
    try:
        result[key] = fn()
    except Exception as e:
        result[key] = {"error": "%s: %s" % (type(e).__name__, e)}

# 必须显式 UTF-8（Jython 用 io.open）：Windows host 裸 open 默认 GBK，
# 人工中文命名/注释会写成非 UTF-8 字节，后端读缓存即 UnicodeDecodeError。
with io.open(OUT, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(result, ensure_ascii=False))
print("exported %d functions -> %s" % (len(functions), OUT))
